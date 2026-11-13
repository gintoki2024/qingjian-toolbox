"""Update approved release documents without changing tested macOS code.

This helper never extracts archives. It preserves Unix mode bits, symlink
payloads, member order and archive comments, then verifies every member's
content and metadata. Only obsolete ZIP64 size fields are regenerated.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import struct
import zipfile


ALLOWED_UPDATES = frozenset({
    '使用说明.txt',
    '程序文件/使用说明.txt',
    '程序文件/MACOS_README.txt',
    '程序文件/macos_build/使用说明-Mac.txt',
    '程序文件/MACOS_VALIDATION.txt',
})
ROOT_ENTRIES = frozenset({'启动青简工具箱.command', '使用说明.txt', '程序文件'})
CHUNK_SIZE = 1024 * 1024


def _without_zip64(extra: bytes) -> bytes:
    """Keep Unix/timestamp extras; let zipfile rebuild size/offset fields."""
    remaining = bytearray()
    position = 0
    while position < len(extra):
        if len(extra) - position < 4:
            raise ValueError('Malformed ZIP extra field')
        kind, length = struct.unpack_from('<HH', extra, position)
        end = position + 4 + length
        if end > len(extra):
            raise ValueError('Truncated ZIP extra field')
        if kind != 0x0001:
            remaining.extend(extra[position:end])
        position = end
    return bytes(remaining)


def _safe_name(name: str) -> None:
    p = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if (not name or '\\' in name or '\x00' in name or p.is_absolute()
            or windows.drive or windows.is_absolute()
            or name.rstrip('/') != str(p)
            or any(part in {'.', '..'} or ':' in part for part in p.parts)):
        raise ValueError('Unsafe ZIP member name: ' + repr(name))


def _stream_digest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[int, str]:
    size, hasher = 0, hashlib.sha256()
    with archive.open(info, 'r') as stream:
        while block := stream.read(CHUNK_SIZE):
            size += len(block)
            hasher.update(block)
    return size, hasher.hexdigest()


def _metadata(info: zipfile.ZipInfo) -> tuple:
    # CRC, compressed size, offsets and descriptor flags are writer-owned.
    # Mode and create_system together encode the Unix symlink/executable bits.
    return (info.filename, info.date_time, info.compress_type, info.create_system,
            info.create_version, info.external_attr, info.internal_attr,
            info.comment, _without_zip64(info.extra), info.volume, info.reserved)


def stamp_archive(source: Path, target: Path, updates: dict[str, bytes]) -> dict:
    """Write a new verified ZIP containing only allowlisted document changes.

    ``target`` must not exist. Each update must name a regular document already
    in the source ZIP. On a copy/verification error, only this call's new target
    is removed. The original archive is always read-only.
    """
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError('Source and target archives must differ')
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    if not isinstance(updates, dict):
        raise TypeError('updates must be a dict[str, bytes]')
    if any(not isinstance(name, str) or name not in ALLOWED_UPDATES for name in updates):
        raise ValueError('Only the five approved release documents may be changed')
    if any(not isinstance(data, bytes) for data in updates.values()):
        raise TypeError('All document updates must be bytes')

    created = False
    try:
        with zipfile.ZipFile(source, 'r') as original:
            members = original.infolist()
            names = [info.filename for info in members]
            if len(names) != len(set(names)):
                raise ValueError('Duplicate ZIP member names are not allowed')
            roots = {name.split('/')[0] for name in names}
            if roots != ROOT_ENTRIES:
                raise ValueError('macOS archive must have exactly the three release root entries')
            for info in members:
                _safe_name(info.filename)
                if info.orig_filename != info.filename:
                    raise ValueError('Noncanonical ZIP member alias: ' + repr(info.orig_filename))
                _without_zip64(info.extra)
                if info.flag_bits & 1:
                    raise ValueError('Encrypted ZIP members are not supported')
            missing = set(updates) - set(names)
            if missing:
                raise ValueError('Update documents missing from ZIP: ' + ', '.join(sorted(missing)))
            for name in updates:
                info = original.getinfo(name)
                file_type = stat.S_IFMT(info.external_attr >> 16)
                if info.is_dir() or file_type not in (0, stat.S_IFREG):
                    raise ValueError('Update document must be a regular file: ' + name)

            before = {}
            with target.open('xb') as target_stream:
                created = True
                with zipfile.ZipFile(target_stream, 'w', allowZip64=True) as output:
                    output.comment = original.comment
                    for info in members:
                        replacement = updates.get(info.filename)
                        clone = copy.copy(info)
                        clone.extra = _without_zip64(info.extra)
                        clone.file_size = len(replacement) if replacement is not None else info.file_size
                        hasher, size = hashlib.sha256(), 0
                        with original.open(info, 'r') as input_stream:
                            with output.open(clone, 'w', force_zip64=clone.file_size > zipfile.ZIP64_LIMIT) as out:
                                # zipfile supplies a default when external_attr
                                # is zero; preserve the original even then.
                                clone.external_attr = info.external_attr
                                while block := input_stream.read(CHUNK_SIZE):
                                    size += len(block)
                                    hasher.update(block)
                                    if replacement is None:
                                        out.write(block)
                                if replacement is not None:
                                    out.write(replacement)
                        if size != info.file_size:
                            raise ValueError('Source member size mismatch: ' + info.filename)
                        before[info.filename] = (size, hasher.hexdigest())

            modified = []
            with zipfile.ZipFile(target, 'r') as result:
                after_members = result.infolist()
                if [info.filename for info in after_members] != names:
                    raise ValueError('Archive member order or names changed')
                if result.comment != original.comment:
                    raise ValueError('Archive comment changed')
                for old, new in zip(members, after_members):
                    if _metadata(old) != _metadata(new):
                        raise ValueError('Archive member permissions or metadata changed: ' + old.filename)
                    actual = _stream_digest(result, new)
                    replacement = updates.get(old.filename)
                    expected = before[old.filename]
                    if replacement is not None:
                        expected = len(replacement), hashlib.sha256(replacement).hexdigest()
                        if expected != before[old.filename]:
                            modified.append(old.filename)
                    if actual != expected:
                        raise ValueError('Archive member content changed unexpectedly: ' + old.filename)
                if result.testzip() is not None:
                    raise ValueError('Output archive CRC verification failed')

        with target.open('rb') as stream:
            target_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'verified': True, 'modified': modified, 'requested_updates': sorted(updates),
                'unchanged_members': len(members) - len(modified), 'members': len(members),
                'root_entries': sorted(ROOT_ENTRIES),
                'symlinks_preserved': sum(stat.S_ISLNK(info.external_attr >> 16) for info in members),
                'target_sha256': target_sha256, 'target_bytes': target.stat().st_size}
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
