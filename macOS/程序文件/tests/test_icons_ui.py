"""Verify the Qingjian icon suite and taskbar icon bindings across top-level windows."""
from __future__ import annotations

from datetime import datetime
import struct

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtWidgets import QApplication

from qingjian.app import Window
from qingjian.converter_ui import ConverterWindow
from qingjian.duplicates_ui import DuplicatesDialog
from qingjian.icons import (
    APP_USER_MODEL_IDS, ICO_FILE_SIZES, ICON_SIZES, apply_process_taskbar_identity,
    build_ico_bytes, converter_icon, duplicates_icon, ensure_ico_file,
    notebook_icon, reminder_icon, render_icon_pixmap, search_icon, tasks_icon,
    toolbox_icon,
)
from qingjian.notebook_runtime import ReminderCard, _icon as tray_notebook_icon
from qingjian.notebook_store import NotebookStore
from qingjian.notebook_ui import NotebookWindow, TaskEditorDialog
from qingjian.results_view import ResultDialog
from qingjian.search_ui import DocumentSearchDialog
from qingjian.window_appearance import AppearanceController, AppearanceDialog


def _png_bytes(pixmap) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buf, "PNG")
    return bytes(data)


def test_qingjian_icon_suite_resolutions_and_distinct_designs():
    app = QApplication.instance() or QApplication([])
    assert app is not None
    suite = {
        "toolbox": toolbox_icon(),
        "notebook": notebook_icon(),
        "tasks": tasks_icon(),
        "reminder": reminder_icon(),
        "converter": converter_icon(),
        "search": search_icon(),
        "duplicates": duplicates_icon(),
    }
    rendered_64 = {}
    for kind, icon_obj in suite.items():
        assert not icon_obj.isNull(), f"{kind} icon should not be null"
        sizes = {(s.width(), s.height()) for s in icon_obj.availableSizes()}
        for expected in ICON_SIZES:
            assert (expected, expected) in sizes, f"{kind} missing {expected}x{expected}"
        pix = render_icon_pixmap(kind, 64)
        img = pix.toImage()
        assert img.pixelColor(0, 0).alpha() == 0, f"{kind} should have rounded transparent corners"
        assert img.pixelColor(32, 32).alpha() == 255, f"{kind} center should be opaque"
        rendered_64[kind] = _png_bytes(pix)

    # Ensure each major window role has a visually distinct icon design
    assert len({rendered_64["toolbox"], rendered_64["notebook"], rendered_64["tasks"],
                rendered_64["converter"], rendered_64["reminder"]}) == 5
    assert _png_bytes(tray_notebook_icon().pixmap(QSize(64, 64))) == _png_bytes(notebook_icon().pixmap(QSize(64, 64)))


def test_multi_size_ico_serialization_and_process_identity():
    app = QApplication.instance() or QApplication([])
    assert app is not None
    assert apply_process_taskbar_identity()
    for kind in APP_USER_MODEL_IDS:
        raw = build_ico_bytes(kind)
        reserved, ico_type, count = struct.unpack("<HHH", raw[:6])
        assert (reserved, ico_type, count) == (0, 1, len(ICO_FILE_SIZES))
        ico_path = ensure_ico_file(kind)
        assert ico_path is not None and ico_path.is_file() and ico_path.stat().st_size > 1000
        disk_header = struct.unpack("<HHH", ico_path.read_bytes()[:6])
        assert disk_header == (0, 1, len(ICO_FILE_SIZES))


def test_all_top_level_windows_bind_matching_qingjian_icons(tmp_path):
    app = QApplication.instance() or QApplication([])
    assert app is not None
    window = Window(start_home=True, data_path=tmp_path / "profile.json")
    store = NotebookStore(tmp_path / "notebook.json")
    notebook = NotebookWindow(store, clock=lambda: datetime(2026, 4, 20, 9, 0))
    editor = TaskEditorDialog(store, clock=lambda: datetime(2026, 4, 20, 9, 0))
    reminder = ReminderCard({"id": "a1", "title": "测试提醒", "due": "2026-04-20T09:00:00"})
    converter = ConverterWindow(tmp_path / "converter.json")
    search_dlg = DocumentSearchDialog([], {"query": "青简"})
    dup_dlg = DuplicatesDialog([], {})
    appearance_ctrl = AppearanceController(tmp_path / "appearance.json")
    appearance_dlg = AppearanceDialog(appearance_ctrl)
    result_dlg = ResultDialog({"folder": str(tmp_path), "records": []})

    try:
        expected_pairs = [
            (window, "toolbox"),
            (notebook, "notebook"),
            (notebook.tasks_window, "tasks"),
            (editor, "notebook"),
            (reminder, "reminder"),
            (converter, "converter"),
            (search_dlg, "search"),
            (dup_dlg, "duplicates"),
            (appearance_dlg, "toolbox"),
            (result_dlg, "toolbox"),
        ]
        for widget, expected_kind in expected_pairs:
            assert getattr(widget, "_qj_icon_kind", None) == expected_kind
            assert not widget.windowIcon().isNull()
            sizes = {(s.width(), s.height()) for s in widget.windowIcon().availableSizes()}
            assert (16, 16) in sizes and (32, 32) in sizes and (64, 64) in sizes and (256, 256) in sizes

        # Sidebar launch buttons also carry bespoke vector icons
        assert not window.converter_button.icon().isNull()
        assert not window.notebook_button.icon().isNull()
    finally:
        for w in (result_dlg, appearance_dlg, dup_dlg, search_dlg, converter, reminder, editor):
            w.close()
            w.deleteLater()
        appearance_ctrl.shutdown()
        notebook.close_windows()
        notebook.deleteLater()
        window.close()
        window.deleteLater()
        app.processEvents()
