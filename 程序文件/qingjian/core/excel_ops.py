"""Offline value-table merge, cleaning and reconciliation.

Inputs: XLSX/XLSM/CSV/TSV; legacy XLS is deliberately unsupported. Formula
results come only from Excel's saved caches and are never calculated here.
``read_table`` uses the first visible nonempty-header sheet by default. Merge
uses all such visible sheets, except tool-generated workbooks, whose marked
primary data sheets are used. Cleaning uses one sheet per file. Outputs contain
values, provenance and audit sheets, not the original charts, styles or macros.

Whitespace comparison means leading/trailing whitespace, not spaces within a
value. Numeric tolerance applies to numeric source cells, not identifier text.
"""

from __future__ import annotations

import csv
import codecs
import io
import itertools
import json
import math
import posixpath
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.styles.numbers import is_datetime
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, from_excel

from .common import (
    CancelledError, atomic_write, ensure_cancel, expand_inputs, make_result,
    notify, unique_path,
)


_EXTENSIONS = {".xlsx", ".xlsm", ".csv", ".tsv"}
_DATA_MARKER = "qingjian:data_sheets="
_STANDARD_WARNING = "结果为标准值表，不保留原工作簿样式、图表、公式或宏；原文件不修改。"
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_GROUPED_NUMBER = re.compile(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?\Z")
_MAX_INPUT_CELLS = 5_000_000
_MAX_SPLIT_GROUPS = 2000


class TableRow(dict):
    """A regular column dict with collision-free provenance attributes."""

    def __init__(self, values=(), *, source_row=0, source_sheet="", source_file="",
                 epoch=CALENDAR_WINDOWS_1900):
        super().__init__(values)
        self.source_row = int(source_row)
        self.source_sheet = str(source_sheet)
        self.source_file = str(source_file)
        self.epoch = epoch

    def with_values(self, values):
        return TableRow(values, source_row=self.source_row, source_sheet=self.source_sheet,
                        source_file=self.source_file, epoch=self.epoch)


@dataclass
class _Table:
    headers: list[str]
    rows: list[TableRow]
    sheet: str
    path: Path


def _warn(warnings: list[str], text: str):
    if text not in warnings:
        warnings.append(text)


def _result(outputs, changes, warnings=(), errors=(), data_outputs=()):
    result = make_result(outputs, changes, warnings, errors)
    result["data_outputs"] = [str(path) for path in data_outputs]
    return result


def _bool(options, key, default=False):
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须为布尔值。")
    return value


def _column_list(options, key):
    value = options.get(key, [])
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} 必须为列名列表。")
    result = [item for item in value if item]
    if len(result) != len(set(result)):
        raise ValueError(f"{key} 中存在重复列名。")
    return result


