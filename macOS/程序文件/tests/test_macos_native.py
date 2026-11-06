"""macOS native preparation can be checked without executing Mac binaries."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import subprocess
import tarfile
import zlib

import pytest

import startup_macos_native as native


def _row(name, data):
    return {'file': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def _tar(path, rows):
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in rows.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if name.endswith(('7zz', '/java', '/gs', '/soffice')) else 0o644
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


@pytest.fixture
def fake_native(tmp_path, monkeypatch):
    project, home = tmp_path / 'program', tmp_path / 'shared'
    project.mkdir()
    cache = home / 'cache/macos-native'
    cache.mkdir(parents=True)
    support = project / 'support/OFD'
    support.mkdir(parents=True)
    (support / 'bridge.jar').write_bytes(b'bridge')
    notices = project / 'support/macos'
    notices.mkdir()
    (notices / 'Ghostscript-COPYING').write_bytes(b'AGPL')
    contents = {
        '7zip': {'7zz': b'mac-seven', 'License.txt': b'license'},
        'ghostscript': {'bin/gs': b'mac-gs', 'share/ghostscript/10.07.0/Resource/Init/gs_init.ps': b'init'},
        'java': {'bundle/Contents/Home/bin/java': b'mac-java', 'bundle/Contents/Home/release': b'21'},
        'libreoffice': {'LibreOffice.app/Contents/MacOS/soffice': b'mac-office', 'LibreOffice.app/Contents/Info.plist': b'plist'},
    }
    artifacts = {}
    for name, rows in contents.items():
        path = cache / (name + '.tar.gz')
        _tar(path, rows)
        spec = _row(path.name, path.read_bytes())
        spec['filename'] = spec.pop('file')
        spec['urls'] = [{'name': '镜像', 'url': 'https://mirror.example/' + path.name},
                        {'name': '官方', 'url': 'https://official.example/' + path.name}]
        artifacts[name] = spec
    jar = cache / 'library.jar'
    jar.write_bytes(b'library')
    artifacts['library'] = dict(filename=jar.name, bytes=7, sha256=hashlib.sha256(b'library').hexdigest(),
                                urls=[{'name': 'a', 'url': 'https://a.example/library.jar'},
                                      {'name': 'b', 'url': 'https://b.example/library.jar'}])
    components = {
        '7zip': dict(kind='tar', artifact='7zip', required=list(contents['7zip']), files=[_row(n, d) for n, d in contents['7zip'].items()]),
        'Ghostscript': dict(kind='mactex-ghostscript', artifact='ghostscript', required=list(contents['ghostscript']), files=[_row(n, d) for n, d in contents['ghostscript'].items()]),
        'OFD': dict(kind='ofd', artifact='java', runtime_prefix='bundle/Contents/Home', libraries=['library'],
                    required=['runtime/bin/java', 'bridge.jar', 'lib/library.jar'],
                    files=[_row('runtime/bin/java', b'mac-java'), _row('bridge.jar', b'bridge'), _row('lib/library.jar', b'library')]),
        'LibreOffice': dict(kind='dmg', artifact='libreoffice', required=list(contents['libreoffice']), files=[]),
    }
    data = dict(schema_version=1, platform='macos', architectures={arch: dict(artifacts=artifacts, components=components) for arch in ('arm64', 'x86_64')})
    (project / 'macos-native-sources.json').write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.delenv(native.ENVIRONMENT_KEY, raising=False)
    monkeypatch.setattr(native, '_extract_ghostscript', lambda archive, root, spec: native._extract_tar(archive, root))
    monkeypatch.setattr(native, '_extract_dmg', lambda archive, root: native._extract_tar(archive, root))
    return project, home, data


@pytest.mark.parametrize('value,expected', [('arm64', 'arm64'), ('aarch64', 'arm64'), ('x64', 'x86_64'), ('AMD64', 'x86_64')])
def test_architecture_aliases(value, expected):
    assert native.architecture_name(value) == expected


def test_unknown_architecture_has_understandable_error():
    with pytest.raises(native.NativeSetupError, match='处理器'):
        native.architecture_name('powerpc')


def test_shipping_manifest_pins_both_architectures_and_all_directions():
    project = Path(__file__).resolve().parents[1]
    for arch in ('arm64', 'x86_64'):
        manifest = native.load_manifest(project, arch)
        assert set(manifest['components']) == set(native.COMPONENTS)
        assert len(manifest['components']['OFD']['libraries']) == 37
        assert manifest['components']['Ghostscript']['kind'] == 'mactex-ghostscript'
        assert 'windows' not in manifest['artifacts']['java']['filename']
        assert manifest['artifacts']['7zip']['filename'].endswith('-mac.tar.xz')
        assert all(row['sha256'] for spec in manifest['components'].values() for row in spec['files'])


@pytest.mark.parametrize('url', ['http://example.com/a', 'file:///tmp/a', 'https://name:password@example.com/a'])
def test_manifest_rejects_insecure_sources(fake_native, url):
    project, home, data = fake_native
    data['architectures']['arm64']['artifacts']['7zip']['urls'][0]['url'] = url
    (project / 'macos-native-sources.json').write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(native.NativeSetupError):
        native.load_manifest(project, 'arm64')


def test_ready_environment_has_no_network_or_reextraction(fake_native, monkeypatch):
    project, home, _ = fake_native
    monkeypatch.setattr(native.urllib.request, 'build_opener', lambda *args: pytest.fail('offline cache must not use network'))
    state = native.ensure_native(project, home, offline=True, architecture='arm64')
    assert state['ready'] and not state['missing']
    assert set(state['components']) == set(native.COMPONENTS)
    assert Path(state['env']['QINGJIAN_LIBREOFFICE']).parts[-4:] == ('LibreOffice.app', 'Contents', 'MacOS', 'soffice')
    assert 'share' in state['env']['GS_LIB']
    monkeypatch.setattr(native, '_download', lambda *args: pytest.fail('ready must not download'))
    assert native.ensure_native(project, home, architecture='arm64') == state


def test_offline_missing_cache_explains_missing_component(fake_native):
    project, home, _ = fake_native
    (home / 'cache/macos-native/7zip.tar.gz').unlink()
    with pytest.raises(native.NativeSetupError, match='离线.*7zip'):
        native.ensure_native(project, home, offline=True, architecture='arm64')
    assert not (home / 'native/.prepare-macos.lock').exists()
    assert not list((home / 'native').glob('.staging-*'))


def test_detects_changed_component_and_repairs_only_it(fake_native, monkeypatch):
    project, home, _ = fake_native
    first = native.ensure_native(project, home, offline=True, architecture='arm64')
    root = Path(first['components']['7zip'])
    (root / '7zz').write_bytes(b'wrong')
    assert native.check_native(project, home, 'arm64')['missing'] == ['7zip']
    real_download = native._download
    seen = []
    def tracking(spec, *args, **kwargs):
        seen.append(spec['filename'])
        return real_download(spec, *args, **kwargs)
    monkeypatch.setattr(native, '_download', tracking)
    assert native.ensure_native(project, home, offline=True, architecture='arm64')['ready']
    assert seen == ['7zip.tar.gz']
    assert list((home / 'native').glob('.damaged-7zip-*'))


def test_packaged_roots_reuse_with_a_new_empty_shared_home(fake_native, monkeypatch):
    project, home, _ = fake_native
    state = native.ensure_native(project, home, offline=True, architecture='arm64')
    for key, root in state['components'].items():
        shutil.copytree(root, project / 'native' / key, symlinks=True)
    monkeypatch.setattr(native, '_download', lambda *args: pytest.fail('full package must be offline'))
    empty = home.parent / 'different-shared-home'
    result = native.ensure_native(project, empty, offline=True, architecture='arm64')
    assert result['ready']
    assert not empty.exists()
    assert all(Path(root).parent == project / 'native' for root in result['components'].values())


def test_environment_can_reuse_explicit_verified_external_roots(fake_native, monkeypatch):
    project, home, _ = fake_native
    state = native.ensure_native(project, home, offline=True, architecture='arm64')
    monkeypatch.setenv(native.ENVIRONMENT_KEY, json.dumps(state['components']))
    assert native.check_native(project, home.parent / 'other', 'arm64')['ready']
    assert not native.check_native(project, home, 'x86_64')['ready']


def test_renamed_archive_does_not_validate_as_component(fake_native):
    project, home, _ = fake_native
    state = native.ensure_native(project, home, offline=True, architecture='arm64')
    root = Path(state['components']['7zip'])
    receipt = json.loads((root / native.RECEIPT).read_text(encoding='utf-8'))
    receipt['files'][0]['file'] = '../../escape'
    (root / native.RECEIPT).write_text(json.dumps(receipt), encoding='utf-8')
    assert native.check_native(project, home, 'arm64')['missing'] == ['7zip']


def test_preparation_lock_is_not_silently_removed(fake_native):
    project, home, _ = fake_native
    lock = home / 'native/.prepare-macos.lock'
    lock.mkdir(parents=True)
    with pytest.raises(native.NativeSetupError, match='另一项'):
        native.ensure_native(project, home, architecture='arm64')
    assert lock.is_dir()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/drive', 'ok/../../escape', 'back\\slash'])
def test_tar_traversal_is_rejected(tmp_path, name):
    archive = tmp_path / 'bad.tar.gz'
    _tar(archive, {name: b'bad'})
    with pytest.raises(native.NativeSetupError, match='路径'):
        native._extract_tar(archive, tmp_path / 'out')
    assert not (tmp_path / 'escape').exists()


def test_tar_external_symlink_is_rejected(tmp_path):
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        row = tarfile.TarInfo('escape')
        row.type, row.linkname = tarfile.SYMTYPE, '../../outside'
        bundle.addfile(row)
    with pytest.raises(native.NativeSetupError, match='链接越界'):
        native._extract_tar(archive, tmp_path / 'out')


def test_tar_file_through_delayed_symlink_is_rejected(tmp_path):
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        link = tarfile.TarInfo('folder')
        link.type, link.linkname = tarfile.SYMTYPE, 'other'
        bundle.addfile(link)
        row = tarfile.TarInfo('folder/content')
        row.size = 3
        bundle.addfile(row, io.BytesIO(b'bad'))
    with pytest.raises(native.NativeSetupError, match='链接路径冲突'):
        native._extract_tar(archive, tmp_path / 'out')


def test_tar_special_device_and_hardlink_rejected(tmp_path):
    for kind in (tarfile.CHRTYPE, tarfile.LNKTYPE):
        archive = tmp_path / ('bad-' + kind.decode() + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as bundle:
            row = tarfile.TarInfo('special')
            row.type, row.linkname = kind, 'target'
            bundle.addfile(row)
        with pytest.raises(native.NativeSetupError, match='特殊文件'):
            native._extract_tar(archive, tmp_path / ('out-' + kind.decode()))


def _cpio(name, data, mode=stat.S_IFREG | 0o644):
    encoded = name.encode() + b'\0'
    header = ('070707' + f'{0:06o}{0:06o}{mode:06o}{0:06o}{0:06o}{1:06o}{0:06o}{0:011o}{len(encoded):06o}{len(data):011o}').encode()
    assert len(header) == 76
    return header + encoded + data


def _pkg(path, entries):
    payload = gzip.compress(b''.join(_cpio(name, data, mode) for name, data, mode in entries) + _cpio('TRAILER!!!', b''))
    toc = ('<xar><toc><file><name>Ghostscript-10.07.0-Start.pkg</name><file><name>Payload</name><data>'
           f'<offset>0</offset><length>{len(payload)}</length></data></file></file></toc></xar>').encode()
    compressed = zlib.compress(toc)
    path.write_bytes(struct.pack('>IHHQQI', 0x78617221, 28, 1, len(compressed), len(toc), 1) + compressed + payload)


def test_ghostscript_extracts_only_non_x11_and_resources_never_scripts(tmp_path):
    pkg, out = tmp_path / 'gs.pkg', tmp_path / 'out'
    _pkg(pkg, [('./usr/local/bin/gs-noX11', b'non-X11', stat.S_IFREG | 0o755),
               ('./usr/local/bin/gs-X11', b'X11', stat.S_IFREG | 0o755),
               ('./usr/local/share/ghostscript/10.07.0/Resource/Init/gs_init.ps', b'init', stat.S_IFREG | 0o644),
               ('./postinstall', b'dangerous script', stat.S_IFREG | 0o755)])
    native._extract_ghostscript(pkg, out, {'package_directory': 'Ghostscript-10.07.0-Start.pkg'})
    assert (out / 'bin/gs').read_bytes() == b'non-X11'
    assert (out / 'share/ghostscript/10.07.0/Resource/Init/gs_init.ps').read_bytes() == b'init'
    assert not (out / 'postinstall').exists()
    assert not (out / 'bin/gs-X11').exists()


def test_ghostscript_rejects_cpio_traversal(tmp_path):
    pkg = tmp_path / 'gs.pkg'
    _pkg(pkg, [('../../escape', b'bad', stat.S_IFREG | 0o644)])
    with pytest.raises(native.NativeSetupError, match='路径'):
        native._extract_ghostscript(pkg, tmp_path / 'out', {'package_directory': 'Ghostscript-10.07.0-Start.pkg'})


class _Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url
        self.headers = {'Content-Length': str(len(data))}


def test_download_falls_back_and_verifies_before_caching(tmp_path, monkeypatch):
    spec = dict(filename='component.tar.gz', bytes=4, sha256=hashlib.sha256(b'good').hexdigest(),
                urls=[{'name': 'first', 'url': 'https://first.example/a'}, {'name': 'second', 'url': 'https://second.example/a'}])
    calls = []
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            return _Response(b'bad!' if len(calls) == 1 else b'good', request.full_url)
    monkeypatch.setattr(native.urllib.request, 'build_opener', lambda *args: Opener())
    output = native._download(spec, tmp_path)
    assert calls == [row['url'] for row in spec['urls']]
    assert output.read_bytes() == b'good'
    assert not list(tmp_path.glob('.download-*'))


def test_all_download_sources_fail_without_a_false_success(tmp_path, monkeypatch):
    spec = dict(filename='a', bytes=4, sha256=hashlib.sha256(b'good').hexdigest(),
                urls=[{'name': 'first', 'url': 'https://a.example/a'}, {'name': 'second', 'url': 'https://b.example/a'}])
    class Opener:
        def open(self, request, **kwargs):
            raise OSError('unavailable')
    monkeypatch.setattr(native.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(native.NativeSetupError, match=r'first.*unavailable[\s\S]*second.*unavailable'):
        native._download(spec, tmp_path)
    assert not (tmp_path / 'a').exists()
    assert not list(tmp_path.glob('.download-*'))


def test_redirect_cannot_downgrade_to_http():
    with pytest.raises(native.NativeSetupError, match='HTTPS'):
        native._HTTPSRedirect().redirect_request(None, None, 302, 'redirect', {}, 'http://example.com/a')


def test_dmg_is_not_claimed_extractable_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(native.platform, 'system', lambda: 'Windows')
    with pytest.raises(native.NativeSetupError, match='需要在 Mac'):
        native._extract_dmg(tmp_path / 'a.dmg', tmp_path / 'out')


def test_dmg_mounts_read_only_copies_privately_then_detaches(tmp_path, monkeypatch):
    monkeypatch.setattr(native.platform, 'system', lambda: 'Darwin')
    destination = tmp_path / 'stage'
    destination.mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'attach':
            mount = Path(command[command.index('-mountpoint') + 1])
            app = mount / 'LibreOffice.app/Contents/MacOS'
            app.mkdir(parents=True)
            (app / 'soffice').write_bytes(b'verified-Mac-binary')
            return subprocess.CompletedProcess(command, 0, plistlib.dumps({'system-entities': [{'mount-point': str(mount)}]}), b'')
        shutil.rmtree(Path(command[-1]) / 'LibreOffice.app')
        return subprocess.CompletedProcess(command, 0, b'', b'')
    monkeypatch.setattr(native.subprocess, 'run', run)
    native._extract_dmg(tmp_path / 'verified.dmg', destination)
    assert (destination / 'LibreOffice.app/Contents/MacOS/soffice').read_bytes() == b'verified-Mac-binary'
    assert '-readonly' in calls[0] and '-nobrowse' in calls[0]
    assert calls[1][:2] == ['/usr/bin/hdiutil', 'detach']
    assert not list(tmp_path.glob('.mount-*'))


def test_dmg_mount_failure_is_explained_and_cleans_mount_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(native.platform, 'system', lambda: 'Darwin')
    destination = tmp_path / 'stage'
    destination.mkdir()
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(native.subprocess, 'run', fail)
    with pytest.raises(native.NativeSetupError, match='LibreOffice 私有解压失败'):
        native._extract_dmg(tmp_path / 'verified.dmg', destination)
    assert not list(tmp_path.glob('.mount-*'))


def test_full_distribution_requires_explicit_source_preparation(fake_native):
    project, home, data = fake_native
    cache = home / 'cache/macos-native'
    source = cache / 'gs-source.tar.xz'
    source.write_bytes(b'original corresponding source')
    metadata = _row(source.name, source.read_bytes())
    metadata['filename'] = metadata.pop('file')
    metadata.update(component='Ghostscript', destination='source/' + source.name,
                    urls=[{'name': 'official', 'url': 'https://source.example/a'},
                          {'name': 'backup', 'url': 'https://backup.example/a'}])
    data['redistribution_artifacts'] = {'ghostscript-source': metadata}
    (project / 'macos-native-sources.json').write_text(json.dumps(data), encoding='utf-8')
    state = native.ensure_native(project, home, offline=True, architecture='arm64')
    root = Path(state['components']['Ghostscript'])
    assert not (root / metadata['destination']).exists()
    assert native.check_redistribution_materials(project, home, 'arm64') == {'ready': False, 'missing': ['ghostscript-source']}
    assert native.ensure_redistribution_materials(project, home, offline=True, architecture='arm64')['ready']
    assert (root / metadata['destination']).read_bytes() == source.read_bytes()
    assert native.check_native(project, home, 'arm64')['ready']
    assert not list(root.glob('.receipt-*'))


def test_python_native_license_material_matches_pinned_runtime():
    project = Path(__file__).resolve().parents[1]
    root = project / 'support/macos/licenses/python'
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    runtime = json.loads((project / 'macos-runtime-sources.json').read_text(encoding='utf-8'))
    assert manifest['python_version'] == '3.13.18'
    assert manifest['build_release'] == '20261026'
    assert manifest['upstream_commit'] == 'fa506e0858ac9f2cc5b289c137b888a2aa24d1ee'
    for arch in ('arm64', 'x86_64'):
        for field in ('filename', 'bytes', 'sha256'):
            assert manifest['runtimes'][arch][field] == runtime['python'][arch][field]
    files = manifest['files']
    assert set(row['file'] for row in files) == {p.name for p in root.iterdir()} - {'manifest.json'}
    for row in files:
        assert native._verified(root / row['file'], row), row['file']
        if row['kind'] == 'upstream-license':
            assert '/' + manifest['upstream_commit'] + '/' in row['source_url']
            assert row['source_url'].startswith('https://raw.githubusercontent.com/astral-sh/python-build-standalone/')


def test_python_native_license_material_covers_official_metadata_both_architectures():
    root = Path(__file__).resolve().parents[1] / 'support/macos/licenses/python'
    manifests = []
    for arch in ('arm64', 'x86_64'):
        data = json.loads((root / ('runtime-metadata-' + arch + '.json')).read_text(encoding='utf-8'))
        assert data['python_version'] == '3.13.18'
        paths = {data['license_path']}
        for variants in data['extensions'].values():
            for variant in variants:
                paths.update(variant.get('license_paths', []))
        assert len(paths) == 14
        assert all((root / Path(path).name).is_file() for path in paths)
        assert 'licenses/LICENSE.openssl-3.txt' in paths
        assert 'licenses/LICENSE.libffi.txt' in paths
        assert 'licenses/LICENSE.bdb.txt' not in paths
        manifests.append(paths)
    assert manifests[0] == manifests[1]
