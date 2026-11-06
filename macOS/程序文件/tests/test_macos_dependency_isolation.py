"""Check metadata/import path rejection without pretending to execute Mac code."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


def verifier():
    source = Path(__file__).resolve().parents[1] / "macos_build/verify_macos.py"
    spec = importlib.util.spec_from_file_location("macos_isolation_verifier", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated(tmp_path):
    runtime = tmp_path / "program/Python"
    site = runtime / "lib/site-packages"
    metadata = site / "example-1.0.dist-info"
    metadata.mkdir(parents=True)
    files = []
    for name in ("first.py", "second.py"):
        path = site / name
        path.write_text("# fictional module", encoding="utf-8")
        files.append(path)
    bootstrap = SimpleNamespace(
        requirements=lambda project: [{"name": "Example", "spec": ">=1,<2", "modules": ["first", "second"]}],
        MODULES={"example": ["first", "second"]})
    distribution = SimpleNamespace(_path=metadata, version="1.0", locate_file=lambda _: site)
    return runtime, site, metadata, files, bootstrap, distribution


def test_all_mapped_modules_and_distribution_metadata_are_recorded(isolated):
    runtime, site, metadata, files, bootstrap, distribution = isolated
    module = verifier()
    with mock.patch.dict("sys.modules", {"bootstrap": bootstrap}), \
            mock.patch.object(module.importlib.metadata, "distribution", return_value=distribution), \
            mock.patch.object(module.importlib, "import_module", side_effect=[SimpleNamespace(__file__=path) for path in files]) as importer:
        rows = module._dependency_isolation(runtime.parent, runtime)
    assert importer.call_args_list == [mock.call("first"), mock.call("second")]
    assert rows[0]["name"] == "Example" and rows[0]["version"] == "1.0"
    assert rows[0]["metadata_directory"] == metadata.relative_to(runtime).as_posix()
    assert rows[0]["distribution_root"] == site.relative_to(runtime).as_posix()
    assert [item["name"] for item in rows[0]["modules"]] == ["first", "second"]


@pytest.mark.parametrize("escape", ["distribution", "metadata", "module"])
def test_external_dependency_locations_are_rejected(isolated, tmp_path, escape):
    runtime, site, metadata, files, bootstrap, distribution = isolated
    external = tmp_path / "external-site"
    external.mkdir()
    outside = external / "module.py"
    outside.write_text("# fictional external module", encoding="utf-8")
    if escape == "distribution":
        distribution.locate_file = lambda _: external
    elif escape == "metadata":
        distribution._path = external
    else:
        files[1] = outside
    module = verifier()
    with mock.patch.dict("sys.modules", {"bootstrap": bootstrap}), \
            mock.patch.object(module.importlib.metadata, "distribution", return_value=distribution), \
            mock.patch.object(module.importlib, "import_module", side_effect=[SimpleNamespace(__file__=path) for path in files]):
        with pytest.raises(AssertionError, match="outside packaged Python"):
            module._dependency_isolation(runtime.parent, runtime)


def test_source_ui_reads_supplied_receipts_without_preparation(tmp_path, monkeypatch):
    module = verifier()
    home, project = tmp_path / "prepared-source-home", tmp_path / "source"
    native = {"ready": True, "components": {}, "env": {"QINGJIAN_SOURCE_UI_TEST": "ready"}}
    monkeypatch.setenv("QINGJIAN_SOURCE_UI_TEST", "not-ready")
    with mock.patch("startup_macos_native.check_native", return_value=native) as check, \
            mock.patch("startup_macos_native.ensure_native", side_effect=AssertionError("no preparation")):
        assert module._source_ui_native(project, home) == native
    check.assert_called_once_with(project, home, architecture=module.platform.machine())
    import os
    assert os.environ["QINGJIAN_SOURCE_UI_TEST"] == "ready"


def test_source_ui_requires_absolute_home_and_complete_receipts(tmp_path):
    module = verifier()
    with pytest.raises(ValueError, match="绝对路径"):
        module._source_ui_native(tmp_path, Path("relative-home"))
    with mock.patch("startup_macos_native.check_native", return_value={"ready": False, "missing": ["OFD"]}):
        with pytest.raises(RuntimeError, match="未就绪.*OFD"):
            module._source_ui_native(tmp_path, tmp_path / "absent-home")


def test_source_ui_cannot_be_combined_with_offscreen_api_preflight(tmp_path):
    module = verifier()
    with mock.patch.object(module, "deny_network", side_effect=AssertionError("validation must occur first")):
        with pytest.raises(ValueError, match="不能同时"):
            module.native_ui(tmp_path, tmp_path / "report.json", source_home=tmp_path, api_preflight=True)
