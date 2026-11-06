"""Create a verified source ZIP; the startup contract is mandatory for every release."""
import argparse
import ast
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path

from bootstrap import DEPENDENCY_INSTALL_POLICY, load_manifest, requirements
from build import verify_source_startup

PROJECT = Path(__file__).resolve().parent
EXCLUDED = {"__pycache__", ".pytest_cache", ".git", ".venv", "venv", "build", "dist", "engines", ".mypy_cache"}
ROOT_ENTRIES = {"启动青简工具箱.exe", "使用说明.txt", "程序文件"}


def light_support_file(relative):
    """Keep only bridge/source/notices; runtime bytes are fetched at startup."""
    parts = relative.parts
    if parts[0] != 'support':
        return True
    if relative.as_posix() == 'support/component-checksums.json':
        return True
    if len(parts) < 3:
        return False
    if parts[2] in {'licenses', 'source'}:
        return True
    if parts[1] == 'OFD' and parts[2] in {
        'bridge.jar', 'build-info.json', 'library-origins.json', 'source-origins.json', '使用与重建说明.txt'
    }:
        return len(parts) == 3
    return parts[1] == '7zip' and len(parts) == 3 and parts[2] in {'License.txt', 'History.txt', 'readme.txt'}


def source_files(project):
    files = []
    for path in sorted(project.rglob("*")):
        relative = path.relative_to(project)
        if relative.parts[0] == 'runtime':
            continue
        if relative.parts[:3] == ('launcher', 'ofd_bridge', 'target'):
            continue
        if relative.as_posix() == "config/startup-selection.json":
            continue
        if relative.as_posix() == 'cmd-package.json':
            continue  # Generated per distribution mode, never copied as input.
        if not light_support_file(relative):
            continue
        if any(part in EXCLUDED or part.startswith(".env-") for part in relative.parts):
            continue
        attributes = path.lstat().st_file_attributes if os.name == "nt" else 0
        if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise RuntimeError("源码包不接受链接或重解析目录：" + str(relative))
        excluded_suffixes = {'.pyc', '.pyo', '.log'} | ({'.zip'} if relative.parts[0] != 'support' else set())
        if path.is_file() and path.suffix.lower() not in excluded_suffixes:
            files.append(path)
    return files


def package(project=PROJECT, output=None, *, entry_mode="exe"):
    project = Path(project).resolve()
    verify_source_startup(project, require_exe=entry_mode != 'cmd')
    load_manifest(project)
    requirements(project)
    tree = ast.parse((project / "qingjian" / "__init__.py").read_text(encoding="utf-8-sig"))
    version = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets))
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+)+", version):
        raise RuntimeError("源码版本号无效。")
    if entry_mode not in {"exe", "cmd"}:
        raise ValueError("启动方式应为exe或cmd。")
    if entry_mode == "cmd":
        for name in ("launcher/start_source.cmd", "launcher/prepare_python.cmd", "launcher/cmd_source.py"):
            if not (project / name).is_file():
                raise RuntimeError("CMD源码入口缺失：" + name)
    target = Path(output).resolve() if output else project.parent / ("青简工具箱_v" + version + ("_CMD源码.zip" if entry_mode == "cmd" else "_源码.zip"))
    if target == project or project in target.parents:
        raise RuntimeError("源码ZIP请放在源码目录以外。")
    if target.exists():
        raise FileExistsError("保留已发布文件，请使用新的版本或输出路径：" + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    inventory = []
    with tempfile.TemporaryDirectory(prefix="qingjian-source-", dir=target.parent) as temporary:
        staged = Path(temporary) / "source.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in source_files(project):
                data = path.read_bytes()
                relative = path.relative_to(project).as_posix()
                if entry_mode == "cmd" and path.suffix.lower() == ".exe":
                    continue
                # Both release types expose the same three root entries. The
                # launcher also lives inside the source tree for direct main.py
                # first-run setup and reproducible development builds.
                name = project.name + "/程序文件/" + relative
                archive.writestr(name, data)
                inventory.append({"name": name, "source": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                if relative in ({"使用说明.txt"} if entry_mode == "cmd" else {"启动青简工具箱.exe", "使用说明.txt"}):
                    name = project.name + "/" + relative
                    archive.writestr(name, data)
                    inventory.append({"name": name, "source": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            if entry_mode == "cmd":
                data = (project / 'launcher/start_source.cmd').read_bytes()
                name = project.name + '/启动青简工具箱.cmd'
                archive.writestr(name, data)
                inventory.append({'name': name, 'source': 'launcher/start_source.cmd', 'bytes': len(data),
                                  'sha256': hashlib.sha256(data).hexdigest()})
                marker = json.dumps({'schema_version': 1, 'mode': 'source', 'version': version}, ensure_ascii=False).encode('utf-8')
                name = project.name + '/程序文件/cmd-package.json'
                archive.writestr(name, marker)
                inventory.append({'name': name, 'source': None, 'bytes': len(marker),
                                  'sha256': hashlib.sha256(marker).hexdigest()})
        with zipfile.ZipFile(staged) as archive:
            if set(archive.namelist()) != {row["name"] for row in inventory}:
                raise RuntimeError("源码ZIP成员不一致。")
            for row in inventory:
                data = archive.read(row["name"])
                current = project / row["source"] if row['source'] is not None else None
                if hashlib.sha256(data).hexdigest() != row["sha256"] or (current is not None and current.read_bytes() != data):
                    raise RuntimeError("打包期间源码变化或ZIP校验失败，请重试。")
            entries = {name.split("/")[1] for name in archive.namelist()}
            expected_entries = {"启动青简工具箱.cmd", "使用说明.txt", "程序文件"} if entry_mode == "cmd" else ROOT_ENTRIES
            if entries != expected_entries:
                raise RuntimeError("源码包根目录必须只有启动器、使用说明、程序文件三个项目。")
        record = {"version": version, "archive": str(target), "files": len(inventory),
                  "bytes": staged.stat().st_size, "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                  "source_inventory": inventory, "startup_contract_verified": True,
                  "root_entries": sorted(expected_entries), "entry_mode": entry_mode,
                  "dependency_install_policy": DEPENDENCY_INSTALL_POLICY, "passed": True}
        # A hard link publishes the completed file without replacing an older release.
        os.link(staged, target)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="校验自动补齐要求并生成轻量源码ZIP，不覆盖旧版。")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--entry-mode", choices=['exe', 'cmd'], default='exe')
    arguments = parser.parse_args()
    result = package(output=arguments.output, entry_mode=arguments.entry_mode)
    print(json.dumps({key: value for key, value in result.items() if key != "source_inventory"}, ensure_ascii=False, indent=2))
