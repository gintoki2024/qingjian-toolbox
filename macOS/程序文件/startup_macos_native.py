"""Verified macOS converter preparation, without installers or global changes.

This module deliberately uses only the Python standard library. A DMG is
mounted read-only; its app is copied privately. The MacTeX package is read as
XAR/gzip/cpio data: its installer scripts are never executed.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zlib

COMPONENTS = ('7zip', 'Ghostscript', 'OFD', 'LibreOffice')
ENVIRONMENT_KEY = 'QINGJIAN_NATIVE_COMPONENTS'
RECEIPT = '.qingjian-macos-native.json'
MAX_FILES = 30000
MAX_EXPANDED = 4 * 1024 ** 3


class NativeSetupError(RuntimeError):
    pass


def architecture_name(value=None):
    value = (value or platform.machine()).lower()
    aliases = {'aarch64': 'arm64', 'arm64': 'arm64', 'x64': 'x86_64',
               'amd64': 'x86_64', 'x86_64': 'x86_64'}
    if value not in aliases:
        raise NativeSetupError('当前 Mac 处理器暂不支持：' + value)
    return aliases[value]


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise NativeSetupError('组件含有不安全的相对路径。')
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or any(p in ('', '.', '..') or ':' in p for p in path.parts):
        raise NativeSetupError('组件含有不安全的相对路径。')
    if str(path) != value:
        raise NativeSetupError('组件路径格式不规范。')
    return Path(*path.parts)


def _file_spec(row):
    _relative(row['file'])
    if (type(row.get('bytes')) is not int or not 0 <= row['bytes'] <= MAX_EXPANDED
            or not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', ''))):
        raise NativeSetupError('组件缺少固定大小或 SHA256。')


def _https(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise NativeSetupError('组件下载只允许不含登录信息的 HTTPS 地址。')
    return url


def load_manifest(project, architecture=None):
    arch = architecture_name(architecture)
    try:
        path = Path(project) / 'macos-native-sources.json'
        if path.stat().st_size > 3 * 1024 ** 2:
            raise ValueError('组件清单过大')
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
        if manifest['schema_version'] != 1 or manifest['platform'] != 'macos':
            raise ValueError('组件清单平台不正确')
        result = manifest['architectures'][arch]
        if set(result['components']) != set(COMPONENTS):
            raise ValueError('组件清单不完整')
        filenames = set()
        for spec in result['artifacts'].values():
            _file_spec(dict(spec, file=spec['filename']))
            if '/' in spec['filename'] or spec['filename'] in filenames or len(spec['urls']) < 2:
                raise ValueError('下载名称重复或缺少备用来源')
            filenames.add(spec['filename'])
            for source in spec['urls']:
                _https(source['url'])
        for key, spec in result['components'].items():
            if spec['kind'] != {'7zip': 'tar', 'Ghostscript': 'mactex-ghostscript',
                                'OFD': 'ofd', 'LibreOffice': 'dmg'}[key]:
                raise ValueError('未知的组件类型')
            if spec['artifact'] not in result['artifacts']:
                raise ValueError('缺少下载信息')
            for row in spec.get('files', []):
                _file_spec(row)
            for name in spec['required']:
                _relative(name)
            for artifact in spec.get('libraries', []):
                if artifact not in result['artifacts']:
                    raise ValueError('缺少 Java 依赖信息')
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise NativeSetupError('Mac 转换组件清单无法读取，请重新完整解压源码包：' + str(exc)) from exc


def _signature(spec, manifest, arch):
    artifacts = [spec['artifact'], *spec.get('libraries', [])]
    pin = {'architecture': arch, 'component': spec,
           'artifacts': {key: {k: manifest['artifacts'][key][k]
                              for k in ('filename', 'bytes', 'sha256')} for key in artifacts}}
    return hashlib.sha256(json.dumps(pin, sort_keys=True).encode()).hexdigest()[:20]


def _verified(path, spec):
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != spec['bytes']:
            return False
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 ** 2), b''):
                digest.update(block)
        return digest.hexdigest() == spec['sha256']
    except OSError:
        return False


def _inside(root, path):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _check_links(root):
    root = Path(root)
    if root.is_symlink():
        raise NativeSetupError('组件目录不能是符号链接。')
    count = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            count += 1
            if count > MAX_FILES:
                raise NativeSetupError('组件解压文件过多。')
            if path.is_symlink() and not _inside(root, path):
                raise NativeSetupError('组件链接指向目录以外。')
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                raise NativeSetupError('组件包含不支持的特殊文件。')


def _snapshot(root):
    _check_links(root)
    rows, total = [], 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if relative == RECEIPT or (path.is_dir() and not path.is_symlink()):
                continue
            if path.is_symlink():
                rows.append({'file': relative, 'link': os.readlink(path)})
                continue
            size = path.stat().st_size
            total += size
            if total > MAX_EXPANDED:
                raise NativeSetupError('组件解压内容超过允许大小。')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 ** 2), b''):
                    digest.update(block)
            rows.append({'file': relative, 'bytes': size, 'sha256': digest.hexdigest(),
                         'executable': bool(path.stat().st_mode & stat.S_IXUSR)})
    return sorted(rows, key=lambda row: row['file'])


def _complete(root, spec, signature):
    try:
        root = Path(root)
        receipt = root / RECEIPT
        if root.is_symlink() or receipt.is_symlink() or receipt.stat().st_size > 12 * 1024 ** 2:
            return False
        _check_links(root)
        data = json.loads(receipt.read_text(encoding='utf-8'))
        if data.get('signature') != signature or data.get('schema_version') != 1:
            return False
        rows = data['files']
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
            return False
        seen = set()
        for row in rows:
            relative = _relative(row['file'])
            if row['file'] in seen:
                return False
            seen.add(row['file'])
            path = root / relative
            if not _inside(root, path):
                return False
            if 'link' in row:
                if not path.is_symlink() or os.readlink(path) != row['link']:
                    return False
            elif not _verified(path, row):
                return False
            elif row.get('executable') and os.name != 'nt' and not os.access(path, os.X_OK):
                return False
        if not all(name in seen and (root / _relative(name)).is_file() for name in spec['required']):
            return False
        return all(_verified(root / _relative(row['file']), row) for row in spec.get('files', []))
    except (OSError, ValueError, TypeError, KeyError, NativeSetupError):
        return False


def _candidates(project, home, key, signature):
    project, home = Path(project).resolve(), Path(home).resolve()
    paths = [project / 'native' / key, home / 'native' / (key + '-' + signature),
             project / 'support' / 'macos' / key]
    try:
        configured = json.loads(os.environ.get(ENVIRONMENT_KEY, '{}'))
        if isinstance(configured.get(key), str) and Path(configured[key]).is_absolute():
            paths.append(Path(configured[key]))
    except (ValueError, TypeError, AttributeError):
        pass
    return list(dict.fromkeys(paths))


def _environment(components):
    env = {ENVIRONMENT_KEY: json.dumps({key: str(root) for key, root in components.items()}, ensure_ascii=False)}
    if 'LibreOffice' in components:
        env['QINGJIAN_LIBREOFFICE'] = str(Path(components['LibreOffice']) / 'LibreOffice.app/Contents/MacOS/soffice')
    if 'Ghostscript' in components:
        base = Path(components['Ghostscript']) / 'share/ghostscript/10.07.0'
        env['GS_LIB'] = os.pathsep.join(str(base / path) for path in ('Resource/Init', 'lib', 'Resource/Font'))
    return env


def check_native(project, home, architecture=None):
    """Check only pinned private roots; do not download, execute, or scan disks."""
    arch = architecture_name(architecture)
    manifest = load_manifest(project, arch)
    components, missing = {}, []
    for key, spec in manifest['components'].items():
        signature = _signature(spec, manifest, arch)
        candidate = next((path for path in _candidates(project, home, key, signature)
                          if _complete(path, spec, signature)), None)
        if candidate is None:
            missing.append(key)
        else:
            components[key] = str(candidate.resolve())
    return {'ready': not missing, 'architecture': arch, 'components': components,
            'missing': missing, 'env': _environment(components)}


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(spec, cache, emit=None, offline=False):
    cache = Path(cache)
    if cache.is_symlink():
        raise NativeSetupError('下载缓存目录不能是符号链接。')
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / spec['filename']
    if _verified(path, spec):
        return path
    if offline:
        raise NativeSetupError('离线检查缺少已校验的组件：' + spec['filename'])
    failures = []
    opener = urllib.request.build_opener(_HTTPSRedirect())
    for source in spec['urls']:
        if emit:
            emit('正在补齐 ' + spec['filename'] + '，来源：' + source['name'])
        temporary = cache / ('.download-' + uuid.uuid4().hex)
        try:
            request = urllib.request.Request(_https(source['url']), headers={'User-Agent': 'QingjianToolbox-Mac/1'})
            digest, size = hashlib.sha256(), 0
            with opener.open(request, timeout=45) as response, temporary.open('xb') as output:
                _https(response.url)
                length = response.headers.get('Content-Length')
                if length and int(length) != spec['bytes']:
                    raise NativeSetupError('下载大小与固定清单不同。')
                while True:
                    block = response.read(1024 ** 2)
                    if not block:
                        break
                    size += len(block)
                    if size > spec['bytes']:
                        raise NativeSetupError('下载内容超过固定大小。')
                    digest.update(block)
                    output.write(block)
            if size != spec['bytes'] or digest.hexdigest() != spec['sha256']:
                raise NativeSetupError('下载内容校验失败。')
            if path.is_symlink():
                raise NativeSetupError('缓存位置不能是符号链接。')
            os.replace(temporary, path)
            return path
        except (OSError, ValueError, NativeSetupError) as exc:
            failures.append(source['name'] + '：' + str(exc))
        finally:
            temporary.unlink(missing_ok=True)
    raise NativeSetupError('组件下载未完成，请检查网络后重试。\n' + '\n'.join(failures))


def _extract_tar(archive, destination, strip_prefix=''):
    """Bounded extraction that never follows archive-created links."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    prefix = strip_prefix.rstrip('/')
    links, seen, total = [], set(), 0
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if len(members) > MAX_FILES:
            raise NativeSetupError('组件压缩包条目过多。')
        for member in members:
            name = member.name.rstrip('/')
            while name.startswith('./'):
                name = name[2:]
            if not name:
                continue
            _relative(name)
            if prefix:
                if name == prefix or prefix.startswith(name + '/'):
                    continue
                if not name.startswith(prefix + '/'):
                    continue
                name = name[len(prefix) + 1:]
            relative = _relative(name)
            if name in seen:
                raise NativeSetupError('组件压缩包含有重复路径。')
            seen.add(name)
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                total += member.size
                if total > MAX_EXPANDED:
                    raise NativeSetupError('组件压缩包解压后过大。')
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as src, target.open('xb') as output:
                    shutil.copyfileobj(src, output, 1024 ** 2)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            elif member.issym():
                if PurePosixPath(member.linkname).is_absolute() or '\\' in member.linkname:
                    raise NativeSetupError('组件压缩包含有外部链接。')
                if not _inside(destination, target.parent / member.linkname):
                    raise NativeSetupError('组件压缩包链接越界。')
                links.append((target, member.linkname))
            else:
                raise NativeSetupError('组件压缩包包含不支持的硬链接或特殊文件。')
    # No file may be placed through a symlink, regardless of member order.
    for target, link in links:
        if target.exists() or any(str(path).startswith(str(target) + os.sep) for path, _ in links):
            raise NativeSetupError('组件压缩包符号链接路径冲突。')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(link)
    _check_links(destination)


