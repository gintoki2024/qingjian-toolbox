"""Read-only, bounded document search sessions for the interactive viewer.

Results and extracted text exist only in memory.  This module deliberately has
no output-folder/report argument and never creates a file or a directory.
"""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .common import CancelledError, ensure_cancel, expand_inputs
from .content_ops import EXTENSIONS, iter_text_units

MAX_HITS = 5000
MAX_RESULT_CHARACTERS = 8 * 1024 * 1024
MAX_PREVIEW_CHARACTERS = 32000
MAX_HIGHLIGHT_RANGES = 256
MAX_READ_ERRORS = 300

SCOPE_DESCRIPTION = (
    "支持 DOCX、PDF、XLSX/XLSM、CSV/TSV、TXT/Markdown 和本地 HTML。"
    "Word 按段落或表格行、PDF 按页、Excel 按行、文本按行定位；"
    "“全部命中”要求关键词出现在同一个定位单元中。扫描图片、旧版 DOC/XLS 不作文字识别。"
)


@dataclass(frozen=True)
class SearchOptions:
    queries: tuple[str, ...]
    match_mode: str
    case_sensitive: bool
    recursive: bool


@dataclass(frozen=True)
class SearchHit:
    location: str
    text: str
    keywords: tuple[str, ...]
    ranges: tuple[tuple[int, int], ...]
    truncated: bool = False


@dataclass(frozen=True)
class FileSearchResult:
    path: Path
    hits: tuple[SearchHit, ...]
    partial: bool = False
    error: str = ""


@dataclass(frozen=True)
class SearchReadError:
    path: Path
    message: str


