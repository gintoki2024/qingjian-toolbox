"""Interactive source selection, safe highlighting and cancellable lifetime."""
from __future__ import annotations

import gc
import threading
import time
import weakref

import pytest

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from qingjian.core.search_session import SearchReport, search_documents
from qingjian.search_ui import DocumentSearchDialog, _LIVE_WORKERS


def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(3)
    assert predicate(), "search window did not finish its expected transition"


def cleanup(dialog):
    if dialog.is_busy():
        dialog.cancel_task()
        until(lambda: not dialog.is_busy())
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def test_construction_does_not_start_and_invalid_input_is_clear(tmp_path):
    dialog = DocumentSearchDialog([tmp_path], {"query": "  "})
    signals = []
    dialog.busy_changed.connect(signals.append)
    try:
        assert not dialog.is_busy() and dialog.worker is None
        assert not dialog.start_search()
        assert "关键词" in dialog.status_label.text()
        assert signals == []
    finally:
        cleanup(dialog)


def test_execution_slot_guard_is_checked_on_every_start(tmp_path):
    path = tmp_path / "材料.txt"
    path.write_text("测试关键词", encoding="utf-8")
    dialog = DocumentSearchDialog([path], {"query": "关键词"})
    events = []
    dialog.busy_changed.connect(events.append)
    try:
        dialog.execution_available = lambda: False
        assert not dialog.start_search()
        assert "另有任务" in dialog.status_label.text()
        assert events == []
        dialog.execution_available = lambda: True
        assert dialog.start_search()
        until(lambda: not dialog.is_busy())
        assert events == [True, False]
        dialog.execution_available = lambda: False
        assert not dialog.start_search()
        assert dialog.file_table.rowCount() == 1
        assert events == [True, False]
    finally:
        cleanup(dialog)


