"""Read saved mail locally; never contact mail accounts or remote images."""
from __future__ import annotations

from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
import html
import mailbox
from pathlib import Path
import re

from .common import ensure_cancel, safe_name, unique_path


def _clean_text(value):
    # Some Outlook storage streams are padded with NUL bytes. These are not
    # visible mail content and cannot be written to Word's XML. Attachments
    # always keep their original bytes.
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', str(value or ''))


def _message(source):
    if source.suffix.lower() == '.msg':
        import extract_msg
        with extract_msg.openMsg(str(source)) as message:
            result = EmailMessage(policy=policy.default)
            cleaned = False
            for key, value in [('Subject', message.subject), ('From', message.sender), ('To', message.to),
                               ('Cc', message.cc), ('Date', str(message.date or ''))]:
                if value:
                    text = _clean_text(value)
                    cleaned = cleaned or text != str(value)
                    result[key] = text.replace('\r', ' ').replace('\n', ' ')
            body = message.body or ''
            clean_body = _clean_text(body)
            result.set_content(clean_body)
            result._qingjian_text_cleaned = cleaned or body != clean_body
            for index, attachment in enumerate(message.attachments):
                data = attachment.data
                if not isinstance(data, bytes):
                    # Embedded MSGs expose a Message object; preserve its bytes
                    # where available rather than pretending an empty file is a copy.
                    if hasattr(data, 'exportBytes'):
                        data = data.exportBytes()
                    else:
                        raise ValueError('该 MSG 包含尚不支持的嵌入对象，请先在 Outlook 中另存附件。')
                name = attachment.longFilename or attachment.shortFilename or f'附件_{index+1}.bin'
                result.add_attachment(data, maintype='application', subtype='octet-stream', filename=name)
            return result
    return BytesParser(policy=policy.default).parsebytes(source.read_bytes())


def _body(message):
    from bs4 import BeautifulSoup
    part = message.get_body(preferencelist=('plain', 'html'))
    if part is None:
        return ''
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        content = part.get_payload(decode=True).decode('utf-8', errors='replace')
    if part.get_content_type() == 'text/html':
        soup = BeautifulSoup(content, 'html.parser')
        for node in soup(['script', 'style', 'iframe', 'object']):
            node.decompose()
        return soup.get_text('\n', strip=True)
    return str(content)


def convert(source, target, options=None):
    options = options or {}
    cancel = options.get('_cancel')
    source, target = Path(source), Path(target)
    ensure_cancel(cancel)
    if source.suffix.lower() == '.mbox':
        if target.suffix.lower() != '.eml':
            raise ValueError('MBOX 请先拆分为 EML 后转换。')
        outputs = []
        box = mailbox.mbox(source, create=False)
        try:
            for index, key in enumerate(box.iterkeys()):
                ensure_cancel(cancel)
                if index >= 10000:
                    raise ValueError('邮件超过一万封，请分批处理。')
                output = target if index == 0 else target.with_name(f'{target.stem}_{index+1:04d}.eml')
                output.write_bytes(box.get_bytes(key, from_=False))
                outputs.append(str(output))
        finally:
            box.close()
        if not outputs:
            raise ValueError('MBOX 中没有邮件。')
        return {'changes': len(outputs), 'outputs': outputs, 'warnings': []}
    message = _message(source)
    if options.get('attachments'):
        target.mkdir()
        count = 0
        for part in message.iter_attachments():
            ensure_cancel(cancel)
            count += 1
            name = safe_name(part.get_filename() or f'附件_{count}.bin')
            output = unique_path(target, name)
            payload = part.get_payload(decode=True)
            if payload is None:
                payload = part.as_bytes(policy=policy.default)
            with output.open('xb') as stream:
                stream.write(payload)
        if not count:
            raise ValueError('此邮件没有附件。')
        return {'changes': count, 'outputs': [str(target)], 'warnings': []}
    if target.suffix.lower() == '.eml':
        target.write_bytes(message.as_bytes(policy=policy.default))
        return {'changes': 1, 'outputs': [str(target)], 'warnings': []}
    labels = [('Subject', '主题'), ('From', '发件人'), ('To', '收件人'), ('Cc', '抄送'), ('Date', '日期')]
    lines = [label + '：' + str(message[key]) for key, label in labels if message.get(key)]
    body = _body(message)
    text = '\n'.join(lines) + '\n\n' + body
    cleaned = bool(getattr(message, '_qingjian_text_cleaned', False))
    attachment_names = [str(part.get_filename() or '未命名附件') for part in message.iter_attachments()]
    if attachment_names:
        text += '\n\n附件：\n' + '\n'.join(attachment_names)
    clean_text = _clean_text(text)
    cleaned = cleaned or clean_text != text
    text = clean_text
    out = target.suffix.lower()
    if out == '.txt':
        target.write_text(text, encoding='utf-8')
    elif out == '.html':
        target.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' +
                          html.escape(str(message.get('Subject', '邮件'))) + '</title><body><pre style="white-space:pre-wrap">' +
                          html.escape(text) + '</pre></body></html>', encoding='utf-8')
    elif out in {'.docx', '.pdf'}:
        from docx import Document
        document = Document()
        document.add_heading(_clean_text(message.get('Subject', '邮件归档')), level=1)
        for line in text.splitlines():
            document.add_paragraph(line)
        intermediate = target if out == '.docx' else target.with_suffix('.mail.docx')
        document.save(intermediate)
        if intermediate != target:
            from .format_office import convert as office_convert
            office_convert(intermediate, target, options)
    else:
        raise ValueError('不支持的邮件输出格式。')
    ensure_cancel(cancel)
    warnings = ['邮件正文按归档格式重新排版，远程图片不联网加载。']
    if cleaned:
        warnings.append('已移除邮件文本中的 NUL 填充或无效控制字符，附件字节保持不变。')
    if attachment_names:
        warnings.append('文档中列出附件名称；附件内容请使用“提取附件”。')
    return {'changes': 1, 'outputs': [str(target)], 'warnings': warnings}
