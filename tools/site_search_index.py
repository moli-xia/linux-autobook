"""Reproduce LECMS' search tokeniser so imported books are findable on the site.

``le_cms_article_search.message`` holds space-separated tokens built from the
title by ``search_build_hex_tokens()`` in the le_search plugin:

* runs of CJK (U+4E00–U+9FFF) become overlapping character-pair tokens,
  ``z`` + the UCS-2BE hex of each character, e.g. 澄怀 -> ``z6f846000``;
* runs of ASCII letters/digits become one lowercase token, kept only when it is
  longer than one character (so SS numbers and ISBNs are indexed verbatim);
* anything else (punctuation, full-width marks, spaces) ends the current run;
* duplicates are dropped, order preserved.

A book inserted without this row exists but can never be found by search.
"""
from __future__ import annotations

import re

CJK_RE = re.compile(r"[一-鿿]")
ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def _flush_cjk(tokens: list[str], run: list[str], prefix: str) -> None:
    if len(run) > 1:
        for index in range(len(run) - 1):
            tokens.append(prefix + run[index] + run[index + 1])
    run.clear()


def _flush_ascii(tokens: list[str], word: list[str]) -> None:
    # PHP checks strlen() > 1, i.e. more than one byte; these tokens are ASCII.
    if len(word) > 1:
        tokens.append("".join(word))
    word.clear()


def build_hex_tokens(text: str, prefix: str = "z") -> str:
    tokens: list[str] = []
    word: list[str] = []
    cjk_run: list[str] = []

    for char in text or "":
        if ALNUM_RE.fullmatch(char):
            _flush_cjk(tokens, cjk_run, prefix)
            word.append(char.lower())
            continue
        _flush_ascii(tokens, word)
        if CJK_RE.fullmatch(char):
            cjk_run.append(format(ord(char), "04x"))
        else:
            _flush_cjk(tokens, cjk_run, prefix)

    _flush_ascii(tokens, word)
    _flush_cjk(tokens, cjk_run, prefix)

    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return " ".join(unique)


def search_message(title: str) -> str:
    """The value stored in le_cms_article_search.message for a book title."""
    return build_hex_tokens(title, "z")
