"""Small stdlib tests, using ordinary QA directories rather than tempfile."""
import hashlib
from pathlib import Path
import stat
import struct
import unittest
import uuid
import warnings
import zipfile

from stamp_macos_release import ALLOWED_UPDATES, stamp_archive


class StampTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent / ('stamp-tests-' + uuid.uuid4().hex[:10])
        cls.root.mkdir()

    def setUp(self):
        self.folder = self.root / self._testMethodName
        self.folder.mkdir()

    def fixture(self, *, missing=None, duplicate=False, symlink_document=False):
        source = self.folder / '原始-Mac.zip'
        with zipfile.ZipFile(source, 'x') as z:
            z.comment = b'preserve release archive comment'
            entries = [
                ('启动青简工具箱.command', b'#!/bin/sh\necho qingjian\n', stat.S_IFREG | 0o755),
                ('程序文件/qingjian/app.py', b'print("unchanged tested code")\n', stat.S_IFREG | 0o644),
                ('程序文件/Python/bin/python3', bytes(range(256)) * 12, stat.S_IFREG | 0o755),
                ('程序文件/Python/bin/python', b'python3', stat.S_IFLNK | 0o777),
                ('程序文件/空目录/', b'', stat.S_IFDIR | 0o755),
            ]
            entries += [(name, ('旧说明 ' + name).encode('utf-8'),
                         stat.S_IFLNK | 0o777 if symlink_document and name == '程序文件/MACOS_VALIDATION.txt' else stat.S_IFREG | 0o644)
                        for name in sorted(ALLOWED_UPDATES) if name != missing]
            for name, data, mode in entries:
                info = zipfile.ZipInfo(name, date_time=(2026, 11, 12, 12, 34, 56))
                info.create_system = 3
                info.external_attr = mode << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                info.comment = b'member comment'
                info.extra = struct.pack('<HHI', 0xCAFE, 4, 1234)
                if name == '程序文件/Python/bin/python3':
                    with z.open(info, 'w', force_zip64=True) as stream:
                        stream.write(data)
                else:
                    z.writestr(info, data)
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    z.writestr('程序文件/qingjian/app.py', b'duplicate')
        return source

    def test_preserves_unicode_executable_symlink_binary_code_and_metadata(self):
        source = self.fixture()
        before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        target = self.folder / '已验收-Mac.zip'
        updates = {name: ('本轮真实验收记录：' + name).encode('utf-8') for name in ALLOWED_UPDATES}
        report = stamp_archive(source, target, updates)
        self.assertTrue(report['verified'])
        self.assertEqual(set(report['modified']), ALLOWED_UPDATES)
        self.assertEqual(report['unchanged_members'], 5)
        self.assertEqual(report['symlinks_preserved'], 1)
        self.assertEqual(report['target_sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(report['target_bytes'], target.stat().st_size)
        self.assertEqual(before_hash, hashlib.sha256(source.read_bytes()).hexdigest())
        with zipfile.ZipFile(source) as old, zipfile.ZipFile(target) as new:
            self.assertEqual(old.namelist(), new.namelist())
            self.assertEqual(old.comment, new.comment)
            for info in old.infolist():
                out = new.getinfo(info.filename)
                self.assertEqual(info.external_attr, out.external_attr)
                self.assertEqual(info.create_system, out.create_system)
                self.assertEqual(info.date_time, out.date_time)
                self.assertEqual(info.extra, out.extra)
                self.assertEqual(new.read(out), updates.get(info.filename, old.read(info)))
            self.assertEqual((new.getinfo('启动青简工具箱.command').external_attr >> 16) & 0o777, 0o755)
            self.assertTrue(stat.S_ISLNK(new.getinfo('程序文件/Python/bin/python').external_attr >> 16))
            self.assertIsNone(new.testzip())

    def test_rejects_nonallowlisted_path_before_creating_target(self):
        source = self.fixture()
        for name in ['程序文件/qingjian/app.py', '../使用说明.txt', '/使用说明.txt', '程序文件\\使用说明.txt']:
            target = self.folder / (uuid.uuid4().hex + '.zip')
            with self.assertRaises(ValueError):
                stamp_archive(source, target, {name: b'changed'})
            self.assertFalse(target.exists())

    def test_rejects_duplicate_members(self):
        source = self.fixture(duplicate=True)
        target = self.folder / 'target.zip'
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            stamp_archive(source, target, {'使用说明.txt': b'changed'})
        self.assertFalse(target.exists())

    def test_rejects_missing_document_and_symlink_document(self):
        source = self.fixture(missing='程序文件/MACOS_VALIDATION.txt')
        target = self.folder / 'target.zip'
        with self.assertRaisesRegex(ValueError, 'missing'):
            stamp_archive(source, target, {'程序文件/MACOS_VALIDATION.txt': b'changed'})
        self.assertFalse(target.exists())
        second = self.folder / 'second'
        second.mkdir()
        previous = self.folder
        self.folder = second
        source = self.fixture(symlink_document=True)
        self.folder = previous
        with self.assertRaisesRegex(ValueError, 'regular file'):
            stamp_archive(source, target, {'程序文件/MACOS_VALIDATION.txt': b'changed'})
        self.assertFalse(target.exists())

    def test_rejects_existing_or_same_target_and_nonbytes(self):
        source = self.fixture()
        with self.assertRaisesRegex(ValueError, 'differ'):
            stamp_archive(source, source, {'使用说明.txt': b'changed'})
        target = self.folder / 'exists.zip'
        target.write_bytes(b'do not overwrite')
        with self.assertRaises(FileExistsError):
            stamp_archive(source, target, {'使用说明.txt': b'changed'})
        self.assertEqual(target.read_bytes(), b'do not overwrite')
        with self.assertRaises(TypeError):
            stamp_archive(source, self.folder/'bad-value.zip', {'使用说明.txt': 'not bytes'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
