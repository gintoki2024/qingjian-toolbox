"""Conservative, structure-preserving DOCX batch operations.

Only editable ``w:t`` text is rewritten.  A successful masking operation is not
a certification that a document contains no personal or confidential data.
"""

from __future__ import annotations

import errno
import os
import posixpath
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from docx import Document
from docx.opc.oxml import serialize_part_xml
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Cm


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_TEXT = qn("w:t")
_PARAGRAPH = qn("w:p")
_BOUNDARY = "\ufffc"
_MAX_PACKAGE_BYTES = 512 * 1024 * 1024
_MAX_PACKAGE_FILES = 10000

# Candidate patterns intentionally do not require an ID checksum: a typo must
# not make a plausibly sensitive number escape masking. They are not validators.
_PHONE = re.compile(
    r"(?<!\d)(?P<prefix>(?:\+?86|0086)[ \-\u00a0]?)?"
    r"(?P<number>1[3-9]\d(?:[ \-\u00a0]?\d){8})(?!\d)"
)
_ID = re.compile(
    r"(?<![0-9A-Za-z])(?:"
    r"[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]"
    r"|[1-9]\d{7}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}"
    r")(?![0-9A-Za-z])"
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}(?![A-Za-z0-9\-])"
)
_COMMENT_TAGS = {
    qn("w:commentRangeStart"), qn("w:commentRangeEnd"),
    qn("w:commentReference"),
}
_COMMENT_REL_NAMES = {
    "comments", "commentsExtended", "commentsIds", "commentsExtensible",
    "people",
}
_REVISION_TAGS = {
    qn("w:" + name) for name in (
        "ins", "del", "delText", "moveFrom", "moveTo", "moveFromRangeStart",
        "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd", "pPrChange",
        "rPrChange", "sectPrChange", "tblPrChange", "tblGridChange", "trPrChange",
        "tcPrChange", "numberingChange", "cellIns", "cellDel", "cellMerge",
    )
}
_STRUCTURAL_TAGS = {
    qn("w:" + name) for name in (
        "drawing", "pict", "object", "fldChar", "instrText", "footnoteReference",
        "endnoteReference", "sym", "lastRenderedPageBreak", "separator",
        "continuationSeparator", "noBreakHyphen", "softHyphen",
    )
}
_BLANK_PROTECTED_TAGS = _STRUCTURAL_TAGS | _COMMENT_TAGS | {
    qn("w:" + name) for name in (
        "br", "cr", "tab", "sectPr", "bookmarkStart", "bookmarkEnd", "hyperlink",
        "pBdr", "numPr", "pageBreakBefore", "sdt", "txbxContent",
    )
}


@dataclass
class _Rule:
    pattern: re.Pattern
    replacement: str | Callable[[re.Match], str]
    regex_template: bool = False


def _warning(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _boolean(options: dict, key: str, default: bool = False) -> bool:
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"选项 {key} 必须为布尔值。")
    return value


def _string_list(options: dict, key: str) -> list[str]:
    values = options.get(key, [])
    if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) for v in values):
        raise ValueError(f"选项 {key} 必须为文本列表。")
    return list(dict.fromkeys(value for value in values if value.strip()))


def _check_xml_text(value: str) -> None:
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise ValueError("查找或替换内容含有 Word XML 不支持的控制字符。")


