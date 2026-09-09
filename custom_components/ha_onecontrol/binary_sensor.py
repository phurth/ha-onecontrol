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
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)



from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneControlCoordinator
from .helpers import is_valid_device_id
from .protocol.events import GeneratorStatus

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

    discovered_gen_quiet: set[str] = set()

    @callback
    def _on_event(event: Any) -> None:
        if isinstance(event, GeneratorStatus):
            if not is_valid_device_id(event.device_id):
                return
            key = f"{event.table_id:02x}:{event.device_id:02x}"
            if key not in discovered_gen_quiet:
                discovered_gen_quiet.add(key)
                async_add_entities(
                    [OneControlGeneratorQuietHours(coordinator, address, event.table_id, event.device_id)]
                )

    coordinator.register_event_callback(_on_event)

    for key, gen in coordinator.generators.items():
        if key not in discovered_gen_quiet:
            discovered_gen_quiet.add(key)
            async_add_entities(
                [OneControlGeneratorQuietHours(coordinator, address, gen.table_id, gen.device_id)]
            )


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

    MyRVLink gateways report this via DeviceLockStatus.  IDS-CAN BLE gateways
    derive it from the NETWORK status bitfield.  In both cases, a non-zero
    level means the RV reports an active in-motion lockout.
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
        """Only available once we've received a MyRVLink or IDS-CAN lockout level."""
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
        return {
            "lockout_level": level,
            "source": "ids_can_network" if self.coordinator.is_can_ble_gateway else "myrvlink_device_lock_status",
        }


class OneControlDataHealthy(
    CoordinatorEntity[OneControlCoordinator], BinarySensorEntity
):
    """Binary sensor showing if data is being received from the gateway.

    MyRVLink gateways require a live connection and turn off after 15 seconds
    without data.  CAN BLE gateways can disconnect between broadcast cycles,
    so this remains on for 30 seconds after the last IDS-CAN event.
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
        attrs: dict[str, Any] = {
            "transport": "ids_can_ble" if self.coordinator.is_can_ble_gateway else "myrvlink_ble",
            "raw_gateway_connected": self.coordinator.connected,
            "raw_gateway_authenticated": self.coordinator.authenticated,
        }
        age = self.coordinator.last_event_age
        if age is not None:
            attrs["last_event_age_seconds"] = round(age, 1)
        if self.coordinator.is_can_ble_gateway:
            attrs.update({
                "can_read_subscribed": self.coordinator.can_read_subscribed,
                "gateway_version": self.coordinator.can_ble_gateway_version,
                "local_host_address": f"0x{self.coordinator.gateway_can_address:02X}",
                "queued_can_commands": self.coordinator.can_command_queue_size,
            })
        return attrs


class OneControlGeneratorQuietHours(
    CoordinatorEntity[OneControlCoordinator], BinarySensorEntity
):
    """Binary sensor for generator quiet hours mode — event 0x0A.

    On when the generator is operating in quiet/reduced-noise mode.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:volume-off"

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
        self._attr_unique_id = f"{mac}_gen_quiet_{device_id:02x}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )
        self._unsub = coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Quiet Hours"

    @property
    def available(self) -> bool:
        return self.coordinator.data_healthy and self._key in self.coordinator.generators

    @property
    def is_on(self) -> bool | None:
        gen = self.coordinator.generators.get(self._key)
        if gen is None:
            return None
        return gen.quiet_hours

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
