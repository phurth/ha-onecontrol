"""Climate platform for OneControl BLE integration.

Creates climate entities for HVAC zones (event 0x0B).
Sends ActionHvac (0x45) commands for mode / setpoint changes.

Reference: INTERNALS.md § HVAC Command
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    HVAC_CAP_GAS,
    HVAC_CAP_HEAT_PUMP,
    HVAC_FAN_AUTO,
    HVAC_FAN_HIGH,
    HVAC_FAN_LOW,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
    HVAC_MODE_OFF,
    HVAC_PRESET_GAS,
    HVAC_PRESET_HEAT_PUMP,
    HVAC_PRESET_NONE,
    HVAC_SETPOINT_DEBOUNCE_S,
)
from .coordinator import OneControlCoordinator
from .protocol.events import HvacZone

_LOGGER = logging.getLogger(__name__)

# Map OneControl heat_mode → HA HVACMode
_OC_TO_HA_MODE = {
    0: HVACMode.OFF,
    1: HVACMode.HEAT,
    2: HVACMode.COOL,
    3: HVACMode.HEAT_COOL,
}

# Reverse
_HA_TO_OC_MODE: dict[HVACMode, int] = {v: k for k, v in _OC_TO_HA_MODE.items()}

# Map OneControl fan_mode → HA fan string
_OC_TO_HA_FAN = {0: "auto", 1: "high", 2: "low"}
_HA_TO_OC_FAN = {"auto": HVAC_FAN_AUTO, "high": HVAC_FAN_HIGH, "low": HVAC_FAN_LOW}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OneControl climate entities from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    discovered: set[str] = set()

    @callback
    def _on_event(event: Any) -> None:
        items = event if isinstance(event, list) else [event]
        for item in items:
            if isinstance(item, HvacZone):
                key = f"{item.table_id:02x}:{item.device_id:02x}"
                if key not in discovered:
                    discovered.add(key)
                    async_add_entities(
                        [OneControlClimate(coordinator, address, item.table_id, item.device_id)]
                    )

    coordinator.register_event_callback(_on_event)

    for key, zone in coordinator.hvac_zones.items():
        if key not in discovered:
            discovered.add(key)
            async_add_entities(
                [OneControlClimate(coordinator, address, zone.table_id, zone.device_id)]
            )


class OneControlClimate(CoordinatorEntity[OneControlCoordinator], ClimateEntity):
    """A OneControl HVAC zone."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
    _attr_fan_modes = ["auto", "high", "low"]
    _attr_min_temp = 40
    _attr_max_temp = 95

    def __init__(
        self,
        coordinator: OneControlCoordinator,
        address: str,
        table_id: int,
        device_id: int,
    ) -> None:
        super().__init__(coordinator)
        self._table_id = table_id
        self._device_id = device_id
        self._key = f"{table_id:02x}:{device_id:02x}"
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_climate_{table_id:02x}{device_id:02x}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )
        self._unsub = coordinator.register_event_callback(self._on_event)
        self._setpoint_debounce_handle: asyncio.TimerHandle | None = None

    @property
    def name(self) -> str:
        return self.coordinator.device_name(self._table_id, self._device_id)

    @property
    def _zone(self) -> HvacZone | None:
        return self.coordinator.hvac_zones.get(self._key)

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Dynamic feature set: single vs dual setpoint, preset mode when capable."""
        features = ClimateEntityFeature.FAN_MODE
        mode = self.hvac_mode
        if mode == HVACMode.HEAT_COOL:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        elif mode != HVACMode.OFF:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        cap = self.coordinator.observed_hvac_capability.get(self._key, 0)
        if cap & (HVAC_CAP_GAS | HVAC_CAP_HEAT_PUMP):
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def hvac_mode(self) -> HVACMode:
        zone = self._zone
        if zone is None:
            return HVACMode.OFF
        return _OC_TO_HA_MODE.get(zone.heat_mode, HVACMode.OFF)

    @property
    def hvac_action(self) -> HVACAction | None:
        zone = self._zone
        if zone is None:
            return None
        active = zone.zone_status & 0x0F
        return {
            0: HVACAction.OFF,
            1: HVACAction.IDLE,
            2: HVACAction.COOLING,
            3: HVACAction.HEATING,  # heat pump heating
            4: HVACAction.HEATING,  # electric heat
            5: HVACAction.HEATING,  # gas furnace
            6: HVACAction.HEATING,  # gas override
            7: HVACAction.IDLE,     # dead time
            8: HVACAction.IDLE,     # load shedding
        }.get(active, HVACAction.OFF)

    @property
    def fan_mode(self) -> str | None:
        zone = self._zone
        if zone is None:
            return None
        return _OC_TO_HA_FAN.get(zone.fan_mode, "auto")

    @property
    def preset_modes(self) -> list[str] | None:
        cap = self.coordinator.observed_hvac_capability.get(self._key, 0)
        if not (cap & (HVAC_CAP_GAS | HVAC_CAP_HEAT_PUMP)):
            return None
        modes = [HVAC_PRESET_NONE]
        if cap & HVAC_CAP_GAS:
            modes.append(HVAC_PRESET_GAS)
        if cap & HVAC_CAP_HEAT_PUMP:
            modes.append(HVAC_PRESET_HEAT_PUMP)
        return modes

    @property
    def preset_mode(self) -> str | None:
        zone = self._zone
        if zone is None:
            return None
        cap = self.coordinator.observed_hvac_capability.get(self._key, 0)
        if not (cap & (HVAC_CAP_GAS | HVAC_CAP_HEAT_PUMP)):
            return None
        return {0: HVAC_PRESET_GAS, 1: HVAC_PRESET_HEAT_PUMP}.get(
            zone.heat_source, HVAC_PRESET_NONE
        )

    @property
    def current_temperature(self) -> float | None:
        zone = self._zone
        return zone.indoor_temp_f if zone else None

    @property
    def target_temperature(self) -> float | None:
        """Single setpoint for heat or cool mode; None for heat_cool / off."""
        zone = self._zone
        if zone is None or zone.heat_mode == 0 or zone.heat_mode == 3:
            return None
        return float(zone.low_trip_f if zone.heat_mode == 1 else zone.high_trip_f)

    @property
    def target_temperature_low(self) -> float | None:
        """Heat setpoint — only meaningful in heat_cool mode."""
        zone = self._zone
        return float(zone.low_trip_f) if zone and zone.heat_mode == 3 else None

    @property
    def target_temperature_high(self) -> float | None:
        """Cool setpoint — only meaningful in heat_cool mode."""
        zone = self._zone
        return float(zone.high_trip_f) if zone and zone.heat_mode == 3 else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self._zone
        attrs: dict[str, Any] = {
            "table_id": self._table_id,
            "device_id": self._device_id,
        }
        if zone:
            attrs["outdoor_temperature_f"] = zone.outdoor_temp_f
            attrs["heat_source"] = zone.heat_source
            attrs["zone_status"] = zone.zone_status
            if zone.dtc_code:
                attrs["dtc_code"] = zone.dtc_code
        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        zone = self._zone
        if zone is None:
            return
        oc_mode = _HA_TO_OC_MODE.get(hvac_mode, HVAC_MODE_OFF)
        new_zone = HvacZone(
            table_id=zone.table_id, device_id=zone.device_id,
            heat_mode=oc_mode, heat_source=zone.heat_source,
            fan_mode=zone.fan_mode, low_trip_f=zone.low_trip_f,
            high_trip_f=zone.high_trip_f, zone_status=zone.zone_status,
            indoor_temp_f=zone.indoor_temp_f, outdoor_temp_f=zone.outdoor_temp_f,
            dtc_code=zone.dtc_code,
        )
        self.coordinator.hvac_zones[self._key] = new_zone
        self.coordinator._hvac_zone_states[self._key] = new_zone
        self.async_write_ha_state()
        await self.coordinator.async_set_hvac(
            self._table_id, self._device_id,
            heat_mode=oc_mode, heat_source=zone.heat_source,
            fan_mode=zone.fan_mode, low_trip_f=zone.low_trip_f,
            high_trip_f=zone.high_trip_f,
            is_setpoint_change=False, is_preset_change=False,
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        zone = self._zone
        if zone is None:
            return
        oc_fan = _HA_TO_OC_FAN.get(fan_mode, HVAC_FAN_AUTO)
        new_zone = HvacZone(
            table_id=zone.table_id, device_id=zone.device_id,
            heat_mode=zone.heat_mode, heat_source=zone.heat_source,
            fan_mode=oc_fan, low_trip_f=zone.low_trip_f,
            high_trip_f=zone.high_trip_f, zone_status=zone.zone_status,
            indoor_temp_f=zone.indoor_temp_f, outdoor_temp_f=zone.outdoor_temp_f,
            dtc_code=zone.dtc_code,
        )
        self.coordinator.hvac_zones[self._key] = new_zone
        self.coordinator._hvac_zone_states[self._key] = new_zone
        self.async_write_ha_state()
        await self.coordinator.async_set_hvac(
            self._table_id, self._device_id,
            heat_mode=zone.heat_mode, heat_source=zone.heat_source,
            fan_mode=oc_fan, low_trip_f=zone.low_trip_f,
            high_trip_f=zone.high_trip_f,
            is_setpoint_change=False, is_preset_change=False,
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        zone = self._zone
        if zone is None:
            return
        low = zone.low_trip_f
        high = zone.high_trip_f

        if "target_temp_low" in kwargs:
            low = int(kwargs["target_temp_low"])
        if "target_temp_high" in kwargs:
            high = int(kwargs["target_temp_high"])
        if "temperature" in kwargs:
            temp = int(kwargs["temperature"])
            if zone.heat_mode == HVAC_MODE_HEAT:
                low = temp
            else:
                high = temp

        # Debounce rapid slider changes (250ms, matching Android plugin)
        if self._setpoint_debounce_handle is not None:
            self._setpoint_debounce_handle.cancel()
            self._setpoint_debounce_handle = None

        # Capture values for the closure
        _low, _high = low, high

        async def _send_setpoint() -> None:
            self._setpoint_debounce_handle = None
            current = self._zone
            if current is None:
                return
            new_zone = HvacZone(
                table_id=current.table_id, device_id=current.device_id,
                heat_mode=current.heat_mode, heat_source=current.heat_source,
                fan_mode=current.fan_mode, low_trip_f=_low,
                high_trip_f=_high, zone_status=current.zone_status,
                indoor_temp_f=current.indoor_temp_f, outdoor_temp_f=current.outdoor_temp_f,
                dtc_code=current.dtc_code,
            )
            self.coordinator.hvac_zones[self._key] = new_zone
            self.coordinator._hvac_zone_states[self._key] = new_zone
            self.async_write_ha_state()
            await self.coordinator.async_set_hvac(
                self._table_id, self._device_id,
                heat_mode=current.heat_mode, heat_source=current.heat_source,
                fan_mode=current.fan_mode, low_trip_f=_low, high_trip_f=_high,
                is_setpoint_change=True, is_preset_change=False,
            )

        loop = asyncio.get_running_loop()
        self._setpoint_debounce_handle = loop.call_later(
            HVAC_SETPOINT_DEBOUNCE_S,
            lambda: self.coordinator.hass.async_create_task(_send_setpoint()),
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        zone = self._zone
        if zone is None:
            return
        heat_source = {
            HVAC_PRESET_GAS: 0,
            HVAC_PRESET_HEAT_PUMP: 1,
        }.get(preset_mode, zone.heat_source)
        new_zone = HvacZone(
            table_id=zone.table_id, device_id=zone.device_id,
            heat_mode=zone.heat_mode, heat_source=heat_source,
            fan_mode=zone.fan_mode, low_trip_f=zone.low_trip_f,
            high_trip_f=zone.high_trip_f, zone_status=zone.zone_status,
            indoor_temp_f=zone.indoor_temp_f, outdoor_temp_f=zone.outdoor_temp_f,
            dtc_code=zone.dtc_code,
        )
        self.coordinator.hvac_zones[self._key] = new_zone
        self.coordinator._hvac_zone_states[self._key] = new_zone
        self.async_write_ha_state()
        await self.coordinator.async_set_hvac(
            self._table_id, self._device_id,
            heat_mode=zone.heat_mode, heat_source=heat_source,
            fan_mode=zone.fan_mode, low_trip_f=zone.low_trip_f,
            high_trip_f=zone.high_trip_f,
            is_setpoint_change=False, is_preset_change=True,
        )

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()
        if self._setpoint_debounce_handle is not None:
            self._setpoint_debounce_handle.cancel()
            self._setpoint_debounce_handle = None

    @callback
    def _on_event(self, event: Any) -> None:
        items = event if isinstance(event, list) else [event]
        for item in items:
            if (
                isinstance(item, HvacZone)
                and item.table_id == self._table_id
                and item.device_id == self._device_id
            ):
                self.async_write_ha_state()
                return
