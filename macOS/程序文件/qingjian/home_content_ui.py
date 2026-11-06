"""Compact home display: local pictures, personal lines and read-only reminders."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRect, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QImageReader, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from .home_content import ContentRotation, ContentSnapshot, HomeContentStore, scan_content, upcoming_reminders

_STYLE = """
QFrame#personalHome {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #fcfdff);
    border: 1px solid #dae0ec; border-radius: 18px;
}
QFrame#personalHome QLabel { background: transparent; border: none; }
QLabel#personalTitle { color: #313a50; font-size: 12px; font-weight: 600; }
QLabel#personalMeta { color: #8891a3; font-size: 10px; }
QLabel#personalNotice { color: #8c6d46; font-size: 10px; }
QLabel#personalEmpty { color: #848ea0; font-size: 11px; padding: 4px; }
QLabel#personalQuote { color: #40485e; padding: 10px 18px; }
QFrame#personalQuoteBox {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f8f8fc, stop:1 #f3f4fa);
    border: 1px solid #eceef6; border-radius: 14px;
}
QFrame#personalReminders { background: transparent; border: none; }
QWidget#personalImageBar { background: transparent; border: none; }
QWidget#personalImageBar QLabel { color: #687084; }
QToolButton#personalAction {
    border: none; border-radius: 9px; background: transparent;
    color: #7f7a9c; padding: 2px 7px; font-size: 10px;
}
QToolButton#personalAction:hover { background: #efecfa; color: #54468c; }
QToolButton#personalAction:pressed { background: #e5e0f7; }
QToolButton#personalAction:disabled { color: #c4c8d2; }
QWidget#personalImageBar QToolButton#personalAction {
    color: #655e84; background: rgba(255, 255, 255, 185);
    border-radius: 10px; min-width: 16px; font-size: 12px;
}
QWidget#personalImageBar QToolButton#personalAction:hover { background: #ffffff; color: #493d82; }
QWidget#personalImageBar QToolButton#personalAction:disabled { color: #c0c3cd; background: rgba(255, 255, 255, 95); }
QFrame#personalTabs { background: #f1f2f8; border: 1px solid #e8eaf2; border-radius: 12px; }
QToolButton#personalTab {
    border: none; border-radius: 9px; background: transparent;
    color: #7e8698; padding: 2px 11px; font-size: 10px;
}
QToolButton#personalTab:hover { color: #564a8e; }
QToolButton#personalTab:checked { background: #ffffff; color: #514488; font-weight: 600; }
QFrame#personalDivider { background: #eceef4; border: none; }
"""
_PRIORITY = {"urgent": ("紧急", "#b96570"), "important": ("重要", "#b38c45"),
             "normal": ("普通", "#7a88b0"), "low": ("低", "#7c9d91")}
_MODE_LABELS = (("顺序循环", "sequential"), ("随机循环", "random"), ("单个不循环", "single"))
_DISPLAY_LABELS = (("图片", "images"), ("文字", "quotes"), ("待办事项", "reminders"))


def _label(text="", object_name="personalMeta", parent=None):
    label = QLabel(text, parent)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.PlainText)
    return label


def _button(text, tooltip, callback=None):
    button = QToolButton()
    button.setObjectName("personalAction")
    button.setText(text)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setCursor(Qt.PointingHandCursor)
    if callback is not None:
        button.clicked.connect(callback)
    return button


class _WorkerSignals(QObject):
    finished = Signal(int, object)


class _ScanTask(QRunnable):
    def __init__(self, token, folder):
        super().__init__()
        self.token, self.folder = token, folder
        self.signals = _WorkerSignals()

    def run(self):
        try:
            result = scan_content(self.folder)
        except Exception:
            result = ContentSnapshot(warnings=("素材读取失败，请检查文件夹后刷新。",), folder=self.folder)
        self.signals.finished.emit(self.token, result)


def read_display_image(path: Path, size: QSize) -> tuple[QImage, str]:
    """Decode into a bounded display image in a worker, never into a GUI pixmap."""
    try:
        if path.is_symlink() or not path.is_file():
            return QImage(), "图片已移走或无法访问，请刷新素材。"
        if path.stat().st_size > 32 * 1024 * 1024:
            return QImage(), "图片超过 32 MB，请使用较小的展示副本。"
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        original = reader.size()
        if not original.isValid() or original.width() * original.height() > 25_000_000:
            return QImage(), "图片无效或超过 2500 万像素，请换用较小的展示副本。"
        target = original.scaled(size.expandedTo(QSize(1, 1)), Qt.KeepAspectRatio)
        if target.width() < original.width() or target.height() < original.height():
            reader.setScaledSize(target)
        result = reader.read()
        if result.isNull():
            return QImage(), "图片无法解码，请检查格式或文件是否完整。"
        if result.width() > size.width() or result.height() > size.height():
            result = result.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return result, ""
    except (OSError, ValueError, RuntimeError):
        return QImage(), "图片无法读取，请检查文件后刷新。"


class _ImageTask(QRunnable):
    def __init__(self, token, path, size, key):
        super().__init__()
        self.token, self.path, self.size, self.key = token, path, size, key
        self.signals = _WorkerSignals()

    def run(self):
        try:
            result, error = read_display_image(self.path, self.size)
        except Exception:
            result, error = QImage(), "图片解码失败，请检查文件后刷新。"
        self.signals.finished.emit(self.token, (self.key, result, error))


class _ImageCanvas(QWidget):
    resized = Signal()

    def __init__(self):
        super().__init__()
        self.image = QImage()
        self._backdrop = QColor("#f3f3f7")
        self.message = ""
        self.setObjectName("personalImage")
        # Grows with the window; the picture is always fitted completely.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch()
        bar = self.bar = QWidget()
        bar.setObjectName("personalImageBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 0, 8, 6)
        row.setSpacing(4)
        self.caption = _label()
        self.caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.caption, 1)
        self.previous = _button("‹", "上一张图片")
        self.next = _button("›", "下一张图片")
        row.addWidget(self.previous)
        row.addWidget(self.next)
        layout.addWidget(bar)

    def set_image(self, image, message=""):
        self.image = image
        self.message = message
        self._backdrop = QColor("#f3f3f7")
        if not image.isNull():
            average = image.scaled(1, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).pixelColor(0, 0)
            self._backdrop = QColor((average.red() + 2 * 247) // 3, (average.green() + 2 * 246) // 3,
                                    (average.blue() + 2 * 244) // 3)
        self.update()

    def image_area(self):
        """Keep the full picture above, rather than underneath, the controls."""
        return QRect(0, 0, self.width(), max(1, self.height() - self.bar.height()))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(0, 0, -1, -1), 14, 14)
        painter.fillPath(path, QColor("#f3f3f7"))
        painter.setClipPath(path)
        area = self.image_area()
        if not self.image.isNull():
            # A pale tint of the picture's own average colour fills the
            # letterbox, so any ratio blends calmly into the pane.
            painter.fillRect(self.rect(), self._backdrop)
            target = self.image.size().scaled(area.size(), Qt.KeepAspectRatio)
            x, y = (area.width() - target.width()) // 2, (area.height() - target.height()) // 2
            painter.drawImage(QRect(x, y, target.width(), target.height()), self.image)
        elif self.message:
            painter.setPen(QColor("#8a93a3"))
            painter.drawText(area.adjusted(16, 10, -16, -10), Qt.AlignCenter | Qt.TextWordWrap, self.message)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class HomeContentSettingsDialog(QDialog):
    """Edits a draft; Save commits only after the parent successfully persists it."""

    def __init__(self, settings, snapshot=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("自定义首页")
        self.resize(510, 565)
        self.setMinimumWidth(390)
        self._settings = dict(settings)
        self._snapshot = snapshot or ContentSnapshot()
        self._scan_token = 0
        self._jobs = {}
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(3, 3, 10, 3)
        tip = _label("图片、文字、待办事项三选一，占满下方展示区。可直接使用首页按钮切换，图片与文字各自记住播放方式。", "personalEmpty")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        display_form = QFormLayout()
        self.display_mode = QComboBox()
        for caption, key in _DISPLAY_LABELS:
            self.display_mode.addItem(caption, key)
        self.display_mode.setCurrentIndex(self.display_mode.findData(settings["display_mode"]))
        display_form.addRow("当前展示", self.display_mode)
        layout.addLayout(display_form)
        layout.addWidget(_label("素材文件夹", "personalTitle"))
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(settings["folder"])
        self.folder.setPlaceholderText("选择放有图片和 TXT 的本地文件夹")
        self.folder.setAccessibleName("首页素材文件夹")
        self.folder.editingFinished.connect(self.reload_choices)
        browse = QPushButton("选择")
        browse.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)
        description = _label("读取文件夹第一层的 JPG、PNG、WEBP 等常见图片和 TXT。TXT 有空行时每段一条，没有空行时每行一条；支持 UTF-8、UTF-16、GB18030。图片按比例完整展示，动图展示首帧。", "personalEmpty")
        description.setWordWrap(True)
        layout.addWidget(description)
        self.modes, self.intervals, self.fixed = {}, {}, {}
        for kind, title in (("image", "图片"), ("quote", "文案")):
            layout.addWidget(_label(title + "播放", "personalTitle"))
            form = QFormLayout()
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            combo = QComboBox()
            for caption, key in _MODE_LABELS:
                combo.addItem(caption, key)
            combo.setCurrentIndex(combo.findData(settings[kind + "_mode"]))
            interval = QSpinBox()
            interval.setRange(3, 3600)
            interval.setSuffix(" 秒")
            interval.setValue(settings[kind + "_interval"])
            fixed = QComboBox()
            fixed.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            fixed.setMinimumContentsLength(15)
            fixed.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.modes[kind], self.intervals[kind], self.fixed[kind] = combo, interval, fixed
            combo.currentIndexChanged.connect(self._mode_changed)
            form.addRow("方式", combo)
            form.addRow("间隔", interval)
            form.addRow("固定内容", fixed)
            layout.addLayout(form)
        form = QFormLayout()
        self.days = QSpinBox()
        self.days.setRange(1, 31)
        self.days.setSuffix(" 天（含今天）")
        self.days.setValue(settings["reminder_days"])
        form.addRow("近期事项", self.days)
        layout.addLayout(form)
        note = _label("同时显示未完成的逾期事项。新增、编辑和实际弹窗提醒仍由“记事与提醒”管理。首页不修改事项或提醒状态。", "personalEmpty")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = _label("", "personalNotice")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._fill_choices(self._snapshot)
        self._mode_changed()
        if self.folder.text().strip() != self._snapshot.folder:
            QTimer.singleShot(0, self.reload_choices)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择首页素材文件夹", self.folder.text())
        if folder:
            self.folder.setText(folder)
            self.reload_choices()

    def reload_choices(self):
        folder = self.folder.text().strip()
        if folder == self._snapshot.folder:
            return
        self._scan_token += 1
        token = self._scan_token
        self.status.setText("正在读取素材目录…")
        task = _ScanTask(token, folder)
        task.signals.finished.connect(self._choices_ready)
        self._jobs[token] = task
        QThreadPool.globalInstance().start(task)

    @Slot(int, object)
    def _choices_ready(self, token, snapshot):
        self._jobs.pop(token, None)
        if token != self._scan_token:
            return
        self._fill_choices(snapshot)

    def _fill_choices(self, snapshot):
        self._snapshot = snapshot
        for kind in ("image", "quote"):
            combo = self.fixed[kind]
            selected = combo.currentData() or self._settings["fixed_" + kind]
            combo.clear()
            combo.addItem("第一项", "")
            values = ((path.name, path.name) for path in snapshot.images) if kind == "image" else ((quote.text.replace("\n", " ")[:55], quote.key) for quote in snapshot.quotes)
            for caption, key in values:
                combo.addItem(caption, key)
            match = combo.findData(selected)
            combo.setCurrentIndex(match if match >= 0 else 0)
        text = f"已找到 {len(snapshot.images)} 张图片、{len(snapshot.quotes)} 条文案。" if snapshot.folder else "尚未选择素材文件夹。"
        if snapshot.warnings:
            text += " " + " ".join(snapshot.warnings)
        self.status.setText(text)

    def _mode_changed(self):
        for kind, combo in self.modes.items():
            single = combo.currentData() == "single"
            self.fixed[kind].setEnabled(single)
            self.intervals[kind].setEnabled(not single)

    def values(self):
        result = dict(self._settings)
        result["folder"] = self.folder.text().strip()
        result["display_mode"] = self.display_mode.currentData()
        for kind in ("image", "quote"):
            result[kind + "_mode"] = self.modes[kind].currentData()
            result[kind + "_interval"] = self.intervals[kind].value()
            result["fixed_" + kind] = self.fixed[kind].currentData() or ""
        result["reminder_days"] = self.days.value()
        return result


class PersonalHomePanel(QFrame):
    notebook_requested = Signal()
    content_loaded = Signal()
    settings_changed = Signal(dict)

    def __init__(self, settings_path=None, parent=None):
        super().__init__(parent)
        self.setObjectName("personalHome")
        self.setStyleSheet(_STYLE)
        # Fills the rest of the home page, so the picture or text pane follows
        # the window size instead of leaving an empty band underneath.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.store = HomeContentStore(settings_path)
        self.snapshot = ContentSnapshot()
        self._notebook_store = None
        self._reminder_token = None
        self._compact = None
        self._scan_token = self._image_token = 0
        self._jobs = {}
        self._image_cache = OrderedDict()
        self._displayed_image_path = None
        self._requested_image_key = None
        self._active_image_token = None
        self._pending_image_request = None
        self.image_rotation, self.quote_rotation = ContentRotation(), ContentRotation()
        self.image_timer = QTimer(self)
        self.quote_timer = QTimer(self)
        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(15000)
        self.image_timer.timeout.connect(lambda: self._step_image(1, automatic=True))
        self.quote_timer.timeout.connect(lambda: self._step_quote(1, automatic=True))
        self.reminder_timer.timeout.connect(self._check_reminders)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self._show_image)
        self._quote_fit_timer = QTimer(self)
        self._quote_fit_timer.setSingleShot(True)
        self._quote_fit_timer.timeout.connect(self._fit_quote_font)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 14)
        outer.setSpacing(8)
        heading = QHBoxLayout()
        heading.setSpacing(4)
        title = _label("我的一隅", "personalTitle")
        # The title yields first in very narrow windows, so this row never
        # widens the home page beyond its viewport.
        title.setMinimumWidth(0)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        heading.addWidget(title, 1)
        self.refresh_button = _button("刷新", "重新读取图片、TXT 和近期事项", self.refresh_content)
        self.settings_button = _button("自定义", "设置首页内容与轮播方式", self.open_settings)
        heading.addWidget(self.refresh_button)
        heading.addWidget(self.settings_button)
        outer.addLayout(heading)
        # A small segmented control on its own row keeps the switcher light,
        # never widens narrow windows, and leaves the height to the picture
        # or text shown below it.
        self.mode_tabs = QFrame()
        self.mode_tabs.setObjectName("personalTabs")
        tabs = QHBoxLayout(self.mode_tabs)
        tabs.setContentsMargins(3, 3, 3, 3)
        tabs.setSpacing(2)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons = {}
        for caption, key in _DISPLAY_LABELS:
            button = _button(caption, "切换为" + caption,
                             lambda checked=False, mode=key: self.select_mode(mode))
            button.setObjectName("personalTab")
            button.setCheckable(True)
            self.mode_group.addButton(button)
            self.mode_buttons[key] = button
            tabs.addWidget(button)
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.addWidget(self.mode_tabs)
        tab_row.addStretch()
        outer.addLayout(tab_row)
        self.notice = _label("", "personalNotice")
        self.notice.setWordWrap(True)
        outer.addWidget(self.notice)
        self.empty = _label("选择一个素材文件夹，放入喜欢的图片与 TXT 文案。", "personalEmpty")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.empty, 1)
        self.image_view = _ImageCanvas()
        self.image_view.previous.clicked.connect(lambda: self._step_image(-1))
        self.image_view.next.clicked.connect(lambda: self._step_image(1))
        self.image_view.resized.connect(self._queue_image_resize)
        outer.addWidget(self.image_view, 1)
        self.quote_box = QFrame()
        self.quote_box.setObjectName("personalQuoteBox")
        quotes_layout = QVBoxLayout(self.quote_box)
        quotes_layout.setContentsMargins(12, 8, 8, 8)
        quotes_layout.setSpacing(0)
        quote_toolbar = QHBoxLayout()
        quote_toolbar.setSpacing(1)
        self.quote_caption = _label("写给自己")
        self.quote_caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        quote_toolbar.addWidget(self.quote_caption, 1)
        self.quote_previous = _button("‹", "上一条文案", lambda: self._step_quote(-1))
        self.quote_next = _button("›", "下一条文案", lambda: self._step_quote(1))
        quote_toolbar.addWidget(self.quote_previous)
        quote_toolbar.addWidget(self.quote_next)
        quotes_layout.addLayout(quote_toolbar)
        self.quote_scroll = QScrollArea()
        self.quote_scroll.setWidgetResizable(True)
        self.quote_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.quote_scroll.setFrameShape(QFrame.NoFrame)
        self.quote_text = _label("", "personalQuote")
        self.quote_text.setWordWrap(True)
        self.quote_text.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.quote_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.quote_text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        self.quote_scroll.setWidget(self.quote_text)
        quotes_layout.addWidget(self.quote_scroll, 1)
        self.quote_scroll.viewport().installEventFilter(self)
        outer.addWidget(self.quote_box, 1)
        self.reminders = QFrame()
        self.reminders.setObjectName("personalReminders")
        reminders_layout = QVBoxLayout(self.reminders)
        reminders_layout.setContentsMargins(0, 1, 0, 0)
        reminders_layout.setSpacing(2)
        reminder_heading = QHBoxLayout()
        self.reminder_caption = _label("近期事项")
        reminder_heading.addWidget(self.reminder_caption, 1)
        self.notebook_button = _button("记事与提醒 ↗", "打开记事与提醒", self.notebook_requested.emit)
        reminder_heading.addWidget(self.notebook_button)
        reminders_layout.addLayout(reminder_heading)
        self.reminder_body = QWidget()
        self.reminder_rows = QVBoxLayout(self.reminder_body)
        self.reminder_rows.setContentsMargins(0, 0, 0, 0)
        self.reminder_rows.setSpacing(2)
        reminders_layout.addWidget(self.reminder_body)
        reminders_layout.addStretch()
        outer.addWidget(self.reminders, 1)
        self.set_compact(True)
        self._apply_snapshot(ContentSnapshot())
        QTimer.singleShot(0, self.refresh_content)

    @property
    def settings(self):
        return self.store.settings

    def set_compact(self, compact):
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        # A minimum keeps the pane readable; any extra window height goes to it.
        height = 190 if compact else 260
        for widget in (self.image_view, self.quote_box, self.reminders, self.empty):
            widget.setMinimumHeight(height)
            widget.setMaximumHeight(16777215)
            widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.quote_text.setAlignment(Qt.AlignCenter)
        self._queue_quote_fit()
        self.refresh_reminders()
        self._queue_image_resize()

    def select_mode(self, mode):
        try:
            self.apply_settings({"display_mode": mode})
        except (OSError, ValueError) as exc:
            self.mode_buttons[self.settings["display_mode"]].setChecked(True)
            self.notice.setText(str(exc))
            self.notice.show()

    def set_notebook_store(self, store):
        self._notebook_store = store
        self._reminder_token = None
        self.refresh_reminders()

    def apply_settings(self, values):
        previous = self.settings
        updated = self.store.update(values)
        self.settings_changed.emit(updated)
        if previous["folder"] != updated["folder"]:
            self.refresh_content()
        else:
            self._apply_snapshot(self.snapshot)

    def open_settings(self):
        dialog = HomeContentSettingsDialog(self.settings, self.snapshot, self)
        if dialog.exec() == QDialog.Accepted:
            try:
                self.apply_settings(dialog.values())
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "首页设置未保存", str(exc))
        dialog.deleteLater()

    def refresh_content(self):
        self.refresh_reminders()
        self._scan_token += 1
        token = self._scan_token
        folder = self.settings["folder"]
        if not folder:
            self._apply_snapshot(ContentSnapshot())
            self.content_loaded.emit()
            return
        self.refresh_button.setEnabled(False)
        self.notice.setText("正在读取素材目录…")
        self.notice.show()
        task = _ScanTask(token, folder)
        task.signals.finished.connect(self._scan_ready)
        self._jobs[("scan", token)] = task
        QThreadPool.globalInstance().start(task)

    @Slot(int, object)
    def _scan_ready(self, token, snapshot):
        self._jobs.pop(("scan", token), None)
        if token != self._scan_token:
            return
        self._image_cache.clear()
        self._requested_image_key = None
        self._apply_snapshot(snapshot)
        self.content_loaded.emit()

    def _apply_snapshot(self, snapshot):
        self.snapshot = snapshot
        settings = self.settings
        self.image_rotation.reset([path.name for path in snapshot.images], settings["image_mode"], settings["fixed_image"], self.image_rotation.current)
        self.quote_rotation.reset([item.key for item in snapshot.quotes], settings["quote_mode"], settings["fixed_quote"], self.quote_rotation.current)
        has_folder = bool(settings["folder"])
        mode = settings["display_mode"]
        self.mode_buttons[mode].setChecked(True)
        self.image_view.setVisible(mode == "images" and has_folder)
        self.quote_box.setVisible(mode == "quotes" and has_folder)
        self.reminders.setVisible(mode == "reminders")
        self.empty.setVisible(not has_folder and mode != "reminders")
        self.empty.setText("点击“自定义”，选择放有图片的素材文件夹。" if mode == "images"
                           else "点击“自定义”，选择放有 TXT 文案的素材文件夹。")
        notices = [self.store.last_error, *snapshot.warnings]
        notices = [item for item in notices if item]
        self.notice.setText(notices[0] if notices else "")
        self.notice.setToolTip("\n".join(notices))
        self.notice.setVisible(bool(notices))
        self.refresh_button.setEnabled(True)
        self._show_image()
        self._show_quote()
        self.refresh_reminders()
        self._sync_timers()

    def _queue_image_resize(self):
        if self.isVisible() and self.settings["display_mode"] == "images":
            self._resize_timer.start()

    def _show_image(self):
        images = self.snapshot.images
        enabled = len(images) > 1
        self.image_view.previous.setEnabled(enabled)
        self.image_view.next.setEnabled(enabled)
        if not images:
            self._image_token += 1
            self._requested_image_key = None
            self._pending_image_request = None
            self._displayed_image_path = None
            self.image_view.caption.setText("图片")
            self.image_view.set_image(QImage(), "文件夹内暂无支持的图片")
            return
        path = images[self.image_rotation.index]
        suffix = " · 固定" if self.image_rotation.mode == "single" else ""
        caption = f"{self.image_rotation.index + 1}/{len(images)} · {path.name}{suffix}"
        self.image_view.caption.setText(caption)
        self.image_view.caption.setToolTip(caption)
        if self.settings["display_mode"] != "images" or not self.isVisible():
            self._pending_image_request = None
            return
        ratio = min(2.0, max(1.0, self.devicePixelRatioF()))
        area = self.image_view.image_area()
        size = QSize(min(1600, max(80, int(area.width() * ratio))), min(1400, max(1, int(area.height() * ratio))))
        key = (str(path), size.width(), size.height())
        unchanged = key == self._requested_image_key
        if not unchanged:
            self._image_token += 1
            self._requested_image_key = key
        cached = self._image_cache.get(key)
        if cached is not None:
            self._pending_image_request = None
            self._image_cache.move_to_end(key)
            self.image_view.set_image(*cached)
            self._displayed_image_path = str(path) if not cached[0].isNull() else None
            self._requested_image_key = key
            return
        pending_token = self._pending_image_request[0] if self._pending_image_request else None
        if unchanged and self._image_token in (self._active_image_token, pending_token):
            return
        token = self._image_token
        if self._displayed_image_path != str(path) or self.image_view.image.isNull():
            self.image_view.set_image(QImage(), "正在载入图片…")
            self._displayed_image_path = None
        # Repeated clicks and resizes replace this tuple; they never enqueue
        # more than one active decoder per panel in the shared thread pool.
        self._pending_image_request = (token, path, size, key)
        self._start_pending_image()

    def _start_pending_image(self):
        if self._active_image_token is not None or self._pending_image_request is None:
            return
        token, path, size, key = self._pending_image_request
        self._pending_image_request = None
        if token != self._image_token or key != self._requested_image_key or not self.isVisible() or self.settings["display_mode"] != "images":
            return
        task = _ImageTask(token, path, size, key)
        task.signals.finished.connect(self._image_ready)
        self._active_image_token = token
        self._jobs[("image", token)] = task
        QThreadPool.globalInstance().start(task)

    @Slot(int, object)
    def _image_ready(self, token, payload):
        self._jobs.pop(("image", token), None)
        if token == self._active_image_token:
            self._active_image_token = None
        key, image, error = payload
        if token == self._image_token and key == self._requested_image_key:
            self._image_cache[key] = (image, error)
            while len(self._image_cache) > 10:
                self._image_cache.popitem(last=False)
            self.image_view.set_image(image, error)
            self._displayed_image_path = key[0] if not image.isNull() else None
        self._start_pending_image()

    def _show_quote(self):
        quotes = self.snapshot.quotes
        self.quote_previous.setEnabled(len(quotes) > 1)
        self.quote_next.setEnabled(len(quotes) > 1)
        if not quotes:
            self.quote_caption.setText("写给自己")
            self.quote_text.setText("在素材文件夹中放入 TXT，写下想看到的话。")
            self._queue_quote_fit()
            self.quote_text.setToolTip("")
            return
        item = quotes[self.quote_rotation.index]
        suffix = " · 固定" if self.quote_rotation.mode == "single" else ""
        self.quote_caption.setText(f"写给自己 · {self.quote_rotation.index + 1}/{len(quotes)}{suffix}")
        self.quote_caption.setToolTip(item.source)
        self.quote_text.setText(item.text)
        self._queue_quote_fit()
        self.quote_scroll.verticalScrollBar().setValue(0)

    def eventFilter(self, watched, event):
        if watched is self.quote_scroll.viewport() and event.type() == QEvent.Resize:
            self._queue_quote_fit()
        return super().eventFilter(watched, event)

    def _queue_quote_fit(self):
        # Child layout sizes are final only after the enclosing resize event.
        # One owned timer also coalesces rapid window resizing.
        if self.quote_box.isVisible():
            self._quote_fit_timer.start(0)
        else:
            self._quote_fit_timer.stop()

    def _fit_quote_font(self):
        """Scale the line to the pane: large for short lines, never overflowing."""
        if not self.quote_box.isVisible():
            return
        viewport = self.quote_scroll.viewport().size()
        width = max(60, viewport.width() - 40)
        height = max(40, viewport.height() - 24)
        text = self.quote_text.text() or " "
        font = QFont(self.quote_text.font())
        size = max(13, min(30, int(min(width / 10, height / 3.4))))
        while size > 13:
            font.setPixelSize(size)
            bounds = QFontMetrics(font).boundingRect(QRect(0, 0, width, 100000), Qt.TextWordWrap | Qt.AlignCenter, text)
            if bounds.height() <= height:
                break
            size -= 1
        if getattr(self, "_quote_px", None) != size:
            self._quote_px = size
            self.quote_text.setStyleSheet(f"font-size:{size}px;")

    def _step_image(self, direction, automatic=False):
        if automatic and (not self.isVisible() or self.settings["display_mode"] != "images"):
            return
        self.image_rotation.step(direction, automatic=automatic)
        self._show_image()
        if not automatic:
            self._remember_fixed("image", self.image_rotation.current)
            self._sync_timers()

    def _step_quote(self, direction, automatic=False):
        if automatic and (not self.isVisible() or self.settings["display_mode"] != "quotes"):
            return
        self.quote_rotation.step(direction, automatic=automatic)
        self._show_quote()
        if not automatic:
            self._remember_fixed("quote", self.quote_rotation.current)
            self._sync_timers()

    def _remember_fixed(self, kind, key):
        if self.settings[kind + "_mode"] == "single" and key:
            try:
                self.store.update({"fixed_" + kind: key})
            except (OSError, ValueError) as exc:
                self.notice.setText(str(exc))
                self.notice.show()

    def _sync_timers(self):
        settings = self.settings
        for kind, timer, count in (("image", self.image_timer, len(self.snapshot.images)), ("quote", self.quote_timer, len(self.snapshot.quotes))):
            display_mode = "images" if kind == "image" else "quotes"
            running = self.isVisible() and settings["display_mode"] == display_mode and settings[kind + "_mode"] != "single" and count > 1
            if running:
                timer.start(settings[kind + "_interval"] * 1000)
            else:
                timer.stop()
        if self.isVisible() and settings["display_mode"] == "reminders":
            self.reminder_timer.start()
        else:
            self.reminder_timer.stop()

    def _check_reminders(self):
        now = datetime.now()
        token = (getattr(self._notebook_store, "revision", None), now.strftime("%Y-%m-%d %H:%M"))
        if token != self._reminder_token:
            self.refresh_reminders()

    def refresh_reminders(self):
        if not hasattr(self, "reminder_rows"):
            return
        now = datetime.now()
        self._reminder_token = (getattr(self._notebook_store, "revision", None), now.strftime("%Y-%m-%d %H:%M"))
        while self.reminder_rows.count():
            item = self.reminder_rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        days = self.settings["reminder_days"]
        try:
            rows = upcoming_reminders(self._notebook_store, now=now, days=days)
        except (OSError, ValueError, TypeError):
            rows = []
        self.reminder_caption.setText(f"近期 {days} 天" + (f" · {len(rows)} 项" if rows else ""))
        if not rows:
            label = _label("暂无近期未完成事项。", "personalEmpty")
            self.reminder_rows.addWidget(label)
            return
        for item in rows[:5 if self._compact else 8]:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(1, 0, 0, 0)
            layout.setSpacing(5)
            name, color = _PRIORITY.get(item["priority"], _PRIORITY["normal"])
            dot = _label("●")
            dot.setStyleSheet(f"color:{color};font-size:8px;")
            dot.setToolTip("重要程度：" + name)
            title = _label(item["title"])
            title.setStyleSheet("color:#565f73;font-size:11px;padding:4px 0;")
            title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            title.setToolTip(item["title"] + ("\n" + item["body"] if item["body"] else ""))
            when = _label(item["label"] + (" ↻" if item["recurring"] else ""))
            when.setStyleSheet(f"color:{'#b36e70' if item['overdue'] else '#8791a2'};font-size:10px;")
            layout.addWidget(dot)
            layout.addWidget(title, 1)
            layout.addWidget(when)
            self.reminder_rows.addWidget(row)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_timers()
        self._check_reminders()
        self._queue_image_resize()
        self._queue_quote_fit()

    def hideEvent(self, event):
        self.image_timer.stop()
        self.quote_timer.stop()
        self.reminder_timer.stop()
        self._resize_timer.stop()
        self._quote_fit_timer.stop()
        self._pending_image_request = None
        super().hideEvent(event)
