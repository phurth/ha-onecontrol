"""Tests for keeping a bonded gateway on the adapter that holds its bond."""

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from custom_components.ha_onecontrol import ble_pinning
from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.ble_agent import _bonded_device_paths_in
from custom_components.ha_onecontrol.ble_pinning import (
    ConnectionPath,
    PinnedAdapterUnavailable,
    connection_path,
    path_matches_pin,
    pinned_candidates,
)
from custom_components.ha_onecontrol.coordinator import (
    OneControlCoordinator,
    _is_pin_refusal,
)

GATEWAY = "24:DC:C3:ED:1E:0A"
DEV = GATEWAY.replace(":", "_")
BONDED = "3C:3B:AD:78:01:B3"
OTHER = "A0:AD:9F:71:28:58"
PROXY = "CC:BA:97:10:C1:86"
BONDED_PATH = f"/org/bluez/hci1/dev_{DEV}"
OTHER_PATH = f"/org/bluez/hci0/dev_{DEV}"


def _candidate(source: str, rssi: int | None, name: str | None = None):
    return SimpleNamespace(
        scanner=SimpleNamespace(source=source, name=name or source),
        ble_device=SimpleNamespace(address=GATEWAY, source=source),
        advertisement=SimpleNamespace(rssi=rssi),
    )


class _FakeWrapper:
    """Stands in for habluetooth's HaBleakClientWrapper (same two hooks)."""

    def __init__(self, address_or_ble_device, *args, **kwargs):
        self.init_kwargs = kwargs
        self.no_slot: set[str] = set()

    def _async_get_best_available_backend_and_device(self, manager):
        return "stock choice"

    def _async_get_backend_for_ble_device(self, manager, scanner, ble_device):
        if scanner.source in self.no_slot:
            return None
        return f"backend via {scanner.source}"


class _Manager:
    def __init__(self, *candidates):
        self.candidates = list(candidates)

    def async_scanner_devices_by_address(self, address, connectable):
        assert address == GATEWAY
        assert connectable is True
        return self.candidates


PinnedClient = ble_pinning._make_pinned_client_class(_FakeWrapper)


# ── Path choice ───────────────────────────────────────────────────────


def test_candidates_on_pinned_sources_strongest_first() -> None:
    weak, strong = _candidate(BONDED, -70), _candidate("11:22:33:44:55:66", -40)
    pinned = frozenset({"3C3BAD7801B3", "112233445566"})

    result = pinned_candidates([_candidate(PROXY, -30), weak, strong], pinned)

    assert result == [strong, weak]


def test_unpinned_client_keeps_home_assistant_choice() -> None:
    client = PinnedClient(GATEWAY)

    assert client._async_get_best_available_backend_and_device(_Manager()) == "stock choice"


def test_pin_beats_a_stronger_unbonded_adapter_and_proxy() -> None:
    manager = _Manager(_candidate(PROXY, -40), _candidate(OTHER, -45), _candidate(BONDED, -75))
    client = PinnedClient(GATEWAY, pinned_sources={BONDED})

    assert client._async_get_best_available_backend_and_device(manager) == f"backend via {BONDED}"


def test_manager_may_be_passed_by_keyword() -> None:
    client = PinnedClient(GATEWAY, pinned_sources={BONDED})

    backend = client._async_get_best_available_backend_and_device(
        manager=_Manager(_candidate(BONDED, -60))
    )

    assert backend == f"backend via {BONDED}"


def test_second_bonded_adapter_is_used_when_the_first_has_no_slot() -> None:
    second = "11:22:33:44:55:66"
    manager = _Manager(_candidate(BONDED, -40), _candidate(second, -70))
    client = PinnedClient(GATEWAY, pinned_sources={BONDED, second})
    client.no_slot = {BONDED}

    assert client._async_get_best_available_backend_and_device(manager) == f"backend via {second}"


def _scored(source: str, rssi: int, score: float):
    candidate = _candidate(source, rssi)
    candidate.score_connection_path = lambda rssi_diff: score
    return candidate


def test_stock_choice_follows_home_assistants_score() -> None:
    """A busy local adapter loses to a weaker proxy in Home Assistant's ranking."""
    busy_local = _scored(BONDED, -43, score=-66.2)
    proxy = _scored(PROXY, -66, score=-66.0)

    assert ble_pinning.stock_choice([busy_local, proxy]) is proxy


def test_stock_choice_is_none_without_scoring() -> None:
    assert ble_pinning.stock_choice([_candidate(BONDED, -40), _candidate(PROXY, -60)]) is None


def test_log_names_the_path_home_assistant_would_have_used(caplog) -> None:
    caplog.set_level("INFO")
    manager = _Manager(_scored(BONDED, -43, score=-66.2), _scored(PROXY, -66, score=-66.0))
    client = PinnedClient(GATEWAY, pinned_sources={BONDED})

    assert client._async_get_best_available_backend_and_device(manager) == f"backend via {BONDED}"
    assert f"Home Assistant would have used {PROXY}" in caplog.text


