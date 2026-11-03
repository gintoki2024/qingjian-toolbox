"""Read-only document text and embedded original-image export."""
from __future__ import annotations

import json
import os
import posixpath
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from .common import (CancelledError, atomic_write, ensure_cancel, expand_inputs,
                     make_result, notify, safe_name, unique_path)
from .conversion import convert_word
from .text_ops import MAX_TEXT_BYTES, _ordinary_path, read_text_file


TEXT_TYPES = {".docx", ".doc", ".pdf", ".txt", ".md"}
IMAGE_TYPES = {".docx", ".doc"}
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_ZIP_MEMBERS = 10000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGES = 10000
MAX_INPUT_FILES = 50000
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp",
                   ".svg", ".emf", ".wmf", ".emz", ".wmz", ".ico", ".heic", ".avif", ".dib", ".eps", ".pict", ".pct"}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _checked_archive(path):
    archive = zipfile.ZipFile(path)
    try:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
            raise ValueError("Word 内部成员超过 10000 个或展开大小超过 512 MB。")
        if not {"[Content_Types].xml", "word/document.xml"}.issubset(archive.namelist()):
            raise ValueError("文件缺少有效 DOCX 文档结构。")
        seen = set()
        for item in members:
            name = PurePosixPath(item.filename)
            key = item.filename.casefold()
            if (name.is_absolute() or ".." in name.parts or "\\" in item.filename
                    or key in seen or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError("Word 内部成员路径不规范、重名或含有链接，已停止该文件。")
            seen.add(key)
            if item.flag_bits & 1:
                raise ValueError("Word 内部成员已加密，无法读取。")
        return archive
    except BaseException:
        archive.close()
        raise


def _xml(archive, name):
    member = archive.getinfo(name)
    if member.file_size > MAX_MEMBER_BYTES:
        raise ValueError("单个 Word XML 部件超过 64 MB，无法安全读取。")
    data = archive.read(member)
    if b"<!DOCTYPE" in data.upper():
        raise ValueError("Word 含有不受支持的 XML DTD，已停止该文件。")
    return ET.fromstring(data)


def _paragraph(element):
    pieces = []
    for item in element.iter():
        if item.tag == _W + "t":
            pieces.append(item.text or "")
        elif item.tag == _W + "tab":
            pieces.append("\t")
        elif item.tag in {_W + "br", _W + "cr"}:
            pieces.append("\n")
    return "".join(pieces)


def _blocks(element, cancel):
    for child in element:
        ensure_cancel(cancel)
        if child.tag == _W + "p":
            yield _paragraph(child)
        elif child.tag == _W + "tbl":
            yield "[表格]"
            for row in child.findall(_W + "tr"):
                cells = [" / ".join(_blocks(cell, cancel)) for cell in row.findall(_W + "tc")]
                yield "\t".join(cells)
        elif child.tag in {_W + "sdt", _W + "sdtContent", _W + "customXml"}:
            yield from _blocks(child, cancel)


def _word_text(path, options, warnings, cancel):
    with _checked_archive(path) as archive:
        root = _xml(archive, "word/document.xml")
        body = root.find(_W + "body")
        if body is None:
            raise ValueError("Word 缺少正文结构。")
        yield "[正文]"
        yield from _blocks(body, cancel)
        if not options.get("include_headers", True):
            return
        relation_path = "word/_rels/document.xml.rels"
        relations = {}
        if relation_path in archive.namelist():
            for relation in _xml(archive, relation_path):
                relations[relation.get("Id")] = relation
        seen = set()
        for reference in root.iter():
            if reference.tag not in {_W + "headerReference", _W + "footerReference"}:
                continue
            relation = relations.get(reference.get(_R + "id"))
            if relation is None or relation.get("TargetMode") == "External":
                warnings.append(f"{path.name} 的页眉页脚引用缺失或为外部内容，未读取该部分。")
                continue
            target = relation.get("Target", "")
            part = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "word/" + target)
            if part in seen:
                continue
            if part.startswith("../") or part not in archive.namelist():
                warnings.append(f"{path.name} 的页眉页脚部件缺失，未读取该部分。")
                continue
            seen.add(part)
            yield "[页眉]" if reference.tag == _W + "headerReference" else "[页脚]"
            yield from _blocks(_xml(archive, part), cancel)


