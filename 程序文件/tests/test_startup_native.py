"""Native source bootstrap must remain light, pinned, reusable and recoverable."""
import copy
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
import stat
import sys
from unittest.mock import patch
import zipfile

import pytest

import bootstrap
import startup_native as native
from package_source import source_files


def row(name, data=b'checked bytes', **extra):
    return {'file': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), **extra}


def write_rows(root, rows, data=b'checked bytes'):
    for item in rows:
        path = root / item['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


@pytest.fixture
def small(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    manifest = {'schema_version': 1, 'platform': 'windows-x64', 'artifacts': {}, 'components': {}}
    for key, kind in [('7zip', 'msi'), ('Ghostscript', 'nsis'), ('OFD', 'ofd')]:
        item = row(key + '.zip')
        item['filename'] = item.pop('file')
        item['urls'] = [{'name': 'official', 'url': 'https://official.example/' + key},
                        {'name': 'mirror', 'url': 'https://mirror.example/' + key}]
        manifest['artifacts'][key] = item
        component = {'kind': kind, 'version': '1', 'artifact': key,
                     'files': [row('bridge.jar' if key == 'OFD' else 'engine.exe')]}
        if key == 'OFD':
            component['runtime_variants'] = {'jlink': [row('runtime/bin/java.exe')],
                                             'jre': [row('runtime/bin/java.exe')]}
        manifest['components'][key] = component
    (source / 'native-sources.json').write_text(json.dumps(manifest), encoding='utf-8')
    return source, tmp_path / 'chosen-D-drive', manifest


def test_manifest_pins_all_components_and_all_37_libraries():
    manifest = native.load_manifest(bootstrap.PROJECT)
    assert len(manifest['artifacts']) == 40
    assert set(manifest['components']) == set(native.COMPONENTS)
    assert len([r for r in manifest['components']['OFD']['files'] if r.get('artifact')]) == 37
    assert 'jre_' in manifest['artifacts']['java']['filename']
    assert not any('maven.zip' in r['filename'] or '-jdk_' in r['filename'] for r in manifest['artifacts'].values())
    assert 'nju.edu.cn' in manifest['artifacts']['7zip']['urls'][1]['url']
    assert 'maven.aliyun.com' in manifest['artifacts']['ofdrw-core-2.5.1.jar']['urls'][1]['url']


@pytest.mark.parametrize('change', ['http', 'sha', 'duplicate', 'escape', 'missing'])
def test_manifest_rejects_unsafe_and_incomplete_download_contract(small, change):
    source, home, manifest = small
    if change == 'http':
        manifest['artifacts']['7zip']['urls'][0]['url'] = 'http://example.test/insecure'
    elif change == 'sha':
        manifest['artifacts']['7zip']['sha256'] = ''
    elif change == 'duplicate':
        manifest['components']['7zip']['files'] *= 2
    elif change == 'escape':
        manifest['components']['7zip']['files'][0]['file'] = '../outside.exe'
    else:
        del manifest['components']['Ghostscript']
    (source / 'native-sources.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises((native.NativeSetupError, bootstrap.BootstrapError)):
        native.load_manifest(source)
    assert not home.exists()


def test_ready_native_reuses_validated_components_without_network_or_new_dirs(small, monkeypatch):
    source, home, manifest = small
    home = source / 'runtime'  # An existing full package keeps its bundled components.
    for key, comp in manifest['components'].items():
        write_rows(source / 'support' / key, comp['files'])
        if key == 'OFD':
            write_rows(source / 'support' / key, comp['runtime_variants']['jlink'])
    monkeypatch.setattr(native, '_download', lambda *a, **k: pytest.fail('unexpected network'))
    roots = native.ensure_native(source, home, offline=True)
    assert set(roots) == set(native.COMPONENTS)
    assert not home.exists()
    (source / 'support/OFD/bridge.jar').write_bytes(b'changed bytes')
    checked = native.check_native(source, home)
    assert checked['native_missing'] == ['OFD']


def test_reuses_verified_neighbor_from_three_entry_layout(small):
    source, home, manifest = small
    old = source.parent / '青简工具箱_v0.9.0/程序文件/support/7zip'
    write_rows(old, manifest['components']['7zip']['files'])
    checked = native.check_native(source, home)
    assert checked['native_components']['7zip'] == str(old.resolve())
    assert set(checked['native_missing']) == {'Ghostscript', 'OFD'}


def test_preserves_all_old_package_components_before_source_deletion(small, monkeypatch):
    source, home, manifest = small
    for key, component in manifest['components'].items():
        write_rows(source / 'support' / key, component['files'])
        if key == 'OFD':
            write_rows(source / 'support' / key, component['runtime_variants']['jlink'])
    monkeypatch.setattr(native, '_download', lambda *a, **k: pytest.fail('complete old components must not download'))
    probed = []
    monkeypatch.setattr(native, '_probe', lambda key, *a: probed.append(key))
    roots = native.ensure_native(source, home, offline=True)
    assert set(probed) == set(native.COMPONENTS)
    for key, root in roots.items():
        assert Path(root).is_relative_to(home)
        assert native._complete(Path(root), manifest['components'][key])
    assert source.resolve().is_relative_to(home.parent.resolve())
    shutil.rmtree(source)
    source.mkdir()
    (source / 'native-sources.json').write_text(json.dumps(manifest), encoding='utf-8')
    assert native.ensure_native(source, home, offline=True) == roots
    assert len(probed) == 3


def test_old_native_signature_is_reused_only_when_current_hashes_match(small):
    source, home, manifest = small
    old = home / 'native/7zip-0000000000000000'
    write_rows(old, manifest['components']['7zip']['files'])
    assert native.check_native(source, home)['native_components']['7zip'] == str(old.resolve())
    (old / 'engine.exe').write_bytes(b'invalid bytes')
    assert '7zip' in native.check_native(source, home)['native_missing']


def test_verified_existing_native_directory_survives_one_damaged_component(small, monkeypatch):
    source, home, manifest = small
    for key, comp in manifest['components'].items():
        write_rows(source / 'support' / key, comp['files'])
        if key == 'OFD':
            write_rows(source / 'support' / key, comp['runtime_variants']['jlink'])
    (source / 'support/7zip/engine.exe').unlink()
    requests = []
    monkeypatch.setattr(native, '_download', lambda m, key, *a: requests.append(key) or Path('verified.msi'))
    monkeypatch.setattr(native, '_extract_seven', lambda a, b, dst, rows, e: write_rows(dst, rows))
    monkeypatch.setattr(native, '_probe', lambda *a: None)
    roots = native.ensure_native(source, home)
    assert requests == ['7zip']
    assert Path(roots['7zip']).is_relative_to(home / 'native')
    assert not list((home / 'native').glob('.native-stage-*'))
    assert native.check_native(source, home)['native_ready']
    requests.clear()
    native.ensure_native(source, home, offline=True)
    assert not requests


def test_failure_preserves_unknown_target_and_never_publishes_partial_native(small, monkeypatch):
    source, home, manifest = small
    target = native._target(home, '7zip', manifest['components']['7zip'])
    target.mkdir(parents=True)
    preserved = target / 'my-file.txt'
    preserved.write_text('user data')
    monkeypatch.setattr(native, '_download', lambda *a: Path('verified.msi'))
    monkeypatch.setattr(native, '_extract_seven', lambda a, b, dst, rows, e: write_rows(dst, rows))
    monkeypatch.setattr(native, '_probe', lambda *a: None)
    with pytest.raises(native.NativeSetupError, match='不属于本工具'):
        native.ensure_native(source, home)
    assert preserved.read_text() == 'user data'
    assert not (target / 'engine.exe').exists()
    assert not list((home / 'native').glob('.native-stage-*'))


def test_failed_native_probe_preserves_download_cache_for_retry(small, monkeypatch):
    source, home, manifest = small
    cached = home / 'cache/artifact.zip'
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b'cached')
    monkeypatch.setattr(native, '_download', lambda *a: cached)
    monkeypatch.setattr(native, '_extract_seven', lambda a, b, dst, rows, e: write_rows(dst, rows))
    monkeypatch.setattr(native, '_probe', lambda *a: (_ for _ in ()).throw(native.NativeSetupError('probe failed')))
    with pytest.raises(native.NativeSetupError, match='probe failed'):
        native.ensure_native(source, home)
    assert cached.read_bytes() == b'cached'
    assert not native._target(home, '7zip', manifest['components']['7zip']).exists()
    assert not list((home / 'native').glob('.native-stage-*'))


@pytest.mark.parametrize('attack', ['traversal', 'symlink', 'wrong_hash', 'duplicate'])
def test_jre_zip_extraction_is_pinned_and_path_safe(tmp_path, attack):
    archive_path = tmp_path / 'jre.zip'
    item = row('runtime/bin/java.exe', archive_key='jre/bin/java.exe')
    with zipfile.ZipFile(archive_path, 'w') as archive:
        info = zipfile.ZipInfo(item['archive_key'])
        if attack == 'symlink':
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b'checked bytes')
        if attack == 'duplicate':
            archive.writestr(info, b'checked bytes')
    if attack == 'traversal':
        item['file'] = '../escaped.exe'
    if attack == 'wrong_hash':
        item['sha256'] = '0' * 64
    with pytest.raises((native.NativeSetupError, native.EngineSetupError)):
        native._zip_files(archive_path, tmp_path / 'target', [item])
    assert not (tmp_path / 'escaped.exe').exists()


def test_light_source_keeps_bridge_notices_and_bootstrap_but_no_native_runtimes():
    paths = {p.relative_to(bootstrap.PROJECT).as_posix() for p in source_files(bootstrap.PROJECT)}
    assert {'native-sources.json', 'startup_native.py', 'support/OFD/bridge.jar'} <= paths
    assert 'support/OFD/source/pom.xml' in paths
    assert any(p.startswith('support/Ghostscript/licenses/') for p in paths)
    assert not any('/sources/' in p or p.startswith(('support/OFD/runtime/', 'support/OFD/lib/',
                   'support/Ghostscript/bin/', 'support/Ghostscript/Resource/')) for p in paths)
    assert 'support/7zip/7z.exe' not in paths and 'support/7zip/7z.dll' not in paths
    assert sum((bootstrap.PROJECT / p).stat().st_size for p in paths) < 8 * 1024 * 1024


def test_runtime_mapping_uses_prepared_components_and_frozen_uses_bundled(tmp_path, monkeypatch):
    from qingjian.core import format_media
    configured = tmp_path / 'chosen-drive/OFD'
    bundled = tmp_path / 'program/support'
    for path in (configured / 'bridge.jar', bundled / 'OFD/bridge.jar'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
    monkeypatch.setenv(native.ENVIRONMENT_KEY, json.dumps({'OFD': str(configured)}))
    monkeypatch.setattr(format_media, 'support_root', lambda: bundled)
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    assert format_media.engine('OFD/bridge.jar') == configured / 'bridge.jar'
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert format_media.engine('OFD/bridge.jar') == bundled / 'OFD/bridge.jar'


def test_bootstrap_reports_native_missing_as_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, 'probe_python', lambda *a: {'ready': True, 'supported': True})
    monkeypatch.setattr('startup_engine.find_engine', lambda *a: tmp_path / 'lo')
    monkeypatch.setattr(native, 'check_native', lambda *a: {'native_ready': False, 'native_missing': ['OFD'], 'native_components': {}})
    checked = bootstrap.check_environment(home=tmp_path)
    assert checked['engine_ready'] and not checked['ready']
    assert checked['native_missing'] == ['OFD']


def test_native_cleanup_rejects_foreign_directories(tmp_path):
    path = tmp_path / '.native-stage-test'
    path.mkdir()
    (path / native._OWNER).write_text(json.dumps({'kind': 'stage', 'token': 'foreign'}))
    with pytest.raises(native.NativeSetupError):
        native._cleanup(path, tmp_path, 'ours')
    assert path.is_dir()


class DownloadResponse(io.BytesIO):
    def __init__(self, data, url='https://mirror.example/artifact'):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


def test_native_download_falls_back_after_bad_official_bytes_and_reuses_cache(small):
    source, home, manifest = small
    home.mkdir()
    opened = []
    def get(request, **kwargs):
        opened.append(request.full_url)
        assert kwargs.get('context') is not None
        return DownloadResponse(b'wrong' if len(opened) == 1 else b'checked bytes')
    with patch('urllib.request.urlopen', side_effect=get):
        downloaded = native._download(manifest, '7zip', home, lambda e: None, False)
    assert len(opened) == 2 and 'mirror.example' in opened[-1]
    assert downloaded.is_relative_to(home / 'cache')
    with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
        assert native._download(manifest, '7zip', home, lambda e: None, True) == downloaded
    assert not list((home / 'cache').glob('*.part'))


def test_all_native_download_sources_failing_remain_actionable_and_do_not_publish(small):
    source, home, manifest = small
    home.mkdir()
    with patch('urllib.request.urlopen', side_effect=OSError('offline')):
        with pytest.raises(native.NativeSetupError, match='检查网络后重新启动'):
            native._download(manifest, '7zip', home, lambda e: None, False)
    assert not list((home / 'cache').iterdir())
    assert not (home / 'native').exists()


def test_native_download_rejects_non_https_redirect(small):
    source, home, manifest = small
    home.mkdir()
    with patch('urllib.request.urlopen', side_effect=lambda *a, **k: DownloadResponse(b'checked bytes', 'http://unsafe.example/file')):
        with pytest.raises(native.NativeSetupError):
            native._download(manifest, '7zip', home, lambda e: None, False)
    assert not list((home / 'cache').iterdir())


def test_ghostscript_vc_dlls_are_pinned_to_the_reviewed_jre_archive():
    manifest = native.load_manifest(bootstrap.PROJECT)
    expected = {r['sha256'] for r in manifest['components']['OFD']['runtime_variants']['jre']}
    crt = [r for r in manifest['components']['Ghostscript']['files'] if r.get('artifact') == 'java']
    assert len(crt) == 3
    assert all(r['sha256'] in expected and '/bin/' in r['archive_key'] for r in crt)
