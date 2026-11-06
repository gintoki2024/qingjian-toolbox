"""Copy-only archive conversion, with traversal/link/expanded-size checks."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile

from .common import ensure_cancel
from .format_media import engine, run_engine

ARCHIVES = ('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.txz', '.zip', '.7z', '.rar', '.tar')
MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 20000


def extension(path):
    name = str(path).lower()
    return next((ext for ext in ARCHIVES if name.endswith(ext)), '')


def _safe_name(name):
    name = str(name).replace('\\', '/')
    parts = PurePosixPath(name).parts
    if not parts or name.startswith('/') or any(part in {'.', '..'} or ':' in part or '\x00' in part
            or part.rstrip(' .') != part or part.split('.')[0].upper() in
            {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))}
            for part in parts):
        raise ValueError('压缩包内包含不安全的路径：' + name[:120])
    return Path(*parts)


def _check_entries(entries):
    if len(entries) > MAX_FILES:
        raise ValueError('压缩包文件超过两万个，请分批处理。')
    total = 0
    seen = set()
    for name, size, linked in entries:
        _safe_name(name)
        normalized = name.replace('\\', '/').rstrip('/').casefold()
        if normalized in seen:
            raise ValueError('压缩包内有重复或仅大小写不同的文件名。')
        seen.add(normalized)
        total += size
        if total > MAX_BYTES or size < 0:
            raise ValueError('压缩包解压后超过 2 GB，请拆分后处理。')
        if linked:
            raise ValueError('压缩包含链接或特殊文件，未执行解压。')


def extract(source, folder, cancel=None):
    ext = extension(source)
    folder.mkdir(parents=True, exist_ok=True)
    if ext == '.zip':
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            _check_entries([(m.filename, m.file_size, stat.S_ISLNK(m.external_attr >> 16)) for m in members])
            for member in members:
                ensure_cancel(cancel)
                target = folder / _safe_name(member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as src, target.open('xb') as dst:
                        _copy(src, dst, cancel)
    elif ext in {'.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz'}:
        with tarfile.open(source, 'r:*') as archive:
            members = archive.getmembers()
            _check_entries([(m.name, m.size, not (m.isfile() or m.isdir())) for m in members])
            for member in members:
                ensure_cancel(cancel)
                target = folder / _safe_name(member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, target.open('xb') as dst:
                        _copy(src, dst, cancel)
    elif ext in {'.7z', '.rar'}:
        executable = engine('7zip/7z.exe')
        listing = run_engine([executable, 'l', '-slt', '-ba', '-sccUTF-8', '-p', source], cancel, cwd=folder)
        blocks = []
        current = {}
        for line in listing.splitlines() + ['']:
            if not line.strip():
                if current:
                    blocks.append(current)
                    current = {}
            elif ' = ' in line:
                key, value = line.split(' = ', 1)
                current[key] = value
        entries = []
        for block in blocks:
            if 'Path' not in block:
                continue
            if block.get('Encrypted') == '+':
                raise ValueError('快捷转换不读取加密压缩包，请先解密后重试。')
            attrs = block.get('Attributes', '')
            linked = any('link' in key.lower() for key in block) or 'L' in attrs or bool(re.search(r'\bl[rwx-]{9}', attrs))
            entries.append((block['Path'], int(block.get('Size', '0')), linked))
        if not entries:
            raise ValueError('压缩包为空或无法读取文件目录。')
        _check_entries(entries)
        run_engine([executable, 'x', '-y', '-p', '-snl-', '-snh-', '-o' + str(folder), source], cancel, cwd=folder)
    else:
        raise ValueError('不支持的压缩包格式。')
    actual_size = 0
    for path in folder.rglob('*'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('解压结果含链接，未发布结果。')
        if path.is_file():
            actual_size += path.stat().st_size
    if actual_size > MAX_BYTES:
        raise ValueError('解压结果超过 2 GB，未发布结果。')


def _copy(src, dst, cancel):
    size = 0
    while True:
        ensure_cancel(cancel)
        chunk = src.read(1024 * 1024)
        if not chunk:
            return
        size += len(chunk)
        if size > MAX_BYTES:
            raise ValueError('解压内容过大。')
        dst.write(chunk)


def pack(folder, target, cancel=None):
    ext = extension(target)
    files = sorted(folder.rglob('*'))
    for path in files:
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('不打包符号链接。')
    if ext == '.zip':
        with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                ensure_cancel(cancel)
                archive.write(path, path.relative_to(folder).as_posix())
    elif ext in {'.tar', '.tar.gz', '.tar.bz2', '.tar.xz'}:
        mode = {'.tar': 'w', '.tar.gz': 'w:gz', '.tar.bz2': 'w:bz2', '.tar.xz': 'w:xz'}[ext]
        with tarfile.open(target, mode) as archive:
            for path in files:
                ensure_cancel(cancel)
                archive.add(path, arcname=path.relative_to(folder).as_posix(), recursive=False)
    elif ext == '.7z':
        run_engine([engine('7zip/7z.exe'), 'a', '-t7z', '-mx=5', str(target), '.'], cancel, cwd=folder)
    else:
        raise ValueError('目标压缩格式不支持。')


def convert(source, target, options=None):
    options = options or {}
    cancel = options.get('_cancel')
    source, target = Path(source), Path(target)
    with tempfile.TemporaryDirectory(prefix='archive-', dir=target.parent) as temporary:
        folder = Path(temporary) / 'content'
        if options.get('pack_sources'):
            folder.mkdir()
            names = set()
            for value in options['pack_sources']:
                ensure_cancel(cancel)
                path = Path(value)
                if path.name.casefold() in names:
                    raise ValueError('合并打包的文件有重名，请先改名或分批打包。')
                names.add(path.name.casefold())
                shutil.copyfile(path, folder / path.name)
        else:
            extract(source, folder, cancel)
        if options.get('extract'):
            folder.rename(target)
        else:
            pack(folder, target, cancel)
    return {'changes': 1, 'outputs': [str(target)], 'warnings': ['按文件内容重新打包；原压缩包保持不变。'] if not options.get('extract') else []}
