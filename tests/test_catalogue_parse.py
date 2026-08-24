"""Tests for catalogue parsing, using the naming variants seen in the real 书表."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.catalogue_parse import (
    BookRecord, isbn13_of, normalise_title, parse_file, parse_line,
    split_identifiers, strip_promotional,
)
from tools.site_keys import keys_from_title

TAB = chr(9)
NEWLINE = chr(10)


class IdentifierTests(unittest.TestCase):
    def test_title_with_ss_only(self) -> None:
        title, ss, isbn, _ = split_identifiers("全球新闻传播史：公元1500-2000年.第2版_12188662")
        self.assertEqual(ss, "12188662")
        self.assertEqual(isbn, "")
        self.assertIn("全球新闻传播史", title)

    def test_title_with_isbn_only(self) -> None:
        title, ss, isbn, _ = split_identifiers("作为文学的《利未记》_9787520125901")
        self.assertEqual(isbn, "9787520125901")
        self.assertEqual(ss, "", "eight digits inside an ISBN must not become an SS number")
        self.assertIn("利未记", title)

    def test_title_with_both_identifiers(self) -> None:
        title, ss, isbn, _ = split_identifiers("中国道路与民营企业发展_9787100178884 14747691")
        self.assertEqual(isbn, "9787100178884")
        self.assertEqual(ss, "14747691")
        self.assertIn("中国道路与民营企业发展", title)

    def test_hyphen_separated_ss(self) -> None:
        title, ss, _isbn, _ = split_identifiers("域外汉籍音乐舞蹈古图集.第八册-15353709")
        self.assertEqual(ss, "15353709")
        self.assertIn("域外汉籍音乐舞蹈古图集", title)

    def test_title_without_any_identifier(self) -> None:
        title, ss, isbn, _ = split_identifiers("作家們都喝什麼酒")
        self.assertEqual((ss, isbn), ("", ""))
        self.assertEqual(title, "作家們都喝什麼酒")

    def test_site_style_title(self) -> None:
        _title, ss, isbn, _ = split_identifiers("《澄怀古雅集》_赵毅著_96364595_9787511582447")
        self.assertEqual(ss, "96364595")
        self.assertEqual(isbn, "9787511582447")


class ParseLineTests(unittest.TestCase):
    def test_tree_prefix_is_stripped(self) -> None:
        record = parse_line("│   ├── 新手学外贸全流程一本通_14425171.pdf")
        self.assertIsNotNone(record)
        self.assertEqual(record.filename, "新手学外贸全流程一本通_14425171.pdf")
        self.assertEqual(record.ss, "14425171")
        self.assertEqual(record.suffix, ".pdf")

    def test_listing_row_keeps_path_and_fsid(self) -> None:
        line = ("佐贺的超级阿嬷_9787544299084.epub, /duxiu/读秀7.0/最新书籍/"
                "佐贺的超级阿嬷_9787544299084.epub, 0.17, 426043135173913")
        record = parse_line(line)
        self.assertIsNotNone(record)
        self.assertEqual(record.suffix, ".epub")
        self.assertEqual(record.isbn, "9787544299084")
        self.assertEqual(record.fs_id, "426043135173913")
        self.assertAlmostEqual(record.size_mb, 0.17)
        self.assertTrue(record.path.startswith("/duxiu/"))

    def test_non_book_lines_are_ignored(self) -> None:
        for line in ("", "   ", "├── 02文件夹", "WinDjView.exe"):
            self.assertIsNone(parse_line(line), line)

    def test_archive_entries_are_flagged(self) -> None:
        record = parse_line("左宗棠传_12793026.zip")
        self.assertIsNotNone(record)
        self.assertTrue(record.is_archive)
        self.assertEqual(record.ss, "12793026")


class KeyTests(unittest.TestCase):
    def test_ss_wins_over_isbn(self) -> None:
        record = BookRecord(filename="x.pdf", title="t", ss="12345678", isbn="9787100178884")
        self.assertEqual(record.key(), "ss:12345678")

    def test_isbn_used_when_no_ss(self) -> None:
        record = BookRecord(filename="x.pdf", title="t", isbn="9787100178884")
        self.assertEqual(record.key(), "isbn:9787100178884")

    def test_title_key_is_punctuation_insensitive(self) -> None:
        self.assertEqual(
            normalise_title("《中国企业如何定战略》"),
            normalise_title("中国企业如何定战略"),
        )
        self.assertEqual(
            normalise_title("全球新闻传播史：公元1500-2000年 第2版"),
            normalise_title("全球新闻传播史:公元1500-2000年第2版"),
        )


class RealWorldFormatTests(unittest.TestCase):
    """Every catalogue layout actually present in the group library."""

    def test_annotated_tree_with_size_and_path(self) -> None:
        line = ("3│  │  │  │  自杀心理危机干预9787121412905_关注更新【公众号】知享书会【微信】whbhpfc.pdf "
                "(3.14 MB)  --  /【大学堂读秀书库】/【大学堂读秀持续更新库】/读秀书库6.0去重版0415/9787/")
        record = parse_line(line)
        self.assertIsNotNone(record)
        self.assertEqual(record.suffix, ".pdf")
        self.assertEqual(record.isbn, "9787121412905")
        self.assertAlmostEqual(record.size_mb, 3.14, places=2)
        self.assertTrue(record.path.startswith("/【大学堂读秀书库】"))
        self.assertIn("自杀心理危机干预", record.title)
        self.assertNotIn("whbhpfc", record.title)
        self.assertNotIn("公众号", record.title)

    def test_annotated_tree_epub_sibling(self) -> None:
        line = ("3│  │  │  │  自杀心理危机干预9787121412905_关注更新【公众号】知享书会【微信】whbhpfc.epub "
                "(755.72 KB)  --  /a/b/")
        record = parse_line(line)
        self.assertIsNotNone(record)
        self.assertEqual(record.suffix, ".epub")
        self.assertLess(record.size_mb, 1.0)

    def test_full_path_row(self) -> None:
        line = "/我的资源/秀2.0——4.0全集/洪老分享16-15c2-电子科技-8/8/一个人的贵族_12194166.zip"
        record = parse_line(line)
        self.assertIsNotNone(record)
        self.assertEqual(record.filename, "一个人的贵族_12194166.zip")
        self.assertEqual(record.ss, "12194166")
        self.assertTrue(record.is_archive)
        self.assertTrue(record.path.startswith("/我的资源/"))

    def test_full_path_row_with_advert_and_leading_comma(self) -> None:
        line = "/读秀书库5.0/1/2000-2/,14762772更多书籍更新请添加微信whbhpfc.zip"
        record = parse_line(line)
        self.assertIsNotNone(record)
        self.assertEqual(record.ss, "14762772")
        self.assertNotIn("whbhpfc", record.title)

    def test_utf16_tsv_row(self) -> None:
        line = TAB.join(["3", "/【大学堂】/111/8280本/1001-2000/", "我们的生存之道_14733797.pdf"])
        record = parse_line(line)
        self.assertIsNotNone(record)
        self.assertEqual(record.filename, "我们的生存之道_14733797.pdf")
        self.assertEqual(record.ss, "14733797")
        self.assertEqual(record.path, "/【大学堂】/111/8280本/1001-2000/")

    def test_directory_rows_are_skipped(self) -> None:
        for line in (
            "1│  ├─读秀书库6.0去重版0415 [文件夹大小:182.52 GB 子文件夹数: 5 子文件数: 2900]",
            "2│  │  ├─9787 [文件夹大小:6.53 GB 子文件夹数: 0 子文件数: 242]",
            "├─【大学堂读秀持续更新库】 ",
            "/我的资源/秀2.0——4.0全集/洪老分享16-15c2-电子科技-8/8",
            "关注书库后续更新请添加微信 whbhpfc",
        ):
            self.assertIsNone(parse_line(line), line)

    def test_utf16_file_is_decoded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "utf16.txt"
            rows = [
                TAB.join(["id", "parent_path", "server_filename"]),
                TAB.join(["1", "/a/b/", "强的补助力 冲刺_14474169.pdf"]),
            ]
            path.write_bytes((NEWLINE.join(rows) + NEWLINE).encode("utf-16"))
            records = list(parse_file(path))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].ss, "14474169")

    def test_mixed_legacy_encoding_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cat.txt"
            with path.open("wb") as handle:
                handle.write(("├── 新手学外贸_14425171.pdf" + NEWLINE).encode("utf-8"))
                handle.write(("├── 旧编码书名_15014541.pdf" + NEWLINE).encode("gb18030"))
                handle.write(("not a book line" + NEWLINE).encode("ascii"))
            records = list(parse_file(path, source="cat"))
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].ss, "14425171")
            self.assertEqual(records[1].ss, "15014541")
            self.assertTrue(all(record.source == "cat" for record in records))


class PromotionalTextTests(unittest.TestCase):
    def test_adverts_are_stripped_from_titles(self) -> None:
        for value in (
            "书名_关注更新【公众号】知享书会【微信】whbhpfc",
            "书名更多书籍更新请添加微信whbhpfc",
            "书名_关注公众号知享书会",
        ):
            self.assertEqual(strip_promotional(value), "书名", value)

    def test_clean_titles_are_untouched(self) -> None:
        self.assertEqual(strip_promotional("中国企业如何定战略"), "中国企业如何定战略")


class IsbnNormalisationTests(unittest.TestCase):
    def test_isbn13_passes_through(self) -> None:
        self.assertEqual(isbn13_of("9787520125901"), "9787520125901")

    def test_isbn10_converts_to_isbn13(self) -> None:
        self.assertEqual(isbn13_of("7206045049"), "9787206045042")

    def test_isbn10_with_x_check_digit(self) -> None:
        result = isbn13_of("780599644X")
        self.assertTrue(result.startswith("9787805996"))
        self.assertEqual(len(result), 13)

    def test_lowercase_check_digit_is_accepted(self) -> None:
        self.assertEqual(isbn13_of("780599644x"), isbn13_of("780599644X"))
        self.assertEqual(len(isbn13_of("780599644x")), 13)

    def test_stray_x_inside_the_body_is_rejected(self) -> None:
        # Feeding a check letter in the wrong position to the checksum used to
        # crash the site-side export.
        self.assertEqual(isbn13_of("x123456789"), "")
        self.assertEqual(isbn13_of("12x4567890"), "")

    def test_rubbish_is_rejected(self) -> None:
        for value in ("", "12345", "1234567890123", "abc"):
            self.assertEqual(isbn13_of(value), "", value)


class SiteKeyTests(unittest.TestCase):
    def test_positional_extraction(self) -> None:
        ss, isbn, title_key = keys_from_title("《澄怀古雅集》_赵毅著_96364595_9787511582447")
        self.assertEqual(ss, "96364595")
        self.assertEqual(isbn, "9787511582447")
        self.assertIn("澄怀古雅集", title_key)

    def test_old_style_isbn10_title(self) -> None:
        ss, isbn, _ = keys_from_title("《韩国政治民主化转型的力学》_（韩）權翼著_13524280_7206045049")
        self.assertEqual(ss, "13524280")
        self.assertEqual(isbn, "9787206045042")

    def test_missing_isbn_is_tolerated(self) -> None:
        ss, isbn, title_key = keys_from_title("《爱丽斯漫游奇境》_（英）卡罗尔著；金波主编_13524193_")
        self.assertEqual(ss, "13524193")
        self.assertEqual(isbn, "")
        self.assertTrue(title_key)

    def test_isbn_digits_never_become_an_ss_number(self) -> None:
        ss, isbn, _ = keys_from_title("《某书》_作者_9787520125901")
        self.assertEqual(ss, "")
        self.assertEqual(isbn, "9787520125901")


if __name__ == "__main__":
    unittest.main()