def _pdf_text(path, options, warnings, cancel):
    from pypdf import PdfReader

    with Path(path).open("rb") as stream:
        reader = PdfReader(stream)
        if reader.is_encrypted:
            try:
                opened = reader.decrypt(options.get("password") or "")
            except Exception as exc:
                raise ValueError("无法打开加密 PDF，请提供正确密码或可读取的解密副本。") from exc
            if not opened:
                raise ValueError("PDF 密码错误或缺少打开密码。")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("单个 PDF 超过 10000 页，请拆分后导出。")
        empty = []
        for index, page in enumerate(reader.pages, 1):
            ensure_cancel(cancel)
            yield f"\n=== 第{index}页 ==="
            text = page.extract_text() or ""
            if not text.strip():
                empty.append(index)
                yield "（本页未提取到电子文字；扫描图像不执行 OCR）"
            else:
                yield text
        if empty:
            pages = "、".join(map(str, empty[:30])) + ("等" if len(empty) > 30 else "")
            warnings.append(f"{path.name} 第{pages}页未提取到电子文字；不执行 OCR。")
        if not reader.pages:
            warnings.append(f"{path.name} 共 0 页，没有可导出的电子文字。")


def _text_export(path, original, target, options, warnings, cancel):
    extension = path.suffix.lower()
    if extension in {".txt", ".md"}:
        chunks = iter([read_text_file(path, warnings)])
    elif extension == ".docx":
        chunks = _word_text(path, options, warnings, cancel)
    elif extension == ".pdf":
        chunks = _pdf_text(path, options, warnings, cancel)
    else:
        raise ValueError("不支持此文件的纯文本导出。")

    def write(staged):
        count = 0
        with staged.open("wb") as stream:
            header = f"来源文件：{original.name}\n来源路径：{original}\n\n".encode("utf-8")
            stream.write(header)
            count += len(header)
            for chunk in chunks:
                ensure_cancel(cancel)
                data = (chunk + "\n").encode("utf-8")
                count += len(data)
                if count > MAX_TEXT_BYTES:
                    raise ValueError("导出的纯文本超过 50 MB，未保存不完整结果。")
                stream.write(data)
    try:
        atomic_write(target, write)
    finally:
        close = getattr(chunks, "close", None)
        if close:
            close()


def _image_directory(folder, label):
    stem = safe_name(label, limit=150)
    for index in range(1, 100000):
        target = folder / (stem if index == 1 else f"{stem}_{index}")
        try:
            target.mkdir()
            return target
        except FileExistsError:
            continue
    raise ValueError("同名图片目录过多，请选择新的结果目录。")


def _images(path, original, folder, index, warnings, cancel):
    with _checked_archive(path) as archive:
        members = []
        for item in archive.infolist():
            name = PurePosixPath(item.filename)
            if item.is_dir() or len(name.parts) != 3 or name.parts[:2] != ("word", "media"):
                continue
            if name.suffix.lower() not in _IMAGE_SUFFIXES:
                warnings.append(f"{original.name}：word/media 中的 {name.name} 不是支持的图像格式，已跳过。")
                continue
            if item.file_size > MAX_MEMBER_BYTES:
                raise ValueError("单张内嵌图像超过 64 MB，已停止该文件。")
            members.append(item)
        if not members:
            warnings.append(f"{original.name}：提取到 0 个内嵌图片；不下载外部链接图片。")
            return
        directory = _image_directory(folder, f"{index:03d}_{original.stem}_图片")
        for ordinal, member in enumerate(members, 1):
            ensure_cancel(cancel)
            suffix = PurePosixPath(member.filename).suffix
            target = unique_path(directory, f"图片_{ordinal:03d}{suffix}")

            def write(staged):
                count = 0
                with archive.open(member) as source, staged.open("wb") as destination:
                    while True:
                        ensure_cancel(cancel)
                        data = source.read(1024 * 1024)
                        if not data:
                            break
                        count += len(data)
                        if count > MAX_MEMBER_BYTES:
                            raise ValueError("内嵌图像超出读取上限，未保存不完整图像。")
                        destination.write(data)
                    if count != member.file_size:
                        raise ValueError("内嵌图像长度校验失败。")
            atomic_write(target, write)
            yield target


