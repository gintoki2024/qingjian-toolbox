"""Source-only environment preparation. Imports here use the standard library.

Packages are installed into the selected Python's standard environment (or
its normal user site), so other scripts using it can reuse them. A complete
existing environment starts without network.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
DEPENDENCY_INSTALL_POLICY = "shared-interpreter"
PYTHON_FLAGS = ["-E", "-X", "utf8"]
MODULES = {
    "pyside6-essentials": ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"],
    "python-docx": ["docx"], "pillow": ["PIL.Image"], "pdfplumber": ["pdfplumber"],
    "pypdfium2": ["pypdfium2"], "requests": ["requests"], "beautifulsoup4": ["bs4"],
    "openpyxl": ["openpyxl"], "pypdf": ["pypdf"],
    "reportlab": ["reportlab.pdfgen.canvas", "reportlab.pdfbase.ttfonts"],
    "python-pptx": ["pptx"], "markdown-it-py": ["markdown_it"], "pillow-heif": ["pillow_heif"],
    "extract-msg": ["extract_msg"], "pyyaml": ["yaml"], "tomli-w": ["tomli_w"], "tomli": ["tomli"],
    "vobject": ["vobject"], "icalendar": ["icalendar"],
}
PROBE_CODE = r'''
import sys
sys.path[:] = [path for path in sys.path if path]
import importlib, importlib.metadata, json, site, struct
result = {"python": sys.executable, "version": list(sys.version_info[:3]),
          "bits": struct.calcsize("P") * 8, "packages": [],
          "user_site": site.getusersitepackages(), "user_enabled": site.ENABLE_USER_SITE is True}
for item in json.loads(sys.argv[1]):
    row = {"name": item["name"], "version": "", "ok": False, "error": ""}
    try:
        distribution = importlib.metadata.distribution(item["name"])
        row["version"] = distribution.version
        row["location"] = str(distribution.locate_file(""))
        for module in item["modules"]:
            importlib.import_module(module)
        row["ok"] = True
    except Exception as exc:
        row["error"] = str(exc)[:700]
    result["packages"].append(row)
print(json.dumps(result, ensure_ascii=True))
'''
PIP_PATH_CODE = r'''
import os, sys, site
sys.path[:] = [path for path in sys.path if path]
if os.name == "nt":
    def extended_path(path):
        path = os.path.abspath(path)
        if path.startswith("\\\\?\\"):
            return path
        return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path
    # Wheel RECORD entries may contain ../../Scripts and forward slashes.
    # Win32 extended paths do not normalize those; normalize only the extended
    # paths joined by this pip child, leaving ordinary path behavior unchanged.
    original_join = os.path.join
    def canonical_join(path, *parts):
        result = original_join(path, *parts)
        if isinstance(result, str) and result.startswith("\\\\?\\"):
            return os.path.normpath(result)
        return result
    os.path.join = canonical_join
    # This child process still installs into the standard user directory.
    # The Win32 extended spelling supports long wheel member names without
    # changing Windows policies, the user's Python configuration or scope.
    site.USER_BASE = extended_path(site.getuserbase())
    site.USER_SITE = extended_path(site.getusersitepackages())
    os.environ["PYTHONUSERBASE"] = site.USER_BASE
    import sysconfig
    sysconfig.get_config_vars()["userbase"] = site.USER_BASE
'''
PIP_CODE = PIP_PATH_CODE + "\nsys.path.insert(0,sys.argv[1]);from pip._internal.cli.main import main;raise SystemExit(main(sys.argv[2:]))"
INSTALL_SCOPE_CODE = r'''
import sys
sys.path[:] = [path for path in sys.path if path]
import json, os, site, sysconfig, tempfile
from pathlib import Path
def writable(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        with tempfile.TemporaryFile(dir=path):
            pass
        return True
    except OSError:
        return False
paths = sysconfig.get_paths()
normal = all(writable(paths[key]) for key in ("purelib", "platlib", "scripts"))
user_site = site.getusersitepackages()
virtual = sys.prefix != getattr(sys, "base_prefix", sys.prefix) or hasattr(sys, "real_prefix")
use_user = not virtual and (sys.argv[1] == "user" or not normal)
if use_user and site.ENABLE_USER_SITE is True and writable(user_site):
    result = {"scope": "user", "target": user_site}
elif normal and not use_user:
    result = {"scope": "environment", "target": paths["purelib"], "prefix": sys.prefix}
else:
    result = {"scope": "unwritable", "target": paths["purelib"]}
print(json.dumps(result))
'''


class BootstrapError(RuntimeError):
    pass


class DependencyResolutionError(BootstrapError):
    """pip could not find a compatible installable dependency set."""


def verified_bootstrap_wheels(project=PROJECT):
    """Expose only small, pinned pure-Python wheels, never a whole directory."""
    project = Path(project)
    manifest_path = project / "bootstrap-wheels.json"
    folder = project / "bootstrap_wheels"
    try:
        if manifest_path.stat().st_size > 131072:
            raise ValueError("组件清单过大")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        artifacts = manifest["artifacts"]
        if (manifest.get("schema_version") != 1 or not isinstance(artifacts, list)
                or not 1 <= len(artifacts) <= 16):
            raise ValueError("组件清单格式无效")
        if folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()):
            raise ValueError("组件目录不能为链接")
        verified, seen = [], set()
        for item in artifacts:
            name, size, expected = item["filename"], item["bytes"], item["sha256"]
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+-py3-none-any\.whl", name)
                    or name.casefold() in seen or type(size) is not int or not 0 < size <= 1048576
                    or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                raise ValueError("组件文件名、大小或校验值无效")
            path = folder / name
            if path.is_symlink() or path.resolve().parent != folder.resolve():
                raise ValueError("组件文件路径无效")
            if not path.is_file() or path.stat().st_size != size:
                raise ValueError("组件缺失或大小不匹配：" + name)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("组件SHA256不匹配：" + name)
            seen.add(name.casefold())
            verified.append(path.resolve())
        return verified
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BootstrapError("随包的轻量依赖校验失败，请完整解压新版源码包：" + str(exc)) from exc


def startup_selection(project=PROJECT):
    """Read a choice made by the native first-run dialog, without creating paths."""
    path = Path(project) / "config" / "startup-selection.json"
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > 16384:
            raise ValueError("配置过大")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if (value.get("schema_version") != 1 or value.get("mode") not in {"reuse", "managed"}
                or not isinstance(value.get("runtime_home"), str)
                or not Path(value["runtime_home"]).is_absolute()
                or (value["mode"] == "reuse" and (not isinstance(value.get("python"), str)
                    or not Path(value["python"]).is_absolute()))):
            raise ValueError("配置字段无效")
        return value
    except (OSError, ValueError, AttributeError) as exc:
        raise BootstrapError("启动位置配置无法读取，请用启动器的 --choose-environment 重新选择环境：" + str(path)) from exc


def source_launcher(project=PROJECT):
    project = Path(project).resolve()
    local = project / "启动青简工具箱.exe"
    if local.is_file():
        return local
    parent = project.parent / "启动青简工具箱.exe"
    if project.name == "程序文件" and parent.is_file():
        return parent
    return None


def runtime_home(value=None, project=PROJECT):
    value = value or os.environ.get("QINGJIAN_RUNTIME_HOME")
    if value:
        return Path(value).expanduser().absolute()
    selected = startup_selection(project)
    if selected:
        return Path(selected["runtime_home"])
    from startup_shared import default_runtime_home, saved_selection
    shared = saved_selection(project)
    if shared:
        return Path(shared['runtime_home'])
    # This is only a suggestion / headless default. Interactive first use asks
    # before creating it, and never silently allocates a runtime in AppData.
    return default_runtime_home(project)


def child_environment(home=None):
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(key, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # pip's isolated mode still reads machine-wide config. os.devnull is its
    # documented per-process switch to ignore all config files, preserving the
    # selected single index even on managed/company computers.
    env["PIP_CONFIG_FILE"] = os.devnull
    home = home or env.get("QINGJIAN_RUNTIME_HOME")
    if home:
        root = Path(home).absolute()
        env["QINGJIAN_RUNTIME_HOME"] = str(root)
        env["PIP_CACHE_DIR"] = str(root / "pip-cache")
        for key in ("TEMP", "TMP", "TMPDIR"):
            env[key] = str(root / "temp")
    return env


def requirements(project=PROJECT):
    values = []
    for line in (Path(project) / "requirements.txt").read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)((?:(?:>=|<=|==|!=|<|>)[0-9.]+)(?:,(?:>=|<=|==|!=|<|>)[0-9.]+)*)", line)
        if not match:
            raise BootstrapError("运行依赖清单格式不受支持，请使用随包的 requirements.txt。")
        name, spec = match.groups()
        key = name.lower().replace("_", "-")
        if key not in MODULES:
            raise BootstrapError("新增依赖缺少自检规则：" + name + "。请更新 bootstrap.py 的 MODULES。")
        values.append({"name": name, "spec": spec, "modules": MODULES[key]})
    if not values:
        raise BootstrapError("运行依赖清单为空，无法准备环境。")
    return values


def version_satisfies(value, spec):
    if not re.fullmatch(r"\d+(?:\.\d+)*(?:\.post\d+)?", str(value)):
        return False
    def parts(text):
        number = tuple(int(item) for item in text.split(".post", 1)[0].split("."))
        return number + (0,) * max(0, 6 - len(number))
    actual = parts(value)
    for condition in spec.split(","):
        match = re.fullmatch(r"(>=|<=|==|!=|<|>)([\d.]+)", condition)
        if not match:
            return False
        operator, requested = match.groups()
        expected = parts(requested)
        if not {">=": actual >= expected, "<=": actual <= expected, "==": actual == expected,
                "!=": actual != expected, "<": actual < expected, ">": actual > expected}[operator]:
            return False
    return True


def probe_python(executable, project=PROJECT):
    entries = requirements(project)
    try:
        process = subprocess.run([str(executable), *PYTHON_FLAGS, "-c", PROBE_CODE, json.dumps(entries)],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 env=child_environment(), timeout=90,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode != 0:
            raise BootstrapError((process.stderr or process.stdout)[-900:])
        result = json.loads(process.stdout.strip().splitlines()[-1])
        if not isinstance(result, dict) or len(result.get("packages", [])) != len(entries):
            raise ValueError("自检响应不完整")
        supported = result.get("bits") == 64 and (3, 10) <= tuple(result.get("version", [])[:2]) < (3, 15)
        missing = []
        for row, entry in zip(result["packages"], entries):
            row["ok"] = bool(row.get("ok")) and version_satisfies(row.get("version", ""), entry["spec"])
            row["required"] = entry["spec"]
            if not row["ok"]:
                missing.append(entry["name"])
        result.update(supported=supported, missing=missing, ready=supported and not missing)
        return result
    except (OSError, ValueError, IndexError, subprocess.SubprocessError, BootstrapError) as exc:
        return {"python": str(executable), "version": [], "bits": 0, "packages": [],
                "supported": False, "missing": [item["name"] for item in entries], "ready": False, "error": str(exc)}


def load_manifest(project=PROJECT):
    value = json.loads((Path(project) / "runtime-sources.json").read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise BootstrapError("下载来源清单格式无效。")
    for key in ("python", "pip", "libreoffice"):
        artifact = value.get(key, {})
        if (not re.fullmatch(r"[a-f0-9]{64}", artifact.get("sha256", ""))
                or not isinstance(artifact.get("bytes"), int) or artifact["bytes"] <= 0
                or not isinstance(artifact.get("filename"), str)
                or Path(artifact["filename"]).name != artifact["filename"]
                or any(character in artifact["filename"] for character in "\\/:")
                or not artifact.get("urls")):
            raise BootstrapError("组件下载信息不完整：" + key)
        for source in artifact["urls"]:
            validate_source(source)
    if not value.get("pip_indexes"):
        raise BootstrapError("缺少依赖下载源。")
    for source in value["pip_indexes"]:
        validate_source(source)
    return value


def validate_source(source):
    if not isinstance(source, dict) or not isinstance(source.get("name"), str):
        raise BootstrapError("下载源格式无效。")
    parsed = urllib.parse.urlsplit(source.get("url", ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BootstrapError("下载源必须使用不含凭据的 HTTPS 地址。")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def filesystem_path(path):
    """Allow deep wheel data paths without changing Windows long-path policy."""
    value = str(Path(path).absolute())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return value


def download_artifact(artifact, folder, emit):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / artifact["filename"]
    if target.is_file() and target.stat().st_size == artifact["bytes"] and digest(target) == artifact["sha256"]:
        emit({"event": "log", "message": "使用已校验的下载缓存：" + target.name})
        return target
    errors = []
    for source_index, source in enumerate(artifact["urls"]):
        validate_source(source)
        staged = folder / (".download-" + uuid.uuid4().hex + ".part")
        try:
            emit({"event": "progress", "message": "正在下载运行组件", "source": source["name"], "progress": 0})
            request = urllib.request.Request(source["url"], headers={"User-Agent": "QingjianSourceBootstrap/1"})
            with urllib.request.urlopen(request, timeout=25) as response, staged.open("xb") as stream:
                if urllib.parse.urlsplit(response.url).scheme != "https":
                    raise BootstrapError("下载重定向不是HTTPS，已拒绝。")
                size, last = 0, -1
                while block := response.read(256 * 1024):
                    size += len(block)
                    if size > artifact["bytes"]:
                        raise BootstrapError("下载大小超出组件清单。")
                    stream.write(block)
                    progress = int(100 * size / artifact["bytes"])
                    if progress != last:
                        emit({"event": "progress", "message": "正在下载运行组件", "source": source["name"], "progress": progress})
                        last = progress
            if size != artifact["bytes"] or digest(staged) != artifact["sha256"]:
                raise BootstrapError("组件校验失败，未使用该下载。")
            os.replace(staged, target)
            return target
        except (OSError, ValueError, BootstrapError) as exc:
            errors.append(source["name"] + "：" + str(exc))
            suffix = "暂不可用，尝试备用源。" if source_index + 1 < len(artifact["urls"]) else "未完成下载，已没有其他备用源。"
            emit({"event": "log", "message": source["name"] + suffix})
        finally:
            staged.unlink(missing_ok=True)
    raise BootstrapError("所有下载源均未成功。请检查网络后重试。\n" + "\n".join(errors))


@contextlib.contextmanager
def preparation_lock(home, wait_seconds=900):
    """OS locks are released even if the launcher cancels the worker."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    path = home / "prepare.lock"
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BootstrapError("另一个启动窗口正在准备环境，请稍后重试。")
                time.sleep(.2)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run_process(command, emit, log_path, *, timeout=1800):
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([str(value) for value in command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding="utf-8", errors="replace", env=child_environment(log_path.parent.parent),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               start_new_session=os.name != "nt")
    lines = queue.Queue()
    def read_lines():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)
    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    deadline, ended = time.monotonic() + timeout, False
    output_tail = deque(maxlen=80)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            while not ended:
                if time.monotonic() >= deadline:
                    raise BootstrapError("环境准备超时，可检查日志后重试：" + str(log_path))
                try:
                    line = lines.get(timeout=.2)
                except queue.Empty:
                    continue
                if line is None:
                    ended = True
                else:
                    output_tail.append(line)
                    log.write(line)
                    log.flush()
                    emit({"event": "log", "detail": "pip", "message": line.strip()[:1000]})
        code = process.wait(timeout=15)
        if code:
            tail = "".join(output_tail)
            if "ResolutionImpossible" in tail or "conflicting dependencies" in tail:
                conflicts = list(dict.fromkeys(re.findall(r"depends on ([^\r\n]+)", tail)))
                detail = "；".join(conflicts)[:500] or "版本约束冲突，或当前Python/平台缺少可安装的发行包"
                raise DependencyResolutionError("组件依赖解析失败：" + detail + "。详细日志：" + str(log_path))
            raise BootstrapError("组件安装未成功，详细日志：" + str(log_path))
    finally:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                               timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
            if process.poll() is None and os.name != "nt":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
        process.stdout.close()


