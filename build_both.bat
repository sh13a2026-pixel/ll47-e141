@echo off
cd /d "%~dp0"

echo [i] Starting build process for v2.6.0...
echo [i] Adding Flutter SDK D:\flutter\3.41.4\bin to PATH...
set "PATH=D:\flutter\3.41.4\bin;%PATH%"

echo [i] Verifying tools...
where flutter
where java
python --version

echo [i] Activating virtual environment...
call .venv\Scripts\activate.bat

echo [i] 1. Building Android APK (v2.6.0)...
flet build apk --project ll47_e141 --org vn.mil.e141 --product "Quan ly LL47 e141" -vv
if errorlevel 1 (
    echo [X] APK Build failed!
    exit /b 1
)
echo [V] APK Build completed successfully!

echo [i] 2. Building Windows Desktop (v2.6.0)...
flet build windows --project ll47_e141 --org vn.mil.e141 --product "Quan ly LL47 e141" -vv
if errorlevel 1 (
    echo [X] Windows Desktop Build failed!
    exit /b 1
)
echo [V] Windows Desktop Build completed successfully!

echo [V] All builds completed successfully!
