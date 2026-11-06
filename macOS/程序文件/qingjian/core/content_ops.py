"""Offline text search and evidence-preserving regular-expression extraction."""

from __future__ import annotations

import codecs
import contextlib
import csv
import io
import re
import zipfile
from pathlib import Path

from .common import (CancelledError, ensure_cancel, expand_inputs, make_result,
                     notify, unique_path, write_report_book)


EXTENSIONS = {".docx", ".pdf", ".xlsx", ".xlsm", ".csv", ".tsv", ".txt", ".md", ".html", ".htm", ".xhtml"}
_CELL_CHUNK = 30000


def _value(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _text_file(path: Path, notices: list) -> str:
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("单个文本/HTML 文件超过 50 MB，请缩小文件后再检索。")
    with path.open("rb") as stream:
        raw = stream.read(50 * 1024 * 1024 + 1)
    if len(raw) > 50 * 1024 * 1024:
        raise ValueError("文本文件超过 50 MB 上限。")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030", "cp1252"):
        try:
            result = raw.decode(encoding)
            if encoding != "utf-8-sig":
                notices.append(f"{path.name} 按 {encoding.upper()} 编码读取，请复核特殊字符。")
            return result
        except UnicodeError:
            continue
    raise ValueError("无法识别文本编码，请另存为 UTF-8 后重试。")


def _zip_check(path):
    try:
        with zipfile.ZipFile(path) as archive:
            items = archive.infolist()
            if len(items) > 30000 or sum(item.file_size for item in items) > 512 * 1024 * 1024:
                raise ValueError("文档展开后超过 512 MB 或内部文件过多，请缩小文档后重试。")
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效的 DOCX/XLSX 文档，可能已损坏或扩展名不正确。") from exc


def _docx_units(path, options, tables, notices):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    _zip_check(path)
    try:
        document = Document(path)
    except Exception as exc:
        raise ValueError("无法读取 Word 文档，请检查文件是否损坏或被加密。") from exc
    table_number = 0

    def walk(element, owner, prefix):
        nonlocal table_number
        paragraph_number = 0
        for child in element.iterchildren():
            if child.tag == qn("w:p"):
                paragraph_number += 1
                text = Paragraph(child, owner).text
                if text.strip():
                    yield f"{prefix} 第{paragraph_number}段", text
            elif child.tag == qn("w:tbl"):
                table_number += 1
                number = table_number
                table = Table(child, owner)
                rows = [[cell.text for cell in row.cells] for row in table.rows]
                location = f"{prefix} 表格{number}"
                if tables is not None:
                    tables.append((location, number, rows))
                for row_number, row in enumerate(rows, 1):
                    text = "\t".join(row)
                    if text.strip():
                        yield f"{location} 第{row_number}行", text
                # Nested table text is absent from python-docx's cell.text.
                visited = set()
                for row in table.rows:
                    for cell in row.cells:
                        if id(cell._tc) in visited:
                            continue
                        visited.add(id(cell._tc))
                        for nested in cell._tc.iterchildren():
                            if nested.tag == qn("w:tbl"):
                                # Traverse the original node without mutating it.
                                yield from nested_table(nested, cell, location)

    def nested_table(element, owner, prefix):
        nonlocal table_number
        table_number += 1
        number = table_number
        table = Table(element, owner)
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        location = f"{prefix} 内嵌表格{number}"
        if tables is not None:
            tables.append((location, number, rows))
        for index, row in enumerate(rows, 1):
            text = "\t".join(row)
            if text.strip():
                yield f"{location} 第{index}行", text

    yield from walk(document.element.body, document, "正文")
    parts = set()
    for section_number, section in enumerate(document.sections, 1):
        for name, header in (("页眉", section.header), ("首页页眉", section.first_page_header),
                             ("偶数页页眉", section.even_page_header), ("页脚", section.footer),
                             ("首页页脚", section.first_page_footer), ("偶数页页脚", section.even_page_footer)):
            # Accessing .part on an undefined inherited header would create one.
            # Inherited definitions have already been inspected in their section.
            if header.is_linked_to_previous:
                continue
            definition = header.part
            identity = str(definition.partname)
            if identity in parts:
                continue
            parts.add(identity)
            yield from walk(definition.element, header, f"第{section_number}节{name}")


def _pdf_units(path, options, tables, notices):
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect

    try:
        with pdfplumber.open(path, password=options.get("password") or None) as document:
            empty, scanned = [], []
            table_number = 0
            for page_number, page in enumerate(document.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    yield f"第{page_number}页", text
                elif page.images:
                    scanned.append(page_number)
                else:
                    empty.append(page_number)
                if tables is not None:
                    for rows in page.extract_tables():
                        table_number += 1
                        tables.append((f"第{page_number}页", table_number,
                                       [[_value(cell) for cell in row] for row in rows]))
                page.close()
            if scanned:
                pages = "、".join(map(str, scanned[:50])) + ("等" if len(scanned) > 50 else "")
                notices.append(f"{path.name} 第{pages}页疑似扫描页，已跳过文字识别；本工具不执行 OCR。")
            if empty:
                pages = "、".join(map(str, empty[:50])) + ("等" if len(empty) > 50 else "")
                notices.append(f"{path.name} 第{pages}页未提取到可检索文字。")
    except PDFPasswordIncorrect as exc:
        raise ValueError("PDF 已加密，请填写正确的打开密码；未读取正文。") from exc
    except (CancelledError, GeneratorExit):
        raise
    except Exception as exc:
        wrapped = [exc, *(arg for arg in exc.args if isinstance(arg, BaseException))]
        if any(isinstance(error, PDFPasswordIncorrect) or "password" in type(error).__name__.lower()
               or "encrypt" in type(error).__name__.lower() for error in wrapped):
            raise ValueError("PDF 已加密或加密方式不受支持，请提供正确密码或解密副本。") from exc
        raise ValueError(f"无法读取 PDF：{exc}") from exc


def _xlsx_units(path, options, tables, notices):
    from openpyxl import load_workbook

    _zip_check(path)
    workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        for sheet in workbook.worksheets:
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                values = [_value(value) for value in row]
                text = "\t".join(values)
                if text.strip():
                    yield f"工作表 [{sheet.title}] 第{row_number}行", text
    finally:
        workbook.close()


def _html_units(path, options, tables, notices):
    from bs4 import BeautifulSoup, Comment, NavigableString, Tag

    soup = BeautifulSoup(_text_file(path, notices), "html.parser")
    for element in list(soup.select("script,style,noscript,iframe,object,embed")):
        element.decompose()
    if soup.title and soup.title.get_text(strip=True):
        yield "网页标题", soup.title.get_text(" ", strip=True)
    root = soup.body or soup
    block_number, table_number = 0, 0

    def walk(node):
        nonlocal block_number, table_number
        pending = []

        def flush():
            nonlocal block_number
            text = " ".join(value for value in pending if value.strip()).strip()
            pending.clear()
            if text:
                block_number += 1
                return (f"文本块{block_number}", text)
            return None

        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                pending.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            block_names = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "li", "table", "div", "section",
                           "article", "main", "header", "footer", "nav", "ul", "ol"}
            if child.name not in block_names and child.name not in {"head", "img"} and not child.find(list(block_names)):
                pending.append("\n" if child.name == "br" else child.get_text(" ", strip=False))
                continue
            buffered = flush()
            if buffered:
                yield buffered
            if child.name == "table":
                table_number += 1
                for row_number, row in enumerate(child.find_all("tr"), 1):
                    if row.find_parent("table") is not child:
                        continue
                    text = "\t".join(cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False))
                    if text.strip():
                        yield f"表格{table_number} 第{row_number}行", text
            elif child.name in {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "li"}:
                if child.name == "li":
                    own = child.__copy__()
                    for nested in own.find_all(["ul", "ol"]):
                        nested.decompose()
                    text = own.get_text(" ", strip=True)
                else:
                    text = child.get_text(" ", strip=True)
                if text:
                    block_number += 1
                    yield f"文本块{block_number}（{child.name}）", text
                if child.name == "li":
                    for nested in child.find_all(["ul", "ol"], recursive=False):
                        yield from walk(nested)
            elif child.name not in {"head", "img", "br"}:
                yield from walk(child)
        buffered = flush()
        if buffered:
            yield buffered

    yield from walk(root)


