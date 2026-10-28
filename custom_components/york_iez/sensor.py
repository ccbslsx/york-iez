"""Sensor platform for York IEZ.

实体归属：::

    机组设备（IEZ 网关 / 外机）: 在线、故障汇总、外机型号、安装信息
    每台内机设备            : 室内温度、额定容量

以往这些实体散在顶层，没有设备归属；现在每个都带 ``device_info``，
在 HA 里按「1 个机组 + 6 台内机」的结构呈现。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import IuStatus
from .const import DOMAIN
from .coordinator import YorkIezCoordinator
from .entity import gateway_device_info, indoor_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator: YorkIezCoordinator = store["coordinator"]
    iez_code: str = store["iez_code"]
    iu_ids: list[str] = store["iu_ids"]
    gw_id: str | None = store.get("gateway_device_id")

    entities: list[SensorEntity] = [
        YorkIezOnlineSensor(coordinator, iez_code),
        YorkIezAlarmSensor(coordinator, iez_code),
        YorkIezOUModelSensor(coordinator, iez_code),
        YorkIezInstallInfoSensor(coordinator, iez_code),
    ]
    for iu_id in iu_ids:
        entities.append(
            YorkIezIndoorTempSensor(coordinator, iu_id, gw_id)
        )
        entities.append(YorkIezCapacitySensor(coordinator, iu_id, gw_id))
    async_add_entities(entities)


class _YorkIezBaseSensor(SensorEntity):
    """订阅 coordinator，自身不轮询。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: YorkIezCoordinator,
        key: str,
        device_info: DeviceInfo,
        unique_prefix: str,
    ) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{unique_prefix}_{key}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )


# --------------------------------------------------------------- 机组设备


class YorkIezOnlineSensor(_YorkIezBaseSensor):
    """机组在线状态（约克云 ↔ IEZ 网关的连通性）。"""

    _attr_name = "在线"
    _attr_icon = "mdi:cloud-check-variant"

    def __init__(self, coordinator: YorkIezCoordinator, iez_code: str) -> None:
        super().__init__(
            coordinator,
            "online",
            gateway_device_info(coordinator),
            f"york_iez_{iez_code}",
        )

    @property
    def native_value(self) -> str:
        if not self.coordinator.online:
            return "unknown"
        return "在线" if self.coordinator.online.get("online") else "离线"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        o = self.coordinator.online
        meta = self.coordinator.system_meta or {}
        attrs: dict[str, Any] = {}
        if o:
            attrs.update(
                {
                    "response_code": o.get("responseCode"),
                    "redis_online": o.get("redisOnline"),
                    "最后上报": o.get("redisUpdateTimeStr"),
                }
            )
        if meta.get("iezCode"):
            attrs["网关编号"] = meta.get("iezCode")
        if meta.get("iuCount") is not None:
            attrs["内机数量"] = meta.get("iuCount")
        if meta.get("ouCount") is not None:
            attrs["外机数量"] = meta.get("ouCount")
        if meta.get("regionCode"):
            attrs["区域代码"] = meta.get("regionCode")
        return attrs


class YorkIezAlarmSensor(_YorkIezBaseSensor):
    """全机组故障汇总（哪台内机有故障码）。"""

    _attr_name = "故障汇总"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: YorkIezCoordinator, iez_code: str) -> None:
        super().__init__(
            coordinator,
            "alarm",
            gateway_device_info(coordinator),
            f"york_iez_{iez_code}",
        )

    @property
    def native_value(self) -> str:
        faulty: list[str] = []
        for st in self.coordinator.statuses.values():
            if st.alarm and st.alarm != "00":
                name = (self.coordinator.iu_meta.get(st.iu_id) or {}).get(
                    "iuName"
                ) or f"内机{st.iu_no}"
                faulty.append(f"{name}({st.alarm})")
        return "、".join(faulty) if faulty else "正常"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        codes: dict[str, str] = {}
        for st in self.coordinator.statuses.values():
            if st.alarm and st.alarm != "00":
                name = (self.coordinator.iu_meta.get(st.iu_id) or {}).get(
                    "iuName"
                ) or f"内机{st.iu_no}"
                codes[name] = st.alarm
        return {"故障内机数": len(codes), "故障清单": codes or {}}


class YorkIezOUModelSensor(_YorkIezBaseSensor):
    """外机型号（约克云只有静态元数据，无外机运行参数）。"""

    _attr_name = "外机型号"
    _attr_icon = "mdi:hvac"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YorkIezCoordinator, iez_code: str) -> None:
        super().__init__(
            coordinator,
            "ou_model",
            gateway_device_info(coordinator),
            f"york_iez_{iez_code}",
        )

    @property
    def native_value(self) -> str:
        ous = (self.coordinator.system_meta or {}).get("ouList") or []
        ou = ous[0] if ous else {}
        return str(ou.get("ouType") or "").strip() or "未知"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        meta = self.coordinator.system_meta or {}
        ous = meta.get("ouList") or []
        ou = ous[0] if ous else {}
        attrs: dict[str, Any] = {}
        for label, key in (
            ("外机编号", "ouNo"),
            ("系统号", "systemNo"),
            ("额定容量", "capacity"),
            ("外机类型码", "ou23Type"),
            ("电量计量", "elecFlg"),
        ):
            if ou.get(key) not in (None, ""):
                attrs[label] = ou.get(key)
        if meta.get("version"):
            attrs["固件版本"] = meta.get("version")
        if meta.get("versionModbus"):
            attrs["Modbus 版本"] = meta.get("versionModbus")
        if meta.get("versionModbusProtocol") is not None:
            attrs["Modbus 协议号"] = meta.get("versionModbusProtocol")
        if meta.get("capacityRates"):
            attrs["容量率"] = meta.get("capacityRates")
        if meta.get("period") is not None:
            attrs["上报周期秒"] = meta.get("period")
        return attrs


