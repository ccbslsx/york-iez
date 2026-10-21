"""约克 IEZ 公建云 API client（只读/控制，逆向自约克智慧家 App）。

设计原则：
- 登录态自动恢复与续期（token → refresh_token → 重新登录）
- 错误信息绝不带 URL 和密码
- 所有请求带 App 同款 X-His-* 头
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import (
    API_BASE,
    API_KEY,
    APP_ID,
    APP_TAG,
    APP_VERSION,
    CTRL_TYPE_SINGLE,
    DEFAULT_LOCALE,
    DEFAULT_OS,
    IEZ_DEVICE_TYPE,
    LOGIN_PATH,
)

_LOGGER = logging.getLogger(__name__)


class YorkAuthError(Exception):
    """凭据被拒绝（密码错误 / 被其它设备顶掉）。"""


class YorkLoginError(Exception):
    """登录接口返回了业务错误码。"""


class YorkApiError(Exception):
    """普通 API 错误（不含敏感信息）。"""


class YorkCommandError(Exception):
    """控制指令下发失败。"""


@dataclass
class Session:
    """登录态，可持久化进 config_entry。"""

    token: str
    user_id: int
    refresh_token: str | None = None
    login_version: int = 0
    phone: str = ""
    home_id: int | None = None
    home_name: str = ""


@dataclass
class IuStatus:
    """一台内机的状态。"""

    iu_id: str
    iu_no: str
    system_no: str = "00"
    iu_type: str = "05"
    onoff: int = 0
    mode: int = 0
    wind: int = 0
    set_temp: float | None = None
    humi: int = 0
    alarm: str = "00"
    t_out: int | None = None
    t_in: int | None = None
    t_wi: int | None = None
    t_wo: int | None = None
    round_time: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IuStatus:
        return cls(
            iu_id=str(d.get("iuId", "")),
            iu_no=str(d.get("iuNo", "")),
            system_no=str(d.get("systemNo", "00")),
            iu_type=str(d.get("iuType", "05")),
            onoff=int(d.get("iu28Onoff") or 0),
            mode=int(d.get("iu29Mode") or 0),
            wind=int(d.get("iu30Wind") or 0),
            set_temp=d.get("iu31Temp"),
            humi=int(d.get("iu32Humi") or 0),
            alarm=str(d.get("iu35Alarm") or "00"),
            t_out=d.get("iu38To"),
            t_in=d.get("iu39Ti"),
            t_wi=d.get("iu44Twi"),
            t_wo=d.get("iu44Two"),
            round_time=int(d.get("roundTime") or 0),
            raw=d,
        )


class YorkIezApi:
    """约克 IEZ 云客户端。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        phone: str = "",
        password: str = "",
        auth: Session | None = None,
    ) -> None:
        self._session = session
        self._phone = phone
        self._password = password
        self.auth = auth
        self._client_id = secrets.token_hex(10)[:19]
        self._login_lock = asyncio.Lock()
        self._last_renewal = 0.0
        # 登录/续期成功后回调，供上层把新 token 持久化进 config_entry，
        # 避免 HA 每次重启都退化成「用密码重新登录」。
        self.on_session_update: Callable[[Session], None] | None = None

    # ------------------------------------------------------------- 请求层

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-His-AppId": APP_ID,
            "X-His-AppTag": APP_TAG,
            "X-His-APIKey": API_KEY,
            "X-His-Version": APP_VERSION,
            "X-His-OS": DEFAULT_OS,
            "X-His-Locale": DEFAULT_LOCALE,
            "X-His-Timestamp": str(int(time.time() * 1000)),
        }
        if self.auth and self.auth.token:
            h["Authorization"] = f"Bearer {self.auth.token}"
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """发一次请求并解出信封（{code,message,data}）。

        抛 YorkApiError；不含 URL/响应体，避免泄露密码。
        """
        url = f"{API_BASE}{path}"
        status = 0
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                status = resp.status
                text = await resp.text()
        except asyncio.TimeoutError as err:
            raise YorkApiError("timeout") from err
        except aiohttp.ClientError as err:
            raise YorkApiError(f"connect: {type(err).__name__}") from err

        try:
            envelope = json.loads(text)
        except ValueError as err:
            raise YorkApiError(f"bad response (http {status})") from err

        if not isinstance(envelope, dict):
            raise YorkApiError("bad response shape")

        code = str(envelope.get("code", ""))
        if code != "200":
            if code in ("204", "401", "403"):
                raise YorkAuthError(str(envelope.get("message") or f"code {code}"))
            raise YorkApiError(str(envelope.get("message") or f"code {code}"))
        return envelope

    async def _authed(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        """带登录态恢复的请求。"""
        if not self.auth or not self.auth.token:
            await self._ensure_login()
        try:
            return await self._request(
                method, path, params=params, json_body=json_body
            )
        except YorkAuthError:
            if not retry:
                raise
            _LOGGER.debug("York IEZ: session rejected, re-login")
            await self._relogin()
            return await self._request(
                method, path, params=params, json_body=json_body
            )

    # ------------------------------------------------------------- 登录

    def _accept_login(self, data: Any) -> Session:
        if not isinstance(data, dict):
            raise YorkLoginError("login: response has no session")
        user = data.get("user") or {}
        token = user.get("token")
        if not token:
            raise YorkLoginError("login: no token")
        homes = data.get("homeList") or []
        home = homes[0] if homes else {}
        sess = Session(
            token=token,
            user_id=int(user.get("userId") or 0),
            refresh_token=user.get("refreshToken") or None,
            login_version=int(user.get("version") or 0),
            phone=self._phone,
            home_id=home.get("homeId"),
            home_name=home.get("homeName") or "",
        )
        self.auth = sess
        if self.on_session_update is not None:
            try:
                self.on_session_update(sess)
            except Exception:  # noqa: BLE001 - 持久化失败不影响本次调用
                _LOGGER.debug("York IEZ: token 持久化回调失败", exc_info=True)
        return sess

    async def _login_request(self, path: str, params: dict[str, Any]) -> Session:
        envelope = await self._request(
            "GET", path, params={**params, "jgRegId": self._client_id}
        )
        return self._accept_login(envelope.get("data"))

    async def _ensure_login(self) -> Session:
        async with self._login_lock:
            if self.auth and self.auth.token:
                return self.auth
            await self._relogin()
            assert self.auth is not None
            return self.auth

    async def _relogin(self) -> Session:
        """优先 refresh_token，失败则用密码。"""
        if self.auth and self.auth.refresh_token:
            try:
                return await self._login_request(
                    "/smsLogin",
                    {
                        "phoneNo": self._phone,
                        "version": self.auth.login_version,
                        "refreshToken": self.auth.refresh_token,
                    },
                )
            except (YorkLoginError, YorkApiError) as err:
                _LOGGER.debug("York IEZ: refresh failed (%s), falling back", err)
        if not self._password:
            raise YorkAuthError("no password configured")
        version = self.auth.login_version if self.auth else 0
        return await self._login_request(
            LOGIN_PATH,
            {"phoneNo": self._phone, "password": self._password, "version": version},
        )

    async def login(self) -> Session:
        """显式登录（config_flow 用）。"""
        if not self._password:
            raise YorkAuthError("no password configured")
        return await self._login_request(
            LOGIN_PATH,
            {"phoneNo": self._phone, "password": self._password, "version": 0},
        )

    # ------------------------------------------------------------- 状态

    async def get_status(
        self, home_id: int, iez_code: str, iu_ids: list[str]
    ) -> dict[str, IuStatus]:
        """拉取内机状态。timestamp=0 取当前值。"""
        body = {
            "boxList": [],
            "homeId": home_id,
            "iuIdList": [
                {
                    "iezCode": iez_code,
                    "iuIds": [{"iuId": x, "timestamp": 0} for x in iu_ids],
                }
            ],
            "xkqListGjy": [],
            "iuIdListGjy": [],
        }
        envelope = await self._authed(
            "POST", f"/api/appstatus/homes/{home_id}/status", json_body=body
        )
        data = envelope.get("data") or {}
        out: dict[str, IuStatus] = {}
        for group in data.get("statusList") or []:
            for v in group.get("values") or []:
                st = IuStatus.from_dict(v)
                if st.iu_id:
                    out[st.iu_id] = st
        return out

    async def get_online(self, iez_code: str) -> dict[str, Any]:
        """网关在线状态。"""
        envelope = await self._authed(
            "POST",
            "/api/appcontrol/cmds/multiOnlineStatus",
            json_body={"ctrlList": [{"iezCode": iez_code, "deviceType": "3"}]},
        )
        data = envelope.get("data") or []
        return data[0] if data else {}

    async def get_smarts(self, home_id: int) -> list[dict[str, Any]]:
        """场景列表。"""
        envelope = await self._authed(
            "GET", f"/api/apptimer/homes/{home_id}/smarts"
        )
        return (envelope.get("data") or {}).get("smartList") or []

    async def get_home_detail(self, home_id: int) -> dict[str, Any]:
        """家庭详情，含 deviceList[].iuList（内机名称/功率/温度范围）。"""
        envelope = await self._authed("GET", f"/api/apphome/homes/{home_id}")
        return (envelope.get("data") or {}).get("homeDetail") or {}

    async def get_iu_names(self, home_id: int) -> dict[str, str]:
        """iuId -> iuName 映射。"""
        meta = await self.get_iu_meta(home_id)
        return {k: v["iuName"] for k, v in meta.items() if v.get("iuName")}

    async def get_home_bundle(
        self, home_id: int
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """一次请求拿到（机组元数据, iuId -> 内机元数据）。

        机组元数据 = homeDetail.deviceList[] 里 type=3 的那台（IEZ 网关/机组），
        去掉 iuList 后保留 iezCode / iezName / version / ouList / iuCount 等；
        外机型号取自 ouList[0].ouType，用于机组设备卡片展示。
        """
        detail = await self.get_home_detail(home_id)
        devices = detail.get("deviceList") or []
        dev = next(
            (d for d in devices if str(d.get("type")) == "3"),
            devices[0] if devices else {},
        )
        system_meta = {k: v for k, v in dev.items() if k != "iuList"}
        iu_meta = self._parse_iu_meta(dev)
        return system_meta, iu_meta

    async def get_iu_meta(self, home_id: int) -> dict[str, dict[str, Any]]:
        """iuId -> 内机元数据（名称/编号/类型/功率/温度范围/sn）。

        这些字段是控制所必需的：比控制接口要 {systemNo, iuSn, iuNo, iuType}。
        """
        _, out = await self.get_home_bundle(home_id)
        return out

    @staticmethod
    def _parse_iu_meta(dev: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """从机组 device 字典解析内机元数据。"""
        out: dict[str, dict[str, Any]] = {}
        iez_code = str(dev.get("iezCode") or "")
        for iu in dev.get("iuList") or []:
            iu_id = str(iu.get("iuId") or "")
            if not iu_id:
                continue
            out[iu_id] = {
                "iezCode": iez_code,
                "iuName": str(iu.get("iuName") or ""),
                "iuNo": str(iu.get("iuNo") or ""),
                "iuType": str(iu.get("iuType") or "05"),
                "systemNo": str(iu.get("systemNo") or "00"),
                "iuSn": str(iu.get("iuSn") or ""),
                "productName": str(iu.get("productName") or ""),
                "capacity": iu.get("capacity"),
                "coldMin": iu.get("coldMin"),
                "coldMax": iu.get("coldMax"),
                "heatMin": iu.get("heatMin"),
                "heatMax": iu.get("heatMax"),
                "heatFlg": str(iu.get("heatFlg") or ""),
                "halfSupport": str(iu.get("halfSupport") or ""),
                "humiMode": str(iu.get("humiMode") or ""),
                "lifeWaterHeat": str(iu.get("lifeWaterHeat") or ""),
            }
        return out

    # ------------------------------------------------------------- 控制

    async def control_iu(
        self,
        home_id: int,
        iez_code: str,
        iu_meta: dict[str, str],
        ctrl_json: dict[str, Any],
    ) -> None:
        """控制单台内机。

        约克 App 的 IEZ 控制体（逆向自 york_smart_home/api.py 的 run_scene
        + scenes.py 的 _iu_ctrl_list，官方 App 同款）：

            {"homeId":id,"ctrlType":"YO420",
             "ctrlList":[{"iuCtrlInfo":[{systemNo,iuSn,iuNo,iuType,ctrlJson}],
                          "ouCtrlInfo":[],"iezCode":...,"deviceType":"3"}],
             "xkqCtrlList":[]}

        ctrlJson 字段：onoff(0/1), mode(1冷/4热/16地暖), temp(16-32), wind(0-3)
        """
        info: dict[str, Any] = {
            "systemNo": str(iu_meta.get("systemNo", "00")),
            "iuNo": str(iu_meta.get("iuNo", "")),
            "iuType": str(iu_meta.get("iuType", "05")),
            "ctrlJson": ctrl_json,
        }
        iu_sn = iu_meta.get("iuSn")
        if iu_sn:
            info["iuSn"] = str(iu_sn)

        await self._authed(
            "POST",
            "/api/appcontrol/cmds/multiIuOuCtrl",
            json_body={
                "homeId": home_id,
                "ctrlType": CTRL_TYPE_SINGLE,
                "ctrlList": [
                    {
                        "iuCtrlInfo": [info],
                        "ouCtrlInfo": [],
                        "iezCode": iez_code,
                        "deviceType": IEZ_DEVICE_TYPE,
                    }
                ],
                "xkqCtrlList": [],
            },
        )
