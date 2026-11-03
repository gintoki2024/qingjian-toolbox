"""Fill literal column placeholders in PPTX text without rebuilding the deck.

Only DrawingML paragraphs in slides, layouts, masters and notes are edited.
Other package members (including images, charts and embedded objects) are
copied unchanged.  This module does not evaluate template expressions.
"""
from __future__ import annotations

import posixpath
import re
import shutil
import zipfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote

from lxml import etree


_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_STRICT_P = "http://purl.oclc.org/ooxml/presentationml/main"
_STRICT_A = "http://purl.oclc.org/ooxml/drawingml/main"
_STRICT_R = "http://purl.oclc.org/ooxml/officeDocument/relationships"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_FIELDS = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_SIMPLE_NAME = re.compile(r"^[\w\u3000-\u303f\uff00-\uffef ]+$", re.UNICODE)
_PART = re.compile(
    r"ppt/(?:slides|slideLayouts|slideMasters|notesSlides|notesMasters)/[^/]+\.xml"
)
_MARKERS = ("{{", "}}", "{%", "%}", "{#", "#}")
_BOUNDARY = "\ufffc"


def _xml(data, name):
    if len(data) > 64 * 1024 * 1024 or b"<!DOCTYPE" in data.upper():
        raise ValueError(f"PPTX 模板 XML 过大或包含不允许的 DTD：{name}")
    try:
        root = etree.fromstring(data, etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False,
        ))
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"PPTX 模板 XML 损坏：{name}") from exc
    if root.getroottree().docinfo.doctype or any(isinstance(node, etree._Entity) for node in root.iter()):
        raise ValueError(f"PPTX 模板 XML 包含不允许的实体或 DTD：{name}")
    return root


def _part_target(base, target):
    target = unquote(target)
    if "\\" in target or "?" in target or "#" in target:
        raise ValueError("PPTX 幻灯片关系路径无效。")
    return posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))


def _package(archive):
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(entries) > 10000 or sum(entry.file_size for entry in entries) > 512 * 1024 * 1024:
        raise ValueError("PPTX 模板文件过大，或包含过多内部文件。")
    if len(set(names)) != len(names):
        raise ValueError("PPTX 模板包含重名内部文件。")
    if any("\\" in name or name.startswith("/") or ".." in name.split("/") for name in names):
        raise ValueError("PPTX 模板包含无效内部文件路径。")
    required = {"[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"}
    if not required.issubset(names):
        raise ValueError("模板不是有效的 PPTX 文件，缺少演示文稿或关系文件。")
    if any(name.startswith("_xmlsignatures/") or name.lower().endswith("vbaproject.bin") for name in names):
        raise ValueError("PPTX 模板含数字签名或宏，请先另存为不含签名和宏的 PPTX 副本。")
    types = _xml(archive.read("[Content_Types].xml"), "[Content_Types].xml")
    main_types = [node.get("ContentType") for node in types.findall("{" + _CT + "}Override")
                  if unquote(node.get("PartName", "")) == "/ppt/presentation.xml"]
    if main_types != [_CONTENT_TYPE]:
        raise ValueError("模板不是普通 PPTX 演示文稿，请在 PowerPoint 中另存为 .pptx。")
    package_links = _xml(archive.read("_rels/.rels"), "_rels/.rels")
    documents = [link for link in package_links.findall("{" + _REL + "}Relationship")
                 if link.get("Type", "").endswith("/officeDocument")]
    if len(documents) != 1 or documents[0].get("TargetMode") == "External" or _part_target("", documents[0].get("Target", "")) != "ppt/presentation.xml":
        raise ValueError("PPTX 文件入口关系无效。")
    main = _xml(archive.read("ppt/presentation.xml"), "ppt/presentation.xml")
    if main.tag not in {"{" + _P + "}presentation", "{" + _STRICT_P + "}presentation"}:
        raise ValueError("PPTX 演示文稿根节点无效。")
    presentation_ns = etree.QName(main).namespace
    drawing_ns = _A if presentation_ns == _P else _STRICT_A
    relationship_ns = _R if presentation_ns == _P else _STRICT_R
    relationships = _xml(archive.read("ppt/_rels/presentation.xml.rels"), "ppt/_rels/presentation.xml.rels")
    links = {}
    for link in relationships.findall("{" + _REL + "}Relationship"):
        key = link.get("Id")
        if not key or key in links:
            raise ValueError("PPTX 演示文稿关系编号无效或重名。")
        links[key] = link
    slides = main.findall("{" + presentation_ns + "}sldIdLst/{" + presentation_ns + "}sldId")
    if not slides:
        raise ValueError("PPTX 模板没有幻灯片。")
    seen = set()
    for slide in slides:
        key = slide.get("{" + relationship_ns + "}id")
        link = links.get(key)
        if key in seen or link is None or not link.get("Type", "").endswith("/slide") or link.get("TargetMode") == "External":
            raise ValueError("PPTX 幻灯片关系无效。")
        seen.add(key)
        target = _part_target("ppt", link.get("Target", ""))
        if target not in names:
            raise ValueError("PPTX 幻灯片关系指向缺失的文件。")
        slide_root = _xml(archive.read(target), target)
        if slide_root.tag != "{" + presentation_ns + "}sld":
            raise ValueError("PPTX 幻灯片根节点无效。")
    return drawing_ns