def environment_stamp(project, python):
    value = hashlib.sha256(str(Path(project).resolve()).encode("utf-8"))
    value.update(str(Path(python).absolute()).casefold().encode("utf-8"))
    for name in ("requirements.txt", "runtime-sources.json", "bootstrap.py", "startup_native.py", "startup_shared.py", "native-sources.json", "bootstrap-wheels.json"):
        value.update((Path(project) / name).read_bytes())
    return value.hexdigest()


def ready_environment(project, python, home, engine, native_components=None):
    from startup_native import ENVIRONMENT_KEY, check_native
    if native_components is None:
        native = check_native(project, home)
        if not native['native_ready']:
            raise BootstrapError('原生转换组件未准备完整：' + '、'.join(native['native_missing']))
        native_components = native['native_components']
    root = Path(engine)
    engine_exe = next((root / "program" / name for name in ("soffice.com", "soffice.exe")
                       if (root / "program" / name).is_file()), None)
    if engine_exe is None:
        raise BootstrapError("文档转换引擎不完整。")
    from startup_shared import remember_runtime
    selected = startup_selection(project)
    mode = selected['mode'] if selected else 'reuse'
    if not Path(python).resolve().is_relative_to(Path(home).resolve()):
        mode = 'reuse'
    remember_runtime(project, python, home, root, native_components, mode=mode)
    temporary = str(Path(home).absolute() / "temp")
    return {"QINGJIAN_BOOTSTRAP_READY": environment_stamp(project, python),
            "QINGJIAN_RUNTIME_HOME": str(Path(home).absolute()), "QINGJIAN_LIBREOFFICE": str(engine_exe.resolve()),
            ENVIRONMENT_KEY: json.dumps(native_components, ensure_ascii=False),
            "TEMP": temporary, "TMP": temporary, "TMPDIR": temporary,
            "PIP_CACHE_DIR": str(Path(home).absolute() / "pip-cache")}


