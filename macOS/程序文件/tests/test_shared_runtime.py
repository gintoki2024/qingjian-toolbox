"""Cross-version runtime memory; every path and index belongs to a fixture."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import startup_shared as shared

from startup_fixtures import real_venv


PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    """Discovery must never consume the developer's actual startup memory."""
    for name in ("QINGJIAN_RUNTIME_HOME", "QINGJIAN_CMD_PYTHON", "QINGJIAN_CMD_SELECTION_DONE",
                 "QINGJIAN_BOOTSTRAP_READY", "QINGJIAN_LIBREOFFICE", "QINGJIAN_NATIVE_COMPONENTS"):
        monkeypatch.delenv(name, raising=False)
    index = tmp_path / "仅测试索引" / "shared-runtime.json"
    monkeypatch.setenv("QINGJIAN_SHARED_RUNTIME_INDEX", str(index))
    monkeypatch.setenv("QINGJIAN_USER_DATA", str(tmp_path / "仅测试用户数据"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "仅测试AppData"))
    return index


def source_at(base, version="0.10.3", *, wrapped=True):
    root = base / ("青简工具箱_v" + version + "_CMD源码_修正版") if wrapped else base
    project = root / "青简工具箱源码" / "程序文件"
    project.mkdir(parents=True, exist_ok=True)
    # Markers are data: discovery must not import or execute an older project.
    (project / "bootstrap.py").write_text("raise AssertionError('old source executed')\n", encoding="utf-8")
    (project / "main.py").write_text("raise AssertionError('old app executed')\n", encoding="utf-8")
    shutil.copy2(PROJECT / "runtime-sources.json", project / "runtime-sources.json")
    return project


def python_at(base):
    path = base / "用户已装Python" / "python.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture interpreter; discovery must not execute it")
    return path


def engine_at(root):
    program = root / "program"
    program.mkdir(parents=True, exist_ok=True)
    for name in ("soffice.com", "soffice.bin", "fundamental.ini"):
        (program / name).write_bytes(b"fixture")
    return root


def choice(project, home, python, *, mode="reuse"):
    value = {"schema_version": 1, "mode": mode, "python": str(python), "runtime_home": str(home)}
    path = project / "config" / "startup-selection.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value


def entry(project, python, home, *, engine=None, mode="reuse", updated=1):
    return {"project": str(project), "python": str(python), "mode": mode, "runtime_home": str(home),
            "engine": str(engine or home / "lo-26.2.6"), "native_components": {}, "updated_ns": updated}


def write_index(index, entries, *, schema=1):
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps({"schema_version": schema, "entries": entries}, ensure_ascii=False), encoding="utf-8")


def load_cmd_launcher():
    spec = importlib.util.spec_from_file_location("shared_runtime_cmd_under_test", PROJECT / "launcher/cmd_source.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("relative", [
    "青简工具箱源码",
    "青简工具箱源码/程序文件",
    "青简工具箱_v0.10.3_CMD源码/青简工具箱源码/程序文件",
    "青简工具箱_v0.10.4_CMD源码_独立启动修正版/青简工具箱源码/程序文件",
    "青简工具箱_v0.10.4_源码/青简工具箱_v0.10.4_CMD源码/青简工具箱源码/程序文件",
])
def test_default_shared_home_survives_source_and_zip_wrapper_changes(tmp_path, relative):
    project = tmp_path / "所选D盘目录" / Path(relative)
    expected = tmp_path / "所选D盘目录" / "青简共享环境"
    assert shared.default_runtime_home(project) == expected
    assert not expected.exists()


def test_catalog_override_is_only_a_location_without_creating_files(isolated_memory):
    assert shared.catalog_path() == isolated_memory
    assert not isolated_memory.parent.exists()


def test_catalog_uses_explicit_user_data_before_localappdata(tmp_path, monkeypatch):
    monkeypatch.delenv("QINGJIAN_SHARED_RUNTIME_INDEX")
    expected = tmp_path / "自选非C盘用户数据" / "shared-runtime.json"
    monkeypatch.setenv("QINGJIAN_USER_DATA", str(expected.parent))
    assert shared.catalog_path() == expected
    assert not expected.parent.exists()


