@echo off
REM Build app Desktop Windows bang PyInstaller (KHONG can Visual Studio)
REM Chay: build_desktop_pyinstaller.bat (double-click)

cd /d "%~dp0"

echo ==========================================
echo  Quan ly LL47 e141 - Build Desktop (PyInstaller)
echo  (Khong can Visual Studio + Flutter desktop)
echo ==========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [X] Can Python 3.10+. Tai: https://www.python.org/
  pause & exit /b 1
)
python --version

REM Tao venv
if not exist .venv (
  echo [i] Tao virtualenv .venv...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [i] Cai/cap nhat dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
python -m pip install pyinstaller pillow

REM Xoa build cu cua PyInstaller (de tranh cache loi)
if exist build_pyi rmdir /s /q build_pyi
if exist dist rmdir /s /q dist

echo.
echo [i] Bat dau dong goi (~3-5 phut lan dau)...
echo ----------------------------------------------

REM Build .exe
REM   --noconsole : khong hien terminal khi mo app
REM   --onedir    : tao folder chua exe + DLL
REM   --name      : ten file .exe
REM   --icon      : icon cho .exe
REM   --add-data  : kem theo assets va storage (path tuyet doi cho chac)
pyinstaller ^
  --noconfirm ^
  --noconsole ^
  --onedir ^
  --name "QuanLyLL47" ^
  --icon "%cd%\assets\logo.ico" ^
  --add-data "%cd%\assets;assets" ^
  --add-data "%cd%\storage;storage" ^
  --add-data "%cd%\app;app" ^
  --hidden-import flet ^
  --hidden-import flet.fastapi ^
  --hidden-import PIL ^
  --collect-all flet ^
  --distpath "%cd%\dist" ^
  --workpath "%cd%\build_pyi" ^
  main.py

if errorlevel 1 (
  echo.
  echo ==========================================
  echo [X] Build that bai. Kiem tra log ben tren.
  echo ==========================================
  pause & exit /b 1
)

echo.
echo ==========================================
echo [OK] Da build xong!
echo.
echo Folder ket qua : %cd%\dist\QuanLyLL47\
echo File chay      : %cd%\dist\QuanLyLL47\QuanLyLL47.exe
echo.
echo De gui cho nguoi khac: zip ca folder dist\QuanLyLL47\
echo (cac DLL phai di kem .exe, khong tach roi duoc)
echo.
echo Khac voi flet build windows:
echo - Khong can Visual Studio
echo - Khong can Flutter SDK
echo - Kich thuoc lon hon (~100-200MB do kem Python runtime)
echo - Mo cham hon 1-2 giay lan dau (extract DLL)
echo ==========================================
pause
