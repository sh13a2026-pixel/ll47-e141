@echo off
REM ================================================================
REM  Build APK - Fixed version (auto-patches AGP + Gradle versions)
REM  AGP 8.3.1 -> 8.9.1  |  Gradle 8.7 -> 8.11.1  |  Java 1.8 -> 11
REM ================================================================
cd /d "%~dp0"

REM --- 1. Clean and prepare clean temp_build_src staging folder ---
echo ==========================================
echo  Preparing clean temp_build_src staging folder...
echo ==========================================
if exist temp_build_src rmdir /s /q temp_build_src
mkdir temp_build_src
mkdir temp_build_src\app
mkdir temp_build_src\assets

copy main.py temp_build_src\ >nul
copy requirements.txt temp_build_src\ >nul
copy pyproject.toml temp_build_src\ >nul
copy cleanup_worker.py temp_build_src\ >nul
xcopy /s /e /y app temp_build_src\app\ >nul
xcopy /s /e /y assets temp_build_src\assets\ >nul

set "SERIOUS_PYTHON_SITE_PACKAGES=%cd%\temp_build_src\build\site-packages"

REM --- 2. Add Flutter to PATH ---
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
echo  LL47 e141 - Build APK (Staging Folder)
echo ==========================================

REM --- 3. Install Python deps ---
if not exist .venv (
    echo Creating virtualenv...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

REM --- 4. Run flet build (creates/updates temp_build_src\build\flutter) ---
echo.
echo [1/4] Running flet build on temp_build_src...
flet build apk temp_build_src --project ll47_e141 --org vn.mil.e141 --product "Quan ly LL47 e141" --no-rich-output 2>&1 | findstr /V "Caching\|cache\|dists"
REM NOTE: flet build will FAIL at Gradle step - that's OK, we patch below

REM --- 5. Package clean app.zip ---
echo.
echo [2/4] Overwriting app.zip with clean zip...
python make_app_zip.py

REM --- 6. Patch Android Gradle Plugin version ---
echo.
echo [3/4] Patching Android Gradle Plugin: 8.3.1 -> 8.9.1 ...
python patch_build.py

REM --- 7. Run flutter build apk directly in temp_build_src ---
echo.
echo [4/4] Running flutter build apk in temp_build_src...
cd temp_build_src\build\flutter
flutter build apk --release
cd ..\..\..

REM --- 8. Copy APK to dist ---
echo.
if exist "temp_build_src\build\flutter\build\app\outputs\flutter-apk\app-release.apk" (
    if not exist "dist" mkdir dist
    copy /Y "temp_build_src\build\flutter\build\app\outputs\flutter-apk\app-release.apk" "dist\LL47_E141_v2.6.0.apk"
    echo ==========================================
    echo  Build THANH CONG!
    echo  APK: dist\LL47_E141_v2.6.0.apk
    echo ==========================================
) else (
    echo [X] APK not found. Check build logs above.
)
pause
