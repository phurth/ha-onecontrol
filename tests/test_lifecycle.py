"""Tests for coordinator teardown, bond retention, NETWORK aggregation,
heartbeat recovery and entry reload/unload."""

import asyncio
import time
from types import SimpleNamespace

from bleak.exc import BleakError

import custom_components.ha_onecontrol as integration
from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.const import (
    CONF_BONDED_SOURCE,
    CONF_ENABLE_COVER_CONTROL,
    DOMAIN,
)
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


# ── Heartbeat recovery (issue #16) ────────────────────────────────────


def _heartbeat_coordinator(monkeypatch, client, *, last_event_age: float, send=None):
    monkeypatch.setattr(coordinator_module, "HEARTBEAT_INTERVAL", 0)
    monkeypatch.setattr(coordinator_module, "_STALLED_DISCONNECT_TIMEOUT_S", 0.05)
    calls = SimpleNamespace(disconnects=[], listener_updates=0)

    async def default_send(table_id: int) -> None:
        return None

    coordinator = _coordinator(
        _connected=True,
        _authenticated=True,
        _client=client,
        _can_ble_confirmed=False,
        gateway_info=SimpleNamespace(table_id=0x28),
        _last_event_time=time.monotonic() - last_event_age,
    )
    coordinator._is_startup_bootstrap_active = lambda table_id: False
    coordinator._send_get_devices_request = send or default_send

    def on_disconnect(c) -> None:
        calls.disconnects.append(c)
        coordinator._connected = False

    def update_listeners() -> None:
        calls.listener_updates += 1

    coordinator._on_disconnect = on_disconnect
    coordinator.async_update_listeners = update_listeners
    return coordinator, calls


def test_stale_link_reconnects_when_disconnect_raises(monkeypatch) -> None:
    async def disconnect() -> None:
        raise BleakError("Not connected")

    client = SimpleNamespace(disconnect=disconnect)
    coordinator, calls = _heartbeat_coordinator(monkeypatch, client, last_event_age=1000)

    asyncio.run(coordinator._heartbeat_loop())

    assert calls.disconnects == [client]


def test_stale_link_reconnects_when_disconnect_hangs(monkeypatch) -> None:
    async def disconnect() -> None:
        await asyncio.sleep(60)

    client = SimpleNamespace(disconnect=disconnect)
    coordinator, calls = _heartbeat_coordinator(monkeypatch, client, last_event_age=1000)

    asyncio.run(coordinator._heartbeat_loop())

    assert calls.disconnects == [client]


def test_stale_link_disconnect_callback_is_not_doubled(monkeypatch) -> None:
    async def disconnect() -> None:
        coordinator._on_disconnect(client)

    client = SimpleNamespace(disconnect=disconnect)
    coordinator, calls = _heartbeat_coordinator(monkeypatch, client, last_event_age=1000)

    asyncio.run(coordinator._heartbeat_loop())

    assert calls.disconnects == [client]


def test_heartbeat_write_failure_reconnects(monkeypatch) -> None:
    disconnected = []

    async def disconnect() -> None:
        disconnected.append(True)

    async def send(table_id: int) -> None:
        raise BleakError("write failed")

    client = SimpleNamespace(disconnect=disconnect)
    coordinator, calls = _heartbeat_coordinator(
        monkeypatch, client, last_event_age=1, send=send
    )

    asyncio.run(coordinator._heartbeat_loop())

    assert disconnected == [True]
    assert calls.disconnects == [client]


def test_stale_data_pushes_one_listener_update(monkeypatch) -> None:
    sent = []

    async def send(table_id: int) -> None:
        sent.append(table_id)
        if len(sent) == 3:
            coordinator._authenticated = False

    client = SimpleNamespace(disconnect=None)
    # Past the 15 s health window but short of the 300 s stale timeout.
    coordinator, calls = _heartbeat_coordinator(
        monkeypatch, client, last_event_age=20, send=send
    )

    asyncio.run(coordinator._heartbeat_loop())

    assert len(sent) == 3
    assert calls.listener_updates == 1
    assert calls.disconnects == []


# ── Entry reload / unload (issue #16) ─────────────────────────────────


def _entry(options: dict, data: dict | None = None):
    return SimpleNamespace(entry_id="e1", data=data or {"address": "AA"}, options=options)


def _reload_hass(entry):
    reloads = []

    async def async_reload(entry_id: str) -> None:
        reloads.append(entry_id)

    hass = SimpleNamespace(
        data={DOMAIN: {("e1", "reload_snapshot"): integration._reload_snapshot(entry)}},
        config_entries=SimpleNamespace(async_reload=async_reload),
    )
    return hass, reloads


def test_bonded_source_write_does_not_reload() -> None:
    entry = _entry({CONF_BONDED_SOURCE: "22:22:22:22:22:22"})
    hass, reloads = _reload_hass(entry)

    entry.options = {CONF_BONDED_SOURCE: "11:11:11:11:11:11"}
    asyncio.run(integration._async_update_listener(hass, entry))

    assert reloads == []


def test_user_option_change_still_reloads() -> None:
    entry = _entry({CONF_BONDED_SOURCE: "22:22:22:22:22:22"})
    hass, reloads = _reload_hass(entry)

    entry.options = {**entry.options, CONF_ENABLE_COVER_CONTROL: True}
    asyncio.run(integration._async_update_listener(hass, entry))

    assert reloads == ["e1"]


def test_unload_closes_coordinator_before_platforms() -> None:
    order = []

    async def disconnect() -> None:
        order.append("disconnect")

    async def unload_platforms(entry, platforms) -> bool:
        order.append("platforms")
        return True

    coordinator = SimpleNamespace(instance_tag="t", async_disconnect=disconnect)
    hass = SimpleNamespace(
        data={DOMAIN: {"e1": coordinator, ("e1", "platforms"): ["light"]}},
        config_entries=SimpleNamespace(async_unload_platforms=unload_platforms),
    )

    assert asyncio.run(integration.async_unload_entry(hass, _entry({})))
    assert order == ["disconnect", "platforms"]
    assert hass.data[DOMAIN] == {}


def test_closed_coordinator_drops_frames() -> None:
    processed = []
    coordinator = _coordinator(_closed=True)
    coordinator._process_frame = processed.append

    coordinator._on_data_read(None, bytearray(b"\x03\x01\x02\x00"))
    coordinator._on_can_read(None, bytearray(b"\x02\x01\x02"))

    assert processed == []
