"""Safe output invariants shared by the newly added tools."""
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook
from qingjian.core.common import unique_path, write_report_book


class CommonOutputTests(unittest.TestCase):
    def test_long_names_keep_extensions_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            first = unique_path(folder, "通知" * 150 + ".docx")
            self.assertEqual(first.suffix, ".docx")
            first.write_text("original", encoding="utf-8")
            second = unique_path(folder, "通知" * 150 + ".docx")
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(encoding="utf-8"), "original")

    def test_source_text_is_never_converted_to_a_formula(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "report.xlsx"
            write_report_book(target, {"记录": [["原文"], ["=1+1"], ["001"]]})
            book = load_workbook(target, data_only=False)
            self.assertEqual(book.active["A2"].data_type, "s")
            self.assertEqual(book.active["A2"].value, "=1+1")
            self.assertEqual(book.active["A3"].value, "001")
            book.close()

    def test_invalid_control_character_is_an_error_not_a_changed_identifier(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "report.xlsx"
            with self.assertRaisesRegex(ValueError, "U\\+0001"):
                write_report_book(target, {"记录": [["编号"], ["AB\x01CD"]]})
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
