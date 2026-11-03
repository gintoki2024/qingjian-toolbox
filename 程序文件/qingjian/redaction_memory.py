"""Explicit, local storage for reusable literal redaction rules.

Rule contents belong only in this store. Errors deliberately describe the
operation, never a rule's find/replace text or the contents of a damaged file.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

MAX_RULES = 5000
MAX_GROUPS = 200
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_TEXT_LENGTH = 100000
_GROUP_KEYS = ("id", "name", "created", "updated", "rules")
_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _valid_text(value: object, *, maximum: int, allow_empty: bool = True) -> bool:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def normalize_rules(rules: object) -> list[dict[str, str]]:
    """Validate and copy literal rules without stripping or parsing their text.

    An empty replacement means deletion. Empty lists are useful while editing;
    saving a named group additionally requires at least one rule.
    """
    if not isinstance(rules, list):
        raise ValueError("替换规则必须是规则列表。")
    if len(rules) > MAX_RULES:
        raise ValueError(f"每组最多保存 {MAX_RULES} 条替换规则，请拆分为多个规则组。")
    clean = []
    for index, item in enumerate(rules, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条替换规则的格式无效。")
        if not _valid_text(item.get("find"), maximum=MAX_TEXT_LENGTH, allow_empty=False):
            raise ValueError(f"第 {index} 条规则的查找内容不能为空，且须为有效文字。")
        if not _valid_text(item.get("replace"), maximum=MAX_TEXT_LENGTH):
            raise ValueError(f"第 {index} 条规则的替换内容须为有效文字，可留空表示删除。")
        clean.append({"find": item["find"], "replace": item["replace"]})
    return clean


def _normalize_name(name: object) -> str:
    if not isinstance(name, str):
        raise ValueError("规则组名称须为 1 至 80 个字。")
    clean = name.strip()
    if not _valid_text(clean, maximum=80, allow_empty=False) or "\x00" in clean:
        raise ValueError("规则组名称须为 1 至 80 个有效文字。")
    return clean


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None


def _valid_date(value: object) -> bool:
    if not _valid_text(value, maximum=80, allow_empty=False):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _reject_constant(_value: str):
    raise ValueError("记忆库中包含不支持的数值。")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("记忆库中包含重复字段。")
        result[key] = value
    return result


class RedactionMemoryStore:
    """A named rule library whose successful edits are durably committed.

    ``path=None`` is deliberately memory-only, useful for an isolated UI session
    and tests. A damaged existing file is retained and blocked from overwrite.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self.last_error = ""
        self._groups: list[dict] = []
        self._restore_id = ""
        self._load_failed = False
        if self.path is not None:
            self._load()

    @property
    def groups(self) -> list[dict]:
        return copy.deepcopy(self._groups)

    @property
    def restore_id(self) -> str:
        return self._restore_id

    def find_group(self, group_id: str) -> dict | None:
        for item in self._groups:
            if item["id"] == group_id:
                return copy.deepcopy(item)
        return None

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            # Bound the read itself; a file may grow after a stat check.
            with self.path.open("rb") as stream:
                encoded = stream.read(MAX_FILE_SIZE + 1)
            if len(encoded) > MAX_FILE_SIZE:
                raise ValueError("记忆库文件超过大小限制。")
            data = json.loads(encoded.decode("utf-8-sig"), parse_constant=_reject_constant,
                              object_pairs_hook=_unique_object)
            if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
                raise ValueError("记忆库版本无法识别。")
            values = data.get("groups")
            restore_id = data.get("restore_id")
            if not isinstance(values, list) or len(values) > MAX_GROUPS or not isinstance(restore_id, str):
                raise ValueError("记忆库结构无效。")
            groups, names, ids = [], set(), set()
            for item in values:
                group = self._read_group(item)
                if group["id"] in ids or group["name"].casefold() in names:
                    raise ValueError("记忆库包含重复规则组。")
                ids.add(group["id"])
                names.add(group["name"].casefold())
                groups.append(group)
            if restore_id and restore_id not in ids:
                raise ValueError("记忆库的自动载入规则组不存在。")
            self._groups, self._restore_id = groups, restore_id
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            self._groups, self._restore_id = [], ""
            self._load_failed = True
            self.last_error = "无法读取脱敏记忆库：文件格式无效、超过大小限制或无法访问。原文件已保留，请先检查或备份后修复，再重新打开工具。"

    @staticmethod
    def _read_group(item: object) -> dict:
        if not isinstance(item, dict) or not all(key in item for key in _GROUP_KEYS):
            raise ValueError("记忆库中的规则组结构无效。")
        if not _valid_id(item["id"]) or not _valid_date(item["created"]) or not _valid_date(item["updated"]):
            raise ValueError("记忆库中的规则组信息无效。")
        name = _normalize_name(item["name"])
        rules = normalize_rules(item["rules"])
        if not rules:
            raise ValueError("记忆库中的规则组不能为空。")
        return {"id": item["id"], "name": name, "created": item["created"],
                "updated": item["updated"], "rules": rules}

    def _ensure_writable(self) -> None:
        if self._load_failed:
            raise ValueError(self.last_error)

    def _commit(self, groups: list[dict], restore_id: str) -> None:
        """Persist first so a failed write cannot pretend a rule was remembered."""
        self._ensure_writable()
        staged = None
        descriptor = None
        try:
            payload = {"schema_version": 1, "groups": groups, "restore_id": restore_id}
            encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_FILE_SIZE:
                raise ValueError("脱敏记忆库已超过 10 MB，请减少规则内容或规则组。")
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, filename = tempfile.mkstemp(prefix=".qingjian-redaction-", suffix=".tmp", dir=self.path.parent)
                staged = Path(filename)
                stream = os.fdopen(descriptor, "wb")
                descriptor = None
                with stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(staged, self.path)
        except OSError:
            self.last_error = "脱敏记忆库保存失败，请检查保存位置是否可写及磁盘空间。此次修改未保存。"
            raise OSError(self.last_error) from None
        except (ValueError, TypeError, UnicodeError):
            self.last_error = "脱敏记忆库内容无效或超过 10 MB，此次修改未保存。"
            raise ValueError(self.last_error) from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass
        self._groups, self._restore_id = groups, restore_id
        self.last_error = ""

    def save_group(self, name: str, rules: list[dict[str, str]], group_id: str | None = None) -> dict:
        self._ensure_writable()
        name, rules = _normalize_name(name), normalize_rules(rules)
        if not rules:
            raise ValueError("至少填写一条替换规则后，才能记住规则组。")
        old = None
        if group_id is not None:
            if not _valid_id(group_id) or (old := self.find_group(group_id)) is None:
                raise ValueError("要更新的规则组不存在，请重新选择。")
        if any(item["name"].casefold() == name.casefold() and item["id"] != group_id for item in self._groups):
            raise ValueError("已有同名规则组，请更换名称，或选择该组后更新。")
        if old is None and len(self._groups) >= MAX_GROUPS:
            raise ValueError(f"记忆库最多保存 {MAX_GROUPS} 个规则组，请先删除不再使用的规则组。")
        now = _now()
        group = {"id": old["id"] if old else uuid.uuid4().hex, "name": name,
                 "created": old["created"] if old else now, "updated": now, "rules": rules}
        groups = copy.deepcopy(self._groups)
        if old is None:
            groups.append(group)
        else:
            groups[next(index for index, item in enumerate(groups) if item["id"] == group_id)] = group
        self._commit(groups, self._restore_id)
        return copy.deepcopy(group)

    def delete_group(self, group_id: str) -> bool:
        self._ensure_writable()
        if not any(item["id"] == group_id for item in self._groups):
            return False
        groups = [copy.deepcopy(item) for item in self._groups if item["id"] != group_id]
        self._commit(groups, "" if self._restore_id == group_id else self._restore_id)
        return True

    def set_restore_group(self, group_id: str) -> None:
        self._ensure_writable()
        if not isinstance(group_id, str) or (group_id and self.find_group(group_id) is None):
            raise ValueError("要自动载入的规则组不存在，请重新选择。")
        if group_id == self._restore_id:
            return
        self._commit(copy.deepcopy(self._groups), group_id)
