"""OFD validation and CPU-only native round trips through the shipped runtime."""
from __future__ import annotations

import hashlib
import os
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from qingjian.core import format_ofd as ofd
from qingjian.core.common import CancelledError
from qingjian.core.format_media import engine, run_engine


def package(path, entries):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            item = zipfile.ZipInfo(name)
            # Preserve deliberately malformed ZIP names on Windows as bytes
            # instead of letting ZipInfo normalize the adversarial sample.
            item.filename = item.orig_filename = name
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, data)
    return path


BASE_XML = '<ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016"><ofd:DocBody><ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody></ofd:OFD>'


@pytest.mark.parametrize("unsafe", ["../escape.xml", "/absolute.xml", "C:/data.xml", "Doc_0\\part.xml",
                                    "Doc_0/../part.xml", "Doc_0/./part.xml", "Doc_0//part.xml", "Doc_0/CON.xml", "Doc_0/a. "])
def test_ofd_archive_paths_are_checked_before_starting_java(tmp_path, unsafe):
    source = package(tmp_path / "unsafe.ofd", [("OFD.xml", BASE_XML), (unsafe, "<Part/>")])
    with patch.object(ofd, "_java", side_effect=AssertionError("must not launch Java")), pytest.raises(ValueError, match="路径|部件"):
        ofd.convert(source, tmp_path / "result.pdf")
    assert not (tmp_path / "result.pdf").exists()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_ofd_blocks_doctype_even_with_utf16_or_utf32_code_units(tmp_path, encoding):
    xml = '<?xml version="1.0"?><!DOCTYPE OFD [<!ENTITY x SYSTEM "file:///C:/secret.txt">]><OFD>&x;</OFD>'
    source = package(tmp_path / "external.ofd", [("OFD.xml", xml.encode(encoding))])
    with pytest.raises(ValueError, match="实体"):
        ofd.validate_ofd(source)


@pytest.mark.parametrize("reference", ["../../secret.txt", "https://example.com/document.xml", "file:///C:/secret.txt", "C:\\secret.txt"])
def test_ofd_resource_references_cannot_escape_archive_or_use_network(tmp_path, reference):
    xml = f'<OFD><DocRoot>{reference}</DocRoot></OFD>'
    source = package(tmp_path / "reference.ofd", [("OFD.xml", xml)])
    with pytest.raises(ValueError, match="资源|路径"):
        ofd.validate_ofd(source)


def test_ofd_archive_absolute_references_mean_package_root(tmp_path):
    source = package(tmp_path / "root.ofd", [("OFD.xml", "<OFD><DocRoot>/Doc_0/Document.xml</DocRoot></OFD>"),
                                             ("Doc_0/Document.xml", "<Document><PublicRes>../PublicRes.xml</PublicRes></Document>")])
    assert ofd.validate_ofd(source) == {"signed": False}


def test_ofd_duplicate_and_expanded_size_limits(tmp_path, monkeypatch):
    source = package(tmp_path / "case.ofd", [("OFD.xml", BASE_XML), ("ofd.XML", BASE_XML)])
    with pytest.raises(ValueError, match="重复"):
        ofd.validate_ofd(source)
    source = package(tmp_path / "large.ofd", [("OFD.xml", BASE_XML)])
    monkeypatch.setattr(ofd, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ValueError, match="展开"):
        ofd.validate_ofd(source)


def test_ofd_existing_output_and_input_are_never_overwritten(tmp_path):
    source = tmp_path / "body.txt"
    source.write_text("original", encoding="utf-8")
    target = tmp_path / "body.ofd"
    target.write_bytes(b"previous result")
    with patch.object(ofd, "_java", side_effect=AssertionError("no conversion")), pytest.raises(FileExistsError):
        ofd.convert(source, target)
    assert target.read_bytes() == b"previous result"
    assert source.read_text() == "original"


