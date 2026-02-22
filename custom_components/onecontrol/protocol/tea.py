"""TEA (Tiny Encryption Algorithm) for OneControl BLE authentication.

Two authentication steps:
  Step 1 — Data Service (UNLOCK_STATUS challenge):
      4-byte key, BIG-ENDIAN, no PIN.
  Step 2 — Auth Service (SEED notification):
      16-byte key, LITTLE-ENDIAN, includes PIN.

Proprietary key-schedule and cipher constants are obfuscated to comply with
DMCA requirements.  They are derived at runtime from a masked blob and are
not stored in plaintext anywhere in the source tree.
"""

from __future__ import annotations

import struct
from base64 import b64decode as _b64d

from ..const import TEA_DELTA, TEA_ROUNDS

MASK32 = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# Obfuscated protocol constants — derived at module load time.
# Each 4-byte big-endian word in _B is XOR'd with _M to recover the value.
# Order: C1, C2, C3, C4, STEP1_CIPHER, STEP2_CIPHER
# ---------------------------------------------------------------------------
_M = 0xC7D2E1F0
_B = _b64d(b"hL2RibW7hpiz8qi0lKGPk+NW0CVG0un9")


def _u(o: int) -> int:
    return struct.unpack_from(">I", _B, o)[0] ^ _M


TEA_CONSTANT_1 = _u(0)
TEA_CONSTANT_2 = _u(4)
TEA_CONSTANT_3 = _u(8)
TEA_CONSTANT_4 = _u(12)
STEP1_CIPHER = _u(16)
STEP2_CIPHER = _u(20)

del _u, _M, _B, _b64d  # Clean up namespace


def tea_encrypt(cipher: int, seed: int) -> int:
    """Run 32-round TEA encrypt.  All values are unsigned 32-bit."""
    c = cipher & MASK32
    s = seed & MASK32
    delta = TEA_DELTA

    for _ in range(TEA_ROUNDS):
        s = (s + (((c << 4) + TEA_CONSTANT_1) ^ (c + delta) ^ ((c >> 5) + TEA_CONSTANT_2))) & MASK32
        c = (c + (((s << 4) + TEA_CONSTANT_3) ^ (s + delta) ^ ((s >> 5) + TEA_CONSTANT_4))) & MASK32
        delta = (delta + TEA_DELTA) & MASK32

    return s


def tea_decrypt(cipher: int, encrypted: int) -> int:
    """Run 32-round TEA decrypt."""
    c = cipher & MASK32
    s = encrypted & MASK32
    delta = (TEA_DELTA * TEA_ROUNDS) & MASK32

    for _ in range(TEA_ROUNDS):
        c = (c - (((s << 4) + TEA_CONSTANT_3) ^ (s + delta) ^ ((s >> 5) + TEA_CONSTANT_4))) & MASK32
        s = (s - (((c << 4) + TEA_CONSTANT_1) ^ (c + delta) ^ ((c >> 5) + TEA_CONSTANT_2))) & MASK32
        delta = (delta - TEA_DELTA) & MASK32

    return s


# ── Step 1 ────────────────────────────────────────────────────────────────


def calculate_step1_key(challenge_bytes: bytes) -> bytes:
    """Compute the 4-byte BIG-ENDIAN key for Step 1 (Data Service auth).

    *challenge_bytes* is the raw 4- byte value read from UNLOCK_STATUS.
    """
    if len(challenge_bytes) != 4:
        raise ValueError(f"Step 1 challenge must be 4 bytes, got {len(challenge_bytes)}")

    seed = struct.unpack(">I", challenge_bytes)[0]  # BIG-ENDIAN
    encrypted = tea_encrypt(STEP1_CIPHER, seed)
    return struct.pack(">I", encrypted & MASK32)  # BIG-ENDIAN result


# ── Step 2 ────────────────────────────────────────────────────────────────


def calculate_step2_key(seed_bytes: bytes, pin: str) -> bytes:
    """Compute the 16-byte key for Step 2 (Auth Service auth).

    *seed_bytes* is the 4-byte SEED notification from the gateway.
    *pin* is the 6-digit PIN string from the gateway sticker.

    Returns 16 bytes:
      [0:4]  — TEA-encrypted seed (LITTLE-ENDIAN)
      [4:10] — PIN as ASCII bytes
      [10:16] — zero padding
    """
    if len(seed_bytes) != 4:
        raise ValueError(f"Step 2 seed must be 4 bytes, got {len(seed_bytes)}")

    seed = struct.unpack("<I", seed_bytes)[0]  # LITTLE-ENDIAN
    encrypted = tea_encrypt(STEP2_CIPHER, seed)

    key = bytearray(16)
    struct.pack_into("<I", key, 0, encrypted & MASK32)  # LITTLE-ENDIAN result

    pin_bytes = pin.encode("ascii")[:6]
    key[4 : 4 + len(pin_bytes)] = pin_bytes

    return bytes(key)