def _xar_payload(archive, expected_name):
    with Path(archive).open('rb') as stream:
        header = stream.read(28)
        if len(header) != 28:
            raise NativeSetupError('Ghostscript 安装包头损坏。')
        magic, header_size, version, compressed, expanded, _ = struct.unpack('>IHHQQI', header)
        if magic != 0x78617221 or version != 1 or header_size < 28 or compressed > 2 ** 20 or expanded > 4 * 2 ** 20:
            raise NativeSetupError('Ghostscript 安装包结构不符合固定版本。')
        stream.seek(header_size)
        decoder = zlib.decompressobj()
        toc = decoder.decompress(stream.read(compressed), expanded + 1)
        if len(toc) != expanded or not decoder.eof or b'<!DOCTYPE' in toc.upper():
            raise NativeSetupError('Ghostscript 安装包目录无效。')
        root = ET.fromstring(toc)
        folders = [node for node in root.findall('./toc/file') if node.findtext('name') == expected_name]
        if len(folders) != 1:
            raise NativeSetupError('Ghostscript 安装包内容与清单不符。')
        payloads = [node for node in folders[0].findall('file') if node.findtext('name') == 'Payload']
        if len(payloads) != 1:
            raise NativeSetupError('Ghostscript 安装包缺少唯一 Payload。')
        data = payloads[0].find('data')
        offset, length = int(data.findtext('offset')), int(data.findtext('length'))
        if offset < 0 or length < 0 or length > 512 * 1024 ** 2:
            raise NativeSetupError('Ghostscript Payload 大小无效。')
        stream.seek(header_size + compressed + offset)
        payload = stream.read(length)
        if len(payload) != length or not payload.startswith(b'\x1f\x8b'):
            raise NativeSetupError('Ghostscript Payload 不完整。')
        return payload


