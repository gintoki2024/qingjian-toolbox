"""End-to-end checks for official formatting, reports and desktop controls."""
from __future__ import annotations

import copy
import json
import os
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches
from PIL import Image
from PySide6.QtCore import QPoint, QRect
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLineEdit

from qingjian.app import MODES, Window
from qingjian.batch import dispatch
from qingjian.core.common import safe_name
from qingjian.core.official_reports import save_official_template


def _source(path: Path, *, phone="", picture=False, without_normal=False):
    document = Document()
    document.add_paragraph("关于做好有关工作的通知")
    document.add_paragraph("一、工作安排" + ("，联系人" + phone if phone else ""))
    document.add_paragraph("请各部门认真落实有关工作。" + ("联系电话：" + phone if phone else ""))
    document.add_paragraph("（一）落实责任")
    document.add_paragraph("请按时完成资料核对。")
    if picture:
        image = BytesIO()
        Image.new("RGB", (40, 20), "#6374d8").save(image, format="PNG")
        image.seek(0)
        document.add_paragraph().add_run().add_picture(image, width=Inches(0.5))
    if without_normal:
        normal = document.styles["Normal"]
        normal._element.getparent().remove(normal._element)
    document.save(path)
    return path


def _report(result):
    paths = [Path(value) for value in result["reports"]]
    assert {value.suffix.lower() for value in paths} == {".json", ".html"}
    assert all(value.is_file() for value in paths)
    return json.loads(next(value for value in paths if value.suffix == ".json").read_text(encoding="utf-8"))


def _pump(app):
    for _ in range(4):
        app.processEvents()
        QTest.qWait(2)


@pytest.fixture
def official_window(desktop_application):
    window = Window()
    window.switch_mode(MODES.index("word"))
    window.show()
    _pump(desktop_application)
    try:
        yield window
    finally:
        if window.worker is not None:
            window.worker.requestInterruption()
            window.worker.wait(5000)
            _pump(desktop_application)
        window.set_busy(False)
        window.close()
        window.deleteLater()
        _pump(desktop_application)


def test_dispatch_formats_official_document_and_preserves_original(tmp_path):
    source = _source(tmp_path / "原稿.docx")
    original = source.read_bytes()
    target = tmp_path / "results" / "排版.docx"
    result = dispatch("word", str(source), target, {
        "official_enabled": True,
        "official": {"title_size": 22, "body_size": 16, "body_font": "仿宋", "line_pt": 28, "top_cm": 3.7},
        # The official layout must own formatting even when the ordinary
        # format page still has its checked state and different values.
        "format_enabled": True, "font_name": "宋体", "font_size": 9,
        "line_spacing": 1.0, "margin_cm": 1.0,
    })
    assert source.read_bytes() == original
    assert Path(result["primary_output"]) == target
    output = Document(target)
    assert [paragraph.text for paragraph in output.paragraphs] == [paragraph.text for paragraph in Document(source).paragraphs]
    body = next(paragraph for paragraph in output.paragraphs if paragraph.text.startswith("请各部门"))
    assert all(run.font.size is not None and run.font.size.pt == 16 for run in body.runs if run.text)
    assert all(run._r.rPr.rFonts.get(qn("w:eastAsia")) == "仿宋" for run in body.runs if run.text)
    assert body.paragraph_format.line_spacing.pt == 28
    assert output.sections[0].top_margin.cm == pytest.approx(3.7, abs=0.005)
    report = _report(result)
    assert report["输出文档"] == str(target)
    assert report["仅检查"] is False
    assert result["official_summary"] == report["检查结果"].get("counts", {})
    assert result["official_summary"]
    assert "audit" not in result


def test_redaction_runs_before_heading_audit_and_local_reports(tmp_path):
    phone = "13800138000"
    source = _source(tmp_path / "含敏感信息原稿.docx", phone=phone)
    original = source.read_bytes()
    target = tmp_path / "results" / "脱敏排版.docx"
    result = dispatch("word", str(source), target, {
        "official_enabled": True, "official": {}, "mask_phone": True,
    })
    assert source.read_bytes() == original
    with ZipFile(target) as package:
        for name in package.namelist():
            if name.endswith(".xml"):
                assert phone not in package.read(name).decode("utf-8"), name
    for filename in result["reports"]:
        assert phone not in Path(filename).read_text(encoding="utf-8")
    assert phone not in json.dumps(result, ensure_ascii=False)
    assert "138****8000" in "".join(paragraph.text for paragraph in Document(target).paragraphs)
    assert _report(result)["检查结果"]


