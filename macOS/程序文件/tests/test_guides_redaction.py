"""Regression checks for editable redaction and practical tool introductions."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from docx import Document
from openpyxl import load_workbook
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QTextBrowser

from qingjian.app import MODES, Window
from qingjian.batch import dispatch
from qingjian.core.word_ops import process_word
from qingjian.guides import GUIDES, build_example, guide_html
from qingjian.tools import execute_tool


_PHONE = "13800138000"
_ID = "110101199001011234"
_EMAIL = "alice@example.com"
_PRACTICE_MODES = ("excel_compare", "excel_clean", "check_materials")


def _source(path):
    document = Document()
    document.add_paragraph("关于隐私保护工作的通知")
    paragraph = document.add_paragraph("一、联系方式：")
    for piece in (_PHONE[:3], _PHONE[3:7], _PHONE[7:], "；证件：", _ID[:6], _ID[6:],
                  "；邮箱：", "alice", "@example.com", "；姓名：张", "三；公司：示例", "公司；临时标记"):
        paragraph.add_run(piece)
    document.save(path)
    return path


def _texts(path):
    return "\n".join(paragraph.text for paragraph in Document(path).paragraphs)


def _pump(app):
    for _ in range(3):
        app.processEvents()
        QTest.qWait(1)


@pytest.fixture
def toolbox_window(desktop_application):
    window = Window()
    window.switch_mode(MODES.index("word"))
    window.resize(1100, 800)
    window.show()
    _pump(desktop_application)
    try:
        yield window
    finally:
        # No test starts a worker; leave neither dummy state nor Qt windows.
        window.worker = None
        window.set_busy(False)
        window.close()
        window.deleteLater()
        _pump(desktop_application)


def test_literal_custom_replacements_cross_runs_and_preserve_original(tmp_path):
    source = _source(tmp_path / "原稿.docx")
    original = source.read_bytes()
    target = tmp_path / "处理后.docx"
    replacements = {"mask_phone_replacement": r"\1=电话\g<name>",
                    "mask_id_replacement": r"[证件]=C:\offline\1",
                    "mask_email_replacement": r"[邮箱]\n=原样保留"}
    process_word(source, target, {"mask_phone": True, "mask_id": True, "mask_email": True, **replacements})
    output = _texts(target)
    assert all(text in output for text in replacements.values())
    assert all(secret not in output for secret in (_PHONE, _ID, _EMAIL))
    assert source.read_bytes() == original
    # These values are literal text even when they resemble regex group syntax.
    assert r"\g<name>" in output and r"\n=原样保留" in output


def test_legacy_api_still_uses_partial_star_masks(tmp_path):
    source = _source(tmp_path / "旧规则.docx")
    target = tmp_path / "旧规则结果.docx"
    process_word(source, target, {"mask_phone": True, "mask_id": True, "mask_email": True,
                                  "custom_sensitive": ["示例公司"]})
    text = _texts(target)
    assert "138****8000" in text
    assert "110101********1234" in text
    assert "***@***" in text
    assert "公司：***" in text


def test_custom_sensitive_mapping_and_empty_replacement_are_literal(tmp_path):
    source = _source(tmp_path / "敏感词.docx")
    original = source.read_bytes()
    target = tmp_path / "自定义结果.docx"
    process_word(source, target, {"mask_phone": True, "mask_phone_replacement": "",
        "mask_id": True, "mask_id_replacement": "", "mask_email": True, "mask_email_replacement": "",
        "custom_sensitive_rules": [{"find": "张三", "replace": r"张先生=\1"}, {"find": "临时标记", "replace": ""}],
        "custom_sensitive": ["示例公司"], "custom_sensitive_replacement": r"某公司\g<1>=代称"})
    text = _texts(target)
    assert r"张先生=\1" in text
    assert r"某公司\g<1>=代称" in text
    assert all(value not in text for value in (_PHONE, _ID, _EMAIL, "临时标记", "张三", "示例公司"))
    assert source.read_bytes() == original


def test_new_masks_are_applied_before_official_reports(tmp_path):
    source = _source(tmp_path / "待脱敏.docx")
    original = source.read_bytes()
    target = tmp_path / "输出" / "脱敏排版.docx"
    result = dispatch("word", str(source), target, {
        "official_enabled": True, "official": {"page_numbers": "keep"},
        "mask_phone": True, "mask_phone_replacement": r"[电话]\1=已隐去",
        "mask_id": True, "mask_id_replacement": "[证件已隐去]",
        "mask_email": True, "mask_email_replacement": "[邮箱已隐去]"})
    assert source.read_bytes() == original
    assert result["reports"]
    for name in result["reports"]:
        contents = Path(name).read_text(encoding="utf-8")
        assert all(secret not in contents for secret in (_PHONE, _ID, _EMAIL))
    with ZipFile(target) as package:
        for name in package.namelist():
            if name.endswith(".xml"):
                contents = package.read(name).decode("utf-8")
                assert all(secret not in contents for secret in (_PHONE, _ID, _EMAIL)), name
    assert all(secret not in json.dumps(result, ensure_ascii=False) for secret in (_PHONE, _ID, _EMAIL))


def test_invalid_replacement_types_fail_before_publishing(tmp_path):
    source = _source(tmp_path / "原稿.docx")
    original = source.read_bytes()
    invalid = []
    for key in ("phone", "id", "email"):
        invalid.extend({f"mask_{key}": True, f"mask_{key}_replacement": value} for value in (None, 42, False, []))
    invalid.extend(({"custom_sensitive": ["张三"], "custom_sensitive_replacement": 7},
                    {"custom_sensitive_rules": [{"find": "张三", "replace": None}]},
                    {"custom_sensitive_rules": {"张三": "某人"}},
                    {"custom_sensitive_rules": [{"find": "", "replace": "某人"}]}))
    for i, options in enumerate(invalid):
        target = tmp_path / f"不可生成_{i}.docx"
        with pytest.raises(ValueError):
            process_word(source, target, options)
        assert not target.exists()
    assert source.read_bytes() == original


def test_ui_nested_rule_export_import_preserves_custom_replacements(toolbox_window, tmp_path):
    window = toolbox_window
    options = window.get_options()
    options.update({"mask_phone": True, "mask_id": True, "mask_email": True,
        "mask_phone_replacement": r"[电话]\1=原样", "mask_id_replacement": "", "mask_email_replacement": "某邮箱",
        "custom_sensitive": ["敏感词"], "custom_sensitive_replacement": r"统一\1=替换",
        "custom_sensitive_rules": [{"find": "张三", "replace": r"张先生=\g<1>"}, {"find": "临时", "replace": ""}],
        "official_enabled": True})
    options["official"].update({"operation": "check", "headings": [
        {**rule, "keywords": "=工作要求" if rule["level"] == 1 else rule["keywords"]}
        for rule in options["official"]["headings"]]})
    window.apply_word_options(options)
    assert window.get_options() == options
    path = tmp_path / "嵌套规则.json"
    with patch("qingjian.app.QFileDialog.getSaveFileName", return_value=(str(path), "JSON")):
        window.export_rules()
    exported = json.loads(path.read_text(encoding="utf-8"))
    assert exported["tool"] == "word" and exported["options"] == options
    window.mask_replacements["mask_phone_replacement"].setText("其他文字")
    window.m_custom.clear()
    with patch("qingjian.app.QFileDialog.getOpenFileName", return_value=(str(path), "JSON")):
        window.import_rules()
    assert window.get_options() == options
    assert window.get_options()["official"]["operation"] == "check"


def test_ui_custom_mapping_preserves_literal_replacement_spaces(toolbox_window):
    values = toolbox_window.get_options()
    values["custom_sensitive_rules"] = [{"find": "张三", "replace": "  张先生  "}]
    toolbox_window.apply_word_options(values)
    assert toolbox_window.get_options()["custom_sensitive_rules"] == values["custom_sensitive_rules"]


def test_invalid_ui_replacement_import_is_atomic(toolbox_window):
    window = toolbox_window
    before = copy.deepcopy(window.get_options())
    for invalid in ({"mask_phone_replacement": None}, {"custom_sensitive_rules": [{"find": "张三", "replace": []}]},
                    {"custom_sensitive_replacement": 12}):
        with pytest.raises(ValueError):
            window.apply_word_options({**before, "mask_phone": True, **invalid})
        assert window.get_options() == before


@pytest.mark.parametrize("mode", _PRACTICE_MODES)
def test_guide_contains_steps_terms_example_and_is_reachable(mode, toolbox_window, desktop_application):
    window = toolbox_window
    window.switch_mode(MODES.index(mode))
    _pump(desktop_application)
    assert window.guide_button.isVisible() and window.guide_button.isEnabled()
    assert any(GUIDES[mode]["quick"] == label.text() for label in window.extra_panels[mode].findChildren(QLabel))
    assert all(GUIDES[mode].get(key) for key in ("steps", "terms", "example"))
    markup = guide_html(mode)
    assert "怎么操作" in markup and "字段怎么填" in markup and "一个例子" in markup
    captured = {}
    def inspect_dialog(dialog):
        captured["title"] = dialog.windowTitle()
        browser = dialog.findChild(QTextBrowser)
        captured["text"] = browser.toPlainText()
        captured["buttons"] = [button.text() for button in dialog.findChildren(QPushButton)]
        return 0
    with patch.object(QDialog, "exec", inspect_dialog):
        window.guide_button.click()
    assert "使用说明" in captured["title"]
    assert GUIDES[mode]["intro"] in captured["text"]
    assert "载入本页示例" in captured["buttons"]


@pytest.mark.parametrize("mode", _PRACTICE_MODES)
def test_generated_practice_can_execute_and_exposes_expected_findings(mode, tmp_path):
    sources, options = build_example(mode, tmp_path / "练习")
    originals = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                 for source in sources for path in ([Path(source)] if Path(source).is_file() else Path(source).rglob("*")) if path.is_file()}
    result = execute_tool(mode, sources, tmp_path / "结果", options)
    assert not result["errors"]
    output = next(Path(path) for path in result["outputs"] if str(path).endswith(".xlsx"))
    book = load_workbook(output, read_only=True, data_only=True)
    try:
        if mode == "excel_compare":
            assert next(book["新增记录"].iter_rows(min_row=2, values_only=True))[0] == "0004"
            assert next(book["删除记录"].iter_rows(min_row=2, values_only=True))[0] == "0003"
            change = next(book["字段修改"].iter_rows(min_row=2, values_only=True))
            assert change[:5] == ("0001", "金额", "值修改", 100, 120)
            summary = dict(book["对账摘要"].iter_rows(min_row=2, max_col=2, values_only=True))
            assert summary["新增记录"] == summary["删除记录"] == summary["逐字段修改"] == 1
        elif mode == "excel_clean":
            rows = list(book["清洗结果"].iter_rows(values_only=True))
            head = rows[0]
            assert len(rows) == 3
            first, second = dict(zip(head, rows[1])), dict(zip(head, rows[2]))
            assert first["编号"] == "0001" and first["姓名"] == "张三" and first["金额"] == 1200.5
            assert second["金额"] == "待补充"
            issues = "\n".join(" ".join(str(value or "") for value in row) for row in book["异常记录"].iter_rows(values_only=True))
            assert "重复" in issues and "姓名" in issues and "金额" in issues and "入职日期" in issues
        else:
            missing = list(book["文件缺漏"].iter_rows(min_row=2, values_only=True))
            complete = [row for row in missing if "项目甲" in row[0]]
            incomplete = [row for row in missing if "项目乙" in row[0]]
            assert complete and all(row[2] == "齐全" for row in complete)
            assert {row[1] for row in incomplete if row[2] == "缺漏"} == {"通知.txt", "附件/*.docx"}
            required = list(book["必填字段异常"].iter_rows(min_row=2, values_only=True))
            assert any("项目乙" in row[0] and row[4] == "部门" and row[5] == "必填字段为空" for row in required)
    finally:
        book.close()
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in originals.items())


@pytest.mark.parametrize("mode,expected_count", [("excel_compare", 2), ("excel_clean", 1), ("check_materials", 2)])
def test_load_example_replaces_current_queue_and_options_without_starting(mode, expected_count, toolbox_window, tmp_path):
    window = toolbox_window
    window.switch_mode(MODES.index(mode))
    window.output_edit.setText(str(tmp_path / "结果"))
    old_sources, _ = build_example(mode, tmp_path / "旧练习")
    window.accept_sources(old_sources)
    old = list(window.files[mode])
    with patch.object(window, "start_task") as start:
        assert window.load_tool_example(mode) is True
        start.assert_not_called()
    assert len(window.files[mode]) == expected_count
    assert window.files[mode] != old and set(window.files[mode]).isdisjoint(old)
    assert all(Path(path).exists() for path in window.files[mode])
    assert window.worker is None
    settings = window.get_options()
    if mode == "excel_compare":
        assert settings["key_columns"] == ["编号"]
    elif mode == "excel_clean":
        assert settings["number_columns"] == ["金额"] and settings["text_columns"] == ["编号"]
    else:
        assert settings["required_files"] == ["通知.txt", "名单.xlsx", "附件/*.docx"]


def test_busy_or_wrong_mode_refuses_practice_without_mutation(toolbox_window, tmp_path):
    window = toolbox_window
    window.switch_mode(MODES.index("excel_compare"))
    window.output_edit.setText(str(tmp_path / "不应新建"))
    source, _ = build_example("excel_compare", tmp_path / "当前练习")
    window.accept_sources(source)
    before = (copy.deepcopy(window.files), copy.deepcopy(window.queue_states), window.get_options(), window.status.text())
    with patch("qingjian.app.build_example") as build:
        window.set_busy(True)
        assert window.load_tool_example("excel_compare") is False
        window.set_busy(False)
        window.worker = object()
        assert window.load_tool_example("excel_compare") is False
        window.worker = None
        assert window.load_tool_example("excel_clean") is False
        build.assert_not_called()
    assert (window.files, window.queue_states, window.get_options(), window.status.text()) == before
    assert not (tmp_path / "不应新建").exists()
