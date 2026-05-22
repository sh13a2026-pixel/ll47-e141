@echo off
REM Tim file .exe da build va mo no
cd /d "%~dp0"

echo ============================================================
echo  TIM FILE EXE TRONG dist\
echo ============================================================
echo.

if not exist "dist" (
  echo [X] Folder dist\ KHONG ton tai!
  echo     Build chua xong hoac that bai. Chay lai build_desktop_pyinstaller.bat
  pause & exit /b 1
)

echo Cay thu muc dist:
echo ----------------------------------------
dir /b /s dist\*.exe
echo ----------------------------------------
echo.

set "EXE=%cd%\dist\QuanLyLL47\QuanLyLL47.exe"
if exist "%EXE%" (
  echo [OK] Tim thay file:
  echo      %EXE%
  echo.
  echo Dung luong folder dist\QuanLyLL47:
  for /f "tokens=3" %%a in ('dir "dist\QuanLyLL47" /s /a-d ^| findstr /C:"File(s)"') do echo      %%a bytes
  echo.
  set /p answer="Mo file .exe ngay bay gio? (y/n) "
  if /i "%answer%"=="y" (
    start "" "%EXE%"
  )
  echo.
  echo De gui cho nguoi khac: zip ca folder dist\QuanLyLL47 ^(khong tach roi DLL^)
  echo.
  echo Mo Windows Explorer den folder do:
  start "" explorer "%cd%\dist\QuanLyLL47"
) else (
  echo [X] KHONG tim thay %EXE%
  echo     Co the build dang chay do dang. Cho them 1-2 phut roi chay lai script nay.
)

echo.
pause
