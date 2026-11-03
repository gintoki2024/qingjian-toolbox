"""A small, independent calendar and notebook window.

The window deliberately owns no scheduler or tray icon.  The notebook runtime
owns that lifecycle; this module only edits the local store and presents it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

from PySide6.QtCore import QDate, QDateTime, QEvent, QLocale, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCalendarWidget, QCheckBox, QComboBox,
    QDateEdit, QDateTimeEdit, QDialog, QFrame, QGridLayout, QHBoxLayout, QTimeEdit,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QStyledItemDelegate, QStyle, QToolButton, QVBoxLayout, QWidget, QWidgetAction,
)


PRIORITIES = {
    "low": ("低", "#8794a8"),
    "normal": ("普通", "#628ce1"),
    "important": ("重要", "#d69a3d"),
    "urgent": ("紧急", "#d86672"),
}
REPEATS = (
    ("单次预约", "once"), ("每天", "daily"), ("周一至周五", "weekdays"),
    ("每周", "weekly"), ("每月", "monthly"), ("每年", "yearly"),
    ("自定义间隔", "interval"),
)
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


STYLE = """
QWidget#notebookWindow, QWidget#notebookTasksWindow, QDialog#notebookEditor { color: #35374d; font-family: 'Microsoft YaHei UI', 'Microsoft YaHei'; }
QFrame#notebookPanel QWidget { background: transparent; color: #44445a; font-size: 12px; }
QFrame#notebookPanel QLabel, QDialog#notebookEditor QLabel { border: none; background: transparent; }
QFrame#notebookPanel QLabel#notebookTitle { color: #37334c; font-size: 13px; font-weight: 600; }
QFrame#notebookPanel QLabel#notebookBrand { background: #8174c2; color: white; font-size: 17px; border-radius: 9px; }
QFrame#notebookPanel QLabel#dateHeadline { color: #383247; font-size: 23px; font-weight: 600; }
QFrame#notebookPanel QLabel#dateLunar { color: #898299; font-size: 11px; }
QFrame#notebookPanel QLabel#sectionHeading { color: #50485e; font-size: 12px; font-weight: 600; }
QFrame#notebookPanel QLabel#notebookFootnote { color: #9990a6; font-size: 10px; }
QFrame#notebookPanel QLabel#notebookError { color: #ae4759; font-size: 11px; }
QFrame#notebookPanel QPushButton, QDialog#notebookEditor QPushButton {
    background: rgba(255,255,255,115); border: 1px solid rgba(139,124,164,35);
    border-radius: 7px; padding: 6px 10px; color: #746885; font-size: 11px;
}
QFrame#notebookPanel QPushButton:hover, QDialog#notebookEditor QPushButton:hover {
    background: #f0eafa; border-color: #cfc0e3; color: #7960a5;
}
QFrame#notebookPanel QToolButton {
    border: none; border-radius: 6px; padding: 3px; color: #8a8197; font-size: 15px;
}
QFrame#notebookPanel QToolButton:hover { background: rgba(135,107,183,26); color: #78599e; }
QFrame#notebookPanel QToolButton:checked { background: rgba(135,107,183,30); color: #7653a1; }
QFrame#notebookPanel QToolButton#closeNotebook:hover { background: #f5dfe5; color: #b86377; }
QFrame#notebookPanel QPushButton#newNotebookTask, QDialog#notebookEditor QPushButton#saveNotebookTask {
    background: #8472bb; border: 1px solid #8472bb; color: white; font-weight: 600;
}
QFrame#notebookPanel QPushButton#newNotebookTask:hover, QDialog#notebookEditor QPushButton#saveNotebookTask:hover { background: #7560ac; }
QFrame#notebookPanel QPushButton#viewTab { background: transparent; border: none; padding: 6px 9px; color: #9a8da5; }
QFrame#notebookPanel QPushButton#viewTab:checked { background: rgba(132,114,187,23); color: #7862a6; font-weight: 600; }
QFrame#notebookPanel QPushButton#layoutMode { background: rgba(134,117,185,18); border: none; padding: 4px 8px; color: #87739d; font-size: 10px; }
QFrame#notebookPanel QLineEdit, QDialog#notebookEditor QLineEdit,
QDialog#notebookEditor QPlainTextEdit, QDialog#notebookEditor QDateEdit,
QDialog#notebookEditor QDateTimeEdit, QDialog#notebookEditor QTimeEdit, QDialog#notebookEditor QComboBox,
QDialog#notebookEditor QSpinBox {
    background: rgba(255,255,255,195); border: 1px solid #e5dfea; border-radius: 7px;
    padding: 7px 8px; color: #494255; font-size: 12px; selection-background-color: #ddd1f3;
}
QFrame#notebookPanel QLineEdit:focus, QDialog#notebookEditor QLineEdit:focus,
QDialog#notebookEditor QPlainTextEdit:focus { border-color: #b39acb; }
QFrame#notebookPanel QLineEdit#notebookSearch { padding: 6px 9px; font-size: 11px; }
QFrame#notebookPanel QComboBox, QFrame#notebookPanel QSpinBox {
    border: none; background: transparent; padding: 3px; color: #6d607c; font-size: 12px;
}
QComboBox::drop-down { border: none; width: 19px; }
QFrame#notebookPanel QCheckBox, QDialog#notebookEditor QCheckBox { spacing: 5px; color: #8e829c; font-size: 11px; padding: 2px 0; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #cfc3dc; border-radius: 4px; background: rgba(255,255,255,150); }
QCheckBox::indicator:checked { background: #9b83bf; border-color: #9b83bf; }
QFrame#notebookPanel QListWidget#notebookTasks { border: none; background: transparent; outline: none; padding: 0; }
QFrame#notebookPanel QListWidget#notebookTasks::item { background: transparent; border: none; padding: 0; margin: 0 0 7px; }
QFrame#notebookPanel QListWidget#notebookTasks::item:selected { background: transparent; }
QFrame#notebookPanel QFrame#notebookTaskCard { background: rgba(255,255,255,136); border: 1px solid rgba(184,169,202,42); border-radius: 9px; }
QFrame#notebookPanel QLabel#taskTitle { color: #55495f; font-size: 12px; font-weight: 600; }
QFrame#notebookPanel QLabel#taskDetail { color: #a396ac; font-size: 10px; }
QFrame#notebookPanel QToolButton#taskAction { color: #9c8bab; font-size: 13px; }
QFrame#notebookPanel QCalendarWidget { background: transparent; border: none; }
QFrame#notebookPanel QCalendarWidget QAbstractItemView { border: none; background: transparent; selection-background-color: transparent; outline: none; }
QScrollBar:vertical { background: transparent; width: 4px; margin: 0; }
QScrollBar::handle:vertical { background: #d9cfe1; border-radius: 2px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QDialog#notebookEditor { background: #f8f5fb; }
QDialog#notebookEditor QScrollArea, QDialog#notebookEditor QWidget#editorBody { background: transparent; border: none; }
QDialog#notebookEditor QLabel#editorHeading { color: #51415f; font-size: 20px; font-weight: 600; }
QDialog#notebookEditor QLabel#editorHint { color: #9b8ba7; font-size: 11px; }
QDialog#notebookEditor QLabel#editorError { color: #b7546a; font-size: 11px; }
"""


def _now(clock: Callable | None = None) -> datetime:
    value = clock() if clock is not None else datetime.now()
    return value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())


def _date(value) -> date:
    if isinstance(value, QDate):
        return value.toPython()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _calendar_info(day: date) -> dict:
    try:
        from .notebook_calendar import calendar_info
        return calendar_info(day)
    except (ImportError, ValueError, OverflowError):
        return {"lunar": "", "label": "", "festival": "", "term": ""}


def _available_work_areas() -> list[QRect]:
    """Qt already reports these in logical pixels, including DPI and taskbars."""
    from PySide6.QtGui import QGuiApplication
    primary = QGuiApplication.primaryScreen()
    screens = QGuiApplication.screens()
    if primary in screens:
        screens = [primary] + [screen for screen in screens if screen != primary]
    return [QRect(screen.availableGeometry()) for screen in screens]


def _fit_notebook_geometry(requested: QRect, available: list[QRect], margin: int = 20, minimum: tuple[int, int] = (380, 580)) -> QRect:
    """Keep the complete small window on its best matching available monitor.

    The usual 20-pixel inset shrinks on particularly small work areas, while
    the existing 380 x 580 minimum stays intact.  A detached monitor's saved
    position moves to the nearest remaining monitor instead of disappearing.
    """
    screens = [QRect(rect) for rect in available if rect.isValid()]
    if not screens:
        return QRect(requested)

    def overlap(rect):
        shared = rect.intersected(requested)
        return max(0, shared.width()) * max(0, shared.height())

    target = max(screens, key=overlap)
    if overlap(target) == 0:
        center = requested.center()

        def distance(rect):
            dx = max(rect.left() - center.x(), 0, center.x() - rect.right())
            dy = max(rect.top() - center.y(), 0, center.y() - rect.bottom())
            return dx * dx + dy * dy

        target = min(screens, key=distance)
    minimum_width, minimum_height = minimum
    horizontal_margin = min(margin, max(0, (target.width() - minimum_width) // 2))
    vertical_margin = min(margin, max(0, (target.height() - minimum_height) // 2))
    usable = target.adjusted(horizontal_margin, vertical_margin, -horizontal_margin, -vertical_margin)
    width = max(minimum_width, min(1000, requested.width(), usable.width()))
    height = max(minimum_height, min(1400, requested.height(), usable.height()))
    x = min(max(requested.x(), usable.left()), max(usable.left(), usable.right() - width + 1))
    y = min(max(requested.y(), usable.top()), max(usable.top(), usable.bottom() - height + 1))
    return QRect(x, y, width, height)


def _occurs_on(task: dict, day: date) -> bool:
    from .reminder_schedule import task_occurs_on
    return task_occurs_on(task, day)


def _repeat_label(task: dict) -> str:
    rule = task.get("repeat") or {"kind": "once"}
    kind = rule.get("kind", "once")
    if kind == "weekly" and rule.get("weekdays"):
        return "每" + "、".join(WEEKDAYS[i] for i in rule["weekdays"] if isinstance(i, int) and 0 <= i <= 6)
    if kind == "interval":
        unit = {"minutes": "分钟", "hours": "小时", "days": "天"}.get(rule.get("unit"), "天")
        return f"每 {rule.get('interval', 1)} {unit}"
    return dict((value, label) for label, value in REPEATS).get(kind, "单次预约")


class _GlassPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.opacity = .9

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.7, .7, -.7, -.7)
        alpha = round(self.opacity * 255)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor(253, 251, 255, alpha))
        gradient.setColorAt(.45, QColor(248, 246, 253, alpha))
        gradient.setColorAt(1, QColor(242, 237, 250, alpha))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(255, 255, 255, 205), 1.2))
        painter.drawRoundedRect(rect, 18, 18)


class _TitleBar(QFrame):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._drag_position = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Manual title-bar movement keeps release delivery reliable. Native
            # startSystemMove can consume release inside its own Windows loop.
            try:
                from .glass import suspend_glass
                suspend_glass(self.window)
            except (ImportError, RuntimeError):
                pass
            self.grabMouse()
            self._drag_position = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_position is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_position = None
        self.releaseMouse()
        try:
            from .glass import resume_glass
            resume_glass(self.window)
        except (ImportError, RuntimeError):
            pass

    def event(self, event):
        if event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate, QEvent.Type.Hide) and getattr(self, "_drag_position", None) is not None:
            self._drag_position = None
            try:
                from .glass import resume_glass
                resume_glass(self.window)
            except (ImportError, RuntimeError):
                pass
        return super().event(event)


class _TaskList(QListWidget):
    controller = None

    def keyPressEvent(self, event):
        if self.controller is not None and self.currentItem() is not None:
            key = self.currentItem().data(Qt.ItemDataRole.UserRole)
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.controller.edit_task(key)
                return
            if event.key() == Qt.Key.Key_Space:
                task = self.controller._tasks_by_id.get(key, {})
                self.controller._complete_task(key, not task.get("completed", False))
                return
            if event.key() == Qt.Key.Key_Delete:
                self.controller._remove_task(key)
                return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "empty_label"):
            self.empty_label.setGeometry(self.viewport().rect())


class NotebookCalendar(QCalendarWidget):
    """Compact month grid with lunar captions and up to three priority dots."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tasks = []
        self.today = date.today()
        self._info_cache = {}
        self._marks_cache = {}
        self.setLocale(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
        self.setMinimumDate(QDate(1900, 1, 1))
        self.setMaximumDate(QDate(2100, 12, 31))
        self.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        self.setNavigationBarVisible(False)
        self.setGridVisible(False)
        self.setFixedHeight(232)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._page_key = None
        self.page_builds = 0
        self.currentPageChanged.connect(self.prepare_page)
        self.prepare_page(self.yearShown(), self.monthShown())

    def set_tasks(self, tasks):
        if tasks == self.tasks:
            return False
        self.tasks = tasks
        self._page_key = None
        self.prepare_page(self.yearShown(), self.monthShown())
        self.updateCells()
        return True

    def prepare_page(self, year, month):
        key = (year, month)
        if self._page_key == key:
            return
        self._page_key = key
        self.page_builds += 1
        first = date(year, month, 1)
        start = first - timedelta(days=first.weekday() + 7)
        days = [start + timedelta(days=offset) for offset in range(56)]
        self._info_cache = {day: _calendar_info(day) for day in days}
        from .calendar_index import calendar_marks
        self._marks_cache = calendar_marks(self.tasks, days)

    def _cell_content_layout(self, painter, rect, number, label, bold=False):
        """Place actual glyph ink and priority dots in separate logical rows.

        Point sizes and vertically centred rectangles can overlap at Windows
        scaling levels. Explicit pixel sizes plus measured baseline placement
        keep the two text lines intact even in the compact six-week grid.
        """
        number_font = QFont(self.font())
        number_font.setBold(bold)
        label_font = QFont(self.font())
        label_font.setBold(False)
        number_size = min(13, max(8, int(rect.height() * .47)))
        label_size = min(9, max(6, int(rect.height() * .30)))
        radius = 1.1 if rect.height() >= 25 else .9
        dot_y = rect.bottom() - 2.5
        top = rect.top() + 1.0
        text_bottom = dot_y - radius - 1.0
        gap = 1.0
        while True:
            number_font.setPixelSize(number_size)
            label_font.setPixelSize(label_size)
            number_metrics = QFontMetricsF(number_font, painter.device())
            label_metrics = QFontMetricsF(label_font, painter.device())
            fitted_label = label_metrics.elidedText(label, Qt.TextElideMode.ElideRight, max(1, rect.width() - 6))
            number_bounds = number_metrics.tightBoundingRect(number)
            label_bounds = label_metrics.tightBoundingRect(fitted_label or "初一")
            total = number_bounds.height() + gap + label_bounds.height()
            if total <= text_bottom - top or (number_size <= 6 and label_size <= 5):
                break
            if number_size > max(6, label_size + 2):
                number_size -= 1
            elif label_size > 5:
                label_size -= 1
            else:
                number_size -= 1
        number_top = top + max(0.0, (text_bottom - top - total) / 2)
        label_top = number_top + number_bounds.height() + gap
        number_baseline = QPointF(rect.left() + (rect.width() - number_metrics.horizontalAdvance(number)) / 2,
                                 number_top - number_bounds.top())
        label_baseline = QPointF(rect.left() + (rect.width() - label_metrics.horizontalAdvance(fitted_label)) / 2,
                                label_top - label_bounds.top())
        return {"number_font": number_font, "label_font": label_font,
                "number_baseline": number_baseline, "label_baseline": label_baseline,
                "number_ink": number_bounds.translated(number_baseline),
                "label_ink": label_bounds.translated(label_baseline),
                "label": fitted_label, "dot_y": dot_y, "dot_radius": radius,
                "dot_row": QRectF(rect.left(), dot_y - radius, rect.width(), 2 * radius)}

    def paintCell(self, painter, rect, qday):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        day = qday.toPython()
        selected = qday == self.selectedDate()
        in_month = qday.month() == self.monthShown()
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#9380c4"))
            painter.drawRoundedRect(QRectF(rect).adjusted(3, 1, -3, -1), 8, 8)
        elif day == self.today:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(144, 120, 184, 23))
            painter.drawRoundedRect(QRectF(rect).adjusted(3, 1, -3, -1), 8, 8)
        info = self._info_cache.get(day, {})
        label = str(info.get("festival") or info.get("term") or info.get("label") or "")
        if len(label) > 4:
            label = label[:4]
        content = self._cell_content_layout(painter, rect, str(day.day), label, selected or day == self.today)
        painter.setFont(content["number_font"])
        painter.setPen(QColor("#ffffff" if selected else "#c7bfce" if not in_month else "#967f9f" if day.weekday() >= 5 else "#564b63"))
        painter.drawText(content["number_baseline"], str(day.day))
        painter.setFont(content["label_font"])
        painter.setPen(QColor("#f3eafc" if selected else "#d4cadc" if not in_month else "#b07d93" if info.get("festival") or info.get("term") else "#a99aaf"))
        painter.drawText(content["label_baseline"], content["label"])
        priorities = self._marks_cache.get(day, ())
        for index, priority in enumerate(priorities):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#f4e9ff" if selected else PRIORITIES.get(priority, PRIORITIES["normal"])[1]))
            painter.drawEllipse(QPointF(rect.center().x() + (index - (len(priorities) - 1) / 2) * 5, content["dot_y"]),
                                content["dot_radius"], content["dot_radius"])
        painter.restore()


class TaskEditorDialog(QDialog):
    """An editor that only closes after a successful atomic store write."""
    def __init__(self, store, task=None, selected_date=None, parent=None, clock=None):
        super().__init__(parent)
        self.store, self.task, self.clock = store, task or {}, clock
        self.saved_task = None
        self.setObjectName("notebookEditor")
        self.setWindowTitle("编辑事项" if task else "添加事项")
        self.setStyleSheet(STYLE)
        self.resize(440, 635)
        self.setMinimumSize(380, 500)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(9)
        heading = QLabel("编辑这件事" if task else "记下一件事")
        heading.setObjectName("editorHeading")
        root.addWidget(heading)
        hint = QLabel("到点提醒、循环安排，让每件事都有着落。")
        hint.setObjectName("editorHint")
        root.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("editorBody")
        form = QVBoxLayout(body)
        form.setContentsMargins(0, 7, 4, 5)
        form.setSpacing(8)
        form.addWidget(QLabel("事项名称"))
        self.title_edit = QLineEdit(str(self.task.get("title", "")))
        self.title_edit.setPlaceholderText("例如：准备周五会议资料")
        self.title_edit.setMaxLength(200)
        self.title_edit.setAccessibleName("事项名称")
        form.addWidget(self.title_edit)
        row = QHBoxLayout()
        date_box = QVBoxLayout()
        date_box.addWidget(QLabel("事项日期"))
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDateRange(QDate(1900, 1, 1), QDate(2100, 12, 31))
        self.date_edit.setDate(QDate(_date(self.task.get("date", selected_date or _now(clock).date()))))
        self.date_edit.setAccessibleName("事项日期")
        date_box.addWidget(self.date_edit)
        row.addLayout(date_box, 1)
        priority_box = QVBoxLayout()
        priority_box.addWidget(QLabel("重要程度"))
        self.priority_combo = QComboBox()
        for value, (label, color) in PRIORITIES.items():
            self.priority_combo.addItem(f"●  {label}", value)
            self.priority_combo.setItemData(self.priority_combo.count() - 1, QColor(color), Qt.ItemDataRole.ForegroundRole)
        self.priority_combo.setCurrentIndex(max(0, self.priority_combo.findData(self.task.get("priority", "normal"))))
        self.priority_combo.setAccessibleName("重要程度")
        priority_box.addWidget(self.priority_combo)
        row.addLayout(priority_box, 1)
        form.addLayout(row)
        form.addWidget(QLabel("详细内容（选填）"))
        self.body_edit = QPlainTextEdit(str(self.task.get("body", "")))
        self.body_edit.setPlaceholderText("资料清单、会议地点，或此刻的一点想法……")
        self.body_edit.setFixedHeight(82)
        self.body_edit.setAccessibleName("详细内容")
        form.addWidget(self.body_edit)
        self.reminder_check = QCheckBox("开启事项提醒（可指定小时、分钟）")
        self.reminder_check.setChecked(bool(self.task.get("remind_at")))
        self.reminder_check.setAccessibleName("开启事项提醒")
        form.addWidget(self.reminder_check)
        self.reminder_panel = QWidget()
        timing = QVBoxLayout(self.reminder_panel)
        timing.setContentsMargins(0, 2, 0, 0)
        timing.setSpacing(8)
        appointment_row = QHBoxLayout()
        appointment_date = QVBoxLayout()
        appointment_date.addWidget(QLabel("预约日期"))
        # Keep the complete datetime in this control so old records (including
        # seconds) and existing scheduling code retain their original meaning.
        # The calendar popup only picks a date; a separate, visible time input
        # makes selecting hours/minutes discoverable without editing a section
        # of the old combined date-time field.
        self.remind_edit = QDateTimeEdit()
        self.remind_edit.setCalendarPopup(True)
        self.remind_edit.setDisplayFormat("yyyy-MM-dd")
        self.remind_edit.setDateTimeRange(QDateTime(QDate(1900, 1, 1), _now(clock).time().replace(hour=0, minute=0, second=0, microsecond=0)), QDateTime(QDate(2100, 12, 31), _now(clock).time().replace(hour=23, minute=59, second=59, microsecond=0)))
        moment = _datetime(self.task.get("remind_at"))
        if moment is None:
            moment = datetime.combine(self.date_edit.date().toPython(), _now(clock).time().replace(second=0, microsecond=0)) + timedelta(hours=1)
            if moment.date() != self.date_edit.date().toPython():
                moment = datetime.combine(self.date_edit.date().toPython(), datetime.min.time()).replace(hour=23, minute=59)
        self.remind_edit.setDateTime(QDateTime(moment))
        self._initial_reminder = self.remind_edit.dateTime()
        self._initial_repeat = dict(self.task.get("repeat") or {})
        self.remind_edit.setAccessibleName("预约提醒日期")
        appointment_date.addWidget(self.remind_edit)
        appointment_row.addLayout(appointment_date, 1)
        appointment_time = QVBoxLayout()
        appointment_time.addWidget(QLabel("提醒时刻（24小时制）"))
        self.remind_time_edit = QTimeEdit()
        self.remind_time_edit.setDisplayFormat("HH:mm")
        self.remind_time_edit.setTime(self.remind_edit.time())
        self.remind_time_edit.setMinimumWidth(112)
        self.remind_time_edit.setAccessibleName("提醒小时和分钟")
        self.remind_time_edit.setToolTip("直接输入小时、分钟，或选中相应数字后用上下箭头调整。")
        self.remind_time_edit.timeChanged.connect(self.remind_edit.setTime)
        self.remind_edit.timeChanged.connect(self.remind_time_edit.setTime)
        appointment_time.addWidget(self.remind_time_edit)
        appointment_row.addLayout(appointment_time, 1)
        timing.addLayout(appointment_row)
        schedule_row = QHBoxLayout()
        repetition = QVBoxLayout()
        repetition.addWidget(QLabel("重复安排"))
        self.repeat_combo = QComboBox()
        for label, value in REPEATS:
            self.repeat_combo.addItem(label, value)
        self.repeat_combo.setCurrentIndex(max(0, self.repeat_combo.findData((self.task.get("repeat") or {}).get("kind", "once"))))
        self.repeat_combo.setAccessibleName("重复安排")
        repetition.addWidget(self.repeat_combo)
        schedule_row.addLayout(repetition, 1)
        lead = QVBoxLayout()
        lead.addWidget(QLabel("提前提醒"))
        self.lead_spin = QSpinBox()
        self.lead_spin.setRange(0, 10080)
        self.lead_spin.setSuffix(" 分钟")
        self.lead_spin.setSpecialValueText("准时提醒")
        self.lead_spin.setValue(int(self.task.get("lead_minutes", 0)))
        self.lead_spin.setAccessibleName("提前提醒分钟数")
        lead.addWidget(self.lead_spin)
        schedule_row.addLayout(lead, 1)
        timing.addLayout(schedule_row)
        self.weekly_panel = QWidget()
        week_row = QHBoxLayout(self.weekly_panel)
        week_row.setContentsMargins(0, 0, 0, 0)
        week_row.setSpacing(5)
        weekdays = (self.task.get("repeat") or {}).get("weekdays", [self.date_edit.date().dayOfWeek() - 1])
        self.weekday_checks = []
        for index in range(7):
            checkbox = QCheckBox("一二三四五六日"[index])
            checkbox.setToolTip(f"每{WEEKDAYS[index]}")
            checkbox.setChecked(index in weekdays)
            self.weekday_checks.append(checkbox)
            week_row.addWidget(checkbox)
        timing.addWidget(self.weekly_panel)
        self.interval_panel = QWidget()
        interval_row = QHBoxLayout(self.interval_panel)
        interval_row.setContentsMargins(0, 0, 0, 0)
        interval_row.addWidget(QLabel("每隔"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1000000)
        self.interval_spin.setValue(int((self.task.get("repeat") or {}).get("interval", 1)))
        interval_row.addWidget(self.interval_spin, 1)
        self.interval_unit = QComboBox()
        for label, unit in (("分钟", "minutes"), ("小时", "hours"), ("天", "days")):
            self.interval_unit.addItem(label, unit)
        self.interval_unit.setCurrentIndex(max(0, self.interval_unit.findData((self.task.get("repeat") or {}).get("unit", "days"))))
        interval_row.addWidget(self.interval_unit, 1)
        timing.addWidget(self.interval_panel)
        schedule_hint = QLabel("按首次预约时间循环；每月遇月底、每年遇闰日时，落在当月最后一天。")
        schedule_hint.setWordWrap(True)
        schedule_hint.setObjectName("editorHint")
        timing.addWidget(schedule_hint)
        form.addWidget(self.reminder_panel)
        form.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self.error_label = QLabel()
        self.error_label.setObjectName("editorError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        root.addWidget(self.error_label)
        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("保存事项")
        self.save_button.setObjectName("saveNotebookTask")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        root.addLayout(buttons)
        self.reminder_check.toggled.connect(self._update_schedule)
        self.repeat_combo.currentIndexChanged.connect(self._update_schedule)
        self.date_edit.dateChanged.connect(self._date_changed)
        self._update_schedule()

    def _date_changed(self, qday):
        if not self.task or not (self.remind_edit.hasFocus() or self.remind_time_edit.hasFocus()):
            self.remind_edit.setDate(qday)

    def _update_schedule(self, *_args):
        self.reminder_panel.setVisible(self.reminder_check.isChecked())
        self.weekly_panel.setVisible(self.repeat_combo.currentData() == "weekly")
        self.interval_panel.setVisible(self.repeat_combo.currentData() == "interval")

    def payload(self) -> dict:
        title = self.title_edit.text().strip()
        if not title:
            raise ValueError("请先填写事项名称。")
        rule = {"kind": self.repeat_combo.currentData() if self.reminder_check.isChecked() else "once"}
        if rule["kind"] == "weekly":
            rule["weekdays"] = [index for index, checkbox in enumerate(self.weekday_checks) if checkbox.isChecked()]
            if not rule["weekdays"]:
                raise ValueError("请至少选择每周提醒的一个星期。")
        elif rule["kind"] == "interval":
            rule.update(interval=self.interval_spin.value(), unit=self.interval_unit.currentData())
        moment = self.remind_edit.dateTime().toPython()
        if self.remind_edit.dateTime() != self._initial_reminder:
            moment = moment.replace(second=0, microsecond=0)
        # A body/title edit preserves an existing custom recurrence interval.
        # The controls only expose the interval input for the interval kind.
        if rule["kind"] == self._initial_repeat.get("kind") and rule["kind"] != "interval":
            rule["interval"] = self._initial_repeat.get("interval", 1)
        return {
            "title": title, "body": self.body_edit.toPlainText(),
            "date": self.date_edit.date().toString("yyyy-MM-dd"),
            "priority": self.priority_combo.currentData(),
            "remind_at": moment.isoformat(timespec="seconds") if self.reminder_check.isChecked() else None,
            "repeat": rule,
            "lead_minutes": self.lead_spin.value() if self.reminder_check.isChecked() else 0,
            "completed": bool(self.task.get("completed", False)),
        }

    def save(self):
        try:
            payload = self.payload()
            self.saved_task = self.store.upsert_task(payload, task_id=self.task.get("id"), now=_now(self.clock))
        except (ValueError, OSError) as error:
            self.error_label.setText(str(error) or "保存失败，请检查本地数据目录。")
            self.error_label.show()
            return
        self.accept()


class _TaskDelegate(QStyledItemDelegate):
    """Only visible rows are painted; no per-task widget tree is allocated."""
    def __init__(self, controller):
        super().__init__(controller.task_list)
        self.controller = controller

    @staticmethod
    def action_rect(rect, action):
        card = rect.adjusted(0, 1, -2, -6)
        if action == "complete":
            return QRect(card.left() + 8, card.center().y() - 12, 24, 24)
        if action == "edit":
            return QRect(card.right() - 28, card.top() + 4, 24, 24)
        if action == "delete":
            return QRect(card.right() - 28, card.bottom() - 28, 24, 24)
        return QRect()

    def sizeHint(self, option, index):
        return QSize(0, 82)

    def paint(self, painter, option, index):
        task = self.controller._tasks_by_id.get(index.data(Qt.ItemDataRole.UserRole))
        if task is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(0, 1, -2, -6)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setBrush(QColor(245, 239, 252, 200) if selected else QColor(255, 255, 255, 190 if hover else 150))
        painter.setPen(QPen(QColor("#cbb9df") if selected else QColor(185, 169, 203, 55), 1))
        painter.drawRoundedRect(QRectF(rect), 10, 10)
        color = QColor(PRIORITIES.get(task.get("priority"), PRIORITIES["normal"])[1])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(rect.left() + 1, rect.top() + 12, 3, rect.height() - 24), 1.5, 1.5)
        completion = self.action_rect(option.rect, "complete")
        painter.setPen(QPen(QColor("#ad9dbb"), 1.2))
        painter.setBrush(QColor("#a18abb") if task.get("completed") else QColor(255, 255, 255, 145))
        painter.drawEllipse(QRectF(completion).adjusted(5, 5, -5, -5))
        font = QFont(option.font)
        font.setPixelSize(11)
        painter.setFont(font)
        if task.get("completed"):
            painter.setPen(QColor("white"))
            painter.drawText(completion, Qt.AlignmentFlag.AlignCenter, "✓")
        text_x = completion.right() + 7
        text_width = max(10, self.action_rect(option.rect, "edit").left() - text_x - 6)
        font.setPixelSize(12)
        font.setBold(True)
        font.setStrikeOut(bool(task.get("completed")))
        painter.setFont(font)
        painter.setPen(QColor("#aa9db5" if task.get("completed") else "#52435f"))
        title = painter.fontMetrics().elidedText(str(task.get("title", "")), Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(QRect(text_x, rect.top() + 8, text_width, 18), Qt.AlignmentFlag.AlignVCenter, title)
        font.setBold(False)
        font.setStrikeOut(False)
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor("#94839f"))
        detail = self.controller._task_summary(task)
        painter.drawText(QRect(text_x, rect.top() + 28, text_width, 15), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(detail, Qt.TextElideMode.ElideRight, text_width))
        body = str(task.get("body", "")).splitlines()
        if body:
            painter.setPen(QColor("#a195ac"))
            painter.drawText(QRect(text_x, rect.top() + 45, text_width, 14), Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(body[0], Qt.TextElideMode.ElideRight, text_width))
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor("#9e8caf"))
        painter.drawText(self.action_rect(option.rect, "edit"), Qt.AlignmentFlag.AlignCenter, "改")
        painter.drawText(self.action_rect(option.rect, "delete"), Qt.AlignmentFlag.AlignCenter, "×")
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() != QEvent.Type.MouseButtonRelease or event.button() != Qt.MouseButton.LeftButton:
            return False
        key = index.data(Qt.ItemDataRole.UserRole)
        point = event.position().toPoint()
        for action in ("complete", "edit", "delete"):
            if self.action_rect(option.rect, action).contains(point):
                if action == "complete":
                    task = self.controller._tasks_by_id.get(key, {})
                    self.controller._complete_task(key, not task.get("completed", False))
                elif action == "edit":
                    self.controller.edit_task(key)
                else:
                    self.controller._remove_task(key)
                return True
        return False


class _TasksWindow(QWidget):
    def __init__(self, controller):
        super().__init__(None)
        self.controller = controller
        self.allow_close = False
        self.setObjectName("notebookTasksWindow")
        self.setWindowTitle("青简 · 当天事项")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self.setMinimumSize(320, 280)
        root = QVBoxLayout(self)
        root.setContentsMargins(5, 5, 5, 5)
        self.panel = _GlassPanel(self)
        self.panel.setObjectName("notebookPanel")
        root.addWidget(self.panel)
        self.content_layout = QVBoxLayout(self.panel)
        self.content_layout.setContentsMargins(12, 10, 12, 10)
        self.content_layout.setSpacing(6)
        self.content_layout.addWidget(controller._make_title_bar(self, "青简 · 事项", companion=True))
        self.date_caption = QLabel()
        self.date_caption.setStyleSheet("color: #a295ad; font-size: 10px;")
        self.content_layout.addWidget(self.date_caption)

    def closeEvent(self, event):
        if self.allow_close:
            event.accept()
            return
        self.controller.save_geometry()
        event.ignore()
        self.controller.close_requested.emit()


class NotebookWindow(QWidget):
    close_requested = Signal()
    hide_requested = Signal()
    task_changed = Signal()
    settings_changed = Signal(dict)
    layout_changed = Signal()

    def __init__(self, store, parent=None, clock=None):
        super().__init__(None)
        self.store, self.clock = store, clock
        self.allow_close = False
        self._group_closed = False
        self._building = True
        self._view = "day"
        self._mode = str(store.settings.get("layout_mode", "split"))
        self._tasks_snapshot = []
        self._tasks_by_id = {}
        self._snapshot_revision = object()
        self._snapshot_generation = 0
        self._filter_key = None
        self._list_signature = None
        self._last_hero_day = None
        self.render_stats = {"list_rebuilds": 0, "calendar_updates": 0, "snapshot_reads": 0, "list_items_changed": 0}
        self.setObjectName("notebookWindow")
        self.setWindowTitle("青简 · 日历记事")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(5, 5, 5, 5)
        self.panel = _GlassPanel(self)
        self.panel.setObjectName("notebookPanel")
        root.addWidget(self.panel)
        self.content_layout = QVBoxLayout(self.panel)
        self.content_layout.setContentsMargins(12, 10, 12, 10)
        self.content_layout.setSpacing(8)
        self.content_layout.addWidget(self._make_title_bar(self, "青简 · 日历"))
        self.calendar_body = self._build_calendar_body()
        self.content_layout.addWidget(self.calendar_body)
        self.tasks_body = self._build_tasks_body()
        self.tasks_window = _TasksWindow(self)
        self.companion_windows = [self.tasks_window]
        self._building = False
        self._apply_layout(self._mode, first=True)
        self.apply_window_settings()
        self.select_date(_now(clock).date())
        self.refresh()

    @property
    def layout_mode(self):
        return self._mode

    def owned_windows(self):
        return [self, self.tasks_window] if self._mode == "split" else [self]

    def _make_title_bar(self, window, text, companion=False):
        bar = _TitleBar(window)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        dot = QLabel("●")
        dot.setStyleSheet("color: #a28bc5; font-size: 9px;")
        dot.setFixedWidth(10)
        dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(dot)
        label = QLabel(text)
        label.setObjectName("notebookTitle")
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(label)
        row.addStretch()
        mode = QPushButton("合并" if self._mode == "split" else "拆分")
        mode.setObjectName("layoutMode")
        mode.setFixedHeight(24)
        mode.clicked.connect(self.toggle_layout)
        row.addWidget(mode)
        if companion:
            self.tasks_mode_button = mode
        else:
            self.mode_button = mode
            self.pin_button = self._tool_button("↑", "置顶所有记事浮窗")
            self.pin_button.setCheckable(True)
            self.pin_button.toggled.connect(self._toggle_on_top)
            row.addWidget(self.pin_button)
        settings = self._tool_button("···", "外观与启动设置")
        settings.clicked.connect(lambda: self._open_settings(settings))
        row.addWidget(settings)
        if not companion:
            self.settings_button = settings
        hide = self._tool_button("—", "收起记事本，继续提醒")
        hide.clicked.connect(self._request_hide)
        row.addWidget(hide)
        close = self._tool_button("×", "关闭浮窗，保留后台提醒")
        close.setObjectName("closeNotebook")
        close.clicked.connect(window.close)
        row.addWidget(close)
        return bar

    def _build_calendar_body(self):
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(5)
        row = QHBoxLayout()
        labels = QVBoxLayout()
        labels.setSpacing(1)
        self.date_headline = QLabel()
        self.date_headline.setObjectName("dateHeadline")
        self.date_lunar = QLabel()
        self.date_lunar.setObjectName("dateLunar")
        labels.addWidget(self.date_headline)
        labels.addWidget(self.date_lunar)
        row.addLayout(labels, 1)
        today = QPushButton("今天")
        today.setFixedWidth(45)
        today.clicked.connect(lambda: self.select_date(_now(self.clock).date()))
        row.addWidget(today)
        box.addLayout(row)
        self.calendar = NotebookCalendar()
        self.calendar.today = _now(self.clock).date()
        month_row = QHBoxLayout()
        month_row.setSpacing(2)
        previous = self._tool_button("‹", "上个月")
        previous.clicked.connect(self.calendar.showPreviousMonth)
        month_row.addWidget(previous)
        self.year_spin = QSpinBox()
        self.year_spin.setRange(1900, 2100)
        self.year_spin.setSuffix(" 年")
        self.year_spin.setFixedWidth(86)
        self.year_spin.setAccessibleName("日历年份")
        month_row.addWidget(self.year_spin)
        self.month_combo = QComboBox()
        for month in range(1, 13):
            self.month_combo.addItem(f"{month} 月", month)
        self.month_combo.setFixedWidth(60)
        self.month_combo.setAccessibleName("日历月份")
        month_row.addWidget(self.month_combo)
        following = self._tool_button("›", "下个月")
        following.clicked.connect(self.calendar.showNextMonth)
        month_row.addWidget(following)
        month_row.addStretch()
        label = QLabel("万年历")
        label.setStyleSheet("color: #b0a2ba; font-size: 9px;")
        month_row.addWidget(label)
        box.addLayout(month_row)
        box.addWidget(self.calendar)
        box.addStretch(1)
        self.calendar.selectionChanged.connect(self._selection_changed)
        self.calendar.currentPageChanged.connect(self._page_changed)
        self.year_spin.valueChanged.connect(self._jump_page)
        self.month_combo.currentIndexChanged.connect(self._jump_page)
        self._page_changed(_now(self.clock).year, _now(self.clock).month)
        return body

    def _build_tasks_body(self):
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        row = QHBoxLayout()
        self.list_heading = QLabel("当天事项")
        self.list_heading.setObjectName("sectionHeading")
        row.addWidget(self.list_heading)
        row.addStretch()
        new = QPushButton("＋  记一件")
        new.setObjectName("newNotebookTask")
        new.clicked.connect(lambda: self.edit_task())
        row.addWidget(new)
        box.addLayout(row)
        tabs = QHBoxLayout()
        tabs.setSpacing(0)
        self.view_buttons = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for value, label in (("day", "当天"), ("upcoming", "即将提醒"), ("all", "全部")):
            button = QPushButton(label)
            button.setObjectName("viewTab")
            button.setCheckable(True)
            button.setChecked(value == "day")
            button.clicked.connect(lambda checked=False, mode=value: self._set_view(mode))
            group.addButton(button)
            self.view_buttons[value] = button
            tabs.addWidget(button)
        tabs.addStretch()
        self.completed_check = QCheckBox("已完成")
        self.completed_check.setToolTip("显示已完成的事项")
        self.completed_check.toggled.connect(self._refresh_list)
        tabs.addWidget(self.completed_check)
        box.addLayout(tabs)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("notebookSearch")
        self.search_edit.setPlaceholderText("搜索事项或笔记")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("搜索事项")
        self.search_edit.textChanged.connect(self._refresh_list)
        box.addWidget(self.search_edit)
        self.task_list = _TaskList()
        self.task_list.controller = self
        self.task_list.setObjectName("notebookTasks")
        self.task_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.task_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.task_list.setMinimumHeight(75)
        self.task_list.setMouseTracking(True)
        self.task_list.setUniformItemSizes(True)
        self.task_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_list.customContextMenuRequested.connect(self._task_menu)
        self.task_list.itemDoubleClicked.connect(lambda item: self.edit_task(item.data(Qt.ItemDataRole.UserRole)))
        self.task_list.setToolTip("Enter 编辑 · 空格 完成或恢复 · Delete 删除；右键查看更多操作")
        self.task_list.setItemDelegate(_TaskDelegate(self))
        box.addWidget(self.task_list, 1)
        self.empty_label = QLabel("这一天还很清爽\n记下一件事，或留一点想法。", self.task_list.viewport())
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color: #aa9ab6; font-size: 11px;")
        self.empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.task_list.empty_label = self.empty_label
        self.error_label = QLabel()
        self.error_label.setObjectName("notebookError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        box.addWidget(self.error_label)
        footer = QLabel("小窗或托盘运行期间提醒")
        footer.setObjectName("notebookFootnote")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(footer)
        return body

    @staticmethod
    def _tool_button(text, tooltip, parent=None):
        button = QToolButton(parent)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(21, 24)
        button.setCursor(Qt.CursorShape.ArrowCursor)
        return button

    def _geometry_values(self):
        def values(window):
            r = window.geometry()
            return [r.x(), r.y(), r.width(), r.height()]
        if self._mode == "split":
            return {"calendar_geometry": values(self), "tasks_geometry": values(self.tasks_window)}
        return {"geometry": values(self)}

    def _restore_mode_geometry(self):
        available = _available_work_areas()
        origin = available[0].topLeft() if available else QPoint()
        settings = self.store.settings
        if self._mode == "split":
            self.setMinimumSize(300, 340)
            requested = settings.get("calendar_geometry") or [origin.x() + 20, origin.y() + 20, 340, 370]
            self.setGeometry(_fit_notebook_geometry(QRect(*requested), available, minimum=(300, 340)))
            requested = settings.get("tasks_geometry") or [self.x() + self.width() + 14, self.y(), 360, 350]
            self.tasks_window.setGeometry(_fit_notebook_geometry(QRect(*requested), available, minimum=(320, 280)))
        else:
            self.setMinimumSize(340, 580)
            requested = settings.get("geometry") or [origin.x() + 20, origin.y() + 20, 360, 680]
            self.setGeometry(_fit_notebook_geometry(QRect(*requested), available, minimum=(340, 580)))
        self._adjust_calendar_size()

    def _apply_layout(self, mode, first=False):
        visible = self.isVisible()
        self._mode = mode if mode in ("split", "combined") else "split"
        self.content_layout.removeWidget(self.tasks_body)
        self.tasks_window.content_layout.removeWidget(self.tasks_body)
        self.tasks_body.hide()
        if self._mode == "split":
            self.tasks_window.content_layout.addWidget(self.tasks_body, 1)
        else:
            self.content_layout.addWidget(self.tasks_body, 1)
            self.tasks_window.hide()
        self.tasks_body.show()
        text = "合并" if self._mode == "split" else "拆分"
        for button in (self.mode_button, self.tasks_mode_button):
            button.setText(text)
            button.setToolTip("把日历与事项合并为一个窗口" if self._mode == "split" else "把日历与事项拆成两个浮窗")
        self._restore_mode_geometry()
        if visible and self._mode == "split":
            self.tasks_window.showNormal()
        if not first:
            self.layout_changed.emit()

    def set_layout_mode(self, mode):
        if mode not in ("split", "combined"):
            raise ValueError("请选择合并或拆分视图。")
        if mode == self._mode:
            return True
        values = self._geometry_values()
        values["layout_mode"] = mode
        if not self._save_settings(values):
            return False
        self._apply_layout(mode)
        return True

    def toggle_layout(self):
        self.set_layout_mode("combined" if self._mode == "split" else "split")

    def show_windows(self):
        if self._group_closed:
            return
        self.showNormal()
        if self._mode == "split":
            self.tasks_window.showNormal()
        for window in self.owned_windows():
            window.raise_()

    def hide_windows(self):
        self.save_geometry()
        self.tasks_window.hide()
        self.hide()

    def minimize_windows(self):
        self.save_geometry()
        for window in self.owned_windows():
            window.showMinimized()

    def close_windows(self):
        if self._group_closed:
            return
        self.save_geometry()
        self._group_closed = True
        self.tasks_window.allow_close = True
        self.tasks_window.close()
        self.tasks_window.deleteLater()
        self.allow_close = True
        self.close()

    def set_glass_opacity(self, value):
        alpha = max(.55, min(1.0, float(value)))
        for panel in (self.panel, self.tasks_window.panel):
            if abs(panel.opacity - alpha) > .001:
                panel.opacity = alpha
                panel.update()

    def apply_window_settings(self):
        settings = self.store.settings
        self.set_glass_opacity(settings.get("opacity", .9))
        top = bool(settings.get("always_on_top", False))
        self.pin_button.blockSignals(True)
        self.pin_button.setChecked(top)
        self.pin_button.blockSignals(False)
        for window in [self, self.tasks_window]:
            if bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) != top:
                visible = window.isVisible()
                window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, top)
                if visible:
                    window.show()

    def _save_settings(self, values):
        try:
            settings = self.store.update_settings(values)
        except (ValueError, OSError) as error:
            self.error_label.setText(str(error) or "设置未保存，请检查本地数据目录。")
            self.error_label.show()
            self.apply_window_settings()
            return False
        self.error_label.hide()
        self.settings_changed.emit(settings)
        return True

    def _toggle_on_top(self, enabled):
        if self._save_settings({"always_on_top": bool(enabled)}):
            self.apply_window_settings()

    def _open_settings(self, anchor=None):
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background: #fcf9ff; border: 1px solid #ddd1e8; border-radius: 8px; padding: 6px; color: #665471; } QMenu::item { padding: 8px 13px; } QMenu::item:selected { background: #eee5f7; }")
        surface = QWidget()
        box = QVBoxLayout(surface)
        box.setContentsMargins(12, 6, 12, 10)
        label = QLabel(f"玻璃不透明度  {round(self.panel.opacity * 100)}%")
        box.addWidget(label)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(55, 100)
        slider.setValue(round(self.panel.opacity * 100))
        slider.setMinimumWidth(180)
        slider.setAccessibleName("玻璃不透明度")
        slider.valueChanged.connect(lambda value: (label.setText(f"玻璃不透明度  {value}%"), self.set_glass_opacity(value / 100)))
        box.addWidget(slider)
        action = QWidgetAction(menu)
        action.setDefaultWidget(surface)
        menu.addAction(action)
        start = menu.addAction("随工具箱启动记事本")
        start.setCheckable(True)
        start.setChecked(bool(self.store.settings.get("start_with_toolbox", True)))
        start.toggled.connect(lambda value: self._save_settings({"start_with_toolbox": value}))
        top = menu.addAction("浮窗始终置顶")
        top.setCheckable(True)
        top.setChecked(self.pin_button.isChecked())
        top.toggled.connect(self._toggle_on_top)
        mode = menu.addAction("合并两个浮窗" if self._mode == "split" else "拆分为两个浮窗")
        mode.triggered.connect(self.toggle_layout)
        menu.addSeparator()
        note = menu.addAction("小窗或托盘运行期间提醒")
        note.setEnabled(False)
        anchor = anchor or self.settings_button
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))
        if abs(float(self.store.settings.get("opacity", .9)) - self.panel.opacity) > .001:
            self._save_settings({"opacity": self.panel.opacity})

    def _page_changed(self, year, month):
        self.year_spin.blockSignals(True)
        self.month_combo.blockSignals(True)
        self.year_spin.setValue(year)
        self.month_combo.setCurrentIndex(month - 1)
        self.year_spin.blockSignals(False)
        self.month_combo.blockSignals(False)

    def _jump_page(self, *_args):
        self.calendar.setCurrentPage(self.year_spin.value(), int(self.month_combo.currentData() or 1))

    def select_date(self, day):
        selected = _date(day)
        self.calendar.setSelectedDate(QDate(selected))
        self.calendar.setCurrentPage(selected.year, selected.month)
        self._selection_changed()

    def _selection_changed(self):
        if self._building:
            return
        day = self.calendar.selectedDate().toPython()
        if day != self._last_hero_day:
            self._last_hero_day = day
            self.date_headline.setText(f"{day.month}月{day.day}日  {WEEKDAYS[day.weekday()]}")
            info = _calendar_info(day)
            parts = [str(info.get("lunar") or "")]
            for key in ("festival", "term"):
                value = str(info.get(key) or "")
                if value and value not in parts[0]:
                    parts.append(value)
            text = "  ·  ".join(part for part in parts if part)
            self.date_lunar.setText(text or f"{day.year} 年 · 每天留一点从容")
            self.date_lunar.setToolTip(text)
            self.tasks_window.date_caption.setText(f"{day.year}年{day.month}月{day.day}日  ·  {WEEKDAYS[day.weekday()]}")
        self._refresh_list()

    def _set_view(self, mode):
        self._view = mode
        self.view_buttons[mode].setChecked(True)
        self._refresh_list()

    def _visible_tasks(self):
        day = self.calendar.selectedDate().toPython()
        query = self.search_edit.text().strip().casefold()
        tasks = []
        now = _now(self.clock)
        for task in self._tasks_snapshot:
            if task.get("completed") and not self.completed_check.isChecked():
                continue
            if query and query not in (str(task.get("title", "")) + "\n" + str(task.get("body", ""))).casefold():
                continue
            if self._view == "day" and not _occurs_on(task, day):
                continue
            if self._view == "upcoming":
                due = _datetime(task.get("next_due")) or _datetime(task.get("remind_at"))
                if not due or task.get("completed") or due > now + timedelta(days=30):
                    continue
            tasks.append(task)
        rank = {value: index for index, value in enumerate(PRIORITIES)}
        return sorted(tasks, key=lambda task: (bool(task.get("completed")), -rank.get(task.get("priority"), 1),
                      str(task.get("next_due") or task.get("remind_at") or task.get("date", "")), str(task.get("title", ""))))

    def _refresh_list(self, *_args):
        if self._building:
            return
        day = self.calendar.selectedDate().toPython()
        now = _now(self.clock)
        filter_key = (self._snapshot_generation, self._view, day,
                      self.search_edit.text().strip().casefold(), self.completed_check.isChecked(),
                      now.replace(second=0, microsecond=0) if self._view == "upcoming" else None)
        if filter_key == self._filter_key:
            return
        self._filter_key = filter_key
        tasks = self._visible_tasks()
        day_heading = "当天事项" if day == now.date() else f"{day.month}月{day.day}日事项"
        heading = {"day": day_heading, "upcoming": "即将提醒", "all": "全部事项"}[self._view]
        self.list_heading.setText(f"{heading}  ·  {len(tasks)}")
        signature = tuple((task["id"], task.get("title"), task.get("body"), task.get("priority"), task.get("date"), task.get("next_due"),
                           task.get("remind_at"), str(task.get("repeat")), task.get("completed")) for task in tasks)
        if signature == self._list_signature:
            return
        self._list_signature = signature
        previous = self.task_list.currentItem()
        selected = previous.data(Qt.ItemDataRole.UserRole) if previous else None
        current_ids = [self.task_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.task_list.count())]
        new_ids = [task["id"] for task in tasks]
        self.task_list.setUpdatesEnabled(False)
        try:
            if current_ids != new_ids:
                self.task_list.clear()
                self.render_stats["list_rebuilds"] += 1
                for task in tasks:
                    item = QListWidgetItem(str(task.get("title", "")))
                    item.setData(Qt.ItemDataRole.UserRole, task["id"])
                    item.setSizeHint(QSize(0, 82))
                    self.task_list.addItem(item)
                    if task["id"] == selected:
                        self.task_list.setCurrentItem(item)
                self.render_stats["list_items_changed"] += len(tasks)
            else:
                for index, task in enumerate(tasks):
                    item = self.task_list.item(index)
                    if item.text() != str(task.get("title", "")):
                        item.setText(str(task.get("title", "")))
                        self.render_stats["list_items_changed"] += 1
            import html
            for index, task in enumerate(tasks):
                tooltip = html.escape(str(task.get("title", "")) + "\n" + str(task.get("body", ""))[:1200])
                self.task_list.item(index).setToolTip("<qt>" + tooltip.replace("\n", "<br>") + "<br><br>Enter 编辑 · 空格 完成/恢复 · Delete 删除</qt>")
        finally:
            self.task_list.setUpdatesEnabled(True)
        self.task_list.viewport().update()
        self.empty_label.setText("这一天还很清爽\n记下一件事，或留一点想法。" if self._view == "day" and not self.search_edit.text() else "没有匹配的事项\n试试其他日期或筛选条件。")
        self.empty_label.setVisible(not tasks)
        self.empty_label.setGeometry(self.task_list.viewport().rect())

    @staticmethod
    def _task_summary(task):
        details = [PRIORITIES.get(task.get("priority"), PRIORITIES["normal"])[0]]
        due = _datetime(task.get("next_due")) or _datetime(task.get("remind_at"))
        if task.get("completed"):
            details.append("已完成")
        elif due:
            details.append(due.strftime("%m-%d %H:%M"))
            if (task.get("repeat") or {}).get("kind", "once") != "once":
                details.append(_repeat_label(task))
        else:
            details.extend((str(task.get("date", "")), "仅记事"))
        return "  ·  ".join(details)

    def task_action_rect(self, item_or_index, action):
        item = self.task_list.item(item_or_index) if isinstance(item_or_index, int) else item_or_index
        if item is None:
            return QRect()
        return _TaskDelegate.action_rect(self.task_list.visualItemRect(item), action)

    def _task_menu(self, point):
        item = self.task_list.itemAt(point)
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        task = self._tasks_by_id.get(key, {})
        menu = QMenu(self.tasks_body)
        menu.addAction("编辑事项  Enter", lambda: self.edit_task(key))
        menu.addAction("恢复待办  Space" if task.get("completed") else "完成事项  Space", lambda: self._complete_task(key, not task.get("completed", False)))
        menu.addSeparator()
        menu.addAction("删除事项  Delete", lambda: self._remove_task(key))
        menu.exec(self.task_list.viewport().mapToGlobal(point))

    def refresh(self):
        if self._building:
            return
        revision = getattr(self.store, "revision", None)
        if revision is None or revision != self._snapshot_revision:
            tasks = self.store.tasks
            self.render_stats["snapshot_reads"] += 1
            self._snapshot_revision = revision
            if tasks != self._tasks_snapshot:
                self._snapshot_generation += 1
                self._tasks_snapshot = tasks
                self._tasks_by_id = {task["id"]: task for task in tasks}
                if self.calendar.set_tasks(tasks):
                    self.render_stats["calendar_updates"] += 1
        self.calendar.today = _now(self.clock).date()
        self._selection_changed()
        if getattr(self.store, "last_error", None):
            self.error_label.setText(str(self.store.last_error))
            self.error_label.show()

    def edit_task(self, task_id=None):
        task = self.store.find_task(task_id) if task_id else None
        if task_id and not task:
            self.error_label.setText("这件事项已不存在，请刷新后再试。")
            self.error_label.show()
            return
        parent = self.tasks_window if self._mode == "split" else self
        dialog = TaskEditorDialog(self.store, task=task, selected_date=self.calendar.selectedDate().toPython(), parent=parent, clock=self.clock)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.error_label.hide()
            self.refresh()
            self.task_changed.emit()

    def focus_task(self, task_id):
        self.refresh()
        self.search_edit.clear()
        self.completed_check.setChecked(True)
        self._set_view("all")
        for index in range(self.task_list.count()):
            item = self.task_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == task_id:
                self.task_list.setCurrentItem(item)
                self.task_list.scrollToItem(item)
                self.task_list.setFocus()
                if self.isVisible() and self._mode == "split":
                    self.tasks_window.showNormal()
                    self.tasks_window.raise_()
                return True
        return False

    def _complete_task(self, task_id, completed=True):
        try:
            self.store.complete_task(task_id, completed=completed, now=_now(self.clock))
        except (ValueError, OSError) as error:
            self.error_label.setText(str(error))
            self.error_label.show()
            return
        self.error_label.hide()
        self.refresh()
        self.task_changed.emit()

    def _remove_task(self, task_id):
        task = self.store.find_task(task_id)
        if not task:
            return
        parent = self.tasks_window if self._mode == "split" else self
        answer = QMessageBox.question(parent, "删除事项", f"确定删除“{str(task.get('title', ''))[:60]}”？\n后续提醒也会一起取消。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.remove_task(task_id)
        except (ValueError, OSError) as error:
            self.error_label.setText(str(error))
            self.error_label.show()
            return
        self.error_label.hide()
        self.refresh()
        self.task_changed.emit()

    def save_geometry(self):
        if self._building or self._group_closed:
            return
        values = self._geometry_values()
        settings = self.store.settings
        changed = {key: value for key, value in values.items() if settings.get(key) != value}
        if changed:
            self._save_settings(changed)

    def _request_hide(self):
        self.save_geometry()
        self.hide_requested.emit()

    def _adjust_calendar_size(self):
        if not hasattr(self, "calendar") or self._building:
            return
        if self._mode == "split":
            height = max(184, min(220, self.height() - 156))
            self.calendar.setFixedHeight(height)
            self.date_lunar.show()
            self.content_layout.setSpacing(6)
        else:
            compact = self.height() < 650
            self.calendar.setFixedHeight(174 if compact else 204)
            self.date_lunar.setVisible(not compact)
            self.content_layout.setSpacing(4 if compact else 7)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adjust_calendar_size()
        if hasattr(self, "empty_label"):
            self.empty_label.setGeometry(self.task_list.viewport().rect())

    def showEvent(self, event):
        super().showEvent(event)
        if not self._building and not self._group_closed and self._mode == "split" and not self.isMinimized():
            self.tasks_window.showNormal()

    def closeEvent(self, event):
        if self.allow_close:
            if not self._group_closed and hasattr(self, "tasks_window"):
                self._group_closed = True
                self.tasks_window.allow_close = True
                self.tasks_window.close()
                self.tasks_window.deleteLater()
            event.accept()
            return
        self.save_geometry()
        event.ignore()
        self.close_requested.emit()
