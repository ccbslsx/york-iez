"""Diagnostics support for York IEZ.

诊断导出会公开贴在 issue 里，所以这里做**双层脱敏**：

1. ``_scrub``：把整份载荷里出现的网关序列号（iezCode）前缀抹掉。
   内机 ID 是 ``iezCode + 4 位序号`` 拼的，抹掉前缀后只剩 ``0002``，
   结构仍可读，但序列号不外泄。
2. ``async_redact_data``：按 key 名打码手机号、密码、令牌、SN、
   以及 ``system_meta`` 里的**家庭住址 / 经纬度 / 小区名 / 人名**
   —— 这些是约克云端顺带返回的，不处理会直接暴露用户位置。
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# 凭据 + 序列号
TO_REDACT = {
    "phone",
    "password",
    "token",
    "refresh_token",
    "user_id",
    "iuSn",
    "ouSn",
    "iezCode",
    "iez_code",
    "macImei",
}

# 约克云顺带返回的住址 / 人名（system_meta 里）
TO_REDACT_META = {
    "title",        # entry.title 里带小区名 + 门牌号
    "home_name",
    "position1",
    "position2",
    "positionLng",
    "positionLat",
    "positionTime",
    "importName",
    "importUser",
    "createUser",
    "updateUser",
}

REDACT_ALL = TO_REDACT | TO_REDACT_META


def _scrub(obj: Any, needle: str) -> Any:
    """把载荷里出现的网关序列号前缀抹掉（含字典 key）。"""
    if not needle:
        return obj
    if isinstance(obj, dict):
        return {_scrub(k, needle): _scrub(v, needle) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, needle) for v in obj]
    if isinstance(obj, str):
        return obj.replace(needle, "")
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """导出诊断信息（不含凭据、序列号与住址）。"""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = store.get("coordinator")

    statuses: dict[str, Any] = {}
    online: dict[str, Any] = {}
    if coordinator is not None:
        online = dict(coordinator.online or {})
        for iu_id, st in (coordinator.statuses or {}).items():
            statuses[iu_id] = {
                "onoff": st.onoff,
                "mode": st.mode,
                "wind": st.wind,
                "set_temp": st.set_temp,
                "alarm": st.alarm,
                "t_in": st.t_in,
                "t_out": st.t_out,
                "t_wi": st.t_wi,
                "t_wo": st.t_wo,
                "round_time": st.round_time,
            }

    payload = {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "options": dict(entry.options),
            "data": dict(entry.data),
        },
        "coordinator": {
            "last_update_success": (
                coordinator.last_update_success if coordinator else None
            ),
            "update_interval": (
                str(coordinator.update_interval) if coordinator else None
            ),
            "system_meta": dict(store.get("system_meta") or {}),
        },
        "online": online,
        "statuses": statuses,
    }

    iez_code = str(entry.data.get("iez_code") or "")
    payload = _scrub(payload, iez_code)
    return async_redact_data(payload, REDACT_ALL)
