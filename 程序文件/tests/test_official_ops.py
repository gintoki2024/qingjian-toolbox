"""Meaningful regression tests for official layout and content preservation."""
from __future__ import annotations

import base64
from io import BytesIO
import unittest

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm, Inches, Pt

from qingjian.core.official_ops import (
    apply_official_format, create_official_template,
    inspect_official_document, validate_official_options,
)


_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2ioAAAAASUVORK5CYII=")


def _link(paragraph, text="链接文字", target="https://example.com/a?x=1&y=2"):
    rid = paragraph.part.relate_to(target, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rid)
    run, node = OxmlElement("w:r"), OxmlElement("w:t")
    node.text = text
    run.append(node)
    link.append(run)
    paragraph._p.append(link)
    return link, rid


def _field(paragraph, instruction=" PAGE ", result="1"):
    for kind, text in (("begin", None), ("instruction", instruction), ("separate", None), ("text", result), ("end", None)):
        run = paragraph.add_run()
        if kind in {"begin", "separate", "end"}:
            node = OxmlElement("w:fldChar")
            node.set(qn("w:fldCharType"), kind)
        elif kind == "instruction":
            node = OxmlElement("w:instrText")
            node.text = text
        else:
            node = OxmlElement("w:t")
            node.text = text
        run._r.append(node)


def _parts(document):
    return {str(part.partname): part.blob for part in document.part.package.parts}


def _roundtrip(document):
    memory = BytesIO()
    document.save(memory)
    memory.seek(0)
    return Document(memory)


def _page_count(document):
    return sum(1 for part in document.part.package.parts if str(part.partname).startswith("/word/footer")
               for node in part.element.iter(qn("w:instrText")) if "PAGE" in (node.text or ""))


