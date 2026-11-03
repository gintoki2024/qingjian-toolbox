"""High-scaling screens may leave only 268 logical pixels for a third-width UI."""
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from qingjian.app import Window, MODES


def pump():
    for _ in range(4):
        QApplication.processEvents()


@pytest.mark.parametrize("width", [268, 325])
def test_all_tool_actions_stay_horizontally_reachable_on_scaled_screens(width, tmp_path):
    window = Window(data_path=tmp_path / "profile.json")
    window.show()
    pump()
    try:
        for wanted in (410, width, 410, width):
            window.resize(wanted, 573)
            pump()
        assert window.width() == width
        for index, mode in enumerate(MODES):
            window.switch_mode(index)
            pump()
            for expanded in (False, True):
                window.files[mode] = [str(tmp_path / "仅用于队列显示.docx")]
                window.refresh_table()
                window.manage_button.setChecked(expanded)
                pump()
                assert window.workspace_scroll.horizontalScrollBar().maximum() == 0, (mode, expanded)
                assert window.source_bar.width() <= window.workspace_scroll.viewport().width(), (mode, expanded)
                for control in (window.manage_button, window.settings_more, window.start_button,
                                window.result_button, window.choose_output_button, window.compact_notebook):
                    assert control.isVisible(), mode
                    x = control.mapTo(window, QPoint()).x()
                    assert 0 <= x and x + control.width() <= window.width(), (mode, control.text(), x, control.width())
    finally:
        window.close()
        window.deleteLater()
        pump()
