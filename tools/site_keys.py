"""Extract the identifiers of every book already on the site.

Runs ON the website server.  The site holds millions of rows on a small box, so
this never materialises the whole result set: it walks the primary key in
bounded ranges, streams each batch with ``mysql --quick`` and writes one key per
line.  Between batches it pauses, and it stops early if the load average climbs,
because a careless full-table export is what took the site down once already.

    python3 tools/site_keys.py --out /root/site_keys.txt

Titles look like ``《书名》_作者_SS号_ISBN``; the SS number and the ISBN are the
last two underscore-separated fields, with the ISBN sometimes absent.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.catalogue_parse import ISBN10_RE, ISBN_RE, SS_RE, isbn13_of, normalise_title  # noqa: E402

DEFAULT_BATCH = 50_000
LOAD_CEILING = 6.0


def load_average() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def keys_from_title(title: str) -> tuple[str, str, str]:
    """Return (ss, isbn13, title_key) for one site title."""
    fields = title.split("_")
    ss = ""
    isbn = ""
    # Preferred: positional, because the site writes 《书名》_作者_SS_ISBN.
    if len(fields) >= 3:
        candidate_ss = fields[-2].strip()
        candidate_isbn = fields[-1].strip()
        if SS_RE.fullmatch(candidate_ss):
            ss = candidate_ss
        if candidate_isbn:
            isbn = isbn13_of(candidate_isbn)
    # Fall back to scanning when the layout differs.
    if not ss:
        masked = ISBN_RE.sub(lambda m: " " * len(m.group(0)), title)
        hit = SS_RE.search(masked)
        ss = hit.group(1) if hit else ""
    if not isbn:
        hit = ISBN_RE.search(title) or ISBN10_RE.search(title)
        isbn = isbn13_of(hit.group(1)) if hit else ""
    head = fields[0] if fields else title
    return ss, isbn, normalise_title(head)


def run_batch(args, low: int, high: int) -> list[str]:
    command = [
        "mysql", "--quick", f"-u{args.user}", f"-p{args.password}", args.database,
        "-N", "-B", "-e",
        f"SELECT title FROM {args.table} WHERE id BETWEEN {low} AND {high}",
    ]
    result = subprocess.run(command, capture_output=True, timeout=args.timeout)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace")
        if "Using a password" not in message or result.returncode != 0:
            raise RuntimeError(f"批次 {low}-{high} 查询失败: {message.strip()[:200]}")
    return result.stdout.decode("utf-8", "replace").splitlines()


def main() -> int:
    parser = argparse.ArgumentParser(description="导出站点已收录图书的标识")
    parser.add_argument("--out", default="/root/site_keys.txt")
    parser.add_argument("--database", default="544544_xyz")
    parser.add_argument("--user", default="544544_xyz")
    parser.add_argument("--password", default="251024")
    parser.add_argument("--table", default="le_cms_article")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--pause", type=float, default=0.4, help="批次之间的休眠秒数")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-id", type=int, default=0)
    parser.add_argument("--start-id", type=int, default=1)
    args = parser.parse_args()

    max_id = args.max_id
    if not max_id:
        probe = subprocess.run(
            ["mysql", f"-u{args.user}", f"-p{args.password}", args.database, "-N", "-B",
             "-e", f"SELECT MAX(id) FROM {args.table}"],
            capture_output=True, timeout=60,
        )
        max_id = int(probe.stdout.decode().strip() or 0)
    print(f"主键上限 {max_id:,}，批大小 {args.batch:,}，输出 {args.out}", flush=True)

    out_path = Path(args.out)
    written = rows = 0
    started = time.time()
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        low = args.start_id
        batch_index = 0
        while low <= max_id:
            high = min(low + args.batch - 1, max_id)
            batch_index += 1
            current_load = load_average()
            if current_load > LOAD_CEILING:
                print(f"  负载 {current_load:.2f} 偏高，暂停 20 秒", flush=True)
                time.sleep(20)
            for title in run_batch(args, low, high):
                if not title:
                    continue
                rows += 1
                ss, isbn, title_key = keys_from_title(title)
                if ss:
                    handle.write(f"ss:{ss}\n")
                    written += 1
                if isbn:
                    handle.write(f"isbn:{isbn}\n")
                    written += 1
                if title_key:
                    handle.write(f"title:{title_key}\n")
                    written += 1
            if batch_index % 20 == 0:
                elapsed = time.time() - started
                percent = high / max_id * 100
                print(f"  {high:>9,}/{max_id:,} ({percent:5.1f}%) 行={rows:,} 键={written:,} "
                      f"负载={load_average():.2f} 用时={elapsed/60:.1f}分", flush=True)
            low = high + 1
            time.sleep(args.pause)

    print(f"完成：扫描 {rows:,} 行，写出 {written:,} 个键，用时 {(time.time()-started)/60:.1f} 分钟", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