@dataclass
class SearchReport:
    queries: tuple[str, ...] = ()
    files: list[FileSearchResult] = field(default_factory=list)
    errors: list[SearchReadError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_files: int = 0
    scanned_files: int = 0
    hit_count: int = 0
    error_count: int = 0
    preview_truncated_count: int = 0
    stored_characters: int = 0
    cancelled: bool = False
    limited: bool = False
    fatal: str = ""


def validate_search_options(options: dict) -> SearchOptions:
    if not isinstance(options, dict):
        raise ValueError("检索设置应为字典。")
    query = options.get("query", "")
    if not isinstance(query, str):
        raise ValueError("检索关键词必须为文本。")
    queries = tuple(dict.fromkeys(line.strip() for line in query.splitlines() if line.strip()))
    if not queries:
        raise ValueError("请至少填写一个检索关键词，每行一个。")
    if len(queries) > 100 or sum(map(len, queries)) > 16000:
        raise ValueError("一次最多填写 100 个关键词，关键词合计不超过 16000 字符，请缩小检索条件。")
    mode = options.get("match_mode", "any")
    if mode not in {"any", "all"}:
        raise ValueError("匹配方式必须为任一命中（any）或全部命中（all）。")
    sensitive = options.get("case_sensitive", False)
    recursive = options.get("recursive", True)
    if not isinstance(sensitive, bool) or not isinstance(recursive, bool):
        raise ValueError("区分大小写和包含子文件夹应为开关值。")
    password = options.get("password", "")
    if password is not None and not isinstance(password, str):
        raise ValueError("PDF 打开密码必须为文本。")
    return SearchOptions(queries, mode, sensitive, recursive)


def keyword_ranges(text: str, queries: tuple[str, ...], case_sensitive: bool,
                   limit: int = MAX_HIGHLIGHT_RANGES) -> tuple[tuple[int, int], ...]:
    """Find literal matches with offsets in the original Python string.

    Case folding may expand one character (for example ß -> ss), so offsets in
    the folded string must never be passed directly to a Qt text cursor.
    """
    if not text or limit <= 0:
        return ()
    if case_sensitive:
        haystack, positions = text, None
    else:
        folded, positions = [], []
        for index, char in enumerate(text):
            value = char.casefold()
            folded.append(value)
            positions.extend([index] * len(value))
        haystack = "".join(folded)
    ranges = set()
    for query in queries:
        needle = query if case_sensitive else query.casefold()
        if not needle:
            continue
        offset = 0
        while len(ranges) < limit:
            start = haystack.find(needle, offset)
            if start < 0:
                break
            end = start + len(needle)
            if positions is None:
                ranges.add((start, end))
            else:
                ranges.add((positions[start], positions[end - 1] + 1))
            offset = start + 1
        if len(ranges) >= limit:
            break
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _original_offset(text: str, folded_offset: int) -> int:
    cursor = 0
    for index, character in enumerate(text):
        next_cursor = cursor + len(character.casefold())
        if next_cursor > folded_offset:
            return index
        cursor = next_cursor
    return len(text)


def _preview(text, haystack, matched, sensitive, limit):
    if len(text) <= limit:
        return text, False
    first = min(haystack.find(query if sensitive else query.casefold()) for query in matched)
    original = first if sensitive else _original_offset(text, first)
    start = max(0, original - min(1000, limit // 4))
    end = min(len(text), start + limit)
    return text[start:end], True


def _input_files(sources, recursive, progress, cancel):
    """Reuse the common input policy, checking cancellation between folders."""
    found, seen = [], set()

    def linked(path):
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())

    def add(paths):
        for path in paths:
            ensure_cancel(cancel)
            key = os.path.normcase(str(path))
            if key not in seen:
                seen.add(key)
                found.append(path)
                if len(found) > 50000:
                    raise ValueError("一次最多读取 50000 个文件，请缩小检索范围。")

    for source in sources:
        ensure_cancel(cancel)
        path = Path(source).expanduser()
        # This also rejects a linked root/ancestor or an inaccessible input.
        add(expand_inputs([path], EXTENSIONS, recursive=False))
        if not recursive or not path.is_dir():
            continue

        def walk_error(error):
            raise OSError(f"无法读取文件夹，请检查权限或连接：{error.filename}") from error

        for current, dirs, _files in os.walk(path, followlinks=False, onerror=walk_error):
            ensure_cancel(cancel)
            current_path = Path(current)
            dirs[:] = sorted(name for name in dirs if not linked(current_path / name))
            if current_path != path:
                add(expand_inputs([current_path], EXTENSIONS, recursive=False))
            if progress:
                progress(0, 0, f"正在整理检索范围，已找到 {len(found)} 个支持的文件…")
    return found


def search_documents(sources: list[Path], options: dict, progress: Callable | None = None,
                     cancel: Callable | None = None, on_file: Callable | None = None,
                     *, max_hits: int = MAX_HITS,
                     max_characters: int = MAX_RESULT_CHARACTERS,
                     preview_characters: int = MAX_PREVIEW_CHARACTERS) -> SearchReport:
    """Search without writing reports; return partial results on cancellation.

    ``on_file`` receives each matching file only once, after its read finishes.
    ``progress`` receives (scanned, total, Chinese status); total=0 means the
    supported input list is still being prepared.  Literal keyword matching is
    intentionally used; no user regular expression or HTML is executed.
    """
    settings = validate_search_options(options)
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
           for value in (max_hits, max_characters, preview_characters)):
        raise ValueError("检索结果上限必须为正整数。")
    report = SearchReport(queries=settings.queries)
    needles = settings.queries if settings.case_sensitive else tuple(query.casefold() for query in settings.queries)
    if progress:
        progress(0, 0, "正在整理检索范围…")
    try:
        files = _input_files(sources, settings.recursive, progress, cancel)
        report.total_files = len(files)
        if not files:
            report.warnings.append("所选范围中没有支持检索的文件，请检查文件格式或是否包含子文件夹。")
            return report
        for index, path in enumerate(files, 1):
            ensure_cancel(cancel)
            if progress:
                progress(index - 1, len(files), f"正在检索：{path.name}")
            hits, error, partial, units_seen = [], "", False, 0
            try:
                with contextlib.closing(iter_text_units(path, options)) as units:
                    for location, text in units:
                        ensure_cancel(cancel)
                        units_seen += 1
                        haystack = text if settings.case_sensitive else text.casefold()
                        matched = tuple(original for original, needle in zip(settings.queries, needles) if needle in haystack)
                        if not matched or (settings.match_mode == "all" and len(matched) != len(settings.queries)):
                            continue
                        remaining = max_characters - report.stored_characters
                        if remaining <= 0:
                            report.limited, partial = True, True
                            break
                        preview, truncated = _preview(text, haystack, matched, settings.case_sensitive,
                                                      min(preview_characters, remaining))
                        hit = SearchHit(str(location), preview, matched,
                                        keyword_ranges(preview, matched, settings.case_sensitive), truncated)
                        hits.append(hit)
                        report.hit_count += 1
                        report.stored_characters += len(preview)
                        report.preview_truncated_count += int(truncated)
                        if report.hit_count >= max_hits or report.stored_characters >= max_characters:
                            report.limited, partial = True, True
                            break
            except CancelledError:
                report.cancelled, partial = True, True
            except Exception as exc:
                error, partial = str(exc), bool(hits)
                report.error_count += 1
                if len(report.errors) < MAX_READ_ERRORS:
                    report.errors.append(SearchReadError(path, error))
            report.scanned_files = index
            if hits:
                item = FileSearchResult(path, tuple(hits), partial, error)
                report.files.append(item)
                if on_file:
                    on_file(item)
            if path.suffix.lower() == ".pdf" and units_seen == 0 and not error and not partial:
                if not any("PDF 未提取到文字" in warning for warning in report.warnings):
                    report.warnings.append("部分 PDF 未提取到文字；扫描图片不执行 OCR，请使用带文字层的 PDF。")
            if progress:
                progress(index, len(files), f"已检查 {index}/{len(files)} 个文件，命中 {report.hit_count} 处")
            if report.limited or report.cancelled:
                break
    except CancelledError:
        report.cancelled = True
    if report.limited:
        report.warnings.append(
            f"已达到结果上限（{max_hits} 处或 {max_characters:,} 个预览字符），"
            "当前文件及后续范围可能尚未检查完；请缩小文件夹范围或细化关键词。")
    if report.preview_truncated_count:
        report.warnings.append(
            f"{report.preview_truncated_count} 处长段落只显示命中附近片段，可能未包含全部关键词；"
            "请按来源位置在原文中复核。")
    if report.error_count > len(report.errors):
        report.warnings.append(f"读取失败共 {report.error_count} 个文件，仅显示前 {len(report.errors)} 条详情。")
    return report
