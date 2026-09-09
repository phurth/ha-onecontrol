"""Cover platform for OneControl BLE integration.

Creates Cover entities that show the current state (opening/closing/stopped)
and allow control via open/close/stop commands.

Cover control is opt-in: awnings/slides use H-bridge motors with no limit
switches or supervision, so this platform is only loaded when the user enables
it in the integration options.

Reference: INTERNALS.md § Cover / Slide / Awning
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneControlCoordinator
from .helpers import is_valid_device_id
from .protocol.events import CoverStatus

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OneControl cover entities from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    discovered: set[str] = set()

    @callback
    def _on_event(event: Any) -> None:
        if isinstance(event, CoverStatus):
            if not is_valid_device_id(event.device_id):
                return
            key = f"{event.table_id:02x}:{event.device_id:02x}"
            if key not in discovered:
                discovered.add(key)
                _add_cover(
                    coordinator, address, event.table_id, event.device_id,
                    async_add_entities,
                )

    coordinator.register_event_callback(_on_event)

    for key, cov in coordinator.covers.items():
        if key not in discovered:
            discovered.add(key)
            _add_cover(
                coordinator, address, cov.table_id, cov.device_id,
                async_add_entities,
            )


def _add_cover(
    coordinator: OneControlCoordinator,
    address: str,
    table_id: int,
    device_id: int,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the cover entity."""
    async_add_entities(
        [OneControlCover(coordinator, address, table_id, device_id)]
    )


class OneControlCover(CoordinatorEntity[OneControlCoordinator], CoverEntity):
    """Cover entity with open/close/stop control.

    Shows opening / closing / stopped state from the H-Bridge status event.
    Position is exposed when available (0xFF means unknown).
    """

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.AWNING
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
    )

    @property
    def supported_features(self) -> int:
        """Return supported features for this cover entity."""
        _LOGGER.debug(
            "Cover supported_features for %s = %s",
            self._key,
            int(self._attr_supported_features),
        )
        return int(self._attr_supported_features)

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
        self._attr_unique_id = f"{mac}_cover_{table_id:02x}_{device_id:02x}"
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
    def available(self) -> bool:
        # Cover controls should remain available once the device has been
        # discovered, and while the gateway link is connected.
        available = self._key in self.coordinator.covers or self.coordinator.connected
        _LOGGER.debug("Cover available for %s = %s", self._key, available)
        return available

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is fully closed.

        Position 0 = fully retracted/closed.
        None returned when position is unknown (0xFF or absent).
        """
        cov = self.coordinator.covers.get(self._key)
        if not cov:
            return None
        # Motor is running — state is transitional
        if cov.ha_state in ("opening", "closing"):
            return False
        # Motor stopped — use position if available
        if cov.position is None or cov.position == 0xFF:
            return None
        return cov.position == 0

    @property
    def is_opening(self) -> bool:
        cov = self.coordinator.covers.get(self._key)
        return cov.ha_state == "opening" if cov else False

    @property
    def is_closing(self) -> bool:
        cov = self.coordinator.covers.get(self._key)
        return cov.ha_state == "closing" if cov else False

    @property
    def current_cover_position(self) -> int | None:
        """Position 0-100, None if unknown."""
        cov = self.coordinator.covers.get(self._key)
        if not cov or cov.position is None or cov.position == 0xFF:
            return None
        return cov.position

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cov = self.coordinator.covers.get(self._key)
        if not cov:
            return {}
        return {
            "raw_status": f"0x{cov.status:02X}",
            "table_id": self._table_id,
            "device_id": self._device_id,
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover (extend motor)."""
        _LOGGER.info("Cover open requested key=%s table=%d device=0x%02X", self._key, self._table_id, self._device_id)
        await self.coordinator.async_cover(self._table_id, self._device_id, 0x01)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover (retract motor)."""
        _LOGGER.debug("Cover close key=%s table=%d device=0x%02X", self._key, self._table_id, self._device_id)
        await self.coordinator.async_cover(self._table_id, self._device_id, 0x02)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover motor."""
        _LOGGER.debug("Cover stop key=%s table=%d device=0x%02X", self._key, self._table_id, self._device_id)
        await self.coordinator.async_cover(self._table_id, self._device_id, 0x00)

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
