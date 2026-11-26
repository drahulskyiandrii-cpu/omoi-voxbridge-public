@echo off
setlocal
cd /d "%~dp0"
set "VOXBRIDGE_PROFILE_ROOT=%LOCALAPPDATA%\OMOI-VoxBridge-Standalone"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-dev.ps1" -IncludeMcp
if errorlevel 1 (
    echo Installation failed. See the message above.
    pause
    exit /b 1
)
echo Installation complete. Open Start-VoxBridge.cmd.
pause
