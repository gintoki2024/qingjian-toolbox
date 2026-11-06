"""macOS source preparation, using only the Python standard library before Qt.

The selected interpreter owns its normal packages. Portable fallback Python and
conversion engines live outside the source release, at the selected location.
No system installation, sudo, global pip configuration, or security changes.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

import bootstrap as common

PROJECT = Path(__file__).resolve().parent
DEPENDENCY_INSTALL_POLICY = "shared-interpreter"
BootstrapError = common.BootstrapError
IDENTITY_CODE = r'''
import json, platform, sys, sysconfig
from pathlib import Path
print(json.dumps({'python':sys.executable,'prefix':sys.prefix,'base_prefix':sys.base_prefix,
 'platform':sys.platform,'architecture':platform.machine(),'version':list(sys.version_info[:3]),
 'externally_managed':sys.prefix==sys.base_prefix and
 (Path(sysconfig.get_path('stdlib'))/'EXTERNALLY-MANAGED').is_file()}))
'''


def architecture(value=None):
    value = value or platform.machine()
    value = {"aarch64": "arm64", "AMD64": "x86_64"}.get(value, value)
    if value not in {"arm64", "x86_64"}:
        raise BootstrapError("暂不支持此 Mac 处理器：" + str(value))
    return value


def absolute_path(value, label):
    path = Path(value).expanduser()
    if not path.is_absolute() or "\x00" in str(path):
        raise BootstrapError(label + "必须是绝对路径。")
    return path.absolute()


def catalog_path():
    explicit = os.environ.get("QINGJIAN_MAC_SELECTION")
    if explicit:
        return absolute_path(explicit, "环境位置记录")
    data = os.environ.get("QINGJIAN_USER_DATA")
    root = absolute_path(data, "用户数据位置") if data else Path.home() / "Library/Application Support/QingjianLocal"
    return root / "macos-startup-selection.json"


def read_json(path, maximum=32768):
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def saved_selection():
    row = read_json(catalog_path())
    if (not row or row.get("schema_version") != 1 or row.get("mode") not in {"managed", "reuse"}
            or row.get("architecture") != architecture()):
        return None
    try:
        row["runtime_home"] = str(absolute_path(row["runtime_home"], "组件位置"))
        row["python"] = str(absolute_path(row["python"], "Python 位置"))
    except (KeyError, TypeError, BootstrapError):
        return None
    return row


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    try:
        with staged.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        staged.chmod(0o600)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def remember(selection):
    atomic_json(catalog_path(), {"schema_version": 1, "architecture": architecture(),
                               "mode": selection["mode"], "python": str(selection["python"]),
                               "runtime_home": str(selection["runtime_home"])})


def load_manifest(project=PROJECT):
    row = read_json(Path(project) / "macos-runtime-sources.json", 131072)
    if not row or row.get("schema_version") != 1:
        raise BootstrapError("Mac 环境下载清单无效，请完整解压源码包。")
    for item in [row.get("pip", {}), *row.get("python", {}).values()]:
        if (not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))
                or type(item.get("bytes")) is not int or item["bytes"] <= 0
                or not re.fullmatch(r"[A-Za-z0-9_.+%-]+", item.get("filename", ""))
                or not isinstance(item.get("urls"), list) or not item["urls"]):
            raise BootstrapError("Mac 环境组件信息不完整。")
        for source in item["urls"]:
            common.validate_source(source)
    if set(row.get("python", {})) != {"arm64", "x86_64"} or not row.get("pip_indexes"):
        raise BootstrapError("Mac 环境清单缺少处理器或镜像信息。")
    for source in row["pip_indexes"]:
        common.validate_source(source)
    return row


def identity(python, *, isolated=False):
    try:
        flags = ["-I", "-X", "utf8"] if isolated else common.PYTHON_FLAGS
        process = subprocess.run([str(python), *flags, "-c", IDENTITY_CODE],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 env=common.child_environment(), timeout=30)
        value = json.loads(process.stdout.strip().splitlines()[-1]) if process.returncode == 0 else {}
        value["supported"] = (value.get("platform") == "darwin"
                              and architecture(value.get("architecture", "unknown")) == architecture()
                              and (3, 10) <= tuple(value.get("version", [])[:2]) < (3, 15))
        return value
    except (OSError, ValueError, IndexError, subprocess.SubprocessError, BootstrapError):
        return {"supported": False}


def safe_extract_python(archive, destination):
    """Extract only a fixed, verified archive; reject escaping links and devices."""
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        total = 0
        seen = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or not path.parts or path.parts[0] != "python"
                    or ".." in path.parts or "\\" in member.name or "\x00" in member.name
                    or member.name in seen or not (member.isdir() or member.isfile() or member.issym() or member.islnk())):
                raise BootstrapError("Python 压缩包包含不安全路径或文件类型。")
            seen.add(member.name)
            total += member.size
            if total > 1024 ** 3 or len(members) > 50000:
                raise BootstrapError("Python 压缩包超出允许大小。")
            if member.issym() or member.islnk():
                if member.linkname.startswith("/") or "\\" in member.linkname:
                    raise BootstrapError("Python 压缩包包含外部链接。")
                target = posixpath.normpath(posixpath.join(str(path.parent), member.linkname)
                                           if member.issym() else member.linkname)
                if target != "python" and not target.startswith("python/"):
                    raise BootstrapError("Python 压缩包包含越界链接。")
        # Creating directories/regular files first prevents writing through an
        # archive symlink; links are installed only after ordinary members.
        for member in members:
            target = root.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open("xb") as out:
                    shutil.copyfileobj(src, out)
                target.chmod(member.mode & 0o777)
        for member in members:
            target = root.joinpath(*PurePosixPath(member.name).parts)
            if member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            elif member.islnk():
                linked = root.joinpath(*PurePosixPath(member.linkname).parts)
                if not linked.is_file() or linked.is_symlink():
                    raise BootstrapError("Python 压缩包硬链接目标无效。")
                os.link(linked, target)
    return root / "python"


def managed_python(project, home, emit=lambda event: None, *, offline=False):
    spec = load_manifest(project)["python"][architecture()]
    target = Path(home) / "python" / architecture()
    executable = target / "bin/python3"
    receipt = read_json(target / ".qingjian-runtime.json")
    if (receipt and receipt.get("sha256") == spec["sha256"] and receipt.get("size") == spec["bytes"]
            and receipt.get("architecture") == architecture() and executable.is_file()
            and identity(executable).get("supported")):
        return executable
    if offline:
        raise BootstrapError("所选位置的 Mac Python 缺失或损坏；离线模式未下载。")
    if target.exists():
        raise BootstrapError("所选 Python 目录不完整，已保留原文件。请更换部署位置，或先备份该目录后重试：" + str(target))
    archive = common.download_artifact(spec, Path(home) / "downloads", emit)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".python-stage-", dir=parent) as temporary:
        source = safe_extract_python(archive, temporary)
        atomic_json(source / ".qingjian-runtime.json", {
            "kind": "python-build-standalone", "architecture": architecture(), "version": spec["version"],
            "sha256": spec["sha256"], "size": spec["bytes"], "source": spec["hash_source"]})
        if not identity(source / "bin/python3").get("supported"):
            raise BootstrapError("下载的 Python 无法在当前 Mac 运行，未启用。")
        os.replace(source, target)
    return executable


def default_home(project=PROJECT):
    return Path(project).resolve().parent.parent / "青简共享环境"


def offline_package(project=PROJECT):
    value = read_json(Path(project) / "macos-package.json")
    return bool(value and value.get("schema_version") == 1 and value.get("mode") == "offline")


def environment_stamp(project, python):
    value = hashlib.sha256(str(Path(project).resolve()).encode("utf-8"))
    value.update(str(Path(python).absolute()).encode("utf-8"))
    for name in ("requirements.txt", "bootstrap.py", "bootstrap_macos.py", "macos-runtime-sources.json",
                 "startup_macos_native.py", "macos-native-sources.json", "bootstrap-wheels.json"):
        value.update((Path(project) / name).read_bytes())
    return value.hexdigest()


def checked_python(python, project):
    isolated = offline_package(project)
    who = identity(python, isolated=isolated)
    if isolated:
        runtime = (Path(project) / "Python").resolve()
        try:
            owned = (Path(python).resolve().is_relative_to(runtime)
                     and Path(who["python"]).resolve().is_relative_to(runtime)
                     and Path(who["prefix"]).resolve() == runtime)
        except (KeyError, OSError, ValueError):
            owned = False
        if not owned:
            who = {**who, "supported": False, "error": "完整包只能使用包内 Python。"}
    if not who.get("supported"):
        return {"ready": False, "supported": False, "missing": [e["name"] for e in common.requirements(project)], "identity": who}
    return {**common.probe_python(python, project, isolated=isolated), "identity": who}


def check_environment(project=PROJECT, python=None, home=None):
    from startup_macos_native import check_native
    selected = {} if offline_package(project) else (saved_selection() or {})
    home = absolute_path(home or os.environ.get("QINGJIAN_RUNTIME_HOME")
                         or selected.get("runtime_home") or default_home(project), "组件位置")
    default_python = Path(project) / "Python/bin/python3" if offline_package(project) else sys.executable
    python = absolute_path(python or selected.get("python") or default_python, "Python 位置")
    probe = checked_python(python, project)
    try:
        native = check_native(Path(project), home)
    except RuntimeError as exc:
        raise BootstrapError("Mac 转换组件检查未完成：" + str(exc)) from exc
    return {"ready": bool(probe["ready"] and native["ready"]), "python": str(python),
            "runtime_home": str(home), "probe": probe, "native": native}


def ready_environment(project, python, home, native):
    env = common.child_environment(home)
    env.update(native["env"])
    env["QINGJIAN_BOOTSTRAP_READY"] = environment_stamp(project, python)
    env["QINGJIAN_RUNTIME_HOME"] = str(home)
    return env


def prepare_environment(project=PROJECT, python=None, home=None, emit=lambda event: None, *,
                        offline=False, managed=False):
    from startup_macos_native import ensure_native
    project = Path(project).resolve()
    home = absolute_path(home or default_home(project), "组件位置")
    offline = offline or os.environ.get("QINGJIAN_OFFLINE") == "1" or offline_package(project)
    manifest = load_manifest(project)
    with common.preparation_lock(home):
        (home / "temp").mkdir(exist_ok=True)
        python = managed_python(project, home, emit, offline=offline) if managed else absolute_path(python or sys.executable, "Python 位置")
        probe = checked_python(python, project)
        if not probe["supported"]:
            raise BootstrapError("需要与当前处理器一致的 Mac Python 3.10–3.14。请使用 .command 启动器选择兼容环境。")
        if not probe["ready"]:
            if offline:
                raise BootstrapError("Python 组件缺失，离线模式未安装：" + "、".join(probe["missing"]))
            if probe["identity"].get("externally_managed"):
                raise BootstrapError("所选 Python 由系统或其他包管理器维护，不能直接补充缺失组件。"
                                     "请重新打开启动器，选择独立部署；完整的现有环境仍可直接复用。")
            common.install_missing_packages(python, project, home, manifest, probe, emit)
        try:
            native = ensure_native(project, home,
                                   lambda message: emit({"event": "progress", "message": str(message)}),
                                   offline=offline)
        except RuntimeError as exc:
            raise BootstrapError("Mac 转换组件准备未完成：" + str(exc)) from exc
        if not native["ready"]:
            raise BootstrapError("Mac 转换组件未准备完整。")
        result = {"event": "ready", "ready": True, "python": str(python), "runtime_home": str(home),
                  "native": native, "environment": ready_environment(project, python, home, native),
                  "message": "环境已就绪。"}
        emit({key: value for key, value in result.items() if key != "environment"})
        return result


def choose_selection(project, args, *, read=input, output=print):
    if offline_package(project):
        data = os.environ.get("QINGJIAN_USER_DATA")
        base = absolute_path(data, "用户数据位置") if data else Path.home() / "Library/Application Support/QingjianLocal"
        home = absolute_path(args.home or os.environ.get("QINGJIAN_RUNTIME_HOME") or base / "offline-runtime", "运行日志位置")
        return {"mode": "reuse", "python": Path(project).resolve() / "Python/bin/python3", "runtime_home": home}
    saved = None if args.choose_environment else saved_selection()
    explicit_home = args.home or os.environ.get("QINGJIAN_RUNTIME_HOME")
    if explicit_home:
        home = absolute_path(explicit_home, "组件位置")
        mode = "managed" if args.managed_python else (args.selection_mode or "reuse")
        default_python = home / "python" / architecture() / "bin/python3" if mode == "managed" else Path(sys.executable)
        return {"mode": mode, "runtime_home": home,
                "python": absolute_path(args.python or default_python, "Python 位置")}
    if saved:
        return {**saved, "runtime_home": Path(saved["runtime_home"]), "python": Path(saved["python"])}
    if args.check_only or not sys.stdin or not sys.stdin.isatty():
        raise BootstrapError("首次启动请双击“启动青简工具箱.command”，先选择 Python 与组件位置。")
    output("1 复用当前 Python（仅补充缺失组件，可供同环境的其他程序共用）")
    output("2 独立部署 Python（不改系统环境）；Q 取消")
    answer = read("请选择 [1]：").strip().casefold()
    if answer == "q":
        return None
    if answer not in {"", "1", "2"}:
        raise BootstrapError("请选择 1、2 或 Q。")
    mode = "managed" if answer == "2" else "reuse"
    output("转换引擎、缓存及日志放在源码包外，更新源码不会重复下载。")
    suggestion = default_home(project)
    raw = read("部署位置（回车采用 " + str(suggestion) + "）：").strip()
    home = absolute_path(raw, "部署位置") if raw else suggestion
    if read("使用 " + str(home) + "，首次补缺可能联网。输入 Y 确认：").strip().casefold() != "y":
        return None
    args.record_selection = True
    return {"mode": mode, "runtime_home": home, "python": Path(sys.executable)}


def emit_console(event):
    if event.get("event") in {"ready", "check", "launched"}:
        print(json.dumps(event, ensure_ascii=False), flush=True)
    elif event.get("message"):
        print(event["message"], flush=True)


def start_detached(project, python, home, environment, arguments, *, emit=emit_console):
    logs = Path(home) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    log_path, ready_path = logs / ("gui-" + token + ".log"), logs / ("gui-ready-" + token + ".json")
    env = dict(environment)
    env["QINGJIAN_GUI_READY_FILE"] = str(ready_path)
    env["QINGJIAN_BOOTSTRAP_READY"] = environment_stamp(project, python)
    with log_path.open("ab") as log:
        child = subprocess.Popen([str(python), *common.PYTHON_FLAGS, "-B", str(Path(project) / "main.py"), *arguments],
                                 cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            marker = read_json(ready_path)
            if (marker and marker.get("pid") == child.pid and marker.get("platform") == "darwin"
                    and Path(marker.get("python", "/nonexistent")).resolve() == Path(python).resolve()):
                emit({"event": "launched", "pid": child.pid, "log": str(log_path),
                      "message": "界面已独立运行，可以关闭准备窗口。"})
                return 0
            if child.poll() is not None:
                raise BootstrapError("界面进程提前退出，请查看启动日志：" + str(log_path))
            time.sleep(.1)
        raise BootstrapError("尚未收到界面就绪信息，请查看启动日志：" + str(log_path))
    finally:
        ready_path.unlink(missing_ok=True)


def start_source(project=PROJECT, argv=None):
    project = Path(project).resolve()
    if os.environ.get("QINGJIAN_BOOTSTRAP_READY") == environment_stamp(project, sys.executable):
        return True
    if offline_package(project):
        home = absolute_path(os.environ.get("QINGJIAN_RUNTIME_HOME") or
                             Path.home() / "Library/Application Support/QingjianLocal/offline-runtime", "运行日志位置")
        checked = check_environment(project, sys.executable, home)
        if not checked["ready"]:
            raise BootstrapError("完整包本地组件校验未通过，请重新完整解压。未连接网络修复。")
        (home / "temp").mkdir(parents=True, exist_ok=True)
        os.environ.update(ready_environment(project, sys.executable, home, checked["native"]))
        return True
    args = build_parser().parse_args([])
    selected = choose_selection(project, args)
    if selected is None:
        return False
    checked = check_environment(project, selected["python"], selected["runtime_home"])
    if checked["ready"] and Path(checked["python"]).resolve() == Path(sys.executable).resolve():
        (Path(checked["runtime_home"]) / "temp").mkdir(parents=True, exist_ok=True)
        os.environ.update(ready_environment(project, sys.executable, Path(checked["runtime_home"]), checked["native"]))
        if args.record_selection:
            remember({**selected, "python": sys.executable})
        return True
    prepared = prepare_environment(project, selected["python"], selected["runtime_home"], emit_console,
                                   managed=selected["mode"] == "managed")
    remember({**selected, "python": prepared["python"]})
    start_detached(project, Path(prepared["python"]), Path(prepared["runtime_home"]), prepared["environment"],
                   list(sys.argv[1:] if argv is None else argv))
    return False


def build_parser():
    parser = argparse.ArgumentParser(description="青简工具箱 Mac 环境准备")
    parser.add_argument("--prepare-environment", "--prepare-only", dest="prepare_only", action="store_true")
    parser.add_argument("--check-environment", "--check-only", dest="check_only", action="store_true")
    parser.add_argument("--home", "--runtime-home", dest="home")
    parser.add_argument("--python")
    parser.add_argument("--managed-python", action="store_true")
    parser.add_argument("--selection-mode", choices=["reuse", "managed"])
    parser.add_argument("--choose-environment", action="store_true")
    parser.add_argument("--record-selection", action="store_true")
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--offline", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args, remaining = parser.parse_known_args(argv)
    if remaining[:1] == ["--"]:
        remaining.pop(0)
    try:
        if sys.platform != "darwin":
            raise BootstrapError("此启动器用于 macOS，请在 Mac 上运行。")
        release = tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
        if release < (14, 4):
            raise BootstrapError("此 Mac 版本需要 macOS 14.4 或更新系统。")
        selected = choose_selection(PROJECT, args)
        if selected is None:
            print("已取消，未创建运行环境。")
            return 130
        if args.check_only:
            checked = check_environment(PROJECT, selected["python"], selected["runtime_home"])
            emit_console({"event": "check", **checked})
            return 0 if checked["ready"] else 1
        result = prepare_environment(PROJECT, selected["python"], selected["runtime_home"], emit_console,
                                     offline=args.offline, managed=selected["mode"] == "managed")
        if args.record_selection:
            remember({**selected, "python": result["python"]})
        if args.no_ui or args.prepare_only:
            return 0
        return start_detached(PROJECT, Path(result["python"]), Path(result["runtime_home"]), result["environment"], remaining)
    except (OSError, BootstrapError, ValueError) as exc:
        print("环境准备未完成：" + str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
