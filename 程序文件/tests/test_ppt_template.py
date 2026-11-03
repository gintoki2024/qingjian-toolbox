from __future__ import annotations

import base64
import csv
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from lxml import etree
from openpyxl import Workbook

from qingjian.core.document_ops import run_documents
from qingjian.core.ppt_template import fill_template, template_fields


P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = f'xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"'
GROUP = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
         '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
         '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')


def runs(*values):
    return "".join(f'<a:r><a:rPr lang="zh-CN" b="1" sz="2000"/><a:t>{escape(value)}</a:t></a:r>' for value in values)


def shape(identifier, text):
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{identifier}" name="Text {identifier}"/>'
            '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="300000" y="300000"/><a:ext cx="4000000" cy="1000000"/>'
            '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p>' + text + '</a:p></p:txBody></p:sp>')


def part(root, children, attributes=""):
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:{root} {NS} {attributes}>{children}</p:{root}>'.encode()


def relations(*values):
    return (f'<Relationships xmlns="{REL}">' + "".join(
        f'<Relationship Id="{identifier}" Type="{R}/{kind}" Target="{target}"/>'
        for identifier, kind, target in values) + '</Relationships>').encode()


class PptTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.template = self.root / "template.pptx"
        self.folder = self.root / "results"

    def tearDown(self):
        self.temporary.cleanup()

    def deck(self, text=None, extras=None, master="固定母版", layout="固定版式", notes="固定备注"):
        slide = shape(2, runs("{{姓名}}") if text is None else text)
        types = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 '<Default Extension="png" ContentType="image/png"/>'
                 '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                 '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
                 '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                 '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
                 '<Override PartName="/ppt/notesSlides/notesSlide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>'
                 '</Types>').encode()
        files = {
            "[Content_Types].xml": types,
            "_rels/.rels": relations(("rId1", "officeDocument", "ppt/presentation.xml")),
            "ppt/presentation.xml": part("presentation", '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rIdM"/></p:sldMasterIdLst>'
                                         '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
                                         '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'),
            "ppt/_rels/presentation.xml.rels": relations(("rId1", "slide", "slides/slide1.xml"), ("rIdM", "slideMaster", "slideMasters/slideMaster1.xml")),
            "ppt/slides/slide1.xml": part("sld", '<p:cSld><p:spTree>' + GROUP + slide + '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'),
            "ppt/slides/_rels/slide1.xml.rels": relations(("rIdL", "slideLayout", "../slideLayouts/slideLayout1.xml"), ("rIdN", "notesSlide", "../notesSlides/notesSlide1.xml")),
            "ppt/slideLayouts/slideLayout1.xml": part("sldLayout", '<p:cSld><p:spTree>' + GROUP + shape(2, runs(layout)) + '</p:spTree></p:cSld>', 'type="blank" preserve="1"'),
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": relations(("rIdM", "slideMaster", "../slideMasters/slideMaster1.xml")),
            "ppt/slideMasters/slideMaster1.xml": part("sldMaster", '<p:cSld><p:spTree>' + GROUP + shape(2, runs(master)) + '</p:spTree></p:cSld>'
                                                    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
                                                    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rIdL"/></p:sldLayoutIdLst><p:txStyles/>'),
            "ppt/slideMasters/_rels/slideMaster1.xml.rels": relations(("rIdL", "slideLayout", "../slideLayouts/slideLayout1.xml")),
            "ppt/notesSlides/notesSlide1.xml": part("notes", '<p:cSld><p:spTree>' + GROUP + shape(2, runs(notes)) + '</p:spTree></p:cSld>'),
            "ppt/notesSlides/_rels/notesSlide1.xml.rels": relations(("rIdS", "slide", "../slides/slide1.xml")),
            "ppt/media/image1.png": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJ0cAAAAASUVORK5CYII="),
            "docProps/core.xml": b'<core xmlns="urn:fixture"><unchanged>metadata</unchanged></core>',
            "customXml/item1.xml": b'<fixture attr="kept">untouched payload</fixture>',
        }
        files.update(extras or {})
        with zipfile.ZipFile(self.template, "w", zipfile.ZIP_DEFLATED) as output:
            output.comment = b"original comment"
            for name, blob in files.items():
                output.writestr(name, blob)
        return self.template

    def csv(self, rows):
        path = self.root / "rows.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            csv.writer(output).writerows(rows)
        return path

    def run_fill(self, source, **options):
        return run_documents("template_fill", [source], self.folder, {"template_path": str(self.template), **options})

    def xml(self, path, name="ppt/slides/slide1.xml"):
        with zipfile.ZipFile(path) as archive:
            return etree.fromstring(archive.read(name))

    def test_text_boxes_tables_groups_cross_runs_and_unmodified_members_preserved(self):
        table = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="5" name="Table"/>'
                 '<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="0" y="0"/>'
                 '<a:ext cx="4000000" cy="1000000"/></p:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
                 '<a:tbl><a:tblPr/><a:tblGrid><a:gridCol w="4000000"/></a:tblGrid><a:tr h="1000000"><a:tc>'
                 '<a:txBody><a:bodyPr/><a:lstStyle/><a:p>' + runs("部门 {{部", "门}}") +
                 '</a:p></a:txBody><a:tcPr/></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
        self.deck(runs("您好 {{姓", "名}} / {{姓名}}，编号 {{编号}}"))
        with zipfile.ZipFile(self.template) as archive:
            files = {entry.filename: archive.read(entry) for entry in archive.infolist()}
        root = etree.fromstring(files["ppt/slides/slide1.xml"])
        tree = root.find("{" + P + "}cSld/{" + P + "}spTree")
        tree.append(etree.fromstring(f'<p:grpSp {NS}>' + GROUP + shape(3, runs("组内 {{编号}}")) + '</p:grpSp>'))
        tree.append(etree.fromstring(f'<wrapper {NS}>' + table + '</wrapper>')[0])
        files["ppt/slides/slide1.xml"] = etree.tostring(root)
        with zipfile.ZipFile(self.template, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.comment = b"original comment"
            for name, data in files.items():
                archive.writestr(name, data)
        before = self.template.read_bytes()
        source = self.csv([["姓名", "编号", "部门"], ["张三 & <同事>", "0000123", "战略规划"]])
        source_before = source.read_bytes()
        result = self.run_fill(source)
        self.assertFalse(result["errors"], result["errors"])
        self.assertEqual(result["changes"], 1)
        output = Path(result["data_outputs"][0])
        texts = "".join(self.xml(output).itertext())
        self.assertIn("您好 张三 & <同事> / 张三 & <同事>，编号 0000123", texts)
        self.assertIn("组内 0000123", texts)
        self.assertIn("部门 战略规划", texts)
        self.assertEqual(self.xml(output).find(".//{" + A + "}rPr").get("b"), "1")
        with zipfile.ZipFile(self.template) as original, zipfile.ZipFile(output) as filled:
            self.assertEqual(original.namelist(), filled.namelist())
            self.assertEqual(filled.comment, original.comment)
            for entry in original.infolist():
                if entry.filename != "ppt/slides/slide1.xml":
                    self.assertEqual(filled.read(entry.filename), original.read(entry), entry.filename)
            self.assertIsNone(filled.testzip())
        self.assertEqual(before, self.template.read_bytes())
        self.assertEqual(source_before, source.read_bytes())

    def test_master_layout_and_notes_are_filled(self):
        self.deck(master="母版 {{部门}}", layout="版式 {{编号}}", notes="备注 {{姓名}}")
        self.assertEqual(template_fields(self.template), {"姓名", "部门", "编号"})
        target = self.root / "filled.pptx"
        self.assertEqual(fill_template(self.template, target, {"姓名": "李四", "部门": "财务", "编号": "001"}), 4)
        for name, expected in (("ppt/slideMasters/slideMaster1.xml", "母版 财务"),
                               ("ppt/slideLayouts/slideLayout1.xml", "版式 001"),
                               ("ppt/notesSlides/notesSlide1.xml", "备注 李四")):
            self.assertIn(expected, "".join(self.xml(target, name).itertext()))

    def test_valid_part_names_do_not_have_to_follow_powerpoint_numbering(self):
        self.deck()
        with zipfile.ZipFile(self.template) as archive:
            files = {entry.filename: archive.read(entry) for entry in archive.infolist()}
        files["ppt/slides/intro.xml"] = files.pop("ppt/slides/slide1.xml")
        files["ppt/slides/_rels/intro.xml.rels"] = files.pop("ppt/slides/_rels/slide1.xml.rels")
        files["ppt/_rels/presentation.xml.rels"] = files["ppt/_rels/presentation.xml.rels"].replace(b"slides/slide1.xml", b"slides/intro.xml")
        files["[Content_Types].xml"] = files["[Content_Types].xml"].replace(b"slides/slide1.xml", b"slides/intro.xml")
        files["ppt/notesSlides/_rels/notesSlide1.xml.rels"] = files["ppt/notesSlides/_rels/notesSlide1.xml.rels"].replace(b"slides/slide1.xml", b"slides/intro.xml")
        with zipfile.ZipFile(self.template, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        target = self.root / "filled.pptx"
        fill_template(self.template, target, {"姓名": "客户甲"})
        self.assertIn("客户甲", "".join(self.xml(target, "ppt/slides/intro.xml").itertext()))

    def test_excel_rows_missing_columns_blank_policy_and_unique_names(self):
        self.deck(runs("{{姓名}}/{{部门}}/{{编号}}/{{序号}}"))
        source = self.root / "data.xlsx"
        book = Workbook()
        book.active.append(["姓名", "编号"])
        book.active.append(["客户", 123])
        book.active["B2"].number_format = "000000"
        book.active.append(["客户", "0009"])
        book.save(source); book.close()
        result = self.run_fill(source)
        self.assertFalse(result["data_outputs"])
        self.assertTrue(all("部门" in error for error in result["errors"]))
        result = self.run_fill(source, missing_policy="blank", filename_template="{姓名}.pptx")
        self.assertFalse(result["errors"], result["errors"])
        outputs = [Path(path) for path in result["data_outputs"]]
        self.assertEqual(len(set(outputs)), 2)
        self.assertTrue(all(path.suffix == ".pptx" and not path.name.endswith(".pptx.pptx") for path in outputs))
        self.assertIn("客户//000123/1", "".join(self.xml(outputs[0]).itertext()))
        self.assertIn("客户//0009/2", "".join(self.xml(outputs[1]).itertext()))
        self.assertTrue(any("部门" in notice for notice in result["warnings"]))

    def test_multiline_values_use_drawingml_breaks_and_keep_text_style(self):
        self.deck(runs("说明 {{内容}} 结束"))
        source = self.csv([["姓名", "内容"], ["甲", "第一行\r\n第二行\t数据\n第三行"]])
        result = self.run_fill(source)
        self.assertFalse(result["errors"], result["errors"])
        root = self.xml(result["data_outputs"][0])
        paragraph = root.find(".//{" + A + "}p")
        self.assertEqual(len(paragraph.findall("{" + A + "}br")), 2)
        self.assertEqual([node.text for node in paragraph.iter("{" + A + "}t")], ["说明 第一行", "第二行\t数据", "第三行 结束"])
        self.assertTrue(all(node.get("b") == "1" for node in paragraph.iter("{" + A + "}rPr")))

    def test_unsupported_chart_smartart_and_dynamic_fields_are_reported(self):
        source = self.csv([["姓名", "部门"], ["甲", "财务"]])
        cases = [
            {"ppt/charts/chart1.xml": f'<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:v>{{{{部</c:v><c:v>门}}}}</c:v></c:chartSpace>'.encode()},
            {"ppt/diagrams/data1.xml": f'<a:root xmlns:a="{A}"><a:t>{{{{姓名}}}}</a:t></a:root>'.encode()},
        ]
        for extras in cases:
            with self.subTest(part=next(iter(extras))):
                self.deck(extras=extras)
                result = self.run_fill(source)
                self.assertFalse(result["data_outputs"])
                self.assertTrue(any("无法填充" in error and next(iter(extras)) in error for error in result["errors"]))
        self.deck('<a:fld id="{00112233-4455-6677-8899-AABBCCDDEEFF}" type="slidenum"><a:rPr/><a:t>{{姓名}}</a:t></a:fld>')
        result = self.run_fill(source)
        self.assertFalse(result["data_outputs"])
        self.assertTrue(any("动态字段" in error for error in result["errors"]))

    def test_unfilled_dynamic_fields_are_preserved(self):
        field = '<a:fld id="{00112233-4455-6677-8899-AABBCCDDEEFF}" type="slidenum"><a:rPr/><a:t>1</a:t></a:fld>'
        self.deck(runs("{{姓名}}") + field)
        target = self.root / "filled.pptx"
        fill_template(self.template, target, {"姓名": "客户"})
        original_field = self.xml(self.template).find(".//{" + A + "}fld")
        result_field = self.xml(target).find(".//{" + A + "}fld")
        self.assertEqual(etree.tostring(original_field), etree.tostring(result_field))

    def test_expression_code_bad_delimiters_and_cross_break_fields_rejected(self):
        source = self.csv([["姓名"], ["甲"]])
        for text in (runs("{{7 * 7}}"), runs("{{姓名.__class__}}"), runs("{% for x in data %}{{姓名}}{% endfor %}"),
                     runs("{{__import__('os')}}"), runs("{{姓名"), runs("{{姓") + '<a:br/>' + runs("名}}")):
            with self.subTest(text=text):
                self.deck(text)
                result = self.run_fill(source)
                self.assertTrue(result["errors"])
                self.assertFalse(result["data_outputs"])

    def test_data_is_literal_even_when_it_contains_template_syntax(self):
        self.deck()
        target = self.root / "filled.pptx"
        value = '{{__import__("os")}} & <tag> {% ignore %}'
        fill_template(self.template, target, {"姓名": value})
        self.assertIn(value, "".join(self.xml(target).itertext()))

    def test_existing_outputs_template_and_invalid_data_are_protected(self):
        self.deck()
        before = self.template.read_bytes()
        with self.assertRaises(ValueError):
            fill_template(self.template, self.template, {"姓名": "甲"})
        target = self.root / "existing.pptx"
        target.write_bytes(b"existing document")
        with self.assertRaises(ValueError):
            fill_template(self.template, target, {"姓名": "甲"})
        self.assertEqual(target.read_bytes(), b"existing document")
        with self.assertRaises(ValueError):
            fill_template(self.template, self.root / "invalid.pptx", {"姓名": "bad\x01text"})
        self.assertFalse((self.root / "invalid.pptx").exists())
        self.assertEqual(self.template.read_bytes(), before)

    def test_slide_relationship_errors_duplicate_members_and_dtd_rejected(self):
        self.deck(extras={"ppt/_rels/presentation.xml.rels": relations(("rId1", "slide", "slides/missing.xml"))})
        with self.assertRaisesRegex(ValueError, "缺失"):
            template_fields(self.template)
        self.deck()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.template, "a") as archive:
                archive.writestr("ppt/slides/slide1.xml", b"duplicate")
        with self.assertRaisesRegex(ValueError, "重名"):
            template_fields(self.template)
        self.deck(extras={"ppt/charts/chart1.xml": b'<!DOCTYPE d [<!ENTITY x "data">]><d>&x;</d>'})
        with self.assertRaisesRegex(ValueError, "DTD"):
            template_fields(self.template)

    def test_pptx_pdf_combination_and_old_ppt_provide_actionable_errors(self):
        self.deck()
        source = self.csv([["姓名"], ["甲"]])
        result = self.run_fill(source, output_pdf=True)
        self.assertTrue(any("仅支持 Word" in error for error in result["errors"]))
        self.assertFalse(result["data_outputs"])
        old = self.root / "legacy.ppt"
        old.write_bytes(b"legacy binary")
        result = run_documents("template_fill", [source], self.folder, {"template_path": str(old)})
        self.assertTrue(any("另存为 .pptx" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
