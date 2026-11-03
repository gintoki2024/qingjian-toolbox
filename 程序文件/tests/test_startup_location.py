"""First-run destination choices, portable routing and no-download cancellation."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import bootstrap


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.delenv("QINGJIAN_RUNTIME_HOME", raising=False)
    monkeypatch.delenv("QINGJIAN_BOOTSTRAP_READY", raising=False)
    folder = tmp_path / "三项目包" / "程序文件"
    folder.mkdir(parents=True)
    for name in ("main.py", "bootstrap.py", "requirements.txt", "runtime-sources.json", "startup_native.py", "startup_shared.py", "native-sources.json", "bootstrap-wheels.json"):
        shutil.copy2(bootstrap.PROJECT / name, folder / name)
    return folder


def write_choice(project, home, *, mode="reuse", python=None):
    path = project / "config" / "startup-selection.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "mode": mode, "runtime_home": str(home),
                               "python": str(python or sys.executable) if mode == "reuse" else ""}), encoding="utf-8")


def test_default_location_is_only_a_suggestion_without_appdata_creation(source, tmp_path, monkeypatch):
    appdata = tmp_path / "C盘模拟"
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    assert bootstrap.runtime_home(project=source) == source.parent.parent / "青简共享环境"
    assert not appdata.exists()
    assert not (source / "runtime").exists()
    assert bootstrap.startup_selection(source) is None


def test_read_saved_location_and_explicit_override(source, tmp_path):
    saved = tmp_path / "D盘" / "共享运行环境"
    write_choice(source, saved)
    assert bootstrap.runtime_home(project=source) == saved
    assert bootstrap.runtime_home(tmp_path / "明确指定", source) == tmp_path / "明确指定"
    assert not saved.exists()


def test_invalid_location_never_silently_falls_back(source):
    path = source / "config" / "startup-selection.json"
    path.parent.mkdir()
    path.write_text('{"schema_version":1,"mode":"reuse","runtime_home":"relative","python":"python.exe"}', encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="重新选择"):
        bootstrap.runtime_home(project=source)
    assert not (source / "runtime").exists()


def test_direct_main_first_run_uses_same_picker_before_even_ready_environment_probe(source):
    launcher = source.parent / "启动青简工具箱.exe"
    launcher.write_bytes(b"test launcher")
    with patch("bootstrap.check_environment", side_effect=AssertionError("choose location first")), \
         patch("bootstrap.prepare_environment", side_effect=AssertionError("download forbidden before choice")), \
         patch("bootstrap.subprocess.Popen") as popen:
        assert bootstrap.start_source(source, ["--notebook"]) is False
    command = popen.call_args.args[0]
    assert command == [str(launcher), "--choose-environment", "--python", sys.executable, "--", "--notebook"]
    assert not (source / "runtime").exists()
    assert not (source / "config").exists()


def test_direct_main_obeys_remembered_different_python(source, tmp_path):
    home, python = tmp_path / "共享运行环境", tmp_path / "另一个Python" / "python.exe"
    write_choice(source, home, python=python)
    launcher = source.parent / "启动青简工具箱.exe"
    launcher.write_bytes(b"test launcher")
    with patch("bootstrap.probe_python", side_effect=AssertionError("do not install into invoking interpreter")), \
         patch("bootstrap.subprocess.Popen") as popen:
        assert bootstrap.start_source(source, []) is False
    assert popen.call_args.args[0] == [str(launcher), "--runtime-home", str(home), "--"]


def test_check_environment_uses_saved_python_without_creating_locations(source, tmp_path):
    home, python = tmp_path / "E盘环境", tmp_path / "E盘环境" / "Python" / "python.exe"
    write_choice(source, home, python=python)
    with patch("bootstrap.probe_python", return_value={"ready": True}) as probe, \
         patch("startup_engine.find_engine", return_value=tmp_path / "LO"), \
         patch("startup_native.check_native", return_value={"native_ready": True, "native_missing": [], "native_components": {}}):
        checked = bootstrap.check_environment(source)
    assert probe.call_args.args[0] == python
    assert checked["runtime_home"] == str(home)
    assert checked["ready"] is True
    assert not home.exists()


def test_managed_choice_does_not_reuse_invoking_or_outside_python(source, tmp_path):
    home = tmp_path / "D盘环境"
    home.mkdir()
    write_choice(source, home, mode="managed")
    (home / "python-selection.json").write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
    assert bootstrap.selected_interpreter(source) is None
    expected = home / "python-3.13.17" / "python.exe"
    expected.parent.mkdir()
    expected.write_bytes(b"managed executable")
    (home / "python-selection.json").write_text(json.dumps({"python": str(expected)}), encoding="utf-8")
    assert bootstrap.selected_interpreter(source) == expected


def test_child_cache_and_large_temp_are_scoped_to_selected_directory(tmp_path, monkeypatch):
    home = tmp_path / "D盘共享环境"
    monkeypatch.setenv("TEMP", "parent-temp")
    monkeypatch.setenv("PIP_CACHE_DIR", "parent-cache")
    child = bootstrap.child_environment(home)
    assert child["TEMP"] == child["TMP"] == child["TMPDIR"] == str(home / "temp")
    assert child["PIP_CACHE_DIR"] == str(home / "pip-cache")
    assert os.environ["TEMP"] == "parent-temp"
    assert os.environ["PIP_CACHE_DIR"] == "parent-cache"
    assert not home.exists()


def compiler():
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if os.name != "nt" or not path.is_file():
        pytest.skip("Windows .NET launcher validation")
    return path


def run_native(command, **kwargs):
    env = os.environ.copy()
    for key in ("QINGJIAN_RUNTIME_HOME", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    return subprocess.run(list(map(str, command)), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


def test_packed_source_check_only_resolves_internal_project_and_does_not_write(source, tmp_path):
    compiler()
    launcher = source.parent / "启动青简工具箱.exe"
    shutil.copy2(bootstrap.PROJECT / launcher.name, launcher)
    home = tmp_path / "未创建的运行环境"
    result = run_native([launcher, "--check-only", "--no-discovery", "--python", sys.executable, "--runtime-home", home])
    assert result.returncode == 0, result.stdout + result.stderr
    event = json.loads(result.stdout.strip().splitlines()[-1])
    assert event["event"] == "check" and event["available"] is True
    assert Path(event["python"]) == Path(sys.executable)
    assert not home.exists()
    assert not (source / "config").exists()


def test_managed_headless_check_does_not_discover_system_python(source, tmp_path):
    compiler()
    launcher = source.parent / "启动青简工具箱.exe"
    shutil.copy2(bootstrap.PROJECT / launcher.name, launcher)
    home = tmp_path / "未部署"
    # A one-off explicit choice must override a previously saved reuse mode.
    write_choice(source, tmp_path / "旧位置", python=sys.executable)
    result = run_native([launcher, "--check-only", "--deploy-runtime", "--runtime-home", home])
    assert result.returncode == 3, result.stdout + result.stderr
    event = json.loads(result.stdout.strip().splitlines()[-1])
    assert event["available"] is False and event["mode"] == "managed"
    assert not home.exists()


@pytest.mark.parametrize("starter", ["启动青简工具箱.exe", "launcher/portable/启动青简工具箱.exe"])
def test_portable_launcher_skips_manifest_and_propagates_arguments_and_exit_code(tmp_path, starter):
    csc = compiler()
    package = tmp_path / "完整包"
    internal = package / "程序文件"
    internal.mkdir(parents=True)
    launcher = package / "启动青简工具箱.exe"
    shutil.copy2(bootstrap.PROJECT / starter, launcher)
    fake = tmp_path / "portable.cs"
    fake.write_text('using System;using System.IO;class P{static int Main(string[] a){'
                    'if(a.Length!=2||a[0]!="--record")return 29;File.WriteAllText(a[1],'
                    'Environment.GetEnvironmentVariable("QINGJIAN_BOOTSTRAP_READY")??"skipped");return 17;}}', encoding="utf-8")
    executable = internal / "青简工具箱.exe"
    built = run_native([csc, "/nologo", "/target:exe", "/out:" + str(executable), fake])
    assert built.returncode == 0, built.stdout + built.stderr
    record = tmp_path / "started.txt"
    checked = run_native([launcher, "--check-only"])
    assert checked.returncode == 0
    assert json.loads(checked.stdout.strip())["bootstrap"] == "skipped"
    result = run_native([launcher, "--no-ui", "--", "--record", record])
    assert result.returncode == 17, result.stdout + result.stderr
    assert record.read_text() == "skipped"
    assert sorted(path.name for path in package.iterdir()) == ["启动青简工具箱.exe", "程序文件"]
    assert not (internal / "runtime").exists()


def test_native_first_run_cancel_creates_no_runtime_or_saved_choice(tmp_path):
    csc = compiler()
    project = tmp_path / "取消测试"
    project.mkdir()
    shutil.copy2(bootstrap.PROJECT / "runtime-sources.json", project / "runtime-sources.json")
    harness = tmp_path / "cancel.cs"
    harness.write_text(r'''
using System;using System.Reflection;using System.Windows.Forms;
class CancelHarness {
 [STAThread] static int Main() {
  Application.Idle += delegate {
   foreach(Form form in Application.OpenForms) {
    if(form.GetType().Name=="EnvironmentChoiceForm") {
     form.BeginInvoke((Action)delegate {form.DialogResult=DialogResult.Cancel;form.Close();});break;
    }
   }
  };
  var program=typeof(Qingjian.SourceLauncher.Options).Assembly.GetType("Qingjian.SourceLauncher.Program");
  return (int)program.GetMethod("Main",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{new string[]{"--no-discovery"}});
 }
}''', encoding="utf-8")
    executable = project / "cancel-test.exe"
    references = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll",
                  "System.Web.Extensions.dll", "System.IO.Compression.dll", "System.IO.Compression.FileSystem.dll"]
    built = run_native([csc, "/nologo", "/target:winexe", "/main:CancelHarness", "/codepage:65001",
                        "/out:" + str(executable), *["/reference:" + item for item in references],
                        bootstrap.PROJECT / "launcher" / "SourceLauncher.cs", harness])
    assert built.returncode == 0, built.stdout + built.stderr
    result = run_native([executable])
    assert result.returncode == 130, result.stdout + result.stderr
    assert not (project / "runtime").exists()
    assert not (project / "config").exists()
    assert sorted(path.name for path in project.iterdir()) == ["cancel-test.exe", "runtime-sources.json"]
