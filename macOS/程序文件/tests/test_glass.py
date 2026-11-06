"""Native ABI guards and readable, non-native fallback."""
import ctypes
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

from qingjian import glass


@pytest.mark.parametrize("value, expected", [(0, .65), (.9, .9), (2, 1), (float("nan"), .9), (None, .9)])
def test_panel_opacity_clamp(value, expected):
    assert glass.panel_opacity(value) == expected


def test_offscreen_never_calls_native_and_never_fades_text(monkeypatch):
    window = QWidget()
    monkeypatch.setattr(glass, "_native_backdrop", lambda *args: pytest.fail("native call offscreen"))
    monkeypatch.setattr(glass, "_native_accent", lambda *args: pytest.fail("native call offscreen"))
    try:
        result = glass.apply_glass(window, .8)
        assert not result.enabled and window.windowOpacity() == 1
        assert window.property("frostedOpacity") == .8
    finally:
        window.deleteLater()


def test_native_failure_remains_usable(monkeypatch):
    window = QWidget()
    monkeypatch.setattr(glass, "_supported", lambda: (True, ""))
    monkeypatch.setattr(glass.sys, "getwindowsversion", lambda: SimpleNamespace(build=19045))
    def fail(*args): raise OSError("effect unavailable")
    monkeypatch.setattr(glass, "_native_accent", fail)
    try:
        result = glass.apply_glass(window)
        assert result.mode == "fallback" and "unavailable" in result.reason
    finally:
        window.deleteLater()


def test_windows11_prefers_documented_api_then_compat(monkeypatch):
    window = QWidget()
    calls = []
    monkeypatch.setattr(glass, "_supported", lambda: (True, ""))
    monkeypatch.setattr(glass.sys, "getwindowsversion", lambda: SimpleNamespace(build=22621))
    monkeypatch.setattr(glass, "_native_backdrop", lambda *args: (calls.append("dwm") or glass.GlassResult("acrylic")))
    monkeypatch.setattr(glass, "_native_accent", lambda *args: (calls.append("accent") or glass.GlassResult("acrylic-compat")))
    try:
        assert glass.apply_glass(window).enabled and calls == ["dwm"]
        monkeypatch.setattr(glass, "_native_backdrop", lambda *args: (calls.append("failed-dwm") or glass.GlassResult("fallback")))
        assert glass.apply_glass(window, force=True).mode == "acrylic-compat"
        assert calls[-2:] == ["failed-dwm", "accent"]
    finally:
        window.deleteLater()


def test_disable_switch_removes_native_effect(monkeypatch):
    window = QWidget()
    calls = []
    monkeypatch.setattr(glass, "remove_glass", lambda win: calls.append(win))
    monkeypatch.setenv("QINGJIAN_DISABLE_GLASS", "1")
    window.setProperty("glassMode", "acrylic")
    try:
        assert not glass.apply_glass(window).enabled
        assert calls == [window]
    finally:
        window.deleteLater()


class NativeFunction:
    def __init__(self, callback): self.callback = callback
    def __call__(self, *args): return self.callback(*args)


def test_compat_native_uses_64bit_safe_hwnd_and_size_t(monkeypatch):
    captured = []
    def setter(hwnd, data):
        value = ctypes.cast(data, ctypes.POINTER(glass._CompositionData)).contents
        policy = ctypes.cast(value.data, ctypes.POINTER(glass._AccentPolicy)).contents
        captured.append((hwnd.value, value.attribute, value.size, policy.state))
        return 1
    api = NativeFunction(setter)
    dll = SimpleNamespace(SetWindowCompositionAttribute=api)
    monkeypatch.setattr(glass.ctypes, "WinDLL", lambda *args, **kwargs: dll)
    handle = 0x12345678ABCDEF
    result = glass._native_accent(handle, True)
    assert result.enabled and captured == [(handle, 19, 16, 4)]
    assert api.argtypes == [ctypes.c_void_p, ctypes.POINTER(glass._CompositionData)]
    assert api.restype == ctypes.c_int32


