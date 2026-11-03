"""Source preparation shares its selected interpreter without real installs."""
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import bootstrap


@pytest.fixture
def project(tmp_path, monkeypatch):
    folder = tmp_path / "源码"
    folder.mkdir()
    for name in ("requirements.txt", "runtime-sources.json", "bootstrap.py", "startup_native.py", "startup_shared.py", "native-sources.json", "bootstrap-wheels.json"):
        (folder / name).write_bytes((bootstrap.PROJECT / name).read_bytes())
    (folder / "bootstrap_wheels").mkdir()
    for wheel in (bootstrap.PROJECT / "bootstrap_wheels").glob("*.whl"):
        (folder / "bootstrap_wheels" / wheel.name).write_bytes(wheel.read_bytes())
    # These tests isolate Python/pip behavior. Native integrity/deployment has
    # its own focused tests, including real archive and engine validation.
    native = {'7zip': str(tmp_path / '7zip'), 'Ghostscript': str(tmp_path / 'Ghostscript'), 'OFD': str(tmp_path / 'OFD')}
    monkeypatch.setattr('startup_native.check_native', lambda *args, **kwargs:
                        {'native_ready': True, 'native_missing': [], 'native_components': native})
    monkeypatch.setattr('startup_native.ensure_native', lambda *args, **kwargs: native)
    return folder


@pytest.fixture
def engine(tmp_path):
    folder = tmp_path / "engine"
    (folder / "program").mkdir(parents=True)
    (folder / "program" / "soffice.com").write_bytes(b"test only")
    return folder


def _probe(python, *, ready=True):
    return {"python": str(python), "version": [3, 12, 14], "bits": 64,
            "supported": True, "ready": ready, "packages": [], "missing": [] if ready else ["Pillow"]}


def _missing_probe(python, *missing, broken=None):
    """A realistic package check, with optional valid metadata/broken import."""
    broken = broken or {}
    versions = {"PySide6-Essentials": "6.8.0", "python-docx": "1.1.2", "Pillow": "11.0.0",
                "pdfplumber": "0.11.0", "pypdfium2": "4.30.0", "requests": "2.32.0",
                "beautifulsoup4": "4.12.0", "openpyxl": "3.1.0", "pypdf": "6.0.0", "reportlab": "4.0.0",
                "python-pptx": "1.0.2", "markdown-it-py": "4.2.0", "pillow-heif": "1.8.1",
                "extract-msg": "0.58.1", "PyYAML": "6.0.3", "tomli-w": "1.2.0", "tomli": "2.5.0",
                "vobject": "0.9.9", "icalendar": "7.3.0"}
    result = _probe(python, ready=not missing)
    result["missing"] = list(missing)
    result["packages"] = [{"name": item["name"], "ok": item["name"] not in missing,
                           "version": broken.get(item["name"], "" if item["name"] in missing else versions[item["name"]]),
                           "error": "broken import" if item["name"] in broken else ""}
                          for item in bootstrap.requirements()]
    return result


def test_manifest_has_checked_official_and_domestic_fallbacks():
    manifest = bootstrap.load_manifest()
    for key in ("python", "pip", "libreoffice"):
        assert len(manifest[key]["urls"]) >= 3
        assert "tuna.tsinghua.edu.cn" in manifest[key]["urls"][1]["url"]
        assert len(manifest[key]["sha256"]) == 64
    assert len(manifest["pip_indexes"]) >= 3
    assert set(bootstrap.MODULES) == {row["name"].lower().replace("_", "-") for row in bootstrap.requirements()}


def test_child_pip_configuration_is_isolated_without_modifying_parent(monkeypatch):
    monkeypatch.setenv("PIP_CONFIG_FILE", "company-pip.ini")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example:443")
    child = bootstrap.child_environment()
    assert child["PIP_CONFIG_FILE"] == os.devnull
    assert os.environ["PIP_CONFIG_FILE"] == "company-pip.ini"
    assert child["HTTPS_PROXY"] == os.environ["HTTPS_PROXY"]


