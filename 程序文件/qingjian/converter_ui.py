"""A detachable, copy-only conversion drop ring backed by the toolbox worker.

No file is opened while hovering. The format catalogue supplies compatible
targets; the main window owns validation, conversion and cancellation.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QKeySequence, QPainter, QPainterPath, QPainterPathStroker, QPen, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu, QProgressBar,
    QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget, QDialog,
    QDialogButtonBox, QListWidget, QListWidgetItem,
)

from .quick_actions import ACTIONS, MAX_INPUTS, parse_mime_payload
from .quick_ui import clipboard_sources
from .drag_wheel import DragWheelBridge
from .icons import bind_window_icon, titlebar_badge_pixmap
from .converter_preferences import ConversionPreferences


# Warm ivory surface, beige segments and a terracotta accent, in the spirit of
# Claude's interface: low contrast borders, generous radii, calm typography.
_IVORY, _SAND, _SAND_DARK, _INK, _MUTED = "#faf9f5", "#eeeae0", "#e4e0d4", "#3d3929", "#8a877d"
_CLAY, _CLAY_DARK, _CLAY_TINT = "#d97757", "#c2613f", "#f6e3d9"

CONVERTER_STYLE = """
QFrame#converterSurface {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fbfaf7, stop:1 #f6f4ec);
    border: 1px solid #e3dfd2; border-radius: 22px;
}
QFrame#converterHeader { border: none; background: transparent; }
QLabel#converterTitle { font-size: 13px; font-weight: 600; color: #383425; }
QLabel#converterCaption { font-size: 11px; color: #868277; background: transparent; }
QLabel#converterSource { font-size: 11px; color: #585448; background: transparent; }
QFrame#converterLine { background: #eae5da; border: none; max-height: 1px; }
QPushButton#converterControl {
    background: transparent; border: none; border-radius: 10px;
    min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px; padding: 1px; color: #868277;
}
QPushButton#converterControl:hover { background: #ede9df; color: #383425; }
QPushButton#converterControl:checked { color: #c2613f; background: #f5e6de; }
QPushButton#converterAction {
    background: #ffffff; border: 1px solid #e3dfd2; border-radius: 12px;
    padding: 6px 11px; font-size: 11px; color: #585448;
}
QPushButton#converterAction:hover { background: #fdf5f0; border-color: #e2bca9; color: #bd5b39; }
QPushButton#converterAction:pressed { background: #f9ebe3; }
QPushButton#converterAction:disabled { color: #b9b5aa; background: #f4f2ec; border-color: #ebe7dd; }
QPushButton#converterPrimary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #dc7a5a, stop:1 #cf6c4c);
    border: 1px solid #d47050; border-radius: 12px;
    padding: 6px 12px; font-size: 11px; font-weight: 600; color: #ffffff;
}
QPushButton#converterPrimary:hover { background: #c2613f; border-color: #c2613f; }
QPushButton#converterPrimary:pressed { background: #b05333; border-color: #b05333; }
QPushButton#converterPrimary:disabled { background: #e9c3b3; border-color: #e9c3b3; color: #ffffff; }
QPushButton#converterTarget {
    background: transparent; border: none; border-radius: 12px;
    padding: 0; font-size: 11px; font-weight: 600; color: #383425;
}
QPushButton#converterTarget:hover, QPushButton#converterTarget:focus { color: #c2613f; background: transparent; }
QPushButton#converterTarget[active="true"] { color: #bd5b39; }
QPushButton#converterTarget:disabled { color: #b9b5aa; }
QPushButton#converterCenter {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #fcfaf6);
    border: 1px solid #e2ded1; border-radius: 34px;
    font-size: 11px; font-weight: 600; color: #c2613f; padding: 0;
}
QPushButton#converterCenter:hover { background: #fdf3ee; border-color: #e3bda9; }
QPushButton#converterCenter:disabled { color: #b9b5aa; }
QPushButton#converterQuiet {
    background: transparent; border: none; border-radius: 9px; padding: 3px 7px;
    font-size: 11px; color: #c2613f;
}
QPushButton#converterQuiet:hover { background: #f1ede3; }
QPushButton#converterQuiet:disabled { color: #b9b5aa; }
QFrame#converterStatus { background: #f1eee6; border: 1px solid #e7e3d8; border-radius: 14px; }
QLabel#converterStatusText { background: transparent; color: #585448; font-size: 11px; }
QProgressBar#converterProgress { background: #e4e0d3; border: none; border-radius: 2px; height: 3px; }
QProgressBar#converterProgress::chunk { background: #d97757; border-radius: 2px; }
QToolTip { background: #383425; color: #faf9f5; border: none; border-radius: 8px; padding: 6px 8px; }
"""


def available_targets(sources, *, preferred=None):
    """Lazy catalogue access keeps startup independent of optional engines."""
    try:
        from .converter_hub import available_targets as catalogue
    except ImportError:
        # The original six channels remain useful in source-only maintenance
        # checkouts where the expanded conversion module is not installed yet.
        targets = []
        for key, spec in ACTIONS.items():
            if sources and all(
                spec["accept_urls"] if str(source).lower().startswith(("http://", "https://"))
                else Path(str(source)).suffix.lower() in spec["extensions"]
                for source in sources
            ):
                targets.append(dict(id=key, title=spec["title"], glyph=spec.get("glyph", "转"),
                                    description=spec["description"]))
        return targets
    return catalogue(sources, preferred=preferred)


def target_title(action_id):
    if action_id in ACTIONS:
        return ACTIONS[action_id]["title"]
    try:
        from .converter_hub import target_title as catalogue_title
        return catalogue_title(action_id)
    except (ImportError, KeyError, ValueError):
        return str(action_id).split(":")[-1].upper()


def _sources(event):
    mime = event.mimeData()
    return parse_mime_payload([url.toString() for url in mime.urls()], mime.text() if mime.hasText() else "")


def _label(text, name, wrap=False):
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setMinimumWidth(0)
    label.setWordWrap(wrap)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


def _button(text, name, callback, tip=""):
    button = QPushButton(text)
    button.setObjectName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(callback)
    if tip:
        button.setToolTip(tip)
        button.setAccessibleName(tip)
    return button


class _DragHandle(QFrame):
    """Only the header's empty area moves the window; buttons remain usable."""

    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self._origin = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.globalPosition().toPoint() - self.owner.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.owner.move(event.globalPosition().toPoint() - self._origin)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._origin is not None:
            self._origin = None
            self.owner.keep_on_screen()
            self.owner.save_position()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class _Bubble(QPushButton):
    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self._press = None
        self._offset = None
        self._moved = False
        self.setFixedSize(70, 70)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("青简格式转换悬浮按钮，拖入文件展开目标格式")
        self.setToolTip("拖入文件或链接 · 点击展开 · 拖动移动\nCtrl+V 粘贴 · 右键更多")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for spread, alpha in ((3.0, 10), (2.0, 16), (1.0, 22)):
            painter.setBrush(QColor(61, 57, 41, alpha))
            painter.drawEllipse(QRectF(3 - spread, 4 - spread, 64 + 2 * spread, 64 + 2 * spread))
        painter.setPen(QPen(QColor("#e6e2d6"), 1))
        painter.setBrush(QColor(_IVORY))
        painter.drawEllipse(QRectF(3, 3, 64, 64))
        pen = QPen(QColor(_CLAY), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(23, 14, 25, 25), 35 * 16, 135 * 16)
        painter.drawArc(QRectF(23, 14, 25, 25), 215 * 16, 135 * 16)
        painter.drawLine(QPointF(23, 23), QPointF(23, 16))
        painter.drawLine(QPointF(23, 23), QPointF(30, 22))
        painter.drawLine(QPointF(48, 30), QPointF(48, 37))
        painter.drawLine(QPointF(48, 30), QPointF(41, 31))
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor(_INK))
        painter.drawText(QRectF(5, 44, 60, 17), Qt.AlignmentFlag.AlignCenter,
                         "转换中" if self.owner.running else "格式转换")
        if self.owner.running:
            track = QPen(QColor(_SAND_DARK), 2.4)
            painter.setPen(track)
            painter.drawEllipse(QRectF(5, 5, 60, 60))
            progress = QPen(QColor(_CLAY), 2.4)
            progress.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(progress)
            painter.drawArc(QRectF(5, 5, 60, 60), 90 * 16,
                            -int(360 * 16 * max(0.06, self.owner.progress_ratio)))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._offset = self._press - self.owner.pos()
            self._moved = False
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if (event.globalPosition().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
                self._moved = True
            if self._moved:
                self.owner.move(event.globalPosition().toPoint() - self._offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._press is not None:
            self._press = None
            self.owner.keep_on_screen()
            self.owner.save_position()
            if not self._moved:
                self.owner.set_collapsed(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.owner.set_collapsed(False)
            event.accept()
        else:
            super().keyPressEvent(event)


class FormatRing(QWidget):
    """Six drop sectors per page, filled in stable priority order."""
    PAGE_SIZE = 6

    preview_requested = Signal(list)
    sources_dropped = Signal(list, str)
    target_clicked = Signal(str)
    choose_requested = Signal()
    page_changed = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(244, 244)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setAccessibleName("可用格式圆环，拖到目标格式松开立即转换")
        self.targets = []
        self.page = 0
        self.buttons = []
        self._hover = -1
        self._mouse_hover = -1
        self._drag_active = False
        self._drag_position = QPointF(122, 122)
        self._wheel_remainder = 0
        self._pixel_remainder = 0
        self.drag_wheel = DragWheelBridge(self)
        self.drag_wheel.ready.connect(self._flush_drag_wheel, Qt.ConnectionType.QueuedConnection)
        self.center = _button("＋\n拖入文件", "converterCenter", self.choose_requested.emit)
        self.center.setParent(self)
        self.center.setGeometry(88, 88, 68, 68)
        self.center.setAccessibleName("选择待转换文件")

    @property
    def visible_targets(self):
        pages = self.target_pages
        return pages[min(self.page, len(pages) - 1)]

    @property
    def target_pages(self):
        common = [t for t in self.targets if t.get('group', 'common') == 'common']
        more = [t for t in self.targets if t.get('group', 'common') != 'common']
        ordered = common + more
        return [ordered[i:i + self.PAGE_SIZE] for i in range(0, len(ordered), self.PAGE_SIZE)] or [[]]

    def set_targets(self, targets):
        clean = [dict(item) for item in targets if isinstance(item, dict) and item.get("id")]
        if clean == self.targets:
            return
        self.targets = clean
        self.page = 0
        self._wheel_remainder = self._pixel_remainder = 0
        self._rebuild()

    def turn_page(self, direction):
        pages = len(self.target_pages)
        if pages <= 1 or not self.isEnabled():
            return
        self.page = (self.page + direction) % pages
        self._rebuild()

    def _rebuild(self):
        self.setUpdatesEnabled(False)
        try:
            for button in self.buttons:
                button.hide()
                button.deleteLater()
            self.buttons = []
            count = len(self.visible_targets)
            for index, target in enumerate(self.visible_targets):
                # Catalogue glyphs are short format labels (PDF, PNG, EPUB), not
                # decorative icons. Fall back to the full name for a generic glyph.
                glyph = str(target.get("glyph") or "")
                text = glyph if glyph and glyph not in {"图", "文", "转", "↗"} else str(target.get("title") or target["id"])
                text = text.replace("转换为", "").replace("转为", "").strip()
                if len(text) > 8:
                    text = text[:7] + "\n" + text[7:15] + ("…" if len(text) > 15 else "")
                button = _button(text, "converterTarget",
                                 lambda checked=False, key=target["id"]: self.target_clicked.emit(key),
                                 str(target.get("title", "")) + "\n" + str(target.get("description", "")))
                button.setParent(self)
                angle = math.radians(-90 + index * 360 / max(1, count))
                x, y = 122 + 85 * math.cos(angle), 122 + 85 * math.sin(angle)
                button.setGeometry(round(x - 32), round(y - 16), 64, 32)
                button.setEnabled(self.isEnabled())
                button.installEventFilter(self)
                button.show()
                self.buttons.append(button)
            self._hover = self.target_at(self._drag_position) if self._drag_active else -1
            self._mouse_hover = -1
            self._sync_active_labels()
            self.center.raise_()
        finally:
            self.setUpdatesEnabled(True)
        self.page_changed.emit(self.page + 1, len(self.target_pages))
        self.update()

    def target_at(self, position):
        dx, dy = position.x() - 122, position.y() - 122
        radius = math.hypot(dx, dy)
        count = len(self.visible_targets)
        if not count or radius < 49 or radius > 118:
            return -1
        angle = (math.degrees(math.atan2(dy, dx)) + 90 + 180 / count) % 360
        return min(count - 1, int(angle / (360 / count)))

    @staticmethod
    def _segment(index, count, outer=113.0, inner=55.0, gap=17.0):
        """One annular sector with an even pixel gap along its whole length."""
        span = 360 / count
        middle = 90 - index * span
        outer_pad = math.degrees(gap / 2 / outer) if count > 1 else 0
        inner_pad = math.degrees(gap / 2 / inner) if count > 1 else 0
        outer_rect = QRectF(122 - outer, 122 - outer, outer * 2, outer * 2)
        inner_rect = QRectF(122 - inner, 122 - inner, inner * 2, inner * 2)
        path = QPainterPath()
        start = middle + span / 2 - outer_pad
        path.arcMoveTo(outer_rect, start)
        path.arcTo(outer_rect, start, -(span - 2 * outer_pad))
        end = math.radians(middle - span / 2 + inner_pad)
        path.lineTo(122 + inner * math.cos(end), 122 - inner * math.sin(end))
        path.arcTo(inner_rect, middle - span / 2 + inner_pad, span - 2 * inner_pad)
        path.closeSubpath()
        return path

    def _active_index(self):
        return self._hover if self._drag_active else self._mouse_hover

    def eventFilter(self, watched, event):
        if watched in self.buttons:
            if event.type() == QEvent.Type.Enter:
                self._set_mouse_hover(self.buttons.index(watched))
            elif event.type() == QEvent.Type.Leave:
                self._set_mouse_hover(self.target_at(QPointF(self.mapFromGlobal(QCursor.pos()))))
        return super().eventFilter(watched, event)

    def _set_mouse_hover(self, index):
        if index != self._mouse_hover:
            self._mouse_hover = index
            self._sync_active_labels()
            self.update()

    def _sync_active_labels(self):
        active = self._active_index()
        for index, button in enumerate(self.buttons):
            value = index == active
            if button.property("active") != value:
                button.setProperty("active", value)
                button.style().unpolish(button)
                button.style().polish(button)

    def mouseMoveEvent(self, event):
        self._set_mouse_hover(self.target_at(event.position()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        index = self.target_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and 0 <= index < len(self.buttons) and self.isEnabled():
            # The whole soft sector is clickable, not only its label.
            self.buttons[index].click()
            event.accept()
            return
        super().mousePressEvent(event)

    def leaveEvent(self, event):
        self._set_mouse_hover(-1)
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count = len(self.visible_targets)
        painter.setPen(Qt.PenStyle.NoPen)
        if not count:
            ring = QPainterPath()
            ring.addEllipse(QRectF(9, 9, 226, 226))
            ring.addEllipse(QRectF(67, 67, 110, 110))
            painter.setBrush(QColor(_SAND))
            painter.drawPath(ring)
            dash = QPen(QColor("#d6d1c3"), 1.2, Qt.PenStyle.CustomDashLine)
            dash.setDashPattern([3, 4])
            dash.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(dash)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(36, 36, 172, 172))
            return
        active = self._active_index()
        # Rounded joins soften every corner; the pen sits inside the hit area.
        for index in range(count):
            path = self._segment(index, count)
            selected = index == active
            fill = QColor(_CLAY_TINT if selected else _SAND)
            if not self.isEnabled():
                fill = QColor("#f2f0ea")
            pen = QPen(fill, 8)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(fill)
            painter.drawPath(path)
            if selected and self.isEnabled():
                stroker = QPainterPathStroker()
                stroker.setWidth(8)
                stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(QPen(QColor(_CLAY), 1.3))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(stroker.createStroke(path).united(path).simplified())
        # Gentle lift under the centre button.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(61, 57, 41, 12))
        painter.drawEllipse(QRectF(86, 88, 72, 72))

    def _preview(self, event):
        sources = _sources(event)
        if not self.isEnabled() or not sources:
            self.end_drag()
            event.ignore()
            return []
        self.preview_requested.emit(sources)
        self._drag_active = True
        self._drag_position = QPointF(event.position())
        self._flush_drag_wheel()
        self._hover = self.target_at(self._drag_position)
        if len(self.target_pages) > 1:
            self.drag_wheel.start()
        self._sync_active_labels()
        self.update()
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        return sources

    def dragEnterEvent(self, event):
        self._preview(event)

    def dragMoveEvent(self, event):
        self._preview(event)

    def dragLeaveEvent(self, event):
        self.end_drag()
        event.accept()

    def dropEvent(self, event):
        sources = self._preview(event)
        if not sources:
            return
        index = self.target_at(event.position())
        key = str(self.visible_targets[index]["id"]) if index >= 0 else ""
        self.end_drag()
        self.sources_dropped.emit(sources, key)

    def end_drag(self):
        self._drag_active = False
        self._hover = -1
        self._wheel_remainder = self._pixel_remainder = 0
        self.drag_wheel.stop()
        self._sync_active_labels()
        self.update()

    def _flush_drag_wheel(self):
        pending = self.drag_wheel.take_pending()
        if self._drag_active and self.isEnabled():
            for delta in pending:
                self._scroll_pages(delta)

    def _scroll_pages(self, delta, *, pixels=False):
        if not delta or not self.isEnabled() or len(self.target_pages) <= 1:
            return False
        name, threshold = ("_pixel_remainder", 40) if pixels else ("_wheel_remainder", 120)
        remainder = getattr(self, name) + delta
        steps = math.trunc(remainder / threshold)
        setattr(self, name, remainder - steps * threshold)
        if steps:
            self.turn_page(-steps)  # wheel up: previous; wheel down: next
        return True

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if self._scroll_pages(delta, pixels=not event.angleDelta().y()):
            event.accept()
        else:
            super().wheelEvent(event)

    def hideEvent(self, event):
        self.end_drag()
        super().hideEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self.end_drag()
        super().changeEvent(event)


class ConverterWindow(QWidget):
    # Slightly narrower than before, so the floating dial sits in proportion
    # beside the one-third-width main window; the margin holds a soft shadow.
    _EXPANDED_SIZE = (326, 536)
    _EXPANDED_MARGINS = (9, 7, 9, 11)
    submitted = Signal(str, list)
    cancel_requested = Signal()
    results_requested = Signal()
    folder_requested = Signal()
    choose_folder_requested = Signal()
    help_requested = Signal()
    advanced_requested = Signal(list)
    options_requested = Signal()
    dock_requested = Signal()
    closed = Signal()

    def __init__(self, settings_path=None):
        # Deliberately no QWidget parent: closing the main UI must not hide this
        # window or break its drag target. The owner keeps the worker alive.
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("青简 · 独立格式转换")
        bind_window_icon(self, "converter")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(CONVERTER_STYLE)
        self.setAcceptDrops(True)
        self.settings_path = Path(settings_path) if settings_path else None
        self.preferences = ConversionPreferences(self.settings_path.with_name('converter-defaults.json') if self.settings_path else None)
        self.sources = []
        self._preview_values = None
        self.running = False
        self._own_job = False
        self.progress_ratio = 0
        self.collapsed = False
        self._last_outputs = []
        self._has_result = False
        self._shutdown = False
        self._position_loaded = False
        self.url_folder = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*self._EXPANDED_MARGINS)
        outer.setSpacing(0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.bubble = _Bubble(self)
        self.stack.addWidget(self.bubble)
        self.surface = QFrame()
        self.surface.setObjectName("converterSurface")
        self.stack.addWidget(self.surface)
        body = QVBoxLayout(self.surface)
        body.setContentsMargins(12, 8, 12, 10)
        body.setSpacing(3)
        self.header = _DragHandle(self)
        self.header.setObjectName("converterHeader")
        self.header.setFixedHeight(28)
        head = QHBoxLayout(self.header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(5)
        badge = QLabel()
        badge.setPixmap(titlebar_badge_pixmap("converter", 16))
        badge.setFixedSize(16, 16)
        badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        head.addWidget(badge)
        heading = _label("格式转换", "converterTitle")
        heading.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        head.addWidget(heading, 1)
        self.help_button = _button("?", "converterControl", self.help_requested.emit, "转换格式与字段说明")
        head.addWidget(self.help_button)
        self.pin_button = _button("◇", "converterControl", self.toggle_pin, "切换窗口置顶")
        self.pin_button.setCheckable(True)
        self.pin_button.setChecked(True)
        head.addWidget(self.pin_button)
        self.collapse_button = _button("−", "converterControl", lambda: self.set_collapsed(True), "折叠为悬浮按钮")
        head.addWidget(self.collapse_button)
        self.dock_button = _button("↙", "converterControl", self.dock_requested.emit, "收回工具箱")
        head.addWidget(self.dock_button)
        self.close_button = _button("×", "converterControl", self.close, "关闭转换小窗")
        head.addWidget(self.close_button)
        body.addWidget(self.header)
        line = QFrame()
        line.setObjectName("converterLine")
        line.setFixedHeight(1)
        body.addWidget(line)
        self.source_label = _label("拖入文件或链接，再选择目标格式", "converterSource", True)
        self.source_label.setFixedHeight(32)
        body.addWidget(self.source_label)
        self.ring = FormatRing()
        self.ring.preview_requested.connect(self.preview_sources)
        self.ring.sources_dropped.connect(self.accept_sources)
        self.ring.target_clicked.connect(self.choose_target)
        self.ring.choose_requested.connect(self.choose_files)
        self.ring.page_changed.connect(self._page_changed)
        body.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignHCenter)
        pagination = QHBoxLayout()
        pagination.setContentsMargins(0, 0, 0, 0)
        self.previous_button = _button("‹", "converterQuiet", lambda: self.ring.turn_page(-1), "上一页格式")
        self.next_button = _button("›", "converterQuiet", lambda: self.ring.turn_page(1), "下一页格式")
        self.page_label = _label("拖到目标区域即可转换", "converterCaption")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pagination.addWidget(self.previous_button)
        pagination.addWidget(self.page_label, 1)
        pagination.addWidget(self.next_button)
        body.addLayout(pagination)
        self._page_changed(1, 1)
        inputs = QHBoxLayout()
        inputs.setSpacing(5)
        self.choose_button = _button("选择文件", "converterPrimary", self.choose_files)
        self.paste_button = _button("粘贴文件 / 链接", "converterAction", self.paste)
        self.clear_button = _button("清空", "converterQuiet", self.clear_sources)
        inputs.addWidget(self.choose_button)
        inputs.addWidget(self.paste_button, 1)
        inputs.addWidget(self.clear_button)
        body.addLayout(inputs)
        tools = QHBoxLayout()
        tools.setSpacing(2)
        self.options_button = _button("参数", "converterQuiet", self.options_requested.emit, "选择目标后调整图片、PDF或表格参数")
        self.advanced_button = _button("更多工具", "converterQuiet", lambda: self.advanced_requested.emit(list(self.sources)),
                                       "内容提取、电子书、数据、通讯录与日历等低频转换")
        self.zip_button = _button("打包 ZIP", "converterQuiet", self.pack_zip, "将选中的本地文件打包为一个 ZIP，保留原件")
        tools.addWidget(self.options_button)
        tools.addWidget(self.advanced_button, 1)
        tools.addWidget(self.zip_button)
        body.addLayout(tools)
        self.state = QFrame()
        self.state.setObjectName("converterStatus")
        status = QVBoxLayout(self.state)
        status.setContentsMargins(11, 6, 11, 6)
        status.setSpacing(5)
        self.status_label = _label("本地文件的结果保存到原文件夹", "converterStatusText", True)
        self.status_label.setMinimumHeight(28)
        self.status_label.setMaximumHeight(46)
        status.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setObjectName("converterProgress")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.hide()
        status.addWidget(self.progress)
        body.addWidget(self.state)
        actions = QHBoxLayout()
        actions.setSpacing(2)
        self.cancel_button = _button("停止", "converterQuiet", self._cancel)
        self.results_button = _button("查看结果", "converterQuiet", self.results_requested.emit)
        self.folder_button = _button("打开结果位置", "converterQuiet", self.folder_requested.emit)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.results_button)
        actions.addWidget(self.folder_button)
        actions.addStretch(1)
        body.addLayout(actions)
        save = QHBoxLayout()
        save.setSpacing(0)
        self.save_label = _label("原件保留 · 同名自动编号", "converterCaption")
        self.save_label.setFixedHeight(18)
        save.addWidget(self.save_label, 1)
        self.save_button = _button("链接保存位置", "converterQuiet", self.choose_folder_requested.emit)
        save.addWidget(self.save_button)
        body.addLayout(save)
        self.shortcut = QShortcut(QKeySequence.StandardKey.Paste, self)
        self.shortcut.activated.connect(self.paste)
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape_shortcut.activated.connect(lambda: self.set_collapsed(True))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.set_busy(False)
        self.stack.setCurrentWidget(self.surface)
        self.setFixedSize(*self._EXPANDED_SIZE)

    def _page_changed(self, page, pages):
        self.previous_button.setVisible(pages > 1)
        self.next_button.setVisible(pages > 1)
        if self.ring.targets:
            text = f"格式 {page}/{pages} · 拖住滚轮切页" if pages > 1 else f"可转 {len(self.ring.targets)} 种 · 拖到目标松开"
        else:
            text = "拖到目标区域即可转换"
        self.page_label.setText(text)

    def show_window(self, collapsed=None):
        if not self._position_loaded:
            self._load_position()
        if collapsed is not None:
            self.set_collapsed(bool(collapsed))
        self.show()
        self.keep_on_screen()
        self.raise_()
        self.activateWindow()

    def set_collapsed(self, collapsed, anchor=None):
        collapsed = bool(collapsed)
        if self.collapsed == collapsed:
            return
        old_center = self.geometry().center()
        self.collapsed = collapsed
        self.stack.setCurrentWidget(self.bubble if collapsed else self.surface)
        self.layout().setContentsMargins(*((3, 3, 3, 3) if collapsed else self._EXPANDED_MARGINS))
        self.setFixedSize(76, 76) if collapsed else self.setFixedSize(*self._EXPANDED_SIZE)
        if anchor is not None and not collapsed:
            # Keep the cursor over the ring's centre when a file enters the
            # folded button, so expansion does not instantly leave the target.
            self.layout().activate()
            self.surface.layout().activate()
            center = self.ring.mapTo(self, QPoint(122, 122))
            self.move(anchor - center)
        else:
            self.move(old_center - self.rect().center())
        self.keep_on_screen()
        self.save_position()

    def paintEvent(self, event):
        if self.collapsed:
            return
        # A layered, low-alpha shadow; cheaper and steadier during drags than
        # a QGraphicsEffect over the whole drop target.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        frame = QRectF(self.surface.geometry())
        for spread, alpha in ((8, 4), (6, 6), (4, 9), (2, 12)):
            painter.setBrush(QColor(61, 57, 41, alpha))
            painter.drawRoundedRect(frame.adjusted(-spread, -spread + 2, spread, spread + 2), 22 + spread, 22 + spread)

    def toggle_pin(self):
        position = self.pos()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.pin_button.isChecked())
        self.show()
        self.move(position)

    def _menu(self, point):
        menu = QMenu(self)
        menu.addAction("展开格式转换" if self.collapsed else "折叠为悬浮按钮",
                       lambda: self.set_collapsed(not self.collapsed))
        paste = menu.addAction("粘贴文件 / 链接", self.paste)
        paste.setEnabled(not self.running)
        menu.addAction("回到工具箱", self.dock_requested.emit)
        menu.addAction("转换格式与字段说明", self.help_requested.emit)
        order = menu.addAction("调整本类文件的常用顺序", self.choose_order)
        order.setEnabled(bool(self.sources) and not self.running)
        menu.addSeparator()
        menu.addAction("关闭转换小窗", self.close)
        menu.exec(self.mapToGlobal(point))

    def keep_on_screen(self):
        screen = QApplication.screenAt(self.geometry().center()) or self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.move(max(available.left(), min(self.x(), available.right() - self.width() + 1)),
                  max(available.top(), min(self.y(), available.bottom() - self.height() + 1)))

    def _load_position(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 20, screen.top() + 30)
        try:
            if self.settings_path and self.settings_path.is_file() and self.settings_path.stat().st_size < 16384:
                values = json.loads(self.settings_path.read_text(encoding="utf-8"))
                x, y = values.get("x"), values.get("y")
                if values.get("collapsed") is True:
                    self.set_collapsed(True)
                if isinstance(x, int) and isinstance(y, int) and -1000000 < x < 1000000 and -1000000 < y < 1000000:
                    self.move(x, y)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self._position_loaded = True
        self.keep_on_screen()

    def save_position(self):
        if not self.settings_path or not self._position_loaded:
            return
        temporary = None
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            handle, path = tempfile.mkstemp(prefix=".converter-ui-", suffix=".tmp", dir=self.settings_path.parent)
            temporary = Path(path)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(dict(x=self.x(), y=self.y(), collapsed=self.collapsed), stream)
            os.replace(temporary, self.settings_path)
        except OSError:
            # A failed position save never prevents a conversion or close.
            pass
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def set_url_folder(self, folder):
        self.url_folder = str(folder)
        self.save_button.setToolTip("链接转换保存到：" + self.url_folder + "\n本地文件仍保存到原文件夹。")

    def preview_sources(self, sources):
        if self.running or list(sources) == self._preview_values:
            return
        self._preview_values = list(sources)
        if len(sources) > MAX_INPUTS:
            self.ring.set_targets([])
            self.notice(f"一次最多处理 {MAX_INPUTS} 项，请分批拖入。")
            return
        targets = available_targets(sources, preferred=self.preferences.order(sources))
        self.ring.set_targets(targets)
        if not targets:
            self.notice("这组文件没有共同的常用转换目标。可按类型分批拖入，或选择更多工具 / 打包 ZIP。")

    def accept_sources(self, sources, target_id=""):
        if self.running:
            return False
        if not sources:
            self.notice("剪贴板中没有文件或网页链接。")
            return False
        if len(sources) > MAX_INPUTS:
            self.notice(f"一次最多处理 {MAX_INPUTS} 项，请分批拖入。")
            return False
        self.sources = list(dict.fromkeys(str(value) for value in sources))
        self._preview_values = None
        self.preview_sources(self.sources)
        names = [Path(value).name if not value.lower().startswith(("http://", "https://")) else value for value in self.sources]
        summary = names[0] if len(names) == 1 else f"已放入 {len(names)} 项 · " + names[0]
        self.source_label.setText(self.source_label.fontMetrics().elidedText(summary, Qt.TextElideMode.ElideMiddle, 285))
        self.source_label.setToolTip("\n".join(self.sources))
        self.ring.center.setText(f"{len(self.sources)} 项\n更换文件")
        self.set_collapsed(False)
        if self.ring.targets:
            self.notice("选择目标格式开始转换；结果保存到各自原文件夹。")
        if target_id:
            return self.choose_target(target_id)
        return bool(self.ring.targets)

    def choose_files(self):
        if self.running:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "选择待转换文件", "", "所有文件 (*)")
        if files:
            self.accept_sources(files)

    def paste(self):
        if not self.running:
            self.set_collapsed(False)
            self.accept_sources(clipboard_sources())

    def clear_sources(self):
        if self.running:
            return
        self.sources = []
        self._preview_values = None
        self.ring.set_targets([])
        self.ring.center.setText("＋\n拖入文件")
        self.source_label.setText("拖入文件或链接，再选择目标格式")
        self.source_label.setToolTip("")
        self.notice("本地文件的结果保存到原文件夹；原件保留。")

    def choose_target(self, action_id):
        if self.running:
            return False
        if not self.sources:
            self.notice("先将文件放入圆环中心，或粘贴文件 / 链接。")
            return False
        # A preview of another drag must not alter the valid targets of the
        # committed selection when a keyboard user clicks a stale button.
        if action_id not in {str(item["id"]) for item in available_targets(self.sources)}:
            self._preview_values = None
            self.preview_sources(self.sources)
            self.notice("此目标不适用于当前全部文件，请重新选择目标。")
            return False
        self.submitted.emit(str(action_id), list(self.sources))
        return True

    def pack_zip(self):
        if self.running:
            return False
        if not self.sources or any(str(s).lower().startswith(('http://', 'https://')) for s in self.sources):
            self.notice("打包 ZIP 需要先放入本地文件，不接受网页链接。")
            return False
        self.submitted.emit('convert:pack_zip', list(self.sources))
        return True

    def choose_order(self):
        if self.running or not self.sources:
            return
        targets = available_targets(self.sources, preferred=self.preferences.order(self.sources))
        if not targets:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("调整常用格式顺序")
        dialog.resize(340, 390)
        layout = QVBoxLayout(dialog)
        caption = QLabel("常用格式优先，各组内按所选顺序排列；每页最多六项，排满后再翻页。只用于这一类文件。")
        caption.setWordWrap(True)
        layout.addWidget(caption)
        items = QListWidget()
        for target in targets:
            item = QListWidgetItem(target['title'])
            item.setData(Qt.ItemDataRole.UserRole, target['id'])
            items.addItem(item)
        items.setCurrentRow(0)
        layout.addWidget(items)
        moves = QHBoxLayout()
        def move(delta):
            row = items.currentRow()
            if 0 <= row + delta < items.count():
                item = items.takeItem(row)
                items.insertItem(row + delta, item)
                items.setCurrentRow(row + delta)
        moves.addWidget(_button("上移", "converterAction", lambda: move(-1)))
        moves.addWidget(_button("下移", "converterAction", lambda: move(1)))
        layout.addLayout(moves)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存顺序")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.preferences.remember_order(self.sources, [items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(items.count())])
                self._preview_values = None
                self.preview_sources(self.sources)
            except OSError as exc:
                self.notice("顺序未能保存：" + str(exc))
        dialog.deleteLater()

    def notice(self, message):
        self.status_label.setText(str(message))
        self.status_label.setToolTip(str(message))
        self.status_label.setAccessibleDescription(str(message))

    def set_busy(self, busy, own_job=False):
        was_other_job = self.running and not self._own_job
        self.running = bool(busy)
        self._own_job = bool(own_job)
        for control in (self.ring, self.choose_button, self.paste_button, self.clear_button,
                        self.save_button, self.previous_button, self.next_button,
                        self.options_button, self.advanced_button, self.zip_button):
            control.setEnabled(not busy)
        self.cancel_button.setVisible(bool(busy and own_job))
        self.cancel_button.setEnabled(bool(busy and own_job))
        self.results_button.setVisible(not busy)
        self.folder_button.setVisible(not busy)
        self.results_button.setEnabled(self._has_result)
        self.folder_button.setEnabled(bool(self._last_outputs))
        if busy and not own_job:
            self.notice("工具箱正在处理其他任务，完成后可继续转换。")
        elif not busy and was_other_job:
            self.notice("工具箱任务已结束，可以继续转换。")
        self.bubble.update()

    def begin(self, action_id, count, *, title=None):
        self._last_outputs = []
        self._has_result = False
        self.progress_ratio = 0
        self.set_busy(True, own_job=True)
        self.progress.show()
        self.progress.setRange(0, 0)
        self.notice((title or target_title(action_id)) + f" · 正在处理 {count} 项…")

    def update_progress(self, done, total, message):
        self.progress.setRange(0, max(1, total) if done else 0)
        if done:
            self.progress.setValue(done)
        self.progress_ratio = done / max(1, total)
        self.notice(message or f"正在处理 {done} / {total}")
        self.bubble.update()

    def finish(self, result):
        self._last_outputs = list(result.get("outputs", []))
        self._has_result = bool(result.get("records") or result.get("errors"))
        records = result.get("records", [])
        failures = sum(row.get("status") == "failed" for row in records)
        if not records and result.get("errors"):
            failures = len(result["errors"])
        text = ("已停止" if result.get("cancelled") else "转换完成") + f" · 生成 {len(self._last_outputs)} 项，失败 {failures} 项"
        if result.get("errors") and not self._last_outputs:
            text += " · " + str(result["errors"][0])
        elif result.get("warnings"):
            text += " · 部分内容需要复核，请查看结果"
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.progress_ratio = 1
        self.set_busy(False)
        self.notice(text)

    def _cancel(self):
        self.cancel_button.setEnabled(False)
        self.notice("正在停止，已经完成的结果会保留…")
        self.cancel_requested.emit()

    def dragEnterEvent(self, event):
        sources = _sources(event)
        if self.running or not sources:
            event.ignore()
            return
        self.preview_sources(sources)
        if self.collapsed:
            self.set_collapsed(False, anchor=QCursor.pos())
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        sources = _sources(event)
        if self.running or not sources:
            event.ignore()
            return
        self.accept_sources(sources)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def closeEvent(self, event):
        if self.running and not self._shutdown:
            self.set_collapsed(False)
            self.notice("任务仍在处理。可折叠小窗，或先停止任务再关闭。")
            event.ignore()
            return
        self.save_position()
        event.accept()
        self.closed.emit()

    def shutdown(self):
        self._shutdown = True
        self.close()
