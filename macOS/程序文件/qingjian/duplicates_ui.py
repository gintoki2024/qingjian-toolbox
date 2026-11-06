"""An inspectable duplicate-file window; all scanning and changes run off-thread."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QFile, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .core.duplicate_session import DuplicateBatch, DuplicateScan, DuplicateSession, scan_duplicates
from .icons import bind_window_icon


_ACTIVE_WORKERS = set()
_PATH_ROLE = Qt.ItemDataRole.UserRole


def _release_worker(worker):
    _ACTIVE_WORKERS.discard(worker)
    worker.deleteLater()


def _size(value):
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024


def _label(text, name="muted"):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setObjectName(name)
    return label


def _trash_supported():
    try:
        return bool(hasattr(QFile, "moveToTrash") and hasattr(QFile, "supportsMoveToTrash") and QFile.supportsMoveToTrash())
    except (TypeError, RuntimeError):
        return False


def _move_to_trash(path):
    if not _trash_supported():
        raise RuntimeError("当前系统不支持移入回收站，请改用移到整理目录。")
    return QFile.moveToTrash(path)


class _DuplicateWorker(QThread):
    progress = Signal(int, int, str)
    result_ready = Signal(object)

    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self._last_progress = 0.0

    def _progress(self, done, total, message):
        now = time.monotonic()
        if now - self._last_progress >= 0.05 or (total > 0 and done >= total):
            self._last_progress = now
            self.progress.emit(done, total, message)

    def run(self):
        try:
            value = self.operation(self._progress, self.isInterruptionRequested)
            self.result_ready.emit({"value": value, "error": ""})
        except Exception as exc:
            self.result_ready.emit({"value": None, "error": str(exc)})


class DuplicateReviewDialog(QDialog):
    """The exact selected paths and each group's preserved copy, before changes."""

    def __init__(self, rows, action, destination=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("确认处理重复副本")
        bind_window_icon(self, "duplicates")
        self.resize(880, 440)
        layout = QVBoxLayout(self)
        verb = "移入系统回收站" if action == "trash" else "移到整理目录"
        text = f"即将{verb} {len(rows)} 个文件。每个重复组至少保留一份未勾选的原文件。"
        if destination:
            text += f"\n整理目录：{destination}；遇到同名文件自动更名，已有文件不覆盖。"
        else:
            text += "\n需要恢复时请使用系统回收站；系统不支持时会报告失败。"
        layout.addWidget(_label(text, "notice"))
        self.table = QTableWidget(len(rows), 3)
        self.table.setHorizontalHeaderLabels(["将处理的文件（完整地址）", "保留的原文件（完整地址）", "大小"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        for index, row in enumerate(rows):
            for column, value in enumerate((row["source"], row["keeper"], _size(row["size"]))):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(index, column, item)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(f"确认{verb}")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("返回检查")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class DuplicatesDialog(QDialog):
    """Standalone nonmodal results window sharing the main app's execution slot.

    The caller shows the window and invokes ``start_scan()``.  Set
    ``execution_available`` to the application's execution-slot predicate; every
    scan and batch checks it before emitting ``busy_changed(True)``.
    """

    busy_changed = Signal(bool)
    scan_finished = Signal(object)
    batch_finished = Signal(object)

    def __init__(self, sources: list[Path], options: dict, parent=None, execution_available=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("重复文件查找与整理 · 青简工具箱")
        bind_window_icon(self, "duplicates")
        self.setObjectName("duplicatesWindow")
        self.setStyleSheet("""
            QTreeWidget#duplicatesTree {
                background: #ffffff; alternate-background-color: #fafbfe;
                border: 1px solid #dce1eb; border-radius: 14px; padding: 8px;
                outline: 0;
            }
            QTreeWidget#duplicatesTree::item { min-height: 30px; padding: 3px 2px; border-radius: 6px; }
            QTreeWidget#duplicatesTree::item:hover { background: #f5f4fe; }
            QTreeWidget#duplicatesTree::item:selected { background: #eeecff; color: #564add; }
            QTreeWidget#duplicatesTree QHeaderView::section {
                background: #f8f9fc; color: #6c768b; border: 0;
                border-bottom: 1px solid #dce1eb; padding: 10px 6px; font-weight: 600;
            }
            QPlainTextEdit#duplicateDetails {
                background: #ffffff; border: 1px solid #dce1eb;
                border-radius: 14px; padding: 10px;
            }
        """)
        self.resize(1100, 660)
        self.setMinimumSize(700, 450)
        self.sources = [Path(path) for path in sources]
        self.options = dict(options)
        self.execution_available = execution_available or (lambda: True)
        self.session = DuplicateSession(DuplicateScan())
        self.worker = None
        self._operation = ""
        self._pending_result = None
        self._close_after_worker = False
        self._destruction_hook = None
        self._items = {}
        self._changing_checks = False
        self.last_batch = None
        self._build_ui()
        self._refresh_controls()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        title_row = QHBoxLayout()
        title = _label("重复文件查找", "pageTitle")
        title_row.addWidget(title, 1)
        self.scan_button = QPushButton("开始扫描")
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button = QPushButton("停止")
        self.cancel_button.clicked.connect(self.cancel_task)
        title_row.addWidget(self.scan_button)
        title_row.addWidget(self.cancel_button)
        layout.addLayout(title_row)
        layout.addWidget(_label("按文件内容校验完全重复的副本，直接在此查看结果。先勾选，再确认移到整理目录或系统回收站；每组至少保留一份原文件。", "notice"))
        scopes = "；".join(str(path) for path in self.sources)
        self.scope_label = _label(f"范围：{scopes}" if scopes else "尚未选择文件或文件夹")
        self.scope_label.setToolTip(scopes)
        self.scope_label.setMaximumHeight(44)
        layout.addWidget(self.scope_label)
        self.summary_label = _label("尚未扫描；扫描结果不自动保存为报告文件。")
        layout.addWidget(self.summary_label)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setObjectName("duplicatesTree")
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["文件名 / 重复组", "完整地址", "大小", "修改时间", "处理状态"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setUniformRowHeights(True)
        for column, width in enumerate((190, 260, 80, 140, 145)):
            self.tree.setColumnWidth(column, width)
        self.tree.header().setStretchLastSection(False)
        self.tree.itemChanged.connect(self._selection_changed)
        self.tree.currentItemChanged.connect(self._show_detail)
        self.splitter.addWidget(self.tree)
        details_panel = QWidget()
        detail_layout = QVBoxLayout(details_panel)
        detail_layout.setContentsMargins(8, 0, 0, 0)
        detail_layout.addWidget(_label("文件详情与操作明细", "cardTitle"))
        self.details = QPlainTextEdit()
        self.details.setObjectName("duplicateDetails")
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("单击某个文件，查看完整路径、校验值和处理状态。")
        detail_layout.addWidget(self.details, 1)
        self.splitter.addWidget(details_panel)
        self.splitter.setSizes([820, 260])
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)

        select_row = QHBoxLayout()
        self.select_rest_button = QPushButton("每组保留首项，勾选其余")
        self.select_rest_button.clicked.connect(self.select_redundant)
        self.clear_button = QPushButton("取消全部勾选")
        self.clear_button.clicked.connect(self.clear_selection)
        self.selection_label = _label("已勾选 0 个文件")
        select_row.addWidget(self.select_rest_button)
        select_row.addWidget(self.clear_button)
        select_row.addWidget(self.selection_label, 1)
        layout.addLayout(select_row)
        action_row = QHBoxLayout()
        self.move_button = QPushButton("移到整理目录…")
        self.move_button.setObjectName("primary")
        self.move_button.clicked.connect(self._choose_destination)
        self.trash_button = QPushButton("移入系统回收站…")
        self.trash_button.clicked.connect(lambda: self.start_batch("trash"))
        if not _trash_supported():
            self.trash_button.setToolTip("当前系统未提供回收站操作，请使用移到整理目录。")
        action_row.addWidget(self.move_button)
        action_row.addWidget(self.trash_button)
        action_row.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        action_row.addWidget(close_button)
        layout.addLayout(action_row)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.status_label = _label("准备就绪")
        layout.addWidget(self.status_label)

    def is_busy(self):
        return self.worker is not None

    def _execution_allowed(self):
        if self.is_busy():
            return False
        if not self.execution_available():
            QMessageBox.information(self, "请稍候", "另有任务正在运行，请等待任务结束后再扫描或整理重复文件。")
            return False
        return True

    def _start_worker(self, operation, callback):
        self._operation = operation
        self._pending_result = None
        worker = _DuplicateWorker(callback)
        self.worker = worker
        _ACTIVE_WORKERS.add(worker)
        worker.progress.connect(self._progress)
        worker.result_ready.connect(self._remember_result)
        worker.finished.connect(self._worker_finished)
        # No QObject parent: even an unexpected parent-widget destruction cannot
        # destroy a running QThread.  The normal close path waits asynchronously.
        self._destruction_hook = lambda _object=None, task=worker: task.requestInterruption() if task.isRunning() else None
        self.destroyed.connect(self._destruction_hook)
        worker.finished.connect(lambda task=worker: _release_worker(task))
        self.busy_changed.emit(True)
        self._refresh_controls()
        self.progress_bar.setRange(0, 0)
        worker.start()

    def start_scan(self):
        if not self._execution_allowed():
            return False
        if not self.sources:
            QMessageBox.information(self, "请选择范围", "请先在工具页选择需要查重的文件或文件夹。")
            return False
        self._close_after_worker = False
        scopes = "；".join(str(path) for path in self.sources)
        self.scope_label.setText("范围：" + scopes)
        self.scope_label.setToolTip(scopes)
        self.status_label.setText("正在读取文件夹和校验内容…")
        sources, options = list(self.sources), dict(self.options)
        self._start_worker("scan", lambda progress, cancel: scan_duplicates(sources, options, progress, cancel))
        return True

    def cancel_task(self):
        if self.worker is not None:
            self.worker.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在停止；已完成的操作会保留。")

    def _progress(self, done, total, message):
        if done <= 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, max(1, total))
            self.progress_bar.setValue(done)
        if not self._close_after_worker:
            self.status_label.setText(message)

    def _remember_result(self, result):
        self._pending_result = result

    def _worker_finished(self):
        worker = self.worker
        result = self._pending_result or {"value": None, "error": "任务未返回结果。"}
        operation = self._operation
        self.worker = None
        self._pending_result = None
        if worker is not None:
            # Disconnect this per-task destruction hook before deleting the worker.
            try:
                self.destroyed.disconnect(self._destruction_hook)
            except (TypeError, RuntimeError):
                pass
            self._destruction_hook = None
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100 if not result["error"] else 0)
        if result["error"]:
            self.status_label.setText(f"任务未完成：{result['error']}")
            self.details.setPlainText(result["error"])
        elif operation == "scan":
            scan = result["value"]
            self.session = DuplicateSession(scan)
            self.last_batch = None
            self._populate()
            self.summary_label.setText(f"已扫描 {scan.scanned_count} 个文件 · {len(scan.groups)} 个重复组 · 多余副本 {scan.duplicate_count} 份（{_size(scan.duplicate_bytes)}）" +
                                       (f" · 跳过 / 异常 {len(scan.errors)} 项" if scan.errors else ""))
            self.status_label.setText("扫描已停止，显示已经校验的结果。" if scan.cancelled else "扫描完成，可勾选需要处理的重复副本。")
            if scan.errors:
                self.details.setPlainText("扫描提示：\n" + "\n".join(scan.errors))
            self.scan_finished.emit(scan)
        else:
            batch = result["value"]
            self.last_batch = batch
            self._apply_outcomes(batch)
            failed = len(batch.items) - batch.success_count
            self.status_label.setText(f"{'已停止' if batch.cancelled else '处理完成'}：成功 {batch.success_count} 个，失败 {failed} 个；未处理文件保持原状。")
            self.details.setPlainText("\n\n".join(f"{item.source}\n{item.message}" for item in batch.items) or "任务已停止，尚未处理文件。")
            self.batch_finished.emit(batch)
        self.busy_changed.emit(False)
        self._refresh_controls()
        if self._close_after_worker:
            self._close_after_worker = False
            self.close()

    def _populate(self):
        self._changing_checks = True
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            self._items.clear()
            self.details.clear()
            for number, group in enumerate(self.session.groups, 1):
                parent = QTreeWidgetItem([f"重复组 {number} · {len(group.files)} 份", "", _size(group.size), "", ""])
                self.tree.addTopLevelItem(parent)
                for entry in group.files:
                    modified = datetime.fromtimestamp(entry.modified_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M")
                    item = QTreeWidgetItem([entry.path.name, str(entry.path), _size(entry.size), modified, "待选择"])
                    item.setData(0, _PATH_ROLE, str(entry.path))
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                    item.setToolTip(0, str(entry.path))
                    item.setToolTip(1, str(entry.path))
                    parent.addChild(item)
                    self._items[str(entry.path)] = item
                parent.setExpanded(True)
        finally:
            self.tree.setUpdatesEnabled(True)
            self._changing_checks = False
        self._refresh_controls()

    def selected_paths(self):
        return [Path(path) for path, item in self._items.items()
                if item.checkState(0) == Qt.CheckState.Checked and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)]

    def select_redundant(self):
        if self.is_busy():
            return
        wanted = {str(path) for path in self.session.suggest_selection()}
        self._changing_checks = True
        for path, item in self._items.items():
            item.setCheckState(0, Qt.CheckState.Checked if path in wanted else Qt.CheckState.Unchecked)
        self._changing_checks = False
        self._refresh_controls()

    def clear_selection(self):
        if self.is_busy():
            return
        self._changing_checks = True
        for item in self._items.values():
            item.setCheckState(0, Qt.CheckState.Unchecked)
        self._changing_checks = False
        self._refresh_controls()

    def _selection_changed(self, _item=None, _column=0):
        if not self._changing_checks:
            self._refresh_controls()

    def _refresh_controls(self):
        busy, count = self.is_busy(), len(self.selected_paths())
        self.scan_button.setEnabled(not busy)
        self.scan_button.setText("重新扫描" if self._items else "开始扫描")
        self.cancel_button.setEnabled(busy)
        self.tree.setEnabled(not busy)
        self.select_rest_button.setEnabled(not busy and bool(self.session.suggest_selection()))
        self.clear_button.setEnabled(not busy and count > 0)
        self.move_button.setEnabled(not busy and count > 0)
        self.trash_button.setEnabled(not busy and count > 0 and _trash_supported())
        self.selection_label.setText(f"已勾选 {count} 个文件")

    def _show_detail(self, item, _previous=None):
        if item is None:
            return
        path = item.data(0, _PATH_ROLE)
        if not path:
            return
        entry = next((entry for group in self.session.groups for entry in group.files if str(entry.path) == path), None)
        if entry is None:
            return
        outcome = next((value for value in self.session.outcomes.values() if value.source == path), None)
        detail = f"文件名：{entry.path.name}\n\n完整地址：\n{entry.path}\n\n大小：{_size(entry.size)}（{entry.size} 字节）\n\nSHA256：\n{entry.sha256}"
        if outcome:
            detail += f"\n\n处理状态：\n{outcome.message}"
            if outcome.target:
                detail += f"\n\n目标位置：\n{outcome.target}"
        self.details.setPlainText(detail)

    def _choose_destination(self):
        directory = QFileDialog.getExistingDirectory(self, "选择整理目录（不会覆盖已有文件）")
        if directory:
            self.start_batch("move", Path(directory))

    def _confirm_batch(self, rows, action, destination):
        return DuplicateReviewDialog(rows, action, destination, self).exec() == QDialog.DialogCode.Accepted

    def start_batch(self, action, destination=None):
        if not self._execution_allowed():
            return False
        selected = self.selected_paths()
        try:
            rows = self.session.review_rows(selected)
            if action not in {"move", "trash"}:
                raise ValueError("仅支持移到整理目录或移入系统回收站。")
            if action == "trash" and not _trash_supported():
                raise ValueError("当前系统不支持移入回收站，请改用移到整理目录。")
            if action == "move" and (destination is None or not Path(destination).is_dir()):
                raise ValueError("请先选择可用的整理目录。")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "请检查选择", str(exc))
            return False
        if not self._confirm_batch(rows, action, destination):
            return False
        # A modal review pumps events; another tool could start in that time.
        if not self._execution_allowed():
            return False
        self._close_after_worker = False
        self.status_label.setText("正在重新核对文件并处理所选副本…")
        session = self.session
        self._start_worker("batch", lambda progress, cancel: session.process(
            selected, action, destination=destination, trash=_move_to_trash,
            progress=progress, cancel=cancel))
        return True

    def _apply_outcomes(self, batch: DuplicateBatch):
        self._changing_checks = True
        self.tree.setUpdatesEnabled(False)
        try:
            for outcome in batch.items:
                item = self._items.get(outcome.source)
                if item is None:
                    continue
                item.setText(4, {"moved": "已移到整理目录", "trashed": "已移入回收站", "failed": "失败，原件保留"}.get(outcome.status, outcome.status))
                item.setToolTip(4, outcome.message)
                if outcome.status in {"moved", "trashed"}:
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable & ~Qt.ItemFlag.ItemIsEnabled)
        finally:
            self.tree.setUpdatesEnabled(True)
            self._changing_checks = False

    def closeEvent(self, event):
        if self.is_busy():
            self._close_after_worker = True
            self.cancel_task()
            self.status_label.setText("正在安全停止任务，结束后自动关闭窗口…")
            event.ignore()
        else:
            event.accept()

    def reject(self):
        self.close()
