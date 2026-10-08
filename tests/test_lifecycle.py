"""Tests for coordinator teardown, bond retention and NETWORK aggregation."""

import asyncio
from types import SimpleNamespace

from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.coordinator import OneControlCoordinator

HCI0_MAC = "11:11:11:11:11:11"
HCI1_MAC = "22:22:22:22:22:22"


def _coordinator(**attrs: object) -> OneControlCoordinator:
    coordinator = object.__new__(OneControlCoordinator)
    for name, value in attrs.items():
        setattr(coordinator, name, value)
    return coordinator


# ── Terminal close ────────────────────────────────────────────────────


def test_closed_coordinator_does_not_connect() -> None:
    calls = []

    async def do_connect() -> None:
        calls.append("connect")

    coordinator = _coordinator(
        _closed=True, _connected=False, _connect_lock=asyncio.Lock()
    )
    coordinator._do_connect = do_connect

    asyncio.run(coordinator.async_connect())

    assert calls == []


def test_closed_coordinator_does_not_schedule_reconnect() -> None:
    hass = SimpleNamespace(async_create_task=lambda coro: 1 / 0)
    coordinator = _coordinator(_closed=True, hass=hass, _reconnect_task=None)

    coordinator._schedule_reconnect()

    assert coordinator._reconnect_task is None


def test_connect_finishing_after_close_drops_the_link() -> None:
    disconnected = []

    async def disconnect() -> None:
        disconnected.append(True)

    coordinator = _coordinator(
        _closed=False, _connected=False, _connect_lock=asyncio.Lock(), _client=None
    )

    async def do_connect() -> None:
        # The entry is unloaded while the connect ladder is still running.
        coordinator._closed = True
        coordinator._client = SimpleNamespace(disconnect=disconnect)

    coordinator._do_connect = do_connect

    asyncio.run(coordinator.async_connect())

    assert disconnected == [True]
    assert coordinator._client is None


# ── Bond retention ────────────────────────────────────────────────────


def _bond_coordinator(monkeypatch, stored: str | None, bonded: set[str]):
    removed = []

    async def bonded_macs(address: str) -> set[str]:
        return bonded

    async def remove_bond(address: str, adapter: str | None = None) -> bool:
        removed.append((address, adapter))
        return True

    monkeypatch.setattr(coordinator_module, "async_get_bonded_adapter_macs", bonded_macs)
    monkeypatch.setattr(coordinator_module, "remove_bond", remove_bond)
    options = {"bonded_source": stored} if stored else {}
    coordinator = _coordinator(
        address="AA:BB:CC:DD:EE:FF",
        entry=SimpleNamespace(options=options),
        _pairing_method="pin",
        _connect_adapter=HCI1_MAC,
        _pin_already_bonded=True,
    )
    return coordinator, removed


def test_bond_that_authenticated_before_is_kept(monkeypatch) -> None:
    coordinator, removed = _bond_coordinator(monkeypatch, HCI1_MAC, {HCI1_MAC})

    asyncio.run(coordinator._remove_stale_bond())

    assert removed == []
    assert coordinator._pin_already_bonded is True


def test_bond_that_never_authenticated_is_removed(monkeypatch) -> None:
    coordinator, removed = _bond_coordinator(monkeypatch, None, {HCI1_MAC})

    asyncio.run(coordinator._remove_stale_bond())

    assert removed == [("AA:BB:CC:DD:EE:FF", HCI1_MAC)]
    assert coordinator._pin_already_bonded is False


def test_bond_on_a_different_adapter_than_authenticated_is_removed(monkeypatch) -> None:
    coordinator, removed = _bond_coordinator(monkeypatch, HCI0_MAC, {HCI1_MAC})

    asyncio.run(coordinator._remove_stale_bond())

    assert len(removed) == 1


def test_unbonded_gateway_still_has_its_device_entry_cleared(monkeypatch) -> None:
    """Gateways that never bond rely on removal to flush BlueZ's GATT cache."""
    coordinator, removed = _bond_coordinator(monkeypatch, HCI1_MAC, set())

    asyncio.run(coordinator._remove_stale_bond())

    assert len(removed) == 1


# ── NETWORK frame aggregation ─────────────────────────────────────────


def _network_frame(src: int, protocol: int, lockout: int):
    wire = SimpleNamespace(message_type=0x00, source_address=src, payload=b"")
    decoded = SimpleNamespace(
        fields={"protocol_version": protocol, "in_motion_lockout_level": lockout}
    )
    return wire, decoded


def test_network_frames_aggregate_across_sources() -> None:
    updates = []
    coordinator = _coordinator(
        _can_device_types={},
        _invalid_can_sources=set(),
        _can_protocol_by_source={},
        _can_lockout_by_source={},
        system_lockout_level=None,
        gateway_info=None,
        _last_event_time=0.0,
    )
    coordinator._build_data = lambda: {}
    coordinator.async_set_updated_data = updates.append

    coordinator._dispatch_can_entity(*_network_frame(0x10, protocol=20, lockout=0))
    coordinator._dispatch_can_entity(*_network_frame(0x11, protocol=12, lockout=1))
    # Repeats of the same heartbeats must not flap the value or push updates.
    coordinator._dispatch_can_entity(*_network_frame(0x10, protocol=20, lockout=0))
    coordinator._dispatch_can_entity(*_network_frame(0x11, protocol=12, lockout=1))

    assert coordinator.gateway_info.protocol_version == 20
    assert coordinator.system_lockout_level == 1
    assert len(updates) == 2

    coordinator._dispatch_can_entity(*_network_frame(0x11, protocol=12, lockout=0))

    assert coordinator.system_lockout_level == 0
    assert len(updates) == 3