def test_catalog_has_small_metadata_fallback_without_environment_deployment(tmp_path, monkeypatch):
    monkeypatch.delenv("QINGJIAN_SHARED_RUNTIME_INDEX")
    monkeypatch.delenv("QINGJIAN_USER_DATA")
    expected = tmp_path / "仅测试AppData" / "QingjianLocal" / "shared-runtime.json"
    assert shared.catalog_path() == expected
    assert not expected.parent.exists()


def test_empty_memory_discovery_does_not_create_selection_or_runtime(tmp_path, isolated_memory):
    project = source_at(tmp_path / "全新解压")
    before = set(tmp_path.rglob("*"))
    assert shared.saved_selection(project) is None
    assert shared.component_locations(project) == []
    shared.default_runtime_home(project)
    assert set(tmp_path.rglob("*")) == before
    assert not isolated_memory.exists()


def test_deleted_old_release_keeps_external_runtime_available(tmp_path, isolated_memory):
    old = source_at(tmp_path / "版本目录", "0.10.3")
    python = python_at(tmp_path)
    home = tmp_path / "D盘共享环境"
    engine = engine_at(home / "lo-26.2.6")
    assert shared.remember_runtime(old, python, home, engine, {})
    old_wrapper = old.parents[1]
    assert old_wrapper.resolve().is_relative_to(tmp_path.resolve())
    shutil.rmtree(old_wrapper)
    new = source_at(tmp_path / "其他解压位置", "0.10.4")
    saved = shared.saved_selection(new)
    assert saved == {"schema_version": 1, "mode": "reuse", "python": str(python), "runtime_home": str(home)}
    assert engine.is_dir()
    assert any(Path(row["runtime_home"]) == home for row in shared.component_locations(new))
    assert not (new / "runtime").exists()


def test_catalog_remembers_each_component_location_without_copying_it(tmp_path, isolated_memory):
    project = source_at(tmp_path)
    python = python_at(tmp_path)
    home = tmp_path / "外部组件"
    engine = engine_at(home / "lo-26.2.6")
    native = home / "native/7zip-fixture"
    native.mkdir(parents=True)
    (native / "7z.exe").write_bytes(b"discovery metadata, not verified by this test")
    assert shared.remember_runtime(project, python, home, engine, {"7zip": str(native)})
    rows = shared.component_locations(project)
    assert any(Path(row["engine"]) == engine and row["native_components"].get("7zip") == str(native) for row in rows)
    assert not (project / "runtime").exists()


def test_managed_runtime_selection_is_preserved_without_system_python_discovery(tmp_path, isolated_memory):
    old = source_at(tmp_path / "旧版目录", "0.10.3")
    new = source_at(tmp_path / "新版目录", "0.10.4")
    home = tmp_path / "非C盘专用环境"
    python = python_at(home)
    engine = engine_at(home / "lo-26.2.6")
    assert shared.remember_runtime(old, python, home, engine, {}, mode="managed")
    assert shared.saved_selection(new) == {"schema_version": 1, "mode": "managed", "python": str(python),
                                           "runtime_home": str(home)}


def test_repeated_remember_deduplicates_and_bounds_the_catalog(tmp_path, isolated_memory):
    python = python_at(tmp_path)
    project = source_at(tmp_path)
    for number in range(19):
        home = tmp_path / ("共享环境" + str(number))
        engine = engine_at(home / "lo-26.2.6")
        assert shared.remember_runtime(project, python, home, engine, {})
        assert shared.remember_runtime(project, python, home, engine, {})
    payload = json.loads(isolated_memory.read_text(encoding="utf-8-sig"))
    assert payload["schema_version"] == 1
    assert len(payload["entries"]) <= 16
    assert len({(row["runtime_home"], row["python"]) for row in payload["entries"]}) == len(payload["entries"])


@pytest.mark.parametrize("payload", [
    b"not-json",
    b"[]",
    b'{"schema_version":2,"entries":[]}',
    b'{"schema_version":1,"entries":{}}',
    b"x" * (1024 * 1024),
])
def test_corrupt_or_oversized_memory_is_optional_and_never_rewritten(tmp_path, isolated_memory, payload):
    project = source_at(tmp_path / "全新版本")
    isolated_memory.parent.mkdir(parents=True)
    isolated_memory.write_bytes(payload)
    assert shared.saved_selection(project) is None
    assert shared.component_locations(project) == []
    assert isolated_memory.read_bytes() == payload
    assert not (project / "runtime").exists()


