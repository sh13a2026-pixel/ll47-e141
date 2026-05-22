# =============================================================
#  Quản lý LL47 e141 — Cài đầy đủ tài nguyên Windows + Build APK
# =============================================================
#
# CHẠY:
#   Chuột phải file install_all.ps1 → "Run with PowerShell"
#   Hoặc trong PowerShell:
#       cd "C:\Users\nhat anh\Downloads\ll47_python"
#       Set-ExecutionPolicy -Scope Process Bypass
#       .\install_all.ps1
#
# Yêu cầu: Windows 10/11 có winget (mặc định đã cài sẵn)
# Cần quyền admin để cài app + set ENV. Script tự xin nâng quyền.
# =============================================================

# ---------- 0. Kiểm tra & xin quyền admin ----------
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole("Administrators")
if (-not $IsAdmin) {
    Write-Host "🔼 Đang xin quyền Administrator..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-NoExit", "-File", "`"$PSCommandPath`""
    exit
}

$ErrorActionPreference = "Stop"
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Quản lý LL47 e141 — Setup Build Environment" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ---------- 1. Kiểm tra winget ----------
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "❌ Không tìm thấy winget. Cài 'App Installer' từ Microsoft Store trước." -ForegroundColor Red
    Write-Host "   https://apps.microsoft.com/detail/9NBLGGH4NNS1"
    pause; exit 1
}
Write-Host "✓ winget có sẵn" -ForegroundColor Green

# ---------- 2. Cài Python 3.11 ----------
Write-Host ""
Write-Host "📦 [1/5] Cài Python 3.11..." -ForegroundColor Cyan
winget install -e --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements

# ---------- 3. Cài Java JDK 17 (Temurin) ----------
Write-Host ""
Write-Host "📦 [2/5] Cài Java JDK 17 (Eclipse Temurin)..." -ForegroundColor Cyan
winget install -e --id EclipseAdoptium.Temurin.17.JDK --silent --accept-source-agreements --accept-package-agreements

# ---------- 4. Cài Git ----------
Write-Host ""
Write-Host "📦 [3/5] Cài Git..." -ForegroundColor Cyan
winget install -e --id Git.Git --silent --accept-source-agreements --accept-package-agreements

# ---------- 5. Cài Android Studio (kèm Android SDK) ----------
Write-Host ""
Write-Host "📦 [4/5] Cài Android Studio (~1GB, có thể lâu)..." -ForegroundColor Cyan
Write-Host "   Bao gồm Android SDK, Build Tools, Platform Tools."
winget install -e --id Google.AndroidStudio --silent --accept-source-agreements --accept-package-agreements

# ---------- 6. Refresh PATH cho session hiện tại ----------
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

# ---------- 7. Tìm JAVA_HOME ----------
$javaPath = Get-ChildItem "C:\Program Files\Eclipse Adoptium" -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "jdk-17*" } | Select-Object -First 1 -ExpandProperty FullName
if (-not $javaPath) {
    $javaPath = Get-ChildItem "C:\Program Files\Java" -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like "jdk-17*" } | Select-Object -First 1 -ExpandProperty FullName
}
if ($javaPath) {
    [Environment]::SetEnvironmentVariable("JAVA_HOME", $javaPath, "Machine")
    $env:JAVA_HOME = $javaPath
    Write-Host "✓ JAVA_HOME = $javaPath" -ForegroundColor Green
} else {
    Write-Host "⚠️ Không tìm thấy JDK 17. Cài Java rồi chạy lại script." -ForegroundColor Yellow
}

# ---------- 8. Set ANDROID_HOME ----------
$androidHome = "$env:LOCALAPPDATA\Android\Sdk"
if (-not (Test-Path $androidHome)) {
    Write-Host ""
    Write-Host "⚠️  Android SDK chưa tồn tại tại: $androidHome" -ForegroundColor Yellow
    Write-Host "    HÃY MỞ Android Studio LẦN ĐẦU để nó tải SDK về:" -ForegroundColor Yellow
    Write-Host "    Start Menu → Android Studio → More Actions → SDK Manager" -ForegroundColor Yellow
    Write-Host "    Cài: Android SDK Platform 34 + SDK Build-Tools 34.0.0 + Platform-Tools" -ForegroundColor Yellow
    Read-Host "Nhấn Enter sau khi mở Android Studio và cài xong các thành phần trên"
}
[Environment]::SetEnvironmentVariable("ANDROID_HOME", $androidHome, "Machine")
[Environment]::SetEnvironmentVariable("ANDROID_SDK_ROOT", $androidHome, "Machine")
$env:ANDROID_HOME = $androidHome
$env:ANDROID_SDK_ROOT = $androidHome
Write-Host "✓ ANDROID_HOME = $androidHome" -ForegroundColor Green

# Thêm platform-tools vào PATH
$plat = "$androidHome\platform-tools"
$cur = [Environment]::GetEnvironmentVariable("Path", "Machine")
if ($cur -notlike "*$plat*") {
    [Environment]::SetEnvironmentVariable("Path", "$cur;$plat", "Machine")
}

# ---------- 9. Tạo virtualenv + cài Flet ----------
Write-Host ""
Write-Host "📦 [5/5] Cài Flet (Python framework)..." -ForegroundColor Cyan
$proj = $PSScriptRoot
Set-Location $proj

if (-not (Test-Path "$proj\.venv")) {
    & python -m venv .venv
}
& "$proj\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$proj\.venv\Scripts\python.exe" -m pip install -r requirements.txt

# ---------- 10. Tóm tắt ----------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " ✅ CÀI ĐẶT XONG" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Python: $(& python --version 2>&1)"
Write-Host " Java:   $((& java -version 2>&1)[0])"
Write-Host " JAVA_HOME: $env:JAVA_HOME"
Write-Host " ANDROID_HOME: $env:ANDROID_HOME"
Write-Host " Flet: $(& "$proj\.venv\Scripts\python.exe" -c 'import flet; print(flet.__version__)')"
Write-Host ""
Write-Host " Tiếp theo: chạy 'build_apk.bat' hoặc:" -ForegroundColor Cyan
Write-Host "   .\.venv\Scripts\activate" -ForegroundColor White
Write-Host "   flet build apk --project ll47_e141 --org vn.mil.e141" -ForegroundColor White
Write-Host ""
$ans = Read-Host "Build APK luôn bây giờ? (y/n)"
if ($ans -eq "y") {
    & "$proj\.venv\Scripts\python.exe" -m flet build apk --project ll47_e141 --org vn.mil.e141
    Write-Host ""
    Write-Host "📦 APK: $proj\build\apk\app-release.apk" -ForegroundColor Green
}

Write-Host ""
Write-Host "Nhấn Enter để đóng..."
Read-Host
