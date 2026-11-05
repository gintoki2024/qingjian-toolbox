"""In-memory duplicate checks and reviewed, reversible file operations.

This module creates neither reports nor persistent scan history.  A session is
owned by one UI worker at a time.  File identities and content are checked again
before every operation; links are deliberately outside the editable scope.
"""
from __future__ import annotations

import hashlib
import os
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .common import CancelledError, ensure_cancel, notify


MAX_FILES = 50000
CHUNK_SIZE = 1024 * 1024
_DONE = frozenset({"moved", "trashed"})


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _absolute(value) -> Path:
    return Path(os.path.abspath(Path(value).expanduser()))


def _reparse(details) -> bool:
    return bool(getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))


def _no_links(path: Path):
    for part in (path, *path.parents):
        try:
            details = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode) or _reparse(details):
            raise ValueError(f"不处理符号链接、目录联接或重解析路径：{path}")


def _fingerprint(details) -> tuple:
    value = (details.st_size, details.st_mtime_ns,
             details.st_dev, details.st_ino, details.st_nlink)
    # Windows ctime is a creation timestamp, rather than a change counter.
    # NTFS may update it lazily and lstat/fstat can expose different values for
    # the same open file.  File ID, size, mtime and link count remain checked;
    # every operation also rechecks the SHA256, including same-size edits.
    return value if os.name == "nt" else value + (details.st_ctime_ns,)


def _regular(path: Path):
    _no_links(path)
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"文件不存在或不是普通文件：{path}")
    if details.st_nlink != 1:
        raise ValueError(f"硬链接文件不计为独立副本，已跳过：{path}")
    return details


@dataclass(frozen=True)
class DuplicateFile:
    path: Path
    size: int
    modified_ns: int
    fingerprint: tuple
    sha256: str


@dataclass(frozen=True)
class DuplicateGroup:
    key: str
    size: int
    sha256: str
    files: tuple[DuplicateFile, ...]


@dataclass(frozen=True)
class DuplicateScan:
    groups: tuple[DuplicateGroup, ...] = ()
    scanned_count: int = 0
    errors: tuple[str, ...] = ()
    cancelled: bool = False

    @property
    def duplicate_count(self) -> int:
        return sum(len(group.files) - 1 for group in self.groups)

    @property
    def duplicate_bytes(self) -> int:
        return sum((len(group.files) - 1) * group.size for group in self.groups)


@dataclass(frozen=True)
class FileOutcome:
    source: str
    status: str
    message: str
    target: str = ""


@dataclass
class DuplicateBatch:
    items: list[FileOutcome] = field(default_factory=list)
    cancelled: bool = False

    @property
    def success_count(self) -> int:
        return sum(item.status in _DONE for item in self.items)


def _hash_file(path: Path, expected=None, cancel=None) -> DuplicateFile:
    before = _regular(path)
    fingerprint = _fingerprint(before)
    if expected is not None and fingerprint != expected.fingerprint:
        raise ValueError("文件已修改、替换或链接关系发生变化，请重新扫描。")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if _fingerprint(os.fstat(stream.fileno())) != fingerprint:
            raise ValueError("打开文件时内容或文件身份发生变化，请重新扫描。")
        while True:
            ensure_cancel(cancel)
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        if _fingerprint(os.fstat(stream.fileno())) != fingerprint:
            raise ValueError("读取过程中发生变化，请关闭编辑程序后重新扫描。")
    if _fingerprint(_regular(path)) != fingerprint:
        raise ValueError("读取后文件发生变化，请重新扫描。")
    value = digest.hexdigest()
    if expected is not None and value != expected.sha256:
        raise ValueError("文件内容已变化，请重新扫描。")
    return DuplicateFile(path, before.st_size, before.st_mtime_ns, fingerprint, value)