@pytest.mark.parametrize("damage", ["missing-home", "missing-python", "relative-home", "unknown-mode"])
def test_invalid_entry_does_not_hide_an_older_usable_selection(tmp_path, isolated_memory, damage):
    project = source_at(tmp_path)
    python = python_at(tmp_path)
    home = tmp_path / "仍可用的共享环境"
    engine_at(home / "lo-26.2.6")
    usable = entry(project, python, home, updated=10)
    broken = dict(usable, updated_ns=20)
    if damage == "missing-home":
        broken["runtime_home"] = str(tmp_path / "已删除运行目录")
    elif damage == "missing-python":
        broken["python"] = str(tmp_path / "不存在的Python.exe")
    elif damage == "relative-home":
        broken["runtime_home"] = "relative/runtime"
    else:
        broken["mode"] = "silently-install-anywhere"
        unexpected_home = tmp_path / "未知模式不能启用此目录"
        engine_at(unexpected_home / "lo-26.2.6")
        broken["runtime_home"] = str(unexpected_home)
    write_index(isolated_memory, [broken, usable])
    saved = shared.saved_selection(project)
    assert saved and Path(saved["runtime_home"]) == home
    assert Path(saved["python"]) == python


def test_legacy_source_with_custom_external_home_is_found_without_registration(tmp_path, isolated_memory):
    old = source_at(tmp_path / "发布目录", "0.10.2")
    new = source_at(tmp_path / "发布目录", "0.10.4")
    python = python_at(tmp_path)
    home = tmp_path / "用户选择的E盘" / "办公共享环境"
    engine_at(home / "lo-26.2.6")
    expected = choice(old, home, python)
    assert shared.saved_selection(new) == expected
    assert shared.saved_selection(new, discover_legacy=False) is None
    assert any(Path(row["runtime_home"]) == home for row in shared.component_locations(new))
    assert not isolated_memory.exists()


def test_legacy_source_private_runtime_is_a_component_hint_not_a_future_default(tmp_path, isolated_memory):
    old = source_at(tmp_path / "发布目录", "0.10.2")
    new = source_at(tmp_path / "发布目录", "0.10.4")
    python = python_at(tmp_path)
    private = old / "runtime"
    engine_at(private / "lo-26.2.6")
    choice(old, private, python)
    assert shared.saved_selection(new) is None
    assert any(Path(row["runtime_home"]) == private for row in shared.component_locations(new))
    assert shared.default_runtime_home(new) == tmp_path / "发布目录" / "青简共享环境"
    assert not isolated_memory.exists()


@pytest.mark.parametrize("location", ["source-wrapper", "zip-wrapper"])
def test_old_runtime_in_an_outer_package_wrapper_is_only_a_migration_source(tmp_path, isolated_memory, location):
    old = source_at(tmp_path / "发布目录", "0.10.2")
    new = source_at(tmp_path / "发布目录", "0.10.4")
    python = python_at(tmp_path)
    private = old.parent / "运行环境" if location == "source-wrapper" else old.parents[1] / "runtime"
    engine = engine_at(private / "lo-26.2.6")
    choice(old, private, python)
    assert shared.remember_runtime(old, python, private, engine, {})
    assert shared.saved_selection(new) is None
    assert any(Path(row["runtime_home"]) == private for row in shared.component_locations(new))
    assert not shared.default_runtime_home(new).exists()


@pytest.mark.parametrize("location", ["source-wrapper", "zip-wrapper"])
def test_local_runtime_inside_a_package_wrapper_moves_its_choice_but_keeps_the_copy_source(tmp_path, location):
    project = source_at(tmp_path / "当前解压目录")
    python = python_at(tmp_path)
    private = project.parent / "运行环境" if location == "source-wrapper" else project.parents[1] / "runtime"
    engine = engine_at(private / "lo-26.2.6")
    choice(project, private, python)
    launcher = load_cmd_launcher()
    args = launcher.parse_args(["--project", str(project)])
    saved = launcher.selection_for(args, read=lambda _: pytest.fail("unexpected prompt"), output=lambda _: None)
    external = shared.default_runtime_home(project)
    assert Path(saved["runtime_home"]) == external
    assert Path(saved["python"]) == python
    assert saved["mode"] == "reuse"
    assert not external.exists()
    assert engine.is_dir()
    assert any(Path(row["runtime_home"]) == private for row in shared.component_locations(project))


