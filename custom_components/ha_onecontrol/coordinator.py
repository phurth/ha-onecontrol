"""Coordinator for OneControl BLE gateway communication.

Manages the BLE connection lifecycle:
  1. Connect via HA Bluetooth (supports ESPHome BT proxy)
  2. Request MTU
  3. Step 1 auth (UNLOCK_STATUS challenge → KEY write)
  4. Enable notifications (DATA_READ, SEED)
  5. Step 2 auth (SEED notification → 16-byte KEY write)
  6. Request device metadata (GetDevicesMetadata 500ms after GatewayInfo)
  7. Stream COBS-decoded events to entity callbacks

Reference: INTERNALS.md § Authentication Flow, § Device Metadata Retrieval
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from bleak import BleakClient, BleakGATTCharacteristic, BleakScanner
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .ble_agent import (
    PinAgentContext,
    async_get_local_adapter_macs,
    async_is_locally_bonded,
    is_pin_pairing_supported,
    pair_push_button,
    prepare_pin_agent,
    prepare_push_button_agent,
    remove_bond,
)
from .const import (
    CONF_ADVERTISED_GATEWAY_VERSION,
    AUTH_SERVICE_UUID,
    BLE_MTU_SIZE,
    CAN_READ_CHAR_UUID,
    CAN_VERSION_CHAR_UUID,
    CAN_WRITE_CHAR_UUID,
    CONF_BLUETOOTH_PIN,
    PASSWORD_UNLOCK_CHAR_UUID,
    CONF_BONDED_SOURCE,
    CONF_GATEWAY_FAMILY,
    CONF_GATEWAY_PIN,
    CONF_PAIRING_METHOD,
    DATA_READ_CHAR_UUID,
    DATA_SERVICE_UUID,
    DATA_WRITE_CHAR_UUID,
    DEFAULT_GATEWAY_PIN,
    DOMAIN,
    GATEWAY_FAMILY_LEGACY,
    GATEWAY_FAMILY_X180T,
    HEARTBEAT_INTERVAL,
    HVAC_CAP_AC,
    HVAC_CAP_GAS,
    HVAC_CAP_HEAT_PUMP,
    HVAC_CAP_MULTISPEED_FAN,
    HVAC_PENDING_WINDOW_S,
    HVAC_PRESET_PENDING_WINDOW_S,
    HVAC_SETPOINT_MAX_RETRIES,
    HVAC_SETPOINT_PENDING_WINDOW_S,
    HVAC_SETPOINT_RETRY_DELAY_S,
    KEY_CHAR_UUID,
    LOCKOUT_CLEAR_THROTTLE,
    NOTIFICATION_ENABLE_DELAY,
    RECONNECT_BACKOFF_BASE,
    RECONNECT_BACKOFF_CAP,
    SEED_CHAR_UUID,
    STALE_CONNECTION_TIMEOUT,
    UNLOCK_STATUS_CHAR_UUID,
    UNLOCK_VERIFY_DELAY,
)
from .protocol.cobs import CobsByteDecoder, cobs_encode
from .protocol.commands import CommandBuilder
from .protocol.ids_can_wire import (
    compose_ids_can_extended_wire_frame,
    compose_ids_can_standard_wire_frame,
    decode_ids_can_payload,
    format_ids_can_payload,
    ids_can_message_type_name,
    ids_can_response_name,
    parse_ids_can_wire_frame,
)
from .protocol.events import (
    CoverStatus,
    DeviceLock,
    DeviceMetadata,
    DeviceOnline,
    DimmableLight,
    GatewayInformation,
    GeneratorStatus,
    HourMeter,
    HvacZone,
    LevelerStatus,
    RealTimeClock,
    RelayStatus,
    RgbLight,
    RvStatus,
    SystemLockout,
    TankAlert,
    TankLevel,
    parse_event,
    parse_metadata_response,
)
from .protocol.dtc_codes import get_name as dtc_get_name, is_fault as dtc_is_fault
from .protocol.function_names import get_friendly_name
from .protocol.tea import (
    CAN_BLE_KEY_SEED_CIPHER,
    RC_CYPHER,
    X180T_KEY_SEED_CIPHER,
    calculate_can_ble_key_seed_key,
    calculate_step1_key,
    calculate_step2_key,
    tea_encrypt,
)

_LOGGER = logging.getLogger(__name__)

_MAX_PENDING_GET_DEVICES_CMDIDS = 128
_STARTUP_BOOTSTRAP_WAIT_SECONDS = 8.0
# Initial backoff between bootstrap retry attempts (doubles each attempt).
_STARTUP_BOOTSTRAP_BACKOFF_SECONDS = 1.0
# Maximum backoff between bootstrap retry attempts.
_STARTUP_BOOTSTRAP_MAX_BACKOFF_SECONDS = 30.0
# Total wall-clock seconds to keep retrying bootstrap before giving up.
# Covers gateways that need minutes to fully boot after a power cycle.
_STARTUP_BOOTSTRAP_TIMEOUT_SECONDS = 600.0

# Seconds to wait after metadata loads before seeding entities for silent
# (always-off) devices.  Lets the initial BLE event burst settle first.
_METADATA_SEED_DELAY_S = 2.0

# Function codes for definite on/off relay loads that are NEVER dimmable or RGB.
# Light function codes are intentionally excluded: any light can be wired as a
# relay, dimmable, or RGB device — we cannot tell from function code alone which
# event type the hardware will emit.  Only codes where we are certain the device
# is always a simple relay (pumps, heaters, fans-as-relay) belong here.
_RELAY_SEED_FUNCTION_CODES: frozenset[int] = frozenset({
    5,    # Water Pump
    6,    # Bath Vent
    167,  # Fireplace
    191,  # Fuel Pump
    264,  # Fan
    265,  # Bath Fan
    266,  # Rear Fan
    267,  # Front Fan
    268,  # Kitchen Fan
    269,  # Ceiling Fan
    270,  # Tank Heater
    295,  # Water Heater
    296,  # Water Heaters
    303,  # Waste Valve
    381,  # Holding Tanks Heater
    398,  # Computer Fan
    399,  # Battery Fan
})


def _device_key(table_id: int, device_id: int) -> str:
    """Canonical string key for a (table, device) pair."""
    return f"{table_id:02x}:{device_id:02x}"


@dataclass
class PendingHvacCommand:
    """State of an in-flight HVAC BLE command used by the pending guard and retry logic."""

    table_id: int
    device_id: int
    heat_mode: int
    heat_source: int
    fan_mode: int
    low_trip_f: int
    high_trip_f: int
    is_setpoint_change: bool
    is_preset_change: bool
    sent_at: float        # time.monotonic() timestamp of last send
    retry_count: int = 0


def _decode_v2_ble_can_frames(raw: bytes) -> list[bytes]:
    """Decode a V2 BLE CAN notification into one or more IDS-CAN wire frames.

    V2 BLE notification types (byte 0):
      1 (Packed)       → 4 synthetic 11-bit IDS-CAN frames per device CAN advertisement
      2 (ElevenBit)    → 1 reconstructed 11-bit IDS-CAN frame
      3 (TwentyNineBit) → 1 reconstructed 29-bit IDS-CAN frame

    Returns an empty list when the notification is not in V2 format.

    Parity: decompiled BleCommunicationsAdapter.OnDataReceived (IDS.Portable.CAN).
    """
    if not raw or raw[0] not in (0x01, 0x02, 0x03):
        return []

    v2_type = raw[0]

    if v2_type == 0x01:  # Packed
        if len(raw) < 19:
            return []
        device_addr = raw[1]
        network_status = raw[2]
        ids_can_version = raw[3]
        mac = bytes(raw[4:10])
        product_id = bytes(raw[10:12])
        product_instance = raw[12]
        device_type = raw[13]
        function_name = bytes(raw[14:16])
        instance_byte = raw[16]
        device_caps = raw[17]
        status_len = raw[18]
        status_data = bytes(raw[19:19 + status_len])
        return [
            # NETWORK (11-bit type=0): [DLC=8][0][addr][status][version][mac6]
            bytes([8, 0x00, device_addr, network_status, ids_can_version]) + mac,
            # DEVICE_ID (11-bit type=2): [DLC=8][2][addr][prodId2][prodInst][devType][funcName2][inst][caps]
            bytes([8, 0x02, device_addr]) + product_id
            + bytes([product_instance, device_type]) + function_name
            + bytes([instance_byte, device_caps]),
            # DEVICE_STATUS (11-bit type=3): [DLC=statusLen][3][addr][statusData]
            bytes([status_len, 0x03, device_addr]) + status_data,
            # FakeCircuitID (11-bit type=1): [DLC=4][1][addr][0,0,0,0]
            bytes([4, 0x01, device_addr, 0, 0, 0, 0]),
        ]

    if v2_type == 0x02:  # ElevenBit
        if len(raw) < 6:
            return []
        msg_type_byte = raw[3]
        device_addr = raw[4]
        dlc = raw[5]
        data = bytes(raw[6:6 + dlc])
        return [bytes([dlc, msg_type_byte, device_addr]) + data]

    # v2_type == 0x03: TwentyNineBit
    if len(raw) < 6:
        return []
    dlc = raw[5]
    id4 = bytes(raw[1:5])
    data = bytes(raw[6:6 + dlc])
    return [bytes([dlc]) + id4 + data]


def _official_can_ble_gateway_version_from_part(data: bytes) -> str:
    """Decode official CAN-BLE gateway version from software part char bytes.

    Parity: ``GatewayVersionExtensions.GetGatewayVersionFromCharacteristic``.
    The characteristic is eight bytes; bytes 0..4 contain BCD-like decimal
    nibbles for part number and byte 6 is the revision character.

    The observed Unity/X1 CAN-BLE bridge reports software part ``24955-G``
    (descriptor: Bluetooth Gateway Daughter Board XT Assembly).  This part is
    absent from the decompiled app's older characteristic lookup table, but it
    behaves like the app's V1 gateway selection while still requiring explicit
    REMOTE_CONTROL seed/key before relay COMMAND frames are accepted.
    """
    if len(data) != 8:
        return "Unknown"
    part_number = (
        (data[0] & 0x0F) * 10000
        + (data[1] & 0x0F) * 1000
        + (data[2] & 0x0F) * 100
        + (data[3] & 0x0F) * 10
        + (data[4] & 0x0F)
    )
    rev = chr(data[6])
    result = "Unknown"
    for known_part, min_rev, version in (
        (20707, "F", "V1"),
        (24955, "A", "V1"),
        (23357, "A", "V2"),
        (23357, "D", "V2_D"),
    ):
        if part_number == known_part and rev >= min_rev:
            result = version
    return result


class OneControlCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate BLE communication with a OneControl gateway."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.unique_id}",
            update_interval=timedelta(seconds=5),
            always_update=True,
        )
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.gateway_pin: str = entry.data.get(CONF_GATEWAY_PIN, DEFAULT_GATEWAY_PIN)

        # ── PIN-based pairing (MyRVLink PIN gateways) ─────────────────
        self._pairing_method: str = entry.data.get(CONF_PAIRING_METHOD, "push_button")
        self._gateway_family: str = entry.data.get(
            CONF_GATEWAY_FAMILY, GATEWAY_FAMILY_LEGACY
        )
        self._instance_tag: str = f"{id(self):x}"[-6:]
        # Android uses gateway_pin for both BLE bonding AND protocol auth.
        # bluetooth_pin is an optional override if the BLE PIN differs.
        # For X180T, BLE pairing is Just Works, so don't use gateway_pin as bluetooth_pin
        bluetooth_pin_default = "" if self._gateway_family == GATEWAY_FAMILY_X180T else self.gateway_pin
        self._bluetooth_pin: str = entry.data.get(
            CONF_BLUETOOTH_PIN, bluetooth_pin_default
        )
        if self.is_x180t_gateway and self._pairing_method != "pin":
            if self._bluetooth_pin:
                _LOGGER.warning(
                    "X180T push-button gateway %s ignoring configured bluetooth_pin; using Just Works pairing",
                    self.address,
                )
            self._bluetooth_pin = ""
        self._pin_agent_ctx: PinAgentContext | None = None  # active D-Bus agent context
        self._pin_dbus_succeeded: bool = False  # bonding completed this session
        self._pin_already_bonded: bool = False  # BlueZ "already bonded" seen (sticky — not reset on disconnect)
        self._push_button_dbus_ok: bool = False
        # Source of the adapter/proxy used for the most recent HA-routed connect
        # attempt.  Persisted to config entry options after successful step-1 auth
        # so subsequent connects are pinned to the same adapter (bond affinity).
        self._current_connect_source: str | None = None

        self._client: BleakClient | None = None
        self._decoder = CobsByteDecoder(use_crc=True)
        self._cmd = CommandBuilder()
        self._authenticated = False
        self._connected = False
        self._connect_lock = asyncio.Lock()
        # Per-table metadata tracking (replaces single _metadata_requested bool)
        self._metadata_requested_tables: set[int] = set()
        self._metadata_loaded_tables: set[int] = set()
        self._metadata_rejected_tables: set[int] = set()
        self._metadata_retry_counts: dict[int, int] = {}   # table_id → 0x0f retry count
        self._metadata_retry_pending: set[int] = set()      # table_ids with a retry task in flight
        self._pending_metadata_cmdids: dict[int, int] = {}  # cmdId → table_id
        self._pending_metadata_entries: dict[int, dict[str, DeviceMetadata]] = {}
        self._pending_get_devices_cmdids: dict[int, int] = {}  # cmdId → table_id
        self._get_devices_loaded_tables: set[int] = set()
        self._get_devices_reject_counts: dict[int, int] = {}  # table_id → consecutive rejection count
        self._bootstrap_waiters: dict[tuple[str, int], asyncio.Future[str]] = {}
        self._startup_bootstrap_task: asyncio.Task | None = None
        self._startup_bootstrap_table_id: int | None = None
        self._unknown_command_counts: dict[int, int] = {}
        self._cmd_correlation_stats: dict[str, int] = {
            "metadata_success_multi_accepted": 0,
            "metadata_success_multi_discarded_get_devices": 0,
            "metadata_success_multi_discarded_unknown": 0,
            "metadata_entries_staged": 0,
            "metadata_commit_success": 0,
            "metadata_commit_crc_mismatch": 0,
            "metadata_commit_count_mismatch": 0,
            "metadata_waiting_get_devices": 0,
            "metadata_retry_scheduled": 0,
            "command_error_unknown": 0,
            "get_devices_rejected": 0,
            "get_devices_completed": 0,
            "pending_get_devices_peak": 0,
        }
        # Set once the initial GetDevices command has been sent after connection.
        # Metadata requests are delayed until this is True to mirror the v2.7.2
        # Android plugin sequencing (GetDevices T+500ms, metadata T+1500ms).
        self._initial_get_devices_sent: bool = False
        # CRC of the metadata last successfully loaded from the gateway.
        # Persists across disconnect/reconnect so we can skip re-requests when
        # the gateway reports the same DeviceMetadataTableCrc (official app behaviour).
        self._last_metadata_crc: int | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_generation: int = 0
        self._consecutive_failures: int = 0
        self._last_lockout_clear: float = 0.0
        self._has_can_write: bool = False
        self._is_can_ble: bool = False  # CAN-only gateway — no MyRvLink AUTH/DATA services
        # Survives disconnect/reconnect — marks this entry as an IDS-CAN BLE gateway.
        self._can_ble_confirmed: bool = False
        # Set to True only after _authenticate_can_ble completes (post-CAN_READ subscribe).
        # Cleared on every disconnect so the live path in async_can_switch is suppressed
        # during the PIN-auth window on reconnect (prevents session open with no CAN_READ).
        self._can_read_subscribed: bool = False
        self._can_device_types: dict[int, int] = {}  # source_address → IDS-CAN device_type
        # Commands queued while CAN BLE gateway is between connections.
        # Tuple: (frame_bytes, enqueue_monotonic, device_id)
        # device_id is needed so flush can open the REMOTE_CONTROL session first.
        self._can_commands_queue: list[tuple[bytes, float, int]] = []

        # ── REMOTE_CONTROL session state (IDS-CAN-only gateways) ────────
        # Session ID 4 (0x0004) is opened with a specific relay device before
        # COMMAND frames; kept alive via 4-second heartbeats.
        self._rc_session_open: bool = False
        self._rc_session_target: int | None = None  # device_id session is open with
        self._rc_session_seed_future: asyncio.Future | None = None
        self._rc_session_key_future: asyncio.Future | None = None
        self._rc_session_last_status_code: int | None = None
        self._rc_heartbeat_task: asyncio.Task | None = None
        self._rc_session_lock: asyncio.Lock = asyncio.Lock()
        self._can_time_source: int | None = None
        # Source address used by official app host-side IDS-CAN requests.
        self._gateway_can_address: int = 0xFA
        self._can_local_host_claimed: bool = False
        self._can_local_host_mac: bytes = self._make_can_local_host_mac()
        self._can_local_host_identity_last_tx: float = 0.0
        self._can_ble_gateway_version: str = entry.data.get(
            CONF_ADVERTISED_GATEWAY_VERSION, "Unknown"
        )
        # CAN-only link keepalive task. Periodic lightweight discovery requests
        # keep the gateway link active during idle periods.
        self._can_keepalive_task: asyncio.Task | None = None

        # ── Data freshness tracking ──────────────────────────────────
        self._last_event_time: float = 0.0  # monotonic timestamp

        # ── DTC fault deduplication ──────────────────────────────────
        self._last_dtc_codes: dict[str, int] = {}  # key → last known dtc_code

        # ── Accumulated state ─────────────────────────────────────────
        self.gateway_info: GatewayInformation | None = None
        self.rv_status: RvStatus | None = None

        # Per-device state keyed by "TT:DD" hex string
        self.relays: dict[str, RelayStatus] = {}
        self.dimmable_lights: dict[str, DimmableLight] = {}
        self.rgb_lights: dict[str, RgbLight] = {}
        self.covers: dict[str, CoverStatus] = {}
        self.hvac_zones: dict[str, HvacZone] = {}
        self.tanks: dict[str, TankLevel] = {}
        self.device_online: dict[str, DeviceOnline] = {}
        self.device_locks: dict[str, DeviceLock] = {}
        self.generators: dict[str, GeneratorStatus] = {}
        self.hour_meters: dict[str, HourMeter] = {}
        self.levelers: dict[str, LevelerStatus] = {}
        self.tank_alerts: dict[str, TankAlert] = {}
        self.rtc: RealTimeClock | None = None
        self.system_lockout_level: int | None = None

        # Metadata: friendly names per device key
        self.device_names: dict[str, str] = {}
        self._metadata_raw: dict[str, DeviceMetadata] = {}

        # Last non-zero brightness per dimmable device (persists across off/on cycles).
        # Mirrors Android lastKnownDimmableBrightness — only updated when brightness > 0.
        self._last_known_dimmable_brightness: dict[str, int] = {}

        # Last known RGB color (R, G, B) per device — updated only when mode > 0 (light is on).
        # Mirrors Android lastKnownRgbColor — never overwritten by an off-state frame (R=0,G=0,B=0).
        self._last_known_rgb_color: dict[str, tuple[int, int, int]] = {}

        # ── HVAC debounce / pending guard / retry ─────────────────────
        # Pending command guard: suppresses stale gateway echoes during command window.
        # Mirrors Android pendingHvacCommands.
        self._pending_hvac: dict[str, PendingHvacCommand] = {}
        # Command merge baseline: kept in sync with hvac_zones but only updated
        # after the pending guard passes (so suppressed echoes don't corrupt merges).
        self._hvac_zone_states: dict[str, HvacZone] = {}
        # Observed capability bitmask learned from status events.
        # Mirrors Android observedHvacCapability (bit0=Gas, bit1=AC, bit2=HeatPump, bit3=Fan).
        self.observed_hvac_capability: dict[str, int] = {}
        # Asyncio timer handles for setpoint retry (one per zone).
        self._hvac_retry_handles: dict[str, asyncio.TimerHandle] = {}

        # Entity platform callbacks (typed)
        self._event_callbacks: list[Callable[[Any], None]] = []

    @property
    def instance_tag(self) -> str:
        return self._instance_tag

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected and self._client is not None

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def data_healthy(self) -> bool:
        """Return True if we've received data recently."""
        if self._last_event_time == 0.0:
            return False
        # IDS-CAN BLE gateways may disconnect after each broadcast cycle
        # and reconnect every ~5-10s.  Don't require _connected so entities stay available
        # between cycles; use a 30s staleness window instead.
        if self._can_ble_confirmed:
            return (time.monotonic() - self._last_event_time) < 30.0
        if not self._connected:
            return False
        return (time.monotonic() - self._last_event_time) < 15.0

    @property
    def is_can_ble_gateway(self) -> bool:
        """True for IDS-CAN BLE gateways that may disconnect each cycle."""
        return self._can_ble_confirmed

    @property
    def can_ble_gateway_version(self) -> str:
        """Official app gateway version selected for CAN BLE message packing."""
        return self._can_ble_gateway_version

    @property
    def can_read_subscribed(self) -> bool:
        """True while the live CAN_READ notification subscription is active."""
        return self._can_read_subscribed

    @property
    def gateway_can_address(self) -> int:
        """LocalHost source address used for IDS-CAN requests."""
        return self._gateway_can_address

    @property
    def can_local_host_mac(self) -> str:
        """Pseudo LocalHost MAC advertised on the IDS-CAN bus."""
        return self._can_local_host_mac.hex()

    @property
    def can_device_types(self) -> dict[int, int]:
        """Discovered IDS-CAN device type by source address."""
        return dict(self._can_device_types)

    @property
    def can_command_queue_size(self) -> int:
        """Number of CAN commands queued for the next reconnect window."""
        return len(self._can_commands_queue)

    @property
    def remote_control_session_open(self) -> bool:
        """True when the coordinator believes REMOTE_CONTROL session 4 is open."""
        return self._rc_session_open

    @property
    def remote_control_session_target(self) -> int | None:
        """Device address for the current REMOTE_CONTROL session, if any."""
        return self._rc_session_target

    @property
    def last_event_age(self) -> float | None:
        """Seconds since last event, or None if no events received."""
        if self._last_event_time == 0.0:
            return None
        return time.monotonic() - self._last_event_time

    def _make_can_local_host_mac(self) -> bytes:
        """Make a stable pseudo-random MAC for this HA IDS-CAN local host.

        Official CAN adapters do not reuse the BLE gateway MAC for LocalHost;
        ``CanAdapterFactory.MakeMac`` generates a random six-byte address for
        the app-side IDS-CAN adapter.  Use a stable hash so HA keeps the same
        identity across reconnects/restarts without colliding with the gateway.
        """
        seed = f"ha-onecontrol-localhost:{self.entry.entry_id}:{self.address}".encode("utf-8")
        mac = bytearray(hashlib.sha256(seed).digest()[:6])
        if all(b == 0x00 for b in mac):
            mac[0] = 0x01
        return bytes(mac)

    def device_name(self, table_id: int, device_id: int) -> str:
        """Return friendly name or fallback like 'Device 0B:05'."""
        key = _device_key(table_id, device_id)
        return self.device_names.get(key, f"Device {key.upper()}")

    def register_event_callback(self, cb: Callable[[Any], None]) -> Callable[[], None]:
        """Register a callback for parsed events. Returns unsubscribe callable."""
        self._event_callbacks.append(cb)

        def _unsub() -> None:
            if cb in self._event_callbacks:
                self._event_callbacks.remove(cb)

        return _unsub

    # ------------------------------------------------------------------
    # Command sending (COBS-encoded writes to DATA_WRITE)
    # ------------------------------------------------------------------

    async def async_send_command(self, raw_command: bytes) -> None:
        """COBS-encode and write a command to the gateway."""
        if not self._client or not self._connected:
            raise BleakError("Not connected to gateway")
        encoded = cobs_encode(raw_command)
        _LOGGER.debug("TX command (%d bytes raw): %s", len(raw_command), raw_command.hex())
        await self._client.write_gatt_char(DATA_WRITE_CHAR_UUID, encoded, response=False)

    def _encode_ble_v2_twenty_nine_bit(self, frame: bytes) -> bytes:
        """Encode raw extended IDS-CAN wire frame into BLE V2 TwentyNineBit format.

        BLE V2 frame layout: [0x03][can_id(4)][dlc][payload].
        """
        wire = parse_ids_can_wire_frame(frame)
        if wire is None or not wire.is_extended:
            return frame
        can_id = ((wire.message_type & 0xFF) << 24) | ((wire.source_address & 0xFF) << 16)
        can_id |= ((wire.target_address or 0) & 0xFF) << 8
        can_id |= (wire.message_data or 0) & 0xFF
        return bytes([0x03]) + can_id.to_bytes(4, "big") + bytes([wire.dlc & 0xFF]) + wire.payload

    async def _write_can_frame(self, client: BleakClient, frame: bytes, *, label: str) -> None:
        """Write IDS-CAN frame to CAN_WRITE with gateway-specific framing."""
        # Official app path writes CAN adapter frames directly to IDS CAN WRITE.
        # Keep TX as raw IDS wire frame (e.g. [dlc][id(4)][payload] for 29-bit)
        # instead of wrapping in BLE V2 0x03 format.
        _LOGGER.debug("CAN BLE: %s RAW tx=%s", label, frame.hex())
        await client.write_gatt_char(CAN_WRITE_CHAR_UUID, frame, response=False)

    def _is_can_ble_v1_gateway(self) -> bool:
        """Return True when official app would use IdsCanSessionManagerAuto."""
        return self._can_ble_gateway_version == "V1"

    async def async_can_switch(self, device_id: int, state: bool) -> None:
        """Send relay on/off via IDS-CAN COMMAND frame to CAN_WRITE.

        If the gateway is currently disconnected (normal for some IDS-CAN BLE gateways),
        the command is queued and sent at the start of the next connection window.
        """
        if self._client and self._connected and self._can_read_subscribed:
            await self._advertise_can_local_host_identity(
                self._client, reason="pre-command", force=True
            )
            # Official command runner activates REMOTE_CONTROL first.  Even
            # for observed V1/24955 gateways, Type2 relays require explicit
            # session seed/key before accepting COMMAND frames from HA.
            session_ok = await self._ensure_remote_control_session(self._client, device_id)
            if not session_ok:
                _LOGGER.warning(
                    "CAN BLE: relay COMMAND skipped — REMOTE_CONTROL activation failed for device=0x%02X gateway_version=%s",
                    device_id,
                    self._can_ble_gateway_version,
                )
                return

            # RelayBasicLatching Type2 parity: command byte goes in message_data
            # (0x01=ON, 0x00=OFF), payload is empty, source is current LocalHost.
            frame = compose_ids_can_extended_wire_frame(
                message_type=0x82,   # COMMAND
                source_address=self._gateway_can_address,
                target_address=device_id,
                message_data=0x01 if state else 0x00,
                payload=b"",
            )
            _LOGGER.info(
                "CAN BLE: relay COMMAND device=0x%02X src=0x%02X state=%s gateway_version=%s frame=%s",
                device_id,
                self._gateway_can_address,
                "ON" if state else "OFF",
                self._can_ble_gateway_version,
                frame.hex(),
            )
            try:
                await self._write_can_frame(self._client, frame, label="relay COMMAND")
            except BleakError as exc:
                _LOGGER.warning("CAN BLE: relay COMMAND failed: %s", exc)
        else:
            frame = compose_ids_can_extended_wire_frame(
                message_type=0x82,
                source_address=self._gateway_can_address,
                target_address=device_id,
                message_data=0x01 if state else 0x00,
                payload=b"",
            )
            _LOGGER.info(
                "CAN BLE: gateway disconnected — queuing relay COMMAND device=0x%02X state=%s and reconnecting immediately",
                device_id, "ON" if state else "OFF",
            )
            self._can_commands_queue.append((frame, time.monotonic(), device_id))
            # Cancel any pending backoff timer and connect right now so the
            # command is delivered in the next ~1-2s rather than waiting up to 5s.
            self._cancel_reconnect()
            self.hass.async_create_task(self.async_connect())

    async def async_switch(
        self, table_id: int, device_id: int, state: bool
    ) -> None:
        """Send a switch on/off command."""
        if self._can_ble_confirmed:
            await self.async_can_switch(device_id, state)
            return
        cmd = self._cmd.build_action_switch(table_id, state, [device_id])
        await self.async_send_command(cmd)

    async def async_set_dimmable(
        self, table_id: int, device_id: int, brightness: int
    ) -> None:
        """Send a dimmable light brightness command."""
        cmd = self._cmd.build_action_dimmable(table_id, device_id, brightness)
        await self.async_send_command(cmd)

    async def async_set_dimmable_effect(
        self,
        table_id: int,
        device_id: int,
        mode: int = 0x02,
        brightness: int = 255,
        duration: int = 0,
        cycle_time1: int = 1055,
        cycle_time2: int = 1055,
    ) -> None:
        """Send a dimmable light effect command (blink/swell)."""
        cmd = self._cmd.build_action_dimmable_effect(
            table_id, device_id, mode, brightness, duration, cycle_time1, cycle_time2,
        )
        await self.async_send_command(cmd)

    async def async_set_hvac(
        self,
        table_id: int,
        device_id: int,
        heat_mode: int = 0,
        heat_source: int = 0,
        fan_mode: int = 0,
        low_trip_f: int = 65,
        high_trip_f: int = 78,
        is_setpoint_change: bool = False,
        is_preset_change: bool = False,
    ) -> None:
        """Send an HVAC command and register a pending command guard."""
        cmd = self._cmd.build_action_hvac(
            table_id, device_id, heat_mode, heat_source, fan_mode, low_trip_f, high_trip_f
        )
        await self.async_send_command(cmd)

        key = _device_key(table_id, device_id)
        self._pending_hvac[key] = PendingHvacCommand(
            table_id=table_id,
            device_id=device_id,
            heat_mode=heat_mode,
            heat_source=heat_source,
            fan_mode=fan_mode,
            low_trip_f=low_trip_f,
            high_trip_f=high_trip_f,
            is_setpoint_change=is_setpoint_change,
            is_preset_change=is_preset_change,
            sent_at=time.monotonic(),
        )
        if is_setpoint_change:
            self._schedule_setpoint_retry(key)

    def _is_startup_bootstrap_active(self, table_id: int | None = None) -> bool:
        """Return True while the serialized startup query flow is active."""
        if self._startup_bootstrap_task is None or self._startup_bootstrap_task.done():
            return False
        if table_id is None:
            return True
        return self._startup_bootstrap_table_id == table_id

    def _resolve_bootstrap_waiter(self, kind: str, table_id: int, result: str) -> None:
        """Resolve a bootstrap waiter for a specific query class/table pair."""
        waiter = self._bootstrap_waiters.pop((kind, table_id), None)
        if waiter is not None and not waiter.done():
            waiter.set_result(result)

    def _cancel_startup_bootstrap(self) -> None:
        """Cancel any active startup bootstrap and fail outstanding waiters."""
        if self._startup_bootstrap_task and not self._startup_bootstrap_task.done():
            self._startup_bootstrap_task.cancel()
        self._startup_bootstrap_task = None
        self._startup_bootstrap_table_id = None
        for waiter in self._bootstrap_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._bootstrap_waiters.clear()

    def _ensure_startup_bootstrap(self, table_id: int) -> None:
        """Start or reuse the serialized startup bootstrap for a table."""
        if table_id == 0 or not self._connected or not self._authenticated:
            return
        if table_id in self._metadata_loaded_tables:
            self._start_heartbeat()
            return
        if self._is_startup_bootstrap_active(table_id):
            return
        if self._startup_bootstrap_task and not self._startup_bootstrap_task.done():
            self._cancel_startup_bootstrap()
        self._startup_bootstrap_table_id = table_id
        self._startup_bootstrap_task = self.hass.async_create_task(
            self._bootstrap_table_queries(table_id)
        )

    async def _send_get_devices_request(self, table_id: int) -> int:
        """Send GetDevices for a specific table and track the pending cmdId."""
        cmd = self._cmd.build_get_devices(table_id)
        cmd_id = int.from_bytes(cmd[0:2], "little")
        self._pending_get_devices_cmdids[cmd_id] = table_id
        if len(self._pending_get_devices_cmdids) > _MAX_PENDING_GET_DEVICES_CMDIDS:
            self._pending_get_devices_cmdids.pop(next(iter(self._pending_get_devices_cmdids)))
        self._cmd_correlation_stats["pending_get_devices_peak"] = max(
            self._cmd_correlation_stats["pending_get_devices_peak"],
            len(self._pending_get_devices_cmdids),
        )
        await self.async_send_command(cmd)
        return cmd_id

    async def _send_query_and_wait(self, kind: str, table_id: int) -> str:
        """Send a bootstrap query and wait for its completion or rejection."""
        waiter_key = (kind, table_id)
        existing_waiter = self._bootstrap_waiters.get(waiter_key)
        if existing_waiter is not None and not existing_waiter.done():
            return await asyncio.wait_for(
                asyncio.shield(existing_waiter),
                timeout=_STARTUP_BOOTSTRAP_WAIT_SECONDS,
            )

        waiter = asyncio.get_running_loop().create_future()
        self._bootstrap_waiters[waiter_key] = waiter
        try:
            if kind == "get_devices":
                cmd_id = await self._send_get_devices_request(table_id)
                self._initial_get_devices_sent = True
                _LOGGER.debug(
                    "Startup GetDevices sent for table %d (cmdId=%d)",
                    table_id,
                    cmd_id,
                )
            else:
                await self._send_metadata_request(table_id)
            return await asyncio.wait_for(
                asyncio.shield(waiter),
                timeout=_STARTUP_BOOTSTRAP_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._bootstrap_waiters.pop(waiter_key, None)
            return "timeout"
        except Exception:
            self._bootstrap_waiters.pop(waiter_key, None)
            raise

    async def _bootstrap_table_queries(self, table_id: int) -> None:
        """Serialize startup GetDevices/metadata queries before heartbeat.

        Retries indefinitely with exponential backoff (capped at
        _STARTUP_BOOTSTRAP_MAX_BACKOFF_SECONDS) until metadata loads
        successfully, the connection drops, or
        _STARTUP_BOOTSTRAP_TIMEOUT_SECONDS elapses.  This covers gateways
        that need many minutes to fully boot after a power cycle — during
        that window the command processor returns 0x0f on every request.
        """
        deadline = time.monotonic() + _STARTUP_BOOTSTRAP_TIMEOUT_SECONDS
        backoff = _STARTUP_BOOTSTRAP_BACKOFF_SECONDS
        attempt = 0
        try:
            if table_id in self._metadata_loaded_tables:
                self._start_heartbeat()
                return

            while True:
                if not self._connected or not self._authenticated:
                    return
                if self.gateway_info is None or self.gateway_info.table_id != table_id:
                    return
                if table_id in self._metadata_loaded_tables:
                    break
                if time.monotonic() > deadline:
                    _LOGGER.warning(
                        "Bootstrap for table %d timed out after %.0fs — starting heartbeat",
                        table_id,
                        _STARTUP_BOOTSTRAP_TIMEOUT_SECONDS,
                    )
                    break

                attempt += 1

                if table_id not in self._get_devices_loaded_tables:
                    result = await self._send_query_and_wait("get_devices", table_id)
                    if result != "completed":
                        _LOGGER.debug(
                            "Startup GetDevices for table %d attempt %d ended with %s"
                            " (backoff=%.1fs)",
                            table_id, attempt, result, backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _STARTUP_BOOTSTRAP_MAX_BACKOFF_SECONDS)
                        continue

                if table_id in self._metadata_loaded_tables:
                    break

                result = await self._send_query_and_wait("metadata", table_id)
                if result == "completed":
                    break

                _LOGGER.debug(
                    "Startup metadata for table %d attempt %d ended with %s"
                    " (backoff=%.1fs)",
                    table_id, attempt, result, backoff,
                )
                self._metadata_requested_tables.discard(table_id)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _STARTUP_BOOTSTRAP_MAX_BACKOFF_SECONDS)

            if self._connected and self._authenticated:
                self._start_heartbeat()
        except asyncio.CancelledError:
            raise
        finally:
            if self._startup_bootstrap_table_id == table_id:
                self._startup_bootstrap_task = None
                self._startup_bootstrap_table_id = None
            self._resolve_bootstrap_waiter("get_devices", table_id, "canceled")
            self._resolve_bootstrap_waiter("metadata", table_id, "canceled")

    # ------------------------------------------------------------------
    # HVAC capability tracking, pending guard, and setpoint retry
    # ------------------------------------------------------------------

    def _update_observed_hvac_capability(self, zone_key: str, zone: HvacZone) -> None:
        """Accumulate observed HVAC capability from status events.

        Mirrors Android observedHvacCapability logic — each status event can
        reveal new capabilities even if GetDevicesMetadata returns 0x00.
        """
        prev = self.observed_hvac_capability.get(zone_key, 0)
        cap = prev

        active_status = zone.zone_status & 0x0F
        if active_status == 2:
            cap |= HVAC_CAP_AC
        elif active_status == 3:
            cap |= HVAC_CAP_HEAT_PUMP | HVAC_CAP_AC
        elif active_status in (5, 6):
            cap |= HVAC_CAP_GAS

        if zone.heat_mode in (1, 3):
            if zone.heat_source == 0:
                cap |= HVAC_CAP_GAS
            elif zone.heat_source == 1:
                cap |= HVAC_CAP_HEAT_PUMP
        if zone.heat_mode in (2, 3):
            cap |= HVAC_CAP_AC
        if zone.fan_mode == 2:
            cap |= HVAC_CAP_MULTISPEED_FAN

        if cap != prev:
            self.observed_hvac_capability[zone_key] = cap
            _LOGGER.debug(
                "HVAC %s: observed capability 0x%02X→0x%02X (status=%d mode=%d src=%d fan=%d)",
                zone_key, prev, cap,
                active_status, zone.heat_mode, zone.heat_source, zone.fan_mode,
            )

    def _handle_hvac_zone(self, zone: HvacZone) -> None:
        """Apply the pending command guard and update hvac_zones / _hvac_zone_states.

        Always updates observed capability and triggers metadata request.
        Only updates state dicts if the event is not suppressed by the guard.
        Mirrors Android handleHvacStatus() pending-guard logic.
        """
        key = _device_key(zone.table_id, zone.device_id)
        self._ensure_metadata_for_table(zone.table_id)
        self._update_observed_hvac_capability(key, zone)

        pending = self._pending_hvac.get(key)
        if pending is not None:
            age = time.monotonic() - pending.sent_at
            window = (
                HVAC_PRESET_PENDING_WINDOW_S if pending.is_preset_change
                else HVAC_SETPOINT_PENDING_WINDOW_S if pending.is_setpoint_change
                else HVAC_PENDING_WINDOW_S
            )
            if age <= window:
                low_ok = abs(zone.low_trip_f - pending.low_trip_f) <= 1
                high_ok = abs(zone.high_trip_f - pending.high_trip_f) <= 1
                matches = (
                    zone.heat_mode == pending.heat_mode
                    and zone.heat_source == pending.heat_source
                    and zone.fan_mode == pending.fan_mode
                    and low_ok and high_ok
                )
                if not matches:
                    _LOGGER.debug(
                        "HVAC guard: suppressing stale echo for %s (age=%.1fs window=%.0fs)",
                        key, age, window,
                    )
                    return  # suppress — do not update hvac_zones
                # Matched — gateway confirmed our command
                if not pending.is_preset_change:
                    # Clear pending immediately (preset guard holds full window)
                    self._pending_hvac.pop(key, None)
                    if key in self._hvac_retry_handles:
                        self._hvac_retry_handles.pop(key).cancel()
                    _LOGGER.debug("HVAC guard: command confirmed for %s (age=%.1fs)", key, age)
            else:
                # Window expired — clear stale pending
                self._pending_hvac.pop(key, None)

        self.hvac_zones[key] = zone
        self._hvac_zone_states[key] = zone

    def _schedule_setpoint_retry(self, zone_key: str) -> None:
        """Schedule a setpoint verification/retry check after HVAC_SETPOINT_RETRY_DELAY_S.

        Mirrors Android scheduleSetpointVerification() — WRITE_TYPE_NO_RESPONSE
        can be silently dropped by the BLE stack; this ensures eventual delivery.
        """
        if zone_key in self._hvac_retry_handles:
            self._hvac_retry_handles.pop(zone_key).cancel()

        def _callback() -> None:
            self.hass.async_create_task(self._do_retry_setpoint(zone_key))

        self._hvac_retry_handles[zone_key] = self.hass.loop.call_later(
            HVAC_SETPOINT_RETRY_DELAY_S, _callback
        )

    async def _do_retry_setpoint(self, zone_key: str) -> None:
        """Re-send an unconfirmed HVAC setpoint command.

        Uses exact values from PendingHvacCommand — no re-merging.
        Mirrors Android retryHvacSetpoint().
        """
        pending = self._pending_hvac.get(zone_key)
        if pending is None or not pending.is_setpoint_change:
            return  # already confirmed — nothing to do
        if pending.retry_count >= HVAC_SETPOINT_MAX_RETRIES:
            _LOGGER.warning(
                "HVAC setpoint retries exhausted (%d) for %s — giving up",
                HVAC_SETPOINT_MAX_RETRIES, zone_key,
            )
            self._pending_hvac.pop(zone_key, None)
            return
        _LOGGER.debug(
            "HVAC setpoint retry %d/%d for %s (low=%d high=%d)",
            pending.retry_count + 1, HVAC_SETPOINT_MAX_RETRIES, zone_key,
            pending.low_trip_f, pending.high_trip_f,
        )
        cmd = self._cmd.build_action_hvac(
            pending.table_id, pending.device_id,
            pending.heat_mode, pending.heat_source, pending.fan_mode,
            pending.low_trip_f, pending.high_trip_f,
        )
        await self.async_send_command(cmd)
        self._pending_hvac[zone_key] = replace(
            pending,
            retry_count=pending.retry_count + 1,
            sent_at=time.monotonic(),
        )
        self._schedule_setpoint_retry(zone_key)

    async def async_set_generator(
        self, table_id: int, device_id: int, run: bool
    ) -> None:
        """Send a generator start/stop command."""
        cmd = self._cmd.build_action_generator(table_id, device_id, run)
        await self.async_send_command(cmd)

    async def async_set_rgb(
        self,
        table_id: int,
        device_id: int,
        mode: int = 0x01,
        red: int = 255,
        green: int = 255,
        blue: int = 255,
        auto_off: int = 0xFF,
        blink_on_interval: int = 0,
        blink_off_interval: int = 0,
        transition_interval: int = 1000,
    ) -> None:
        """Send an RGB light command."""
        cmd = self._cmd.build_action_rgb(
            table_id, device_id, mode, red, green, blue,
            auto_off, blink_on_interval, blink_off_interval, transition_interval,
        )
        await self.async_send_command(cmd)

    async def async_clear_lockout(self) -> None:
        """Send lockout clear sequence (0x55 arm → 100ms → 0xAA clear).

        Preferred path: raw writes to CAN_WRITE characteristic.
        Fallback: COBS-encoded via DATA_WRITE.
        Throttled to one attempt per 5 seconds.

        Reference: Android requestLockoutClear() — MyRvLinkBleManager.kt
        """
        if self._can_ble_confirmed:
            _LOGGER.warning(
                "Lockout clear ignored for IDS-CAN BLE gateway — MyRVLink 0x55/0xAA sequence is not a CAN frame"
            )
            return

        now = time.monotonic()
        if now - self._last_lockout_clear < LOCKOUT_CLEAR_THROTTLE:
            _LOGGER.warning("Lockout clear throttled (min %ss)", LOCKOUT_CLEAR_THROTTLE)
            return
        self._last_lockout_clear = now

        if not self._client or not self._connected:
            raise BleakError("Not connected to gateway")

        arm = bytes([0x55])
        clear = bytes([0xAA])

        if self._has_can_write:
            _LOGGER.info("Lockout clear: writing 0x55 → CAN_WRITE")
            await self._client.write_gatt_char(CAN_WRITE_CHAR_UUID, arm, response=False)
            await asyncio.sleep(0.1)
            _LOGGER.info("Lockout clear: writing 0xAA → CAN_WRITE")
            await self._client.write_gatt_char(CAN_WRITE_CHAR_UUID, clear, response=False)
        else:
            _LOGGER.info("Lockout clear: CAN_WRITE not available, using DATA_WRITE fallback")
            await self._client.write_gatt_char(
                DATA_WRITE_CHAR_UUID, cobs_encode(arm), response=False
            )
            await asyncio.sleep(0.1)
            await self._client.write_gatt_char(
                DATA_WRITE_CHAR_UUID, cobs_encode(clear), response=False
            )

    async def async_refresh_metadata(self) -> None:
        """Re-request device metadata for all known table IDs."""
        if self._can_ble_confirmed:
            _LOGGER.info(
                "Refresh Metadata ignored for IDS-CAN BLE gateway — names are learned from DEVICE_ID broadcasts"
            )
            return

        # Reset per-table state so all tables can be re-requested
        self._metadata_requested_tables.clear()
        self._metadata_loaded_tables.clear()
        self._metadata_rejected_tables.clear()
        self._metadata_retry_counts.clear()
        self._metadata_retry_pending.clear()
        self._pending_metadata_cmdids.clear()
        self._pending_metadata_entries.clear()
        self._pending_get_devices_cmdids.clear()

        # Collect all known table IDs: gateway, previously loaded metadata,
        # and all observed device status tables (covers tables we saw via status
        # events but may not have successfully loaded metadata for)
        table_ids: set[int] = set()
        if self.gateway_info:
            table_ids.add(self.gateway_info.table_id)
        for meta in self._metadata_raw.values():
            table_ids.add(meta.table_id)
        for status_dict in (
            self.relays, self.dimmable_lights, self.rgb_lights, self.covers,
            self.hvac_zones, self.tanks, self.device_online, self.device_locks,
            self.generators, self.hour_meters, self.levelers, self.tank_alerts, self.unknown_devices,
        ):
            for key in status_dict:
                t = int(key.split(":")[0], 16)
                if t != 0:
                    table_ids.add(t)
        for tid in sorted(table_ids):
            await self._send_metadata_request(tid)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def async_connect(self) -> None:
        """Establish BLE connection and authenticate."""
        async with self._connect_lock:
            if self._connected:
                return
            await self._do_connect()

    async def async_disconnect(self) -> None:
        """Disconnect from the gateway."""
        self._stop_heartbeat()
        self._cancel_startup_bootstrap()
        self._cancel_reconnect()
        self._connected = False
        self._authenticated = False
        if self._client:
            try:
                await self._client.disconnect()
            except BleakError:
                pass
            self._client = None
        self._decoder.reset()

    async def _do_connect(self) -> None:
        """Internal connect routine with retry logic."""
        max_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                await self._try_connect(attempt)
                return
            except Exception as exc:
                last_exc = exc
                _LOGGER.warning(
                    "Connection attempt %d/%d failed: %s",
                    attempt, max_attempts, exc,
                )
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None
                self._connected = False
                self._authenticated = False

                if attempt < max_attempts:
                    delay = 3 * attempt
                    _LOGGER.info("Retrying in %ds...", delay)
                    await asyncio.sleep(delay)

        assert last_exc is not None

        # Stale bond detection: if BlueZ reported "already bonded" at any point
        # this session but all connection attempts still failed, the bond is stale
        # (e.g. created by a prior push_button session or after a gateway reset).
        # _pin_already_bonded is a sticky flag — unlike _pin_dbus_succeeded it is
        # NOT cleared by _on_disconnect, so it survives across the retry loop.
        # We remove the stale bond and attempt one fresh PIN pairing.
        if (self.is_pin_gateway or self.is_x180t_gateway) and self._pin_already_bonded:
            _LOGGER.warning(
                "%s gateway %s: BlueZ bond present but all connection attempts failed "
                "— removing stale bond and retrying with fresh pairing",
                "X180T" if self.is_x180t_gateway else "PIN",
                self.address,
            )
            removed = await remove_bond(self.address)
            if removed:
                _LOGGER.info(
                    "Stale bond removed for %s — attempting fresh PIN pairing",
                    self.address,
                )
                self._pin_dbus_succeeded = False
                self._pin_already_bonded = False
                try:
                    await self._try_connect(max_attempts + 1)
                    return
                except Exception as stale_exc:
                    last_exc = stale_exc
                    _LOGGER.warning(
                        "Re-pair attempt after stale bond removal failed for %s: %s",
                        self.address, stale_exc,
                    )

        # All HA-routed attempts failed — try direct HCI adapters as fallback.
        # This handles the case where the ESPHome BT proxy has no free slots
        # but a local USB/onboard adapter can reach the gateway.
        _LOGGER.warning(
            "All %d HA-routed connection attempts failed for %s; "
            "trying direct HCI adapter fallback",
            max_attempts, self.address,
        )
        try:
            hci_adapters = sorted(
                name
                for name in await asyncio.to_thread(os.listdir, "/sys/class/bluetooth")
                if name.startswith("hci")
            )
        except OSError:
            hci_adapters = ["hci0"]
        if not hci_adapters:
            hci_adapters = ["hci0"]
        for adapter in hci_adapters:
            _LOGGER.info(
                "Direct BLE connect to %s via %s", self.address, adapter,
            )
            try:
                await self._try_connect_direct(adapter)
                _LOGGER.info(
                    "Direct connect succeeded via %s for %s",
                    adapter, self.address,
                )
                return
            except Exception as exc:
                _LOGGER.debug("Direct connect via %s failed: %s", adapter, exc)
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None
                self._connected = False
                self._authenticated = False

        # All paths exhausted
        raise last_exc

    @property
    def is_pin_gateway(self) -> bool:
        """True if this gateway uses PIN-based BLE pairing."""
        return self._pairing_method == "pin"

    @property
    def is_x180t_gateway(self) -> bool:
        """True for Unity X180T CAN-BLE gateways."""
        return self._gateway_family == GATEWAY_FAMILY_X180T

    async def _try_connect(self, attempt: int) -> None:
        """Single connection attempt — connect, pair, authenticate."""
        _LOGGER.info(
            "Connecting to OneControl gateway %s (attempt %d, method=%s)",
            self.address, attempt, self._pairing_method,
        )

        # ── Source-pinning: prefer the adapter where the bond lives ─────────
        # CONF_BONDED_SOURCE records the HA scanner source (hciX adapter MAC
        # or ESPHome proxy name) that carries the BLE bond (LTK).  For bonds
        # created via local BlueZ D-Bus pairing the LTK lives on the local
        # adapter — connecting through a proxy would produce an unencrypted
        # link, causing INSUF_AUTH (status=5) on secured characteristics and
        # a gateway-initiated disconnect (error 19).  We therefore check BlueZ
        # at connect time and, when a local bond exists, always prefer a local
        # HCI scanner candidate over any proxy — regardless of what
        # CONF_BONDED_SOURCE currently stores.
        device = None
        self._current_connect_source = None
        bonded_source: str | None = self.entry.options.get(CONF_BONDED_SOURCE)

        try:
            candidates = bluetooth.async_scanner_devices_by_address(
                self.hass, self.address, connectable=True
            )
        except Exception:  # API unavailable on this HA version
            candidates = []

        # Check whether BlueZ holds a local bond for this device.  If so,
        # prefer a local HCI adapter scanner over any proxy — the LTK is only
        # usable via the local radio.
        locally_bonded = await async_is_locally_bonded(self.address)
        local_macs = await async_get_local_adapter_macs()
        candidate_sources = [c.scanner.source for c in candidates]
        _LOGGER.debug(
            "Bond check %s: locally_bonded=%s local_macs=%s candidate_sources=%s",
            self.address, locally_bonded, local_macs, candidate_sources,
        )

        # ── PIN gateways must always use a local HCI adapter ────────────────
        # BlueZ D-Bus pairing (required for PIN/passkey exchange) does not work
        # through ESPHome BT proxies — the proxy radio is remote and BlueZ
        # cannot perform SMP key exchange over it.  Force local HCI regardless
        # of adapter scores, bond state, or CONF_BONDED_SOURCE.
        if self.is_pin_gateway and candidates:
            _pin_local = next(
                (c for c in candidates
                 if c.scanner.source.upper().replace(":", "") in
                    {m.replace(":", "") for m in local_macs}),
                None,
            )
            if _pin_local is not None:
                device = _pin_local.ble_device
                self._current_connect_source = _pin_local.scanner.source
                _LOGGER.info(
                    "PIN gateway %s: forcing local HCI adapter %s "
                    "(proxy pairing unsupported)",
                    self.address, self._current_connect_source,
                )
            else:
                _LOGGER.warning(
                    "PIN gateway %s: no local HCI scanner visible — "
                    "will attempt via proxy but pairing will likely fail",
                    self.address,
                )

        if device is None and locally_bonded and candidates:
            local_candidate = next(
                (c for c in candidates
                 if c.scanner.source.upper().replace(":", "") in
                    {m.replace(":", "") for m in local_macs}),
                None,
            )
            if local_candidate is not None:
                device = local_candidate.ble_device
                self._current_connect_source = local_candidate.scanner.source
                _LOGGER.info(
                    "Connecting to %s via local HCI adapter %s (local BlueZ bond)",
                    self.address, self._current_connect_source,
                )
            else:
                _LOGGER.debug(
                    "Local BlueZ bond for %s but no local HCI scanner candidate "
                    "(local_macs=%s, candidate_sources=%s) — falling back",
                    self.address, local_macs, candidate_sources,
                )

        if device is None and bonded_source and candidates:
            preferred = next(
                (c for c in candidates if c.scanner.source == bonded_source), None
            )
            if preferred is not None:
                device = preferred.ble_device
                self._current_connect_source = preferred.scanner.source
                _LOGGER.info(
                    "Connecting to %s via bonded source %s (attempt %d)",
                    self.address, bonded_source, attempt,
                )
            else:
                _LOGGER.warning(
                    "Bonded source %s not available for %s — falling back to HA routing",
                    bonded_source, self.address,
                )

        if device is None:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is not None and candidates:
                # Capture the source so we can persist it on auth success.
                # Prefer a local HCI adapter (MAC-address source) over a proxy
                # so that the D-Bus agent works and the LTK is stored locally.
                addr_upper = self.address.upper()
                addr_candidates = [
                    c for c in candidates
                    if c.ble_device.address.upper() == addr_upper
                ]
                local_preferred = next(
                    (c for c in addr_candidates
                     if c.scanner.source.upper().replace(":", "") in
                        {m.replace(":", "") for m in local_macs}),
                    None,
                )
                matched = local_preferred or (addr_candidates[0] if addr_candidates else None)
                self._current_connect_source = matched.scanner.source if matched else None

        if device is None:
            raise BleakError(
                f"OneControl device {self.address} not found by HA Bluetooth"
            )

        # ── D-Bus setup BEFORE Bleak connect ──────────────────────────
        self._push_button_dbus_ok = False

        if self.is_pin_gateway:
            # Register the PIN agent NOW so it is waiting when BlueZ asks
            # for the PIN during client.pair() after GATT connect.
            # We do NOT call Device1.Pair() here — that is done post-connect,
            # matching the Android flow: connectGatt() → createBond() in
            # onConnectionStateChange.
            ctx = await prepare_pin_agent(self.address, self._bluetooth_pin)
            self._pin_agent_ctx = ctx
            if ctx and ctx.already_bonded:
                self._pin_dbus_succeeded = True
                self._pin_already_bonded = True
                _LOGGER.info(
                    "PIN gateway %s — already bonded, connecting directly",
                    self.address,
                )
        elif self.is_x180t_gateway:
            # Official X180T flow connects first, then creates the bond.
            _LOGGER.info(
                "X180T gateway %s (pairing_method=%s) — registering Just Works agent",
                self.address,
                self._pairing_method,
            )
            ctx = await prepare_push_button_agent(self.address)
            self._pin_agent_ctx = ctx
            if ctx and ctx.already_bonded:
                self._push_button_dbus_ok = True
                self._pin_already_bonded = True
                _LOGGER.info(
                    "X180T gateway %s — already bonded, connecting directly",
                    self.address,
                )
            else:
                _LOGGER.debug(
                    "X180T gateway %s — agent registered: %s",
                    self.address,
                    ctx.agent_registered if ctx else "ctx is None",
                )
        elif is_pin_pairing_supported():
            _LOGGER.info(
                "PushButton gateway — attempting D-Bus Just Works pairing "
                "with %s before connect",
                self.address,
            )
            dbus_ok = await pair_push_button(self.address, timeout=30.0)
            if dbus_ok:
                self._push_button_dbus_ok = True
                _LOGGER.info(
                    "D-Bus PushButton pairing OK for %s (bonded or already bonded)",
                    self.address,
                )
            else:
                _LOGGER.warning(
                    "D-Bus PushButton pairing failed for %s — "
                    "will attempt Bleak pair() after connect",
                    self.address,
                )
        else:
            _LOGGER.debug("D-Bus not available — skipping pre-pairing")

        try:
            client = await establish_connection(
                BleakClient,
                device,
                self.address,
                disconnected_callback=self._on_disconnect,
            )
            await self._finish_connect(client)
        except Exception:
            # Ensure PIN agent is cleaned up if we never reach _finish_connect
            if self._pin_agent_ctx:
                await self._pin_agent_ctx.cleanup()
                self._pin_agent_ctx = None
            raise

    async def _try_connect_direct(self, adapter: str) -> None:
        """Connect directly via a local HCI adapter, bypassing HA routing.

        Used as fallback when the ESPHome BT proxy has no free connection
        slots but a local USB/onboard adapter can reach the gateway.

        Performs a BLE scan first so BlueZ discovers the device and
        populates the correct address type (public vs random). Then
        connects using the BLEDevice object.
        """
        _LOGGER.info(
            "Direct connecting to OneControl %s via %s (method=%s, scanning first)",
            self.address, adapter, self._pairing_method,
        )

        ble_device = None
        scanner = BleakScanner(adapter=adapter)
        try:
            await scanner.start()
            await asyncio.sleep(5.0)
            await scanner.stop()
        except (BleakError, OSError) as scan_exc:
            raise BleakError(
                f"Scan on {adapter} failed (adapter may not exist): {scan_exc}"
            ) from scan_exc

        for dev in scanner.discovered_devices:
            if dev.address.upper() == self.address.upper():
                ble_device = dev
                break

        if ble_device is None:
            raise BleakError(f"Device {self.address} not found in scan on {adapter}")

        _LOGGER.info(
            "Found %s on %s (rssi=%s), connecting...",
            self.address, adapter, getattr(ble_device, "rssi", "?"),
        )

        self._push_button_dbus_ok = False
        if self.is_pin_gateway:
            ctx = await prepare_pin_agent(self.address, self._bluetooth_pin)
            self._pin_agent_ctx = ctx
            if ctx and ctx.already_bonded:
                self._pin_dbus_succeeded = True
                self._pin_already_bonded = True
                _LOGGER.info(
                    "PIN gateway %s — already bonded on %s, connecting directly",
                    self.address, adapter,
                )
        elif self.is_x180t_gateway:
            ctx = await prepare_push_button_agent(self.address)
            self._pin_agent_ctx = ctx
            if ctx and ctx.already_bonded:
                self._push_button_dbus_ok = True
                self._pin_already_bonded = True
                _LOGGER.info(
                    "X180T gateway %s — already bonded on %s, connecting directly",
                    self.address, adapter,
                )
        elif is_pin_pairing_supported():
            _LOGGER.info(
                "PushButton gateway — attempting D-Bus Just Works pairing "
                "with %s before direct connect",
                self.address,
            )
            dbus_ok = await pair_push_button(self.address, timeout=30.0)
            if dbus_ok:
                self._push_button_dbus_ok = True

        client = await establish_connection(
            BleakClient,
            ble_device,
            self.address,
            disconnected_callback=self._on_disconnect,
            adapter=adapter,
        )

        await self._finish_connect(client)

    async def _finish_connect(self, client: BleakClient) -> None:
        """Complete connection: connect, pair, enumerate, authenticate."""
        self._client = client
        self._connected = True
        self.async_set_updated_data(self._build_data())
        _LOGGER.debug("Connected to %s", self.address)

        # Official parity: request MTU 185 when supported.
        # Keep this best-effort so older backends/paths continue to work.
        try:
            if hasattr(client, "request_mtu"):
                mtu = await client.request_mtu(185)
                _LOGGER.debug("Requested MTU 185, negotiated=%s", mtu)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("MTU request not supported or failed: %s", exc)

        # ── Pairing ────────────────────────────────────────────────────
        if not self.is_pin_gateway:
            # PushButton: D-Bus Just Works pairing ran pre-connect; call pair()
            # here as a belt-and-suspenders fallback in case it didn't bond.
            if self._push_button_dbus_ok:
                _LOGGER.info(
                    "PushButton %s — skipping BLE pair(); D-Bus pairing already succeeded",
                    self.address,
                )
            else:
                try:
                    if self.is_x180t_gateway:
                        if self._bluetooth_pin:
                            _LOGGER.info(
                                "X180T PushButton gateway %s — requesting post-connect BLE bond with PIN agent",
                                self.address,
                            )
                        else:
                            _LOGGER.info(
                                "X180T PushButton gateway %s — requesting post-connect BLE bond",
                                self.address,
                            )
                    else:
                        _LOGGER.debug("Requesting BLE pair (PushButton) with %s", self.address)
                    if hasattr(client, "pair"):
                        paired = await client.pair()
                        _LOGGER.info("BLE pair() result: %s", paired)
                        if not paired:
                            # Check if BlueZ reports bonded despite pair() returning False
                            if await async_is_locally_bonded(self.address):
                                _LOGGER.info(
                                    "BLE pair() returned %s but BlueZ reports bonded — proceeding",
                                    paired,
                                )
                                paired = True
                            else:
                                # For X180T, retry pairing once after a short delay
                                if self.is_x180t_gateway:
                                    _LOGGER.info("X180T pairing failed, retrying after delay...")
                                    await asyncio.sleep(1.0)
                                    paired = await client.pair()
                                    _LOGGER.info("BLE pair() retry result: %s", paired)
                                    if not paired and await async_is_locally_bonded(self.address):
                                        _LOGGER.info("BLE pair() retry failed but BlueZ reports bonded — proceeding")
                                        paired = True
                                if not paired:
                                    _LOGGER.warning(
                                        "BLE pair() failed and not bonded — auth may fail"
                                    )
                        if paired:
                            # Wait for bond to stabilize before proceeding with auth
                            await asyncio.sleep(1.0)
                            _LOGGER.debug("Bond established, proceeding with authentication")
                    else:
                        _LOGGER.debug("pair() not available on client wrapper")
                except NotImplementedError:
                    _LOGGER.info("pair() not implemented — may already be bonded")
                except Exception as exc:
                    if self.is_x180t_gateway:
                        _LOGGER.warning("X180T BLE pair() failed: %s", exc)
                        raise BleakError("X180T BLE bonding failed") from exc
                    _LOGGER.warning("pair() failed: %s — continuing", exc)
                finally:
                    if self.is_x180t_gateway and self._pin_agent_ctx:
                        await self._pin_agent_ctx.cleanup()
                        self._pin_agent_ctx = None
        else:
            if self.is_x180t_gateway:
                # Official X180T flow bonds first, then performs key/seed
                # unlock.  The PIN agent is already registered so BlueZ can
                # answer DisplayOnly/passkey requests during pair().
                try:
                    if self._pin_already_bonded:
                        _LOGGER.info(
                            "X180T PIN gateway %s — already bonded, skipping pair()",
                            self.address,
                        )
                    elif hasattr(client, "pair"):
                        _LOGGER.info("X180T PIN gateway %s — requesting BLE bond", self.address)
                        paired = await client.pair()
                        self._pin_dbus_succeeded = bool(paired)
                        _LOGGER.info("X180T BLE pair() result: %s", paired)
                        if not paired:
                            raise BleakError("X180T BLE PIN bonding returned False")
                    else:
                        _LOGGER.debug("pair() not available on client wrapper")
                except NotImplementedError:
                    _LOGGER.info("pair() not implemented — may already be bonded")
                except Exception as exc:
                    _LOGGER.warning("X180T BLE pair() failed: %s", exc)
                    raise BleakError("X180T BLE PIN bonding failed") from exc
                finally:
                    if self._pin_agent_ctx:
                        await self._pin_agent_ctx.cleanup()
                        self._pin_agent_ctx = None
            else:
                # Legacy PIN gateways: skip BLE SMP pair() entirely.
                # These gateways authenticate at the GATT application layer via
                # TEA key exchange (UNLOCK_STATUS/KEY characteristics). Calling
                # pair() causes an immediate AuthenticationFailed — the device
                # does not use SMP bonding at all.
                if self._pin_agent_ctx:
                    await self._pin_agent_ctx.cleanup()
                    self._pin_agent_ctx = None
                _LOGGER.info(
                    "PIN gateway %s — skipping BLE pair(), authenticating via GATT TEA",
                    self.address,
                )

        await asyncio.sleep(0.5)

        # ── Enumerate services (diagnostic) ───────────────────────────
        try:
            services = client.services
            if services:
                svc_uuids = [s.uuid for s in services]
                _LOGGER.debug("GATT services: %s", svc_uuids)
                # Check for CAN_WRITE and UNLOCK_STATUS to identify gateway protocol
                _has_unlock_status = False
                for svc in services:
                    for char in svc.characteristics:
                        if char.uuid == CAN_WRITE_CHAR_UUID:
                            self._has_can_write = True
                            _LOGGER.debug("CAN_WRITE characteristic available")
                        if char.uuid == UNLOCK_STATUS_CHAR_UUID:
                            _has_unlock_status = True
                # CAN-only gateways expose CAN service but no MyRvLink AUTH service.
                # X180T exposes the key/seed auth service as well, but official
                # app still routes it through the CAN-BLE adapter.
                if self._has_can_write and (not _has_unlock_status or self.is_x180t_gateway):
                    self._is_can_ble = True
                    _LOGGER.info(
                        "CAN BLE gateway detected%s — will use IDS-CAN BLE path",
                        " (X180T)" if self.is_x180t_gateway else " (no UNLOCK_STATUS)",
                    )
            else:
                _LOGGER.warning("No GATT services discovered")
        except Exception as exc:
            _LOGGER.warning("Failed to enumerate services: %s", exc)

        if self.is_x180t_gateway:
            self._is_can_ble = True

        if self._is_can_ble:
            # ── CAN-only path ──────────────────────────────────────────
            await self._authenticate_can_ble(client)
        else:
            # ── Step 1: Data Service Auth ─────────────────────────────────
            await self._authenticate_step1(client)

            await asyncio.sleep(NOTIFICATION_ENABLE_DELAY)

            # ── Enable notifications ──────────────────────────────────────
            await self._enable_notifications(client)

        _LOGGER.debug("OneControl %s — notifications enabled, waiting for SEED", self.address)

        # For non-PIN gateways authenticated in step 1, start the heartbeat now.
        # PIN gateways become authenticated in _authenticate_step2 after the
        # SEED handshake.  Query bootstrap and heartbeat start after GatewayInfo
        # so startup commands stay serialized.

        # ── Persist bonded source ─────────────────────────────────────
        # Only persist when authentication actually succeeded so that a failed
        # pairing attempt doesn't lock future connects to the wrong adapter.
        # For PIN gateways the bond is only valid when D-Bus pairing completed
        # (_pin_dbus_succeeded).  For non-PIN gateways, step 1 auth is enough.
        _pairing_ok = self._authenticated
        if self._current_connect_source is not None and _pairing_ok:
            stored_source = self.entry.options.get(CONF_BONDED_SOURCE)
            if stored_source != self._current_connect_source:
                _LOGGER.info(
                    "Persisting bonded source %s for %s",
                    self._current_connect_source, self.address,
                )
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    options={
                        **self.entry.options,
                        CONF_BONDED_SOURCE: self._current_connect_source,
                    },
                )

    # ------------------------------------------------------------------
    # CAN BLE gateway path (Unity XZ and similar IDS-CAN-only devices)
    # ------------------------------------------------------------------

    async def _authenticate_can_ble(self, client: BleakClient) -> None:
        """Authenticate a CAN-only gateway and subscribe to IDS-CAN frames.

        1. Try official CAN-BLE key/seed unlock (chars 00000012/00000013) when present.
        2. Read PASSWORD_UNLOCK (char 00000005): if locked, write gateway_pin as UTF-8.
        3. Subscribe CAN_READ (char 00000002) to receive inbound IDS-CAN frames.
          4. Mark authenticated and optionally send one DEVICE_ID broadcast when
              no devices are known yet.
        """
        _LOGGER.info("CAN BLE gateway %s — starting IDS-CAN authentication", self.address)

        # --- Official CAN-BLE key/seed unlock ---
        # BleManager performs this before BleCommunicationsAdapter opens the CAN
        # service on X180T-style gateways.  It is harmless on gateways that do not
        # expose these characteristics and may be required before writes are honored.
        unlock_success = False
        try:
            seed_data = await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID)
            if seed_data.lower() == b"unlocked":
                _LOGGER.debug("CAN BLE: key/seed unlock already verified")
                unlock_success = True
            elif len(seed_data) >= 4:
                seed = bytes(seed_data[:4])
                cipher = X180T_KEY_SEED_CIPHER if self.is_x180t_gateway else CAN_BLE_KEY_SEED_CIPHER
                key = calculate_can_ble_key_seed_key(seed, cipher)
                _LOGGER.debug("CAN BLE: key/seed unlock seed=%s key=computed", seed.hex())
                await client.write_gatt_char(KEY_CHAR_UUID, key, response=True)
                await asyncio.sleep(0.5)
                verify = await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID)
                _LOGGER.debug("CAN BLE: key/seed unlock verify=%s", verify.hex())
                # Check if unlock succeeded (should be "unlocked" or non-zero)
                if verify.lower() == b"unlocked" or (len(verify) > 0 and verify[0] != 0x00):
                    unlock_success = True
                    _LOGGER.debug("CAN BLE: key/seed unlock successful")
                else:
                    _LOGGER.warning("CAN BLE: key/seed unlock failed — verify=%s", verify.hex())
            else:
                _LOGGER.debug("CAN BLE: key/seed unlock seed unavailable: %s", seed_data.hex())
        except BleakError as exc:
            _LOGGER.debug("CAN BLE: key/seed unlock not available (%s)", exc)
        except Exception as exc:
            _LOGGER.warning("CAN BLE: key/seed unlock failed (%s) — proceeding", exc)

        # For X180T gateways, key/seed unlock is required
        if self.is_x180t_gateway and not unlock_success:
            raise BleakError("X180T CAN BLE key/seed unlock failed")

        # --- Gateway protocol version ---
        # Official app selects session strategy from this value:
        # V1 => IdsCanSessionManagerAuto (no explicit seed/key), V2/V2_D => explicit session open.
        try:
            version_data = bytes(await client.read_gatt_char(CAN_VERSION_CHAR_UUID))
            decoded_version = _official_can_ble_gateway_version_from_part(version_data)
            self._can_ble_gateway_version = decoded_version
            _LOGGER.debug(
                "CAN BLE: software part/version char=%s official_gateway_version=%s",
                version_data.hex(),
                decoded_version,
            )
        except BleakError as exc:
            _LOGGER.debug(
                "CAN BLE: software part/version char unavailable (%s); gateway_version remains %s",
                exc,
                self._can_ble_gateway_version,
            )
        except Exception as exc:
            _LOGGER.warning("CAN BLE: software part/version decode failed (%s)", exc)

        # --- PIN unlock ---
        # Skip PASSWORD_UNLOCK for X180T gateways as they use key/seed unlock only
        if not self.is_x180t_gateway:
            try:
                lock_data = await client.read_gatt_char(PASSWORD_UNLOCK_CHAR_UUID)
                locked = len(lock_data) == 0 or lock_data[0] == 0x00
                _LOGGER.debug(
                    "CAN BLE: PASSWORD_UNLOCK read = %s (locked=%s)", lock_data.hex(), locked
                )
                if locked:
                    pin_bytes = self.gateway_pin.encode("utf-8")
                    _LOGGER.debug("CAN BLE: writing gateway PIN to PASSWORD_UNLOCK")

                    unlock_ok = False
                    verify = b""
                    for attempt in range(2):
                        await client.write_gatt_char(
                            PASSWORD_UNLOCK_CHAR_UUID, pin_bytes, response=False
                        )
                        await asyncio.sleep(1.0)
                        verify = await client.read_gatt_char(PASSWORD_UNLOCK_CHAR_UUID)
                        if len(verify) > 0 and verify[0] != 0x00:
                            unlock_ok = True
                            break
                        if attempt == 0:
                            _LOGGER.warning(
                                "CAN BLE: PIN unlock verify still locked (%s), retrying write",
                                verify.hex(),
                            )

                    if unlock_ok:
                        _LOGGER.debug(
                            "CAN BLE: PIN unlock verified = %s", verify.hex()
                        )
                    else:
                        raise BleakError(
                            f"CAN BLE unlock failed — verify={verify.hex() or 'empty'}"
                        )
            except BleakError as exc:
                _LOGGER.warning(
                    "CAN BLE: PASSWORD_UNLOCK char not accessible (%s) — proceeding", exc
                )

        if self._can_ble_gateway_version == "Unknown":
            try:
                version_data = bytes(await client.read_gatt_char(CAN_VERSION_CHAR_UUID))
                decoded_version = _official_can_ble_gateway_version_from_part(version_data)
                self._can_ble_gateway_version = decoded_version
                _LOGGER.debug(
                    "CAN BLE: post-unlock software part/version char=%s official_gateway_version=%s",
                    version_data.hex(),
                    decoded_version,
                )
            except BleakError as exc:
                _LOGGER.info(
                    "CAN BLE: post-unlock software part/version char unavailable (%s); gateway_version remains %s",
                    exc,
                    self._can_ble_gateway_version,
                )
            except Exception as exc:
                _LOGGER.warning("CAN BLE: post-unlock software part/version decode failed (%s)", exc)

        # --- Subscribe CAN_READ for inbound frames ---
        try:
            await client.start_notify(CAN_READ_CHAR_UUID, self._on_can_read)
            _LOGGER.debug("CAN BLE: subscribed to CAN_READ (%s)", CAN_READ_CHAR_UUID)
            await asyncio.sleep(0.1)  # Brief delay to allow subscription to settle
            
            # Try reading one CAN_READ byte to verify subscription is active
            try:
                test_read = await client.read_gatt_char(CAN_READ_CHAR_UUID)
                _LOGGER.debug("CAN BLE: CAN_READ subscription verified (test read: %d bytes)", len(test_read))
            except Exception as read_exc:
                _LOGGER.debug("CAN BLE: CAN_READ test read not available: %s (normal for notify-only)", read_exc)
        except BleakError as exc:
            _LOGGER.warning("CAN BLE: could not subscribe CAN_READ: %s", exc)
            self._can_read_subscribed = False
            raise BleakError("CAN BLE authentication failed before CAN_READ subscribe") from exc

        self._authenticated = True
        self._can_ble_confirmed = True
        self._can_read_subscribed = True  # auth complete; live relay commands now safe
        self.async_set_updated_data(self._build_data())
        _LOGGER.info("CAN BLE gateway %s — authenticated", self.address)

        # Small gap so GATT notifications settle before writing
        await asyncio.sleep(0.2)

        # Official IDS-CAN adapters enable a LocalHost, wait for the bus to settle,
        # claim an unused source address with a NETWORK broadcast, then use that
        # claimed address for session and command traffic.  Without this, devices
        # may ignore session requests from an unknown source.
        await self._claim_can_local_host_address(client)

        # --- Flush any commands queued while gateway was between connections ---
        await self._flush_can_commands(client)

        # --- Kick off discovery and start link keepalive ---
        # Send immediate discovery to establish baseline connection and trigger first response
        await self._send_can_device_discovery(client)
        _LOGGER.debug("CAN BLE: initial device discovery sent")
        
        # Ensure any old keepalive task is cancelled
        if self._can_keepalive_task and not self._can_keepalive_task.done():
            self._can_keepalive_task.cancel()
        
        # Start keepalive loop which will send periodic discovery frames
        self._can_keepalive_task = self.hass.async_create_background_task(
            self._can_keepalive_loop(client), name="ha_onecontrol_can_keepalive"
        )
        _LOGGER.debug("CAN BLE: keepalive loop started for persistent connection")

    def _on_can_read(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle a CAN_READ notification — decode V2 BLE wrapper and dispatch entity events.

        V2 BLE frame types (byte 0):
          0x01 = Packed  → NETWORK + DEVICE_ID + DEVICE_STATUS + FakeCircuitID
          0x02 = ElevenBit → one 11-bit IDS-CAN frame
          0x03 = TwentyNineBit → one 29-bit IDS-CAN frame
        All other values: try V1 raw IDS-CAN parse directly.
        """
        raw = bytes(data)
        # Update last event time for keepalive tracking
        self._last_event_time = time.monotonic()
        if len(raw) > 0:
            _LOGGER.debug("CAN RX raw (%d bytes, type=0x%02X): %s", len(raw), raw[0] if raw else 0, raw.hex())
        can_frames = _decode_v2_ble_can_frames(raw)
        if not can_frames:
            # V1 or unrecognised — try raw parse
            wire = parse_ids_can_wire_frame(raw)
            if wire is None:
                _LOGGER.warning("CAN RX: unrecognised frame %s", raw.hex())
                return
            if self._can_ble_gateway_version == "Unknown":
                self._can_ble_gateway_version = "V1"
                _LOGGER.info(
                    "CAN BLE: inferred official_gateway_version=V1 from raw CAN_READ notification"
                )
            can_frames = [raw]
        for can_frame in can_frames:
            wire = parse_ids_can_wire_frame(can_frame)
            if wire is None:
                continue
            decoded = decode_ids_can_payload(wire)
            if wire.message_type == 0x07:
                self._can_time_source = wire.source_address & 0xFF
            if self._rc_session_seed_future is not None or self._rc_session_key_future is not None:
                _LOGGER.debug(
                    "CAN BLE: SESSION_WAIT RX mt=0x%02X src=0x%02X tgt=%s mdata=%s payload=%s",
                    wire.message_type,
                    wire.source_address,
                    f"0x{wire.target_address:02X}" if wire.target_address is not None else "N/A",
                    f"0x{wire.message_data:02X}" if wire.message_data is not None else "N/A",
                    wire.payload.hex(),
                )
            _LOGGER.debug(
                "PACKET RX IDS mt=0x%02X(%s) src=0x%02X tgt=%s mdata=%s dlc=%d payload=%s%s",
                wire.message_type,
                ids_can_message_type_name(wire.message_type),
                wire.source_address,
                f"0x{wire.target_address:02X}" if wire.target_address is not None else "N/A",
                f"0x{wire.message_data:02X}" if wire.message_data is not None else "N/A",
                wire.dlc,
                wire.payload.hex(),
                format_ids_can_payload(decoded),
            )
            # Learn controller source only from extended request/command traffic.
            # DEVICE_STATUS/NETWORK source addresses are often endpoint devices,
            # not the host-side controller address used for outgoing requests.
            if (
                wire.is_extended
                and decoded is not None
                and decoded.kind in {"request", "command"}
                and not self._can_local_host_claimed
            ):
                learned_source = wire.source_address & 0xFF
                if learned_source != self._gateway_can_address:
                    _LOGGER.info(
                        "CAN BLE: controller source learned from bus traffic 0x%02X -> 0x%02X",
                        self._gateway_can_address,
                        learned_source,
                    )
                    self._gateway_can_address = learned_source
            if wire.message_type >= 0x80:
                _LOGGER.debug(
                    "CAN BLE: EXT RX mt=0x%02X src=0x%02X tgt=%s mdata=%s payload=%s",
                    wire.message_type,
                    wire.source_address,
                    f"0x{wire.target_address:02X}" if wire.target_address is not None else "N/A",
                    f"0x{wire.message_data:02X}" if wire.message_data is not None else "N/A",
                    wire.payload.hex(),
                )
            # Session RESPONSE frames are handled separately; do not dispatch
            # as entity events.
            if wire.message_type == 0x81 and wire.message_data in (0x42, 0x43, 0x44, 0x45):
                _LOGGER.debug(
                    "CAN BLE: SESSION_RESPONSE rx src=0x%02X dst=0x%02X mdata=0x%02X payload=%s",
                    wire.source_address,
                    wire.target_address or 0,
                    wire.message_data or 0,
                    wire.payload.hex(),
                )
                self._handle_session_response(wire)
                continue
            self._dispatch_can_entity(wire, decoded)

    def _dispatch_can_entity(
        self,
        wire: "IdsCanWireFrame",
        decoded: "IdsCanDecodedPayload | None",
    ) -> None:
        """Map DEVICE_ID and DEVICE_STATUS IDS-CAN frames to HA entity state.

        DEVICE_ID (mt=0x02): cache device_type and populate device_names so
            entity platforms can display a human-readable label.
        DEVICE_STATUS (mt=0x03): construct a CoverStatus or RelayStatus and
            fire the coordinator event callbacks so cover/sensor platforms
            create or update their entities.

        Convention: table_id=0, device_id=source_address (IDS-CAN bus has no
        table concept; each gateway coordinator is a single-bus instance).
        """
        mt = wire.message_type
        src = wire.source_address

        if mt == 0x00 and decoded is not None:  # NETWORK — synthesize gateway_info + lockout
            proto = int(decoded.fields.get("protocol_version", 0))
            lockout = int(decoded.fields.get("in_motion_lockout_level", 0))
            self.system_lockout_level = lockout
            self.gateway_info = GatewayInformation(
                protocol_version=proto,
                table_id=0,
                device_count=len(self._can_device_types),
            )
            self._last_event_time = time.monotonic()
            self.async_set_updated_data(self._build_data())
            return

        if mt == 0x02 and decoded is not None:  # DEVICE_ID
            dev_type = int(decoded.fields.get("device_type", 0))
            label = str(decoded.fields.get("function_label", f"Device 0x{src:02X}"))
            self._can_device_types[src] = dev_type
            self.device_names[_device_key(0, src)] = label
            # Refresh device_count in gateway_info after each new device is seen
            if self.gateway_info is not None:
                self.gateway_info = GatewayInformation(
                    protocol_version=self.gateway_info.protocol_version,
                    table_id=0,
                    device_count=len(self._can_device_types),
                )
            return

        if mt != 0x03:  # only DEVICE_STATUS triggers entity updates
            return

        dev_type = self._can_device_types.get(src)
        if dev_type is None:
            return  # DEVICE_ID not yet seen for this source

        payload = wire.payload
        key = _device_key(0, src)
        event: CoverStatus | RelayStatus | None = None

        if dev_type == 33:  # H-Bridge / cover (slide-out, awning)
            status = payload[0] if len(payload) >= 1 else 0xC0
            pos: int | None = payload[1] if len(payload) >= 2 else None
            if pos == 0xFF:
                pos = None
            event = CoverStatus(table_id=0, device_id=src, status=status, position=pos)
            self.covers[key] = event

        elif dev_type == 30:  # Relay (light, switch)
            status_byte = payload[0] if len(payload) >= 1 else 0x00
            is_on = (status_byte & 0x0F) == 0x01
            event = RelayStatus(
                table_id=0, device_id=src, is_on=is_on, status_byte=status_byte
            )
            self.relays[key] = event

        if event is not None:
            self._last_event_time = time.monotonic()
            for cb in self._event_callbacks:
                try:
                    cb(event)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Error in CAN entity callback")
            self.async_set_updated_data(self._build_data())

    async def _send_can_device_discovery(self, client: BleakClient) -> None:
        """Broadcast a DEVICE_ID REQUEST to enumerate all devices on the IDS-CAN bus."""
        try:
            # REQUEST (0x80) + DEVICE_ID (0x02) broadcast to 0xFF, no payload
            frame = compose_ids_can_extended_wire_frame(
                message_type=0x80,
                source_address=self._gateway_can_address,
                target_address=0xFF,  # broadcast
                message_data=0x02,   # DEVICE_ID
                payload=b"",
            )
            await self._write_can_frame(client, frame, label="DEVICE_ID discovery")
            _LOGGER.debug(
                "CAN BLE: sent DEVICE_ID broadcast (%s)", frame.hex()
            )
        except Exception as exc:
            _LOGGER.warning("CAN BLE: failed to send device discovery: %s", exc)

    async def _send_can_keepalive_status(self, client: BleakClient) -> None:
        """Send a DEVICE_STATUS message as keepalive to maintain CAN BLE connection."""
        try:
            source = self._gateway_can_address & 0xFF
            frame = compose_ids_can_standard_wire_frame(
                message_type=0x03,  # DEVICE_STATUS
                source_address=source,
                payload=b"",
            )
            await self._write_can_frame(client, frame, label="DEVICE_STATUS keepalive")
            _LOGGER.debug("CAN BLE: sent DEVICE_STATUS keepalive from 0x%02X", source)
        except Exception as exc:
            _LOGGER.warning("CAN BLE: failed to send keepalive status: %s", exc)

    def _choose_can_local_host_address(self) -> int:
        """Choose a likely-unused IDS-CAN local host address.

        Official ``AddressDetectManager`` keeps a randomized list of all valid
        device addresses and returns the next one not seen during the initial
        one-second listen window.  Do not prefer 0xFA or a learned controller
        source; those may be real/reserved on some coaches.
        """
        observed = set(self._can_device_types.keys())
        if self._can_time_source is not None:
            observed.add(self._can_time_source & 0xFF)
        candidates = [candidate for candidate in range(0x01, 0xFF) if candidate not in observed]
        if candidates:
            seed = hashlib.sha256(self._can_local_host_mac + self.address.encode("utf-8")).digest()
            start = int.from_bytes(seed[:2], "big") % len(candidates)
            for offset in range(len(candidates)):
                candidate = candidates[(start + offset) % len(candidates)]
                if candidate not in (0x00, 0xFF):
                    return candidate
        for candidate in range(0x01, 0xFF):
            if candidate not in (0x00, 0xFF):
                return candidate
        return 0xFA

    async def _claim_can_local_host_address(self, client: BleakClient) -> None:
        """Claim the HA local host IDS-CAN source address like official LocalDevice."""
        if self._can_local_host_claimed:
            return
        # Official AddressDetectManager listens for one second before choosing.
        await asyncio.sleep(1.05)
        claimed = self._choose_can_local_host_address()
        self._gateway_can_address = claimed

        claim_payload = bytes([claimed, 0x12]) + self._can_local_host_mac
        claim_frame = compose_ids_can_standard_wire_frame(
            message_type=0x00,  # NETWORK
            source_address=0xFF,  # ADDRESS.BROADCAST during address claim
            payload=claim_payload,
        )
        _LOGGER.info(
            "CAN BLE: LocalHost address-claim addr=0x%02X mac=%s frame=%s",
            claimed,
            self._can_local_host_mac.hex(),
            claim_frame.hex(),
        )
        await self._write_can_frame(client, claim_frame, label="LocalHost ADDRESS_CLAIM")

        # Wait out the official 1s claim window, then advertise as online from
        # the claimed source.  Payload [status=0, version=0x12, mac6] mirrors
        # LocalDevice.TransmitNetworkMessage().
        await asyncio.sleep(1.05)
        online_frame = compose_ids_can_standard_wire_frame(
            message_type=0x00,
            source_address=claimed,
            payload=bytes([0x00, 0x12]) + self._can_local_host_mac,
        )
        _LOGGER.info(
            "CAN BLE: LocalHost NETWORK online addr=0x%02X frame=%s",
            claimed,
            online_frame.hex(),
        )
        await self._write_can_frame(client, online_frame, label="LocalHost NETWORK")
        self._can_local_host_claimed = True

        # Official LocalDevice does not stop at address claim. Once online,
        # its background task advertises the app-side LocalHost identity on
        # the CAN bus before command traffic: NETWORK, PRODUCT_STATUS,
        # DEVICE_ID, CIRCUIT_ID, and DEVICE_STATUS.  Some devices appear to
        # gate REMOTE_CONTROL acceptance on this LocalHost identity being
        # known, not just on the final COMMAND frame being correctly encoded.
        await self._advertise_can_local_host_identity(
            client, reason="post-claim", force=True
        )

    async def _advertise_can_local_host_identity(
        self, client: BleakClient, *, reason: str, force: bool = False
    ) -> None:
        """Advertise HA's IDS-CAN LocalHost like official app LocalDevice.

        Official app LocalHost is an Android mobile app IDS-CAN device:
        PRODUCT_ID=46 (OneControl Android Mobile App), DEVICE_TYPE=22
        (ANDROID_MOBILE_DEVICE), FUNCTION_NAME=2 (MYRV_TABLET), function
        instance/device instance/capabilities all zero for non-touch-panel
        Android builds. Product instance tracks the claimed CAN address.
        """
        if not self._can_local_host_claimed:
            return

        now = time.monotonic()
        if not force and now - self._can_local_host_identity_last_tx < 10.0:
            return

        source = self._gateway_can_address & 0xFF
        mac = self._can_local_host_mac

        frames: list[tuple[str, bytes]] = [
            (
                "LocalHost NETWORK",
                compose_ids_can_standard_wire_frame(
                    message_type=0x00,
                    source_address=source,
                    payload=bytes([0x00, 0x12]) + mac,
                ),
            ),
            (
                "LocalHost PRODUCT_STATUS",
                compose_ids_can_standard_wire_frame(
                    message_type=0x06,
                    source_address=source,
                    payload=b"\x00",
                ),
            ),
            (
                "LocalHost DEVICE_ID",
                compose_ids_can_standard_wire_frame(
                    message_type=0x02,
                    source_address=source,
                    payload=bytes(
                        [
                            0x00,
                            0x2E,  # PRODUCT_ID 46: OneControl Android Mobile App
                            source,  # product instance == LocalProduct address
                            0x16,  # DEVICE_TYPE 22: ANDROID_MOBILE_DEVICE
                            0x00,
                            0x02,  # FUNCTION_NAME 2: MYRV_TABLET
                            0x00,  # device_instance=0, function_instance=0
                            0x00,  # capabilities=0 for Android mobile app
                        ]
                    ),
                ),
            ),
            (
                "LocalHost CIRCUIT_ID",
                compose_ids_can_standard_wire_frame(
                    message_type=0x01,
                    source_address=source,
                    payload=b"\x00\x00\x00\x00",
                ),
            ),
            (
                "LocalHost DEVICE_STATUS",
                compose_ids_can_standard_wire_frame(
                    message_type=0x03,
                    source_address=source,
                    payload=b"",
                ),
            ),
        ]

        _LOGGER.debug(
            "CAN BLE: advertising official LocalHost identity reason=%s src=0x%02X mac=%s",
            reason,
            source,
            mac.hex(),
        )
        for label, frame in frames:
            await self._write_can_frame(client, frame, label=label)
            await asyncio.sleep(0.04)
        self._can_local_host_identity_last_tx = time.monotonic()

    async def _can_keepalive_loop(self, client: BleakClient) -> None:
        """Keep CAN-only BLE links active during idle windows.

        Some CAN-only gateways drop idle BLE links quickly unless periodic
        host traffic is present. Send keepalive frames every 30 seconds to keep
        the link alive and establish baseline connectivity.
        """
        try:
            # Send first keepalive immediately (no sleep) to prevent early disconnect
            if (
                self._connected
                and self._authenticated
                and self._can_read_subscribed
                and self._client is client
            ):
                _LOGGER.debug("CAN BLE: sending immediate keepalive (no delay)")
                try:
                    await self._advertise_can_local_host_identity(
                        client, reason="keepalive-initial", force=False
                    )
                    await self._send_can_keepalive_status(client)
                except Exception as exc:
                    _LOGGER.warning("CAN BLE: initial keepalive failed: %s", exc)
            
            # Then enter periodic keepalive loop
            while (
                self._connected
                and self._authenticated
                and self._can_read_subscribed
                and self._client is client
            ):
                # Longer interval (30s) to keep connection alive without excessive traffic
                await asyncio.sleep(30.0)
                if not (
                    self._connected
                    and self._authenticated
                    and self._can_read_subscribed
                    and self._client is client
                ):
                    break
                # Avoid interleaving broadcast keepalive traffic with
                # REMOTE_CONTROL session open/heartbeat traffic.
                if (
                    self._rc_session_seed_future is not None
                    or self._rc_session_key_future is not None
                    or self._rc_session_open
                ):
                    continue
                _LOGGER.debug("CAN BLE: keepalive tick (connected=%s authenticated=%s subscribed=%s)",
                    self._connected, self._authenticated, self._can_read_subscribed)
                try:
                    await self._advertise_can_local_host_identity(
                        client, reason="keepalive", force=False
                    )
                    await self._send_can_keepalive_status(client)
                except Exception as exc:
                    _LOGGER.warning("CAN BLE: keepalive tick failed: %s", exc)
                    # Don't break; let normal disconnect handling deal with it
        except asyncio.CancelledError:
            _LOGGER.debug("CAN BLE: keepalive loop cancelled")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("CAN BLE: keepalive loop ended due to error: %s", exc)

    async def _flush_can_commands(self, client: BleakClient) -> None:
        """Flush any relay/cover commands queued while the gateway was disconnected.

        Commands older than 30 s are discarded as stale.
        """
        _CAN_CMD_TTL = 30.0
        now = time.monotonic()
        fresh = [(frame, t, did) for frame, t, did in self._can_commands_queue if now - t < _CAN_CMD_TTL]
        stale = len(self._can_commands_queue) - len(fresh)
        self._can_commands_queue.clear()
        if stale:
            _LOGGER.debug("CAN BLE: discarded %d stale queued command(s)", stale)
        for frame, _, device_id in fresh:
            try:
                await self._advertise_can_local_host_identity(
                    client, reason="pre-queued-command", force=True
                )
                # Open/activate REMOTE_CONTROL before sending the COMMAND frame.
                if not await self._ensure_remote_control_session(client, device_id):
                    _LOGGER.warning(
                        "CAN BLE: queued COMMAND skipped — REMOTE_CONTROL activation failed for device=0x%02X gateway_version=%s",
                        device_id,
                        self._can_ble_gateway_version,
                    )
                    continue
                parsed = parse_ids_can_wire_frame(frame)
                command = parsed.message_data if parsed and parsed.message_data is not None else 0x00
                current_frame = compose_ids_can_extended_wire_frame(
                    message_type=0x82,
                    source_address=self._gateway_can_address,
                    target_address=device_id,
                    message_data=command,
                    payload=b"",
                )
                _LOGGER.info(
                    "CAN BLE: flushing queued command src=0x%02X device=0x%02X frame=%s",
                    self._gateway_can_address,
                    device_id,
                    current_frame.hex(),
                )
                await self._write_can_frame(client, current_frame, label="queued COMMAND")
                await asyncio.sleep(0.1)
            except Exception as exc:
                _LOGGER.warning("CAN BLE: failed to flush queued command: %s", exc)

    # ------------------------------------------------------------------
    # REMOTE_CONTROL session management (IDS-CAN session ID 4)
    # ------------------------------------------------------------------

    _RC_SESSION_ID = 0x0004
    _RC_SESSION_ID_HI = 0x00
    _RC_SESSION_ID_LO = 0x04

    def _handle_session_response(self, wire: "IdsCanWireFrame") -> None:
        """Dispatch IDS-CAN RESPONSE (0x81) session frames to awaiting futures.

        Called from _on_can_read for message_data values 0x42–0x45.
        """
        payload = wire.payload
        msg = wire.message_data

        # Some gateways return single-byte IDS status responses for session
        # requests instead of full session payloads.
        if len(payload) == 1 and msg in (0x42, 0x43):
            status_code = payload[0] & 0xFF
            self._rc_session_last_status_code = status_code
            status_name = ids_can_response_name(status_code)
            _LOGGER.warning(
                "CAN BLE: SESSION_RESPONSE status req=0x%02X code=0x%02X(%s)",
                msg,
                status_code,
                status_name,
            )
            err = RuntimeError(
                f"session req 0x{msg:02X} rejected with 0x{status_code:02X}({status_name})"
            )
            if msg == 0x42 and self._rc_session_seed_future and not self._rc_session_seed_future.done():
                self._rc_session_seed_future.set_exception(err)
            if msg == 0x43 and self._rc_session_key_future and not self._rc_session_key_future.done():
                self._rc_session_key_future.set_exception(err)
            return

        if len(payload) < 2:
            return
        # Validate that the session ID in the payload matches REMOTE_CONTROL.
        sid = (payload[0] << 8) | payload[1]
        if sid not in (self._RC_SESSION_ID, 0x0400):
            return

        if msg == 0x42:  # SESSION_REQUEST_SEED response — payload: [sid×2, seed×4]
            if len(payload) == 6 and self._rc_session_seed_future and not self._rc_session_seed_future.done():
                seed = int.from_bytes(payload[2:6], "big")
                _LOGGER.debug("CAN BLE: session seed rx 0x%08X", seed)
                self._rc_session_seed_future.set_result(seed)

        elif msg == 0x43:  # SESSION_TRANSMIT_KEY response — payload: [sid×2]
            if len(payload) == 2 and self._rc_session_key_future and not self._rc_session_key_future.done():
                _LOGGER.debug("CAN BLE: session key confirmed — REMOTE_CONTROL session open")
                self._rc_session_key_future.set_result(None)

        elif msg in (0x44, 0x45):  # Device closing the session (heartbeat close / session end)
            if len(payload) >= 3:
                reason = payload[2]
                _LOGGER.info(
                    "CAN BLE: REMOTE_CONTROL session terminated by device, reason=0x%02X", reason
                )
            self._rc_session_open = False
            self._rc_session_target = None
            if self._rc_heartbeat_task and not self._rc_heartbeat_task.done():
                self._rc_heartbeat_task.cancel()

    async def _ensure_remote_control_session(
        self, client: BleakClient, device_id: int
    ) -> bool:
        """Open a REMOTE_CONTROL IDS-CAN session with *device_id* if not yet open.

        Protocol (from decompiled SessionClient.cs):
          1. Send REQUEST(0x80) msg_data=0x42 payload=[0x00, 0x04] → seed request
          2. Receive RESPONSE(0x81) msg_data=0x42 payload=[sid×2, seed×4]
          3. Encrypt seed with TEA(REMOTE_CONTROL cypher from SESSION_ID descriptors)
          4. Send REQUEST(0x80) msg_data=0x43 payload=[sid×2, key×4] → transmit key
          5. Receive RESPONSE(0x81) msg_data=0x43 payload=[sid×2] → session open
        Returns True when session is (already) open.
        """
        async with self._rc_session_lock:
            v1_gateway = self._is_can_ble_v1_gateway()
            if not v1_gateway and self._rc_session_open and self._rc_session_target == device_id:
                return True

            if v1_gateway:
                _LOGGER.debug(
                    "CAN BLE: V1/24955 gateway — opening explicit REMOTE_CONTROL seed/key after LocalHost identity src=0x%02X device=0x%02X",
                    self._gateway_can_address,
                    device_id,
                )

            # Cancel heartbeat for any previous session with a different device.
            if self._rc_heartbeat_task and not self._rc_heartbeat_task.done():
                self._rc_heartbeat_task.cancel()
            self._rc_session_open = False
            self._rc_session_target = None

            _LOGGER.info(
                "CAN BLE: opening REMOTE_CONTROL session with device 0x%02X", device_id
            )
            self._rc_session_last_status_code = None
            sid_candidates = (bytes([self._RC_SESSION_ID_HI, self._RC_SESSION_ID_LO]),)

            # ── Step 1: request seed ────────────────────────────────
            loop = asyncio.get_running_loop()
            source_candidates: list[int] = []
            dynamic_time_source = None if v1_gateway else self._can_time_source
            raw_source_candidates = (
                (self._gateway_can_address,)
                if v1_gateway
                else (
                    self._gateway_can_address,
                    dynamic_time_source,
                    0xFA,
                    *sorted(self._can_device_types.keys()),
                    0x3A,
                    0x02,
                    0x12,
                )
            )
            for src in raw_source_candidates:
                if src is None:
                    continue
                if src not in source_candidates:
                    source_candidates.append(src)

            selected_source: int | None = None
            selected_target: int | None = None
            selected_sid_bytes: bytes | None = None
            seed: int | None = None

            target_candidates: list[int] = []
            raw_target_candidates = (device_id,) if v1_gateway else (device_id, self._can_time_source)
            for dst in raw_target_candidates:
                if dst is None:
                    continue
                if dst not in target_candidates:
                    target_candidates.append(dst)

            # Official IDS-CAN flow queries device sessions first
            # (device.Sessions.QueryDevice()) before trying to open one.
            # On some controllers this appears required before 0x42/0x43 are accepted.
            _LOGGER.debug(
                "CAN BLE: preflight call entering src=%s dst=%s",
                ",".join(f"0x{s:02X}" for s in source_candidates),
                ",".join(f"0x{d:02X}" for d in target_candidates),
            )
            await self._query_remote_control_sessions(
                client=client,
                source_candidates=source_candidates,
                target_candidates=target_candidates,
            )
            _LOGGER.debug("CAN BLE: preflight call completed")

            for dst in target_candidates:
                for sid_bytes in sid_candidates:
                    for src in source_candidates:
                        seed_req = compose_ids_can_extended_wire_frame(
                            message_type=0x80,
                            source_address=src,
                            target_address=dst,
                            message_data=0x42,  # SESSION_REQUEST_SEED
                            payload=sid_bytes,
                        )
                        self._rc_session_seed_future = loop.create_future()
                        try:
                            _LOGGER.debug(
                                "CAN BLE: SESSION_REQUEST_SEED tx src=0x%02X dst=0x%02X sid=%s frame=%s",
                                src,
                                dst,
                                sid_bytes.hex(),
                                seed_req.hex(),
                            )
                            await self._write_can_frame(client, seed_req, label="SESSION_REQUEST_SEED")
                            seed = await asyncio.wait_for(
                                asyncio.shield(self._rc_session_seed_future), timeout=0.45
                            )
                            selected_source = src
                            selected_target = dst
                            selected_sid_bytes = sid_bytes
                            break
                        except asyncio.TimeoutError:
                            _LOGGER.debug(
                                "CAN BLE: SESSION_REQUEST_SEED timeout for src=0x%02X dst=0x%02X sid=%s",
                                src,
                                dst,
                                sid_bytes.hex(),
                            )
                            continue
                        except Exception as exc:
                            _LOGGER.warning(
                                "CAN BLE: SESSION_REQUEST_SEED error for src=0x%02X dst=0x%02X sid=%s: %s",
                                src,
                                dst,
                                sid_bytes.hex(),
                                exc,
                            )
                            continue
                        finally:
                            self._rc_session_seed_future = None
                    if selected_source is not None:
                        break
                if selected_source is not None:
                    break

            if (
                selected_source is None
                or selected_target is None
                or selected_sid_bytes is None
                or seed is None
            ):
                if v1_gateway:
                    _LOGGER.warning(
                        "CAN BLE: explicit REMOTE_CONTROL seed timed out on V1/24955 device 0x%02X after LocalHost identity (src=%s dst=%s); falling back to official auto-session wrapper",
                        device_id,
                        ",".join(f"0x{s:02X}" for s in source_candidates),
                        ",".join(f"0x{d:02X}" for d in target_candidates),
                    )
                    self._rc_session_open = False
                    self._rc_session_target = device_id
                    return True
                else:
                    _LOGGER.warning(
                        "CAN BLE: REMOTE_CONTROL session seed timed out for device 0x%02X (tried src=%s dst=%s sid=0004)",
                        device_id,
                        ",".join(f"0x{s:02X}" for s in source_candidates),
                        ",".join(f"0x{d:02X}" for d in target_candidates),
                    )
                    return False

            if selected_source != self._gateway_can_address:
                _LOGGER.info(
                    "CAN BLE: session source accepted as 0x%02X (was 0x%02X)",
                    selected_source,
                    self._gateway_can_address,
                )
                self._gateway_can_address = selected_source

            # ── Step 2: encrypt seed, transmit key ────────────────
            key = tea_encrypt(RC_CYPHER, seed)
            key_req = compose_ids_can_extended_wire_frame(
                message_type=0x80,
                source_address=selected_source,
                target_address=selected_target,
                message_data=0x43,  # SESSION_TRANSMIT_KEY
                payload=selected_sid_bytes + key.to_bytes(4, "big"),
            )
            self._rc_session_key_future = loop.create_future()
            try:
                _LOGGER.debug(
                    "CAN BLE: SESSION_TRANSMIT_KEY tx src=0x%02X dst=0x%02X frame=%s",
                    selected_source,
                    device_id,
                    key_req.hex(),
                )
                await self._write_can_frame(client, key_req, label="SESSION_TRANSMIT_KEY")
                await asyncio.wait_for(
                    asyncio.shield(self._rc_session_key_future), timeout=3.0
                )
            except asyncio.TimeoutError:
                _LOGGER.warning("CAN BLE: REMOTE_CONTROL session key timed out for device 0x%02X", device_id)
                self._rc_session_key_future = None
                return False
            except Exception as exc:
                if self._rc_session_last_status_code is not None:
                    _LOGGER.warning(
                        "CAN BLE: REMOTE_CONTROL session key rejected for device 0x%02X status=0x%02X(%s)",
                        device_id,
                        self._rc_session_last_status_code,
                        ids_can_response_name(self._rc_session_last_status_code),
                    )
                else:
                    _LOGGER.warning("CAN BLE: REMOTE_CONTROL session key error: %s", exc)
                self._rc_session_key_future = None
                return False
            finally:
                self._rc_session_key_future = None

            self._rc_session_open = True
            self._rc_session_target = device_id
            _LOGGER.info(
                "CAN BLE: REMOTE_CONTROL session established for device 0x%02X", device_id
            )
            # SessionKeepAliveTime=0: no heartbeat needed — session expires naturally
            # on the device side after ~5s of inactivity.
            return True

    async def _query_remote_control_sessions(
        self,
        client: BleakClient,
        source_candidates: list[int],
        target_candidates: list[int],
    ) -> None:
        """Query session list/status before requesting seed for REMOTE_CONTROL.

        Decompiled official flow (IdsCanSessionManager.ActivateSessionAsync) calls
        `device.Sessions.QueryDevice()` whenever no session object is present.
                Official request payloads are not empty:
                    0x40 = SESSION_READ_LIST   payload=[index_hi, index_lo]
                    0x41 = SESSION_READ_STATUS payload=[session_hi, session_lo]
        """
        _LOGGER.debug(
            "CAN BLE: session preflight begin src=%s dst=%s",
            ",".join(f"0x{s:02X}" for s in source_candidates),
            ",".join(f"0x{d:02X}" for d in target_candidates),
        )
        try:
            sent = 0
            for dst in target_candidates:
                # Keep traffic small: probe with the first two most likely sources.
                for src in source_candidates[:2]:
                    preflight_requests = (
                        (0x40, "SESSION_READ_LIST", b"\x00\x00"),
                        # The first read-list response can report up to two supported
                        # sessions; index 1 mirrors the official paged query if more
                        # than two are present.
                        (0x40, "SESSION_READ_LIST", b"\x00\x01"),
                        (0x41, "SESSION_READ_STATUS", bytes([self._RC_SESSION_ID_HI, self._RC_SESSION_ID_LO])),
                    )
                    for req_code, label, payload in preflight_requests:
                        frame = compose_ids_can_extended_wire_frame(
                            message_type=0x80,
                            source_address=src,
                            target_address=dst,
                            message_data=req_code,
                            payload=payload,
                        )
                        _LOGGER.debug(
                            "CAN BLE: %s preflight tx src=0x%02X dst=0x%02X payload=%s frame=%s",
                            label,
                            src,
                            dst,
                            payload.hex(),
                            frame.hex(),
                        )
                        await self._write_can_frame(client, frame, label=label)
                        sent += 1
                        await asyncio.sleep(0.05)
            if sent:
                # Allow quick processing window before seed request loop.
                await asyncio.sleep(0.20)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("CAN BLE: session preflight query failed: %s", exc)

    async def _rc_session_heartbeat(self, client: BleakClient, device_id: int) -> None:
        """Send SESSION_HEARTBEAT (0x44) every 4 s to keep REMOTE_CONTROL session alive.

        The device closes the session if no heartbeat arrives within 5 s.
        """
        sid_bytes = bytes([self._RC_SESSION_ID_HI, self._RC_SESSION_ID_LO])
        hb_frame = compose_ids_can_extended_wire_frame(
            message_type=0x80,
            source_address=self._gateway_can_address,
            target_address=device_id,
            message_data=0x44,  # SESSION_HEARTBEAT
            payload=sid_bytes,
        )
        try:
            while self._rc_session_open and self._rc_session_target == device_id and self._connected:
                await asyncio.sleep(4.0)
                if not (self._rc_session_open and self._rc_session_target == device_id and self._connected):
                    break
                try:
                    await self._write_can_frame(client, hb_frame, label="SESSION_HEARTBEAT")
                    _LOGGER.debug(
                        "CAN BLE: REMOTE_CONTROL heartbeat → device 0x%02X", device_id
                    )
                except Exception as exc:
                    _LOGGER.warning("CAN BLE: REMOTE_CONTROL heartbeat failed: %s", exc)
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if self._rc_session_target == device_id:
                self._rc_session_open = False
                self._rc_session_target = None

    # ------------------------------------------------------------------
    # Step 1: UNLOCK_STATUS challenge → KEY response
    # ------------------------------------------------------------------

    async def _authenticate_step1(self, client: BleakClient) -> None:
        """Read UNLOCK_STATUS, compute 4-byte TEA key, write to KEY."""
        _LOGGER.debug("Step 1: reading UNLOCK_STATUS")
        try:
            data = await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID)
        except BleakError as exc:
            _LOGGER.warning("Step 1: failed to read UNLOCK_STATUS: %s", exc)
            return

        text = data.decode("utf-8", errors="replace")
        if "unlocked" in text.lower():
            _LOGGER.info("Step 1: gateway already unlocked")
            self._authenticated = True
            return

        if len(data) != 4:
            _LOGGER.warning("Step 1: unexpected UNLOCK_STATUS size %d", len(data))
            return

        if data == b"\x00\x00\x00\x00":
            _LOGGER.warning("Step 1: all-zeros challenge — gateway not ready")
            return

        _LOGGER.debug("Step 1: challenge = %s", data.hex())
        key = calculate_step1_key(data)
        _LOGGER.debug("Step 1: writing key = %s", key.hex())

        await client.write_gatt_char(KEY_CHAR_UUID, key, response=False)

        await asyncio.sleep(UNLOCK_VERIFY_DELAY)
        verify = await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID)
        verify_text = verify.decode("utf-8", errors="replace")
        if "unlocked" in verify_text.lower():
            _LOGGER.info("Step 1: gateway UNLOCKED")
            self._authenticated = True
            self.async_set_updated_data(self._build_data())
        else:
            _LOGGER.warning("Step 1: unlock verify failed — got %s", verify.hex())

    # ------------------------------------------------------------------
    # Enable notifications
    # ------------------------------------------------------------------

    async def _enable_notifications(self, client: BleakClient) -> None:
        """Subscribe to DATA_READ and SEED characteristics."""
        try:
            await client.start_notify(DATA_READ_CHAR_UUID, self._on_data_read)
            _LOGGER.debug("Subscribed to DATA_READ (0x0034)")
        except BleakError as exc:
            _LOGGER.warning("Failed to subscribe DATA_READ: %s", exc)

        try:
            await client.start_notify(SEED_CHAR_UUID, self._on_seed_notification)
            _LOGGER.debug("Subscribed to SEED (0x0011)")
        except BleakError as exc:
            _LOGGER.warning("Failed to subscribe SEED: %s", exc)

    async def _remove_stale_bond(self) -> None:
        """Remove a stale bond and reset for re-pairing.

        Called when authentication fails repeatedly on a PIN gateway,
        suggesting the bond keys are out of sync.
        """
        if not self.is_pin_gateway:
            return

        _LOGGER.info("Removing stale bond for PIN gateway %s", self.address)
        removed = await remove_bond(self.address)
        if removed:
            self._pin_already_bonded = False
            _LOGGER.info("Bond removed — will re-pair on next connection")
        else:
            _LOGGER.warning("Could not remove bond for %s", self.address)

    # ------------------------------------------------------------------
    # Step 2: SEED notification → 16-byte KEY response
    # ------------------------------------------------------------------

    def _on_seed_notification(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle SEED notification — schedule Step 2 auth."""
        _LOGGER.debug("Step 2: SEED notification = %s", bytes(data).hex())
        self.hass.async_create_task(self._authenticate_step2(bytes(data)))

    async def _authenticate_step2(self, seed: bytes) -> None:
        """Compute 16-byte auth key and write to KEY characteristic."""
        if len(seed) != 4:
            _LOGGER.warning("Step 2: unexpected seed size %d", len(seed))
            return

        key = calculate_step2_key(seed, self.gateway_pin)
        _LOGGER.debug("Step 2: writing auth key = %s", key.hex())

        if self._client is None:
            _LOGGER.warning("Step 2: no BLE client")
            return

        try:
            await self._client.write_gatt_char(KEY_CHAR_UUID, key, response=False)
            _LOGGER.info("Step 2: auth key written — authentication complete")
            self._authenticated = True
            self.async_set_updated_data(self._build_data())
        except BleakError as exc:
            _LOGGER.error("Step 2: failed to write KEY: %s", exc)

    # ------------------------------------------------------------------
    # Metadata request (triggered 500ms after GatewayInfo)
    # ------------------------------------------------------------------

    async def _send_metadata_request(self, table_id: int) -> None:
        """Send GetDevicesMetadata for a single table ID."""
        cmd = self._cmd.build_get_devices_metadata(table_id)
        cmd_id = int.from_bytes(cmd[0:2], "little")
        self._pending_metadata_cmdids[cmd_id] = table_id
        self._pending_metadata_entries.pop(cmd_id, None)
        self._metadata_requested_tables.add(table_id)
        try:
            await self.async_send_command(cmd)
            _LOGGER.info("Sent GetDevicesMetadata for table %d (cmdId=%d)", table_id, cmd_id)
        except Exception as exc:
            self._pending_metadata_cmdids.pop(cmd_id, None)
            self._pending_metadata_entries.pop(cmd_id, None)
            _LOGGER.warning("Failed to send metadata request: %s", exc)

    async def _retry_metadata_after_rejection(self, table_id: int) -> None:
        """Retry GetDevicesMetadata 10s after a rejection.

        At most one retry task is queued per table at any time; callers must
        check _metadata_retry_pending before scheduling.
        """
        try:
            await asyncio.sleep(10.0)
            if not self._connected:
                return
            if self._is_startup_bootstrap_active(table_id):
                _LOGGER.debug(
                    "Retry for metadata table %d suppressed — startup bootstrap active",
                    table_id,
                )
                return
            if table_id in self._metadata_loaded_tables:
                return
            _LOGGER.debug("Retrying metadata for table_id=%d after 0x0f rejection", table_id)
            self._metadata_requested_tables.discard(table_id)
            if table_id not in self._get_devices_loaded_tables:
                self._cmd_correlation_stats["metadata_waiting_get_devices"] += 1
                _LOGGER.debug(
                    "Retry for metadata table %d deferred — waiting for GetDevices completion",
                    table_id,
                )
                return
            await self._send_metadata_request(table_id)
        finally:
            self._metadata_retry_pending.discard(table_id)

    async def _send_initial_get_devices(self) -> None:
        """Send GetDevices at T+500ms to wake the gateway before metadata is requested.

        Mirrors v2.7.2 Android plugin sequencing: GetDevices fires 500ms after
        notifications are enabled, metadata fires 1500ms after.  Some gateway
        firmware requires the device-list request to be processed before it will
        serve GetDevicesMetadata.

        If GatewayInfo hasn't arrived within 500ms this call is a no-op; the
        GatewayInfo handler will call _do_send_initial_get_devices() directly
        as a fallback when it stores the first GatewayInfo event.
        """
        await asyncio.sleep(0.5)
        if self.gateway_info is not None:
            self._ensure_startup_bootstrap(self.gateway_info.table_id)

    async def _do_send_initial_get_devices(self) -> None:
        """Send the initial GetDevices command if not already sent.

        Idempotent — skipped if already sent or if connection/auth state is invalid.
        """
        if self._initial_get_devices_sent:
            return
        if not self._connected or not self._authenticated:
            return
        if self.gateway_info is None:
            return
        try:
            cmd_id = await self._send_get_devices_request(self.gateway_info.table_id)
            self._initial_get_devices_sent = True
            _LOGGER.debug(
                "Initial GetDevices sent for table %d (cmdId=%d)",
                self.gateway_info.table_id, cmd_id,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Initial GetDevices failed: %s", exc)

    async def _request_metadata_after_delay(self, table_id: int) -> None:
        """Wait 1500ms then request metadata.

        The 1.5 s delay matches the v2.7.2 Android plugin (GetDevices at T+500ms,
        metadata at T+1500ms), giving the gateway time to process the device-list
        request before we ask for metadata.
        """
        await asyncio.sleep(1.5)
        if self._is_startup_bootstrap_active(table_id):
            _LOGGER.debug(
                "Metadata request for table %d suppressed — startup bootstrap active",
                table_id,
            )
            return
        if table_id in self._metadata_loaded_tables:
            return
        if table_id in self._metadata_requested_tables:
            return
        if table_id not in self._get_devices_loaded_tables:
            self._cmd_correlation_stats["metadata_waiting_get_devices"] += 1
            _LOGGER.debug(
                "Metadata request deferred for table %d — waiting for GetDevices completion",
                table_id,
            )
            return
        await self._send_metadata_request(table_id)

    def _ensure_metadata_for_table(self, table_id: int) -> None:
        """Request metadata for an observed table_id if not yet requested/loaded/rejected.

        Implements the observed-table path: any status event carrying a table_id
        triggers a metadata request for that table if we haven't already loaded or
        requested it.  This mirrors Android's ensureMetadataRequestedForTable().
        """
        if table_id == 0:
            return
        if (
            table_id in self._metadata_loaded_tables
            or table_id in self._metadata_requested_tables
        ):
            return
        if self._is_startup_bootstrap_active(table_id):
            _LOGGER.debug(
                "Observed table_id=%d while startup bootstrap is active — waiting",
                table_id,
            )
            return
        if table_id not in self._get_devices_loaded_tables:
            self._cmd_correlation_stats["metadata_waiting_get_devices"] += 1
            _LOGGER.debug(
                "Observed table_id=%d but delaying metadata until GetDevices completes",
                table_id,
            )
            return
        _LOGGER.debug("Requesting metadata for observed table_id=%d", table_id)
        self.hass.async_create_task(self._send_metadata_request(table_id))

    # ------------------------------------------------------------------
    # Heartbeat keepalive (GetDevices every 5 seconds)
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Start the heartbeat loop after authentication."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._stop_heartbeat()
        self._heartbeat_task = self.hass.async_create_background_task(
            self._heartbeat_loop(), name="ha_onecontrol_heartbeat"
        )
        _LOGGER.debug("Heartbeat started (every %.0fs)", HEARTBEAT_INTERVAL)

    def _stop_heartbeat(self) -> None:
        """Cancel the heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
            _LOGGER.debug("Heartbeat stopped")

    async def _heartbeat_loop(self) -> None:
        """Send GetDevices periodically to keep BLE connection alive.

        Also monitors data freshness — if no events for STALE_CONNECTION_TIMEOUT
        seconds, forces a reconnect.

        Reference: Android HEARTBEAT_INTERVAL_MS = 5000L
        """
        try:
            while self._connected and self._authenticated:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self._connected or not self.gateway_info:
                    break

                # Stale connection detection
                if (
                    self._last_event_time > 0
                    and (time.monotonic() - self._last_event_time) > STALE_CONNECTION_TIMEOUT
                ):
                    _LOGGER.warning(
                        "No events for %.0fs — connection stale, forcing reconnect",
                        STALE_CONNECTION_TIMEOUT,
                    )
                    if self._client:
                        try:
                            await self._client.disconnect()
                        except Exception:
                            pass
                    break

                try:
                    if self._is_startup_bootstrap_active(self.gateway_info.table_id):
                        continue
                    await self._send_get_devices_request(self.gateway_info.table_id)
                except BleakError as exc:
                    _LOGGER.warning("Heartbeat BLE write failed: %s", exc)
                    break
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Heartbeat error")
                    break
        except asyncio.CancelledError:
            pass
        _LOGGER.debug("Heartbeat loop exited")

    # ------------------------------------------------------------------
    # DATA_READ notification handler (COBS stream)
    # ------------------------------------------------------------------

    def _on_data_read(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Receive raw bytes from DATA_READ, feed through COBS decoder."""
        for byte_val in data:
            frame = self._decoder.decode_byte(byte_val)
            if frame is not None:
                self._process_frame(frame)

    def _process_frame(self, frame: bytes) -> None:
        """Parse a decoded COBS frame and update coordinator state."""
        if not frame:
            return

        # Track data freshness
        self._last_event_time = time.monotonic()

        event_type = frame[0]

        # Detect metadata error/completion responses before full parse.
        # responseType byte 3: 0x01=SuccessMulti, 0x81=SuccessComplete, 0x02/0x82=Fail
        # Reference: METADATA_RETRIEVAL.md § Response Format; MyRvLinkCommandGetDevicesMetadata.cs
        if event_type == 0x02 and len(frame) >= 4:
            response_type = frame[3] & 0xFF
            if response_type == 0x81:
                # SuccessComplete: final frame carrying DeviceMetadataTableCrc (bytes 4–7 LE)
                # and total device count (byte 8). Validate CRC against GatewayInformation.
                cmd_id = (frame[1] & 0xFF) | ((frame[2] & 0xFF) << 8)
                completed_get_devices_table = self._pending_get_devices_cmdids.pop(cmd_id, None)
                if completed_get_devices_table is not None:
                    self._cmd_correlation_stats["get_devices_completed"] += 1
                    self._get_devices_loaded_tables.add(completed_get_devices_table)
                    self._resolve_bootstrap_waiter(
                        "get_devices", completed_get_devices_table, "completed"
                    )
                    _LOGGER.debug(
                        "GetDevices completion frame (cmdId=%d table=%d, loaded_tables=%d)",
                        cmd_id,
                        completed_get_devices_table,
                        len(self._get_devices_loaded_tables),
                    )
                    if (
                        completed_get_devices_table not in self._metadata_loaded_tables
                        and completed_get_devices_table not in self._metadata_requested_tables
                        and not self._is_startup_bootstrap_active(completed_get_devices_table)
                    ):
                        _LOGGER.debug(
                            "Scheduling metadata request after GetDevices completion for table %d",
                            completed_get_devices_table,
                        )
                        self.hass.async_create_task(
                            self._send_metadata_request(completed_get_devices_table)
                        )
                    return
                completed_table = self._pending_metadata_cmdids.pop(cmd_id, None)
                if completed_table is not None and len(frame) >= 8:
                    # CRC is big-endian per MyRvLinkCommandGetDevicesMetadataResponseCompleted.cs
                    # (GetValueUInt32 defaults to Endian.Big in ArrayExtension.cs)
                    response_crc = int.from_bytes(frame[4:8], "big")
                    response_count = frame[8] & 0xFF if len(frame) >= 9 else None
                    staged_entries = self._pending_metadata_entries.pop(cmd_id, {})
                    staged_count = len(staged_entries)
                    expected_crc = (
                        self.gateway_info.device_metadata_table_crc
                        if self.gateway_info is not None
                        else 0
                    )
                    if expected_crc != 0 and response_crc != expected_crc:
                        self._cmd_correlation_stats["metadata_commit_crc_mismatch"] += 1
                        _LOGGER.warning(
                            "Metadata CRC mismatch for table %d: "
                            "response=0x%08x, expected=0x%08x — discarding",
                            completed_table,
                            response_crc,
                            expected_crc,
                        )
                        self._metadata_loaded_tables.discard(completed_table)
                        self._metadata_requested_tables.discard(completed_table)
                        self._last_metadata_crc = None
                    elif response_count is not None and response_count != staged_count:
                        self._cmd_correlation_stats["metadata_commit_count_mismatch"] += 1
                        _LOGGER.warning(
                            "Metadata count mismatch for table %d: completed=%d staged=%d — discarding",
                            completed_table,
                            response_count,
                            staged_count,
                        )
                        self._metadata_loaded_tables.discard(completed_table)
                        self._metadata_requested_tables.discard(completed_table)
                        self._last_metadata_crc = None
                    else:
                        for meta in staged_entries.values():
                            self._process_metadata(meta)
                        self._metadata_loaded_tables.add(completed_table)
                        self._last_metadata_crc = response_crc
                        self._cmd_correlation_stats["metadata_commit_success"] += 1
                        self._resolve_bootstrap_waiter("metadata", completed_table, "completed")
                        _LOGGER.debug(
                            "Metadata completion OK for table %d (CRC=0x%08x, entries=%d)",
                            completed_table,
                            response_crc,
                            staged_count,
                        )
                        self.hass.async_create_task(
                            self._async_seed_silent_devices(completed_table)
                        )
                return
            if response_type == 0x82:
                cmd_id = (frame[1] & 0xFF) | ((frame[2] & 0xFF) << 8)
                rejected_table = self._pending_metadata_cmdids.pop(cmd_id, None)
                self._pending_metadata_entries.pop(cmd_id, None)
                if rejected_table is not None:
                    error_code = frame[4] & 0xFF if len(frame) >= 5 else -1
                    rejection_result = (
                        f"rejected:0x{error_code:02x}" if error_code >= 0 else "rejected"
                    )
                    if error_code == 0x0F:
                        retry_count = self._metadata_retry_counts.get(rejected_table, 0) + 1
                        self._metadata_retry_counts[rejected_table] = retry_count
                        self._resolve_bootstrap_waiter("metadata", rejected_table, rejection_result)
                        if rejected_table not in self._metadata_retry_pending:
                            self._metadata_retry_pending.add(rejected_table)
                            self._cmd_correlation_stats["metadata_retry_scheduled"] += 1
                            _LOGGER.warning(
                                "Metadata rejected by gateway for table_id=%d (errorCode=0x0f)"
                                " — scheduling retry #%d in 10s",
                                rejected_table,
                                retry_count,
                            )
                            self.hass.async_create_task(
                                self._retry_metadata_after_rejection(rejected_table)
                            )
                        else:
                            _LOGGER.debug(
                                "Metadata retry already pending for table_id=%d — skipping duplicate",
                                rejected_table,
                            )
                    else:
                        self._resolve_bootstrap_waiter("metadata", rejected_table, rejection_result)
                        _LOGGER.warning(
                            "Metadata request failed for table_id=%d (errorCode=0x%02x)",
                            rejected_table,
                            error_code if error_code >= 0 else 0,
                        )
                else:
                    # Check if this is a GetDevices rejection instead of metadata.
                    gd_table = self._pending_get_devices_cmdids.pop(cmd_id, None)
                    if gd_table is not None:
                        self._cmd_correlation_stats["get_devices_rejected"] += 1
                        self._get_devices_loaded_tables.discard(gd_table)
                        error_code = frame[4] & 0xFF if len(frame) >= 5 else -1
                        rejection_result = (
                            f"rejected:0x{error_code:02x}" if error_code >= 0 else "rejected"
                        )
                        reject_count = self._get_devices_reject_counts.get(gd_table, 0) + 1
                        self._get_devices_reject_counts[gd_table] = reject_count
                        self._resolve_bootstrap_waiter("get_devices", gd_table, rejection_result)
                        _LOGGER.warning(
                            "GetDevices rejected by gateway for table_id=%d "
                            "(cmdId=%d errorCode=0x%02x, reject #%d) — bootstrap will retry",
                            gd_table, cmd_id,
                            error_code if error_code >= 0 else 0,
                            reject_count,
                        )
                    else:
                        self._cmd_correlation_stats["command_error_unknown"] += 1
                        count = self._unknown_command_counts.get(cmd_id, 0) + 1
                        self._unknown_command_counts[cmd_id] = count
                        if count <= 3 or count in (10, 50, 100) or count % 500 == 0:
                            _LOGGER.debug(
                                "Command error response for unknown cmdId=%d (count=%d)",
                                cmd_id,
                                count,
                            )
                return
            # SuccessMulti (0x01): contains actual device/metadata entries.
            # GetDevices and GetDevicesMetadata both use event_type=0x02 with
            # response_type=0x01; the ONLY distinguisher is the cmdId in bytes 1-2.
            # Without this gate, GetDevices device-row frames (payloadSize=10) are
            # incorrectly passed to parse_metadata_response and silently skipped.
            if response_type == 0x01 and len(frame) >= 3:
                cmd_id = (frame[1] & 0xFF) | ((frame[2] & 0xFF) << 8)
                if cmd_id not in self._pending_metadata_cmdids:
                    if cmd_id in self._pending_get_devices_cmdids:
                        self._cmd_correlation_stats[
                            "metadata_success_multi_discarded_get_devices"
                        ] += 1
                        _LOGGER.debug(
                            "GetDevices response frame (cmdId=%d) — discarding "
                            "(not a metadata request)", cmd_id
                        )
                    else:
                        self._cmd_correlation_stats[
                            "metadata_success_multi_discarded_unknown"
                        ] += 1
                        count = self._unknown_command_counts.get(cmd_id, 0) + 1
                        self._unknown_command_counts[cmd_id] = count
                        if count <= 3 or count in (10, 50, 100) or count % 500 == 0:
                            _LOGGER.debug(
                                "Command response frame for unknown cmdId=%d — discarding (count=%d)",
                                cmd_id,
                                count,
                            )
                    return
                self._cmd_correlation_stats["metadata_success_multi_accepted"] += 1
                staged = self._pending_metadata_entries.setdefault(cmd_id, {})
                added = 0
                for meta in parse_metadata_response(frame):
                    key = _device_key(meta.table_id, meta.device_id)
                    if key not in staged:
                        added += 1
                    staged[key] = meta
                if added:
                    self._cmd_correlation_stats["metadata_entries_staged"] += added
                return

        event = parse_event(frame)
        _LOGGER.debug(
            "Event 0x%02X (%d bytes): %s",
            event_type,
            len(frame),
            type(event).__name__ if not isinstance(event, (bytes, bytearray, type(None))) else "raw",
        )

        # ── Update accumulated state ──────────────────────────────────
        if isinstance(event, GatewayInformation):
            _LOGGER.debug(
                "GatewayInfo: table_id=%d, devices=%d, "
                "table_crc=0x%08x, metadata_crc=0x%08x",
                event.table_id,
                event.device_count,
                event.device_table_crc,
                event.device_metadata_table_crc,
            )

            # CRC-gated metadata logic (mirrors official app DeviceMetadataTracker):
            # If the gateway reports the same DeviceMetadataTableCrc we last loaded,
            # the metadata in _metadata_raw is still valid — restore tracking state
            # and skip the BLE request entirely.
            # If the CRC has changed, invalidate cached metadata for this table so
            # a fresh request is triggered (e.g. after a gateway firmware update).
            crc = event.device_metadata_table_crc
            if crc != 0 and crc == self._last_metadata_crc:
                self._metadata_loaded_tables.add(event.table_id)
                _LOGGER.debug(
                    "Metadata CRC unchanged (0x%08x), skipping re-request for table %d",
                    crc,
                    event.table_id,
                )
            elif (
                self._last_metadata_crc is not None
                and crc != self._last_metadata_crc
                and event.table_id in self._metadata_loaded_tables
            ):
                _LOGGER.info(
                    "Metadata CRC changed (0x%08x → 0x%08x), invalidating table %d",
                    self._last_metadata_crc,
                    crc,
                    event.table_id,
                )
                self._last_metadata_crc = None
                prefix = f"{event.table_id:02x}:"
                for k in list(self._metadata_raw):
                    if k.startswith(prefix):
                        del self._metadata_raw[k]
                        self.device_names.pop(k, None)
                self._metadata_requested_tables.discard(event.table_id)
                self._metadata_loaded_tables.discard(event.table_id)
                self._metadata_rejected_tables.discard(event.table_id)

            self.gateway_info = event

            self._ensure_startup_bootstrap(event.table_id)

        elif isinstance(event, RvStatus):
            self.rv_status = event
            _LOGGER.debug(
                "RvStatus: voltage=%s V, temp=%s °F",
                f"{event.voltage:.2f}" if event.voltage is not None else "N/A",
                f"{event.temperature:.1f}" if event.temperature is not None else "N/A",
            )

        elif isinstance(event, RelayStatus):
            key = _device_key(event.table_id, event.device_id)
            self.relays[key] = event
            self._ensure_metadata_for_table(event.table_id)
            # Fire HA event for DTC faults (only on change, gas appliances only)
            # Android behaviour: only publish DTC for devices with "gas" in name
            prev_dtc = self._last_dtc_codes.get(key, 0)
            self._last_dtc_codes[key] = event.dtc_code
            if event.dtc_code != prev_dtc and event.dtc_code and dtc_is_fault(event.dtc_code):
                device_name = self.device_name(event.table_id, event.device_id)
                dtc_name = dtc_get_name(event.dtc_code)
                is_gas = "gas" in device_name.lower()
                if is_gas:
                    _LOGGER.warning(
                        "DTC fault on %s: code=%d (%s)",
                        device_name, event.dtc_code, dtc_name,
                    )
                    self.hass.bus.async_fire(
                        "onecontrol_dtc_fault",
                        {
                            "device_key": key,
                            "device_name": device_name,
                            "dtc_code": event.dtc_code,
                            "dtc_name": dtc_name,
                            "table_id": event.table_id,
                            "device_id": event.device_id,
                        },
                    )
                else:
                    _LOGGER.debug(
                        "DTC on %s (non-gas, ignored): code=%d (%s)",
                        device_name, event.dtc_code, dtc_name,
                    )

        elif isinstance(event, DimmableLight):
            key = _device_key(event.table_id, event.device_id)
            self.dimmable_lights[key] = event
            if event.brightness > 0:
                self._last_known_dimmable_brightness[key] = event.brightness
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, RgbLight):
            key = _device_key(event.table_id, event.device_id)
            self.rgb_lights[key] = event
            # Only persist non-zero color — mirrors Android lastKnownRgbColor update guard.
            if event.is_on:
                self._last_known_rgb_color[key] = (event.red, event.green, event.blue)
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, CoverStatus):
            key = _device_key(event.table_id, event.device_id)
            self.covers[key] = event
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, list):
            # Multi-item events: HvacZone list, TankLevel list, DeviceMetadata list
            for item in event:
                if isinstance(item, HvacZone):
                    self._handle_hvac_zone(item)
                elif isinstance(item, TankLevel):
                    key = _device_key(item.table_id, item.device_id)
                    self.tanks[key] = item
                    self._ensure_metadata_for_table(item.table_id)
                elif isinstance(item, DeviceMetadata):
                    self._process_metadata(item)

        elif isinstance(event, TankLevel):
            key = _device_key(event.table_id, event.device_id)
            self.tanks[key] = event
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, HvacZone):
            self._handle_hvac_zone(event)

        elif isinstance(event, DeviceOnline):
            key = _device_key(event.table_id, event.device_id)
            self.device_online[key] = event
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, SystemLockout):
            self.system_lockout_level = event.lockout_level
            _LOGGER.debug(
                "SystemLockout: level=%d table=%d devices=%d",
                event.lockout_level, event.table_id, event.device_count,
            )

        elif isinstance(event, DeviceLock):
            key = _device_key(event.table_id, event.device_id)
            self.device_locks[key] = event
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, GeneratorStatus):
            key = _device_key(event.table_id, event.device_id)
            self.generators[key] = event
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, HourMeter):
            key = _device_key(event.table_id, event.device_id)
            self.hour_meters[key] = event
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, LevelerStatus):
            key = _device_key(event.table_id, event.device_id)
            self.levelers[key] = event
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, TankAlert):
            key = _device_key(event.table_id, event.device_id)
            self.tank_alerts[key] = event
            self._ensure_metadata_for_table(event.table_id)

        elif isinstance(event, RealTimeClock):
            self.rtc = event

        # ── Notify entity callbacks ───────────────────────────────────
        for cb in self._event_callbacks:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in event callback")

        # ── Trigger HA state update ───────────────────────────────────
        self.async_set_updated_data(self._build_data())

    def _process_metadata(self, meta: DeviceMetadata) -> None:
        """Store metadata and resolve friendly name."""
        key = _device_key(meta.table_id, meta.device_id)
        self._metadata_raw[key] = meta
        name = get_friendly_name(meta.function_name, meta.function_instance)
        self.device_names[key] = name
        self._metadata_loaded_tables.add(meta.table_id)
        # Record the CRC for the gateway's primary table so reconnects can skip
        # re-requesting metadata when the CRC hasn't changed.
        if (
            self.gateway_info is not None
            and meta.table_id == self.gateway_info.table_id
            and self.gateway_info.device_metadata_table_crc != 0
        ):
            self._last_metadata_crc = self.gateway_info.device_metadata_table_crc
        _LOGGER.info(
            "Metadata: %s → func=%d inst=%d → %s",
            key.upper(), meta.function_name, meta.function_instance, name,
        )

    async def _async_seed_silent_devices(self, table_id: int) -> None:
        """Seed switch entities for relay-type devices that never emit BLE events.

        After waiting _METADATA_SEED_DELAY_S for the initial BLE event burst to
        settle, any metadata entry whose function code is in
        _RELAY_SEED_FUNCTION_CODES and that still has no discovered relay state
        receives a RelayStatus(is_on=False) stub so switch.py can create an entity.

        All other device types (lights, covers, levelers, …) are silently skipped.
        They will appear once the gateway emits a real event for them; until then,
        no entity is created.  This prevents misclassified entities for devices
        whose event type (relay vs dimmable vs RGB) we cannot determine from the
        function code alone.
        """
        await asyncio.sleep(_METADATA_SEED_DELAY_S)

        for key, meta in list(self._metadata_raw.items()):
            if meta.table_id != table_id:
                continue
            if meta.function_name not in _RELAY_SEED_FUNCTION_CODES:
                continue
            # Already discovered via a live relay event — skip.
            if key in self.relays:
                continue
            device_name = self.device_names.get(key, key)
            stub = RelayStatus(
                table_id=meta.table_id,
                device_id=meta.device_id,
                is_on=False,
            )
            self.relays[key] = stub
            _LOGGER.info(
                "Seeding silent relay entity for %s (func=%d) from metadata",
                device_name,
                meta.function_name,
            )
            for cb in self._event_callbacks:
                try:
                    cb(stub)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Error in event callback during device seed")

    def _build_data(self) -> dict[str, Any]:
        """Build the coordinator data dict consumed by entities."""
        data: dict[str, Any] = {
            "connected": self._connected,
            "authenticated": self._authenticated,
        }
        if self.rv_status:
            data["voltage"] = self.rv_status.voltage
            data["temperature"] = self.rv_status.temperature
        if self.gateway_info:
            data["table_id"] = self.gateway_info.table_id
            data["device_count"] = self.gateway_info.device_count
        return data

    # ------------------------------------------------------------------
    # Disconnect callback + automatic reconnection
    # ------------------------------------------------------------------

    @callback
    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle unexpected BLE disconnect — schedule reconnect with backoff."""
        _LOGGER.debug("OneControl %s disconnected (instance=%s)", self.address, self._instance_tag)
        self._stop_heartbeat()
        self._connected = False
        self._authenticated = False
        self._decoder.reset()
        self._metadata_requested_tables.clear()
        self._metadata_loaded_tables.clear()
        self._metadata_rejected_tables.clear()
        self._metadata_retry_counts.clear()
        self._metadata_retry_pending.clear()
        self._pending_metadata_cmdids.clear()
        self._pending_metadata_entries.clear()
        self._pending_get_devices_cmdids.clear()
        self._get_devices_loaded_tables.clear()
        self._get_devices_reject_counts.clear()
        self._cancel_startup_bootstrap()
        self._unknown_command_counts.clear()
        self._initial_get_devices_sent = False
        self._has_can_write = False
        self._is_can_ble = False
        self._can_device_types = {}
        # Tear down any open REMOTE_CONTROL session — will be re-opened on next connect.
        self._can_read_subscribed = False
        self._can_local_host_claimed = False
        self._can_local_host_identity_last_tx = 0.0
        self._rc_session_open = False
        self._rc_session_target = None
        if self._rc_session_seed_future and not self._rc_session_seed_future.done():
            self._rc_session_seed_future.cancel()
        if self._rc_session_key_future and not self._rc_session_key_future.done():
            self._rc_session_key_future.cancel()
        if self._rc_heartbeat_task and not self._rc_heartbeat_task.done():
            self._rc_heartbeat_task.cancel()
        self._rc_heartbeat_task = None
        if self._can_keepalive_task and not self._can_keepalive_task.done():
            _LOGGER.debug("Cancelling CAN keepalive task on disconnect")
            self._can_keepalive_task.cancel()
        self._can_keepalive_task = None
        self._pin_dbus_succeeded = False
        self._push_button_dbus_ok = False
        # PIN agent context is cleaned up inside _finish_connect; if somehow
        # still set here, schedule async cleanup (callback is synchronous).
        if self._pin_agent_ctx:
            ctx = self._pin_agent_ctx
            self._pin_agent_ctx = None
            self.hass.async_create_task(ctx.cleanup())

        self.async_set_updated_data(self._build_data())

        # Schedule automatic reconnection with exponential backoff
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnect attempt with exponential backoff.

        Cancels any in-progress reconnect timer and restarts it.  This debounces
        rapid _on_disconnect calls that fire during BRC internal retries and
        prevents multiple concurrent reconnect coroutines from racing each other
        into BlueZ's "InProgress" error state.
        """
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        self._reconnect_generation += 1
        generation = self._reconnect_generation
        
        # For CAN BLE gateways, add extra delay on first reconnect to let BlueZ settle
        # after the disconnect. CAN BLE disconnects can leave BlueZ in "InProgress" state
        # if we reconnect too quickly.
        if self._can_ble_confirmed and self._consecutive_failures == 1:
            delay = 2.0  # Give BlueZ 2s to clean up after CAN BLE disconnect
            _LOGGER.debug(
                "CAN BLE disconnect detected — delaying reconnect by %.1fs to allow BlueZ cleanup",
                delay
            )
        else:
            delay = min(
                RECONNECT_BACKOFF_BASE * (2 ** self._consecutive_failures),
                RECONNECT_BACKOFF_CAP,
            )
        
        self._consecutive_failures += 1
        _LOGGER.debug(
            "Scheduling reconnect in %.0fs (attempt %d, gen=%d, instance=%s)",
            delay, self._consecutive_failures, generation, self._instance_tag,
        )
        self._reconnect_task = self.hass.async_create_task(
            self._reconnect_with_delay(delay, generation)
        )

    async def _reconnect_with_delay(self, delay: float, generation: int) -> None:
        """Wait then attempt reconnection."""
        try:
            await asyncio.sleep(delay)
            if generation != self._reconnect_generation:
                _LOGGER.debug(
                    "Skipping stale reconnect task (gen=%d current=%d instance=%s)",
                    generation, self._reconnect_generation, self._instance_tag,
                )
                return
            if self._connected:
                return  # Already reconnected by another path

            # For PIN gateways, remove stale bond after 3 consecutive failures
            # (suggests the bond keys are out of sync with the gateway)
            if (
                self.is_pin_gateway
                and self._consecutive_failures >= 3
                and self._consecutive_failures % 3 == 0
            ):
                _LOGGER.info(
                    "PIN gateway: %d failures — removing possibly stale bond",
                    self._consecutive_failures,
                )
                await self._remove_stale_bond()

            _LOGGER.debug(
                "Attempting reconnection to %s (gen=%d, instance=%s)...",
                self.address, generation, self._instance_tag,
            )
            await self.async_connect()
            # Reset backoff only when the connection is actually usable
            # (authenticated, or bonded for PIN gateways).  If async_connect
            # returned without raising but we're not authenticated, we consider
            # it a partial failure and keep the backoff counter intact.
            if self._authenticated or self._pin_dbus_succeeded:
                self._consecutive_failures = 0
                _LOGGER.debug("Reconnected to %s (instance=%s)", self.address, self._instance_tag)
            else:
                _LOGGER.warning(
                    "async_connect returned but %s is not authenticated — "
                    "keeping backoff counter (%d)",
                    self.address, self._consecutive_failures,
                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.warning("Reconnect failed: %s", exc)
            # Schedule next attempt with increased backoff
            self._schedule_reconnect()

    def _cancel_reconnect(self) -> None:
        """Cancel any pending reconnect task."""
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None

    # ------------------------------------------------------------------
    # DataUpdateCoordinator._async_update_data (fallback / heartbeat)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Called by the coordinator on its polling interval (if set)."""
        # IDS-CAN BLE gateways manage reconnection via _schedule_reconnect;
        # polling-based reconnects here would race with the backoff scheduler and cause
        # spurious entity refreshes every 5s.  Skip the connect attempt for these devices.
        if not self._can_ble_confirmed and not self._connected:
            try:
                await self.async_connect()
            except BleakError as exc:
                _LOGGER.warning("Reconnect failed: %s", exc)
        return self._build_data()
