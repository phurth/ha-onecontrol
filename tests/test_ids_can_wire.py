"""Tests for IDS-CAN wire-frame parsing from Ethernet captures."""

from __future__ import annotations

from custom_components.ha_onecontrol.protocol.ids_can_wire import (
    compose_ids_can_extended_wire_frame,
    decode_ids_can_payload,
    format_ids_can_payload,
    ids_can_message_type_name,
    ids_can_request_name,
    ids_can_response_name,
    parse_ids_can_wire_frame,
)


def test_parse_standard_can_wire_frame_from_capture() -> None:
    """11-bit CAN wire frame should decode message type/source/payload."""
    frame = bytes.fromhex("0800cd031400000008e9cd")

    parsed = parse_ids_can_wire_frame(frame)

    assert parsed is not None
    assert parsed.dlc == 8
    assert parsed.is_extended is False
    assert parsed.can_id == 0x00CD
    assert parsed.message_type == 0x00
    assert ids_can_message_type_name(parsed.message_type) == "NETWORK"
    assert parsed.source_address == 0xCD
    assert parsed.target_address is None
    assert parsed.message_data is None
    assert parsed.payload == bytes.fromhex("031400000008e9cd")

    decoded = decode_ids_can_payload(parsed)
    assert decoded is not None
    assert decoded.kind == "network"
    assert decoded.fields["protocol_version"] == 0x14
    assert decoded.fields["mac"] == "00000008E9CD"
    assert decoded.fields["in_motion_lockout_level"] == 0


def test_parse_standard_device_status_wire_frame_from_capture() -> None:
    """11-bit DEVICE_STATUS frame shape should decode correctly."""
    frame = bytes.fromhex("0603da80ff00200000")

    parsed = parse_ids_can_wire_frame(frame)

    assert parsed is not None
    assert parsed.dlc == 6
    assert parsed.is_extended is False
    assert parsed.can_id == 0x03DA
    assert parsed.message_type == 0x03
    assert ids_can_message_type_name(parsed.message_type) == "DEVICE_STATUS"
    assert parsed.source_address == 0xDA
    assert parsed.payload == bytes.fromhex("80ff00200000")

    decoded = decode_ids_can_payload(parsed)
    assert decoded is not None
    assert decoded.kind == "device_status"
    assert decoded.fields["status_length"] == 6
    assert decoded.fields["status_hex"] == "80ff00200000"


def test_parse_extended_request_wire_frame_from_capture() -> None:
    """29-bit extended REQUEST frame shape should decode p2p fields."""
    frame = bytes.fromhex("0280e87511002b")

    parsed = parse_ids_can_wire_frame(frame)

    assert parsed is not None
    assert parsed.dlc == 2
    assert parsed.is_extended is True
    assert parsed.can_id == 0x00E87511
    assert parsed.message_type == 0x80
    assert ids_can_message_type_name(parsed.message_type) == "REQUEST"
    assert parsed.source_address == 0x3A
    assert parsed.target_address == 0x75
    assert parsed.message_data == 0x11
    assert parsed.payload == bytes.fromhex("002b")

    decoded = decode_ids_can_payload(parsed)
    assert decoded is not None
    assert decoded.kind == "request"
    assert decoded.fields["request_code"] == 0x11
    assert decoded.fields["request_name"] == "PID_READ_WRITE"


def test_parse_extended_text_console_wire_frame_from_capture() -> None:
    """29-bit TEXT_CONSOLE frame should decode as type 0x84."""
    frame = bytes.fromhex("088564002a2020202020202020")

    parsed = parse_ids_can_wire_frame(frame)

    assert parsed is not None
    assert parsed.dlc == 8
    assert parsed.is_extended is True
    assert parsed.can_id == 0x0564002A
    assert parsed.message_type == 0x84
    assert ids_can_message_type_name(parsed.message_type) == "TEXT_CONSOLE"
    assert parsed.source_address == 0x59
    assert parsed.target_address == 0x00
    assert parsed.message_data == 0x2A
    assert parsed.payload == bytes.fromhex("2020202020202020")

    decoded = decode_ids_can_payload(parsed)
    assert decoded is not None
    assert decoded.kind == "text_console"
    assert decoded.fields["text_ascii"] == "        "