def test_discovery_does_not_recurse_through_unrelated_directories(tmp_path, isolated_memory):
    old = source_at(tmp_path / "公共目录" / "其他工作文件", "0.10.2")
    new = source_at(tmp_path / "公共目录", "0.10.4")
    python = python_at(tmp_path)
    home = tmp_path / "不应自动发现的环境"
    engine_at(home / "lo-26.2.6")
    choice(old, home, python)
    assert shared.saved_selection(new) is None
    assert all(Path(row["runtime_home"]) != home for row in shared.component_locations(new))


@pytest.mark.parametrize("component", ["engines/LibreOffice", "runtime/lo-26.2.6", "support/7zip"])
def test_bundled_components_need_preservation_before_the_release_can_be_deleted(tmp_path, component):
    old = source_at(tmp_path, "0.10.2")
    new = source_at(tmp_path, "0.10.4")
    home = tmp_path / "青简共享环境"
    root = old / component
    root.mkdir(parents=True)
    assert shared.needs_preservation(root, new, home)


def test_components_already_in_the_selected_home_or_external_install_do_not_need_copying(tmp_path):
    project = source_at(tmp_path)
    home = tmp_path / "青简共享环境"
    for root in (home / "lo-26.2.6", tmp_path / "ProgramFiles/LibreOffice", tmp_path / "独立工具/LibreOffice"):
        root.mkdir(parents=True)
        assert not shared.needs_preservation(root, project, home)


def test_current_project_configuration_wins_over_remembered_other_runtime(tmp_path, isolated_memory):
    project = source_at(tmp_path)
    python = python_at(tmp_path)
    remembered_home = tmp_path / "上次的共享环境"
    engine = engine_at(remembered_home / "lo-26.2.6")
    assert shared.remember_runtime(project, python, remembered_home, engine, {})
    chosen_home = tmp_path / "本项目明确选择"
    chosen_home.mkdir()
    expected = choice(project, chosen_home, python)
    launcher = load_cmd_launcher()
    args = launcher.parse_args(["--project", str(project)])
    assert launcher.selection_for(args, read=lambda _: pytest.fail("unexpected prompt")) == expected


def test_explicit_runtime_and_python_win_over_memory_without_writing_it(tmp_path, isolated_memory):
    project = source_at(tmp_path)
    python = python_at(tmp_path)
    old_home = tmp_path / "旧共享环境"
    assert shared.remember_runtime(project, python, old_home, engine_at(old_home / "lo-26.2.6"), {})
    before = isolated_memory.read_bytes()
    explicit = tmp_path / "这次明确指定的新目录"
    launcher = load_cmd_launcher()
    args = launcher.parse_args(["--project", str(project), "--python", str(python),
                               "--runtime-home", str(explicit), "--no-ui"])
    selected = launcher.selection_for(args, read=lambda _: pytest.fail("unexpected prompt"))
    assert Path(selected["runtime_home"]) == explicit
    assert Path(selected["python"]) == python
    assert isolated_memory.read_bytes() == before
    assert not explicit.exists()


def test_choose_environment_still_prompts_and_cancel_keeps_memory(tmp_path, isolated_memory):
    project = source_at(tmp_path)
    python = python_at(tmp_path)
    home = tmp_path / "共享运行环境"
    assert shared.remember_runtime(project, python, home, engine_at(home / "lo-26.2.6"), {})
    before = isolated_memory.read_bytes()
    launcher = load_cmd_launcher()
    args = launcher.parse_args(["--project", str(project), "--choose-environment"])
    questions = []
    def cancel(prompt):
        questions.append(prompt)
        return "Q"
    assert launcher.selection_for(args, read=cancel, output=lambda _: None) is None
    assert questions
    assert isolated_memory.read_bytes() == before
    assert not (project / "config/startup-selection.json").exists()


