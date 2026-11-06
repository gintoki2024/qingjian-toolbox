"""Cohesive 'Qingjian' (青简) vector icon suite and Windows taskbar integration.

Provides multi-resolution QIcons (16px to 256px) for the main toolbox, calendar
notebook, companion tasks float, reminder cards, quick format converter, and
interactive dialogs, plus native Win32 taskbar icon/AppUserModelID binding so
Windows never falls back to default python/executable icons.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import tempfile
from pathlib import Path

if sys.platform == "win32":
    from ctypes import wintypes

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QObject, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor, QFont, QGuiApplication, QIcon, QImage, QLinearGradient,
    QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QWidget
from .platform_support import ui_font_family

ICON_SIZES: tuple[int, ...] = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ICO_FILE_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)

APP_USER_MODEL_IDS: dict[str, str] = {
    "toolbox": "QingjianLocal.QingjianToolbox.Main",
    "notebook": "QingjianLocal.QingjianToolbox.Notebook",
    "tasks": "QingjianLocal.QingjianToolbox.Tasks",
    "reminder": "QingjianLocal.QingjianToolbox.Reminder",
    "converter": "QingjianLocal.QingjianToolbox.Converter",
    "search": "QingjianLocal.QingjianToolbox.Search",
    "duplicates": "QingjianLocal.QingjianToolbox.Duplicates",
}

_ICON_CACHE: dict[str, QIcon] = {}
_PIXMAP_CACHE: dict[tuple[str, int], QPixmap] = {}
_ICO_PATH_CACHE: dict[str, Path] = {}
_HICON_CACHE: dict[tuple[str, int], int] = {}
_ICON_VERSION = "v3"


def _ui_font(pixel_size: int, weight: QFont.Weight = QFont.Weight.Bold) -> QFont:
    font = QFont(ui_font_family())
    font.setPixelSize(max(8, int(round(pixel_size))))
    font.setWeight(weight)
    return font


def _squircle_path(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def _draw_squircle_base(
    painter: QPainter,
    size: int,
    stops: tuple[tuple[float, str], ...],
    glow_color: str,
    glow_pos: tuple[float, float] = (0.82, 0.18),
) -> QRectF:
    s = float(size)
    pad = max(0.75, round(s * 0.055, 2))
    rect = QRectF(pad, pad, s - pad * 2.0, s - pad * 2.0)
    radius = s * 0.24

    # Soft outer drop shadow for larger sizes
    if size >= 24:
        shadow_rect = rect.adjusted(0, s * 0.02, 0, s * 0.03)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(22, 18, 48, 38))
        painter.drawRoundedRect(shadow_rect, radius, radius)

    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    for pos, color in stops:
        grad.setColorAt(pos, QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(grad)
    painter.drawRoundedRect(rect, radius, radius)

    # Ambient top-right or top-left jade/warm radial glow
    glow = QRadialGradient(
        QPointF(rect.left() + rect.width() * glow_pos[0], rect.top() + rect.height() * glow_pos[1]),
        rect.width() * 0.65,
    )
    c_glow = QColor(glow_color)
    c_glow.setAlpha(95)
    c_zero = QColor(glow_color)
    c_zero.setAlpha(0)
    glow.setColorAt(0.0, c_glow)
    glow.setColorAt(1.0, c_zero)
    painter.setBrush(glow)
    painter.drawRoundedRect(rect, radius, radius)

    # Crisp glass inner highlight border
    border_w = max(0.8, s * 0.022)
    border_grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    border_grad.setColorAt(0.0, QColor(255, 255, 255, 115))
    border_grad.setColorAt(0.55, QColor(255, 255, 255, 38))
    border_grad.setColorAt(1.0, QColor(15, 23, 42, 55))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(border_grad, border_w))
    inset = border_w * 0.5
    painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset), radius - inset, radius - inset)
    return rect


def _paint_toolbox(painter: QPainter, size: int) -> None:
    """Main Qingjian Toolbox icon: Indigo-jade squircle + bamboo slips + '简' emblem."""
    s = float(size)
    rect = _draw_squircle_base(
        painter,
        size,
        ((0.0, "#3a32b4"), (0.52, "#584ce6"), (1.0, "#6f5ef2")),
        "#2dd4bf",
        (0.84, 0.16),
    )

    # Left bamboo-slip (竹简) vertical slats & binding cord
    slip_w = max(1.8, s * 0.072)
    slip1_x = rect.left() + rect.width() * 0.14
    slip_top = rect.top() + rect.height() * 0.19
    slip_h = rect.height() * 0.62
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(94, 234, 212, 165))
    painter.drawRoundedRect(QRectF(slip1_x, slip_top, slip_w, slip_h), slip_w * 0.48, slip_w * 0.48)

    if size >= 24:
        slip2_x = slip1_x + slip_w * 1.42
        painter.setBrush(QColor(255, 255, 255, 68))
        painter.drawRoundedRect(QRectF(slip2_x, slip_top + s * 0.04, slip_w * 0.88, slip_h - s * 0.08), slip_w * 0.44, slip_w * 0.44)
        # Bamboo binding threads
        cord_pen = QPen(QColor(255, 255, 255, 130), max(1.0, s * 0.022), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(cord_pen)
        for ratio in (0.33, 0.67):
            cy = rect.top() + rect.height() * ratio
            painter.drawLine(QPointF(slip1_x - s * 0.01, cy), QPointF(slip2_x + slip_w, cy))

    # Stylized '简' character on the main stage
    text_rect = QRectF(
        rect.left() + rect.width() * (0.22 if size >= 24 else 0.16),
        rect.top() + rect.height() * 0.06,
        rect.width() * (0.72 if size >= 24 else 0.78),
        rect.height() * 0.86,
    )
    font_px = round(s * (0.48 if size >= 24 else 0.54))
    painter.setFont(_ui_font(font_px, QFont.Weight.Bold))
    if size >= 24:
        painter.setPen(QColor(24, 18, 66, 75))
        painter.drawText(text_rect.translated(0, max(1.0, s * 0.02)), Qt.AlignmentFlag.AlignCenter, "简")
    painter.setPen(QColor("#ffffff"))
    painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "简")

    # Signature Qingjian jade seal dot at top-right
    dot_r = max(1.6, s * 0.062)
    dot_center = QPointF(rect.right() - rect.width() * 0.17, rect.top() + rect.height() * 0.21)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#5eead4"))
    painter.drawEllipse(dot_center, dot_r, dot_r)


def _paint_notebook(painter: QPainter, size: int) -> None:
    """Notebook & Reminder Calendar icon: Wisteria-violet squircle + calendar card + bell dot."""
    s = float(size)
    rect = _draw_squircle_base(
        painter,
        size,
        ((0.0, "#5141d6"), (0.52, "#6f58e8"), (1.0, "#9171f2")),
        "#f0abfc",
        (0.80, 0.18),
    )

    # Inner floating calendar page card
    card_rect = QRectF(
        rect.left() + rect.width() * 0.16,
        rect.top() + rect.height() * 0.18,
        rect.width() * 0.68,
        rect.height() * 0.66,
    )
    card_radius = s * 0.12
    card_grad = QLinearGradient(card_rect.topLeft(), card_rect.bottomLeft())
    card_grad.setColorAt(0.0, QColor("#ffffff"))
    card_grad.setColorAt(1.0, QColor("#f3eeff"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(card_grad)
    painter.drawRoundedRect(card_rect, card_radius, card_radius)

    # Calendar top header band
    header_h = card_rect.height() * 0.28
    header_path = QPainterPath()
    header_path.addRoundedRect(card_rect, card_radius, card_radius)
    clip_path = QPainterPath()
    clip_path.addRect(QRectF(card_rect.left(), card_rect.top(), card_rect.width(), header_h))
    painter.save()
    painter.setClipPath(header_path.intersected(clip_path))
    header_grad = QLinearGradient(card_rect.topLeft(), card_rect.topRight())
    header_grad.setColorAt(0.0, QColor("#4335be"))
    header_grad.setColorAt(1.0, QColor("#6850e3"))
    painter.setBrush(header_grad)
    painter.drawRect(QRectF(card_rect.left(), card_rect.top(), card_rect.width(), header_h))
    painter.restore()

    # Two spiral binding rings at the top of the calendar
    ring_w = max(1.6, s * 0.055)
    ring_h = max(2.8, s * 0.125)
    ring_y = card_rect.top() - ring_h * 0.42
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#ffffff"))
    for rx in (card_rect.left() + card_rect.width() * 0.27 - ring_w * 0.5,
               card_rect.left() + card_rect.width() * 0.73 - ring_w * 0.5):
        painter.drawRoundedRect(QRectF(rx, ring_y, ring_w, ring_h), ring_w * 0.5, ring_w * 0.5)

    # Calendar body content: crisp '记' at >= 24px, or clean grid dots at small tray sizes
    body_rect = QRectF(card_rect.left(), card_rect.top() + header_h, card_rect.width(), card_rect.height() - header_h)
    if size >= 24:
        painter.setFont(_ui_font(round(s * 0.34), QFont.Weight.Bold))
        painter.setPen(QColor("#4738c2"))
        painter.drawText(body_rect.adjusted(0, -s * 0.01, 0, 0), Qt.AlignmentFlag.AlignCenter, "记")
    else:
        dot_r = max(1.0, s * 0.052)
        for row_i, ry in enumerate((0.36, 0.68)):
            for col_i, cx in enumerate((0.28, 0.52, 0.74)):
                painter.setBrush(QColor("#f97316" if (row_i == 0 and col_i == 2) else "#5848d6"))
                painter.drawEllipse(
                    QPointF(card_rect.left() + card_rect.width() * cx, body_rect.top() + body_rect.height() * ry),
                    dot_r,
                    dot_r,
                )

    # Warm coral-amber reminder badge dot on top-right corner of the card
    badge_r = max(2.2, s * 0.095)
    badge_center = QPointF(card_rect.right() - s * 0.02, card_rect.top() + header_h * 0.55)
    painter.setPen(QPen(QColor("#ffffff"), max(1.0, s * 0.028)))
    painter.setBrush(QColor("#fb7185"))
    painter.drawEllipse(badge_center, badge_r, badge_r)


def _paint_tasks(painter: QPainter, size: int) -> None:
    """Companion Daily Tasks icon: Indigo-teal squircle + task slip + emerald check badge."""
    s = float(size)
    rect = _draw_squircle_base(
        painter,
        size,
        ((0.0, "#3f46cc"), (0.55, "#5c59e3"), (1.0, "#2ea89c")),
        "#5eead4",
        (0.82, 0.80),
    )

    # Floating checklist slip card
    card_rect = QRectF(
        rect.left() + rect.width() * 0.16,
        rect.top() + rect.height() * 0.16,
        rect.width() * 0.64,
        rect.height() * 0.68,
    )
    card_radius = s * 0.11
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#faf9ff"))
    painter.drawRoundedRect(card_rect, card_radius, card_radius)

    # Checklist lines on the card
    line_pen = QPen(QColor("#5245cd"), max(1.3, s * 0.048), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    dot_r = max(1.2, s * 0.036)
    for idx, y_ratio in enumerate((0.30, 0.52, 0.74)):
        ly = card_rect.top() + card_rect.height() * y_ratio
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#10b981" if idx == 0 else "#7c69ef"))
        painter.drawEllipse(QPointF(card_rect.left() + card_rect.width() * 0.22, ly), dot_r, dot_r)
        painter.setPen(line_pen)
        end_ratio = 0.78 if idx < 2 else 0.56
        painter.drawLine(
            QPointF(card_rect.left() + card_rect.width() * 0.36, ly),
            QPointF(card_rect.left() + card_rect.width() * end_ratio, ly),
        )

    # Glowing emerald-jade checkmark badge at bottom-right
    badge_r = max(3.6, s * 0.20)
    badge_center = QPointF(rect.right() - rect.width() * 0.24, rect.bottom() - rect.height() * 0.24)
    painter.setPen(QPen(QColor("#ffffff"), max(1.1, s * 0.032)))
    painter.setBrush(QColor("#10b981"))
    painter.drawEllipse(badge_center, badge_r, badge_r)

    check_pen = QPen(QColor("#ffffff"), max(1.4, s * 0.052), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(check_pen)
    p1 = QPointF(badge_center.x() - badge_r * 0.42, badge_center.y() + badge_r * 0.02)
    p2 = QPointF(badge_center.x() - badge_r * 0.10, badge_center.y() + badge_r * 0.34)
    p3 = QPointF(badge_center.x() + badge_r * 0.46, badge_center.y() - badge_r * 0.30)
    painter.drawPolyline(QPolygonF([p1, p2, p3]))


def _paint_reminder(painter: QPainter, size: int) -> None:
    """Reminder alert card icon: Deep violet-indigo squircle + golden-ivory bell + alert dot."""
    s = float(size)
    rect = _draw_squircle_base(
        painter,
        size,
        ((0.0, "#4b3cd1"), (0.55, "#6b54e8"), (1.0, "#9368f0")),
        "#fbbf24",
        (0.78, 0.20),
    )

    cx = rect.center().x()
    cy = rect.center().y() + s * 0.02
    bell_w = rect.width() * 0.48
    bell_h = rect.height() * 0.46

    bell = QPainterPath()
    top_y = cy - bell_h * 0.52
    bot_y = cy + bell_h * 0.38
    bell.moveTo(cx - bell_w * 0.52, bot_y)
    bell.quadTo(cx - bell_w * 0.40, cy + bell_h * 0.08, cx - bell_w * 0.38, cy - bell_h * 0.12)
    bell.cubicTo(
        cx - bell_w * 0.38, top_y,
        cx + bell_w * 0.38, top_y,
        cx + bell_w * 0.38, cy - bell_h * 0.12,
    )
    bell.quadTo(cx + bell_w * 0.40, cy + bell_h * 0.08, cx + bell_w * 0.52, bot_y)
    bell.closeSubpath()

    bell_grad = QLinearGradient(QPointF(cx, top_y), QPointF(cx, bot_y))
    bell_grad.setColorAt(0.0, QColor("#ffffff"))
    bell_grad.setColorAt(1.0, QColor("#fde68a"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(bell_grad)
    painter.drawPath(bell)

    # Bell rim & clapper
    painter.setBrush(QColor("#ffffff"))
    rim_h = max(1.6, s * 0.055)
    painter.drawRoundedRect(QRectF(cx - bell_w * 0.56, bot_y - rim_h * 0.4, bell_w * 1.12, rim_h), rim_h * 0.5, rim_h * 0.5)
    clapper_r = max(1.6, s * 0.065)
    painter.setBrush(QColor("#fbbf24"))
    painter.drawEllipse(QPointF(cx, bot_y + rim_h * 0.85), clapper_r, clapper_r)

    # Coral notification dot
    dot_r = max(2.2, s * 0.095)
    painter.setPen(QPen(QColor("#ffffff"), max(1.0, s * 0.028)))
    painter.setBrush(QColor("#fb7185"))
    painter.drawEllipse(QPointF(cx + bell_w * 0.38, top_y + bell_h * 0.14), dot_r, dot_r)


def _draw_arc_arrow(
    painter: QPainter,
    center: QPointF,
    radius: float,
    base_deg: float,
    tip_deg: float,
    half_width: float,
    color: QColor,
) -> None:
    import math
    t_rad = math.radians(tip_deg)
    b_rad = math.radians(base_deg)
    tip = QPointF(center.x() + radius * math.cos(t_rad), center.y() - radius * math.sin(t_rad))
    outer = QPointF(
        center.x() + (radius + half_width) * math.cos(b_rad),
        center.y() - (radius + half_width) * math.sin(b_rad),
    )
    inner = QPointF(
        center.x() + (radius - half_width) * math.cos(b_rad),
        center.y() - (radius - half_width) * math.sin(b_rad),
    )
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPolygon(QPolygonF([tip, outer, inner]))
    painter.restore()


def _paint_converter(painter: QPainter, size: int) -> None:
    """Quick Format Converter icon: Warm terracotta-amber squircle + FormatRing orbit + '转' core."""
    s = float(size)
    rect = _draw_squircle_base(
        painter,
        size,
        ((0.0, "#b84a29"), (0.52, "#d97757"), (1.0, "#e89572")),
        "#fef3c7",
        (0.22, 0.18),
    )

    center = rect.center()
    ring_r = rect.width() * 0.335
    ring_rect = QRectF(center.x() - ring_r, center.y() - ring_r, ring_r * 2.0, ring_r * 2.0)

    # Orbiting conversion arcs (matching ConverterWindow's FormatRing)
    arc_w = max(1.6, s * 0.068)
    arc_pen = QPen(QColor("#ffffff"), arc_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(arc_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(ring_rect, 42 * 16, 112 * 16)
    painter.drawArc(ring_rect, 222 * 16, 112 * 16)

    # Radial arrowheads at the clockwise ends of both arcs
    half_w = max(2.2, s * 0.095)
    _draw_arc_arrow(painter, center, ring_r, 44.0, 18.0, half_w, QColor("#ffffff"))
    _draw_arc_arrow(painter, center, ring_r, 224.0, 198.0, half_w, QColor("#ffffff"))

    # Warm ivory inner disc with '转' emblem (or crisp dual-dot core at tiny sizes)
    core_r = rect.width() * (0.215 if size >= 24 else 0.17)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#faf9f5"))
    painter.drawEllipse(center, core_r, core_r)

    if size >= 24:
        painter.setFont(_ui_font(round(s * 0.25), QFont.Weight.Bold))
        painter.setPen(QColor("#bd5534"))
        core_rect = QRectF(center.x() - core_r, center.y() - core_r, core_r * 2.0, core_r * 2.0)
        painter.drawText(core_rect.adjusted(0, -s * 0.008, 0, 0), Qt.AlignmentFlag.AlignCenter, "转")


def _paint_search(painter: QPainter, size: int) -> None:
    """Document Search dialog icon: Qingjian indigo-teal squircle + document + magnifier."""
    s = float(size)
    rect = _draw_squircle_base(
        painter,
        size,
        ((0.0, "#3142b8"), (0.55, "#3b66df"), (1.0, "#14968f")),
        "#5eead4",
        (0.82, 0.20),
    )
    lens_r = rect.width() * 0.22
    lens_c = QPointF(rect.center().x() - s * 0.04, rect.center().y() - s * 0.04)
    painter.setPen(QPen(QColor("#ffffff"), max(1.8, s * 0.075), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.setBrush(QColor(255, 255, 255, 42))
    painter.drawEllipse(lens_c, lens_r, lens_r)
    handle_start = QPointF(lens_c.x() + lens_r * 0.72, lens_c.y() + lens_r * 0.72)
    handle_end = QPointF(rect.right() - rect.width() * 0.20, rect.bottom() - rect.height() * 0.20)
    painter.drawLine(handle_start, handle_end)


def _paint_duplicates(painter: QPainter, size: int) -> None:
    """Duplicate Finder dialog icon: Qingjian indigo-cyan squircle + twin overlapping sheets."""
    s = float(size)
    rect = _draw_squircle_base(
        painter,
        size,
        ((0.0, "#3b36b8"), (0.55, "#524ce6"), (1.0, "#0ea5e9")),
        "#67e8f9",
        (0.82, 0.20),
    )
    card_w = rect.width() * 0.42
    card_h = rect.height() * 0.50
    r = s * 0.08
    back = QRectF(rect.left() + rect.width() * 0.20, rect.top() + rect.height() * 0.18, card_w, card_h)
    front = QRectF(rect.left() + rect.width() * 0.38, rect.top() + rect.height() * 0.32, card_w, card_h)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255, 145))
    painter.drawRoundedRect(back, r, r)
    painter.setBrush(QColor("#ffffff"))
    painter.drawRoundedRect(front, r, r)


_PAINTERS = {
    "toolbox": _paint_toolbox,
    "notebook": _paint_notebook,
    "tasks": _paint_tasks,
    "reminder": _paint_reminder,
    "converter": _paint_converter,
    "search": _paint_search,
    "duplicates": _paint_duplicates,
}


def render_icon_pixmap(kind: str = "toolbox", size: int = 64) -> QPixmap:
    """Render a single resolution QPixmap for the requested Qingjian icon kind."""
    kind = kind if kind in _PAINTERS else "toolbox"
    size = max(12, int(size))
    key = (kind, size)
    cached = _PIXMAP_CACHE.get(key)
    if cached is not None and not cached.isNull():
        return cached
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    _PAINTERS[kind](painter, size)
    painter.end()
    _PIXMAP_CACHE[key] = pixmap
    return pixmap


def get_icon(kind: str = "toolbox") -> QIcon:
    """Return a cached multi-size QIcon (16px..256px) for the given role."""
    kind = kind if kind in _PAINTERS else "toolbox"
    cached = _ICON_CACHE.get(kind)
    if cached is not None and not cached.isNull():
        return cached
    icon_obj = QIcon()
    for sz in ICON_SIZES:
        pix = render_icon_pixmap(kind, sz)
        icon_obj.addPixmap(pix, QIcon.Mode.Normal, QIcon.State.Off)
        icon_obj.addPixmap(pix, QIcon.Mode.Active, QIcon.State.Off)
        icon_obj.addPixmap(pix, QIcon.Mode.Selected, QIcon.State.Off)
    _ICON_CACHE[kind] = icon_obj
    return icon_obj


def toolbox_icon() -> QIcon:
    return get_icon("toolbox")


def notebook_icon() -> QIcon:
    return get_icon("notebook")


def tasks_icon() -> QIcon:
    return get_icon("tasks")


def reminder_icon() -> QIcon:
    return get_icon("reminder")


def converter_icon() -> QIcon:
    return get_icon("converter")


def search_icon() -> QIcon:
    return get_icon("search")


def duplicates_icon() -> QIcon:
    return get_icon("duplicates")


def titlebar_badge_pixmap(kind: str = "toolbox", size: int = 16) -> QPixmap:
    """Return a crisp high-DPI mini badge pixmap for custom frameless titlebars."""
    ratio = 2
    raw = render_icon_pixmap(kind, size * ratio)
    copied = QPixmap(raw)
    copied.setDevicePixelRatio(ratio)
    return copied


def sidebar_converter_icon(size: int = 20) -> QIcon:
    """Vector dual-arc conversion ring icon for the sidebar converter launch button."""
    key = ("sidebar_converter", size)
    if key in _PIXMAP_CACHE:
        return QIcon(_PIXMAP_CACHE[key])
    s = max(16, int(size))
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    center = QPointF(s * 0.5, s * 0.5)
    r = s * 0.34
    ring_rect = QRectF(center.x() - r, center.y() - r, r * 2.0, r * 2.0)
    color = QColor("#b35334")
    painter.setPen(QPen(color, max(1.6, s * 0.085), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawArc(ring_rect, 44 * 16, 110 * 16)
    painter.drawArc(ring_rect, 224 * 16, 110 * 16)
    half_w = max(2.0, s * 0.105)
    _draw_arc_arrow(painter, center, r, 46.0, 16.0, half_w, color)
    _draw_arc_arrow(painter, center, r, 226.0, 196.0, half_w, color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#d97757"))
    painter.drawEllipse(center, s * 0.11, s * 0.11)
    painter.end()
    _PIXMAP_CACHE[key] = pix
    return QIcon(pix)


def sidebar_notebook_icon(size: int = 22) -> QIcon:
    """Vector calendar + reminder dot icon for the sidebar notebook launch button."""
    key = ("sidebar_notebook", size)
    if key in _PIXMAP_CACHE:
        return QIcon(_PIXMAP_CACHE[key])
    s = max(16, int(size))
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor("#ffffff"), 1.65, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(3.0, 4.2, s - 6.0, s - 7.2), 3.5, 3.5)
    painter.drawLine(QPointF(3.5, 9.0), QPointF(s - 3.5, 9.0))
    painter.drawLine(QPointF(7.2, 2.2), QPointF(7.2, 5.8))
    painter.drawLine(QPointF(s - 7.2, 2.2), QPointF(s - 7.2, 5.8))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#ffffff"))
    for gx, gy in ((7.8, 12.8), (11.2, 12.8), (7.8, 15.8)):
        painter.drawEllipse(QPointF(gx, gy), 1.15, 1.15)
    painter.setBrush(QColor("#fde68a"))
    painter.drawEllipse(QPointF(14.5, 14.8), 1.65, 1.65)
    painter.end()
    _PIXMAP_CACHE[key] = pix
    return QIcon(pix)


def _ico_cache_dir() -> Path:
    base = Path(tempfile.gettempdir()) / "QingjianToolbox-icons"
    base.mkdir(parents=True, exist_ok=True)
    return base


def build_ico_bytes(kind: str = "toolbox") -> bytes:
    """Serialize a multi-resolution Windows .ico file (16px..256px RGBA PNG frames)."""
    kind = kind if kind in _PAINTERS else "toolbox"
    payloads: list[tuple[int, bytes]] = []
    for sz in ICO_FILE_SIZES:
        img = render_icon_pixmap(kind, sz).toImage().convertToFormat(QImage.Format.Format_ARGB32)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        payloads.append((sz, bytes(buf.data())))

    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = 6 + 16 * len(payloads)
    entries: list[bytes] = []
    for sz, data in payloads:
        w = 0 if sz >= 256 else sz
        h = 0 if sz >= 256 else sz
        entries.append(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    return header + b"".join(entries) + b"".join(data for _, data in payloads)


def ensure_ico_file(kind: str = "toolbox") -> Path | None:
    """Write and cache a multi-size .ico file for Win32 taskbar and HICON APIs."""
    kind = kind if kind in _PAINTERS else "toolbox"
    cached = _ICO_PATH_CACHE.get(kind)
    if cached is not None and cached.is_file():
        return cached
    try:
        folder = _ico_cache_dir()
        target = folder / f"qingjian-{kind}-{_ICON_VERSION}.ico"
        if not target.is_file() or target.stat().st_size < 256:
            data = build_ico_bytes(kind)
            fd, tmp_name = tempfile.mkstemp(prefix=f".qj-{kind}-", suffix=".tmp", dir=folder)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(tmp_path, target)
            finally:
                tmp_path.unlink(missing_ok=True)
        _ICO_PATH_CACHE[kind] = target
        return target
    except OSError:
        return None


def apply_process_taskbar_identity(app_id: str | None = None) -> bool:
    """Set the Windows process-level explicit AppUserModelID before showing windows."""
    if sys.platform != "win32":
        return False
    try:
        target_id = app_id or APP_USER_MODEL_IDS["toolbox"]
        hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(target_id))
        return hr == 0
    except Exception:
        return False


if sys.platform == "win32":
    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]

    class _PROPVARIANT(ctypes.Structure):
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
            ("wReserved2", ctypes.c_ushort),
            ("wReserved3", ctypes.c_ushort),
            ("pwszVal", ctypes.c_wchar_p),
            ("padding", ctypes.c_ulonglong),
        ]

    _IID_IPropertyStore = _GUID(
        0x886D8EEB, 0x8CF2, 0x4446,
        (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99),
    )
    _PKEY_AppUserModel_Group = _GUID(
        0x9F4C2855, 0x9F79, 0x4B39,
        (ctypes.c_ubyte * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3),
    )
    _PKEY_AppUserModel_ID = _PROPERTYKEY(_PKEY_AppUserModel_Group, 5)
    _PKEY_AppUserModel_RelaunchIconResource = _PROPERTYKEY(_PKEY_AppUserModel_Group, 3)
    _PKEY_AppUserModel_RelaunchDisplayNameResource = _PROPERTYKEY(_PKEY_AppUserModel_Group, 4)


def _load_win32_hicon(ico_path: Path, kind: str, pixel_size: int) -> int:
    key = (kind, pixel_size)
    if key in _HICON_CACHE and _HICON_CACHE[key]:
        return _HICON_CACHE[key]
    LR_LOADFROMFILE = 0x00000010
    IMAGE_ICON = 1
    LoadImageW = ctypes.windll.user32.LoadImageW
    LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    LoadImageW.restype = wintypes.HANDLE
    handle = LoadImageW(None, str(ico_path), IMAGE_ICON, int(pixel_size), int(pixel_size), LR_LOADFROMFILE)
    hicon = int(handle or 0)
    if hicon:
        _HICON_CACHE[key] = hicon
    return hicon


def _apply_hwnd_property_store(hwnd: int, app_id: str, ico_path: Path, display_name: str) -> None:
    pps = ctypes.c_void_p()
    SHGetPropertyStoreForWindow = ctypes.windll.shell32.SHGetPropertyStoreForWindow
    SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    SHGetPropertyStoreForWindow.restype = ctypes.c_long
    hr = SHGetPropertyStoreForWindow(wintypes.HWND(hwnd), ctypes.byref(_IID_IPropertyStore), ctypes.byref(pps))
    if hr != 0 or not pps.value:
        return
    try:
        vt_table = ctypes.cast(ctypes.cast(pps, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        SetValue = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT)
        )(vt_table[6])
        Commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vt_table[7])
        for pkey, text in (
            (_PKEY_AppUserModel_ID, app_id),
            (_PKEY_AppUserModel_RelaunchIconResource, f"{ico_path},0"),
            (_PKEY_AppUserModel_RelaunchDisplayNameResource, display_name),
        ):
            if text:
                pv = _PROPVARIANT(vt=31, pwszVal=str(text))
                SetValue(pps, ctypes.byref(pkey), ctypes.byref(pv))
        Commit(pps)
    finally:
        vt_table = ctypes.cast(ctypes.cast(pps, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt_table[2])
        Release(pps)


def apply_native_window_icon(widget: QWidget, kind: str = "toolbox") -> bool:
    """Apply Win32 WM_SETICON, frameless WS_SYSMENU fix, and Shell IPropertyStore icon."""
    if sys.platform != "win32":
        return False
    app = QGuiApplication.instance()
    if app is None or app.platformName().lower() != "windows":
        return False
    try:
        hwnd = int(widget.winId())
    except Exception:
        return False
    if not hwnd:
        return False
    kind = kind if kind in _PAINTERS else "toolbox"
    if getattr(widget, "_qj_synced_hwnd", None) == (hwnd, kind):
        return True

    ico_path = ensure_ico_file(kind)
    if ico_path is None:
        return False

    try:
        user32 = ctypes.windll.user32
        # Frameless windows (WS_POPUP without WS_CAPTION) lack WS_SYSMENU by default,
        # which causes Windows Explorer's taskbar to ignore WM_GETICON and show the
        # generic default icon. Adding WS_SYSMENU (while keeping WS_CAPTION unset)
        # preserves the exact frameless geometry and transparency while enabling taskbar icons.
        GWL_STYLE = -16
        WS_CAPTION = 0x00C00000
        WS_SYSMENU = 0x00080000
        if widget.windowFlags() & Qt.WindowType.FramelessWindowHint:
            style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
            if not (style & WS_CAPTION) and not (style & WS_SYSMENU):
                user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, style | WS_SYSMENU)

        hicon_small = _load_win32_hicon(ico_path, kind, 16)
        hicon_big = _load_win32_hicon(ico_path, kind, 64)
        WM_SETICON = 0x0080
        SendMessageW = user32.SendMessageW
        SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        SendMessageW.restype = wintypes.LPARAM
        if hicon_small:
            SendMessageW(wintypes.HWND(hwnd), WM_SETICON, 0, hicon_small)
        if hicon_big:
            SendMessageW(wintypes.HWND(hwnd), WM_SETICON, 1, hicon_big)

        if kind == "toolbox" and hasattr(user32, "SetClassLongPtrW"):
            SetClassLongPtrW = user32.SetClassLongPtrW
            SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            SetClassLongPtrW.restype = ctypes.c_void_p
            if hicon_big:
                SetClassLongPtrW(wintypes.HWND(hwnd), -14, ctypes.c_void_p(hicon_big))
            if hicon_small:
                SetClassLongPtrW(wintypes.HWND(hwnd), -34, ctypes.c_void_p(hicon_small))

        app_id = APP_USER_MODEL_IDS.get(kind, APP_USER_MODEL_IDS["toolbox"])
        title = widget.windowTitle() or "青简工具箱"
        _apply_hwnd_property_store(hwnd, app_id, ico_path, title)
        widget._qj_synced_hwnd = (hwnd, kind)
        return True
    except Exception:
        return False


class _TaskbarIconEventFilter(QObject):
    """Re-applies native Win32 taskbar icons whenever a top-level HWND is shown or recreated."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        etype = event.type()
        if etype in (QEvent.Type.Show, QEvent.Type.WinIdChange) and isinstance(watched, QWidget):
            kind = getattr(watched, "_qj_icon_kind", None)
            if kind and watched.isWindow():
                if etype == QEvent.Type.WinIdChange:
                    watched._qj_synced_hwnd = None
                if watched.isVisible() or etype == QEvent.Type.Show:
                    apply_native_window_icon(watched, kind)
        return False


_FILTER_INSTANCE: _TaskbarIconEventFilter | None = None


def _icon_event_filter() -> _TaskbarIconEventFilter:
    global _FILTER_INSTANCE
    if _FILTER_INSTANCE is None:
        app = QApplication.instance()
        _FILTER_INSTANCE = _TaskbarIconEventFilter(app)
    return _FILTER_INSTANCE


def bind_window_icon(widget: QWidget, kind: str = "toolbox") -> QIcon:
    """Assign a multi-size Qingjian QIcon to a window and keep its Win32 taskbar icon synced."""
    kind = kind if kind in _PAINTERS else "toolbox"
    icon_obj = get_icon(kind)
    widget._qj_icon_kind = kind
    widget._qj_synced_hwnd = None
    widget.setWindowIcon(icon_obj)
    if not getattr(widget, "_qj_icon_filter_installed", False):
        widget.installEventFilter(_icon_event_filter())
        widget._qj_icon_filter_installed = True
    if widget.isVisible():
        apply_native_window_icon(widget, kind)
    return icon_obj
