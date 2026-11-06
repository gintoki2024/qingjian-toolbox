"""Small local stores for explicit presets and non-content task history."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from .catalog import EXTRA_ORDER
from .tools import scrub_secrets

PRESET_MODES = frozenset({"word", "web", "convert", "image", *EXTRA_ORDER})
HISTORY_MODES = PRESET_MODES | {"undo_rename"}
HISTORY_STATUSES = frozenset({"完成", "部分完成", "失败", "已停止"})
MAX_PRESETS, MAX_HISTORY = 20, 30
MAX_FILE_SIZE = 5 * 1024 * 1024

_TRANSIENT_KEYS = frozenset({
    "privateplan", "plan", "sources", "source", "sourcepath", "sourcepaths",
    "inputs", "inputfile", "inputfiles", "inputpath", "inputpaths", "files", "queue", "filequeue", "selectedfiles",
    "documenttext", "fulltext", "body", "records", "rawcontent", "documentcontent",
})
_SECRET_KEYS = frozenset({
    "secret", "clientsecret", "apikey", "accesstoken", "refreshtoken", "token",
    "authorization", "authheader", "cookie", "cookies", "credential", "credentials",
    "privatekey", "accesskey", "sessiontoken",
})


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _string(value, limit=32768):
    return isinstance(value, str) and len(value) <= limit and "\x00" not in value


def _plain_json(value, depth=0, budget=None):
    """Reject unexpected objects and pathological trees before copying them."""
    if budget is None:
        budget = [50000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 32:
        raise ValueError("常用方案内容过多或层级过深。")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 100000:
            raise ValueError("常用方案中的文字过长。")
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_plain_json(item, depth + 1, budget) for item in value]
    if isinstance(value, dict) and all(_string(key, 100) for key in value):
        return {key: _plain_json(item, depth + 1, budget) for key, item in value.items()}
    raise ValueError("常用方案包含不支持的设置内容。")


def _strip_credentials(value):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _TRANSIENT_KEYS:
                continue
            if "password" in normalized or normalized in _SECRET_KEYS:
                clean[key] = ""
            else:
                clean[key] = _strip_credentials(item)
        return clean
    if isinstance(value, list):
        return [_strip_credentials(item) for item in value]
    return value


def clean_preset_options(options):
    """Retain user-authored processing rules, never a password or file queue."""
    if not isinstance(options, dict):
        raise ValueError("常用方案的设置格式无效。")
    clean = _strip_credentials(scrub_secrets(_plain_json(options)))
    if len(json.dumps(clean, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
        raise ValueError("常用方案超过保存大小，请减少处理规则。")
    return clean


def task_records(result):
    values = result.get("records", []) if isinstance(result, dict) else []
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def task_outputs(result):
    """Collect unique output and audit paths without reading their contents."""
    result = result if isinstance(result, dict) else {}
    outputs, seen = [], set()
    for record in task_records(result):
        values = [record.get("输出", "")]
        reports = record.get("检查报告", [])
        if isinstance(reports, list):
            values.extend(reports)
        for value in values:
            if not _string(value) or not value.strip():
                continue
            key = os.path.normcase(value)
            if key not in seen:
                seen.add(key)
                outputs.append(value)
    return outputs


def task_counts(result):
    """Count produced paths and exceptions, rather than claiming input success."""
    result = result if isinstance(result, dict) else {}
    records = task_records(result)
    failed = sum(item.get("状态") == "失败" for item in records)
    explicit = result.get("error_count", 0)
    explicit = explicit if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit >= 0 else 0
    failed = max(failed, explicit)
    fatal = result.get("fatal")
    if fatal and not any(item.get("状态") == "失败" and item.get("原因") == fatal for item in records):
        failed += 1
    if result.get("report_error"):
        failed += 1
    return len(task_outputs(result)), failed


def task_status(result):
    outputs, errors = task_counts(result)
    if result.get("cancelled"):
        return "已停止"
    if errors:
        return "部分完成" if outputs else "失败"
    return "完成"


def _valid_date(value):
    if not _string(value, 80):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


class ProfileStore:
    """JSON persistence is optional; absent paths keep an isolated session."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self.last_error = ""
        self._presets, self._history = [], []
        if self.path is not None:
            self._load()

    @property
    def presets(self):
        return copy.deepcopy(self._presets)

    @property
    def history(self):
        return copy.deepcopy(self._history)

    def _load(self):
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > MAX_FILE_SIZE:
                raise ValueError("本地方案文件超过大小限制。")
            def invalid_constant(value):
                raise ValueError("无效的数值：" + value)
            data = json.loads(self.path.read_text(encoding="utf-8"), parse_constant=invalid_constant)
            if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
                raise ValueError("本地方案文件版本无法识别。")
            if not isinstance(data.get("presets", []), list) or not isinstance(data.get("history", []), list):
                raise ValueError("本地方案文件结构无效。")
            skipped, seen = False, set()
            for item in data.get("presets", [])[:1000]:
                try:
                    value = self._read_preset(item)
                    if value["id"] in seen:
                        raise ValueError()
                    seen.add(value["id"])
                    if len(self._presets) < MAX_PRESETS:
                        self._presets.append(value)
                except (ValueError, TypeError, KeyError, RecursionError):
                    skipped = True
            seen = set()
            for item in data.get("history", [])[:1000]:
                try:
                    value = self._read_history(item)
                    if value["id"] in seen:
                        raise ValueError()
                    seen.add(value["id"])
                    if len(self._history) < MAX_HISTORY:
                        self._history.append(value)
                except (ValueError, TypeError, KeyError):
                    skipped = True
            if skipped:
                self.last_error = "部分本地方案或任务记录格式无效，已跳过。"
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            self._presets, self._history = [], []
            self.last_error = "无法读取本地常用方案与最近任务：" + str(exc)

    @staticmethod
    def _read_preset(item):
        if not isinstance(item, dict) or item.get("mode") not in PRESET_MODES:
            raise ValueError()
        if not _string(item.get("id"), 100) or not item["id"] or not _string(item.get("name"), 80) or not item["name"].strip():
            raise ValueError()
        if not _valid_date(item.get("created")) or not _string(item.get("scene", ""), 80):
            raise ValueError()
        return {"id": item["id"], "name": item["name"].strip(), "mode": item["mode"],
                "scene": item.get("scene", ""), "created": item["created"],
                "options": clean_preset_options(item.get("options"))}

    @staticmethod
    def _read_history(item):
        if not isinstance(item, dict) or item.get("mode") not in HISTORY_MODES:
            raise ValueError()
        if not _string(item.get("id"), 100) or not item["id"] or not _string(item.get("title"), 160):
            raise ValueError()
        if not _valid_date(item.get("created")) or item.get("status") not in HISTORY_STATUSES:
            raise ValueError()
        if not _string(item.get("scene", ""), 80) or not _string(item.get("folder", "")):
            raise ValueError()
        outputs = item.get("outputs", [])
        if not isinstance(outputs, list) or len(outputs) > 50000 or not all(_string(value) for value in outputs):
            raise ValueError()
        for key in ("output_count", "error_count"):
            if not isinstance(item.get(key), int) or isinstance(item[key], bool) or item[key] < 0:
                raise ValueError()
        return {key: copy.deepcopy(item[key]) for key in (
            "id", "title", "mode", "created", "status", "folder", "outputs", "output_count", "error_count"
        )} | {"scene": item.get("scene", "")}

    def _save(self):
        self.last_error = ""
        if self.path is None:
            return
        staged = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"schema_version": 1, "presets": self._presets, "history": self._history}
            encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_FILE_SIZE:
                raise ValueError("本地方案与任务记录过大，暂未保存到磁盘。")
            descriptor, name = tempfile.mkstemp(prefix=".qingjian-profile-", suffix=".tmp", dir=self.path.parent)
            staged = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, self.path)
        except (OSError, ValueError) as exc:
            self.last_error = "本地记录保存失败，当前会话仍可使用：" + str(exc)
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass

    def save_preset(self, name, mode, options, scene=""):
        if mode not in PRESET_MODES or not _string(name, 80) or not name.strip() or not _string(scene, 80):
            raise ValueError("请填写1至80字的方案名称，并选择有效工具。")
        value = {"id": uuid.uuid4().hex, "name": name.strip(), "mode": mode, "scene": scene,
                 "created": _now(), "options": clean_preset_options(options)}
        self._presets.insert(0, value)
        del self._presets[MAX_PRESETS:]
        self._save()
        return copy.deepcopy(value)

    def remove_preset(self, preset_id):
        count = len(self._presets)
        self._presets = [item for item in self._presets if item["id"] != preset_id]
        if len(self._presets) != count:
            self._save()
            return True
        return False

    def record_task(self, mode, title, result, scene=""):
        if mode not in HISTORY_MODES or not _string(title, 160) or not _string(scene, 80) or not isinstance(result, dict):
            raise ValueError("任务记录格式无效。")
        folder = result.get("folder", "")
        if not _string(folder):
            folder = ""
        outputs, errors = task_counts(result)
        value = {"id": uuid.uuid4().hex, "title": title, "mode": mode, "scene": scene,
                 "created": _now(), "status": task_status(result), "folder": folder,
                 "outputs": task_outputs(result), "output_count": outputs, "error_count": errors}
        self._history.insert(0, value)
        del self._history[MAX_HISTORY:]
        self._save()
        return copy.deepcopy(value)

    def clear_history(self):
        self._history = []
        self._save()
