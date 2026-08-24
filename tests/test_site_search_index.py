"""The tokeniser is verified against rows taken straight from the live site."""
from __future__ import annotations

import unittest

from tools.site_search_index import build_hex_tokens, search_message

# (title, message) pairs copied out of le_cms_article / le_cms_article_search.
LIVE_ROWS = [
    (
        "《澄怀古雅集》_赵毅著_96364595_9787511582447",
        "z6f846000 z600053e4 z53e496c5 z96c596c6 z8d756bc5 z6bc58457 "
        "96364595 9787511582447",
    ),
    (
        "《新时代万有文库丛书  商君书》_(战国)商鞅；刘跃进总主编；王亚伟校注_96363610_9787545168501",
        "z65b065f6 z65f64ee3 z4ee34e07 z4e076709 z67096587 z65875e93 z5e934e1b "
        "z4e1b4e66 z5546541b z541b4e66 z621856fd z55469785 z52188dc3 z8dc38fdb "
        "z8fdb603b z603b4e3b z4e3b7f16 z738b4e9a z4e9a4f1f z4f1f6821 z68216ce8 "
        "96363610 9787545168501",
    ),
]


class LiveParityTests(unittest.TestCase):
    def test_matches_the_site_byte_for_byte(self) -> None:
        for title, expected in LIVE_ROWS:
            self.assertEqual(search_message(title), expected, title)


class TokeniserRuleTests(unittest.TestCase):
    def test_cjk_run_becomes_overlapping_pairs(self) -> None:
        # 澄怀古 -> 澄怀, 怀古
        self.assertEqual(build_hex_tokens("澄怀古"), "z6f846000 z600053e4")

    def test_single_cjk_character_yields_nothing(self) -> None:
        self.assertEqual(build_hex_tokens("澄"), "")

    def test_punctuation_breaks_the_run(self) -> None:
        # The pair spanning the separator must not be emitted.
        self.assertNotIn("z96c68d75", build_hex_tokens("集_赵毅"))

    def test_identifiers_are_indexed_verbatim(self) -> None:
        self.assertIn("96364595", build_hex_tokens("_96364595_"))
        self.assertIn("9787511582447", build_hex_tokens("_9787511582447"))

    def test_single_character_ascii_token_is_dropped(self) -> None:
        self.assertEqual(build_hex_tokens("a"), "")
        self.assertEqual(build_hex_tokens("ab"), "ab")

    def test_ascii_is_lowercased(self) -> None:
        self.assertEqual(build_hex_tokens("CATIA"), "catia")

    def test_duplicates_are_removed_but_order_kept(self) -> None:
        # 书书书 gives the same pair twice; only one survives.
        self.assertEqual(build_hex_tokens("书书书"), "z4e664e66")
        self.assertEqual(build_hex_tokens("ab_ab_cd"), "ab cd")

    def test_full_width_and_spaces_break_runs(self) -> None:
        message = build_hex_tokens("商君书》_(战国)")
        self.assertIn("z5546541b", message)
        self.assertIn("z621856fd", message)
        self.assertNotIn("z4e666218", message)

    def test_empty_input(self) -> None:
        self.assertEqual(build_hex_tokens(""), "")
        self.assertEqual(search_message(""), "")

    def test_isbn_only_title_is_still_indexed(self) -> None:
        # The books this import adds often have no SS number at all.
        message = search_message("《中国企业如何定战略》__9785451325438")
        self.assertIn("9785451325438", message)
        self.assertTrue(message.startswith("z"))


if __name__ == "__main__":
    unittest.main()
