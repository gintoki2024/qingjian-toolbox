"""Magnetic movement for the main window and the independent toolbox widgets.

Keep one ``WindowSnapManager`` on QApplication, then ``register(window)`` for
each top-level widget, including NotebookWindow.tasks_window. Registration does
not move or resize anything. Existing QWidget.move-based title bars need no
changes. A native-title-bar window must additionally forward its nativeEvent to
manager.native_event(self, event_type, message), returning the result if handled,
otherwise delegating to super().nativeEvent. Synchronous Windows WM_MOVING is
not guaranteed to pass through QAbstractNativeEventFilter. There is no polling
timer, mouse hook or system-setting change. Hold Alt to move freely. Snapping
never creates a persistent group of windows.

Qt movement is entirely in logical pixels. The Windows bridge keeps WM_MOVING,
GetWindowRect and monitor work areas entirely in native coordinates, scaling
only the 14/6 logical-pixel distances by the window's DPR (native DPI fallback).
Never multiply virtual desktop origins by a screen's DPR: that fails on
mixed-DPI/negative desktops.

API references:
https://doc.qt.io/qt-6/qabstractnativeeventfilter.html
https://doc.qt.io/qt-6/highdpi.html
https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-moving
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowrect
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getdpiforwindow
"""
from __future__ import annotations

import ctypes
import os
import weakref

if os.name == "nt":
    from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, QRect, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QWidget


def _interval_distance(first: int, length: int, other: int, other_length: int) -> int:
    """Distance between half-open intervals; zero when they overlap or touch."""
    return max(0, first - other - other_length, other - first - length)


def snap_rect(rect: QRect, screens=(), peers=(), *, threshold=14, gap=6) -> QRect:
    """Return a translated outer-frame rectangle; all inputs use the same units.

    Screen edges have zero gap. Peer edges have ``gap`` space between them;
    nearby parallel edges can align when the windows sit beside one another.
    There is no screen clamping, resizing or attachment. Negative desktop
    coordinates are ordinary coordinates, and input rectangles are not mutated.
    """
    result = QRect(rect)
    if not result.isValid() or threshold < 0:
        return result
    threshold, gap = int(threshold), max(0, int(gap))
    x, y, width, height = rect.x(), rect.y(), rect.width(), rect.height()
    dxs, dys = [], []

    def add(bucket, delta):
        if abs(delta) <= threshold:
            bucket.append(delta)

    for screen in screens:
        if not screen.isValid():
            continue
        # A remote display must not attract a window across an empty virtual
        # desktop gap just because one of its x/y coordinates happens to match.
        if _interval_distance(y, height, screen.y(), screen.height()) <= threshold:
            add(dxs, screen.x() - x)
            add(dxs, screen.x() + screen.width() - x - width)
        if _interval_distance(x, width, screen.x(), screen.width()) <= threshold:
            add(dys, screen.y() - y)
            add(dys, screen.y() + screen.height() - y - height)

    for peer in peers:
        if not peer.isValid():
            continue
        px, py, pw, ph = peer.x(), peer.y(), peer.width(), peer.height()
        horizontal_distance = _interval_distance(x, width, px, pw)
        vertical_distance = _interval_distance(y, height, py, ph)
        if vertical_distance <= gap + threshold:
            add(dxs, px - gap - width - x)
            add(dxs, px + pw + gap - x)
        if horizontal_distance <= gap + threshold:
            add(dys, py - gap - height - y)
            add(dys, py + ph + gap - y)
        # Same-edge alignment is useful beside a peer, but should not pull two
        # overlapping windows directly on top of one another.
        if min(abs(y + height + gap - py), abs(py + ph + gap - y)) <= threshold:
            add(dxs, px - x)
            add(dxs, px + pw - x - width)
        if min(abs(x + width + gap - px), abs(px + pw + gap - x)) <= threshold:
            add(dys, py - y)
            add(dys, py + ph - y - height)

    result.translate(min(dxs, key=abs) if dxs else 0,
                     min(dys, key=abs) if dys else 0)
    return result


if os.name == "nt":
    class _MSG(ctypes.Structure):
        # Win32 MSG, with pointer-sized WPARAM/LPARAM on 64-bit Python.
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                    ("lPrivate", wintypes.DWORD)]


    class _MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _qrect(rect):
    return QRect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


