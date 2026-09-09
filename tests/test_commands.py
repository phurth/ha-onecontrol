from custom_components.ha_onecontrol.protocol.commands import CommandBuilder


def test_build_action_hbridge_encodes_open_close_stop() -> None:
    builder = CommandBuilder()

    open_command = builder.build_action_hbridge(1, 0x12, builder.HBRIDGE_OPEN)
    assert open_command[-4:] == bytes([
        builder.CMD_ACTION_HBRIDGE,
        1,
        0x12,
        builder.HBRIDGE_OPEN_CMD,
    ])

    close_command = builder.build_action_hbridge(2, 0x34, builder.HBRIDGE_CLOSE)
    assert close_command[-4:] == bytes([
        builder.CMD_ACTION_HBRIDGE,
        2,
        0x34,
        builder.HBRIDGE_CLOSE_CMD,
    ])

    stop_command = builder.build_action_hbridge(3, 0x56, builder.HBRIDGE_STOP)
    assert stop_command[-4:] == bytes([
        builder.CMD_ACTION_HBRIDGE,
        3,
        0x56,
        builder.HBRIDGE_STOP_CMD,
    ])


def test_build_action_hbridge_raw_command_bytes() -> None:
    """Raw command bytes (high bit set) pass through unchanged."""
    builder = CommandBuilder()

    cmd = builder.build_action_hbridge(1, 0x0C, 0x82)
    assert cmd[-4:] == bytes([
        builder.CMD_ACTION_HBRIDGE,
        1,
        0x0C,
        0x82,
    ])


def test_hbridge_command_byte_mapping() -> None:
    """_hbridge_command_byte maps logical directions and passes through raw bytes."""
    builder = CommandBuilder()

    assert builder._hbridge_command_byte(builder.HBRIDGE_STOP) == builder.HBRIDGE_STOP_CMD
    assert builder._hbridge_command_byte(builder.HBRIDGE_OPEN) == builder.HBRIDGE_OPEN_CMD
    assert builder._hbridge_command_byte(builder.HBRIDGE_CLOSE) == builder.HBRIDGE_CLOSE_CMD

    # Raw command bytes pass through unchanged (high bit set)
    assert builder._hbridge_command_byte(0x80) == 0x80
    assert builder._hbridge_command_byte(0x81) == 0x81
    assert builder._hbridge_command_byte(0x82) == 0x82

    # Unknown direction values fall back to STOP
    assert builder._hbridge_command_byte(0x07) == builder.HBRIDGE_STOP_CMD
    assert builder._hbridge_command_byte(0xFF) == 0xFF  # high bit set → passthrough
