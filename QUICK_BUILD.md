# 🚀 Build APK nhanh — chọn 1 trong 3 cách

> Sandbox Claude không build được vì thiếu Java 17 + Android SDK + Flutter SDK
> (cần >7GB disk + access PyPI). Bạn chọn cách dễ nhất với máy của bạn.

---

## ⭐ Cách 1: GitHub Actions — KHÔNG cần cài gì (khuyến nghị)

**Phù hợp**: bạn không muốn cài Java/SDK lên máy. CI server của GitHub build hộ.

1. Tạo repo mới trên https://github.com → push thư mục `ll47_python` lên
   ```bash
   cd ll47_python
   git init && git add . && git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<USER>/ll47-e141.git
   git push -u origin main
   ```

2. File `.github/workflows/build-apk.yml` đã có sẵn — GitHub tự chạy

3. Vào tab **Actions** → chờ ~10 phút → tải APK từ phần **Artifacts**

---

## 🐳 Cách 2: Docker — chỉ cần cài Docker Desktop

**Phù hợp**: bạn có Docker Desktop. Một lệnh là xong.

```bash
cd ll47_python

# Build image (lần đầu mất 10-15 phút)
docker build -t ll47-builder .

# Build APK (mất 5-10 phút)
docker run --rm -v "$(pwd)/build:/app/build" ll47-builder

# APK sẽ ở: build/apk/app-release.apk
```

**Windows PowerShell**:
```powershell
docker run --rm -v "${PWD}/build:/app/build" ll47-builder
```

---

## 🛠 Cách 3: Build trực tiếp trên máy

**Phù hợp**: bạn muốn dev/debug nhiều, sửa code rồi build lại nhanh.

### Cài 1 lần duy nhất:

1. **Python 3.10+**: https://www.python.org/downloads/
2. **Java JDK 17**: https://adoptium.net/temurin/releases/?version=17
3. **Android Studio** (kèm SDK): https://developer.android.com/studio
   - Mở Android Studio → SDK Manager → cài "Android SDK 34" + "Build-Tools 34.0.0"

### Set biến môi trường

**Windows (PowerShell)**:
```powershell
[Environment]::SetEnvironmentVariable("JAVA_HOME", "C:\Program Files\Eclipse Adoptium\jdk-17", "User")
[Environment]::SetEnvironmentVariable("ANDROID_HOME", "$env:LOCALAPPDATA\Android\Sdk", "User")
```

**macOS / Linux** (thêm vào `~/.bashrc` hoặc `~/.zshrc`):
```bash
export JAVA_HOME=/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home
export ANDROID_HOME=$HOME/Library/Android/sdk          # macOS
export ANDROID_HOME=$HOME/Android/Sdk                  # Linux
export PATH=$PATH:$ANDROID_HOME/platform-tools
```

### Build

**Windows**: double-click `build_apk.bat`

**macOS / Linux**:
```bash
bash build_apk.sh
```

Lần đầu mất 15-25 phút (Flet kéo Flutter + Gradle build). Lần sau ~2 phút.

**Output**: `build/apk/app-release.apk`

---

## 📱 Cài APK lên điện thoại

1. Copy file `app-release.apk` vào điện thoại (qua USB / Bluetooth / Drive / Zalo)
2. Mở file APK trên điện thoại
3. Lần đầu sẽ hỏi cho phép "cài đặt từ nguồn không xác định" — bấm Cài đặt → Cho phép → Tiếp tục
4. App **Quản lý LL47 e141** xuất hiện trên màn hình chính

---

## 🆘 Lỗi thường gặp

### "JAVA_HOME is not set"
Set như hướng dẫn trên. Quan trọng: trỏ đến **JDK 17**, không phải JRE 17.

### "Gradle build failed: SDK location not found"
Set `ANDROID_HOME`. Hoặc tạo file `local.properties` trong `build/flutter/android/`:
```
sdk.dir=/Users/you/Library/Android/sdk
```

### "Flutter SDK not found"
Flet sẽ tự kéo Flutter. Nếu mạng chậm có thể fail, thử lại hoặc cài thủ công:
```bash
git clone https://github.com/flutter/flutter.git -b stable ~/flutter
export PATH=$PATH:~/flutter/bin
```

### Build mất quá lâu
Lần đầu **bình thường** mất 15-25 phút. Nếu treo, ctrl+c rồi chạy lại — sẽ resume từ chỗ dừng.

### APK không cài được
- Kiểm tra Android phiên bản >= 5.0 (API 21)
- Tắt Google Play Protect tạm thời (Cài đặt → Play Store → Play Protect → tắt scan)

---

## 📦 Build IPA cho iOS

Chỉ làm được trên **macOS có Xcode**:

```bash
cd ll47_python
source .venv/bin/activate
flet build ipa --project ll47_e141 --org vn.mil.e141
```

Output: `build/ipa/`. Mở Xcode → Window → Organizer → Distribute App → TestFlight hoặc App Store.

Cần Apple Developer Account ($99/năm) để phát hành. Chi tiết xem `BUILD_GUIDE.md`.
