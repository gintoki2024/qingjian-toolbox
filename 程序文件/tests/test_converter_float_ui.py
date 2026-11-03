"""Real copy-only drag targets, detachable lifetime and shared-worker checks."""
from __future__ import annotations

import json
import threading
import time

from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import pytest

from qingjian.app import Window
from qingjian.converter_ui import ConverterWindow, FormatRing, available_targets
from qingjian.quick_ui import QuickPanel


def pump(count=4):
    for _ in range(count):
        QApplication.processEvents()
        QTest.qWait(2)


def until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        pump(1)
    assert predicate(), "shared conversion worker did not reach expected state"


@pytest.fixture
def toolbox(tmp_path):
    window = Window(start_home=True, data_path=tmp_path / "profile.json")
    window.show()
    window.show_converter()
    pump()
    yield window
    if window.worker is not None:
        window.worker.requestInterruption()
        until(lambda: window.worker is None)
    window.converter_window.shutdown()
    window.converter_window.deleteLater()
    window.close()
    window.deleteLater()
    QApplication.clipboard().clear()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    pump()


def image_source(tmp_path, name="来自 原目录.png"):
    source = tmp_path / name
    Image.new("RGBA", (40, 25), (100, 140, 190, 160)).save(source)
    return source


def jpeg_target(source):
    targets = available_targets([str(source)])
    for target in targets:
        if target["id"] in {"convert:jpg", "convert:jpeg", "image_to_jpg"}:
            return target["id"]
    pytest.fail("PNG should offer a JPEG conversion target")


