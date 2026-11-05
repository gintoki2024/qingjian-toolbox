"""The ten local office additions remain discoverable and require an explicit run."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect
from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton, QTextBrowser

from qingjian.app import MODES, Window
from qingjian.catalog import EXTRA_ORDER, OFFICE_SPECS, TOOLS
from qingjian.feature_registry import SPECS
from qingjian.forms import OptionPanel
from qingjian.guides import GUIDES, build_example, guide_html
from qingjian.tools import validate_tool_options
from qingjian.workbench import TOOL_GROUPS, TOOL_ITEMS, ToolCatalogPage, recommend_actions


OFFICE_MODES = tuple(SPECS)
LEGACY_MODES = (
    "word", "web", "convert", "image", "excel_merge", "excel_compare", "excel_clean",
    "template_fill", "extract_fields", "document_export", "pdf_pages", "image_pack",
    "file_organize", "content_search", "find_duplicates", "check_materials", "workflow",
)


def pump(app):
    for _ in range(3):
        app.processEvents()


def dispose(widget, app):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    pump(app)


@pytest.fixture
def office_window(desktop_application, tmp_path):
    window = Window(data_path=tmp_path / "test-profiles.json")
    window.output_edit.setText(str(tmp_path / "results"))
    window.show()
    pump(desktop_application)
    # Exercise the expanded tool layout after its initial screen-relative show.
    window.resize(1050, 760)
    pump(desktop_application)
    yield window
    window.set_busy(False)
    dispose(window, desktop_application)


def test_registry_adds_ten_tools_after_unchanged_legacy_mode_positions():
    assert tuple(MODES[:17]) == LEGACY_MODES
    assert tuple(MODES[17:]) == OFFICE_MODES
    assert len(MODES) == len(set(MODES)) == 27
    assert set(OFFICE_SPECS) == set(SPECS) == set(MODES) - set(LEGACY_MODES)
    assert all(mode in EXTRA_ORDER and mode in TOOL_ITEMS and mode in GUIDES for mode in OFFICE_MODES)
    assert not {"ocr", "whisper", "llm", "transcribe"}.intersection(MODES)


@pytest.mark.parametrize("mode", OFFICE_MODES)
def test_each_office_panel_roundtrips_examples_and_rejects_invalid_import_atomically(mode, desktop_application, tmp_path):
    sources, example = build_example(mode, tmp_path / mode)
    panel = OptionPanel(mode)
    try:
        defaults = panel.options()
        panel.set_options(defaults)
        assert panel.options() == defaults
        panel.set_options(example)
        actual = panel.options()
        for key, value in example.items():
            if key in panel.controls:
                assert actual[key] == value, (mode, key, actual[key], value)
        validate_tool_options(mode, actual, sources)
        panel.set_options(actual)
        assert panel.options() == actual
        field = next(item for item in TOOLS[mode]["fields"] if item["kind"] == "bool")
        with pytest.raises(ValueError):
            panel.set_options({field["key"]: "true"})
        assert panel.options() == actual
    finally:
        dispose(panel, desktop_application)


@pytest.mark.parametrize("mode", OFFICE_MODES)
def test_each_office_guide_can_load_practice_without_starting_work(mode, office_window, desktop_application, tmp_path, monkeypatch):
    window = office_window
    window.switch_mode(MODES.index(mode))
    original_sources, _ = build_example(mode, tmp_path / "original-examples")
    source_hashes = {Path(source): hashlib.sha256(Path(source).read_bytes()).hexdigest() for source in original_sources}
    window.accept_sources(original_sources)
    warning = Mock()
    tool_worker, batch_worker, preview_worker = Mock(), Mock(), Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    monkeypatch.setattr("qingjian.app.ToolWorker", tool_worker)
    monkeypatch.setattr("qingjian.app.BatchWorker", batch_worker)
    monkeypatch.setattr("qingjian.app.PreviewWorker", preview_worker)
    opened = []

    def inspect_dialog(dialog):
        assert TOOLS[mode]["title"] in dialog.windowTitle()
        browser = dialog.findChild(QTextBrowser)
        assert browser is not None
        plain = browser.toPlainText()
        assert GUIDES[mode]["intro"] in plain
        assert "怎么操作" in plain and "字段怎么填" in plain and "一个例子" in plain
        practice = next(button for button in dialog.findChildren(QPushButton) if button.text() == "载入本页示例")
        assert "开始处理" in practice.toolTip()
        practice.click()
        opened.append(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", inspect_dialog)
    assert window.guide_button.isVisible()
    window.show_tool_guide()
    pump(desktop_application)
    assert opened == [True]
    warning.assert_not_called()
    assert window.files[mode]
    assert window.files[mode] != original_sources
    assert all(Path(source).is_file() for source in window.files[mode])
    assert all("上手示例" in str(source) for source in window.files[mode])
    validate_tool_options(mode, window.get_options(), window.files[mode])
    assert "点击开始处理" in window.status.text()
    assert window.worker is None and not window._busy
    assert window.last_result is None
    for worker in (tool_worker, batch_worker, preview_worker):
        worker.assert_not_called()
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in source_hashes.items())


@pytest.mark.parametrize("mode", OFFICE_MODES)
def test_each_office_tool_is_searchable_by_name_and_routes_in_its_category(mode, desktop_application):
    page = ToolCatalogPage()
    selected = []
    page.tool_requested.connect(selected.append)
    try:
        page.search_edit.setText(TOOLS[mode]["title"])
        assert mode in page.visible_modes
        page.search_edit.clear()
        group = next(group for group in TOOL_GROUPS if mode in group["modes"])
        page.set_group(group["id"])
        assert mode in page.visible_modes
        assert all(TOOL_ITEMS[item]["group"] == group["id"] for item in page.visible_modes)
        page.tool_cards[mode].click()
        assert selected == [mode]
        page.search_edit.setText("不会存在的办公功能名字_123456")
        assert page.visible_modes == []
        assert not page.empty_label.isHidden()
    finally:
        dispose(page, desktop_application)


def test_all_twenty_seven_tools_keep_action_bar_visible_at_minimum_window_size(office_window, desktop_application):
    window = office_window
    window.resize(980, 680)
    for mode in MODES:
        window.switch_mode(MODES.index(mode))
        pump(desktop_application)
        assert window.settings.currentIndex() == MODES.index(mode)
        assert window.queue_panel.isHidden()
        controls = [window.start_button, window.result_button]
        if mode in {"content_search", "find_duplicates"}:
            # Interactive results stay in their own windows and do not export
            # reports, so these two modes deliberately have no output path.
            assert window.output_row_widget.isHidden(), mode
            assert not window.output_edit.isVisible(), mode
            assert not window.choose_output_button.isVisible(), mode
        else:
            assert window.output_row_widget.isVisible(), mode
            controls.extend((window.output_edit, window.choose_output_button))
        for control in controls:
            assert control.isVisible(), mode
            rectangle = QRect(control.mapTo(window, QPoint(0, 0)), control.size())
            assert window.rect().contains(rectangle), (mode, rectangle, window.rect())
        assert window.worker is None and not window._busy, mode
        assert window.workspace_scroll.horizontalScrollBar().maximum() == 0, mode


def test_office_page_switch_keeps_each_queue_and_options_separate_without_processing(office_window, desktop_application, tmp_path, monkeypatch):
    window = office_window
    worker = Mock()
    monkeypatch.setattr("qingjian.app.ToolWorker", worker)
    saved = {}
    for mode in OFFICE_MODES:
        sources, options = build_example(mode, tmp_path / (mode + "-source"))
        window.switch_mode(MODES.index(mode))
        window.apply_options(mode, options)
        window.accept_sources(sources)
        saved[mode] = (window.files[mode].copy(), window.get_options())
    for mode, (sources, options) in saved.items():
        window.switch_mode(MODES.index(mode))
        pump(desktop_application)
        assert window.files[mode] == sources
        assert window.get_options() == options
    assert window.worker is None and window.last_result is None
    worker.assert_not_called()


def test_practice_loading_is_blocked_while_busy_or_on_another_page(office_window, monkeypatch):
    window = office_window
    builder = Mock()
    monkeypatch.setattr("qingjian.app.build_example", builder)
    window.switch_mode(MODES.index("document_compare"))
    assert not window.load_tool_example("material_consistency")
    window.set_busy(True)
    assert not window.load_tool_example("document_compare")
    builder.assert_not_called()


def test_new_queue_hints_and_table_column_helpers_are_visible(office_window, desktop_application):
    window = office_window
    for mode, fragments in (("document_compare", ("旧版", "新版")), ("excel_lookup", ("目标表", "参考表")), ("submission_bind", ("顺序", "装订"))):
        window.switch_mode(MODES.index(mode))
        pump(desktop_application)
        assert all(fragment in window.empty_hint.text() for fragment in fragments)
    for mode in ("excel_audit", "excel_lookup", "excel_print"):
        window.switch_mode(MODES.index(mode))
        pump(desktop_application)
        assert window.columns_button.isVisible()


def test_recommendations_offer_rule_review_and_assembly_without_reading_inputs(monkeypatch):
    def forbid(*args, **kwargs):
        raise AssertionError("建议操作不应读取文件内容")
    monkeypatch.setattr(Path, "read_bytes", forbid)
    monkeypatch.setattr(Path, "read_text", forbid)
    pairs = recommend_actions(["旧版.docx", "新版.pdf"])
    assert {"document_compare", "material_consistency", "submission_bind"} <= {action["id"] for action in pairs}
    one = recommend_actions(["材料.docx"])
    assert "document_compare" not in {action["id"] for action in one}
    ppt = recommend_actions(["材料.pptx"])
    assert {"ppt_prepare", "submission_preflight"} <= {action["id"] for action in ppt}
    assert all(action["files"] == ["旧版.docx", "新版.pdf"] for action in pairs)
