# LL47 backend — Node.js + Express + Socket.io + MongoDB

Backend thay thế Firebase cho app LL47. Gồm:

- **Auth (JWT)** — đăng nhập/đăng ký bằng số quân (`/auth/*`), thay Firebase Auth.
- **Doc-store** — kho document theo "path" kiểu Firestore (`/doc`, `/collection`, `/query`), lưu trong MongoDB.
- **Storage (GridFS)** — upload/tải ảnh & minh chứng (`/storage/*`), thay Firebase Storage.
- **Realtime (Socket.io)** — đẩy thay đổi tới client (chat, danh sách...).
- **FCM worker** — đọc hàng đợi `fcm_queue` rồi gửi push nền qua firebase-admin (tuỳ chọn).

## Chạy local

Cần Node 18+ và MongoDB. Nhanh nhất là Docker:

```bash
cd server
docker compose up --build
# backend: http://localhost:8080  — kiểm tra http://localhost:8080/health
```

Hoặc chạy trực tiếp (cần sẵn mongod ở localhost:27017):

```bash
cd server
cp .env.example .env      # rồi sửa giá trị nếu cần
npm install
npm start
```

## Deploy production

- **MongoDB**: tạo cluster free trên [MongoDB Atlas], lấy connection string.
- **Backend**: deploy lên [Render] bằng Blueprint `render.yaml` ở thư mục gốc repo
  (Render → New → Blueprint). Khi được hỏi, điền:
  - `MONGODB_URI` = connection string Atlas
  - `PUBLIC_URL` = URL Render của service (vd `https://ll47-backend.onrender.com`)
  - (tuỳ chọn) `FIREBASE_SERVICE_ACCOUNT` = JSON service account để bật push nền
- Sau khi có URL backend, đặt nó vào app: sửa `_DEFAULT_API_BASE` trong
  `app/firebase_config.py` (hoặc truyền biến `LL47_API_BASE` khi build trên Codemagic).

## Biến môi trường

Xem `.env.example`. Bắt buộc đổi `JWT_SECRET` khi chạy thật.

## Push nền (FCM/APNs)

Socket.io chỉ realtime khi app đang mở. Để thông báo khi app đóng vẫn cần FCM/APNs.
Tạo service account Firebase (Project settings → Service accounts → Generate key),
dán JSON vào `FIREBASE_SERVICE_ACCOUNT`. Nếu để trống, mọi thứ vẫn chạy, chỉ là
không có push nền.

[MongoDB Atlas]: https://www.mongodb.com/atlas/database
[Render]: https://render.com
