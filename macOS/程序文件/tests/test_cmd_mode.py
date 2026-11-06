"""Direct main.py must keep CMD distributions on their Python route."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

import bootstrap


@pytest.mark.parametrize('mode', ['source', 'offline'])
def test_cmd_main_uses_python_route_without_legacy_exe(tmp_path, mode):
    (tmp_path / 'cmd-package.json').write_text(json.dumps({'schema_version': 1, 'mode': mode}), encoding='utf-8')
    launcher = tmp_path / 'launcher'
    launcher.mkdir()
    entry = launcher / ('cmd_source.py' if mode == 'source' else 'offline_cmd.py')
    entry.write_text('# fixture', encoding='utf-8')
    if mode == 'offline':
        python = tmp_path / 'Python/python.exe'
        python.parent.mkdir()
        python.write_bytes(b'fixture')
    else:
        python = Path(sys.executable)
    with patch.object(bootstrap, 'environment_stamp', return_value='not-ready'), \
         patch.object(bootstrap, 'source_launcher', side_effect=AssertionError('legacy EXE must not be used')), \
         patch.object(bootstrap.subprocess, 'call', return_value=0) as called:
        assert bootstrap.start_source(tmp_path, ['--layout-report', '中文 空格.json']) is False
    command = called.call_args.args[0]
    assert command[0] == str(python)
    assert str(entry) in command
    assert command[-2:] == ['--layout-report', '中文 空格.json']
    assert '启动青简工具箱.exe' not in ' '.join(command)


@pytest.mark.parametrize('marker', ['[]', '{"schema_version":1,"mode":"other"}', '{invalid'])
def test_invalid_cmd_marker_fails_before_handoff(tmp_path, marker):
    (tmp_path / 'cmd-package.json').write_text(marker, encoding='utf-8')
    with patch.object(bootstrap, 'environment_stamp', return_value='not-ready'), \
         patch.object(bootstrap.subprocess, 'call', side_effect=AssertionError('must not launch')):
        with pytest.raises(bootstrap.BootstrapError, match='启动标记无效'):
            bootstrap.start_source(tmp_path)


def test_cmd_child_failure_is_reported(tmp_path):
    (tmp_path / 'cmd-package.json').write_text('{"schema_version":1,"mode":"source"}', encoding='utf-8')
    (tmp_path / 'launcher').mkdir()
    (tmp_path / 'launcher/cmd_source.py').touch()
    with patch.object(bootstrap, 'environment_stamp', return_value='not-ready'), \
         patch.object(bootstrap.subprocess, 'call', return_value=5):
        with pytest.raises(bootstrap.BootstrapError, match='退出码：5'):
            bootstrap.start_source(tmp_path)
