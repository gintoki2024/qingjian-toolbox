"""Bounded, read-only home materials and independently saved display choices."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import random
import re
import tempfile

from .reminder_schedule import first_occurrence, next_occurrence

MODES = ("sequential", "random", "single")
DISPLAY_MODES = ("images", "quotes", "reminders")
DEFAULT_SETTINGS = {
    "folder": "", "show_images": True, "show_quotes": True, "show_reminders": True,
    "display_mode": "reminders",
    "image_mode": "sequential", "quote_mode": "sequential",
    "image_interval": 15, "quote_interval": 20,
    "fixed_image": "", "fixed_quote": "", "reminder_days": 7,
}
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".ico"})
MAX_DIRECTORY_ENTRIES = 4096
MAX_IMAGES, MAX_TEXT_FILES, MAX_QUOTES = 512, 20, 500
MAX_TEXT_BYTES, MAX_QUOTE_LENGTH = 512 * 1024, 8000
MAX_SETTINGS_BYTES = 64 * 1024


def clean_settings(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise ValueError("首页设置包含不支持的选项。")
    result = copy.deepcopy(DEFAULT_SETTINGS)
    result.update(copy.deepcopy(value))
    # Migrate v0.10.0's independent switches without losing its folder or
    # playback choices. Future saves remember the single selected page.
    if "display_mode" not in value and any(key in value for key in ("show_images", "show_quotes", "show_reminders")):
        if result["folder"] and result["show_images"]:
            result["display_mode"] = "images"
        elif result["folder"] and result["show_quotes"]:
            result["display_mode"] = "quotes"
    if result["display_mode"] not in DISPLAY_MODES:
        raise ValueError("首页内容须选择图片、文字或待办事项中的一项。")
    for key in ("folder", "fixed_image", "fixed_quote"):
        item = result[key]
        if not isinstance(item, str) or len(item) > 4096 or "\x00" in item:
            raise ValueError("素材文件夹或固定内容的设置无效。")
        try:
            item.encode("utf-8")
        except UnicodeError:
            raise ValueError("首页设置中包含无效字符。") from None
    for key in ("show_images", "show_quotes", "show_reminders"):
        if type(result[key]) is not bool:
            raise ValueError("首页显示开关无效。")
    for key in ("image_mode", "quote_mode"):
        if result[key] not in MODES:
            raise ValueError("轮播方式须为顺序循环、随机循环或单个不循环。")
    for key in ("image_interval", "quote_interval"):
        if type(result[key]) is not int or not 3 <= result[key] <= 3600:
            raise ValueError("轮播间隔须为 3 至 3600 秒。")
    if type(result["reminder_days"]) is not int or not 1 <= result["reminder_days"] <= 31:
        raise ValueError("近期事项范围须为 1 至 31 天。")
    return result


class HomeContentStore:
    """Only preferences are persisted. Existing malformed files stay untouched."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self.last_error = ""
        self._load_failed = False
        self._settings = copy.deepcopy(DEFAULT_SETTINGS)
        if self.path is None or not self.path.exists():
            return
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise ValueError
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"schema", "settings"} or type(payload["schema"]) is not int or payload["schema"] != 1:
                raise ValueError
            self._settings = clean_settings(payload["settings"])
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            self._load_failed = True
            self.last_error = "首页设置无法读取，原文件已保留。请备份并修复设置文件后重启；本次可继续浏览。"

    @property
    def settings(self) -> dict:
        return copy.deepcopy(self._settings)

    def update(self, values: dict) -> dict:
        if self._load_failed:
            raise ValueError(self.last_error)
        if not isinstance(values, dict):
            raise ValueError("首页设置无效。")
        result = clean_settings({**self._settings, **values})
        staged = None
        try:
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                data = json.dumps({"schema": 1, "settings": result}, ensure_ascii=False).encode("utf-8")
                with tempfile.NamedTemporaryFile("wb", prefix=".home-content-", suffix=".tmp", dir=self.path.parent, delete=False) as stream:
                    staged = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(staged, self.path)
        except OSError:
            self.last_error = "首页设置保存失败，请检查保存位置和剩余空间；本次修改未保存。"
            raise OSError(self.last_error) from None
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass
        self._settings = result
        self.last_error = ""
        return self.settings


@dataclass(frozen=True)
class QuoteItem:
    key: str
    text: str
    source: str


@dataclass(frozen=True)
class ContentSnapshot:
    images: tuple[Path, ...] = ()
    quotes: tuple[QuoteItem, ...] = ()
    warnings: tuple[str, ...] = ()
    folder: str = ""


def read_quotes(path: Path) -> list[QuoteItem]:
    """A blank line separates paragraphs; without blank lines, each line is one."""
    with path.open("rb") as stream:
        raw = stream.read(MAX_TEXT_BYTES + 1)
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError(f"{path.name} 超过 512 KB，已跳过。")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    elif b"\x00" in raw[:4096]:
        # BOM-less Windows UTF-16 is common. Choose byte order from ASCII zeros.
        even, odd = raw[:4096:2].count(0), raw[1:4096:2].count(0)
        encodings = ("utf-16-le", "utf-16-be") if odd >= even else ("utf-16-be", "utf-16-le")
    else:
        encodings = ("utf-8-sig", "gb18030")
    text = None
    for encoding in encodings:
        try:
            candidate = raw.decode(encoding)
            if "\x00" in candidate or any(ord(char) < 32 and char not in "\r\n\t" for char in candidate):
                continue
            text = candidate
            break
        except UnicodeError:
            continue
    if text is None:
        raise ValueError(f"{path.name} 不是可读取的 UTF-8、UTF-16 或 GB18030 文本，已跳过。")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    pieces = re.split(r"\n[\t ]*\n+", text) if re.search(r"\n[\t ]*\n", text) else text.splitlines()
    result = []
    for index, piece in enumerate(pieces):
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) > MAX_QUOTE_LENGTH:
            raise ValueError(f"{path.name} 有文案超过 8000 字，已跳过该文件；请用空行分成较短的段落。")
        result.append(QuoteItem(f"{path.name}#{index}", piece, path.name))
        if len(result) >= MAX_QUOTES:
            break
    return result


