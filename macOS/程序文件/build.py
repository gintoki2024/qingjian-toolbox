"""Build a portable Windows folder; only the build machine needs Python/network."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def verify_source_startup(project: Path, *, require_exe=True):
    """Fail future releases which accidentally omit source bootstrapping."""
    required = ("main.py", "bootstrap.py", "startup_engine.py", "startup_shared.py", "runtime-sources.json", "startup_native.py", "native-sources.json", "bootstrap-wheels.json", "AGENTS.md", "package_source.py",
                "启动青简工具箱.exe", "launcher/SourceLauncher.cs", "launcher/PortableLauncher.cs", "launcher/portable/启动青简工具箱.exe",
                "launcher/build_launcher.py", "launcher/build-info.json")
    required = tuple(name for name in required if require_exe or not name.lower().endswith('.exe'))
    if not require_exe:
        required += ('launcher/start_source.cmd', 'launcher/prepare_python.cmd', 'launcher/cmd_source.py',
                     'launcher/start_offline.cmd', 'launcher/offline_cmd.py')
    missing = [name for name in required if not (project / name).is_file()]
    if missing:
        raise RuntimeError("源码启动自检文件缺失，不能发布：" + "、".join(missing))
    source = ast.parse((project / "main.py").read_text(encoding="utf-8-sig"))
    run = next((node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "run"), None)
    imports = sorted((node.lineno, node.module) for node in ast.walk(run) if isinstance(node, ast.ImportFrom)) if run else []
    bootstrap_line = next((line for line, module in imports if module == "bootstrap"), None)
    ui_line = next((line for line, module in imports if module == "qingjian.app"), None)
    if bootstrap_line is None or ui_line is None or bootstrap_line >= ui_line:
        raise RuntimeError("源码必须先自检环境，再导入图形界面。")
    bootstrap = ast.parse((project / "bootstrap.py").read_text(encoding="utf-8-sig"))
    install_policy = next((node.value.value for node in bootstrap.body
                           if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                           and any(isinstance(target, ast.Name) and target.id == "DEPENDENCY_INSTALL_POLICY"
                                   for target in node.targets)), None)
    if install_policy != "shared-interpreter":
        raise RuntimeError("源码必须保留向所选Python共享环境补齐依赖的安装策略。")
    from bootstrap import verified_bootstrap_wheels
    verified_bootstrap_wheels(project)
    from startup_native import load_manifest
    native = load_manifest(project)
    native_calls = {node.func.id for node in ast.walk(bootstrap)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    if not {'check_native', 'ensure_native'} <= native_calls:
        raise RuntimeError('轻量源码必须集成全部原生组件的自检、自动补齐与路径发现。')
    bridge = next(row for row in native['components']['OFD']['files'] if row['file'] == 'bridge.jar')
    if hashlib.sha256((project / 'support/OFD/bridge.jar').read_bytes()).hexdigest() != bridge['sha256']:
        raise RuntimeError('轻量源码中的 OFD 桥文件未通过固定哈希校验。')
    record = json.loads((project / "launcher" / "build-info.json").read_text(encoding="utf-8"))
    for name, key in (("启动青简工具箱.exe", "launcher_sha256"), ("launcher/SourceLauncher.cs", "source_sha256"),
                      ("launcher/portable/启动青简工具箱.exe", "portable_launcher_sha256"),
                      ("launcher/PortableLauncher.cs", "portable_source_sha256"), ("launcher/build_launcher.py", "builder_sha256")):
        if not require_exe and name.lower().endswith('.exe'):
            continue
        if hashlib.sha256((project / name).read_bytes()).hexdigest() != record.get(key):
            raise RuntimeError("源码启动器与构建记录不一致，请运行 launcher/build_launcher.py 重新构建。")


def export_licenses(project: Path, bundle: Path):
    licenses = bundle / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copy2(project / "第三方组件说明.txt", licenses / "第三方组件说明.txt")
    for name in ("LICENSE.txt", "来源.json"):
        source = project / "lunar_python" / name
        if source.is_file():
            target = licenses / "lunar_python" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(source, target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    if (project / "排版引擎来源.json").is_file():
        shutil.copy2(project / "排版引擎来源.json", licenses / "排版引擎来源.json")
    inventory = []
    for dist in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].casefold()):
        name = dist.metadata["Name"]
        if name.lower() in {"pip", "pytest", "pyinstaller", "altgraph", "pefile", "pluggy", "iniconfig", "pygments"}:
            continue
        inventory.append({"name": name, "version": dist.version,
                          "home": dist.metadata.get("Home-page", ""),
                          "project_urls": dist.metadata.get_all("Project-URL", [])})
        for file in dist.files or []:
            text = str(file).lower().replace("\\", "/")
            if (".dist-info/" in text and any(x in Path(text).name for x in ("license", "licence", "copying", "copyright", "notice"))) or "/licenses/" in text or (name.lower() == "reportlab" and any(x in Path(text).name for x in ("license", "copying", "copyright"))):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    target = licenses / name / Path(str(file).replace("..", "_"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    calendar_origin = project / "lunar_python" / "来源.json"
    if calendar_origin.is_file():
        calendar = json.loads(calendar_origin.read_text(encoding="utf-8"))
        inventory.append({"name": calendar["name"], "version": calendar["version"], "home": calendar["upstream"],
                          "license": calendar["license"], "vendored_unmodified": True, "source_sha256": calendar["sha256"]})
    (licenses / "组件版本.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, required=True, help="LibreOffice root containing program/soffice.exe")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--work", type=Path, default=Path("build"))
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("请在 Windows 64 位上打包 Windows 版。")
    if not (args.engine / "program" / "soffice.exe").is_file():
        raise SystemExit("--engine 目录应包含 program/soffice.exe。")
    project = Path(__file__).resolve().parent
    verify_source_startup(project)
    out = args.output.resolve()
    work = args.work.resolve()
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    bundle = out / "青简工具箱"
    if bundle.exists():
        raise FileExistsError("目标目录已存在，请使用新的输出目录，避免覆盖已发布版本：" + str(bundle))
    private_dist = work / "onedir"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
               "--name", "青简工具箱", "--distpath", str(private_dist), "--workpath", str(work / "pyinstaller"),
               "--specpath", str(work), "--paths", str(project),
               "--collect-data", "docx", "--collect-all", "pypdfium2_raw",
               "--collect-data", "certifi", "--exclude-module", "tkinter",
               "--collect-data", "pptx", "--collect-all", "pillow_heif",
               "--collect-submodules", "extract_msg", "--collect-data", "tzdata",
               "--collect-data", "icalendar", "--hidden-import", "PySide6.QtSvg",
               "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.QtQml",
               str(project / "main.py")]
    # Build hosts may expose Poppler/LibreOffice DLLs on PATH. Their versioned
    # ICU DLL has the same filename as Windows' unversioned ICU used by Qt.
    # A minimal build PATH prevents accidentally bundling those unrelated DLLs.
    build_env = os.environ.copy()
    windows = Path(os.environ.get("WINDIR", "C:/Windows"))
    build_env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), sys.base_prefix,
                                        str(windows / "System32"), str(windows)])
    for name in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        build_env.pop(name, None)
    subprocess.run(command, cwd=project, env=build_env, check=True)
    bundle.mkdir()
    program = bundle / "程序文件"
    built = private_dist / "青简工具箱"
    if not built.resolve().is_relative_to(work) or not program.resolve().is_relative_to(out):
        raise RuntimeError("构建目录越出目标工作区。")
    shutil.move(str(built), str(program))
    shutil.copytree(args.engine, program / "engines" / "LibreOffice", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.msi"))
    # The MSI's administrative extraction leaves VC++ redistributables in
    # System64. Use app-local deployment so a clean PC needs no runtime install.
    crt = args.engine / "System64"
    if crt.is_dir():
        for dll in crt.glob("*.dll"):
            shutil.copy2(dll, program / "engines" / "LibreOffice" / "program" / dll.name)
    shutil.copy2(project / "使用说明.txt", bundle / "使用说明.txt")
    shutil.copy2(project / "launcher/portable/启动青简工具箱.exe", bundle / "启动青简工具箱.exe")
    if not (project / "support").is_dir():
        raise RuntimeError("缺少所选格式的离线 support 组件，不能发布完整包。")
    shutil.copytree(project / "support", program / "support")
    if (project / "示例材料").is_dir():
        shutil.copytree(project / "示例材料", program / "示例材料", dirs_exist_ok=True)
    for name in ("LICENSE.txt", "更新说明.txt", "第三方组件说明.txt", "转换格式说明.txt", "原生组件准备说明.txt", "启动诊断.cmd"):
        if (project / name).is_file():
            shutil.copy2(project / name, program / name)
    export_licenses(project, program)
    if {path.name for path in bundle.iterdir()} != {"启动青简工具箱.exe", "使用说明.txt", "程序文件"}:
        raise RuntimeError("完整包根目录必须保持三个项目。")
    print("Portable bundle:", bundle)


if __name__ == "__main__":
    main()
