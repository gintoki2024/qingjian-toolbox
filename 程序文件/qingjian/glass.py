"""Optional Windows Acrylic; readable alpha-panel fallback.

Only window-local DWM attributes are touched. No registry, system policy,
whole-window opacity is used. Microsoft documents
DWMSBT_TRANSIENTWINDOW (3) as Desktop Acrylic on Windows 11 build 22621+:
https://learn.microsoft.com/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type
"""
from __future__ import annotations

import ctypes
import math
import os
import sys
from dataclasses import dataclass

from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class GlassResult:
    mode: str
    reason: str = ""
    hresult: int | None = None

    @property
    def enabled(self):
        return self.mode in {"acrylic", "acrylic-compat"}


class _Margins(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in
                ("left", "right", "top", "bottom")]


class _AccentPolicy(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ("state", "flags", "tint", "animation")]


class _CompositionData(ctypes.Structure):
    _fields_ = [("attribute", ctypes.c_int32), ("data", ctypes.c_void_p),
                ("size", ctypes.c_size_t)]


def panel_opacity(value):
    """Clamp panel alpha without making text, controls or icons translucent."""
    try:
        value = float(value)
        return min(1.0, max(0.65, value)) if math.isfinite(value) else 0.9
    except (TypeError, ValueError):
        return 0.9


def _supported():
    app = QApplication.instance()
    if sys.platform != "win32":
        return False, "非 Windows，使用半透明面板"
    if app is None or app.platformName().lower() != "windows":
        return False, "非桌面显示平台，使用半透明面板"
    if sys.getwindowsversion().build < 17134:
        return False, "当前 Windows 使用半透明面板"
    return True, ""


def _native_backdrop(hwnd, enable):
    # LOAD_LIBRARY_SEARCH_SYSTEM32 prevents DLL lookup in the working directory.
    dwm = ctypes.WinDLL("dwmapi.dll", use_last_error=True, winmode=0x800)
    setter = dwm.DwmSetWindowAttribute
    setter.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.c_uint32]
    setter.restype = ctypes.c_int32  # HRESULT is 32-bit, including on x64.
    extend = dwm.DwmExtendFrameIntoClientArea
    extend.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Margins)]
    extend.restype = ctypes.c_int32
    composed = dwm.DwmIsCompositionEnabled
    composed.argtypes = [ctypes.POINTER(ctypes.c_int32)]
    composed.restype = ctypes.c_int32
    composition = ctypes.c_int32()
    hr = composed(ctypes.byref(composition))
    if hr < 0 or (enable and not composition.value):
        return GlassResult("fallback", "系统未启用桌面合成", int(hr))
    handle = ctypes.c_void_p(int(hwnd))
    backdrop = ctypes.c_int32(3 if enable else 1)
    hr = setter(handle, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
    if hr < 0:
        return GlassResult("fallback", "系统未提供毛玻璃效果", int(hr))
    margins = _Margins(*([-1] * 4 if enable else [0] * 4))
    frame_hr = extend(handle, ctypes.byref(margins))
    if frame_hr < 0:
        # Roll back a partially enabled backdrop; UI retains its alpha surface.
        plain = ctypes.c_int32(1)
        setter(handle, 38, ctypes.byref(plain), ctypes.sizeof(plain))
        return GlassResult("fallback", "系统使用半透明面板", int(frame_hr))
    if enable:
        corner = ctypes.c_int32(2)  # DWMWCP_ROUND; a cosmetic best effort.
        setter(handle, 33, ctypes.byref(corner), ctypes.sizeof(corner))
    return GlassResult("acrylic" if enable else "fallback")


def _native_accent(hwnd, enable):
    """Small Windows 10 compatibility boundary; no framework code is copied.

    Microsoft documents the exported function and BOOL ABI but recommends DWM
    for new apps. Accent constants are compatibility values (not a public SDK
    guarantee). We dynamically resolve them only on supported Windows builds,
    check BOOL and retain a complete alpha fallback.
    https://learn.microsoft.com/windows/win32/dwm/setwindowcompositionattribute
    """
    user = ctypes.WinDLL("user32.dll", use_last_error=True, winmode=0x800)
    setter = user.SetWindowCompositionAttribute
    setter.argtypes = [ctypes.c_void_p, ctypes.POINTER(_CompositionData)]
    setter.restype = ctypes.c_int32  # Win32 BOOL, not ctypes.c_bool.
    # state 4 = Acrylic, attribute 19 = Accent Policy. A neutral ABGR tint
    # adds gentle diffusion; the UI independently paints its readable panel.
    policy = _AccentPolicy(4 if enable else 0, 0, 0x22FAF7F5, 0)
    data = _CompositionData(19, ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p),
                            ctypes.sizeof(policy))
    success = setter(ctypes.c_void_p(int(hwnd)), ctypes.byref(data))
    if not success:
        return GlassResult("fallback", "系统未启用兼容毛玻璃效果", ctypes.get_last_error())
    return GlassResult("acrylic-compat" if enable else "fallback", "Windows 10 兼容效果")


