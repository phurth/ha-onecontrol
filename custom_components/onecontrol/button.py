"""Button platform for OneControl BLE integration.

Creates button entities for:
  - Clear In-Motion Lockout (sends 0x55 arm → 100ms → 0xAA clear)

Reference: INTERNALS.md § In-Motion Lockout, Android requestLockoutClear()
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneControlCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OneControl button entities from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    async_add_entities([
        OneControlClearLockoutButton(coordinator, address),
    ])


class OneControlClearLockoutButton(
    CoordinatorEntity[OneControlCoordinator], ButtonEntity
):
    """Button to clear the in-motion lockout on the gateway.

    Sends the arm (0x55) + clear (0xAA) sequence via CAN_WRITE or
    DATA_WRITE fallback.  Throttled to one press per 5 seconds.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Clear In-Motion Lockout"
    _attr_icon = "mdi:car-brake-hold"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_clear_lockout"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def available(self) -> bool:
        """Available when connected and lockout state is known."""
        return (
            self.coordinator.connected
            and self.coordinator.system_lockout_level is not None
        )

    async def async_press(self) -> None:
        """Send lockout clear sequence to gateway."""
        _LOGGER.info("Lockout clear button pressed")
        await self.coordinator.async_clear_lockout()
