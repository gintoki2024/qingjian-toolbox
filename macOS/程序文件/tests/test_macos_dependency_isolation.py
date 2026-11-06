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
