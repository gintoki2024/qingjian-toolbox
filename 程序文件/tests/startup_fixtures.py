"""Small real interpreters for Windows startup tests, without pip or downloads."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import venv


def real_venv(folder: Path, *, console_only=False) -> Path:
    """Use Windows' actual venv redirector, rather than copying base python.exe.

    A base executable copied away from its Python DLL cannot run merely because
    pyvenv.cfg was written next to it. EnvBuilder selects the proper redirector;
    this fixture also probes SSL and executable identity before testing CMD.
    """
    folder = Path(folder).resolve()
    venv.EnvBuilder(with_pip=False, symlinks=False).create(folder)
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if console_only:
        pythonw = python.with_name("pythonw.exe")
        if pythonw.is_file():
            pythonw.unlink()
        assert not pythonw.exists()
    code = (
        "import json,ssl,struct,sys; "
        "print(json.dumps(dict(python=sys.executable,prefix=sys.prefix,"
        "version=list(sys.version_info[:3]),bits=struct.calcsize('P')*8)))"
    )
    checked = subprocess.run([str(python), "-E", "-X", "utf8", "-c", code],
                             capture_output=True, text=True, encoding="utf-8", timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    probe = json.loads(checked.stdout)
    assert Path(probe["python"]).samefile(python), probe
    assert Path(probe["prefix"]).samefile(folder), probe
    assert probe["version"] == list(sys.version_info[:3]), probe
    assert probe["bits"] == 64, probe
    return python
