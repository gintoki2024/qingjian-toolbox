"""Console source launcher; runs ordinary Python, never a packaged launcher."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import bootstrap


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="青简工具箱 CMD 源码启动与环境准备")
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--runtime-home", type=Path)
    parser.add_argument("--choose-environment", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--no-ui", action="store_true", help="保留诊断控制台并等待程序结束")
    parser.add_argument("--no-discovery", action="store_true")
    args, rest = parser.parse_known_args(argv)
    args.application_args = rest[1:] if rest[:1] == ["--"] else rest
    return args


def absolute_path(value, label):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise bootstrap.BootstrapError(label + "必须为绝对路径。")
    return path.absolute()


def save_selection(project, selection):
    folder = Path(project) / "config"
    folder.mkdir(parents=True, exist_ok=True)
    staging = folder / (".startup-selection-" + uuid.uuid4().hex + ".tmp")
    try:
        staging.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staging, folder / "startup-selection.json")
    finally:
        staging.unlink(missing_ok=True)


def selection_for(args, *, read=None, output=print):
    """No directory creation or download until an explicit choice is made."""
    read = read or input
    project = args.project.resolve()
    saved = bootstrap.startup_selection(project)
    explicit_python = args.python or os.environ.get("QINGJIAN_CMD_PYTHON")
    explicit_home = args.runtime_home or os.environ.get("QINGJIAN_RUNTIME_HOME")
    if explicit_home:
        python = absolute_path(explicit_python or sys.executable, "Python 路径")
        home = absolute_path(explicit_home, "部署位置")
        return {"schema_version": 1, "mode": "reuse", "python": str(python), "runtime_home": str(home)}
    choice_done = os.environ.get("QINGJIAN_CMD_SELECTION_DONE") == "1"
    if saved and (not args.choose_environment or choice_done):
        if explicit_python:
            saved = {**saved, "mode": "reuse", "python": str(absolute_path(explicit_python, "Python 路径"))}
        # Earlier releases put the default native runtime inside a removable
        # source package. Preserve its components in the new external default.
        from startup_shared import default_runtime_home, package_root
        if saved['mode'] == 'reuse' and Path(saved['runtime_home']).resolve().is_relative_to(package_root(project)):
            previous = saved.get('previous_runtime_homes', [])
            previous = previous if isinstance(previous, list) else []
            previous = [str(Path(saved['runtime_home']).resolve()),
                        *(value for value in previous if isinstance(value, str) and Path(value).is_absolute())]
            saved = {**saved, 'runtime_home': str(default_runtime_home(project)),
                     'previous_runtime_homes': list(dict.fromkeys(previous))[:4]}
            if not args.check_only:
                save_selection(project, saved)
            output('旧版包内运行目录将迁移复用至包外共享目录：' + saved['runtime_home'])
        return saved
    from startup_shared import default_runtime_home, saved_selection
    if not args.choose_environment:
        shared = saved_selection(project, discover_legacy=not args.no_discovery)
        if shared:
            if explicit_python:
                shared = {**shared, 'mode': 'reuse', 'python': str(absolute_path(explicit_python, 'Python 路径'))}
            if not args.check_only:
                save_selection(project, shared)
            output('复用上次选择的共享运行目录：' + shared['runtime_home'])
            return shared
    if args.no_ui or args.check_only:
        raise bootstrap.BootstrapError("尚未选择部署位置。请先双击 CMD 启动器，或同时指定 --python 与 --runtime-home。")
    output("首次启动：复用当前 Python；缺少的 Python 库补入该解释器的标准位置，不会再部署一套 Python。")
    output("当前 Python：" + str(explicit_python or sys.executable))
    output("另选的目录仅用于转换引擎、下载缓存、临时文件和日志。")
    default_home = default_runtime_home(project)
    output("工具组件与缓存默认目录：" + str(default_home))
    output("该目录独立于源码包，更新时删除旧源码不会删除运行组件。")
    answer = read("直接回车使用默认目录；也可输入其他绝对路径；输入 Q 取消：").strip().strip('"')
    if answer.casefold() == "q":
        return None
    home = absolute_path(answer, "工具组件与缓存目录") if answer else default_home
    output("工具组件与缓存目录：" + str(home))
    selection = {"schema_version": 1, "mode": "reuse", "python": str(absolute_path(explicit_python or sys.executable, "Python 路径")),
                 "runtime_home": str(home)}
    if read("将使用上述位置，首次补缺可能联网。输入 Y 确认，其他输入取消：").strip().casefold() != "y":
        return None
    save_selection(project, selection)
    return selection


def emit_console(value):
    print(value.get("message", ""), flush=True)


def managed_python(project, selection):
    if selection["mode"] == "reuse":
        return absolute_path(selection["python"], "Python 路径")
    return bootstrap.selected_interpreter(project, selection["runtime_home"])


def start_detached(project, python, home, environment, arguments, *, emit=emit_console):
    """Give the GUI its own process without console/pipe ownership."""
    windowed = python.with_name("pythonw.exe")
    has_pythonw = windowed.is_file()
    gui_python = windowed if has_pythonw else python
    # A venv's console redirector can create a new console for base Python
    # under DETACHED_PROCESS. CREATE_NO_WINDOW also covers that fallback.
    flags = (subprocess.DETACHED_PROCESS if has_pythonw else subprocess.CREATE_NO_WINDOW)
    flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    environment = dict(environment)
    # pythonw has a different executable path, even though it uses the selected
    # interpreter's packages. main.py must recognize this exact ready process.
    environment["QINGJIAN_BOOTSTRAP_READY"] = bootstrap.environment_stamp(project, gui_python)
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / ("source-gui-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".log")
    command = [str(gui_python), *bootstrap.PYTHON_FLAGS, str(project / "main.py"), *arguments]
    try:
        with log_path.open("ab") as log:
            log.write(("青简工具箱源码独立界面启动\n解释器：" + str(gui_python) + "\n").encode("utf-8"))
            log.flush()
            child = subprocess.Popen(command, cwd=project, env=environment,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     close_fds=True,
                                     creationflags=flags)
    except OSError as exc:
        with contextlib.suppress(OSError):
            with log_path.open("ab") as log:
                log.write(("界面进程创建失败：" + str(exc) + "\n").encode("utf-8", errors="replace"))
        raise bootstrap.BootstrapError("界面独立启动未完成，请查看日志：" + str(log_path) + "。" + str(exc)) from exc
    emit({"event": "launched", "pid": child.pid, "python": str(gui_python), "log": str(log_path),
          "message": "界面已独立启动，准备窗口将自动关闭。启动日志：" + str(log_path)})
    return 0


def run(args, *, emit=emit_console):
    project = args.project.resolve()
    selection = selection_for(args)
    if selection is None:
        emit({"message": "已取消，未下载或创建运行环境。"})
        return 130
    home = absolute_path(selection["runtime_home"], "部署位置")
    python = managed_python(project, selection)
    if python is None or not python.is_file() or python.suffix.casefold() not in {".exe", ""}:
        raise bootstrap.BootstrapError("所选 Python 不可用。请重新运行根目录 CMD 启动器选择或补齐 Python。")
    checked = bootstrap.check_environment(project, python, home)
    if args.check_only:
        print(json.dumps({"event": "check", **checked}, ensure_ascii=False), flush=True)
        return 0 if checked["ready"] else 1
    if not checked.get("supported"):
        raise bootstrap.BootstrapError("需要可运行的 Python 3.10–3.14 64位环境，未修改其他 Python 环境。")
    if checked["ready"]:
        (home / "temp").mkdir(parents=True, exist_ok=True)
        environment = bootstrap.ready_environment(project, python, home, checked["engine"], checked.get("native_components"))
        emit({"message": "所选环境已就绪，直接复用，无需下载。"})
    else:
        environment = bootstrap.prepare_environment(project, python, home, emit=emit, offline=args.offline)["environment"]
    if args.prepare_only:
        return 0
    env = bootstrap.child_environment(home)
    env.update(environment)
    env["QINGJIAN_CMD_START"] = "1"
    if os.name == "nt" and not args.no_ui:
        return start_detached(project, python, home, env, args.application_args, emit=emit)
    command = [str(python), *bootstrap.PYTHON_FLAGS, str(project / "main.py"), *args.application_args]
    return subprocess.run(command, cwd=project, env=env, check=False).returncode


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        return run(args)
    except (KeyboardInterrupt, EOFError):
        print("已取消环境准备。", flush=True)
        return 130
    except Exception as exc:
        print("CMD 源码启动未完成：" + str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