def selected_interpreter(project=PROJECT, home=None):
    """Resolve saved reuse/managed choices without discovering another Python."""
    selected = startup_selection(project)
    if not selected:
        from startup_shared import saved_selection
        selected = saved_selection(project)
    if not selected:
        return Path(sys.executable)
    if selected["mode"] == "reuse":
        return Path(selected["python"])
    home = runtime_home(home, project).resolve()
    candidates = []
    cached = home / "python-selection.json"
    try:
        if cached.is_file() and cached.stat().st_size < 8192:
            candidate = Path(json.loads(cached.read_text(encoding="utf-8-sig"))["python"])
            if candidate.is_absolute() and candidate.resolve().is_relative_to(home):
                candidates.append(candidate)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    spec = load_manifest(project)["python"]
    candidates += [home / ("python-" + spec["version"]) / "python.exe",
                   home / ("python-" + spec["version"] + "-" + spec["sha256"][:12]) / "python.exe"]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def check_environment(project=PROJECT, python=None, home=None):
    from startup_engine import find_engine
    from startup_native import check_native
    project, home = Path(project), runtime_home(home, project)
    manifest = load_manifest(project)
    python = Path(python) if python else selected_interpreter(project, home)
    if python is None:
        return {"python": "", "ready": False, "supported": False, "missing": [], "engine_ready": False,
                "runtime_home": str(home), "dependency_install_policy": DEPENDENCY_INSTALL_POLICY,
                "message": "所选位置尚未部署 Python，请使用启动器准备。"}
    result = probe_python(python, project)
    result.update(check_native(project, home))
    engine = find_engine(project, home, manifest["libreoffice"]["version"])
    result.update(engine=str(engine) if engine else "", engine_ready=engine is not None,
                  dependency_install_policy=DEPENDENCY_INSTALL_POLICY, runtime_home=str(home),
                  configuration_required=startup_selection(project) is None and not os.environ.get("QINGJIAN_RUNTIME_HOME"))
    from startup_shared import needs_preservation
    preservation = bool(engine and needs_preservation(engine, project, home)) or any(
        needs_preservation(root, project, home) for root in result['native_components'].values())
    result['preservation_required'] = preservation
    result["ready"] = result["ready"] and result["engine_ready"] and result['native_ready'] and not preservation
    return result


