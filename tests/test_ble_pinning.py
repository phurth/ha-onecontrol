"""Tests for how the coordinator uses the ha-ble-pinning library.

The library's own behaviour is tested in its repository
(https://github.com/phurth/ha-ble-pinning).
"""

import asyncio
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.ble_agent import _bonded_device_paths_in
from ha_ble_pinning import PinnedAdapterUnavailable
from custom_components.ha_onecontrol.coordinator import (
    OneControlCoordinator,
    _is_pin_refusal,
)

GATEWAY = "AA:BB:CC:DD:EE:01"
DEV = GATEWAY.replace(":", "_")
BONDED = "11:11:11:11:11:11"
OTHER = "22:22:22:22:22:22"
PROXY = "33:33:33:33:33:33"
BONDED_PATH = f"/org/bluez/hci1/dev_{DEV}"
OTHER_PATH = f"/org/bluez/hci0/dev_{DEV}"


def _candidate(source: str, rssi: int | None, name: str | None = None):
    return SimpleNamespace(
        scanner=SimpleNamespace(source=source, name=name or source),
        ble_device=SimpleNamespace(address=GATEWAY, source=source),
        advertisement=SimpleNamespace(rssi=rssi),
    )


PINNED = frozenset({"111111111111"})
PINNED_PATHS = frozenset({BONDED_PATH})


def test_bonded_device_paths_come_from_bluez_objects() -> None:
    adapter = "org.bluez.Adapter1"
    device = "org.bluez.Device1"
    objects = {
        "/org/bluez/hci0": {adapter: {"Address": OTHER}},
        "/org/bluez/hci1": {adapter: {"Address": BONDED}},
        OTHER_PATH: {device: {"Paired": False}},
        BONDED_PATH: {device: {"Paired": True}},
    }

    assert _bonded_device_paths_in(objects, GATEWAY) == {BONDED: BONDED_PATH}


# ── Coordinator ───────────────────────────────────────────────────────


def _coordinator(**attrs: object) -> OneControlCoordinator:
    coordinator = object.__new__(OneControlCoordinator)
    defaults = {
        "address": GATEWAY,
        "_closed": False,
        "_client": None,
        "_connected": False,
        "_authenticated": False,
        "_current_connect_source": None,
        "_connect_adapter": None,
        "_pinned_sources": PINNED,
        "_pinned_device_paths": PINNED_PATHS,
        "_fallback_sources": frozenset(),
        "_pin_refused": False,
        "connection_source": None,
        "fallback_source": None,
        "bonded_adapters": {},
    }
    defaults.update(attrs)
    for name, value in defaults.items():
        setattr(coordinator, name, value)
    return coordinator


class _LiveClient:
    def __init__(self, source, device_path):
        self._connected_scanner = SimpleNamespace(source=source)
        self._backend = SimpleNamespace(_device_path=device_path)
        self.callback_cleared = False
        self.disconnected = False

    def set_disconnected_callback(self, callback):
        self.callback_cleared = callback is None

    async def disconnect(self):
        self.disconnected = True


def test_link_on_the_bonded_adapter_is_kept_and_recorded() -> None:
    coordinator = _coordinator()
    client = _LiveClient(BONDED, BONDED_PATH)

    asyncio.run(coordinator._check_connection_path(client))

    assert not client.disconnected
    assert coordinator._current_connect_source == BONDED
    assert coordinator._connect_adapter == BONDED


def test_link_that_bypassed_the_pin_is_dropped_quietly() -> None:
    coordinator = _coordinator()
    client = _LiveClient(OTHER, OTHER_PATH)

    with pytest.raises(PinnedAdapterUnavailable):
        asyncio.run(coordinator._check_connection_path(client))

    assert client.callback_cleared, "the drop must not schedule a second reconnect"
    assert client.disconnected


def test_link_through_a_proxy_that_authenticated_before_is_kept() -> None:
    coordinator = _coordinator(_fallback_sources=frozenset({"333333333333"}))
    client = _LiveClient(PROXY, None)

    asyncio.run(coordinator._check_connection_path(client))

    assert not client.disconnected
    assert coordinator.connection_source == PROXY


def test_proxy_link_records_no_local_adapter() -> None:
    coordinator = _coordinator(_pinned_sources=frozenset(), _pinned_device_paths=frozenset())
    client = _LiveClient(PROXY, None)

    asyncio.run(coordinator._check_connection_path(client))

    assert coordinator._current_connect_source == PROXY
    assert coordinator._connect_adapter is None


def test_refusal_is_found_through_wrapping() -> None:
    try:
        try:
            raise PinnedAdapterUnavailable("pinned adapter busy")
        except PinnedAdapterUnavailable as inner:
            raise BleakError("Failed to connect after 4 attempt(s)") from inner
    except BleakError as outer:
        assert _is_pin_refusal(outer)
    assert not _is_pin_refusal(BleakError("Insufficient authentication"))
    assert not _is_pin_refusal(None)


def _ladder(monkeypatch, failure: Exception, **attrs):
    removed, direct = [], []

    async def try_connect(attempt: int) -> None:
        raise failure

    async def try_direct(adapter: str) -> None:
        direct.append(adapter)
        raise BleakError("direct failed")

    async def remove_bond(address: str, adapter: str | None = None) -> bool:
        removed.append(address)
        return True

    async def proven() -> bool:
        return False

    async def no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(coordinator_module, "remove_bond", remove_bond)
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", no_sleep)
    coordinator = _coordinator(
        **{
            "_pairing_method": "pin",
            "_gateway_family": "myrvlink",
            "_pin_already_bonded": True,
            **attrs,
        }
    )
    coordinator._try_connect = try_connect
    coordinator._try_connect_direct = try_direct
    coordinator._async_has_proven_bond = proven
    return coordinator, removed, direct


def _wrapped_refusal() -> BleakError:
    try:
        try:
            raise PinnedAdapterUnavailable("has not heard the gateway recently")
        except PinnedAdapterUnavailable as inner:
            raise BleakError("Failed to connect") from inner
    except BleakError as outer:
        return outer


def test_refused_connects_never_remove_the_bond(monkeypatch) -> None:
    coordinator, removed, direct = _ladder(monkeypatch, _wrapped_refusal())

    with pytest.raises(BleakError):
        asyncio.run(coordinator._do_connect())

    assert removed == []
    assert direct == []
    assert coordinator._pin_refused is True


def test_pinned_gateway_skips_the_unpinned_direct_fallback(monkeypatch) -> None:
    coordinator, removed, direct = _ladder(
        monkeypatch, BleakError("timeout"), _pin_already_bonded=False
    )

    with pytest.raises(BleakError):
        asyncio.run(coordinator._do_connect())

    assert direct == []
    assert coordinator._pin_refused is False


def test_unpinned_gateway_keeps_the_direct_fallback(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_module.os, "listdir", lambda path: ["hci0"])
    coordinator, removed, direct = _ladder(
        monkeypatch,
        BleakError("timeout"),
        _pin_already_bonded=False,
        _pinned_sources=frozenset(),
        _pinned_device_paths=frozenset(),
    )

    with pytest.raises(BleakError):
        asyncio.run(coordinator._do_connect())

    assert direct == ["hci0"]
