"""Focused regression tests for DOCX structure and confidentiality boundaries."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Inches, Pt

from qingjian.core.word_ops import process_word


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2ioAAAAASUVORK5CYII="
)


def _package(path: Path) -> dict[str, bytes]:
    with ZipFile(path) as package:
        return {name: package.read(name) for name in package.namelist()}


def _rewrite(path: Path, entries: dict[str, bytes]) -> None:
    # This is a fixture constructor; atomic publishing is exercised separately
    # against the production function. Keep fixture data out of deliverables.
    with ZipFile(path, "w", ZIP_DEFLATED) as target:
        for name, value in entries.items():
            target.writestr(name, value)


def _attach_part(path: Path, name: str, xml: bytes, relation: str, content_type: str) -> None:
    entries = _package(path)
    entries[name] = xml
    rels = ET.fromstring(entries["word/_rels/document.xml.rels"])
    ET.SubElement(rels, "{" + _RELS + "}Relationship", {
        "Id": "rIdTest" + str(len(rels)), "Type": relation,
        "Target": name.removeprefix("word/"),
    })
    entries["word/_rels/document.xml.rels"] = ET.tostring(rels, encoding="utf-8", xml_declaration=True)
    types = ET.fromstring(entries["[Content_Types].xml"])
    ET.SubElement(types, "{" + _TYPES + "}Override", {"PartName": "/" + name, "ContentType": content_type})
    entries["[Content_Types].xml"] = ET.tostring(types, encoding="utf-8", xml_declaration=True)
    _rewrite(path, entries)


def _all_text(path: Path, part: str = "word/document.xml") -> str:
    root = ET.fromstring(_package(path)[part])
    return "".join(node.text or "" for node in root.iter("{" + _W + "}t"))


class WordOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="word-qa-", dir=os.environ.get("QINGJIAN_TEST_TMP"))
        self.folder = Path(self.temporary.name)
        self.source = self.folder / "原稿.docx"
        self.target = self.folder / "处理后.docx"

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, document) -> None:
        document.save(self.source)

    def test_cross_run_replacement_preserves_first_format_image_and_hyperlink(self):
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run("姓名：张").bold = True
        paragraph.add_run("三；照片：").italic = True
        paragraph.add_run().add_picture(BytesIO(_PNG), width=Inches(0.1))
        link = OxmlElement("w:hyperlink")
        link.set(qn("r:id"), document.part.relate_to("https://example.test", RT.HYPERLINK, is_external=True))
        for text in ("联系", "入口"):
            run = OxmlElement("w:r")
            node = OxmlElement("w:t")
            node.text = text
            run.append(node)
            link.append(run)
        paragraph._p.append(link)
        self.save(document)
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = process_word(self.source, self.target, {"replacements": [
            {"find": "张三", "replace": "李四"}, {"find": "联系入口", "replace": "访问网站"},
        ]})
        modified = Document(self.target)
        self.assertEqual(result["changes"], 2)
        self.assertIn("姓名：李四；照片：访问网站", _all_text(self.target))
        self.assertTrue(modified.paragraphs[0].runs[0].bold)
        self.assertTrue(modified.paragraphs[0].runs[1].italic)
        self.assertEqual(len(modified.inline_shapes), 1)
        self.assertEqual(len(list(modified.element.iter(qn("w:hyperlink")))), 1)
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_tables_nested_tables_headers_footers_and_textbox_processed_once(self):
        document = Document()
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "秘密"
        nested = table.cell(0, 0).add_table(rows=1, cols=1)
        nested.cell(0, 0).text = "秘密"
        document.sections[0].header.paragraphs[0].text = "秘密"
        document.sections[0].footer.paragraphs[0].text = "秘密"
        paragraph = document.add_paragraph()
        paragraph._p.append(parse_xml((
            '<w:r xmlns:w="' + _W + '" xmlns:v="urn:schemas-microsoft-com:vml">'
            '<w:pict><v:shape id="TestTextBox"><v:textbox><w:txbxContent>'
            '<w:p><w:r><w:t>秘密</w:t></w:r></w:p>'
            '</w:txbxContent></v:textbox></v:shape></w:pict></w:r>'
        ).encode()))
        self.save(document)
        result = process_word(self.source, self.target, {"replacements": [{"find": "秘密", "replace": "秘密已改"}]})
        self.assertEqual(result["changes"], 5)
        self.assertEqual(_all_text(self.target).count("秘密已改"), 3)
        self.assertNotIn("秘密已改已改", _all_text(self.target))
        self.assertIn("秘密已改", _all_text(self.target, "word/header1.xml"))
        self.assertIn("秘密已改", _all_text(self.target, "word/footer1.xml"))

    def test_builtin_masks_cross_runs_and_custom_sensitive(self):
        document = Document()
        paragraph = document.add_paragraph()
        for text in ("138", "1234", "5678；+86 139 1234 5678；", "11010119900307123X", "；",
                     "130503670401001", "；alice+tag@example.com；客户张三"):
            paragraph.add_run(text)
        self.save(document)
        result = process_word(self.source, self.target, {
            "mask_phone": True, "mask_id": True, "mask_email": True,
            "custom_sensitive": ["张三"], "clean_metadata": True,
        })
        text = _all_text(self.target)
        self.assertIn("138****5678", text)
        self.assertIn("+86 139****5678", text)
        self.assertIn("110101********123X", text)
        self.assertIn("130503******001", text)
        self.assertIn("***@***", text)
        self.assertIn("客户***", text)
        self.assertTrue(any("人工复核" in warning for warning in result["warnings"]))
        self.assertNotIn("11010119900307123X", repr(result))
        self.assertNotIn("alice+tag", repr(result))

    def test_regex_groups_and_multiple_matches_in_same_run(self):
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run("姓名张")
        paragraph.add_run("三，姓名张三")
        self.save(document)
        result = process_word(self.source, self.target, {
            "replacements": [{"find": r"姓名张(三)", "replace": r"客户李\1", "regex": True}],
        })
        self.assertEqual(_all_text(self.target), "客户李三，客户李三")
        self.assertEqual(result["changes"], 2)

    def test_replacement_newline_becomes_word_break(self):
        document = Document()
        document.add_paragraph("甲乙")
        self.save(document)
        process_word(self.source, self.target, {"replacements": [{"find": "甲乙", "replace": "甲\n乙\t丙"}]})
        paragraph = Document(self.target).paragraphs[0]
        self.assertEqual(paragraph.text, "甲\n乙\t丙")

    def test_delete_and_remove_blanks_keep_images_page_break_and_valid_cells(self):
        document = Document()
        document.add_paragraph("删除这一段")
        document.add_paragraph("   ")
        document.add_paragraph().add_run().add_picture(BytesIO(_PNG), width=Inches(0.1))
        document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        document.add_paragraph("保留正文")
        document.add_table(rows=1, cols=1).cell(0, 0).text = "删除这一段"
        self.save(document)
        process_word(self.source, self.target, {
            "delete_paragraphs": ["删除"], "remove_blank_paragraphs": True,
        })
        modified = Document(self.target)
        self.assertNotIn("删除", _all_text(self.target))
        self.assertEqual(len(modified.inline_shapes), 1)
        self.assertEqual(len(list(modified.element.iter(qn("w:br")))), 1)
        self.assertEqual(modified.tables[0].cell(0, 0)._tc[-1].tag, qn("w:p"))
        self.assertEqual(modified.paragraphs[-1].text, "保留正文")

    def test_formatting_preserves_heading_and_run_bold(self):
        document = Document()
        heading = document.add_heading("标题", level=1)
        heading.runs[0].font.size = Pt(20)
        body = document.add_paragraph("正文")
        body.runs[0].bold = True
        self.save(document)
        process_word(self.source, self.target, {
            "format_enabled": True, "font_name": "仿宋", "font_size": 14,
            "line_spacing": 2, "margin_cm": 2.5, "preserve_headings": True,
        })
        modified = Document(self.target)
        self.assertEqual(modified.paragraphs[0].runs[0].font.size.pt, 20)
        self.assertEqual(modified.paragraphs[1].runs[0].font.size.pt, 14)
        self.assertTrue(modified.paragraphs[1].runs[0].bold)
        self.assertEqual(modified.paragraphs[1].runs[0]._r.rPr.rFonts.get(qn("w:eastAsia")), "仿宋")
        self.assertAlmostEqual(modified.sections[0].left_margin.cm, 2.5, places=2)

    def test_comments_removed_at_package_level_and_namespaces_retained(self):
        document = Document()
        paragraph = document.add_paragraph("正文")
        for tag in ("commentRangeStart", "commentRangeEnd"):
            marker = OxmlElement("w:" + tag)
            marker.set(qn("w:id"), "0")
            paragraph._p.append(marker)
        run = paragraph.add_run()
        marker = OxmlElement("w:commentReference")
        marker.set(qn("w:id"), "0")
        run._r.append(marker)
        document.core_properties.author = "敏感作者"
        document.core_properties.subject = "敏感项目"
        self.save(document)
        comments = ('<w:comments xmlns:w="' + _W + '"><w:comment w:id="0" w:author="敏感作者">'
                    '<w:p><w:r><w:t>批注的敏感内容</w:t></w:r></w:p></w:comment></w:comments>').encode()
        _attach_part(self.source, "word/comments.xml", comments, RT.COMMENTS,
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml")
        with self.assertRaisesRegex(ValueError, "未删除的批注"):
            process_word(self.source, self.target, {"mask_phone": True})
        result = process_word(self.source, self.target, {"remove_comments": True, "clean_metadata": True})
        self.assertGreater(result["changes"], 0)
        entries = _package(self.target)
        self.assertNotIn("word/comments.xml", entries)
        self.assertNotIn(b"comments.xml", entries["word/_rels/document.xml.rels"])
        self.assertNotIn(b"commentReference", entries["word/document.xml"])
        self.assertNotIn("敏感作者".encode(), entries["docProps/core.xml"])
        root = parse_xml(entries["word/document.xml"])
        ignorable = root.get("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable", "")
        self.assertTrue(all(prefix in root.nsmap for prefix in ignorable.split()))

    def test_revision_warns_and_masking_refuses_without_partial_output(self):
        document = Document()
        paragraph = document.add_paragraph("正文")
        insertion = OxmlElement("w:ins")
        insertion.set(qn("w:id"), "1")
        insertion.set(qn("w:author"), "作者")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "13812345678"
        run.append(text)
        insertion.append(run)
        paragraph._p.append(insertion)
        self.save(document)
        result = process_word(self.source, self.target, {"format_enabled": True})
        self.assertTrue(any("修订" in warning for warning in result["warnings"]))
        self.target.unlink()
        with self.assertRaisesRegex(ValueError, "修订"):
            process_word(self.source, self.target, {"mask_phone": True})
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.folder.glob(".qingjian-*")))

    def test_comment_only_attachments_removed_and_shared_images_retained(self):
        document = Document()
        document.add_paragraph("正文").add_run().add_picture(BytesIO(_PNG), width=Inches(0.1))
        self.save(document)
        comments = ('<w:comments xmlns:w="' + _W + '"><w:comment w:id="0" w:author="作者">'
                    '<w:p><w:r><w:t>批注</w:t></w:r></w:p></w:comment></w:comments>').encode()
        _attach_part(self.source, "word/comments.xml", comments, RT.COMMENTS,
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml")
        entries = _package(self.source)
        entries["word/media/comment-only.png"] = _PNG
        entries["word/_rels/comments.xml.rels"] = (
            '<Relationships xmlns="' + _RELS + '">'
            '<Relationship Id="rIdCommentOnly" Type="' + RT.IMAGE + '" Target="media/comment-only.png"/>'
            '<Relationship Id="rIdShared" Type="' + RT.IMAGE + '" Target="media/image1.png"/>'
            '</Relationships>'
        ).encode()
        _rewrite(self.source, entries)
        process_word(self.source, self.target, {"remove_comments": True})
        result_entries = _package(self.target)
        self.assertNotIn("word/comments.xml", result_entries)
        self.assertNotIn("word/_rels/comments.xml.rels", result_entries)
        self.assertNotIn("word/media/comment-only.png", result_entries)
        self.assertIn("word/media/image1.png", result_entries)
        self.assertEqual(len(Document(self.target).inline_shapes), 1)

    def test_footnote_masking_refuses(self):
        document = Document()
        document.add_paragraph("正文")
        self.save(document)
        notes = ('<w:footnotes xmlns:w="' + _W + '"><w:footnote w:id="1">'
                 '<w:p><w:r><w:t>脚注敏感内容</w:t></w:r></w:p></w:footnote></w:footnotes>').encode()
        _attach_part(self.source, "word/footnotes.xml", notes, RT.FOOTNOTES,
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml")
        with self.assertRaisesRegex(ValueError, "脚注或尾注"):
            process_word(self.source, self.target, {"custom_sensitive": ["敏感"]})
        self.assertFalse(self.target.exists())

    def test_existing_target_and_source_never_overwritten(self):
        document = Document()
        document.add_paragraph("原文")
        self.save(document)
        before = self.source.read_bytes()
        with self.assertRaises(ValueError):
            process_word(self.source, self.source, {})
        self.target.write_bytes(b"existing result")
        with self.assertRaises(FileExistsError):
            process_word(self.source, self.target, {})
        self.assertEqual(self.target.read_bytes(), b"existing result")
        self.assertEqual(self.source.read_bytes(), before)

    def test_invalid_and_zero_width_regex_do_not_publish(self):
        document = Document()
        document.add_paragraph("原文")
        self.save(document)
        for find in ("[", "(?=原)"):
            with self.subTest(find=find), self.assertRaises(ValueError):
                process_word(self.source, self.target, {"replacements": [{"find": find, "replace": "x", "regex": True}]})
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