def _iter_units(path: Path, options: dict, tables=None, notices=None):
    path = Path(path)
    notes = notices if notices is not None else []
    if path.is_symlink() or not path.is_file():
        raise ValueError("请选择实际存在的普通文件，不读取符号链接。")
    extension = path.suffix.lower()
    if extension == ".docx":
        yield from _docx_units(path, options, tables, notes)
    elif extension == ".pdf":
        yield from _pdf_units(path, options, tables, notes)
    elif extension in {".xlsx", ".xlsm"}:
        yield from _xlsx_units(path, options, tables, notes)
    elif extension in {".csv", ".tsv"}:
        reader = csv.reader(io.StringIO(_text_file(path, notes), newline=""), delimiter="\t" if extension == ".tsv" else ",")
        for record, row in enumerate(reader, 1):
            text = "\t".join(row)
            if text.strip():
                yield f"第{record}条记录（原文至第{reader.line_num}行）", text
    elif extension in {".html", ".htm", ".xhtml"}:
        yield from _html_units(path, options, tables, notes)
    elif extension in {".txt", ".md"}:
        for line_number, line in enumerate(_text_file(path, notes).splitlines(), 1):
            if line.strip():
                yield f"第{line_number}行", line
    else:
        raise ValueError(f"不支持检索该文件格式：{extension or '无扩展名'}")


