"""Tests for the site import: title shaping, filtering and SQL escaping."""
from __future__ import annotations

import unittest

from tools.import_books import (
    TITLE_MAX, clean_title, escape, site_intro, site_title, usable_title,
)
from tools.site_search_index import search_message


def row(title: str, ss: str = "", isbn: str = "", suffix: str = ".pdf") -> dict:
    return {"title": title, "ss": ss, "isbn": isbn, "suffix": suffix}


class TitleFilterTests(unittest.TestCase):
    def test_real_titles_are_kept(self) -> None:
        for value in ("中国企业如何定战略", "The grass is singing",
                      "基层女性生存指北", "CATIA 软件建模"):
            self.assertTrue(usable_title(value), value)

    def test_sequence_numbers_and_noise_are_rejected(self) -> None:
        # 71% of the catalogue diff looked like this; importing it would just
        # fill the site with untitled rows.
        for value in ("", "   ", "001", "463", "08", "(1)", "01(1)",
                      "ss", "n", "《", "###############"):
            self.assertFalse(usable_title(value), repr(value))


class TitleShapeTests(unittest.TestCase):
    def test_layout_matches_the_site_convention(self) -> None:
        title = site_title(row("澄怀古雅集", ss="96364595", isbn="9787511582447"))
        self.assertEqual(title, "《澄怀古雅集》__96364595_9787511582447")

    def test_ss_only(self) -> None:
        self.assertEqual(site_title(row("某书", ss="12345678")), "《某书》__12345678_")

    def test_isbn_only(self) -> None:
        title = site_title(row("中国企业如何定战略", isbn="9785451325438"))
        self.assertEqual(title, "《中国企业如何定战略》___9785451325438")

    def test_title_only(self) -> None:
        self.assertEqual(site_title(row("作家們都喝什麼酒")), "《作家們都喝什麼酒》")

    def test_uploader_index_marker_is_removed(self) -> None:
        self.assertEqual(clean_title("#1000125 《 基层女性生存指北 》 王慧玲"),
                         "《 基层女性生存指北 》 王慧玲")
        self.assertEqual(clean_title("#@1000027《这就是人性1》"), "《这就是人性1》")

    def test_overlong_title_is_trimmed_but_keeps_identifiers(self) -> None:
        long_name = "长" * 200
        title = site_title(row(long_name, ss="12345678", isbn="9787511582447"))
        self.assertLessEqual(len(title), TITLE_MAX)
        self.assertIn("12345678", title)
        self.assertIn("9787511582447", title)

    def test_site_parser_can_still_read_the_ss_number(self) -> None:
        # The delivery plugin looks for _(\d{8})_ in the title.
        import re
        title = site_title(row("某书", ss="12345678", isbn="9787511582447"))
        self.assertIsNotNone(re.search(r"_(\d{8})_", title))

    def test_generated_title_is_searchable(self) -> None:
        title = site_title(row("中国企业如何定战略", isbn="9785451325438"))
        message = search_message(title)
        self.assertIn("9785451325438", message)
        self.assertTrue(message.startswith("z"))


class IntroTests(unittest.TestCase):
    def test_intro_lists_what_is_known(self) -> None:
        intro = site_intro(row("某书", ss="12345678", isbn="9787511582447", suffix=".epub"))
        self.assertIn("【书名】：《某书》", intro)
        self.assertIn("【ISBN】：9787511582447", intro)
        self.assertIn("【SS码】：12345678", intro)
        self.assertIn("EPUB", intro)
        self.assertIn("百度网盘群文件库", intro)

    def test_intro_omits_absent_identifiers(self) -> None:
        intro = site_intro(row("某书"))
        self.assertNotIn("ISBN", intro)
        self.assertNotIn("SS码", intro)

    def test_intro_is_bounded(self) -> None:
        intro = site_intro(row("书" * 400, ss="12345678"))
        self.assertLessEqual(len(intro), 500)

    def test_separator_tags_stay_literal(self) -> None:
        # The site stores real <br /> tags; escaping them showed the markup
        # to readers instead of breaking the line.
        intro = site_intro(row("某书", ss="12345678"))
        self.assertIn("<br />", intro)
        self.assertNotIn("&lt;br", intro)

    def test_angle_brackets_in_a_title_are_escaped(self) -> None:
        intro = site_intro(row("a<b>c", ss="12345678"))
        self.assertIn("a&lt;b&gt;c", intro)


class EscapingTests(unittest.TestCase):
    def test_quotes_and_backslashes_are_escaped(self) -> None:
        self.assertEqual(escape("it's"), "it\\'s")
        self.assertEqual(escape("a\\b"), "a\\\\b")

    def test_newlines_become_escapes(self) -> None:
        self.assertEqual(escape("a" + chr(10) + "b"), "a\\nb")
        self.assertEqual(escape("a" + chr(13) + "b"), "a\\rb")

    def test_null_bytes_are_dropped(self) -> None:
        self.assertEqual(escape("a" + chr(0) + "b"), "ab")

    def test_a_title_with_an_apostrophe_round_trips(self) -> None:
        title = site_title(row("It's a book", isbn="9787511582447"))
        self.assertNotIn("''", escape(title))
        self.assertIn("\\'", escape(title))


if __name__ == "__main__":
    unittest.main()