@pytest.mark.parametrize("version,spec,expected", [
    ("6.12.0", ">=6.8,<6.13", True), ("6.13.0", ">=6.8,<6.13", False),
    ("6.7.9", ">=6.8,<6.13", False), ("12.3.0", ">=11,<13", True),
    ("6.12.0rc1", ">=6.8,<6.13", False), ("1.2.0.post1", ">=1.1,<2", True),
])
def test_versions_are_checked_without_third_party_parser(version, spec, expected):
    assert bootstrap.version_satisfies(version, spec) is expected


def test_unknown_dependency_requires_a_new_import_check(project):
    (project / "requirements.txt").write_text("unreviewed-library>=1,<2\n", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="缺少自检规则"):
        bootstrap.requirements(project)


@pytest.mark.parametrize("url", ["http://example.org/simple", "https://name:secret@example.org/simple", "file:///tmp/a"])
def test_download_sources_keep_tls_and_do_not_accept_credentials(url):
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.validate_source({"name": "source", "url": url})


def test_verified_download_falls_back_after_wrong_official_bytes(tmp_path):
    expected = b"trusted bytes"
    artifact = {"filename": "component.zip", "bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest(),
                "urls": [{"name": "official", "url": "https://official.example/a"},
                         {"name": "domestic", "url": "https://mirror.example/a"}]}
    requests, events = [], []
    def open_url(request, timeout):
        requests.append(request.full_url)
        response = io.BytesIO(b"bad" if len(requests) == 1 else expected)
        response.url = request.full_url
        return response
    with patch("bootstrap.urllib.request.urlopen", side_effect=open_url):
        target = bootstrap.download_artifact(artifact, tmp_path / "cache", events.append)
    assert target.read_bytes() == expected
    assert len(requests) == 2
    with patch("bootstrap.urllib.request.urlopen", side_effect=AssertionError("must stay offline")):
        assert bootstrap.download_artifact(artifact, target.parent, events.append) == target
    assert not list(target.parent.glob("*.part"))


def test_all_download_failures_leave_no_executable_or_part_file(tmp_path):
    artifact = {"filename": "component.whl", "bytes": 3, "sha256": "0" * 64,
                "urls": [{"name": "official", "url": "https://official.example/a"}]}
    with patch("bootstrap.urllib.request.urlopen", side_effect=OSError("offline")), pytest.raises(bootstrap.BootstrapError, match="所有下载源"):
        bootstrap.download_artifact(artifact, tmp_path, lambda value: None)
    assert not list(tmp_path.iterdir())


def test_complete_environment_does_not_download_or_create_virtualenv(project, engine, tmp_path):
    home = tmp_path / "private"
    original_python = Path(sys.executable)
    events = []
    with patch("bootstrap.probe_python", return_value=_probe(original_python)), \
         patch("startup_engine.ensure_engine", return_value=engine), \
         patch("bootstrap.download_artifact", side_effect=AssertionError("network")), \
         patch("bootstrap.run_process", side_effect=AssertionError("install")):
        result = bootstrap.prepare_environment(project, home=home, emit=events.append)
    assert result["python"] == str(original_python)
    assert result["environment"]["QINGJIAN_LIBREOFFICE"].endswith("soffice.com")
    assert not (home / "envs").exists()
    assert events[-1]["event"] == "ready"


