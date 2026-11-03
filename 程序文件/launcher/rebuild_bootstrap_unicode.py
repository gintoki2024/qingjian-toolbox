"""Build the pure-Python win-unicode-console dependency from pinned upstream sources.

Build environment: Python 3.12, setuptools 84.2.1. Runtime needs no build tools.
The upstream PyPI sdist omits LICENSE; the identical version's Git tag supplies it.
Only that license text is added. Python module bytes are preserved unchanged.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

SOURCE_NAME='win_unicode_console-0.5.zip'
SOURCE_URL='https://files.pythonhosted.org/packages/89/8d/7aad74930380c8972ab282304a2ff45f3d4927108bb6693cabcc9fc6a099/win_unicode_console-0.5.zip'
SOURCE_HASH='d4142d4d56d46f449d6f00536a73625a871cba040f0bc1a2e305a04578f07d1e'
SOURCE_BYTES=31420
LICENSE_URL='https://raw.githubusercontent.com/Drekin/win-unicode-console/6beb5df5219ac1e5495b415d286e634a27720b2e/LICENSE'
LICENSE_HASH='575eb041efcedf791415a04171f18f48b0303611d2fe85ef8c8da3025d905647'
LICENSE_BYTES=1072
WHEEL_NAME='win_unicode_console-0.5-py3-none-any.whl'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def ensure(path,url,size,digest):
    if not path.exists():
        data=urllib.request.urlopen(url,timeout=60).read()
        assert len(data)==size and sha(data)==digest,'Upstream checksum mismatch'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
    data=path.read_bytes()
    assert len(data)==size and sha(data)==digest,'Local input checksum mismatch'
    return data

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).with_name(SOURCE_NAME))
    parser.add_argument('--license',type=Path,default=Path(__file__).with_name('win-unicode-console-LICENSE.txt'))
    parser.add_argument('--output',type=Path,default=Path(__file__).with_name('wheels'))
    args=parser.parse_args()
    assert importlib.metadata.version('setuptools')=='84.2.1','Rebuild with setuptools==84.2.1'
    ensure(args.source,SOURCE_URL,SOURCE_BYTES,SOURCE_HASH)
    license_data=ensure(args.license,LICENSE_URL,LICENSE_BYTES,LICENSE_HASH)
    args.output.mkdir(parents=True,exist_ok=True)
    output=args.output.resolve()
    with tempfile.TemporaryDirectory(prefix='unicode-wheel-',dir=output.parent) as scratch:
        root=Path(scratch)
        with zipfile.ZipFile(args.source) as source:
            for name in source.namelist():
                part=PurePosixPath(name)
                assert not part.is_absolute() and '..' not in part.parts,'Unsafe archive path'
                target=root.joinpath(*part.parts)
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(source.read(name))
        project=root/'win_unicode_console-0.5'
        (project/'LICENSE').write_bytes(license_data)
        env=dict(os.environ,SOURCE_DATE_EPOCH='1703548800',PYTHONHASHSEED='0')
        subprocess.run([sys.executable,'setup.py','-q','bdist_wheel','--dist-dir',str(output)],cwd=project,env=env,check=True)
        result=output/WHEEL_NAME
        with zipfile.ZipFile(result) as archive:
            modules=[name for name in archive.namelist() if '.dist-info/' not in name]
            assert len(modules)==12 and all(n.endswith('.py') for n in modules)
            for name in modules:
                assert archive.read(name)==(project/name).read_bytes(),'Runtime code was modified'
            wheel_metadata=archive.read('win_unicode_console-0.5.dist-info/WHEEL').decode()
            assert 'Root-Is-Purelib: true' in wheel_metadata and 'Tag: py3-none-any' in wheel_metadata
            license_name=next(n for n in archive.namelist() if n.endswith('/LICENSE'))
            assert archive.read(license_name)==license_data
            report={'wheel':str(result),'bytes':result.stat().st_size,'sha256':sha(result.read_bytes()),'pure_python':True,
                'original_modules_unmodified':True,'license':'MIT','license_path_in_wheel':license_name,
                'source':{'url':SOURCE_URL,'sha256':SOURCE_HASH,'bytes':SOURCE_BYTES},
                'license_source':{'url':LICENSE_URL,'sha256':LICENSE_HASH,'bytes':LICENSE_BYTES},
                'members':archive.namelist(),'wheel_metadata':wheel_metadata,
                'build':{'python':sys.version,'setuptools':importlib.metadata.version('setuptools'),'source_date_epoch':env['SOURCE_DATE_EPOCH']}}
            (output/'unicode-build-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report,indent=2))

if __name__=='__main__':
    main()
