"""Rule-memory persistence, literal fidelity and failed-write guarantees."""
import json
from pathlib import Path

import pytest

from qingjian import redaction_memory as memory
from qingjian.redaction_memory import RedactionMemoryStore, normalize_rules


def sample_rules(count=1):
    return [{"find": f"原文={index}\n 保留空格 ", "replace": f"替换={index}\n "} for index in range(count)]


def payload(groups=None, restore_id=""):
    return {"schema_version": 1, "groups": groups or [], "restore_id": restore_id}


def valid_group(name="常用规则", group_id="a"):
    return {"id": group_id, "name": name, "created": "2026-10-29T10:30:00+08:00",
            "updated": "2026-10-29T10:30:00+08:00", "rules": sample_rules()}


def write_payload(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_normalize_preserves_literal_text_and_drops_unrelated_fields():
    rules = [{"find": "  A=B\n甲=乙\t ", "replace": " 替换=正文\n ", "unused": "private"},
             {"find": " ", "replace": ""}]
    result = normalize_rules(rules)
    assert result == [{"find": "  A=B\n甲=乙\t ", "replace": " 替换=正文\n "},
                      {"find": " ", "replace": ""}]
    result[0]["find"] = "改动"
    assert rules[0]["find"] == "  A=B\n甲=乙\t "
    assert normalize_rules([]) == []


@pytest.mark.parametrize("rules", [
    None, {}, (), "敏感正文", [None], ["敏感正文"], [{"replace": "替换"}],
    [{"find": "", "replace": "x"}], [{"find": None, "replace": "x"}],
    [{"find": True, "replace": "x"}], [{"find": "敏感正文"}],
    [{"find": "敏感正文", "replace": None}], [{"find": "敏感正文", "replace": False}],
    [{"find": "\ud800", "replace": ""}], [{"find": "敏感正文", "replace": "\ud800"}],
])
def test_invalid_rules_are_rejected_without_echoing_contents(rules):
    with pytest.raises(ValueError) as error:
        normalize_rules(rules)
    assert "敏感正文" not in str(error.value)


def test_rule_limits_allow_thousands_and_reject_excess():
    rules = sample_rules(memory.MAX_RULES)
    assert len(normalize_rules(rules)) == 5000
    assert len(RedactionMemoryStore().save_group("五千条", rules)["rules"]) == 5000
    with pytest.raises(ValueError, match="5000"):
        normalize_rules(rules + sample_rules())


def test_single_rule_text_limit_is_checked(monkeypatch):
    monkeypatch.setattr(memory, "MAX_TEXT_LENGTH", 3)
    with pytest.raises(ValueError):
        normalize_rules([{"find": "甲乙丙丁", "replace": ""}])
    with pytest.raises(ValueError):
        normalize_rules([{"find": "甲", "replace": "甲乙丙丁"}])


def test_memory_only_store_never_creates_files(monkeypatch):
    monkeypatch.setattr(memory.tempfile, "mkstemp", lambda **kwargs: pytest.fail("memory-only store wrote a file"))
    store = RedactionMemoryStore()
    group = store.save_group("内存规则", sample_rules())
    store.set_restore_group(group["id"])
    assert store.restore_id == group["id"]
    assert store.last_error == ""


def test_hundreds_of_literal_rules_and_default_survive_a_restart(tmp_path):
    path = tmp_path / "new" / "redaction-memory.json"
    store = RedactionMemoryStore(path)
    rules = sample_rules(480) + [{"find": "待删除", "replace": ""}]
    first = store.save_group("公司名称与人员", rules)
    assert store.restore_id == ""  # Remembering alone does not opt in to restoring.
    store.set_restore_group(first["id"])
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["restore_id"] == first["id"]
    reloaded = RedactionMemoryStore(path)
    assert not reloaded.last_error
    assert reloaded.restore_id == first["id"]
    assert reloaded.find_group(first["id"])["rules"] == rules


def test_all_public_rule_values_are_detached():
    store = RedactionMemoryStore()
    rules = sample_rules()
    original = sample_rules()
    group = store.save_group("独立副本", rules)
    rules[0]["find"] = "改变输入"
    group["rules"][0]["replace"] = "改变返回值"
    store.groups[0]["rules"][0]["find"] = "改变列表"
    store.find_group(group["id"])["rules"][0]["find"] = "改变查询"
    assert store.groups[0]["rules"] == original
    assert store.find_group("missing") is None


def test_rename_and_rule_update_keep_identity_creation_and_default(tmp_path, monkeypatch):
    dates = iter(["2026-10-29T10:30:00+08:00", "2026-10-29T10:31:00+08:00"])
    monkeypatch.setattr(memory, "_now", lambda: next(dates))
    path = tmp_path / "memory.json"
    store = RedactionMemoryStore(path)
    group = store.save_group("原名称", sample_rules())
    store.set_restore_group(group["id"])
    changed = store.save_group(" 新名称 ", sample_rules(100), group["id"])
    assert changed["id"] == group["id"]
    assert changed["created"] == group["created"]
    assert changed["updated"] != group["updated"]
    assert changed["name"] == "新名称"
    assert len(store.groups) == 1
    assert store.restore_id == group["id"]
    assert RedactionMemoryStore(path).groups == store.groups


@pytest.mark.parametrize("name", ["", "  ", None, 42, "甲" * 81, "坏\x00名称", "\ud800"])
def test_invalid_group_names_are_rejected(name):
    with pytest.raises(ValueError):
        RedactionMemoryStore().save_group(name, sample_rules())


def test_empty_group_cannot_be_saved():
    with pytest.raises(ValueError, match="至少"):
        RedactionMemoryStore().save_group("空规则", [])


def test_duplicate_names_are_rejected_but_existing_group_can_keep_name():
    store = RedactionMemoryStore()
    first = store.save_group("A 规则", sample_rules())
    with pytest.raises(ValueError, match="同名"):
        store.save_group(" a 规则 ", sample_rules())
    store.save_group("A 规则", sample_rules(2), first["id"])
    other = store.save_group("另一组", sample_rules())
    with pytest.raises(ValueError, match="同名"):
        store.save_group("A 规则", sample_rules(), other["id"])
    assert len(store.groups) == 2


def test_updating_an_unknown_group_does_not_create_one():
    store = RedactionMemoryStore()
    for group_id in ("missing", "", 42):
        with pytest.raises(ValueError, match="不存在"):
            store.save_group("常用", sample_rules(), group_id)
    assert store.groups == []


def test_group_capacity_updates_and_deletions():
    store = RedactionMemoryStore()
    for index in range(memory.MAX_GROUPS):
        store.save_group(f"第 {index} 组", sample_rules())
    first = store.groups[0]
    with pytest.raises(ValueError, match="200"):
        store.save_group("溢出", sample_rules())
    store.save_group("更新首组", sample_rules(60), first["id"])
    assert len(store.groups) == 200
    assert store.delete_group(first["id"])
    assert not store.delete_group(first["id"])
    store.save_group("释放容量后新增", sample_rules())
    assert len(store.groups) == 200


def test_delete_only_removes_selected_group_and_clears_its_default(tmp_path):
    path = tmp_path / "memory.json"
    store = RedactionMemoryStore(path)
    first = store.save_group("待删除", sample_rules())
    other = store.save_group("保留", sample_rules(20))
    store.set_restore_group(first["id"])
    assert store.delete_group(first["id"])
    assert store.restore_id == ""
    assert store.groups == [other]
    assert RedactionMemoryStore(path).groups == [other]


def test_clear_default_and_invalid_default_do_not_alter_rules(tmp_path):
    store = RedactionMemoryStore(tmp_path / "memory.json")
    group = store.save_group("保留", sample_rules())
    store.set_restore_group(group["id"])
    for value in ("missing", None, 1):
        with pytest.raises(ValueError, match="不存在"):
            store.set_restore_group(value)
        assert store.restore_id == group["id"]
    store.set_restore_group("")
    assert store.restore_id == ""
    assert store.groups == [group]


@pytest.mark.parametrize("data", [
    [], {}, payload() | {"schema_version": 2}, payload() | {"schema_version": True},
    payload() | {"groups": {}}, payload() | {"restore_id": None},
    payload(restore_id="unknown"), payload([None]), payload([{"name": "secret"}]),
    payload([valid_group() | {"rules": [{"find": "secret", "replace": None}]}]),
    payload([valid_group() | {"rules": []}]), payload([valid_group() | {"created": "secret"}]),
    payload([valid_group() | {"id": "invalid/id"}]),
    payload([valid_group(), valid_group(name="第二组")]),
    payload([valid_group(), valid_group(group_id="b")]),
    payload([valid_group() | {"rules": sample_rules(memory.MAX_RULES + 1)}]),
])
def test_invalid_library_entries_block_overwrite_and_do_not_echo_content(tmp_path, data):
    path = tmp_path / "memory.json"
    write_payload(path, data)
    original = path.read_bytes()
    store = RedactionMemoryStore(path)
    assert store.groups == [] and store.restore_id == ""
    assert store.last_error and "secret" not in store.last_error
    for operation in (lambda: store.save_group("新规则", sample_rules()),
                      lambda: store.delete_group("anything"), lambda: store.set_restore_group("")):
        with pytest.raises(ValueError, match="原文件已保留"):
            operation()
        assert path.read_bytes() == original


@pytest.mark.parametrize("encoded", [b'{"secret"', b"\xff\xfe", b'{"schema_version":1,"groups":[],"restore_id":"","secret":NaN}',
                                    b'{"schema_version":1,"groups":[],"restore_id":"","restore_id":"a"}'])
def test_bad_json_is_preserved_without_exposing_its_text(tmp_path, encoded):
    path = tmp_path / "memory.json"
    path.write_bytes(encoded)
    store = RedactionMemoryStore(path)
    assert store.last_error and "secret" not in store.last_error
    with pytest.raises(ValueError):
        store.save_group("新规则", sample_rules())
    assert path.read_bytes() == encoded


def test_load_failure_is_per_store_and_recovery_requires_reopen(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text("damaged", encoding="utf-8")
    failed = RedactionMemoryStore(path)
    write_payload(path, payload())
    with pytest.raises(ValueError):
        failed.save_group("仍应阻止", sample_rules())
    repaired = RedactionMemoryStore(path)
    assert repaired.last_error == ""
    repaired.save_group("修复后重开", sample_rules())
    assert len(repaired.groups) == 1


def test_reject_oversized_file_and_in_memory_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MAX_FILE_SIZE", 400)
    path = tmp_path / "memory.json"
    store = RedactionMemoryStore(path)
    group = store.save_group("小组", [{"find": "甲", "replace": "乙"}])
    original = path.read_bytes()
    with pytest.raises(ValueError, match="10 MB"):
        store.save_group("大组", sample_rules(10))
    assert store.groups == [group]
    assert path.read_bytes() == original
    path.write_bytes(b"x" * 401)
    blocked = RedactionMemoryStore(path)
    assert blocked.last_error
    assert path.stat().st_size == 401


@pytest.mark.parametrize("operation", ["create", "update", "delete", "restore", "clear_restore"])
def test_atomic_replace_failure_rolls_back_all_in_memory_state(tmp_path, monkeypatch, operation):
    path = tmp_path / "memory.json"
    store = RedactionMemoryStore(path)
    first = store.save_group("已有规则", sample_rules())
    second = store.save_group("第二组", sample_rules(50))
    store.set_restore_group(first["id"])
    groups, restore_id, original = store.groups, store.restore_id, path.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("secret sensitive find/replace contents")

    monkeypatch.setattr(memory.os, "replace", fail)
    operations = {"create": lambda: store.save_group("新规则", sample_rules()),
                  "update": lambda: store.save_group("改名", sample_rules(200), first["id"]),
                  "delete": lambda: store.delete_group(first["id"]),
                  "restore": lambda: store.set_restore_group(second["id"]),
                  "clear_restore": lambda: store.set_restore_group("")}
    with pytest.raises(OSError, match="此次修改未保存") as error:
        operations[operation]()
    assert "secret" not in str(error.value) and "secret" not in store.last_error
    assert store.groups == groups and store.restore_id == restore_id
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".qingjian-redaction-*"))
    assert RedactionMemoryStore(path).groups == groups


