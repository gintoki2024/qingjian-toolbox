from __future__ import annotations

import json
from datetime import datetime
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from docx import Document
from lxml import etree
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment

from qingjian.core import office_prepare as op


def members(path):
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def rewrite(path, changes=None, mutate=None):
    parts = members(path)
    if mutate:
        mutate(parts)
    parts.update(changes or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)


class OfficePrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.folder = self.root / "result"

    def tearDown(self):
        self.temporary.cleanup()

    def run_tool(self, mode="submission_preflight", paths=None, **options):
        return op.run(mode, paths or [self.document()], self.folder, options)

    def report(self, result):
        return json.loads(Path(result["report"]).read_text(encoding="utf-8"))

    def document(self):
        path = self.root / "原件.docx"
        if path.exists():
            return path
        doc = Document()
        doc.core_properties.author = "=HYPERLINK(\"https://example.invalid\",\"作者\")"
        doc.core_properties.title = "业务标题需保留"
        doc.core_properties.last_printed = datetime(2026, 1, 1, 9, 0, 0)
        doc.add_paragraph("原始正文")
        doc.add_paragraph().add_run("隐藏业务文字").font.hidden = True
        if hasattr(doc, "add_comment"):
            doc.add_comment(doc.add_paragraph().add_run("带批注"), text="批注内容", author="批注作者")
        p = doc.add_paragraph("修订示例：")._p
        for tag, text in (("del", "修改前"), ("ins", "修改后")):
            change = etree.SubElement(p, "{" + op.W + "}" + tag, {"{" + op.W + "}id": "1", "{" + op.W + "}author": "修订人"})
            run = etree.SubElement(change, "{" + op.W + "}r")
            etree.SubElement(run, "{" + op.W + "}" + ("delText" if tag == "del" else "t")).text = text
        doc.save(path)
        return path

    def workbook(self, suffix=".xlsx"):
        path = self.root / ("原表" + suffix)
        book = Workbook()
        sheet = book.active
        sheet.title = "数据"
        sheet["A1"] = "编号"
        sheet["A2"] = "0001"
        sheet["A2"].comment = Comment("请核实", "批注人")
        sheet["B2"] = "='[outside.xlsx]Sheet1'!A1"
        sheet.row_dimensions[2].hidden = True
        sheet.column_dimensions["C"].hidden = True
        hidden = book.create_sheet("内部说明")
        hidden.sheet_state = "veryHidden"
        hidden["A1"] = "不可自动删除的业务内容"
        book.save(path)
        book.close()
        return path

    def deck(self):
        paths, _ = op.build_example("ppt_prepare", self.root)
        return Path(paths[0])

    def test_inspection_preserves_source_and_reports_locations(self):
        source = self.document()
        before = source.read_bytes()
        result = self.run_tool(paths=[source])
        self.assertEqual([], result["errors"])
        self.assertEqual([], result["data_outputs"])
        self.assertEqual(before, source.read_bytes())
        issues = self.report(result)["records"][0]["findings"]
        self.assertTrue({"身份属性", "隐藏文字", "Word修订"}.issubset({v["category"] for v in issues}))
        self.assertTrue(any("段落" in v["location"] for v in issues if v["category"] == "Word修订"))

    def test_clean_selected_properties_comments_only_and_keep_business_content(self):
        source = self.document()
        before = members(source)
        result = self.run_tool(paths=[source], action="clean", clear_properties=True, remove_comments=True)
        self.assertFalse(result["errors"])
        target = Path(result["data_outputs"][0])
        doc = Document(target)
        self.assertFalse(doc.core_properties.author)
        self.assertIsNone(doc.core_properties.last_printed)
        self.assertEqual("业务标题需保留", doc.core_properties.title)
        after = members(target)
        root = etree.fromstring(after["word/document.xml"])
        self.assertIn("隐藏业务文字", "".join(root.itertext()))
        self.assertTrue(root.xpath(".//*[local-name()='ins']"))
        self.assertFalse(root.xpath(".//*[local-name()='commentRangeStart' or local-name()='commentReference']"))
        self.assertFalse(any(op._comment_part(n) for n in after))
        self.assertEqual(before["word/styles.xml"], after["word/styles.xml"])
        self.assertEqual(before["word/theme/theme1.xml"], after["word/theme/theme1.xml"])
        self.assertEqual(before, members(source))

    def test_accept_and_reject_actual_word_changes(self):
        source = self.document()
        for action, keep, remove in (("accept", "修改后", "修改前"), ("reject", "修改前", "修改后")):
            result = self.run_tool(paths=[source], action="clean", revision_action=action)
            self.assertFalse(result["errors"])
            root = etree.fromstring(members(result["data_outputs"][0])["word/document.xml"])
            self.assertIn(keep, "".join(root.itertext()))
            self.assertNotIn(remove, "".join(root.itertext()))
            self.assertFalse(root.xpath(".//*[local-name()='ins' or local-name()='del' or local-name()='delText']"))
            Document(result["data_outputs"][0])

    def test_property_revision_reject_restores_previous_font(self):
        source = self.document()
        def mutate(parts):
            root = etree.fromstring(parts["word/document.xml"])
            run = root.xpath(".//*[local-name()='r']")[0]
            props = etree.Element("{" + op.W + "}rPr")
            etree.SubElement(props, "{" + op.W + "}b")
            change = etree.SubElement(props, "{" + op.W + "}rPrChange")
            old = etree.SubElement(change, "{" + op.W + "}rPr")
            etree.SubElement(old, "{" + op.W + "}i")
            run.insert(0, props)
            parts["word/document.xml"] = etree.tostring(root)
        rewrite(source, mutate=mutate)
        result = self.run_tool(paths=[source], action="clean", revision_action="reject")
        root = etree.fromstring(members(result["data_outputs"][0])["word/document.xml"])
        props = root.xpath(".//*[local-name()='r']")[0][0]
        self.assertEqual(["i"], [op._local(n) for n in props])

    def test_complex_revision_errors_without_partial_copy(self):
        source = self.document()
        def mutate(parts):
            root = etree.fromstring(parts["word/document.xml"])
            para = root.xpath(".//*[local-name()='p']")[0]
            pp = etree.SubElement(para, "{" + op.W + "}pPr")
            rp = etree.SubElement(pp, "{" + op.W + "}rPr")
            etree.SubElement(rp, "{" + op.W + "}del")
            parts["word/document.xml"] = etree.tostring(root)
        rewrite(source, mutate=mutate)
        result = self.run_tool(paths=[source], action="clean", revision_action="accept")
        self.assertFalse(result["data_outputs"])
        self.assertIn("复杂修订", result["errors"][0])
        self.assertTrue(self.report(result)["records"][0]["findings"])
        self.assertFalse(list(self.folder.glob("*.docx")))

    def test_excel_comments_clean_preserves_hidden_data_and_formula(self):
        source = self.workbook()
        before = members(source)
        result = self.run_tool(paths=[source], action="clean", remove_comments=True)
        self.assertFalse(result["errors"])
        after = members(result["data_outputs"][0])
        self.assertEqual(before["xl/workbook.xml"], after["xl/workbook.xml"])
        self.assertFalse(any(op._comment_part(n) for n in after))
        book = load_workbook(result["data_outputs"][0], data_only=False)
        try:
            self.assertIsNone(book["数据"]["A2"].comment)
            self.assertEqual("='[outside.xlsx]Sheet1'!A1", book["数据"]["B2"].value)
            self.assertTrue(book["数据"].row_dimensions[2].hidden)
            self.assertEqual("veryHidden", book["内部说明"].sheet_state)
            self.assertEqual("不可自动删除的业务内容", book["内部说明"]["A1"].value)
        finally:
            book.close()
        categories = {i["category"] for i in self.report(result)["records"][0]["findings"]}
        self.assertTrue({"批注", "隐藏行列", "隐藏工作表", "外链公式（疑似）"}.issubset(categories))

    def test_xlsm_embedded_macro_preserved_without_execution(self):
        source = self.workbook(".xlsm")
        macro = b"inert-fixture-macro-bytes"
        rewrite(source, {"xl/vbaProject.bin": macro, "xl/embeddings/oleObject1.bin": b"inert-object"})
        result = self.run_tool(paths=[source], action="clean", clear_properties=True)
        self.assertFalse(result["errors"])
        after = members(result["data_outputs"][0])
        self.assertEqual(macro, after["xl/vbaProject.bin"])
        self.assertEqual(b"inert-object", after["xl/embeddings/oleObject1.bin"])
        self.assertEqual(".xlsm", Path(result["data_outputs"][0]).suffix)
        self.assertIn("宏", {i["category"] for i in self.report(result)["records"][0]["findings"]})

    def test_excel_vml_note_removal_preserves_other_form_controls(self):
        source = self.workbook()
        def mutate(parts):
            name = next(n for n in parts if n.endswith(".vml"))
            root = etree.fromstring(parts[name])
            shape = etree.SubElement(root, "{urn:schemas-microsoft-com:vml}shape", id="form-control-keep")
            etree.SubElement(shape, "{urn:schemas-microsoft-com:office:excel}ClientData", ObjectType="Button")
            parts[name] = etree.tostring(root)
        rewrite(source, mutate=mutate)
        original_vml = next(n for n in members(source) if n.endswith(".vml"))
        result = self.run_tool(paths=[source], action="clean", remove_comments=True)
        self.assertFalse(result["errors"])
        after = members(result["data_outputs"][0])
        self.assertIn(original_vml, after)
        root = etree.fromstring(after[original_vml])
        self.assertFalse(root.xpath(".//*[local-name()='ClientData' and @ObjectType='Note']"))
        self.assertTrue(root.xpath(".//*[local-name()='ClientData' and @ObjectType='Button']"))
        sheet = etree.fromstring(after["xl/worksheets/sheet1.xml"])
        self.assertTrue(sheet.xpath(".//*[local-name()='legacyDrawing']"))

    def test_external_links_reported_unchanged(self):
        source = self.document()
        def mutate(parts):
            name = "word/_rels/document.xml.rels"
            root = etree.fromstring(parts[name])
            etree.SubElement(root, "{" + op.REL + "}Relationship", Id="rIdRemote", Type=op.R + "/hyperlink", Target="https://example.invalid/data", TargetMode="External")
            parts[name] = etree.tostring(root)
        rewrite(source, mutate=mutate)
        result = self.run_tool(paths=[source], action="clean", clear_properties=True)
        self.assertEqual(members(source)["word/_rels/document.xml.rels"], members(result["data_outputs"][0])["word/_rels/document.xml.rels"])
        self.assertIn("外部链接", {i["category"] for i in self.report(result)["records"][0]["findings"]})

    def test_report_cells_never_become_formulas(self):
        result = self.run_tool()
        book = load_workbook(next(p for p in result["outputs"] if p.endswith(".xlsx")), data_only=False)
        try:
            found = [cell for row in book["检查明细"] for cell in row if isinstance(cell.value, str) and cell.value.startswith("=HYPERLINK")]
            self.assertTrue(found)
            self.assertTrue(all(c.data_type == "s" for c in found))
        finally:
            book.close()

    def test_signed_files_inspect_only_and_skip_modification(self):
        source = self.document()
        rewrite(source, {"_xmlsignatures/sig1.xml": b"<Signature/>"})
        inspect = self.run_tool(paths=[source])
        self.assertFalse(inspect["errors"])
        self.assertIn("数字签名", {i["category"] for i in self.report(inspect)["records"][0]["findings"]})
        clean = self.run_tool(paths=[source], action="clean", clear_properties=True)
        self.assertFalse(clean["data_outputs"])
        self.assertIn("数字签名", clean["errors"][0])

    def test_signature_detected_by_relationship_when_folder_is_nonstandard(self):
        source = self.document()
        def mutate(parts):
            root = etree.fromstring(parts["_rels/.rels"])
            etree.SubElement(root, "{" + op.REL + "}Relationship", Id="signature", Type="http://schemas.openxmlformats.org/package/2006/relationships/digital-signature/origin", Target="custom/signature-origin.sigs")
            parts["_rels/.rels"] = etree.tostring(root)
            parts["custom/signature-origin.sigs"] = b"inert-signature"
        rewrite(source, mutate=mutate)
        result = self.run_tool(paths=[source], action="clean", clear_properties=True)
        self.assertFalse(result["data_outputs"])
        self.assertIn("数字签名", result["errors"][0])

    def test_comment_cleanup_keeps_unrelated_fragment_relationship(self):
        source = self.document()
        def mutate(parts):
            name = "word/_rels/document.xml.rels"
            root = etree.fromstring(parts[name])
            etree.SubElement(root, "{" + op.REL + "}Relationship", Id="local-anchor", Type=op.R + "/hyperlink", Target="#bookmark")
            parts[name] = etree.tostring(root)
        rewrite(source, mutate=mutate)
        result = self.run_tool(paths=[source], action="clean", remove_comments=True)
        self.assertFalse(result["errors"])
        root = etree.fromstring(members(result["data_outputs"][0])["word/_rels/document.xml.rels"])
        self.assertTrue(root.xpath(".//*[@Target='#bookmark']"))

    def test_ppt_example_inspects_fonts_notes_hidden_ratio(self):
        source = self.deck()
        result = self.run_tool("ppt_prepare", [source])
        self.assertFalse(result["errors"])
        issues = self.report(result)["records"][0]["findings"]
        self.assertTrue({"页面比例", "幻灯片文字", "字号超出检查范围", "隐藏幻灯片", "演讲者备注"}.issubset({i["category"] for i in issues}))
        self.assertEqual(2, len([i for i in issues if i["category"] == "幻灯片文字"]))

    def test_ppt_prepare_changes_only_slides_preserves_masters_relations(self):
        source = self.deck()
        before = members(source)
        result = self.run_tool("ppt_prepare", [source], action="prepare", font_name="微软雅黑", font_size=22, add_page_numbers=True, page_number_start=10)
        self.assertFalse(result["errors"])
        after = members(result["data_outputs"][0])
        slide_names = {"ppt/slides/slide1.xml", "ppt/slides/slide2.xml"}
        self.assertEqual(set(before), set(after))
        self.assertTrue(all(before[n] == after[n] for n in before if n not in slide_names))
        for index, name in enumerate(sorted(slide_names)):
            root = etree.fromstring(after[name])
            marker = root.xpath(".//*[local-name()='sp'][*[local-name()='nvSpPr']/*[local-name()='cNvPr' and @descr='Qingjian page number v1']]")
            self.assertEqual(1, len(marker))
            self.assertEqual(str(10 + index), marker[0].xpath(".//*[local-name()='t']")[0].text)
            props = root.xpath(".//*[local-name()='rPr'][not(ancestor::*[local-name()='sp'][*[local-name()='nvSpPr']/*[local-name()='cNvPr' and @descr='Qingjian page number v1']])]")
            self.assertTrue(all(n.get("sz") == "2200" for n in props))
            self.assertTrue(all(n.xpath("./*[local-name()='ea' and @typeface='微软雅黑']") for n in props))
        self.assertEqual("0", etree.fromstring(after["ppt/slides/slide2.xml"]).get("show"))
        # Existing independent text boxes are updated, not duplicated.
        second = op.run("ppt_prepare", [Path(result["data_outputs"][0])], self.root / "second", {"action": "prepare", "add_page_numbers": True})
        self.assertFalse(second["errors"])
        root = etree.fromstring(members(second["data_outputs"][0])["ppt/slides/slide1.xml"])
        self.assertEqual(1, len(root.xpath(".//*[local-name()='cNvPr' and @descr='Qingjian page number v1']")))

    def test_ppt_notes_clean_retains_thumbnail_and_notes_master(self):
        source = self.deck()
        before = members(source)
        result = self.run_tool(paths=[source], action="clean", remove_ppt_notes=True)
        self.assertFalse(result["errors"])
        after = members(result["data_outputs"][0])
        for name in after:
            if name.startswith("ppt/notesSlides/") and name.endswith(".xml"):
                self.assertFalse(any(n.text for n in op._note_text_nodes(etree.fromstring(after[name]))))
            elif not name.startswith("ppt/notesSlides/"):
                self.assertEqual(before[name], after[name])

    def test_ppt_prepare_default_changes_no_parts(self):
        source = self.deck()
        result = self.run_tool("ppt_prepare", [source], action="prepare")
        self.assertEqual(members(source), members(result["data_outputs"][0]))
        self.assertEqual(0, result["changes"])

    def test_ppt_table_text_prepared_and_chart_smartart_bytes_retained(self):
        source = self.deck()
        def mutate(parts):
            root = etree.fromstring(parts["ppt/slides/slide1.xml"])
            tree = root.xpath(".//*[local-name()='spTree']")[0]
            table = etree.fromstring(f'<p:graphicFrame xmlns:p="{op.P}" xmlns:a="{op.A}"><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>表格内容保留</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
            tree.append(table)
            parts["ppt/slides/slide1.xml"] = etree.tostring(root)
            parts["ppt/charts/chart1.xml"] = b'<chart xmlns="urn:fixture"><text>chart original</text></chart>'
            parts["ppt/diagrams/data1.xml"] = b'<diagram xmlns="urn:fixture"><text>smartart original</text></diagram>'
        rewrite(source, mutate=mutate)
        before = members(source)
        result = self.run_tool("ppt_prepare", [source], action="prepare", font_name="宋体", font_size=18)
        self.assertFalse(result["errors"])
        after = members(result["data_outputs"][0])
        root = etree.fromstring(after["ppt/slides/slide1.xml"])
        self.assertTrue(root.xpath(".//*[local-name()='tc']//*[local-name()='rPr' and @sz='1800']/*[local-name()='ea' and @typeface='宋体']"))
        self.assertEqual(before["ppt/charts/chart1.xml"], after["ppt/charts/chart1.xml"])
        self.assertEqual(before["ppt/diagrams/data1.xml"], after["ppt/diagrams/data1.xml"])

    def test_word_example_without_optional_comment_api(self):
        paths, options = op.build_example("submission_preflight", self.root)
        result = self.run_tool(paths=[Path(paths[0])], **options)
        self.assertFalse(result["errors"])

    def test_cancel_preserves_complete_previous_results_and_no_temp_files(self):
        source = self.document()
        result = op.run("submission_preflight", [source], self.folder, {"action": "clean"}, cancel=lambda: True)
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["data_outputs"])
        self.assertTrue(Path(result["report"]).is_file())
        self.assertFalse(list(self.folder.glob(".qingjian-write-*")))

    def test_corrupt_file_is_reported_and_next_file_is_checked(self):
        bad = self.root / "bad.docx"
        bad.write_bytes(b"not a zip")
        result = self.run_tool(paths=[bad, self.document()])
        self.assertEqual(1, len(result["errors"]))
        self.assertEqual(["失败", "成功"], [r["status"] for r in self.report(result)["records"]])

    def test_zip_duplicate_traversal_entity_and_member_limits(self):
        source = self.document()
        original = source.read_bytes()
        cases = [("../outside.xml", b"<x/>"), ("word/unsafe.xml", b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///C:/Windows/win.ini">]><x>&e;</x>')]
        for name, data in cases:
            source.write_bytes(original)
            rewrite(source, {name: data})
            self.assertTrue(self.run_tool(paths=[source])["errors"])
        source.write_bytes(original)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(source, "a") as archive:
                archive.writestr("word/document.xml", b"<x/>")
        self.assertIn("重名", self.run_tool(paths=[source])["errors"][0])
        source.write_bytes(original)
        from unittest.mock import patch
        with patch.object(op, "MAX_MEMBERS", 1):
            self.assertIn("20000", self.run_tool(paths=[source])["errors"][0])

    def test_never_overwrites_same_named_output(self):
        source = self.document()
        first = self.run_tool(paths=[source], action="clean")
        first_path = Path(first["data_outputs"][0])
        first_bytes = first_path.read_bytes()
        second = self.run_tool(paths=[source], action="clean")
        self.assertNotEqual(first["data_outputs"], second["data_outputs"])
        self.assertEqual(first_bytes, first_path.read_bytes())

    def test_validate_rejects_invalid_values(self):
        for options in ({"font_size": float("nan")}, {"font_size": -1}, {"font_size": 0.5}, {"page_number_start": 1.5},
                        {"add_page_numbers": "yes"}, {"font_name": 123}, {"action": "unknown"}, {"min_font_size": 20, "max_font_size": 10}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                op.validate("ppt_prepare", options)


if __name__ == "__main__":
    unittest.main()
