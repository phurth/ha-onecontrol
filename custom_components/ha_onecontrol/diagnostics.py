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

from .climate_zones import (
    stored_zone_overrides,
    zone_override,
    zone_override_defaults,
    zone_settings,
)
from .const import (
    DOMAIN,
    HVAC_CAP_AC,
    HVAC_CAP_ELECTRIC_HEAT,
    HVAC_CAP_GAS,
    HVAC_CAP_HEAT_PUMP,
    HVAC_CAP_MULTISPEED_FAN,
)
from .coordinator import OneControlCoordinator
from .protocol.dtc_codes import get_name as dtc_get_name

# Keys to redact from config entry data (PII / secrets)
TO_REDACT_CONFIG = {"gateway_pin", "bluetooth_pin"}

_HVAC_CAPABILITY_NAMES = (
    (HVAC_CAP_GAS, "gas"),
    (HVAC_CAP_AC, "ac"),
    (HVAC_CAP_HEAT_PUMP, "heat_pump"),
    (HVAC_CAP_MULTISPEED_FAN, "multispeed_fan"),
    (HVAC_CAP_ELECTRIC_HEAT, "electric_heat"),
)


def _capability_names(capability: int) -> list[str]:
    """Decode learned HVAC capability bits into names."""
    return [name for bit, name in _HVAC_CAPABILITY_NAMES if capability & bit]


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
        "gateway_family": coordinator._gateway_family,
        "is_pin_gateway": coordinator.is_pin_gateway,
        "is_x180t_gateway": coordinator.is_x180t_gateway,
        "pin_bond_attempted": getattr(coordinator, "_pin_bond_attempted", None),
        "has_can_write": coordinator._has_can_write,
        "consecutive_reconnect_failures": coordinator._consecutive_failures,
        "pending_metadata_cmdids": len(coordinator._pending_metadata_cmdids),
        "pending_get_devices_cmdids": len(coordinator._pending_get_devices_cmdids),
        "cmd_correlation": dict(coordinator._cmd_correlation_stats),
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
        "source": (
            "ids_can_network"
            if coordinator.is_can_ble_gateway
            else "myrvlink_device_lock_status"
        ),
    }

    # ── CAN BLE / IDS-CAN state ─────────────────────────────────
    can_ble = {
        "confirmed": coordinator.is_can_ble_gateway,
    }
    if coordinator.is_can_ble_gateway:
        can_ble.update({
            "gateway_version": coordinator.can_ble_gateway_version,
            "can_read_subscribed": coordinator.can_read_subscribed,
            "local_host_address": f"0x{coordinator.gateway_can_address:02X}",
            "local_host_mac": coordinator.can_local_host_mac,
            "discovered_device_types": {
                f"0x{address:02X}": f"0x{device_type:02X}"
                for address, device_type in sorted(coordinator.can_device_types.items())
            },
            "queued_can_commands": coordinator.can_command_queue_size,
            "remote_control_session_open": coordinator.remote_control_session_open,
            "remote_control_session_target": (
                f"0x{coordinator.remote_control_session_target:02X}"
                if coordinator.remote_control_session_target is not None
                else None
            ),
        })

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
        zone_id = coordinator.entity_identity(zone.table_id, zone.device_id) or key
        hvacs[key] = {
            "heat_mode": zone.heat_mode,
            "fan_mode": zone.fan_mode,
            "current_temp": zone.indoor_temp_f,
            "low_trip": zone.low_trip_f,
            "high_trip": zone.high_trip_f,
            "name": coordinator.device_name(zone.table_id, zone.device_id),
            # What the zone is doing and which heat source it uses, the
            # capability learned from its status so far (HVAC_CAP_* bits), and
            # the per-zone settings in effect (see climate_zones.py).
            "zone_status": zone.zone_status,
            "heat_source": zone.heat_source,
            "learned_capability": coordinator.observed_hvac_capability.get(key, 0),
            "learned_capability_names": _capability_names(
                coordinator.observed_hvac_capability.get(key, 0)
            ),
            "settings_key": zone_id,
            "settings": zone_override(zone_settings(entry.options, zone_id))
            or zone_override_defaults(),
            "settings_overridden": zone_id in stored_zone_overrides(entry.options),
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
        "can_ble": can_ble,
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
        "identity": {
            "mode": coordinator.identity_mode,
            "tables": sorted(f"{t:02x}" for t in coordinator._identity_tables),
            "addresses": {
                token: f"{addr[0]:02x}:{addr[1]:02x}"
                for token, addr in sorted(coordinator._identity_address.items())
            },
        },
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
