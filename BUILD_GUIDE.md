# Hướng dẫn build APK + iOS chi tiết

## 1. Build APK Android

### Yêu cầu hệ thống
- **OS**: Windows / macOS / Linux đều được
- **Python**: 3.10 trở lên
- **Java**: JDK 17 (https://adoptium.net/)
- **Android SDK + Build Tools 34**: tải qua Android Studio hoặc cmdline-tools
- **Flutter SDK 3.24+**: Flet sẽ tự kéo về nếu chưa có
- **Disk**: ~5GB cho lần build đầu

### Bước 1: Cài Java + Android SDK

**Windows / macOS**: tải Android Studio (https://developer.android.com/studio)
→ chạy SDK Manager → cài "Android SDK Platform 34" + "Android SDK Build-Tools 34.0.0"
+ "Android SDK Command-line Tools".

**Linux** (server không GUI):
```bash
mkdir -p ~/android/sdk && cd ~/android/sdk
wget https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
unzip commandlinetools-linux-11076708_latest.zip
mkdir -p cmdline-tools/latest && mv cmdline-tools/* cmdline-tools/latest/
export ANDROID_SDK_ROOT=~/android/sdk
export PATH=$PATH:$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools
sdkmanager --licenses     # Nhấn y cho mọi license
sdkmanager "platforms;android-34" "build-tools;34.0.0" "platform-tools"
```

Set biến môi trường:
```bash
export JAVA_HOME=/path/to/jdk-17
export ANDROID_HOME=$ANDROID_SDK_ROOT
```

### Bước 2: Cài Flet

```bash
cd ll47_python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Bước 3: Build APK

```bash
flet build apk
```

Lần đầu Flet sẽ tải Flutter (~1GB) và compile, mất 10–20 phút.

**Output**: `build/apk/app-release.apk`

### Bước 4: Cài lên điện thoại

- Copy file `.apk` qua USB / Bluetooth / Drive
- Trên điện thoại: Cài đặt → Bảo mật → Cho phép cài đặt từ nguồn không xác định
- Bấm vào file APK → Cài

### Tuỳ chọn nâng cao

**Đổi icon** (icon mặc định là logo Flet):
- Đặt file `assets/icon.png` (1024×1024 PNG)
- Build lại

**Ký APK để phát hành Play Store**:
```bash
keytool -genkey -v -keystore ll47.keystore -alias ll47 \
        -keyalg RSA -keysize 2048 -validity 10000

flet build apk --android-signing-key-store ll47.keystore \
               --android-signing-key-alias ll47 \
               --android-signing-key-store-password XXX \
               --android-signing-key-password XXX
```

---

## 2. Build IPA iOS

### Yêu cầu hệ thống
- **OS**: bắt buộc **macOS** (iOS chỉ build được trên Mac)
- **Xcode**: 15+ (cài từ App Store)
- **Apple Developer Account**: $99/năm (https://developer.apple.com)
- **CocoaPods**: `sudo gem install cocoapods`
- **Python**: 3.10+
- **Flutter SDK**: Flet sẽ tự kéo

### Bước 1: Cài Xcode + tools

```bash
xcode-select --install                    # Cài Command Line Tools
sudo xcodebuild -license accept           # Chấp nhận license
sudo gem install cocoapods                # Cài CocoaPods
```

### Bước 2: Tạo signing identity trong Xcode

- Mở Xcode → Settings → Accounts
- Đăng nhập Apple ID có Developer membership
- Tải về Provisioning Profile cho bundle ID `vn.mil.e141.ll47`
  (đăng ký trên https://developer.apple.com/account/resources/identifiers)

### Bước 3: Build

```bash
cd ll47_python
source .venv/bin/activate
flet build ipa
```

Lần đầu sẽ mất 15–30 phút.

**Output**: `build/ipa/ll47_e141.ipa`

### Bước 4: Phân phối

**TestFlight** (khuyến nghị cho thử nghiệm nội bộ):
- Mở Xcode → Window → Organizer
- Drop file `.ipa` vào → Distribute App → TestFlight
- Mời tester qua email — họ cài qua app TestFlight

**App Store Connect** (phát hành chính thức):
- Tương tự nhưng chọn "App Store"
- Submit for Review (Apple duyệt 1–3 ngày)

**Cài trực tiếp lên iPhone của mình** (dev mode):
- Cắm iPhone qua cáp
- Mở Xcode → mở project ở `build/flutter/ios/Runner.xcworkspace`
- Chọn iPhone của bạn → Run

---

## 3. Troubleshooting

### Lỗi "Flutter not found"
```bash
flet build apk --flutter-path /path/to/flutter
# Hoặc cài Flutter: https://flutter.dev/docs/get-started/install
```

### Lỗi "Gradle build failed"
- Kiểm tra `JAVA_HOME` trỏ đúng JDK 17 (KHÔNG phải JDK 21)
- Xoá cache: `rm -rf ~/.gradle ~/.flutter build`
- Build lại

### Lỗi "Provisioning profile not found" (iOS)
- Vào https://developer.apple.com/account/resources/identifiers
- Tạo App ID với bundle ID `vn.mil.e141.ll47`
- Tạo Provisioning Profile cho App ID đó
- Tải về và double-click để Xcode tự cài

### App quá nặng
Flet bundle gồm Flutter engine (~30MB). Để giảm:
```bash
flet build apk --split-per-abi    # Tạo APK riêng cho từng kiến trúc CPU
```

---

## 4. Update app sau khi đã phát hành

Mỗi lần phát hành phiên bản mới:

1. Sửa `pyproject.toml`:
```toml
build_number = 2          # Tăng mỗi lần build
build_version = "1.0.1"   # Số phiên bản hiển thị
```

2. Build lại + upload lên TestFlight / Play Console.

---

## 5. So sánh với bản web HTML

| Mặt | Web HTML | Python (Flet) |
|---|---|---|
| Cài đặt | Mở URL là chạy | Tải APK + cài |
| Offline | Có (PWA) | Có (lưu JSON cục bộ) |
| Push notification | Cần FCM/Web Push | Cần FCM/APNs |
| Camera / GPS | Có (Web API) | Có (plugin Flet) |
| Update | Tức thì (đẩy file mới) | Phải build + cài lại / qua store |
| File size | ~360 KB | ~30–50 MB |
| Tốc độ phát triển | Nhanh (HTML+JS) | Chậm hơn (build + cài) |

App Python hợp lý khi cần: chạy offline ổn định hơn, có icon trên home screen,
truy cập sâu vào hệ điều hành (camera, contact, file system), hoặc khi muốn
phát hành qua Play Store / App Store.
