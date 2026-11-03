"""Actual parameter-free conversions and no-overwrite/cancellation contracts."""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
from pathlib import Path
from unittest import mock

import pytest
from docx import Document
from PIL import Image
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from qingjian import quick_actions as quick


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def picture(path, mode="RGBA", color=(20, 90, 160, 100)):
    with Image.new(mode, (24, 16), color) as image:
        image.save(path)
    return path


def word(path, text="A fictional office document"):
    document = Document()
    document.add_paragraph(text)
    document.add_table(rows=1, cols=2).rows[0].cells[0].text = "Table content"
    document.save(path)
    return path


def pdf(path, text="Editable example 2026"):
    document = canvas.Canvas(str(path), pagesize=(595, 842))
    document.drawString(72, 720, text)
    document.save()
    return path


def assert_no_staging(folder):
    assert not list(folder.glob(".qingjian-quick-*"))


def test_metadata_is_stable_and_has_no_model_actions():
    assert list(quick.ACTIONS) == ["to_pdf", "pdf_to_word", "web_to_word", "to_text", "image_to_jpg", "image_to_png"]
    for key, action in quick.ACTIONS.items():
        assert action["id"] == key
        assert action["title"] and action["description"] and action["glyph"]
        assert all(suffix.startswith(".") for suffix in action["extensions"])
    assert quick.ACTIONS["web_to_word"]["accept_urls"]
    assert set(quick.ACTIONS["web_to_word"]["extensions"]) == {".html", ".htm", ".xhtml", ".mht", ".mhtml"}
    assert not {".xls", ".ppt", ".py", ".exe"} & set(quick.ACTIONS["to_pdf"]["extensions"])


def test_parse_file_uri_plain_paths_multiple_lines_and_dedup(tmp_path):
    source = picture(tmp_path / "文件 带空格.png")
    parsed = quick.parse_mime_payload([], f'"{source}"\nhttps://example.com/a?q=1\n{source.as_uri()}\nhttps://example.com/a?q=1')
    assert parsed == [str(source), "https://example.com/a?q=1"]
    assert quick.parse_mime_payload([source.as_uri()], "Copied file title") == [str(source)]


def test_parse_url_mime_takes_precedence_over_browser_title():
    assert quick.parse_mime_payload(["https://example.com/report"], "报告标题") == ["https://example.com/report"]


@pytest.mark.parametrize("text", ["普通文字，请帮我转成 Word", "<html><body>粘贴片段</body></html>",
                                     "javascript:alert(1)", "data:text/html,abc", "ftp://example.com/a",
                                     "https://user:password@example.com/a", "https://example.com:bad/a",
                                     "https://example.com/a b", "file:///C:/test.html?unexpected=1"])
def test_parse_does_not_interpret_prose_html_or_non_http_schemes(text):
    assert quick.parse_mime_payload([], text) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_parse_windows_drive_and_unc_file_uri():
    assert quick.parse_mime_payload(["file:///C:/Work/%E6%96%87%E6%A1%A3.docx"]) == [r"C:\Work\文档.docx"]
    assert quick.parse_mime_payload(["file://server/share/example.html"]) == [r"\\server\share\example.html"]
    assert quick.parse_mime_payload([], '"C:\\Work\\office file.docx"') == [r"C:\Work\office file.docx"]


def test_validate_does_not_create_output_or_scan_folders(tmp_path):
    source = picture(tmp_path / "example.png")
    assert quick.validate_sources("to_pdf", [str(source)]) == []
    with mock.patch.object(Path, "iterdir", side_effect=AssertionError("must not scan")):
        errors = quick.validate_sources("to_pdf", [str(tmp_path)])
    assert "不会扫描文件夹" in errors[0]
    assert quick.validate_sources("missing", [str(source)])
    assert quick.validate_sources("to_pdf", [])
    assert quick.validate_sources("to_pdf", str(source))
    assert not (tmp_path / "download").exists()


def test_validate_mixed_input_lists_only_invalid_items(tmp_path):
    source = picture(tmp_path / "example.png")
    script = tmp_path / "never_run.py"
    script.write_text("raise AssertionError('never execute')", encoding="utf-8")
    assert len(quick.validate_sources("to_pdf", [source, script])) == 1
    assert quick.validate_sources("to_pdf", ["https://example.com/"])
    assert quick.validate_sources("web_to_word", ["https://example.com/"]) == []


