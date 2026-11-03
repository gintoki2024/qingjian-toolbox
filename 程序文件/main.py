"""Entry point with diagnostics for windowed/frozen startup failures."""
import json
import sys
import traceback
from pathlib import Path

# Make the chosen source project importable before environment checks, even
# when the caller starts Python with flags that omit the script directory.
PROJECT = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(PROJECT))


def run():
    report = None
    if "--self-check" in sys.argv:
        index = sys.argv.index("--self-check")
        if index + 1 < len(sys.argv):
            report = Path(sys.argv[index + 1])
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text('{"phase":"starting","passed":false}', encoding="utf-8")
    try:
        if not getattr(sys, "frozen", False):
            from bootstrap import main as environment_main, start_source
            if "--check-environment" in sys.argv:
                return environment_main(["--check-only"])
            if "--prepare-environment" in sys.argv:
                return environment_main(["--prepare-only"])
            if not start_source(PROJECT):
                return 0
        from qingjian.app import main
        return main()
    except Exception as exc:
        detail = traceback.format_exc()
        if report:
            try:
                previous = json.loads(report.read_text(encoding="utf-8"))
            except Exception:
                previous = {}
            previous.update({"passed": False, "startup_error": detail})
            report.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            message = (("程序无法启动，请确认已经完整解压，并保留完整的“程序文件”文件夹。" if getattr(sys, "frozen", False)
                        else "源码启动未完成。请使用包内“启动青简工具箱.cmd”或原EXE启动器重新检查环境，或查看专用运行目录中的 logs。") + "\n\n" + str(exc))
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                app = QApplication.instance() or QApplication([])
                QMessageBox.critical(None, "青简工具箱启动失败", message)
            except Exception:
                if sys.platform == "win32":
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(None, message, "青简工具箱启动失败", 0x10)
        return 2

if __name__ == "__main__":
    raise SystemExit(run())
