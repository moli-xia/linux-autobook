"""Tests for deleting delivered files once their share has expired."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from autobook_linux.drive_cleanup import (
    DEFAULT_GRACE_DAYS, DriveCleaner, DriveCleanupError, file_age_reference, parse_timestamp,
)


def iso(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.astimezone(timezone(timedelta(hours=8))).isoformat()


def entry(name: str, days_ago: float, size: int = 1024, kind: int = 0) -> dict:
    return {
        "name": name,
        "path": f"cloudreve://my/transfer/{name}",
        "size": size,
        "type": kind,
        "created_at": iso(days_ago),
        "updated_at": iso(days_ago + 30),   # deliberately older; must be ignored
    }


class TimestampTests(unittest.TestCase):
    def test_offset_form_is_parsed(self) -> None:
        parsed = parse_timestamp("2026-08-24T20:27:02+08:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.hour, 12)

    def test_zulu_form_is_parsed(self) -> None:
        self.assertIsNotNone(parse_timestamp("2026-08-24T12:27:02Z"))

    def test_rubbish_returns_none(self) -> None:
        for value in ("", "not a date", None):
            self.assertIsNone(parse_timestamp(value))

    def test_creation_time_is_the_reference_not_modification(self) -> None:
        # updated_at carries the source file's mtime and can predate the upload
        # by years, which would delete a fresh delivery.
        row = entry("book.pdf", days_ago=1)
        reference = file_age_reference(row)
        self.assertIsNotNone(reference)
        age = datetime.now(timezone.utc) - reference
        self.assertLess(age.days, 2)


class FakeCleaner(DriveCleaner):
    """A cleaner whose network calls are replaced by scripted data."""

    def __init__(self, files, **kwargs):
        super().__init__(
            base_url="https://drive.test", email="a@b.c", password="pw",
            target_dir="transfer", expire_days=kwargs.pop("expire_days", 7),
            grace_days=kwargs.pop("grace_days", DEFAULT_GRACE_DAYS),
        )
        self._files = files
        self.deleted: list[str] = []
        self.fail_delete = kwargs.pop("fail_delete", False)

    def login(self):
        self._token = "t"

    def iter_files(self):
        for item in self._files:
            if int(item.get("type", 0)) == 1:
                continue
            yield item

    def delete(self, uris):
        if self.fail_delete:
            raise DriveCleanupError("boom")
        self.deleted.extend(uris)


class SelectionTests(unittest.TestCase):
    def test_only_files_past_share_expiry_plus_grace_are_removed(self) -> None:
        cleaner = FakeCleaner([
            entry("fresh.pdf", days_ago=1),
            entry("still-shared.pdf", days_ago=6),
            entry("just-expired.pdf", days_ago=7.5),      # inside the grace day
            entry("long-expired.pdf", days_ago=30),
        ], expire_days=7)
        result = cleaner.run(dry_run=False)
        names = [uri.rsplit("/", 1)[-1] for uri in cleaner.deleted]
        self.assertEqual(names, ["long-expired.pdf"])
        self.assertEqual(result.scanned, 4)
        self.assertEqual(result.deleted, 1)

    def test_grace_period_can_be_disabled(self) -> None:
        cleaner = FakeCleaner([entry("just-expired.pdf", days_ago=7.5)],
                              expire_days=7, grace_days=0)
        cleaner.run(dry_run=False)
        self.assertEqual(len(cleaner.deleted), 1)

    def test_directories_are_never_touched(self) -> None:
        cleaner = FakeCleaner([
            entry("old-folder", days_ago=90, kind=1),
            entry("old.pdf", days_ago=90),
        ])
        cleaner.run(dry_run=False)
        self.assertEqual([u.rsplit("/", 1)[-1] for u in cleaner.deleted], ["old.pdf"])

    def test_entries_without_a_timestamp_are_left_alone(self) -> None:
        broken = entry("mystery.pdf", days_ago=90)
        broken["created_at"] = ""
        cleaner = FakeCleaner([broken])
        result = cleaner.run(dry_run=False)
        self.assertEqual(cleaner.deleted, [])
        self.assertEqual(result.expired, 0)

    def test_expire_days_is_honoured(self) -> None:
        files = [entry("d10.pdf", days_ago=10)]
        self.assertEqual(len(FakeCleaner(files, expire_days=7).run(dry_run=False).samples), 1)
        # With a 30-day share the same file is still live.
        self.assertEqual(FakeCleaner(files, expire_days=30).run(dry_run=False).deleted, 0)


class DryRunTests(unittest.TestCase):
    def test_dry_run_reports_but_deletes_nothing(self) -> None:
        cleaner = FakeCleaner([entry("old.pdf", days_ago=90, size=5 * 1024 * 1024)])
        result = cleaner.run(dry_run=True)
        self.assertEqual(cleaner.deleted, [])
        self.assertEqual(result.deleted, 1)
        self.assertEqual(result.freed_bytes, 5 * 1024 * 1024)
        self.assertIn("可清理", result.summary())

    def test_limit_caps_the_work(self) -> None:
        cleaner = FakeCleaner([entry(f"old{i}.pdf", days_ago=90) for i in range(10)])
        result = cleaner.run(dry_run=True, limit=3)
        self.assertEqual(result.expired, 3)


class FailureTests(unittest.TestCase):
    def test_a_failed_batch_is_recorded_not_raised(self) -> None:
        # A peer node may have deleted the same expired file first.
        cleaner = FakeCleaner([entry("old.pdf", days_ago=90)], fail_delete=True)
        result = cleaner.run(dry_run=False)
        self.assertEqual(result.deleted, 0)
        self.assertTrue(result.errors)

    def test_missing_credentials_are_rejected(self) -> None:
        cleaner = DriveCleaner("", "", "", "transfer", 7)
        with self.assertRaises(DriveCleanupError):
            cleaner.run(dry_run=True)


class UriTests(unittest.TestCase):
    def test_target_directory_shapes_the_uri(self) -> None:
        self.assertEqual(DriveCleaner("https://d", "a", "b", "transfer", 7).uri,
                         "cloudreve://my/transfer")
        self.assertEqual(DriveCleaner("https://d", "a", "b", "/transfer/", 7).uri,
                         "cloudreve://my/transfer")
        self.assertEqual(DriveCleaner("https://d", "a", "b", "", 7).uri, "cloudreve://my")


if __name__ == "__main__":
    unittest.main()
