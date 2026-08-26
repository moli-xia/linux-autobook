"""Tests for the version 2 administration panel."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from autobook_linux.panel import diagnostics, maintenance, schema
from autobook_linux.panel.auth import LoginLimiter, PasswordStore, SessionStore
from autobook_linux.panel.envfile import read_env_file, write_env_file
from autobook_linux.panel.jobs import JobManager
from autobook_linux.panel.server import make_handler
from autobook_linux.panel.settings import PanelSettings


class EnvFileTests(unittest.TestCase):
    def test_round_trip_preserves_quoting(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "worker.env"
            values = {"WORKER_TOKEN": 'a"b\\c', "SITE_BASE_URL": "https://example.test", "EXTRA": ""}
            write_env_file(path, values, schema.key_order("worker"))
            self.assertEqual(read_env_file(path), values)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_managed_keys_are_written_first(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "worker.env"
            write_env_file(path, {"ZZZ_CUSTOM": "1", "SITE_BASE_URL": "https://a.test"}, schema.key_order("worker"))
            body = [line for line in path.read_text(encoding="utf-8").splitlines() if "=" in line]
            self.assertTrue(body[0].startswith("SITE_BASE_URL="))

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(read_env_file(Path("/nonexistent/does-not-exist.env")), {})


class SchemaTests(unittest.TestCase):
    def test_every_field_has_help_text(self) -> None:
        for field in schema.FIELDS:
            self.assertTrue(field.help, f"{field.key} 缺少说明文字")

    def test_defaults_do_not_overwrite_meaningful_blanks(self) -> None:
        values = {"BAIDU_GROUP_NAME": "", "WORKER_QUEUE": ""}
        schema.apply_defaults(values, "worker")
        schema.apply_defaults(values, "gateway")
        self.assertEqual(values["BAIDU_GROUP_NAME"], "")     # keep_blank field
        self.assertEqual(values["WORKER_QUEUE"], "pdf")      # normal default

    def test_public_schema_is_role_filtered(self) -> None:
        groups = schema.public_schema(["worker"])
        self.assertTrue(groups)
        self.assertTrue(all(group["target"] == "worker" for group in groups))

    def test_shared_field_belongs_to_both_targets(self) -> None:
        self.assertIn("BAIDU_GATEWAY_TOKEN", schema.key_order("worker"))
        self.assertIn("BAIDU_GATEWAY_TOKEN", schema.key_order("gateway"))


class ReadinessTests(unittest.TestCase):
    def test_worker_reports_every_missing_secret(self) -> None:
        issues = diagnostics.readiness("worker", {})
        keys = {issue.key for issue in issues}
        self.assertIn("WORKER_TOKEN", keys)
        self.assertIn("DRIVE_PASSWORD", keys)
        self.assertIn("BAIDU_GATEWAY_URL", keys)

    def test_worker_rejects_plain_http_gateway(self) -> None:
        issues = diagnostics.readiness("worker", {"BAIDU_GATEWAY_URL": "http://gw.test:8765"})
        self.assertTrue(any("https" in issue.message for issue in issues))

    def test_gateway_requires_login_material(self) -> None:
        issues = diagnostics.readiness("gateway", {"BAIDU_GATEWAY_TOKEN": "x" * 32})
        messages = " ".join(issue.message for issue in issues)
        self.assertIn("扫码登录", messages)

    def test_gateway_rejects_half_configured_cookies(self) -> None:
        issues = diagnostics.readiness("gateway", {"BAIDU_BDUSS": "abc"})
        self.assertTrue(any("STOKEN" in issue.message for issue in issues))


class AuthTests(unittest.TestCase):
    def test_password_round_trip_and_change(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = PasswordStore(Path(folder) / "state.json")
            self.assertTrue(store.authenticate("admin", "admin"))
            self.assertTrue(store.must_change())
            store.set_credentials("operator", "correct-horse-battery")
            self.assertFalse(store.authenticate("admin", "admin"))
            self.assertTrue(store.authenticate("operator", "correct-horse-battery"))
            self.assertFalse(store.must_change())

    def test_short_passwords_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = PasswordStore(Path(folder) / "state.json")
            with self.assertRaises(ValueError):
                store.set_credentials("admin", "short")

    def test_sessions_expire_and_delete(self) -> None:
        store = SessionStore(lifetime=3600)
        session = store.create("admin")
        self.assertIsNotNone(store.get(session["token"]))
        store.delete(session["token"])
        self.assertIsNone(store.get(session["token"]))

    def test_login_limiter_locks_after_repeated_failures(self) -> None:
        limiter = LoginLimiter(limit=3, window=600)
        for _ in range(3):
            limiter.fail("10.0.0.1")
        self.assertFalse(limiter.allowed("10.0.0.1"))
        limiter.clear("10.0.0.1")
        self.assertTrue(limiter.allowed("10.0.0.1"))


class JobTests(unittest.TestCase):
    def test_command_job_captures_output(self) -> None:
        manager = JobManager()
        job = manager.spawn_command("test", "回显", [sys.executable, "-c", "print('hello-job')"])
        for _ in range(100):
            if job.status != "running":
                break
            threading.Event().wait(0.1)
        snapshot = job.snapshot()
        self.assertEqual(snapshot["status"], "success")
        self.assertIn("hello-job", snapshot["log"])

    def test_exclusive_jobs_do_not_overlap(self) -> None:
        manager = JobManager()
        manager.spawn("slow", "慢任务", lambda job: threading.Event().wait(2))
        with self.assertRaises(RuntimeError):
            manager.spawn("slow", "慢任务", lambda job: None)


class ActivityTests(unittest.TestCase):
    def test_journal_lines_become_task_rows(self) -> None:
        self.assertTrue(maintenance.CLAIM_RE.search("2026-01-01T00:00:00+0000 host py[1]: 领取任务 #42: 测试书名"))
        match = maintenance.DONE_RE.search("2026-01-01T00:01:00+0000 host py[1]: 任务 #42 完成: https://drive.test/s/x")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "42")


class CleanupActionTests(unittest.TestCase):
    """The maintenance button must preview by default and delete only on request."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.settings = PanelSettings(
            bind="127.0.0.1", port=0,
            tls_cert=root / "admin.crt", tls_key=root / "admin.key",
            state_file=root / "admin-state.json",
            config_dir=root, install_dir=root,
            install_env=root / "install.env",
            gateway_env=root / "gateway.env", worker_env=root / "worker.env",
            session_seconds=3600, public_host="127.0.0.1", role="worker",
        )
        self.spawned: dict = {}

        class Recorder:
            def spawn_command(inner, kind, title, command, cwd=None, env=None, timeout=3600):
                self.spawned.update(kind=kind, title=title, command=command, env=env)
                return types.SimpleNamespace(id="job-1", title=title)

        self.manager = Recorder()

    def test_preview_does_not_pass_execute(self) -> None:
        job = maintenance.run_cleanup(self.settings, self.manager, execute=False)
        self.assertNotIn("--execute", self.spawned["command"])
        self.assertIn("storage_sweep.py", " ".join(self.spawned["command"]))
        self.assertEqual(job.id, "job-1")

    def test_deleting_passes_execute(self) -> None:
        maintenance.run_cleanup(self.settings, self.manager, execute=True)
        self.assertIn("--execute", self.spawned["command"])

    def test_the_child_keeps_a_usable_environment(self) -> None:
        # spawn_command replaces the environment wholesale; a bare one-key env
        # would leave the child without PATH.
        maintenance.run_cleanup(self.settings, self.manager, execute=False)
        env = self.spawned["env"]
        self.assertEqual(env["ADMIN_CONFIG_DIR"], str(self.settings.config_dir))
        self.assertTrue(len(env) > 1)