class YorkIezInstallInfoSensor(_YorkIezBaseSensor):
    """装机与位置信息（diagnostic，含隐私，默认折叠）。"""

    _attr_name = "装机信息"
    _attr_icon = "mdi:wrench-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YorkIezCoordinator, iez_code: str) -> None:
        super().__init__(
            coordinator,
            "install_info",
            gateway_device_info(coordinator),
            f"york_iez_{iez_code}",
        )

    @staticmethod
    def _ts(v: Any) -> str | None:
        if isinstance(v, (int, float)) and v > 10**11:
            return time.strftime("%Y-%m-%d", time.localtime(v / 1000))
        return None

    @property
    def native_value(self) -> str:
        meta = self.coordinator.system_meta or {}
        bind = self._ts(meta.get("bindTime"))
        return f"绑定于 {bind}" if bind else "未知"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        meta = self.coordinator.system_meta or {}
        attrs: dict[str, Any] = {}
        for label, key in (
            ("省份市区", "position1"),
            ("小区", "position2"),
            ("区域代码", "regionCode"),
            ("品牌代码", "brand"),
        ):
            if meta.get(key) not in (None, ""):
                attrs[label] = meta.get(key)
        if self._ts(meta.get("bindTime")):
            attrs["绑定时间"] = self._ts(meta.get("bindTime"))
        if self._ts(meta.get("heartTime")):
            attrs["最后心跳"] = self._ts(meta.get("heartTime"))
        if meta.get("otaFlg") is not None:
            attrs["固件可升级"] = str(meta.get("otaFlg")) == "1"
        if meta.get("autoOta") is not None:
            attrs["自动升级"] = str(meta.get("autoOta")) == "1"
        return attrs


# --------------------------------------------------------------- 内机设备


class YorkIezIndoorTempSensor(_YorkIezBaseSensor):
    """内机室内温度（回风温度 Ti）。

    独立成 sensor 而不再只做 climate 属性：这样可以进长期统计、
    画历史曲线、参与自动化数值比较。
    """

    _attr_name = "室内温度"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "°C"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: YorkIezCoordinator,
        iu_id: str,
        gateway_device_id: str | None = None,
    ) -> None:
        super().__init__(
            coordinator,
            "temp_in",
            indoor_device_info(coordinator, iu_id, gateway_device_id),
            f"york_iez_{coordinator.iez_code}_{iu_id}",
        )
        self._iu_id = iu_id

    @property
    def _status(self) -> IuStatus | None:
        return self.coordinator.statuses.get(self._iu_id)

    @property
    def native_value(self) -> float | None:
        st = self._status
        if st is None or st.t_in is None:
            return None
        return float(st.t_in)

    @property
    def available(self) -> bool:
        return self._status is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._status
        if st is None:
            return {}
        attrs: dict[str, Any] = {
            "数据来源": "回风温度 iu39Ti",
        }
        if st.t_out is not None:
            attrs["出风温度"] = st.t_out
        if st.t_wi is not None:
            attrs["盘管进水温度"] = st.t_wi
        if st.t_wo is not None:
            attrs["盘管出水温度"] = st.t_wo
        if st.round_time:
            attrs["上报时间"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(st.round_time / 1000)
            )
        return attrs


class YorkIezCapacitySensor(_YorkIezBaseSensor):
    """内机额定容量（制冷量，kW）。

    来自约克云 ``homeDetail.deviceList[].iuList[].capacity``。
    恒为静态值，不随运行变化，仅用于判断各内机大小/配型。
    """

    _attr_name = "额定容量"
    _attr_icon = "mdi:air-conditioner"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "kW"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: YorkIezCoordinator,
        iu_id: str,
        gateway_device_id: str | None = None,
    ) -> None:
        super().__init__(
            coordinator,
            "capacity",
            indoor_device_info(coordinator, iu_id, gateway_device_id),
            f"york_iez_{coordinator.iez_code}_{iu_id}",
        )
        self._iu_id = iu_id

    @property
    def _meta(self) -> dict[str, Any]:
        return self.coordinator.iu_meta.get(self._iu_id) or {}

    @property
    def native_value(self) -> float | None:
        cap = self._meta.get("capacity")
        try:
            return None if cap is None else float(cap)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        meta = self._meta
        attrs: dict[str, Any] = {}
        if meta.get("iuNo"):
            attrs["内机编号"] = meta.get("iuNo")
        if meta.get("iuType"):
            attrs["内机类型码"] = meta.get("iuType")
        for label, key in (
            ("制冷下限", "coldMin"),
            ("制冷上限", "coldMax"),
            ("制热下限", "heatMin"),
            ("制热上限", "heatMax"),
        ):
            if meta.get(key) is not None:
                attrs[label] = meta.get(key)
        if meta.get("heatFlg"):
            attrs["支持地暖"] = str(meta.get("heatFlg")) == "1"
        if meta.get("halfSupport"):
            attrs["支持半度温控"] = str(meta.get("halfSupport")) == "1"
        return attrs