class _WindowsGeometry:
    """A small read-only Win32 geometry adapter; only WM_MOVING's RECT is edited."""

    def __init__(self):
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.api.GetWindowRect.restype = wintypes.BOOL
        self.api.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
        self.api.MonitorFromRect.restype = wintypes.HANDLE
        self.api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
        self.api.GetMonitorInfoW.restype = wintypes.BOOL
        # GetDpiForWindow arrived in Windows 10 1607. Qt already supplies the
        # window's DPR, so its absence must not disable the native move bridge.
        self._get_dpi = getattr(self.api, "GetDpiForWindow", None)
        if self._get_dpi is not None:
            self._get_dpi.argtypes = [wintypes.HWND]
            self._get_dpi.restype = wintypes.UINT
        self.api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.api.GetAsyncKeyState.restype = ctypes.c_short

    def rect(self, hwnd):
        rect = wintypes.RECT()
        return _qrect(rect) if self.api.GetWindowRect(hwnd, ctypes.byref(rect)) else None

    def work_area(self, rect):
        native = wintypes.RECT(rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height())
        monitor = self.api.MonitorFromRect(ctypes.byref(native), 2)  # nearest
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        return _qrect(info.rcWork) if monitor and self.api.GetMonitorInfoW(monitor, ctypes.byref(info)) else None

    def distances(self, hwnd, threshold, gap, device_ratio=None):
        # Qt's ratio also includes an optional QT_SCALE_FACTOR, whereas native
        # GetDpiForWindow alone only includes the Windows display preference.
        dpi = self._get_dpi(hwnd) if self._get_dpi is not None and not device_ratio else 96
        scale = device_ratio or (dpi or 96) / 96.0
        return round(threshold * scale), round(gap * scale)

    def alt_pressed(self):
        return bool(self.api.GetAsyncKeyState(0x12) & 0x8000)


class _NativeMoveFilter(QAbstractNativeEventFilter):
    def __init__(self, manager):
        super().__init__()
        self.manager = weakref.ref(manager)

    def nativeEventFilter(self, event_type, message):
        manager = self.manager()
        if manager is not None and bytes(event_type) == b"windows_generic_MSG":
            return manager._native_message(message)
        return False, 0


