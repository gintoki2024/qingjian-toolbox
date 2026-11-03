"""Local PDF finishing and indexed submission books, without model downloads."""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import tempfile
import threading
from pathlib import Path

from .common import (CancelledError, atomic_write, ensure_cancel, expand_inputs,
                     make_result, notify, safe_name, unique_path, write_report_book)


def _field(key, title, kind="text", default="", **extra):
    return dict(key=key, title=title, kind=kind, default=default, **extra)


TOOL_SPECS = {
    "pdf_finish": dict(title="PDF压缩与标记", nav="PDF压缩与标记", group="文档与图片", extensions={".pdf"},
        description="压缩重复内容、旋转页面，批量添加页码、水印和书签。",
        note="原件保留；压缩不删除图片、不降低清晰度，体积不保证减小。页码和水印为叠加标记，不用于脱敏。含数字签名的PDF不生成修改副本；加密PDF需正确密码，输出副本不加密。",
        fields=[_field("compress", "无损压缩", "bool", True),
                _field("rotation", "顺时针旋转", "choice", 0, choices=[("不旋转", 0), ("90°", 90), ("180°", 180), ("270°", 270)]),
                _field("pages", "旋转页码", default="", placeholder="留空全部；例如 1-3,5"),
                _field("add_page_numbers", "添加页脚页码", "bool", False),
                _field("page_number_start", "页码起始值", "int", 1, minimum=1, maximum=1000000),
                _field("watermark", "水印文字", default="", placeholder="留空不加，例如：内部资料"),
                _field("watermark_opacity", "水印不透明度", "float", 0.15, minimum=0.01, maximum=1, decimals=2),
                _field("bookmarks", "新增书签", "rules", [], columns=[("title", "书签名称"), ("page", "PDF实际页序号")]),
                _field("password", "PDF打开密码", "password", ""), _field("recursive", "包含子文件夹", "bool", True)]),
    "submission_bind": dict(title="报送材料装订", nav="报送材料装订", group="文档自动化",
        extensions={".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"},
        description="按文件队列汇编材料，生成封面、目录、书签、连续页码和来源清单。",
        note="文件清单的上下顺序决定装订顺序；Office先由本地引擎转PDF。正文保留原纸型，图片按A4适配，多页TIFF逐页加入；不识别图片文字。任一材料失败时不发布缺页汇编。双面装订可补空白页，目录页码按整册实际页序计算。",
        fields=[_field("book_title", "材料册标题", default="报送材料汇编"),
                _field("subtitle", "副标题或报送单位", default=""),
                _field("cover", "生成封面", "bool", True), _field("contents", "生成目录", "bool", True),
                _field("add_page_numbers", "添加连续页码", "bool", True),
                _field("duplex_blank", "双面装订：每份材料从正面开始", "bool", False),
                _field("password", "PDF打开密码", "password", ""), _field("recursive", "包含子文件夹", "bool", False)]),
}

GUIDES = {
    "pdf_finish": dict(intro="为报送PDF统一方向和页码，或添加内部流转水印及导航书签。",
        quick="① 添加PDF　② 勾选压缩、旋转或标记　③ 查看结果与处理清单。",
        steps=["添加一个或多个PDF。原文件不会被覆盖。", "旋转页码留空处理全部页，也可填写1-3,5。",
               "页码按当前文件顺序连续编号；书签页码填写原PDF中的实际页序号，从1开始。",
               "点击开始处理。检查页脚是否与原文重叠，压缩前后体积见处理清单。"],
        terms=[("无损压缩", "合并重复对象并压缩内容流；不删除图片或改为低清图片。"),
               ("水印不透明度", "越小越淡；水印并不删除下方文字，不能替代脱敏。"),
               ("加密和签名", "密码只用于打开输入；输出不加密。签名PDF需另行制作允许修改的副本。")],
        example="示例PDF含一页文字，默认添加“内部资料”水印、连续页码和正文书签。"),
    "submission_bind": dict(intro="把已有文件编成一册有封面、目录和书签的PDF，保留来源页码清单。",
        quick="① 添加材料并调整队列顺序　② 填标题和装订设置　③ 查看整册PDF与来源清单。",
        steps=["添加PDF、DOCX、XLSX、PPTX或图片；展开文件清单，使用上移/下移排好顺序。",
               "填写材料册标题；按需要勾选封面、目录、连续页码和双面补空白页。",
               "Office材料依赖现有本地排版引擎；外部数据链接应先保存为完整本地副本。",
               "开始后打开PDF目录与书签。来源清单记录每份原件在整册中的起止页；有输入失败就不生成缺页汇编。"],
        terms=[("实际页序", "从整册第1页起，包含封面、目录和补入的空白页，与PDF阅读器一致。"),
               ("双面装订", "每份材料从整册奇数页开始，必要时在前一材料后补一张空白页。"),
               ("保留原纸型", "不会强制拉伸正文；Excel需先检查打印布局，复杂Office版式请复核转换结果。")],
        example="示例为两份虚构PDF材料，可试验目录、书签与双面补空白页。"),
}