def _extract_ghostscript(archive, destination, spec):
    payload = _xar_payload(archive, spec['package_directory'])
    destination = Path(destination)
    total, count, seen = 0, 0, set()
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
        while True:
            header = stream.read(76)
            if len(header) != 76 or header[:6] != b'070707':
                raise NativeSetupError('Ghostscript cpio 数据损坏。')
            mode, size, name_size = int(header[18:24], 8), int(header[65:76], 8), int(header[59:65], 8)
            if not 1 <= name_size <= 4096 or size > 512 * 1024 ** 2:
                raise NativeSetupError('Ghostscript cpio 条目过大。')
            raw_name = stream.read(name_size)
            if len(raw_name) != name_size or raw_name[-1:] != b'\x00':
                raise NativeSetupError('Ghostscript cpio 名称损坏。')
            name = raw_name[:-1].decode('utf-8')
            if name == 'TRAILER!!!':
                break
            count += 1
            total += size
            if count > MAX_FILES or total > MAX_EXPANDED:
                raise NativeSetupError('Ghostscript 解压内容超过允许大小。')
            while name.startswith('./'):
                name = name[2:]
            if name not in ('', '.'):
                _relative(name)
            target_name = None
            if name == 'usr/local/bin/gs-noX11':
                target_name = 'bin/gs'
            elif name.startswith('usr/local/share/ghostscript/') and '/._' not in name:
                target_name = name[len('usr/local/'):]
            content = stream.read(size)
            if len(content) != size:
                raise NativeSetupError('Ghostscript cpio 条目不完整。')
            if not target_name:
                continue
            target = destination / _relative(target_name)
            if target_name in seen:
                raise NativeSetupError('Ghostscript 文件路径重复。')
            seen.add(target_name)
            if stat.S_ISDIR(mode):
                target.mkdir(parents=True, exist_ok=True)
            elif stat.S_ISREG(mode):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o755 if mode & 0o111 else 0o644)
            else:
                raise NativeSetupError('Ghostscript 资源目录含有未核验链接。')


