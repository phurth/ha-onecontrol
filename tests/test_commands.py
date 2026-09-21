"""Tests for the MyRvLink command builder.

The H-Bridge assertions deliberately spell out the wire bytes rather than
comparing against the builder's own constants: 1.0.44-1.0.46 shipped movement
codes that were each one too low, and a test written in terms of the constants
passes no matter what they hold.
"""

from custom_components.ha_onecontrol.protocol.commands import CommandBuilder


def test_build_action_hbridge_encodes_open_close_stop() -> None:
    """Open extends (movement code 2), close retracts (3), stop is 0."""
    builder = CommandBuilder()

    open_command = builder.build_action_hbridge(1, 0x12, builder.HBRIDGE_OPEN)
    assert open_command[-4:] == bytes([builder.CMD_ACTION_HBRIDGE, 1, 0x12, 0x82])

    close_command = builder.build_action_hbridge(2, 0x34, builder.HBRIDGE_CLOSE)
    assert close_command[-4:] == bytes([builder.CMD_ACTION_HBRIDGE, 2, 0x34, 0x83])

    stop_command = builder.build_action_hbridge(3, 0x56, builder.HBRIDGE_STOP)
    assert stop_command[-4:] == bytes([builder.CMD_ACTION_HBRIDGE, 3, 0x56, 0x80])


def test_open_does_not_use_the_reserved_movement_code() -> None:
    """0x81 is a reserved slot; gateways reject it as an invalid command.

    Sending it left the awning motionless while the gateway answered with
    failure code 14, so guard the specific byte rather than only the mapping.
    """
    assert CommandBuilder.HBRIDGE_OPEN_CMD != 0x81


def test_close_does_not_send_the_forward_movement() -> None:
    """Retract must not reuse the extend code.

    When close carried the forward movement code, pressing close drove awnings
    open instead of shut.
    """
    assert CommandBuilder.HBRIDGE_CLOSE_CMD != CommandBuilder.HBRIDGE_OPEN_CMD
    assert CommandBuilder.HBRIDGE_CLOSE_CMD != 0x82


def test_every_hbridge_command_sets_the_valid_bit() -> None:
    """Bit 7 marks the movement command valid; without it the gateway ignores it."""
    for command in (
        CommandBuilder.HBRIDGE_STOP_CMD,
        CommandBuilder.HBRIDGE_OPEN_CMD,
        CommandBuilder.HBRIDGE_CLOSE_CMD,
    ):
        assert command & CommandBuilder.HBRIDGE_VALID_BIT


def test_build_action_hbridge_frame_layout() -> None:
    """The full frame is [cmdId LE][0x41][table][device][movement]."""
    builder = CommandBuilder()

    command = builder.build_action_hbridge(0x0C, 0x06, builder.HBRIDGE_OPEN)

    assert len(command) == 6
    assert command[2] == builder.CMD_ACTION_HBRIDGE
    assert command[3] == 0x0C
    assert command[4] == 0x06
    assert command[5] == 0x82


def test_build_action_hbridge_raw_command_bytes() -> None:
    """Raw command bytes (valid bit set) pass through unchanged."""
    builder = CommandBuilder()

    cmd = builder.build_action_hbridge(1, 0x0C, 0x83)
    assert cmd[-4:] == bytes([builder.CMD_ACTION_HBRIDGE, 1, 0x0C, 0x83])


def test_hbridge_command_byte_mapping() -> None:
    """_hbridge_command_byte maps logical directions and passes through raw bytes."""
    builder = CommandBuilder()

    assert builder._hbridge_command_byte(builder.HBRIDGE_STOP) == 0x80
    assert builder._hbridge_command_byte(builder.HBRIDGE_OPEN) == 0x82
    assert builder._hbridge_command_byte(builder.HBRIDGE_CLOSE) == 0x83

    # Raw command bytes pass through unchanged (valid bit set)
    assert builder._hbridge_command_byte(0x80) == 0x80
    assert builder._hbridge_command_byte(0x82) == 0x82
    assert builder._hbridge_command_byte(0x83) == 0x83

    # Unknown directions fall back to stop rather than guessing a movement
    assert builder._hbridge_command_byte(0x07) == 0x80
    assert builder._hbridge_command_byte(0xFF) == 0xFF  # valid bit set → passthrough
