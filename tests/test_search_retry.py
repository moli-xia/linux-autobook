"""The group search answers inconsistently, so an empty result is not final.

Measured on the live library: the same query returned two results and then
zero, alternating, for six consecutive calls.  Without a retry every other
delivery would fail for a book that is plainly in the library.
"""
from __future__ import annotations

import unittest
from unittest import mock

from autobook_linux.baidu_pan import SEARCH_EMPTY_RETRIES, BaiduPanClient, GroupShareFile


def share(name: str = "book.pdf") -> GroupShareFile:
    return GroupShareFile(gid="1", msg_id="m", from_uk="u", fs_id=1, name=name,
                          path=f"/{name}", size=1, is_dir=False, server_mtime=1)


class Flaky(BaiduPanClient):
    """A client whose single-shot search follows a scripted sequence."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def _search_group_once(self, gid, keyword):
        self.calls += 1
        return self.answers.pop(0) if self.answers else []


class RetryTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("autobook_linux.baidu_pan.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_empty_answer_is_retried(self) -> None:
        client = Flaky([[], [share()]])
        self.assertEqual(len(client.search_group_files("1", "书名")), 1)
        self.assertEqual(client.calls, 2)

    def test_a_hit_returns_immediately(self) -> None:
        client = Flaky([[share()], [share()]])
        client.search_group_files("1", "书名")
        self.assertEqual(client.calls, 1, "a successful search must not be repeated")

    def test_retries_are_bounded(self) -> None:
        client = Flaky([])
        self.assertEqual(client.search_group_files("1", "书名"), [])
        self.assertEqual(client.calls, SEARCH_EMPTY_RETRIES)

    def test_the_caller_can_choose_the_budget(self) -> None:
        client = Flaky([])
        client.search_group_files("1", "书名", retries=2)
        self.assertEqual(client.calls, 2)

    def test_it_waits_between_attempts_but_not_after_the_last(self) -> None:
        client = Flaky([])
        client.search_group_files("1", "书名", retries=3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_a_blank_keyword_never_reaches_the_network(self) -> None:
        client = Flaky([[share()]])
        self.assertEqual(client.search_group_files("1", "   "), [])
        self.assertEqual(client.calls, 0)

    def test_a_zero_budget_still_tries_once(self) -> None:
        client = Flaky([[share()]])
        self.assertEqual(len(client.search_group_files("1", "书名", retries=0)), 1)
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