def test_image_to_jpg_uses_original_parent_keeps_size_and_white_alpha(tmp_path):
    source = picture(tmp_path / "透明.png", color=(0, 0, 0, 0))
    before = digest(source)
    result = quick.run_quick_action("image_to_jpg", [source], tmp_path / "unused")
    assert not result["errors"] and result["changes"] == 1 and result["total"] == 1
    output = Path(result["outputs"][0])
    assert output == tmp_path / "透明.jpg"
    assert result["records"][0]["status"] == "success"
    assert result["output_folders"] == [str(tmp_path)]
    with Image.open(output) as image:
        assert image.size == (24, 16) and image.mode == "RGB"
        assert min(image.getpixel((0, 0))) > 245
    assert digest(source) == before
    assert not (tmp_path / "unused").exists()
    assert_no_staging(tmp_path)


def test_image_to_png_preserves_transparency_and_existing_same_suffix(tmp_path):
    source = picture(tmp_path / "source.png")
    before = digest(source)
    occupied = tmp_path / "source_2.png"
    occupied.write_bytes(b"existing result")
    result = quick.run_quick_action("image_to_png", [str(source)], None)
    assert not result["errors"]
    assert Path(result["outputs"][0]).name == "source_3.png"
    with Image.open(result["outputs"][0]) as image:
        assert image.size == (24, 16) and image.mode == "RGBA"
    assert digest(source) == before and occupied.read_bytes() == b"existing result"


def test_publish_retries_an_atomic_filename_race(tmp_path):
    source = picture(tmp_path / "race.png")
    original_commit = quick.commit_file
    calls = []

    def competing_writer(staged, target):
        calls.append(target)
        if len(calls) == 1:
            target.write_bytes(b"other process")
            raise FileExistsError("another writer arrived")
        return original_commit(staged, target)

    with mock.patch.object(quick, "commit_file", side_effect=competing_writer):
        result = quick.run_quick_action("image_to_jpg", [source], None)
    assert not result["errors"]
    assert (tmp_path / "race.jpg").read_bytes() == b"other process"
    assert Path(result["outputs"][0]).name == "race_2.jpg"
    assert_no_staging(tmp_path)


def test_multiple_original_directories_each_get_their_own_result(tmp_path):
    first, second = tmp_path / "A", tmp_path / "B"
    first.mkdir(); second.mkdir()
    inputs = [picture(first / "same.png"), picture(second / "same.png")]
    before = {str(path): digest(path) for path in inputs}
    result = quick.run_quick_action("image_to_jpg", inputs, tmp_path / "unused")
    assert not result["errors"] and result["changes"] == 2
    assert [Path(path).parent for path in result["outputs"]] == [first, second]
    assert before == {str(path): digest(path) for path in inputs}


def test_image_to_pdf_is_real_pdf_and_multiframe_tiff_keeps_all_pages(tmp_path):
    source = tmp_path / "双页.tiff"
    with Image.new("RGB", (18, 30), "red") as first, Image.new("RGB", (18, 30), "blue") as second:
        first.save(source, save_all=True, append_images=[second])
    before = digest(source)
    result = quick.run_quick_action("to_pdf", [source], None)
    assert not result["errors"]
    assert len(PdfReader(result["outputs"][0]).pages) == 2
    assert digest(source) == before
    assert_no_staging(tmp_path)


def test_pdf_to_word_real_text_is_editable_no_ocr(tmp_path):
    source = pdf(tmp_path / "electronic.pdf")
    before = digest(source)
    result = quick.run_quick_action("pdf_to_word", [source], None)
    assert not result["errors"]
    text = "\n".join(paragraph.text for paragraph in Document(result["outputs"][0]).paragraphs)
    assert "Editable example 2026" in text
    assert any("不执行 OCR" in warning for warning in result["warnings"])
    assert digest(source) == before


