"""Small, explicit drop targets for immediate conversions on the home page."""
from __future__ import annotations

from .platform_support import shortcut_text

import json
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize, QStandardPaths
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .quick_actions import ACTIONS, parse_mime_payload, validate_sources


QUICK_STYLE = """
QPushButton#quickCard { background: white; border: 1px solid #cbd4e3; border-radius: 8px; text-align: left; }
QPushButton#quickCard:hover { background: #f8f7ff; border-color: #ada3ef; }
QPushButton#quickCard:focus { border: 2px solid #8d7de7; }
QPushButton#quickCard[dragActive="true"] { background: #eeebff; border: 2px solid #8d7de7; }
QPushButton#quickCard[dragInvalid="true"] { background: #fff3f1; border-color: #dca397; }
QPushButton#quickCard:disabled { background: #f5f6f9; border-color: #e7e9f0; }
QLabel#quickTitle { color: #364057; font-size: 14px; font-weight: 600; }
QLabel#quickHint { color: #8a92a5; font-size: 11px; }
QLabel#quickDescription { color: #78839a; font-size: 11px; }
QLineEdit#quickUrl { background: white; padding: 9px 12px; border: 1px solid #e0e4ee; border-radius: 9px; }
QPushButton#quickPaste { background: #6257ed; color: white; border: none; border-radius: 8px; padding: 9px 14px; }
QPushButton#quickPaste:disabled { background: #cbc6ee; }
QPushButton#quickQuiet { border: none; background: transparent; color: #6257ed; padding: 3px 6px; font-size: 11px; }
QFrame#quickState { background: #f2f1fa; border: 1px solid #cbc8e4; border-radius: 7px; }
QProgressBar#quickProgress { border: none; background: #dfdaef; border-radius: 2px; height: 3px; }
QProgressBar#quickProgress::chunk { background: #7768db; border-radius: 2px; }
QWidget#quickPanel[compact="true"] QLabel#quickTitle { font-size: 13px; }
QWidget#quickPanel[compact="true"] QLabel#quickGlyph { font-size: 10px; }
QWidget#quickPanel[compact="true"] QLineEdit#quickUrl { padding: 4px 8px; border-radius: 8px; }
QWidget#quickPanel[compact="true"] QPushButton#quickPaste { padding: 4px 9px; }
QWidget#quickPanel[compact="true"] QPushButton#quickQuiet { padding: 2px 4px; }
"""


_COMPACT_TYPES = {
    "to_pdf": "文档 / 表格 / 图片",
    "pdf_to_word": "可编辑 PDF",
    "web_to_word": "链接 / HTML / MHTML",
    "to_text": "文档 / PDF / Markdown",
    "image_to_jpg": "常见图片格式",
    "image_to_png": "常见图片格式",
}


def _text(value, name, wrap=False):
    item = QLabel(value)
    item.setObjectName(name)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(wrap)
    item.setMinimumWidth(0)
    return item


def clipboard_sources():
    mime = QApplication.clipboard().mimeData()
    if mime is None:
        return []
    return parse_mime_payload([url.toString() for url in mime.urls()], mime.text() if mime.hasText() else "")


def _matches(action_id, source):
    """Hover feedback never reads files or walks potentially slow directories."""
    spec = ACTIONS[action_id]
    if str(source).lower().startswith(("http://", "https://")):
        return spec["accept_urls"]
    return Path(str(source)).suffix.lower() in spec["extensions"]


class _WebInput(QLineEdit):
    sources_pasted = Signal(list)

    def paste(self):
        if self.isEnabled():
            self.sources_pasted.emit(clipboard_sources())

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste()
            event.accept()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("撤销", self.undo)
        menu.addAction("剪切", self.cut)
        menu.addAction("复制", self.copy)
        menu.addAction("粘贴并转换", self.paste)
        menu.addSeparator()
        menu.addAction("全选", self.selectAll)
        menu.exec(event.globalPos())

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        sources = parse_mime_payload([url.toString() for url in mime.urls()], mime.text() if mime.hasText() else "")
        if self.isEnabled() and sources:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        sources = parse_mime_payload([url.toString() for url in mime.urls()], mime.text() if mime.hasText() else "")
        if self.isEnabled() and sources:
            self.sources_pasted.emit(sources)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


