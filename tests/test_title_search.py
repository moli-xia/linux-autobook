"""Title-only books must be findable, and must never resolve to a different book.

The names here are real entries from the live library, kept verbatim: the
spacing inside 《 》 and the promotional bracket are exactly what broke the
original substring matching.
"""
from __future__ import annotations

import unittest

from autobook_linux.baidu_pan import GroupShareFile
from autobook_linux.library_index import pick_best_file
from autobook_linux.lookup import (
    Lookup, core_token, coverage, normalize_for_match, plan_from_task,
    queries_for, search_queries, title_matches, token_windows,
)

TITLE = "基层女性生存指北 王慧玲 2023"

# Verbatim from the group library.
NAMES = {
    "big_pdf": "#1000125 《 基层女性生存指北 》 王慧玲 【 2023 】.pdf",
    "epub": "#1000125 《 基层女性生存指北 》 王慧玲 【 2023 】.epub",
    "small_pdf": "基层女性生存指北_9787516834787 15262536.pdf",
    "plain": "基层女性生存指北.PDF",
    "other_book": "基层女性【热门女性话题vlogger“玲玲Peter和四只猫”首部作品，爱、金钱与精神世界的建立！】 (王慧玲).pdf",
    "same_author": "33106937_有线电视实用技术与新技术_王慧玲等编着西安电子科技大学.djvu",
}


def share(name: str, mtime: int = 1000, size: int = 1024) -> GroupShareFile:
    return GroupShareFile(
        gid="1", msg_id="m", from_uk="u", fs_id=abs(hash(name)) % 10**9,
        name=name, path=f"/{name}", size=size, is_dir=False, server_mtime=mtime,
    )


class QueryTests(unittest.TestCase):
    def test_the_full_title_is_tried_first(self) -> None:
        self.assertEqual(search_queries(TITLE)[0], TITLE)

    def test_the_bare_book_name_is_reached(self) -> None:
        # The live search returns 0 hits for the full string and 9 for this one.
        self.assertIn("基层女性生存指北", search_queries(TITLE))

    def test_prefixes_come_before_later_runs(self) -> None:
        queries = search_queries(TITLE)
        self.assertLess(queries.index("基层女性生存指北"), queries.index("王慧玲"))

    def test_a_bare_year_is_never_a_query_on_its_own(self) -> None:
        self.assertNotIn("2023", search_queries(TITLE))

    def test_a_long_name_falls_back_to_slices_of_itself(self) -> None:
        # The live index answers 0 for 中国企业如何定战略 and finds the file
        # named after it only for 如何定战略.
        queries = search_queries("中国企业如何定战略")
        self.assertEqual(queries[0], "中国企业如何定战略")
        self.assertIn("如何定战略", queries)

    def test_slices_come_after_the_whole_title(self) -> None:
        queries = search_queries(TITLE)
        self.assertLess(queries.index("基层女性生存指北"), queries.index("基层女性生存"))

    def test_a_short_name_needs_no_slices(self) -> None:
        self.assertEqual(search_queries("活着呢"), ["活着呢"])

    def test_edges_are_preferred_over_the_middle(self) -> None:
        windows = token_windows("中国企业如何定战略")
        self.assertLess(windows.index("如何定战略"), windows.index("企业如何定"))

    def test_the_number_of_searches_is_capped(self) -> None:
        long_title = " ".join(f"词{index}词" for index in range(12))
        self.assertLessEqual(len(search_queries(long_title)), 8)

    def test_an_empty_title_asks_for_nothing(self) -> None:
        self.assertEqual(search_queries("   "), [])

    def test_ss_and_isbn_are_searched_verbatim(self) -> None:
        self.assertEqual(queries_for(Lookup("ss", "13128895")), ["13128895"])
        self.assertEqual(queries_for(Lookup("isbn", "9787516834787")), ["9787516834787"])


