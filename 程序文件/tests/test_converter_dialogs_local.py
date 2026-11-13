"""Bounded previews, preserved retry sources and honest advanced targets."""
from __future__ import annotations

import csv
import io
import threading
import time
import zipfile

from PySide6.QtCore import QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian import converter_dialogs as dialogs


def until(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(5)
    assert predicate()


def close(dialog):
    dialog.reject()
    dialog.deleteLater()
    QApplication.processEvents()


def test_csv_preview_preserves_identifiers_and_has_bounded_sample(tmp_path):
    source = tmp_path / 'identifiers.csv'
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=';')
    for _ in range(1000):
        writer.writerow(['00123', '13612345678', '中文', '重复', '重复'])
    source.write_bytes(buffer.getvalue().encode('gb18030'))
    dialog = dialogs.ConversionOptionsDialog('convert:xlsx', [str(source)])
    until(lambda: dialog.preview_table.rowCount() > 0)
    assert dialog.preview_table.rowCount() == 20
    assert dialog.preview_table.item(0, 0).text() == '00123'
    assert dialog.preview_table.item(0, 2).text() == '中文'
    assert dialog.options() == {'input_encoding': 'auto', 'input_delimiter': 'auto'}
    assert source.read_bytes() == buffer.getvalue().encode('gb18030')
    close(dialog)


def test_byte_limited_preview_does_not_misidentify_partial_utf8_character(tmp_path):
    source = tmp_path / 'long-cell.csv'
    source.write_text('字' * 30000 + ',001\n', encoding='utf-8')
    dialog = dialogs.ConversionOptionsDialog('convert:xlsx', [str(source)], {'input_encoding': 'utf-8-sig'})
    until(lambda: dialog.preview_table.rowCount() > 0)
    assert dialog.preview_table.item(0, 0).text() == '字' * 500
    assert 'UTF' in dialog.preview_note.text().upper()
    close(dialog)


def test_preview_read_runs_outside_gui_and_reject_waits_for_real_thread(tmp_path, monkeypatch):
    source = tmp_path / 'data.csv'
    source.write_text('a,b\n1,2', encoding='utf-8')
    entered = threading.Event()
    exited = threading.Event()
    original = dialogs._PreviewWorker._read
    observed = []

    def cooperative_read(worker, handle, limit):
        observed.append(QThread.currentThread() == QApplication.instance().thread())
        entered.set()
        while not worker.isInterruptionRequested():
            time.sleep(.002)
        exited.set()
        return original(worker, handle, limit)

    monkeypatch.setattr(dialogs._PreviewWorker, '_read', cooperative_read)
    dialog = dialogs.ConversionOptionsDialog('convert:xlsx', [str(source)])
    until(entered.is_set)
    worker = dialog._worker
    dialog.reject()
    assert exited.is_set() and not worker.isRunning()
    assert dialog._worker is None and observed == [False]
    dialog.deleteLater()


def test_xlsx_sheet_names_are_read_without_openpyxl_or_sheet_bodies(tmp_path):
    source = tmp_path / 'two.xlsx'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="First"/><sheet name="Second"/></sheets></workbook>')
        archive.writestr('xl/worksheets/sheet1.xml', 'not parsed')
    dialog = dialogs.ConversionOptionsDialog('convert:csv', [str(source)], {'sheet': 'Second', 'sheet_mode': 'selected', 'encoding': 'gb18030', 'delimiter': ';'})
    until(lambda: dialog.sheet_combo.count() == 2)
    assert dialog.sheet_combo.isEnabled()
    assert dialog.options() == {'sheet_mode': 'selected', 'sheet': 'Second', 'encoding': 'gb18030', 'delimiter': ';'}
    dialog.sheet_mode_combo.setCurrentIndex(0)
    assert 'sheet' not in dialog.options() and dialog.options()['sheet_mode'] == 'all'
    close(dialog)


def test_multiple_workbooks_cannot_select_sheet_from_first_file(tmp_path):
    source = tmp_path / 'one.xlsx'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('xl/workbook.xml', '<workbook><sheets><sheet name="A"/></sheets></workbook>')
    dialog = dialogs.ConversionOptionsDialog('convert:tsv', [str(source), str(tmp_path / 'two.xlsx')], {'sheet_mode': 'selected', 'sheet': 'A'})
    until(lambda: dialog.sheet_combo.count() == 1)
    assert dialog.options()['sheet_mode'] == 'all'
    assert not dialog.sheet_mode_combo.isEnabled()
    close(dialog)


