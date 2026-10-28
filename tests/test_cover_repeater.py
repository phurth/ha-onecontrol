"""Behavioural tests for the CAN cover repeater.

These exercise the repeater as a coroutine (rather than asserting on source
text) because the invariants it carries are timing and teardown invariants:

* travel time is measured from the first COMMAND written, not from the press,
  so a slow REMOTE_CONTROL session handshake cannot eat the run;
* getting the motor started is bounded separately from the travel window;
* the failsafe STOP is itself bounded, so a pathological write cannot wedge the
  ``finally`` teardown (which is what releases the session back to the panel);
* a newer command (generation bump) takes over without the old repeater sending
  a STOP, and the session is still released on that path.

The coordinator is built with ``object.__new__`` so no Home Assistant runtime is
needed; every collaborator the repeater awaits is replaced with a fake.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import pytest

from custom_components.ha_onecontrol import coordinator as coordinator_module
from custom_components.ha_onecontrol.coordinator import OneControlCoordinator

COVER_KEY = "00:0c"
DEVICE_ID = 0x0C
GATEWAY_ADDRESS = 0x11
EXTEND = 0x01
RETRACT = 0x02


class _FakeHass:
    """Just enough of HomeAssistant for ``async_create_background_task``."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def async_create_background_task(
        self, coro: Any, name: str | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


class RepeaterHarness:
    """Fake coordinator collaborators + captured calls."""

    def __init__(
        self,
        *,
        travel: float = 0.3,
        ceiling: float | None = None,
        session_open: bool = True,
        session_setup_delay: float = 0.0,
        session_ok: bool = True,
        hang: Callable[[str], bool] | None = None,
    ) -> None:
        self.coordinator = object.__new__(OneControlCoordinator)
        c = self.coordinator
        c._cover_travel_overrides = {}
        c._cover_travel_fallback_s = travel
        c._cover_gen = {COVER_KEY: 1}
        c._cover_command_tasks = {}
        c._client = object()
        c._connected = True
        c._can_read_subscribed = True
        c._rc_session_open = session_open
        c._rc_session_target = DEVICE_ID if session_open else None
        # Session-release bookkeeping the COMMAND paths touch.
        c._rc_session_dirty = {}
        c._rc_session_confirmed = set()
        c._rc_session_awaiting_confirmation = {}
        c._rc_janitor_task = None
        c._gateway_can_address = GATEWAY_ADDRESS
        c.hass = _FakeHass()

        if ceiling is not None:
            c._cover_safety_ceiling_s = lambda _travel: ceiling  # type: ignore[method-assign]

        self.travel = travel
        self.commands: list[float] = []
        self.stops: list[tuple[str, float]] = []
        self.session_setup_calls = 0
        self.closed_sessions: list[tuple[int | None, str]] = []
        self._session_setup_delay = session_setup_delay
        self._session_ok = session_ok
        self._hang = hang or (lambda label: False)

        async def _write_can_frame(client: Any, frame: Any, label: str = "") -> None:
            if self._hang(label):
                await asyncio.sleep(3600)
            if "STOP" in label:
                self.stops.append((label, time.monotonic()))
            else:
                self.commands.append(time.monotonic())

        async def _ensure_remote_control_session(client: Any, device_id: int) -> bool:
            self.session_setup_calls += 1
            if self._session_setup_delay:
                await asyncio.sleep(self._session_setup_delay)
            c._rc_session_open = self._session_ok
            c._rc_session_target = device_id if self._session_ok else None
            return self._session_ok

        async def _async_close_remote_control_session(
            device_id: int | None = None, reason: str = ""
        ) -> None:
            self.closed_sessions.append((device_id, reason))

        c._write_can_frame = _write_can_frame  # type: ignore[method-assign]
        c._ensure_remote_control_session = _ensure_remote_control_session  # type: ignore[method-assign]
        c._async_close_remote_control_session = _async_close_remote_control_session  # type: ignore[method-assign]

    def run(self, *, gen: int = 1, direction: int = EXTEND) -> Any:
        return self.coordinator._cover_command_repeater(
            COVER_KEY, DEVICE_ID, direction, gen
        )

    async def settle(self, timeout: float = 2.0) -> None:
        """Let scheduled background tasks (session release) run."""
        pending = [task for task in self.coordinator.hass.tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)


def test_travel_complete_sends_stop_after_travel_time() -> None:
    """A normal run resends the direction, then STOPs after the travel time."""

    async def scenario() -> RepeaterHarness:
        harness = RepeaterHarness(travel=0.4)
        await asyncio.wait_for(harness.run(), timeout=5)
        await harness.settle()
        return harness

    harness = asyncio.run(scenario())

    assert harness.commands, "the repeater never wrote a COMMAND"
    labels = [label for label, _ in harness.stops]
    assert labels == ["cover STOP (travel-complete)"]
    # Travel is measured from the first COMMAND, not from the press.
    elapsed = harness.stops[0][1] - harness.commands[0]
    assert 0.35 <= elapsed <= 1.0, f"stopped after {elapsed:.2f}s, expected ~0.4s"
    # The session is released back to the panel exactly once.
    assert harness.closed_sessions == [(DEVICE_ID, "cover movement finished")]


def test_travel_clock_starts_at_first_command_not_at_press() -> None:
    """A slow session handshake must not be charged against the travel time."""

    async def scenario() -> RepeaterHarness:
        # Session is not open yet: phase 1 has to set it up, which takes 0.8s.
        harness = RepeaterHarness(
            travel=0.4, session_open=False, session_setup_delay=0.8
        )
        await asyncio.wait_for(harness.run(), timeout=5)
        await harness.settle()
        return harness

    harness = asyncio.run(scenario())

    assert harness.session_setup_calls >= 1
    assert harness.commands, "the repeater never wrote a COMMAND"
    elapsed = harness.stops[0][1] - harness.commands[0]
    assert elapsed <= 1.0, (
        f"travel ran {elapsed:.2f}s for a 0.4s setting — setup time leaked into "
        "the travel window"
    )


