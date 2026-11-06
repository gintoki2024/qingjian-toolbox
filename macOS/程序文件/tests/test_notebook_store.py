import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from qingjian import notebook_store as module
from qingjian.notebook_store import NotebookStore

NOW = datetime(2026, 10, 29, 9, 0)


def data(**values):
    payload = {"title": "工作事项", "body": "会议材料", "date": "2026-10-29", "remind_at": "2026-10-29T10:00:00"}
    payload.update(values)
    return payload


def test_memory_only_and_public_values_are_detached():
    store = NotebookStore()
    payload = data(repeat={"kind": "weekly", "weekdays": [3]})
    task = store.upsert_task(payload, now=NOW)
    payload["repeat"]["weekdays"].append(1)
    task["title"] = "修改外部副本"
    tasks = store.tasks
    tasks[0]["repeat"]["weekdays"].clear()
    settings = store.settings
    settings["opacity"] = .4
    assert store.tasks[0]["title"] == "工作事项"
    assert store.tasks[0]["repeat"]["weekdays"] == [3]
    assert store.settings["opacity"] == .9
    assert store.path is None


def test_roundtrip_unicode_notes_and_settings(tmp_path):
    path = tmp_path / "个人记事" / "notebook.json"
    store = NotebookStore(path)
    task = store.upsert_task(data(body="中文 🗓️\n第二行", remind_at=None), now=NOW)
    store.update_settings({"geometry": [-100, 20, 460, 700], "always_on_top": True})
    loaded = NotebookStore(path)
    assert loaded.last_error == ""
    assert loaded.find_task(task["id"]) == task
    assert loaded.settings["geometry"] == [-100, 20, 460, 700]
    assert loaded.collect_due(NOW + timedelta(days=100)) == []


def test_lead_minutes_triggers_at_exact_boundary_and_not_repeated(tmp_path, monkeypatch):
    store = NotebookStore(tmp_path / "notes.json")
    task = store.upsert_task(data(lead_minutes=15), now=NOW)
    assert store.collect_due(NOW.replace(minute=44)) == []
    alerts = store.collect_due(NOW.replace(minute=45))
    assert len(alerts) == 1
    assert alerts[0]["due_at"] == "2026-10-29T10:00:00"
    monkeypatch.setattr(module.os, "replace", lambda *_: pytest.fail("unchanged tick rewrites file"))
    assert store.collect_due(NOW.replace(minute=46))[0]["id"] == alerts[0]["id"]
    assert store.find_task(task["id"])["next_due"] is None


def test_pending_survives_restart_and_snooze(tmp_path):
    path = tmp_path / "notes.json"
    store = NotebookStore(path)
    task = store.upsert_task(data(), now=NOW)
    alert = store.collect_due(NOW.replace(hour=10))[0]
    store.snooze(alert["id"], 10, NOW.replace(hour=10))
    reopened = NotebookStore(path)
    assert reopened.collect_due(NOW.replace(hour=10, minute=9)) == []
    again = reopened.collect_due(NOW.replace(hour=10, minute=10))[0]
    assert again["id"] == alert["id"]
    assert again["task"]["id"] == task["id"]
    again["task"]["title"] = "外部"
    assert reopened.find_task(task["id"])["title"] == "工作事项"


def test_acknowledge_once_completes_task_and_is_idempotent():
    store = NotebookStore()
    task = store.upsert_task(data(), now=NOW)
    alert = store.collect_due(NOW.replace(hour=10))[0]
    assert store.acknowledge(alert["id"], NOW.replace(hour=10))
    assert store.find_task(task["id"])["completed"]
    assert not store.acknowledge(alert["id"], NOW)
    assert store.collect_due(NOW + timedelta(days=365)) == []


def test_acknowledge_repeat_only_closes_current_occurrence():
    store = NotebookStore()
    task = store.upsert_task(data(repeat={"kind": "daily"}), now=NOW)
    alert = store.collect_due(NOW.replace(hour=10))[0]
    assert store.acknowledge(alert["id"], NOW.replace(hour=10))
    assert not store.find_task(task["id"])["completed"]
    assert store.find_task(task["id"])["next_due"] == "2026-10-30T10:00:00"
    assert store.collect_due(NOW.replace(hour=11)) == []
    assert len(store.collect_due(NOW.replace(hour=10) + timedelta(days=1))) == 1


def test_sleep_gap_coalesces_many_occurrences_into_one():
    store = NotebookStore()
    task = store.upsert_task(data(remind_at="2000-01-01T10:00:00", repeat={"kind": "interval", "unit": "minutes"}), now=NOW)
    alerts = store.collect_due(NOW)
    assert len(alerts) == 1
    assert alerts[0]["missed_count"] > 1000000
    assert store.find_task(task["id"])["next_due"] == "2026-10-29T09:01:00"
    pending = store.collect_due(NOW + timedelta(days=2))
    assert len(pending) == 1 and pending[0]["id"] == alerts[0]["id"]
    assert pending[0]["missed_count"] == alerts[0]["missed_count"] + 2880


