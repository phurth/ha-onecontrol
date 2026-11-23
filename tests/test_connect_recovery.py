"""Tests for recovering from connects that stall instead of failing (issue #22)."""

import asyncio
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.coordinator import OneControlCoordinator


def _coordinator(**attrs: object) -> OneControlCoordinator:
    coordinator = object.__new__(OneControlCoordinator)
    defaults = {
        "address": "AA:BB:CC:DD:EE:01",
        "_closed": False,
        "_connected": True,
        "_client": None,
        "_pairing_method": "push_button",
        "_last_frame_time": 0.0,
    }
    defaults.update(attrs)
    for name, value in defaults.items():
        setattr(coordinator, name, value)
    return coordinator


class _Client:
    def __init__(self, fail_notify: set[str] | None = None) -> None:
        self.disconnected = False
        self.fail_notify = fail_notify or set()

    async def disconnect(self) -> None:
        self.disconnected = True

    async def start_notify(self, uuid, callback) -> None:
        if uuid in self.fail_notify:
            raise BleakError("Insufficient authentication")


# ── A connect that stalls after the link is up ────────────────────────


def test_stalled_connect_is_abandoned_and_the_link_dropped(monkeypatch) -> None:
    """A GATT read that never returns must not hold the connect lock forever."""
    monkeypatch.setattr(coordinator_module, "_FINISH_CONNECT_TIMEOUT_S", 0.05)
    coordinator = _coordinator()
    client = _Client()

    async def path_ok(client) -> None:
        return None

    async def hang(client) -> None:
        await asyncio.Event().wait()

    coordinator._check_connection_path = path_ok
    coordinator._finish_connect = hang

    with pytest.raises(TimeoutError):
        asyncio.run(coordinator._complete_connect(client))
    assert client.disconnected


def test_normal_connect_is_not_affected(monkeypatch) -> None:
    coordinator = _coordinator()
    finished = []

    async def path_ok(client) -> None:
        return None

    async def finish(client) -> None:
        finished.append(client)

    coordinator._check_connection_path = path_ok
    coordinator._finish_connect = finish
    client = _Client()

    asyncio.run(coordinator._complete_connect(client))

    assert finished == [client]
    assert not client.disconnected


# ── Notification setup ────────────────────────────────────────────────


def test_failed_data_read_subscription_fails_the_attempt() -> None:
    coordinator = _coordinator()
    client = _Client({coordinator_module.DATA_READ_CHAR_UUID})

    with pytest.raises(BleakError, match="DATA_READ"):
        asyncio.run(coordinator._enable_notifications(client))


def test_failed_seed_subscription_fails_only_pin_gateways() -> None:
    seed = {coordinator_module.SEED_CHAR_UUID}

    asyncio.run(_coordinator()._enable_notifications(_Client(seed)))  # push-button: warning only

    with pytest.raises(BleakError, match="SEED"):
        asyncio.run(_coordinator(_pairing_method="pin")._enable_notifications(_Client(seed)))


# ── First-frame watchdog ──────────────────────────────────────────────


def _watched(monkeypatch, **attrs):
    monkeypatch.setattr(coordinator_module, "_FIRST_FRAME_TIMEOUT_S", 0)
    client = _Client()
    coordinator = _coordinator(_client=client, **attrs)
    dropped = []

    async def drop(c) -> None:
        dropped.append(c)

    coordinator._drop_stalled_link = drop
    return coordinator, client, dropped


def test_silent_link_is_dropped(monkeypatch) -> None:
    coordinator, client, dropped = _watched(monkeypatch)

    asyncio.run(coordinator._first_frame_watchdog(client, started=100.0))

    assert dropped == [client]


def test_link_that_produced_frames_is_kept(monkeypatch) -> None:
    coordinator, client, dropped = _watched(monkeypatch, _last_frame_time=101.0)

    asyncio.run(coordinator._first_frame_watchdog(client, started=100.0))

    assert dropped == []


def test_watchdog_ignores_a_link_that_was_replaced(monkeypatch) -> None:
    coordinator, client, dropped = _watched(monkeypatch)
    coordinator._client = _Client()

    asyncio.run(coordinator._first_frame_watchdog(client, started=100.0))

    assert dropped == []


# ── Startup queries ───────────────────────────────────────────────────


def test_failed_startup_query_drops_the_link(monkeypatch) -> None:
    """The task used to die silently, leaving no heartbeat on a live link."""
    scheduled = []
    client = _Client()
    coordinator = _coordinator(
        _client=client,
        _authenticated=True,
        gateway_info=SimpleNamespace(table_id=7),
        _metadata_loaded_tables=set(),
        _get_devices_loaded_tables=set(),
        _startup_bootstrap_table_id=7,
        _startup_bootstrap_task=None,
        hass=SimpleNamespace(async_create_task=lambda coro: scheduled.append(coro)),
    )
    coordinator._resolve_bootstrap_waiter = lambda *args: None

    async def write_fails(kind, table_id):
        raise BleakError("Not connected")

    coordinator._send_query_and_wait = write_fails

    asyncio.run(coordinator._bootstrap_table_queries(7))

    assert len(scheduled) == 1
    scheduled[0].close()
