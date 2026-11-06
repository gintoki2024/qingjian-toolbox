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
    def run(arguments, **kwargs):
        if '--basetemp' in arguments:
            target = Path(arguments[arguments.index('--basetemp') + 1])
            assert target.parent.is_dir()
        return Mock(returncode=0)
    with patch.object(module.subprocess, 'run', side_effect=run):
        module.focused_regression(tmp_path / 'python', tmp_path / 'home', tmp_path / 'output')


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
