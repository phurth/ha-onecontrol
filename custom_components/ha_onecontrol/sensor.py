"""Sensor platform for OneControl BLE integration.

Creates sensor entities for:
  - System voltage (RvStatus event 0x07)
  - System temperature (RvStatus event 0x07) — diagnostic, unavailable when gateway lacks sensor
  - Tank levels (TankSensorStatus events 0x0C / 0x1B)
  - Generator status (event 0x0A)
  - Hour meter (event 0x0F)
  - Cover / slide / awning STATE (events 0x0D/0x0E) — no control, safety
  - Gateway diagnostics: device count, table ID, protocol version

Reference: INTERNALS.md § Event Types, § Cover / Slide / Awning
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
from homeassistant.const import (
    CONF_ADDRESS,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneControlCoordinator
from .protocol.events import CoverStatus, GeneratorStatus, HourMeter, TankLevel

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OneControl sensors from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    # Always-present sensors
    entities: list[SensorEntity] = [
        OneControlVoltageSensor(coordinator, address),
        OneControlTemperatureSensor(coordinator, address),
        # Diagnostic sensors
        OneControlDeviceCountSensor(coordinator, address),
        OneControlTableIdSensor(coordinator, address),
        OneControlProtocolVersionSensor(coordinator, address),
    ]

    discovered_tanks: set[str] = set()
    discovered_generators: set[str] = set()
    discovered_hour_meters: set[str] = set()
    discovered_covers: set[str] = set()

    @callback
    def _on_event(event: Any) -> None:
        """Dynamically add sensor entities as new devices appear."""
        new: list[SensorEntity] = []

        items = event if isinstance(event, list) else [event]
        for item in items:
            if isinstance(item, TankLevel):
                key = f"{item.table_id:02x}:{item.device_id:02x}"
                if key not in discovered_tanks:
                    discovered_tanks.add(key)
                    new.append(OneControlTankSensor(coordinator, address, item.table_id, item.device_id))

            elif isinstance(item, GeneratorStatus):
                key = f"{item.table_id:02x}:{item.device_id:02x}"
                if key not in discovered_generators:
                    discovered_generators.add(key)
                    new.extend([
                        OneControlGeneratorSensor(coordinator, address, item.table_id, item.device_id),
                        OneControlGeneratorBatterySensor(coordinator, address, item.table_id, item.device_id),
                        OneControlGeneratorTemperatureSensor(coordinator, address, item.table_id, item.device_id),
                    ])

            elif isinstance(item, HourMeter):
                key = f"{item.table_id:02x}:{item.device_id:02x}"
                if key not in discovered_hour_meters:
                    discovered_hour_meters.add(key)
                    new.append(OneControlHourMeterSensor(coordinator, address, item.table_id, item.device_id))

            elif isinstance(item, CoverStatus):
                key = f"{item.table_id:02x}:{item.device_id:02x}"
                if key not in discovered_covers:
                    discovered_covers.add(key)
                    new.append(OneControlCoverStateSensor(coordinator, address, item.table_id, item.device_id))

        if new:
            async_add_entities(new)

    coordinator.register_event_callback(_on_event)

    # Pre-discover from coordinator state
    for key, tank in coordinator.tanks.items():
        if key not in discovered_tanks:
            discovered_tanks.add(key)
            entities.append(OneControlTankSensor(coordinator, address, tank.table_id, tank.device_id))
    for key, gen in coordinator.generators.items():
        if key not in discovered_generators:
            discovered_generators.add(key)
            entities.extend([
                OneControlGeneratorSensor(coordinator, address, gen.table_id, gen.device_id),
                OneControlGeneratorBatterySensor(coordinator, address, gen.table_id, gen.device_id),
                OneControlGeneratorTemperatureSensor(coordinator, address, gen.table_id, gen.device_id),
            ])
    for key, hm in coordinator.hour_meters.items():
        if key not in discovered_hour_meters:
            discovered_hour_meters.add(key)
            entities.append(OneControlHourMeterSensor(coordinator, address, hm.table_id, hm.device_id))
    for key, cov in coordinator.covers.items():
        if key not in discovered_covers:
            discovered_covers.add(key)
            entities.append(OneControlCoverStateSensor(coordinator, address, cov.table_id, cov.device_id))

    async_add_entities(entities)


# ── Base ──────────────────────────────────────────────────────────────────


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


# ── System Sensors ────────────────────────────────────────────────────────


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
    """System temperature from RvStatus events.

    Many OneControl gateways don't have a temperature sensor; the gateway
    returns 0xFFFF / None in that case.  Mark entity unavailable so it
    doesn't clutter the dashboard.
    """

    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "System Temperature"
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{self._mac}_system_temperature"

    @property
    def available(self) -> bool:
        """Only available when gateway actually reports a temperature."""
        data = self.coordinator.data
        return bool(data and data.get("temperature") is not None)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data and "temperature" in data:
            return data["temperature"]
        return None


# ── Diagnostic Sensors ────────────────────────────────────────────────────


class OneControlDeviceCountSensor(_OneControlSensorBase):
    """Number of CAN bus devices reported by gateway."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Device Count"
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{self._mac}_device_count"

    @property
    def native_value(self) -> int | None:
        gi = self.coordinator.gateway_info
        return gi.device_count if gi else None


