"""Background execution for aggregate tasks and rename previews."""
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .batch import new_run_directory, write_report
from .catalog import TOOLS
from .core.common import CancelledError
from .tools import execute_tool


class ToolWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)

    def __init__(self, mode, sources, options, base, folder=None):
        super().__init__()
        self.mode, self.sources, self.options = mode, list(sources), dict(options)
        self.base, self.folder = Path(base), Path(folder) if folder else None

    def run(self):
        folder, fatal, report_error = self.folder, "", ""
        result = {"outputs": [], "warnings": [], "errors": [], "changes": 0}
        try:
            folder = folder or new_run_directory(self.base)
            result = execute_tool(self.mode, self.sources, folder, self.options,
                                  lambda d, t, m="": self.progress.emit(d, t, m), self.isInterruptionRequested)
        except CancelledError as exc:
            result["warnings"].append(str(exc))
            if folder:
                result["outputs"] = [str(p) for p in folder.rglob("*") if p.is_file()
                                     and not any(part.startswith(".") for part in p.relative_to(folder).parts)]
        except Exception as exc:
            fatal = str(exc)
            if folder:
                result["outputs"] = [str(p) for p in folder.rglob("*") if p.is_file()
                                     and not any(part.startswith(".") for part in p.relative_to(folder).parts)]
        records = []
        for index, output in enumerate(result.get("outputs", [])):
            records.append({"输入": TOOLS.get(self.mode, {}).get("title", "撤销改名"), "输出": str(output), "状态": "成功",
                            "提示": result.get("warnings", []) if index == 0 else [], "处理统计": result.get("changes", 0)})
        for error in result.get("errors", []):
            records.append({"输入": "异常记录", "输出": "", "状态": "失败", "原因": str(error)})
        if fatal:
            records.append({"输入": "任务", "输出": "", "状态": "失败", "原因": fatal})
        if folder:
            try:
                write_report(folder, records, self.isInterruptionRequested() or bool(result.get("cancelled")))
            except Exception as exc:
                report_error = str(exc)
        self.completed.emit({"kind": "group", "folder": str(folder) if folder else "", "records": records,
                             "cancelled": self.isInterruptionRequested() or bool(result.get("cancelled")), "fatal": fatal, "report_error": report_error,
                             "total": len(self.sources), "output_count": len(result.get("outputs", [])),
                             "error_count": len(result.get("errors", [])), "warnings": result.get("warnings", [])})


class PreviewWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)

    def __init__(self, sources, options, base):
        super().__init__()
        self.sources, self.options, self.base = list(sources), dict(options), Path(base)

    def run(self):
        folder = None
        result = {"fatal": "", "cancelled": False, "plan": [], "sources": self.sources, "options": self.options}
        try:
            from .core.file_ops import plan_organize
            folder = new_run_directory(self.base)
            result["plan"] = plan_organize([Path(p) for p in self.sources], folder, self.options,
                                           lambda d, t, m="": self.progress.emit(d, t, m), self.isInterruptionRequested)
        except CancelledError:
            result["cancelled"] = True
        except Exception as exc:
            result["fatal"] = str(exc)
        result["folder"] = str(folder) if folder else ""
        self.completed.emit(result)
