"""Real-file coverage for TXT rules and read-only text/image document export."""
from __future__ import annotations

import codecs
import json
import shutil
from collections import Counter
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from docx import Document
from docx.shared import Inches
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from qingjian.core.document_export import run_document_export
from qingjian.core.text_ops import process_text


def _pdf(path, texts=("First page", "Second page"), password=None):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data((f"BT /F1 12 Tf 25 270 Td ({text}) Tj ET\n" if text else "").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    if password:
        writer.encrypt(password)
    writer.write(path)
    return path


def _image(kind, color):
    stream = BytesIO()
    Image.new("RGB", (40, 30), color).save(stream, format=kind)
    return stream.getvalue()


def _word(path, images=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    document.add_paragraph("第一段正文")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "表格甲"
    table.cell(0, 1).text = "表格乙"
    document.add_paragraph("最后一段正文")
    document.sections[0].header.paragraphs[0].text = "页眉机密标记"
    document.sections[0].footer.paragraphs[0].text = "页脚标记"
    for image in images:
        document.add_paragraph().add_run().add_picture(BytesIO(image), width=Inches(0.3))
    document.save(path)
    return path


def _report(result):
    assert result["report"] in result["outputs"]
    path = Path(result["report"])
    assert path.is_file()
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "gb18030"])
def test_text_decodes_chinese_encodings_preserves_newlines_and_literal_replacements(tmp_path, encoding):
    source = tmp_path / "原稿.txt"
    content = "张三 帐号 13800138000\r\n删除此行\r\n  \r\n保留\\路径"
    original = content.encode(encoding)
    source.write_bytes(original)
    target = tmp_path / "结果.txt"
    result = process_text(source, target, {
        "replacements": [{"find": "帐号", "replace": r"账号\资料", "regex": False}],
        "custom_sensitive_rules": [{"find": "张三", "replace": "李四"}],
        "mask_phone": True, "mask_phone_replacement": r"\g<手机号>",
        "delete_paragraphs": ["删除"], "remove_blank_paragraphs": True,
    })
    assert target.read_bytes().decode("utf-8") == "李四 账号\\资料 \\g<手机号>\r\n保留\\路径"
    assert not target.read_bytes().startswith(codecs.BOM_UTF8)
    assert source.read_bytes() == original
    assert result["changes"] == 5


def test_text_regex_groups_and_sensitive_mapping_use_their_intended_semantics(tmp_path):
    source = tmp_path / "映射.md"
    source.write_text("编号=A12;张三;李四;13800138000;11010519491231002X;user@example.com;旧路径", encoding="utf-8")
    target = tmp_path / "新映射.md"
    result = process_text(source, target, {
        "replacements": [{"find": r"编号=([A-Z])(\d+)", "replace": r"Code:\1-\2", "regex": True},
                         {"find": "旧路径", "replace": r"C:\new\1", "regex": False}],
        "custom_sensitive_rules": [{"find": "张三", "replace": "员工甲"}],
        "custom_sensitive": ["李四"], "custom_sensitive_replacement": r"\1",
        "mask_phone": True, "mask_id": True, "mask_email": True,
        "mask_phone_replacement": "[电话]", "mask_id_replacement": "[证件]", "mask_email_replacement": "[邮箱]",
    })
    assert target.read_text(encoding="utf-8") == r"Code:A-12;员工甲;\1;[电话];[证件];[邮箱];C:\new\1"
    assert result["changes"] == 7
    assert "张三" in source.read_text(encoding="utf-8")


def test_text_rejects_format_only_official_invalid_rules_and_existing_destinations(tmp_path):
    source = tmp_path / "原稿.txt"
    source.write_text("姓名 张三", encoding="utf-8")
    original = source.read_bytes()
    target = tmp_path / "结果.txt"
    for options, message in (({"format_enabled": True}, "未选择"),
                             ({"official_enabled": True, "mask_phone": True}, "不支持公文排版"),
                             ({"replacements": [{"find": "[", "replace": "x", "regex": True}]}, "正则"),
                             ({"replacements": [{"find": "(a)", "replace": r"\g<missing>", "regex": True}]}, "正则")):
        with pytest.raises(ValueError, match=message):
            process_text(source, target, options)
        assert not target.exists()
    with pytest.raises(ValueError, match="原文件相同"):
        process_text(source, source, {"custom_sensitive": ["张三"]})
    target.write_bytes(b"existing result")
    with pytest.raises(FileExistsError):
        process_text(source, target, {"custom_sensitive": ["张三"]})
    assert target.read_bytes() == b"existing result"
    assert source.read_bytes() == original


