"""Pinned, private native converters for the lightweight source distribution.

Only the standard library is imported. Installers are read as archives; no
installer, compiler, package manager, or machine-wide registration is run.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
import uuid
import zipfile

from startup_engine import (EngineSetupError, _MsiDatabase, _FileEntry, _extract_cabinet,
                            _download_msi, _is_link, _safe_name, _verified)

ENVIRONMENT_KEY = 'QINGJIAN_NATIVE_COMPONENTS'
COMPONENTS = ('7zip', 'Ghostscript', 'OFD')
_OWNER = '.qingjian-native-owner.json'


class NativeSetupError(RuntimeError):
    pass


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise NativeSetupError('转换组件清单含有不安全的相对路径。')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(_safe_name(p) != p for p in path.parts):
        raise NativeSetupError('转换组件清单含有不安全的相对路径。')
    return Path(*path.parts)


def _file_spec(row):
    _relative(row.get('file'))
    if (type(row.get('bytes')) is not int or not 0 <= row['bytes'] <= 512 * 1024 ** 2
            or not re.fullmatch('[a-f0-9]{64}', row.get('sha256', ''))):
        raise NativeSetupError('转换组件清单缺少固定大小或 SHA256。')
    if 'archive_key' in row:
        _relative(row['archive_key'])


def load_manifest(project):
    from bootstrap import validate_source
    try:
        path = Path(project) / 'native-sources.json'
        if path.stat().st_size > 2 * 1024 ** 2:
            raise ValueError('清单过大')
        result = json.loads(path.read_text(encoding='utf-8-sig'))
        if result.get('schema_version') != 1 or result.get('platform') != 'windows-x64':
            raise ValueError('清单版本无效')
        if set(result.get('components', {})) != set(COMPONENTS):
            raise ValueError('必需的原生组件不完整')
        filenames = set()
        for spec in result['artifacts'].values():
            _file_spec(dict(spec, file=spec.get('filename')))
            _safe_name(spec['filename'])
            if spec['filename'].casefold() in filenames or len(spec.get('urls', [])) < 2:
                raise ValueError('下载文件重复或缺少备用源')
            filenames.add(spec['filename'].casefold())
            for source in spec['urls']:
                validate_source(source)
        for key, component in result['components'].items():
            if component.get('kind') != {'7zip': 'msi', 'Ghostscript': 'nsis', 'OFD': 'ofd'}[key]:
                raise ValueError('组件解包方式无效')
            if component.get('artifact') not in result['artifacts']:
                raise ValueError('缺少组件下载资料')
            groups = [component['files']]
            if key == 'OFD':
                variants = component['runtime_variants']
                if set(variants) != {'jlink', 'jre'}:
                    raise ValueError('Java 校验信息不完整')
                groups.extend(variants.values())
            for rows in groups:
                if not rows or len(rows) > 3000:
                    raise ValueError('组件文件清单无效')
                seen = set()
                for row in rows:
                    _file_spec(row)
                    if row['file'].casefold() in seen:
                        raise ValueError('组件文件路径重复')
                    seen.add(row['file'].casefold())
                    if row.get('artifact') and row['artifact'] not in result['artifacts']:
                        raise ValueError('缺少依赖下载资料')
        return result
    except (OSError, ValueError, KeyError, TypeError, EngineSetupError) as exc:
        raise NativeSetupError('原生转换组件清单无法读取，请完整解压轻量源码包：' + str(exc)) from exc


def _signature(component):
    return hashlib.sha256(json.dumps(component, sort_keys=True).encode()).hexdigest()[:16]


def _target(home, key, component):
    return Path(home) / 'native' / (key + '-' + _signature(component))


def _safe_tree_file(root, relative):
    root = Path(root)
    current = root
    if _is_link(root):
        return None
    for part in _relative(relative).parts:
        current = current / part
        if _is_link(current):
            return None
    return current


def _files_complete(root, rows):
    try:
        for row in rows:
            path = _safe_tree_file(root, row['file'])
            if path is None or not _verified(path, row):
                return False
        return True
    except (OSError, ValueError, NativeSetupError):
        return False


def _complete(root, component):
    if not _files_complete(root, component['files']):
        return False
    variants = component.get('runtime_variants')
    return not variants or any(_files_complete(root, rows) for rows in variants.values())


def _candidates(project, home, key, component):
    project = Path(project).resolve()
    result = [_target(home, key, component), project / 'support' / key]
    try:
        result.extend(sorted((path for path in (Path(home) / 'native').glob(key + '-*')
                              if re.fullmatch(re.escape(key) + r'-[a-f0-9]{16}', path.name)), reverse=True)[:16])
    except OSError:
        pass
    try:
        configured = json.loads(os.environ.get(ENVIRONMENT_KEY, '{}'))
        value = configured.get(key) if isinstance(configured, dict) else None
        if isinstance(value, str) and Path(value).is_absolute():
            result.append(Path(value))
    except ValueError:
        pass
    from startup_shared import component_locations
    for location in component_locations(project):
        configured = location.get('native_components', {}).get(key)
        if configured:
            result.append(Path(configured))
        old_home = Path(location['runtime_home'])
        result.append(_target(old_home, key, component))
        try:
            result.extend(sorted((path for path in (old_home / 'native').glob(key + '-*')
                                  if re.fullmatch(re.escape(key) + r'-[a-f0-9]{16}', path.name)), reverse=True)[:16])
        except OSError:
            pass
    parents = [project.parent]
    if project.name == '程序文件':
        parents.append(project.parent.parent)
    releases = []
    for parent in parents:
        try:
            for neighbor in parent.iterdir():
                match = re.fullmatch(r'青简工具箱_v(\d+)\.(\d+)\.(\d+)', neighbor.name)
                if match and neighbor.is_dir() and not _is_link(neighbor):
                    releases.append((tuple(map(int, match.groups())), neighbor))
        except OSError:
            pass
    for _, neighbor in sorted(releases, reverse=True):
        result.extend((neighbor / '程序文件/support' / key, neighbor / 'support' / key))
    # Installed components are reused only if every required byte matches the
    # reviewed version. No PATH search or execution of an unverified candidate.
    for env_key in ('ProgramW6432', 'ProgramFiles'):
        value = os.environ.get(env_key)
        if value and key == '7zip':
            result.append(Path(value) / '7-Zip')
        if value and key == 'Ghostscript':
            result.append(Path(value) / 'gs' / ('gs' + component['version']))
    return list(dict.fromkeys(result))


def check_native(project, home, manifest=None):
    manifest = manifest or load_manifest(project)
    roots = {}
    for key, component in manifest['components'].items():
        existing = next((p for p in _candidates(project, home, key, component) if _complete(p, component)), None)
        if existing:
            roots[key] = str(existing.resolve())
    missing = [key for key in COMPONENTS if key not in roots]
    return {'native_ready': not missing, 'native_missing': missing, 'native_components': roots}


def _download(manifest, key, home, emit, offline):
    spec = manifest['artifacts'][key]
    def forward(event):
        value = dict(event)
        value['stage'] = 'native-download'
        value['component'] = key
        value['message'] = value.get('message', '').replace('排版引擎', spec['filename'])
        emit(value)
    try:
        from startup_shared import component_locations
        # Native artifacts share the same verified cache reader as LibreOffice.
        # Project is the manifest's owner; no drive or arbitrary archive scan.
        project = manifest.get('_project')
        locations = component_locations(project) if project else []
        return _download_msi(Path(home), spec, forward, offline=offline,
                             cache_homes=[row['runtime_home'] for row in locations])
    except EngineSetupError as exc:
        raise NativeSetupError('补齐转换组件 ' + key + ' 未完成。缓存已保留，请检查网络后重新启动。' +
                               str(exc).replace('排版引擎', spec['filename'])) from exc


def _copy_checked(source, destination, row):
    if not _verified(source, row):
        raise NativeSetupError('转换组件未通过校验：' + row['file'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as src, destination.open('xb') as dst:
        shutil.copyfileobj(src, dst)
    if not _verified(destination, row):
        raise NativeSetupError('转换组件复制校验失败：' + row['file'])


def _zip_files(archive_path, destination, rows):
    """Write only pinned regular members, never archive-controlled paths."""
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > 5000 or len({m.filename.casefold() for m in members}) != len(members):
            raise NativeSetupError('JRE 压缩包文件数量或重名检查失败。')
        index = {m.filename: m for m in members}
        for row in rows:
            item = index.get(row['archive_key'])
            if (item is None or item.is_dir() or item.file_size != row['bytes']
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise NativeSetupError('JRE 压缩包缺少或包含不安全文件。')
            target = destination / _relative(row['file'])
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as src, target.open('xb') as dst:
                remaining = row['bytes']
                while remaining:
                    block = src.read(min(1024 * 1024, remaining))
                    if not block:
                        raise NativeSetupError('JRE 文件内容不完整。')
                    dst.write(block)
                    remaining -= len(block)
                if src.read(1):
                    raise NativeSetupError('JRE 文件大小超出清单。')
            if not _verified(target, row):
                raise NativeSetupError('JRE 解包后的 SHA256 校验失败。')


def _extract_seven(msi, stage, destination, rows, emit):
    with _MsiDatabase(msi) as db:
        entries = {}
        for key, filename, size, sequence in db.rows('SELECT `File`,`FileName`,`FileSize`,`Sequence` FROM `File`', 4):
            _safe_name(key)
            _safe_name(filename)
            if key in entries or not 0 < int(size) < 16 * 1024 ** 2:
                raise NativeSetupError('7-Zip MSI 文件表无效。')
            entries[key] = _FileEntry(key, Path(filename), int(size), int(sequence))
        media = db.rows('SELECT `Cabinet` FROM `Media`', 1)
        if len(media) != 1 or not media[0][0].startswith('#') or len(entries) > 2000:
            raise NativeSetupError('7-Zip MSI 压缩包结构无效。')
        _safe_name(media[0][0][1:])
        cabinet = stage / '7zip.cab'
        db.export_stream(media[0][0][1:], cabinet)
    extracted = _extract_cabinet(cabinet, stage / 'flat', entries, emit, deadline=time.monotonic() + 120)
    for row in rows:
        source = extracted.get(row['archive_key'])
        if source is None:
            raise NativeSetupError('7-Zip 解包缺少文件。')
        _copy_checked(source, destination / _relative(row['file']), row)


def _run(command, cwd, *, timeout=90):
    env = os.environ.copy()
    for key in ('JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'CLASSPATH'):
        env.pop(key, None)
    windows = Path(os.environ.get('WINDIR', 'C:/Windows'))
    env['PATH'] = os.pathsep.join([str(Path(command[0]).parent), str(windows / 'System32'), str(windows)])
    for key in ('TEMP', 'TMP', 'TMPDIR'):
        env[key] = str(cwd)
    try:
        result = subprocess.run(list(map(str, command)), cwd=cwd, env=env, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeSetupError('转换组件启动验证失败，缓存已保留，可检查权限后重试。') from exc
    if result.returncode:
        raise NativeSetupError('转换组件启动失败：' + (result.stderr or result.stdout).decode('utf-8', 'replace')[-1000:])
    return result.stdout + result.stderr


def _extract_ghost(archive, stage, destination, rows, seven):
    extracted = stage / 'gs'
    extracted.mkdir()
    # The reviewed installer is never executed. 7-Zip receives only fixed
    # subdirectories and writes into this newly-created private staging area.
    _run([seven / '7z.exe', 'x', '-y', '-bd', '-bb0', '-o' + str(extracted), str(archive),
          'bin/*', 'Resource/*', 'iccprofiles/*', 'lib/*'], stage, timeout=180)
    for row in rows:
        source = _safe_tree_file(extracted, row['archive_key'])
        if source is None:
            raise NativeSetupError('Ghostscript 压缩包包含链接文件。')
        _copy_checked(source, destination / _relative(row['file']), row)


def _prepare_ghost_crt(project, home, manifest, component, destination, emit, offline):
    missing = []
    for row in component['files']:
        if not row.get('artifact'):
            continue
        candidates = [p / _relative(row['file']) for p in _candidates(project, home, 'Ghostscript', component)]
        candidates += [p / 'runtime' / _relative(row['file'])
                       for p in _candidates(project, home, 'OFD', manifest['components']['OFD'])]
        existing = next((p for p in candidates if _verified(p, row)), None)
        if existing:
            _copy_checked(existing, destination / _relative(row['file']), row)
        else:
            missing.append(row)
    if missing:
        archive = _download(manifest, 'java', home, emit, offline)
        _zip_files(archive, destination, missing)


def _prepare_ofd(project, home, manifest, component, destination, emit, offline):
    candidates = _candidates(project, home, 'OFD', component)
    pending = []
    for row in component['files']:
        existing = next((p / _relative(row['file']) for p in candidates if _files_complete(p, [row])), None)
        if existing is not None:
            _copy_checked(existing, destination / _relative(row['file']), row)
        elif row.get('artifact'):
            pending.append(row)
        else:
            raise NativeSetupError('源码中的 OFD 小型桥文件缺失或损坏，请完整重新解压源码包。')
    def fetch(row):
        downloaded = _download(manifest, row['artifact'], home, emit, offline)
        _copy_checked(downloaded, destination / _relative(row['file']), row)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, pending))
    for candidate in candidates:
        for rows in component['runtime_variants'].values():
            if _files_complete(candidate, rows):
                for row in rows:
                    _copy_checked(candidate / _relative(row['file']), destination / _relative(row['file']), row)
                return
    archive = _download(manifest, component['artifact'], home, emit, offline)
    _zip_files(archive, destination, component['runtime_variants']['jre'])


def _probe(key, root, temporary):
    if key == '7zip':
        output = _run([root / '7z.exe', 'i'], temporary)
        expected = b'7-Zip 26.05'
    elif key == 'Ghostscript':
        output = _run([root / 'bin/gswin64c.exe', '--version'], temporary)
        expected = b'10.08.0'
    else:
        output = _run([root / 'runtime/bin/java.exe', '-Djava.awt.headless=true', '-cp',
                       str(root / 'bridge.jar') + os.pathsep + str(root / 'lib/*'),
                       'local.qingjian.OFDBridge', '--version'], temporary)
        expected = b'OFDRW 2.5.1'
    if expected not in output:
        raise NativeSetupError('转换组件版本验证失败：' + key)


def _copy_notices(project, key, destination):
    source = Path(project) / 'support' / key
    paths = []
    if (source / 'licenses').is_dir():
        paths.extend((source / 'licenses').rglob('*'))
    for name in ('source-origins.json', 'library-origins.json', '使用与重建说明.txt'):
        paths.append(source / name)
    for path in paths:
        if path.is_file() and not _is_link(path):
            if path.stat().st_size > 1024 * 1024:
                raise NativeSetupError('许可材料大小异常。')
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _cleanup(stage, parent, token):
    if (stage.parent.resolve() != parent.resolve() or _is_link(stage)
            or not stage.name.startswith('.native-stage-')):
        raise NativeSetupError('拒绝清理不属于本次启动的目录。')
    record = json.loads((stage / _OWNER).read_text(encoding='utf-8'))
    if record != {'token': token, 'kind': 'stage'}:
        raise NativeSetupError('临时目录标记不一致，已保留目录。')
    shutil.rmtree(stage)


def ensure_native(project, home, emit=lambda event: None, *, offline=False):
    """Caller holds bootstrap.preparation_lock over the selected runtime home."""
    manifest = load_manifest(project)
    manifest['_project'] = str(Path(project).resolve())
    checked = check_native(project, home, manifest)
    from startup_shared import copy_component_tree, needs_preservation
    preserving = {key for key, root in checked['native_components'].items()
                  if needs_preservation(root, project, home)}
    if checked['native_ready'] and not preserving:
        emit({'event': 'log', 'message': '原生转换组件完整，直接复用，不重复下载。'})
        return checked['native_components']
    if os.name != 'nt':
        raise NativeSetupError('自动准备原生转换组件目前支持 Windows 64 位。')
    parent = Path(home).resolve() / 'native'
    if _is_link(parent):
        raise NativeSetupError('原生组件部署目录不能是链接或重解析目录。')
    parent.mkdir(parents=True, exist_ok=True)
    roots = dict(checked['native_components'])
    for key in COMPONENTS:
        if key in roots and key not in preserving:
            continue
        component = manifest['components'][key]
        target = _target(home, key, component).absolute()
        if _is_link(target):
            raise NativeSetupError('原生组件目标目录不能是链接。')
        emit({'event': 'progress', 'message': '正在补齐离线转换组件：' + key, 'stage': 'native-prepare'})
        token = uuid.uuid4().hex
        stage = parent / ('.native-stage-' + token[:10])
        stage.mkdir()
        (stage / _OWNER).write_text(json.dumps({'token': token, 'kind': 'stage'}), encoding='utf-8')
        destination = stage / 'component'
        destination.mkdir()
        try:
            required = sum(r['bytes'] for r in component['files']) + 256 * 1024 ** 2
            if shutil.disk_usage(parent).free < required:
                raise NativeSetupError('所选部署盘剩余空间不足，请释放空间后重试。')
            if key in preserving:
                emit({'event': 'log', 'message': '正在保存已有' + key + '到包外共享目录，无需下载。'})
                copy_component_tree(roots[key], destination)
            elif key == 'OFD':
                _prepare_ofd(project, home, manifest, component, destination, emit, offline)
            else:
                archive = _download(manifest, component['artifact'], home, emit, offline)
                if key == '7zip':
                    _extract_seven(archive, stage, destination, component['files'], emit)
                else:
                    _extract_ghost(archive, stage, destination,
                                   [r for r in component['files'] if not r.get('artifact')], Path(roots['7zip']))
                    _prepare_ghost_crt(project, home, manifest, component, destination, emit, offline)
            if not _complete(destination, component):
                raise NativeSetupError('原生组件解包后完整性验证失败：' + key)
            _copy_notices(project, key, destination)
            _probe(key, destination, stage)
            (destination / _OWNER).write_text(json.dumps({'kind': 'component', 'component': key,
                'signature': _signature(component)}), encoding='utf-8')
            if target.exists():
                try:
                    owned = json.loads((target / _OWNER).read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    owned = {}
                if owned.get('kind') != 'component' or owned.get('component') != key:
                    raise NativeSetupError('目标目录已存在且不属于本工具，请重新选择部署位置：' + str(target))
                # Preserve damaged files for diagnosis; never remove a user's
                # directory or replace a running engine in place.
                target.rename(parent / (key + '-incomplete-' + uuid.uuid4().hex[:10]))
            destination.rename(target)
            roots[key] = str(target.resolve())
            emit({'event': 'log', 'message': key + ' 已准备完成，今后可离线复用。'})
        finally:
            try:
                _cleanup(stage, parent, token)
            except (OSError, ValueError, NativeSetupError) as exc:
                emit({'event': 'log', 'message': '已保留临时目录，可稍后清理：' + str(stage) + '（' + str(exc) + '）'})
    return roots
