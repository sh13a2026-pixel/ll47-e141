@echo off
REM Build app duoi dang web (HTML/JS) — chay tren trinh duyet
REM Khong can Visual Studio, khong can cai gi tren may dich

cd /d "%~dp0"

REM Tu dong them Flutter SDK
where flutter >nul 2>&1
if errorlevel 1 (
  if exist "D:\flutter\3.41.4\bin" set "PATH=D:\flutter\3.41.4\bin;%PATH%"
  if exist "C:\Users\nhat anh\flutter\3.41.4\bin" set "PATH=C:\Users\nhat anh\flutter\3.41.4\bin;%PATH%"
)

echo ==========================================
echo  Quan ly LL47 e141 - Build Web (HTML/JS)
echo ==========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [X] Can Python 3.10+. Tai: https://www.python.org/
  pause & exit /b 1
)

if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt

echo.
echo [i] Bat dau build web (~3-5 phut)...
echo ----------------------------------------------
flet build web --project ll47_e141 --org vn.mil.e141 --product "Quan ly LL47 e141" -vv

if errorlevel 1 (
  echo [X] Build that bai. Kiem tra log.
  pause & exit /b 1
)

echo.
echo ==========================================
echo [OK] Da build xong!
echo Folder: %cd%\build\web\
echo.
echo De chay thu LUOC tren may minh:
echo   cd build\web
echo   python -m http.server 8000
echo   Mo trinh duyet: http://localhost:8000
echo.
echo De host len internet: upload folder build\web\ len
echo bat ky web server nao (GitHub Pages, Netlify, Vercel, server rieng...)
echo ==========================================
pause
