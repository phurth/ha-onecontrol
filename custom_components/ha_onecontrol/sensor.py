"""Sensor platform for OneControl BLE integration.

Creates sensor entities for:
  - System voltage (RvStatus event 0x07)
  - AC voltage (RvStatus event 0x07, extended frame) — when available
  - System temperature (RvStatus event 0x07) — diagnostic, unavailable when gateway lacks sensor
  - Tank levels (TankSensorStatus events 0x0C / 0x1B)
  - Tank alerts (threshold/connectivity events)
  - Generator status (event 0x0A)
  - Hour meter (event 0x0F)
  - Leveler position (event 0x10)
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
from .coordinator import MetadataCommitted, OneControlCoordinator
from .entity import DeviceEntityAdder, OneControlDeviceEntity
from .protocol.events import CoverStatus, GeneratorStatus, HourMeter, LevelerStatus, TankAlert, TankLevel

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
        OneControlAcVoltageSensor(coordinator, address),
        OneControlTemperatureSensor(coordinator, address),
        # Diagnostic sensors
        OneControlDeviceCountSensor(coordinator, address),
        OneControlTableIdSensor(coordinator, address),
        OneControlProtocolVersionSensor(coordinator, address),
    ]

    adder = DeviceEntityAdder(hass, coordinator, address, async_add_entities, "sensor")
    _generator_sensors = (
        OneControlGeneratorSensor,
        OneControlGeneratorBatterySensor,
        OneControlGeneratorTemperatureSensor,
    )

    @callback
    def _build(item: Any) -> list[SensorEntity]:
        """Return the sensors due for one status item."""
        if isinstance(item, TankLevel):
            return adder.build("tank", item.table_id, item.device_id, OneControlTankSensor)
        if isinstance(item, GeneratorStatus):
            return adder.build("generator", item.table_id, item.device_id, *_generator_sensors)
        if isinstance(item, HourMeter):
            return adder.build("hour_meter", item.table_id, item.device_id, OneControlHourMeterSensor)
        if isinstance(item, CoverStatus):
            return adder.build("cover", item.table_id, item.device_id, OneControlCoverStateSensor)
        if isinstance(item, LevelerStatus):
            return adder.build("leveler", item.table_id, item.device_id, OneControlLevelerPositionSensor)
        if isinstance(item, TankAlert):
            return adder.build("tank_alert", item.table_id, item.device_id, OneControlTankAlertSensor)
        return []

    @callback
    def _sweep() -> list[SensorEntity]:
        """Return sensors for devices already known to the coordinator."""
        new: list[SensorEntity] = []
        for status_dict in (
            coordinator.tanks,
            coordinator.generators,
            coordinator.hour_meters,
            coordinator.covers,
            coordinator.levelers,
            coordinator.tank_alerts,
        ):
            for item in list(status_dict.values()):
                new.extend(_build(item))
        return new

    @callback
    def _on_event(event: Any) -> None:
        """Dynamically add sensor entities as new devices appear."""
        if isinstance(event, MetadataCommitted):
            new = _sweep()
        else:
            new = []
            for item in event if isinstance(event, list) else [event]:
                new.extend(_build(item))
        if new:
            async_add_entities(new)

    coordinator.register_event_callback(_on_event)

    # Pre-discover from coordinator state
    entities.extend(_sweep())
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

    @property
    def available(self) -> bool:
        return self.coordinator.data_healthy


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
    def available(self) -> bool:
        """Only available when MyRVLink RvStatus voltage is present."""
        if self.coordinator.is_can_ble_gateway:
            return False
        data = self.coordinator.data
        return super().available and bool(data and data.get("voltage") is not None)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data and "voltage" in data:
            return data["voltage"]
        return None


class OneControlAcVoltageSensor(_OneControlSensorBase):
    """AC voltage from RvStatus events (extended frame).

    Only available on gateways with AC voltage monitoring capability.
    """

    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "AC Voltage"
    _attr_icon = "mdi:flash"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{self._mac}_ac_voltage"

    @property
    def available(self) -> bool:
        """Only available when AC voltage is present."""
        rv_status = self.coordinator.rv_status
        return super().available and bool(rv_status and rv_status.ac_voltage is not None)

    @property
    def native_value(self) -> float | None:
        rv_status = self.coordinator.rv_status
        if rv_status and rv_status.ac_voltage is not None:
            return rv_status.ac_voltage
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
        if self.coordinator.is_can_ble_gateway:
            return False
        data = self.coordinator.data
        return super().available and bool(data and data.get("temperature") is not None)

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
    """MyRVLink gateway CAN table ID."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Table ID"
    _attr_icon = "mdi:identifier"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{self._mac}_table_id"

    @property
    def available(self) -> bool:
        """IDS-CAN gateways do not expose a meaningful MyRVLink table ID."""
        return super().available and not self.coordinator.is_can_ble_gateway

    @property
    def native_value(self) -> int | None:
        if self.coordinator.is_can_ble_gateway:
            return None
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


