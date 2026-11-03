"""Chinese desktop UI; processing stays in one background worker."""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QStandardPaths, QUrl, QSize, QMargins
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QAbstractSpinBox, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from . import __version__
from .batch import SUPPORTED, dispatch, is_url, new_run_directory, output_path, validate_options, write_report
from .core.conversion import find_libreoffice
from .catalog import EXTRA_ORDER, TOOLS, OFFICE_SPECS
from .forms import OptionPanel, WorkflowPanel
from .official_panel import OfficialPanel
from .tools import scrub_secrets, validate_tool_options
from .workers import ToolWorker, PreviewWorker
from .theme import STYLE
from .guides import GUIDES, build_example, guide_html
from .profile import ProfileStore
from .redaction_editor import SensitiveRuleEditor
from .redaction_library import RedactionLibraryDialog
from .redaction_memory import MAX_RULES, RedactionMemoryStore, normalize_rules
from .results_view import HistoryPage, PresetsPage, ResultDialog
from .workbench import HomePage, ToolCatalogPage, SourceActionDialog, SCENES, TOOL_GROUPS
from .quick_actions import MAX_INPUTS as QUICK_MAX_INPUTS, QuickActionWorker, validate_sources as validate_quick_sources
from .window_layout import compact_window_geometry, compact_window_minimum_size
from .converter_ui import ConverterWindow, target_title as conversion_target_title
from .conversion_session import ConversionSession

MODES = ["word", "web", "convert", "image"]
TITLES = ["Word / TXT 批量处理", "网页转 Word", "Word / PDF 互转", "图片批量裁剪"]
DESCRIPTIONS = [
    "统一格式、替换内容与规则脱敏，把重复操作一次做完。",
    "粘贴多个链接，或导入已保存的 HTML / MHTML，归档为图文 Word。",
    "文档转换在本机完成，按用途选择 PDF 的转出方式。",
    "按边距、比例或目标尺寸裁剪，批量生成新的图片。",
]
MODES += EXTRA_ORDER
TITLES += [TOOLS[mode]["title"] for mode in EXTRA_ORDER]
DESCRIPTIONS += [TOOLS[mode]["description"] for mode in EXTRA_ORDER]
SUPPORTED.update({mode: TOOLS[mode]["extensions"] for mode in EXTRA_ORDER})


def label(text: str, name: str | None = None, wrap: bool = False) -> QLabel:
    value = QLabel(text)
    if name:
        value.setObjectName(name)
    value.setWordWrap(wrap)
    if wrap:
        value.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    return value


def button(text: str, callback=None, name: str | None = None) -> QPushButton:
    value = QPushButton(text)
    value.setCursor(Qt.CursorShape.PointingHandCursor)
    if callback:
        value.clicked.connect(callback)
    if name:
        value.setObjectName(name)
    return value


def notebook_snapshot(window) -> QPixmap:
    """Capture both desktop widgets without including other applications."""
    peers = window.owned_windows() if hasattr(window, "owned_windows") else [window]
    shots = [peer.grab() for peer in peers if peer.isVisible()]
    if not shots:
        return window.grab()
    if len(shots) == 1:
        return shots[0]
    gap = 16
    widths = [math.ceil(shot.width() / shot.devicePixelRatio()) for shot in shots]
    heights = [math.ceil(shot.height() / shot.devicePixelRatio()) for shot in shots]
    ratio = max(shot.devicePixelRatio() for shot in shots)
    canvas = QPixmap(math.ceil((sum(widths) + gap * (len(shots) - 1)) * ratio),
                     math.ceil(max(heights) * ratio))
    canvas.setDevicePixelRatio(ratio)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    x = 0
    for shot, width in zip(shots, widths):
        painter.drawPixmap(x, 0, shot)
        x += width + gap
    painter.end()
    return canvas


def card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 18, 22, 20)
    layout.setSpacing(16)
    return frame, layout


def checkbox(text: str, checked: bool = False) -> QCheckBox:
    control = QCheckBox(text)
    control.setChecked(checked)
    return control


def spin(value: float, low: float, high: float, suffix: str = "", decimals: int = 1):
    control = QDoubleSpinBox()
    control.setDecimals(decimals)
    control.setRange(low, high)
    control.setValue(value)
    control.setSuffix(suffix)
    control.setMinimumWidth(100)
    return control


def icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(QColor("#6257ed"))
    painter = QPainter(pix)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Microsoft YaHei UI", 31, QFont.Weight.Bold))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "简")
    painter.end()
    return QIcon(pix)


class BatchWorker(QThread):
    file_started = Signal(int)
    file_done = Signal(int, bool, str)
    progress = Signal(int, int)
    completed = Signal(object)

    def __init__(self, mode: str, files: list[str], options: dict, base: Path):
        super().__init__()
        self.mode, self.files, self.options, self.base = mode, files, options, base

    def run(self):
        records = []
        folder = None
        fatal = ""
        report_error = ""
        try:
            folder = new_run_directory(self.base)
            for index, source in enumerate(self.files):
                if self.isInterruptionRequested():
                    break
                self.file_started.emit(index)
                target = output_path(self.mode, source, folder, self.options, index)
                record = {"输入": source, "输出": str(target), "状态": "成功"}
                try:
                    result = dispatch(self.mode, source, target, self.options)
                    warnings = result.get("warnings", [])
                    record.update({"处理统计": result.get("changes", 0), "提示": warnings})
                    record["输出"] = result.get("primary_output", str(target))
                    if result.get("reports"):
                        record["检查报告"] = result["reports"]
                        record["公文统计"] = result.get("official_summary", {})
                    text = "完成 · 请复核" if warnings else "完成"
                    self.file_done.emit(index, True, text)
                except Exception as exc:
                    record.update({"状态": "失败", "原因": str(exc), "输出": ""})
                    self.file_done.emit(index, False, str(exc))
                records.append(record)
                self.progress.emit(index + 1, len(self.files))
        except Exception as exc:
            fatal = str(exc)
        if folder:
            try:
                write_report(folder, records, self.isInterruptionRequested())
            except Exception as exc:
                report_error = str(exc)
        self.completed.emit({"folder": str(folder) if folder else "", "records": records,
                             "cancelled": self.isInterruptionRequested(), "fatal": fatal,
                             "report_error": report_error, "total": len(self.files)})


class FileTable(QTableWidget):
    paths_dropped = Signal(list)

    def __init__(self):
        super().__init__(0, 3)
        self.setHorizontalHeaderLabels(["文件 / 网页", "类型", "状态"])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(1, 80)
        self.setColumnWidth(2, 150)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(38)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAcceptDrops(True)
        self.setMinimumHeight(82)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

    def dragMoveEvent(self, event):
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dropEvent(self, event):
        self.paths_dropped.emit([u.toLocalFile() if u.isLocalFile() else u.toString()
                                 for u in event.mimeData().urls()])
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()


class StatusLabel(QLabel):
    """Retain the complete message when the fixed footer wraps long errors."""

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)