def test_text_invalid_encoding_or_size_never_publishes_a_partial_output(tmp_path):
    source = tmp_path / "损坏编码.txt"
    source.write_bytes(b"\xff")
    target = tmp_path / "不得生成.txt"
    with pytest.raises(ValueError, match="无法.*识别"):
        process_text(source, target, {"remove_blank_paragraphs": True})
    assert not target.exists()
    source.write_bytes(b"0123456789")
    with patch("qingjian.core.text_ops.MAX_TEXT_BYTES", 8), pytest.raises(ValueError, match="超过"):
        process_text(source, target, {"remove_blank_paragraphs": True})
    assert not target.exists()
    assert not list(tmp_path.glob(".qingjian-write-*"))


def test_export_text_preserves_word_block_order_pdf_pages_and_original_files(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    word = _word(inputs / "公文.docx")
    text = inputs / "中文.txt"
    text.write_bytes("中文文本原文\r\n下一行".encode("gb18030"))
    pdf = _pdf(inputs / "文档.pdf", ("PAGE ONE", "", "PAGE THREE"))
    originals = {path: path.read_bytes() for path in (word, text, pdf)}
    folder = tmp_path / "out"
    result = run_document_export([word, text, pdf], folder, {"action": "text", "include_headers": False})
    assert result["changes"] == 3
    assert not result["errors"]
    assert len(result["data_outputs"]) == 3
    records = _report(result)["records"]
    by_source = {record["来源"]: Path(record["输出"][0]).read_text(encoding="utf-8") for record in records}
    word_text = by_source[str(word)]
    assert word_text.index("第一段正文") < word_text.index("表格甲") < word_text.index("最后一段正文")
    assert "表格乙" in word_text and "页眉机密标记" not in word_text and "页脚标记" not in word_text
    assert "中文文本原文" in by_source[str(text)]
    pdf_text = by_source[str(pdf)]
    assert "来源文件：文档.pdf" in pdf_text
    assert pdf_text.index("第1页") < pdf_text.index("PAGE ONE") < pdf_text.index("第2页") < pdf_text.index("第3页") < pdf_text.index("PAGE THREE")
    assert "OCR" in pdf_text and any("不执行 OCR" in message for message in result["warnings"])
    assert all(path.read_bytes() == data for path, data in originals.items())
    first_files = {Path(path): Path(path).read_bytes() for path in result["outputs"]}
    second = run_document_export([word], folder, {"action": "text", "include_headers": True})
    assert "页眉机密标记" in Path(second["data_outputs"][0]).read_text(encoding="utf-8")
    assert "页脚标记" in Path(second["data_outputs"][0]).read_text(encoding="utf-8")
    assert Path(second["data_outputs"][0]) not in first_files
    assert all(path.read_bytes() == data for path, data in first_files.items())


def test_pdf_password_failure_and_missing_source_do_not_stop_later_text_export(tmp_path):
    pdf = _pdf(tmp_path / "受保护.pdf", password="secret")
    text = tmp_path / "正常.txt"
    text.write_text("正常文件继续处理", encoding="utf-8")
    original = pdf.read_bytes()
    result = run_document_export([tmp_path / "不存在.txt", pdf, text], tmp_path / "failed", {"action": "text", "password": "wrong"})
    assert len(result["errors"]) == 2
    assert len(result["data_outputs"]) == 1
    assert "正常文件继续处理" in Path(result["data_outputs"][0]).read_text(encoding="utf-8")
    assert sum(record["状态"] == "失败" for record in _report(result)["records"]) == 2
    opened = run_document_export([pdf], tmp_path / "opened", {"action": "text", "password": "secret"})
    assert not opened["errors"]
    assert "First page" in Path(opened["data_outputs"][0]).read_text(encoding="utf-8")
    assert "secret" not in Path(opened["report"]).read_text(encoding="utf-8")
    assert pdf.read_bytes() == original


def test_original_images_keep_bytes_format_and_separate_same_named_inputs(tmp_path):
    png, jpg = _image("PNG", "blue"), _image("JPEG", "orange")
    first = _word(tmp_path / "one" / "同名.docx", [png, jpg])
    second = _word(tmp_path / "two" / "同名.docx", [png])
    empty = _word(tmp_path / "无图.docx")
    broken = tmp_path / "损坏.docx"
    broken.write_bytes(b"broken zip")
    unsupported = _pdf(tmp_path / "不支持原图.pdf")
    originals = {path: path.read_bytes() for path in (first, second, empty, broken, unsupported)}
    result = run_document_export([broken, first, second, empty, unsupported], tmp_path / "images", {"action": "images"})
    assert len(result["errors"]) == 1
    exported = [Path(path) for path in result["data_outputs"]]
    assert len(exported) == result["changes"] == 3
    assert Counter(path.read_bytes() for path in exported) == Counter([png, png, jpg])
    assert {path.suffix.lower() for path in exported} == {".png", ".jpg"}
    assert len({path.parent for path in exported}) == 2
    assert any("0 个" in message for message in result["warnings"])
    assert any("不支持" in message and "不支持原图.pdf" in message for message in result["warnings"])
    assert any(record["状态"] == "跳过" for record in _report(result)["records"])
    assert all(path.read_bytes() == data for path, data in originals.items())
    first_outputs = {path: path.read_bytes() for path in exported}
    again = run_document_export([first], tmp_path / "images", {"action": "images"})
    assert set(again["data_outputs"]).isdisjoint(map(str, first_outputs))
    repeated = run_document_export([first], tmp_path / "images", {"action": "images"})
    assert set(repeated["data_outputs"]).isdisjoint(again["data_outputs"])
    assert all(path.read_bytes() == data for path, data in first_outputs.items())


def test_malformed_zip_member_and_member_limit_are_reported_without_extraction(tmp_path):
    source = _word(tmp_path / "非法成员.docx")
    with ZipFile(source, "a", ZIP_DEFLATED) as archive:
        archive.writestr("word/media/../../outside.png", _image("PNG", "red"))
    original = source.read_bytes()
    folder = tmp_path / "out"
    result = run_document_export([source], folder, {"action": "images"})
    assert result["errors"] and not result["data_outputs"]
    assert not (tmp_path / "outside.png").exists()
    assert source.read_bytes() == original
    safe = _word(tmp_path / "正常文档.docx", [_image("PNG", "black")])
    with patch("qingjian.core.document_export.MAX_ZIP_MEMBERS", 1):
        limited = run_document_export([safe], tmp_path / "limited", {"action": "images"})
    assert limited["errors"] and not limited["data_outputs"]


def test_legacy_doc_routes_through_local_converter_before_export(tmp_path):
    source = tmp_path / "历史.doc"
    source.write_bytes(b"legacy DOC fixture; conversion boundary is mocked")
    converted_fixture = _word(tmp_path / "已转换.docx")
    original = source.read_bytes()

    def convert(path, target, options):
        assert path == source and Path(target).suffix == ".docx"
        shutil.copyfile(converted_fixture, target)
        return {"changes": 1, "warnings": []}

    with patch("qingjian.core.document_export.convert_word", side_effect=convert) as converter:
        result = run_document_export([source], tmp_path / "out", {"action": "text"})
    converter.assert_called_once()
    assert not result["errors"]
    assert "第一段正文" in Path(result["data_outputs"][0]).read_text(encoding="utf-8")
    assert any("旧版 DOC" in message for message in result["warnings"])
    assert source.read_bytes() == original


def test_export_cancellation_keeps_completed_outputs_and_writes_report(tmp_path):
    first, second = tmp_path / "一.txt", tmp_path / "二.txt"
    first.write_text("第一份", encoding="utf-8")
    second.write_text("第二份", encoding="utf-8")
    stopped = False

    def progress(done, total, message):
        nonlocal stopped
        if done == 1:
            stopped = True

    result = run_document_export([first, second], tmp_path / "out", {"action": "text"},
                                 progress=progress, cancel=lambda: stopped)
    assert result["cancelled"] is True
    assert len(result["data_outputs"]) == result["changes"] == 1
    assert "第一份" in Path(result["data_outputs"][0]).read_text(encoding="utf-8")
    assert _report(result)["cancelled"] is True
    assert not list((tmp_path / "out").glob(".qingjian-write-*"))
    assert first.read_text(encoding="utf-8") == "第一份"
    assert second.read_text(encoding="utf-8") == "第二份"


def test_export_file_limit_stops_collection_and_reports_unchecked_files(tmp_path):
    sources = []
    for number in range(3):
        path = tmp_path / f"{number}.txt"
        path.write_text(f"文本{number}", encoding="utf-8")
        sources.append(path)
    with patch("qingjian.core.document_export.MAX_INPUT_FILES", 2):
        result = run_document_export(sources, tmp_path / "out", {"action": "text"})
    assert result["changes"] == len(result["data_outputs"]) == 2
    assert any("后续文件未继续" in value for value in result["warnings"])
    assert any(record["状态"] == "跳过" and record["来源"] == str(sources[2]) for record in _report(result)["records"])
