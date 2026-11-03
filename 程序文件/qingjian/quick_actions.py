"""Small, parameter-free conversions for the home page's drop targets.

Only explicit files and HTTP(S) URLs are accepted. Local results live beside
their original, and every file is first completed in a private same-volume
directory, then published atomically without replacing an existing filename.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .core.common import CancelledError, commit_file, ensure_cancel, notify, safe_name


_IMAGES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
_OFFICE = (".doc", ".docx", ".rtf", ".xlsx", ".pptx")
_WEB = (".html", ".htm", ".xhtml", ".mht", ".mhtml")
MAX_INPUTS = 500
MAX_SOURCE_BYTES = 256 * 1024 * 1024
_ACTION_LOCK = threading.Lock()

ACTIONS = {
    "to_pdf": {
        "id": "to_pdf", "title": "一键转 PDF", "glyph": "PDF",
        "description": "Word、Excel、PPT 或图片拖入即转，结果保存在原文件旁",
        "extensions": _OFFICE + _IMAGES, "accept_urls": False,
    },
    "pdf_to_word": {
        "id": "pdf_to_word", "title": "PDF 转 Word", "glyph": "W",
        "description": "提取可编辑文字与表格；扫描页保留图片，不执行 OCR",
        "extensions": (".pdf",), "accept_urls": False,
    },
    "web_to_word": {
        "id": "web_to_word", "title": "网页转 Word", "glyph": "↗",
        "description": "拖入链接或本地 HTML、MHTML；也可粘贴网页链接",
        "extensions": _WEB, "accept_urls": True,
    },
    "to_text": {
        "id": "to_text", "title": "文档转纯文本", "glyph": "TXT",
        "description": "Word、PDF、TXT、Markdown 一键导出 UTF-8 文本，无 OCR",
        "extensions": (".doc", ".docx", ".pdf", ".txt", ".md"), "accept_urls": False,
    },
    "image_to_jpg": {
        "id": "image_to_jpg", "title": "图片转 JPG", "glyph": "JPG",
        "description": "保持原尺寸，透明部分填白；多帧文件仅转换第一帧",
        "extensions": _IMAGES, "accept_urls": False,
    },
    "image_to_png": {
        "id": "image_to_png", "title": "图片转 PNG", "glyph": "PNG",
        "description": "保持原尺寸与透明通道；多帧文件仅转换第一帧",
        "extensions": _IMAGES, "accept_urls": False,
    },
}


def _is_http(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        # Access the port to reject malformed values before starting a worker.
        _ = parsed.port
        return (parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not re.search(r"[\s\x00-\x1f<>]", value))
    except ValueError:
        return False


def _payload_item(value) -> str | None:
    """Recognize one clipboard/drop item without interpreting arbitrary text."""
    if not isinstance(value, (str, Path)):
        return None
    value = str(value).strip().lstrip("\ufeff")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        return None
    if _is_http(value):
        return value
    if value.lower().startswith("file:"):
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if parsed.scheme.lower() != "file" or parsed.query or parsed.fragment:
            return None
        path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path = "//" + parsed.netloc + path
        elif re.match(r"^/[a-zA-Z]:[/\\]", path):
            path = path[1:]
        if not path or "\x00" in path or any(c in path for c in "\r\n"):
            return None
        return str(Path(path).absolute())
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value) and not re.match(r"^[a-zA-Z]:[/\\]", value):
        return None
    path = Path(value).expanduser()
    if path.is_absolute() or re.match(r"^[a-zA-Z]:[/\\]", value) or value.startswith("\\\\"):
        return str(path.absolute())
    # An actual explicitly named local file is useful for developer/API callers;
    # prose and unsupported HTML clipboard fragments are never instructions.
    try:
        if path.is_file():
            return str(path.absolute())
    except OSError:
        return None
    return None


def parse_mime_payload(urls: list[str], text: str = "") -> list[str]:
    """Parse file/URL MIME values or one file/URL per plain-text line.

    URL MIME takes precedence when present: browsers often provide an article's
    title alongside its URL, and that title is not another input. Raw HTML MIME
    and arbitrary clipboard text are intentionally unsupported.
    """
    values = list(urls or [])
    if not values and isinstance(text, str):
        values = text.splitlines()
    result, seen = [], set()
    for value in values:
        item = _payload_item(value)
        if item is None:
            continue
        key = item if _is_http(item) else os.path.normcase(str(Path(item).resolve()))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _local_source(value, action) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError("输入应为文件路径或网页链接。")
    item = _payload_item(value)
    if item is None:
        raise ValueError("只接受明确的本地文件路径或 HTTP(S) 网页链接。")
    if _is_http(item):
        raise ValueError("此快捷通道不接受网页链接，请拖到“网页转 Word”。")
    path = Path(item).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("请拖入实际文件，不读取符号链接或目录联接。")
    if path.is_dir():
        raise ValueError("请拖入具体文件；快捷通道不会扫描文件夹。")
    if not path.is_file():
        raise FileNotFoundError("找不到该输入文件，请确认文件仍在原位置。")
    if path.name.startswith("~$"):
        raise ValueError("这是 Office 临时锁定文件，请选择实际文档。")
    if path.suffix.lower() not in action["extensions"]:
        raise ValueError(f"不支持 {path.suffix or '无扩展名'} 文件；可接受：{'、'.join(action['extensions'])}。")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("单个文件超过 256 MB，请拆分后转换。")
    return path.resolve()


def validate_sources(action_id, sources) -> list[str]:
    """Return per-input errors without scanning, creating or modifying files.

    Mixed batches may still be run: run_quick_action records each invalid item
    and continues processing the valid ones.
    """
    if isinstance(action_id, str) and action_id.startswith("convert:"):
        from .converter_hub import validate_sources as validate_conversion
        return validate_conversion(action_id, sources)
    if action_id not in ACTIONS:
        return ["未知的快捷操作。"]
    if isinstance(sources, (str, Path)) or not isinstance(sources, (list, tuple)):
        return ["请传入文件或网页链接列表。"]
    if not sources:
        return ["请拖入文件或粘贴网页链接。"]
    if len(sources) > MAX_INPUTS:
        return [f"一次快捷转换最多处理 {MAX_INPUTS} 项，请分批拖入。"]
    action, errors = ACTIONS[action_id], []
    for source in sources:
        try:
            if isinstance(source, str) and _is_http(source):
                if not action["accept_urls"]:
                    raise ValueError("此快捷通道不接受网页链接，请拖到“网页转 Word”。")
            else:
                _local_source(source, action)
        except (OSError, ValueError) as exc:
            errors.append(f"{source}：{exc}")
    return errors


def _convert(action_id, source, target, cancel):
    """Reuse existing guarded converters. All targets remain private here."""
    ensure_cancel(cancel)
    options = {"_cancel": cancel}
    if action_id == "to_pdf":
        if Path(source).suffix.lower() in _IMAGES:
            from .core.pdf_office import _image_pdf
            _image_pdf(Path(source), target, cancel)
            return {"changes": 1, "warnings": ["图片按原比例置于 A4 页面，多帧或多页图片逐页转换。"]}
        from .core.conversion import convert_office_to_pdf
        return convert_office_to_pdf(Path(source), target, options)
    if action_id == "pdf_to_word":
        from .core.conversion import pdf_to_word
        result = pdf_to_word(Path(source), target, {**options, "pdf_mode": "text"})
        result.setdefault("warnings", []).append("此快捷操作不执行 OCR；扫描页面保留为图片，不能直接编辑其中的文字。")
        return result
    if action_id == "web_to_word":
        from .core.web_ops import webpage_to_word
        return webpage_to_word(source, target, {**options, "include_images": True, "max_images": 40})
    if action_id == "to_text":
        from .core.document_export import run_document_export
        result = run_document_export([str(source)], target.parent, {**options, "action": "text", "recursive": False}, cancel=cancel)
        ensure_cancel(cancel)
        if result.get("errors") or len(result.get("data_outputs", [])) != 1:
            raise ValueError("；".join(result.get("errors", [])) or "没有生成完整的纯文本文件。")
        exported = Path(result["data_outputs"][0])
        commit_file(exported, target)
        return result
    if action_id in {"image_to_jpg", "image_to_png"}:
        from .core.image_ops import crop_image
        return crop_image(Path(source), target, {"mode": "margins", "format": "jpg" if action_id == "image_to_jpg" else "png"})
    raise ValueError("未知的快捷操作。")


def _publish(staged, folder, stem, suffix, cancel):
    # Existing filenames and competing writers both go through the same
    # no-overwrite primitive. A raced filename is retried with the next suffix.
    cleaned = safe_name(stem, fallback="网页文章", limit=140)
    for number in range(1, 100000):
        ensure_cancel(cancel)
        target = folder / (cleaned + (f"_{number}" if number > 1 else "") + suffix)
        try:
            commit_file(staged, target)
            return target
        except FileExistsError:
            continue
    raise ValueError("同名输出过多，请整理目录后重试。")


def run_quick_action(action_id, sources, url_folder, progress=None, cancel=None) -> dict:
    """Convert independently, continuing after per-file failures.

    progress(done,total,message) is emitted from the calling worker thread.
    Cancellation preserves completed results and discards the current private
    staging directory. It never uploads local documents or runs local scripts.
    """
    if isinstance(action_id, str) and action_id.startswith("convert:"):
        from .converter_hub import run_conversion
        return run_conversion(action_id, sources, url_folder, progress, cancel)
    result = {"outputs": [], "records": [], "errors": [], "warnings": [], "changes": 0,
              "cancelled": False, "output_folders": [], "action_id": action_id,
              "total": len(sources) if isinstance(sources, (list, tuple)) else 0}
    if action_id not in ACTIONS:
        result["errors"].append("未知的快捷操作。")
        return result
    if isinstance(sources, (str, Path)) or not isinstance(sources, (list, tuple)) or not sources:
        result["errors"].append("请拖入文件或粘贴网页链接。")
        return result
    if len(sources) > MAX_INPUTS:
        result["errors"].append(f"一次快捷转换最多处理 {MAX_INPUTS} 项，请分批拖入。")
        return result
    action = ACTIONS[action_id]
    total = len(sources)
    # PDFium calls must be serial. Multiple quick workers queue here rather than
    # entering native conversion libraries at the same time.
    acquired = False
    try:
        while not acquired:
            ensure_cancel(cancel)
            acquired = _ACTION_LOCK.acquire(timeout=0.1)
        for index, value in enumerate(sources):
            ensure_cancel(cancel)
            record = {"source": str(value), "status": "failed", "output": "", "output_folder": "",
                      "warnings": [], "error": "", "changes": 0}
            result["records"].append(record)
            label = str(value) if _is_http(str(value)) else Path(str(value)).name
            notify(progress, index, total, f"正在转换：{label}")
            try:
                if isinstance(value, str) and _is_http(value):
                    if not action["accept_urls"]:
                        raise ValueError("此快捷通道不接受网页链接，请拖到“网页转 Word”。")
                    if not url_folder:
                        raise ValueError("网页链接没有原文件目录，请先设置快捷网页的保存位置。")
                    source = value
                    folder = Path(url_folder).expanduser().absolute()
                    for part in (folder, *folder.parents):
                        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                            raise ValueError("网页保存目录不能是符号链接或目录联接。")
                    folder.mkdir(parents=True, exist_ok=True)
                    folder = folder.resolve()
                    stem = "网页文章"
                else:
                    source = _local_source(value, action)
                    folder, stem = source.parent, source.stem
                suffix = {"to_pdf": ".pdf", "pdf_to_word": ".docx", "web_to_word": ".docx",
                          "to_text": ".txt", "image_to_jpg": ".jpg", "image_to_png": ".png"}[action_id]
                with tempfile.TemporaryDirectory(prefix=".qingjian-quick-", dir=folder) as temporary:
                    staged = Path(temporary) / ("result" + suffix)
                    converted = _convert(action_id, source, staged, cancel) or {}
                    ensure_cancel(cancel)
                    if not staged.is_file() or staged.stat().st_size == 0:
                        raise ValueError("转换器没有生成完整结果，未保存输出。")
                    if isinstance(source, str) and _is_http(source):
                        from docx import Document
                        stem = Document(staged).core_properties.title or "网页文章"
                    target = _publish(staged, folder, stem, suffix, cancel)
                record.update({"status": "success", "output": str(target), "output_folder": str(folder),
                               "warnings": list(converted.get("warnings", [])), "changes": 1})
                result["outputs"].append(str(target))
                result["changes"] += 1
                if str(folder) not in result["output_folders"]:
                    result["output_folders"].append(str(folder))
                result["warnings"].extend(f"{label}：{warning}" for warning in record["warnings"])
            except CancelledError:
                record.update({"status": "cancelled", "error": "已停止；本项未保存不完整结果。"})
                raise
            except PermissionError as exc:
                record["error"] = "无法读取源文件或写入结果目录，请检查原目录的只读状态、权限及文件占用；未改存其他位置。"
                result["errors"].append(f"{label}：{record['error']}（{exc}）")
            except Exception as exc:
                record["error"] = str(exc)
                result["errors"].append(f"{label}：{exc}")
            notify(progress, index + 1, total, f"{'已完成' if record['status'] == 'success' else '转换失败'}：{label}")
    except CancelledError:
        result["cancelled"] = True
        result["warnings"].append("已停止快捷转换，已完成的结果保留，当前项的临时文件已清理。")
    finally:
        if acquired:
            _ACTION_LOCK.release()
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


# The adapter emits data only; it does not touch widgets from its worker thread.
from PySide6.QtCore import QThread, Signal


class QuickActionWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)

    def __init__(self, action_id, sources, url_folder, parent=None):
        super().__init__(parent)
        self.action_id, self.sources = action_id, list(sources)
        self.url_folder = url_folder

    def run(self):
        try:
            result = run_quick_action(self.action_id, self.sources, self.url_folder,
                                      lambda done, total, message: self.progress.emit(done, total, message),
                                      self.isInterruptionRequested)
        except Exception as exc:
            result = {"outputs": [], "records": [], "errors": [str(exc)], "warnings": [],
                      "cancelled": self.isInterruptionRequested(), "changes": 0,
                      "output_folders": [], "action_id": self.action_id, "total": len(self.sources)}
        self.completed.emit(result)
