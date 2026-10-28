"""Climate platform for York IEZ — one entity per indoor unit."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.climate import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import IuStatus
from .const import (
    DOMAIN,
    FAN_TO_WIND,
    HVAC_TO_MODE,
    MODE_FLOOR_HEAT,
    MODE_FLOOR_HEAT_REPORT,
    MODE_TO_HVAC,
    TEMP_MAX,
    TEMP_MIN,
    TEMP_STEP,
    WIND_AUTO,
    WIND_TO_FAN,
)
from .coordinator import YorkIezCoordinator
from .entity import indoor_device_info, indoor_meta

_LOGGER = logging.getLogger(__name__)

SUPPORT_FLAGS = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TURN_OFF
)

# 约克 IEZ 实际支持：制冷 1 / 制热 4 / 地暖 16。
# auto(0)、dry(8)、fan_only(2) 来自厂商常量，未在本机型实测，
# 若云端拒绝可在 App 侧看到报错（HA 会抛 HomeAssistantError）。
HVAC_MODES = [
    HVACMode.OFF,
    HVACMode.AUTO,
    HVACMode.COOL,
    HVACMode.HEAT,
    HVACMode.DRY,
    HVACMode.FAN_ONLY,
]

FAN_MODES = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]

# 下发后云端大约 1~2 个轮询周期才回读到新值。这段时间内若把乐观值丢掉，
# UI 会先跳到新值再弹回旧值。给一个宽限窗口，直到云端确认或超时。
OPTIMISTIC_GRACE = 20


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
        YorkIezClimate(
            coordinator=coordinator, iu_id=iu_id, gateway_device_id=gw_id
        )
        for iu_id in iu_ids
    )


class YorkIezClimate(ClimateEntity):
    """一台约克 IEZ 内机。"""

    # has_entity_name + name=None → 实体名直接取设备名（房间名），
    # 卡片上不再出现「约克 书房」这种重复前缀。
    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = "°C"
    _attr_supported_features = SUPPORT_FLAGS
    _attr_hvac_modes = HVAC_MODES
    _attr_fan_modes = FAN_MODES
    _attr_min_temp = TEMP_MIN
    _attr_max_temp = TEMP_MAX
    _attr_target_temperature_step = TEMP_STEP
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: YorkIezCoordinator,
        iu_id: str,
        gateway_device_id: str | None = None,
    ) -> None:
        self.coordinator = coordinator
        self._iu_id = iu_id
        self._attr_unique_id = f"york_iez_{coordinator.iez_code}_{iu_id}"
        self._attr_device_info = indoor_device_info(
            coordinator, iu_id, gateway_device_id
        )
        # 下发后的乐观值：只在「还没等到云端回读」这段时间内顶替真实值，
        # 收到新一轮轮询立刻丢弃，避免 UI 先跳过去又弹回来。
        self._optimistic: IuStatus | None = None
        self._optimistic_deadline = 0.0

    async def async_added_to_hass(self) -> None:
        """订阅 coordinator，由它推送状态，实体自身不轮询。"""
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """新数据到达：云端已确认下发值（或宽限期过了）才丢掉乐观值。"""
        if self._optimistic is not None:
            st = self._status
            if st is None:
                self._optimistic = None
            elif self._confirmed_by(st, self._optimistic):
                self._optimistic = None
            elif time.monotonic() >= self._optimistic_deadline:
                self._optimistic = None
        self.async_write_ha_state()

    @staticmethod
    def _confirmed_by(real: IuStatus, want: IuStatus) -> bool:
        """云端回读是否已经和刚才下发的值一致。"""
        return (
            real.onoff == want.onoff
            and real.mode == want.mode
            and real.wind == want.wind
            and real.set_temp == want.set_temp
        )

    # ------------------------------------------------------------- 数据源

    @property
    def _status(self) -> IuStatus | None:
        return self.coordinator.statuses.get(self._iu_id)

    @property
    def _effective(self) -> IuStatus | None:
        """下发瞬间用乐观值，其余时间用 coordinator 的真实值。"""
        return self._optimistic or self._status

    # ------------------------------------------------------------- HVAC 模式

    @property
    def hvac_mode(self) -> HVACMode:
        st = self._effective
        if not st or not st.onoff:
            return HVACMode.OFF
        if st.mode in (MODE_FLOOR_HEAT, MODE_FLOOR_HEAT_REPORT):
            # 地暖在 HA 里也归到 heat，具体是地暖还是热泵由 switch 区分
            return HVACMode.HEAT
        try:
            return HVACMode(MODE_TO_HVAC.get(st.mode, HVACMode.COOL))
        except ValueError:
            return HVACMode.COOL

    async def async_set_hvac_mode(self, hvac_mode: HVACMode | None) -> None:
        st = self._effective
        if hvac_mode == HVACMode.OFF:
            await self._send({"onoff": 0})
            return
        if (
            hvac_mode == HVACMode.HEAT
            and st
            and st.mode in (MODE_FLOOR_HEAT, MODE_FLOOR_HEAT_REPORT)
        ):
            # 已经在烧地暖：只开机，别把它切回热泵制热
            await self._send({"onoff": 1})
            return
        mode = HVAC_TO_MODE.get(str(hvac_mode))
        if mode is None:
            _LOGGER.warning("York IEZ: 不支持的模式 %s", hvac_mode)
            return
        await self._send({"onoff": 1, "mode": mode})

    # ------------------------------------------------------------- 温度

    @property
    def current_temperature(self) -> float | None:
        st = self._effective
        if not st or st.t_in is None:
            return None
        # 统一用回风温度（Ti）：地暖模式下的盘管水温（Twi）是水温不是室温，
        # 拿来当室温会显示成 30~40°C。水温走属性 + 独立传感器。
        return float(st.t_in)

    @property
    def target_temperature(self) -> float | None:
        st = self._effective
        if not st or st.set_temp is None:
            return None
        return float(st.set_temp)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """设定温度。

        HA 的 climate.set_temperature 会把 entity_id / hvac_mode 等一并作为
        kwargs 传进来，所以签名必须是 **kwargs，否则服务调用直接 500
        （TypeError: unexpected keyword argument 'entity_id'）。
        """
        temperature = kwargs.get("temperature")
        if temperature is None:
            temperature = kwargs.get("target_temp")
        if temperature is None:
            _LOGGER.warning("York IEZ set_temperature 缺少 temperature 参数")
            return
        temp = int(round(float(temperature)))
        temp = max(TEMP_MIN, min(TEMP_MAX, temp))
        # 只改设定温度，不改变开关/模式/风量
        await self._send({"temp": temp})

    # ------------------------------------------------------------- 风量

    @property
    def fan_mode(self) -> str | None:
        st = self._effective
        if not st:
            return None
        fan = WIND_TO_FAN.get(st.wind, FAN_AUTO)
        return fan if fan in FAN_MODES else FAN_AUTO

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        wind = FAN_TO_WIND.get(str(fan_mode), WIND_AUTO)
        # 只改风量，不改变开关/模式/温度
        await self._send({"wind": wind})

    # ------------------------------------------------------------- 开关

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send({"onoff": 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send({"onoff": 0})

    # ------------------------------------------------------------- 状态

    @property
    def available(self) -> bool:
        return self._status is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._effective
        if not st:
            return {}
        meta = indoor_meta(self.coordinator, self._iu_id)
        attrs: dict[str, Any] = {
            "内机编号": st.iu_no,
            "系统号": st.system_no,
            "原始模式码": st.mode,
            "故障码": st.alarm,
        }
        if meta.get("capacity") is not None:
            attrs["额定容量"] = meta.get("capacity")
        if meta.get("heatFlg") == "1":
            attrs["支持地暖"] = True
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

    # ------------------------------------------------------------- 下发

    async def _send(self, patch: dict[str, Any]) -> None:
        """下发指令（coordinator 会补全为全量 ctrlJson）。"""
        ctrl_json = self.coordinator.build_ctrl_json(self._iu_id, patch)

        # 乐观更新，避免 UI 回弹
        st = self._status
        patched = dict(st.raw) if st is not None else {}
        patched.update(
            {
                "iu28Onoff": ctrl_json["onoff"],
                "iu29Mode": ctrl_json["mode"],
                "iu31Temp": ctrl_json["temp"],
                "iu30Wind": ctrl_json["wind"],
            }
        )
        self._optimistic = IuStatus.from_dict(patched)
        self._optimistic_deadline = time.monotonic() + OPTIMISTIC_GRACE
        self.async_write_ha_state()

        try:
            await self.coordinator.async_control(self._iu_id, patch)
        except Exception:  # noqa: BLE001
            # 失败 → 撤掉乐观值，立刻显示真实状态
            self._optimistic = None
            self.async_write_ha_state()
            raise
