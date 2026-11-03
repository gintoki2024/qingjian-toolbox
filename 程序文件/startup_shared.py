"""Small cross-release location memory; runtime bytes stay on the chosen disk."""
from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

_PACKAGE = re.compile(r'青简工具箱(?:源码|_v\d+\.\d+\.\d+(?:[_ ].*)?)?\Z')
_MAX_RECORDS = 16


def _link(path):
    try:
        status = Path(path).lstat()
        return stat.S_ISLNK(status.st_mode) or bool(getattr(status, 'st_file_attributes', 0) & 0x400)
    except OSError:
        return False


def package_root(project):
    """The removable package includes known outer ZIP wrapper directories."""
    root = Path(project).resolve()
    if root.name == '程序文件':
        root = root.parent
    while root != root.parent and _PACKAGE.fullmatch(root.parent.name):
        root = root.parent
    return root


def default_runtime_home(project):
    """Suggest a writable sibling, outside known source/ZIP wrapper folders."""
    return package_root(project).parent / '青简共享环境'


def catalog_path():
    override = os.environ.get('QINGJIAN_SHARED_RUNTIME_INDEX')
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else None
    user_data = os.environ.get('QINGJIAN_USER_DATA')
    if user_data and Path(user_data).is_absolute():
        return Path(user_data) / 'shared-runtime.json'
    local = os.environ.get('LOCALAPPDATA')
    if local and Path(local).is_absolute():
        return Path(local) / 'QingjianLocal' / 'shared-runtime.json'
    return None


def _read(path, maximum=65536):
    try:
        path = Path(path)
        if _link(path) or _link(path.parent) or path.stat().st_size > maximum:
            return None
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def _absolute(value):
    return isinstance(value, str) and bool(value) and '\x00' not in value and Path(value).is_absolute()


def _entry(value):
    if (not isinstance(value, dict) or not _absolute(value.get('runtime_home'))
            or value.get('mode') not in {'reuse', 'managed'}):
        return None
    result = dict(value)
    for key in ('project', 'python', 'engine'):
        if not _absolute(result.get(key)):
            result.pop(key, None)
    mapping = result.get('native_components', {})
    result['native_components'] = {key: path for key, path in mapping.items()
                                   if key in {'7zip', 'Ghostscript', 'OFD'} and _absolute(path)} if isinstance(mapping, dict) else {}
    result['mode'] = value['mode']
    result['updated_ns'] = result.get('updated_ns') if type(result.get('updated_ns')) is int else 0
    return result


def _entries():
    path = catalog_path()
    value = _read(path) if path is not None else None
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        return []
    rows = value.get('entries')
    if not isinstance(rows, list) or len(rows) > _MAX_RECORDS:
        return []
    return sorted((entry for row in rows if (entry := _entry(row))), key=lambda row: row['updated_ns'], reverse=True)


