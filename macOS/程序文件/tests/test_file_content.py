"""Real-format regression coverage for file organization and content tools."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from qingjian.core.common import CancelledError
from qingjian.core.content_ops import iter_text_units, run_content
from qingjian.core.file_ops import plan_organize, run_files, undo_organize


def _book_rows(path, sheet):
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        return list(workbook[sheet].iter_rows(values_only=True))
    finally:
        workbook.close()


def _pdf(path, text="ALPHA Code=PDF12", password=None, table=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    page[NameObject("/Resources")] = resources
    commands = f"BT /F1 12 Tf 30 270 Td ({text}) Tj ET\n"
    if table:
        for y in (150, 185, 220):
            commands += f"30 {y} m 270 {y} l S\n"
        for x in (30, 150, 270):
            commands += f"{x} 150 m {x} 220 l S\n"
        for x, y, value in ((40, 200, "Label"), (160, 200, "Amount"), (40, 165, "Policy"), (160, 165, "42")):
            commands += f"BT /F1 12 Tf {x} {y} Td ({value}) Tj ET\n"
    content = DecodedStreamObject()
    content.set_data(commands.encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    if password:
        writer.encrypt(password)
    writer.write(path)


class _TestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("qingjian-file-content-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.outputs = self.root / "outputs"

    def tearDown(self):
        shutil.rmtree(self.root)

    def text(self, name, text):
        path = self.inputs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class FileToolsTests(_TestCase):
    def test_copy_preview_is_read_only_and_archive_does_not_replace(self):
        source = self.text("alpha.txt", "original contents")
        options = {"action": "copy", "group_by": "extension"}
        plan = plan_organize([source], self.outputs, options)
        self.assertTrue(source.is_file())
        self.assertFalse(self.outputs.exists())
        self.assertEqual(Path(plan[0]["target"]).parent.name, "txt")
        options["_plan"] = plan
        result = run_files("file_organize", [source], self.outputs, options)
        self.assertEqual(result["changes"], 1)
        self.assertEqual(len(result["data_outputs"]), 1)
        self.assertEqual(Path(result["data_outputs"][0]).read_text(), source.read_text())
        self.assertTrue(any(str(path).endswith(".xlsx") for path in result["outputs"]))
        with self.assertRaises(FileExistsError):
            run_files("file_organize", [source], self.outputs, options)

    def test_original_rename_execute_and_undo_hash_manifest(self):
        first, second = self.text("a.txt", "one"), self.text("b.txt", "two")
        options = {"action": "rename", "filename_template": "文件_{序号:03d}"}
        options["_plan"] = plan_organize([first, second], self.outputs, options)
        result = run_files("file_organize", [first, second], self.outputs, options)
        self.assertEqual(result["changes"], 2)
        self.assertFalse(first.exists())
        self.assertTrue((self.inputs / "文件_001.txt").is_file())
        manifest = next(Path(value) for value in result["outputs"] if value.endswith(".json"))
        entries = json.loads(manifest.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(len(entries[0]["sha256"]), 64)
        restored = undo_organize(manifest, self.root / "undo")
        self.assertEqual(restored["changes"], 2)
        self.assertEqual(first.read_text(), "one")
        self.assertEqual(second.read_text(), "two")
        self.assertTrue(manifest.is_file())

    def test_two_phase_rename_allows_swap(self):
        first = self.text("002.txt", "first source")
        second = self.text("001.txt", "second source")
        options = {"action": "rename", "filename_template": "{序号:03d}"}
        options["_plan"] = plan_organize([first, second], self.outputs, options)
        result = run_files("file_organize", [first, second], self.outputs, options)
        self.assertEqual((self.inputs / "001.txt").read_text(), "first source")
        self.assertEqual((self.inputs / "002.txt").read_text(), "second source")
        undo_organize(next(Path(value) for value in result["outputs"] if value.endswith(".json")), self.root / "undo")
        self.assertEqual(first.read_text(), "first source")
        self.assertEqual(second.read_text(), "second source")

    def test_rename_rolls_back_on_error_or_cancel(self):
        from qingjian.core import file_ops
        sources = [self.text("a.txt", "A"), self.text("b.txt", "B")]
        options = {"action": "rename"}
        options["_plan"] = plan_organize(sources, self.outputs, options)
        original_move = file_ops._move
        failed = False

        def move(source, target):
            nonlocal failed
            if target == Path(options["_plan"][1]["target"]) and not failed:
                failed = True
                raise OSError("simulated target failure")
            return original_move(source, target)

        with patch("qingjian.core.file_ops._move", side_effect=move):
            with self.assertRaises(OSError):
                run_files("file_organize", sources, self.outputs, options)
        self.assertEqual([path.read_text() for path in sources], ["A", "B"])
        self.assertFalse(list(self.inputs.glob(".qingjian-*")))
        cancelled = False

        def progress(done, total, message):
            nonlocal cancelled
            cancelled = done == 1

        with self.assertRaises(CancelledError):
            run_files("file_organize", sources, self.outputs, options, progress, lambda: cancelled)
        self.assertEqual([path.read_text() for path in sources], ["A", "B"])
        self.assertFalse(list(self.inputs.glob(".qingjian-*")))
        self.assertFalse(list(self.outputs.glob("*.json")))

    def test_undo_refuses_changed_files_and_occupied_original_names(self):
        source = self.text("a.txt", "original")
        options = {"action": "rename"}
        options["_plan"] = plan_organize([source], self.outputs, options)
        result = run_files("file_organize", [source], self.outputs, options)
        renamed = Path(result["data_outputs"][0])
        manifest = next(Path(value) for value in result["outputs"] if value.endswith(".json"))
        renamed.write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "修改"):
            undo_organize(manifest, self.root / "undo")
        self.assertEqual(renamed.read_text(), "modified")
        renamed.write_text("original", encoding="utf-8")
        source.write_text("new user data", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            undo_organize(manifest, self.root / "undo")
        self.assertEqual(source.read_text(), "new user data")
        self.assertEqual(renamed.read_text(), "original")

    def test_changed_or_tampered_plan_is_rejected(self):
        source = self.text("a.txt", "original")
        options = {"action": "copy"}
        options["_plan"] = plan_organize([source], self.outputs, options)
        source.write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "改变"):
            run_files("file_organize", [source], self.outputs, options)
        options["_plan"] = plan_organize([source], self.outputs, options)
        options["_plan"][0]["target"] = str(self.root / "outside.txt")
        with self.assertRaises(ValueError):
            run_files("file_organize", [source], self.outputs, options)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_invalid_names_templates_collisions_and_grouping(self):
        source = self.text("a.txt", "original")
        for options in ({"filename_template": "../escape"}, {"filename_template": "CON"},
                        {"filename_template": "bad?name"}, {"filename_template": "{原名.__class__}"},
                        {"filename_template": "{序号:999999999d}"},
                        {"action": "rename", "group_by": "extension"}, {"group_by": "pattern", "group_pattern": "abc"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                plan_organize([source], self.outputs, options)
        second = self.text("sub/a.txt", "different")
        with self.assertRaises(ValueError):
            plan_organize([source, second], self.outputs, {"filename_template": "{原名}"})
        grouped = plan_organize([source], self.outputs, {"group_by": "pattern", "group_pattern": "(a)"})
        self.assertEqual(Path(grouped[0]["target"]).parent.name, "a")

    def test_manifest_write_failure_rolls_names_back(self):
        source = self.text("a.txt", "contents")
        options = {"action": "rename"}
        options["_plan"] = plan_organize([source], self.outputs, options)
        with patch("qingjian.core.file_ops.atomic_write", side_effect=OSError("manifest cannot be saved")):
            with self.assertRaises(OSError):
                run_files("file_organize", [source], self.outputs, options)
        self.assertEqual(source.read_text(), "contents")
        self.assertFalse(Path(options["_plan"][0]["target"]).exists())

    def test_requires_preview(self):
        source = self.text("a.txt", "contents")
        with self.assertRaisesRegex(ValueError, "预览"):
            run_files("file_organize", [source], self.outputs, {})

    def test_unchanged_rename_still_passes_business_files_to_next_tool(self):
        source = self.text("a.txt", "contents")
        options = {"action": "rename", "filename_template": "{原名}"}
        options["_plan"] = plan_organize([source], self.outputs, options)
        result = run_files("file_organize", [source], self.outputs, options)
        self.assertEqual(result["changes"], 0)
        self.assertEqual([Path(value).resolve() for value in result["data_outputs"]], [source.resolve()])
        self.assertEqual(source.read_text(), "contents")

    def test_exact_duplicates_use_both_size_and_sha_without_deleting(self):
        first, second = self.text("a.txt", "ABCD"), self.text("sub/b.txt", "ABCD")
        distinct = self.text("c.txt", "WXYZ")
        result = run_files("find_duplicates", [self.inputs], self.outputs, {})
        self.assertEqual(result["changes"], 1)
        rows = _book_rows(result["outputs"][0], "重复文件")
        self.assertEqual(len(rows), 3)
        self.assertEqual({Path(row[1]).name for row in rows[1:]}, {"a.txt", "b.txt"})
        self.assertTrue(all(path.exists() for path in (first, second, distinct)))

    def test_material_roots_are_never_combined_into_false_completeness(self):
        first, second = self.inputs / "甲", self.inputs / "乙"
        first.mkdir()
        second.mkdir()
        (first / "report.docx").write_bytes(b"sample")
        (second / "data.xlsx").write_bytes(b"sample")
        result = run_files("check_materials", [first, second], self.outputs,
                           {"required_files": ["report.docx", "data.xlsx"]})
        self.assertEqual(result["changes"], 2)
        missing = [row for row in _book_rows(result["outputs"][0], "文件缺漏")[1:] if row[2] == "缺漏"]
        self.assertEqual(len(missing), 2)
        self.assertEqual({Path(row[0]).name for row in missing}, {"甲", "乙"})

    def test_material_relative_matching_and_no_root_warning(self):
        source = self.text("nested/form.txt", "contents")
        result = run_files("check_materials", [self.inputs], self.outputs,
                           {"required_files": ["nested/*.txt", "**/form.txt"]})
        self.assertEqual(result["changes"], 0)
        result = run_files("check_materials", [source], self.outputs,
                           {"required_files": ["nested/*.txt"]})
        self.assertEqual(result["changes"], 0)
        self.assertTrue(result["warnings"])
        self.assertIn("未判断", _book_rows(result["outputs"][0], "文件缺漏")[1][2])

    def test_material_required_columns_all_visible_sheets_and_provenance(self):
        path = self.inputs / "data.xlsx"
        workbook = Workbook()
        workbook.active.title = "First"
        workbook.active.append(["Name", "Value"])
        workbook.active.append(["alpha", 0])
        second = workbook.create_sheet("Second")
        second.append(["Name", "Value"])
        second.append(["beta", 3])
        second.append(["gamma", None])
        workbook.save(path)
        workbook.close()
        result = run_files("check_materials", [path], self.outputs,
                           {"required_columns": ["Name", "Value", "Missing"]})
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["changes"], 3)
        rows = _book_rows(result["outputs"][0], "必填字段异常")[1:]
        self.assertTrue(any(row[2:5] == ("Second", 3, "Value") for row in rows))
        self.assertFalse(any(row[2:5] == ("First", 2, "Value") for row in rows))

    def test_material_csv_blank_field_preserves_original_row(self):
        source = self.text("data.csv", "Name,Value\nalpha,1\nbeta,\n")
        result = run_files("check_materials", [source], self.outputs, {"required_columns": ["Value"]})
        rows = _book_rows(result["outputs"][0], "必填字段异常")
        self.assertEqual(result["changes"], 1)
        self.assertEqual(rows[1][3], 3)

    def test_material_path_glob_star_does_not_cross_directories(self):
        self.text("nested/deeper/form.txt", "contents")
        result = run_files("check_materials", [self.inputs], self.outputs,
                           {"required_files": ["nested/*.txt", "nested/**/*.txt"]})
        rows = _book_rows(result["outputs"][0], "文件缺漏")[1:]
        self.assertEqual(rows[0][2], "缺漏")
        self.assertEqual(rows[1][2], "齐全")
        self.assertEqual(result["changes"], 1)

    @unittest.skipUnless(os.name != "nt" or hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_input_is_rejected(self):
        source = self.text("real.txt", "data")
        linked = self.inputs / "link.txt"
        try:
            linked.symlink_to(source)
        except OSError:
            self.skipTest("current Windows account cannot create symbolic links")
        with self.assertRaises(ValueError):
            plan_organize([linked], self.outputs, {})


class ContentToolsTests(_TestCase):
    def docx(self):
        path = self.inputs / "sample.docx"
        document = Document()
        document.add_paragraph("Alpha Beta Code=DOC77")
        document.add_paragraph("Code=DOC88")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Name"
        table.cell(0, 1).text = "Value"
        table.cell(1, 0).text = "TableToken"
        table.cell(1, 1).text = "42"
        document.sections[0].header.paragraphs[0].text = "HeaderToken=H88"
        document.save(path)
        return path

    def test_docx_body_tables_headers_and_input_preserved(self):
        source = self.docx()
        before = source.read_bytes()
        units = list(iter_text_units(source, {}))
        self.assertTrue(any("正文" in position and "Code=DOC77" in text for position, text in units))
        self.assertTrue(any("表格" in position and "TableToken" in text for position, text in units))
        self.assertTrue(any("页眉" in position and "HeaderToken" in text for position, text in units))
        self.assertEqual(before, source.read_bytes())

    def test_search_real_docx_pdf_xlsx_text_csv_html_with_positions(self):
        docx = self.docx()
        pdf = self.inputs / "sample.pdf"
        _pdf(pdf)
        self.text("sample.txt", "text alpha source\nother line")
        self.text("sample.csv", "Name,Value\nalpha,10\n")
        self.text("sample.html", "<html><body><p>Alpha <strong>Beta</strong></p><script>Alpha fake</script></body></html>")
        xlsx = self.inputs / "sample.xlsx"
        workbook = Workbook()
        workbook.active.append(["Name", "Value"])
        workbook.active.append(["alpha", 0])
        workbook.save(xlsx)
        workbook.close()
        result = run_content("content_search", [self.inputs], self.outputs, {"query": "ALPHA"})
        self.assertEqual(result["errors"], [])
        rows = _book_rows(result["outputs"][0], "命中结果")[1:]
        self.assertEqual(result["changes"], 6)
        self.assertEqual({Path(row[0]).suffix for row in rows}, {".docx", ".pdf", ".txt", ".csv", ".html", ".xlsx"})
        self.assertTrue(any("第1页" in row[1] for row in rows))
        self.assertTrue(any("工作表" in row[1] for row in rows))
        self.assertFalse(any("fake" in row[3] for row in rows))

    def test_search_all_is_within_same_source_unit_and_case_sensitive(self):
        source = self.text("sample.txt", "Alpha Beta\nalpha only\nBETA only\n")
        result = run_content("content_search", [source], self.outputs,
                             {"query": "alpha\nbeta", "match_mode": "all"})
        self.assertEqual(result["changes"], 1)
        self.assertEqual(_book_rows(result["outputs"][0], "命中结果")[1][3], "Alpha Beta")
        result = run_content("content_search", [source], self.outputs,
                             {"query": "ALPHA", "case_sensitive": True})
        self.assertEqual(result["changes"], 0)

    def test_search_xlsm_accepted_by_catalog_is_not_silently_skipped(self):
        path = self.inputs / "values.xlsm"
        workbook = Workbook()
        workbook.active.append(["alpha", 0])
        workbook.save(path)
        workbook.close()
        result = run_content("content_search", [path], self.outputs, {"query": "alpha"})
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["changes"], 1)
        self.assertEqual(Path(_book_rows(result["outputs"][0], "命中结果")[1][0]), path.resolve())

    def test_search_limit_is_explicit(self):
        source = self.text("many.txt", "\n".join("Alpha " + str(index) for index in range(5005)))
        result = run_content("content_search", [source], self.outputs, {"query": "Alpha"})
        self.assertEqual(result["changes"], 5000)
        self.assertTrue(any("5000" in warning for warning in result["warnings"]))

    def test_scanned_pdf_warns_and_encrypted_pdf_errors_are_chinese(self):
        scanned = self.inputs / "scan.pdf"
        Image.new("RGB", (60, 50), "white").save(scanned, "PDF")
        result = run_content("content_search", [scanned], self.outputs, {"query": "Alpha"})
        self.assertEqual(result["changes"], 0)
        self.assertTrue(any("扫描" in warning and "OCR" in warning for warning in result["warnings"]))
        encrypted = self.inputs / "locked.pdf"
        _pdf(encrypted, password="secret")
        result = run_content("content_search", [encrypted], self.outputs, {"query": "Alpha"})
        self.assertTrue(result["errors"])
        self.assertTrue(any("密码" in error for error in result["errors"]))
        result = run_content("content_search", [encrypted], self.outputs, {"query": "Alpha", "password": "secret"})
        self.assertEqual(result["changes"], 1)
        self.assertEqual(result["errors"], [])

    def test_extract_first_match_missing_value_original_evidence_and_tables(self):
        source = self.docx()
        result = run_content("extract_fields", [source], self.outputs,
                             {"fields": [{"name": "Code", "pattern": r"Code=([A-Z0-9]+)"},
                                         {"name": "Missing", "pattern": r"Missing=(\d+)"}]})
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["changes"], 1)
        rows = _book_rows(result["outputs"][0], "字段提取")[1:]
        hit = next(row for row in rows if row[1] == "Code")
        self.assertEqual(hit[2], "DOC77")
        self.assertIn("Code=DOC77", hit[4])
        missing = next(row for row in rows if row[1] == "Missing")
        self.assertIsNone(missing[2])
        self.assertTrue(result["warnings"])
        table = _book_rows(result["outputs"][0], "识别表格")
        self.assertTrue(any(row[-1] == "TableToken" for row in table[1:]))

    def test_extract_no_group_all_matches_and_formula_is_literal(self):
        source = self.text("values.txt", "Code=ONE\nCode=TWO\nValue==1+1")
        result = run_content("extract_fields", [source], self.outputs,
                             {"fields": [{"name": "Full", "pattern": "Code=[A-Z]+"},
                                         {"name": "Formula", "pattern": r"Value=(.+)"}],
                              "per_file_first": False, "include_tables": False})
        self.assertEqual(result["changes"], 3)
        workbook = load_workbook(result["outputs"][0], data_only=False)
        try:
            values = list(workbook["字段提取"].iter_rows())
            extracted = [row[2].value for row in values[1:]]
            self.assertEqual(extracted, ["Code=ONE", "Code=TWO", "=1+1"])
            self.assertEqual(values[-1][2].data_type, "s")
        finally:
            workbook.close()

    def test_extract_docx_and_pdf_opened_once_for_text_and_tables(self):
        import pdfplumber
        source = self.docx()
        pdf = self.inputs / "table.pdf"
        _pdf(pdf, table=True)
        original_docx = Document
        original_pdf = pdfplumber.open
        with patch("docx.Document", wraps=original_docx) as docx_open, patch("pdfplumber.open", wraps=original_pdf) as pdf_open:
            result = run_content("extract_fields", [source, pdf], self.outputs,
                                 {"fields": [{"name": "Code", "pattern": r"Code=([A-Z0-9]+)"}], "include_tables": True})
        self.assertEqual(result["errors"], [])
        self.assertEqual(docx_open.call_count, 1)
        self.assertEqual(pdf_open.call_count, 1)
        table = _book_rows(result["outputs"][0], "识别表格")
        self.assertTrue(any(Path(row[0]).suffix == ".pdf" and row[-1] == "42" for row in table[1:]))

    def test_extract_tables_only_matches_ui_contract(self):
        source = self.docx()
        result = run_content("extract_fields", [source], self.outputs, {"fields": [], "include_tables": True})
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["changes"], 4)
        rows = _book_rows(result["outputs"][0], "识别表格")
        self.assertEqual(len(rows), 5)
        self.assertEqual({row[-1] for row in rows[1:]}, {"Name", "Value", "TableToken", "42"})
        with self.assertRaises(ValueError):
            run_content("extract_fields", [source], self.outputs, {"fields": [], "include_tables": False})

    def test_long_context_is_preserved_in_multiple_excel_rows(self):
        original = "Alpha " + "x" * 40000
        source = self.text("long.txt", original)
        result = run_content("content_search", [source], self.outputs, {"query": "Alpha"})
        rows = _book_rows(result["outputs"][0], "命中结果")[1:]
        self.assertEqual(result["changes"], 1)
        self.assertEqual("".join(row[3] for row in rows), original)

    def test_invalid_rules_and_cancel(self):
        source = self.text("sample.txt", "Alpha Code=ONE")
        with self.assertRaises(ValueError):
            run_content("content_search", [source], self.outputs, {"query": ""})
        for fields in ([{"name": "bad", "pattern": "("}], [{"name": "empty", "pattern": ".*"}],
                       [{"name": "a", "pattern": ".+"}, {"name": "a", "pattern": ".+"}]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                run_content("extract_fields", [source], self.outputs, {"fields": fields})
        with self.assertRaises(CancelledError):
            run_content("content_search", [source], self.outputs, {"query": "Alpha"}, cancel=lambda: True)


if __name__ == "__main__":
    unittest.main()
