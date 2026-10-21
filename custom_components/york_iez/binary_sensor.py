"""Binary sensor platform for York IEZ — 每台内机的故障状态。

约克云的 ``iu35Alarm`` 是故障码字符串（"00" = 无故障）。
挂成 PROBLEM 类型的 binary_sensor，方便自动化直接判断。
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import IuStatus
from .const import DOMAIN
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

    async_add_entities(
        YorkIezFaultBinarySensor(coordinator, iu_id, gw_id)
        for iu_id in iu_ids
    )


class YorkIezFaultBinarySensor(BinarySensorEntity):
    """一台内机的故障开关量。"""

    _attr_has_entity_name = True
    _attr_name = "故障"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: YorkIezCoordinator,
        iu_id: str,
        gateway_device_id: str | None = None,
    ) -> None:
        self.coordinator = coordinator
        self._iu_id = iu_id
        self._attr_unique_id = (
            f"york_iez_{coordinator.iez_code}_{iu_id}_fault"
        )
        self._attr_device_info = indoor_device_info(
            coordinator, iu_id, gateway_device_id
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def _status(self) -> IuStatus | None:
        return self.coordinator.statuses.get(self._iu_id)

    @property
    def is_on(self) -> bool | None:
        st = self._status
        if st is None:
            return None
        return bool(st.alarm and st.alarm != "00")

    @property
    def available(self) -> bool:
        return self._status is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._status
        if st is None:
            return {}
        return {
            "故障码": st.alarm or "00",
            "内机编号": st.iu_no,
            "原始字段": "iu35Alarm",
        }
