"""Integration coverage for the desktop workflow and dispatch boundary."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtCore import QPoint

from qingjian.app import Window
from qingjian.batch import output_path, validate_options


class BatchUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")

    def test_output_extensions_and_disambiguation(self):
        folder = Path("out")
        self.assertEqual(output_path("convert", "a.docx", folder, {}, 0).suffix, ".pdf")
        self.assertEqual(output_path("convert", "a.pdf", folder, {}, 0).suffix, ".docx")
        self.assertEqual(output_path("image", "a.png", folder, {"format": "jpg"}, 0).suffix, ".jpg")
        self.assertNotEqual(output_path("word", "one/a.docx", folder, {}, 0),
                            output_path("word", "two/a.docx", folder, {}, 1))

    def test_word_options_reject_bad_regex_and_no_operation(self):
        with self.assertRaises(ValueError):
            validate_options("word", {})
        with self.assertRaises(ValueError):
            validate_options("word", {"replacements": [{"find": "[", "replace": "x", "regex": True}]})
        validate_options("word", {"mask_phone": True})

    def test_real_ui_batch_continues_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            broken = folder / "broken.png"
            broken.write_text("not an image", encoding="utf-8")
            source = folder / "photo.png"
            Image.new("RGBA", (200, 120), (10, 80, 120, 90)).save(source)
            original = source.read_bytes()
            window = Window()
            window.switch_mode(3)
            window.accept_sources([str(broken), str(source), str(source)])
            self.assertEqual(window.table.rowCount(), 2)
            window.i_mode.setCurrentIndex(1)
            window.i_format.setCurrentIndex(2)
            window.output_edit.setText(str(folder / "results"))
            window.start_task()
            self.assertFalse(window.start_button.isEnabled())
            deadline = time.monotonic() + 20
            while window.worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
                QTest.qWait(10)
            self.assertIsNone(window.worker, "worker did not finish")
            self.assertTrue(window.start_button.isEnabled())
            records = window.last_result["records"]
            self.assertEqual([r["状态"] for r in records], ["失败", "成功"])
            target = Path(records[1]["输出"])
            with Image.open(target) as cropped:
                self.assertEqual(cropped.size, (120, 120))
                self.assertEqual(cropped.format, "JPEG")
            self.assertEqual(source.read_bytes(), original)
            report = json.loads((target.parent / "处理记录.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["结果"]), 2)
            self.assertTrue(window.result_button.isEnabled())
            window.close()

    def test_word_rule_round_trip(self):
        window = Window()
        values = {"format_enabled": False, "mask_email": True, "font_size": 16,
                  "custom_sensitive": ["张三"], "replacements": [{"find": "旧文字", "replace": "新文字", "regex": False}]}
        window.apply_word_options(values)
        actual = window.get_options()
        for key in values:
            self.assertEqual(actual[key], values[key])
        window.close()

    def test_unreadable_folder_shows_message(self):
        with tempfile.TemporaryDirectory() as temp:
            window = Window()
            with patch.object(Path, "iterdir", side_effect=PermissionError("test ACL")), \
                    patch("qingjian.app.QMessageBox.warning") as warning:
                window.accept_sources([temp])
                warning.assert_called_once()
            self.assertEqual(window.table.rowCount(), 0)
            window.close()

    def test_small_window_keeps_action_bar_outside_scrolling_workspace(self):
        window = Window()
        window.resize(1050, 760)
        window.show()
        self.app.processEvents()
        workspace_bottom = window.workspace_scroll.mapTo(window, QPoint(0, window.workspace_scroll.height())).y()
        output_top = window.output_edit.mapTo(window, QPoint(0, 0)).y()
        self.assertGreater(output_top, workspace_bottom)
        self.assertLessEqual(window.height(), 760)
        self.assertTrue(window.start_button.isVisible())
        window.close()


if __name__ == "__main__":
    unittest.main()
