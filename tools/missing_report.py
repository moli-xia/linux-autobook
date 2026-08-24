"""Inspect the missing-book list before anything is written to the site.

Importing straight from a catalogue would drag in rows whose "title" is only an
ISBN, a bare number, or leftover advertising.  This report shows what would be
imported so the filtering rules can be judged on real data.

    python tools/missing_report.py runtime/missing_books.tsv
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CJK_RE = re.compile(r"[一-鿿]")
LETTER_RE = re.compile(r"[A-Za-z]")

COLUMNS = ("key", "rank", "title", "ss", "isbn", "suffix", "filename", "path", "source")


def read_rows(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(COLUMNS):
                parts += [""] * (len(COLUMNS) - len(parts))
            yield dict(zip(COLUMNS, parts))


def title_quality(row) -> str:
    """Classify how usable a row's title is."""
    title = (row["title"] or "").strip()
    if not title:
        return "空标题"
    compact = re.sub(r"[\s_\-—–.]+", "", title)
    if not compact:
        return "空标题"
    if compact.isdigit():
        return "纯数字"
    if len(compact) <= 2:
        return "过短"
    if not CJK_RE.search(title) and not LETTER_RE.search(title):
        return "无文字"
    return "可用"


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "runtime/missing_books.tsv")
    quality = Counter()
    by_kind = Counter()
    suffix_by_quality = Counter()
    samples: dict[str, list] = {}
    total = 0

    for row in read_rows(path):
        total += 1
        grade = title_quality(row)
        kind = row["key"].split(":", 1)[0]
        quality[grade] += 1
        by_kind[(kind, grade)] += 1
        suffix_by_quality[(grade, row["suffix"])] += 1
        bucket = samples.setdefault(f"{kind}/{grade}", [])
        if len(bucket) < 4:
            bucket.append(row)

    print(f"缺失条目总计: {total:,}\n")
    print("标题质量:")
    for grade, count in quality.most_common():
        print(f"  {grade:8} {count:8,}  ({count/total*100:5.1f}%)")

    print("\n按标识类型 × 质量:")
    for (kind, grade), count in sorted(by_kind.items(), key=lambda item: -item[1]):
        print(f"  {kind:6} {grade:8} {count:8,}")

    print("\n可用条目的文件类型:")
    usable = [(suffix, count) for (grade, suffix), count in suffix_by_quality.items() if grade == "可用"]
    for suffix, count in sorted(usable, key=lambda item: -item[1])[:12]:
        print(f"  {suffix:8} {count:8,}")

    print("\n样本:")
    for label in sorted(samples):
        print(f"\n  [{label}]")
        for row in samples[label]:
            print(f"    标题={row['title'][:52]!r} ss={row['ss']} isbn={row['isbn']} "
                  f"{row['suffix']} 来源={row['source'][:18]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
