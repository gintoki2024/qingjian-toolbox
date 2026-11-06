"""Offline data, address-book and calendar conversion with explicit schemas."""
from __future__ import annotations

import csv
from datetime import date, datetime
import io
import json
from pathlib import Path
import re
import uuid
import xml.etree.ElementTree as ET

from .common import ensure_cancel

DATA = {'.json', '.jsonl', '.ndjson', '.yaml', '.yml', '.toml', '.xml'}
TABLES = {'.csv', '.tsv', '.xlsx'}
MAX_ROWS = 100000


def read_text(path):
    data = Path(path).read_bytes()
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError('无法识别文本编码，请先另存为 UTF-8。')


def _xml_value(node):
    value = {'@' + key: item for key, item in node.attrib.items()}
    for child in node:
        item = _xml_value(child)
        if child.tag in value:
            if not isinstance(value[child.tag], list):
                value[child.tag] = [value[child.tag]]
            value[child.tag].append(item)
        else:
            value[child.tag] = item
    content = (node.text or '').strip()
    if not value:
        return content
    if content:
        value['#text'] = content
    if any((child.tail or '').strip() for child in node):
        raise ValueError('XML 含交错文本，无法自动转换而完整保留顺序。')
    return value


def _xml_node(name, value):
    if not re.fullmatch(r'[A-Za-z_\u0080-\uffff][\w.\-\u0080-\uffff]*', str(name)):
        raise ValueError('XML 字段名无效或含命名空间，请先整理字段：' + str(name))
    node = ET.Element(str(name))
    if isinstance(value, dict):
        for key, item in value.items():
            if key.startswith('@'):
                if not re.fullmatch(r'[A-Za-z_\u0080-\uffff][\w.\-\u0080-\uffff]*', key[1:]):
                    raise ValueError('XML 属性名称无效：' + key)
                node.set(key[1:], str(item))
            elif key == '#text':
                node.text = str(item)
            else:
                for part in item if isinstance(item, list) else [item]:
                    node.append(_xml_node(key, part))
    elif isinstance(value, list):
        for item in value:
            node.append(_xml_node('item', item))
    elif value is not None:
        node.text = str(value).lower() if isinstance(value, bool) else str(value)
    return node


def read_data(source, cancel=None):
    source = Path(source)
    ext = source.suffix.lower()
    if ext in TABLES:
        return read_table(source, cancel)
    text = read_text(source)
    if ext == '.json':
        return json.loads(text)
    if ext in {'.jsonl', '.ndjson'}:
        values = []
        for line in text.splitlines():
            ensure_cancel(cancel)
            if line.strip():
                values.append(json.loads(line))
            if len(values) > MAX_ROWS:
                raise ValueError('数据超过十万条，请分批转换。')
        return values
    if ext in {'.yaml', '.yml'}:
        import yaml
        return yaml.safe_load(text)
    if ext == '.toml':
        try:
            import tomllib
        except ImportError:  # Python 3.10 remains a supported source runtime.
            import tomli as tomllib
        return tomllib.loads(text)
    if ext == '.xml':
        if re.search(r'<!\s*(DOCTYPE|ENTITY)', text, re.I):
            raise ValueError('不读取包含外部实体或文档类型声明的 XML。')
        root = ET.fromstring(text)
        return {root.tag: _xml_value(root)}
    raise ValueError('不支持的数据格式：' + ext)


def read_table(source, cancel=None):
    source = Path(source)
    if source.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(source, read_only=True, data_only=False, keep_links=False)
        try:
            if len(book.worksheets) != 1:
                raise ValueError('该文件有多个工作表，请先在表格工具中选择或拆分工作表后转换。')
            values = list(book.active.iter_rows(values_only=True))
        finally:
            book.close()
    else:
        values = list(csv.reader(io.StringIO(read_text(source)), delimiter='\t' if source.suffix.lower() == '.tsv' else ','))
    if not values:
        raise ValueError('表格为空。')
    if len(values) > MAX_ROWS + 1:
        raise ValueError('数据超过十万行，请分批转换。')
    headers = [str(v or '').strip() for v in values[0]]
    if not all(headers) or len(set(headers)) != len(headers):
        raise ValueError('第一行须为不重复、非空的字段名称。')
    rows = []
    for cells in values[1:]:
        ensure_cancel(cancel)
        if len(cells) > len(headers):
            raise ValueError('表格中的数据列数超过表头，请先整理。')
        rows.append({key: (cells[i] if i < len(cells) and cells[i] is not None else '') for i, key in enumerate(headers)})
    return rows