class ElidedLabel(QLabel):
    """Keep the source summary on one line without widening the workspace."""

    def __init__(self, text=""):
        super().__init__()
        self.full_text = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text):
        self.full_text = text
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(
            self.full_text, Qt.TextElideMode.ElideMiddle, max(0, self.contentsRect().width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class SourceBar(QFrame):
    """Accept file drops while the optional queue stays out of the way."""

    paths_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("sourceBar")
        self.setAcceptDrops(True)

    def _drag_state(self, active):
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and self.acceptDrops():
            self._drag_state(True)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() and self.acceptDrops():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

    def dragLeaveEvent(self, event):
        self._drag_state(False)
        event.accept()

    def dropEvent(self, event):
        self._drag_state(False)
        if self.acceptDrops() and event.mimeData().hasUrls():
            self.paths_dropped.emit([u.toLocalFile() if u.isLocalFile() else u.toString()
                                     for u in event.mimeData().urls()])
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()


class Window(QMainWindow):
    notebook_requested = Signal()

    def __init__(self, *, start_home=False, data_path=None):
        super().__init__()
        self.setWindowTitle(f"青简工具箱 · {__version__}")
        self.setWindowIcon(icon())
        self._ui_ready = False
        self._layout_compact = None
        self._frame_corrected = False
        screen = QApplication.primaryScreen().availableGeometry()
        self.setMinimumSize(compact_window_minimum_size(screen))
        self.setGeometry(compact_window_geometry(screen))
        self.mode = "word"
        self.files = {mode: [] for mode in MODES}
        self.queue_states = {mode: {} for mode in MODES}
        self._busy = False
        self._quick_active = False
        self._quick_action_id = ""
        self.worker = None
        self.last_result = None
        self.quick_result = None
        self.converter_window = None
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        self.conversion_session = ConversionSession(
            Path(data_path).parent / "quick-settings.json" if data_path else None,
            default_folder=Path(downloads or str(Path.home() / "Downloads")) / "青简快捷转换")
        self._converter_settings = Path(data_path).parent / "converter-window.json" if data_path else None
        self.pending_preview = None
        self.nav_buttons = []
        self.profile_store = ProfileStore(data_path)
        self.redaction_store = RedactionMemoryStore(Path(data_path).parent / "redaction-library.json" if data_path else None)
        self.active_scene = ""
        self.active_context = None
        self.last_context = None
        self._word_scene = "word"
        self.word_scene_options = {}
        self.surface_buttons = {}

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        side = QFrame()
        self.sidebar = side
        side.setObjectName("sidebar")
        side.setFixedWidth(204)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(18, 25, 16, 20)
        side_layout.setSpacing(8)
        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        brand_mark = label("简", "brandMark")
        brand_mark.setFixedSize(38, 38)
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(brand_mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(2)
        brand_text.addWidget(label("青简工具箱", "brand"))
        brand_text.addWidget(label("让办公少一点重复", "brandSub"))
        brand_row.addLayout(brand_text, 1)
        side_layout.addLayout(brand_row)
        side_layout.addSpacing(16)
        self.converter_button = button("独立格式转换", self.show_converter, "converterLaunch")
        self.converter_button.setMinimumHeight(44)
        self.converter_button.setAccessibleName("独立格式转换")
        self.converter_button.setToolTip("拖入文件，选择可用目标格式 · 可折叠为悬浮按钮 · Ctrl+Shift+C")
        side_layout.addWidget(self.converter_button)
        self.notebook_button = button("记事与提醒", lambda checked=False: self.notebook_requested.emit(), "notebookLaunch")
        self.notebook_button.setMinimumHeight(48)
        self.notebook_button.setAccessibleName("记事与提醒")
        self.notebook_button.setToolTip("打开可拆分的日历与事项浮窗 · Ctrl+Shift+N")
        notebook_icon = QPixmap(22, 22)
        notebook_icon.fill(Qt.GlobalColor.transparent)
        icon_painter = QPainter(notebook_icon)
        icon_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        icon_painter.setPen(QPen(QColor("#ffffff"), 1.6))
        icon_painter.drawRoundedRect(3, 4, 16, 15, 3, 3)
        icon_painter.drawLine(3, 9, 19, 9)
        icon_painter.drawLine(7, 2, 7, 6)
        icon_painter.drawLine(15, 2, 15, 6)
        icon_painter.drawPoint(8, 13)
        icon_painter.drawPoint(14, 13)
        icon_painter.drawPoint(8, 16)
        icon_painter.end()
        self.notebook_button.setIcon(QIcon(notebook_icon))
        self.notebook_button.setIconSize(QSize(22, 22))
        side_layout.addWidget(self.notebook_button)
        side_layout.addSpacing(8)
        for key, title in (("home", "工作台"), ("catalog", "全部工具"),
                           ("presets", "常用方案"), ("history", "最近任务")):
            control = button(title, lambda checked=False, name=key: self.show_surface(name), "surfaceNav")
            control.setCheckable(True)
            side_layout.addWidget(control)
            self.surface_buttons[key] = control
        self.context_heading = label("当前工具", "navGroup")
        side_layout.addWidget(self.context_heading)
        self.nav_scroll = QScrollArea()
        self.nav_scroll.setWidgetResizable(True)
        self.nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav_scroll.setObjectName("navScroll")
        nav_body = QWidget()
        nav_body.setObjectName("navBody")
        nav_layout = QVBoxLayout(nav_body)
        nav_layout.setContentsMargins(0, 0, 5, 0)
        nav_layout.setSpacing(3)
        for index, mode in enumerate(MODES):
            title = ["文档批处理", "网页转 Word", "Word / PDF", "图片裁剪"][index] if index < 4 else TOOLS[mode]["nav"]
            nav = button(title, lambda checked=False, i=index: self.switch_mode(i), "nav")
            nav.setCheckable(True)
            nav_layout.addWidget(nav)
            self.nav_buttons.append(nav)
        nav_layout.addStretch()
        self.nav_scroll.setWidget(nav_body)
        side_layout.addWidget(self.nav_scroll, 1)
        self.sidebar_space = QWidget()
        side_layout.addWidget(self.sidebar_space, 1)
        self.sidebar_space.hide()
        help_button = button("使用说明与能力范围", self.show_help, "nav")
        side_layout.addWidget(help_button)
        side_layout.addSpacing(8)
        side_layout.addWidget(label(f"文件在本机处理\nv{__version__}  ·  Windows 64 位", "sideFoot"))
        outer.addWidget(side)

        self.content_stack = QStackedWidget()
        self.content_stack.setMinimumWidth(0)
        self.content_stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.job_page = QWidget()
        self.job_page.setMinimumWidth(0)
        self.job_page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.job_page.setObjectName("jobPage")
        main_layout = QVBoxLayout(self.job_page)
        main_layout.setContentsMargins(28, 25, 26, 20)
        main_layout.setSpacing(16)
        self.content_stack.addWidget(self.job_page)
        host = QWidget()
        host.setMinimumWidth(0)
        host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)
        self.compact_toolbar = QFrame()
        self.compact_toolbar.setObjectName("compactToolbar")
        toolbar = QHBoxLayout(self.compact_toolbar)
        toolbar.setContentsMargins(12, 8, 12, 5)
        toolbar.setSpacing(6)
        self.compact_home = button("首页", lambda: self.show_surface("home"), "surfaceNav")
        self.compact_home.setCheckable(True)
        self.compact_tools = button("工具", name="quiet")
        self.compact_menu = QMenu(self.compact_tools)
        self.compact_menu.addAction("全部工具 / 搜索", lambda: self.show_surface("catalog"))
        self.compact_menu.addSeparator()
        for group in TOOL_GROUPS:
            menu = self.compact_menu.addMenu(group["title"])
            for mode in group["modes"]:
                menu.addAction(TITLES[MODES.index(mode)], lambda checked=False, m=mode: self.switch_mode(MODES.index(m)))
        self.compact_menu.addSeparator()
        self.compact_menu.addAction("常用方案", lambda: self.show_surface("presets"))
        self.compact_menu.addAction("最近任务", lambda: self.show_surface("history"))
        self.compact_menu.addAction("添加文件 / 文件夹 / 链接…", self.choose_smart_sources)
        self.compact_menu.addAction("使用说明", self.show_help)
        self.compact_menu.addAction("转换格式与字段说明", self.show_conversion_help)
        self.compact_menu.addAction("恢复贴边小窗", self.apply_compact_geometry)
        self.compact_tools.setMenu(self.compact_menu)
        self.compact_history = button("记录", lambda: self.show_surface("history"), "quiet")
        self.compact_converter = button("转换", self.show_converter, "converterLaunch")
        self.compact_converter.setToolTip("打开独立格式转换浮窗 · Ctrl+Shift+C")
        self.compact_converter.setAccessibleName("独立格式转换浮窗")
        self.compact_notebook = button("记事提醒", lambda: self.notebook_requested.emit(), "notebookLaunch")
        self.compact_notebook.setToolTip("打开日历与事项浮窗 · Ctrl+Shift+N")
        for control in (self.compact_home, self.compact_tools, self.compact_history, self.compact_converter, self.compact_notebook):
            control.setStyleSheet("QPushButton { padding: 6px 7px; min-height: 20px; }")
            toolbar.addWidget(control)
        toolbar.insertStretch(3, 1)
        host_layout.addWidget(self.compact_toolbar)
        host_layout.addWidget(self.content_stack, 1)
        outer.addWidget(host, 1)
        self.main_layout = main_layout
        # Windows can clamp a large window to the available screen at high DPI.
        # Keep the action bar fixed and let the workspace scroll at small sizes.
        self.workspace_scroll = QScrollArea()
        self.workspace_scroll.setWidgetResizable(True)
        self.workspace_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        workspace = QWidget()
        workspace.setObjectName("workspace")
        right = QVBoxLayout(workspace)
        right.setContentsMargins(0, 0, 6, 0)
        right.setSpacing(18)
        self.workspace_scroll.setWidget(workspace)
        main_layout.addWidget(self.workspace_scroll, 1)
        top = QHBoxLayout()
        top.setContentsMargins(2, 0, 2, 3)
        heading = QVBoxLayout()
        heading.setSpacing(7)
        self.title = label("", "pageTitle")
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.subtitle = label("", "subTitle", True)
        heading.addWidget(self.title)
        heading.addWidget(self.subtitle)
        top.addLayout(heading, 1)
        self.local_badge = label("●  本地处理", "badge")
        top.addWidget(self.local_badge, 0, Qt.AlignmentFlag.AlignTop)
        right.addLayout(top)

        self.source_bar = SourceBar()
        self.source_bar.paths_dropped.connect(self.accept_sources)
        input_layout = QVBoxLayout(self.source_bar)
        input_layout.setContentsMargins(18, 15, 18, 15)
        input_layout.setSpacing(14)
        file_top = QHBoxLayout()
        self.file_top = file_top
        self.source_layout = input_layout
        file_top.setSpacing(8)
        source_mark = label("＋", "sourceMark")
        self.source_mark = source_mark
        source_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        source_mark.setFixedSize(40, 40)
        file_top.addWidget(source_mark)
        file_top.addSpacing(3)
        source_text = QVBoxLayout()
        source_text.setSpacing(4)
        source_heading = QHBoxLayout()
        source_heading.setSpacing(8)
        source_heading.addWidget(label("处理文件", "sourceTitle"))
        self.count_label = label("未选择", "countBadge")
        source_heading.addWidget(self.count_label)
        source_heading.addStretch()
        source_text.addLayout(source_heading)
        self.source_summary = ElidedLabel()
        self.source_summary.setObjectName("sourceSummary")
        source_text.addWidget(self.source_summary)
        file_top.addLayout(source_text, 1)
        file_top.addSpacing(10)
        self.add_files_button = button("添加文件", self.add_files, "sourceAdd")
        self.add_folder_button = button("文件夹", self.add_folder, "quiet")
        self.add_url_button = button("添加链接", self.add_urls, "sourceAdd")
        self.manage_button = button("文件清单", name="quiet")
        self.manage_button.setCheckable(True)
        self.manage_button.toggled.connect(self.toggle_queue)
        for control in [self.add_url_button, self.add_files_button, self.add_folder_button, self.manage_button]:
            file_top.addWidget(control)
        input_layout.addLayout(file_top)
        self.queue_panel = QWidget()
        queue_layout = QVBoxLayout(self.queue_panel)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(10)
        self.table = FileTable()
        self.table.setMinimumWidth(0)
        self.table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.table.paths_dropped.connect(self.accept_sources)
        queue_layout.addWidget(self.table)
        queue_actions = QHBoxLayout()
        self.up_button = button("↑ 上移", lambda: self.move_selected(-1), "quiet")
        self.down_button = button("↓ 下移", lambda: self.move_selected(1), "quiet")
        self.clear_button = button("移除所选", self.remove_selected, "quiet")
        for control in (self.up_button, self.down_button, self.clear_button):
            queue_actions.addWidget(control)
        queue_actions.addStretch()
        self.empty_hint = label("按清单顺序处理", "muted")
        queue_layout.addWidget(self.empty_hint)
        queue_layout.addLayout(queue_actions)
        input_layout.addWidget(self.queue_panel)
        self.queue_panel.hide()
        right.addWidget(self.source_bar)

        settings_card, settings_layout = card()
        self.settings_card = settings_card
        settings_top = QHBoxLayout()
        self.settings_top = settings_top
        self.settings_title = label("处理设置", "cardTitle")
        settings_top.addWidget(self.settings_title)
        settings_top.addStretch()
        self.columns_button = button("查看列名", self.show_columns, "quiet")
        self.undo_button = button("撤销原地改名", self.undo_rename, "quiet")
        self.export_button = button("导出规则", self.export_rules, "quiet")
        self.import_button = button("导入规则", self.import_rules, "quiet")
        self.guide_button = button("使用说明 / 示例", self.show_tool_guide, "quiet")
        self.preset_button = button("保存方案", self.save_current_preset, "quiet")
        self.preset_button.setToolTip("保存当前设置到常用方案；不保存密码和文件队列")
        settings_top.addWidget(self.guide_button)
        settings_top.addWidget(self.columns_button)
        settings_top.addWidget(self.undo_button)
        settings_top.addWidget(self.import_button)
        settings_top.addWidget(self.export_button)
        settings_top.addWidget(self.preset_button)
        self.settings_more = button("更多", name="quiet")
        self.settings_more_menu = QMenu(self.settings_more)
        self.settings_more.setMenu(self.settings_more_menu)
        settings_top.addWidget(self.settings_more)
        self.settings_more.hide()
        settings_layout.addLayout(settings_top)
        self.settings = QStackedWidget()
        self.settings.addWidget(self.build_word())
        self.settings.addWidget(self.build_web())
        self.settings.addWidget(self.build_convert())
        self.settings.addWidget(self.build_image())
        self.extra_panels = {}
        for mode in EXTRA_ORDER:
            panel = WorkflowPanel() if mode == "workflow" else OptionPanel(mode)
            panel.changed.connect(self.fit_settings)
            self.extra_panels[mode] = panel
            self.settings.addWidget(panel)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setMinimumWidth(0)
        self.settings_scroll.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setWidget(self.settings)
        settings_layout.addWidget(self.settings_scroll)
        right.addWidget(settings_card)
        right.addStretch(1)

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        action_layout = QVBoxLayout(action_bar)
        self.action_layout = action_layout
        action_layout.setContentsMargins(18, 13, 18, 14)
        action_layout.setSpacing(10)
        output_row = QHBoxLayout()
        output_row.setSpacing(10)
        output_row.addWidget(label("保存位置", "muted"))
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        self.output_edit = QLineEdit(str(Path(docs or str(Path.home())) / "青简处理结果"))
        self.output_edit.setObjectName("outputPath")
        self.output_edit.setReadOnly(True)
        self.output_edit.setMinimumWidth(0)
        self.choose_output_button = button("更改", self.choose_output, "quiet")
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.choose_output_button)
        action_layout.addLayout(output_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        action_layout.addWidget(self.progress)
        bottom = QHBoxLayout()
        self.action_bottom = bottom
        self.status = StatusLabel()
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.status.setText("就绪 · 每次处理自动创建独立结果文件夹")
        self.status.setMinimumWidth(0)
        self.status.setMaximumHeight(42)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bottom.addWidget(self.status, 1)
        self.result_button = button("查看结果", self.show_results)
        self.result_button.setEnabled(False)
        self.stop_button = button("停止任务", self.stop_task, "danger")
        self.stop_button.hide()
        self.start_button = button("开始处理  →", self.start_task, "primary")
        for control in [self.result_button, self.stop_button, self.start_button]:
            bottom.addWidget(control)
        action_layout.addLayout(bottom)
        main_layout.addWidget(action_bar)
        self.home_page = HomePage(settings_path=Path(data_path).parent / "home-settings.json" if data_path else None)
        self.catalog_page = ToolCatalogPage()
        self.presets_page = PresetsPage()
        self.history_page = HistoryPage()
        self.surfaces = {"home": self.home_page, "catalog": self.catalog_page,
                         "presets": self.presets_page, "history": self.history_page}
        for page in self.surfaces.values():
            self.content_stack.addWidget(page)
        self.home_page.group_requested.connect(self.open_tool_group)
        self.home_page.catalog_requested.connect(lambda: self.open_tool_group("all"))
        self.home_page.notebook_requested.connect(self.notebook_requested)
        self.catalog_page.tool_requested.connect(lambda mode: self.switch_mode(MODES.index(mode)))
        self.catalog_page.scene_requested.connect(self.open_scene)
        self.presets_page.preset_requested.connect(self.open_preset)
        self.presets_page.delete_requested.connect(self.remove_preset)
        self.history_page.result_requested.connect(self.show_history_result)
        self.history_page.open_folder_requested.connect(self.open_history_folder)
        self.history_page.clear_requested.connect(self.clear_history)
        self.search_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self.search_shortcut.activated.connect(self.focus_tool_search)
        self.notebook_shortcut = QShortcut(QKeySequence("Ctrl+Shift+N"), self)
        self.notebook_shortcut.activated.connect(self.notebook_requested.emit)
        self.converter_shortcut = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        self.converter_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.converter_shortcut.activated.connect(self.show_converter)
        self.word_scene_options["word"] = copy.deepcopy(self.get_options("word"))
        self.restore_redaction_memory()
        for control in self.findChildren(QAbstractSpinBox):
            control.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        self.refresh_workbench()
        self.switch_mode(0)
        if start_home:
            self.show_surface("home")
        self._ui_ready = True
        self.update_responsive_layout()

    def apply_compact_geometry(self):
        if self.isMaximized():
            self.showNormal()
        handle = self.windowHandle()
        screen = (handle.screen() if handle else QApplication.primaryScreen()).availableGeometry()
        geometry, frame = self.geometry(), self.frameGeometry()
        margins = QMargins(max(0, geometry.left() - frame.left()), max(0, geometry.top() - frame.top()),
                           max(0, frame.right() - geometry.right()), max(0, frame.bottom() - geometry.bottom()))
        self.setMinimumSize(compact_window_minimum_size(screen, margins))
        self.setGeometry(compact_window_geometry(screen, margins))

    def showEvent(self, event):
        super().showEvent(event)
        if not self._frame_corrected:
            self._frame_corrected = True
            QTimer.singleShot(0, self.apply_compact_geometry)

    def changeEvent(self, event):
        super().changeEvent(event)
        if getattr(self, "_ui_ready", False) and event.type() == event.Type.WindowStateChange:
            if event.oldState() & Qt.WindowState.WindowMaximized and not self.isMaximized():
                QTimer.singleShot(0, self.apply_compact_geometry)
            QTimer.singleShot(0, self.update_responsive_layout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, "_ui_ready", False):
            self.update_responsive_layout()

    def update_responsive_layout(self):
        compact = self.width() < 900
        changed = compact != self._layout_compact
        self._layout_compact = compact
        self.sidebar.setVisible(not compact)
        self.compact_toolbar.setVisible(compact)
        # On a high-DPI display a third-width window can be under 300 logical
        # pixels. Keep both highlighted float buttons legible; history remains
        # available from the tools menu at that width.
        self.compact_history.setVisible(self.width() >= 340)
        self.compact_home.setChecked(self.content_stack.currentWidget() == self.home_page)
        if changed:
            self.home_page.set_compact(compact)
            self.catalog_page.set_compact(compact)
            self.main_layout.setContentsMargins(*( (12, 8, 12, 10) if compact else (28, 25, 26, 20) ))
            self.main_layout.setSpacing(10 if compact else 16)
            self.workspace_scroll.widget().layout().setSpacing(10 if compact else 18)
            self.source_layout.setContentsMargins(*( (12, 10, 12, 10) if compact else (18, 15, 18, 15) ))
            self.action_layout.setContentsMargins(*( (12, 9, 12, 10) if compact else (18, 13, 18, 14) ))
            self.settings_card.layout().setContentsMargins(*( (12, 12, 12, 12) if compact else (22, 18, 22, 20) ))
            self.source_mark.setVisible(not compact)
            self.title.setStyleSheet("font-size: 20px;" if compact else "")
            self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if compact else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            if not hasattr(self, "source_actions"):
                self.source_actions = QGridLayout()
                self.source_actions.setSpacing(6)
            controls = (self.add_url_button, self.add_files_button, self.add_folder_button, self.manage_button)
            for control in controls:
                self.file_top.removeWidget(control)
                self.source_actions.removeWidget(control)
            if compact:
                self.source_layout.insertLayout(1, self.source_actions)
                for control in controls:
                    control.setStyleSheet("QPushButton { padding: 6px 8px; }")
                self.action_bottom.removeWidget(self.status)
                self.action_layout.insertWidget(2, self.status)
            else:
                self.source_layout.removeItem(self.source_actions)
                for control in controls:
                    self.file_top.addWidget(control)
                    control.setStyleSheet("")
                self.action_layout.removeWidget(self.status)
                self.action_bottom.insertWidget(0, self.status, 1)
            QTimer.singleShot(0, self.fit_settings)
        self.start_button.setVisible(not(compact and self._busy))
        narrow = compact and self.width() < 360
        self.title.setWordWrap(compact)
        self.local_badge.setVisible(not narrow)
        self.settings_title.setWordWrap(narrow)
        self.empty_hint.setWordWrap(True)
        if compact:
            controls = (self.add_url_button, self.add_files_button, self.add_folder_button, self.manage_button)
            for control in controls:
                self.source_actions.removeWidget(control)
            visible = [control for control in controls if not control.isHidden()]
            for index, control in enumerate(visible):
                self.source_actions.addWidget(control, index // 2 if narrow else 0, index % 2 if narrow else index)
        self._update_settings_actions()

    def _update_settings_actions(self):
        compact = bool(self._layout_compact)
        available = {
            self.guide_button: self.mode in GUIDES,
            self.columns_button: self.mode in {"excel_merge", "excel_compare", "excel_clean", "template_fill", "check_materials", "excel_audit", "excel_lookup", "excel_print"},
            self.undo_button: self.mode == "file_organize",
            self.import_button: self.mode == "word" or self.mode in EXTRA_ORDER,
            self.export_button: self.mode == "word" or self.mode in EXTRA_ORDER,
            self.preset_button: True,
        }
        self.settings_more_menu.clear()
        for control, enabled in available.items():
            is_guide = control is self.guide_button
            control.setVisible(enabled and (not compact or is_guide))
            if enabled and not (compact and is_guide):
                action = self.settings_more_menu.addAction(control.text(), control.click)
                action.setEnabled(control.isEnabled())
        self.guide_button.setText("说明示例" if compact else "使用说明 / 示例")
        self.settings_more.setVisible(compact)
        self.settings_more.setEnabled(not self._busy)

    def build_word(self) -> QWidget:
        tabs = QTabWidget()
        format_page = QWidget()
        form = QFormLayout(format_page)
        form.setContentsMargins(3, 12, 3, 4)
        form.setHorizontalSpacing(25)
        self.f_enabled = checkbox("统一正文格式", True)
        self.f_headings = checkbox("保留标题样式及字号", True)
        row = QHBoxLayout()
        row.setSpacing(24)
        row.addWidget(self.f_enabled)
        row.addWidget(self.f_headings)
        row.addStretch()
        form.addRow(row)
        self.f_font = QComboBox()
        self.f_font.setEditable(True)
        self.f_font.addItems(["宋体", "仿宋", "微软雅黑", "黑体", "楷体", "Arial"])
        self.f_size = spin(12, 5, 72, " 磅")
        font_row = QHBoxLayout()
        font_row.setSpacing(14)
        font_row.addWidget(self.f_font, 2)
        font_row.addWidget(label("字号"))
        font_row.addWidget(self.f_size, 1)
        form.addRow("正文字体", font_row)
        self.f_line = spin(1.5, 0.8, 4, " 倍")
        self.f_margin = spin(2.54, 0.3, 8, " cm", 2)
        layout_row = QHBoxLayout()
        layout_row.setSpacing(14)
        layout_row.addWidget(self.f_line)
        layout_row.addWidget(label("四边页边距"))
        layout_row.addWidget(self.f_margin)
        form.addRow("行距", layout_row)
        form.addRow(label("Word可统一正文、表格和页眉页脚。TXT用于替换、脱敏及空行清理。", "muted", True))
        tabs.addTab(format_page, "通用格式")
        self.official_panel = OfficialPanel()
        self.official_panel.changed.connect(self.official_changed)
        self.official_panel.layout_changed.connect(lambda: QTimer.singleShot(0, self.fit_settings))
        self.official_panel.preflight_requested.connect(self.preflight_official)
        self.official_panel.template_requested.connect(self.generate_official_template)
        tabs.addTab(self.official_panel, "公文排版")
        self.word_tabs = tabs

        mask_page = QWidget()
        mask = QVBoxLayout(mask_page)
        mask.setContentsMargins(3, 12, 3, 3)
        row = QHBoxLayout()
        row.addWidget(label("批量粘贴的默认替换文字", "muted"))
        self.m_custom_replacement = QLineEdit("[内容已隐去]")
        self.m_custom_replacement.setMinimumWidth(0)
        row.addWidget(self.m_custom_replacement, 1)
        mask.addLayout(row)
        self.m_custom = SensitiveRuleEditor(default_replacement=self.m_custom_replacement.text)
        self.m_custom_replacement.textChanged.connect(self.m_custom.refresh_default_replacement)
        self.m_custom.changed.connect(lambda: QTimer.singleShot(0, self.fit_settings))
        mask.addWidget(self.m_custom)
        self.redaction_remember_button = button("记住当前规则", self.remember_redaction_rules, "sourceAdd")
        self.redaction_library_button = button("规则记忆库", self.open_redaction_library)
        for control in (self.redaction_remember_button, self.redaction_library_button):
            control.setStyleSheet("QPushButton { padding: 5px 8px; }")
            self.m_custom.tools_layout.addWidget(control)
        self.redaction_memory_summary = label("", "muted", True)
        mask.addWidget(self.redaction_memory_summary)
        self.refresh_redaction_memory()
        mask.addWidget(label("常见号码（可选）", "muted"))
        self.m_phone = checkbox("手机号")
        self.m_id = checkbox("身份证号")
        self.m_email = checkbox("邮箱")
        self.mask_replacements = {}
        for key, control, default in (("mask_phone_replacement", self.m_phone, "[手机号已隐去]"),
                                       ("mask_id_replacement", self.m_id, "[证件号码已隐去]"),
                                       ("mask_email_replacement", self.m_email, "[邮箱已隐去]")):
            row = QHBoxLayout()
            control.setMinimumWidth(100)
            row.addWidget(control)
            row.addWidget(label("替换为"))
            edit = QLineEdit(default)
            edit.setPlaceholderText("自定义替换文字；留空即删除")
            self.mask_replacements[key] = edit
            row.addWidget(edit, 1)
            mask.addLayout(row)
        mask.addWidget(label("规则脱敏只覆盖可识别文字。图片、附件、链接地址需人工复核；存在未覆盖的修订等内容时会拦截。", "notice", True))
        tabs.addTab(mask_page, "脱敏")

        replace_page = QWidget()
        replace = QVBoxLayout(replace_page)
        replace.setContentsMargins(3, 10, 3, 3)
        self.rules_table = QTableWidget(0, 3)
        self.rules_table.setHorizontalHeaderLabels(["查找内容", "替换为（留空即删除）", "匹配方式"])
        self.rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.rules_table.setColumnWidth(2, 100)
        self.rules_table.verticalHeader().hide()
        self.rules_table.verticalHeader().setDefaultSectionSize(32)
        replace.addWidget(self.rules_table)
        rule_row = QHBoxLayout()
        rule_row.addWidget(button("＋ 添加规则", self.add_rule))
        rule_row.addWidget(button("删除所选规则", self.remove_rule))
        rule_row.addStretch()
        rule_row.addWidget(label("从上到下依次执行", "muted"))
        replace.addLayout(rule_row)
        tabs.addTab(replace_page, "查找替换")

        clean_page = QWidget()
        clean = QVBoxLayout(clean_page)
        clean.setContentsMargins(3, 10, 3, 3)
        self.c_blank = checkbox("删除空白段落")
        self.c_metadata = checkbox("清除作者、公司等文档属性")
        self.c_comments = checkbox("删除批注")
        clean_row = QHBoxLayout()
        for control in [self.c_blank, self.c_metadata, self.c_comments]:
            clean_row.addWidget(control)
        clean.addLayout(clean_row)
        self.c_delete = QPlainTextEdit()
        self.c_delete.setPlaceholderText("删除包含指定文字的整段内容，每行一条。请仔细检查；表格中只删除相应段落。")
        clean.addWidget(self.c_delete)
        tabs.addTab(clean_page, "内容清理")
        tabs.currentChanged.connect(lambda _: self.fit_settings())
        return tabs

    def official_changed(self):
        enabled = self.official_panel.enabled.isChecked()
        for control in (self.f_enabled, self.f_font, self.f_size, self.f_line, self.f_margin, self.f_headings):
            control.setEnabled(not enabled)
        if hasattr(self, "status") and self.mode == "word":
            checking = enabled and self.official_panel.operation.currentData() == "check"
            self.start_button.setText("开始检查  →" if checking else "开始处理  →")
            self.status.setText("仅检查 · 不执行格式、脱敏或替换" if checking else "公文排版已启用 · 使用公文页的版式设置" if enabled else "就绪 · 使用通用 Word 处理设置")

    def preflight_official(self):
        if self.worker or self._busy:
            return
        self.start_task(check_official=True)

    def generate_official_template(self):
        if self.worker or self._busy:
            return
        try:
            options = self.official_panel.options()
        except Exception as exc:
            QMessageBox.warning(self, "请检查公文设置", str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("生成公文样板")
        dialog.setMinimumWidth(490)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        title = QLineEdit()
        title.setPlaceholderText("例如：关于做好有关工作的通知")
        issuer = QLineEdit(options.get("company_name", ""))
        issued = QLineEdit()
        issued.setPlaceholderText("填写实际签发或会议通过日期；留空保留占位")
        form.addRow("公文种类", QLabel(options["doc_type"]))
        form.addRow("标题", title)
        form.addRow("发文单位署名", issuer)
        form.addRow("成文日期", issued)
        layout.addLayout(form)
        layout.addWidget(label("保存到下方选择的结果位置，按标题命名。样板正文为示例，请自行替换。", "muted", True))
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", dialog.reject))
        generate = button("生成样板", name="primary")
        def accept():
            if not title.text().strip():
                QMessageBox.information(dialog, "请填写标题", "输入公文标题后即可生成样板。")
                return
            dialog.accept()
        generate.clicked.connect(accept)
        actions.addWidget(generate)
        layout.addLayout(actions)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            from .core.common import safe_name
            from .core.official_reports import save_official_template
            folder = new_run_directory(Path(self.output_edit.text()))
            target = folder / (safe_name(title.text(), limit=160) + ".docx")
            save_official_template(target, options, title.text(), issuer.text(), issued.text(), options["doc_type"])
            self.last_result = {"folder": str(folder), "records": [{"状态": "成功", "输入": "生成公文样板", "输出": str(target)}],
                                "total": 1, "cancelled": False, "fatal": "", "report_error": ""}
            self.last_context = None
            self.profile_store.record_task("word", "生成公文样板", self.last_result, "official")
            self.refresh_workbench()
            self.result_button.setEnabled(True)
            self.progress.setValue(100)
            self.status.setText(f"公文样板已生成 · {target.name}")
        except Exception as exc:
            QMessageBox.warning(self, "样板生成失败", str(exc))

    def build_web(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(3, 8, 3, 4)
        layout.addWidget(label("上方“添加链接”支持每行一个网址，也可添加本地 HTML / MHTML 文件。", "muted", True))
        self.w_images = checkbox("保留网页中的图片", True)
        layout.addWidget(self.w_images)
        layout.addWidget(label("支持文章标题、正文、图片、基础列表与表格。在线链接需要联网读取；本地 HTML / MHTML 可离线转换。", "notice", True))
        layout.addWidget(label("登录、验证码或依赖脚本加载的页面可能无法直接提取；可先在浏览器中另存为 HTML。", "muted", True))
        layout.addStretch()
        return page

    def build_convert(self) -> QWidget:
        page = QWidget()
        layout = QFormLayout(page)
        layout.setContentsMargins(3, 10, 3, 4)
        self.p_mode = QComboBox()
        self.p_mode.addItems(["可编辑文本 · 重新排版", "保留页面外观 · 整页图片"])
        layout.addRow("PDF → Word", self.p_mode)
        self.p_password = QLineEdit()
        self.p_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.p_password.setPlaceholderText("仅加密 PDF 需要；不保存密码")
        layout.addRow("PDF 密码", self.p_password)
        self.engine_label = label("", "muted", True)
        layout.addRow(self.engine_label)
        layout.addRow(label("Word 自动转为 PDF，PDF 自动转为 DOCX，可混合加入队列。可编辑模式会重新排版；扫描页自动保留为图片，当前版本不含 OCR。", "notice", True))
        return page

    def build_image(self) -> QWidget:
        page = QWidget()
        layout = QFormLayout(page)
        layout.setContentsMargins(3, 10, 3, 4)
        self.i_mode = QComboBox()
        self.i_mode.addItems(["按四边百分比裁剪", "按指定比例裁剪", "按目标尺寸裁剪"])
        layout.addRow("裁剪方式", self.i_mode)
        self.image_stack = QStackedWidget()
        self.image_stack.setFixedHeight(48)
        edge = QWidget()
        edge_layout = QHBoxLayout(edge)
        edge_layout.setContentsMargins(0, 0, 0, 0)
        self.i_margins = {}
        for key, title in [("left", "左"), ("top", "上"), ("right", "右"), ("bottom", "下")]:
            edge_label = label(title)
            edge_label.setFixedWidth(20)
            edge_layout.addWidget(edge_label)
            control = spin(0, 0, 49.9, "%")
            control.setMinimumWidth(68)
            self.i_margins[key] = control
            edge_layout.addWidget(control)
        self.image_stack.addWidget(edge)
        ratio = QWidget()
        ratio_layout = QHBoxLayout(ratio)
        ratio_layout.setContentsMargins(0, 0, 0, 0)
        self.i_ratio_w, self.i_ratio_h = spin(1, 0.1, 1000), spin(1, 0.1, 1000)
        ratio_layout.addWidget(self.i_ratio_w)
        ratio_layout.addWidget(label("∶"))
        ratio_layout.addWidget(self.i_ratio_h)
        self.image_stack.addWidget(ratio)
        size = QWidget()
        size_layout = QHBoxLayout(size)
        size_layout.setContentsMargins(0, 0, 0, 0)
        self.i_width = spin(800, 1, 30000, " px", 0)
        self.i_height = spin(800, 1, 30000, " px", 0)
        size_layout.addWidget(self.i_width)
        size_layout.addWidget(label("×"))
        size_layout.addWidget(self.i_height)
        self.image_stack.addWidget(size)
        layout.addRow("裁剪参数", self.image_stack)
        self.i_mode.currentIndexChanged.connect(self.image_stack.setCurrentIndex)
        self.i_anchor = QComboBox()
        self.i_anchor.addItems(["居中", "左上", "顶部居中", "底部居中"])
        self.i_format = QComboBox()
        self.i_format.addItems(["保留原格式", "PNG", "JPG"])
        row = QHBoxLayout()
        row.addWidget(self.i_anchor)
        row.addWidget(label("输出格式"))
        row.addWidget(self.i_format)
        layout.addRow("对齐位置", row)
        layout.addRow(label("自动校正照片方向并移除输出 EXIF；尺寸模式只缩小、不放大。透明图片转 JPG 时使用白色背景。", "notice", True))
        return page

    def switch_mode(self, index: int):
        if self.worker or self._busy:
            return
        self.mode = MODES[index]
        self.active_scene = self._word_scene if self.mode == "word" and self._word_scene != "word" else ""
        self.content_stack.setCurrentWidget(self.job_page)
        for control in self.surface_buttons.values():
            control.setChecked(False)
        group = next((group for group in TOOL_GROUPS if self.mode in group["modes"]), None)
        self.context_heading.setText(group["title"] if group else "当前工具")
        self.context_heading.show()
        self.sidebar_space.hide()
        self.nav_scroll.show()
        for i, control in enumerate(self.nav_buttons):
            control.setChecked(i == index)
            control.setVisible(not group or MODES[i] in group["modes"])
        self.title.setText(TITLES[index])
        self.subtitle.setText(DESCRIPTIONS[index])
        self.settings.setCurrentIndex(index)
        self.nav_scroll.ensureWidgetVisible(self.nav_buttons[index], 0, 20)
        self.add_url_button.setVisible(index == 1)
        self.import_button.setVisible(index == 0 or self.mode in EXTRA_ORDER)
        self.export_button.setVisible(index == 0 or self.mode in EXTRA_ORDER)
        self.columns_button.setVisible(self.mode in {"excel_merge", "excel_compare", "excel_clean", "template_fill", "check_materials",
                                                     "excel_audit", "excel_lookup", "excel_print"})
        self.undo_button.setVisible(self.mode == "file_organize")
        self.guide_button.setVisible(self.mode in GUIDES)
        self.add_files_button.setVisible(self.mode != "check_materials")
        self.start_button.setText("生成预览  →" if self.mode == "file_organize" else "开始处理  →")
        if self.mode == "word" and self.official_panel.enabled.isChecked() and self.official_panel.operation.currentData() == "check":
            self.start_button.setText("开始检查  →")
        self.empty_hint.setText({"excel_compare": "第 1 份为旧表，第 2 份为新表", "document_compare": "第 1 份为旧版，第 2 份为新版",
                                "excel_lookup": "第 1 份为目标表，第 2 份为参考表", "submission_bind": "按文件清单顺序装订，可上移或下移调整"}.get(self.mode, "按清单顺序处理"))
        self.manage_button.setChecked(False)
        self.workspace_scroll.verticalScrollBar().setValue(0)
        self.refresh_table()
        QTimer.singleShot(0, self.fit_settings)
        if index == 2:
            engine = find_libreoffice()
            self.engine_label.setText("Word 排版引擎：已就绪" if engine else "Word 排版引擎：未检测到，请保留“程序文件/engines”；源码运行可通过启动器补齐。PDF 转 Word 可用。")
        if self._ui_ready:
            self.update_responsive_layout()

    def refresh_workbench(self):
        history, presets = self.profile_store.history, self.profile_store.presets
        self.presets_page.set_presets(presets)
        self.history_page.set_history(history)

    def show_converter(self):
        if self.converter_window is None:
            self.converter_window = ConverterWindow(self._converter_settings)
            self.converter_window.setWindowIcon(icon())
            self.converter_window.submitted.connect(self.start_quick_action)
            self.converter_window.cancel_requested.connect(self.stop_task)
            self.converter_window.results_requested.connect(self.show_quick_results)
            self.converter_window.folder_requested.connect(self.open_quick_folder)
            self.converter_window.choose_folder_requested.connect(self.choose_converter_folder)
            self.converter_window.help_requested.connect(self.show_conversion_help)
            self.converter_window.dock_requested.connect(self.dock_converter)
            QApplication.instance().aboutToQuit.connect(self.converter_window.shutdown)
            if self.worker is not None and self._quick_active:
                self.worker.progress.connect(self.converter_window.update_progress)
                self.converter_window.progress.show()
        self.converter_window.set_url_folder(self.conversion_session.url_folder)
        self.converter_window.set_busy(self._busy, own_job=self._quick_active)
        if self._quick_active:
            self.converter_window.notice(self.conversion_session.status_text)
        elif self.quick_result and not self.converter_window._has_result:
            # Reopening the float restores the last conversion result.
            self.converter_window._last_outputs = list(self.conversion_session._last_outputs)
            self.converter_window._has_result = True
            self.converter_window.set_busy(False)
            self.converter_window.notice(self.conversion_session.status_text)
        self.converter_window.show_window()

    def converter_is_visible(self):
        return self.converter_window is not None and self.converter_window.isVisible()

    def dock_converter(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.converter_window is not None:
            self.converter_window.save_position()
            self.converter_window.hide()

    def choose_converter_folder(self):
        if self._busy:
            return
        session = self.conversion_session
        folder = QFileDialog.getExistingDirectory(self.converter_window or self, "链接转换结果保存位置", str(session.url_folder))
        if folder:
            try:
                session.set_folder(folder)
                self.update_converter_folder(session.url_folder)
            except OSError as exc:
                self._quick_notice("保存位置未能记住，请检查目录权限：" + str(exc))

    def update_converter_folder(self, folder):
        if self.converter_window is not None:
            self.converter_window.set_url_folder(folder)

    def _quick_notice(self, message):
        self.conversion_session.notice(message)
        if self.converter_window is not None:
            self.converter_window.notice(message)

    def start_quick_action(self, action_id, sources):
        if self.worker or self._busy:
            self._quick_notice("已有任务正在处理，完成后可继续转换。")
            return False
        panel = self.conversion_session
        if not isinstance(action_id, str) or not sources:
            self._quick_notice("请选择有效的快捷通道和待处理内容。")
            return False
        if len(sources) > QUICK_MAX_INPUTS:
            self._quick_notice(f"一次快捷转换最多处理 {QUICK_MAX_INPUTS} 项，请分批拖入。")
            return False
        if not any(not validate_quick_sources(action_id, [source]) for source in sources):
            self._quick_notice("\n".join(validate_quick_sources(action_id, sources)[:3]))
            return False
        if not self.converter_is_visible():
            self.show_converter()
        self._quick_active = True
        self._quick_action_id = action_id
        self.last_context = None
        self.active_context = None
        panel.begin(action_id, len(sources), title=conversion_target_title(action_id))
        if self.converter_window is not None:
            self.converter_window.begin(action_id, len(sources))
        self.set_busy(True)
        self.worker = QuickActionWorker(action_id, list(sources), panel.url_folder, self)
        self.worker.progress.connect(panel.update_progress)
        if self.converter_window is not None:
            self.worker.progress.connect(self.converter_window.update_progress)
        self.worker.completed.connect(self.on_quick_complete)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()
        return True

    def on_quick_complete(self, result):
        panel = self.conversion_session
        panel.finish(result)
        if self.converter_window is not None:
            self.converter_window.finish(result)
        records = [{"输入": row.get("source", ""), "输出": row.get("output", ""),
                    "状态": {"success": "成功", "failed": "失败", "cancelled": "已停止"}.get(row.get("status"), "失败"),
                    "原因": row.get("error", ""), "提示": row.get("warnings", []),
                    "处理统计": row.get("changes", 0)} for row in result.get("records", [])]
        outputs = result.get("outputs", [])
        folder = str(Path(outputs[0]).parent) if outputs else ""
        self.last_result = {"kind": "quick", "folder": folder, "records": records,
                            "cancelled": result.get("cancelled", False), "total": result.get("total", len(records)),
                            "fatal": "；".join(str(value) for value in result.get("errors", [])) if not records else "", "report_error": ""}
        self.last_context = None
        self.quick_result = copy.deepcopy(self.last_result)
        mode = {"web_to_word": "web", "to_text": "document_export", "image_to_jpg": "image_pack", "image_to_png": "image_pack"}.get(self._quick_action_id, "convert")
        self.profile_store.record_task(mode, "快捷 · " + conversion_target_title(self._quick_action_id), self.last_result, "quick:" + self._quick_action_id)
        self.refresh_workbench()
        self.result_button.setEnabled(bool(records))
        self.status.setText(panel.status_text)
        self.progress.setValue(100)

    def open_quick_folder(self):
        outputs = self.conversion_session._last_outputs
        if outputs:
            first = Path(outputs[0])
            folder = first if first.is_dir() else first.parent
            if folder.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def show_quick_results(self):
        if self.quick_result:
            owner = self.converter_window if self.converter_is_visible() else self
            ResultDialog(self.quick_result, owner, title="快捷转换 · 结果详情").exec()

    def show_surface(self, name):
        if self.worker or self._busy or name not in self.surfaces:
            return
        self.refresh_workbench()
        self.content_stack.setCurrentWidget(self.surfaces[name])
        self.context_heading.hide()
        self.nav_scroll.hide()
        self.sidebar_space.show()
        for key, control in self.surface_buttons.items():
            control.setChecked(key == name)
        if self._ui_ready:
            self.compact_home.setChecked(name == "home")

    def open_tool_group(self, group_id):
        if self.worker or self._busy:
            return
        self.catalog_page.search_edit.clear()
        self.catalog_page.set_group(group_id)
        self.show_surface("catalog")

    def focus_tool_search(self):
        if self.worker or self._busy:
            return
        self.show_surface("catalog")
        self.catalog_page.search_edit.setFocus()
        self.catalog_page.search_edit.selectAll()

    def load_scene_example(self, scene_id):
        if self.worker or self._busy:
            return False
        if scene_id == "materials" and self.open_scene(scene_id):
            return self.load_tool_example("check_materials")
        return False

    def open_scene(self, scene_id):
        if self.worker or self._busy:
            return False
        scene = next((item for item in SCENES if item["id"] == scene_id), None)
        if scene is None:
            return False
        if scene["mode"] == "word" and not self.select_word_context(scene_id):
            return False
        self.switch_mode(MODES.index(scene["mode"]))
        self.active_scene = scene_id
        self.title.setText(scene["title"])
        self.subtitle.setText(scene["description"])
        if scene["mode"] == "word":
            tab = next((i for i in range(self.word_tabs.count()) if self.word_tabs.tabText(i) == scene["tab"]), 0)
            self.word_tabs.setCurrentIndex(tab)
        self.status.setText("已载入此场景设置 · 添加文件并核对设置后开始处理")
        return True

    def select_word_context(self, scene_id):
        if self._word_scene == scene_id:
            return True
        try:
            self.word_scene_options[self._word_scene] = copy.deepcopy(self.get_options("word"))
            values = copy.deepcopy(self.word_scene_options.get(scene_id, self.word_scene_options["word"]))
            if scene_id not in self.word_scene_options:
                values.update(format_enabled=False, official_enabled=scene_id == "official",
                              mask_phone=scene_id == "redact", mask_id=scene_id == "redact", mask_email=False,
                              custom_sensitive=[], custom_sensitive_rules=[], replacements=[], delete_paragraphs=[],
                              clean_metadata=False, remove_comments=False, remove_blank_paragraphs=False)
            self.apply_word_options(values)
            self._word_scene = scene_id
            return True
        except Exception as exc:
            QMessageBox.warning(self, "请检查当前规则", str(exc))
            return False

    def choose_smart_sources(self):
        if self.worker or self._busy:
            return
        menu = QMenu(self)
        file_action = menu.addAction("选择文件…")
        folder_action = menu.addAction("选择材料文件夹…")
        link_action = menu.addAction("添加网页链接…")
        selected = menu.exec(self.compact_tools.mapToGlobal(self.compact_tools.rect().bottomLeft()))
        if selected == file_action:
            paths, _ = QFileDialog.getOpenFileNames(self, "选择文件，推荐适用操作", "", "办公文件 (*.docx *.doc *.txt *.pdf *.xlsx *.xlsm *.csv *.tsv *.pptx *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.html *.htm *.xhtml *.mht *.mhtml);;全部文件 (*)")
            self.recommend_sources(paths)
        elif selected == folder_action:
            path = QFileDialog.getExistingDirectory(self, "选择材料文件夹，推荐适用操作")
            if path:
                self.recommend_sources([path])
        elif selected == link_action:
            text, accepted = QInputDialog.getMultiLineText(self, "添加网页链接", "每行一个 http:// 或 https:// 链接：")
            if accepted:
                self.recommend_sources([line.strip() for line in text.splitlines() if line.strip()])

    def recommend_sources(self, sources):
        if self.worker or self._busy or not sources:
            return
        dialog = SourceActionDialog(sources, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_action:
            self.apply_source_action(dialog.selected_action)

    def apply_source_action(self, action):
        if self.worker or self._busy or action.get("mode") not in MODES:
            return False
        if action.get("scene"):
            if not self.open_scene(action["scene"]):
                return False
        else:
            if action["mode"] == "word" and action.get("tab") and not self.select_word_context(action["id"]):
                return False
            self.switch_mode(MODES.index(action["mode"]))
        if action.get("tab") and action["mode"] == "word":
            for index in range(self.word_tabs.count()):
                if self.word_tabs.tabText(index) == action["tab"]:
                    self.word_tabs.setCurrentIndex(index)
        if action.get("action") and action["mode"] in self.extra_panels:
            self.extra_panels[action["mode"]].set_options({"action": action["action"]})
        if action.get("template_path"):
            self.extra_panels["template_fill"].controls["template_path"].setText(action["template_path"])
        if action.get("files") or action.get("template_path"):
            self.files[self.mode].clear()
            self.queue_states[self.mode].clear()
        self.accept_sources(action.get("files", []))
        if action.get("template_path"):
            self.status.setText("已选择 PPT 模板 · 请添加 Excel / CSV 数据表，核对模板字段后开始")
        return True

    def apply_options(self, mode, values):
        """Validate an entire saved configuration before mutating its controls."""
        if not isinstance(values, dict):
            raise ValueError("方案设置格式无效。")
        if mode == "word":
            validate_options(mode, values)
            self.apply_word_options(values)
        elif mode in self.extra_panels:
            self.extra_panels[mode].set_options(values)
        elif mode == "web":
            if not isinstance(values.get("include_images", True), bool):
                raise ValueError("网页图片设置无效。")
            self.w_images.setChecked(values.get("include_images", True))
        elif mode == "convert":
            if values.get("pdf_mode", "text") not in {"text", "appearance"} or not isinstance(values.get("password", ""), str):
                raise ValueError("文档转换设置无效。")
            self.p_mode.setCurrentIndex(["text", "appearance"].index(values.get("pdf_mode", "text")))
            self.p_password.setText(values.get("password", ""))
        elif mode == "image":
            choices = (("mode", self.i_mode, ["margins", "ratio", "size"]),
                       ("anchor", self.i_anchor, ["center", "top-left", "top", "bottom"]),
                       ("format", self.i_format, ["original", "png", "jpg"]))
            numbers = {**self.i_margins, "ratio_w": self.i_ratio_w, "ratio_h": self.i_ratio_h,
                       "width": self.i_width, "height": self.i_height}
            for key, _, allowed in choices:
                if key in values and values[key] not in allowed:
                    raise ValueError("图片方案包含未知选项。")
            for key, control in numbers.items():
                if key in values and (isinstance(values[key], bool) or not isinstance(values[key], (int, float))
                    or not math.isfinite(values[key]) or not control.minimum() <= values[key] <= control.maximum()):
                    raise ValueError("图片裁剪参数超出允许范围。")
            for key, control, allowed in choices:
                if key in values:
                    control.setCurrentIndex(allowed.index(values[key]))
            for key, control in numbers.items():
                if key in values:
                    control.setValue(values[key])
        else:
            raise ValueError("方案使用了未知工具。")

    def save_current_preset(self):
        if self.worker or self._busy:
            return
        try:
            values = self.get_options()
            if self.mode == "word":
                validate_options("word", values)
        except Exception as exc:
            QMessageBox.warning(self, "请检查处理规则", str(exc))
            return
        name, accepted = QInputDialog.getText(self, "保存为常用方案", "方案名称", text=self.title.text())
        if not accepted or not name.strip():
            return
        try:
            self.profile_store.save_preset(name.strip(), self.mode, values, self.active_scene)
            self.refresh_workbench()
            self.status.setText("已保存常用方案 · 不保存密码和文件队列")
            self.show_profile_error()
        except Exception as exc:
            QMessageBox.warning(self, "方案保存失败", str(exc))

    def open_preset(self, preset_id):
        if self.worker or self._busy:
            return False
        item = next((item for item in self.profile_store.presets if item["id"] == preset_id), None)
        if item is None:
            return False
        try:
            if item["mode"] == "word":
                self.word_scene_options[self._word_scene] = copy.deepcopy(self.get_options("word"))
            self.apply_options(item["mode"], item["options"])
            if item["mode"] == "word":
                self._word_scene = item.get("scene") or "word"
            self.switch_mode(MODES.index(item["mode"]))
            self.active_scene = item.get("scene", "")
            if self.active_scene in {"official", "redact"}:
                self.word_tabs.setCurrentIndex(1 if self.active_scene == "official" else 2)
            self.status.setText("已载入方案「" + item["name"] + "」· 请添加或核对文件与设置")
            return True
        except Exception as exc:
            QMessageBox.warning(self, "方案无法载入", str(exc))
            return False

    def remove_preset(self, preset_id):
        if self.worker or self._busy:
            return
        self.profile_store.remove_preset(preset_id)
        self.refresh_workbench()
        self.show_profile_error()

    def clear_history(self):
        if self.worker or self._busy:
            return
        self.profile_store.clear_history()
        self.refresh_workbench()
        self.show_profile_error()

    def show_profile_error(self):
        if self.profile_store.last_error:
            QMessageBox.warning(self, "本地配置未保存", self.profile_store.last_error)

    def open_history_folder(self, task_id):
        item = next((item for item in self.profile_store.history if item["id"] == task_id), None)
        if item and item.get("folder") and Path(item["folder"]).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(item["folder"]))

    def show_history_result(self, task_id):
        item = next((item for item in self.profile_store.history if item["id"] == task_id), None)
        if not item:
            return
        records = [{"输入": item["title"], "输出": path, "状态": "成功"} for path in item["outputs"]]
        cancelled = item["status"] == "已停止"
        # Quick tasks share the original directory with unrelated files. Its
        # generic processing report may belong to another job and must not be read.
        report = Path(item["folder"]) / "处理记录.json" if item.get("folder") and not item.get("scene", "").startswith("quick:") else None
        if report and report.is_file():
            try:
                if report.stat().st_size <= 4 * 1024 * 1024:
                    saved = json.loads(report.read_text(encoding="utf-8-sig"))
                    saved_records = saved.get("结果") if isinstance(saved, dict) else None
                    if isinstance(saved_records, list) and all(isinstance(row, dict) for row in saved_records):
                        records = saved_records
            except (OSError, ValueError):
                pass
        result = {"folder": item["folder"], "records": records, "cancelled": cancelled,
                  "kind": "group", "output_count": item["output_count"], "error_count": item["error_count"],
                  "total": len(records), "fatal": "", "report_error": ""}
        ResultDialog(result, self, title=item["title"] + " · 历史结果").exec()

    def fit_settings(self):
        if not isValid(self) or not isValid(self.settings):
            return
        current = self.settings.currentWidget()
        if current is not None:
            current.layout().activate() if current.layout() else None
            height = current.sizeHint().height()
            if isinstance(current, QTabWidget):
                height = current.currentWidget().sizeHint().height() + current.tabBar().sizeHint().height() + 8
            self.settings.setFixedHeight(max(210, height))
            self.settings_scroll.setFixedHeight(max(210, height) + (20 if self._layout_compact else 0))

    def toggle_queue(self, expanded):
        self.queue_panel.setVisible(expanded and bool(self.files[self.mode]))
        self.manage_button.setText("收起清单" if expanded else "文件清单")

    def refresh_table(self):
        files = self.files[self.mode]
        states = self.queue_states[self.mode]
        self.queue_states[self.mode] = states = {source: states[source] for source in files if source in states}
        self.table.setRowCount(len(files))
        for index, source in enumerate(files):
            name = source if is_url(source) else Path(source).name
            kind = "链接" if is_url(source) else ("文件夹" if Path(source).is_dir() else Path(source).suffix.upper().lstrip("."))
            state, success, detail = states.get(source, ("待处理", None, source))
            for col, value in enumerate([name, kind, state]):
                item = QTableWidgetItem(value)
                item.setToolTip(source)
                self.table.setItem(index, col, item)
            self.table.item(index, 2).setToolTip(detail)
            if success is not None:
                self.table.item(index, 2).setForeground(QColor("#327e66" if success else "#b54e4e"))
        self.table.setFixedHeight(40 + 38 * min(5, max(1, len(files))))
        self.count_label.setText(f"{len(files)} 个项目" if files else "未选择")
        if files:
            names = [source if is_url(source) else Path(source).name for source in files]
            summary = "  ·  ".join(names[:2]) + (f"  等 {len(files)} 项" if len(files) > 2 else "")
        elif self.mode == "check_materials":
            summary = "拖入待检查的文件夹，或点击右侧添加"
        elif self.mode == "web":
            summary = "添加网页链接，或拖入已保存的 HTML"
        else:
            summary = "拖入文件到这里，或点击右侧添加"
        self.source_summary.setText(summary)
        self.source_summary.setToolTip("\n".join(files) if files else summary)
        self.manage_button.setEnabled(bool(files))
        if not files:
            self.manage_button.setChecked(False)
            self.queue_panel.hide()

    def accept_sources(self, values: list[str]):
        if self.worker or self._busy:
            return
        existing = self.files[self.mode]
        keys = {str(Path(s).resolve()).casefold() if not is_url(s) else s for s in existing}
        skipped = 0
        for value in values:
            path = Path(value) if not is_url(value) else None
            if path and path.is_dir() and self.mode not in EXTRA_ORDER:
                # Folder drops are shallow, consistent with the folder picker default.
                try:
                    children = [str(p) for p in sorted(path.iterdir()) if p.is_file()]
                except OSError as exc:
                    QMessageBox.warning(self, "无法读取文件夹", f"请检查目录权限或连接状态。\n{exc}")
                    continue
                self.accept_sources(children)
                keys = {str(Path(s).resolve()).casefold() if not is_url(s) else s for s in existing}
                continue
            extensions = SUPPORTED[self.mode]
            valid = (self.mode == "web" and is_url(value)) or bool(path and (
                (path.is_dir() and self.mode in EXTRA_ORDER) or
                (path.is_file() and self.mode != "check_materials" and
                 (extensions is None or path.suffix.lower() in extensions) and not path.name.startswith("~$"))))
            if not valid:
                skipped += 1
                continue
            canonical = value if is_url(value) else str(path.resolve())
            key = canonical if is_url(value) else canonical.casefold()
            if key not in keys:
                existing.append(canonical)
                keys.add(key)
        self.refresh_table()
        self.status.setText(f"已加入 {len(existing)} 个项目" + (f" · 忽略 {skipped} 个不支持的项目" if skipped else ""))

    def add_files(self):
        extensions = SUPPORTED[self.mode]
        extension = " ".join("*" + e for e in sorted(extensions)) if extensions else "*"
        paths, _ = QFileDialog.getOpenFileNames(self, "添加待处理文件", "", f"支持的文件 ({extension})")
        self.accept_sources(paths)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹" if self.mode in EXTRA_ORDER else "选择文件夹（读取当前层级）")
        if folder:
            self.accept_sources([folder])

    def add_urls(self):
        text, accepted = QInputDialog.getMultiLineText(self, "批量添加网页链接", "每行一个 http:// 或 https:// 链接：")
        if accepted:
            self.accept_sources([line.strip() for line in text.splitlines() if line.strip()])

    def remove_selected(self):
        if self.worker or self._busy:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            del self.files[self.mode][row]
        self.refresh_table()

    def move_selected(self, direction):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows or self.worker or self._busy:
            return
        selected = set(rows)
        files = self.files[self.mode]
        for row in rows if direction < 0 else reversed(rows):
            target = row + direction
            if 0 <= target < len(files) and target not in selected:
                files[row], files[target] = files[target], files[row]
                selected.remove(row)
                selected.add(target)
        self.refresh_table()
        self.table.clearSelection()
        for row in selected:
            self.table.selectRow(row) if len(selected) == 1 else self.table.item(row, 0).setSelected(True)

    def show_columns(self):
        from .core.excel_ops import read_headers
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        sources = [self.files[self.mode][i] for i in rows] if rows else self.files[self.mode][:2]
        sources = [p for p in sources if Path(p).is_file()]
        if not sources:
            path, _ = QFileDialog.getOpenFileName(self, "选择一个表格查看列名", "", "表格 (*.xlsx *.xlsm *.csv *.tsv)")
            sources = [path] if path else []
        try:
            options = self.get_options()
            lines = []
            for index, source in enumerate(sources[:2]):
                sheet = options.get("sheet", "")
                if self.mode == "excel_compare":
                    sheet = options.get("sheet_left" if index == 0 else "sheet_right", "")
                columns = read_headers(Path(source), sheet, options.get("header_row", 1))
                lines.append(Path(source).name + "\n" + "，".join(columns))
            if lines:
                dialog = QDialog(self)
                dialog.setWindowTitle("表格列名（可复制）")
                dialog.resize(650, 330)
                body = QVBoxLayout(dialog)
                text = QPlainTextEdit("\n\n".join(lines))
                text.setReadOnly(True)
                body.addWidget(text)
                body.addWidget(button("关闭", dialog.accept))
                dialog.exec()
        except Exception as exc:
            QMessageBox.warning(self, "列名读取失败", str(exc))

    def undo_rename(self):
        if self.worker:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择本工具生成的撤销改名清单", "", "JSON (*.json)")
        if path:
            self.launch_tool("undo_rename", [path], {})

    def choose_output(self):
        value = QFileDialog.getExistingDirectory(self, "选择保存目录", self.output_edit.text())
        if value:
            self.output_edit.setText(value)

    def add_rule(self, checked=False, rule=None):
        row = self.rules_table.rowCount()
        self.rules_table.insertRow(row)
        rule = rule or {"find": "", "replace": "", "regex": False}
        for col, key in enumerate(["find", "replace"]):
            self.rules_table.setItem(row, col, QTableWidgetItem(str(rule.get(key, ""))))
        mode = QComboBox()
        mode.addItems(["普通文字", "正则"])
        mode.setCurrentIndex(int(bool(rule.get("regex"))))
        self.rules_table.setCellWidget(row, 2, mode)

    def remove_rule(self):
        for row in sorted({i.row() for i in self.rules_table.selectedIndexes()}, reverse=True):
            self.rules_table.removeRow(row)

    def refresh_redaction_memory(self):
        count = len(self.redaction_store.groups)
        group = self.redaction_store.find_group(self.redaction_store.restore_id)
        self.redaction_memory_summary.setTextFormat(Qt.TextFormat.PlainText)
        if self.redaction_store.last_error:
            text = "规则记忆库暂不可用，打开记忆库可查看原因。"
        elif group:
            text = f"本机已记住 {count} 组 · 下次启动后自动载入：{group['name']}"
        else:
            text = f"本机已记住 {count} 组 · 点击“记住当前规则”，下次可直接使用。"
        self.redaction_memory_summary.setText(text)
        self.redaction_memory_summary.setToolTip(self.redaction_store.last_error or "只保存自定义替换规则；手机号、格式、公文及文件清单等设置各自保留。")
        QTimer.singleShot(0, self.fit_settings)

    def restore_redaction_memory(self):
        """Restore explicit custom rules into the redaction scene only."""
        group = self.redaction_store.find_group(self.redaction_store.restore_id)
        if group:
            values = copy.deepcopy(self.word_scene_options["word"])
            values.update(format_enabled=False, official_enabled=False, mask_phone=True, mask_id=True, mask_email=False,
                          custom_sensitive=[], custom_sensitive_rules=group["rules"], replacements=[], delete_paragraphs=[],
                          clean_metadata=False, remove_comments=False, remove_blank_paragraphs=False)
            self.word_scene_options["redact"] = values

    def remember_redaction_rules(self):
        if self.worker or self._busy:
            return
        try:
            rules = normalize_rules(self.m_custom.rules())
            if not rules:
                raise ValueError("请先在表格中填写至少一组替换规则。")
            name, accepted = QInputDialog.getText(self, "记住当前规则", "给这组常用规则起个名字：", text="常用脱敏规则")
            if not accepted:
                return
            group = self.redaction_store.save_group(name, rules)
            try:
                self.redaction_store.set_restore_group(group["id"])
            except (ValueError, OSError) as exc:
                self.refresh_redaction_memory()
                QMessageBox.warning(self, "规则已保存，自动载入未设置", str(exc))
                return
            self.refresh_redaction_memory()
            self.status.setText(f"已记住“{group['name']}”的 {len(rules)} 条规则 · 下次启动后进入脱敏场景自动载入")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "规则未记住", str(exc))

    def apply_redaction_memory_rules(self, rules, append=False):
        if self.worker or self._busy:
            return False
        try:
            rules = normalize_rules(rules)
            if append:
                result = self.m_custom.append_rules(rules)
                self.status.setText(f"已追加 {result['added']} 条记忆规则 · 跳过 {result['duplicates']} 条完全重复的规则")
            else:
                self.m_custom.set_rules(rules)
                self.status.setText(f"已载入 {len(rules)} 条记忆规则 · 可继续编辑，核对后开始处理")
            return True
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "规则未载入", str(exc))
            return False

    def open_redaction_library(self):
        if self.worker or self._busy:
            return
        dialog = RedactionLibraryDialog(self.redaction_store, self.m_custom.rules, self,
                                        apply_rules=self.apply_redaction_memory_rules)
        dialog.exec()
        self.refresh_redaction_memory()

    def get_options(self, mode=None) -> dict:
        mode = mode or self.mode
        if mode in self.extra_panels:
            return self.extra_panels[mode].options()
        if mode == "word":
            sensitive, sensitive_rules = self.m_custom.option_rules()
            rules = []
            for row in range(self.rules_table.rowCount()):
                find = self.rules_table.item(row, 0).text()
                replace = self.rules_table.item(row, 1).text()
                if find or replace:
                    rules.append({"find": find, "replace": replace,
                                  "regex": self.rules_table.cellWidget(row, 2).currentIndex() == 1})
            return {"format_enabled": self.f_enabled.isChecked(), "font_name": self.f_font.currentText(),
                    "font_size": self.f_size.value(), "line_spacing": self.f_line.value(),
                    "margin_cm": self.f_margin.value(), "preserve_headings": self.f_headings.isChecked(),
                    "official_enabled": self.official_panel.enabled.isChecked(),
                    "official": self.official_panel.options(),
                    "mask_phone": self.m_phone.isChecked(), "mask_id": self.m_id.isChecked(),
                    "mask_email": self.m_email.isChecked(),
                    "custom_sensitive": sensitive, "custom_sensitive_rules": sensitive_rules,
                    "custom_sensitive_replacement": self.m_custom_replacement.text(),
                    **{key: edit.text() for key, edit in self.mask_replacements.items()},
                    "replacements": rules,
                    "delete_paragraphs": [s.strip() for s in self.c_delete.toPlainText().splitlines() if s.strip()],
                    "clean_metadata": self.c_metadata.isChecked(), "remove_comments": self.c_comments.isChecked(),
                    "remove_blank_paragraphs": self.c_blank.isChecked(), "include_headers": True}
        if mode == "web":
            return {"include_images": self.w_images.isChecked(), "timeout": 20, "max_images": 40}
        if mode == "convert":
            return {"pdf_mode": ["text", "appearance"][self.p_mode.currentIndex()],
                    "password": self.p_password.text(), "dpi": 144}
        return {"mode": ["margins", "ratio", "size"][self.i_mode.currentIndex()],
                **{key: control.value() for key, control in self.i_margins.items()},
                "ratio_w": self.i_ratio_w.value(), "ratio_h": self.i_ratio_h.value(),
                "width": int(self.i_width.value()), "height": int(self.i_height.value()),
                "anchor": ["center", "top-left", "top", "bottom"][self.i_anchor.currentIndex()],
                "format": ["original", "png", "jpg"][self.i_format.currentIndex()], "quality": 92}

    def apply_word_options(self, values: dict):
        if not isinstance(values, dict):
            raise ValueError("Word规则应为字段和值组成的对象。")
        bool_fields = ("format_enabled", "preserve_headings", "mask_phone", "mask_id", "mask_email",
                       "remove_blank_paragraphs", "clean_metadata", "remove_comments", "include_headers", "official_enabled")
        for key in bool_fields:
            if key in values and not isinstance(values[key], bool):
                raise ValueError(f"{key}应为是或否。")
        for key, control in (("font_size", self.f_size), ("line_spacing", self.f_line), ("margin_cm", self.f_margin)):
            if key in values:
                value = values[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not control.minimum() <= value <= control.maximum():
                    raise ValueError(f"{key}不是界面允许范围内的数字。")
        if "font_name" in values and (not isinstance(values["font_name"], str) or not values["font_name"].strip()):
            raise ValueError("字体名称应为非空文字。")
        for key in ("custom_sensitive", "delete_paragraphs"):
            if key in values and (not isinstance(values[key], list) or any(not isinstance(item, str) for item in values[key])):
                raise ValueError(f"{key}应为文字列表。")
        custom_rules = values.get("custom_sensitive_rules", [])
        if not isinstance(custom_rules, list) or any(not isinstance(rule, dict) or not isinstance(rule.get("find"), str)
                or not rule["find"] or not isinstance(rule.get("replace", ""), str) for rule in custom_rules):
            raise ValueError("自定义脱敏规则需要填写原内容和替换文字。")
        normalize_rules(custom_rules)
        if len(values.get("custom_sensitive", [])) + len(custom_rules) > MAX_RULES:
            raise ValueError(f"每次最多使用 {MAX_RULES} 条自定义脱敏规则，请分组处理。")
        if any(not item for item in values.get("custom_sensitive", [])):
            raise ValueError("统一替换词不能为空。")
        for key in (*self.mask_replacements, "custom_sensitive_replacement"):
            if key in values and not isinstance(values[key], str):
                raise ValueError("脱敏替换内容应为文字。")
        from .core.official_ops import validate_official_options
        official_options = validate_official_options(values.get("official", {}))
        rules = values.get("replacements", [])
        if not isinstance(rules, list) or any(not isinstance(rule, dict) or not isinstance(rule.get("find"), str)
                or not isinstance(rule.get("replace", ""), str) or not isinstance(rule.get("regex", False), bool) for rule in rules):
            raise ValueError("查找替换规则格式无效。")
        for key, control in [("format_enabled", self.f_enabled), ("preserve_headings", self.f_headings),
                             ("mask_phone", self.m_phone), ("mask_id", self.m_id), ("mask_email", self.m_email),
                             ("remove_blank_paragraphs", self.c_blank), ("clean_metadata", self.c_metadata),
                             ("remove_comments", self.c_comments)]:
            if key in values:
                control.setChecked(bool(values[key]))
        for key, control in [("font_size", self.f_size), ("line_spacing", self.f_line), ("margin_cm", self.f_margin)]:
            if key in values:
                control.setValue(float(values[key]))
        self.f_font.setCurrentText(str(values.get("font_name", "宋体")))
        for key, edit in self.mask_replacements.items():
            if key in values:
                edit.setText(values[key])
        if "custom_sensitive_replacement" in values:
            self.m_custom_replacement.setText(values["custom_sensitive_replacement"])
        self.m_custom.set_content(values.get("custom_sensitive", []), custom_rules)
        self.c_delete.setPlainText("\n".join(values.get("delete_paragraphs", [])))
        self.rules_table.setRowCount(0)
        for rule in values.get("replacements", []):
            self.add_rule(rule=rule)
        self.official_panel.set_options(official_options, enabled=values.get("official_enabled", False))

    def export_rules(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存处理规则", self.title.text() + "规则.json", "JSON (*.json)")
        if path:
            try:
                values = {"version": 2, "tool": self.mode, "options": scrub_secrets(self.get_options())}
                Path(path).write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.setText("已导出规则 · 不保存密码；规则可能含自定义词与本机路径")
            except Exception as exc:
                QMessageBox.warning(self, "规则保存失败", str(exc))

    def import_rules(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入处理规则", "", "JSON (*.json)")
        if path:
            try:
                if Path(path).stat().st_size > 1024 * 1024:
                    raise ValueError("规则文件超过 1 MB。")
                values = json.loads(Path(path).read_text(encoding="utf-8-sig"))
                if not isinstance(values, dict):
                    raise ValueError("规则文件需要是 JSON 对象。")
                if "tool" in values:
                    mode, values = values["tool"], values.get("options", {})
                    if mode not in MODES or (mode != "word" and mode not in EXTRA_ORDER):
                        raise ValueError("规则使用了当前版本不支持的工具。")
                else:
                    mode = "word"  # Compatible with the original Word-only rules.
                if mode == "word":
                    validate_options("word", values)
                    self.apply_word_options(values)
                else:
                    self.extra_panels[mode].set_options(values)
                self.switch_mode(MODES.index(mode))
                self.status.setText("已导入规则，请核对设置后开始处理")
            except Exception as exc:
                QMessageBox.warning(self, "规则导入失败", str(exc))

    def set_busy(self, busy: bool):
        self._busy = busy
        if self.converter_window is not None:
            self.converter_window.set_busy(busy, own_job=self._quick_active)
        controls = self.nav_buttons + list(self.surface_buttons.values()) + [self.content_stack.widget(i) for i in range(1, self.content_stack.count())] + [self.add_files_button, self.add_folder_button, self.add_url_button,
            self.clear_button, self.choose_output_button, self.settings, self.import_button,
            self.export_button, self.start_button, self.up_button, self.down_button, self.columns_button, self.undo_button, self.guide_button, self.preset_button]
        for control in controls:
            control.setEnabled(not busy)
        # Personal content remains usable while a background conversion runs.
        self.home_page.setEnabled(True)
        self.home_page.group_section.setEnabled(not busy)
        self.table.setAcceptDrops(not busy)
        self.source_bar.setAcceptDrops(not busy)
        self.source_bar._drag_state(False)
        self.manage_button.setEnabled(bool(self.files[self.mode]))
        self.stop_button.setVisible(busy)
        self.stop_button.setEnabled(busy)
        for control in (self.compact_home, self.compact_tools, self.compact_history):
            control.setEnabled(not busy)
        if self._ui_ready:
            self.update_responsive_layout()

    def start_task(self, checked=False, *, check_official=False):
        if self.worker:
            return
        files = self.files[self.mode].copy()
        if not files:
            QMessageBox.information(self, "还没有待处理项目", "请先添加文件或网页链接。")
            return
        try:
            options = self.get_options()
            if check_official:
                options["official_enabled"] = True
                options["official"]["operation"] = "check"
            if self.mode in EXTRA_ORDER:
                validate_tool_options(self.mode, options, files)
            else:
                validate_options(self.mode, options)
            if self.mode in {"word", "convert"} and any(Path(f).suffix.lower() in (
                    {".doc"} if self.mode == "word" else {".doc", ".docx", ".rtf"}) for f in files):
                if not find_libreoffice():
                    raise ValueError("未找到 Word 排版引擎。请保留“程序文件/engines”，或使用源码启动器自检补齐后重试。")
        except Exception as exc:
            QMessageBox.warning(self, "请检查处理设置", str(exc))
            return
        self.queue_states[self.mode].clear()
        self.refresh_table()
        self.progress.setValue(0)
        self.status.setText("正在准备结果文件夹…")
        self.active_context = {"mode": self.mode, "options": copy.deepcopy(options), "files": files.copy(),
                               "scene": self.active_scene, "title": self.title.text()}
        if self.mode in EXTRA_ORDER:
            if self.mode == "file_organize":
                self.set_busy(True)
                self.worker = PreviewWorker(files, options, Path(self.output_edit.text()))
                self.worker.progress.connect(self.on_tool_progress)
                self.worker.completed.connect(self.store_preview)
                self.worker.finished.connect(self.on_worker_finished)
                self.worker.start()
            else:
                self.launch_tool(self.mode, files, options)
            return
        self.set_busy(True)
        self.worker = BatchWorker(self.mode, files, options, Path(self.output_edit.text()))
        self.worker.file_started.connect(self.on_file_started)
        self.worker.file_done.connect(self.on_file_done)
        self.worker.progress.connect(self.on_progress)
        self.worker.completed.connect(self.on_complete)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def launch_tool(self, mode, files, options, folder=None):
        self.active_context = {"mode": mode, "options": copy.deepcopy(options), "files": list(files),
                               "scene": self.active_scene, "title": "撤销原地改名" if mode == "undo_rename" else self.title.text()}
        self.set_busy(True)
        self.progress.setValue(0)
        self.status.setText("正在处理 · 结果与核查清单将写入本次文件夹")
        self.worker = ToolWorker(mode, files, options, Path(self.output_edit.text()), folder)
        self.worker.progress.connect(self.on_tool_progress)
        self.worker.completed.connect(self.on_complete)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def store_preview(self, result):
        self.pending_preview = result

    def review_preview(self):
        result, self.pending_preview = self.pending_preview, None
        if not result:
            return
        if result["fatal"]:
            QMessageBox.warning(self, "无法生成预览", result["fatal"])
        elif result["cancelled"] or not result["plan"]:
            self.status.setText("预览已取消或没有匹配文件")
        else:
            dialog = QDialog(self)
            dialog.setWindowTitle("文件改名与归档预览")
            dialog.resize(1000, 570)
            layout = QVBoxLayout(dialog)
            rename = result["options"].get("action") == "rename"
            layout.addWidget(label(f"共 {len(result['plan'])} 个文件 · " +
                                   ("执行后修改源文件名，并保存撤销清单" if rename else "将复制到结果目录，保留原件"), "cardTitle", True))
            table = QTableWidget(len(result["plan"]), 2)
            table.setHorizontalHeaderLabels(["当前路径", "处理后路径"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            for row, item in enumerate(result["plan"]):
                for column, key in enumerate(("source", "target")):
                    cell = QTableWidgetItem(str(item[key]))
                    cell.setToolTip(str(item[key]))
                    table.setItem(row, column, cell)
            layout.addWidget(table)
            actions = QHBoxLayout()
            actions.addStretch()
            actions.addWidget(button("取消", dialog.reject))
            actions.addWidget(button("执行以上改名" if rename else "执行以上复制归档", dialog.accept, "primary"))
            layout.addLayout(actions)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                options = dict(result["options"], _plan=result["plan"])
                self.launch_tool("file_organize", result["sources"], options, result["folder"])
                return
            self.status.setText("已取消预览，源文件未变更")
        if result.get("folder"):
            try:
                Path(result["folder"]).rmdir()  # Only remove an empty preview folder.
            except OSError:
                pass

    def on_file_started(self, index):
        self.queue_states[self.mode][self.files[self.mode][index]] = ("处理中…", None, "处理中…")
        self.table.item(index, 2).setText("处理中…")
        self.table.item(index, 2).setToolTip("处理中…")
        self.status.setText(f"正在处理 {index + 1} / {self.table.rowCount()} · 可继续使用电脑")

    def on_file_done(self, index, success, message):
        self.queue_states[self.mode][self.files[self.mode][index]] = (
            message if success else "失败 · 查看结果", success, message)
        item = self.table.item(index, 2)
        item.setText(message if success else "失败 · 查看结果")
        item.setForeground(QColor("#327e66" if success else "#b54e4e"))
        item.setToolTip(message)

    def on_progress(self, done, total):
        self.progress.setValue(round(done / max(1, total) * 100))

    def on_tool_progress(self, done, total, message):
        self.on_progress(done, total)
        self.status.setText(message or "正在处理…")

    def stop_task(self):
        if self.worker:
            self.worker.requestInterruption()
            self.stop_button.setEnabled(False)
            self.status.setText("停止请求已收到 · 当前文件完成后停止")
            if self._quick_active:
                self.conversion_session.notice("正在停止，已完成的结果会保留…")
                if self.converter_window is not None:
                    self.converter_window.cancel_button.setEnabled(False)
                    self.converter_window.notice("正在停止，已完成的结果会保留…")

    def on_complete(self, result):
        self.last_result = result
        self.last_context = copy.deepcopy(self.active_context)
        context = self.active_context or {"mode": self.mode, "title": self.title.text(), "scene": self.active_scene}
        self.profile_store.record_task(context["mode"], context["title"], result, context.get("scene", ""))
        self.active_context = None
        self.refresh_workbench()
        self.result_button.setEnabled(bool(result["folder"]))
        records = result["records"]
        successes = sum(r["状态"] == "成功" for r in records)
        failures = len(records) - successes
        if result["fatal"]:
            self.status.setText("任务未完成：" + result["fatal"])
        elif result.get("kind") == "group":
            self.status.setText(("已停止" if result["cancelled"] else "处理完成") +
                                f" · 生成 {result['output_count']} 个结果，异常 {result['error_count']} 项 · 查看结果及核查清单")
        else:
            self.status.setText(("已停止" if result["cancelled"] else "处理完成") +
                                f" · 成功 {successes} 项，失败 {failures} 项 · 点击查看结果")
        if result.get("kind") == "group":
            message = "已停止 · 查看结果" if result["cancelled"] else ("有异常 · 查看结果" if failures else "完成 · 查看结果")
            for row in range(self.table.rowCount()):
                self.table.item(row, 2).setText(message)
                self.queue_states[self.mode][self.files[self.mode][row]] = (message, None, message)
        if result["report_error"]:
            QMessageBox.warning(self, "处理记录保存失败", result["report_error"])
        if self.profile_store.last_error:
            self.status.setText(self.status.text() + " · 最近任务未能保存，本次结果仍可查看")
            self.status.setToolTip(self.status.text() + "\n" + self.profile_store.last_error)

    def on_worker_finished(self):
        finished = self.worker
        self.worker = None
        self._quick_active = False
        self.set_busy(False)
        if finished:
            finished.deleteLater()
        if self.pending_preview is not None:
            QTimer.singleShot(0, self.review_preview)

    def show_results(self):
        if not self.last_result:
            return
        dialog = ResultDialog(self.last_result, self, retryable=bool(self.failed_sources()))
        dialog.retry_requested.connect(lambda: QTimer.singleShot(0, self.retry_failed))
        dialog.exec()

    def failed_sources(self):
        if not self.last_context or not self.last_result or self.last_result.get("kind") == "group":
            return []
        sources = set(self.last_context["files"])
        return [row["输入"] for row in self.last_result.get("records", [])
                if row.get("状态") == "失败" and row.get("输入") in sources]

    def retry_failed(self):
        if self.worker or self._busy:
            return False
        failed = self.failed_sources()
        if not failed:
            return False
        context = copy.deepcopy(self.last_context)
        try:
            if context["mode"] == "word":
                self.word_scene_options[self._word_scene] = copy.deepcopy(self.get_options("word"))
            self.apply_options(context["mode"], context["options"])
            if context["mode"] == "word":
                self._word_scene = context.get("scene") or "word"
            self.switch_mode(MODES.index(context["mode"]))
            self.active_scene = context.get("scene", "")
            self.files[self.mode] = failed
            self.queue_states[self.mode].clear()
            self.refresh_table()
            self.start_task()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "重试未开始", str(exc))
            return False

    def show_tool_guide(self):
        if self.mode not in GUIDES:
            return
        mode = self.mode
        dialog = QDialog(self)
        dialog.setWindowTitle(TOOLS[mode]["title"] + " · 使用说明")
        dialog.resize(720, 610)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setHtml(guide_html(mode))
        layout.addWidget(browser)
        actions = QHBoxLayout()
        actions.addWidget(button("关闭", dialog.reject))
        actions.addStretch()
        if mode in {"excel_compare", "excel_clean", "check_materials"} or mode in OFFICE_SPECS:
            practice = button("载入本页示例", name="primary")
            practice.setToolTip("将本页文件清单和设置换成练习数据，再点击开始处理。")
            def load():
                if self.load_tool_example(mode):
                    dialog.accept()
            practice.clicked.connect(load)
            actions.addWidget(practice)
        layout.addLayout(actions)
        dialog.exec()

    def load_tool_example(self, mode):
        if self.worker or self._busy or mode != self.mode:
            return False
        try:
            folder = new_run_directory(Path(self.output_edit.text())) / "上手示例"
            sources, options = build_example(mode, folder)
            self.extra_panels[mode].set_options(options)
            self.files[mode].clear()
            self.queue_states[mode].clear()
            self.accept_sources(sources)
            self.status.setText("已载入示例和对应设置 · 点击开始处理即可查看结果")
            return True
        except Exception as exc:
            QMessageBox.warning(self, "示例载入失败", str(exc))
            return False

    def _show_text_help(self, title, text, *, conversion=False):
        owner = self.converter_window if self.converter_is_visible() else self
        dialog = QDialog(owner)
        dialog.setWindowTitle(title)
        screen = owner.screen().availableGeometry()
        dialog.resize(min(700, screen.width() - 35), min(650, screen.height() - 55))
        layout = QVBoxLayout(dialog)
        viewer = QTextBrowser()
        viewer.setPlainText(text)
        viewer.setOpenExternalLinks(False)
        viewer.setAccessibleName(title)
        layout.addWidget(viewer, 1)
        actions = QHBoxLayout()
        if conversion:
            actions.addWidget(button("转换格式与字段说明", self.show_conversion_help))
        actions.addStretch(1)
        actions.addWidget(button("关闭", dialog.accept))
        layout.addLayout(actions)
        dialog.exec()

    def show_conversion_help(self):
        base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
        roots = (base / "support", base, base / "程序文件" / "support", base / "程序文件")
        paths = [root / "转换格式说明.txt" for root in roots]
        text = "转换格式说明文件未找到。请保留完整的“程序文件”文件夹。\n\n独立转换：拖入文件或链接，只显示当前输入共同支持的目标格式；点击目标或拖到扇区松开即可开始。\n\nVCF / ICS 需要标准列名。请查看包内的《转换格式说明.txt》及对应样例。"
        for path in paths:
            try:
                if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
                    text = path.read_text(encoding="utf-8-sig")
                    break
            except (OSError, UnicodeError):
                continue
        self._show_text_help("转换格式与字段说明", text)

    def show_help(self):
        self._show_text_help("使用说明与能力范围", (
            "青简工具箱 " + __version__ + "\n\n"
            "格式转换浮窗\n"
            "点击高亮“转换”按钮、主页“拆出”或 Ctrl+Shift+C 打开。文件拖入圆环中心后点选目标；也可以直接拖到目标扇区松开。支持批量文件、粘贴文件或链接、分页查看更多格式、折叠为小按钮、置顶和自由拖动。主窗口关闭后浮窗与正在执行的转换继续运行。\n"
            "本地输入的结果保存在原文件旁；合并任务保存到首个文件所在目录；链接保存位置可更改。原件保留，同名结果自动编号，失败原因在“查看结果”中查看。\n\n"
            "转换范围\n"
            "包含已选 01—15 类：Word、表格、PPT、PDF 内容与页面、图片制作文档、本地网页、文本/Markdown、常用图片、手机照片、矢量图、压缩包、邮件、结构化数据、通讯录与日历；另含 OFD 和轻量 EPUB 电子书，不需要 GPU。每种输入的具体方向由圆环显示，格式限制和 VCF/ICS 标准列名见《转换格式说明》。\n"
            "在线网址需要联网；保存完整资源的 HTML/MHTML 可离线。不执行网页脚本。PDF 扫描页不进行 OCR；PDF 转 PPT 使用页面图片。\n\n"
            "原有办公工具\n"
            "从“全部工具”选择任务，添加文件并设置参数后开始。常规结果写入独立文件夹；原地改名需预览，并可通过撤销清单恢复。检查、提取与对账请复核生成的结果清单。\n"
            "公文排版可配置四级标题和版式、仅检查或生成样板；自定义脱敏支持多组替换规则与记忆库。Word/PPT 模板按 {{列名}} 批量填充。\n"
            "对账、清洗、材料核查等页面有“说明示例”。这些原有表格分析工具接收 XLSX/XLSM/CSV/TSV；旧 XLS/ODS 可先通过转换浮窗转为 XLSX 再使用。数据分析输出不保留宏与原版式，公式需先计算并保存缓存。\n\n"
            "记事与提醒\n"
            "高亮按钮或 Ctrl+Shift+N 打开日历和当天事项浮窗，可分别拖动、拆分或合并。支持公历农历、预约和循环提醒、重要程度颜色、本机保存；模块或托盘运行期间提醒生效，主窗口关闭后继续运行。\n\n"
            "文件与环境\n"
            "发布文件夹只保留启动器、使用说明和“程序文件”三项。请完整保留“程序文件/engines”；第三方许可位于“程序文件/support/licenses”。源码首次启动可选择已有 Python 或部署位置，复用满足要求的组件，只补缺失项；初始化后可离线使用。工具不上传文档；在线网页访问会连接相应网站。"
        ), conversion=True)

    def closeEvent(self, event):
        if self.converter_is_visible():
            # This QMainWindow remains alive (no WA_DeleteOnClose), so the one
            # shared worker and completion slots survive closing the main UI.
            event.accept()
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "任务正在处理中", "请点击停止任务，等待当前文件完成后再关闭窗口。")
            event.ignore()
        else:
            event.accept()


def main():
    parser = argparse.ArgumentParser(description="青简工具箱")
    parser.add_argument("--screenshot", help="离屏界面检查：保存页面图片后退出")
    parser.add_argument("--native-screenshot", action="store_true", help="界面检查时使用当前屏幕的原生窗口")
    parser.add_argument("--layout-report", help="将界面检查时的窗口尺寸与布局写入 JSON")
    parser.add_argument("--page", type=int, choices=range(len(MODES)), default=None)
    parser.add_argument("--scene", choices=[item["id"] for item in SCENES], help="打开指定办公场景")
    parser.add_argument("--surface", choices=["home", "catalog", "presets", "history"], help="打开工作台页面")
    parser.add_argument("--self-check", help="运行内置离线功能检查，将报告写入指定文件")
    parser.add_argument("--notebook", action="store_true", help="只打开独立记事提醒小窗")
    parser.add_argument("--converter", action="store_true", help="只打开独立格式转换小窗")
    args = parser.parse_args()
    if args.self_check:
        from .selfcheck import run
        return run(Path(args.self_check))
    if args.screenshot and not args.native_screenshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    elif args.screenshot and args.native_screenshot:
        os.environ.pop("QT_QPA_PLATFORM", None)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("青简工具箱")
    app.setOrganizationName("QingjianLocal")
    # Qt's offscreen plugin has no Windows font registry integration. Load the
    # already-installed Windows fonts for headless QA; no font is redistributed.
    if not QFontDatabase.families():
        font_folder = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for font in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
            if (font_folder / font).is_file():
                QFontDatabase.addApplicationFont(str(font_folder / font))
    app.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#6257ed"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    def show_exception(exc_type, exc_value, exc_traceback):
        QMessageBox.critical(None, "操作未完成", f"请检查输入或保存位置后重试。\n{exc_value}")
    sys.excepthook = show_exception
    data_folder = os.environ.get("QINGJIAN_USER_DATA")
    if not data_folder and not args.screenshot:
        data_folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    window = Window(start_home=True, data_path=Path(data_folder) / "profile.json" if data_folder else None)
    from .notebook_store import NotebookStore
    from .notebook_runtime import NotebookController
    notebook_store = NotebookStore(Path(data_folder) / "notebook.json" if data_folder else None)

    def open_toolbox():
        window.showNormal()
        window.raise_()
        window.activateWindow()

    window.home_page.personal_panel.set_notebook_store(notebook_store)
    notebook = NotebookController(notebook_store, open_toolbox=open_toolbox, parent=app,
                                  enable_tray=not bool(args.screenshot), test_mode=bool(args.screenshot))
    notebook.store_changed.connect(window.home_page.personal_panel.set_notebook_store)
    def report_notebook_error(message):
        window.status.setText(message)
        if notebook.window is not None:
            notebook.window.error_label.setText(message)
            notebook.window.error_label.show()

    notebook.error.connect(report_notebook_error)
    window.notebook_requested.connect(notebook.show_window)
    notebook.stopped.connect(lambda: app.quit() if not window.isVisible() and not window.converter_is_visible() else None)
    app.aboutToQuit.connect(notebook.shutdown)
    if args.page is not None:
        window.switch_mode(args.page)
    if args.scene:
        window.open_scene(args.scene)
    if args.surface:
        window.show_surface(args.surface)
    if args.converter:
        window.show_converter()
    elif args.notebook:
        if not notebook.show_window():
            if notebook.forwarded:
                return 0
            window.show()
    else:
        window.show()
        if not args.screenshot:
            notebook.start_if_enabled()
    if args.screenshot:
        def capture():
            path = Path(args.screenshot)
            path.parent.mkdir(parents=True, exist_ok=True)
            snapshot = (window.converter_window.grab() if args.converter else
                        notebook_snapshot(notebook.window) if args.notebook else window.grab())
            snapshot.save(str(path))
            if args.layout_report:
                target_window = window.converter_window if args.converter else notebook.window if args.notebook else window
                screen = target_window.screen()
                def rect(value):
                    return dict(x=value.x(), y=value.y(), width=value.width(), height=value.height())
                report = dict(frameGeometry=rect(target_window.frameGeometry()), availableGeometry=rect(screen.availableGeometry()),
                              screenGeometry=rect(screen.geometry()), isMaximized=target_window.isMaximized(), version=__version__,
                              quick_card_count=0,
                              home_group_count=len(window.home_page.group_cards),
                              home_sections=["groups", "personal"],
                              compact_mode=False if args.notebook or args.converter else window._layout_compact,
                              target="converter" if args.converter else "notebook" if args.notebook else "toolbox")
                report_path = Path(args.layout_report)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            notebook.shutdown()
            app.quit()
        QTimer.singleShot(700, capture)
    return app.exec()
