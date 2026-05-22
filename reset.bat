@echo off
REM Script reset hoan toan: xoa venv, xoa cache, cai lai

cd /d "%~dp0"

echo [!] CANH BAO: Script nay se xoa .venv va cai lai tu dau.
set /p confirm=Tiep tuc? (y/N):
if /i not "%confirm%"=="y" (
    echo Da huy.
    exit /b
)

echo.
echo Dang xoa .venv...
if exist .venv rmdir /s /q .venv

echo Dang xoa pycache...
if exist __pycache__ rmdir /s /q __pycache__
if exist app\__pycache__ rmdir /s /q app\__pycache__

echo.
echo Dang tao venv moi...
python -m venv .venv
call .venv\Scripts\activate.bat

echo.
echo Dang cai dependencies...
pip install --upgrade pip
pip install -r requirements.txt

echo.
echo === XONG ===
echo Bam Enter de chay app, hoac dong cua so de thoat.
pause >nul

set PYTHONUTF8=1
flet run
pause