class OneControlTableIdSensor(_OneControlSensorBase):
    """Gateway CAN table ID."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Table ID"
    _attr_icon = "mdi:identifier"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{self._mac}_table_id"

    @property
    def native_value(self) -> int | None:
        gi = self.coordinator.gateway_info
        return gi.table_id if gi else None


class OneControlProtocolVersionSensor(_OneControlSensorBase):
    """Gateway protocol version."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Protocol Version"
    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{self._mac}_protocol_version"

    @property
    def native_value(self) -> int | None:
        gi = self.coordinator.gateway_info
        return gi.protocol_version if gi else None


# ── Tank Sensors ──────────────────────────────────────────────────────────


class OneControlTankSensor(_OneControlSensorBase):
    """Tank level sensor — created dynamically as tanks are discovered."""

    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gauge"

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
        self._key = f"{table_id:02x}:{device_id:02x}"
        self._attr_unique_id = f"{self._mac}_tank_{table_id:02x}{device_id:02x}"
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Level"

    @property
    def native_value(self) -> int | None:
        tank = self.coordinator.tanks.get(self._key)
        return tank.level if tank else None

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        items = event if isinstance(event, list) else [event]
        for item in items:
            if (
                isinstance(item, TankLevel)
                and item.table_id == self._table_id
                and item.device_id == self._device_id
            ):
                self.async_write_ha_state()
                return


# ── Generator / Hour Meter ────────────────────────────────────────────────


class OneControlGeneratorSensor(_OneControlSensorBase):
    """Generator running/stopped sensor — event 0x0A."""

    _attr_icon = "mdi:engine"

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
        self._key = f"{table_id:02x}:{device_id:02x}"
        self._attr_unique_id = f"{self._mac}_generator_{table_id:02x}{device_id:02x}"
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Status"

    @property
    def native_value(self) -> str | None:
        gen = self.coordinator.generators.get(self._key)
        if gen is None:
            return None
        return gen.state_name.capitalize()  # Off/Priming/Starting/Running/Stopping

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, GeneratorStatus)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()


class OneControlGeneratorBatterySensor(_OneControlSensorBase):
    """Generator battery voltage sensor — event 0x0A."""

    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:car-battery"

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
        self._key = f"{table_id:02x}:{device_id:02x}"
        self._attr_unique_id = f"{self._mac}_gen_battery_{table_id:02x}{device_id:02x}"
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Battery"

    @property
    def native_value(self) -> float | None:
        gen = self.coordinator.generators.get(self._key)
        if gen is None:
            return None
        return round(gen.battery_voltage, 2)

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, GeneratorStatus)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()


class OneControlGeneratorTemperatureSensor(_OneControlSensorBase):
    """Generator temperature sensor — event 0x0A.

    Returns None (unavailable) when the generator reports 0x8000 (not supported)
    or 0x7FFF (sensor invalid).
    """

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"

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
        self._key = f"{table_id:02x}:{device_id:02x}"
        self._attr_unique_id = f"{self._mac}_gen_temp_{table_id:02x}{device_id:02x}"
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Temperature"

    @property
    def native_value(self) -> float | None:
        gen = self.coordinator.generators.get(self._key)
        if gen is None or gen.temperature_c is None:
            return None
        return round(gen.temperature_c, 1)

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, GeneratorStatus)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()


class OneControlHourMeterSensor(_OneControlSensorBase):
    """Hour meter sensor — event 0x0F."""

    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_icon = "mdi:timer-outline"

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
        self._key = f"{table_id:02x}:{device_id:02x}"
        self._attr_unique_id = f"{self._mac}_hourmeter_{table_id:02x}{device_id:02x}"
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Hours"

    @property
    def native_value(self) -> float | None:
        hm = self.coordinator.hour_meters.get(self._key)
        return hm.hours if hm else None

    @property
    def extra_state_attributes(self) -> dict | None:
        hm = self.coordinator.hour_meters.get(self._key)
        if hm is None:
            return None
        return {
            "maintenance_due": hm.maintenance_due,
            "maintenance_past_due": hm.maintenance_past_due,
            "error": hm.error,
        }

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, HourMeter)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()


# ── Cover State Sensors (state-only, no control — INTERNALS.md safety) ───


class OneControlCoverStateSensor(_OneControlSensorBase):
    """Cover/Slide/Awning state as a sensor.

    Per INTERNALS.md safety decision covers are state-only:
      "19A/39A H-bridge motors, no limit switches — no automatic safety."
    Exposed as a text sensor (Opening/Closing/Stopped), NOT as a Cover entity.
    """

    _attr_icon = "mdi:blinds-horizontal"

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
        self._key = f"{table_id:02x}:{device_id:02x}"
        self._attr_unique_id = f"{self._mac}_cover_{table_id:02x}{device_id:02x}"
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        return self.coordinator.device_name(self._table_id, self._device_id)

    @property
    def native_value(self) -> str | None:
        cov = self.coordinator.covers.get(self._key)
        if not cov:
            return None
        return cov.ha_state.capitalize()  # "Opening", "Closing", "Stopped"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cov = self.coordinator.covers.get(self._key)
        if not cov:
            return {}
        attrs: dict[str, Any] = {
            "raw_status": f"0x{cov.status:02X}",
            "control_disabled": True,
        }
        if cov.position is not None:
            attrs["position"] = cov.position
        return attrs

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, CoverStatus)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()