class MatchingTests(unittest.TestCase):
    def test_spacing_and_brackets_do_not_prevent_a_match(self) -> None:
        self.assertTrue(title_matches(NAMES["big_pdf"], TITLE))

    def test_a_name_without_the_author_still_matches(self) -> None:
        self.assertTrue(title_matches(NAMES["small_pdf"], TITLE))
        self.assertTrue(title_matches(NAMES["plain"], TITLE))

    def test_sharing_only_the_author_is_not_a_match(self) -> None:
        # Searching for the author alone must never deliver their other book.
        self.assertFalse(title_matches(NAMES["same_author"], TITLE))

    def test_a_different_book_by_the_same_author_is_not_a_match(self) -> None:
        self.assertFalse(title_matches(NAMES["other_book"], TITLE))

    def test_core_token_is_the_book_name_not_the_year(self) -> None:
        self.assertEqual(core_token(TITLE), "基层女性生存指北")

    def test_coverage_ranks_a_complete_name_higher(self) -> None:
        self.assertGreater(coverage(NAMES["big_pdf"], TITLE), coverage(NAMES["small_pdf"], TITLE))

    def test_normalisation_strips_decoration(self) -> None:
        self.assertEqual(normalize_for_match("《 基层女性生存指北 》"), "基层女性生存指北")


class SelectionTests(unittest.TestCase):
    def test_the_matching_book_wins_over_an_unrelated_one(self) -> None:
        best = pick_best_file(
            [share(NAMES["other_book"]), share(NAMES["same_author"]), share(NAMES["big_pdf"])],
            TITLE, "title")
        self.assertEqual(best.name, NAMES["big_pdf"])

    def test_a_pdf_is_preferred_over_an_epub_of_the_same_book(self) -> None:
        best = pick_best_file([share(NAMES["epub"]), share(NAMES["big_pdf"])], TITLE, "title")
        self.assertEqual(best.name, NAMES["big_pdf"])

    def test_the_fuller_name_wins_between_two_pdfs(self) -> None:
        best = pick_best_file([share(NAMES["small_pdf"]), share(NAMES["big_pdf"])], TITLE, "title")
        self.assertEqual(best.name, NAMES["big_pdf"])

    def test_only_unrelated_results_means_no_delivery(self) -> None:
        best = pick_best_file(
            [share(NAMES["same_author"]), share(NAMES["other_book"])], TITLE, "title")
        self.assertIsNone(best)

    def test_an_empty_result_set_is_no_delivery(self) -> None:
        self.assertIsNone(pick_best_file([], TITLE, "title"))


class PlanTests(unittest.TestCase):
    def test_a_title_only_task_plans_a_title_search(self) -> None:
        plan = plan_from_task({"book_title": TITLE})
        self.assertEqual([entry.kind for entry in plan], ["title"])

    def test_an_ss_task_keeps_the_title_as_a_fallback(self) -> None:
        plan = plan_from_task({"ssno": "13128895", "book_title": TITLE})
        self.assertEqual([entry.kind for entry in plan], ["ss", "title"])
        self.assertEqual(plan[0].value, "13128895")

    def test_isbn_outranks_the_title_but_both_are_kept(self) -> None:
        plan = plan_from_task({"book_title": "中国企业如何定战略_9785451325438"})
        self.assertEqual([entry.kind for entry in plan], ["isbn", "title"])

    def test_an_isbn_is_never_read_as_an_ss_number(self) -> None:
        plan = plan_from_task({"book_title": "某书_9787516834787"})
        self.assertNotIn("ss", [entry.kind for entry in plan])

    def test_all_three_keys_are_planned_when_present(self) -> None:
        plan = plan_from_task({"ssno": "13128895", "book_title": "某书_9787516834787"})
        self.assertEqual([entry.kind for entry in plan], ["ss", "isbn", "title"])

    def test_a_task_with_nothing_usable_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            plan_from_task({"book_title": "", "keyword": ""})


if __name__ == "__main__":
    unittest.main()
