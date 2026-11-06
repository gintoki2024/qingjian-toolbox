"""A local, named library for reusable custom replacement groups."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


def _button(text, callback):
    value = QPushButton(text)
    value.setCursor(Qt.CursorShape.PointingHandCursor)
    value.clicked.connect(callback)
    return value


class RedactionLibraryDialog(QDialog):
    rules_requested = Signal(list, bool)

    def __init__(self, store, current_rules, parent=None, *, apply_rules=None):
        super().__init__(parent)
        self.store, self.current_rules = store, current_rules
        self.apply_rules = apply_rules
        self.setWindowTitle("本地规则记忆库")
        self.resize(900, 600)
        self.setMinimumSize(720, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        title = QLabel("保存一次，反复使用")
        title.setObjectName("cardTitle")
        layout.addWidget(title)
        note = QLabel("按用途保存多组自定义替换规则。载入后可继续编辑，原文件和其他处理设置不会改变。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        save_row = QHBoxLayout()
        self.save_button = _button("记住当前规则…", self.save_current)
        self.update_button = _button("用当前规则更新选中组", self.update_selected)
        save_row.addWidget(self.save_button)
        save_row.addWidget(self.update_button)
        save_row.addStretch()
        layout.addLayout(save_row)
        body = QHBoxLayout()
        body.setSpacing(16)
        left = QWidget()
        left.setMinimumWidth(210)
        left.setMaximumWidth(270)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索规则组或原内容")
        self.search_edit.textChanged.connect(self.refresh)
        left_layout.addWidget(self.search_edit)
        self.group_list = QListWidget()
        self.group_list.setObjectName("redactionGroups")
        self.group_list.setMinimumHeight(100)
        self.group_list.currentItemChanged.connect(self.show_selected)
        left_layout.addWidget(self.group_list, 1)
        manage = QHBoxLayout()
        self.rename_button = _button("重命名", self.rename_selected)
        self.delete_button = _button("删除", self.delete_selected)
        self.delete_button.setObjectName("danger")
        manage.addWidget(self.rename_button)
        manage.addWidget(self.delete_button)
        left_layout.addLayout(manage)
        body.addWidget(left)
        right = QVBoxLayout()
        self.group_title = QLabel()
        self.group_title.setObjectName("cardTitle")
        self.group_title.setTextFormat(Qt.TextFormat.PlainText)
        self.group_title.setWordWrap(True)
        right.addWidget(self.group_title)
        self.details = QLabel()
        self.details.setObjectName("muted")
        self.details.setWordWrap(True)
        right.addWidget(self.details)
        self.preview = QTableWidget(0, 2)
        self.preview.setHorizontalHeaderLabels(["原内容", "替换为（空白表示删除）"])
        self.preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.preview.verticalHeader().hide()
        self.preview.verticalHeader().setDefaultSectionSize(32)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview.setAlternatingRowColors(True)
        right.addWidget(self.preview, 1)
        self.restore_checkbox = QCheckBox("下次启动后进入脱敏场景时自动载入这一组")
        self.restore_checkbox.toggled.connect(self.set_restore)
        right.addWidget(self.restore_checkbox)
        body.addLayout(right, 1)
        layout.addLayout(body, 1)
        self.feedback = QLabel()
        self.feedback.setObjectName("muted")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        bottom = QHBoxLayout()
        hint = QLabel("规则仅保存在本机")
        hint.setObjectName("muted")
        bottom.addWidget(hint, 1)
        self.append_button = _button("追加到当前", lambda: self.use_selected(True))
        self.replace_button = _button("覆盖当前规则", lambda: self.use_selected(False))
        self.replace_button.setObjectName("primary")
        bottom.addWidget(self.append_button)
        bottom.addWidget(self.replace_button)
        bottom.addWidget(_button("关闭", self.reject))
        layout.addLayout(bottom)
        self.refresh(self.store.restore_id)
        if self.store.last_error:
            self.feedback.setText(self.store.last_error)

    def selected_group(self):
        item = self.group_list.currentItem()
        return self.store.find_group(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def refresh(self, selected_id=None):
        if not isinstance(selected_id, str):
            selected = self.selected_group()
            selected_id = selected["id"] if selected else ""
        query = self.search_edit.text().casefold()
        self.group_list.blockSignals(True)
        self.group_list.clear()
        selected_row = 0
        groups = self.store.groups
        for group in groups:
            if query and query not in group["name"].casefold() and not any(query in rule["find"].casefold() for rule in group["rules"]):
                continue
            prefix = "★ " if group["id"] == self.store.restore_id else ""
            item = QListWidgetItem(prefix + group["name"] + f"  ·  {len(group['rules'])}条")
            item.setData(Qt.ItemDataRole.UserRole, group["id"])
            item.setToolTip(group["name"])
            self.group_list.addItem(item)
            if group["id"] == selected_id:
                selected_row = self.group_list.count() - 1
        if self.group_list.count():
            self.group_list.setCurrentRow(selected_row)
        self.group_list.blockSignals(False)
        self.show_selected()

    def show_selected(self, *_):
        group = self.selected_group()
        for control in (self.update_button, self.rename_button, self.delete_button,
                        self.append_button, self.replace_button, self.restore_checkbox):
            control.setEnabled(group is not None)
        self.restore_checkbox.blockSignals(True)
        self.restore_checkbox.setChecked(bool(group and self.store.restore_id == group["id"]))
        self.restore_checkbox.blockSignals(False)
        self.preview.setRowCount(0)
        if not group:
            self.group_title.setText("还没有匹配的规则组")
            self.details.setText("在脱敏页填写规则后，点击“记住当前规则”保存。")
            return
        self.group_title.setText(group["name"])
        self.details.setText(f"{len(group['rules'])} 条替换规则 · 双击脱敏页表格可编辑，记忆库预览为只读")
        self.preview.setRowCount(len(group["rules"]))
        for row, rule in enumerate(group["rules"]):
            for column, key in enumerate(("find", "replace")):
                item = QTableWidgetItem(rule[key])
                item.setToolTip(rule[key] if rule[key] else "删除匹配的原内容")
                self.preview.setItem(row, column, item)

    def _current(self):
        rules = self.current_rules()
        if not rules:
            raise ValueError("请先在脱敏页填写至少一条自定义替换规则。")
        return rules

    def save_current(self):
        try:
            rules = self._current()
            name, accepted = QInputDialog.getText(self, "记住当前规则", "给这组规则起个名字：", text="常用脱敏规则")
            if not accepted:
                return
            group = self.store.save_group(name, rules)
            self.search_edit.clear()
            self.refresh(group["id"])
            self.feedback.setText(f"已记住“{group['name']}”，共 {len(group['rules'])} 条。可勾选下次自动载入。")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "规则未保存", str(exc))

    def update_selected(self):
        group = self.selected_group()
        if not group:
            return
        try:
            rules = self._current()
            answer = QMessageBox.question(self, "更新记忆规则", f"用当前的 {len(rules)} 条规则更新“{group['name']}”？",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.store.save_group(group["name"], rules, group_id=group["id"])
            self.refresh(group["id"])
            self.feedback.setText("已更新规则组，下次载入将使用更新后的内容。")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "规则未更新", str(exc))

    def rename_selected(self):
        group = self.selected_group()
        if not group:
            return
        name, accepted = QInputDialog.getText(self, "重命名规则组", "规则组名称：", text=group["name"])
        if accepted:
            try:
                self.store.save_group(name, group["rules"], group_id=group["id"])
                self.refresh(group["id"])
                self.feedback.setText("规则组已重命名。")
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "名称未保存", str(exc))

    def delete_selected(self):
        group = self.selected_group()
        if not group:
            return
        answer = QMessageBox.question(self, "删除记忆规则", f"从记忆库删除“{group['name']}”？当前编辑区和已处理文件不会删除。",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self.store.delete_group(group["id"])
                self.refresh()
                self.feedback.setText("已从记忆库删除该组。")
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "规则未删除", str(exc))

    def set_restore(self, enabled):
        group = self.selected_group()
        if not group:
            return
        try:
            self.store.set_restore_group(group["id"] if enabled else "")
            self.refresh(group["id"])
            self.feedback.setText("下次启动后进入脱敏场景会自动载入这一组。" if enabled else "已关闭这一组的自动载入。")
        except (ValueError, OSError) as exc:
            self.show_selected()
            QMessageBox.warning(self, "自动载入设置未保存", str(exc))

    def use_selected(self, append):
        group = self.selected_group()
        if group:
            if self.apply_rules is not None:
                if self.apply_rules(group["rules"], append) is False:
                    return
            else:
                self.rules_requested.emit(group["rules"], append)
            self.accept()
