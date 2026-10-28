"""Tests for releasing REMOTE_CONTROL sessions the panel needs back.

An IDS-CAN REMOTE_CONTROL session belongs to whoever opened it until it is
explicitly closed, and the device keeps it even when the BLE link drops.  Every
COMMAND (relay, queued replay, cover) therefore has to be followed by a release,
or the coach's physical panel stays locked out of that device until the gateway
is power-cycled.

The release is deliberately NOT on the command path: closing immediately after a
relay write produced session terminations (reason 0x0E), unconfirmed COMMANDs and
state that reverted a few seconds later.  These tests pin the janitor's rules:

* an idle session is released after the grace period;
* a session owned by a cover movement is left alone;
* a session owed from before a disconnect is still closed afterwards;
* releasing a stale device does not cancel the heartbeat of the live session.

The coordinator is built with ``object.__new__``; only the collaborators these
methods touch are replaced.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.coordinator import OneControlCoordinator

IDLE_DEVICE = 0x86
LIVE_DEVICE = 0x91
# Cover tasks are keyed by the "tt:dd" cover key the covers map uses.
IDLE_COVER_KEY = f"00:{IDLE_DEVICE:02x}"
LIVE_COVER_KEY = f"00:{LIVE_DEVICE:02x}"


class _FakeHass:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def async_create_background_task(
        self, coro: Any, name: str | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


class SessionHarness:
    def __init__(self, *, connected: bool = True) -> None:
        self.coordinator = object.__new__(OneControlCoordinator)
        c = self.coordinator
        c._client = object()
        c._connected = connected
        c._can_read_subscribed = connected
        c._gateway_can_address = 0x11
        c._rc_session_lock = asyncio.Lock()
        c._rc_session_open = True
        c._rc_session_target = IDLE_DEVICE
        c._rc_session_sid_bytes = bytes([0x00, 0x04])
        c._rc_session_dirty = {IDLE_DEVICE: time.monotonic()}
        c._rc_session_confirmed = set()
        c._rc_session_awaiting_confirmation = {}
        c._rc_janitor_task = None
        c._rc_heartbeat_task = None
        c._cover_command_tasks = {}
        c.hass = _FakeHass()
        self.frames: list[tuple[str, int, int]] = []  # (label, target, message_data)

    @property
    def c(self) -> Any:
        return self.coordinator

    async def _write_can_frame(self, client: Any, frame: Any, *, label: str = "") -> None:
        # IDS-CAN wire frame: [dlc][id(4)][payload...]; message_data is payload[0]
        payload = bytes(frame)
        self.frames.append((label, payload[5] if len(payload) > 5 else -1, len(payload)))

    def install(self, **covers: Any) -> None:
        self.c._write_can_frame = self._write_can_frame

    def closes(self) -> list[str]:
        return [label for label, _payload, _len in self.frames if "SESSION_CLOSE" in label]


def _install_writer(harness: SessionHarness) -> None:
    """Capture the CAN target address of each frame, not just the label."""

    async def _write(client: Any, frame: Any, *, label: str = "") -> None:
        raw = bytes(frame)
        # 29-bit extended frame: dlc, id[4] (target in the low byte), payload
        target = raw[5] if len(raw) > 5 else -1
        harness.frames.append((label, target, len(raw)))

    harness.c._write_can_frame = _write


@pytest.fixture(autouse=True)
def fast_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the grace periods and janitor interval for the tests."""
    monkeypatch.setattr(coordinator_module, "RC_SESSION_IDLE_RELEASE_S", 0.2)
    monkeypatch.setattr(coordinator_module, "RC_SESSION_CONFIRMED_RELEASE_S", 0.05)
    monkeypatch.setattr(coordinator_module, "RC_SESSION_JANITOR_INTERVAL_S", 0.05)


def test_idle_session_is_released() -> None:
    """A relay session goes quiet; the janitor hands the device back."""

    async def scenario() -> SessionHarness:
        harness = SessionHarness()
        _install_writer(harness)
        harness.c._rc_session_dirty = {IDLE_DEVICE: time.monotonic() - 1.0}
        await harness.c._async_reap_idle_rc_sessions()
        return harness

    harness = asyncio.run(scenario())

    assert harness.closes() == ["SESSION_CLOSE"]
    assert harness.c._rc_session_dirty == {}
    assert harness.c._rc_session_open is False
    assert harness.c._rc_session_target is None


