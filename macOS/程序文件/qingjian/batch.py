"""Batch dispatch, isolated output directories and local audit reports."""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .core.conversion import convert_word, pdf_to_word
from .core.image_ops import crop_image
from .core.web_ops import webpage_to_word
from .core.word_ops import process_word

SUPPORTED = {
    "word": {".docx", ".doc", ".txt"},
    "web": {".html", ".htm", ".xhtml", ".mht", ".mhtml"},
    "convert": {".docx", ".doc", ".rtf", ".pdf"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"},
}


def is_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def validate_options(mode: str, options: dict) -> None:
    if mode == "word":
        if not isinstance(options.get("official_enabled", False), bool):
            raise ValueError("公文排版开关应为是或否。")
        if options.get("official_enabled"):
            from .core.official_ops import validate_official_options
            validate_official_options(options.get("official", {}))
        for rule in options.get("replacements", []):
            if not rule["find"]:
                raise ValueError("查找内容不能为空。")
            if rule.get("regex"):
                try:
                    compiled = re.compile(rule["find"])
                    compiled.sub(rule["replace"], "")
                except (re.error, IndexError) as exc:
                    raise ValueError(f"替换规则的正则表达式或替换写法无效：{exc}") from exc
        if not any((options.get("format_enabled"), options.get("replacements"),
                    options.get("delete_paragraphs"), options.get("mask_phone"),
                    options.get("mask_id"), options.get("mask_email"),
                    options.get("custom_sensitive"), options.get("custom_sensitive_rules"), options.get("clean_metadata"),
                    options.get("remove_comments"), options.get("remove_blank_paragraphs"),
                    options.get("official_enabled"))):
            raise ValueError("请至少选择一项 Word 处理操作。")


def new_run_directory(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("青简处理_%Y%m%d_%H%M%S")
    for index in range(10000):
        path = base / (name if index == 0 else f"{name}_{index}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise OSError("无法创建独立结果目录，请更换保存位置。")


def output_path(mode: str, source: str, folder: Path, options: dict, index: int) -> Path:
    if is_url(source):
        stem = f"网页_{index + 1:03d}"
        suffix = ".docx"
    else:
        file = Path(source)
        stem = file.stem
        suffix = file.suffix.lower()
        if mode == "word" and suffix == ".txt":
            suffix = ".txt"
        elif mode in {"word", "web"}:
            suffix = ".docx"
        elif mode == "convert":
            suffix = ".docx" if suffix == ".pdf" else ".pdf"
        elif mode == "image" and options.get("format", "original") != "original":
            suffix = "." + options["format"]
    # Stable numeric prefix disambiguates identical names from different folders.
    return folder / f"{index + 1:03d}_{stem[:180]}{suffix}"


def dispatch(mode: str, source: str, target: Path, options: dict) -> dict:
    if mode == "web":
        return webpage_to_word(source, target, options)
    path = Path(source)
    if mode == "word":
        if path.suffix.lower() == ".txt":
            from .core.text_ops import process_text
            return process_text(path, target, options)
        def word_file(input_path):
            official = options.get("official_enabled", False)
            config = options.get("official", {})
            check_only = official and config.get("operation", "format") == "check"
            if check_only:
                from docx import Document
                from .core.official_ops import inspect_official_document, validate_official_options
                config = validate_official_options(config)
                result = inspect_official_document(Document(input_path), config)
            else:
                result = process_word(input_path, target, options)
            if official:
                from .core.official_reports import write_official_reports
                try:
                    reports = write_official_reports(source, target, result, config, check_only=check_only)
                    result["reports"] = reports
                    result["primary_output"] = reports[0] if check_only else str(target)
                except Exception as exc:
                    if check_only:
                        raise
                    result.setdefault("warnings", []).append(f"排版文档已生成，但检查报告保存失败：{exc}")
                if check_only:
                    result.setdefault("warnings", []).append("仅检查任务未执行脱敏、替换或格式修改；报告包含标题摘要。")
                # The detailed local report contains the audit. The batch log
                # keeps only its paths and counts, including redaction jobs.
                result["official_summary"] = result.get("audit", {}).get("counts", {})
                result.pop("audit", None)
            return result
        if path.suffix.lower() == ".doc":
            with tempfile.TemporaryDirectory(prefix="qingjian_doc_") as tmp:
                intermediate = Path(tmp) / "converted.docx"
                convert_word(path, intermediate, options)
                result = word_file(intermediate)
                result.setdefault("warnings", []).insert(0, "旧版 DOC 已先转为 DOCX，请复核版式。")
                return result
        return word_file(path)
    if mode == "convert":
        return (pdf_to_word if path.suffix.lower() == ".pdf" else convert_word)(path, target, options)
    if mode == "image":
        return crop_image(path, target, options)
    raise ValueError("未知的工具类型。")


def write_report(folder: Path, records: list[dict], cancelled: bool) -> Path:
    report = {
        "工具": "青简工具箱 " + __version__,
        "时间": datetime.now().astimezone().isoformat(timespec="seconds"),
        "已停止": cancelled,
        "说明": "本记录仅保存在本机，包含文件路径、处理统计和警告，不记录文档正文或脱敏前后内容。",
        "结果": records,
    }
    target = folder / "处理记录.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
