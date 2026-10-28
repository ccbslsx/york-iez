"""The York IEZ integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Session, YorkIezApi
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import YorkIezCoordinator
from .entity import async_ensure_gateway_device

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["climate", "sensor", "binary_sensor", "switch", "scene"]


def _scan_interval(entry: ConfigEntry) -> int:
    """读轮询间隔，越界自动夹紧。"""
    try:
        value = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_INTERVAL
    return max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, value))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    auth = Session(
        token=data.get("token", ""),
        user_id=int(data.get("user_id") or 0),
        refresh_token=data.get("refresh_token"),
        login_version=int(data.get("login_version") or 0),
        phone=data.get("phone", ""),
        home_id=data.get("home_id"),
        home_name=data.get("home_name", ""),
    )
    api = YorkIezApi(
        async_get_clientsession(hass),
        phone=data.get("phone", ""),
        password=data.get("password", ""),
        auth=auth,
    )

    def _persist_session(sess: Session) -> None:
        """登录/续期成功后把新 token 写回 config_entry。

        不写的话每次 HA 重启都会拿着过期 token 撞 401，再退回「用密码登录」，
        既慢又容易把手机 App 的登录态顶掉。refresh_token 也会因为长期不更新
        而失去轮换机会。
        """
        current = entry.data
        if (
            current.get("token") == sess.token
            and current.get("refresh_token") == sess.refresh_token
            and current.get("login_version") == sess.login_version
        ):
            return
        hass.config_entries.async_update_entry(
            entry,
            data={
                **current,
                "token": sess.token,
                "refresh_token": sess.refresh_token,
                "login_version": sess.login_version,
                "user_id": sess.user_id,
            },
        )
        _LOGGER.debug("York IEZ: 已持久化新的登录态")

    api.on_session_update = _persist_session

    iez_code = data.get("iez_code") or ""
    home_id = int(data["home_id"])
    iu_ids = list(data.get("iu_ids") or [])
    iu_meta: dict[str, dict[str, Any]] = dict(data.get("iu_meta") or {})
    iu_names: dict[str, str] = dict(data.get("iu_names") or {})
    system_meta: dict[str, Any] = {}

    # 每次启动都重新拉一次 homeDetail：
    #  1) 自愈老 entry（早期发现逻辑有 bug，iez_code/iu_meta 可能为空），
    #     免去用户删条目重填密码；
    #  2) 拿到机组元数据（iezName / 外机型号 / 固件版本）用于设备卡片；
    #  3) App 里改了房间名，这里能跟上。
    try:
        system_meta, fresh_meta = await api.get_home_bundle(home_id)
        if fresh_meta:
            iu_meta = fresh_meta
            iu_names = {
                k: v["iuName"] for k, v in iu_meta.items() if v.get("iuName")
            }
            if not iu_ids:
                iu_ids = list(iu_meta.keys())
        if not iez_code:
            iez_code = str(system_meta.get("iezCode") or "")
    except Exception:  # noqa: BLE001
        _LOGGER.exception("York IEZ: 设备发现失败，沿用已保存的入口数据")

    if iez_code and (
        iez_code != data.get("iez_code")
        or iu_meta != (data.get("iu_meta") or {})
        or iu_names != (data.get("iu_names") or {})
        or iu_ids != list(data.get("iu_ids") or [])
    ):
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                "iez_code": iez_code,
                "iu_ids": iu_ids,
                "iu_names": iu_names,
                "iu_meta": iu_meta,
            },
        )
        _LOGGER.info(
            "York IEZ: 已更新入口数据 iezCode=%s，内机 %d 台",
            iez_code,
            len(iu_ids),
        )

    if not iez_code or not iu_ids:
        _LOGGER.error(
            "York IEZ: 未能发现网关或内机（iezCode=%r, iu_ids=%d）",
            iez_code,
            len(iu_ids),
        )
        raise ConfigEntryNotReady(
            f"未能发现约克 IEZ 网关或内机（iezCode={iez_code!r}）"
        )

    coordinator = YorkIezCoordinator(
        hass,
        api,
        home_id,
        iez_code,
        iu_ids,
        iu_meta,
        system_meta,
        scan_interval=_scan_interval(entry),
    )
    # 机组设备先建出来，内机设备要用它的 id 做 via_device_id
    gateway_device_id = await async_ensure_gateway_device(
        hass, entry.entry_id, coordinator
    )
    await coordinator.async_config_entry_first_refresh()
    # 场景/定时列表：启动时拉一次（多条明细请求，故不放在轮询里高频打）
    await coordinator.async_load_scenes()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "iez_code": iez_code,
        "home_id": home_id,
        "iu_ids": iu_ids,
        "iu_names": iu_names,
        "iu_meta": iu_meta,
        "system_meta": system_meta,
        "gateway_device_id": gateway_device_id,
    }

    # 选项（轮询间隔）改动即时生效：不重载条目，避免打断实体与乐观状态。
    # 注意：token 持久化也会触发 update_listener，所以这里必须幂等、不能写日志噪音。
    async def _async_options_updated(
        hass_: HomeAssistant, entry_: ConfigEntry
    ) -> None:
        store = hass_.data.get(DOMAIN, {}).get(entry_.entry_id)
        if not store:
            return
        interval = _scan_interval(entry_)
        coord: YorkIezCoordinator = store["coordinator"]
        if coord.update_interval != timedelta(seconds=interval):
            coord.update_interval = timedelta(seconds=interval)
            _LOGGER.debug("York IEZ: 轮询间隔改为 %ss", interval)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return ok
