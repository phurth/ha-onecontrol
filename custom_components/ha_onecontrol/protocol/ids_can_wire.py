"""IDS-CAN wire-frame parsing helpers.

These helpers decode the raw CAN adapter frame format seen on IDS-CAN
TCP bridges (per decompiled `CanAdapter.OnPhysicalNetworkReceived`).

Frame layout:
- Byte 0: payload length (DLC, 0..8)
- Bytes 1..N: CAN ID (2 bytes for 11-bit, 4 bytes for 29-bit with ext flag)
- Remaining: payload bytes (DLC count)
"""

from __future__ import annotations

from dataclasses import dataclass

from .function_names import FUNCTION_NAMES


_IDS_CAN_MESSAGE_TYPE_NAMES: dict[int, str] = {
    0x00: "NETWORK",
    0x01: "CIRCUIT_ID",
    0x02: "DEVICE_ID",
    0x03: "DEVICE_STATUS",
    0x06: "PRODUCT_STATUS",
    0x07: "TIME",
    0x80: "REQUEST",
    0x81: "RESPONSE",
    0x82: "COMMAND",
    0x83: "EXT_STATUS",
    0x84: "TEXT_CONSOLE",
    0x85: "GROUP_ID",
    0x9B: "DAQ",
    0x9D: "IOT",
    0x9F: "BULK_XFER",
}


@dataclass(frozen=True)
class IdsCanWireFrame:
    """Decoded IDS-CAN wire frame."""

    dlc: int
    is_extended: bool
    can_id: int
    message_type: int
    source_address: int
    target_address: int | None
    message_data: int | None
    payload: bytes


@dataclass(frozen=True)
class IdsCanDecodedPayload:
    """Semantic decode of IDS-CAN payload bytes for known message types."""

    kind: str
    fields: dict[str, int | str | bool]


def ids_can_message_type_name(message_type: int) -> str:
    """Return IDS-CAN message type name from decompiled enum values."""
    return _IDS_CAN_MESSAGE_TYPE_NAMES.get(message_type & 0xFF, "UNKNOWN")


def ids_can_request_name(request_code: int) -> str:
    """Return IDS-CAN REQUEST code name from decompiled REQUEST constants."""
    return {
        0x00: "PART_NUMBER_READ",
        0x01: "MUTE_DEVICE",
        0x02: "IN_MOTION_LOCKOUT",
        0x03: "SOFTWARE_UPDATE_AUTHORIZATION",
        0x04: "NOTIFICATION_ALERT",
        0x10: "PID_READ_LIST",
        0x11: "PID_READ_WRITE",
        0x12: "GET_PID_PROPERTIES",
        0x20: "BLOCK_READ_LIST",
        0x21: "BLOCK_READ_PROPERTIES",
        0x22: "BLOCK_READ_DATA",
        0x23: "BEGIN_BLOCK_WRITE",
        0x24: "BEGIN_BLOCK_WRITE_BULK_XFER",
        0x25: "BLOCK_END_BULK_XFER",
        0x26: "END_BLOCK_WRITE",
        0x27: "SET_BLOCK_ADDRESS",
        0x28: "SET_BLOCK_SIZE",
        0x30: "READ_CONTINUOUS_DTCS",
        0x31: "CONTINUOUS_DTC_COMMAND",
        0x40: "SESSION_READ_LIST",
        0x41: "SESSION_READ_STATUS",
        0x42: "SESSION_REQUEST_SEED",
        0x43: "SESSION_TRANSMIT_KEY",
        0x44: "SESSION_HEARTBEAT",
        0x45: "SESSION_END",
        0x51: "IDS_CAN_REQUEST_DAQ_NUM_CHANNELS",
        0x52: "IDS_CAN_REQUEST_DAQ_AUTO_TX_SETTINGS",
        0x53: "IDS_CAN_REQUEST_DAQ_CHANNEL_SETTINGS",
        0x54: "IDS_CAN_REQUEST_DAQ_PID_ADDRESS",
        0x60: "IDS_CAN_REQUEST_LEVELER_TYPE_5_CONTROL",
    }.get(request_code & 0xFF, f"UNKNOWN_{request_code & 0xFF:02X}")