def _rules(options: dict) -> tuple[list[_Rule], bool]:
    rules: list[_Rule] = []
    replacements = options.get("replacements", [])
    if not isinstance(replacements, (list, tuple)):
        raise ValueError("replacements 必须为替换规则列表。")
    for item in replacements:
        if not isinstance(item, dict):
            raise ValueError("每条替换规则必须含 find 和 replace。")
        find, replacement = item.get("find"), item.get("replace", "")
        if not isinstance(find, str) or not find:
            raise ValueError("查找内容不能为空。")
        if not isinstance(replacement, str):
            raise ValueError("替换内容必须为文本。")
        _check_xml_text(find)
        _check_xml_text(replacement)
        regex = _boolean(item, "regex")
        try:
            pattern = re.compile(find if regex else re.escape(find))
            if regex:
                pattern.sub(replacement, "")  # Validate group references up front.
        except re.error as exc:
            raise ValueError(f"正则表达式或替换分组无效：{exc}") from exc
        rules.append(_Rule(pattern, replacement, regex))

    mask_id = _boolean(options, "mask_id")
    mask_phone = _boolean(options, "mask_phone")
    mask_email = _boolean(options, "mask_email")
    sensitive = _string_list(options, "custom_sensitive")
    for value in sensitive:
        _check_xml_text(value)
    def literal_replacement(key):
        value = options[key]
        if not isinstance(value, str):
            raise ValueError("脱敏替换内容必须为文字；留空表示删除。")
        _check_xml_text(value)
        return value
    custom_rules = options.get("custom_sensitive_rules", [])
    if not isinstance(custom_rules, list):
        raise ValueError("自定义脱敏规则应为列表。")
    for rule in custom_rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("find"), str) or not rule["find"]:
            raise ValueError("自定义脱敏规则需要填写原内容。")
        replacement = rule.get("replace", "")
        if not isinstance(replacement, str):
            raise ValueError("自定义脱敏替换内容必须为文字。")
        _check_xml_text(rule["find"])
        _check_xml_text(replacement)
        rules.append(_Rule(re.compile(re.escape(rule["find"])), replacement))
    if mask_id and "mask_id_replacement" in options:
        rules.append(_Rule(_ID, literal_replacement("mask_id_replacement")))
    elif mask_id:
        rules.append(_Rule(_ID, lambda match: (
            match[0][:6] + "*" * (len(match[0]) - 10) + match[0][-4:]
            if len(match[0]) == 18 else match[0][:6] + "******" + match[0][-3:]
        )))
    if mask_phone and "mask_phone_replacement" in options:
        rules.append(_Rule(_PHONE, literal_replacement("mask_phone_replacement")))
    elif mask_phone:
        def phone_replacement(match: re.Match) -> str:
            digits = re.sub(r"\D", "", match["number"])
            return (match["prefix"] or "") + digits[:3] + "****" + digits[-4:]
        rules.append(_Rule(_PHONE, phone_replacement))
    if mask_email:
        rules.append(_Rule(_EMAIL, literal_replacement("mask_email_replacement") if "mask_email_replacement" in options else "***@***"))
    if sensitive:
        pattern = re.compile("|".join(re.escape(value) for value in sorted(sensitive, key=len, reverse=True)))
        rules.append(_Rule(pattern, literal_replacement("custom_sensitive_replacement") if "custom_sensitive_replacement" in options else "***"))
    return rules, mask_id or mask_phone or mask_email or bool(sensitive) or bool(custom_rules)


def _xml(data: bytes, name: str):
    if b"<!DOCTYPE" in data.upper():
        raise ValueError("DOCX 含有不受支持的 XML DTD，已停止处理。")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"DOCX 内的 XML 文件损坏：{name}") from exc


def _harmless_custom_xml(root) -> bool:
    """Allow only Word's empty bibliography scaffolding, not arbitrary data."""
    bibliography = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
    datastore = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"
    if root.tag == "{" + bibliography + "}Sources":
        return (len(root) == 0 and not (root.text or "").strip()
                and all((key == "SelectedStyle" and value == "/APA.XSL")
                        or (key == "StyleName" and value == "APA")
                        for key, value in root.attrib.items()))
    if root.tag == "{" + datastore + "}datastoreItem":
        allowed = {"datastoreItem", "schemaRefs", "schemaRef"}
        return all(
            element.tag in {"{" + datastore + "}" + local for local in allowed}
            and not (element.text or "").strip()
            and all((key == "{" + datastore + "}itemID" and bool(re.fullmatch(r"\{[0-9A-Fa-f\-]{36}\}", value)))
                    or (key == "{" + datastore + "}uri" and value == bibliography)
                    for key, value in element.attrib.items())
            for element in root.iter()
        )
    return False


