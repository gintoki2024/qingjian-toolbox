"""Behavior introduced by the merged UI, using generated local fixtures."""
import time

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian.converter_ui import ConverterWindow, FormatRing
from qingjian.home_content_ui import PersonalHomePanel


def settle():
    for _ in range(8):
        QApplication.processEvents()
        QTest.qWait(4)


def test_quote_font_and_content_height_follow_final_viewport_size(tmp_path):
    materials = tmp_path / "素材"
    materials.mkdir()
    (materials / "文案.txt").write_text("把事情做完，也给自己留一点空白。", encoding="utf-8")
    panel = PersonalHomePanel(tmp_path / "settings.json")
    try:
        panel.resize(250, 300)
        panel.show()
        panel.apply_settings({"folder": str(materials), "display_mode": "quotes"})
        deadline = time.monotonic() + 5
        while (panel._jobs or not panel.snapshot.quotes) and time.monotonic() < deadline:
            settle()
        assert panel.snapshot.quotes
        settle()
        small_font, small_height = panel._quote_px, panel.quote_box.height()
        small_panel_height = panel.height()
        for width, height in ((340, 420), (510, 540), (720, 660)):
            panel.resize(width, height)
        settle()
        assert panel._quote_px > small_font
        growth = panel.quote_box.height() - small_height
        assert growth > 0
        assert abs(growth - (panel.height() - small_panel_height)) <= 4
        assert panel.quote_scroll.verticalScrollBar().maximum() == 0
        assert panel.quote_scroll.horizontalScrollBar().maximum() == 0
        assert not panel._quote_fit_timer.isActive()
    finally:
        panel.close()
        panel.deleteLater()
        settle()


def test_whole_ring_sector_click_and_hover_select_the_displayed_target():
    ring = FormatRing()
    ring.set_targets([{"id": f"format-{index}", "glyph": str(index)} for index in range(8)])
    selected = []
    ring.target_clicked.connect(selected.append)
    try:
        ring.show()
        settle()
        # Top sector background above the label, rather than the label button.
        position = QPoint(122, 14)
        assert not ring.buttons[0].geometry().contains(position)
        QTest.mouseMove(ring, position)
        settle()
        assert ring.buttons[0].property("active") is True
        QTest.mouseClick(ring, Qt.LeftButton, pos=position)
        assert selected == ["format-0"]
        ring.setEnabled(False)
        QTest.mouseClick(ring, Qt.LeftButton, pos=position)
        assert selected == ["format-0"]
    finally:
        ring.close()
        ring.deleteLater()
        settle()


def test_conversion_window_bottom_controls_fit_with_pagination_and_progress(tmp_path):
    image = QImage(40, 30, QImage.Format_RGB32)
    image.fill(Qt.white)
    source = tmp_path / "多种目标.png"
    assert image.save(str(source))
    window = ConverterWindow(tmp_path / "converter.json")
    try:
        window.show_window(collapsed=False)
        window.accept_sources([str(source)])
        window.begin(window.ring.targets[0]["id"], 1)
        settle()
        assert len(window.ring.targets) > 8
        assert window.minimumSizeHint().height() <= window.height()
        for control in (window.previous_button, window.next_button, window.progress,
                        window.cancel_button, window.save_button):
            assert control.isVisible()
            rect = QRect(control.mapTo(window, QPoint()), control.size())
            assert window.rect().contains(rect), control.objectName()
        window.set_busy(False)
        settle()
        for control in (window.results_button, window.folder_button, window.save_button):
            assert control.isVisible()
            assert window.rect().contains(QRect(control.mapTo(window, QPoint()), control.size()))
    finally:
        window.set_busy(False)
        window.shutdown()
        window.close()
        window.deleteLater()
        settle()
