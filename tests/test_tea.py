"""Tests for TEA encryption — verify against known values from the Android app."""

from custom_components.ha_onecontrol.runtime.ids_can_runtime import (
    IdsCanRuntime,
    _IDS_SESSION_REMOTE_CONTROL_CYPHER,
)
from custom_components.ha_onecontrol.protocol.tea import (
    MASK32,
    STEP1_CIPHER,
    STEP2_CIPHER,
    calculate_step1_key,
    calculate_step2_key,
    tea_decrypt,
    tea_encrypt,
)


class TestTeaEncrypt:
    """Basic round-trip and known-value tests for TEA."""

    def test_encrypt_decrypt_not_trivially_roundtrip(self):
        """OneControl TEA returns only the seed half — so single-value
        encrypt→decrypt is NOT a roundtrip (only the block-mode decrypt
        on 8-byte chunks is reversible).  Verify the values differ."""
        cipher = STEP2_CIPHER
        seed = 0xDEADBEEF
        encrypted = tea_encrypt(cipher, seed)
        assert encrypted != seed  # Not identity
        decrypted = tea_decrypt(cipher, encrypted)
        # In this protocol usage, decrypt(encrypt(x)) != x because
        # the cipher half is modified during encryption but not returned.
        # This is expected and correct for authentication keys.
        assert isinstance(decrypted, int)

    def test_encrypt_deterministic(self):
        """Same inputs must produce the same output."""
        a = tea_encrypt(STEP1_CIPHER, 0x12345678)
        b = tea_encrypt(STEP1_CIPHER, 0x12345678)
        assert a == b

    def test_encrypt_different_ciphers(self):
        """Different cipher constants must produce different outputs."""
        seed = 0xCAFEBABE
        a = tea_encrypt(STEP1_CIPHER, seed)
        b = tea_encrypt(STEP2_CIPHER, seed)
        assert a != b

    def test_encrypt_zero_seed(self):
        """Encryption with zero seed should still produce a non-zero output."""
        result = tea_encrypt(STEP1_CIPHER, 0)
        assert result != 0

    def test_encrypt_stays_32bit(self):
        """Result should always fit in 32 bits."""
        result = tea_encrypt(0xFFFFFFFF, 0xFFFFFFFF)
        assert 0 <= result <= MASK32


class TestStep1Key:
    """Step 1 key calculation — 4 bytes, BIG-ENDIAN."""

    def test_output_length(self):
        challenge = b"\x01\x02\x03\x04"
        key = calculate_step1_key(challenge)
        assert len(key) == 4

    def test_big_endian_format(self):
        """The key is BIG-ENDIAN: MSB first."""
        import struct

        challenge = b"\x00\x00\x00\x01"  # seed = 1 in BE
        key = calculate_step1_key(challenge)
        val = struct.unpack(">I", key)[0]
        assert val == tea_encrypt(STEP1_CIPHER, 1)

    def test_all_zeros_challenge(self):
        """Even all-zeros produces a valid (non-zero) key."""
        # Note: in practice the coordinator skips all-zeros,
        # but the crypto should still work.
        key = calculate_step1_key(b"\x00\x00\x00\x00")
        assert key != b"\x00\x00\x00\x00"

    def test_rejects_wrong_size(self):
        import pytest

        with pytest.raises(ValueError):
            calculate_step1_key(b"\x01\x02")


class TestStep2Key:
    """Step 2 key calculation — 16 bytes, LITTLE-ENDIAN, includes PIN."""

    def test_output_length(self):
        seed = b"\x01\x02\x03\x04"
        key = calculate_step2_key(seed, "090336")
        assert len(key) == 16

    def test_pin_embedded(self):
        """PIN bytes should appear at offset 4-9."""
        seed = b"\x01\x02\x03\x04"
        pin = "090336"
        key = calculate_step2_key(seed, pin)
        assert key[4:10] == pin.encode("ascii")

    def test_zero_padding(self):
        """Bytes 10-15 should be zero."""
        seed = b"\x01\x02\x03\x04"
        key = calculate_step2_key(seed, "090336")
        assert key[10:16] == b"\x00\x00\x00\x00\x00\x00"

    def test_little_endian_encrypted_seed(self):
        """First 4 bytes are TEA(cipher, seed) in LITTLE-ENDIAN."""
        import struct

        seed_bytes = b"\x78\x56\x34\x12"  # LE → seed = 0x12345678
        key = calculate_step2_key(seed_bytes, "090336")
        encrypted = struct.unpack("<I", key[:4])[0]
        expected = tea_encrypt(STEP2_CIPHER, 0x12345678)
        assert encrypted == expected

    def test_different_pins_different_keys(self):
        seed = b"\x01\x02\x03\x04"
        k1 = calculate_step2_key(seed, "090336")
        k2 = calculate_step2_key(seed, "123456")
        assert k1 != k2
        # Encrypted seed portion should be the same
        assert k1[:4] == k2[:4]
        # PIN portions differ
        assert k1[4:10] != k2[4:10]


class TestIdsCanSessionCipher:
    """Regression guard for the IDS-CAN REMOTE_CONTROL session cipher constant.

    The expected output below was computed with the correct constant (0xB16B00B5)
    and differs from the output of the previously wrong constant (0xB16B5E95).
    Any future change to _IDS_SESSION_REMOTE_CONTROL_CYPHER will break this test.
    """

    def test_cipher_constant_matches_decompiled_source(self):
        # SESSION_ID(4, 2976579765u) in IDS.Core.IDS_CAN.Descriptors/SESSION_ID.cs
        assert _IDS_SESSION_REMOTE_CONTROL_CYPHER == 0xB16B00B5, (
            "IDS REMOTE_CONTROL session cipher must match decompiled SESSION_ID.cs "
            "(value=4, cypher=2976579765). Wrong constant causes KEY_NOT_CORRECT on "
            "every SESSION_TRANSMIT_KEY, silently blocking all device commands."
        )

    def test_encrypt_known_vector(self):
        """Output must match the value computed from the correct C# cipher."""
        # Computed from the C# Encrypt() implementation in SESSION_ID.cs with
        # seed=0xDEADBEEF and cypher=0xB16B00B5 (REMOTE_CONTROL session).
        # Old wrong cypher (0xB16B5E95) would produce 0xDC3AFD9F instead.
        result = IdsCanRuntime._ids_encrypt_session_seed(None, 0xDEADBEEF)  # type: ignore[arg-type]
        assert result == 0x2A053086, (
            f"IDS session cipher produced 0x{result:08X}, expected 0x2A053086. "
            "Check _IDS_SESSION_REMOTE_CONTROL_CYPHER — wrong value causes "
            "KEY_NOT_CORRECT (0x0D) on every session auth attempt."
        )
