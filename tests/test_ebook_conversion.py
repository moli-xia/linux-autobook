"""Tests for the e-book branch of the pipeline."""
from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from autobook_linux import pipeline
from autobook_linux.pipeline import EBOOK_SUFFIXES, TaskPipeline, find_ebook_converter


class FakeConfig:
    def __init__(self, root: Path) -> None:
        self.work_root = root / "work"
        self.download_root = root / "dl"
        self.seven_zip_bin = "7z"
        self.password_dict = root / "pw.txt"
        self.download_timeout_seconds = 60
        self.pdg_dpi = 200
        self.baidu_group_gid = "1"
        self.baidu_group_name = "g"
        self.baidu_save_dir = "/inbox"
        self.drive_target_dir = "t"
        self.drive_expire_days = 7


def make_pipeline(root: Path) -> TaskPipeline:
    return TaskPipeline(FakeConfig(root), gateway=object())


class FormatRoutingTests(unittest.TestCase):
    def test_ebook_suffixes_cover_what_the_netdisk_holds(self) -> None:
        for suffix in (".epub", ".mobi", ".azw3"):
            self.assertIn(suffix, EBOOK_SUFFIXES)
        # PDFs and archives keep their own paths.
        self.assertNotIn(".pdf", EBOOK_SUFFIXES)
        self.assertNotIn(".zip", EBOOK_SUFFIXES)


class ConverterDiscoveryTests(unittest.TestCase):
    def test_returns_path_when_present(self) -> None:
        with mock.patch("autobook_linux.pipeline.shutil.which", return_value="/usr/bin/ebook-convert"):
            self.assertEqual(find_ebook_converter(), "/usr/bin/ebook-convert")

    def test_returns_none_when_absent(self) -> None:
        with mock.patch("autobook_linux.pipeline.shutil.which", return_value=None):
            self.assertIsNone(find_ebook_converter())


class ConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.job = self.root / "job"
        self.job.mkdir(parents=True)
        self.source = self.root / "book.epub"
        self.source.write_bytes(b"PK\x03\x04epub")
        self.target = self.job / "book.pdf"
        self.pipeline = make_pipeline(self.root)

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_successful_conversion_returns_the_pdf(self) -> None:
        def fake_run(command, **kwargs):
            Path(command[2]).write_bytes(b"%PDF-1.7 body")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("autobook_linux.pipeline.find_ebook_converter", return_value="ebook-convert"), \
             mock.patch("autobook_linux.pipeline.subprocess.run", side_effect=fake_run):
            produced = self.pipeline._from_ebook(self.source, self.job, self.target)
        self.assertEqual(produced, self.target)
        self.assertTrue(produced.read_bytes().startswith(b"%PDF"))

    def test_missing_converter_delivers_the_original(self) -> None:
        # A readable e-book beats a failed task.
        with mock.patch("autobook_linux.pipeline.find_ebook_converter", return_value=None):
            produced = self.pipeline._from_ebook(self.source, self.job, self.target)
        self.assertEqual(produced.suffix, ".epub")
        self.assertEqual(produced.read_bytes(), self.source.read_bytes())

    def test_failed_conversion_falls_back_to_the_original(self) -> None:
        def failing_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "boom")

        with mock.patch("autobook_linux.pipeline.find_ebook_converter", return_value="ebook-convert"), \
             mock.patch("autobook_linux.pipeline.subprocess.run", side_effect=failing_run):
            produced = self.pipeline._from_ebook(self.source, self.job, self.target)
        self.assertEqual(produced.suffix, ".epub")
        self.assertFalse(self.target.exists())

    def test_empty_output_counts_as_failure(self) -> None:
        def empty_run(command, **kwargs):
            Path(command[2]).write_bytes(b"")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("autobook_linux.pipeline.find_ebook_converter", return_value="ebook-convert"), \
             mock.patch("autobook_linux.pipeline.subprocess.run", side_effect=empty_run):
            produced = self.pipeline._from_ebook(self.source, self.job, self.target)
        self.assertEqual(produced.suffix, ".epub")

    def test_headless_chromium_flags_are_passed(self) -> None:
        captured = {}

        def capture_run(command, **kwargs):
            captured.update(kwargs.get("env") or {})
            Path(command[2]).write_bytes(b"%PDF-1.7")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("autobook_linux.pipeline.find_ebook_converter", return_value="ebook-convert"), \
             mock.patch("autobook_linux.pipeline.subprocess.run", side_effect=capture_run):
            self.pipeline._from_ebook(self.source, self.job, self.target)

        # Without --no-sandbox calibre's Chromium refuses to start as root.
        self.assertIn("--no-sandbox", captured.get("QTWEBENGINE_CHROMIUM_FLAGS", ""))
        self.assertEqual(captured.get("QT_QPA_PLATFORM"), "offscreen")
        # It also needs a writable HOME for its config directory.
        self.assertTrue(captured.get("HOME", "").startswith(str(self.job)))
        self.assertTrue(Path(captured["HOME"]).is_dir())


class SerialisationTests(unittest.TestCase):
    def test_conversions_do_not_run_concurrently_by_default(self) -> None:
        # Each conversion spawns a Chromium; two at once exhaust a small worker.
        self.assertEqual(pipeline.EBOOK_CONVERT_SLOTS, 1)

        folder = tempfile.TemporaryDirectory()
        root = Path(folder.name)
        overlap = []
        running = threading.Semaphore(1)

        def slow_run(command, **kwargs):
            acquired = running.acquire(blocking=False)
            overlap.append(acquired)
            threading.Event().wait(0.2)
            if acquired:
                running.release()
            Path(command[2]).write_bytes(b"%PDF-1.7")
            return subprocess.CompletedProcess(command, 0, "", "")

        def convert(index: int) -> None:
            job = root / f"job{index}"
            job.mkdir(parents=True)
            source = root / f"b{index}.epub"
            source.write_bytes(b"PK\x03\x04")
            make_pipeline(root)._from_ebook(source, job, job / "out.pdf")

        with mock.patch("autobook_linux.pipeline.find_ebook_converter", return_value="ebook-convert"), \
             mock.patch("autobook_linux.pipeline.subprocess.run", side_effect=slow_run):
            threads = [threading.Thread(target=convert, args=(i,)) for i in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertTrue(all(overlap), "两个转换同时运行，串行化未生效")
        folder.cleanup()


if __name__ == "__main__":
    unittest.main()
