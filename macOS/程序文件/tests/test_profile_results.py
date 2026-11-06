"""Persistence boundaries and safe, usable desktop result navigation."""
import csv
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QByteArray, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QPushButton

from qingjian.profile import ProfileStore, clean_preset_options, task_counts
from qingjian.results_view import (
    MAX_REPORT_PREVIEW_SIZE, HistoryPage, PresetsPage, ReadOnlyReportBrowser,
    ResultDialog, open_result_file, safe_file_path, safe_official_report_path,
)


def _result(folder, output=None):
    output = output or folder / "通知.docx"
    return {"folder": str(folder), "records": [
        {"输入": "绝不保存的原始输入.docx", "输出": str(output), "状态": "成功",
         "提示": ["绝不保存的正文片段"], "处理统计": {"替换": 2}},
        {"输入": "另一个输入", "输出": "", "状态": "失败", "原因": "绝不保存的异常内容"},
    ], "total": 2, "cancelled": False, "fatal": "", "report_error": ""}


def test_memory_store_copies_presets_and_scrubs_credentials():
    options = {"password": "document-secret", "api_key": "api-secret", "private_plan": [{"source": "private.docx"}],
               "sources": ["input.docx"], "document_text": "read body", "template_path": "模板.pptx",
               "custom_sensitive_rules": [{"find": "客户甲", "replace": "客户A"}],
               "steps": [{"options": {"_plan": ["private"], "PDF_PASSWORD": "nested", "token": "key", "pages": "3,1"}}]}
    store = ProfileStore()
    saved = store.save_preset(" 对外材料脱敏 ", "word", options, scene="redact")
    assert saved["name"] == "对外材料脱敏"
    assert saved["options"]["custom_sensitive_rules"] == options["custom_sensitive_rules"]
    assert saved["options"]["template_path"] == "模板.pptx"
    assert "sources" not in saved["options"]
    assert "document_text" not in saved["options"]
    assert "private_plan" not in saved["options"]
    assert saved["options"]["steps"][0]["options"] == {"PDF_PASSWORD": "", "token": "", "pages": "3,1"}
    assert saved["options"]["password"] == saved["options"]["api_key"] == ""
    saved["options"]["custom_sensitive_rules"].clear()
    external = store.presets
    external[0]["name"] = "changed"
    assert store.presets[0]["name"] == "对外材料脱敏"
    assert store.presets[0]["options"]["custom_sensitive_rules"]
    assert options["password"] == "document-secret"


def test_disk_round_trip_keeps_history_metadata_but_never_input_or_body(tmp_path):
    output = tmp_path / "通知.docx"
    output.write_text("actual file", encoding="utf-8")
    report = tmp_path / "检查报告.json"
    report.write_text("{}", encoding="utf-8")
    result = _result(tmp_path, output)
    result["records"][0]["检查报告"] = [str(report), str(output)]
    store = ProfileStore(tmp_path / "profile.json")
    preset = store.save_preset("公文", "word", {"official_enabled": True}, "official")
    job = store.record_task("word", "公文规范排版", result, "official")
    assert job["status"] == "部分完成"
    assert job["outputs"] == [str(output), str(report)]
    assert (job["output_count"], job["error_count"]) == (2, 1)
    content = store.path.read_text(encoding="utf-8")
    assert "绝不保存" not in content
    assert "处理统计" not in content
    restored = ProfileStore(store.path)
    assert restored.last_error == ""
    assert restored.presets == [preset]
    assert restored.history == [job]
    restored.history[0]["outputs"].clear()
    assert len(restored.history[0]["outputs"]) == 2
    restored.clear_history()
    assert ProfileStore(store.path).history == []
    assert output.read_text(encoding="utf-8") == "actual file"
    assert report.is_file()
    assert restored.remove_preset(preset["id"])
    assert not restored.remove_preset(preset["id"])


