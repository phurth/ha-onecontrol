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
import logging
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
    is_pin_pairing_supported,
    pair_push_button,
    prepare_pin_agent,
    remove_bond,
)
from .const import (
    AUTH_SERVICE_UUID,
    BLE_MTU_SIZE,
    CAN_WRITE_CHAR_UUID,
    CONF_BLUETOOTH_PIN,
    CONF_GATEWAY_PIN,
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
from .protocol.cobs import CobsByteDecoder, cobs_encode
from .protocol.commands import CommandBuilder
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
    RealTimeClock,
    RelayStatus,
    RgbLight,
    RvStatus,
    SystemLockout,
    TankLevel,
    parse_event,
)
from .protocol.dtc_codes import get_name as dtc_get_name, is_fault as dtc_is_fault
from .protocol.function_names import get_friendly_name
from .protocol.tea import calculate_step1_key, calculate_step2_key

_LOGGER = logging.getLogger(__name__)


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
            update_interval=None,  # push-based, no polling
        )
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.gateway_pin: str = entry.data.get(CONF_GATEWAY_PIN, DEFAULT_GATEWAY_PIN)

        # ── PIN-based pairing (legacy gateways) ──────────────────────
        self._pairing_method: str = entry.data.get(CONF_PAIRING_METHOD, "push_button")
        # Android uses gateway_pin for both BLE bonding AND protocol auth.
        # bluetooth_pin is an optional override if the BLE PIN differs.
        self._bluetooth_pin: str = entry.data.get(
            CONF_BLUETOOTH_PIN, ""
        ) or self.gateway_pin
        self._pin_agent_ctx: PinAgentContext | None = None  # active D-Bus agent context
        self._pin_dbus_succeeded: bool = False  # bonding completed this session
        self._pin_already_bonded: bool = False  # BlueZ "already bonded" seen (sticky — not reset on disconnect)

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
        self._pending_metadata_cmdids: dict[int, int] = {}  # cmdId → table_id
        # CRC of the metadata last successfully loaded from the gateway.
        # Persists across disconnect/reconnect so we can skip re-requests when
        # the gateway reports the same DeviceMetadataTableCrc (official app behaviour).
        self._last_metadata_crc: int | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
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

        # Last non-zero brightness per dimmable device (persists across off/on cycles).
        # Mirrors Android lastKnownDimmableBrightness — only updated when brightness > 0.
        self._last_known_dimmable_brightness: dict[str, int] = {}

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

    async def async_switch(
        self, table_id: int, device_id: int, state: bool
    ) -> None:
        """Send a switch on/off command."""
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
        auto_off: int = 0,
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
        # Reset per-table state so all tables can be re-requested
        self._metadata_requested_tables.clear()
        self._metadata_loaded_tables.clear()
        self._metadata_rejected_tables.clear()
        self._pending_metadata_cmdids.clear()

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
            self.generators, self.hour_meters,
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
        for adapter in ("hci0", "hci1", "hci2", "hci3"):
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
        """True if this gateway uses PIN-based (legacy) BLE pairing."""
        return self._pairing_method == "pin"

    async def _try_connect(self, attempt: int) -> None:
        """Single connection attempt — connect, pair, authenticate."""
        _LOGGER.info(
            "Connecting to OneControl gateway %s (attempt %d, method=%s)",
            self.address, attempt, self._pairing_method,
        )

        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            raise BleakError(
                f"OneControl device {self.address} not found by HA Bluetooth"
            )

        # ── D-Bus setup BEFORE Bleak connect ──────────────────────────
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
        elif is_pin_pairing_supported():
            _LOGGER.info(
                "PushButton gateway — attempting D-Bus Just Works pairing "
                "with %s before connect",
                self.address,
            )
            dbus_ok = await pair_push_button(self.address, timeout=30.0)
            if dbus_ok:
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
        _LOGGER.info("Connected to %s", self.address)

        # ── Pairing ────────────────────────────────────────────────────
        if not self.is_pin_gateway:
            # PushButton: D-Bus Just Works pairing ran pre-connect; call pair()
            # here as a belt-and-suspenders fallback in case it didn't bond.
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
            self._start_heartbeat()
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
        self._metadata_requested_tables.add(table_id)
        try:
            await self.async_send_command(cmd)
            _LOGGER.info("Sent GetDevicesMetadata for table %d (cmdId=%d)", table_id, cmd_id)
        except Exception as exc:
            _LOGGER.warning("Failed to send metadata request: %s", exc)

    async def _request_metadata_after_delay(self, table_id: int) -> None:
        """Wait 500ms then request metadata (INTERNALS.md timing)."""
        await asyncio.sleep(0.5)
        if (
            table_id not in self._metadata_requested_tables
            and table_id not in self._metadata_rejected_tables
        ):
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
            or table_id in self._metadata_rejected_tables
            or table_id in self._metadata_requested_tables
        ):
            return
        _LOGGER.info("Requesting metadata for observed table_id=%d", table_id)
        self.hass.async_create_task(self._send_metadata_request(table_id))

    # ------------------------------------------------------------------
    # Heartbeat keepalive (GetDevices every 5 seconds)
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Start the heartbeat loop after authentication."""
        self._stop_heartbeat()
        self._heartbeat_task = self.hass.async_create_task(self._heartbeat_loop())
        _LOGGER.info("Heartbeat started (every %.0fs)", HEARTBEAT_INTERVAL)

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
                    cmd = self._cmd.build_get_devices(self.gateway_info.table_id)
                    await self.async_send_command(cmd)
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
                completed_table = self._pending_metadata_cmdids.pop(cmd_id, None)
                if completed_table is not None and len(frame) >= 8:
                    response_crc = int.from_bytes(frame[4:8], "little")
                    expected_crc = (
                        self.gateway_info.device_metadata_table_crc
                        if self.gateway_info is not None
                        else 0
                    )
                    if expected_crc != 0 and response_crc != expected_crc:
                        _LOGGER.warning(
                            "Metadata CRC mismatch for table %d: "
                            "response=0x%08x, expected=0x%08x — discarding",
                            completed_table,
                            response_crc,
                            expected_crc,
                        )
                        self._metadata_loaded_tables.discard(completed_table)
                        self._last_metadata_crc = None
                    else:
                        _LOGGER.debug(
                            "Metadata completion OK for table %d (CRC=0x%08x)",
                            completed_table,
                            response_crc,
                        )
                return
            if response_type == 0x82:
                cmd_id = (frame[1] & 0xFF) | ((frame[2] & 0xFF) << 8)
                rejected_table = self._pending_metadata_cmdids.pop(cmd_id, None)
                if rejected_table is not None:
                    error_code = frame[4] & 0xFF if len(frame) >= 5 else -1
                    if error_code == 0x0F:
                        self._metadata_rejected_tables.add(rejected_table)
                        _LOGGER.warning(
                            "Metadata rejected by gateway for table_id=%d (errorCode=0x0f)"
                            " — suppressing retries",
                            rejected_table,
                        )
                    else:
                        _LOGGER.warning(
                            "Metadata request failed for table_id=%d (errorCode=0x%02x)",
                            rejected_table,
                            error_code if error_code >= 0 else 0,
                        )
                else:
                    _LOGGER.debug(
                        "Metadata error response for unknown cmdId=%d", cmd_id
                    )
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

            # Schedule metadata request if not already handled by CRC gate above.
            if (
                event.table_id not in self._metadata_loaded_tables
                and event.table_id not in self._metadata_requested_tables
                and event.table_id not in self._metadata_rejected_tables
            ):
                self.hass.async_create_task(
                    self._request_metadata_after_delay(event.table_id)
                )

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
        _LOGGER.warning("OneControl %s disconnected", self.address)
        self._stop_heartbeat()
        self._connected = False
        self._authenticated = False
        self._decoder.reset()
        self._metadata_requested_tables.clear()
        self._metadata_loaded_tables.clear()
        self._metadata_rejected_tables.clear()
        self._pending_metadata_cmdids.clear()
        self._has_can_write = False
        self._pin_dbus_succeeded = False
        # PIN agent context is cleaned up inside _finish_connect; if somehow
        # still set here, schedule async cleanup (callback is synchronous).
        if self._pin_agent_ctx:
            ctx = self._pin_agent_ctx
            self._pin_agent_ctx = None
            self.hass.async_create_task(ctx.cleanup())

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

        delay = min(
            RECONNECT_BACKOFF_BASE * (2 ** self._consecutive_failures),
            RECONNECT_BACKOFF_CAP,
        )
        self._consecutive_failures += 1
        _LOGGER.info(
            "Scheduling reconnect in %.0fs (attempt %d)",
            delay, self._consecutive_failures,
        )
        self._reconnect_task = self.hass.async_create_task(
            self._reconnect_with_delay(delay)
        )

    async def _reconnect_with_delay(self, delay: float) -> None:
        """Wait then attempt reconnection."""
        try:
            await asyncio.sleep(delay)
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

            _LOGGER.info("Attempting reconnection to %s...", self.address)
            await self.async_connect()
            # Success — reset backoff counter
            self._consecutive_failures = 0
            _LOGGER.info("Reconnected to %s", self.address)
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
        if not self._connected:
            try:
                await self.async_connect()
            except BleakError as exc:
                _LOGGER.warning("Reconnect failed: %s", exc)
        return self._build_data()
