"""Real-file regressions for practical local office conversion behavior."""
import csv
import json
import re
import threading
import zipfile
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image
from reportlab.pdfgen import canvas

from qingjian.core import format_office, format_tables
from qingjian.core.common import CancelledError
from qingjian.core.format_media import image_convert


def _book(path, *, formula=False, second=False):
    book = Workbook()
    book.active.title = "清单"
    book.active.append([None, "重复", "重复"])
    book.active.append(["00123", "=1+2" if formula else "办公室", "0007"])
    if second:
        book.create_sheet("其他材料").append(["其他", "00456"])
    book.save(path)
    book.close()


def _rows(path, encoding="utf-8-sig", delimiter=","):
    with Path(path).open(encoding=encoding, newline="") as stream:
        return list(csv.reader(stream, delimiter=delimiter))


def test_pdf_table_directions_removed_even_when_pdf_contains_tables(tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(b"not used")
    for suffix in (".xlsx", ".csv"):
        assert not format_office.supports(".pdf", suffix)
        assert suffix not in format_office.targets_for(".pdf")
        with pytest.raises(ValueError, match="不支持"):
            format_office.convert(source, tmp_path / ("removed" + suffix))


def test_csv_import_preserves_blank_duplicate_headers_and_text_identifiers(tmp_path):
    source, target = tmp_path / "input.csv", tmp_path / "output.xlsx"
    source.write_text(',编号,编号\n00123,12345678901234567890,=1+2\n', encoding="utf-8")
    result = format_office.convert(source, target)
    book = load_workbook(target)
    try:
        assert [cell.value for cell in book.active[1]] == [None, "编号", "编号"]
        assert [cell.value for cell in book.active[2]] == ["00123", "12345678901234567890", "=1+2"]
        assert all(cell.data_type == "s" for cell in book.active[2])
    finally:
        book.close()
    assert result["changes"] == 2


def test_gb18030_semicolon_input_and_utf8_output_detected_independently(tmp_path):
    source, target = tmp_path / "input.csv", tmp_path / "output.tsv"
    source.write_bytes('姓名;编号\r\n办公室;0007\r\n'.encode("gb18030"))
    format_tables.convert(source, target, {"encoding": "utf-8-sig", "delimiter": "auto"})
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    assert _rows(target, delimiter="\t") == [["姓名", "编号"], ["办公室", "0007"]]


def test_same_format_encoding_and_delimiter_conversion_preserves_multiline(tmp_path):
    source, target = tmp_path / "input.csv", tmp_path / "output.csv"
    source.write_text('编号,说明\n0007,"第一行\n第二行,含逗号"\n', encoding="utf-8", newline="")
    format_office.convert(source, target, {"encoding": "gb18030", "delimiter": ";"})
    assert _rows(target, encoding="gb18030", delimiter=";") == [["编号", "说明"], ["0007", "第一行\n第二行,含逗号"]]


def test_multisheet_requires_explicit_choice_and_all_outputs_keep_values(tmp_path):
    source, target = tmp_path / "sheets.xlsx", tmp_path / "output.csv"
    _book(source, second=True)
    with pytest.raises(ValueError, match="多个工作表"):
        format_office.convert(source, target)
    assert not target.exists()
    result = format_office.convert(source, target, {"sheet_mode": "all"})
    assert result["output_sheets"] == ["清单", "其他材料"]
    assert len(result["outputs"]) == 2
    assert _rows(result["outputs"][0])[1] == ["00123", "办公室", "0007"]
    assert _rows(result["outputs"][1]) == [["其他", "00456"]]
    selected = tmp_path / "selected.csv"
    format_office.convert(source, selected, {"sheet": "其他材料"})
    assert _rows(selected) == [["其他", "00456"]]


def test_missing_formula_cache_fails_and_publishes_no_partial_workbooks(tmp_path):
    source, target = tmp_path / "formula.xlsx", tmp_path / "output.csv"
    _book(source, formula=True, second=True)
    with pytest.raises(ValueError, match="B2.*公式"):
        format_tables.convert(source, target, {"sheet_mode": "all"})
    assert not list(tmp_path.glob("output*"))
    book = load_workbook(source)
    try:
        assert book.active["B2"].value == "=1+2"
    finally:
        book.close()


@pytest.mark.parametrize("cached", ["0", "3"])
def test_formula_exports_cached_numeric_result_including_zero(tmp_path, cached):
    original, source = tmp_path / "original.xlsx", tmp_path / "cached.xlsx"
    _book(original, formula=True)
    with zipfile.ZipFile(original) as package, zipfile.ZipFile(source, "w") as output:
        for entry in package.infolist():
            data = package.read(entry)
            if entry.filename == "xl/worksheets/sheet1.xml":
                data, count = re.subn(rb"(<f>1\+2</f>)<v></v>", rb"\1<v>" + cached.encode() + b"</v>", data)
                assert count == 1
            output.writestr(entry, data)
    target = tmp_path / "out.csv"
    format_tables.convert(source, target)
    assert _rows(target)[1][1] == cached
    assert "=1+2" not in target.read_text(encoding="utf-8-sig")


def test_all_sheet_export_preflights_second_existing_file(tmp_path):
    source, target = tmp_path / "sheets.xlsx", tmp_path / "output.csv"
    _book(source, second=True)
    other = tmp_path / "output_002_其他材料.csv"
    other.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        format_tables.convert(source, target, {"sheet_mode": "all"})
    assert not target.exists()
    assert other.read_bytes() == b"keep"


def test_image_resize_background_and_multiple_frame_outputs(tmp_path):
    source = tmp_path / "transparent.png"
    Image.new("RGBA", (400, 200), (0, 0, 0, 0)).save(source)
    target = tmp_path / "background.jpg"
    image_convert(source, target, {"max_edge": 100, "quality": 100, "jpeg_background": "#ff0000"})
    with Image.open(target) as output:
        assert output.size == (100, 50)
        red, green, blue = output.getpixel((10, 10))
        assert red > 245 and green < 10 and blue < 10
    source = tmp_path / "pages.tiff"
    Image.new("RGB", (30, 20), "red").save(source, save_all=True,
        append_images=[Image.new("RGB", (30, 20), "blue")])
    result = image_convert(source, tmp_path / "pages.png")
    assert len(result["outputs"]) == 2 and result["changes"] == 2
    with Image.open(result["outputs"][1]) as output:
        assert output.getpixel((10, 10))[:3] == (0, 0, 255)
    assert any("2" in warning and "未丢弃" in warning for warning in result["warnings"])


@pytest.mark.parametrize("options", [{"quality": 101}, {"max_edge": -1}, {"jpeg_background": "bad-color"}])
def test_bad_image_options_never_publish(tmp_path, options):
    source, target = tmp_path / "input.png", tmp_path / "output.jpg"
    Image.new("RGB", (10, 10)).save(source)
    with pytest.raises(ValueError):
        image_convert(source, target, options)
    assert not target.exists()


def _pdf(path, picture, *, mixed=False):
    document = canvas.Canvas(str(path), pagesize=(300, 300))
    if mixed:
        document.drawString(20, 250, "Editable first page")
        document.showPage()
    document.drawImage(str(picture), 20, 20, width=100, height=100)
    document.showPage()
    document.save()


@pytest.mark.parametrize("suffix", [".txt", ".docx"])
def test_scanned_pdf_refuses_empty_editable_output(tmp_path, suffix):
    picture, source = tmp_path / "scan.png", tmp_path / "scan.pdf"
    Image.new("RGB", (80, 80), "red").save(picture)
    _pdf(source, picture)
    target = tmp_path / ("out" + suffix)
    with pytest.raises(ValueError, match="OCR"):
        format_office.convert(source, target)
    assert not target.exists()


def test_mixed_pdf_reports_noneditable_page_and_dpi_is_applied(tmp_path):
    picture, source = tmp_path / "scan.png", tmp_path / "mixed.pdf"
    Image.new("RGB", (80, 80), "blue").save(picture)
    _pdf(source, picture, mixed=True)
    target = tmp_path / "mixed.docx"
    result = format_office.convert(source, target)
    assert any("2" in warning and "OCR" in warning for warning in result["warnings"])
    document = Document(target)
    assert any("Editable first page" in paragraph.text for paragraph in document.paragraphs)
    assert document.inline_shapes
    rendered = format_office.convert(source, tmp_path / "page.png", {"dpi": 72})
    assert len(rendered["outputs"]) == 2
    with Image.open(rendered["outputs"][0]) as image:
        assert image.size == (300, 300)


def test_cancelled_table_job_leaves_source_and_outputs_unchanged(tmp_path):
    source, target = tmp_path / "input.csv", tmp_path / "out.xlsx"
    source.write_bytes(b"id\n00123\n")
    event = threading.Event()
    event.set()
    with pytest.raises(CancelledError):
        format_tables.convert(source, target, {"_cancel": event})
    assert source.read_bytes() == b"id\n00123\n" and not target.exists()


def test_advanced_csv_to_json_uses_selected_input_encoding_and_separator(tmp_path):
    from qingjian.core import format_data
    source, target = tmp_path / "contacts.csv", tmp_path / "contacts.json"
    source.write_bytes('姓名;电话;备注\n张三;00123;部门,一组\n'.encode("gb18030"))
    options = {"input_encoding": "gb18030", "input_delimiter": ";"}
    format_data.convert(source, target, options)
    assert json.loads(target.read_text(encoding="utf-8")) == [{"姓名": "张三", "电话": "00123", "备注": "部门,一组"}]
    # Explicit input encoding must be consumed, rather than silently falling
    # back to auto detection when the user selected an incompatible encoding.
    with pytest.raises(ValueError, match="UTF-8"):
        format_data.read_data(source, options={"input_encoding": "utf-8-sig", "input_delimiter": ";"})


def test_advanced_csv_to_vcf_uses_selected_input_encoding_and_separator(tmp_path):
    import vobject
    from qingjian.core import format_data
    source, target = tmp_path / "contacts.csv", tmp_path / "contacts.vcf"
    source.write_bytes('姓名;电话;邮箱\n办公室;0012300;office@example.test\n'.encode("gb18030"))
    format_data.convert(source, target, {"input_encoding": "gb18030", "input_delimiter": ";"})
    contact = vobject.readOne(target.read_text(encoding="utf-8"))
    assert contact.fn.value == "办公室"
    assert contact.tel.value == "0012300"
    assert contact.email.value == "office@example.test"


def test_advanced_xlsx_to_json_honors_sheet_after_office_normalization(tmp_path):
    from qingjian.core import format_data
    source, target = tmp_path / "normalised.xlsx", tmp_path / "selected.json"
    book = Workbook()
    book.active.title = "首表"
    book.active.append(["编号", "内容"])
    book.active.append(["001", "首表内容"])
    book.create_sheet("指定表").append(["编号", "内容"])
    book["指定表"].append(["0007", "目标内容"])
    book.save(source)
    book.close()
    format_data.convert(source, target, {"sheet": "指定表", "input_delimiter": ";", "input_encoding": "gb18030"})
    assert json.loads(target.read_text(encoding="utf-8")) == [{"编号": "0007", "内容": "目标内容"}]
