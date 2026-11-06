"""Local document conversion without an installed Python environment.

LibreOffice is optional at import time.  PDF conversion uses the bundled
pdfplumber / PDFium stack and never uploads source documents.
"""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from ..platform_support import default_document_font


def _engine_candidates(root: Path) -> list[Path]:
    if sys.platform == "darwin":
        return _selected_engine_candidates(root / "engines" / "LibreOffice")
    base = root / "engines" / "LibreOffice" / "program"
    return [base / "soffice.com", base / "soffice.exe"]


def _selected_engine_candidates(selected: Path) -> list[Path]:
    if sys.platform == "darwin":
        return [selected / "Contents/MacOS/soffice",
                selected / "LibreOffice.app/Contents/MacOS/soffice",
                selected / "bin/soffice", selected]
    return [selected]


def _bundled_roots() -> list[Path]:
    roots = [Path(sys.executable).resolve().parent, Path(__file__).resolve().parents[2]]
    # PyInstaller one-folder builds can also put resources in _internal.
    if getattr(sys, "_MEIPASS", None):
        roots.append(Path(sys._MEIPASS))
    return list(dict.fromkeys(roots))


def find_libreoffice() -> Path | None:
    """Find a bundled engine first, then an explicitly selected/local engine."""
    candidates: list[Path] = []
    for root in _bundled_roots():
        candidates.extend(_engine_candidates(root))
    explicit = os.environ.get("QINGJIAN_LIBREOFFICE", "").strip().strip('"')
    if explicit:
        candidates.extend(_selected_engine_candidates(Path(explicit).expanduser()))
    if sys.platform == "darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            candidates.append(base / "LibreOffice.app/Contents/MacOS/soffice")
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(variable):
            base = Path(os.environ[variable]) / "LibreOffice" / "program"
            candidates.extend((base / "soffice.com", base / "soffice.exe"))
    if os.name == "nt":
        for base in (Path("C:/Program Files"), Path("C:/Program Files (x86)")):
            candidates.extend(
                (base / "LibreOffice/program/soffice.com", base / "LibreOffice/program/soffice.exe")
            )
    for name in (("soffice", "libreoffice") if sys.platform == "darwin" else ("soffice.com", "soffice.exe", "soffice", "libreoffice")):
        if found := shutil.which(name):
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file() and (sys.platform != "darwin" or os.access(candidate, os.X_OK)):
            return candidate.resolve()
    return None