def ids_can_response_name(response_code: int) -> str:
    """Return IDS-CAN RESPONSE enum name from decompiled RESPONSE values."""
    return {
        0x00: "SUCCESS",
        0x01: "REQUEST_NOT_SUPPORTED",
        0x02: "BAD_REQUEST",
        0x03: "VALUE_OUT_OF_RANGE",
        0x04: "UNKNOWN_ID",
        0x05: "VALUE_TOO_LARGE",
        0x06: "INVALID_ADDRESS",
        0x07: "READ_ONLY",
        0x08: "WRITE_ONLY",
        0x09: "CONDITIONS_NOT_CORRECT",
        0x0A: "FEATURE_NOT_SUPPORTED",
        0x0B: "BUSY",
        0x0C: "SEED_NOT_REQUESTED",
        0x0D: "KEY_NOT_CORRECT",
        0x0E: "SESSION_NOT_OPEN",
        0x0F: "TIMEOUT",
        0x10: "REMOTE_REQUEST_NOT_SUPPORTED",
        0x11: "IN_MOTION_LOCKOUT_ACTIVE",
        0x12: "CRC_INVALID",
        0x13: "CANCELLED",
        0x14: "ABORTED",
        0x15: "FAILED",
        0x16: "IN_PROGRESS",
    }.get(response_code & 0xFF, f"UNKNOWN_{response_code & 0xFF:02X}")


def decode_ids_can_payload(wire: IdsCanWireFrame) -> IdsCanDecodedPayload | None:
    """Decode known IDS-CAN message payload formats with decompiled parity."""
    message_type = wire.message_type & 0xFF
    payload = wire.payload

    if message_type == 0x00 and len(payload) == 8:
        # C# parity: MAC is bytes [2:8], protocol version is byte [1], and
        # NETWORK_STATUS bitfields are interpreted from byte [0].
        status = payload[0] & 0xFF
        return IdsCanDecodedPayload(
            kind="network",
            fields={
                "advertised_address": status,
                "protocol_version": payload[1] & 0xFF,
                "mac": payload[2:8].hex().upper(),
                "has_active_dtcs": bool(status & 0x01),
                "has_stored_dtcs": bool(status & 0x02),
                "has_open_sessions": bool(status & 0x04),
                "in_motion_lockout_level": (status >> 3) & 0x03,
                "has_extended_cloud_capabilities": bool(status & 0x40),
                "is_hazardous_device": bool(status & 0x80),
            },
        )

    if message_type == 0x01 and len(payload) >= 4:
        c1, c2, c3, c4 = payload[0], payload[1], payload[2], payload[3]
        return IdsCanDecodedPayload(
            kind="circuit_id",
            fields={
                "circuit_id": int.from_bytes(payload[0:4], "big"),
                "circuit_id_text": f"{c1:02X}:{c2:02X}:{c3:02X}:{c4:02X}",
            },
        )

    if message_type == 0x02 and len(payload) in (7, 8):
        function_name = int.from_bytes(payload[4:6], "big")
        function_instance = payload[6] & 0x0F
        fields: dict[str, int | str | bool] = {
            "product_id": int.from_bytes(payload[0:2], "big"),
            "product_instance": payload[2] & 0xFF,
            "device_type": payload[3] & 0xFF,
            "device_instance": (payload[6] >> 4) & 0x0F,
            "function_name": function_name,
            "function_instance": function_instance,
            "function_label": FUNCTION_NAMES.get(function_name, f"Function 0x{function_name:04X}"),
        }
        if len(payload) >= 8:
            fields["device_capabilities"] = payload[7] & 0xFF
        return IdsCanDecodedPayload(kind="device_id", fields=fields)

    if message_type == 0x03 and len(payload) >= 1:
        return IdsCanDecodedPayload(
            kind="device_status",
            fields={
                "status_length": len(payload),
                "status_hex": payload.hex(),
            },
        )

    if message_type == 0x06 and len(payload) >= 1:
        return IdsCanDecodedPayload(
            kind="product_status",
            fields={
                "software_update_state": payload[0] & 0x03,
            },
        )

    if message_type == 0x80 and wire.message_data is not None:
        return IdsCanDecodedPayload(
            kind="request",
            fields={
                "request_code": wire.message_data & 0xFF,
                "request_name": ids_can_request_name(wire.message_data),
            },
        )

    if message_type == 0x81 and wire.message_data is not None:
        # RESPONSE frame message_data is the echoed request code.
        # Any status/response code (if present) lives in payload bytes and varies by request.
        fields: dict[str, int | str | bool] = {
            "request_code": wire.message_data & 0xFF,
            "request_name": ids_can_request_name(wire.message_data),
        }
        if len(payload) == 1:
            fields["status_code"] = payload[0] & 0xFF
            fields["status_name"] = ids_can_response_name(payload[0])
        return IdsCanDecodedPayload(kind="response", fields=fields)

    if message_type == 0x82 and wire.message_data is not None:
        return IdsCanDecodedPayload(
            kind="command",
            fields={
                "command_code": wire.message_data & 0xFF,
            },
        )

    if message_type == 0x84:
        return IdsCanDecodedPayload(
            kind="text_console",
            fields={
                "text_ascii": "".join(chr(b) if 32 <= b <= 126 else "." for b in payload),
            },
        )

    return None


