from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

LINUX_ROOT = Path(__file__).resolve().parents[1]
if str(LINUX_ROOT) not in sys.path:
    sys.path.insert(0, str(LINUX_ROOT))

import requests

from autobook_linux.admin_panel import (
    AdminSettings,
    PasswordStore,
    apply_config_defaults,
    make_handler,
    read_env_file,
    write_env_file,
)


class AdminPasswordTests(unittest.TestCase):
    def test_default_admin_login_is_hashed_and_marked_for_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "admin-state.json"
            store = PasswordStore(state_path)
            state = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertTrue(store.authenticate("admin", "admin"))
            self.assertFalse(store.authenticate("admin", "wrong"))
            self.assertTrue(store.must_change())
            self.assertNotEqual(state["password_hash"], "admin")
            if os.name != "nt":
                self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_password_change_requires_a_stronger_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PasswordStore(Path(tmp) / "state.json")
            with self.assertRaises(ValueError):
                store.set_credentials("admin", "short")
            store.set_credentials("operator", "a-longer-password")
            self.assertTrue(store.authenticate("operator", "a-longer-password"))
            self.assertFalse(store.must_change())


class AdminEnvironmentTests(unittest.TestCase):
    def test_defaults_restore_required_paths_but_not_optional_secrets(self) -> None:
        values = {"BAIDU_AUTH_FILE": "", "BAIDU_BDUSS": ""}
        apply_config_defaults(values, "gateway")
        self.assertEqual(values["BAIDU_AUTH_FILE"], "/opt/autobook-linux/runtime/baidu_credentials.json")
        self.assertEqual(values["BAIDU_BDUSS"], "")

    def test_environment_writer_round_trips_spaces_quotes_and_backslashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker.env"
            values = {
                "WORKER_ID": "node one",
                "WORKER_TOKEN": 'quote"and\\slash',
                "CUSTOM_SETTING": "preserved",
            }
            write_env_file(path, values, "worker")
            loaded = read_env_file(path)

            self.assertEqual(loaded["WORKER_ID"], values["WORKER_ID"])
            self.assertEqual(loaded["WORKER_TOKEN"], values["WORKER_TOKEN"])
            self.assertEqual(loaded["CUSTOM_SETTING"], "preserved")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class AdminHttpTests(unittest.TestCase):
    def test_health_endpoint_does_not_require_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = AdminSettings(
                bind="127.0.0.1",
                port=0,
                tls_cert=root / "unused.crt",
                tls_key=root / "unused.key",
                state_file=root / "state.json",
                gateway_env=root / "gateway.env",
                worker_env=root / "worker.env",
                session_seconds=3600,
                public_host="127.0.0.1",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(settings))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                response = requests.get(
                    f"http://127.0.0.1:{server.server_port}/health",
                    timeout=10,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "ok"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_default_account_sets_a_hardened_session_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = AdminSettings(
                bind="127.0.0.1",
                port=0,
                tls_cert=root / "unused.crt",
                tls_key=root / "unused.key",
                state_file=root / "state.json",
                gateway_env=root / "gateway.env",
                worker_env=root / "worker.env",
                session_seconds=3600,
                public_host="127.0.0.1",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(settings))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                response = requests.post(
                    f"http://127.0.0.1:{server.server_port}/login",
                    data={"username": "admin", "password": "admin"},
                    allow_redirects=False,
                    timeout=10,
                )
                cookie = response.headers.get("Set-Cookie", "")
                self.assertEqual(response.status_code, 303)
                self.assertIn("Secure", cookie)
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite=Strict", cookie)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