def test_cover_movement_keeps_its_session() -> None:
    """A moving cover owns the session: the janitor must not close it."""

    async def scenario() -> SessionHarness:
        harness = SessionHarness()
        _install_writer(harness)
        harness.c._rc_session_dirty = {IDLE_DEVICE: time.monotonic() - 60.0}
        harness.c._cover_command_tasks = {IDLE_COVER_KEY: object()}
        await harness.c._async_reap_idle_rc_sessions()
        return harness

    harness = asyncio.run(scenario())

    assert harness.closes() == []
    assert harness.c._rc_session_open is True


def test_recent_command_keeps_its_session() -> None:
    """The grace period protects the confirmation window after a write."""

    async def scenario() -> SessionHarness:
        harness = SessionHarness()
        _install_writer(harness)
        harness.c._note_rc_session_command(IDLE_DEVICE)
        await harness.c._async_reap_idle_rc_sessions()
        return harness

    harness = asyncio.run(scenario())

    assert harness.closes() == []
    assert harness.c._rc_session_open is True


def test_session_owed_from_before_a_disconnect_is_released_after_it() -> None:
    """The device keeps the session across a link drop, so we still owe a close."""

    async def scenario() -> SessionHarness:
        harness = SessionHarness()
        _install_writer(harness)
        # Connected again, but this coordinator holds no live session: the map
        # is what remembers the session opened before the disconnect.
        harness.c._rc_session_open = False
        harness.c._rc_session_target = None
        harness.c._rc_session_dirty = {IDLE_DEVICE: time.monotonic() - 1.0}
        await harness.c._async_reap_idle_rc_sessions()
        return harness

    harness = asyncio.run(scenario())

    assert harness.closes() == ["SESSION_CLOSE"]
    assert harness.c._rc_session_dirty == {}


def test_releasing_a_stale_device_leaves_the_live_session_alone() -> None:
    """Closing device A must not cancel the heartbeat holding device B."""

    async def scenario() -> tuple[SessionHarness, bool]:
        harness = SessionHarness()
        _install_writer(harness)

        class _Heartbeat:
            cancelled = False

            def done(self) -> bool:
                return False

            def cancel(self) -> None:
                self.cancelled = True

        heartbeat = _Heartbeat()
        harness.c._rc_heartbeat_task = heartbeat
        harness.c._rc_session_target = LIVE_DEVICE
        harness.c._rc_session_dirty = {
            IDLE_DEVICE: time.monotonic() - 60.0,
            LIVE_DEVICE: time.monotonic(),
        }
        harness.c._cover_command_tasks = {LIVE_COVER_KEY: object()}

        await harness.c._async_reap_idle_rc_sessions()
        return harness, heartbeat.cancelled

    harness, cancelled = asyncio.run(scenario())

    assert harness.closes() == ["SESSION_CLOSE"]
    assert cancelled is False, "the live session's heartbeat was cancelled"
    assert harness.c._rc_session_open is True
    assert IDLE_DEVICE not in harness.c._rc_session_dirty
    assert LIVE_DEVICE in harness.c._rc_session_dirty


def test_janitor_runs_while_connected_and_stops_when_not() -> None:
    """The janitor loop closes idle sessions on its own, and exits on disconnect."""

    async def scenario() -> SessionHarness:
        harness = SessionHarness()
        _install_writer(harness)
        harness.c._rc_session_dirty = {IDLE_DEVICE: time.monotonic() - 1.0}
        harness.c._start_rc_session_janitor()
        await asyncio.sleep(0.2)
        harness.c._connected = False
        await asyncio.sleep(0.15)
        return harness

    harness = asyncio.run(scenario())

    assert harness.closes() == ["SESSION_CLOSE"]
    assert harness.c._rc_session_dirty == {}