def test_counts_report_produced_files_and_actual_exceptions():
    result = {"kind": "group", "records": [
        {"输出": "a.xlsx", "状态": "成功"}, {"输出": "a.xlsx", "状态": "成功"},
        {"输出": "", "状态": "失败"}], "output_count": 999, "error_count": 0,
        "fatal": "fatal", "report_error": "log failed"}
    assert task_counts(result) == (1, 3)
    job = ProfileStore().record_task("excel_clean", "清洗", result)
    assert job["output_count"] == 1
    assert job["status"] == "部分完成"
    result["cancelled"] = True
    assert ProfileStore().record_task("excel_clean", "清洗", result)["status"] == "已停止"
    assert ProfileStore().record_task("word", "失败", {"fatal": "error"})["status"] == "失败"


def test_fatal_exception_counts_once_when_already_recorded():
    result = {"records": [{"状态": "成功", "输出": "a.xlsx"}, {"状态": "失败", "原因": "fatal"}],
              "error_count": 0, "fatal": "fatal"}
    assert task_counts(result) == (1, 1)
    assert task_counts({"records": None, "fatal": "fatal"}) == (0, 1)
    assert task_counts({"records": {"状态": "失败"}, "error_count": "10"}) == (0, 0)


def test_store_limits_and_unknown_save_modes():
    store = ProfileStore()
    for number in range(25):
        store.save_preset(str(number), "word", {"mask_phone": True})
    for number in range(35):
        store.record_task("word", str(number), {"records": []})
    assert len(store.presets) == 20
    assert store.presets[0]["name"] == "24"
    assert len(store.history) == 30
    assert store.history[-1]["title"] == "5"
    with pytest.raises(ValueError):
        store.save_preset("bad", "execute", {})
    with pytest.raises(ValueError):
        store.save_preset("internal", "word_to_pdf", {})
    with pytest.raises(ValueError):
        store.save_preset(" ", "word", {})
    with pytest.raises(ValueError):
        clean_preset_options({"field": object()})
    with pytest.raises(ValueError):
        clean_preset_options({1: "not a string key"})
    with pytest.raises(ValueError):
        clean_preset_options({"value": float("nan")})


@pytest.mark.parametrize("content", ["not JSON", "[]", '{"schema_version": 88}', '{"schema_version": 1, "presets": {}}'])
def test_bad_store_is_read_only_empty_fallback(tmp_path, content):
    path = tmp_path / "profile.json"
    path.write_text(content, encoding="utf-8")
    store = ProfileStore(path)
    assert store.history == store.presets == []
    assert store.last_error
    assert path.read_text(encoding="utf-8") == content


def test_mixed_store_skips_malformed_and_unknown_records(tmp_path):
    store = ProfileStore()
    preset = store.save_preset("有效", "word", {"password": ""})
    job = store.record_task("undo_rename", "撤销改名", {"records": []})
    data = {"schema_version": 1,
            "presets": [None, {**preset, "mode": "execute"}, {**preset, "mode": []}, preset, preset],
            "history": [{**job, "outputs": "not a list"}, {**job, "status": []}, job, job]}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    restored = ProfileStore(path)
    assert restored.presets == [preset]
    assert restored.history == [job]
    assert restored.last_error


def test_atomic_failure_keeps_previous_file_and_current_session(tmp_path):
    store = ProfileStore(tmp_path / "profile.json")
    store.save_preset("before", "word", {})
    before = store.path.read_bytes()
    with patch("qingjian.profile.os.replace", side_effect=OSError("write denied")):
        store.save_preset("after", "word", {})
    assert store.path.read_bytes() == before
    assert store.presets[0]["name"] == "after"
    assert store.last_error
    assert not list(tmp_path.glob(".qingjian-profile-*"))


@pytest.mark.parametrize("suffix", [".exe", ".bat", ".cmd", ".py", ".js", ".html", ".svg", ".lnk", ".url", ".ps1", ".scr", ".chm", ".hta"])
def test_dangerous_results_never_reach_desktop_services(tmp_path, suffix):
    target = tmp_path / ("file" + suffix)
    target.write_text("content", encoding="utf-8")
    with patch("qingjian.results_view.QDesktopServices.openUrl") as opener:
        assert safe_file_path(str(target)) is None
        assert not open_result_file(str(target))
        opener.assert_not_called()