_FONT_LOCK = threading.Lock()
_FONT = None


def _font():
    global _FONT
    with _FONT_LOCK:
        if _FONT:
            return _FONT
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("msyh.ttc", "simsun.ttc", "simhei.ttf"):
            try:
                if (fonts / filename).is_file():
                    pdfmetrics.registerFont(TTFont("QingjianOfficeCJK", str(fonts / filename), subfontIndex=0))
                    _FONT = "QingjianOfficeCJK"
                    return _FONT
            except Exception:
                continue
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _FONT = "STSong-Light"
        return _FONT


def _number(value, label, minimum, maximum, integer=False):
    if isinstance(value, bool):
        raise ValueError(label + "应为有效数字。")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(label + "应为有效数字。") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum or integer and result != int(result):
        raise ValueError(f"{label}应在{minimum}至{maximum}之间。")
    return int(result) if integer else result


def validate(mode, options, sources=None):
    if mode not in TOOL_SPECS:
        raise ValueError("未知PDF办公工具。")
    for key in ("compress", "add_page_numbers", "cover", "contents", "duplex_blank", "recursive"):
        if key in options and not isinstance(options[key], bool):
            raise ValueError(key + "应为是或否。")
    for key, limit in (("watermark", 100), ("book_title", 300), ("subtitle", 500), ("pages", 2000), ("password", 1000)):
        if key in options and (not isinstance(options[key], str) or len(options[key]) > limit):
            raise ValueError(key + "的文字类型或长度无效。")
    if mode == "pdf_finish":
        if options.get("rotation", 0) not in (0, 90, 180, 270):
            raise ValueError("旋转角度应为0、90、180或270。")
        _number(options.get("page_number_start", 1), "页码起始值", 1, 1000000, True)
        _number(options.get("watermark_opacity", 0.15), "水印不透明度", 0.01, 1)
        bookmarks = options.get("bookmarks", [])
        if not isinstance(bookmarks, list) or len(bookmarks) > 1000:
            raise ValueError("书签应为不超过1000行的列表。")
        for row in bookmarks:
            if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not row["title"].strip() or len(row["title"]) > 300:
                raise ValueError("每条书签需要填写有效名称。")
            _number(row.get("page"), "书签页码", 1, 5000, True)
    elif not options.get("book_title", "报送材料汇编").strip():
        raise ValueError("请填写材料册标题。")


def _signed(reader):
    root = reader.trailer["/Root"]
    permissions = root.get("/Perms")
    if permissions:
        permissions = permissions.get_object()
        if isinstance(permissions, dict):
            for key in ("/DocMDP", "/UR", "/UR3"):
                signature = permissions.get(key)
                if signature:
                    signature = signature.get_object()
                    if isinstance(signature, dict) and ("/ByteRange" in signature or signature.get("/Type") == "/Sig"):
                        return True
    form = root.get("/AcroForm")
    if not form:
        return False
    pending, seen = list(form.get_object().get("/Fields", [])), set()
    while pending:
        item = pending.pop().get_object()
        if id(item) in seen:
            continue
        seen.add(id(item))
        value = item.get("/V")
        if value:
            resolved = value.get_object()
            if item.get("/FT") == "/Sig" or isinstance(resolved, dict) and "/ByteRange" in resolved:
                return True
        pending.extend(item.get("/Kids", []))
    return False


