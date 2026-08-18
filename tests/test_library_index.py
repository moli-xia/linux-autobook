from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

LINUX_ROOT = Path(__file__).resolve().parents[1]
if str(LINUX_ROOT) not in sys.path:
    sys.path.insert(0, str(LINUX_ROOT))

from autobook_linux.baidu_pan import GroupShareFile
from autobook_linux.library_index import LibraryIndex


def _item(fs_id: int, name: str, is_dir: bool, path: str) -> dict:
    return {
        "fs_id": fs_id,
        "server_filename": name,
        "path": path,
        "size": 0 if is_dir else 100,
        "isdir": 1 if is_dir else 0,
        "server_mtime": fs_id,
    }


class _RecursiveGroupClient:
    def iter_group_shares(self, _gid: str):
        yield 1, [
            {
                "msg_id": "message-1",
                "uk": "owner-1",
                "file_list": [
                    _item(1, "root", True, "/root"),
                    _item(2, "root-book.pdf", False, "/root-book.pdf"),
                ],
            }
        ]

    def iter_shareinfo_pages(self, _gid: str, _msg_id: str, _from_uk: str, fs_id: int):
        if fs_id == 1:
            yield [
                _item(3, "nested", True, "/root/nested"),
                _item(4, "12345678.zip", False, "/root/12345678.zip"),
            ]
        elif fs_id == 3:
            yield [_item(5, "deep-book.pdf", False, "/root/nested/deep-book.pdf")]


class _ServerSearchClient:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, str]] = []

    def search_group_files(self, gid: str, keyword: str) -> list[GroupShareFile]:
        self.search_calls.append((gid, keyword))
        return [
            GroupShareFile(
                gid=gid,
                msg_id="message-2",
                from_uk="owner-2",
                fs_id=10,
                name=f"{keyword}_book.pdf",
                path=f"/{keyword}_book.pdf",
                size=200,
                is_dir=False,
                server_mtime=100,
                dlink="https://example.invalid/book.pdf",
            ),
            GroupShareFile(
                gid=gid,
                msg_id="message-3",
                from_uk="owner-2",
                fs_id=11,
                name=f"{keyword}.zip",
                path=f"/{keyword}.zip",
                size=300,
                is_dir=False,
                server_mtime=200,
                dlink="https://example.invalid/book.zip",
            ),
        ]

    def iter_group_shares(self, _gid: str):
        raise AssertionError("normal search must not crawl the group library")


class LibraryIndexTests(unittest.TestCase):
    def test_sync_recursively_indexes_nested_group_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = LibraryIndex(Path(tmp) / "index.sqlite3", _RecursiveGroupClient())
            inserted = index.sync("gid", incremental=False)
            matches = index._query("gid", "12345678")
            total = index._conn.execute("SELECT count(*) FROM files").fetchone()[0]
            non_dirs = index._conn.execute(
                "SELECT count(*) FROM files WHERE is_dir=0"
            ).fetchone()[0]
            index._conn.close()

        self.assertEqual(inserted, 5)
        self.assertEqual(total, 5)
        self.assertEqual(non_dirs, 3)
        self.assertEqual([item.name for item in matches], ["12345678.zip"])

    def test_search_uses_server_endpoint_without_recursive_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _ServerSearchClient()
            index = LibraryIndex(Path(tmp) / "index.sqlite3", client)
            matches = index.search("498636198303058255", "12607753")
            cached = index._query("498636198303058255", "12607753")
            index._conn.close()

        self.assertEqual(client.search_calls, [("498636198303058255", "12607753")])
        self.assertEqual([item.name for item in matches], ["12607753_book.pdf", "12607753.zip"])
        self.assertEqual([item.name for item in cached], ["12607753.zip", "12607753_book.pdf"])

    def test_pick_best_prefers_ready_pdf_over_exact_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _ServerSearchClient()
            index = LibraryIndex(Path(tmp) / "index.sqlite3", client)
            item = index.pick_best("498636198303058255", "12607753")
            index._conn.close()

        self.assertIsNotNone(item)
        self.assertEqual(item.name, "12607753_book.pdf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