def _field_name(value):
    name = value.strip()
    if not name or not _SIMPLE_NAME.fullmatch(name):
        raise ValueError(f"不支持的模板字段“{name}”：只允许列名，不允许 Jinja 代码、属性访问或表达式。")
    return name


def _fields(text, name):
    if any(marker in text for marker in ("{%", "%}", "{#", "#}")):
        raise ValueError(f"PPTX 模板只支持 {{{{列名}}}}，不支持 Jinja 代码、循环、条件或注释：{name}")
    matches = list(_FIELDS.finditer(text))
    if any(_BOUNDARY in match.group(1) for match in matches):
        raise ValueError(f"PPTX 占位符跨越换行或动态字段，无法填充：{name}")
    residue = _FIELDS.sub("", text)
    if "{{" in residue or "}}" in residue:
        raise ValueError(f"PPTX 占位符没有正确闭合，或跨越换行/动态字段，请使用 {{{{列名}}}}：{name}")
    return {_field_name(match.group(1)) for match in matches}


def _paragraph_stream(paragraph, namespace):
    pieces, spans, position = [], [], 0
    run_tag, text_tag = "{" + namespace + "}r", "{" + namespace + "}t"
    for child in paragraph:
        if child.tag in {"{" + namespace + "}br", "{" + namespace + "}fld"}:
            pieces.append(_BOUNDARY)
            position += 1
        elif child.tag == run_tag:
            for node in child.findall(text_tag):
                value = node.text or ""
                pieces.append(value)
                spans.append((node, position, position + len(value)))
                position += len(value)
    return "".join(pieces), spans


def _has_markers(text):
    return any(marker in text for marker in _MARKERS)


def _roots(template):
    roots, fields = {}, set()
    with zipfile.ZipFile(template) as archive:
        namespace = _package(archive)
        paragraph_tag = "{" + namespace + "}p"
        for entry in archive.infolist():
            name = entry.filename
            if not name.startswith("ppt/") or not name.endswith(".xml"):
                continue
            root = _xml(archive.read(entry), name)
            if not _PART.fullmatch(name):
                if _has_markers("".join(root.itertext())) or any(
                    _has_markers(value) for node in root.iter() for value in node.attrib.values()
                ):
                    raise ValueError(f"发现无法填充的 PPTX 占位符：{name}；图表、SmartArt、嵌入对象等内容请改用普通文本框。")
                continue
            allowed = set()
            for paragraph in root.iter(paragraph_tag):
                text, spans = _paragraph_stream(paragraph, namespace)
                fields.update(_fields(text, name))
                allowed.update(node for node, _, _ in spans)
            # Fields such as automatic slide numbers must retain their IDs and
            # semantics.  A user placeholder inside them is reported, not erased.
            unhandled = "".join(node.text or "" for node in root.iter() if node not in allowed)
            if _has_markers(unhandled) or any(
                _has_markers(value) for node in root.iter() for value in node.attrib.values()
            ):
                raise ValueError(f"发现无法填充的 PPTX 动态字段或非正文占位符：{name}；请改用普通文本框。")
            roots[name] = root
    if not fields:
        raise ValueError("PPTX 模板中未找到 {{列名}} 占位符。图片内文字、图表和 SmartArt 不支持模板填充。")
    return roots, fields, namespace