def test_safe_file_open_checks_existing_files_and_directory_disguises(tmp_path):
    target = tmp_path / "report.TXT"
    target.write_text("report", encoding="utf-8")
    disguised = tmp_path / "fake.docx"
    disguised.mkdir()
    with patch("qingjian.results_view.QDesktopServices.openUrl", return_value=True) as opener:
        assert open_result_file(target)
        assert opener.call_args.args[0].isLocalFile()
        assert not open_result_file(disguised)
        assert not open_result_file(tmp_path / "missing.txt")
        assert not open_result_file("https://example.com/report.txt")
        assert opener.call_count == 1


def test_result_dialog_details_retry_and_csv_are_explicit(tmp_path):
    target = tmp_path / "report.txt"
    target.write_text("result", encoding="utf-8")
    result = _result(tmp_path, target)
    result["records"][0]["输入"] = "=HYPERLINK(fake)"
    result["records"][0]["提示"] = ["第一条提示", "第二条提示"]
    dialog = ResultDialog(result, title="公文结果", retryable=True)
    retries = []
    dialog.retry_requested.connect(lambda: retries.append(True))
    with patch("qingjian.results_view.QDesktopServices.openUrl", return_value=True) as opener:
        dialog.show()
        QApplication.processEvents()
        opener.assert_not_called()
        assert "产物 1 个" in dialog.summary_label.text()
        assert "异常 1 项" in dialog.summary_label.text()
        assert "第一条提示\n第二条提示" in dialog.details.toPlainText()
        assert '"替换": 2' in dialog.details.toPlainText()
        assert dialog.open_file_button.isEnabled()
        QTest.mouseClick(dialog.open_file_button, Qt.MouseButton.LeftButton)
        assert opener.call_count == 1
        dialog.table.selectRow(1)
        QApplication.processEvents()
        assert not dialog.open_file_button.isEnabled()
        assert "绝不保存的异常内容" in dialog.details.toPlainText()
    csv_path = tmp_path / "export.csv"
    with patch("qingjian.results_view.QFileDialog.getSaveFileName", return_value=(str(csv_path), "CSV")):
        dialog.export_csv()
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows[1][0] == "'=HYPERLINK(fake)"
    assert "第一条提示\n第二条提示" == rows[1][3]
    QTest.mouseClick(dialog.retry_button, Qt.MouseButton.LeftButton)
    assert retries == [True]
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.deleteLater()


def test_history_and_presets_small_windows_keep_actions_visible(tmp_path):
    store = ProfileStore()
    preset = store.save_preset("很长的公文模板方案" * 5, "word", {}, "official")
    job = store.record_task("word", "公文规范排版" * 15, _result(tmp_path))
    for page, values, setter in [(PresetsPage(), [preset], "set_presets"), (HistoryPage(), [job], "set_history")]:
        getattr(page, setter)(values)
        page.resize(630, 460)
        page.show()
        QApplication.processEvents()
        assert page.scroll.horizontalScrollBar().maximum() == 0
        assert page.body.width() <= page.scroll.viewport().width()
        for button in page.findChildren(QPushButton):
            if button.isVisible():
                assert button.width() > 0
                assert button.mapTo(page, button.rect().topRight()).x() <= page.width()
        getattr(page, setter)([])
        QApplication.processEvents()
        assert page.scroll.horizontalScrollBar().maximum() == 0
        page.close()
        page.deleteLater()


def test_result_dialog_minimum_size_keeps_action_rows_inside(tmp_path):
    dialog = ResultDialog(_result(tmp_path), retryable=True)
    dialog.resize(600, 560)
    dialog.show()
    QApplication.processEvents()
    assert dialog.width() == 600
    assert dialog.table.geometry().bottom() < dialog.selection_hint.geometry().top()
    assert dialog.selection_hint.geometry().bottom() < dialog.details.geometry().top()
    assert dialog.details.geometry().bottom() < dialog.open_file_button.geometry().top()
    for button in dialog.findChildren(QPushButton):
        if button.isVisible():
            assert button.mapTo(dialog, button.rect().bottomRight()).x() < dialog.width()
            assert button.mapTo(dialog, button.rect().bottomRight()).y() < dialog.height()
    dialog.close()
    dialog.deleteLater()