def test_parse_standard_device_id_wire_frame() -> None:
    """DEVICE_ID payload bytes should decode to C#-parity fields."""
    # dlc=8, id=0x02DA (message type 0x02, source 0xDA)
    # payload: product_id=0x1234, product_instance=0x56, device_type=0x78,
    # function_name=0x0123, hi/lo nibble=dev_inst 0xA, fn_inst 0xB, caps=0xCD
    frame = bytes.fromhex("0802da123456780123abcd")

    parsed = parse_ids_can_wire_frame(frame)
    assert parsed is not None
    assert parsed.message_type == 0x02

    decoded = decode_ids_can_payload(parsed)
    assert decoded is not None
    assert decoded.kind == "device_id"
    assert decoded.fields["product_id"] == 0x1234
    assert decoded.fields["product_instance"] == 0x56
    assert decoded.fields["device_type"] == 0x78
    assert decoded.fields["device_instance"] == 0x0A
    assert decoded.fields["function_name"] == 0x0123
    assert decoded.fields["function_instance"] == 0x0B
    assert decoded.fields["device_capabilities"] == 0xCD


def test_parse_standard_circuit_id_wire_frame() -> None:
    """CIRCUIT_ID payload should decode to uint and text form."""
    frame = bytes.fromhex("0401da12345678")

    parsed = parse_ids_can_wire_frame(frame)
    assert parsed is not None
    assert parsed.message_type == 0x01

    decoded = decode_ids_can_payload(parsed)
    assert decoded is not None
    assert decoded.kind == "circuit_id"
    assert decoded.fields["circuit_id"] == 0x12345678
    assert decoded.fields["circuit_id_text"] == "12:34:56:78"


def test_parse_standard_product_status_wire_frame() -> None:
    """PRODUCT_STATUS payload byte 0 low two bits carry update state."""
    frame = bytes.fromhex("0106da83")

    parsed = parse_ids_can_wire_frame(frame)
    assert parsed is not None
    assert parsed.message_type == 0x06

    decoded = decode_ids_can_payload(parsed)
    assert decoded is not None
    assert decoded.kind == "product_status"
    assert decoded.fields["software_update_state"] == 0x03


def test_request_and_response_name_maps() -> None:
    """Request/response enum names should match decompiled constants."""
    assert ids_can_request_name(0x42) == "SESSION_REQUEST_SEED"
    assert ids_can_request_name(0xEE).startswith("UNKNOWN_")
    assert ids_can_response_name(0x00) == "SUCCESS"
    assert ids_can_response_name(0x16) == "IN_PROGRESS"
    assert ids_can_response_name(0xEE).startswith("UNKNOWN_")


def test_format_ids_can_payload_compact_suffix() -> None:
    """Formatted semantic suffix should include semantic kind and key-value fields."""
    frame = bytes.fromhex("0401da12345678")
    parsed = parse_ids_can_wire_frame(frame)
    assert parsed is not None
    decoded = decode_ids_can_payload(parsed)
    suffix = format_ids_can_payload(decoded)

    assert suffix.startswith(" semantic=circuit_id ")
    assert "circuit_id=305419896" in suffix
    assert "circuit_id_text=12:34:56:78" in suffix


def test_reject_non_wire_frame() -> None:
    """MyRvLink command bytes should not be misread as IDS wire frames."""
    frame = bytes.fromhex("0000010200ff")

    parsed = parse_ids_can_wire_frame(frame)

    assert parsed is None


def test_compose_extended_wire_frame_round_trips() -> None:
    """Composed extended frame should parse back to the same semantic fields."""
    payload = bytes.fromhex("0123")
    frame = compose_ids_can_extended_wire_frame(
        message_type=0x82,
        source_address=0x3A,
        target_address=0x75,
        message_data=0x00,
        payload=payload,
    )

    parsed = parse_ids_can_wire_frame(frame)

    assert parsed is not None
    assert parsed.is_extended is True
    assert parsed.message_type == 0x82
    assert parsed.source_address == 0x3A
    assert parsed.target_address == 0x75
    assert parsed.message_data == 0x00
    assert parsed.payload == payload


def test_parse_extended_wire_frame_with_flagged_dlc_byte() -> None:
    """Adapters may set upper DLC nibble flags; parser should use lower nibble length."""
    frame = bytes.fromhex("1880eac1000000000000000000")

    parsed = parse_ids_can_wire_frame(frame)

    assert parsed is not None
    assert parsed.dlc == 8
    assert parsed.is_extended is True
    assert parsed.message_type == 0x82
    assert parsed.source_address == 0x3A
    assert parsed.target_address == 0xC1
    assert parsed.message_data == 0x00
    assert parsed.payload == bytes.fromhex("0000000000000000")