def test_generation_bump_takes_over_without_stopping() -> None:
    """A newer command owns the motor; the old repeater exits without a STOP."""

    async def scenario() -> RepeaterHarness:
        harness = RepeaterHarness(travel=1.5)
        task = asyncio.ensure_future(harness.run())
        await asyncio.sleep(0.3)
        harness.coordinator._cover_gen[COVER_KEY] += 1
        await asyncio.wait_for(task, timeout=3)
        await harness.settle()
        return harness

    harness = asyncio.run(scenario())

    assert harness.commands, "the repeater never wrote a COMMAND"
    assert harness.stops == [], "a superseded repeater must not stop the motor"
    # ...but it must NOT hand the device back: the command that took over is
    # using that session, so closing it would force a re-open mid-movement.
    assert harness.closed_sessions == [], (
        "a superseded repeater closed the session the new command is using"
    )


def test_cancelled_repeater_does_not_write_stop_but_releases_session() -> None:
    """The user-STOP path sends its own STOP; the cancelled repeater stays quiet."""

    async def scenario() -> RepeaterHarness:
        harness = RepeaterHarness(travel=5.0)
        task = asyncio.ensure_future(harness.run())
        await asyncio.sleep(0.25)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await harness.settle()
        return harness

    harness = asyncio.run(scenario())

    assert harness.stops == []
    assert harness.closed_sessions == [(DEVICE_ID, "cover movement finished")]


def test_hung_command_write_falls_back_to_bounded_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that never returns must not wedge the repeater or its teardown."""
    monkeypatch.setattr(coordinator_module, "COVER_START_TIMEOUT_S", 0.2)
    monkeypatch.setattr(coordinator_module, "COVER_STOP_TIMEOUT_S", 0.2)

    async def scenario() -> tuple[RepeaterHarness, float]:
        harness = RepeaterHarness(travel=1.0, hang=lambda label: True)
        began = time.monotonic()
        await asyncio.wait_for(harness.run(), timeout=5)
        await harness.settle()
        return harness, time.monotonic() - began

    harness, elapsed = asyncio.run(scenario())

    # Every write hangs, so nothing lands — but each attempt was bounded, so the
    # repeater still finished and the session was still released.
    assert harness.commands == []
    assert harness.stops == []
    assert elapsed < 1.5, f"bounded paths took {elapsed:.2f}s"
    assert harness.closed_sessions == [(DEVICE_ID, "cover movement finished")]


def test_safety_ceiling_force_stops_a_wedged_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling is armed outside the loop, so it fires even while it spins."""
    monkeypatch.setattr(coordinator_module, "COVER_STOP_TIMEOUT_S", 0.5)

    async def scenario() -> RepeaterHarness:
        # The first COMMAND lands, then the link wedges: every later COMMAND
        # write never returns, so the loop is stuck inside an await.  Only a
        # ceiling armed outside the loop can still stop the motor.
        harness = RepeaterHarness(travel=10.0, ceiling=0.4)

        async def _write_can_frame(client: Any, frame: Any, label: str = "") -> None:
            if "STOP" in label:
                harness.stops.append((label, time.monotonic()))
                return
            harness.commands.append(time.monotonic())
            if len(harness.commands) > 1:
                await asyncio.sleep(3600)

        harness.coordinator._write_can_frame = _write_can_frame  # type: ignore[method-assign]
        await asyncio.wait_for(harness.run(), timeout=5)
        await harness.settle()
        return harness

    harness = asyncio.run(scenario())

    assert [label for label, _ in harness.stops] == ["cover STOP (safety-ceiling)"]
    assert harness.closed_sessions == [(DEVICE_ID, "cover movement finished")]


def test_retract_uses_retract_override() -> None:
    """Per-direction overrides resolve independently (extend vs retract)."""

    async def scenario() -> RepeaterHarness:
        # Fallback is 3s; the retract override is 1s (the minimum the
        # point-of-use clamp allows), so a 1s run proves the override applied.
        harness = RepeaterHarness(travel=3.0)
        harness.coordinator._cover_travel_overrides = {
            COVER_KEY: {"extend": 3.0, "retract": 1.0}
        }
        await asyncio.wait_for(harness.run(direction=RETRACT), timeout=5)
        await harness.settle()
        return harness

    harness = asyncio.run(scenario())

    elapsed = harness.stops[0][1] - harness.commands[0]
    assert 0.9 <= elapsed <= 1.6, (
        f"retract ignored its override ({elapsed:.2f}s, fallback is 3s)"
    )


def test_no_start_means_no_travel_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the session never opens, the start bound ends the run."""
    monkeypatch.setattr(coordinator_module, "COVER_START_TIMEOUT_S", 0.2)
    monkeypatch.setattr(coordinator_module, "COVER_STOP_TIMEOUT_S", 0.2)

    async def scenario() -> RepeaterHarness:
        harness = RepeaterHarness(
            travel=5.0, session_open=False, session_ok=False
        )
        await asyncio.wait_for(harness.run(), timeout=5)
        await harness.settle()
        return harness

    harness = asyncio.run(scenario())

    assert harness.commands == [], "no COMMAND can go out without a session"
    assert [label for label, _ in harness.stops] == ["cover STOP (start-timeout)"]
    assert harness.closed_sessions == [(DEVICE_ID, "cover movement finished")]
