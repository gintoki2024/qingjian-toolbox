"""Task-first desktop workbench and conservative file action suggestions.

This module owns presentation and routing metadata only. Choosing an action
opens an existing tool; no document is read or processed by the workbench.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QDialog, QDialogButtonBox, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .catalog import EXCEL_TYPES, IMAGE_TYPES, TEXT_TYPES
from .home_content_ui import PersonalHomePanel


SCENES = [
    dict(id="official", title="公文规范排版", description="统一版式与四级标题，输出预检和纠错报告。",
         mode="word", tab="公文排版", tag="重点推荐", glyph="文", accent="#6257ed", tint="#efedff"),
    dict(id="materials", title="材料完整性核查", description="对照材料清单查缺件，核对表格必填项。",
         mode="check_materials", tag="重点推荐", glyph="核", accent="#198a79", tint="#e6f6f2"),
    dict(id="redact", title="自定义内容脱敏", description="替换敏感词与号码，按你的规则保留表达。",
         mode="word", tab="脱敏", tag="文档处理", glyph="隐", accent="#4f70c7", tint="#eaf0ff"),
    dict(id="compare", title="表格对账与差异", description="按主键比较两份表，定位新增、删除与变更。",
         mode="excel_compare", tag="数据核对", glyph="账", accent="#a66b28", tint="#fff2df"),
    dict(id="template", title="Word / PPT 模板生成", description="用一张数据表，批量填充通知、证明和演示稿。",
         mode="template_fill", tag="批量生成", glyph="模", accent="#9562bc", tint="#f4edfb"),
    dict(id="convert", title="Word / PDF 互转", description="在本机转换文档，按用途选择 PDF 转出方式。",
         mode="convert", tag="格式转换", glyph="转", accent="#3f88a7", tint="#e9f5fa"),
]
SCENE_BY_ID = {item["id"]: item for item in SCENES}

TOOL_GROUPS = [
    dict(id="doc", title="文档处理", modes=["word", "web", "template_fill", "extract_fields", "document_export"]),
    dict(id="table", title="表格与核查", modes=["excel_merge", "excel_compare", "excel_clean", "check_materials"]),
    dict(id="media", title="PDF 与图片", modes=["convert", "image", "pdf_pages", "image_pack"]),
    dict(id="file", title="文件管理", modes=["file_organize", "content_search", "find_duplicates"]),
    dict(id="flow", title="组合流程", modes=["workflow"]),
]

_TOOL_ITEMS = [
    dict(mode="word", title="Word / TXT 批量处理", description="公文排版、格式统一、内容替换与自定义脱敏。", glyph="文", keywords="公文 标题 样板 预检 纠错 脱敏 手机 身份证 替换 删除 清理"),
    dict(mode="web", title="网页转 Word", description="将网页链接或已保存的 HTML 归档为图文文档。", glyph="网", keywords="链接 网址 微信 文章 HTML 离线归档"),
    dict(mode="template_fill", title="Word / PPT 模板填充", description="一行数据生成一份文档，保留模板版式。", glyph="模", keywords="模板 批量填充 通知 证明 PPTX DOCX 演示稿"),
    dict(mode="extract_fields", title="文档信息提取", description="汇总 Word、电子 PDF 的指定字段和表格。", glyph="提", keywords="提取 正则 姓名 编号 表格 字段 汇总"),
    dict(mode="document_export", title="文档导出与提图", description="导出纯文本，或提取 Word 内嵌的原始图片。", glyph="出", keywords="导出 TXT 图片 原图 文本"),
    dict(mode="excel_merge", title="表格合并与拆分", description="按列名汇总表格，或按部门等字段分成多个文件。", glyph="合", keywords="Excel XLSX CSV TSV 汇总 合并 拆分 部门"),
    dict(mode="excel_compare", title="表格对账与差异", description="按主键核对旧表和新表，找出新增、删除与变更。", glyph="账", keywords="Excel XLSX CSV TSV 对比 差异 主键 旧表 新表"),
    dict(mode="excel_clean", title="数据清洗与检查", description="统一空白、日期与数值，检查重复和必填项。", glyph="洗", keywords="Excel XLSX CSV TSV 去重 日期 格式 清洗 空值"),
    dict(mode="check_materials", title="材料完整性核查", description="按材料清单检查文件夹，识别缺件与表格漏填。", glyph="核", keywords="材料 完整性 检查 清单 缺件 附件 必填 核查"),
    dict(mode="convert", title="Word / PDF 互转", description="离线转换 Word 与 PDF，按用途选择转出方式。", glyph="转", keywords="转换 DOC DOCX RTF 电子 扫描 PDF Word"),
    dict(mode="image", title="图片批量裁剪", description="按边距、比例或目标尺寸，批量生成裁剪图片。", glyph="裁", keywords="照片 图片 裁剪 尺寸 比例 JPG PNG"),
    dict(mode="pdf_pages", title="PDF 页面整理", description="合并、拆分、抽取或重新排列 PDF 页面。", glyph="页", keywords="PDF 页面 合并 拆分 页码 抽取 重排"),
    dict(mode="image_pack", title="图片压缩与合图", description="压缩图片、转换格式，或按顺序合成 PDF。", glyph="图", keywords="图片 照片 压缩 格式 KB 合图 PDF JPG PNG"),
    dict(mode="file_organize", title="文件命名与归档", description="先预览，再复制归档或执行可撤销的原地改名。", glyph="档", keywords="文件 文件夹 批量 重命名 命名 归档 分类 撤销"),
    dict(mode="content_search", title="文件全文检索", description="搜索多个文档与文件夹，列出命中位置及上下文。", glyph="搜", keywords="全文 搜索 检索 关键词 文件夹 查找"),
    dict(mode="find_duplicates", title="重复文件查找", description="按实际内容识别完全重复的文件，生成分组清单。", glyph="重", keywords="重复 文件 去重 查重 清单"),
    dict(mode="workflow", title="组合流程", description="保存多个常用步骤，更换文件即可重复运行。", glyph="流", keywords="流程 步骤 组合 预设 自动化"),
]
from .feature_registry import SPECS as OFFICE_SPECS
_OFFICE_GROUP = {"document_compare": "doc", "material_consistency": "table", "word_chapters": "doc",
                 "excel_audit": "table", "excel_lookup": "table", "excel_print": "table",
                 "submission_preflight": "doc", "ppt_prepare": "doc", "pdf_finish": "media", "submission_bind": "doc"}
for _mode, _spec in OFFICE_SPECS.items():
    next(group for group in TOOL_GROUPS if group["id"] == _OFFICE_GROUP[_mode])["modes"].append(_mode)
    _TOOL_ITEMS.append(dict(mode=_mode, title=_spec["title"], description=_spec["description"],
                            glyph="核" if _mode in {"document_compare", "material_consistency", "excel_audit", "submission_preflight"} else "整",
                            keywords="本轮新增 离线 " + _spec["title"] + " " + _spec["note"] + " " + " ".join(sorted(_spec["extensions"]))))
TOOL_ITEMS = {item["mode"]: item for item in _TOOL_ITEMS}
TOOL_BY_MODE = TOOL_ITEMS
for _group in TOOL_GROUPS:
    for _mode in _group["modes"]:
        TOOL_BY_MODE[_mode]["group"] = _group["id"]
        TOOL_BY_MODE[_mode]["group_title"] = _group["title"]


WORKBENCH_STYLE = """
QWidget#wbPage, QWidget#wbCanvas, QScrollArea#wbScroll { background: transparent; border: none; }
QLabel#wbEyebrow { color: #5c50e6; font-size: 12px; font-weight: 600; }
QLabel#wbTitle { color: #1f273d; font-size: 29px; font-weight: 700; }
QLabel#wbSubtitle { color: #768095; font-size: 13px; }
QLabel#wbSection { color: #2d374e; font-size: 16px; font-weight: 600; }
QLabel#wbSmall { color: #828c9f; font-size: 11px; }
QFrame#wbQuickBar { background: #ffffff; border: 1px solid #e2e6f0; border-radius: 16px; }
QFrame#wbQuickBar[dragActive="true"] { background: #efedff; border-color: #8b80ed; }
QAbstractButton#wbTaskCard {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #fcfdff);
    border: 1px solid #e1e5ef; border-radius: 18px;
}
QAbstractButton#wbTaskCard:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #f7f5ff);
    border-color: #c7bff4;
}
QAbstractButton#wbTaskCard:focus { border: 2px solid #a39af0; }
QAbstractButton#wbTaskCard:pressed { background: #f1eeff; border-color: #b5acf0; }
QAbstractButton#wbTaskCard[compact="true"] { border-radius: 14px; }
QLabel#wbCardTitle { color: #2c364e; font-size: 15px; font-weight: 600; }
QLabel#wbCardText { color: #747f95; font-size: 12px; }
QLabel#wbTag {
    color: #7d869c; background: #f3f4fa; border-radius: 7px;
    padding: 2px 7px; font-size: 10px; font-weight: 600;
}
QLabel#wbArrow { color: #a7b0c2; font-size: 18px; }
QPushButton#wbAdd { background: #6358e6; color: #ffffff; border: 1px solid #6358e6; border-radius: 12px; padding: 8px 16px; }
QPushButton#wbAdd:hover { background: #5246d8; }
QPushButton#wbLink { border: none; background: transparent; color: #5c50e6; font-size: 12px; font-weight: 600; padding: 4px 0px; }
QPushButton#wbLink:hover { color: #4337c9; }
QPushButton#wbFilter {
    background: #ffffff; border: 1px solid #e2e6f0; color: #727c92;
    padding: 6px 12px; border-radius: 14px;
}
QPushButton#wbFilter:hover { background: #f7f6ff; border-color: #d0caf6; color: #564add; }
QPushButton#wbFilter:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ece9ff, stop:1 #f3f1ff);
    color: #584ce4; border-color: #cfc7fc; font-weight: 600;
}
QFrame#wbSummary { background: #ffffff; border: 1px solid #e2e6f0; border-radius: 16px; }
QPushButton#wbRow { text-align: left; background: #fafbfe; color: #55607a; border: 1px solid #eaecf4; padding: 9px 12px; border-radius: 11px; }
QPushButton#wbRow:hover { background: #efedff; color: #5c50e6; border-color: #dcd6fa; }
QLabel#wbEmpty { color: #8c96a8; font-size: 12px; padding: 10px 0px; }
QLineEdit#wbSearch {
    padding: 11px 16px; border: 1px solid #dfe4ee; border-radius: 20px; background: #ffffff;
}
QLineEdit#wbSearch:hover { border-color: #c8d0e0; }
QLineEdit#wbSearch:focus { border-color: #938be8; background: #ffffff; }
QFrame#wbFacts { border: 1px solid #e4e8f1; background: #f6f7fb; border-radius: 12px; }
QLabel#wbFact { color: #788297; font-size: 11px; padding: 3px 0px; }
QPushButton#wbGroupCard {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #fcfdff);
    border: 1px solid #dae0eb; border-radius: 18px; text-align: left;
}
QPushButton#wbGroupCard:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #f7f6ff);
    border-color: #c6bef0;
}
QPushButton#wbGroupCard:focus { border: 2px solid #a89ee6; }
QPushButton#wbGroupCard:pressed { background: #f0edfb; }
QLabel#wbGroupTitle { color: #2e3952; font-size: 14px; font-weight: 600; }
QLabel#wbGroupText { color: #778195; font-size: 11px; }
QLabel#wbNoResults { color: #808ca0; padding: 30px 10px; font-size: 13px; }
"""


def _label(text: str, name: str, *, wrap=False) -> QLabel:
    value = QLabel(text)
    value.setObjectName(name)
    value.setWordWrap(wrap)
    value.setMinimumWidth(0)
    value.setTextFormat(Qt.TextFormat.PlainText)
    if wrap:
        value.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    return value


def _button(text, callback=None, name="wbLink"):
    value = QPushButton(text)
    value.setObjectName(name)
    value.setCursor(Qt.CursorShape.PointingHandCursor)
    if callback is not None:
        value.clicked.connect(callback)
    return value


class TaskCard(QPushButton):
    """A keyboard-accessible native button with an ordinary Qt layout."""

    def __init__(self, title, description, *, glyph="文", tag="", accent="#6257ed", tint="#efedff", parent=None):
        super().__init__(parent)
        self._compact = False
        self.setObjectName("wbTaskCard")
        self.setAutoDefault(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        self.setToolTip(description)
        self.setMinimumWidth(0)
        self.setMinimumHeight(145)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.body_layout = QVBoxLayout(self)
        self.body_layout.setContentsMargins(18, 16, 18, 16)
        self.body_layout.setSpacing(9)
        self.top_widget = QWidget()
        top = QHBoxLayout(self.top_widget)
        top.setContentsMargins(0, 0, 0, 0)
        mark = _label(glyph, "wbGlyph")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(34, 34)
        mark.setStyleSheet(f"color: {accent}; background: {tint}; border: none; border-radius: 12px; font-size: 17px; font-weight: 600;")
        top.addWidget(mark)
        top.addStretch()
        top.addWidget(_label(tag, "wbTag"))
        self.body_layout.addWidget(self.top_widget)
        self.title_label = _label(title, "wbCardTitle", wrap=True)
        self.body_layout.addWidget(self.title_label)
        self.bottom_widget = QWidget()
        bottom = QHBoxLayout(self.bottom_widget)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(10)
        self.description_label = _label(description, "wbCardText", wrap=True)
        bottom.addWidget(self.description_label, 1)
        bottom.addWidget(_label("→", "wbArrow"), 0, Qt.AlignmentFlag.AlignBottom)
        self.body_layout.addWidget(self.bottom_widget)
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def sizeHint(self):
        return QSize(160, 48) if self._compact else QSize(265, 156)

    def minimumSizeHint(self):
        return QSize(0, 48 if self._compact else 145)

    def set_compact(self, compact):
        """Keep a complete native action with its title in a small window."""
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self.setProperty("compact", compact)
        if compact:
            self.setMinimumHeight(48)
            self.setMaximumHeight(48)
        else:
            self.setMaximumHeight(16777215)
            self.setMinimumHeight(145)
        self.body_layout.setContentsMargins(*((9, 6, 9, 6) if compact else (18, 16, 18, 16)))
        self.body_layout.setSpacing(0 if compact else 9)
        self.top_widget.setVisible(not compact)
        self.bottom_widget.setVisible(not compact)
        self.title_label.setStyleSheet("font-size: 13px;" if compact else "")
        self.style().unpolish(self)
        self.style().polish(self)
        self.updateGeometry()


class _SummaryButton(QPushButton):
    """Long saved names must never widen the workbench."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.full_text = text
        self.setObjectName("wbRow")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(text)
        self.setToolTip(text)

    def minimumSizeHint(self):
        return QSize(0, 34)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        super().setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideRight, max(0, self.width() - 25)))


