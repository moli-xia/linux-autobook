"""Export every book the netdisk actually holds, one row per unique book.

Emits TSV so the comparison against the site can be done with sorted files and
``comm``/``join`` instead of multi-hundred-megabyte in-memory sets:

    key <TAB> title <TAB> ss <TAB> isbn <TAB> suffix <TAB> filename <TAB> path <TAB> source

Run on the gateway node, then de-duplicate by key with an external sort:

    python tools/catalogue_export.py runtime/catalogues > /tmp/netdisk_all.tsv
    LC_ALL=C sort -k1,1 -k2,2n -t$'\\t' /tmp/netdisk_all.tsv \\
      | awk -F'\\t' '$1!=p{print;p=$1}' > /tmp/netdisk_books.tsv

Sorting by key and then by format rank, and keeping the first row of each key
run, picks the best available format for every book with constant memory.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.catalogue_parse import parse_file  # noqa: E402

# Prefer a directly usable book over an archive that still needs unpacking, and
# prefer a real page scan over a text-only rendering.
SUFFIX_RANK = {
    ".pdf": 0, ".epub": 1, ".azw3": 2, ".mobi": 3, ".djvu": 4,
    ".uvz": 5, ".zip": 6, ".rar": 7, ".7z": 8, ".cbz": 9,
    ".tar": 10, ".gz": 11, ".caj": 12, ".txt": 13,
}


def clean(value: str) -> str:
    """TSV-safe single-line field."""
    return (value or "").replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def main() -> int:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/autobook-linux/runtime/catalogues")
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".txt")
    if not files:
        print(f"没有找到书表文件: {folder}", file=sys.stderr)
        return 1

    written = 0
    out = sys.stdout
    for path in files:
        for record in parse_file(path):
            rank = SUFFIX_RANK.get(record.suffix, 99)
            # The rank leads the sort key so `sort -u -k1,1` keeps the best
            # format for each book while still de-duplicating on identity.
            out.write(
                f"{record.key()}\t{rank}\t{clean(record.title)}\t{record.ss}\t{record.isbn}\t"
                f"{record.suffix}\t{clean(record.filename)}\t{clean(record.path)}\t{record.source}\n"
            )
            written += 1
        print(f"  已处理 {path.name}（累计 {written:,} 行）", file=sys.stderr, flush=True)
    print(f"共写出 {written:,} 行", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