@contextlib.contextmanager
def _pdf(path, password="", *, reject_signature=True):
    from pypdf import PdfReader
    path = Path(path)
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("单个PDF超过512MB，请分批处理。")
    reader = PdfReader(path, strict=False)
    try:
        if reader.is_encrypted and not reader.decrypt(password or ""):
            raise ValueError("PDF密码不正确或未填写密码。")
        if not 1 <= len(reader.pages) <= 5000:
            raise ValueError("PDF应有1至5000页。")
        if reject_signature and _signed(reader):
            raise ValueError("PDF含数字签名，修改或汇编会使签名失效，请提供允许修改的无签名副本。")
        yield reader
    finally:
        reader.close()


def _wrapped(text, width, size):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    lines, line = [], ""
    for char in str(text):
        if char in "\r\n":
            if line:
                lines.append(line)
            line = ""
        elif line and stringWidth(line + char, _font(), size) > width:
            lines.append(line)
            line = char
        else:
            line += char
    return lines + ([line] if line else []) or [""]


def _overlay(page, number=None, total=None, watermark="", opacity=0.15):
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas
    page.transfer_rotation_to_content()
    left, bottom, right, top = map(float, page.cropbox)
    width, height = right - left, top - bottom
    buffer = io.BytesIO()
    sheet = canvas.Canvas(buffer, pagesize=(float(page.mediabox.width), float(page.mediabox.height)), pageCompression=1)
    if watermark:
        from reportlab.pdfbase.pdfmetrics import stringWidth
        text_width = max(0.1, stringWidth(watermark, _font(), 1))
        angle = math.radians(35)
        box_width = text_width * math.cos(angle) + 1.2 * math.sin(angle)
        box_height = text_width * math.sin(angle) + 1.2 * math.cos(angle)
        size = min(44, width * 0.75 / box_width, height * 0.65 / box_height)
        sheet.saveState()
        sheet.setFillColorRGB(0.33, 0.34, 0.43)
        sheet.setFillAlpha(opacity)
        sheet.translate(left + width / 2, bottom + height / 2)
        sheet.rotate(35)
        sheet.setFont(_font(), size)
        sheet.drawCentredString(0, 0, watermark)
        sheet.restoreState()
    if number is not None:
        sheet.setFillColorRGB(0.35, 0.38, 0.44)
        sheet.setFont(_font(), 9)
        sheet.drawCentredString(left + width / 2, bottom + min(20, height / 20), f"第 {number} 页 / 共 {total} 页")
    sheet.showPage()
    sheet.save()
    buffer.seek(0)
    stamp = PdfReader(buffer)
    overlay = stamp.pages[0]
    overlay.mediabox = page.mediabox
    overlay.cropbox = page.cropbox
    page.merge_page(overlay)


def _write_pdf(writer, target, cancel=None):
    def write(path):
        ensure_cancel(cancel)
        writer.write(path)
        with _pdf(path, reject_signature=False) as result:
            if len(result.pages) != len(writer.pages):
                raise ValueError("生成PDF的页数校验未通过。")
        ensure_cancel(cancel)
    atomic_write(target, write)


def _report(mode, folder, outputs, data, records, warnings, errors, changes):
    title = TOOL_SPECS[mode]["title"]
    table = unique_path(folder, title + "_核查清单.xlsx")
    keys = list(dict.fromkeys(key for record in records for key in record)) or ["状态", "说明"]
    write_report_book(table, {"处理明细": [keys, *[[record.get(key, "") for key in keys] for record in records]],
                              "说明": [["类别", "内容"], *[["提示", value] for value in warnings], *[["异常", value] for value in errors]]})
    record_file = unique_path(folder, title + "_处理记录.json")
    atomic_write(record_file, lambda path: path.write_text(json.dumps({"tool": mode, "records": records, "warnings": warnings,
                 "errors": errors, "outputs": data}, ensure_ascii=False, indent=2), encoding="utf-8"))
    result = make_result([*outputs, str(table), str(record_file)], changes, warnings, errors)
    result["data_outputs"] = list(data)
    return result


