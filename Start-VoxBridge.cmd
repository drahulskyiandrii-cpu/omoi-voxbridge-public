@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    if exist "%LOCALAPPDATA%\Programs\OMOI-VoxBridge\Start-VoxBridge.cmd" (
        call "%LOCALAPPDATA%\Programs\OMOI-VoxBridge\Start-VoxBridge.cmd"
        exit /b
    )
    echo Run Setup-VoxBridge.cmd first.
    pause
    exit /b 2
)
"%~dp0.venv\Scripts\python.exe" -I -c "import voxbridge.app"
if errorlevel 1 (
    echo Run Setup-VoxBridge.cmd to repair dependencies.
    pause
    exit /b 2
)
set "VOXBRIDGE_PROFILE_ROOT=%LOCALAPPDATA%\OMOI-VoxBridge-Standalone"
start "" /D "%~dp0." "%~dp0.venv\Scripts\pythonw.exe" -I -c "from voxbridge.app import main_standalone; raise SystemExit(main_standalone())"
