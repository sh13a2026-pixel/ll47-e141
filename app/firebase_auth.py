"""
LL47 e141 — Auth client (gọi backend Node.js, thay Firebase Identity Toolkit).

GIỮ NGUYÊN toàn bộ API công khai để main.py không phải đổi:
    sign_up / sign_in_with_password / refresh_id_token /
    send_password_reset_email / update_password / get_account_info /
    login_with_username / signup_with_username /
    FirebaseAuthError / TokenCache / friendly_error
"""
from __future__ import annotations

import json
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import firebase_config as fc


class FirebaseAuthError(Exception):
    """Lỗi auth (sai mật khẩu, user không tồn tại, ...)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ============================================================================
# Keep-alive: ping server mỗi 10 phút để Render free tier không sleep
# ============================================================================

def _ping_server():
    """Ping /health endpoint để giữ server thức."""
    try:
        url = f"{fc.API_BASE_URL}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass  # Bỏ qua lỗi ping — không ảnh hưởng app


def start_keepalive():
    """Chạy background thread ping server mỗi 10 phút."""
    def _loop():
        while True:
            time.sleep(600)  # 10 phút
            _ping_server()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    # Ping ngay lần đầu để warm up server khi app khởi động
    threading.Thread(target=_ping_server, daemon=True).start()


# ============================================================================
# HTTP helper
# ============================================================================

def _post(path: str, body: dict, id_token: str | None = None, timeout: float = 15.0) -> dict:
    url = f"{fc.AUTH_BASE}/{path}"
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if id_token:
        headers["Authorization"] = f"Bearer {id_token}"
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8")
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        # 502/503/504 = server đang khởi động (Render free tier sleep)
        if e.code in (502, 503, 504):
            raise FirebaseAuthError(
                "SERVER_WAKING",
                f"Server đang khởi động lại (HTTP {e.code}). Vui lòng chờ..."
            ) from e
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = err.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        raise FirebaseAuthError(msg.split(":")[0].strip(), msg) from e
    except urllib.error.URLError as e:
        raise FirebaseAuthError("NETWORK", f"Không kết nối được máy chủ: {e}") from e


# ============================================================================
# Token cache (giữ nguyên — lưu idToken + refreshToken ra file)
# ============================================================================

class TokenCache:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save(self, creds: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# ============================================================================
# Auth API
# ============================================================================

def _normalize_creds(res: dict) -> dict:
    return {
        "idToken": res.get("idToken", ""),
        "refreshToken": res.get("refreshToken", ""),
        "localId": res.get("localId", ""),
        "email": res.get("email", ""),
        "expiresAt": int(time.time()) + int(res.get("expiresIn", "3600") or 3600),
    }


def sign_up(email: str, password: str) -> dict:
    res = _post("signup", {"email": email, "password": password})
    return _normalize_creds(res)


def sign_in_with_password(email: str, password: str) -> dict:
    res = _post("signin", {"email": email, "password": password})
    return _normalize_creds(res)


def refresh_id_token(refresh_token: str) -> dict:
    res = _post("refresh", {"refreshToken": refresh_token})
    return {
        "idToken": res.get("idToken", ""),
        "refreshToken": res.get("refreshToken", ""),
        "localId": res.get("localId", ""),
        "email": res.get("email", ""),
        "expiresAt": int(time.time()) + int(res.get("expiresIn", "3600") or 3600),
    }


def send_password_reset_email(email: str) -> None:
    # Backend chỉ trả ok (email nội bộ @ll47.local không gửi mail thật được).
    _post("reset", {"email": email})


def update_password(id_token: str, new_password: str) -> dict:
    res = _post("update-password", {"password": new_password}, id_token=id_token)
    return _normalize_creds(res)


def get_account_info(id_token: str) -> dict:
    res = _post("lookup", {"idToken": id_token})
    users = res.get("users", [])
    return users[0] if users else {}


# ============================================================================
# Tiện ích cho login bằng số quân
# ============================================================================

def login_with_username(username: str, password: str) -> dict:
    return sign_in_with_password(fc.username_to_email(username), password)


def signup_with_username(username: str, password: str) -> dict:
    return sign_up(fc.username_to_email(username), password)


# ============================================================================
# Friendly error messages (tiếng Việt)
# ============================================================================

ERROR_MESSAGES_VI: dict[str, str] = {
    "EMAIL_NOT_FOUND": "Số quân chưa được đăng ký.",
    "INVALID_PASSWORD": "Mật khẩu không đúng.",
    "INVALID_LOGIN_CREDENTIALS": "Số quân hoặc mật khẩu không đúng.",
    "USER_DISABLED": "Tài khoản đã bị khoá. Liên hệ chỉ huy.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Đăng nhập sai quá nhiều lần. Thử lại sau.",
    "EMAIL_EXISTS": "Số quân đã được đăng ký rồi.",
    "WEAK_PASSWORD": "Mật khẩu yếu (tối thiểu 6 ký tự).",
    "INVALID_EMAIL": "Số quân không hợp lệ.",
    "OPERATION_NOT_ALLOWED": "Phương thức đăng nhập chưa được bật.",
    "TOKEN_EXPIRED": "Phiên đăng nhập đã hết hạn. Đăng nhập lại.",
    "INVALID_ID_TOKEN": "Phiên đăng nhập không hợp lệ. Đăng nhập lại.",
    "INVALID_REFRESH_TOKEN": "Phiên đăng nhập đã hết hạn. Đăng nhập lại.",
    "NETWORK": "Mất kết nối. Kiểm tra mạng.",
    "SERVER_WAKING": "Server đang khởi động, vui lòng chờ và thử lại...",
}


def friendly_error(err: Exception) -> str:
    if isinstance(err, FirebaseAuthError):
        for key, vi in ERROR_MESSAGES_VI.items():
            if key in (err.code or "") or key in (err.message or ""):
                return vi
        return err.message or str(err)
    return str(err)
