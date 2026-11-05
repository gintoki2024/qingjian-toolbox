import json

import pytest
from PySide6.QtWidgets import QDialog, QWidget

from qingjian.window_appearance import AppearanceController, AppearanceDialog, opacity_value


def test_live_opacity_applies_to_existing_and_later_windows(desktop_application, tmp_path):
    controller = AppearanceController(tmp_path / "appearance.json")
    main, floating = QWidget(), QWidget()
    controller.register(main)
    main.show()
    controller.update_settings(opacity=.73, snap_enabled=False)
    floating.show()
    desktop_application.processEvents()
    assert main.windowOpacity() == pytest.approx(.73, abs=.005)
    assert floating.windowOpacity() == pytest.approx(.73, abs=.005)
    controller.flush()
    data = json.loads((tmp_path / "appearance.json").read_text(encoding="utf-8"))
    assert data == {"schema_version": 1, "opacity": .73, "snap_enabled": False}
    controller.shutdown()
    main.close()
    floating.close()
    restored = AppearanceController(tmp_path / "appearance.json")
    assert restored.settings == {"opacity": .73, "snap_enabled": False}
    restored.shutdown()


def test_ui_restores_defaults_and_flushes_on_close(desktop_application, tmp_path):
    controller = AppearanceController(tmp_path / "appearance.json")
    dialog = AppearanceDialog(controller)
    dialog.show()
    dialog.opacity_slider.setValue(61)
    dialog.snap_check.setChecked(False)
    assert controller.opacity == .61
    assert not controller.snap_enabled
    assert "61%" in dialog.value_label.text()
    controller.update_settings(opacity=1, snap_enabled=True)
    assert dialog.opacity_slider.value() == 100 and dialog.snap_check.isChecked()
    dialog.accept()
    assert json.loads((tmp_path / "appearance.json").read_text(encoding="utf-8"))["opacity"] == 1
    controller.shutdown()


def test_invalid_persisted_values_never_hide_controls(desktop_application, tmp_path):
    path = tmp_path / "appearance.json"
    path.write_text('{"schema_version":1,"opacity":-10,"snap_enabled":"false"}', encoding="utf-8")
    controller = AppearanceController(path)
    assert controller.opacity == .5 and controller.snap_enabled is True
    controller.shutdown()
    for value in (float("nan"), float("inf"), True, None, "0.2"):
        assert opacity_value(value) == 1
    path.write_text("broken", encoding="utf-8")
    controller = AppearanceController(path)
    assert controller.opacity == 1 and controller.last_error
    controller.shutdown()


def test_save_failure_keeps_session_usable(desktop_application, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("occupied", encoding="utf-8")
    controller = AppearanceController(blocked / "appearance.json")
    controller.update_settings(opacity=.8)
    assert controller.flush() is False
    assert controller.opacity == .8 and controller.last_error
    controller.shutdown()
