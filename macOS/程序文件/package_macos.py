"""Create Mac source/full ZIPs with three root entries and Unix permissions.

This module is platform independent; a full distribution must first be built
and accepted on macOS by build_macos.py.  Packaging never downloads anything.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parent
ROOT_ENTRIES = {"启动青简工具箱.command", "使用说明.txt", "程序文件"}
EXCLUDED_DIRS = {
    ".git", ".agents", ".codex", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", "target",
    "runtime", "Python", "native", "engines", "config", "logs", "cache",
    "downloads", "node_modules", "验证",
}
WINDOWS_SUFFIXES = {".exe", ".dll", ".pyd", ".msi", ".cab", ".pdb", ".lib", ".cmd", ".bat", ".ps1", ".reg", ".cs"}
ARCHIVE_SUFFIXES = {".zip", ".7z", ".tar", ".gz", ".xz", ".bz2", ".dmg", ".pkg"}
GENERATED_NAMES = {"cmd-package.json", "macos-package.json", "启动诊断.cmd", "build-manifest.json",
                   "macos-build-validation.json", "macos-build-inventory.json"}
# Preserve the upstream signed LibreOffice.app resource tree. CPython ships
# this historical, non-runtime maintenance text; it is never executed here.
# No Windows binary, launcher, arbitrary batch file or different bytes qualify.
UPSTREAM_SUPPORT_RESOURCES = {
    "程序文件/native/LibreOffice/LibreOffice.app/Contents/Frameworks/LibreOfficePython.framework/Versions/3.13/lib/python3.13/ctypes/macholib/fetch_macholib.bat": {
        "bytes": 74,
        "sha256": "cda880175e916ece98346966b5e081d91f420b5c7c0eeb8443690f2c79b49809",
        "source": "https://github.com/python/cpython/blob/v3.13.18/Lib/ctypes/macholib/fetch_macholib.bat",
    },
}


def is_upstream_support_resource(name: str, data: bytes) -> bool:
    spec = UPSTREAM_SUPPORT_RESOURCES.get(name)
    return bool(spec and len(data) == spec["bytes"]
                and hashlib.sha256(data).hexdigest() == spec["sha256"])


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def version_of(project: Path) -> str:
    tree = ast.parse((project / "qingjian/__init__.py").read_text(encoding="utf-8-sig"))
    value = next((ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)), None)
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)+", value):
        raise RuntimeError("源码版本号无效。")
    return value


def macos_guide(project: Path) -> bytes:
    preferred = Path(project) / "MACOS_README.txt"
    return (preferred if preferred.is_file() else Path(project) / "macos_build/使用说明-Mac.txt").read_bytes()


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _light_support(relative: Path) -> bool:
    if relative.parts[0] != "support":
        return True
    if len(relative.parts) < 3:
        return False
    if relative.parts[1] == "macos" and len(relative.parts) == 3:
        return relative.suffix.lower() in {".txt", ".md"} or relative.name in {"Ghostscript-COPYING", "LICENSE", "COPYING", "NOTICE"}
    if relative.parts[2] in {"licenses", "source"}:
        return True
    if relative.parts[1] == "OFD" and len(relative.parts) == 3:
        return relative.name in {"bridge.jar", "build-info.json", "library-origins.json", "source-origins.json", "使用与重建说明.txt"}
    return relative.parts[1] == "7zip" and relative.name in {"License.txt", "History.txt", "readme.txt"}


def source_files(project: Path = PROJECT) -> list[Path]:
    project = Path(project).resolve()
    selected = []
    for directory, subdirs, names in os.walk(project, followlinks=False):
        base = Path(directory)
        for name in list(subdirs):
            entry = base / name
            if _is_link(entry):
                raise RuntimeError("源码不接受链接或重解析目录：" + str(entry.relative_to(project)))
        subdirs[:] = sorted(name for name in subdirs if name not in EXCLUDED_DIRS and not name.startswith(".env-"))
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(project)
            if _is_link(path):
                raise RuntimeError("源码不接受链接文件：" + relative.as_posix())
            if name in GENERATED_NAMES or path.suffix.lower() in WINDOWS_SUFFIXES | ARCHIVE_SUFFIXES | {".pyc", ".pyo", ".log", ".dmp"}:
                continue
            if name.startswith(".env") or name == ".DS_Store" or not _light_support(relative):
                continue
            # Old Windows launcher source is retained in the working checkout,
            # not in this Mac distribution. The Java bridge is platform neutral.
            if relative.parts[0] == "launcher" and len(relative.parts) > 1 and relative.parts[1] != "ofd_bridge":
                if not (path.name.endswith(".command") or path.suffix.lower() in {".py", ".sh"}):
                    continue
            if path.stat().st_size > 12 * 1024 * 1024:
                raise RuntimeError("轻量源码中出现大型文件，请检查发行内容：" + relative.as_posix())
            selected.append(path)
    return selected


def source_contract(project: Path) -> None:
    required = (
        "main.py", "bootstrap_macos.py", "startup_macos_native.py",
        "macos-runtime-sources.json", "macos-native-sources.json",
        "launcher/start_macos.command", "requirements.txt", "bootstrap-wheels.json",
        "AGENTS.md", "LICENSE.txt", "build_macos.py", "package_macos.py",
        "macos_build/start_offline.command", "macos_build/offline_start.py",
        "macos_build/verify_macos.py", "macos_build/使用说明-Mac.txt",
        "macos_build/verify_native_macos.py",
        "macos_build/第三方组件说明-Mac.txt",
        "support/macos/Ghostscript-COPYING", "support/macos/原生组件来源与许可.txt",
    )
    missing = [name for name in required if not (project / name).is_file()]
    if missing:
        raise RuntimeError("Mac 启动或构建文件缺失：" + "、".join(missing))
    launcher = (project / "launcher/start_macos.command").read_text(encoding="utf-8-sig")
    if not launcher.startswith("#!") or "\x00" in launcher:
        raise RuntimeError("Mac 启动器必须为可执行文本脚本。")


def _zip_info(name: str, mode: int, *, link: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = ((stat.S_IFLNK if link else stat.S_IFREG) | mode) << 16
    return info


def _write_bytes(archive: zipfile.ZipFile, name: str, data: bytes, mode: int = 0o644) -> None:
    if name.endswith((".command", ".sh")):
        data = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        mode = 0o755
    archive.writestr(_zip_info(name, mode), data)


def _publish(staged: Path, target: Path) -> None:
    # Atomic no-replace publication also works for very large archives.
    os.link(staged, target)


def verify_archive(path: Path, *, source: bool) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("ZIP 内有重复路径。")
        if {name.split("/")[0] for name in names} != ROOT_ENTRIES:
            raise RuntimeError("解压后根目录必须只有启动器、说明和程序文件三项。")
        for info in archive.infolist():
            rel = Path(info.filename)
            if info.filename.startswith("/") or ".." in rel.parts or "\\" in info.filename:
                raise RuntimeError("ZIP 路径不安全：" + info.filename)
            if rel.suffix.lower() in WINDOWS_SUFFIXES:
                spec = UPSTREAM_SUPPORT_RESOURCES.get(info.filename)
                if (source or not spec or info.file_size != spec["bytes"]
                        or not is_upstream_support_resource(info.filename, archive.read(info))):
                    raise RuntimeError("Mac 发行包中包含 Windows 文件：" + info.filename)
        entry = archive.getinfo("启动青简工具箱.command")
        if (entry.external_attr >> 16) & 0o111 != 0o111 or b"\r" in archive.read(entry):
            raise RuntimeError("Mac 启动器必须保留 Unix 可执行权限与 LF 换行。")
        bad = archive.testzip()
        if bad:
            raise RuntimeError("ZIP 校验失败：" + bad)
        if source and any(name.startswith(("程序文件/Python/", "程序文件/native/", "程序文件/runtime/")) for name in names):
            raise RuntimeError("轻量源码中混入了大型运行环境。")
        return {"members": len(names), "uncompressed_bytes": sum(info.file_size for info in archive.infolist()),
                "root_entries": sorted(ROOT_ENTRIES), "unix_launcher": True}


def package_source(project: Path = PROJECT, output: Path | None = None) -> dict:
    project = Path(project).resolve()
    source_contract(project)
    version = version_of(project)
    target = Path(output).resolve() if output else project.parent / f"QingjianToolbox-v{version}-macOS-Source.zip"
    if target == project or project in target.parents:
        raise RuntimeError("发行 ZIP 必须放在源码文件夹以外。")
    if target.exists():
        raise FileExistsError("保留已有发行包，请使用新路径：" + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qingjian-mac-source-", dir=target.parent) as temporary:
        staged = Path(temporary) / "source.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source in source_files(project):
                relative = source.relative_to(project).as_posix()
                data = macos_guide(project) if relative == "使用说明.txt" else source.read_bytes()
                if relative == "第三方组件说明.txt":
                    data = (project / "macos_build/第三方组件说明-Mac.txt").read_bytes()
                _write_bytes(archive, "程序文件/" + relative, data)
            _write_bytes(archive, "启动青简工具箱.command", (project / "launcher/start_macos.command").read_bytes())
            _write_bytes(archive, "使用说明.txt", macos_guide(project))
            marker = {"schema_version": 1, "mode": "source", "version": version,
                      "platform": "macos", "architectures": ["arm64", "x86_64"], "minimum_macos": "14.4"}
            _write_bytes(archive, "程序文件/macos-package.json", json.dumps(marker, ensure_ascii=False, indent=2).encode("utf-8"))
        check = verify_archive(staged, source=True)
        result = {"version": version, "mode": "macos-source", "bytes": staged.stat().st_size,
                  "sha256": digest(staged), "archive": str(target), **check}
        _publish(staged, target)
    return result


def package_full(bundle: Path, output: Path, *, require_validation: bool = True) -> dict:
    bundle, target = Path(bundle).resolve(), Path(output).resolve()
    if bundle == target or bundle in target.parents:
        raise RuntimeError("发行 ZIP 必须放在完整包文件夹以外。")
    if target.exists():
        raise FileExistsError("保留已有发行包，请使用新路径：" + str(target))
    marker = json.loads((bundle / "程序文件/macos-package.json").read_text(encoding="utf-8"))
    if marker.get("mode") != "offline" or marker.get("platform") != "macos":
        raise RuntimeError("完整包元信息不正确。")
    if require_validation:
        report = json.loads((bundle / "程序文件/macos-build-validation.json").read_text(encoding="utf-8"))
        if report.get("passed") is not True or report.get("platform") != "darwin" or report.get("architecture") != marker.get("architecture"):
            raise RuntimeError("须先在对应架构的真实 macOS 上完成完整包验收。")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qingjian-mac-full-", dir=target.parent) as temporary:
        staged = Path(temporary) / "full.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for directory, subdirs, names in os.walk(bundle, followlinks=False):
                base = Path(directory)
                for name in sorted(subdirs + names):
                    path = base / name
                    relative = path.relative_to(bundle).as_posix()
                    if path.is_symlink():
                        destination = os.readlink(path)
                        resolved = path.resolve(strict=True)
                        if not resolved.is_relative_to(bundle) or os.path.isabs(destination):
                            raise RuntimeError("完整包链接指向包外：" + relative)
                        archive.writestr(_zip_info(relative, 0o777, link=True), destination.encode("utf-8"))
                    elif path.is_file():
                        mode = stat.S_IMODE(path.stat().st_mode)
                        # Stream large native runtimes rather than loading them in memory.
                        info = _zip_info(relative, mode)
                        with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as destination:
                            import shutil
                            shutil.copyfileobj(source, destination, length=4 * 1024 * 1024)
                subdirs[:] = sorted(name for name in subdirs if not (base / name).is_symlink())
        check = verify_archive(staged, source=False)
        result = {"version": marker["version"], "mode": "macos-offline", "architecture": marker["architecture"],
                  "archive": str(target), "bytes": staged.stat().st_size, "sha256": digest(staged), **check}
        _publish(staged, target)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--full-bundle", type=Path)
    arguments = parser.parse_args()
    if arguments.full_bundle:
        if not arguments.output:
            parser.error("--full-bundle 需要 --output")
        result = package_full(arguments.full_bundle, arguments.output)
    else:
        result = package_source(arguments.project, arguments.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