def test_release_all_sessions_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teardown must not hang on a wedged write."""
    monkeypatch.setattr(coordinator_module, "RC_SESSION_RELEASE_TIMEOUT_S", 0.2)

    async def scenario() -> tuple[SessionHarness, float]:
        harness = SessionHarness()

        async def _hang(client: Any, frame: Any, *, label: str = "") -> None:
            await asyncio.sleep(3600)

        harness.c._write_can_frame = _hang
        harness.c._rc_session_dirty = {IDLE_DEVICE: time.monotonic()}
        began = time.monotonic()
        await harness.c._async_release_all_rc_sessions("coordinator shutting down")
        return harness, time.monotonic() - began

    harness, elapsed = asyncio.run(scenario())

    assert elapsed < 1.0, f"teardown blocked for {elapsed:.2f}s"
    # The write never went out, so the debt is kept and re-stamped — retried on
    # a later pass rather than abandoned (the panel is still locked out).
    assert IDLE_DEVICE in harness.c._rc_session_dirty
    assert harness.c._rc_session_dirty[IDLE_DEVICE] > 0


def test_failed_release_is_retried_after_another_grace_period() -> None:
    """A close that fails must not spin, and must not be forgotten."""

    async def scenario() -> tuple[SessionHarness, float]:
        harness = SessionHarness()

        async def _fail(client: Any, frame: Any, *, label: str = "") -> None:
            raise RuntimeError("BLE write failed")

        harness.c._write_can_frame = _fail
        harness.c._rc_session_dirty = {IDLE_DEVICE: time.monotonic() - 60.0}
        began = time.monotonic()
        # Two janitor passes back to back: the second must skip the device
        # because the failure re-stamped it.
        await harness.c._async_reap_idle_rc_sessions()
        await harness.c._async_reap_idle_rc_sessions()
        return harness, time.monotonic() - began

    harness, _elapsed = asyncio.run(scenario())

    assert IDLE_DEVICE in harness.c._rc_session_dirty
    assert time.monotonic() - harness.c._rc_session_dirty[IDLE_DEVICE] < 5.0


def test_confirmed_command_is_released_sooner_than_an_unconfirmed_one() -> None:
    """A confirmed COMMAND frees the panel as soon as it has landed.

    The panel is refused for as long as the session is held, so the window is
    sized by what the COMMAND needs, not by how long confirmation may take.
    """

    async def scenario() -> tuple[SessionHarness, SessionHarness]:
        now = time.monotonic()

        unconfirmed = SessionHarness()
        _install_writer(unconfirmed)
        unconfirmed.c._rc_session_dirty = {IDLE_DEVICE: now - 0.1}

        confirmed = SessionHarness()
        _install_writer(confirmed)
        confirmed.c._rc_session_dirty = {IDLE_DEVICE: now - 0.1}
        confirmed.c._mark_rc_session_confirmed(IDLE_DEVICE)

        await unconfirmed.c._async_reap_idle_rc_sessions()
        await confirmed.c._async_reap_idle_rc_sessions()
        return unconfirmed, confirmed

    unconfirmed, confirmed = asyncio.run(scenario())

    assert unconfirmed.closes() == [], "an unconfirmed COMMAND keeps its grace period"
    assert confirmed.closes() == ["SESSION_CLOSE"]
    assert IDLE_DEVICE in unconfirmed.c._rc_session_dirty
    assert confirmed.c._rc_session_dirty == {}


def test_new_command_clears_the_previous_confirmation() -> None:
    """A fresh write is unconfirmed again until its own feedback arrives."""

    async def scenario() -> SessionHarness:
        harness = SessionHarness()
        _install_writer(harness)
        harness.c._mark_rc_session_confirmed(IDLE_DEVICE)
        harness.c._note_rc_session_command(IDLE_DEVICE)
        await asyncio.sleep(0.12)  # past the confirmed grace, inside the idle one
        await harness.c._async_reap_idle_rc_sessions()
        return harness

    harness = asyncio.run(scenario())

    assert harness.closes() == []
    assert harness.c._rc_session_confirmed == set()


def test_close_forgets_the_confirmation() -> None:
    """Nothing stale is left behind for the next command on that device."""

    async def scenario() -> SessionHarness:
        harness = SessionHarness()
        _install_writer(harness)
        harness.c._mark_rc_session_confirmed(IDLE_DEVICE)
        await harness.c._async_close_remote_control_session(IDLE_DEVICE, "test")
        return harness

    harness = asyncio.run(scenario())

    assert harness.closes() == ["SESSION_CLOSE"]
    assert harness.c._rc_session_confirmed == set()


def test_relay_command_marks_its_session_confirmed() -> None:
    """A confirmed relay COMMAND must leave the session confirmed, not cleared.

    Ordering matters here: stamping the write clears any previous confirmation,
    so the confirmation has to be recorded *after* the write is noted.
    """

    async def scenario() -> SessionHarness:
        harness = SessionHarness()

        async def _advertise(*args: Any, **kwargs: Any) -> None:
            return None

        async def _ensure(client: Any, device_id: int) -> bool:
            harness.c._rc_session_open = True
            harness.c._rc_session_target = device_id
            return True

        async def _confirmed(device_id: int, state: bool, *, timeout: float) -> bool:
            return True

        harness.c._advertise_can_local_host_identity = _advertise
        harness.c._ensure_remote_control_session = _ensure
        harness.c._wait_for_can_relay_state = _confirmed
        harness.c._can_ble_gateway_version = "Unknown"
        harness.c.relays = {}
        harness.c._is_can_ble_v1_gateway = lambda: False
        _install_writer(harness)

        await harness.c.async_can_switch(IDLE_DEVICE, True)
        return harness

    harness = asyncio.run(scenario())

    assert IDLE_DEVICE in harness.c._rc_session_confirmed
    assert IDLE_DEVICE in harness.c._rc_session_dirty


def test_release_all_sessions_shares_one_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teardown gets ONE bound for the whole sweep, not one per device.

    Otherwise three owed sessions can hold up an unload for three times the
    timeout on a single wedged write.
    """
    monkeypatch.setattr(coordinator_module, "RC_SESSION_RELEASE_TIMEOUT_S", 0.2)

    async def scenario() -> float:
        harness = SessionHarness()

        async def _hang(client: Any, frame: Any, *, label: str = "") -> None:
            await asyncio.sleep(3600)

        harness.c._write_can_frame = _hang
        now = time.monotonic()
        harness.c._rc_session_dirty = {
            IDLE_DEVICE: now,
            LIVE_DEVICE: now,
            0x0C: now,
        }
        began = time.monotonic()
        await harness.c._async_release_all_rc_sessions("coordinator shutting down")
        return time.monotonic() - began

    elapsed = asyncio.run(scenario())

    # Per-device bounds would take >= 0.6s here (3 x 0.2s).
    assert elapsed < 0.4, f"teardown was bounded per device ({elapsed:.2f}s)"


