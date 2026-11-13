"""Small local conversion dialogs; previews are bounded and read-only."""
from __future__ import annotations

import copy
import codecs
import csv
import io
from pathlib import Path
import zipfile
from xml.etree import ElementTree

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .results_view import open_result_file, safe_file_path

_MAX_PREVIEW_BYTES = 64 * 1024
_MAX_WORKBOOK_BYTES = 2 * 1024 * 1024
_MAX_ROWS, _MAX_COLUMNS = 20, 25
_IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif', '.bmp', '.tif', '.tiff', '.gif', '.avif', '.ico', '.jp2'}
_STYLE = """
QDialog#conversionDialog { background: #faf9f5; color: #3d3929; }
QDialog#conversionDialog QLabel { color: #585448; background: transparent; }
QDialog#conversionDialog QLabel#dialogTitle { font-size: 19px; font-weight: 600; color: #3d3929; }
QDialog#conversionDialog QScrollArea { border: none; background: transparent; }
QDialog#conversionDialog QWidget#dialogBody { background: transparent; }
QDialog#conversionDialog QPushButton { background: #fffefa; border: 1px solid #ded9cc; border-radius: 8px; padding: 7px 12px; color: #585448; }
QDialog#conversionDialog QPushButton:hover { background: #f8eee6; border-color: #d8ab93; }
QDialog#conversionDialog QPushButton:disabled { color: #aaa69c; background: #f1eee6; }
QDialog#conversionDialog QPushButton#dialogPrimary { background: #d97757; color: white; border-color: #cf6b4d; }
QDialog#conversionDialog QComboBox, QDialog#conversionDialog QSpinBox, QDialog#conversionDialog QListWidget, QDialog#conversionDialog QTableWidget, QDialog#conversionDialog QPlainTextEdit { background: #fffefa; color: #3d3929; border: 1px solid #e3dfd2; border-radius: 6px; padding: 4px; }
QDialog#conversionDialog QHeaderView::section { background: #eeeae0; color: #585448; border: none; padding: 6px; }
"""


def _label(value, title=False):
    result = QLabel(str(value))
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(True)
    result.setMinimumWidth(0)
    if title:
        result.setObjectName('dialogTitle')
    return result


def _combo(items, default):
    result = QComboBox()
    for text, value in items:
        result.addItem(text, value)
    index = result.findData(default)
    result.setCurrentIndex(max(0, index))
    return result


def _target_title(action_id):
    try:
        from .converter_hub import target_title
        return target_title(action_id)
    except (ImportError, KeyError, ValueError):
        return str(action_id)


