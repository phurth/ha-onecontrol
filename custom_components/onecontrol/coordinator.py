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
from typing import Any, Callable

from bleak import BleakClient, BleakGATTCharacteristic
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AUTH_SERVICE_UUID,
    BLE_MTU_SIZE,
    CAN_WRITE_CHAR_UUID,
    CONF_GATEWAY_PIN,
    DATA_READ_CHAR_UUID,
    DATA_SERVICE_UUID,
    DATA_WRITE_CHAR_UUID,
    DEFAULT_GATEWAY_PIN,
    DOMAIN,
    HEARTBEAT_INTERVAL,
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

        self._client: BleakClient | None = None
        self._decoder = CobsByteDecoder(use_crc=True)
        self._cmd = CommandBuilder()
        self._authenticated = False
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._metadata_requested = False
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
    ) -> None:
        """Send an HVAC command."""
        cmd = self._cmd.build_action_hvac(
            table_id, device_id, heat_mode, heat_source, fan_mode, low_trip_f, high_trip_f
        )
        await self.async_send_command(cmd)

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
        table_ids = set()
        if self.gateway_info:
            table_ids.add(self.gateway_info.table_id)
        for key in list(self._metadata_raw.keys()):
            table_ids.add(self._metadata_raw[key].table_id)
        if not table_ids and self.gateway_info:
            table_ids.add(self.gateway_info.table_id)
        self._metadata_requested = False
        for tid in table_ids:
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
        for attempt in range(1, max_attempts + 1):
            try:
                await self._try_connect(attempt)
                return
            except Exception as exc:
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
                else:
                    raise

    async def _try_connect(self, attempt: int) -> None:
        """Single connection attempt — connect, pair, authenticate."""
        _LOGGER.info(
            "Connecting to OneControl gateway %s (attempt %d)",
            self.address, attempt,
        )

        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            raise BleakError(
                f"OneControl device {self.address} not found by HA Bluetooth"
            )

        client = BleakClient(
            device,
            disconnected_callback=self._on_disconnect,
            timeout=20.0,
        )

        # ── Connect with pairing ──────────────────────────────────────
        if hasattr(client, "_pair_before_connect"):
            client._pair_before_connect = True
            _LOGGER.info("BLE pair-before-connect enabled")

        await client.connect()
        self._client = client
        self._connected = True
        _LOGGER.info("Connected to %s (paired=%s)", self.address, client.is_connected)

        try:
            _LOGGER.info("Requesting explicit BLE pair with %s", self.address)
            if hasattr(client, "pair"):
                paired = await client.pair()
                _LOGGER.info("BLE pair() result: %s", paired)
            else:
                _LOGGER.debug("pair() not available on client wrapper")
        except NotImplementedError:
            _LOGGER.info("pair() not implemented on this backend — may already be bonded")
        except Exception as exc:
            _LOGGER.warning("pair() failed: %s — continuing", exc)

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
        try:
            await self.async_send_command(cmd)
            _LOGGER.info("Sent GetDevicesMetadata for table %d", table_id)
        except Exception as exc:
            _LOGGER.warning("Failed to send metadata request: %s", exc)

    async def _request_metadata_after_delay(self) -> None:
        """Wait 500ms then request metadata (INTERNALS.md timing)."""
        await asyncio.sleep(0.5)
        if self.gateway_info and not self._metadata_requested:
            self._metadata_requested = True
            await self._send_metadata_request(self.gateway_info.table_id)

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

        event = parse_event(frame)
        event_type = frame[0]
        _LOGGER.debug(
            "Event 0x%02X (%d bytes): %s",
            event_type,
            len(frame),
            type(event).__name__ if not isinstance(event, (bytes, bytearray, type(None))) else "raw",
        )

        # ── Update accumulated state ──────────────────────────────────
        if isinstance(event, GatewayInformation):
            self.gateway_info = event
            _LOGGER.debug(
                "GatewayInfo: table_id=%d, devices=%d",
                event.table_id,
                event.device_count,
            )
            # Trigger metadata request 500ms later (INTERNALS.md § Timing)
            if not self._metadata_requested:
                self.hass.async_create_task(self._request_metadata_after_delay())

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

        elif isinstance(event, RgbLight):
            key = _device_key(event.table_id, event.device_id)
            self.rgb_lights[key] = event

        elif isinstance(event, CoverStatus):
            key = _device_key(event.table_id, event.device_id)
            self.covers[key] = event

        elif isinstance(event, list):
            # Multi-item events: HvacZone list, TankLevel list, DeviceMetadata list
            for item in event:
                if isinstance(item, HvacZone):
                    key = _device_key(item.table_id, item.device_id)
                    self.hvac_zones[key] = item
                elif isinstance(item, TankLevel):
                    key = _device_key(item.table_id, item.device_id)
                    self.tanks[key] = item
                elif isinstance(item, DeviceMetadata):
                    self._process_metadata(item)

        elif isinstance(event, TankLevel):
            key = _device_key(event.table_id, event.device_id)
            self.tanks[key] = event

        elif isinstance(event, HvacZone):
            key = _device_key(event.table_id, event.device_id)
            self.hvac_zones[key] = event

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
        self._metadata_requested = False
        self._has_can_write = False

        # Schedule automatic reconnection with exponential backoff
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnect attempt with exponential backoff."""
        if self._reconnect_task and not self._reconnect_task.done():
            return  # Already scheduled

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
