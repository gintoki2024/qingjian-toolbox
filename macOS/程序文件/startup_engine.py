"""Private LibreOffice bootstrap using only Python's standard library.

The MSI is read as a database and its cabinet is unpacked as data.  Neither
Windows Installer execution nor machine-wide registration is involved.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable


class EngineSetupError(RuntimeError):
    """A readable, recoverable source-startup engine failure."""


_OWNER = ".qingjian-engine-owner.json"
_STAGE_OWNER = ".qingjian-stage-owner.json"
_APP = "qingjian-source-runtime"
_MAX_DOWNLOAD_SECONDS = 15 * 60
_MAX_EXTRACT_SECONDS = 20 * 60
_CHUNK = 1024 * 1024
EventSink = Callable[[dict], None]


def _event(emit: EventSink, message: str, *, stage: str = "engine", **extra) -> None:
    emit({"event": "log", "stage": stage, "message": message, **extra})


def _engine_root(candidate: Path) -> Path | None:
    try:
        candidate = candidate.expanduser()
        if candidate.is_file():
            candidate = candidate.parent
        if candidate.name.casefold() == "program":
            candidate = candidate.parent
        program = candidate / "program"
        launcher = any((program / name).is_file() for name in ("soffice.com", "soffice.exe"))
        if launcher and (program / "soffice.bin").is_file() and (program / "fundamental.ini").is_file():
            return candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def find_engine(project: Path, runtime_home: Path, version: str = "26.2.6") -> Path | None:
    """Return an existing LibreOffice root without changing the computer."""
    candidates = []
    if (re.fullmatch(r"\d+\.\d+\.\d+", version)
            and not Path(runtime_home).resolve().is_relative_to(Path(project).resolve())):
        candidates.append(runtime_home / ('lo-' + version))
    candidates.extend([
        project / "engines" / "LibreOffice",
        project / "engines",
        project.parent / "青简工具箱" / "engines" / "LibreOffice",
        project.parent / "青简工具箱" / "engines",
    ])
    configured = os.environ.get("QINGJIAN_LIBREOFFICE", "").strip().strip('"')
    if configured:
        candidates.append(Path(configured))
    if re.fullmatch(r"\d+\.\d+\.\d+", version):
        candidates.append(runtime_home / ("lo-" + version))
    from startup_shared import component_locations
    for location in component_locations(project):
        if location.get('engine'):
            candidates.append(Path(location['engine']))
        old_home = Path(location['runtime_home'])
        if re.fullmatch(r"\d+\.\d+\.\d+", version):
            candidates.append(old_home / ('lo-' + version))
        try:
            candidates.extend(sorted((path for path in old_home.glob('lo-*')
                                      if re.fullmatch(r'lo-\d+\.\d+\.\d+', path.name)), reverse=True)[:8])
        except OSError:
            pass
    # Published portable releases may live beside the source as versioned
    # folders. Inspect this one directory only; never search the user's drive.
    parents = [project.parent]
    if project.name == '程序文件':
        parents.append(project.parent.parent)
    neighbors = []
    for parent in parents:
        for name in ('青简工具箱',):
            candidates.append(parent / name / '程序文件' / 'engines' / 'LibreOffice')
        try:
            neighbors.extend(parent.iterdir())
        except OSError:
            continue
    releases = []
    for neighbor in neighbors:
        match = re.fullmatch(r"青简工具箱_v([0-9]+)\.([0-9]+)\.([0-9]+)", neighbor.name)
        if match and neighbor.is_dir():
            releases.append((tuple(int(part) for part in match.groups()), neighbor))
    for _, neighbor in sorted(releases, key=lambda item: item[0], reverse=True):
        candidates.extend((neighbor / '程序文件' / 'engines' / 'LibreOffice',
                           neighbor / "engines" / "LibreOffice", neighbor / "engines"))
    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(name)
        if value:
            candidates.append(Path(value) / "LibreOffice")
    for name in ("soffice.com", "soffice.exe"):
        launcher = shutil.which(name)
        if launcher:
            candidates.append(Path(launcher))
    seen = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        root = _engine_root(candidate)
        if root is not None:
            return root
    return None


def _safe_name(value: str) -> str:
    """Accept one normal Windows filename, never a path or device name."""
    if (not isinstance(value, str) or not value or value in {".", ".."}
            or any(char in value for char in '/\\:\x00<>"|?*')
            or value[-1:] in {" ", "."}
            or any(ord(char) < 32 for char in value)):
        raise EngineSetupError("排版引擎包含不安全的文件路径。")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value):
        raise EngineSetupError("排版引擎包含 Windows 保留文件名。")
    if PureWindowsPath(value).is_absolute():
        raise EngineSetupError("排版引擎包含绝对路径。")
    return value


def _long_name(value: str, *, directory: bool = False) -> str:
    # MSI DefaultDir is target[:source]; each side can be short|long.
    if directory:
        value = value.split(":", 1)[0]
    value = value.rsplit("|", 1)[-1]
    if directory and value == ".":
        return ""
    return _safe_name(value)


@dataclass(frozen=True)
class _FileEntry:
    key: str
    relative: Path
    size: int
    sequence: int


def _file_plan(directories: list, components: list, files: list) -> dict[str, _FileEntry]:
    """Resolve MSI target names; INSTALLLOCATION becomes the private root."""
    directory_map = {row[0]: (row[1], row[2]) for row in directories}
    if len(directory_map) != len(directories):
        raise EngineSetupError("排版引擎目录表存在重复项。")
    component_map = {row[0]: row[1] for row in components}
    if len(component_map) != len(components):
        raise EngineSetupError("排版引擎组件表存在重复项。")
    resolved: dict[str, Path] = {"TARGETDIR": Path(), "INSTALLLOCATION": Path()}
    visiting: set[str] = set()

    def directory(key: str) -> Path:
        if key in resolved:
            return resolved[key]
        if key in visiting or key not in directory_map or len(visiting) > 128:
            raise EngineSetupError("排版引擎目录关系不完整或循环。")
        visiting.add(key)
        parent, name = directory_map[key]
        if not parent:
            raise EngineSetupError("排版引擎出现未知根目录。")
        base = directory(parent)
        name = _long_name(name, directory=True)
        result = base / name if name else base
        visiting.remove(key)
        resolved[key] = result
        return result

    plan: dict[str, _FileEntry] = {}
    paths: set[str] = set()
    for row in files:
        key, component, filename, size, sequence = row[:5]
        if key in plan or component not in component_map:
            raise EngineSetupError("排版引擎文件表存在重复项或缺失组件。")
        _safe_name(key)
        relative = directory(component_map[component]) / _long_name(filename)
        # This x64 MSI also contains conditional ARM64 VC redistributables.
        # Their MSI destination aliases System64, although their bytes differ.
        # Keep those files isolated; only the AMD64 System64 set is deployed
        # beside the x64 LibreOffice executable below.
        if (relative.parent == Path("System64") and "_arm64." in key
                and "_arm64." in component):
            relative = Path("System64-arm64") / relative.name
        folded = str(relative).casefold()
        if folded in paths:
            raise EngineSetupError("排版引擎文件路径发生冲突。")
        try:
            size, sequence = int(size), int(sequence)
        except (TypeError, ValueError) as exc:
            raise EngineSetupError("排版引擎文件大小或顺序无效。") from exc
        if size < 0 or size > 2 * 1024 ** 3 or sequence < 1:
            raise EngineSetupError("排版引擎文件大小或顺序越界。")
        paths.add(folded)
        plan[key] = _FileEntry(key, relative, size, sequence)
    if not plan:
        raise EngineSetupError("排版引擎文件表为空。")
    return plan


class _MsiDatabase:
    """Minimal Windows Installer read-only handle wrapper; no install calls."""

    def __init__(self, path: Path):
        if os.name != "nt":
            raise EngineSetupError("自动配置 LibreOffice 目前支持 Windows 64 位。")
        from ctypes import wintypes as wt
        self.wt = wt
        self.api = ctypes.WinDLL("msi.dll", use_last_error=True, winmode=0x800)
        handle = wt.UINT
        signatures = {
            "MsiOpenDatabaseW": [wt.LPCWSTR, wt.LPCWSTR, ctypes.POINTER(handle)],
            "MsiDatabaseOpenViewW": [handle, wt.LPCWSTR, ctypes.POINTER(handle)],
            "MsiViewExecute": [handle, handle],
            "MsiViewFetch": [handle, ctypes.POINTER(handle)],
            "MsiRecordGetStringW": [handle, wt.UINT, wt.LPWSTR, ctypes.POINTER(wt.DWORD)],
            "MsiRecordReadStream": [handle, wt.UINT, ctypes.c_void_p, ctypes.POINTER(wt.DWORD)],
            "MsiCloseHandle": [handle],
        }
        for name, arguments in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = wt.UINT
        self.handle = handle()
        self._check(self.api.MsiOpenDatabaseW(str(path), None, ctypes.byref(self.handle)), "读取排版引擎")

    @staticmethod
    def _check(code: int, action: str) -> None:
        if code:
            raise EngineSetupError(f"{action}失败（Windows 错误 {code}）。")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.handle:
            self.api.MsiCloseHandle(self.handle)
            self.handle = self.wt.UINT()

    def _view(self, query: str):
        view = self.wt.UINT()
        self._check(self.api.MsiDatabaseOpenViewW(self.handle, query, ctypes.byref(view)), "读取引擎文件表")
        try:
            self._check(self.api.MsiViewExecute(view, 0), "查询引擎文件表")
        except Exception:
            self.api.MsiCloseHandle(view)
            raise
        return view

    def _string(self, record, index: int) -> str:
        length = self.wt.DWORD()
        code = self.api.MsiRecordGetStringW(record, index, None, ctypes.byref(length))
        if code not in {0, 234} or length.value > 32767:
            raise EngineSetupError("排版引擎文件表包含无效文字字段。")
        buffer = ctypes.create_unicode_buffer(length.value + 1)
        length.value += 1
        self._check(self.api.MsiRecordGetStringW(record, index, buffer, ctypes.byref(length)), "读取引擎文件名")
        return buffer.value

    def rows(self, query: str, columns: int) -> list[list[str]]:
        view = self._view(query)
        result = []
        try:
            while True:
                record = self.wt.UINT()
                code = self.api.MsiViewFetch(view, ctypes.byref(record))
                if code == 259:
                    break
                self._check(code, "读取引擎文件表")
                try:
                    result.append([self._string(record, index) for index in range(1, columns + 1)])
                finally:
                    self.api.MsiCloseHandle(record)
        finally:
            self.api.MsiCloseHandle(view)
        return result

    def export_stream(self, name: str, target: Path) -> None:
        escaped = name.replace("'", "''")
        view = self._view("SELECT `Data` FROM `_Streams` WHERE `Name`='" + escaped + "'")
        record = self.wt.UINT()
        try:
            self._check(self.api.MsiViewFetch(view, ctypes.byref(record)), "读取引擎压缩包")
            buffer = ctypes.create_string_buffer(_CHUNK)
            with target.open("xb") as output:
                while True:
                    size = self.wt.DWORD(_CHUNK)
                    self._check(self.api.MsiRecordReadStream(record, 1, buffer, ctypes.byref(size)), "导出引擎压缩包")
                    if not size.value:
                        break
                    output.write(buffer.raw[:size.value])
        finally:
            if record:
                self.api.MsiCloseHandle(record)
            self.api.MsiCloseHandle(view)


def _extract_cabinet(cabinet: Path, flat: Path, entries: dict[str, _FileEntry], emit: EventSink,
                     *, deadline: float) -> dict[str, Path]:
    """Extract CAB entries into numbered flat files, preventing CAB path writes."""
    if os.name != "nt":
        raise EngineSetupError("自动解包排版引擎需要 Windows。")
    from ctypes import wintypes as wt

    class CabinetFile(ctypes.Structure):
        _fields_ = [("NameInCabinet", wt.LPCWSTR), ("FileSize", wt.DWORD),
                    ("Win32Error", wt.DWORD), ("DosDate", wt.WORD), ("DosTime", wt.WORD),
                    ("DosAttribs", wt.WORD), ("FullTargetName", wt.WCHAR * 260)]

    class FilePaths(ctypes.Structure):
        _fields_ = [("Target", wt.LPCWSTR), ("Source", wt.LPCWSTR),
                    ("Win32Error", wt.UINT), ("Flags", wt.DWORD)]

    callback_type = ctypes.WINFUNCTYPE(wt.UINT, ctypes.c_void_p, wt.UINT,
                                      ctypes.c_size_t, ctypes.c_size_t)
    api = ctypes.WinDLL("setupapi.dll", use_last_error=True, winmode=0x800)
    api.SetupIterateCabinetW.argtypes = [wt.LPCWSTR, wt.DWORD, callback_type, ctypes.c_void_p]
    api.SetupIterateCabinetW.restype = wt.BOOL
    flat.mkdir(exist_ok=False)
    extracted: dict[str, Path] = {}
    failure = []
    last_progress = [0.0]

    @callback_type
    def callback(_context, notification, param1, _param2):
        try:
            if time.monotonic() > deadline:
                raise EngineSetupError("排版引擎解包超时；下载缓存已保留，请关闭后重试。")
            if notification == 0x11:  # SPFILENOTIFY_FILEINCABINET
                info = ctypes.cast(param1, ctypes.POINTER(CabinetFile)).contents
                key = info.NameInCabinet
                if key not in entries or key in extracted:
                    raise EngineSetupError("排版引擎压缩包包含未知或重复文件。")
                if info.FileSize != entries[key].size:
                    raise EngineSetupError("排版引擎压缩包中文件大小与清单不一致。")
                destination = flat / (f"{len(extracted):06d}.bin")
                if len(str(destination)) >= 260:
                    raise EngineSetupError("运行环境目录过长；请将源码移至较短路径后重试。")
                info.FullTargetName = str(destination)
                extracted[key] = destination
                now = time.monotonic()
                if now - last_progress[0] >= 0.5:
                    emit({"event": "progress", "stage": "engine-extract", "progress": round(
                        len(extracted) * 100 / len(entries), 1), "message": "正在解包排版引擎"})
                    last_progress[0] = now
                return 1  # FILEOP_DOIT
            if notification == 0x13:  # SPFILENOTIFY_FILEEXTRACTED
                info = ctypes.cast(param1, ctypes.POINTER(FilePaths)).contents
                if info.Win32Error:
                    raise EngineSetupError(f"排版引擎文件解包失败（Windows 错误 {info.Win32Error}）。")
            if notification == 0x12:  # SPFILENOTIFY_NEEDNEWCABINET
                raise EngineSetupError("排版引擎要求未知的后续压缩包。")
            return 0
        except Exception as exc:
            if not failure:
                failure.append(exc)
            return 0  # FILEOP_ABORT / error

    ok = api.SetupIterateCabinetW(str(cabinet), 0, callback, None)
    if failure:
        raise EngineSetupError(str(failure[0])) from failure[0]
    if not ok:
        raise EngineSetupError(f"系统无法解包排版引擎（Windows 错误 {ctypes.get_last_error()}）。")
    for key, path in extracted.items():
        if not path.is_file() or path.stat().st_size != entries[key].size:
            raise EngineSetupError("排版引擎解包后文件不完整。")
    return extracted


def _extract_msi(msi: Path, stage: Path, emit: EventSink) -> Path:
    """Unpack a verified MSI in an already owned, private staging directory."""
    deadline = time.monotonic() + _MAX_EXTRACT_SECONDS
    output = stage / "engine"
    output.mkdir(exist_ok=False)
    _event(emit, "正在只读解析排版引擎，无需管理员安装。", stage="engine-extract")
    with _MsiDatabase(msi) as database:
        plan = _file_plan(
            database.rows("SELECT `Directory`, `Directory_Parent`, `DefaultDir` FROM `Directory`", 3),
            database.rows("SELECT `Component`, `Directory_` FROM `Component`", 2),
            database.rows("SELECT `File`, `Component_`, `FileName`, `FileSize`, `Sequence` FROM `File`", 5),
        )
        required = sum(entry.size for entry in plan.values()) + msi.stat().st_size + 64 * 1024 ** 2
        if shutil.disk_usage(stage).free < required:
            raise EngineSetupError(f"排版引擎解包需要约 {round(required / 1024 ** 3, 1)} GB 可用空间；"
                                   "下载缓存已保留，释放空间后重试即可。")
        media = database.rows("SELECT `DiskId`, `LastSequence`, `Cabinet` FROM `Media`", 3)
        try:
            media = sorted(media, key=lambda row: int(row[0]))
        except (ValueError, TypeError) as exc:
            raise EngineSetupError("排版引擎压缩包清单无效。") from exc
        extracted = {}
        previous = 0
        for index, row in enumerate(media):
            _, last, cabinet = row
            if not cabinet.startswith("#"):
                raise EngineSetupError("排版引擎压缩包未内嵌于已校验的 MSI。")
            _safe_name(cabinet[1:])
            last_sequence = int(last)
            part = {key: value for key, value in plan.items() if previous < value.sequence <= last_sequence}
            if not part:
                raise EngineSetupError("排版引擎压缩包对应的文件清单为空。")
            cab = stage / (f"payload-{index}.cab")
            database.export_stream(cabinet[1:], cab)
            extracted.update(_extract_cabinet(cab, stage / f"flat-{index}", part, emit, deadline=deadline))
            previous = last_sequence
        if set(extracted) != set(plan):
            raise EngineSetupError("排版引擎压缩包缺少必要文件。")
        _event(emit, "正在还原排版引擎目录。", stage="engine-extract")
        for key, source in extracted.items():
            if time.monotonic() > deadline:
                raise EngineSetupError("排版引擎目录还原超时；下载缓存已保留。")
            target = output / plan[key].relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
    # MSI files that normally install to the system directory stay app-local.
    system64 = output / "System64"
    if system64.is_dir():
        for dll in system64.glob("*.dll"):
            shutil.copy2(dll, output / "program" / dll.name)
    if _engine_root(output) is None:
        raise EngineSetupError("排版引擎解包后未找到完整的 LibreOffice。")
    return output


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _child(base: Path, name: str) -> Path:
    name = _safe_name(name)
    target = base / name
    if target.parent.resolve() != base.resolve() or _is_link(target):
        raise EngineSetupError("运行环境目录包含不安全的路径链接。")
    return target


def _owned(path: Path, *, kind: str, token: str | None = None) -> bool:
    marker = path / (_STAGE_OWNER if kind == "stage" else _OWNER)
    try:
        if _is_link(path) or _is_link(marker):
            return False
        data = json.loads(marker.read_text(encoding="utf-8"))
        return data.get("app") == _APP and data.get("kind") == kind and (
            token is None or data.get("token") == token)
    except (OSError, ValueError, AttributeError):
        return False


def _cleanup_stage(stage: Path, runtime_home: Path, token: str) -> None:
    # Only delete a direct child we created and still own.  Refuse all links.
    if (stage.parent.resolve() != runtime_home.resolve() or _is_link(stage)
            or not stage.name.startswith("lo-stage-") or not _owned(stage, kind="stage", token=token)):
        raise EngineSetupError("临时目录归属校验失败，已保留该目录。")
    for root, dirs, files in os.walk(stage, followlinks=False):
        for name in dirs + files:
            if _is_link(Path(root) / name):
                raise EngineSetupError("临时目录包含链接，已保留以避免误删。")
    shutil.rmtree(stage)


def _manifest_spec(manifest: dict) -> dict:
    try:
        spec = manifest["libreoffice"]
        version, filename = spec["version"], spec["filename"]
        digest, size = spec["sha256"], spec["bytes"]
        urls = spec["urls"]
        _safe_name(filename)
        if (not re.fullmatch(r"\d+\.\d+\.\d+", version)
                or not filename.lower().endswith(".msi")
                or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
                or isinstance(size, bool) or not isinstance(size, int) or not 0 < size < 2 * 1024 ** 3
                or not isinstance(urls, list) or not urls):
            raise ValueError()
        for source in urls:
            parsed = urllib.parse.urlsplit(source["url"])
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
            if not isinstance(source.get("name"), str) or not source["name"]:
                raise ValueError()
        return {**spec, "sha256": digest.lower()}
    except (KeyError, TypeError, ValueError, EngineSetupError) as exc:
        raise EngineSetupError("排版引擎下载清单不完整或不安全，请重新解压完整源码。") from exc


def _verified(path: Path, spec: dict) -> bool:
    try:
        if _is_link(path) or not path.is_file() or path.stat().st_size != spec["bytes"]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(_CHUNK), b""):
                digest.update(block)
        return digest.hexdigest() == spec["sha256"]
    except OSError:
        return False


def _download_msi(runtime_home: Path, spec: dict, emit: EventSink, *, offline: bool, cache_homes=()) -> Path:
    cache = _child(runtime_home, "cache")
    cache.mkdir(exist_ok=True)
    target = _child(cache, spec["filename"])
    if _verified(target, spec):
        _event(emit, "已找到通过校验的排版引擎下载缓存。", stage="engine-download")
        return target
    for old_home in dict.fromkeys(map(Path, cache_homes)):
        candidate = old_home / 'cache' / spec['filename']
        if not _is_link(candidate.parent) and _verified(candidate, spec):
            _event(emit, '复用旧目录中通过大小和SHA256校验的下载缓存，无需下载：' + str(candidate), stage='engine-download')
            return candidate
    if offline:
        raise EngineSetupError("离线模式下缺少排版引擎。请先联网启动一次，或将便携版 engines 文件夹复制到源码目录。")
    failures = []
    context = ssl.create_default_context()
    for source in spec["urls"]:
        name, url = source["name"], source["url"]
        part = _child(cache, spec["filename"] + "." + uuid.uuid4().hex[:10] + ".part")
        _event(emit, f"正在从{name}下载排版引擎（约 {round(spec['bytes'] / 1024 ** 2)} MB）。",
               stage="engine-download", source=name)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Qingjian-Source-Setup/1", "Accept-Encoding": "identity"})
            started = time.monotonic()
            last_progress = started - 1
            digest = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(request, timeout=30, context=context) as response, part.open("xb") as output:
                if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                    raise EngineSetupError("下载源重定向至不安全的地址。")
                while True:
                    block = response.read(_CHUNK)
                    if not block:
                        break
                    total += len(block)
                    if total > spec["bytes"]:
                        raise EngineSetupError("下载文件大小超过校验清单。")
                    output.write(block)
                    digest.update(block)
                    now = time.monotonic()
                    if now - started > _MAX_DOWNLOAD_SECONDS:
                        raise EngineSetupError("该下载源连接超时。")
                    if now - last_progress >= 0.5:
                        emit({"event": "progress", "stage": "engine-download", "source": name,
                              "progress": round(total * 100 / spec["bytes"], 1), "message": "正在下载排版引擎"})
                        last_progress = now
            if total != spec["bytes"] or digest.hexdigest() != spec["sha256"]:
                raise EngineSetupError("下载文件未通过大小和 SHA-256 校验。")
            part.replace(target)
            _event(emit, "排版引擎下载完成，校验通过。", stage="engine-download", source=name)
            return target
        except Exception as exc:
            failures.append(f"{name}：{type(exc).__name__}")
            _event(emit, f"{name}未能完成下载与校验，正在尝试下一个下载源。", stage="engine-download", source=name)
        finally:
            # A random, exclusively created file owned by this download only.
            if part.is_file() and not _is_link(part):
                part.unlink()
    raise EngineSetupError("所有排版引擎下载源均未成功（" + "；".join(failures)
                           + "）。请检查网络或稍后重试；不会关闭证书校验。")


def _probe_engine(root: Path) -> None:
    program = root / "program"
    executable = program / "soffice.com"
    if not executable.is_file():
        executable = program / "soffice.exe"
    env = os.environ.copy()
    # App-local redistributables are deliberately in program, not global PATH.
    env.pop("URE_BOOTSTRAP", None)
    try:
        result = subprocess.run([str(executable), "--headless", "--version"], cwd=program,
                                env=env, capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineSetupError("排版引擎已解包但启动验证失败；下载缓存已保留，请检查磁盘或公司运行策略。") from exc
    if result.returncode != 0 or b"LibreOffice" not in result.stdout + result.stderr:
        raise EngineSetupError("排版引擎未通过启动验证；下载缓存已保留，不会修改系统配置。")


def ensure_engine(project: Path, runtime_home: Path, manifest: dict, emit: EventSink,
                  *, offline: bool = False) -> Path:
    """Find a complete engine or obtain one in this application's private home."""
    spec = _manifest_spec(manifest)
    from startup_shared import copy_component_tree, needs_preservation
    existing = find_engine(Path(project), Path(runtime_home), spec["version"])
    if existing is not None and not needs_preservation(existing, project, runtime_home):
        _event(emit, "已找到可复用的 LibreOffice，无需下载：" + str(existing))
        return existing
    if os.name != "nt":
        raise EngineSetupError("自动配置排版引擎目前支持 Windows 64 位。")
    runtime_home = Path(runtime_home).expanduser().resolve()
    runtime_home.mkdir(parents=True, exist_ok=True)
    target = _child(runtime_home, "lo-" + spec["version"])
    if existing is not None:
        msi = None
    else:
        from startup_shared import component_locations
        msi = _download_msi(runtime_home, spec, emit, offline=offline,
                            cache_homes=[row['runtime_home'] for row in component_locations(project)])
    token = uuid.uuid4().hex
    stage = _child(runtime_home, "lo-stage-" + token[:10])
    stage.mkdir(exist_ok=False)
    (stage / _STAGE_OWNER).write_text(json.dumps({"app": _APP, "kind": "stage", "token": token}), encoding="utf-8")
    try:
        if existing is not None:
            _event(emit, "正在将已有 LibreOffice 保存到包外共享目录，无需重新下载：" + str(runtime_home))
            engine = stage / 'engine'
            try:
                copy_component_tree(existing, engine)
            except OSError as exc:
                raise EngineSetupError('已有LibreOffice迁移未完成，原目录保持不变：' + str(exc)) from exc
        else:
            engine = _extract_msi(msi, stage, emit)
        _event(emit, "正在验证排版引擎能否启动。")
        _probe_engine(engine)
        (engine / _OWNER).write_text(json.dumps({"app": _APP, "kind": "engine", "version": spec["version"],
                                               "sha256": spec["sha256"]}, ensure_ascii=False), encoding="utf-8")
        if target.exists():
            # A second source launch can finish while this one is unpacking.
            winner = _engine_root(target)
            if winner is not None:
                return winner
            if not _owned(target, kind="engine"):
                raise EngineSetupError("排版引擎目标目录已存在且不属于本程序，请更换运行环境目录。")
            backup = _child(runtime_home, "lo-incomplete-" + uuid.uuid4().hex[:10])
            target.rename(backup)
            _event(emit, "原有不完整环境已另存，正在启用新引擎。")
        engine.rename(target)
        _event(emit, "排版引擎配置完成，可在断网后继续使用。")
        return target
    finally:
        try:
            _cleanup_stage(stage, runtime_home, token)
        except (OSError, EngineSetupError) as exc:
            _event(emit, f"临时目录已保留，可稍后清理：{stage.name}（{type(exc).__name__}）。")
