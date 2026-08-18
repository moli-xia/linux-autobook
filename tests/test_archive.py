from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LINUX_ROOT = Path(__file__).resolve().parents[1]
if str(LINUX_ROOT) not in sys.path:
    sys.path.insert(0, str(LINUX_ROOT))

from autobook_linux.archive import (
    ARCHIVE_SUFFIXES,
    extract_archive,
    find_files_by_suffix,
    looks_like_archive,
)


class ArchiveTests(unittest.TestCase):
    def test_uvz_and_rar_are_supported_archives(self) -> None:
        self.assertIn(".uvz", ARCHIVE_SUFFIXES)
        self.assertIn(".rar", ARCHIVE_SUFFIXES)
        self.assertTrue(looks_like_archive(Path("BOOK.UVZ")))
        self.assertTrue(looks_like_archive(Path("BOOK.RAR")))

    def test_magic_sniffing_accepts_custom_zip_and_rar_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zip_file = root / "book.data"
            rar_file = root / "book.bin"
            zip_file.write_bytes(b"PK\x03\x04" + b"\0" * 20)
            rar_file.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\0" * 20)
            self.assertTrue(looks_like_archive(zip_file))
            self.assertTrue(looks_like_archive(rar_file))

    def test_case_insensitive_content_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "BOOK.PDF").write_bytes(b"pdf")
            (root / "PAGE.PDG").write_bytes(b"pdg")
            self.assertEqual(
                {path.name for path in find_files_by_suffix(root, {".pdf", ".pdg"})},
                {"BOOK.PDF", "PAGE.PDG"},
            )

    @patch("autobook_linux.archive.subprocess.run")
    def test_uvz_is_tested_then_extracted_by_7z(self, run) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "Everything is Ok", ""),
            subprocess.CompletedProcess([], 0, "Everything is Ok", ""),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "book.uvz"
            archive.write_bytes(b"PK\x03\x04")
            password_file = root / "password.txt"
            password_file.write_text("secret\n", encoding="utf-8")
            target = root / "out"
            result = extract_archive(archive, "7z", password_file, target)

        self.assertEqual(result, target)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][:3], ["7z", "t", "-y"])
        self.assertEqual(run.call_args_list[1].args[0][:3], ["7z", "x", "-y"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
