"""Wheel input during Windows OLE file drags, scoped to one hovered widget.

OLE can consume WM_MOUSEWHEEL before Qt creates QWheelEvent. A short-lived
WH_MOUSE_LL hook collects only vertical wheel input over this drop target.
It never opens files, starts a conversion, or installs a system-wide setting.
Other mouse input always continues through CallNextHookEx.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication


class DragWheelBridge(QObject):
    ready = Signal()

    def __init__(self, target):
        super().__init__(target)
        self.target = target
        self._hook = None
        self._callback = None
        self._api = None
        self._pending = []
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(150)
        self._watchdog.timeout.connect(self._check_lifetime)
        self.destroyed.connect(lambda: self.stop())
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self.stop)

    @property
    def active(self):
        return bool(self._hook)

    def start(self):
        if self.active or os.name != "nt" or QApplication.platformName() != "windows":
            return
        try:
            api = ctypes.WinDLL("user32", use_last_error=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            result_type = ctypes.c_ssize_t
            callback_type = ctypes.WINFUNCTYPE(result_type, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

            class MouseInfo(ctypes.Structure):
                _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                            ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_size_t)]

            api.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
            api.SetWindowsHookExW.restype = wintypes.HHOOK
            api.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            api.CallNextHookEx.restype = result_type
            api.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
            api.UnhookWindowsHookEx.restype = wintypes.BOOL
            api.GetAsyncKeyState.argtypes = [ctypes.c_int]
            api.GetAsyncKeyState.restype = ctypes.c_short
            kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel.GetModuleHandleW.restype = wintypes.HMODULE
            self._api = api

            def hook(code, message, address):
                if code == 0 and message == 0x020A and self.active:  # WM_MOUSEWHEEL
                    try:
                        target = self.target
                        # Qt maps physical coordinates to the correct monitor's
                        # logical coordinates, including mixed-DPI desktops.
                        local = target.mapFromGlobal(QCursor.pos())
                        if target._drag_active and target.isVisible() and target.isEnabled() and len(target.targets) > 8 and target.rect().contains(local):
                            data = ctypes.cast(address, ctypes.POINTER(MouseInfo)).contents
                            delta = ctypes.c_short(data.mouseData >> 16).value
                            if delta:
                                self._pending.append(delta)
                                self.ready.emit()  # queued: never rebuild Qt controls in the hook
                                return 1  # prevent a second Qt event and source-window scrolling
                    except (RuntimeError, ValueError, OSError):
                        pass
                return api.CallNextHookEx(self._hook, code, message, address)

            self._callback = callback_type(hook)
            self._hook = api.SetWindowsHookExW(14, self._callback, kernel.GetModuleHandleW(None), 0)
            if self._hook:
                self._watchdog.start()
        except (AttributeError, OSError, ValueError):
            # Normal Qt wheel events and visible page buttons remain available.
            self._hook = None

    def take_pending(self):
        pending, self._pending = self._pending, []
        return pending

    def _check_lifetime(self):
        try:
            dragging = any(self._api.GetAsyncKeyState(key) & 0x8000 for key in (1, 2))
            escape = self._api.GetAsyncKeyState(0x1B) & 0x8000
            if not dragging or escape or not self.target.isVisible() or not self.target.isEnabled():
                self.target.end_drag()
        except RuntimeError:
            self.stop()

    def stop(self):
        if self._hook:
            self._api.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self._pending.clear()
        try:
            self._watchdog.stop()
        except RuntimeError:
            pass