def test_relay_command_is_stamped_before_the_write() -> None:
    """The session must be stamped BEFORE the frame goes out.

    A janitor tick during the write sees the previous command's grace period
    otherwise, and can close the session out from under an in-flight command.
    """

    async def scenario() -> list[bool]:
        harness = SessionHarness()
        stamped_at_write: list[bool] = []

        async def _advertise(*args: Any, **kwargs: Any) -> None:
            return None

        async def _ensure(client: Any, device_id: int) -> bool:
            harness.c._rc_session_open = True
            harness.c._rc_session_target = device_id
            return True

        async def _confirmed(device_id: int, state: bool, *, timeout: float) -> bool:
            return True

        async def _write(client: Any, frame: Any, *, label: str = "") -> None:
            if label == "relay COMMAND":
                stamped_at_write.append(IDLE_DEVICE in harness.c._rc_session_dirty)

        harness.c._advertise_can_local_host_identity = _advertise
        harness.c._ensure_remote_control_session = _ensure
        harness.c._wait_for_can_relay_state = _confirmed
        harness.c._can_ble_gateway_version = "Unknown"
        harness.c.relays = {}
        harness.c._is_can_ble_v1_gateway = lambda: False
        harness.c._write_can_frame = _write

        await harness.c.async_can_switch(IDLE_DEVICE, True)
        return stamped_at_write

    stamped = asyncio.run(scenario())

    assert stamped == [True], (
        "the COMMAND was written before its session was stamped"
    )


