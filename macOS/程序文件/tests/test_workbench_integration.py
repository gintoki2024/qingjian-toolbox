"""Exercise workbench routing against the actual processing window."""
import copy
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian.app import MODES, Window
from qingjian.workbench import TOOL_GROUPS, recommend_actions


@pytest.fixture
def window(tmp_path):
    value = Window(start_home=True, data_path=tmp_path / "profile.json")
    value.show()
    QApplication.processEvents()
    yield value
    value.set_busy(False)
    value.close()
    value.deleteLater()
    QApplication.processEvents()


def test_home_group_routes_clear_search_and_keep_all_tools_available(window):
    # Home groups navigate to their full catalog, never start a task.
    window.resize(1280, 860)
    QApplication.processEvents()
    assert window.content_stack.currentWidget() is window.home_page
    assert set(window.home_page.group_cards) == {group["id"] for group in TOOL_GROUPS}
    assert not hasattr(window.home_page, "quick_panel")
    assert not window.nav_scroll.isVisible()
    for group in TOOL_GROUPS:
        window.catalog_page.search_edit.setText("nonmatching stale query")
        QTest.mouseClick(window.home_page.group_cards[group["id"]], Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        assert window.content_stack.currentWidget() is window.catalog_page
        assert window.catalog_page.current_group == group["id"]
        assert window.catalog_page.search_edit.text() == ""
        assert set(window.catalog_page.visible_modes) == set(group["modes"])
        assert window.worker is None
        window.catalog_page.tool_cards[group["modes"][0]].click()
        assert window.mode == group["modes"][0]
        assert window.content_stack.currentWidget() is window.job_page
        window.show_surface("home")


def test_scenes_keep_independent_word_settings_and_leave_other_pages_intact(window):
    window.open_scene("official")
    window.official_panel.controls["body_font"].setCurrentText("仿宋")
    window.official_panel.controls["body_size"].setValue(17)
    window.open_scene("redact")
    assert not window.official_panel.enabled.isChecked()
    assert not window.f_enabled.isChecked()
    window.m_custom.setPlainText("张三=张先生")
    window.mask_replacements["mask_phone_replacement"].setText("号码已隐藏")
    window.switch_mode(MODES.index("excel_compare"))
    window.open_scene("official")
    assert window.official_panel.enabled.isChecked()
    assert window.official_panel.controls["body_size"].value() == 17
    assert not window.m_phone.isChecked()
    assert window.m_custom.toPlainText() == ""
    window.open_scene("redact")
    assert window.m_custom.toPlainText() == "张三=张先生"
    assert window.mask_replacements["mask_phone_replacement"].text() == "号码已隐藏"


def test_source_routes_configure_template_and_export_without_starting(window, tmp_path):
    template = tmp_path / "模板.pptx"
    template.write_bytes(b"routing only")
    window.files["template_fill"] = ["old-data.xlsx"]
    action = recommend_actions([str(template)])[0]
    assert window.apply_source_action(action)
    assert window.mode == "template_fill"
    assert window.files["template_fill"] == []
    assert window.extra_panels["template_fill"].options()["template_path"] == str(template)
    assert window.worker is None
    text = tmp_path / "资料.txt"
    text.write_text("文字", encoding="utf-8")
    window.extra_panels["document_export"].set_options({"action": "images"})
    export = next(item for item in recommend_actions([str(text)]) if item["id"] == "text_export")
    window.apply_source_action(export)
    assert window.extra_panels["document_export"].options()["action"] == "text"
    assert window.worker is None
    window.open_scene("official")
    replace = next(item for item in recommend_actions([str(text)]) if item["id"] == "replace")
    window.apply_source_action(replace)
    assert window.word_tabs.tabText(window.word_tabs.currentIndex()) == "查找替换"
    assert not window.official_panel.enabled.isChecked()
    assert not window.m_phone.isChecked()
    assert window.worker is None


def test_home_source_action_replaces_previous_queue(window, tmp_path):
    first, second = tmp_path / "上次.txt", tmp_path / "本次.txt"
    first.write_text("old", encoding="utf-8")
    second.write_text("new", encoding="utf-8")
    window.files["word"] = [str(first)]
    action = next(item for item in recommend_actions([str(second)]) if item["id"] == "redact")
    assert window.apply_source_action(action)
    assert window.files["word"] == [str(second.resolve())]
    assert str(first) not in window.source_summary.toolTip()


def test_loading_another_word_preset_preserves_current_scene_draft(window):
    window.open_scene("redact")
    options = copy.deepcopy(window.get_options())
    preset = window.profile_store.save_preset("共享用脱敏", "word", options, "redact")
    window.open_scene("official")
    window.official_panel.controls["body_size"].setValue(19)
    assert window.open_preset(preset["id"])
    assert not window.official_panel.enabled.isChecked()
    assert window.open_scene("official")
    assert window.official_panel.controls["body_size"].value() == 19


def test_busy_locks_all_navigation_and_queued_routes(window, tmp_path):
    window.open_scene("official")
    original = window.mode
    window.set_busy(True)
    assert not window.home_page.group_section.isEnabled()
    assert window.home_page.personal_panel.isEnabled()
    for name in window.surfaces:
        window.show_surface(name)
    window.open_scene("materials")
    window.open_tool_group("table")
    window.switch_mode(MODES.index("image"))
    window.focus_tool_search()
    assert not window.apply_source_action({"mode": "image", "files": []})
    assert window.mode == original
    assert window.content_stack.currentWidget() is window.job_page
    assert all(not button.isEnabled() for button in window.surface_buttons.values())
    assert not window.preset_button.isEnabled()


def test_materials_demo_prepares_both_cases_and_never_runs_automatically(window, tmp_path):
    window.output_edit.setText(str(tmp_path / "practice"))
    assert window.load_scene_example("materials")
    assert window.mode == "check_materials"
    assert len(window.files["check_materials"]) == 2
    sources = [Path(value) for value in window.files["check_materials"]]
    assert all(value.is_dir() for value in sources)
    assert (sources[0] / "通知.txt").is_file()
    assert not (sources[1] / "通知.txt").exists()
    assert window.extra_panels["check_materials"].options()["required_files"] == ["通知.txt", "名单.xlsx", "附件/*.docx"]
    assert window.worker is None


@pytest.mark.parametrize("scene", ["official", "materials", "redact", "compare", "template", "convert"])
def test_prioritized_jobs_keep_primary_action_inside_small_window(window, scene):
    window.resize(980, 680)
    window.open_scene(scene)
    QApplication.processEvents()
    for widget in (window.start_button, window.choose_output_button, window.result_button):
        bounds = QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())
        assert window.rect().contains(bounds)
    assert window.workspace_scroll.horizontalScrollBar().maximum() == 0


