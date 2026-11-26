# Explicit per-user shortcuts. No application launch or overwrite.
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\OMOI-VoxBridge'),
    [string]$SourceDir = '',
    [string]$Destination = ([Environment]::GetFolderPath('Desktop'))
)
$ErrorActionPreference = 'Stop'
function LocalDirectory([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path) -or $Path.StartsWith('\\')) { throw 'Absolute local path required' }
    $item = Get-Item -LiteralPath $Path
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Invalid directory' }
    return $item.FullName
}
$InstallDir = LocalDirectory $InstallDir
$Destination = LocalDirectory $Destination
$start = Join-Path $InstallDir 'Start-VoxBridge.cmd'
$python = Join-Path $InstallDir 'venv\Scripts\pythonw.exe'
$doctor = Join-Path $InstallDir 'Doctor.cmd'
foreach ($file in @($start,$python,$doctor)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing installation file: $file" }
}
$links = @(
    @{Name='OMOI VoxBridge.lnk';Target=$start;Work=$InstallDir;Icon="$python,0";Description='Start OMOI VoxBridge'},
    @{Name='OMOI VoxBridge - Diagnose.lnk';Target=$doctor;Work=$InstallDir;Icon="$python,0";Description='Offline diagnostics'}
)
if ($SourceDir) {
    $SourceDir = LocalDirectory $SourceDir
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDir 'SOURCE_MANIFEST.json') -PathType Leaf)) { throw 'Source manifest missing' }
    $links += @{Name='OMOI VoxBridge - Quellcode.lnk';Target=$SourceDir;Work=$SourceDir;Icon='';Description='Source code'}
}
$shell = New-Object -ComObject WScript.Shell
# Validate all existing links before creating any.
foreach ($spec in $links) {
    $path = Join-Path $Destination $spec.Name
    if (Test-Path -LiteralPath $path) {
        $item = Get-Item -LiteralPath $path
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Existing shortcut is not a regular file' }
        $old = $shell.CreateShortcut($path)
        if ($old.TargetPath -ne $spec.Target -or $old.Arguments -ne '' -or $old.WorkingDirectory -ne $spec.Work) { throw "Existing shortcut not overwritten: $path" }
    }
}
foreach ($spec in $links) {
    $path = Join-Path $Destination $spec.Name
    if (-not (Test-Path -LiteralPath $path)) {
        $link = $shell.CreateShortcut($path)
        $link.TargetPath = $spec.Target
        $link.WorkingDirectory = $spec.Work
        $link.Description = $spec.Description
        if ($spec.Icon) { $link.IconLocation = $spec.Icon }
        $link.Save()
    }
    $actual = $shell.CreateShortcut($path)
    if ($actual.TargetPath -ne $spec.Target -or $actual.Arguments -ne '' -or $actual.WorkingDirectory -ne $spec.Work) { throw "Shortcut verification failed: $path" }
    Write-Output "SHORTCUT_READY $path"
}
