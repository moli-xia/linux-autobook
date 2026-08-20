"""End-to-end: a managing panel driving a second panel over the fleet API."""
from __future__ import annotations

import json
import shutil
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from autobook_linux.panel import nodes, passwords
from autobook_linux.panel.envfile import read_env_file, write_env_file
from autobook_linux.panel.server import make_handler
from autobook_linux.panel.settings import PanelSettings
from tests.test_nodes import make_self_signed


def build_panel(root: Path, role: str, cert: Path, key: Path) -> PanelSettings:
    (root / "install.env").write_text(f"INSTALL_ROLE={role}\nPUBLIC_HOST=127.0.0.1\n", encoding="utf-8")
    write_env_file(root / "worker.env", {"PASSWORD_DICT": str(root / "password.txt")}, [])
    return PanelSettings(
        bind="127.0.0.1", port=0, tls_cert=cert, tls_key=key,
        state_file=root / "admin-state.json", config_dir=root, install_dir=root,
        gateway_env=root / "gateway.env", worker_env=root / "worker.env",
        install_env=root / "install.env", session_seconds=3600,
        public_host="127.0.0.1", role=role,
    )


@unittest.skipIf(shutil.which("openssl") is None, "openssl is needed to make a test certificate")
class FleetApiTests(unittest.TestCase):
    """Panel A (gateway) manages panel B (worker) exactly as the UI does."""

    def setUp(self) -> None:
        self.folders = [tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()]
        main_root = Path(self.folders[0].name)
        worker_root = Path(self.folders[1].name)

        # Panel B serves TLS, because the fleet client pins its certificate.
        cert_b, key_b = make_self_signed(worker_root)
        self.worker_settings = build_panel(worker_root, "worker", cert_b, key_b)
        self.worker_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.worker_settings))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_b), str(key_b))
        self.worker_server.socket = context.wrap_socket(self.worker_server.socket, server_side=True)
        self.worker_server.daemon_threads = True
        threading.Thread(target=self.worker_server.serve_forever, daemon=True).start()
        self.worker_port = self.worker_server.server_address[1]

        # Panel A is driven over plain HTTP; only its session layer is exercised.
        cert_a, key_a = make_self_signed(main_root)
        self.main_settings = build_panel(main_root, "all", cert_a, key_a)
        self.main_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.main_settings))
        self.main_server.daemon_threads = True
        threading.Thread(target=self.main_server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.main_server.server_address[1]}"
        self.cookie = ""
        self.csrf = ""
        self._login()

        self.join_code = nodes.make_join_code(
            "worker-b",
            f"https://127.0.0.1:{self.worker_port}",
            nodes.certificate_fingerprint(cert_b),
            nodes.NodeTokenStore(worker_root / nodes.NODE_TOKEN_FILE).get(),
        )

    def tearDown(self) -> None:
        for server in (self.main_server, self.worker_server):
            server.shutdown()
            server.server_close()
        for folder in self.folders:
            folder.cleanup()

    # ------------------------------------------------------------------
    def call(self, path: str, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        if self.csrf:
            request.add_header("X-CSRF-Token", self.csrf)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                cookie = response.headers.get("Set-Cookie", "")
                if cookie:
                    self.cookie = cookie.split(";", 1)[0]
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _login(self) -> None:
        status, payload = self.call("/api/login", "POST", {"username": "admin", "password": "admin"})
        self.assertEqual(status, 200)
        self.csrf = payload["csrf"]

    def add_worker(self) -> str:
        status, payload = self.call("/api/fleet/add", "POST", {"code": self.join_code, "name": "香港节点"})
        self.assertEqual(status, 200, payload)
        return payload["node"]["id"]

    # ------------------------------------------------------------------
    def test_join_code_is_self_describing(self) -> None:
        status, payload = self.call("/api/node/join")
        self.assertEqual(status, 200)
        parsed = nodes.parse_join_code(payload["code"])
        self.assertEqual(parsed["url"], payload["url"])
        self.assertEqual(parsed["fingerprint"], payload["fingerprint"])

    def test_add_node_and_read_its_status(self) -> None:
        node_id = self.add_worker()
        status, payload = self.call("/api/fleet")
        self.assertEqual(status, 200)
        self.assertEqual(payload["local"]["id"], "local")
        entry = [item for item in payload["nodes"] if item["id"] == node_id][0]
        self.assertEqual(entry["name"], "香港节点")
        self.assertTrue(entry["online"], entry.get("error"))
        self.assertEqual(entry["role"], "worker")
        self.assertEqual(payload["summary"]["nodes_total"], 2)

    def test_unreachable_node_is_reported_without_breaking_the_page(self) -> None:
        code = nodes.make_join_code("dead", "https://127.0.0.1:9", "ab" * 32, "tok")
        self.call("/api/fleet/add", "POST", {"code": code})
        status, payload = self.call("/api/fleet")
        self.assertEqual(status, 200)
        self.assertFalse(payload["nodes"][0]["online"])
        self.assertEqual(payload["summary"]["nodes_offline"], 1)

    def test_remote_activity_and_logs_are_proxied(self) -> None:
        node_id = self.add_worker()
        status, payload = self.call(f"/api/fleet/activity?id={node_id}")
        self.assertEqual(status, 200)
        self.assertIn("tasks", payload)
        status, payload = self.call(f"/api/fleet/logs?id={node_id}&service=worker&lines=50")
        self.assertEqual(status, 200)
        self.assertIn("text", payload)

    def test_remote_check_is_proxied(self) -> None:
        node_id = self.add_worker()
        status, payload = self.call("/api/fleet/check", "POST", {"id": node_id, "role": "worker"})
        self.assertEqual(status, 200, payload)
        titles = [item["title"] for item in payload["results"]]
        self.assertIn("配置完整性", titles)

    def test_push_copies_credentials_and_certificate(self) -> None:
        node_id = self.add_worker()
        write_env_file(
            self.main_settings.worker_env,
            {
                "SITE_BASE_URL": "https://site.test",
                "WORKER_TOKEN": "shared-site-token",
                "DRIVE_BASE_URL": "https://drive.test",
                "DRIVE_EMAIL": "ops@example.com",
                "DRIVE_PASSWORD": "drive-secret",
                "PASSWORD_DICT": str(Path(self.main_settings.install_dir) / "password.txt"),
            },
            [],
        )
        write_env_file(
            self.main_settings.gateway_env,
            {
                "BAIDU_GATEWAY_TOKEN": "shared-gateway-token",
                "GATEWAY_PORT": "8765",
                "GATEWAY_TLS_CERT": str(self.main_settings.tls_cert),
            },
            [],
        )
        passwords.save(self.main_settings, ["pushed-one", "pushed-two"])

        status, payload = self.call("/api/fleet/push", "POST", {
            "ids": [node_id],
            "groups": ["site", "drive", "gateway", "passwords"],
        })
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)

        remote = read_env_file(self.worker_settings.worker_env)
        self.assertEqual(remote["WORKER_TOKEN"], "shared-site-token")
        self.assertEqual(remote["DRIVE_PASSWORD"], "drive-secret")
        self.assertEqual(remote["BAIDU_GATEWAY_TOKEN"], "shared-gateway-token")
        self.assertEqual(remote["BAIDU_GATEWAY_URL"], "https://127.0.0.1:8765")
        # The certificate travelled with it and is already wired up.
        cert_path = Path(remote["BAIDU_GATEWAY_CA_FILE"])
        self.assertTrue(cert_path.is_file())
        self.assertIn("BEGIN CERTIFICATE", cert_path.read_text(encoding="utf-8"))
        self.assertEqual(passwords.load(self.worker_settings), ["pushed-one", "pushed-two"])

    def test_push_requires_a_selection(self) -> None:
        self.add_worker()
        status, payload = self.call("/api/fleet/push", "POST", {"ids": [], "groups": ["site"]})
        self.assertEqual(status, 400)
        self.assertIn("节点", payload["error"])

    def test_rename_and_remove_round_trip(self) -> None:
        node_id = self.add_worker()
        self.call("/api/fleet/rename", "POST", {"id": node_id, "name": "新名字"})
        _, payload = self.call("/api/fleet")
        self.assertEqual(payload["nodes"][0]["name"], "新名字")
        self.call("/api/fleet/remove", "POST", {"id": node_id})
        _, payload = self.call("/api/fleet")
        self.assertEqual(payload["nodes"], [])

    def test_rotating_the_worker_token_invalidates_the_old_code(self) -> None:
        node_id = self.add_worker()
        nodes.NodeTokenStore(Path(self.worker_settings.config_dir) / nodes.NODE_TOKEN_FILE).rotate()
        status, payload = self.call("/api/fleet/refresh", "POST")
        self.assertEqual(status, 200)
        entry = [item for item in payload["nodes"] if item["id"] == node_id][0]
        self.assertFalse(entry["online"])
        self.assertIn("令牌", entry["error"])

    def test_fleet_routes_require_a_session(self) -> None:
        self.cookie = ""
        self.csrf = ""
        for path in ("/api/fleet", "/api/node/join"):
            status, _ = self.call(path)
            self.assertEqual(status, 401, path)


if __name__ == "__main__":
    unittest.main()
