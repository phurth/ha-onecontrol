"""Runtime-focused tests for IDS-CAN command-frame handling."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.ha_onecontrol.protocol.cobs import CobsByteDecoder
from custom_components.ha_onecontrol.protocol.events import DeviceIdentity, DeviceMetadata, HvacZone
from custom_components.ha_onecontrol.protocol.ids_can_wire import parse_ids_can_wire_frame
from custom_components.ha_onecontrol.runtime.ids_can_runtime import IdsCanRuntime


class _FakeHass:
    def __init__(self) -> None:
        self.tasks = []

    def async_create_task(self, coro):
        self.tasks.append(coro)
        # These tests don't need task execution; avoid un-awaited coroutine warnings.
        try:
            coro.close()
        except Exception:
            pass
        return None


class _FakeEthWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.drains = 0

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        self.drains += 1


def _base_state() -> SimpleNamespace:
    stats = {
        "get_devices_identity_rows": 0,
        "metadata_success_multi_discarded_get_devices": 0,
        "metadata_success_multi_discarded_unknown": 0,
        "metadata_success_multi_accepted": 0,
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
        "get_devices_identity_rows_fallback": 0,
        "pending_get_devices_peak": 0,
        "frame_parse_errors": 0,
        "pending_cmdid_pruned": 0,
        "unknown_cmdids_pruned": 0,
        "external_names_applied": 0,
    }

    return SimpleNamespace(
        is_ethernet_gateway=True,
        _connected=True,
        _eth_writer=_FakeEthWriter(),
        _last_ethernet_tx_time=0.0,
        _classify_frame_family=lambda _: "myrvlink_command",
        _frame_family_stats={"myrvlink_command": 0},
        _pending_get_devices_cmdids={0x1234: 0x03},
        _pending_get_devices_sent_at={0x1234: 1.0},
        _pending_metadata_cmdids={},
        _pending_metadata_sent_at={},
        _pending_metadata_entries={},
        _metadata_loaded_tables=set(),
        _metadata_requested_tables=set(),
        _metadata_rejected_tables=set(),
        _metadata_retry_counts={},
        _get_devices_loaded_tables=set(),
        _cmd_correlation_stats=stats,
        _unknown_command_counts={},
        _device_identities={},
        _supports_metadata_requests=False,
        _process_metadata=lambda *_: None,
        _apply_external_name=lambda *_: None,
        _bump_unknown_cmd_count=lambda _: 1,
        _last_metadata_crc=None,
        gateway_info=None,
        hass=_FakeHass(),
        _send_metadata_request=lambda *_: None,
        _retry_metadata_after_rejection=lambda *_: None,
        relays={},
        dimmable_lights={},
        rgb_lights={},
        covers={},
        hvac_zones={},
        tanks={},
        generators={},
        hour_meters={},
        _select_get_devices_table_id=lambda: 0x03,
        _dispatch_event_update=lambda *_: None,
    )


def test_ids_runtime_accepts_markerless_get_devices_successmulti() -> None:
    """Markerless [cmdL cmdH resp] envelopes are accepted for pending cmdIds."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # [cmdL][cmdH][0x01(resp)][table][start][count][protocol][size][payload(10)]
    frame = bytes(
        [
            0x34,
            0x12,
            0x01,
            0x03,
            0x00,
            0x01,
            0x01,
            0x0A,
            0x14,
            0x01,
            0x00,
            0x67,
            0x00,
            0x00,
            0x00,
            0x08,
            0xE9,
            0xBC,
        ]
    )

    consumed = runtime.handle_frame(frame)

    assert consumed is True
    assert state._cmd_correlation_stats["get_devices_identity_rows"] == 1
    assert "03:00" in state._device_identities


def test_ids_runtime_ignores_unknown_event02_non_command() -> None:
    """Ethernet event 0x02 frames without valid command envelope are consumed/ignored."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # event=0x02 but no valid responseType/cmd correlation
    frame = bytes([0x02, 0xAA, 0xBB, 0xCC, 0xDD])

    consumed = runtime.handle_frame(frame)

    assert consumed is True
    assert state._cmd_correlation_stats["get_devices_identity_rows"] == 0


def test_ids_runtime_get_devices_successcomplete_marks_table_loaded() -> None:
    """0x81 completion for pending GetDevices should mark table loaded."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    frame = bytes([0x02, 0x34, 0x12, 0x81, 0x00, 0x00, 0x00, 0x00])
    consumed = runtime.handle_frame(frame)

    assert consumed is True
    assert state._cmd_correlation_stats["get_devices_completed"] == 1
    assert 0x03 in state._get_devices_loaded_tables
    assert 0x1234 not in state._pending_get_devices_cmdids


