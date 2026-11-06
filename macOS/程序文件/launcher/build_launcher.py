"""Rebuild the source starter using Windows' installed .NET compiler."""
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
MANIFEST = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
 <assemblyIdentity version="1.0.0.0" name="Qingjian.SourceLauncher" />
 <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
  <requestedExecutionLevel level="asInvoker" uiAccess="false" />
 </requestedPrivileges></security></trustInfo>
 <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application>
  <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}" />
 </application></compatibility>
</assembly>'''


def build(project=PROJECT):
    project = Path(project).resolve()
    source = project / "launcher" / "SourceLauncher.cs"
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise RuntimeError("需要Windows自带的.NET Framework C#编译器。")
    target = project / "启动青简工具箱.exe"
    with tempfile.TemporaryDirectory(prefix="qingjian-launcher-") as temporary:
        work = Path(temporary)
        manifest = work / "app.manifest"
        manifest.write_text(MANIFEST, encoding="utf-8")
        staged = work / "launcher.exe"
        command = [str(compiler), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+", "/utf8output",
                   "/codepage:65001", "/win32manifest:" + str(manifest), "/out:" + str(staged)]
        for name in ("System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll",
                     "System.Web.Extensions.dll", "System.IO.Compression.dll", "System.IO.Compression.FileSystem.dll"):
            command.append("/reference:" + name)
        command.append(str(source))
        subprocess.run(command, check=True, timeout=120)
        target.write_bytes(staged.read_bytes())
    record = {"launcher_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "bytes": target.stat().st_size, "administrator_required": False}
    portable_source = project / "launcher" / "PortableLauncher.cs"
    portable_target = project / "launcher" / "portable" / "启动青简工具箱.exe"
    portable_target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qingjian-portable-starter-") as temporary:
        work = Path(temporary)
        manifest = work / "app.manifest"
        manifest.write_text(MANIFEST.replace("Qingjian.SourceLauncher", "Qingjian.PortableLauncher"), encoding="utf-8")
        staged = work / "portable.exe"
        subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+", "/utf8output",
                        "/codepage:65001", "/win32manifest:" + str(manifest), "/out:" + str(staged),
                        "/reference:System.dll", "/reference:System.Core.dll", "/reference:System.Windows.Forms.dll",
                        str(portable_source)], check=True, timeout=120)
        portable_target.write_bytes(staged.read_bytes())
    record.update(portable_launcher_sha256=hashlib.sha256(portable_target.read_bytes()).hexdigest(),
                  portable_source_sha256=hashlib.sha256(portable_source.read_bytes()).hexdigest(),
                  portable_bytes=portable_target.stat().st_size,
                  builder_sha256=hashlib.sha256((project / "launcher" / "build_launcher.py").read_bytes()).hexdigest())
    (project / "launcher" / "build-info.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
