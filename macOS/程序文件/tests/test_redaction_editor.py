"""Real table editing, bulk rule import, and legacy option compatibility."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from qingjian.redaction_editor import SensitiveRuleEditor, parse_pasted_rules


@pytest.fixture
def editor():
    control = SensitiveRuleEditor()
    yield control
    control.close()
    control.deleteLater()
    QApplication.processEvents()


def test_table_has_two_editable_columns_and_can_hold_five_hundred_rules(editor):
    values = [{"find": f"机构 {i}=分公司", "replace": f"单位 {i}"} for i in range(500)]
    editor.set_rules(values)
    assert editor.table.columnCount() == 2
    assert editor.table.horizontalHeaderItem(0).text() == "原内容"
    assert editor.table.rowCount() == 500
    assert editor.rules() == values
    assert "500" in editor.status.text()


def test_double_click_edits_real_cell_without_interpreting_equals_or_spaces(editor):
    editor.add_rule("原文字", "原替换")
    editor.resize(480, 280)
    editor.show()
    QApplication.processEvents()
    cell = editor.table.item(0, 0)
    point = editor.table.visualItemRect(cell).center()
    QTest.mouseDClick(editor.table.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()
    inline = editor.table.findChild(QLineEdit)
    if inline is None:
        editor.table.editItem(cell)
        QApplication.processEvents()
        inline = editor.table.findChild(QLineEdit)
    assert inline is not None
    inline.setFocus()
    inline.selectAll()
    QTest.keyClicks(inline, "  department=A  ")
    assert inline.text() == "  department=A  "
    QTest.keyClick(inline, Qt.Key.Key_Return)
    QApplication.processEvents()
    assert editor.rules() == [{"find": "  department=A  ", "replace": "原替换"}]


def test_set_rules_is_atomic_and_does_not_emit_when_invalid(editor):
    editor.set_rules([{"find": "保留", "replace": "现值"}])
    spy = QSignalSpy(editor.changed)
    with pytest.raises(ValueError, match="第 2 行"):
        editor.set_rules([{"find": "新规则", "replace": "值"}, {"find": "", "replace": "不能用"}])
    assert editor.rules() == [{"find": "保留", "replace": "现值"}]
    assert spy.count() == 0


def test_blank_new_rows_are_ignored_but_missing_find_has_actual_row_number(editor):
    editor.add_rule()
    editor.add_rule("机构", "")
    editor.add_rule("", "尚未填原内容")
    with pytest.raises(ValueError, match="第 3 行"):
        editor.rules()
    editor.table.item(2, 0).setText("姓名")
    assert editor.rules() == [{"find": "机构", "replace": ""}, {"find": "姓名", "replace": "尚未填原内容"}]


def test_append_skips_exact_duplicates_and_retains_visible_conflicts(editor):
    editor.set_rules([{"find": "姓名", "replace": "甲"}])
    result = editor.append_rules([{"find": "姓名", "replace": "甲"},
                                  {"find": "姓名", "replace": "乙"},
                                  {"find": "组织", "replace": "单位"},
                                  {"find": "组织", "replace": "单位"}])
    assert result == {"added": 2, "duplicates": 2, "conflicts": 1}
    assert editor.rules() == [{"find": "姓名", "replace": "甲"},
                              {"find": "姓名", "replace": "乙"},
                              {"find": "组织", "replace": "单位"}]
    assert "已全部保留" in editor.status.text()
    assert "1 个原内容" in editor.status.text()


def test_append_does_not_block_incomplete_row_or_change_it(editor):
    editor.add_rule("", "待编辑")
    result = editor.append_rules([{"find": "追加", "replace": "成功"}])
    assert result["added"] == 1
    assert editor.table.item(0, 0).text() == ""
    assert editor.table.item(0, 1).text() == "待编辑"
    with pytest.raises(ValueError, match="第 1 行"):
        editor.rules()


def test_excel_paste_preserves_literal_equals_spaces_and_empty_replacement():
    assert parse_pasted_rules("  公司=一  \t  新公司=二  \r\n旧编号\t\r\n人员\t[姓名]\r\n\t\r\n") == [
        {"find": "  公司=一  ", "replace": "  新公司=二  "},
        {"find": "旧编号", "replace": ""},
        {"find": "人员", "replace": "[姓名]"},
    ]


def test_excel_quoted_cells_keep_multiline_and_quotes():
    assert parse_pasted_rules('"原文\n第二行"\t"含""引号""=替换"\r\n') == [
        {"find": "原文\n第二行", "replace": '含"引号"=替换'}]


def test_legacy_lines_use_default_and_first_equals_only():
    assert parse_pasted_rules("张三\n 编号 = A=B \n删去=\n", "[隐藏]") == [
        {"find": "张三", "replace": "[隐藏]"},
        {"find": " 编号 ", "replace": " A=B "},
        {"find": "删去", "replace": ""},
    ]


@pytest.mark.parametrize("text, message", [
    ("甲\t乙\t丙", "第 1 行"), ("\t有替换", "第 1 行"),
    ("有效=规则\n=缺原文", "第 2 行"), ('"坏引号\t替换', "格式不完整"),
])
def test_invalid_bulk_input_has_clear_message_and_no_partial_changes(editor, text, message):
    editor.add_rule("原规则", "保留")
    with pytest.raises(ValueError, match=message):
        editor.append_pasted_text(text)
    assert editor.rules() == [{"find": "原规则", "replace": "保留"}]


def test_dynamic_default_is_used_at_paste_time(editor):
    value = ["[旧隐藏]"]
    editor.set_default_replacement(lambda: value[0])
    value[0] = "[新隐藏]"
    editor.append_pasted_text("张三\n张三\n李四=李先生")
    assert editor.rules() == [{"find": "张三", "replace": "[新隐藏]"}, {"find": "李四", "replace": "李先生"}]


def test_remove_selected_keeps_unselected_rows_and_signal(editor):
    editor.set_rules([{"find": str(i), "replace": str(i)} for i in range(4)])
    editor.table.item(1, 0).setSelected(True)
    editor.table.item(3, 1).setSelected(True)
    spy = QSignalSpy(editor.changed)
    editor.remove_selected()
    assert [rule["find"] for rule in editor.rules()] == ["0", "2"]
    assert spy.count() == 1


def test_move_selected_rows_preserves_relative_order(editor):
    editor.set_rules([{"find": str(i), "replace": ""} for i in range(5)])
    editor.table.item(1, 0).setSelected(True)
    editor.table.item(2, 0).setSelected(True)
    editor.move_selected(1)
    assert [rule["find"] for rule in editor.rules()] == ["0", "3", "1", "2", "4"]
    editor.move_selected(-1)
    assert [rule["find"] for rule in editor.rules()] == ["0", "1", "2", "3", "4"]


def test_clear_empties_and_emits_once(editor):
    editor.add_rule("姓名", "")
    spy = QSignalSpy(editor.changed)
    editor.clear()
    assert editor.rules() == []
    assert editor.table.rowCount() == 0
    assert spy.count() == 1


def test_structured_interface_preserves_newlines_tabs_and_nonbreaking_spaces(editor):
    rules = [{"find": "\u00a0姓名=\n\t甲\u3000", "replace": "\u3000 [含=原样]\t\n "},
             {"find": " ", "replace": ""}]
    editor.set_rules(rules)
    assert editor.rules() == rules
    assert editor.option_rules() == ([], rules)


def test_legacy_plain_text_restores_unified_and_explicit_rules(editor):
    editor.setPlainText("张三\n示例机构=某公司\n原文=\n")
    assert editor.option_rules() == (["张三"], [{"find": "示例机构", "replace": "某公司"}, {"find": "原文", "replace": ""}])
    assert editor.toPlainText() == "张三\n示例机构=某公司\n原文="


def test_set_content_and_default_changes_preserve_legacy_round_trip(editor):
    value = ["[隐藏]"]
    editor.set_default_replacement(lambda: value[0])
    explicit = [{"find": "机构=甲", "replace": "某公司"}]
    editor.set_content(["人名=张三", "另一人"], explicit)
    assert editor.option_rules() == (["人名=张三", "另一人"], explicit)
    value[0] = "[脱敏]"
    editor.refresh_default_replacement()
    assert editor.rules()[:2] == [{"find": "人名=张三", "replace": "[脱敏]"}, {"find": "另一人", "replace": "[脱敏]"}]
    assert editor.option_rules() == (["人名=张三", "另一人"], explicit)


def test_manual_replacement_edit_converts_unified_row_to_explicit(editor):
    value = ["[统一]"]
    editor.set_default_replacement(lambda: value[0])
    editor.set_content(["张三", "李四"], [])
    editor.table.item(0, 1).setText("张先生")
    value[0] = "[新的统一]"
    editor.refresh_default_replacement()
    assert editor.rules() == [{"find": "张三", "replace": "张先生"}, {"find": "李四", "replace": "[新的统一]"}]
    assert editor.option_rules() == (["李四"], [{"find": "张三", "replace": "张先生"}])


def test_append_and_move_keep_unified_replacement_tags(editor):
    editor.set_content(["张三"], [{"find": "单位", "replace": "某公司"}])
    editor.append_rules([{"find": "材料", "replace": "文档"}])
    editor.table.clearSelection()
    editor.table.item(0, 0).setSelected(True)
    editor.move_selected(1)
    assert editor.option_rules() == (["张三"], [{"find": "单位", "replace": "某公司"}, {"find": "材料", "replace": "文档"}])


def test_invalid_legacy_content_does_not_replace_existing_values(editor):
    editor.set_content(["原词"], [])
    with pytest.raises(ValueError):
        editor.set_content(["新词", ""], [{"find": "机构", "replace": "某公司"}])
    assert editor.option_rules() == (["原词"], [])


def test_toolbar_fits_a_narrow_editor_without_horizontal_clipping(editor):
    editor.resize(400, 280)
    editor.show()
    QApplication.processEvents()
    assert editor.width() == 400
    assert editor.down_button.geometry().right() <= editor.width()
    assert editor.table.horizontalScrollBar().maximum() == 0
    assert editor.table.height() >= 150

