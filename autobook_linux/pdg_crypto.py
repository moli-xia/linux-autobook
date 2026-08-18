"""Pure-Python compatibility transforms for legacy Chaoxing PDG variants."""
from __future__ import annotations

import hashlib
import struct


def decrypt_03h_to_00h(raw_bytes: bytes) -> bytes:
    """Decrypt legacy 03H monochrome PDG into the 00H container form.

    03H uses the same 16-round, 16-byte-block XTEA-like transform as 02H,
    but XORs the first eight MD5 key bytes with ``SUPERSTA``. The decrypted
    payload is the private CCITT stream handled by the existing 00H decoder.
    """
    if len(raw_bytes) < 0x70 or raw_bytes[:2] != b"HH" or raw_bytes[0x0F] != 0x03:
        return raw_bytes

    data_offset, data_size = struct.unpack_from("<II", raw_bytes, 0x18)
    if data_size <= 0 or data_offset < 0x70 or data_offset + data_size > len(raw_bytes):
        raise ValueError("03H PDG 数据区越界或为空")

    key = bytearray(hashlib.md5(raw_bytes[0x40:0x70]).digest())
    for index, value in enumerate(b"SUPERSTA"):
        key[index] ^= value
    a, b, c, d = struct.unpack("<4I", key)

    payload = bytearray(raw_bytes[data_offset:data_offset + data_size])
    mask = 0xFFFFFFFF
    for position in range(0, len(payload) - 15, 16):
        v0, v1, v2, v3 = struct.unpack_from("<4I", payload, position)
        state = 0xE3779B90
        for _ in range(16):
            v3 = (v3 - ((((v0 << 4) & mask) + c) ^ (state + v0) ^ ((v0 >> 5) + b))) & mask
            v2 = (v2 - ((((v3 << 4) & mask) + a) ^ (state + v3) ^ ((v3 >> 5) + d))) & mask
            v1 = (v1 - ((((v2 << 4) & mask) + c) ^ (state + v2) ^ ((v2 >> 5) + d))) & mask
            tmp = (state + v1) & mask
            state = (state + 0x61C88647) & mask
            v0 = (v0 - ((((v1 << 4) & mask) + a) ^ tmp ^ ((v1 >> 5) + b))) & mask
        struct.pack_into("<4I", payload, position, v0, v1, v2, v3)

    decoded = bytearray(raw_bytes)
    decoded[data_offset:data_offset + data_size] = payload
    decoded[0x0F] = 0x00
    return bytes(decoded)


def normalize_legacy_pdg(raw_bytes: bytes) -> bytes:
    """Normalize legacy variants that contain a decodable 00H CCITT stream."""
    decoded = decrypt_03h_to_00h(raw_bytes)
    if len(decoded) >= 0x70 and decoded[:2] == b"HH" and decoded[0x0F] == 0x11:
        data_offset, data_size = struct.unpack_from("<II", decoded, 0x18)
        if data_size <= 0 or data_offset < 0x70 or data_offset + data_size > len(decoded):
            raise ValueError("11H PDG 数据区越界或为空")
        normalized = bytearray(decoded)
        normalized[0x0F] = 0x00
        return bytes(normalized)
    return decoded
