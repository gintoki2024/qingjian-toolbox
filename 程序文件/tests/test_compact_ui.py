"""Behavior checks for the compact input bar; no processing worker is started."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian.app import MODES, Window
from qingjian.theme import STYLE


class CompactInputUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # QApplication is shared across the UI suites. Keep the real theme
        # for its lifetime; clearing it while older dialogs still exist can
        # break Qt's native style cleanup at interpreter exit on Windows.
        cls.app.setStyleSheet(STYLE)
        if not QFontDatabase.families() and Path("C:/Windows/Fonts/msyh.ttc").is_file():
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR"))
        self.root = Path(self.temporary.name)
        self.window = Window()
        self.window.show()
        self.pump()

    def tearDown(self):
        # Tests exercise the presentation/queue API only, so there is no real
        # worker whose lifetime could be truncated by closing the window.
        self.window.set_busy(False)
        self.window.close()
        self.window.deleteLater()
        self.pump()
        self.temporary.cleanup()

    def pump(self):
        for _ in range(3):
            self.app.processEvents()
            QTest.qWait(1)

    def source(self, name, subfolder=""):
        path = self.root / subfolder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"queue-only fixture; no document processor is run")
        return path.resolve()

    def toggle_list(self):
        QTest.mouseClick(self.window.manage_button, Qt.MouseButton.LeftButton)
        self.pump()

    def state_by_path(self):
        states = {}
        for row, path in enumerate(self.window.files[self.window.mode]):
            status = self.window.table.item(row, 2)
            states[path] = (status.text(), status.toolTip())
        return states

    def test_empty_queue_is_collapsed_and_has_no_visible_table_space(self):
        window = self.window
        self.assertEqual(window.files[window.mode], [])
        self.assertEqual(window.table.rowCount(), 0)
        self.assertTrue(window.source_bar.isVisible())
        self.assertFalse(window.manage_button.isChecked())
        self.assertTrue(window.queue_panel.isHidden())
        self.assertFalse(window.table.isVisible())
        self.assertFalse(window.settings.visibleRegion().isEmpty())

    def test_added_files_stay_compact_and_summary_matches_deduplicated_queue(self):
        window = self.window
        empty_height = window.source_bar.height()
        long_name = "长文件名用于验证紧凑摘要保持完整信息_" * 4 + ".docx"
        first = self.source(long_name)
        window.accept_sources([str(first), str(first)])
        self.pump()
        self.assertEqual(window.files[window.mode], [str(first)])
        self.assertIn(long_name, window.source_summary.full_text)
        self.assertIn(str(first), window.source_summary.toolTip())
        self.assertFalse(window.manage_button.isChecked())
        self.assertTrue(window.queue_panel.isHidden())
        self.assertLessEqual(window.source_bar.height(), empty_height + 2 * window.fontMetrics().height())
        second, third = self.source("second.docx"), self.source("third.docx")
        window.accept_sources([str(second), str(third), str(second)])
        self.pump()
        self.assertEqual(window.files[window.mode], [str(first), str(second), str(third)])
        self.assertEqual(window.table.rowCount(), 3)
        self.assertIn("3", window.source_summary.full_text + window.manage_button.text())
        self.assertTrue(window.queue_panel.isHidden())

    def test_expanded_queue_reorders_removes_and_preserves_states_by_full_path(self):
        window = self.window
        first = self.source("same-name.docx", "one")
        second = self.source("same-name.docx", "two")
        third = self.source("third.docx")
        window.accept_sources([str(first), str(second), str(third)])
        self.pump()
        collapsed_hint = window.workspace_scroll.widget().sizeHint().height()
        self.toggle_list()
        self.assertTrue(window.queue_panel.isVisible())
        self.assertTrue(window.table.isVisible())
        self.assertGreater(window.workspace_scroll.widget().sizeHint().height(), collapsed_hint)
        window.on_file_done(0, True, "完成 · 第一份请复核")
        window.on_file_done(1, False, "第二份的具体错误")
        # Keep the third item pending.  In a real active job reordering is
        # disabled, so a synthetic in-progress row here would violate that flow.
        expected = self.state_by_path()

        window.table.selectRow(1)
        QTest.mouseClick(window.up_button, Qt.MouseButton.LeftButton)
        self.pump()
        self.assertEqual(window.files[window.mode], [str(second), str(first), str(third)])
        self.assertEqual(self.state_by_path(), expected)
        window.table.selectRow(0)
        QTest.mouseClick(window.down_button, Qt.MouseButton.LeftButton)
        self.pump()
        self.assertEqual(window.files[window.mode], [str(first), str(second), str(third)])
        self.assertEqual(self.state_by_path(), expected)

        self.toggle_list()
        self.assertTrue(window.queue_panel.isHidden())
        self.toggle_list()
        self.assertEqual(self.state_by_path(), expected)
        window.switch_mode(MODES.index("pdf_pages"))
        self.pump()
        window.switch_mode(MODES.index("word"))
        self.pump()
        self.assertTrue(window.queue_panel.isHidden())
        self.assertFalse(window.manage_button.isChecked())
        self.assertEqual(self.state_by_path(), expected)

        self.toggle_list()
        window.table.selectRow(0)
        QTest.mouseClick(window.clear_button, Qt.MouseButton.LeftButton)
        self.pump()
        self.assertEqual(window.files[window.mode], [str(second), str(third)])
        self.assertEqual(self.state_by_path(), {path: state for path, state in expected.items() if path != str(first)})

    def test_all_pages_keep_compact_inputs_and_visible_actions_at_small_size(self):
        window = self.window
        window.resize(1050, 760)
        self.assertEqual(len(MODES), 27)
        for index, mode in enumerate(MODES):
            with self.subTest(mode=mode):
                window.switch_mode(index)
                self.pump()
                self.assertEqual(window.settings.currentIndex(), index)
                self.assertTrue(window.queue_panel.isHidden())
                self.assertFalse(window.table.isVisible())
                for control in (window.start_button, window.output_edit, window.choose_output_button):
                    if mode in {"content_search", "find_duplicates"} and control in (window.output_edit, window.choose_output_button):
                        self.assertFalse(control.isVisible())
                        continue
                    self.assertTrue(control.isVisible())
                    bounds = QRect(control.mapTo(window, QPoint(0, 0)), control.size())
                    self.assertTrue(window.rect().contains(bounds), f"{mode}: action is clipped")
                if mode not in {"content_search", "find_duplicates"}:
                    workspace_bottom = window.workspace_scroll.mapTo(window, QPoint(0, window.workspace_scroll.height())).y()
                    self.assertGreater(window.output_edit.mapTo(window, QPoint(0, 0)).y(), workspace_bottom)

    def test_busy_blocks_queue_mutations_and_both_drop_surfaces(self):
        window = self.window
        first, second = self.source("first.docx"), self.source("second.docx")
        window.accept_sources([str(first)])
        self.toggle_list()
        window.table.selectRow(0)
        original = window.files[window.mode].copy()
        window.set_busy(True)
        self.pump()
        self.assertTrue(window.manage_button.isEnabled())
        self.toggle_list()
        self.assertTrue(window.queue_panel.isHidden())
        self.toggle_list()
        self.assertTrue(window.queue_panel.isVisible())
        for control in (window.add_files_button, window.add_folder_button, window.clear_button,
                        window.up_button, window.down_button):
            self.assertFalse(control.isEnabled())
        self.assertFalse(window.source_bar.acceptDrops())
        self.assertFalse(window.table.acceptDrops())

        # Direct callbacks must be guarded too: disabling widgets alone does
        # not protect against queued drop signals or an already queued click.
        window.accept_sources([str(second)])
        window.remove_selected()
        window.move_selected(1)
        self.assertEqual(window.files[window.mode], original)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(second))])
        for surface in (window.source_bar, window.table.viewport()):
            enter = QDragEnterEvent(QPointF(8, 8), Qt.DropAction.CopyAction, mime,
                                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(surface, enter)
            drop = QDropEvent(QPointF(8, 8), Qt.DropAction.CopyAction, mime,
                              Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(surface, drop)
            self.pump()
        self.assertEqual(window.files[window.mode], original)
        window.set_busy(False)
        self.assertTrue(window.add_files_button.isEnabled())
        self.assertTrue(window.source_bar.acceptDrops())
        self.assertTrue(window.table.acceptDrops())

    def test_collapsed_source_bar_accepts_file_and_link_drops(self):
        window = self.window
        first = self.source("dropped.docx")
        for mode, url, expected in (
            ("word", QUrl.fromLocalFile(str(first)), str(first)),
            ("web", QUrl("https://example.org/article"), "https://example.org/article"),
        ):
            with self.subTest(mode=mode):
                window.switch_mode(MODES.index(mode))
                mime = QMimeData()
                mime.setUrls([url])
                enter = QDragEnterEvent(QPointF(8, 8), Qt.DropAction.CopyAction, mime,
                                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(window.source_bar, enter)
                self.assertTrue(enter.isAccepted())
                drop = QDropEvent(QPointF(8, 8), Qt.DropAction.CopyAction, mime,
                                  Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(window.source_bar, drop)
                self.pump()
                self.assertEqual(window.files[mode], [expected])
                self.assertTrue(window.queue_panel.isHidden())

    def test_long_fatal_error_remains_available_in_status_tooltip(self):
        window = self.window
        error = "无法创建结果目录：" + ("较长的文件夹名称/" * 35) + "权限不足"
        window.on_complete({"folder": "", "records": [], "fatal": error,
                            "report_error": "", "cancelled": False, "total": 0})
        self.pump()
        self.assertIn(error, window.status.toolTip())
        self.assertFalse(window.result_button.isEnabled())
        self.assertLessEqual(window.status.height(), 42)


if __name__ == "__main__":
    unittest.main()
