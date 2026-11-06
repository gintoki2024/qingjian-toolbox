"""Detached Mac offline GUI launch; diagnostics stay synchronous."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]


def main() -> int:
    marker = json.loads((PROJECT / "macos-package.json").read_text(encoding="utf-8"))
    if sys.platform != "darwin" or marker.get("mode") != "offline" or marker.get("architecture") != platform.machine():
        raise RuntimeError("此完整包与当前 Mac 架构不一致，请下载对应 Apple 芯片或 Intel 版本。")
    os.environ["QINGJIAN_OFFLINE"] = "1"
    os.environ["PYTHONNOUSERSITE"] = "1"
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        os.environ.pop(name, None)
    arguments = sys.argv[1:]
    diagnostic = bool(arguments)  # explicit check/screenshot/no-ui remains observable
    command = [sys.executable, "-I", "-B", str(PROJECT / "main.py"), *arguments]
    if diagnostic:
        return subprocess.run(command, cwd=PROJECT, check=False).returncode
    # Only user logs are external. No release-root writes and no shared console
    # process group that would close the GUI when Terminal is closed.
    logs = Path.home() / "Library/Logs/青简工具箱"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / (time.strftime("launch-%Y%m%d-%H%M%S-") + str(os.getpid()) + ".log")
    selected_ready = os.environ.get("QINGJIAN_GUI_READY_FILE")
    ready = Path(selected_ready) if selected_ready else logs / (log.stem + ".ready.json")
    if not ready.is_absolute() or not ready.parent.is_dir() or ready.exists():
        raise RuntimeError("界面启动确认路径无效或已存在。")
    environment = os.environ.copy()
    environment["QINGJIAN_GUI_READY_FILE"] = str(ready)
    with log.open("ab") as output, open(os.devnull, "rb") as source:
        child = subprocess.Popen(command, cwd=PROJECT, stdin=source, stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True, close_fds=True, env=environment)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if ready.is_file():
            state = json.loads(ready.read_text(encoding="utf-8"))
            if state.get("pid") != child.pid or Path(state.get("python", "")).resolve() != Path(sys.executable).resolve():
                raise RuntimeError("界面启动确认与所选解释器不一致。日志：" + str(log))
            if not selected_ready:
                ready.unlink()
            break
        code = child.poll()
        if code is not None:
            print("青简工具箱启动失败。日志：" + str(log), file=sys.stderr)
            return code or 2
        time.sleep(.1)
    else:
        # The GUI can still be starting. Keep its session independent and give
        # an accurate outcome, without killing or relaunching it repeatedly.
        print("界面尚未完成启动确认。日志：" + str(log), file=sys.stderr)
        return 2
    print("青简工具箱已启动，准备窗口可以关闭。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
