@echo off
REM Chan doan Visual Studio + Flutter desktop toolchain
REM Chay: diagnose_vs.bat

cd /d "%~dp0"

REM Them Flutter SDK vao PATH
if exist "D:\flutter\3.41.4\bin" set "PATH=D:\flutter\3.41.4\bin;%PATH%"
if exist "C:\Users\nhat anh\flutter\3.41.4\bin" set "PATH=C:\Users\nhat anh\flutter\3.41.4\bin;%PATH%"

echo ============================================================
echo  CHAN DOAN VISUAL STUDIO + FLUTTER DESKTOP
echo ============================================================
echo.

echo [1] Tim Visual Studio bang vswhere...
echo ------------------------------------------------------------
set "VSWHERE=C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
if exist "%VSWHERE%" (
  echo Tim thay vswhere.exe
  "%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Workload.NativeDesktop -property installationPath
  echo.
  echo --- Tat ca cac ban VS da cai ---
  "%VSWHERE%" -all -products * -property displayName -property installationPath
) else (
  echo [X] Khong tim thay vswhere.exe tai duong dan:
  echo     %VSWHERE%
  echo     ^^ Co nghia la Visual Studio CHUA cai dat tren may
  echo     ^(VS Code la khac voi Visual Studio 2022 — VS Code khong build duoc Windows desktop^)
)
echo.

echo [2] Kiem tra cl.exe (Visual C++ compiler)...
echo ------------------------------------------------------------
where cl 2>nul
if errorlevel 1 (
  echo [X] cl.exe khong co trong PATH ^(binh thuong — chi co trong Developer Command Prompt^)
)
echo.

echo [3] Flutter doctor...
echo ------------------------------------------------------------
where flutter >nul 2>&1
if errorlevel 1 (
  echo [X] Khong tim thay flutter trong PATH
) else (
  flutter doctor -v
)
echo.

echo [4] Workload da cai tren VS...
echo ------------------------------------------------------------
if exist "%VSWHERE%" (
  echo --- Workload trong VS ---
  "%VSWHERE%" -latest -property packages 2>nul | findstr /i "Workload"
)
echo.

echo ============================================================
echo  KET LUAN
echo ============================================================
echo.
echo Neu o muc [1] khong thay duong dan "installationPath":
echo   - Truy cap https://visualstudio.microsoft.com/downloads/
echo   - Cai Visual Studio 2022 Community ^(MIEN PHI^)
echo   - Khi cai bat buoc tich workload: "Desktop development with C++"
echo.
echo Neu thay [1] in ra duong dan nhung Flutter van bao chua co:
echo   - Restart may de Flutter nhan lai
echo   - Hoac chay: flutter config --enable-windows-desktop
echo   - Hoac dam bao VS phien ban 2019 tro len ^(2017 khong duoc^)
echo.
echo Neu VS da co nhung THIEU workload "Desktop development with C++":
echo   - Mo Visual Studio Installer ^(co san trong Start Menu^)
echo   - Bam Modify tren ban VS hien co
echo   - Tich "Desktop development with C++"
echo   - Bam Modify de cai workload do
echo.
pause
