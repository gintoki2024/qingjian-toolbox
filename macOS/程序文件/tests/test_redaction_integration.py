"""Exercise rule memory through real widgets, restarts and the batch worker."""
import copy
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from docx import Document
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from qingjian.app import Window
from qingjian.redaction_library import RedactionLibraryDialog
from qingjian.redaction_memory import RedactionMemoryStore


@pytest.fixture
def windows(tmp_path):
    created = []
    def make():
        window = Window(start_home=True, data_path=tmp_path / "data" / "profile.json")
        window.show()
        QApplication.processEvents()
        created.append(window)
        return window
    yield make
    for window in created:
        window.set_busy(False)
        window.close()
        window.deleteLater()
    QApplication.processEvents()


def test_remember_button_persists_and_restart_restores_only_redaction(windows):
    first = windows()
    first.open_scene("redact")
    rules = [{"find": "姓名=张三", "replace": r"  某先生=\1  "},
             {"find": "内部标记\n第二行", "replace": ""}]
    first.m_custom.set_rules(rules)
    with patch("qingjian.app.QInputDialog.getText", return_value=("对外共享", True)):
        QTest.mouseClick(first.redaction_remember_button, Qt.MouseButton.LeftButton)
    saved = RedactionMemoryStore(first.redaction_store.path)
    assert saved.groups[0]["rules"] == rules
    assert saved.restore_id == saved.groups[0]["id"]
    first.m_custom.table.item(0, 1).setText("未保存的临时修改")
    second = windows()
    assert second.get_options()["custom_sensitive_rules"] == []
    assert second.open_scene("official")
    assert second.get_options()["custom_sensitive_rules"] == []
    assert second.open_scene("redact")
    assert second.get_options()["custom_sensitive_rules"] == rules
    second.redaction_store.set_restore_group("")
    third = windows()
    third.open_scene("redact")
    assert third.get_options()["custom_sensitive_rules"] == []
    assert RedactionMemoryStore(first.redaction_store.path).groups[0]["rules"] == rules


def test_memory_append_deduplicates_and_preserves_all_other_settings(windows):
    window = windows()
    window.open_scene("redact")
    window.mask_replacements["mask_phone_replacement"].setText("[联系电话]")
    original = {"find": "张三", "replace": "员工甲"}
    window.m_custom.set_rules([original])
    before = copy.deepcopy(window.get_options())
    assert window.apply_redaction_memory_rules([original, {"find": "某某公司", "replace": "某公司"}], True)
    after = window.get_options()
    assert len(after.pop("custom_sensitive_rules")) == 2
    before.pop("custom_sensitive_rules")
    assert after == before
    assert window.apply_redaction_memory_rules([{"find": "部门", "replace": "某部门"}])
    after = window.get_options()
    assert after.pop("custom_sensitive_rules") == [{"find": "部门", "replace": "某部门"}]
    assert after == before
    window.set_busy(True)
    with patch("qingjian.app.QInputDialog.getText") as prompt:
        window.remember_redaction_rules()
    prompt.assert_not_called()
    assert not window.apply_redaction_memory_rules([original])
    assert window.get_options()["custom_sensitive_rules"] == [{"find": "部门", "replace": "某部门"}]


