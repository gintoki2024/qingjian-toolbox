"""Synthetic, offline conversion checks shared by source and frozen releases."""
from pathlib import Path
import json


def run(folder, check):
    from PIL import Image
    from docx import Document
    from .converter_hub import run_conversion, available_targets
    root = Path(folder) / 'converter'
    root.mkdir()

    def action(key, paths):
        result = run_conversion('convert:' + key, paths)
        assert not result['errors'], result
        assert result['outputs'] and all(Path(path).exists() for path in result['outputs'])
        return result

    def data():
        source = root / 'data.json'
        source.write_text('[{"姓名":"合成示例","编号":"001","数量":3}]', encoding='utf-8')
        result = action('xlsx', [source])
        from openpyxl import load_workbook
        book = load_workbook(result['outputs'][0])
        assert book.active['A2'].value == '合成示例' and book.active['B2'].value == '001'
        book.close()
        result = action('yaml', [source])
        result = action('json', result['outputs'])
        assert json.loads(Path(result['outputs'][0]).read_text(encoding='utf-8'))[0]['数量'] == 3
        return {'xlsx_yaml_json': True}
    check('converter_structured_data', data)

    def contacts():
        source = root / 'contact.vcf'
        source.write_bytes('BEGIN:VCARD\r\nVERSION:3.0\r\nFN:合成联系人\r\nN:合成联系人;;;;\r\nTEL:13800000000\r\nEND:VCARD\r\n'.encode())
        rows = action('xlsx', [source])
        back = action('vcf', rows['outputs'])
        assert '13800000000' in Path(back['outputs'][0]).read_text(encoding='utf-8')
        return {'contact_roundtrip': True}
    check('converter_contacts', contacts)

    def calendar():
        source = root / 'meeting.ics'
        source.write_bytes('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:sample@local\r\nSUMMARY:示例周会\r\nDTSTART:20261102T010000Z\r\nRRULE:FREQ=WEEKLY;COUNT=2\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'.encode())
        rows = action('csv', [source])
        back = action('ics', rows['outputs'])
        assert 'FREQ=WEEKLY' in Path(back['outputs'][0]).read_text(encoding='utf-8')
        return {'recurrence_preserved': True}
    check('converter_calendar', calendar)

    def mail():
        import extract_msg
        from email.message import EmailMessage
        source = root / 'mail.eml'
        message = EmailMessage()
        message['Subject'] = '合成归档邮件'
        message['From'] = 'sample@example.invalid'
        message.set_content('OFFLINE_MAIL_CHECK')
        message.add_attachment(b'example attachment', maintype='application', subtype='octet-stream', filename='example.txt')
        source.write_bytes(message.as_bytes())
        result = action('docx', [source])
        assert 'OFFLINE_MAIL_CHECK' in '\n'.join(p.text for p in Document(result['outputs'][0]).paragraphs)
        result = action('attachments', [source])
        assert (Path(result['outputs'][0]) / 'example.txt').read_bytes() == b'example attachment'
        return {'email_body_and_attachment': True, 'msg_reader_imported': bool(extract_msg)}
    check('converter_mail', mail)

    def epub():
        source = root / 'book.md'
        source.write_text('# 离线电子书\n\nEPUB_CPU_ONLY\n\n轻量章节内容。', encoding='utf-8')
        output = action('epub', [source])
        back = action('docx', output['outputs'])
        assert 'EPUB_CPU_ONLY' in '\n'.join(p.text for p in Document(back['outputs'][0]).paragraphs)
        return {'epub_roundtrip': True, 'gpu_required': False}
    check('converter_lightweight_epub', epub)

    def images():
        import pillow_heif
        pillow_heif.register_heif_opener()
        source = root / 'phone.heic'
        Image.new('RGB', (24, 16), '#326898').save(source, quality=90)
        output = action('png', [source])
        with Image.open(output['outputs'][0]) as image:
            assert image.size == (24, 16)
        original = root / 'still.png'
        Image.new('RGBA', (24, 16), (255, 0, 0, 128)).save(original)
        for key in ('webp', 'avif', 'jp2', 'ico', 'tiff', 'gif'):
            output = action(key, [original])
            with Image.open(output['outputs'][0]) as image:
                image.load()
        return {'heic_and_six_image_outputs': True}
    check('converter_image_codecs', images)

    def images_document():
        paths = []
        for index, color in enumerate(('red', 'blue')):
            source = root / f'page{index}.png'
            Image.new('RGB', (120,80), color).save(source)
            paths.append(source)
        output = action('images_pptx', paths)
        from pptx import Presentation
        assert len(Presentation(output['outputs'][0]).slides) == 2
        output = action('images_pdf', paths)
        from pypdf import PdfReader
        assert len(PdfReader(output['outputs'][0]).pages) == 2
        rendered = action('png', output['outputs'])
        assert len(rendered['outputs']) == 2
        return {'merged_documents_and_all_pdf_pages': True}
    check('converter_image_documents_and_pdf_pages', images_document)

    def archives():
        import zipfile
        source = root / 'archive.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('材料/说明.txt', 'ARCHIVE_CHECK'.encode())
        compressed = action('7z', [source])
        extracted = action('extract', compressed['outputs'])
        assert (Path(extracted['outputs'][0]) / '材料/说明.txt').read_text() == 'ARCHIVE_CHECK'
        return {'seven_zip_repack_and_extract': True}
    check('converter_7zip_offline_engine', archives)

    def postscript():
        source = root / 'shape.eps'
        source.write_text('%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 60\n1 0 0 setrgbcolor\n0 0 100 60 rectfill\nshowpage\n%%EOF\n', encoding='ascii')
        result = action('png', [source])
        with Image.open(result['outputs'][0]) as image:
            assert image.width > 0 and image.height > 0
        return {'ghostscript_offline': True}
    check('converter_postscript_offline_engine', postscript)

    def ofd():
        source = root / 'ofd-source.txt'
        source.write_text('OFD_OFFLINE_CHECK\n合成公文核验。', encoding='utf-8')
        output = action('ofd', [source])
        text = action('txt', output['outputs'])
        assert 'OFD_OFFLINE_CHECK' in Path(text['outputs'][0]).read_text(encoding='utf-8')
        pdf = action('pdf', output['outputs'])
        from pypdf import PdfReader
        assert len(PdfReader(pdf['outputs'][0]).pages) >= 1
        return {'ofd_without_system_java': True}
    check('converter_ofd_offline_engine', ofd)

    def registry():
        office = {row['id'] for row in available_targets(['Z:/a.docx'])}
        assert {'convert:pdf', 'convert:epub', 'convert:md'} <= office
        mixed = {row['id'] for row in available_targets(['Z:/a.docx', 'Z:/a.pptx'])}
        assert 'convert:pdf' in mixed and 'convert:png' not in mixed
        return {'file_type_targets': True}
    check('converter_direction_catalogue', registry)
