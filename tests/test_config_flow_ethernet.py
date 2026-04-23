"""Tests for Ethernet-specific config flow behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.ha_onecontrol.config_flow import (
    OneControlConfigFlow,
    OneControlOptionsFlow,
)


class _DummyWriter:
    def __init__(self) -> None:
        self.closed = False
        self.wait_closed_called = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True


def test_async_can_connect_ethernet_success(monkeypatch) -> None:
    """Connectivity helper returns True when TCP connect succeeds."""
    writer = _DummyWriter()

    async def _fake_open_connection(host: str, port: int):
        assert host == "192.168.1.1"
        assert port == 6969
        return object(), writer

    monkeypatch.setattr(asyncio, "open_connection", _fake_open_connection)

    flow = OneControlConfigFlow()
    ok = asyncio.run(flow._async_can_connect_ethernet("192.168.1.1", 6969))

    assert ok is True
    assert writer.closed is True
    assert writer.wait_closed_called is True


def test_async_can_connect_ethernet_failure(monkeypatch) -> None:
    """Connectivity helper returns False when TCP connect fails."""

    async def _fake_open_connection(host: str, port: int):
        raise OSError("connection refused")

    monkeypatch.setattr(asyncio, "open_connection", _fake_open_connection)

    flow = OneControlConfigFlow()
    ok = asyncio.run(flow._async_can_connect_ethernet("192.168.1.1", 6969))

    assert ok is False


def test_options_flow_init_stores_entry_on_private_attr() -> None:
    """Options flow init should not write to read-only base config_entry property."""
    entry = SimpleNamespace(options={})

    flow = OneControlOptionsFlow(entry)  # type: ignore[arg-type]

    assert flow._config_entry is entry