def test_file_rows_switch_original_context_and_navigate_hits(tmp_path):
    first = tmp_path / "甲材料.txt"
    first.write_text("第一行 Alpha\n无关内容\n第三行 ALPHA\n", encoding="utf-8")
    second = tmp_path / "乙材料.txt"
    second.write_text("另一个 Alpha 原文", encoding="utf-8")
    before = {path: path.read_bytes() for path in (first, second)}
    dialog = DocumentSearchDialog([first, second], {"query": "alpha"})
    finished = []
    dialog.completed.connect(lambda report: finished.append((report, dialog.is_busy())))
    dialog.show()
    try:
        assert dialog.start_search()
        until(lambda: not dialog.is_busy())
        assert finished and finished[0][1] is False
        assert dialog.file_table.rowCount() == 2
        assert [dialog.file_table.horizontalHeaderItem(i).text() for i in range(2)] == ["文件名", "文件地址"]
        assert dialog.file_table.item(0, 0).text() == first.name
        assert dialog.file_table.item(0, 1).text() == str(first.resolve())
        assert dialog.preview.toPlainText() == "第一行 Alpha"
        assert "第1行" in dialog.hit_details.text()
        QTest.mouseClick(dialog.next_button, Qt.MouseButton.LeftButton)
        assert dialog.preview.toPlainText() == "第三行 ALPHA"
        assert "第3行" in dialog.hit_details.text()
        assert not dialog.next_button.isEnabled()
        rect = dialog.file_table.visualItemRect(dialog.file_table.item(1, 0))
        QTest.mouseClick(dialog.file_table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        assert dialog.preview.toPlainText() == "另一个 Alpha 原文"
        assert dialog.hit_selector.count() == 1
        assert dialog.preview.extraSelections()[0].cursor.selectedText() == "Alpha"
        assert before == {path: path.read_bytes() for path in (first, second)}
        assert len(list(tmp_path.iterdir())) == 2
        dialog.close()
        assert dialog.report is None and dialog.preview.toPlainText() == ""
        assert not dialog._files
    finally:
        cleanup(dialog)


def test_literal_html_unicode_and_file_url_are_plain_text(tmp_path):
    path = tmp_path / "文本.txt"
    body = '😀 Straße <img src="file:///C:/secret.png"> <script>unsafe()</script> https://example.com'
    path.write_text(body, encoding="utf-8")
    dialog = DocumentSearchDialog([path], {"query": "STRASSE\n<img"})
    try:
        assert dialog.start_search()
        until(lambda: not dialog.is_busy())
        assert dialog.preview.toPlainText() == body
        assert [selection.cursor.selectedText() for selection in dialog.preview.extraSelections()] == ["Straße", "<img"]
        assert dialog.preview.isReadOnly()
    finally:
        cleanup(dialog)


def test_restart_clears_old_results_and_does_not_accumulate_history(tmp_path):
    path = tmp_path / "更新.txt"
    path.write_text("旧关键词", encoding="utf-8")
    dialog = DocumentSearchDialog([path], {"query": "关键词"})
    try:
        dialog.start_search()
        until(lambda: not dialog.is_busy())
        assert dialog.preview.toPlainText() == "旧关键词"
        path.write_text("新关键词", encoding="utf-8")
        QTest.mouseClick(dialog.restart_button, Qt.MouseButton.LeftButton)
        until(lambda: not dialog.is_busy())
        assert dialog.file_table.rowCount() == 1
        assert dialog.preview.toPlainText() == "新关键词"
        assert dialog.report.hit_count == 1
    finally:
        cleanup(dialog)


def test_multiline_csv_cell_highlights_after_qt_newline_normalization(tmp_path):
    path = tmp_path / "多行.csv"
    path.write_bytes('"第一行\r\n😀 Alpha 命中",信息\r\n'.encode("utf-8"))
    dialog = DocumentSearchDialog([path], {"query": "Alpha"})
    try:
        dialog.start_search()
        until(lambda: not dialog.is_busy())
        assert "第一行\n😀 Alpha" in dialog.preview.toPlainText()
        assert dialog.preview.extraSelections()[0].cursor.selectedText() == "Alpha"
    finally:
        cleanup(dialog)


@pytest.mark.parametrize("close_action", ["close", "escape"])
def test_close_requests_cancel_and_keeps_window_alive_until_thread_finished(tmp_path, monkeypatch, close_action):
    path = tmp_path / "材料.txt"
    path.write_text("关键词", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()

    def delayed(_sources, _options, _progress, cancel, _on_file):
        entered.set()
        release.wait(5)
        return SearchReport(cancelled=cancel())

    monkeypatch.setattr("qingjian.search_ui.search_documents", delayed)
    dialog = DocumentSearchDialog([path], {"query": "关键词"})
    events = []
    dialog.busy_changed.connect(events.append)
    dialog.show()
    try:
        assert dialog.start_search()
        until(entered.is_set)
        if close_action == "escape":
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            assert not dialog.close()
        assert dialog.isVisible() and dialog.is_busy()
        assert events == [True]
        assert dialog.worker.isInterruptionRequested()
        assert not dialog.wait_for_finished(1)
        release.set()
        until(lambda: not dialog.is_busy() and not dialog.isVisible())
        assert events == [True, False]
        assert dialog.report is None
        monkeypatch.setattr("qingjian.search_ui.search_documents", search_documents)
        path.write_text("重开后的新词原文", encoding="utf-8")
        dialog.options = {"query": "新词"}
        dialog.show()
        assert dialog.start_search()
        until(lambda: not dialog.is_busy())
        assert dialog.isVisible()
        assert dialog.preview.toPlainText() == "重开后的新词原文"
        assert events == [True, False, True, False]
    finally:
        release.set()
        cleanup(dialog)


def test_escape_after_completion_clears_transient_text(tmp_path):
    path = tmp_path / "材料.txt"
    path.write_text("关键词原文", encoding="utf-8")
    dialog = DocumentSearchDialog([path], {"query": "关键词"})
    dialog.show()
    try:
        assert dialog.start_search()
        until(lambda: not dialog.is_busy())
        assert dialog.report is not None and dialog.preview.toPlainText()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert not dialog.isVisible()
        assert dialog.report is None and dialog.preview.toPlainText() == ""
        assert not dialog._files
    finally:
        cleanup(dialog)


def test_result_ready_does_not_release_execution_slot_before_thread_finished(tmp_path, monkeypatch):
    report = SearchReport()
    release = threading.Event()

    def staged_run(worker):
        worker.result_ready.emit(report)
        release.wait(5)

    monkeypatch.setattr("qingjian.search_ui._SearchWorker.run", staged_run)
    dialog = DocumentSearchDialog([tmp_path], {"query": "关键词"})
    events, completed = [], []
    dialog.busy_changed.connect(events.append)
    dialog.completed.connect(completed.append)
    try:
        assert dialog.start_search()
        until(lambda: dialog._pending_report is report)
        assert dialog.worker.isRunning() and dialog.is_busy()
        assert events == [True] and completed == []
        assert not dialog.start_search()
        release.set()
        until(lambda: not dialog.is_busy())
        assert events == [True, False] and completed == [report]
    finally:
        release.set()
        cleanup(dialog)


def test_parent_destruction_never_destroys_running_qthread(tmp_path, monkeypatch):
    entered, release, interrupted = threading.Event(), threading.Event(), threading.Event()

    def delayed(_sources, _options, _progress, cancel, _on_file):
        entered.set()
        release.wait(5)
        if cancel():
            interrupted.set()
        return SearchReport(cancelled=cancel())

    monkeypatch.setattr("qingjian.search_ui.search_documents", delayed)
    parent = QWidget()
    dialog = DocumentSearchDialog([tmp_path], {"query": "关键词"}, parent)
    starting_workers = len(_LIVE_WORKERS)
    try:
        assert dialog.start_search()
        until(entered.is_set)
        worker_ref = weakref.ref(dialog.worker)
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        del dialog, parent
        gc.collect()
        assert len(_LIVE_WORKERS) == starting_workers + 1
        assert worker_ref() is not None
        release.set()
        until(lambda: len(_LIVE_WORKERS) == starting_workers)
        assert interrupted.is_set()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        gc.collect()
        assert worker_ref() is None
    finally:
        release.set()
        until(lambda: len(_LIVE_WORKERS) == starting_workers)


def test_errors_and_empty_state_are_visible_without_result_files(tmp_path):
    bad = tmp_path / "损坏.docx"
    bad.write_bytes(b"invalid")
    dialog = DocumentSearchDialog([bad], {"query": "关键词"})
    dialog.show()
    try:
        dialog.start_search()
        until(lambda: not dialog.is_busy())
        assert dialog.file_table.rowCount() == 0
        assert "未找到命中" in dialog.list_empty.text()
        assert "读取失败 1" in dialog.status_label.text()
        assert dialog.details_toggle.isVisible()
        QTest.mouseClick(dialog.details_toggle, Qt.MouseButton.LeftButton)
        assert dialog.details.isVisible()
        assert str(bad.resolve()) in dialog.details.toPlainText()
        assert len(list(tmp_path.iterdir())) == 1
    finally:
        cleanup(dialog)
