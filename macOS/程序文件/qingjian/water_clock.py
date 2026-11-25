"""Launch the bundled native clock independently of the toolbox."""
from pathlib import Path
import os
import subprocess
import sys


ASSETS = Path(__file__).resolve().parent / "assets" / "waterclock"


def _copy_download_attribute(archive, app):
    """Keep the download's macOS security metadata when unpacking the app."""
    import ctypes
    import errno

    libc = ctypes.CDLL(None, use_errno=True)
    attr = b"com.apple.quarantine"
    path = os.fsencode(archive)
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                              ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    libc.setxattr.argtypes = libc.getxattr.argtypes
    libc.setxattr.restype = ctypes.c_int
    size = libc.getxattr(path, attr, None, 0, 0, 0)
    if size < 0:
        code = ctypes.get_errno()
        if code in (errno.ENOATTR, errno.ENOTSUP):
            return
        raise OSError(code, os.strerror(code), archive)
    value = ctypes.create_string_buffer(size)
    count = libc.getxattr(path, attr, value, size, 0, 0)
    if count < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), archive)
    for target in (app, app / "Contents/MacOS/WaterClock"):
        if libc.setxattr(os.fsencode(target), attr, value, count, 0, 0) < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), target)


def _restore_windows_clock():
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowExW.restype = wintypes.HWND
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    window = None
    while True:
        window = user.FindWindowExW(None, window, None, "水流时钟")
        if not window:
            return False
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(window, name, len(name))
        if name.value.startswith("WindowsForms10.Window."):
            user.ShowWindowAsync(window, 9)  # SW_RESTORE
            user.SetForegroundWindow(window)
            return True


def launch_clock():
    if sys.platform == "win32":
        if not _restore_windows_clock():
            os.startfile(str(ASSETS / "WaterflowClock.exe"))
        return
    if sys.platform == "darwin":
        import hashlib
        import tempfile
        import zipfile

        archive = ASSETS / "WaterflowClock.zip"
        version = hashlib.sha256(archive.read_bytes()).hexdigest()[:16]
        parent = Path.home() / "Library/Application Support/QingjianLocal/WaterflowClock"
        parent.mkdir(parents=True, exist_ok=True)
        app = parent / version / "水流时钟.app"
        if not app.is_dir():
            with tempfile.TemporaryDirectory(dir=parent) as staging:
                stage = Path(staging)
                with zipfile.ZipFile(archive) as bundle:
                    for relative in ("Contents/Info.plist", "Contents/_CodeSignature/CodeResources",
                                     "Contents/MacOS/WaterClock"):
                        member = "水流时钟.app/" + relative
                        target = stage / member
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(bundle.read(member))
                (stage / "水流时钟.app/Contents/MacOS/WaterClock").chmod(0o755)
                _copy_download_attribute(archive, stage / app.name)
                # Publish a complete bundle atomically when two toolbox windows race.
                app.parent.mkdir(exist_ok=True)
                try:
                    (stage / app.name).rename(app)
                except OSError:
                    if not app.is_dir():
                        raise
        result = subprocess.run(["/usr/bin/open", str(app)], capture_output=True, text=True)
        if result.returncode:
            raise OSError(result.stderr.strip() or "macOS 无法打开水流时钟。")
        return
    raise OSError("水流时钟支持 Windows 和 macOS。")