def format_ids_can_payload(decoded: IdsCanDecodedPayload | None) -> str:
    """Format decoded payload fields as compact key=value pairs for logging."""
    if decoded is None:
        return ""

    ordered_items: list[tuple[str, int | str | bool]] = []
    for key in sorted(decoded.fields.keys()):
        ordered_items.append((key, decoded.fields[key]))
    joined = " ".join(f"{k}={v}" for k, v in ordered_items)
    return f" semantic={decoded.kind} {joined}".rstrip()


def parse_ids_can_wire_frame(frame: bytes) -> IdsCanWireFrame | None:
    """Parse one raw IDS-CAN wire frame.

    Returns None when bytes do not match the CAN adapter framing format.
    """
    if len(frame) < 3:
        return None

    dlc_raw = frame[0] & 0xFF
    dlc = dlc_raw
    if dlc > 8:
        # Some IDS bridge adapters include flags in the upper nibble of the DLC byte.
        # Treat lower nibble as the payload length when it is in a valid CAN range.
        flagged_dlc = dlc_raw & 0x0F
        if flagged_dlc <= 8 and (dlc_raw & 0xF0) != 0:
            dlc = flagged_dlc
        else:
            return None

    # remaining = id bytes + payload bytes
    remaining = len(frame) - 1
    id_len = remaining - dlc
    if id_len not in (2, 4):
        return None

    id_bytes = frame[1 : 1 + id_len]
    payload = frame[1 + id_len :]
    if len(payload) != dlc:
        return None

    if id_len == 2:
        can_id = ((id_bytes[0] & 0xFF) << 8) | (id_bytes[1] & 0xFF)
        message_type = (can_id >> 8) & 0x07
        source_address = can_id & 0xFF
        return IdsCanWireFrame(
            dlc=dlc,
            is_extended=False,
            can_id=can_id,
            message_type=message_type,
            source_address=source_address,
            target_address=None,
            message_data=None,
            payload=payload,
        )

    id_word = (
        ((id_bytes[0] & 0xFF) << 24)
        | ((id_bytes[1] & 0xFF) << 16)
        | ((id_bytes[2] & 0xFF) << 8)
        | (id_bytes[3] & 0xFF)
    )
    is_extended = (id_word & 0x80000000) != 0
    if not is_extended:
        return None

    can_id = id_word & 0x7FFFFFFF
    message_type = 0x80 | ((can_id >> 24) & 0x1C) | ((can_id >> 16) & 0x03)
    source_address = (can_id >> 18) & 0xFF
    target_address = (can_id >> 8) & 0xFF
    message_data = can_id & 0xFF

    return IdsCanWireFrame(
        dlc=dlc,
        is_extended=True,
        can_id=can_id,
        message_type=message_type,
        source_address=source_address,
        target_address=target_address,
        message_data=message_data,
        payload=payload,
    )


def compose_ids_can_extended_wire_frame(
    message_type: int,
    source_address: int,
    target_address: int,
    message_data: int,
    payload: bytes,
) -> bytes:
    """Compose a raw IDS 29-bit wire frame: [dlc][id(4)][payload].

    Mirrors the inverse of ``parse_ids_can_wire_frame`` for extended frames.
    """
    dlc = len(payload)
    if dlc > 8:
        raise ValueError("IDS-CAN payload must be 0..8 bytes")

    msg = message_type & 0xFF
    src = source_address & 0xFF
    dst = target_address & 0xFF
    mdata = message_data & 0xFF

    # 29-bit CAN id packing (decompiled parity):
    #   message_type = 0x80 | ((can_id >> 24) & 0x1C) | ((can_id >> 16) & 0x03)
    # Inverse mapping keeps upper 3 type bits at [28:26] and lower 2 bits at [17:16].
    can_id = ((msg & 0x1C) << 24) | (src << 18) | ((msg & 0x03) << 16) | (dst << 8) | mdata
    id_word = 0x80000000 | (can_id & 0x7FFFFFFF)
    return bytes([dlc]) + id_word.to_bytes(4, "big") + payload
