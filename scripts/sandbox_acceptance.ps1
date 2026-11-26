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
    $installer = Join-Path $inputRoot 'python-3.11.9-amd64.exe'
    $expected = (Get-Content "$inputRoot\python.sha256" -Raw).Trim()
    if ((Get-FileHash $installer -Algorithm SHA256).Hash -ne $expected) { throw 'Python installer hash mismatch' }
    Write-Output 'SANDBOX_PYTHON_INSTALL_START'
    # Copy executable out of the read-only mapping before executing it.
    $localInstaller = Join-Path $env:TEMP 'voxbridge-test-python.exe'
    Copy-Item -LiteralPath $installer -Destination $localInstaller
    if ((Get-FileHash $localInstaller -Algorithm SHA256).Hash -ne $expected) { throw 'Copied installer hash mismatch' }
    $proc = Start-Process -FilePath $localInstaller -ArgumentList @('/quiet','InstallAllUsers=0',"TargetDir=$pythonRoot",'Include_launcher=0','Include_test=0','Include_doc=0','Include_tcltk=0','Include_pip=1','PrependPath=0','Shortcuts=0') -PassThru
    if (-not $proc.WaitForExit(600000)) {
        $proc.Kill()
        throw 'Python installer timeout; close disposable Sandbox'
    }
    if ($proc.ExitCode -ne 0) { throw "Python installer exit $($proc.ExitCode)" }
    Remove-Item -LiteralPath $localInstaller
    Write-Output 'SANDBOX_PYTHON_INSTALL_PASS'
    $python = Join-Path $pythonRoot 'python.exe'
    & $python -I -V
    if ($LASTEXITCODE -ne 0) { throw 'Python failed' }
    $wheel = Get-Item "$inputRoot\wheelhouse\omoi_voxbridge-0.1.0-py3-none-any.whl"
    $sha = (Get-Content "$inputRoot\wheel.sha256" -Raw).Trim()
    & $python -I "$inputRoot\install_standalone.py" --wheel $wheel.FullName --sha256 $sha --wheelhouse "$inputRoot\wheelhouse" --target $target
    if ($LASTEXITCODE -ne 0) { throw 'Install failed' }
    $installed = Join-Path $target 'venv\Scripts\python.exe'
    Set-Location $target
    & $installed -I -c "from pathlib import Path; import sys,voxbridge; from PySide6 import QtCore,QtGui,QtWidgets,QtMultimedia; import openai,cartesia,httpx,numpy,soundcard,sounddevice,websockets,mcp,comtypes; assert Path(voxbridge.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()); print('SANDBOX_RUNTIME_IMPORTS_PASS',QtCore.qVersion())"
    if ($LASTEXITCODE -ne 0) { throw 'Runtime imports failed' }
    foreach ($test in @('smoke_app_offscreen.py','smoke_captions_mock.py')) {
        & $installed -I "$inputRoot\$test"
        if ($LASTEXITCODE -ne 0) { throw "Smoke failed: $test" }
    }
    & $installed -I "$inputRoot\check_hotkey_lifecycle.py" --register-test-hotkey
    if ($LASTEXITCODE -ne 0) { throw 'Native hotkey lifecycle failed' }
    # No recording, model loading or paid API access. Actual native GUI test is separate.
    Write-Output 'SANDBOX_ACCEPTANCE_PASS'
    Set-Content -Path "$outputRoot\result.txt" -Value 'PASS' -Encoding ASCII
} catch {
    Write-Output $_.Exception.Message
    Set-Content -Path "$outputRoot\result.txt" -Value 'FAIL' -Encoding ASCII
} finally {
    Stop-Transcript
}
