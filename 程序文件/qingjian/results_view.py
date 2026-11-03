"""Local presets, task history and an inspectable result dialog."""
from __future__ import annotations

import csv
import json
import os
import stat
import tempfile
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from .catalog import TOOLS
from .profile import task_counts, task_outputs, task_records, task_status

SAFE_OPEN_SUFFIXES = frozenset({
    ".doc", ".docx", ".rtf", ".odt", ".pdf", ".xls", ".xlsx", ".ods",
    ".ppt", ".pptx", ".odp", ".csv", ".tsv", ".txt", ".md", ".json", ".log",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff",
})
MAX_REPORT_PREVIEW_SIZE = 4 * 1024 * 1024
OFFICIAL_REPORT_ENDINGS = ("_公文检查.html", "_公文排版报告.html")
_MODE_TITLES = {"word": "Word 批量处理", "web": "网页转 Word", "convert": "Word / PDF 互转",
                "image": "图片裁剪", "undo_rename": "撤销改名"}


def safe_file_path(value):
    """Return an existing ordinary office/image/text file, never a launcher."""
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        return None
    try:
        path = Path(value)
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            return None
        resolved = path.resolve(strict=True)
        if path.suffix.lower() not in SAFE_OPEN_SUFFIXES or resolved.suffix.lower() not in SAFE_OPEN_SUFFIXES:
            return None
        if not resolved.is_file():
            return None
        return resolved
    except (OSError, ValueError, RuntimeError):
        return None


def open_result_file(value):
    path = safe_file_path(value)
    return bool(path is not None and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))


def _folder_path(value):
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    try:
        path = Path(value).resolve(strict=True)
        return path if path.is_dir() else None
    except (OSError, ValueError, RuntimeError):
        return None


