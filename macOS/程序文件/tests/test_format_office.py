"""Content-level coverage of offline format directions and safe publication."""
from __future__ import annotations

import csv
import hashlib
import io
import os
from pathlib import Path
import threading
from unittest import mock
import zipfile
from xml.etree import ElementTree as ET

import pytest
from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from qingjian.core.common import CancelledError
from qingjian.core.format_office import convert, supports, targets_for


@pytest.fixture
def word(tmp_path):
    path = tmp_path / "办公材料.docx"
    document = Document()
    document.core_properties.title = "示例材料"
    document.add_heading("第一节 事项", 1)
    paragraph = document.add_paragraph("项目进度：")
    paragraph.add_run("已完成").bold = True
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "部门"
    table.cell(0, 1).text = "数量"
    table.cell(1, 0).text = "综合部"
    table.cell(1, 1).text = "42"
    picture = tmp_path / "插图.png"
    Image.new("RGBA", (80, 40), (210, 30, 60, 180)).save(picture)
    document.add_picture(str(picture), width=Inches(1))
    document.save(path)
    return path


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "table.pdf"
    document = canvas.Canvas(str(path), pagesize=(300, 300))
    for number in range(2):
        document.drawString(24, 270, f"Document page {number + 1}")
        for y in (200, 170, 140):
            document.line(30, y, 260, y)
        for x in (30, 150, 260):
            document.line(x, 140, x, 200)
        document.drawString(40, 180, "Department")
        document.drawString(160, 180, "Amount")
        document.drawString(40, 150, f"Office {number + 1}")
        document.drawString(160, 150, str(20 + number))
        document.showPage()
    document.save()
    return path


@pytest.fixture
def slides(tmp_path):
    path = tmp_path / "季度会议.pptx"
    presentation = Presentation()
    for number in range(2):
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(2))
        box.text = f"第{number + 1}页事项\n责任部门：综合部"
        table = slide.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(6), Inches(1)).table
        table.cell(0, 0).text = "指标"
        table.cell(0, 1).text = "数值"
        table.cell(1, 0).text = "完成项"
        table.cell(1, 1).text = str(number + 10)
    presentation.save(path)
    return path


def test_matrix_only_advertises_implemented_directions():
    assert supports("doc", "odt")
    assert supports(".pdf", ".xlsx")
    assert supports("https", "docx")
    assert supports(".epub", ".pdf")
    assert ".pptx" in targets_for(".png")
    assert not supports(".pdf", ".ppt")
    assert not supports(".epub", ".mobi")
    assert not supports(".docx", ".docx")
    assert not supports(".docx", ".xlsx")


@pytest.mark.parametrize("extension", [".html", ".txt", ".md", ".epub"])
def test_word_content_survives_reflow_formats(word, tmp_path, extension):
    before = hashlib.sha256(word.read_bytes()).hexdigest()
    target = tmp_path / ("结果" + extension)
    result = convert(word, target)
    assert result["outputs"] == [target]
    assert hashlib.sha256(word.read_bytes()).hexdigest() == before
    if extension == ".epub":
        with zipfile.ZipFile(target) as package:
            first = package.infolist()[0]
            assert first.filename == "mimetype"
            assert first.compress_type == zipfile.ZIP_STORED
            assert package.read("mimetype") == b"application/epub+zip"
            content = package.read("OEBPS/chapter.xhtml").decode("utf-8")
            ET.fromstring(content)
            ET.fromstring(package.read("OEBPS/content.opf"))
            assert len([name for name in package.namelist() if name.endswith(".png")]) == 1
    else:
        content = target.read_text(encoding="utf-8")
    assert "第一节 事项" in content
    assert "已完成" in content
    assert "综合部" in content
    assert "42" in content
    if extension == ".html":
        assert "data:image/png;base64," in content
        assert "<strong>已完成</strong>" in content
    if extension == ".md":
        assert "# 第一节 事项" in content
        assert "| 部门 | 数量 |" in content


def test_epub_roundtrip_preserves_reading_order_and_images(word, tmp_path):
    epub = tmp_path / "电子书.epub"
    convert(word, epub)
    docx = tmp_path / "电子书正文.docx"
    convert(epub, docx)
    document = Document(docx)
    assert any("已完成" in paragraph.text for paragraph in document.paragraphs)
    assert document.tables[0].cell(1, 0).text == "综合部"
    assert len(document.inline_shapes) == 1
    txt = tmp_path / "电子书正文.txt"
    convert(epub, txt)
    text = txt.read_text(encoding="utf-8")
    assert text.index("第一节 事项") < text.index("已完成") < text.index("综合部")


