"""Lossless decoding and safe UTF-8 output for local TXT/Markdown rules."""
from __future__ import annotations

import codecs
import os
from pathlib import Path

from .common import atomic_write
from .word_ops import _boolean, _rules, _string_list


TEXT_EXTENSIONS = {".txt", ".md"}
MAX_TEXT_BYTES = 50 * 1024 * 1024


def _ordinary_path(path: Path):
    for item in (path.absolute(), *path.absolute().parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("请选择实际文件或目录，不读取或写入符号链接、目录联接。")


def read_text_file(source: Path, warnings: list | None = None) -> str:
    """Decode supported text encodings strictly, never discard unknown bytes."""
    source = Path(source)
    _ordinary_path(source)
    if not source.is_file():
        raise FileNotFoundError(f"找不到文本文件：{source.name}")
    if source.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("单个文本文件不能超过 50 MB，请拆分后处理。")
    with source.open("rb") as stream:
        raw = stream.read(MAX_TEXT_BYTES + 1)
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError("单个文本文件不能超过 50 MB，请拆分后处理。")
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise ValueError("当前不支持 UTF-32，请将文件另存为 UTF-8 后处理。")
    candidates = ("utf-16",) if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else ("utf-8-sig", "gb18030")
    for encoding in candidates:
        try:
            text = raw.decode(encoding)
        except UnicodeError:
            continue
        if "\x00" in text:
            raise ValueError("文本中含有空字符，可能是无 BOM 的 UTF-16 或二进制文件；请另存为 UTF-8 后处理。")
        if warnings is not None and encoding != "utf-8-sig":
            warnings.append(f"{source.name} 按 {encoding.upper()} 读取，结果使用 UTF-8 保存。")
        return text
    raise ValueError("无法按 UTF-8、带 BOM 的 UTF-16 或 GB18030 识别文本；未忽略任何无法解码的字节。")


def process_text(source: Path, target: Path, options: dict) -> dict:
    """Apply text rules to a new UTF-8 file; source and existing outputs stay intact.

    Changes count matched replacements and removed lines, matching Word's
    operation-count convention. Literal replacement values never expand regex
    escapes; only explicit regex replacement rules may use capture groups.
    """
    if not isinstance(options, dict):
        raise ValueError("文本处理选项应为字段和值组成的对象。")
    if _boolean(options, "official_enabled"):
        raise ValueError("TXT/Markdown 不支持公文排版，请取消公文排版后使用替换、脱敏或删除行功能。")
    try:
        rules, _redact = _rules(options)
    except IndexError as exc:
        raise ValueError("正则替换引用了不存在的命名分组，请核对替换规则。") from exc
    delete_terms = _string_list(options, "delete_paragraphs")
    remove_blanks = _boolean(options, "remove_blank_paragraphs")
    if not (rules or delete_terms or remove_blanks):
        raise ValueError("未选择可用于 TXT/Markdown 的操作；请选择替换、脱敏、删除指定行或删除空白行。字体与页边距不适用于文本文件。")

    source, target = Path(source), Path(target)
    _ordinary_path(source)
    _ordinary_path(target)
    if source.suffix.lower() not in TEXT_EXTENSIONS or target.suffix.lower() not in TEXT_EXTENSIONS:
        raise ValueError("文本批量处理仅支持 .txt 和 .md 文件，请保留文本扩展名。")
    if source.resolve() == target.resolve():
        raise ValueError("输出路径不能与原文件相同。")
    if os.path.lexists(target):
        raise FileExistsError("输出文件已存在，未覆盖既有文件。")
    warnings = []
    text = read_text_file(source, warnings)
    changes = 0
    if delete_terms:
        retained = []
        for line in text.splitlines(keepends=True):
            if any(term in line for term in delete_terms):
                changes += 1
            else:
                retained.append(line)
        text = "".join(retained)
    for rule in rules:
        replacement = rule.replacement
        if not callable(replacement) and not rule.regex_template:
            replacement = lambda _match, value=replacement: value
        text, count = rule.pattern.subn(replacement, text)
        changes += count
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("规则替换后的文本超过 50 MB，未保存结果，请缩小处理范围。")
    if remove_blanks:
        retained = []
        for line in text.splitlines(keepends=True):
            if not line.strip():
                changes += 1
            else:
                retained.append(line)
        text = "".join(retained)
    if options.get("format_enabled"):
        warnings.append("TXT/Markdown 没有字体、字号或页边距，已仅执行所选文字操作。")
    atomic_write(target, lambda staged: staged.write_bytes(text.encode("utf-8")))
    return {"changes": changes, "warnings": warnings}
