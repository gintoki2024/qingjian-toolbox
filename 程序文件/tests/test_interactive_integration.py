"""Main-window routing, shared execution slot and floating appearance regression."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from qingjian.app import MODES, Window
from qingjian.core.search_session import SearchReport
from qingjian.guides import build_example, guide_html
from qingjian.notebook_runtime import NotebookController
from qingjian.notebook_store import NotebookStore


def until(predicate, seconds=10):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        QApplication.processEvents()
        QTest.qWait(3)
    assert predicate(), "main-window integration did not finish"


@pytest.fixture
def toolbox(tmp_path):
    window = Window(start_home=True, data_path=tmp_path / "user" / "profile.json")
    window.show()
    QApplication.processEvents()
    yield window
    window.shutdown_interactive()
    until(lambda: window._interactive_owner is None)
    for dialog in window.interactive_dialogs.values():
        dialog.close()
        dialog.deleteLater()
    if window.converter_window is not None:
        window.converter_window.shutdown()
        window.converter_window.deleteLater()
    window.window_snap.shutdown()
    window.appearance.shutdown()
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.mark.parametrize("mode", ["content_search", "find_duplicates"])
def test_main_routes_to_window_without_export_worker_or_result_folder(toolbox, tmp_path, mode):
    sources, options = build_example(mode, tmp_path / "practice")
    originals = {p: p.read_bytes() for p in Path(sources[0]).iterdir()}
    output = tmp_path / "must-not-be-created"
    toolbox.output_edit.setText(str(output))
    toolbox.switch_mode(MODES.index(mode))
    toolbox.extra_panels[mode].set_options(options)
    toolbox.accept_sources(sources)
    assert not toolbox.output_row_widget.isVisible()
    with patch("qingjian.app.ToolWorker", side_effect=AssertionError("export worker must not run")):
        toolbox.start_task()
        dialog = toolbox.interactive_dialogs[mode]
        assert toolbox._busy and toolbox.worker is None
        assert toolbox._interactive_owner is dialog
        until(lambda: not toolbox._busy)
    assert dialog.isVisible() and not dialog.is_busy()
    assert not output.exists() and toolbox.profile_store.history == []
    assert toolbox.last_result is None and toolbox.active_context is None
    assert originals == {p: p.read_bytes() for p in originals}
    assert "怎么操作" in guide_html(mode)
    if mode == "content_search":
        assert dialog.file_table.rowCount() == 2
        assert "项目甲" in dialog.preview.toPlainText()
        assert dialog.file_table.item(0, 1).text() in {str(p.resolve()) for p in originals}
    else:
        assert dialog.tree.topLevelItemCount() == 1
        assert dialog.selected_paths() == []
    dialog.hide()
    toolbox.show_results()
    assert dialog.isVisible()
    assert dialog.parent() is None
    assert toolbox.close()
    QApplication.processEvents()
    assert dialog.isVisible() and not toolbox.isVisible()
    toolbox.show()
    toolbox.switch_mode(MODES.index("image"))
    assert toolbox.output_row_widget.isVisible()


def test_search_holds_shared_slot_until_finished_and_main_can_cancel(toolbox, tmp_path, monkeypatch):
    source = tmp_path / "材料.txt"
    source.write_text("项目甲", encoding="utf-8")
    started, release = threading.Event(), threading.Event()

    def slow_search(sources, options, progress=None, cancel=None, file_callback=None):
        started.set()
        release.wait(5)
        return SearchReport(cancelled=bool(cancel and cancel()))

    monkeypatch.setattr("qingjian.search_ui.search_documents", slow_search)
    toolbox.switch_mode(MODES.index("content_search"))
    toolbox.extra_panels["content_search"].set_options({"query": "项目甲"})
    toolbox.accept_sources([str(source)])
    toolbox.show_converter()
    try:
        toolbox.start_task()
        until(started.is_set)
        owner = toolbox._interactive_owner
        assert owner is not None and toolbox._busy
        assert toolbox.converter_window.running and not toolbox._quick_active
        assert not toolbox.start_quick_action("image_to_jpg", [str(source)])
        assert not toolbox.open_interactive_tool("find_duplicates", [source], {})
        with patch.object(QMessageBox, "information") as message:
            assert not toolbox.close()
        message.assert_called_once()
        assert toolbox.isVisible()
        toolbox.stop_task()
        assert owner.worker.isInterruptionRequested()
        assert toolbox._busy and not toolbox.stop_button.isEnabled()
    finally:
        release.set()
    until(lambda: not toolbox._busy)
    assert toolbox._interactive_owner is None and owner.report.cancelled
    assert not toolbox.converter_window.running


def test_cached_results_cannot_rescan_or_move_during_another_task(toolbox, tmp_path):
    sources, options = build_example("find_duplicates", tmp_path / "practice")
    toolbox.open_interactive_tool("find_duplicates", sources, options)
    until(lambda: not toolbox._busy)
    dialog = toolbox.interactive_dialogs["find_duplicates"]
    dialog.select_redundant()
    toolbox.set_busy(True)
    try:
        with patch("qingjian.duplicates_ui.QMessageBox.information"):
            assert not dialog.start_scan()
            assert not dialog.start_batch("move", tmp_path)
        assert not dialog.is_busy() and len(dialog.selected_paths()) == 2
    finally:
        toolbox.set_busy(False)


def test_appearance_and_snap_register_main_converter_and_both_notebook_windows(toolbox, tmp_path):
    controller = NotebookController(NotebookStore(tmp_path / "notebook.json"), enable_tray=False, test_mode=True)
    controller.window_created.connect(toolbox.register_notebook_windows)
    try:
        toolbox.appearance.update_settings(opacity=.72, snap_enabled=False)
        toolbox.show_converter()
        assert controller.show_window()
        QApplication.processEvents()
        windows = [toolbox, toolbox.converter_window, *controller.window.owned_windows()]
        for window in windows:
            assert window in toolbox.window_snap._windows
            assert window.windowOpacity() == pytest.approx(.72, abs=.005)
        assert not toolbox.window_snap.is_enabled()
        toolbox.show_appearance()
        toolbox.appearance_dialog.opacity_slider.setValue(83)
        toolbox.appearance_dialog.snap_check.setChecked(True)
        for window in [*windows, toolbox.appearance_dialog]:
            assert window.windowOpacity() == pytest.approx(.83, abs=.005)
        assert toolbox.window_snap.is_enabled()
        toolbox.appearance_dialog.accept()
        assert toolbox.appearance.path.is_file()
    finally:
        controller.shutdown()
        controller.deleteLater()


def test_main_forwards_native_title_bar_moves_to_snap_manager(toolbox):
    with patch.object(toolbox.window_snap, "native_event", return_value=(True, 1)) as native:
        assert toolbox.nativeEvent(b"windows_generic_MSG", 123) == (True, 1)
    native.assert_called_once_with(toolbox, b"windows_generic_MSG", 123)
