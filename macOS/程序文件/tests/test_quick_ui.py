"""Legacy input widgets in isolation plus current floating-converter results."""
from __future__ import annotations

import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from docx import Document
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian.app import MODES, Window
from qingjian.core.common import CancelledError
from qingjian.conversion_session import ConversionSession
from qingjian.profile import ProfileStore
from qingjian.quick_ui import QuickPanel
from qingjian.theme import STYLE


class QuickChannelUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(STYLE)
        if not QFontDatabase.families() and Path("C:/Windows/Fonts/msyh.ttc").is_file():
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR"))
        self.root = Path(self.temporary.name)
        self.window = Window(start_home=True, data_path=self.root / "profile.json")
        # Keep input-widget regressions without constructing a hidden shortcut
        # panel in the product homepage or using it as conversion state.
        self.panel = QuickPanel(self.root / "legacy-input-settings.json")
        self.panel.submitted.connect(self.window.start_quick_action)
        self.panel.resize(900, 430)
        self.panel.show()
        self.window.conversion_session.url_folder = self.root / "链接结果"
        self.window.show()
        self.window.show_converter()
        self.floating = self.window.converter_window
        self.panel.activateWindow()
        self.pump()

    def tearDown(self):
        if self.window.worker is not None:
            self.window.worker.requestInterruption()
            self.wait_for_worker()
        QApplication.clipboard().clear()
        self.panel.close()
        self.panel.deleteLater()
        self.floating.shutdown()
        self.floating.deleteLater()
        self.window.close()
        self.window.deleteLater()
        self.pump()
        self.temporary.cleanup()

    def pump(self, count=4):
        for _ in range(count):
            self.app.processEvents()
            QTest.qWait(2)

    def wait_until(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.app.processEvents()
        self.assertTrue(predicate(), "Qt worker or UI did not reach the expected state")

    def wait_for_worker(self):
        self.wait_until(lambda: self.window.worker is None, 30)
        self.pump()

    def image(self, name="来源/测试 图片.png"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (64, 40), (120, 60, 30, 128)).save(path)
        return path.resolve()

    def html(self, name="网页来源/已存网页.html"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('<meta charset="utf-8"><article><h1>快捷网页</h1>'
                        '<p>无需设置即可转换的中文正文。</p></article>', encoding="utf-8")
        return path.resolve()

    def clipboard(self, *, urls=(), text=""):
        mime = QMimeData()
        if urls:
            mime.setUrls([value if isinstance(value, QUrl) else QUrl(value) for value in urls])
        if text:
            mime.setText(text)
        QApplication.clipboard().setMimeData(mime)
        self.pump(1)

    def drop(self, widget, *, urls=(), text=""):
        mime = QMimeData()
        if urls:
            mime.setUrls([value if isinstance(value, QUrl) else QUrl(value) for value in urls])
        if text:
            mime.setText(text)
        enter = QDragEnterEvent(QPoint(8, 8), Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(widget, enter)
        event = QDropEvent(QPointF(8, 8), Qt.DropAction.CopyAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(widget, event)
        self.pump(1)
        return enter.isAccepted(), event.isAccepted()

    def test_actual_html_drop_finishes_beside_source_and_preserves_tool_state(self):
        window = self.window
        queued = self.root / "原有工具队列.docx"
        document = Document()
        document.add_paragraph("保留原来的工具任务")
        document.save(queued)
        window.accept_sources([str(queued)])
        window.f_size.setValue(17)
        window.w_images.setChecked(False)
        window.p_mode.setCurrentIndex(1)
        window.p_password.setText("仅测试已有设置")
        window.output_edit.setText(str(self.root / "普通任务结果"))
        files = copy.deepcopy(window.files)
        states = copy.deepcopy(window.queue_states)
        settings = {mode: copy.deepcopy(window.get_options(mode)) for mode in MODES}
        mode = window.mode
        source = self.html()
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        window.show_surface("home")
        self.pump()
        entered, dropped = self.drop(self.panel.cards["web_to_word"], urls=[QUrl.fromLocalFile(str(source))])
        self.assertTrue(entered)
        self.assertTrue(dropped)
        self.wait_for_worker()
        result = window.quick_result
        self.assertEqual(result["records"][0]["状态"], "成功")
        target = Path(result["records"][0]["输出"])
        self.assertEqual(target.parent, source.parent)
        self.assertIn("无需设置即可转换", "".join(p.text for p in Document(target).paragraphs))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)
        self.assertEqual(window.files, files)
        self.assertEqual(window.queue_states, states)
        self.assertEqual(window.mode, mode)
        self.assertEqual({key: window.get_options(key) for key in MODES}, settings)
        self.assertEqual(window.output_edit.text(), str(self.root / "普通任务结果"))
        self.assertEqual(window.profile_store.history[0]["outputs"], [str(target)])
        self.assertEqual(window.profile_store.history[0]["scene"], "quick:web_to_word")
        reloaded = ProfileStore(self.root / "profile.json")
        self.assertEqual(reloaded.history[0]["outputs"], [str(target)])
        self.assertEqual(list(source.parent.glob(".qingjian-quick-*")), [])

    def test_card_ctrl_v_explorer_file_uri_starts_actual_image_conversion(self):
        source = self.image()
        original = source.read_bytes()
        self.clipboard(urls=[QUrl.fromLocalFile(str(source))], text="资源管理器中的说明不会成为另一项输入")
        card = self.panel.cards["image_to_jpg"]
        card.setFocus()
        QTest.keyClick(card, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.wait_for_worker()
        row = self.window.quick_result["records"][0]
        self.assertEqual(row["输入"], str(source))
        target = Path(row["输出"])
        self.assertEqual(target.parent, source.parent)
        self.assertEqual(target.suffix, ".jpg")
        with Image.open(target) as converted:
            self.assertEqual(converted.size, (64, 40))
            self.assertEqual(converted.mode, "RGB")
        self.assertEqual(source.read_bytes(), original)

    def test_explorer_move_proposal_is_forced_to_copy_and_source_survives(self):
        source = self.image()
        original = source.read_bytes()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        card = self.panel.cards["image_to_jpg"]
        actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
        enter = QDragEnterEvent(QPoint(8, 8), actions, mime, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(enter.proposedAction(), Qt.DropAction.MoveAction)
        self.app.sendEvent(card, enter)
        self.assertTrue(enter.isAccepted())
        self.assertEqual(enter.dropAction(), Qt.DropAction.CopyAction)
        drop = QDropEvent(QPointF(8, 8), actions, mime, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(drop.proposedAction(), Qt.DropAction.MoveAction)
        self.app.sendEvent(card, drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(drop.dropAction(), Qt.DropAction.CopyAction)
        self.wait_for_worker()
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(Path(self.window.quick_result["records"][0]["输出"]).is_file())

    def test_web_input_ctrl_v_file_uri_immediately_converts_without_enter(self):
        source = self.html("带 空格/本地 网页.xhtml")
        self.clipboard(text=QUrl.fromLocalFile(str(source)).toString())
        self.panel.url_edit.setFocus()
        QTest.keyClick(self.panel.url_edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.wait_for_worker()
        target = Path(self.window.quick_result["records"][0]["输出"])
        self.assertEqual(target.parent, source.parent)
        self.assertEqual(target.suffix, ".docx")
        self.assertEqual(self.panel.url_edit.text(), "")

    def test_click_select_file_starts_and_keeps_existing_same_name_result(self):
        source = self.image()
        existing = source.with_suffix(".jpg")
        existing.write_bytes(b"previous output must survive")
        with patch("qingjian.quick_ui.QFileDialog.getOpenFileNames", return_value=([str(source)], "")) as picker:
            QTest.mouseClick(self.panel.cards["image_to_jpg"], Qt.MouseButton.LeftButton)
        self.wait_for_worker()
        picker.assert_called_once()
        target = Path(self.window.quick_result["records"][0]["输出"])
        self.assertEqual(target.parent, source.parent)
        self.assertNotEqual(target, existing)
        self.assertEqual(existing.read_bytes(), b"previous output must survive")
        with Image.open(target) as image:
            self.assertEqual(image.size, (64, 40))

    def test_invalid_drop_and_clipboard_show_reason_without_starting(self):
        invalid = self.root / "不支持.exe"
        invalid.write_bytes(b"not executable; test fixture only")
        # A rejected legacy input must not fall through into the
        # toolbox's generic recommendation dialog. Mock the modal boundary so a
        # regression gives an assertion instead of leaving unattended Qt stuck.
        with patch("qingjian.app.SourceActionDialog") as recommendation:
            self.drop(self.panel.cards["to_pdf"], urls=[QUrl.fromLocalFile(str(invalid))])
        recommendation.assert_not_called()
        self.assertIsNone(self.window.worker)
        self.clipboard(text="这些普通文字不应成为输入")
        QTest.keyClick(self.panel.cards["to_pdf"], Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.assertIsNone(self.window.worker)
        self.assertTrue(self.panel.state.isVisible())
        self.assertTrue(self.panel.status_label.text())
        self.panel.url_edit.setText("javascript:alert('never execute')")
        QTest.keyClick(self.panel.url_edit, Qt.Key.Key_Return)
        self.assertIsNone(self.window.worker)
        self.assertEqual(self.window.profile_store.history, [])
        self.assertFalse(self.window.start_quick_action("unknown", [str(invalid)]))

    def test_busy_disables_inputs_and_navigation_while_stop_remains_usable(self):
        source = self.image()
        entered_conversion = threading.Event()

        def wait_for_cancel(action_id, value, target, cancel):
            entered_conversion.set()
            deadline = time.monotonic() + 10
            while not cancel() and time.monotonic() < deadline:
                time.sleep(0.01)
            if cancel():
                raise CancelledError("cancelled by UI test")
            raise AssertionError("UI never delivered the stop request")

        with patch("qingjian.quick_actions._convert", side_effect=wait_for_cancel):
            self.assertTrue(self.window.start_quick_action("image_to_jpg", [str(source)]))
            self.wait_until(entered_conversion.is_set)
            self.assertTrue(self.window._busy)
            for control in [self.floating.ring, self.floating.choose_button, self.floating.paste_button,
                            self.floating.save_button, self.window.add_files_button,
                            *self.window.surface_buttons.values()]:
                self.assertFalse(control.isEnabled())
            self.assertTrue(self.floating.cancel_button.isVisible())
            self.assertTrue(self.floating.cancel_button.isEnabled())
            self.assertFalse(self.window.start_quick_action("image_to_png", [str(source)]))
            QTest.mouseClick(self.floating.cancel_button, Qt.MouseButton.LeftButton)
            self.wait_for_worker()
        self.assertTrue(self.window.quick_result["cancelled"])
        self.assertIn("已停止", self.floating.status_label.text())
        self.assertIn("已停止", self.window.conversion_session.status_text)
        self.assertFalse(self.floating.cancel_button.isVisible())
        self.assertTrue(self.floating.ring.isEnabled())
        self.assertTrue(self.floating.paste_button.isEnabled())
        self.assertEqual(list(source.parent.glob(".qingjian-quick-*")), [])

    def test_failed_conversion_has_details_and_preserves_independent_quick_result(self):
        source = self.root / "损坏图片.png"
        source.write_bytes(b"not an image")
        self.assertTrue(self.window.start_quick_action("image_to_jpg", [str(source)]))
        self.wait_for_worker()
        expected = copy.deepcopy(self.window.quick_result)
        expected_raw = copy.deepcopy(self.window._raw_conversion_result)
        self.assertEqual(expected["records"][0]["状态"], "失败")
        self.assertTrue(expected["records"][0]["原因"])
        self.assertTrue(self.floating.results_button.isEnabled())
        self.assertTrue(self.floating.results_button.isVisible())
        self.assertFalse(self.floating.folder_button.isEnabled())
        self.assertEqual(self.window.profile_store.history[0]["status"], "失败")
        self.window.last_result = {"folder": "another ordinary result", "records": []}
        with patch("qingjian.converter_dialogs.ConversionResultDialog") as dialog, \
                patch.object(self.window.window_snap, "register"), patch.object(self.window.appearance, "register"):
            QTest.mouseClick(self.floating.results_button, Qt.MouseButton.LeftButton)
        dialog.assert_called_once()
        self.assertEqual(dialog.call_args.args[0], expected_raw)
        self.assertEqual(dialog.call_args.args[0]['records'][0]['status'], 'failed')
        self.assertEqual(self.window.quick_result, expected)

    def test_unexpected_worker_error_reaches_error_details_and_history(self):
        source = self.image()
        with patch("qingjian.quick_actions.run_quick_action", side_effect=RuntimeError("虚构后台异常：测试详情保留")):
            self.assertTrue(self.window.start_quick_action("image_to_jpg", [str(source)]))
            self.wait_for_worker()
        self.assertTrue(self.floating.results_button.isEnabled())
        result = self.window.quick_result
        all_errors = result.get("fatal", "") + "\n" + "\n".join(row.get("原因", "") for row in result.get("records", []))
        self.assertIn("虚构后台异常", all_errors)
        self.assertEqual(self.window.profile_store.history[0]["status"], "失败")
        with patch("qingjian.converter_dialogs.ConversionResultDialog") as dialog, \
                patch.object(self.window.window_snap, "register"), patch.object(self.window.appearance, "register"):
            QTest.mouseClick(self.floating.results_button, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.call_args.args[0], self.window._raw_conversion_result)
        self.assertIn('虚构后台异常', '\n'.join(dialog.call_args.args[0]['errors']))

    def test_mixed_drop_keeps_success_and_reports_invalid_item(self):
        source = self.image()
        invalid = self.root / "应报告不支持.txt"
        invalid.write_text("不会删除", encoding="utf-8")
        entered, dropped = self.drop(self.panel.cards["image_to_jpg"], urls=[
            QUrl.fromLocalFile(str(source)), QUrl.fromLocalFile(str(invalid))])
        self.assertTrue(entered)
        self.assertTrue(dropped)
        self.wait_for_worker()
        records = self.window.quick_result["records"]
        self.assertEqual([row["状态"] for row in records], ["成功", "失败"])
        self.assertTrue(Path(records[0]["输出"]).is_file())
        self.assertIn("不支持", records[1]["原因"])
        self.assertEqual(self.window.profile_store.history[0]["status"], "部分完成")

    def test_quick_history_ignores_unrelated_generic_report_in_original_folder(self):
        source = self.image()
        unrelated_source = source.parent / "其他任务的原件.docx"
        unrelated_output = source.parent / "其他任务的输出.txt"
        unrelated_source.write_bytes(b"unrelated source fixture")
        unrelated_output.write_text("这属于另一次普通任务", encoding="utf-8")
        report = source.parent / "处理记录.json"
        report.write_text(json.dumps({"结果": [{"输入": str(unrelated_source), "输出": str(unrelated_output),
                                                 "状态": "成功", "原因": "无关任务的记录"}]},
                                    ensure_ascii=False), encoding="utf-8")
        original_report = report.read_bytes()
        self.assertTrue(self.window.start_quick_action("image_to_jpg", [str(source)]))
        self.wait_for_worker()
        target = Path(self.window.quick_result["records"][0]["输出"])
        self.assertTrue(target.is_file())
        self.assertEqual(target.parent, source.parent)
        # Reload the actual saved history so this also covers opening it after
        # restarting the application, without relying on the in-memory result.
        self.window.profile_store = ProfileStore(self.root / "profile.json")
        item = self.window.profile_store.history[0]
        self.assertEqual(item["scene"], "quick:image_to_jpg")
        with patch("qingjian.app.ResultDialog") as dialog:
            self.window.show_history_result(item["id"])
        dialog.assert_called_once()
        result = dialog.call_args.args[0]
        self.assertEqual([row["输出"] for row in result["records"]], [str(target)])
        self.assertEqual(result["output_count"], 1)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(unrelated_source), serialized)
        self.assertNotIn(str(unrelated_output), serialized)
        self.assertNotIn("无关任务的记录", serialized)
        self.assertEqual(report.read_bytes(), original_report)
        self.assertEqual(unrelated_output.read_text(encoding="utf-8"), "这属于另一次普通任务")

    def test_actual_http_paste_uses_remembered_link_folder(self):
        html = '<meta charset="utf-8"><article><h1>本地模拟网页</h1><p>真实请求与后台转换。</p></article>'.encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        destination = self.root / "用户选定链接保存位置"
        try:
            with patch("qingjian.app.QFileDialog.getExistingDirectory", return_value=str(destination)):
                QTest.mouseClick(self.floating.save_button, Qt.MouseButton.LeftButton)
            self.assertEqual(self.window.conversion_session.url_folder, destination)
            self.assertEqual(self.floating.url_folder, str(destination))
            settings = self.root / "quick-settings.json"
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["url_folder"], str(destination))
            url = f"http://127.0.0.1:{server.server_port}/article"
            with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "HTTP_PROXY": "", "HTTPS_PROXY": ""}):
                self.clipboard(urls=[QUrl(url)], text="浏览器提供的网页标题")
                QTest.mouseClick(self.floating.paste_button, Qt.MouseButton.LeftButton)
                self.assertIsNone(self.window.worker)
                self.assertTrue(self.floating.choose_target("convert:docx"))
                self.wait_for_worker()
            target = Path(self.window.quick_result["records"][0]["输出"])
            self.assertEqual(target.parent.resolve(), destination.resolve())
            self.assertIn("真实请求与后台转换", "".join(p.text for p in Document(target).paragraphs))
            self.assertEqual(self.window.quick_result["records"][0]["输入"], url)
            reloaded = ConversionSession(settings)
            self.assertEqual(reloaded.url_folder, destination)
            self.pump()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_converter_long_status_and_folder_do_not_widen_new_home(self):
        window = self.window
        for width, height in ((980, 680), (1280, 860)):
            with self.subTest(size=(width, height)):
                window.resize(width, height)
                window.show_surface("home")
                self.floating.set_url_folder(Path.home() / Path("非常长的链接保存目录/" * 18))
                self.floating.notice("处理失败的具体原因 " * 100)
                self.pump(8)
                page = window.home_page
                self.assertLessEqual(page.canvas.width(), page.scroll.viewport().width())
                self.assertEqual(page.scroll.horizontalScrollBar().maximum(), 0)
                for control in [page.personal_panel, page.group_section, *page.group_cards.values()]:
                    rectangle = QRect(control.mapTo(page.canvas, QPoint(0, 0)), control.size())
                    self.assertGreaterEqual(rectangle.left(), 0, control.objectName())
                    self.assertLessEqual(rectangle.right(), page.canvas.width(), control.objectName())
                self.assertFalse(hasattr(page, "quick_panel"))
                self.assertIn("本地文件仍保存到原文件夹", self.floating.save_button.toolTip())


class QuickPasteRoutingUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_http_https_mime_drop_and_ctrl_v_route_once_without_needing_enter(self):
        panel = QuickPanel()
        panel.resize(900, 430)
        panel.show()
        self.app.processEvents()
        submitted = []
        panel.submitted.connect(lambda action, sources: submitted.append((action, sources)))
        for url in ("http://example.test/article", "https://example.test/中文?x=1"):
            mime = QMimeData()
            mime.setUrls([QUrl(url)])
            mime.setText("网页标题不是输入")
            QApplication.clipboard().setMimeData(mime)
            panel.url_edit.setFocus()
            QTest.keyClick(panel.url_edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            self.app.processEvents()
            self.assertEqual(submitted[-1], ("web_to_word", [QUrl(url).toString()]))
            for surface in (panel.cards["web_to_word"], panel.url_edit):
                before = len(submitted)
                enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(surface, enter)
                drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                                  Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(surface, drop)
                self.app.processEvents()
                self.assertTrue(enter.isAccepted())
                self.assertTrue(drop.isAccepted())
                self.assertEqual(len(submitted), before + 1)
                self.assertEqual(submitted[-1], ("web_to_word", [QUrl(url).toString()]))
        QApplication.clipboard().clear()
        panel.close()
        panel.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