def template_fields(template: Path) -> set[str]:
    """Validate a PPTX template and list the literal data-column names."""
    return _roots(template)[1]


def _valid_text(value):
    return all(character in "\t\n\r" or 0x20 <= ord(character) <= 0xD7FF
               or 0xE000 <= ord(character) <= 0xFFFD or 0x10000 <= ord(character) <= 0x10FFFF
               for character in value)


def _line_breaks(node, namespace):
    value = node.text or ""
    if "\n" not in value and "\r" not in value:
        return
    parts = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    node.text = parts[0]
    node.set(_SPACE, "preserve")
    run = node.getparent()
    previous = run
    properties = run.find("{" + namespace + "}rPr")
    for part in parts[1:]:
        line_break = etree.Element("{" + namespace + "}br")
        if properties is not None:
            line_break.append(deepcopy(properties))
        following = etree.Element(run.tag, dict(run.attrib))
        if properties is not None:
            following.append(deepcopy(properties))
        text = etree.SubElement(following, "{" + namespace + "}t")
        text.set(_SPACE, "preserve")
        text.text = part
        previous.addnext(line_break)
        line_break.addnext(following)
        previous = following


def fill_template(template: Path, target: Path, values: dict[str, str]) -> int:
    """Write a new PPTX, preserving every unedited package member's bytes."""
    template, target = Path(template), Path(target)
    if template.resolve() == target.resolve() or target.exists():
        raise ValueError("PPTX 输出必须为新文件，不能覆盖模板或已有文件。")
    roots, _, namespace = _roots(template)
    changed_parts, changes = {}, 0
    for name, original in roots.items():
        root, part_changes = deepcopy(original), 0
        for paragraph in root.iter("{" + namespace + "}p"):
            text, spans = _paragraph_stream(paragraph, namespace)
            edited = set()
            for match in reversed(list(_FIELDS.finditer(text))):
                replacement = values.get(_field_name(match.group(1)), "")
                if not isinstance(replacement, str) or not _valid_text(replacement):
                    raise ValueError("数据中包含 PPTX 不支持的控制字符或无效文本。")
                affected = [(node, start, end) for node, start, end in spans if end > match.start() and start < match.end()]
                for index, (node, start, end) in enumerate(affected):
                    value = node.text or ""
                    left, right = max(0, match.start() - start), min(len(value), match.end() - start)
                    node.text = value[:left] + (replacement if index == 0 else "") + value[right:]
                    node.set(_SPACE, "preserve")
                    edited.add(node)
                part_changes += 1
            for node in edited:
                _line_breaks(node, namespace)
        if part_changes:
            changed_parts[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            changes += part_changes
    created = False
    try:
        with zipfile.ZipFile(template) as source, zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as output:
            created = True
            output.comment = source.comment
            names = source.namelist()
            for entry in source.infolist():
                if entry.filename in changed_parts:
                    output.writestr(entry, changed_parts[entry.filename])
                else:
                    with source.open(entry) as incoming, output.open(entry, "w") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        with zipfile.ZipFile(target) as output:
            if output.namelist() != names or output.testzip() is not None:
                raise ValueError("PPTX 输出完整性校验失败，没有发布此行结果。")
            _package(output)
            for name in changed_parts:
                _xml(output.read(name), name)
    except BaseException:
        # The exclusive create above and the preflight guard ensure this can
        # only remove an incomplete file created by this call.
        if created and target.is_file():
            target.unlink()
        raise
    return changes