def test_text_and_priority_edits_preserve_schedule_and_pending():
    store = NotebookStore()
    task = store.upsert_task(data(repeat={"kind": "daily"}), now=NOW)
    alert = store.collect_due(NOW.replace(hour=10))[0]
    next_due = store.find_task(task["id"])["next_due"]
    updated = store.upsert_task({"title": "改后标题", "body": "新增正文", "priority": "urgent"}, task["id"], NOW.replace(hour=11))
    assert updated["next_due"] == next_due
    pending = store.collect_due(NOW.replace(hour=11))
    assert pending[0]["id"] == alert["id"]
    assert pending[0]["task"]["title"] == "改后标题"
    store.acknowledge(alert["id"], NOW)
    store.upsert_task({"body": "再次编辑"}, task["id"], NOW)
    assert store.collect_due(NOW.replace(hour=11)) == []


def test_changing_date_or_repeat_resets_old_alert():
    store = NotebookStore()
    task = store.upsert_task(data(), now=NOW)
    store.collect_due(NOW.replace(hour=10))
    updated = store.upsert_task({"date": "2026-10-30", "remind_at": "2026-10-30T12:00:00", "repeat": {"kind": "daily"}}, task["id"], NOW)
    assert updated["next_due"] == "2026-10-30T12:00:00"
    assert store.pending_alerts == []


def test_complete_and_remove_clear_pending_and_stop_recurrence():
    store = NotebookStore()
    task = store.upsert_task(data(repeat={"kind": "daily"}), now=NOW)
    store.collect_due(NOW.replace(hour=10))
    store.complete_task(task["id"], now=NOW)
    assert store.pending_alerts == []
    assert store.collect_due(NOW + timedelta(days=10)) == []
    store.complete_task(task["id"], False, NOW)
    assert store.collect_due(NOW.replace(hour=10))
    assert store.remove_task(task["id"])
    assert not store.remove_task(task["id"])
    assert store.tasks == [] and store.pending_alerts == []


@pytest.mark.parametrize("operation", ["edit", "tick", "ack", "snooze", "settings", "remove", "complete"])
def test_failed_atomic_save_rolls_back_state_and_original(tmp_path, monkeypatch, operation):
    path = tmp_path / "notes.json"
    store = NotebookStore(path)
    task = store.upsert_task(data(repeat={"kind": "daily"}), now=NOW)
    alert = store.collect_due(NOW.replace(hour=10))[0]
    original, before = path.read_bytes(), copy.deepcopy(store._state)
    monkeypatch.setattr(module.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="未保存"):
        if operation == "edit":
            store.upsert_task({"title": "不应保留"}, task["id"], NOW)
        elif operation == "tick":
            store.collect_due(NOW + timedelta(days=1, hours=1))
        elif operation == "ack":
            store.acknowledge(alert["id"], NOW)
        elif operation == "snooze":
            store.snooze(alert["id"], 5, NOW)
        elif operation == "settings":
            store.update_settings({"opacity": .8})
        elif operation == "remove":
            store.remove_task(task["id"])
        else:
            store.complete_task(task["id"], now=NOW)
    assert path.read_bytes() == original
    assert store._state == before
    assert not list(tmp_path.glob(".notebook-*.tmp"))
    assert store.last_error


@pytest.mark.parametrize("content", [b"not json", b'{"schema":2,"tasks":[],"settings":{},"alerts":[]}', b'{"schema":1,"schema":1,"tasks":[],"settings":{},"alerts":[]}'])
def test_invalid_or_future_file_is_preserved_and_blocked(tmp_path, content):
    path = tmp_path / "notes.json"
    path.write_bytes(content)
    store = NotebookStore(path)
    assert store.last_error and store.tasks == []
    with pytest.raises(ValueError, match="原文件已保留"):
        store.upsert_task(data(), now=NOW)
    assert path.read_bytes() == content


def test_large_existing_file_and_large_edits_are_preserved(tmp_path, monkeypatch):
    path = tmp_path / "notes.json"
    path.write_bytes(b"x" * 101)
    monkeypatch.setattr(module, "MAX_FILE_SIZE", 100)
    store = NotebookStore(path)
    assert store.last_error and path.read_bytes() == b"x" * 101
    fresh = NotebookStore()
    with pytest.raises(ValueError, match="10 MB"):
        fresh.upsert_task(data(), now=NOW)
    assert fresh.tasks == []


@pytest.mark.parametrize("fields", [{"title": " "}, {"body": "\x00"}, {"priority": "wrong"}, {"date": "2026-2-30"},
    {"remind_at": "2026-10-29T10:00:00+08:00"}, {"lead_minutes": -1}, {"lead_minutes": True},
    {"completed": 1}, {"id": "injected"}, {"next_due": "2026-10-29T09:00:00"}, {"repeat": {"kind": "shell"}}])