def test_image_options_order_and_pdf_dpi_are_clamped_to_supported_bounds(tmp_path):
    sources = [str(tmp_path / 'first.png'), str(tmp_path / 'second.png')]
    dialog = dialogs.ConversionOptionsDialog('convert:images_pdf', sources, {'quality': 999, 'max_edge': -5, 'jpeg_background': '#124c64'})
    dialog._move_source(1)
    assert dialog.ordered_sources() == list(reversed(sources))
    assert dialog.options() == {}
    assert not hasattr(dialog, 'quality_spin') and not hasattr(dialog, 'max_edge_spin')
    assert sources[0].endswith('first.png')
    close(dialog)
    jpeg = dialogs.ConversionOptionsDialog('convert:jpg', sources, {'quality': 999, 'max_edge': -5, 'jpeg_background': '#124c64'})
    assert jpeg.options() == {'quality': 100, 'max_edge': 0, 'jpeg_background': '#124c64'}
    close(jpeg)
    pdf = dialogs.ConversionOptionsDialog('convert:png', [str(tmp_path / 'file.pdf')], {'dpi': 999})
    assert pdf.options() == {'dpi': 300}
    close(pdf)


def test_image_parameters_only_appear_when_backend_uses_them(tmp_path):
    source = str(tmp_path / 'picture.png')
    expected = {
        'png': {'max_edge'}, 'webp': {'max_edge', 'quality'},
        'bmp': {'max_edge', 'jpeg_background'}, 'jp2': {'max_edge', 'jpeg_background'},
        'tiff': {'max_edge'}, 'avif': {'max_edge', 'quality'},
        'pdf': set(), 'images_docx': set(), 'images_pptx': set(),
    }
    for target, keys in expected.items():
        dialog = dialogs.ConversionOptionsDialog('convert:' + target, [source])
        assert set(dialog.options()) == keys
        close(dialog)


def test_text_extraction_limitations_are_visible_in_dialog(tmp_path):
    pdf = dialogs.ConversionOptionsDialog('convert:docx', [str(tmp_path / 'scan.pdf')])
    assert 'OCR' in pdf.body.findChildren(dialogs.QLabel)[1].text()
    close(pdf)
    ppt = dialogs.ConversionOptionsDialog('convert:docx', [str(tmp_path / 'slides.pptx')])
    assert '提取' in ppt.body.findChildren(dialogs.QLabel)[1].text()
    close(ppt)


def test_result_retry_uses_original_source_array_and_only_matching_failed_group(tmp_path):
    literal = str(tmp_path / 'company、division.csv')
    successful = tmp_path / 'done.xlsx'
    successful.write_text('result', encoding='utf-8')
    first_options = {'encoding': 'gb18030', 'delimiter': ';'}
    raw = {'action_id': 'convert:csv', 'options': {}, 'records': [
        {'sources': [literal, str(tmp_path / 'second.csv')], 'source': 'misleading、display', 'status': 'failed', 'action_id': 'convert:csv', 'options': first_options, 'error': 'No permission'},
        {'sources': [str(tmp_path / 'third.csv')], 'status': 'failed', 'action_id': 'convert:csv', 'options': dict(first_options)},
        {'sources': [str(tmp_path / 'fourth.csv')], 'status': 'failed', 'action_id': 'convert:csv', 'options': {'encoding': 'utf-8-sig'}},
        {'sources': [str(tmp_path / 'done.csv')], 'status': 'success', 'outputs': [str(successful)]},
    ]}
    dialog = dialogs.ConversionResultDialog(raw)
    retries = []
    dialog.retry_requested.connect(lambda action, sources, options: retries.append((action, sources, options)))
    assert dialog.retry_button.isEnabled()
    assert dialog.table.rowCount() == 4
    dialog._retry_failed()
    dialog._retry_failed()
    assert retries == [('convert:csv', [literal, str(tmp_path / 'second.csv'), str(tmp_path / 'third.csv')], first_options)]
    assert raw['records'][0]['sources'][0] == literal
    dialog.deleteLater()


def test_result_safe_open_rejects_executable_and_uses_selected_existing_result(tmp_path, monkeypatch):
    executable = tmp_path / 'bad.exe'
    executable.write_text('not runnable', encoding='utf-8')
    report = tmp_path / 'report.txt'
    report.write_text('complete', encoding='utf-8')
    opened = []
    monkeypatch.setattr(dialogs, 'open_result_file', lambda value: opened.append(value) or True)
    dialog = dialogs.ConversionResultDialog({'records': [
        {'sources': ['one'], 'status': 'success', 'outputs': [str(executable)]},
        {'sources': ['two'], 'status': 'success', 'outputs': [str(report)], 'warnings': ['Review layout']},
    ]})
    assert not dialog.open_button.isEnabled() and not dialog.retry_button.isEnabled()
    dialog._open_selected()
    assert opened == []
    dialog.table.selectRow(1)
    assert dialog.open_button.isEnabled() and dialog.folder_button.isEnabled()
    assert 'Review layout' in dialog.detail.toPlainText()
    dialog._open_selected()
    assert opened == [str(report)]
    close(dialog)


