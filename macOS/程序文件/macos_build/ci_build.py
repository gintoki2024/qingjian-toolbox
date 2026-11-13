"""Prepare, build, accept, then archive a Mac release on a real runner.

This is deliberately not a publishing script. It never pushes Git commits,
changes repository visibility, creates a Release, or starts another workflow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]

# Run each Qt module in its own process, including the new conversion routes,
# preflight/worker interaction and six-target pagination on both Mac arches.
REGRESSION_MODULES = (
    "test_macos_bootstrap", "test_macos_native", "test_macos_core", "test_macos_packaging",
    "test_macos_dependency_isolation", "test_macos_ci_launcher",
    "test_converter_office_catalog", "test_office_conversion_local",
    "test_converter_dialogs_local", "test_converter_office_integration",
    "test_converter_float_ui", "test_drag_wheel", "test_quick_ui",
    "test_conversion_hub_v090", "test_format_office", "test_quick_actions",
    "test_saved_web_quick", "test_quick_compact",
)


def source_payload_acceptance(project: Path, bundle: Path) -> dict:
    """Reject stale Python code or startup manifests in the offline bundle."""
    from package_macos import source_files
    program = bundle / "程序文件"
    startup_names = {
        "requirements.txt", "bootstrap-wheels.json", "macos-runtime-sources.json",
        "macos-native-sources.json", "runtime-sources.json", "native-sources.json",
    }
    checked = []
    for source in source_files(project):
        relative = source.relative_to(project)
        if source.suffix != ".py" and not (len(relative.parts) == 1 and source.name in startup_names):
            continue
        target = program / relative
        if not target.is_file() or source.read_bytes() != target.read_bytes():
            raise RuntimeError("完整包与本轮源码不一致：" + relative.as_posix())
        checked.append({"file": relative.as_posix(), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    return {"passed": True, "python_and_startup_files": len(checked), "files": checked}


def execute(arguments: list[str], *, cwd: Path = PROJECT, env=None) -> None:
    print("执行：" + " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=cwd, check=True, env=env)


def source_launcher_acceptance(home: Path, output: Path, environment: dict) -> None:
    """Exercise the real source launcher offline and confirm its GUI detaches."""
    destination = output / "acceptance" / platform.machine() / "source-ui"
    destination.mkdir(parents=True, exist_ok=True)
    environment = {**environment, "QINGJIAN_USER_DATA": str(destination / "fictional-user")}
    command = ["/bin/bash", str(PROJECT / "launcher/start_macos.command"),
               "--home", str(home), "--managed-python", "--offline"]
    done = subprocess.run(command, cwd=PROJECT, env=environment, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    (destination / "source-launcher.stdout.txt").write_text(done.stdout, encoding="utf-8")
    (destination / "source-launcher.stderr.txt").write_text(done.stderr, encoding="utf-8")
    if done.returncode:
        raise RuntimeError("源码启动器离线启动失败：" + done.stdout[-2000:] + done.stderr[-2000:])
    events = []
    for line in done.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("event") == "launched":
            events.append(item)
    if len(events) != 1 or type(events[0].get("pid")) is not int:
        raise RuntimeError("源码启动器未返回唯一的界面就绪记录。")
    pid = events[0]["pid"]
    if pid <= 1 or os.getpgid(pid) != pid:
        raise RuntimeError("源码界面未与准备终端分离。")
    # Stop only the GUI created and confirmed by this acceptance invocation.
    os.killpg(pid, signal.SIGTERM)
    (destination / "源码启动器.json").write_text(json.dumps({
        "passed": True, "platform": sys.platform, "architecture": platform.machine(),
        "offline": True, "system_only_path": True, "detached_gui": True,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def focused_regression(python: Path, home: Path, output: Path) -> None:
    """Install test tooling only after the complete runtime has been copied."""
    manifest = json.loads((PROJECT / "macos-runtime-sources.json").read_text(encoding="utf-8-sig"))
    failures = []
    for source in manifest["pip_indexes"]:
        try:
            execute([str(python), "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--no-input",
                     "--timeout", "20", "--retries", "1", "--index-url", source["url"],
                     "--cache-dir", str(home / "pip-cache"), "pytest>=8,<10"])
            break
        except subprocess.CalledProcessError as exc:
            failures.append(source["name"] + ": " + str(exc.returncode))
    else:
        raise RuntimeError("Mac 验收依赖安装未完成：" + "；".join(failures))
    destination = output / "acceptance" / platform.machine()
    destination.mkdir(parents=True, exist_ok=True)
    test_temp = (output / "tests-temp").resolve()
    test_temp.mkdir(parents=True, exist_ok=True)
    # Discover only the already verified source environment in each child.
    # This never installs engines, and lets content-level office tests exercise
    # the same private LibreOffice/7zip/GS as source acceptance instead of
    # silently skipping when no system engine is present.
    runner = (
        "import os,sys; from pathlib import Path; "
        "from startup_macos_native import check_native; "
        "native=check_native(Path(sys.argv.pop(1)),Path(sys.argv.pop(1))); "
        "assert native.get('ready'), 'Mac regression requires prepared native engines'; "
        "os.environ.update(native['env']); "
        "os.environ['QINGJIAN_OFFICE_TEST_ENGINE']=native['env']['QINGJIAN_LIBREOFFICE']; "
        "import pytest; "
        "raise SystemExit(pytest.main(sys.argv[1:]))"
    )
    for name in REGRESSION_MODULES:
        environment = os.environ.copy()
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        # This applies only to the isolated test child. Keep real file fixtures
        # under the runner's canonical work directory, rather than macOS's
        # /var alias, without weakening the application's source protections.
        environment.update({key: str(test_temp) for key in ("TMPDIR", "TEMP", "TMP", "QINGJIAN_TEST_TMPDIR")})
        subprocess.run([str(python), "-c", runner, str(PROJECT), str(home), "-q", "-p", "no:cacheprovider", "tests/" + name + ".py",
                        "--basetemp", str(test_temp / name),
                        "--junitxml", str(destination / (name + ".xml"))], cwd=PROJECT, env=environment, check=True)


def main(home: Path, output: Path) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("CI 完整包只能在真实 macOS 构建环境执行。")
    home, output = home.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    arch = platform.machine()
    if arch not in {"arm64", "x86_64"}:
        raise RuntimeError("不支持的 Mac 架构：" + arch)
    first_environment = os.environ.copy()
    first_environment.update({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                              "QINGJIAN_MAC_SELECTION": str(output / "first-selection.json")})
    execute(["/bin/bash", str(PROJECT / "launcher/start_macos.command"), "--home", str(home),
             "--managed-python", "--prepare-only", "--no-ui"], env=first_environment)
    # Repeat the same preparation using its public Python entry point to check
    # receipt validation and component reuse, before an explicitly offline pass.
    execute([sys.executable, str(PROJECT / "bootstrap_macos.py"), "--prepare-environment", "--home", str(home),
             "--managed-python", "--no-ui"])
    python = home / "python" / arch / "bin/python3"
    if not python.is_file():
        raise RuntimeError("环境准备没有生成约定的可移动 Python。")
    execute([str(python), str(PROJECT / "bootstrap_macos.py"), "--check-environment", "--home", str(home),
             "--python", str(python), "--offline", "--no-ui"])
    # Imports alone cannot detect incompatible transitive package metadata.
    execute([str(python), "-I", "-m", "pip", "--isolated", "check"])
    source_launcher_acceptance(home, output, first_environment)
    source_ui = output / "acceptance" / arch / "source-ui"
    source_ui.mkdir(parents=True, exist_ok=True)
    execute([str(python), "-I", "-B", str(PROJECT / "macos_build/verify_macos.py"),
             "--case", "ui", "--project", str(PROJECT), "--source-home", str(home),
             "--report", str(source_ui / "源码原生界面.json")])
    execute([str(python), "-c", "import sys; from startup_macos_native import ensure_redistribution_materials; "
             "ensure_redistribution_materials(sys.argv[1], sys.argv[2], emit=print)", str(PROJECT), str(home)])
    execute([str(python), str(PROJECT / "build_macos.py"), "--output", str(output / "build"),
             "--runtime", str(python.parents[1]), "--native-home", str(home), "--architecture", arch])
    sys.path.insert(0, str(PROJECT))
    from package_macos import package_full, version_of
    version = version_of(PROJECT)
    bundle = output / "build" / f"青简工具箱_v{version}_macOS_{arch}"
    packaged_python = bundle / "程序文件/Python/bin/python3"
    verification = output / "acceptance" / arch
    execute([str(packaged_python), "-I", "-B", str(bundle / "程序文件/macos_build/verify_macos.py"),
             "--bundle", str(bundle), "--output", str(verification)])
    # Keep real application acceptance independent of test-tool setup. Both
    # must pass before any distributable complete ZIP is produced.
    focused_regression(python, home, output)
    integrity = source_payload_acceptance(PROJECT, bundle)
    (verification / "源码与完整包一致性.json").write_text(
        json.dumps(integrity, ensure_ascii=False, indent=2), encoding="utf-8")
    # Validation must precede publication of a complete archive.
    release = output / "release"
    release.mkdir(exist_ok=True)
    archive = release / f"QingjianToolbox-v{version}-macOS-{arch}-Full.zip"
    record = package_full(bundle, archive)
    (release / (archive.name + ".sha256")).write_text(record["sha256"] + "  " + archive.name + "\n", encoding="utf-8")
    (release / (archive.stem + "-inventory.json")).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.home, args.output)
