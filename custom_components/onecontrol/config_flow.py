"""Config flow for OneControl BLE integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_BLUETOOTH_PIN,
    CONF_GATEWAY_PIN,
    CONF_PAIRING_METHOD,
    DEFAULT_GATEWAY_PIN,
    DOMAIN,
    LIPPERT_MANUFACTURER_ID,
)
from .protocol.advertisement import PairingMethod, parse_manufacturer_data

_LOGGER = logging.getLogger(__name__)


class OneControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OneControl."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None
        self._pairing_method: PairingMethod = PairingMethod.UNKNOWN

    # ------------------------------------------------------------------
    # Bluetooth discovery entry point
    # ------------------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered via Bluetooth."""
        _LOGGER.debug(
            "OneControl device discovered: %s (%s)",
            discovery_info.name,
            discovery_info.address,
        )

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._name = discovery_info.name or f"OneControl {discovery_info.address}"

        # Parse pairing capabilities from advertisement
        caps = parse_manufacturer_data(discovery_info.manufacturer_data)
        self._pairing_method = caps.pairing_method

        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    # ------------------------------------------------------------------
    # User-initiated flow (manual add)
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user."""
        if user_input is not None:
            # User picked a device from the list
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()

            self._address = address

            # Find the discovery info for this address
            for info in async_discovered_service_info(self.hass):
                if info.address == address:
                    self._discovery_info = info
                    self._name = info.name or f"OneControl {address}"
                    caps = parse_manufacturer_data(info.manufacturer_data)
                    self._pairing_method = caps.pairing_method
                    break
            else:
                self._name = f"OneControl {address}"

            return await self.async_step_confirm()

        # Build a list of discovered OneControl gateways
        devices: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass):
            if LIPPERT_MANUFACTURER_ID in info.manufacturer_data:
                devices[info.address] = info.name or info.address

        if not devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(devices)}
            ),
        )

    # ------------------------------------------------------------------
    # Confirm & collect PIN
    # ------------------------------------------------------------------

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user for the gateway PIN and create the config entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            pin = user_input.get(CONF_GATEWAY_PIN, DEFAULT_GATEWAY_PIN)
            bt_pin = user_input.get(CONF_BLUETOOTH_PIN, "")

            if not pin or len(pin) != 6 or not pin.isdigit():
                errors[CONF_GATEWAY_PIN] = "invalid_pin"
            else:
                data = {
                    CONF_ADDRESS: self._address,
                    CONF_GATEWAY_PIN: pin,
                    CONF_PAIRING_METHOD: self._pairing_method.value,
                }
                if bt_pin:
                    data[CONF_BLUETOOTH_PIN] = bt_pin

                return self.async_create_entry(
                    title=self._name or "OneControl",
                    data=data,
                )

        # Build the form — always ask for gateway PIN; show BT PIN field
        # only for legacy PIN gateways.
        fields: dict[Any, Any] = {
            vol.Required(CONF_GATEWAY_PIN, default=DEFAULT_GATEWAY_PIN): str,
        }
        if self._pairing_method == PairingMethod.PIN:
            fields[vol.Optional(CONF_BLUETOOTH_PIN, default="")] = str

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"name": self._name or "OneControl"},
        )