def _validate_paths(source: Path, target: Path, source_types: set[str], target_types: set[str]) -> tuple[Path, Path]:
    source = Path(source).expanduser().resolve()
    target = Path(target).expanduser().absolute()
    if os.path.lexists(target):
        if os.path.normcase(str(source)) == os.path.normcase(str(target.resolve())):
            raise ValueError("输入文件与输出文件不能相同，请选择新的输出文件名。")
        raise FileExistsError(f"输出文件已存在，为保护原文件已停止：{target.name}")
    target = target.resolve()
    if os.path.normcase(str(source)) == os.path.normcase(str(target)):
        raise ValueError("输入文件与输出文件不能相同，请选择新的输出文件名。")
    if not source.is_file():
        raise FileNotFoundError(f"找不到输入文件：{source.name}")
    if source.suffix.lower() not in source_types:
        raise ValueError(f"不支持此输入格式：{source.suffix or '无扩展名'}")
    if target.suffix.lower() not in target_types:
        raise ValueError(f"不支持此输出格式：{target.suffix or '无扩展名'}")
    if os.path.lexists(target):
        raise FileExistsError(f"输出文件已存在，为保护原文件已停止：{target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return source, target


def _commit_output(staged: Path, target: Path) -> None:
    """Publish a complete file atomically, without an overwrite race."""
    if os.path.lexists(target):
        raise FileExistsError(f"输出文件已存在，为保护原文件已停止：{target.name}")
    if os.name == "nt":
        # Windows MoveFile (used by rename) refuses to replace an existing file.
        staged.rename(target)
    else:
        # POSIX rename replaces destinations; a hard link is the no-replace form.
        os.link(staged, target)
        staged.unlink()


def _validate_docx(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            required = {"[Content_Types].xml", "word/document.xml"}
            if not required.issubset(set(archive.namelist())) or archive.testzip() is not None:
                raise ValueError("Word 文档结构不完整。")
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        raise RuntimeError("转换结果不是完整的 Word 文档，未保存输出。") from exc


def _open_pdf(path: Path, password: str | None = None):
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(path), password=password or None)
        if len(document) < 1:
            document.close()
            raise ValueError("PDF 中没有可转换的页面。")
        return document
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == 4 or "password" in str(exc).lower():
            raise ValueError("PDF 密码错误或文件需要密码，请输入正确的打开密码。") from exc
        raise ValueError("无法读取 PDF，文件可能已损坏或并非有效 PDF。") from exc


def _validate_pdf(path: Path) -> int:
    try:
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("缺少 PDF 文件头。")
        with _open_pdf(path) as document:
            return len(document)
    except (OSError, ValueError) as exc:
        raise RuntimeError("转换结果不是完整的 PDF，未保存输出。") from exc


def _attach_windows_job(process: subprocess.Popen) -> Any:
    """Attach the still-suspended conversion process to its own Windows job.

    The process must not run before this assignment: otherwise a quickly spawned
    child could escape the job and keep writing its temporary profile later.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    handle = None
    try:
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            return None
        handle = kernel.OpenProcess(0x0100 | 0x0001 | 0x1000, False, process.pid)
        if not handle or not kernel.AssignProcessToJobObject(job, handle):
            return None
        result = (kernel, job)
        job = None
        return result
    finally:
        if handle:
            kernel.CloseHandle(handle)
        if job:
            kernel.CloseHandle(job)


def _resume_windows_process(process: subprocess.Popen) -> None:
    """Resume the primary thread after job assignment, using documented APIs."""
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    thread = None
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                break
            available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        if thread is None:
            raise RuntimeError("无法启动本次转换的隔离进程。")
        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if thread:
            kernel.CloseHandle(thread)
        kernel.CloseHandle(snapshot)


def _close_windows_job(job: Any, timeout: float = 10) -> None:
    """Stop this job's remaining children and wait until every child has exited.

    CloseHandle alone initiates asynchronous termination.  Waiting for the job's
    ActiveProcesses count to reach zero prevents profile-cleanup races.
    """
    if job is None:
        return
    import ctypes
    from ctypes import wintypes

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel, handle = job
    try:
        if not kernel.TerminateJobObject(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while True:
            accounting = Accounting()
            if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.ActiveProcesses == 0:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("本次转换的子进程未能及时退出，未保存输出，请稍后重试。")
            time.sleep(min(0.05, remaining))
    finally:
        kernel.CloseHandle(handle)


def _cleanup_conversion_directory(path: Path, parent: Path, timeout: float = 3) -> None:
    """Remove only our private directory, retrying transient Windows races."""
    resolved = path.resolve()
    if resolved.parent != parent.resolve() or not resolved.name.startswith((".qj-", ".qingjian-convert-")):
        raise ValueError("临时转换目录不在指定工作目录内，已停止清理。")
    filesystem_path: str | Path = resolved
    if os.name == "nt":
        native = str(resolved)
        # LibreOffice creates very long OpenCL-cache and extension-registry
        # names.  A normal-path walk can see these files but fail to unlink
        # them (WinError 3), leaving a non-empty directory behind (145).
        if not native.startswith("\\\\?\\"):
            filesystem_path = "\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native
    deadline = time.monotonic() + timeout
    while True:
        try:
            shutil.rmtree(filesystem_path)
            return
        except FileNotFoundError:
            if not resolved.exists():
                return
            raise
        except OSError as exc:
            # Do not retry/mask access-denied (5) or genuine permission errors.
            if getattr(exc, "winerror", None) not in {32, 33, 145}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.1, remaining))


@contextlib.contextmanager
def _conversion_workspace(parent: Path):
    # LibreOffice creates deep registry/cache paths even for a tiny input.
    # A quick conversion adds another staging directory beside the original;
    # keep the engine's private profile under the shorter standard temp root
    # on Windows. Complete output is still staged and committed beside target.
    runtime_parent = Path(parent).resolve()
    if os.name == "nt":
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if len(str(temporary_root)) < len(str(runtime_parent)):
            runtime_parent = temporary_root
    # Leave room for LibreOffice's own long cache/registry filenames even
    # when the user selected a nested runtime folder. Keep the chosen drive.
    path = Path(tempfile.mkdtemp(prefix=".qj-", dir=runtime_parent))
    try:
        yield path
    except BaseException as original:
        try:
            _cleanup_conversion_directory(path, runtime_parent)
        except OSError as cleanup_error:
            raise RuntimeError(f"{original}\n临时转换目录清理失败：{cleanup_error}") from original
        raise
    else:
        _cleanup_conversion_directory(path, runtime_parent)


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt" and process.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.communicate(timeout=5)


def _engine_profile_uri(profile: Path) -> str:
    """Use an existing Windows short-name alias without moving the profile.

    Some LibreOffice registry/cache paths include the encoded profile URL.
    A nested Chinese deployment folder can hit their limit even when its
    ordinary filesystem path fits. Volumes without short names fall back.
    """
    resolved = profile.resolve()
    if os.name == "nt":
        try:
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            function = kernel.GetShortPathNameW
            function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
            function.restype = ctypes.c_ulong
            buffer = ctypes.create_unicode_buffer(32768)
            length = function(str(resolved), buffer, len(buffer))
            if 0 < length < len(buffer):
                alias = Path(buffer.value)
                if alias.is_absolute() and len(alias.as_uri()) < len(resolved.as_uri()) and alias.samefile(resolved):
                    return alias.as_uri()  # resolve() here would expand the alias again
        except (AttributeError, OSError, ValueError):
            pass
    return resolved.as_uri()


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须为大于 0 的数值。")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须为大于 0 的数值。") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label}必须为大于 0 的数值。")
    return result


def convert_word(source: Path, target: Path, options: dict) -> dict:
    """Convert .doc/.docx/.rtf to .pdf/.docx with an isolated LibreOffice."""
    return _convert_office(source, target, options, {".doc", ".docx", ".rtf"}, {".pdf", ".docx"})


def convert_office_to_pdf(source: Path, target: Path, options: dict | None = None) -> dict:
    """Render a local office copy with Writer, Calc or Impress, without macros."""
    source = Path(source)
    if source.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
        from zipfile import ZipFile
        from xml.etree import ElementTree as ET
        with ZipFile(source) as package:
            entries = package.infolist()
            if len(entries) > 30000 or sum(item.file_size for item in entries) > 512 * 1024 * 1024:
                raise ValueError("Office文件展开过大，请缩小文件后转换。")
            for item in entries:
                name = item.filename.lower()
                if name.endswith("vbaproject.bin") or "/externallinks/" in name:
                    raise ValueError("该Office文件包含宏或外部数据链接，请先另存为不含这些内容的本地副本再转换。")
                if name.endswith(".rels"):
                    relations = ET.fromstring(package.read(item))
                    for relation in relations:
                        if relation.get("TargetMode", "").lower() == "external" and not relation.get("Type", "").endswith("/hyperlink"):
                            raise ValueError("文件包含外部图片或数据链接，请先将内容保存到文件内部再转换。")
    return _convert_office(source, target, options, {".doc", ".docx", ".rtf", ".xlsx", ".pptx"}, {".pdf"})


def _convert_office(source, target, options, source_types, target_types, *, filter_name=None, input_filter=None):
    options = options or {}
    source, target = _validate_paths(source, target, source_types, target_types)
    timeout = _positive_number(options.get("timeout", 120), "转换超时时间")
    selected = options.get("libreoffice_path")
    engine = next((path for path in _selected_engine_candidates(Path(str(selected).strip('"')).expanduser())
                   if path.is_file()), None) if selected else find_libreoffice()
    if engine is None or not engine.is_file():
        raise FileNotFoundError(
            "未找到 LibreOffice 转换引擎。请重新运行源码启动器补齐，"
            "或在设置中选择 LibreOffice 的 soffice 程序。"
        )
    warnings: list[str] = []
    staged_output: Path | None = None
    try:
        with _conversion_workspace(target.parent) as temporary_path:
            profile = temporary_path / "p"
            (profile / "user").mkdir(parents=True)
            (profile / "user" / "registrymodifications.xcu").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
                '<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>'
                '</item></oor:items>', encoding="utf-8")
            converted = temporary_path / "o"
            converted.mkdir()
            if filter_name is None:
                filter_name = "pdf:writer_pdf_Export" if target.suffix.lower() == ".pdf" else "docx:Office Open XML Text"
                if target.suffix.lower() == ".pdf" and source.suffix.lower() in {".xlsx", ".pptx"}:
                    filter_name = "pdf:" + ("calc_pdf_Export" if source.suffix.lower() == ".xlsx" else "impress_pdf_Export")
            command = [
                str(engine.resolve()), f"-env:UserInstallation={_engine_profile_uri(profile)}",
                "--headless", "--nologo", "--nodefault", "--norestore", "--nolockcheck",
                "--convert-to", filter_name, "--outdir", str(converted), str(source),
            ]
            if input_filter:
                command.insert(-1, "--infilter=" + input_filter)
            launch: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
            if os.name == "nt":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
                launch["startupinfo"] = startup
                # Assign the new process to its job before its first instruction
                # can launch soffice.bin or an extension-initialization helper.
                launch["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
            else:
                launch["start_new_session"] = True
            process = subprocess.Popen(command, **launch)
            job = None
            process_error = None
            try:
                job = _attach_windows_job(process)
                if os.name == "nt":
                    if job is None:
                        raise RuntimeError("无法隔离本次转换进程，已停止转换；请检查运行权限后重试。")
                    _resume_windows_process(process)
                try:
                    cancel = options.get("_cancel")
                    if hasattr(cancel, "is_set"):
                        cancel = cancel.is_set
                    if callable(cancel):
                        from .common import ensure_cancel
                        deadline = time.monotonic() + timeout
                        while True:
                            ensure_cancel(cancel)
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired(command, timeout)
                            try:
                                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                                break
                            except subprocess.TimeoutExpired:
                                if time.monotonic() >= deadline:
                                    raise
                    else:
                        stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    _terminate_process_tree(process)
                    raise TimeoutError(f"转换超过 {timeout:g} 秒，已结束本次转换进程；原文件未改动。") from exc
            except BaseException as exc:
                process_error = exc
                _terminate_process_tree(process)
                raise
            finally:
                try:
                    _close_windows_job(job)
                except Exception as close_error:
                    if process_error is not None:
                        raise RuntimeError(f"{process_error}\n隔离进程清理失败：{close_error}") from process_error
                    raise
            staged = converted / (source.stem + target.suffix.lower())
            if process.returncode != 0 or not staged.is_file():
                detail = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()[-1000:]
                raise RuntimeError("LibreOffice 转换失败，未生成输出。" + (f"\n{detail}" if detail else ""))
            changes = _validate_pdf(staged) if target.suffix.lower() == ".pdf" else 1
            if target.suffix.lower() == ".docx":
                _validate_docx(staged)
            if not staged.stat().st_size:
                raise RuntimeError("LibreOffice 生成了空文件，未保存输出。")
            descriptor, filename = tempfile.mkstemp(prefix=".qingjian-output-", suffix=target.suffix, dir=target.parent)
            os.close(descriptor)
            staged_output = Path(filename)
            shutil.copyfile(staged, staged_output)
        # Both the process tree and its writable profile are gone before the
        # complete result is atomically published to the user-visible filename.
        from .common import ensure_cancel
        cancel = options.get("_cancel")
        ensure_cancel(cancel.is_set if hasattr(cancel, "is_set") else cancel)
        _commit_output(staged_output, target)
        staged_output = None
    finally:
        if staged_output is not None:
            with contextlib.suppress(FileNotFoundError):
                staged_output.unlink()
    return {"changes": changes, "warnings": warnings}


def _configure_section(section, width: float, height: float, margin: float) -> None:
    from docx.enum.section import WD_ORIENT
    from docx.shared import Pt

    if width <= 0 or height <= 0:
        raise ValueError("PDF 页面大小无效。")
    section.orientation = WD_ORIENT.LANDSCAPE if width > height else WD_ORIENT.PORTRAIT
    section.page_width = Pt(width)
    section.page_height = Pt(height)
    section.left_margin = section.right_margin = Pt(margin)
    section.top_margin = section.bottom_margin = Pt(margin)
    section.header_distance = section.footer_distance = Pt(min(margin / 2, 7))


def _next_section(document, previous_image_paragraph):
    from docx.enum.section import WD_SECTION_START
    from docx.shared import Pt

    section = document.add_section(WD_SECTION_START.NEW_PAGE)
    break_paragraph = document.paragraphs[-1]
    if previous_image_paragraph is not None:
        # Put the section break on the image paragraph itself.  An extra empty
        # break paragraph can overflow an otherwise full image page in Word.
        properties = break_paragraph._p.get_or_add_pPr()
        previous_image_paragraph._p.get_or_add_pPr().append(properties.sectPr)
        break_paragraph._p.getparent().remove(break_paragraph._p)
    else:
        formatting = break_paragraph.paragraph_format
        formatting.space_before = formatting.space_after = Pt(0)
        formatting.line_spacing = Pt(1)
        for run in break_paragraph.runs:
            run.font.size = Pt(1)
    return section


def _render_page(pdf_page, path: Path, dpi: int) -> None:
    width, height = pdf_page.get_size()
    if width * height * (dpi / 72) ** 2 > 100_000_000:
        raise ValueError("此 PDF 页面过大，请降低图片清晰度后重试。")
    bitmap = pdf_page.render(scale=dpi / 72)
    try:
        image = bitmap.to_pil()
        try:
            rgb = image.convert("RGB")
            try:
                rgb.save(path, "PNG")
            finally:
                rgb.close()
        finally:
            image.close()
    finally:
        bitmap.close()


def _add_page_image(document, path: Path, section):
    from PIL import Image
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.shared import Pt

    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    formatting = paragraph.paragraph_format
    formatting.space_before = formatting.space_after = Pt(0)
    formatting.keep_together = True
    formatting.keep_with_next = False
    formatting.widow_control = False
    available_width = (section.page_width - section.left_margin - section.right_margin) / 12700
    available_height = (section.page_height - section.top_margin - section.bottom_margin) / 12700 - 2
    if available_width <= 0 or available_height <= 0:
        raise ValueError("PDF 页面过小，无法放入 Word 页面。")
    with Image.open(path) as image:
        scale = min(available_width / image.width, available_height / image.height)
        width, height = image.width * scale, image.height * scale
    formatting.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    formatting.line_spacing = Pt(height + 1)
    run = paragraph.add_run()
    run.font.size = Pt(1)
    run.add_picture(str(path), width=Pt(width), height=Pt(height))
    return paragraph


def _in_box(word: dict, box: tuple[float, float, float, float]) -> bool:
    midpoint_x = (float(word["x0"]) + float(word["x1"])) / 2
    midpoint_y = (float(word["top"]) + float(word["bottom"])) / 2
    return box[0] <= midpoint_x <= box[2] and box[1] <= midpoint_y <= box[3]


def _join_words(words: list[dict]) -> str:
    def cjk(character: str) -> bool:
        return "\u2e80" <= character <= "\u9fff" or "\uf900" <= character <= "\ufaff"

    result = ""
    previous = None
    for word in sorted(words, key=lambda item: item["x0"]):
        text = str(word.get("text", ""))
        if not text:
            continue
        if previous is not None:
            gap = float(word["x0"]) - float(previous["x1"])
            # Preserve spaces between Latin words, without inserting spaces
            # between Chinese characters that pdfplumber split into words.
            if not (cjk(result[-1]) and cjk(text[0]) and gap < 6):
                result += " "
        result += text
        previous = word
    return result


def _text_lines(words: list[dict]) -> list[tuple[float, float, str, Any]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda item: (round(float(item["top"]), 1), float(item["x0"]))):
        tolerance = max(2.0, (float(word["bottom"]) - float(word["top"])) * 0.25)
        if lines and abs(float(word["top"]) - float(lines[-1][0]["top"])) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [(float(line[0]["top"]), min(float(word["x0"]) for word in line), "text", _join_words(line)) for line in lines]


def _add_table(document, rows: list[list[Any]]) -> None:
    from docx.oxml import OxmlElement
    from docx.shared import Pt

    columns = max((len(row) for row in rows), default=0)
    if columns == 0:
        return
    table = document.add_table(rows=0, cols=columns)
    table.style = "Table Grid"
    for row_index, values in enumerate(rows):
        row = table.add_row()
        for column, cell in enumerate(row.cells):
            cell.text = str(values[column] or "") if column < len(values) else ""
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.line_spacing = 1.0
                for run in paragraph.runs:
                    run.font.size = Pt(9.5)
        if row_index == 0:
            header = OxmlElement("w:tblHeader")
            row._tr.get_or_add_trPr().append(header)


def _image_events(page, rendered: Path, temporary: Path, page_index: int, warnings: list[str]):
    from PIL import Image

    events: list[tuple[float, float, str, Any]] = []
    seen: set[tuple[float, ...]] = set()
    if not page.images:
        return events
    with Image.open(rendered) as image:
        scale_x, scale_y = image.width / page.width, image.height / page.height
        page_x, page_top = page.bbox[:2]
        for index, item in enumerate(page.images):
            box = (float(item["x0"]), float(item["top"]), float(item["x1"]), float(item["bottom"]))
            key = tuple(round(value, 1) for value in box)
            if key in seen or box[2] - box[0] < 2 or box[3] - box[1] < 2:
                continue
            seen.add(key)
            crop_box = (
                max(0, int((box[0] - page_x) * scale_x)),
                max(0, int((box[1] - page_top) * scale_y)),
                min(image.width, math.ceil((box[2] - page_x) * scale_x)),
                min(image.height, math.ceil((box[3] - page_top) * scale_y)),
            )
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                continue
            try:
                cropped = image.crop(crop_box)
                try:
                    path = temporary / f"page-{page_index + 1}-image-{index + 1}.png"
                    cropped.save(path, "PNG")
                finally:
                    cropped.close()
                events.append((box[1], box[0], "image", (path, box[2] - box[0], box[3] - box[1])))
            except (OSError, ValueError):
                warnings.append(f"第 {page_index + 1} 页的第 {index + 1} 张图片未能提取。")
    return events


def _add_inline_image(document, data, section) -> None:
    from docx.shared import Pt

    path, width, height = data
    available_width = (section.page_width - section.left_margin - section.right_margin) / 12700
    available_height = (section.page_height - section.top_margin - section.bottom_margin) / 12700 - 12
    scale = min(1.0, available_width / width, available_height / height)
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(4)
    paragraph.add_run().add_picture(str(path), width=Pt(width * scale), height=Pt(height * scale))


def pdf_to_word(source: Path, target: Path, options: dict) -> dict:
    """Convert a PDF to editable reflowed content or per-page appearance images.

    PDFium calls intentionally remain serial.  Callers should use one conversion
    worker rather than sharing PDFium objects across threads.
    """
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    options = options or {}
    from .common import ensure_cancel
    cancel = options.get("_cancel")
    cancel = cancel.is_set if hasattr(cancel, "is_set") else cancel
    ensure_cancel(cancel)
    source, target = _validate_paths(source, target, {".pdf"}, {".docx"})
    mode = options.get("pdf_mode", "text")
    if not isinstance(mode, str) or mode not in {"text", "appearance"}:
        raise ValueError("PDF 转 Word 模式必须为 text（可编辑）或 appearance（保留版式）。")
    dpi_value = _positive_number(options.get("dpi", 144), "图片清晰度")
    if dpi_value != int(dpi_value) or not 72 <= dpi_value <= 600:
        raise ValueError("图片清晰度必须为 72 至 600 的整数 DPI。")
    dpi = int(dpi_value)
    password = options.get("password")
    if password is not None and not isinstance(password, str):
        raise ValueError("PDF 密码必须为文字。")
    warnings: list[str] = []
    if mode == "text":
        warnings.append("可编辑模式按阅读顺序重排文字和表格，复杂版式可能需要手动调整。")
    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(3)
    fonts = normal.element.get_or_add_rPr().find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        normal.element.get_or_add_rPr().append(fonts)
    fonts.set(qn("w:eastAsia"), default_document_font("宋体"))
    with tempfile.TemporaryDirectory(prefix=".qingjian-convert-", dir=target.parent) as temporary:
        temporary_path = Path(temporary)
        with _open_pdf(source, password) as pdf:
            page_count = len(pdf)
            with contextlib.ExitStack() as stack:
                plumber = None
                if mode == "text":
                    import pdfplumber
                    try:
                        plumber = stack.enter_context(pdfplumber.open(source, password=password or None))
                    except Exception as exc:
                        # PDFium has already checked the supplied password; this
                        # branch reports an extraction-specific file error.
                        raise ValueError("无法提取此 PDF 的文字结构，请尝试保留版式模式。") from exc
                previous_image = None
                for index in range(page_count):
                    ensure_cancel(cancel)
                    section = document.sections[0] if index == 0 else _next_section(document, previous_image)
                    previous_image = None
                    pdf_page = pdf[index]
                    try:
                        width, height = pdf_page.get_size()
                        margin = min(14.1732 if mode == "appearance" else 28.3465, width / 10, height / 10)
                        _configure_section(section, width, height, margin)
                        rendered = temporary_path / f"page-{index + 1}.png"
                        if mode == "appearance":
                            _render_page(pdf_page, rendered, dpi)
                            previous_image = _add_page_image(document, rendered, section)
                            continue
                        page = plumber.pages[index]
                        # Match PDFium's visible CropBox.  pdfplumber otherwise
                        # extracts from the entire MediaBox, including hidden
                        # printer marks/text, and image crop coordinates drift.
                        crop = page.cropbox
                        visible_box = (
                            max(page.bbox[0], crop[0]), max(page.bbox[1], crop[1]),
                            min(page.bbox[2], crop[2]), min(page.bbox[3], crop[3]),
                        )
                        if visible_box != page.bbox and visible_box[2] > visible_box[0] and visible_box[3] > visible_box[1]:
                            page = page.crop(visible_box)
                        words = page.extract_words(x_tolerance=3, y_tolerance=3, keep_blank_chars=False)
                        if not any(str(word.get("text", "")).strip() for word in words):
                            # A scanned page cannot become editable without OCR.
                            _configure_section(section, width, height, min(14.1732, width / 10, height / 10))
                            _render_page(pdf_page, rendered, dpi)
                            previous_image = _add_page_image(document, rendered, section)
                            warnings.append(
                                f"第 {index + 1} 页未检测到可提取文字，已自动以整页图片保留；"
                                "如需编辑文字，请先进行 OCR。"
                            )
                            continue
                        tables = []
                        try:
                            for table in page.find_tables():
                                rows = table.extract()
                                if rows and any(any(str(cell or "").strip() for cell in row) for row in rows):
                                    tables.append((table.bbox, rows))
                        except Exception:
                            warnings.append(f"第 {index + 1} 页的表格未能识别，已保留其可提取文字。")
                        remaining_words = [word for word in words if not any(_in_box(word, box) for box, _ in tables)]
                        events = _text_lines(remaining_words)
                        events.extend((float(box[1]), float(box[0]), "table", rows) for box, rows in tables)
                        if page.images:
                            _render_page(pdf_page, rendered, dpi)
                            events.extend(_image_events(page, rendered, temporary_path, index, warnings))
                        for _, _, kind, data in sorted(events, key=lambda item: (item[0], item[1])):
                            if kind == "text":
                                document.add_paragraph(data)
                            elif kind == "table":
                                _add_table(document, data)
                            else:
                                _add_inline_image(document, data, section)
                    finally:
                        pdf_page.close()
        staged = temporary_path / "result.docx"
        document.save(staged)
        _validate_docx(staged)
        ensure_cancel(cancel)
        _commit_output(staged, target)
    return {"changes": page_count, "warnings": list(dict.fromkeys(warnings))}
