"""IDS-CAN (Ethernet bridge) runtime.

This runtime owns Ethernet transport/session handling while delegating shared
state updates and frame parsing back to the coordinator.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import TYPE_CHECKING, Callable

from ..protocol.ids_can_wire import (
    compose_ids_can_extended_wire_frame,
    decode_ids_can_payload,
    format_ids_can_payload,
    ids_can_message_type_name,
    parse_ids_can_wire_frame,
)
from ..protocol.cobs import cobs_encode
from ..protocol.events import (
    CoverStatus,
    DeviceIdentity,
    DimmableLight,
    GeneratorStatus,
    HourMeter,
    HvacZone,
    RelayStatus,
    RgbLight,
    TankLevel,
    parse_get_devices_response,
    parse_metadata_response,
)

if TYPE_CHECKING:
    from ..coordinator import OneControlCoordinator

_LOGGER = logging.getLogger(__name__)

_IDS_SESSION_REMOTE_CONTROL = 0x0004
_IDS_SESSION_REMOTE_CONTROL_CYPHER = 0xB16B5E95
_IDS_COMMAND_SETTLE_WINDOW_S = 8.0
_HVAC_INVALID_TEMPERATURE_RAW_VALUES = {0x8000, 0x2FF0}


class IdsCanRuntime:
    """Runtime for IDS-CAN over Ethernet transport."""

    def __init__(self, coordinator: OneControlCoordinator) -> None:
        self._c = coordinator
        self._ids_source_product_mac: dict[int, str] = {}
        self._ids_source_identities: dict[int, DeviceIdentity] = {}
        # Learned source address used when transmitting IDS extended COMMAND frames.
        self._ids_controller_source_address: int = 0x3A
        self._ids_recent_command_targets: dict[int, float] = {}
        self._ids_session_opened_at: dict[int, float] = {}
        self._ids_session_waiters: dict[int, asyncio.Event] = {}
        self._ids_session_results: dict[int, bool | None] = {}
        self._ids_session_locks: dict[int, asyncio.Lock] = {}
        self._ids_session_seed_requested_at: dict[int, float] = {}
        self._ids_session_last_status_code: dict[int, int] = {}
        self._ids_session_last_heartbeat_at: dict[int, float] = {}
        self._ids_active_session_target: int | None = None
        self._ids_pending_status_expectations: dict[tuple[int, int], tuple[bool, float]] = {}
        self._ids_command_locks: dict[int, asyncio.Lock] = {}

    def _record_recent_command_target(self, target_address: int) -> None:
        """Track recent IDS command targets for response correlation logs."""
        now = time.monotonic()
        self._ids_recent_command_targets[target_address & 0xFF] = now
        # Keep map small and recent.
        stale_cutoff = now - 5.0
        stale_keys = [k for k, ts in self._ids_recent_command_targets.items() if ts < stale_cutoff]
        for key in stale_keys:
            self._ids_recent_command_targets.pop(key, None)

    def _set_pending_status_expectation(self, device_type: int, device_id: int, desired_on: bool) -> None:
        """Record short-lived desired state to suppress immediate stale status echoes."""
        self._ids_pending_status_expectations[(device_type & 0xFF, device_id & 0xFF)] = (
            desired_on,
            time.monotonic() + _IDS_COMMAND_SETTLE_WINDOW_S,
        )

    def _clear_pending_status_expectation(self, device_type: int, device_id: int) -> None:
        """Clear pending status expectation for a device when command send fails."""
        self._ids_pending_status_expectations.pop((device_type & 0xFF, device_id & 0xFF), None)

    def _should_accept_status(self, device_type: int, device_id: int, observed_on: bool) -> bool:
        """Gate status updates during a small post-command settle window."""
        key = (device_type & 0xFF, device_id & 0xFF)
        expectation = self._ids_pending_status_expectations.get(key)
        if expectation is None:
            return True

        desired_on, expires_at = expectation
        now = time.monotonic()
        if now >= expires_at:
            self._ids_pending_status_expectations.pop(key, None)
            return True

        if observed_on == desired_on:
            self._ids_pending_status_expectations.pop(key, None)
            return True

        _LOGGER.debug(
            "PACKET RX IDS status suppressed device_type=%d device=0x%02X observed_on=%s desired_on=%s remaining_ms=%d",
            device_type & 0xFF,
            device_id & 0xFF,
            observed_on,
            desired_on,
            int((expires_at - now) * 1000),
        )
        return False

    def _ids_encrypt_session_seed(self, seed: int) -> int:
        """Encrypt IDS session seed using REMOTE_CONTROL session cypher parity."""
        s = seed & 0xFFFFFFFF
        c = _IDS_SESSION_REMOTE_CONTROL_CYPHER & 0xFFFFFFFF
        rounds = 32
        delta = 0x9E3779B9
        summation = delta
        while True:
            s = (s + (((c << 4) + 1131376761) ^ (c + summation) ^ ((c >> 5) + 1919510376))) & 0xFFFFFFFF
            rounds -= 1
            if rounds <= 0:
                break
            c = (c + (((s << 4) + 1948272964) ^ (s + summation) ^ ((s >> 5) + 1400073827))) & 0xFFFFFFFF
            summation = (summation + delta) & 0xFFFFFFFF
        return s

    @staticmethod
    def _decode_hvac_temp_88(raw: int) -> float | None:
        """Decode signed big-endian 8.8 HVAC temperature with native invalid markers."""
        value = raw & 0xFFFF
        if value in _HVAC_INVALID_TEMPERATURE_RAW_VALUES:
            return None
        signed = value - 0x10000 if value >= 0x8000 else value
        return signed / 256.0

    async def _send_ids_request(self, target_address: int, request_code: int, payload: bytes) -> bool:
        """Send one IDS REQUEST(0x80) frame to target over Ethernet."""
        if not self._c.is_ethernet_gateway or not self._c._connected or self._c._eth_writer is None:
            return False
        raw = compose_ids_can_extended_wire_frame(
            message_type=0x80,
            source_address=self._ids_controller_source_address,
            target_address=target_address & 0xFF,
            message_data=request_code & 0xFF,
            payload=payload,
        )
        self._c._eth_writer.write(cobs_encode(raw))
        await self._c._eth_writer.drain()
        self._c._last_ethernet_tx_time = time.monotonic()
        _LOGGER.warning(
            "PACKET TX IDS request src=0x%02X dst=0x%02X req=0x%02X payload=%s raw=%s",
            self._ids_controller_source_address & 0xFF,
            target_address & 0xFF,
            request_code & 0xFF,
            payload.hex(),
            raw.hex(),
        )
        return True

    def _get_command_lock(self, target_address: int) -> asyncio.Lock:
        """Return per-target lock to serialize IDS commands per device."""
        target = target_address & 0xFF
        lock = self._ids_command_locks.get(target)
        if lock is None:
            lock = asyncio.Lock()
            self._ids_command_locks[target] = lock
        return lock

    async def _send_ids_command_with_retry(
        self,
        target_address: int,
        compose_frame: Callable[[], bytes],
        command_name: str,
        max_attempts: int = 3,
    ) -> tuple[bool, bytes | None]:
        """Serialize and retry IDS command sends with session establishment."""
        target = target_address & 0xFF
        lock = self._get_command_lock(target)
        async with lock:
            for attempt in range(1, max_attempts + 1):
                if not self._c.is_ethernet_gateway or not self._c._connected or self._c._eth_writer is None:
                    _LOGGER.warning(
                        "PACKET TX IDS %s aborted dst=0x%02X reason=transport-not-ready attempt=%d/%d",
                        command_name,
                        target,
                        attempt,
                        max_attempts,
                    )
                    return False, None

                session_ok = await self.ensure_remote_control_session(target)
                if not session_ok:
                    _LOGGER.warning(
                        "PACKET TX IDS %s session-not-ready dst=0x%02X attempt=%d/%d",
                        command_name,
                        target,
                        attempt,
                        max_attempts,
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(0.2 * attempt)
                        continue
                    return False, None

                raw = compose_frame()
                try:
                    self._record_recent_command_target(target)
                    self._c._eth_writer.write(cobs_encode(raw))
                    await self._c._eth_writer.drain()
                    self._c._last_ethernet_tx_time = time.monotonic()
                    return True, raw
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "PACKET TX IDS %s write failed dst=0x%02X attempt=%d/%d err=%s",
                        command_name,
                        target,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(0.2 * attempt)

            return False, None

    async def ensure_remote_control_session(self, target_address: int) -> bool:
        """Best-effort IDS REMOTE_CONTROL session open for command-capable devices."""
        target = target_address & 0xFF
        lock = self._ids_session_locks.get(target)
        if lock is None:
            lock = asyncio.Lock()
            self._ids_session_locks[target] = lock

        async with lock:
            opened_at = self._ids_session_opened_at.get(target)
            now = time.monotonic()
            if opened_at is not None and (now - opened_at) <= 45.0:
                self._ids_active_session_target = target
                last_heartbeat_at = self._ids_session_last_heartbeat_at.get(target, 0.0)
                if (now - last_heartbeat_at) >= 1.0:
                    heartbeat_payload = _IDS_SESSION_REMOTE_CONTROL.to_bytes(2, "big")
                    sent_heartbeat = await self._send_ids_request(target, 0x44, heartbeat_payload)
                    if sent_heartbeat:
                        self._ids_session_last_heartbeat_at[target] = now
                return True

            previous_target = self._ids_active_session_target
            if previous_target is not None and previous_target != target:
                previous_opened_at = self._ids_session_opened_at.get(previous_target)
                if previous_opened_at is not None and (now - previous_opened_at) <= 180.0:
                    end_payload = _IDS_SESSION_REMOTE_CONTROL.to_bytes(2, "big")
                    sent_end = await self._send_ids_request(previous_target, 0x45, end_payload)
                    if sent_end:
                        _LOGGER.warning(
                            "PACKET TX IDS session-end src=0x%02X dst=0x%02X session=0x%04X",
                            self._ids_controller_source_address & 0xFF,
                            previous_target & 0xFF,
                            _IDS_SESSION_REMOTE_CONTROL,
                        )
                self._ids_session_opened_at.pop(previous_target, None)
                self._ids_session_last_heartbeat_at.pop(previous_target, None)

            max_attempts = 2
            for attempt in range(1, max_attempts + 1):
                event = self._ids_session_waiters.get(target)
                if event is None:
                    event = asyncio.Event()
                    self._ids_session_waiters[target] = event
                else:
                    event.clear()

                self._ids_session_results[target] = None
                self._ids_session_seed_requested_at[target] = time.monotonic()

                seed_req_payload = _IDS_SESSION_REMOTE_CONTROL.to_bytes(2, "big")
                sent = await self._send_ids_request(target, 0x42, seed_req_payload)
                if not sent:
                    self._ids_session_seed_requested_at.pop(target, None)
                    self._ids_session_results.pop(target, None)
                    return False

                try:
                    await asyncio.wait_for(event.wait(), timeout=1.2)
                except TimeoutError:
                    _LOGGER.warning(
                        "PACKET TX IDS session-open timeout dst=0x%02X attempt=%d/%d; skipping IDS command send",
                        target,
                        attempt,
                        max_attempts,
                    )
                    self._ids_session_seed_requested_at.pop(target, None)
                    self._ids_session_results.pop(target, None)
                    return False

                result = self._ids_session_results.get(target)
                if result is True:
                    self._ids_active_session_target = target
                    return True

                status_code = self._ids_session_last_status_code.get(target)
                if status_code == 0x0B and attempt < max_attempts:
                    _LOGGER.warning(
                        "PACKET TX IDS session-open busy dst=0x%02X; retrying attempt=%d/%d",
                        target,
                        attempt + 1,
                        max_attempts,
                    )
                    await asyncio.sleep(0.15)
                    continue

                # Some devices report BUSY for seed requests while still honoring
                # commands on an already-open remote-control session. If we have a
                # recent successful open, keep using it instead of hard-failing.
                if status_code == 0x0B:
                    recent_opened_at = self._ids_session_opened_at.get(target)
                    if recent_opened_at is not None and (time.monotonic() - recent_opened_at) <= 180.0:
                        _LOGGER.warning(
                            "PACKET TX IDS session-open busy dst=0x%02X; reusing recent session-open age=%.1fs",
                            target,
                            time.monotonic() - recent_opened_at,
                        )
                        return True

                _LOGGER.warning(
                    "PACKET TX IDS session-open failed dst=0x%02X attempt=%d/%d; skipping IDS command send",
                    target,
                    attempt,
                    max_attempts,
                )
                return False

            return False

    def _ids_default_table_id(self) -> int:
        """Best-effort table id for IDS-only Ethernet traffic."""
        if self._c.gateway_info is not None and self._c.gateway_info.table_id != 0:
            return self._c.gateway_info.table_id & 0xFF
        inferred = self._c._select_get_devices_table_id()
        if inferred is not None and inferred != 0:
            return inferred & 0xFF
        return 0x03

    def _resolve_ids_identity(
        self,
        table_id: int,
        device_id: int,
        expected_device_type: int,
    ) -> tuple[str, DeviceIdentity] | None:
        """Resolve IDS identity by exact key first, then by device-id fallback.

        Some gateways report table ids inconsistently between discovery and control
        paths, so we allow a controlled fallback on matching device_id/type.
        """
        key = f"{table_id & 0xFF:02x}:{device_id & 0xFF:02x}"
        identity = self._c._device_identities.get(key)
        if identity is not None:
            return key, identity

        candidates: list[tuple[str, DeviceIdentity]] = []
        for identity_key, candidate in self._c._device_identities.items():
            if (
                candidate.protocol == 2
                and (candidate.device_id & 0xFF) == (device_id & 0xFF)
                and (candidate.device_type & 0xFF) == (expected_device_type & 0xFF)
            ):
                candidates.append((identity_key, candidate))

        if len(candidates) == 1:
            matched_key, matched_identity = candidates[0]
            _LOGGER.warning(
                "PACKET TX IDS identity fallback requested=%s matched=%s device_type=%d",
                key,
                matched_key,
                matched_identity.device_type,
            )
            return matched_key, matched_identity

        if len(candidates) > 1:
            _LOGGER.warning(
                "PACKET TX IDS identity ambiguous requested=%s matches=%d device_type=%d",
                key,
                len(candidates),
                expected_device_type,
            )
        return None

    def _ensure_event_store_maps(self) -> None:
        """Ensure coordinator state maps exist for lightweight test doubles."""
        for attr in (
            "relays",
            "dimmable_lights",
            "rgb_lights",
            "covers",
            "hvac_zones",
            "tanks",
            "generators",
            "hour_meters",
            "_device_identities",
        ):
            if not hasattr(self._c, attr):
                setattr(self._c, attr, {})

    def _dispatch_state_event(self, event: object) -> None:
        """Publish translated IDS state updates through normal coordinator fan-out."""
        dispatch = getattr(self._c, "_dispatch_event_update", None)
        if callable(dispatch):
            dispatch(event)
            return
        if hasattr(self._c, "async_set_updated_data") and hasattr(self._c, "_build_data"):
            self._c.async_set_updated_data(self._c._build_data())

    def _bootstrap_entity_from_identity(self, identity: DeviceIdentity) -> None:
        """Seed coordinator state dicts from IDS DEVICE_ID payloads."""
        self._ensure_event_store_maps()
        key = f"{identity.table_id:02x}:{identity.device_id:02x}"
        emitted_event: object | None = None

        if identity.device_type == 30 and key not in self._c.relays:
            event = RelayStatus(
                table_id=identity.table_id,
                device_id=identity.device_id,
                is_on=False,
                status_byte=0,
                dtc_code=0,
            )
            self._c.relays[key] = event
            emitted_event = event
        elif identity.device_type == 20 and key not in self._c.dimmable_lights:
            event = DimmableLight(
                table_id=identity.table_id,
                device_id=identity.device_id,
                brightness=0,
                mode=0,
            )
            self._c.dimmable_lights[key] = event
            emitted_event = event
        elif identity.device_type == 13 and key not in self._c.rgb_lights:
            event = RgbLight(
                table_id=identity.table_id,
                device_id=identity.device_id,
                mode=0,
                red=0,
                green=0,
                blue=0,
                brightness=255,
            )
            self._c.rgb_lights[key] = event
            emitted_event = event
        elif identity.device_type == 33 and key not in self._c.covers:
            event = CoverStatus(
                table_id=identity.table_id,
                device_id=identity.device_id,
                status=0xC0,
                position=None,
            )
            self._c.covers[key] = event
            emitted_event = event
        elif identity.device_type == 16 and key not in self._c.hvac_zones:
            event = HvacZone(
                table_id=identity.table_id,
                device_id=identity.device_id,
                heat_mode=0,
                heat_source=0,
                fan_mode=0,
                low_trip_f=68,
                high_trip_f=72,
                zone_status=0,
                indoor_temp_f=None,
                outdoor_temp_f=None,
                dtc_code=0,
            )
            self._c.hvac_zones[key] = event
            emitted_event = event
        elif identity.device_type == 10 and key not in self._c.tanks:
            event = TankLevel(
                table_id=identity.table_id,
                device_id=identity.device_id,
                level=0,
            )
            self._c.tanks[key] = event
            emitted_event = event
        elif identity.device_type == 24 and key not in self._c.generators:
            event = GeneratorStatus(
                table_id=identity.table_id,
                device_id=identity.device_id,
                state=0,
                battery_voltage=0.0,
                temperature_c=None,
                quiet_hours=False,
            )
            self._c.generators[key] = event
            emitted_event = event
        elif identity.device_type == 12 and key not in self._c.hour_meters:
            event = HourMeter(
                table_id=identity.table_id,
                device_id=identity.device_id,
                hours=0.0,
                maintenance_due=False,
                maintenance_past_due=False,
                error=False,
            )
            self._c.hour_meters[key] = event
            emitted_event = event

        if emitted_event is not None:
            self._dispatch_state_event(emitted_event)

    def _handle_ids_device_id(self, source_address: int, semantic_fields: dict[str, int | str | bool]) -> None:
        """Map IDS DEVICE_ID frames into coordinator identity/name state."""
        table_id = self._ids_default_table_id()
        device_id = source_address & 0xFF
        product_id = int(semantic_fields.get("product_id", 0))
        device_type = int(semantic_fields.get("device_type", 0))
        device_instance = int(semantic_fields.get("device_instance", 0))
        raw_device_capability = int(semantic_fields.get("device_capabilities", 0))
        product_mac = self._ids_source_product_mac.get(device_id, "")

        identity = DeviceIdentity(
            table_id=table_id,
            device_id=device_id,
            protocol=2,
            device_type=device_type,
            device_instance=device_instance,
            product_id=product_id,
            product_mac=product_mac,
            raw_device_capability=raw_device_capability,
        )
        key = f"{table_id:02x}:{device_id:02x}"
        self._ids_source_identities[device_id] = identity
        self._c._device_identities[key] = identity
        self._c._apply_external_name(key, identity)
        self._bootstrap_entity_from_identity(identity)

    def _handle_ids_device_status(self, source_address: int, payload: bytes) -> None:
        """Translate core IDS DEVICE_STATUS payloads into coordinator state events."""
        if not payload:
            return

        identity = self._ids_source_identities.get(source_address & 0xFF)
        if identity is None:
            return

        self._ensure_event_store_maps()
        table_id = identity.table_id
        device_id = identity.device_id
        key = f"{table_id:02x}:{device_id:02x}"
        status0 = payload[0] & 0xFF
        on_state = (status0 & 0x01) != 0

        if identity.device_type in {13, 20, 30} and not self._should_accept_status(
            identity.device_type,
            device_id,
            on_state,
        ):
            return

        emitted_event: object | None = None
        if identity.device_type == 30:
            event = RelayStatus(
                table_id=table_id,
                device_id=device_id,
                is_on=on_state,
                status_byte=status0,
                dtc_code=0,
            )
            self._c.relays[key] = event
            emitted_event = event
        elif identity.device_type == 20:
            brightness = payload[3] & 0xFF if len(payload) >= 4 else (255 if on_state else 0)
            event = DimmableLight(
                table_id=table_id,
                device_id=device_id,
                brightness=brightness,
                mode=1 if on_state else 0,
            )
            self._c.dimmable_lights[key] = event
            emitted_event = event
        elif identity.device_type == 13:
            current = self._c.rgb_lights.get(key)
            event = RgbLight(
                table_id=table_id,
                device_id=device_id,
                mode=1 if on_state else 0,
                red=current.red if current else 0,
                green=current.green if current else 0,
                blue=current.blue if current else 0,
                brightness=current.brightness if current else 255,
            )
            self._c.rgb_lights[key] = event
            emitted_event = event
        elif identity.device_type == 16 and len(payload) >= 8:
            cmd = payload[0] & 0xFF
            low_trip_f = payload[1] & 0xFF
            high_trip_f = payload[2] & 0xFF
            zone_status = payload[3] & 0x8F
            indoor_raw = ((payload[4] & 0xFF) << 8) | (payload[5] & 0xFF)
            outdoor_raw = ((payload[6] & 0xFF) << 8) | (payload[7] & 0xFF)
            dtc_code = int.from_bytes(payload[8:10], "big") if len(payload) >= 10 else 0

            event = HvacZone(
                table_id=table_id,
                device_id=device_id,
                heat_mode=cmd & 0x07,
                heat_source=(cmd >> 4) & 0x03,
                fan_mode=(cmd >> 6) & 0x03,
                low_trip_f=low_trip_f,
                high_trip_f=high_trip_f,
                zone_status=zone_status,
                indoor_temp_f=self._decode_hvac_temp_88(indoor_raw),
                outdoor_temp_f=self._decode_hvac_temp_88(outdoor_raw),
                dtc_code=dtc_code,
            )
            handle_hvac_zone = getattr(self._c, "_handle_hvac_zone", None)
            if callable(handle_hvac_zone):
                handle_hvac_zone(event)
            else:
                self._c.hvac_zones[key] = event
                if hasattr(self._c, "_hvac_zone_states"):
                    self._c._hvac_zone_states[key] = event
            emitted_event = event
        elif identity.device_type == 33:
            position = payload[1] & 0xFF if len(payload) >= 2 and payload[1] != 0xFF else None
            event = CoverStatus(
                table_id=table_id,
                device_id=device_id,
                status=status0,
                position=position,
            )
            self._c.covers[key] = event
            emitted_event = event
        elif identity.device_type == 10:
            event = TankLevel(
                table_id=table_id,
                device_id=device_id,
                level=payload[0] & 0xFF,
            )
            self._c.tanks[key] = event
            emitted_event = event
        elif identity.device_type == 24:
            event = GeneratorStatus(
                table_id=table_id,
                device_id=device_id,
                state=status0 & 0x07,
                battery_voltage=0.0,
                temperature_c=None,
                quiet_hours=bool(status0 & 0x80),
            )
            self._c.generators[key] = event
            emitted_event = event
        elif identity.device_type == 12 and len(payload) >= 4:
            operating_seconds = int.from_bytes(payload[0:4], "big")
            event = HourMeter(
                table_id=table_id,
                device_id=device_id,
                hours=operating_seconds / 3600.0,
                maintenance_due=False,
                maintenance_past_due=False,
                error=False,
            )
            self._c.hour_meters[key] = event
            emitted_event = event

        if emitted_event is not None:
            self._dispatch_state_event(emitted_event)

    async def connect(self) -> None:
        """Connect to an IDS CAN-to-Ethernet bridge with retries."""
        max_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                await self._try_connect(attempt)
                return
            except Exception as exc:
                last_exc = exc
                _LOGGER.warning(
                    "Ethernet connection attempt %d/%d failed: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                await self.close_transport()
                self._c._connected = False
                self._c._authenticated = False
                if attempt < max_attempts:
                    delay = 2 * attempt
                    _LOGGER.info("Retrying Ethernet in %ds...", delay)
                    await asyncio.sleep(delay)

        assert last_exc is not None
        raise last_exc

    async def _try_connect(self, attempt: int) -> None:
        """Open TCP connection to Ethernet bridge and start reader task."""
        host = self._c._eth_host or self._c.address
        port = self._c._eth_port
        if not host or port <= 0:
            raise ConnectionError("Ethernet host/port are not configured")

        _LOGGER.info(
            "Connecting to OneControl bridge %s:%d (attempt %d)",
            host,
            port,
            attempt,
        )

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port),
            timeout=10.0,
        )

        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, "TCP_KEEPIDLE"):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                if hasattr(socket, "TCP_KEEPINTVL"):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                if hasattr(socket, "TCP_KEEPCNT"):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Unable to apply TCP keepalive socket options", exc_info=True)

        self._c._eth_reader = reader
        self._c._eth_writer = writer
        self._c._last_ethernet_tx_time = time.monotonic()
        self._c._connected = True
        self._c._authenticated = True
        self._c._decoder.reset()
        self._c.async_set_updated_data(self._c._build_data())

        self._c._ethernet_reader_task = self._c.hass.async_create_background_task(
            self.read_loop(),
            "ha_onecontrol_ethernet_reader",
        )
        self._c._start_heartbeat()
        self._c.hass.async_create_task(self._c._send_initial_get_devices())

    async def read_loop(self) -> None:
        """Read Ethernet bytes and decode COBS frames into protocol events."""
        if self._c._eth_reader is None:
            return

        try:
            while self._c._connected and self._c._eth_reader is not None:
                chunk = await self._c._eth_reader.read(512)
                if not chunk:
                    raise ConnectionError("Ethernet bridge closed connection")
                for byte_val in chunk:
                    frame = self._c._decoder.decode_byte(byte_val)
                    if frame is not None:
                        looks_like_myrvlink_cmd = (
                            len(frame) >= 4
                            and (frame[0] & 0xFF) == 0x02
                            and (frame[3] & 0xFF) in {0x01, 0x02, 0x81, 0x82}
                        )
                        wire = parse_ids_can_wire_frame(frame)
                        if wire is not None and not looks_like_myrvlink_cmd:
                            semantic = decode_ids_can_payload(wire)
                            semantic_suffix = format_ids_can_payload(semantic)
                            log_level = logging.WARNING
                            if semantic is not None:
                                log_level = logging.DEBUG

                            if _LOGGER.isEnabledFor(log_level):
                                if wire.is_extended:
                                    _LOGGER.log(
                                        log_level,
                                        "PACKET RX IDS dlc=%d id=0x%08X ext=1 type=0x%02X(%s) src=0x%02X dst=0x%02X msg=0x%02X payload=%s%s",
                                        wire.dlc,
                                        wire.can_id,
                                        wire.message_type,
                                        ids_can_message_type_name(wire.message_type),
                                        wire.source_address,
                                        wire.target_address if wire.target_address is not None else 0,
                                        wire.message_data if wire.message_data is not None else 0,
                                        wire.payload.hex(),
                                        semantic_suffix,
                                    )
                                else:
                                    _LOGGER.log(
                                        log_level,
                                        "PACKET RX IDS dlc=%d id=0x%03X ext=0 type=0x%02X(%s) src=0x%02X payload=%s%s",
                                        wire.dlc,
                                        wire.can_id,
                                        wire.message_type,
                                        ids_can_message_type_name(wire.message_type),
                                        wire.source_address,
                                        wire.payload.hex(),
                                        semantic_suffix,
                                    )
                        else:
                            _LOGGER.warning(
                                "PACKET RX ETH frame len=%d raw=%s",
                                len(frame),
                                frame.hex(),
                            )
                        self._c._process_frame(frame)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Ethernet read loop ended: %s", exc)
        finally:
            if self._c._connected:
                self._c._handle_transport_disconnect("ethernet", "read loop ended")

    async def close_transport(self) -> None:
        """Close active Ethernet socket and reader task."""
        if self._c._ethernet_reader_task and not self._c._ethernet_reader_task.done():
            self._c._ethernet_reader_task.cancel()
            self._c._ethernet_reader_task = None

        if self._c._eth_writer is not None:
            self._c._eth_writer.close()
            try:
                await self._c._eth_writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

        self._c._eth_reader = None
        self._c._eth_writer = None
        self._c._last_ethernet_tx_time = 0.0

    async def send_transport_keepalive(self, interval_s: float) -> None:
        """Send a transport-level delimiter to prevent idle TCP closes."""
        if not self._c.is_ethernet_gateway or not self._c._connected or self._c._eth_writer is None:
            return
        if (time.monotonic() - self._c._last_ethernet_tx_time) < interval_s:
            return
        self._c._eth_writer.write(b"\x00")
        await self._c._eth_writer.drain()
        self._c._last_ethernet_tx_time = time.monotonic()
        self._c._ethernet_transport_keepalives_sent += 1
        _LOGGER.debug("TX Ethernet transport keepalive delimiter")

    async def force_reconnect(self, reason: str) -> None:
        """Close Ethernet transport and trigger reconnect handling once."""
        if not self._c.is_ethernet_gateway or not self._c._connected:
            return
        _LOGGER.debug("Forcing Ethernet reconnect (%s)", reason)
        await self.close_transport()
        if self._c._connected:
            self._c._handle_transport_disconnect("ethernet", reason)

    async def heartbeat_pre_gateway_cycle(self, keepalive_interval_s: float) -> None:
        """Keep transport alive and optionally probe table IDs before GatewayInfo arrives."""
        if not self._c.is_ethernet_gateway or not self._c._connected:
            return
        if self._c.gateway_info is not None:
            return

        await self.send_transport_keepalive(keepalive_interval_s)
        if self._c._pending_get_devices_cmdids:
            return
        table_id = self._c._select_get_devices_table_id()
        if table_id is None:
            return

        cmd = self._c._cmd.build_get_devices(table_id)
        cmd_id = int.from_bytes(cmd[0:2], "little")
        self._c._record_pending_get_devices_cmd(cmd_id, table_id)
        await self._c.async_send_command(cmd)

    async def send_light_toggle_command(self, table_id: int, device_id: int, turn_on: bool) -> bool:
        """Send an IDS native COMMAND(0x82) frame for dimmable light on/off.

        Returns True when IDS-native command path was used and written.
        Returns False when prerequisites are missing so callers can fallback.
        """
        brightness = 255 if turn_on else 0
        return await self.send_light_brightness_command(table_id, device_id, brightness)

    async def send_light_brightness_command(self, table_id: int, device_id: int, brightness: int) -> bool:
        """Send IDS dimmable brightness command using native 8-byte payload."""
        if not self._c.is_ethernet_gateway or not self._c._connected or self._c._eth_writer is None:
            _LOGGER.warning(
                "PACKET TX IDS light-set skipped table=0x%02X device=0x%02X reason=transport-not-ready",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        resolved = self._resolve_ids_identity(table_id, device_id, expected_device_type=20)
        if resolved is None:
            _LOGGER.warning(
                "PACKET TX IDS light-set skipped table=0x%02X device=0x%02X reason=identity-not-found",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        key, identity = resolved
        if identity.protocol != 2 or identity.device_type != 20:
            _LOGGER.warning(
                "PACKET TX IDS light-set skipped key=%s reason=identity-mismatch protocol=%d device_type=%d",
                key,
                identity.protocol,
                identity.device_type,
            )
            return False

        clamped_brightness = min(max(brightness, 0), 255)
        mode = 0x00 if clamped_brightness == 0 else 0x01
        payload = bytes([mode, clamped_brightness, 0x00, 0x00, 0xDC, 0x00, 0xDC, 0x00])

        self._set_pending_status_expectation(identity.device_type, device_id, clamped_brightness > 0)

        sent, raw = await self._send_ids_command_with_retry(
            device_id & 0xFF,
            lambda: compose_ids_can_extended_wire_frame(
                message_type=0x82,
                source_address=self._ids_controller_source_address,
                target_address=device_id & 0xFF,
                message_data=0x00,
                payload=payload,
            ),
            "light-set",
        )
        if not sent or raw is None:
            self._clear_pending_status_expectation(identity.device_type, device_id)
            return False

        # Optimistic state mirrors BLE behavior and is corrected by incoming status.
        self._ensure_event_store_maps()
        event = DimmableLight(
            table_id=table_id & 0xFF,
            device_id=device_id & 0xFF,
            brightness=clamped_brightness,
            mode=mode,
        )
        self._c.dimmable_lights[key] = event
        self._dispatch_state_event(event)

        _LOGGER.warning(
            "PACKET TX IDS light-set src=0x%02X dst=0x%02X cmd=0x00 mode=0x%02X brightness=%d payload=%s raw=%s",
            self._ids_controller_source_address & 0xFF,
            device_id & 0xFF,
            mode,
            clamped_brightness,
            payload.hex(),
            raw.hex(),
        )
        return True

    async def send_light_effect_command(
        self,
        table_id: int,
        device_id: int,
        mode: int,
        brightness: int,
        duration: int,
        cycle_time1: int,
        cycle_time2: int,
    ) -> bool:
        """Send IDS dimmable effect command (blink/swell) via COMMAND(0x82)."""
        if not self._c.is_ethernet_gateway or not self._c._connected or self._c._eth_writer is None:
            _LOGGER.warning(
                "PACKET TX IDS light-effect skipped table=0x%02X device=0x%02X reason=transport-not-ready",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        resolved = self._resolve_ids_identity(table_id, device_id, expected_device_type=20)
        if resolved is None:
            _LOGGER.warning(
                "PACKET TX IDS light-effect skipped table=0x%02X device=0x%02X reason=identity-not-found",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        key, identity = resolved
        if identity.protocol != 2 or identity.device_type != 20:
            _LOGGER.warning(
                "PACKET TX IDS light-effect skipped key=%s reason=identity-mismatch protocol=%d device_type=%d",
                key,
                identity.protocol,
                identity.device_type,
            )
            return False

        effect_mode = mode & 0xFF
        clamped_brightness = min(max(brightness, 0), 255)
        dur = duration & 0xFF
        ct1 = min(max(cycle_time1, 0), 0xFFFF)
        ct2 = min(max(cycle_time2, 0), 0xFFFF)
        # Dimmable effect payload parity uses little-endian cycle timing fields.
        payload = bytes([
            effect_mode,
            clamped_brightness,
            dur,
            0x00,
            ct1 & 0xFF,
            (ct1 >> 8) & 0xFF,
            ct2 & 0xFF,
            (ct2 >> 8) & 0xFF,
        ])

        self._set_pending_status_expectation(identity.device_type, device_id, clamped_brightness > 0)

        sent, raw = await self._send_ids_command_with_retry(
            device_id & 0xFF,
            lambda: compose_ids_can_extended_wire_frame(
                message_type=0x82,
                source_address=self._ids_controller_source_address,
                target_address=device_id & 0xFF,
                message_data=0x00,
                payload=payload,
            ),
            "light-effect",
        )
        if not sent or raw is None:
            self._clear_pending_status_expectation(identity.device_type, device_id)
            return False

        # Optimistic state mirrors requested brightness/effect mode until status arrives.
        self._ensure_event_store_maps()
        event = DimmableLight(
            table_id=table_id & 0xFF,
            device_id=device_id & 0xFF,
            brightness=clamped_brightness,
            mode=effect_mode,
        )
        self._c.dimmable_lights[key] = event
        self._dispatch_state_event(event)

        _LOGGER.warning(
            "PACKET TX IDS light-effect src=0x%02X dst=0x%02X mode=0x%02X brightness=%d duration=%d ct1=%d ct2=%d payload=%s raw=%s",
            self._ids_controller_source_address & 0xFF,
            device_id & 0xFF,
            effect_mode,
            clamped_brightness,
            dur,
            ct1,
            ct2,
            payload.hex(),
            raw.hex(),
        )
        return True

    async def send_rgb_command(
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
    ) -> bool:
        """Send IDS RGB command as COMMAND(0x82) with native 8-byte payload."""
        if not self._c.is_ethernet_gateway or not self._c._connected or self._c._eth_writer is None:
            _LOGGER.warning(
                "PACKET TX IDS rgb-set skipped table=0x%02X device=0x%02X reason=transport-not-ready",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        resolved = self._resolve_ids_identity(table_id, device_id, expected_device_type=13)
        if resolved is None:
            _LOGGER.warning(
                "PACKET TX IDS rgb-set skipped table=0x%02X device=0x%02X reason=identity-not-found",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        key, identity = resolved
        if identity.protocol != 2 or identity.device_type != 13:
            _LOGGER.warning(
                "PACKET TX IDS rgb-set skipped key=%s reason=identity-mismatch protocol=%d device_type=%d",
                key,
                identity.protocol,
                identity.device_type,
            )
            return False

        mode_byte = mode & 0xFF
        r = min(max(red, 0), 255)
        g = min(max(green, 0), 255)
        b = min(max(blue, 0), 255)
        auto = auto_off & 0xFF
        interval_msb = 0
        interval_lsb = 0

        if mode_byte == 0x02:
            interval_msb = blink_on_interval & 0xFF
            interval_lsb = blink_off_interval & 0xFF
        elif 0x04 <= mode_byte <= 0x08:
            interval = min(max(transition_interval, 0), 0xFFFF)
            interval_msb = (interval >> 8) & 0xFF
            interval_lsb = interval & 0xFF

        payload = bytes([mode_byte, r, g, b, auto, interval_msb, interval_lsb, 0x00])

        self._set_pending_status_expectation(identity.device_type, device_id, mode_byte > 0)

        sent, raw = await self._send_ids_command_with_retry(
            device_id & 0xFF,
            lambda: compose_ids_can_extended_wire_frame(
                message_type=0x82,
                source_address=self._ids_controller_source_address,
                target_address=device_id & 0xFF,
                message_data=0x00,
                payload=payload,
            ),
            "rgb-set",
        )
        if not sent or raw is None:
            self._clear_pending_status_expectation(identity.device_type, device_id)
            return False

        self._ensure_event_store_maps()
        current = self._c.rgb_lights.get(key)
        brightness = current.brightness if current else 255
        if mode_byte == 0x00:
            brightness = 0
        elif mode_byte in {0x01, 0x02}:
            brightness = max(r, g, b)
        event = RgbLight(
            table_id=table_id & 0xFF,
            device_id=device_id & 0xFF,
            mode=mode_byte,
            red=r,
            green=g,
            blue=b,
            brightness=brightness,
        )
        self._c.rgb_lights[key] = event
        self._dispatch_state_event(event)

        _LOGGER.warning(
            "PACKET TX IDS rgb-set src=0x%02X dst=0x%02X mode=0x%02X rgb=(%d,%d,%d) auto_off=%d i1=0x%02X i2=0x%02X payload=%s raw=%s",
            self._ids_controller_source_address & 0xFF,
            device_id & 0xFF,
            mode_byte,
            r,
            g,
            b,
            auto,
            interval_msb,
            interval_lsb,
            payload.hex(),
            raw.hex(),
        )
        return True

    async def send_hvac_command(
        self,
        table_id: int,
        device_id: int,
        heat_mode: int = 0,
        heat_source: int = 0,
        fan_mode: int = 0,
        low_trip_f: int = 65,
        high_trip_f: int = 78,
    ) -> bool:
        """Send IDS HVAC command as COMMAND(0x82) with native 3-byte payload."""
        if not self._c.is_ethernet_gateway or not self._c._connected or self._c._eth_writer is None:
            _LOGGER.warning(
                "PACKET TX IDS hvac-set skipped table=0x%02X device=0x%02X reason=transport-not-ready",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        resolved = self._resolve_ids_identity(table_id, device_id, expected_device_type=16)
        if resolved is None:
            _LOGGER.warning(
                "PACKET TX IDS hvac-set skipped table=0x%02X device=0x%02X reason=identity-not-found",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        key, identity = resolved
        if identity.protocol != 2 or identity.device_type != 16:
            _LOGGER.warning(
                "PACKET TX IDS hvac-set skipped key=%s reason=identity-mismatch protocol=%d device_type=%d",
                key,
                identity.protocol,
                identity.device_type,
            )
            return False

        command_byte = (
            (heat_mode & 0x07)
            | ((heat_source & 0x03) << 4)
            | ((fan_mode & 0x03) << 6)
        )
        low = min(max(low_trip_f, 0), 255)
        high = min(max(high_trip_f, 0), 255)
        payload = bytes([command_byte & 0xFF, low, high])

        sent, raw = await self._send_ids_command_with_retry(
            device_id & 0xFF,
            lambda: compose_ids_can_extended_wire_frame(
                message_type=0x82,
                source_address=self._ids_controller_source_address,
                target_address=device_id & 0xFF,
                message_data=0x00,
                payload=payload,
            ),
            "hvac-set",
        )
        if not sent or raw is None:
            return False

        self._ensure_event_store_maps()
        current = self._c.hvac_zones.get(key)
        event = HvacZone(
            table_id=table_id & 0xFF,
            device_id=device_id & 0xFF,
            heat_mode=command_byte & 0x07,
            heat_source=(command_byte >> 4) & 0x03,
            fan_mode=(command_byte >> 6) & 0x03,
            low_trip_f=low,
            high_trip_f=high,
            zone_status=current.zone_status if current else 0,
            indoor_temp_f=current.indoor_temp_f if current else None,
            outdoor_temp_f=current.outdoor_temp_f if current else None,
            dtc_code=current.dtc_code if current else 0,
        )
        self._c.hvac_zones[key] = event
        if hasattr(self._c, "_hvac_zone_states"):
            self._c._hvac_zone_states[key] = event
        self._dispatch_state_event(event)

        _LOGGER.warning(
            "PACKET TX IDS hvac-set src=0x%02X dst=0x%02X cmd=0x%02X low=%d high=%d payload=%s raw=%s",
            self._ids_controller_source_address & 0xFF,
            device_id & 0xFF,
            command_byte & 0xFF,
            low,
            high,
            payload.hex(),
            raw.hex(),
        )
        return True

    async def send_relay_toggle_command(self, table_id: int, device_id: int, turn_on: bool) -> bool:
        """Send an IDS native COMMAND(0x82) frame for relay on/off.

        Returns True when IDS-native command path was used and written.
        Returns False when prerequisites are missing so callers can fallback.
        """
        if not self._c.is_ethernet_gateway or not self._c._connected or self._c._eth_writer is None:
            _LOGGER.warning(
                "PACKET TX IDS relay-toggle skipped table=0x%02X device=0x%02X reason=transport-not-ready",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        resolved = self._resolve_ids_identity(table_id, device_id, expected_device_type=30)
        if resolved is None:
            _LOGGER.warning(
                "PACKET TX IDS relay-toggle skipped table=0x%02X device=0x%02X reason=identity-not-found",
                table_id & 0xFF,
                device_id & 0xFF,
            )
            return False

        key, identity = resolved
        if identity.protocol != 2 or identity.device_type != 30:
            _LOGGER.warning(
                "PACKET TX IDS relay-toggle skipped key=%s reason=identity-mismatch protocol=%d device_type=%d",
                key,
                identity.protocol,
                identity.device_type,
            )
            return False

        # RelayBasicLatching Type2 parity:
        # Off=commandByte 0x00, On=commandByte 0x01, empty data payload.
        self._set_pending_status_expectation(identity.device_type, device_id, turn_on)
        sent, raw = await self._send_ids_command_with_retry(
            device_id & 0xFF,
            lambda: compose_ids_can_extended_wire_frame(
                message_type=0x82,
                source_address=self._ids_controller_source_address,
                target_address=device_id & 0xFF,
                message_data=0x01 if turn_on else 0x00,
                payload=b"",
            ),
            "relay-toggle",
        )
        if not sent or raw is None:
            self._clear_pending_status_expectation(identity.device_type, device_id)
            return False

        # Optimistic state mirrors BLE behavior and is corrected by incoming status.
        self._ensure_event_store_maps()
        event = RelayStatus(
            table_id=table_id & 0xFF,
            device_id=device_id & 0xFF,
            is_on=turn_on,
            status_byte=0x01 if turn_on else 0x00,
            dtc_code=0,
        )
        self._c.relays[key] = event
        self._dispatch_state_event(event)

        _LOGGER.warning(
            "PACKET TX IDS relay-toggle src=0x%02X dst=0x%02X cmd=0x%02X on=%s raw=%s",
            self._ids_controller_source_address & 0xFF,
            device_id & 0xFF,
            0x01 if turn_on else 0x00,
            turn_on,
            raw.hex(),
        )
        return True

    def cleanup_on_disconnect(self) -> None:
        """Synchronous cleanup used by disconnect callback path."""
        if self._c._eth_writer is not None:
            self._c._eth_writer.close()
        self._c._eth_reader = None
        self._c._eth_writer = None
        if self._c._ethernet_reader_task and not self._c._ethernet_reader_task.done():
            self._c._ethernet_reader_task.cancel()
        self._c._ethernet_reader_task = None
        self._ids_pending_status_expectations.clear()

    def handle_frame(self, frame: bytes) -> bool:
        """Handle IDS-CAN/Ethernet command envelopes and cmdId correlation.

        Returns True when the frame is fully consumed and should not be parsed
        further by the coordinator.
        """
        if not self._c.is_ethernet_gateway:
            return False

        if not frame:
            return True

        event_type = frame[0] & 0xFF
        response_types = {0x01, 0x02, 0x81, 0x82}
        looks_like_myrvlink_cmd = (
            len(frame) >= 4
            and event_type == 0x02
            and (frame[3] & 0xFF) in response_types
        )

        wire = parse_ids_can_wire_frame(frame)
        if wire is not None and not looks_like_myrvlink_cmd:
            self._c._cmd_correlation_stats["ids_can_wire_frames_seen"] = (
                self._c._cmd_correlation_stats.get("ids_can_wire_frames_seen", 0) + 1
            )
            key = f"ids_can_type_{wire.message_type:02x}"
            self._c._cmd_correlation_stats[key] = self._c._cmd_correlation_stats.get(key, 0) + 1
            semantic = decode_ids_can_payload(wire)
            if semantic is not None:
                if wire.is_extended and semantic.kind in {"request", "command"}:
                    self._ids_controller_source_address = wire.source_address & 0xFF
                semantic_key = f"ids_can_semantic_{semantic.kind}"
                self._c._cmd_correlation_stats[semantic_key] = (
                    self._c._cmd_correlation_stats.get(semantic_key, 0) + 1
                )
                if semantic.kind == "network":
                    mac = semantic.fields.get("mac")
                    if isinstance(mac, str) and mac:
                        self._ids_source_product_mac[wire.source_address & 0xFF] = mac
                elif semantic.kind == "response":
                    if wire.is_extended and wire.message_data == 0x42 and len(wire.payload) == 6:
                        session_id = int.from_bytes(wire.payload[0:2], "big")
                        if session_id == _IDS_SESSION_REMOTE_CONTROL:
                            target = wire.source_address & 0xFF
                            requested_at = self._ids_session_seed_requested_at.get(target)
                            # Ignore stale seeds if no matching in-flight request exists.
                            if requested_at is None or (time.monotonic() - requested_at) > 2.0:
                                return True
                            self._ids_session_seed_requested_at.pop(target, None)
                            seed = int.from_bytes(wire.payload[2:6], "big")
                            key = self._ids_encrypt_session_seed(seed)
                            key_payload = (
                                _IDS_SESSION_REMOTE_CONTROL.to_bytes(2, "big")
                                + key.to_bytes(4, "big")
                            )
                            self._c.hass.async_create_task(
                                self._send_ids_request(target, 0x43, key_payload)
                            )
                    elif wire.is_extended and wire.message_data == 0x43 and len(wire.payload) == 2:
                        session_id = int.from_bytes(wire.payload[0:2], "big")
                        if session_id == _IDS_SESSION_REMOTE_CONTROL:
                            target = wire.source_address & 0xFF
                            self._ids_session_opened_at[target] = time.monotonic()
                            self._ids_session_results[target] = True
                            self._ids_session_last_heartbeat_at[target] = time.monotonic()
                            self._ids_active_session_target = target
                            waiter = self._ids_session_waiters.get(target)
                            if waiter is not None:
                                waiter.set()
                            _LOGGER.warning(
                                "PACKET RX IDS session-opened src=0x%02X session=0x%04X",
                                target,
                                session_id,
                            )
                    elif wire.is_extended and wire.message_data in {0x44, 0x45} and len(wire.payload) == 3:
                        session_id = int.from_bytes(wire.payload[0:2], "big")
                        if session_id == _IDS_SESSION_REMOTE_CONTROL:
                            target = wire.source_address & 0xFF
                            reason = wire.payload[2] & 0xFF
                            self._ids_session_opened_at.pop(target, None)
                            self._ids_session_last_heartbeat_at.pop(target, None)
                            self._ids_session_results[target] = False
                            if self._ids_active_session_target == target:
                                self._ids_active_session_target = None
                            _LOGGER.warning(
                                "PACKET RX IDS session-closed src=0x%02X req=0x%02X reason=0x%02X",
                                target,
                                wire.message_data & 0xFF,
                                reason,
                            )
                            waiter = self._ids_session_waiters.get(target)
                            if waiter is not None:
                                waiter.set()
                    status_code = semantic.fields.get("status_code")
                    if isinstance(status_code, int) and status_code != 0x00:
                        status_name = str(semantic.fields.get("status_name", "UNKNOWN"))
                        request_code = int(semantic.fields.get("request_code", 0))
                        request_name = str(semantic.fields.get("request_name", "UNKNOWN"))
                        src_addr = wire.source_address & 0xFF
                        now = time.monotonic()
                        target_seen_at = self._ids_recent_command_targets.get(src_addr)
                        likely_related_to_recent_tx = (
                            target_seen_at is not None and (now - target_seen_at) <= 2.5
                        )
                        log_level = logging.WARNING if likely_related_to_recent_tx else logging.DEBUG
                        _LOGGER.log(
                            log_level,
                            "PACKET RX IDS response-status src=0x%02X dst=0x%02X req=0x%02X(%s) status=0x%02X(%s) related_to_recent_tx=%s payload=%s",
                            src_addr,
                            wire.target_address if wire.target_address is not None else 0,
                            request_code & 0xFF,
                            request_name,
                            status_code & 0xFF,
                            status_name,
                            likely_related_to_recent_tx,
                            wire.payload.hex(),
                        )
                        if request_code in {0x42, 0x43}:
                            src_addr = wire.source_address & 0xFF
                            self._ids_session_last_status_code[src_addr] = status_code & 0xFF
                            # Only fail an in-flight open attempt. Ignore late status errors
                            # that arrive after we already marked session-open success.
                            if self._ids_session_results.get(src_addr) is None:
                                self._ids_session_opened_at.pop(src_addr, None)
                                self._ids_session_last_heartbeat_at.pop(src_addr, None)
                                self._ids_session_results[src_addr] = False
                                waiter = self._ids_session_waiters.get(src_addr)
                                if waiter is not None:
                                    waiter.set()
                        if status_code == 0x0E:
                            _LOGGER.warning(
                                "PACKET RX IDS SESSION_NOT_OPEN observed; command path may require IDS session open/auth before COMMAND(0x82)."
                            )
                elif semantic.kind == "device_id":
                    self._handle_ids_device_id(wire.source_address, semantic.fields)
                elif semantic.kind == "device_status":
                    self._handle_ids_device_status(wire.source_address, wire.payload)
            return True

        def _resolve_pending_cmd_id(raw_cmd_id: int | None) -> int | None:
            """Match cmdId against pending maps with LE/BE tolerance."""
            if raw_cmd_id is None:
                return None
            if (
                raw_cmd_id in self._c._pending_get_devices_cmdids
                or raw_cmd_id in self._c._pending_metadata_cmdids
            ):
                return raw_cmd_id
            swapped = ((raw_cmd_id & 0xFF) << 8) | ((raw_cmd_id >> 8) & 0xFF)
            if (
                swapped in self._c._pending_get_devices_cmdids
                or swapped in self._c._pending_metadata_cmdids
            ):
                return swapped
            return raw_cmd_id

        markerless_cmd_id = (
            (frame[0] & 0xFF) | ((frame[1] & 0xFF) << 8)
            if len(frame) >= 3
            else None
        )
        markerless_cmd_id = _resolve_pending_cmd_id(markerless_cmd_id)
        pending_cmd_ids = set(self._c._pending_get_devices_cmdids) | set(self._c._pending_metadata_cmdids)
        is_markerless_command_frame = (
            markerless_cmd_id is not None
            and len(frame) >= 3
            and (frame[2] & 0xFF) in response_types
            and markerless_cmd_id in pending_cmd_ids
        )
        is_standard_command_frame = event_type == 0x02
        is_command_frame = is_standard_command_frame or is_markerless_command_frame
        if is_command_frame:
            self._c._cmd_correlation_stats["ids_command_candidates_seen"] = (
                self._c._cmd_correlation_stats.get("ids_command_candidates_seen", 0) + 1
            )
            _LOGGER.warning(
                "PACKET RX ETH cmd-candidate standard=%s markerless=%s pending_get=%d pending_meta=%d raw=%s",
                is_standard_command_frame,
                is_markerless_command_frame,
                len(self._c._pending_get_devices_cmdids),
                len(self._c._pending_metadata_cmdids),
                frame.hex(),
            )
        elif event_type == 0x02 and len(frame) >= 4:
            self._c._cmd_correlation_stats["ids_command_candidates_unmatched"] = (
                self._c._cmd_correlation_stats.get("ids_command_candidates_unmatched", 0) + 1
            )
            _LOGGER.warning("PACKET RX ETH unmatched evt=0x02 frame=%s", frame.hex())

        family = self._c._classify_frame_family(frame)
        self._c._frame_family_stats[family] = self._c._frame_family_stats.get(family, 0) + 1

        if not is_command_frame:
            # Keep old behavior: event 0x02 frames that are not explicit command
            # responses are ignored on Ethernet.
            return event_type == 0x02

        cmd_id: int
        response_type: int | None
        command_frame = frame
        if is_standard_command_frame:
            cmd_id = (frame[1] & 0xFF) | ((frame[2] & 0xFF) << 8)
            cmd_id = _resolve_pending_cmd_id(cmd_id) or cmd_id
            response_type = frame[3] & 0xFF if len(frame) >= 4 else None
        else:
            cmd_id = markerless_cmd_id if markerless_cmd_id is not None else 0
            response_type = frame[2] & 0xFF
            command_frame = bytes([frame[0], frame[1], 0x02, frame[2]]) + frame[3:]

        if response_type is None:
            return True

        _LOGGER.warning(
            "PACKET RX ETH cmd_id=0x%04X response_type=0x%02X frame=%s",
            cmd_id & 0xFFFF,
            response_type & 0xFF,
            command_frame.hex(),
        )

        if response_type == 0x81:
            completed_get_devices_table = self._c._pending_get_devices_cmdids.pop(cmd_id, None)
            self._c._pending_get_devices_sent_at.pop(cmd_id, None)
            if completed_get_devices_table is not None:
                self._c._cmd_correlation_stats["get_devices_completed"] += 1
                self._c._get_devices_loaded_tables.add(completed_get_devices_table)
                _LOGGER.warning(
                    "PACKET GetDevices completed cmd_id=0x%04X table=%d",
                    cmd_id & 0xFFFF,
                    completed_get_devices_table,
                )
                if (
                    self._c._supports_metadata_requests
                    and completed_get_devices_table not in self._c._metadata_loaded_tables
                    and completed_get_devices_table not in self._c._metadata_requested_tables
                ):
                    self._c.hass.async_create_task(
                        self._c._send_metadata_request(completed_get_devices_table)
                    )
                return True

            completed_table = self._c._pending_metadata_cmdids.pop(cmd_id, None)
            self._c._pending_metadata_sent_at.pop(cmd_id, None)
            if completed_table is not None and len(command_frame) >= 8:
                response_crc = int.from_bytes(command_frame[4:8], "big")
                response_count = command_frame[8] & 0xFF if len(command_frame) >= 9 else None
                staged_entries = self._c._pending_metadata_entries.pop(cmd_id, {})
                staged_count = len(staged_entries)
                expected_crc = (
                    self._c.gateway_info.device_metadata_table_crc
                    if self._c.gateway_info is not None
                    else 0
                )
                if expected_crc != 0 and response_crc != expected_crc:
                    self._c._cmd_correlation_stats["metadata_commit_crc_mismatch"] += 1
                    self._c._metadata_loaded_tables.discard(completed_table)
                    self._c._metadata_requested_tables.discard(completed_table)
                    self._c._last_metadata_crc = None
                elif response_count is not None and response_count != staged_count:
                    self._c._cmd_correlation_stats["metadata_commit_count_mismatch"] += 1
                    self._c._metadata_loaded_tables.discard(completed_table)
                    self._c._metadata_requested_tables.discard(completed_table)
                    self._c._last_metadata_crc = None
                else:
                    for meta in staged_entries.values():
                        self._c._process_metadata(meta)
                    self._c._metadata_loaded_tables.add(completed_table)
                    self._c._metadata_rejected_tables.discard(completed_table)
                    self._c._last_metadata_crc = response_crc
                    self._c._cmd_correlation_stats["metadata_commit_success"] += 1
            return True

        if response_type == 0x82:
            rejected_table = self._c._pending_metadata_cmdids.pop(cmd_id, None)
            self._c._pending_metadata_sent_at.pop(cmd_id, None)
            self._c._pending_metadata_entries.pop(cmd_id, None)
            if rejected_table is not None:
                error_code = command_frame[4] & 0xFF if len(command_frame) >= 5 else -1
                if error_code == 0x0F:
                    self._c._metadata_rejected_tables.discard(rejected_table)
                    retry_count = self._c._metadata_retry_counts.get(rejected_table, 0) + 1
                    self._c._metadata_retry_counts[rejected_table] = retry_count
                    self._c._cmd_correlation_stats["metadata_retry_scheduled"] += 1
                    self._c.hass.async_create_task(
                        self._c._retry_metadata_after_rejection(rejected_table)
                    )
                else:
                    self._c._metadata_requested_tables.discard(rejected_table)
                    self._c._metadata_rejected_tables.add(rejected_table)
            else:
                gd_table = self._c._pending_get_devices_cmdids.pop(cmd_id, None)
                self._c._pending_get_devices_sent_at.pop(cmd_id, None)
                if gd_table is not None:
                    self._c._cmd_correlation_stats["get_devices_rejected"] += 1
                    self._c._get_devices_loaded_tables.discard(gd_table)
                else:
                    self._c._cmd_correlation_stats["command_error_unknown"] += 1
                    self._c._bump_unknown_cmd_count(cmd_id)
            return True

        if response_type == 0x01 and len(frame) >= 3:
            identities = parse_get_devices_response(command_frame)
            if identities:
                self._c._cmd_correlation_stats["get_devices_identity_rows"] += len(identities)
                _LOGGER.warning(
                    "PACKET GetDevices identities parsed rows=%d cmd_id=0x%04X",
                    len(identities),
                    cmd_id & 0xFFFF,
                )
                for identity in identities:
                    key = f"{identity.table_id:02x}:{identity.device_id:02x}"
                    self._c._device_identities[key] = identity
                    self._c._apply_external_name(key, identity)

                if cmd_id in self._c._pending_get_devices_cmdids:
                    return True

                # IDS-CAN Ethernet gateways may rewrite/omit client cmdId values
                # in command responses. If payload decodes as valid identity rows,
                # trust the payload and advance pending state using table-id evidence.
                self._c._cmd_correlation_stats["get_devices_identity_rows_fallback"] = (
                    self._c._cmd_correlation_stats.get("get_devices_identity_rows_fallback", 0)
                    + len(identities)
                )
                inferred_table_id = identities[0].table_id & 0xFF
                matched_cmd_id = next(
                    (
                        pending_cmd_id
                        for pending_cmd_id, pending_table_id in self._c._pending_get_devices_cmdids.items()
                        if pending_table_id == inferred_table_id
                    ),
                    None,
                )
                if matched_cmd_id is None and len(self._c._pending_get_devices_cmdids) == 1:
                    matched_cmd_id = next(iter(self._c._pending_get_devices_cmdids))

                if matched_cmd_id is not None:
                    self._c._pending_get_devices_cmdids.pop(matched_cmd_id, None)
                    self._c._pending_get_devices_sent_at.pop(matched_cmd_id, None)
                    self._c._get_devices_loaded_tables.add(inferred_table_id)
                    self._c._cmd_correlation_stats["get_devices_completed_fallback"] = (
                        self._c._cmd_correlation_stats.get("get_devices_completed_fallback", 0)
                        + 1
                    )
                    _LOGGER.warning(
                        "PACKET GetDevices fallback completion inferred_table=%d matched_pending_cmd_id=0x%04X",
                        inferred_table_id,
                        matched_cmd_id & 0xFFFF,
                    )
                return True
            self._c._cmd_correlation_stats["get_devices_identity_parse_empty"] = (
                self._c._cmd_correlation_stats.get("get_devices_identity_parse_empty", 0) + 1
            )
            _LOGGER.warning(
                "PACKET GetDevices identity parse empty cmd_id=0x%04X len=%d frame=%s",
                cmd_id & 0xFFFF,
                len(command_frame),
                command_frame.hex(),
            )

            if cmd_id not in self._c._pending_metadata_cmdids:
                self._c._cmd_correlation_stats["metadata_success_multi_discarded_unknown"] += 1
                self._c._bump_unknown_cmd_count(cmd_id)
                return True

            self._c._cmd_correlation_stats["metadata_success_multi_accepted"] += 1
            staged = self._c._pending_metadata_entries.setdefault(cmd_id, {})
            added = 0
            try:
                parsed_metadata = parse_metadata_response(command_frame)
            except Exception:
                self._c._cmd_correlation_stats["metadata_parse_errors"] += 1
                return True

            for meta in parsed_metadata:
                key = f"{meta.table_id:02x}:{meta.device_id:02x}"
                if key not in staged:
                    added += 1
                staged[key] = meta
            if added:
                self._c._cmd_correlation_stats["metadata_entries_staged"] += added
            return True

        return True