def execute_cmd(path, arguments, environment):
    pieces = []
    for value in [str(path), *map(str, arguments)]:
        quoted = subprocess.list2cmdline([value])
        if any(character in value for character in "&|<>()^") and not quoted.startswith('"'):
            quoted = '"' + quoted + '"'
        pieces.append(quoted)
    command = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"])
    command += ' "' + " ".join(pieces) + '"'
    return subprocess.run(command, cwd=path.parent, env=environment, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", input="", timeout=90)


@pytest.mark.skipif(os.name != "nt", reason="Windows CMD startup memory")
@pytest.mark.parametrize("mode", ["reuse", "managed"])
def test_cmd_with_python_discovery_disabled_restores_the_remembered_interpreter(tmp_path, isolated_memory, mode):
    old = source_at(tmp_path / "旧版位置", "0.10.3")
    home = tmp_path / "包外共享环境"
    engine = engine_at(home / "lo-26.2.6")
    python = Path(sys.executable)
    version = ".".join(map(str, sys.version_info[:3]))
    if mode == "managed":
        # Exercise a real Windows venv redirector, not a fake executable or
        # patched probe. The supported managed cache records its real location
        # beneath the chosen home, so neither system discovery nor a download
        # is needed when restoring this selection in a new source release.
        python = real_venv(home / ("python-" + version))
        (home / "python-selection.json").write_text(
            json.dumps({"python": str(python)}), encoding="utf-8")
    assert shared.remember_runtime(old, python, home, engine, {}, mode=mode)
    old_wrapper = old.parents[1]
    assert old_wrapper.resolve().is_relative_to(tmp_path.resolve())
    shutil.rmtree(old_wrapper)
    project = source_at(tmp_path / "新版位置", "0.10.4")
    if mode == "managed":
        # No artifact is fetched. Only the expected real interpreter version
        # and matching filename change for Read-RuntimeSpec's normal checks.
        manifest_path = project / "runtime-sources.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest["python"].update(version=version, filename="python-" + version + "-amd64.zip")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    root = project.parent
    (project / "launcher").mkdir()
    for filename in ("prepare_python.cmd", "cmd_source.py"):
        shutil.copy2(PROJECT / "launcher" / filename, project / "launcher" / filename)
    shutil.copy2(PROJECT / "launcher/start_source.cmd", root / "启动青简工具箱.cmd")
    shutil.copy2(PROJECT / "startup_shared.py", project / "startup_shared.py")
    (project / "bootstrap.py").write_text('''
import json,os
from pathlib import Path
class BootstrapError(RuntimeError): pass
PYTHON_FLAGS=['-E','-X','utf8']
def startup_selection(project):
    path=project/'config/startup-selection.json'
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else None
def selected_interpreter(project,home):
    version=json.loads((project/'runtime-sources.json').read_text(encoding='utf-8-sig'))['python']['version']
    cached=Path(home)/'python-selection.json'
    return Path(json.loads(cached.read_text(encoding='utf-8-sig'))['python']) if cached.exists() else Path(home)/('python-'+version)/'python.exe'
def check_environment(project,python,home):
    return dict(ready=True,supported=True,engine='fixture',native_components={})
def ready_environment(project,python,home,engine,native_components):
    return dict(QINGJIAN_RUNTIME_HOME=str(home))
def child_environment(home=None): return os.environ.copy()
def prepare_environment(*args,**kwargs): raise AssertionError('unexpected preparation or network')
''', encoding="utf-8")
    env = os.environ.copy()
    for name in ("QJ_CMD_PROJECT", "QJ_CMD_PYTHON", "QJ_CMD_HOME", "QJ_CMD_RESULT"):
        env.pop(name, None)
    result = execute_cmd(root / "启动青简工具箱.cmd",
                         ["--no-discovery", "--no-ui", "--offline", "--prepare-only"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    saved = json.loads((project / "config/startup-selection.json").read_text(encoding="utf-8-sig"))
    assert Path(saved["python"]) == python
    assert saved["mode"] == mode
    assert Path(saved["runtime_home"]) == home
    assert "正在下载" not in result.stdout
    assert not (home / "downloads").exists()
