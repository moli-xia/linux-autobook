from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pikepdf

LINUX_ROOT = Path(__file__).resolve().parents[1]
if str(LINUX_ROOT) not in sys.path:
    sys.path.insert(0, str(LINUX_ROOT))

from autobook_linux.gateway_client import BaiduGatewayClient
from autobook_linux.gateway_server import make_handler
from autobook_linux.pdg_fallback import (
    PdgFallbackError,
    PdgFallbackResult,
    PdgFallbackService,
    _pdg_page_count,
    _safe_extract_zip,
)
from autobook_linux.pipeline import PipelineError, TaskPipeline


class _IdleManager:
    def stats(self):
        return {"pending": 0, "running": 0, "ready": 0, "failed": 0}


class _FakeFallback:
    enabled = True

    def __init__(self, root: Path) -> None:
        self.root = root
        self.received_names: list[str] = []
        self.cleaned = False

    def status(self):
        return {"enabled": True, "docker_socket": True, "image": "test-image"}

    def convert(self, stream, length, expected_sha256=""):
        payload = stream.read(length)
        self.assert_digest(payload, expected_sha256)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.received_names = archive.namelist()
        job = self.root / "job"
        job.mkdir()
        output = job / "output.pdf"
        with pikepdf.new() as document:
            document.add_blank_page(page_size=(100, 100))
            document.save(output)
        data = output.read_bytes()
        return PdgFallbackResult(job, output, len(data), hashlib.sha256(data).hexdigest(), 1)

    @staticmethod
    def assert_digest(payload: bytes, expected: str) -> None:
        if hashlib.sha256(payload).hexdigest() != expected:
            raise AssertionError("upload digest mismatch")

    def cleanup(self, result):
        self.cleaned = True


def _write_pdf(path: Path, pages: int = 1) -> None:
    with pikepdf.new() as document:
        for _ in range(pages):
            document.add_blank_page(page_size=(100, 100))
        document.save(path)


class _SuccessfulFallbackService(PdgFallbackService):
    def _run_container(self, job_dir: Path, input_dir: Path, output: Path) -> None:
        _write_pdf(output, _pdg_page_count(input_dir))


class _PipelineConfig:
    def __init__(self, root: Path) -> None:
        self.seven_zip_bin = "7z"
        self.password_dict = root / "passwords.txt"
        self.download_timeout_seconds = 60
        self.pdg_dpi = 200


class _PipelineGateway:
    def __init__(self, *, error: str = "") -> None:
        self.error = error
        self.calls: list[Path] = []

    def convert_pdg_fallback(self, source_dir: Path, target_pdf: Path) -> Path:
        self.calls.append(source_dir)
        if self.error:
            raise RuntimeError(self.error)
        _write_pdf(target_pdf, _pdg_page_count(source_dir))
        return target_pdf


