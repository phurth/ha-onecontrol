"""Tests for BLE advertisement parsing."""

from custom_components.ha_onecontrol.const import (
    LIPPERT_MANUFACTURER_ID,
    X180T_DISCOVERY_SERVICE_UUID,
)
from custom_components.ha_onecontrol.protocol.advertisement import (
    BleCapability,
    PairingMethod,
    parse_gateway_advertisement,
    parse_manufacturer_data,
)


def test_legacy_pairing_info_does_not_infer_method() -> None:
    """Legacy first-byte PairingInfo reports button capability/state but NOT the
    pairing method.

    Bit 0 ("push-to-pair button present on the bus") is a hardware capability —
    a PIN gateway can also expose a Connect button — so it must not be read as
    "pairs via push-to-pair".  The legacy advertisement doesn't encode the
    method, so it stays UNKNOWN and the config flow asks the user.
    """
    caps = parse_manufacturer_data({LIPPERT_MANUFACTURER_ID: bytes([0x03])})

    assert caps.pairing_method == PairingMethod.UNKNOWN
    assert caps.supports_push_to_pair is True  # bit 0: Connect button present
    assert caps.pairing_enabled is True  # bit 1: button currently pressed
    assert caps.uses_modern_tlv is False


def test_no_manufacturer_data_is_unknown() -> None:
    """Absence of Lippert manufacturer data must not be assumed push-to-pair."""
    caps = parse_manufacturer_data({})

    assert caps.pairing_method == PairingMethod.UNKNOWN
    assert caps.supports_push_to_pair is False
    assert caps.pairing_enabled is False


def test_modern_tlv_display_only_maps_to_pin() -> None:
    """Official ConnectionInfo DisplayOnly maps to PIN pairing."""
    # [len=3][type=0 ConnectionInfo][status=DisplayOnly][pairing=supported+available]
    # [len=2][type=5 PairingInfo][push button present]
    raw = bytes([0x03, 0x00, 0x00, 0x03, 0x02, 0x05, 0x01])
    caps = parse_manufacturer_data({LIPPERT_MANUFACTURER_ID: raw})

    assert caps.uses_modern_tlv is True
    assert caps.ble_capability == BleCapability.DISPLAY_ONLY
    assert caps.pairing_method == PairingMethod.PIN
    assert caps.supports_push_to_pair is True
    assert caps.pairing_enabled is True


def test_modern_tlv_no_io_maps_to_push_button() -> None:
    """Official ConnectionInfo NoIO maps to push-button pairing."""
    raw = bytes([0x03, 0x00, 0x03, 0x03])
    caps = parse_manufacturer_data({LIPPERT_MANUFACTURER_ID: raw})

    assert caps.uses_modern_tlv is True
    assert caps.ble_capability == BleCapability.NO_IO
    assert caps.pairing_method == PairingMethod.PUSH_BUTTON
    assert caps.pairing_enabled is True


def test_x180t_primary_service_and_gateway_version() -> None:
    """X180T is classified by its official primary service."""
    raw = bytes([
        0x03, 0x00, 0x00, 0x03,  # ConnectionInfo: DisplayOnly, available
        0x02, 0x01, 0x44,        # BleCanGatewayProtocolVersion: V2_D
        0x02, 0x05, 0x01,        # PairingInfo: button present
    ])
    caps = parse_gateway_advertisement(
        {LIPPERT_MANUFACTURER_ID: raw},
        [X180T_DISCOVERY_SERVICE_UUID.upper()],
    )

    assert caps.is_x180t is True
    assert caps.pairing_method == PairingMethod.PIN
    assert caps.can_gateway_protocol_version == 0x44
    assert caps.advertised_gateway_version == "V2_D"


def test_x180t_without_tlv_requires_manual_pairing_choice() -> None:
    """Do not assume Just Works for X180T if official TLV data is absent."""
    caps = parse_gateway_advertisement({}, [X180T_DISCOVERY_SERVICE_UUID])

    assert caps.is_x180t is True
    assert caps.pairing_method == PairingMethod.UNKNOWN
    assert caps.pairing_enabled is False
