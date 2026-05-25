@echo off
REM ================================================================
REM  Build APK - Fixed version (auto-patches AGP + Gradle versions)
REM  AGP 8.3.1 -> 8.9.1  |  Gradle 8.7 -> 8.11.1  |  Java 1.8 -> 11
REM ================================================================
cd /d "%~dp0"
set "SERIOUS_PYTHON_SITE_PACKAGES=%cd%\build\site-packages"

REM --- 1. Add Flutter to PATH ---
where flutter >nul 2>&1
if errorlevel 1 (
    if exist "D:\flutter\3.41.4\bin" (
        echo [i] Adding Flutter to PATH...
        set "PATH=D:\flutter\3.41.4\bin;%PATH%"
    ) else if exist "C:\Users\nhat anh\flutter\3.41.4\bin" (
        set "PATH=C:\Users\nhat anh\flutter\3.41.4\bin;%PATH%"
    ) else (
        echo [X] Flutter SDK not found!
        pause & exit /b 1
    )
)

echo ==========================================
echo  LL47 e141 - Build APK (Fixed AGP)
echo ==========================================

REM --- 2. Install Python deps ---
if not exist .venv (
    echo Creating virtualenv...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

REM --- 3. Run flet build (creates/updates build\flutter) ---
echo.
echo [1/3] Running flet build to prepare Flutter project...
flet build apk --project ll47_e141 --org vn.mil.e141 --product "Quan ly LL47 e141" --no-rich-output 2>&1 | findstr /V "Caching\|cache\|dists"
REM NOTE: flet build will FAIL at Gradle step - that's OK, we patch below

REM --- 4. Patch Android Gradle Plugin version ---
echo.
echo [2/3] Patching Android Gradle Plugin: 8.3.1 -> 8.9.1 ...
python patch_build.py

REM --- 5. Run flutter build apk directly ---
echo.
echo [3/3] Running flutter build apk...
cd build\flutter
flutter build apk --release
cd ..\..

REM --- 6. Copy APK to dist ---
echo.
if exist "build\flutter\build\app\outputs\flutter-apk\app-release.apk" (
    if not exist "dist" mkdir dist
    copy /Y "build\flutter\build\app\outputs\flutter-apk\app-release.apk" "dist\LL47_E141_v2.6.0.apk"
    echo ==========================================
    echo  Build THANH CONG!
    echo  APK: dist\LL47_E141_v2.6.0.apk
    echo ==========================================
) else (
    echo [X] APK not found. Check build logs above.
)
pause