def _rows(data):
    if isinstance(data, dict):
        if data and all(not isinstance(v, (dict, list)) for v in data.values()):
            data = [data]
        elif len(data) == 1 and isinstance(next(iter(data.values())), list):
            data = next(iter(data.values()))
        else:
            raise ValueError('转表格需要对象列表或一条简单对象；请先展开嵌套数据。')
    if not isinstance(data, list) or not data or not all(isinstance(row, dict) for row in data):
        raise ValueError('转表格需要非空对象列表，例如 [{"姓名":"张三","数量":2}]。')
    if len(data) > MAX_ROWS:
        raise ValueError('数据超过十万条，请分批转换。')
    if any(not isinstance(key, str) for row in data for key in row):
        raise ValueError('表格字段名必须是文本；请先将数字等键名转换为不重复的文本字段。')
    return data


def _cell(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return '' if value is None else value


def write_table(data, target, cancel=None):
    rows = _rows(data)
    headers = list(dict.fromkeys(str(key) for row in rows for key in row))
    if len(headers) > 16384:
        raise ValueError('字段数量超过 Excel 列数限制。')
    warnings = []
    if any(isinstance(v, (list, dict)) for row in rows for v in row.values()):
        warnings.append('嵌套对象与数组保存为单元格内的 JSON 文本。')
    if target.suffix.lower() == '.xlsx':
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = '转换数据'
        sheet.append(headers)
        for cell in sheet[1]:
            cell.data_type = 's'
        for row in rows:
            ensure_cancel(cancel)
            values = [_cell(row.get(key)) for key in headers]
            if any(isinstance(v, str) and len(v) > 32767 for v in values):
                raise ValueError('某字段超过 Excel 单元格字符上限，建议改为 JSON。')
            sheet.append(values)
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        book.save(target)
        book.close()
    else:
        with target.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream, delimiter='\t' if target.suffix.lower() == '.tsv' else ',')
            writer.writerow(headers)
            for row in rows:
                ensure_cancel(cancel)
                writer.writerow([_cell(row.get(key)) for key in headers])
    return warnings


def _contact_summary(card):
    def values(key):
        return '；'.join(str(v.value) for v in card.contents.get(key, []))
    return {'姓名': values('fn'), '电话': values('tel'), '邮箱': values('email'),
            '单位': '；'.join(';'.join(v.value) if isinstance(v.value, list) else str(v.value) for v in card.contents.get('org', [])),
            '备注': values('note')}


def read_contacts(source):
    import vobject
    rows = []
    for card in vobject.readComponents(read_text(source)):
        if card.name.upper() != 'VCARD':
            continue
        rows.append({**_contact_summary(card), 'vCard原文': card.serialize()})
    if not rows:
        raise ValueError('没有找到联系人。')
    return rows


def write_contacts(rows, target):
    import vobject
    cards = []
    for row in _rows(rows):
        card = vobject.readOne(row['vCard原文']) if row.get('vCard原文') else vobject.vCard()
        original_values = _contact_summary(card)
        for field, key in [('姓名', 'fn'), ('电话', 'tel'), ('邮箱', 'email'), ('单位', 'org'), ('备注', 'note')]:
            if field not in row:
                continue
            if row.get('vCard原文') and str(row.get(field) or '') == original_values[field]:
                continue
            card.contents.pop(key, None)
            text = str(row.get(field) or '').strip()
            for value in text.split('；') if key in {'tel', 'email'} else [text]:
                if value:
                    card.add(key).value = value.split(';') if key == 'org' else value
        if not card.contents.get('fn'):
            raise ValueError('通讯录表格必须有“姓名”列且不能为空。')
        if not card.contents.get('n'):
            card.add('n').value = vobject.vcard.Name(family=str(card.fn.value))
        cards.append(card.serialize())
    target.write_text(''.join(cards), encoding='utf-8')


def read_calendar(source):
    from icalendar import Calendar
    calendar = Calendar.from_ical(Path(source).read_bytes())
    zones = ''.join(zone.to_ical().decode('utf-8') for zone in calendar.walk('VTIMEZONE'))
    rows = []
    for event in calendar.walk('VEVENT'):
        def text(key):
            value = event.get(key)
            if value is None:
                return ''
            if hasattr(value, 'dt'):
                return value.dt.isoformat()
            if key in {'RRULE', 'EXDATE'}:
                return value.to_ical().decode('utf-8')
            return str(value)
        rows.append({'标题': text('SUMMARY'), '开始时间': text('DTSTART'), '结束时间': text('DTEND'),
                     '地点': text('LOCATION'), '说明': text('DESCRIPTION'), '重复规则': text('RRULE'),
                     'UID': text('UID'), '事件原文': event.to_ical().decode('utf-8'), '时区定义': zones})
    if not rows:
        raise ValueError('没有找到日历事项。')
    return rows


