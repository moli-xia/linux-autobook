"""Parse Baidu group-library catalogue (书表) files into normalised book records.

The group library ships five different catalogue layouts that all describe the
same thing — which books actually exist in the netdisk:

  1. tree            ``│   ├── 全球新闻传播史_12188662.pdf``
  2. listing         ``书名_9787544299084.epub, /duxiu/…/书名.epub, 0.17, 4260431``
  3. annotated tree  ``3│  │  书名9787121412905_….pdf (3.14 MB)  --  /【…】/9787/``
  4. UTF-16 TSV      ``id \t parent_path \t server_filename``
  5. full paths      ``/我的资源/秀2.0——4.0全集/…/一个人的贵族_12194166.zip``

Filenames carry the identifiers inconsistently: some have an 8-digit 读秀 SS
number, some only an ISBN (10- or 13-digit), some both, some neither.  Several
libraries also append promotional text to every filename, which must not end up
in the book title.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterator

# Extensions that hold a readable book (as opposed to tooling or notes).
BOOK_SUFFIXES = {
    ".pdf", ".epub", ".mobi", ".azw3", ".djvu", ".txt", ".caj",
    ".zip", ".rar", ".7z", ".uvz", ".cbz", ".tar", ".gz",
}
ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".uvz", ".cbz", ".tar", ".gz"}

ISBN_RE = re.compile(r"(?<!\d)(97[89]\d{10})(?!\d)")
# The site stores plenty of pre-2007 ISBN-10 values, including the X check digit.
ISBN10_RE = re.compile(r"(?<![\dXx])(\d{9}[\dXx])(?![\dXx])")
SS_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")

# Tree decoration, optionally preceded by a depth number: "3│  │  ├─".
TREE_PREFIX_RE = re.compile(r"^[\s\d]*[│├└─|`+\-\s]*")
# "name, path, size, fs_id" — the listing format used by 读秀7.0 and friends.
LISTING_RE = re.compile(r"^(?P<name>.+?),\s*(?P<path>/[^,]*),\s*(?P<size>[\d.]+),\s*(?P<fsid>\d+)\s*$")
# "  --  /some/path/" tail on annotated tree rows.
PATH_TAIL_RE = re.compile(r"\s+--\s+(?P<path>/.*)$")
# " (3.14 MB)" size annotation directly after the filename.
SIZE_TAIL_RE = re.compile(r"\s*\((?P<value>[\d.]+)\s*(?P<unit>[KMGT]?B)\)\s*$", re.IGNORECASE)
SEPARATOR_RUN_RE = re.compile(r"[\s_\-—–]+")

# Uploaders staple adverts onto every filename; strip them from the title.
PROMO_PATTERNS = (
    re.compile(r"[_\-\s]*关注更新[【\[].*?[】\]].*$"),
    re.compile(r"[_\-\s]*(?:更多)?书籍?更新请添加微信\S*"),
    re.compile(r"[_\-\s]*关注(?:公众号|微信)\S*"),
    re.compile(r"[_\-\s]*whbhpfc\S*", re.IGNORECASE),
    re.compile(r"[【\[](?:公众号|微信)[】\]]\S*"),
)

SIZE_UNITS = {"B": 1 / 1024 / 1024, "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0, "TB": 1024.0 * 1024}


@dataclass
class BookRecord:
    """One book file as described by a catalogue."""

    filename: str
    title: str
    ss: str = ""
    isbn: str = ""
    suffix: str = ""
    path: str = ""
    size_mb: float = 0.0
    fs_id: str = ""
    source: str = ""
    extra_ss: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_archive(self) -> bool:
        return self.suffix in ARCHIVE_SUFFIXES

    def key(self) -> str:
        """Stable identity used for de-duplication and site matching."""
        if self.ss:
            return f"ss:{self.ss}"
        if self.isbn:
            return f"isbn:{self.isbn}"
        return f"title:{normalise_title(self.title)}"


def isbn13_of(value: str) -> str:
    """Normalise an ISBN-10 or ISBN-13 to ISBN-13, or return '' if it is neither.

    The site holds both generations while netdisk filenames almost always use
    ISBN-13, so everything is compared in ISBN-13 form.
    """
    digits = re.sub(r"[^0-9Xx]", "", value or "").upper()
    if len(digits) == 13 and digits.isdigit() and digits[:3] in ("978", "979"):
        return digits
    # ISBN-10: nine digits plus a check digit that may be X.
    if len(digits) == 10 and digits[:9].isdigit() and (digits[9].isdigit() or digits[9] == "X"):
        body = "978" + digits[:9]
        total = sum((1 if index % 2 == 0 else 3) * int(char) for index, char in enumerate(body))
        return body + str((10 - total % 10) % 10)
    return ""


def strip_promotional(value: str) -> str:
    """Remove uploader adverts that are stapled onto filenames."""
    text = value
    for pattern in PROMO_PATTERNS:
        text = pattern.sub("", text)
    return text.strip(" _-—–.,，、")


def normalise_title(value: str) -> str:
    """Collapse punctuation and spacing so the same book matches itself."""
    text = strip_promotional(value).strip().lower()
    text = text.replace("《", "").replace("》", "").replace("（", "(").replace("）", ")")
    text = text.replace("：", ":").replace("，", ",").replace("　", " ")
    text = re.sub(r"[\s_\-—–.·、,:;()\[\]【】]+", "", text)
    return text


def split_identifiers(stem: str) -> tuple[str, str, str, tuple[str, ...]]:
    """Return (title, ss, isbn13, other_ss) pulled out of a filename stem."""
    cleaned = strip_promotional(stem)

    isbn = ""
    hit = ISBN_RE.search(cleaned)
    if hit:
        isbn = isbn13_of(hit.group(1))
    # Blank the ISBN-13 before hunting for an SS number, so the eight digits
    # inside a 13-digit ISBN can never be read as one.
    masked = ISBN_RE.sub(lambda m: " " * len(m.group(0)), cleaned)
    ss_hits = SS_RE.findall(masked)
    ss = ss_hits[0] if ss_hits else ""

    if not isbn:
        # Only look for a 10-digit ISBN once the SS numbers are out of the way,
        # otherwise an 8-digit SS beside two digits can masquerade as one.
        without_ss = masked
        for value in ss_hits:
            without_ss = without_ss.replace(value, " ")
        hit10 = ISBN10_RE.search(without_ss)
        if hit10:
            isbn = isbn13_of(hit10.group(1))

    title = masked
    for value in ss_hits:
        title = title.replace(value, " ")
    title = SEPARATOR_RUN_RE.sub(" ", title).strip(" _-—–.,，、")
    return title, ss, isbn, tuple(ss_hits[1:])


def _size_to_mb(value: str, unit: str) -> float:
    try:
        return float(value) * SIZE_UNITS.get(unit.upper(), 1.0)
    except (TypeError, ValueError):
        return 0.0


def parse_line(line: str, source: str = "") -> BookRecord | None:
    """Turn one catalogue line into a record, or None when it is not a book."""
    raw = line.rstrip("\n\r").replace("﻿", "")
    if not raw.strip():
        return None

    path = ""
    size_mb = 0.0
    fs_id = ""

    listing = LISTING_RE.match(raw.strip())
    if listing:
        name = listing.group("name").strip()
        path = listing.group("path").strip()
        fs_id = listing.group("fsid")
        try:
            size_mb = float(listing.group("size"))
        except ValueError:
            size_mb = 0.0
    else:
        working = raw
        # UTF-16 TSV rows: id \t parent_path \t server_filename
        if "\t" in working:
            columns = [column.strip() for column in working.split("\t") if column.strip()]
            if columns:
                if len(columns) >= 3 and columns[-2].startswith("/"):
                    path = columns[-2]
                working = columns[-1]
        # Annotated tree rows carry the directory after "  --  ".
        tail = PATH_TAIL_RE.search(working)
        if tail:
            path = tail.group("path").strip()
            working = working[: tail.start()]
        working = TREE_PREFIX_RE.sub("", working).strip()
        size_hit = SIZE_TAIL_RE.search(working)
        if size_hit:
            size_mb = _size_to_mb(size_hit.group("value"), size_hit.group("unit"))
            working = working[: size_hit.start()].strip()
        name = working

    if not name or name.endswith("/"):
        return None
    # Full-path rows: keep only the basename, remember the directory.
    if "/" in name:
        pure = PurePosixPath(name)
        if not path:
            path = str(pure.parent)
        name = pure.name
    name = name.strip().strip(",")
    if not name:
        return None

    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in BOOK_SUFFIXES:
        return None

    stem = name[: -len(suffix)] if suffix else name
    title, ss, isbn, extra = split_identifiers(stem)
    if not title and not ss and not isbn:
        return None
    return BookRecord(
        filename=name, title=title, ss=ss, isbn=isbn, suffix=suffix,
        path=path, size_mb=size_mb, fs_id=fs_id, source=source, extra_ss=extra,
    )


def _decode_stream(path) -> Iterator[str]:
    """Yield text lines, detecting UTF-16 and tolerating mixed legacy encodings."""
    with open(path, "rb") as handle:
        head = handle.read(4)
        handle.seek(0)
        if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
            encoding = "utf-16"
            import io

            wrapper = io.TextIOWrapper(handle, encoding=encoding, errors="replace", newline="")
            for line in wrapper:
                yield line
            return
        for chunk in handle:
            try:
                yield chunk.decode("utf-8")
            except UnicodeDecodeError:
                yield chunk.decode("gb18030", errors="replace")


def parse_file(path, source: str = "") -> Iterator[BookRecord]:
    """Stream records out of one catalogue file."""
    from pathlib import Path

    path = Path(path)
    label = source or path.name
    for line in _decode_stream(path):
        record = parse_line(line, label)
        if record is not None:
            yield record