def safe_official_report_path(value, folder):
    """Only generated-name HTML reports inside this task can be previewed."""
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        return None
    if not isinstance(folder, (str, Path)) or not str(folder):
        return None
    try:
        path, root = Path(value), Path(folder)
        if not path.name.lower().endswith(OFFICIAL_REPORT_ENDINGS):
            return None
        # Do not accept a report, task folder, or intermediate directory that
        # redirects to another location, even when its target is in the task.
        for original in (path.absolute(), root.absolute()):
            for part in (original, *original.parents):
                if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                    return None
        root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if not root.is_dir() or not resolved.is_relative_to(root):
            return None
        details = resolved.stat(follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_REPORT_PREVIEW_SIZE or details.st_nlink != 1:
            return None
        return resolved
    except (OSError, ValueError, RuntimeError):
        return None


class ReadOnlyReportBrowser(QTextBrowser):
    """Rich text only: neither link navigation nor resource fetching exists."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setSearchPaths([])

    def loadResource(self, resource_type, name):
        # This includes http(s), file:, relative paths, data:, images and CSS.
        # Never fall back to QTextBrowser's file/network resource loader.
        return QByteArray()


def _label(text, name="muted"):
    label = QLabel(str(text))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setObjectName(name)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


def _button(text, slot=None, name=""):
    button = QPushButton(text)
    if name:
        button.setObjectName(name)
    if slot:
        button.clicked.connect(slot)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _mode_title(mode):
    return TOOLS.get(mode, {}).get("nav", _MODE_TITLES.get(mode, "办公任务"))


def _date_text(value):
    return str(value).replace("T", " ")[:16]


class _ListPage(QWidget):
    def __init__(self, title, subtitle, parent=None):
        super().__init__(parent)
        self.setObjectName("workspace")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 26, 28, 22)
        outer.setSpacing(10)
        self.header = QHBoxLayout()
        self.header.addWidget(_label(title, "pageTitle"), 1)
        outer.addLayout(self.header)
        outer.addWidget(_label(subtitle))
        outer.addSpacing(10)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("workspace")
        self.body.setMinimumWidth(0)
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 0, 4, 0)
        self.rows.setSpacing(12)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)

    def _clear(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                widget = item.widget()
                widget.hide()
                widget.deleteLater()

    def _empty(self, title, description):
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 32, 24, 32)
        layout.setSpacing(12)
        layout.addWidget(_label(title, "cardTitle"))
        layout.addWidget(_label(description))
        self.rows.addWidget(card)
        self.rows.addStretch()


class PresetsPage(_ListPage):
    preset_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__("常用方案", "把常用设置保存下来，下次选择文件后即可复用。密码和文件队列不会保存。", parent)
        self.set_presets([])

    def set_presets(self, presets):
        self._clear()
        values = [item for item in presets if isinstance(item, dict)] if isinstance(presets, list) else []
        if not values:
            self._empty("还没有常用方案", "进入任意工具，调整好处理规则后，点击“保存方案”。")
            return
        for item in values:
            preset_id = str(item.get("id", ""))
            card = QFrame()
            card.setObjectName("card")
            card.setMinimumWidth(0)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(20, 16, 20, 16)
            layout.setSpacing(9)
            layout.addWidget(_label(item.get("name", "未命名方案"), "cardTitle"))
            layout.addWidget(_label(_mode_title(item.get("mode")) + "  ·  " + _date_text(item.get("created", ""))))
            actions = QHBoxLayout()
            actions.addWidget(_button("使用方案", lambda checked=False, key=preset_id: self.preset_requested.emit(key), "sourceAdd"))
            actions.addStretch(1)
            actions.addWidget(_button("删除方案", lambda checked=False, key=preset_id: self.delete_requested.emit(key), "quiet"))
            layout.addLayout(actions)
            self.rows.addWidget(card)
        self.rows.addStretch()


class HistoryPage(_ListPage):
    result_requested = Signal(str)
    open_folder_requested = Signal(str)
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__("最近任务", "回看最近30次处理结果。清空记录不会删除实际文件。", parent)
        self.clear_button = _button("清空记录", self.clear_requested.emit, "quiet")
        self.header.addWidget(self.clear_button)
        self.set_history([])

    def set_history(self, history):
        self._clear()
        values = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        self.clear_button.setEnabled(bool(values))
        if not values:
            self._empty("开始第一项办公任务", "处理完成后，这里会保留时间、结果位置和异常数量，方便继续核查。")
            return
        for item in values:
            job_id = str(item.get("id", ""))
            card = QFrame()
            card.setObjectName("card")
            card.setMinimumWidth(0)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(20, 16, 20, 16)
            layout.setSpacing(9)
            layout.addWidget(_label(item.get("title", "办公任务"), "cardTitle"))
            summary = f"{item.get('status', '完成')}  ·  产物 {item.get('output_count', 0)} 个  ·  异常 {item.get('error_count', 0)} 项"
            layout.addWidget(_label(summary + "  ·  " + _date_text(item.get("created", ""))))
            if item.get("folder"):
                path_label = _label(item["folder"])
                path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                layout.addWidget(path_label)
            actions = QHBoxLayout()
            actions.addWidget(_button("查看结果", lambda checked=False, key=job_id: self.result_requested.emit(key), "sourceAdd"))
            folder_button = _button("打开输出目录", lambda checked=False, key=job_id: self.open_folder_requested.emit(key), "quiet")
            exists = _folder_path(item.get("folder", "")) is not None
            folder_button.setEnabled(exists)
            if not exists:
                folder_button.setToolTip("原结果目录已移动或不存在。")
            actions.addWidget(folder_button)
            actions.addStretch(1)
            layout.addLayout(actions)
            self.rows.addWidget(card)
        self.rows.addStretch()


def _warnings(record):
    parts = []
    if record.get("原因"):
        parts.append(str(record["原因"]))
    warnings = record.get("提示", [])
    if isinstance(warnings, list):
        parts.extend(str(value) for value in warnings)
    elif warnings:
        parts.append(str(warnings))
    return "\n".join(parts)


def _csv_cell(value):
    text = str(value)
    # Spreadsheet applications should display generated text as text, even
    # when a filename or a document warning begins with a formula marker.
    if text.lstrip(" \ufeff").startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + text
    return text


class ResultDialog(QDialog):
    retry_requested = Signal()

    def __init__(self, result, parent=None, title="处理结果", retryable=False):
        super().__init__(parent)
        self.result_data = result if isinstance(result, dict) else {}
        self.records = task_records(self.result_data)
        self.setWindowTitle(title)
        self.resize(940, 650)
        self.setMinimumSize(600, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(10)
        outer.addWidget(_label(title, "pageTitle"))
        outputs, errors = task_counts(self.result_data)
        self.summary_label = _label(f"{task_status(self.result_data)}  ·  产物 {outputs} 个  ·  异常 {errors} 项", "cardTitle")
        outer.addWidget(self.summary_label)
        if self.result_data.get("history_only"):
            outer.addWidget(_label("本页为已保存的结果位置。详细提示请打开输出目录中的处理记录。"))
        elif self.result_data.get("cancelled"):
            outer.addWidget(_label("任务已停止，已经生成的文件保留在输出目录，请核查后使用。"))
        if self.result_data.get("fatal") or self.result_data.get("report_error"):
            notice = str(self.result_data.get("fatal") or "处理记录未能保存：" + str(self.result_data["report_error"]))
            outer.addWidget(_label(notice[:200] + ("…" if len(notice) > 200 else ""), "notice"))

        self.table = QTableWidget(len(self.records), 4)
        self.table.setHorizontalHeaderLabels(["输入 / 任务", "输出文件", "状态", "提示"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(39)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(2, 75)
        self.table.setMinimumHeight(110)
        for row, record in enumerate(self.records):
            input_value, output = str(record.get("输入", "")), str(record.get("输出", ""))
            name = Path(input_value).name if input_value and not input_value.startswith(("https://", "http://")) else input_value
            values = [name, Path(output).name if output else "—", record.get("状态", ""), _warnings(record).replace("\n", "；")]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(input_value if column == 0 else output if column == 1 else str(value))
                self.table.setItem(row, column, cell)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        outer.addWidget(self.table, 3)
        self.selection_hint = _label("选中一项，查看完整提示与处理统计。")
        outer.addWidget(self.selection_hint)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(80)
        outer.addWidget(self.details, 2)

        first_actions = QHBoxLayout()
        self.open_file_button = _button("打开/预览选中文件", self._open_selected, "sourceAdd")
        self.open_folder_button = _button("打开输出目录", self._open_folder)
        self.report_button = _button("打开处理记录", self._open_report)
        self.report_paths = self._report_paths()
        self.report_button.setEnabled(bool(self.report_paths))
        self.open_folder_button.setEnabled(_folder_path(self.result_data.get("folder", "")) is not None)
        first_actions.addWidget(self.open_file_button)
        first_actions.addWidget(self.open_folder_button)
        first_actions.addWidget(self.report_button)
        first_actions.addStretch(1)
        outer.addLayout(first_actions)
        last_actions = QHBoxLayout()
        self.export_button = _button("导出结果清单 CSV", self.export_csv, "quiet")
        self.export_button.setEnabled(bool(self.records))
        last_actions.addWidget(self.export_button)
        last_actions.addStretch(1)
        self.retry_button = _button("重试失败项", self._retry, "sourceAdd")
        self.retry_button.setVisible(bool(retryable))
        self.retry_button.setEnabled(bool(retryable))
        last_actions.addWidget(self.retry_button)
        last_actions.addWidget(_button("关闭", self.accept, "primary"))
        outer.addLayout(last_actions)
        if self.records:
            self.table.selectRow(0)
        else:
            self._selection_changed()
            self.details.setPlainText(str(self.result_data.get("fatal") or self.result_data.get("report_error") or
                                         "当前任务没有生成可列出的文件。可检查异常提示，或调整设置后重新处理。"))

    def _selected_record(self):
        row = self.table.currentRow()
        return self.records[row] if 0 <= row < len(self.records) else None

    def _selection_changed(self):
        record = self._selected_record()
        output = record.get("输出", "") if record else ""
        self.open_file_button.setEnabled(safe_file_path(output) is not None or
                                         safe_official_report_path(output, self.result_data.get("folder", "")) is not None)
        if not record:
            self.details.clear()
            return
        lines = ["输入 / 任务：" + str(record.get("输入", "")), "状态：" + str(record.get("状态", ""))]
        if output:
            lines.append("输出：" + str(output))
        warnings = _warnings(record)
        if warnings:
            lines.extend(["", "完整提示：", warnings])
        for key in ("处理统计", "公文统计"):
            if key in record:
                value = record[key]
                try:
                    text = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else str(value)
                except (TypeError, ValueError):
                    text = str(value)
                lines.extend(["", key + "：", text])
        reports = record.get("检查报告", [])
        if isinstance(reports, list) and reports:
            lines.extend(["", "检查报告：", *[str(value) for value in reports]])
        self.details.setPlainText("\n".join(lines))

    def _open_selected(self):
        record = self._selected_record()
        if not record:
            return
        if not self._open_result_path(record.get("输出", "")):
            self._selection_changed()
            QMessageBox.information(self, "无法打开文件", "文件可能已移动，或当前文件类型不支持直接打开。可打开输出目录查看。")

    def _open_folder(self):
        path = _folder_path(self.result_data.get("folder", ""))
        if path is None or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.information(self, "无法打开目录", "输出目录已移动或不存在。")

    def _report_paths(self):
        values = []
        folder = _folder_path(self.result_data.get("folder", ""))
        if folder:
            values.append(str(folder / "处理记录.json"))
        for record in self.records:
            reports = record.get("检查报告", [])
            if isinstance(reports, list):
                values.extend(reports)
        values.extend(value for value in task_outputs(self.result_data)
                      if Path(value).name.lower().endswith(OFFICIAL_REPORT_ENDINGS) or
                      any(word in Path(value).stem for word in ("报告", "清单", "记录", "异常", "对账", "核查")))
        html_reports, result, seen = [], [], set()
        for value in values:
            html_path = safe_official_report_path(value, self.result_data.get("folder", ""))
            path = html_path or safe_file_path(value)
            if path and str(path) not in seen:
                seen.add(str(path))
                (html_reports if html_path else result).append(str(path))
        return html_reports + result

    def _open_result_path(self, value):
        if safe_official_report_path(value, self.result_data.get("folder", "")) is not None:
            return self._preview_report(value)
        return open_result_file(value)

    def _preview_report(self, value):
        path = safe_official_report_path(value, self.result_data.get("folder", ""))
        if path is None:
            return False
        try:
            before = path.stat(follow_symlinks=False)
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    return False
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    return False
                payload = stream.read(MAX_REPORT_PREVIEW_SIZE + 1)
            if len(payload) > MAX_REPORT_PREVIEW_SIZE or safe_official_report_path(path, self.result_data.get("folder", "")) is None:
                return False
            content = payload.decode("utf-8-sig")
        except (OSError, ValueError, UnicodeError):
            return False
        preview = QDialog(self)
        preview.setObjectName("officialReportPreview")
        preview.setWindowTitle("公文报告 · 只读预览")
        preview.resize(900, 650)
        preview.setMinimumSize(560, 420)
        layout = QVBoxLayout(preview)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(_label(path.name, "cardTitle"))
        browser = ReadOnlyReportBrowser(preview)
        browser.setObjectName("officialReportBrowser")
        browser.setHtml(content)
        layout.addWidget(browser, 1)
        actions = QHBoxLayout()
        actions.addWidget(_label("可复制报告内容，核查后再使用。"), 1)
        actions.addWidget(_button("关闭", preview.accept, "primary"))
        layout.addLayout(actions)
        preview.exec()
        preview.deleteLater()
        return True

    def _open_report(self):
        paths = self._report_paths()
        if len(paths) == 1:
            if not self._open_result_path(paths[0]):
                QMessageBox.information(self, "无法打开记录", "处理记录已移动或无法打开。")
        elif paths:
            menu = QMenu(self)
            for path in paths:
                action = menu.addAction(Path(path).name)
                action.triggered.connect(lambda checked=False, value=path: self._open_result_path(value))
            menu.exec(self.report_button.mapToGlobal(self.report_button.rect().bottomLeft()))
        else:
            self.report_button.setEnabled(False)

    def _retry(self):
        self.accept()
        self.retry_requested.emit()

    def export_csv(self):
        folder = _folder_path(self.result_data.get("folder", ""))
        default = str(folder / "结果清单.csv") if folder else "结果清单.csv"
        target, _ = QFileDialog.getSaveFileName(self, "导出结果清单", default, "CSV 文件 (*.csv)")
        if not target:
            return
        path = Path(target)
        if path.suffix.lower() != ".csv":
            path = Path(str(path) + ".csv")
            if path.exists():
                replace = QMessageBox.question(
                    self, "替换已有清单", f"{path.name} 已存在，是否替换？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if replace != QMessageBox.StandardButton.Yes:
                    return
        staged = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".qingjian-csv-", suffix=".tmp", dir=path.parent)
            staged = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["输入 / 任务", "输出文件", "状态", "提示", "处理统计", "检查报告"])
                for record in self.records:
                    statistics = record.get("处理统计", "")
                    if isinstance(statistics, (dict, list)):
                        statistics = json.dumps(statistics, ensure_ascii=False)
                    reports = record.get("检查报告", [])
                    reports = "\n".join(map(str, reports)) if isinstance(reports, list) else str(reports)
                    writer.writerow([_csv_cell(value) for value in (
                        record.get("输入", ""), record.get("输出", ""), record.get("状态", ""),
                        _warnings(record), statistics, reports,
                    )])
            os.replace(staged, path)
            self.details.setPlainText("结果清单已保存：" + str(path))
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "清单保存失败", str(exc))
        finally:
            if staged:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass
