"""Offline Office preflight and narrowly scoped presentation preparation.

Work on OOXML members directly: untouched parts, macros and relationships are
preserved byte for byte. External links and macros are never executed.
"""
from __future__ import annotations

import json
import math
import posixpath
import re
import sys
import zipfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

from .common import (CancelledError, atomic_write, ensure_cancel, expand_inputs,
                     make_result, notify, unique_path, write_report_book)

MAX_MEMBERS = 20000
MAX_BYTES = 512 * 1024 * 1024
MAX_XML = 32 * 1024 * 1024
MAX_FINDINGS = 50000
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REV_WRAPS = {"ins", "del", "moveFrom", "moveTo"}
_REV_PROPS = {"rPrChange", "pPrChange", "tblPrChange", "tblPrExChange", "trPrChange", "tcPrChange", "sectPrChange", "tblGridChange"}
_REV_RANGES = {"moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd"}
_REV_UNSUPPORTED = {"cellIns", "cellDel", "cellMerge", "numberingChange", "customXmlInsRangeStart", "customXmlInsRangeEnd",
                    "customXmlDelRangeStart", "customXmlDelRangeEnd", "customXmlMoveFromRangeStart", "customXmlMoveFromRangeEnd",
                    "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd", "conflictIns", "conflictDel"}
_IDENTITY = {"creator", "lastModifiedBy", "lastPrinted", "Company", "Manager"}


def _field(key, title, kind="text", default="", **kwargs):
    return dict(key=key, title=title, kind=kind, default=default, **kwargs)


TOOL_SPECS = {
    "submission_preflight": dict(title="报送前隐藏信息检查", nav="报送前检查", group="材料核查",
        extensions={".docx", ".xlsx", ".xlsm", ".pptx"},
        description="检查批注、修订、身份属性、隐藏内容、外链和嵌入对象，按选项生成清理副本。",
        note="原件始终保留。隐藏业务内容、外链、宏及自定义属性只报告，不自动删除；不执行宏或访问外链。数字签名文件只检查。复杂结构修订不支持自动接受/拒绝时会跳过该文件。",
        fields=[_field("action", "处理方式", "choice", "inspect", choices=[("只检查并输出清单", "inspect"), ("检查并生成清理副本", "clean")]),
                _field("clear_properties", "清除作者、最后修改人、公司等身份属性", "bool", False, when=("action", ["clean"])),
                _field("remove_comments", "移除批注（含表格备注/线程批注）", "bool", False, when=("action", ["clean"])),
                _field("revision_action", "Word已有修订", "choice", "keep", choices=[("保留修订", "keep"), ("接受已有修订", "accept"), ("拒绝已有修订", "reject")], when=("action", ["clean"])),
                _field("remove_ppt_notes", "清空PPT演讲者备注文字", "bool", False, when=("action", ["clean"])),
                _field("recursive", "包含子文件夹", "bool", True)]),
    "ppt_prepare": dict(title="PPT批量整理与检查", nav="PPT整理检查", group="文档自动化", extensions={".pptx"},
        description="检查字体、显式字号、隐藏页、备注和页面比例，按需统一文本字体、字号并添加页码。",
        note="仅处理普通文本框和表格文字；母版、布局、图表、SmartArt和嵌入对象保留。未指定字体/字号时保持原值；字体是否安装、替换后是否溢出需在PowerPoint中复核。隐藏页保留且参与连续编号。数字签名文件只检查。",
        fields=[_field("action", "处理方式", "choice", "inspect", choices=[("只检查", "inspect"), ("检查并生成整理副本", "prepare")]),
                _field("font_name", "统一字体", "text", "", placeholder="留空保持，例如：微软雅黑", when=("action", ["prepare"])),
                _field("font_size", "统一字号（磅，0表示保持）", "float", 0, minimum=0, maximum=400, decimals=1, when=("action", ["prepare"])),
                _field("min_font_size", "字号检查下限（磅）", "float", 12, minimum=1, maximum=400, decimals=1),
                _field("max_font_size", "字号检查上限（磅）", "float", 40, minimum=1, maximum=400, decimals=1),
                _field("add_page_numbers", "添加或更新青简页码", "bool", False, when=("action", ["prepare"])),
                _field("page_number_start", "起始页码", "int", 1, minimum=0, maximum=1000000, when=("action", ["prepare"])),
                _field("recursive", "包含子文件夹", "bool", True)]),
}

