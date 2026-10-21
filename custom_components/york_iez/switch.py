"""Switch platform for York IEZ — 每台内机的「地暖模式」开关。

约克 IEZ 的 mode=16 是地暖/热水模式，与普通制热（mode=4，热泵制热）
是两套不同的物理系统，HA 的 climate 没有对应 HVACMode，故单独暴露成开关。

ON  = 该内机处于地暖模式且开机
OFF = 关机或非地暖模式
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import IuStatus
from .const import DOMAIN, MODE_FLOOR_HEAT
from .coordinator import YorkIezCoordinator
from .entity import indoor_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator: YorkIezCoordinator = store["coordinator"]
    iu_ids: list[str] = store["iu_ids"]
    gw_id: str | None = store.get("gateway_device_id")

    entities: list[YorkIezFloorHeatSwitch] = []
    for iu_id in iu_ids:
        entities.append(
            YorkIezFloorHeatSwitch(coordinator, iu_id, gw_id)
        )
    async_add_entities(entities)


class YorkIezFloorHeatSwitch(SwitchEntity):
    """一台内机的地暖模式开关。"""

    _attr_has_entity_name = True
    _attr_name = "地暖"
    _attr_icon = "mdi:radiator"

    def __init__(
        self,
        coordinator: YorkIezCoordinator,
        iu_id: str,
        gateway_device_id: str | None = None,
    ) -> None:
        self.coordinator = coordinator
        self._iu_id = iu_id
        self._attr_unique_id = (
            f"york_iez_{coordinator.iez_code}_{iu_id}_floorheat"
        )
        self._attr_device_info = indoor_device_info(
            coordinator, iu_id, gateway_device_id
        )
        self._attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def _status(self) -> IuStatus | None:
        return self.coordinator.statuses.get(self._iu_id)

    @property
    def is_on(self) -> bool:
        st = self._status
        return bool(st and st.onoff and st.mode == MODE_FLOOR_HEAT)

    @property
    def available(self) -> bool:
        return self._status is not None

    async def _send(self, patch: dict[str, Any]) -> None:
        try:
            await self.coordinator.async_control(self._iu_id, patch)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "York IEZ floor-heat %s failed: %s", self._iu_id, err
            )
            raise

    async def async_turn_on(self, **kwargs: Any) -> None:
        # 开机 + 切到地暖模式；温度/风量由 coordinator 用当前值补全
        await self._send({"onoff": 1, "mode": MODE_FLOOR_HEAT})

    async def async_turn_off(self, **kwargs: Any) -> None:
        # 退地暖 = 关机（不擅自改成制冷/制热）
        await self._send({"onoff": 0})