def test_ofd_cancelled_before_start_never_launches_native_process(tmp_path):
    event = threading.Event()
    event.set()
    with patch.object(ofd, "_java", side_effect=AssertionError("cancelled")), pytest.raises(CancelledError):
        ofd.convert(tmp_path / "not-read.ofd", tmp_path / "not-written.pdf", {"_cancel": event})


@pytest.fixture
def native(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows offline component runtime")
    try:
        engine("OFD/runtime/bin/java.exe")
        engine("OFD/bridge.jar")
    except RuntimeError:
        pytest.skip("OFD support bundle not built")
    windows = Path(os.environ.get("WINDIR", "C:/Windows"))
    monkeypatch.setenv("PATH", str(windows / "System32") + os.pathsep + str(windows))
    monkeypatch.setenv("JAVA_HOME", str(tmp_path / "absent-system-java"))
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Dnot.a.real.test.flag=ignored")
    return tmp_path


def test_native_txt_ofd_text_roundtrip_is_offline_and_preserves_chinese(native):
    source = native / "中文原文.txt"
    content = "青简工具箱\n材料核查 2026 年\nCPU offline conversion 123"
    source.write_text(content, encoding="utf-8")
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    document, output = native / "归档.ofd", native / "返回文本.txt"
    with patch("urllib.request.urlopen", side_effect=AssertionError("offline")):
        created = ofd.convert(source, document)
        exported = ofd.convert(document, output)
    assert "青简工具箱" in output.read_text(encoding="utf-8")
    assert "123" in output.read_text(encoding="utf-8")
    assert created["outputs"] == [str(document)]
    assert any("电子签名" in text for text in exported["warnings"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original


def test_native_pdf_roundtrip_keeps_page_count_and_renders_all_pages(native):
    from reportlab.pdfgen.canvas import Canvas
    from pypdf import PdfReader
    from PIL import Image
    source = native / "two-pages.pdf"
    canvas = Canvas(str(source))
    for number in (1, 2):
        canvas.drawString(80, 720, f"Qingjian page {number}")
        canvas.showPage()
    canvas.save()
    document = native / "two-pages.ofd"
    ofd.convert(source, document)
    pdf = native / "returned.pdf"
    ofd.convert(document, pdf)
    assert len(PdfReader(pdf).pages) == 2
    images = ofd.convert(document, native / "page.png", {"dpi": 72})
    assert len(images["outputs"]) == 2
    for path in images["outputs"]:
        with Image.open(path) as image:
            assert image.width > 0 and image.height > 0
    assert source.exists()


def test_native_image_ofd_jpg_requires_no_local_gpu_or_java(native):
    from PIL import Image
    source = native / "图片.png"
    Image.new("RGB", (240, 160), "#5878ad").save(source)
    document = native / "图片.ofd"
    ofd.convert(source, document)
    result = ofd.convert(document, native / "图片返回.jpg", {"dpi": 72})
    with Image.open(result["outputs"][0]) as image:
        assert image.width > 0 and image.height > 0


def test_native_7zip_and_ghostscript_start_from_bundled_paths(native):
    seven = run_engine([engine("7zip/7z.exe"), "i"], timeout=30)
    gs = run_engine([engine("Ghostscript/bin/gswin64c.exe"), "--version"], timeout=30)
    assert "7-Zip" in seven
    assert "10.08.0" in gs


def test_native_two_page_postscript_exports_both_images(native):
    from qingjian.core.format_media import vector_convert
    from PIL import Image
    source = native / "two-pages.ps"
    source.write_text("%!PS-Adobe-3.0\n%%Pages: 2\n<< /PageSize [180 120] >> setpagedevice\n"
                      "/Helvetica findfont 16 scalefont setfont\n"
                      "20 60 moveto (Page One) show showpage\n"
                      "20 60 moveto (Page Two) show showpage\n%%EOF\n", encoding="ascii")
    result = vector_convert(source, native / "converted.png")
    assert len(result["outputs"]) == 2
    for path in result["outputs"]:
        with Image.open(path) as page:
            assert page.width > 0 and page.height > 0
