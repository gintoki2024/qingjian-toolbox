"""Ordinary table conversion without schema/header inference or type coercion.

``encoding`` and ``delimiter`` select the *output* representation. ``auto``
means UTF-8 BOM and the target extension's separator, respectively. Input is
detected independently; ``input_encoding``/``input_delimiter`` can override it.
Every CSV field becomes an explicit text cell in XLSX, including leading zeros
and strings beginning with ``=``. No formulas are calculated by this module.
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
from pathlib import Path

from . import conversion
from .common import ensure_cancel
from .text_ops import _ordinary_path, read_text_file

INPUTS = frozenset({".xls", ".xlsx", ".ods", ".csv", ".tsv"})
OUTPUTS = frozenset({".xlsx", ".csv", ".tsv"})
ENCODINGS = frozenset({"utf-8", "utf-8-sig", "utf-16", "gb18030"})
DELIMITERS = frozenset({",", "\t", ";"})


def supports(source_ext, target_ext):
    source_ext = "." + str(source_ext).lower().lstrip(".")
    target_ext = "." + str(target_ext).lower().lstrip(".")
    return source_ext in INPUTS and target_ext in OUTPUTS and (
        source_ext != target_ext or source_ext in {".csv", ".tsv"})


def _check(options):
    cancel = options.get("_cancel")
    ensure_cancel(cancel.is_set if hasattr(cancel, "is_set") else cancel)


def _encoding(options, *, input=False):
    value = options.get("input_encoding" if input else "encoding", "auto") or "auto"
    if value not in ENCODINGS | {"auto"}:
        raise ValueError("文本编码仅支持自动、UTF-8、UTF-8 BOM、UTF-16 或 GB18030。")
    return value if input or value != "auto" else "utf-8-sig"


def _delimiter(options, suffix, *, input=False, content=""):
    value = options.get("input_delimiter" if input else "delimiter", "auto") or "auto"
    if value == "\\t":
        value = "\t"
    if value not in DELIMITERS | {"auto"}:
        raise ValueError("分隔符仅支持自动、逗号、制表符或分号。")
    if value == "auto":
        if input and content:
            try:
                return csv.Sniffer().sniff(content[:65536], delimiters=",\t;").delimiter
            except csv.Error:
                pass
        return "\t" if suffix == ".tsv" else ","
    return value


def _text_rows(source, options, warnings):
    encoding = _encoding(options, input=True)
    if encoding == "auto":
        # Omit read_text_file's old "saved as UTF-8" warning: the caller can
        # deliberately select GB18030 as this operation's output encoding.
        content = read_text_file(source)
    else:
        if source.stat().st_size > 50 * 1024 * 1024:
            raise ValueError("文本表格超过 50 MB，请拆分后处理。")
        try:
            content = source.read_bytes().decode(encoding)
        except UnicodeError as exc:
            raise ValueError(f"无法按 {encoding.upper()} 读取文本表格；请调整输入编码。") from exc
        if "\x00" in content:
            raise ValueError("文本表格包含空字符，请确认输入编码。")
    separator = _delimiter(options, source.suffix.lower(), input=True, content=content)
    for row in csv.reader(io.StringIO(content, newline=""), delimiter=separator, strict=True):
        _check(options)
        yield row


def _write_xlsx(rows, target, options):
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    book = Workbook(write_only=True)
    sheet = book.create_sheet("数据")
    count = 0
    try:
        for count, row in enumerate(rows, 1):
            _check(options)
            if count > 1_048_576 or len(row) > 16384:
                raise ValueError("文本表格超过 Excel 行数或列数上限。")
            cells = []
            for value in row:
                if len(value) > 32767:
                    raise ValueError("单个单元格超过 Excel 的 32767 字符上限。")
                cell = WriteOnlyCell(sheet, value=value)
                cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
        book.save(target)
    finally:
        # Close the write-only streams even when a bad row/cancellation stops
        # input before save; no incomplete workbook is published.
        if not sheet.closed:
            sheet.close()
        book.close()
    return count


def _write_csv(rows, target, options):
    count = 0
    with target.open("x", encoding=_encoding(options), newline="") as stream:
        writer = csv.writer(stream, delimiter=_delimiter(options, target.suffix.lower()))
        for count, row in enumerate(rows, 1):
            _check(options)
            writer.writerow(row)
    return count


def _workbook_rows(sheet, formula_sheet, options, warnings):
    formula_rows = formula_sheet.iter_rows()
    for row in sheet.iter_rows():
        _check(options)
        original = next(formula_rows)
        values = []
        for cell, formula in zip(row, original):
            value = cell.value
            if formula.data_type == "f" and value is None:
                raise ValueError(
                    f"工作表“{sheet.title}”的 {formula.coordinate} 含有未缓存计算结果的公式。"
                    "请用 Excel 或 LibreOffice 重新计算并保存后再导出；未用公式文本替代数值。")
            if isinstance(value, str) and value.startswith(("=", "+", "@")):
                value = "'" + value
                warnings.append("可能被表格软件当作公式的文本已加前导单引号，防止打开 CSV/TSV 时执行。")
            values.append(value)
        yield values


def _sheet_filename(target, index, title):
    if index == 1:
        return target
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:60] or "工作表"
    return target.with_name(f"{target.stem}_{index:03d}_{title}{target.suffix}")


def _publish(staged, destination, options):
    _check(options)
    # Exclusive creation avoids replacing an existing result, including a file
    # created between validation and publication.
    created = False
    try:
        with destination.open("xb") as output, staged.open("rb") as source:
            created = True
            shutil.copyfileobj(source, output)
        _check(options)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return destination


def convert(source, target, options=None):
    """Convert a table to XLSX/CSV/TSV, returning all produced ``outputs``.

    A workbook with several worksheets requires ``sheet='name'`` or
    ``sheet_mode='all'`` for delimited output. The latter returns one file per
    worksheet and ``output_sheets`` aligned with ``outputs``. The first output
    uses ``target``; later names contain sequence number and worksheet name.
    """
    from .format_office import _source, _office_safety, _lo
    options = dict(options or {})
    _check(options)
    source, target = _source(source), Path(target).expanduser().absolute()
    _ordinary_path(target)
    if not supports(source.suffix, target.suffix):
        raise ValueError(f"不支持此表格转换方向：{source.suffix} → {target.suffix}")
    if os.path.lexists(target):
        raise FileExistsError("输出文件已存在，未覆盖：" + target.name)
    _encoding(options)
    _delimiter(options, target.suffix.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    warnings, staged_outputs, destinations, sheet_names = [], [], [], []
    count = 0
    with conversion._conversion_workspace(target.parent) as temporary:
        ext = target.suffix.lower()
        if source.suffix.lower() in {".csv", ".tsv"}:
            staged = temporary / ("result" + ext)
            rows = _text_rows(source, options, warnings)
            if ext == ".xlsx":
                count = _write_xlsx(rows, staged, options)
                warnings.append("CSV/TSV 按文本值导入，保留空表头、重复列名、长编号和前导零；公式样式字符串不会执行。")
            else:
                count = _write_csv(rows, staged, options)
            staged_outputs, destinations = [staged], [target]
        else:
            xlsx = source
            if source.suffix.lower() != ".xlsx":
                xlsx = temporary / "normalised.xlsx"
                _lo(source, xlsx, "sheet", options)
            _office_safety(xlsx)
            if ext == ".xlsx":
                staged = temporary / "result.xlsx"
                shutil.copyfile(xlsx, staged)
                staged_outputs, destinations = [staged], [target]
                count = 1
            else:
                from openpyxl import load_workbook
                book = load_workbook(xlsx, read_only=True, data_only=True, keep_links=False)
                formulas = load_workbook(xlsx, read_only=True, data_only=False, keep_links=False)
                try:
                    selected, mode = options.get("sheet"), options.get("sheet_mode", "selected")
                    if mode not in {"selected", "all", "single", "auto"}:
                        raise ValueError("工作表导出方式应为指定工作表或全部工作表。")
                    if selected:
                        if str(selected) not in book.sheetnames:
                            raise ValueError("未找到指定工作表：" + str(selected))
                        sheets = [book[str(selected)]]
                    elif mode == "all":
                        sheets = list(book.worksheets)
                    elif len(book.worksheets) > 1:
                        raise ValueError("工作簿含多个工作表，请选择一个工作表，或选择“全部工作表分别导出”。")
                    else:
                        sheets = list(book.worksheets)
                    if not sheets:
                        raise ValueError("工作簿没有可导出的工作表。")
                    for index, sheet in enumerate(sheets, 1):
                        destination = _sheet_filename(target, index, sheet.title)
                        if os.path.lexists(destination):
                            raise FileExistsError("输出文件已存在，未覆盖：" + destination.name)
                        staged = temporary / f"table-{index}{ext}"
                        count += _write_csv(_workbook_rows(sheet, formulas[sheet.title], options, warnings), staged, options)
                        staged_outputs.append(staged)
                        destinations.append(destination)
                        sheet_names.append(sheet.title)
                    warnings.append("CSV/TSV 只保存所选工作表的单元格值，不保留样式、图表和公式计算功能。")
                finally:
                    book.close()
                    formulas.close()
        _check(options)
        published = []
        try:
            for staged, destination in zip(staged_outputs, destinations):
                published.append(_publish(staged, destination, options))
        except BaseException:
            for path in published:
                path.unlink(missing_ok=True)
            raise
    return {"changes": count, "warnings": list(dict.fromkeys(warnings)),
            "outputs": destinations, "output_sheets": sheet_names}
