"""OneControl BLE integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OneControl from a config entry."""
    coordinator = OneControlCoordinator(hass, entry)

    # Store coordinator for platform setup
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Connect in a background task so bootstrap completion isn't blocked.
    hass.async_create_background_task(
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
        await coordinator.async_disconnect()

    return unload_ok