def test_missing_packages_install_to_same_interpreter_and_are_reused(project, engine, tmp_path):
    home, base = tmp_path / "runtime-cache", Path(sys.executable)
    another_project = tmp_path / "另一个源码程序"
    another_project.mkdir()
    for name in ("requirements.txt", "runtime-sources.json", "bootstrap.py", "startup_native.py", "startup_shared.py", "native-sources.json", "bootstrap-wheels.json"):
        (another_project / name).write_bytes((project / name).read_bytes())
    calls, events, state = [], [], {"installed": False}
    def probe(python, project):
        assert Path(python) == base
        return _missing_probe(python, *([] if state["installed"] else ["Pillow"]))
    def run(command, emit, log_path, timeout=1800):
        calls.append(list(map(str, command)))
        state["installed"] = True
    with patch("bootstrap.probe_python", side_effect=probe), patch("bootstrap.run_process", side_effect=run), \
         patch("bootstrap.installation_scope", return_value={"scope": "environment", "target": str(tmp_path / "site-packages")}), \
         patch("bootstrap.pip_command", return_value=[str(base), "-m", "pip"]), \
         patch("bootstrap.download_artifact", side_effect=AssertionError("do not download pip")), \
         patch("startup_engine.ensure_engine", return_value=engine):
        ready = bootstrap.prepare_environment(project, home=home, emit=events.append)
        before = len(calls)
        second = bootstrap.prepare_environment(project, home=home, emit=events.append, offline=True)
        shared = bootstrap.prepare_environment(another_project, python=base, home=tmp_path / "other-cache",
                                              emit=events.append, offline=True)
    assert ready["python"] == second["python"] == shared["python"] == str(base)
    assert len(calls) == before
    installs = [call for call in calls if "install" in call]
    assert len(installs) == 1
    assert all(call.count("--index-url") == 1 for call in installs)
    assert all("--isolated" in call and "--only-binary=:all:" in call for call in installs)
    assert installs[0][-1] == "Pillow>=11,<13"
    assert installs[0][installs[0].index("--constraint") + 1] == str(project / "requirements.txt")
    assert not any(argument.startswith("PySide6-Essentials") for argument in installs[0])
    assert all("--extra-index-url" not in call and "--trusted-host" not in call for call in installs)
    assert all("--target" not in call and "--upgrade" not in call and "--user" not in call for call in installs)
    assert all(call[0] == str(base) for call in calls)
    assert not (home / "envs").exists()
    assert any(event.get("install_scope") == "environment" for event in events)
    assert bootstrap.DEPENDENCY_INSTALL_POLICY == "shared-interpreter"


def test_mirror_retry_rechecks_partial_success_and_only_requests_remaining_packages(project, engine, tmp_path):
    python, calls, state = Path(sys.executable), [], {"attempt": 0}
    def probe(executable, project):
        return _missing_probe(executable, *({0: ["Pillow", "requests"], 1: ["requests"], 2: []}[state["attempt"]]))
    def run(command, emit, log_path):
        calls.append(list(map(str, command)))
        state["attempt"] += 1
        if state["attempt"] == 1:
            raise bootstrap.BootstrapError("connection interrupted after Pillow installation")
    with patch("bootstrap.probe_python", side_effect=probe), \
         patch("bootstrap.installation_scope", return_value={"scope": "environment", "target": str(tmp_path / "site")}), \
         patch("bootstrap.pip_command", return_value=[str(python), "-m", "pip"]), \
         patch("bootstrap.run_process", side_effect=run), patch("startup_engine.ensure_engine", return_value=engine):
        result = bootstrap.prepare_environment(project, python=python, home=tmp_path / "runtime")
    assert result["python"] == str(python)
    assert len(calls) == 2
    assert calls[0][-2:] == ["Pillow>=11,<13", "requests>=2.32,<3"]
    assert calls[1][-1] == "requests>=2.32,<3"
    assert "Pillow>=11,<13" not in calls[1]
    manifest = bootstrap.load_manifest(project)
    assert [call[call.index("--index-url") + 1] for call in calls] == [source["url"] for source in manifest["pip_indexes"][:2]]
    assert all(call.count("--index-url") == 1 and "--extra-index-url" not in call for call in calls)


def test_user_site_install_uses_standard_user_flag_without_private_target(project, tmp_path):
    python, calls = Path(sys.executable), []
    with patch("bootstrap.installation_scope", return_value={"scope": "user", "target": str(tmp_path / "user-site")}), \
         patch("bootstrap.pip_command", return_value=[str(python), "-m", "pip"]), \
         patch("bootstrap.run_process", side_effect=lambda command, emit, log: calls.append(command)), \
         patch("bootstrap.probe_python", return_value=_missing_probe(python)):
        bootstrap.install_missing_packages(python, project, tmp_path, bootstrap.load_manifest(project),
                                           _missing_probe(python, "Pillow"), lambda event: None)
    assert len(calls) == 1
    assert "--user" in calls[0]
    assert "--target" not in calls[0] and "venv" not in calls[0]


