from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

LINUX_ROOT = Path(__file__).resolve().parents[1]
if str(LINUX_ROOT) not in sys.path:
    sys.path.insert(0, str(LINUX_ROOT))

from autobook_linux.pdg_crypto import decrypt_03h_to_00h, normalize_legacy_pdg


class Pdg03Tests(unittest.TestCase):
    def test_known_03h_block_decrypts_to_00h_payload(self) -> None:
        raw = bytearray(0x8C + 16)
        raw[:2] = b"HH"
        raw[0x0F] = 0x03
        struct.pack_into("<HH", raw, 0x10, 1570, 2368)
        struct.pack_into("<II", raw, 0x18, 0x8C, 16)
        raw[0x40:0x70] = bytes.fromhex(
            "0000000000000000000000000000000000000000000000000000000000009a2e"
            "00000000000000000000000000000000"
        )
        raw[0x8C:] = bytes.fromhex("44ad489a2f6a4512574af916dfa9933e")

        decoded = decrypt_03h_to_00h(bytes(raw))

        self.assertEqual(decoded[0x0F], 0x00)
        self.assertEqual(decoded[0x8C:], b"\xff" * 16)

    def test_non_03h_input_is_unchanged(self) -> None:
        raw = b"\xff\xd8jpeg"
        self.assertIs(decrypt_03h_to_00h(raw), raw)

    def test_11h_marker_is_normalized_after_bounds_check(self) -> None:
        raw = bytearray(0x8C + 16)
        raw[:2] = b"HH"
        raw[0x0F] = 0x11
        struct.pack_into("<II", raw, 0x18, 0x8C, 16)
        raw[0x8C:] = b"\xff" * 16

        normalized = normalize_legacy_pdg(bytes(raw))

        self.assertEqual(normalized[0x0F], 0x00)
        self.assertEqual(normalized[0x8C:], b"\xff" * 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
