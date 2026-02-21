"""TEA (Tiny Encryption Algorithm) for OneControl BLE authentication.

Two authentication steps:
  Step 1 — Data Service (UNLOCK_STATUS challenge):
      4-byte key, BIG-ENDIAN, cipher = STEP1_CIPHER, no PIN.
  Step 2 — Auth Service (SEED notification):
      16-byte key, LITTLE-ENDIAN, cipher = STEP2_CIPHER, PIN in bytes 4-9.
"""

from __future__ import annotations

import struct

from ..const import (
    STEP1_CIPHER,
    STEP2_CIPHER,
    TEA_CONSTANT_1,
    TEA_CONSTANT_2,
    TEA_CONSTANT_3,
    TEA_CONSTANT_4,
    TEA_DELTA,
    TEA_ROUNDS,
)

MASK32 = 0xFFFFFFFF


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
    *pin* is the 6-digit PIN string (e.g. "090336").

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
