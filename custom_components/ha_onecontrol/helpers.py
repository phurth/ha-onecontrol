from __future__ import annotations


def is_valid_device_id(device_id: int) -> bool:
    """Return True for a real device address.

    IDS-CAN source addresses are 8-bit and 0x00 is the "no device" sentinel the
    gateway reports for empty/phantom slots.  Entity platforms use this to avoid
    creating entities for those slots.  (Coach-specific phantom addresses are
    filtered structurally at the coordinator level via ``_invalid_can_sources``,
    not hardcoded here.)
    """
    return device_id != 0x00
