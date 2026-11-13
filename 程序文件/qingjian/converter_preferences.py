"""Small per-family preferences; no source paths or document contents are saved."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def source_family(sources):
    families = set()
    for source in sources:
        value = str(source)
        ext = Path(value).suffix.lower()
        if value.lower().startswith(('http://', 'https://')):
            family = 'web'
        elif ext in {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.bmp', '.tif', '.tiff', '.gif', '.avif', '.ico', '.jp2', '.j2k'}:
            family = 'image'
        elif ext in {'.xls', '.xlsx', '.ods', '.csv', '.tsv'}:
            family = 'table'
        elif ext in {'.doc', '.docx', '.rtf', '.odt'}:
            family = 'word'
        elif ext in {'.ppt', '.pptx', '.odp'}:
            family = 'slide'
        elif ext in {'.html', '.htm', '.mht', '.mhtml', '.xhtml'}:
            family = 'web'
        elif ext in {'.txt', '.md', '.markdown'}:
            family = 'text'
        else:
            family = ext.lstrip('.') or 'other'
        families.add(family)
    return next(iter(families)) if len(families) == 1 else 'mixed'


OPTION_KEYS = frozenset({'quality', 'max_edge', 'jpeg_background', 'dpi', 'sheet_mode',
                         'encoding', 'delimiter', 'input_encoding', 'input_delimiter'})


class ConversionPreferences:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.values = {'options': {}, 'orders': {}}
        try:
            if self.path and self.path.is_file() and self.path.stat().st_size < 65536:
                value = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(value, dict):
                    for key in self.values:
                        if isinstance(value.get(key), dict):
                            self.values[key] = value[key]
        except (OSError, ValueError, TypeError, RecursionError):
            pass

    def options(self, sources):
        value = self.values['options'].get(source_family(sources), {})
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if k in OPTION_KEYS and isinstance(v, (str, int, float))}

    def order(self, sources):
        value = self.values['orders'].get(source_family(sources), [])
        return [v for v in value if isinstance(v, str) and v.startswith('convert:')][:60] if isinstance(value, list) else []

    def remember_options(self, sources, options):
        # Sheet names belong to the current document and never become defaults.
        remembered = self.options(sources)
        remembered.update({
            k: v for k, v in options.items() if k in OPTION_KEYS and isinstance(v, (str, int, float))
        })
        self.values['options'][source_family(sources)] = remembered
        if self.values['options'][source_family(sources)].get('sheet_mode') == 'selected':
            self.values['options'][source_family(sources)]['sheet_mode'] = 'all'
        self.save()

    def remember_order(self, sources, order):
        self.values['orders'][source_family(sources)] = list(dict.fromkeys(order))[:60]
        self.save()

    def save(self):
        if not self.path:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.conversion-defaults-', suffix='.tmp', dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.values, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
