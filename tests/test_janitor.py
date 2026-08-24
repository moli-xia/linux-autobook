"""Tests for the periodic storage cleanup scheduler and the Baidu inbox sweep."""
from __future__ import annotations

import time
import types
import unittest

from autobook_linux import janitor
from autobook_linux.baidu_pan import BaiduPanClient


def config(**overrides):
    values = {
        "cleanup_enabled": True,
        "cleanup_interval_hours": 6,
        "drive_cleanup_grace_days": 1,
        "baidu_inbox_orphan_hours": 6,
        "drive_base_url": "https://drive.test",
        "drive_email": "a@b.c",
        "baidu_save_dir": "/autobook_inbox",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class SchedulerTests(unittest.TestCase):
    def test_every_task_runs(self) -> None:
        calls = []
        keeper = janitor.Janitor(
            {"a": lambda: calls.append("a") or "ra", "b": lambda: calls.append("b") or "rb"}, 6)
        results = keeper.run_once()
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(results, {"a": "ra", "b": "rb"})

    def test_one_failing_task_does_not_skip_the_others(self) -> None:
        def boom():
            raise RuntimeError("network down")

        ran = []
        keeper = janitor.Janitor({"broken": boom, "ok": lambda: ran.append(1)}, 6)
        results = keeper.run_once()
        self.assertIn("network down", results["broken"]["error"])
        self.assertEqual(ran, [1])

    def test_results_of_the_last_pass_are_kept(self) -> None:
        keeper = janitor.Janitor({"a": lambda: 42}, 6)
        keeper.run_once()
        self.assertEqual(keeper.last_results, {"a": 42})

    def test_interval_is_at_least_one_hour(self) -> None:
        self.assertEqual(janitor.Janitor({}, 0).interval, 3600)
        self.assertEqual(janitor.Janitor({}, 6).interval, 6 * 3600)

    def test_thread_runs_then_stops(self) -> None:
        calls = []
        keeper = janitor.Janitor({"a": lambda: calls.append(1)}, 6, startup_delay=0)
        keeper.start()
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.02)
        keeper.stop()
        self.assertEqual(calls, [1], "the first pass should run promptly after startup")

    def test_no_tasks_means_no_thread(self) -> None:
        keeper = janitor.Janitor({}, 6)
        keeper.start()
        self.assertIsNone(keeper._thread)


class WiringTests(unittest.TestCase):
    def test_worker_cleans_the_result_drive(self) -> None:
        self.assertEqual(list(janitor.for_worker(config()).tasks), ["结果网盘"])

    def test_gateway_cleans_the_baidu_inbox(self) -> None:
        keeper = janitor.for_gateway(config(), lambda: None)
        self.assertEqual(list(keeper.tasks), ["百度转存目录"])

    def test_disabling_cleanup_removes_every_task(self) -> None:
        self.assertEqual(janitor.for_worker(config(cleanup_enabled=False)).tasks, {})
        self.assertEqual(janitor.for_gateway(config(cleanup_enabled=False), lambda: None).tasks, {})

    def test_unconfigured_drive_is_not_swept(self) -> None:
        self.assertEqual(janitor.for_worker(config(drive_email="")).tasks, {})
        self.assertEqual(janitor.for_worker(config(drive_base_url="")).tasks, {})

    def test_gateway_task_sweeps_with_the_configured_window(self) -> None:
        seen = {}

        class FakeClient:
            def sweep_inbox(self, save_dir, hours):
                seen["args"] = (save_dir, hours)
                return {"deleted": 3}

        keeper = janitor.for_gateway(config(baidu_inbox_orphan_hours=12), FakeClient)
        self.assertEqual(keeper.run_once()["百度转存目录"], {"deleted": 3})
        self.assertEqual(seen["args"], ("/autobook_inbox", 12))


class FakePan(BaiduPanClient):
    """A client whose directory listing and deletes are scripted."""

    def __init__(self, entries):
        self.entries = entries
        self.deleted: list[str] = []

    def list_dir(self, path):
        return self.entries

    accepts_deletes = True

    def delete_own_files(self, paths):
        if not self.accepts_deletes:
            return False
        self.deleted.extend(paths)
        return True


def inbox_entry(name, hours_ago, size=1024, isdir=False):
    return {
        "server_filename": name,
        "path": f"/autobook_inbox/{name}",
        "size": size,
        "isdir": 1 if isdir else 0,
        "server_mtime": int(time.time() - hours_ago * 3600),
    }