def legacy_projects(project):
    """Inspect fixed layouts in at most three parents; never recurse a drive."""
    project = Path(project).resolve()
    candidates = [project]
    for parent in list(project.parents)[:3]:
        if parent == parent.parent:
            continue
        try:
            with os.scandir(parent) as entries:
                neighbors = [Path(item.path) for item in itertools.islice(entries, 256)
                             if _PACKAGE.fullmatch(item.name) and item.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        def release_order(item):
            match = re.search(r'_v(\d+)\.(\d+)\.(\d+)', item.name)
            return (tuple(map(int, match.groups())) if match else (0, 0, 0), item.name)
        for neighbor in sorted(neighbors, key=release_order, reverse=True):
            candidates.extend((neighbor / '程序文件', neighbor / '青简工具箱源码/程序文件',
                               neighbor / '青简工具箱源码', neighbor))
    result = []
    for candidate in dict.fromkeys(candidates):
        try:
            if candidate.is_dir() and not _link(candidate) and not _link(candidate.parent):
                result.append(candidate)
        except OSError:
            pass
    return result[:128]


def component_locations(project):
    """Untrusted location hints: callers must validate every component again."""
    result = _entries()
    for folder in legacy_projects(project):
        choice = _read(folder / 'config/startup-selection.json', 16384)
        if isinstance(choice, dict) and choice.get('schema_version') == 1:
            entry = _entry({**choice, 'project': str(folder)})
            if entry:
                result.append(entry)
                previous = choice.get('previous_runtime_homes', [])
                if isinstance(previous, list):
                    result.extend({'runtime_home': value, 'project': str(folder), 'native_components': {}}
                                  for value in previous[:4] if _absolute(value))
        home = folder / 'runtime'
        if home.is_dir() and not _link(home):
            result.append({'runtime_home': str(home), 'project': str(folder), 'native_components': {}})
        # Old full packages have bundled engines/support, rather than runtime.
        if (folder / 'engines').is_dir() or (folder / 'support').is_dir():
            result.append({'runtime_home': str(home), 'project': str(folder),
                           'engine': str(folder / 'engines/LibreOffice'),
                           'native_components': {key: str(folder / 'support' / key) for key in ('7zip', 'Ghostscript', 'OFD')}})
    return result


def _inside(path, parent):
    try:
        return Path(path).resolve().is_relative_to(Path(parent).resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def saved_selection(project, *, discover_legacy=True):
    """Recover a previous external choice; never resurrect a deleted location."""
    for row in component_locations(project) if discover_legacy else _entries():
        home, python = Path(row['runtime_home']), row.get('python')
        if not python or not home.is_dir() or not Path(python).is_file() or _link(home):
            continue
        if row.get('project') and _inside(home, package_root(row['project'])):
            continue  # An old package-local runtime still needs preservation.
        return {'schema_version': 1, 'mode': row.get('mode', 'reuse'),
                'python': python, 'runtime_home': str(home)}
    return None


def needs_preservation(root, project, home):
    """Preserve engines borrowed from removable toolbox folders, not installs."""
    if not root or _inside(root, home) or _inside(home, package_root(project)):
        return False
    if _inside(root, package_root(project)):
        return True
    if any(row.get('project') and _inside(root, package_root(row['project'])) for row in _entries()):
        return True
    return any(_PACKAGE.fullmatch(parent.name) for parent in (Path(root), *Path(root).parents))


@contextlib.contextmanager
def _catalog_lock(path):
    # OS locks are released even if a launcher crashes; no stale lock removal.
    with path.with_suffix('.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        locked = False
        for _ in range(20):
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                time.sleep(.05)
        if not locked:
            raise OSError('共享位置记录正由其他启动器更新。')
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def remember_runtime(project, python, home, engine, native_components, *, mode='reuse'):
    """Record only paths after successful preparation; no runtime bytes in C:."""
    path = catalog_path()
    if path is None:
        return False
    temporary = None
    try:
        if _link(path) or _link(path.parent) or not Path(home).is_dir():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with _catalog_lock(path):
            record = {'project': str(Path(project).resolve()), 'python': str(Path(python).absolute()),
                      'runtime_home': str(Path(home).resolve()), 'mode': mode,
                      'engine': str(Path(engine).resolve()),
                      'native_components': native_components, 'updated_ns': time.time_ns()}
            rows = [record] + [row for row in _entries()
                               if Path(row['runtime_home']).resolve() != Path(home).resolve()]
            temporary = path.with_name('.shared-runtime-' + uuid.uuid4().hex + '.tmp')
            temporary.write_text(json.dumps({'schema_version': 1, 'entries': rows[:_MAX_RECORDS]}, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def copy_component_tree(source, target):
    """Copy into a caller-owned stage, checking bytes and rejecting reparse data."""
    def filesystem(value):
        value = str(Path(value).absolute())
        if os.name == 'nt' and not value.startswith('\\\\?\\'):
            value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
        return Path(value)
    source, target = filesystem(source), filesystem(target)
    if _link(source):
        raise OSError('已有组件目录不能是链接或重解析目录。')
    count, total, directory_count = 0, 0, 0
    for directory, folders, files in os.walk(source, followlinks=False):
        directory_count += 1
        if directory_count > 50000:
            raise OSError('已有组件目录超过复制范围。')
        relative = Path(directory).relative_to(source)
        for name in [*folders, *files]:
            if _link(Path(directory) / name):
                raise OSError('已有组件含有链接，未复制：' + name)
        destination = target / relative
        destination.mkdir(parents=True, exist_ok=True)
        for name in files:
            original, copied = Path(directory) / name, destination / name
            if not stat.S_ISREG(original.lstat().st_mode):
                raise OSError('已有组件包含非普通文件：' + name)
            count += 1
            total += original.stat().st_size
            if count > 50000 or total > 4 * 1024 ** 3:
                raise OSError('已有组件目录超过复制范围。')
            expected = hashlib.sha256()
            with original.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    expected.update(block)
            shutil.copy2(original, copied)
            actual = hashlib.sha256()
            with copied.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    actual.update(block)
            if actual.digest() != expected.digest():
                raise OSError('已有组件复制后校验不一致：' + name)
