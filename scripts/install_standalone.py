"""Install a reviewed wheel into a new per-user Windows directory.

Python 3.11 x64 prerequisite. No elevation, autostart, model download or
existing environment modification. Offline wheelhouse supported.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

WHEEL_NAME = "omoi_voxbridge-0.1.0-py3-none-any.whl"


def safe_directory(path: Path) -> Path:
    if not path.is_absolute() or path.anchor.startswith("\\\\"):
        raise ValueError("Absolute local path required")
    for part in (path, *path.parents):
        if part.exists() and (part.is_symlink() or
                part.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("Redirected path refused")
    return path


def run(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel",required=True,type=Path)
    parser.add_argument("--sha256",required=True)
    parser.add_argument("--wheelhouse",type=Path,help="Offline dependency directory")
    parser.add_argument("--target",type=Path,
                        default=Path(os.environ.get("LOCALAPPDATA",str(Path.home())))/
                        "Programs"/"OMOI-VoxBridge")
    parser.add_argument("--plan",action="store_true")
    a=parser.parse_args(argv)
    if os.name!="nt" or sys.version_info[:2]!=(3,11) or sys.maxsize<=2**32:
        parser.error("Windows x64 Python 3.11 required")
    target=safe_directory(a.target)
    wheel=safe_directory(a.wheel)
    if wheel.name!=WHEEL_NAME or not wheel.is_file() or hashlib.sha256(wheel.read_bytes()).hexdigest()!=a.sha256.lower():
        parser.error("Wheel filename or SHA256 mismatch")
    if a.wheelhouse is not None and not safe_directory(a.wheelhouse).is_dir():
        parser.error("Wheelhouse missing")
    if target.exists():
        parser.error("Existing destination not overwritten; choose a new directory")
    print("INSTALL_PLAN",target,"offline" if a.wheelhouse else "PyPI",flush=True)
    if a.plan:return 0
    env={k:v for k,v in os.environ.items() if k.upper() in {
        "SYSTEMROOT","WINDIR","SYSTEMDRIVE","COMSPEC","PATH","TEMP","TMP",
        "USERPROFILE","HOME","APPDATA","LOCALAPPDATA","PROCESSOR_ARCHITECTURE","NUMBER_OF_PROCESSORS"}}
    env.update(PIP_CONFIG_FILE=os.devnull,CUDA_VISIBLE_DEVICES="")
    target.mkdir(parents=True,exist_ok=False)
    try:
        venv=target/"venv"
        subprocess.run([sys.executable,"-I","-m","venv",str(venv)],
                       cwd=target,env=env,check=True,timeout=120)
        python=venv/"Scripts/python.exe"
        pip=[str(python),"-I","-m","pip","--isolated","--disable-pip-version-check"]
        index=["--no-index","--find-links",str(a.wheelhouse)] if a.wheelhouse else ["--index-url","https://pypi.org/simple"]
        subprocess.run([*pip,"install","--only-binary=:all:","--no-cache-dir","--timeout","20","--retries","0",
                        *index,str(wheel)+"[cloud,captions,mcp]"],
                       cwd=target,env=env,check=True,timeout=600)
        subprocess.run([*pip,"check"],cwd=target,env=env,check=True,timeout=30)
        # Diagnose with an empty disposable profile, never the user's existing settings.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="vb-install-check-") as tmp:
            check_env=dict(env,LOCALAPPDATA=tmp,APPDATA=tmp,USERPROFILE=tmp,HOME=tmp)
            subprocess.run([str(python),"-I","-m","voxbridge.diagnostics"],cwd=target,
                           env=check_env,check=True,timeout=60)
        (target/"Start-VoxBridge.cmd").write_text(
            '@echo off\r\nsetlocal\r\nset "VOXBRIDGE_PROFILE_ROOT=%LOCALAPPDATA%\\OMOI-VoxBridge-Standalone"\r\nstart "" "%~dp0venv\\Scripts\\pythonw.exe" -I -c "from voxbridge.app import main_standalone; raise SystemExit(main_standalone())"\r\n',
            encoding="ascii",newline="")
        (target/"Doctor.cmd").write_text(
            '@echo off\r\nsetlocal\r\nset "VOXBRIDGE_PROFILE_ROOT=%LOCALAPPDATA%\\OMOI-VoxBridge-Standalone"\r\n"%~dp0venv\\Scripts\\python.exe" -I -m voxbridge.diagnostics\r\n',
            encoding="ascii",newline="")
        print("INSTALL_PASS",target,flush=True)
        return 0
    except BaseException:
        # Only our freshly created install directory, never a previous installation.
        shutil.rmtree(target)
        raise


if __name__=="__main__":
    raise SystemExit(run())