def _header_row(value):
    if isinstance(value, bool):
        raise ValueError("表头行必须为正整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("表头行必须为正整数。") from exc
    if number < 1 or number > 1048576 or str(number) != str(value).strip():
        raise ValueError("表头行必须为 1–1048576 的整数。")
    return number


def _display(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def _blank(value):
    return value is None or isinstance(value, str) and not value.strip()


def _headers(values, width, warnings):
    result, used = [], set()
    for index in range(width):
        raw = values[index] if index < len(values) else None
        base = _display(raw).strip() or f"未命名列_{index + 1}"
        name, number = base, 2
        while name in used:
            name = f"{base}__{number}"
            number += 1
        if name != base:
            _warn(warnings, "表头含重名列，已添加 __2 等后缀区分，未合并或覆盖列。")
        if _blank(raw):
            _warn(warnings, "部分数据列没有表头，已使用“未命名列_列号”。")
        result.append(name)
        used.add(name)
    return result


def _table_from_raw(path, sheet, raw, warnings, epoch=CALENDAR_WINDOWS_1900):
    if not raw or not any(not _blank(value) for value in raw[0][1]):
        raise ValueError("所选表头行为空，请指定正确的表头行。")
    while len(raw) > 1 and all(_blank(value) for value in raw[-1][1]):
        raw.pop()
    width = max((index + 1 for _row, values in raw for index, value in enumerate(values)
                 if not _blank(value)), default=0)
    names = _headers(raw[0][1], width, warnings)
    rows = [TableRow(((name, values[index] if index < len(values) else None)
                     for index, name in enumerate(names)), source_row=row,
                    source_sheet=sheet, source_file=str(path.resolve()), epoch=epoch)
            for row, values in raw[1:]]
    return _Table(names, rows, sheet, path)


def _csv_table(path, header_row, warnings, cancel):
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("文本表格超过 512 MB，请分批处理。")
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings = ["utf-32"]
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    else:
        encodings = ["utf-8-sig", "gb18030"]
    text, encoding = None, ""
    for candidate in encodings:
        try:
            text = data.decode(candidate, errors="strict")
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if text is None or "\x00" in text:
        raise ValueError("无法可靠识别文本编码；请另存为 UTF-8、带 BOM 的 UTF-16 或 GB18030。")
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    if path.suffix.lower() == ".csv":
        try:
            delimiter = csv.Sniffer().sniff(text[:65536], delimiters=",;\t").delimiter
        except csv.Error:
            pass
    raw, previous_line = [], 0
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        for record, values in enumerate(reader, 1):
            start_line, previous_line = previous_line + 1, reader.line_num
            if record < header_row:
                continue
            if record % 128 == 0:
                ensure_cancel(cancel)
            raw.append((start_line, values))
    except csv.Error as exc:
        raise ValueError(f"CSV/TSV 结构无效：{exc}") from exc
    _warn(warnings, f"{path.name}：按 {encoding.upper()} 读取文本；字段保留为文本，来源行表示记录起始物理行。")
    return _table_from_raw(path, path.stem, raw, warnings)


def _check_xlsx(path):
    try:
        with ZipFile(path) as package:
            infos = package.infolist()
            if len(infos) > 20000 or sum(info.file_size for info in infos) > 512 * 1024 * 1024:
                raise ValueError("工作簿解压后超过 512 MB 或内部文件过多，请分批处理。")
            if len({info.filename for info in infos}) != len(infos):
                raise ValueError("工作簿 ZIP 包含重复条目，不能可靠读取。")
    except BadZipFile as exc:
        raise ValueError("文件不是有效的 XLSX/XLSM，或已加密；请先用 Excel 解密并另存。") from exc


def _raw_date_serials(path, sheet_name, coordinates, cancel=None):
    """Recover only epoch edge cases normalized by openpyxl on input."""
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    relation_id = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    result = {}
    with ZipFile(path) as package:
        workbook = ET.fromstring(package.read("xl/workbook.xml"))
        sheets = workbook.find(namespace + "sheets")
        identifier = next((sheet.get(relation_id) for sheet in sheets if sheet.get("name") == sheet_name), None)
        relationships = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
        target = next((item.get("Target") for item in relationships if item.get("Id") == identifier), None)
        if not target:
            raise ValueError("无法核验日期系统边界值，请先把日期列在 Excel 中另存为明确文本。")
        part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        with package.open(part) as stream:
            count = 0
            for _event, element in ET.iterparse(stream, events=("end",)):
                if element.tag == namespace + "c":
                    count += 1
                    if count % 1024 == 0:
                        ensure_cancel(cancel)
                    coordinate = element.get("r")
                    if coordinate in coordinates and element.get("t", "n") == "n":
                        value = element.find(namespace + "v")
                        if value is not None and value.text:
                            try:
                                number = Decimal(value.text)
                                result[coordinate] = int(number) if number == number.to_integral_value() else float(number)
                            except (InvalidOperation, ValueError, OverflowError):
                                pass
                    element.clear()
                elif element.tag == namespace + "row":
                    element.clear()
    return result


def _read_excel_sheet(path, formulas, cached, name, header_row, warnings, cancel):
    formula_sheet, value_sheet = formulas[name], cached[name]
    max_row = max(formula_sheet.max_row or 0, value_sheet.max_row or 0)
    max_col = max(formula_sheet.max_column or 0, value_sheet.max_column or 0)
    if max_row * max_col > _MAX_INPUT_CELLS:
        raise ValueError("工作表使用区域超过 500 万个单元格；请拆分或先清理多余格式占用。")
    raw, formula_count, date_edges = [], 0, {}
    source_rows = formula_sheet.iter_rows(min_row=header_row, max_row=max_row or None, max_col=max_col or None)
    value_rows = value_sheet.iter_rows(min_row=header_row, max_row=max_row or None, max_col=max_col or None)
    for row_number, (formula_cells, value_cells) in enumerate(zip(source_rows, value_rows), header_row):
        if row_number % 128 == 0:
            ensure_cancel(cancel)
        values = []
        for column, (original, saved) in enumerate(zip(formula_cells, value_cells), 1):
            value = saved.value
            coordinate = f"{get_column_letter(column)}{row_number}"
            if original.data_type == "f":
                formula_count += 1
                if value is None:
                    raise ValueError(f"工作表“{name}”的 {coordinate} 公式缺少可用缓存；请用 Excel 重算并保存，或先粘贴为值。")
                if saved.data_type == "e":
                    raise ValueError(f"工作表“{name}”的 {coordinate} 公式缓存为 Excel 错误值；请先修复源表。")
            elif saved.data_type == "e":
                _warn(warnings, f"工作表“{name}”含 Excel 错误值，已作为文字保留；请人工核查。")
            number_format = getattr(original, "number_format", "") or ""
            if ((isinstance(value, datetime) and formulas.epoch == CALENDAR_WINDOWS_1900
                 and value.date() == date(1900, 2, 28))
                    or (isinstance(value, time) and is_datetime(number_format) in {"date", "datetime"})):
                date_edges[coordinate] = (len(raw), len(values))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if re.fullmatch(r"0{2,}", number_format) and math.isfinite(value) and value == int(value):
                    value = ("-" if value < 0 else "") + str(abs(int(value))).zfill(len(number_format))
                    _warn(warnings, "纯零占位格式的编号已按显示位数转为文本，以保留前导零。")
                elif isinstance(value, int) and len(str(abs(value))) > 15:
                    value = str(value)
                    _warn(warnings, "超过 15 位的整数已按文本保留；源表若已发生 Excel 精度损失，本工具不能还原。")
            values.append(value)
        raw.append((row_number, values))
    if date_edges:
        serials = _raw_date_serials(path, name, set(date_edges), cancel)
        for coordinate, serial in serials.items():
            r, c = date_edges[coordinate]
            if formulas.epoch == CALENDAR_MAC_1904 and 0 <= serial < 1:
                raw[r][1][c] = formulas.epoch + timedelta(days=serial)
            elif formulas.epoch == CALENDAR_WINDOWS_1900 and (0 <= serial < 1 or 60 <= serial < 61):
                raw[r][1][c] = serial
                _warn(warnings, "遇到 Excel 1900 日期系统的无效 0/60 序号，保留原序号而未伪造日期；日期清洗会记录异常。")
    if formula_count:
        _warn(warnings, "公式使用 Excel 已保存的缓存值，未重新计算；缓存可能过期，请先重算并保存源表。")
    return _table_from_raw(path, name, raw, warnings, formulas.epoch)


def _primary_sheets(workbook, warnings):
    keywords = workbook.properties.keywords or ""
    if not keywords.startswith(_DATA_MARKER):
        return []
    try:
        names = json.loads(keywords[len(_DATA_MARKER):])
    except (ValueError, TypeError):
        return []
    if isinstance(names, list) and names and all(isinstance(name, str) and name in workbook.sheetnames for name in names):
        _warn(warnings, "检测到青简结果文件，默认仅读取其主数据页；辅助异常/摘要页可通过指定工作表名读取。")
        return names
    return []


def _load_tables(path, sheet="", header_row=1, all_sheets=False, cancel=None):
    path = Path(path)
    ensure_cancel(cancel)
    if not path.is_file():
        raise FileNotFoundError(f"找不到输入文件：{path.name}")
    suffix = path.suffix.lower()
    if suffix == ".xls":
        raise ValueError("不支持旧版 XLS；请先用 Excel 另存为 XLSX。")
    if suffix not in _EXTENSIONS:
        raise ValueError("仅支持 XLSX、XLSM、CSV 和 TSV。")
    header_row = _header_row(header_row)
    warnings = []
    if suffix in {".csv", ".tsv"}:
        if sheet:
            _warn(warnings, "CSV/TSV 没有工作表，已忽略指定的工作表名称。")
        return [_csv_table(path, header_row, warnings, cancel)], warnings
    _check_xlsx(path)
    formulas = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    cached = None
    try:
        cached = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        if sheet:
            if sheet not in formulas.sheetnames:
                raise ValueError(f"找不到工作表“{sheet}”。")
            names = [sheet]
        else:
            names = _primary_sheets(formulas, warnings)
            if not names:
                names = [name for name in formulas.sheetnames if formulas[name].sheet_state == "visible"]
                if len(names) != len(formulas.sheetnames):
                    _warn(warnings, "默认跳过隐藏工作表；可指定表名读取。")
        tables = []
        for name in names:
            ensure_cancel(cancel)
            try:
                table = _read_excel_sheet(path, formulas, cached, name, header_row, warnings, cancel)
            except ValueError as exc:
                if not sheet and str(exc).startswith("所选表头行为空"):
                    _warn(warnings, f"已跳过表头行为空的工作表“{name}”。")
                    continue
                raise
            tables.append(table)
            if not all_sheets:
                break
        if not tables:
            raise ValueError("没有找到可用的表头；请检查工作表和表头行。")
        if suffix == ".xlsm":
            _warn(warnings, "XLSM 仅提取单元格值，输出不含宏；原宏文件保持不变。")
        return tables, warnings
    finally:
        formulas.close()
        if cached is not None:
            cached.close()


def read_table(path: Path, sheet="", header_row=1):
    """Return headers, ordinary dict-like rows and warnings.

    Provenance uses ``row.source_row``, ``row.source_sheet`` and
    ``row.source_file`` attributes, so a business column named ``_source_row``
    or ``来源文件`` is never overwritten. Only the selected/first data sheet is
    returned. Errors, especially absent formula caches, are explicit exceptions.
    """
    tables, warnings = _load_tables(path, sheet, header_row)
    return tables[0].headers, tables[0].rows, warnings


def _decoded_lines(stream, encoding):
    """Decode through the requested record, without decoding later data."""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    line, carriage_return = [], False
    while True:
        raw = stream.read(1)
        text = decoder.decode(raw, final=not raw)
        for character in text:
            if carriage_return:
                carriage_return = False
                if character == "\n":
                    continue
            line.append(character)
            if character == "\r":
                carriage_return = True
                completed, line = "".join(line), []
                yield completed
            elif character == "\n":
                completed, line = "".join(line), []
                yield completed
        if not raw:
            if line:
                yield "".join(line)
            return


def _csv_headers(path, header_row):
    with path.open("rb") as stream:
        prefix = stream.read(4)
    if prefix.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings = ["utf-32"]
    elif prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    else:
        encodings = ["utf-8-sig", "gb18030"]
    last_error = None
    for encoding in encodings:
        try:
            with path.open("rb") as stream:
                lines = _decoded_lines(stream, encoding)
                sample = list(itertools.islice(lines, header_row))
                delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
                if path.suffix.lower() == ".csv":
                    try:
                        delimiter = csv.Sniffer().sniff("".join(sample), delimiters=",;\t").delimiter
                    except csv.Error:
                        pass
                reader = csv.reader(itertools.chain(sample, lines), delimiter=delimiter, strict=True)
                header = next(itertools.islice(reader, header_row - 1, header_row), None)
                if not header or not any(not _blank(value) for value in header):
                    raise ValueError("所选表头行为空，请指定正确的表头行。")
                return _headers(header, len(header), [])
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError("无法识别表头编码，请另存为 UTF-8、带 BOM 的 UTF-16 或 GB18030。") from last_error


def read_headers(path: Path, sheet="", header_row=1) -> list[str]:
    """Preview only header cells, without validating data-area formula caches.

    CSV/TSV decoding stops at the header record. XLSX uses the selected/default
    visible data sheet and its declared column extent; empty names and duplicate
    names follow ``read_table`` rules. Formatting-only trailing XLSX columns may
    therefore appear as unnamed columns in this lightweight preview.
    """
    path = Path(path)
    header_row = _header_row(header_row)
    if not path.is_file():
        raise FileNotFoundError(f"找不到输入文件：{path.name}")
    suffix = path.suffix.lower()
    if suffix == ".xls":
        raise ValueError("不支持旧版 XLS；请先另存为 XLSX。")
    if suffix in {".csv", ".tsv"}:
        return _csv_headers(path, header_row)
    if suffix not in {".xlsx", ".xlsm"}:
        raise ValueError("仅支持 XLSX、XLSM、CSV 和 TSV。")
    _check_xlsx(path)
    workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        if sheet:
            if sheet not in workbook.sheetnames:
                raise ValueError(f"找不到工作表“{sheet}”。")
            names = [sheet]
        else:
            names = _primary_sheets(workbook, []) or [name for name in workbook.sheetnames
                                                     if workbook[name].sheet_state == "visible"]
        for name in names:
            worksheet = workbook[name]
            row = next(worksheet.iter_rows(min_row=header_row, max_row=header_row, values_only=True), ())
            if any(not _blank(value) for value in row):
                return _headers(list(row), len(row), [])
            if sheet:
                break
        raise ValueError("表头行为空或公式表头没有已保存缓存，请检查表名和表头行。")
    finally:
        workbook.close()


def _metadata_names(headers, labels=("来源文件", "来源工作表", "来源行")):
    used, result = set(headers), []
    for label in labels:
        base, name, index = "工具_" + label, "工具_" + label, 2
        while name in used:
            name = f"{base}__{index}"
            index += 1
        used.add(name)
        result.append(name)
    return result


def _provenance(row):
    return [row.source_file, row.source_sheet, row.source_row]


def _write_book(target, sheets, *, primary=(), row_colors=None, cell_colors=None, cancel=None):
    """Literal-string, styled value output with atomic no-overwrite publishing."""
    row_colors, cell_colors = row_colors or {}, cell_colors or {}

    def writer(staged):
        workbook = Workbook()
        workbook.remove(workbook.active)
        workbook.properties.creator = "青简工具箱"
        workbook.properties.keywords = _DATA_MARKER + json.dumps(list(primary), ensure_ascii=False)
        try:
            for title, rows in sheets.items():
                ensure_cancel(cancel)
                worksheet = workbook.create_sheet(title)
                widths = {}
                for r, row in enumerate(rows, 1):
                    if r > 1048576 or len(row) > 16384:
                        raise ValueError("结果超出 Excel 工作表行列上限，请拆分处理。")
                    if r % 128 == 0:
                        ensure_cancel(cancel)
                    for c, value in enumerate(row, 1):
                        if isinstance(value, Path):
                            value = str(value)
                        if isinstance(value, str):
                            if len(value) > 32767:
                                raise ValueError("结果单元格超过 32767 字符，未截断数据；请先缩小文本内容。")
                            if _INVALID_XML.search(value):
                                raise ValueError("结果含 Excel 不支持的控制字符；未删除原文，请先修正输入。")
                        if isinstance(value, datetime) and value.tzinfo is not None:
                            raise ValueError("Excel 不支持带时区时间，未自动改变时区；请先规范输入。")
                        cell = worksheet.cell(r, c, value)
                        if isinstance(value, str):
                            cell.data_type = "s"  # Preserve =, +, -, @ text literally.
                        cell.alignment = Alignment(vertical="top", wrap_text=True)
                        cell.font = Font(name="微软雅黑", size=10, bold=r == 1,
                                         color="FFFFFF" if r == 1 else "24352F")
                        if r == 1:
                            color = "287D70"
                        else:
                            color = cell_colors.get(title, {}).get(c) or row_colors.get(title)
                            color = color or ("F0F6F4" if r % 2 == 0 else "FFFFFF")
                        cell.fill = PatternFill("solid", fgColor=color)
                        if r <= 250:
                            width = max((len(line) for line in _display(value).splitlines()), default=0)
                            widths[c] = max(widths.get(c, 10), min(60, width * 1.25 + 3))
                worksheet.freeze_panes = "A2"
                worksheet.auto_filter.ref = worksheet.dimensions
                for column, width in widths.items():
                    worksheet.column_dimensions[get_column_letter(column)].width = width
            ensure_cancel(cancel)
            workbook.save(staged)
        finally:
            workbook.close()
    atomic_write(target, writer)


def _expand_sources(sources, folder, recursive=True):
    result, seen = [], set()
    for source in sources:
        path = Path(source)
        values = expand_inputs([path], extensions=_EXTENSIONS | {".xls"} if path.is_dir() else None,
                               recursive=recursive, exclude_root=folder if path.is_dir() else None)
        for value in values:
            key = str(value.resolve()).casefold()
            if key not in seen:
                seen.add(key)
                result.append(value)
    return result


def _union_headers(tables):
    return list(dict.fromkeys(header for table in tables for header in table.headers))


def _atom(value, ignore_whitespace=False):
    if value is None:
        return ("blank", "")
    if isinstance(value, str):
        value = value.strip() if ignore_whitespace else value
        return ("blank", "") if value == "" else ("text", value)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
            return ("number", number.normalize()) if number.is_finite() else ("other", str(value))
        except InvalidOperation:
            return ("other", str(value))
    if isinstance(value, (datetime, date, time)):
        return (type(value).__name__, value.isoformat())
    if isinstance(value, timedelta):
        return ("timedelta", value.total_seconds())
    return (type(value).__name__, str(value))


def _merge(files, folder, options, progress, cancel):
    warnings, errors, tables = [_STANDARD_WARNING], [], []
    action = options.get("action", "merge")
    if action not in {"merge", "split"}:
        raise ValueError("合并动作必须为 merge 或 split。")
    for done, path in enumerate(files, 1):
        ensure_cancel(cancel)
        notify(progress, done - 1, len(files) + 1, f"读取 {path.name}")
        try:
            current, current_warnings = _load_tables(path, options.get("sheet", ""),
                                                   options.get("header_row", 1), True, cancel)
            tables.extend(current)
            for warning in current_warnings:
                _warn(warnings, warning)
        except CancelledError:
            raise
        except Exception as exc:
            errors.append(f"{path.name}：{exc}")
    if not tables:
        return _result([], 0, warnings, errors or ["没有可处理的表格。"])
    if errors:
        _warn(warnings, "部分输入读取失败，结果只包含成功读取的文件；请核对 errors 后再使用。")
    headers = _union_headers(tables)
    rows = [row for table in tables for row in table.rows]
    metadata = _metadata_names(headers)
    output_headers = headers + metadata
    information = [["项目", "说明"], ["输出类型", "标准值表"], ["来源工作表数", len(tables)],
                   ["数据记录数", len(rows)], ["默认工作表", "普通文件读取全部可见且有表头的工作表；青简产物只读取标记主数据页"],
                   ["来源列", "工具_来源文件/工作表/行；与数据列冲突时自动加后缀"],
                   ["公式", "只读 Excel 已保存缓存，不计算公式"], ["限制", _STANDARD_WARNING]]
    outputs = []
    if action == "merge":
        ensure_cancel(cancel)
        target = unique_path(folder, "表格合并.xlsx")
        data = [output_headers] + [[row.get(header) for header in headers] + _provenance(row) for row in rows]
        _write_book(target, {"合并数据": data, "处理说明": information}, primary=["合并数据"], cancel=cancel)
        outputs.append(target)
    else:
        column = options.get("split_column", "")
        if not isinstance(column, str) or not column or column not in headers:
            raise ValueError("拆分必须指定存在的 split_column 列名。")
        if any(column not in table.headers for table in tables):
            _warn(warnings, "部分来源工作表没有拆分列，对应记录归入空分组并保留来源。")
        groups = OrderedDict()
        for row in rows:
            value = row.get(column)
            key = ("blank", "") if _blank(value) else _atom(value)
            groups.setdefault(key, (value, []))[1].append(row)
        if len(groups) > _MAX_SPLIT_GROUPS:
            raise ValueError("拆分分组超过 2000 个，请更换分组列或分批处理。")
        for number, (value, group) in enumerate(groups.values(), 1):
            ensure_cancel(cancel)
            label = "空分组" if _blank(value) else _display(value)
            target = unique_path(folder, f"拆分_{label[:100]}.xlsx")
            data = [output_headers] + [[row.get(header) for header in headers] + _provenance(row) for row in group]
            group_info = information + [["拆分列", column], ["分组值", value], ["本分组记录数", len(group)]]
            try:
                _write_book(target, {"拆分数据": data, "处理说明": group_info}, primary=["拆分数据"], cancel=cancel)
                outputs.append(target)
            except CancelledError:
                raise
            except Exception as exc:
                errors.append(f"分组 {number}：{exc}")
            notify(progress, number, len(groups), f"已生成分组 {number}/{len(groups)}")
        if not groups:
            _warn(warnings, "表格只有表头，没有可拆分的数据记录。")
    notify(progress, len(files) + 1, len(files) + 1, "表格处理完成")
    return _result(outputs, len(rows), warnings, errors, outputs)


def _convert_number(value):
    if _blank(value):
        return value, None
    if isinstance(value, bool) or isinstance(value, (datetime, date, time, timedelta)):
        return value, "类型不是可转换的数值，已保留原值"
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            return value, "非有限数值，已保留原值"
        return value, None
    if not isinstance(value, str):
        return value, "无法识别数值，已保留原值"
    if not (_NUMBER.fullmatch(value) or _GROUPED_NUMBER.fullmatch(value)):
        return value, "数值格式无效；不推测货币、百分比或破损数字"
    plain = value.replace(",", "")
    if re.match(r"[+-]?0\d", plain):
        return value, "文本含前导零，为保护编号未转为数字"
    try:
        number = Decimal(plain)
        if not number.is_finite():
            raise InvalidOperation
        integer = number == number.to_integral_value()
        digits = list(number.as_tuple().digits)
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
        significant = len(digits)
        if significant > 15 or integer and number != 0 and number.adjusted() > 14:
            return value, "数值超过 Excel 15 位有效精度，已按原文本保留"
        if number != 0 and number.adjusted() < -307:
            return value, "数值过小，可能在 Excel 中下溢为零，已保留原文本"
        converted = int(number) if integer else float(number)
        if isinstance(converted, float) and not math.isfinite(converted):
            return value, "数值超出可保存范围，已保留原文本"
        return converted, None
    except (InvalidOperation, ValueError, OverflowError):
        return value, "数值格式无效，已保留原值"


def _convert_date(value, epoch):
    if _blank(value):
        return value, None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return _display(value), "时间带时区，未擅自转换时区"
        return value, None
    if isinstance(value, date):
        return value, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value < 0 or epoch != CALENDAR_MAC_1904 and value < 1:
            return value, "无效或有歧义的 Excel 日期序号，已保留原值"
        if epoch == CALENDAR_WINDOWS_1900 and 60 <= value < 61:
            return value, "Excel 1900 日期系统的虚构 2 月 29 日，未自动修正"
        try:
            converted = (epoch + timedelta(days=value) if epoch == CALENDAR_MAC_1904 and value < 1
                         else from_excel(value, epoch=epoch))
            if not isinstance(converted, datetime):
                return value, "日期序号仅含时间，未推测日期"
            return converted.date() if converted.time() == time() else converted, None
        except (ValueError, OverflowError, TypeError):
            return value, "日期序号超出范围，已保留原值"
    if isinstance(value, str):
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].+)?", value):
                converted = datetime.fromisoformat(value) if len(value) > 10 else date.fromisoformat(value)
                if isinstance(converted, datetime) and converted.tzinfo is not None:
                    return value, "时间带时区，未擅自转换时区"
                return converted, None
            if re.fullmatch(r"\d{4}[/\.]\d{1,2}[/\.]\d{1,2}", value):
                return datetime.strptime(value, "%Y/%m/%d" if "/" in value else "%Y.%m.%d").date(), None
            if re.fullmatch(r"\d{8}", value):
                return datetime.strptime(value, "%Y%m%d").date(), None
            match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
            if match:
                return date(*(int(part) for part in match.groups())), None
        except (ValueError, OverflowError):
            pass
    return value, "日期格式无效或有歧义；仅接受明确年月日或有效 Excel 日期序号"


