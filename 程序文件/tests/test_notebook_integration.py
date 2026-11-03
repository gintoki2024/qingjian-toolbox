"""Bounded integration of the real toolbox, notebook UI, store and controller.

No application event loop, native notifications, tray, network or source
bootstrap is started here. The session fixture owns Qt and each case cleans up
its own independent windows and reminder timer.
"""
from datetime import datetime

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from shiboken6 import isValid

from qingjian.app import Window, notebook_snapshot
from qingjian.notebook_runtime import NotebookController
from qingjian.notebook_store import NotebookStore


@pytest.fixture
def integrated_notebook(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("test_mode attempted to use the native tray or a real notification")

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", forbidden)
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", forbidden)
    monkeypatch.setattr(QApplication, "beep", forbidden)
    now = [datetime(2026, 10, 29, 10)]
    toolbox = Window(start_home=True)
    store = NotebookStore()
    controller = NotebookController(store, parent=toolbox, clock=lambda: now[0],
                                    test_mode=True, enable_tray=True,
                                    open_toolbox=toolbox.showNormal)
    toolbox.notebook_requested.connect(controller.show_window)
    toolbox.show()
    QApplication.processEvents()
    toolbox.resize(1280, 860)
    toolbox.activateWindow()
    QApplication.processEvents()
    yield toolbox, controller, store, now
    controller.shutdown()
    if isValid(toolbox):
        toolbox.set_busy(False)
        toolbox.close()
        toolbox.deleteLater()
    controller.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def test_sidebar_opens_independent_notebook_without_switching_work(integrated_notebook):
    toolbox, controller, _, _ = integrated_notebook
    toolbox.open_scene("official")
    mode, page = toolbox.mode, toolbox.content_stack.currentWidget()
    spy = QSignalSpy(toolbox.notebook_requested)
    assert toolbox.notebook_button.text() == "记事与提醒"
    assert toolbox.notebook_button.isVisible()
    assert toolbox.notebook_button.objectName() == "notebookLaunch"
    assert toolbox.notebook_button.height() >= 44
    assert not toolbox.notebook_button.icon().isNull()
    assert toolbox.notebook_button.geometry().bottom() < toolbox.surface_buttons["home"].geometry().top()
    QTest.mouseClick(toolbox.notebook_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert spy.count() == 1
    assert controller.active and controller.timer.isActive()
    assert controller.window.isVisible() and controller.window.parent() is None
    assert controller.window.isWindow()
    assert controller.parent() is None
    assert toolbox.mode == mode and toolbox.content_stack.currentWidget() is page


def test_control_shift_n_keyboard_binding_opens_notebook(integrated_notebook):
    toolbox, controller, _, _ = integrated_notebook
    assert toolbox.notebook_shortcut.key().toString() == "Ctrl+Shift+N"
    spy = QSignalSpy(toolbox.notebook_requested)
    toolbox.raise_()
    toolbox.activateWindow()
    toolbox.setFocus()
    QApplication.processEvents()
    QTest.keyClick(toolbox, Qt.Key.Key_N,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    QApplication.processEvents()
    assert spy.count() == 1
    assert controller.window is not None and controller.window.isVisible()


def test_notebook_survives_toolbox_close_and_destruction(integrated_notebook):
    toolbox, controller, store, now = integrated_notebook
    assert controller.show_window()
    independent = controller.window
    toolbox.close()
    toolbox.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    assert not isValid(toolbox)
    assert isValid(controller) and isValid(independent)
    assert independent.isVisible() and controller.timer.isActive()
    task = store.upsert_task({"title": "主程序关闭后的提醒", "remind_at": "2026-10-29T10:00:00"}, now=now[0])
    controller.tick()
    assert controller.card is not None and controller.card.alert["task_id"] == task["id"]
    assert controller.complete_alert(controller.card.alert["id"])
    assert store.find_task(task["id"])["completed"] is True


def test_real_reminder_works_without_native_tray_or_notifications(integrated_notebook):
    toolbox, controller, store, now = integrated_notebook
    task = store.upsert_task({"title": "周期事项", "priority": "urgent",
                             "remind_at": "2026-10-29T10:00:00", "repeat": {"kind": "daily"}}, now=now[0])
    QTest.mouseClick(toolbox.notebook_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert controller.tray is None and controller.card is not None
    card = controller.card
    assert card.title.text() == "周期事项" and card.complete.text() == "完成本次"
    for _ in range(3):
        controller.tick()
    assert controller.card is card
    QTest.mouseClick(card.complete, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert controller.card is None
    assert not store.find_task(task["id"])["completed"]
    assert store.find_task(task["id"])["next_due"] == "2026-10-30T10:00:00"


def test_task_completion_in_real_notebook_cancels_its_visible_reminder(integrated_notebook):
    _, controller, store, now = integrated_notebook
    task = store.upsert_task({"title": "整项完成取消周期", "remind_at": "2026-10-29T10:00:00",
                             "repeat": {"kind": "daily"}}, now=now[0])
    assert controller.show_window()
    assert controller.card is not None
    controller.window.focus_task(task["id"])
    task_list = controller.window.task_list
    item = next(task_list.item(row) for row in range(task_list.count())
                if task_list.item(row).data(Qt.ItemDataRole.UserRole) == task["id"])
    complete = controller.window.task_action_rect(item, "complete")
    QTest.mouseClick(task_list.viewport(), Qt.MouseButton.LeftButton, pos=complete.center())
    QApplication.processEvents()
    assert store.find_task(task["id"])["completed"]
    assert store.pending_alerts == [] and controller.card is None


def test_reminder_can_open_while_toolbox_is_busy_and_closing_small_window_keeps_timer(integrated_notebook):
    toolbox, controller, _, _ = integrated_notebook
    toolbox.open_scene("official")
    toolbox.set_busy(True)
    assert toolbox.notebook_button.isEnabled()
    QTest.mouseClick(toolbox.notebook_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert controller.active and controller.window.isVisible()
    controller.window.close()
    QApplication.processEvents()
    assert controller.active and controller.timer.isActive()
    assert controller.window.isVisible() and controller.window.isMinimized()
    assert toolbox._busy


def test_launcher_shows_both_widgets_and_snapshot_includes_the_pair(integrated_notebook):
    toolbox, controller, _, _ = integrated_notebook
    QTest.mouseClick(toolbox.notebook_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    peers = controller.window.owned_windows()
    assert len(peers) == 2 and all(peer.isVisible() for peer in peers)
    assert all(peer.isWindow() for peer in peers)
    shot = notebook_snapshot(controller.window)
    logical_width = shot.width() / shot.devicePixelRatio()
    assert logical_width > max(peer.width() for peer in peers)
    controller.hide_window()
    QApplication.processEvents()
    assert controller.timer.isActive()
    assert all(peer.isMinimized() for peer in peers)