class _ConversionDialog(QDialog):
    def __init__(self, title, subtitle, parent=None):
        super().__init__(parent)
        self.setObjectName('conversionDialog')
        self.setWindowTitle(title)
        self.setStyleSheet(_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        layout.addWidget(_label(title, True))
        layout.addWidget(_label(subtitle))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName('dialogBody')
        self.content = QVBoxLayout(self.body)
        self.content.setContentsMargins(0, 0, 5, 0)
        self.content.setSpacing(12)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        self.footer = QHBoxLayout()
        layout.addLayout(self.footer)
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        width = min(760, max(300, int(area.width() * .90))) if area else 760
        height = min(690, max(240, int(area.height() * .86))) if area else 690
        self.resize(width, height)
        if area:
            self.setMaximumSize(area.size())


class _PreviewWorker(QThread):
    ready = Signal(int, dict)

    def __init__(self, source, generation, encoding='auto', delimiter='auto', parent=None):
        super().__init__(parent)
        self.source, self.generation = str(source), generation
        self.encoding, self.delimiter = encoding, delimiter

    def _read(self, handle, limit):
        chunks, remaining = [], limit
        while remaining > 0 and not self.isInterruptionRequested():
            data = handle.read(min(8192, remaining))
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
        return b''.join(chunks)

    def run(self):
        result = {}
        try:
            source = Path(self.source)
            if source.suffix.lower() in {'.csv', '.tsv', '.xlsx'}:
                from .quick_actions import _local_source
                source = _local_source(self.source, {'extensions': {'.csv', '.tsv', '.xlsx'}})
            if self.isInterruptionRequested():
                return
            if source.suffix.lower() == '.xlsx':
                with zipfile.ZipFile(source) as archive:
                    entry = archive.getinfo('xl/workbook.xml')
                    if entry.file_size > _MAX_WORKBOOK_BYTES:
                        raise ValueError('工作簿目录过大，无法在此预览；仍可按每表分别导出。')
                    with archive.open(entry) as handle:
                        data = self._read(handle, _MAX_WORKBOOK_BYTES + 1)
                if self.isInterruptionRequested():
                    return
                root = ElementTree.fromstring(data)
                names = [element.get('name', '') for element in root.iter()
                         if element.tag.rsplit('}', 1)[-1] == 'sheet'][:10000]
                result = {'sheet_names': names, 'note': f'已读取 {len(names)} 张工作表名称；未读取表格正文。'}
            elif source.suffix.lower() in {'.csv', '.tsv'}:
                with source.open('rb') as handle:
                    data = self._read(handle, _MAX_PREVIEW_BYTES + 1)
                if self.isInterruptionRequested():
                    return
                truncated = len(data) > _MAX_PREVIEW_BYTES
                data = data[:_MAX_PREVIEW_BYTES]
                # A byte-limited sample can end midway through a multibyte character.
                # Keep only complete lines when the file is larger than the sample.
                if truncated:
                    end = max(data.rfind(b'\n'), data.rfind(b'\r'))
                    data = data[:end] if end >= 0 else data
                candidates = [self.encoding] if self.encoding != 'auto' else ['utf-8-sig', 'gb18030']
                text, used_encoding = None, ''
                for encoding in candidates:
                    try:
                        decoder = codecs.getincrementaldecoder(encoding)(errors='strict')
                        text, used_encoding = decoder.decode(data, final=not truncated), encoding
                        break
                    except UnicodeError:
                        continue
                if text is None:
                    raise ValueError('此编码无法读取预览，请切换“读取编码”。')
                delimiter = self.delimiter
                if delimiter == 'auto':
                    try:
                        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=',\t;').delimiter
                    except csv.Error:
                        delimiter = '\t' if source.suffix.lower() == '.tsv' else ','
                rows = []
                for row in csv.reader(io.StringIO(text), delimiter=delimiter):
                    if self.isInterruptionRequested():
                        return
                    rows.append([cell[:500] for cell in row[:_MAX_COLUMNS]])
                    if len(rows) >= _MAX_ROWS:
                        break
                shown = '制表符' if delimiter == '\t' else delimiter
                result = {'rows': rows, 'detected_encoding': used_encoding, 'detected_delimiter': delimiter,
                          'note': f'只读预览：前 {_MAX_ROWS} 行、最多 {_MAX_COLUMNS} 列／64 KB；{used_encoding}，分隔符 {shown}。首行按普通数据展示。'}
            else:
                result = {'note': '旧版表格请先转为 XLSX；转换时可按每张工作表分别导出。'}
        except (OSError, ValueError, LookupError, RuntimeError, zipfile.BadZipFile, KeyError, ElementTree.ParseError, csv.Error) as exc:
            result = {'error': str(exc)}
        if not self.isInterruptionRequested():
            self.ready.emit(self.generation, result)


