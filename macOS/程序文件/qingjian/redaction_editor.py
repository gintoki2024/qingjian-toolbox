"""Editable custom redaction rules with a lossless, structured interface."""
from __future__ import annotations

import csv
import io
from collections.abc import Callable

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .redaction_memory import MAX_RULES, normalize_rules


def _validated_rules(rules) -> list[dict[str, str]]:
    """Validate the whole value before a caller changes the table."""
    if not isinstance(rules, (list, tuple)):
        raise ValueError("替换规则应为列表。")
    if len(rules) > MAX_RULES:
        raise ValueError(f"最多使用 {MAX_RULES} 条规则，请拆分为多个规则组。")
    result = []
    for row, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            raise ValueError(f"第 {row} 行替换规则格式不正确。")
        find, replacement = rule.get("find"), rule.get("replace", "")
        if not isinstance(find, str) or not isinstance(replacement, str):
            raise ValueError(f"第 {row} 行的原内容和替换文字应为文字。")
        if not find:
            raise ValueError(f"第 {row} 行的原内容不能为空。")
        result.append({"find": find, "replace": replacement})
    return normalize_rules(result)


def parse_pasted_rules(text: str, default_replacement: str = "[内容已隐去]") -> list[dict[str, str]]:
    """Read two Excel columns or the older one-rule-per-line notation.

    Tabs select the two-column format, where equals signs are always literal.
    With one column, the first equals sign separates find from replacement.
    Neither format strips intentional leading/trailing spaces.
    """
    if not isinstance(text, str) or not isinstance(default_replacement, str):
        raise ValueError("粘贴内容和统一替换文字应为文字。")
    result = []
    if "\t" in text:
        try:
            rows = list(csv.reader(io.StringIO(text, newline=""), delimiter="\t", strict=True))
        except csv.Error as error:
            raise ValueError("两列粘贴内容格式不完整，请重新从表格复制。") from error
        for row, cells in enumerate(rows, 1):
            if not cells or all(cell == "" for cell in cells):
                continue
            if len(cells) > 2:
                raise ValueError(f"第 {row} 行有 {len(cells)} 列，请只复制原内容和替换文字两列。")
            find = cells[0]
            replacement = cells[1] if len(cells) == 2 else default_replacement
            if not find:
                raise ValueError(f"第 {row} 行的原内容不能为空。")
            result.append({"find": find, "replace": replacement})
    else:
        for row, line in enumerate(text.splitlines(), 1):
            if not line:
                continue
            find, separator, replacement = line.partition("=")
            if not find:
                raise ValueError(f"第 {row} 行的原内容不能为空。")
            result.append({"find": find, "replace": replacement if separator else default_replacement})
    return result


