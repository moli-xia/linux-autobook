"""Tests for the editable password dictionary and the container supervisor."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from autobook_linux.panel import passwords, services
from autobook_linux.panel.envfile import write_env_file
from autobook_linux.panel.settings import PanelSettings
from autobook_linux.panel.supervisor import Supervisor


def build_settings(root: Path) -> PanelSettings:
    (root / "install.env").write_text("INSTALL_ROLE=all\n", encoding="utf-8")
    write_env_file(root / "worker.env", {"PASSWORD_DICT": str(root / "password.txt")}, [])
    return PanelSettings(
        bind="127.0.0.1", port=0,
        tls_cert=root / "admin.crt", tls_key=root / "admin.key",
        state_file=root / "admin-state.json",
        config_dir=root, install_dir=root,
        gateway_env=root / "gateway.env", worker_env=root / "worker.env",
        install_env=root / "install.env",
        session_seconds=3600, public_host="127.0.0.1", role="all",
    )


class PasswordDictionaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.settings = build_settings(Path(self.folder.name))

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_built_in_defaults_are_shipped(self) -> None:
        defaults = passwords.default_entries()
        self.assertGreater(len(defaults), 100)
        self.assertEqual(len(defaults), len(set(defaults)))

    def test_missing_dictionary_is_seeded_from_defaults(self) -> None:
        self.assertTrue(passwords.ensure_seeded(self.settings))
        self.assertEqual(passwords.load(self.settings), passwords.default_entries())
        # A second call must not overwrite an existing dictionary.
        self.assertFalse(passwords.ensure_seeded(self.settings))

    def test_add_update_and_remove(self) -> None:
        passwords.save(self.settings, ["alpha", "beta"])
        passwords.add(self.settings, "gamma")
        self.assertEqual(passwords.load(self.settings), ["alpha", "beta", "gamma"])
        passwords.update(self.settings, "beta", "beta2")
        self.assertEqual(passwords.load(self.settings), ["alpha", "beta2", "gamma"])
        passwords.remove(self.settings, "alpha")
        self.assertEqual(passwords.load(self.settings), ["beta2", "gamma"])

    def test_duplicates_are_rejected_and_deduplicated(self) -> None:
        passwords.save(self.settings, ["a", "b", "a"])
        self.assertEqual(passwords.load(self.settings), ["a", "b"])
        with self.assertRaises(ValueError):
            passwords.add(self.settings, "b")
        with self.assertRaises(ValueError):
            passwords.add(self.settings, "   ")

    def test_update_of_a_removed_entry_fails_clearly(self) -> None:
        passwords.save(self.settings, ["a"])
        with self.assertRaises(ValueError) as caught:
            passwords.update(self.settings, "missing", "x")
        self.assertIn("已不存在", str(caught.exception))

    def test_merge_defaults_keeps_custom_entries(self) -> None:
        passwords.save(self.settings, ["my-own-password"])
        count, _ = passwords.merge_defaults(self.settings)
        entries = passwords.load(self.settings)
        self.assertEqual(entries[0], "my-own-password")
        self.assertEqual(count, len(entries))
        for value in passwords.default_entries():
            self.assertIn(value, entries)

    def test_restore_defaults_replaces_everything(self) -> None:
        passwords.save(self.settings, ["my-own-password"])
        passwords.restore_defaults(self.settings)
        self.assertEqual(passwords.load(self.settings), passwords.default_entries())

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        path = Path(self.folder.name) / "password.txt"
        path.write_text("# comment\n\nalpha\n  beta  \n", encoding="utf-8")
        self.assertEqual(passwords.load(self.settings), ["alpha", "beta"])

    def test_dictionary_cannot_escape_the_install_directory(self) -> None:
        write_env_file(self.settings.worker_env, {"PASSWORD_DICT": "/etc/shadow"}, [])
        with self.assertRaises(ValueError):
            passwords.load(self.settings)

    def test_snapshot_counts_custom_and_missing_entries(self) -> None:
        defaults = passwords.default_entries()
        passwords.save(self.settings, defaults[:5] + ["custom-one"])
        snapshot = passwords.snapshot(self.settings)
        self.assertEqual(snapshot["count"], 6)
        self.assertEqual(snapshot["custom_count"], 1)
        self.assertEqual(snapshot["missing_defaults"], len(defaults) - 5)

    def test_saved_file_is_private(self) -> None:
        passwords.save(self.settings, ["alpha"])
        path = Path(self.folder.name) / "password.txt"
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_text(encoding="utf-8"), "alpha\n")


@unittest.skipIf(os.name == "nt", "the supervisor uses POSIX process groups")
class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.supervisor = Supervisor(self.root, self.root, self.root / "runtime")

    def tearDown(self) -> None:
        self.supervisor.shutdown()
        self.folder.cleanup()

    def _use_script(self, name: str, body: str) -> None:
        self.supervisor.get(name).command = [sys.executable, "-c", body]

    def test_start_stop_and_log_capture(self) -> None:
        self._use_script("worker", "import time;print('worker up', flush=True);time.sleep(30)")
        process = self.supervisor.get("worker")
        process.start()
        time.sleep(1.0)
        self.assertTrue(process.running())
        self.assertIn("worker up", process.read_log(50))
        process.stop(grace=5)
        self.assertFalse(process.running())

    def test_config_failure_exit_stops_the_restart_loop(self) -> None:
        self._use_script("worker", "raise SystemExit(78)")
        process = self.supervisor.get("worker")
        process.start()
        for _ in range(50):
            process.reap()
            if not process.want_running:
                break
            time.sleep(0.1)
        self.assertFalse(process.want_running)
        self.assertEqual(process.last_exit, 78)
        self.assertIn("配置", process.snapshot()["message"])

    def test_state_round_trip_drives_autostart(self) -> None:
        self._use_script("worker", "import time;time.sleep(30)")
        self.supervisor.get("worker").want_running = True
        self.supervisor.save_state()
        self.assertEqual(self.supervisor.load_state()["worker"], True)

    def test_services_backend_reports_supervised_state(self) -> None:
        os.environ["AUTOBOOK_SUPERVISOR"] = "internal"
        services.bind_supervisor(self.supervisor)
        try:
            self._use_script("worker", "import time;print('hello', flush=True);time.sleep(30)")
            state = services.status("worker", ["worker"])
            self.assertFalse(state["running"])
            self.assertTrue(state["installed"])
            services.control("worker", "start")
            time.sleep(1.0)
            self.assertTrue(services.status("worker", ["worker"])["running"])
            self.assertIn("hello", services.logs("worker", 50))
            services.control("worker", "stop")
            self.assertFalse(services.status("worker", ["worker"])["running"])
        finally:
            services.bind_supervisor(None)
            os.environ.pop("AUTOBOOK_SUPERVISOR", None)

    def test_worker_only_role_marks_gateway_not_installed(self) -> None:
        os.environ["AUTOBOOK_SUPERVISOR"] = "internal"
        services.bind_supervisor(self.supervisor)
        try:
            self.assertEqual(services.status("gateway", ["worker"])["active"], "not-installed")
        finally:
            services.bind_supervisor(None)
            os.environ.pop("AUTOBOOK_SUPERVISOR", None)


class LogRedactionTests(unittest.TestCase):
    def test_secrets_never_reach_the_browser(self) -> None:
        text = services.redact("fetch https://d.pcs.baidu.com/file?sign=abc BDUSS=secretvalue")
        self.assertNotIn("secretvalue", text)
        self.assertNotIn("sign=abc", text)


if __name__ == "__main__":
    unittest.main()
