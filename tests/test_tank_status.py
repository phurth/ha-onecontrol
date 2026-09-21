"""Tests for tank sensor status parsing (0x0C and 0x1B).

Both frame types carry the fill level as a percentage (0-100) directly.  1.0.44
rescaled them as if the byte were a 0-255 gauge reading, which reported a third
of the true level: a tank at 33% came through as 12%.
"""

from custom_components.ha_onecontrol.protocol.events import (
    parse_tank_status,
    parse_tank_status_v2,
)


def test_v2_level_is_a_percentage() -> None:
    """A real 0x1B frame from a tank at one third reports 33%, not 12%.

    Captured from an X270D gateway whose monitor panel showed the first of
    three LEDs lit; the status byte is 0x21 = 33.
    """
    tank = parse_tank_status_v2(bytes.fromhex("1b0c1321ffff80804e0000"))

    assert tank is not None
    assert tank.table_id == 0x0C
    assert tank.device_id == 0x13
    assert tank.level == 33


def test_v2_masks_the_status_flag_bit() -> None:
    """Bit 7 of the status byte is a flag, not part of the level."""
    tank = parse_tank_status_v2(bytes([0x1B, 0x0C, 0x13, 0x21 | 0x80]))

    assert tank is not None
    assert tank.level == 33


def test_v2_clamps_above_full() -> None:
    """Values past 100 (once masked) are clamped rather than reported raw."""
    tank = parse_tank_status_v2(bytes([0x1B, 0x0C, 0x13, 0x7F]))

    assert tank is not None
    assert tank.level == 100


def test_v2_short_frame_is_rejected() -> None:
    """A frame without a status byte yields nothing."""
    assert parse_tank_status_v2(bytes([0x1B, 0x0C, 0x13])) is None


def test_multi_tank_levels_are_percentages() -> None:
    """Each level byte in a batched 0x0C frame is a percentage."""
    tanks = parse_tank_status(bytes([0x0C, 0x0C, 0x0B, 0x21, 0x0D, 0x64]))

    assert [(tank.device_id, tank.level) for tank in tanks] == [(0x0B, 33), (0x0D, 100)]
    assert all(tank.table_id == 0x0C for tank in tanks)


def test_multi_tank_clamps_above_full() -> None:
    """An out-of-range byte is clamped to full, not rescaled."""
    tanks = parse_tank_status(bytes([0x0C, 0x0C, 0x0B, 0xFF]))

    assert [tank.level for tank in tanks] == [100]


def test_multi_tank_short_frame_is_rejected() -> None:
    """A frame with no device/level pair yields no tanks."""
    assert parse_tank_status(bytes([0x0C, 0x0C, 0x0B])) == []