def test_invalid_user_fields_do_not_mutate(fields):
    store = NotebookStore()
    with pytest.raises(ValueError):
        store.upsert_task(data(**fields), now=NOW)
    assert store.tasks == []


@pytest.mark.parametrize("settings", [{"opacity": True}, {"opacity": float("nan")}, {"opacity": .1}, {"always_on_top": 1},
    {"geometry": [0, 0, 0, 200]}, {"geometry": "bad"}, {"new_option": 1}])
def test_invalid_settings_do_not_mutate(settings):
    store = NotebookStore()
    with pytest.raises(ValueError):
        store.update_settings(settings)
    assert store.settings["opacity"] == .9


@pytest.mark.parametrize("minutes", [0, -1, True, 10081, "10"])
def test_invalid_snooze(minutes):
    store = NotebookStore()
    store.upsert_task(data(), now=NOW)
    alert = store.collect_due(NOW.replace(hour=10))[0]
    with pytest.raises(ValueError):
        store.snooze(alert["id"], minutes, NOW)
    assert store.pending_alerts[0]["snoozed_until"] is None


def test_capacity_and_unknown_ids(monkeypatch):
    monkeypatch.setattr(module, "MAX_TASKS", 2)
    store = NotebookStore()
    store.upsert_task(data(title="甲"), now=NOW)
    store.upsert_task(data(title="乙"), now=NOW)
    with pytest.raises(ValueError, match="2 个"):
        store.upsert_task(data(), now=NOW)
    with pytest.raises(ValueError):
        store.upsert_task({"title": "丙"}, "unknown", NOW)
    with pytest.raises(ValueError):
        store.complete_task("unknown")
    assert store.find_task("unknown") is None


def test_aware_clock_is_rejected():
    with pytest.raises(ValueError):
        NotebookStore().upsert_task(data(), now=NOW.replace(tzinfo=timezone.utc))


def test_file_tampering_of_internal_schedule_is_blocked(tmp_path):
    path = tmp_path / "notes.json"
    store = NotebookStore(path)
    store.upsert_task(data(repeat={"kind": "daily"}), now=NOW)
    content = json.loads(path.read_text(encoding="utf-8"))
    content["tasks"][0]["next_due"] = "2026-10-29T12:00:00"
    path.write_text(json.dumps(content), encoding="utf-8")
    assert NotebookStore(path).last_error


def test_legacy_settings_gain_split_defaults_without_rewriting_original(tmp_path):
    path = tmp_path / "legacy.json"
    previous = {"schema": 1, "tasks": [], "alerts": [], "settings": {
        "opacity": .85, "always_on_top": True, "start_with_toolbox": False,
        "geometry": [-200, 50, 440, 720],
    }}
    encoded = json.dumps(previous).encode("utf-8")
    path.write_bytes(encoded)
    store = NotebookStore(path)
    assert not store.last_error
    assert store.settings["layout_mode"] == "split"
    assert store.settings["calendar_geometry"] is None and store.settings["tasks_geometry"] is None
    assert store.settings["geometry"] == previous["settings"]["geometry"]
    assert store.settings["opacity"] == .85 and store.settings["always_on_top"] is True
    assert path.read_bytes() == encoded
    store.update_settings({"calendar_geometry": [10, 20, 340, 370]})
    assert NotebookStore(path).settings["geometry"] == [-200, 50, 440, 720]
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1


def test_split_geometry_roundtrip_and_copy_isolation(tmp_path):
    path = tmp_path / "layout.json"
    store = NotebookStore(path)
    calendar, tasks = [-800, 20, 340, 370], [100, -40, 360, 350]
    settings = store.update_settings({"layout_mode": "combined", "calendar_geometry": calendar,
                                      "tasks_geometry": tasks, "geometry": [20, 10, 360, 680]})
    calendar[0] = 500
    tasks[2] = 1
    settings["tasks_geometry"][0] = 999
    loaded = NotebookStore(path)
    assert loaded.settings["layout_mode"] == "combined"
    assert loaded.settings["calendar_geometry"] == [-800, 20, 340, 370]
    assert loaded.settings["tasks_geometry"] == [100, -40, 360, 350]
    assert loaded.settings["geometry"] == [20, 10, 360, 680]
    loaded.update_settings({"layout_mode": "split"})
    assert loaded.settings["geometry"] == [20, 10, 360, 680]


