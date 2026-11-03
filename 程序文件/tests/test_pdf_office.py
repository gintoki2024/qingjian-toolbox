"""Real local PDF finishing, page provenance and all-or-nothing binding."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject
from reportlab.pdfgen import canvas

from qingjian.core.common import CancelledError
from qingjian.core import conversion
from qingjian.core.pdf_office import run, validate


def pdf(path, pages=1, *, size=(300, 400)):
    sheet = canvas.Canvas(str(path), pagesize=size)
    for index in range(pages):
        sheet.drawString(30, 340, f"Source page {index + 1}")
        sheet.showPage()
    sheet.save()
    return path


def detail(result):
    path = next(Path(value) for value in result["outputs"] if value.endswith(".json"))
    return json.loads(path.read_text(encoding="utf-8"))


def test_finish_keeps_original_pages_and_only_rotates_requested_page(tmp_path):
    source = pdf(tmp_path / "原件.pdf", 2)
    original = source.read_bytes()
    result = run("pdf_finish", [source], tmp_path / "out", {"compress": True, "rotation": 90, "pages": "2"})
    assert not result["errors"]
    with PdfReader(result["data_outputs"][0]) as document:
        assert len(document.pages) == 2
        assert document.pages[0].rotation == 0
        assert document.pages[1].rotation == 90
        assert "Source page 2" in document.pages[1].extract_text()
    assert source.read_bytes() == original


def test_watermark_numbering_and_bookmarks_survive_rotated_pages(tmp_path):
    source = pdf(tmp_path / "input.pdf", 2)
    options = dict(rotation=90, add_page_numbers=True, page_number_start=5, watermark="内部资料", watermark_opacity=0.12,
                   bookmarks=[dict(title="正文", page="2")])
    result = run("pdf_finish", [source], tmp_path / "out", options)
    assert not result["errors"]
    with PdfReader(result["data_outputs"][0]) as document:
        assert all(page.rotation == 0 for page in document.pages)
        text = document.pages[0].extract_text()
        assert "内部资料" in text and "第 5 页 / 共 6 页" in text
        assert float(document.pages[0].mediabox.width) == pytest.approx(400)
        assert document.outline[0]["/Title"] == "正文"
        assert document.get_destination_page_number(document.outline[0]) == 1


def test_invalid_bookmark_creates_report_but_no_incomplete_pdf(tmp_path):
    source = pdf(tmp_path / "input.pdf")
    result = run("pdf_finish", [source], tmp_path / "out", {"bookmarks": [dict(title="错页", page="2")]})
    assert result["errors"] and not result["data_outputs"]
    assert not list((tmp_path / "out").glob("*.pdf"))
    assert detail(result)["records"][0]["状态"] == "失败"


def test_open_encrypted_pdf_needs_password_and_output_is_explicitly_unencrypted(tmp_path):
    source = pdf(tmp_path / "plain.pdf")
    writer = PdfWriter(clone_from=source)
    encrypted = tmp_path / "encrypted.pdf"
    writer.encrypt("test-pass")
    writer.write(encrypted)
    writer.close()
    wrong = run("pdf_finish", [encrypted], tmp_path / "wrong", {"password": "wrong"})
    assert wrong["errors"] and not wrong["data_outputs"]
    right = run("pdf_finish", [encrypted], tmp_path / "right", {"password": "test-pass"})
    assert not right["errors"] and any("未加密" in value for value in right["warnings"])
    with PdfReader(right["data_outputs"][0]) as document:
        assert not document.is_encrypted


def test_numeric_form_value_is_not_mistaken_for_a_signature(tmp_path):
    source = pdf(tmp_path / "plain.pdf")
    writer = PdfWriter(clone_from=source)
    field = DictionaryObject({NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): TextStringObject("amount"), NameObject("/V"): NumberObject(100)})
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject([writer._add_object(field)])})
    target = tmp_path / "form.pdf"
    writer.write(target)
    writer.close()
    result = run("pdf_finish", [target], tmp_path / "out", {})
    assert not result["errors"] and result["data_outputs"]


def test_signed_pdf_is_refused_without_publishing_modified_copy(tmp_path):
    source = pdf(tmp_path / "plain.pdf")
    writer = PdfWriter(clone_from=source)
    signature = DictionaryObject({NameObject("/Type"): NameObject("/Sig"), NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(1), NumberObject(2), NumberObject(3)])})
    field = DictionaryObject({NameObject("/FT"): NameObject("/Sig"), NameObject("/T"): TextStringObject("signature"), NameObject("/V"): writer._add_object(signature)})
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject([writer._add_object(field)])})
    signed = tmp_path / "signed.pdf"
    writer.write(signed)
    writer.close()
    result = run("pdf_finish", [signed], tmp_path / "out", {})
    assert any("签名" in error for error in result["errors"])
    assert not result["data_outputs"]


def test_certified_pdf_permissions_are_detected_without_acroform(tmp_path):
    source = pdf(tmp_path / "plain.pdf")
    writer = PdfWriter(clone_from=source)
    signature = DictionaryObject({NameObject("/Type"): NameObject("/Sig"), NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(1), NumberObject(2), NumberObject(3)])})
    writer._root_object[NameObject("/Perms")] = DictionaryObject({NameObject("/DocMDP"): writer._add_object(signature)})
    certified = tmp_path / "certified.pdf"
    writer.write(certified)
    writer.close()
    result = run("pdf_finish", [certified], tmp_path / "out", {})
    assert any("签名" in error for error in result["errors"]) and not result["data_outputs"]


def test_multiframe_tiff_binding_keeps_all_material_pages(tmp_path):
    from PIL import Image
    frames = [Image.new("RGB", (100, 200), color) for color in ("red", "green", "blue")]
    source = tmp_path / "three.tiff"
    frames[0].save(source, save_all=True, append_images=frames[1:])
    for frame in frames:
        frame.close()
    result = run("submission_bind", [source], tmp_path / "out", {"cover": False, "contents": False, "add_page_numbers": False})
    assert not result["errors"]
    assert detail(result)["records"][0]["原始页数"] == 3
    with PdfReader(result["data_outputs"][0]) as document:
        assert len(document.pages) == 3


def test_long_cover_text_stays_above_footer(tmp_path):
    source = pdf(tmp_path / "body.pdf")
    title, subtitle = "标题" * 150, "副笺" * 250
    result = run("submission_bind", [source], tmp_path / "out", {"book_title": title, "subtitle": subtitle, "contents": False})
    assert not result["errors"]
    with PdfReader(result["data_outputs"][0]) as document:
        positions = []
        def visit(text, cm, tm, font, size):
            if text.strip() and ("标题" in text or "副笺" in text):
                positions.append(tm[5])
        text = document.pages[0].extract_text(visitor_text=visit)
        assert text.count("标") == 150 and text.count("笺") == 250
        assert positions and min(positions) > 180


def test_binding_toc_bookmarks_and_duplex_page_provenance(tmp_path):
    sources = [pdf(tmp_path / "一.pdf"), pdf(tmp_path / "二.pdf")]
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in sources]
    result = run("submission_bind", sources, tmp_path / "out", dict(book_title="报送材料", cover=True, contents=True, duplex_blank=True, add_page_numbers=True))
    assert not result["errors"]
    records = detail(result)["records"]
    assert [(row["整册起始页"], row["整册结束页"]) for row in records] == [(3, 3), (5, 5)]
    with PdfReader(result["data_outputs"][0]) as document:
        assert len(document.pages) == 5
        assert "报送材料" in document.pages[0].extract_text()
        assert "材料目录" in document.pages[1].extract_text()
        assert "Source page 1" in document.pages[2].extract_text()
        assert "Source page" not in document.pages[3].extract_text()
        assert [document.get_destination_page_number(item) for item in document.outline] == [2, 4]
    assert hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in sources]


def test_binding_without_frontmatter_does_not_add_blank_pages(tmp_path):
    sources = [pdf(tmp_path / "a.pdf", 2), pdf(tmp_path / "b.pdf")]
    result = run("submission_bind", sources, tmp_path / "out", dict(cover=False, contents=False, add_page_numbers=False))
    assert not result["errors"]
    assert [(row["整册起始页"], row["整册结束页"]) for row in detail(result)["records"]] == [(1, 2), (3, 3)]
    with PdfReader(result["data_outputs"][0]) as document:
        assert len(document.pages) == 3


def test_binding_any_failed_input_prevents_incomplete_final_book(tmp_path):
    good = pdf(tmp_path / "good.pdf")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"invalid PDF")
    result = run("submission_bind", [good, bad], tmp_path / "out", {})
    assert result["errors"] and not result["data_outputs"]
    assert not list((tmp_path / "out").glob("*.pdf"))
    assert any("未发布缺页" in value for value in result["warnings"])


def test_cancellation_is_propagated_without_a_final_book(tmp_path):
    source = pdf(tmp_path / "input.pdf")
    with pytest.raises(CancelledError):
        run("submission_bind", [source], tmp_path / "out", {}, cancel=lambda: True)
    assert not list((tmp_path / "out").glob("*.pdf"))


@pytest.mark.parametrize("options", [{"watermark_opacity": float("nan")}, {"rotation": 22}, {"bookmarks": [{"title": "a", "page": "0"}]}, {"compress": "yes"}])
def test_invalid_pdf_options_are_rejected(options):
    with pytest.raises(ValueError):
        validate("pdf_finish", options)


@pytest.mark.parametrize("suffix,filter_name", [(".docx", "writer"), (".xlsx", "calc"), (".pptx", "impress")])
def test_office_pdf_helper_accepts_only_supported_local_packages(tmp_path, suffix, filter_name):
    import zipfile
    path = tmp_path / ("document" + suffix)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("empty.xml", "<empty/>")
    with patch.object(conversion, "_convert_office", return_value={"changes": 1}) as run_office:
        assert conversion.convert_office_to_pdf(path, tmp_path / "target.pdf") == {"changes": 1}
    assert suffix in run_office.call_args.args[3]
    assert run_office.call_args.args[4] == {".pdf"}


def test_office_pdf_helper_rejects_external_data_before_starting_engine(tmp_path):
    import zipfile
    source = tmp_path / "external.xlsx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("xl/externalLinks/externalLink1.xml", "<external/>")
    with patch.object(conversion, "_convert_office", side_effect=AssertionError("must not launch")), pytest.raises(ValueError, match="外部"):
        conversion.convert_office_to_pdf(source, tmp_path / "output.pdf")
