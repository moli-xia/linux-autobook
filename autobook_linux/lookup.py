"""How a delivery task is turned into a non-standard file search.

Most books are found by their 8-digit 读秀 SS number.  The books imported from
the netdisk catalogue often have no SS number at all — only an ISBN, or only a
title — so a task has to be able to say *what kind* of key it carries and the
search has to adapt accordingly.

A task can also carry several usable keys at once (an SS number *and* a title).
``plan_from_task`` returns all of them in order of precision, so a search that
comes up empty on the SS number can still fall back to the title instead of
failing the task.

Titles need one more step.  The group search matches the whole query as a
phrase, so ``基层女性生存指北 王慧玲 2023`` finds nothing while
``基层女性生存指北`` finds nine files.  ``search_queries`` produces the
progressively shorter strings to try, and ``pick_best_file`` still judges the
results against the *full* title so a short query cannot deliver a wrong book.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SS_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
ISBN_RE = re.compile(r"(97[89][\d\-]{10,16}\d)")
ISBN_STRICT_RE = re.compile(r"(?<!\d)(97[89]\d{10})(?!\d)")
TITLE_MAX = 60
VALID_KINDS = ("ss", "isbn", "title")
# Keep the search string harmless: no control characters, no absurd lengths.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Punctuation and spacing differ freely between a stored title and a file name
# ("《 基层女性生存指北 》"), so both sides are stripped down before comparing.
NOISE_RE = re.compile(r"[\s　_\-—–~·:：,，.。、;；!！?？/\\|\"'`*#$%^&+=<>()（）\[\]【】{}《》〈〉“”‘’]+")
# How many search strings one title may cost us in network calls.
MAX_QUERIES = 8
# The group search is not a plain substring match: ``中国企业如何定战略`` finds
# nothing while ``如何定战略`` finds the very file named after it.  When a whole
# title draws a blank, windows of the book's name are tried instead.
WINDOW_SIZES = (6, 5)
# A file name has to carry this share of the title's characters to be accepted.
MIN_COVERAGE = 0.34
# Below this length a token is too generic to identify a book on its own.
CORE_TOKEN_MIN = 2
YEAR_RE = re.compile(r"^(1[89]|20)\d{2}$")


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


def normalize_for_match(value: str) -> str:
    """Strip everything that differs cosmetically between a title and a name."""
    return NOISE_RE.sub("", (value or "")).lower()


def clean_title(value: str) -> str:
    """Reduce a stored book title to something worth searching for.

    Site titles look like ``《书名》_作者_SS_ISBN``; only the book name is a
    useful search term, and the group search dislikes punctuation.
    """
    text = CONTROL_RE.sub(" ", value or "")
    head = text.split("_", 1)[0]
    head = head.replace("《", " ").replace("》", " ")
    head = re.sub(r"[\[\]【】()（）:：,，.。、;；!！?？/\\|\"'`~*#$%^&+=<>]+", " ", head)
    head = re.sub(r"\s+", " ", head).strip()
    return head[:TITLE_MAX]


def title_tokens(title: str) -> list[str]:
    """The whitespace-separated pieces of a cleaned title."""
    return [token for token in clean_title(title).split(" ") if token]


def core_token(title: str) -> str:
    """The single most identifying piece of a title.

    Usually the book name itself: it is the longest run, while the author and
    the year that trail it are short.
    """
    tokens = title_tokens(title)
    if not tokens:
        return ""
    return max(tokens, key=lambda token: (0 if YEAR_RE.match(token) else 1, len(token)))


def token_windows(token: str) -> list[str]:
    """Shorter slices of one long word, for when the whole word finds nothing.

    Prefix and suffix first: a book's name usually starts or ends on a phrase
    the index knows, while a slice through the middle is the least likely to
    line up with anything.
    """
    windows: list[str] = []
    for size in WINDOW_SIZES:
        if len(token) <= size:
            continue
        middle = (len(token) - size) // 2
        for candidate in (token[:size], token[-size:], token[middle:middle + size]):
            if candidate != token and candidate not in windows:
                windows.append(candidate)
    # Interleave so both sizes' prefixes and suffixes are tried before any
    # middle slice, which is the weakest guess.
    edges = [w for w in windows if len(w) in WINDOW_SIZES][: 2 * len(WINDOW_SIZES)]
    return edges + [w for w in windows if w not in edges]


def search_queries(title: str, limit: int = MAX_QUERIES) -> list[str]:
    """Search strings to try for a title, most promising first.

    The group search treats the query as one phrase, so a title carrying an
    author and a year matches nothing.  Contiguous runs of tokens are tried
    from the longest prefix down, which reaches the bare book name in a few
    steps and still covers titles that lead with the author.  Only when none
    of those can work does it fall back to slices of the book's own name.
    """
    tokens = title_tokens(title)
    if not tokens:
        return []
    queries: list[str] = []
    for start in range(len(tokens)):
        for end in range(len(tokens), start, -1):
            run = tokens[start:end]
            candidate = " ".join(run)
            # A bare year, or a single character, identifies nothing.
            if len(run) == 1 and (YEAR_RE.match(run[0]) or len(run[0]) < CORE_TOKEN_MIN):
                continue
            if candidate not in queries:
                queries.append(candidate)
    for window in token_windows(core_token(title)):
        if window not in queries:
            queries.append(window)
    return queries[:limit]


def coverage(name: str, title: str) -> float:
    """How much of the title's text the file name accounts for, 0.0 to 1.0."""
    tokens = title_tokens(title)
    if not tokens:
        return 0.0
    haystack = normalize_for_match(name)
    total = sum(len(normalize_for_match(token)) for token in tokens)
    if not total:
        return 0.0
    found = sum(
        len(normalize_for_match(token))
        for token in tokens
        if normalize_for_match(token) and normalize_for_match(token) in haystack
    )
    return found / total


