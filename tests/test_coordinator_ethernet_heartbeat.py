"""Unit tests for Ethernet heartbeat/keepalive behavior in coordinator."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.coordinator import OneControlCoordinator
from custom_components.ha_onecontrol.runtime.ids_can_runtime import IdsCanRuntime


class _DummyWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        return


def _fake_eth_state() -> SimpleNamespace:
    return SimpleNamespace(
        is_ethernet_gateway=True,
        _connected=True,
        _eth_writer=_DummyWriter(),
        _last_ethernet_tx_time=0.0,
        _ethernet_transport_keepalives_sent=0,
    )


def test_send_ethernet_transport_keepalive_only_when_idle(monkeypatch) -> None:
    """Transport keepalive writes delimiter only when idle threshold is met."""
    state = _fake_eth_state()

    now = 100.0

    def _monotonic() -> float:
        return now

    monkeypatch.setattr(coordinator_module.time, "monotonic", _monotonic)

    # First call should send one delimiter and bump counter.
    asyncio.run(OneControlCoordinator._send_ethernet_transport_keepalive(cast(Any, state)))
    assert state._eth_writer.writes == [b"\x00"]
    assert state._ethernet_transport_keepalives_sent == 1

    # Simulate a recent TX less than keepalive interval; no new write expected.
    state._last_ethernet_tx_time = now - 1.0
    asyncio.run(OneControlCoordinator._send_ethernet_transport_keepalive(cast(Any, state)))
    assert state._eth_writer.writes == [b"\x00"]
    assert state._ethernet_transport_keepalives_sent == 1


def test_heartbeat_loop_sends_transport_keepalive_without_gateway_info(monkeypatch) -> None:
    """Ethernet heartbeat must keep socket alive even before gateway info is known."""
    sent: list[str] = []

    async def _sleep(_: float) -> None:
        return

    async def _send_keepalive() -> None:
        sent.append("keepalive")
        state._connected = False  # exit loop after one keepalive cycle

    async def _force_reconnect(_: str) -> None:
        raise AssertionError("Should not reconnect on healthy keepalive")

    state = SimpleNamespace(
        is_ethernet_gateway=True,
        _connected=True,
        _authenticated=True,
        gateway_info=None,
        _last_event_time=0.0,
        _select_get_devices_table_id=lambda: None,
        _send_ethernet_transport_keepalive=_send_keepalive,
        _force_ethernet_reconnect=_force_reconnect,
    )

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    asyncio.run(OneControlCoordinator._heartbeat_loop(cast(Any, state)))

    assert sent == ["keepalive"]


def test_heartbeat_loop_reconnects_when_keepalive_raises(monkeypatch) -> None:
    """Ethernet heartbeat should force reconnect when transport keepalive fails."""
    reasons: list[str] = []

    async def _sleep(_: float) -> None:
        return

    async def _send_keepalive() -> None:
        raise RuntimeError("socket write failed")

    async def _force_reconnect(reason: str) -> None:
        reasons.append(reason)
        state._connected = False

    state = SimpleNamespace(
        is_ethernet_gateway=True,
        _connected=True,
        _authenticated=True,
        gateway_info=None,
        _last_event_time=0.0,
        _select_get_devices_table_id=lambda: None,
        _send_ethernet_transport_keepalive=_send_keepalive,
        _force_ethernet_reconnect=_force_reconnect,
    )

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    asyncio.run(OneControlCoordinator._heartbeat_loop(cast(Any, state)))

    assert reasons == ["transport keepalive failed"]


def test_select_get_devices_table_id_falls_back_to_observed_tables() -> None:
    """Fallback GetDevices table should come from observed TT:DD state keys."""
    state = SimpleNamespace(
        gateway_info=None,
        relays={"03:d6": object(), "03:da": object(), "81:d5": object()},
        dimmable_lights={"03:7d": object(), "02:cd": object()},
        rgb_lights={},
        covers={},
        hvac_zones={},
        tanks={},
        device_online={},
        device_locks={},
        generators={},
        hour_meters={},
    )

    selected = OneControlCoordinator._select_get_devices_table_id(cast(Any, state))

    assert selected == 0x03


def test_process_frame_ignores_alt_cmdid_envelope_for_get_devices() -> None:
    """Unsupported [cmdL][cmdH][0x02][resp] envelope must not be treated as command response."""
    cmd_id = 0x1234
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
        "pending_get_devices_peak": 0,
        "frame_parse_errors": 0,
        "pending_cmdid_pruned": 0,
        "unknown_cmdids_pruned": 0,
        "external_names_applied": 0,
    }
    state = SimpleNamespace(
        is_ethernet_gateway=True,
        _last_event_time=0.0,
        _prune_pending_command_state=lambda: None,
        _classify_frame_family=lambda _: "myrvlink_command",
        _frame_family_stats={"myrvlink_command": 0},
        _cmd_correlation_stats=stats,
        _pending_get_devices_cmdids={cmd_id: 0x03},
        _pending_get_devices_sent_at={cmd_id: 1.0},
        _pending_metadata_cmdids={},
        _pending_metadata_entries={},
        _unknown_command_counts={},
        _device_identities={},
        _apply_external_name=lambda *_: None,
        _bump_unknown_cmd_count=lambda _: 1,
    )
    state._ids_runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # [cmdL][cmdH][0x02][0x01][table][start][count][protocol][size][payload(10)]
    frame = bytes(
        [
            0x34,
            0x12,
            0x02,
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

    OneControlCoordinator._process_frame(cast(Any, state), frame)

    assert stats["get_devices_identity_rows"] == 0
    assert "03:00" not in state._device_identities


def test_process_frame_accepts_markerless_cmdid_envelope_for_get_devices() -> None:
    """Ethernet bridge can emit [cmdL][cmdH][resp] envelopes without explicit 0x02 marker."""
    cmd_id = 0x1234
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
        "pending_get_devices_peak": 0,
        "frame_parse_errors": 0,
        "pending_cmdid_pruned": 0,
        "unknown_cmdids_pruned": 0,
        "external_names_applied": 0,
    }
    state = SimpleNamespace(
        is_ethernet_gateway=True,
        _last_event_time=0.0,
        _prune_pending_command_state=lambda: None,
        _classify_frame_family=lambda _: "myrvlink_command",
        _frame_family_stats={"myrvlink_command": 0},
        _cmd_correlation_stats=stats,
        _pending_get_devices_cmdids={cmd_id: 0x03},
        _pending_get_devices_sent_at={cmd_id: 1.0},
        _pending_metadata_cmdids={},
        _pending_metadata_entries={},
        _unknown_command_counts={},
        _device_identities={},
        _apply_external_name=lambda *_: None,
        _bump_unknown_cmd_count=lambda _: 1,
    )
    state._ids_runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # [cmdL][cmdH][0x01][table][start][count][protocol][size][payload(10)]
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

    OneControlCoordinator._process_frame(cast(Any, state), frame)

    assert stats["get_devices_identity_rows"] == 1
    assert "03:00" in state._device_identities


def test_process_frame_ignores_typed_markerless_cmdid_envelope_for_get_devices() -> None:
    """Unsupported [cmdL][cmdH][cmdType][resp] envelope must not be treated as command response."""
    cmd_id = 0x1234
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
        "pending_get_devices_peak": 0,
        "frame_parse_errors": 0,
        "pending_cmdid_pruned": 0,
        "unknown_cmdids_pruned": 0,
        "external_names_applied": 0,
    }
    state = SimpleNamespace(
        is_ethernet_gateway=True,
        _last_event_time=0.0,
        _prune_pending_command_state=lambda: None,
        _classify_frame_family=lambda _: "myrvlink_command",
        _frame_family_stats={"myrvlink_command": 0},
        _cmd_correlation_stats=stats,
        _pending_get_devices_cmdids={cmd_id: 0x03},
        _pending_get_devices_sent_at={cmd_id: 1.0},
        _pending_metadata_cmdids={},
        _pending_metadata_entries={},
        _unknown_command_counts={},
        _device_identities={},
        _apply_external_name=lambda *_: None,
        _bump_unknown_cmd_count=lambda _: 1,
    )
    state._ids_runtime = IdsCanRuntime(state)  # type: ignore[arg-type]

    # [cmdL][cmdH][0x01(cmdType)][0x01(resp)][table][start][count][protocol][size][payload(10)]
    frame = bytes(
        [
            0x34,
            0x12,
            0x01,
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

    OneControlCoordinator._process_frame(cast(Any, state), frame)

    assert stats["get_devices_identity_rows"] == 0
    assert "03:00" not in state._device_identities
