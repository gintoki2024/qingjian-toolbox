"""Reusable Chinese settings panels; configuration contains no executable code."""
from __future__ import annotations

import math
import re

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QWidget, QFormLayout, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox, QPushButton, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QTabWidget, QScrollArea, QFrame, QSizePolicy,
    QMessageBox)

from .catalog import TOOLS, WORKFLOW_CHOICES
from .guides import GUIDES


def text_label(text, notice=False):
    value = QLabel(text)
    value.setWordWrap(True)
    value.setObjectName("notice" if notice else "muted")
    return value


def split_columns(text):
    return [part.strip() for part in re.split(r"[,，;；\n]", text) if part.strip()]


class OptionPanel(QWidget):
    changed = Signal()

    def __init__(self, mode, parent=None, *, wrap_long_rows=False):
        super().__init__(parent)
        self.mode, self.controls, self.rows = mode, {}, {}
        self.spec = TOOLS[mode]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 8, 3, 3)
        layout.setSpacing(12)
        if mode in GUIDES:
            layout.addWidget(text_label(GUIDES[mode]["quick"]))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        if wrap_long_rows:
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.addLayout(form)
        for item in self.spec["fields"]:
            key, kind = item["key"], item["kind"]
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            if kind == "bool":
                control = QCheckBox(item["title"])
                control.setChecked(bool(item["default"]))
                control.toggled.connect(self._changed)
            elif kind == "choice":
                control = QComboBox()
                for title, value in item["choices"]:
                    control.addItem(title, value)
                control.setCurrentIndex(max(0, control.findData(item["default"])))
                control.currentIndexChanged.connect(self._changed)
            elif kind in {"int", "float"}:
                control = QSpinBox() if kind == "int" else QDoubleSpinBox()
                if kind == "float":
                    control.setDecimals(item.get("decimals", 3))
                control.setRange(item.get("minimum", 0), item.get("maximum", 1000000))
                control.setValue(item["default"])
                control.valueChanged.connect(self._changed)
            elif kind in {"multiline", "lines"}:
                control = QPlainTextEdit()
                control.setFixedHeight(78)
                control.setPlaceholderText(item.get("placeholder", ""))
                control.setPlainText("\n".join(item["default"]) if kind == "lines" else str(item["default"]))
                control.textChanged.connect(self._changed)
            elif kind == "rules":
                control = QTableWidget(0, len(item["columns"]))
                control.setHorizontalHeaderLabels([col[1] for col in item["columns"]])
                control.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
                control.verticalHeader().hide()
                control.setFixedHeight(115)
                control.itemChanged.connect(self._changed)
            else:
                control = QLineEdit()
                control.setPlaceholderText(item.get("placeholder", ""))
                control.setText(", ".join(item["default"]) if kind == "list" else str(item["default"]))
                if kind == "password":
                    control.setEchoMode(QLineEdit.EchoMode.Password)
                control.textChanged.connect(self._changed)
            self.controls[key] = control
            row_layout.addWidget(control, 1)
            if kind == "file":
                browse = QPushButton("浏览…")
                def choose(checked=False, edit=control, entry=item):
                    path, _ = QFileDialog.getOpenFileName(self, entry["title"], "", entry.get("filter", "所有文件 (*)"))
                    if path:
                        edit.setText(path)
                browse.clicked.connect(choose)
                row_layout.addWidget(browse)
            title = QLabel("" if kind == "bool" else item["title"])
            form.addRow(title, row)
            self.rows[key] = (title, row)
            if kind == "rules":
                tools = QHBoxLayout()
                add = QPushButton("＋ 添加一行")
                remove = QPushButton("删除选中行")
                add.clicked.connect(lambda checked=False, k=key: self.add_rule(k))
                remove.clicked.connect(lambda checked=False, k=key: self.remove_rule(k))
                tools.addWidget(add)
                tools.addWidget(remove)
                tools.addStretch()
                form.addRow("", tools)
                for value in item["default"]:
                    self.add_rule(key, value)
        layout.addWidget(text_label(self.spec["note"], True))
        self._changed()

    def _changed(self, *_):
        for item in self.spec["fields"]:
            if "when" in item:
                dependency, values = item["when"]
                control = self.controls.get(dependency)
                selected = control.currentData() if isinstance(control, QComboBox) else control.text()
                for widget in self.rows[item["key"]]:
                    widget.setVisible(selected in values)
        if self.mode == "file_organize":
            rename = self.controls["action"].currentData() == "rename"
            self.controls["group_by"].setEnabled(not rename)
            if rename and self.controls["group_by"].currentData() != "none":
                self.controls["group_by"].setCurrentIndex(self.controls["group_by"].findData("none"))
        self.changed.emit()

    def add_rule(self, key, values=None):
        table = self.controls[key]
        entry = next(f for f in self.spec["fields"] if f["key"] == key)
        row = table.rowCount()
        table.insertRow(row)
        for column, (field_key, _) in enumerate(entry["columns"]):
            table.setItem(row, column, QTableWidgetItem(str((values or {}).get(field_key, ""))))
        self.changed.emit()

    def remove_rule(self, key):
        table = self.controls[key]
        for row in sorted({index.row() for index in table.selectedIndexes()}, reverse=True):
            table.removeRow(row)
        self.changed.emit()

    def options(self):
        values = {}
        for item in self.spec["fields"]:
            key, kind, control = item["key"], item["kind"], self.controls[item["key"]]
            if kind == "bool":
                value = control.isChecked()
            elif kind == "choice":
                value = control.currentData()
            elif kind in {"int", "float"}:
                value = control.value()
            elif kind == "lines":
                value = [s.strip() for s in control.toPlainText().splitlines() if s.strip()]
            elif kind == "multiline":
                value = control.toPlainText().strip()
            elif kind == "list":
                value = split_columns(control.text())
            elif kind == "rules":
                value = []
                for row in range(control.rowCount()):
                    record = {col[0]: (control.item(row, index).text() if control.item(row, index) else "")
                              for index, col in enumerate(item["columns"])}
                    if any(record.values()):
                        value.append(record)
            else:
                value = control.text().strip()
            values[key] = value
        return values

    def set_options(self, values):
        if not isinstance(values, dict):
            raise ValueError("设置需要是字段和值组成的对象。")
        # Validate values before changing any control.
        for item in self.spec["fields"]:
            if item["key"] not in values:
                continue
            value, kind = values[item["key"]], item["kind"]
            if kind in {"list", "lines", "rules"} and not isinstance(value, list):
                raise ValueError(item["title"] + "的格式应为列表。")
            if kind in {"list", "lines"} and any(not isinstance(v, str) for v in value):
                raise ValueError(item["title"] + "中应填写文字。")
            if kind == "rules" and any(not isinstance(v, dict) for v in value):
                raise ValueError("字段规则格式无效。")
            if kind == "bool" and not isinstance(value, bool):
                raise ValueError(item["title"] + "的格式应为是或否。")
            if kind == "choice" and value not in [v for _, v in item["choices"]]:
                raise ValueError(item["title"] + "包含未知选项。")
            if kind in {"int", "float"}:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(item["title"] + "应为有效数字。")
                if not item.get("minimum", 0) <= value <= item.get("maximum", 1000000) or (kind == "int" and int(value) != value):
                    raise ValueError(item["title"] + "超出允许范围。")
            if kind in {"text", "file", "password", "multiline"} and not isinstance(value, str):
                raise ValueError(item["title"] + "应填写文字。")
        for item in self.spec["fields"]:
            key, kind = item["key"], item["kind"]
            if key not in values:
                continue
            control, value = self.controls[key], values[key]
            if kind == "bool":
                control.setChecked(value)
            elif kind == "choice":
                control.setCurrentIndex(control.findData(value))
            elif kind in {"int", "float"}:
                control.setValue(value)
            elif kind in {"lines", "multiline"}:
                control.setPlainText("\n".join(value) if kind == "lines" else value)
            elif kind == "list":
                control.setText(", ".join(value))
            elif kind == "rules":
                control.setRowCount(0)
                for row in value:
                    self.add_rule(key, row)
            else:
                control.setText(str(value))
        self._changed()