def _collect_files(sources, recursive, errors, progress, cancel):
    """Bounded walk which never enters a link, including Windows junctions."""
    files, seen, seen_dirs = [], set(), set()
    stack = [(_absolute(value), True) for value in reversed(sources)]
    while stack:
        ensure_cancel(cancel)
        path, explicit = stack.pop()
        try:
            _no_links(path)
            details = path.lstat()
            if stat.S_ISDIR(details.st_mode):
                key = _path_key(path)
                if key in seen_dirs:
                    continue
                seen_dirs.add(key)
                notify(progress, 0, 0, f"读取文件夹：{path}")
                with os.scandir(path) as entries:
                    children = sorted(entries, key=lambda entry: entry.name.casefold())
                for entry in reversed(children):
                    child = path / entry.name
                    ensure_cancel(cancel)
                    if entry.name.startswith("~$"):
                        continue
                    try:
                        child_details = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(child_details.st_mode) or _reparse(child_details):
                            errors.append(f"已跳过链接：{child}")
                            continue
                        if stat.S_ISDIR(child_details.st_mode):
                            if recursive:
                                stack.append((child, False))
                        elif stat.S_ISREG(child_details.st_mode):
                            stack.append((child, False))
                    except OSError as exc:
                        errors.append(f"{child}：{exc}")
            else:
                _regular(path)
                if path.name.startswith("~$"):
                    continue
                key = _path_key(path)
                if key in seen:
                    continue
                seen.add(key)
                files.append(path)
                if len(files) > MAX_FILES:
                    raise OverflowError(f"一次最多扫描 {MAX_FILES} 个文件，请缩小选择范围。")
        except OverflowError:
            raise
        except (OSError, ValueError) as exc:
            errors.append(f"{path}：{exc}")
    return files


def scan_duplicates(sources, options=None, progress=None, cancel=None) -> DuplicateScan:
    """Find exact byte-for-byte copies without writing any result files."""
    options = dict(options or {})
    errors, hashed, buckets = [], defaultdict(list), defaultdict(list)
    files, done, cancelled = [], 0, False
    try:
        files = _collect_files(list(sources), bool(options.get("recursive", True)), errors, progress, cancel)
        for source in files:
            ensure_cancel(cancel)
            try:
                buckets[_regular(source).st_size].append(source)
            except (OSError, ValueError) as exc:
                errors.append(f"{source}：{exc}")
        for size, paths in buckets.items():
            for path in paths:
                ensure_cancel(cancel)
                if len(paths) > 1:
                    try:
                        item = _hash_file(path, cancel=cancel)
                        if item.size != size:
                            raise ValueError("文件大小在扫描期间发生变化，请重新扫描。")
                        hashed[(size, item.sha256)].append(item)
                    except CancelledError:
                        raise
                    except (OSError, ValueError) as exc:
                        errors.append(f"{path}：{exc}")
                done += 1
                notify(progress, done, len(files), f"校验文件：{path.name}")
    except CancelledError:
        cancelled = True
    groups = tuple(DuplicateGroup(f"{size}:{digest}", size, digest,
                                  tuple(sorted(items, key=lambda item: _path_key(item.path))))
                   for (size, digest), items in sorted(hashed.items()) if len(items) > 1)
    return DuplicateScan(groups, len(files), tuple(errors), cancelled)


def _destination(value, selected):
    if value is None or not str(value).strip():
        raise ValueError("请先选择整理目录。")
    directory = _absolute(value)
    _no_links(directory)
    if not directory.is_dir():
        raise ValueError("整理目录不存在或不是文件夹，请重新选择。")
    if any(_path_key(item.path.parent) == _path_key(directory) for item in selected):
        raise ValueError("整理目录不能是本次所选文件的原目录，请另选文件夹。")
    return directory


def _remove_owned_copy(path, identity):
    try:
        details = _regular(path)
        if (details.st_dev, details.st_ino) == identity:
            path.unlink()
    except (OSError, ValueError):
        pass


