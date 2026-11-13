"""Office-first catalogue and transaction/retry data for local conversions."""
from pathlib import Path

import pytest

from qingjian import converter_hub as hub


def ids(sources, **kwargs):
    return [row['id'] for row in hub.available_targets(sources, **kwargs)]


@pytest.mark.parametrize('scope', ['quick', 'all'])
def test_pdf_table_directions_are_removed_everywhere(tmp_path, scope):
    source = tmp_path / 'material.pdf'
    source.write_bytes(b'%PDF-test')
    offered = ids([source], scope=scope)
    assert 'convert:csv' not in offered and 'convert:xlsx' not in offered
    assert hub.validate_sources('convert:csv', [source])
    assert hub.validate_sources('convert:xlsx', [source])
    assert ids([source])[:4] == ['convert:docx', 'convert:jpg', 'convert:png', 'convert:txt']


@pytest.mark.parametrize('suffix', ['json', 'yaml', 'toml', 'xml', 'vcf', 'ics', 'epub', 'unrecognized'])
def test_specialist_inputs_have_no_quick_ring(suffix):
    source = 'Z:/missing/input.' + suffix
    assert ids([source]) == []
    if suffix != 'unrecognized':
        assert ids([source], scope='all')


def test_advanced_routes_are_explicit_without_polluting_ring():
    assert 'convert:epub' not in ids(['Z:/report.docx'])
    assert 'convert:epub' in ids(['Z:/report.docx'], scope='all')
    assert 'convert:docx' not in ids(['Z:/photo.png'])
    assert 'convert:docx' in ids(['Z:/photo.png'], scope='all')
    assert 'convert:pptx' not in ids(['Z:/report.pdf'])
    assert 'convert:pptx' in ids(['Z:/report.pdf'], scope='all')
    assert not {'convert:json', 'convert:vcf', 'convert:ics'} & set(ids(['Z:/sheet.xlsx']))


def test_common_mixed_targets_are_not_general_pack_actions(tmp_path):
    paths = [tmp_path / 'report.docx', tmp_path / 'slides.pptx']
    for source in paths:
        source.write_bytes(b'placeholder')
    for scope in ['quick', 'all']:
        offered = ids(paths, scope=scope)
        assert 'convert:pdf' in offered
        assert 'convert:png' not in offered and 'convert:pack_zip' not in offered
    assert not hub.validate_sources('convert:pack_zip', paths)


