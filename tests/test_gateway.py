from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

LINUX_ROOT = Path(__file__).resolve().parents[1]
if str(LINUX_ROOT) not in sys.path:
    sys.path.insert(0, str(LINUX_ROOT))

from autobook_linux.gateway_client import BaiduGatewayClient, GatewayError
from autobook_linux.gateway_server import GatewayJob, _safe_filename, make_handler


class _ReadyManager:
    def __init__(self, artifact: Path) -> None:
        self.job = GatewayJob(
            job_id="a" * 32,
            request_id="task-1-lease",
            ssno="12345678",
            status="ready",
            filename="测试_12345678.pdf",
            artifact=artifact,
            size=artifact.stat().st_size,
        )
        import hashlib

        self.job.sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.deleted = False

    def stats(self):
        return {"pending": 0, "running": 0, "ready": 1, "failed": 0}

    def submit(self, ssno, request_id, kind="ss", plan=None):
        if ssno != self.job.ssno or request_id != self.job.request_id:
            raise ValueError("unexpected request")
        self.last_kind = kind
        self.last_plan = plan
        return self.job

    def get(self, job_id):
        return self.job if job_id == self.job.job_id else None

    def delete(self, job_id):
        self.deleted = job_id == self.job.job_id
        return self.deleted


class GatewayProtocolTests(unittest.TestCase):
    def test_gateway_filename_cannot_escape_job_directory(self) -> None:
        self.assertEqual(_safe_filename("../../book.pdf", "12345678"), "_.._book.pdf")
        self.assertEqual(_safe_filename("", "12345678"), "12345678.bin")

    def test_authenticated_fetch_streams_and_verifies_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "source.pdf"
            artifact.write_bytes(b"%PDF-1.7\ncentral-gateway-test")
            manager = _ReadyManager(artifact)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager, "secret-token"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = BaiduGatewayClient(
                    f"http://127.0.0.1:{server.server_port}",
                    "secret-token",
                    None,
                    timeout_seconds=10,
                    poll_seconds=1,
                )
                downloaded, original = client.fetch("12345678", "task-1-lease", root / "downloads")
                self.assertEqual(downloaded.read_bytes(), artifact.read_bytes())
                self.assertEqual(original, "测试_12345678.pdf")
                self.assertTrue(manager.deleted)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_gateway_rejects_wrong_bearer_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "source.pdf"
            artifact.write_bytes(b"test")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_ReadyManager(artifact), "right"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = BaiduGatewayClient(
                    f"http://127.0.0.1:{server.server_port}",
                    "wrong",
                    None,
                )
                with self.assertRaises(GatewayError):
                    client.fetch("12345678", "task-1-lease", root / "downloads")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