def _finish(sources, folder, options, progress, cancel):
    from pypdf import PdfWriter
    from .document_ops import _page_numbers
    files = expand_inputs(sources, {".pdf"}, options.get("recursive", True), folder)
    if not files:
        raise ValueError("没有可处理的PDF。")
    outputs, records, errors, warnings = [], [], [], []
    for index, source in enumerate(files, 1):
        ensure_cancel(cancel)
        notify(progress, index - 1, len(files), source.name)
        try:
            with _pdf(source, options.get("password", "")) as reader:
                if reader.is_encrypted:
                    warnings.append(f"{source.name}：输出为未加密副本。")
                writer = PdfWriter(clone_from=reader)
                try:
                    selected = set(_page_numbers(options["pages"], len(reader.pages))) if options.get("pages", "").strip() else set(range(len(reader.pages)))
                    for number, page in enumerate(writer.pages):
                        ensure_cancel(cancel)
                        if number in selected and options.get("rotation", 0):
                            page.rotate(options["rotation"])
                        if options.get("add_page_numbers") or options.get("watermark", ""):
                            _overlay(page, number + int(options.get("page_number_start", 1)) if options.get("add_page_numbers") else None,
                                     len(writer.pages) + int(options.get("page_number_start", 1)) - 1,
                                     options.get("watermark", ""), options.get("watermark_opacity", 0.15))
                        if options.get("compress", True):
                            page.compress_content_streams()
                    for row in options.get("bookmarks", []):
                        page_number = _number(row["page"], "书签页码", 1, len(writer.pages), True)
                        writer.add_outline_item(row["title"], page_number - 1)
                    if options.get("compress", True):
                        writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
                    target = unique_path(folder, source.stem + "_整理.pdf")
                    _write_pdf(writer, target, cancel)
                    outputs.append(str(target))
                    before, after = source.stat().st_size, target.stat().st_size
                    records.append({"输入": str(source), "输出": str(target), "状态": "完成", "页数": len(writer.pages), "原体积字节": before, "结果字节": after})
                    if options.get("compress", True) and after >= before:
                        warnings.append(f"{source.name}：整理后体积未减小；保留全部页面和图片。")
                finally:
                    writer.close()
        except CancelledError:
            raise
        except Exception as exc:
            errors.append(f"{source.name}：{exc}")
            records.append({"输入": str(source), "状态": "失败", "说明": str(exc)})
        notify(progress, index, len(files), source.name)
    return _report("pdf_finish", folder, outputs, outputs, records, warnings, errors, len(outputs))


def _image_pdf(source, target, cancel=None):
    from PIL import Image, ImageOps
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    with Image.open(source) as original:
        frames = getattr(original, "n_frames", 1)
        if frames > 1000:
            raise ValueError("单个图片文件超过1000帧，请拆分后再装订。")
        width, height = 595.2756, 841.8898
        sheet = canvas.Canvas(str(target), pagesize=(width, height), pageCompression=1)
        for index in range(frames):
            ensure_cancel(cancel)
            original.seek(index)
            if original.width * original.height > 40_000_000:
                raise ValueError("单帧图片超过4000万像素，请缩小后再装订。")
            oriented = ImageOps.exif_transpose(original)
            image = oriented.convert("RGBA")
            background = Image.new("RGB", image.size, "white")
            try:
                background.paste(image, mask=image.getchannel("A"))
                ratio = min((width - 60) / image.width, (height - 60) / image.height)
                w, h = image.width * ratio, image.height * ratio
                sheet.drawImage(ImageReader(background), (width - w) / 2, (height - h) / 2, w, h)
                sheet.showPage()
            finally:
                oriented.close()
                image.close()
                background.close()
        sheet.save()


