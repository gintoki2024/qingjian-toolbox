"""Screen-relative geometry for the small, side-by-side office window.

All values are Qt logical pixels; callers must use QScreen.availableGeometry()
without multiplying by devicePixelRatio().  The returned QRect is the client
geometry accepted by QWidget.setGeometry(), with native window decorations
already allowed for.  This module does not move windows, select screens, change
DPI settings, or constrain maximized windows.
"""
from __future__ import annotations

from PySide6.QtCore import QMargins, QRect, QSize


_FALLBACK_SCREEN = (0, 0, 1280, 720)
_FALLBACK_FRAME = (8, 31, 8, 8)
_MAX_OUTER_WIDTH = 540
_MAX_OUTER_HEIGHT = 780


def _screen_rect(available: QRect) -> QRect:
    """Keep multi-monitor coordinates; use a safe size before a screen exists."""
    if available.width() <= 0 or available.height() <= 0:
        return QRect(*_FALLBACK_SCREEN)
    return QRect(available)


def _margins(frame_margins: QMargins | None) -> tuple[int, int, int, int]:
    if frame_margins is None:
        return _FALLBACK_FRAME
    # Some platforms briefly report negative frame margins before show().
    return tuple(max(0, value) for value in (
        frame_margins.left(), frame_margins.top(),
        frame_margins.right(), frame_margins.bottom(),
    ))


def compact_window_geometry(
    available: QRect, frame_margins: QMargins | None = None,
) -> QRect:
    """Return a compact client rectangle near the current screen's right edge.

    The complete outer frame takes at most one third of the available screen's
    width (capped at 540), and 90% of its height (capped at 780).  Its area is
    consequently below one third as well.  Twelve logical pixels separate the
    outer frame from the right screen edge when space permits.

    Pass actual nonnegative decoration margins after the window is shown, or
    omit them for a conservative Windows title-bar fallback.  Degenerate screen
    rectangles use a 1280 x 720 fallback.  For physically impossible screen/frame
    combinations, the client keeps a positive one-pixel size; a frame wider or
    taller than the screen cannot be made to fit by setting client geometry.
    """
    screen = _screen_rect(available)
    left, top, right, bottom = _margins(frame_margins)
    frame_width, frame_height = left + right, top + bottom
    outer_width = max(frame_width + 1, min(_MAX_OUTER_WIDTH, screen.width() // 3))
    outer_height = max(frame_height + 1, min(_MAX_OUTER_HEIGHT, screen.height() * 9 // 10))
    # Preserve meaningful placement for small valid screens as well.  The
    # one-pixel client fallback above is the only possible oversize case.
    right_gap = min(12, max(0, (screen.width() - outer_width) // 2))
    outer_x = screen.x() + screen.width() - outer_width - right_gap
    outer_y = screen.y() + max(0, (screen.height() - outer_height) // 2)
    return QRect(outer_x + left, outer_y + top,
                 outer_width - frame_width, outer_height - frame_height)


def compact_window_minimum_size(
    available: QRect, frame_margins: QMargins | None = None,
) -> QSize:
    """Return a practical client minimum that never prevents compact sizing.

    Normally 360 x 480, the minimum shrinks on a small logical screen so that
    native decorations do not push the compact outer frame above its budget.
    Layouts must also allow their widgets to shrink or scroll to this width.
    """
    geometry = compact_window_geometry(available, frame_margins)
    return QSize(min(360, geometry.width()), min(480, geometry.height()))
