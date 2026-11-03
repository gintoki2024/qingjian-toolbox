"""Build a complete offline CMD/source distribution using an existing Python."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import sys

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from build import verify_source_startup
from package_source import source_files
from bootstrap import filesystem_path

PROJECT = Path(__file__).resolve().parent
ROOT_ENTRIES = {'启动青简工具箱.cmd', '使用说明.txt', '程序文件'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def runtime_dependencies(project):
    """Keep installed direct/transitive requirements, excluding build tooling."""
    pending = [(Requirement(line), frozenset()) for line in (project / 'requirements.txt').read_text(encoding='utf-8').splitlines()
               if line.strip() and not line.lstrip().startswith('#')]
    selected, expanded = {}, set()
    while pending:
        req, parent_extras = pending.pop()
        contexts = parent_extras or {''}
        if req.marker and not any(req.marker.evaluate({**default_environment(), 'extra': extra}) for extra in contexts):
            continue
        name = canonicalize_name(req.name)
        dist = selected.setdefault(name, metadata.distribution(name))
        if req.specifier and not req.specifier.contains(dist.version, prereleases=True):
            raise RuntimeError(f'Build environment does not satisfy {req}: {dist.version}')
        key = name, frozenset(req.extras)
        if key in expanded:
            continue
        expanded.add(key)
        pending += [(Requirement(child), req.extras) for child in dist.requires or []]
    return selected


def copy_runtime(project, target):
    base = Path(sys.base_prefix).resolve()
    target.mkdir(exist_ok=True)
    def copy_file(src, dst):
        return shutil.copy2(filesystem_path(Path(src).absolute()), filesystem_path(Path(dst).absolute()))
    for item in base.iterdir():
        if item.is_file() and (item.suffix.lower() in {'.exe', '.dll'} or item.name == 'LICENSE.txt'):
            copy_file(item, target / item.name)
    for name in ('DLLs', 'Lib'):
        def ignore(directory, names):
            omitted = {'__pycache__'}
            normal = str(directory)
            if normal.startswith('\\\\?\\'):
                normal = normal[4:]
            if Path(normal).resolve() == (base / 'Lib').resolve():
                omitted |= {'site-packages', 'test', 'tests', 'idlelib', 'ensurepip', 'tkinter'}
            return omitted.intersection(names) | {n for n in names if n.endswith(('.pyc', '.pyo'))}
        shutil.copytree(filesystem_path(base / name), filesystem_path(target / name), ignore=ignore,
                        copy_function=copy_file, dirs_exist_ok=True)
    site = target / 'Lib/site-packages'
    site.mkdir()
    distributions = runtime_dependencies(project)
    records = []
    for name, dist in sorted(distributions.items()):
        source_site = Path(dist.locate_file('')).resolve()
        copied = 0
        for relative in dist.files or []:
            # C++ development headers are not needed by the installed wheels;
            # omitting them also keeps extraction paths short for Windows 10.
            if len(relative.parts) >= 2 and relative.parts[0].casefold() in {'pyside6', 'shiboken6'} and relative.parts[1] == 'include':
                continue
            src = Path(dist.locate_file(relative)).resolve()
            if src.suffix.lower() in {'.pyc', '.pyo'} or not src.is_file():
                continue
            if not src.is_relative_to(source_site):
                # Installed console entry points are unnecessary for importing
                # libraries; no external build-machine wrappers are copied.
                continue
            out = site / src.relative_to(source_site)
            os.makedirs(filesystem_path(out.parent), exist_ok=True)
            copy_file(src, out)
            copied += 1
        records.append({'name': name, 'version': dist.version, 'files': copied})
    if not (target / 'python.exe').is_file() or not (target / 'pythonw.exe').is_file():
        raise RuntimeError('The Python base does not contain both Windows interpreters')
    # Resolve imports entirely within this folder even when a user's machine
    # has PYTHONPATH, user-site packages or a different Python installation.
    (target / 'python312._pth').write_text('.\nLib\nDLLs\nLib/site-packages\n', encoding='utf-8')
    return {'version': sys.version, 'source_base': str(base), 'packages': records,
            'python_sha256': digest(target / 'python.exe'), 'pythonw_sha256': digest(target / 'pythonw.exe'),
            'dll_sha256': digest(target / 'python312.dll')}


def build(output, native_bundle, project=PROJECT):
    project, output, native_bundle = Path(project).resolve(), Path(output).resolve(), Path(native_bundle).resolve()
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        raise RuntimeError('Build this CMD runtime with the verified Windows Python 3.12 build environment')
    verify_source_startup(project, require_exe=False)
    from qingjian import __version__
    bundle = output / ('青简工具箱_v' + __version__)
    if bundle.exists():
        raise FileExistsError('Keep prior releases; choose a fresh output directory: ' + str(bundle))
    for relative in ('engines/LibreOffice/program/soffice.exe', 'support/component-checksums.json', 'licenses/组件版本.json'):
        if not (native_bundle / relative).is_file():
            raise RuntimeError('Verified native bundle is incomplete: ' + relative)
    program = bundle / '程序文件'
    program.mkdir(parents=True)
    for src in source_files(project):
        relative = src.relative_to(project)
        if src.suffix.lower() == '.exe' or relative.parts[0] == 'support':
            continue
        target = program / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    for name in ('engines', 'support', 'licenses'):
        shutil.copytree(native_bundle / name, program / name)
    runtime = copy_runtime(project, program / 'Python')
    (program / 'cmd-package.json').write_text(json.dumps({'schema_version': 1, 'mode': 'offline', 'version': __version__}, ensure_ascii=False), encoding='utf-8')
    (program / 'Python/运行时来源.json').write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.copy2(project / 'launcher/start_offline.cmd', bundle / '启动青简工具箱.cmd')
    shutil.copy2(project / '使用说明.txt', bundle / '使用说明.txt')
    if {p.name for p in bundle.iterdir()} != ROOT_ENTRIES:
        raise RuntimeError('The CMD release root must contain exactly three entries')
    return {'folder': str(bundle), 'mode': 'offline-cmd', 'version': __version__, 'runtime': runtime,
            'root_entries': sorted(ROOT_ENTRIES)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--native-bundle', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.native_bundle), ensure_ascii=False, indent=2))
