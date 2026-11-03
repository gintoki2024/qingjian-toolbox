"""Real-file regression tests for the spreadsheet batch tools."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904

from qingjian.core.common import CancelledError
from qingjian.core.excel_ops import read_headers, read_table, run_excel


def _source(path, sheets, epoch=None):
    workbook = Workbook()
    workbook.remove(workbook.active)
    if epoch is not None:
        workbook.epoch = epoch
    for title, rows in sheets.items():
        worksheet = workbook.create_sheet(title)
        for r, row in enumerate(rows, 1):
            for c, value in enumerate(row, 1):
                cell = worksheet.cell(r, c, value)
                if isinstance(value, str):
                    cell.data_type = "s"
    workbook.save(path)
    workbook.close()


def _text_source(path, rows, encoding="utf-8-sig", delimiter=","):
    output = io.StringIO(newline="")
    csv.writer(output, delimiter=delimiter).writerows(rows)
    path.write_bytes(output.getvalue().encode(encoding))


def _rows(path, sheet=0):
    workbook = load_workbook(path, data_only=False)
    try:
        worksheet = workbook.worksheets[sheet] if isinstance(sheet, int) else workbook[sheet]
        values = list(worksheet.values)
        return [dict(zip(values[0], row)) for row in values[1:]], values[0]
    finally:
        workbook.close()


def _set_formula_cache(path, coordinate, value, error=False):
    with ZipFile(path) as package:
        entries = {name: package.read(name) for name in package.namelist()}
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    root = ET.fromstring(entries["xl/worksheets/sheet1.xml"])
    for cell in root.iter(namespace + "c"):
        if cell.get("r") == coordinate:
            cached = cell.find(namespace + "v")
            if cached is None:
                cached = ET.SubElement(cell, namespace + "v")
            cached.text = str(value)
            cell.set("t", "e" if error else "n")
    entries["xl/worksheets/sheet1.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as package:
        for name, data in entries.items():
            package.writestr(name, data)


class ExcelOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="excel-qa-", dir=os.environ.get("QINGJIAN_TEST_TMP"))
        self.root = Path(self.temporary.name)
        self.output = self.root / "结果"

    def tearDown(self):
        self.temporary.cleanup()

    def test_merge_aligns_names_all_visible_sheets_preserves_provenance_and_literals(self):
        first, second = self.root / "甲.xlsx", self.root / "乙.csv"
        _source(first, {"甲表": [["ID", "金额", "工具_来源文件"], ["001", 1, "业务列"]],
                        "乙表": [["金额", "ID"], [2, "002"]], "隐藏": [["ID"], ["不应出现"]]})
        workbook = load_workbook(first)
        workbook["隐藏"].sheet_state = "hidden"
        workbook.save(first)
        workbook.close()
        _text_source(second, [["ID", "备注"], ["003", "=SUM(1+1)"]])
        before = hashlib.sha256(first.read_bytes()).hexdigest()
        result = run_excel("excel_merge", [first, second], self.output, {})
        self.assertFalse(result["errors"], result)
        self.assertEqual(result["changes"], 3)
        self.assertEqual(result["data_outputs"], result["outputs"])
        rows, headers = _rows(result["outputs"][0])
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(headers), len(set(headers)))
        self.assertEqual(rows[0]["工具_来源文件"], "业务列")
        self.assertEqual(rows[0]["工具_来源文件__2"], str(first.resolve()))
        self.assertEqual(rows[1]["ID"], "002")
        self.assertEqual(rows[2]["备注"], "=SUM(1+1)")
        workbook = load_workbook(result["outputs"][0], data_only=False)
        cell = workbook.worksheets[0].cell(4, list(headers).index("备注") + 1)
        self.assertEqual(cell.data_type, "s")
        workbook.close()
        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), before)
        self.assertTrue(any("隐藏" in warning for warning in result["warnings"]))

    def test_split_preserves_blank_group_and_sanitized_name_collisions(self):
        source = self.root / "拆分源.csv"
        _text_source(source, [["ID", "分组"], ["001", "A/B"], ["002", "A\\B"], ["003", ""], ["004", " "]])
        result = run_excel("excel_merge", [source], self.output, {"action": "split", "split_column": "分组"})
        self.assertFalse(result["errors"], result)
        self.assertEqual(len(result["outputs"]), 3)
        self.assertEqual(len({Path(path).name for path in result["outputs"]}), 3)
        blank = next(path for path in result["outputs"] if "空分组" in Path(path).name)
        self.assertEqual(len(_rows(blank)[0]), 2)
        self.assertEqual(sum(len(_rows(path)[0]) for path in result["outputs"]), 4)

    def test_recursive_directory_option_controls_descendant_files(self):
        inputs = self.root / "输入"
        nested = inputs / "子目录"
        nested.mkdir(parents=True)
        _text_source(inputs / "本层.csv", [["ID"], ["本层"]])
        _text_source(nested / "下层.csv", [["ID"], ["下层"]])
        direct = run_excel("excel_merge", [inputs], self.output, {"recursive": False})
        recursive = run_excel("excel_merge", [inputs], self.output, {"recursive": True})
        self.assertFalse(direct["errors"], direct)
        self.assertFalse(recursive["errors"], recursive)
        self.assertEqual([row["ID"] for row in _rows(direct["outputs"][0])[0]], ["本层"])
        self.assertEqual({row["ID"] for row in _rows(recursive["outputs"][0])[0]}, {"本层", "下层"})

    def test_clean_values_duplicates_invalid_values_and_identifier_zeros(self):
        source = self.root / "清洗.xlsx"
        _source(source, {"数据": [
            ["编号", "金额", "日期", "必填", "文本", "_source_row"],
            ["00123", "1,234.50", "2026/10/28", " ok ", " =SUM(A1) ", "业务行"],
            ["00123", "1,234.50", "2026/10/28", "ok", "=SUM(A1)", "业务行"],
            [None, None, None, None, None, None],
            ["00007", "12,34", "2026-02-30", None, 123, "原始标记"],
            ["00008", "0012", 60, None, 5, "标记"],
        ]})
        result = run_excel("excel_clean", [source], self.output, {
            "key_columns": ["编号"], "required_columns": ["必填"], "date_columns": ["日期"],
            "number_columns": ["金额"], "text_columns": ["文本"],
        })
        self.assertFalse(result["errors"], result)
        rows, _headers = _rows(result["outputs"][0], "清洗结果")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["编号"], "00123")
        self.assertEqual(rows[0]["金额"], 1234.5)
        self.assertEqual(rows[0]["日期"], datetime(2026, 10, 28))
        self.assertEqual(rows[0]["文本"], "=SUM(A1)")
        self.assertEqual(rows[0]["_source_row"], "业务行")
        self.assertEqual(rows[1]["金额"], "12,34")
        self.assertEqual(rows[1]["日期"], "2026-02-30")
        self.assertEqual(rows[1]["文本"], "123")
        self.assertEqual(rows[2]["金额"], "0012")
        self.assertEqual(rows[2]["日期"], 60)
        audit, _ = _rows(result["outputs"][0], "异常记录")
        self.assertGreaterEqual(len(audit), 7)
        self.assertTrue(any("重复" in str(row["工具_问题"]) for row in audit))
        self.assertTrue(any(row["工具_原值"] == "12,34" for row in audit))

    def test_1904_date_serials_and_1900_leap_bug_are_explicit(self):
        source = self.root / "1904.xlsx"
        _source(source, {"数据": [["编号", "日期"], ["A", 0], ["B", 1]]}, CALENDAR_MAC_1904)
        result = run_excel("excel_clean", [source], self.output, {"date_columns": ["日期"]})
        self.assertFalse(result["errors"], result)
        rows, _ = _rows(result["outputs"][0])
        self.assertEqual(rows[0]["日期"], datetime(1904, 1, 1))
        self.assertEqual(rows[1]["日期"], datetime(1904, 1, 2))

    def test_formatted_epoch_boundary_dates_do_not_silently_shift(self):
        mac, windows = self.root / "格式1904.xlsx", self.root / "格式1900.xlsx"
        _source(mac, {"数据": [["ID", "日期"], ["A", 0]]}, CALENDAR_MAC_1904)
        _source(windows, {"数据": [["ID", "日期"], ["A", 60], ["B", 59]]})
        for path in (mac, windows):
            workbook = load_workbook(path)
            for row in workbook.active.iter_rows(min_row=2):
                row[1].number_format = "yyyy-mm-dd"
            workbook.save(path)
            workbook.close()
        mac_rows = read_table(mac)[1]
        self.assertEqual(mac_rows[0]["日期"], datetime(1904, 1, 1))
        windows_rows = read_table(windows)[1]
        self.assertEqual(windows_rows[0]["日期"], 60)
        self.assertEqual(windows_rows[1]["日期"], datetime(1900, 2, 28))
        result = run_excel("excel_clean", [windows], self.output, {"date_columns": ["日期"]})
        self.assertFalse(result["errors"], result)
        self.assertEqual(len(_rows(result["outputs"][0], "异常记录")[0]), 1)

    def test_compare_composite_keys_tolerance_schema_changes_and_duplicate_exclusion(self):
        old, new = self.root / "旧表.xlsx", self.root / "新表.xlsx"
        _source(old, {"数据": [["ID", "分部", "金额", "名称", "旧字段"],
                              ["A", "x", 100, " Alice ", "旧"],
                              ["B", "x", 10, "重复1", None], ["B", "x", 20, "重复2", None],
                              ["C", "x", 30, "删除", None], [None, "x", 1, "空键", None],
                              ["D", "x", 40, "first", None], ["E", "y", 2, "甲", None], ["E", "x", 5, "乙", None]]})
        _source(new, {"数据": [["ID", "分部", "金额", "名称", "新字段"],
                              ["A", "x", 100.005, "Alice", "新"], ["B", "x", 15, "不可匹配", None],
                              ["F", "x", 22, "新增", None], ["D", "x", 41, "second", None],
                              ["E", "y", 2, "甲", None], ["E", "x", 5, "乙", None]]})
        result = run_excel("excel_compare", [old, new], self.output, {
            "key_columns": ["ID", "分部"], "numeric_tolerance": 0.01,
        })
        self.assertFalse(result["errors"], result)
        self.assertEqual(result["data_outputs"], [])
        report = result["outputs"][0]
        self.assertEqual([row["ID"] for row in _rows(report, "新增记录")[0]], ["F"])
        self.assertEqual([row["ID"] for row in _rows(report, "删除记录")[0]], ["C"])
        modifications, headers = _rows(report, "字段修改")
        amount_changes = [row for row in modifications if row["工具_字段"] == "金额"]
        self.assertEqual(len(amount_changes), 1)
        self.assertEqual(amount_changes[0]["主键_ID"], "D")
        self.assertEqual(len(_rows(report, "主键异常")[0]), 4)
        self.assertEqual(len(_rows(report, "字段变化")[0]), 2)
        self.assertEqual(len(modifications), 10)
        workbook = load_workbook(report)
        self.assertEqual(workbook["新增记录"].cell(2, 1).fill.fgColor.rgb[-6:], "E2F0D9")
        self.assertEqual(workbook["字段修改"].cell(2, list(headers).index("工具_原值") + 1).fill.fgColor.rgb[-6:], "FCE4D6")
        self.assertEqual(workbook["字段修改"].cell(2, list(headers).index("工具_现值") + 1).fill.fgColor.rgb[-6:], "E2F0D9")
        workbook.close()

    def test_formula_cache_missing_is_error_and_cached_result_is_used(self):
        source = self.root / "公式.xlsx"
        _source(source, {"数据": [["ID", "结果"], ["A", None]]})
        workbook = load_workbook(source)
        workbook.active["B2"] = "=1+2"
        workbook.save(source)
        workbook.close()
        self.assertEqual(read_headers(source), ["ID", "结果"])
        with self.assertRaisesRegex(ValueError, "缓存"):
            read_table(source)
        result = run_excel("excel_merge", [source], self.output, {})
        self.assertTrue(result["errors"])
        self.assertFalse(result["outputs"])
        _set_formula_cache(source, "B2", 3)
        headers, rows, warnings = read_table(source)
        self.assertEqual(headers, ["ID", "结果"])
        self.assertEqual(rows[0]["结果"], 3)
        self.assertTrue(any("缓存" in warning for warning in warnings))
        _set_formula_cache(source, "B2", "#DIV/0!", error=True)
        with self.assertRaisesRegex(ValueError, "错误值"):
            read_table(source)

    def test_csv_gb18030_utf16_tsv_and_physical_source_lines(self):
        gbk, tsv = self.root / "中文.csv", self.root / "表格.tsv"
        _text_source(gbk, [["说明"], ["编号", "名称"], ["00001", "中文"], ["00002", "两行\n内容"], ["00003", "末行"]], "gb18030")
        headers, rows, warnings = read_table(gbk, header_row=2)
        self.assertEqual(headers, ["编号", "名称"])
        self.assertEqual(rows[0]["编号"], "00001")
        self.assertEqual(rows[2].source_row, 6)
        self.assertTrue(any("GB18030" in warning for warning in warnings))
        _text_source(tsv, [["编号", "内容"], ["00008", "中文\t列内制表"]], "utf-16", "\t")
        self.assertEqual(read_table(tsv)[1][0]["内容"], "中文\t列内制表")
        self.assertEqual(read_headers(tsv), ["编号", "内容"])

    def test_header_preview_stops_before_invalid_csv_body(self):
        source = self.root / "仅预览.csv"
        source.write_bytes("说明\r\n编号,名称\r\n".encode("utf-8") + b"\xff\xff\xff\xff")
        self.assertEqual(read_headers(source, header_row=2), ["编号", "名称"])
        with self.assertRaises(ValueError):
            read_table(source, header_row=2)

    def test_duplicate_blank_headers_padding_and_source_row_column_do_not_collide(self):
        source = self.root / "表头.xlsx"
        _source(source, {"数据": [["编号", "编号", None, "_source_row"], [123, "第二列", "内容", "业务行号"]]})
        workbook = load_workbook(source)
        workbook.active["A2"].number_format = "000000"
        workbook.save(source)
        workbook.close()
        headers, rows, warnings = read_table(source)
        self.assertEqual(headers, ["编号", "编号__2", "未命名列_3", "_source_row"])
        self.assertEqual(read_headers(source), headers)
        self.assertEqual(rows[0]["编号"], "000123")
        self.assertEqual(rows[0]["_source_row"], "业务行号")
        self.assertEqual(rows[0].source_row, 2)
        self.assertTrue(warnings)

    def test_cancel_before_read_and_after_first_complete_split(self):
        source = self.root / "分组.csv"
        _text_source(source, [["ID", "分组"], ["1", "A"], ["2", "B"]])
        before = source.read_bytes()
        with self.assertRaises(CancelledError):
            run_excel("excel_merge", [source], self.output, {}, cancel=lambda: True)
        stopped = {"value": False}

        def progress(_done, _total, message):
            if message.startswith("已生成分组 1/"):
                stopped["value"] = True

        with self.assertRaises(CancelledError):
            run_excel("excel_merge", [source], self.output, {"action": "split", "split_column": "分组"},
                      progress=progress, cancel=lambda: stopped["value"])
        self.assertEqual(len(list(self.output.glob("*.xlsx"))), 1)
        self.assertFalse(list(self.output.glob(".qingjian-*")))
        self.assertEqual(source.read_bytes(), before)

    def test_existing_output_and_source_in_same_folder_are_not_overwritten(self):
        source = self.root / "表格合并.xlsx"
        _source(source, {"数据": [["ID"], ["001"]]})
        before = source.read_bytes()
        first = run_excel("excel_merge", [source], self.root, {})
        second = run_excel("excel_merge", [source], self.root, {})
        self.assertFalse(first["errors"], first)
        self.assertFalse(second["errors"], second)
        self.assertNotEqual(first["outputs"], second["outputs"])
        self.assertNotEqual(Path(first["outputs"][0]), source)
        self.assertEqual(source.read_bytes(), before)

    def test_clean_output_pipeline_reads_only_primary_sheet(self):
        source = self.root / "流水.csv"
        _text_source(source, [["ID", "金额"], ["001", "坏数字"], ["002", "10"]])
        clean = run_excel("excel_clean", [source], self.output, {"number_columns": ["金额"]})
        self.assertFalse(clean["errors"], clean)
        merged = run_excel("excel_merge", [Path(clean["data_outputs"][0])], self.output, {})
        self.assertFalse(merged["errors"], merged)
        self.assertEqual(len(_rows(merged["outputs"][0])[0]), 2)
        self.assertTrue(any("主数据页" in warning for warning in merged["warnings"]))

    def test_precision_leading_zero_underflow_and_ambiguous_numbers_stay_literal(self):
        source = self.root / "精度.csv"
        values = ["9999999999999999", "00123", "1e-10000", "12,34", "００１２", "1.25"]
        _text_source(source, [["ID", "金额"]] + [[str(index), value] for index, value in enumerate(values)])
        result = run_excel("excel_clean", [source], self.output, {"number_columns": ["金额"]})
        self.assertFalse(result["errors"], result)
        rows, _ = _rows(result["outputs"][0])
        self.assertEqual([row["金额"] for row in rows[:-1]], values[:-1])
        self.assertEqual(rows[-1]["金额"], 1.25)
        self.assertEqual(len(_rows(result["outputs"][0], "异常记录")[0]), 5)

    def test_xls_and_missing_key_are_explicit_errors(self):
        old = self.root / "旧版.xls"
        old.write_bytes(b"not a supported workbook")
        with self.assertRaisesRegex(ValueError, "XLS"):
            read_table(old)
        source = self.root / "数据.csv"
        _text_source(source, [["ID"], ["A"]])
        compare = run_excel("excel_compare", [source, old], self.output, {})
        self.assertTrue(compare["errors"])
        self.assertFalse(compare["outputs"])
        clean = run_excel("excel_clean", [source], self.output, {"required_columns": ["不存在"]})
        self.assertTrue(clean["errors"])
        self.assertFalse(clean["outputs"])


if __name__ == "__main__":
    unittest.main()
