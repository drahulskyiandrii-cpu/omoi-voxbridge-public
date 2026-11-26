"""Build and install a wheel in a disposable staging directory; no app startup.

By default only build tooling may download packages. Optional --full-runtime
installs Python dependencies in a separate temporary venv and runs mocked smokes.
No local model, audio device or paid API is contacted.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


CHECK = """
import sys
from pathlib import Path
from importlib.metadata import distributions
target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target))
import voxbridge
from voxbridge.app import main, main_standalone
from voxbridge.diagnostics import main as doctor
from voxbridge.mcp_stdio import create_server
assert Path(voxbridge.__file__).resolve().is_relative_to(target)
assert all(callable(x) for x in (main, main_standalone, doctor, create_server))
dist = next(d for d in distributions(path=[str(target)]) if d.metadata["Name"] == "omoi-voxbridge")
assert {e.name: e.value for e in dist.entry_points if e.group == "console_scripts"} == {
    "voxbridge": "voxbridge.app:main", "voxbridge-standalone": "voxbridge.app:main_standalone", "voxbridge-doctor": "voxbridge.diagnostics:main",
    "voxbridge-mcp": "voxbridge.mcp_stdio:main",
}
print("INSTALLED_WHEEL_IMPORT_AND_ENTRYPOINTS_PASS", dist.version)
"""


CLEAN_CHECK = """
import importlib.metadata as metadata
import importlib.util
from pathlib import Path
import sys
import voxbridge
from voxbridge.diagnostics import collect
from voxbridge.mcp_tools import capability_manifest
prefix = Path(sys.prefix).resolve()
assert prefix != Path(sys.base_prefix).resolve()
assert Path(voxbridge.__file__).resolve().is_relative_to(prefix)
dist = metadata.distribution("omoi-voxbridge")
assert {e.name for e in dist.entry_points if e.group == "console_scripts"} == {
    "voxbridge", "voxbridge-standalone", "voxbridge-doctor", "voxbridge-mcp"
}
for package in ("PySide6", "openai", "cartesia", "httpx", "mcp"):
    assert importlib.util.find_spec(package) is None, package