@pytest.mark.parametrize("extension", [".docx", ".pdf", ".txt", ".md"])
def test_document_text_export_uses_existing_exporter_without_extra_reports(tmp_path, extension):
    source = tmp_path / ("办公示例" + extension)
    if extension == ".docx":
        word(source, "A fictional office document")
    elif extension == ".pdf":
        pdf(source, "A fictional office document")
    else:
        source.write_text("A fictional office document", encoding="utf-8")
    before = digest(source)
    result = quick.run_quick_action("to_text", [source], None)
    assert not result["errors"] and result["changes"] == 1
    output = Path(result["outputs"][0])
    assert output.parent == source.parent and output != source
    assert "A fictional office document" in output.read_text(encoding="utf-8")
    assert digest(source) == before
    assert not list(tmp_path.glob("*报告.json"))
    assert_no_staging(tmp_path)


def test_local_html_actual_conversion_saves_beside_html_even_with_download_folder(tmp_path):
    source = tmp_path / "本地文章.html"
    source.write_text("<html><head><title>网页标题</title></head><body><article><h1>网页标题</h1><p>虚构办公正文。</p></article></body></html>", encoding="utf-8")
    before = digest(source)
    result = quick.run_quick_action("web_to_word", [str(source)], tmp_path / "downloads")
    assert not result["errors"]
    assert Path(result["outputs"][0]) == tmp_path / "本地文章.docx"
    assert "虚构办公正文。" in "\n".join(p.text for p in Document(result["outputs"][0]).paragraphs)
    assert digest(source) == before and not (tmp_path / "downloads").exists()


def test_saved_mhtml_actual_conversion_is_offline_and_uses_original_parent(tmp_path):
    from email.message import EmailMessage
    message = EmailMessage()
    message.set_type("multipart/related")
    part = EmailMessage()
    part.set_content("<html><head><title>保存的网页</title></head><body><article><p>离线虚构归档内容。</p></article></body></html>", subtype="html", charset="utf-8")
    part["Content-Location"] = "https://example.com/saved-page"
    message.attach(part)
    source = tmp_path / "保存网页.mhtml"
    source.write_bytes(message.as_bytes())
    before = digest(source)
    with mock.patch("requests.Session.get", side_effect=AssertionError("saved page must remain offline")):
        result = quick.run_quick_action("web_to_word", [source], tmp_path / "unused")
    assert not result["errors"] and Path(result["outputs"][0]) == source.with_suffix(".docx")
    assert "离线虚构归档内容。" in "\n".join(p.text for p in Document(result["outputs"][0]).paragraphs)
    assert digest(source) == before
    assert not (tmp_path / "unused").exists()


def test_url_uses_generated_title_safely_and_existing_title_is_preserved(tmp_path):
    from qingjian.core import web_ops
    html = "<html><head><title>../../CON:财务/示例?</title></head><body><article><p>虚构说明正文，不能逃逸输出目录。</p></article></body></html>".encode("utf-8")
    downloads = tmp_path / "downloads" / "青简快捷转换"
    with mock.patch.object(web_ops._Reader, "_download", return_value=html):
        first = quick.run_quick_action("web_to_word", ["https://example.com/report"], downloads)
        second = quick.run_quick_action("web_to_word", ["https://example.com/report"], downloads)
    assert not first["errors"] and not second["errors"]
    output, repeated = Path(first["outputs"][0]), Path(second["outputs"][0])
    assert output.parent == downloads and repeated.parent == downloads
    assert output != repeated and repeated.stem.endswith("_2")
    assert not any(character in output.name for character in '<>:"/\\|?*')
    assert "财务" in output.name
    assert output.is_file() and repeated.is_file()
    assert_no_staging(downloads)


def test_url_requires_explicit_configured_directory_and_local_input_does_not(tmp_path):
    with mock.patch.object(quick, "_convert", side_effect=AssertionError("should not convert")):
        result = quick.run_quick_action("web_to_word", ["https://example.com/report"], None)
    assert result["errors"] and "保存位置" in result["errors"][0]
    assert result["outputs"] == []