def test_result_every_output_can_be_selected_and_opened(tmp_path, monkeypatch):
    outputs = []
    for name in ['sheet-one.csv', 'sheet-two.csv', 'sheet-three.csv']:
        output = tmp_path / name
        output.write_text('00123', encoding='utf-8')
        outputs.append(str(output))
    opened = []
    monkeypatch.setattr(dialogs, 'open_result_file', lambda value: opened.append(value) or True)
    dialog = dialogs.ConversionResultDialog({'records': [{'sources': ['book.xlsx'], 'status': 'success', 'outputs': outputs}]})
    assert dialog.output_combo.count() == 3
    for index in range(3):
        dialog.output_combo.setCurrentIndex(index)
        assert dialog.open_button.isEnabled()
        dialog._open_selected()
    assert opened == outputs
    close(dialog)


def test_advanced_only_shows_targets_not_already_on_ring(monkeypatch):
    from qingjian import converter_hub
    common = {'id': 'convert:pdf', 'title': 'PDF', 'description': 'Common'}
    advanced = {'id': 'convert:images_docx', 'title': '图片装入 Word', 'description': '图片文字不会变成可编辑内容。'}
    calls = []

    def targets(sources, scope='quick'):
        calls.append(scope)
        return [common] if scope == 'quick' else [common, advanced]

    monkeypatch.setattr(converter_hub, 'available_targets', targets)
    dialog = dialogs.AdvancedConversionDialog(['a.png'])
    assert calls == ['quick', 'all']
    assert dialog.target_list.count() == 1
    assert dialog.selected_action() == 'convert:images_docx'
    assert '不会' in dialog.description_label.text()
    close(dialog)


def test_advanced_without_extra_target_disables_selection(monkeypatch):
    from qingjian import converter_hub
    monkeypatch.setattr(converter_hub, 'available_targets', lambda sources, scope='quick': [])
    dialog = dialogs.AdvancedConversionDialog(['a.pdf'])
    assert dialog.selected_action() is None and not dialog.choose_button.isEnabled()
    close(dialog)


def test_missing_preview_file_is_nonfatal_and_closed_dialog_has_no_worker(tmp_path):
    dialog = dialogs.ConversionOptionsDialog('convert:xlsx', [str(tmp_path / 'missing.csv')])
    until(lambda: '正在' not in dialog.preview_note.text())
    assert dialog.preview_table.rowCount() == 0
    close(dialog)
    assert dialog._worker is None


def test_selected_sheet_never_silently_becomes_all_before_or_after_preview_failure(tmp_path):
    source = str(tmp_path / 'missing.xlsx')
    dialog = dialogs.ConversionOptionsDialog('convert:csv', [source], {'sheet_mode': 'selected', 'sheet': 'Other document sheet'})
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    dialog.accept()
    assert accepted == [] and not dialog._closed
    assert dialog.options()['sheet_mode'] == 'selected' and 'sheet' not in dialog.options()
    assert '等待' in dialog.validation_note.text()
    until(lambda: not dialog._preview_pending)
    dialog.accept()
    assert accepted == [] and not dialog._closed
    assert '无法执行' in dialog.validation_note.text()
    assert dialog.options()['sheet_mode'] == 'selected'
    dialog.sheet_mode_combo.setCurrentIndex(0)
    dialog.accept()
    assert accepted == [True] and dialog._worker is None
    dialog.deleteLater()


def test_wrong_csv_encoding_blocks_accept_and_adjusting_encoding_can_recover(tmp_path):
    source = tmp_path / 'chinese.csv'
    source.write_bytes('编号,姓名\n00123,中文'.encode('gb18030'))
    dialog = dialogs.ConversionOptionsDialog('convert:xlsx', [str(source)], {'input_encoding': 'utf-8-sig'})
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    until(lambda: not dialog._preview_pending)
    assert dialog._preview_error
    dialog.accept()
    assert accepted == [] and '调整读取编码' in dialog.validation_note.text()
    dialog.input_encoding_combo.setCurrentIndex(0)
    dialog.accept()
    assert accepted == [] and dialog._preview_pending
    until(lambda: not dialog._preview_pending)
    assert not dialog._preview_error and dialog.preview_table.item(1, 0).text() == '00123'
    dialog.accept()
    assert accepted == [True] and dialog._worker is None
    dialog.deleteLater()


def test_preview_validates_source_before_opening_csv_or_workbook(tmp_path, monkeypatch):
    from qingjian import quick_actions
    from pathlib import Path
    validated, opened = [], []

    def reject_source(value, action):
        validated.append((str(value), action['extensions']))
        raise ValueError('不读取符号链接或目录联接。')

    def unexpected_open(path, *args, **kwargs):
        opened.append(str(path))
        raise AssertionError('Unsafe source reached file opening')

    monkeypatch.setattr(quick_actions, '_local_source', reject_source)
    monkeypatch.setattr(Path, 'open', unexpected_open)
    for suffix in ('.csv', '.xlsx'):
        dialog = dialogs.ConversionOptionsDialog('convert:csv' if suffix == '.xlsx' else 'convert:xlsx', [str(tmp_path / ('link' + suffix))])
        until(lambda: not dialog._preview_pending)
        assert '符号链接' in dialog._preview_error
        close(dialog)
    assert len(validated) == 2 and opened == []
