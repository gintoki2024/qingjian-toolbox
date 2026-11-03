"""Real Qt layout and input checks for the compact homepage drop targets."""
from __future__ import annotations

import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian.quick_ui import QuickPanel, QUICK_STYLE
from qingjian.theme import STYLE


class CompactQuickPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if Path("C:/Windows/Fonts/msyh.ttc").is_file():
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
        cls.app.setStyleSheet(STYLE + QUICK_STYLE)

    def setUp(self):
        self.panel = QuickPanel()
        self.panel.set_compact(True)
        self.panel.resize(360, 338)
        self.panel.show()
        self.pump()

    def tearDown(self):
        QApplication.clipboard().clear()
        self.panel.close()
        self.panel.deleteLater()
        self.pump()

    def pump(self):
        for _ in range(3):
            self.app.processEvents()

    def assert_inside(self, widget, parent):
        rectangle = QRect(widget.mapTo(parent, QPoint(0, 0)), widget.size())
        self.assertGreaterEqual(rectangle.left(), 0, widget.objectName())
        self.assertLess(rectangle.right(), parent.width(), widget.objectName())

    def test_six_reachable_targets_in_two_columns_at_small_window_widths(self):
        for width in (280, 290, 360, 420, 520):
            with self.subTest(width=width):
                self.panel.resize(width, 338)
                self.panel.begin("web_to_word", 8)
                self.pump()
                cards = list(self.panel.cards.values())
                self.assertEqual(len(cards), 6)
                self.assertEqual(len({card.y() for card in cards}), 3)
                self.assertEqual(len({card.x() for card in cards}), 2)
                self.assertLessEqual(self.panel.minimumSizeHint().height(), 340)
                self.assertLessEqual(self.panel.sizeHint().height(), 340)
                for card in cards:
                    self.assertTrue(card.isVisible())
                    self.assertGreaterEqual(card.height(), 64)
                    self.assertLessEqual(card.height(), 72)
                    self.assert_inside(card, self.panel.grid)
                    self.assert_inside(card.title_label, card)
                    self.assert_inside(card.hint_label, card)
                    self.assertTrue(card.description_label.isHidden())
                    self.assertNotIn("Ctrl+V", card.hint_label.text())
                    self.assertIn("Ctrl+V", card.toolTip())
                    self.assertIn(card.spec["description"], card.accessibleDescription())
                for control in (self.panel.url_edit, self.panel.paste_button,
                                self.panel.choose_folder_button, self.panel.cancel_button):
                    self.assert_inside(control, self.panel)

    def test_status_and_errors_stay_bounded_with_full_details_accessible(self):
        long_url = "https://example.test/" + "非常长的网页路径" * 2000
        self.panel.begin("web_to_word", 2)
        self.panel.update_progress(1, 2, long_url)
        self.pump()
        self.assertEqual(self.panel.status_label.toolTip(), long_url)
        self.assertEqual(self.panel.status_label.accessibleDescription(), long_url)
        self.assertLessEqual(self.panel.status_label.height(), 28)
        self.assertLessEqual(self.panel.state.height(), 44)
        self.assertLessEqual(self.panel.sizeHint().height(), 340)
        failure = "保存失败：" + "目录不可写，" * 1000
        self.panel.finish({"total": 2, "errors": [failure], "records": [], "outputs": []})
        self.panel.notice(failure)
        self.pump()
        self.assertEqual(self.panel.status_label.toolTip(), failure)
        self.assertLessEqual(self.panel.state.height(), 44)
        self.assertTrue(self.panel.result_button.isEnabled())
        for control in (self.panel.result_button, self.panel.open_folder_button):
            self.assertTrue(control.isVisible())
            self.assert_inside(control, self.panel)
        self.panel.url_folder = Path("C:/" + "较长的保存位置/" * 100)
        self.panel.resize(420, 338)
        self.pump()
        self.assertIn(str(self.panel.url_folder), self.panel.folder_label.toolTip())
        self.assertIn("本地文件存回原文件夹", self.panel.folder_label.toolTip())
        self.assert_inside(self.panel.choose_folder_button, self.panel)

    def test_standard_layout_restores_after_compact_mode(self):
        self.panel.resize(900, 430)
        self.panel.set_compact(False)
        self.pump()
        self.assertEqual(len({card.y() for card in self.panel.cards.values()}), 2)
        self.assertEqual(len({card.x() for card in self.panel.cards.values()}), 3)
        for card in self.panel.cards.values():
            self.assertTrue(card.description_label.isVisible())
            self.assertIn("Ctrl+V", card.hint_label.text())
            self.assertGreaterEqual(card.height(), 102)
        self.assertEqual(self.panel.choose_folder_button.text(), "更改链接保存位置")
        self.assertEqual(self.panel.paste_button.text(), "粘贴并转换")
        self.panel.set_compact(True)
        self.panel.resize(420, 338)
        self.pump()
        self.assertEqual(len({card.y() for card in self.panel.cards.values()}), 3)
        self.assertLessEqual(self.panel.sizeHint().height(), 340)

    def test_extremely_narrow_panel_uses_one_column_without_overflow(self):
        self.panel.resize(260, 600)
        self.pump()
        self.assertEqual(len({card.x() for card in self.panel.cards.values()}), 1)
        self.assertEqual(len({card.y() for card in self.panel.cards.values()}), 6)
        for card in self.panel.cards.values():
            self.assert_inside(card, self.panel.grid)

    def test_compact_clipboard_and_shift_drop_route_once_as_copy(self):
        emitted = []
        self.panel.submitted.connect(lambda action, values: emitted.append((action, values)))
        url = "https://example.test/compact"
        mime = QMimeData()
        mime.setUrls([QUrl(url)])
        QApplication.clipboard().setMimeData(mime)
        self.panel.url_edit.setFocus()
        QTest.keyClick(self.panel.url_edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(emitted, [("web_to_word", [url])])
        card = self.panel.cards["web_to_word"]
        actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
        enter = QDragEnterEvent(QPoint(10, 10), actions, mime, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.ShiftModifier)
        self.app.sendEvent(card, enter)
        drop = QDropEvent(QPointF(10, 10), actions, mime, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.ShiftModifier)
        self.app.sendEvent(card, drop)
        self.assertTrue(enter.isAccepted())
        self.assertTrue(drop.isAccepted())
        self.assertEqual(drop.dropAction(), Qt.DropAction.CopyAction)
        self.assertEqual(emitted, [("web_to_word", [url])] * 2)

    def test_invalid_target_drop_is_consumed_and_stop_remains_available(self):
        emitted = []
        self.panel.submitted.connect(lambda *values: emitted.append(values))
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.test/not-an-image")])
        card = self.panel.cards["image_to_jpg"]
        enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(card, enter)
        drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(card, drop)
        self.assertTrue(drop.isAccepted())
        self.assertFalse(emitted)
        self.assertTrue(self.panel.status_label.text())
        cancelled = []
        self.panel.cancel_requested.connect(lambda: cancelled.append(True))
        self.panel.begin("web_to_word", 1)
        self.pump()
        QTest.mouseClick(self.panel.cancel_button, Qt.MouseButton.LeftButton)
        self.assertEqual(cancelled, [True])
        self.assertIn("正在停止", self.panel.status_label.text())
        self.assertEqual(self.panel.status_label.text(), self.panel.status_label.toolTip())
        self.assertFalse(self.panel.cancel_button.isEnabled())
        self.assertTrue(all(not card.isEnabled() for card in self.panel.cards.values()))


if __name__ == "__main__":
    unittest.main()