def _preserve_modified_time(stream, modified_ns):
    """Set the created copy's time through its handle, never a followed path."""
    descriptor = stream.fileno()
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        # Windows FILETIME counts 100 ns intervals from 1601-01-01 UTC.
        ticks = modified_ns // 100 + 116444736000000000
        value = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        set_time = ctypes.WinDLL("kernel32", use_last_error=True).SetFileTime
        set_time.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                             ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
        set_time.restype = wintypes.BOOL
        if not set_time(msvcrt.get_osfhandle(descriptor), None, None, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.utime(descriptor, ns=(os.fstat(descriptor).st_atime_ns, modified_ns))


def _copy_exclusive(item, directory, cancel):
    """Copy on the destination volume; exclusive creation never overwrites."""
    _no_links(directory)
    if not directory.is_dir():
        raise ValueError("整理目录已变化，请重新选择。")
    source = _hash_file(item.path, item, cancel)
    target, identity, destination = None, None, None
    # Keep enough room for the disambiguating suffix in Windows filename limits.
    stem, suffix = item.path.stem, item.path.suffix
    if len(suffix) > 32:
        stem, suffix = item.path.name, ""
    for number in range(1, 100000):
        name = item.path.name if number == 1 else f"{stem[:160]}_{number}{suffix}"
        target = directory / name
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o666)
            destination = os.fdopen(descriptor, "wb")
            created = os.fstat(destination.fileno())
            identity = (created.st_dev, created.st_ino)
            break
        except FileExistsError:
            continue
    if destination is None:
        raise FileExistsError("整理目录中同名文件过多，请更换目录。")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        digest = hashlib.sha256()
        with destination, os.fdopen(os.open(item.path, flags), "rb") as origin:
            if _fingerprint(os.fstat(origin.fileno())) != source.fingerprint:
                raise ValueError("复制前原文件发生变化，未移动。")
            while True:
                ensure_cancel(cancel)
                chunk = origin.read(CHUNK_SIZE)
                if not chunk:
                    break
                destination.write(chunk)
                digest.update(chunk)
            destination.flush()
            os.fsync(destination.fileno())
            if _fingerprint(os.fstat(origin.fileno())) != source.fingerprint or digest.hexdigest() != source.sha256:
                raise ValueError("复制期间原文件发生变化，未移动。")
            # Windows does not support utime(path, follow_symlinks=False).
            # Updating the still-open destination also prevents a substituted
            # path from directing the timestamp write to an unrelated file.
            _preserve_modified_time(destination, item.modified_ns)
        if _hash_file(target, cancel=cancel).sha256 != item.sha256:
            raise ValueError("目标副本校验失败，原文件保留。")
        return target, identity
    except BaseException:
        destination.close()
        _remove_owned_copy(target, identity)
        raise


