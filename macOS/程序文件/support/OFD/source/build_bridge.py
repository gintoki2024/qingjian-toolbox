"""Rebuild the offline OFD bridge and a minimal Java runtime from a local JDK.

No system Java installation, PATH change or GPU is needed. Maven's repository
and all build files stay in the explicitly provided work directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
PROJECT = BRIDGE.parent.parent
MODULES = "java.base,java.desktop,java.logging,java.xml,java.naming,java.management,jdk.crypto.ec,jdk.unsupported,jdk.charsets"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(jdk, work, maven=None, skip_maven=False, output=None):
    jdk, work = Path(jdk).resolve(), Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    repository = work / "m2"
    build_output = BRIDGE / "target"
    env = os.environ.copy()
    env["JAVA_HOME"] = str(jdk)
    windows = Path(os.environ.get("WINDIR", "C:/Windows"))
    env["PATH"] = os.pathsep.join([str(jdk / "bin"), str(windows / "System32"), str(windows)])
    env["MAVEN_OPTS"] = "-Dfile.encoding=UTF-8"
    temporary = work / "temp"
    temporary.mkdir(exist_ok=True)
    env["TEMP"] = env["TMP"] = str(temporary)
    if not skip_maven:
        if not maven:
            raise ValueError("请用 --maven 指定本地 Maven 文件夹。")
        subprocess.run([str(Path(maven).resolve() / "bin" / "mvn.cmd"), "-B", "-ntp", "-DskipTests",
                        "-Dmaven.repo.local=" + str(repository), "-f", str(BRIDGE / "pom.xml"), "package"],
                       env=env, check=True, timeout=1200)
    jar = build_output / "qingjian-ofd-bridge-0.9.0.jar"
    if not jar.is_file():
        raise FileNotFoundError("请先构建 Java 桥。")
    stage = work / ("ofd-runtime-" + uuid.uuid4().hex[:10])
    stage.mkdir()
    subprocess.run([str(jdk / "bin" / "jlink.exe"), "--add-modules", MODULES, "--strip-debug", "--compress=zip-6",
                    "--no-header-files", "--no-man-pages", "--output", str(stage / "runtime")],
                   env=env, check=True, timeout=180)
    shutil.copy2(jdk / "NOTICE", stage / "runtime" / "NOTICE")
    shutil.copy2(jar, stage / "bridge.jar")
    shutil.copytree(build_output / "lib", stage / "lib")
    licenses = stage / "licenses"
    licenses.mkdir()
    origins = []
    for library in sorted((stage / "lib").glob("*.jar")):
        digest = sha(library)
        candidates = list(repository.rglob(library.name))
        match = next((path for path in candidates if sha(path) == digest), None)
        if match is None:
            raise RuntimeError("找不到依赖来源：" + library.name)
        relative = match.relative_to(repository).as_posix()
        pom = match.with_suffix(".pom")
        notice_folder = licenses / library.stem
        notice_folder.mkdir()
        if pom.is_file():
            shutil.copy2(pom, notice_folder / pom.name)
        with zipfile.ZipFile(library) as archive:
            for entry in archive.infolist():
                basename = Path(entry.filename).name
                if ("license" in basename.lower() or basename.lower().startswith(("notice", "copying"))) and not entry.is_dir():
                    if entry.file_size > 4 * 1024 * 1024:
                        raise RuntimeError("依赖许可证异常过大：" + library.name)
                    target = notice_folder / basename
                    if target.exists():
                        target = notice_folder / (str(len(list(notice_folder.iterdir()))) + "-" + basename)
                    target.write_bytes(archive.read(entry))
        origins.append({"file": library.name, "sha256": digest, "bytes": library.stat().st_size,
                        "url": "https://repo.maven.apache.org/maven2/" + relative,
                        "source_url": "https://repo.maven.apache.org/maven2/" + relative[:-4] + "-sources.jar"})
    (stage / "library-origins.json").write_text(json.dumps(origins, ensure_ascii=False, indent=2), encoding="utf-8")
    destination = Path(output).resolve() if output else PROJECT / "support" / "OFD"
    if destination.exists():
        raise FileExistsError("已有 OFD 支持目录，构建结果保留于：" + str(stage))
    destination.parent.mkdir(exist_ok=True)
    shutil.copytree(stage, destination)
    record = {"java": subprocess.check_output([str(destination / "runtime/bin/java.exe"), "-version"], stderr=subprocess.STDOUT, text=True),
              "modules": MODULES.split(","), "bridge_source_sha256": sha(BRIDGE / "src/main/java/local/qingjian/OFDBridge.java"),
              "bridge_sha256": sha(destination / "bridge.jar"), "libraries": len(origins), "offline": True}
    (destination / "build-info.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jdk", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--maven", type=Path)
    parser.add_argument("--skip-maven", action="store_true")
    parser.add_argument("--output", type=Path, help="可选的新 OFD 支持目录；不会覆盖已有目录")
    args = parser.parse_args()
    build(args.jdk, args.work, args.maven, args.skip_maven, args.output)
