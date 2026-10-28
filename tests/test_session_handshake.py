"""Tests for the REMOTE_CONTROL seed/key handshake and session-end handling."""

import asyncio
from types import SimpleNamespace

from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.coordinator import OneControlCoordinator
from custom_components.ha_onecontrol.protocol.ids_can_wire import IdsCanWireFrame

HOST = 0x64
DEVICE = 0xD6
OTHER = 0xE1


def _coordinator(**attrs: object) -> OneControlCoordinator:
    coordinator = object.__new__(OneControlCoordinator)
    defaults = dict(
        _rc_session_seed_future=None,
        _rc_session_key_future=None,
        _rc_session_expected_src=None,
        _rc_session_last_status_code=None,
        _rc_session_open=False,
        _rc_session_target=None,
        _rc_session_sid_bytes=b"\x00\x04",
        _rc_heartbeat_task=None,
        # Release bookkeeping added with the session janitor: a successful
        # handshake records the device and starts the janitor, which this
        # harness short-circuits with an already-running fake task.
        _rc_session_dirty={},
        _rc_session_confirmed=set(),
        _rc_session_awaiting_confirmation={},
        _rc_janitor_task=SimpleNamespace(done=lambda: False),
    )
    defaults.update(attrs)
    for name, value in defaults.items():
        setattr(coordinator, name, value)
    return coordinator


def _response(src: int, msg: int, payload: str) -> IdsCanWireFrame:
    return IdsCanWireFrame(
        dlc=len(bytes.fromhex(payload)),
        is_extended=True,
        can_id=0,
        message_type=0x81,
        source_address=src,
        target_address=HOST,
        message_data=msg,
        payload=bytes.fromhex(payload),
    )


# ── Response parsing ─────────────────────────────────────────────────


def test_key_response_with_status_byte_is_a_rejection() -> None:
    async def run() -> None:
        future = asyncio.get_running_loop().create_future()
        coordinator = _coordinator(_rc_session_key_future=future, _rc_session_expected_src=DEVICE)
        coordinator._handle_session_response(_response(DEVICE, 0x43, "00040d"))
        assert future.done() and future.exception() is not None
        assert coordinator._rc_session_last_status_code == 0x0D

    asyncio.run(run())


def test_bare_key_response_opens_the_session() -> None:
    async def run() -> None:
        future = asyncio.get_running_loop().create_future()
        coordinator = _coordinator(_rc_session_key_future=future, _rc_session_expected_src=DEVICE)
        coordinator._handle_session_response(_response(DEVICE, 0x43, "0004"))
        assert future.done() and future.exception() is None

    asyncio.run(run())


def test_seed_from_another_device_is_ignored() -> None:
    async def run() -> None:
        future = asyncio.get_running_loop().create_future()
        coordinator = _coordinator(_rc_session_seed_future=future, _rc_session_expected_src=DEVICE)
        coordinator._handle_session_response(_response(OTHER, 0x42, "00047f568721"))
        assert not future.done()
        coordinator._handle_session_response(_response(DEVICE, 0x42, "00047f568721"))
        assert future.result() == 0x7F568721

    asyncio.run(run())


def test_earlier_session_ending_does_not_tear_down_current_one() -> None:
    task = SimpleNamespace(done=lambda: False, cancelled=[])
    task.cancel = lambda: task.cancelled.append(True)
    coordinator = _coordinator(
        _rc_session_open=True, _rc_session_target=DEVICE, _rc_heartbeat_task=task
    )

    coordinator._handle_session_response(_response(OTHER, 0x45, "00040f"))
    assert coordinator._rc_session_open is True
    assert coordinator._rc_session_target == DEVICE
    assert task.cancelled == []

    coordinator._handle_session_response(_response(DEVICE, 0x44, "00040e"))
    assert coordinator._rc_session_open is False
    assert task.cancelled == [True]


# ── Handshake ────────────────────────────────────────────────────────


def _handshake_coordinator(key_answers: list[str], seeds: list[str], writes: list[str]):
    """Coordinator whose writes are answered by a scripted V1 relay device."""

    async def write(client, frame, label):
        writes.append(label)
        if label == "SESSION_REQUEST_SEED":
            answer = _response(DEVICE, 0x42, seeds.pop(0))
        elif label == "SESSION_TRANSMIT_KEY":
            answer = _response(DEVICE, 0x43, key_answers.pop(0))
        else:
            return
        asyncio.get_running_loop().call_soon(coordinator._handle_session_response, answer)

    async def noop(*args, **kwargs):
        return None

    coordinator = _coordinator(
        _rc_session_lock=asyncio.Lock(),
        _gateway_can_address=HOST,
        _can_time_source=None,
        _can_device_types={},
        _write_can_frame=write,
        _query_remote_control_sessions=noop,
        _advertise_can_local_host_identity=noop,
        _is_can_ble_v1_gateway=lambda: True,
        _rc_session_heartbeat=noop,
        hass=SimpleNamespace(
            async_create_background_task=lambda coro, name: (coro.close(), None)[1]
        ),
    )
    return coordinator


def test_stale_seed_key_rejection_recovers_with_fresh_seed(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_module, "_RC_SESSION_RESEED_SETTLE_S", 0)
    writes: list[str] = []

    async def run() -> bool:
        coordinator = _handshake_coordinator(
            key_answers=["00040d", "0004"],
            seeds=["0004978ced5e", "0004123035b9"],
            writes=writes,
        )
        ok = await coordinator._ensure_remote_control_session(object(), DEVICE)
        assert coordinator._rc_session_open is True
        assert coordinator._rc_session_target == DEVICE
        return ok

    assert asyncio.run(run()) is True
    assert writes == [
        "SESSION_REQUEST_SEED",
        "SESSION_TRANSMIT_KEY",
        "SESSION_REQUEST_SEED",
        "SESSION_TRANSMIT_KEY",
    ]


def test_repeated_key_rejection_fails_instead_of_faking_a_session(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_module, "_RC_SESSION_RESEED_SETTLE_S", 0)
    writes: list[str] = []

    async def run() -> bool:
        coordinator = _handshake_coordinator(
            key_answers=["00040d", "00040d"],
            seeds=["0004978ced5e", "0004123035b9"],
            writes=writes,
        )
        ok = await coordinator._ensure_remote_control_session(object(), DEVICE)
        assert coordinator._rc_session_open is False
        return ok

    assert asyncio.run(run()) is False
