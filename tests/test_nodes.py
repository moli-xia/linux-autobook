"""Tests for fleet management: join codes, pinned TLS and peer authorisation."""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from autobook_linux.panel import nodes
from autobook_linux.panel.envfile import write_env_file
from autobook_linux.panel.server import make_handler
from autobook_linux.panel.settings import PanelSettings


def make_self_signed(folder: Path, host: str = "127.0.0.1") -> tuple[Path, Path]:
    cert, key = folder / "admin.crt", folder / "admin.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "2",
         "-keyout", str(key), "-out", str(cert), "-subj", f"/CN={host}",
         "-addext", f"subjectAltName=IP:{host}"],
        capture_output=True, check=True, timeout=120,
    )
    return cert, key


class JoinCodeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        code = nodes.make_join_code("node-a", "https://10.0.0.5:8766/", "ab" * 32, "tok")
        parsed = nodes.parse_join_code("  " + code + "\n")
        self.assertEqual(parsed["name"], "node-a")
        self.assertEqual(parsed["url"], "https://10.0.0.5:8766")
        self.assertEqual(parsed["fingerprint"], "ab" * 32)
        self.assertEqual(parsed["token"], "tok")

    def test_garbage_is_rejected_with_a_readable_message(self) -> None:
        for bad in ("", "hello", nodes.JOIN_PREFIX + "!!!!"):
            with self.assertRaises(ValueError):
                nodes.parse_join_code(bad)

    def test_plain_http_is_refused(self) -> None:
        code = nodes.make_join_code("n", "http://10.0.0.5:8766", "ab" * 32, "tok")
        with self.assertRaises(ValueError) as caught:
            nodes.parse_join_code(code)
        self.assertIn("https", str(caught.exception))


class NodeTokenTests(unittest.TestCase):
    def test_token_is_created_once_and_compared_safely(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = nodes.NodeTokenStore(Path(folder) / "node-token")
            token = store.get()
            self.assertGreater(len(token), 30)
            self.assertEqual(store.get(), token)
            self.assertTrue(store.matches(token))
            self.assertFalse(store.matches("wrong"))
            self.assertFalse(store.matches(""))
            if os.name == "posix":
                self.assertEqual((Path(folder) / "node-token").stat().st_mode & 0o777, 0o600)

    def test_rotation_invalidates_the_previous_token(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = nodes.NodeTokenStore(Path(folder) / "node-token")
            first = store.get()
            second = store.rotate()
            self.assertNotEqual(first, second)
            self.assertFalse(store.matches(first))
            self.assertTrue(store.matches(second))


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "nodes.json"
        self.registry = nodes.NodeRegistry(self.path)

    def tearDown(self) -> None:
        self.folder.cleanup()

    def _code(self, url: str = "https://10.0.0.5:8766", token: str = "tok") -> str:
        return nodes.make_join_code("worker-a", url, "ab" * 32, token)

    def test_add_persists_and_reloads(self) -> None:
        node = self.registry.add(self._code())
        self.assertEqual(len(self.registry.list()), 1)
        reloaded = nodes.NodeRegistry(self.path)
        self.assertEqual([n.id for n in reloaded.list()], [node.id])
        self.assertEqual(reloaded.get(node.id).url, "https://10.0.0.5:8766")

    def test_re_adding_the_same_address_refreshes_credentials(self) -> None:
        first = self.registry.add(self._code(token="old"))
        second = self.registry.add(self._code(token="new"))
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.registry.list()), 1)
        self.assertEqual(self.registry.get(first.id).token, "new")

    def test_rename_and_remove(self) -> None:
        node = self.registry.add(self._code())
        self.registry.rename(node.id, "香港节点")
        self.assertEqual(self.registry.get(node.id).name, "香港节点")
        with self.assertRaises(nodes.NodeError):
            self.registry.rename(node.id, "   ")
        self.registry.remove(node.id)
        self.assertEqual(self.registry.list(), [])
        with self.assertRaises(nodes.NodeError):
            self.registry.get(node.id)

    def test_offline_node_is_reported_not_raised(self) -> None:
        node = self.registry.add(nodes.make_join_code("dead", "https://127.0.0.1:9", "ab" * 32, "t"))
        status = self.registry.poll(node.id)
        self.assertFalse(status["online"])
        self.assertTrue(status["error"])

    def test_summary_counts_running_workers(self) -> None:
        local = {"services": [{"name": "worker", "installed": True, "running": True},
                              {"name": "gateway", "installed": True, "running": True}],
                 "issue_count": 0}
        remote = [
            {"online": True, "issue_count": 2,
             "services": [{"name": "worker", "installed": True, "running": False}]},
            {"online": False, "error": "boom"},
        ]
        summary = nodes.summarise(local, remote)
        self.assertEqual(summary["nodes_total"], 3)
        self.assertEqual(summary["nodes_offline"], 1)
        self.assertEqual(summary["workers_total"], 2)
        self.assertEqual(summary["workers_running"], 1)
        self.assertEqual(summary["gateways_running"], 1)
        self.assertEqual(summary["issues"], 2)