class ConversionOptionsDialog(_ConversionDialog):
    """Collect only relevant conversion options; never writes source files."""

    def __init__(self, action_id, sources, defaults=None, parent=None):
        super().__init__('转换设置 · ' + _target_title(action_id), '原文件保留。简单设置会记住，下一次可继续使用。', parent)
        self.action_id = str(action_id)
        self.sources = list(map(str, sources))
        self.defaults = dict(defaults or {})
        self._worker = None
        self._generation = 0
        self._closed = False
        self._preview_pending = False
        self._preview_error = ''
        self._fields = {}
        self._sheet_names = []
        self._key = self.action_id.removeprefix('convert:')
        kinds = {Path(source).suffix.lower() for source in self.sources}
        self.content.addWidget(_label(f'本次 {len(self.sources)} 个文件 · ' + '、'.join(sorted(kinds))))
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.content.addLayout(form)
        is_image_input = bool(kinds) and kinds <= _IMAGES
        if is_image_input and self._key in {'jpg', 'jpeg', 'png', 'webp', 'tiff', 'bmp', 'gif', 'ico', 'avif', 'jp2'}:
            if self._key in {'jpg', 'jpeg', 'webp', 'avif'}:
                self.quality_spin = QSpinBox()
                self.quality_spin.setRange(50, 100)
                self.quality_spin.setValue(self._integer_default('quality', 92))
                form.addRow('图片质量', self.quality_spin)
                self._fields['quality'] = self.quality_spin
            self.max_edge_spin = QSpinBox()
            self.max_edge_spin.setRange(0, 10000)
            self.max_edge_spin.setSpecialValueText('保留原尺寸')
            self.max_edge_spin.setSuffix(' 像素')
            self.max_edge_spin.setValue(self._integer_default('max_edge', 0))
            form.addRow('最长边', self.max_edge_spin)
            self._fields['max_edge'] = self.max_edge_spin
            if self._key in {'jpg', 'jpeg', 'bmp', 'jp2'}:
                self.background_combo = _combo([('白色', '#ffffff'), ('黑色', '#000000'), ('自选颜色…', 'custom')], '#ffffff')
                color = QColor(str(self.defaults.get('jpeg_background', '#ffffff')))
                self._background = color.name() if color.isValid() else '#ffffff'
                self.background_combo.setCurrentIndex(max(0, self.background_combo.findData(self._background)))
                if self._background not in {'#ffffff', '#000000'}:
                    self.background_combo.setItemText(2, '自选颜色 ' + self._background)
                    self.background_combo.setCurrentIndex(2)
                self.background_combo.activated.connect(self._choose_background)
                form.addRow('透明区域背景色', self.background_combo)
                self._fields['jpeg_background'] = self.background_combo
            self.content.addWidget(_label('最长边为 0 时保留原尺寸；只缩小，不放大原图。'))
        elif is_image_input and self._key in {'pdf', 'docx', 'pptx', 'images_pdf', 'images_docx', 'images_pptx'}:
            if self._key in {'pdf', 'images_pdf'}:
                explanation = '按原图生成 PDF 页面，多图合并可在下方调整顺序。图片内文字不会被识别。'
            else:
                explanation = '将原图片装入文档或幻灯片；图片内的文字仍为图片内容，不会变成可编辑文字。多图合并可在下方调整顺序。'
            self.content.addWidget(_label(explanation))
        elif '.pdf' in kinds and self._key == 'docx':
            self.content.addWidget(_label('适用于含可提取文字的 PDF。扫描件不会进行 OCR，也不保证还原复杂排版；转换后请复核。'))
        elif kinds & {'.ppt', '.pptx', '.odp'} and self._key in {'docx', 'txt'}:
            self.content.addWidget(_label('此操作提取幻灯片中的文字，无法将动画、图表和整套幻灯片版式还原到 Word 或文本中。'))
        if '.pdf' in kinds and self._key in {'png', 'jpg', 'jpeg'}:
            self.dpi_spin = QSpinBox()
            self.dpi_spin.setRange(72, 300)
            self.dpi_spin.setSuffix(' DPI')
            self.dpi_spin.setValue(self._integer_default('dpi', 144))
            self._fields['dpi'] = self.dpi_spin
            form.addRow('逐页图片清晰度', self.dpi_spin)
        is_delimited_input = bool(kinds & {'.csv', '.tsv'})
        is_delimited_output = self._key in {'csv', 'tsv'}
        if is_delimited_input:
            self.input_encoding_combo = _combo([('自动识别', 'auto'), ('UTF-8 / UTF-8 BOM', 'utf-8-sig'), ('GB18030（中文）', 'gb18030')], self.defaults.get('input_encoding', 'auto'))
            self.input_delimiter_combo = self._delimiter_combo(self.defaults.get('input_delimiter', 'auto'))
            self.input_delimiter_combo.setItemText(0, '自动识别')
            form.addRow('读取编码', self.input_encoding_combo)
            form.addRow('读取分隔符', self.input_delimiter_combo)
            self._fields.update(input_encoding=self.input_encoding_combo, input_delimiter=self.input_delimiter_combo)
        if is_delimited_output:
            self.encoding_combo = _combo([('UTF-8 BOM（Excel 推荐）', 'utf-8-sig'), ('GB18030（中文）', 'gb18030')], self.defaults.get('encoding', 'utf-8-sig'))
            self.delimiter_combo = self._delimiter_combo(self.defaults.get('delimiter', 'auto'))
            form.addRow('导出编码', self.encoding_combo)
            form.addRow('导出分隔符', self.delimiter_combo)
            self._fields.update(encoding=self.encoding_combo, delimiter=self.delimiter_combo)
        if kinds & {'.xlsx', '.xls', '.ods'} and is_delimited_output:
            self.sheet_mode_combo = _combo([('每张工作表分别导出', 'all'), ('只导出指定工作表', 'selected')], self.defaults.get('sheet_mode', 'all'))
            self.sheet_combo = QComboBox()
            self.sheet_combo.setEnabled(False)
            self.sheet_mode_combo.currentIndexChanged.connect(self._sheet_mode_changed)
            form.addRow('工作表范围', self.sheet_mode_combo)
            form.addRow('工作表', self.sheet_combo)
            self._fields['sheet_mode'] = self.sheet_mode_combo
            if len(self.sources) != 1 or kinds != {'.xlsx'}:
                self.sheet_mode_combo.setCurrentIndex(0)
                self.sheet_mode_combo.setEnabled(False)
                self.content.addWidget(_label('批量文件默认每张工作表分别导出；如需指定单张工作表，请单独选择一个 XLSX 文件。'))
        if self._key in {'images_pdf', 'images_docx', 'images_pptx'} and len(self.sources) > 1:
            self.content.addWidget(_label('合并顺序 · 从上到下'))
            self.order_list = QListWidget()
            self.order_list.setMinimumHeight(110)
            self.order_list.setMaximumHeight(220)
            for source in self.sources:
                item = QListWidgetItem(Path(source).name)
                item.setData(Qt.ItemDataRole.UserRole, source)
                item.setToolTip(source)
                self.order_list.addItem(item)
            self.order_list.setCurrentRow(0)
            self.content.addWidget(self.order_list)
            row = QHBoxLayout()
            for title, delta in [('上移', -1), ('下移', 1)]:
                button = QPushButton(title)
                button.clicked.connect(lambda checked=False, delta=delta: self._move_source(delta))
                row.addWidget(button)
            row.addStretch()
            self.content.addLayout(row)
        if is_delimited_input or (kinds & {'.xlsx', '.xls', '.ods'} and is_delimited_output):
            self._preview_pending = True
            self.preview_note = _label('正在读取只读预览…')
            self.content.addWidget(self.preview_note)
            self.preview_table = QTableWidget()
            self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.preview_table.setMinimumHeight(130)
            self.preview_table.setMaximumHeight(240)
            self.preview_table.setVisible(is_delimited_input)
            self.content.addWidget(self.preview_table)
            self._preview_timer = QTimer(self)
            self._preview_timer.setSingleShot(True)
            self._preview_timer.setInterval(100)
            self._preview_timer.timeout.connect(self._start_preview)
            for key in ('input_encoding', 'input_delimiter'):
                if key in self._fields:
                    self._fields[key].currentIndexChanged.connect(self._schedule_preview)
            QTimer.singleShot(0, self._start_preview)
        if not self._fields and not (is_image_input or ('.pdf' in kinds and self._key == 'docx') or (kinds & {'.ppt', '.pptx', '.odp'} and self._key in {'docx', 'txt'})):
            self.content.addWidget(_label('本操作使用原文件的默认页面或打印设置，结果保存到原目录。复杂排版、字体和分页请在转换后检查。'))
        self.validation_note = _label('')
        self.validation_note.setStyleSheet('color: #a04b32;')
        self.validation_note.hide()
        self.content.addWidget(self.validation_note)
        self.content.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('开始转换')
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName('dialogPrimary')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.footer.addWidget(buttons)
        self.resize(min(600, self.width()), min(620, self.height()))

    def _integer_default(self, key, default):
        try:
            return int(self.defaults.get(key, default))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _delimiter_combo(default):
        return _combo([('自动（按目标格式）', 'auto'), ('逗号 ,', ','), ('制表符 Tab', '\t'), ('分号 ;', ';')], default)

    def _choose_background(self):
        value = self.background_combo.currentData()
        if value != 'custom':
            self._background = value
            return
        color = QColorDialog.getColor(QColor(self._background), self, '选择 JPG 背景色')
        if color.isValid():
            self._background = color.name()
            self.background_combo.setItemText(2, '自选颜色 ' + self._background)
        else:
            index = self.background_combo.findData(self._background)
            self.background_combo.setCurrentIndex(index if index >= 0 else 2)

    def _move_source(self, delta):
        row = self.order_list.currentRow()
        target = row + delta
        if row >= 0 and 0 <= target < self.order_list.count():
            item = self.order_list.takeItem(row)
            self.order_list.insertItem(target, item)
            self.order_list.setCurrentRow(target)

    def ordered_sources(self):
        if hasattr(self, 'order_list'):
            return [self.order_list.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.order_list.count())]
        return list(self.sources)

    def _sheet_mode_changed(self):
        self.sheet_combo.setEnabled(self.sheet_mode_combo.currentData() == 'selected' and bool(self._sheet_names))
        if hasattr(self, 'validation_note'):
            self.validation_note.hide()

    def options(self):
        result = {}
        for key, widget in self._fields.items():
            if key == 'jpeg_background':
                result[key] = self._background if widget.currentData() == 'custom' else widget.currentData()
            elif isinstance(widget, QSpinBox):
                result[key] = widget.value()
            else:
                result[key] = widget.currentData()
        if result.get('sheet_mode') == 'selected':
            sheet = self.sheet_combo.currentText()
            if sheet and sheet in self._sheet_names:
                result['sheet'] = sheet
        return result

    def _invalid_options(self, message):
        self.validation_note.setText(message)
        self.validation_note.show()
        self.scroll.ensureWidgetVisible(self.validation_note)

    def accept(self):
        if hasattr(self, 'sheet_mode_combo') and self.sheet_mode_combo.currentData() == 'selected':
            sheet = self.sheet_combo.currentText()
            if not sheet or sheet not in self._sheet_names:
                if self._preview_pending:
                    message = '正在读取工作表，请等待后选择一张工作表，或改为“每张工作表分别导出”。'
                else:
                    message = '未能读取有效的工作表，无法执行“只导出指定工作表”。请检查文件，或主动改为“每张工作表分别导出”。'
                self._invalid_options(message)
                return
        if 'input_encoding' in self._fields:
            if self._preview_pending:
                self._invalid_options('正在检查读取编码和分隔符，请等待预览完成后再开始转换。')
                return
            if self._preview_error:
                self._invalid_options('读取预览失败，请调整读取编码或分隔符，并检查文件后重试。' + self._preview_error)
                return
        super().accept()

    def _schedule_preview(self):
        if self._closed:
            return
        self._generation += 1
        self._preview_pending = True
        self._preview_error = ''
        self.preview_note.setText('正在按新的读取设置检查预览…')
        self.validation_note.hide()
        self._preview_timer.start()

    def _stop_preview(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.requestInterruption()
            worker.wait()
            worker.deleteLater()

    def _start_preview(self):
        if self._closed or not self.sources:
            return
        self._stop_preview()
        self._generation += 1
        self._preview_pending = True
        self._preview_error = ''
        worker = _PreviewWorker(self.sources[0], self._generation,
                                self._fields['input_encoding'].currentData() if 'input_encoding' in self._fields else 'auto',
                                self._fields['input_delimiter'].currentData() if 'input_delimiter' in self._fields else 'auto', self)
        worker.ready.connect(self._show_preview)
        self._worker = worker
        worker.start()

    def _show_preview(self, generation, result):
        if self._closed or generation != self._generation:
            return
        self._preview_pending = False
        self._preview_error = str(result.get('error') or '')
        self.preview_note.setText(self._preview_error or result.get('note', ''))
        self.preview_table.setRowCount(0)
        self.preview_table.setColumnCount(0)
        if 'sheet_names' in result:
            self._sheet_names = result['sheet_names']
            self.sheet_combo.clear()
            self.sheet_combo.addItems(self._sheet_names)
            wanted = self.defaults.get('sheet')
            if wanted in self._sheet_names:
                self.sheet_combo.setCurrentText(wanted)
            if len(self.sources) == 1 and self._sheet_names:
                self.sheet_mode_combo.setEnabled(True)
            self._sheet_mode_changed()
        rows = result.get('rows', [])
        if rows:
            columns = max(map(len, rows))
            self.preview_table.setRowCount(len(rows))
            self.preview_table.setColumnCount(columns)
            self.preview_table.setHorizontalHeaderLabels([str(index + 1) for index in range(columns)])
            for row_index, row in enumerate(rows):
                for column, value in enumerate(row):
                    self.preview_table.setItem(row_index, column, QTableWidgetItem(value))
            self.preview_table.resizeColumnsToContents()

    def done(self, result):
        self._closed = True
        if hasattr(self, '_preview_timer'):
            self._preview_timer.stop()
        self._stop_preview()
        super().done(result)

    def closeEvent(self, event):
        self._closed = True
        if hasattr(self, '_preview_timer'):
            self._preview_timer.stop()
        self._stop_preview()
        super().closeEvent(event)


def _existing_local_folder(value):
    text = str(value or '')
    if not text or '\x00' in text or text.startswith(('\\\\', '//')) or '://' in text:
        return None
    try:
        path = Path(text)
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            return None
        path = path.resolve(strict=True)
        return path if path.is_dir() else None
    except (OSError, ValueError, RuntimeError):
        return None


class ConversionResultDialog(_ConversionDialog):
    retry_requested = Signal(str, list, dict)

    def __init__(self, raw_result, parent=None):
        super().__init__('转换结果', '单击查看说明；双击打开结果文件。失败项目可以保留原设置重试。', parent)
        self.raw_result = raw_result if isinstance(raw_result, dict) else {}
        self.records = [dict(record) for record in self.raw_result.get('records', []) if isinstance(record, dict)]
        if not self.records and self.raw_result.get('errors'):
            self.records = [{'status': 'failed', 'error': '\n'.join(map(str, self.raw_result['errors']))}]
        self.table = QTableWidget(len(self.records), 4)
        self.table.setHorizontalHeaderLabels(['源文件', '状态', '结果文件', '说明'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(170)
        self.table.horizontalHeader().setStretchLastSection(True)
        for row, record in enumerate(self.records):
            sources = self._sources(record)
            source = '\n'.join(sources) or str(record.get('source', ''))
            output = '\n'.join(self._outputs(record))
            status = {'success': '成功', 'failed': '失败', 'cancelled': '已取消', 'skipped': '已跳过'}.get(str(record.get('status', '')), str(record.get('status', '')))
            if record.get('status') == 'success' and record.get('warnings'):
                status = '已生成·请复核'
            note = self._note(record)
            short_sources = '、'.join(Path(value).name if '://' not in value else value for value in sources) or source
            short_outputs = '、'.join(Path(value).name for value in self._outputs(record))
            for column, (value, tooltip) in enumerate(((short_sources, source), (status, status), (short_outputs, output), (note, note))):
                item = QTableWidgetItem(value)
                item.setToolTip(tooltip)
                self.table.setItem(row, column, item)
        self.content.addWidget(self.table, 1)
        self.content.addWidget(_label('选择结果文件 · 多页图片、多个工作表可逐一打开'))
        self.output_combo = QComboBox()
        self.output_combo.setMinimumWidth(0)
        self.output_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.output_combo.setMinimumContentsLength(18)
        self.content.addWidget(self.output_combo)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(100)
        self.detail.setMaximumHeight(190)
        self.content.addWidget(self.detail)
        actions = QHBoxLayout()
        self.open_button = QPushButton('打开结果文件')
        self.folder_button = QPushButton('打开所在文件夹')
        self.retry_button = QPushButton('重试失败项')
        self.open_button.clicked.connect(self._open_selected)
        self.folder_button.clicked.connect(self._open_folder)
        self.retry_button.clicked.connect(self._retry_failed)
        for button in (self.open_button, self.folder_button, self.retry_button):
            actions.addWidget(button)
        self.content.addLayout(actions)
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        self.footer.addStretch()
        self.footer.addWidget(close)
        self.table.itemSelectionChanged.connect(self._select_record)
        self.table.cellDoubleClicked.connect(lambda *_: self._open_selected())
        self.output_combo.currentIndexChanged.connect(self._update_open_buttons)
        groups = self._retry_groups()
        self.retry_button.setEnabled(bool(groups))
        if len(groups) > 1:
            self.retry_button.setText('重试首组失败项')
            self.retry_button.setToolTip('不同任务或设置分组重试；本次重试首组，结果列表保留全部记录。')
        if self.records:
            self.table.selectRow(0)
        self._select_record()

    @staticmethod
    def _sources(record):
        sources = record.get('sources', [])
        return [str(value) for value in sources if isinstance(value, (str, Path))] if isinstance(sources, (list, tuple)) else []

    @staticmethod
    def _outputs(record):
        outputs = record.get('outputs')
        if isinstance(outputs, (list, tuple)):
            return [str(value) for value in outputs if isinstance(value, (str, Path))]
        output = record.get('output')
        return [str(output)] if isinstance(output, (str, Path)) and str(output) else []

    @staticmethod
    def _note(record):
        warnings = record.get('warnings', [])
        if not isinstance(warnings, (list, tuple)):
            warnings = [warnings]
        return '\n'.join(str(value) for value in [record.get('error'), *warnings] if value)

    def _selected(self):
        row = self.table.currentRow()
        return self.records[row] if 0 <= row < len(self.records) else {}

    def _selected_file(self):
        value = self.output_combo.currentData()
        return value if value and safe_file_path(value) is not None else None

    def _selected_folder(self):
        record = self._selected()
        selected = self.output_combo.currentData()
        for value in [selected] if isinstance(selected, str) and selected else []:
            if value.startswith(('\\\\', '//')) or '://' in value or '\x00' in value:
                continue
            folder = _existing_local_folder(value) or _existing_local_folder(Path(value).parent)
            if folder is not None:
                return folder
        return _existing_local_folder(record.get('output_folder'))

    def _select_record(self):
        record = self._selected()
        sources = self._sources(record)
        details = ['源文件：', *(sources or [str(record.get('source', ''))]), '', '输出文件：', *(self._outputs(record) or ['未生成结果']), '', self._note(record)]
        self.detail.setPlainText('\n'.join(details))
        self.output_combo.blockSignals(True)
        self.output_combo.clear()
        outputs = self._outputs(record)
        for index, value in enumerate(outputs):
            self.output_combo.addItem(f'{index + 1}. {Path(value).name}', value)
            self.output_combo.setItemData(index, value, Qt.ItemDataRole.ToolTipRole)
        if not outputs:
            self.output_combo.addItem('未生成结果文件', None)
        self.output_combo.setEnabled(bool(outputs))
        self.output_combo.blockSignals(False)
        self._update_open_buttons()

    def _update_open_buttons(self):
        self.open_button.setEnabled(self._selected_file() is not None)
        self.folder_button.setEnabled(self._selected_folder() is not None)

    def _open_selected(self):
        value = self._selected_file()
        if value and not open_result_file(value):
            QMessageBox.information(self, '无法打开', '文件已移动，或系统没有可用的打开方式。')

    def _open_folder(self):
        folder = self._selected_folder()
        if folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _retry_groups(self):
        groups = []
        for record in self.records:
            if record.get('status') != 'failed':
                continue
            action = record.get('action_id', self.raw_result.get('action_id', ''))
            options = record.get('options', self.raw_result.get('options', {}))
            sources = self._sources(record)
            if not action or not sources or not isinstance(options, dict):
                continue
            existing = next((group for group in groups if group[0] == action and group[2] == options), None)
            if existing is None:
                groups.append([str(action), list(sources), copy.deepcopy(options)])
            else:
                existing[1].extend(sources)
        return groups

    def _retry_failed(self):
        groups = self._retry_groups()
        if not groups or not self.retry_button.isEnabled():
            return
        self.retry_button.setEnabled(False)
        action, sources, options = groups[0]
        self.accept()
        self.retry_requested.emit(action, sources, options)


class AdvancedConversionDialog(_ConversionDialog):
    """Select an uncommon target without executing it."""

    def __init__(self, sources, parent=None):
        super().__init__('更多格式与内容工具', '当前文件的常用格式已在转换轮盘中展示。这里仅列出其余可用操作。', parent)
        self.resize(min(540, self.width()), min(500, self.height()))
        from .converter_hub import available_targets
        sources = list(map(str, sources))
        quick = {item['id'] for item in available_targets(sources, scope='quick')}
        targets = [item for item in available_targets(sources, scope='all') if item['id'] not in quick]
        self.targets = targets
        self.target_list = QListWidget()
        self.target_list.setMinimumHeight(150)
        for target in targets:
            item = QListWidgetItem(str(target.get('title', target['id'])))
            item.setData(Qt.ItemDataRole.UserRole, target['id'])
            item.setToolTip(str(target.get('description', '')))
            self.target_list.addItem(item)
        self.content.addWidget(self.target_list, 1)
        self.description_label = _label('')
        self.content.addWidget(self.description_label)
        kinds = sorted({Path(source).suffix.lower().lstrip('.') or '网页链接' for source in sources})
        self.source_label = _label('输入：' + '、'.join(kinds))
        self.content.addWidget(self.source_label)
        self.target_note = _label('')
        self.content.addWidget(self.target_note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.choose_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.choose_button.setText('使用此操作')
        self.choose_button.setObjectName('dialogPrimary')
        self.choose_button.setEnabled(bool(targets))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.footer.addWidget(buttons)
        self.target_list.currentRowChanged.connect(self._select_target)
        self.target_list.itemDoubleClicked.connect(lambda _: self.accept() if self.selected_action() else None)
        if targets:
            self.target_list.setCurrentRow(0)
        else:
            self.description_label.setText('这些文件没有更多可用格式，请使用轮盘中的常用操作。')

    def _select_target(self, row):
        if 0 <= row < len(self.targets):
            target = self.targets[row]
            self.description_label.setText(str(target.get('description', '')))
            self.target_note.setText('操作：' + str(target.get('title', '')) + ' · ' + str(target.get('id', '')).removeprefix('convert:'))

    def selected_action(self):
        item = self.target_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None
