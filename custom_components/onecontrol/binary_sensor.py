"""Binary sensor platform for OneControl BLE integration.

Creates binary_sensor entities for:
  - Gateway connectivity (diagnostic)
  - Gateway authenticated (diagnostic)

Device online/offline sensors are intentionally NOT created here.
The gateway reports DeviceOnline events for all device IDs including
phantom entries beyond device_count.  Per-device connectivity is better
represented through each entity's availability.

Reference: INTERNALS.md § Event Types
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    """Set up OneControl binary sensors from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    async_add_entities([
        OneControlGatewayConnectivity(coordinator, address),
        OneControlGatewayAuthenticated(coordinator, address),
        OneControlInMotionLockout(coordinator, address),
        OneControlDataHealthy(coordinator, address),
    ])


class OneControlGatewayConnectivity(
    CoordinatorEntity[OneControlCoordinator], BinarySensorEntity
):
    """Binary sensor showing if the BLE gateway is connected."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Gateway Connected"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_gateway_connected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.connected


class OneControlGatewayAuthenticated(
    CoordinatorEntity[OneControlCoordinator], BinarySensorEntity
):
    """Binary sensor showing if Step 2 authentication completed."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Gateway Authenticated"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_gateway_authenticated"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.authenticated


class OneControlInMotionLockout(
    CoordinatorEntity[OneControlCoordinator], BinarySensorEntity
):
    """Binary sensor showing if the RV in-motion lockout is active.

    When active (lockout_level > 0), the gateway prevents device control
    because the vehicle is in motion.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "In-Motion Lockout"
    _attr_icon = "mdi:car-brake-alert"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_in_motion_lockout"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def available(self) -> bool:
        """Only available once we've received at least one DeviceLockStatus."""
        return self.coordinator.system_lockout_level is not None

    @property
    def is_on(self) -> bool | None:
        level = self.coordinator.system_lockout_level
        if level is None:
            return None
        return level > 0

    @property
    def extra_state_attributes(self) -> dict:
        level = self.coordinator.system_lockout_level
        if level is None:
            return {}
        return {"lockout_level": level}


class OneControlDataHealthy(
    CoordinatorEntity[OneControlCoordinator], BinarySensorEntity
):
    """Binary sensor showing if data is being received from the gateway.

    Turns off if no events received for >15 seconds.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Data Healthy"
    _attr_icon = "mdi:heart-pulse"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_data_healthy"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.data_healthy

    @property
    def extra_state_attributes(self) -> dict:
        age = self.coordinator.last_event_age
        if age is None:
            return {}
        return {"last_event_age_seconds": round(age, 1)}
