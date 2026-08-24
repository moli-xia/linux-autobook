"""How a delivery task is turned into a group-library search.

Most books are found by their 8-digit 读秀 SS number.  The books imported from
the netdisk catalogue often have no SS number at all — only an ISBN, or only a
title — so a task has to be able to say *what kind* of key it carries and the
gateway has to search accordingly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SS_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
ISBN_RE = re.compile(r"(?<!\d)(97[89]\d{10})(?!\d)")
TITLE_MAX = 60
VALID_KINDS = ("ss", "isbn", "title")
# Keep the search string harmless: no control characters, no absurd lengths.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class LookupError(ValueError):
    """The task does not carry anything usable to search for."""


@dataclass(frozen=True)
class Lookup:
    kind: str    # ss | isbn | title
    value: str

    def label(self) -> str:
        return {"ss": "SS", "isbn": "ISBN", "title": "书名"}[self.kind] + "=" + self.value

    def as_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


def clean_title(value: str) -> str:
    """Reduce a stored book title to something worth searching for.

    Site titles look like ``《书名》_作者_SS_ISBN``; only the book name is a
    useful search term, and Baidu's group search dislikes punctuation.
    """
    text = CONTROL_RE.sub(" ", value or "")
    head = text.split("_", 1)[0]
    head = head.replace("《", " ").replace("》", " ")
    head = re.sub(r"[\[\]【】()（）:：,，.。、;；!！?？/\\|\"'`~*#$%^&+=<>]+", " ", head)
    head = re.sub(r"\s+", " ", head).strip()
    return head[:TITLE_MAX]


def validate(kind: str, value: str) -> Lookup:
    """Normalise and check one lookup, raising LookupError when unusable."""
    kind = (kind or "").strip().lower()
    value = (value or "").strip()
    if kind not in VALID_KINDS:
        raise LookupError(f"不支持的检索类型: {kind!r}")
    if kind == "ss":
        if not re.fullmatch(r"\d{8}", value):
            raise LookupError("SS 号必须是 8 位数字")
    elif kind == "isbn":
        if not re.fullmatch(r"97[89]\d{10}", value):
            raise LookupError("ISBN 必须是 13 位且以 978/979 开头")
    else:
        value = clean_title(value)
        if len(value) < 2:
            raise LookupError("书名过短，无法检索")
    return Lookup(kind=kind, value=value)


def from_task(task: dict) -> Lookup:
    """Pick the best available search key for a delivery task.

    Order matters: an SS number identifies a single book, an ISBN identifies an
    edition, a title is a last resort that may match several files.
    """
    explicit = str(task.get("ssno") or "").strip()
    if explicit:
        hit = SS_RE.search(explicit)
        if hit:
            return Lookup("ss", hit.group(1))

    haystacks = [str(task.get(field) or "") for field in ("book_title", "keyword")]
    for text in haystacks:
        # Mask ISBNs first so their digits can never be read as an SS number.
        masked = ISBN_RE.sub(lambda m: " " * len(m.group(0)), text)
        hit = SS_RE.search(masked)
        if hit:
            return Lookup("ss", hit.group(1))
    for text in haystacks:
        hit = ISBN_RE.search(text)
        if hit:
            return Lookup("isbn", hit.group(1))
    for text in haystacks:
        title = clean_title(text)
        if len(title) >= 2:
            return Lookup("title", title)
    raise LookupError("任务既没有 SS 号，也没有 ISBN 或可用书名")
