"""Compact Chinese controls for official document formatting and inspection."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSizePolicy, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .core.official_ops import validate_official_options


FONTS = ["仿宋", "楷体", "黑体", "宋体", "方正小标宋简体", "微软雅黑"]
SEQUENCES = ["一、", "（一）", "1.", "（1）", "第一条", "一是", "不识别"]
DOC_TYPES = ["通知", "请示", "报告", "函", "批复", "会议纪要"]


def note(text):
    control = QLabel(text)
    control.setObjectName("muted")
    control.setWordWrap(True)
    control.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    return control


def font_box():
    box = QComboBox()
    box.setEditable(True)
    box.addItems(FONTS)
    box.setMinimumWidth(110)
    return box


def number(value, low, high, suffix="", decimals=1):
    control = QDoubleSpinBox()
    control.setDecimals(decimals)
    control.setRange(low, high)
    control.setValue(value)
    control.setSuffix(suffix)
    control.setMinimumWidth(85)
    return control


class OfficialPanel(QWidget):
    changed = Signal()
    layout_changed = Signal()
    preflight_requested = Signal()
    template_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.controls = {}
        self._loading = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 6, 3, 3)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.enabled = QCheckBox("启用公文排版")
        self.enabled.setToolTip("启用后使用本页的标题、正文与页面设置，替代通用格式。脱敏、替换和内容清理仍可同时使用。")
        top.addWidget(self.enabled)
        self.operation = QComboBox()
        self.operation.addItem("排版并检查", "format")
        self.operation.addItem("仅生成检查报告", "check")
        top.addWidget(self.operation)
        top.addStretch()
        self.preflight_button = QPushButton("仅检查所选文件")
        self.preflight_button.setToolTip("只生成格式与纠错报告，保留原文件。")
        self.template_button = QPushButton("生成公文样板")
        for control in (self.preflight_button, self.template_button):
            control.setCursor(Qt.CursorShape.PointingHandCursor)
            top.addWidget(control)
        layout.addLayout(top)

        presets = QHBoxLayout()
        presets.addWidget(QLabel("版式预设"))
        self.profile = QComboBox()
        self.profile.addItem("通用公文格式", "company")
        self.profile.addItem("自定义公文格式", "general")
        self.profile.setToolTip("提供可调整的标题、正文、页边距和页码设置，不代表国家标准符合性认证。切换预设会恢复本页版式和标题层级默认值。")
        presets.addWidget(self.profile, 2)
        presets.addSpacing(10)
        presets.addWidget(QLabel("公文种类"))
        self.doc_type = QComboBox()
        self.doc_type.addItems(DOC_TYPES)
        presets.addWidget(self.doc_type, 1)
        layout.addLayout(presets)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_layout(), "版式与页码")
        self.tabs.addTab(self.build_headings(), "标题识别")
        self.tabs.addTab(self.build_review(), "清理与校对")
        layout.addWidget(self.tabs)
        layout.addWidget(note("公文排版可与脱敏、替换一起使用；检查报告随结果保存。样板不生成文号。"))
        self.set_options({})
        self._loading = False
        self.enabled.toggled.connect(self.changed)
        self.operation.currentIndexChanged.connect(self.changed)
        self.doc_type.currentTextChanged.connect(self.changed)
        self.profile.currentIndexChanged.connect(self.reset_profile)
        self.tabs.currentChanged.connect(self.resize_content)
        self.preflight_button.clicked.connect(self.preflight_requested)
        self.template_button.clicked.connect(self.template_requested)
        self.resize_content()

    def build_layout(self):
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(4, 12, 4, 5)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(6)
        for label, prefix, size in (("文档标题", "title", 22), ("正文", "body", 16)):
            row = QHBoxLayout()
            row.setSpacing(10)
            font = self.controls[prefix + "_font"] = font_box()
            points = self.controls[prefix + "_size"] = number(size, 6, 48 if prefix == "body" else 72, " 磅")
            row.addWidget(font, 2)
            row.addWidget(QLabel("字号"))
            row.addWidget(points, 1)
            form.addRow(label, row)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.controls["line_pt"] = number(28, 6, 100, " 磅")
        self.controls["indent_chars"] = number(2, 0, 8, " 字符")
        row.addWidget(self.controls["line_pt"], 1)
        row.addWidget(QLabel("正文首行缩进"))
        row.addWidget(self.controls["indent_chars"], 1)
        form.addRow("固定行距", row)
        margins = QHBoxLayout()
        margins.setSpacing(6)
        for key, title, value in (("top_cm", "上", 3.7), ("bottom_cm", "下", 3.5),
                                  ("left_cm", "左", 2.8), ("right_cm", "右", 2.6)):
            margins.addWidget(QLabel(title))
            self.controls[key] = number(value, 0, 10 if key in {"top_cm", "bottom_cm"} else 8, " cm", 2)
            margins.addWidget(self.controls[key], 1)
        form.addRow("A4 页边距", margins)
        page_numbers = self.controls["page_numbers"] = QComboBox()
        for title, value in (("奇数页靠右，偶数页靠左", "outside"), ("居中页码", "center"), ("保留原页脚", "keep")):
            page_numbers.addItem(title, value)
        form.addRow("页码", page_numbers)
        tables = self.controls["format_tables"] = QCheckBox("同时统一表格内文字格式")
        form.addRow("表格", tables)
        form.addRow(note("标题使用单倍行距；正文使用固定行距。自定义字体需在使用电脑上可用。"))
        self.connect_controls(page)
        return page

    def build_headings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 4, 5)
        self.headings = QTableWidget(4, 5)
        self.headings.setHorizontalHeaderLabels(["层级", "序号类型", "字体", "字号", "识别关键字"])
        self.headings.verticalHeader().hide()
        self.headings.verticalHeader().setDefaultSectionSize(42)
        self.headings.setFixedHeight(206)
        self.headings.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = self.headings.horizontalHeader()
        for column, width in ((0, 48), (1, 104), (2, 143), (3, 97)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.headings.setColumnWidth(column, width)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.heading_controls = []
        for index, name in enumerate(("一级", "二级", "三级", "四级")):
            label = QTableWidgetItem(name)
            label.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.headings.setItem(index, 0, label)
            sequence, font, size, keywords = QComboBox(), font_box(), number(16, 6, 72), QLineEdit()
            sequence.addItems(SEQUENCES)
            font.setMinimumWidth(70)
            size.setMinimumWidth(60)
            keywords.setPlaceholderText("智能识别")
            keywords.setToolTip("留空使用智能识别。普通文字按包含匹配；多个关键字用 |、逗号或分号分隔。=全文用于精确匹配，末尾*用于开头匹配；正则:用于表达式。自定义关键字优先。")
            for column, control in enumerate((sequence, font, size, keywords), 1):
                self.headings.setCellWidget(index, column, control)
            self.heading_controls.append((sequence, font, size, keywords))
        layout.addWidget(self.headings)
        layout.addWidget(note("关键字优先，其次按序号和原有标题样式识别。序号只用于识别，不自动改写原文编号。"))
        self.connect_controls(page)
        return page

    def build_review(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 4, 5)
        layout.setSpacing(10)
        cleanup = QHBoxLayout()
        for key, title in (("remove_blank_paragraphs", "删除空白段落"),
                           ("remove_extra_spaces", "清理多余空格"),
                           ("split_line_breaks", "按手动换行拆段")):
            self.controls[key] = QCheckBox(title)
            cleanup.addWidget(self.controls[key])
        cleanup.addStretch()
        layout.addLayout(cleanup)
        punctuation = QHBoxLayout()
        punctuation.addWidget(QLabel("统一标点"))
        self.controls["punctuation"] = QComboBox()
        for title, value in (("保留原样", "keep"), ("中文标点", "zh"), ("英文标点", "en")):
            self.controls["punctuation"].addItem(title, value)
        punctuation.addWidget(self.controls["punctuation"], 1)
        punctuation.addStretch(2)
        layout.addLayout(punctuation)
        checks = QHBoxLayout()
        for key, title in (("preflight", "格式预检"), ("proofread", "规则纠错"),
                           ("company_term_check", "单位称谓"), ("placeholder_check", "残留占位符")):
            self.controls[key] = QCheckBox(title)
            checks.addWidget(self.controls[key])
        checks.addStretch()
        layout.addLayout(checks)
        company = QHBoxLayout()
        company.addWidget(QLabel("标准单位名称"))
        self.controls["company_name"] = QLineEdit()
        self.controls["company_name"].setPlaceholderText("选填；留空不检查单位名称")
        self.controls["company_name"].setToolTip("仅以你填写的名称为依据，提示同主体名称的常见公司后缀差异，不猜测单位简称、不自动替换。其他写法可在自定义错词库中明确指定。")
        company.addWidget(self.controls["company_name"], 1)
        layout.addLayout(company)
        terms_header = QHBoxLayout()
        terms_header.addWidget(QLabel("自定义错词库"))
        terms_header.addStretch()
        self.load_terms_button = QPushButton("载入词库文件")
        self.load_terms_button.clicked.connect(self.load_terms)
        terms_header.addWidget(self.load_terms_button)
        layout.addLayout(terms_header)
        self.controls["custom_terms"] = QPlainTextEdit()
        self.controls["custom_terms"].setFixedHeight(82)
        self.controls["custom_terms"].setPlaceholderText("内置常见错词提示；可在这里补充，每行一条，例如：布署=部署")
        layout.addWidget(self.controls["custom_terms"])
        layout.addWidget(note("纠错只给建议，不替换原文；日期、标点、括号、编号连续性和占位内容一并检查。"))
        self.connect_controls(page)
        return page

    def connect_controls(self, page):
        for control in page.findChildren(QWidget):
            if isinstance(control, QComboBox):
                control.currentTextChanged.connect(self._changed)
            elif isinstance(control, QDoubleSpinBox):
                control.valueChanged.connect(self._changed)
            elif isinstance(control, QCheckBox):
                control.toggled.connect(self._changed)
            elif isinstance(control, QLineEdit):
                control.textChanged.connect(self._changed)
            elif isinstance(control, QPlainTextEdit):
                control.textChanged.connect(self._changed)

    def _changed(self, *_):
        if not self._loading:
            self.changed.emit()

    def resize_content(self, *_):
        page = self.tabs.currentWidget()
        page.layout().activate()
        self.tabs.setFixedHeight(page.sizeHint().height() + self.tabs.tabBar().sizeHint().height() + 9)
        self.layout_changed.emit()

    def options(self):
        values = {"profile": self.profile.currentData(), "doc_type": self.doc_type.currentText(), "operation": self.operation.currentData()}
        for key, control in self.controls.items():
            if isinstance(control, QComboBox):
                values[key] = control.currentData() if key in {"page_numbers", "punctuation"} else control.currentText().strip()
            elif isinstance(control, QDoubleSpinBox):
                values[key] = control.value()
            elif isinstance(control, QCheckBox):
                values[key] = control.isChecked()
            elif isinstance(control, QPlainTextEdit):
                values[key] = control.toPlainText()
            else:
                values[key] = control.text().strip()
        values["headings"] = [{"level": index + 1, "sequence": sequence.currentText(),
                              "font": font.currentText().strip(), "size": size.value(),
                              "keywords": keywords.text().strip()}
                             for index, (sequence, font, size, keywords) in enumerate(self.heading_controls)]
        return validate_official_options(values)

    def set_options(self, values, enabled=None):
        values = validate_official_options(values)
        loading = self._loading
        self._loading = True
        try:
            self.profile.setCurrentIndex(max(0, self.profile.findData(values["profile"])))
            self.doc_type.setCurrentText(values["doc_type"])
            self.operation.setCurrentIndex(self.operation.findData(values["operation"]))
            for key, control in self.controls.items():
                value = values[key]
                if isinstance(control, QComboBox):
                    if key in {"page_numbers", "punctuation"}:
                        control.setCurrentIndex(control.findData(value))
                    else:
                        control.setCurrentText(str(value))
                elif isinstance(control, QDoubleSpinBox):
                    control.setValue(value)
                elif isinstance(control, QCheckBox):
                    control.setChecked(value)
                elif isinstance(control, QPlainTextEdit):
                    control.setPlainText(value)
                else:
                    control.setText(value)
            for rule, (sequence, font, size, keywords) in zip(values["headings"], self.heading_controls):
                sequence.setCurrentText(rule["sequence"])
                font.setCurrentText(rule["font"])
                size.setValue(rule["size"])
                keywords.setText(rule["keywords"])
            if enabled is not None:
                self.enabled.setChecked(enabled)
        finally:
            self._loading = loading
        self._changed()

    def reset_profile(self, *_):
        if self._loading:
            return
        self.set_options({"profile": self.profile.currentData(), "doc_type": self.doc_type.currentText()})

    def load_terms(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择本地错词库", "", "文本词库 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            file = Path(path)
            if file.stat().st_size > 1024 * 1024:
                raise ValueError("词库文件不能超过 1 MB。")
            text = file.read_text(encoding="utf-8-sig")
            validate_official_options({"custom_terms": text})
            self.controls["custom_terms"].setPlainText(text)
        except Exception as exc:
            QMessageBox.warning(self, "词库载入失败", str(exc))
