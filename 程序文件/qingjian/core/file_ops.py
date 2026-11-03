"""Read-only file checks and previewed, reversible file organization."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import string
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .common import (CancelledError, atomic_write, ensure_cancel, expand_inputs,
                     make_result, notify, unique_path, write_report_book)


_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_FIELDS = {"序号", "原名", "扩展名", "修改日期", "父目录"}


def _key(path: Path) -> str:
    return os.path.normcase(str(path.absolute()))


def _linked(path: Path) -> bool:
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            return True
    return False


def _regular(path: Path):
    if _linked(path):
        raise ValueError(f"不处理符号链接或目录联接：{path}")
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在或不是普通文件：{path}")


def _safe_component(value: str) -> str:
    if not value or value in {".", ".."} or value[-1:] in {" ", "."}:
        raise ValueError(f"文件名或分类名无效：{value!r}")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
        raise ValueError(f"文件名或分类名含 Windows 不允许的字符：{value!r}")
    base = unicodedata.normalize("NFKC", value.split(".", 1)[0].rstrip(" .")).upper()
    if base in _RESERVED:
        raise ValueError(f"不能使用 Windows 保留名称：{value}")
    if len(value.encode("utf-16-le")) // 2 > 240:
        raise ValueError("文件名过长，请缩短命名模板或分类名。")
    return value


def _fingerprint(path: Path) -> dict:
    state = path.stat()
    return {"size": state.st_size, "mtime_ns": state.st_mtime_ns,
            "device": state.st_dev, "inode": state.st_ino}


def _sha(path: Path, cancel=None) -> str:
    _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            ensure_cancel(cancel)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _inputs(sources, folder, options):
    for value in sources:
        if _linked(Path(value).absolute()):
            raise ValueError(f"不读取符号链接或目录联接：{value}")
    files = expand_inputs(sources, recursive=bool(options.get("recursive", True)), exclude_root=folder)
    for path in files:
        _regular(path)
    return files


def _organize_options(options):
    action = options.get("action", "copy")
    group_by = options.get("group_by", "none")
    if action not in {"copy", "rename"}:
        raise ValueError("整理动作必须为 copy 或 rename。")
    if group_by not in {"none", "extension", "month", "pattern"}:
        raise ValueError("不支持该归档分类方式。")
    if action == "rename" and group_by != "none":
        raise ValueError("原地重命名不能同时分类或移动目录，请选择“不分类”。")
    template = options.get("filename_template", "{序号:03d}_{原名}")
    if not isinstance(template, str) or not template.strip() or len(template) > 500:
        raise ValueError("请填写有效的文件名模板（最多 500 字符）。")
    try:
        for _, field, specification, conversion in string.Formatter().parse(template):
            if field is not None and (field not in _FIELDS or conversion or "{" in specification or "}" in specification):
                raise ValueError("模板仅支持：序号、原名、扩展名、修改日期、父目录；不支持属性或嵌套表达式。")
            if field is not None and (len(specification) > 16 or any(int(number) > 240 for number in re.findall(r"\d+", specification))):
                raise ValueError("模板格式的宽度或精度不能超过 240。")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"文件名模板无效：{exc}") from exc
    pattern = None
    if group_by == "pattern":
        try:
            pattern = re.compile(str(options.get("group_pattern", "")))
        except re.error as exc:
            raise ValueError(f"分类正则表达式无效：{exc}") from exc
        if not pattern.groups:
            raise ValueError("正则分类需要至少一个捕获组，例如 (202[0-9])。")
    return action, group_by, template, pattern


def _target(source, index, folder, settings):
    action, group_by, template, pattern = settings
    modified = datetime.fromtimestamp(source.stat().st_mtime)
    values = {"序号": index, "原名": source.stem, "扩展名": source.suffix.lstrip("."),
              "修改日期": modified.strftime("%Y-%m-%d"), "父目录": source.parent.name}
    try:
        name = template.format_map(values)
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"文件名模板无法应用到 {source.name}：{exc}") from exc
    if source.suffix and not name.lower().endswith(source.suffix.lower()):
        name += source.suffix
    _safe_component(name)
    directory = source.parent if action == "rename" else Path(folder).absolute()
    category = None
    if group_by == "extension":
        category = source.suffix.lstrip(".").lower() or "无扩展名"
    elif group_by == "month":
        category = modified.strftime("%Y-%m")
    elif group_by == "pattern":
        match = pattern.search(source.name)
        category = (match.group(1) if match else None) or "未匹配"
    if category is not None:
        directory /= _safe_component(category)
    target = directory / name
    if _linked(target):
        raise ValueError(f"输出路径包含符号链接或目录联接：{target}")
    if action == "copy" and not target.resolve().is_relative_to(Path(folder).resolve()):
        raise ValueError("输出路径超出本次结果目录，已拒绝操作。")
    return target


def _conflicts(plan, action):
    sources = {_key(Path(row["source"])) for row in plan}
    seen = set()
    for row in plan:
        target = Path(row["target"])
        key = _key(target)
        if key in seen:
            raise ValueError(f"多个文件将使用同一目标名称，请修改模板：{target}")
        seen.add(key)
        if os.path.lexists(target) and not (action == "rename" and key in sources):
            raise FileExistsError(f"目标已被占用，未覆盖：{target}")


def plan_organize(sources: list[Path], folder: Path, options: dict, progress=None, cancel=None) -> list[dict]:
    """Return exact paths and source hashes; this function never modifies inputs."""
    ensure_cancel(cancel)
    settings = _organize_options(options)
    folder = Path(folder).absolute()
    if _linked(folder):
        raise ValueError("结果目录不能位于符号链接或目录联接内。")
    files = _inputs(sources, folder, options)
    if not files:
        raise ValueError("所选范围中没有可整理的普通文件。")
    plan = []
    for index, source in enumerate(files, 1):
        ensure_cancel(cancel)
        before = _fingerprint(source)
        digest = _sha(source, cancel)
        if before != _fingerprint(source):
            raise ValueError(f"生成预览时文件发生变化，请关闭正在编辑的程序后重试：{source}")
        target = _target(source, index, folder, settings)
        plan.append({"source": str(source), "target": str(target), "index": index,
                     "sha256": digest, "fingerprint": before, "action": settings[0]})
        notify(progress, index, len(files), f"预览：{source.name}")
    _conflicts(plan, settings[0])
    return plan


def _verify_plan(sources, folder, options, cancel):
    plan = options.get("_plan")
    if not isinstance(plan, list) or not plan:
        raise ValueError("请先生成整理预览，确认计划后再执行。")
    settings = _organize_options(options)
    files = _inputs(sources, folder, options)
    actual = {_key(path) for path in files}
    planned = set()
    for index, row in enumerate(plan, 1):
        ensure_cancel(cancel)
        if not isinstance(row, dict) or not {"source", "target", "sha256", "fingerprint"}.issubset(row):
            raise ValueError("整理计划不完整，请重新生成预览。")
        source, target = Path(row["source"]).absolute(), Path(row["target"]).absolute()
        source_key = _key(source)
        if source_key not in actual or source_key in planned or row.get("index") != index or row.get("action") != settings[0]:
            raise ValueError("所选文件或整理方式已变化，请重新生成预览。")
        planned.add(source_key)
        _regular(source)
        if row["fingerprint"] != _fingerprint(source) or row["sha256"] != _sha(source, cancel):
            raise ValueError(f"文件在预览后已改变，未执行整理：{source}")
        if str(target) != str(_target(source, index, folder, settings)):
            raise ValueError("目标路径或命名规则与预览不一致，请重新生成预览。")
        if settings[0] == "rename" and source.parent != target.parent:
            raise ValueError("原地改名不得移动文件所在目录。")
    if planned != actual:
        raise ValueError("所选范围中的文件数量已变化，请重新生成预览。")
    _conflicts(plan, settings[0])
    return plan, settings[0]


def _move(source: Path, target: Path):
    """Same-directory file move which never overwrites an occupied destination."""
    if os.path.lexists(target):
        raise FileExistsError(f"目标路径已被占用：{target}")
    if os.name == "nt":
        os.rename(source, target)
    else:
        os.link(source, target)
        source.unlink()


def _rollback(locations: dict[str, Path]) -> list[str]:
    failures, staged = [], []
    for original, current in locations.items():
        original = Path(original)
        if str(current) == str(original):
            continue
        temporary = current.parent / (".qingjian-rollback-" + uuid.uuid4().hex + ".tmp")
        try:
            _move(current, temporary)
            staged.append((original, temporary))
        except Exception as exc:
            failures.append(f"无法取回 {current}：{exc}")
    for original, temporary in staged:
        try:
            _move(temporary, original)
        except Exception as exc:
            failures.append(f"原名称被占用或无法恢复；文件保留在 {temporary}：{exc}")
    return failures


def _rename_transaction(plan, folder, progress, cancel, action="rename", undo_of=None):
    folder = Path(folder).absolute()
    if _linked(folder):
        raise ValueError("结果目录不能位于符号链接或目录联接内。")
    folder.mkdir(parents=True, exist_ok=True)
    changed = [row for row in plan if str(row["source"]) != str(row["target"])]
    if not changed:
        targets = [row["target"] for row in plan]
        result = make_result(targets, 0, ["文件名与原名相同，没有执行改名。"])
        result["data_outputs"] = targets
        return result
    locations = {}
    try:
        for index, row in enumerate(changed, 1):
            ensure_cancel(cancel)
            source = Path(row["source"])
            _regular(source)
            if _sha(source, cancel) != row["sha256"]:
                raise ValueError(f"执行前文件已变化：{source}")
            temporary = source.parent / (".qingjian-rename-" + uuid.uuid4().hex + ".tmp")
            _move(source, temporary)
            locations[str(source)] = temporary
            notify(progress, index, len(changed) * 2, f"准备改名：{source.name}")
        for index, row in enumerate(changed, 1):
            ensure_cancel(cancel)
            source, target = Path(row["source"]), Path(row["target"])
            if _linked(target):
                raise ValueError(f"目标路径出现符号链接：{target}")
            _move(locations[str(source)], target)
            locations[str(source)] = target
            notify(progress, len(changed) + index, len(changed) * 2, f"已改名：{target.name}")
        ensure_cancel(cancel)
        for row in changed:
            if _sha(Path(row["target"]), cancel) != row["sha256"]:
                raise ValueError("改名期间文件内容被其他程序修改，正在恢复原名称。")
        manifest = unique_path(folder, "改名清单.json" if action == "rename" else "撤销清单.json")
        body = {"version": 1, "tool": "file_organize", "action": action,
                "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                "entries": [{"source": row["source"], "target": row["target"], "sha256": row["sha256"],
                             "size": Path(row["target"]).stat().st_size} for row in changed]}
        if undo_of:
            body["undo_of"] = str(undo_of)
        atomic_write(manifest, lambda path: path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"))
    except Exception:
        failures = _rollback(locations)
        if failures:
            raise RuntimeError("改名未完成；回滚遇到冲突，所有能取回的文件均保留，详情：\n" + "\n".join(failures))
        raise
    targets = [row["target"] for row in plan]
    result = make_result([*targets, manifest], len(changed))
    result["data_outputs"] = targets
    return result


def undo_organize(manifest_path: Path, folder: Path, progress=None, cancel=None) -> dict:
    """Restore names only when every target still has its recorded content hash."""
    ensure_cancel(cancel)
    manifest_path = Path(manifest_path).absolute()
    _regular(manifest_path)
    if manifest_path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("改名清单超过 128 MB，请检查文件是否正确。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("改名清单不是有效的 JSON 文件。") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("tool") != "file_organize" or manifest.get("action") != "rename":
        raise ValueError("请选择本工具生成的原地改名清单，撤销记录不能再次撤销。")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > 50000:
        raise ValueError("改名清单缺少有效的文件条目。")
    plan, seen_source, seen_target = [], set(), set()
    for index, entry in enumerate(entries, 1):
        ensure_cancel(cancel)
        if not isinstance(entry, dict) or not {"source", "target", "sha256", "size"}.issubset(entry):
            raise ValueError("改名清单条目不完整。")
        source, target = Path(entry["source"]), Path(entry["target"])
        if not source.is_absolute() or not target.is_absolute() or source.parent != target.parent:
            raise ValueError("改名清单中的路径无效：仅可恢复同一目录内的文件名。")
        _safe_component(source.name)
        _safe_component(target.name)
        if _linked(source) or _linked(target):
            raise ValueError("改名清单路径包含符号链接，未执行撤销。")
        if _key(source) in seen_source or _key(target) in seen_target or not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"])):
            raise ValueError("改名清单中存在重复路径或无效的哈希。")
        seen_source.add(_key(source))
        seen_target.add(_key(target))
        _regular(target)
        if target.stat().st_size != entry["size"] or _sha(target, cancel) != entry["sha256"]:
            raise ValueError(f"文件已被修改，未执行撤销：{target}")
        plan.append({"source": str(target), "target": str(source), "sha256": entry["sha256"]})
        notify(progress, index, len(entries), f"验证撤销：{target.name}")
    _conflicts(plan, "rename")
    return _rename_transaction(plan, folder, progress, cancel, action="undo", undo_of=manifest_path)


def _copy_organize(plan, folder, progress, cancel):
    outputs, records, errors = [], [["源文件", "输出文件", "SHA256", "大小（字节）", "结果"]], []
    for index, row in enumerate(plan, 1):
        ensure_cancel(cancel)
        source, target = Path(row["source"]), Path(row["target"])
        try:
            _regular(source)
            if _linked(target) or not target.resolve().is_relative_to(Path(folder).resolve()):
                raise ValueError("目标路径越界或包含符号链接。")

            def writer(temporary):
                shutil.copy2(source, temporary)
                if _sha(temporary, cancel) != row["sha256"] or _sha(source, cancel) != row["sha256"]:
                    raise ValueError("复制期间源文件发生变化，本文件未输出。")

            atomic_write(target, writer)
            outputs.append(target)
            records.append([str(source), str(target), row["sha256"], target.stat().st_size, "已复制，原文件保留"])
        except CancelledError:
            raise
        except Exception as exc:
            errors.append(f"{source}：{exc}")
            records.append([str(source), str(target), row["sha256"], "", str(exc)])
        notify(progress, index, len(plan), f"归档：{source.name}")
    data_outputs = [str(path) for path in outputs]
    report = unique_path(folder, "文件整理清单.xlsx")
    try:
        write_report_book(report, {"整理清单": records})
        outputs.append(report)
    except Exception as exc:
        errors.append(f"整理清单保存失败：{exc}")
    result = make_result(outputs, len(data_outputs), errors=errors)
    result["data_outputs"] = data_outputs
    return result


def _find_duplicates(sources, folder, options, progress, cancel):
    files = _inputs(sources, folder, options)
    buckets, errors, hashed = defaultdict(list), [], defaultdict(list)
    for source in files:
        ensure_cancel(cancel)
        try:
            buckets[source.stat().st_size].append(source)
        except OSError as exc:
            errors.append(f"{source}：{exc}")
    done = 0
    for size, paths in buckets.items():
        for source in paths:
            ensure_cancel(cancel)
            if len(paths) > 1:
                try:
                    before = _fingerprint(source)
                    digest = _sha(source, cancel)
                    if before != _fingerprint(source):
                        raise ValueError("校验期间文件发生变化，未参与查重。")
                    hashed[(size, digest)].append(source)
                except CancelledError:
                    raise
                except Exception as exc:
                    errors.append(f"{source}：{exc}")
            done += 1
            notify(progress, done, len(files), f"查重：{source.name}")
    rows = [["重复组", "文件路径", "大小（字节）", "SHA256"]]
    groups = [((size, digest), paths) for (size, digest), paths in hashed.items() if len(paths) > 1]
    copies, savings = 0, 0
    for group, ((size, digest), paths) in enumerate(groups, 1):
        copies += len(paths) - 1
        savings += (len(paths) - 1) * size
        for path in paths:
            rows.append([group, str(path), size, digest])
    report = unique_path(folder, "完全重复文件检查.xlsx")
    write_report_book(report, {"重复文件": rows,
                             "汇总": [["项目", "数量"], ["扫描文件", len(files)], ["重复组", len(groups)],
                                    ["多余副本数（未删除）", copies], ["理论可节省字节（未删除）", savings]],
                             "检查范围": [["输入范围", "递归子目录"], *[[str(value), bool(options.get("recursive", True))] for value in sources]],
                             "读取异常": [["说明"], *[[error] for error in errors]]})
    return make_result([report], copies, errors=errors)


def _patterns(options, key):
    values = options.get(key, [])
    if isinstance(values, str):
        values = values.splitlines()
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{key} 应为文本列表。")
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _glob_matches(path, root, pattern):
    normalized = pattern.replace("\\", "/")
    if "/" in normalized:
        if root is None:
            return None
        candidate = path.relative_to(root).as_posix()
    else:
        candidate = path.name
    candidate, normalized = candidate.casefold(), normalized.casefold()
    if "/" not in normalized:
        return fnmatch.fnmatchcase(candidate, normalized)
    parts, patterns = candidate.split("/"), normalized.removeprefix("./").split("/")

    @lru_cache(None)
    def match(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and match(i + 1, j + 1)

    return match(0, 0)


def _check_materials(sources, folder, options, progress, cancel):
    required_files = _patterns(options, "required_files")
    required_columns = _patterns(options, "required_columns")
    if not required_files and not required_columns:
        raise ValueError("请至少填写一种必需文件或必填列。")
    scopes, singles, warnings, errors = [], [], [], []
    for value in sources:
        ensure_cancel(cancel)
        path = Path(value).absolute()
        if _linked(path):
            raise ValueError(f"不读取符号链接或目录联接：{path}")
        if path.is_dir():
            scopes.append((str(path), path.resolve(), _inputs([path], folder, options)))
        elif path.is_file():
            _regular(path)
            singles.append(path.resolve())
        else:
            errors.append(f"输入不存在：{path}")
    if singles:
        scopes.append(("所选文件（仅按文件名匹配）", None, list(dict.fromkeys(singles))))
    missing = [["检查范围", "必需文件规则", "结果", "匹配文件"]]
    blank = [["检查范围", "来源文件", "工作表", "原始行", "必填列", "异常"]]
    inventory = [["检查范围", "相对路径/文件名", "完整路径", "大小（字节）"]]
    scope_rows = [["检查范围", "文件夹根路径", "文件数", "递归子目录", "相对路径判断", "必填列工作表范围"]]
    issues, done = 0, 0
    total = sum(len(files) for _, _, files in scopes)
    for label, root, files in scopes:
        ensure_cancel(cancel)
        scope_rows.append([label, str(root) if root else "", len(files), bool(options.get("recursive", True)),
                           "支持" if root else "未判断", options.get("sheet") or "全部可见工作表；CSV/TSV 全表"])
        for pattern in required_files:
            if root is None and "/" in pattern.replace("\\", "/"):
                missing.append([label, pattern, "未判断：需要文件夹根目录", ""])
                warnings.append(f"{pattern}：未选择文件夹根目录，不能判断相对路径缺漏。")
                continue
            matches = [path for path in files if _glob_matches(path, root, pattern)]
            if matches:
                missing.extend([label, pattern, "齐全", str(path)] for path in matches)
            else:
                missing.append([label, pattern, "缺漏", ""])
            if not matches:
                issues += 1
        for path in files:
            ensure_cancel(cancel)
            try:
                inventory.append([label, path.relative_to(root).as_posix() if root else path.name, str(path), path.stat().st_size])
                if required_columns and path.suffix.lower() in {".xlsx", ".xlsm", ".csv", ".tsv"}:
                    from .excel_ops import read_table
                    selected_sheet = str(options.get("sheet", ""))
                    sheets = [selected_sheet]
                    if path.suffix.lower() in {".xlsx", ".xlsm"} and not selected_sheet:
                        from openpyxl import load_workbook
                        workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
                        try:
                            sheets = [sheet.title for sheet in workbook.worksheets if sheet.sheet_state == "visible"]
                            if len(sheets) != len(workbook.sheetnames):
                                warnings.append(f"{path.name}：必填列检查默认跳过隐藏工作表；可指定工作表单独检查。")
                        finally:
                            workbook.close()
                    for sheet in sheets:
                        ensure_cancel(cancel)
                        try:
                            headers, rows, notes = read_table(path, sheet=sheet, header_row=int(options.get("header_row", 1)))
                            warnings.extend(f"{path.name}：{note}" for note in notes)
                            absent = [name for name in required_columns if name not in headers]
                            for name in absent:
                                blank.append([label, str(path), sheet, "", name, "缺少必填列"])
                                issues += 1
                            for row in rows:
                                ensure_cancel(cancel)
                                for name in required_columns:
                                    if name not in headers:
                                        continue
                                    value = row.get(name)
                                    if value is None or (isinstance(value, str) and not value.strip()):
                                        blank.append([label, str(path), getattr(row, "source_sheet", ""), getattr(row, "source_row", ""), name, "必填字段为空"])
                                        issues += 1
                        except CancelledError:
                            raise
                        except Exception as exc:
                            errors.append(f"{path} [{sheet or '数据表'}]：{exc}")
            except CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{path}：{exc}")
            done += 1
            notify(progress, done, total, f"核对材料：{path.name}")
    report = unique_path(folder, "材料完整性检查.xlsx")
    write_report_book(report, {"文件缺漏": missing, "必填字段异常": blank, "材料清单": inventory, "检查范围": scope_rows,
                             "检查条件": [["条件", "值"], *[["必需文件规则", value] for value in required_files],
                                      *[["必填列", value] for value in required_columns],
                                      ["表头行", options.get("header_row", 1)]],
                             "读取提示": [["类型", "说明"], *[["提示", value] for value in warnings], *[["异常", value] for value in errors]]})
    return make_result([report], issues, warnings, errors)


def run_files(tool: str, sources: list[Path], folder: Path, options: dict, progress=None, cancel=None) -> dict:
    ensure_cancel(cancel)
    folder = Path(folder).absolute()
    if _linked(folder):
        raise ValueError("结果目录不能位于符号链接或目录联接内。")
    if tool == "file_organize":
        plan, action = _verify_plan(sources, folder, options, cancel)
        return (_rename_transaction(plan, folder, progress, cancel) if action == "rename"
                else _copy_organize(plan, folder, progress, cancel))
    if tool == "find_duplicates":
        return _find_duplicates(sources, folder, options, progress, cancel)
    if tool == "check_materials":
        return _check_materials(sources, folder, options, progress, cancel)
    raise ValueError(f"不支持的文件工具：{tool}")