def test_repeated_show_and_opacity_changes_do_not_reapply_native_effect(monkeypatch):
    window = QWidget()
    calls = []
    monkeypatch.setattr(glass, "_supported", lambda: (True, ""))
    monkeypatch.setattr(glass.sys, "getwindowsversion", lambda: SimpleNamespace(build=19045))
    monkeypatch.setattr(glass, "_native_accent", lambda *args: (calls.append(args) or glass.GlassResult("acrylic-compat")))
    try:
        for opacity in [.9, .8, .7, .9]:
            glass.apply_glass(window, opacity)
        assert len(calls) == 1
        assert window.property("frostedOpacity") == .9
        assert window.windowOpacity() == 1
    finally:
        window.deleteLater()


def test_drag_pauses_compat_effect_and_restores_only_once(monkeypatch):
    window = QWidget()
    calls = []
    monkeypatch.setattr(glass, "_supported", lambda: (True, ""))
    monkeypatch.setattr(glass.sys, "getwindowsversion", lambda: SimpleNamespace(build=19045))
    monkeypatch.setattr(glass, "_native_accent", lambda hwnd, enabled: (calls.append(enabled) or glass.GlassResult("acrylic-compat" if enabled else "fallback")))
    try:
        glass.apply_glass(window)
        assert glass.suspend_glass(window)
        assert not glass.suspend_glass(window)
        for _ in range(10):
            assert not glass.apply_glass(window).enabled
        assert calls == [True, False]
        assert glass.resume_glass(window)
        assert not glass.resume_glass(window)
        assert calls == [True, False, True]
        assert window.property("glassMode") == "acrylic-compat"
    finally:
        window.deleteLater()


def test_new_native_handle_invalidates_backdrop_cache(monkeypatch):
    class Window:
        handle = 123
        values = {}
        def winId(self): return self.handle
        def setProperty(self, key, value): self.values[key] = value
        def property(self, key): return self.values.get(key)
    window = Window()
    calls = []
    monkeypatch.setattr(glass, "_supported", lambda: (True, ""))
    monkeypatch.setattr(glass.sys, "getwindowsversion", lambda: SimpleNamespace(build=19045))
    monkeypatch.setattr(glass, "_native_accent", lambda hwnd, enable: (calls.append(hwnd) or glass.GlassResult("acrylic-compat")))
    glass.apply_glass(window)
    window.handle = 456
    glass.apply_glass(window)
    assert calls == [123, 456]
    assert ctypes.sizeof(glass._CompositionData) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 12)


def test_compat_failure_is_checked(monkeypatch):
    api = NativeFunction(lambda *args: 0)
    monkeypatch.setattr(glass.ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(SetWindowCompositionAttribute=api))
    assert not glass._native_accent(123, True).enabled


def test_dwm_uses_hresult_and_checks_frame_failure(monkeypatch):
    calls = []
    def compose(pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int32)).contents.value = 1
        return 0
    def setter(hwnd, attribute, data, size):
        calls.append((hwnd.value, attribute, ctypes.cast(data, ctypes.POINTER(ctypes.c_int32)).contents.value, size))
        return 0
    api = NativeFunction(setter)
    dll = SimpleNamespace(DwmSetWindowAttribute=api,
                          DwmIsCompositionEnabled=NativeFunction(compose),
                          DwmExtendFrameIntoClientArea=NativeFunction(lambda *args: -1))
    monkeypatch.setattr(glass.ctypes, "WinDLL", lambda *args, **kwargs: dll)
    result = glass._native_backdrop(0x123456789, True)
    assert not result.enabled and result.hresult == -1
    assert calls == [(0x123456789, 38, 3, 4), (0x123456789, 38, 1, 4)]
    assert api.restype == ctypes.c_int32