def test_csv_suffix_does_not_silently_overwrite_another_existing_file(tmp_path):
    existing = tmp_path / "export.csv"
    existing.write_text("original file", encoding="utf-8")
    dialog = ResultDialog(_result(tmp_path))
    with patch("qingjian.results_view.QFileDialog.getSaveFileName", return_value=(str(tmp_path / "export"), "CSV")), \
         patch("qingjian.results_view.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
        dialog.export_csv()
    assert existing.read_text(encoding="utf-8") == "original file"
    dialog.close()
    dialog.deleteLater()


def test_page_actions_emit_ids_and_clear_only_when_nonempty(tmp_path):
    store = ProfileStore()
    preset = store.save_preset("方案", "word", {})
    job = store.record_task("word", "任务", _result(tmp_path))
    presets, history = PresetsPage(), HistoryPage()
    presets.set_presets([preset])
    history.set_history([job])
    values = []
    presets.preset_requested.connect(lambda value: values.append(("preset", value)))
    presets.delete_requested.connect(lambda value: values.append(("delete", value)))
    history.result_requested.connect(lambda value: values.append(("result", value)))
    history.open_folder_requested.connect(lambda value: values.append(("folder", value)))
    history.clear_requested.connect(lambda: values.append(("clear",)))
    for page in (presets, history):
        page.resize(700, 500)
        page.show()
    QApplication.processEvents()
    for page, text in [(presets, "使用方案"), (presets, "删除方案"), (history, "查看结果"), (history, "打开输出目录"), (history, "清空记录")]:
        button = next(value for value in page.findChildren(QPushButton) if value.text() == text and value.isVisible())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert values == [("preset", preset["id"]), ("delete", preset["id"]), ("result", job["id"]), ("folder", job["id"]), ("clear",)]
    history.set_history([])
    assert not history.clear_button.isEnabled()
    presets.close()
    history.close()
    presets.deleteLater()
    history.deleteLater()


def test_generated_official_html_is_previewed_only_after_explicit_click(tmp_path):
    from qingjian.core.official_reports import write_official_reports

    paths = write_official_reports(
        tmp_path / "通知原稿.docx", tmp_path / "001_通知.docx",
        {"changes": 3, "warnings": ["请核对文号"],
         "audit": {"counts": {"paragraphs": 18}, "headings": [{"text": "一、工作要求", "level": 1}]}},
        {"doc_type": "通知"}, check_only=True,
    )
    report, data = Path(paths[0]), Path(paths[1])
    (tmp_path / "处理记录.json").write_text("{}", encoding="utf-8")
    result = {"folder": str(tmp_path), "records": [
        {"输入": "通知原稿.docx", "输出": str(report), "状态": "成功", "检查报告": paths}]}
    opened = []

    def preview_exec(preview):
        browser = preview.findChild(ReadOnlyReportBrowser)
        assert preview.objectName() == "officialReportPreview"
        assert browser is not None
        assert "公文检查报告" in browser.toPlainText()
        assert "一、工作要求" in browser.toPlainText()
        assert browser.isReadOnly() and not browser.openLinks() and not browser.openExternalLinks()
        opened.append(preview.windowTitle())
        return QDialog.DialogCode.Accepted

    with patch("qingjian.results_view.QDesktopServices.openUrl", return_value=True) as external, \
         patch.object(QDialog, "exec", new=preview_exec):
        dialog = ResultDialog(result)
        dialog.show()
        QApplication.processEvents()
        assert dialog.findChildren(ReadOnlyReportBrowser) == []
        assert dialog.open_file_button.isEnabled()
        assert safe_file_path(report) is None
        assert not open_result_file(report)
        assert dialog._report_paths()[0] == str(report.resolve())
        assert str(data.resolve()) in dialog._report_paths()
        assert opened == []
        QTest.mouseClick(dialog.open_file_button, Qt.MouseButton.LeftButton)
        assert len(opened) == 1

        class SelectingMenu(QMenu):
            def exec(self, *args):
                assert self.actions()[0].text() == report.name
                self.actions()[0].trigger()

        with patch("qingjian.results_view.QMenu", SelectingMenu):
            dialog._open_report()
        assert len(opened) == 2
        external.assert_not_called()
        dialog.close()
        dialog.deleteLater()


def test_official_preview_rejects_wrong_folder_names_links_and_directories(tmp_path):
    folder = tmp_path / "task"
    folder.mkdir()
    regular = folder / "001_通知_公文排版报告.html"
    regular.write_text("<h1>公文报告</h1>", encoding="utf-8")
    outside = tmp_path / "001_通知_公文检查.html"
    outside.write_text("<p>outside</p>", encoding="utf-8")
    normal_html = folder / "普通网页.html"
    normal_html.write_text("<p>ordinary</p>", encoding="utf-8")
    directory = folder / "目录_公文检查.html"
    directory.mkdir()
    alias = folder / "链接_公文检查.html"
    os.link(outside, alias)
    dialog = ResultDialog({"folder": str(folder), "records": []})
    with patch("qingjian.results_view.QDesktopServices.openUrl") as external, \
         patch.object(QDialog, "exec") as preview_exec:
        assert safe_official_report_path(regular, folder) == regular.resolve()
        for value in (outside, normal_html, directory, alias, folder / "缺失_公文检查.html"):
            assert safe_official_report_path(value, folder) is None
            assert not dialog._preview_report(value)
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", new=lambda value: value == regular or original(value)):
            assert safe_official_report_path(regular, folder) is None
            assert not dialog._preview_report(regular)
        preview_exec.assert_not_called()
        external.assert_not_called()
    dialog.close()
    dialog.deleteLater()


def test_official_preview_rejects_oversized_and_invalid_utf8_reports(tmp_path):
    oversized = tmp_path / "超大_公文检查.html"
    oversized.write_bytes(b"x" * (MAX_REPORT_PREVIEW_SIZE + 1))
    invalid = tmp_path / "无效_公文排版报告.html"
    invalid.write_bytes(b"\xff\xfe\xfa")
    result = {"folder": str(tmp_path), "records": [{"输入": "源文件", "输出": str(oversized), "状态": "成功"}]}
    dialog = ResultDialog(result)
    assert not dialog.open_file_button.isEnabled()
    assert dialog._report_paths() == []
    with patch.object(QDialog, "exec") as preview_exec, \
         patch("qingjian.results_view.QDesktopServices.openUrl") as external:
        assert safe_official_report_path(oversized, tmp_path) is None
        assert not dialog._preview_report(oversized)
        assert not dialog._preview_report(invalid)
        preview_exec.assert_not_called()
        external.assert_not_called()
    dialog.close()
    dialog.deleteLater()


def test_read_only_report_browser_returns_no_resources_for_any_scheme(tmp_path):
    local = tmp_path / "private.txt"
    local.write_text("must not load", encoding="utf-8")
    browser = ReadOnlyReportBrowser()
    assert browser.isReadOnly() and not browser.openLinks() and not browser.openExternalLinks()
    urls = [QUrl("https://example.com/image.png"), QUrl("http://example.com/style.css"),
            QUrl.fromLocalFile(str(local)), QUrl("relative-image.png"), QUrl("data:image/png;base64,AAAA"),
            QUrl("qrc:/image.png"), QUrl("javascript:alert(1)")]
    with patch("qingjian.results_view.QDesktopServices.openUrl") as external:
        for kind in (1, 2, 3, 4, 100):
            for url in urls:
                value = browser.loadResource(kind, url)
                assert isinstance(value, QByteArray) and value.isEmpty()
                resource = browser.document().resource(kind, url)
                assert isinstance(resource, QByteArray) and resource.isEmpty()
        browser.setHtml("<p>报告正文</p><img src='https://example.com/image.png'>"
                        "<img src='" + QUrl.fromLocalFile(str(local)).toString() + "'>"
                        "<a href='https://example.com'>外部链接</a><script>alert('run')</script>")
        browser.show()
        QApplication.processEvents()
        browser.anchorClicked.emit(QUrl("https://example.com"))
        assert "报告正文" in browser.toPlainText()
        assert browser.source().isEmpty()
        external.assert_not_called()
    browser.close()
    browser.deleteLater()