def test_pinned_adapter_not_hearing_the_device_is_refused() -> None:
    client = PinnedClient(GATEWAY, pinned_sources={BONDED})

    with pytest.raises(PinnedAdapterUnavailable, match="has not heard"):
        client._async_get_best_available_backend_and_device(_Manager(_candidate(PROXY, -40)))


def test_pinned_adapter_without_a_slot_is_refused() -> None:
    client = PinnedClient(GATEWAY, pinned_sources={BONDED})
    client.no_slot = {BONDED}

    with pytest.raises(PinnedAdapterUnavailable, match="no free connection slot"):
        client._async_get_best_available_backend_and_device(_Manager(_candidate(BONDED, -40)))


def test_refusal_is_a_bleak_error() -> None:
    """Callers that catch BleakError (the 5 s poll) must still catch it."""
    assert issubclass(PinnedAdapterUnavailable, BleakError)


def test_pin_argument_is_consumed_and_the_rest_passed_on() -> None:
    client = PinnedClient(GATEWAY, pinned_sources={BONDED}, _is_retry_client=True)

    assert client.init_kwargs == {"_is_retry_client": True}
    assert client._oc_pinned == frozenset({"3C3BAD7801B3"})


def test_ble_device_address_is_used() -> None:
    client = PinnedClient(SimpleNamespace(address=GATEWAY), pinned_sources={BONDED})

    assert client._oc_address == GATEWAY


# ── Availability of the hooks ─────────────────────────────────────────


def _install_fake_habluetooth(monkeypatch, wrapper: type) -> None:
    package = types.ModuleType("habluetooth")
    wrappers = types.ModuleType("habluetooth.wrappers")
    wrappers.HaBleakClientWrapper = wrapper
    package.wrappers = wrappers
    monkeypatch.setitem(sys.modules, "habluetooth", package)
    monkeypatch.setitem(sys.modules, "habluetooth.wrappers", wrappers)
    monkeypatch.setattr(ble_pinning, "_CLIENT_CLASS_CACHE", {})


def test_client_class_subclasses_home_assistants_wrapper(monkeypatch) -> None:
    _install_fake_habluetooth(monkeypatch, _FakeWrapper)

    cls = ble_pinning.pinned_client_class()

    assert cls is not None and issubclass(cls, _FakeWrapper)


def test_no_client_class_when_the_hooks_are_gone(monkeypatch) -> None:
    _install_fake_habluetooth(monkeypatch, type("Wrapper", (), {}))

    assert ble_pinning.pinned_client_class() is None


def test_no_client_class_without_habluetooth(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "habluetooth", None)
    monkeypatch.setitem(sys.modules, "habluetooth.wrappers", None)
    monkeypatch.setattr(ble_pinning, "_CLIENT_CLASS_CACHE", {})

    assert ble_pinning.pinned_client_class() is None


# ── Telling where a link really went ──────────────────────────────────


def _client(source: str | None, device_path: str | None):
    return SimpleNamespace(
        _connected_scanner=SimpleNamespace(source=source) if source else None,
        _backend=SimpleNamespace(_device_path=device_path),
    )


PINNED = frozenset({"3C3BAD7801B3"})
PINNED_PATHS = frozenset({BONDED_PATH})


def test_link_on_the_bonded_adapter_matches() -> None:
    path = connection_path(_client(BONDED, BONDED_PATH))

    assert path == ConnectionPath(BONDED, BONDED_PATH)
    assert path_matches_pin(path, PINNED, PINNED_PATHS) is True


def test_link_on_another_local_adapter_does_not_match() -> None:
    path = connection_path(_client(OTHER, OTHER_PATH))

    assert path_matches_pin(path, PINNED, PINNED_PATHS) is False


def test_link_through_a_proxy_does_not_match() -> None:
    path = connection_path(_client(PROXY, None))

    assert path_matches_pin(path, PINNED, PINNED_PATHS) is False


def test_local_link_without_a_bluez_path_falls_back_to_the_source() -> None:
    """A future non-BlueZ local backend still reports its adapter."""
    path = connection_path(_client(BONDED, None))

    assert path_matches_pin(path, PINNED, PINNED_PATHS) is True


def test_bluez_path_alone_is_enough() -> None:
    path = connection_path(_client(None, BONDED_PATH))

    assert path_matches_pin(path, PINNED, PINNED_PATHS) is True


def test_unknown_path_cannot_be_judged() -> None:
    path = connection_path(SimpleNamespace())

    assert path == ConnectionPath(None, None)
    assert path_matches_pin(path, PINNED, PINNED_PATHS) is None


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
        "_pin_refused": False,
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
