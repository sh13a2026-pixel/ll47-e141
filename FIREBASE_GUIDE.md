# Hướng dẫn cài Firebase cho LL47 e141

App đã được tích hợp Firebase Auth + Firestore + Storage. File này hướng dẫn các bước cần làm trong **Firebase Console** để app chạy thật.

Project Firebase đang dùng: **`sh13a-d6020`** (lấy từ `app/firebase_config.py`).

---

## 1. Bật Authentication

1. Vào https://console.firebase.google.com → chọn project `sh13a-d6020`.
2. Sidebar trái → **Build → Authentication → Get started**.
3. Tab **Sign-in method** → chọn **Email/Password** → bật **Enable** → Save.
4. (Khuyến nghị) Tab **Settings → User actions** → tắt "Enable create (sign-up)" sau khi đã đăng ký xong toàn bộ chiến sĩ, để chống user lạ tự đăng ký.

App đăng nhập bằng số quân (vd `e141-009`); bên trong code map sang email ảo `e141-009@ll47.local`. Bạn sẽ thấy email dạng này trong tab **Users**.

---

## 2. Tạo Firestore Database

1. Sidebar → **Build → Firestore Database → Create database**.
2. Chọn **Start in production mode** (rules đóng) → Next.
3. Chọn **location** gần nhất (vd `asia-southeast1` cho VN) → Enable.

### Deploy Security Rules

Cách 1 — dán thủ công:
- Tab **Rules** trong Firestore → copy nội dung file `firestore.rules` ở repo này → Publish.

Cách 2 — dùng Firebase CLI:
```bash
npm install -g firebase-tools
firebase login
firebase use sh13a-d6020
firebase deploy --only firestore:rules
```

### Tạo admin đầu tiên

Sau khi 1 chỉ huy đã đăng ký xong qua app:

1. Vào **Authentication → Users**, copy `User UID`.
2. Vào **Firestore Database → Data**, tạo collection `users`, document id = UID vừa copy, thêm field:
   - `isAdmin` (bool) = `true`
   - `username` (string) = số quân (vd `e141-001`)
3. Save.

Từ giờ user này được phép tạo/xoá phòng chat và sau này quản trị toàn hệ thống.

---

## 3. Bật Storage

1. Sidebar → **Build → Storage → Get started**.
2. Chọn **Start in production mode** → Next.
3. Chọn cùng location với Firestore → Done.

### Deploy Storage Rules

Tab **Rules** → copy nội dung `storage.rules` → Publish.

Hoặc CLI:
```bash
firebase deploy --only storage
```

---

## 4. (Tuỳ chọn) Bật Cloud Messaging (push notification)

Push notification thật cần native plugin Flutter / Cloud Function. Để bật cơ bản:

1. Sidebar → **Engage → Cloud Messaging**. Mặc định FCM đã bật khi project tạo.
2. Tab **Cloud Messaging → Project settings (bánh răng) → Cloud Messaging**:
   - Lấy **Server key** (legacy) hoặc tạo Service Account key (HTTP v1).
3. Để app gửi push: viết 1 Cloud Function lắng nghe collection `fcm_queue` (xem `app/fcm.py`) và gọi FCM HTTP v1.

App hiện tại đã có:
- Helper `app/fcm.py` để client ghi token thiết bị vào `users/{uid}/fcm_tokens/{token}`.
- Helper `queue_notification()` để client tạo yêu cầu gửi push.

Phần thiếu: lấy được FCM token thật từ thiết bị (cần Flet plugin / native bridge — Flet 0.25 chưa expose).

---

## 5. Chạy app

Sau khi đã làm xong 1-3 ở trên:

```bash
# Cài deps
pip install -r requirements.txt

# Chạy thử desktop
flet run

# Hoặc thiết bị Android
flet run --android
```

Lần đầu: bấm **"Chưa có tài khoản? Đăng ký"** → nhập số quân + mật khẩu (>= 6 ký tự).

Lần sau: app tự đăng nhập lại từ token cache (file `auth_creds.json` trong app data dir).

---

## 6. Cấu trúc dữ liệu trong Firestore

```
users/{uid}                         <- profile + isAdmin
users/{uid}/fcm_tokens/{token}      <- token push của từng thiết bị

app_data/units                      <- cây đơn vị (single doc, field "value")
app_data/soldiers                   <- danh sách quân nhân
app_data/userProfile                <- profile mặc định (sẽ migrate sang users/{uid})
app_data/notifs                     <- thông báo (50 cái gần nhất)
app_data/shifts                     <- ca trực
app_data/reports                    <- báo cáo
app_data/f47Campaigns               <- chiến dịch F47 + submissions
app_data/chat_rooms                 <- danh sách phòng chat (cache, dùng cho UI)
app_data/activity                   <- nhật ký hoạt động

chat_rooms/{roomId}                 <- doc phòng (lastMessage, members, ...)
chat_rooms/{roomId}/messages/{id}   <- tin nhắn (collection thật, hỗ trợ realtime)

fcm_queue/{id}                      <- yêu cầu gửi push (Cloud Function consume)
```

**Lưu ý**: cấu trúc dùng `app_data/{key}` với 1 field `value` chứa toàn bộ dict/list là cách map đơn giản nhất từ store cũ sang Firestore. Khi có thời gian nên chuyển sang collection thật cho từng loại data (vd `soldiers/{id}`, `shifts/{id}`) để query / phân quyền tốt hơn.

---

## 7. Bảo mật khi build APK

1. Trước khi build APK phát cho lính, kiểm tra:
   - Đã tắt sign-up tự do trong Authentication.
   - Security Rules đã publish.
   - Tài khoản admin đã được tạo và verified.
2. App lưu `idToken` + `refreshToken` trong file local — token này sống ~1h, refreshToken cho phép cấp idToken mới. Nếu mất điện thoại, vào Firebase Console → Authentication → Users → "Disable account" để vô hiệu hoá.
3. Cân nhắc thêm App Check (https://firebase.google.com/docs/app-check) để chỉ APK chính thống mới gọi được Firebase.

---

## 8. Khắc phục lỗi thường gặp

| Lỗi | Nguyên nhân | Cách xử lý |
|---|---|---|
| `OPERATION_NOT_ALLOWED` | Chưa bật Email/Password trong Auth | Bước 1 |
| `PERMISSION_DENIED` khi đọc Firestore | Rules chặn | Bước 2 — deploy rules |
| `Failed to load resource: 403` upload Storage | Storage rules chặn / chưa bật | Bước 3 |
| `EMAIL_EXISTS` khi đăng ký | Số quân đã đăng ký | Đăng nhập thay vì đăng ký |
| Mất kết nối liên tục | App vẫn chạy local-only, set/get vẫn lưu cache, sẽ retry khi có mạng | (đã tự xử lý trong `store.flush_pending`) |
