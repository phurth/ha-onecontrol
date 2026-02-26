"""COBS (Consistent Overhead Byte Stuffing) codec for OneControl BLE frames.

The gateway uses COBS with CRC8 on the DATA_READ / DATA_WRITE characteristics.
Frame structure on the wire: [0x00] <cobs-encoded payload + crc8> [0x00]

This module provides:
  - ``CobsByteDecoder``  — stateful byte-by-byte decoder (for BLE notifications)
  - ``cobs_encode``      — one-shot encoder (for building commands)
"""

from __future__ import annotations

from .crc8 import crc8

FRAME_CHAR = 0x00
MAX_DATA_BYTES = 63        # 2^6 - 1
FRAME_BYTE_COUNT_LSB = 64  # 2^6
MAX_COMPRESSED_FRAME_BYTES = 192  # 255 - 63
MAX_BUFFER = 382


class CobsByteDecoder:
    """Stateful COBS byte-by-byte decoder with CRC8 verification.

    Feed each byte from a BLE notification through ``decode_byte()``.
    When a complete frame is received it returns the decoded payload;
    otherwise it returns ``None``.
    """

    def __init__(self, use_crc: bool = True) -> None:
        self._use_crc = use_crc
        self._min_payload = 1 if use_crc else 0
        self._buf = bytearray(MAX_BUFFER)
        self._dst = 0
        self._code = 0

    def reset(self) -> None:
        self._dst = 0
        self._code = 0

    def decode_byte(self, b: int) -> bytes | None:
        """Process a single byte.  Returns decoded frame or None."""
        b &= 0xFF

        if b == FRAME_CHAR:
            # Frame terminator
            if self._code != 0:
                self.reset()
                return None
            if self._dst <= self._min_payload:
                self.reset()
                return None

            if self._use_crc:
                received_crc = self._buf[self._dst - 1]
                self._dst -= 1
                calculated = crc8(bytes(self._buf[: self._dst]))
                if calculated != received_crc:
                    self.reset()
                    return None

            result = bytes(self._buf[: self._dst])
            self.reset()
            return result

        if self._code <= 0:
            # Start of a new code block
            self._code = b
        else:
            self._code -= 1
            if self._dst < MAX_BUFFER:
                self._buf[self._dst] = b
                self._dst += 1

        # Insert implicit zeros when code block consumed
        if (self._code & MAX_DATA_BYTES) == 0:
            while self._code > 0:
                if self._dst < MAX_BUFFER:
                    self._buf[self._dst] = FRAME_CHAR
                    self._dst += 1
                self._code -= FRAME_BYTE_COUNT_LSB

        return None


def cobs_encode(data: bytes, prepend_start: bool = True, use_crc: bool = True) -> bytes:
    """COBS-encode *data* with optional start-frame byte and CRC8 suffix.

    Returns the wire-ready byte string including frame delimiters.
    """
    out = bytearray(MAX_BUFFER)
    idx = 0

    if prepend_start:
        out[idx] = FRAME_CHAR
        idx += 1

    if not data:
        out[idx] = FRAME_CHAR
        idx += 1
        return bytes(out[:idx])

    src_len = len(data)
    total = src_len + (1 if use_crc else 0)
    crc_val = 0x55  # CRC8 init
    src_idx = 0

    while src_idx < total:
        code_idx = idx
        code = 0
        out[idx] = 0xFF  # placeholder
        idx += 1

        # Non-zero data bytes
        while src_idx < total:
            if src_idx < src_len:
                bval = data[src_idx]
                if bval == FRAME_CHAR:
                    break
                crc_val = _crc_update(crc_val, bval)
            else:
                # CRC byte position
                bval = crc_val & 0xFF
                if bval == FRAME_CHAR:
                    break

            src_idx += 1
            out[idx] = bval
            idx += 1
            code += 1

            if code >= MAX_DATA_BYTES:
                break

        # Handle consecutive zeros (compressed)
        while src_idx < total:
            bval = data[src_idx] if src_idx < src_len else (crc_val & 0xFF)
            if bval != FRAME_CHAR:
                break
            if src_idx < src_len:
                crc_val = _crc_update(crc_val, FRAME_CHAR)
            src_idx += 1
            code += FRAME_BYTE_COUNT_LSB
            if code >= MAX_COMPRESSED_FRAME_BYTES:
                break

        out[code_idx] = code & 0xFF

    out[idx] = FRAME_CHAR
    idx += 1
    return bytes(out[:idx])


# ── CRC helper ────────────────────────────────────────────────────────────

# Import table from crc8 module for one-byte update
from .crc8 import _TABLE as _CRC_TABLE  # noqa: E402


def _crc_update(crc: int, b: int) -> int:
    return _CRC_TABLE[(crc ^ b) & 0xFF]
