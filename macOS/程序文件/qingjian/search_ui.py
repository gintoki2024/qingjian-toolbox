"""A read-only document search window with in-memory context previews."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy,
    QSplitter, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from .core.search_session import (
    SCOPE_DESCRIPTION, FileSearchResult, SearchHit, SearchReport, keyword_ranges, search_documents,
    validate_search_options,
)
from .icons import bind_window_icon

# A worker has no QObject parent, so even an externally destroyed parent window
# cannot destroy a running QThread. It is released only after finished.
_LIVE_WORKERS = set()


def _label(text="", name="muted"):
    label = QLabel(str(text))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setObjectName(name)
    label.setMinimumWidth(0)
    return label


def _button(text, slot, name=""):
    button = QPushButton(text)
    button.clicked.connect(slot)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        button.setObjectName(name)
    return button


class _SearchWorker(QThread):
    progress = Signal(int, int, str)
    file_ready = Signal(object)
    result_ready = Signal(object)

    def __init__(self, sources, options):
        super().__init__()
        self.sources, self.options = list(sources), dict(options)
        self._last_progress = 0.0

    def _progress(self, done, total, message):
        now = time.monotonic()
        if (total > 0 and done == total) or now - self._last_progress >= 0.1:
            self._last_progress = now
            self.progress.emit(done, total, message)

    def run(self):
        try:
            report = search_documents(self.sources, self.options, self._progress,
                                      self.isInterruptionRequested, self.file_ready.emit)
        except Exception as exc:
            report = SearchReport(fatal=str(exc))
        self.result_ready.emit(report)


def _release_worker(worker):
    _LIVE_WORKERS.discard(worker)
    worker.deleteLater()


class DocumentSearchDialog(QDialog):
    """Construct, show, then call start_search after reserving the shared slot.

    Set ``execution_available`` to a callable returning whether other toolbox
    work is idle. ``busy_changed(False)`` is emitted only once QThread.finished
    has arrived. This viewer uses pdfplumber (not PDFium), but the application
    may still serialize it with conversion work to keep CPU and UI responsive.
    ``completed`` contains transient text and must not be recorded as history.
    """

    busy_changed = Signal(bool)
    completed = Signal(object)

    def __init__(self, sources: list[Path], options: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文档检索 · 青简工具箱")
        self.setWindowFlag(Qt.WindowType.Window, True)
        bind_window_icon(self, "search")
        self.setModal(False)
        self.sources, self.options = [Path(path) for path in sources], dict(options)
        self.execution_available = lambda: True
        self._worker = None
        self._pending_report = None
        self._report = None
        self._files: list[FileSearchResult] = []
        self._closing = False
        self._active_result = None
        self.setMinimumSize(680, 460)
        self.resize(1030, 660)
        screen = self.screen()
        if screen:
            available = screen.availableGeometry()
            self.resize(min(self.width(), max(680, available.width() - 40)),
                        min(self.height(), max(460, available.height() - 60)))
        self._build_ui()

    @property
    def worker(self):
        return self._worker

    @property
    def report(self):
        return self._report

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)
        header = QHBoxLayout()
        title = _label("文档检索", "pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        badge = _label("只读 · 本地检索", "badge")
        badge.setWordWrap(False)
        header.addWidget(badge)
        root.addLayout(header)
        self.criteria_label = _label(self._criteria_description(), "subTitle")
        self.criteria_label.setToolTip(SCOPE_DESCRIPTION)
        root.addWidget(self.criteria_label)
        scope = _label(SCOPE_DESCRIPTION, "notice")
        root.addWidget(scope)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(9)
        left = QFrame()
        left.setObjectName("card")
        left.setMinimumWidth(280)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(13, 12, 13, 12)
        self.file_count = _label("命中文件", "cardTitle")
        left_layout.addWidget(self.file_count)
        self.file_table = QTableWidget(0, 2)
        self.file_table.setHorizontalHeaderLabels(["文件名", "文件地址"])
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setWordWrap(False)
        self.file_table.verticalHeader().hide()
        self.file_table.verticalHeader().setDefaultSectionSize(38)
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.file_table.setColumnWidth(0, 166)
        self.file_table.currentCellChanged.connect(self._selection_changed)
        left_layout.addWidget(self.file_table, 1)
        self.list_empty = _label("准备就绪后开始检索，命中文件会逐个出现在这里。")
        left_layout.addWidget(self.list_empty)
        self.splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("card")
        right.setMinimumWidth(280)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(14, 12, 14, 12)
        self.preview_title = _label("命中内容", "cardTitle")
        right_layout.addWidget(self.preview_title)
        self.preview_path = _label("单击左侧文件，在这里查看命中关键词的原文及位置。")
        self.preview_path.setMaximumHeight(56)
        right_layout.addWidget(self.preview_path)
        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.previous_button = _button("上一处", lambda: self._move_hit(-1))
        self.next_button = _button("下一处", lambda: self._move_hit(1))
        self.hit_selector = QComboBox()
        self.hit_selector.setMinimumWidth(0)
        self.hit_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.hit_selector.setMinimumContentsLength(8)
        self.hit_selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.hit_selector.currentIndexChanged.connect(self._show_hit)
        nav.addWidget(self.previous_button)
        nav.addWidget(self.hit_selector, 1)
        nav.addWidget(self.next_button)
        right_layout.addLayout(nav)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("原文只在当前窗口中展示，不生成检索报告或正文历史。")
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        right_layout.addWidget(self.preview, 1)
        self.hit_details = _label("")
        right_layout.addWidget(self.hit_details)
        actions = QHBoxLayout()
        self.copy_path_button = _button("复制文件地址", self._copy_path, "quiet")
        self.open_folder_button = _button("打开所在文件夹", self._open_folder, "quiet")
        actions.addWidget(self.copy_path_button)
        actions.addWidget(self.open_folder_button)
        actions.addStretch(1)
        right_layout.addLayout(actions)
        self.splitter.addWidget(right)
        self.splitter.setSizes([440, 510])
        root.addWidget(self.splitter, 1)

        self.details_toggle = _button("读取提示", self._toggle_details, "quiet")
        self.details_toggle.setCheckable(True)
        self.details_toggle.hide()
        root.addWidget(self.details_toggle)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(100)
        self.details.hide()
        root.addWidget(self.details)
        self.status_label = _label("准备就绪。检索只读取文件，不修改原件。")
        root.addWidget(self.status_label)
        footer = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(7)
        footer.addWidget(self.progress_bar, 1)
        self.restart_button = _button("开始检索", self.start_search, "primary")
        self.cancel_button = _button("停止", self.cancel_task)
        self.close_button = _button("关闭", self.close)
        footer.addWidget(self.restart_button)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.close_button)
        root.addLayout(footer)
        self._set_busy_controls(False)
        self._set_preview_controls(False)

    def _criteria_description(self):
        query = self.options.get("query", "")
        words = [line.strip() for line in query.splitlines() if line.strip()] if isinstance(query, str) else []
        preview = " / ".join(words)[:160] or "尚未填写关键词"
        mode = "同一单元全部命中" if self.options.get("match_mode") == "all" else "任一关键词命中"
        sensitive = "区分大小写" if self.options.get("case_sensitive") else "不区分大小写"
        return f"关键词：{preview}  ·  {mode}  ·  {sensitive}  ·  {len(self.sources)} 个输入"

    def validation_error(self):
        try:
            validate_search_options(self.options)
            if not self.sources:
                return "请先添加要检索的文件或文件夹。"
        except ValueError as exc:
            return str(exc)
        return ""

    def is_busy(self):
        return self._worker is not None

    def _set_busy_controls(self, busy):
        self.restart_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    def _set_preview_controls(self, enabled):
        self.hit_selector.setEnabled(enabled)
        self.previous_button.setEnabled(enabled and self.hit_selector.currentIndex() > 0)
        self.next_button.setEnabled(enabled and self.hit_selector.currentIndex() < self.hit_selector.count() - 1)
        self.copy_path_button.setEnabled(enabled)
        self.open_folder_button.setEnabled(enabled)

    def _clear_results(self):
        self._files.clear()
        self._active_result = None
        self._report = None
        self._pending_report = None
        self.file_table.setRowCount(0)
        self.hit_selector.clear()
        self.preview.clear()
        self.preview.setExtraSelections([])
        self.preview_title.setText("命中内容")
        self.preview_path.setText("单击左侧文件查看命中内容。")
        self.hit_details.clear()
        self.details.clear()
        self.details.hide()
        self.details_toggle.hide()
        self.details_toggle.setChecked(False)
        self.file_count.setText("命中文件")
        self._set_preview_controls(False)

    @Slot()
    def start_search(self):
        if self.is_busy() or self._closing:
            return False
        error = self.validation_error()
        if error:
            self.status_label.setText(error)
            return False
        if not self.execution_available():
            self.status_label.setText("另有任务正在运行，请等待完成后重新检索。")
            return False
        self._clear_results()
        self.criteria_label.setText(self._criteria_description())
        self.list_empty.setText("正在扫描，命中文件会逐个出现在这里…")
        self.list_empty.show()
        self.status_label.setText("正在准备检索范围…")
        self.progress_bar.setRange(0, 0)
        worker = _SearchWorker(self.sources, self.options)
        self._worker = worker
        _LIVE_WORKERS.add(worker)
        self.destroyed.connect(worker.requestInterruption)
        worker.progress.connect(self._progress)
        worker.file_ready.connect(self._append_file)
        worker.result_ready.connect(self._receive_report)
        worker.finished.connect(self._worker_finished)
        worker.finished.connect(lambda: _release_worker(worker))
        self._set_busy_controls(True)
        self.busy_changed.emit(True)
        worker.start()
        return True

    @Slot()
    def cancel_task(self):
        if self._worker is not None:
            self._worker.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在停止；当前文档读取完成后退出，已有命中保留在本窗口。")

    def cancel_search(self):
        self.cancel_task()

    def wait_for_finished(self, timeout_ms=0):
        """Optional bounded wait for application shutdown; never terminate a thread."""
        return self._worker is None or self._worker.wait(max(0, int(timeout_ms)))

    def shutdown(self):
        self._closing = True
        if self.is_busy():
            self.cancel_task()
            return False
        self.close()
        return True

    @Slot(int, int, str)
    def _progress(self, done, total, message):
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
        if not self._closing and (self._worker is None or not self._worker.isInterruptionRequested()):
            self.status_label.setText(message)

    @Slot(object)
    def _append_file(self, result):
        row = len(self._files)
        self._files.append(result)
        self.file_table.insertRow(row)
        name = QTableWidgetItem(result.path.name)
        name.setToolTip(f"{result.path.name}\n命中 {len(result.hits)} 处" + ("；未完整检索" if result.partial else ""))
        path_item = QTableWidgetItem(str(result.path))
        path_item.setToolTip(str(result.path))
        self.file_table.setItem(row, 0, name)
        self.file_table.setItem(row, 1, path_item)
        self.file_count.setText(f"命中文件 · {len(self._files)}")
        self.list_empty.hide()
        if self.file_table.currentRow() < 0:
            self.file_table.selectRow(0)

    @Slot(object)
    def _receive_report(self, report):
        self._pending_report = report

    @Slot()
    def _worker_finished(self):
        self._worker = None
        report = self._pending_report or SearchReport(fatal="检索线程未返回结果，请重试。")
        self._pending_report = None
        self._report = report
        self._set_busy_controls(False)
        self.restart_button.setText("重新检索")
        self.progress_bar.setRange(0, max(1, report.total_files))
        self.progress_bar.setValue(report.scanned_files)
        status = "已停止" if report.cancelled else "达到上限" if report.limited else "检索完成"
        if report.fatal:
            self.status_label.setText(f"检索未完成：{report.fatal}")
        else:
            self.status_label.setText(
                f"{status} · 已检查 {report.scanned_files}/{report.total_files} 个文件 · "
                f"命中 {len(report.files)} 个文件、{report.hit_count} 处 · 读取失败 {report.error_count} 个")
        notes = [*report.warnings, *[f"读取失败：{item.path}\n{item.message}" for item in report.errors]]
        if report.fatal:
            notes.insert(0, report.fatal)
        self.details.setPlainText("\n\n".join(notes))
        self.details_toggle.setText(f"读取提示 · {len(notes)} 条")
        self.details_toggle.setVisible(bool(notes))
        if report.limited or report.fatal:
            self.details_toggle.setChecked(True)
            self.details.show()
        if not self._files:
            message = "检索已停止，尚未找到命中。" if report.cancelled else "未找到命中，请调整关键词或检查检索范围。"
            if not report.total_files:
                message = "没有可检索的文件，请检查所选位置与支持格式。"
            if report.fatal:
                message = "检索未完成，请查看下方读取提示。"
            self.list_empty.setText(message)
            self.list_empty.show()
        self.busy_changed.emit(False)
        self.completed.emit(report)
        if self._closing:
            QTimer.singleShot(0, self.close)

    def _toggle_details(self):
        self.details.setVisible(self.details_toggle.isChecked())

    @Slot(int, int, int, int)
    def _selection_changed(self, row, _column, _previous_row, _previous_column):
        if row < 0 or row >= len(self._files):
            return
        result = self._files[row]
        self._active_result = result
        self.preview_title.setText(result.path.name)
        self.preview_title.setToolTip(str(result.path))
        self.preview_path.setText(str(result.path))
        self.preview_path.setToolTip(str(result.path))
        self.hit_selector.blockSignals(True)
        self.hit_selector.clear()
        self.hit_selector.addItems([f"{index} · {hit.location}" for index, hit in enumerate(result.hits, 1)])
        self.hit_selector.blockSignals(False)
        self.hit_selector.setCurrentIndex(0)
        self._show_hit(0)

    @Slot(int)
    def _show_hit(self, index):
        result = self._active_result
        if result is None or index < 0 or index >= len(result.hits):
            return
        hit: SearchHit = result.hits[index]
        # Plain-text insertion and QTextCursor formatting never interpret a
        # document's HTML, Markdown, file URLs, scripts, or embedded resources.
        self.preview.setPlainText(hit.text)
        # Qt normalizes CRLF, CR, and paragraph separators to a single newline.
        # Calculate display offsets against that actual text, rather than the
        # unnormalized source string (multiline spreadsheet cells may use CRLF).
        display_text = self.preview.toPlainText()
        ranges = keyword_ranges(display_text, hit.keywords,
                                bool(self.options.get("case_sensitive", False)))
        selections = []
        formatting = QTextCharFormat()
        formatting.setBackground(QColor("#fff1ad"))
        formatting.setForeground(QColor("#604b12"))
        for start, end in ranges:
            # QTextCursor offsets count UTF-16 code units, Python counts Unicode
            # code points. Astral characters before a hit therefore need mapping.
            utf16_start = len(display_text[:start].encode("utf-16-le")) // 2
            utf16_end = len(display_text[:end].encode("utf-16-le")) // 2
            selection = QTextEdit.ExtraSelection()
            cursor = QTextCursor(self.preview.document())
            cursor.setPosition(utf16_start)
            cursor.setPosition(utf16_end, QTextCursor.MoveMode.KeepAnchor)
            selection.cursor, selection.format = cursor, formatting
            selections.append(selection)
        self.preview.setExtraSelections(selections)
        if selections:
            cursor = QTextCursor(selections[0].cursor)
            cursor.clearSelection()
            self.preview.setTextCursor(cursor)
            self.preview.centerCursor()
        message = f"第 {index + 1}/{len(result.hits)} 处 · {hit.location} · 命中：{'、'.join(hit.keywords)[:160]}"
        if hit.truncated:
            message += " · 长段落仅显示命中附近片段"
        if result.partial:
            message += " · 此文件尚未完整检索"
        self.hit_details.setText(message)
        self.hit_details.setToolTip(result.error)
        self._set_preview_controls(True)

    def _move_hit(self, delta):
        target = self.hit_selector.currentIndex() + delta
        if 0 <= target < self.hit_selector.count():
            self.hit_selector.setCurrentIndex(target)

    def _copy_path(self):
        if self._active_result is not None:
            QApplication.clipboard().setText(str(self._active_result.path))

    def _open_folder(self):
        if self._active_result is None:
            return
        try:
            folder = self._active_result.path.parent.resolve(strict=True)
            if folder.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except (OSError, ValueError, RuntimeError):
            self.status_label.setText("原文件所在文件夹已不可用，请检查文件是否被移动。")

    def reject(self):
        if self.is_busy():
            self.close()
            return
        # QDialog.closeEvent itself calls reject(). Calling close() again from
        # here would recursively re-enter that event and keep the window open.
        self._clear_results()
        super().reject()
        # The main window retains this dialog for future searches. A deferred
        # close ends only this session, so reopening must allow a fresh start.
        self._closing = False

    def closeEvent(self, event):
        if self.is_busy():
            self._closing = True
            self.cancel_task()
            self.status_label.setText("正在停止检索，读取结束后将关闭窗口…")
            event.ignore()
            return
        super().closeEvent(event)
