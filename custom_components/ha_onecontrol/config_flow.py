"""Config flow for OneControl BLE integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers import selector

from .const import (
    CONNECTION_TYPE_BLE,
    CONNECTION_TYPE_ETHERNET,
    CONF_CONNECTION_TYPE,
    CONF_BLUETOOTH_PIN,
    CONF_ETH_HOST,
    CONF_ETH_PORT,
    CONF_GATEWAY_PIN,
    CONF_NAMING_MANIFEST_JSON,
    CONF_NAMING_MANIFEST_PATH,
    CONF_NAMING_SNAPSHOT_JSON,
    CONF_NAMING_SNAPSHOT_PATH,
    CONF_PAIRING_METHOD,
    DEFAULT_ETH_HOST,
    DEFAULT_ETH_PORT,
    ETH_DISCOVERY_LISTEN_SECS,
    DEFAULT_GATEWAY_PIN,
    DOMAIN,
    GATEWAY_NAME_PREFIX,
    LIPPERT_MANUFACTURER_ID,
    LIPPERT_MANUFACTURER_ID_ALT,
)
from .name_catalog import load_external_name_catalog
from .protocol.advertisement import PairingMethod
from .protocol.ethernet_discovery import discover_can_ethernet_bridges

_LOGGER = logging.getLogger(__name__)


class OneControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OneControl."""

    VERSION = 2

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OneControlOptionsFlow:
        """Return the options flow handler."""
        return OneControlOptionsFlow(config_entry)

    def __init__(self) -> None:
        """Initialise flow state."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None
        self._pairing_method: PairingMethod = PairingMethod.UNKNOWN
        self._connection_type: str = CONNECTION_TYPE_BLE
        self._eth_host: str = DEFAULT_ETH_HOST
        self._eth_port: int = DEFAULT_ETH_PORT

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
            self._connection_type = user_input[CONF_CONNECTION_TYPE]
            if self._connection_type == CONNECTION_TYPE_ETHERNET:
                return await self.async_step_ethernet()
            return await self.async_step_user_ble()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CONNECTION_TYPE, default=CONNECTION_TYPE_BLE): vol.In(
                        {
                            CONNECTION_TYPE_BLE: "Bluetooth gateway",
                            CONNECTION_TYPE_ETHERNET: "CAN-to-Ethernet bridge",
                        }
                    )
                }
            ),
        )

    async def async_step_user_ble(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual BLE gateway selection."""
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
            ):
                devices[info.address] = info.name or info.address

        if not devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user_ble",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(devices)}
            ),
        )

    async def async_step_ethernet(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure an IDS CAN-to-Ethernet bridge endpoint."""
        errors: dict[str, str] = {}

        if user_input is None:
            discovered = await discover_can_ethernet_bridges(ETH_DISCOVERY_LISTEN_SECS)
            if discovered:
                self._name = discovered[0].name
                self._eth_host = discovered[0].host
                self._eth_port = discovered[0].port
                _LOGGER.debug(
                    "Ethernet discovery selected bridge: name=%s host=%s port=%d (candidates=%d)",
                    self._name,
                    self._eth_host,
                    self._eth_port,
                    len(discovered),
                )
            else:
                _LOGGER.debug(
                    "Ethernet discovery found no bridge advertisements; using defaults host=%s port=%d",
                    self._eth_host,
                    self._eth_port,
                )

        if user_input is not None:
            host = str(user_input[CONF_ETH_HOST]).strip()
            port = user_input[CONF_ETH_PORT]
            pin = str(user_input.get(CONF_GATEWAY_PIN, DEFAULT_GATEWAY_PIN)).strip()

            if not host:
                errors[CONF_ETH_HOST] = "invalid_host"
            elif not isinstance(port, int) or port < 1 or port > 65535:
                errors[CONF_ETH_PORT] = "invalid_port"
            elif pin and (len(pin) != 6 or not pin.isdigit()):
                errors[CONF_GATEWAY_PIN] = "invalid_pin"
            elif not await self._async_can_connect_ethernet(host, int(port)):
                errors["base"] = "cannot_connect"
            else:
                unique_id = f"eth:{host}:{port}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                self._eth_host = host
                self._eth_port = int(port)
                self._address = host
                self._name = self._name or f"OneControl Ethernet {host}"

                return self.async_create_entry(
                    title=self._name,
                    data={
                        CONF_CONNECTION_TYPE: CONNECTION_TYPE_ETHERNET,
                        CONF_ETH_HOST: self._eth_host,
                        CONF_ETH_PORT: self._eth_port,
                        CONF_ADDRESS: self._eth_host,
                        CONF_GATEWAY_PIN: pin or DEFAULT_GATEWAY_PIN,
                        CONF_PAIRING_METHOD: PairingMethod.NONE.value,
                    },
                )

        return self.async_show_form(
            step_id="ethernet",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ETH_HOST, default=self._eth_host): str,
                    vol.Required(CONF_ETH_PORT, default=self._eth_port): int,
                    vol.Optional(CONF_GATEWAY_PIN, default=DEFAULT_GATEWAY_PIN): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "default_host": DEFAULT_ETH_HOST,
                "default_port": str(DEFAULT_ETH_PORT),
            },
        )

    async def _async_can_connect_ethernet(self, host: str, port: int) -> bool:
        """Return True if the configured Ethernet endpoint is reachable."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host=host, port=port),
                timeout=3.0,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Ethernet connectivity probe failed for %s:%d: %s", host, port, exc)
            return False

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True

    # ------------------------------------------------------------------
    # Pairing method selection
    # ------------------------------------------------------------------

    async def async_step_pairing_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user whether their gateway uses Push-to-Pair or PIN."""
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
                            PairingMethod.PIN.value: "PIN (legacy gateway, no Connect button)",
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
            pin = user_input.get(CONF_GATEWAY_PIN, DEFAULT_GATEWAY_PIN)
            bt_pin = user_input.get(CONF_BLUETOOTH_PIN, "")

            if not pin or len(pin) != 6 or not pin.isdigit():
                errors[CONF_GATEWAY_PIN] = "invalid_pin"
            elif bt_pin and (len(bt_pin) != 6 or not bt_pin.isdigit()):
                errors[CONF_BLUETOOTH_PIN] = "invalid_pin"
            else:
                data = {
                    CONF_ADDRESS: self._address,
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_BLE,
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

        # For PIN gateways, show a separate step with extra context
        step_id = "confirm"
        if self._pairing_method == PairingMethod.PIN:
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


class OneControlOptionsFlow(OptionsFlow):
    """Handle options for OneControl."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            manifest_path = str(user_input.get(CONF_NAMING_MANIFEST_PATH, "")).strip()
            snapshot_path = str(user_input.get(CONF_NAMING_SNAPSHOT_PATH, "")).strip()
            manifest_json = str(user_input.get(CONF_NAMING_MANIFEST_JSON, "")).strip()
            snapshot_json = str(user_input.get(CONF_NAMING_SNAPSHOT_JSON, "")).strip()

            if manifest_path or snapshot_path or manifest_json or snapshot_json:
                try:
                    await self.hass.async_add_executor_job(
                        load_external_name_catalog,
                        manifest_path or None,
                        snapshot_path or None,
                        manifest_json or None,
                        snapshot_json or None,
                    )
                except Exception:  # noqa: BLE001
                    errors["base"] = "invalid_naming_file"

            if not errors:
                options: dict[str, Any] = dict(self._config_entry.options)
                if manifest_path:
                    options[CONF_NAMING_MANIFEST_PATH] = manifest_path
                else:
                    options.pop(CONF_NAMING_MANIFEST_PATH, None)

                if snapshot_path:
                    options[CONF_NAMING_SNAPSHOT_PATH] = snapshot_path
                else:
                    options.pop(CONF_NAMING_SNAPSHOT_PATH, None)

                if manifest_json:
                    options[CONF_NAMING_MANIFEST_JSON] = manifest_json
                else:
                    options.pop(CONF_NAMING_MANIFEST_JSON, None)

                if snapshot_json:
                    options[CONF_NAMING_SNAPSHOT_JSON] = snapshot_json
                else:
                    options.pop(CONF_NAMING_SNAPSHOT_JSON, None)

                return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_NAMING_MANIFEST_PATH,
                        default=self._config_entry.options.get(CONF_NAMING_MANIFEST_PATH, ""),
                    ): str,
                    vol.Optional(
                        CONF_NAMING_SNAPSHOT_PATH,
                        default=self._config_entry.options.get(CONF_NAMING_SNAPSHOT_PATH, ""),
                    ): str,
                    vol.Optional(
                        CONF_NAMING_MANIFEST_JSON,
                        default=self._config_entry.options.get(CONF_NAMING_MANIFEST_JSON, ""),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional(
                        CONF_NAMING_SNAPSHOT_JSON,
                        default=self._config_entry.options.get(CONF_NAMING_SNAPSHOT_JSON, ""),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                }
            ),
            errors=errors,
        )