checks = collect()
by_name = {check.name: check for check in checks}
assert by_name["PySide6"].status == "blocked"
assert by_name["OpenAI credential"].status == "not_configured"
assert by_name["Cartesia credential"].status == "not_configured"
assert by_name["settings"].status == "ready"
assert by_name["local models"].status == "untested"
assert not capability_manifest()["can_send_cloud_requests"]
print("CLEAN_VENV_NO_DEPS_DOCTOR_PASS", dist.version)
"""


def clean_venv_check(root, wheel):
    """Fresh environment, no credentials, no runtime dependencies or index."""
    home = root / "clean-home"
    home.mkdir()
    for name in ("appdata", "local", "temp"):
        (home / name).mkdir()
    allow = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allow}
    env.update(USERPROFILE=str(home), HOME=str(home),
               APPDATA=str(home / "appdata"), LOCALAPPDATA=str(home / "local"),
               TEMP=str(home / "temp"), TMP=str(home / "temp"),
               PIP_CONFIG_FILE=os.devnull)
    venv = root / "clean-env"
    subprocess.run([sys.executable, "-I", "-m", "venv", str(venv)],
                   cwd=root, env=env, check=True, timeout=120)
    python = venv / "Scripts" / "python.exe"
    subprocess.run([str(python), "-I", "-m", "pip", "--isolated",
                    "--disable-pip-version-check", "install", "--no-index",
                    "--no-deps", "--no-cache-dir", str(wheel)],
                   cwd=root, env=env, check=True, timeout=60)
    subprocess.run([str(python), "-I", "-c", CLEAN_CHECK],
                   cwd=root, env=env, check=True, timeout=30)
    if any((home / "local").iterdir()) or any((home / "appdata").iterdir()):
        raise RuntimeError("CLEAN_DOCTOR_UNEXPECTED_PROFILE_WRITE")


FULL_CHECK = """
import importlib.metadata as metadata
from pathlib import Path
import sys
import voxbridge
from PySide6 import QtCore, QtGui, QtWidgets, QtMultimedia
import sounddevice, comtypes, openai, cartesia, httpx, soundcard, numpy, websockets, mcp
from voxbridge.diagnostics import collect
assert Path(voxbridge.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
installed = {d.metadata["Name"].lower().replace("_", "-") for d in metadata.distributions()}
assert not installed.intersection({"pyside6", "pyside6-pdf", "pyside6-webengine"})
assert {"pyside6-essentials", "pyside6-addons", "shiboken6"} <= installed
assert not any(c.status == "blocked" for c in collect())
print("MINIMAL_QT_RUNTIME_IMPORTS_AND_DOCTOR_PASS", QtCore.qVersion())
"""


def full_runtime_check(root, wheel, source_root):
    """Reproduce independent installation with all advertised Python extras."""
    home = root / "runtime-home"
    home.mkdir()
    for name in ("appdata", "local", "temp"):
        (home / name).mkdir()
    allow = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH",
             "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allow}
    env.update(USERPROFILE=str(home), HOME=str(home), APPDATA=str(home / "appdata"),
               LOCALAPPDATA=str(home / "local"), TEMP=str(home / "temp"),
               TMP=str(home / "temp"), PIP_CONFIG_FILE=os.devnull,
               QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
    venv = root / "runtime-env"
    subprocess.run([sys.executable, "-I", "-m", "venv", str(venv)],
                   cwd=root, env=env, check=True, timeout=120)
    python = venv / "Scripts" / "python.exe"
    pip = [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check"]
    subprocess.run([*pip, "install", "--no-cache-dir", "--timeout", "20", "--retries", "1",
                    str(wheel) + "[cloud,captions,mcp]"], cwd=root, env=env,
                   check=True, timeout=600)
    subprocess.run([*pip, "check"], cwd=root, env=env, check=True, timeout=30)
    subprocess.run([str(python), "-I", "-c", FULL_CHECK],
                   cwd=root, env=env, check=True, timeout=90)
    for script in ("smoke_app_offscreen.py", "smoke_captions_mock.py"):
        subprocess.run([str(python), "-I", str(source_root / "scripts" / script)],
                       cwd=root, env=env, check=True, timeout=90)
    size = sum(p.stat().st_size for p in venv.rglob("*") if p.is_file())
    print("FULL_RUNTIME_INSTALL_AND_SMOKES_PASS", "venv_bytes", size)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true",
                        help="Do not use a package index even for build tools (requires installed build tools).")
    parser.add_argument("--clean-venv", action="store_true",
                        help="Also verify installation and diagnostics without runtime dependencies in a fresh venv.")
    parser.add_argument("--full-runtime", action="store_true",
                        help="Install all Python extras in a fresh venv and run mocked smokes.")
    parser.add_argument("--source", type=Path, help="Build an explicit clean source snapshot instead of checkout.")
    parser.add_argument("--output", type=Path,
                        help="Retain the verified wheel in this directory (no overwrite).")
    args = parser.parse_args(argv)
    if args.offline and args.full_runtime:
        parser.error("--full-runtime requires a package index; cannot combine with --offline")
    repo = Path(__file__).resolve().parents[1]
    venv = repo / ".venv"
    if Path(sys.prefix).resolve() != venv.resolve():
        parser.error("run using this project's .venv Python")
    input_root = args.source.resolve() if args.source else repo
    with tempfile.TemporaryDirectory(prefix="voxbridge-wheel-") as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        shutil.copy2(input_root / "pyproject.toml", source)
        license_path = input_root / "LICENSE"
        if not license_path.is_file():
            license_path = input_root / "distribution" / "LICENSE"
        if license_path.is_file():
            shutil.copy2(license_path, source / "LICENSE")
        elif args.source:
            raise RuntimeError("SOURCE_LICENSE_MISSING")
        shutil.copytree(input_root / "src", source / "src",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        wheel_dir = root / "wheel"
        wheel_dir.mkdir()
        pip = [sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check"]
        command = [*pip, "wheel", "--no-deps", "--wheel-dir", str(wheel_dir),
                   "--timeout", "15", "--retries", "1"]
        if args.offline:
            command += ["--no-index", "--no-build-isolation"]
        subprocess.run([*command, str(source)], check=True, timeout=180)
        wheels = list(wheel_dir.glob("omoi_voxbridge-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("WHEEL_EXPECTED_SINGLE_ARTIFACT")
        with zipfile.ZipFile(wheels[0]) as archive:
            names = archive.namelist()
            if license_path.is_file():
                license_names = [n for n in names if n.endswith("/LICENSE")]
                if len(license_names) != 1 or archive.read(license_names[0]) != license_path.read_bytes():
                    raise RuntimeError("WHEEL_LICENSE_MISSING_OR_DIFFERENT")
            assert "voxbridge/app.py" in names
            assert "voxbridge/openvino_worker.mjs" in names
            assert "voxbridge/windows_ocr.ps1" in names
            assert "voxbridge/diagnostics.py" in names
            assert "voxbridge/windows_network.py" in names
            assert not any(n.startswith("benchmarks/") or n in {
                "voxbridge/" + module + ".py" for module in (
                    "cpu_caption_runtime", "qualified_cpu_translation", "mt_literals", "mt_quality_lab")
            } for n in names)
            assert not any(n.endswith((".wav", ".pcm", ".env", ".safetensors", ".gguf", ".pt"))
                           for n in names)
            assert not any("tests/" in n or "__pycache__" in n for n in names)
        target = root / "installed"
        subprocess.run([*pip, "install", "--no-deps", "--no-index", "--target", str(target),
                        str(wheels[0])], check=True, timeout=60)
        subprocess.run([sys.executable, "-I", "-c", CHECK, str(target)],
                       cwd=root, check=True, timeout=30)
        if args.clean_venv:
            clean_venv_check(root, wheels[0])
        print("WHEEL_BUILD_INSTALL_CONTRACT_PASS; NO_REAL_DEVICES_OR_INFERENCE")
        if args.full_runtime:
            full_runtime_check(root, wheels[0], input_root)
        if args.output:
            destination = args.output.resolve()
            destination.mkdir(parents=True, exist_ok=True)
            path = destination / wheels[0].name
            with path.open("xb") as handle:
                handle.write(wheels[0].read_bytes())
            print("VERIFIED_WHEEL", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
