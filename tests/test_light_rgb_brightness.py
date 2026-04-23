"""Tests for HA RGB brightness handling."""

from custom_components.ha_onecontrol.light import _apply_brightness_to_rgb


def test_apply_brightness_to_rgb_scales_preserving_ratios() -> None:
    """Brightness scaling should preserve color ratios for non-zero colors."""
    # Native mapping clamps to 5..250 before converting to HSV value.
    assert _apply_brightness_to_rgb((200, 100, 50), 100) == (99, 49, 25)


def test_apply_brightness_to_rgb_uses_white_when_color_unknown() -> None:
    """If source color is black/unknown, brightness should map to neutral white."""
    assert _apply_brightness_to_rgb((0, 0, 0), 64) == (61, 61, 61)


def test_apply_brightness_to_rgb_handles_off() -> None:
    """Zero brightness should produce black."""
    assert _apply_brightness_to_rgb((255, 10, 10), 0) == (0, 0, 0)
