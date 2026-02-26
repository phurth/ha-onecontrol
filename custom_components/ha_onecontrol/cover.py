"""Cover platform for OneControl BLE integration.

IMPORTANT: Covers are STATE-ONLY — no open/close/stop commands are sent.
Per INTERNALS.md safety decision:
  "Cover control was intentionally disabled... RV awnings and slides have
   no automatic safety mechanisms. The 19A/39A H-bridge motors could cause
   damage or injury without manual supervision."

Creates Cover entities that show the current state (opening/closing/stopped)
but do NOT allow control via Home Assistant.

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
            key = f"{event.table_id:02x}:{event.device_id:02x}"
            if key not in discovered:
                discovered.add(key)
                async_add_entities(
                    [OneControlCover(coordinator, address, event.table_id, event.device_id)]
                )

    coordinator.register_event_callback(_on_event)

    for key, cov in coordinator.covers.items():
        if key not in discovered:
            discovered.add(key)
            async_add_entities(
                [OneControlCover(coordinator, address, cov.table_id, cov.device_id)]
            )


class OneControlCover(CoordinatorEntity[OneControlCoordinator], CoverEntity):
    """Cover entity — state-only, no control commands.

    Shows opening / closing / stopped state from the H-Bridge status event.
    Position is exposed when available (0xFF means unknown).
    """

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.AWNING
    _attr_supported_features = CoverEntityFeature(0)  # No control features

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
        self._attr_unique_id = f"{mac}_cover_{table_id:02x}{device_id:02x}"
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
    def is_closed(self) -> bool | None:
        """Return True if the cover is fully closed (stopped, position 0 or unknown)."""
        cov = self.coordinator.covers.get(self._key)
        if not cov:
            return None
        # If stopped and position is 0% → closed
        # If position unknown (0xFF) and stopped → assume closed
        if cov.ha_state == "closed":
            return True
        if cov.ha_state in ("opening", "closing"):
            return False
        # stopped with unknown or partial position
        return None if cov.position == 0xFF else (cov.position == 0)

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
        if not cov or cov.position == 0xFF:
            return None
        return cov.position

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cov = self.coordinator.covers.get(self._key)
        if not cov:
            return {}
        return {
            "raw_status": f"0x{cov.status:02X}",
            "control_disabled": True,
            "safety_note": "Cover control intentionally disabled — no limit switches",
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Intentionally not implemented — safety."""
        _LOGGER.warning("Cover open command blocked — safety: no limit switches")

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Intentionally not implemented — safety."""
        _LOGGER.warning("Cover close command blocked — safety: no limit switches")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Intentionally not implemented — safety."""
        _LOGGER.warning("Cover stop command blocked — safety: no limit switches")

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
