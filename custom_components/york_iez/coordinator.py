"""DataUpdateCoordinator for York IEZ."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import IuStatus, YorkIezApi
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MODE_COOL,
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
        return statuses

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
        st = self.statuses.get(iu_id)
        if st is not None:
            if "onoff" in ctrl_json:
                st.onoff = int(ctrl_json["onoff"])
            if ctrl_json.get("mode") is not None:
                st.mode = int(ctrl_json["mode"])
            if ctrl_json.get("wind") is not None:
                st.wind = int(ctrl_json["wind"])
            if ctrl_json.get("temp") is not None:
                st.set_temp = float(ctrl_json["temp"])
        await self.async_request_refresh()
        return ctrl_json
