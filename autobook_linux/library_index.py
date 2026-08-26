"""Small local cache for Baidu group-library search results.

Normal task lookup uses Baidu's server-side group search.  The legacy crawler
is retained only as an explicit maintenance/debugging operation; task workers
never start a recursive million-file sync after a cache miss.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from autobook_linux.archive import ARCHIVE_SUFFIXES
from autobook_linux.baidu_pan import BaiduPanClient, GroupShareFile
from autobook_linux.lookup import coverage, title_matches

LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    gid TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    from_uk TEXT NOT NULL,
    fs_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    is_dir INTEGER NOT NULL DEFAULT 0,
    server_mtime INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (gid, msg_id, fs_id)
);
CREATE INDEX IF NOT EXISTS idx_files_name ON files (gid, name);
CREATE TABLE IF NOT EXISTS sync_state (
    gid TEXT PRIMARY KEY,
    newest_msg_id TEXT,
    full_synced INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
"""


# Formats a reader can open directly, best first; archives still need work.
READABLE_SUFFIXES = (".pdf", ".epub", ".azw3", ".mobi", ".djvu")


def pick_best_file(
    candidates: list[GroupShareFile], needle: str, kind: str = "ss"
) -> GroupShareFile | None:
    """Choose a deterministic best match without requiring a local index.

    For an SS number the filename usually *is* the number, so an exact stem
    match wins.  For an ISBN or a title the number is only part of the name, so
    containment is what counts and the tie is broken by format then recency.
    """
    if not candidates:
        return None
    needle_lower = (needle or "").lower()

    def score(item: GroupShareFile) -> tuple[int, ...]:
        name = item.name
        stem = name.rsplit(".", 1)[0] if "." in name else name
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if suffix == ".pdf":
            type_rank = 0
        elif suffix in READABLE_SUFFIXES:
            type_rank = 1
        elif suffix in ARCHIVE_SUFFIXES:
            type_rank = 2
        else:
            type_rank = 3
        if kind == "ss":
            # Every candidate already matches the SS number, so prefer a file
            # that needs no unpacking over one merely named after the code.
            exact_rank = 0 if stem == needle else 1
            return (type_rank, exact_rank, -item.server_mtime)
        if kind == "title":
            # The query that found these may have been a shortened form of the
            # title, so rank on how much of the *whole* title each name carries.
            match_rank = 0 if title_matches(name, needle) else 1
            return (match_rank, -int(coverage(name, needle) * 1000), type_rank, -item.server_mtime)
        # An ISBN search can return unrelated files, so a name that actually
        # contains the needle outranks a convenient format.
        match_rank = 0 if needle_lower in name.lower() else 1
        return (match_rank, type_rank, -item.server_mtime)

    best = sorted(candidates, key=score)[0]
    if kind == "title":
        # Never hand back a book that merely shares an author or a word.
        return best if title_matches(best.name, needle) else None
    if kind != "ss" and needle_lower not in best.name.lower():
        return None
    return best