class _WorkflowOptionsScroll(QScrollArea):
    """Keep a long step inside the editor instead of enlarging the whole page."""

    def sizeHint(self):
        return QSize(340, 300)

    def minimumSizeHint(self):
        return QSize(0, 150)


class WorkflowStep(QWidget):
    changed = Signal()

    def __init__(self, mode="excel_merge", options=None):
        super().__init__()
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 4, 0, 0)
        self.choice = QComboBox()
        for title, value in WORKFLOW_CHOICES:
            self.choice.addItem(title, value)
        self.choice.setCurrentIndex(max(0, self.choice.findData(mode)))
        self.body.addWidget(self.choice)
        self.options_scroll = _WorkflowOptionsScroll()
        self.options_scroll.setObjectName("workflowOptionsScroll")
        self.options_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.options_scroll.setWidgetResizable(True)
        self.options_scroll.viewport().setAutoFillBackground(False)
        self.options_scroll.setMinimumWidth(0)
        self.options_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.options_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.body.addWidget(self.options_scroll, 1)
        self.panel = None
        self.rebuild()
        self.choice.currentIndexChanged.connect(self.rebuild)
        if options:
            self.panel.set_options(options)
        self._restrict()

    def _restrict(self):
        if self.choice.currentData() == "file_organize":
            self.panel.controls["action"].setCurrentIndex(0)
            self.panel.controls["action"].setEnabled(False)

    def rebuild(self, *_):
        if self.panel is not None:
            self.options_scroll.takeWidget().deleteLater()
        self.panel = OptionPanel(self.choice.currentData(), wrap_long_rows=True)
        self.panel.changed.connect(self.changed.emit)
        self.options_scroll.setWidget(self.panel)
        self.panel.setAutoFillBackground(False)
        self.options_scroll.verticalScrollBar().setValue(0)
        self._restrict()
        self.changed.emit()

    def options(self):
        return {"tool": self.choice.currentData(), "options": self.panel.options()}