class ApiTests(unittest.TestCase):
    """End-to-end HTTP tests against the handler (TLS is terminated elsewhere)."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        (root / "install.env").write_text("INSTALL_ROLE=worker\nPUBLIC_HOST=127.0.0.1\n", encoding="utf-8")
        self.settings = PanelSettings(
            bind="127.0.0.1", port=0,
            tls_cert=root / "admin.crt", tls_key=root / "admin.key",
            state_file=root / "admin-state.json",
            config_dir=root, install_dir=root,
            gateway_env=root / "gateway.env", worker_env=root / "worker.env",
            install_env=root / "install.env",
            session_seconds=3600, public_host="127.0.0.1", role="worker",
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.settings))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookie = ""
        self.csrf = ""

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def call(self, path: str, method: str = "GET", body: dict | None = None, csrf: str | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        token = self.csrf if csrf is None else csrf
        if token:
            request.add_header("X-CSRF-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                cookie = response.headers.get("Set-Cookie", "")
                if cookie:
                    self.cookie = cookie.split(";", 1)[0]
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def login(self) -> None:
        status, payload = self.call("/api/login", "POST", {"username": "admin", "password": "admin"})
        self.assertEqual(status, 200)
        self.csrf = payload["csrf"]

    def test_health_needs_no_session(self) -> None:
        status, payload = self.call("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_protected_endpoints_require_login(self) -> None:
        status, _ = self.call("/api/config")
        self.assertEqual(status, 401)

    def test_bad_password_is_rejected(self) -> None:
        status, payload = self.call("/api/login", "POST", {"username": "admin", "password": "nope"})
        self.assertEqual(status, 401)
        self.assertIn("不正确", payload["error"])

    def test_config_save_round_trip(self) -> None:
        self.login()
        status, payload = self.call("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(payload["schema"])
        status, payload = self.call("/api/config", "POST", {"values": {
            "SITE_BASE_URL": "https://site.test",
            "WORKER_TOKEN": "token-value",
            "WORKER_ID": "unit-test",
        }})
        self.assertEqual(status, 200, payload)
        stored = read_env_file(self.settings.worker_env)
        self.assertEqual(stored["SITE_BASE_URL"], "https://site.test")
        self.assertEqual(stored["WORKER_TOKEN"], "token-value")
        _, payload = self.call("/api/config")
        self.assertNotIn("WORKER_TOKEN", payload["values"])          # secrets never echo back
        self.assertTrue(payload["secrets_set"]["WORKER_TOKEN"])

    def test_blank_secret_keeps_previous_value(self) -> None:
        self.login()
        self.call("/api/config", "POST", {"values": {"WORKER_TOKEN": "keep-me"}})
        self.call("/api/config", "POST", {"values": {"WORKER_TOKEN": "", "WORKER_ID": "changed"}})
        stored = read_env_file(self.settings.worker_env)
        self.assertEqual(stored["WORKER_TOKEN"], "keep-me")
        self.assertEqual(stored["WORKER_ID"], "changed")

    def test_numeric_bounds_are_enforced(self) -> None:
        self.login()
        status, payload = self.call("/api/config", "POST", {"values": {"CONCURRENCY": "99"}})
        self.assertEqual(status, 400)
        self.assertIn("不能大于", payload["error"])

    def test_missing_csrf_header_is_rejected(self) -> None:
        self.login()
        status, payload = self.call("/api/config", "POST", {"values": {}}, csrf="")
        self.assertEqual(status, 403)
        self.assertIn("会话校验", payload["error"])

    def test_start_is_blocked_while_configuration_is_incomplete(self) -> None:
        self.login()
        status, payload = self.call("/api/service", "POST", {"service": "worker", "action": "start"})
        self.assertEqual(status, 400)
        self.assertIn("配置尚未完成", payload["error"])

    def test_gateway_endpoints_are_hidden_for_worker_role(self) -> None:
        self.login()
        status, payload = self.call("/api/baidu/start", "POST", {})
        self.assertEqual(status, 400)
        self.assertIn("未安装", payload["error"])

    def test_static_paths_cannot_escape_the_asset_folder(self) -> None:
        request = urllib.request.Request(self.base + "/../server.py")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertNotEqual(response.status, 200)
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (400, 404))

    def test_session_reports_role_from_install_state(self) -> None:
        self.login()
        status, payload = self.call("/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "worker")
        self.assertEqual(payload["roles"], ["worker"])


if __name__ == "__main__":
    unittest.main()
