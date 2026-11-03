"""Offline wheel supplements stay verified and pip failures stay actionable.

No test installs a package or changes the selected interpreter.  Real child
processes only emit representative pip output and terminate.
"""
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest

import bootstrap


def _write_manifest(project, artifacts, *, schema_version=1):
    manifest = {"schema_version": schema_version, "artifacts": artifacts}
    (project / "bootstrap-wheels.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def wheel_project(tmp_path):
    project = tmp_path / "含 空格的源码"
    project.mkdir()
    wheel_dir = project / "bootstrap_wheels"
    wheel_dir.mkdir()
    wheel = wheel_dir / "fixture_dependency-1.0-py3-none-any.whl"
    # Verification is over immutable distribution bytes, not an imported module.
    wheel.write_bytes(b"a local fixture wheel; never imported or installed")
    artifact = {"filename": wheel.name, "bytes": wheel.stat().st_size,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    _write_manifest(project, [artifact])
    (project / "requirements.txt").write_text("Pillow>=11,<13\n", encoding="utf-8")
    return project, wheel, artifact


def _probe(*, ready=False):
    return {"python": sys.executable, "supported": True, "ready": ready,
            "missing": [] if ready else ["Pillow"],
            "packages": [{"name": "Pillow", "ok": ready,
                          "version": "11.0.0" if ready else "", "error": ""}]}


def _pip_manifest():
    return {"pip_indexes": [
        {"name": "官方源", "url": "https://official.example/simple"},
        {"name": "国内备用一", "url": "https://first.example/simple"},
        {"name": "国内备用二", "url": "https://last.example/simple"},
    ]}


def test_distribution_supplements_are_universal_python_with_mit_license():
    wheels = bootstrap.verified_bootstrap_wheels(bootstrap.PROJECT)
    assert wheels, "The source release must include its verified supplements."
    for wheel in wheels:
        assert wheel.name.endswith("-py3-none-any.whl")
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert archive.testzip() is None
            assert all(not PurePosixPath(name).is_absolute()
                       and ".." not in PurePosixPath(name).parts for name in names)
            assert not any(Path(name).suffix.lower() in {".dll", ".pyd", ".so", ".dylib", ".exe"}
                           for name in names), wheel.name
            wheel_metadata = [name for name in names if name.endswith(".dist-info/WHEEL")]
            assert len(wheel_metadata) == 1
            metadata = archive.read(wheel_metadata[0]).decode("utf-8")
            assert "Root-Is-Purelib: true" in metadata
            assert "Tag: py3-none-any" in metadata
            licenses = [name for name in names if any("license" in part.lower() for part in PurePosixPath(name).parts)
                        or Path(name).name.lower() == "copying"]
            assert licenses, wheel.name
            assert any("Permission is hereby granted, free of charge" in
                       archive.read(name).decode("utf-8-sig", errors="replace")
                       for name in licenses), wheel.name


def test_only_manifest_wheels_are_selected(wheel_project):
    project, wheel, _ = wheel_project
    (wheel.parent / "unreviewed-99.0-py3-none-any.whl").write_bytes(b"not trusted")
    assert bootstrap.verified_bootstrap_wheels(project) == [wheel.resolve()]


@pytest.mark.parametrize("change", ["missing_manifest", "invalid_json", "wrong_schema", "empty", "too_many"])
def test_unusable_wheel_manifests_fail_closed(wheel_project, change):
    project, _, artifact = wheel_project
    manifest = project / "bootstrap-wheels.json"
    if change == "missing_manifest":
        manifest.unlink()
    elif change == "invalid_json":
        manifest.write_text("{broken", encoding="utf-8")
    elif change == "wrong_schema":
        _write_manifest(project, [artifact], schema_version=2)
    elif change == "empty":
        _write_manifest(project, [])
    else:
        _write_manifest(project, [artifact] * 17)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.verified_bootstrap_wheels(project)


@pytest.mark.parametrize("change", ["missing", "same_size_tampering", "wrong_size", "bad_hash", "oversize"])
def test_missing_or_unverified_wheel_bytes_are_rejected(wheel_project, change):
    project, wheel, artifact = wheel_project
    if change == "missing":
        wheel.unlink()
    elif change == "same_size_tampering":
        original = wheel.read_bytes()
        wheel.write_bytes(b"X" + original[1:])
    elif change == "wrong_size":
        artifact["bytes"] += 1
    elif change == "bad_hash":
        artifact["sha256"] = "not a sha256"
    else:
        artifact["bytes"] = 1024 * 1024 + 1
    _write_manifest(project, [artifact])
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.verified_bootstrap_wheels(project)


@pytest.mark.parametrize("filename", [
    "../outside-1-py3-none-any.whl",
    "..\\outside-1-py3-none-any.whl",
    "/outside-1-py3-none-any.whl",
    "C:\\outside-1-py3-none-any.whl",
    "subdir/fixture_dependency-1.0-py3-none-any.whl",
    "fixture_dependency-1.0-cp312-cp312-win_amd64.whl",
    "fixture_dependency-1.0-py3-none-any.whl?download=1",
    "fixture_dependency-1.0-py3-none-any.whl\n",
])
def test_manifest_cannot_select_paths_or_native_wheels(wheel_project, filename):
    project, _, artifact = wheel_project
    artifact["filename"] = filename
    _write_manifest(project, [artifact])
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.verified_bootstrap_wheels(project)


@pytest.mark.parametrize("target_kind", ["directory", "wheel"])
def test_wheel_directory_and_wheel_symlinks_are_rejected(wheel_project, tmp_path, target_kind):
    project, wheel, _ = wheel_project
    if target_kind == "directory":
        original, target = wheel.parent, tmp_path / "outside-wheels"
    else:
        original, target = wheel, tmp_path / wheel.name
    original.rename(target)
    try:
        original.symlink_to(target, target_is_directory=target_kind == "directory")
    except OSError as exc:
        pytest.skip("Creating a test symlink is unavailable on this host: " + str(exc))
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.verified_bootstrap_wheels(project)


def test_pip_gets_only_exact_verified_wheel_uris(wheel_project, tmp_path):
    project, wheel, _ = wheel_project
    other = wheel.parent / "unreviewed-99.0-py3-none-any.whl"
    other.write_bytes(b"not trusted")
    calls = []
    with patch("bootstrap.installation_scope", return_value={"scope": "environment", "target": "selected-site"}), \
         patch("bootstrap.pip_command", return_value=[sys.executable, "-m", "pip"]), \
         patch("bootstrap.run_process", side_effect=lambda command, emit, log: calls.append(list(command))), \
         patch("bootstrap.probe_python", return_value=_probe(ready=True)):
        bootstrap.install_missing_packages(Path(sys.executable), project, tmp_path / "runtime",
                                           _pip_manifest(), _probe(), lambda event: None)
    assert len(calls) == 1
    command = calls[0]
    links = [command[index + 1] for index, item in enumerate(command) if item == "--find-links"]
    assert links == [wheel.resolve().as_uri()]
    assert other.as_uri() not in command
    assert str(wheel.parent) not in command and wheel.parent.as_uri() not in command
    assert "--only-binary=:all:" in command
    assert "--extra-index-url" not in command and "--trusted-host" not in command
    assert command.count("--index-url") == 1
    assert command[-1] == "Pillow>=11,<13"


def test_ready_install_entry_does_not_validate_wheels_or_run_pip(wheel_project, tmp_path):
    project, _, _ = wheel_project
    with patch("bootstrap.verified_bootstrap_wheels", side_effect=AssertionError("already complete")), \
         patch("bootstrap.installation_scope", side_effect=AssertionError("no installation")), \
         patch("bootstrap.pip_command", side_effect=AssertionError("no pip")), \
         patch("bootstrap.run_process", side_effect=AssertionError("no process")):
        bootstrap.install_missing_packages(Path(sys.executable), project, tmp_path / "runtime",
                                           _pip_manifest(), _probe(ready=True), lambda event: None)


def test_complete_environment_starts_without_reading_wheel_supplements(wheel_project, tmp_path):
    project, _, _ = wheel_project
    # Existing usable Python environments do not depend on supplement files.
    shutil.rmtree(project / "bootstrap_wheels")
    (project / "bootstrap-wheels.json").unlink()
    with patch("bootstrap.load_manifest", return_value=_pip_manifest()), \
         patch("bootstrap.probe_python", return_value=_probe(ready=True)), \
         patch("bootstrap.verified_bootstrap_wheels", side_effect=AssertionError("do not validate unused wheels")), \
         patch("bootstrap.install_missing_packages", side_effect=AssertionError("do not install")), \
         patch("bootstrap.download_artifact", side_effect=AssertionError("no network")), \
         patch("startup_engine.ensure_engine", return_value=tmp_path / "engine"), \
         patch("startup_native.ensure_native", return_value={}), \
         patch("bootstrap.ready_environment", return_value={"QINGJIAN_BOOTSTRAP_READY": "ready"}):
        result = bootstrap.prepare_environment(project, python=sys.executable,
                                               home=tmp_path / "runtime", offline=True)
    assert result["python"] == sys.executable


@pytest.mark.parametrize("message", [
    "ERROR: ResolutionImpossible: the requested dependency graph has no solution",
    "ERROR: Cannot install these versions because these package versions have conflicting dependencies.",
])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_real_failed_subprocess_is_classified_as_dependency_resolution(tmp_path, message, stream):
    log = tmp_path / "logs" / "pip-resolution.log"
    script = ("import sys\n"
              "print('harmless download progress\\n' * 130, flush=True)\n"
              f"print({message!r}, file=sys.{stream})\n"
              "raise SystemExit(1)\n")
    with pytest.raises(bootstrap.DependencyResolutionError) as failure:
        bootstrap.run_process([sys.executable, "-X", "utf8", "-c", script],
                              lambda event: None, log, timeout=20)
    assert isinstance(failure.value, bootstrap.BootstrapError)
    assert str(log) in str(failure.value)
    assert message in log.read_text(encoding="utf-8")


def test_real_generic_failure_keeps_non_resolution_class(tmp_path):
    log = tmp_path / "logs" / "pip-generic.log"
    script = "print('PermissionError: access denied'); raise SystemExit(1)"
    with pytest.raises(bootstrap.BootstrapError) as failure:
        bootstrap.run_process([sys.executable, "-c", script], lambda event: None, log, timeout=20)
    assert not isinstance(failure.value, bootstrap.DependencyResolutionError)
    assert str(log) in str(failure.value)


def test_successful_child_output_is_not_treated_as_resolution_failure(tmp_path):
    bootstrap.run_process(
        [sys.executable, "-c", "print('ResolutionImpossible mentioned in help text only')"],
        lambda event: None, tmp_path / "logs" / "success.log", timeout=20)


@pytest.mark.parametrize("resolution_failure", [False, True])
def test_last_mirror_failure_does_not_promise_another_mirror(wheel_project, tmp_path, resolution_failure):
    project, _, _ = wheel_project
    manifest, calls, events = _pip_manifest(), [], []
    error_type = bootstrap.DependencyResolutionError if resolution_failure else bootstrap.BootstrapError

    def fail(command, emit, log):
        calls.append(list(command))
        raise error_type(("依赖解析失败，" if resolution_failure else "连接中断，") + "详细日志：" + str(log))

    with patch("bootstrap.installation_scope", return_value={"scope": "environment", "target": "selected-site"}), \
         patch("bootstrap.pip_command", return_value=[sys.executable, "-m", "pip"]), \
         patch("bootstrap.run_process", side_effect=fail), \
         patch("bootstrap.probe_python", return_value=_probe()), \
         pytest.raises(bootstrap.BootstrapError) as failure:
        bootstrap.install_missing_packages(Path(sys.executable), project, tmp_path / "runtime",
                                           manifest, _probe(), events.append)
    assert len(calls) == len(manifest["pip_indexes"])
    switches = [event["message"] for event in events if "切换" in event.get("message", "")]
    assert len(switches) == len(manifest["pip_indexes"]) - 1
    assert all(manifest["pip_indexes"][-1]["name"] not in message for message in switches)
    error = str(failure.value)
    assert all(source["name"] in error for source in manifest["pip_indexes"])
    assert all("pip-" + str(index) + ".log" in error for index in range(len(calls)))
    if resolution_failure:
        assert "依赖" in error
        assert "请检查网络或安装权限" not in error
    else:
        assert not isinstance(failure.value, bootstrap.DependencyResolutionError)