class WindowSnapManager(QObject):
    """Register only independent top-level windows belonging to this toolbox.

    A QApplication parent is recommended, so closing the main window does not
    disable snapping for the independent notebook/converter. ``shutdown`` is
    idempotent and is automatically called at application exit.
    """

    def __init__(self, parent=None, *, threshold=14, gap=6):
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("WindowSnapManager requires QApplication")
        super().__init__(parent or app)
        self.threshold, self.gap = max(0, int(threshold)), max(0, int(gap))
        self._windows = weakref.WeakSet()
        self._handles = weakref.WeakValueDictionary()
        self._enabled = True
        self._adjusting = False
        self._pressed = None
        self._press_size = None
        self._native_loop = set()
        self._app = app
        self._native = None
        self._bridge = None
        self._closed = False
        if os.name == "nt" and QApplication.platformName() == "windows":
            try:
                self._native = _WindowsGeometry()
                self._bridge = _NativeMoveFilter(self)
                app.installNativeEventFilter(self._bridge)
            except (AttributeError, OSError):
                self._native = None
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.shutdown)

    def register(self, window):
        if not isinstance(window, QWidget) or not window.isWindow():
            raise ValueError("Only top-level QWidget windows can be registered")
        self._windows.add(window)
        if window.isVisible():
            self._remember_handle(window)

    def unregister(self, window):
        self._windows.discard(window)
        for handle, candidate in list(self._handles.items()):
            if candidate is window:
                self._handles.pop(handle, None)
                self._native_loop.discard(handle)
        if self._pressed is not None and self._pressed() is window:
            self._clear_press()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        if not self._enabled:
            self._clear_press()

    def is_enabled(self):
        return self._enabled

    def native_event(self, window, event_type, message):
        """Forward a native-title-bar QWidget's nativeEvent here before super().

        Return the usual PySide6 (handled, result) pair. Qt 6's Windows native
        filter sees dispatched events, but synchronous WM_MOVING from the
        native modal loop needs this per-window virtual-method forwarding.
        Frameless windows using QWidget.move already work through registration.
        """
        if (self._native is None or self._closed or window not in self._windows
                or bytes(event_type) != b"windows_generic_MSG"):
            return False, 0
        return self._native_message(message)

    def _remember_handle(self, window):
        if self._native is not None:
            for handle, candidate in list(self._handles.items()):
                if candidate is window:
                    self._handles.pop(handle, None)
            # effectiveWinId does not create a new native window while Qt is
            # destroying/recreating the old HWND (WinIdChange also reports 0).
            handle = int(window.effectiveWinId() or 0)
            if handle:
                self._handles[handle] = window

    @staticmethod
    def _eligible(window):
        try:
            return (window.isVisible() and window.isEnabled() and not window.isMinimized()
                    and not window.isMaximized() and not window.isFullScreen())
        except RuntimeError:  # A Qt-deleted widget can outlive its Python wrapper.
            return False

    def _alt_pressed(self):
        if self._native is not None:
            return self._native.alt_pressed()
        return bool(QApplication.queryKeyboardModifiers() & Qt.KeyboardModifier.AltModifier)

    def _left_pressed(self):
        return bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)

    def _clear_press(self):
        self._pressed = self._press_size = None

    def eventFilter(self, watched, event):
        kind = event.type()
        if isinstance(watched, QWidget):
            if watched in self._windows:
                if kind in (QEvent.Type.Show, QEvent.Type.WinIdChange):
                    self._remember_handle(watched)
                elif kind in (QEvent.Type.Hide, QEvent.Type.Close, QEvent.Type.WindowStateChange):
                    if self._pressed is not None and self._pressed() is watched:
                        self._clear_press()
                elif kind == QEvent.Type.Move:
                    self._snap_qt_move(watched)
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                # Third-party/legacy widgets may use "window" as a data field.
                # Call Qt's base implementation, not an instance override.
                window = QWidget.window(watched)
                if self._enabled and window in self._windows and self._eligible(window):
                    self._pressed = weakref.ref(window)
                    self._press_size = window.size()
            elif kind in (QEvent.Type.MouseButtonRelease, QEvent.Type.UngrabMouse):
                self._clear_press()
        elif kind == QEvent.Type.ApplicationDeactivate:
            self._clear_press()
        return False

    def _snap_qt_move(self, window):
        if (self._closed or not self._enabled or self._adjusting or self._pressed is None
                or self._pressed() is not window or self._press_size != window.size()
                or not self._left_pressed() or not self._eligible(window) or self._alt_pressed()):
            return
        if self._native is not None and int(window.effectiveWinId() or 0) in self._native_loop:
            return  # WM_MOVING already handles native move/resize loops.
        screen = QApplication.screenAt(QCursor.pos()) or window.screen()
        screens = [screen.availableGeometry()] if screen else []
        peers = [peer.frameGeometry() for peer in list(self._windows)
                 if peer is not window and self._eligible(peer)]
        original = window.frameGeometry()
        snapped = snap_rect(original, screens, peers, threshold=self.threshold, gap=self.gap)
        delta = snapped.topLeft() - original.topLeft()
        if not delta.isNull():
            self._adjusting = True
            try:
                # pos() includes a top-level window's frame; add the correction
                # rather than guessing the native title bar/client margins.
                window.move(window.pos() + delta)
            finally:
                self._adjusting = False

    def _native_message(self, address):
        if self._native is None:
            return False, 0
        try:
            message = _MSG.from_address(int(address))
            handle = int(message.hwnd or 0)
            window = self._handles.get(handle)
            if window is None:
                return False, 0
            if message.message == 0x0231:  # WM_ENTERSIZEMOVE
                self._native_loop.add(handle)
            elif message.message == 0x0232:  # WM_EXITSIZEMOVE
                self._native_loop.discard(handle)
                self._clear_press()
            elif (message.message == 0x0216 and self._enabled and not self._closed
                  and message.lParam and self._eligible(window) and not self._alt_pressed()):
                native_rect = wintypes.RECT.from_address(message.lParam)
                original = _qrect(native_rect)
                area = self._native.work_area(original)
                peers = []
                for peer_handle, peer in list(self._handles.items()):
                    if peer is not window and self._eligible(peer):
                        peer_rect = self._native.rect(peer_handle)
                        if peer_rect is not None:
                            peers.append(peer_rect)
                threshold, gap = self._native.distances(
                    handle, self.threshold, self.gap, window.devicePixelRatioF())
                snapped = snap_rect(original, [area] if area else [], peers,
                                    threshold=threshold, gap=gap)
                if snapped != original:
                    native_rect.left, native_rect.top = snapped.x(), snapped.y()
                    native_rect.right = snapped.x() + snapped.width()
                    native_rect.bottom = snapped.y() + snapped.height()
                    return True, 1  # WM_MOVING explicitly requires TRUE.
        except (RuntimeError, ValueError, TypeError, OSError):
            pass  # A closing/recreated native window must not break Qt dispatch.
        return False, 0

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._clear_press()
        self._native_loop.clear()
        self._app.removeEventFilter(self)
        if self._bridge is not None:
            self._app.removeNativeEventFilter(self._bridge)
        self._windows.clear()
        self._handles.clear()