def _extract_dmg(archive, destination):
    if platform.system() != 'Darwin':
        raise NativeSetupError('LibreOffice 的 DMG 挂载和签名验证需要在 Mac 上完成。')
    destination = Path(destination)
    mount = destination.parent / ('.mount-' + uuid.uuid4().hex)
    mount.mkdir()
    attached = False
    try:
        result = subprocess.run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-noautoopen',
                                 '-plist', '-mountpoint', str(mount), str(archive)],
                                capture_output=True, check=True, timeout=180)
        data = plistlib.loads(result.stdout)
        attached = any(entity.get('mount-point') == str(mount) for entity in data.get('system-entities', []))
        if not attached:
            raise NativeSetupError('LibreOffice 镜像未在指定只读位置挂载。')
        bundle = mount / 'LibreOffice.app'
        if not bundle.is_dir() or bundle.is_symlink():
            raise NativeSetupError('镜像中没有预期的 LibreOffice.app。')
        _check_links(bundle)
        shutil.copytree(bundle, destination / 'LibreOffice.app', symlinks=True)
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        raise NativeSetupError('LibreOffice 私有解压失败：' + str(exc)) from exc
    finally:
        # Also attempt detach when attach succeeded but plist parsing failed.
        if attached or mount.is_mount():
            result = subprocess.run(['/usr/bin/hdiutil', 'detach', str(mount)], capture_output=True, timeout=60)
            if result.returncode:
                raise NativeSetupError('LibreOffice 只读镜像仍被占用，请弹出后重试：' + str(mount))
        if mount.exists() and not mount.is_mount():
            mount.rmdir()