class OneControlTankSensor(OneControlDeviceEntity, _OneControlSensorBase):
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
        self._bind_device(address, table_id, device_id, "tank")
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


class OneControlGeneratorSensor(OneControlDeviceEntity, _OneControlSensorBase):
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
        self._bind_device(address, table_id, device_id, "generator")
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


class OneControlGeneratorBatterySensor(OneControlDeviceEntity, _OneControlSensorBase):
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
        self._bind_device(address, table_id, device_id, "gen_battery")
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


class OneControlGeneratorTemperatureSensor(OneControlDeviceEntity, _OneControlSensorBase):
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
        self._bind_device(address, table_id, device_id, "gen_temp")
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


class OneControlHourMeterSensor(OneControlDeviceEntity, _OneControlSensorBase):
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
        self._bind_device(address, table_id, device_id, "hourmeter")
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


class OneControlCoverStateSensor(OneControlDeviceEntity, _OneControlSensorBase):
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
        self._bind_device(address, table_id, device_id, "cover")
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base}"

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


# ── Leveler Sensors ───────────────────────────────────────────────────────


class OneControlLevelerPositionSensor(OneControlDeviceEntity, _OneControlSensorBase):
    """Leveler position sensor — created dynamically as levelers are discovered."""

    _attr_icon = "mdi:format-horizontal-align-center"

    def __init__(
        self,
        coordinator: OneControlCoordinator,
        address: str,
        table_id: int,
        device_id: int,
    ) -> None:
        super().__init__(coordinator, address)
        self._bind_device(address, table_id, device_id, "leveler_position")
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Position"

    @property
    def native_value(self) -> str | None:
        leveler = self.coordinator.levelers.get(self._key)
        if not leveler:
            return None
        positions = {0: "retracted", 1: "extended", 2: "mid", 3: "error"}
        return positions.get(leveler.position_code, f"unknown_{leveler.position_code}")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        leveler = self.coordinator.levelers.get(self._key)
        if not leveler:
            return {}
        return {
            "is_active": leveler.is_active,
            "level_achieved": leveler.level_achieved,
            "raw_position_code": leveler.position_code,
        }

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, LevelerStatus)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()


# ── Tank Alert Sensors ────────────────────────────────────────────────────


class OneControlTankAlertSensor(OneControlDeviceEntity, _OneControlSensorBase):
    """Tank alert sensor — alert type and status."""

    _attr_icon = "mdi:water-alert"

    def __init__(
        self,
        coordinator: OneControlCoordinator,
        address: str,
        table_id: int,
        device_id: int,
    ) -> None:
        super().__init__(coordinator, address)
        self._bind_device(address, table_id, device_id, "tank_alert")
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Alert"

    @property
    def native_value(self) -> str | None:
        alert = self.coordinator.tank_alerts.get(self._key)
        if not alert:
            return None
        alert_types = {0: "connectivity", 1: "low_level", 2: "high_level"}
        alert_type_str = alert_types.get(alert.alert_type, f"unknown_{alert.alert_type}")
        return "triggered" if alert.is_triggered else f"{alert_type_str}_clear"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alert = self.coordinator.tank_alerts.get(self._key)
        if not alert:
            return {}
        return {
            "alert_type": {0: "connectivity", 1: "low_level", 2: "high_level"}.get(
                alert.alert_type, f"unknown_{alert.alert_type}"
            ),
            "is_triggered": alert.is_triggered,
        }

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, TankAlert)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()