class WorkflowPanel(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 8, 3, 3)
        row = QHBoxLayout()
        add, remove = QPushButton("＋ 添加步骤"), QPushButton("删除当前步骤")
        add.clicked.connect(lambda: self.add_step())
        remove.clicked.connect(self.remove_step)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        self.scope_button = QPushButton("步骤范围")
        self.scope_button.setObjectName("quiet")
        self.scope_button.setToolTip("查看可组合步骤与独立工具的区别")
        self.scope_button.clicked.connect(self.show_step_scope)
        row.addWidget(self.scope_button)
        layout.addLayout(row)
        self.tabs = QTabWidget()
        self.tabs.setMovable(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.currentChanged.connect(lambda *_: self.changed.emit())
        layout.addWidget(self.tabs)
        self.archive = QCheckBox("完成后将最终文件压缩归档为ZIP")
        self.archive.setChecked(True)
        self.stop_on_error = QCheckBox("步骤出现异常时停止后续步骤")
        self.stop_on_error.setChecked(True)
        layout.addWidget(self.archive)
        layout.addWidget(self.stop_on_error)
        layout.addWidget(text_label("按标签顺序运行，可拖动标签调整。匹配文件进入当前步骤，其余文件留给后续步骤；中间结果在本次文件夹内保留。流程中的归档操作只处理副本。", True))
        self.add_step("excel_merge")

    def show_step_scope(self):
        QMessageBox.information(self, "可组合步骤的范围",
            "当前开放：" + "、".join(title for title, _ in WORKFLOW_CHOICES) + "。\n\n"
            "组合步骤需要生成供下一步处理的文件。对账、核查、检索与查重等需要明确输入角色、阅读检查结果或交互确认，请从对应独立工具入口使用。\n\n"
            "其他转换和整理工具尚未接入组合流程；未列出不代表不能单独使用。")

    def add_step(self, mode="excel_merge", options=None):
        step = WorkflowStep(mode, options)
        step.changed.connect(self.changed.emit)
        self.tabs.addTab(step, f"步骤 {self.tabs.count() + 1}")
        self.tabs.setCurrentWidget(step)
        self.changed.emit()

    def remove_step(self):
        index = self.tabs.currentIndex()
        if index >= 0:
            widget = self.tabs.widget(index)
            self.tabs.removeTab(index)
            widget.deleteLater()
            for i in range(self.tabs.count()):
                self.tabs.setTabText(i, f"步骤 {i + 1}")
            self.changed.emit()

    def options(self):
        return {"steps": [self.tabs.widget(i).options() for i in range(self.tabs.count())],
                "archive": self.archive.isChecked(), "stop_on_error": self.stop_on_error.isChecked()}

    def set_options(self, values):
        if not isinstance(values, dict):
            raise ValueError("流程设置应为字段和值组成的对象。")
        steps = values.get("steps", [])
        allowed = {value for _, value in WORKFLOW_CHOICES}
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20 or any(not isinstance(s, dict) or s.get("tool") not in allowed for s in steps):
            raise ValueError("流程步骤格式无效，一次最多20步。")
        for key in ("archive", "stop_on_error"):
            if key in values and not isinstance(values[key], bool):
                raise ValueError("流程开关应为是或否。")
        for step in steps:
            options = step.get("options", {})
            if not isinstance(options, dict):
                raise ValueError("步骤参数应为对象。")
            if step["tool"] == "file_organize" and options.get("action", "copy") != "copy":
                raise ValueError("流程仅支持复制归档，请在文件管理工具中预览后执行原地改名。")
        # Build detached first, so a bad import does not discard a valid recipe.
        new_steps = [WorkflowStep(s["tool"], s.get("options", {})) for s in steps]
        while self.tabs.count():
            item = self.tabs.widget(0)
            self.tabs.removeTab(0)
            item.deleteLater()
        for step in new_steps:
            step.changed.connect(self.changed.emit)
            self.tabs.addTab(step, f"步骤 {self.tabs.count() + 1}")
        self.archive.setChecked(bool(values.get("archive", True)))
        self.stop_on_error.setChecked(bool(values.get("stop_on_error", True)))
        self.changed.emit()
