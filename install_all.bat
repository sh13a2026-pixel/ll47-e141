@echo off
REM Wrapper goi PowerShell de chay install_all.ps1 voi quyen admin
REM Double-click file nay la xong

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_all.ps1"
pause
