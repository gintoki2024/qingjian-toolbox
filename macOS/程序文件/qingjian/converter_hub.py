"""One catalogue and copy-only runner for the detachable conversion module."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
import tempfile

from .core.common import CancelledError, commit_file, ensure_cancel, notify, safe_name
from .core.format_archive import ARCHIVES, extension as archive_extension
from .core.format_data import DATA, TABLES
from .core.format_media import IMAGES, IMAGE_OUTPUTS
from .core.format_office import targets_for as office_targets

_LABELS = {
    'pdf': ('PDF', 'PDF 文档'), 'docx': ('Word', 'Word 文档'), 'doc': ('DOC', '旧版 Word'),
    'rtf': ('RTF', '富文本'), 'odt': ('ODT', '开放文档'), 'xlsx': ('Excel', 'Excel 表格'),
    'xls': ('XLS', '旧版 Excel'), 'ods': ('ODS', '开放表格'), 'pptx': ('PPT', 'PPT 演示文稿'),
    'ppt': ('PPT旧', '旧版 PPT'), 'odp': ('ODP', '开放演示文稿'), 'txt': ('TXT', '纯文本 UTF-8'),
    'md': ('MD', 'Markdown'), 'html': ('HTML', '本地网页'), 'epub': ('EPUB', '轻量电子书'),
    'utf8': ('UTF-8', '转 UTF-8 编码'),
    'png': ('PNG', 'PNG 图片'), 'jpg': ('JPG', 'JPG 图片'), 'tiff': ('TIFF', 'TIFF 图片'),
    'webp': ('WebP', 'WebP 图片'), 'bmp': ('BMP', 'BMP 图片'), 'gif': ('GIF', 'GIF 图片'),
    'ico': ('ICO', '图标'), 'avif': ('AVIF', 'AVIF 图片'), 'jp2': ('JP2', 'JPEG 2000 图片'),
    'svg': ('SVG', 'SVG 矢量图'), 'svgz': ('SVGZ', '压缩 SVG'), 'csv': ('CSV', 'CSV 表格'),
    'tsv': ('TSV', 'TSV 表格'), 'json': ('JSON', 'JSON 数据'), 'jsonl': ('JSONL', '逐行 JSON'),
    'yaml': ('YAML', 'YAML 数据'), 'toml': ('TOML', 'TOML 数据'), 'xml': ('XML', 'XML 数据'),
    'vcf': ('通讯录', 'VCF 通讯录'), 'ics': ('日历', 'ICS 日历'), 'eml': ('EML', 'EML 邮件'),
    'ofd': ('OFD', 'OFD 文档'), 'zip': ('ZIP', 'ZIP 压缩包'), '7z': ('7Z', '7Z 压缩包'),
    'tar': ('TAR', 'TAR 归档'), 'tar.gz': ('TAR.GZ', 'GZIP 归档'),
    'tar.bz2': ('TAR.BZ2', 'BZIP2 归档'), 'tar.xz': ('TAR.XZ', 'XZ 归档'),
    'extract': ('解压', '解压到新文件夹'), 'attachments': ('附件', '提取邮件附件'),
    'pack_zip': ('合并ZIP', '合并打包 ZIP'), 'images_pdf': ('合并PDF', '图片合并 PDF'),
    'images_docx': ('图片Word', '图片合并 Word'), 'images_pptx': ('图片PPT', '图片合并 PPT'),
}
_ORDER = list(_LABELS)
_CANONICAL = {'.jpeg': '.jpg', '.tif': '.tiff', '.markdown': '.md', '.yml': '.yaml', '.ndjson': '.jsonl'}


def target_title(action_id):
    from .quick_actions import ACTIONS
    if action_id in ACTIONS:
        return ACTIONS[action_id]['title']
    key = str(action_id).removeprefix('convert:')
    return _LABELS.get(key, (key, key))[1]


def _kind(value):
    from .quick_actions import _is_http
    if _is_http(str(value)):
        return 'url'
    return archive_extension(value) or Path(str(value)).suffix.lower()


def _targets(kind):
    targets = {_CANONICAL.get(ext, ext).lstrip('.') for ext in office_targets(kind)}
    if kind in IMAGES:
        targets.update(ext.lstrip('.') for ext in IMAGE_OUTPUTS)
        targets.add('ofd')
    if kind == '.pdf':
        targets.add('ofd')
    if kind == '.ofd':
        targets.update({'pdf', 'png', 'jpg', 'txt'})
    if kind == '.txt':
        targets.add('ofd')
    if kind in {'.txt', '.md', '.markdown'}:
        targets.add('utf8')
    if kind in {'.svg', '.svgz'}:
        targets.update({'svg', 'svgz', 'png', 'jpg', 'pdf'})
    if kind in {'.ps', '.eps'}:
        targets.update({'pdf', 'png', 'jpg'})
    if kind in ARCHIVES:
        targets.update({'extract', 'zip', '7z', 'tar', 'tar.gz', 'tar.bz2', 'tar.xz'})
    if kind in {'.eml', '.msg'}:
        targets.update({'docx', 'pdf', 'html', 'txt', 'attachments'})
        if kind == '.msg':
            targets.add('eml')
    if kind == '.mbox':
        targets.add('eml')
    if kind in DATA | TABLES:
        targets.update({'json', 'jsonl', 'yaml', 'toml', 'xml', 'csv', 'tsv', 'xlsx'})
    if kind in {'.csv', '.tsv', '.xlsx'}:
        targets.update({'vcf', 'ics'})
    if kind in {'.vcf', '.ics'}:
        targets.update({'csv', 'xlsx', 'json'})
    if kind in {'.xls', '.ods'}:
        targets.add('json')
    targets.discard(_CANONICAL.get(kind, kind).lstrip('.'))
    return targets


def available_targets(sources):
    """Pure extension matching: safe to call on each drag-hover event."""
    if not isinstance(sources, (list, tuple)) or not sources or len(sources) > 500:
        return []
    kinds = [_kind(value) for value in sources]
    common = set.intersection(*[_targets(kind) for kind in kinds])
    if all(kind != 'url' for kind in kinds):
        common.add('pack_zip')
    if len(sources) > 1 and all(kind in IMAGES for kind in kinds):
        common.update({'images_pdf', 'images_docx', 'images_pptx'})
    result = []
    for key in _ORDER:
        if key not in common:
            continue
        glyph, title = _LABELS[key]
        description = '拖入即转换，结果保存到原文件旁；保留原件。'
        if key == 'pptx' and '.pdf' in kinds:
            description = 'PDF 每页保存为一张幻灯片图片，文字不能直接编辑。'
        elif key in {'csv', 'xlsx', 'docx', 'txt'} and '.pdf' in kinds:
            description = '提取已有文字与表格；扫描件不进行 OCR。'
        elif key.startswith('images_') or key == 'pack_zip':
            description = '合并选中的文件，保存到第一个文件所在目录。'
        elif key == 'epub':
            description = '轻量 EPUB，无需 GPU；保留内容与基本章节排版。'
        elif key == 'utf8':
            description = '保留文字与空白，生成 UTF-8 编码副本，原文件不变。'
        elif key in {'vcf', 'ics'}:
            description = '按标准列名导入；日历需要“标题、开始时间”，通讯录需要“姓名”。'
        result.append({'id': 'convert:' + key, 'title': title, 'glyph': glyph, 'description': description})
    return result


def validate_sources(action_id, sources):
    from .quick_actions import _is_http, _local_source
    key = str(action_id).removeprefix('convert:')
    if not str(action_id).startswith('convert:') or key not in _LABELS:
        return ['未知的转换目标。']
    if not isinstance(sources, (list, tuple)) or not sources:
        return ['请拖入文件或粘贴网页链接。']
    if len(sources) > 500:
        return ['一次最多处理 500 项，请分批操作。']
    errors = []
    for value in sources:
        try:
            kind = _kind(value)
            if kind == 'url':
                if key not in _targets('url'):
                    raise ValueError('网页链接不能转换为此目标格式。')
            else:
                path = Path(str(value))
                _local_source(value, {'extensions': (path.suffix.lower(),)})
                if key not in _targets(kind) and key != 'pack_zip' and not (key.startswith('images_') and kind in IMAGES):
                    raise ValueError('该文件不支持所选转换方向。')
        except (OSError, ValueError) as exc:
            errors.append(str(value) + '：' + str(exc))
    return errors


def _convert(source, target, key, options):
    from .core import format_office, format_data, format_media, format_mail, format_archive
    kind = _kind(source)
    out = target.suffix.lower()
    if key in {'pack_zip', 'extract', 'zip', '7z', 'tar', 'tar.gz', 'tar.bz2', 'tar.xz'}:
        return format_archive.convert(source, target, options)
    if kind in {'.eml', '.msg', '.mbox'}:
        return format_mail.convert(source, target, options)
    if kind == '.ofd' or out == '.ofd':
        from .core.format_ofd import convert
        return convert(source, target, options)
    if kind in IMAGES and out in IMAGE_OUTPUTS:
        return format_media.image_convert(source, target, options)
    if kind in {'.svg', '.svgz', '.ps', '.eps'}:
        return format_media.vector_convert(source, target, options)
    if kind in DATA | {'.vcf', '.ics'} or (kind in TABLES and out in DATA | TABLES | {'.vcf', '.ics'}):
        return format_data.convert(source, target, options)
    if kind in {'.xls', '.ods'} and out == '.json':
        intermediate = target.with_suffix('.intermediate.xlsx')
        format_office.convert(source, intermediate, options)
        return format_data.convert(intermediate, target, options)
    return format_office.convert(source, target, options)


def _publish(staged, folder, stem, suffix, cancel):
    cleaned = safe_name(stem, fallback='转换结果', limit=130)
    for index in range(1, 100000):
        ensure_cancel(cancel)
        target = folder / (cleaned + (f'_{index}' if index > 1 else '') + suffix)
        try:
            if staged.is_dir():
                # Windows rename refuses an existing destination, including
                # links. Files are handled by the shared atomic primitive.
                if os.path.lexists(target):
                    continue
                staged.rename(target)
            else:
                commit_file(staged, target)
            return target
        except FileExistsError:
            continue
    raise ValueError('同名结果过多，请整理输出目录。')


@contextmanager
def _temporary_area(folder, cleanup_warnings):
    handle = tempfile.TemporaryDirectory(prefix='.qj-convert-', dir=folder)
    try:
        yield Path(handle.name)
    finally:
        try:
            handle.cleanup()
        except OSError:
            # Windows indexers/antivirus may briefly hold an intermediate file.
            # A completed, published result remains a success in this case.
            cleanup_warnings.append('结果已处理；部分临时文件被占用，未能自动清理：' + handle.name)


def run_conversion(action_id, sources, url_folder=None, progress=None, cancel=None):
    from .quick_actions import _is_http, _local_source, _ACTION_LOCK
    result = {'outputs': [], 'records': [], 'errors': [], 'warnings': [], 'changes': 0,
              'cancelled': False, 'output_folders': [], 'action_id': action_id,
              'total': len(sources) if isinstance(sources, (list, tuple)) else 0}
    if not isinstance(sources, (list, tuple)) or not sources or len(sources) > 500:
        result['errors'] = ['请提供 1—500 个文件或网页链接。']
        return result
    key = str(action_id).removeprefix('convert:')
    if key not in _LABELS:
        result['errors'] = ['未知的转换目标。']
        return result
    merged = key.startswith('images_') or key == 'pack_zip'
    groups = [list(sources)] if merged else [[value] for value in sources]
    acquired = False
    try:
        while not acquired:
            ensure_cancel(cancel)
            acquired = _ACTION_LOCK.acquire(timeout=0.1)
        for index, group in enumerate(groups):
            ensure_cancel(cancel)
            value = group[0]
            label = str(value) if _is_http(str(value)) else Path(str(value)).name
            record = {'source': '；'.join(map(str, group)), 'status': 'failed', 'output': '', 'output_folder': '',
                      'warnings': [], 'error': '', 'changes': 0}
            result['records'].append(record)
            notify(progress, index, len(groups), '正在转换：' + label)
            try:
                errors = validate_sources(action_id, group)
                if errors:
                    raise ValueError('；'.join(errors))
                if _is_http(str(value)):
                    if not url_folder:
                        raise ValueError('请先选择网页链接的结果保存位置。')
                    source, folder, stem = str(value), Path(url_folder).expanduser().absolute(), '网页文章'
                    for part in [folder, *folder.parents]:
                        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
                            raise ValueError('保存位置不能是符号链接或目录联接。')
                    folder.mkdir(parents=True, exist_ok=True)
                else:
                    source = _local_source(value, {'extensions': (Path(str(value)).suffix.lower(),)})
                    folder, stem = source.parent, source.stem
                suffix = '.' + ({'pack_zip': 'zip'}.get(key, key.split('_')[-1] if key.startswith('images_') else key))
                if key == 'utf8':
                    suffix = Path(source).suffix.lower()
                if key in {'extract', 'attachments'}:
                    suffix = ''
                cleanup_warnings = []
                with _temporary_area(folder, cleanup_warnings) as staging:
                    staged = staging / ('result' + suffix)
                    options = {'_cancel': cancel, 'extract': key == 'extract', 'attachments': key == 'attachments'}
                    if key == 'utf8':
                        options['encoding'] = 'utf-8'
                    if merged:
                        local_sources = [_local_source(item, {'extensions': (Path(str(item)).suffix.lower(),)}) for item in group]
                        options['_sources'] = local_sources
                        if key == 'pack_zip':
                            options['pack_sources'] = local_sources
                    converted = _convert(source, staged, key, options) or {}
                    outputs = [Path(p) for p in converted.get('outputs', [staged])]
                    if not outputs:
                        raise ValueError('转换没有生成结果。')
                    for path in outputs:
                        if not path.resolve().is_relative_to(staging.resolve()) or path.is_symlink():
                            raise ValueError('转换器返回了非临时结果路径。')
                        if not path.exists() or (path.is_file() and not path.stat().st_size):
                            raise ValueError('转换器没有生成完整结果。')
                    ensure_cancel(cancel)
                    name = stem + ('_合并' if merged else '')
                    if key == 'utf8':
                        name += '_UTF8'
                    if key in {'extract', 'attachments'}:
                        name += '_解压' if key == 'extract' else '_附件'
                    if len(outputs) == 1:
                        published = _publish(outputs[0], folder, name, suffix, cancel)
                        published_outputs = [str(published)]
                    else:
                        bundle = staging / 'complete'
                        bundle.mkdir()
                        for page, output in enumerate(outputs, 1):
                            destination = bundle / (safe_name(stem, limit=100) + f'_{page:03d}' + output.suffix)
                            output.rename(destination)
                        published = _publish(bundle, folder, name + '_转' + _LABELS[key][0], '', cancel)
                        published_outputs = [str(p) for p in sorted(published.iterdir())]
                warnings = list(converted.get('warnings', [])) + cleanup_warnings
                if merged:
                    warnings.append('合并结果保存在第一个文件所在目录。')
                record.update(status='success', output=str(published), output_folder=str(folder),
                              warnings=warnings, changes=len(published_outputs))
                result['outputs'].extend(published_outputs)
                result['changes'] += len(published_outputs)
                if str(folder) not in result['output_folders']:
                    result['output_folders'].append(str(folder))
                result['warnings'].extend(label + '：' + text for text in warnings)
            except CancelledError:
                record.update(status='cancelled', error='已停止，当前项未发布不完整结果。')
                raise
            except PermissionError:
                record['error'] = '无法读取文件或写入原目录，请检查权限、只读状态及文件占用。'
                result['errors'].append(label + '：' + record['error'])
            except Exception as exc:
                record['error'] = str(exc)
                result['errors'].append(label + '：' + str(exc))
            notify(progress, index + 1, len(groups), ('已完成：' if record['status'] == 'success' else '转换失败：') + label)
    except CancelledError:
        result['cancelled'] = True
        result['warnings'].append('已停止，已完成结果保留，未改动原件。')
    finally:
        if acquired:
            _ACTION_LOCK.release()
    result['warnings'] = list(dict.fromkeys(result['warnings']))
    return result
