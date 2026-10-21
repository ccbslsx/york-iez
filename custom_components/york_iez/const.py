"""Constants for the York IEZ integration."""

DOMAIN = "york_iez"
TITLE = "York IEZ 公建云"

# ---------------------------------------------------------------- API 配置

API_BASE: str = "https://1app.hicloud.hisensehitachi.com"
API_KEY: str = "61PdDpLyYzpctYwwVpbHRb2"
APP_ID: str = "com.hisensehitachi.iyes2"
APP_TAG: str = "V1"
APP_VERSION: str = "1.8.2.260807104325_release"
DEFAULT_OS: str = "Android"
DEFAULT_LOCALE: str = "zh_CN"

# ---------------------------------------------------------------- 登录

LOGIN_PATH = "/login"

# ---------------------------------------------------------------- 接口路径

PATH_STATUS = "/api/appstatus/homes/{home_id}/status"
PATH_ONLINE = "/api/appcontrol/cmds/multiOnlineStatus"
PATH_CONTROL = "/api/appcontrol/cmds/multiIuOuCtrl"
PATH_SMARTS = "/api/apptimer/homes/{home_id}/smarts"
PATH_DEVICE_LIST = "/api/apphome/homes/homeList/deviceList"
PATH_HOME_DETAIL = "/api/apphome/homes/{home_id}"
PATH_USER_HOMES = "/api/apphome/users/{user_id}/homes"

# ---------------------------------------------------------------- 设备常量

IEZ_DEVICE_TYPE = "3"
IEZ_BRAND = "3"

# 单设备控制的 ctrlType（App 的 SINGLE_CONTROL）
CTRL_TYPE_SINGLE = "YO420"

# 模式编码（约克 IEZ）
MODE_COOL = 1
MODE_HEAT = 4
MODE_FAN = 2
MODE_DRY = 8
MODE_AUTO = 0
MODE_FLOOR_HEAT = 16   # 地暖 / 热水

# 模式码 → HA hvac_mode 映射
MODE_TO_HVAC: dict[int, str] = {
    MODE_AUTO: "auto",
    MODE_COOL: "cool",
    MODE_DRY: "dry",
    MODE_FAN: "fan_only",
    MODE_HEAT: "heat",
    MODE_FLOOR_HEAT: "heat",
}
HVAC_TO_MODE: dict[str, int] = {
    "off": None,
    "auto": MODE_AUTO,
    "cool": MODE_COOL,
    "dry": MODE_DRY,
    "fan_only": MODE_FAN,
    "heat": MODE_HEAT,
}

# 风量编码
WIND_AUTO = 0
WIND_LOW = 1
WIND_MID = 2
WIND_HIGH = 3

WIND_TO_FAN: dict[int, str] = {
    WIND_AUTO: "auto",
    WIND_LOW: "low",
    WIND_MID: "medium",
    WIND_HIGH: "high",
}
FAN_TO_WIND: dict[str, int] = {
    "auto": WIND_AUTO,
    "low": WIND_LOW,
    "medium": WIND_MID,
    "high": WIND_HIGH,
}

# ---------------------------------------------------------------- 温度

TEMP_MIN = 16
TEMP_MAX = 32
TEMP_STEP = 1.0

# ---------------------------------------------------------------- 轮询

DEFAULT_SCAN_INTERVAL = 60          # 状态轮询（秒）
MIN_SCAN_INTERVAL = 15              # 下限：别把约克云打爆
MAX_SCAN_INTERVAL = 600             # 上限
ONLINE_SCAN_INTERVAL = 300          # 在线状态轮询（秒）
COMMAND_DELAY = 1.5                 # 控制后等待（秒），与集成一致

# ---------------------------------------------------------------- 字段名（status）

F_ROUND_TIME = "roundTime"
F_IEZ_CODE = "iezCode"
F_IU_ID = "iuId"
F_IU_NO = "iuNo"
F_SYSTEM_NO = "systemNo"
F_IU_TYPE = "iuType"
F_ONOFF = "iu28Onoff"
F_MODE = "iu29Mode"
F_WIND = "iu30Wind"
F_SET_TEMP = "iu31Temp"
F_HUMI = "iu32Humi"
F_ALARM = "iu35Alarm"
F_T_OUT = "iu38To"
F_T_IN = "iu39Ti"
F_TWI = "iu44Twi"
F_TWO = "iu44Two"

# ---------------------------------------------------------------- 配置键

CONF_PHONE = "phone"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"

# ---------------------------------------------------------------- 错误码

ERROR_AUTH_FAILED = "invalid_auth"
ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_UNKNOWN = "unknown"
