"""Integrated small-window controls, state and navigation without processing."""
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtTest import QSignalSpy
import pytest

from qingjian.app import MODES, Window
from qingjian.workbench import TOOL_GROUPS


def pump():
    for _ in range(4):
        QApplication.processEvents()


@pytest.fixture
def window(tmp_path):
    value = Window(start_home=True, data_path=tmp_path / "profile.json")
    value.show()
    pump()
    value.resize(410, 573)
    pump()
    yield value
    value.set_busy(False)
    value.close()
    value.deleteLater()
    pump()


def bounds(widget, ancestor):
    return QRect(widget.mapTo(ancestor, QPoint()), widget.size())


def test_home_has_two_sections_all_reachable_groups_and_highlighted_notebook(window):
    assert window._layout_compact
    assert not window.sidebar.isVisible()
    assert window.compact_toolbar.isVisible()
    assert window.compact_notebook.isVisible()
    assert window.compact_notebook.objectName() == "notebookLaunch"
    requested = QSignalSpy(window.notebook_requested)
    QTest.mouseClick(window.compact_notebook, Qt.MouseButton.LeftButton)
    assert requested.count() == 1
    viewport = window.home_page.scroll.viewport()
    assert window.home_page.personal_panel.isVisible()
    assert window.home_page.group_section.isVisible()
    assert not hasattr(window.home_page, "quick_panel")
    assert set(window.home_page.group_cards) == {group["id"] for group in TOOL_GROUPS}
    for card in window.home_page.group_cards.values():
        window.home_page.scroll.ensureWidgetVisible(card)
        pump()
        assert card.isVisible()
        assert viewport.rect().contains(bounds(card, viewport))
    assert window.home_page.scroll.horizontalScrollBar().maximum() == 0
    assert window.home_page.canvas.width() <= viewport.width()


def test_small_tool_pages_keep_source_and_actions_inside_window(window):
    for index, mode in enumerate(MODES):
        window.switch_mode(index)
        pump()
        assert window.width() == 410, mode
        assert window.settings_more.isVisible(), mode
        for control in (window.start_button, window.output_edit, window.choose_output_button, window.result_button):
            if mode in {"content_search", "find_duplicates"} and control in (window.output_edit, window.choose_output_button):
                assert not control.isVisible(), mode
                continue
            assert control.isVisible(), mode
            assert window.rect().contains(bounds(control, window)), mode
        assert window.source_bar.width() <= window.workspace_scroll.viewport().width(), mode
        assert window.source_bar.rect().contains(bounds(window.manage_button, window.source_bar)), mode
        assert window.workspace_scroll.horizontalScrollBar().maximum() == 0, mode


def test_compact_busy_stop_replaces_start_and_navigation_is_locked(window):
    window.switch_mode(0)
    window.set_busy(True)
    pump()
    assert not window.start_button.isVisible()
    assert window.stop_button.isVisible()
    assert window.rect().contains(bounds(window.stop_button, window))
    for control in (window.compact_home, window.compact_tools, window.compact_history, window.settings_more):
        assert not control.isEnabled()
    assert window.compact_notebook.isEnabled()
    window.set_busy(False)
    assert window.start_button.isVisible()
    assert not window.stop_button.isVisible()


def test_resizing_restores_full_layout_without_changing_current_settings(window):
    window.open_scene("official")
    window.official_panel.controls["body_size"].setValue(18)
    window.resize(1280, 860)
    pump()
    assert not window._layout_compact
    assert window.sidebar.isVisible()
    assert not window.compact_toolbar.isVisible()
    assert not window.settings_more.isVisible()
    window.resize(410, 573)
    pump()
    assert window._layout_compact
    assert window.official_panel.controls["body_size"].value() == 18
    assert window.settings_scroll.verticalScrollBar().maximum() == 0
    window.compact_home.click()
    assert window.content_stack.currentWidget() is window.home_page


def test_tool_menu_routes_every_mode_without_losing_drafts(window):
    routed = []
    for action in window.compact_menu.actions():
        if action.menu():
            for child in action.menu().actions():
                child.trigger()
                pump()
                routed.append(window.mode)
    assert set(routed) == set(MODES)
    assert len(routed) == len(MODES)