def test_ids_runtime_get_devices_successcomplete_matches_swapped_cmdid() -> None:
    """0x81 completion with BE cmdId bytes should still match pending LE cmdId."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # Pending cmdId is 0x1234, but frame reports bytes as 12 34 (raw cmd=0x3412).
    frame = bytes([0x02, 0x12, 0x34, 0x81, 0x00, 0x00, 0x00, 0x00])
    consumed = runtime.handle_frame(frame)

    assert consumed is True
    assert state._cmd_correlation_stats["get_devices_completed"] == 1
    assert 0x03 in state._get_devices_loaded_tables
    assert 0x1234 not in state._pending_get_devices_cmdids


def test_ids_runtime_get_devices_successmulti_fallbacks_when_cmdid_unmatched() -> None:
    """Valid identity rows should be accepted even when gateway rewrites cmdId."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # cmdId bytes (0x5678) do not match pending (0x1234), but payload is valid GetDevices row.
    frame = bytes(
        [
            0x02,
            0x78,
            0x56,
            0x01,
            0x03,
            0x00,
            0x01,
            0x01,
            0x0A,
            0x14,
            0x01,
            0x00,
            0x67,
            0x00,
            0x00,
            0x00,
            0x08,
            0xE9,
            0xBC,
        ]
    )

    consumed = runtime.handle_frame(frame)

    assert consumed is True
    assert state._cmd_correlation_stats["get_devices_identity_rows"] == 1
    assert state._cmd_correlation_stats["get_devices_identity_rows_fallback"] == 1
    assert state._cmd_correlation_stats["get_devices_completed_fallback"] == 1
    assert "03:00" in state._device_identities
    assert 0x03 in state._get_devices_loaded_tables
    assert 0x1234 not in state._pending_get_devices_cmdids


def test_ids_runtime_metadata_completion_commits_on_crc_and_count_match() -> None:
    """Metadata 0x81 completion commits staged entries when CRC/count match."""
    state = _base_state()
    state._pending_get_devices_cmdids = {}
    state._pending_get_devices_sent_at = {}
    state._pending_metadata_cmdids = {0x2222: 0x03}
    state._pending_metadata_sent_at = {0x2222: 1.0}
    state._pending_metadata_entries = {
        0x2222: {
            "03:10": DeviceMetadata(table_id=0x03, device_id=0x10, function_name=1, function_instance=1)
        }
    }
    processed = []
    state._process_metadata = processed.append
    state.gateway_info = SimpleNamespace(device_metadata_table_crc=0x11223344)

    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    frame = bytes([0x02, 0x22, 0x22, 0x81, 0x11, 0x22, 0x33, 0x44, 0x01])
    consumed = runtime.handle_frame(frame)

    assert consumed is True
    assert state._cmd_correlation_stats["metadata_commit_success"] == 1
    assert len(processed) == 1
    assert 0x03 in state._metadata_loaded_tables
    assert state._last_metadata_crc == 0x11223344


def test_ids_runtime_metadata_completion_rejects_crc_mismatch() -> None:
    """Metadata 0x81 completion with CRC mismatch should be discarded."""
    state = _base_state()
    state._pending_get_devices_cmdids = {}
    state._pending_get_devices_sent_at = {}
    state._pending_metadata_cmdids = {0x3333: 0x03}
    state._pending_metadata_sent_at = {0x3333: 1.0}
    state._pending_metadata_entries = {
        0x3333: {
            "03:11": DeviceMetadata(table_id=0x03, device_id=0x11, function_name=1, function_instance=1)
        }
    }
    processed = []
    state._process_metadata = processed.append
    state._metadata_loaded_tables = {0x03}
    state._metadata_requested_tables = {0x03}
    state.gateway_info = SimpleNamespace(device_metadata_table_crc=0xDEADBEEF)

    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    frame = bytes([0x02, 0x33, 0x33, 0x81, 0x11, 0x22, 0x33, 0x44, 0x01])
    consumed = runtime.handle_frame(frame)

    assert consumed is True
    assert state._cmd_correlation_stats["metadata_commit_crc_mismatch"] == 1
    assert state._cmd_correlation_stats["metadata_commit_success"] == 0
    assert len(processed) == 0
    assert 0x03 not in state._metadata_loaded_tables


