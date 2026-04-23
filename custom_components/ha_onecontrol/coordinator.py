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
import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .ble_agent import (
    PinAgentContext,
    async_get_local_adapter_macs,
    async_is_locally_bonded,
    is_pin_pairing_supported,
    pair_push_button,
    prepare_pin_agent,
    remove_bond,
)
from .const import (
    AUTH_SERVICE_UUID,
    BLE_MTU_SIZE,
    CAN_WRITE_CHAR_UUID,
    CONNECTION_TYPE_ETHERNET,
    CONF_CONNECTION_TYPE,
    CONF_BLUETOOTH_PIN,
    CONF_BONDED_SOURCE,
    CONF_ETH_HOST,
    CONF_ETH_PORT,
    CONF_GATEWAY_PIN,
    CONF_NAMING_MANIFEST_JSON,
    CONF_NAMING_MANIFEST_PATH,
    CONF_NAMING_SNAPSHOT_JSON,
    CONF_NAMING_SNAPSHOT_PATH,
    CONF_PAIRING_METHOD,
    DATA_READ_CHAR_UUID,
    DATA_SERVICE_UUID,
    DATA_WRITE_CHAR_UUID,
    DEFAULT_GATEWAY_PIN,
    DOMAIN,
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
from .name_catalog import ExternalNameCatalog, load_external_name_catalog
from .protocol.cobs import CobsByteDecoder, cobs_encode
from .protocol.commands import CommandBuilder
from .protocol.events import (
    CoverStatus,
    DeviceLock,
    DeviceMetadata,
    DeviceIdentity,
    DeviceOnline,
    DimmableLight,
    GatewayInformation,
    GeneratorStatus,
    HourMeter,
    HvacZone,
    RealTimeClock,
    RelayStatus,
    RgbLight,
    RvStatus,
    SystemLockout,
    TankLevel,
    parse_event,
)
from .protocol.dtc_codes import get_name as dtc_get_name, is_fault as dtc_is_fault
from .protocol.tea import calculate_step1_key, calculate_step2_key
from .runtime import IdsCanRuntime

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
_MAX_PENDING_METADATA_CMDIDS = 128
_MAX_UNKNOWN_COMMAND_IDS = 512
_CMDID_STALE_TIMEOUT_S = 30.0
_ETHERNET_HEARTBEAT_INTERVAL_S = 2.0
_ETHERNET_TRANSPORT_KEEPALIVE_INTERVAL_S = 3.0


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
        self._connection_type: str = entry.data.get(CONF_CONNECTION_TYPE, "ble")
        self._eth_host: str = entry.data.get(CONF_ETH_HOST, "")
        self._eth_port: int = int(entry.data.get(CONF_ETH_PORT, 0) or 0)

        # ── PIN-based pairing (legacy gateways) ──────────────────────
        self._pairing_method: str = entry.data.get(CONF_PAIRING_METHOD, "push_button")
        self._instance_tag: str = f"{id(self):x}"[-6:]
        # Android uses gateway_pin for both BLE bonding AND protocol auth.
        # bluetooth_pin is an optional override if the BLE PIN differs.
        self._bluetooth_pin: str = entry.data.get(
            CONF_BLUETOOTH_PIN, ""
        ) or self.gateway_pin
        self._pin_agent_ctx: PinAgentContext | None = None  # active D-Bus agent context
        self._pin_dbus_succeeded: bool = False  # bonding completed this session
        self._pin_already_bonded: bool = False  # BlueZ "already bonded" seen (sticky — not reset on disconnect)
        self._push_button_dbus_ok: bool = False
        # Source of the adapter/proxy used for the most recent HA-routed connect
        # attempt.  Persisted to config entry options after successful step-1 auth
        # so subsequent connects are pinned to the same adapter (bond affinity).
        self._current_connect_source: str | None = None

        self._client: BleakClient | None = None
        self._eth_reader: asyncio.StreamReader | None = None
        self._eth_writer: asyncio.StreamWriter | None = None
        self._ethernet_reader_task: asyncio.Task | None = None
        self._last_ethernet_tx_time: float = 0.0
        self._ethernet_transport_keepalives_sent: int = 0
        self._disconnect_count: int = 0
        self._last_disconnect_reason: str | None = None
        self._stop_listener = None
        self._naming_manifest_path: str = entry.options.get(CONF_NAMING_MANIFEST_PATH, "")
        self._naming_snapshot_path: str = entry.options.get(CONF_NAMING_SNAPSHOT_PATH, "")
        self._naming_manifest_json: str = entry.options.get(CONF_NAMING_MANIFEST_JSON, "")
        self._naming_snapshot_json: str = entry.options.get(CONF_NAMING_SNAPSHOT_JSON, "")
        self._external_name_catalog: ExternalNameCatalog = ExternalNameCatalog()
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
        self._pending_metadata_sent_at: dict[int, float] = {}  # cmdId → monotonic timestamp
        self._pending_metadata_entries: dict[int, dict[str, DeviceMetadata]] = {}
        self._pending_get_devices_cmdids: dict[int, int] = {}  # cmdId → table_id
        self._pending_get_devices_sent_at: dict[int, float] = {}  # cmdId → monotonic timestamp
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
            "metadata_parse_errors": 0,
            "metadata_commit_success": 0,
            "metadata_commit_crc_mismatch": 0,
            "metadata_commit_count_mismatch": 0,
            "metadata_waiting_get_devices": 0,
            "metadata_retry_scheduled": 0,
            "command_error_unknown": 0,
            "get_devices_rejected": 0,
            "get_devices_completed": 0,
            "get_devices_completed_fallback": 0,
            "get_devices_identity_rows": 0,
            "get_devices_identity_rows_fallback": 0,
            "get_devices_identity_parse_empty": 0,
            "ids_command_candidates_seen": 0,
            "ids_command_candidates_unmatched": 0,
            "external_names_applied": 0,
            "pending_get_devices_peak": 0,
            "frame_parse_errors": 0,
            "pending_cmdid_pruned": 0,
            "unknown_cmdids_pruned": 0,
        }
        self._frame_family_stats: dict[str, int] = {
            "myrvlink_state": 0,
            "myrvlink_command": 0,
            "ids_can_like": 0,
            "unknown": 0,
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
        self.rtc: RealTimeClock | None = None
        self.system_lockout_level: int | None = None

        # Metadata: friendly names per device key
        self.device_names: dict[str, str] = {}
        self._metadata_raw: dict[str, DeviceMetadata] = {}
        self._device_identities: dict[str, DeviceIdentity] = {}

        self._load_external_name_catalog()

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

        # Protocol runtimes: keep coordinator as HA-facing facade while
        # transport/protocol orchestration is split by backend.
        self._ids_runtime = IdsCanRuntime(self)

        # Cancel non-critical reconnect/heartbeat tasks as HA stops.
        if hasattr(self.hass, "bus") and hasattr(self.hass.bus, "async_listen_once"):
            self._stop_listener = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._on_hass_stop
            )

    @property
    def instance_tag(self) -> str:
        return self._instance_tag

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        if self.is_ethernet_gateway:
            return self._connected and self._eth_writer is not None
        return self._connected and self._client is not None

    @property
    def is_ethernet_gateway(self) -> bool:
        """Return True if this entry uses the Ethernet bridge transport."""
        return self._connection_type == CONNECTION_TYPE_ETHERNET

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def data_healthy(self) -> bool:
        """Return True if we've received data recently (within 15s)."""
        if not self._connected or self._last_event_time == 0.0:
            return False
        return (time.monotonic() - self._last_event_time) < 15.0

    @property
    def last_event_age(self) -> float | None:
        """Seconds since last event, or None if no events received."""
        if self._last_event_time == 0.0:
            return None
        return time.monotonic() - self._last_event_time

    def device_name(self, table_id: int, device_id: int) -> str:
        """Return friendly name or fallback like 'Device 0B:05'."""
        key = _device_key(table_id, device_id)
        return self.device_names.get(key, f"Device {key.upper()}")

    def _load_external_name_catalog(self) -> None:
        """Load optional manifest/snapshot naming catalogs from config entry options."""
        manifest_path = self._naming_manifest_path.strip() or None
        snapshot_path = self._naming_snapshot_path.strip() or None
        manifest_json = self._naming_manifest_json.strip() or None
        snapshot_json = self._naming_snapshot_json.strip() or None
        if not manifest_path and not snapshot_path and not manifest_json and not snapshot_json:
            self._external_name_catalog = ExternalNameCatalog()
            return

        try:
            self._external_name_catalog = load_external_name_catalog(
                manifest_path,
                snapshot_path,
                manifest_json,
                snapshot_json,
            )
            _LOGGER.info(
                "Loaded external naming catalog: entries=%d manifest_path=%s snapshot_path=%s manifest_json=%s snapshot_json=%s",
                self._external_name_catalog.entries,
                manifest_path or "",
                snapshot_path or "",
                "yes" if manifest_json else "no",
                "yes" if snapshot_json else "no",
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Failed loading external naming catalog: %s", exc)
            self._external_name_catalog = ExternalNameCatalog()

    def _apply_external_name(self, key: str, identity: DeviceIdentity) -> None:
        """Apply external name when identity matches manifest/snapshot catalog."""
        if self._external_name_catalog.entries == 0:
            return

        resolved_name = self._external_name_catalog.lookup(
            identity.device_type,
            identity.device_instance,
            identity.product_id,
            identity.product_mac,
        )
        if not resolved_name:
            return

        if key not in self.device_names:
            self.device_names[key] = resolved_name
            self._cmd_correlation_stats["external_names_applied"] += 1

    def register_event_callback(self, cb: Callable[[Any], None]) -> Callable[[], None]:
        """Register a callback for parsed events. Returns unsubscribe callable."""
        self._event_callbacks.append(cb)

        def _unsub() -> None:
            if cb in self._event_callbacks:
                self._event_callbacks.remove(cb)

        return _unsub

    def _dispatch_event_update(self, event: Any) -> None:
        """Protocol-neutral event fan-out + coordinator state publication."""
        for cb in self._event_callbacks:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in event callback")

        self.async_set_updated_data(self._build_data())

    # ------------------------------------------------------------------
    # Command sending (COBS-encoded writes to DATA_WRITE)
    # ------------------------------------------------------------------

    async def async_send_command(self, raw_command: bytes) -> None:
        """COBS-encode and write a command to the gateway."""
        if self.is_ethernet_gateway:
            if not self._eth_writer or not self._connected:
                raise ConnectionError("Not connected to Ethernet bridge")
            encoded = cobs_encode(raw_command)
            cmd_id = int.from_bytes(raw_command[0:2], "little") if len(raw_command) >= 2 else -1
            cmd_type = raw_command[2] if len(raw_command) >= 3 else -1
            cmd_name = {
                0x01: "GetDevices",
                0x02: "GetDevicesMetadata",
                0x40: "ActionSwitch",
                0x41: "ActionHBridge",
                0x42: "ActionGenerator",
                0x43: "ActionDimmable",
                0x44: "ActionRgb",
                0x45: "ActionHvac",
            }.get(cmd_type, "Unknown")
            _LOGGER.warning(
                "PACKET TX ETH cmd_id=0x%04X type=0x%02X(%s) raw_len=%d raw=%s",
                cmd_id & 0xFFFF,
                cmd_type & 0xFF,
                cmd_name,
                len(raw_command),
                raw_command.hex(),
            )
            self._eth_writer.write(encoded)
            await self._eth_writer.drain()
            self._last_ethernet_tx_time = time.monotonic()
            return

        if not self._client or not self._connected:
            raise BleakError("Not connected to gateway")
        encoded = cobs_encode(raw_command)
        _LOGGER.debug("TX command (%d bytes raw): %s", len(raw_command), raw_command.hex())
        await self._client.write_gatt_char(DATA_WRITE_CHAR_UUID, encoded, response=False)

    async def async_switch(
        self, table_id: int, device_id: int, state: bool
    ) -> None:
        """Send a switch on/off command."""
        if self.is_ethernet_gateway:
            used_ids_native = await self._ids_runtime.send_relay_toggle_command(
                table_id,
                device_id,
                state,
            )
            if used_ids_native:
                _LOGGER.warning(
                    "PACKET TX IDS relay-toggle accepted table=0x%02X device=0x%02X state=%s (IDS-only mode)",
                    table_id & 0xFF,
                    device_id & 0xFF,
                    state,
                )
                return
            _LOGGER.warning(
                "PACKET TX IDS relay-toggle skipped table=0x%02X device=0x%02X state=%s (IDS-only mode; legacy fallback disabled)",
                table_id & 0xFF,
                device_id & 0xFF,
                state,
            )
            return

        cmd = self._cmd.build_action_switch(table_id, state, [device_id])
        await self.async_send_command(cmd)

    async def async_set_dimmable(
        self, table_id: int, device_id: int, brightness: int
    ) -> None:
        """Send a dimmable light brightness command."""
        if self.is_ethernet_gateway:
            used_ids_native = await self._ids_runtime.send_light_brightness_command(
                table_id,
                device_id,
                brightness,
            )
            if used_ids_native:
                _LOGGER.warning(
                    "PACKET TX IDS light-set accepted table=0x%02X device=0x%02X brightness=%d (IDS-only mode)",
                    table_id & 0xFF,
                    device_id & 0xFF,
                    brightness,
                )
                return
            _LOGGER.warning(
                "PACKET TX IDS light-set skipped table=0x%02X device=0x%02X brightness=%d (IDS-only mode; legacy fallback disabled)",
                table_id & 0xFF,
                device_id & 0xFF,
                brightness,
            )
            return

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
        if self.is_ethernet_gateway:
            used_ids_native = await self._ids_runtime.send_light_effect_command(
                table_id,
                device_id,
                mode,
                brightness,
                duration,
                cycle_time1,
                cycle_time2,
            )
            if used_ids_native:
                _LOGGER.warning(
                    "PACKET TX IDS light-effect accepted table=0x%02X device=0x%02X mode=0x%02X brightness=%d duration=%d (IDS-only mode)",
                    table_id & 0xFF,
                    device_id & 0xFF,
                    mode & 0xFF,
                    brightness,
                    duration,
                )
                return
            _LOGGER.warning(
                "PACKET TX IDS light-effect skipped table=0x%02X device=0x%02X mode=0x%02X brightness=%d (IDS-only mode; legacy fallback disabled)",
                table_id & 0xFF,
                device_id & 0xFF,
                mode & 0xFF,
                brightness,
            )
            return

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
        command_sent = False
        if self.is_ethernet_gateway:
            used_ids_native = await self._ids_runtime.send_hvac_command(
                table_id=table_id,
                device_id=device_id,
                heat_mode=heat_mode,
                heat_source=heat_source,
                fan_mode=fan_mode,
                low_trip_f=low_trip_f,
                high_trip_f=high_trip_f,
            )
            if used_ids_native:
                command_sent = True
                _LOGGER.warning(
                    "PACKET TX IDS hvac-set accepted table=0x%02X device=0x%02X mode=%d source=%d fan=%d low=%d high=%d",
                    table_id & 0xFF,
                    device_id & 0xFF,
                    heat_mode & 0x07,
                    heat_source & 0x03,
                    fan_mode & 0x03,
                    low_trip_f,
                    high_trip_f,
                )
            else:
                _LOGGER.warning(
                    "PACKET TX IDS hvac-set skipped table=0x%02X device=0x%02X reason=ids-path-not-ready",
                    table_id & 0xFF,
                    device_id & 0xFF,
                )
        else:
            cmd = self._cmd.build_action_hvac(
                table_id, device_id, heat_mode, heat_source, fan_mode, low_trip_f, high_trip_f
            )
            await self.async_send_command(cmd)
            command_sent = True

        if not command_sent:
            return

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

        identity = self._device_identities.get(zone_key)
        if identity is not None:
            cap |= getattr(identity, "raw_device_capability", 0) & 0x0F

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
        if self.is_ethernet_gateway:
            sent = await self._ids_runtime.send_hvac_command(
                table_id=pending.table_id,
                device_id=pending.device_id,
                heat_mode=pending.heat_mode,
                heat_source=pending.heat_source,
                fan_mode=pending.fan_mode,
                low_trip_f=pending.low_trip_f,
                high_trip_f=pending.high_trip_f,
            )
            if not sent:
                _LOGGER.warning(
                    "HVAC setpoint retry skipped for %s (ids-path-not-ready)",
                    zone_key,
                )
        else:
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
        if self.is_ethernet_gateway:
            used_ids_native = await self._ids_runtime.send_rgb_command(
                table_id=table_id,
                device_id=device_id,
                mode=mode,
                red=red,
                green=green,
                blue=blue,
                auto_off=auto_off,
                blink_on_interval=blink_on_interval,
                blink_off_interval=blink_off_interval,
                transition_interval=transition_interval,
            )
            if used_ids_native:
                _LOGGER.warning(
                    "PACKET TX IDS rgb-set accepted table=0x%02X device=0x%02X mode=0x%02X rgb=(%d,%d,%d)",
                    table_id & 0xFF,
                    device_id & 0xFF,
                    mode & 0xFF,
                    red & 0xFF,
                    green & 0xFF,
                    blue & 0xFF,
                )
            else:
                _LOGGER.warning(
                    "PACKET TX IDS rgb-set skipped table=0x%02X device=0x%02X reason=ids-path-not-ready",
                    table_id & 0xFF,
                    device_id & 0xFF,
                )
            return

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
        now = time.monotonic()
        if now - self._last_lockout_clear < LOCKOUT_CLEAR_THROTTLE:
            _LOGGER.warning("Lockout clear throttled (min %ss)", LOCKOUT_CLEAR_THROTTLE)
            return
        self._last_lockout_clear = now

        if self.is_ethernet_gateway:
            _LOGGER.warning("Lockout clear over Ethernet bridge is not implemented")
            return

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
        if not self._supports_metadata_requests:
            _LOGGER.debug(
                "Skipping metadata refresh for %s: metadata requests disabled on Ethernet/IDS-CAN",
                self.address,
            )
            return
        # Reset per-table state so all tables can be re-requested
        self._metadata_requested_tables.clear()
        self._metadata_loaded_tables.clear()
        self._metadata_rejected_tables.clear()
        self._metadata_retry_counts.clear()
        self._metadata_retry_pending.clear()
        self._pending_metadata_cmdids.clear()
        self._pending_metadata_sent_at.clear()
        self._pending_metadata_entries.clear()
        self._pending_get_devices_cmdids.clear()
        self._pending_get_devices_sent_at.clear()

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
            self.generators, self.hour_meters, self.unknown_devices,
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
        if self.is_ethernet_gateway:
            await self._close_ethernet_transport()
            self._decoder.reset()
            return
        if self._client:
            try:
                await self._client.disconnect()
            except BleakError:
                pass
            self._client = None
        self._decoder.reset()

    async def _do_connect(self) -> None:
        """Internal connect routine with retry logic."""
        if self.is_ethernet_gateway:
            await self._do_connect_ethernet()
            return

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
        if self.is_pin_gateway and self._pin_already_bonded:
            _LOGGER.warning(
                "PIN gateway %s: BlueZ bond present but all connection attempts failed "
                "— removing stale bond and retrying with fresh PIN pairing",
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

        # PIN gateways require bonding before any connection can succeed.
        # If bonding hasn't succeeded yet, skip direct adapter fallback —
        # unbonded connects will fail and each attempt leaves BlueZ with
        # a pending InProgress state that blocks all subsequent attempts.
        if self.is_pin_gateway and not self._pin_dbus_succeeded:
            _LOGGER.warning(
                "PIN gateway %s: D-Bus bonding did not succeed — skipping "
                "direct adapter fallback.  Ensure the gateway PIN is correct "
                "and the device is powered on and in pairing mode.",
                self.address,
            )
            raise last_exc

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
                for name in os.listdir("/sys/class/bluetooth")
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

    async def _do_connect_ethernet(self) -> None:
        """Connect to an IDS CAN-to-Ethernet bridge with retries."""
        await self._ids_runtime.connect()

    async def _try_connect_ethernet(self, attempt: int) -> None:
        """Open TCP connection to Ethernet bridge and start reader task."""
        await self._ids_runtime._try_connect(attempt)

    async def _ethernet_read_loop(self) -> None:
        """Read Ethernet bytes and decode COBS frames into protocol events."""
        await self._ids_runtime.read_loop()

    async def _close_ethernet_transport(self) -> None:
        """Close active Ethernet socket and reader task."""
        await self._ids_runtime.close_transport()

    async def _send_ethernet_transport_keepalive(self) -> None:
        """Send a transport-level frame delimiter to prevent idle TCP closes."""
        runtime = getattr(self, "_ids_runtime", None)
        if runtime is not None:
            await runtime.send_transport_keepalive(_ETHERNET_TRANSPORT_KEEPALIVE_INTERVAL_S)
            return

        # Compatibility fallback for tests that invoke this method on a lightweight
        # stand-in object via OneControlCoordinator._send_ethernet_transport_keepalive(...).
        if not self.is_ethernet_gateway or not self._connected or self._eth_writer is None:
            return
        if (time.monotonic() - self._last_ethernet_tx_time) < _ETHERNET_TRANSPORT_KEEPALIVE_INTERVAL_S:
            return
        self._eth_writer.write(b"\x00")
        await self._eth_writer.drain()
        self._last_ethernet_tx_time = time.monotonic()
        self._ethernet_transport_keepalives_sent += 1
        _LOGGER.debug("TX Ethernet transport keepalive delimiter")

    @property
    def is_pin_gateway(self) -> bool:
        """True if this gateway uses PIN-based (legacy) BLE pairing."""
        return self._pairing_method == "pin"

    @property
    def _supports_metadata_requests(self) -> bool:
        """True when metadata command path is supported for current transport."""
        # Legacy IDS-CAN over Ethernet bridges have not shown reliable support
        # for GetDevicesMetadata in field testing.
        return not self.is_ethernet_gateway

    def _classify_frame_family(self, frame: bytes) -> str:
        """Best-effort classifier for mixed protocol captures.

        This is heuristic telemetry for diagnostics, not a strict decoder.
        """
        if not frame:
            return "unknown"

        event_type = frame[0] & 0xFF

        # Known MyRVLink state/event types currently parsed by this integration.
        if event_type in {
            0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09,
            0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x1A, 0x1B, 0x20,
        }:
            return "myrvlink_state"

        # MyRVLink command response envelope.
        if event_type == 0x02 and len(frame) >= 4 and (frame[3] & 0xFF) in {0x01, 0x02, 0x81, 0x82}:
            return "myrvlink_command"

        # IDS-CAN message-type-like values from decompiled references.
        if event_type in {0x00, 0x80, 0x81, 0x82, 0x83, 0x84, 0x9B, 0x9D, 0x9F}:
            return "ids_can_like"

        return "unknown"

    def _record_pending_get_devices_cmd(self, cmd_id: int, table_id: int) -> None:
        """Track an in-flight GetDevices command id with bounded retention."""
        runtime = getattr(self, "_myrvlink_runtime", None)
        if runtime is not None:
            runtime.record_pending_get_devices_cmd(
                cmd_id,
                table_id,
                max_pending=_MAX_PENDING_GET_DEVICES_CMDIDS,
            )
            return

        now = time.monotonic()
        self._pending_get_devices_cmdids[cmd_id] = table_id
        self._pending_get_devices_sent_at[cmd_id] = now
        while len(self._pending_get_devices_cmdids) > _MAX_PENDING_GET_DEVICES_CMDIDS:
            oldest_cmd_id = next(iter(self._pending_get_devices_cmdids))
            self._pending_get_devices_cmdids.pop(oldest_cmd_id, None)
            self._pending_get_devices_sent_at.pop(oldest_cmd_id, None)
            self._cmd_correlation_stats["pending_cmdid_pruned"] += 1
        self._cmd_correlation_stats["pending_get_devices_peak"] = max(
            self._cmd_correlation_stats["pending_get_devices_peak"],
            len(self._pending_get_devices_cmdids),
        )

    def _record_pending_metadata_cmd(self, cmd_id: int, table_id: int) -> None:
        """Track an in-flight metadata command id with bounded retention."""
        runtime = getattr(self, "_myrvlink_runtime", None)
        if runtime is not None:
            runtime.record_pending_metadata_cmd(
                cmd_id,
                table_id,
                max_pending=_MAX_PENDING_METADATA_CMDIDS,
            )
            return

        now = time.monotonic()
        self._pending_metadata_cmdids[cmd_id] = table_id
        self._pending_metadata_sent_at[cmd_id] = now
        while len(self._pending_metadata_cmdids) > _MAX_PENDING_METADATA_CMDIDS:
            oldest_cmd_id = next(iter(self._pending_metadata_cmdids))
            self._pending_metadata_cmdids.pop(oldest_cmd_id, None)
            self._pending_metadata_sent_at.pop(oldest_cmd_id, None)
            self._pending_metadata_entries.pop(oldest_cmd_id, None)
            self._cmd_correlation_stats["pending_cmdid_pruned"] += 1

    def _prune_pending_command_state(self) -> None:
        """Drop stale pending cmdIds so late/missing responses do not accumulate forever."""
        runtime = getattr(self, "_myrvlink_runtime", None)
        if runtime is not None:
            runtime.prune_pending_command_state(_CMDID_STALE_TIMEOUT_S)
            return

        cutoff = time.monotonic() - _CMDID_STALE_TIMEOUT_S

        stale_get_devices = [
            cmd_id
            for cmd_id, sent_at in self._pending_get_devices_sent_at.items()
            if sent_at < cutoff
        ]
        for cmd_id in stale_get_devices:
            self._pending_get_devices_sent_at.pop(cmd_id, None)
            self._pending_get_devices_cmdids.pop(cmd_id, None)
            self._cmd_correlation_stats["pending_cmdid_pruned"] += 1

        stale_metadata = [
            cmd_id
            for cmd_id, sent_at in self._pending_metadata_sent_at.items()
            if sent_at < cutoff
        ]
        for cmd_id in stale_metadata:
            self._pending_metadata_sent_at.pop(cmd_id, None)
            self._pending_metadata_cmdids.pop(cmd_id, None)
            self._pending_metadata_entries.pop(cmd_id, None)
            self._cmd_correlation_stats["pending_cmdid_pruned"] += 1

    def _bump_unknown_cmd_count(self, cmd_id: int) -> int:
        """Increment unknown cmdId counter and bound map size."""
        runtime = getattr(self, "_myrvlink_runtime", None)
        if runtime is not None:
            return runtime.bump_unknown_cmd_count(
                cmd_id,
                max_unknown=_MAX_UNKNOWN_COMMAND_IDS,
            )

        count = self._unknown_command_counts.get(cmd_id, 0) + 1
        self._unknown_command_counts[cmd_id] = count
        while len(self._unknown_command_counts) > _MAX_UNKNOWN_COMMAND_IDS:
            self._unknown_command_counts.pop(next(iter(self._unknown_command_counts)))
            self._cmd_correlation_stats["unknown_cmdids_pruned"] += 1
        return count

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
        if locally_bonded and candidates:
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
                # Capture the source so we can persist it on auth success
                matched = next(
                    (c for c in candidates if c.ble_device.address.upper() == device.address.upper()),
                    None,
                )
                self._current_connect_source = matched.scanner.source if matched else None

        if device is None:
            raise BleakError(
                f"OneControl device {self.address} not found by HA Bluetooth"
            )

        # ── D-Bus setup BEFORE Bleak connect ──────────────────────────
        self._push_button_dbus_ok = False

        if self.is_pin_gateway:
            ctx = await prepare_pin_agent(self.address, self._bluetooth_pin)
            self._pin_agent_ctx = ctx
            if ctx and ctx.already_bonded:
                self._pin_dbus_succeeded = True
                self._pin_already_bonded = True
                _LOGGER.info(
                    "PIN gateway %s — already bonded, connecting directly",
                    self.address,
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
        _LOGGER.info("Connected to %s", self.address)

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
                    _LOGGER.debug("Requesting BLE pair (PushButton) with %s", self.address)
                    if hasattr(client, "pair"):
                        paired = await client.pair()
                        _LOGGER.info("BLE pair() result: %s", paired)
                    else:
                        _LOGGER.debug("pair() not available on client wrapper")
                except NotImplementedError:
                    _LOGGER.info("pair() not implemented — may already be bonded")
                except Exception as exc:
                    _LOGGER.warning("pair() failed: %s — continuing", exc)
        elif self._pin_agent_ctx and self._pin_agent_ctx.already_bonded:
            # Already bonded in BlueZ — no re-pairing needed.
            _LOGGER.info("PIN gateway %s — already bonded, skipping pair()", self.address)
            await self._pin_agent_ctx.cleanup()
            self._pin_agent_ctx = None
        elif self._pin_agent_ctx:
            # Agent is registered and waiting.  Call pair() now — BlueZ will
            # invoke our agent's RequestPinCode/RequestPasskey.
            # This matches Android: createBond() in onConnectionStateChange.
            _LOGGER.info(
                "PIN gateway %s — calling pair() with D-Bus agent active",
                self.address,
            )
            try:
                if hasattr(client, "pair"):
                    await client.pair()
                    _LOGGER.info(
                        "PIN bonding completed for %s (agent responded: %s)",
                        self.address,
                        self._pin_agent_ctx.agent_responded,
                    )
                    self._pin_dbus_succeeded = True
                else:
                    _LOGGER.warning("pair() not available — PIN bonding may fail")
            except NotImplementedError:
                _LOGGER.warning("pair() not implemented — PIN gateway may fail to authenticate")
            except Exception as exc:
                _LOGGER.warning("PIN pair() failed: %s", exc)
            finally:
                await self._pin_agent_ctx.cleanup()
                self._pin_agent_ctx = None
        else:
            # D-Bus not available (non-Linux / dev machine).
            _LOGGER.info(
                "PIN gateway %s — D-Bus not available, attempting Bleak pair() without agent",
                self.address,
            )
            try:
                if hasattr(client, "pair"):
                    paired = await client.pair()
                    _LOGGER.info("Bleak pair() result: %s", paired)
                else:
                    _LOGGER.warning("pair() not available on client wrapper")
            except NotImplementedError:
                _LOGGER.warning("pair() not implemented on this backend")
            except Exception as exc:
                _LOGGER.warning("Bleak pair() failed: %s", exc)

        await asyncio.sleep(0.5)

        # ── Enumerate services (diagnostic) ───────────────────────────
        try:
            services = client.services
            if services:
                svc_uuids = [s.uuid for s in services]
                _LOGGER.info("GATT services: %s", svc_uuids)
                # Check for CAN_WRITE characteristic (preferred lockout clear path)
                for svc in services:
                    for char in svc.characteristics:
                        if char.uuid == CAN_WRITE_CHAR_UUID:
                            self._has_can_write = True
                            _LOGGER.info("CAN_WRITE characteristic available")
                            break
            else:
                _LOGGER.warning("No GATT services discovered")
        except Exception as exc:
            _LOGGER.warning("Failed to enumerate services: %s", exc)

        # ── Step 1: Data Service Auth ─────────────────────────────────
        await self._authenticate_step1(client)

        await asyncio.sleep(NOTIFICATION_ENABLE_DELAY)

        # ── Enable notifications ──────────────────────────────────────
        await self._enable_notifications(client)

        _LOGGER.info("OneControl %s — notifications enabled, waiting for SEED", self.address)

        # For non-PIN gateways authenticated in step 1, start the heartbeat now.
        # PIN gateways become authenticated in _authenticate_step2 after the
        # SEED handshake.  Query bootstrap and heartbeat start after GatewayInfo
        # so startup commands stay serialized.

        # ── Persist bonded source ─────────────────────────────────────
        # Step 1 auth succeeded (reached here without exception), so the
        # adapter/proxy used for this connection holds a valid bond.  Store
        # it so future connects are pinned to the same source.
        if self._current_connect_source is not None:
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
        await self._myrvlink_runtime.send_metadata_request(table_id)

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
        table_id = self._select_get_devices_table_id()
        if table_id is None:
            return
        try:
            cmd_id = await self._send_get_devices_request(table_id)
            self._initial_get_devices_sent = True
            _LOGGER.debug(
                "Initial GetDevices sent for table %d (cmdId=%d)",
                table_id, cmd_id,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Initial GetDevices failed: %s", exc)

    def _select_get_devices_table_id(self) -> int | None:
        """Pick a table id for GetDevices when gateway info may be unavailable.

        GatewayInfo table id is preferred. On Ethernet bridges that never emit
        GatewayInfo, fall back to the most frequently observed non-zero table id
        from live device state keys (TT:DD).
        """
        if self.gateway_info is not None and self.gateway_info.table_id != 0:
            return self.gateway_info.table_id

        table_counts: dict[int, int] = {}
        for status_dict in (
            self.relays,
            self.dimmable_lights,
            self.rgb_lights,
            self.covers,
            self.hvac_zones,
            self.tanks,
            self.device_online,
            self.device_locks,
            self.generators,
            self.hour_meters,
        ):
            for key in status_dict:
                try:
                    table_id = int(key.split(":", 1)[0], 16)
                except (ValueError, IndexError):
                    continue
                if table_id == 0:
                    continue
                table_counts[table_id] = table_counts.get(table_id, 0) + 1

        if not table_counts:
            return None

        return max(table_counts, key=lambda tid: table_counts[tid])

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
        _LOGGER.info("Requesting metadata for observed table_id=%d", table_id)
        self.hass.async_create_task(self._send_metadata_request(table_id))

    # ------------------------------------------------------------------
    # Heartbeat keepalive (GetDevices every 5 seconds)
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Start the heartbeat loop after authentication."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._stop_heartbeat()
        interval = (
            _ETHERNET_HEARTBEAT_INTERVAL_S
            if self.is_ethernet_gateway
            else HEARTBEAT_INTERVAL
        )
        self._heartbeat_task = self.hass.async_create_background_task(
            self._heartbeat_loop(), name="ha_onecontrol_heartbeat"
        )
        _LOGGER.info("Heartbeat started (every %.1fs)", interval)

    def _stop_heartbeat(self) -> None:
        """Cancel the heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
            _LOGGER.debug("Heartbeat stopped")

    async def _force_ethernet_reconnect(self, reason: str) -> None:
        """Close Ethernet transport and trigger reconnect handling once."""
        runtime = getattr(self, "_ids_runtime", None)
        if runtime is not None:
            await runtime.force_reconnect(reason)
            return

        if not self.is_ethernet_gateway or not self._connected:
            return
        _LOGGER.debug("Forcing Ethernet reconnect (%s)", reason)
        await self._close_ethernet_transport()
        if self._connected:
            self._handle_transport_disconnect("ethernet", reason)

    async def _heartbeat_loop(self) -> None:
        """Send GetDevices periodically to keep BLE connection alive.

        Also monitors data freshness — if no events for STALE_CONNECTION_TIMEOUT
        seconds, forces a reconnect.

        Reference: Android HEARTBEAT_INTERVAL_MS = 5000L
        """
        interval = (
            _ETHERNET_HEARTBEAT_INTERVAL_S
            if self.is_ethernet_gateway
            else HEARTBEAT_INTERVAL
        )
        try:
            while self._connected and self._authenticated:
                await asyncio.sleep(interval)
                if not self._connected:
                    break

                if self.is_ethernet_gateway and not self.gateway_info:
                    runtime = getattr(self, "_ids_runtime", None)
                    try:
                        if runtime is not None:
                            await runtime.heartbeat_pre_gateway_cycle(
                                _ETHERNET_TRANSPORT_KEEPALIVE_INTERVAL_S
                            )
                        else:
                            await self._send_ethernet_transport_keepalive()
                            if getattr(self, "_pending_get_devices_cmdids", {}):
                                continue
                            table_id = self._select_get_devices_table_id()
                            if table_id is not None:
                                cmd = self._cmd.build_get_devices(table_id)
                                cmd_id = int.from_bytes(cmd[0:2], "little")
                                self._record_pending_get_devices_cmd(cmd_id, table_id)
                                await self.async_send_command(cmd)
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Ethernet transport keepalive error")
                        await self._force_ethernet_reconnect("transport keepalive failed")
                        break
                    continue

                if not self.gateway_info:
                    continue

                # Stale connection detection
                if (
                    self._last_event_time > 0
                    and (time.monotonic() - self._last_event_time) > STALE_CONNECTION_TIMEOUT
                ):
                    _LOGGER.warning(
                        "No events for %.0fs - connection stale, forcing reconnect",
                        STALE_CONNECTION_TIMEOUT,
                    )
                    if self.is_ethernet_gateway:
                        await self._force_ethernet_reconnect("stale heartbeat")
                    elif self._client:
                        try:
                            await self._client.disconnect()
                        except Exception:
                            pass
                    break

                try:
                    table_id = self._select_get_devices_table_id()
                    if table_id is None:
                        continue
                    if self._is_startup_bootstrap_active(table_id):
                        continue
                    await self._send_get_devices_request(table_id)
                except BleakError as exc:
                    _LOGGER.warning("Heartbeat BLE write failed: %s", exc)
                    if self.is_ethernet_gateway:
                        await self._force_ethernet_reconnect("heartbeat write failed")
                    break
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Heartbeat error")
                    if self.is_ethernet_gateway:
                        await self._force_ethernet_reconnect("heartbeat exception")
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
        self._prune_pending_command_state()

        event_type = frame[0]
        family = self._classify_frame_family(frame)
        self._frame_family_stats[family] = self._frame_family_stats.get(family, 0) + 1

        runtime = getattr(self, "_ids_runtime", None)
        if self.is_ethernet_gateway and runtime is not None and runtime.handle_frame(frame):
            return
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

        myrv_runtime = getattr(self, "_myrvlink_runtime", None)
        if (
            not self.is_ethernet_gateway
            and myrv_runtime is not None
            and myrv_runtime.handle_command_frame(frame)
        ):
            return

        try:
            event = parse_event(frame)
        except Exception as exc:  # noqa: BLE001
            self._cmd_correlation_stats["frame_parse_errors"] += 1
            count = self._cmd_correlation_stats["frame_parse_errors"]
            if count <= 3 or count in (10, 50, 100) or count % 500 == 0:
                _LOGGER.warning(
                    "Frame parse failed (event=0x%02X count=%d): %s frame=%s",
                    event_type,
                    count,
                    exc,
                    frame.hex(),
                )
            return
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

        elif isinstance(event, list):
            # Multi-item events: HvacZone list, TankLevel list, DeviceMetadata list
            for item in event:
                if isinstance(item, HvacZone):
                    self._handle_hvac_zone(item)
                elif isinstance(item, TankLevel):
                    key = _device_key(item.table_id, item.device_id)
                    self.tanks[key] = item

        elif isinstance(event, TankLevel):
            key = _device_key(event.table_id, event.device_id)
            self.tanks[key] = event

        elif isinstance(event, HvacZone):
            self._handle_hvac_zone(event)

        elif isinstance(event, DeviceOnline):
            key = _device_key(event.table_id, event.device_id)
            self.device_online[key] = event

        elif isinstance(event, SystemLockout):
            self.system_lockout_level = event.lockout_level
            _LOGGER.debug(
                "SystemLockout: level=%d table=%d devices=%d",
                event.lockout_level, event.table_id, event.device_count,
            )

        elif isinstance(event, DeviceLock):
            key = _device_key(event.table_id, event.device_id)
            self.device_locks[key] = event

        elif isinstance(event, GeneratorStatus):
            key = _device_key(event.table_id, event.device_id)
            self.generators[key] = event

        elif isinstance(event, HourMeter):
            key = _device_key(event.table_id, event.device_id)
            self.hour_meters[key] = event

        elif isinstance(event, RealTimeClock):
            self.rtc = event

        self._myrvlink_runtime.handle_metadata_for_event(event)
        self._dispatch_event_update(event)

    def _process_metadata(self, meta: DeviceMetadata) -> None:
        """Store metadata and resolve friendly name."""
        self._myrvlink_runtime.process_metadata(meta)

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
            "connection_type": self._connection_type,
        }
        if self.is_ethernet_gateway:
            data["eth_host"] = self._eth_host
            data["eth_port"] = self._eth_port
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
        self._handle_transport_disconnect("ble", "ble disconnected callback")

    @callback
    def _on_hass_stop(self, event: Any) -> None:
        """Stop non-critical background tasks during Home Assistant shutdown."""
        self._cancel_reconnect()
        self._stop_heartbeat()
        if self._ethernet_reader_task and not self._ethernet_reader_task.done():
            self._ethernet_reader_task.cancel()
            self._ethernet_reader_task = None
        self._ids_runtime.cleanup_on_disconnect()

    def _handle_transport_disconnect(self, transport: str, reason: str | None = None) -> None:
        """Handle unexpected transport disconnect and schedule reconnect."""
        reason_text = reason or "unknown"
        self._disconnect_count += 1
        self._last_disconnect_reason = f"{transport}:{reason_text}"
        _LOGGER.warning("OneControl %s disconnected (instance=%s)", self.address, self._instance_tag)
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
        self._pin_dbus_succeeded = False
        self._push_button_dbus_ok = False
        # PIN agent context is cleaned up inside _finish_connect; if somehow
        # still set here, schedule async cleanup (callback is synchronous).
        if self._pin_agent_ctx:
            ctx = self._pin_agent_ctx
            self._pin_agent_ctx = None
            self.hass.async_create_task(ctx.cleanup())

        if transport == "ethernet":
            self._ids_runtime.cleanup_on_disconnect()

        self.async_set_updated_data(self._build_data())

        # Schedule automatic reconnection with exponential backoff
        if getattr(self.hass, "is_stopping", False):
            return
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnect attempt with exponential backoff.

        Cancels any in-progress reconnect timer and restarts it.  This debounces
        rapid _on_disconnect calls that fire during BRC internal retries and
        prevents multiple concurrent reconnect coroutines from racing each other
        into BlueZ's "InProgress" error state.
        """
        if getattr(self.hass, "is_stopping", False):
            _LOGGER.debug("Skipping reconnect scheduling because Home Assistant is stopping")
            return
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        self._reconnect_generation += 1
        generation = self._reconnect_generation
        delay = min(
            RECONNECT_BACKOFF_BASE * (2 ** self._consecutive_failures),
            RECONNECT_BACKOFF_CAP,
        )
        self._consecutive_failures += 1
        _LOGGER.info(
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
            if getattr(self.hass, "is_stopping", False):
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

            _LOGGER.info(
                "Attempting reconnection to %s (gen=%d, instance=%s)...",
                self.address, generation, self._instance_tag,
            )
            await self.async_connect()
            # Success — reset backoff counter
            self._consecutive_failures = 0
            _LOGGER.info("Reconnected to %s (instance=%s)", self.address, self._instance_tag)
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
        if not self._connected and not getattr(self.hass, "is_stopping", False):
            try:
                await self.async_connect()
            except BleakError as exc:
                _LOGGER.warning("Reconnect failed: %s", exc)
        return self._build_data()
