"""Offline source entry: validate bundled components, then run the app normally."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import traceback

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def run(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description='青简完整CMD包离线启动')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--no-ui', action='store_true')
    options, app_arguments = parser.parse_known_args(arguments)
    if app_arguments[:1] == ['--']:
        app_arguments = app_arguments[1:]
    log = None
    try:
        mode = json.loads((PROJECT / 'cmd-package.json').read_text(encoding='utf-8'))
        if mode.get('schema_version') != 1 or mode.get('mode') != 'offline':
            raise RuntimeError('完整CMD包标记无效，请完整解压后重试。')
        bundled = PROJECT / 'Python/python.exe'
        allowed = (bundled, PROJECT / 'Python/pythonw.exe')
        if not bundled.is_file() or not any(p.is_file() and Path(sys.executable).samefile(p) for p in allowed):
            raise RuntimeError('完整CMD包必须使用包内Python解释器。请使用根目录CMD启动。')
        home = PROJECT / 'runtime'
        (home / 'logs').mkdir(parents=True, exist_ok=True)
        for key in ('PYTHONHOME', 'PYTHONPATH', 'QINGJIAN_BOOTSTRAP_READY',
                    'QINGJIAN_LIBREOFFICE', 'QINGJIAN_NATIVE_COMPONENTS', 'QINGJIAN_NATIVE_ROOTS',
                    'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
            os.environ.pop(key, None)
        os.environ['QINGJIAN_RUNTIME_HOME'] = str(home)
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        from bootstrap import check_environment, prepare_environment
        if options.check_only:
            checked = check_environment(PROJECT, python=bundled, home=home)
            print(json.dumps({'event': 'check', 'mode': 'offline-cmd', **checked}, ensure_ascii=False), flush=True)
            return 0 if checked['ready'] else 1
        import datetime
        name = 'cmd-startup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.jsonl'
        log = home / 'logs' / name
        def emit(value):
            with log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        # offline=True makes all repair/download paths fail explicitly. This
        # package never installs into a system interpreter or fetches components.
        ready = prepare_environment(PROJECT, python=bundled, home=home, emit=emit, offline=True)
        # Probes need console Python's captured streams; the GUI itself stays
        # in pythonw. Its ready stamp must refer to the running interpreter.
        from bootstrap import environment_stamp
        ready['environment']['QINGJIAN_BOOTSTRAP_READY'] = environment_stamp(PROJECT, sys.executable)
        os.environ.update(ready['environment'])
        if options.prepare_only:
            return 0
        sys.argv = [str(PROJECT / 'main.py'), *app_arguments]
        import main
        code = main.run()
        emit({'event': 'exit', 'code': code})
        return code
    except Exception as error:
        detail = traceback.format_exc()
        if log:
            try:
                with log.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'event': 'error', 'detail': detail}, ensure_ascii=False) + '\n')
            except OSError:
                pass
        message = 'CMD完整包启动失败：' + str(error)
        if log:
            message += '\n日志：' + str(log)
        if options.no_ui or options.check_only:
            print(json.dumps({'event': 'error', 'message': message}, ensure_ascii=False), flush=True)
        elif sys.platform == 'win32':
            ctypes.windll.user32.MessageBoxW(None, message, '青简工具箱CMD启动诊断', 0x10)
        return 2


if __name__ == '__main__':
    raise SystemExit(run())