def test_broken_import_with_valid_metadata_is_repaired_without_reinstalling_dependencies(project, tmp_path):
    python, calls = Path(sys.executable), []
    broken = _missing_probe(python, "Pillow", broken={"Pillow": "11.0.0"})
    with patch("bootstrap.installation_scope", return_value={"scope": "environment", "target": str(tmp_path / "site")}), \
         patch("bootstrap.pip_command", return_value=[str(python), "-m", "pip"]), \
         patch("bootstrap.run_process", side_effect=lambda command, emit, log: calls.append(command)), \
         patch("bootstrap.probe_python", side_effect=[broken, _missing_probe(python)]):
        bootstrap.install_missing_packages(python, project, tmp_path, bootstrap.load_manifest(project), broken, lambda event: None)
    assert len(calls) == 2
    assert "--force-reinstall" not in calls[0]
    assert "--force-reinstall" in calls[1] and "--no-deps" in calls[1]
    assert calls[1][-1] == "Pillow>=11,<13"
    assert not any(argument.startswith("requests") for argument in calls[1])


def test_all_pip_mirrors_failing_report_actionable_error_without_private_env(project, tmp_path):
    python, calls, events = Path(sys.executable), [], []
    def fail(command, emit, log):
        calls.append(command)
        raise bootstrap.BootstrapError("permission denied or offline")
    manifest = bootstrap.load_manifest(project)
    with patch("bootstrap.installation_scope", return_value={"scope": "environment", "target": str(tmp_path / "site")}), \
         patch("bootstrap.pip_command", return_value=[str(python), "-m", "pip"]), \
         patch("bootstrap.run_process", side_effect=fail), \
         patch("bootstrap.probe_python", return_value=_missing_probe(python, "Pillow")), \
         pytest.raises(bootstrap.BootstrapError, match="本地 Python 组件补充未完成") as failure:
        bootstrap.install_missing_packages(python, project, tmp_path, manifest, _missing_probe(python, "Pillow"), events.append)
    assert len(calls) == len(manifest["pip_indexes"])
    assert all(source["name"] in str(failure.value) for source in manifest["pip_indexes"])
    assert "permission denied or offline" in str(failure.value)
    assert not (tmp_path / "envs").exists()


def test_successful_install_command_that_fails_import_check_tries_next_mirror(project, tmp_path):
    python, calls = Path(sys.executable), []
    missing = _missing_probe(python, "Pillow")
    with patch("bootstrap.installation_scope", return_value={"scope": "environment", "target": str(tmp_path / "site")}), \
         patch("bootstrap.pip_command", return_value=[str(python), "-m", "pip"]), \
         patch("bootstrap.run_process", side_effect=lambda command, emit, log: calls.append(command)), \
         patch("bootstrap.probe_python", side_effect=[missing, missing, _missing_probe(python)]):
        bootstrap.install_missing_packages(python, project, tmp_path, bootstrap.load_manifest(project), missing, lambda event: None)
    assert len(calls) == 2


def test_probe_keeps_normal_user_site_visible_and_reports_its_packages(project, tmp_path):
    python = Path(sys.executable)
    payload = _missing_probe(python)
    payload.update(user_site=str(tmp_path / "user-site"), user_enabled=True)
    with patch("bootstrap.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")) as run:
        result = bootstrap.probe_python(python, project)
    command = run.call_args.args[0]
    assert result["ready"] is True
    assert result["user_enabled"] is True and result["user_site"] == payload["user_site"]
    assert "-E" in command and "-I" not in command and "-s" not in command and "-S" not in command
    assert command[0] == str(python)
    assert bootstrap.PROBE_CODE in command
    assert "site.ENABLE_USER_SITE" in bootstrap.PROBE_CODE
    assert "distribution.locate_file" in bootstrap.PROBE_CODE


def test_probe_rejects_missing_metadata_and_out_of_range_versions(project):
    payload = _missing_probe(sys.executable)
    payload["packages"][0].update(version="99.0.0", ok=True)
    payload["packages"][2].update(version="", ok=False)
    with patch("bootstrap.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")):
        result = bootstrap.probe_python(sys.executable, project)
    assert result["ready"] is False
    assert result["missing"] == ["PySide6-Essentials", "Pillow"]


