"""Only the GUI session confirmed by source acceptance may be stopped."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


def coordinator():
    path = Path(__file__).resolve().parents[1] / 'macos_build/ci_build.py'
    spec = importlib.util.spec_from_file_location('mac_ci_launcher_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_focused_regression_creates_parent_for_pytest_basetemp(tmp_path):
    module = coordinator()
    tested=[]
    def run(arguments, **kwargs):
        if '--basetemp' in arguments:
            target = Path(arguments[arguments.index('--basetemp') + 1])
            assert target.parent.is_dir()
            tested.append(target.name)
            assert 'check_native' in arguments[arguments.index('-c')+1]
            assert str(tmp_path/'home') in arguments
        return Mock(returncode=0)
    with patch.object(module.subprocess, 'run', side_effect=run):
        module.focused_regression(tmp_path / 'python', tmp_path / 'home', tmp_path / 'output')
    assert tuple(tested)==module.REGRESSION_MODULES
    assert {'test_converter_float_ui', 'test_converter_dialogs_local',
            'test_converter_office_integration', 'test_office_conversion_local',
            'test_converter_office_catalog', 'test_format_office'} <= set(tested)


def test_source_payload_acceptance_rejects_stale_conversion_code(tmp_path):
    module=coordinator()
    project=tmp_path/'source'
    bundle=tmp_path/'bundle'
    relative=Path('qingjian/converter_ui.py')
    source=project/relative
    target=bundle/'程序文件'/relative
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_text('PAGE_SIZE = 6\n',encoding='utf-8')
    target.write_bytes(source.read_bytes())
    assert module.source_payload_acceptance(project,bundle)['passed']
    target.write_text('PAGE_SIZE = 8\n',encoding='utf-8')
    with pytest.raises(RuntimeError,match='converter_ui.py'):
        module.source_payload_acceptance(project,bundle)


def test_focused_regression_uses_canonical_temp_directory_only_in_children(tmp_path,monkeypatch):
    module=coordinator()
    # A path with an alias-like ".." component has a real, portable canonical
    # directory without requiring symlink privileges on the Windows rehearsal.
    alias=tmp_path/'alias'
    alias.mkdir()
    output=alias/'..'/'actual-output'
    expected=(tmp_path/'actual-output/tests-temp').resolve()
    keys=('TMPDIR','TEMP','TMP','QINGJIAN_TEST_TMPDIR')
    for key in keys:
        monkeypatch.setenv(key,'caller-value-'+key)
    calls=[]
    def run(arguments,**kwargs):
        values=kwargs['env']
        calls.append(values)
        assert expected.is_dir()
        assert all(Path(values[key])==expected for key in keys)
        assert all(Path(values[key]).resolve()==Path(values[key]) for key in keys)
        base=Path(arguments[arguments.index('--basetemp')+1])
        assert base.parent.samefile(expected)
        return Mock(returncode=0)
    with patch.object(module,'execute'),patch.object(module.subprocess,'run',side_effect=run):
        module.focused_regression(tmp_path/'python',tmp_path/'home',output)
    assert len(calls)==len(module.REGRESSION_MODULES)
    assert all(module.os.environ[key]=='caller-value-'+key for key in keys)


@pytest.mark.parametrize('failure', ['', 'exit', 'missing', 'other-group'])
def test_source_launch_cleanup_requires_ready_and_own_session(tmp_path, failure):
    module = coordinator()
    stdout = json.dumps({'event': 'launched', 'pid': 12345}) if failure != 'missing' else 'no ready marker'
    done = Mock(returncode=2 if failure == 'exit' else 0, stdout=stdout, stderr='')
    with patch.object(module.subprocess, 'run', return_value=done), \
         patch.object(module.platform, 'machine', return_value='arm64'), \
         patch.object(module.os, 'getpgid', create=True, return_value=222 if failure == 'other-group' else 12345), \
         patch.object(module.os, 'killpg', create=True) as stop:
        if failure:
            with pytest.raises(RuntimeError):
                module.source_launcher_acceptance(tmp_path / 'home', tmp_path, {})
            stop.assert_not_called()
        else:
            module.source_launcher_acceptance(tmp_path / 'home', tmp_path, {})
            stop.assert_called_once_with(12345, module.signal.SIGTERM)
            report = json.loads((tmp_path / 'acceptance/arm64/source-ui/源码启动器.json').read_text(encoding='utf-8'))
            assert report['passed'] and report['detached_gui'] and report['offline']
