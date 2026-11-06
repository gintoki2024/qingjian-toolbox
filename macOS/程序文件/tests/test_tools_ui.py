"""End-to-end checks for v0.2 aggregate tools and UI controls."""
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from docx import Document
from openpyxl import Workbook, load_workbook
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from qingjian.app import MODES, Window
from qingjian.catalog import EXTRA_ORDER
from qingjian.forms import OptionPanel, WorkflowPanel
from qingjian.tools import execute_tool, scrub_secrets, validate_tool_options
from qingjian.core.file_ops import plan_organize
from qingjian.core.common import CancelledError


def data_file(folder, name="名单.xlsx"):
    path = folder / name
    book = Workbook()
    book.active.append(["编号", "姓名", "部门"])
    book.active.append(["001", " 张三 ", "业务部"])
    book.active.append(["002", "李四", "技术部"])
    book.save(path)
    book.close()
    return path


class ToolsIntegrationTests(unittest.TestCase):
    def test_clean_template_workflow_only_passes_business_outputs(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR")) as temp:
            root = Path(temp)
            data = data_file(root)
            template = root / "模板.docx"
            doc = Document()
            doc.add_paragraph("通知：{{姓名}}，编号{{编号}}，{{部门}}")
            doc.save(template)
            attachment = root / "说明.txt"
            attachment.write_text("保留附件", encoding="utf-8")
            result = execute_tool("workflow", [data, attachment], root / "out", {
                "steps": [
                    {"tool": "excel_clean", "options": {"trim_text": True, "text_columns": ["编号"]}},
                    {"tool": "template_fill", "options": {"template_path": str(template), "filename_template": "{姓名}"}},
                    {"tool": "file_organize", "options": {"action": "copy", "filename_template": "{序号:03d}_{原名}"}},
                ], "archive": True})
            self.assertEqual(result["errors"], [])
            primary = [Path(p) for p in result["data_outputs"]]
            self.assertEqual(len(primary), 3)
            docs = [p for p in primary if p.suffix == ".docx"]
            self.assertEqual(len(docs), 2)
            self.assertTrue(any("张三，编号001" in Document(p).paragraphs[0].text for p in docs))
            archive = next(Path(p) for p in result["outputs"] if p.endswith(".zip"))
            with zipfile.ZipFile(archive) as stream:
                self.assertEqual(len(stream.namelist()), 3)
                self.assertTrue(all("报告" not in p and "审计" not in p for p in stream.namelist()))
            self.assertEqual(attachment.read_text(encoding="utf-8"), "保留附件")

    def test_workflow_stops_on_row_error_without_archiving_incomplete_result(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR")) as temp:
            root = Path(temp)
            data = data_file(root)
            template = root / "template.docx"
            doc = Document()
            doc.add_paragraph("{{不存在的字段}}")
            doc.save(template)
            result = execute_tool("workflow", [data], root / "out", {
                "steps": [
                    {"tool": "template_fill", "options": {"template_path": str(template), "filename_template": "{序号:03d}"}},
                    {"tool": "file_organize", "options": {"filename_template": "{原名}"}},
                ], "archive": True, "stop_on_error": True})
            self.assertTrue(result["errors"])
            self.assertFalse(any(p.endswith(".zip") for p in result["outputs"]))
            self.assertFalse((root / "out" / "02_文件命名与归档").exists())

    def test_configuration_scrubs_nested_passwords_and_preview_plans(self):
        original = {"password": "secret", "steps": [{"options": {"password": "abc", "_plan": [{"source": "x"}], "pages": "2,1"}}]}
        clean = scrub_secrets(original)
        self.assertEqual(clean["password"], "")
        self.assertEqual(clean["steps"][0]["options"], {"password": "", "pages": "2,1"})
        self.assertEqual(original["password"], "secret")
        with self.assertRaises(ValueError):
            validate_tool_options("workflow", {"steps": [{"tool": "file_organize", "options": {"action": "rename", "filename_template": "{原名}"}}]})

    def test_partial_step_returns_new_business_output_and_no_final_zip(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR")) as temp:
            root = Path(temp)
            good = data_file(root)
            bad = root / "broken.xlsx"
            bad.write_text("invalid workbook", encoding="utf-8")
            result = execute_tool("workflow", [good, bad], root / "out", {
                "steps": [{"tool": "excel_clean", "options": {}}], "archive": True})
            self.assertTrue(result["errors"])
            self.assertEqual(len(result["data_outputs"]), 1)
            self.assertNotEqual(Path(result["data_outputs"][0]).resolve(), good.resolve())
            self.assertTrue(Path(result["data_outputs"][0]).is_relative_to((root / "out").resolve()))
            self.assertFalse(any(p.endswith(".zip") for p in result["outputs"]))

    def test_cancelled_step_writes_flow_record_and_retains_published_files(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR")) as temp:
            root = Path(temp)
            source = data_file(root)
            def interrupted(tool, sources, folder, options, progress, cancel):
                (folder / "已完成.xlsx").write_bytes(b"published file")
                raise CancelledError("stop")
            with patch("qingjian.core.excel_ops.run_excel", side_effect=interrupted):
                result = execute_tool("workflow", [source], root / "out", {
                    "steps": [{"tool": "excel_clean", "options": {}}], "archive": True})
            self.assertTrue(result["cancelled"])
            self.assertTrue(any("已完成.xlsx" in p for p in result["outputs"]))
            record = json.loads((root / "out" / "流程记录.json").read_text(encoding="utf-8"))
            self.assertEqual(record["状态"], "已取消")
            self.assertFalse(any(p.endswith(".zip") for p in result["outputs"]))


class AdvancedUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for_worker(self, window):
        deadline = time.monotonic() + 30
        while window.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(10)
        self.app.processEvents()
        self.assertIsNone(window.worker)

    def test_all_pages_have_settings_and_persistent_action_bar(self):
        window = Window()
        window.resize(1050, 760)
        window.show()
        for mode in MODES:
            window.switch_mode(MODES.index(mode))
            self.app.processEvents()
            self.assertEqual(window.settings.currentIndex(), MODES.index(mode))
            self.assertTrue(window.start_button.isVisible())
            self.assertIsInstance(window.get_options(), dict)
            if mode in {"content_search", "find_duplicates"}:
                self.assertFalse(window.output_edit.isVisible())
            else:
                bottom = window.workspace_scroll.mapTo(window, QPoint(0, window.workspace_scroll.height())).y()
                self.assertGreater(window.output_edit.mapTo(window, QPoint(0, 0)).y(), bottom)
        self.assertEqual(len(MODES), 27)
        window.close()

    def test_option_round_trip_and_invalid_import_preserves_values(self):
        for mode in EXTRA_ORDER:
            if mode == "workflow":
                continue
            panel = OptionPanel(mode)
            values = panel.options()
            panel.set_options(values)
            self.assertEqual(values, panel.options())
            panel.close()
        panel = OptionPanel("excel_clean")
        before = panel.options()
        with self.assertRaises(ValueError):
            panel.set_options({"required_columns": "姓名", "header_row": 2})
        self.assertEqual(panel.options(), before)

    def test_flow_editor_reorders_and_restores_steps(self):
        panel = WorkflowPanel()
        panel.add_step("image_pack", {"action": "pdf"})
        panel.tabs.tabBar().moveTab(1, 0)
        values = panel.options()
        self.assertEqual(values["steps"][0]["tool"], "image_pack")
        second = WorkflowPanel()
        second.set_options(values)
        self.assertEqual(values, second.options())
        with self.assertRaises(ValueError):
            second.set_options({"steps": [{"tool": "untrusted_code"}]})
        self.assertEqual(values, second.options())
        for invalid in ({"steps": []}, {**values, "archive": "false"}, {**values, "stop_on_error": 0}):
            with self.assertRaises(ValueError):
                second.set_options(invalid)
            self.assertEqual(values, second.options())

    def test_invalid_word_rule_import_preserves_current_settings(self):
        window = Window()
        before = window.get_options()
        with self.assertRaises(ValueError):
            window.apply_word_options({"format_enabled": False, "mask_phone": True, "font_size": "not-a-number"})
        self.assertEqual(before, window.get_options())
        window.close()

    def test_folder_queue_and_selected_file_order(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR")) as temp:
            root = Path(temp)
            a = data_file(root, "a.xlsx")
            b = data_file(root, "b.xlsx")
            window = Window()
            window.switch_mode(MODES.index("excel_merge"))
            window.accept_sources([str(root), str(root)])
            self.assertEqual(window.files["excel_merge"], [str(root.resolve())])
            window.switch_mode(MODES.index("excel_compare"))
            window.accept_sources([str(a), str(b)])
            window.table.selectRow(1)
            window.move_selected(-1)
            self.assertEqual(Path(window.files["excel_compare"][0]).resolve(), b.resolve())
            window.close()

    def test_advanced_worker_produces_real_workbook(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR")) as temp:
            root = Path(temp)
            source = data_file(root)
            window = Window()
            window.switch_mode(MODES.index("excel_clean"))
            window.accept_sources([str(source)])
            window.output_edit.setText(str(root / "results"))
            window.start_task()
            self.wait_for_worker(window)
            self.assertEqual(window.last_result["fatal"], "")
            self.assertEqual(window.last_result["error_count"], 0)
            books = [Path(r["输出"]) for r in window.last_result["records"] if r["输出"].endswith(".xlsx")]
            book = load_workbook(books[0])
            self.assertEqual(book.worksheets[0]["B2"].value, "张三")
            book.close()
            window.close()

    def test_preview_cancel_leaves_sources_and_accept_executes(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR")) as temp:
            root = Path(temp)
            source = root / "original.txt"
            source.write_text("source contents", encoding="utf-8")
            window = Window()
            window.switch_mode(MODES.index("file_organize"))
            window.accept_sources([str(source)])
            window.output_edit.setText(str(root / "out"))
            options = window.get_options()
            destination = root / "preview"
            destination.mkdir()
            plan = plan_organize([source], destination, options)
            preview = {"fatal": "", "cancelled": False, "plan": plan, "sources": [str(source)], "options": options, "folder": str(destination)}
            window.pending_preview = preview
            with patch.object(QDialog, "exec", return_value=QDialog.DialogCode.Rejected):
                window.review_preview()
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())
            destination.mkdir()
            window.pending_preview = preview
            with patch.object(QDialog, "exec", return_value=QDialog.DialogCode.Accepted):
                window.review_preview()
            self.wait_for_worker(window)
            self.assertEqual(window.last_result["fatal"], "")
            self.assertTrue(Path(plan[0]["target"]).exists())
            self.assertTrue(source.exists())
            window.close()


if __name__ == "__main__":
    unittest.main()
