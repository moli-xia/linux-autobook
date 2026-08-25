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
    UNRAR_BAD_PASSWORD,
    extract_archive,
    find_files_by_suffix,
    is_rar,
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


RAR5_MAGIC = bytes.fromhex("52617221" "1a070100")  # Rar! 1A 07 01 00
RAR4_MAGIC = bytes.fromhex("52617221" "1a0700")     # Rar! 1A 07 00
ZIP_MAGIC = bytes.fromhex("504b0304")               # PK 03 04
PW_LINES = "nope1\nbeirenwuze\nnope2\n"


def _cp(rc, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class RarRoutingTests(unittest.TestCase):
    """RAR must go through unrar, which the bundled 7z cannot replace for RAR5."""

    def _rar(self, root):
        archive = root / "book.rar"
        archive.write_bytes(RAR5_MAGIC + bytes(20))
        (root / "password.txt").write_text(PW_LINES, encoding="utf-8")
        return archive

    def test_is_rar_detects_rar4_and_rar5(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").write_bytes(RAR5_MAGIC + b"xx")
            (root / "b").write_bytes(RAR4_MAGIC + b"xx")
            (root / "c").write_bytes(ZIP_MAGIC + b"xx")
            self.assertTrue(is_rar(root / "a"))
            self.assertTrue(is_rar(root / "b"))
            self.assertFalse(is_rar(root / "c"))

    @patch("autobook_linux.archive._unrar_test_member", return_value=("book/big.pdg", True))
    @patch("autobook_linux.archive.find_unrar", return_value="/usr/bin/unrar")
    @patch("autobook_linux.archive.subprocess.run")
    def test_correct_password_is_found_and_used(self, run, _fu, _tm):
        # empty candidate skipped; nope1 wrong; beirenwuze right -> extract.
        run.side_effect = [_cp(UNRAR_BAD_PASSWORD), _cp(0), _cp(0, "All OK")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._rar(root)
            target = root / "out"
            result = extract_archive(archive, "7z", root / "password.txt", target)
        self.assertEqual(result, target)
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all(c.args[0][0] == "/usr/bin/unrar" for c in run.call_args_list))
        self.assertEqual(run.call_args_list[0].args[0][:2], ["/usr/bin/unrar", "t"])
        self.assertEqual(run.call_args_list[2].args[0][1], "x")
        self.assertIn("-pbeirenwuze", run.call_args_list[2].args[0])

    @patch("autobook_linux.archive._unrar_test_member", return_value=("book/big.pdg", True))
    @patch("autobook_linux.archive.find_unrar", return_value="/usr/bin/unrar")
    @patch("autobook_linux.archive.subprocess.run")
    def test_password_tests_target_a_single_member(self, run, _fu, _tm):
        run.side_effect = [_cp(0), _cp(0, "All OK")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._rar(root)
            extract_archive(archive, "7z", root / "password.txt", root / "out")
        # one named member -> one KDF per guess, not one per file.
        self.assertEqual(run.call_args_list[0].args[0][-1], "book/big.pdg")

    @patch("autobook_linux.archive._unrar_test_member", return_value=(None, False))
    @patch("autobook_linux.archive.find_unrar", return_value="/usr/bin/unrar")
    @patch("autobook_linux.archive.subprocess.run")
    def test_unencrypted_rar_extracts_without_a_password(self, run, _fu, _tm):
        run.side_effect = [_cp(0, "All OK")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._rar(root)
            extract_archive(archive, "7z", root / "password.txt", root / "out")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args_list[0].args[0][1], "x")
        self.assertIn("-p-", run.call_args_list[0].args[0])

    @patch("autobook_linux.archive._unrar_test_member", return_value=("book/big.pdg", True))
    @patch("autobook_linux.archive.find_unrar", return_value="/usr/bin/unrar")
    @patch("autobook_linux.archive.subprocess.run")
    def test_no_working_password_reports_clearly(self, run, _fu, _tm):
        # three real passwords in the dictionary, all rejected.
        run.side_effect = [_cp(UNRAR_BAD_PASSWORD)] * 3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._rar(root)
            with self.assertRaises(RuntimeError) as caught:
                extract_archive(archive, "7z", root / "password.txt", root / "out")
        self.assertIn("密码字典未找到可用密码", str(caught.exception))

    @patch("autobook_linux.archive.find_unrar", return_value=None)
    @patch("autobook_linux.archive.subprocess.run")
    def test_rar_falls_back_to_7z_when_unrar_missing(self, run, _fu):
        run.side_effect = [_cp(0), _cp(0, "Everything is Ok")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._rar(root)
            extract_archive(archive, "7z", root / "password.txt", root / "out")
        self.assertEqual(run.call_args_list[0].args[0][0], "7z")


class SevenZipCodecTests(unittest.TestCase):
    @patch("autobook_linux.archive.find_unrar", return_value=None)
    @patch("autobook_linux.archive.subprocess.run")
    def test_unsupported_method_is_not_a_bad_password(self, run, _fu):
        run.side_effect = [_cp(2, "ERROR: Unsupported Method : book/000001.pdg")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "book.uvz"
            archive.write_bytes(ZIP_MAGIC)
            (root / "password.txt").write_text("secret\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as caught:
                extract_archive(archive, "7z", root / "password.txt", root / "out")
        message = str(caught.exception)
        self.assertIn("不支持该压缩格式", message)
        self.assertNotIn("密码字典未找到可用密码", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
