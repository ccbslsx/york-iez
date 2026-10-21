"""Config flow for York IEZ."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.data_entry_flow import FlowResult

from .api import YorkAuthError, YorkApiError, YorkIezApi, YorkLoginError
from .const import (
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    ERROR_CANNOT_CONNECT,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEL)
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


def _empty() -> dict[str, Any]:
    return {
        "iez_code": "",
        "iu_ids": [],
        "iu_names": {},
        "iu_meta": {},
    }


async def _discover(api: YorkIezApi, home_id: int) -> dict[str, Any]:
    """从 homeDetail 拿 IEZ 网关 + 内机清单（唯一权威数据源）。

    GET /api/apphome/homes/{id} -> data.homeDetail.deviceList[]：
      iezCode / type("3"=IEZ) / iuCount / iuList[]（iuId/iuName/iuNo/
      iuType/systemNo/heatFlg…）

    注意：/api/apphome/homes/homeList/deviceList 返回的字段名是
    allHomeDeviceList（不是 homeList），且那里也拿不到 iuId，故弃用。
    """
    system_meta, iu_meta = await api.get_home_bundle(home_id)
    iez_code = str(system_meta.get("iezCode") or "")
    if not iez_code:
        _LOGGER.warning("York IEZ: homeDetail 里没有 IEZ 网关（iezCode 为空）")
        return _empty()

    iu_ids = list(iu_meta.keys())
    if not iu_ids:
        # 兜底：iuList 缺失时按 iuCount 推 iuId（iezCode + 4 位序号）
        iu_count = int(system_meta.get("iuCount") or 0)
        iu_ids = [f"{iez_code}{i:04d}" for i in range(iu_count)]

    iu_names = {k: v["iuName"] for k, v in iu_meta.items() if v.get("iuName")}

    _LOGGER.info(
        "York IEZ 发现完成：iezCode=%s，网关=%s，内机 %d 台 %s",
        iez_code,
        system_meta.get("iezName"),
        len(iu_ids),
        [iu_names.get(i) or i for i in iu_ids],
    )

    return {
        "iez_code": iez_code,
        "iu_ids": iu_ids,
        "iu_names": iu_names,
        "iu_meta": iu_meta,
    }


class YorkIezConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for York IEZ."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            phone = user_input[CONF_PHONE].strip()
            password = user_input[CONF_PASSWORD]
            api = YorkIezApi(
                async_get_clientsession(self.hass),
                phone=phone,
                password=password,
            )
            try:
                sess = await api.login()
            except (YorkAuthError, YorkLoginError):
                errors["base"] = ERROR_AUTH_FAILED
            except YorkApiError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during York IEZ login")
                errors["base"] = ERROR_CANNOT_CONNECT
            else:
                if not sess.home_id:
                    errors["base"] = ERROR_CANNOT_CONNECT
                else:
                    # 去重：同一家庭只允许一个条目
                    await self.async_set_unique_id(f"york_iez_{sess.home_id}")
                    self._abort_if_unique_id_configured()

                    try:
                        devices = await _discover(api, int(sess.home_id))
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("York IEZ discovery failed")
                        devices = _empty()

                    return self.async_create_entry(
                        title=f"约克 IEZ · {sess.home_name or sess.home_id}",
                        data={
                            "phone": phone,
                            "password": password,
                            "token": sess.token,
                            "user_id": sess.user_id,
                            "refresh_token": sess.refresh_token,
                            "login_version": sess.login_version,
                            "home_id": sess.home_id,
                            "home_name": sess.home_name,
                            **devices,
                        },
                    )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    # ------------------------------------------------------------- 重新认证

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """约克改密码 / 账号被顶掉时，从「重新认证」直接改密码。"""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id", "")
        )
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        saved_phone = entry.data.get(CONF_PHONE, "")

        if user_input is not None:
            phone = user_input[CONF_PHONE].strip() or saved_phone
            password = user_input[CONF_PASSWORD]
            api = YorkIezApi(
                async_get_clientsession(self.hass),
                phone=phone,
                password=password,
            )
            try:
                sess = await api.login()
            except (YorkAuthError, YorkLoginError):
                errors["base"] = ERROR_AUTH_FAILED
            except YorkApiError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during York IEZ reauth")
                errors["base"] = ERROR_CANNOT_CONNECT
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_PHONE: phone,
                        CONF_PASSWORD: password,
                        "token": sess.token,
                        "refresh_token": sess.refresh_token,
                        "login_version": sess.login_version,
                        "user_id": sess.user_id,
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PHONE, default=saved_phone): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEL)
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            description_placeholders={"phone": saved_phone},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return YorkIezOptionsFlow()


class YorkIezOptionsFlow(config_entries.OptionsFlow):
    """选项：轮询间隔。改完即时生效，不重载集成。"""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=current
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=5,
                            unit_of_measurement="秒",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )
