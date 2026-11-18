"""Per-zone climate settings stored in the config entry options.

The gateway does not say which HVAC modes a zone supports, nor how low or high
its thermostat can be set, and neither can be detected reliably (capability is
learned from the mode a zone is *set to* as well as from what it runs).  The
user can therefore narrow a zone's modes and setpoint range in the options
flow.  A zone without stored settings keeps the defaults — every mode,
40–95 °F — so nothing changes for installs that never open that page.

Settings are keyed by the zone's identity token (function name + instance,
e.g. ``fn00c0_00``) so they stay with the zone when the gateway renumbers its
device table.  Installs that key entities by address (IDS-CAN BLE gateways, or
no metadata) use the ``tt:dd`` address key instead.

This module deliberately has no Home Assistant imports: it is shared by the
options flow, the climate platform and diagnostics, and unit-tested directly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .const import (
    CONF_CLIMATE_ZONES,
    CONF_ZONE_HEAT_SOURCES,
    CONF_ZONE_MAX_TEMP,
    CONF_ZONE_MIN_TEMP,
    CONF_ZONE_MODES,
    DEFAULT_HVAC_MAX_TEMP_F,
    DEFAULT_HVAC_MIN_TEMP_F,
    HVAC_CAP_GAS,
    HVAC_CAP_HEAT_PUMP,
    HVAC_ZONE_HEAT_SOURCES,
    HVAC_ZONE_MODES,
)

_HEAT_SOURCE_CAPABILITY = {"gas": HVAC_CAP_GAS, "heat_pump": HVAC_CAP_HEAT_PUMP}


@dataclass(frozen=True)
class ZoneSettings:
    """What one climate zone offers.

    ``modes`` are the HVAC modes besides Off.  ``heat_sources`` are the heat
    sources the zone is declared to have; they only ever add to what is
    learned from the zone's status, never hide a learned one.
    """

    modes: tuple[str, ...] = HVAC_ZONE_MODES
    min_temp: int = DEFAULT_HVAC_MIN_TEMP_F
    max_temp: int = DEFAULT_HVAC_MAX_TEMP_F
    heat_sources: tuple[str, ...] = ()

    @property
    def capability(self) -> int:
        """HVAC_CAP_* bits for the declared heat sources."""
        bits = 0
        for source in self.heat_sources:
            bits |= _HEAT_SOURCE_CAPABILITY[source]
        return bits


DEFAULT_ZONE_SETTINGS = ZoneSettings()


def _as_temp(value: object, default: int) -> int:
    """Coerce a stored setpoint limit, clamped to the supported range."""
    if isinstance(value, bool):
        return default
    try:
        temp = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return max(DEFAULT_HVAC_MIN_TEMP_F, min(DEFAULT_HVAC_MAX_TEMP_F, temp))


def _as_choices(
    value: object, known: tuple[str, ...], default: tuple[str, ...]
) -> tuple[str, ...]:
    """Coerce a stored list of names: known ones only, in canonical order."""
    if isinstance(value, str) or not isinstance(value, Iterable):
        return default
    chosen = {name for name in value if isinstance(name, str)}
    return tuple(name for name in known if name in chosen)


def zone_settings(options: Mapping[str, Any] | None, zone_id: str | None) -> ZoneSettings:
    """Return the settings for *zone_id*, falling back to the defaults.

    Tolerant by design: stored options are not re-validated, and a malformed
    entry must leave the zone usable rather than break the climate entity.
    """
    if not options or not zone_id:
        return DEFAULT_ZONE_SETTINGS
    zones = options.get(CONF_CLIMATE_ZONES)
    if not isinstance(zones, Mapping):
        return DEFAULT_ZONE_SETTINGS
    raw = zones.get(zone_id)
    if not isinstance(raw, Mapping):
        return DEFAULT_ZONE_SETTINGS
    modes = _as_choices(
        raw.get(CONF_ZONE_MODES, HVAC_ZONE_MODES), HVAC_ZONE_MODES, HVAC_ZONE_MODES
    )
    heat_sources = _as_choices(
        raw.get(CONF_ZONE_HEAT_SOURCES, ()), HVAC_ZONE_HEAT_SOURCES, ()
    )
    min_temp = _as_temp(raw.get(CONF_ZONE_MIN_TEMP), DEFAULT_HVAC_MIN_TEMP_F)
    max_temp = _as_temp(raw.get(CONF_ZONE_MAX_TEMP), DEFAULT_HVAC_MAX_TEMP_F)
    if min_temp >= max_temp:
        min_temp, max_temp = DEFAULT_HVAC_MIN_TEMP_F, DEFAULT_HVAC_MAX_TEMP_F
    return ZoneSettings(
        modes=modes, min_temp=min_temp, max_temp=max_temp, heat_sources=heat_sources
    )


def _as_dict(settings: ZoneSettings) -> dict[str, Any]:
    return {
        CONF_ZONE_MODES: list(settings.modes),
        CONF_ZONE_MIN_TEMP: settings.min_temp,
        CONF_ZONE_MAX_TEMP: settings.max_temp,
        CONF_ZONE_HEAT_SOURCES: list(settings.heat_sources),
    }


def zone_override(settings: ZoneSettings) -> dict[str, Any] | None:
    """Return the dict to store for *settings*, or None when they are the defaults."""
    if settings == DEFAULT_ZONE_SETTINGS:
        return None
    return _as_dict(settings)


def zone_override_defaults() -> dict[str, Any]:
    """Return the default settings in stored form (for diagnostics)."""
    return _as_dict(DEFAULT_ZONE_SETTINGS)


def stored_zone_overrides(options: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return the stored per-zone settings, defensively copied."""
    raw = options.get(CONF_CLIMATE_ZONES) if options else None
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: dict(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, Mapping)
    }
