"""Summarise what the downloaded catalogues contain.

Run on the gateway node:
    python tools/catalogue_stats.py /opt/autobook-linux/runtime/catalogues
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.catalogue_parse import parse_file  # noqa: E402


def main() -> int:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/autobook-linux/runtime/catalogues")
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".txt")
    if not files:
        print(f"没有找到书表文件: {folder}")
        return 1

    grand_total = 0
    suffixes: Counter[str] = Counter()
    id_kinds: Counter[str] = Counter()
    unique_keys: set[str] = set()

    print(f"{'书表':38} {'条目':>9} {'有SS':>9} {'仅ISBN':>9} {'无标识':>9}")
    print("-" * 78)
    for path in files:
        total = with_ss = isbn_only = bare = 0
        for record in parse_file(path):
            total += 1
            suffixes[record.suffix] += 1
            unique_keys.add(record.key())
            if record.ss:
                with_ss += 1
                id_kinds["有SS号"] += 1
            elif record.isbn:
                isbn_only += 1
                id_kinds["仅ISBN"] += 1
            else:
                bare += 1
                id_kinds["仅书名"] += 1
        grand_total += total
        print(f"{path.name[:38]:38} {total:9,} {with_ss:9,} {isbn_only:9,} {bare:9,}")

    print("-" * 78)
    print(f"{'合计':38} {grand_total:9,}")
    print(f"\n去重后唯一书目: {len(unique_keys):,}")
    print("\n标识分布:")
    for kind, count in id_kinds.most_common():
        share = count / grand_total * 100 if grand_total else 0
        print(f"  {kind:10} {count:9,}  ({share:5.1f}%)")
    print("\n文件类型分布:")
    for suffix, count in suffixes.most_common(12):
        share = count / grand_total * 100 if grand_total else 0
        print(f"  {suffix:8} {count:9,}  ({share:5.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