def _copy_ofd_support(project, destination, spec):
    source = Path(project) / 'support/OFD'
    for name in ('bridge.jar', 'licenses', 'source', 'build-info.json', 'library-origins.json', 'source-origins.json', '使用与重建说明.txt'):
        path = source / name
        if not path.exists():
            continue
        if path.is_symlink():
            raise NativeSetupError('OFD 支持文件不能是符号链接。')
        if path.is_dir():
            _check_links(path)
            shutil.copytree(path, destination / name, symlinks=True)
        else:
            shutil.copy2(path, destination / name)
    for row in spec['files']:
        if row['file'] == 'bridge.jar' and not _verified(destination / 'bridge.jar', row):
            raise NativeSetupError('随包 OFD 桥校验失败，请重新解压源码包。')


def _copy_notices(project, destination, key):
    notices = Path(project) / 'support/macos'
    selected = ['原生组件来源与许可.txt']
    if key == 'Ghostscript':
        selected.append('Ghostscript-COPYING')
    for name in selected:
        path = notices / name
        if path.is_file() and not path.is_symlink():
            directory = Path(destination) / 'licenses'
            if directory.is_symlink():
                raise NativeSetupError('许可材料目录不能是符号链接。')
            directory.mkdir(exist_ok=True)
            shutil.copy2(path, directory / name)


