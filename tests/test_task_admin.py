"""Panel-side task management: who runs what, and who may delete it."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autobook_linux.panel import tasks as site_tasks
from autobook_linux.panel.settings import PanelSettings


def settings_in(root: Path, role: str = "all") -> PanelSettings:
    return PanelSettings(
        bind="127.0.0.1", port=0,
        tls_cert=root / "admin.crt", tls_key=root / "admin.key",
        state_file=root / "admin-state.json",
        config_dir=root, install_dir=root,
        install_env=root / "install.env",
        gateway_env=root / "gateway.env", worker_env=root / "worker.env",
        session_seconds=3600, public_host="127.0.0.1", role=role,
    )


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "worker.env").write_text(
            "SITE_BASE_URL=https://site.test\nWORKER_TOKEN=tok\n", encoding="utf-8")
        (self.root / "gateway.env").write_text("", encoding="utf-8")
        self.settings = settings_in(self.root)


class CredentialTests(Fixture):
    def test_the_site_address_comes_from_the_worker_file(self) -> None:
        self.assertEqual(site_tasks.site_credentials(self.settings), ("https://site.test", "tok"))

    def test_a_trailing_slash_is_dropped(self) -> None:
        (self.root / "worker.env").write_text(
            "SITE_BASE_URL=https://site.test/\nWORKER_TOKEN=tok\n", encoding="utf-8")
        self.assertEqual(site_tasks.site_credentials(self.settings)[0], "https://site.test")

    def test_the_gateway_file_is_used_when_the_worker_one_is_bare(self) -> None:
        (self.root / "worker.env").write_text("", encoding="utf-8")
        (self.root / "gateway.env").write_text(
            "SITE_BASE_URL=https://g.test\nWORKER_TOKEN=gtok\n", encoding="utf-8")
        self.assertEqual(site_tasks.site_credentials(self.settings), ("https://g.test", "gtok"))

    def test_missing_configuration_says_what_to_fill_in(self) -> None:
        (self.root / "worker.env").write_text("", encoding="utf-8")
        with self.assertRaises(site_tasks.TaskAdminError) as caught:
            site_tasks.site_credentials(self.settings)
        self.assertIn("配置", str(caught.exception))


class DescribeTests(unittest.TestCase):
    def test_a_running_task_reports_how_long_it_has_been_going(self) -> None:
        row = site_tasks.describe(
            {"id": 5, "status": 2, "started_at": 1000, "heartbeat_at": 1500}, now=1600)
        self.assertEqual(row["elapsed"], 600)
        self.assertEqual(row["status_label"], "PDF处理中")

    def test_a_finished_task_reports_how_long_it_took(self) -> None:
        row = site_tasks.describe(
            {"id": 5, "status": 3, "started_at": 1000, "finished_at": 1300}, now=9999)
        self.assertEqual(row["elapsed"], 300)

    def test_a_silent_worker_marks_the_task_stale(self) -> None:
        row = site_tasks.describe(
            {"id": 5, "status": 2, "started_at": 1000, "heartbeat_at": 1000}, now=2000)
        self.assertTrue(row["stale"], "no heartbeat for 1000s must be visible")

    def test_a_healthy_task_is_not_stale(self) -> None:
        row = site_tasks.describe(
            {"id": 5, "status": 2, "started_at": 1000, "heartbeat_at": 1990}, now=2000)
        self.assertFalse(row["stale"])

    def test_a_queued_task_is_never_stale(self) -> None:
        row = site_tasks.describe({"id": 5, "status": 1, "dateline": 10}, now=99999)
        self.assertFalse(row["stale"])
        self.assertEqual(row["elapsed"], 0)

    def test_the_keyword_stands_in_for_a_missing_title(self) -> None:
        row = site_tasks.describe({"id": 5, "status": 1, "keyword": "某书"}, now=1)
        self.assertEqual(row["title"], "某书")

    def test_the_worker_that_holds_the_task_is_reported(self) -> None:
        row = site_tasks.describe({"id": 5, "status": 2, "worker_id": "main-107-worker"}, now=1)
        self.assertEqual(row["worker_id"], "main-107-worker")


class ListingTests(Fixture):
    def respond(self, payload):
        response = mock.Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return mock.patch("autobook_linux.panel.tasks.requests.post", return_value=response)

    def test_tasks_are_summarised_by_state(self) -> None:
        payload = {
            "kong_status": 1, "now": 2000,
            "counts": {"1": 3, "2": 1, "3": 10, "4": 2, "6": 1, "7": 1},
            "tasks": [
                {"id": 1, "status": 2, "worker_id": "node-a", "started_at": 1000, "heartbeat_at": 1990},
                {"id": 2, "status": 1, "worker_id": ""},
            ],
        }
        with self.respond(payload):
            result = site_tasks.list_tasks(self.settings)
        self.assertEqual(result["running"], 2)   # statuses 2 and 7
        self.assertEqual(result["queued"], 4)    # statuses 1 and 6
        self.assertEqual(result["workers"], ["node-a"])

    def test_a_site_error_is_surfaced_verbatim(self) -> None:
        with self.respond({"kong_status": 0, "message": "worker token 不正确"}):
            with self.assertRaises(site_tasks.TaskAdminError) as caught:
                site_tasks.list_tasks(self.settings)
        self.assertIn("worker token", str(caught.exception))

    def test_an_old_site_plugin_is_reported_clearly(self) -> None:
        response = mock.Mock()
        response.json.side_effect = ValueError("not json")
        response.raise_for_status.return_value = None
        with mock.patch("autobook_linux.panel.tasks.requests.post", return_value=response):
            with self.assertRaises(site_tasks.TaskAdminError) as caught:
                site_tasks.list_tasks(self.settings)
        self.assertIn("插件版本", str(caught.exception))

    def test_the_limit_is_clamped(self) -> None:
        with self.respond({"kong_status": 1, "tasks": [], "counts": {}}) as post:
            site_tasks.list_tasks(self.settings, limit=9999)
        self.assertEqual(post.call_args.kwargs["data"]["limit"], 200)


class DeleteTests(Fixture):
    def respond(self, payload):
        response = mock.Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return mock.patch("autobook_linux.panel.tasks.requests.post", return_value=response)

    def test_deleting_passes_the_ids(self) -> None:
        with self.respond({"kong_status": 1, "removed": 2}) as post:
            self.assertEqual(site_tasks.delete_tasks(self.settings, [4, 7]), 2)
        self.assertEqual(post.call_args.kwargs["data"]["ids"], "4,7")

    def test_an_empty_selection_is_refused_without_a_request(self) -> None:
        with mock.patch("autobook_linux.panel.tasks.requests.post") as post:
            with self.assertRaises(site_tasks.TaskAdminError):
                site_tasks.delete_tasks(self.settings, [])
        post.assert_not_called()

    def test_an_absurd_batch_is_refused(self) -> None:
        with self.assertRaises(site_tasks.TaskAdminError):
            site_tasks.delete_tasks(self.settings, list(range(1, 600)))

    def test_clearing_passes_the_statuses(self) -> None:
        with self.respond({"kong_status": 1, "removed": 9}) as post:
            self.assertEqual(site_tasks.clear_tasks(self.settings, [3, 4]), 9)
        self.assertEqual(post.call_args.kwargs["data"]["status"], "3,4")

    def test_an_out_of_range_status_is_dropped(self) -> None:
        with self.assertRaises(site_tasks.TaskAdminError):
            site_tasks.clear_tasks(self.settings, [0, 99])


class RoleTests(unittest.TestCase):
    def test_only_a_gateway_node_may_delete(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertTrue(settings_in(root, "all").has_role("gateway"))
            self.assertTrue(settings_in(root, "gateway").has_role("gateway"))
            self.assertFalse(settings_in(root, "worker").has_role("gateway"))


if __name__ == "__main__":
    unittest.main()
