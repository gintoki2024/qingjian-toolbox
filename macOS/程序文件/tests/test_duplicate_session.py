"""Exact-copy scanning and actual reversible batch operations on disposable files."""
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from qingjian.core import duplicate_session as duplicate
from qingjian.core.common import CancelledError
from qingjian.core.duplicate_session import DuplicateSession, scan_duplicates


def _copies(tmp_path, count=3):
    scope = tmp_path / "输入"
    scope.mkdir()
    files = []
    for index in range(count):
        path = scope / f"副本{index}.txt"
        path.write_text("相同的办公材料\n第二段。", encoding="utf-8")
        files.append(path)
    return scope, files


def _session(tmp_path, count=3):
    scope, files = _copies(tmp_path, count)
    return DuplicateSession(scan_duplicates([scope])), scope, files


def test_scan_groups_content_not_just_name_or_size_and_writes_nothing(tmp_path):
    scope, files = _copies(tmp_path)
    (scope / "内容不同.txt").write_bytes(b"x" * files[0].stat().st_size)
    (scope / "不重复.txt").write_text("single", encoding="utf-8")
    before = set(scope.iterdir())
    result = scan_duplicates([scope, files[0]])
    assert result.scanned_count == 5
    assert len(result.groups) == 1
    assert set(item.path for item in result.groups[0].files) == set(files)
    assert result.duplicate_count == 2
    assert result.duplicate_bytes == files[0].stat().st_size * 2
    assert set(scope.iterdir()) == before
    assert not result.errors


@pytest.mark.skipif(os.name != "nt", reason="Windows creation-time semantics")
def test_windows_scan_accepts_ctime_difference_between_path_and_handle(tmp_path):
    scope, files = _copies(tmp_path)
    real_fstat = os.fstat

    def delayed_creation_time(descriptor):
        details = real_fstat(descriptor)
        values = {name: getattr(details, name) for name in dir(details) if name.startswith("st_")}
        values["st_ctime_ns"] += 10000000
        return SimpleNamespace(**values)

    with patch.object(duplicate.os, "fstat", side_effect=delayed_creation_time):
        result = scan_duplicates([scope])
    assert not result.errors
    assert len(result.groups) == 1
    assert {item.path for item in result.groups[0].files} == set(files)


def test_empty_files_are_exact_duplicates_and_recursive_false_respected(tmp_path):
    (tmp_path / "a").write_bytes(b"")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b").write_bytes(b"")
    assert not scan_duplicates([tmp_path], {"recursive": False}).groups
    assert scan_duplicates([tmp_path]).groups[0].size == 0


def test_no_inputs_or_missing_paths_give_readable_in_memory_result(tmp_path):
    assert scan_duplicates([]).scanned_count == 0
    result = scan_duplicates([tmp_path / "missing"])
    assert not result.groups
    assert result.errors


def test_cancel_during_walk_and_hash_is_recorded(tmp_path):
    scope, _ = _copies(tmp_path)
    assert scan_duplicates([scope], cancel=lambda: True).cancelled
    stop = threading.Event()
    result = scan_duplicates([scope], progress=lambda d, t, m: stop.set() if d >= 1 else None, cancel=stop.is_set)
    assert result.cancelled


def test_hard_links_never_count_as_independent_copies(tmp_path):
    scope, files = _copies(tmp_path, 2)
    alias = scope / "硬链接.txt"
    try:
        os.link(files[0], alias)
    except OSError as exc:
        pytest.skip(f"当前文件系统不可创建硬链接：{exc}")
    result = scan_duplicates([scope])
    assert not result.groups
    assert any("硬链接" in message for message in result.errors)


def test_linked_folder_is_not_walked_and_linked_file_not_counted(tmp_path):
    scope, files = _copies(tmp_path, 2)
    links = tmp_path / "links"
    links.mkdir()
    try:
        (links / "目录链接").symlink_to(scope, target_is_directory=True)
        (links / "文件链接").symlink_to(files[0])
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建符号链接：{exc}")
    result = scan_duplicates([links])
    assert result.scanned_count == 0
    assert not result.groups
    assert len(result.errors) == 2


