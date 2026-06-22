"""OneControl BLE integration for Home Assistant."""

from __future__ import annotations

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BLUETOOTH_PIN,
    CONF_GATEWAY_FAMILY,
    CONF_PAIRING_METHOD,
    GATEWAY_FAMILY_X180T,
    DOMAIN,
)
from .coordinator import OneControlCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    "binary_sensor",
    "button",
    "climate",
    "light",
    "sensor",
    "switch",
]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to new version."""
    _LOGGER.info(
        "Migrating OneControl entry %s from version %s",
        entry.entry_id,
        entry.version,
    )

    version = entry.version

    if version == 1:
        # v1→v2: drop table_id from device entity unique_ids.
        # Old format: {mac12hex}_{type}_{table:02x}{device:02x}
        # New format: {mac12hex}_{type}_{device:02x}
        _DEVICE_TYPES = (
            "switch", "gen_switch", "light", "rgb", "climate", "cover",
            "gen_quiet", "tank", "generator", "gen_battery", "gen_temp",
            "hourmeter",
        )
        pattern = re.compile(
            r"^([0-9a-f]{12})_("
            + "|".join(re.escape(t) for t in _DEVICE_TYPES)
            + r")_([0-9a-f]{2})([0-9a-f]{2})$"
        )

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)

        # Group old-format entries by their normalized (target) unique_id
        normalized: dict[str, list[er.RegistryEntry]] = {}
        for ent in entries:
            m = pattern.match(ent.unique_id or "")
            if m:
                mac, etype, _table, device = m.groups()
                norm_id = f"{mac}_{etype}_{device}"
                normalized.setdefault(norm_id, []).append(ent)

        for norm_id, ents in normalized.items():
            if len(ents) == 1:
                ent_reg.async_update_entity(ents[0].entity_id, new_unique_id=norm_id)
                _LOGGER.debug("Migration: renamed %s → %s", ents[0].unique_id, norm_id)
            else:
                # Multiple entries for the same logical device (e.g. table_id drifted).
                # Keep the most recently modified; remove the rest.
                ents_sorted = sorted(
                    ents,
                    key=lambda e: getattr(e, "modified_at", None) or 0,
                    reverse=True,
                )
                survivor = ents_sorted[0]
                for stale in ents_sorted[1:]:
                    ent_reg.async_remove(stale.entity_id)
                    _LOGGER.info(
                        "Migration: removed duplicate entity %s (unique_id=%s)",
                        stale.entity_id,
                        stale.unique_id,
                    )
                ent_reg.async_update_entity(survivor.entity_id, new_unique_id=norm_id)
                _LOGGER.info(
                    "Migration: renamed survivor %s → %s", survivor.unique_id, norm_id
                )

        hass.config_entries.async_update_entry(entry, version=2)
        _LOGGER.info(
            "Migration of OneControl entry %s to version 2 complete", entry.entry_id
        )
        version = 2

    if version == 2:
        if (
            entry.data.get(CONF_GATEWAY_FAMILY) == GATEWAY_FAMILY_X180T
            and entry.data.get(CONF_PAIRING_METHOD) != "pin"
            and CONF_BLUETOOTH_PIN in entry.data
        ):
            new_data = {
                key: value
                for key, value in entry.data.items()
                if key != CONF_BLUETOOTH_PIN
            }
            hass.config_entries.async_update_entry(
                entry,
                data=new_data,
                version=3,
            )
            _LOGGER.info(
                "Migrated X180T push-button entry %s by removing stale bluetooth_pin",
                entry.entry_id,
            )
            version = 3
        else:
            hass.config_entries.async_update_entry(entry, version=3)
            _LOGGER.info(
                "Migration of OneControl entry %s to version 3 complete", entry.entry_id
            )
            version = 3

    if version == 3:
        hass.config_entries.async_update_entry(entry, version=4)
        _LOGGER.info(
            "Migration of OneControl entry %s to version 4 complete", entry.entry_id
        )
        version = 4

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OneControl from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    existing: OneControlCoordinator | None = hass.data[DOMAIN].get(entry.entry_id)
    if existing is not None:
        _LOGGER.warning(
            "Stale OneControl coordinator detected for entry %s (instance=%s) — disconnecting before setup",
            entry.entry_id,
            getattr(existing, "instance_tag", "unknown"),
        )
        try:
            await existing.async_disconnect()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed disconnecting stale OneControl coordinator")

    coordinator = OneControlCoordinator(hass, entry)

    # Store coordinator for platform setup
    hass.data[DOMAIN][entry.entry_id] = coordinator
    _LOGGER.info(
        "Initialized OneControl coordinator for entry %s (instance=%s)",
        entry.entry_id,
        coordinator.instance_tag,
    )

    # Connect in a background task so bootstrap completion isn't blocked.  Tie it
    # to the entry (not the global loop) so Home Assistant cancels it on unload —
    # otherwise an in-flight connect ladder could outlive the entry and keep
    # driving the adapter after a reload.
    entry.async_create_background_task(
        hass,
        coordinator.async_connect(),
        "ha_onecontrol_initial_connect",
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: OneControlCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.info(
            "Unloading OneControl coordinator for entry %s (instance=%s)",
            entry.entry_id,
            coordinator.instance_tag,
        )
        await coordinator.async_disconnect()

    return unload_ok
