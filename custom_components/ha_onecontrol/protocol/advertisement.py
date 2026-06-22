"""BLE advertisement parser for OneControl gateways.

Detects gateway capabilities from Lippert manufacturer-specific data.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from ..const import LIPPERT_MANUFACTURER_ID, X180T_DISCOVERY_SERVICE_UUID


class PairingMethod(enum.Enum):
    """How the gateway expects to be paired."""

    UNKNOWN = "unknown"
    NONE = "none"
    PIN = "pin"
    PUSH_BUTTON = "push_button"


class BleCapability(enum.IntEnum):
    """Official BLE IO capability advertised in ConnectionInfo TLV."""

    DISPLAY_ONLY = 0
    DISPLAY_YES_NO = 1
    KEYBOARD_ONLY = 2
    NO_IO = 3
    KEYBOARD_AND_DISPLAY = 4


@dataclass(frozen=True)
class GatewayCapabilities:
    """Parsed capabilities from a gateway advertisement."""

    pairing_method: PairingMethod
    supports_push_to_pair: bool
    pairing_enabled: bool  # True when physical Connect button is pressed
    is_x180t: bool = False
    ble_capability: BleCapability | None = None
    can_gateway_protocol_version: int | None = None
    advertised_gateway_version: str | None = None
    uses_modern_tlv: bool = False


def _normalise_uuid(uuid: str) -> str:
    """Return a lower-case UUID string for comparison."""
    return uuid.lower()


def _decode_advertised_gateway_version(raw_version: int | None) -> str | None:
    """Decode official advertised CAN gateway protocol version."""
    if raw_version is None:
        return None
    # Official BleCanGatewayProtocolVersion maps values below 68 to Unknown;
    # 68+ indicates the V2_D gateway packing strategy.
    return "V2_D" if raw_version >= 68 else "Unknown"


def _pairing_method_from_capability(
    capability: BleCapability | None,
) -> PairingMethod:
    """Map official ConnectionInfo.BleCapability to PairingMethod."""
    if capability is None:
        return PairingMethod.UNKNOWN
    if capability == BleCapability.DISPLAY_ONLY:
        return PairingMethod.PIN
    if capability == BleCapability.NO_IO:
        return PairingMethod.PUSH_BUTTON
    return PairingMethod.NONE


def _parse_lippert_tlv(raw: bytes) -> GatewayCapabilities | None:
    """Parse official Lippert TLV manufacturer data.

    Home Assistant/Bleak manufacturer_data values exclude the two-byte company
    identifier, so TLV records begin at offset 0 here.  Each record is encoded
    as [length][type][payload...], where length includes the type byte but not
    the length byte.
    """
    index = 0
    connection_info: tuple[int, int] | None = None
    pairing_info: int | None = None
    can_protocol_version: int | None = None
    recognised = False

    while index + 2 <= len(raw):
        length = raw[index]
        if length < 1 or index + 1 + length > len(raw):
            return None

        tlv_type = raw[index + 1]
        payload = raw[index + 2:index + 1 + length]

        if tlv_type == 0 and len(payload) >= 2:  # ConnectionInfo
            connection_info = (payload[0], payload[1])
            recognised = True
        elif tlv_type == 1 and payload:  # BleCanGatewayProtocolVersion
            can_protocol_version = payload[0]
            recognised = True
        elif tlv_type == 5 and payload:  # PairingInfo
            pairing_info = payload[0]
            recognised = True

        index += 1 + length

    if not recognised or index != len(raw):
        return None

    ble_capability: BleCapability | None = None
    pairing_supported = False
    pairing_available_now = False
    if connection_info is not None:
        status, pairing = connection_info
        try:
            ble_capability = BleCapability(status & 0x0F)
        except ValueError:
            ble_capability = None
        pairing_supported = bool(pairing & 0x01)
        pairing_available_now = bool(pairing & 0x02)

    method = _pairing_method_from_capability(ble_capability)
    supports_push_to_pair = bool(pairing_info & 0x01) if pairing_info is not None else (
        method == PairingMethod.PUSH_BUTTON
    )
    pairing_enabled = (
        pairing_supported
        and pairing_available_now
        and method not in (PairingMethod.NONE, PairingMethod.UNKNOWN)
    )

    return GatewayCapabilities(
        pairing_method=method,
        supports_push_to_pair=supports_push_to_pair,
        pairing_enabled=pairing_enabled,
        ble_capability=ble_capability,
        can_gateway_protocol_version=can_protocol_version,
        advertised_gateway_version=_decode_advertised_gateway_version(
            can_protocol_version
        ),
        uses_modern_tlv=True,
    )


def parse_manufacturer_data(
    manufacturer_data: dict[int, bytes],
) -> GatewayCapabilities:
    """Parse manufacturer-specific data dict from a BLE advertisement.

    *manufacturer_data* maps company-id → raw data (as provided by Bleak /
    HA ``BluetoothServiceInfoBleak``).

        Lippert manufacturer ID is 0x0499 (1177).  Modern IDS-CAN gateways use
        the official TLV format.  Older gateways are interpreted with the legacy
        first-byte ``PairingInfo`` format:
      - Bit 0: IsPushToPairButtonPresentOnBus
      - Bit 1: PairingEnabled (button currently pressed)
    """
    raw = manufacturer_data.get(LIPPERT_MANUFACTURER_ID)

    if raw is None or len(raw) == 0:
        # No Lippert manufacturer data in this advertisement.  Its absence does
        # NOT imply a pairing method — legacy PIN gateways are among the units
        # that advertise no parseable manufacturer payload.  Leave the method
        # UNKNOWN so the config flow asks the user explicitly instead of silently
        # assuming push-to-pair (which mislabeled PIN gateways).
        return GatewayCapabilities(
            pairing_method=PairingMethod.UNKNOWN,
            supports_push_to_pair=False,
            pairing_enabled=False,
        )

    tlv = _parse_lippert_tlv(raw)
    if tlv is not None:
        return tlv

    # Legacy (non-TLV) PairingInfo first byte:
    #   Bit 0: IsPushToPairButtonPresentOnBus — a hardware *capability*
    #   Bit 1: PairingEnabled — button currently pressed (pairing window open)
    # Bit 0 only reports that a Connect button exists on the bus; it does NOT
    # mean the gateway pairs via push-to-pair.  A gateway can expose the button
    # and still require a PIN, so the method must not be inferred from it.  The
    # legacy advertisement does not encode the method, so leave it UNKNOWN and
    # let the config flow ask.
    pairing_info = raw[0] & 0xFF
    has_push_button = bool(pairing_info & 0x01)
    pairing_active = bool(pairing_info & 0x02)

    return GatewayCapabilities(
        pairing_method=PairingMethod.UNKNOWN,
        supports_push_to_pair=has_push_button,
        pairing_enabled=pairing_active,
    )


def parse_gateway_advertisement(
    manufacturer_data: dict[int, bytes],
    service_uuids: list[str] | tuple[str, ...] | None,
) -> GatewayCapabilities:
    """Parse a complete gateway advertisement.

    Adds primary-service classification on top of manufacturer data parsing.
    The Unity X180T is identified by official app service 0000000F-0200-...
    """
    capabilities = parse_manufacturer_data(manufacturer_data)
    advertised_services = {
        _normalise_uuid(uuid) for uuid in (service_uuids or [])
    }
    is_x180t = _normalise_uuid(X180T_DISCOVERY_SERVICE_UUID) in advertised_services

    if not is_x180t:
        return capabilities

    if not capabilities.uses_modern_tlv:
        return GatewayCapabilities(
            pairing_method=PairingMethod.UNKNOWN,
            supports_push_to_pair=capabilities.supports_push_to_pair,
            pairing_enabled=False,
            is_x180t=True,
        )

    return GatewayCapabilities(
        pairing_method=capabilities.pairing_method,
        supports_push_to_pair=capabilities.supports_push_to_pair,
        pairing_enabled=capabilities.pairing_enabled,
        is_x180t=True,
        ble_capability=capabilities.ble_capability,
        can_gateway_protocol_version=capabilities.can_gateway_protocol_version,
        advertised_gateway_version=capabilities.advertised_gateway_version,
        uses_modern_tlv=capabilities.uses_modern_tlv,
    )
