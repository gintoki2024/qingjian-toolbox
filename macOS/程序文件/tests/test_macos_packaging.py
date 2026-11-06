"""Mac ZIP invariants are testable without pretending to run macOS binaries."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import stat
import zipfile

import pytest

import build_macos


UPSTREAM_FETCH_TEXT = b'svn export --force http://svn.red-bean.com/bob/macholib/trunk/macholib/ .\n'


@pytest.mark.parametrize('variant', ['original', 'changed', 'renamed', 'source'])
def test_only_exact_upstream_signed_app_resource_is_accepted(tmp_path, variant):
    name = next(iter(package.UPSTREAM_SUPPORT_RESOURCES))
    data = UPSTREAM_FETCH_TEXT
    if variant == 'changed':
        data = b'X' + data[1:]
    if variant == 'renamed':
        name = name.replace('fetch_macholib.bat', 'other.bat')
    path = tmp_path / 'resource.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        package._write_bytes(archive, '启动青简工具箱.command', b'#!/bin/bash\nexit 0\n')
        package._write_bytes(archive, '使用说明.txt', b'fixture')
        package._write_bytes(archive, name, data)
    if variant == 'original':
        assert package.verify_archive(path, source=False)['members'] == 3
    else:
        with pytest.raises(RuntimeError, match='Windows'):
            package.verify_archive(path, source=variant == 'source')


def test_native_scan_preserves_and_records_verified_upstream_text(tmp_path, monkeypatch):
    if os.name == 'nt':
        tmp_path = Path('\\\\?\\' + str(tmp_path))
    name = next(iter(package.UPSTREAM_SUPPORT_RESOURCES))
    resource = tmp_path / name.removeprefix('程序文件/')
    resource.parent.mkdir(parents=True)
    resource.write_bytes(UPSTREAM_FETCH_TEXT)
    (tmp_path / 'python3').write_bytes(b'\xcf\xfa\xed\xfe')
    monkeypatch.setattr(build_macos, 'run', lambda command: 'arm64\n' if 'lipo' in command[0] else
                        otool_load_commands(('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib')))
    result = build_macos.portable_binary_dependencies(tmp_path, 'arm64')
    assert result['macho_files'] == 1 and len(result['upstream_support_resources']) == 1
    assert resource.read_bytes() == UPSTREAM_FETCH_TEXT
    resource.write_bytes(b'X' + UPSTREAM_FETCH_TEXT[1:])
    with pytest.raises(RuntimeError, match='Windows'):
        build_macos.portable_binary_dependencies(tmp_path, 'arm64')
import package_macos as package


@pytest.fixture
def source(tmp_path):
    project = tmp_path / "Mac Source 中文"
    project.mkdir()
    for name in (
        "main.py", "bootstrap_macos.py", "startup_macos_native.py",
        "macos-runtime-sources.json", "macos-native-sources.json",
        "requirements.txt", "bootstrap-wheels.json", "AGENTS.md", "LICENSE.txt",
        "build_macos.py", "package_macos.py", "macos_build/offline_start.py",
        "macos_build/verify_macos.py", "macos_build/使用说明-Mac.txt",
        "macos_build/verify_native_macos.py",
        "macos_build/第三方组件说明-Mac.txt",
        "support/macos/Ghostscript-COPYING", "support/macos/原生组件来源与许可.txt",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if name.endswith(".json") else "example\n", encoding="utf-8")
    for name in ("launcher/start_macos.command", "macos_build/start_offline.command"):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"#!/bin/bash\r\nprintf 'hello'\r\n")
    version = project / "qingjian/__init__.py"
    version.parent.mkdir()
    version.write_text('__version__ = "0.12.0"\n', encoding="utf-8")
    return project


def test_source_zip_is_three_flat_root_entries_with_executable_lf_launcher(source, tmp_path):
    output = tmp_path / "release.zip"
    record = package.package_source(source, output)
    assert record["mode"] == "macos-source" and record["version"] == "0.12.0"
    assert record["root_entries"] == sorted(package.ROOT_ENTRIES)
    assert record["sha256"] == package.digest(output)
    with zipfile.ZipFile(output) as archive:
        assert {name.split("/")[0] for name in archive.namelist()} == package.ROOT_ENTRIES
        launcher = archive.getinfo("启动青简工具箱.command")
        assert launcher.create_system == 3
        assert stat.S_IMODE(launcher.external_attr >> 16) == 0o755
        assert b"\r" not in archive.read(launcher)
        marker = json.loads(archive.read("程序文件/macos-package.json"))
        assert marker["architectures"] == ["arm64", "x86_64"]
        assert "程序文件/support/macos/Ghostscript-COPYING" in archive.namelist()
        assert "程序文件/support/macos/原生组件来源与许可.txt" in archive.namelist()


def test_source_excludes_user_files_downloads_windows_and_engine_bytes(source, tmp_path):
    forbidden = [
        "config/startup-selection.json", "runtime/logs/pip.log", "native/7zip/7zz",
        "engines/LibreOffice/app", "Python/bin/python3", ".venv/lib/value.py",
        "qingjian/__pycache__/app.pyc", "launcher/启动.exe", "launcher/task.cmd",
        "support/OFD/large-runtime.tar.gz", "support/Ghostscript/bin/gs.exe",
    ]
    for name in forbidden:
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"private engine bytes")
    bridge = source / "support/OFD/bridge.jar"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_bytes(b"portable Java bridge")
    license_file = source / "support/OFD/licenses/NOTICE.txt"
    license_file.parent.mkdir(parents=True)
    license_file.write_text("notice", encoding="utf-8")
    output = tmp_path / "light.zip"
    package.package_source(source, output)
    with zipfile.ZipFile(output) as archive:
        assert not {"程序文件/" + name for name in forbidden}.intersection(archive.namelist())
        assert "程序文件/support/OFD/bridge.jar" in archive.namelist()
        assert "程序文件/support/OFD/licenses/NOTICE.txt" in archive.namelist()


def test_source_does_not_replace_old_release(source, tmp_path):
    target = tmp_path / "existing.zip"
    target.write_bytes(b"published")
    with pytest.raises(FileExistsError):
        package.package_source(source, target)
    assert target.read_bytes() == b"published"


def test_source_uses_current_mac_readme_as_root_guide(source, tmp_path):
    (source / "MACOS_README.txt").write_text("当前的 Mac 使用说明", encoding="utf-8")
    output = tmp_path / "guide.zip"
    package.package_source(source, output)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("使用说明.txt").decode("utf-8") == "当前的 Mac 使用说明"


def test_source_archive_cannot_be_written_under_input(source):
    with pytest.raises(RuntimeError, match="以外"):
        package.package_source(source, source / "new.zip")


def test_source_contract_checks_no_python_launcher(source):
    (source / "launcher/start_macos.command").unlink()
    with pytest.raises(RuntimeError, match="start_macos.command"):
        package.source_contract(source)


def test_large_source_engine_is_rejected(source):
    giant = source / "accidental-runtime.bin"
    with giant.open("wb") as stream:
        stream.truncate(12 * 1024 * 1024 + 1)
    with pytest.raises(RuntimeError, match="大型文件"):
        package.source_files(source)


def test_version_is_parsed_without_running_source_code(source):
    (source / "qingjian/__init__.py").write_text('raise AssertionError("do not execute")\n__version__="0.12.1"\n', encoding="utf-8")
    assert package.version_of(source) == "0.12.1"


def test_full_packaging_requires_real_mac_validation(tmp_path):
    bundle = tmp_path / "offline"
    program = bundle / "程序文件"
    program.mkdir(parents=True)
    (program / "macos-package.json").write_text(json.dumps({"platform": "macos", "mode": "offline", "version": "0.12.0", "architecture": "arm64"}), encoding="utf-8")
    (bundle / "启动青简工具箱.command").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (bundle / "启动青简工具箱.command").chmod(0o755)
    (bundle / "使用说明.txt").write_text("guide", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        package.package_full(bundle, tmp_path / "full.zip")
    (program / "macos-build-validation.json").write_text(json.dumps({"passed": True, "platform": "win32", "architecture": "arm64"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="真实 macOS"):
        package.package_full(bundle, tmp_path / "full.zip")
    assert not (tmp_path / "full.zip").exists()


def test_build_refuses_windows_before_touching_output(tmp_path, monkeypatch):
    monkeypatch.setattr(build_macos.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="真实 macOS"):
        build_macos.build(tmp_path / "out", tmp_path / "python", tmp_path / "home")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(("name", "expected"), [("aarch64", "arm64"), ("arm64", "arm64"), ("AMD64", "x86_64"), ("x86_64", "x86_64")])
def test_architecture_aliases(name, expected):
    assert build_macos.architecture(name) == expected


def test_runtime_origin_requires_verified_relocatable_python(tmp_path):
    receipt = tmp_path / ".qingjian-runtime.json"
    receipt.write_text(json.dumps({"architecture": "arm64", "kind": "system-python"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="可移动 Python"):
        build_macos.runtime_origin(tmp_path, "arm64")
    receipt.write_text(json.dumps({"architecture": "arm64", "kind": "python-build-standalone", "sha256": "a" * 64, "size": 123,
                                   "path": "/Users/private/build", "version": "3.12"}), encoding="utf-8")
    result = build_macos.runtime_origin(tmp_path, "arm64")
    assert "path" not in result and result["sha256"] == "a" * 64


def test_runtime_sanitizer_removes_unused_windows_pip_stubs_and_absolute_wrappers(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/python3").write_bytes(b"binary")
    (tmp_path / "bin/pytest").write_bytes(b"#!/Users/private/python/bin/python3\n")
    site = tmp_path / "lib/python3.13/site-packages"
    (site / "pip/_vendor/distlib").mkdir(parents=True)
    (site / "pip/_vendor/distlib/w64.exe").write_bytes(b"unused Windows launcher stub")
    (site / "runtime.py").write_text("value = 1", encoding="utf-8")
    build_macos.sanitize_runtime(tmp_path)
    assert (tmp_path / "bin/python3").is_file()
    assert not (tmp_path / "bin/pytest").exists()
    assert not (site / "pip/_vendor/distlib/w64.exe").exists()
    assert (site / "runtime.py").is_file()


def test_sanitizer_removes_only_optional_qt_sql_drivers_from_the_private_copy(tmp_path):
    import shutil
    shared = tmp_path / "shared-runtime"
    (shared / "bin").mkdir(parents=True)
    (shared / "bin/python3").write_bytes(b"python")
    site = Path("lib/python3.13/site-packages")
    drivers = site / "PySide6/Qt/plugins/sqldrivers"
    for name in ("libqsqlmimer.dylib", "libqsqlpsql.dylib", "libqsqlodbc.dylib", "libqsqlite.dylib"):
        file = shared / drivers / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"optional database plugin")
    kept = (site / "PySide6/Qt/plugins/platforms/libqcocoa.dylib", site / "PySide6/QtSql.abi3.so",
            site / "PySide6_Essentials.dist-info/licenses/QtSql-LICENSE.txt", site / "otherpackage/sqldrivers/plugin.py")
    for relative in kept:
        file = shared / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"keep")
    private = tmp_path / "private-runtime"
    shutil.copytree(shared, private)
    build_macos.sanitize_runtime(private)
    assert not (private / drivers).exists()
    assert all((private / relative).read_bytes() == b"keep" for relative in kept)
    assert len(list((shared / drivers).glob("*.dylib"))) == 4


def test_product_does_not_depend_on_removed_qt_sql_plugins():
    import ast
    package_root = Path(build_macos.__file__).parent / "qingjian"
    for path in package_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("PySide6.QtSql") for alias in node.names), path
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("PySide6.QtSql"), path
                assert not (node.module == "PySide6" and any(alias.name == "QtSql" for alias in node.names)), path
        assert "QSqlDatabase" not in source and "PySide6.QtSql" not in source, path


def test_sanitizer_does_not_follow_qt_driver_symlink_outside_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    outside = tmp_path / "shared-driver-files"
    outside.mkdir()
    (outside / "libqsqlmimer.dylib").write_bytes(b"shared original")
    plugins = runtime / "lib/python3.13/site-packages/PySide6/Qt/plugins"
    plugins.mkdir(parents=True)
    try:
        (plugins / "sqldrivers").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip("Host cannot create test symlinks: " + str(exc))
    with pytest.raises(RuntimeError, match="包内普通目录"):
        build_macos.sanitize_runtime(runtime)
    assert (outside / "libqsqlmimer.dylib").read_bytes() == b"shared original"


def test_component_inventory_uses_packaged_interpreter_and_actual_metadata(tmp_path, monkeypatch):
    program = tmp_path / "程序文件"
    program.mkdir()
    commands = []
    actual = {"python": {"version": "3.13.18", "license_file": "lib/python3.13/LICENSE.txt"},
              "packages": [{"name": "python-docx", "version": "1.2.0", "license_identifier": "MIT",
                            "direct_requirement": True, "license_files": ["lib/python3.13/site-packages/python_docx-1.2.0.dist-info/licenses/LICENSE"]}]}

    def metadata_probe(command):
        commands.append(command)
        assert command[1:4] == ["-I", "-B", "-c"]
        import ast
        ast.parse(command[4])
        return json.dumps(actual)

    monkeypatch.setattr(build_macos, "run", metadata_probe)
    project = Path(build_macos.__file__).parent
    licenses = project / 'support/macos/licenses/python'
    shutil.copytree(licenses, program / 'support/macos/licenses/python')
    metadata = json.loads((licenses / 'manifest.json').read_text(encoding='utf-8'))
    expected = metadata['runtimes']['arm64']
    (program / 'Python').mkdir()
    (program / 'Python/.qingjian-runtime.json').write_text(json.dumps({
        'kind': 'python-build-standalone', 'architecture': 'arm64', 'version': metadata['python_version'],
        'sha256': expected['sha256'], 'size': expected['bytes']}), encoding='utf-8')
    inventory = build_macos.export_component_inventory(program, project, "arm64")
    assert Path(commands[0][0]) == program / "Python/bin/python3"
    assert inventory["packages"] == actual["packages"]
    assert set(inventory["native_components"]) == {"LibreOffice", "7zip", "Ghostscript", "OFD"}
    assert "lib/python3.13/LICENSE.txt" in (program / "第三方组件清单-Mac.json").read_text(encoding="utf-8")
    assert "Windows" in (program / "第三方组件说明.txt").read_text(encoding="utf-8")


def otool_load_commands(*entries):
    rows = ["/portable/libexample.dylib:"]
    for index, (command, name) in enumerate(entries):
        rows.extend([f"Load command {index}", "          cmd " + command, "      cmdsize 64",
                     "         name " + name + " (offset 24)",
                     "   time stamp 1 Thu Jan  1 00:00:01 1970", "      current version 1.0.0"])
    return "\n".join(rows) + "\n"


def test_dynamic_library_scan_rejects_external_absolute_path(tmp_path, monkeypatch):
    binary = tmp_path / "python3"
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"x" * 20)
    monkeypatch.setattr(build_macos, "run", lambda cmd: "arm64\n" if "lipo" in cmd[0] else otool_load_commands(("LC_LOAD_DYLIB", "/opt/homebrew/lib/libbad.dylib")))
    with pytest.raises(RuntimeError, match="包外"):
        build_macos.portable_binary_dependencies(tmp_path, "arm64")


def test_dynamic_library_scan_accepts_system_and_relative_dependencies(tmp_path, monkeypatch):
    binary = tmp_path / "python3"
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"x" * 20)
    monkeypatch.setattr(build_macos, "run", lambda cmd: "arm64 x86_64\n" if "lipo" in cmd[0] else otool_load_commands(("LC_LOAD_DYLIB", "@rpath/libpython.dylib"), ("LC_LOAD_DYLIB", "/usr/lib/libSystem.B.dylib")))
    assert build_macos.portable_binary_dependencies(tmp_path, "arm64")["macho_files"] == 1


def test_dynamic_library_scan_rejects_wrong_architecture(tmp_path, monkeypatch):
    binary = tmp_path / "python3"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(build_macos, "run", lambda command: "x86_64\n")
    with pytest.raises(RuntimeError, match="其他架构"):
        build_macos.portable_binary_dependencies(tmp_path, "arm64")


@pytest.mark.parametrize("install_id", ["libitcl4.3.8.dylib", "/original/build/libitcl4.3.8.dylib", "@rpath/libitcl4.3.8.dylib"])
def test_dynamic_library_install_id_is_not_a_loaded_dependency(tmp_path, monkeypatch, install_id):
    binary = tmp_path / "libitcl4.3.8.dylib"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    calls = []

    def inspect(command):
        calls.append(command)
        if "lipo" in command[0]:
            return "arm64\n"
        return otool_load_commands(("LC_ID_DYLIB", install_id), ("LC_LOAD_DYLIB", "/usr/lib/libSystem.B.dylib"))

    monkeypatch.setattr(build_macos, "run", inspect)
    assert build_macos.portable_binary_dependencies(tmp_path, "arm64")["macho_files"] == 1
    assert calls[-1] == ["/usr/bin/otool", "-arch", "arm64", "-l", str(binary)]


@pytest.mark.parametrize("dependency", ["libitcl4.3.8.dylib", "libother.dylib", "/outside/libother.dylib"])
def test_install_id_does_not_whitelist_a_genuine_loaded_name(tmp_path, monkeypatch, dependency):
    binary = tmp_path / "libitcl4.3.8.dylib"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    output = otool_load_commands(("LC_ID_DYLIB", "libitcl4.3.8.dylib"), ("LC_LOAD_DYLIB", dependency))
    monkeypatch.setattr(build_macos, "run", lambda command: "arm64\n" if "lipo" in command[0] else output)
    with pytest.raises(RuntimeError, match="包外"):
        build_macos.portable_binary_dependencies(tmp_path, "arm64")


def test_genuine_absolute_load_of_the_binary_itself_is_not_exempted(tmp_path, monkeypatch):
    binary = tmp_path / "libexample.dylib"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    output = otool_load_commands(("LC_LOAD_DYLIB", str(binary)))
    monkeypatch.setattr(build_macos, "run", lambda command: "arm64\n" if "lipo" in command[0] else output)
    with pytest.raises(RuntimeError, match="包外"):
        build_macos.portable_binary_dependencies(tmp_path, "arm64")


@pytest.mark.parametrize("command", sorted(build_macos.DYLIB_LOAD_COMMANDS))
def test_load_command_parser_covers_all_dylib_dependency_types(command):
    output = otool_load_commands(("LC_ID_DYLIB", "self.dylib"), (command, "@loader_path/a library.dylib"))
    assert build_macos.loaded_dylibs(output) == ["@loader_path/a library.dylib"]


def test_load_command_parser_ignores_rpaths_and_architecture_headers():
    output = otool_load_commands(("LC_ID_DYLIB", "own.dylib")) + "Load command 7\n cmd LC_RPATH\n path @loader_path/../lib (offset 12)\n"
    output += "/portable/libexample.dylib (architecture x86_64):\n"
    output += otool_load_commands(("LC_ID_DYLIB", "own.dylib"), ("LC_LOAD_DYLIB", "/usr/lib/libSystem.B.dylib"))
    assert build_macos.loaded_dylibs(output) == ["/usr/lib/libSystem.B.dylib"]


@pytest.mark.parametrize("output", ["Load command 1\n cmd LC_LOAD_DYLIB\n", "Load command 1\n cmd LC_LOAD_DYLIB\nLoad command 2\n cmd LC_UUID\n",
                                   "Load command 1\n cmd LC_LOAD_DYLIB\n name missing-offset\n"])
def test_load_command_parser_fails_if_dependency_name_cannot_be_read(output):
    with pytest.raises(RuntimeError, match="名称"):
        build_macos.loaded_dylibs(output)


def test_binary_scan_aggregates_all_external_libraries_and_wrong_architectures(tmp_path, monkeypatch):
    names = ("a.dylib", "b.dylib", "c-wrong-arch.dylib")
    for name in names:
        (tmp_path / name).write_bytes(b"\xcf\xfa\xed\xfe")
    calls = []

    def inspect(command):
        name = Path(command[-1]).name
        calls.append(name)
        if "lipo" in command[0]:
            return "x86_64\n" if name == "c-wrong-arch.dylib" else "arm64\n"
        if name == "a.dylib":
            return otool_load_commands(("LC_LOAD_DYLIB", "/usr/local/lib/libmimerapi.dylib"),
                                       ("LC_LOAD_WEAK_DYLIB", "libanother.dylib"))
        return otool_load_commands(("LC_REEXPORT_DYLIB", "/opt/homebrew/lib/libother.dylib"))

    monkeypatch.setattr(build_macos, "run", inspect)
    with pytest.raises(RuntimeError) as caught:
        build_macos.portable_binary_dependencies(tmp_path, "arm64")
    message = str(caught.value)
    assert "4项" in message
    assert "a.dylib -> /usr/local/lib/libmimerapi.dylib" in message
    assert "a.dylib -> libanother.dylib" in message
    assert "b.dylib -> /opt/homebrew/lib/libother.dylib" in message
    assert "包含其他架构的原生文件：c-wrong-arch.dylib (x86_64)" in message
    assert all(name in calls for name in names)


def test_binary_scan_reports_tool_errors_and_continues_to_other_files(tmp_path, monkeypatch):
    for name in ("bad-tool.dylib", "bad-dependency.dylib"):
        (tmp_path / name).write_bytes(b"\xcf\xfa\xed\xfe")

    def inspect(command):
        if Path(command[-1]).name == "bad-tool.dylib":
            raise OSError("fixture inspection tool failure")
        return "arm64\n" if "lipo" in command[0] else otool_load_commands(("LC_LOAD_DYLIB", "libnotbundled.dylib"))

    monkeypatch.setattr(build_macos, "run", inspect)
    with pytest.raises(RuntimeError) as caught:
        build_macos.portable_binary_dependencies(tmp_path, "arm64")
    assert "bad-tool.dylib：fixture inspection tool failure" in str(caught.value)
    assert "bad-dependency.dylib -> libnotbundled.dylib" in str(caught.value)


def minimal_java_class():
    import struct
    utf8 = lambda text: b"\x01" + struct.pack(">H", len(text)) + text
    pool = utf8(b"Sample") + b"\x07\x00\x01" + utf8(b"java/lang/Object") + b"\x07\x00\x03"
    return b"\xca\xfe\xba\xbe" + struct.pack(">HHH", 0, 61, 5) + pool + struct.pack(">HHHHHHH", 0x21, 2, 4, 0, 0, 0, 0)


def test_class_structure_recognizes_actual_bundled_java_resources():
    project = Path(build_macos.__file__).parent
    files = sorted((project / "support/OFD/licenses/bcprov-jdk18on-1.85").glob("*.class"))
    assert {path.name for path in files} == {"LICENSE.class", "NoticeReference.class"}
    assert all(build_macos.is_java_class(path.read_bytes()) for path in files)
    assert build_macos.is_java_class(minimal_java_class())


def test_binary_scan_skips_valid_java_without_using_the_filename(tmp_path, monkeypatch):
    (tmp_path / "java-resource.bin").write_bytes(minimal_java_class())
    (tmp_path / "native.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
    project = Path(build_macos.__file__).parent
    for path in (project / "support/OFD/licenses/bcprov-jdk18on-1.85").glob("*.class"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    calls = []

    def inspect(command):
        calls.append(command)
        assert Path(command[-1]).name == "native.dylib"
        return "arm64\n" if "lipo" in command[0] else otool_load_commands(("LC_LOAD_DYLIB", "/usr/lib/libSystem.B.dylib"))

    monkeypatch.setattr(build_macos, "run", inspect)
    assert build_macos.portable_binary_dependencies(tmp_path, "arm64")["macho_files"] == 1
    assert len(calls) == 2


@pytest.mark.parametrize("universal", [False, True])
def test_real_macho_renamed_class_still_receives_architecture_checks(tmp_path, monkeypatch, universal):
    import struct
    # A complete empty MH_OBJECT header; the FAT case wraps the same x86_64
    # object with a valid architecture table and aligned payload offset.
    payload = struct.pack("<IiiIIIII", 0xfeedfacf, 0x1000007, 3, 1, 0, 0, 0, 0)
    if universal:
        payload = struct.pack(">IIIIIII", 0xcafebabe, 1, 0x1000007, 3, 28, len(payload), 2) + payload
    binary = tmp_path / "real-native.class"
    binary.write_bytes(payload)
    calls = []

    def inspect(command):
        calls.append(command)
        return "x86_64\n"

    monkeypatch.setattr(build_macos, "run", inspect)
    with pytest.raises(RuntimeError, match="包含其他架构"):
        build_macos.portable_binary_dependencies(tmp_path, "arm64")
    assert calls[0] == ["/usr/bin/lipo", "-archs", str(binary)]


def test_macho_renamed_class_still_receives_dependency_checks(tmp_path, monkeypatch):
    binary = tmp_path / "native.class"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(build_macos, "run", lambda command: "arm64\n" if "lipo" in command[0] else otool_load_commands(("LC_LOAD_DYLIB", "libexternal.dylib")))
    with pytest.raises(RuntimeError, match="native.class -> libexternal.dylib"):
        build_macos.portable_binary_dependencies(tmp_path, "arm64")


@pytest.mark.parametrize("damage", ["truncated", "extra", "bad_tag", "bad_reference", "bad_attribute", "bad_version"])
def test_malformed_java_lookalike_is_not_skipped(tmp_path, monkeypatch, damage):
    data = bytearray(minimal_java_class())
    if damage == "truncated":
        data = data[:-1]
    elif damage == "extra":
        data += b"unparsed bytes"
    elif damage == "bad_tag":
        data[10] = 0xff
    elif damage == "bad_reference":
        data[-12:-10] = b"\xff\xff"  # this_class must reference CONSTANT_Class
    elif damage == "bad_attribute":
        data[-2:] = b"\x00\x01"  # one declared class attribute but no payload
    elif damage == "bad_version":
        data[6:8] = b"\x00\x02"  # not a Java class version
    assert not build_macos.is_java_class(bytes(data))
    binary = tmp_path / "malformed.class"
    binary.write_bytes(data)
    calls = []

    def fail_inspection(command):
        calls.append(command)
        raise OSError("not a valid Mach-O file")

    monkeypatch.setattr(build_macos, "run", fail_inspection)
    with pytest.raises(RuntimeError, match="无法检查原生文件架构"):
        build_macos.portable_binary_dependencies(tmp_path, "arm64")
    assert calls, "Malformed CAFEBABE resources must not be silently ignored"


def test_workflow_is_only_manual_and_does_not_publish():
    workflow = (Path(package.__file__).parent / "macos_build/workflows/build-macos.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow and "timeout-minutes: 35" in workflow
    assert "macos-15-intel" in workflow and "retention-days: 3" in workflow
    assert "contents: read" in workflow and "contents: write" not in workflow
    assert "gh release" not in workflow and "pull_request:" not in workflow and "  push:" not in workflow


def test_verify_script_refuses_non_mac(tmp_path):
    import importlib.util
    path = Path(package.__file__).parent / "macos_build/verify_macos.py"
    spec = importlib.util.spec_from_file_location("qingjian_verify_macos_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.sys.platform != "darwin":
        with pytest.raises(RuntimeError, match="真实 Mac"):
            module.coordinator(tmp_path, tmp_path / "evidence")


def test_python_runtime_license_materials_match_fixed_runtime():
    project = Path(package.__file__).parent
    metadata = json.loads((project / 'support/macos/licenses/python/manifest.json').read_text(encoding='utf-8'))
    for arch, expected in metadata['runtimes'].items():
        origin = {'version': metadata['python_version'], 'sha256': expected['sha256'], 'size': expected['bytes']}
        record = build_macos.verify_python_runtime_licenses(project, arch, origin)
        assert record['files'] >= 14
        with pytest.raises(RuntimeError, match='不一致'):
            build_macos.verify_python_runtime_licenses(project, arch, {**origin, 'sha256': '0' * 64})


def test_source_contains_runtime_native_license_notices(source, tmp_path):
    folder = source / 'support/macos/licenses/python'
    folder.mkdir(parents=True)
    (folder / 'LICENSE.openssl-3.txt').write_text('fictional license for package inclusion test', encoding='utf-8')
    output = tmp_path / 'license-notices.zip'
    package.package_source(source, output)
    with zipfile.ZipFile(output) as archive:
        assert '程序文件/support/macos/licenses/python/LICENSE.openssl-3.txt' in archive.namelist()


def test_sanitize_removes_only_aliases_to_removed_console_scripts(tmp_path):
    binary = tmp_path / 'bin'
    binary.mkdir()
    (binary / 'python3.13').write_bytes(b'\xcf\xfa\xed\xfe' + b'python')
    (binary / 'idle3.13').write_bytes(b'#!/build/path/python3\nprint("idle")\n')
    try:
        (binary / 'idle3').symlink_to('idle3.13')
        (binary / 'idle-alias').symlink_to('idle3')
        (binary / 'python3').symlink_to('python3.13')
        (binary / 'unknown-tool').symlink_to('missing-unrelated')
    except OSError as exc:
        pytest.skip('Host cannot create test symlinks: ' + str(exc))
    build_macos.sanitize_runtime(tmp_path)
    assert not (binary / 'idle3.13').exists() and not (binary / 'idle3').is_symlink()
    assert not (binary / 'idle-alias').is_symlink()
    assert (binary / 'python3').is_symlink() and (binary / 'python3').is_file()
    assert (binary / 'unknown-tool').is_symlink()
