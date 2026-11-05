"""Interactive duplicate lists, review gates and background-window lifetime."""
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from qingjian.core.duplicate_session import DuplicateScan
from qingjian.duplicates_ui import DuplicatesDialog, DuplicateReviewDialog, _ACTIVE_WORKERS


def _wait(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(5)
    assert predicate(), "后台任务未在预期时间结束"


@pytest.fixture
def dialog(tmp_path):
    scope = tmp_path / "材料"
    scope.mkdir()
    for name in ("文件甲.txt", "文件乙.txt", "文件丙.txt"):
        (scope / name).write_text("相同的文件内容", encoding="utf-8")
    view = DuplicatesDialog([scope], {"recursive": True})
    yield view
    if view.is_busy():
        view.cancel_task()
        _wait(lambda: not view.is_busy())
    view.close()
    view.deleteLater()
    QApplication.processEvents()


def test_scan_is_explicit_shows_paths_and_defaults_to_no_selection(dialog, tmp_path):
    assert not dialog.is_busy()
    assert dialog.tree.topLevelItemCount() == 0
    assert dialog.start_scan()
    assert not dialog.scan_button.isEnabled()
    _wait(lambda: not dialog.is_busy())
    assert dialog.tree.topLevelItemCount() == 1
    group = dialog.tree.topLevelItem(0)
    assert group.childCount() == 3
    assert dialog.selected_paths() == []
    assert not dialog.move_button.isEnabled()
    assert "3 个文件" in dialog.summary_label.text()
    child = group.child(0)
    assert child.text(0).endswith(".txt")
    assert str(tmp_path / "材料") in child.text(1)
    dialog.tree.setCurrentItem(child)
    assert "SHA256" in dialog.details.toPlainText()
    assert child.text(1) in dialog.details.toPlainText()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["材料"]


def test_reusing_window_shows_current_source_scope(dialog, tmp_path):
    current = tmp_path / "本次材料"
    current.mkdir()
    dialog.sources = [current]
    assert dialog.start_scan()
    _wait(lambda: not dialog.is_busy())
    assert str(current) in dialog.scope_label.text()
    assert dialog.scope_label.toolTip() == str(current)
    assert dialog.tree.topLevelItemCount() == 0


def test_suggested_selection_keeps_first_and_cancel_clears(dialog):
    dialog.start_scan()
    _wait(lambda: not dialog.is_busy())
    dialog.select_redundant()
    assert len(dialog.selected_paths()) == 2
    first = dialog.tree.topLevelItem(0).child(0)
    assert first.checkState(0) == Qt.CheckState.Unchecked
    assert dialog.move_button.isEnabled()
    dialog.clear_selection()
    assert dialog.selected_paths() == []


def test_declining_review_causes_no_file_operations(dialog, tmp_path):
    destination = tmp_path / "整理"
    destination.mkdir()
    dialog.start_scan()
    _wait(lambda: not dialog.is_busy())
    dialog.select_redundant()
    with patch.object(dialog, "_confirm_batch", return_value=False) as confirm:
        assert not dialog.start_batch("move", destination)
    assert len(confirm.call_args.args[0]) == 2
    assert not dialog.is_busy()
    assert len(list(dialog.sources[0].iterdir())) == 3
    assert not list(destination.iterdir())


def test_batch_uses_background_slot_and_updates_rows_without_report(dialog, tmp_path):
    destination = tmp_path / "整理"
    destination.mkdir()
    events = []
    dialog.busy_changed.connect(events.append)
    assert dialog.start_scan()
    _wait(lambda: not dialog.is_busy())
    dialog.select_redundant()
    with patch.object(dialog, "_confirm_batch", return_value=True):
        assert dialog.start_batch("move", destination)
    assert dialog.is_busy()
    _wait(lambda: not dialog.is_busy())
    assert events == [True, False, True, False]
    assert dialog.last_batch.success_count == 2
    assert dialog.selected_paths() == []
    assert len(list(destination.iterdir())) == 2
    assert all(path.suffix == ".txt" for path in destination.iterdir())
    statuses = [dialog.tree.topLevelItem(0).child(i).text(4) for i in range(3)]
    assert statuses.count("已移到整理目录") == 2
    assert "成功 2 个" in dialog.status_label.text()
    assert dialog.start_scan()
    _wait(lambda: not dialog.is_busy())
    assert dialog.tree.topLevelItemCount() == 0
    assert dialog.last_batch is None


def test_whole_group_selection_is_rejected_before_review(dialog, tmp_path):
    destination = tmp_path / "整理"
    destination.mkdir()
    dialog.start_scan()
    _wait(lambda: not dialog.is_busy())
    parent = dialog.tree.topLevelItem(0)
    for index in range(parent.childCount()):
        parent.child(index).setCheckState(0, Qt.CheckState.Checked)
    with patch("qingjian.duplicates_ui.QMessageBox.warning") as warning, patch.object(dialog, "_confirm_batch") as confirm:
        assert not dialog.start_batch("move", destination)
    assert "至少保留" in warning.call_args.args[2]
    confirm.assert_not_called()
    assert not dialog.is_busy()


def test_main_execution_slot_is_rechecked_for_scan_batch_and_after_review(dialog, tmp_path):
    destination = tmp_path / "整理"
    destination.mkdir()
    dialog.execution_available = lambda: False
    with patch("qingjian.duplicates_ui.QMessageBox.information") as message:
        assert not dialog.start_scan()
        assert not dialog.start_batch("move", destination)
    assert message.call_count == 2
    assert not dialog.is_busy()
    dialog.execution_available = lambda: True
    dialog.start_scan()
    _wait(lambda: not dialog.is_busy())
    dialog.select_redundant()

    def confirmed(*_args):
        dialog.execution_available = lambda: False
        return True

    with patch.object(dialog, "_confirm_batch", side_effect=confirmed), patch("qingjian.duplicates_ui.QMessageBox.information"):
        assert not dialog.start_batch("move", destination)
    assert len(list(dialog.sources[0].iterdir())) == 3


def test_close_requests_cancel_and_waits_asynchronously(dialog):
    entered = threading.Event()

    def slow_scan(sources, options, progress, cancel):
        entered.set()
        while not cancel():
            time.sleep(0.005)
        # Keep the worker alive briefly so close() must defer destruction.
        time.sleep(0.04)
        return DuplicateScan(cancelled=True)

    dialog.show()
    with patch("qingjian.duplicates_ui.scan_duplicates", side_effect=slow_scan):
        dialog.start_scan()
        _wait(entered.is_set)
        assert not dialog.close()
        assert dialog.is_busy()
        assert dialog.isVisible()
        _wait(lambda: not dialog.is_busy())
    assert not dialog.isVisible()
    assert not _ACTIVE_WORKERS


def test_unsupported_recycle_bin_is_disabled_without_delete_fallback(dialog):
    dialog.start_scan()
    _wait(lambda: not dialog.is_busy())
    dialog.select_redundant()
    with patch("qingjian.duplicates_ui._trash_supported", return_value=False), patch("qingjian.duplicates_ui.QMessageBox.warning") as warning:
        dialog._refresh_controls()
        assert not dialog.trash_button.isEnabled()
        assert not dialog.start_batch("trash")
    assert "不支持" in warning.call_args.args[2]
    assert len(list(dialog.sources[0].iterdir())) == 3


def test_review_lists_exact_affected_and_kept_paths_and_is_read_only():
    rows = [{"source": "D:/材料/副本.txt", "keeper": "D:/材料/保留.txt", "size": 20}]
    view = DuplicateReviewDialog(rows, "move", Path("D:/整理"))
    assert view.table.item(0, 0).text() == rows[0]["source"]
    assert view.table.item(0, 1).text() == rows[0]["keeper"]
    assert not view.table.editTriggers()
    view.close()
    view.deleteLater()


def test_unexpected_dialog_destruction_does_not_destroy_running_thread(tmp_path):
    entered = threading.Event()

    def slow_scan(sources, options, progress, cancel):
        entered.set()
        while not cancel():
            time.sleep(0.005)
        return DuplicateScan(cancelled=True)

    view = DuplicatesDialog([tmp_path], {})
    with patch("qingjian.duplicates_ui.scan_duplicates", side_effect=slow_scan):
        view.start_scan()
        _wait(entered.is_set)
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _wait(lambda: not _ACTIVE_WORKERS)
