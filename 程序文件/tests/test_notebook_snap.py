"""Real notebook headers must coexist with the app-wide snapping filter."""
from datetime import datetime
from types import SimpleNamespace
import sys

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian.app import Window
from qingjian.notebook_store import NotebookStore
from qingjian.notebook_ui import NotebookWindow, _TitleBar
from qingjian.window_appearance import AppearanceController
from qingjian.window_snap import WindowSnapManager


def pump():
    QApplication.processEvents()
    QTest.qWait(5)


@pytest.fixture
def notebook_pair(tmp_path):
    store = NotebookStore(tmp_path / "notebook.json")
    book = NotebookWindow(store, clock=lambda: datetime(2026, 11, 3, 10))
    manager = WindowSnapManager()
    appearance = AppearanceController()
    Window.register_notebook_windows(SimpleNamespace(window_snap=manager, appearance=appearance), book)
    book.show_windows()
    pump()
    yield book, manager, appearance
    manager.shutdown()
    appearance.shutdown()
    book.close_windows()
    book.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.mark.parametrize("component", ["calendar", "tasks"])
def test_notebook_titlebar_keeps_qt_window_method_and_can_start_drag(notebook_pair, component):
    book, manager, _ = notebook_pair
    top = book if component == "calendar" else book.tasks_window
    header = top.findChildren(_TitleBar)[0]
    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(20, 12),
                        QPointF(header.mapToGlobal(QPoint(20, 12))),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    assert manager.eventFilter(header, event) is False
    assert manager._pressed() is top
    assert header.window() is top


@pytest.mark.parametrize("snap_enabled", [True, False])
def test_actual_header_click_drag_and_layout_toggle_have_no_callback_errors(notebook_pair, monkeypatch, snap_enabled):
    book, manager, appearance = notebook_pair
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb: errors.append((kind.__name__, str(value))))
    manager.set_enabled(snap_enabled)
    appearance.update_settings(opacity=.78)
    book.move(60, 60)
    book.tasks_window.move(430, 190)
    pump()
    for top, other in [(book, book.tasks_window), (book.tasks_window, book)]:
        header = top.findChildren(_TitleBar)[0]
        before, peer_before = QPoint(top.pos()), QPoint(other.pos())
        QTest.mousePress(header, Qt.MouseButton.LeftButton, pos=QPoint(20, 12))
        QTest.mouseMove(header, QPoint(60, 47), delay=10)
        QTest.mouseRelease(header, Qt.MouseButton.LeftButton, pos=QPoint(20, 12))
        pump()
        assert top.pos() != before
        assert other.pos() == peer_before
        assert header._drag_position is None
        assert top.windowOpacity() == pytest.approx(.78, abs=.005)
    QTest.mouseClick(book.mode_button, Qt.MouseButton.LeftButton)
    pump()
    assert book.layout_mode == "combined" and not book.tasks_window.isVisible()
    QTest.mouseClick(book.mode_button, Qt.MouseButton.LeftButton)
    pump()
    assert book.layout_mode == "split" and book.tasks_window.isVisible()
    assert errors == []


def test_combined_layout_also_registers_hidden_tasks_for_later_split(notebook_pair):
    book, manager, appearance = notebook_pair
    book.set_layout_mode("combined")
    manager.unregister(book.tasks_window)
    assert not book.tasks_window.isVisible()
    Window.register_notebook_windows(SimpleNamespace(window_snap=manager, appearance=appearance), book)
    assert book.tasks_window in manager._windows
    book.set_layout_mode("split")
    pump()
    assert book.tasks_window.isVisible()
