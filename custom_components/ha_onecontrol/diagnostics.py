"""Diagnostics support for OneControl BLE integration.

Provides a one-click "Download diagnostics" button on the integration page
that dumps all coordinator state for troubleshooting.

Reference: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import OneControlCoordinator
from .protocol.dtc_codes import get_name as dtc_get_name

# Keys to redact from config entry data (PII / secrets)
TO_REDACT_CONFIG = {"gateway_pin", "bluetooth_pin"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]

    # ── Connection state ─────────────────────────────────────────
    connection = {
        "connected": coordinator.connected,
        "authenticated": coordinator.authenticated,
        "data_healthy": coordinator.data_healthy,
        "last_event_age_seconds": (
            round(coordinator.last_event_age, 1)
            if coordinator.last_event_age is not None
            else None
        ),
        "pairing_method": coordinator._pairing_method,
        "is_pin_gateway": coordinator.is_pin_gateway,
        "pin_bond_attempted": coordinator._pin_bond_attempted,
        "has_can_write": coordinator._has_can_write,
        "consecutive_reconnect_failures": coordinator._consecutive_failures,
    }

    # ── Gateway info ─────────────────────────────────────────────
    gw = coordinator.gateway_info
    gateway = {}
    if gw:
        gateway = {
            "table_id": gw.table_id,
            "device_count": gw.device_count,
            "protocol_version": getattr(gw, "protocol_version", None),
        }

    # ── RV status ────────────────────────────────────────────────
    rv = coordinator.rv_status
    rv_status = {}
    if rv:
        rv_status = {
            "voltage": rv.voltage,
            "temperature": rv.temperature,
        }

    # ── Lockout ──────────────────────────────────────────────────
    lockout = {
        "system_lockout_level": coordinator.system_lockout_level,
    }

    # ── Relays (switches) ────────────────────────────────────────
    relays = {}
    for key, relay in coordinator.relays.items():
        relays[key] = {
            "is_on": relay.is_on,
            "dtc_code": relay.dtc_code,
            "dtc_name": dtc_get_name(relay.dtc_code) if relay.dtc_code else None,
            "name": coordinator.device_name(relay.table_id, relay.device_id),
        }

    # ── Dimmable lights ──────────────────────────────────────────
    dimmables = {}
    for key, light in coordinator.dimmable_lights.items():
        dimmables[key] = {
            "brightness": light.brightness,
            "name": coordinator.device_name(light.table_id, light.device_id),
        }

    # ── RGB lights ───────────────────────────────────────────────
    rgbs = {}
    for key, light in coordinator.rgb_lights.items():
        rgbs[key] = {
            "red": light.red,
            "green": light.green,
            "blue": light.blue,
            "mode": light.mode,
            "name": coordinator.device_name(light.table_id, light.device_id),
        }

    # ── HVAC zones ───────────────────────────────────────────────
    hvacs = {}
    for key, zone in coordinator.hvac_zones.items():
        hvacs[key] = {
            "heat_mode": zone.heat_mode,
            "fan_mode": zone.fan_mode,
            "current_temp": zone.current_temp,
            "low_trip": zone.low_trip,
            "high_trip": zone.high_trip,
            "name": coordinator.device_name(zone.table_id, zone.device_id),
        }

    # ── Tanks ────────────────────────────────────────────────────
    tanks = {}
    for key, tank in coordinator.tanks.items():
        tanks[key] = {
            "level": tank.level,
            "name": coordinator.device_name(tank.table_id, tank.device_id),
        }

    # ── Covers ───────────────────────────────────────────────────
    covers = {}
    for key, cover in coordinator.covers.items():
        covers[key] = {
            "status": cover.status,
            "position": cover.position,
            "name": coordinator.device_name(cover.table_id, cover.device_id),
        }

    # ── Generators ───────────────────────────────────────────────
    generators = {}
    for key, gen in coordinator.generators.items():
        generators[key] = {
            "is_running": gen.is_running,
            "name": coordinator.device_name(gen.table_id, gen.device_id),
        }

    # ── Device metadata ──────────────────────────────────────────
    metadata = {}
    for key, meta in coordinator._metadata_raw.items():
        metadata[key] = {
            "function_name": meta.function_name,
            "function_instance": meta.function_instance,
            "resolved_name": coordinator.device_names.get(key, "unknown"),
        }

    return {
        "config_entry": async_redact_data(entry.as_dict(), TO_REDACT_CONFIG),
        "connection": connection,
        "gateway": gateway,
        "rv_status": rv_status,
        "lockout": lockout,
        "devices": {
            "relays": relays,
            "dimmable_lights": dimmables,
            "rgb_lights": rgbs,
            "hvac_zones": hvacs,
            "tanks": tanks,
            "covers": covers,
            "generators": generators,
        },
        "metadata": metadata,
        "device_count": {
            "relays": len(relays),
            "dimmable_lights": len(dimmables),
            "rgb_lights": len(rgbs),
            "hvac_zones": len(hvacs),
            "tanks": len(tanks),
            "covers": len(covers),
            "generators": len(generators),
            "total_metadata": len(metadata),
        },
    }