GUIDES = {
    "submission_preflight": dict(intro="在报送前找到文档里不容易看到的信息，先检查再选择清理项。",
        quick="① 添加Word/Excel/PPT　② 先只检查　③ 核对清单后按需生成清理副本。",
        steps=["添加DOCX、XLSX、XLSM或PPTX；旧DOC、XLS、PPT先另存为新格式。", "先使用“只检查”，在结果清单中按文件和内部位置查看批注、修订、隐藏内容、外链等。",
               "需要清理时选择生成副本，分别勾选身份属性、批注、备注；Word修订明确选择保留、接受或拒绝。", "打开清理副本复核；隐藏内容、外链、宏、嵌入对象只报告，原件始终保留。"],
        terms=[("身份属性", "作者、最后修改人、公司、经理等；标题、主题和自定义业务属性保留。"), ("接受/拒绝修订", "接受采用修改后内容；拒绝恢复修改前内容。段落标记、表格结构等复杂修订会明确报错跳过。"),
               ("数字签名", "修改会破坏签名，含签名文件只检查，不生成修改副本。"), ("清单位置", "列出内部XML、段落、单元格或幻灯片位置，不代表完成内容合规审查。")],
        example="练习Word含作者、批注、插入修订和隐藏文字；清理时可以移除作者和批注，同时保留隐藏业务文字。"),
    "ppt_prepare": dict(intro="汇总每页字体与显式字号，检查隐藏页、备注和页面比例，再做有范围的批量整理。",
        quick="① 添加PPTX　② 检查清单　③ 按需填字体/字号、勾选页码后生成副本。",
        steps=["添加PPTX，先以默认“只检查”输出每页检查清单。", "字号范围只核对文件中显式写出的字号；继承主题、母版的文字会标记为继承，需在PowerPoint中复核。",
               "选择整理副本后填写统一字体；字号0表示保持原字号，也可只添加页码。", "打开成稿检查长文本、表格、字体替代和页码遮挡；工具没有执行PowerPoint视觉渲染。"],
        terms=[("处理范围", "普通文本框和表格。母版、布局、图表、SmartArt、嵌入对象不改。"), ("青简页码", "每页右下角新增独立文本框，再次处理会更新本工具页码；已有手工页码不会删除。"),
               ("页面比例", "按整份文件页面宽高汇总；不改变页面比例，以免内容错位。"), ("隐藏页", "只报告，不取消隐藏；参与页码编号。")],
        example="练习PPT有小字号、混用字体、隐藏页和备注。示例仅检查，可选择统一字体和添加页码查看副本。"),
}


def validate(mode, options, sources=None):
    if mode not in TOOL_SPECS or not isinstance(options, dict):
        raise ValueError("工具或选项无效。")
    for item in TOOL_SPECS[mode]["fields"]:
        value = options.get(item["key"], item["default"])
        if item["kind"] == "bool" and not isinstance(value, bool):
            raise ValueError(item["title"] + "应为是或否。")
        if item["kind"] == "choice" and value not in [v for _, v in item["choices"]]:
            raise ValueError(item["title"] + "包含未知选项。")
        if item["kind"] in {"int", "float"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(item["title"] + "应为有效数字。")
            if not item["minimum"] <= value <= item["maximum"] or item["kind"] == "int" and int(value) != value:
                raise ValueError(item["title"] + "超出允许范围。")
        if item["kind"] == "text" and not isinstance(value, str):
            raise ValueError(item["title"] + "应为文字。")
    if mode == "ppt_prepare":
        if options.get("min_font_size", 12) > options.get("max_font_size", 40):
            raise ValueError("字号检查下限不能大于上限。")
        size = options.get("font_size", 0)
        if 0 < size < 1:
            raise ValueError("统一字号应为0或1至400磅。")
        name = options.get("font_name", "").strip()
        if len(name) > 200 or any(ord(c) < 32 for c in name):
            raise ValueError("字体名称过长或包含控制字符。")


def _local(node):
    return etree.QName(node).localname if isinstance(node.tag, str) else ""


def _xml(data, name):
    if len(data) > MAX_XML:
        raise ValueError("内部XML超过32MB：" + name)
    try:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False))
    except etree.XMLSyntaxError as exc:
        raise ValueError("内部XML损坏：" + name) from exc
    if root.getroottree().docinfo.doctype or any(isinstance(n, etree._Entity) for n in root.iter()):
        raise ValueError("不允许XML包含DTD或实体：" + name)
    return root


def _target(owner, value):
    target = unquote(value)
    if "\\" in target or "?" in target or "#" in target or ":" in target:
        raise ValueError("内部关系路径无效。")
    result = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(owner), target))
    if result.startswith("../") or result == "..":
        raise ValueError("内部关系路径超出文档范围。")
    return result


def _owner(rels):
    if rels == "_rels/.rels":
        return ""
    base, leaf = rels.rsplit("/_rels/", 1)
    return base + "/" + leaf[:-5]