@unittest.skipIf(shutil.which("openssl") is None, "openssl is needed to make a test certificate")
class PeerApiTests(unittest.TestCase):
    """Drive a real TLS panel through NodeClient, exactly as the fleet page does."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        (root / "install.env").write_text("INSTALL_ROLE=worker\nPUBLIC_HOST=127.0.0.1\n", encoding="utf-8")
        write_env_file(root / "worker.env", {"PASSWORD_DICT": str(root / "password.txt")}, [])
        cert, key = make_self_signed(root)
        self.settings = PanelSettings(
            bind="127.0.0.1", port=0, tls_cert=cert, tls_key=key,
            state_file=root / "admin-state.json", config_dir=root, install_dir=root,
            gateway_env=root / "gateway.env", worker_env=root / "worker.env",
            install_env=root / "install.env", session_seconds=3600,
            public_host="127.0.0.1", role="worker",
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.settings))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.fingerprint = nodes.certificate_fingerprint(cert)
        self.token = nodes.NodeTokenStore(root / nodes.NODE_TOKEN_FILE).get()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def client(self, token: str = "", fingerprint: str = "") -> nodes.NodeClient:
        return nodes.NodeClient(
            f"https://127.0.0.1:{self.port}",
            fingerprint or self.fingerprint,
            token or self.token,
        )

    def test_node_token_reads_the_overview(self) -> None:
        overview = self.client().request("GET", "/api/overview")
        self.assertEqual(overview["role"], "worker")
        self.assertIn("services", overview)

    def test_node_token_reads_activity_and_passwords(self) -> None:
        self.assertIn("tasks", self.client().request("GET", "/api/activity"))
        self.assertIn("count", self.client().request("GET", "/api/passwords"))

    def test_wrong_fingerprint_is_refused_before_any_request(self) -> None:
        with self.assertRaises(nodes.NodeError) as caught:
            self.client(fingerprint="cd" * 32).request("GET", "/api/overview")
        self.assertIn("指纹", str(caught.exception))

    def test_wrong_token_is_rejected(self) -> None:
        with self.assertRaises(nodes.NodeError):
            self.client(token="not-the-token").request("GET", "/api/overview")

    def test_token_cannot_reach_account_or_fleet_routes(self) -> None:
        for method, path in (("POST", "/api/account"), ("GET", "/api/node/join"),
                             ("GET", "/api/fleet"), ("POST", "/api/fleet/add"),
                             ("POST", "/api/maintenance")):
            with self.assertRaises(nodes.NodeError, msg=f"{method} {path} must stay private"):
                self.client().request(method, path, {} if method == "POST" else None)

    def test_peer_can_push_configuration(self) -> None:
        self.client().request("POST", "/api/config", {"values": {"WORKER_ID": "pushed-name"}})
        config = self.client().request("GET", "/api/config")
        self.assertEqual(config["values"]["WORKER_ID"], "pushed-name")

    def test_peer_can_replace_the_password_dictionary(self) -> None:
        result = self.client().request(
            "POST", "/api/passwords", {"action": "replace", "content": ["alpha", "beta"]})
        self.assertEqual(result["count"], 2)

    def test_peer_can_install_a_gateway_certificate(self) -> None:
        pem = Path(self.settings.tls_cert).read_text(encoding="utf-8")
        result = self.client().request("POST", "/api/gateway-cert", {"pem": pem})
        self.assertTrue(result["ok"])
        target = Path(result["path"])
        self.assertTrue(target.is_file())
        stored = json.dumps(target.read_text(encoding="utf-8"))
        self.assertIn("BEGIN CERTIFICATE", stored)

    def test_invalid_certificate_is_rejected(self) -> None:
        with self.assertRaises(nodes.NodeError):
            self.client().request("POST", "/api/gateway-cert", {"pem": "not a certificate"})

    def test_registry_polls_a_live_node(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            registry = nodes.NodeRegistry(Path(other) / "nodes.json")
            code = nodes.make_join_code(
                "worker-1", f"https://127.0.0.1:{self.port}", self.fingerprint, self.token)
            node = registry.add(code)
            status = registry.poll(node.id)
            self.assertTrue(status["online"], status.get("error"))
            self.assertEqual(status["role"], "worker")
            summary = nodes.summarise({"services": [], "issue_count": 0}, registry.all_status())
            self.assertEqual(summary["nodes_total"], 2)


if __name__ == "__main__":
    unittest.main()