class OfficialOperationsTests(unittest.TestCase):
    def doc(self, *texts):
        document = Document()
        for text in (texts or ("测试公文标题", "正文内容。")):
            document.add_paragraph(text)
        return document

    def test_defaults_and_nested_options_and_validation_before_mutation(self):
        defaults = validate_official_options({"official": {}})
        self.assertEqual(defaults["company_name"], "利安人寿保险股份有限公司")
        self.assertEqual(defaults["body_font"], "仿宋")
        self.assertEqual(defaults["headings"][1]["font"], "楷体")
        self.assertEqual(validate_official_options({"profile": "general"})["company_name"], "")
        document = self.doc()
        before = _parts(document)
        for invalid in ({"line_pt": float("nan")}, {"body_size": True}, {"top_cm": 20},
                        {"format_tables": "yes"}, {"custom_terms": "缺少分隔符"},
                        {"headings": [{"keywords": "正则:("}]}, {"headings": [{"level": 1}, {"level": 1}]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                apply_official_format(document, invalid)
            self.assertEqual(_parts(document), before)

    def test_inspection_and_check_are_exactly_read_only(self):
        document = self.doc("标题", "一、工作要求", "截止目前，待补充。")
        before = _parts(document)
        result = inspect_official_document(document, {})
        self.assertEqual(result["changes"], 0)
        self.assertTrue(result["audit"]["corrections"])
        self.assertEqual(_parts(document), before)
        result = apply_official_format(document, {"operation": "check"})
        self.assertEqual(result["changes"], 0)
        self.assertEqual(_parts(document), before)
        self.assertEqual(_page_count(document), 0)

    def test_inherited_indent_and_missing_normal_do_not_break_layout(self):
        document = self.doc()
        normal = document.styles["Normal"]
        normal.paragraph_format.left_indent = Cm(2)
        normal.paragraph_format.right_indent = Cm(1)
        normal.paragraph_format.first_line_indent = Pt(-16)
        apply_official_format(document, {"page_numbers": "keep"})
        body = document.paragraphs[1]
        self.assertEqual(body.paragraph_format.left_indent, Pt(0))
        self.assertEqual(body.paragraph_format.right_indent, Pt(0))
        self.assertEqual(body.paragraph_format.first_line_indent, Pt(32))
        self.assertEqual(body._p.pPr.ind.get(qn("w:firstLineChars")), "200")
        self.assertEqual(document.paragraphs[0]._p.pPr.ind.get(qn("w:firstLineChars")), "0")
        document2 = self.doc()
        element = document2.styles["Normal"].element
        element.getparent().remove(element)
        apply_official_format(document2, {})
        self.assertEqual(document2.paragraphs[1].runs[0].font.name, "仿宋")
        self.assertEqual(_roundtrip(document2).paragraphs[1].text, "正文内容。")

    def test_four_levels_and_custom_keyword_precedence(self):
        document = self.doc("标题", "一、工作部署", "（一）工作要求", "1.工作措施", "（1）落实责任", "普通段落中含专项标题关键词")
        options = {"headings": [{"level": 2, "keywords": "专项标题", "font": "宋体", "size": 18}]}
        result = apply_official_format(document, options)
        self.assertEqual([h["level"] for h in result["audit"]["headings"]], [1, 2, 3, 4, 2])
        self.assertEqual(document.paragraphs[-1].runs[0].font.name, "宋体")
        self.assertEqual(document.paragraphs[-1].runs[0].font.size, Pt(18))
        self.assertEqual(document.paragraphs[1].paragraph_format.first_line_indent, Pt(0))

    def test_exact_prefix_regex_and_word_heading_mapping(self):
        document = self.doc("标题", "工作要求", "正文中提到工作要求。", "趋势一：经营情况", "1.1 经营形势", "1.1.1 细分情况", "第二章工作机制")
        for i in range(1, 5):
            document.add_paragraph(f"样式标题 {i}", style=f"Heading {i}")
        document.add_paragraph("前缀条款")
        options = {"headings": [{"level": 1, "keywords": "=工作要求\n正则:^(前缀|特定)条款$"}]}
        result = apply_official_format(document, options)
        record = {h["text"]: h for h in result["audit"]["headings"]}
        self.assertNotIn("正文中提到工作要求。", record)
        self.assertEqual(record["趋势一：经营情况"]["level"], 2)
        self.assertEqual(record["1.1 经营形势"]["level"], 2)
        self.assertEqual(record["1.1.1 细分情况"]["level"], 3)
        for i in range(1, 5):
            self.assertEqual(record[f"样式标题 {i}"]["level"], i)
            self.assertEqual(record[f"样式标题 {i}"]["source"], "Word 标题样式")
        self.assertEqual(record["前缀条款"]["level"], 1)
        self.assertEqual(inspect_official_document(self.doc("标题", "前缀条款"), {"headings": [{"keywords": "前缀*"}]})["audit"]["headings"][0]["level"], 1)

    def test_rich_content_is_preserved_while_cross_run_cleanup_works(self):
        document = self.doc("标题")
        paragraph = document.add_paragraph()
        paragraph.add_run("  中 ")
        paragraph.add_run(" 文, ")
        paragraph.add_run().add_picture(BytesIO(_PNG), width=Inches(.1))
        link, rid = _link(paragraph)
        _field(paragraph, " DOCPROPERTY SomeField ", "  域  内容,  ")
        paragraph.add_run("网址 https://example.com/a?x=1&y=2 金额1.25")
        images = {k: v for k, v in _parts(document).items() if "/media/" in k}
        result = apply_official_format(document, {"remove_extra_spaces": True, "punctuation": "zh", "page_numbers": "keep"})
        self.assertEqual(result["audit"]["counts"]["cleaned_paragraphs"], 1)
        self.assertEqual(len(list(paragraph._p.iter(qn("w:drawing")))), 1)
        self.assertEqual(len(list(paragraph._p.iter(qn("w:hyperlink")))), 1)
        self.assertEqual(link.get(qn("r:id")), rid)
        self.assertEqual(document.part.rels[rid].target_ref, "https://example.com/a?x=1&y=2")
        self.assertIn("中文，", paragraph.text)
        self.assertIn("  域  内容,  ", paragraph.text)
        self.assertIn("https://example.com/a?x=1&y=2", paragraph.text)
        self.assertIn("1.25", paragraph.text)
        self.assertEqual(images, {k: v for k, v in _parts(document).items() if "/media/" in k})

    def test_manual_break_split_keeps_image_hyperlink_bookmark_and_section(self):
        document = self.doc("标题")
        p = document.add_paragraph("第一段")
        p.add_run().add_break()
        p.add_run().add_picture(BytesIO(_PNG), width=Inches(.1))
        _link(p, "第二段")
        p.add_run().add_break()
        p.add_run("第三段")
        section = OxmlElement("w:sectPr")
        p._p.get_or_add_pPr().append(section)
        result = apply_official_format(document, {"split_line_breaks": True, "page_numbers": "keep"})
        self.assertEqual(result["audit"]["counts"]["split_paragraphs"], 2)
        self.assertEqual([p.text for p in document.paragraphs], ["标题", "第一段", "第二段", "第三段"])
        self.assertEqual(len(list(document.element.body.iter(qn("w:drawing")))), 1)
        self.assertEqual(len(list(document.element.body.iter(qn("w:hyperlink")))), 1)
        self.assertIsNone(document.paragraphs[1]._p.pPr.find(qn("w:sectPr")))
        self.assertIsNotNone(document.paragraphs[-1]._p.pPr.find(qn("w:sectPr")))
        self.assertEqual(len(_roundtrip(document).paragraphs), 4)

    def test_breaks_inside_hyperlinks_split_without_losing_relationship(self):
        document = self.doc("标题")
        p = document.add_paragraph()
        link, rid = _link(p, "链接前段")
        run = OxmlElement("w:r")
        run.append(OxmlElement("w:br"))
        text = OxmlElement("w:t")
        text.text = "链接后段"
        run.append(text)
        link.append(run)
        result = apply_official_format(document, {"split_line_breaks": True, "page_numbers": "keep"})
        self.assertEqual(result["audit"]["counts"]["split_paragraphs"], 1)
        self.assertEqual([p.text for p in document.paragraphs], ["标题", "链接前段", "链接后段"])
        self.assertTrue(all(l.get(qn("r:id")) == rid for l in document.element.body.iter(qn("w:hyperlink"))))

    def test_complex_field_breaks_and_page_breaks_are_preserved(self):
        document = self.doc("标题")
        p = document.add_paragraph()
        _field(p, " DOCPROPERTY Date ", "字段内容")
        p.add_run().add_break()
        p.add_run("尾部")
        page = document.add_paragraph()
        page.add_run().add_break(WD_BREAK.PAGE)
        result = apply_official_format(document, {"split_line_breaks": True, "page_numbers": "keep"})
        self.assertTrue(any("未拆段" in w for w in result["warnings"]))
        self.assertEqual(len(document.paragraphs), 3)
        self.assertEqual(len(list(page._p.iter(qn("w:br")))), 1)

    def test_blank_removal_keeps_image_only_and_table_required_paragraph(self):
        document = self.doc("标题", "")
        p = document.add_paragraph()
        p.add_run().add_picture(BytesIO(_PNG), width=Inches(.1))
        bookmark_p = document.add_paragraph()
        mark = OxmlElement("w:bookmarkStart")
        mark.set(qn("w:id"), "1")
        mark.set(qn("w:name"), "Preserve")
        bookmark_p._p.append(mark)
        table = document.add_table(rows=1, cols=1)
        result = apply_official_format(document, {"remove_blank_paragraphs": True, "format_tables": True, "page_numbers": "keep"})
        self.assertEqual(result["audit"]["counts"]["removed_blank_paragraphs"], 1)
        self.assertEqual(len(document.paragraphs), 3)
        self.assertEqual(len(table.cell(0, 0).paragraphs), 1)
        self.assertEqual(len(list(document.element.body.iter(qn("w:drawing")))), 1)

    def test_table_is_optional_and_nested_tables_format_once(self):
        document = self.doc()
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "表格文字"
        table.cell(0, 0).paragraphs[0].runs[0].font.size = Pt(9)
        nested = table.cell(0, 0).add_table(rows=1, cols=1)
        nested.cell(0, 0).text = "嵌套文字"
        before = table._tbl.xml
        apply_official_format(document, {"page_numbers": "keep"})
        self.assertEqual(table._tbl.xml, before)
        apply_official_format(document, {"format_tables": True, "page_numbers": "keep"})
        self.assertEqual(nested.cell(0, 0).paragraphs[0].runs[0].font.size, Pt(16))
        self.assertEqual(table.cell(0, 0).paragraphs[0].text, "表格文字")

    def test_footer_content_header_images_and_existing_fields_survive_repeat(self):
        document = self.doc()
        section = document.sections[0]
        section.header.paragraphs[0].text = "公司页眉"
        section.header.paragraphs[0].add_run().add_picture(BytesIO(_PNG), width=Inches(.1))
        section.footer.paragraphs[0].text = "内部资料"
        section.different_first_page_header_footer = True
        section.first_page_footer.paragraphs[0].text = "首首页脚"
        first = apply_official_format(document, {})
        count = _page_count(document)
        self.assertGreaterEqual(count, 3)
        self.assertTrue(document.settings.odd_and_even_pages_header_footer)
        self.assertEqual(section.even_page_header.paragraphs[0].text, "公司页眉")
        self.assertEqual(section.even_page_header.part, section.header.part)
        second = apply_official_format(document, {})
        self.assertEqual(second["changes"], 0)
        self.assertEqual(_page_count(document), count)
        self.assertIn("内部资料", "".join(p.text for p in section.footer.paragraphs))
        self.assertIn("内部资料", "".join(p.text for p in section.even_page_footer.paragraphs))
        self.assertIn("首首页脚", "".join(p.text for p in section.first_page_footer.paragraphs))
        reloaded = _roundtrip(document)
        for part in reloaded.part.package.parts:
            if str(part.partname).startswith("/word/footer"):
                for run in part.element.iter(qn("w:r")):
                    if any(x.tag == qn("w:instrText") and "PAGE" in (x.text or "") for x in run):
                        self.assertEqual(run.find("./" + qn("w:rPr") + "/" + qn("w:rFonts")).get(qn("w:eastAsia")), "宋体")
                        self.assertEqual(run.find("./" + qn("w:rPr") + "/" + qn("w:sz")).get(qn("w:val")), "28")

    def test_existing_mixed_footer_page_does_not_duplicate_or_delete_text(self):
        document = self.doc()
        paragraph = document.sections[0].footer.paragraphs[0]
        paragraph.add_run("保密资料 第")
        _field(paragraph)
        paragraph.add_run("页")
        result = apply_official_format(document, {"page_numbers": "center"})
        self.assertEqual(_page_count(document), 1)
        self.assertEqual(paragraph.text, "保密资料 第1页")
        self.assertTrue(any("既有页码" in w for w in result["warnings"]))

    def test_continuous_landscape_sections_and_word_numbering_are_retained(self):
        document = self.doc()
        p = document.add_paragraph("自动编号")
        num = p._p.get_or_add_pPr().get_or_add_numPr()
        num.get_or_add_numId().val = 1
        section = document.add_section(WD_SECTION.CONTINUOUS)
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = Cm(29.7), Cm(21)
        apply_official_format(document, {})
        self.assertEqual(section.start_type, WD_SECTION.CONTINUOUS)
        self.assertEqual(section.orientation, WD_ORIENT.LANDSCAPE)
        self.assertAlmostEqual(section.page_width.cm, 29.7, places=2)
        self.assertIsNotNone(p._p.pPr.numPr)

    def test_company_name_and_document_number_are_not_mistaken_for_title(self):
        document = self.doc("利安人寿保险股份有限公司", "利安发〔2026〕10号", "关于测试工作的通知", "正文。")
        first_xml = document.paragraphs[0]._p.xml
        result = apply_official_format(document, {"page_numbers": "keep"})
        self.assertEqual(document.paragraphs[0]._p.xml, first_xml)
        self.assertEqual(document.paragraphs[2].alignment, WD_ALIGN_PARAGRAPH.CENTER)
        self.assertEqual(document.paragraphs[2].runs[0].font.size, Pt(22))
        snap = document.paragraphs[2]._p.pPr.find(qn("w:snapToGrid"))
        self.assertEqual(snap.get(qn("w:val")), "0")

    def test_proofreading_is_suggestion_only_with_parent_scoped_numbering(self):
        document = self.doc("标题", "一、工作要求", "（一）事项一", "（二）事项二", "二、其他要求", "（一）再次从一开始", "（三）缺第二项", "截止目前，利安人寿保险有限公司【】 2026年2月30日（。", "2026-10-29 台帐")
        original = [p.text for p in document.paragraphs]
        result = apply_official_format(document, {"custom_terms": "事项=具体事项", "page_numbers": "keep"})
        self.assertEqual([p.text for p in document.paragraphs], original)
        issues = result["audit"]["corrections"]
        self.assertTrue(any("截至目前" in x for x in issues))
        self.assertTrue(any("具体事项" in x for x in issues))
        self.assertTrue(any("不是有效日期" in x for x in issues))
        self.assertTrue(any("占位" in x for x in issues))
        self.assertTrue(any("括号" in x for x in issues))
        numbering = [x for x in issues if "序号疑似" in x]
        self.assertEqual(len(numbering), 1)
        self.assertNotIn("正文中", "".join(issues))

    def test_decimal_numbering_uses_parent_scope_and_signoff_is_formatted(self):
        document = self.doc("标题", "1.1 第一项", "1.2 第二项", "2.1 新父级第一项", "正文。",
                            "利安人寿保险股份有限公司", "2026年10月29日")
        result = apply_official_format(document, {"page_numbers": "keep"})
        self.assertFalse(any("序号疑似" in x for x in result["audit"]["corrections"]))
        for paragraph in document.paragraphs[-2:]:
            self.assertEqual(paragraph.alignment, WD_ALIGN_PARAGRAPH.RIGHT)
            self.assertEqual(paragraph.runs[0].font.name, "仿宋")
            self.assertEqual(paragraph.paragraph_format.first_line_indent, Pt(0))

    def test_odd_even_footer_clone_preserves_image_relationships_and_unique_bookmarks(self):
        document = self.doc()
        paragraph = document.sections[0].footer.paragraphs[0]
        paragraph.add_run("页脚图片")
        paragraph.add_run().add_picture(BytesIO(_PNG), width=Inches(.1))
        mark = OxmlElement("w:bookmarkStart")
        mark.set(qn("w:id"), "0")
        mark.set(qn("w:name"), "FooterMark")
        paragraph._p.append(mark)
        end = OxmlElement("w:bookmarkEnd")
        end.set(qn("w:id"), "0")
        paragraph._p.append(end)
        apply_official_format(document, {})
        reloaded = _roundtrip(document)
        names, identifiers = [], []
        for part in reloaded.part.package.parts:
            if str(part.partname).startswith("/word/footer"):
                for node in part.element.iter(qn("w:bookmarkStart")):
                    names.append(node.get(qn("w:name")))
                    identifiers.append(node.get(qn("w:id")))
                for blip in part.element.iter(qn("a:blip")):
                    self.assertEqual(part.rels[blip.get(qn("r:embed"))].target_part.blob, _PNG)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_preflight_reports_font_size_line_indent_and_page_issues_read_only(self):
        document = self.doc("标题", "一、一级标题", "（一）二级标题", "1.三级标题", "（1）四级标题", "正文内容。")
        for paragraph in document.paragraphs:
            paragraph.runs[0].font.name = "宋体"
            paragraph.runs[0].font.size = Pt(12)
            paragraph.paragraph_format.first_line_indent = Pt(0)
            paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            paragraph.paragraph_format.line_spacing = Pt(24)
        before = _parts(document)
        result = inspect_official_document(document, {})
        self.assertEqual(_parts(document), before)
        text = "\n".join(result["audit"]["preflight"])
        self.assertIn("字体", text)
        self.assertIn("字号", text)
        self.assertIn("固定 28 磅", text)
        self.assertIn("首行缩进", text)
        self.assertIn("单倍行距", text)
        self.assertIn("奇偶页", text)
        self.assertIn("PAGE", text)
        for location in range(1, 7):
            self.assertTrue(any(f"正文第 {location} 段（" in item for item in result["audit"]["preflight"]))
        formatted = apply_official_format(document, {})
        self.assertEqual(formatted["audit"]["postflight"], [])
        rechecked = inspect_official_document(document, {})
        self.assertFalse(any("字体" in x or "行距" in x or "首行缩进" in x or "PAGE" in x for x in rechecked["audit"]["preflight"]))

    def test_preflight_resolves_paragraph_and_character_style_inheritance(self):
        document = self.doc("标题", "正文内容。")
        apply_official_format(document, {"page_numbers": "keep"})
        body = document.paragraphs[1]
        pstyle = document.styles.add_style("测试继承正文", WD_STYLE_TYPE.PARAGRAPH)
        pstyle.base_style = document.styles["青简公文正文"]
        pstyle.paragraph_format.first_line_indent = Pt(32)
        pstyle.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        pstyle.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "仿宋")
        pstyle.font.size = Pt(16)
        body.style = pstyle
        body._p.pPr.remove(body._p.pPr.ind)
        body._p.pPr.remove(body._p.pPr.spacing)
        charstyle = document.styles.add_style("测试继承字符", WD_STYLE_TYPE.CHARACTER)
        charstyle.font.size = Pt(16)
        charstyle.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "仿宋")
        run = body.runs[0]
        rpr = run._r.rPr
        for node in list(rpr):
            rpr.remove(node)
        run.style = charstyle
        result = inspect_official_document(document, {"page_numbers": "keep"})
        self.assertFalse(any("正文第 2 段" in x for x in result["audit"]["preflight"]))
        charstyle.font.size = Pt(12)
        result = inspect_official_document(document, {"page_numbers": "keep"})
        self.assertTrue(any("正文第 2 段" in x and "继承样式" in x and "字号" in x for x in result["audit"]["preflight"]))

    def test_postflight_detects_actual_value_changes_not_just_spacing_rule(self):
        from qingjian.core.official_ops import _all_paragraphs, _main_title, _postflight
        document = self.doc()
        options = validate_official_options({"page_numbers": "keep"})
        apply_official_format(document, options)
        body = document.paragraphs[1]
        body.paragraph_format.line_spacing = Pt(24)
        body.paragraph_format.first_line_indent = Pt(0)
        body._p.pPr.ind.set(qn("w:firstLineChars"), "0")
        body.runs[0].font.size = Pt(9)
        paragraphs = _all_paragraphs(document)
        issues = _postflight(document, paragraphs, _main_title(paragraphs, options), options)
        text = "\n".join(issues)
        self.assertIn("固定 28 磅", text)
        self.assertIn("首行缩进", text)
        self.assertIn("字号", text)

    def test_attachment_descriptions_and_page_markers_keep_their_distinct_format(self):
        document = self.doc("标题", "正文。", "附件：材料名称", "附件1：编号材料", "附件 2 材料目录", "附件", "附件 1")
        original = [p.text for p in document.paragraphs]
        result = apply_official_format(document, {"page_numbers": "keep"})
        self.assertEqual([p.text for p in document.paragraphs], original)
        for paragraph in document.paragraphs[2:5]:
            self.assertEqual(paragraph.runs[0].font.name, "仿宋")
            self.assertEqual(paragraph.paragraph_format.first_line_indent, Pt(32))
        for paragraph in document.paragraphs[5:]:
            self.assertEqual(paragraph.runs[0].font.name, "黑体")
            self.assertEqual(paragraph.paragraph_format.first_line_indent, Pt(0))
        self.assertTrue(any("附件" in x and "不搬动" in x for x in result["audit"]["preflight"]))
        self.assertEqual(result["audit"]["postflight"], [])

    def test_company_and_placeholder_checks_work_independently_of_proofreading(self):
        document = self.doc("标题", "利安人寿保险有限公司 待补充 截止目前 2026年2月30日（")
        original = [p.text for p in document.paragraphs]
        for company, placeholder in ((True, False), (False, True), (False, False)):
            result = inspect_official_document(document, {"proofread": False,
                "company_term_check": company, "placeholder_check": placeholder})
            issues = "\n".join(result["audit"]["corrections"])
            self.assertEqual("公司称谓" in issues, company)
            self.assertEqual("占位文字" in issues, placeholder)
            self.assertNotIn("截至目前", issues)
            self.assertNotIn("不是有效日期", issues)
        self.assertEqual([p.text for p in document.paragraphs], original)

    def test_template_types_are_editable_placeholders_without_invented_facts(self):
        for kind in ("通知", "请示", "报告", "函", "批复", "会议纪要"):
            with self.subTest(kind=kind):
                document = create_official_template({}, "", "", "", kind)
                texts = "\n".join(p.text for p in document.paragraphs)
                self.assertIn(kind, document.paragraphs[0].text)
                self.assertIn("【发文单位署名】", texts)
                self.assertIn("【成文日期", texts)
                self.assertNotIn("〔2026〕", texts)
                self.assertNotIn("2026年10月29日", texts)
                self.assertEqual(document.paragraphs[-1].alignment, WD_ALIGN_PARAGRAPH.RIGHT)
                self.assertGreater(_page_count(document), 0)
        explicit = create_official_template({}, "指定标题", "某部门", "2026年10月29日", "通知")
        self.assertEqual(explicit.paragraphs[0].text, "指定标题")
        self.assertEqual(explicit.paragraphs[-2].text, "某部门")


if __name__ == "__main__":
    unittest.main()
