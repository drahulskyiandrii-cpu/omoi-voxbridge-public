# Runs only inside a disposable Windows Sandbox, with dedicated mapped folders.
$ErrorActionPreference = 'Stop'
$inputRoot = 'C:\VBInput'
$outputRoot = 'C:\VBOutput'
if (-not (Test-Path "$inputRoot\wheelhouse") -or -not (Test-Path $outputRoot)) { throw 'Sandbox mappings missing' }
if ($env:USERNAME -ne 'WDAGUtilityAccount') { throw 'Sandbox account required' }
Start-Transcript -Path "$outputRoot\acceptance.log"
try {
    $pythonRoot = Join-Path $env:LOCALAPPDATA 'VBTestPython'
    $target = Join-Path $env:LOCALAPPDATA 'Programs\OMOI-VoxBridge'
    $expected = (Get-Content "$inputRoot\python.sha256" -Raw).Trim()
    $package = Join-Path $inputRoot 'python.3.11.9.nupkg'
    if (Test-Path -LiteralPath $package) {
        Write-Output 'SANDBOX_PYTHON_NUGET_START'
        $localPackage = Join-Path $env:TEMP 'voxbridge-test-python.zip'
        Copy-Item -LiteralPath $package -Destination $localPackage
        if ((Get-FileHash $localPackage -Algorithm SHA256).Hash -ne $expected) { throw 'Python package hash mismatch' }
        # Signed PSF NuGet package, independently verified on host.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($localPackage, $pythonRoot)
        Remove-Item -LiteralPath $localPackage
        $python = Join-Path $pythonRoot 'tools\python.exe'
        Write-Output 'SANDBOX_PYTHON_NUGET_READY'
    } else {
        $installer = Join-Path $inputRoot 'python-3.11.9-amd64.exe'
        if ((Get-FileHash $installer -Algorithm SHA256).Hash -ne $expected) { throw 'Python installer hash mismatch' }
        Write-Output 'SANDBOX_PYTHON_INSTALL_START'
        $localInstaller = Join-Path $env:TEMP 'voxbridge-test-python.exe'
        Copy-Item -LiteralPath $installer -Destination $localInstaller
        if ((Get-FileHash $localInstaller -Algorithm SHA256).Hash -ne $expected) { throw 'Copied installer hash mismatch' }
        $proc = Start-Process -FilePath $localInstaller -ArgumentList @('/quiet','InstallAllUsers=0',"TargetDir=$pythonRoot",'Include_launcher=0','Include_test=0','Include_doc=0','Include_dev=0','Include_tcltk=0','Include_pip=1','CompileAll=0','PrependPath=0','Shortcuts=0') -PassThru
        if (-not $proc.WaitForExit(600000)) {
            $proc.Kill()
            throw 'Python installer timeout; close disposable Sandbox'
        }
        if ($proc.ExitCode -ne 0) { throw "Python installer exit $($proc.ExitCode)" }
        Remove-Item -LiteralPath $localInstaller
        Write-Output 'SANDBOX_PYTHON_INSTALL_PASS'
        $python = Join-Path $pythonRoot 'python.exe'
    }
    & $python -I -V
    if ($LASTEXITCODE -ne 0) { throw 'Python failed' }
    # Avoid repeated small I/O through the mapped drive.
    $guestHouse = Join-Path $env:TEMP 'voxbridge-test-wheelhouse'
    Copy-Item -LiteralPath "$inputRoot\wheelhouse" -Destination $guestHouse -Recurse
    $wheel = Get-Item "$guestHouse\omoi_voxbridge-0.1.0-py3-none-any.whl"
    $sha = (Get-Content "$inputRoot\wheel.sha256" -Raw).Trim()
    & $python -I "$inputRoot\install_standalone.py" --wheel $wheel.FullName --sha256 $sha --wheelhouse $guestHouse --target $target
    if ($LASTEXITCODE -ne 0) { throw 'Install failed' }
    $installed = Join-Path $target 'venv\Scripts\python.exe'
    Set-Location $target
    # COM and WASAPI have independent process boundaries.
    $verify = Join-Path $env:TEMP 'vb-acceptance.py'
    @'
from pathlib import Path
import subprocess, sys
from PySide6 import QtCore, QtGui, QtWidgets, QtMultimedia
import voxbridge, openai, cartesia, httpx, numpy, sounddevice, websockets, mcp
assert Path(voxbridge.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
for module in ("soundcard", "comtypes"):
    subprocess.run([sys.executable, "-I", "-c", "import "+module], check=True, timeout=30)
print("SANDBOX_RUNTIME_IMPORTS_PASS", QtCore.qVersion(), flush=True)
for script in ("smoke_app_offscreen.py","smoke_captions_mock.py","check_hotkey_lifecycle.py"):
    args = ["--register-test-hotkey"] if script.startswith("check_hotkey") else []
    subprocess.run([sys.executable,"-I",str(Path(r"C:\VBInput")/script),*args],check=True,timeout=60)
print("SANDBOX_NATIVE_AND_MOCK_CHECKS_PASS",flush=True)
'@ | Set-Content -Path $verify -Encoding ASCII
    $check = Start-Process -FilePath $installed -ArgumentList @('-I', ('"'+$verify+'"')) -RedirectStandardOutput "$outputRoot\checks.stdout.txt" -RedirectStandardError "$outputRoot\checks.stderr.txt" -PassThru
    if (-not $check.WaitForExit(240000)) { $check.Kill(); throw 'Runtime checks timeout' }
    $check.Refresh()
    if ($check.ExitCode -ne 0 -or -not (Select-String -Path "$outputRoot\checks.stdout.txt" -SimpleMatch 'SANDBOX_NATIVE_AND_MOCK_CHECKS_PASS' -Quiet)) { throw 'Runtime checks failed; see logs' }
    # No recording, model loading or paid API access. Actual native GUI test is separate.
    Write-Output 'SANDBOX_ACCEPTANCE_PASS'
    Set-Content -Path "$outputRoot\result.txt" -Value 'PASS' -Encoding ASCII
} catch {
    Write-Output $_.Exception.Message
    Set-Content -Path "$outputRoot\result.txt" -Value 'FAIL' -Encoding ASCII
} finally {
    Stop-Transcript
}
