# Quản lý LL47 e141 — Python / Flet

Ứng dụng Python tái tạo bản web HTML, build được **APK Android**, **IPA iOS**, web, và desktop từ cùng một mã nguồn.

## Cấu trúc

```
ll47_python/
├── main.py                 # App chính (login, nav, view, modules)
├── app/
│   ├── __init__.py
│   └── store.py            # Lưu trữ JSON cục bộ + seed data
├── assets/                 # Icon, ảnh kèm theo
├── requirements.txt        # flet>=0.25
├── pyproject.toml          # Cấu hình build (Android + iOS bundle ID, permission)
├── README.md               # File này
└── BUILD_GUIDE.md          # Hướng dẫn build APK + IPA chi tiết
```

## Cài đặt

```bash
# Khuyến nghị Python 3.10+
python -m venv .venv
source .venv/bin/activate         # Linux/macOS
.venv\Scripts\activate            # Windows

pip install -r requirements.txt
```

## Chạy thử (dev)

```bash
flet run                          # Mở cửa sổ desktop
flet run --web                    # Mở trong trình duyệt
flet run --android                # Mở trên thiết bị Android (qua Flet Companion app)
flet run --ios                    # Mở trên thiết bị iOS (qua Flet Companion app)
```

> **Mẹo**: Cài app **Flet** trên điện thoại từ Play Store / App Store. Khi chạy
> `flet run --android` hoặc `--ios`, app sẽ load thẳng vào điện thoại để test
> không cần build APK / IPA.

## Build cho phát hành

### APK Android

```bash
# Yêu cầu: Java JDK 17, Android SDK + cmdline-tools, Flutter (Flet sẽ tự kéo)
flet build apk --project ll47_e141 --org vn.mil.e141
```

Output: `build/apk/app-release.apk` — copy vào điện thoại Android, bật
"Cài đặt từ nguồn không xác định" rồi cài.

### IPA iOS

```bash
# Yêu cầu: macOS + Xcode + Apple Developer Account
flet build ipa --project ll47_e141 --org vn.mil.e141
```

Output: `build/ipa/`. Mở Xcode, ký bằng cert Developer của bạn rồi
distribute qua TestFlight hoặc App Store Connect.

### Web

```bash
flet build web
# Deploy thư mục build/web/ lên bất kỳ static host (Vercel, Netlify, GitHub Pages...)
```

### Desktop

```bash
flet build windows         # Windows EXE
flet build macos           # macOS app bundle
flet build linux           # Linux binary
```

## Tính năng

| Module | Mô tả |
|---|---|
| 🔐 Đăng nhập | Form số quân + mật khẩu, ghi nhớ user |
| 🏠 Trang chủ | Lời chào theo giờ, 4 stat card click được, danh sách thông báo |
| 💬 Tin nhắn | Danh sách phòng + mở chat detail (placeholder cho Firebase) |
| ⚡ Tiện ích | Lưới 14 module |
| 📅 Lịch trực | CRUD ca trực, hôm nay / sắp tới / đã hoàn thành |
| 📋 Báo cáo | Tạo / Duyệt / Từ chối |
| 🛡 Lực lượng 47 | List chiến dịch, countdown thật, progress bar |
| 👥 Quản lý quân nhân | Cây đơn vị + chỉ huy mỗi cấp |
| 👤 Cá nhân | Hồ sơ + 7 menu (lịch trực, thành tích, F47, bảo mật, ...) |
| 🔔 Thông báo | Badge đếm, click dẫn tới module liên quan |

## Lưu trữ

Data lưu vào file JSON trong:

- **Windows**: `%LOCALAPPDATA%\LL47e141\ll47_data.json`
- **macOS**: `~/Library/Application Support/LL47e141/ll47_data.json`
- **Linux**: `~/.ll47e141/ll47_data.json`
- **Android / iOS**: thư mục app storage do hệ điều hành quản lý

## Mở rộng

App được viết theo kiểu controller (`class App`) với mỗi view là 1 method.
Để thêm module mới, sửa `view_utilities()` (thêm cell trong grid) +
`view_module()` (thêm nhánh `if key == "newkey"`) + viết hàm `module_xxx()`
trả về `ft.Control`.

Chi tiết build: xem [BUILD_GUIDE.md](BUILD_GUIDE.md)