def test_epub_uses_spine_order_and_nested_relative_images(tmp_path):
    path = tmp_path / "order.epub"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("mimetype", "application/epub+zip")
        package.writestr("META-INF/container.xml", '<container><rootfile full-path="Book/book.opf"/></container>')
        package.writestr("Book/book.opf", '<package><metadata><title>顺序测试</title></metadata><manifest><item id="b" href="Text/b.xhtml" media-type="application/xhtml+xml"/><item id="a" href="Text/a.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
        package.writestr("Book/Text/b.xhtml", '<html><body><p>第二章</p></body></html>')
        package.writestr("Book/Text/a.xhtml", '<html><body><p>第一章</p></body></html>')
    target = tmp_path / "order.txt"
    convert(path, target)
    assert target.read_text(encoding="utf-8").index("第一章") < target.read_text(encoding="utf-8").index("第二章")


@pytest.mark.parametrize("member", ["../evil.xhtml", "/evil.xhtml", "C:/evil.xhtml", "folder\\evil.xhtml"])
def test_epub_rejects_unsafe_member_paths(tmp_path, member):
    path = tmp_path / "bad.epub"
    with zipfile.ZipFile(path, "w") as package:
        entry = zipfile.ZipInfo("placeholder")
        entry.filename = member
        package.writestr(entry, "forbidden")
    target = tmp_path / "out.txt"
    with pytest.raises(ValueError, match="路径"):
        convert(path, target)
    assert not target.exists()


def test_epub_rejects_encryption_and_entities(tmp_path):
    path = tmp_path / "protected.epub"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("META-INF/encryption.xml", '<encryption><EncryptedData/></encryption>')
    with pytest.raises(ValueError, match="加密"):
        convert(path, tmp_path / "protected.txt")
    path = tmp_path / "entity.epub"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("META-INF/container.xml", '<!DOCTYPE x [<!ENTITY a "abc">]><container>&a;</container>')
    with pytest.raises(ValueError, match="DTD"):
        convert(path, tmp_path / "entity.txt")
    utf16 = tmp_path / "entity16.epub"
    with zipfile.ZipFile(utf16, "w") as package:
        package.writestr("META-INF/container.xml", '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY a "abc">]><container>&a;</container>'.encode("utf-16"))
    with pytest.raises(ValueError, match="DTD"):
        convert(utf16, tmp_path / "entity16.txt")


def test_markdown_table_local_image_and_no_network(tmp_path):
    picture = tmp_path / "plot.png"
    Image.new("RGB", (20, 20), "red").save(picture)
    source = tmp_path / "说明.md"
    source.write_text("# 操作说明\n\n**重点事项**\n\n|项目|金额|\n|---|---|\n|甲|20|\n\n![图示](plot.png)\n\n![网络](https://example.com/pic.png)", encoding="utf-8")
    target = tmp_path / "说明.docx"
    with mock.patch("requests.Session.get", side_effect=AssertionError("offline conversion used network")):
        result = convert(source, target)
    document = Document(target)
    assert any("重点事项" in paragraph.text for paragraph in document.paragraphs)
    assert document.tables[0].cell(1, 1).text == "20"
    assert len(document.inline_shapes) == 1
    assert any("阻止" in warning for warning in result["warnings"])


def test_plain_text_is_not_executed_as_html(tmp_path):
    source = tmp_path / "文字.txt"
    source.write_text('<script>alert(1)</script>\n条件：x < 5 & y > 2', encoding="utf-8")
    target = tmp_path / "文字.html"
    convert(source, target)
    content = target.read_text(encoding="utf-8")
    assert "&lt;script&gt;" in content
    assert "<script>" not in content
    assert "x &lt; 5 &amp; y &gt; 2" in content


@pytest.mark.parametrize("extension", [".txt", ".md", ".markdown"])
def test_same_extension_utf8_preserves_all_whitespace(tmp_path, extension):
    source = tmp_path / ("编码" + extension)
    original = "  # 标题\r\n\r\n\t**原文字**  \r\n最后一行  "
    source.write_bytes(original.encode("gb18030"))
    target = tmp_path / ("UTF8" + extension)
    result = convert(source, target, {"encoding": "utf-8"})
    assert supports(extension, extension)
    assert extension not in targets_for(extension)
    assert target.read_bytes() == original.encode("utf-8")
    assert source.read_bytes() == original.encode("gb18030")
    assert any("UTF-8" in warning for warning in result["warnings"])


def test_csv_to_xlsx_preserves_identifiers_and_disables_formula_execution(tmp_path):
    source = tmp_path / "名册.csv"
    source.write_text('编号,内容\n00123,=HYPERLINK("https://example.com")\n00007,测试\n', encoding="utf-8")
    target = tmp_path / "名册.xlsx"
    convert(source, target)
    book = load_workbook(target)
    try:
        assert book.active["A2"].value == "00123"
        assert book.active["B2"].data_type == "s"
        assert book.active["B3"].value == "测试"
    finally:
        book.close()


def test_tsv_csv_roundtrip_quotes_and_chinese(tmp_path):
    source = tmp_path / "表格.tsv"
    source.write_text('编号\t说明\n001\t"含,逗号"\n002\t"第一行\n第二行"\n', encoding="utf-8", newline="\n")
    target = tmp_path / "表格.csv"
    convert(source, target)
    with target.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows == [["编号", "说明"], ["001", "含,逗号"], ["002", "第一行\n第二行"]]


def test_csv_html_handles_xlsx_without_dimension_tag(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("编号,事项\n00123,处理材料", encoding="utf-8")
    target = tmp_path / "source.html"
    convert(source, target)
    content = target.read_text(encoding="utf-8")
    assert "00123" in content
    assert "处理材料" in content


def test_xlsx_csv_warns_for_other_sheets_and_formula_cache(tmp_path):
    source = tmp_path / "多表.xlsx"
    book = Workbook()
    book.active.title = "账目"
    book.active.append(["项目", "金额"])
    book.active.append(["甲", "=1+2"])
    book.create_sheet("附件").append(["其他工作表"])
    book.save(source)
    book.close()
    target = tmp_path / "多表.csv"
    result = convert(source, target)
    assert "'=1+2" in target.read_text(encoding="utf-8-sig")
    assert any("其余 1" in warning for warning in result["warnings"])
    assert any("没有已计算值" in warning for warning in result["warnings"])
    target = tmp_path / "指定表.tsv"
    convert(source, target, {"sheet": "附件"})
    assert "其他工作表" in target.read_text(encoding="utf-8-sig")


def test_xlsx_html_contains_all_sheets(tmp_path):
    source = tmp_path / "workbook.xlsx"
    book = Workbook()
    book.active.title = "第一张表"
    book.active.append(["销售", 12])
    book.active.append(["公式", "=SUM(B1:B1)"])
    book.create_sheet("第二张表").append(["费用", 8])
    book.save(source)
    book.close()
    target = tmp_path / "workbook.html"
    result = convert(source, target)
    text = target.read_text(encoding="utf-8")
    assert all(value in text for value in ("第一张表", "第二张表", "销售", "费用", "12", "8"))
    assert "=SUM(B1:B1)" in text
    assert any("没有已计算值" in warning for warning in result["warnings"])


def test_xls_rejects_out_of_range_cells_before_engine_start(tmp_path):
    source = tmp_path / "large.xlsx"
    book = Workbook()
    book.active["A65537"] = "不能截断此行"
    book.save(source)
    book.close()
    target = tmp_path / "large.xls"
    with mock.patch("qingjian.core.format_office._lo", side_effect=AssertionError("must validate before engine")):
        with pytest.raises(ValueError, match="65536"):
            convert(source, target)
    assert not target.exists()


@pytest.mark.parametrize("extension", [".txt", ".docx"])
def test_slide_text_and_tables(slides, tmp_path, extension):
    target = tmp_path / ("会议记录" + extension)
    result = convert(slides, target)
    assert result["changes"] == 2
    text = target.read_text(encoding="utf-8") if extension == ".txt" else "\n".join(p.text for p in Document(target).paragraphs)
    assert all(value in text for value in ("第1页事项", "第2页事项", "责任部门：综合部", "完成项", "10", "11"))


@pytest.mark.parametrize("extension", [".png", ".jpg", ".tiff", ".pptx"])
def test_pdf_pages_are_all_retained(pdf, tmp_path, extension):
    target = tmp_path / ("页面" + extension)
    result = convert(pdf, target)
    assert result["changes"] == 2
    if extension in {".png", ".jpg"}:
        assert len(result["outputs"]) == 2
        assert result["outputs"][1].name == "页面_002" + extension
        with Image.open(result["outputs"][1]) as image:
            assert image.size == (600, 600)
            assert image.convert("L").getextrema()[0] < 100
    elif extension == ".tiff":
        with Image.open(target) as image:
            assert image.n_frames == 2
    else:
        presentation = Presentation(target)
        assert len(presentation.slides) == 2
        assert all(len(slide.shapes) == 1 and slide.shapes[0].shape_type == 13 for slide in presentation.slides)
        assert any("图片" in warning and "不可" in warning for warning in result["warnings"])


def test_pdf_text_word_and_table_content(pdf, tmp_path):
    txt = tmp_path / "内容.txt"
    convert(pdf, txt)
    assert "Document page 2" in txt.read_text(encoding="utf-8")
    docx = tmp_path / "内容.docx"
    convert(pdf, docx)
    document = Document(docx)
    assert any("Document page 1" in p.text for p in document.paragraphs)
    assert len(document.tables) == 2
    xlsx = tmp_path / "表格.xlsx"
    result = convert(pdf, xlsx)
    book = load_workbook(xlsx)
    try:
        assert result["changes"] == 2
        assert len(book.sheetnames) == 2
        assert book.worksheets[0]["A2"].value == "Office 1"
        assert book.worksheets[1]["B2"].value == "21"
    finally:
        book.close()
    csv_path = tmp_path / "表格.csv"
    convert(pdf, csv_path)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows[0][:3] == ["来源页码", "表格序号", "表内行号"]
    assert rows[-1] == ["2", "1", "2", "Office 2", "21"]


@pytest.mark.parametrize("extension", [".pdf", ".docx", ".pptx"])
def test_multiple_images_make_one_document(tmp_path, extension):
    sources = []
    for index, color in enumerate(("red", "blue")):
        source = tmp_path / f"pic{index}.png"
        Image.new("RGBA", (40, 80) if index else (80, 40), color).save(source)
        sources.append(source)
    target = tmp_path / ("合并" + extension)
    result = convert(sources[0], target, {"_sources": sources})
    assert result["changes"] == 2
    if extension == ".pdf":
        reader = PdfReader(target)
        assert len(reader.pages) == 2
        assert len(reader.pages[0].images) == 1
        reader.close()
    elif extension == ".docx":
        document = Document(target)
        assert len(document.inline_shapes) == 2
        assert len(document.sections) == 2
    else:
        assert len(Presentation(target).slides) == 2


def test_local_html_is_offline_and_scripts_are_removed(tmp_path):
    source = tmp_path / "网页.html"
    source.write_text('<html><body><h1>本地文章</h1><p>正文内容</p><script>bad()</script><img src="https://example.com/x.png"></body></html>', encoding="utf-8")
    target = tmp_path / "网页.txt"
    with mock.patch("requests.Session.get", side_effect=AssertionError("network")):
        result = convert(source, target)
    assert "正文内容" in target.read_text(encoding="utf-8")
    assert "bad()" not in target.read_text(encoding="utf-8")
    assert any("阻止" in warning for warning in result["warnings"])


def test_cancellation_and_existing_results_remain_untouched(word, tmp_path):
    event = threading.Event()
    event.set()
    target = tmp_path / "取消.html"
    with pytest.raises(CancelledError):
        convert(word, target, {"_cancel": event})
    assert not target.exists()
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        convert(word, target)
    assert target.read_text(encoding="utf-8") == "existing"


def test_multi_page_preflights_existing_later_output(pdf, tmp_path):
    target = tmp_path / "页面.png"
    other = tmp_path / "页面_002.png"
    other.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        convert(pdf, target)
    assert not target.exists()
    assert other.read_bytes() == b"existing"


def test_cancellation_during_render_publishes_no_page(pdf, tmp_path):
    from qingjian.core import conversion
    original = conversion._render_page
    event = threading.Event()
    def render(*args):
        original(*args)
        event.set()
    target = tmp_path / "cancel.png"
    with mock.patch("qingjian.core.conversion._render_page", side_effect=render):
        with pytest.raises(CancelledError):
            convert(pdf, target, {"_cancel": event})
    assert not target.exists()
    assert not (tmp_path / "cancel_002.png").exists()


def test_word_external_image_is_rejected(word, tmp_path):
    source = tmp_path / "external.docx"
    with zipfile.ZipFile(word) as original, zipfile.ZipFile(source, "w") as modified:
        for item in original.infolist():
            data = original.read(item)
            if item.filename == "word/_rels/document.xml.rels":
                root = ET.fromstring(data)
                ET.SubElement(root, "Relationship", {"Id": "ext99", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", "TargetMode": "External", "Target": "https://example.com/secret.png"})
                data = ET.tostring(root)
            modified.writestr(item, data)
    with pytest.raises(ValueError, match="外部"):
        convert(source, tmp_path / "external.html")


@pytest.mark.skipif(not os.environ.get("QINGJIAN_OFFICE_TEST_ENGINE"), reason="Explicit scratch LibreOffice engine required")
@pytest.mark.parametrize("extension", [".doc", ".rtf", ".odt", ".pdf"])
def test_native_word_directions_roundtrip(word, tmp_path, extension):
    options = {"libreoffice_path": os.environ["QINGJIAN_OFFICE_TEST_ENGINE"]}
    target = tmp_path / ("converted" + extension)
    convert(word, target, options)
    if extension == ".pdf":
        assert len(PdfReader(target).pages) >= 1
    else:
        back = tmp_path / "back.docx"
        convert(target, back, options)
        assert any("已完成" in p.text for p in Document(back).paragraphs)
        assert Document(back).tables[0].cell(1, 0).text == "综合部"


@pytest.mark.skipif(not os.environ.get("QINGJIAN_OFFICE_TEST_ENGINE"), reason="Explicit scratch LibreOffice engine required")
@pytest.mark.parametrize("extension", [".xls", ".ods", ".pdf"])
def test_native_sheet_directions_roundtrip(tmp_path, extension):
    options = {"libreoffice_path": os.environ["QINGJIAN_OFFICE_TEST_ENGINE"]}
    source = tmp_path / "input.csv"
    source.write_text("编号,金额\n00123,20\n00007,10", encoding="utf-8")
    target = tmp_path / ("converted" + extension)
    convert(source, target, options)
    if extension == ".pdf":
        assert len(PdfReader(target).pages) >= 1
    else:
        back = tmp_path / "back.xlsx"
        convert(target, back, options)
        book = load_workbook(back)
        assert book.active["A2"].value == "00123"
        assert str(book.active["B3"].value) == "10"
        book.close()


@pytest.mark.skipif(not os.environ.get("QINGJIAN_OFFICE_TEST_ENGINE"), reason="Explicit scratch LibreOffice engine required")
@pytest.mark.parametrize("extension", [".ppt", ".odp", ".pdf", ".png"])
def test_native_slides_roundtrip(slides, tmp_path, extension):
    options = {"libreoffice_path": os.environ["QINGJIAN_OFFICE_TEST_ENGINE"]}
    target = tmp_path / ("converted" + extension)
    result = convert(slides, target, options)
    if extension == ".pdf":
        assert len(PdfReader(target).pages) == 2
    elif extension == ".png":
        assert len(result["outputs"]) == 2
    else:
        back = tmp_path / "back.pptx"
        convert(target, back, options)
        assert len(Presentation(back).slides) == 2
        text = tmp_path / "back.txt"
        convert(back, text)
        assert "责任部门：综合部" in text.read_text(encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("QINGJIAN_OFFICE_TEST_ENGINE"), reason="Explicit scratch LibreOffice engine required")
def test_native_markdown_and_epub_pdf(tmp_path):
    source = tmp_path / "help.md"
    source.write_text("# 办公说明\n\n步骤一：添加材料。\n\n步骤二：检查结果。", encoding="utf-8")
    epub = tmp_path / "help.epub"
    convert(source, epub)
    options = {"libreoffice_path": os.environ["QINGJIAN_OFFICE_TEST_ENGINE"]}
    for item in (source, epub):
        target = tmp_path / (item.suffix[1:] + ".pdf")
        convert(item, target, options)
        reader = PdfReader(target)
        text = "\n".join(page.extract_text() for page in reader.pages)
        assert "步骤" in text
        reader.close()