def test_mixed_valid_unsupported_missing_folder_and_script_continue_safely(tmp_path):
    first = picture(tmp_path / "first.png")
    second = picture(tmp_path / "last.png")
    script, marker = tmp_path / "unsafe.py", tmp_path / "executed.txt"
    script.write_text(f"open({str(marker)!r}, 'w').write('bad')", encoding="utf-8")
    with mock.patch("subprocess.Popen", side_effect=AssertionError("no program execution")):
        result = quick.run_quick_action("image_to_jpg", [first, script, tmp_path / "missing.png", tmp_path, second], None)
    assert result["changes"] == 2 and len(result["errors"]) == 3
    assert [row["status"] for row in result["records"]] == ["success", "failed", "failed", "failed", "success"]
    assert not marker.exists()
    assert_no_staging(tmp_path)


def test_conversion_failure_removes_half_result_and_continues(tmp_path):
    first, second = picture(tmp_path / "broken.png"), picture(tmp_path / "good.png")
    actual_convert = quick._convert

    def convert(action_id, source, target, cancel):
        if Path(source) == first:
            target.write_bytes(b"not a complete output")
            raise ValueError("虚构失败")
        return actual_convert(action_id, source, target, cancel)

    with mock.patch.object(quick, "_convert", side_effect=convert):
        result = quick.run_quick_action("image_to_jpg", [first, second], None)
    assert result["changes"] == 1 and len(result["errors"]) == 1
    assert not (tmp_path / "broken.jpg").exists()
    assert (tmp_path / "good.jpg").exists()
    assert_no_staging(tmp_path)


def test_cancel_before_start_does_not_create_any_result_or_folder(tmp_path):
    result = quick.run_quick_action("web_to_word", ["https://example.com/"], tmp_path / "new", cancel=lambda: True)
    assert result["cancelled"] and result["outputs"] == [] and result["records"] == []
    assert not (tmp_path / "new").exists()


def test_cancel_current_item_discards_generated_file_before_publish(tmp_path):
    source = picture(tmp_path / "cancel.png")
    flag = [False]

    def convert(action_id, source, target, cancel):
        target.write_bytes(b"completed privately")
        flag[0] = True
        return {"changes": 1}

    with mock.patch.object(quick, "_convert", side_effect=convert):
        result = quick.run_quick_action("image_to_jpg", [source], None, cancel=lambda: flag[0])
    assert result["cancelled"] and result["outputs"] == []
    assert result["records"][0]["status"] == "cancelled"
    assert not (tmp_path / "cancel.jpg").exists()
    assert_no_staging(tmp_path)


def test_cancel_between_files_retains_only_completed_results(tmp_path):
    sources = [picture(tmp_path / f"source{number}.png") for number in (1, 2)]
    flag, events = [False], []

    def progress(done, total, message):
        events.append((done, total, message))
        if done == 1:
            flag[0] = True

    result = quick.run_quick_action("image_to_jpg", sources, None, progress, lambda: flag[0])
    assert result["cancelled"] and result["changes"] == 1 and result["total"] == 2
    assert Path(result["outputs"][0]).name == "source1.jpg"
    assert not (tmp_path / "source2.jpg").exists()
    assert events[-1][0:2] == (1, 2)
    assert_no_staging(tmp_path)


def test_write_permission_error_is_explicit_and_never_falls_back(tmp_path):
    source = picture(tmp_path / "restricted.png")
    fallback = tmp_path / "unused"
    with mock.patch.object(quick.tempfile, "TemporaryDirectory", side_effect=PermissionError("access denied")):
        result = quick.run_quick_action("image_to_jpg", [source], fallback)
    assert result["outputs"] == [] and "只读" in result["errors"][0]
    assert "未改存其他位置" in result["errors"][0]
    assert not fallback.exists()


def test_input_size_guard_and_batch_limit_do_not_start_converters(tmp_path):
    source = picture(tmp_path / "size.png")
    with mock.patch.object(quick, "MAX_SOURCE_BYTES", 1), mock.patch.object(quick, "_convert", side_effect=AssertionError("must not convert")):
        result = quick.run_quick_action("image_to_jpg", [source], None)
    assert "256 MB" in result["errors"][0]
    assert quick.run_quick_action("image_to_jpg", [source] * (quick.MAX_INPUTS + 1), None)["errors"]