class LibraryIndex:
    def __init__(self, db_path: Path, client: BaiduPanClient, full_sync_max_pages: int = 2000) -> None:
        self.db_path = db_path
        self.client = client
        self.full_sync_max_pages = full_sync_max_pages
        self._lock = threading.Lock()  # serialize syncs across worker threads
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    def search(self, gid: str, ssno: str) -> list[GroupShareFile]:
        """Find files with Baidu's fast server-side search and cache metadata."""
        try:
            rows = self.client.search_group_files(gid, ssno)
        except Exception:
            cached = self._query(gid, ssno)
            if cached:
                LOGGER.exception("群文件库服务端搜索失败，使用本地缓存 SS=%s", ssno)
                return cached
            raise
        if rows:
            self._store_search_results(rows)
        return rows

    def _store_search_results(self, rows: list[GroupShareFile]) -> None:
        for item in rows:
            self._conn.execute(
                "INSERT INTO files (gid, msg_id, from_uk, fs_id, name, path, size, is_dir, server_mtime) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(gid, msg_id, fs_id) DO UPDATE SET "
                "from_uk=excluded.from_uk, name=excluded.name, path=excluded.path, "
                "size=excluded.size, is_dir=excluded.is_dir, server_mtime=excluded.server_mtime",
                (
                    item.gid,
                    item.msg_id,
                    item.from_uk,
                    item.fs_id,
                    item.name,
                    item.path,
                    item.size,
                    1 if item.is_dir else 0,
                    item.server_mtime,
                ),
            )
        self._conn.commit()

    def _query(self, gid: str, ssno: str) -> list[GroupShareFile]:
        cursor = self._conn.execute(
            "SELECT msg_id, from_uk, fs_id, name, path, size, is_dir, server_mtime "
            "FROM files WHERE gid=? AND is_dir=0 AND name LIKE ? ORDER BY server_mtime DESC",
            (gid, f"%{ssno}%"),
        )
        return [
            GroupShareFile(
                gid=gid,
                msg_id=row[0],
                from_uk=row[1],
                fs_id=int(row[2]),
                name=row[3],
                path=row[4],
                size=int(row[5]),
                is_dir=bool(row[6]),
                server_mtime=int(row[7]),
            )
            for row in cursor.fetchall()
        ]

    # ------------------------------------------------------------------
    def sync(self, gid: str, incremental: bool = True) -> int:
        with self._lock:
            state = self._conn.execute(
                "SELECT newest_msg_id, full_synced FROM sync_state WHERE gid=?", (gid,)
            ).fetchone()
            stop_at_msg_id = None
            if incremental and state and state[0]:
                stop_at_msg_id = state[0]

            inserted = 0
            newest_seen: str | None = None
            done = False
            pages = 0
            for page, msg_list in self.client.iter_group_shares(gid):
                pages += 1
                for msg in msg_list:
                    msg_id = str(msg.get("msg_id"))
                    if newest_seen is None:
                        newest_seen = msg_id
                    if stop_at_msg_id and msg_id == stop_at_msg_id:
                        done = True
                        break
                    inserted += self._store_message(gid, msg)
                self._conn.commit()
                if done or pages >= self.full_sync_max_pages:
                    if pages >= self.full_sync_max_pages:
                        LOGGER.warning("同步达到页数上限 %d，停止", self.full_sync_max_pages)
                    break

            if incremental and stop_at_msg_id:
                full_synced = state[1] if state else 0
            else:
                full_synced = 1
            self._conn.execute(
                "INSERT INTO sync_state (gid, newest_msg_id, full_synced, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(gid) DO UPDATE SET newest_msg_id=excluded.newest_msg_id, "
                "full_synced=excluded.full_synced, updated_at=excluded.updated_at",
                (gid, newest_seen or (state[0] if state else None), full_synced, int(time.time())),
            )
            self._conn.commit()
            LOGGER.info("群库索引同步完成 gid=%s 写入 %d 条 (pages=%d)", gid, inserted, pages)
            return inserted

    def _store_message(self, gid: str, msg: dict[str, Any]) -> int:
        msg_id = str(msg.get("msg_id"))
        from_uk = str(msg.get("uk"))
        count = 0
        pending = deque(msg.get("file_list") or [])
        seen_fs_ids: set[int] = set()
        while pending:
            file_item = pending.popleft()
            try:
                fs_id = int(file_item.get("fs_id"))
                if fs_id in seen_fs_ids:
                    continue
                seen_fs_ids.add(fs_id)
                is_dir = str(file_item.get("isdir")) == "1"
                self._conn.execute(
                    "INSERT INTO files (gid, msg_id, from_uk, fs_id, name, path, size, is_dir, server_mtime) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(gid, msg_id, fs_id) DO UPDATE SET "
                    "name=excluded.name, path=excluded.path, size=excluded.size, "
                    "is_dir=excluded.is_dir, server_mtime=excluded.server_mtime",
                    (
                        gid,
                        msg_id,
                        from_uk,
                        fs_id,
                        str(file_item.get("server_filename", "")),
                        str(file_item.get("path", "")),
                        int(file_item.get("size") or 0),
                        1 if is_dir else 0,
                        int(file_item.get("server_mtime") or 0),
                    ),
                )
                count += 1
                if is_dir:
                    for children in self.client.iter_shareinfo_pages(
                        gid,
                        msg_id,
                        from_uk,
                        fs_id,
                    ):
                        pending.extend(children)
            except (TypeError, ValueError):
                continue
        return count

    # ------------------------------------------------------------------
    def pick_best(self, gid: str, needle: str, kind: str = "ss") -> GroupShareFile | None:
        """Choose the best matching file for an SS code, ISBN or title.

        Preference: a matching name first, then PDF > other readable formats >
        archive, then newest first.  This avoids downloading and converting an
        archive when the group search already returned a ready-to-use file.
        """
        return pick_best_file(self.search(gid, needle), needle, kind)