def test_fsync_failure_preserves_old_file_and_cleans_staging_file(tmp_path, monkeypatch):
    path = tmp_path / "memory.json"
    store = RedactionMemoryStore(path)
    group = store.save_group("原规则", sample_rules())
    original = path.read_bytes()

    def fail(_descriptor):
        raise OSError("secret disk error")

    monkeypatch.setattr(memory.os, "fsync", fail)
    with pytest.raises(OSError):
        store.save_group("新增", sample_rules())
    assert store.groups == [group]
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".qingjian-redaction-*"))


def test_temporary_file_failure_does_not_claim_a_new_group_was_saved(tmp_path, monkeypatch):
    path = tmp_path / "memory.json"
    store = RedactionMemoryStore(path)

    def fail(**_kwargs):
        raise PermissionError("secret inaccessible directory")

    monkeypatch.setattr(memory.tempfile, "mkstemp", fail)
    with pytest.raises(OSError, match="保存失败") as error:
        store.save_group("敏感组名", sample_rules())
    assert "secret" not in str(error.value) and "敏感组名" not in str(error.value)
    assert not path.exists()
    assert store.groups == [] and store.restore_id == ""


def test_success_after_write_failure_clears_error(tmp_path, monkeypatch):
    store = RedactionMemoryStore(tmp_path / "memory.json")
    real_replace = memory.os.replace

    def fail(*_args, **_kwargs):
        raise OSError("temporary failure")

    monkeypatch.setattr(memory.os, "replace", fail)
    with pytest.raises(OSError):
        store.save_group("第一次", sample_rules())
    assert store.last_error
    monkeypatch.setattr(memory.os, "replace", real_replace)
    group = store.save_group("第二次", sample_rules())
    assert store.last_error == "" and store.groups == [group]


def test_nonexistent_delete_and_unchanged_default_do_not_write(tmp_path, monkeypatch):
    store = RedactionMemoryStore(tmp_path / "memory.json")
    group = store.save_group("组名", sample_rules())
    store.set_restore_group(group["id"])
    monkeypatch.setattr(memory.os, "replace", lambda *_args: pytest.fail("unnecessary write"))
    assert not store.delete_group("missing")
    store.set_restore_group(group["id"])


def test_store_does_not_touch_other_files_when_groups_are_deleted(tmp_path):
    document = tmp_path / "原始材料.docx"
    document.write_bytes(b"user document stays intact")
    store = RedactionMemoryStore(tmp_path / "memory.json")
    group = store.save_group("以文件路径为替换规则", [{"find": str(document), "replace": ""}])
    assert store.delete_group(group["id"])
    assert document.read_bytes() == b"user document stays intact"
