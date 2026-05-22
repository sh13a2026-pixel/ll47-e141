# Di trú LL47: Firebase → Node.js + Socket.io + MongoDB + Codemagic

Tài liệu mô tả việc chuyển backend của app từ **Firebase** sang **Node.js +
Express + Socket.io + MongoDB (GridFS)** và đóng gói app iOS/Android bằng
**Codemagic**.

## 1. Đổi những gì?

| Trước (Firebase) | Sau (tự host) |
|---|---|
| Firebase Authentication | Auth JWT trong `server/` (`/auth/*`) |
| Cloud Firestore | Doc-store trên MongoDB (`/doc`, `/collection`, `/query`) |
| Firebase Storage | GridFS trong MongoDB (`/storage/*`) |
| Listen Firestore (polling) | Socket.io realtime (tự fallback polling) |
| Cloud Function `processFCMQueue` | Worker trong `server/src/fcm.js` |
| Build APK qua GitHub Actions | Thêm Codemagic build APK/AAB + IPA |

Điểm mấu chốt: lớp client trong `app/` được giữ **nguyên tên file và chữ ký
hàm**, chỉ thay phần "ruột". Nhờ vậy **`main.py` (13k+ dòng) gần như không phải
sửa** — chỉ cần đặt đúng URL backend.

## 2. Kiến trúc mới

```
Flet app (main.py)
   │  (app/ giữ nguyên API)
   ├─ firebase_auth.py     ─┐
   ├─ firestore_client.py   │  HTTP REST + Socket.io
   ├─ firebase_storage.py   ├──────────────►  Backend Node (server/)
   ├─ fcm.py                │                    ├─ /auth   (JWT, bcrypt)
   └─ firebase_config.py  ──┘                    ├─ /doc /collection /query  ─► MongoDB (collection "documents")
        API_BASE_URL                             ├─ /storage  ─► GridFS
                                                 ├─ Socket.io  (realtime "change")
                                                 └─ FCM worker ─► FCM/APNs (push nền)
```

Doc-store lưu mọi document trong 1 collection MongoDB tên `documents`, theo
"path" kiểu Firestore (`app_data/units`, `users/<uid>`,
`chat_rooms/<rid>/messages/<mid>`, `v2_soldiers/e141/<id>`...). Quy tắc: `_parent`
= path bỏ segment cuối; `list_collection(path)` trả mọi doc có `_parent == path`.

## 3. Backend — chạy & deploy

### Chạy local (Docker)
```bash
cd server
docker compose up --build
# http://localhost:8080/health  -> {"ok":true}
```

### Deploy production
1. **MongoDB Atlas** (free): tạo cluster → lấy connection string `mongodb+srv://...`.
2. **Render**: Dashboard → New → Blueprint → chọn repo (đọc `render.yaml`).
   Điền `MONGODB_URI` (Atlas), `PUBLIC_URL` (URL Render), tuỳ chọn
   `FIREBASE_SERVICE_ACCOUNT`.
3. Lấy URL backend, ví dụ `https://ll47-backend.onrender.com`.

Chi tiết: xem `server/README.md`.

## 4. Trỏ app sang backend

Sửa 1 dòng trong `app/firebase_config.py`:
```python
_DEFAULT_API_BASE = "https://ll47-backend.onrender.com"   # URL backend của bạn
```
Khi dev có thể đặt biến môi trường `LL47_API_BASE` để ghi đè mà không sửa code.

`requirements.txt` đã thêm `python-socketio[client]` + `websocket-client` cho
realtime. Nếu thiếu, app **tự fallback sang polling** nên vẫn chạy.

## 5. Đóng gói app bằng Codemagic

File `codemagic.yaml` đã có 2 workflow:

- **android-flet** → `flet build apk` + `flet build aab` (chạy trên Linux).
- **ios-flet** → `flet build ipa` (chạy trên macOS, cần ký số).

Các bước trong Codemagic UI (làm 1 lần):
1. Integrations → kết nối GitHub, chọn repo này.
2. (iOS) Thêm App Store Connect API key, đặt tên integration trùng
   `LL47_ASC_API_KEY`; thêm certificate + provisioning profile cho bundle id
   `vn.mil.e141.ll47`.
3. (Android, nếu ký release) tạo Environment group `ll47_android` chứa keystore.
4. Đổi biến `LL47_API_BASE` trong `codemagic.yaml` thành URL backend.
5. Start build → tải APK/AAB/IPA ở mục **Artifacts** (hoặc tự lên TestFlight/email).

> GitHub Actions cũ (`.github/workflows/build-apk.yml`) vẫn dùng được để build
> APK nhanh; giữ lại hay xoá tuỳ bạn.

## 6. Push nền (FCM/APNs)

Socket.io chỉ realtime khi app **đang mở**. Để nhận thông báo khi app **đóng**
vẫn cần FCM (Android) / APNs (iOS). Backend worker `server/src/fcm.js` gửi push
qua `firebase-admin`:
- Tạo service account Firebase → dán JSON vào `FIREBASE_SERVICE_ACCOUNT`.
- Để trống ⇒ app vẫn chạy, chỉ không có push nền.

> Lưu ý (giống bản cũ): để lấy **device token thật** trên thiết bị, Flet cần một
> native plugin (`firebase_messaging`). Phần này nằm ngoài đợt di trú này; hiện
> code đọc token từ biến `FCM_TOKEN` như trước.

## 7. File Firebase cũ — không còn dùng

Có thể giữ để tham khảo hoặc xoá: `functions/` (Cloud Function),
`firebase.json`, `firestore.rules`, `storage.rules`. Logic của chúng đã được
thay bằng `server/`.

## 8. Đã kiểm thử

- `node --check` toàn bộ `server/src/*.js` — OK.
- `py_compile` toàn bộ `app/*.py` và `main.py` — OK (main.py không phải sửa).
- Kiểm thử client ↔ backend giả lập (auth, doc CRUD/merge/cascade/query, path
  "v2", chat subcollection, storage upload/delete, listen polling) — **22/22 PASS**.

Việc còn lại cần bạn làm thủ công: cài `npm install` trên máy/Render (registry bị
chặn trong môi trường tạo file này), tạo Atlas + Render, và cấu hình ký số iOS
trên Codemagic.