def test_ids_runtime_metadata_rejection_0x0f_schedules_retry() -> None:
    """Metadata 0x82 with errorCode 0x0F should schedule a retry like native code."""
    state = _base_state()
    state._pending_get_devices_cmdids = {}
    state._pending_get_devices_sent_at = {}
    state._pending_metadata_cmdids = {0x4444: 0x05}
    state._pending_metadata_sent_at = {0x4444: 1.0}
    state._pending_metadata_entries = {0x4444: {}}
    scheduled = []

    def _retry_metadata_after_rejection(table_id: int):
        scheduled.append(table_id)

        async def _noop():
            return None

        return _noop()

    state._retry_metadata_after_rejection = _retry_metadata_after_rejection

    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    frame = bytes([0x02, 0x44, 0x44, 0x82, 0x0F])
    consumed = runtime.handle_frame(frame)

    assert consumed is True
    assert state._cmd_correlation_stats["metadata_retry_scheduled"] == 1
    assert state._metadata_retry_counts.get(0x05) == 1
    assert scheduled == [0x05]


def test_ids_runtime_force_reconnect_closes_and_dispatches_disconnect() -> None:
    """force_reconnect should close transport and notify disconnect handler."""
    state = _base_state()
    close_calls: list[str] = []
    disconnect_calls: list[tuple[str, str]] = []

    async def _close() -> None:
        close_calls.append("closed")

    def _handle_disconnect(transport: str, reason: str) -> None:
        disconnect_calls.append((transport, reason))

    state._connected = True
    state._handle_transport_disconnect = _handle_disconnect
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]
    runtime.close_transport = _close  # type: ignore[method-assign]

    import asyncio

    asyncio.run(runtime.force_reconnect("stale heartbeat"))

    assert close_calls == ["closed"]
    assert disconnect_calls == [("ethernet", "stale heartbeat")]


def test_ids_runtime_pre_gateway_heartbeat_sends_keepalive_and_get_devices() -> None:
    """pre-gateway heartbeat should keep transport alive and send GetDevices when table is known."""
    state = _base_state()
    state._pending_get_devices_cmdids = {}
    sent: list[bytes] = []
    recorded: list[tuple[int, int]] = []

    async def _send_keepalive(_: float) -> None:
        return

    class _Cmd:
        @staticmethod
        def build_get_devices(table_id: int) -> bytes:
            return bytes([0x34, 0x12, table_id])

    async def _send_command(cmd: bytes) -> None:
        sent.append(bytes(cmd))

    def _record(cmd_id: int, table_id: int) -> None:
        recorded.append((cmd_id, table_id))

    state._connected = True
    state.gateway_info = None
    state._cmd = _Cmd()
    state._select_get_devices_table_id = lambda: 0x03
    state.async_send_command = _send_command
    state._record_pending_get_devices_cmd = _record

    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]
    runtime.send_transport_keepalive = _send_keepalive  # type: ignore[method-assign]

    import asyncio

    asyncio.run(runtime.heartbeat_pre_gateway_cycle(3.0))

    assert sent == [bytes([0x34, 0x12, 0x03])]
    assert recorded == [(0x1234, 0x03)]


def test_ids_runtime_device_id_bootstraps_identity_and_entity_state() -> None:
    """DEVICE_ID semantic frames should seed identity and default state for entity creation."""
    state = _base_state()
    dispatched = []
    state._dispatch_event_update = lambda event: dispatched.append(event)
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # NETWORK frame first, so source->product MAC is known.
    network_frame = bytes.fromhex("0800cd031400000008e9cd")
    assert runtime.handle_frame(network_frame) is True

    # DEVICE_ID for source 0xCD, device_type=20 (dimmable light).
    # payload: product_id=0x0067 product_instance=0xCD device_type=0x14
    # function_name=0x0027 (39), dev_inst=4 fn_inst=0 caps=0xD0
    device_id_frame = bytes.fromhex("0802cd0067cd14002740d0")
    assert runtime.handle_frame(device_id_frame) is True

    key = "03:cd"
    assert key in state._device_identities
    identity = state._device_identities[key]
    assert identity.device_type == 20
    assert identity.device_instance == 4
    assert identity.product_id == 103
    assert identity.product_mac == "00000008E9CD"
    assert key in state.dimmable_lights
    assert len(dispatched) >= 1


