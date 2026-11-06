"""Focused source/bootstrap behavior; actual macOS launch is a separate check."""
import io
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
from unittest.mock import Mock, patch

import pytest

import bootstrap_macos as mac


def archive(tmp_path, members):
    path = tmp_path / 'python.tar.gz'
    with tarfile.open(path, 'w:gz') as tar:
        for name, body, kind in members:
            entry = tarfile.TarInfo(name)
            if kind == 'file':
                data = body.encode()
                entry.size = len(data)
                entry.mode = 0o755
                tar.addfile(entry, io.BytesIO(data))
            elif kind == 'symlink':
                entry.type = tarfile.SYMTYPE
                entry.linkname = body
                tar.addfile(entry)
            elif kind == 'fifo':
                entry.type = tarfile.FIFOTYPE
                tar.addfile(entry)
    return path


@pytest.mark.parametrize('name,body,kind', [
    ('/python/evil', '', 'file'), ('python/../../evil', '', 'file'),
    ('other/evil', '', 'file'), ('python/link', '/etc/passwd', 'symlink'),
    ('python/link', '../../escape', 'symlink'), ('python/pipe', '', 'fifo'),
])
def test_archive_rejects_escape_and_special(tmp_path, name, body, kind):
    path = archive(tmp_path, [(name, body, kind)])
    with pytest.raises(mac.BootstrapError):
        mac.safe_extract_python(path, tmp_path / 'stage')
    assert not (tmp_path / 'evil').exists()


def test_archive_regular_executable(tmp_path):
    path = archive(tmp_path, [('python/bin/python3', 'verified binary placeholder', 'file')])
    root = mac.safe_extract_python(path, tmp_path / 'stage')
    assert (root / 'bin/python3').read_text() == 'verified binary placeholder'


def test_archive_does_not_write_through_link(tmp_path):
    path = archive(tmp_path, [('python/lib', 'elsewhere', 'symlink'), ('python/lib/file', 'x', 'file')])
    with pytest.raises((OSError, mac.BootstrapError)):
        mac.safe_extract_python(path, tmp_path / 'stage')


def test_manifest_fixed_and_https():
    row = mac.load_manifest()
    assert set(row['python']) == {'arm64', 'x86_64'}
    for item in row['python'].values():
        assert len(item['sha256']) == 64
        assert item['bytes'] > 20_000_000
        assert any('nju.edu.cn' in url['url'] for url in item['urls'])


def test_selection_cancel_has_no_side_effect(tmp_path):
    args = mac.build_parser().parse_args(['--choose-environment'])
    with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(mac, 'default_home', return_value=tmp_path / 'absent'):
        assert mac.choose_selection(tmp_path, args, read=lambda _: 'q', output=lambda _: None) is None
    assert not (tmp_path / 'absent').exists()


def test_explicit_home_needs_no_prompt_or_creation(tmp_path):
    home = tmp_path / 'not-created'
    args = mac.build_parser().parse_args(['--home', str(home), '--managed-python'])
    row = mac.choose_selection(tmp_path, args, read=Mock(side_effect=AssertionError('prompted')))
    assert row['python'] == home / 'python' / mac.architecture() / 'bin/python3'
    assert not home.exists()


def test_saved_selection_survives_source_replacement(tmp_path, monkeypatch):
    record = tmp_path / 'user' / 'index.json'
    monkeypatch.setenv('QINGJIAN_MAC_SELECTION', str(record))
    home = tmp_path / 'external-components'
    python = tmp_path / 'shared-python'
    mac.remember({'mode': 'reuse', 'runtime_home': home, 'python': python})
    assert mac.saved_selection()['runtime_home'] == str(home)
    assert mac.saved_selection()['python'] == str(python)
    assert not home.exists()


def test_ready_environment_returns_without_preparation(tmp_path, monkeypatch):
    monkeypatch.setenv('QINGJIAN_BOOTSTRAP_READY', 'correct-stamp')
    with patch.object(mac, 'environment_stamp', return_value='correct-stamp'), patch.object(mac, 'prepare_environment') as prepare:
        assert mac.start_source(tmp_path)
    prepare.assert_not_called()


def test_pep668_missing_does_not_install(tmp_path):
    probe = {'supported': True, 'ready': False, 'missing': ['Pillow'], 'identity': {'externally_managed': True}}
    with patch.object(mac, 'checked_python', return_value=probe), patch.object(mac.common, 'install_missing_packages') as installer:
        with pytest.raises(mac.BootstrapError, match='维护'):
            mac.prepare_environment(mac.PROJECT, Path(sys.executable), tmp_path)
    installer.assert_not_called()


def test_offline_missing_does_not_install(tmp_path):
    probe = {'supported': True, 'ready': False, 'missing': ['Pillow'], 'identity': {}}
    with patch.object(mac, 'checked_python', return_value=probe), patch.object(mac.common, 'install_missing_packages') as installer:
        with pytest.raises(mac.BootstrapError, match='离线'):
            mac.prepare_environment(mac.PROJECT, Path(sys.executable), tmp_path, offline=True)
    installer.assert_not_called()