def iter_text_units(path: Path, options: dict):
    """Yield (human-readable source position, original extracted text)."""
    yield from _iter_units(Path(path), options)


def _context_rows(prefix: list, text: str, position_index: int):
    chunks = [text[index:index + _CELL_CHUNK] for index in range(0, len(text), _CELL_CHUNK)] or [""]
    for index, chunk in enumerate(chunks, 1):
        row = list(prefix)
        if len(chunks) > 1:
            row[position_index] += f"（原文片段{index}/{len(chunks)}）"
        yield [*row, chunk]


def _search(sources, folder, options, progress, cancel):
    query = options.get("query", "")
    if not isinstance(query, str):
        raise ValueError("检索关键词必须为文本。")
    queries = list(dict.fromkeys(line.strip() for line in query.splitlines() if line.strip()))
    if not queries:
        raise ValueError("请至少填写一个检索关键词，每行一个。")
    match_mode = options.get("match_mode", "any")
    if match_mode not in {"any", "all"}:
        raise ValueError("匹配方式必须为 any 或 all。")
    sensitive = options.get("case_sensitive", False)
    if not isinstance(sensitive, bool):
        raise ValueError("区分大小写应为布尔值。")
    needles = queries if sensitive else [query.casefold() for query in queries]
    files = expand_inputs(sources, EXTENSIONS, bool(options.get("recursive", True)), exclude_root=folder)
    rows = [["来源文件", "来源位置", "命中关键词", "上下文原文"]]
    scope = [["来源文件", "检查结果", "命中单元数"]]
    warnings, errors, count, reached = [], [], 0, False
    for index, path in enumerate(files, 1):
        ensure_cancel(cancel)
        hits = 0
        try:
            with contextlib.closing(_iter_units(path, options, notices=warnings)) as units:
                for position, text in units:
                    ensure_cancel(cancel)
                    haystack = text if sensitive else text.casefold()
                    matched = [original for original, needle in zip(queries, needles) if needle in haystack]
                    if not matched or (match_mode == "all" and len(matched) != len(queries)):
                        continue
                    count += 1
                    hits += 1
                    rows.extend(_context_rows([str(path), position, "\n".join(matched)], text, 1))
                    if count >= 5000:
                        reached = True
                        break
            scope.append([str(path), "已达命中上限，后续未继续检索" if reached else "已检索", hits])
        except CancelledError:
            raise
        except Exception as exc:
            errors.append(f"{path}：{exc}")
            scope.append([str(path), f"读取失败：{exc}", hits])
        notify(progress, index, len(files), f"检索：{path.name}")
        if reached:
            for rest in files[index:]:
                scope.append([str(rest), "未检索：已达 5000 命中上限", ""])
            warnings.append("已达到 5000 个命中单元上限，后续内容和文件未继续检索；请缩小范围或细化关键词。")
            break
    if not files:
        warnings.append("所选范围中没有支持检索的文件。")
    report = unique_path(folder, "全文检索结果.xlsx")
    write_report_book(report, {"命中结果": rows, "检查范围": scope,
                             "检索条件": [["条件", "值"], ["关键词（逐行）", "\n".join(queries)],
                                      ["匹配方式", "同一段落/表格行/PDF页中全部命中" if match_mode == "all" else "任一关键词命中"],
                                      ["区分大小写", sensitive], ["递归子目录", bool(options.get("recursive", True))]],
                             "读取提示": [["类型", "说明"], *[["提示", value] for value in warnings], *[["异常", value] for value in errors]]})
    return make_result([report], count, warnings, errors)


def _compile_fields(options):
    fields = options.get("fields", [])
    if not isinstance(fields, list):
        raise ValueError("字段规则应为列表。")
    if not fields and not options.get("include_tables", True):
        raise ValueError("请至少添加一个字段名和正则表达式，或勾选识别表格。")
    compiled, names = [], set()
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str) or not isinstance(field.get("pattern"), str):
            raise ValueError("字段需要 name 和 pattern 两项文本。")
        name, pattern = field["name"].strip(), field["pattern"]
        if not name or name in names or not pattern:
            raise ValueError("字段名不可为空或重复，正则表达式不可为空。")
        try:
            expression = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"字段“{name}”的正则表达式无效：{exc}") from exc
        if expression.search("") is not None:
            raise ValueError(f"字段“{name}”的正则可匹配空文本，请改为明确的非空规则。")
        compiled.append((name, expression))
        names.add(name)
    return compiled


