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
    _confirmed_04h_pages,
    _safe_extract_zip,
)


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

    def test_gateway_rechecks_that_upload_contains_confirmed_04h(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ordinary.pdg").write_bytes(b"HH" + b"\0" * 14)
            (root / "unknown.pdg").write_bytes(b"HH" + b"\0" * 13 + b"\x05")
            self.assertEqual(_confirmed_04h_pages(root), 0)
            (root / "confirmed.pdg").write_bytes(b"HH" + b"\0" * 13 + b"\x04")
            self.assertEqual(_confirmed_04h_pages(root), 1)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
