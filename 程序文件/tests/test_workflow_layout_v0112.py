"""Bounded workflow editors and real business-file chaining for new steps."""
import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image
from pypdf import PdfReader
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget
from reportlab.pdfgen import canvas

from qingjian.catalog import WORKFLOW_CHOICES
from qingjian.core.conversion import find_libreoffice
from qingjian.forms import WorkflowPanel
from qingjian.tools import execute_tool, validate_tool_options


NEW_STEPS = {"word_chapters", "document_export", "excel_print", "pdf_finish", "submission_bind"}


def pump():
    for _ in range(5):
        QApplication.processEvents()


def bounds(widget, ancestor):
    return QRect(widget.mapTo(ancestor, QPoint()), widget.size())


def reveal(scroll, control):
    # Center the painted control, not QLineEdit's smaller text focus rectangle.
    top = control.mapTo(scroll.widget(), QPoint()).y()
    scroll.verticalScrollBar().setValue(top - max(0, (scroll.viewport().height() - control.height()) // 2))
    pump()


def assert_wrapped_labels_fit(panel):
    for label in panel.findChildren(QLabel):
        if label.isVisible() and label.wordWrap():
            assert panel.rect().contains(bounds(label, panel)), label.text()
            assert label.height() >= label.heightForWidth(label.width()), label.text()


@pytest.mark.parametrize("size", [(400, 500), (480, 600), (640, 800)])
def test_step_controls_scroll_without_stretching_the_window(size):
    host = QWidget()
    layout = QVBoxLayout(host)
    editor = WorkflowPanel()
    layout.addWidget(editor)
    host.resize(*size)
    host.show()
    try:
        for _, mode in WORKFLOW_CHOICES:
            editor.set_options({"steps": [{"tool": mode}]})
            pump()
            assert host.width() == size[0], mode
            assert host.height() == size[1], mode
            step = editor.tabs.currentWidget()
            scroll = step.options_scroll
            viewport = scroll.viewport()
            assert scroll.horizontalScrollBar().maximum() == 0, mode
            assert step.panel.width() <= viewport.width(), mode
            assert_wrapped_labels_fit(step.panel)
            for control in step.panel.controls.values():
                if not control.isVisible():
                    continue
                reveal(scroll, control)
                assert viewport.rect().contains(bounds(control, viewport)), (mode, control.objectName())
            for control in editor.findChildren(QPushButton, options=Qt.FindChildOption.FindDirectChildrenOnly):
                assert editor.rect().contains(bounds(control, editor)), control.text()
            assert editor.rect().contains(bounds(editor.archive, editor))
            assert editor.rect().contains(bounds(editor.stop_on_error, editor))
    finally:
        host.close()
        host.deleteLater()


@pytest.mark.parametrize("size", [(480, 600), (640, 800)])
def test_workflow_in_real_main_window_keeps_last_fields_and_actions_reachable(tmp_path, size):
    from qingjian.app import MODES, Window
    window = Window(start_home=True, data_path=tmp_path / "profile.json")
    window.show()
    pump()  # Initial screen placement runs on the first show event.
    window.resize(*size)
    window.switch_mode(MODES.index("workflow"))
    editor = window.extra_panels["workflow"]
    step = editor.tabs.currentWidget()
    step.choice.setCurrentIndex(step.choice.findData("excel_clean"))
    pump()
    window.fit_settings()
    pump()
    try:
        assert (window.width(), window.height()) == size
        assert window.workspace_scroll.horizontalScrollBar().maximum() == 0
        assert window.settings_scroll.horizontalScrollBar().maximum() == 0
        assert window.settings.width() <= window.settings_scroll.viewport().width()
        assert editor.width() <= window.settings.width()
        assert step.panel.width() <= step.options_scroll.viewport().width()
        assert_wrapped_labels_fit(step.panel)
        assert step.options_scroll.verticalScrollBar().maximum() > 0
        outer = window.workspace_scroll
        for key in ("sheet", "text_columns", "recursive"):
            control = step.panel.controls[key]
            reveal(step.options_scroll, control)
            reveal(outer, control)
            assert step.options_scroll.viewport().rect().contains(bounds(control, step.options_scroll.viewport()))
            assert outer.viewport().rect().contains(bounds(control, outer.viewport()))
            assert window.rect().contains(bounds(window.start_button, window))
            assert window.start_button.isVisible()
        for control in [*editor.findChildren(QPushButton, options=Qt.FindChildOption.FindDirectChildrenOnly),
                        editor.archive, editor.stop_on_error]:
            reveal(outer, control)
            assert outer.viewport().rect().contains(bounds(control, outer.viewport())), control.text()
            assert window.rect().contains(bounds(window.start_button, window))
    finally:
        window.close()
        window.deleteLater()


def test_clean_options_remain_editable_at_bottom_and_persist_after_switching():
    host = QWidget()
    layout = QVBoxLayout(host)
    editor = WorkflowPanel()
    layout.addWidget(editor)
    editor.set_options({"steps": [{"tool": "excel_clean"}, {"tool": "pdf_finish"}]})
    host.resize(480, 600)
    host.show()
    editor.tabs.setCurrentIndex(0)
    pump()
    try:
        step = editor.tabs.currentWidget()
        control = step.panel.controls["text_columns"]
        assert step.options_scroll.verticalScrollBar().maximum() > 0
        step.options_scroll.ensureWidgetVisible(control)
        pump()
        QTest.mouseClick(control, Qt.MouseButton.LeftButton)
        QTest.keyClicks(control, "EmployeeCode")
        editor.tabs.setCurrentIndex(1)
        editor.tabs.setCurrentIndex(0)
        assert editor.options()["steps"][0]["options"]["text_columns"] == ["EmployeeCode"]
        assert editor.tabs.usesScrollButtons()
        with patch.object(QMessageBox, "information") as explain:
            editor.scope_button.click()
        assert explain.call_count == 1
        assert "全文检索" not in {title for title, _ in WORKFLOW_CHOICES}
        assert NEW_STEPS <= {key for _, key in WORKFLOW_CHOICES}
        assert "独立工具入口" in explain.call_args.args[2]
    finally:
        host.close()
        host.deleteLater()


def pdf(path, *pages):
    sheet = canvas.Canvas(str(path), pagesize=(300, 400))
    for text in pages:
        sheet.drawString(40, 300, text)
        sheet.showPage()
    sheet.save()
    return path


def workbook(path):
    book = Workbook()
    book.active.append(["Code", "Name"])
    book.active.append(["001", "Alice"])
    book.save(path)
    book.close()
    return path


def workflow(tmp_path, mode, inputs, options, *, final_step=None):
    attachment = tmp_path / "keep.bin"
    attachment.write_bytes(b"unmatched attachment")
    sources = [*inputs, attachment]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    steps = [{"tool": mode, "options": options}]
    if final_step:
        steps.append(final_step)
    steps.append({"tool": "file_organize", "options": {"action": "copy", "filename_template": "{原名}"}})
    recipe = {"steps": steps, "archive": True, "stop_on_error": True}
    validate_tool_options("workflow", recipe)
    result = execute_tool("workflow", sources, tmp_path / "result", recipe)
    assert not result["errors"], result["errors"]
    assert not result.get("cancelled")
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in before.items())
    outputs = [Path(value) for value in result["data_outputs"]]
    assert all(path.is_relative_to(tmp_path / "result") for path in outputs)
    assert any(path.read_bytes() == attachment.read_bytes() for path in outputs)
    archive = next(Path(value) for value in result["outputs"] if value.endswith(".zip"))
    with zipfile.ZipFile(archive) as stream:
        assert len(stream.namelist()) == len(outputs)
        assert all("清单" not in value and "处理记录" not in value and "报告" not in value for value in stream.namelist())
    stages = json.loads((tmp_path / "result" / "流程记录.json").read_text(encoding="utf-8"))["步骤"]
    assert all(stage["状态"] == "完成" for stage in stages)
    return result, outputs


@pytest.mark.parametrize("action", ["split", "merge"])
def test_word_chapters_chain_preserves_docx_and_sources(tmp_path, action):
    documents = []
    for index in range(2):
        document = Document()
        document.add_heading(f"Part {index + 1}", 1)
        document.add_paragraph(f"Text {index + 1}")
        if action == "split":
            document.add_heading("Second chapter", 1)
            document.add_paragraph("Second text")
        target = tmp_path / f"source{index}.docx"
        document.save(target)
        documents.append(target)
        if action == "split":
            break
    _, outputs = workflow(tmp_path, "word_chapters", documents, {"action": action, "heading_level": 1})
    docs = [path for path in outputs if path.suffix == ".docx"]
    assert len(docs) == (2 if action == "split" else 1)
    text = "\n".join(paragraph.text for path in docs for paragraph in Document(path).paragraphs)
    assert "Text 1" in text
    assert ("Second text" if action == "split" else "Text 2") in text


def test_document_text_exports_feed_copy_archive_without_report_pollution(tmp_path):
    document = Document()
    document.add_paragraph("Export this paragraph")
    source = tmp_path / "source.docx"
    document.save(source)
    _, outputs = workflow(tmp_path, "document_export", [source], {"action": "text"})
    text = [path for path in outputs if path.suffix == ".txt"]
    assert len(text) == 1
    assert "Export this paragraph" in text[0].read_text(encoding="utf-8")


def test_image_export_carries_nonword_files_to_next_step(tmp_path):
    image = tmp_path / "embedded.png"
    Image.new("RGB", (40, 30), "blue").save(image)
    document = Document()
    document.add_picture(str(image))
    source = tmp_path / "with-picture.docx"
    document.save(source)
    original_pdf = pdf(tmp_path / "keep.pdf", "Keep this original PDF")
    original_text = tmp_path / "keep.txt"
    original_text.write_text("Keep this original text", encoding="utf-8")
    _, outputs = workflow(tmp_path, "document_export", [source, original_pdf, original_text], {"action": "images"})
    extracted = [path for path in outputs if path.suffix == ".png"]
    assert len(extracted) == 1
    with Image.open(extracted[0]) as result:
        assert result.size == (40, 30)
    assert any(path.read_bytes() == original_pdf.read_bytes() for path in outputs)
    assert any(path.read_bytes() == original_text.read_bytes() for path in outputs)


@pytest.mark.parametrize("output_pdf", [False, True])
def test_excel_print_chains_print_copy_and_optional_pdf(tmp_path, output_pdf):
    if output_pdf and find_libreoffice() is None:
        pytest.skip("实际打印PDF串联验收需要现有LibreOffice，不自动下载")
    source = workbook(tmp_path / "source.xlsx")
    _, outputs = workflow(tmp_path, "excel_print", [source], {
        "orientation": "landscape", "paper": "A4", "fit_width": True,
        "repeat_rows": "1:1", "print_area": "A1:B2", "output_pdf": output_pdf})
    copies = [path for path in outputs if path.suffix == ".xlsx"]
    assert len(copies) == 1
    book = load_workbook(copies[0])
    try:
        assert book.active["A2"].value == "001"
        assert book.active.page_setup.orientation == "landscape"
        assert book.active.print_title_rows == "$1:$1"
    finally:
        book.close()
    pdfs = [path for path in outputs if path.suffix == ".pdf"]
    assert len(pdfs) == int(output_pdf)
    if output_pdf:
        assert len(PdfReader(pdfs[0]).pages) == 1
        assert "Alice" in PdfReader(pdfs[0]).pages[0].extract_text()


def test_pdf_finish_chains_valid_rotated_pdf_without_reports(tmp_path):
    source = pdf(tmp_path / "source.pdf", "First page", "Second page")
    _, outputs = workflow(tmp_path, "pdf_finish", [source], {"rotation": 90, "pages": "1", "compress": True})
    pdfs = [path for path in outputs if path.suffix == ".pdf"]
    assert len(pdfs) == 1
    reader = PdfReader(pdfs[0])
    assert len(reader.pages) == 2
    assert reader.pages[0].rotation == 90
    assert reader.pages[1].rotation == 0
    assert "First page" in reader.pages[0].extract_text()


def test_submission_book_feeds_pdf_finishing_and_copy_archive(tmp_path):
    first = pdf(tmp_path / "first.pdf", "First material")
    second = pdf(tmp_path / "second.pdf", "Second material")
    _, outputs = workflow(tmp_path, "submission_bind", [first, second], {
        "book_title": "Practice materials", "cover": True, "contents": True,
        "add_page_numbers": True, "duplex_blank": True},
        final_step={"tool": "pdf_finish", "options": {"compress": True}})
    pdfs = [path for path in outputs if path.suffix == ".pdf"]
    assert len(pdfs) == 1
    reader = PdfReader(pdfs[0])
    assert len(reader.pages) >= 4
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "First material" in text
    assert "Second material" in text

