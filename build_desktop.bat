@echo off
REM Build ung dung Desktop Windows (.exe) tu Flet
REM Chay: build_desktop.bat (double-click cung duoc)

cd /d "%~dp0"

REM Tu dong phat hien va them Flutter SDK vao PATH neu chua co
where flutter >nul 2>&1
if errorlevel 1 (
  if exist "D:\flutter\3.41.4\bin" (
    echo [i] Tim thay Flutter SDK tai D:\flutter\3.41.4\bin. Dang them vao PATH...
    set "PATH=D:\flutter\3.41.4\bin;%PATH%"
  ) else if exist "C:\Users\nhat anh\flutter\3.41.4\bin" (
    echo [i] Tim thay Flutter SDK tai C:\Users\nhat anh\flutter\3.41.4\bin. Dang them vao PATH...
    set "PATH=C:\Users\nhat anh\flutter\3.41.4\bin;%PATH%"
  ) else (
    echo [X] Khong tim thay Flutter SDK. Hay dam bao da cai dat Flutter.
  )
)

echo ==========================================
echo  Quan ly LL47 e141 - Build Desktop Windows
echo ==========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [X] Can Python 3.10+. Tai: https://www.python.org/
  pause & exit /b 1
)
python --version

REM Visual Studio C++ Build Tools (Flutter desktop yeu cau)
echo [i] Kiem tra Visual Studio Build Tools...
where cl >nul 2>&1
if errorlevel 1 (
  echo [!] Khong tim thay cl.exe. Neu build loi, hay cai:
  echo     Visual Studio 2022 Community + "Desktop development with C++"
  echo     https://visualstudio.microsoft.com/downloads/
  echo.
)

REM Tao venv
if not exist .venv (
  echo Tao virtualenv...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo Cai flet...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt

echo.
echo Bat dau build Desktop Windows ^(lan dau mat 5-15 phut^)...
echo ----------------------------------------------
flet build windows --project ll47_e141 --org vn.mil.e141 --product "Quan ly LL47 e141" -vv

if errorlevel 1 (
  echo.
  echo ==========================================
  echo [X] Build that bai. Kiem tra log ben tren.
  echo ==========================================
  pause & exit /b 1
)

echo.
echo ==========================================
echo Da build xong!
echo Folder ket qua: %cd%\build\windows\
echo File chay: %cd%\build\windows\ll47_e141.exe
echo.
echo Co the zip ca folder build\windows\ de gui cho nguoi khac.
echo Truoc khi chay tren may khac can: Microsoft Visual C++ Redistributable
echo (https://aka.ms/vs/17/release/vc_redist.x64.exe)
echo ==========================================
pause
