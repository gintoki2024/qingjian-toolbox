"""Real Windows source-CMD lifecycle checks, isolated from user application data.

Each case creates its own small Qt application and console under work/.  The
application exits through its own stop file; only the PIDs owned by that case
may be terminated in failure cleanup.  No installer, network, or real toolbox
profile is involved.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows console lifecycle")
PROJECT = Path(__file__).resolve().parents[1]
WORK = PROJECT.parents[1] / "work" / "source-detach-verification"

BOOTSTRAP_FIXTURE = r'''
import hashlib, json, os
from pathlib import Path
class BootstrapError(RuntimeError): pass
PYTHON_FLAGS = ['-E', '-X', 'utf8']
def startup_selection(project):
    path = project / 'config/startup-selection.json'
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else None
def environment_stamp(project, python):
    return hashlib.sha256((str(Path(project).resolve()).casefold() + '|' + str(Path(python).absolute()).casefold()).encode('utf-8')).hexdigest()
def check_environment(project, python, home):
    return {'ready': True, 'supported': True, 'engine': 'fixture', 'native_components': {}}
def ready_environment(project, python, home, engine, native_components=None):
    return {'QINGJIAN_BOOTSTRAP_READY': environment_stamp(project, python), 'QINGJIAN_RUNTIME_HOME': str(home)}
def child_environment(home=None): return os.environ.copy()
def prepare_environment(*args, **kwargs): raise AssertionError('fixture must never install or download')
'''

MAIN_FIXTURE = r'''
import argparse, ctypes, json, os, sys, time
from pathlib import Path
import bootstrap
parser = argparse.ArgumentParser()
parser.add_argument('--record', type=Path, required=True)
parser.add_argument('--stop-file', type=Path, required=True)
parser.add_argument('--diagnostic', action='store_true')
parser.add_argument('--exit-code', type=int, default=0)
parser.add_argument('original', nargs='*')
args = parser.parse_args()
token = os.environ['QJ_LIFECYCLE_TOKEN']
record = {'pid': os.getpid(), 'token': token, 'python': sys.executable,
          'status': 'starting', 'args': args.original,
          'user_data': os.environ.get('QINGJIAN_USER_DATA'),
          'ready_stamp': os.environ.get('QINGJIAN_BOOTSTRAP_READY')}
def write_record():
    staging = args.record.with_suffix('.tmp')
    staging.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    os.replace(staging, args.record)
write_record()
print('fixture stdout ' + token, flush=True)
print('fixture stderr ' + token, file=sys.stderr, flush=True)
if record['ready_stamp'] != bootstrap.environment_stamp(Path(__file__).parent, sys.executable):
    record['status'] = 'bad-ready-stamp'
    write_record()
    raise SystemExit(43)
if args.diagnostic:
    record['status'] = 'diagnostic-running'
    write_record()
    time.sleep(0.8)
    record['status'] = 'diagnostic-complete'
    write_record()
    raise SystemExit(args.exit_code)
site_packages = os.environ.get('QJ_LIFECYCLE_SITE_PACKAGES')
if site_packages:
    sys.path.insert(0, site_packages)
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
application = QApplication([])
windows = []
for index, name in enumerate(('青简生命周期主窗口', '可拆记事窗口', '可拆转换窗口')):
    widget = QWidget()
    widget.setWindowTitle(name + ' ' + token[:8])
    widget.resize(290, 120)
    widget.move(60 + index * 315, 80)
    layout = QVBoxLayout(widget)
    layout.addWidget(QLabel(name))
    layout.addWidget(QLabel('此窗口仅供独立启动生命周期验证'))
    widget.show()
    windows.append(widget)
application.processEvents()
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetConsoleWindow.restype = ctypes.c_void_p
user = ctypes.WinDLL('user32', use_last_error=True)
user.IsWindowVisible.argtypes = [ctypes.c_void_p]
user.IsWindowVisible.restype = ctypes.c_int
processes = (ctypes.c_ulong * 16)()
record.update(status='ready', console_hwnd=int(kernel.GetConsoleWindow() or 0),
              console_processes=int(kernel.GetConsoleProcessList(processes, len(processes))),
              console_pids=[int(pid) for pid in processes if pid],
              console_visible=bool(user.IsWindowVisible(kernel.GetConsoleWindow())),
              windows_visible=[bool(user.IsWindowVisible(int(window.winId()))) for window in windows],
              windows=[int(window.winId()) for window in windows], heartbeat=0)
started = time.monotonic()
def tick():
    record['heartbeat'] += 1
    write_record()
    if args.stop_file.exists() or time.monotonic() - started > 45:
        record['status'] = 'stopped'
        write_record()
        application.quit()
timer = QTimer()
timer.timeout.connect(tick)
timer.start(100)
write_record()
application.exec()
raise SystemExit(args.exit_code)
'''


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def wait_until(check, *, timeout=20, explanation="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for " + explanation)


def command_line(script, arguments):
    # An extra outer quote is required by cmd /s /c or /k.  Quote shell
    # metacharacters even when list2cmdline sees no spaces.
    pieces = []
    for value in [str(script), *map(str, arguments)]:
        piece = subprocess.list2cmdline([value])
        if any(character in value for character in "&|<>()^") and not piece.startswith('"'):
            piece = '"' + piece + '"'
        pieces.append(piece)
    return " ".join(pieces)


def cmd_process(script, arguments, *, keep=False, **kwargs):
    prefix = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/q", "/s", "/k" if keep else "/c"])
    return subprocess.Popen(prefix + ' "' + command_line(script, arguments) + '"', **kwargs)


def process_running(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only.
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel.CloseHandle(handle)


def terminate_owned_pid(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE only.
    if handle:
        try:
            kernel.TerminateProcess(handle, 99)
        finally:
            kernel.CloseHandle(handle)


def assert_windows_live(record):
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    assert process_running(record["pid"]), record
    assert len(record["windows"]) == 3, record
    assert all(record["windows_visible"]), record
    assert all(user.IsWindow(handle) for handle in record["windows"]), record


class Case:
    def __init__(self):
        self.token = uuid.uuid4().hex
        self.base = WORK / self.token
        self.root = self.base / "中文 CMD 包 含空格"
        self.project = self.root / "程序文件"
        (self.project / "launcher").mkdir(parents=True)
        for name in ("cmd_source.py", "prepare_python.cmd"):
            shutil.copy2(PROJECT / "launcher" / name, self.project / "launcher" / name)
        self.entry = self.root / "启动青简工具箱.cmd"
        shutil.copy2(PROJECT / "launcher/start_source.cmd", self.entry)
        (self.project / "bootstrap.py").write_text(BOOTSTRAP_FIXTURE, encoding="utf-8")
        (self.project / "main.py").write_text(MAIN_FIXTURE, encoding="utf-8")
        self.record = self.base / "窗口 运行记录.json"
        self.stop = self.base / "仅关闭本测试.stop"
        self.runtime = self.base / "工具缓存 所选目录"
        self.python = Path(sys.executable)
        self.processes = []
        self.environment = os.environ.copy()
        for name in ("QINGJIAN_RUNTIME_HOME", "QINGJIAN_CMD_PYTHON", "QJ_CMD_PROJECT", "QJ_CMD_RESULT", "QINGJIAN_BOOTSTRAP_READY", "QINGJIAN_CMD_SELECTION_DONE", "QINGJIAN_CMD_START"):
            self.environment.pop(name, None)
        self.environment.update(QJ_LIFECYCLE_TOKEN=self.token,
                                QINGJIAN_USER_DATA=str(self.base / "隔离 用户数据"),
                                QJ_LIFECYCLE_SITE_PACKAGES=str(Path(sys.prefix) / "Lib/site-packages"),
                                QT_QPA_PLATFORM="windows")

    def arguments(self, *extra):
        return ["--python", str(self.python), "--runtime-home", str(self.runtime), "--offline", *extra,
                "--", "--record", str(self.record), "--stop-file", str(self.stop)]

    def ready(self):
        return wait_until(lambda: (record if (record := read_json(self.record)) and record.get("status") == "ready" else None),
                          explanation="isolated Qt windows")

    def save_proof(self, value):
        (self.base / "verification.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def stop_gui(self):
        self.stop.write_text(self.token, encoding="utf-8")
        record = read_json(self.record)
        if record and record.get("token") == self.token and process_running(record["pid"]):
            try:
                wait_until(lambda: not process_running(record["pid"]), timeout=8, explanation="fixture stop file")
            except AssertionError:
                terminate_owned_pid(record["pid"])

    def close(self):
        self.stop_gui()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()  # Only the exact Popen PID created by this case.
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.fixture
def case():
    value = Case()
    try:
        yield value
    finally:
        value.close()


def assert_detached_ready(case):
    record = case.ready()
    assert_windows_live(record)
    assert record["console_hwnd"] == 0, record
    assert record["console_visible"] is False, record
    if case.python.with_name("pythonw.exe").is_file():
        assert record["console_processes"] == 0, record
    assert record["user_data"] == str(case.base / "隔离 用户数据")
    matching_logs = []
    for path in (case.runtime / "logs").glob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if "fixture stdout " + case.token in text and "fixture stderr " + case.token in text:
                matching_logs.append(str(path))
    assert matching_logs, "Detached stdout/stderr must remain available in the selected runtime log directory"
    record["matching_logs"] = matching_logs
    return record


@pytest.mark.parametrize("without_pythonw", [False, True], ids=["pythonw", "python-console-fallback"])
def test_source_cmd_returns_before_three_detached_windows_stop(case, without_pythonw):
    if without_pythonw:
        use_console_only_python(case)
    original = ["中文 空格", "literal&(x)"]
    process = cmd_process(case.entry, [*case.arguments(), *original], cwd=case.root, env=case.environment,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    case.processes.append(process)
    output, errors = process.communicate(timeout=30)
    assert process.returncode == 0, (output + errors).decode("utf-8", errors="replace")
    record = assert_detached_ready(case)
    expected_python = case.python if without_pythonw else case.python.with_name("pythonw.exe")
    assert Path(record["python"]).samefile(expected_python), record
    assert record["args"] == original, record
    previous = record["heartbeat"]
    wait_until(lambda: (value := read_json(case.record)) and value.get("heartbeat", -1) > previous,
               explanation="GUI heartbeat after CMD exited")
    assert_windows_live(record)
    case.save_proof({"passed": True, "case": "cmd-exit-before-gui", "without_pythonw": without_pythonw,
                     "cmd_exit_code": process.returncode, "record": record})


def use_console_only_python(case):
    # A real venv console redirector with no sibling pythonw.  Qt modules are
    # inserted only by the fixture main, never installed into this environment.
    interpreter = case.base / "只有控制台的 Python 环境"
    scripts = interpreter / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(sys.executable, scripts / "python.exe")
    (interpreter / "pyvenv.cfg").write_text("home = " + str(Path(sys.base_prefix)) + "\ninclude-system-site-packages = false\n", encoding="utf-8")
    case.python = scripts / "python.exe"
    assert not case.python.with_name("pythonw.exe").exists()


@pytest.mark.parametrize("without_pythonw", [False, True], ids=["pythonw", "python-console-fallback"])
def test_closing_only_the_test_cmd_console_preserves_all_qt_windows(case, without_pythonw):
    if without_pythonw:
        use_console_only_python(case)
    console_record = case.base / "本测试 控制台.json"
    result_record = case.base / "启动器 返回码.txt"
    recorder = case.base / "record_console.py"
    recorder.write_text("import ctypes,json,os,sys\nfrom pathlib import Path\nk=ctypes.WinDLL('kernel32',use_last_error=True)\nk.GetConsoleWindow.restype=ctypes.c_void_p\nPath(sys.argv[1]).write_text(json.dumps({'hwnd':int(k.GetConsoleWindow() or 0),'pid':os.getpid()}),encoding='utf-8')\n", encoding="utf-8")
    wrapper = case.base / "仅本测试 控制台.cmd"
    wrapper.write_text('@echo off\r\nchcp 65001 >nul\r\n' + command_line(case.python, ["-E", "-X", "utf8", recorder, console_record]) + "\r\ncall " + command_line(case.entry, case.arguments()) + '\r\n>"' + str(result_record) + '" echo %errorlevel%\r\n', encoding="utf-8")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # Own test console remains hidden.
    process = cmd_process(wrapper, [], keep=True, cwd=case.root, env=case.environment,
                          creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
                          startupinfo=startup)
    case.processes.append(process)
    wait_until(lambda: result_record.exists(), explanation="source launcher return inside dedicated console")
    assert result_record.read_text(encoding="utf-8").strip() == "0"
    console = read_json(console_record)
    assert console and console["hwnd"], console
    record = assert_detached_ready(case)
    assert record["console_hwnd"] != console["hwnd"], record
    assert process.pid not in record["console_pids"], record
    assert process.poll() is None
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    assert user.PostMessageW(console["hwnd"], 0x0010, 0, 0), ctypes.get_last_error()  # WM_CLOSE to our console only.
    process.wait(timeout=12)
    previous = record["heartbeat"]
    wait_until(lambda: (value := read_json(case.record)) and value.get("heartbeat", -1) > previous + 2,
               explanation="GUI heartbeat after test console WM_CLOSE")
    assert_windows_live(record)
    case.save_proof({"passed": True, "case": "own-console-wm-close", "without_pythonw": without_pythonw, "console": console,
                     "closed_cmd_exit_code": process.returncode, "record": read_json(case.record)})


def test_no_ui_diagnostics_wait_and_return_the_application_exit_code(case):
    arguments = case.arguments("--no-ui") + ["--diagnostic", "--exit-code", "37"]
    started = time.monotonic()
    process = cmd_process(case.entry, arguments, cwd=case.root, env=case.environment,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    case.processes.append(process)
    output, errors = process.communicate(timeout=30)
    assert process.returncode == 37, (output + errors).decode("utf-8", errors="replace")
    elapsed = time.monotonic() - started
    record = read_json(case.record)
    assert record and record["status"] == "diagnostic-complete", record
    assert Path(record["python"]).samefile(case.python), record
    assert elapsed >= 0.8
    assert not process_running(record["pid"])
    assert ("fixture stdout " + case.token).encode() in output
    assert ("fixture stderr " + case.token).encode() in errors
    case.save_proof({"passed": True, "case": "no-ui-synchronous", "cmd_exit_code": process.returncode,
                     "elapsed_seconds": elapsed, "record": record})