class _AdaptiveGrid(QWidget):
    """Reflow cards without imposing a horizontal size on the scroll area."""

    def __init__(self, *, breakpoints=(540, 860), maximum_columns=3, stretch_last=False, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        self.items = []
        self.columns = 0
        self.breakpoints = breakpoints
        self.maximum_columns = maximum_columns
        self.stretch_last = stretch_last

    def set_items(self, items):
        new_items = list(items)
        if new_items == self.items and self.columns > 0:
            return
        self.setUpdatesEnabled(False)
        try:
            for item in self.items:
                self.grid.removeWidget(item)
                if item not in new_items:
                    item.hide()
            self.items = new_items
            self.columns = 0
            self._reflow()
        finally:
            self.setUpdatesEnabled(True)

    def _reflow(self):
        columns = min(self.maximum_columns, 1 + sum(self.width() >= point for point in self.breakpoints))
        if columns == self.columns:
            return
        self.columns = columns
        self.setUpdatesEnabled(False)
        try:
            for column in range(6):
                self.grid.setColumnStretch(column, int(column < columns))
            for index, item in enumerate(self.items):
                self.grid.removeWidget(item)
                span = columns - index % columns if self.stretch_last and index == len(self.items) - 1 else 1
                self.grid.addWidget(item, index // columns, index % columns, 1, span)
                item.show()
            self.updateGeometry()
        finally:
            self.setUpdatesEnabled(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()


class _ScrollPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("wbPage")
        self.setStyleSheet(WORKBENCH_STYLE)
        self.setMinimumWidth(0)
        outside = QVBoxLayout(self)
        outside.setContentsMargins(28, 25, 26, 20)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("wbScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.canvas = QWidget()
        self.canvas.setObjectName("wbCanvas")
        self.canvas.setMinimumWidth(0)
        self.content = QVBoxLayout(self.canvas)
        self.content.setContentsMargins(0, 0, 6, 18)
        self.content.setSpacing(20)
        self.scroll.setWidget(self.canvas)
        outside.addWidget(self.scroll)


class _DropBar(QFrame):
    paths_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("wbQuickBar")
        self.setAcceptDrops(True)
        self.setMinimumWidth(0)

    def _paths(self, event):
        return [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile() and url.toLocalFile()]

    def _active(self, value):
        value = bool(value)
        if bool(self.property("dragActive")) == value:
            return
        self.setProperty("dragActive", value)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):
        if self.isEnabled() and self._paths(event):
            self._active(True)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.isEnabled() and self._paths(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._active(False)
        event.accept()

    def dropEvent(self, event):
        self._active(False)
        paths = self._paths(event)
        if self.isEnabled() and paths:
            self.paths_dropped.emit(paths)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


class GroupCard(QPushButton):
    """Compact, line-bordered group entry, with a readable description."""

    def __init__(self, group, description, glyph, accent, parent=None):
        super().__init__(parent)
        self._description = description
        self._short_description = {
            "doc": "公文 · 脱敏 · 模板", "table": "对账 · 清洗 · 核查",
            "media": "裁剪 · 合图 · 整理", "file": "归档 · 命名 · 检索",
            "flow": "多步骤批量处理",
        }[group["id"]]
        self.setObjectName("wbGroupCard")
        self.setAccessibleName(group["title"])
        self.setToolTip(description + f" · {len(group['modes'])} 个工具")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoDefault(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.body = QHBoxLayout(self)
        self.body.setContentsMargins(13, 9, 13, 9)
        self.body.setSpacing(12)
        self.mark = _label(glyph, "wbGroupGlyph")
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mark.setFixedSize(34, 34)
        tint = QColor(accent)
        tint.setAlpha(28)
        self.mark.setStyleSheet(f"color: {accent}; background: rgba({tint.red()}, {tint.green()}, {tint.blue()}, {tint.alpha()}); "
                                "border: none; border-radius: 11px; font-size: 16px; font-weight: 600;")
        self.body.addWidget(self.mark)
        words = QVBoxLayout()
        words.setSpacing(3)
        self.title_label = _label(group["title"], "wbGroupTitle")
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.description_label = _label(description, "wbGroupText")
        self.description_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        words.addWidget(self.title_label)
        words.addWidget(self.description_label)
        self.body.addLayout(words, 1)
        arrow = _label("›", "wbArrow")
        self.body.addWidget(arrow)
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._compact = None
        self.set_compact(False)

    def set_compact(self, compact):
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self.setFixedHeight(54 if compact else 82)
        self.body.setContentsMargins(9 if compact else 12, 7 if compact else 13, 9 if compact else 12, 7 if compact else 13)
        self.body.setSpacing(8 if compact else 12)
        self.mark.setFixedSize(30 if compact else 36, 30 if compact else 36)
        self.description_label.setText(self._short_description if compact else self._description)

    def minimumSizeHint(self):
        return QSize(0, self.minimumHeight())


class HomePage(_ScrollPage):
    """Grouped office tools above a single personal content pane."""
    group_requested = Signal(str)
    catalog_requested = Signal()
    notebook_requested = Signal()

    def __init__(self, parent=None, *, settings_path=None):
        super().__init__(parent)
        self._compact = False
        self.personal_panel = PersonalHomePanel(settings_path=settings_path)
        self.personal_panel.notebook_requested.connect(self.notebook_requested)
        self.group_section = QWidget()
        self.group_section.setMinimumWidth(0)
        group_layout = QVBoxLayout(self.group_section)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(10)
        heading = QHBoxLayout()
        self.group_heading = _label("办公工具", "wbSection")
        heading.addWidget(self.group_heading)
        heading.addStretch()
        self.catalog_button = _button("全部工具  →", self.catalog_requested.emit)
        heading.addWidget(self.catalog_button)
        group_layout.addLayout(heading)
        self.group_grid = _AdaptiveGrid(breakpoints=(720, 1100), maximum_columns=3, stretch_last=True)
        self.group_grid.grid.setSpacing(9)
        descriptions = {
            "doc": ("公文排版 · 内容脱敏 · 模板填充", "文", "#6558ba"),
            "table": ("材料核查 · 表格对账 · 数据清洗", "表", "#328575"),
            "media": ("PDF 整理 · 图片裁剪 · 压缩合图", "图", "#487fa6"),
            "file": ("批量命名 · 文件归档 · 全文检索", "档", "#9b7438"),
            "flow": ("保存常用步骤，一次完成多项处理", "流", "#82659c"),
        }
        self.group_cards = {}
        for group in TOOL_GROUPS:
            description, glyph, accent = descriptions[group["id"]]
            item = GroupCard(group, description, glyph, accent)
            item.clicked.connect(lambda checked=False, key=group["id"]: self.group_requested.emit(key))
            self.group_cards[group["id"]] = item
        self.group_grid.set_items(self.group_cards.values())
        group_layout.addWidget(self.group_grid)
        self.content.addWidget(self.group_section)
        self.content.addWidget(self.personal_panel, 1)

    def set_compact(self, compact):
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self.layout().setContentsMargins(*((12, 10, 12, 10) if compact else (28, 25, 26, 20)))
        self.content.setContentsMargins(*((0, 0, 0, 0) if compact else (0, 0, 6, 18)))
        self.content.setSpacing(14 if compact else 22)
        self.personal_panel.set_compact(compact)
        self.group_heading.setStyleSheet("font-size: 14px;" if compact else "")
        self.group_grid.breakpoints = (360,) if compact else (720, 1100)
        self.group_grid.maximum_columns = 2 if compact else 3
        self.group_grid.columns = 0
        self.group_grid._reflow()
        for card in self.group_cards.values():
            card.set_compact(compact)
        self.updateGeometry()


class ToolCatalogPage(_ScrollPage):
    tool_requested = Signal(str)
    scene_requested = Signal(str)
    conversion_tools_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._compact = False
        self.current_group = "all"
        self.visible_modes = []
        self.title_label = _label("全部工具", "wbTitle")
        self.subtitle_label = _label("按办公任务分类，也可以搜索工具名称、文件格式或用途。", "wbSubtitle", wrap=True)
        self.content.addWidget(self.title_label)
        self.content.addWidget(self.subtitle_label)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("wbSearch")
        self.search_edit.setPlaceholderText("搜索工具，例如：公文、材料、对账、PPT…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("搜索工具")
        self.search_edit.textChanged.connect(self._filter)
        self.content.addWidget(self.search_edit)
        self.conversion_tools_button = _button("更多格式与内容提取 · 电子书 / 数据 / 通讯录 / 日历", self.conversion_tools_requested.emit, "wbRow")
        self.conversion_tools_button.setToolTip("低频转换集中在此，选择文件后查看适用目标；PDF 转 Excel / CSV 已移除。")
        self.content.addWidget(self.conversion_tools_button)
        self.filters = _AdaptiveGrid(breakpoints=(250, 390, 520, 650, 780), maximum_columns=6)
        self.filters.grid.setHorizontalSpacing(8)
        self.filters.grid.setVerticalSpacing(8)
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.group_buttons = {}
        for item in [dict(id="all", title="全部"), *TOOL_GROUPS]:
            control = _button(item["title"], lambda checked=False, key=item["id"]: self.set_group(key), "wbFilter")
            control.setCheckable(True)
            self.filter_group.addButton(control)
            self.group_buttons[item["id"]] = control
        self.group_buttons["all"].setChecked(True)
        self.filters.set_items(self.group_buttons.values())
        self.content.addWidget(self.filters)
        self.count_label = _label("", "wbSmall")
        self.content.addWidget(self.count_label)
        self.tool_grid = _AdaptiveGrid()
        self.tool_cards = {}
        for tool in TOOL_ITEMS.values():
            card = TaskCard(tool["title"], tool["description"], glyph=tool["glyph"], tag=tool["group_title"])
            card.clicked.connect(lambda checked=False, mode=tool["mode"]: self.tool_requested.emit(mode))
            self.tool_cards[tool["mode"]] = card
        self.content.addWidget(self.tool_grid)
        self.empty_label = _label("没有找到匹配工具。试试“公文”“表格”或“PDF”。", "wbNoResults", wrap=True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content.addWidget(self.empty_label)
        self.content.addStretch()
        self._filter()

    def set_compact(self, compact):
        """Reflow the complete searchable catalog without dropping tools."""
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self.layout().setContentsMargins(*((12, 10, 12, 10) if compact else (28, 25, 26, 20)))
        self.content.setContentsMargins(*((0, 0, 0, 0) if compact else (0, 0, 6, 18)))
        self.content.setSpacing(10 if compact else 20)
        self.title_label.setStyleSheet("font-size: 20px;" if compact else "")
        self.subtitle_label.setVisible(not compact)
        self.filters.breakpoints = (120, 240) if compact else (250, 390, 520, 650, 780)
        self.filters.maximum_columns = 3 if compact else 6
        self.filters.columns = 0
        self.filters._reflow()
        self.tool_grid.breakpoints = (280,) if compact else (540, 860)
        self.tool_grid.maximum_columns = 2 if compact else 3
        self.tool_grid.grid.setHorizontalSpacing(8 if compact else 14)
        self.tool_grid.grid.setVerticalSpacing(8 if compact else 14)
        for card in self.tool_cards.values():
            card.set_compact(compact)
        # Preserve the user's search and category while changing presentation.
        self.tool_grid.columns = 0
        self.tool_grid._reflow()
        self.updateGeometry()

    def set_group(self, group_id: str):
        if group_id not in self.group_buttons:
            group_id = "all"
        self.current_group = group_id
        self.group_buttons[group_id].setChecked(True)
        self._filter()

    def _filter(self, *_):
        terms = self.search_edit.text().casefold().split()
        modes = []
        for item in TOOL_ITEMS.values():
            if self.current_group != "all" and item["group"] != self.current_group:
                continue
            haystack = " ".join(str(item.get(key, "")) for key in ("title", "description", "keywords", "group_title", "mode")).casefold()
            if all(term in haystack for term in terms):
                modes.append(item["mode"])
        self.visible_modes = modes
        self.tool_grid.set_items([self.tool_cards[mode] for mode in modes])
        self.empty_label.setVisible(not modes)
        self.count_label.setText(f"共 {len(modes)} 个工具" if not terms and self.current_group == "all" else f"找到 {len(modes)} 个工具")

    def focus_search(self):
        self.search_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_edit.selectAll()


def _http_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)
    except (TypeError, ValueError):
        return False


def recommend_actions(sources: list[str]) -> list[dict]:
    """Recommend existing tools compatible with the complete selection.

    Only suffixes and directory identity are inspected. No content is opened,
    directories are never scanned, and a recommendation never starts work.
    """
    if not isinstance(sources, (list, tuple)) or not sources:
        return []
    files = []
    kinds = []
    for source in sources:
        if not isinstance(source, (str, Path)):
            return []
        value = str(source).strip()
        if not value or "\x00" in value:
            return []
        if value in files:
            continue
        files.append(value)
        if _http_url(value):
            kinds.append("url")
            continue
        try:
            path = Path(value)
            kinds.append("directory" if path.is_dir() else path.suffix.casefold())
        except (OSError, ValueError):
            return []
    if not files:
        return []
    suffixes = set(kinds)
    actions = []

    def add(action_id, title, description, mode, scene=None, **extra):
        item = dict(id=action_id, title=title, description=description, mode=mode, files=list(files))
        if scene:
            item["scene"] = scene
        item.update(extra)
        actions.append(item)

    word = {".doc", ".docx"}
    if suffixes <= word:
        add("official", "公文规范排版", "调整 Word 版式与标题层级，生成检查报告。", "word", "official")
    if suffixes == {"directory"}:
        add("materials", "材料完整性核查", "把所选文件夹与自定义材料清单逐项比对。", "check_materials", "materials")
    if suffixes <= word | {".txt"}:
        add("redact", "自定义内容脱敏", "按自定义文字替换敏感词、号码等内容。", "word", "redact")
        if ".txt" in suffixes:
            add("replace", "查找与替换内容", "批量替换文字，支持规则匹配。", "word", tab="查找替换")
    if suffixes <= EXCEL_TYPES:
        if len(files) == 2:
            add("compare", "表格对账与差异", "第一份作为旧表，第二份作为新表；可在清单调整顺序。", "excel_compare", "compare")
        add("excel_clean", "数据清洗与检查", "清除多余空白，检查重复、空值与数据格式。", "excel_clean")
        add("template", "Word / PPT 模板填充", "把数据表的每一行填入下方选择的文档模板。", "template_fill", "template")
        add("excel_merge", "表格合并与拆分", "按列名汇总数据，或按指定字段拆分。", "excel_merge")
    if suffixes == {".pptx"} and len(files) == 1:
        add("template", "用作 PPT 模板", "将此文件设为模板；进入后再添加用于填充的数据表。", "template_fill", "template", template_path=files[0], files=[])
    if suffixes <= word | {".rtf", ".pdf"}:
        add("convert", "Word / PDF 互转", "Word 转为 PDF，或选择 PDF 转为 Word 的方式。", "convert", "convert")
    if suffixes == {".pdf"}:
        add("pdf_pages", "PDF 页面整理", "合并、抽取、拆分或调整页面顺序。", "pdf_pages")
    if suffixes <= IMAGE_TYPES:
        add("image", "图片批量裁剪", "按边距、比例或指定尺寸裁剪图片。", "image")
        add("image_pack", "图片压缩与合图", "调整图片体积与格式，或合成 PDF。", "image_pack")
    if suffixes <= word | {".pdf", ".txt"}:
        add("text_export", "导出文档纯文本", "导出可读取的文档文字；扫描 PDF 不进行文字识别。", "document_export", action="text")
    if suffixes <= {".docx", ".pdf"}:
        add("extract_fields", "文档信息提取", "按规则提取字段，汇总可识别的文档表格。", "extract_fields")
    if suffixes <= TEXT_TYPES | {"directory"}:
        add("content_search", "文件全文检索", "搜索文字并列出位置；文件夹只扫描支持的文档格式。", "content_search")
    if "directory" in suffixes and suffixes <= TEXT_TYPES | IMAGE_TYPES | {"directory", ".doc", ".rtf", ".pptx"}:
        add("find_duplicates", "重复文件查找", "按实际内容生成重复文件清单，不自动删除。", "find_duplicates")
        add("file_organize", "文件命名与归档", "预览命名规则，复制归档并保留原件。", "file_organize")
    if suffixes <= {"url", ".html", ".htm", ".xhtml", ".mht", ".mhtml"}:
        add("web", "网页转 Word", "把链接或本地 HTML 转为图文 Word；访问在线链接需要联网。", "web")
    for mode, spec in OFFICE_SPECS.items():
        compatible = suffixes <= spec["extensions"]
        if mode == "material_consistency" and suffixes == {"directory"}:
            compatible = True
        if mode in {"document_compare", "excel_lookup"} and len(files) != 2:
            compatible = False
        if mode == "submission_bind" and len(files) < 2:
            compatible = False
        if compatible:
            add(mode, spec["title"], spec["description"], mode)
    return actions


class SourceActionDialog(QDialog):
    """Choose an action before placing input into an existing tool queue."""

    def __init__(self, sources, parent=None):
        super().__init__(parent)
        self.setWindowTitle("为文件选择操作")
        self.setStyleSheet(WORKBENCH_STYLE)
        self.resize(680, 600)
        self.setMinimumSize(440, 380)
        self.selected_action = None
        self.actions = recommend_actions(sources)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        count = len(dict.fromkeys(str(item).strip() for item in sources)) if isinstance(sources, (list, tuple)) else 0
        layout.addWidget(_label(f"已选择 {count} 项，接下来做什么？", "wbSection", wrap=True))
        layout.addWidget(_label("只显示与本次全部输入兼容的操作。选择后可继续调整参数。", "wbSmall", wrap=True))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        canvas = QWidget()
        body = QVBoxLayout(canvas)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(10)
        self.action_cards = {}
        for action in self.actions:
            scene = SCENE_BY_ID.get(action.get("scene"), {})
            metadata = TOOL_BY_MODE.get(action["mode"], {})
            card = TaskCard(action["title"], action["description"], glyph=scene.get("glyph", metadata.get("glyph", "文")), accent=scene.get("accent", "#6257ed"), tint=scene.get("tint", "#efedff"))
            card.setMinimumHeight(125)
            card.clicked.connect(lambda checked=False, chosen=action: self._choose(chosen))
            self.action_cards[action["id"]] = card
            body.addWidget(card)
        if not self.actions:
            body.addWidget(_label("暂时没有适合这组文件的快捷操作。\n\n请分别选择同类文件，或在“全部工具”中查看支持范围。常用类型包括 Word、PDF、表格、图片、TXT、网页和材料文件夹。", "wbNoResults", wrap=True))
        body.addStretch()
        scroll.setWidget(canvas)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose(self, action):
        self.selected_action = dict(action)
        self.accept()
