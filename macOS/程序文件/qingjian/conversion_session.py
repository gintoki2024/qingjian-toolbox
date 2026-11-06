"""Conversion state shared by the floating window, without homepage widgets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


class ConversionSession:
    def __init__(self, settings_path=None, *, default_folder=None):
        self.settings_path = Path(settings_path) if settings_path else None
        self.url_folder = Path(default_folder or Path.home() / 'Downloads' / '青简快捷转换')
        self.status_text = ''
        self._running = False
        self._last_outputs = []
        self._has_result = False
        try:
            if self.settings_path and self.settings_path.is_file() and self.settings_path.stat().st_size < 65536:
                value = json.loads(self.settings_path.read_text(encoding='utf-8')).get('url_folder')
                if isinstance(value, str) and value and '\x00' not in value and Path(value).is_absolute():
                    self.url_folder = Path(value)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            pass

    def set_folder(self, folder):
        folder = Path(folder)
        if not folder.is_absolute():
            raise ValueError('请选择完整的本地保存路径。')
        staged = None
        try:
            if self.settings_path:
                self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                handle, name = tempfile.mkstemp(prefix='.qingjian-quick-', suffix='.tmp', dir=self.settings_path.parent)
                staged = Path(name)
                with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                    json.dump({'url_folder': str(folder)}, stream, ensure_ascii=False)
                os.replace(staged, self.settings_path)
            self.url_folder = folder
        finally:
            if staged:
                staged.unlink(missing_ok=True)

    def notice(self, message):
        self.status_text = str(message)

    def begin(self, action_id, count, title=None):
        self._last_outputs = []
        self._has_result = False
        self._running = True
        self.notice((title or '格式转换') + f' · 正在处理 {count} 项…')

    def update_progress(self, done, total, message):
        self.notice(message or f'正在处理 {done} / {total}')

    def finish(self, result):
        self._last_outputs = list(result.get('outputs', []))
        self._has_result = bool(result.get('records') or result.get('errors'))
        failures = sum(row.get('status') == 'failed' for row in result.get('records', []))
        if not result.get('records') and result.get('errors'):
            failures = len(result['errors'])
        prefix = '已停止' if result.get('cancelled') else '转换完成'
        message = f'{prefix} · 成功 {len(self._last_outputs)} 项，失败 {failures} 项'
        if result.get('warnings'):
            message += ' · 有提示，请查看结果'
        if result.get('errors') and not self._last_outputs:
            message += ' · ' + str(result['errors'][0])
        self._running = False
        self.notice(message)
