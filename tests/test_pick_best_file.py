"""Which group-library file gets picked for each kind of lookup."""
from __future__ import annotations

import unittest

from autobook_linux.baidu_pan import GroupShareFile
from autobook_linux.library_index import pick_best_file


def item(name: str, mtime: int = 0) -> GroupShareFile:
    return GroupShareFile(
        gid="g", msg_id="m", from_uk="u", fs_id=1, name=name, path="/p",
        size=1, is_dir=False, server_mtime=mtime,
    )


class SsLookupTests(unittest.TestCase):
    def test_ready_pdf_beats_an_exactly_named_archive(self) -> None:
        # Unpacking is expensive; a usable PDF wins even without an exact stem.
        best = pick_best_file([item("12607753.zip"), item("12607753_book.pdf")], "12607753", "ss")
        self.assertEqual(best.name, "12607753_book.pdf")

    def test_exact_stem_breaks_ties_within_a_format(self) -> None:
        best = pick_best_file([item("12607753_extra.zip"), item("12607753.zip")], "12607753", "ss")
        self.assertEqual(best.name, "12607753.zip")

    def test_newest_wins_when_all_else_is_equal(self) -> None:
        best = pick_best_file(
            [item("12607753_a.pdf", mtime=100), item("12607753_b.pdf", mtime=200)],
            "12607753", "ss",
        )
        self.assertEqual(best.name, "12607753_b.pdf")

    def test_epub_ranks_above_an_archive(self) -> None:
        best = pick_best_file([item("book.zip"), item("book.epub")], "12607753", "ss")
        self.assertEqual(best.name, "book.epub")


class IsbnAndTitleLookupTests(unittest.TestCase):
    def test_name_containing_the_isbn_wins_over_a_convenient_pdf(self) -> None:
        # A title/ISBN search returns loosely related files; relevance first.
        best = pick_best_file(
            [item("unrelated.pdf"), item("某书_9787511582447.zip")],
            "9787511582447", "isbn",
        )
        self.assertEqual(best.name, "某书_9787511582447.zip")

    def test_format_breaks_the_tie_among_matches(self) -> None:
        best = pick_best_file(
            [item("某书_9787511582447.zip"), item("某书_9787511582447.pdf")],
            "9787511582447", "isbn",
        )
        self.assertEqual(best.name, "某书_9787511582447.pdf")

    def test_no_match_returns_nothing_rather_than_a_wrong_book(self) -> None:
        # Delivering an unrelated file would be worse than failing the task.
        self.assertIsNone(
            pick_best_file([item("完全无关.pdf")], "9787511582447", "isbn"))
        self.assertIsNone(
            pick_best_file([item("完全无关.pdf")], "中国企业如何定战略", "title"))

    def test_title_match_is_case_insensitive(self) -> None:
        best = pick_best_file([item("The Grass Is Singing.epub")], "the grass is singing", "title")
        self.assertIsNotNone(best)

    def test_empty_candidate_list(self) -> None:
        self.assertIsNone(pick_best_file([], "12607753", "ss"))
        self.assertIsNone(pick_best_file([], "9787511582447", "isbn"))


if __name__ == "__main__":
    unittest.main()
