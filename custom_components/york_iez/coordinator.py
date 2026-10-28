"""DataUpdateCoordinator for York IEZ."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import IuStatus, YorkIezApi
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MODE_COOL,
    SCENE_REFRESH_INTERVAL,
    TEMP_MAX,
    TEMP_MIN,
    WIND_AUTO,
)

_LOGGER = logging.getLogger(__name__)


class YorkIezCoordinator(DataUpdateCoordinator):
    """轮询内机状态 + 网关在线状态。"""

    def __init__(
        self,
        hass: HomeAssistant,
        api: YorkIezApi,
        home_id: int,
        iez_code: str,
        iu_ids: list[str],
        iu_meta: dict[str, dict[str, Any]] | None = None,
        system_meta: dict[str, Any] | None = None,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        self.api = api
        self.home_id = home_id
        self.iez_code = iez_code
        self.iu_ids = iu_ids
        self.iu_meta: dict[str, dict[str, Any]] = iu_meta or {}
        # 机组元数据（iezName / version / ouList / iuCount …），用于设备卡片
        self.system_meta: dict[str, Any] = system_meta or {}
        self.statuses: dict[str, IuStatus] = {}
        self.online: dict[str, Any] = {}
        # 云端场景/定时里「带具体内机指令」的条目，可本地重放成 HA scene。
        # App 里改了场景不会主动推送，所以按 SCENE_REFRESH_INTERVAL 慢刷新。
        self.scenes: list[dict[str, Any]] = []
        self._scenes_at: float = 0.0
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> dict[str, IuStatus]:
        statuses = await self.api.get_status(
            self.home_id, self.iez_code, self.iu_ids
        )
        self.statuses = statuses
        try:
            self.online = await self.api.get_online(self.iez_code)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("York IEZ online poll failed: %s", err)
        if time.monotonic() - self._scenes_at > SCENE_REFRESH_INTERVAL:
            await self.async_load_scenes()
        return statuses

    # ------------------------------------------------------------- 场景

    async def async_load_scenes(self) -> list[dict[str, Any]]:
        """（慢刷新）拉取可重放的云端场景/定时。失败不抛出，沿用旧列表。"""
        try:
            scenes = await self.api.get_replayable_scenes(self.home_id, self.iu_meta)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("York IEZ: 场景列表刷新失败（沿用旧数据）: %s", err)
            return self.scenes
        self._scenes_at = time.monotonic()
        if [s.get("smart_id") for s in scenes] != [
            s.get("smart_id") for s in self.scenes
        ]:
            _LOGGER.info(
                "York IEZ: 可重放场景 %d 个%s",
                len(scenes),
                "（列表有变化，重载集成可同步实体）" if self.scenes else "",
            )
        self.scenes = scenes
        return scenes

    async def async_apply_ctrl_json(
        self, iu_id: str, ctrl_json: dict[str, Any]
    ) -> None:
        """把一份 ctrlJson 乐观写回内存状态（选中即生效，UI 不回弹）。"""
        st = self.statuses.get(iu_id)
        if st is None:
            return
        if ctrl_json.get("onoff") is not None:
            st.onoff = int(ctrl_json["onoff"])
        if ctrl_json.get("mode") is not None:
            st.mode = int(ctrl_json["mode"])
        if ctrl_json.get("wind") is not None:
            st.wind = int(ctrl_json["wind"])
        if ctrl_json.get("temp") is not None:
            st.set_temp = float(ctrl_json["temp"])

    async def async_run_scene(self, record: dict[str, Any]) -> None:
        """重放一个约克场景/定时：一次请求下发它存的所有内机指令。"""
        items: list[tuple[dict[str, Any], dict[str, Any]]] = []
        applied: list[tuple[str, dict[str, Any]]] = []
        for cmd in record.get("commands") or []:
            iu_id = str(cmd.get("iu_id") or "")
            meta = dict(self.iu_meta.get(iu_id) or {})
            if not meta:
                # 元数据缺失时用场景自带的身份字段兜底（iuNo/iuType/systemNo/iuSn）
                meta = {
                    "systemNo": cmd.get("system_no") or "00",
                    "iuNo": cmd.get("iu_no") or "",
                    "iuType": cmd.get("iu_type") or "05",
                    "iuSn": cmd.get("iu_sn") or "",
                }
            ctrl_json = dict(cmd.get("ctrl_json") or {})
            if not ctrl_json:
                continue
            items.append((meta, ctrl_json))
            applied.append((iu_id, ctrl_json))

        if not items:
            raise HomeAssistantError(
                f"场景「{record.get('name')}」没有可重放的内机指令"
            )

        _LOGGER.debug(
            "York IEZ: 重放场景 %s -> %s", record.get("name"), record.get("commands")
        )
        await self.api.control_ius(self.home_id, self.iez_code, items)
        for iu_id, ctrl_json in applied:
            await self.async_apply_ctrl_json(iu_id, ctrl_json)
        await self.async_request_refresh()

    # ------------------------------------------------------------- 控制

    def build_ctrl_json(
        self, iu_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """用当前状态拼出完整四要素，再叠加改动项。

        只发改动项时云端可能重置其它字段（例如改风量把模式打回制冷），
        所以每次都发全量。
        """
        st = self.statuses.get(iu_id)
        # 注意：mode=0（自动）与 wind=0（自动）都是合法值，
        # 不能用真值判断回退，否则自动模式会被悄悄改成制冷。
        full: dict[str, Any] = {
            "onoff": st.onoff if st is not None else 0,
            "mode": st.mode if st is not None else MODE_COOL,
            "temp": (
                int(round(float(st.set_temp)))
                if st is not None and st.set_temp is not None
                else 26
            ),
            "wind": st.wind if st is not None else WIND_AUTO,
        }
        full["temp"] = max(TEMP_MIN, min(TEMP_MAX, full["temp"]))
        full.update(patch)
        return full

    async def async_control(
        self, iu_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """下发控制（自动补全为全量 ctrlJson）并立即刷新。"""
        ctrl_json = self.build_ctrl_json(iu_id, patch)
        meta = self.iu_meta.get(iu_id) or {}
        if not meta:
            _LOGGER.warning(
                "York IEZ: %s 缺少内机元数据（systemNo/iuNo/iuType），控制可能被拒",
                iu_id,
            )
        _LOGGER.debug("York IEZ control %s -> %s", iu_id, ctrl_json)
        await self.api.control_iu(
            self.home_id, self.iez_code, meta, ctrl_json
        )
        # 先把下发值写回内存状态：否则「刚开机又立刻调温」时，
        # build_ctrl_json 仍拿着上一次轮询的 onoff=0，会把空调又关掉。
        await self.async_apply_ctrl_json(iu_id, ctrl_json)
        await self.async_request_refresh()
        return ctrl_json
