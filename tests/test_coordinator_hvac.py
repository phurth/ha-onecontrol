"""Unit tests for coordinator HVAC routing behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from custom_components.ha_onecontrol.coordinator import OneControlCoordinator, PendingHvacCommand
from custom_components.ha_onecontrol.protocol.events import DeviceIdentity, HvacZone


class _FakeIdsRuntime:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: list[dict[str, int]] = []

    async def send_hvac_command(self, **kwargs: int) -> bool:
        self.calls.append(dict(kwargs))
        return self.result


def test_async_set_hvac_routes_to_ids_runtime_for_ethernet() -> None:
    """Ethernet HVAC commands should use IDS-native sender and set pending guard."""
    ids_runtime = _FakeIdsRuntime(result=True)
    retry_keys: list[str] = []

    async def _async_send_command(_: bytes) -> None:
        raise AssertionError("Legacy HVAC path should not be used on Ethernet")

    state = SimpleNamespace(
        is_ethernet_gateway=True,
        _ids_runtime=ids_runtime,
        _cmd=SimpleNamespace(build_action_hvac=lambda *args, **kwargs: b""),
        async_send_command=_async_send_command,
        _pending_hvac={},
        _schedule_setpoint_retry=lambda key: retry_keys.append(key),
    )

    asyncio.run(
        OneControlCoordinator.async_set_hvac(
            cast(Any, state),
            table_id=0x03,
            device_id=0xCF,
            heat_mode=3,
            heat_source=1,
            fan_mode=2,
            low_trip_f=66,
            high_trip_f=79,
            is_setpoint_change=True,
            is_preset_change=False,
        )
    )

    assert len(ids_runtime.calls) == 1
    assert ids_runtime.calls[0]["table_id"] == 0x03
    assert ids_runtime.calls[0]["device_id"] == 0xCF

    pending = state._pending_hvac.get("03:cf")
    assert pending is not None
    assert pending.low_trip_f == 66
    assert pending.high_trip_f == 79
    assert pending.is_setpoint_change is True
    assert retry_keys == ["03:cf"]


def test_async_set_hvac_skips_pending_when_ids_send_fails() -> None:
    """When IDS HVAC send is unavailable, coordinator should not create pending guard."""
    ids_runtime = _FakeIdsRuntime(result=False)

    async def _async_send_command(_: bytes) -> None:
        raise AssertionError("Legacy HVAC path should not be used on Ethernet")

    state = SimpleNamespace(
        is_ethernet_gateway=True,
        _ids_runtime=ids_runtime,
        _cmd=SimpleNamespace(build_action_hvac=lambda *args, **kwargs: b""),
        async_send_command=_async_send_command,
        _pending_hvac={},
        _schedule_setpoint_retry=lambda *_: None,
    )

    asyncio.run(
        OneControlCoordinator.async_set_hvac(
            cast(Any, state),
            table_id=0x03,
            device_id=0xCF,
            heat_mode=1,
            heat_source=0,
            fan_mode=0,
            low_trip_f=65,
            high_trip_f=78,
            is_setpoint_change=False,
            is_preset_change=False,
        )
    )

    assert len(ids_runtime.calls) == 1
    assert state._pending_hvac == {}


def test_do_retry_setpoint_uses_ids_hvac_sender_on_ethernet() -> None:
    """Setpoint retries on Ethernet should use IDS-native HVAC sender."""
    ids_runtime = _FakeIdsRuntime(result=True)
    scheduled: list[str] = []

    state = SimpleNamespace(
        is_ethernet_gateway=True,
        _ids_runtime=ids_runtime,
        _cmd=SimpleNamespace(build_action_hvac=lambda *args, **kwargs: b""),
        async_send_command=lambda *_: None,
        _pending_hvac={
            "03:cf": PendingHvacCommand(
                table_id=0x03,
                device_id=0xCF,
                heat_mode=3,
                heat_source=1,
                fan_mode=2,
                low_trip_f=66,
                high_trip_f=79,
                is_setpoint_change=True,
                is_preset_change=False,
                sent_at=1.0,
                retry_count=0,
            )
        },
        _hvac_retry_handles={},
        _schedule_setpoint_retry=lambda key: scheduled.append(key),
    )

    asyncio.run(OneControlCoordinator._do_retry_setpoint(cast(Any, state), "03:cf"))

    assert len(ids_runtime.calls) == 1
    pending = state._pending_hvac["03:cf"]
    assert pending.retry_count == 1
    assert scheduled == ["03:cf"]


def test_update_observed_hvac_capability_seeds_from_identity_raw_capability() -> None:
    """Observed HVAC capability should include IDS raw capability bitmask hints."""
    state = SimpleNamespace(
        observed_hvac_capability={},
        _device_identities={
            "03:cf": DeviceIdentity(
                table_id=0x03,
                device_id=0xCF,
                protocol=2,
                device_type=16,
                device_instance=0,
                product_id=0,
                product_mac="",
                raw_device_capability=0x0D,  # Gas + HeatPump + MultiSpeedFan
            )
        },
    )
    zone = HvacZone(
        table_id=0x03,
        device_id=0xCF,
        heat_mode=0,
        heat_source=0,
        fan_mode=0,
        low_trip_f=66,
        high_trip_f=79,
        zone_status=1,
        indoor_temp_f=72.0,
        outdoor_temp_f=55.0,
        dtc_code=0,
    )

    OneControlCoordinator._update_observed_hvac_capability(cast(Any, state), "03:cf", zone)

    assert state.observed_hvac_capability["03:cf"] & 0x0D == 0x0D
