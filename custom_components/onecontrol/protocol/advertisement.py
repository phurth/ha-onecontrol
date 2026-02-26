"""BLE advertisement parser for OneControl gateways.

Detects gateway capabilities from Lippert manufacturer-specific data.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from ..const import LIPPERT_MANUFACTURER_ID


class PairingMethod(enum.Enum):
    """How the gateway expects to be paired."""

    UNKNOWN = "unknown"
    NONE = "none"
    PIN = "pin"
    PUSH_BUTTON = "push_button"


@dataclass(frozen=True)
class GatewayCapabilities:
    """Parsed capabilities from a gateway advertisement."""

    pairing_method: PairingMethod
    supports_push_to_pair: bool
    pairing_enabled: bool  # True when physical Connect button is pressed


def parse_manufacturer_data(
    manufacturer_data: dict[int, bytes],
) -> GatewayCapabilities:
    """Parse manufacturer-specific data dict from a BLE advertisement.

    *manufacturer_data* maps company-id → raw data (as provided by Bleak /
    HA ``BluetoothServiceInfoBleak``).

    Lippert manufacturer ID is 0x0499 (1177).  The first byte after the
    company ID is the ``PairingInfo`` byte:
      - Bit 0: IsPushToPairButtonPresentOnBus
      - Bit 1: PairingEnabled (button currently pressed)
    """
    raw = manufacturer_data.get(LIPPERT_MANUFACTURER_ID)

    if raw is None or len(raw) == 0:
        # No Lippert data → default to push-to-pair (newer gateway assumption)
        return GatewayCapabilities(
            pairing_method=PairingMethod.PUSH_BUTTON,
            supports_push_to_pair=True,
            pairing_enabled=False,
        )

    pairing_info = raw[0] & 0xFF
    has_push_button = bool(pairing_info & 0x01)
    pairing_active = bool(pairing_info & 0x02)

    method = PairingMethod.PUSH_BUTTON if has_push_button else PairingMethod.PIN

    return GatewayCapabilities(
        pairing_method=method,
        supports_push_to_pair=has_push_button,
        pairing_enabled=pairing_active,
    )