def _clean(files, folder, options, progress, cancel):
    warnings, errors, outputs, changes = [_STANDARD_WARNING], [], [], 0
    trim = _bool(options, "trim_text", True)
    remove_blank = _bool(options, "remove_blank_rows", True)
    deduplicate = _bool(options, "deduplicate", True)
    keys = _column_list(options, "key_columns")
    required = _column_list(options, "required_columns")
    dates = _column_list(options, "date_columns")
    numbers = _column_list(options, "number_columns")
    texts = _column_list(options, "text_columns")
    if len(set(dates + numbers + texts)) != len(dates + numbers + texts):
        raise ValueError("同一列不能同时指定为日期、数值和文本。")
    for done, path in enumerate(files, 1):
        ensure_cancel(cancel)
        notify(progress, done - 1, len(files), f"清洗 {path.name}")
        try:
            tables, current_warnings = _load_tables(path, options.get("sheet", ""),
                                                   options.get("header_row", 1), False, cancel)
            table = tables[0]
            for warning in current_warnings:
                _warn(warnings, warning)
            missing = set(keys + required + dates + numbers + texts) - set(table.headers)
            if missing:
                raise ValueError("指定列不存在：" + "、".join(sorted(missing)))
            cleaned, issues, seen = [], [], {}
            removed_blank = removed_duplicate = file_changes = 0
            for index, row in enumerate(table.rows, 1):
                if index % 128 == 0:
                    ensure_cancel(cancel)
                original = dict(row)
                values = dict(row)

                def issue(column, reason, result="保留原值"):
                    issues.append(_provenance(row) + [column, reason, original.get(column), result]
                                  + [original.get(header) for header in table.headers])

                if trim:
                    for column, value in values.items():
                        if isinstance(value, str) and value != value.strip():
                            values[column] = value.strip()
                            file_changes += 1
                if remove_blank and all(_blank(value) for value in values.values()):
                    removed_blank += 1
                    file_changes += 1
                    continue
                for column in required:
                    if _blank(values[column]):
                        issue(column, "必填值为空", "保留该行并标记异常")
                for column in dates:
                    value, problem = _convert_date(values[column], row.epoch)
                    if problem:
                        issue(column, problem)
                    elif _atom(value) != _atom(values[column]):
                        file_changes += 1
                    values[column] = value
                for column in numbers:
                    value, problem = _convert_number(values[column])
                    if problem:
                        issue(column, problem)
                    elif _atom(value) != _atom(values[column]):
                        file_changes += 1
                    values[column] = value
                for column in texts:
                    value = values[column]
                    if value is not None and not isinstance(value, str):
                        values[column] = _display(value)
                        file_changes += 1
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            _warn(warnings, "数值单元格转文本只保留当前数值；没有格式证据时不推测或补齐编号前导零。")
                cleaned_row = row.with_values(values)
                if deduplicate:
                    if keys and any(_blank(values[column]) for column in keys):
                        issue("、".join(keys), "去重主键为空", "保留该行，未自动去重")
                    else:
                        key = tuple(_atom(values[column]) for column in (keys or table.headers))
                        if key in seen:
                            issue("、".join(keys), f"重复记录；首次记录来源行 {seen[key]}", "删除该重复行；本页保留原始字段")
                            removed_duplicate += 1
                            file_changes += 1
                            continue
                        seen[key] = row.source_row
                cleaned.append(cleaned_row)
            provenance = _metadata_names(table.headers)
            data = [table.headers + provenance] + [[row.get(header) for header in table.headers] + _provenance(row)
                                                  for row in cleaned]
            issue_meta = _metadata_names(["原始_" + header for header in table.headers],
                                         ("来源文件", "来源工作表", "来源行", "列名", "问题", "原值", "处理结果"))
            audit = [issue_meta + ["原始_" + header for header in table.headers]] + issues
            summary = [["项目", "数量/说明"], ["输入记录", len(table.rows)], ["输出记录", len(cleaned)],
                       ["删除空行", removed_blank], ["删除重复行", removed_duplicate], ["异常条目", len(issues)],
                       ["有效改动", file_changes], ["日期系统", "1904" if table.rows and table.rows[0].epoch == CALENDAR_MAC_1904 else "1900"],
                       ["说明", "非法值保留；重复行原始字段保存在异常记录；不猜测损坏数字"], ["输出限制", _STANDARD_WARNING]]
            target = unique_path(folder, f"{path.stem[:110]}_清洗.xlsx")
            _write_book(target, {"清洗结果": data, "异常记录": audit, "处理摘要": summary},
                        primary=["清洗结果"], row_colors={"异常记录": "FFF2CC"}, cancel=cancel)
            outputs.append(target)
            changes += file_changes
            if issues:
                _warn(warnings, f"{path.name}：发现 {len(issues)} 条异常，请查看“异常记录”页。")
        except CancelledError:
            raise
        except Exception as exc:
            errors.append(f"{path.name}：{exc}")
        notify(progress, done, len(files), f"已处理 {done}/{len(files)} 个文件")
    return _result(outputs, changes, warnings, errors, outputs)


