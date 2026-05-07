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
    CONF_ADVERTISED_GATEWAY_VERSION,
    CONF_BLUETOOTH_PIN,
    CONF_GATEWAY_FAMILY,
    CONF_GATEWAY_PIN,
    CONF_PAIRING_METHOD,
    DEFAULT_GATEWAY_PIN,
    DOMAIN,
    GATEWAY_FAMILY_LEGACY,
    GATEWAY_FAMILY_X180T,
    GATEWAY_NAME_PREFIX,
    LIPPERT_MANUFACTURER_ID,
    LIPPERT_MANUFACTURER_ID_ALT,
    X180T_DISCOVERY_SERVICE_UUID,
)
from .protocol.advertisement import PairingMethod, parse_gateway_advertisement

_LOGGER = logging.getLogger(__name__)


class OneControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OneControl."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialise flow state."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None
        self._pairing_method: PairingMethod = PairingMethod.UNKNOWN
        self._gateway_family: str = GATEWAY_FAMILY_LEGACY
        self._advertised_gateway_version: str | None = None

    def _set_discovery_info(self, discovery_info: BluetoothServiceInfoBleak) -> None:
        """Store discovery info and parse official advertisement metadata."""
        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._name = discovery_info.name or f"OneControl {discovery_info.address}"

        capabilities = parse_gateway_advertisement(
            discovery_info.manufacturer_data,
            discovery_info.service_uuids,
        )
        self._pairing_method = capabilities.pairing_method
        self._gateway_family = (
            GATEWAY_FAMILY_X180T if capabilities.is_x180t else GATEWAY_FAMILY_LEGACY
        )
        self._advertised_gateway_version = capabilities.advertised_gateway_version

        _LOGGER.info(
            "OneControl advertisement %s: family=%s method=%s pairing_enabled=%s "
            "push_button=%s tlv=%s ble_capability=%s advertised_gateway_version=%s",
            discovery_info.address,
            self._gateway_family,
            capabilities.pairing_method.value,
            capabilities.pairing_enabled,
            capabilities.supports_push_to_pair,
            capabilities.uses_modern_tlv,
            capabilities.ble_capability.name if capabilities.ble_capability else None,
            capabilities.advertised_gateway_version,
        )

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

        self._set_discovery_info(discovery_info)

        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_pairing_method()

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
                    self._set_discovery_info(info)
                    break
            else:
                self._name = f"OneControl {address}"

            return await self.async_step_pairing_method()

        # Build a list of discovered OneControl gateways.
        # Match on either known manufacturer ID or the "LCIRemote" name prefix
        # to cover gateway variants that advertise a different company ID.
        devices: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass):
            if (
                LIPPERT_MANUFACTURER_ID in info.manufacturer_data
                or LIPPERT_MANUFACTURER_ID_ALT in info.manufacturer_data
                or (info.name and info.name.startswith(GATEWAY_NAME_PREFIX))
                or X180T_DISCOVERY_SERVICE_UUID in {
                    uuid.lower() for uuid in info.service_uuids
                }
            ):
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
    # Pairing method selection
    # ------------------------------------------------------------------

    async def async_step_pairing_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user whether their gateway uses Push-to-Pair or PIN."""
        if (
            user_input is None
            and self._gateway_family == GATEWAY_FAMILY_X180T
            and self._pairing_method not in (PairingMethod.UNKNOWN, PairingMethod.NONE)
        ):
            return await self.async_step_confirm()

        if user_input is not None:
            self._pairing_method = PairingMethod(user_input[CONF_PAIRING_METHOD])
            return await self.async_step_confirm()

        return self.async_show_form(
            step_id="pairing_method",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_METHOD): vol.In(
                        {
                            PairingMethod.PUSH_BUTTON.value: "Push-to-Pair (has a physical Connect button)",
                            PairingMethod.PIN.value: "PIN/passkey pairing (6-digit Bluetooth PIN)",
                        }
                    )
                }
            ),
            description_placeholders={"name": self._name or "OneControl"},
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
            if self._gateway_family == GATEWAY_FAMILY_X180T:
                pin = ""
            else:
                pin = user_input.get(CONF_GATEWAY_PIN, DEFAULT_GATEWAY_PIN)
            bt_pin = user_input.get(CONF_BLUETOOTH_PIN, "")

            if self._gateway_family == GATEWAY_FAMILY_X180T:
                if self._pairing_method == PairingMethod.PIN and (
                    not bt_pin or len(bt_pin) != 6 or not bt_pin.isdigit()
                ):
                    errors[CONF_BLUETOOTH_PIN] = "invalid_pin"
                elif bt_pin and (len(bt_pin) != 6 or not bt_pin.isdigit()):
                    errors[CONF_BLUETOOTH_PIN] = "invalid_pin"
            elif not pin or len(pin) != 6 or not pin.isdigit():
                errors[CONF_GATEWAY_PIN] = "invalid_pin"
            elif bt_pin and (len(bt_pin) != 6 or not bt_pin.isdigit()):
                errors[CONF_BLUETOOTH_PIN] = "invalid_pin"

            if not errors:
                data = {
                    CONF_ADDRESS: self._address,
                    CONF_GATEWAY_PIN: pin,
                    CONF_PAIRING_METHOD: self._pairing_method.value,
                    CONF_GATEWAY_FAMILY: self._gateway_family,
                }
                if bt_pin:
                    data[CONF_BLUETOOTH_PIN] = bt_pin
                if self._advertised_gateway_version:
                    data[CONF_ADVERTISED_GATEWAY_VERSION] = (
                        self._advertised_gateway_version
                    )

                return self.async_create_entry(
                    title=self._name or "OneControl",
                    data=data,
                )

        # Build the form — always ask for gateway PIN; show BT PIN field
        # only for PIN-based gateways.
        fields: dict[Any, Any] = {
            vol.Required(CONF_GATEWAY_PIN, default=DEFAULT_GATEWAY_PIN): str,
        }

        # For PIN gateways, show a separate step with extra context
        step_id = "confirm"
        if self._gateway_family == GATEWAY_FAMILY_X180T:
            fields = {}
            if self._pairing_method == PairingMethod.PIN:
                fields[vol.Required(CONF_BLUETOOTH_PIN)] = str
            step_id = "confirm_x180t"
        elif self._pairing_method == PairingMethod.PIN:
            fields[vol.Optional(CONF_BLUETOOTH_PIN, default="")] = str
            step_id = "confirm_pin"

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"name": self._name or "OneControl"},
        )

    async def async_step_confirm_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the PIN-gateway confirmation step (delegates to confirm)."""
        return await self.async_step_confirm(user_input)

    async def async_step_confirm_x180t(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the X180T confirmation step (delegates to confirm)."""
        return await self.async_step_confirm(user_input)