def test_selection_preserves_one_copy_and_requires_current_scope(tmp_path):
    session, _, files = _session(tmp_path)
    assert session.suggest_selection() == files[1:]
    with pytest.raises(ValueError, match="至少保留"):
        session.validate_selection(files)
    with pytest.raises(ValueError, match="先勾选"):
        session.validate_selection([])
    with pytest.raises(ValueError, match="不属于"):
        session.validate_selection([tmp_path / "foreign"])
    assert session.review_rows(files[1:])[0]["keeper"] == str(files[0])


def test_batch_move_is_real_exclusive_verified_and_retains_original_copy(tmp_path):
    session, _, files = _session(tmp_path)
    modified = {str(path): path.stat().st_mtime_ns for path in files}
    destination = tmp_path / "整理"
    destination.mkdir()
    conflict = destination / files[1].name
    conflict.write_text("已有内容不能覆盖", encoding="utf-8")
    result = session.process(files[1:], "move", destination)
    assert result.success_count == 2
    assert files[0].is_file()
    assert not files[1].exists() and not files[2].exists()
    assert conflict.read_text(encoding="utf-8") == "已有内容不能覆盖"
    assert Path(result.items[0].target).name == "副本1_2.txt"
    for outcome in result.items:
        assert Path(outcome.target).read_bytes() == files[0].read_bytes()
        assert Path(outcome.target).stat().st_mtime_ns == modified[outcome.source]
    assert not session.suggest_selection()
    with pytest.raises(ValueError, match="已经处理"):
        session.process([files[1]], "move", destination)
    with pytest.raises(ValueError, match="至少保留"):
        session.process([files[0]], "move", destination)


def test_original_directory_and_permanent_delete_are_refused(tmp_path):
    session, scope, files = _session(tmp_path)
    with pytest.raises(ValueError, match="原目录"):
        session.process([files[1]], "move", scope)
    with pytest.raises(ValueError, match="仅支持"):
        session.process([files[1]], "delete")
    assert all(path.exists() for path in files)


def test_changed_keeper_blocks_every_selected_operation(tmp_path):
    session, _, files = _session(tmp_path)
    files[0].write_text("保留项已编辑", encoding="utf-8")
    destination = tmp_path / "整理"
    destination.mkdir()
    result = session.process(files[1:], "move", destination)
    assert result.success_count == 0
    assert len(result.items) == 2
    assert all("保留副本" in row.message for row in result.items)
    assert all(path.exists() for path in files)
    assert not list(destination.iterdir())


def test_changed_selected_file_fails_while_other_valid_selection_succeeds(tmp_path):
    session, _, files = _session(tmp_path)
    files[1].write_text("编辑后的内容", encoding="utf-8")
    destination = tmp_path / "整理"
    destination.mkdir()
    result = session.process(files[1:], "move", destination)
    assert [item.status for item in result.items] == ["failed", "moved"]
    assert files[0].exists() and files[1].exists() and not files[2].exists()
    assert files[1].read_text(encoding="utf-8") == "编辑后的内容"


def test_same_size_edit_with_restored_mtime_is_rejected_by_content(tmp_path):
    session, _, files = _session(tmp_path)
    before = files[1].stat()
    edited = b"x" * before.st_size
    files[1].write_bytes(edited)
    os.utime(files[1], ns=(before.st_atime_ns, before.st_mtime_ns))
    destination = tmp_path / "整理"
    destination.mkdir()
    result = session.process([files[1]], "move", destination)
    assert result.success_count == 0
    assert result.items[0].status == "failed"
    assert files[1].read_bytes() == edited
    assert all(path.exists() for path in files)
    assert not list(destination.iterdir())


def test_replacement_with_identical_bytes_and_mtime_is_rejected_by_file_id(tmp_path):
    session, _, files = _session(tmp_path)
    before = files[1].stat()
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(files[1].read_bytes())
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(files[1])
    destination = tmp_path / "整理"
    destination.mkdir()
    result = session.process([files[1]], "move", destination)
    assert result.success_count == 0
    assert result.items[0].status == "failed"
    assert all(path.exists() for path in files)
    assert not list(destination.iterdir())


def test_copy_timestamp_update_does_not_follow_a_destination_path(tmp_path):
    session, _, files = _session(tmp_path)
    destination = tmp_path / "整理"
    destination.mkdir()
    original_utime = os.utime

    def handle_only(value, *args, **kwargs):
        assert isinstance(value, int), "timestamp writes must use the owned file handle"
        return original_utime(value, *args, **kwargs)

    with patch.object(duplicate.os, "utime", side_effect=handle_only):
        result = session.process([files[1]], "move", destination)
    assert result.success_count == 1
    assert not files[1].exists()
    assert Path(result.items[0].target).read_bytes() == files[0].read_bytes()


