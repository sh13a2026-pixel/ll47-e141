@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================================
echo   LL47 e141 — Deploy Server len VPS 27.71.20.168
echo ============================================================
echo.

REM Kiem tra SSH co san khong
where ssh >nul 2>&1
if errorlevel 1 (
    echo [X] Khong tim thay ssh.exe. Can cai OpenSSH hoac Git Bash.
    pause & exit /b 1
)

set VPS_HOST=27.71.20.168
set VPS_USER=root
set VPS_PASS=Zzxcvbnm12@
set REMOTE_DIR=/root/ll47_v3

echo [i] Dang ket noi VPS va deploy...
echo.

REM Dung sshpass neu co, neu khong dung SSH key
where sshpass >nul 2>&1
if not errorlevel 1 (
    set SSH_CMD=sshpass -p "%VPS_PASS%" ssh -o StrictHostKeyChecking=no %VPS_USER%@%VPS_HOST%
) else (
    set SSH_CMD=ssh -o StrictHostKeyChecking=no %VPS_USER%@%VPS_HOST%
)

REM Chay deploy qua Python script (xu ly SSH tot hon bat)
python deploy_server.py

if errorlevel 1 (
    echo.
    echo [X] Deploy that bai.
    pause & exit /b 1
)

echo.
echo ============================================================
echo  [OK] Deploy hoan thanh!
echo  Server dang chay tai: http://%VPS_HOST%:8080
echo  Kiem tra: http://%VPS_HOST%/health
echo ============================================================
pause
