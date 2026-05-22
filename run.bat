@echo off
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv
)

set VPY=.venv\Scripts\python.exe
set VFLET=.venv\Scripts\flet.exe

REM Kiem tra Flet va flet-desktop
"%VPY%" -c "import flet" 2>nul
if errorlevel 1 (
    echo Cai Flet vao venv...
    "%VPY%" -m pip install --upgrade pip
    "%VPY%" -m pip install -r requirements.txt
)

REM Cai flet-desktop neu thieu (binary native runtime)
"%VPY%" -c "import flet_desktop" 2>nul
if errorlevel 1 (
    echo Cai flet-desktop...
    "%VPY%" -m pip install flet-desktop==0.25.2
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo.
echo === Chay ung dung Quan ly LL47 (Desktop)... ===
echo Bam Ctrl+C de dung.
echo.
"%VFLET%" run main.py
