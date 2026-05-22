# 📋 Lệnh CMD/PowerShell cài đầy đủ tài nguyên + build APK

> **Cách nhanh nhất**: double-click `install_all.bat` — script tự xin admin, cài tất cả, build luôn.
>
> Hoặc chạy thủ công các lệnh dưới đây trong **Command Prompt (Run as Administrator)**.

---

## ⚡ ALL-IN-ONE — copy-paste 1 dòng PowerShell

Mở **PowerShell as Administrator**, dán toàn bộ khối dưới và Enter:

```powershell
# 1. Cài 4 thứ qua winget
winget install -e --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements
winget install -e --id EclipseAdoptium.Temurin.17.JDK --silent --accept-source-agreements --accept-package-agreements
winget install -e --id Git.Git --silent --accept-source-agreements --accept-package-agreements
winget install -e --id Google.AndroidStudio --silent --accept-source-agreements --accept-package-agreements

# 2. Set ENV
$jdk = (Get-ChildItem "C:\Program Files\Eclipse Adoptium" -Directory | Where-Object Name -like "jdk-17*" | Select-Object -First 1).FullName
[Environment]::SetEnvironmentVariable("JAVA_HOME", $jdk, "Machine")
[Environment]::SetEnvironmentVariable("ANDROID_HOME", "$env:LOCALAPPDATA\Android\Sdk", "Machine")
[Environment]::SetEnvironmentVariable("ANDROID_SDK_ROOT", "$env:LOCALAPPDATA\Android\Sdk", "Machine")

# 3. Reload ENV cho session hiện tại
$env:JAVA_HOME = $jdk
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")

# 4. Cài Flet
cd "$env:USERPROFILE\Downloads\ll47_python"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt

# 5. Mở Android Studio LẦN ĐẦU để nó tải SDK xuống
Write-Host "→ Mở Android Studio: chọn SDK Manager → cài Platform 34 + Build-Tools 34" -ForegroundColor Yellow
Start-Process "C:\Program Files\Android\Android Studio\bin\studio64.exe"
Read-Host "Sau khi cài xong SDK trong Android Studio, nhấn Enter để tiếp tục"

# 6. Build APK
flet build apk --project ll47_e141 --org vn.mil.e141
Write-Host "✅ APK: $pwd\build\apk\app-release.apk" -ForegroundColor Green
```

---

## 🛠 Cài từng phần thủ công (CMD)

### 1. Mở CMD as Administrator

Start → gõ "cmd" → chuột phải Command Prompt → **Run as administrator**

### 2. Cài Python 3.11

```cmd
winget install -e --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements
```

Kiểm tra:
```cmd
python --version
```

### 3. Cài Java JDK 17

```cmd
winget install -e --id EclipseAdoptium.Temurin.17.JDK --silent --accept-source-agreements --accept-package-agreements
```

### 4. Cài Git

```cmd
winget install -e --id Git.Git --silent --accept-source-agreements --accept-package-agreements
```

### 5. Cài Android Studio (kèm Android SDK)

```cmd
winget install -e --id Google.AndroidStudio --silent --accept-source-agreements --accept-package-agreements
```

### 6. Set biến môi trường (CMD)

```cmd
setx JAVA_HOME "C:\Program Files\Eclipse Adoptium\jdk-17.0.13.11-hotspot" /M
setx ANDROID_HOME "%LOCALAPPDATA%\Android\Sdk" /M
setx ANDROID_SDK_ROOT "%LOCALAPPDATA%\Android\Sdk" /M
```

> ⚠️ Đổi `jdk-17.0.13.11-hotspot` thành tên thư mục JDK thật:
> ```cmd
> dir "C:\Program Files\Eclipse Adoptium\"
> ```

### 7. Mở Android Studio LẦN ĐẦU để cài SDK

```cmd
start "" "C:\Program Files\Android\Android Studio\bin\studio64.exe"
```

Trong Android Studio:
1. Chọn **More Actions → SDK Manager**
2. Tab **SDK Platforms**: tick **Android 14 (API 34)** → Apply
3. Tab **SDK Tools**: tick **Android SDK Build-Tools 34.0.0** + **Platform-Tools** → Apply
4. Đóng Android Studio

### 8. **ĐÓNG CMD và mở CMD MỚI** (để load lại biến môi trường)

```cmd
echo %JAVA_HOME%
echo %ANDROID_HOME%
java -version
```

Nếu cả 3 đều ra kết quả → OK

### 9. Cài Flet

```cmd
cd /d "%USERPROFILE%\Downloads\ll47_python"
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 10. Build APK

```cmd
flet build apk --project ll47_e141 --org vn.mil.e141
```

> Lần đầu mất **15-25 phút** (Flet kéo Flutter SDK ~2GB + build).
> Lần sau ~2-3 phút.

### 11. Lấy APK

```cmd
dir build\apk
```

File: `build\apk\app-release.apk`

Mở Explorer:
```cmd
explorer build\apk
```

Copy file `app-release.apk` vào điện thoại Android → cài.

---

## 🆘 Troubleshooting

### `winget` không tìm thấy lệnh
- Mở Microsoft Store → tìm "App Installer" → cài
- Hoặc tải: https://apps.microsoft.com/detail/9NBLGGH4NNS1

### `python` không tìm thấy lệnh sau khi cài
- Đóng và mở lại CMD
- Hoặc reboot máy (PATH cần refresh)

### `JAVA_HOME` không nhận
```cmd
REM Xem hiện đang trỏ đâu
echo %JAVA_HOME%

REM Set lại với đường dẫn đúng
dir "C:\Program Files\Eclipse Adoptium\"
setx JAVA_HOME "C:\Program Files\Eclipse Adoptium\<tên-thư-mục-jdk-17>" /M
```

### Lỗi build "SDK location not found"
- Đảm bảo đã mở Android Studio 1 lần để nó tạo `%LOCALAPPDATA%\Android\Sdk`
- Hoặc tạo file `local.properties` trong `build/flutter/android/`:
  ```
  sdk.dir=C:\\Users\\<user>\\AppData\\Local\\Android\\Sdk
  ```

### Lỗi build "license not accepted"
```cmd
cd /d "%ANDROID_HOME%\cmdline-tools\latest\bin"
sdkmanager --licenses
```
Nhấn `y` cho mọi license.

### Build mất quá lâu (>30 phút)
Bình thường lần đầu. Kiểm tra Task Manager: `gradle.exe`, `java.exe` có chạy không. Nếu không tăng CPU thì có thể đang download Flutter — đợi tiếp.

### Flutter download bị fail
```cmd
git clone https://github.com/flutter/flutter.git -b stable %USERPROFILE%\flutter
setx PATH "%PATH%;%USERPROFILE%\flutter\bin" /M
```
Đóng/mở CMD rồi chạy lại `flet build apk`.

---

## 📊 Tổng dung lượng cần

| Phần | Dung lượng |
|---|---|
| Python 3.11 | ~100 MB |
| JDK 17 | ~200 MB |
| Git | ~300 MB |
| Android Studio | ~1 GB |
| Android SDK + Build Tools | ~3 GB |
| Flutter SDK (Flet auto kéo) | ~2 GB |
| Gradle cache | ~1.5 GB |
| **Tổng** | **~8 GB** |

Nên có ≥10GB free disk trước khi cài.

---

## 🚀 Sau khi cài xong, build lại lần sau

```cmd
cd /d "%USERPROFILE%\Downloads\ll47_python"
.venv\Scripts\activate
flet build apk
```

~2-3 phút.

Sửa code Python trong `main.py` → save → build lại → APK mới sẵn sàng.
