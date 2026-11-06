"""Acceptance checks that must run on a real Mac, using the packaged Python.

No success result is generated on other platforms. The coordinator launches
separate processes for imports, native UI and business checks so Qt/PDFium
state cannot leak between tests. All sample data is generated and fictional.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import time
import traceback


def deny_network() -> None:
    def blocked(*args, **kwargs):
        raise RuntimeError("离线验收禁止建立网络连接。")
    socket.create_connection = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked


def set_native(project: Path) -> dict:
    sys.path.insert(0, str(project))
    from startup_macos_native import check_native
    native = check_native(project, project, architecture=platform.machine())
    if not native.get("ready"):
        raise RuntimeError("完整包原生组件不完整：" + str(native.get("missing")))
    os.environ.update(native["env"])
    return native


def _dependency_isolation(project: Path, runtime: Path) -> list[dict]:
    """Inspect every direct dependency using the source startup's own mapping.

    Package metadata and the imported modules are checked independently: a
    local dist-info folder must not conceal an import from a user/system site.
    Returned paths are relative to the packaged interpreter for portable QA.
    """
    import bootstrap
    rows = []
    for requirement in bootstrap.requirements(project):
        name = requirement["name"]
        modules = bootstrap.MODULES[name.lower().replace("_", "-")]
        distribution = importlib.metadata.distribution(name)
        root = Path(distribution.locate_file("")).resolve()
        assert root.is_relative_to(runtime), f"{name} distribution root is outside packaged Python: {root}"
        # Pip installs ordinary PathDistribution metadata into dist-info/egg-info.
        # Check the selected distribution's actual metadata location, not just
        # a candidate metadata file discovered separately on sys.path.
        metadata_value = getattr(distribution, "_path", None)
        assert metadata_value is not None, f"{name} distribution has no inspectable metadata location"
        metadata = Path(metadata_value).resolve()
        assert metadata.is_relative_to(runtime), f"{name} metadata is outside packaged Python: {metadata}"
        assert metadata.is_dir(), f"{name} metadata directory is missing: {metadata}"
        imported = []
        for module_name in modules:
            module = importlib.import_module(module_name)
            filename = getattr(module, "__file__", None)
            assert filename, f"{module_name} has no inspectable module file"
            path = Path(filename).resolve()
            assert path.is_relative_to(runtime), f"{module_name} is outside packaged Python: {path}"
            assert path.is_file(), f"{module_name} module file is missing: {path}"
            imported.append({"name": module_name, "path": path.relative_to(runtime).as_posix()})
        rows.append({"name": name, "version": distribution.version,
                     "distribution_root": root.relative_to(runtime).as_posix(),
                     "metadata_directory": metadata.relative_to(runtime).as_posix(), "modules": imported})
    return rows


def probe(project: Path, report: Path) -> None:
    deny_network()
    native = set_native(project)
    runtime = (project / "Python").resolve()
    assert sys.flags.isolated and sys.flags.no_user_site
    assert Path(sys.prefix).resolve() == runtime
    dependencies = _dependency_isolation(project, runtime)
    assert all(Path(root).resolve().is_relative_to(project) for root in native["components"].values())
    from qingjian.core.conversion import find_libreoffice
    from qingjian.core.format_media import engine
    office = find_libreoffice()
    assert office and Path(office).resolve().is_relative_to(project)
    engines = [engine(relative) for relative in ("7zip/7z.exe", "Ghostscript/bin/gswin64c.exe", "OFD/runtime/bin/java.exe")]
    assert all(Path(path).resolve().is_relative_to(project) for path in engines)
    report.write_text(json.dumps({"passed": True, "platform": sys.platform, "architecture": platform.machine(),
                                  "isolated": True, "user_site_disabled": True, "external_environment": False,
                                  "network_calls_denied": True, "direct_dependency_count": len(dependencies),
                                  "direct_dependency_names": [row["name"] for row in dependencies],
                                  "imported_module_count": sum(len(row["modules"]) for row in dependencies),
                                  "dependencies": dependencies}, ensure_ascii=False, indent=2), encoding="utf-8")


def _source_ui_native(project: Path, source_home: Path) -> dict:
    """Read verified source-component receipts without preparing/downloading."""
    source_home = Path(source_home)
    if not source_home.is_absolute():
        raise ValueError("源码界面验收的 --source-home 必须为绝对路径。")
    sys.path.insert(0, str(project))
    from startup_macos_native import check_native
    native = check_native(project, source_home, architecture=platform.machine())
    if not native.get("ready"):
        raise RuntimeError("源码界面验收所需原生组件未就绪：" + str(native.get("missing")))
    os.environ.update(native["env"])
    return native


def native_ui(project: Path, report: Path, *, api_preflight: bool = False,
              source_home: Path | None = None) -> None:
    if api_preflight and source_home is not None:
        raise ValueError("source_home 与 api_preflight 不能同时使用。")
    deny_network()
    if api_preflight:
        # A Windows API rehearsal can reveal stale method/attribute names;
        # its report explicitly cannot satisfy Mac release acceptance.
        sys.path.insert(0, str(project))
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    elif source_home is not None:
        _source_ui_native(project, source_home)
        os.environ.pop("QT_QPA_PLATFORM", None)
    else:
        set_native(project)
        os.environ.pop("QT_QPA_PLATFORM", None)
    from PySide6.QtCore import QCoreApplication, QEvent, QTime, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qingjian.app import MODES, Window
    from qingjian.notebook_store import NotebookStore
    from qingjian.notebook_runtime import NotebookController
    from qingjian.notebook_ui import TaskEditorDialog
    from qingjian.platform_support import ui_font_family
    from qingjian.theme import STYLE
    result = {"passed": False, "platform": sys.platform, "architecture": platform.machine(),
              "native_qt_platform": "", "pages": [], "python_errors": [], "network_calls_denied": True,
              "api_preflight": api_preflight,
              "macos_runtime_verified": not api_preflight and source_home is None,
              "source_runtime_verified": not api_preflight and source_home is not None,
              "verification_scope": "api-preflight" if api_preflight else "source-ui" if source_home is not None else "full-ui"}
    screenshots = report.parent / "source-ui-screenshots" if source_home is not None else report.parent
    screenshots.mkdir(parents=True, exist_ok=True)
    data_folder = report.parent / ("source-fictional-user" if source_home is not None else "fictional-user")
    result["screenshot_subdirectory"] = screenshots.relative_to(report.parent).as_posix()
    app = QApplication([])
    result["native_qt_platform"] = app.platformName()
    if not api_preflight:
        assert app.platformName() == "cocoa", "Native Mac UI validation requires Cocoa"
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setFont(QFont(ui_font_family(), 12))
    app.setQuitOnLastWindowClosed(False)
    window = Window(start_home=True, data_path=data_folder / "profile.json")
    notebook = NotebookController(NotebookStore(data_folder / "notebook.json"),
                                  parent=app, enable_tray=False, test_mode=True)
    notebook.window_created.connect(window.register_notebook_windows)
    old_hook = sys.excepthook
    sys.excepthook = lambda *exc: result["python_errors"].append("".join(traceback.format_exception(*exc)))

    def exercise():
        try:
            window.show()
            app.processEvents()
            window.grab().save(str(screenshots / "Mac-首页.png"))
            for mode in MODES:
                window.switch_mode(MODES.index(mode))
                app.processEvents()
                assert window.settings.currentIndex() == MODES.index(mode)
                assert window.start_button.isVisible(), mode
                options = window.get_options()
                assert isinstance(options, dict), mode
                result["pages"].append(mode)
            window.show_converter()
            app.processEvents()
            assert window.converter_is_visible()
            window.converter_window.grab().save(str(screenshots / "Mac-转换浮窗.png"))
            notebook.show_window()
            app.processEvents()
            notebook.window.set_layout_mode("split")
            peers = notebook.window.owned_windows()
            assert len(peers) == 2
            assert all(peer is not None for peer in peers)
            for index, peer in enumerate(peers):
                peer.show()
                app.processEvents()
                peer.grab().save(str(screenshots / f"Mac-记事浮窗{index + 1}.png"))
            # The appointment control must preserve hours and minutes.
            dialog = TaskEditorDialog(notebook.store, parent=notebook.window)
            dialog.remind_time_edit.setTime(QTime(13, 27))
            assert dialog.remind_edit.time().toString("HH:mm") == "13:27"
            dialog.close()
            dialog.deleteLater()
            result["notebook_windows"] = len(peers)
            result["reminder_hour_minute"] = True
            result["converter_visible"] = True
            result["passed"] = len(result["pages"]) == len(MODES) and not result["python_errors"]
        except Exception:
            result["python_errors"].append(traceback.format_exc())
        finally:
            notebook.shutdown()
            for peer in app.topLevelWidgets():
                peer.close()
                peer.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            result["passed"] = result["passed"] and not result["python_errors"]
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            app.quit()

    QTimer.singleShot(300, exercise)
    QTimer.singleShot(90000, app.quit)
    app.exec()
    sys.excepthook = old_hook
    if not result["passed"]:
        raise RuntimeError("Mac 原生界面验收未通过：" + "\n".join(result["python_errors"]))


def coordinator(bundle: Path, output: Path) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("真实 Mac 验收不能在 Windows 或模拟平台执行。")
    bundle, output = bundle.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    project = bundle / "程序文件"
    marker = json.loads((project / "macos-package.json").read_text(encoding="utf-8"))
    assert marker.get("mode") == "offline" and marker["architecture"] == platform.machine()
    python = project / "Python/bin/python3"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "QINGJIAN", "QJ_", "QT_", "JAVA_", "JDK_", "LIBREOFFICE"))}
    env.update({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONNOUSERSITE": "1", "QINGJIAN_OFFLINE": "1",
                "QINGJIAN_USER_DATA": str(output / "fictional-user"),
                "QINGJIAN_SHARED_RUNTIME_INDEX": str(output / "absent-shared-runtime.json")})
    result = {"passed": False, "platform": sys.platform, "architecture": platform.machine(),
              "version": marker["version"], "checks": [], "path_contains_only_system_tools": True}

    def check(name, command, timeout=240, *, extra_env=None):
        started = time.monotonic()
        done = subprocess.run(command, env={**env, **(extra_env or {})}, cwd=output, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        (output / (name + ".stdout.txt")).write_text(done.stdout, encoding="utf-8")
        (output / (name + ".stderr.txt")).write_text(done.stderr, encoding="utf-8")
        result["checks"].append({"name": name, "returncode": done.returncode, "seconds": round(time.monotonic() - started, 2)})
        (output / "验收汇总.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if done.returncode != 0:
            raise RuntimeError(name + " failed: " + done.stdout[-3000:] + done.stderr[-3000:])

    script = project / "macos_build/verify_macos.py"
    prefix = [str(python), "-I", "-B", str(script)]
    check("01-isolated-environment", prefix + ["--case", "probe", "--project", str(project), "--report", str(output / "环境.json")])
    check("02-root-launcher-check", ["/bin/bash", str(bundle / "启动青简工具箱.command"), "--check-environment", "--offline"])
    check("03-repeat-offline-check", ["/bin/bash", str(bundle / "启动青简工具箱.command"), "--check-environment", "--offline"])
    ready = output / "root-launcher-ready.json"
    check("03b-detached-root-launcher", ["/bin/bash", str(bundle / "启动青简工具箱.command")],
          extra_env={"QINGJIAN_GUI_READY_FILE": str(ready)}, timeout=90)
    launched = json.loads(ready.read_text(encoding="utf-8"))
    assert launched["platform"] == "darwin" and Path(launched["python"]).resolve() == python.resolve()
    pid = int(launched["pid"])
    assert pid > 1 and os.getpgid(pid) == pid
    # Only this test-created, confirmed session is stopped. A detached PID that
    # survived its launcher exit verifies Terminal closure independence.
    os.killpg(pid, signal.SIGTERM)
    result["root_launcher_detached_gui"] = True
    check("04-native-ui", prefix + ["--case", "ui", "--project", str(project), "--report", str(output / "原生界面.json")], timeout=120)
    check("05-real-business", prefix + ["--case", "business", "--project", str(project), "--report", str(output / "业务检查.json")], timeout=600)
    business = json.loads((output / "业务检查.json").read_text(encoding="utf-8"))
    assert business["passed"] and all(item["ok"] for item in business["checks"])
    result["business_checks"] = len(business["checks"])
    assert Path(business["executable"]).resolve() == python.resolve()
    check("06-real-native-conversions", prefix + ["--case", "native", "--project", str(project),
          "--report", str(output / "原生转换.json")], timeout=900)
    native_business = json.loads((output / "原生转换.json").read_text(encoding="utf-8"))
    assert native_business["passed"] and all(item["ok"] for item in native_business["checks"])
    result["native_business_checks"] = len(native_business["checks"])
    result["native_business"] = [{"name": item["name"], "ok": item["ok"]} for item in native_business["checks"]]
    result["passed"] = True
    (output / "验收汇总.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # Only a verified full package is eligible for archive publication.
    (project / "macos-build-validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (project / "MACOS_VALIDATION.txt").write_text(
        "Mac 完整包云端验收已通过\n"
        + "系统：macOS；架构：" + platform.machine() + "；版本：" + marker["version"] + "\n"
        + "已验证包内独立 Python、完整本地转换组件、两次离线检查、根启动器分离启动、"
          "Cocoa 原生 27 页、转换浮窗、记事双窗及小时分钟、真实业务自检，"
          "以及 OFD、7zip、Ghostscript 的真实文件往返转换。\n"
        + "详细结果见 macos-build-validation.json；云端截图和日志随构建验收材料提供。\n"
        + "仍需人工桌面确认：Finder 拖住文件时滚轮翻页、系统通知/休眠唤醒、多屏吸附，"
          "以及下载后首次打开的安全提示。未进行 Developer ID 签名或公证。\n",
        encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=("probe", "ui", "business", "native"))
    parser.add_argument("--project", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--source-home", type=Path,
                        help="仅用于 --case ui：已准备源码原生组件的绝对路径，不进行下载")
    args = parser.parse_args()
    if args.source_home is not None and args.case != "ui":
        parser.error("--source-home 仅可与 --case ui 一起使用")
    if args.source_home is not None and not args.source_home.is_absolute():
        parser.error("--source-home 必须是绝对路径")
    if sys.platform != "darwin":
        parser.error("必须在真实 macOS 执行。")
    if args.case:
        if not args.project or not args.report:
            parser.error("--case 需要 --project 与 --report")
        args.report.parent.mkdir(parents=True, exist_ok=True)
        if args.case == "probe":
            probe(args.project.resolve(), args.report)
        elif args.case == "ui":
            native_ui(args.project.resolve(), args.report, source_home=args.source_home)
        elif args.case == "native":
            set_native(args.project.resolve())
            from macos_build.verify_native_macos import run as native_check
            raise SystemExit(0 if native_check(args.project.resolve(), args.report)["passed"] else 1)
        else:
            deny_network()
            set_native(args.project.resolve())
            from qingjian.selfcheck import run as selfcheck
            raise SystemExit(selfcheck(args.report))
    else:
        if not args.bundle or not args.output:
            parser.error("需要 --bundle 与 --output")
        print(json.dumps(coordinator(args.bundle, args.output), ensure_ascii=False, indent=2))
