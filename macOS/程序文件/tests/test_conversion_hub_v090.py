from email.message import EmailMessage
import hashlib
import json
import mailbox
from pathlib import Path
import threading
import zipfile

import pytest

from qingjian.converter_hub import available_targets, run_conversion, validate_sources
from qingjian.core import format_data, format_media, format_mail, format_archive


def test_hover_is_extension_only_and_common(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('hover must not read files')
    monkeypatch.setattr(Path, 'read_bytes', fail)
    options = {row['id'] for row in available_targets(['Z:/missing.docx', 'Z:/missing.pptx'])}
    assert 'convert:pdf' in options
    assert 'convert:png' not in options
    assert 'convert:pack_zip' not in options
    assert not any('mobi' in row['id'] for row in available_targets(['Z:/a.epub']))


def test_batch_original_directories_and_no_overwrite(tmp_path):
    from PIL import Image
    sources = []
    for name in ['甲', '乙']:
        folder = tmp_path / name
        folder.mkdir()
        source = folder / '照片.jpg'
        Image.new('RGB', (19, 13), 'red').save(source)
        sources.append(source)
        (folder / '照片.png').write_bytes(b'keep existing')
    originals = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    result = run_conversion('convert:png', sources)
    assert not result['errors']
    assert len(result['outputs']) == 2
    for source, value in zip(sources, result['outputs']):
        output = Path(value)
        assert output.parent == source.parent
        assert output.name == '照片_2.png'
        with Image.open(output) as image:
            assert image.size == (19, 13)
        assert (source.parent / '照片.png').read_bytes() == b'keep existing'
        assert hashlib.sha256(source.read_bytes()).hexdigest() == originals[str(source)]


def test_batch_failure_continues_and_cancel_publishes_nothing(tmp_path):
    good, bad = tmp_path / 'ok.json', tmp_path / 'bad.json'
    good.write_text('[{"姓名":"测试","数量":2}]', encoding='utf-8')
    bad.write_text('bad json', encoding='utf-8')
    result = run_conversion('convert:xlsx', [bad, good])
    assert len(result['outputs']) == 1 and len(result['errors']) == 1
    cancelled = run_conversion('convert:yaml', [good], cancel=lambda: True)
    assert cancelled['cancelled'] and not cancelled['outputs']
    assert not list(tmp_path.glob('.qj-convert-*'))


def test_json_yaml_toml_xml_roundtrips(tmp_path):
    source = tmp_path / 'data.json'
    data = {'名称': '青简', 'enabled': True, '数量': 12, '条目': ['甲', '乙']}
    source.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    for ext in ['yaml', 'toml']:
        target = tmp_path / ('data.' + ext)
        format_data.convert(source, target)
        assert format_data.read_data(target) == data
    xml = tmp_path / 'data.xml'
    format_data.convert(source, xml)
    decoded = format_data.read_data(xml)
    assert decoded['root']['名称'] == '青简'
    assert decoded['root']['条目'] == ['甲', '乙']


def test_xlsx_keeps_formula_like_text_and_leading_zero(tmp_path):
    from openpyxl import load_workbook
    source = tmp_path / 'input.json'
    source.write_text('[{"编号":"001","文字":"=1+1"}]', encoding='utf-8')
    target = tmp_path / 'result.xlsx'
    format_data.convert(source, target)
    book = load_workbook(target, data_only=False)
    assert book.active['A2'].value == '001'
    assert book.active['B2'].value == '=1+1'
    assert book.active['B2'].data_type == 's'
    book.close()


def test_incompatible_toml_and_xml_are_explicit(tmp_path):
    source = tmp_path / 'bad.json'
    source.write_text('{"x":null}', encoding='utf-8')
    with pytest.raises(ValueError, match='TOML'):
        format_data.convert(source, tmp_path / 'x.toml')
    evil = tmp_path / 'bad.xml'
    evil.write_text('<!DOCTYPE a [<!ENTITY e SYSTEM "file:///private">]><a>&e;</a>')
    with pytest.raises(ValueError, match='实体'):
        format_data.read_data(evil)


def test_contacts_and_recurring_calendar_roundtrip(tmp_path):
    vcf = tmp_path / 'contacts.vcf'
    vcf.write_text('BEGIN:VCARD\r\nVERSION:3.0\r\nFN:示例甲\r\nN:示例甲;;;;\r\nTEL:13800000000\r\nEMAIL:test@example.invalid\r\nX-TEAM:示例组\r\nEND:VCARD\r\n', encoding='utf-8')
    table = tmp_path / 'contacts.xlsx'
    format_data.convert(vcf, table)
    copy = tmp_path / 'copy.vcf'
    format_data.convert(table, copy)
    assert 'X-TEAM:示例组' in copy.read_text(encoding='utf-8')
    assert '13800000000' in copy.read_text(encoding='utf-8')
    ics = tmp_path / 'calendar.ics'
    ics.write_bytes('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:demo@local\r\nDTSTART:20261102T010000Z\r\nDTEND:20261102T020000Z\r\nSUMMARY:周会示例\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE:20261109T010000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'.encode('utf-8'))
    csv = tmp_path / 'calendar.csv'
    format_data.convert(ics, csv)
    copy = tmp_path / 'copy.ics'
    format_data.convert(csv, copy)
    text = copy.read_text(encoding='utf-8')
    assert 'FREQ=WEEKLY' in text and 'EXDATE' in text and 'UID:demo@local' in text


def test_mail_content_and_attachment_no_remote_fetch(tmp_path):
    message = EmailMessage()
    message['Subject'] = '合成办公邮件'
    message['From'] = 'sample@example.invalid'
    message['To'] = 'reader@example.invalid'
    message.set_content('请核查附件，金额 12.50 元。')
    message.add_attachment(b'attachment payload', maintype='application', subtype='octet-stream', filename='../附件.txt')
    source = tmp_path / 'mail.eml'
    source.write_bytes(message.as_bytes())
    target = tmp_path / 'mail.docx'
    format_mail.convert(source, target)
    from docx import Document
    assert '金额 12.50' in '\n'.join(p.text for p in Document(target).paragraphs)
    folder = tmp_path / 'attachments'
    format_mail.convert(source, folder, {'attachments': True})
    files = list(folder.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == b'attachment payload'
    assert files[0].parent == folder


def test_mbox_all_messages_are_published_as_one_group(tmp_path):
    source = tmp_path / 'messages.mbox'
    box = mailbox.mbox(source)
    for text in ['第一封', '第二封']:
        message = EmailMessage()
        message['Subject'] = text
        message.set_content(text)
        box.add(message)
    box.close()
    result = run_conversion('convert:eml', [source])
    assert not result['errors'] and len(result['outputs']) == 2
    assert Path(result['outputs'][0]).parent == Path(result['outputs'][1]).parent
    assert Path(result['outputs'][0]).parent.parent == tmp_path


def test_archive_repack_and_traversal_rejection(tmp_path):
    source = tmp_path / 'source.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('材料/说明.txt', '你好'.encode())
    result = run_conversion('convert:tar.gz', [source])
    assert not result['errors'] and len(result['outputs']) == 1
    target = tmp_path / 'unpacked'
    format_archive.extract(Path(result['outputs'][0]), target)
    assert (target / '材料/说明.txt').read_text() == '你好'
    evil = tmp_path / 'bad.zip'
    with zipfile.ZipFile(evil, 'w') as archive:
        archive.writestr('../outside.txt', 'no')
    result = run_conversion('convert:extract', [evil])
    assert result['errors'] and not (tmp_path / 'outside.txt').exists()


def test_pack_multiple_different_files(tmp_path):
    files = [tmp_path / '甲.txt', tmp_path / '乙.json']
    files[0].write_text('alpha')
    files[1].write_text('{"x":1}')
    result = run_conversion('convert:pack_zip', files)
    assert not result['errors']
    with zipfile.ZipFile(result['outputs'][0]) as archive:
        assert set(archive.namelist()) == {'甲.txt', '乙.json'}
        assert archive.read('甲.txt') == b'alpha'


def test_transparency_and_animated_image_conversion(tmp_path):
    from PIL import Image
    png = tmp_path / 'transparent.png'
    Image.new('RGBA', (20, 20), (0, 0, 0, 0)).save(png)
    jpg = tmp_path / 'white.jpg'
    format_media.image_convert(png, jpg)
    with Image.open(jpg) as image:
        assert image.getpixel((1, 1)) == (255, 255, 255)
    gif = tmp_path / 'two.gif'
    Image.new('RGB', (20,20), 'red').save(gif, save_all=True, append_images=[Image.new('RGB', (20,20), 'blue')], duration=[80,120])
    tiff = tmp_path / 'frames.tiff'
    format_media.image_convert(gif, tiff)
    with Image.open(tiff) as image:
        assert image.n_frames == 2


def test_sources_reject_links_unknown_and_non_files(tmp_path):
    assert validate_sources('convert:exe', ['C:/a.txt'])
    assert validate_sources('convert:png', [tmp_path])
    assert validate_sources('convert:png', ['https://example.invalid/article'])


def test_svg_conversion_is_offline_and_blocks_external_files(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    source = tmp_path / 'shape.svg'
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="60"><rect width="100" height="60" fill="red"/></svg>')
    png = tmp_path / 'shape.png'
    format_media.vector_convert(source, png)
    from PIL import Image
    with Image.open(png) as image:
        assert image.size == (100, 60)
        assert image.getpixel((10, 10))[0] == 255
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///C:/secret.png"/></svg>')
    with pytest.raises(ValueError, match='外部资源'):
        format_media.vector_convert(source, tmp_path / 'blocked.png')


def test_no_loss_for_nonstring_keys_and_invalid_xml_attributes(tmp_path):
    with pytest.raises(ValueError, match='字段名'):
        format_data.write_table([{1: 'must not disappear'}], tmp_path / 'invalid.xlsx')
    with pytest.raises(ValueError, match='字段名'):
        format_data.write_table({1: 'must not disappear'}, tmp_path / 'invalid-single.xlsx')
    source = tmp_path / 'invalid.json'
    source.write_text('{"root":{"@bad name":"x"}}')
    with pytest.raises(ValueError, match='属性名称'):
        format_data.convert(source, tmp_path / 'invalid.xml')


def test_duplicate_attachment_names_never_replace_each_other(tmp_path):
    message = EmailMessage()
    message.set_content('Example')
    for name, data in [('003_report.txt', b'first'), ('report.txt', b'second'), ('report.txt', b'third')]:
        message.add_attachment(data, maintype='application', subtype='octet-stream', filename=name)
    source = tmp_path / 'duplicate.eml'
    source.write_bytes(message.as_bytes())
    target = tmp_path / 'attachments'
    format_mail.convert(source, target, {'attachments': True})
    assert {path.read_bytes() for path in target.iterdir()} == {b'first', b'second', b'third'}


def test_contact_parameter_and_org_structure_roundtrip(tmp_path):
    import vobject
    source = tmp_path / 'complex.vcf'
    source.write_bytes(b'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Example\r\nN:Example;;;;\r\nTEL;TYPE=HOME,VOICE:123\r\nTEL;TYPE=WORK,FAX:456\r\nORG:Company;Dept\r\nEND:VCARD\r\n')
    table = tmp_path / 'complex.xlsx'
    format_data.convert(source, table)
    target = tmp_path / 'restored.vcf'
    format_data.convert(table, target)
    restored = vobject.readOne(target.read_text(encoding='utf-8'))
    assert restored.contents['tel'][0].params['TYPE'] == ['HOME', 'VOICE']
    assert restored.contents['tel'][1].params['TYPE'] == ['WORK', 'FAX']
    assert restored.org.value == ['Company', 'Dept']


def test_svgz_decompression_is_bounded(tmp_path):
    import gzip
    source = tmp_path / 'huge.svgz'
    source.write_bytes(gzip.compress(b' ' * (32 * 1024 * 1024 + 1)))
    with pytest.raises(ValueError, match='内容过大'):
        format_media.vector_convert(source, tmp_path / 'huge.svg')


def test_postscript_retains_multiple_rendered_pages(tmp_path, monkeypatch):
    from PIL import Image
    source = tmp_path / 'two.ps'
    source.write_text('%!PS')
    # A render stub still needs a complete private resource layout on macOS.
    # Keep the production resource checks active before the mocked process.
    gs = tmp_path / 'Ghostscript/bin/gs'
    gs.parent.mkdir(parents=True)
    gs.write_bytes(b'test-render-stub')
    gs.chmod(0o755)
    resources = gs.parent.parent / 'share/ghostscript/10.07.0'
    for name in ('Resource/Init/gs_init.ps', 'iccprofiles/default_rgb.icc'):
        entry = resources / name
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b'fixture-resource')
    monkeypatch.setattr(format_media, 'engine', lambda _: gs)
    def render(args, cancel):
        if format_media.sys.platform == 'darwin':
            assert '-sGenericResourceDir=' + str(resources / 'Resource') + '/' in args
            assert '-sICCProfilesDir=' + str(resources / 'iccprofiles') + '/' in args
        pattern = next(str(a)[len('-sOutputFile='):] for a in args if str(a).startswith('-sOutputFile='))
        for number, color in [(1, 'red'), (2, 'blue')]:
            Image.new('RGB', (20,20), color).save(pattern % number)
    monkeypatch.setattr(format_media, 'run_engine', render)
    result = format_media.vector_convert(source, tmp_path / 'pages.jpg')
    assert len(result['outputs']) == 2
    assert all(Path(p).exists() for p in result['outputs'])


@pytest.mark.parametrize('extension', ['.txt', '.md', '.markdown'])
def test_utf8_entry_keeps_newlines_and_source_bytes(tmp_path, extension):
    source = tmp_path / ('encoding' + extension)
    original = '  中文\r\n第二行\n\t末尾  '.encode('gb18030')
    source.write_bytes(original)
    assert 'convert:utf8' in {item['id'] for item in available_targets([source])}
    result = run_conversion('convert:utf8', [source])
    assert not result['errors'], result
    assert Path(result['outputs'][0]).read_bytes() == original.decode('gb18030').encode('utf-8')
    assert source.read_bytes() == original


def test_published_results_survive_temporary_cleanup_error(tmp_path, monkeypatch):
    import qingjian.converter_hub as hub
    original_cleanup = hub.tempfile.TemporaryDirectory.cleanup
    def fail_cleanup(handle):
        original_cleanup(handle)
        raise PermissionError('Synthetic cleanup contention')
    source = tmp_path / 'input.json'
    source.write_text('[{"name":"keep the result"}]')
    monkeypatch.setattr(hub.tempfile.TemporaryDirectory, 'cleanup', fail_cleanup)
    result = run_conversion('convert:csv', [source])
    assert not result['errors']
    assert result['records'][0]['status'] == 'success'
    assert len(result['outputs']) == 1 and Path(result['outputs'][0]).exists()
    assert any('临时文件' in warning for warning in result['warnings'])


def test_toml_fallback_on_python_without_tomllib(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def import_module(name, *args, **kwargs):
        if name == 'tomllib':
            raise ImportError('Python 3.10 compatibility check')
        return original(name, *args, **kwargs)
    source = tmp_path / 'fallback.toml'
    source.write_text('name = "example"\nquantity = 2\n', encoding='utf-8')
    monkeypatch.setattr(builtins, '__import__', import_module)
    assert format_data.read_data(source) == {'name': 'example', 'quantity': 2}


def test_xlsx_field_names_are_text_not_formulas(tmp_path):
    from openpyxl import load_workbook
    source = tmp_path / 'headers.json'
    source.write_text('[{"=1+1":"literal field name"}]', encoding='utf-8')
    target = tmp_path / 'headers.xlsx'
    format_data.convert(source, target)
    book = load_workbook(target, data_only=False)
    try:
        assert book.active['A1'].value == '=1+1'
        assert book.active['A1'].data_type == 's'
        assert book.active['A2'].value == 'literal field name'
    finally:
        book.close()
