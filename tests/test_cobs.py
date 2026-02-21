"""Tests for COBS encoder/decoder with CRC8."""

from custom_components.onecontrol.protocol.cobs import CobsByteDecoder, cobs_encode
from custom_components.onecontrol.protocol.crc8 import crc8


class TestCobsByteDecoder:
    """Stateful byte-by-byte COBS decoder."""

    def test_empty_frame(self):
        """Two consecutive frame chars → no output (empty payload)."""
        dec = CobsByteDecoder(use_crc=True)
        assert dec.decode_byte(0x00) is None  # start
        assert dec.decode_byte(0x00) is None  # end (empty)

    def test_roundtrip(self):
        """Encode then decode should return original data."""
        original = bytes([0x07, 0x0C, 0x80, 0x1A, 0x00])  # includes a zero byte
        encoded = cobs_encode(original, prepend_start=True, use_crc=True)

        dec = CobsByteDecoder(use_crc=True)
        result = None
        for b in encoded:
            frame = dec.decode_byte(b)
            if frame is not None:
                result = frame
                break

        assert result is not None
        assert result == original

    def test_roundtrip_no_crc(self):
        """Roundtrip without CRC."""
        original = b"\x01\x02\x03"
        encoded = cobs_encode(original, prepend_start=True, use_crc=False)

        dec = CobsByteDecoder(use_crc=False)
        result = None
        for b in encoded:
            frame = dec.decode_byte(b)
            if frame is not None:
                result = frame
                break

        assert result == original

    def test_multiple_frames(self):
        """Decoder should handle multiple consecutive frames."""
        dec = CobsByteDecoder(use_crc=True)
        frames = []

        for payload in [b"\x01\x02", b"\x03\x04", b"\x05\x06"]:
            encoded = cobs_encode(payload, prepend_start=True, use_crc=True)
            for b in encoded:
                frame = dec.decode_byte(b)
                if frame is not None:
                    frames.append(frame)

        assert len(frames) == 3
        assert frames[0] == b"\x01\x02"
        assert frames[1] == b"\x03\x04"
        assert frames[2] == b"\x05\x06"

    def test_reset(self):
        """After reset, partial data should be discarded."""
        dec = CobsByteDecoder(use_crc=True)
        # Feed partial data
        dec.decode_byte(0x03)
        dec.decode_byte(0x01)
        dec.reset()
        # Now a valid frame
        encoded = cobs_encode(b"\xAA", prepend_start=True, use_crc=True)
        result = None
        for b in encoded:
            frame = dec.decode_byte(b)
            if frame is not None:
                result = frame
        assert result == b"\xAA"

    def test_crc_mismatch_returns_none(self):
        """Corrupted CRC should cause the frame to be dropped."""
        original = b"\x01\x02\x03"
        encoded = bytearray(cobs_encode(original, prepend_start=True, use_crc=True))
        # Corrupt a data byte (not the frame delimiters)
        if len(encoded) > 3:
            encoded[2] ^= 0xFF  # flip bits in a data/CRC byte

        dec = CobsByteDecoder(use_crc=True)
        result = None
        for b in encoded:
            frame = dec.decode_byte(b)
            if frame is not None:
                result = frame
        # Should be None since CRC was corrupted
        assert result is None


class TestCobsEncode:
    """COBS encoder."""

    def test_starts_with_frame_char(self):
        encoded = cobs_encode(b"\x01", prepend_start=True)
        assert encoded[0] == 0x00

    def test_ends_with_frame_char(self):
        encoded = cobs_encode(b"\x01", prepend_start=True)
        assert encoded[-1] == 0x00

    def test_no_start_frame(self):
        encoded = cobs_encode(b"\x01", prepend_start=False)
        assert encoded[0] != 0x00
        assert encoded[-1] == 0x00

    def test_empty_data(self):
        encoded = cobs_encode(b"", prepend_start=True)
        # Should be just [0x00, 0x00] (start + end)
        assert encoded == b"\x00\x00"
