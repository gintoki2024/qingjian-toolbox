"""Real UI-to-worker office conversion, including preflight and saved defaults."""
import csv
from pathlib import Path
import time

from openpyxl import Workbook, load_workbook
from PIL import Image
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import pytest

from qingjian.app import Window
from qingjian.converter_dialogs import ConversionOptionsDialog
from qingjian.converter_preferences import ConversionPreferences


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(5)
    assert predicate()


@pytest.fixture
def toolbox(tmp_path):
    window = Window(start_home=True, data_path=tmp_path / 'profile.json')
    window.show()
    window.show_converter()
    QApplication.processEvents()
    yield window
    if window.worker:
        window.worker.requestInterruption()
        wait_for(lambda: window.worker is None)
    window.converter_window.shutdown()
    window.converter_window.deleteLater()
    window.close()
    window.deleteLater()


def schedule_preflight(accept=True):
    state = {}
    deadline = time.monotonic() + 10
    def finish():
        candidates = [w for w in QApplication.topLevelWidgets() if isinstance(w, ConversionOptionsDialog) and w.isVisible()]
        if not candidates:
            if time.monotonic() < deadline:
                QTimer.singleShot(10, finish)
            return
        dialog = candidates[0]
        worker = getattr(dialog, '_worker', None)
        if worker and worker.isRunning() and time.monotonic() < deadline:
            QTimer.singleShot(10, finish)
            return
        state['enabled'] = dialog.isEnabled()
        state['options'] = dialog.options()
        dialog.accept() if accept else dialog.reject()
    QTimer.singleShot(10, finish)
    return state


def test_multisheet_preflight_exports_all_names_and_preserves_duplicate_empty_headers(toolbox, tmp_path):
    source = tmp_path / '部门台账.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.title = '第一组'
    sheet.append([None, '姓名', '姓名'])
    sheet.append(['00001', '甲', '乙'])
    book.create_sheet('第二组').append(['00999', '丙'])
    book.save(source)
    book.close()
    state = schedule_preflight()
    assert toolbox.request_conversion('convert:csv', [str(source)])
    assert state['enabled'] and state['options']['sheet_mode'] == 'all'
    wait_for(lambda: toolbox.worker is None)
    raw = toolbox._raw_conversion_result
    assert not raw['errors'] and len(raw['outputs']) == 2
    assert any('第一组' in Path(p).name for p in raw['outputs'])
    assert any('第二组' in Path(p).name for p in raw['outputs'])
    first = next(Path(p) for p in raw['outputs'] if '第一组' in Path(p).name)
    rows = list(csv.reader(first.open(encoding='utf-8-sig', newline='')))
    assert rows == [['', '姓名', '姓名'], ['00001', '甲', '乙']]
    assert source.exists() and not toolbox._busy


def test_csv_import_options_travel_through_real_worker_without_type_guessing(toolbox, tmp_path):
    source = tmp_path / '旧系统名单.csv'
    source.write_bytes('工号;工号\n0001;13800000000\n'.encode('gb18030'))
    options = {'input_encoding': 'gb18030', 'input_delimiter': ';'}
    assert toolbox.start_quick_action('convert:xlsx', [str(source)], options)
    wait_for(lambda: toolbox.worker is None)
    raw = toolbox._raw_conversion_result
    assert not raw['errors']
    book = load_workbook(raw['outputs'][0])
    assert book.active['A2'].value == '0001' and book.active['B2'].value == '13800000000'
    assert book.active['A1'].value == book.active['B1'].value == '工号'
    book.close()
    assert raw['records'][0]['options'] == options


def test_saved_image_settings_apply_to_later_one_step_conversion(toolbox, tmp_path):
    source = tmp_path / '透明材料.png'
    Image.new('RGBA', (120, 80), (255, 0, 0, 0)).save(source)
    defaults = {'quality': 90, 'max_edge': 60, 'jpeg_background': '#000000'}
    toolbox.converter_window.preferences.remember_options([str(source)], defaults)
    assert toolbox.request_conversion('convert:jpg', [str(source)])
    wait_for(lambda: toolbox.worker is None)
    raw = toolbox._raw_conversion_result
    assert not raw['errors']
    with Image.open(raw['outputs'][0]) as picture:
        assert picture.size == (60, 40)
        assert max(picture.convert('RGB').getpixel((20, 20))) < 10
    assert source.exists()


def test_cancel_preflight_does_not_create_outputs_or_hold_execution_slot(toolbox, tmp_path):
    source = tmp_path / '待确认.csv'
    source.write_text('编号,姓名\n0001,甲\n', encoding='utf-8')
    state = schedule_preflight(accept=False)
    assert not toolbox.request_conversion('convert:xlsx', [str(source)])
    QApplication.processEvents()
    assert state['enabled'] and toolbox.worker is None and not toolbox._busy
    assert not list(tmp_path.glob('*.xlsx'))


def test_merging_images_does_not_erase_saved_format_parameters(tmp_path):
    path = tmp_path / 'converter-defaults.json'
    sources = ['one.png', 'two.png']
    prefs = ConversionPreferences(path)
    prefs.remember_options(sources, {'quality': 85, 'max_edge': 1000})
    # Combining pages exposes no image codec fields. It must retain prior defaults.
    prefs.remember_options(sources, {})
    reloaded = ConversionPreferences(path)
    assert reloaded.options(['another.jpg']) == {'quality': 85, 'max_edge': 1000}
    prefs.remember_options(['book.xlsx'], {'sheet_mode': 'selected', 'sheet': '客户名单'})
    assert ConversionPreferences(path).options(['other.xlsx']) == {'sheet_mode': 'all'}
    assert '客户名单' not in path.read_text(encoding='utf-8')
