"""Tests for turning a delivery task into a group-library search."""
from __future__ import annotations

import unittest

from autobook_linux.lookup import (
    Lookup, LookupError, clean_title, from_task, validate,
)


class FromTaskTests(unittest.TestCase):
    def test_explicit_ss_number_wins(self) -> None:
        lookup = from_task({"ssno": "12793026", "book_title": "《某书》__99999999_9787511582447"})
        self.assertEqual(lookup, Lookup("ss", "12793026"))

    def test_ss_number_read_from_the_title(self) -> None:
        lookup = from_task({"ssno": "", "book_title": "《澄怀古雅集》_赵毅著_96364595_9787511582447"})
        self.assertEqual(lookup, Lookup("ss", "96364595"))

    def test_isbn_only_book_falls_back_to_isbn(self) -> None:
        # The shape the catalogue import writes for ISBN-only books.
        lookup = from_task({"ssno": "", "book_title": "《中国企业如何定战略》___9787511582447"})
        self.assertEqual(lookup, Lookup("isbn", "9787511582447"))

    def test_isbn_digits_are_never_taken_as_an_ss_number(self) -> None:
        lookup = from_task({"book_title": "《某书》___9787520125901"})
        self.assertEqual(lookup.kind, "isbn")

    def test_title_only_book_falls_back_to_the_title(self) -> None:
        lookup = from_task({"ssno": "", "book_title": "《作家們都喝什麼酒》"})
        self.assertEqual(lookup.kind, "title")
        self.assertEqual(lookup.value, "作家們都喝什麼酒")

    def test_keyword_is_used_when_there_is_no_title(self) -> None:
        lookup = from_task({"keyword": "12793026"})
        self.assertEqual(lookup, Lookup("ss", "12793026"))

    def test_empty_task_is_rejected_with_a_clear_message(self) -> None:
        with self.assertRaises(LookupError) as caught:
            from_task({"ssno": "", "book_title": "", "keyword": ""})
        self.assertIn("SS", str(caught.exception))

    def test_a_one_character_title_is_not_searchable(self) -> None:
        with self.assertRaises(LookupError):
            from_task({"book_title": "《A》"})


class CleanTitleTests(unittest.TestCase):
    def test_site_title_is_reduced_to_the_book_name(self) -> None:
        self.assertEqual(
            clean_title("《澄怀古雅集》_赵毅著_96364595_9787511582447"),
            "澄怀古雅集",
        )

    def test_punctuation_is_flattened(self) -> None:
        self.assertEqual(
            clean_title("《全球新闻传播史：公元1500-2000年.第2版》"),
            "全球新闻传播史 公元1500-2000年 第2版",
        )

    def test_control_characters_are_removed(self) -> None:
        self.assertNotIn(chr(10), clean_title("书名" + chr(10) + "第二行"))

    def test_length_is_bounded(self) -> None:
        self.assertLessEqual(len(clean_title("长" * 500)), 60)


class ValidateTests(unittest.TestCase):
    def test_valid_keys(self) -> None:
        self.assertEqual(validate("ss", "12345678").value, "12345678")
        self.assertEqual(validate("isbn", "9787511582447").value, "9787511582447")
        self.assertEqual(validate("title", "《某本书》").value, "某本书")

    def test_bad_ss_number(self) -> None:
        for value in ("1234567", "123456789", "abcdefgh", ""):
            with self.assertRaises(LookupError):
                validate("ss", value)

    def test_bad_isbn(self) -> None:
        for value in ("1234567890123", "978123456789", ""):
            with self.assertRaises(LookupError):
                validate("isbn", value)

    def test_unknown_kind(self) -> None:
        with self.assertRaises(LookupError):
            validate("magic", "x")

    def test_payload_round_trip(self) -> None:
        lookup = validate("isbn", "9787511582447")
        self.assertEqual(lookup.as_payload(), {"kind": "isbn", "value": "9787511582447"})
        self.assertIn("ISBN", lookup.label())


if __name__ == "__main__":
    unittest.main()