@pytest.mark.parametrize("scope", ["environment", "user"])
def test_installation_scope_returns_normal_environment_or_user_site(scope, tmp_path):
    payload = {"scope": scope, "target": str(tmp_path / scope)}
    with patch("bootstrap.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")) as run:
        assert bootstrap.installation_scope(sys.executable, _probe(sys.executable)) == payload
    command = run.call_args.args[0]
    assert command[0] == sys.executable and command[-1] == "environment"
    assert "-I" not in command and "-s" not in command


def test_outdated_or_broken_user_package_prefers_user_installation_scope(tmp_path):
    user_site = tmp_path / "user-site"
    probe = _missing_probe(sys.executable, "Pillow", broken={"Pillow": "11.0.0"})
    probe.update(user_site=str(user_site), user_enabled=True)
    probe["packages"][2]["location"] = str(user_site)
    payload = {"scope": "user", "target": str(user_site)}
    with patch("bootstrap.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")) as run:
        assert bootstrap.installation_scope(sys.executable, probe) == payload
    assert run.call_args.args[0][-1] == "user"


@pytest.mark.parametrize("normal_writable,user_enabled,user_writable,expected", [
    (True, True, True, "environment"), (False, True, True, "user"),
    (False, True, False, "unwritable"), (False, False, True, "unwritable"),
])
def test_scope_script_checks_normal_and_user_directory_permissions(tmp_path, capsys, normal_writable,
                                                                 user_enabled, user_writable, expected):
    normal, user = tmp_path / "standard-site", tmp_path / "user-site"
    normal.mkdir()
    user.mkdir()
    def temporary_file(*, dir):
        allowed = normal_writable if Path(dir) == normal else user_writable
        if not allowed:
            raise PermissionError("read-only test directory")
        return io.BytesIO()
    paths = {"purelib": str(normal), "platlib": str(normal), "scripts": str(normal)}
    with patch("sysconfig.get_paths", return_value=paths), patch("site.getusersitepackages", return_value=str(user)), \
         patch("site.ENABLE_USER_SITE", user_enabled), patch("tempfile.TemporaryFile", side_effect=temporary_file), \
         patch.object(sys, "argv", ["scope-probe", "environment"]), \
         patch.object(sys, "prefix", "test-base-python"), patch.object(sys, "base_prefix", "test-base-python"), \
         patch.object(sys, "path", list(sys.path)):
        exec(bootstrap.INSTALL_SCOPE_CODE, {})
    assert json.loads(capsys.readouterr().out)["scope"] == expected


@pytest.mark.parametrize("normal_writable,expected", [(True, "environment"), (False, "unwritable")])
@pytest.mark.parametrize("virtual_kind", ["venv", "legacy"])
def test_explicit_virtual_environment_never_installs_to_user_site(tmp_path, capsys, monkeypatch,
                                                               normal_writable, expected, virtual_kind):
    normal, user, checked = tmp_path / "venv-site", tmp_path / "user-site", []
    normal.mkdir()
    user.mkdir()
    def temporary_file(*, dir):
        checked.append(Path(dir))
        if Path(dir) == normal and not normal_writable:
            raise PermissionError("read-only venv")
        return io.BytesIO()
    paths = {"purelib": str(normal), "platlib": str(normal), "scripts": str(normal)}
    monkeypatch.setattr(sys, "prefix", "selected-venv")
    monkeypatch.setattr(sys, "base_prefix", "base-python" if virtual_kind == "venv" else "selected-venv")
    if virtual_kind == "legacy":
        monkeypatch.setattr(sys, "real_prefix", "base-python", raising=False)
    else:
        monkeypatch.delattr(sys, "real_prefix", raising=False)
    with patch("sysconfig.get_paths", return_value=paths), patch("site.getusersitepackages", return_value=str(user)), \
         patch("site.ENABLE_USER_SITE", True), patch("tempfile.TemporaryFile", side_effect=temporary_file), \
         patch.object(sys, "argv", ["scope-probe", "user"]), patch.object(sys, "path", list(sys.path)):
        exec(bootstrap.INSTALL_SCOPE_CODE, {})
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == expected
    assert result["target"] == str(normal)
    assert user not in checked
    assert not list(user.iterdir())