def _inspect_package(source: Path, redact: bool, remove_comments: bool,
                     include_headers: bool, clean_metadata: bool) -> list[str]:
    warnings: list[str] = []
    risks: set[str] = set()
    has_comments = False
    has_headers = False
    has_links = False
    has_media = False
    has_field_codes = False
    try:
        with ZipFile(source) as package:
            infos = package.infolist()
            names = {info.filename for info in infos}
            if len(infos) != len(names):
                raise ValueError("DOCX 内含重复文件名，无法安全处理。")
            if len(infos) > _MAX_PACKAGE_FILES or sum(info.file_size for info in infos) > _MAX_PACKAGE_BYTES:
                raise ValueError("DOCX 解压后的大小超过 512 MB 或文件过多，请拆分后处理。")
            if any(info.flag_bits & 1 for info in infos):
                raise ValueError("不支持加密的 DOCX，请先在 Word 中解除密码。")
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise ValueError("文件不是有效的 DOCX 文档。")
            for info in infos:
                name = info.filename
                lower = name.lower()
                has_media = has_media or lower.startswith("word/media/")
                has_headers = has_headers or bool(re.fullmatch(r"word/(?:header|footer)[^/]*\.xml", lower))
                if lower.startswith("word/embeddings/") or lower.endswith("vbaproject.bin"):
                    risks.add("嵌入文件或对象")
                if lower.startswith("word/glossary/"):
                    risks.add("文档部件库")
                if lower.startswith(("word/charts/", "word/diagrams/")):
                    risks.add("图表或 SmartArt 数据")
                if lower.startswith("_xmlsignatures/"):
                    _warning(warnings, "原文含数字签名，修改后的副本不再保持原签名有效性。")
                if not (lower.endswith(".xml") or lower.endswith(".rels")):
                    continue
                root = _xml(package.read(info), name)
                if (lower.startswith("customxml/") and lower.endswith(".xml")
                        and not _harmless_custom_xml(root)):
                    risks.add("自定义 XML 数据")
                if any(element.tag in _REVISION_TAGS for element in root.iter()):
                    risks.add("尚未接受或拒绝的修订")
                if lower in {"word/footnotes.xml", "word/endnotes.xml"}:
                    if any(element.tag in {_TEXT, qn("w:drawing"), qn("w:pict"), qn("w:object")}
                           for element in root.iter()):
                        risks.add("脚注或尾注")
                if any(element.tag == qn("w:altChunk") for element in root.iter()):
                    risks.add("外部导入内容块")
                if any(element.tag in {qn("w:instrText"), qn("w:fldSimple")} for element in root.iter()):
                    has_field_codes = True
                if any(element.tag == "{http://schemas.openxmlformats.org/officeDocument/2006/math}t"
                       for element in root.iter()):
                    risks.add("公式中的文字")
                if any(element.tag in _COMMENT_TAGS for element in root.iter()):
                    has_comments = True
                if "comments" in posixpath.basename(lower) and lower.startswith("word/"):
                    has_comments = True
                if lower.endswith(".rels"):
                    for rel in root:
                        rel_name = rel.get("Type", "").rsplit("/", 1)[-1]
                        if rel_name in _COMMENT_REL_NAMES:
                            has_comments = True
                        if rel_name == "hyperlink":
                            has_links = True
            if has_comments and not remove_comments:
                risks.add("未删除的批注")
    except BadZipFile as exc:
        raise ValueError("无法读取 DOCX；文件可能损坏、加密或实际为旧版 DOC。") from exc

    if risks:
        message = "、".join(sorted(risks))
        if redact:
            raise ValueError(
                "为避免遗漏敏感信息，已停止脱敏：文档含有" + message
                + "。请先在 Word 中处理这些内容；批注也可勾选“删除批注”。"
            )
        _warning(warnings, "未完整处理以下内容：" + message + "。请在 Word 中人工核查。")
    if redact:
        _warning(warnings, "自动脱敏仅处理可编辑文字，不能保证完全去除敏感信息；分享前请人工复核。")
        _warning(warnings, "规则覆盖常见中国大陆手机号、15/18 位居民身份证候选号码及常见 ASCII 邮箱；其他号码和特殊写法需另行检查。")
        if has_media:
            _warning(warnings, "文档含图片；图片、扫描文字及图片元数据未做脱敏。")
        if has_links:
            _warning(warnings, "超链接显示文字已纳入处理，但链接目标地址保留，可能仍含个人信息。")
        if has_field_codes:
            _warning(warnings, "域的显示文字已纳入处理，但域代码未修改，可能仍含路径或个人信息；请人工核查。")
        if has_headers and not include_headers:
            _warning(warnings, "本次未处理页眉页脚，其中可能仍含敏感信息。")
        if not clean_metadata:
            _warning(warnings, "未清理文档属性，作者和自定义属性中可能仍含敏感信息。")
    return warnings


