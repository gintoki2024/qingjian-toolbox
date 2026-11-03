"""CMD source startup: no packaged EXE, explicit deployment, verified downloads."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "launcher" / "cmd_source.py"
spec = importlib.util.spec_from_file_location("cmd_source_under_test", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def args_for(tmp_path, *extra):
    return launcher.parse_args(["--project", str(tmp_path), *extra])


def selection(tmp_path, mode="reuse"):
    return {"schema_version": 1, "mode": mode, "python": sys.executable,
            "runtime_home": str(tmp_path / "chosen-runtime")}


@pytest.fixture(autouse=True)
def clean_bootstrap_environment(monkeypatch):
    for name in ("QINGJIAN_RUNTIME_HOME", "QINGJIAN_CMD_PYTHON", "QINGJIAN_CMD_SELECTION_DONE"):
        monkeypatch.delenv(name, raising=False)


def test_cancel_first_choice_creates_nothing(tmp_path):
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=None):
        assert launcher.selection_for(args_for(tmp_path), read=lambda _: "q", output=lambda _: None) is None
    assert list(tmp_path.iterdir()) == []


def test_enter_uses_package_runtime_and_preserves_existing_python(tmp_path):
    answers = iter(["", "Y"])
    messages = []
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=None):
        selected = launcher.selection_for(args_for(tmp_path), read=lambda _: next(answers), output=messages.append)
    assert selected["mode"] == "reuse"
    assert selected["python"] == str(Path(sys.executable).absolute())
    from startup_shared import default_runtime_home
    assert selected["runtime_home"] == str(default_runtime_home(tmp_path))
    assert json.loads((tmp_path / "config/startup-selection.json").read_text(encoding="utf-8")) == selected
    assert any(str(default_runtime_home(tmp_path)) in message for message in messages)
    assert not (tmp_path / "runtime").exists()


def test_default_directory_is_not_created_without_confirmation(tmp_path):
    answers = iter(["", "n"])
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=None):
        assert launcher.selection_for(args_for(tmp_path), read=lambda _: next(answers), output=lambda _: None) is None
    assert list(tmp_path.iterdir()) == []


def test_decline_confirmation_creates_nothing(tmp_path):
    answers = iter([str(tmp_path / "must-not-exist"), "n"])
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=None):
        assert launcher.selection_for(args_for(tmp_path), read=lambda _: next(answers), output=lambda _: None) is None
    assert list(tmp_path.iterdir()) == []


def test_noninteractive_requires_deployment_choice(tmp_path):
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=None):
        with pytest.raises(launcher.bootstrap.BootstrapError, match="尚未选择"):
            launcher.selection_for(args_for(tmp_path, "--no-ui"))
    assert list(tmp_path.iterdir()) == []


def test_explicit_paths_reuse_existing_python_without_creating_environment(tmp_path):
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=None):
        value = launcher.selection_for(args_for(tmp_path, "--python", sys.executable,
                                               "--runtime-home", str(tmp_path / "selected"), "--no-ui"))
    assert value["mode"] == "reuse"
    assert value["python"] == str(Path(sys.executable).absolute())
    assert list(tmp_path.iterdir()) == []


def test_saved_managed_location_remains_managed(tmp_path):
    saved = selection(tmp_path, "managed")
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=saved):
        assert launcher.selection_for(args_for(tmp_path)) == saved


def test_cmd_completed_choice_does_not_prompt_a_second_time(tmp_path, monkeypatch):
    saved = selection(tmp_path)
    saved['runtime_home'] = str(tmp_path.parent / (tmp_path.name + '-external-shared'))
    monkeypatch.setenv("QINGJIAN_CMD_SELECTION_DONE", "1")
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=saved):
        assert launcher.selection_for(args_for(tmp_path, "--choose-environment"),
                                      read=lambda _: pytest.fail("choice already completed")) == saved


def test_confirmed_choice_is_saved_only_inside_program_config(tmp_path):
    answers = iter([str(tmp_path / "chosen"), "Y"])
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=None):
        selected = launcher.selection_for(args_for(tmp_path), read=lambda _: next(answers), output=lambda _: None)
    assert json.loads((tmp_path / "config/startup-selection.json").read_text(encoding="utf-8")) == selected
    assert not (tmp_path / "chosen").exists()
    assert not list((tmp_path / "config").glob("*.tmp"))


def test_ready_environment_is_offline_and_forwards_unicode_arguments(tmp_path):
    saved = selection(tmp_path)
    checked = {"ready": True, "supported": True, "engine": "fixture-engine", "native_components": {"x": "y"}}
    application = ["--self-check", str(tmp_path / "中文 路径.json"), "A&B", "$(literal)", 'a"b']
    args = args_for(tmp_path, "--offline", "--no-ui", "--", *application)
    with mock.patch.object(launcher, "selection_for", return_value=saved), \
         mock.patch.object(launcher.bootstrap, "check_environment", return_value=checked), \
         mock.patch.object(launcher.bootstrap, "ready_environment", return_value={"QINGJIAN_BOOTSTRAP_READY": "fixture-stamp"}), \
         mock.patch.object(launcher.bootstrap, "prepare_environment") as prepare, \
         mock.patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as run:
        assert launcher.run(args, emit=lambda _: None) == 7
    prepare.assert_not_called()
    command = run.call_args.args[0]
    assert command == [sys.executable, *launcher.bootstrap.PYTHON_FLAGS, str(tmp_path / "main.py"), *application]
    assert run.call_args.kwargs["env"]["QINGJIAN_BOOTSTRAP_READY"] == "fixture-stamp"
    assert run.call_args.kwargs["env"]["QINGJIAN_CMD_START"] == "1"
    assert "shell" not in run.call_args.kwargs
    assert not any("启动青简" in item or "PortableLauncher" in item for item in command)


def test_missing_environment_obeys_offline_flag_and_selected_python(tmp_path):
    saved = selection(tmp_path)
    with mock.patch.object(launcher, "selection_for", return_value=saved), \
         mock.patch.object(launcher.bootstrap, "check_environment", return_value={"ready": False, "supported": True}), \
         mock.patch.object(launcher.bootstrap, "prepare_environment", return_value={"environment": {}}) as prepare:
        assert launcher.run(args_for(tmp_path, "--prepare-only", "--offline"), emit=lambda _: None) == 0
    assert prepare.call_args.args[:3] == (tmp_path, Path(sys.executable), tmp_path / "chosen-runtime")
    assert prepare.call_args.kwargs["offline"] is True


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI process lifecycle")
@pytest.mark.parametrize("has_pythonw", [False, True], ids=["console-python-fallback", "pythonw"])
def test_default_launch_has_no_console_or_pipe_dependency_and_uses_gui_stamp(tmp_path, has_pythonw):
    python = tmp_path / "共享 Python" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"fixture")
    windowed = python.with_name("pythonw.exe")
    if has_pythonw:
        windowed.write_bytes(b"fixture")
    selected = {**selection(tmp_path), "python": str(python)}
    application = ["--surface", "home", "中文 空格", "literal&(x)"]
    checked = {"ready": True, "supported": True, "engine": "fixture", "native_components": {}}
    events = []
    with mock.patch.object(launcher, "selection_for", return_value=selected), \
         mock.patch.object(launcher.bootstrap, "check_environment", return_value=checked), \
         mock.patch.object(launcher.bootstrap, "ready_environment", return_value={"QINGJIAN_BOOTSTRAP_READY": "console-stamp"}), \
         mock.patch.object(launcher.bootstrap, "environment_stamp", return_value="gui-stamp") as stamp, \
         mock.patch.object(launcher.bootstrap, "prepare_environment") as prepare, \
         mock.patch.object(launcher.subprocess, "run") as synchronous, \
         mock.patch.object(launcher.subprocess, "Popen") as spawn:
        spawn.return_value.pid = 4321
        assert launcher.run(args_for(tmp_path, "--offline", "--", *application), emit=events.append) == 0
    gui = windowed if has_pythonw else python
    stamp.assert_called_once_with(tmp_path, gui)
    prepare.assert_not_called()
    synchronous.assert_not_called()
    assert spawn.call_args.args[0] == [str(gui), *launcher.bootstrap.PYTHON_FLAGS, str(tmp_path / "main.py"), *application]
    options = spawn.call_args.kwargs
    expected_flag = subprocess.DETACHED_PROCESS if has_pythonw else subprocess.CREATE_NO_WINDOW
    assert options["creationflags"] == expected_flag | subprocess.CREATE_NEW_PROCESS_GROUP
    assert options["close_fds"] is True
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.STDOUT
    assert options["env"]["QINGJIAN_BOOTSTRAP_READY"] == "gui-stamp"
    assert options["env"]["QINGJIAN_CMD_START"] == "1"
    assert "shell" not in options
    log_path = Path(events[-1]["log"])
    assert log_path.parent == tmp_path / "chosen-runtime/logs"
    assert options["stdout"].closed
    assert "独立界面启动" in log_path.read_text(encoding="utf-8")
    assert events[-1]["pid"] == 4321


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI process lifecycle")
def test_detached_spawn_failure_keeps_reason_in_log_without_retrying(tmp_path):
    python = Path(sys.executable)
    with mock.patch.object(launcher.bootstrap, "environment_stamp", return_value="stamp"), \
         mock.patch.object(launcher.subprocess, "Popen", side_effect=OSError("fixture access denied")) as spawn:
        with pytest.raises(launcher.bootstrap.BootstrapError, match="界面独立启动未完成"):
            launcher.start_detached(tmp_path, python, tmp_path / "runtime", {}, [])
    spawn.assert_called_once()
    log = next((tmp_path / "runtime/logs").glob("source-gui-*.log"))
    assert "fixture access denied" in log.read_text(encoding="utf-8")


def test_check_only_does_not_install_or_create_runtime(tmp_path, capsys):
    with mock.patch.object(launcher, "selection_for", return_value=selection(tmp_path)), \
         mock.patch.object(launcher.bootstrap, "check_environment", return_value={"ready": False, "supported": True}), \
         mock.patch.object(launcher.bootstrap, "prepare_environment") as prepare, \
         mock.patch.object(launcher.subprocess, "run") as run:
        assert launcher.run(args_for(tmp_path, "--check-only")) == 1
    prepare.assert_not_called()
    run.assert_not_called()
    assert list(tmp_path.iterdir()) == []
    assert json.loads(capsys.readouterr().out)["event"] == "check"


def test_no_environment_on_cancel(tmp_path):
    with mock.patch.object(launcher, "selection_for", return_value=None), \
         mock.patch.object(launcher.bootstrap, "check_environment") as check, \
         mock.patch.object(launcher.bootstrap, "prepare_environment") as prepare:
        assert launcher.run(args_for(tmp_path), emit=lambda _: None) == 130
    check.assert_not_called()
    prepare.assert_not_called()


def test_python_option_cannot_be_an_interpreter_flag(tmp_path):
    with mock.patch.object(launcher.bootstrap, "startup_selection", return_value=None):
        with pytest.raises(launcher.bootstrap.BootstrapError, match="绝对路径"):
            launcher.selection_for(args_for(tmp_path, "--python=-c", "--runtime-home", str(tmp_path)))


POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def run_powershell_functions(code, env=None):
    script = PROJECT / "launcher/prepare_python.cmd"
    shell_env = os.environ.copy()
    shell_env.update(env or {})
    shell_env["QJ_FUNCTION_SOURCE"] = str(script)
    load = "$s=[IO.File]::ReadAllText($env:QJ_FUNCTION_SOURCE,[Text.Encoding]::UTF8); $a=$s.LastIndexOf('#<QJ-POWERSHELL>'); $b=$s.IndexOf('#<QJ-MAIN>'); . ([ScriptBlock]::Create($s.Substring($a,$b-$a))); "
    result = subprocess.run([str(POWERSHELL), "-NoLogo", "-NoProfile", "-Command", load + code], env=shell_env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell implementation")
def test_powershell_real_interpreter_probe_and_no_discovery():
    output = run_powershell_functions("$x=Probe-Python $env:QJ_FIXTURE_PYTHON; if(-not $x){throw 'probe failed'}; $env:QJ_CMD_NO_DISCOVERY='1'; $env:QJ_CMD_PYTHON=''; $env:QINGJIAN_CMD_PYTHON=''; if(Find-Python){throw 'discovery not disabled'}; Write-Host 'PROBE_OK'",
                                      {"QJ_FIXTURE_PYTHON": sys.executable})
    assert "PROBE_OK" in output


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell implementation")
def test_powershell_main_source_failure_uses_backup_and_validates_hash(tmp_path):
    fixture = tmp_path / "fixture.zip"
    fixture.write_bytes(b"verified fixture payload")
    data = {"filename": "python-3.13.17-amd64.zip", "bytes": fixture.stat().st_size,
            "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "urls": [{"name": "primary", "url": "https://primary.invalid/python.zip"},
                     {"name": "backup", "url": "https://backup.invalid/python.zip"}]}
    manifest = tmp_path / "spec.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    code = "$spec=[IO.File]::ReadAllText($env:QJ_FIXTURE_SPEC)|ConvertFrom-Json; $script:attempts=0; function Get-RemoteArtifact($Source,[string]$Target,[long]$ExpectedBytes){$script:attempts++;if($Source.name -eq 'primary'){throw 'fixture unavailable'};[IO.File]::Copy($env:QJ_FIXTURE_ARCHIVE,$Target)}; $archive=Get-PythonArchive $spec $env:QJ_FIXTURE_HOME $false; if($script:attempts -ne 2 -or -not(Test-Artifact $archive $spec)){throw 'fallback failed'}; $script:attempts=0; $again=Get-PythonArchive $spec $env:QJ_FIXTURE_HOME $true; if($script:attempts -ne 0 -or $again -ne $archive){throw 'offline reuse failed'}; Write-Host 'FALLBACK_OK'"
    output = run_powershell_functions(code, {"QJ_FIXTURE_SPEC": str(manifest), "QJ_FIXTURE_ARCHIVE": str(fixture), "QJ_FIXTURE_HOME": str(tmp_path / "runtime")})
    assert "FALLBACK_OK" in output


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell implementation")
def test_powershell_hash_mismatch_is_never_extracted(tmp_path):
    fixture = tmp_path / "fixture.zip"
    fixture.write_bytes(b"mismatched")
    data = {"filename": "python-3.13.17-amd64.zip", "bytes": fixture.stat().st_size,
            "sha256": "0" * 64, "urls": [{"name": "primary", "url": "https://invalid.example/python.zip"}]}
    manifest = tmp_path / "spec.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    code = "$spec=[IO.File]::ReadAllText($env:QJ_FIXTURE_SPEC)|ConvertFrom-Json; function Get-RemoteArtifact($Source,[string]$Target,[long]$ExpectedBytes){[IO.File]::Copy($env:QJ_FIXTURE_ARCHIVE,$Target)}; $rejected=$false; try{Get-PythonArchive $spec $env:QJ_FIXTURE_HOME $false|Out-Null}catch{$rejected=$true}; if(-not $rejected){throw 'hash accepted'}; if(@([IO.Directory]::GetFiles([IO.Path]::Combine($env:QJ_FIXTURE_HOME,'downloads'))).Count){throw 'unverified file retained'}; Write-Host 'HASH_REJECTED'"
    output = run_powershell_functions(code, {"QJ_FIXTURE_SPEC": str(manifest), "QJ_FIXTURE_ARCHIVE": str(fixture), "QJ_FIXTURE_HOME": str(tmp_path / "runtime")})
    assert "HASH_REJECTED" in output


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell implementation")
def test_powershell_rejects_zip_traversal(tmp_path):
    import zipfile
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("../outside.txt", "must stay absent")
    target = tmp_path / "extract"
    target.mkdir()
    output = run_powershell_functions("$rejected=$false;try{Expand-VerifiedZip $env:QJ_FIXTURE_ARCHIVE $env:QJ_FIXTURE_HOME}catch{$rejected=$true};if(-not $rejected){throw 'unsafe zip accepted'};Write-Host 'ZIP_REJECTED'",
                                      {"QJ_FIXTURE_ARCHIVE": str(archive), "QJ_FIXTURE_HOME": str(target)})
    assert "ZIP_REJECTED" in output
    assert not (tmp_path / "outside.txt").exists()


def test_cmd_sources_do_not_launch_old_executables_or_change_system_policy():
    scripts = "\n".join((PROJECT / "launcher" / name).read_text(encoding="utf-8") for name in
                        ("start_source.cmd", "prepare_python.cmd", "cmd_source.py"))
    lowered = scripts.casefold()
    for forbidden in ("executionpolicy", "set-executionpolicy", "setx ", "reg add", "icacls ", "csc.exe", "sourcelauncher.exe", "启动青简工具箱.exe"):
        assert forbidden not in lowered
    assert "certificatevalidationcallback" not in lowered
    assert "-command" in lowered


def cmd_fixture(tmp_path):
    root = tmp_path / "中文 CMD 包"
    project = root / "程序文件"
    (project / "launcher").mkdir(parents=True)
    for name in ("cmd_source.py", "prepare_python.cmd"):
        shutil.copy2(PROJECT / "launcher" / name, project / "launcher" / name)
    shutil.copy2(PROJECT / "launcher/start_source.cmd", root / "启动青简工具箱.cmd")
    shutil.copy2(PROJECT / "startup_shared.py", project / "startup_shared.py")
    (project / "bootstrap.py").write_text('''
import json,os
from pathlib import Path
class BootstrapError(RuntimeError): pass
PYTHON_FLAGS = ['-E', '-X', 'utf8']
def startup_selection(project):
    path = project / 'config/startup-selection.json'
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else None
def check_environment(project, python, home): return {'ready':True, 'supported':True, 'engine':'fixture','native_components':{}}
def ready_environment(project, python, home, engine, native_components): return {'QINGJIAN_BOOTSTRAP_READY':'fixture-stamp','QINGJIAN_RUNTIME_HOME':str(home)}
def child_environment(home=None): return os.environ.copy()
def prepare_environment(*args,**kwargs): raise AssertionError('must remain offline')
''', encoding="utf-8")
    (project / "main.py").write_text('''
import json,os,sys
from pathlib import Path
Path(sys.argv[2]).write_text(json.dumps({'args':sys.argv[3:],'ready':os.environ.get('QINGJIAN_BOOTSTRAP_READY'),'runtime':os.environ.get('QINGJIAN_RUNTIME_HOME')},ensure_ascii=False),encoding='utf-8')
''', encoding="utf-8")
    return root, project


def execute_cmd(path, arguments, *, stdin=None, env=None):
    pieces = []
    for value in [str(path), *arguments]:
        piece = subprocess.list2cmdline([value])
        if any(character in value for character in "&|<>()^") and not piece.startswith('"'):
            piece = '"' + piece + '"'
        pieces.append(piece)
    command = " ".join(pieces)
    # The outer quotes belong to cmd /s /c; inner ones remain native argument quotes.
    line = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"]) + ' "' + command + '"'
    child_env = os.environ.copy()
    for name in ("QINGJIAN_RUNTIME_HOME", "QINGJIAN_CMD_PYTHON", "QJ_CMD_PROJECT", "QJ_CMD_RESULT"):
        child_env.pop(name, None)
    child_env.update(env or {})
    return subprocess.run(line, input=stdin, env=child_env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=90)


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd entry")
def test_real_cmd_keeps_unicode_spaces_and_forwards_application_arguments(tmp_path):
    root, project = cmd_fixture(tmp_path)
    report = tmp_path / "参数 记录.json"
    runtime = tmp_path / "所选 环境"
    original = ["中文 空格", "plain", "literal&(x)"]
    result = execute_cmd(root / "启动青简工具箱.cmd", ["--python", sys.executable, "--runtime-home", str(runtime),
                            "--no-ui", "--offline", "--", "--record", str(report), *original])
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = json.loads(report.read_text(encoding="utf-8"))
    assert recorded["args"] == original
    assert recorded["ready"] == "fixture-stamp"
    assert recorded["runtime"] == str(runtime)
    assert not list((project / "config").glob(".cmd-python-*"))
    assert not list(root.glob("*.exe"))


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd entry")
@pytest.mark.parametrize("custom", [False, True], ids=["enter-default", "custom-path"])
def test_real_cmd_reuse_saves_directory_without_deploying_python_or_prompting_again(tmp_path, custom):
    root, project = cmd_fixture(tmp_path)
    from startup_shared import default_runtime_home
    runtime = tmp_path / "自选 工具缓存" if custom else default_runtime_home(project)
    directory_answer = '"' + str(runtime) + '"' if custom else ""
    result = execute_cmd(root / "启动青简工具箱.cmd", ["--python", sys.executable, "--no-discovery",
                         "--offline", "--prepare-only"], stdin="1\n" + directory_answer + "\nY\n")
    assert result.returncode == 0, result.stdout + result.stderr
    selected = json.loads((project / "config/startup-selection.json").read_text(encoding="utf-8-sig"))
    assert selected["mode"] == "reuse"
    assert Path(selected["python"]) == Path(sys.executable)
    assert Path(selected["runtime_home"]) == runtime
    assert "工具组件与缓存目录" in result.stdout
    assert (runtime / "temp").is_dir()
    assert not (runtime / "python-selection.json").exists()
    assert not (runtime / "downloads").exists()
    again = execute_cmd(root / "启动青简工具箱.cmd", ["--no-discovery", "--offline", "--check-only"], stdin="")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "输入 1" not in again.stdout
    assert "输入 Y" not in again.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd entry")
@pytest.mark.parametrize("answers", ["1\nq\n", "1\n\nn\n", "2\n\n"],
                         ids=["reuse-q", "reuse-decline-default", "managed-cancel"])
def test_real_cmd_cancel_reuse_or_managed_creates_no_runtime_or_config(tmp_path, answers):
    root, project = cmd_fixture(tmp_path)
    result = execute_cmd(root / "启动青简工具箱.cmd", ["--python", sys.executable, "--no-discovery"], stdin=answers)
    assert result.returncode == 130, result.stdout + result.stderr
    assert not (project / "config").exists()
    assert not (project / "runtime").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd entry")
def test_real_cmd_no_python_cancel_creates_no_config_or_runtime(tmp_path):
    root, project = cmd_fixture(tmp_path)
    result = execute_cmd(root / "启动青简工具箱.cmd", ["--no-discovery"], stdin="\n")
    assert result.returncode == 130, result.stdout + result.stderr
    assert not (project / "config").exists()
    assert not (project / "runtime").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd entry")
def test_real_cmd_no_python_check_only_never_downloads_or_creates_runtime(tmp_path):
    root, project = cmd_fixture(tmp_path)
    shutil.copy2(PROJECT / "runtime-sources.json", project / "runtime-sources.json")
    runtime = tmp_path / "尚未部署"
    result = execute_cmd(root / "启动青简工具箱.cmd", ["--no-discovery", "--runtime-home", str(runtime), "--check-only"])
    assert result.returncode != 0
    assert "仅检查模式未下载" in result.stdout
    assert not (project / "config").exists()
    assert not runtime.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd entry")
def test_real_cmd_invalid_explicit_python_does_not_switch_or_download(tmp_path):
    root, project = cmd_fixture(tmp_path)
    runtime = tmp_path / "不要下载"
    result = execute_cmd(root / "启动青简工具箱.cmd", ["--python", str(tmp_path / "absent.exe"),
                         "--runtime-home", str(runtime), "--no-ui"])
    assert result.returncode != 0
    assert "未切换其他解释器、未下载" in result.stdout
    assert not runtime.exists()
    assert not (project / "config").exists()