def run_document_export(sources, folder, options, progress=None, cancel=None):
    """Export per file with explicit skips/errors and preserve complete outputs."""
    if not isinstance(options, dict):
        raise ValueError("文档导出设置应为字段和值组成的对象。")
    action = options.get("action", "text")
    if action not in {"text", "images"}:
        raise ValueError("导出操作应为 text 或 images。")
    for key in ("include_headers", "recursive"):
        if key in options and not isinstance(options[key], bool):
            raise ValueError(f"{key} 应为是或否。")
    if options.get("password") is not None and not isinstance(options["password"], str):
        raise ValueError("PDF 打开密码应为文字。")
    folder = Path(folder).expanduser().absolute()
    _ordinary_path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    files, seen, outputs, warnings, errors, records = [], set(), [], [], [], []
    cancelled = False
    limit_reached = False
    try:
        for source in sources:
            ensure_cancel(cancel)
            try:
                found = expand_inputs([source], recursive=options.get("recursive", True), exclude_root=folder)
                if not found:
                    warnings.append(f"{source}：没有可导出的实际文件，或位于本次结果目录中，已跳过。")
                    records.append({"来源": str(source), "状态": "跳过", "输出": [], "说明": "未找到可导出文件"})
                for path in found:
                    ensure_cancel(cancel)
                    key = os.path.normcase(str(path))
                    if key not in seen:
                        if len(files) >= MAX_INPUT_FILES:
                            limit_reached = True
                            warnings.append(f"达到 {MAX_INPUT_FILES} 个文件上限，后续文件未继续检查或导出，请缩小范围。")
                            records.append({"来源": str(path), "状态": "跳过", "输出": [], "说明": "达到文件数量上限，后续未检查"})
                            break
                        files.append(path)
                        seen.add(key)
            except CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{source}：{exc}")
                records.append({"来源": str(source), "状态": "失败", "输出": [], "说明": str(exc)})
            if limit_reached:
                break
        allowed = TEXT_TYPES if action == "text" else IMAGE_TYPES
        for index, source in enumerate(files, 1):
            ensure_cancel(cancel)
            notify(progress, index - 1, len(files), f"导出：{source.name}")
            record = {"来源": str(source), "状态": "成功", "输出": [], "说明": ""}
            records.append(record)
            if source.suffix.lower() not in allowed:
                record.update({"状态": "跳过", "说明": "此格式不支持当前导出操作"})
                warnings.append(f"{source.name}：不支持当前{'原图' if action == 'images' else '纯文本'}导出操作，已跳过。")
                notify(progress, index, len(files), f"已跳过：{source.name}")
                continue
            try:
                if source.stat().st_size > MAX_SOURCE_BYTES:
                    raise ValueError("单个源文档超过 256 MB，请拆分后导出。")
                with tempfile.TemporaryDirectory(prefix="qj_export_") as temporary:
                    path = source
                    if source.suffix.lower() == ".doc":
                        path = Path(temporary) / "converted.docx"
                        result = convert_word(source, path, options)
                        warnings.extend(f"{source.name}：{value}" for value in result.get("warnings", []))
                        warnings.append(f"{source.name}：旧版 DOC 已经本机排版引擎转换后提取，请复核转换结果。")
                        ensure_cancel(cancel)
                    if action == "text":
                        target = unique_path(folder, f"{index:03d}_{source.stem}.txt")
                        _text_export(path, source, target, options, warnings, cancel)
                        record["输出"].append(str(target))
                        outputs.append(str(target))
                    else:
                        for target in _images(path, source, folder, index, warnings, cancel):
                            record["输出"].append(str(target))
                            outputs.append(str(target))
                        if not record["输出"]:
                            record["说明"] = "提取到 0 个内嵌图片"
            except CancelledError:
                record.update({"状态": "已停止", "说明": "已停止，已生成的完整结果保留"})
                raise
            except Exception as exc:
                record.update({"状态": "部分成功" if record["输出"] else "失败", "说明": str(exc)})
                errors.append(f"{source.name}：{exc}")
            notify(progress, index, len(files), f"已检查：{source.name}")
    except CancelledError:
        cancelled = True
        warnings.append("已停止导出，已生成的完整结果保留；未保存不完整文件。")
    warnings = list(dict.fromkeys(warnings))
    data_outputs = list(outputs)
    report = unique_path(folder, "文档导出报告.json")
    payload = {"tool": "document_export", "action": action, "changes": len(data_outputs),
               "cancelled": cancelled, "outputs": data_outputs, "warnings": warnings,
               "errors": errors, "records": records}
    try:
        atomic_write(report, lambda staged: staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"))
        outputs.append(str(report))
    except Exception as exc:
        errors.append(f"文档导出报告保存失败：{exc}")
        report = None
    result = make_result(outputs, len(data_outputs), warnings, errors)
    result.update({"data_outputs": data_outputs, "report": str(report) if report else "", "cancelled": cancelled})
    return result