@pytest.mark.parametrize("payload", [
    {"scope": "unwritable", "target": "read-only-site"}, {"scope": "user"}, {},
])
def test_read_only_or_invalid_installation_scope_has_actionable_error(payload):
    with patch("bootstrap.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")), \
         pytest.raises(bootstrap.BootstrapError, match="没有写入权限") as failure:
        bootstrap.installation_scope(sys.executable, _probe(sys.executable))
    assert "--python" in str(failure.value)
    assert sys.executable in str(failure.value)


def test_read_only_scope_stops_before_any_pip_or_network(project, engine, tmp_path):
    with patch("bootstrap.probe_python", return_value=_missing_probe(sys.executable, "Pillow")), \
         patch("bootstrap.installation_scope", side_effect=bootstrap.BootstrapError("没有写入权限")), \
         patch("bootstrap.pip_command", side_effect=AssertionError("pip must not run")), \
         patch("bootstrap.download_artifact", side_effect=AssertionError("network must not run")), \
         patch("startup_engine.ensure_engine", side_effect=AssertionError("engine must not run")), \
         pytest.raises(bootstrap.BootstrapError, match="没有写入权限"):
        bootstrap.prepare_environment(project, home=tmp_path / "runtime")


def test_pip_is_reused_from_selected_interpreter_without_download(tmp_path):
    python = Path(sys.executable)
    with patch("bootstrap.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "pip already present", "")) as run, \
         patch("bootstrap.download_artifact", side_effect=AssertionError("must reuse pip")):
        command = bootstrap.pip_command(python, bootstrap.load_manifest(), tmp_path, lambda event: None)
    assert command[0] == str(python)
    assert command[-2] == "-c" and "from pip._internal.cli.main import main" in command[-1]
    assert run.call_args.args[0] == [*command, "--isolated", "--version"]
    assert "-I" not in command and "-s" not in command


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 1, "", "No module named pip"), OSError("unusable pip"),
])
def test_missing_pip_uses_verified_manifest_wheel_without_installing_pip(result, tmp_path):
    python, manifest = Path(sys.executable), bootstrap.load_manifest()
    wheel = tmp_path / "checked-pip.whl"
    run = patch("bootstrap.subprocess.run", side_effect=result) if isinstance(result, Exception) else patch("bootstrap.subprocess.run", return_value=result)
    with run, patch("bootstrap.download_artifact", return_value=wheel) as download:
        command = bootstrap.pip_command(python, manifest, tmp_path / "runtime", lambda event: None)
    assert command[0] == str(python)
    assert command[-2:] == [bootstrap.PIP_CODE, str(wheel)]
    assert "install" not in command
    assert download.call_args.args[:2] == (manifest["pip"], tmp_path / "runtime" / "downloads")


def test_source_handoff_uses_selected_shared_python(project, tmp_path, monkeypatch):
    launcher = project / "启动青简工具箱.exe"
    launcher.write_bytes(b"test only")
    monkeypatch.delenv("QINGJIAN_BOOTSTRAP_READY", raising=False)
    monkeypatch.setenv("QINGJIAN_RUNTIME_HOME", str(tmp_path / "runtime"))
    with patch("bootstrap.check_environment", return_value={"ready": False}), \
         patch("bootstrap.subprocess.Popen") as popen:
        assert bootstrap.start_source(project, ["--notebook"]) is False
    command = popen.call_args.args[0]
    assert command[:3] == [str(launcher), "--python", sys.executable]
    assert command[-2:] == ["--", "--notebook"]