def _equal(left, right, ignore_whitespace, tolerance):
    a, b = _atom(left, ignore_whitespace), _atom(right, ignore_whitespace)
    if a[0] == b[0] == "number":
        return abs(a[1] - b[1]) <= tolerance
    return a == b


def _compare(files, folder, options, progress, cancel):
    if len(files) != 2:
        raise ValueError("表格对账必须恰好选择两个输入文件，顺序为旧表、新表。")
    keys = _column_list(options, "key_columns")
    if not keys:
        raise ValueError("表格对账必须指定至少一个主键列 key_columns。")
    ignore = _bool(options, "ignore_whitespace", True)
    try:
        tolerance = Decimal(str(options.get("numeric_tolerance", 0)))
        if not tolerance.is_finite() or tolerance < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("数值容差必须为有限非负数。") from exc
    warnings = [_STANDARD_WARNING, "数值容差仅用于实际数值单元格；文本编号和 CSV 文本不自动转数字。"]
    tables = []
    for index, (path, sheet_option) in enumerate(zip(files, ("sheet_left", "sheet_right")), 1):
        ensure_cancel(cancel)
        notify(progress, index - 1, 3, f"读取{'旧表' if index == 1 else '新表'} {path.name}")
        current, current_warnings = _load_tables(path, options.get(sheet_option, ""),
                                               options.get("header_row", 1), False, cancel)
        tables.append(current[0])
        for warning in current_warnings:
            _warn(warnings, warning)
    left, right = tables
    for label, table in (("旧表", left), ("新表", right)):
        missing = set(keys) - set(table.headers)
        if missing:
            raise ValueError(label + "缺少主键列：" + "、".join(sorted(missing)))
    headers = _union_headers(tables)
    groups, exceptions = [], []
    for label, table in (("旧表", left), ("新表", right)):
        index = defaultdict(list)
        for number, row in enumerate(table.rows, 1):
            if number % 128 == 0:
                ensure_cancel(cancel)
            if any(_blank(row[key]) for key in keys):
                exceptions.append(["主键为空，已排除匹配", label] + _provenance(row)
                                  + [row.get(header) for header in headers])
                continue
            index[tuple(_atom(row[key], ignore) for key in keys)].append(row)
        groups.append(index)
    old_groups, new_groups = groups
    ambiguous = {key for index in groups for key, records in index.items() if len(records) > 1}
    for label, index in (("旧表", old_groups), ("新表", new_groups)):
        for key in ambiguous:
            for row in index.get(key, []):
                reason = "本侧主键重复，已排除匹配" if len(index[key]) > 1 else "对侧主键重复，已排除匹配"
                exceptions.append([reason, label] + _provenance(row) + [row.get(header) for header in headers])
    old = {key: rows[0] for key, rows in old_groups.items() if key not in ambiguous}
    new = {key: rows[0] for key, rows in new_groups.items() if key not in ambiguous}
    added = [row for key, row in new.items() if key not in old]
    deleted = [row for key, row in old.items() if key not in new]
    fields_added = [header for header in right.headers if header not in left.headers]
    fields_deleted = [header for header in left.headers if header not in right.headers]
    field_events = [["新增字段", header, None, right.path.name] for header in fields_added]
    field_events += [["删除字段", header, left.path.name, None] for header in fields_deleted]
    modifications = []
    matched = 0
    for number, (key, old_row) in enumerate(old.items(), 1):
        if number % 128 == 0:
            ensure_cancel(cancel)
        if key not in new:
            continue
        matched += 1
        new_row = new[key]
        for header in headers:
            if header in keys:
                continue
            old_value, new_value = old_row.get(header), new_row.get(header)
            kind = "字段新增" if header in fields_added else "字段删除" if header in fields_deleted else "值修改"
            if kind != "值修改" or not _equal(old_value, new_value, ignore, tolerance):
                modifications.append([new_row[column] for column in keys] + [header, kind, old_value, new_value]
                                     + _provenance(old_row) + _provenance(new_row))
    metadata = _metadata_names(headers)
    new_rows = [headers + metadata] + [[row.get(header) for header in headers] + _provenance(row) for row in added]
    old_rows = [headers + metadata] + [[row.get(header) for header in headers] + _provenance(row) for row in deleted]
    modification_headers = ["主键_" + key for key in keys] + [
        "工具_字段", "工具_变化类型", "工具_原值", "工具_现值", "工具_旧来源文件", "工具_旧工作表",
        "工具_旧来源行", "工具_新来源文件", "工具_新工作表", "工具_新来源行",
    ]
    exception_headers = _metadata_names(headers, ("问题", "侧别", "来源文件", "来源工作表", "来源行")) + headers
    summary = [["指标", "数量/说明"], ["旧表记录", len(left.rows)], ["新表记录", len(right.rows)],
               ["成功匹配主键", matched], ["新增记录", len(added)], ["删除记录", len(deleted)],
               ["逐字段修改", len(modifications)], ["主键异常记录", len(exceptions)],
               ["新增字段数", len(fields_added)], ["删除字段数", len(fields_deleted)],
               ["主键列", "、".join(keys)], ["忽略首尾空白", ignore], ["数值容差", str(tolerance)],
               ["重复主键", "本侧及对侧对应记录均排除匹配，不取第一条，不列为新增或删除"],
               ["限制", _STANDARD_WARNING]]
    target = unique_path(folder, "表格对账.xlsx")
    _write_book(target, {"对账摘要": summary, "新增记录": new_rows, "删除记录": old_rows,
                        "字段修改": [modification_headers] + modifications,
                        "主键异常": [exception_headers] + exceptions,
                        "字段变化": [["变化类型", "字段", "旧文件", "新文件"]] + field_events},
                row_colors={"新增记录": "E2F0D9", "删除记录": "FCE4D6", "主键异常": "FFF2CC", "字段变化": "FFF2CC"},
                cell_colors={"字段修改": {len(keys) + 3: "FCE4D6", len(keys) + 4: "E2F0D9"}}, cancel=cancel)
    if exceptions:
        _warn(warnings, f"有 {len(exceptions)} 条记录因重复或空主键排除匹配，请查看“主键异常”页。")
    if fields_added or fields_deleted:
        _warn(warnings, "两表字段结构有变化，已同时在“字段变化”和逐字段修改中报告。")
    notify(progress, 3, 3, "表格对账完成")
    return _result([target], len(added) + len(deleted) + len(modifications) + len(exceptions) + len(field_events), warnings)


