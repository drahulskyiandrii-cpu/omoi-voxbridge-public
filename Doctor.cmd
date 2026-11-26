@echo off
setlocal
cd /d "%~dp0"
set "VB_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%VB_PYTHON%" set "VB_PYTHON=%LOCALAPPDATA%\Programs\OMOI-VoxBridge\venv\Scripts\python.exe"
if not exist "%VB_PYTHON%" (
    echo Run Setup-VoxBridge.cmd first.
    pause
    exit /b 2
)
set "VOXBRIDGE_PROFILE_ROOT=%LOCALAPPDATA%\OMOI-VoxBridge-Standalone"
"%VB_PYTHON%" -I -m voxbridge.diagnostics
pause