def _owned_remove(path, parent):
    path, parent = Path(path).absolute(), Path(parent).resolve()
    if (not path.resolve().is_relative_to(parent) or path.parent.resolve() != parent or path.is_symlink()
            or not (path / ".qingjian-owned").is_file() or not path.name.startswith(".env-")):
        raise BootstrapError("临时环境目录不属于本工具，已停止清理。")
    shutil.rmtree(filesystem_path(path))


def installation_scope(python, probe):
    """Choose a normal pip destination without elevation or a project venv."""
    user_site = str(probe.get("user_site", ""))
    prefer_user = False
    if user_site and probe.get("user_enabled"):
        for row in probe.get("packages", []):
            if row.get("ok") or not row.get("location"):
                continue
            try:
                if Path(row["location"]).resolve().is_relative_to(Path(user_site).resolve()):
                    prefer_user = True
            except (OSError, ValueError):
                pass
    try:
        result = subprocess.run([str(python), *PYTHON_FLAGS, "-c", INSTALL_SCOPE_CODE,
                                 "user" if prefer_user else "environment"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env=child_environment(), timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        scope = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else {}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        raise BootstrapError("无法检查所选 Python 的安装目录：" + str(exc)) from exc
    if scope.get("scope") not in {"environment", "user"} or not scope.get("target"):
        raise BootstrapError("所选 Python 的组件目录没有写入权限，且不能使用标准用户组件目录。"
                             "请通过 --python 指定一个可写的 Python 环境：" + str(python))
    return scope


def pip_command(python, manifest, home, emit):
    """Reuse the selected interpreter's pip; a verified wheel is a fallback."""
    prefix = [str(python), *PYTHON_FLAGS, "-c"]
    code = PIP_PATH_CODE + "\nfrom pip._internal.cli.main import main;raise SystemExit(main(sys.argv[1:]))"
    try:
        result = subprocess.run([*prefix, code, "--isolated", "--version"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env=child_environment(), timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            return [*prefix, code]
    except (OSError, subprocess.SubprocessError):
        pass
    wheel = download_artifact(manifest["pip"], Path(home) / "downloads", emit)
    return [*prefix, PIP_CODE, str(wheel)]


def install_missing_packages(python, project, home, manifest, probe, emit):
    if probe["ready"]:
        return
    wheels = verified_bootstrap_wheels(project)
    scope = installation_scope(python, probe)
    emit({"event": "log", "message": "依赖将补充到所选 Python，可供使用同一环境的程序共用。",
          "python": str(python), "install_scope": scope["scope"], "install_target": scope["target"]})
    command = pip_command(python, manifest, home, emit)
    entries = requirements(project)
    errors, resolution_failures = [], 0
    for index, source in enumerate(manifest["pip_indexes"]):
        if probe["ready"]:
            return
        wanted = [entry["name"] + entry["spec"] for entry in entries if entry["name"] in probe["missing"]]
        options = ["--isolated", "install", "--disable-pip-version-check", "--no-input", "--timeout", "20",
                   "--retries", "1", "--only-binary=:all:", "--index-url", source["url"],
                   "--no-warn-script-location", "--cache-dir", str(Path(home) / "pip-cache"),
                   "--constraint", str(Path(project) / "requirements.txt")]
        for wheel in wheels:
            options.extend(["--find-links", wheel.as_uri()])
        if scope["scope"] == "user":
            options.append("--user")
        elif os.name == "nt" and scope.get("prefix"):
            # The destination is unchanged; only its Windows path spelling is
            # extended. This also covers explicitly selected deep venv paths.
            options.extend(["--prefix", filesystem_path(scope["prefix"])])
        emit({"event": "progress", "message": "正在补齐本地 Python 组件", "source": source["name"],
              "python": str(python), "packages": list(probe["missing"])})
        try:
            run_process([*command, *options, *wanted], emit, Path(home) / "logs" / ("pip-" + str(index) + ".log"))
            probe = probe_python(python, project)
            # A package with valid metadata but a broken import needs repair;
            # do not reinstall its already-satisfied dependency tree.
            broken = [entry["name"] + entry["spec"] for entry in entries
                      for row in probe.get("packages", []) if row["name"] == entry["name"]
                      and not row["ok"] and version_satisfies(row.get("version", ""), entry["spec"])]
            if broken:
                run_process([*command, *options, "--force-reinstall", "--no-deps", *broken], emit,
                            Path(home) / "logs" / ("pip-repair-" + str(index) + ".log"))
                probe = probe_python(python, project)
            if not probe["ready"]:
                raise BootstrapError("安装后自检未通过，仍需处理：" + "、".join(probe["missing"]))
            return
        except (OSError, BootstrapError) as exc:
            resolution_failures += isinstance(exc, DependencyResolutionError)
            errors.append(source["name"] + "：" + str(exc))
            # A failed command may have installed some packages. Recheck so a
            # mirror retry only requests what remains missing.
            probe = probe_python(python, project)
            if probe["ready"]:
                return
            if index + 1 < len(manifest["pip_indexes"]):
                emit({"event": "log", "message": source["name"] + "未完成安装，正在切换备用镜像。"})
    if errors and resolution_failures == len(errors):
        raise DependencyResolutionError("本地 Python 组件补充未完成：所有来源均出现依赖解析失败。"
                                        "请查看下列冲突详情，并使用修复后的源码包重试。\n" + "\n".join(errors))
    raise BootstrapError("本地 Python 组件补充未完成。请按下列具体原因处理后重试。\n" + "\n".join(errors))


def prepare_environment(project=PROJECT, python=None, home=None, emit=lambda value: None, *, offline=False):
    from startup_engine import ensure_engine
    from startup_native import ensure_native
    project, home = Path(project).resolve(), runtime_home(home, project)
    manifest = load_manifest(project)
    python = Path(python or sys.executable).absolute()
    with preparation_lock(home):
        (home / "temp").mkdir(exist_ok=True)
        emit({"event": "progress", "message": "正在检查本地 Python 与运行组件", "python": str(python)})
        probe = probe_python(python, project)
        if not probe["supported"]:
            raise BootstrapError("需要 Python 3.10–3.14 的64位环境。请双击“启动青简工具箱.exe”自动准备专用Python。")
        if not probe["ready"]:
            if offline:
                raise BootstrapError("当前 Python 缺少运行组件，离线模式未下载。请联网启动一次，或使用完整便携版。")
            install_missing_packages(python, project, home, manifest, probe, emit)
        engine = ensure_engine(project, home, manifest, emit, offline=offline)
        native_components = ensure_native(project, home, emit, offline=offline)
        environment = ready_environment(project, python, home, engine, native_components)
        result = {"event": "ready", "message": "环境已就绪，正在打开青简工具箱", "python": str(python), "environment": environment}
        emit(result)
        return result


def start_source(project=PROJECT, argv=None):
    """Return True for an already-ready interpreter; otherwise hand off once."""
    project, argv = Path(project).resolve(), list(argv if argv is not None else sys.argv[1:])
    if os.environ.get("QINGJIAN_BOOTSTRAP_READY") == environment_stamp(project, sys.executable):
        return True
    # CMD distributions run transparent source through Python. Direct main.py
    # invocation must keep that route rather than handing back to a legacy EXE.
    marker = project / 'cmd-package.json'
    if marker.is_file():
        try:
            if marker.stat().st_size > 4096:
                raise ValueError('marker too large')
            mode = json.loads(marker.read_text(encoding='utf-8'))
            if not isinstance(mode, dict) or mode.get('schema_version') != 1 or mode.get('mode') not in {'source', 'offline'}:
                raise ValueError('unsupported CMD package mode')
        except (OSError, ValueError, TypeError) as exc:
            raise BootstrapError('CMD包启动标记无效，请完整解压后重试。') from exc
        if mode['mode'] == 'offline':
            python = project / 'Python/python.exe'
            entry = project / 'launcher/offline_cmd.py'
            arguments = ['--', *argv]
        else:
            python = Path(sys.executable)
            entry = project / 'launcher/cmd_source.py'
            arguments = ['--project', str(project), '--', *argv]
        if not python.is_file() or not entry.is_file():
            raise BootstrapError('CMD包启动文件缺失，请完整解压后重试。')
        code = subprocess.call([str(python), *PYTHON_FLAGS, '-B', str(entry), *arguments], cwd=project,
                               env=child_environment())
        if code:
            raise BootstrapError('CMD源码启动未完成，退出码：' + str(code))
        return False
    selected = startup_selection(project)
    launcher = source_launcher(project)
    explicit_home = os.environ.get("QINGJIAN_RUNTIME_HOME")
    if selected is None and not explicit_home:
        if launcher:
            subprocess.Popen([str(launcher), "--choose-environment", "--python", sys.executable, "--", *argv],
                             cwd=project, env=child_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return False
        raise BootstrapError("首次启动需要选择 Python 与部署位置。请完整解压源码包并使用启动器；"
                             "命令行也可先设置 QINGJIAN_RUNTIME_HOME 为明确选定的运行目录。")
    home = runtime_home(project=project)
    # A remembered interpreter choice takes precedence over a different Python
    # used to invoke main.py. Handoff through the same launcher preserves the
    # managed-runtime recovery and shared-interpreter policy.
    selected_python = selected.get("python", "") if selected else ""
    needs_handoff = bool(selected and (selected["mode"] == "managed" or
                            Path(selected_python).absolute() != Path(sys.executable).absolute()))
    if needs_handoff and launcher:
        subprocess.Popen([str(launcher), "--runtime-home", str(home), "--", *argv], cwd=project,
                         env=child_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return False
    if needs_handoff:
        raise BootstrapError("启动器缺失，无法打开已选择的 Python 环境。请完整解压源码包。")
    checked = check_environment(project, home=home)
    if checked["ready"]:
        (home / "temp").mkdir(parents=True, exist_ok=True)
        environment = ready_environment(project, sys.executable, home, checked["engine"], checked.get('native_components'))
        if sys.flags.no_user_site or sys.flags.isolated or sys.flags.no_site:
            env = child_environment()
            env.update(environment)
            subprocess.Popen([sys.executable, *PYTHON_FLAGS, str(project / "main.py"), *argv],
                             cwd=project, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return False
        os.environ.update(environment)
        return True
    if launcher:
        subprocess.Popen([str(launcher), "--python", sys.executable, "--runtime-home", str(home), "--", *argv],
                         cwd=project, env=child_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return False
    def emit(value):
        print(value.get("message", ""), flush=True)
    ready = prepare_environment(project, home=home, emit=emit)
    env = child_environment()
    env.update(ready["environment"])
    subprocess.Popen([ready["python"], *PYTHON_FLAGS, str(project / "main.py"), *argv], cwd=project, env=env,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return False


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="青简源码环境自检与准备")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--runtime-home")
    parser.add_argument("--python")
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--events", type=Path)
    args = parser.parse_args(argv)
    def emit(value):
        text = json.dumps(value, ensure_ascii=False)
        print(text, flush=True)
        if args.events:
            args.events.parent.mkdir(parents=True, exist_ok=True)
            with args.events.open("a", encoding="utf-8") as stream:
                stream.write(text + "\n")
    try:
        if args.check_only:
            checked = check_environment(python=args.python, home=args.runtime_home)
            emit({"event": "check", **checked})
            return 0 if checked["ready"] else 1
        selected = startup_selection()
        if not args.runtime_home and not os.environ.get("QINGJIAN_RUNTIME_HOME") and selected is None:
            launcher = source_launcher()
            if launcher and not args.no_ui:
                subprocess.Popen([str(launcher), "--choose-environment", "--python", args.python or sys.executable,
                                  "--prepare-only"], cwd=PROJECT, env=child_environment(),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                emit({"event": "selection", "message": "请先在启动窗口选择 Python 与部署位置，确认后再开始下载。"})
                return 0
            raise BootstrapError("尚未选择部署位置。请运行启动器，或明确指定 --runtime-home 后再准备环境。")
        python = Path(args.python) if args.python else selected_interpreter(home=args.runtime_home)
        if python is None:
            raise BootstrapError("所选位置尚未部署 Python，请使用启动器自动准备。")
        prepare_environment(python=python, home=args.runtime_home, emit=emit, offline=args.offline)
        return 0
    except Exception as exc:
        emit({"event": "error", "message": str(exc)})
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT))
    raise SystemExit(main())