def _toc_groups(entries):
    groups, current, height = [], [], 0
    for entry in entries:
        lines = _wrapped(entry["title"], 385, 10)
        row_height = max(29, len(lines) * 16 + 12)
        if current and height + row_height > 645:
            groups.append(current)
            current, height = [], 0
        current.append((entry, lines, row_height))
        height += row_height
    return groups + ([current] if current else [])


def _frontmatter(entries, options, groups, total):
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    width, height = 595.2756, 841.8898
    sheet = canvas.Canvas(buffer, pagesize=(width, height), pageCompression=1)
    if options.get("cover", True):
        sheet.setFillColorRGB(0.39, 0.34, 0.87)
        sheet.rect(55, height - 175, 6, 80, fill=1, stroke=0)
        sheet.setFont(_font(), 12)
        sheet.drawString(78, height - 115, "报送材料 · 本地汇编")
        sheet.setFillColorRGB(0.15, 0.19, 0.28)
        title_size, subtitle_size = 25, 12
        title, subtitle = options.get("book_title", "报送材料汇编"), options.get("subtitle", "")
        while True:
            title_lines = _wrapped(title, width - 130, title_size)
            subtitle_lines = _wrapped(subtitle, width - 130, subtitle_size) if subtitle else []
            block_height = len(title_lines) * title_size * 1.5 + len(subtitle_lines) * subtitle_size * 1.7 + 35
            if block_height <= 400:
                break
            if title_size > 14:
                title_size -= 1
            elif subtitle_size > 9:
                subtitle_size -= 0.5
            elif title_size > 10:
                title_size -= 1
            else:
                raise ValueError("封面文字排版超出一页，请缩短标题或副标题。")
        y = height - 220
        for line in title_lines:
            sheet.setFont(_font(), title_size)
            sheet.drawString(65, y, line)
            y -= title_size * 1.5
        sheet.setFillColorRGB(0.4, 0.44, 0.52)
        sheet.setFont(_font(), subtitle_size)
        for line in subtitle_lines:
            sheet.drawString(65, y - 25, line)
            y -= subtitle_size * 1.7
        sheet.drawString(65, 130, f"共 {len(entries)} 份材料 · 整册 {total} 页")
        sheet.drawString(65, 106, "原始文件保留，来源及页码见随附核查清单。")
        sheet.showPage()
    for group in groups:
        sheet.setFillColorRGB(0.15, 0.19, 0.28)
        sheet.setFont(_font(), 22)
        sheet.drawString(55, height - 75, "材料目录")
        sheet.setStrokeColorRGB(0.85, 0.86, 0.91)
        sheet.line(55, height - 93, width - 55, height - 93)
        y = height - 126
        for entry, lines, row_height in group:
            sheet.setFont(_font(), 10)
            sheet.setFillColorRGB(0.15, 0.19, 0.28)
            for offset, line in enumerate(lines):
                sheet.drawString(55, y - offset * 16, line)
            sheet.setFillColorRGB(0.39, 0.34, 0.87)
            sheet.drawRightString(width - 55, y, f"{entry['start']}–{entry['end']}")
            y -= row_height
        sheet.showPage()
    sheet.save()
    buffer.seek(0)
    return PdfReader(buffer)