class PdgFallbackTests(unittest.TestCase):
    def test_safe_extract_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape.pdg", b"HH")
            with self.assertRaisesRegex(PdgFallbackError, "不安全路径"):
                _safe_extract_zip(archive, root / "input", 1024)
            self.assertFalse((root.parent / "escape.pdg").exists())

    def test_gateway_accepts_any_pdg_variant_for_conversion_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ordinary.pdg").write_bytes(b"HH" + b"\0" * 14)
            (root / "unknown.PDG").write_bytes(b"HH" + b"\0" * 13 + b"\x05")
            self.assertEqual(_pdg_page_count(root), 2)

    def test_service_runs_for_non_04h_pdg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            socket_path = root / "docker.sock"
            socket_path.touch()
            config = SimpleNamespace(
                pdg_fallback_enabled=True,
                pdg_fallback_job_root=root / "jobs",
                pdg_fallback_docker_socket=socket_path,
                pdg_fallback_image="test-image",
                pdg_fallback_runtime_volume="",
                pdg_fallback_memory_mb=2048,
                pdg_fallback_cpus=2,
                pdg_fallback_timeout_seconds=7200,
                pdg_fallback_max_upload_mb=16,
            )
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr("pages/000001.PDG", b"HH" + b"\0" * 13 + b"\x05")
            upload = payload.getvalue()
            result = _SuccessfulFallbackService(config).convert(
                io.BytesIO(upload),
                len(upload),
                hashlib.sha256(upload).hexdigest(),
            )
            try:
                self.assertEqual(result.pages, 1)
                self.assertTrue(result.pdf.is_file())
            finally:
                PdgFallbackService.cleanup(result)

    def test_container_is_networkless_limited_and_ephemeral(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "jobs"
            config = SimpleNamespace(
                pdg_fallback_enabled=True,
                pdg_fallback_job_root=root,
                pdg_fallback_docker_socket=Path("/var/run/docker.sock"),
                pdg_fallback_image="autobook-pdg2pic-wine:local",
                pdg_fallback_runtime_volume="autobook-docker_autobook-runtime",
                pdg_fallback_memory_mb=2048,
                pdg_fallback_cpus=2,
                pdg_fallback_timeout_seconds=7200,
            )
            service = PdgFallbackService(config)
            job = root / "abc"
            payload = service._container_payload(job, job / "input", job / "output.pdf")
            host = payload["HostConfig"]
            self.assertEqual(host["NetworkMode"], "none")
            self.assertEqual(host["Memory"], 2048 * 1024 * 1024)
            self.assertEqual(host["NanoCpus"], 2_000_000_000)
            self.assertEqual(host["CapDrop"], ["ALL"])
            self.assertEqual(host["SecurityOpt"], ["no-new-privileges"])
            self.assertEqual(
                host["Binds"],
                ["autobook-docker_autobook-runtime:/opt/autobook-linux/runtime:rw"],
            )
            self.assertNotIn("VolumesFrom", host)
            self.assertEqual(payload["Labels"]["xyz.544544.autobook.ephemeral"], "true")

    def test_authenticated_client_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "pages"
            source.mkdir()
            (source / "000001.pdg").write_bytes(b"HH" + b"\0" * 13 + b"\x04")
            fallback = _FakeFallback(root)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(_IdleManager(), "secret-token", fallback),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = BaiduGatewayClient(
                    f"http://127.0.0.1:{server.server_port}",
                    "secret-token",
                    None,
                    timeout_seconds=10,
                )
                target = root / "result.pdf"
                client.convert_pdg_fallback(source, target)
                with pikepdf.open(target) as document:
                    self.assertEqual(len(document.pages), 1)
                self.assertEqual(fallback.received_names, ["000001.pdg"])
                self.assertTrue(fallback.cleaned)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class PipelineFallbackRoutingTests(unittest.TestCase):
    def _convert(self, root: Path, page_bytes: bytes, gateway: _PipelineGateway) -> tuple[Path, mock.Mock]:
        downloaded = root / "book.zip"
        downloaded.write_bytes(b"PK\x03\x04")
        job = root / "job"
        job.mkdir()

        def extract(_archive, *, target_dir, **_kwargs):
            target_dir.mkdir(parents=True)
            (target_dir / "000001.pdg").write_bytes(page_bytes)
            return target_dir

        converter = mock.Mock()
        converter.convert.side_effect = RuntimeError("open decoder failed")
        pipeline = TaskPipeline(_PipelineConfig(root), gateway=gateway)
        with mock.patch("autobook_linux.pipeline.extract_archive", side_effect=extract), mock.patch(
            "autobook_linux.pipeline.PdgConverter", return_value=converter
        ):
            result = pipeline._to_pdf(downloaded, job, "12345678", "Test Book")
        return result, converter

    def test_open_decoder_failure_uses_wine_for_an_ordinary_pdg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = _PipelineGateway()
            ordinary_00h = b"HH" + b"\0" * 14
            result, converter = self._convert(root, ordinary_00h, gateway)
            converter.convert.assert_called_once_with()
            self.assertEqual(len(gateway.calls), 1)
            with pikepdf.open(result) as document:
                self.assertEqual(len(document.pages), 1)

    def test_successful_open_conversion_does_not_start_wine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloaded = root / "book.zip"
            downloaded.write_bytes(b"PK\x03\x04")
            job = root / "job"
            job.mkdir()
            gateway = _PipelineGateway()

            def extract(_archive, *, target_dir, **_kwargs):
                target_dir.mkdir(parents=True)
                (target_dir / "000001.pdg").write_bytes(b"HH" + b"\0" * 14)
                return target_dir

            class SuccessfulConverter:
                def __init__(self, _source, *, output_path, dpi):
                    self.output = Path(output_path)

                def convert(self):
                    _write_pdf(self.output)

            pipeline = TaskPipeline(_PipelineConfig(root), gateway=gateway)
            with mock.patch("autobook_linux.pipeline.extract_archive", side_effect=extract), mock.patch(
                "autobook_linux.pipeline.PdgConverter", SuccessfulConverter
            ):
                result = pipeline._to_pdf(downloaded, job, "12345678", "Test Book")
            self.assertEqual(gateway.calls, [])
            self.assertTrue(result.is_file())

    def test_incomplete_open_pdf_is_replaced_by_complete_wine_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloaded = root / "book.zip"
            downloaded.write_bytes(b"PK\x03\x04")
            job = root / "job"
            job.mkdir()
            gateway = _PipelineGateway()

            def extract(_archive, *, target_dir, **_kwargs):
                target_dir.mkdir(parents=True)
                for index in range(2):
                    (target_dir / f"{index + 1:06d}.pdg").write_bytes(b"HH" + b"\0" * 14)
                return target_dir

            class IncompleteConverter:
                def __init__(self, _source, *, output_path, dpi):
                    self.output = Path(output_path)

                def convert(self):
                    _write_pdf(self.output, 1)

            pipeline = TaskPipeline(_PipelineConfig(root), gateway=gateway)
            with mock.patch("autobook_linux.pipeline.extract_archive", side_effect=extract), mock.patch(
                "autobook_linux.pipeline.PdgConverter", IncompleteConverter
            ):
                result = pipeline._to_pdf(downloaded, job, "12345678", "Test Book")
            self.assertEqual(len(gateway.calls), 1)
            with pikepdf.open(result) as document:
                self.assertEqual(len(document.pages), 2)

    def test_known_04h_still_bypasses_the_open_decoder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloaded = root / "book.zip"
            downloaded.write_bytes(b"PK\x03\x04")
            job = root / "job"
            job.mkdir()
            gateway = _PipelineGateway()

            def extract(_archive, *, target_dir, **_kwargs):
                target_dir.mkdir(parents=True)
                (target_dir / "000001.pdg").write_bytes(b"HH" + b"\0" * 13 + b"\x04")
                return target_dir

            pipeline = TaskPipeline(_PipelineConfig(root), gateway=gateway)
            with mock.patch("autobook_linux.pipeline.extract_archive", side_effect=extract), mock.patch(
                "autobook_linux.pipeline.PdgConverter"
            ) as converter:
                result = pipeline._to_pdf(downloaded, job, "12345678", "Test Book")
            converter.assert_not_called()
            self.assertEqual(len(gateway.calls), 1)
            self.assertTrue(result.is_file())

    def test_both_converter_errors_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = _PipelineGateway(error="wine failed")
            with self.assertRaises(PipelineError) as caught:
                self._convert(root, b"HH" + b"\0" * 14, gateway)
            message = str(caught.exception)
            self.assertIn("open decoder failed", message)
            self.assertIn("wine failed", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
