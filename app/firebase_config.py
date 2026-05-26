"""
LL47 e141 — Cấu hình kết nối backend.

ĐÃ CHUYỂN từ Firebase sang backend riêng (Node.js + Socket.io + MongoDB).
Toàn bộ Auth / dữ liệu / Storage / push đi qua 1 URL backend duy nhất:
    API_BASE_URL

Cách đổi URL backend:
  - Khi build app (mobile/desktop): SỬA hằng _DEFAULT_API_BASE bên dưới thành
    URL production của bạn (vd: https://ll47-backend.onrender.com).
  - Khi chạy dev: đặt biến môi trường LL47_API_BASE để ghi đè mà không sửa code.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# URL backend — ĐỔI dòng này thành URL production khi phát hành.
# ---------------------------------------------------------------------------
_DEFAULT_API_BASE = "http://27.71.20.168"

API_BASE_URL = os.environ.get("LL47_API_BASE", _DEFAULT_API_BASE).rstrip("/")

# Đường dẫn con của backend
AUTH_BASE = f"{API_BASE_URL}/auth"        # /auth/signup, /signin, /refresh, ...
DOC_BASE = f"{API_BASE_URL}/doc"          # /doc/<path>
COLLECTION_BASE = f"{API_BASE_URL}/collection"  # /collection/<path>
QUERY_BASE = f"{API_BASE_URL}/query"      # /query/<collection>
STORAGE_BASE = f"{API_BASE_URL}/storage"  # /storage/upload, /storage/file/<path>

# ---------------------------------------------------------------------------
# Map số quân -> email "ảo" dùng làm khoá đăng nhập (giữ nguyên như trước để
# không phải đổi dữ liệu tài khoản cũ). Email này KHÔNG gửi đi đâu cả.
# ---------------------------------------------------------------------------
EMAIL_DOMAIN = "ll47.local"


def username_to_email(username: str) -> str:
    """Map số quân -> email khoá đăng nhập. Ví dụ: 'e141-009' -> 'e141-009@ll47.local'."""
    u = (username or "").strip().lower().replace(" ", "")
    if "@" in u:
        return u
    return f"{u}@{EMAIL_DOMAIN}"


def email_to_username(email: str) -> str:
    """Lấy lại số quân từ email khoá đăng nhập."""
    if not email:
        return ""
    return email.split("@", 1)[0]