def _extract(sources, folder, options, progress, cancel):
    fields = _compile_fields(options)
    first_only = options.get("per_file_first", True)
    include_tables = options.get("include_tables", True)
    if not isinstance(first_only, bool) or not isinstance(include_tables, bool):
        raise ValueError("首个匹配和识别表格选项应为布尔值。")
    files = expand_inputs(sources, EXTENSIONS, bool(options.get("recursive", True)), exclude_root=folder)
    rows = [["来源文件", "字段名", "提取值", "来源位置", "来源原文"]]
    overview = [["来源文件", "提取数量", "未命中字段", "检查结果"]]
    table_rows = [["来源文件", "来源位置", "表格编号", "原表行", "原表列", "单元格原文"]]
    warnings, errors, changes = [], [], 0
    value_limit = 50000
    table_limit = 200000
    stopped = False
    for index, path in enumerate(files, 1):
        ensure_cancel(cancel)
        found, tables, file_hits = set(), ([] if include_tables else None), 0
        try:
            with contextlib.closing(_iter_units(path, options, tables=tables, notices=warnings)) as units:
                for position, text in units:
                    ensure_cancel(cancel)
                    for name, expression in fields:
                        if first_only and name in found:
                            continue
                        for match in expression.finditer(text):
                            value = match.group(1) if expression.groups else match.group(0)
                            if value is None:
                                continue
                            found.add(name)
                            changes += 1
                            file_hits += 1
                            rows.extend(_context_rows([str(path), name, value, position], text, 3))
                            if changes >= value_limit:
                                stopped = True
                                break
                            if first_only:
                                break
                        if stopped:
                            break
                    if stopped or (first_only and len(found) == len(fields) and not include_tables):
                        break
            missing = [name for name, _ in fields if name not in found]
            for name in missing:
                rows.append([str(path), name, "", "", ""])
            if missing:
                warnings.append(f"{path.name}：字段未命中，结果留空：{'、'.join(missing)}")
            overview.append([str(path), file_hits, "、".join(missing), "达到提取上限，未完全检查" if stopped else "已检查"])
            if tables is not None:
                for position, table_number, matrix in tables:
                    for row_number, row in enumerate(matrix, 1):
                        ensure_cancel(cancel)
                        for column_number, value in enumerate(row, 1):
                            if len(table_rows) - 1 >= table_limit:
                                break
                            for text_row in _context_rows([str(path), position, table_number, row_number, column_number], _value(value), 1):
                                table_rows.append(text_row)
                    if len(table_rows) - 1 >= table_limit:
                        break
        except CancelledError:
            raise
        except Exception as exc:
            errors.append(f"{path}：{exc}")
            overview.append([str(path), file_hits, "", f"读取失败：{exc}"])
        notify(progress, index, len(files), f"提取：{path.name}")
        if stopped:
            warnings.append("达到 50000 条提取值上限，后续内容未继续提取，请缩小范围。")
            for rest in files[index:]:
                overview.append([str(rest), "", "", "未检查：达到提取上限"])
            break
    if len(table_rows) - 1 >= table_limit:
        warnings.append("识别表格达到 200000 个输出单元格上限，超出部分未导出，请缩小范围。")
    if not fields:
        changes = len(table_rows) - 1
        if not changes:
            warnings.append("未在所选 DOCX/PDF 中识别到可导出的表格；不对扫描图像执行 OCR。")
    if not files:
        warnings.append("所选范围中没有支持提取的文件。")
    sheets = {"字段提取": rows, "文件检查范围": overview,
              "字段规则": [["字段名", "正则表达式", "每文件仅保留首个匹配"],
                       *[[name, expression.pattern, first_only] for name, expression in fields]]}
    if include_tables:
        sheets["识别表格"] = table_rows
    sheets["读取提示"] = [["类型", "说明"], *[["提示", value] for value in warnings], *[["异常", value] for value in errors]]
    report = unique_path(folder, "字段与表格提取.xlsx")
    write_report_book(report, sheets)
    return make_result([report], changes, warnings, errors)


def run_content(tool: str, sources: list[Path], folder: Path, options: dict, progress=None, cancel=None) -> dict:
    ensure_cancel(cancel)
    for candidate in (Path(folder).absolute(), *Path(folder).absolute().parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise ValueError("结果目录不能位于符号链接或目录联接内。")
    if tool == "content_search":
        return _search(sources, Path(folder), options, progress, cancel)
    if tool == "extract_fields":
        return _extract(sources, Path(folder), options, progress, cancel)
    raise ValueError(f"不支持的内容工具：{tool}")