class SensitiveRuleEditor(QWidget):
    """A multi-row table that does not reinterpret ordinary cell contents."""

    changed = Signal()

    def __init__(self, parent=None, *, default_replacement: str | Callable[[], str] = "[内容已隐去]"):
        super().__init__(parent)
        self.default_replacement = default_replacement
        self.setObjectName("sensitiveRuleEditor")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        tip = QLabel("每行一组，双击单元格编辑；替换为空会删除原内容，按从上到下依次处理。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        tools = QHBoxLayout()
        self.tools_layout = tools
        tools.setSpacing(6)
        self.add_button = self._button("＋ 新增", "增加一组替换规则", lambda: self.add_rule())
        self.remove_button = self._button("删除选中", "删除选中的规则行", self.remove_selected)
        self.paste_button = self._button("批量粘贴", "从 Excel 两列或多行文字追加规则", self.show_paste_dialog)
        self.up_button = self._button("↑", "将选中的规则上移", lambda: self.move_selected(-1))
        self.down_button = self._button("↓", "将选中的规则下移", lambda: self.move_selected(1))
        for control in (self.add_button, self.remove_button, self.paste_button, self.up_button, self.down_button):
            tools.addWidget(control)
        tools.addStretch(1)
        layout.addLayout(tools)

        self.table = QTableWidget(0, 2)
        self.table.setObjectName("sensitiveRulesTable")
        self.table.setHorizontalHeaderLabels(["原内容", "替换为（留空删除）"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(60)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.verticalHeader().setMinimumWidth(26)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                   QAbstractItemView.EditTrigger.EditKeyPressed |
                                   QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setMinimumWidth(0)
        self.table.setMinimumHeight(150)
        self.table.setMaximumHeight(210)
        self.table.itemChanged.connect(self._edited)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self._refresh_status()
        self._selection_changed()

    @staticmethod
    def _button(title, tooltip, callback):
        value = QPushButton(title)
        value.setToolTip(tooltip)
        value.setMinimumWidth(0)
        value.setStyleSheet("QPushButton { padding: 5px 8px; }")
        value.clicked.connect(lambda _checked=False: callback())
        return value

    def _replacement(self) -> str:
        value = self.default_replacement() if callable(self.default_replacement) else self.default_replacement
        if not isinstance(value, str):
            raise ValueError("统一替换内容应为文字。")
        return value

    def set_default_replacement(self, value: str | Callable[[], str]):
        self.default_replacement = value

    def _row(self, row: int) -> dict[str, str]:
        return {key: self.table.item(row, column).text() if self.table.item(row, column) else ""
                for column, key in enumerate(("find", "replace"))}

    def _raw_rows(self) -> list[dict[str, str]]:
        return [self._row(row) for row in range(self.table.rowCount())]

    def _rows_with_mode(self):
        rows = self._raw_rows()
        for row, rule in enumerate(rows):
            item = self.table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole):
                rule["_use_default"] = True
        return rows

    def rules(self) -> list[dict[str, str]]:
        result = []
        for row in range(self.table.rowCount()):
            rule = self._row(row)
            if not rule["find"] and not rule["replace"]:
                continue
            if not rule["find"]:
                raise ValueError(f"第 {row + 1} 行的原内容不能为空；请填写原内容或删除该行。")
            result.append(rule)
        return normalize_rules(result)

    def _write_rows(self, rules):
        with QSignalBlocker(self.table):
            self.table.setRowCount(len(rules))
            for row, rule in enumerate(rules):
                for column, key in enumerate(("find", "replace")):
                    self.table.setItem(row, column, QTableWidgetItem(rule[key]))
                self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, bool(rule.get("_use_default")))
                if rule.get("_use_default"):
                    self.table.item(row, 1).setToolTip("使用上方统一替换文字；手动修改此格后会独立保存。")
        self._refresh_status()
        self._selection_changed()
        self.changed.emit()

    def set_rules(self, rules):
        self._write_rows(_validated_rules(rules))

    def set_content(self, sensitive: list[str], rules: list[dict]):
        """Restore older unified-replacement words and explicit mappings."""
        explicit = _validated_rules(rules)
        if not isinstance(sensitive, list):
            raise ValueError("统一替换词应为文字列表。")
        unified = []
        replacement = self._replacement()
        for row, find in enumerate(sensitive, 1):
            if not isinstance(find, str) or not find:
                raise ValueError(f"第 {row} 个统一替换词应为非空文字。")
            unified.append({"find": find, "replace": replacement, "_use_default": True})
        _validated_rules(unified + explicit)
        self._write_rows(unified + explicit)

    def option_rules(self) -> tuple[list[str], list[dict[str, str]]]:
        self.rules()  # Validate every nonempty row before returning any options.
        sensitive, explicit = [], []
        for row, rule in enumerate(self._raw_rows()):
            if not rule["find"] and not rule["replace"]:
                continue
            item = self.table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole):
                sensitive.append(rule["find"])
            else:
                explicit.append(rule)
        return sensitive, explicit

    def refresh_default_replacement(self):
        replacement = self._replacement()
        changed = False
        with QSignalBlocker(self.table):
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item and item.data(Qt.ItemDataRole.UserRole) and self._row(row)["replace"] != replacement:
                    self.table.item(row, 1).setText(replacement)
                    changed = True
        if changed:
            self._refresh_status()
            self.changed.emit()

    def append_rules(self, rules) -> dict[str, int]:
        incoming = _validated_rules(rules)
        current = self._rows_with_mode()
        seen = {(rule["find"], rule["replace"]) for rule in current if rule["find"]}
        replacements = {}
        for find, replacement in seen:
            replacements.setdefault(find, set()).add(replacement)
        added = duplicates = conflicts = 0
        for rule in incoming:
            key = (rule["find"], rule["replace"])
            if key in seen:
                duplicates += 1
                continue
            if rule["find"] in replacements and rule["replace"] not in replacements[rule["find"]]:
                conflicts += 1
            seen.add(key)
            replacements.setdefault(rule["find"], set()).add(rule["replace"])
            current.append(rule)
            added += 1
        if added:
            if len(current) > MAX_RULES:
                raise ValueError(f"追加后超过 {MAX_RULES} 条规则，请减少规则或另存为其他规则组。")
            self._write_rows(current)
            self.table.scrollToItem(self.table.item(self.table.rowCount() - 1, 0))
        self._refresh_status(f"已追加 {added} 组" + (f"，跳过 {duplicates} 组重复规则" if duplicates else ""))
        return {"added": added, "duplicates": duplicates, "conflicts": conflicts}

    def add_rule(self, find: str = "", replace: str = ""):
        if not isinstance(find, str) or not isinstance(replace, str):
            raise ValueError("原内容和替换文字应为文字。")
        if self.table.rowCount() >= MAX_RULES:
            raise ValueError(f"最多使用 {MAX_RULES} 条规则，请拆分为多个规则组。")
        row = self.table.rowCount()
        with QSignalBlocker(self.table):
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(find))
            self.table.setItem(row, 1, QTableWidgetItem(replace))
        self.table.setCurrentCell(row, 0)
        self.table.scrollToItem(self.table.item(row, 0))
        self._refresh_status()
        self.changed.emit()

    def clear(self):
        self._write_rows([])

    def remove_selected(self):
        selected = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not selected:
            return
        with QSignalBlocker(self.table):
            for row in selected:
                self.table.removeRow(row)
        self._refresh_status()
        self._selection_changed()
        self.changed.emit()

    def move_selected(self, direction: int):
        if direction not in (-1, 1):
            raise ValueError("移动方向应为上移或下移。")
        selected = sorted({index.row() for index in self.table.selectedIndexes()})
        if not selected or (direction == -1 and selected[0] == 0) or \
                (direction == 1 and selected[-1] == self.table.rowCount() - 1):
            return
        rows = self._rows_with_mode()
        for row in selected if direction == -1 else reversed(selected):
            rows[row], rows[row + direction] = rows[row + direction], rows[row]
        self._write_rows(rows)
        self.table.clearSelection()
        for row in selected:
            for column in range(2):
                self.table.item(row + direction, column).setSelected(True)
        self.table.scrollToItem(self.table.item(selected[0] + direction, 0))

    def _selection_changed(self):
        selected = {index.row() for index in self.table.selectedIndexes()}
        self.remove_button.setEnabled(bool(selected))
        self.up_button.setEnabled(bool(selected) and min(selected) > 0)
        self.down_button.setEnabled(bool(selected) and max(selected) < self.table.rowCount() - 1)

    def _edited(self, item):
        if item.column() == 1:
            with QSignalBlocker(self.table):
                self.table.item(item.row(), 0).setData(Qt.ItemDataRole.UserRole, False)
                item.setToolTip("")
        self._refresh_status()
        self.changed.emit()

    def _refresh_status(self, prefix: str = ""):
        by_find = {}
        invalid = []
        count = 0
        for row, rule in enumerate(self._raw_rows(), 1):
            if rule["find"]:
                count += 1
                by_find.setdefault(rule["find"], set()).add(rule["replace"])
            elif rule["replace"]:
                invalid.append(row)
        conflict_count = sum(len(values) > 1 for values in by_find.values())
        text = prefix or f"共 {count} 组规则，可滚动查看。"
        if invalid:
            text += f" 第 {invalid[0]} 行尚未填写原内容。"
        if conflict_count:
            text += f" 有 {conflict_count} 个原内容对应不同替换文字，已全部保留，请检查。"
        self.status.setText(text)
        self.status.setToolTip("规则按表格顺序保存；完全相同的追加规则自动跳过。")
        self.add_button.setEnabled(self.table.rowCount() < MAX_RULES)

    def append_pasted_text(self, text: str) -> dict[str, int]:
        return self.append_rules(parse_pasted_rules(text, self._replacement()))

    def show_paste_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("批量追加脱敏规则")
        dialog.resize(620, 400)
        layout = QVBoxLayout(dialog)
        tip = QLabel("可从 Excel 复制两列：左列为原内容，右列为替换文字。\n"
                     "也可每行填写“原内容=替换文字”；只写原内容时使用上方统一替换文字。\n"
                     "等号是原内容的一部分时，请使用 Excel 两列或直接在规则表格中编辑。")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        layout.addWidget(tip)
        text = QPlainTextEdit()
        text.setPlaceholderText("张三\t张先生\n某某有限公司\t某公司\n旧编号\t")
        text.setPlainText(QApplication.clipboard().text())
        layout.addWidget(text, 1)
        message = QLabel("留空的替换文字表示删除；原有规则会保留，完全相同的规则不会重复增加。")
        message.setWordWrap(True)
        message.setObjectName("muted")
        layout.addWidget(message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("追加到表格")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.rejected.connect(dialog.reject)
        def accept():
            try:
                self.append_pasted_text(text.toPlainText())
            except ValueError as error:
                QMessageBox.warning(dialog, "请检查粘贴内容", str(error))
                return
            dialog.accept()
        buttons.accepted.connect(accept)
        layout.addWidget(buttons)
        dialog.exec()

    def toPlainText(self) -> str:
        """Legacy display notation; use rules/option_rules for lossless values."""
        self.rules()
        return "\n".join(rule["find"] if rule.get("_use_default") else rule["find"] + "=" + rule["replace"]
                         for rule in self._rows_with_mode() if rule["find"])

    def setPlainText(self, text: str):
        rules = parse_pasted_rules(text, self._replacement())
        if "\t" not in text:
            for rule, line in zip(rules, (line for line in text.splitlines() if line)):
                rule["_use_default"] = "=" not in line
        self._write_rows(rules)

    def setPlaceholderText(self, text: str):
        # Kept for older callers that treated this control as a plain text box.
        self.table.setToolTip(text)

