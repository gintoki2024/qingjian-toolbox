"""Shared safe I/O for the offline batch tools."""
from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path


class CancelledError(Exception):
    """Raised between units of work after a user stop request."""


def ensure_cancel(cancel=None):
    if cancel and cancel():
        raise CancelledError("任务已停止，已经生成的结果予以保留。")


def notify(progress, done, total, message=""):
    if progress:
        progress(int(done), max(1, int(total)), str(message))


def expand_inputs(sources, extensions=None, recursive=True, exclude_root=None):
    suffixes = {s.lower() if s.startswith(".") else "." + s.lower() for s in extensions} if extensions is not None else None
    exclusion = Path(exclude_root).resolve() if exclude_root else None
    result, seen = [], set()

    def linked(path):
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())

    def include(path):
        if linked(path) or path.name.startswith("~$") or not path.is_file():
            return
        resolved = path.resolve()
        if exclusion and (resolved == exclusion or exclusion in resolved.parents):
            return
        if suffixes is not None and resolved.suffix.lower() not in suffixes:
            return
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
        if len(result) > 50000:
            raise ValueError("一次任务最多读取50000个文件，请缩小选择范围。")

    for value in sources:
        path = Path(value).expanduser()
        if any(linked(part) for part in [path.absolute(), *path.absolute().parents]):
            raise ValueError(f"不读取符号链接，请直接选择实际文件或目录：{path.name}")
        if exclusion and path.resolve().is_relative_to(exclusion):
            continue
        if path.is_dir():
            if recursive:
                def walk_error(exc):
                    raise OSError(f"无法读取文件夹，请检查权限或连接：{exc.filename}") from exc
                for current, dirs, files in os.walk(path, followlinks=False, onerror=walk_error):
                    current_path = Path(current)
                    dirs[:] = sorted(d for d in dirs if not linked(current_path / d)
                                      and (not exclusion or (current_path / d).resolve() != exclusion))
                    for name in sorted(files):
                        include(current_path / name)
            else:
                for candidate in sorted(path.iterdir()):
                    include(candidate)
        elif path.is_file():
            include(path)
        else:
            raise FileNotFoundError(f"找不到输入文件或文件夹：{path}")
    return result


def safe_name(value, fallback="未命名", limit=180):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
    if not text:
        text = fallback
    if text.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        text = "_" + text
    if os.name != "nt":
        # APFS and other POSIX filesystems cap each filename by UTF-8 bytes.
        # Leave headroom for suffixes/numbers instead of emitting a 540-byte
        # filename when a Chinese title contains 180 ordinary characters.
        return text.encode("utf-8", errors="ignore")[:min(limit, 240)].decode("utf-8", errors="ignore").rstrip(" .") or fallback
    return text.encode("utf-16-le", errors="ignore")[:limit * 2].decode("utf-16-le", errors="ignore").rstrip(" .") or fallback


def unique_path(folder, name):
    directory = Path(folder).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    cleaned = safe_name(name, limit=32767)
    suffix = Path(cleaned).suffix
    # Keep the document/image extension even when a long generated name is
    # shortened. Windows measures filename limits in UTF-16 code units.
    if len(suffix) > 20:
        suffix = ""
    stem = cleaned[:-len(suffix)] if suffix else cleaned
    filename = safe_name(stem, limit=180 - len(suffix)) + suffix
    path = directory / filename
    if not path.exists():
        return path
    for number in range(2, 100000):
        candidate = directory / f"{path.stem[:150]}_{number}{path.suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError("同名输出过多，请使用新的结果文件夹。")


def commit_file(staged, target):
    staged, target = Path(staged), Path(target)
    if os.path.lexists(target):
        raise FileExistsError(f"输出已存在，未覆盖：{target.name}")
    if os.name == "nt":
        staged.rename(target)
    else:
        os.link(staged, target)
        staged.unlink()


def atomic_write(target, writer):
    destination = Path(target).absolute()
    if os.path.lexists(destination):
        raise FileExistsError(f"输出已存在，未覆盖：{destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".qingjian-write-", suffix=destination.suffix, dir=destination.parent)
    os.close(descriptor)
    staged = Path(name)
    try:
        writer(staged)
        if not staged.is_file():
            raise OSError("处理器没有生成结果文件。")
        commit_file(staged, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            staged.unlink()
    return destination


def write_report_book(target, sheets):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    workbook = Workbook()
    workbook.remove(workbook.active)
    used_names = set()
    for name, rows in sheets.items():
        title = re.sub(r'[\\/*?:\[\]]', "_", str(name))[:31] or "结果"
        initial, index = title, 2
        while title in used_names:
            title = initial[:26] + "_" + str(index)
            index += 1
        used_names.add(title)
        sheet = workbook.create_sheet(title)
        widths = {}
        for r, row in enumerate(rows, 1):
            if r > 1048576:
                raise ValueError("结果超过Excel每个工作表的行数限制，请缩小范围。")
            if len(row) > 16384:
                raise ValueError("结果超过Excel列数限制。")
            for c, value in enumerate(row, 1):
                if isinstance(value, Path):
                    value = str(value)
                if isinstance(value, str):
                    invalid = re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value)
                    if invalid:
                        raise ValueError(f"结果第{r}行第{c}列含Excel不支持的控制字符U+{ord(invalid.group()):04X}，未静默修改内容。")
                    # A workbook cell has a fixed Excel limit; preserve the rest
                    # via an explicit error instead of quietly truncating data.
                    if len(value) > 32767:
                        raise ValueError("单个结果单元格超过32767字符，请缩小提取范围。")
                cell = sheet.cell(r, c, value)
                if isinstance(value, str):
                    cell.data_type = "s"
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if r == 1:
                    cell.font = Font(name="微软雅黑", size=10, color="FFFFFF", bold=True)
                    cell.fill = PatternFill("solid", fgColor="287D70")
                else:
                    cell.font = Font(name="微软雅黑", size=10)
                    if r % 2 == 0:
                        cell.fill = PatternFill("solid", fgColor="F0F6F4")
                if r <= 250:
                    widths[c] = max(widths.get(c, 10), min(60, max((len(x) for x in str(value or "").splitlines()), default=0) * 1.3 + 3))
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column, width in widths.items():
            sheet.column_dimensions[get_column_letter(column)].width = width
    if not workbook.sheetnames:
        workbook.create_sheet("结果")
    try:
        atomic_write(target, workbook.save)
    finally:
        workbook.close()
    return Path(target)


def make_result(outputs, changes, warnings=None, errors=None):
    return {"outputs": [str(p) for p in outputs], "changes": int(changes),
            "warnings": list(warnings or []), "errors": list(errors or [])}