class _Package:
    def __init__(self, archive, suffix, cancel=None):
        self.archive, self.cancel = archive, cancel
        self.entries = archive.infolist()
        self.names = {e.filename for e in self.entries}
        self.roots, self.changed, self.deleted = {}, set(), set()
        if len(self.entries) > MAX_MEMBERS or sum(e.file_size for e in self.entries) > MAX_BYTES:
            raise ValueError("Office文件解压后超过512MB或内部文件超过20000项。")
        if len(self.names) != len(self.entries):
            raise ValueError("Office文件含重名内部文件。")
        for e in self.entries:
            name = e.filename
            if e.flag_bits & 1 or "\\" in name or name.startswith("/") or ":" in name or ".." in name.split("/"):
                raise ValueError("Office文件加密或内部路径无效。")
            if name.endswith((".xml", ".rels", ".vml")) and e.file_size > MAX_XML:
                raise ValueError("内部XML超过32MB：" + name)
        main = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".xlsm": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}[suffix]
        if not {"[Content_Types].xml", "_rels/.rels", main}.issubset(self.names):
            raise ValueError("文件不是完整的Office Open XML文档。")
        for e in self.entries:
            ensure_cancel(cancel)
            if e.filename.endswith((".xml", ".rels", ".vml")):
                self.root(e.filename)
        expected = {".docx": "document", ".xlsx": "workbook", ".xlsm": "workbook", ".pptx": "presentation"}[suffix]
        if _local(self.root(main)) != expected:
            raise ValueError("文档类型与扩展名不符。")

    def root(self, name):
        if name not in self.roots:
            self.roots[name] = _xml(self.archive.read(name), name)
        return self.roots[name]

    def edit(self, name):
        self.changed.add(name)
        return self.root(name)

    def delete(self, name):
        if name in self.names:
            self.deleted.add(name)
            rel = posixpath.join(posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels")
            if rel in self.names:
                self.deleted.add(rel)

    @property
    def signed(self):
        return (any(n.lower().startswith("_xmlsignatures/") for n in self.names)
                or any("/digital-signature/" in node.get("Type", "").lower()
                       for name, root in self.roots.items() if name.endswith(".rels") for node in root)
                or any("digital-signature-xmlsignature" in node.get("ContentType", "").lower()
                       for node in self.root("[Content_Types].xml")))

    def write(self, staged):
        with zipfile.ZipFile(staged, "w") as destination:
            destination.comment = self.archive.comment
            for e in self.entries:
                ensure_cancel(self.cancel)
                name = e.filename
                if name in self.deleted:
                    continue
                data = (etree.tostring(self.roots[name], encoding="UTF-8", xml_declaration=True)
                        if name in self.changed else self.archive.read(e))
                destination.writestr(e, data)


def _value(node, key, default=""):
    return next((v for k, v in node.attrib.items() if etree.QName(k).localname == key), default)


def _relationship_id(node):
    return next((value for key, value in node.attrib.items()
                 if key.startswith("{") and etree.QName(key).localname == "id"
                 and etree.QName(key).namespace in {R, "http://purl.oclc.org/ooxml/officeDocument/relationships"}), "")


def _on(node):
    return str(_value(node, "val", "true")).lower() not in {"0", "false", "off", "no"}


def _text(node, limit=800):
    text = "".join(n.text or "" for n in node.iter() if _local(n) in {"t", "delText", "v", "f", "text"})
    return text[:limit] + ("…（清单摘要已截短）" if len(text) > limit else "")


def _where(node, name, paragraph_positions=None):
    location = name
    for parent in [node, *node.iterancestors()]:
        if _local(parent) == "c" and parent.get("r"):
            return location + " / 单元格 " + parent.get("r")
        if _local(parent) == "row" and parent.get("r"):
            return location + " / 行 " + parent.get("r")
        if _local(parent) == "p":
            ordinal = paragraph_positions.get(parent, "?") if paragraph_positions is not None else "?"
            return location + " / 段落 " + str(ordinal)
    return location


def _add(findings, category, location, detail, level="提示"):
    if len(findings) >= MAX_FINDINGS:
        raise ValueError("单个文件检查结果超过50000项，请拆分文件后检查；未伪报检查完整。")
    findings.append(dict(category=category, location=location, detail=str(detail)[:1800], level=level))


def _is_word(node):
    return isinstance(node.tag, str) and etree.QName(node).namespace in {W, "http://purl.oclc.org/ooxml/wordprocessingml/main"}


def _comment_part(name):
    lower = name.lower()
    return (bool(re.fullmatch(r"word/(?:comments[^/]*|people)\.xml", lower))
            or bool(re.fullmatch(r"xl/comments\d*\.xml", lower))
            or lower.startswith(("xl/comments/", "xl/threadedcomments/", "xl/persons/", "ppt/comments/", "ppt/moderncomments/", "ppt/commentauthors/", "ppt/authors/"))
            or lower in {"ppt/commentauthors.xml", "ppt/authors.xml"})


def _inspect(pkg, suffix):
    findings = []
    if pkg.signed:
        _add(findings, "数字签名", "_xmlsignatures", "修改会使原签名失效；本工具只检查该文件，不生成修改副本。", "需核实")
    for name in sorted(pkg.names):
        ensure_cancel(pkg.cancel)
        lower = name.lower()
        if "embeddings/" in lower and not lower.endswith("/"):
            _add(findings, "嵌入对象", name, "嵌入文件只报告，未打开或执行。", "需核实")
        if lower.endswith("vbaproject.bin"):
            _add(findings, "宏", name, "宏原样保留，未执行或验证其安全性。", "需核实")
        if _comment_part(name) and lower.endswith(".xml"):
            root = pkg.root(name)
            count = sum(_local(n) in {"comment", "threadedComment", "cm"} for n in root.iter())
            _add(findings, "批注", name, f"含批注相关内容，记录数约{count}；" + _text(root))
        if name not in pkg.roots:
            continue
        root = pkg.root(name)
        paragraph_positions = {node: index for index, node in enumerate(
            (n for n in root.iter() if _local(n) == "p"), 1)} if lower.startswith("word/") else {}
        if lower.startswith("docprops/"):
            for node in root:
                text = "".join(node.itertext()).strip()
                if text:
                    _add(findings, "身份属性" if _local(node) in _IDENTITY else "文档属性", name + " / " + (_value(node, "name") or _local(node)), text)
        if lower.endswith(".rels"):
            for rel in root:
                if rel.get("TargetMode", "").lower() == "external":
                    _add(findings, "外部链接", _owner(name) + " / 关系 " + rel.get("Id", ""), rel.get("Type", "").rsplit("/", 1)[-1] + "：" + rel.get("Target", ""), "需核实")
        if lower.startswith("word/"):
            for ordinal, node in enumerate(root.iter()):
                if ordinal % 256 == 0:
                    ensure_cancel(pkg.cancel)
                local = _local(node)
                if not _is_word(node):
                    continue
                if local in _REV_WRAPS | _REV_PROPS | _REV_UNSUPPORTED:
                    _add(findings, "Word修订", _where(node, name, paragraph_positions), local + "；作者=" + _value(node, "author") + "；" + _text(node), "需核实")
                if local in {"vanish", "webHidden", "specVanish"} and _on(node):
                    owner = node.getparent()
                    _add(findings, "隐藏文字", _where(node, name, paragraph_positions), "隐藏格式（可能来自样式）；" + _text(owner.getparent() if owner is not None and owner.getparent() is not None else node), "需核实")
                if local == "instrText" and re.search(r"\b(?:INCLUDETEXT|INCLUDEPICTURE|LINK|DDE|DDEAUTO)\b", node.text or "", re.I):
                    _add(findings, "外部域代码", _where(node, name, paragraph_positions), node.text or "", "需核实")
        if lower.startswith("xl/"):
            for ordinal, node in enumerate(root.iter()):
                if ordinal % 256 == 0:
                    ensure_cancel(pkg.cancel)
                local = _local(node)
                if local == "sheet" and node.get("state", "visible") != "visible":
                    _add(findings, "隐藏工作表", name, node.get("name", "") + "（" + node.get("state", "") + "）", "需核实")
                if local in {"row", "col"} and node.get("hidden", "0").lower() in {"1", "true"}:
                    desc = "行" + node.get("r", "") if local == "row" else "列范围" + node.get("min", "") + "-" + node.get("max", "")
                    _add(findings, "隐藏行列", name, desc, "需核实")
                if local == "f" and re.search(r"\[[^\]]+\]|\b(?:HYPERLINK|WEBSERVICE|RTD|DDE)\s*\(", node.text or "", re.I):
                    _add(findings, "外链公式（疑似）", _where(node, name), node.text or "", "需核实")
                if local == "sheetFormatPr" and node.get("zeroHeight", "0").lower() in {"1", "true"}:
                    _add(findings, "隐藏行列", name, "默认行高设为隐藏（zeroHeight）", "需核实")
        if lower.startswith("ppt/slides/") and not lower.endswith(".rels") and _local(root) == "sld" and root.get("show", "1").lower() in {"0", "false"}:
            _add(findings, "隐藏幻灯片", name, "隐藏状态保留。", "需核实")
        if lower.startswith("ppt/notesslides/") and _local(root) == "notes":
            texts = _note_text_nodes(root)
            if any(n.text for n in texts):
                _add(findings, "演讲者备注", name, "".join(n.text or "" for n in texts)[:1200], "需核实")
    return findings


def _remove_node(node):
    parent = node.getparent()
    if parent is not None:
        parent.remove(node)


def _unwrap(node):
    parent = node.getparent()
    if parent is None:
        raise ValueError("修订包裹节点结构无效。")
    index = parent.index(node)
    for child in list(node):
        parent.insert(index, child)
        index += 1
    parent.remove(node)


def _apply_revisions(pkg, action):
    changes = 0
    for name in sorted(pkg.roots):
        if not name.startswith("word/") or name.endswith(".rels"):
            continue
        root = pkg.root(name)
        nodes = [n for n in root.iter() if _is_word(n)]
        for node in nodes:
            local = _local(node)
            parent = node.getparent()
            if local in _REV_UNSUPPORTED or local in {"ins", "del"} and parent is not None and _local(parent) in {"rPr", "trPr", "pPr"}:
                raise ValueError("该文件含段落标记/表格结构等复杂修订，无法可靠自动接受或拒绝：" + name + " / " + local + "。请先在Word中处理。")
            if local in _REV_PROPS and (parent is None or len(node) != 1 or _local(node[0]) != _local(parent)):
                raise ValueError("修订历史属性结构不完整，未生成副本：" + name)
        for node in reversed(nodes):
            local = _local(node)
            if local in _REV_PROPS:
                if action == "reject":
                    parent = node.getparent()
                    previous = deepcopy(node[0])
                    parent.attrib.clear()
                    parent.attrib.update(previous.attrib)
                    parent[:] = list(previous)
                else:
                    _remove_node(node)
                pkg.changed.add(name)
                changes += 1
            elif local in _REV_WRAPS:
                keep = local in ({"ins", "moveTo"} if action == "accept" else {"del", "moveFrom"})
                if keep:
                    if action == "reject":
                        for child in node.iter():
                            if _local(child) in {"delText", "delInstrText"}:
                                child.tag = "{" + etree.QName(child).namespace + "}" + {"delText": "t", "delInstrText": "instrText"}[_local(child)]
                    _unwrap(node)
                else:
                    _remove_node(node)
                pkg.changed.add(name)
                changes += 1
            elif local in _REV_RANGES:
                _remove_node(node)
                pkg.changed.add(name)
                changes += 1
    return changes


def _note_text_nodes(root):
    # Only body placeholders are speaker notes. Slide thumbnails, page numbers,
    # date/footer placeholders and notes-master content are not notes text.
    result = []
    for shape in root.xpath(".//*[local-name()='sp']"):
        placeholders = shape.xpath("./*[local-name()='nvSpPr']/*[local-name()='nvPr']/*[local-name()='ph']")
        if any(p.get("type") == "body" for p in placeholders) or not placeholders:
            result.extend(shape.xpath("./*[local-name()='txBody']//*[local-name()='t']"))
    return result


def _remove_comments(pkg):
    changes = 0
    for name in list(pkg.names):
        if _comment_part(name):
            pkg.delete(name)
            changes += 1
    # VML can also hold form controls: remove only note shapes, never a whole
    # drawing containing unrelated controls or visual content.
    for name in sorted(pkg.roots):
        root = pkg.root(name)
        if name.startswith("xl/") and name.endswith(".vml"):
            removed = False
            for shape in list(root.xpath(".//*[local-name()='shape']")):
                if shape.xpath(".//*[local-name()='ClientData' and @ObjectType='Note']"):
                    _remove_node(shape)
                    changes += 1
                    removed = True
            if removed:
                pkg.changed.add(name)
                if not root.xpath(".//*[local-name()='shape']"):
                    pkg.delete(name)
        if name.startswith("word/"):
            for node in list(root.iter()):
                if _is_word(node) and _local(node) in {"commentRangeStart", "commentRangeEnd", "commentReference"}:
                    _remove_node(node)
                    pkg.changed.add(name)
                    changes += 1
    # Remove only links whose targets were removed, and their matching markup.
    for name in sorted(pkg.roots):
        if not name.endswith(".rels") or name in pkg.deleted:
            continue
        root = pkg.root(name)
        owner = _owner(name)
        for rel in list(root):
            if rel.get("TargetMode", "").lower() == "external":
                continue
            try:
                target = _target(owner, rel.get("Target", "").split("#", 1)[0])
            except ValueError:
                # Unknown non-comment links are retained, never followed. Their
                # syntax must not prevent removal of unrelated known comments.
                continue
            if target not in pkg.deleted:
                continue
            rid = rel.get("Id", "")
            root.remove(rel)
            pkg.changed.add(name)
            if owner in pkg.roots:
                for node in list(pkg.root(owner).iter()):
                    if any(etree.QName(k).localname == "id" and v == rid for k, v in node.attrib.items()):
                        # legacyDrawing and comment relation list items only.
                        if _local(node) in {"legacyDrawing", "comment", "commentRel", "cmRel"}:
                            _remove_node(node)
                            pkg.changed.add(owner)
    types = pkg.root("[Content_Types].xml")
    for node in list(types):
        if unquote(node.get("PartName", "")).lstrip("/") in pkg.deleted:
            types.remove(node)
            pkg.changed.add("[Content_Types].xml")
    return changes


def _clean(pkg, options):
    changes = 0
    if options.get("revision_action", "keep") != "keep":
        changes += _apply_revisions(pkg, options["revision_action"])
    if options.get("clear_properties", False):
        for name in sorted(pkg.roots):
            if not name.lower().startswith("docprops/") or name.lower().endswith("custom.xml"):
                continue
            for node in list(pkg.root(name)):
                if _local(node) in _IDENTITY and node.text:
                    if _local(node) == "lastPrinted":
                        # Optional dateTime properties cannot be an empty
                        # string in OOXML; omit this optional property instead.
                        _remove_node(node)
                    else:
                        node.text = ""
                    pkg.changed.add(name)
                    changes += 1
    if options.get("remove_comments", False):
        changes += _remove_comments(pkg)
    if options.get("remove_ppt_notes", False):
        for name in sorted(pkg.roots):
            if name.startswith("ppt/notesSlides/") and _local(pkg.root(name)) == "notes":
                for node in _note_text_nodes(pkg.root(name)):
                    if node.text:
                        node.text = ""
                        pkg.changed.add(name)
                        changes += 1
    return changes


def _slides(pkg):
    main = pkg.root("ppt/presentation.xml")
    relname = "ppt/_rels/presentation.xml.rels"
    if relname not in pkg.names:
        raise ValueError("PPT缺少幻灯片关系文件。")
    links = {}
    for rel in pkg.root(relname):
        rid = rel.get("Id")
        if not rid or rid in links:
            raise ValueError("PPT幻灯片关系编号无效或重复。")
        links[rid] = rel
    slides = []
    for item in main.xpath("./*[local-name()='sldIdLst']/*[local-name()='sldId']"):
        rel = links.get(_relationship_id(item))
        if rel is None or rel.get("TargetMode", "").lower() == "external" or not rel.get("Type", "").endswith("/slide"):
            raise ValueError("PPT幻灯片关系无效。")
        name = _target("ppt/presentation.xml", rel.get("Target", ""))
        if name not in pkg.names or _local(pkg.root(name)) != "sld" or name in slides:
            raise ValueError("PPT幻灯片缺失、重复或类型无效。")
        slides.append(name)
    if not slides:
        raise ValueError("PPT没有幻灯片。")
    dimensions = main.xpath("./*[local-name()='sldSz']")
    if len(dimensions) != 1:
        raise ValueError("PPT缺少页面尺寸。")
    try:
        width, height = int(dimensions[0].get("cx", "0")), int(dimensions[0].get("cy", "0"))
        if not 1 <= width <= 2147483647 or not 1 <= height <= 2147483647:
            raise ValueError()
    except ValueError as exc:
        raise ValueError("PPT页面尺寸无效。") from exc
    return slides, width, height


def _text_bodies(root):
    return root.xpath(".//*[local-name()='sp']/*[local-name()='txBody'] | .//*[local-name()='graphicFrame']//*[local-name()='tbl']//*[local-name()='tc']/*[local-name()='txBody']")


def _ppt_inspect(pkg, options, findings):
    slides, width, height = _slides(pkg)
    divisor = math.gcd(width, height)
    _add(findings, "页面比例", "ppt/presentation.xml", f"{width // divisor}:{height // divisor}；{width / 914400:.2f}×{height / 914400:.2f}英寸；共{len(slides)}页")
    for index, name in enumerate(slides, 1):
        root, fonts, sizes, inherited = pkg.root(name), set(), set(), 0
        for body in _text_bodies(root):
            for props in body.xpath(".//*[local-name()='rPr' or local-name()='defRPr' or local-name()='endParaRPr']"):
                fonts.update(n.get("typeface") for n in props if _local(n) in {"latin", "ea", "cs"} and n.get("typeface"))
                if props.get("sz"):
                    try:
                        size = int(props.get("sz")) / 100
                    except ValueError as exc:
                        raise ValueError("PPT显式字号无效：" + name) from exc
                    sizes.add(size)
            for run in body.xpath(".//*[local-name()='r' or local-name()='fld']"):
                if not run.xpath("./*[local-name()='rPr' and @sz]"):
                    inherited += 1
        details = "字体=" + ("、".join(sorted(fonts)) or "继承主题/母版") + "；显式字号=" + ("、".join(f"{v:g}" for v in sorted(sizes)) or "无") + f"；未直接指定字号的文字段={inherited}（需结合段落/母版复核）"
        _add(findings, "幻灯片文字", f"第{index}页 / {name}", details)
        outside = [v for v in sorted(sizes) if not options.get("min_font_size", 12) <= v <= options.get("max_font_size", 40)]
        if outside:
            _add(findings, "字号超出检查范围", f"第{index}页 / {name}", "、".join(f"{v:g}磅" for v in outside), "需核实")
        count = len(root.xpath(".//*[local-name()='graphicFrame']//*[local-name()='chart' or local-name()='relIds' or local-name()='oleObj']"))
        if count:
            _add(findings, "复杂图形", f"第{index}页 / {name}", f"{count}个图表/SmartArt/嵌入引用不做字体改写，需人工检查。")
    return slides, width, height


def _props_font(props, namespace, name, size):
    if size:
        props.set("sz", str(round(size * 100)))
    if name:
        for tag in ("latin", "ea", "cs"):
            child = next((n for n in props if _local(n) == tag), None)
            if child is None:
                child = etree.Element("{" + namespace + "}" + tag)
                before = next((i for i, n in enumerate(props) if _local(n) in {"hlinkClick", "hlinkMouseOver", "extLst"} or tag == "latin" and _local(n) in {"ea", "cs", "sym"} or tag == "ea" and _local(n) in {"cs", "sym"} or tag == "cs" and _local(n) == "sym"), len(props))
                props.insert(before, child)
            child.set("typeface", name)


def _page_number(root, width, height, number):
    pns = etree.QName(root).namespace
    ans = A if pns == P else "http://purl.oclc.org/ooxml/drawingml/main"
    tags = lambda ns, name: "{" + ns + "}" + name
    tree = next((n for n in root if _local(n) == "cSld"), None)
    tree = next((n for n in tree if _local(n) == "spTree"), None) if tree is not None else None
    if tree is None:
        raise ValueError("PPT缺少可放置页码的形状树。")
    marker = "Qingjian page number v1"
    existing = root.xpath(".//*[local-name()='sp'][*[local-name()='nvSpPr']/*[local-name()='cNvPr' and @descr=$marker]]", marker=marker)
    if existing:
        shape = existing[0]
        texts = shape.xpath(".//*[local-name()='t']")
        if len(texts) != 1:
            raise ValueError("青简页码已被手工改为复杂结构，请先在PPT中调整。")
        texts[0].text = str(number)
        return
    ids = [int(n.get("id")) for n in root.xpath(".//*[local-name()='cNvPr']") if str(n.get("id", "")).isdigit()]
    shape = etree.SubElement(tree, tags(pns, "sp"))
    nv = etree.SubElement(shape, tags(pns, "nvSpPr"))
    etree.SubElement(nv, tags(pns, "cNvPr"), id=str(max(ids, default=0) + 1), name="青简页码", descr=marker)
    etree.SubElement(nv, tags(pns, "cNvSpPr"), txBox="1")
    etree.SubElement(nv, tags(pns, "nvPr"))
    props = etree.SubElement(shape, tags(pns, "spPr"))
    xfrm = etree.SubElement(props, tags(ans, "xfrm"))
    cx, cy = min(914400, width), min(274320, height)
    etree.SubElement(xfrm, tags(ans, "off"), x=str(max(0, width - cx - 182880)), y=str(max(0, height - cy - 91440)))
    etree.SubElement(xfrm, tags(ans, "ext"), cx=str(cx), cy=str(cy))
    geometry = etree.SubElement(props, tags(ans, "prstGeom"), prst="rect")
    etree.SubElement(geometry, tags(ans, "avLst"))
    etree.SubElement(props, tags(ans, "noFill"))
    line = etree.SubElement(props, tags(ans, "ln"))
    etree.SubElement(line, tags(ans, "noFill"))
    body = etree.SubElement(shape, tags(pns, "txBody"))
    etree.SubElement(body, tags(ans, "bodyPr"), lIns="0", rIns="0", tIns="0", bIns="0", anchor="ctr")
    etree.SubElement(body, tags(ans, "lstStyle"))
    para = etree.SubElement(body, tags(ans, "p"))
    etree.SubElement(para, tags(ans, "pPr"), algn="r")
    run = etree.SubElement(para, tags(ans, "r"))
    rp = etree.SubElement(run, tags(ans, "rPr"), lang="zh-CN", sz="1200")
    fill = etree.SubElement(rp, tags(ans, "solidFill"))
    etree.SubElement(fill, tags(ans, "srgbClr"), val="666666")
    etree.SubElement(run, tags(ans, "t")).text = str(number)


def _ppt_prepare(pkg, options, slides, width, height):
    changes = 0
    font, size = options.get("font_name", "").strip(), options.get("font_size", 0)
    for index, name in enumerate(slides):
        ensure_cancel(pkg.cancel)
        root = pkg.root(name)
        if font or size:
            for body in _text_bodies(root):
                ans = etree.QName(body).namespace
                if ans in {P, "http://purl.oclc.org/ooxml/presentationml/main"}:
                    ans = A if ans == P else "http://purl.oclc.org/ooxml/drawingml/main"
                for paragraph in body.xpath(".//*[local-name()='p']"):
                    ppr = next((n for n in paragraph if _local(n) == "pPr"), None)
                    if ppr is None:
                        ppr = etree.Element("{" + ans + "}pPr")
                        paragraph.insert(0, ppr)
                    default = next((n for n in ppr if _local(n) == "defRPr"), None)
                    if default is None:
                        default = etree.Element("{" + ans + "}defRPr")
                        ext_index = next((i for i, n in enumerate(ppr) if _local(n) == "extLst"), len(ppr))
                        ppr.insert(ext_index, default)
                    _props_font(default, ans, font, size)
                for run in body.xpath(".//*[local-name()='r' or local-name()='fld']"):
                    props = next((n for n in run if _local(n) == "rPr"), None)
                    if props is None:
                        props = etree.Element("{" + ans + "}rPr")
                        run.insert(0, props)
                    _props_font(props, ans, font, size)
                    changes += 1
                for props in body.xpath(".//*[local-name()='endParaRPr']"):
                    _props_font(props, ans, font, size)
            pkg.changed.add(name)
        if options.get("add_page_numbers", False):
            _page_number(root, width, height, options.get("page_number_start", 1) + index)
            pkg.changed.add(name)
            changes += 1
    return changes


def run(mode, sources, folder, options, progress=None, cancel=None):
    validate(mode, options, sources)
    options = {**{f["key"]: f["default"] for f in TOOL_SPECS[mode]["fields"]}, **options}
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=True)
    files = expand_inputs(sources, TOOL_SPECS[mode]["extensions"], recursive=options["recursive"], exclude_root=folder)
    if not files:
        raise ValueError("没有找到支持的Office文件。")
    outputs, warnings, errors, records, rows = [], [], [], [], []
    changes, cancelled = 0, False
    modifying = options["action"] in {"clean", "prepare"}
    if mode == "ppt_prepare":
        warnings.append("字号清单仅统计显式字号，继承主题/母版的最终样式需复核；未执行视觉渲染、字体可用性或溢出检查。")
    try:
        for index, source in enumerate(files, 1):
            ensure_cancel(cancel)
            notify(progress, index - 1, len(files), "检查：" + source.name)
            record = dict(source=str(source), status="成功", output="", findings=[], modifications=0)
            records.append(record)
            try:
                with zipfile.ZipFile(source) as archive:
                    pkg = _Package(archive, source.suffix.lower(), cancel)
                    findings = _inspect(pkg, source.suffix.lower())
                    record["findings"] = findings
                    if mode == "ppt_prepare":
                        slides, width, height = _ppt_inspect(pkg, options, findings)
                    if modifying:
                        if pkg.signed:
                            raise ValueError("文件含数字签名，修改会使签名失效，已保留原件并跳过修改副本。")
                        count = (_clean(pkg, options) if mode == "submission_preflight" else _ppt_prepare(pkg, options, slides, width, height))
                        ensure_cancel(cancel)
                        target = unique_path(folder, source.stem + ("_清理" if mode == "submission_preflight" else "_整理") + source.suffix.lower())
                        atomic_write(target, pkg.write)
                        outputs.append(str(target))
                        record.update(output=str(target), modifications=count)
                        changes += count
                    else:
                        changes += len(findings)
            except CancelledError:
                record["status"] = "已停止"
                raise
            except Exception as exc:
                record.update(status="失败", error=str(exc))
                errors.append(source.name + "：" + str(exc))
            for issue in record["findings"]:
                rows.append([str(source), issue["category"], issue["level"], issue["location"], issue["detail"]])
            notify(progress, index, len(files), "已检查：" + source.name)
    except CancelledError:
        cancelled = True
        warnings.append("任务已停止；已生成的完整副本保留，未保存不完整文件。")
    data_outputs = list(outputs)
    label = "报送前检查" if mode == "submission_preflight" else "PPT整理检查"
    payload = dict(tool=mode, options=options, cancelled=cancelled, records=records, outputs=data_outputs, warnings=warnings, errors=errors)
    report = unique_path(folder, label + "_报告.json")
    atomic_write(report, lambda p: p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"))
    outputs.append(str(report))
    report_book = unique_path(folder, label + "_清单.xlsx")
    summary = [["文件", "状态", "发现项", "修改计数", "副本", "错误/说明"]]
    summary.extend([r["source"], r["status"], len(r["findings"]), r["modifications"], r["output"], r.get("error", "")] for r in records)
    write_report_book(report_book, {"文件摘要": summary, "检查明细": [["来源文件", "类别", "级别", "位置", "说明"], *rows],
                                  "范围说明": [["说明"], ["原件保留；宏不执行、外链不访问；隐藏内容只报告。"], ["清单为原文件检查结果；修改计数为处理的XML结构/文字项数量。"], *[[w] for w in warnings]]})
    outputs.append(str(report_book))
    result = make_result(outputs, changes, warnings, errors)
    result.update(data_outputs=data_outputs, report=str(report), cancelled=cancelled)
    return result


def build_example(mode, folder):
    """Create harmless local examples without touching user input."""
    if mode not in TOOL_SPECS:
        raise ValueError("未知示例。")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if mode == "submission_preflight":
        from docx import Document
        document = Document()
        document.core_properties.author = "示例作者"
        document.add_paragraph("报送前检查练习（不含真实业务信息）")
        paragraph = document.add_paragraph()
        run = paragraph.add_run("这是需人工核实的隐藏文字")
        run.font.hidden = True
        para = document.add_paragraph("原始正文。")
        insertion = etree.SubElement(para._p, "{" + W + "}ins", {"{" + W + "}id": "1", "{" + W + "}author": "示例修改人"})
        child = etree.SubElement(insertion, "{" + W + "}r")
        etree.SubElement(child, "{" + W + "}t").text = "新增文字。"
        if hasattr(document, "add_comment"):
            document.add_comment(document.add_paragraph().add_run("带批注的说明"), text="请核实此处。", author="示例批注人")
        target = unique_path(folder, "报送前检查练习.docx")
        atomic_write(target, document.save)
    else:
        # Reuse the application's own harmless example package; python-pptx is
        # intentionally not required for this feature or for its practice data.
        roots = [Path(__file__).resolve().parents[2], Path(sys.executable).resolve().parent]
        if getattr(sys, "_MEIPASS", None):
            roots.append(Path(sys._MEIPASS))
        template = next((root / "示例材料" / "PPT模板示例.pptx" for root in roots
                         if (root / "示例材料" / "PPT模板示例.pptx").is_file()), None)
        if template is None:
            raise FileNotFoundError("缺少随程序提供的“示例材料/PPT模板示例.pptx”，请使用完整程序目录。")
        target = unique_path(folder, "PPT整理检查练习.pptx")
        with zipfile.ZipFile(template) as archive:
            package = _Package(archive, ".pptx")
            slides, _, _ = _slides(package)
            for index, name in enumerate(slides):
                root = package.edit(name)
                if index:
                    root.set("show", "0")
                for node in root.xpath(".//*[local-name()='t']"):
                    if node.text:
                        node.text = re.sub(r"\{\{.*?\}\}", "练习数据", node.text)
                bodies = _text_bodies(root)
                for body in bodies:
                    for props in body.xpath(".//*[local-name()='rPr']"):
                        props.set("sz", "800" if index == 0 else "2800")
            for name in sorted(package.roots):
                if name.startswith("ppt/notesSlides/") and _local(package.root(name)) == "notes":
                    for node in _note_text_nodes(package.root(name)):
                        node.text = "示例演讲者备注，请在报送前核实。"
                    package.changed.add(name)
            atomic_write(target, package.write)
    return [str(target)], {f["key"]: f["default"] for f in TOOL_SPECS[mode]["fields"]}