class DuplicateSession:
    """A scan plus in-memory per-file status; no filesystem side effects at init."""

    def __init__(self, scan: DuplicateScan):
        self.scan = scan
        self.groups = scan.groups
        self.outcomes: dict[str, FileOutcome] = {}
        self._files = {_path_key(item.path): (group, item)
                       for group in self.groups for item in group.files}

    def available_files(self, group):
        return tuple(item for item in group.files
                     if self.outcomes.get(_path_key(item.path), FileOutcome("", "", "")).status not in _DONE)

    def suggest_selection(self):
        """Keep the first displayed copy of every group; nothing is selected by default."""
        return [item.path for group in self.groups for item in self.available_files(group)[1:]]

    def validate_selection(self, selected_paths):
        selected, seen, by_group = [], set(), defaultdict(list)
        for path in selected_paths:
            key = _path_key(_absolute(path))
            if key in seen:
                continue
            seen.add(key)
            if key not in self._files:
                raise ValueError("所选文件不属于当前扫描，请重新扫描。")
            group, item = self._files[key]
            if item not in self.available_files(group):
                raise ValueError(f"文件已经处理，不能重复操作：{item.path}")
            selected.append(item)
            by_group[group.key].append(item)
        if not selected:
            raise ValueError("请先勾选需要处理的重复副本。")
        for group in self.groups:
            if len(by_group[group.key]) >= len(self.available_files(group)):
                raise ValueError("每个重复组至少保留一份未勾选的原文件，不能整组移走。")
        return selected

    def review_rows(self, selected_paths):
        selected = self.validate_selection(selected_paths)
        keys = {_path_key(item.path) for item in selected}
        rows = []
        for item in selected:
            group, _ = self._files[_path_key(item.path)]
            keeper = next(other for other in self.available_files(group) if _path_key(other.path) not in keys)
            rows.append({"source": str(item.path), "keeper": str(keeper.path), "size": item.size})
        return rows

    def _keeper(self, group, selected_keys, cancel):
        # Match the first retained path displayed by review_rows.  If it changes,
        # do not silently substitute an unreviewed keeper and continue the batch.
        item = next((item for item in self.available_files(group)
                     if _path_key(item.path) not in selected_keys), None)
        if item is None:
            raise ValueError("未找到确认清单中的保留副本，已停止处理本文件；请重新扫描。")
        try:
            return _hash_file(item.path, item, cancel)
        except CancelledError:
            raise
        except (OSError, ValueError) as exc:
            raise ValueError(f"确认清单中的保留副本已变化或无法访问，已停止处理本文件；请重新扫描。 {exc}") from exc

    def process(self, selected_paths, action, destination=None, trash=None, progress=None, cancel=None):
        """Recheck and move selected duplicates.  There is no permanent-delete action.

        ``trash`` is the platform's recycle-bin callback, returning ``(ok, path)``.
        Its absence is an error; it must never be replaced with unlink/rmtree.
        """
        if action not in {"move", "trash"}:
            raise ValueError("仅支持移到整理目录或移入系统回收站。")
        selected = self.validate_selection(selected_paths)
        directory = _destination(destination, selected) if action == "move" else None
        if action == "trash" and not callable(trash):
            raise ValueError("当前系统不支持移入回收站；请使用移到整理目录。")
        selected_keys = {_path_key(item.path) for item in selected}
        result = DuplicateBatch()
        for index, item in enumerate(selected, 1):
            target, identity = None, None
            try:
                ensure_cancel(cancel)
                group, _ = self._files[_path_key(item.path)]
                self._keeper(group, selected_keys, cancel)
                _hash_file(item.path, item, cancel)
                if action == "move":
                    target, identity = _copy_exclusive(item, directory, cancel)
                    ensure_cancel(cancel)
                    # Check both the untouched copy and the source immediately
                    # before removing the source name.  Target bytes are verified too.
                    self._keeper(group, selected_keys, cancel)
                    _hash_file(item.path, item, cancel)
                    if _hash_file(target, cancel=cancel).sha256 != item.sha256:
                        raise ValueError("目标副本已变化，原文件保留。")
                    ensure_cancel(cancel)
                    if _fingerprint(_regular(item.path)) != item.fingerprint:
                        raise ValueError("移动前原文件发生变化，原文件保留。")
                    item.path.unlink()
                    outcome = FileOutcome(str(item.path), "moved", "已移到整理目录", str(target))
                else:
                    self._keeper(group, selected_keys, cancel)
                    _hash_file(item.path, item, cancel)
                    ensure_cancel(cancel)
                    response = trash(str(item.path))
                    ok, recycled_path = (response[0], response[1] if len(response) > 1 else "") if isinstance(response, tuple) else (bool(response), "")
                    if not ok:
                        raise OSError("系统未能将文件移入回收站，原文件未主动删除。")
                    if os.path.lexists(item.path):
                        raise OSError("系统返回成功但原路径仍有文件，状态未确认，请重新扫描。")
                    outcome = FileOutcome(str(item.path), "trashed", "已移入系统回收站", str(recycled_path or ""))
                self.outcomes[_path_key(item.path)] = outcome
                result.items.append(outcome)
            except CancelledError:
                if target is not None:
                    _remove_owned_copy(target, identity)
                result.cancelled = True
                break
            except Exception as exc:
                if target is not None:
                    # Keep a verified copy if removing the source failed.  This
                    # avoids losing either version after an unexpected filesystem error.
                    message = f"{exc}；整理目录中的副本已保留：{target}"
                else:
                    message = str(exc)
                outcome = FileOutcome(str(item.path), "failed", message, str(target or ""))
                self.outcomes[_path_key(item.path)] = outcome
                result.items.append(outcome)
            notify(progress, index, len(selected), f"处理重复副本：{item.path.name}")
        return result
