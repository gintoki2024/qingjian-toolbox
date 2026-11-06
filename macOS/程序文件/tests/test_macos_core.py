"""Platform contracts exercisable on Windows, plus real POSIX-only coverage.

Simulation checks branch selection; they do not replace Finder/Cocoa or native
Mac conversion acceptance on actual Apple Silicon/Intel hardware.
"""
from __future__ import annotations

import json
import builtins
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from qingjian import platform_support
from qingjian.core import common, conversion, format_archive, format_media


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test executable")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("name", ["icons", "window_snap", "drag_wheel"])
def test_native_ui_modules_import_without_windows_ctypes(name):
    """Execute the module's actual imports/definitions with Darwin/Posix values."""
    original_import = builtins.__import__

    def guarded_import(module, globals=None, locals=None, fromlist=(), level=0):
        if module == "ctypes" and "wintypes" in (fromlist or ()):
            raise AssertionError("macOS must not import Win32 types")
        if module == "os":
            return SimpleNamespace(name="posix")
        if module == "sys":
            return SimpleNamespace(platform="darwin")
        return original_import(module, globals, locals, fromlist, level)

    source = Path(__file__).resolve().parents[1] / "qingjian" / (name + ".py")
    namespace = {"__name__": "qingjian._mac_import_" + name, "__package__": "qingjian",
                 "__builtins__": {**vars(builtins), "__import__": guarded_import}}
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
    assert "wintypes" not in namespace


@pytest.mark.parametrize("component,relative,payload", [
    ("7zip", "7zip/7z.exe", "7zz"),
    ("7zip", "7zip/7z.exe", "7z"),
    ("Ghostscript", "Ghostscript/bin/gswin64c.exe", "bin/gs"),
    ("OFD", "OFD/runtime/bin/java.exe", "runtime/bin/java"),
    ("OFD", "OFD/runtime/bin/java.exe", "runtime/Contents/Home/bin/java"),
    ("OFD", "OFD/bridge.jar", "bridge.jar"),
])
def test_native_component_root_contract_on_macos(tmp_path, monkeypatch, component, relative, payload):
    root = tmp_path / component
    expected = executable(root / payload)
    monkeypatch.setenv("QINGJIAN_NATIVE_COMPONENTS", json.dumps({component: str(root)}))
    with mock.patch.object(sys, "platform", "darwin"):
        assert format_media.engine(relative) == expected


def test_macos_component_does_not_launch_windows_executable(tmp_path, monkeypatch):
    root = tmp_path / "7zip"
    executable(root / "7z.exe")
    monkeypatch.setenv("QINGJIAN_NATIVE_COMPONENTS", json.dumps({"7zip": str(root)}))
    with mock.patch.object(sys, "platform", "darwin"), pytest.raises(RuntimeError, match="缺少离线转换组件"):
        format_media.engine("7zip/7z.exe")


def test_macos_engine_requires_execute_permission(tmp_path, monkeypatch):
    root = tmp_path / "7zip"
    executable(root / "7zz")
    monkeypatch.setenv("QINGJIAN_NATIVE_COMPONENTS", json.dumps({"7zip": str(root)}))
    with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(format_media.os, "access", return_value=False):
        with pytest.raises(RuntimeError, match="执行权限"):
            format_media.engine("7zip/7z.exe")


@pytest.mark.parametrize("selection", ["LibreOffice.app", ".", "LibreOffice.app/Contents/MacOS/soffice"])
def test_macos_libreoffice_accepts_app_directory_and_binary(tmp_path, monkeypatch, selection):
    expected = executable(tmp_path / "LibreOffice.app/Contents/MacOS/soffice")
    monkeypatch.setenv("QINGJIAN_LIBREOFFICE", str(tmp_path / selection))
    with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(conversion, "_bundled_roots", return_value=[]):
        assert conversion.find_libreoffice() == expected.resolve()


