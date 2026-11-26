"""Stage an offline Windows Sandbox acceptance test from reviewed local files.

No downloads, no automatic launch, no credentials or project directory mapping.
Verify the Python Authenticode or PSF NuGet signature separately before staging.
"""
import argparse
import hashlib
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

SCRIPTS=("install_standalone.py","sandbox_acceptance.ps1","smoke_app_offscreen.py",
         "smoke_captions_mock.py","check_hotkey_lifecycle.py","demo_captions.py")
PYTHON_INSTALLER="python-3.11.9-amd64.exe"  # historical qualification baseline
PYTHON_NUGET="python.3.11.9.nupkg"
WHEEL="omoi_voxbridge-0.1.0-py3-none-any.whl"


def digest(p):
    with p.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    runtime=p.add_mutually_exclusive_group(required=True)
    runtime.add_argument("--python-installer",type=Path)
    runtime.add_argument("--python-nupkg",type=Path,help="Previously signature-verified PSF NuGet x64 package")
    p.add_argument("--python-sha256",required=True)
    p.add_argument("--wheelhouse",required=True,type=Path)
    p.add_argument("--wheel-sha256",required=True)
    p.add_argument("--target",required=True,type=Path)
    a=p.parse_args()
    wheel=a.wheelhouse/WHEEL
    python_asset=a.python_nupkg or a.python_installer
    python_name=PYTHON_NUGET if a.python_nupkg else PYTHON_INSTALLER
    if not python_asset.is_file() or digest(python_asset)!=a.python_sha256.lower():
        p.error("Python asset digest mismatch")
    if not wheel.is_file() or digest(wheel)!=a.wheel_sha256.lower():
        p.error("Application wheel digest mismatch")
    if a.target.exists() or not a.target.is_absolute():
        p.error("New absolute target required")
    wheels=list(a.wheelhouse.iterdir())
    if not wheels or len(wheels)>100 or any(not f.is_file() or f.is_symlink() or f.suffix!=".whl" for f in wheels):
        p.error("Wheelhouse must contain only wheels")
    root=a.target
    root.mkdir(parents=True,exist_ok=False)
    try:
        inputs=root/"input";output=root/"output"
        (inputs/"wheelhouse").mkdir(parents=True)
        output.mkdir()
        for f in wheels:shutil.copyfile(f,inputs/"wheelhouse"/f.name)
        shutil.copyfile(python_asset,inputs/python_name)
        for name in SCRIPTS:
            shutil.copyfile(Path(__file__).resolve().parent/name,inputs/name)
        (inputs/"python.sha256").write_text(a.python_sha256.lower(),encoding="ascii")
        (inputs/"wheel.sha256").write_text(a.wheel_sha256.lower(),encoding="ascii")
        config=ET.Element("Configuration")
        for tag,value in (("VGpu","Disable"),("Networking","Disable"),("AudioInput","Disable"),
                          ("VideoInput","Disable"),("ClipboardRedirection","Disable"),
                          ("PrinterRedirection","Disable"),("MemoryInMB","4096")):
            ET.SubElement(config,tag).text=value
        folders=ET.SubElement(config,"MappedFolders")
        for host,guest,readonly in ((inputs,r"C:\VBInput","true"),(output,r"C:\VBOutput","false")):
            folder=ET.SubElement(folders,"MappedFolder")
            for tag,value in (("HostFolder",str(host)),("SandboxFolder",guest),("ReadOnly",readonly)):
                ET.SubElement(folder,tag).text=value
        ET.SubElement(ET.SubElement(config,"LogonCommand"),"Command").text=(
            r"powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\VBInput\sandbox_acceptance.ps1")
        ET.indent(config)
        ET.ElementTree(config).write(root/"VoxBridge-test.wsb",encoding="utf-8",xml_declaration=True)
        print("SANDBOX_CONFIG_READY",root/"VoxBridge-test.wsb",len(wheels))
    except BaseException:
        shutil.rmtree(root)
        raise


if __name__=="__main__":main()
