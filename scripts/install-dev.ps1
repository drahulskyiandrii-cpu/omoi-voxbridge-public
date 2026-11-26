# Install ONLY into the dedicated VoxBridge environment. Does not start the app.
param(
    [switch]$IncludeSelection,
    [switch]$IncludeMcp,
    [switch]$Plan
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$extras = @("cloud", "captions")
if ($IncludeSelection) { $extras += "selection" }
if ($IncludeMcp) { $extras += "mcp" }
$target = ".[" + ($extras -join ",") + "]"
Write-Host "VoxBridge installation: $target (dedicated .venv; no auto-launch)"
if ($Plan) { exit 0 }
if (Test-Path $venv) {
    $item = Get-Item -LiteralPath $venv
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing redirected .venv"
    }
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath (Join-Path $venv "pyvenv.cfg"))) {
        throw "Existing .venv is not a valid Python environment; not overwritten"
    }
} else {
    py -3.11 -m venv $venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Push-Location $root
try {
    & $python -c "import sys,pathlib; assert pathlib.Path(sys.prefix).resolve() == pathlib.Path('.venv').resolve() and sys.prefix != sys.base_prefix and sys.version_info >= (3,11)"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python -m pip install --disable-pip-version-check -e $target
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python -m voxbridge.diagnostics
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
