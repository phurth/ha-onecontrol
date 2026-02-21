"""Constants for the OneControl BLE integration."""

DOMAIN = "onecontrol"

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

DISCOVERY_SERVICE_UUID = f"00000041{UUID_BASE}"

# ---------------------------------------------------------------------------
# Manufacturer / Advertisement
# ---------------------------------------------------------------------------
LIPPERT_MANUFACTURER_ID = 0x0499  # 1177 decimal — Lippert Components

# ---------------------------------------------------------------------------
# TEA Encryption Constants
# ---------------------------------------------------------------------------
TEA_DELTA: int = 0x9E3779B9
TEA_CONSTANT_1: int = 0x43729561  # 1131376761
TEA_CONSTANT_2: int = 0x7265746E  # 1919510376
TEA_CONSTANT_3: int = 0x7421ED44  # 1948272964
TEA_CONSTANT_4: int = 0x5378A963  # 1400073827
TEA_ROUNDS: int = 32

# Cipher for Step 1 (UNLOCK_STATUS / Data Service auth) — no PIN
STEP1_CIPHER: int = 0x9E3779B9 ^ 0xBAB3486C  # = 0x248431D5

# Cipher for Step 2 (SEED / Auth Service auth) — includes PIN
STEP2_CIPHER: int = 0x8100080D

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
# Config entry data keys
# ---------------------------------------------------------------------------
CONF_GATEWAY_PIN = "gateway_pin"
CONF_BLUETOOTH_PIN = "bluetooth_pin"
CONF_PAIRING_METHOD = "pairing_method"