def test_macos_bundled_libreoffice_takes_priority(tmp_path, monkeypatch):
    expected = executable(tmp_path / "engines/LibreOffice/LibreOffice.app/Contents/MacOS/soffice")
    external = executable(tmp_path / "external/soffice")
    monkeypatch.setenv("QINGJIAN_LIBREOFFICE", str(external))
    with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(conversion, "_bundled_roots", return_value=[tmp_path]):
        assert conversion.find_libreoffice() == expected.resolve()


def test_posix_engine_launch_uses_private_session_and_safe_path(tmp_path, monkeypatch):
    native = executable(tmp_path / "bin/gs")
    process = mock.Mock(returncode=0, pid=1234)
    process.communicate.return_value = (b"complete", b"")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/unexpected/library")
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-javaagent:unexpected")
    fake_os = mock.Mock(wraps=os)
    fake_os.name = "posix"
    with mock.patch.object(format_media, "os", fake_os), mock.patch.object(conversion, "_attach_windows_job", return_value=None), \
            mock.patch.object(conversion, "_close_windows_job"), mock.patch.object(format_media.subprocess, "Popen", return_value=process) as launch:
        assert format_media.run_engine([native, "-version"]) == "complete"
    kwargs = launch.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["creationflags"] == 0
    assert kwargs["env"]["PATH"].split(os.pathsep) == [str(native.parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert "DYLD_INSERT_LIBRARIES" not in kwargs["env"]
    assert "JAVA_TOOL_OPTIONS" not in kwargs["env"]


def test_posix_cancel_reaps_group_even_if_leader_has_exited():
    process = mock.Mock(pid=2468)
    process.poll.return_value = 0
    fake_os = mock.Mock(wraps=os)
    fake_os.name = "posix"
    fake_os.killpg = mock.Mock()
    with mock.patch.object(conversion, "os", fake_os), mock.patch.object(conversion, "signal", SimpleNamespace(SIGKILL=9)):
        conversion._terminate_process_tree(process)
    fake_os.killpg.assert_called_once_with(2468, 9)
    process.communicate.assert_called_once_with(timeout=5)


def test_archive_pack_uses_platform_neutral_content_argument(tmp_path):
    target = tmp_path / "target.7z"
    with mock.patch.object(format_archive, "engine", return_value=Path("7zz")), \
            mock.patch.object(format_archive, "run_engine") as run:
        format_archive.pack(tmp_path, target)
    assert run.call_args.args[0][-1] == "."
    assert run.call_args.kwargs["cwd"] == tmp_path


def test_macos_vector_conversion_uses_private_ghostscript_resource_and_icc_directories(tmp_path):
    gs = executable(tmp_path / "Ghostscript/bin/gs")
    base = tmp_path / "Ghostscript/share/ghostscript/10.07.0"
    for name in ("Resource/Init/gs_init.ps", "iccprofiles/default_rgb.icc"):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"resource")
    source, target = tmp_path / "source.eps", tmp_path / "result.pdf"
    source.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n")
    with mock.patch.object(sys, "platform", "darwin"), \
            mock.patch.object(format_media, "engine", return_value=gs), \
            mock.patch.object(format_media, "run_engine") as run:
        result = format_media.vector_convert(source, target)
    command = run.call_args.args[0]
    assert "-sGenericResourceDir=" + str(base / "Resource") + "/" in command
    assert "-sICCProfilesDir=" + str(base / "iccprofiles") + "/" in command
    assert command.index("-sGenericResourceDir=" + str(base / "Resource") + "/") < command.index(source)
    assert "-dSAFER" in command
    assert result["outputs"] == [str(target)]


def test_macos_ghostscript_missing_resources_does_not_launch_engine(tmp_path):
    gs = executable(tmp_path / "Ghostscript/bin/gs")
    with mock.patch.object(sys, "platform", "darwin"), \
            mock.patch.object(format_media, "engine", return_value=gs), \
            mock.patch.object(format_media, "run_engine") as run:
        with pytest.raises(RuntimeError, match="本地资源"):
            format_media.vector_convert(tmp_path / "source.eps", tmp_path / "result.pdf")
    run.assert_not_called()


def test_windows_ghostscript_relocation_options_remain_unchanged():
    with mock.patch.object(sys, "platform", "win32"):
        assert format_media._ghostscript_resource_options(Path("gswin64c.exe")) == []


def test_macos_defaults_preserve_user_fonts():
    from qingjian.core.official_ops import validate_official_options
    with mock.patch.object(sys, "platform", "darwin"):
        assert platform_support.ui_font_family() == "PingFang SC"
        assert platform_support.platform_description() == "macOS"
        defaults = validate_official_options({})
        assert defaults["body_font"] == "STFangsong"
        assert defaults["title_font"] == "Songti SC"
        selected = validate_official_options({"body_font": "公司自选字体", "title_font": "方正小标宋简体"})
        assert selected["body_font"] == "公司自选字体"
        assert selected["title_font"] == "方正小标宋简体"
        assert platform_support.shortcut_text("Ctrl+V · Alt+Left") == "⌘+V · ⌥+Left"
        assert "'PingFang SC'" in platform_support.platform_stylesheet("font-family: 'Microsoft YaHei UI', 'Microsoft YaHei'")
    with mock.patch.object(sys, "platform", "win32"):
        style = "font-family: 'Microsoft YaHei UI', 'Microsoft YaHei'"
        assert platform_support.platform_stylesheet(style) == style


def test_posix_generated_chinese_filename_fits_filesystem_byte_limit():
    fake_os = mock.Mock(wraps=os)
    fake_os.name = "posix"
    with mock.patch.object(common, "os", fake_os):
        name = common.safe_name("中文办公材料" * 80)
    assert len(name.encode("utf-8")) <= 180
    assert "�" not in name


def test_cocoa_ui_does_not_call_windows_apis(desktop_application):
    import ctypes
    from PySide6.QtWidgets import QWidget, QApplication
    from qingjian import icons
    from qingjian.drag_wheel import DragWheelBridge
    from qingjian.window_snap import WindowSnapManager
    widget = QWidget()
    try:
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(QApplication, "platformName", return_value="cocoa"), \
                mock.patch.object(ctypes, "WinDLL", create=True, side_effect=AssertionError("Windows API called")):
            assert not icons.apply_process_taskbar_identity()
            assert not icons.apply_native_window_icon(widget)
            bridge = DragWheelBridge(widget)
            bridge.start()
            assert not bridge.active
            manager = WindowSnapManager()
            assert manager._native is None
            assert manager._native_message(0) == (False, 0)
            manager.shutdown()
    finally:
        widget.deleteLater()


def test_gui_ready_handshake_reports_real_interpreter_and_pid(tmp_path, monkeypatch):
    from qingjian.app import _write_gui_ready
    marker = tmp_path / "gui-ready.json"
    monkeypatch.setenv("QINGJIAN_GUI_READY_FILE", str(marker))
    with mock.patch.object(sys, "platform", "darwin"):
        _write_gui_ready()
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema_version": 1, "pid": os.getpid(), "python": sys.executable, "platform": "darwin"}
    assert list(tmp_path.iterdir()) == [marker]


def test_gui_ready_handshake_rejects_relative_paths(tmp_path, monkeypatch):
    from qingjian.app import _write_gui_ready
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QINGJIAN_GUI_READY_FILE", "relative.json")
    _write_gui_ready()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Real POSIX process groups require macOS/Linux")
def test_real_posix_process_group_cancel(tmp_path):
    """The child must not outlive a canceled converter, even after parent exit."""
    marker = tmp_path / "late-child.txt"
    code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1]])"
    child = f"import time,pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('orphan')"
    process = subprocess.Popen([sys.executable, "-c", code, child], start_new_session=True)
    try:
        process.wait(timeout=5)
        conversion._terminate_process_tree(process)
        import time
        time.sleep(2.2)
        assert not marker.exists()
    finally:
        conversion._terminate_process_tree(process)
