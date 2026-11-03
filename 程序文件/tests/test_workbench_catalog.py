"""Workbench routing must stay conservative and never launch processing."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

from qingjian.catalog import EXTRA_ORDER, OFFICE_SPECS
from qingjian.workbench import (
    HomePage, SCENES, SourceActionDialog, TOOL_GROUPS, TOOL_ITEMS,
    ToolCatalogPage, recommend_actions,
)


def _ids(actions):
    return {action["id"] for action in actions}


def _pump(app):
    for _ in range(3):
        app.processEvents()


def test_catalog_routes_cover_all_twenty_seven_tools_once():
    expected = {"word", "web", "convert", "image", *EXTRA_ORDER}
    assert set(TOOL_ITEMS) == expected
    grouped = [mode for group in TOOL_GROUPS for mode in group["modes"]]
    assert set(grouped) == expected
    assert len(grouped) == len(expected) == 27
    assert len(OFFICE_SPECS) == 10
    assert [scene["id"] for scene in SCENES] == ["official", "materials", "redact", "compare", "template", "convert"]
    assert all(scene["mode"] in expected for scene in SCENES)
    assert SCENES[0]["tab"] == "公文排版"
    assert SCENES[2]["tab"] == "脱敏"


@pytest.mark.parametrize("sources", [[], None, [""], ["bad\x00.docx"], [17], ["unknown.bin"], ["https://"], ["a.docx", "b.bin"]])
def test_invalid_or_unknown_selection_has_no_recommendations(sources):
    assert recommend_actions(sources) == []


def test_word_recommends_official_and_custom_redaction_without_mutation():
    sources = ["通知.DOCX", "材料.doc"]
    actions = recommend_actions(sources)
    assert {"official", "redact", "convert", "text_export"} <= _ids(actions)
    assert actions[0]["scene"] == "official"
    assert all(action["files"] == sources for action in actions)
    assert sources == ["通知.DOCX", "材料.doc"]


def test_mixed_word_pdf_only_offers_compatible_actions():
    actions = recommend_actions(["a.docx", "b.pdf"])
    assert {"convert", "text_export", "extract_fields"} <= _ids(actions)
    assert not {"official", "redact", "pdf_pages"} & _ids(actions)


def test_text_may_redact_or_replace_but_never_official_format():
    actions = recommend_actions(["a.docx", "b.txt"])
    assert {"redact", "replace", "text_export", "content_search"} <= _ids(actions)
    assert not {"official", "convert"} & _ids(actions)
    assert next(action for action in actions if action["id"] == "replace")["tab"] == "查找替换"


@pytest.mark.parametrize("sources,compare", [(["a.xlsx"], False), (["a.xlsx", "b.csv"], True), (["a.xlsx", "b.csv", "c.tsv"], False), (["a.xlsx", "a.xlsx"], False)])
def test_compare_requires_exactly_two_distinct_supported_tables(sources, compare):
    actions = recommend_actions(sources)
    assert ("compare" in _ids(actions)) is compare
    assert {"excel_clean", "template", "excel_merge"} <= _ids(actions)


def test_ppt_template_is_a_template_not_input_data():
    actions = recommend_actions(["通知模板.PPTX"])
    assert _ids(actions) == {"template", "submission_preflight", "ppt_prepare"}
    template = next(action for action in actions if action["id"] == "template")
    assert template["mode"] == "template_fill"
    assert template["template_path"] == "通知模板.PPTX"
    assert template["files"] == []
    assert _ids(recommend_actions(["模板.pptx", "数据.xlsx"])) == {"submission_preflight", "submission_bind"}
    assert _ids(recommend_actions(["a.pptx", "b.pptx"])) == {"submission_preflight", "ppt_prepare", "submission_bind"}


def test_local_directory_recommends_materials_without_scanning(tmp_path):
    directory = tmp_path / "材料"
    directory.mkdir()
    actions = recommend_actions([str(directory)])
    assert _ids(actions) == {"materials", "content_search", "find_duplicates", "file_organize", "material_consistency"}
    assert actions[0]["mode"] == "check_materials"
    assert list(directory.iterdir()) == []


def test_image_and_url_choices_do_not_offer_unsupported_word_tools():
    assert _ids(recommend_actions(["a.jpg", "b.png"])) == {"image", "image_pack", "submission_bind"}
    assert _ids(recommend_actions(["https://example.com/article", "本地.html"])) == {"web"}
    assert _ids(recommend_actions(["a.pdf", "b.jpg"])) == {"submission_bind"}


def test_home_group_buttons_emit_each_category_without_legacy_shortcuts(desktop_application):
    page = HomePage()
    seen = []
    page.group_requested.connect(seen.append)
    for key, card in page.group_cards.items():
        card.click()
        assert seen[-1] == key
    assert seen == [group["id"] for group in TOOL_GROUPS]
    assert not hasattr(page, "quick_panel")
    assert not hasattr(page, "source_bar")
    assert not hasattr(page, "scene_cards")
    assert page.personal_panel.parent() is page.canvas
    assert page.group_section.parent() is page.canvas
    page.close()
    page.deleteLater()


def test_home_notebook_and_catalog_buttons_only_emit_requests(desktop_application, tmp_path):
    page = HomePage()
    page.resize(850, 680)
    page.show()
    _pump(desktop_application)
    notebook, catalogs = [], []
    page.notebook_requested.connect(lambda: notebook.append(True))
    page.catalog_requested.connect(lambda: catalogs.append(True))
    QTest.mouseClick(page.personal_panel.notebook_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.catalog_button, Qt.MouseButton.LeftButton)
    assert notebook == [True]
    assert catalogs == [True]
    page.setEnabled(False)
    QTest.mouseClick(page.personal_panel.notebook_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.catalog_button, Qt.MouseButton.LeftButton)
    assert notebook == [True] and catalogs == [True]
    page.close()
    page.deleteLater()


def test_catalog_search_category_and_button_route(desktop_application):
    page = ToolCatalogPage()
    seen = []
    page.tool_requested.connect(seen.append)
    assert len(page.visible_modes) == 27
    page.search_edit.setText("公文")
    assert page.visible_modes == ["word"]
    page.tool_cards["word"].click()
    assert seen == ["word"]
    page.search_edit.clear()
    page.set_group("table")
    assert set(page.visible_modes) == {"excel_merge", "excel_compare", "excel_clean", "check_materials", "material_consistency", "excel_audit", "excel_lookup", "excel_print"}
    page.search_edit.setText("PPT")
    assert page.visible_modes == []
    assert not page.empty_label.isHidden()
    page.set_group("all")
    assert {"template_fill", "ppt_prepare", "submission_preflight"} <= set(page.visible_modes)
    page.set_group("not-a-group")
    assert page.current_group == "all"
    page.close()
    page.deleteLater()


@pytest.mark.parametrize("size,home_columns,catalog_columns", [((1000, 680), 2, 3), ((720, 680), 1, 2), ((460, 680), 1, 1)])
def test_pages_reflow_without_horizontal_overflow(desktop_application, size, home_columns, catalog_columns):
    for kind in (HomePage, ToolCatalogPage):
        page = kind()
        page.resize(*size)
        page.show()
        _pump(desktop_application)
        grid = page.group_grid if kind is HomePage else page.tool_grid
        assert grid.columns == (home_columns if kind is HomePage else catalog_columns)
        assert page.scroll.horizontalScrollBar().maximum() == 0
        assert page.canvas.width() <= page.scroll.viewport().width()
        page.close()
        page.deleteLater()


def test_action_dialog_returns_a_choice_without_processing(desktop_application):
    dialog = SourceActionDialog(["通知.docx"])
    assert dialog.selected_action is None
    dialog.action_cards["official"].click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_action["scene"] == "official"
    assert dialog.selected_action["files"] == ["通知.docx"]
    dialog.close()
    dialog.deleteLater()


def test_unsupported_dialog_can_cancel(desktop_application):
    dialog = SourceActionDialog(["unknown.bin"])
    assert dialog.actions == []
    assert dialog.selected_action is None
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.close()
    dialog.deleteLater()