def run_excel(tool: str, sources: list[Path], folder: Path, options: dict,
              progress=None, cancel=None) -> dict:
    """Execute one spreadsheet tool; cancellation propagates as CancelledError.

    ``outputs`` includes all produced books. ``data_outputs`` includes only
    merge/split/clean primary-data books, allowing safe use in a following step.
    Per-file problems are collected in ``errors``; reconciliation validation is
    a whole-operation error. Successful earlier outputs are retained on cancel.
    """
    ensure_cancel(cancel)
    try:
        if tool not in {"excel_merge", "excel_clean", "excel_compare"}:
            raise ValueError("未知表格工具。")
        if not isinstance(options, dict):
            raise ValueError("处理选项必须为字典。")
        folder = Path(folder)
        files = _expand_sources(sources, folder, _bool(options, "recursive", True))
        if not files:
            raise ValueError("没有可读取的表格；输出目录会从批量输入文件夹中排除。")
        if tool == "excel_merge":
            return _merge(files, folder, options, progress, cancel)
        if tool == "excel_clean":
            return _clean(files, folder, options, progress, cancel)
        return _compare(files, folder, options, progress, cancel)
    except CancelledError:
        raise
    except Exception as exc:
        return _result([], 0, [_STANDARD_WARNING], [str(exc)])
