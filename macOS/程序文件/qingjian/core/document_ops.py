"""Offline template generation, PDF page tools and image packaging.

Templates contain literal ``{{列名}}`` fields only.  No Jinja interpreter,
Python expressions, attribute traversal or network services are used here.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import shutil
import string
import tempfile
import warnings as python_warnings
import zipfile
from dataclasses import dataclass
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from .common import (
    CancelledError, atomic_write, ensure_cancel, expand_inputs, make_result,
    notify, safe_name, unique_path, write_report_book,
)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_T = "{" + _W + "}t"
_P = "{" + _W + "}p"
_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_BOUNDARY = "\ufffc"
_BLOCKS = {"{" + _W + "}" + name for name in (
    "drawing", "pict", "object", "tab", "br", "cr", "fldChar", "instrText",
)}
_FIELDS = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_SIMPLE_NAME = re.compile(r"^[\w\u3000-\u303f\uff00-\uffef ]+$", re.UNICODE)
_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _integer(value, name, minimum=1, maximum=1_000_000):
    if isinstance(value, bool):
        raise ValueError(f"{name}必须为整数。")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name}必须为整数。") from exc
    if not math.isfinite(number) or number != int(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name}必须为 {minimum} 至 {maximum} 的整数。")
    return int(number)


def _boolean(options, key, default=False):
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须为布尔值。")
    return value


def _text(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _record(records, source, location, status, output="", detail=""):
    records.append({"来源": str(source), "原始行或页": location, "状态": status,
                    "输出": str(output), "说明": str(detail)})


def _report(tool, folder, outputs, changes, warnings, errors, records):
    label = {"template_fill": "模板填充", "pdf_pages": "PDF页面整理", "image_pack": "图片压缩合成"}[tool]
    report = unique_path(folder, label + "_处理报告.xlsx")
    rows = [["来源", "原始行或页", "状态", "输出", "说明"]]
    rows.extend([record.get(key, "") for key in rows[0]] for record in records)
    summary = [["项目", "内容"], ["功能", label], ["实际处理数量", changes]]
    summary.extend(["警告", message] for message in warnings)
    summary.extend(["错误", message] for message in errors)
    try:
        write_report_book(report, {"处理明细": rows, "汇总": summary})
        outputs.append(str(report))
    except Exception as exc:
        errors.append(f"无法写入 Excel 处理报告：{exc}")
    report_json = unique_path(folder, label + "_处理报告.json")
    payload = {"tool": tool, "changes": changes, "outputs": list(outputs),
               "warnings": list(warnings), "errors": list(errors), "records": records}
    try:
        atomic_write(report_json, lambda path: path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"))
        outputs.append(str(report_json))
    except Exception as exc:
        errors.append(f"无法写入 JSON 处理报告：{exc}")


@dataclass
class _DataRow:
    values: dict
    number: int
    error: str = ""


def _headers(values):
    names = [_text(value).strip() for value in values]
    while names and not names[-1]:
        names.pop()
    if not names or not any(names):
        raise ValueError("表头为空，请检查表头行号。")
    names = [name or f"列{index + 1}" for index, name in enumerate(names)]
    duplicate = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicate:
        raise ValueError("表头存在重名列，请先重命名：" + "、".join(duplicate))
    return names


def _csv_rows(source, header_row, options):
    encoding = options.get("encoding")
    if encoding is None:
        for candidate in ("utf-8-sig", "gb18030"):
            try:
                with source.open("r", encoding=candidate, newline="") as stream:
                    for _ in stream:
                        pass
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        if encoding is None:
            raise ValueError("无法识别文本编码，请将数据文件另存为 UTF-8。")
    rows = []
    with source.open("r", encoding=encoding, newline="") as stream:
        reader = csv.reader(stream, delimiter="\t" if source.suffix.lower() == ".tsv" else ",")
        header = None
        for number, values in enumerate(reader, 1):
            if number < header_row:
                continue
            if number == header_row:
                header = _headers(values)
                continue
            if not any(value.strip() for value in values):
                continue
            error = "此行的数据列多于表头列，请检查分隔符或表头。" if len(values) > len(header) and any(values[len(header):]) else ""
            rows.append(_DataRow({name: values[index] if index < len(values) else "" for index, name in enumerate(header)}, number, error))
            if len(rows) > 50000:
                raise ValueError("单个数据文件最多处理 50000 条模板记录，请拆分文件。")
    if header is None:
        raise ValueError("指定的表头行不存在。")
    return header, rows, []


def _xlsx_rows(source, header_row, options):
    from openpyxl import load_workbook

    cached = load_workbook(source, read_only=True, data_only=True)
    formulas = None
    try:
        formulas = load_workbook(source, read_only=True, data_only=False)
        requested = options.get("sheet")
        if requested in (None, ""):
            title = cached.sheetnames[0]
        elif isinstance(requested, int) and not isinstance(requested, bool):
            if not 1 <= requested <= len(cached.sheetnames):
                raise ValueError("工作表序号超出范围。")
            title = cached.sheetnames[requested - 1]
        elif isinstance(requested, str) and requested in cached.sheetnames:
            title = requested
        else:
            raise ValueError(f"找不到工作表：{requested}")
        sheet, raw_sheet = cached[title], formulas[title]
        cached_rows = sheet.iter_rows(min_row=header_row)
        raw_rows = raw_sheet.iter_rows(min_row=header_row)
        head, raw_head = next(cached_rows, None), next(raw_rows, None)
        if head is None or raw_head is None:
            raise ValueError("指定的表头行不存在。")
        if any(raw.data_type == "f" and cell.value is None for cell, raw in zip(head, raw_head)):
            raise ValueError("表头公式没有已保存的计算结果，请在 Excel 中计算并保存后重试。")
        header = _headers([cell.value for cell in head])
        rows, notices = [], []
        for number, (cells, raw_cells) in enumerate(zip(cached_rows, raw_rows), header_row + 1):
            if not any(cell.value is not None for cell in raw_cells):
                continue
            values, missing = {}, []
            for index, name in enumerate(header):
                cell = cells[index] if index < len(cells) else None
                raw = raw_cells[index] if index < len(raw_cells) else None
                value = cell.value if cell is not None else None
                if raw is not None and raw.data_type == "f" and value is None:
                    missing.append(name)
                if cell is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
                    if re.fullmatch(r"0{2,}", cell.number_format or "") and float(value).is_integer():
                        value = str(int(value)).zfill(len(cell.number_format))
                    elif abs(value) >= 10**15:
                        notices.append(f"{source.name} 第 {number} 行“{name}”为长数字；Excel 数值可能已损失精度，长编号请使用文本单元格。")
                values[name] = value
            extra = [raw.value for raw in raw_cells[len(header):]]
            error = ""
            if missing:
                error = "公式缺少已保存的计算结果：" + "、".join(missing) + "；请在 Excel 中计算并保存后重试。"
            elif any(value is not None for value in extra):
                error = "此行的数据列多于表头列。"
            rows.append(_DataRow(values, number, error))
            if len(rows) > 50000:
                raise ValueError("单个数据文件最多处理 50000 条模板记录，请拆分文件。")
        return header, rows, notices
    finally:
        cached.close()
        if formulas is not None:
            formulas.close()


def _load_rows(source, options):
    header_row = _integer(options.get("header_row", 1), "表头行号", 1, 1048576)
    result = (_xlsx_rows if source.suffix.lower() in {".xlsx", ".xlsm"} else _csv_rows)(source, header_row, options)
    if source.suffix.lower() == ".xlsm":
        result[2].append(f"{source.name} 仅按已保存的数据读取；不执行、复制或保留 XLSM 宏。")
    return result


def _paragraph_stream(paragraph):
    pieces, spans, position = [], [], 0

    def walk(node):
        nonlocal position
        if node is not paragraph and node.tag == _P:
            return
        if node.tag in _BLOCKS:
            pieces.append(_BOUNDARY)
            position += 1
            return
        if node.tag == _T:
            value = node.text or ""
            pieces.append(value)
            spans.append((node, position, position + len(value)))
            position += len(value)
            return
        for child in node:
            walk(child)

    walk(paragraph)
    return "".join(pieces), spans


def _field_name(name):
    name = name.strip()
    if not name or not _SIMPLE_NAME.fullmatch(name):
        raise ValueError(f"不支持的模板字段“{name}”：只允许列名，不允许 Jinja 代码、属性访问或表达式。")
    return name


def _template_roots(template):
    from lxml import etree

    roots, fields = {}, set()
    with zipfile.ZipFile(template) as archive:
        if len(archive.infolist()) > 10000 or sum(entry.file_size for entry in archive.infolist()) > 512 * 1024 * 1024:
            raise ValueError("模板文件过大，或包含过多内部文件。")
        if "word/document.xml" not in archive.namelist():
            raise ValueError("模板不是有效的 DOCX 文件。")
        for name in archive.namelist():
            if not re.fullmatch(r"word/(?:document|header\d*|footer\d*|footnotes|endnotes)\.xml", name):
                continue
            data = archive.read(name)
            if len(data) > 64 * 1024 * 1024 or b"<!DOCTYPE" in data.upper():
                raise ValueError("模板 XML 过大或包含不允许的 DTD。")
            root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
            roots[name] = root
            for paragraph in root.iter(_P):
                text, _ = _paragraph_stream(paragraph)
                if any(marker in text for marker in ("{%", "%}", "{#", "#}")):
                    raise ValueError("模板只支持 {{列名}}，不支持 Jinja 代码、循环、条件或注释。")
                for match in _FIELDS.finditer(text):
                    fields.add(_field_name(match.group(1)))
                residue = _FIELDS.sub("", text)
                if "{{" in residue or "}}" in residue:
                    raise ValueError("模板占位符没有正确闭合，请使用 {{列名}}。")
    if not fields:
        raise ValueError("模板中未找到 {{列名}} 占位符。")
    return roots, fields


def _replace_template(template, target, values):
    from copy import deepcopy
    from lxml import etree

    roots, _ = _template_roots(template)
    changes = 0
    for name, original in list(roots.items()):
        root = deepcopy(original)
        for paragraph in root.iter(_P):
            text, spans = _paragraph_stream(paragraph)
            for match in reversed(list(_FIELDS.finditer(text))):
                replacement = _text(values.get(_field_name(match.group(1)), ""))
                if any(ord(char) < 32 and char not in "\t\n\r" for char in replacement):
                    raise ValueError("数据中包含 Word 不支持的控制字符。")
                affected = [(node, start, end) for node, start, end in spans if end > match.start() and start < match.end()]
                if not affected:
                    continue
                for index, (node, start, end) in enumerate(affected):
                    left = max(0, match.start() - start)
                    right = min(len(node.text or ""), match.end() - start)
                    node.text = (node.text or "")[:left] + (replacement if index == 0 else "") + (node.text or "")[right:]
                    node.set(_SPACE, "preserve")
                changes += 1
            # Word renders w:br/w:tab, rather than literal newline/tab bytes in
            # w:t.  Split only after every placeholder edit so offsets stay valid.
            for node in list(paragraph.iter(_T)):
                value = node.text or ""
                if not any(character in value for character in "\r\n\t"):
                    continue
                parts = re.split(r"(\r\n|\r|\n|\t)", value)
                node.text = parts[0]
                previous = node
                for part in parts[1:]:
                    element = etree.Element("{" + _W + "}" + ("tab" if part == "\t" else "br")) if part in {"\r\n", "\r", "\n", "\t"} else etree.Element(_T)
                    if element.tag == _T:
                        element.text = part
                        element.set(_SPACE, "preserve")
                    previous.addnext(element)
                    previous = element
        roots[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(template) as source, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
        for entry in source.infolist():
            if entry.filename in roots:
                output.writestr(entry, roots[entry.filename])
            else:
                with source.open(entry) as incoming, output.open(entry, "w") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
    return changes


def _filename(pattern, values, missing_policy):
    parts = []
    for literal, field, specification, conversion in string.Formatter().parse(pattern):
        parts.append(literal)
        if field is None:
            continue
        name = _field_name(field)
        if conversion or "{" in specification or "}" in specification or len(specification) > 30:
            raise ValueError("文件名模板不支持代码、属性访问、转换或嵌套表达式。")
        if any(int(number) > 1000 for number in re.findall(r"\d+", specification)):
            raise ValueError("文件名格式宽度过大。")
        value = values.get(name)
        if value is None or value == "":
            if missing_policy == "error":
                raise ValueError(f"文件名字段“{name}”为空或不存在。")
            value = ""
            specification = ""
        try:
            parts.append(format(value, specification))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"文件名字段“{name}”与格式“{specification}”不兼容。") from exc
    name = safe_name("".join(parts))[:100]
    if Path(name).suffix.lower() in {".docx", ".pptx", ".pdf"}:
        name = Path(name).stem
    return name or "未命名"


def _run_template(sources, folder, options, outputs, data_outputs, warnings, errors, records, progress, cancel):
    template = Path(str(options.get("template_path", ""))).expanduser()
    suffix = template.suffix.lower()
    if not template.is_file() or suffix not in {".docx", ".pptx"}:
        raise ValueError("请选择有效的 DOCX 或 PPTX 模板；旧版 .ppt 请先另存为 .pptx。")
    if suffix == ".pptx":
        from .ppt_template import template_fields
        fields = template_fields(template)
        warnings.append("PPTX 填充普通文本框、表格、分组内文字，以及母版、版式和备注中的文字；图片内文字和嵌入文件内容不处理。图表、SmartArt、动态字段中检测到占位符会报错，请改用普通文本框。较长内容请检查是否超出原文本框。")
    else:
        _, fields = _template_roots(template)
    policy = options.get("missing_policy", "error")
    if policy not in {"error", "blank"}:
        raise ValueError("缺失字段策略必须为 error 或 blank。")
    pdf = _boolean(options, "output_pdf")
    if pdf and suffix == ".pptx":
        raise ValueError("同时导出 PDF 目前仅支持 Word 模板；PPTX 模板请关闭此项。")
    pattern = options.get("filename_template", "{序号:03d}_{姓名}")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("文件名模板不能为空。")
    files = expand_inputs(sources, {".xlsx", ".xlsm", ".csv", ".tsv"}, recursive=_boolean(options, "recursive", True), exclude_root=folder)
    if not files:
        raise ValueError("没有找到 XLSX、XLSM、CSV 或 TSV 数据文件。")
    sequence, changes = 0, 0
    for file_index, source in enumerate(files):
        ensure_cancel(cancel)
        try:
            _, rows, notices = _load_rows(source, options)
            warnings.extend(notices)
        except Exception as exc:
            errors.append(f"{source.name}：{exc}")
            _record(records, source, "", "失败", detail=exc)
            continue
        if not rows:
            warnings.append(f"{source.name} 没有可填充的数据行。")
        for row in rows:
            ensure_cancel(cancel)
            sequence += 1
            published = []
            try:
                if row.error:
                    raise ValueError(row.error)
                values = dict(row.values)
                values.setdefault("序号", sequence)
                missing = sorted(field for field in fields if values.get(field) is None or values.get(field) == "")
                if missing and policy == "error":
                    raise ValueError("模板字段为空或不存在：" + "、".join(missing))
                if missing:
                    warnings.append(f"{source.name} 第 {row.number} 行缺少“{'、'.join(missing)}”，已按空白填充。")
                basename = _filename(pattern, values, policy)
                with tempfile.TemporaryDirectory(prefix="qingjian_fill_", dir=folder) as temporary:
                    temporary = Path(temporary)
                    filled = temporary / ("filled" + suffix)
                    if suffix == ".pptx":
                        from .ppt_template import fill_template
                        fill_template(template, filled, {key: _text(value) for key, value in values.items()})
                    else:
                        _replace_template(template, filled, values)
                    deliverables = [(filled, unique_path(folder, basename + suffix))]
                    if pdf:
                        ensure_cancel(cancel)
                        from .conversion import convert_word
                        converted = temporary / "filled.pdf"
                        result = convert_word(filled, converted, options)
                        warnings.extend(result.get("warnings", []))
                        deliverables.append((converted, unique_path(folder, basename + ".pdf")))
                    for staged, target in deliverables:
                        atomic_write(target, lambda destination, staged=staged: shutil.copyfile(staged, destination))
                        published.append(target)
                outputs.extend(str(path) for path in published)
                data_outputs.extend(str(path) for path in published)
                changes += 1
                _record(records, source, row.number, "成功", "；".join(str(path) for path in published))
            except CancelledError:
                raise
            except Exception as exc:
                for path in published:
                    path.unlink()  # Only files created by this row are rolled back.
                errors.append(f"{source.name} 第 {row.number} 行：{exc}")
                _record(records, source, row.number, "失败", detail=exc)
            notify(progress, file_index, len(files), f"{source.name}：已处理数据行 {row.number}")
        notify(progress, file_index + 1, len(files), source.name)
    return changes


def _page_numbers(specification, total):
    if not isinstance(specification, str) or not specification.strip():
        raise ValueError("页码范围不能为空，例如 1-3,5,8-6。")
    pages = []
    for token in specification.replace("，", ",").split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", token)
        if not match:
            raise ValueError(f"页码范围无效：{token or '空范围'}")
        start, end = int(match.group(1)), int(match.group(2) or match.group(1))
        if min(start, end) < 1 or max(start, end) > total:
            raise ValueError(f"页码 {token.strip()} 超出范围，此文件共有 {total} 页，页码从 1 开始。")
        pages.extend(range(start - 1, end - 1 + (1 if end >= start else -1), 1 if end >= start else -1))
        if len(pages) > 1_000_000:
            raise ValueError("一次最多输出 100 万页，请缩小页码范围。")
    return pages


def _reader(stream, password):
    from pypdf import PdfReader
    reader = PdfReader(stream, strict=False)
    if reader.is_encrypted and not reader.decrypt(password or ""):
        raise ValueError("PDF 密码错误或文件需要打开密码。")
    if not len(reader.pages):
        raise ValueError("PDF 没有可处理的页面。")
    return reader


def _write_pdf(writer, path):
    with path.open("wb") as stream:
        writer.write(stream)


def _pdf_filename(value):
    name = safe_name(value)[:100]
    return (Path(name).stem if Path(name).suffix.lower() == ".pdf" else name) + ".pdf"


def _run_pdf(sources, folder, options, outputs, data_outputs, warnings, errors, records, progress, cancel):
    from pypdf import PdfWriter

    action = options.get("action", "merge")
    if action not in {"merge", "extract", "split", "reorder"}:
        raise ValueError("PDF 页面操作必须为 merge、extract、split 或 reorder。")
    password = options.get("password", "")
    if not isinstance(password, str):
        raise ValueError("PDF 密码必须为文字。")
    every = _integer(options.get("split_every", 1), "每份页数")
    files = expand_inputs(sources, {".pdf"}, recursive=_boolean(options, "recursive", True), exclude_root=folder)
    if not files:
        raise ValueError("没有找到 PDF 文件。")
    changes = 0
    if action == "merge":
        failed = False
        with PdfWriter() as writer:
            for index, source in enumerate(files):
                ensure_cancel(cancel)
                try:
                    with source.open("rb") as stream:
                        reader = _reader(stream, password)
                        count = len(reader.pages)
                        writer.append(reader, import_outline=True)
                    _record(records, source, count, "已读取")
                except Exception as exc:
                    errors.append(f"{source.name}：{exc}")
                    _record(records, source, "", "失败", detail=exc)
                    failed = True
                notify(progress, index + 1, len(files), source.name)
            if failed:
                warnings.append("合并已停止：至少一个输入文件读取失败，没有输出缺页的合并文件。")
                return 0
            ensure_cancel(cancel)
            target = unique_path(folder, _pdf_filename(options.get("output_name", "合并文档")))
            atomic_write(target, lambda path: _write_pdf(writer, path))
            changes = len(writer.pages)
        outputs.append(str(target)); data_outputs.append(str(target))
        _record(records, "全部输入", changes, "合并成功", target)
        return changes
    for index, source in enumerate(files):
        ensure_cancel(cancel)
        try:
            with source.open("rb") as stream:
                reader = _reader(stream, password)
                count = len(reader.pages)
                if action == "split":
                    groups = [list(range(start, min(start + every, count))) for start in range(0, count, every)]
                else:
                    groups = [_page_numbers(options.get("pages", ""), count)]
                for pages in groups:
                    ensure_cancel(cancel)
                    with PdfWriter() as writer:
                        for page in pages:
                            ensure_cancel(cancel)
                            writer.add_page(reader.pages[page])
                        suffix = f"第{pages[0] + 1:03d}-{pages[-1] + 1:03d}页" if action == "split" else ("抽页" if action == "extract" else "重排")
                        target = unique_path(folder, source.stem + "_" + suffix + ".pdf")
                        atomic_write(target, lambda path: _write_pdf(writer, path))
                    outputs.append(str(target)); data_outputs.append(str(target))
                    changes += len(pages)
                    _record(records, source, ",".join(str(page + 1) for page in pages), "成功", target)
        except CancelledError:
            raise
        except Exception as exc:
            errors.append(f"{source.name}：{exc}")
            _record(records, source, "", "失败", detail=exc)
        notify(progress, index + 1, len(files), source.name)
    return changes


def _open_image(source, width, height, warnings):
    from PIL import Image, ImageOps

    with python_warnings.catch_warnings():
        python_warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source) as original:
            if original.width * original.height > 80_000_000:
                raise ValueError("图片超过 8000 万像素，请先缩小后处理。")
            original.seek(0)
            original_format = original.format
            if getattr(original, "n_frames", 1) > 1:
                warnings.append(f"{source.name} 包含多帧或多页，本次只读取第一帧。")
            transposed = ImageOps.exif_transpose(original)
            try:
                transposed.thumbnail((width, height), Image.Resampling.LANCZOS)
                converted = transposed.convert("RGBA" if "A" in transposed.getbands() or "transparency" in transposed.info else "RGB")
                try:
                    clean = Image.new(converted.mode, converted.size)
                    clean.paste(converted)
                finally:
                    converted.close()
            finally:
                transposed.close()
    return clean, original_format


def _flatten(image):
    from PIL import Image
    if "A" not in image.getbands():
        return image.convert("RGB")
    result = Image.new("RGB", image.size, "white")
    result.paste(image, mask=image.getchannel("A"))
    return result


def _encode_image(image, output_format, quality):
    stream = io.BytesIO()
    settings = {}
    actual = image
    if output_format == "JPEG":
        actual = _flatten(image)
        settings = {"quality": quality, "optimize": True, "progressive": True, "exif": b""}
    elif output_format == "WEBP":
        settings = {"quality": quality, "method": 6, "exif": b""}
    elif output_format == "PNG":
        settings = {"optimize": True, "compress_level": 9}
    elif output_format == "TIFF":
        settings = {"compression": "tiff_deflate"}
    try:
        actual.save(stream, format=output_format, **settings)
        return stream.getvalue()
    finally:
        if actual is not image:
            actual.close()


def _compress(image, output_format, quality, target_kb, cancel):
    from PIL import Image

    current = image.copy()
    target = target_kb * 1024
    best = b""
    try:
        for _ in range(12):
            ensure_cancel(cancel)
            best = _encode_image(current, output_format, quality)
            if not target or len(best) <= target:
                return best, current.size
            if output_format in {"JPEG", "WEBP"}:
                minimum = min(5, quality)
                smallest = _encode_image(current, output_format, minimum)
                if len(smallest) <= target:
                    best = smallest
                    low, high = minimum + 1, quality - 1
                    while low <= high:
                        ensure_cancel(cancel)
                        middle = (low + high) // 2
                        candidate = _encode_image(current, output_format, middle)
                        if len(candidate) <= target:
                            best, low = candidate, middle + 1
                        else:
                            high = middle - 1
                    return best, current.size
                best = smallest
            if current.size == (1, 1):
                break
            ratio = min(0.85, max(0.15, math.sqrt(target / len(best)) * 0.9))
            size = (max(1, int(current.width * ratio)), max(1, int(current.height * ratio)))
            if size == current.size:
                size = (max(1, current.width - 1), max(1, current.height - 1))
            resized = current.resize(size, Image.Resampling.LANCZOS)
            current.close()
            current = resized
        raise ValueError(f"未达到 {target_kb} KB 目标（最小尝试结果 {len(best) / 1024:.1f} KB），未输出文件；请调整目标或输出格式。")
    finally:
        current.close()


def _run_images(sources, folder, options, outputs, data_outputs, warnings, errors, records, progress, cancel):
    action = options.get("action", "compress")
    if action not in {"compress", "pdf"}:
        raise ValueError("图片操作必须为 compress 或 pdf。")
    chosen = options.get("output_format", "original")
    if chosen not in {"jpg", "png", "webp", "original"}:
        raise ValueError("图片格式必须为 jpg、png、webp 或 original。")
    width = _integer(options.get("max_width", 1600), "最大宽度", 1, 100000)
    height = _integer(options.get("max_height", 1600), "最大高度", 1, 100000)
    quality = _integer(options.get("quality", 85), "图片质量", 1, 100)
    target_kb = _integer(options.get("target_kb", 0), "目标体积 KB", 0, 1024 * 1024)
    page_size = options.get("pdf_page_size", "A4")
    if page_size not in {"A4", "image"}:
        raise ValueError("PDF 纸型必须为 A4 或 image。")
    landscape = _boolean(options, "pdf_landscape")
    files = expand_inputs(sources, _IMAGE_TYPES, recursive=_boolean(options, "recursive", True), exclude_root=folder)
    if not files:
        raise ValueError("没有找到支持的图片文件。")
    changes = 0
    if action == "compress":
        for index, source in enumerate(files):
            ensure_cancel(cancel)
            image = None
            try:
                image, original_format = _open_image(source, width, height, warnings)
                output_format = original_format if chosen == "original" else {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}[chosen]
                if output_format not in {"JPEG", "PNG", "WEBP", "BMP", "TIFF"}:
                    raise ValueError("不支持此图片的实际编码格式。")
                blob, dimensions = _compress(image, output_format, quality, target_kb, cancel)
                suffix = source.suffix.lower() if chosen == "original" else "." + chosen
                actual_suffixes = {"JPEG": {".jpg", ".jpeg"}, "PNG": {".png"}, "WEBP": {".webp"}, "BMP": {".bmp"}, "TIFF": {".tif", ".tiff"}}
                if chosen == "original" and suffix not in actual_suffixes[output_format]:
                    suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tiff"}[output_format]
                if len(blob) > source.stat().st_size:
                    warnings.append(f"{source.name} 的结果体积大于原图；已按指定尺寸、格式和质量输出。")
                target = unique_path(folder, source.stem + "_压缩" + suffix)
                atomic_write(target, lambda path: path.write_bytes(blob))
                outputs.append(str(target)); data_outputs.append(str(target))
                changes += 1
                _record(records, source, "第一帧", "成功", target,
                        f"{dimensions[0]}×{dimensions[1]} 像素；{len(blob) / 1024:.1f} KB；EXIF 已清除")
            except CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{source.name}：{exc}")
                _record(records, source, "", "失败", detail=exc)
            finally:
                if image is not None:
                    image.close()
            notify(progress, index + 1, len(files), source.name)
        return changes
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf._page import PageObject

    if target_kb:
        warnings.append("合成 PDF 时不限制整个 PDF 的体积；目标 KB 仅用于图片压缩操作。")
    failed = False
    with tempfile.TemporaryDirectory(prefix="qingjian_image_pdf_", dir=folder) as temporary:
        temporary = Path(temporary)
        with PdfWriter() as writer:
            for index, source in enumerate(files):
                ensure_cancel(cancel)
                image = rgb = None
                try:
                    image, _ = _open_image(source, width, height, warnings)
                    rgb = _flatten(image)
                    single = temporary / f"page-{index + 1}.pdf"
                    rgb.save(single, format="PDF", resolution=144.0, quality=quality)
                    with single.open("rb") as stream:
                        reader = PdfReader(stream)
                        image_page = reader.pages[0]
                        if page_size == "image":
                            writer.add_page(image_page)
                        else:
                            paper = (841.8898, 595.2756) if landscape else (595.2756, 841.8898)
                            page = PageObject.create_blank_page(width=paper[0], height=paper[1])
                            w, h = float(image_page.mediabox.width), float(image_page.mediabox.height)
                            scale = min(1.0, (paper[0] - 28.3465) / w, (paper[1] - 28.3465) / h)
                            transform = Transformation().scale(scale).translate((paper[0] - w * scale) / 2, (paper[1] - h * scale) / 2)
                            page.merge_transformed_page(image_page, transform)
                            writer.add_page(page)
                    _record(records, source, "第一帧", "已读取", detail=f"合成顺序 {index + 1}")
                except Exception as exc:
                    errors.append(f"{source.name}：{exc}")
                    _record(records, source, "", "失败", detail=exc)
                    failed = True
                finally:
                    if rgb is not None:
                        rgb.close()
                    if image is not None:
                        image.close()
                notify(progress, index + 1, len(files), source.name)
            if failed:
                warnings.append("合成已停止：至少一张图片读取失败，没有输出缺页的 PDF。")
                return 0
            ensure_cancel(cancel)
            target = unique_path(folder, _pdf_filename(options.get("output_name", "图片合成")))
            atomic_write(target, lambda path: _write_pdf(writer, path))
            changes = len(writer.pages)
    outputs.append(str(target)); data_outputs.append(str(target))
    _record(records, "全部输入", changes, "合成成功", target)
    return changes


def run_documents(tool, sources: list[Path], folder: Path, options: dict, progress=None, cancel=None) -> dict:
    """Run one document tool, preserving complete results and a row/file audit."""
    if tool not in {"template_fill", "pdf_pages", "image_pack"}:
        raise ValueError(f"不支持的文档工具：{tool}")
    folder = Path(folder).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    options = dict(options or {})
    outputs, data_outputs, warnings, errors, records = [], [], [], [], []
    changes = 0
    handlers = {"template_fill": _run_template, "pdf_pages": _run_pdf, "image_pack": _run_images}
    try:
        changes = handlers[tool](sources, folder, options, outputs, data_outputs, warnings, errors, records, progress, cancel)
    except CancelledError:
        warnings.append("已停止处理；已生成的完整结果保留在输出文件夹，详见处理报告。")
        # Count the completed audited units, rather than template DOCX+PDF files
        # or PDF chunks, whose number differs from rows/pages actually processed.
        if tool == "pdf_pages":
            changes = sum(len(str(record["原始行或页"]).split(",")) for record in records if record["状态"] == "成功")
        else:
            changes = sum(record["状态"] == "成功" for record in records)
    except Exception as exc:
        errors.append(str(exc))
        _record(records, "任务", "", "失败", detail=exc)
    warnings[:] = list(dict.fromkeys(warnings))
    _report(tool, folder, outputs, changes, warnings, errors, records)
    result = make_result(outputs, changes, warnings, errors)
    result["data_outputs"] = list(data_outputs)
    return result
