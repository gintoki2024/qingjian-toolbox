"""Navigate real tool pages back to their origin without losing drafts."""
import copy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian.app import MODES, Window
from qingjian.workbench import TOOL_GROUPS


@pytest.fixture
def window(tmp_path):
    value = Window(start_home=True, data_path=tmp_path / "profile.json")
    value.show()
    QApplication.processEvents()
    yield value
    value.set_busy(False)
    value.close()
    value.deleteLater()


def test_every_tool_has_visible_back_to_its_group(window):
    assert window.page_navigation.isHidden()
    visited = set()
    for group in TOOL_GROUPS:
        window.home_page.group_cards[group["id"]].click()
        QApplication.processEvents()
        assert window.page_navigation.isVisible()
        assert window.parent_label.full_text == "首页"
        for mode in group["modes"]:
            window.catalog_page.tool_cards[mode].click()
            QApplication.processEvents()
            assert window.mode == mode
            assert window.content_stack.currentWidget() is window.job_page
            assert window.back_button.isVisible(), mode
            assert window.back_button.isEnabled(), mode
            assert window.parent_label.full_text == group["title"]
            QTest.mouseClick(window.back_button, Qt.MouseButton.LeftButton)
            QApplication.processEvents()
            assert window.content_stack.currentWidget() is window.catalog_page, mode
            assert window.catalog_page.current_group == group["id"]
            assert window.worker is None
            visited.add(mode)
        window.back_button.click()
        assert window.content_stack.currentWidget() is window.home_page
    assert visited == set(MODES)


def test_back_preserves_catalog_filter_search_scroll_and_tool_draft(window):
    window.open_tool_group("doc")
    window.catalog_page.search_edit.setText("公文")
    QApplication.processEvents()
    visible = list(window.catalog_page.visible_modes)
    scroll = window.catalog_page.scroll.verticalScrollBar()
    scroll.setValue(scroll.maximum())
    previous_scroll = scroll.value()
    window.catalog_page.tool_cards["word"].click()
    window.word_tabs.setCurrentIndex(1)
    window.official_panel.enabled.setChecked(True)
    window.official_panel.controls["body_size"].setValue(17)
    window.files["word"] = ["保留的文件.docx"]
    before = copy.deepcopy(window.get_options("word"))
    assert window.go_back()
    QApplication.processEvents()
    assert window.catalog_page.current_group == "doc"
    assert window.catalog_page.search_edit.text() == "公文"
    assert window.catalog_page.visible_modes == visible
    assert scroll.value() == min(previous_scroll, scroll.maximum())
    window.catalog_page.tool_cards["word"].click()
    assert window.get_options("word") == before
    assert window.files["word"] == ["保留的文件.docx"]


def test_preset_and_sidebar_tools_return_to_original_page(window):
    window.open_scene("official")
    preset = window.profile_store.save_preset("示例排版", "word", window.get_options(), "official")
    window.show_surface("presets")
    assert window.open_preset(preset["id"])
    assert window.parent_label.full_text == "常用方案"
    # Moving between peer tools should not build a long back-history chain.
    window.switch_mode(MODES.index("excel_clean"))
    assert window.go_back()
    assert window.content_stack.currentWidget() is window.presets_page
    assert window.go_back()
    assert window.content_stack.currentWidget() is window.home_page


def test_home_scenes_surface_back_and_busy_guard(window):
    assert window.open_scene("official")
    assert window.parent_label.full_text == "首页"
    window.set_busy(True)
    assert not window.back_button.isEnabled()
    assert not window.go_back()
    assert window.content_stack.currentWidget() is window.job_page
    window.set_busy(False)
    assert window.back_button.isEnabled()
    window.activateWindow()
    window.setFocus()
    QApplication.processEvents()
    QTest.keyClick(window, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
    QApplication.processEvents()
    assert window.content_stack.currentWidget() is window.home_page
    assert window.page_navigation.isHidden()
    for name in ("catalog", "presets", "history"):
        window.show_surface(name)
        assert window.back_button.isVisible()
        assert window.go_back()
        assert window.content_stack.currentWidget() is window.home_page
    assert not window.go_back()