def _date_value(text):
    if isinstance(text, (date, datetime)):
        return text
    text = str(text).strip()
    return date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text.replace('Z', '+00:00'))


def write_calendar(rows, target):
    from icalendar import Calendar, Event, vRecur
    calendar = Calendar()
    calendar.add('prodid', '-//Qingjian//Offline calendar converter//ZH')
    calendar.add('version', '2.0')
    seen_zones = set()
    for row in _rows(rows):
        if row.get('事件原文'):
            parsed = Calendar.from_ical('BEGIN:VCALENDAR\r\nVERSION:2.0\r\n' + str(row.get('时区定义') or '') +
                                        str(row['事件原文']) + 'END:VCALENDAR\r\n')
            event = next(iter(parsed.walk('VEVENT')), None)
            if event is None:
                raise ValueError('事件原文无效。')
            for zone in parsed.walk('VTIMEZONE'):
                identifier = str(zone.get('TZID', ''))
                if identifier not in seen_zones:
                    seen_zones.add(identifier)
                    calendar.add_component(zone)
        else:
            event = Event()
        for field, key in [('标题', 'SUMMARY'), ('开始时间', 'DTSTART'), ('结束时间', 'DTEND'),
                           ('地点', 'LOCATION'), ('说明', 'DESCRIPTION'), ('重复规则', 'RRULE'), ('UID', 'UID')]:
            if field not in row:
                continue
            value = row[field]
            previous = event.get(key)
            # Keeping unchanged time properties preserves TZID and DST rules.
            if key in {'DTSTART', 'DTEND'} and previous is not None and hasattr(previous, 'dt') and str(value) == previous.dt.isoformat():
                continue
            event.pop(key, None)
            if value is None or value == '':
                continue
            if key in {'DTSTART', 'DTEND'}:
                value = _date_value(value)
                if previous is not None and hasattr(previous, 'dt') and isinstance(value, datetime):
                    tz = getattr(previous.dt, 'tzinfo', None)
                    if tz is not None:
                        value = value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
            elif key == 'RRULE':
                value = vRecur.from_ical(str(value))
            event.add(key, value)
        if not event.get('SUMMARY') or not event.get('DTSTART'):
            raise ValueError('日历表格需要非空“标题”和“开始时间”列，时间使用 ISO 格式。')
        if not event.get('UID'):
            event.add('uid', str(uuid.uuid4()) + '@qingjian.local')
        calendar.add_component(event)
    target.write_bytes(calendar.to_ical())


def convert(source, target, options=None):
    options = options or {}
    cancel = options.get('_cancel')
    source, target = Path(source), Path(target)
    ensure_cancel(cancel)
    ext, out = source.suffix.lower(), target.suffix.lower()
    if ext == '.vcf':
        data = read_contacts(source)
    elif ext == '.ics':
        data = read_calendar(source)
    else:
        data = read_data(source, cancel)
    warnings = []
    if out in TABLES:
        warnings += write_table(data, target, cancel)
    elif out == '.vcf':
        write_contacts(data, target)
    elif out == '.ics':
        write_calendar(data, target)
    elif out == '.json':
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    elif out in {'.jsonl', '.ndjson'}:
        values = data if isinstance(data, list) else [data]
        target.write_text(''.join(json.dumps(row, ensure_ascii=False, default=str) + '\n' for row in values), encoding='utf-8')
    elif out in {'.yaml', '.yml'}:
        import yaml
        target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')
    elif out == '.toml':
        import tomli_w
        if not isinstance(data, dict):
            data = {'records': data}
            warnings.append('TOML 顶层使用 records 包含原始记录。')
        try:
            target.write_text(tomli_w.dumps(data), encoding='utf-8')
        except (TypeError, ValueError) as exc:
            raise ValueError('数据含 TOML 不支持的类型（例如空值），请先整理或选择 JSON。') from exc
    elif out == '.xml':
        if isinstance(data, dict) and len(data) == 1:
            key, value = next(iter(data.items()))
        else:
            key, value = 'root', data
        root = _xml_node(key, value)
        ET.indent(root)
        ET.ElementTree(root).write(target, encoding='utf-8', xml_declaration=True)
        warnings.append('XML 使用文本表达标量，重新导入时数字与布尔值可能成为文本。')
    else:
        raise ValueError('不支持的目标数据格式：' + out)
    ensure_cancel(cancel)
    return {'changes': 1, 'outputs': [str(target)], 'warnings': warnings}