def apply_glass(window, opacity=0.9, *, enabled=True, force=False):
    """Call after show()/native-handle creation; always safe to fall back.

    ``frostedOpacity`` describes the UI's painted panel, never Windows' layered
    whole-window opacity. Actual DWM appearance follows the user's visual
    effects and accessibility preferences even when the API returns S_OK.
    """
    opacity = panel_opacity(opacity)
    if window.property("frostedOpacity") != opacity:
        window.setProperty("frostedOpacity", opacity)
    supported, reason = _supported()
    disabled = not enabled or os.environ.get("QINGJIAN_DISABLE_GLASS") == "1" or bool(window.property("disableNativeGlass"))
    # Native backdrops belong to a HWND, not to each opacity/geometry setting.
    # Reapplying Acrylic during normal show/drag events is expensive on Win10.
    hwnd = int(window.winId()) if supported else None
    key = (hwnd, bool(supported), disabled)
    cached = getattr(window, "_qingjian_glass_cache", None)
    if not force and cached is not None and cached[0] == key:
        return cached[1]
    if getattr(window, "_qingjian_glass_suspended", False):
        return GlassResult("fallback", "拖动期间使用轻量背景")
    if disabled:
        if window.property("glassMode") in {"acrylic", "acrylic-compat"}:
            remove_glass(window)
        window.setProperty("glassMode", "fallback")
        result = GlassResult("fallback", "已关闭系统毛玻璃，使用半透明面板")
        window._qingjian_glass_cache = (key, result)
        return result
    result = GlassResult("fallback", reason)
    if supported:
        try:
            if sys.getwindowsversion().build >= 22621:
                result = _native_backdrop(hwnd, True)
            if not result.enabled:
                result = _native_accent(hwnd, True)
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            result = GlassResult("fallback", str(exc))
    window.setProperty("glassMode", result.mode)
    window._qingjian_glass_cache = (key, result)
    return result


def remove_glass(window):
    window._qingjian_glass_cache = None
    supported, reason = _supported()
    if not supported:
        window.setProperty("glassMode", "fallback")
        return GlassResult("fallback", reason)
    mode = window.property("glassMode")
    try:
        if mode == "acrylic-compat" or sys.getwindowsversion().build < 22621:
            result = _native_accent(window.winId(), False)
        else:
            result = _native_backdrop(window.winId(), False)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        result = GlassResult("fallback", str(exc))
    window.setProperty("glassMode", "fallback")
    return result


def suspend_glass(window):
    """Temporarily release Win10 Acrylic for a responsive manual window drag.

    Idempotent: the caller may receive several mouse/native move begin events.
    Window content remains opaque; only the expensive background is paused.
    """
    if getattr(window, "_qingjian_glass_suspended", False):
        return False
    if window.property("glassMode") != "acrylic-compat":
        return False
    window._qingjian_glass_suspended = True
    remove_glass(window)
    return True


def resume_glass(window):
    """Restore the static effect once after drag end, never on every move."""
    if not getattr(window, "_qingjian_glass_suspended", False):
        return False
    window._qingjian_glass_suspended = False
    apply_glass(window, window.property("frostedOpacity") or 0.9, force=True)
    return True