def test_check_dispatch_creates_reports_without_a_docx_or_source_changes(tmp_path):
    source = _source(tmp_path / "仅检查原稿.docx")
    original = source.read_bytes()
    folder = tmp_path / "reports"
    target = folder / "不应生成.docx"
    result = dispatch("word", str(source), target, {
        "official_enabled": True, "official": {"operation": "check"},
        "mask_phone": True, "format_enabled": True,
    })
    assert source.read_bytes() == original
    assert not target.exists()
    assert not list(folder.glob("*.docx"))
    assert Path(result["primary_output"]).suffix == ".html"
    assert result["primary_output"] in result["reports"]
    report = _report(result)
    assert report["仅检查"] is True
    assert not report["输出文档"]
    assert report["检查结果"]
    assert result["official_summary"] == report["检查结果"].get("counts", {})


def test_formatting_handles_missing_normal_and_preserves_picture_only_paragraph(tmp_path):
    source = _source(tmp_path / "缺Normal的图文模板.docx", picture=True, without_normal=True)
    original = source.read_bytes()
    assert "Normal" not in Document(source).styles
    target = tmp_path / "results" / "保留图片.docx"
    result = dispatch("word", str(source), target, {
        "official_enabled": True,
        "official": {"remove_blank_paragraphs": True, "remove_extra_spaces": True},
    })
    assert source.read_bytes() == original
    output = Document(target)
    assert len(output.inline_shapes) >= 1
    assert any(paragraph.text.startswith("请各部门") for paragraph in output.paragraphs)
    assert _report(result)["检查结果"]


def test_nested_official_rules_export_import_and_invalid_import_are_atomic(official_window, tmp_path):
    window = official_window
    values = window.get_options()
    values.update({"official_enabled": True, "mask_phone": True, "font_size": 14})
    values["official"].update({"title_font": "方正小标宋简体", "body_size": 15,
                               "custom_terms": "布署=部署\n帐号=账号", "page_numbers": "center"})
    values["official"]["headings"][1]["keywords"] = "重点工作|执行安排"
    window.apply_word_options(values)
    expected = copy.deepcopy(window.get_options())
    rules = tmp_path / "公文规则.json"
    with patch("qingjian.app.QFileDialog.getSaveFileName", return_value=(str(rules), "JSON")), \
            patch("qingjian.app.QMessageBox.warning") as warning:
        window.export_rules()
        warning.assert_not_called()
    exported = json.loads(rules.read_text(encoding="utf-8"))
    assert exported["tool"] == "word"
    assert exported["options"]["official"] == expected["official"]
    window.official_panel.enabled.setChecked(False)
    window.official_panel.controls["body_size"].setValue(12)
    with patch("qingjian.app.QFileDialog.getOpenFileName", return_value=(str(rules), "JSON")), \
            patch("qingjian.app.QMessageBox.warning") as warning:
        window.import_rules()
        warning.assert_not_called()
    assert window.get_options() == expected

    invalid = copy.deepcopy(exported)
    invalid["options"]["font_size"] = 30
    invalid["options"]["mask_phone"] = False
    invalid["options"]["official"]["body_size"] = "无效数字"
    rules.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
    with patch("qingjian.app.QFileDialog.getOpenFileName", return_value=(str(rules), "JSON")), \
            patch("qingjian.app.QMessageBox.warning") as warning:
        window.import_rules()
        warning.assert_called_once()
    assert window.get_options() == expected