def test_hard_link_added_after_scan_prevents_operation(tmp_path):
    session, _, files = _session(tmp_path)
    try:
        os.link(files[1], tmp_path / "alias")
    except OSError as exc:
        pytest.skip(f"当前文件系统不可创建硬链接：{exc}")
    destination = tmp_path / "整理"
    destination.mkdir()
    result = session.process([files[1]], "move", destination)
    assert result.items[0].status == "failed"
    assert "硬链接" in result.items[0].message
    assert files[1].exists()


def test_missing_or_unsupported_trash_never_deletes(tmp_path):
    session, _, files = _session(tmp_path)
    with pytest.raises(ValueError, match="不支持"):
        session.process([files[1]], "trash")
    result = session.process([files[1]], "trash", trash=lambda path: (False, ""))
    assert result.items[0].status == "failed"
    assert files[1].exists()


def test_trash_success_uses_injected_system_operation_and_retains_keeper(tmp_path):
    session, _, files = _session(tmp_path)
    recycle = tmp_path / "模拟系统回收站"
    recycle.mkdir()

    def fake_trash(value):
        source = Path(value)
        target = recycle / source.name
        source.rename(target)
        return True, str(target)

    result = session.process(files[1:], "trash", trash=fake_trash)
    assert result.success_count == 2
    assert all(item.status == "trashed" for item in result.items)
    assert files[0].exists()
    assert len(list(recycle.iterdir())) == 2


def test_false_trash_success_is_not_reported_as_processed(tmp_path):
    session, _, files = _session(tmp_path)
    result = session.process([files[1]], "trash", trash=lambda value: (True, ""))
    assert result.success_count == 0
    assert "原路径仍有文件" in result.items[0].message
    assert files[1].exists()


def test_cancel_after_first_batch_item_retains_completed_and_remaining(tmp_path):
    session, _, files = _session(tmp_path)
    destination = tmp_path / "整理"
    destination.mkdir()
    stop = threading.Event()
    result = session.process(files[1:], "move", destination,
                             progress=lambda d, t, m: stop.set(), cancel=stop.is_set)
    assert result.cancelled and result.success_count == 1
    assert files[0].exists() and files[2].exists()
    assert not files[1].exists()


def test_cancel_during_copy_removes_only_own_partial_destination(tmp_path):
    session, _, files = _session(tmp_path)
    destination = tmp_path / "整理"
    destination.mkdir()
    existing = destination / "already.txt"
    existing.write_text("must remain", encoding="utf-8")
    original_fsync = duplicate.os.fsync
    stop = threading.Event()

    def stop_after_copy(descriptor):
        original_fsync(descriptor)
        stop.set()

    with patch.object(duplicate.os, "fsync", side_effect=stop_after_copy):
        result = session.process([files[1]], "move", destination, cancel=stop.is_set)
    assert result.cancelled
    assert all(path.exists() for path in files)
    assert list(destination.iterdir()) == [existing]


def test_partial_failure_keeps_verified_copy_and_explains_it(tmp_path):
    session, _, files = _session(tmp_path)
    destination = tmp_path / "整理"
    destination.mkdir()
    unlink = Path.unlink

    def deny_source(value, *args, **kwargs):
        if value == files[1]:
            raise PermissionError("文件被占用")
        return unlink(value, *args, **kwargs)

    with patch.object(Path, "unlink", deny_source):
        result = session.process([files[1]], "move", destination)
    assert result.success_count == 0
    assert "副本已保留" in result.items[0].message
    assert Path(result.items[0].target).read_bytes() == files[1].read_bytes()
    assert all(path.exists() for path in files)


def test_modified_keeper_between_copy_and_unlink_stops_source_removal(tmp_path):
    session, _, files = _session(tmp_path)
    destination = tmp_path / "整理"
    destination.mkdir()
    copy = duplicate._copy_exclusive

    def mutate_keeper(*args):
        target, identity = copy(*args)
        files[0].write_text("变化了", encoding="utf-8")
        return target, identity

    with patch.object(duplicate, "_copy_exclusive", side_effect=mutate_keeper):
        result = session.process([files[1]], "move", destination)
    assert result.items[0].status == "failed"
    assert files[1].exists()
