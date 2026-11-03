"""Real Qt home panel behavior, using only generated local fixtures."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian.home_content import ContentSnapshot, QuoteItem
from qingjian.home_content_ui import HomeContentSettingsDialog, PersonalHomePanel, read_display_image
from qingjian.notebook_store import NotebookStore
from qingjian.theme import STYLE


class HomeContentUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR"))
        self.root = Path(self.temp.name)
        self.panel = PersonalHomePanel(self.root / "home_content.json")
        self.panel.resize(460, 300)
        self.panel.show()
        self.pump()

    def tearDown(self):
        self.wait_until(lambda: not self.panel._jobs)
        self.panel.close()
        self.panel.deleteLater()
        self.pump()
        self.temp.cleanup()

    def pump(self):
        for _ in range(4):
            self.app.processEvents()
            QTest.qWait(3)

    def wait_until(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            QTest.qWait(8)
        self.assertTrue(predicate())

    def materials(self):
        folder = self.root / "素材"
        folder.mkdir()
        for name, color in (("第一张.png", "#7a9ca0"), ("第二张.png", "#a79caf")):
            image = QImage(1200, 600, QImage.Format_RGB32)
            image.fill(QColor(color))
            self.assertTrue(image.save(str(folder / name)))
        (folder / "句子.txt").write_text("<b>作为原文显示</b>\n\n第二段。\n还有下一行。", "utf-8")
        return folder

    def load_materials(self):
        folder = self.materials()
        self.panel.apply_settings({"folder": str(folder), "display_mode": "images"})
        self.wait_until(lambda: len(self.panel.snapshot.images) == 2 and not self.panel._jobs
                        and not self.panel._resize_timer.isActive() and not self.panel.image_view.image.isNull())
        self.pump()
        return folder

    def test_default_is_compact_and_has_only_one_material_hint(self):
        self.assertLessEqual(self.panel.sizeHint().height(), 285)
        self.assertFalse(self.panel.empty.isVisible())
        self.assertFalse(self.panel.image_view.isVisible())
        self.assertFalse(self.panel.quote_box.isVisible())
        self.assertTrue(self.panel.reminders.isVisible())
        self.assertFalse(self.panel.image_timer.isActive())

    def test_content_buttons_select_exactly_one_large_pane_and_remember_choice(self):
        self.load_materials()
        self.assertFalse(self.panel.image_view.image.isNull())
        for mode, view in (("images", self.panel.image_view), ("quotes", self.panel.quote_box), ("reminders", self.panel.reminders)):
            QTest.mouseClick(self.panel.mode_buttons[mode], Qt.LeftButton)
            self.pump()
            panes = [self.panel.image_view, self.panel.quote_box, self.panel.reminders]
            self.assertEqual([pane for pane in panes if pane.isVisible()], [view])
            self.assertGreaterEqual(view.height(), 190)
            self.assertEqual(self.panel.settings["display_mode"], mode)
            from qingjian.home_content import HomeContentStore
            self.assertEqual(HomeContentStore(self.panel.store.path).settings["display_mode"], mode)
            self.assertEqual(sum(button.isChecked() for button in self.panel.mode_buttons.values()), 1)
        self.assertLessEqual(self.panel.sizeHint().height(), 310)
        self.assertEqual(self.panel.quote_text.textFormat(), Qt.PlainText)
        self.assertEqual(self.panel.quote_text.text(), "<b>作为原文显示</b>")

    def test_hidden_panel_stops_all_timers_and_show_restarts(self):
        self.load_materials()
        self.assertTrue(self.panel.image_timer.isActive())
        self.assertFalse(self.panel.quote_timer.isActive())
        self.assertFalse(self.panel.reminder_timer.isActive())
        self.panel.hide()
        self.pump()
        for timer in (self.panel.image_timer, self.panel.quote_timer, self.panel.reminder_timer):
            self.assertFalse(timer.isActive())
        index = self.panel.image_rotation.index
        self.panel._step_image(1, automatic=True)
        self.assertEqual(index, self.panel.image_rotation.index)
        self.panel.show()
        self.pump()
        self.assertTrue(self.panel.image_timer.isActive())

    def test_fixed_item_stays_put_and_manual_switch_is_remembered(self):
        self.load_materials()
        key = self.panel.snapshot.quotes[1].key
        self.panel.apply_settings({"display_mode": "quotes", "quote_mode": "single", "fixed_quote": key})
        self.assertFalse(self.panel.quote_timer.isActive())
        self.assertEqual(self.panel.quote_rotation.current, key)
        self.panel._step_quote(1, automatic=True)
        self.assertEqual(self.panel.quote_rotation.current, key)
        self.panel._step_quote(-1)
        self.assertEqual(self.panel.settings["fixed_quote"], self.panel.snapshot.quotes[0].key)
        self.assertIn("固定", self.panel.quote_caption.text())

    def test_timer_rotates_from_snapshot_without_rescanning_folder(self):
        self.load_materials()
        with patch("qingjian.home_content_ui.scan_content", side_effect=AssertionError("unexpected scan")):
            self.panel.select_mode("quotes")
            self.panel._step_quote(1, automatic=True)
            self.panel.select_mode("images")
            self.panel._step_image(1, automatic=True)
            self.wait_until(lambda: not self.panel._jobs)
        self.assertEqual(self.panel.quote_rotation.index, 1)

    def test_late_image_result_does_not_replace_a_cached_previous_selection(self):
        self.load_materials()
        original_color = self.panel.image_view.image.pixelColor(1, 1)
        self.panel._step_image(1)
        self.panel._step_image(-1)
        self.wait_until(lambda: not self.panel._jobs)
        self.assertEqual(self.panel.image_rotation.index, 0)
        self.assertEqual(self.panel.image_view.image.pixelColor(1, 1), original_color)

    def test_resizing_keeps_current_picture_until_rescaled_image_is_ready(self):
        self.load_materials()
        original_color = self.panel.image_view.image.pixelColor(1, 1)
        started, release = threading.Event(), threading.Event()

        def slow_decode(path, size):
            started.set()
            release.wait(5)
            return read_display_image(path, size)

        with patch("qingjian.home_content_ui.read_display_image", side_effect=slow_decode):
            try:
                self.panel.resize(self.panel.width() + 37, self.panel.height() + 47)
                self.pump()
                self.panel._show_image()
                self.wait_until(started.is_set)
                self.assertFalse(self.panel.image_view.image.isNull())
                self.assertEqual(self.panel.image_view.image.pixelColor(1, 1), original_color)
            finally:
                release.set()
            self.wait_until(lambda: not self.panel._jobs and not self.panel._resize_timer.isActive())
        self.assertFalse(self.panel.image_view.image.isNull())

    def test_rapid_clicks_and_resizes_keep_one_decoder_and_only_latest_request(self):
        self.load_materials()
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow_decode(path, size):
            calls.append((path, size.width(), size.height()))
            if len(calls) == 1:
                started.set()
                release.wait(5)
            return read_display_image(path, size)

        with patch("qingjian.home_content_ui.read_display_image", side_effect=slow_decode):
            try:
                self.panel._step_image(1)
                self.wait_until(started.is_set)
                for index in range(30):
                    self.panel.resize(461 + index, 300)
                    self.app.processEvents()
                    self.panel._resize_timer.stop()
                    self.panel._step_image(1)
                    self.assertLessEqual(sum(kind == "image" for kind, token in self.panel._jobs), 1)
                self.panel.image_rotation.index = 1
                self.panel._show_image()
                latest = self.panel._pending_image_request
                self.assertIsNotNone(latest)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
            self.wait_until(lambda: not self.panel._jobs)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], (latest[1], latest[2].width(), latest[2].height()))
        self.assertIsNone(self.panel._pending_image_request)
        self.assertEqual(self.panel.image_rotation.index, 1)

    def test_full_picture_is_drawn_above_its_caption_bar(self):
        self.load_materials()
        view = self.panel.image_view
        area = view.image_area()
        self.assertEqual(area.bottom() + 1, view.bar.geometry().top())
        self.panel.image_timer.stop()
        self.panel._resize_timer.stop()
        self.wait_until(lambda: not self.panel._jobs)
        image = QImage(200, 100, QImage.Format_RGB32)
        image.fill(QColor("#df4d4d"))
        for y in range(85, 100):
            for x in range(200):
                image.setPixelColor(x, y, QColor("#438b68"))
        view.set_image(image)
        self.pump()
        rendered = view.grab().toImage()
        ratio = rendered.devicePixelRatioF()
        target_height = min(area.height(), area.width() / 2)
        image_bottom = (area.height() + target_height) / 2
        pixel = rendered.pixelColor(int(view.width() * ratio / 2), int((image_bottom - 2) * ratio))
        self.assertEqual(pixel, QColor("#438b68"))

    def test_clearing_folder_during_refresh_restores_button_and_ignores_late_scan(self):
        folder = self.load_materials()
        previous = self.panel.snapshot
        started, release = threading.Event(), threading.Event()

        def slow_scan(path):
            started.set()
            release.wait(5)
            return previous

        with patch("qingjian.home_content_ui.scan_content", side_effect=slow_scan):
            try:
                self.panel.refresh_content()
                self.wait_until(started.is_set)
                self.assertFalse(self.panel.refresh_button.isEnabled())
                self.panel.apply_settings({"folder": ""})
                self.assertTrue(self.panel.refresh_button.isEnabled())
                self.assertFalse(self.panel.snapshot.images)
            finally:
                release.set()
            self.wait_until(lambda: not self.panel._jobs)
        self.assertTrue(self.panel.refresh_button.isEnabled())
        self.assertEqual(self.panel.snapshot.folder, "")
        self.assertFalse(self.panel.image_view.isVisible())

    def test_manual_refresh_picks_up_edited_text(self):
        folder = self.load_materials()
        (folder / "句子.txt").write_text("新的内容", "utf-8")
        self.assertNotEqual(self.panel.quote_text.text(), "新的内容")
        self.panel.refresh_content()
        self.wait_until(lambda: len(self.panel.snapshot.quotes) == 1 and not self.panel._jobs)
        self.assertEqual(self.panel.quote_text.text(), "新的内容")

    def test_missing_and_broken_images_have_readable_empty_state(self):
        folder = self.root / "不存在"
        self.panel.apply_settings({"folder": str(folder), "display_mode": "images"})
        self.wait_until(lambda: not self.panel._jobs)
        self.assertIn("无法访问", self.panel.notice.text())
        folder.mkdir()
        (folder / "broken.png").write_bytes(b"not an image")
        self.panel.refresh_content()
        self.wait_until(lambda: len(self.panel.snapshot.images) == 1 and not self.panel._jobs)
        self.assertTrue(self.panel.image_view.image.isNull())
        self.assertTrue(self.panel.image_view.message)

    def test_image_decoding_is_scaled_and_file_is_unchanged(self):
        folder = self.materials()
        path = folder / "第一张.png"
        original = path.read_bytes()
        image, error = read_display_image(path, QSize(200, 100))
        self.assertFalse(error)
        self.assertLessEqual(image.width(), 200)
        self.assertLessEqual(image.height(), 100)
        self.assertEqual(path.read_bytes(), original)

    def test_oversized_image_rejected_before_decode(self):
        path = self.root / "large.png"
        with path.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
        image, error = read_display_image(path, QSize(200, 100))
        self.assertTrue(image.isNull())
        self.assertIn("32 MB", error)

    def test_reminder_button_and_preview_leave_notebook_unchanged(self):
        store = NotebookStore()
        store.upsert_task({"title": "虚构示例", "date": "2000-01-01", "remind_at": "2000-01-01T09:00:00"})
        revision = store.revision
        self.panel.set_notebook_store(store)
        self.assertEqual(store.revision, revision)
        self.assertFalse(store.pending_alerts)
        received = []
        self.panel.notebook_requested.connect(lambda: received.append(True))
        QTest.mouseClick(self.panel.notebook_button, Qt.LeftButton)
        self.assertEqual(received, [True])
        with patch.object(store, "collect_due", side_effect=AssertionError("consumed reminder")):
            self.panel.refresh_reminders()
        self.assertEqual(store.revision, revision)

    def test_settings_cancel_is_draft_and_empty_selected_mode_has_one_hint(self):
        before = self.panel.settings
        dialog = HomeContentSettingsDialog(before, self.panel.snapshot, self.panel)
        dialog.display_mode.setCurrentIndex(dialog.display_mode.findData("quotes"))
        dialog.reject()
        self.assertEqual(self.panel.settings, before)
        dialog.deleteLater()
        self.panel.select_mode("quotes")
        self.assertTrue(self.panel.empty.isVisible())
        self.assertFalse(self.panel.reminders.isVisible())
        self.assertFalse(self.panel.reminder_timer.isActive())

    def test_long_quote_can_scroll_and_narrow_width_has_no_horizontal_bar(self):
        self.load_materials()
        self.panel.select_mode("quotes")
        snapshot = ContentSnapshot(self.panel.snapshot.images, (QuoteItem("long", "这是一段较长的话。" * 120, "fixture.txt"),), folder=self.panel.settings["folder"])
        self.panel._apply_snapshot(snapshot)
        self.panel.resize(400, self.panel.sizeHint().height())
        self.pump()
        self.assertGreater(self.panel.quote_scroll.verticalScrollBar().maximum(), 0)
        self.assertFalse(self.panel.quote_scroll.horizontalScrollBar().isVisible())

    def test_inactive_mode_does_not_advance_or_decode_and_switch_starts_only_its_timer(self):
        self.load_materials()
        self.panel.select_mode("quotes")
        self.wait_until(lambda: not self.panel._jobs)
        index = self.panel.image_rotation.index
        with patch("qingjian.home_content_ui.read_display_image", side_effect=AssertionError("inactive decode")):
            self.panel._step_image(1, automatic=True)
            self.panel._show_image()
            self.pump()
        self.assertEqual(self.panel.image_rotation.index, index)
        self.assertTrue(self.panel.quote_timer.isActive())
        self.assertFalse(self.panel.image_timer.isActive())
        self.panel.select_mode("reminders")
        self.assertTrue(self.panel.reminder_timer.isActive())
        self.assertFalse(self.panel.quote_timer.isActive())

    def test_failed_mode_save_restores_visible_page_and_selected_button(self):
        with patch.object(self.panel.store, "update", side_effect=OSError("只读测试")):
            QTest.mouseClick(self.panel.mode_buttons["images"], Qt.LeftButton)
        self.assertTrue(self.panel.reminders.isVisible())
        self.assertTrue(self.panel.mode_buttons["reminders"].isChecked())
        self.assertIn("只读", self.panel.notice.text())


if __name__ == "__main__":
    unittest.main()
