"""Build a real, relocatable macOS offline folder using prepared components.

Run on the target Mac architecture. This intentionally refuses cross-building
with Windows Python, copying /usr/bin Python, or downloading during packaging.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys

from package_macos import ROOT_ENTRIES, WINDOWS_SUFFIXES, macos_guide, source_contract, source_files, version_of

PROJECT = Path(__file__).resolve().parent
MACHO_MAGICS = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
DYLIB_LOAD_COMMANDS = {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB",
                       "LC_LAZY_LOAD_DYLIB", "LC_LOAD_UPWARD_DYLIB"}
JAVA_CLASS_LIMIT = 16 * 1024 * 1024


def architecture(value: str | None = None) -> str:
    value = (value or platform.machine()).lower()
    if value in {"arm64", "aarch64"}:
        return "arm64"
    if value in {"x86_64", "amd64"}:
        return "x86_64"
    raise RuntimeError("仅支持 Apple 芯片 arm64 和 Intel x86_64。")


def run(arguments: list[str], **kwargs) -> str:
    result = subprocess.run(arguments, check=True, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=kwargs.pop("timeout", 180), **kwargs)
    return result.stdout


def runtime_origin(runtime: Path, arch: str) -> dict:
    receipt = json.loads((runtime / ".qingjian-runtime.json").read_text(encoding="utf-8"))
    if receipt.get("architecture") != arch or receipt.get("kind") != "python-build-standalone":
        raise RuntimeError("完整包需要本架构经过校验的可移动 Python，不能直接复制系统 Python。")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", str(receipt.get("sha256", ""))) or not isinstance(receipt.get("size"), int) or receipt["size"] <= 0:
        raise RuntimeError("Python 来源收据缺少固定大小或 SHA256。")
    # Keep portable provenance, never the build machine's selected directory.
    allowed = {"kind", "architecture", "sha256", "size", "version", "release", "url", "source", "license"}
    return {key: value for key, value in receipt.items() if key in allowed}


def verify_links(root: Path) -> int:
    count = 0
    for directory, subdirs, names in os.walk(root, followlinks=False):
        for name in subdirs + names:
            item = Path(directory) / name
            if not item.is_symlink():
                continue
            if os.path.isabs(os.readlink(item)) or not item.resolve(strict=True).is_relative_to(root):
                raise RuntimeError("运行环境链接指向包外：" + str(item.relative_to(root)))
            count += 1
        subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
    return count


def loaded_dylibs(load_commands: str) -> list[str]:
    """Read dependency commands, excluding the library's own LC_ID_DYLIB.

    otool -L also prints the install ID, which can legitimately be a bare
    filename. Classifying the actual load command avoids exempting a genuine
    dependency merely because it happens to share the library's own name.
    """
    result = []
    awaiting_name = None
    for raw in load_commands.splitlines():
        line = raw.strip()
        if line.startswith("Load command "):
            if awaiting_name:
                raise RuntimeError("Mach-O 动态库依赖命令缺少名称：" + awaiting_name)
            awaiting_name = None
        elif match := re.fullmatch(r"cmd (LC_[A-Z0-9_]+)", line):
            if awaiting_name:
                raise RuntimeError("Mach-O 动态库依赖命令缺少名称：" + awaiting_name)
            command = match.group(1)
            awaiting_name = command if command in DYLIB_LOAD_COMMANDS else None
        elif awaiting_name and line.startswith("name "):
            match = re.fullmatch(r"name (.+) \(offset \d+\)", line)
            if not match:
                raise RuntimeError("无法读取 Mach-O 动态库依赖名称：" + line)
            result.append(match.group(1))
            awaiting_name = None
    if awaiting_name:
        raise RuntimeError("Mach-O 动态库依赖命令缺少名称：" + awaiting_name)
    return result


def java_class_header(data: bytes) -> bool:
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        return False
    minor, major, constants = struct.unpack_from(">HHH", data, 4)
    return major >= 45 and (major < 56 or minor in {0, 65535}) and constants >= 2


def is_java_class(data: bytes) -> bool:
    """Recognize a bounded class-file structure, not a .class filename.

    Java and universal Mach-O share CAFEBABE. Validate the constant pool and
    every outer class/member/attribute boundary through EOF before skipping a
    Java resource. This is a file-type check, not a Java bytecode verifier.
    """
    if len(data) > JAVA_CLASS_LIMIT or not java_class_header(data):
        return False
    _, major, count = struct.unpack_from(">HHH", data, 4)
    position = 10
    pool = [None] * count

    def take(length):
        nonlocal position
        if length < 0 or position + length > len(data):
            raise ValueError("Truncated class file")
        result = data[position:position + length]
        position += length
        return result

    def number(size):
        return int.from_bytes(take(size), "big")

    def reference(index, tags):
        if not 0 < index < count or pool[index] is None or pool[index][0] not in tags:
            raise ValueError("Invalid class constant reference")

    def attributes():
        for _ in range(number(2)):
            reference(number(2), {1})
            take(number(4))

    def members():
        for _ in range(number(2)):
            number(2)  # access flags
            reference(number(2), {1})
            reference(number(2), {1})
            attributes()

    try:
        index = 1
        while index < count:
            tag = number(1)
            if tag == 1:
                value = take(number(2))
                # JVM CONSTANT_Utf8 uses modified UTF-8: NUL is C0 80 and
                # supplementary characters use surrogate pairs, not 4 bytes.
                if b"\x00" in value or any(byte >= 0xf0 for byte in value):
                    return False
                value.replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass")
            elif tag in {3, 4, 5, 6}:
                value = take(8 if tag in {5, 6} else 4)
            elif tag in {7, 8, 16, 19, 20}:
                value = (number(2),)
            elif tag in {9, 10, 11, 12, 17, 18}:
                value = (number(2), number(2))
            elif tag == 15:
                value = (number(1), number(2))
            else:
                return False
            if ((tag in {15, 16, 18} and major < 51) or (tag == 17 and major < 55)
                    or (tag in {19, 20} and major < 53)):
                return False
            pool[index] = (tag, value)
            index += 2 if tag in {5, 6} else 1
            if index > count:
                return False
        for entry in pool[1:]:
            if entry is None:
                continue  # reserved second slot of a long/double
            tag, value = entry
            if tag in {7, 8, 16, 19, 20}:
                reference(value[0], {1})
            elif tag in {9, 10, 11}:
                reference(value[0], {7})
                reference(value[1], {12})
            elif tag == 12:
                reference(value[0], {1})
                reference(value[1], {1})
            elif tag in {17, 18}:
                reference(value[1], {12})  # first value is a bootstrap index
            elif tag == 15:
                kind, target = value
                if not 1 <= kind <= 9:
                    return False
                tags = {9} if kind <= 4 else ({11} if kind == 9 else {10})
                if kind in {6, 7} and major >= 52:
                    tags = {10, 11}
                reference(target, tags)
        number(2)  # class access flags
        reference(number(2), {7})
        superclass = number(2)
        if superclass:
            reference(superclass, {7})
        for _ in range(number(2)):
            reference(number(2), {7})
        members()  # fields
        members()  # methods
        attributes()
        return position == len(data)
    except (ValueError, IndexError, UnicodeError, struct.error):
        return False


def portable_binary_dependencies(root: Path, arch: str) -> dict:
    """Reject foreign architecture and undeclared absolute dylib dependencies.

    macOS system libraries remain provided by macOS. @rpath/loader/executable
    references are left to dyld and exercised by the actual offline launch and
    conversion checks; no install_name_tool rewrite is hidden in packaging.
    """
    checked = 0
    problems = []
    for item in sorted(root.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        with item.open("rb") as stream:
            header = stream.read(4)
            if header == b"\xca\xfe\xba\xbe":
                prefix = header + stream.read(6)
                if java_class_header(prefix) and is_java_class(prefix + stream.read(JAVA_CLASS_LIMIT + 1 - len(prefix))):
                    continue
        if header not in MACHO_MAGICS:
            continue
        checked += 1
        relative = str(item.relative_to(root))
        try:
            arches = run(["/usr/bin/lipo", "-archs", str(item)]).strip().split()
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append("无法检查原生文件架构：" + relative + "：" + str(exc))
            continue
        if arch not in arches:
            problems.append("包含其他架构的原生文件：" + relative + " (" + " ".join(arches) + ")")
            continue
        try:
            dependencies = loaded_dylibs(run(["/usr/bin/otool", "-arch", arch, "-l", str(item)]))
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            problems.append("无法检查原生文件依赖：" + relative + "：" + str(exc))
            continue
        for dependency in dependencies:
            if dependency.startswith(("/usr/lib/", "/System/Library/", "@rpath/", "@loader_path/", "@executable_path/")):
                continue
            problems.append("依赖包外的动态库，无法离线分发：" + relative + " -> " + dependency)
    if problems:
        raise RuntimeError("完整包原生文件检查未通过（" + str(len(problems)) + "项）：\n" + "\n".join(problems))
    if checked == 0:
        raise RuntimeError("运行环境中没有经过验证的 Mach-O 文件。")
    return {"macho_files": checked, "architecture": arch, "external_absolute_libraries": 0}


def sanitize_runtime(runtime: Path) -> None:
    # The original prepared shared runtime remains untouched.
    runtime = Path(runtime).resolve()
    # The application does not use QtSql. These optional plugins target
    # database client SDKs (Mimer/PostgreSQL/ODBC, etc.) that the wheel need not
    # bundle. Remove the complete unused SQL-driver group in the private copy;
    # leave Qt modules, normal UI plugins and all license files unchanged.
    for site in (runtime / "lib").glob("python*/site-packages"):
        drivers = site / "PySide6/Qt/plugins/sqldrivers"
        if not drivers.exists() and not drivers.is_symlink():
            continue
        if (not drivers.resolve(strict=True).is_relative_to(runtime) or not drivers.is_dir()
                or any(path.is_symlink() for path in (drivers, *drivers.parents) if path != runtime and runtime in path.parents)):
            raise RuntimeError("可选 Qt SQL 插件目录不是包内普通目录：" + str(drivers))
        shutil.rmtree(drivers)
    for item in sorted(runtime.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if item.is_symlink():
            continue
        if item.is_dir() and item.name in {"__pycache__", ".pytest_cache"}:
            shutil.rmtree(item)
        elif item.is_file() and item.suffix.lower() in {".pyc", ".pyo"} | WINDOWS_SUFFIXES:
            item.unlink()
        elif item.is_file() and item.suffix == ".pth":
            lines = item.read_text(encoding="utf-8").splitlines()
            if any(line.strip() and not line.lstrip().startswith(("#", "import ", "import\t")) and Path(line.strip()).is_absolute() for line in lines):
                raise RuntimeError("运行环境包含指向外部的 .pth：" + str(item.relative_to(runtime)))
    # Console scripts generated by pip include build-machine shebangs. Runtime
    # libraries need none of them; Python itself and versioned symlinks remain.
    removed_scripts = set()
    for item in (runtime / "bin").iterdir():
        if item.is_symlink() or not item.is_file() or item.name.startswith("python"):
            continue
        with item.open("rb") as stream:
            header = stream.read(2)
        if header == b"#!":
            removed_scripts.add(item.resolve())
            item.unlink()
    # PBS ships idle3/pydoc3 aliases to versioned scripts. Their build-path
    # shebang targets were intentionally removed above; remove only aliases
    # resolving to those exact files, while preserving Python and every link
    # that still needs verification against the bundle boundary.
    aliases = [item for item in (runtime / "bin").iterdir()
               if item.is_symlink() and item.resolve(strict=False) in removed_scripts]
    for item in aliases:
        item.unlink()


def verify_python_runtime_licenses(project: Path, arch: str, origin: dict) -> dict:
    folder = Path(project) / "support/macos/licenses/python"
    manifest_path = folder / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 262144:
        raise RuntimeError("Python 原生依赖许可清单无效。")
    metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = metadata.get("runtimes", {}).get(arch, {})
    if (metadata.get("schema_version") != 1 or not expected
            or origin.get("sha256") != expected.get("sha256") or origin.get("size") != expected.get("bytes")
            or origin.get("version") != metadata.get("python_version")):
        raise RuntimeError("Python 运行时版本与原生依赖许可材料不一致。")
    rows = metadata.get("files", [])
    if not isinstance(rows, list) or not 14 <= len(rows) <= 64:
        raise RuntimeError("Python 原生依赖许可材料不完整。")
    names = set()
    for row in rows:
        name = row.get("file", "")
        if not name or name in names or Path(name).name != name or "/" in name or "\\" in name:
            raise RuntimeError("Python 许可文件路径无效。")
        path = folder / name
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != row.get("bytes")
                or hashlib.file_digest(path.open("rb"), "sha256").hexdigest() != row.get("sha256")):
            raise RuntimeError("Python 许可文件缺失或校验失败：" + name)
        names.add(name)
    if any(component.get("license_file") not in names for component in metadata.get("components", {}).values()):
        raise RuntimeError("Python 依赖有未附带的许可文件。")
    return {"manifest": "support/macos/licenses/python/manifest.json", "files": len(rows),
            "python_version": metadata["python_version"], "build_release": metadata["build_release"]}


def export_component_inventory(program: Path, project: Path, arch: str) -> dict:
    """Ask the copied interpreter, rather than recording the Windows build set."""
    script = r'''
import importlib.metadata as m, json, re, sys
from pathlib import Path
runtime = Path(sys.prefix).resolve()
requirements = Path(sys.argv[1]).read_text(encoding="utf-8-sig")
direct = {re.match(r"[A-Za-z0-9_.-]+", line.strip()).group().lower().replace("_", "-")
          for line in requirements.splitlines() if line.strip() and not line.lstrip().startswith("#")}
packages = []
for dist in sorted(m.distributions(), key=lambda row: row.metadata.get("Name", "").lower()):
    name = dist.metadata.get("Name", "unknown")
    license_text = dist.metadata.get("License-Expression") or dist.metadata.get("License", "")
    if "\n" in license_text or len(license_text) > 180:
        license_text = "See bundled license files"
    paths = []
    for relative in dist.files or []:
        text = str(relative).lower()
        if not any(word in text for word in ("license", "licence", "copying", "copyright", "notice")):
            continue
        path = Path(dist.locate_file(relative)).resolve()
        if path.is_file() and path.is_relative_to(runtime):
            paths.append(path.relative_to(runtime).as_posix())
    packages.append({"name": name, "version": dist.version, "license_identifier": license_text,
                     "direct_requirement": name.lower().replace("_", "-") in direct,
                     "license_files": sorted(set(paths))})
license_file = runtime / ("lib/python%d.%d/LICENSE.txt" % sys.version_info[:2])
if not license_file.is_file():
    raise RuntimeError("Portable Python LICENSE.txt is missing")
print(json.dumps({"python": {"version": sys.version.split()[0], "license_file": license_file.relative_to(runtime).as_posix()},
                  "packages": packages}, ensure_ascii=False))
'''
    result = json.loads(run([str(program / "Python/bin/python3"), "-I", "-B", "-c", script,
                             str(project / "requirements.txt")]))
    result["python"]["native_dependency_licenses"] = verify_python_runtime_licenses(
        program, arch, runtime_origin(program / "Python", arch))
    from startup_macos_native import load_manifest
    native = load_manifest(project, architecture=arch)
    result.update({"schema_version": 1, "platform": "macos", "architecture": arch,
                   "native_components": {}})
    for key, spec in native["components"].items():
        artifact = native["artifacts"][spec["artifact"]]
        result["native_components"][key] = {
            "version": artifact.get("version", artifact["filename"]), "sha256": artifact["sha256"],
            "license_directory": "native/" + key + "/licenses",
            "libraries": [{"artifact": name, "sha256": native["artifacts"][name]["sha256"]}
                          for name in spec.get("libraries", [])],
        }
    vendored = project / "lunar_python/来源.json"
    if vendored.is_file():
        source = json.loads(vendored.read_text(encoding="utf-8-sig"))
        result["vendored_packages"] = [{key: source[key] for key in ("name", "version", "license", "upstream", "sha256") if key in source}]
    (program / "第三方组件清单-Mac.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (program / "第三方组件说明.txt").write_bytes((project / "macos_build/第三方组件说明-Mac.txt").read_bytes())
    return result


def build(output: Path, runtime: Path, native_home: Path, project: Path = PROJECT, *, arch: str | None = None) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("Mac 完整运行包必须在真实 macOS 上构建；Windows 只可生成轻量源码包。")
    arch = architecture(arch)
    if architecture() != arch:
        raise RuntimeError("请使用与目标相同架构的 macOS 构建环境。")
    project, runtime, native_home, output = map(lambda path: Path(path).resolve(), (project, runtime, native_home, output))
    if project == output or project in output.parents or runtime == output or runtime in output.parents:
        raise RuntimeError("构建输出目录不能位于源码或共享运行环境内。")
    source_contract(project)
    origin = runtime_origin(runtime, arch)
    python = runtime / "bin/python3"
    if not python.is_file():
        raise RuntimeError("可移动 Python 缺失 bin/python3。")
    verify_links(runtime)
    if run([str(python), "-I", "-c", "import platform; print(platform.machine())"]).strip() != arch:
        raise RuntimeError("Python 实际运行架构与目标不一致。")
    from startup_macos_native import check_native, check_redistribution_materials
    native = check_native(project, native_home, architecture=arch)
    if not native.get("ready"):
        raise RuntimeError("先完成原生环境准备，缺失：" + str(native.get("missing")))
    redistribution = check_redistribution_materials(project, native_home, architecture=arch)
    if not redistribution.get("ready"):
        raise RuntimeError("先准备完整包的许可与对应源码材料：" + str(redistribution.get("missing")))
    version = version_of(project)
    bundle = output / f"青简工具箱_v{version}_macOS_{arch}"
    if bundle.exists():
        raise FileExistsError("不覆盖既有构建，请使用新输出目录：" + str(bundle))
    program = bundle / "程序文件"
    program.mkdir(parents=True)
    for source in source_files(project):
        relative = source.relative_to(project)
        destination = program / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copytree(runtime, program / "Python", symlinks=True)
    sanitize_runtime(program / "Python")
    for name, root in native["components"].items():
        if name not in {"LibreOffice", "7zip", "Ghostscript", "OFD"}:
            raise RuntimeError("意外的原生组件：" + name)
        shutil.copytree(Path(root), program / "native" / name, symlinks=True)
    # root source README is deliberately identical to the Mac-specific guide.
    (bundle / "使用说明.txt").write_bytes(macos_guide(project))
    (program / "使用说明.txt").write_bytes(macos_guide(project))
    command = (project / "macos_build/start_offline.command").read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    (bundle / "启动青简工具箱.command").write_text(command, encoding="utf-8", newline="\n")
    (bundle / "启动青简工具箱.command").chmod(0o755)
    marker = {"schema_version": 1, "mode": "offline", "platform": "macos", "version": version,
              "architecture": arch, "minimum_macos": "14.4", "runtime": "Python/bin/python3"}
    (program / "macos-package.json").write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    (program / "Python/运行时来源.json").write_text(json.dumps(origin, ensure_ascii=False, indent=2), encoding="utf-8")
    inventory = export_component_inventory(program, project, arch)
    checked_native = check_native(program, program, architecture=arch)
    if not checked_native.get("ready"):
        raise RuntimeError("复制后的原生组件未通过离线检查：" + str(checked_native.get("missing")))
    if not check_redistribution_materials(program, program, architecture=arch).get("ready"):
        raise RuntimeError("复制后的许可与对应源码材料校验失败。")
    links = verify_links(bundle)
    binaries = portable_binary_dependencies(program, arch)
    if {item.name for item in bundle.iterdir()} != ROOT_ENTRIES:
        raise RuntimeError("完整包根目录必须恰好三项。")
    # Every installed dependency is checked by the packaged interpreter, with
    # PYTHONPATH/user-site/system site unavailable. Bootstrap is forced offline.
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    environment.update({"PYTHONNOUSERSITE": "1", "QINGJIAN_OFFLINE": "1"})
    run([str(program / "Python/bin/python3"), "-I", "-B", str(program / "main.py"), "--check-environment", "--offline"], env=environment, timeout=240)
    result = {"version": version, "architecture": arch, "folder": str(bundle), "platform": "darwin",
              "root_entries": sorted(ROOT_ENTRIES), "relative_symlinks": links, "runtime_origin": origin, **binaries}
    result["python_packages"] = len(inventory["packages"])
    (program / "macos-build-inventory.json").write_text(json.dumps({key: value for key, value in result.items() if key != "folder"}, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--native-home", required=True, type=Path)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--architecture", choices=("arm64", "x86_64"))
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.runtime, args.native_home, args.project, arch=args.architecture), ensure_ascii=False, indent=2))
