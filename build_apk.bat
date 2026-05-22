@echo off
REM Build APK tu dong tren Windows
REM Chay: build_apk.bat (double-click cung duoc)

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
echo  Quan ly LL47 e141 - Build APK Windows
echo ==========================================

where python >nul 2>&1
if errorlevel 1 (
  echo [X] Can Python 3.10+. Tai: https://www.python.org/
  pause & exit /b 1
)
python --version

where java >nul 2>&1
if errorlevel 1 (
  echo [X] Can Java JDK 17. Tai: https://adoptium.net/
  pause & exit /b 1
)
java -version

REM Tao venv
if not exist .venv (
  echo Tao virtualenv...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo Cai flet...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo Bat dau build APK ^(lan dau mat 10-20 phut^)...
echo ----------------------------------------------
flet build apk --project ll47_e141 --org vn.mil.e141 --product "Quan ly LL47 e141" -vv

echo.
echo ==========================================
echo Da build xong!
echo File APK: %cd%\build\apk\app-release.apk
echo Copy file nay vao dien thoai Android va cai
echo ==========================================
pause
