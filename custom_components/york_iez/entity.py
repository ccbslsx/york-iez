"""Shared device (DeviceInfo) builders for the York IEZ integration.

设备模型（与约克 App 一致）::

    空调机组4636   ← IEZ 网关机型，即「1 个外机」这台设备
    ├── 儿童房     ← 内机 00
    ├── 餐厅       ← 内机 01
    ├── 书房       ← 内机 02
    ├── 主卧       ← 内机 03
    ├── 次卧       ← 内机 04
    └── 客厅       ← 内机 05

所有实体都通过 ``device_info`` 归到对应设备下，不再散落在实体列表里。
内机用 ``via_device_id`` 指回机组，HA 里显示为「机组 → 内机」的层级。

注意：外机（ouList）在约克云侧只有静态元数据，``/appstatus/.../status``
接口带 ouList 也返回空 —— App 本身也从不查外机运行数据。因此外机运行
参数（频率/高压/低压/电流）拿不到，机组设备只暴露在线、故障汇总和
外机型号这三项。
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .coordinator import YorkIezCoordinator

MANUFACTURER = "York 约克"


def clean_text(value: Any) -> str:
    """清洗云端字符串：去掉占位符尾巴。

    约克云的外机型号形如 ``RAS-180------???``，机型位是出厂没填的占位符，
    直接显示很难看。去掉尾部连字符与问号即可。
    """
    text = str(value or "").strip()
    return re.sub(r"[-?]+$", "", text).strip()


def _first_ou(system_meta: dict[str, Any]) -> dict[str, Any]:
    ous = system_meta.get("ouList") or []
    return ous[0] if ous else {}


def gateway_device_info(coordinator: YorkIezCoordinator) -> DeviceInfo:
    """机组（IEZ 网关 / 外机）设备。"""
    meta = coordinator.system_meta or {}
    ou = _first_ou(meta)
    iez = coordinator.iez_code
    name = str(meta.get("iezName") or "").strip() or f"约克空调机组 {iez[-4:]}"
    return DeviceInfo(
        identifiers={(DOMAIN, iez)},
        name=name,
        manufacturer=MANUFACTURER,
        model=clean_text(ou.get("ouType")) or "IEZ 多联机机组",
        sw_version=clean_text(meta.get("version")) or None,
        serial_number=iez,
    )


async def async_ensure_gateway_device(
    hass: HomeAssistant,
    config_entry_id: str,
    coordinator: YorkIezCoordinator,
) -> str:
    """先把机组设备建出来，返回它的 device_id。

    必须在转发平台之前调用：内机设备要用 ``via_device_id`` 挂在机组下面，
    而 ``via_device_id`` 需要真实的注册表 ID。早于平台创建也避免了
    DeviceInfo 里已废弃的 ``via_device`` 元组写法。
    """
    info = gateway_device_info(coordinator)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=config_entry_id,
        identifiers=info["identifiers"],
        name=info["name"],
        manufacturer=info.get("manufacturer"),
        model=info.get("model"),
        sw_version=info.get("sw_version"),
        serial_number=info.get("serial_number"),
    )
    return device.id


def indoor_device_info(
    coordinator: YorkIezCoordinator,
    iu_id: str,
    gateway_device_id: str | None = None,
) -> DeviceInfo:
    """一台内机的设备。设备名 = App 里的房间名。"""
    meta = coordinator.iu_meta.get(iu_id) or {}
    name = str(meta.get("iuName") or "").strip()
    if not name:
        idx = iu_id[-2:] if iu_id[-2:].isdigit() else "00"
        name = f"内机{idx}"
    info = DeviceInfo(
        identifiers={(DOMAIN, iu_id)},
        name=name,
        manufacturer=MANUFACTURER,
        model=clean_text(meta.get("productName")) or "约克聚好看多联机",
        serial_number=iu_id,
    )
    if gateway_device_id:
        info["via_device_id"] = gateway_device_id
    return info


def indoor_meta(coordinator: YorkIezCoordinator, iu_id: str) -> dict[str, Any]:
    """内机静态元数据（名称/功率/温度范围/能力标志），缺失时返回 {}。"""
    return coordinator.iu_meta.get(iu_id) or {}
