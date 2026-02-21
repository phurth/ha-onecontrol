"""Switch platform for OneControl BLE integration.

Creates switch entities for relay/latching devices (events 0x05/0x06).
Sends ActionSwitch (0x40) commands on toggle.

Reference: INTERNALS.md § Relay Status, § Switch Command
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneControlCoordinator
from .protocol.dtc_codes import get_name as dtc_get_name, is_fault as dtc_is_fault
from .protocol.events import RelayStatus

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OneControl switch entities from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    discovered: set[str] = set()

    @callback
    def _on_event(event: Any) -> None:
        if isinstance(event, RelayStatus):
            key = f"{event.table_id:02x}:{event.device_id:02x}"
            if key not in discovered:
                discovered.add(key)
                async_add_entities(
                    [OneControlSwitch(coordinator, address, event.table_id, event.device_id)]
                )

    coordinator.register_event_callback(_on_event)

    # Also create entities for any relays already discovered
    for key, relay in coordinator.relays.items():
        if key not in discovered:
            discovered.add(key)
            async_add_entities(
                [OneControlSwitch(coordinator, address, relay.table_id, relay.device_id)]
            )


class OneControlSwitch(CoordinatorEntity[OneControlCoordinator], SwitchEntity):
    """A OneControl relay switch."""

    _attr_has_entity_name = True

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
        self._attr_unique_id = f"{mac}_switch_{table_id:02x}{device_id:02x}"
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
        return self.coordinator.device_name(self._table_id, self._device_id)

    @property
    def is_on(self) -> bool | None:
        relay = self.coordinator.relays.get(self._key)
        return relay.is_on if relay else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        relay = self.coordinator.relays.get(self._key)
        attrs: dict[str, Any] = {
            "table_id": self._table_id,
            "device_id": self._device_id,
        }
        if relay and relay.dtc_code:
            attrs["dtc_code"] = relay.dtc_code
            attrs["dtc_name"] = dtc_get_name(relay.dtc_code)
            attrs["dtc_fault"] = dtc_is_fault(relay.dtc_code)
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Optimistic: update local state immediately for responsive UI
        relay = self.coordinator.relays.get(self._key)
        if relay:
            self.coordinator.relays[self._key] = RelayStatus(
                table_id=relay.table_id,
                device_id=relay.device_id,
                is_on=True,
                dtc_code=relay.dtc_code,
            )
            self.async_write_ha_state()
        await self.coordinator.async_switch(self._table_id, self._device_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        relay = self.coordinator.relays.get(self._key)
        if relay:
            self.coordinator.relays[self._key] = RelayStatus(
                table_id=relay.table_id,
                device_id=relay.device_id,
                is_on=False,
                dtc_code=relay.dtc_code,
            )
            self.async_write_ha_state()
        await self.coordinator.async_switch(self._table_id, self._device_id, False)

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, RelayStatus)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()
