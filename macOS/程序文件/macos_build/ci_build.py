"""Prepare, build, accept, then archive a Mac release on a real runner.

This is deliberately not a publishing script. It never pushes Git commits,
changes repository visibility, creates a Release, or starts another workflow.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]


def execute(arguments: list[str], *, cwd: Path = PROJECT, env=None) -> None:
    print("执行：" + " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=cwd, check=True, env=env)


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
    for name in ("test_macos_bootstrap", "test_macos_native", "test_macos_core", "test_macos_packaging",
                 "test_macos_dependency_isolation"):
        environment = os.environ.copy()
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        subprocess.run([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/" + name + ".py",
                        "--basetemp", str(output / "tests-temp" / name),
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
    execute([str(python), "-c", "import sys; from startup_macos_native import ensure_redistribution_materials; "
             "ensure_redistribution_materials(sys.argv[1], sys.argv[2], emit=print)", str(PROJECT), str(home)])
    execute([str(python), str(PROJECT / "build_macos.py"), "--output", str(output / "build"),
             "--runtime", str(python.parents[1]), "--native-home", str(home), "--architecture", arch])
    focused_regression(python, home, output)
    sys.path.insert(0, str(PROJECT))
    from package_macos import package_full, version_of
    version = version_of(PROJECT)
    bundle = output / "build" / f"青简工具箱_v{version}_macOS_{arch}"
    packaged_python = bundle / "程序文件/Python/bin/python3"
    verification = output / "acceptance" / arch
    execute([str(packaged_python), "-I", "-B", str(bundle / "程序文件/macos_build/verify_macos.py"),
             "--bundle", str(bundle), "--output", str(verification)])
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