def test_janitor_waits_for_an_outstanding_command() -> None:
    """A COMMAND inside its verify window owns the session, retry included.

    The unconfirmed grace (2 s) is shorter than the V1 relay verify window
    (5 s), so without this the janitor can release the session while the
    COMMAND is still waiting for DEVICE_STATUS — exactly the race the release
    is supposed to stay out of.
    """

    async def scenario() -> SessionHarness:
        harness = SessionHarness()
        _install_writer(harness)
        harness.c._rc_session_dirty = {IDLE_DEVICE: time.monotonic() - 60.0}
        harness.c._claim_rc_session(IDLE_DEVICE)
        await harness.c._async_reap_idle_rc_sessions()
        return harness

    harness = asyncio.run(scenario())

    assert harness.closes() == [], "the janitor released a session mid-verify"
    assert IDLE_DEVICE in harness.c._rc_session_dirty
    assert harness.c._rc_session_open is True


def _pending_probe(harness: "SessionHarness", seen: list[bool], *, results: list[bool]) -> Any:
    """Return a ``_wait_for_can_relay_state`` fake recording the claim state."""

    async def _wait(device_id: int, state: bool, *, timeout: float) -> bool:
        seen.append(device_id in harness.c._rc_session_awaiting_confirmation)
        return results.pop(0)

    return _wait


def _relay_harness(harness: "SessionHarness") -> None:
    """Wire a SessionHarness up for ``async_can_switch`` (deps only)."""

    async def _advertise(*args: Any, **kwargs: Any) -> None:
        return None

    async def _ensure(client: Any, device_id: int) -> bool:
        harness.c._rc_session_open = True
        harness.c._rc_session_target = device_id
        return True

    harness.c._advertise_can_local_host_identity = _advertise
    harness.c._ensure_remote_control_session = _ensure
    harness.c._can_ble_gateway_version = "Unknown"
    harness.c.relays = {}
    harness.c._is_can_ble_v1_gateway = lambda: False


def test_relay_command_holds_the_session_through_the_verify_window() -> None:
    """The session is held while the COMMAND waits, and released after."""

    async def scenario() -> tuple[list[bool], SessionHarness]:
        harness = SessionHarness()
        _install_writer(harness)
        pending_during_wait: list[bool] = []
        _relay_harness(harness)
        harness.c._wait_for_can_relay_state = _pending_probe(
            harness, pending_during_wait, results=[True]
        )
        await harness.c.async_can_switch(IDLE_DEVICE, True)
        return pending_during_wait, harness

    pending_during_wait, harness = asyncio.run(scenario())

    assert pending_during_wait == [True], (
        "the janitor was free to reap the session during the verify window"
    )
    assert IDLE_DEVICE not in harness.c._rc_session_awaiting_confirmation, (
        "the confirmation claim outlived the verify window"
    )


def test_relay_retry_also_holds_the_session() -> None:
    """The retry's verify wait is covered too, not just the first one."""

    async def scenario() -> tuple[list[bool], SessionHarness]:
        harness = SessionHarness()
        _install_writer(harness)
        pending_during_wait: list[bool] = []
        _relay_harness(harness)
        harness.c._wait_for_can_relay_state = _pending_probe(
            harness, pending_during_wait, results=[False, True]
        )
        await harness.c.async_can_switch(IDLE_DEVICE, True)
        return pending_during_wait, harness

    pending_during_wait, harness = asyncio.run(scenario())

    assert pending_during_wait == [True, True], (
        f"a verify wait ran without the claim: {pending_during_wait}"
    )
    assert IDLE_DEVICE not in harness.c._rc_session_awaiting_confirmation


def test_closing_clears_a_stale_confirmation_claim() -> None:
    """A release must drop any claim, so it can never block later reaps."""

    async def scenario() -> SessionHarness:
        harness = SessionHarness()
        _install_writer(harness)
        harness.c._claim_rc_session(IDLE_DEVICE)
        await harness.c._async_close_remote_control_session(IDLE_DEVICE, "test")
        return harness

    harness = asyncio.run(scenario())

    assert IDLE_DEVICE not in harness.c._rc_session_awaiting_confirmation


