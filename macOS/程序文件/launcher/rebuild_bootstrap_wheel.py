"""Rebuild upstream pure-Python wheel, without installing packages.

Requires Python 3.12 and setuptools 84.2.1 in the build interpreter.
The source hash is published on https://pypi.org/project/red-black-tree-mod/1.22/.
The archive is downloaded only if --source does not already exist.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

SDIST_NAME = 'red-black-tree-mod-1.22.tar.gz'
SDIST_URL = 'https://files.pythonhosted.org/packages/48/75/bfa342a2ebfc9623b701f1c6995b9906fd6dd2cedf6bce777d09e23303ac/red-black-tree-mod-1.22.tar.gz'
SDIST_SHA256 = '38e3652903a2bf96379c27c2082ca0b7b905158662dd7ef0c97f4fd93a9aa908'
SDIST_BYTES = 34173
WHEEL_NAME = 'red_black_tree_mod-1.22-py3-none-any.whl'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).with_name(SDIST_NAME))
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('wheels'))
    args = parser.parse_args()
    if importlib.metadata.version('setuptools') != '84.2.1':
        raise SystemExit('Use a build interpreter with setuptools==84.2.1 for matching bytes.')
    if not args.source.exists():
        args.source.parent.mkdir(parents=True, exist_ok=True)
        data = urllib.request.urlopen(SDIST_URL, timeout=60).read()
        if len(data) != SDIST_BYTES or sha(data) != SDIST_SHA256:
            raise SystemExit('Upstream archive failed size/SHA256 verification.')
        args.source.write_bytes(data)
    data = args.source.read_bytes()
    if len(data) != SDIST_BYTES or sha(data) != SDIST_SHA256:
        raise SystemExit('Upstream archive failed size/SHA256 verification.')
    args.output.mkdir(parents=True, exist_ok=True)
    out = args.output.resolve()
    with tempfile.TemporaryDirectory(prefix='rbt-wheel-', dir=out.parent) as scratch:
        root = Path(scratch)
        with tarfile.open(args.source, 'r:gz') as archive:
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or '..' in relative.parts or member.issym() or member.islnk():
                    raise SystemExit('Unexpected unsafe source archive member.')
                destination = root.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.extractfile(member).read())
                else:
                    raise SystemExit('Unexpected source archive member type.')
        env = dict(os.environ, SOURCE_DATE_EPOCH='1703548800', PYTHONHASHSEED='0')
        subprocess.run([sys.executable, 'setup.py', '-q', 'bdist_wheel', '--dist-dir', str(out)],
                       cwd=root/'red-black-tree-mod-1.22', env=env, check=True)
        wheel_path = out/WHEEL_NAME
        with zipfile.ZipFile(wheel_path) as archive:
            names = archive.namelist()
            assert sorted(name for name in names if not '.dist-info/' in name) == ['red_black_dict_mod.py', 'red_black_set_mod.py']
            wheel_metadata = archive.read('red_black_tree_mod-1.22.dist-info/WHEEL').decode()
            assert 'Root-Is-Purelib: true' in wheel_metadata and 'Tag: py3-none-any' in wheel_metadata
            for filename in ('red_black_dict_mod.py','red_black_set_mod.py'):
                assert archive.read(filename) == (root/'red-black-tree-mod-1.22'/filename).read_bytes()
            copying = next(name for name in names if name.endswith('/COPYING'))
            assert archive.read(copying) == (root/'red-black-tree-mod-1.22'/'COPYING').read_bytes()
            report = {'wheel':str(wheel_path),'bytes':wheel_path.stat().st_size,'sha256':sha(wheel_path.read_bytes()),
                      'pure_python':True,'original_modules_unmodified':True,'license':'MIT',
                      'license_path_in_wheel':copying,'members':names,'wheel_metadata':wheel_metadata,
                      'source':{'url':SDIST_URL,'sha256':SDIST_SHA256,'bytes':SDIST_BYTES},
                      'build':{'python':sys.version,'setuptools':importlib.metadata.version('setuptools'),
                               'source_date_epoch':env['SOURCE_DATE_EPOCH']}}
            (out/'build-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report,indent=2))

if __name__ == '__main__':
    main()