def _write_receipt(root, key, spec, signature, architecture):
    for row in spec.get('files', []):
        if not _verified(Path(root) / _relative(row['file']), row):
            raise NativeSetupError('解压后的固定组件文件校验失败：' + row['file'])
    if not all((Path(root) / _relative(name)).is_file() for name in spec['required']):
        raise NativeSetupError('解压后的转换组件不完整：' + key)
    receipt = {'schema_version': 1, 'component': key, 'signature': signature,
               'architecture': architecture, 'files': _snapshot(root)}
    temporary = Path(root) / ('.receipt-' + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        os.replace(temporary, Path(root) / RECEIPT)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_native(project, home, emit=None, offline=False, architecture=None):
    """Reuse verified roots; otherwise prepare only missing private components."""
    arch = architecture_name(architecture)
    state = check_native(project, home, arch)
    if state['ready']:
        return state
    manifest = load_manifest(project, arch)
    shared_home = Path(home).resolve()
    native = shared_home / 'native'
    cache = shared_home / 'cache/macos-native'
    if native.is_symlink() or cache.is_symlink() or (shared_home / 'cache').is_symlink():
        raise NativeSetupError('组件或缓存目录不能是符号链接。')
    native.mkdir(parents=True, exist_ok=True)
    lock = native / '.prepare-macos.lock'
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise NativeSetupError('另一项 Mac 环境准备仍在进行。若上次被强制中断，请确认没有准备进程后删除：' + str(lock)) from exc
    try:
        for key in state['missing']:
            spec = manifest['components'][key]
            signature = _signature(spec, manifest, arch)
            target = native / (key + '-' + signature)
            if _complete(target, spec, signature):
                continue
            stage = native / ('.staging-' + uuid.uuid4().hex)
            stage.mkdir()
            try:
                archive = _download(manifest['artifacts'][spec['artifact']], cache, emit, offline)
                kind = spec['kind']
                if kind == 'tar':
                    _extract_tar(archive, stage)
                elif kind == 'dmg':
                    _extract_dmg(archive, stage)
                elif kind == 'mactex-ghostscript':
                    _extract_ghostscript(archive, stage, spec)
                elif kind == 'ofd':
                    _extract_tar(archive, stage / 'runtime', spec['runtime_prefix'])
                    _copy_ofd_support(project, stage, spec)
                    libraries = stage / 'lib'
                    libraries.mkdir()
                    for library in spec['libraries']:
                        metadata = manifest['artifacts'][library]
                        path = _download(metadata, cache, emit, offline)
                        shutil.copy2(path, libraries / metadata['filename'])
                _copy_notices(project, stage, key)
                _write_receipt(stage, key, spec, signature, arch)
                if not _complete(stage, spec, signature):
                    raise NativeSetupError('组件完成后的再次校验失败：' + key)
                if target.exists() or target.is_symlink():
                    # Preserve damaged old bytes for review; never overwrite a source.
                    target.rename(native / ('.damaged-' + key + '-' + uuid.uuid4().hex))
                stage.rename(target)
                if emit:
                    emit(key + ' 已就绪，后续可离线复用。')
            finally:
                if stage.exists() and stage.parent == native and stage.name.startswith('.staging-') and not stage.is_symlink():
                    shutil.rmtree(stage)
        return check_native(project, home, arch)
    finally:
        lock.rmdir()


def check_redistribution_materials(project, home, architecture=None):
    """A full-package builder must call this in addition to check_native."""
    state = check_native(project, home, architecture)
    if not state['ready']:
        return dict(ready=False, missing=state['missing'])
    metadata = json.loads((Path(project) / 'macos-native-sources.json').read_text(encoding='utf-8-sig'))
    missing = []
    for key, spec in metadata.get('redistribution_artifacts', {}).items():
        target = Path(state['components'][spec['component']]) / _relative(spec['destination'])
        if not _verified(target, spec):
            missing.append(key)
    root = Path(state['components']['Ghostscript'])
    if not (root / 'licenses/Ghostscript-COPYING').is_file():
        missing.append('Ghostscript-COPYING')
    return dict(ready=not missing, missing=missing)


def ensure_redistribution_materials(project, home, emit=None, offline=False, architecture=None):
    """Explicit build preparation; ordinary lightweight startup skips sources.

    Download fixed corresponding source materials privately, before an offline
    full builder copies complete verified component roots. No source is built.
    """
    arch = architecture_name(architecture)
    state = ensure_native(project, home, emit=emit, offline=offline, architecture=arch)
    metadata = json.loads((Path(project) / 'macos-native-sources.json').read_text(encoding='utf-8-sig'))
    cache = Path(home).resolve() / 'cache/macos-native'
    for spec in metadata.get('redistribution_artifacts', {}).values():
        _file_spec(dict(spec, file=spec['filename']))
        _relative(spec['destination'])
        for source in spec['urls']:
            _https(source['url'])
        root = Path(state['components'][spec['component']])
        target = root / _relative(spec['destination'])
        if not _inside(root, target):
            raise NativeSetupError('发行源码材料路径越界。')
        if not _verified(target, spec):
            archive = _download(spec, cache, emit, offline)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.parent / ('.source-' + uuid.uuid4().hex)
            try:
                shutil.copy2(archive, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        _copy_notices(project, root, spec['component'])
        manifest = load_manifest(project, arch)
        component = manifest['components'][spec['component']]
        _write_receipt(root, spec['component'], component, _signature(component, manifest, arch), arch)
    result = check_redistribution_materials(project, home, arch)
    if not result['ready']:
        raise NativeSetupError('完整包缺少源码或许可材料：' + '、'.join(result['missing']))
    return result
