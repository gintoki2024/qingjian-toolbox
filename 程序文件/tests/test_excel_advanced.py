"""Real XLSX checks for rule audit, conservative lookup and print copies."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.workbook.defined_name import DefinedName

from qingjian.core import excel_advanced as advanced
from qingjian.core.common import CancelledError

N = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def book(path, rows, title="数据", formulas=()):
    workbook = Workbook()
    try:
        workbook.active.title = title
        for row in rows:
            workbook.active.append(row)
        for coordinate, formula in formulas:
            workbook.active[coordinate] = formula
        workbook.save(path)
    finally:
        workbook.close()
    return path


def replace_entries(path, replacements):
    with ZipFile(path) as archive:
        entries = {item.filename: archive.read(item) for item in archive.infolist()}
    entries.update(replacements)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def cache(path, coordinate, value, error=False):
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    for cell in root.iter(N + "c"):
        if cell.get("r") == coordinate:
            cell.set("t", "e" if error else "n")
            saved = cell.find(N + "v")
            if saved is None:
                saved = ET.SubElement(cell, N + "v")
            saved.text = str(value)
    replace_entries(path, {"xl/worksheets/sheet1.xml": ET.tostring(root)})


def rows(path, sheet):
    workbook = load_workbook(path, data_only=False)
    try:
        return list(workbook[sheet].values)
    finally:
        workbook.close()


class AdvancedExcelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.folder = self.root / "结果"

    def tearDown(self):
        self.temporary.cleanup()

    def audit(self, path, rules=None, **options):
        settings = dict(header_row=1, numeric_tolerance=0, rules=rules or [{"name": "合计", "left": "{合计}", "right": "{收入}+{补贴}"}])
        settings.update(options)
        return advanced.run("excel_audit", [path], self.folder, settings)

    def lookup(self, first, second, **options):
        settings = dict(key_mappings=[{"target": "编号", "source": "工号"}], fill_mappings=[{"target": "部门", "source": "参考部门"}], header_row=1, overwrite=False, trim_keys=True)
        settings.update(options)
        return advanced.run("excel_lookup", [first, second], self.folder, settings)

    def test_audit_decimal_exactness_tolerance_and_blank(self):
        source = book(self.root / "金额.xlsx", [["收入", "补贴", "合计"], [0.1, 0.2, 0.3], [100, 20, 120.009], [None, 0, 0], ["错误", 2, 2]])
        result = self.audit(source, numeric_tolerance=0.01)
        self.assertEqual(result["errors"], [])
        detail = rows(result["outputs"][0], "核验明细")
        self.assertEqual([row[4] for row in detail[1:]], ["通过", "通过", "数值异常", "数值异常"])
        self.assertIn("未按0", detail[3][8])

    def test_audit_cell_range_round_and_division_zero(self):
        source = book(self.root / "数字.xlsx", [["金额", "总计"], ["0.1", 0.3], ["0.2", 0]])
        rules = [{"name": "总计", "left": "[B2]", "right": "SUM([A2:A3])"}, {"name": "四舍五入", "left": "ROUND(-1.25,1)", "right": "-1.3"}, {"name": "除0", "left": "1/[B3]", "right": "0"}]
        result = self.audit(source, rules)
        detail = rows(result["outputs"][0], "核验明细")
        self.assertEqual([row[4] for row in detail[1:]], ["通过", "通过", "数值异常"])
        self.assertIn("除数为0", detail[-1][8])

    def test_audit_sum_blank_is_anomaly(self):
        source = book(self.root / "空格.xlsx", [["金额"], [1], [None], [2]])
        result = self.audit(source, [{"name": "区域", "left": "SUM([A2:A4])", "right": "3"}])
        self.assertIn("A3为空", rows(result["outputs"][0], "核验明细")[1][8])

    def test_formula_missing_error_and_external_cache_are_reported(self):
        source = book(self.root / "公式.xlsx", [["收入", "补贴", "合计"], [100, 20, None], [100, 20, None], [100, 20, None]], formulas=[("C2", "=A2+B2"), ("C3", "=1/0"), ("C4", "='[其他.xlsx]数据'!C2")])
        cache(source, "C3", "#DIV/0!", error=True)
        cache(source, "C4", 120)
        result = self.audit(source)
        detail = rows(result["outputs"][0], "核验明细")
        self.assertEqual([row[4] for row in detail[1:]], ["数值异常", "数值异常", "通过"])
        issues = rows(result["outputs"][0], "公式与外链检查")
        self.assertEqual(len(issues), 4)
        self.assertIn("外部", issues[-1][3])

    def test_missing_and_duplicate_headers_never_choose_arbitrarily(self):
        source = book(self.root / "重名.xlsx", [["收入", "收入", "合计"], [1, 2, 3]])
        result = self.audit(source, [{"name": "规则", "left": "{合计}", "right": "{收入}"}])
        self.assertEqual(rows(result["outputs"][0], "核验明细")[1][4], "规则异常")

    def test_audit_failure_file_does_not_hide_valid_file(self):
        good = book(self.root / "可读.xlsx", [["收入", "补贴", "合计"], [1, 2, 4]])
        broken = self.root / "损坏.xlsx"
        broken.write_text("not xlsx")
        result = advanced.run("excel_audit", [good, broken], self.folder, {"rules": [{"left": "{合计}", "right": "{收入}+{补贴}"}]})
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(rows(result["outputs"][0], "核验明细")), 2)

    def test_no_arbitrary_python_or_unbounded_arithmetic(self):
        for expression in ["__import__('os').system('x')", "(1).__class__", "[x for x in y]", "2**999999", "lambda:1", "SUM(x=1)", "open(1)", "True", "1e999999", "{x}[0]"]:
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                advanced.validate("excel_audit", {"rules": [{"left": expression, "right": "1"}]})
        with self.assertRaises(ValueError):
            advanced.validate("excel_audit", {"rules": [{"left": "{收入}", "right": "[A1]"}]})

    def test_lookup_preserves_conflict_reports_duplicates_blank_unmatched(self):
        target = book(self.root / "目标.xlsx", [["编号", "部门"], ["01", ""], ["02", "旧部门"], ["03", ""], ["04", ""], [None, ""], ["05", ""], ["05", ""]])
        reference = book(self.root / "参考.xlsx", [["工号", "参考部门"], ["01", "综合部"], ["02", "业务部"], ["04", "甲"], ["04", "乙"], ["05", "财务部"], [None, "无主键"]])
        before = hashlib.sha256(target.read_bytes()).digest()
        result = self.lookup(target, reference)
        output = result["outputs"][0]
        self.assertEqual(result["changes"], 1)
        self.assertEqual(rows(output, "回填结果")[1:4], [("01", "综合部"), ("02", "旧部门"), ("03", None)])
        self.assertEqual(len(rows(output, "原值冲突")), 2)
        self.assertEqual(len(rows(output, "未匹配")), 2)
        self.assertEqual(len(rows(output, "主键异常")), 7)
        self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), before)

    def test_lookup_explicit_overwrite_and_new_columns_literals(self):
        target = book(self.root / "目标.xlsx", [["编号", "部门"], [" 01 ", "旧部门"]])
        reference = book(self.root / "参考.xlsx", [["工号", "参考部门", "电话"], ["01", "新部门", "placeholder"]])
        workbook = load_workbook(reference)
        workbook.active["C2"] = "=HYPERLINK(\"https://example.invalid\")"
        workbook.active["C2"].data_type = "s"
        workbook.save(reference)
        workbook.close()
        result = self.lookup(target, reference, overwrite=True, fill_mappings=[{"target": "部门", "source": "参考部门"}, {"target": "电话", "source": "电话"}])
        workbook = load_workbook(result["outputs"][0])
        try:
            self.assertEqual(workbook["回填结果"]["B2"].value, "新部门")
            self.assertEqual(workbook["回填结果"]["C2"].data_type, "s")
            self.assertEqual(workbook["原值冲突"]["F2"].value, "旧部门")
        finally:
            workbook.close()

    def test_lookup_combined_keys_and_numeric_text_not_coerced(self):
        target = book(self.root / "目标.xlsx", [["编号", "城市"], ["1", "北京"], [1, "北京"], [1, "上海"]])
        reference = book(self.root / "参考.xlsx", [["工号", "地区", "参考部门"], [1, "北京", "综合部"], [1, "上海", "业务部"]])
        result = self.lookup(target, reference, key_mappings=[{"target": "编号", "source": "工号"}, {"target": "城市", "source": "地区"}])
        result_rows = rows(result["outputs"][0], "回填结果")
        self.assertEqual([row[2] for row in result_rows[1:]], [None, "综合部", "业务部"])

    def test_lookup_validation_prevents_key_overwrite_missing_columns_same_file(self):
        a = book(self.root / "a.xlsx", [["编号"], [1]])
        b = book(self.root / "b.xlsx", [["工号"], [1]])
        with self.assertRaises(ValueError):
            self.lookup(a, b, fill_mappings=[{"target": "编号", "source": "工号"}])
        with self.assertRaisesRegex(ValueError, "列名缺失"):
            self.lookup(a, b)
        with self.assertRaises(ValueError):
            self.lookup(a, a)

    def test_print_copy_preserves_formulas_caches_and_hidden_sheet(self):
        source = book(self.root / "排版.xlsx", [["收入", "补贴", "合计"], [100, 20, None]], formulas=[("C2", "=SUM(A2:B2)")])
        workbook = load_workbook(source)
        workbook.create_sheet("隐藏").sheet_state = "hidden"
        workbook["隐藏"]["A1"] = "hidden value"
        workbook.save(source)
        workbook.close()
        cache(source, "C2", 120)
        before = source.read_bytes()
        with ZipFile(source) as archive:
            hidden_before = archive.read("xl/worksheets/sheet2.xml")
        result = advanced.run("excel_print", [source], self.folder, {"paper": "A3", "orientation": "landscape", "repeat_rows": "1:2", "print_area": "A1:C2", "margin_cm": 1.27})
        self.assertEqual(result["errors"], [])
        output = Path(result["outputs"][0])
        self.assertEqual(source.read_bytes(), before)
        with ZipFile(output) as archive:
            self.assertEqual(archive.read("xl/worksheets/sheet2.xml"), hidden_before)
        workbook = load_workbook(output)
        try:
            sheet = workbook["数据"]
            self.assertEqual(sheet.page_setup.paperSize, 8)
            self.assertEqual(sheet.page_setup.orientation, "landscape")
            self.assertTrue(sheet.sheet_properties.pageSetUpPr.fitToPage)
            self.assertEqual(sheet.page_setup.fitToHeight, 0)
            self.assertEqual(sheet.print_title_rows, "$1:$2")
            self.assertEqual(sheet.page_margins.left, 0.5)
            self.assertEqual(sheet["C2"].value, "=SUM(A2:B2)")
            self.assertEqual(workbook["隐藏"].page_setup.orientation, None)
        finally:
            workbook.close()
        workbook = load_workbook(output, data_only=True)
        self.assertEqual(workbook["数据"]["C2"].value, 120)
        workbook.close()

    def test_print_namespace_extension_prefix_stays_declared(self):
        source = book(self.root / "扩展.xlsx", [["名称"], ["甲"]])
        with ZipFile(source) as archive:
            data = archive.read("xl/worksheets/sheet1.xml")
        data = data.replace(b"<worksheet ", b'<worksheet xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" mc:Ignorable="x14ac" ', 1)
        replace_entries(source, {"xl/worksheets/sheet1.xml": data})
        result = advanced.run("excel_print", [source], self.folder, {})
        self.assertEqual(result["errors"], [])
        with ZipFile(result["outputs"][0]) as archive:
            output = archive.read("xl/worksheets/sheet1.xml")
        self.assertIn(b'xmlns:x14ac=', output)
        ET.fromstring(output)

    def test_print_clear_repeated_rows_preserves_existing_repeated_columns(self):
        source = book(self.root / "重复设置.xlsx", [["金额"], [2]], title="销售,一部")
        workbook = load_workbook(source)
        workbook.active.print_title_rows = "1:1"
        workbook.active.print_title_cols = "A:B"
        workbook.active.print_area = "A1:C20"
        workbook.active.page_setup.scale = 80
        workbook.save(source)
        workbook.close()
        result = advanced.run("excel_print", [source], self.folder, {"repeat_rows": "", "fit_width": False})
        self.assertEqual(result["errors"], [])
        workbook = load_workbook(result["outputs"][0])
        try:
            self.assertIsNone(workbook.active.print_title_rows)
            self.assertEqual(workbook.active.print_title_cols, "$A:$B")
            self.assertIn("$A$1:$C$20", str(workbook.active.print_area))
            self.assertEqual(workbook.active.page_setup.scale, 80)
            self.assertFalse(workbook.active.sheet_properties.pageSetUpPr.fitToPage)
        finally:
            workbook.close()

    def test_mislabeled_macro_workbook_refused_without_output(self):
        source = book(self.root / "宏.xlsx", [["金额"], [2]])
        replace_entries(source, {"xl/vbaProject.bin": b"not executable"})
        result = advanced.run("excel_print", [source], self.folder, {})
        self.assertEqual(result["outputs"], [])
        self.assertIn("含宏", result["errors"][0])

    def test_static_pdf_input_removes_formula_external_link_sources(self):
        source = book(self.root / "源.xlsx", [["合计"], [None]], formulas=[("A2", "='[外部.xlsx]数据'!A1")])
        cache(source, "A2", 120)
        static = self.root / "静态.xlsx"
        advanced._static_pdf_input(source, static, None)
        workbook = load_workbook(static, data_only=False)
        try:
            self.assertEqual(workbook.active["A2"].value, 120)
            self.assertNotEqual(workbook.active["A2"].data_type, "f")
        finally:
            workbook.close()

    def test_static_pdf_missing_cache_and_active_connection_refused(self):
        source = book(self.root / "源.xlsx", [["合计"], [None]], formulas=[("A2", "=1+1")])
        with self.assertRaisesRegex(ValueError, "缺少缓存"):
            advanced._static_pdf_input(source, self.root / "静态.xlsx", None)
        cache(source, "A2", 2)
        replace_entries(source, {"xl/connections.xml": b"<connections/>"})
        with self.assertRaisesRegex(ValueError, "活动数据连接"):
            advanced._static_pdf_input(source, self.root / "静态2.xlsx", None)

    def test_static_pdf_preserves_local_named_ranges_rejects_dynamic_names(self):
        source = book(self.root / "命名范围.xlsx", [["金额"], [2]])
        workbook = load_workbook(source)
        workbook.defined_names.add(DefinedName("金额范围", attr_text="'数据'!$A$2:$A$2"))
        workbook.save(source)
        workbook.close()
        static = self.root / "静态.xlsx"
        advanced._static_pdf_input(source, static, None)
        workbook = load_workbook(static)
        self.assertIn("金额范围", workbook.defined_names)
        workbook.close()
        workbook = load_workbook(source)
        workbook.defined_names.add(DefinedName("动态范围", attr_text="OFFSET('数据'!$A$1,0,0,2)"))
        workbook.save(source)
        workbook.close()
        with self.assertRaisesRegex(ValueError, "动态命名公式"):
            advanced._static_pdf_input(source, self.root / "静态2.xlsx", None)

    def test_pdf_helper_receives_static_copy_and_print_copy_survives_error(self):
        source = book(self.root / "源.xlsx", [["金额"], [2]])
        def fake_convert(input_path, target, options):
            self.assertIn("_cancel", options)
            self.assertTrue(input_path.is_file())
            raise RuntimeError("引擎不兼容")
        with patch.object(advanced.conversion, "convert_office_to_pdf", fake_convert):
            result = advanced.run("excel_print", [source], self.folder, {"output_pdf": True})
        self.assertEqual(len(result["outputs"]), 1)
        self.assertTrue(Path(result["outputs"][0]).is_file())
        self.assertIn("引擎不兼容", result["errors"][0])

    def test_cancel_and_no_source_overwrite(self):
        source = book(self.root / "源.xlsx", [["金额"], [2]])
        with self.assertRaises(CancelledError):
            advanced.run("excel_print", [source], self.folder, {}, cancel=lambda: True)
        first = advanced.run("excel_print", [source], self.root, {})
        second = advanced.run("excel_print", [source], self.root, {})
        self.assertNotEqual(first["outputs"], second["outputs"])
        self.assertTrue(source.is_file())

    def test_oversized_sheet_refused_before_output(self):
        source = book(self.root / "范围.xlsx", [["合计"]])
        workbook = load_workbook(source)
        workbook.active["XFD1048576"] = 1
        workbook.save(source)
        workbook.close()
        result = self.audit(source, [{"left": "[A1]", "right": "1"}])
        self.assertEqual(result["outputs"], [])
        self.assertIn("200万", result["errors"][0])

    def test_all_examples_are_valid_and_work_offline(self):
        for mode in advanced.TOOL_SPECS:
            with self.subTest(mode=mode):
                sources, options = advanced.build_example(mode, self.root / mode)
                advanced.validate(mode, options, sources)
                result = advanced.run(mode, sources, self.folder / mode, options)
                self.assertTrue(result["outputs"])
                self.assertEqual(result["errors"], [])
                self.assertTrue(all(Path(path).is_file() for path in result["outputs"]))


if __name__ == "__main__":
    unittest.main()
