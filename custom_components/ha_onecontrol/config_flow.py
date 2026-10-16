"""Config flow for OneControl BLE integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_ADVERTISED_GATEWAY_VERSION,
    CONF_BLUETOOTH_PIN,
    CONF_ENABLE_COVER_CONTROL,
    CONF_GATEWAY_FAMILY,
    CONF_COVER_SAFETY_TIMEOUT,
    CONF_COVER_TRAVEL,
    CONF_GATEWAY_PIN,
    CONF_PAIRING_METHOD,
    DEFAULT_COVER_SAFETY_TIMEOUT,
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

# Sentinel shown when the advertisement can't tell us the pairing method (legacy
# gateways).  Forces the user to choose explicitly rather than letting the form
# silently default to its first option (push-to-pair), which mislabeled PIN
# gateways that expose a Connect button but pair with a PIN.
_PAIRING_METHOD_UNSET = "unset"


class OneControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OneControl."""

    # Must match the highest version async_migrate_entry produces.  Home
    # Assistant refuses to load an entry whose stored version is ahead of this
    # number, so a migration ladder that outruns it strands upgraded installs
    # on their second restart.
    VERSION = 4

    def __init__(self) -> None:
        """Initialise flow state."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None
        self._pairing_method: PairingMethod = PairingMethod.UNKNOWN
        self._gateway_family: str = GATEWAY_FAMILY_LEGACY
        self._advertised_gateway_version: str | None = None
        # Pre-fill values when reconfiguring an existing entry.
        self._current_gateway_pin: str | None = None
        self._current_bt_pin: str = ""

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
            "push_button=%s tlv=%s ble_capability=%s advertised_gateway_version=%s "
            "services=%s manufacturer_data=%s",
            discovery_info.address,
            self._gateway_family,
            capabilities.pairing_method.value,
            capabilities.pairing_enabled,
            capabilities.supports_push_to_pair,
            capabilities.uses_modern_tlv,
            capabilities.ble_capability.name if capabilities.ble_capability else None,
            capabilities.advertised_gateway_version,
            discovery_info.service_uuids,
            discovery_info.manufacturer_data,
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

    @staticmethod
    def async_get_options_flow(
        config_entry: "ConfigEntry",
    ) -> OptionsFlow:
        """Return the options flow for this handler."""
        return OneControlOptionsFlow()

    # ------------------------------------------------------------------
    # Reconfigure an existing entry (change pairing method / PIN in place)
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-ask the pairing method and PIN for an already-configured gateway.

        Lets a user correct a mislabeled gateway (e.g. a PIN gateway that was
        onboarded as push-to-pair) without deleting and re-adding it.  Reuses the
        pairing-method → confirm steps; the difference is the final action
        updates and reloads the existing entry instead of creating a new one.
        """
        entry = self._get_reconfigure_entry()
        self._address = entry.data[CONF_ADDRESS]
        self._name = entry.title
        self._gateway_family = entry.data.get(
            CONF_GATEWAY_FAMILY, GATEWAY_FAMILY_LEGACY
        )
        self._advertised_gateway_version = entry.data.get(
            CONF_ADVERTISED_GATEWAY_VERSION
        )
        self._current_gateway_pin = entry.data.get(CONF_GATEWAY_PIN)
        self._current_bt_pin = entry.data.get(CONF_BLUETOOTH_PIN, "")
        try:
            # Pre-select the gateway's currently-stored method in the form.
            self._pairing_method = PairingMethod(
                entry.data.get(CONF_PAIRING_METHOD, PairingMethod.UNKNOWN.value)
            )
        except ValueError:
            self._pairing_method = PairingMethod.UNKNOWN

        return await self.async_step_pairing_method()

    # ------------------------------------------------------------------
    # Pairing method selection
    # ------------------------------------------------------------------

    async def async_step_pairing_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user whether their gateway uses Push-to-Pair or PIN."""
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = user_input[CONF_PAIRING_METHOD]
            if chosen == _PAIRING_METHOD_UNSET:
                errors[CONF_PAIRING_METHOD] = "select_pairing_method"
            else:
                self._pairing_method = PairingMethod(chosen)
                return await self.async_step_confirm()

        # Pre-select a method ONLY when the advertisement told us authoritatively
        # (modern TLV BleCapability → PIN or PUSH_BUTTON).  Legacy gateways arrive
        # as UNKNOWN and the advertisement cannot distinguish PIN from push-to-pair,
        # so force an explicit choice rather than letting the form default to its
        # first option (which silently mislabeled PIN gateways as push-to-pair).
        options: dict[str, str] = {
            PairingMethod.PIN.value: "PIN/passkey pairing (6-digit Bluetooth PIN)",
            PairingMethod.PUSH_BUTTON.value: "Push-to-Pair (has a physical Connect button)",
        }
        if self._pairing_method in (PairingMethod.PIN, PairingMethod.PUSH_BUTTON):
            method_key: Any = vol.Required(
                CONF_PAIRING_METHOD, default=self._pairing_method.value
            )
        else:
            options = {
                _PAIRING_METHOD_UNSET: "— Select your gateway's pairing method —",
                **options,
            }
            method_key = vol.Required(
                CONF_PAIRING_METHOD, default=_PAIRING_METHOD_UNSET
            )

        return self.async_show_form(
            step_id="pairing_method",
            data_schema=vol.Schema({method_key: vol.In(options)}),
            errors=errors,
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
                # Reconfigure: update the changed fields on the existing entry,
                # preserving everything else, then reload.
                if self.source == SOURCE_RECONFIGURE:
                    entry = self._get_reconfigure_entry()
                    new_data = {**entry.data}
                    new_data[CONF_GATEWAY_PIN] = pin
                    new_data[CONF_PAIRING_METHOD] = self._pairing_method.value
                    if bt_pin:
                        new_data[CONF_BLUETOOTH_PIN] = bt_pin
                    else:
                        new_data.pop(CONF_BLUETOOTH_PIN, None)
                    return self.async_update_reload_and_abort(entry, data=new_data)

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
        # only for PIN-based gateways.  When reconfiguring, pre-fill the values
        # already stored on the entry.
        gateway_pin_default = self._current_gateway_pin or DEFAULT_GATEWAY_PIN
        fields: dict[Any, Any] = {
            vol.Required(CONF_GATEWAY_PIN, default=gateway_pin_default): str,
        }

        # For PIN gateways, show a separate step with extra context
        step_id = "confirm"
        if self._gateway_family == GATEWAY_FAMILY_X180T:
            fields = {}
            if self._pairing_method == PairingMethod.PIN:
                fields[
                    vol.Required(CONF_BLUETOOTH_PIN, default=self._current_bt_pin)
                ] = str
            step_id = "confirm_x180t"
        elif self._pairing_method == PairingMethod.PIN:
            fields[
                vol.Optional(CONF_BLUETOOTH_PIN, default=self._current_bt_pin)
            ] = str
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


class OneControlOptionsFlow(OptionsFlow):
    """Handle options for an existing OneControl entry.

    Cover (awning/slide) motor control is opt-in and off by default.  When
    enabled, two travel-time notions are kept distinct:

    * the **fallback travel time** (``cover_safety_timeout``) — how long one
      open/close press runs the motor when a cover has no override, and
    * **per-cover overrides** (``cover_travel``) — optional, per-direction,
      keyed by the "tt:dd" cover key.

    The actual failsafe ceiling is derived from the travel time (see the
    coordinator) and never entered by hand.
    """

    def _coordinator(self) -> Any:
        """Return the live coordinator for this entry, if one exists."""
        entries = self.hass.data.get(DOMAIN, {}) if self.hass else {}
        return entries.get(self.config_entry.entry_id)

    def _discovered_covers(self) -> list[tuple[str, str, int, int]]:
        """Return discovered covers as (key, friendly name, table, device).

        Covers are only enumerable while the gateway is connected, so this may
        be empty mid-setup; the travel step is simply skipped in that case.
        """
        coordinator = self._coordinator()
        if coordinator is None:
            return []
        out: list[tuple[str, str, int, int]] = []
        for key in sorted(coordinator.covers):
            try:
                table_s, device_s = key.split(":")
                table_id, device_id = int(table_s, 16), int(device_s, 16)
            except ValueError:
                continue
            name = coordinator.device_name(table_id, device_id)
            out.append((key, name, table_id, device_id))
        return out

    @staticmethod
    def _travel_field(value: Any) -> float | None:
        """Coerce a travel-time form field; blank/None means "use the fallback"."""
        if value is None or value == "":
            return None
        try:
            return max(1.0, min(60.0, float(value)))
        except (TypeError, ValueError):
            raise vol.Invalid("must be a number of seconds between 1 and 60")

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options: cover-control toggle + fallback travel time."""
        current = self.config_entry.options

        def _schema() -> vol.Schema:
            return vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLE_COVER_CONTROL,
                        default=current.get(CONF_ENABLE_COVER_CONTROL, False),
                    ): bool,
                    vol.Required(
                        CONF_COVER_SAFETY_TIMEOUT,
                        default=current.get(
                            CONF_COVER_SAFETY_TIMEOUT,
                            DEFAULT_COVER_SAFETY_TIMEOUT,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=1.0, max=60.0)),
                }
            )

        if user_input is not None:
            self._enable_cover_control = bool(user_input[CONF_ENABLE_COVER_CONTROL])
            self._fallback_travel = float(user_input[CONF_COVER_SAFETY_TIMEOUT])
            # Preserve any previously stored per-cover overrides even when cover
            # control is being toggled off (they only take effect when enabled),
            # and when no covers are currently discoverable (offline gateway).
            existing = self.config_entry.options.get(CONF_COVER_TRAVEL)
            kept: dict[str, dict[str, float]] = {}
            if isinstance(existing, dict):
                kept = {k: dict(v) for k, v in existing.items() if isinstance(v, dict)}
            if self._enable_cover_control and self._discovered_covers():
                self._kept_overrides = kept
                return await self.async_step_travel()
            return self._create_options(kept)

        return self.async_show_form(
            step_id="init",
            data_schema=_schema(),
        )

    async def async_step_travel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect optional per-cover, per-direction travel-time overrides."""
        covers = self._discovered_covers()
        current_overrides: dict[str, Any] = (
            self.config_entry.options.get(CONF_COVER_TRAVEL) or {}
        )

        if user_input is not None:
            merged: dict[str, dict[str, float]] = {}
            for cover_key, _name, _table, _device in covers:
                entry: dict[str, float] = {}
                ext = self._travel_field(user_input.get(f"extend_{cover_key}"))
                ret = self._travel_field(user_input.get(f"retract_{cover_key}"))
                if ext is not None:
                    entry["extend"] = ext
                if ret is not None:
                    entry["retract"] = ret
                if entry:
                    merged[cover_key] = entry
            # Carry overrides for covers not currently discovered so a temporary
            # disconnect does not wipe them.
            kept = getattr(self, "_kept_overrides", {})
            for key, val in kept.items():
                if key not in merged:
                    merged[key] = dict(val)
            return self._create_options(merged)

        fields: dict[Any, Any] = {}
        for cover_key, _name, _table, _device in covers:
            prev = current_overrides.get(cover_key) or {}
            fields[vol.Optional(
                f"extend_{cover_key}",
                default=prev.get("extend"),
            )] = self._travel_field
            fields[vol.Optional(
                f"retract_{cover_key}",
                default=prev.get("retract"),
            )] = self._travel_field

        return self.async_show_form(
            step_id="travel",
            data_schema=vol.Schema(fields),
        )

    def _create_options(self, cover_travel: dict[str, dict[str, float]]) -> ConfigFlowResult:
        """Build and persist the options entry.

        Merges onto the existing options so unrelated keys (e.g. the bonded
        BLE source the coordinator persists there) survive a save of this form.
        """
        options = {
            **self.config_entry.options,
            CONF_ENABLE_COVER_CONTROL: self._enable_cover_control,
            CONF_COVER_SAFETY_TIMEOUT: self._fallback_travel,
        }
        if cover_travel:
            options[CONF_COVER_TRAVEL] = cover_travel
        else:
            options.pop(CONF_COVER_TRAVEL, None)
        return self.async_create_entry(data=options)