def drag_drop(widget, sources, position):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source)) for source in sources])
    actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
    enter = QDragEnterEvent(position, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    QApplication.sendEvent(widget, enter)
    drop = QDropEvent(QPointF(position), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    QApplication.sendEvent(widget, drop)
    pump(1)
    return enter, drop


def test_detached_geometry_highlight_controls_and_docking(toolbox):
    floating = toolbox.converter_window
    assert floating.parent() is None and floating.isWindow() and floating.isVisible()
    assert toolbox.compact_converter.objectName() == "converterLaunch"
    assert toolbox.converter_button.objectName() == "converterLaunch"
    assert not hasattr(toolbox.home_page, "quick_panel")
    assert not toolbox.findChildren(QuickPanel)
    assert len(toolbox.home_page.group_cards) == 5
    assert floating.width() <= 340 and floating.height() <= 550
    floating.set_collapsed(True)
    pump()
    assert floating.size().width() == 76 and floating.size().height() == 76
    QTest.mouseClick(floating.bubble, Qt.MouseButton.LeftButton)
    pump()
    assert not floating.collapsed and floating.ring.isVisible()
    toolbox.close()
    pump()
    assert not toolbox.isVisible() and floating.isVisible()
    QTest.mouseClick(floating.dock_button, Qt.MouseButton.LeftButton)
    pump()
    assert toolbox.isVisible() and not floating.isVisible()


def test_ring_center_drop_only_stages_then_click_converts_beside_source(toolbox, tmp_path):
    floating = toolbox.converter_window
    source = image_source(tmp_path)
    original = source.read_bytes()
    enter, drop = drag_drop(floating.ring, [source], QPoint(122, 122))
    assert enter.isAccepted() and drop.isAccepted()
    assert enter.dropAction() == drop.dropAction() == Qt.DropAction.CopyAction
    assert toolbox.worker is None
    assert floating.sources == [str(source)]
    assert floating.choose_target(jpeg_target(source))
    until(lambda: toolbox.worker is None)
    assert source.read_bytes() == original
    outputs = floating._last_outputs
    assert len(outputs) == 1
    from pathlib import Path
    output = Path(outputs[0])
    assert output.parent == source.parent and output.suffix.lower() in {".jpg", ".jpeg"}
    assert Image.open(output).size == (40, 25)
    assert floating.results_button.isEnabled() and floating.folder_button.isEnabled()
    assert toolbox.quick_result["records"][0]["状态"] == "成功"


def test_actual_drop_on_target_sector_starts_conversion_with_copy_semantics(toolbox, tmp_path):
    floating = toolbox.converter_window
    source = image_source(tmp_path)
    original = source.read_bytes()
    floating.preview_sources([str(source)])
    key = jpeg_target(source)
    index = next(i for i, target in enumerate(floating.ring.targets) if target["id"] == key)
    floating.ring.page = index // 8
    floating.ring._rebuild()
    point = floating.ring.buttons[index % 8].geometry().center()
    enter, drop = drag_drop(floating.ring, [source], point)
    assert enter.isAccepted() and drop.isAccepted()
    assert drop.dropAction() == Qt.DropAction.CopyAction
    until(lambda: toolbox.worker is None)
    assert floating._last_outputs and source.read_bytes() == original


def test_shared_worker_continues_after_main_close_and_cancel_is_visible(toolbox, tmp_path, monkeypatch):
    floating = toolbox.converter_window
    source = image_source(tmp_path)
    started, release = threading.Event(), threading.Event()

    def delayed(action_id, sources, folder, progress, cancel):
        started.set()
        progress(0, 1, "正在处理测试文件")
        release.wait(5)
        return {"outputs": [], "records": [{"source": sources[0], "status": "cancelled", "output": "", "error": "已停止"}],
                "errors": [], "warnings": [], "cancelled": cancel(), "total": 1}

    monkeypatch.setattr("qingjian.quick_actions.run_quick_action", delayed)
    assert toolbox.start_quick_action("image_to_jpg", [str(source)])
    until(started.is_set)
    worker = toolbox.worker
    try:
        toolbox.close()
        pump()
        assert not toolbox.isVisible() and floating.isVisible()
        assert toolbox.worker is worker and worker.isRunning()
        assert not toolbox.start_quick_action("image_to_jpg", [str(source)])
        assert toolbox.worker is worker
        floating.set_collapsed(True)
        floating.set_collapsed(False)
        assert floating.cancel_button.isVisible()
        QTest.mouseClick(floating.cancel_button, Qt.MouseButton.LeftButton)
        assert worker.isInterruptionRequested()
    finally:
        release.set()
    until(lambda: toolbox.worker is None)
    assert floating.isVisible() and not floating.running
    assert "已停止" in floating.status_label.text()


def test_paste_clipboard_file_and_invalid_mixed_types_never_spawn_an_extra_worker(toolbox, tmp_path):
    floating = toolbox.converter_window
    source = image_source(tmp_path)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    QApplication.clipboard().setMimeData(mime)
    floating.set_collapsed(True)
    floating.paste()
    assert not floating.collapsed and floating.sources == [str(source)]
    assert toolbox.worker is None
    # Arbitrary local files can still be packed as ZIP; a local unknown type
    # plus an online URL has no common archive or conversion destination.
    assert not floating.accept_sources(["https://example.invalid/article", str(tmp_path / "未知.xyz-unhandled")])
    assert not floating.ring.targets and toolbox.worker is None
    assert "没有共同" in floating.status_label.text()


def test_preview_is_not_authorization_to_use_a_new_source(toolbox, tmp_path):
    floating = toolbox.converter_window
    source = image_source(tmp_path)
    floating.preview_sources([str(source)])
    assert floating.ring.targets and not floating.sources
    assert not floating.choose_target(jpeg_target(source))
    assert toolbox.worker is None


def test_ring_pagination_exposes_every_target_without_small_hitboxes():
    ring = FormatRing()
    targets = [dict(id=f"test:{i}", title=f"目标{i}", glyph=str(i), description="测试目标") for i in range(19)]
    ring.set_targets(targets)
    seen = []
    for _ in range(3):
        seen.extend(item["id"] for item in ring.visible_targets)
        assert len(ring.buttons) == len(ring.visible_targets)
        for index, button in enumerate(ring.buttons):
            assert button.height() >= 32 and button.width() >= 64
            assert ring.target_at(button.geometry().center()) == index
        ring.turn_page(1)
    assert seen == [item["id"] for item in targets]
    ring.deleteLater()


def test_busy_close_preserves_window_and_position_is_remembered(tmp_path):
    path = tmp_path / "converter-window.json"
    floating = ConverterWindow(path)
    floating.show_window()
    floating.move(70, 80)
    floating.save_position()
    assert json.loads(path.read_text(encoding="utf-8"))["x"] == 70
    floating.set_busy(True, own_job=True)
    floating.close()
    assert floating.isVisible()
    assert "任务仍在处理" in floating.status_label.text()
    floating.set_busy(False)
    floating.set_collapsed(True)
    floating.close()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["collapsed"] is True
    restored = ConverterWindow(path)
    restored.show_window()
    assert restored.collapsed and restored.width() == 76
    assert restored.x() == data["x"] and restored.y() == data["y"]
    restored.shutdown()
    restored.deleteLater()
    floating.deleteLater()


def test_help_entry_uses_packaged_support_file_and_main_help_separates_table_tools(toolbox, tmp_path, monkeypatch):
    program = tmp_path / "程序文件"
    support = program / "support"
    support.mkdir(parents=True)
    guide = support / "转换格式说明.txt"
    guide.write_text("标题、开始时间：虚构字段说明", encoding="utf-8-sig")
    monkeypatch.setattr("qingjian.app.sys.frozen", True, raising=False)
    monkeypatch.setattr("qingjian.app.sys.executable", str(program / "青简工具箱.exe"))
    captured = []
    monkeypatch.setattr(toolbox, "_show_text_help", lambda title, text, **kwargs: captured.append((title, text)))
    toolbox.show_conversion_help()
    assert captured[-1][1] == "标题、开始时间：虚构字段说明"
    assert any(action.text() == "转换格式与字段说明" for action in toolbox.compact_menu.actions())
    toolbox.converter_window.help_button.click()
    assert captured[-1][0] == "转换格式与字段说明"
    toolbox.show_help()
    assert "旧 XLS/ODS 可先通过转换浮窗转为 XLSX" in captured[-1][1]
    assert "程序文件/support/licenses" in captured[-1][1]
    assert "不支持旧XLS" not in captured[-1][1]
