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
    pdg2pic_direct_type,
    unsupported_pdg_type,
    unwrap_simple_jpeg,
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

    def test_only_confirmed_04h_bypasses_the_open_decoder(self) -> None:
        self.assertEqual(pdg2pic_direct_type(self._hh(0x04)), 0x04)
        for marker in (0x00, 0x02, 0x03, 0x05, 0x11, 0x60, 0x6A, 0xA0, 0xFF):
            self.assertIsNone(pdg2pic_direct_type(self._hh(marker)))

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


class SimpleWrapperTests(unittest.TestCase):
    """4-byte LE length + 1-byte type + raw JPEG must be unwrapped, strictly."""

    def _wrap(self, jpeg: bytes, type_byte: int = 0x02) -> bytes:
        return struct.pack("<I", len(jpeg)) + bytes([type_byte]) + jpeg

    def test_wrapped_jpeg_is_unwrapped(self) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"payload" + b"\xff\xd9"
        self.assertEqual(unwrap_simple_jpeg(self._wrap(jpeg)), jpeg)

    def test_bare_jpeg_is_left_for_the_direct_path(self) -> None:
        self.assertIsNone(unwrap_simple_jpeg(b"\xff\xd8\xff\xe0jpeg"))

    def test_hh_container_is_not_mistaken_for_a_wrapper(self) -> None:
        self.assertIsNone(unwrap_simple_jpeg(b"HH\x02\x4a" + bytes(200)))

    def test_length_field_must_match_exactly(self) -> None:
        jpeg = b"\xff\xd8\xff\xe0body\xff\xd9"
        bad = struct.pack("<I", len(jpeg) + 7) + b"\x02" + jpeg
        self.assertIsNone(unwrap_simple_jpeg(bad))

    def test_five_byte_header_without_a_jpeg_is_ignored(self) -> None:
        blob = struct.pack("<I", 4) + b"\x02" + b"\x00\x01\x02\x03"
        self.assertIsNone(unwrap_simple_jpeg(blob))


if __name__ == "__main__":
    unittest.main(verbosity=2)