def test_ids_runtime_device_status_updates_bootstrapped_dimmable_state() -> None:
    """DEVICE_STATUS should update coordinator light state after DEVICE_ID bootstrap."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800cd031400000008e9cd"))
    runtime.handle_frame(bytes.fromhex("0802cd0067cd14002740d0"))

    # DEVICE_STATUS for source 0xCD: status0=0x81 (on), payload[3]=0x1F brightness.
    status_frame = bytes.fromhex("0603cd81ff001f0000")
    assert runtime.handle_frame(status_frame) is True

    light = state.dimmable_lights.get("03:cd")
    assert light is not None
    assert light.mode == 1
    assert light.brightness == 0x1F


def test_ids_runtime_dimmable_stale_status_is_suppressed_during_settle_window() -> None:
    """Contradictory DEVICE_STATUS immediately after command should be ignored."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800cd031400000008e9cd"))
    runtime.handle_frame(bytes.fromhex("0802cd0067cd14002740d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCD] = time.monotonic()
    used = asyncio.run(runtime.send_light_toggle_command(0x03, 0xCD, True))
    assert used is True

    # Immediate stale status reports off (status0 bit0 == 0).
    stale_off_status = bytes.fromhex("0603cd000000000000")
    assert runtime.handle_frame(stale_off_status) is True

    light = state.dimmable_lights.get("03:cd")
    assert light is not None
    assert light.mode == 1
    assert light.brightness == 255


def test_ids_runtime_dimmable_stale_status_is_applied_after_settle_timeout() -> None:
    """Contradictory status should apply once settle window has expired."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800cd031400000008e9cd"))
    runtime.handle_frame(bytes.fromhex("0802cd0067cd14002740d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCD] = time.monotonic()
    used = asyncio.run(runtime.send_light_toggle_command(0x03, 0xCD, True))
    assert used is True

    expectation = runtime._ids_pending_status_expectations.get((20, 0xCD))
    assert expectation is not None
    desired_on, _expires_at = expectation
    runtime._ids_pending_status_expectations[(20, 0xCD)] = (desired_on, time.monotonic() - 0.01)

    stale_off_status = bytes.fromhex("0603cd000000000000")
    assert runtime.handle_frame(stale_off_status) is True

    light = state.dimmable_lights.get("03:cd")
    assert light is not None
    assert light.mode == 0
    assert light.brightness == 0


def test_ids_runtime_send_light_toggle_command_writes_extended_ids_frame() -> None:
    """send_light_toggle_command should emit REQUEST(0x80) then COMMAND(0x82)."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # Seed identity map for dimmable light key used by command path.
    runtime.handle_frame(bytes.fromhex("0800cd031400000008e9cd"))
    runtime.handle_frame(bytes.fromhex("0802cd0067cd14002740d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCD] = time.monotonic()

    used = asyncio.run(runtime.send_light_toggle_command(0x03, 0xCD, True))
    assert used is True

    writer = state._eth_writer
    assert writer.drains >= 1
    assert len(writer.writes) >= 1

    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.is_extended is True
    assert parsed.message_type == 0x82
    assert parsed.source_address == 0x3A
    assert parsed.target_address == 0xCD
    assert parsed.message_data == 0x00
    assert parsed.payload == bytes.fromhex("01ff0000dc00dc00")

    light = state.dimmable_lights.get("03:cd")
    assert light is not None
    assert light.mode == 1
    assert light.brightness == 255


def test_ids_runtime_send_relay_toggle_command_writes_extended_ids_frame() -> None:
    """send_relay_toggle_command should emit REQUEST(0x80) then COMMAND(0x82)."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # Seed identity map for relay key used by command path.
    runtime.handle_frame(bytes.fromhex("0800da031400000008e9bc"))
    runtime.handle_frame(bytes.fromhex("0802da0067da1e002250d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xDA] = time.monotonic()

    used = asyncio.run(runtime.send_relay_toggle_command(0x03, 0xDA, True))
    assert used is True

    writer = state._eth_writer
    assert writer.drains >= 1
    assert len(writer.writes) >= 1

    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.is_extended is True
    assert parsed.message_type == 0x82
    assert parsed.source_address == 0x3A
    assert parsed.target_address == 0xDA
    assert parsed.message_data == 0x01
    assert parsed.payload == b""

    relay = state.relays.get("03:da")
    assert relay is not None
    assert relay.is_on is True
    assert relay.status_byte == 0x01


def test_ids_runtime_send_light_brightness_command_writes_brightness_payload() -> None:
    """send_light_brightness_command should preserve requested brightness in payload."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800cd031400000008e9cd"))
    runtime.handle_frame(bytes.fromhex("0802cd0067cd14002740d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCD] = time.monotonic()

    used = asyncio.run(runtime.send_light_brightness_command(0x03, 0xCD, 82))
    assert used is True

    writer = state._eth_writer
    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.message_type == 0x82
    assert parsed.target_address == 0xCD
    assert parsed.message_data == 0x00
    assert parsed.payload == bytes.fromhex("01520000dc00dc00")

    light = state.dimmable_lights.get("03:cd")
    assert light is not None
    assert light.mode == 1
    assert light.brightness == 82


def test_ids_runtime_send_light_effect_command_writes_effect_payload() -> None:
    """send_light_effect_command should emit effect mode and timing payload fields."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800cd031400000008e9cd"))
    runtime.handle_frame(bytes.fromhex("0802cd0067cd14002740d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCD] = time.monotonic()

    used = asyncio.run(
        runtime.send_light_effect_command(
            0x03,
            0xCD,
            mode=0x02,
            brightness=128,
            duration=5,
            cycle_time1=1055,
            cycle_time2=2447,
        )
    )
    assert used is True

    writer = state._eth_writer
    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.message_type == 0x82
    assert parsed.target_address == 0xCD
    assert parsed.message_data == 0x00
    # mode=0x02, brightness=0x80, duration=0x05, reserved=0x00,
    # cycle1=1055=>0x041F (little-endian 1f04), cycle2=2447=>0x098F (8f09)
    assert parsed.payload == bytes.fromhex("028005001f048f09")

    light = state.dimmable_lights.get("03:cd")
    assert light is not None
    assert light.mode == 0x02
    assert light.brightness == 128


def test_ids_runtime_light_toggle_uses_device_id_fallback_when_table_mismatch() -> None:
    """Light toggle should still send when requested table id differs but device id/type match."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800cd031400000008e9cd"))
    runtime.handle_frame(bytes.fromhex("0802cd0067cd14002740d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCD] = time.monotonic()

    used = asyncio.run(runtime.send_light_toggle_command(0x81, 0xCD, True))
    assert used is True

    writer = state._eth_writer
    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.target_address == 0xCD
    assert parsed.message_type == 0x82


def test_ids_runtime_relay_toggle_uses_device_id_fallback_when_table_mismatch() -> None:
    """Relay toggle should still send when requested table id differs but device id/type match."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800da031400000008e9bc"))
    runtime.handle_frame(bytes.fromhex("0802da0067da1e002250d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xDA] = time.monotonic()

    used = asyncio.run(runtime.send_relay_toggle_command(0x81, 0xDA, True))
    assert used is True

    writer = state._eth_writer
    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.target_address == 0xDA
    assert parsed.message_type == 0x82


def test_ids_runtime_send_rgb_command_writes_solid_payload() -> None:
    """send_rgb_command should emit native 8-byte RGB payload for solid mode."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800ce031400000008e9ce"))
    runtime.handle_frame(bytes.fromhex("0802ce0067ce0d002740d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCE] = time.monotonic()

    used = asyncio.run(
        runtime.send_rgb_command(
            0x03,
            0xCE,
            mode=0x01,
            red=0x11,
            green=0x22,
            blue=0x33,
            auto_off=0x04,
        )
    )
    assert used is True

    writer = state._eth_writer
    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.message_type == 0x82
    assert parsed.target_address == 0xCE
    assert parsed.message_data == 0x00
    assert parsed.payload == bytes.fromhex("0111223304000000")

    light = state.rgb_lights.get("03:ce")
    assert light is not None
    assert light.mode == 0x01
    assert (light.red, light.green, light.blue) == (0x11, 0x22, 0x33)


def test_ids_runtime_send_rgb_command_writes_transition_payload() -> None:
    """Transition RGB modes should encode transition interval as big-endian."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800ce031400000008e9ce"))
    runtime.handle_frame(bytes.fromhex("0802ce0067ce0d002740d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCE] = time.monotonic()

    used = asyncio.run(
        runtime.send_rgb_command(
            0x03,
            0xCE,
            mode=0x08,
            auto_off=0x09,
            transition_interval=0x1234,
        )
    )
    assert used is True

    writer = state._eth_writer
    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.message_type == 0x82
    assert parsed.target_address == 0xCE
    assert parsed.message_data == 0x00
    assert parsed.payload == bytes.fromhex("08ffffff09123400")


def test_ids_runtime_send_rgb_command_writes_off_payload() -> None:
    """Off RGB command should set mode byte to 0x00 in native payload."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime.handle_frame(bytes.fromhex("0800ce031400000008e9ce"))
    runtime.handle_frame(bytes.fromhex("0802ce0067ce0d002740d0"))

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCE] = time.monotonic()

    used = asyncio.run(runtime.send_rgb_command(0x03, 0xCE, mode=0x00))
    assert used is True

    writer = state._eth_writer
    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.message_type == 0x82
    assert parsed.target_address == 0xCE
    assert parsed.message_data == 0x00
    assert parsed.payload[0] == 0x00

    light = state.rgb_lights.get("03:ce")
    assert light is not None
    assert light.mode == 0x00


def test_ids_runtime_send_hvac_command_writes_native_payload() -> None:
    """send_hvac_command should emit native 3-byte HVAC payload and optimistic state."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    state._device_identities["03:cf"] = DeviceIdentity(
        table_id=0x03,
        device_id=0xCF,
        protocol=2,
        device_type=16,
        device_instance=0,
        product_id=0,
        product_mac="",
    )
    state.hvac_zones["03:cf"] = HvacZone(
        table_id=0x03,
        device_id=0xCF,
        heat_mode=0,
        heat_source=0,
        fan_mode=0,
        low_trip_f=68,
        high_trip_f=72,
        zone_status=1,
        indoor_temp_f=70,
        outdoor_temp_f=55,
        dtc_code=0,
    )

    import asyncio
    import time

    runtime._ids_session_opened_at[0xCF] = time.monotonic()

    used = asyncio.run(
        runtime.send_hvac_command(
            0x03,
            0xCF,
            heat_mode=3,
            heat_source=1,
            fan_mode=2,
            low_trip_f=66,
            high_trip_f=79,
        )
    )
    assert used is True

    writer = state._eth_writer
    decoder = CobsByteDecoder(use_crc=True)
    raw = None
    for byte_val in writer.writes[-1]:
        raw = decoder.decode_byte(byte_val)
        if raw is not None:
            break

    assert raw is not None
    parsed = parse_ids_can_wire_frame(raw)
    assert parsed is not None
    assert parsed.message_type == 0x82
    assert parsed.target_address == 0xCF
    assert parsed.message_data == 0x00
    assert parsed.payload == bytes([0x93, 66, 79])

    zone = state.hvac_zones.get("03:cf")
    assert zone is not None
    assert zone.heat_mode == 3
    assert zone.heat_source == 1
    assert zone.fan_mode == 2
    assert zone.low_trip_f == 66
    assert zone.high_trip_f == 79


def test_ids_runtime_hvac_device_status_decodes_temperature_feedback() -> None:
    """IDS HVAC DEVICE_STATUS payload should decode command/setpoint/temperature fields."""
    state = _base_state()
    runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    runtime._ids_source_identities[0xCF] = DeviceIdentity(
        table_id=0x03,
        device_id=0xCF,
        protocol=2,
        device_type=16,
        device_instance=0,
        product_id=0,
        product_mac="",
    )

    payload = bytes([
        0x93,  # heat_mode=3 heat_source=1 fan_mode=2
        66,    # low trip
        79,    # high trip
        0x03,  # zone status
        0x48, 0x00,  # indoor 72.0F
        0x37, 0x00,  # outdoor 55.0F
        0x12, 0x34,  # dtc
    ])
    runtime._handle_ids_device_status(0xCF, payload)

    zone = state.hvac_zones.get("03:cf")
    assert zone is not None
    assert zone.heat_mode == 3
    assert zone.heat_source == 1
    assert zone.fan_mode == 2
    assert zone.low_trip_f == 66
    assert zone.high_trip_f == 79
    assert zone.zone_status == 0x03
    assert zone.indoor_temp_f == 72.0
    assert zone.outdoor_temp_f == 55.0
    assert zone.dtc_code == 0x1234
