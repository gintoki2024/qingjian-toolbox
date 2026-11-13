"""Small platform choices shared by Qt UI and document defaults.

Only *new defaults* are mapped. User supplied or saved font names are always
preserved; no system font is copied into the distribution.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def user_cache_dir() -> Path:
    """Keep reusable application caches outside replaceable source folders."""
    selected = os.environ.get("QINGJIAN_USER_DATA")
    if selected:
        return Path(selected).expanduser() / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/QingjianLocal"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "QingjianLocal/Cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "QingjianLocal"


def ui_font_family() -> str:
    return "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei UI"


def platform_stylesheet(style: str) -> str:
    if sys.platform == "darwin":
        return style.replace("'Microsoft YaHei UI', 'Microsoft YaHei'",
                             "'PingFang SC', 'Microsoft YaHei UI', 'Microsoft YaHei'")
    return style


def default_document_font(name: str) -> str:
    if sys.platform != "darwin":
        return name
    return {
        "宋体": "Songti SC", "仿宋": "STFangsong", "楷体": "Kaiti SC",
        "黑体": "Heiti SC", "方正小标宋简体": "Songti SC",
        "微软雅黑": "PingFang SC", "Microsoft YaHei UI": "PingFang SC",
    }.get(name, name)


def document_font_choices(names) -> list[str]:
    mapped = [default_document_font(name) for name in names]
    return list(dict.fromkeys([*mapped, *names]))


def shortcut_text(text: str) -> str:
    return text.replace("Ctrl+", "⌘+").replace("Alt+", "⌥+") if sys.platform == "darwin" else text


def platform_description() -> str:
    return "macOS" if sys.platform == "darwin" else "Windows 64 位" if sys.platform == "win32" else "本地桌面版"