@pytest.mark.parametrize("key", ["geometry", "calendar_geometry", "tasks_geometry"])
def test_geometry_accepts_compact_windows_but_validates_size(key):
    store = NotebookStore()
    assert store.update_settings({key: [0, 0, 80, 90]})[key] == [0, 0, 80, 90]
    for invalid in ([0, 0, 79, 90], [0, 0, 90, 10001], [100001, 0, 100, 100], [0, 0, True, 100], "bad"):
        with pytest.raises(ValueError):
            store.update_settings({key: invalid})
    assert store.settings[key] == [0, 0, 80, 90]


@pytest.mark.parametrize("mode", ["bad", True, None, [], {}])
def test_layout_mode_rejects_unknown_values_without_mutating(mode):
    store = NotebookStore()
    with pytest.raises(ValueError):
        store.update_settings({"layout_mode": mode})
    assert store.settings["layout_mode"] == "split"


def test_idle_tick_uses_schedule_index_without_full_copy_or_date_parsing(monkeypatch):
    store = NotebookStore()
    store.upsert_task(data(body="long body" * 2000, remind_at="2030-01-01T10:00:00"), now=NOW)
    original_revision = store.revision
    monkeypatch.setattr(module.copy, "deepcopy", lambda *_: pytest.fail("idle tick deep-copied store data"))
    monkeypatch.setattr(module, "_time", lambda *_: pytest.fail("idle tick reparsed every schedule"))
    monkeypatch.setattr(store, "_commit", lambda *_: pytest.fail("idle tick wrote a transaction"))
    for minutes in range(30):
        assert store.collect_due(NOW + timedelta(minutes=minutes)) == []
    assert store.revision == original_revision


def test_pending_tick_copies_only_alerts_and_corresponding_tasks(monkeypatch):
    store = NotebookStore()
    task = store.upsert_task(data(), now=NOW)
    store.upsert_task(data(title="future", remind_at="2030-01-01T10:00:00"), now=NOW)
    store.collect_due(NOW.replace(hour=10))
    revision = store.revision
    original_copy, copied = module.copy.deepcopy, []
    def traced(value):
        copied.append(value)
        assert value is not store._state and value is not store._state["tasks"]
        return original_copy(value)
    monkeypatch.setattr(module.copy, "deepcopy", traced)
    alerts = store.collect_due(NOW.replace(hour=11))
    assert len(copied) == 2 and len(alerts) == 1
    alerts[0]["task"]["body"] = "outside change"
    alerts[0]["missed_count"] = 999
    assert store.find_task(task["id"])["body"] == "会议材料"
    assert store.pending_alerts[0]["missed_count"] == 0
    assert store.revision == revision


def test_successful_commits_invalidate_schedule_cache_and_increment_revision():
    store = NotebookStore()
    assert store.revision == 0
    later = store.upsert_task(data(remind_at="2030-01-01T10:00:00"), now=NOW)
    revision = store.revision
    assert store.collect_due(NOW.replace(hour=10)) == []
    earlier = store.upsert_task(data(title="earlier"), now=NOW)
    assert store.revision > revision
    assert store.collect_due(NOW.replace(hour=10))[0]["task_id"] == earlier["id"]
    store.remove_task(earlier["id"])
    assert store.find_task(earlier["id"]) is None
    assert store.find_task(later["id"])["remind_at"] == "2030-01-01T10:00:00"
    assert store.collect_due(NOW.replace(hour=10)) == []


def test_failed_commit_keeps_revision_task_index_and_schedule_cache(tmp_path, monkeypatch):
    store = NotebookStore(tmp_path / "indexed.json")
    task = store.upsert_task(data(), now=NOW)
    revision, trigger, indexed = store.revision, store._next_trigger, store._tasks_by_id
    original = store.path.read_bytes()
    with monkeypatch.context() as blocked:
        blocked.setattr(module.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("write failed")))
        with pytest.raises(OSError):
            store.collect_due(NOW.replace(hour=10))
        assert store.revision == revision and store._next_trigger == trigger
        assert store._tasks_by_id is indexed and store.path.read_bytes() == original
    assert store.collect_due(NOW.replace(hour=10))[0]["task_id"] == task["id"]
    assert store.revision > revision


def test_reloaded_store_indexes_next_trigger_and_preserves_recurrence(tmp_path):
    path = tmp_path / "repeat.json"
    store = NotebookStore(path)
    task = store.upsert_task(data(repeat={"kind": "daily"}, lead_minutes=15), now=NOW)
    store.collect_due(NOW.replace(minute=45))
    reopened = NotebookStore(path)
    assert reopened.revision > 0 and reopened.find_task(task["id"]) is not None
    old_alert = reopened.collect_due(NOW.replace(hour=10))[0]
    reopened.acknowledge(old_alert["id"], NOW.replace(hour=10))
    assert reopened.collect_due(NOW.replace(hour=9, minute=44) + timedelta(days=1)) == []
    assert reopened.collect_due(NOW.replace(hour=9, minute=45) + timedelta(days=1))[0]["due_at"] == "2026-10-30T10:00:00"