def _paragraph_stream(paragraph) -> tuple[str, list[tuple[object | None, int, int]]]:
    chunks: list[str] = []
    spans: list[tuple[object | None, int, int]] = []
    position = 0

    def append(node, value: str) -> None:
        nonlocal position
        chunks.append(value)
        spans.append((node, position, position + len(value)))
        position += len(value)

    def visit(element) -> None:
        for child in element:
            if child.tag == _PARAGRAPH:
                continue  # A textbox paragraph is processed separately exactly once.
            if child.tag == _TEXT:
                append(child, child.text or "")
            elif child.tag in {qn("w:tab"), qn("w:br"), qn("w:cr")}:
                append(None, "\t" if child.tag == qn("w:tab") else "\n")
            elif child.tag in _STRUCTURAL_TAGS:
                append(None, _BOUNDARY)
            else:
                visit(child)

    visit(paragraph)
    return "".join(chunks), spans


def _set_text(node, value: str) -> None:
    """Write text plus real Word breaks/tabs without replacing the owning run."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    pieces = re.split(r"([\n\t])", value)
    node.text = pieces[0]
    if pieces[0][:1].isspace() or pieces[0][-1:].isspace():
        node.set(_XML_SPACE, "preserve")
    else:
        node.attrib.pop(_XML_SPACE, None)
    parent = node.getparent()
    position = parent.index(node) + 1
    for piece in pieces[1:]:
        element = OxmlElement("w:br" if piece == "\n" else "w:tab" if piece == "\t" else "w:t")
        if piece not in {"\n", "\t"}:
            element.text = piece
            if piece[:1].isspace() or piece[-1:].isspace():
                element.set(_XML_SPACE, "preserve")
        parent.insert(position, element)
        position += 1


def _replace(paragraph, rule: _Rule, warnings: list[str]) -> int:
    text, spans = _paragraph_stream(paragraph)
    edits: list[tuple[re.Match, list[tuple[object, int, int]]]] = []
    for match in rule.pattern.finditer(text):
        if match.start() == match.end():
            raise ValueError("不支持匹配空字符串的正则表达式，请使用匹配实际文字的表达式。")
        affected = [(node, start, end) for node, start, end in spans
                    if start < match.end() and end > match.start()]
        if not affected:
            continue
        if any(node is None for node, _start, _end in affected):
            _warning(warnings, "部分匹配跨越图片、域或换行等结构，已跳过以保护原有结构。")
            continue
        edits.append((match, affected))
    updates: dict[object, str] = {}
    count = 0
    for match, affected in reversed(edits):
        replacement = (rule.replacement(match) if callable(rule.replacement)
                       else match.expand(rule.replacement) if rule.regex_template
                       else rule.replacement)
        _check_xml_text(replacement)
        if replacement == match[0]:
            continue
        first, first_start, _first_end = affected[0]
        last, last_start, _last_end = affected[-1]
        start_offset, end_offset = match.start() - first_start, match.end() - last_start
        first_text = updates.get(first, first.text or "")
        last_text = updates.get(last, last.text or "")
        if first is last:
            updates[first] = first_text[:start_offset] + replacement + first_text[end_offset:]
        else:
            updates[first] = first_text[:start_offset] + replacement
            for node, _start, _end in affected[1:-1]:
                updates[node] = ""
            updates[last] = last_text[end_offset:]
        count += 1
    for node, value in updates.items():
        _set_text(node, value)
    return count


def _heading(paragraph, styles: dict) -> bool:
    ppr = paragraph.find(qn("w:pPr"))
    style_id = None
    if ppr is not None:
        outline = ppr.find(qn("w:outlineLvl"))
        if outline is not None and outline.get(qn("w:val"), "9") != "9":
            return True
        pstyle = ppr.find(qn("w:pStyle"))
        style_id = pstyle.get(qn("w:val")) if pstyle is not None else None
    seen: set[str] = set()
    while style_id and style_id not in seen:
        seen.add(style_id)
        style = styles.get(style_id)
        if re.match(r"^(?:Heading\s*\d*|Title|Subtitle|标题.*)$", style_id, re.IGNORECASE):
            return True
        if style is None:
            break
        if re.match(r"^(?:heading\s*\d*|title|subtitle|标题.*)$", style.name or "", re.IGNORECASE):
            return True
        spr = style.element.find(qn("w:pPr"))
        outline = spr.find(qn("w:outlineLvl")) if spr is not None else None
        if outline is not None and outline.get(qn("w:val"), "9") != "9":
            return True
        style_id = style.base_style.style_id if style.base_style is not None else None
    return False


def _format_paragraph(paragraph, font: str, size: float, spacing: float) -> int:
    changes = 0
    ppr = paragraph.get_or_add_pPr()
    space = ppr.get_or_add_spacing()
    values = {qn("w:line"): str(round(spacing * 240)), qn("w:lineRule"): "auto"}
    if any(space.get(key) != value for key, value in values.items()):
        space.attrib.update(values)
        changes += 1
    _text, spans = _paragraph_stream(paragraph)
    runs = set()
    for node, _start, _end in spans:
        if node is None:
            continue
        run = node.getparent()
        if run.tag == qn("w:r"):
            runs.add(run)
    for run in runs:
        rpr = run.get_or_add_rPr()
        fonts = rpr.get_or_add_rFonts()
        changed = False
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            name = qn("w:" + attr)
            if fonts.get(name) != font:
                fonts.set(name, font)
                changed = True
            if fonts.attrib.pop(qn("w:" + attr + "Theme"), None) is not None:
                changed = True
        if fonts.attrib.pop(qn("w:cstheme"), None) is not None:
            changed = True
        size_element = rpr.get_or_add_sz()
        complex_size = rpr.find(qn("w:szCs"))
        if complex_size is None:
            complex_size = OxmlElement("w:szCs")
            rpr.insert(rpr.index(size_element) + 1, complex_size)
        for element in (size_element, complex_size):
            if element.get(qn("w:val")) != str(round(size * 2)):
                element.set(qn("w:val"), str(round(size * 2)))
                changed = True
        changes += int(changed)
    return changes


def _blank(paragraph) -> bool:
    text, _spans = _paragraph_stream(paragraph)
    return not text.strip() and not any(element.tag in _BLANK_PROTECTED_TAGS for element in paragraph.iter())


def _attached(element, root) -> bool:
    return element is root or any(parent is root for parent in element.iterancestors())


def _delete_paragraph(paragraph, warnings: list[str]) -> None:
    ppr = paragraph.find(qn("w:pPr"))
    if ppr is not None and ppr.find(qn("w:sectPr")) is not None:
        for child in list(paragraph):
            if child is not ppr:
                paragraph.remove(child)
        _warning(warnings, "删除命中的分节段落时保留了分节属性和空段，以维持页面结构。")
    else:
        paragraph.getparent().remove(paragraph)


def _ensure_required_paragraphs(root) -> None:
    for cell in root.iter(qn("w:tc")):
        if len(cell) == 0 or cell[-1].tag != _PARAGRAPH:
            cell.append(OxmlElement("w:p"))
    if root.tag in {qn("w:hdr"), qn("w:ftr")} and not len(root):
        root.append(OxmlElement("w:p"))
    body = root.find(qn("w:body"))
    if body is not None and not any(child.tag in {_PARAGRAPH, qn("w:tbl")} for child in body):
        body.insert(0, OxmlElement("w:p"))


def _rel_target(rels_name: str, target: str) -> str:
    target = unquote(target).replace("\\", "/")
    if target.startswith("/"):
        return posixpath.normpath(target).lstrip("/")
    folder = rels_name.rsplit("/_rels/", 1)[0] if "/_rels/" in rels_name else ""
    return posixpath.normpath(posixpath.join(folder, target)).lstrip("/")


def _comment_dependencies(removed: set[str], xmls: dict) -> set[str]:
    """Remove comment-only images/attachments while retaining shared media."""
    graph: dict[str, set[str]] = {}
    relationship_parts: dict[str, str] = {}
    for name, root in xmls.items():
        if not name.endswith(".rels"):
            continue
        if name == "_rels/.rels":
            owner = ""
        elif "/_rels/" in name:
            folder, filename = name.rsplit("/_rels/", 1)
            owner = posixpath.join(folder, filename.removesuffix(".rels"))
        else:
            continue
        relationship_parts[owner] = name
        graph[owner] = {
            _rel_target(name, rel.get("Target", "")) for rel in root
            if rel.get("TargetMode") != "External" and rel.get("Target")
        }

    def descendants(start: set[str], skip: set[str]) -> set[str]:
        visited: set[str] = set()
        stack = list(start)
        while stack:
            current = stack.pop()
            if current in visited or current in skip:
                continue
            visited.add(current)
            stack.extend(graph.get(current, set()) - visited)
        return visited

    candidates = descendants(removed, set())
    retained = descendants({""}, removed)
    result = removed | (candidates - retained)
    result.update(relationship_parts[name] for name in tuple(result) if name in relationship_parts)
    return result


def _package_cleanup(path: Path, remove_comments: bool, clean_metadata: bool) -> int:
    if not (remove_comments or clean_metadata):
        return 0
    temp: Path | None = None
    changes = 0
    try:
        with ZipFile(path) as source:
            names = set(source.namelist())
            removed: set[str] = set()
            xmls: dict[str, object] = {}
            for name in names:
                if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml":
                    # python-docx's lxml parser retains namespace declarations.
                    # ElementTree would drop declarations referenced only by
                    # mc:Ignorable and can make otherwise valid DOCX unreadable.
                    xmls[name] = parse_xml(source.read(name))
            if remove_comments:
                for name in names:
                    if name.startswith("word/") and posixpath.basename(name).lower().startswith(
                        ("comments", "people")
                    ) and name.endswith(".xml"):
                        removed.add(name)
                for name, root in xmls.items():
                    if name.endswith(".rels"):
                        for rel in root:
                            if rel.get("Type", "").rsplit("/", 1)[-1] in _COMMENT_REL_NAMES:
                                removed.add(_rel_target(name, rel.get("Target", "")))
                removed = _comment_dependencies(removed, xmls)
                changes += len(removed & names)
            changed_xmls: dict[str, bytes] = {}
            for name, root in xmls.items():
                if name in removed:
                    continue
                changed = False
                if remove_comments:
                    for parent in root.iter():
                        for child in list(parent):
                            if child.tag in _COMMENT_TAGS:
                                parent.remove(child)
                                changes += 1
                                changed = True
                    if name.endswith(".rels"):
                        for rel in list(root):
                            if (rel.get("Type", "").rsplit("/", 1)[-1] in _COMMENT_REL_NAMES
                                    or (rel.get("TargetMode") != "External"
                                        and _rel_target(name, rel.get("Target", "")) in removed)):
                                root.remove(rel)
                                changed = True
                                changes += 1
                    elif name == "[Content_Types].xml":
                        for content_type in list(root):
                            if content_type.get("PartName", "").lstrip("/") in removed:
                                root.remove(content_type)
                                changed = True
                                changes += 1
                if clean_metadata and name in {"docProps/core.xml", "docProps/custom.xml"}:
                    if len(root) or root.text or root.attrib:
                        for child in list(root):
                            root.remove(child)
                        root.text = None
                        root.attrib.clear()
                        changed = True
                        changes += 1
                elif clean_metadata and name == "docProps/app.xml":
                    for child in list(root):
                        if child.tag.rsplit("}", 1)[-1] in {"Company", "Manager", "HyperlinkBase", "Template"}:
                            if child.text or len(child):
                                child.text = None
                                for descendant in list(child):
                                    child.remove(descendant)
                                changed = True
                                changes += 1
                if changed:
                    changed_xmls[name] = serialize_part_xml(root)
            if not removed and not changed_xmls:
                return changes
            descriptor, filename = tempfile.mkstemp(prefix=".qingjian-clean-", suffix=".docx", dir=path.parent)
            os.close(descriptor)
            temp = Path(filename)
            with ZipFile(temp, "w", ZIP_DEFLATED) as target:
                for info in source.infolist():
                    if info.filename not in removed:
                        target.writestr(info, changed_xmls.get(info.filename, source.read(info)))
        # Both paths are private staging files. Windows scanners may briefly
        # hold a just-written DOCX without sharing delete access.
        for attempt in range(5):
            try:
                os.replace(temp, path)
                break
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(0.05 * (2 ** attempt))
        temp = None
        return changes
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _publish_without_overwrite(temp: Path, target: Path) -> None:
    try:
        os.link(temp, target)
    except FileExistsError:
        raise FileExistsError("输出文件已存在，为保护既有文件已停止处理。") from None
    except OSError as exc:
        # Same-directory rename on Windows is atomic and, unlike POSIX rename,
        # refuses an existing destination. It also supports filesystems without
        # hard-link support, such as a removable FAT/exFAT drive.
        if os.name == "nt" and (exc.errno in {errno.EPERM, errno.EACCES, errno.ENOTSUP}
                                or getattr(exc, "winerror", None) in {1, 5, 50}):
            os.rename(temp, target)
        else:
            raise
    temp.unlink(missing_ok=True)


def process_word(source: Path, target: Path, options: dict) -> dict:
    """Create a new processed DOCX and return non-sensitive audit counts.

    ``changes`` counts changed text matches, deleted paragraphs, formatting
    groups and package cleanup items. Existing source and target files are never
    overwritten. Errors leave neither a partial output nor temporary documents.
    """
    source, target = Path(source), Path(target)
    if not isinstance(options, dict):
        raise ValueError("处理选项必须为字典。")
    if source.suffix.lower() != ".docx" or target.suffix.lower() != ".docx":
        raise ValueError("本模块仅支持 DOCX；旧版 DOC 请先转换为 DOCX。")
    if not source.is_file():
        raise FileNotFoundError("未找到待处理的 DOCX 文件。")
    if source.resolve() == target.resolve():
        raise ValueError("输出路径不能与原文件相同。")
    if os.path.lexists(target):
        raise FileExistsError("输出文件已存在，为保护既有文件已停止处理。")
    rules, redact = _rules(options)
    remove_comments = _boolean(options, "remove_comments")
    clean_metadata = _boolean(options, "clean_metadata")
    include_headers = _boolean(options, "include_headers", True)
    official_enabled = _boolean(options, "official_enabled")
    official_options = None
    if official_enabled:
        from .official_ops import validate_official_options
        official_options = validate_official_options(options.get("official", {}))
    # The official preset owns page and paragraph layout. Other Word actions
    # still run before it, including redaction, replacement and package cleanup.
    format_enabled = _boolean(options, "format_enabled") and not official_enabled
    preserve_headings = _boolean(options, "preserve_headings", True)
    remove_blanks = _boolean(options, "remove_blank_paragraphs")
    delete_terms = _string_list(options, "delete_paragraphs")
    for term in delete_terms:
        _check_xml_text(term)
    font = options.get("font_name", "宋体")
    if not isinstance(font, str) or not font.strip():
        raise ValueError("字体名称不能为空。")
    try:
        size = float(options.get("font_size", 12))
        spacing = float(options.get("line_spacing", 1.5))
        margin = float(options.get("margin_cm", 2.54))
    except (TypeError, ValueError) as exc:
        raise ValueError("字号、行距和页边距必须为数字。") from exc
    if not (1 <= size <= 400 and 0.5 <= spacing <= 10 and 0 <= margin <= 20):
        raise ValueError("字号应为 1–400 磅，行距 0.5–10 倍，页边距 0–20 厘米。")
    warnings = _inspect_package(source, redact, remove_comments, include_headers, clean_metadata)
    document = Document(source)
    changes = 0
    if format_enabled:
        margin_value = Cm(margin)
        for section in document.sections:
            if ((section.page_width and 2 * margin_value >= section.page_width)
                    or (section.page_height and 2 * margin_value >= section.page_height)):
                raise ValueError("页边距过大，将使该文档没有可用正文区域。")
            attrs = ("top_margin", "bottom_margin", "left_margin", "right_margin")
            if any(getattr(section, attr) != margin_value for attr in attrs):
                for attr in attrs:
                    setattr(section, attr, margin_value)
                changes += 1
    roots = [document.element]
    if include_headers:
        for relation in document.part.rels.values():
            if not relation.is_external and relation.reltype.rsplit("/", 1)[-1] in {"header", "footer"}:
                root = relation.target_part.element
                if all(root is not current for current in roots):
                    roots.append(root)
    styles = {style.style_id: style for style in document.styles}
    for root in roots:
        for paragraph in list(root.iter(_PARAGRAPH)):
            if not _attached(paragraph, root):
                continue
            text, _spans = _paragraph_stream(paragraph)
            if any(term in text for term in delete_terms):
                _delete_paragraph(paragraph, warnings)
                changes += 1
                continue
            for rule in rules:
                changes += _replace(paragraph, rule, warnings)
            if remove_blanks and _blank(paragraph):
                _delete_paragraph(paragraph, warnings)
                changes += 1
                continue
            if format_enabled and not (preserve_headings and _heading(paragraph, styles)):
                changes += _format_paragraph(paragraph, font.strip(), size, spacing)
        _ensure_required_paragraphs(root)
    official_result = None
    if official_enabled:
        from .official_ops import apply_official_format
        official_result = apply_official_format(document, official_options)
        changes += official_result.get("changes", 0)
        for message in official_result.get("warnings", []):
            _warning(warnings, message)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        descriptor, filename = tempfile.mkstemp(prefix=".qingjian-word-", suffix=".docx", dir=target.parent)
        os.close(descriptor)
        temp = Path(filename)
        document.save(temp)
        changes += _package_cleanup(temp, remove_comments, clean_metadata)
        with ZipFile(temp) as package:
            if package.testzip() is not None:
                raise ValueError("输出文件完整性检查失败，未生成结果。")
        # Reopening catches damaged relationships before an output becomes visible.
        Document(temp)
        with temp.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        _publish_without_overwrite(temp, target)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    result = {"changes": changes, "warnings": warnings}
    if official_result is not None:
        result["audit"] = official_result.get("audit", {})
    return result