class InboxSweepTests(unittest.TestCase):
    def test_only_files_older_than_the_window_are_deleted(self) -> None:
        pan = FakePan([
            inbox_entry("running-now.pdf", hours_ago=0.2),
            inbox_entry("recent.pdf", hours_ago=3),
            inbox_entry("stranded.epub", hours_ago=9),
        ])
        report = pan.sweep_inbox("/autobook_inbox", older_than_hours=6)
        self.assertEqual(pan.deleted, ["/autobook_inbox/stranded.epub"])
        self.assertEqual((report["scanned"], report["deleted"]), (3, 1))

    def test_retry_duplicates_are_reclaimed(self) -> None:
        # ondup=newcopy leaves "name(1).epub" behind when a transfer is retried.
        pan = FakePan([
            inbox_entry("book.epub", hours_ago=9),
            inbox_entry("book(1).epub", hours_ago=9),
            inbox_entry("book(2).epub", hours_ago=9),
        ])
        report = pan.sweep_inbox("/autobook_inbox")
        self.assertEqual(len(pan.deleted), 3)
        self.assertEqual(report["freed_bytes"], 3 * 1024)

    def test_directories_are_never_deleted(self) -> None:
        pan = FakePan([inbox_entry("old-folder", hours_ago=99, isdir=True)])
        pan.sweep_inbox("/autobook_inbox")
        self.assertEqual(pan.deleted, [])

    def test_entries_without_a_timestamp_are_left_alone(self) -> None:
        entry = inbox_entry("mystery.pdf", hours_ago=99)
        entry["server_mtime"] = 0
        pan = FakePan([entry])
        self.assertEqual(pan.sweep_inbox("/autobook_inbox")["deleted"], 0)
        self.assertEqual(pan.deleted, [])

    def test_dry_run_reports_without_deleting(self) -> None:
        pan = FakePan([inbox_entry("stranded.pdf", hours_ago=99, size=2048)])
        report = pan.sweep_inbox("/autobook_inbox", dry_run=True)
        self.assertEqual(pan.deleted, [])
        self.assertEqual(report["deleted"], 1)
        self.assertEqual(report["freed_bytes"], 2048)

    def test_a_listing_failure_is_reported_not_raised(self) -> None:
        class Broken(FakePan):
            def list_dir(self, path):
                raise RuntimeError("errno=-9")

        report = Broken([]).sweep_inbox("/autobook_inbox")
        self.assertIn("errno=-9", report["error"])
        self.assertEqual(report["deleted"], 0)

    def test_a_refused_delete_is_not_counted_as_freed_space(self) -> None:
        # Baidu returns errno=132 when the account needs a security check; the
        # HTTP call still succeeds, so believing it would report phantom
        # cleanups while the directory kept growing.
        pan = FakePan([inbox_entry("stranded.pdf", hours_ago=99, size=4096)])
        pan.accepts_deletes = False
        report = pan.sweep_inbox("/autobook_inbox")
        self.assertEqual(report["deleted"], 0)
        self.assertEqual(report["freed_bytes"], 0)
        self.assertIn("拒绝", report["error"])

    def test_large_sweeps_are_batched(self) -> None:
        pan = FakePan([inbox_entry(f"old{i}.pdf", hours_ago=99) for i in range(120)])
        report = pan.sweep_inbox("/autobook_inbox")
        self.assertEqual(report["deleted"], 120)
        self.assertEqual(len(pan.deleted), 120)


class TransferPickTests(unittest.TestCase):
    def test_the_newest_copy_is_taken_so_our_own_is_the_one_deleted(self) -> None:
        # A peer worker's older "book.epub" may sit in the inbox while our
        # retried transfer landed as "book(1).epub".  Taking the first match
        # would download theirs, delete theirs, and strand ours forever.
        pan = FakePan([
            inbox_entry("book.epub", hours_ago=5),
            inbox_entry("book(1).epub", hours_ago=0.01),
        ])
        for item in pan.entries:
            item["fs_id"] = 1
        chosen = pan.wait_transferred_file("/autobook_inbox", "book")
        self.assertEqual(chosen["server_filename"], "book(1).epub")


if __name__ == "__main__":
    unittest.main()
