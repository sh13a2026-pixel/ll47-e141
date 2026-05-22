@echo off
REM Chay app trong trinh duyet (de chac chan thay duoc)
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv
)

set VPY=.venv\Scripts\python.exe
set VFLET=.venv\Scripts\flet.exe

"%VPY%" -c "import flet" 2>nul
if errorlevel 1 (
    "%VPY%" -m pip install -r requirements.txt
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo.
echo === Chay app o che do WEB ===
echo Trinh duyet se tu mo. Neu khong, vao thu http://127.0.0.1:8550
echo Bam Ctrl+C de dung.
echo.

"%VFLET%" run --web main.py

pause
