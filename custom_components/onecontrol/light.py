"""Light platform for OneControl BLE integration.

Creates light entities for:
  - Dimmable lights (event 0x08) → ActionDimmable (0x43)
  - RGB lights (event 0x09) → ActionRgb (0x44)

Reference: INTERNALS.md § Dimmable Light, § RGB Light
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneControlCoordinator
from .protocol.commands import CommandBuilder
from .protocol.events import DimmableLight, RgbLight

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OneControl light entities from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    discovered: set[str] = set()

    @callback
    def _on_event(event: Any) -> None:
        if isinstance(event, DimmableLight):
            key = f"dim_{event.table_id:02x}:{event.device_id:02x}"
            if key not in discovered:
                discovered.add(key)
                async_add_entities(
                    [OneControlDimmableLight(coordinator, address, event.table_id, event.device_id)]
                )
        elif isinstance(event, RgbLight):
            key = f"rgb_{event.table_id:02x}:{event.device_id:02x}"
            if key not in discovered:
                discovered.add(key)
                async_add_entities(
                    [OneControlRgbLight(coordinator, address, event.table_id, event.device_id)]
                )

    coordinator.register_event_callback(_on_event)

    for key, light in coordinator.dimmable_lights.items():
        disc_key = f"dim_{key}"
        if disc_key not in discovered:
            discovered.add(disc_key)
            async_add_entities(
                [OneControlDimmableLight(coordinator, address, light.table_id, light.device_id)]
            )

    for key, light in coordinator.rgb_lights.items():
        disc_key = f"rgb_{key}"
        if disc_key not in discovered:
            discovered.add(disc_key)
            async_add_entities(
                [OneControlRgbLight(coordinator, address, light.table_id, light.device_id)]
            )


# ── Dimmable Effect Presets ──────────────────────────────────────────────────
# Android speed presets: Fast=220ms, Medium=1055ms, Slow=2447ms
_DIMMABLE_SPEED_MS = {"Slow": 2447, "Medium": 1055, "Fast": 220}

# (mode, cycle_time1, cycle_time2)
_DIMMABLE_EFFECTS: dict[str, tuple[int, int, int]] = {
    "Blink Slow": (CommandBuilder.DIMMABLE_MODE_BLINK, 2447, 2447),
    "Blink Medium": (CommandBuilder.DIMMABLE_MODE_BLINK, 1055, 1055),
    "Blink Fast": (CommandBuilder.DIMMABLE_MODE_BLINK, 220, 220),
    "Swell Slow": (CommandBuilder.DIMMABLE_MODE_SWELL, 2447, 2447),
    "Swell Medium": (CommandBuilder.DIMMABLE_MODE_SWELL, 1055, 1055),
    "Swell Fast": (CommandBuilder.DIMMABLE_MODE_SWELL, 220, 220),
}


class OneControlDimmableLight(CoordinatorEntity[OneControlCoordinator], LightEntity):
    """A OneControl dimmable light."""

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = list(_DIMMABLE_EFFECTS.keys())

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
        self._attr_unique_id = f"{mac}_light_{table_id:02x}{device_id:02x}"
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
        light = self.coordinator.dimmable_lights.get(self._key)
        return light.is_on if light else None

    @property
    def brightness(self) -> int | None:
        """Return HA brightness (0-255)."""
        light = self.coordinator.dimmable_lights.get(self._key)
        return light.brightness if light else None

    @property
    def effect(self) -> str | None:
        """Return current effect name from mode (best-guess, speed unknown)."""
        light = self.coordinator.dimmable_lights.get(self._key)
        if light is None or light.mode <= 1:
            return None
        # Mode 2=Blink, 3=Swell — default to Medium since event doesn't carry speed
        if light.mode == CommandBuilder.DIMMABLE_MODE_BLINK:
            return "Blink Medium"
        if light.mode == CommandBuilder.DIMMABLE_MODE_SWELL:
            return "Swell Medium"
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            light = self.coordinator.dimmable_lights.get(self._key)
            brightness = light.brightness if light and light.brightness > 0 else 255

        effect = kwargs.get(ATTR_EFFECT)
        if effect and effect in _DIMMABLE_EFFECTS:
            # Send 12-byte effect command (blink/swell at chosen speed)
            mode, ct1, ct2 = _DIMMABLE_EFFECTS[effect]
            light = self.coordinator.dimmable_lights.get(self._key)
            if light:
                self.coordinator.dimmable_lights[self._key] = DimmableLight(
                    table_id=light.table_id,
                    device_id=light.device_id,
                    brightness=brightness,
                    mode=mode,
                )
                self.async_write_ha_state()
            await self.coordinator.async_set_dimmable_effect(
                self._table_id, self._device_id,
                mode=mode, brightness=brightness,
                cycle_time1=ct1, cycle_time2=ct2,
            )
            return

        # Standard brightness command (8-byte)
        light = self.coordinator.dimmable_lights.get(self._key)
        if light:
            self.coordinator.dimmable_lights[self._key] = DimmableLight(
                table_id=light.table_id,
                device_id=light.device_id,
                brightness=brightness,
                mode=1,
            )
            self.async_write_ha_state()
        await self.coordinator.async_set_dimmable(
            self._table_id, self._device_id, brightness
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        light = self.coordinator.dimmable_lights.get(self._key)
        if light:
            self.coordinator.dimmable_lights[self._key] = DimmableLight(
                table_id=light.table_id,
                device_id=light.device_id,
                brightness=0,
                mode=0,
            )
            self.async_write_ha_state()
        await self.coordinator.async_set_dimmable(
            self._table_id, self._device_id, 0
        )

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, DimmableLight)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()


# ── RGB Light Effect Names ──────────────────────────────────────────────────
_RGB_EFFECTS = {
    "Solid": CommandBuilder.RGB_MODE_SOLID,
    "Blink": CommandBuilder.RGB_MODE_BLINK,
    "Transition Solid": CommandBuilder.RGB_MODE_TRANSITION_SOLID,
    "Transition Blink": CommandBuilder.RGB_MODE_TRANSITION_BLINK,
    "Transition Breathe": CommandBuilder.RGB_MODE_TRANSITION_BREATHE,
    "Transition Marquee": CommandBuilder.RGB_MODE_TRANSITION_MARQUEE,
    "Rainbow": CommandBuilder.RGB_MODE_TRANSITION_RAINBOW,
}
_EFFECT_NAME_TO_MODE = {k: v for k, v in _RGB_EFFECTS.items()}


class OneControlRgbLight(CoordinatorEntity[OneControlCoordinator], LightEntity):
    """A OneControl RGB light."""

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = list(_RGB_EFFECTS.keys())

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
        self._attr_unique_id = f"{mac}_rgb_{table_id:02x}{device_id:02x}"
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
        light = self.coordinator.rgb_lights.get(self._key)
        return light.is_on if light else None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        light = self.coordinator.rgb_lights.get(self._key)
        if light is None:
            return None
        return (light.red, light.green, light.blue)

    @property
    def brightness(self) -> int | None:
        light = self.coordinator.rgb_lights.get(self._key)
        return light.brightness if light else None

    @property
    def effect(self) -> str | None:
        light = self.coordinator.rgb_lights.get(self._key)
        if light is None:
            return None
        for name, mode in _RGB_EFFECTS.items():
            if mode == light.mode:
                return name
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        light = self.coordinator.rgb_lights.get(self._key)

        # Resolve RGB color
        rgb = kwargs.get(ATTR_RGB_COLOR)
        if rgb:
            r, g, b = rgb
        elif light:
            r, g, b = light.red, light.green, light.blue
        else:
            r, g, b = 255, 255, 255

        # Resolve effect / mode
        effect = kwargs.get(ATTR_EFFECT)
        if effect and effect in _EFFECT_NAME_TO_MODE:
            mode = _EFFECT_NAME_TO_MODE[effect]
        elif light and light.mode > 0:
            mode = light.mode
        else:
            mode = CommandBuilder.RGB_MODE_SOLID

        # Optimistic update
        if light:
            self.coordinator.rgb_lights[self._key] = RgbLight(
                table_id=light.table_id, device_id=light.device_id,
                mode=mode, red=r, green=g, blue=b, brightness=light.brightness,
            )
            self.async_write_ha_state()

        await self.coordinator.async_set_rgb(
            self._table_id, self._device_id,
            mode=mode, red=r, green=g, blue=b,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        light = self.coordinator.rgb_lights.get(self._key)
        if light:
            self.coordinator.rgb_lights[self._key] = RgbLight(
                table_id=light.table_id, device_id=light.device_id,
                mode=0, red=light.red, green=light.green, blue=light.blue,
                brightness=light.brightness,
            )
            self.async_write_ha_state()
        await self.coordinator.async_set_rgb(
            self._table_id, self._device_id,
            mode=CommandBuilder.RGB_MODE_OFF,
        )

    async def async_will_remove_from_hass(self) -> None:
        self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, RgbLight)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()