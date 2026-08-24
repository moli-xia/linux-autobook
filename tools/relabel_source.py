"""Rewrite the 【来源】 label on already-imported books.

    python3 tools/relabel_source.py --db-name X --db-user Y --db-pass Z          # preview
    python3 tools/relabel_source.py --db-name X --db-user Y --db-pass Z --execute

The site's article table holds millions of rows on a small box, so this never
runs ``WHERE intro LIKE '%…%'`` — that is a full table scan and it takes the
site down.  Work is done in primary-key ranges, one small batch at a time, and
pauses whenever the load average climbs.  REPLACE leaves rows without the old
label untouched, so a slightly wider range costs nothing.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.import_books import LEGACY_SOURCE_LABEL, SOURCE_LABEL  # noqa: E402

BATCH = 10_000
PAUSE_SECONDS = 0.4
LOAD_CEILING = 4.0
LOAD_WAIT = 5


def load_average() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def mysql(args, sql: str) -> str:
    """Run one statement, feeding it on stdin so long SQL cannot blow argv."""
    command = ["mysql", f"-u{args.db_user}", f"-p{args.db_pass}", args.db_name,
               "--quick", "-N", "--default-character-set=utf8mb4"]
    result = subprocess.run(command, input=sql, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:400])
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧的来源标签改成新的")
    parser.add_argument("--db-name", required=True)
    parser.add_argument("--db-user", required=True)
    parser.add_argument("--db-pass", required=True)
    parser.add_argument("--table", default="le_cms_article")
    parser.add_argument("--column", default="intro")
    parser.add_argument("--old", default=LEGACY_SOURCE_LABEL)
    parser.add_argument("--new", default=SOURCE_LABEL)
    parser.add_argument("--min-id", type=int, help="起始主键，缺省取全表最小值")
    parser.add_argument("--max-id", type=int, help="结束主键，缺省取全表最大值")
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--execute", action="store_true", help="真正写入；缺省只预览")
    args = parser.parse_args()

    bounds = mysql(args, f"SELECT MIN(id), MAX(id) FROM {args.table}").split()
    low = args.min_id if args.min_id is not None else int(bounds[0])
    high = args.max_id if args.max_id is not None else int(bounds[1])
    print(f"表 {args.table}.{args.column}  主键区间 {low}..{high}")
    print(f"「{args.old}」 -> 「{args.new}」")
    if not args.execute:
        print("（预览模式，不会写入任何数据；加 --execute 才会真正修改）")

    changed = 0
    batches = 0
    started = time.time()
    cursor = low
    while cursor <= high:
        upper = min(cursor + args.batch - 1, high)
        while load_average() > LOAD_CEILING:
            print(f"  负载 {load_average():.2f} 偏高，等待 {LOAD_WAIT}s…")
            time.sleep(LOAD_WAIT)
        if args.execute:
            # ROW_COUNT only survives inside the connection that ran the UPDATE,
            # so both statements have to go in the same invocation.
            output = mysql(args, (
                f"UPDATE {args.table} SET {args.column}="
                f"REPLACE({args.column},'{args.old}','{args.new}') "
                f"WHERE id BETWEEN {cursor} AND {upper}; SELECT ROW_COUNT();"
            ))
            # Counts only rows whose value really changed.
            hits = int((output.splitlines() or ["0"])[-1].strip() or 0)
        else:
            hits = int(mysql(args, (
                f"SELECT COUNT(*) FROM {args.table} "
                f"WHERE id BETWEEN {cursor} AND {upper} "
                f"AND {args.column} LIKE '%{args.old}%'"
            )) or 0)
        changed += hits
        batches += 1
        if hits:
            print(f"  {cursor}..{upper}: {hits} 行")
        cursor = upper + 1
        time.sleep(PAUSE_SECONDS)

    verb = "已修改" if args.execute else "待修改"
    print(f"\n{verb} {changed} 行，共 {batches} 批，用时 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