def test_hover_saved_preferences_are_stable_and_never_read_sources(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('hover must only inspect extensions')
    monkeypatch.setattr(Path, 'read_bytes', fail)
    monkeypatch.setattr(Path, 'stat', fail)
    sources = ['Z:/photo.png']
    before = hub.available_targets(sources)
    preferred = ['convert:tiff', 'convert:jpg', 'convert:pack_zip', 'convert:bad']
    rows = hub.available_targets(sources, preferred=preferred)
    assert rows[0]['id'] == 'convert:jpg'
    assert hub.available_targets(sources, preferred=preferred) == rows
    common = [row for row in rows if row['group'] == 'common']
    more = [row for row in rows if row['group'] == 'more']
    assert len(common) <= 6 and rows == common + more
    assert more[0]['id'] == 'convert:tiff'
    assert set(row['id'] for row in rows) == set(row['id'] for row in before)


def test_word_and_ppt_priorities_keep_common_formats_first():
    word = hub.available_targets(['Z:/report.docx'])
    assert {row['id'] for row in word if row['group'] == 'common'} == {'convert:pdf', 'convert:txt'}
    assert all(row['group'] == 'more' for row in word if row['id'] in {'convert:doc', 'convert:rtf', 'convert:odt'})
    ppt = hub.available_targets(['Z:/slides.pptx'])
    assert {row['id'] for row in ppt if row['group'] == 'common'} == {'convert:pdf', 'convert:jpg', 'convert:png'}
    assert all(row['group'] == 'more' for row in ppt if row['id'] in {'convert:docx', 'convert:txt'})


def test_labels_explain_extracting_text_and_inserting_images():
    rows = {row['id']: row for row in hub.available_targets(['Z:/report.pdf'])}
    assert rows['convert:docx']['title'] == '文字PDF转Word'
    assert rows['convert:txt']['title'] == '提取文字'
    rows = {row['id']: row for row in hub.available_targets(['Z:/slides.pptx'], scope='all')}
    assert rows['convert:docx']['title'] == '提取PPT文字到Word'
    rows = {row['id']: row for row in hub.available_targets(['Z:/image.png'], scope='all')}
    assert '不会被识别' in rows['convert:docx']['description']
    rows = {row['id']: row for row in hub.available_targets(['Z:/a.png', 'Z:/b.jpg'])}
    assert rows['convert:images_pdf']['title'] == '多图合并PDF'
    assert rows['convert:pdf']['title'] == '每张分别转PDF'


def test_safe_options_and_retry_record_do_not_accept_control_injection(tmp_path, monkeypatch):
    source = tmp_path / 'input.png'
    source.write_bytes(b'original')
    submitted = {'quality': 81, 'max_edge': 1000, 'jpeg_background': '#fafafa',
                 'dpi': 150, 'input_encoding': 'auto', 'input_delimiter': ';',
                 '_cancel': 'untrusted', '_sources': ['untrusted'], 'timeout': 1}
    captured = {}
    cancel = lambda: False

    def convert(value, target, key, options):
        captured.update(options)
        target.write_bytes(b'result')
        return {'outputs': [target]}

    monkeypatch.setattr(hub, '_convert', convert)
    result = hub.run_conversion('convert:jpg', [source], cancel=cancel, options=submitted)
    assert not result['errors'], result
    assert captured['_cancel'] is cancel
    assert '_sources' not in captured and 'timeout' not in captured
    assert captured['quality'] == 81 and captured['input_delimiter'] == ';'
    assert submitted['_cancel'] == 'untrusted'  # caller object unchanged
    record = result['records'][0]
    assert record['sources'] == [str(source)]
    assert record['action_id'] == result['action_id'] == 'convert:jpg'
    assert record['options'] == result['options']
    assert '_cancel' not in record['options']
    assert record['outputs'] == result['outputs']
    assert source.read_bytes() == b'original'


def test_multisheet_outputs_retain_names_and_publish_together(tmp_path, monkeypatch):
    source = tmp_path / 'book.xlsx'
    source.write_bytes(b'original workbook')

    def convert(value, target, key, options):
        assert options['sheet_mode'] == 'all'
        outputs = []
        for name, payload in [('销售', b'sales'), ('库存', b'inventory')]:
            sheet = target.with_name('result_' + name + '.csv')
            sheet.write_bytes(payload)
            outputs.append(sheet)
        return {'outputs': outputs}

    monkeypatch.setattr(hub, '_convert', convert)
    result = hub.run_conversion('convert:csv', [source], options={'sheet_mode': 'all'})
    assert not result['errors'], result
    assert {Path(output).name for output in result['outputs']} == {'book_销售.csv', 'book_库存.csv'}
    assert len({Path(output).parent for output in result['outputs']}) == 1
    assert Path(result['records'][0]['output']).is_dir()


def test_incomplete_multifile_conversion_does_not_publish_partial_result(tmp_path, monkeypatch):
    source = tmp_path / 'slides.pptx'
    source.write_bytes(b'original slides')

    def convert(value, target, key, options):
        target.write_bytes(b'page one')
        return {'outputs': [target, target.with_name('missing.jpg')]}

    monkeypatch.setattr(hub, '_convert', convert)
    result = hub.run_conversion('convert:jpg', [source])
    assert result['errors'] and not result['outputs']
    assert not result['records'][0]['outputs']
    assert list(tmp_path.iterdir()) == [source]


def test_multisheet_metadata_names_first_sheet_too(tmp_path, monkeypatch):
    source = tmp_path / 'book.xlsx'
    source.write_bytes(b'original workbook')

    def convert(value, target, key, options):
        second = target.with_name('result_002_inventory.csv')
        target.write_bytes(b'first')
        second.write_bytes(b'second')
        return {'outputs': [target, second], 'output_sheets': ['销售', '库存']}

    monkeypatch.setattr(hub, '_convert', convert)
    result = hub.run_conversion('convert:csv', [source], options={'sheet_mode': 'all'})
    assert not result['errors'], result
    assert {Path(output).name for output in result['outputs']} == {'book_销售.csv', 'book_库存.csv'}


def test_sheet_conversion_uses_ordinary_adapter(tmp_path, monkeypatch):
    from qingjian.core import format_data, format_office
    source = tmp_path / 'ordinary.csv'
    source.write_text(',same,same\n001,2,3\n', encoding='utf-8')
    invoked = []

    def ordinary(value, target, options):
        invoked.append((value, dict(options)))
        target.write_bytes(b'ordinary workbook')
        return {'outputs': [target]}

    def structured(*args, **kwargs):
        raise AssertionError('ordinary conversion must not use structured-data schema')

    monkeypatch.setattr(format_office, 'convert', ordinary)
    monkeypatch.setattr(format_data, 'convert', structured)
    result = hub.run_conversion('convert:xlsx', [source], options={'input_encoding': 'utf-8'})
    assert not result['errors'], result
    assert len(invoked) == 1 and invoked[0][1]['input_encoding'] == 'utf-8'


def test_table_utf8_action_retains_original_extension(tmp_path, monkeypatch):
    source = tmp_path / 'encoded.tsv'
    source.write_bytes(b'A\tB\r\n001\t002\r\n')
    assert 'convert:utf8' in ids([source])

    def convert(value, target, key, options):
        assert target.suffix == '.tsv'
        assert options['encoding'] == 'utf-8'
        target.write_bytes(source.read_bytes())
        return {'outputs': [target]}

    monkeypatch.setattr(hub, '_convert', convert)
    result = hub.run_conversion('convert:utf8', [source], options={'encoding': 'gb18030'})
    assert not result['errors']
    assert Path(result['outputs'][0]).name == 'encoded_UTF8.tsv'


def test_invalid_option_object_returns_actionable_failure(tmp_path):
    result = hub.run_conversion('convert:jpg', [tmp_path / 'missing.png'], options={'sheet': ['bad']})
    assert result['errors'] and not result['outputs']
