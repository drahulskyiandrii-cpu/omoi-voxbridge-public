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
"%VB_PYTHON%" -I "%~dp0scripts\demo_captions.py"
if errorlevel 1 pause