def test_real_text_job_records_history_and_reopens_with_saved_rules(window, tmp_path):
    source = tmp_path / "材料.txt"
    source.write_text("张三 13800138000", encoding="utf-8")
    profile_path = tmp_path / "local" / "profile.json"
    from qingjian.profile import ProfileStore
    window.profile_store = ProfileStore(profile_path)
    window.output_edit.setText(str(tmp_path / "out"))
    window.open_scene("redact")
    window.m_custom.setPlainText("张三=张先生")
    window.mask_replacements["mask_phone_replacement"].setText("联系电话已隐去")
    with patch("qingjian.app.QInputDialog.getText", return_value=("对外共享", True)):
        window.save_current_preset()
    window.accept_sources([str(source)])
    window.start_task()
    end = time.monotonic() + 15
    while window.worker is not None and time.monotonic() < end:
        QApplication.processEvents()
        QTest.qWait(10)
    assert window.worker is None
    assert window.last_result["records"][0]["状态"] == "成功"
    output = Path(window.last_result["records"][0]["输出"])
    assert output.read_text(encoding="utf-8") == "张先生 联系电话已隐去"
    saved = ProfileStore(profile_path)
    assert len(saved.history) == len(saved.presets) == 1
    assert saved.history[0]["scene"] == "redact"
    assert saved.history[0]["output_count"] == 1
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    assert "records" not in payload["history"][0]
    assert str(source) not in json.dumps(payload, ensure_ascii=False)
    window.m_custom.clear()
    assert window.open_preset(saved.presets[0]["id"])
    assert window.m_custom.toPlainText() == "张三=张先生"
    with patch("qingjian.app.ResultDialog.exec", return_value=0) as show:
        window.show_history_result(saved.history[0]["id"])
    show.assert_called_once()


def test_retry_only_failed_sources_and_uses_original_options_snapshot(window):
    window.open_scene("redact")
    original = copy.deepcopy(window.get_options())
    window.last_context = {"mode": "word", "options": original, "files": ["a.txt", "b.txt"], "scene": "redact", "title": "脱敏"}
    window.last_result = {"records": [
        {"输入": "a.txt", "状态": "成功", "输出": "out.txt"},
        {"输入": "b.txt", "状态": "失败", "输出": "", "原因": "busy"},
        {"输入": "unknown.txt", "状态": "失败", "输出": ""},
    ]}
    window.mask_replacements["mask_phone_replacement"].setText("刚才新改的设置")
    window.switch_mode(MODES.index("image"))
    with patch.object(window, "start_task") as start:
        assert window.retry_failed()
    start.assert_called_once()
    assert window.files["word"] == ["b.txt"]
    assert window.get_options() == original
    assert window.worker is None