def title_matches(name: str, title: str) -> bool:
    """Is this file plausibly the book the title names?

    The core token has to be present — that is the book's own name, and without
    it a match on the author alone would hand back a completely different book.
    """
    core = normalize_for_match(core_token(title))
    if len(core) < CORE_TOKEN_MIN:
        return False
    if core not in normalize_for_match(name):
        return False
    return coverage(name, title) >= MIN_COVERAGE


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


def queries_for(lookup: Lookup) -> list[str]:
    """The search strings that stand a chance of finding this lookup."""
    if lookup.kind == "title":
        return search_queries(lookup.value)
    return [lookup.value]


def plan_from_task(task: dict) -> list[Lookup]:
    """Every usable search key for a task, most precise first.

    An SS number identifies one book, an ISBN identifies an edition, a title is
    the last resort.  Returning all of them lets a search that finds nothing on
    the precise key fall back instead of failing the task outright.
    """
    plan: list[Lookup] = []

    def add(kind: str, value: str) -> None:
        try:
            lookup = validate(kind, value)
        except LookupError:
            return
        if lookup not in plan:
            plan.append(lookup)

    haystacks = [str(task.get(field) or "") for field in ("book_title", "keyword")]

    explicit = str(task.get("ssno") or "").strip()
    if explicit:
        hit = SS_RE.search(explicit)
        if hit:
            add("ss", hit.group(1))
    for text in haystacks:
        # Mask ISBNs first so their digits can never be read as an SS number.
        masked = ISBN_STRICT_RE.sub(lambda m: " " * len(m.group(0)), text)
        hit = SS_RE.search(masked)
        if hit:
            add("ss", hit.group(1))
    for text in haystacks:
        hit = ISBN_STRICT_RE.search(text)
        if hit:
            add("isbn", hit.group(1))
    for text in haystacks:
        add("title", text)

    if not plan:
        raise LookupError("任务既没有 SS 号，也没有 ISBN 或可用书名")
    return plan


def from_task(task: dict) -> Lookup:
    """The single most precise search key for a task."""
    return plan_from_task(task)[0]
