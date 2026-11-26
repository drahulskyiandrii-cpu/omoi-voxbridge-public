# Starts ONLY VoxBridge, using its own venv. Does not install or load models.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "No VoxBridge .venv. Run scripts/install-dev.ps1 explicitly"
    exit 2
}
$env:PYTHONPATH = Join-Path $root "src"
& $python -m voxbridge
exit $LASTEXITCODE
