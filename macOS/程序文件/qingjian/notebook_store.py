"""Atomic local notebook and durable reminders, independent of the Qt window."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from .reminder_schedule import first_occurrence, next_occurrence, normalize_repeat, occurrence_count

MAX_TASKS = 2000
MAX_FILE_SIZE = 10 * 1024 * 1024
DEFAULT_SETTINGS = {"opacity": .9, "always_on_top": False, "start_with_toolbox": True,
                    "layout_mode": "split", "geometry": None,
                    "calendar_geometry": None, "tasks_geometry": None}
TASK_FIELDS = frozenset({"title", "body", "priority", "date", "remind_at", "repeat", "lead_minutes", "completed"})
_INTERNAL_FIELDS = frozenset({"id", "created", "updated", "next_due"})
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


def _now(value: datetime | None = None) -> datetime:
    value = datetime.now() if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("请使用本机日期时间，不包含时区。")
    return value.replace(microsecond=0)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("提醒日期时间格式无效。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("提醒日期时间格式无效。") from None
    return _now(parsed)


def _text(value: object, maximum: int, name: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value or (required and not value.strip()):
        raise ValueError(f"{name}须为有效文字，最多 {maximum} 个字。")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name}中包含无效字符。") from None
    return value.strip() if required else value


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _clean_settings(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise ValueError("记事本设置包含不支持的选项。")
    result = copy.deepcopy(DEFAULT_SETTINGS)
    result.update(copy.deepcopy(value))
    opacity = result["opacity"]
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not math.isfinite(opacity) or not .35 <= opacity <= 1:
        raise ValueError("窗口透明度须在 35% 至 100% 之间。")
    for key in ("always_on_top", "start_with_toolbox"):
        if not isinstance(result[key], bool):
            raise ValueError("记事本开关设置无效。")
    if not isinstance(result["layout_mode"], str) or result["layout_mode"] not in {"split", "combined"}:
        raise ValueError("记事本布局须为拆分或合并模式。")
    for key in ("geometry", "calendar_geometry", "tasks_geometry"):
        geometry = result[key]
        if geometry is not None:
            if not isinstance(geometry, list) or len(geometry) != 4 or any(isinstance(number, bool) or not isinstance(number, int) for number in geometry):
                raise ValueError("窗口位置设置无效。")
            if any(abs(number) > 100000 for number in geometry[:2]) or not all(80 <= number <= 10000 for number in geometry[2:]):
                raise ValueError("窗口位置或尺寸超出范围。")
    return result


def _user_task(payload: object, previous: dict | None, now: datetime) -> dict:
    if not isinstance(payload, dict) or set(payload) - TASK_FIELDS:
        raise ValueError("事项中包含不支持的字段。")
    result = {"title": "", "body": "", "priority": "normal", "date": now.date().isoformat(),
              "remind_at": None, "repeat": {"kind": "once", "interval": 1}, "lead_minutes": 0, "completed": False}
    if previous:
        result.update({key: copy.deepcopy(previous[key]) for key in TASK_FIELDS})
    result.update(copy.deepcopy(payload))
    result["title"] = _text(result["title"], 200, "事项标题", required=True)
    result["body"] = _text(result["body"], 100000, "事项正文")
    if result["priority"] not in {"low", "normal", "important", "urgent"}:
        raise ValueError("事项重要程度无效。")
    try:
        if not isinstance(result["date"], str) or date.fromisoformat(result["date"]).isoformat() != result["date"]:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("事项日期须为 YYYY-MM-DD 格式。") from None
    if result["remind_at"] is not None:
        result["remind_at"] = _stamp(_time(result["remind_at"]))
    result["repeat"] = normalize_repeat(result["repeat"])
    lead = result["lead_minutes"]
    if isinstance(lead, bool) or not isinstance(lead, int) or not 0 <= lead <= 10080:
        raise ValueError("提前提醒须为 0 至 10080 分钟。")
    if not isinstance(result["completed"], bool):
        raise ValueError("事项完成状态无效。")
    return result


def _initial_due(task: dict) -> str | None:
    if task["completed"] or task["remind_at"] is None:
        return None
    value = first_occurrence(_time(task["remind_at"]), task["repeat"])
    return _stamp(value) if value is not None else None


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("文件包含重复字段。")
        value[key] = item
    return value


def _reject_constant(_):
    raise ValueError("文件包含无效数值。")


class NotebookStore:
    """One JSON file; failed writes never advance the in-memory schedule.

    ``path=None`` deliberately keeps all data in memory. A damaged existing file
    is left intact and write-protected until the caller repairs it and reopens.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self.last_error = ""
        self._load_failed = False
        self._state = {"schema": 1, "tasks": [], "settings": copy.deepcopy(DEFAULT_SETTINGS), "alerts": []}
        self._revision = 0
        self._tasks_by_id = {}
        self._next_trigger = None
        if self.path is not None:
            self._load()

    @property
    def revision(self) -> int:
        """Cheap state token: changes only after a successful load or commit."""
        return self._revision

    @property
    def tasks(self) -> list[dict]:
        return copy.deepcopy(self._state["tasks"])

    @property
    def settings(self) -> dict:
        return copy.deepcopy(self._state["settings"])

    @property
    def pending_alerts(self) -> list[dict]:
        return self._alerts(self._state)

    def find_task(self, task_id: str) -> dict | None:
        task = self._tasks_by_id.get(task_id)
        return copy.deepcopy(task) if task is not None else None

    def _index_state(self) -> None:
        """Cache immutable transaction results, never uncommitted draft state."""
        self._tasks_by_id = {task["id"]: task for task in self._state["tasks"]}
        earliest = None
        for task in self._state["tasks"]:
            if task["completed"] or task["next_due"] is None:
                continue
            try:
                trigger = _time(task["next_due"]) - timedelta(minutes=task["lead_minutes"])
            except OverflowError:
                trigger = datetime.min
            if earliest is None or trigger < earliest:
                earliest = trigger
        self._next_trigger = earliest
        self._revision += 1

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > MAX_FILE_SIZE:
                raise ValueError
            state = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json, parse_constant=_reject_constant)
            if not isinstance(state, dict) or set(state) != {"schema", "tasks", "settings", "alerts"} or type(state["schema"]) is not int or state["schema"] != 1:
                raise ValueError
            tasks, alerts = state["tasks"], state["alerts"]
            if not isinstance(tasks, list) or len(tasks) > MAX_TASKS or not isinstance(alerts, list) or len(alerts) > MAX_TASKS:
                raise ValueError
            ids = set()
            for task in tasks:
                if not isinstance(task, dict) or set(task) != TASK_FIELDS | _INTERNAL_FIELDS or not _valid_id(task["id"]) or task["id"] in ids:
                    raise ValueError
                _user_task({key: task[key] for key in TASK_FIELDS}, None, datetime.now())
                _time(task["created"])
                _time(task["updated"])
                if task["next_due"] is not None:
                    due = _time(task["next_due"])
                    if task["completed"] or task["remind_at"] is None or occurrence_count(_time(task["remind_at"]), task["repeat"], due) <= occurrence_count(_time(task["remind_at"]), task["repeat"], due - timedelta(seconds=1)):
                        raise ValueError
                ids.add(task["id"])
            alert_ids, alerted_tasks = set(), set()
            for alert in alerts:
                if not isinstance(alert, dict) or set(alert) != {"id", "task_id", "due_at", "created_at", "snoozed_until", "missed_count"}:
                    raise ValueError
                if not _valid_id(alert["id"]) or alert["id"] in alert_ids or alert["task_id"] not in ids or alert["task_id"] in alerted_tasks:
                    raise ValueError
                _time(alert["due_at"])
                _time(alert["created_at"])
                if alert["snoozed_until"] is not None:
                    _time(alert["snoozed_until"])
                if type(alert["missed_count"]) is not int or alert["missed_count"] < 0:
                    raise ValueError
                if next(task for task in tasks if task["id"] == alert["task_id"])["completed"]:
                    raise ValueError
                alert_ids.add(alert["id"])
                alerted_tasks.add(alert["task_id"])
            state["settings"] = _clean_settings(state["settings"])
            self._state = state
            self._index_state()
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            self._load_failed = True
            self.last_error = "无法读取记事本：文件格式无效、超过大小限制或无法访问。原文件已保留，请先备份并修复，再重新打开工具。"

    def _commit(self, state: dict) -> None:
        if self._load_failed:
            raise ValueError(self.last_error)
        staged = None
        try:
            encoded = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_FILE_SIZE:
                raise ValueError("记事本超过 10 MB，请精简正文或删除不再需要的事项。")
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="wb", prefix=".notebook-", suffix=".tmp", dir=self.path.parent, delete=False) as stream:
                    staged = Path(stream.name)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(staged, self.path)
        except OSError:
            self.last_error = "记事本保存失败，请检查保存位置及磁盘空间。此次修改未保存。"
            raise OSError(self.last_error) from None
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass
        self._state = state
        self._index_state()
        self.last_error = ""

    def upsert_task(self, payload: dict, task_id: str | None = None, now: datetime | None = None) -> dict:
        moment = _now(now)
        previous = self.find_task(task_id) if task_id is not None else None
        if task_id is not None and previous is None:
            raise ValueError("该事项已不存在，请刷新后重试。")
        if previous is None and len(self._state["tasks"]) >= MAX_TASKS:
            raise ValueError(f"记事本最多保存 {MAX_TASKS} 个事项。")
        task = _user_task(payload, previous, moment)
        task.update({"id": previous["id"] if previous else uuid.uuid4().hex,
                     "created": previous["created"] if previous else _stamp(moment), "updated": _stamp(moment)})
        schedule_changed = previous is None or any(previous[key] != task[key] for key in ("date", "remind_at", "repeat", "lead_minutes", "completed"))
        task["next_due"] = _initial_due(task) if schedule_changed else previous["next_due"]
        state = copy.deepcopy(self._state)
        if previous is None:
            state["tasks"].append(task)
        else:
            state["tasks"] = [task if item["id"] == task["id"] else item for item in state["tasks"]]
        if schedule_changed:
            state["alerts"] = [alert for alert in state["alerts"] if alert["task_id"] != task["id"]]
        self._commit(state)
        return copy.deepcopy(task)

    def remove_task(self, task_id: str) -> bool:
        if self.find_task(task_id) is None:
            return False
        state = copy.deepcopy(self._state)
        state["tasks"] = [task for task in state["tasks"] if task["id"] != task_id]
        state["alerts"] = [alert for alert in state["alerts"] if alert["task_id"] != task_id]
        self._commit(state)
        return True

    def complete_task(self, task_id: str, completed: bool = True, now: datetime | None = None) -> dict:
        task = self.find_task(task_id)
        if task is None:
            raise ValueError("该事项已不存在。")
        if type(completed) is not bool:
            raise ValueError("事项完成状态无效。")
        if task["completed"] == completed:
            return task
        return self.upsert_task({"completed": completed}, task_id, now)

    def _alerts(self, state: dict, now: datetime | None = None) -> list[dict]:
        if not state["alerts"]:
            return []
        tasks = self._tasks_by_id if state is self._state else {task["id"]: task for task in state["tasks"]}
        result = []
        for alert in state["alerts"]:
            if now is not None and alert["snoozed_until"] is not None and _time(alert["snoozed_until"]) > now:
                continue
            item = copy.deepcopy(alert)
            item["task"] = copy.deepcopy(tasks[alert["task_id"]])
            result.append(item)
        return sorted(result, key=lambda alert: (alert["due_at"], alert["created_at"], alert["id"]))

    def collect_due(self, now: datetime | None = None) -> list[dict]:
        moment = _now(now)
        # Most one-second ticks change nothing. The transaction index lets them
        # avoid copying every task, parsing every schedule or touching the disk.
        if self._next_trigger is None or moment < self._next_trigger:
            return self._alerts(self._state, moment)
        state = copy.deepcopy(self._state)
        pending = {alert["task_id"]: alert for alert in state["alerts"]}
        changed = False
        for task in state["tasks"]:
            if task["completed"] or task["next_due"] is None:
                continue
            try:
                boundary = moment + timedelta(minutes=task["lead_minutes"])
            except OverflowError:
                boundary = datetime.max
            due = _time(task["next_due"])
            if due > boundary:
                continue
            anchor = _time(task["remind_at"])
            count = occurrence_count(anchor, task["repeat"], boundary) - occurrence_count(anchor, task["repeat"], due - timedelta(seconds=1))
            alert = pending.get(task["id"])
            if alert is None:
                alert = {"id": uuid.uuid4().hex, "task_id": task["id"], "due_at": _stamp(due), "created_at": _stamp(moment), "snoozed_until": None, "missed_count": max(0, count - 1)}
                state["alerts"].append(alert)
                pending[task["id"]] = alert
            else:
                alert["missed_count"] += max(1, count)
            future = next_occurrence(anchor, task["repeat"], boundary)
            task["next_due"] = _stamp(future) if future is not None else None
            changed = True
        if changed:
            self._commit(state)
        return self._alerts(self._state, moment)

    def acknowledge(self, alert_id: str, now: datetime | None = None) -> bool:
        moment = _now(now)
        alert = next((item for item in self._state["alerts"] if item["id"] == alert_id), None)
        if alert is None:
            return False
        state = copy.deepcopy(self._state)
        state["alerts"] = [item for item in state["alerts"] if item["id"] != alert_id]
        task = next(task for task in state["tasks"] if task["id"] == alert["task_id"])
        if task["repeat"]["kind"] == "once":
            task["completed"], task["next_due"], task["updated"] = True, None, _stamp(moment)
        self._commit(state)
        return True

    def snooze(self, alert_id: str, minutes: int, now: datetime | None = None) -> dict:
        moment = _now(now)
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 10080:
            raise ValueError("稍后提醒须为 1 至 10080 分钟。")
        state = copy.deepcopy(self._state)
        alert = next((item for item in state["alerts"] if item["id"] == alert_id), None)
        if alert is None:
            raise ValueError("该提醒已关闭。")
        try:
            alert["snoozed_until"] = _stamp(moment + timedelta(minutes=minutes))
        except OverflowError:
            raise ValueError("稍后提醒时间超出日期范围。") from None
        self._commit(state)
        return next(item for item in self._alerts(state) if item["id"] == alert_id)

    def update_settings(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ValueError("记事本设置无效。")
        state = copy.deepcopy(self._state)
        settings = copy.deepcopy(state["settings"])
        settings.update(copy.deepcopy(values))
        state["settings"] = _clean_settings(settings)
        self._commit(state)
        return self.settings
