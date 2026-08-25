from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

LINUX_ROOT = Path(__file__).resolve().parents[1]
if str(LINUX_ROOT) not in sys.path:
    sys.path.insert(0, str(LINUX_ROOT))

from autobook_linux.pdg_crypto import (
    decrypt_03h_to_00h,
    normalize_legacy_pdg,
    unsupported_pdg_type,
)


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


class UnsupportedTypeTests(unittest.TestCase):
    """A proprietary 'HH' type must be named, not left to a cryptic decode code."""

    def _hh(self, type_byte: int) -> bytes:
        raw = bytearray(0x8C + 16)
        raw[:2] = b"HH"
        raw[0x0F] = type_byte
        struct.pack_into("<HH", raw, 0x10, 1792, 2728)
        struct.pack_into("<II", raw, 0x18, 0x8C, 16)
        return bytes(raw)

    def test_type_04h_is_reported_as_unsupported(self) -> None:
        # The exact variant seen in 尤怡研究文集 (header "HH ... 04").
        self.assertEqual(unsupported_pdg_type(self._hh(0x04)), 0x04)

    def test_other_proprietary_families_are_reported(self) -> None:
        for marker in (0x05, 0x60, 0x6A, 0xA0, 0xFF):
            self.assertEqual(unsupported_pdg_type(self._hh(marker)), marker)

    def test_supported_ccitt_types_pass(self) -> None:
        self.assertIsNone(unsupported_pdg_type(self._hh(0x00)))
        self.assertIsNone(unsupported_pdg_type(self._hh(0x02)))

    def test_standard_images_are_never_flagged(self) -> None:
        self.assertIsNone(unsupported_pdg_type(b"\xff\xd8\xff\xe0jpeg"))
        self.assertIsNone(unsupported_pdg_type(b"\x89PNG\r\n"))

    def test_short_or_non_hh_input_is_ignored(self) -> None:
        self.assertIsNone(unsupported_pdg_type(b"HH"))
        self.assertIsNone(unsupported_pdg_type(b"not a pdg"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