class QuickActionCard(QPushButton):
    submitted = Signal(str, list)
    rejected = Signal(str)

    def __init__(self, action_id, parent=None):
        super().__init__(parent)
        self.action_id = action_id
        self.spec = ACTIONS[action_id]
        self._compact = False
        self.setObjectName("quickCard")
        self.setAccessibleName(self.spec["title"] + "，拖入或粘贴后立即转换")
        self.setAccessibleDescription(shortcut_text(self.spec["description"] + "；点击选择文件，Ctrl+V 粘贴。"))
        self.setToolTip(shortcut_text(self.spec["description"] + "\n点击选择文件；拖入即转；选中卡片后按 Ctrl+V 粘贴文件或链接。"))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumWidth(0)
        self.setMinimumHeight(102)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.body_layout = QVBoxLayout(self)
        self.body_layout.setContentsMargins(15, 12, 15, 11)
        self.body_layout.setSpacing(7)
        self.heading_layout = QHBoxLayout()
        self.glyph_label = _text(self.spec.get("glyph", "转"), "quickGlyph")
        self.glyph_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.glyph_label.setFixedSize(27, 27)
        self.glyph_label.setStyleSheet("color: #6b5ed5; background: #fcfbff; border: 1px solid #c6bde8; border-radius: 5px; font-weight: 600;")
        self.heading_layout.addWidget(self.glyph_label)
        self.title_label = _text(self.spec["title"], "quickTitle", True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.heading_layout.addWidget(self.title_label, 1)
        self.body_layout.addLayout(self.heading_layout)
        self.description_label = _text(self.spec["description"], "quickDescription", True)
        self.body_layout.addWidget(self.description_label)
        self.hint_label = _text("拖入即转  ·  点击选择  ·  Ctrl+V", "quickHint")
        self.hint_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.body_layout.addWidget(self.hint_label)
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.clicked.connect(self.choose_files)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def sizeHint(self):
        return QSize(164, 64) if self._compact else QSize(250, 110)

    def minimumSizeHint(self):
        return QSize(0, 64 if self._compact else 102)

    def set_compact(self, compact):
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self.setMinimumHeight(64 if compact else 102)
        self.setMaximumHeight(64 if compact else 16777215)
        self.body_layout.setContentsMargins(*((8, 7, 8, 6) if compact else (15, 12, 15, 11)))
        self.body_layout.setSpacing(3 if compact else 7)
        self.heading_layout.setSpacing(6)
        self.glyph_label.setFixedSize(22 if compact else 27, 22 if compact else 27)
        self.title_label.setWordWrap(not compact)
        self.description_label.setVisible(not compact)
        self.hint_label.setText(shortcut_text(_COMPACT_TYPES[self.action_id] if compact else "拖入即转  ·  点击选择  ·  Ctrl+V"))
        self.updateGeometry()

    def _menu(self, point):
        if not self.isEnabled():
            return
        menu = QMenu(self)
        menu.addAction("粘贴并立即转换", self.paste)
        menu.addAction("选择文件…", self.choose_files)
        menu.exec(self.mapToGlobal(point))

    def choose_files(self):
        if not self.isEnabled():
            return
        extensions = " ".join("*" + suffix for suffix in self.spec["extensions"])
        sources, _ = QFileDialog.getOpenFileNames(self, self.spec["title"], "", "支持的文件 (" + extensions + ")")
        if sources:
            self.submit(sources)

    def submit(self, sources):
        if not self.isEnabled():
            return False
        if not sources:
            self.rejected.emit("剪贴板中没有可用的文件或网页链接。")
            return False
        if not any(not validate_sources(self.action_id, [value]) for value in sources):
            errors = validate_sources(self.action_id, sources)
            self.rejected.emit("\n".join(errors[:3]) or "这些内容不适用于此快捷通道。")
            return False
        self.submitted.emit(self.action_id, list(sources))
        return True

    def paste(self):
        return self.submit(clipboard_sources())

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _sources(self, event):
        mime = event.mimeData()
        return parse_mime_payload([url.toString() for url in mime.urls()], mime.text() if mime.hasText() else "")

    def _active(self, value, invalid=False):
        value, invalid = bool(value), bool(invalid)
        if bool(self.property("dragActive")) == value and bool(self.property("dragInvalid")) == invalid:
            return
        self.setProperty("dragActive", value)
        self.setProperty("dragInvalid", invalid)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def dragEnterEvent(self, event):
        sources = self._sources(event)
        accepted = self.isEnabled() and bool(sources)
        compatible = any(_matches(self.action_id, value) for value in sources)
        self._active(accepted and compatible, accepted and not compatible)
        if accepted:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self._active(False)
        event.accept()

    def dropEvent(self, event):
        self._active(False)
        sources = self._sources(event)
        if self.isEnabled() and sources:
            # A drop explicitly aimed at this card must not bubble into the
            # home's unrelated "recommend tools" dialog when it is invalid.
            self.submit(sources)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


class _QuickGrid(QWidget):
    def __init__(self, cards):
        super().__init__()
        self.cards = list(cards)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        self.columns = 0
        self._compact = False
        self.setMinimumWidth(0)
        self.reflow()

    def reflow(self):
        if self._compact:
            columns = 2 if self.width() >= 280 else 1
        else:
            columns = 3 if self.width() >= 790 else 2 if self.width() >= 440 else 1
        if columns == self.columns:
            return
        self.columns = columns
        for column in range(3):
            self.grid.setColumnStretch(column, int(column < columns))
        for index, card in enumerate(self.cards):
            self.grid.removeWidget(card)
            self.grid.addWidget(card, index // columns, index % columns)

    def set_compact(self, compact):
        self._compact = bool(compact)
        self.grid.setSpacing(6 if self._compact else 10)
        for card in self.cards:
            card.set_compact(self._compact)
        self.reflow()
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()


class QuickPanel(QWidget):
    submitted = Signal(str, list)
    cancel_requested = Signal()
    results_requested = Signal()
    folder_requested = Signal()
    folder_changed = Signal(str)
    detach_requested = Signal()

    def __init__(self, settings_path=None, parent=None):
        super().__init__(parent)
        self.setObjectName("quickPanel")
        self.setMinimumWidth(0)
        self._compact = False
        self.setProperty("compact", False)
        self.settings_path = Path(settings_path) if settings_path else None
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        self.url_folder = Path(downloads or str(Path.home() / "Downloads")) / "青简快捷转换"
        self._load_folder()
        self._running = False
        self._last_outputs = []
        self._has_result = False
        self.body_layout = QVBoxLayout(self)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(10)
        title = QHBoxLayout()
        self.heading_label = _text("快捷转换", "wbSection")
        title.addWidget(self.heading_label)
        title.addStretch()
        self.heading_hint = _text("拖入即转，无需设置", "quickHint")
        title.addWidget(self.heading_hint)
        self.detach_button = QPushButton("拆出 ↗")
        self.detach_button.setObjectName("quickQuiet")
        self.detach_button.setToolTip("打开独立转换圆环：更多格式，可折叠、自由拖动。")
        self.detach_button.setAccessibleName("拆出独立格式转换模块")
        self.detach_button.clicked.connect(self.detach_requested)
        title.addWidget(self.detach_button)
        self.body_layout.addLayout(title)
        self.cards = {}
        for action_id in ACTIONS:
            card = QuickActionCard(action_id)
            card.submitted.connect(self.submitted)
            card.rejected.connect(self.notice)
            self.cards[action_id] = card
        self.grid = _QuickGrid(self.cards.values())
        self.body_layout.addWidget(self.grid)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.url_edit = _WebInput()
        self.url_edit.setObjectName("quickUrl")
        self.url_edit.setMinimumWidth(0)
        self.url_edit.setPlaceholderText("网页转 Word：粘贴链接即转，或拖入 HTML / MHTML")
        self.url_edit.setAccessibleName("快捷网页转Word链接")
        self.url_edit.returnPressed.connect(self.submit_web)
        self.url_edit.sources_pasted.connect(self._submit_pasted_web)
        row.addWidget(self.url_edit, 1)
        self.paste_button = QPushButton("粘贴并转换")
        self.paste_button.setObjectName("quickPaste")
        self.paste_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.paste_button.clicked.connect(self.submit_web)
        self.url_edit.textChanged.connect(self._show_paste_action)
        row.addWidget(self.paste_button)
        self.body_layout.addLayout(row)
        save_row = QHBoxLayout()
        save_row.setSpacing(3)
        self.folder_label = _text("", "quickHint")
        self.folder_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        save_row.addWidget(self.folder_label, 1)
        self.choose_folder_button = QPushButton("更改链接保存位置")
        self.choose_folder_button.setObjectName("quickQuiet")
        self.choose_folder_button.setToolTip("更改网页链接的结果保存位置；本地文件仍保存在原文件夹。")
        self.choose_folder_button.setAccessibleName("更改链接保存位置")
        self.choose_folder_button.clicked.connect(self.choose_folder)
        save_row.addWidget(self.choose_folder_button)
        self.body_layout.addLayout(save_row)
        self._show_folder()
        self.state = QFrame()
        self.state.setObjectName("quickState")
        self.state_layout = QVBoxLayout(self.state)
        self.state_layout.setContentsMargins(12, 9, 12, 8)
        self.state_layout.setSpacing(6)
        state_row = QHBoxLayout()
        state_row.setSpacing(4)
        self.status_label = _text("", "quickDescription", True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        state_row.addWidget(self.status_label, 1)
        self.cancel_button = QPushButton("停止")
        self.cancel_button.setObjectName("quickQuiet")
        self.cancel_button.setToolTip("停止当前快捷转换；保留已经完成的结果。")
        self.cancel_button.clicked.connect(self._cancel)
        self.result_button = QPushButton("查看结果")
        self.result_button.setObjectName("quickQuiet")
        self.result_button.setToolTip("查看完整转换结果、失败原因和提示。")
        self.result_button.clicked.connect(self.results_requested)
        self.open_folder_button = QPushButton("打开文件夹")
        self.open_folder_button.setObjectName("quickQuiet")
        self.open_folder_button.setToolTip("打开转换结果所在的文件夹。")
        self.open_folder_button.clicked.connect(self.folder_requested)
        for item in (self.cancel_button, self.result_button, self.open_folder_button):
            state_row.addWidget(item)
        self.state_layout.addLayout(state_row)
        self.progress = QProgressBar()
        self.progress.setObjectName("quickProgress")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.state_layout.addWidget(self.progress)
        self.body_layout.addWidget(self.state)
        self.state.hide()
        self.set_running(False)

    def set_compact(self, compact):
        """Keep quick drop targets within reach in the home's small window."""
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self.setProperty("compact", compact)
        self.body_layout.setSpacing(6 if compact else 10)
        self.grid.set_compact(compact)
        self.heading_label.setMinimumHeight(18 if compact else 0)
        self.heading_label.setMaximumHeight(18 if compact else 16777215)
        self.heading_label.setStyleSheet("font-size: 13px; font-weight: 600;" if compact else "")
        self.heading_hint.setVisible(not compact)
        self.url_edit.setPlaceholderText("粘贴网页链接，或拖入本地网页" if compact else
                                         "网页转 Word：粘贴链接即转，或拖入 HTML / MHTML")
        self.url_edit.setToolTip("网页转 Word：粘贴链接即转；支持 HTML、MHT、MHTML 本地网页。")
        self.choose_folder_button.setText("更改" if compact else "更改链接保存位置")
        for control in (self.url_edit, self.paste_button):
            control.setMinimumHeight(30 if compact else 0)
            control.setMaximumHeight(30 if compact else 16777215)
        self.choose_folder_button.setMinimumHeight(18 if compact else 0)
        self.choose_folder_button.setMaximumHeight(18 if compact else 16777215)
        self.folder_label.setMinimumHeight(18 if compact else 0)
        self.folder_label.setMaximumHeight(18 if compact else 16777215)
        self.state_layout.setContentsMargins(*((8, 5, 8, 5) if compact else (12, 9, 12, 8)))
        self.state_layout.setSpacing(3 if compact else 6)
        self.status_label.setMinimumHeight(28 if compact else 0)
        self.status_label.setMaximumHeight(28 if compact else 16777215)
        self.state.setMinimumHeight(44 if compact else 0)
        self.state.setMaximumHeight(44 if compact else 16777215)
        self.setUpdatesEnabled(False)
        try:
            for control in (self, self.url_edit, self.paste_button, self.detach_button, self.choose_folder_button,
                            self.cancel_button, self.result_button, self.open_folder_button,
                            *[card.title_label for card in self.cards.values()],
                            *[card.glyph_label for card in self.cards.values()]):
                control.style().unpolish(control)
                control.style().polish(control)
                control.updateGeometry()
            self._show_paste_action()
            self._show_folder()
            self.updateGeometry()
        finally:
            self.setUpdatesEnabled(True)

    def _show_paste_action(self, value=None):
        value = self.url_edit.text() if value is None else value
        self.paste_button.setText("立即转换" if value.strip() else
                                  "粘贴转换" if self._compact else "粘贴并转换")

    def _set_status(self, message):
        message = str(message)
        self.status_label.setText(message)
        self.status_label.setToolTip(message)
        self.status_label.setAccessibleDescription(message)

    def _load_folder(self):
        try:
            if self.settings_path and self.settings_path.is_file() and self.settings_path.stat().st_size < 65536:
                value = json.loads(self.settings_path.read_text(encoding="utf-8")).get("url_folder")
                if isinstance(value, str) and value and "\x00" not in value and Path(value).is_absolute():
                    self.url_folder = Path(value)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            pass

    def _show_folder(self):
        full = "本地文件存回原文件夹；链接保存到：" + str(self.url_folder)
        self.folder_label.setToolTip(full)
        self.folder_label.setAccessibleName(full)
        visible = "链接保存到：" + str(self.url_folder) if self._compact else full
        self.folder_label.setText(self.folder_label.fontMetrics().elidedText(visible, Qt.TextElideMode.ElideMiddle, max(0, self.folder_label.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._show_folder()

    def choose_folder(self, checked=False, *, dialog_parent=None):
        if self._running:
            return
        value = QFileDialog.getExistingDirectory(dialog_parent or self, "链接转换结果保存位置", str(self.url_folder))
        if not value:
            return
        self.url_folder = Path(value)
        self._show_folder()
        self.folder_changed.emit(str(self.url_folder))
        staged = None
        try:
            if self.settings_path:
                self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                handle, name = tempfile.mkstemp(prefix=".qingjian-quick-", suffix=".tmp", dir=self.settings_path.parent)
                staged = Path(name)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump({"url_folder": str(self.url_folder)}, stream, ensure_ascii=False)
                os.replace(staged, self.settings_path)
        except OSError as exc:
            self.notice("本次保存位置已更改，但未能记住设置：" + str(exc))
        finally:
            if staged:
                staged.unlink(missing_ok=True)

    def submit_web(self):
        if self._running:
            return False
        sources = parse_mime_payload([], self.url_edit.text()) if self.url_edit.text().strip() else clipboard_sources()
        started = self.cards["web_to_word"].submit(sources)
        if started:
            self.url_edit.clear()
        return started

    def _submit_pasted_web(self, sources):
        if self.cards["web_to_word"].submit(sources):
            self.url_edit.clear()

    def notice(self, message):
        if self._running:
            return
        self.state.show()
        self._set_status(message)
        self.progress.hide()
        self.result_button.setEnabled(self._has_result)
        self.open_folder_button.setEnabled(bool(self._last_outputs))

    def set_running(self, running):
        self._running = running
        for control in [*self.cards.values(), self.url_edit, self.paste_button, self.choose_folder_button]:
            control.setEnabled(not running)
        self.cancel_button.setVisible(running)
        self.cancel_button.setEnabled(running)
        self.result_button.setVisible(not running)
        self.open_folder_button.setVisible(not running)
        self.result_button.setEnabled(self._has_result)
        self.open_folder_button.setEnabled(bool(self._last_outputs))

    def begin(self, action_id, count, title=None):
        self._last_outputs = []
        self._has_result = False
        self.set_running(True)
        self.state.show()
        self.progress.show()
        self.progress.setRange(0, 0)
        title = title or ACTIONS.get(action_id, {}).get("title", "格式转换")
        self._set_status(title + f" · 正在处理 {count} 项…")

    def update_progress(self, done, total, message):
        self.progress.setRange(0, max(1, total) if done else 0)
        if done:
            self.progress.setValue(done)
        self._set_status(message or f"正在处理 {done} / {total}")

    def finish(self, result):
        self._last_outputs = list(result.get("outputs", []))
        self._has_result = bool(result.get("records") or result.get("errors"))
        failures = sum(row.get("status") == "failed" for row in result.get("records", []))
        if not result.get("records") and result.get("errors"):
            failures = len(result["errors"])
        prefix = "已停止" if result.get("cancelled") else "转换完成"
        message = f"{prefix} · 成功 {len(self._last_outputs)} 项，失败 {failures} 项"
        if result.get("warnings"):
            message += " · 有提示，请查看结果"
        if result.get("errors") and not self._last_outputs:
            message += " · " + str(result["errors"][0])
        self._set_status(message)
        self.progress.setRange(0, max(1, result.get("total", len(result.get("records", [])))))
        self.progress.setValue(sum(row.get("status") in {"success", "failed"} for row in result.get("records", [])))
        self.result_button.setText("查看详情" if failures and not self._last_outputs else "查看结果")
        self.set_running(False)

    def _cancel(self):
        self.cancel_button.setEnabled(False)
        self._set_status("正在停止，已完成的结果会保留…")
        self.cancel_requested.emit()
