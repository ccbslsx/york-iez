"""Scene platform for York IEZ — 把约克云端「定时/场景」重放成 HA 场景。

背景（逆向结论）：约克 App **没有**「执行场景」接口，场景页是把场景里存好的
``iuList[].ctrlJson`` 通过 ``multiIuOuCtrl`` 重放一遍。所以：

* 只导出**带内机指令**的条目（实测家里 14 条里 8 条是这种「定时」）；
* 纯云端推荐场景（离家/回家/睡眠/离家节能/夜间静音/提前暖房，type 1/2/5/6/7/G）
  只有云端模板、没有内机指令，本地无法重放 —— 不导出，避免点了没反应；
* ``scene.turn_on`` 下发的是「现在立刻执行一次」，不会改动它在约克云里的定时
  计划，也不会开/关它的开关状态。

场景列表在集成启动时拉一次（列表接口一次请求就带全部指令），之后由
coordinator 每小时慢刷新；App 里新增/删除了场景，重载集成即可同步出实体。
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import YorkApiError, YorkCommandError
from .const import DOMAIN, MODE_COOL, MODE_FLOOR_HEAT, MODE_HEAT
from .coordinator import YorkIezCoordinator
from .entity import gateway_device_info

_LOGGER = logging.getLogger(__name__)

# 场景里第一条指令的模式 → 图标
_MODE_ICONS = {
    MODE_COOL: "mdi:snowflake",
    MODE_HEAT: "mdi:fire",
    MODE_FLOOR_HEAT: "mdi:heating-coil",
}


def _scene_icon(record: dict[str, Any]) -> str:
    for cmd in record.get("commands") or []:
        ctrl = cmd.get("ctrl_json") or {}
        if ctrl.get("onoff") == 0:
            continue
        mode = ctrl.get("mode")
        if mode is not None and int(mode) in _MODE_ICONS:
            return _MODE_ICONS[int(mode)]
    return "mdi:palette-swatch"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator: YorkIezCoordinator = store["coordinator"]

    if not coordinator.scenes:
        # 首次设置时 __init__ 已拉过一次；为空则再试一次，仍为空就正常收工
        await coordinator.async_load_scenes()
    if not coordinator.scenes:
        _LOGGER.info(
            "York IEZ: 云端没有「带内机指令」的场景/定时，不创建 scene 实体"
        )
        return

    async_add_entities(
        [YorkIezScene(coordinator, record) for record in coordinator.scenes]
    )


class YorkIezScene(Scene):
    """一个可重放的约克场景/定时。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: YorkIezCoordinator, record: dict[str, Any]
    ) -> None:
        self.coordinator = coordinator
        self._record = record
        self._attr_unique_id = (
            f"york_iez_{coordinator.iez_code}_scene_{record.get('smart_id')}"
        )
        self._attr_name = str(record.get("name") or "约克场景")
        self._attr_icon = _scene_icon(record)
        self._attr_device_info = gateway_device_info(coordinator)

    async def async_activate(self, **kwargs: Any) -> None:
        """执行一次（重放场景里存的内机指令）。"""
        try:
            await self.coordinator.async_run_scene(self._record)
        except (YorkApiError, YorkCommandError) as err:
            raise HomeAssistantError(
                f"约克场景「{self._attr_name}」执行失败：{err}"
            ) from err

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cmds = []
        for cmd in self._record.get("commands") or []:
            iu_id = str(cmd.get("iu_id") or "")
            name = (
                (self.coordinator.iu_meta.get(iu_id) or {}).get("iuName")
                or cmd.get("iu_no")
                or iu_id
            )
            cmds.append({"内机": name, "指令": cmd.get("ctrl_json")})
        attrs: dict[str, Any] = {
            "约克场景 ID": self._record.get("smart_id"),
            "内机数": len(cmds),
            "指令": cmds,
        }
        if self._record.get("is_timer"):
            attrs["约克侧为定时"] = True
        return attrs