def _bind(sources, folder, options, progress, cancel):
    from pypdf import PdfWriter
    from .conversion import convert_office_to_pdf
    files = expand_inputs(sources, TOOL_SPECS["submission_bind"]["extensions"], options.get("recursive", False), folder)
    if not files or len(files) > 500:
        raise ValueError("请添加1至500份材料；队列顺序决定装订顺序。")
    records, errors, warnings, entries, outputs = [], [], [], [], []
    with tempfile.TemporaryDirectory(prefix=".qingjian-bind-", dir=folder) as temporary:
        temporary = Path(temporary)
        for index, source in enumerate(files, 1):
            ensure_cancel(cancel)
            notify(progress, index - 1, len(files) + 1, source.name)
            try:
                target = temporary / f"{index:04d}.pdf"
                if source.suffix.lower() == ".pdf":
                    target = source
                elif source.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
                    detail = convert_office_to_pdf(source, target, {**options, "_cancel": cancel})
                    warnings.extend(detail.get("warnings", []))
                else:
                    _image_pdf(source, target, cancel)
                with _pdf(target, options.get("password", "")) as reader:
                    if reader.is_encrypted:
                        warnings.append(f"{source.name}：汇编输出不加密。")
                    entry = dict(source=str(source), title=source.stem, path=target, pages=len(reader.pages))
                    entries.append(entry)
            except CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{source.name}：{exc}")
                records.append({"来源": str(source), "状态": "失败", "说明": str(exc)})
        if errors:
            warnings.append("部分材料无法处理，未发布缺页汇编。请根据清单修正后重新装订。")
        else:
            groups = _toc_groups(entries) if options.get("contents", True) else []
            prefix = int(options.get("cover", True)) + len(groups)
            cursor = prefix
            for entry in entries:
                entry["blank_before"] = bool(options.get("duplex_blank", False) and cursor % 2)
                cursor += int(entry["blank_before"])
                entry["start"], entry["end"] = cursor + 1, cursor + entry["pages"]
                cursor += entry["pages"]
            if cursor > 5000:
                raise ValueError("整册超过5000页，请分册装订。")
            writer = PdfWriter()
            try:
                if prefix:
                    front = _frontmatter(entries, options, groups, cursor)
                    writer.append(front, import_outline=False)
                for entry in entries:
                    ensure_cancel(cancel)
                    if entry["blank_before"]:
                        writer.add_blank_page(width=595.2756, height=841.8898)
                    with _pdf(entry["path"], options.get("password", "")) as reader:
                        writer.append(reader, import_outline=False)
                    writer.add_outline_item(entry["title"], entry["start"] - 1)
                    records.append({"来源": entry["source"], "标题": entry["title"], "状态": "完成", "原始页数": entry["pages"],
                                    "整册起始页": entry["start"], "整册结束页": entry["end"], "此前补空白页": entry["blank_before"]})
                if options.get("add_page_numbers", True):
                    for index, page in enumerate(writer.pages, 1):
                        ensure_cancel(cancel)
                        _overlay(page, index, len(writer.pages))
                writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
                target = unique_path(folder, safe_name(options.get("book_title", "报送材料汇编"), limit=100) + ".pdf")
                _write_pdf(writer, target, cancel)
                outputs.append(str(target))
                notify(progress, len(files) + 1, len(files) + 1, "材料册已完成")
            finally:
                writer.close()
    return _report("submission_bind", folder, outputs, outputs, records, warnings, errors, len(files) if outputs else 0)


def run(mode, sources, folder, options, progress=None, cancel=None):
    validate(mode, options, sources)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    ensure_cancel(cancel)
    return (_finish if mode == "pdf_finish" else _bind)(sources, folder, options, progress, cancel)


def build_example(mode, folder):
    from reportlab.pdfgen import canvas
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    sources = []
    for index in range(1, 3):
        target = folder / f"{index:02d}_示例材料.pdf"
        sheet = canvas.Canvas(str(target), pagesize=(595.2756, 841.8898))
        sheet.setFont(_font(), 18)
        sheet.drawString(60, 745, f"虚构办公材料 {index}")
        sheet.setFont(_font(), 11)
        sheet.drawString(60, 705, "此文件仅用于练习，不含真实业务资料。")
        sheet.save()
        sources.append(str(target))
    if mode == "pdf_finish":
        return [sources[0]], dict(compress=True, rotation=0, add_page_numbers=True, watermark="内部资料", watermark_opacity=0.15,
                                  bookmarks=[dict(title="示例正文", page="1")])
    return sources, dict(book_title="练习材料汇编", subtitle="综合办公 · 虚构示例", cover=True, contents=True,
                         add_page_numbers=True, duplex_blank=True, recursive=False)