def scan_content(folder: str) -> ContentSnapshot:
    """Scan only the selected directory, with hard limits and no file writes."""
    if not folder:
        return ContentSnapshot()
    root = Path(folder)
    warnings = []
    files = []
    try:
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_DIRECTORY_ENTRIES:
                    warnings.append("文件夹内容较多，仅检查前 4096 项；建议单独建立首页素材文件夹。")
                    break
                if entry.is_file(follow_symlinks=False):
                    files.append(Path(entry.path))
    except (OSError, ValueError):
        return ContentSnapshot(warnings=("素材文件夹不存在或无法访问，请在设置中重新选择。",), folder=folder)
    files.sort(key=lambda path: (path.name.casefold(), path.name))
    image_files = [path for path in files if path.suffix.lower() in IMAGE_SUFFIXES]
    text_files = [path for path in files if path.suffix.lower() == ".txt"]
    if len(image_files) > MAX_IMAGES:
        warnings.append("图片超过 512 张，仅展示按名称排列的前 512 张。")
    if len(text_files) > MAX_TEXT_FILES:
        warnings.append("TXT 超过 20 个，仅载入按名称排列的前 20 个。")
    quotes = []
    for path in text_files[:MAX_TEXT_FILES]:
        try:
            quotes.extend(read_quotes(path))
        except (OSError, ValueError) as exc:
            warnings.append(str(exc) if isinstance(exc, ValueError) else f"{path.name} 无法读取，已跳过。")
        if len(quotes) >= MAX_QUOTES:
            warnings.append("文案达到 500 条，仅载入前 500 条。")
            quotes = quotes[:MAX_QUOTES]
            break
    return ContentSnapshot(tuple(image_files[:MAX_IMAGES]), tuple(quotes), tuple(warnings), folder)


class ContentRotation:
    """A predictable cursor; random cycling uses every item before reshuffling."""

    def __init__(self, rng=None):
        self.keys = []
        self.mode = "sequential"
        self.index = 0
        self._rng = rng or random.Random()
        self._bag = []

    @property
    def current(self) -> str | None:
        return self.keys[self.index] if self.keys else None

    def reset(self, keys, mode="sequential", fixed="", keep=None):
        self.keys = list(dict.fromkeys(keys))
        self.mode = mode if mode in MODES else "sequential"
        chosen = fixed if self.mode == "single" else keep
        if chosen in self.keys:
            self.index = self.keys.index(chosen)
        elif self.mode == "random" and self.keys:
            self.index = self._rng.randrange(len(self.keys))
        else:
            self.index = 0
        self._bag = []

    def step(self, direction=1, *, automatic=False):
        if not self.keys or (automatic and self.mode == "single"):
            return self.current
        if self.mode == "random" and direction > 0:
            if not self._bag:
                self._bag = [index for index in range(len(self.keys)) if index != self.index]
                self._rng.shuffle(self._bag)
            if self._bag:
                self.index = self._bag.pop()
        else:
            self.index = (self.index + direction) % len(self.keys)
        return self.current


def upcoming_reminders(store, *, now: datetime | None = None, days=7) -> list[dict]:
    """Read the notebook without collecting, acknowledging or advancing alerts."""
    if store is None:
        return []
    now = now or datetime.now()
    days = max(1, min(31, int(days)))
    try:
        until = datetime.combine(now.date() + timedelta(days=days - 1), time.max)
    except OverflowError:
        until = datetime.max
    pending = {item["task_id"]: item for item in store.pending_alerts}
    rows = []
    for task in store.tasks:
        if task.get("completed"):
            continue
        try:
            scheduled = bool(task.get("remind_at"))
            if scheduled:
                anchor = datetime.fromisoformat(task["remind_at"])
                rule = task.get("repeat", {"kind": "once", "interval": 1})
                alert = pending.get(task["id"])
                if alert is not None:
                    due = datetime.fromisoformat(alert["due_at"])
                elif task.get("next_due"):
                    due = datetime.fromisoformat(task["next_due"])
                elif rule["kind"] == "once":
                    due = first_occurrence(anchor, rule)
                else:
                    due = next_occurrence(anchor, rule, now)
                if due is None:
                    continue
            else:
                due = datetime.combine(date.fromisoformat(task["date"]), time.min)
            if due > until:
                continue
            overdue = due < now if scheduled else due.date() < now.date()
            day_label = "今天" if due.date() == now.date() else "明天" if due.date() == now.date() + timedelta(days=1) else due.strftime("%m-%d")
            label = day_label + (due.strftime(" %H:%M") if scheduled else " · 全天")
            if overdue:
                label = "已逾期 · " + label
            rows.append({"id": task["id"], "title": task["title"], "body": task.get("body", ""),
                         "priority": task.get("priority", "normal"), "due": due,
                         "overdue": overdue, "label": label,
                         "recurring": bool(scheduled and task.get("repeat", {}).get("kind") != "once")})
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    priority = {"urgent": 0, "important": 1, "normal": 2, "low": 3}
    return sorted(rows, key=lambda row: (not row["overdue"], row["due"], priority.get(row["priority"], 2), row["id"]))
