"""Magnetic geometry and integration with existing QWidget.move title bars."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget
import pytest

from qingjian.window_snap import WindowSnapManager, _MSG, _WindowsGeometry, snap_rect


def test_screen_edges_preserve_size_and_inputs():
    available = QRect(0, 0, 1920, 1040)
    for initial, expected in [
        (QRect(9, 300, 320, 200), QRect(0, 300, 320, 200)),
        (QRect(1588, 300, 320, 200), QRect(1600, 300, 320, 200)),
        (QRect(350, -11, 320, 200), QRect(350, 0, 320, 200)),
        (QRect(350, 828, 320, 200), QRect(350, 840, 320, 200)),
    ]:
        snapshot = QRect(initial)
        assert snap_rect(initial, [available]) == expected
        assert initial == snapshot
        assert snap_rect(initial, [available]).size() == initial.size()
    assert available == QRect(0, 0, 1920, 1040)


def test_threshold_is_bounded_and_does_not_clamp_offscreen_windows():
    screen = QRect(0, 0, 800, 600)
    assert snap_rect(QRect(14, 100, 120, 100), [screen]).x() == 0
    assert snap_rect(QRect(15, 100, 120, 100), [screen]).x() == 15
    assert snap_rect(QRect(-160, 100, 120, 100), [screen]).x() == -160
    assert snap_rect(QRect(7, 100, 120, 100), [screen], threshold=-1).x() == 7


def test_negative_and_disjoint_monitor_coordinates():
    left = QRect(-2560, -200, 1707, 900)  # Windows mixed-DPI "island" origins
    primary = QRect(0, 0, 1920, 1040)
    assert snap_rect(QRect(-2551, 100, 300, 200), [left, primary]).x() == -2560
    assert snap_rect(QRect(-1161, 490, 300, 200), [left, primary]) == QRect(-1153, 500, 300, 200)
    # An x coordinate matching a remote screen does not attract vertically.
    remote = QRect(0, -1800, 1280, 720)
    assert snap_rect(QRect(9, 300, 300, 200), [remote]).x() == 9


def test_peer_gap_and_parallel_edge_alignment_on_all_four_sides():
    peer = QRect(400, 300, 300, 200)
    pairs = [
        (QRect(710, 306, 120, 100), QRect(706, 300, 120, 100)),
        (QRect(268, 306, 120, 100), QRect(274, 300, 120, 100)),
        (QRect(405, 511, 120, 100), QRect(400, 506, 120, 100)),
        (QRect(405, 183, 120, 100), QRect(400, 194, 120, 100)),
    ]
    for initial, expected in pairs:
        assert snap_rect(initial, peers=[peer]) == expected


def test_no_peer_attraction_from_far_away_or_overlapping_alignment():
    peer = QRect(400, 300, 300, 200)
    far = QRect(710, -500, 120, 100)
    overlapping = QRect(405, 340, 120, 100)
    assert snap_rect(far, peers=[peer]) == far
    assert snap_rect(overlapping, peers=[peer]) == overlapping


def test_closest_candidate_wins_and_correction_is_idempotent():
    initial = QRect(711, 306, 120, 100)
    peers = [QRect(400, 300, 300, 200), QRect(720, -200, 200, 200)]
    snapped = snap_rect(initial, peers=peers)
    assert snapped == QRect(706, 300, 120, 100)
    assert snap_rect(snapped, peers=peers) == snapped
    # A fresh pointer-derived rectangle can leave the threshold immediately.
    assert snap_rect(QRect(735, 300, 120, 100), peers=peers).x() == 735


@pytest.fixture
def windows(monkeypatch):
    manager = WindowSnapManager()
    first, second = QWidget(), QWidget()
    for window in (first, second):
        window.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        window.resize(120, 100)
        manager.register(window)
        window.show()
    QApplication.processEvents()
    first.move(150, 120)
    second.move(400, 300)
    QApplication.processEvents()
    monkeypatch.setattr(manager, "_alt_pressed", lambda: False)
    monkeypatch.setattr(manager, "_left_pressed", lambda: True)
    yield manager, first, second
    manager.shutdown()
    first.close()
    second.close()
    first.deleteLater()
    second.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def press(window):
    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(30, 20),
                        QPointF(window.mapToGlobal(QPoint(30, 20))),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window, event)


def release(window):
    event = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(30, 20),
                        QPointF(window.mapToGlobal(QPoint(30, 20))),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window, event)


def test_registration_and_programmatic_movement_never_relayout(windows):
    manager, moving, peer = windows
    moving.move(peer.x() + peer.width() + 10, peer.y())
    assert moving.x() == peer.x() + peer.width() + 10
    size = moving.size()
    manager.register(moving)  # duplicate registration is harmless
    assert moving.size() == size
    assert moving.x() == peer.x() + peer.width() + 10


def test_child_data_named_window_does_not_shadow_top_level_lookup(windows):
    manager, moving, _ = windows
    child = QWidget(moving)
    child.window = moving  # A legacy/custom widget may shadow QWidget.window.
    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5, 5),
                        QPointF(child.mapToGlobal(QPoint(5, 5))),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    assert manager.eventFilter(child, event) is False
    assert manager._pressed() is moving


def test_custom_move_drag_snaps_then_releases_without_binding(windows):
    manager, moving, peer = windows
    press(moving)
    moving.move(530, 305)
    assert moving.pos() == QPoint(526, 300)
    moving.move(550, 300)
    assert moving.pos() == QPoint(550, 300)
    release(moving)
    moving.move(530, 305)
    assert moving.pos() == QPoint(530, 305)
    assert peer.pos() == QPoint(400, 300)


def test_alt_disable_and_unregister_bypass_snapping(windows, monkeypatch):
    manager, moving, peer = windows
    press(moving)
    monkeypatch.setattr(manager, "_alt_pressed", lambda: True)
    moving.move(530, 305)
    assert moving.pos() == QPoint(530, 305)
    monkeypatch.setattr(manager, "_alt_pressed", lambda: False)
    manager.set_enabled(False)
    assert not manager.is_enabled()
    moving.move(531, 305)
    assert moving.pos() == QPoint(531, 305)
    manager.set_enabled(True)
    press(moving)
    manager.unregister(peer)
    moving.move(532, 305)
    assert moving.pos() == QPoint(532, 305)


def test_hidden_minimized_and_maximized_peers_do_not_attract(windows):
    manager, moving, peer = windows
    peer.hide()
    press(moving)
    moving.move(530, 305)
    assert moving.pos() == QPoint(530, 305)
    peer.showMinimized()
    moving.move(531, 305)
    assert moving.pos() == QPoint(531, 305)
    peer.showMaximized()
    assert not manager._eligible(peer)


def test_resize_and_lost_press_never_snap(windows, monkeypatch):
    manager, moving, peer = windows
    press(moving)
    moving.resize(130, 100)
    moving.move(530, 305)
    assert moving.pos() == QPoint(530, 305)
    release(moving)
    press(moving)
    monkeypatch.setattr(manager, "_left_pressed", lambda: False)
    moving.move(531, 305)
    assert moving.pos() == QPoint(531, 305)


class NativeGeometry:
    def rect(self, handle):
        return QRect(-1500, -800, 300, 200)

    def work_area(self, rect):
        return QRect(-1920, -1080, 1920, 1040)

    def distances(self, handle, threshold, gap, device_ratio=None):
        return round(threshold * 1.5), round(gap * 1.5)


def native_message(manager, hwnd, kind, rect=None):
    message = _MSG()
    message.hwnd = hwnd
    message.message = kind
    message.lParam = ctypes.addressof(rect) if rect is not None else 0
    return manager._native_message(ctypes.addressof(message))


def test_native_rect_uses_physical_coordinates_without_scaling_desktop_origins(windows):
    manager, moving, peer = windows
    manager._native = NativeGeometry()
    manager._handles[101] = moving
    manager._handles[102] = peer
    # Peer right -1200 + nine physical pixels gap, offset by five pixels.
    rect = wintypes.RECT(-1186, -794, -1006, -644)
    assert native_message(manager, 101, 0x0216, rect) == (True, 1)
    assert (rect.left, rect.top, rect.right, rect.bottom) == (-1191, -800, -1011, -650)


def test_native_resize_unrelated_handles_alt_and_maximize_are_untouched(windows, monkeypatch):
    manager, moving, peer = windows
    manager._native = NativeGeometry()
    manager._handles[101] = moving
    rect = wintypes.RECT(-1910, -800, -1730, -650)
    original = bytes(rect)
    assert native_message(manager, 101, 0x0214, rect) == (False, 0)  # WM_SIZING
    assert native_message(manager, 999, 0x0216, rect) == (False, 0)
    monkeypatch.setattr(manager, "_alt_pressed", lambda: True)
    assert native_message(manager, 101, 0x0216, rect) == (False, 0)
    assert bytes(rect) == original
    monkeypatch.setattr(manager, "_alt_pressed", lambda: False)
    manager.set_enabled(False)
    assert native_message(manager, 101, 0x0216, rect) == (False, 0)
    manager.set_enabled(True)
    moving.showMaximized()
    assert native_message(manager, 101, 0x0216, rect) == (False, 0)
    moving.showNormal()
    moving.hide()
    assert native_message(manager, 101, 0x0216, rect) == (False, 0)
    assert bytes(rect) == original
    native_message(manager, 101, 0x0231)
    assert 101 in manager._native_loop
    native_message(manager, 101, 0x0232)
    assert not manager._native_loop


@pytest.mark.parametrize("native_dpi", [None, 0, 144])
def test_native_distances_use_qt_dpr_and_support_missing_win10_dpi_api(monkeypatch, native_dpi):
    class Function:
        def __init__(self, result=1):
            self.result = result

        def __call__(self, *args):
            return self.result

    class User32:
        GetWindowRect = Function()
        MonitorFromRect = Function()
        GetMonitorInfoW = Function()
        GetAsyncKeyState = Function(0)

    api = User32()
    if native_dpi is not None:
        api.GetDpiForWindow = Function(native_dpi)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    geometry = _WindowsGeometry()
    # QT_SCALE_FACTOR and mixed-DPI values are already part of Qt's DPR.
    assert geometry.distances(101, 14, 6, 2.0) == (28, 12)
    expected_scale = (native_dpi or 96) / 96
    assert geometry.distances(101, 14, 6) == (round(14 * expected_scale), round(6 * expected_scale))
    assert not geometry.alt_pressed()


@pytest.mark.skipif(os.name != "nt" or os.environ.get("QT_QPA_PLATFORM") != "windows",
                    reason="requires real Windows QPA")
def test_native_filter_receives_actual_windows_messages():
    # Run separately with QT_QPA_PLATFORM=windows. SendMessageW traverses Qt's
    # real native window virtual rather than calling manager._native_message.
    manager = WindowSnapManager()

    class NativeWindow(QWidget):
        def nativeEvent(self, event_type, message):
            handled, result = manager.native_event(self, event_type, message)
            return (handled, result) if handled else super().nativeEvent(event_type, message)

    window = NativeWindow()
    window.resize(240, 160)
    window.show()
    manager.register(window)
    QApplication.processEvents()
    try:
        handle = int(window.winId())
        area = manager._native.work_area(manager._native.rect(handle))
        rect = wintypes.RECT(area.left() + 8, area.top() + 100,
                             area.left() + 308, area.top() + 300)
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        api.SendMessageW.restype = ctypes.c_ssize_t
        result = api.SendMessageW(handle, 0x0216, 0, ctypes.addressof(rect))
        assert result == 1
        assert rect.left == area.left()
        assert rect.right - rect.left == 300
        assert rect.bottom - rect.top == 200
    finally:
        manager.shutdown()
        window.close()
        window.deleteLater()