def test_official_enablement_disables_conflicting_general_format_controls(official_window):
    window = official_window
    general_controls = (window.f_enabled, window.f_font, window.f_size,
                        window.f_line, window.f_margin, window.f_headings)
    assert all(control.isEnabled() for control in general_controls)
    window.official_panel.enabled.setChecked(True)
    assert all(not control.isEnabled() for control in general_controls)
    assert window.official_panel.controls["body_font"].isEnabled()
    assert window.m_phone.isEnabled()
    window.official_panel.enabled.setChecked(False)
    assert all(control.isEnabled() for control in general_controls)


@pytest.mark.parametrize("width,height", [(1050, 760), (980, 680)])
def test_official_subpages_keep_bottom_actions_visible(official_window, desktop_application, width, height):
    window = official_window
    window.resize(width, height)
    official_index = next(index for index in range(window.word_tabs.count())
                          if window.word_tabs.tabText(index) == "公文排版")
    window.word_tabs.setCurrentIndex(official_index)
    for index in range(window.official_panel.tabs.count()):
        window.official_panel.tabs.setCurrentIndex(index)
        _pump(desktop_application)
        context = (width, height, window.official_panel.tabs.tabText(index))
        assert window.width() <= width and window.height() <= height, context
        for control in (window.start_button, window.output_edit, window.choose_output_button, window.progress):
            assert control.isVisible(), context
            bounds = QRect(control.mapTo(window, QPoint(0, 0)), control.size())
            assert window.rect().contains(bounds), (context, bounds)
        workspace_bottom = window.workspace_scroll.mapTo(window, QPoint(0, window.workspace_scroll.height())).y()
        assert window.output_edit.mapTo(window, QPoint(0, 0)).y() > workspace_bottom, context
        assert window.workspace_scroll.horizontalScrollBar().maximum() == 0, context


def test_check_button_runs_real_batch_and_records_report_as_primary_output(official_window, desktop_application, tmp_path):
    window = official_window
    source = _source(tmp_path / "按钮检查原稿.docx")
    original = source.read_bytes()
    window.accept_sources([str(source)])
    window.output_edit.setText(str(tmp_path / "results"))
    assert not window.official_panel.enabled.isChecked()
    window.official_panel.preflight_button.click()
    deadline = time.monotonic() + 15
    while window.worker is not None and time.monotonic() < deadline:
        desktop_application.processEvents()
        QTest.qWait(10)
    assert window.worker is None, "official inspection worker did not finish"
    assert source.read_bytes() == original
    result = window.last_result
    assert not result["fatal"]
    assert len(result["records"]) == 1
    record = result["records"][0]
    assert record["状态"] == "成功", record
    assert Path(record["输出"]).suffix == ".html"
    assert Path(record["输出"]).is_file()
    assert not list(Path(result["folder"]).glob("*.docx"))
    batch_report = json.loads((Path(result["folder"]) / "处理记录.json").read_text(encoding="utf-8"))
    assert batch_report["结果"][0]["输出"] == record["输出"]
    assert window.result_button.isEnabled()


def test_template_ui_uses_safe_title_name_and_helper_refuses_overwrite(official_window, tmp_path):
    window = official_window
    folder = tmp_path / "templates"
    folder.mkdir()
    title = "关于/数据:处理*的通知?"

    def accept_filled_dialog(dialog):
        fields = dialog.findChildren(QLineEdit)
        assert len(fields) == 3
        fields[0].setText(title)
        fields[1].setText("利安人寿保险股份有限公司")
        fields[2].setText("2026年10月29日")
        return QDialog.DialogCode.Accepted

    with patch("qingjian.app.QDialog.exec", accept_filled_dialog), \
            patch("qingjian.app.new_run_directory", return_value=folder), \
            patch("qingjian.app.QMessageBox.warning") as warning:
        window.generate_official_template()
        warning.assert_not_called()
    target = Path(window.last_result["records"][0]["输出"])
    assert target == folder / (safe_name(title, limit=160) + ".docx")
    assert target.parent == folder
    assert not any(character in target.name for character in '<>:"/\\|?*')
    assert any(paragraph.text == title for paragraph in Document(target).paragraphs)
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        save_official_template(target, window.official_panel.options(), "新的标题", "其他单位", "2026年10月30日", "通知")
    assert target.read_bytes() == original
    assert not list(folder.glob(".qingjian-write-*"))
