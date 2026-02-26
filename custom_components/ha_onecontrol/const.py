"""Constants for the OneControl BLE integration."""

DOMAIN = "ha_onecontrol"

# ---------------------------------------------------------------------------
# BLE Service & Characteristic UUIDs
# ---------------------------------------------------------------------------
UUID_BASE = "-0200-a58e-e411-afe28044e62c"

AUTH_SERVICE_UUID = f"00000010{UUID_BASE}"
SEED_CHAR_UUID = f"00000011{UUID_BASE}"
UNLOCK_STATUS_CHAR_UUID = f"00000012{UUID_BASE}"
KEY_CHAR_UUID = f"00000013{UUID_BASE}"
AUTH_STATUS_CHAR_UUID = f"00000014{UUID_BASE}"

DATA_SERVICE_UUID = f"00000030{UUID_BASE}"
DATA_WRITE_CHAR_UUID = f"00000033{UUID_BASE}"
DATA_READ_CHAR_UUID = f"00000034{UUID_BASE}"

CAN_SERVICE_UUID = f"00000000{UUID_BASE}"
CAN_WRITE_CHAR_UUID = f"00000001{UUID_BASE}"

DISCOVERY_SERVICE_UUID = f"00000041{UUID_BASE}"

# ---------------------------------------------------------------------------
# Manufacturer / Advertisement
# ---------------------------------------------------------------------------
LIPPERT_MANUFACTURER_ID = 0x0499  # 1177 decimal — Lippert Components

# ---------------------------------------------------------------------------
# TEA Encryption Constants (public / standard)
# ---------------------------------------------------------------------------
TEA_DELTA: int = 0x9E3779B9  # Standard TEA delta
TEA_ROUNDS: int = 32

# Proprietary key-schedule and cipher constants are intentionally NOT stored
# here in plaintext.  They are derived at runtime in protocol/tea.py.

# ---------------------------------------------------------------------------
# Default gateway PIN (from sticker)
# ---------------------------------------------------------------------------
DEFAULT_GATEWAY_PIN = "090336"

# ---------------------------------------------------------------------------
# Timing (seconds)
# ---------------------------------------------------------------------------
AUTH_TIMEOUT = 10.0
UNLOCK_VERIFY_DELAY = 0.5
NOTIFICATION_ENABLE_DELAY = 0.2
BLE_MTU_SIZE = 185
HEARTBEAT_INTERVAL = 5.0  # GetDevices keepalive (Android: 5000ms)
LOCKOUT_CLEAR_THROTTLE = 5.0  # Minimum time between lockout clear attempts
RECONNECT_BACKOFF_BASE = 5.0  # Initial reconnect delay (doubles per failure)
RECONNECT_BACKOFF_CAP = 120.0  # Maximum reconnect delay
STALE_CONNECTION_TIMEOUT = 300.0  # 5 min without events → force reconnect

# ---------------------------------------------------------------------------
# Event Types (MyRvLink Protocol — first byte of decoded COBS frame)
# ---------------------------------------------------------------------------
EVENT_GATEWAY_INFORMATION = 0x01
EVENT_DEVICE_COMMAND = 0x02
EVENT_DEVICE_ONLINE_STATUS = 0x03
EVENT_DEVICE_LOCK_STATUS = 0x04
EVENT_RELAY_BASIC_LATCHING_1 = 0x05
EVENT_RELAY_BASIC_LATCHING_2 = 0x06
EVENT_RV_STATUS = 0x07
EVENT_DIMMABLE_LIGHT = 0x08
EVENT_RGB_LIGHT = 0x09
EVENT_GENERATOR_GENIE = 0x0A
EVENT_HVAC_STATUS = 0x0B
EVENT_TANK_SENSOR = 0x0C
EVENT_HBRIDGE_1 = 0x0D
EVENT_HBRIDGE_2 = 0x0E
EVENT_HOUR_METER = 0x0F
EVENT_LEVELER = 0x10
EVENT_SESSION_STATUS = 0x1A
EVENT_TANK_SENSOR_V2 = 0x1B
EVENT_REAL_TIME_CLOCK = 0x20

# ---------------------------------------------------------------------------
# Command Types (for outbound command builder)
# ---------------------------------------------------------------------------
CMD_GET_DEVICES = 0x01
CMD_GET_DEVICES_METADATA = 0x02
CMD_ACTION_SWITCH = 0x40
CMD_ACTION_HBRIDGE = 0x41
CMD_ACTION_GENERATOR = 0x42
CMD_ACTION_DIMMABLE = 0x43
CMD_ACTION_RGB = 0x44
CMD_ACTION_HVAC = 0x45

# ---------------------------------------------------------------------------
# HVAC mode constants (from INTERNALS.md § HVAC Command)
# ---------------------------------------------------------------------------
HVAC_MODE_OFF = 0
HVAC_MODE_HEAT = 1
HVAC_MODE_COOL = 2
HVAC_MODE_HEAT_COOL = 3
HVAC_MODE_SCHEDULE = 4

HVAC_SOURCE_GAS = 0
HVAC_SOURCE_HEAT_PUMP = 1

HVAC_FAN_AUTO = 0
HVAC_FAN_HIGH = 1
HVAC_FAN_LOW = 2

# Pending command guard windows (seconds) — match Android plugin constants
HVAC_PENDING_WINDOW_S = 8.0           # suppress stale echoes after mode/fan command
HVAC_SETPOINT_PENDING_WINDOW_S = 20.0  # wider window for setpoint changes (+ retries)
HVAC_PRESET_PENDING_WINDOW_S = 70.0   # extended guard for preset (delayed echoes arrive 30-60s later)

# Setpoint debounce and retry
HVAC_SETPOINT_DEBOUNCE_S = 0.25
HVAC_SETPOINT_RETRY_DELAY_S = 5.0
HVAC_SETPOINT_MAX_RETRIES = 3

# Observed capability bitmask bits (accumulated from status events)
HVAC_CAP_GAS = 0x01
HVAC_CAP_AC = 0x02
HVAC_CAP_HEAT_PUMP = 0x04
HVAC_CAP_MULTISPEED_FAN = 0x08

# Heat source preset names (match Android / HA climate preset_mode)
HVAC_PRESET_GAS = "Prefer Gas"
HVAC_PRESET_HEAT_PUMP = "Prefer Heat Pump"
HVAC_PRESET_NONE = "none"

# ---------------------------------------------------------------------------
# Cover status byte values (state-only, no commands — INTERNALS.md § Cover)
# ---------------------------------------------------------------------------
COVER_STOPPED = 0xC0
COVER_OPENING = 0xC2
COVER_CLOSING = 0xC3

# ---------------------------------------------------------------------------
# Metadata protocol constants (INTERNALS.md § Device Metadata Retrieval)
# ---------------------------------------------------------------------------
METADATA_PROTOCOL_HOST = 1
METADATA_PROTOCOL_IDS_CAN = 2
METADATA_PAYLOAD_SIZE_FULL = 17

# ---------------------------------------------------------------------------
# Config entry data keys
# ---------------------------------------------------------------------------
CONF_GATEWAY_PIN = "gateway_pin"
CONF_BLUETOOTH_PIN = "bluetooth_pin"
CONF_PAIRING_METHOD = "pairing_method"
