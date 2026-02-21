"""Sensor platform for OneControl BLE integration.

Creates sensor entities for:
  - System voltage (RvStatus event 0x07)
  - System temperature (RvStatus event 0x07)
  - Tank levels (TankSensorStatus events 0x0C / 0x1B)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, UnitOfElectricPotential, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneControlCoordinator
from .protocol.events import RvStatus, TankLevel

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OneControl sensors from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    entities: list[SensorEntity] = [
        OneControlVoltageSensor(coordinator, address),
        OneControlTemperatureSensor(coordinator, address),
    ]

    # Dynamically add tank sensors as they are discovered
    discovered_tanks: set[str] = set()

    @callback
    def _on_event(event: Any) -> None:
        """Handle events that might create new entities."""
        new_entities: list[SensorEntity] = []

        if isinstance(event, list):
            for item in event:
                if isinstance(item, TankLevel):
                    key = f"{item.table_id}_{item.device_id}"
                    if key not in discovered_tanks:
                        discovered_tanks.add(key)
                        new_entities.append(
                            OneControlTankSensor(coordinator, address, item.table_id, item.device_id)
                        )
        elif isinstance(event, TankLevel):
            key = f"{event.table_id}_{event.device_id}"
            if key not in discovered_tanks:
                discovered_tanks.add(key)
                new_entities.append(
                    OneControlTankSensor(coordinator, address, event.table_id, event.device_id)
                )

        if new_entities:
            async_add_entities(new_entities)

    coordinator.register_event_callback(_on_event)
    async_add_entities(entities)


class _OneControlSensorBase(CoordinatorEntity[OneControlCoordinator], SensorEntity):
    """Base class for OneControl sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator)
        self._address = address
        mac = address.replace(":", "").lower()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )
        self._mac = mac


class OneControlVoltageSensor(_OneControlSensorBase):
    """System voltage from RvStatus events."""

    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "System Voltage"
    _attr_icon = "mdi:car-battery"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{self._mac}_system_voltage"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data and "voltage" in data:
            return data["voltage"]
        return None


class OneControlTemperatureSensor(_OneControlSensorBase):
    """System temperature from RvStatus events."""

    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "System Temperature"
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{self._mac}_system_temperature"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data and "temperature" in data:
            return data["temperature"]
        return None


class OneControlTankSensor(_OneControlSensorBase):
    """Tank level sensor — created dynamically as tanks are discovered."""

    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gauge"
    _attr_name: str  # set in __init__

    def __init__(
        self,
        coordinator: OneControlCoordinator,
        address: str,
        table_id: int,
        device_id: int,
    ) -> None:
        super().__init__(coordinator, address)
        self._table_id = table_id
        self._device_id = device_id
        self._attr_unique_id = f"{self._mac}_tank_{table_id:02x}{device_id:02x}"
        self._attr_name = f"Tank {table_id}:{device_id}"
        self._level: int | None = None

        # Register for live tank updates
        self._unsub = coordinator.register_event_callback(self._on_event)

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        """Update level from incoming tank events."""
        targets: list[TankLevel] = []
        if isinstance(event, list):
            targets = [e for e in event if isinstance(e, TankLevel)]
        elif isinstance(event, TankLevel):
            targets = [event]

        for tank in targets:
            if tank.table_id == self._table_id and tank.device_id == self._device_id:
                self._level = tank.level
                self.async_write_ha_state()
                return

    @property
    def native_value(self) -> int | None:
        return self._level