def test_complete_python_and_native_have_no_network(tmp_path):
    native = {'ready': True, 'components': {}, 'env': {}}
    with patch.object(mac, 'checked_python', return_value={'supported': True, 'ready': True}), \
         patch('startup_macos_native.ensure_native', return_value=native) as engines, \
         patch.object(mac.common, 'install_missing_packages') as installer, \
         patch.object(mac.common, 'download_artifact', side_effect=AssertionError('network')):
        result = mac.prepare_environment(mac.PROJECT, Path(sys.executable), tmp_path)
    assert result['ready']
    installer.assert_not_called()
    engines.assert_called_once()


def test_gui_detaches_from_terminal(tmp_path):
    child = Mock(pid=12345)
    child.poll.return_value = None
    marker = {'schema_version': 1, 'pid': 12345, 'platform': 'darwin', 'python': sys.executable}
    with patch.object(mac, 'environment_stamp', return_value='stamp'), patch.object(mac, 'read_json', return_value=marker), \
         patch.object(mac.subprocess, 'Popen', return_value=child) as launch:
        assert mac.start_detached(tmp_path, Path(sys.executable), tmp_path, {}, [], emit=lambda _: None) == 0
    assert launch.call_args.kwargs['start_new_session'] is True
    assert launch.call_args.kwargs['stdin'] is mac.subprocess.DEVNULL
    assert launch.call_args.kwargs['stderr'] is mac.subprocess.STDOUT


def test_unsupported_platform_cannot_prepare():
    with patch.object(sys, 'platform', 'win32'), patch.object(mac, 'prepare_environment') as prepare:
        assert mac.main(['--home', str(mac.PROJECT), '--managed-python', '--no-ui']) == 2
    prepare.assert_not_called()


def test_native_error_is_readable_and_retryable(tmp_path):
    from startup_macos_native import NativeSetupError
    with patch.object(mac, 'checked_python', return_value={'supported': True, 'ready': True}), \
         patch('startup_macos_native.ensure_native', side_effect=NativeSetupError('所有镜像不可用')):
        with pytest.raises(mac.BootstrapError, match='所有镜像不可用'):
            mac.prepare_environment(mac.PROJECT, Path(sys.executable), tmp_path)


def test_first_ready_direct_source_remembers_selection(tmp_path, monkeypatch):
    monkeypatch.delenv('QINGJIAN_BOOTSTRAP_READY', raising=False)
    args = mac.build_parser().parse_args(['--record-selection'])
    selection = {'mode': 'reuse', 'python': Path(sys.executable), 'runtime_home': tmp_path}
    checked = {'ready': True, 'python': sys.executable, 'runtime_home': str(tmp_path), 'native': {}}
    with patch.object(mac, 'environment_stamp', return_value='stamp'), \
         patch.object(mac, 'offline_package', return_value=False), \
         patch.object(mac, 'build_parser') as parser, patch.object(mac, 'choose_selection', return_value=selection), \
         patch.object(mac, 'check_environment', return_value=checked), patch.object(mac, 'ready_environment', return_value={}), \
         patch.object(mac, 'remember') as remember:
        parser.return_value.parse_args.return_value = args
        assert mac.start_source(tmp_path)
    remember.assert_called_once()


def test_offline_full_selection_ignores_source_catalog(tmp_path, monkeypatch):
    mac.atomic_json(tmp_path / 'macos-package.json', {'schema_version': 1, 'mode': 'offline'})
    monkeypatch.setenv('QINGJIAN_USER_DATA', str(tmp_path / 'fake-user-data'))
    args = mac.build_parser().parse_args(['--check-environment', '--offline'])
    with patch.object(mac, 'saved_selection', side_effect=AssertionError('source catalog accessed')):
        selected = mac.choose_selection(tmp_path, args, read=Mock(side_effect=AssertionError('prompted')))
    assert selected['python'] == tmp_path / 'Python/bin/python3'
    assert selected['runtime_home'] == tmp_path / 'fake-user-data/offline-runtime'
    assert not selected['runtime_home'].exists()


def test_native_progress_uses_console_event_contract(tmp_path):
    native = {'ready': True, 'components': {}, 'env': {}}
    events = []
    def prepare_native(project, home, emit, **kwargs):
        emit('正在补齐原生组件')
        return native
    with patch.object(mac, 'checked_python', return_value={'supported': True, 'ready': True}), \
         patch('startup_macos_native.ensure_native', side_effect=prepare_native):
        result = mac.prepare_environment(mac.PROJECT, Path(sys.executable), tmp_path, emit=events.append)
    assert result['ready']
    assert {'event': 'progress', 'message': '正在补齐原生组件'} in events


@pytest.mark.parametrize('operation', ['--check-environment', '--prepare-environment'])
def test_main_keeps_mac_offline_and_selected_home(operation, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('qingjian_entry_test', mac.PROJECT / 'main.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    arguments = [operation, '--offline', '--home', str(tmp_path), '--python', sys.executable]
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(sys, 'argv', ['main.py', *arguments])
    with patch.object(mac, 'main', return_value=0) as prepare:
        assert entry.run() == 0
    prepare.assert_called_once_with(arguments)