def test_worker_executes_off_ui_thread_and_delivers_complete_contract(tmp_path, desktop_application):
    from PySide6.QtTest import QSignalSpy
    source = picture(tmp_path / "thread.png")
    main_thread, actual_threads = threading.get_ident(), []
    actual_convert = quick._convert

    def convert(*args):
        actual_threads.append(threading.get_ident())
        return actual_convert(*args)

    worker = quick.QuickActionWorker("image_to_jpg", [source], None)
    completed, progress = QSignalSpy(worker.completed), QSignalSpy(worker.progress)
    with mock.patch.object(quick, "_convert", side_effect=convert):
        worker.start()
        assert worker.wait(20000)
    desktop_application.processEvents()
    assert completed.count() == 1 and progress.count() >= 2
    result = completed.at(0)[0]
    assert result["action_id"] == "image_to_jpg" and result["total"] == 1
    assert result["changes"] == 1 and not result["errors"]
    assert actual_threads and actual_threads[0] != main_thread


def test_to_pdf_office_dispatch_uses_guarded_engine_and_cancel_callback(tmp_path):
    from qingjian.core import conversion
    source = word(tmp_path / "office.docx")
    before = digest(source)
    seen = []

    def engine(path, target, options):
        seen.append((path, target, options))
        pdf(target, "office renderer")
        return {"changes": 1, "warnings": []}

    cancel = lambda: False
    with mock.patch.object(conversion, "convert_office_to_pdf", side_effect=engine):
        result = quick.run_quick_action("to_pdf", [source], None, cancel=cancel)
    assert not result["errors"] and seen[0][0] == source
    assert seen[0][1].parent.parent == source.parent
    assert seen[0][2]["_cancel"] is cancel
    assert PdfReader(result["outputs"][0]).pages[0].extract_text().strip() == "office renderer"
    assert digest(source) == before


@pytest.mark.parametrize("extension", [".docx", ".xlsx", ".pptx"])
def test_actual_office_to_pdf_when_configured(tmp_path, extension):
    engine = os.environ.get("QINGJIAN_LIBREOFFICE")
    if not engine or not Path(engine).is_file():
        pytest.skip("Set QINGJIAN_LIBREOFFICE to run the actual bundled-engine conversion.")
    source = tmp_path / ("Native office example" + extension)
    if extension == ".docx":
        word(source, "Native office quick conversion")
    elif extension == ".xlsx":
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(["Native office quick conversion", 2026])
        book.save(source)
        book.close()
    else:
        example = Path(__file__).resolve().parents[1] / "示例材料" / "PPT模板示例.pptx"
        assert example.is_file()
        shutil.copyfile(example, source)
    before = digest(source)
    result = quick.run_quick_action("to_pdf", [source], None)
    assert not result["errors"] and result["changes"] == 1
    output = Path(result["outputs"][0])
    assert output == source.with_suffix(".pdf") and len(PdfReader(output).pages) >= 1
    assert digest(source) == before
    assert_no_staging(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows engine profile path limitation")
@pytest.mark.parametrize("temp_is_shorter", [True, False])
@pytest.mark.parametrize("fail_during_conversion", [True, False])
def test_engine_workspace_uses_short_private_root_and_cleans_only_its_own(tmp_path, temp_is_shorter, fail_during_conversion):
    from qingjian.core import conversion
    short = tmp_path / "short"
    deep = tmp_path / "a deliberately longer source parent" / "another nested staging directory"
    short.mkdir()
    deep.mkdir(parents=True)
    target_parent, temporary_root = (deep, short) if temp_is_shorter else (short, deep)
    unrelated = temporary_root / "unrelated-user-file.txt"
    unrelated.write_text("preserve this unrelated file", encoding="utf-8")
    created = []

    def invoke():
        with conversion._conversion_workspace(target_parent) as workspace:
            created.append(workspace)
            assert workspace.parent == short.resolve()
            assert workspace.name.startswith(".qj-")
            (workspace / "profile-cache").write_text("private temporary data", encoding="utf-8")
            if fail_during_conversion:
                raise ValueError("fictional conversion failure")

    with mock.patch.object(conversion.tempfile, "gettempdir", return_value=str(temporary_root)):
        if fail_during_conversion:
            with pytest.raises(ValueError, match="fictional conversion failure"):
                invoke()
        else:
            invoke()
    assert created and not created[0].exists()
    assert unrelated.read_text(encoding="utf-8") == "preserve this unrelated file"
