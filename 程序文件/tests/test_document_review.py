from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import RGBColor
from openpyxl import Workbook, load_workbook
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from qingjian.core.common import CancelledError
from qingjian.core import document_review as review


class DocumentReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qingjian-review-", dir=os.environ.get("QINGJIAN_TEST_TMPDIR"))
        self.root = Path(self.temp.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()
        self.output = self.root / "output"

    def tearDown(self):
        self.temp.cleanup()

    def text(self, name, content):
        path = self.inputs / name
        path.write_text(content, encoding="utf-8")
        return path

    def word(self, name="示例.docx", text="正文", picture=False):
        document = Document()
        if text:
            document.add_paragraph(text)
        if picture:
            image = self.inputs / (Path(name).stem + ".png")
            Image.new("RGB", (24, 24), "#2277cc").save(image)
            document.add_picture(str(image))
        path = self.inputs / name
        document.save(path)
        return path

    def book(self, rows, name="申请表.xlsx"):
        workbook = Workbook()
        workbook.active.title = "申请信息"
        for row in rows:
            workbook.active.append(row)
        path = self.inputs / name
        workbook.save(path)
        workbook.close()
        return path

    def pdf(self, name, text="Project: Alpha", image=False, password=None):
        path = self.inputs / name
        if image:
            Image.new("RGB", (60, 60), "#00aacc").save(path, "PDF")
            return path
        writer = PdfWriter()
        page = writer.add_blank_page(300, 200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(("BT /F1 12 Tf 30 150 Td (" + text + ") Tj ET").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        if password:
            writer.encrypt(password)
        with path.open("wb") as output:
            writer.write(output)
        writer.close()
        return path

    def read_sheet(self, result, name):
        path = next(Path(value) for value in result["outputs"] if Path(value).suffix == ".xlsx")
        workbook = load_workbook(path, read_only=True, data_only=False)
        try:
            return list(workbook[name].values)
        finally:
            workbook.close()

    def run_compare(self, old, new, **options):
        return review.run("document_compare", [old, new], self.output, options)

    def run_consistency(self, paths, **options):
        settings = {"rules": [{"name": "项目名称", "pattern": "项目名称", "expected": ""}]}
        settings.update(options)
        return review.run("material_consistency", paths, self.output, settings)

    def test_compare_changes_html_escape_and_source_preservation(self):
        old = self.text("旧.txt", "标题\n预算100万元\n删除内容\n结尾")
        new = self.text("新.txt", "标题\n预算120万元\n<script>alert(1)</script>\n结尾\n新增内容")
        before = {path: path.read_bytes() for path in (old, new)}
        result = self.run_compare(old, new)
        self.assertEqual(result["changes"], 3)
        page = next(Path(value) for value in result["outputs"] if value.endswith(".html")).read_text("utf-8")
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>alert", page)
        self.assertIn("<del>", page)
        self.assertIn("<ins>", page)
        self.assertTrue(all(path.read_bytes() == value for path, value in before.items()))

    def test_compare_default_ignores_whitespace(self):
        old = self.text("旧.txt", "项目 名称：青禾")
        new = self.text("新.txt", "项目名称： 青禾")
        self.assertEqual(self.run_compare(old, new)["changes"], 0)
        result = self.run_compare(old, new, ignore_whitespace=False)
        self.assertEqual(result["changes"], 1)
        self.assertEqual(len(list(self.output.glob("*.xlsx"))), 2)

    def test_compare_add_and_delete_units(self):
        result = self.run_compare(self.text("旧.txt", "开始\n将删除\n结束"), self.text("新.txt", "开始\n结束\n新增"))
        rows = self.read_sheet(result, "文字差异")
        self.assertEqual({row[0] for row in rows[1:]}, {"删除", "新增"})
        self.assertTrue(all(row[1] or row[2] for row in rows[1:]))

    def test_compare_empty_new_version_reports_all_deletions(self):
        result = self.run_compare(self.text("旧.txt", "段落甲\n段落乙"), self.text("新.txt", ""))
        self.assertEqual(result["changes"], 2)
        self.assertTrue(all(row[0] == "删除" for row in self.read_sheet(result, "文字差异")[1:]))

    def test_compare_docx_table_and_header_locations(self):
        paths = []
        for number, amount in enumerate(("100", "120")):
            document = Document()
            document.add_paragraph("正文一致")
            document.add_table(1, 1).cell(0, 0).text = "预算" + amount
            document.sections[0].header.paragraphs[0].text = "页眉" + amount
            path = self.inputs / f"{number}.docx"
            document.save(path)
            paths.append(path)
        rows = self.read_sheet(self.run_compare(*paths), "文字差异")
        self.assertTrue(any("表格" in row[1] for row in rows[1:]))
        self.assertTrue(any("页眉" in row[1] for row in rows[1:]))

    def test_compare_electronic_pdf_and_password(self):
        old = self.pdf("旧.pdf", "Amount 100", password="abc")
        new = self.pdf("新.pdf", "Amount 120", password="abc")
        result = self.run_compare(old, new, password="abc")
        self.assertEqual(result["changes"], 1)
        self.assertIn("第1页", self.read_sheet(result, "文字差异")[1][1])

    def test_compare_wrong_pdf_password_fails(self):
        with self.assertRaisesRegex(ValueError, "密码"):
            self.run_compare(self.pdf("旧.pdf", password="abc"), self.pdf("新.pdf"), password="wrong")

    def test_compare_scanned_pdf_explicitly_fails(self):
        with self.assertRaisesRegex(ValueError, "扫描|核查文字"):
            self.run_compare(self.pdf("旧.pdf", image=True), self.pdf("新.pdf"))
        self.assertFalse(list(self.output.iterdir()))

    def test_compare_requires_exactly_two(self):
        with self.assertRaisesRegex(ValueError, "恰好两份"):
            review.run("document_compare", [self.text("一.txt", "文字")], self.output, {})

    def test_compare_limit_is_explicit_and_no_partial_result(self):
        old = self.text("旧.txt", "\n".join(str(index) for index in range(5002)))
        new = self.text("新.txt", "文字")
        with self.assertRaisesRegex(ValueError, "上限"):
            self.run_compare(old, new)
        self.assertFalse(list(self.output.iterdir()))

    def test_compare_long_units_preserve_complete_text_in_excel_chunks(self):
        old = self.text("旧.txt", "旧" * 35000)
        new = self.text("新.txt", "新" * 36000)
        result = self.run_compare(old, new)
        rows = self.read_sheet(result, "文字差异")
        self.assertEqual("".join(row[3] for row in rows[1:]), "旧" * 35000)
        self.assertEqual("".join(row[4] for row in rows[1:]), "新" * 36000)

    def test_consistency_mismatch_and_excel_source_locations(self):
        word = self.word(text="项目名称：青禾项目")
        book = self.book([["项目名称", "青和项目"]])
        result = self.run_consistency([word, book])
        rows = self.read_sheet(result, "核查结论")
        self.assertTrue(all("跨材料不一致" in row[2] for row in rows[1:]))
        hits = self.read_sheet(result, "全部命中")
        self.assertTrue(any("申请信息" in row[3] and "第1行" in row[3] for row in hits[1:]))
        self.assertEqual(result["changes"], 2)

    def test_consistency_missing_multivalue_expected_and_all_hits(self):
        first = self.text("甲.txt", "项目名称：青禾\n项目名称：青和\n项目名称：青禾")
        second = self.text("乙.txt", "没有此字段")
        result = self.run_consistency([first, second], rules=[{"name": "项目名称", "pattern": "项目名称", "expected": "青禾"}])
        rows = self.read_sheet(result, "核查结论")
        self.assertIn("文件内多值", rows[1][2])
        self.assertIn("不符预期", rows[1][2])
        self.assertEqual(rows[2][2], "缺失")
        self.assertEqual(len(self.read_sheet(result, "全部命中")), 4)

    def test_consistency_identical_repeated_values_are_not_multivalue(self):
        first = self.text("甲.txt", "项目名称：青禾\n项目名称：青禾")
        second = self.text("乙.txt", "项目名称：青 禾")
        result = self.run_consistency([first, second])
        self.assertTrue(all(row[2] == "一致" for row in self.read_sheet(result, "核查结论")[1:]))
        self.assertEqual(len(self.read_sheet(result, "全部命中")), 4)

    def test_consistency_regex_extracts_all_and_first_capture(self):
        first = self.text("甲.txt", "项目名称：青禾 项目名称：青和")
        result = self.run_consistency([first], match_kind="regex", rules=[{"name": "项目名称", "pattern": r"项目名称[：:]\s*(\S+)", "expected": "青禾"}])
        rows = self.read_sheet(result, "全部命中")
        self.assertEqual([row[2] for row in rows[1:]], ["青禾", "青和"])

    def test_consistency_regex_no_capture_uses_whole_match(self):
        path = self.text("编号.txt", "编号QH-001，编号QH-002")
        result = self.run_consistency([path], match_kind="regex", rules=[{"name": "编号", "pattern": r"QH-\d+", "expected": ""}])
        self.assertEqual([row[2] for row in self.read_sheet(result, "全部命中")[1:]], ["QH-001", "QH-002"])

    def test_consistency_keyword_and_blank_value(self):
        path = self.text("空值.txt", "项目名称：")
        result = self.run_consistency([path])
        self.assertEqual(self.read_sheet(result, "核查结论")[1][2], "空值")

    def test_consistency_read_failure_is_not_passed(self):
        good = self.word(text="项目名称：青禾")
        broken = self.inputs / "损坏.docx"
        broken.write_bytes(b"broken docx")
        result = self.run_consistency([good, broken])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("读取失败", self.read_sheet(result, "核查结论")[2][2])

    def test_consistency_scanned_pdf_reported_and_no_partial_values(self):
        pdf = self.pdf("扫描.pdf", image=True)
        result = self.run_consistency([pdf])
        self.assertTrue(result["errors"])
        self.assertIn("读取失败", self.read_sheet(result, "核查结论")[1][2])
        self.assertEqual(len(self.read_sheet(result, "全部命中")), 1)

    def test_consistency_csv_html_pdf_locate(self):
        csv = self.text("申请.csv", "项目名称,青禾\n")
        page = self.text("说明.html", "<p>项目名称：青禾</p><script>项目名称：假值</script>")
        pdf = self.pdf("英文.pdf", "Project: Alpha")
        result = self.run_consistency([csv, page, pdf], rules=[{"name": "项目名称", "pattern": "项目名称", "expected": "青禾"}, {"name": "英文", "pattern": "Project", "expected": "Alpha"}])
        rows = self.read_sheet(result, "全部命中")
        self.assertEqual(len(rows), 4)
        self.assertTrue(any("第1页" in row[3] for row in rows[1:]))
        self.assertFalse(any("假值" in str(row) for row in rows))

    def test_consistency_excel_formula_value_is_text_not_executed(self):
        source = self.text("公式.txt", "项目名称：=1+2")
        result = self.run_consistency([source])
        workbook = load_workbook(result["outputs"][0], read_only=False)
        try:
            cell = workbook["全部命中"].cell(2, 3)
            self.assertEqual(cell.value, "=1+2")
            self.assertEqual(cell.data_type, "s")
        finally:
            workbook.close()

    def test_consistency_regex_long_line_reported_without_partial_values(self):
        source = self.text("长行.txt", "编号A\n" + "a" * 4097)
        result = self.run_consistency([source], match_kind="regex", rules=[{"name": "编号", "pattern": r"编号(\w+)", "expected": ""}])
        self.assertTrue(result["errors"])
        self.assertEqual(len(self.read_sheet(result, "全部命中")), 1)

    def test_invalid_rules_and_unsafe_regex(self):
        cases = [[], [{"name": "", "pattern": "项目"}], [{"name": "编号", "pattern": "("}],
                 [{"name": "编号", "pattern": ".*"}], [{"name": "编号", "pattern": "(a+)+$"}],
                 [{"name": "编号", "pattern": "(a|aa)+$"}], [{"name": "编号", "pattern": r"(a)\1"}],
                 [{"name": "编号", "pattern": r"(?=a)a"}], [{"name": "编号", "pattern": r"a*a*a*Z"}]]
        cases += [[{"name": "编号", "pattern": "a*a*b"}], [{"name": "编号", "pattern": "(?i)a*Aa*b"}]]
        for rules in cases:
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                review.validate("material_consistency", {"rules": rules, "match_kind": "regex"})
        review.validate("material_consistency", {"rules": [{"name": "项目名称", "pattern": r"项目名称[：:]\s*([^\s]+)"}], "match_kind": "regex"})
        review.validate("material_consistency", {"rules": [{"name": "日期", "pattern": r"日期[：:]\s*(\d{4}-\d{2}-\d{2})"}], "match_kind": "regex"})
        with self.assertRaises(ValueError):
            review.validate("material_consistency", {"rules": [{"name": "编号", "pattern": "a"}, {"name": "编号", "pattern": "b"}]})

    def test_chapter_example_split_then_merge_preserves_images_tables_and_text(self):
        sources, options = review.build_example("word_chapters", self.inputs)
        before = Path(sources[0]).read_bytes()
        result = review.run("word_chapters", sources, self.output, options)
        self.assertEqual(len(result["data_outputs"]), 3)
        self.assertEqual(Document(result["data_outputs"][0]).paragraphs[0].text, "本文件为虚构练习材料，可按一级标题拆分。")
        for path in result["data_outputs"][1:]:
            document = Document(path)
            self.assertEqual(len(document.inline_shapes), 1)
            self.assertEqual(len(document.tables), 1)
            self.assertIn("可编辑正文", "".join(p.text for p in document.paragraphs))
        merged = review.run("word_chapters", result["data_outputs"], self.root / "merged", {"action": "merge"})
        document = Document(merged["data_outputs"][0])
        self.assertEqual(len(document.inline_shapes), 2)
        self.assertEqual(len(document.tables), 2)
        self.assertIn("第1章", "".join(p.text for p in document.paragraphs))
        self.assertIn("第2章", "".join(p.text for p in document.paragraphs))
        self.assertEqual(Path(sources[0]).read_bytes(), before)
        with zipfile.ZipFile(merged["data_outputs"][0]) as archive:
            self.assertIsNone(archive.testzip())
            media = [name for name in archive.namelist() if "/media/" in name]
            self.assertGreaterEqual(len(media), 2)

    def test_split_level_two_uses_outline_and_keeps_nested_body(self):
        document = Document()
        document.add_heading("总标题", 1)
        document.add_heading("小节甲", 2)
        document.add_paragraph("正文甲")
        document.add_heading("小节乙", 2)
        document.add_paragraph("正文乙")
        source = self.inputs / "两级.docx"
        document.save(source)
        result = review.run("word_chapters", [source], self.output, {"heading_level": 2})
        self.assertEqual(len(result["data_outputs"]), 3)
        texts = ["".join(p.text for p in Document(path).paragraphs) for path in result["data_outputs"]]
        self.assertIn("正文甲", texts[1])
        self.assertNotIn("正文乙", texts[1])

    def test_split_does_not_infer_visually_large_titles(self):
        source = self.word(text="第一章 看起来像标题")
        result = review.run("word_chapters", [source], self.output, {})
        self.assertTrue(result["errors"])
        self.assertEqual(result["data_outputs"], [])

    def test_split_first_heading_does_not_create_empty_preface(self):
        document = Document()
        document.add_heading("第一章", 1)
        document.add_paragraph("正文")
        source = self.inputs / "无前言.docx"
        document.save(source)
        result = review.run("word_chapters", [source], self.output, {})
        self.assertEqual(len(result["data_outputs"]), 1)
        self.assertTrue(Path(result["data_outputs"][0]).stem.endswith("001_第一章"))

    def test_merge_queue_and_page_break_option(self):
        left, right = self.word("左.docx", "左内容"), self.word("右.docx", "右内容")
        result = review.run("word_chapters", [right, left], self.output, {"action": "merge", "page_break": False})
        document = Document(result["data_outputs"][0])
        self.assertEqual([p.text for p in document.paragraphs], ["右内容", "左内容"])
        self.assertFalse(document.element.xpath(".//w:br"))

    def test_merge_preserves_distinct_style_definitions(self):
        paths = []
        for index, color in enumerate(("FF0000", "0000FF")):
            document = Document()
            style = document.styles.add_style("同名样式", 1)
            style.font.color.rgb = RGBColor.from_string(color)
            document.add_paragraph("文字" + str(index), "同名样式")
            document.styles["Normal"].font.color.rgb = RGBColor.from_string(color)
            document.add_paragraph("普通" + str(index))
            path = self.inputs / f"{index}.docx"
            document.save(path)
            paths.append(path)
        result = review.run("word_chapters", paths, self.output, {"action": "merge", "page_break": False})
        document = Document(result["data_outputs"][0])
        self.assertNotEqual(document.paragraphs[0].style.style_id, document.paragraphs[2].style.style_id)
        self.assertEqual(str(document.paragraphs[0].style.font.color.rgb), "FF0000")
        self.assertEqual(str(document.paragraphs[2].style.font.color.rgb), "0000FF")
        self.assertEqual(str(document.paragraphs[3].style.font.color.rgb), "0000FF")

    def test_merge_list_numbering_is_remapped(self):
        paths = []
        for index in (1, 2):
            document = Document()
            document.add_paragraph("列表" + str(index), "List Number")
            path = self.inputs / f"列表{index}.docx"
            document.save(path)
            paths.append(path)
        result = review.run("word_chapters", paths, self.output, {"action": "merge", "page_break": False})
        document = Document(result["data_outputs"][0])
        first, second = [p.style.element for p in document.paragraphs]
        self.assertNotEqual(first.xpath(".//w:numId")[0].get(qn("w:val")), second.xpath(".//w:numId")[0].get(qn("w:val")))
        ids = [element.get(qn("w:numId")) for element in document.part.numbering_part.element if element.tag == qn("w:num")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_merge_external_hyperlink_and_images_are_preserved(self):
        first = self.word("首.docx", "首")
        second = self.word("次.docx", "次", picture=True)
        document = Document(second)
        p = document.add_paragraph()
        hyperlink = OxmlElement("w:hyperlink")
        hyperlink.set(qn("r:id"), document.part.relate_to("https://example.com/report", RT.HYPERLINK, True))
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "链接"
        run.append(text)
        hyperlink.append(run)
        p._p.append(hyperlink)
        document.save(second)
        before = hashlib.sha256(second.read_bytes()).hexdigest()
        result = review.run("word_chapters", [first, second], self.output, {"action": "merge"})
        document = Document(result["data_outputs"][0])
        self.assertEqual(len(document.inline_shapes), 1)
        self.assertTrue(any(rel.is_external and rel.target_ref == "https://example.com/report" for rel in document.part.rels.values()))
        self.assertEqual(hashlib.sha256(second.read_bytes()).hexdigest(), before)

    def test_merge_bookmark_ids_and_anchor_names_remap_together(self):
        paths = []
        for index in (1, 2):
            document = Document()
            p = document.add_paragraph("书签正文")
            start, end = OxmlElement("w:bookmarkStart"), OxmlElement("w:bookmarkEnd")
            start.set(qn("w:id"), "1")
            start.set(qn("w:name"), "target")
            end.set(qn("w:id"), "1")
            p._p.insert(0, start)
            p._p.append(end)
            link = OxmlElement("w:hyperlink")
            link.set(qn("w:anchor"), "target")
            document.add_paragraph()._p.append(link)
            path = self.inputs / f"书签{index}.docx"
            document.save(path)
            paths.append(path)
        result = review.run("word_chapters", paths, self.output, {"action": "merge"})
        document = Document(result["data_outputs"][0])
        starts = document.element.xpath(".//w:bookmarkStart")
        ends = document.element.xpath(".//w:bookmarkEnd")
        anchors = document.element.xpath(".//w:hyperlink")
        self.assertEqual(len({node.get(qn("w:id")) for node in starts}), 2)
        self.assertEqual([node.get(qn("w:id")) for node in starts], [node.get(qn("w:id")) for node in ends])
        self.assertEqual([node.get(qn("w:name")) for node in starts], [node.get(qn("w:anchor")) for node in anchors])

    def test_chapters_refuse_complex_sections_headers_fields_revisions(self):
        for feature in ("section", "header", "field", "revision", "footnote"):
            with self.subTest(feature=feature):
                document = Document()
                document.add_heading("第一章", 1)
                paragraph = document.add_paragraph("正文")
                if feature == "section":
                    document.add_section(WD_SECTION.NEW_PAGE)
                elif feature == "header":
                    document.sections[0].header.paragraphs[0].text = "页眉"
                else:
                    element = OxmlElement({"field": "w:fldSimple", "revision": "w:ins", "footnote": "w:footnoteReference"}[feature])
                    paragraph._p.append(element)
                source = self.inputs / (feature + ".docx")
                document.save(source)
                result = review.run("word_chapters", [source], self.output, {})
                self.assertTrue(result["errors"])
                self.assertFalse(result["data_outputs"])
                with self.assertRaisesRegex(ValueError, "暂不处理"):
                    review.run("word_chapters", [self.word("普通.docx"), source], self.output, {"action": "merge"})

    def test_merge_invalid_image_relationship_does_not_publish_word(self):
        first = self.word("首.docx")
        second = self.word("次.docx")
        document = Document(second)
        p = document.add_paragraph()
        drawing = OxmlElement("w:drawing")
        drawing.set(qn("r:embed"), "rId9999")
        p._p.append(drawing)
        document.save(second)
        with self.assertRaisesRegex(ValueError, "无效.*关系"):
            review.run("word_chapters", [first, second], self.output, {"action": "merge"})
        self.assertFalse(list(self.output.glob("*.docx")))

    def test_output_names_are_unique_and_sources_not_overwritten(self):
        paths = [self.word("首.docx", "首"), self.word("次.docx", "次")]
        first = review.run("word_chapters", paths, self.output, {"action": "merge"})
        second = review.run("word_chapters", paths, self.output, {"action": "merge"})
        self.assertNotEqual(first["data_outputs"], second["data_outputs"])
        self.assertTrue(all(Path(path).exists() for path in first["outputs"] + second["outputs"]))

    def test_cancel_before_and_during_read_produces_no_reports(self):
        old, new = self.text("旧.txt", "a\nb"), self.text("新.txt", "a\nc")
        with self.assertRaises(CancelledError):
            review.run("document_compare", [old, new], self.output, {}, cancel=lambda: True)
        count = 0
        def cancel():
            nonlocal count
            count += 1
            return count > 2
        with self.assertRaises(CancelledError):
            review.run("document_compare", [old, new], self.output, {}, cancel=cancel)
        self.assertFalse(list(self.output.iterdir()))

    def test_cancel_consistency_propagates_and_releases_workbook(self):
        source = self.book([["项目名称", "青禾"], ["项目名称", "青和"]])
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 4
        with self.assertRaises(CancelledError):
            self.run_consistency_with_cancel(source, cancel)
        source.rename(source.with_name("已释放.xlsx"))

    def run_consistency_with_cancel(self, source, cancel):
        return review.run("material_consistency", [source], self.output, {"rules": [{"name": "项目名称", "pattern": "项目名称"}]}, cancel=cancel)

    def test_atomic_save_failure_does_not_publish_incomplete_word(self):
        source, options = review.build_example("word_chapters", self.inputs)
        with mock.patch.object(review, "_audit_relationships", side_effect=ValueError("关系失败")):
            result = review.run("word_chapters", source, self.output, options)
        self.assertFalse(result["data_outputs"])
        self.assertTrue(result["errors"])
        self.assertFalse(list(self.output.glob("*.docx")))

    def test_validate_types_and_modes(self):
        for options in ({"ignore_whitespace": "yes"}, {"password": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                review.validate("document_compare", options)
        for options in ({"heading_level": 0}, {"heading_level": True}, {"action": "delete"}, {"page_break": "yes"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                review.validate("word_chapters", options)
        with self.assertRaises(ValueError):
            review.validate("missing", {})

    def test_all_examples_are_runnable_and_guides_complete(self):
        for mode in review.TOOL_SPECS:
            with self.subTest(mode=mode):
                source, options = review.build_example(mode, self.root / (mode + "-example"))
                review.validate(mode, options, source)
                result = review.run(mode, source, self.root / (mode + "-result"), options)
                self.assertFalse(result["errors"])
                self.assertTrue(result["outputs"])
                self.assertTrue(all(Path(path).is_file() for path in result["outputs"]))
                self.assertTrue({"intro", "quick", "steps", "terms", "example"} <= review.GUIDES[mode].keys())


if __name__ == "__main__":
    unittest.main()