def test_library_search_update_rename_default_and_delete_are_durable(windows):
    window = windows()
    window.open_scene("redact")
    first = window.redaction_store.save_group("人事信息", [{"find": "张三", "replace": "员工甲"}])
    second = window.redaction_store.save_group("对外材料", [{"find": "内部公司", "replace": "某公司"}])
    window.m_custom.set_rules([{"find": "李四", "replace": "员工乙"}])
    dialog = RedactionLibraryDialog(window.redaction_store, window.m_custom.rules, window)
    dialog.show()
    dialog.search_edit.setText("内部公司")
    assert dialog.group_list.count() == 1 and dialog.selected_group()["id"] == second["id"]
    dialog.search_edit.clear()
    dialog.refresh(first["id"])
    with patch("qingjian.redaction_library.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
        dialog.update_selected()
    with patch("qingjian.redaction_library.QInputDialog.getText", return_value=("人事常用", True)):
        dialog.rename_selected()
    dialog.restore_checkbox.setChecked(True)
    saved = RedactionMemoryStore(window.redaction_store.path)
    assert saved.find_group(first["id"])["name"] == "人事常用"
    assert saved.find_group(first["id"])["rules"] == window.m_custom.rules()
    assert saved.restore_id == first["id"]
    with patch("qingjian.redaction_library.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
        dialog.delete_selected()
    saved = RedactionMemoryStore(window.redaction_store.path)
    assert saved.restore_id == "" and saved.find_group(first["id"]) is None
    assert saved.find_group(second["id"]) is not None
    dialog.close()


def test_failed_load_keeps_library_open_and_failed_save_has_no_success(windows):
    window = windows()
    window.redaction_store.save_group("常用", [{"find": "张三", "replace": "某人"}])
    dialog = RedactionLibraryDialog(window.redaction_store, window.m_custom.rules, window,
                                     apply_rules=lambda rules, append: False)
    dialog.show()
    dialog.use_selected(True)
    assert dialog.isVisible() and dialog.result() != QDialog.DialogCode.Accepted
    dialog.apply_rules = lambda rules, append: True
    dialog.use_selected(False)
    assert dialog.result() == QDialog.DialogCode.Accepted
    window.m_custom.set_rules([{"find": "张三", "replace": "某人"}])
    before = window.redaction_store.groups
    with patch("qingjian.app.QInputDialog.getText", return_value=("新组", True)), \
         patch("qingjian.redaction_memory.os.replace", side_effect=OSError("disk full")), \
         patch("qingjian.app.QMessageBox.warning") as warning:
        window.remember_redaction_rules()
    warning.assert_called_once()
    assert window.redaction_store.groups == before
    assert "已记住“新组”" not in window.status.text()


def test_hundreds_of_remembered_rules_run_through_actual_word_and_text_batch(windows, tmp_path):
    window = windows()
    rules = [{"find": f"【原{i:04d}】", "replace": f"【匿名{i:04d}】"} for i in range(300)]
    group = window.redaction_store.save_group("批量外发", rules)
    window.redaction_store.set_restore_group(group["id"])
    window = windows()
    window.open_scene("redact")
    content = " ".join(rule["find"] for rule in rules)
    expected = " ".join(rule["replace"] for rule in rules)
    text = tmp_path / "原文.txt"
    text.write_text(content, encoding="utf-8")
    word = tmp_path / "原文.docx"
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run(content[:8])
    paragraph.add_run(content[8:])
    document.save(word)
    originals = {path: path.read_bytes() for path in (text, word)}
    window.output_edit.setText(str(tmp_path / "out"))
    window.accept_sources([str(text), str(word)])
    window.start_task()
    deadline = time.monotonic() + 30
    while window.worker is not None and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    assert window.worker is None
    assert len(window.last_result["records"]) == 2
    for record in window.last_result["records"]:
        assert record["状态"] == "成功", record
        output = Path(record["输出"])
        actual = output.read_text(encoding="utf-8") if output.suffix == ".txt" else Document(output).paragraphs[0].text
        assert actual == expected
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_many_rules_keep_actions_and_memory_controls_usable_at_small_width(windows):
    window = windows()
    window.resize(980, 680)
    window.open_scene("redact")
    window.m_custom.set_rules([{"find": f"词{i}", "replace": f"代称{i}"} for i in range(500)])
    QApplication.processEvents()
    for widget in (window.start_button, window.choose_output_button, window.result_button):
        assert window.rect().contains(QRect(widget.mapTo(window, QPoint(0, 0)), widget.size()))
    assert window.workspace_scroll.horizontalScrollBar().maximum() == 0
    assert window.m_custom.table.rowCount() == 500
    assert window.m_custom.table.columnWidth(0) > 150
    assert window.redaction_library_button.isEnabled()
    assert window.redaction_remember_button.isEnabled()
