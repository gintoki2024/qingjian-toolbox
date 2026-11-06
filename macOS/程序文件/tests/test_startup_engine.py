"""Source startup engine protections, with no real network or installer use."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import startup_engine as engine


def make_engine(root: Path) -> Path:
    program = root / "program"
    program.mkdir(parents=True)
    for name in ("soffice.com", "soffice.bin", "fundamental.ini"):
        (program / name).write_bytes(b"test")
    return root.resolve()


@pytest.fixture
def spec():
    payload = b"verified MSI test bytes"
    return {"version": "26.2.6", "filename": "LibreOffice_test.msi", "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "urls": [{"name": "官方", "url": "https://official.example/engine.msi"},
                     {"name": "国内备用", "url": "https://mirror.example/engine.msi"}]}


@pytest.fixture
def clean_discovery(monkeypatch):
    for name in ("QINGJIAN_LIBREOFFICE", "ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(engine.shutil, "which", lambda _name: None)


def test_find_prioritizes_project_and_accepts_program_path(tmp_path, monkeypatch, clean_discovery):
    project = tmp_path / "source"
    expected = make_engine(project / "engines" / "LibreOffice")
    alternative = make_engine(tmp_path / "configured")
    monkeypatch.setenv("QINGJIAN_LIBREOFFICE", str(alternative / "program" / "soffice.com"))
    assert engine.find_engine(project, tmp_path / "runtime") == expected
    (expected / "program" / "soffice.bin").unlink()
    assert engine.find_engine(project, tmp_path / "runtime") == alternative


def test_find_prefers_existing_shared_engine_over_removable_neighbor(tmp_path, clean_discovery):
    make_engine(tmp_path / "青简工具箱" / "engines" / "LibreOffice")
    expected = make_engine(tmp_path / "runtime" / "lo-26.2.6")
    assert engine.find_engine(tmp_path / "源码", tmp_path / "runtime") == expected


def test_find_versioned_portable_sorts_versions_numerically_and_skips_incomplete(tmp_path, clean_discovery):
    make_engine(tmp_path / "青简工具箱_v0.6.0" / "engines" / "LibreOffice")
    make_engine(tmp_path / "青简工具箱_v0.9.9" / "engines" / "LibreOffice")
    expected = make_engine(tmp_path / "青简工具箱_v0.10.1" / "engines" / "LibreOffice")
    incomplete = make_engine(tmp_path / "青简工具箱_v1.0.0" / "engines" / "LibreOffice")
    (incomplete / "program" / "fundamental.ini").unlink()
    assert engine.find_engine(tmp_path / "源码", tmp_path / "runtime") == expected


def test_find_versioned_portable_accepts_engine_directly_in_engines(tmp_path, clean_discovery):
    expected = make_engine(tmp_path / "青简工具箱_v0.6.0" / "engines")
    assert engine.find_engine(tmp_path / "源码", tmp_path / "runtime") == expected


def test_find_versioned_portable_keeps_explicit_engine_priority(tmp_path, monkeypatch, clean_discovery):
    make_engine(tmp_path / "青简工具箱_v0.6.0" / "engines" / "LibreOffice")
    configured = make_engine(tmp_path / "configured")
    monkeypatch.setenv("QINGJIAN_LIBREOFFICE", str(configured))
    assert engine.find_engine(tmp_path / "源码", tmp_path / "runtime") == configured


def test_find_ignores_unrelated_names_and_arbitrary_nested_folders(tmp_path, clean_discovery):
    for name in ("青简工具箱_v0.6", "unrelated_v0.6.0_源码", "other_tools_Windows64"):
        make_engine(tmp_path / name / "engines" / "LibreOffice")
    make_engine(tmp_path / "other" / "青简工具箱_v99.0.0" / "engines" / "LibreOffice")
    assert engine.find_engine(tmp_path / "源码", tmp_path / "runtime") is None


def test_ensure_reuses_versioned_neighbor_without_downloading(tmp_path, spec, monkeypatch, clean_discovery):
    original = make_engine(tmp_path / "青简工具箱_v0.6.0" / "engines" / "LibreOffice")
    expected = tmp_path / 'runtime/lo-26.2.6'
    monkeypatch.setattr(engine, "_download_msi", lambda *a, **k: pytest.fail("download used"))
    monkeypatch.setattr(engine, '_probe_engine', lambda *a: None)
    assert engine.ensure_engine(tmp_path / "源码", tmp_path / "runtime", {"libreoffice": spec},
                                lambda _: None, offline=True) == expected
    assert (original / 'program/soffice.bin').read_bytes() == (expected / 'program/soffice.bin').read_bytes()


def test_find_ignores_incomplete_engine_and_unsafe_version(tmp_path, clean_discovery):
    incomplete = tmp_path / "runtime" / "lo-26.2.6" / "program"
    incomplete.mkdir(parents=True)
    (incomplete / "soffice.exe").write_bytes(b"not enough")
    assert engine.find_engine(tmp_path / "source", tmp_path / "runtime") is None
    assert engine.find_engine(tmp_path / "source", tmp_path / "runtime", "../outside") is None


def test_find_reuses_new_three_entry_neighbor_layout(tmp_path, spec, monkeypatch, clean_discovery):
    project = tmp_path / '青简工具箱源码' / '程序文件'
    project.mkdir(parents=True)
    original = make_engine(tmp_path / '青简工具箱_v0.9.0' / '程序文件' / 'engines' / 'LibreOffice')
    expected = tmp_path / 'chosen-runtime/lo-26.2.6'
    monkeypatch.setattr(engine, '_download_msi', lambda *a, **k: pytest.fail('existing engine must be reused'))
    monkeypatch.setattr(engine, '_probe_engine', lambda *a: None)
    assert engine.ensure_engine(project, tmp_path / 'chosen-runtime', {'libreoffice': spec}, lambda _: None, offline=True) == expected
    assert original.is_dir()


def test_find_nested_source_still_accepts_old_neighbor_layout(tmp_path, clean_discovery):
    project = tmp_path / '青简工具箱源码' / '程序文件'
    project.mkdir(parents=True)
    expected = make_engine(tmp_path / '青简工具箱_v0.8.1' / 'engines' / 'LibreOffice')
    assert engine.find_engine(project, tmp_path / 'runtime') == expected


def test_old_source_engine_is_preserved_before_old_release_is_deleted(tmp_path, spec, monkeypatch, clean_discovery):
    old = tmp_path / '青简工具箱_v0.10.2_CMD源码' / '青简工具箱源码' / '程序文件'
    original = make_engine(old / 'runtime/lo-26.2.6')
    project = tmp_path / '青简工具箱_v0.10.3_CMD源码_修正' / '青简工具箱源码' / '程序文件'
    project.mkdir(parents=True)
    home = tmp_path / '青简共享环境'
    monkeypatch.setattr(engine, '_download_msi', lambda *a, **k: pytest.fail('must preserve the old engine offline'))
    probes = []
    monkeypatch.setattr(engine, '_probe_engine', probes.append)
    copied = engine.ensure_engine(project, home, {'libreoffice': spec}, lambda _: None, offline=True)
    assert copied == home / 'lo-26.2.6'
    assert (copied / 'program/soffice.bin').read_bytes() == (original / 'program/soffice.bin').read_bytes()
    assert len(probes) == 1
    old_release = old.parents[1].resolve()
    assert old_release.is_relative_to(tmp_path.resolve())
    shutil.rmtree(old_release)
    assert engine.ensure_engine(project, home, {'libreoffice': spec}, lambda _: None, offline=True) == copied
    assert len(probes) == 1  # Reuse does not copy or start another migration.


def test_preservation_failure_keeps_original_and_cleans_only_its_stage(tmp_path, spec, monkeypatch, clean_discovery):
    project = tmp_path / '青简工具箱源码/程序文件'
    original = make_engine(project / 'runtime/lo-26.2.6')
    home = tmp_path / '青简共享环境'
    monkeypatch.setattr(engine, '_download_msi', lambda *a, **k: pytest.fail('must not download after copy failure'))
    monkeypatch.setattr('startup_shared.copy_component_tree', lambda *a: (_ for _ in ()).throw(OSError('fixture disk full')))
    with pytest.raises(engine.EngineSetupError, match='原目录保持不变'):
        engine.ensure_engine(project, home, {'libreoffice': spec}, lambda _: None, offline=True)
    assert (original / 'program/soffice.bin').read_bytes() == b'test'
    assert not (home / 'lo-26.2.6').exists()
    assert not list(home.glob('lo-stage-*'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows long-path preservation')
def test_preservation_keeps_deep_engine_files_without_system_policy_change(tmp_path, spec, monkeypatch, clean_discovery):
    project = tmp_path / '青简工具箱源码/程序文件'
    original = make_engine(project / 'runtime/lo-26.2.6')
    relative = Path('share') / ('m' * 95) / ('n' * 95) / 'data.txt'
    deep = Path('\\\\?\\' + str(original / relative))
    assert len(str(deep)) > 260
    deep.parent.mkdir(parents=True)
    deep.write_bytes(b'long engine resource')
    home = tmp_path / '青简共享环境'
    monkeypatch.setattr(engine, '_download_msi', lambda *a, **k: pytest.fail('must not download'))
    monkeypatch.setattr(engine, '_probe_engine', lambda *a: None)
    copied = engine.ensure_engine(project, home, {'libreoffice': spec}, lambda _: None, offline=True)
    assert Path('\\\\?\\' + str(copied / relative)).read_bytes() == b'long engine resource'


def test_file_plan_restores_msi_target_names_and_isolates_arm64():
    directories = [["TARGETDIR", "", "SourceDir"], ["INSTALLLOCATION", "PFiles", "LibreOffice:."],
                   ["program", "INSTALLLOCATION", "PROGRA~1|program:source"],
                   ["child", "program", "."], ["sys", "TARGETDIR", "System64"]]
    components = [["base", "child"], ["runtime_amd64.test", "sys"], ["runtime_arm64.test", "sys"]]
    files = [["app", "base", "SOFFIC~1.COM|soffice.com", "100", "1"],
             ["vcruntime_amd64.test", "runtime_amd64.test", "VC~1.DLL|vcruntime140.dll", "2", "2"],
             ["vcruntime_arm64.test", "runtime_arm64.test", "VC~2.DLL|vcruntime140.dll", "3", "3"]]
    plan = engine._file_plan(directories, components, files)
    assert plan["app"].relative == Path("program/soffice.com")
    assert plan["vcruntime_amd64.test"].relative == Path("System64/vcruntime140.dll")
    assert plan["vcruntime_arm64.test"].relative == Path("System64-arm64/vcruntime140.dll")


@pytest.mark.parametrize("bad_name", ["..", "../escape", "C:\\escape", "sub/file", "x\x00x", "CON", "NUL.txt", "bad."])
def test_file_plan_rejects_unsafe_target_names(bad_name):
    with pytest.raises(engine.EngineSetupError):
        engine._file_plan([["program", "INSTALLLOCATION", "program"]], [["c", "program"]],
                          [["safe-id", "c", bad_name, "1", "1"]])


def test_file_plan_rejects_directory_cycle_and_destination_collision():
    with pytest.raises(engine.EngineSetupError, match="循环"):
        engine._file_plan([["a", "b", "a"], ["b", "a", "b"]], [["c", "a"]],
                          [["f", "c", "x", "1", "1"]])
    with pytest.raises(engine.EngineSetupError, match="冲突"):
        engine._file_plan([["p", "INSTALLLOCATION", "program"]], [["c", "p"]],
                          [["f1", "c", "X", "1", "1"], ["f2", "c", "x", "1", "2"]])


@pytest.mark.parametrize("change", [
    {"filename": "../engine.msi"}, {"sha256": "f" * 63}, {"bytes": -1}, {"bytes": True},
    {"version": "../../other"}, {"urls": []},
    {"urls": [{"name": "bad", "url": "http://mirror.example/runtime.msi"}]},
    {"urls": [{"name": "bad", "url": "https://user:password@mirror.example/runtime.msi"}]},
])
def test_manifest_requires_pinned_integrity_and_https(spec, change):
    with pytest.raises(engine.EngineSetupError, match="清单"):
        engine._manifest_spec({"libreoffice": {**spec, **change}})


class Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str):
        super().__init__(payload)
        self.url = url

    def geturl(self):
        return self.url


def test_download_switches_after_bad_hash_and_keeps_tls_verification(tmp_path, spec, monkeypatch):
    calls = []
    events = []

    def open_url(request, *, timeout, context):
        calls.append(request.full_url)
        assert timeout > 0
        assert context.check_hostname is True
        payload = b"x" * spec["bytes"] if len(calls) == 1 else b"verified MSI test bytes"
        return Response(payload, request.full_url)

    monkeypatch.setattr(engine.urllib.request, "urlopen", open_url)
    downloaded = engine._download_msi(tmp_path, spec, events.append, offline=False)
    assert downloaded.read_bytes() == b"verified MSI test bytes"
    assert len(calls) == 2
    assert not list(downloaded.parent.glob("*.part"))
    assert any(event.get("source") == "国内备用" for event in events)


def test_valid_cache_works_offline_without_network(tmp_path, spec, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    expected = cache / spec["filename"]
    expected.write_bytes(b"verified MSI test bytes")
    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network used"))
    assert engine._download_msi(tmp_path, spec, lambda _: None, offline=True) == expected


def test_corrupt_cache_offline_fails_without_extracting_or_altering_it(tmp_path, spec):
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / spec["filename"]
    cached.write_bytes(b"wrong")
    with pytest.raises(engine.EngineSetupError, match="离线模式"):
        engine._download_msi(tmp_path, spec, lambda _: None, offline=True)
    assert cached.read_bytes() == b"wrong"


def test_download_rejects_https_to_http_redirect_and_excess_bytes(tmp_path, spec, monkeypatch):
    calls = []

    def open_url(request, **kwargs):
        calls.append(request.full_url)
        if len(calls) == 1:
            return Response(b"verified MSI test bytes", "http://unsafe.example/data")
        return Response(b"x" * (spec["bytes"] + 1), request.full_url)

    monkeypatch.setattr(engine.urllib.request, "urlopen", open_url)
    with pytest.raises(engine.EngineSetupError, match="所有.*下载源"):
        engine._download_msi(tmp_path, spec, lambda _: None, offline=False)
    assert len(calls) == 2
    assert not list((tmp_path / "cache").glob("*.part"))


def test_stage_cleanup_requires_matching_owner_and_direct_child(tmp_path):
    stage = tmp_path / "lo-stage-one"
    stage.mkdir()
    (stage / "precious.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(engine.EngineSetupError, match="归属"):
        engine._cleanup_stage(stage, tmp_path, "token")
    assert (stage / "precious.txt").is_file()
    (stage / engine._STAGE_OWNER).write_text(json.dumps(
        {"app": engine._APP, "kind": "stage", "token": "token"}), encoding="utf-8")
    with pytest.raises(engine.EngineSetupError, match="归属"):
        engine._cleanup_stage(stage, tmp_path / "different", "token")
    engine._cleanup_stage(stage, tmp_path, "token")
    assert not stage.exists()


def test_child_rejects_link(tmp_path):
    destination = tmp_path / "outside"
    destination.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip("Windows test account cannot create symlinks")
    with pytest.raises(engine.EngineSetupError, match="路径链接"):
        engine._child(tmp_path, "linked")


def test_unpack_checks_space_before_exporting_cabinet(tmp_path, monkeypatch):
    msi = tmp_path / "engine.msi"
    msi.write_bytes(b"test")
    stage = tmp_path / "stage"
    stage.mkdir()

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def rows(self, query, _columns):
            if "FROM `Directory`" in query:
                return [["p", "INSTALLLOCATION", "program"]]
            if "FROM `Component`" in query:
                return [["c", "p"]]
            if "FROM `File`" in query:
                return [["f", "c", "soffice.com", "10", "1"]]
            pytest.fail("CAB read attempted without disk space")

    monkeypatch.setattr(engine, "_MsiDatabase", lambda _: Database())
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(engine.EngineSetupError, match="可用空间"):
        engine._extract_msi(msi, stage, lambda _: None)


def test_ensure_reuses_complete_engine_without_downloading(tmp_path, spec, monkeypatch):
    expected = make_engine(tmp_path / "existing")
    monkeypatch.setattr(engine, "find_engine", lambda *a: expected)
    monkeypatch.setattr(engine, "_download_msi", lambda *a, **k: pytest.fail("download used"))
    assert engine.ensure_engine(tmp_path / "source", tmp_path / "runtime", {"libreoffice": spec},
                                lambda _: None, offline=True) == expected


def test_old_verified_download_cache_is_reused_offline(tmp_path, spec, monkeypatch):
    old_home, new_home = tmp_path / 'old-runtime', tmp_path / 'shared-runtime'
    old_cache = old_home / 'cache' / spec['filename']
    old_cache.parent.mkdir(parents=True)
    old_cache.write_bytes(b'verified MSI test bytes')
    new_home.mkdir()
    monkeypatch.setattr(engine.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('cached archive must not fetch'))
    assert engine._download_msi(new_home, spec, lambda _: None, offline=True, cache_homes=[old_home]) == old_cache
    assert not (new_home / 'cache' / spec['filename']).exists()


def test_damaged_old_download_cache_is_not_trusted(tmp_path, spec, monkeypatch):
    old_home, new_home = tmp_path / 'old-runtime', tmp_path / 'shared-runtime'
    old_cache = old_home / 'cache' / spec['filename']
    old_cache.parent.mkdir(parents=True)
    old_cache.write_bytes(b'changed MSI test bytes!')
    new_home.mkdir()
    monkeypatch.setattr(engine.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('offline must not fetch'))
    with pytest.raises(engine.EngineSetupError, match='离线模式'):
        engine._download_msi(new_home, spec, lambda _: None, offline=True, cache_homes=[old_home])


@pytest.mark.skipif(os.name != "nt", reason="Windows private engine installation")
def test_ensure_publishes_only_after_probe_and_preserves_unknown_target(tmp_path, spec, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    unknown = runtime / "lo-26.2.6"
    unknown.mkdir()
    (unknown / "user-file.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(engine, "find_engine", lambda *a: None)
    monkeypatch.setattr(engine, "_download_msi", lambda *a, **k: tmp_path / "cached.msi")
    monkeypatch.setattr(engine, "_extract_msi", lambda _msi, stage, _emit: make_engine(stage / "engine"))
    probes = []
    monkeypatch.setattr(engine, "_probe_engine", lambda root: probes.append(root))
    with pytest.raises(engine.EngineSetupError, match="不属于本程序"):
        engine.ensure_engine(tmp_path / "source", runtime, {"libreoffice": spec}, lambda _: None)
    assert len(probes) == 1
    assert (unknown / "user-file.txt").read_text(encoding="utf-8") == "keep"
    assert not list(runtime.glob("lo-stage-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows private engine installation")
def test_ensure_failed_probe_cleans_owned_stage_and_preserves_cache(tmp_path, spec, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache = runtime / "cache.msi"
    cache.write_bytes(b"keep cached download")
    monkeypatch.setattr(engine, "find_engine", lambda *a: None)
    monkeypatch.setattr(engine, "_download_msi", lambda *a, **k: cache)
    monkeypatch.setattr(engine, "_extract_msi", lambda _msi, stage, _emit: make_engine(stage / "engine"))
    monkeypatch.setattr(engine, "_probe_engine", lambda _: (_ for _ in ()).throw(engine.EngineSetupError("probe failed")))
    with pytest.raises(engine.EngineSetupError, match="probe failed"):
        engine.ensure_engine(tmp_path / "source", runtime, {"libreoffice": spec}, lambda _: None)
    assert cache.is_file()
    assert not (runtime / "lo-26.2.6").exists()
    assert not list(runtime.glob("lo-stage-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows private engine installation")
def test_ensure_success_replaces_only_owned_incomplete_environment(tmp_path, spec, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    previous = runtime / "lo-26.2.6"
    previous.mkdir()
    (previous / engine._OWNER).write_text(json.dumps({"app": engine._APP, "kind": "engine"}), encoding="utf-8")
    (previous / "old.txt").write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(engine, "find_engine", lambda *a: None)
    monkeypatch.setattr(engine, "_download_msi", lambda *a, **k: tmp_path / "cached.msi")
    monkeypatch.setattr(engine, "_extract_msi", lambda _msi, stage, _emit: make_engine(stage / "engine"))
    monkeypatch.setattr(engine, "_probe_engine", lambda _: None)
    result = engine.ensure_engine(tmp_path / "source", runtime, {"libreoffice": spec}, lambda _: None)
    assert result == runtime / "lo-26.2.6"
    assert engine._engine_root(result) == result.resolve()
    marker = json.loads((result / engine._OWNER).read_text(encoding="utf-8"))
    assert marker["version"] == "26.2.6" and marker["sha256"] == spec["sha256"]
    backups = list(runtime.glob("lo-incomplete-*"))
    assert len(backups) == 1
    assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "preserve"
    assert not list(runtime.glob("lo-stage-*"))


def test_probe_uses_no_shell_or_elevation_and_reports_timeout(tmp_path, monkeypatch):
    root = make_engine(tmp_path / "engine")

    def run(command, **kwargs):
        assert "--version" in command
        assert kwargs.get("shell", False) is False
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(engine.subprocess, "run", run)
    with pytest.raises(engine.EngineSetupError, match="启动验证失败"):
        engine._probe_engine(root)