@pytest.mark.parametrize("no_user_site,isolated,no_site", [(1, 0, 0), (0, 1, 0), (1, 1, 0), (0, 0, 1)])
def test_source_with_user_site_disabled_restarts_in_standard_user_site_mode(project, engine, tmp_path, monkeypatch,
                                                                         no_user_site, isolated, no_site):
    monkeypatch.delenv("QINGJIAN_BOOTSTRAP_READY", raising=False)
    monkeypatch.setenv("QINGJIAN_RUNTIME_HOME", str(tmp_path / "runtime"))
    checked = {"ready": True, "engine": str(engine), "user_enabled": True}
    with patch("bootstrap.check_environment", return_value=checked), \
         patch("bootstrap.prepare_environment", side_effect=AssertionError("already ready; do not install")), \
         patch.object(sys, "flags", types.SimpleNamespace(no_user_site=no_user_site, isolated=isolated, no_site=no_site)), \
         patch("bootstrap.subprocess.Popen") as popen:
        assert bootstrap.start_source(project, ["--notebook"]) is False
    command = popen.call_args.args[0]
    assert command == [sys.executable, "-E", "-X", "utf8", str(project / "main.py"), "--notebook"]
    assert "-I" not in command and "-s" not in command
    assert popen.call_args.kwargs["env"]["QINGJIAN_BOOTSTRAP_READY"] == bootstrap.environment_stamp(project, sys.executable)
    assert "QINGJIAN_BOOTSTRAP_READY" not in os.environ


def test_source_without_launcher_restarts_prepared_interpreter_with_same_flags(project, engine, tmp_path, monkeypatch):
    monkeypatch.delenv("QINGJIAN_BOOTSTRAP_READY", raising=False)
    monkeypatch.setenv("QINGJIAN_RUNTIME_HOME", str(tmp_path / "runtime"))
    python = str(tmp_path / "selected-python.exe")
    ready = {"python": python, "environment": {"QINGJIAN_BOOTSTRAP_READY": "prepared-token", "QINGJIAN_LIBREOFFICE": str(engine)}}
    with patch("bootstrap.check_environment", return_value={"ready": False}), \
         patch("bootstrap.prepare_environment", return_value=ready) as prepare, \
         patch("bootstrap.subprocess.Popen") as popen:
        assert bootstrap.start_source(project, ["--notebook"]) is False
    assert prepare.call_count == 1
    assert popen.call_args.args[0] == [python, "-E", "-X", "utf8", str(project / "main.py"), "--notebook"]
    assert popen.call_args.kwargs["env"]["QINGJIAN_BOOTSTRAP_READY"] == "prepared-token"
    assert "QINGJIAN_BOOTSTRAP_READY" not in os.environ


def test_offline_missing_dependencies_do_not_run_pip(project, engine, tmp_path):
    with patch("bootstrap.probe_python", return_value=_probe(sys.executable, ready=False)), \
         patch("bootstrap.run_process", side_effect=AssertionError("install")), \
         pytest.raises(bootstrap.BootstrapError, match="离线"):
        bootstrap.prepare_environment(project, home=tmp_path / "private", offline=True)


def test_cleanup_refuses_foreign_and_outside_directories(tmp_path):
    parent = tmp_path / "envs"
    parent.mkdir()
    foreign = parent / ".env-someone-else"
    foreign.mkdir()
    (foreign / "important.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap._owned_remove(foreign, parent)
    assert (foreign / "important.txt").is_file()
    owned = parent / ".env-owned"
    owned.mkdir()
    (owned / ".qingjian-owned").write_text("test", encoding="utf-8")
    bootstrap._owned_remove(owned, parent)
    assert not owned.exists()


def test_ready_token_changes_when_requirements_change(project):
    before = bootstrap.environment_stamp(project, sys.executable)
    with (project / "requirements.txt").open("a", encoding="utf-8") as stream:
        stream.write("\n# new release\n")
    assert bootstrap.environment_stamp(project, sys.executable) != before


def test_frozen_entry_never_prepares_or_downloads_environment(monkeypatch):
    spec = importlib.util.spec_from_file_location("qingjian_entry_test", bootstrap.PROJECT / "main.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    fake = types.ModuleType("qingjian.app")
    fake.main = lambda: 17
    monkeypatch.setitem(sys.modules, "qingjian.app", fake)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["青简工具箱.exe"])
    with patch("bootstrap.start_source", side_effect=AssertionError("frozen network")):
        assert entry.run() == 17


def test_future_source_contract_is_part_of_the_project():
    project = bootstrap.PROJECT
    policy = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "每轮更新" in policy and "HTTPS" in policy
    for name in ("bootstrap.py", "startup_engine.py", "runtime-sources.json", "启动青简工具箱.exe", "launcher/SourceLauncher.cs"):
        assert (project / name).is_file(), name