def test_claim_is_taken_before_the_session_handshake() -> None:
    """The claim must be visible before ``_ensure_remote_control_session``.

    The handshake holds ``_rc_session_lock`` for about a second on V1 gateways.
    A janitor that decided to reap the previous session is queued on that lock,
    so the claim has to be in place before the handshake starts, not after it —
    otherwise the janitor gets the lock, finds no claim, and closes right behind
    the COMMAND that just went out.
    """

    async def scenario() -> list[bool]:
        harness = SessionHarness()
        _install_writer(harness)
        _relay_harness(harness)
        claimed_at_handshake: list[bool] = []

        async def _ensure(client: Any, device_id: int) -> bool:
            claimed_at_handshake.append(
                device_id in harness.c._rc_session_awaiting_confirmation
            )
            harness.c._rc_session_open = True
            harness.c._rc_session_target = device_id
            return True

        async def _confirmed(device_id: int, state: bool, *, timeout: float) -> bool:
            return True

        harness.c._ensure_remote_control_session = _ensure
        harness.c._wait_for_can_relay_state = _confirmed

        await harness.c.async_can_switch(IDLE_DEVICE, True)
        return claimed_at_handshake

    assert asyncio.run(scenario()) == [True], (
        "the session was not claimed before the handshake"
    )


def test_janitor_rechecks_under_the_lock() -> None:
    """A command that claims the device while the janitor waits is not reaped.

    Reproduces the V1 re-toggle: the janitor's pre-check passes, the handshake
    holds the lock, a command claims the device, and then the janitor gets the
    lock.  It has to look again before closing.
    """

    async def scenario() -> tuple[SessionHarness, list[str]]:
        harness = SessionHarness()
        _install_writer(harness)
        harness.c._rc_session_dirty = {IDLE_DEVICE: time.monotonic() - 60.0}
        order: list[str] = []

        async def _janitor() -> None:
            await harness.c._async_reap_idle_rc_sessions()
            order.append("janitor")

        # Stand in for the handshake holding the lock, then let the janitor
        # queue on it while a command claims the device.
        await harness.c._rc_session_lock.acquire()
        try:
            janitor = asyncio.ensure_future(_janitor())
            await asyncio.sleep(0.05)
            harness.c._claim_rc_session(IDLE_DEVICE)
            order.append("claim")
        finally:
            harness.c._rc_session_lock.release()
        await janitor
        return harness, order

    harness, order = asyncio.run(scenario())

    assert order == ["claim", "janitor"], f"the janitor did not wait on the lock: {order}"
    assert harness.closes() == [], "the janitor closed a session a command had claimed"
    assert IDLE_DEVICE in harness.c._rc_session_dirty


def test_overlapping_commands_hold_the_claim_until_the_last_one_finishes() -> None:
    """Two commands to one device: the first to finish must not clear the claim."""

    async def scenario() -> tuple[SessionHarness, int | None, int | None, int | None]:
        harness = SessionHarness()
        _install_writer(harness)
        _relay_harness(harness)

        gates = [asyncio.Event(), asyncio.Event()]
        entered = 0

        async def _wait(device_id: int, state: bool, *, timeout: float) -> bool:
            nonlocal entered
            gate = gates[min(entered, 1)]
            entered += 1
            await gate.wait()
            return True

        harness.c._wait_for_can_relay_state = _wait
        tasks = [
            asyncio.ensure_future(harness.c.async_can_switch(IDLE_DEVICE, True)),
            asyncio.ensure_future(harness.c.async_can_switch(IDLE_DEVICE, False)),
        ]
        while entered < 2:
            await asyncio.sleep(0.01)
        both_held = harness.c._rc_session_awaiting_confirmation.get(IDLE_DEVICE)

        gates[0].set()
        await tasks[0]
        one_left = harness.c._rc_session_awaiting_confirmation.get(IDLE_DEVICE)

        gates[1].set()
        await tasks[1]
        after = harness.c._rc_session_awaiting_confirmation.get(IDLE_DEVICE)
        return harness, both_held, one_left, after

    harness, both_held, one_left, after = asyncio.run(scenario())

    assert both_held == 2, f"the second command did not add its own claim: {both_held}"
    assert one_left == 1, "the first command to finish cleared the second one's claim"
    assert after is None, "the claim outlived the last command"
    assert harness.c._rc_session_awaiting_confirmation == {}
