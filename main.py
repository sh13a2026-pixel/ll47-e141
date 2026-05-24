"""
LL47 e141 — Ứng dụng Quản lý Lực lượng 47 Trung đoàn 141.
Viết bằng Flet (Flutter for Python). Build được Android APK + iOS IPA + Web + Desktop.

Chạy thử:    flet run
Build APK:   flet build apk
Build iOS:   flet build ipa  (cần macOS + Xcode)
Build Web:   flet build web
"""
from __future__ import annotations

import sys
import os

# Ghi log ra file CHỈ khi đặt biến môi trường LL47_DEBUG=1 (tránh nuốt output
# và tránh phá giao tiếp stdio của flet run desktop).
if os.environ.get("LL47_DEBUG") == "1":
    try:
        _log_file = open("python_logs.txt", "w", buffering=1, encoding="utf-8")
        sys.stdout = _log_file
        sys.stderr = _log_file
        print("Python logs started...")
        print(f"CWD: {os.getcwd()}")
    except Exception:
        pass

import base64
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import flet as ft

from app import store
from app import firebase_auth, firebase_config
from app.firebase_auth import FirebaseAuthError, friendly_error
from app.firestore_client import FirestoreClient
from app import firebase_storage as fb_storage
from app import fcm

# ============================================================
# ===== TRẠNG THÁI AUTH TOÀN CỤC                          =====
# ============================================================

# Lưu credentials sau khi đăng nhập (để FirestoreClient và Storage dùng).
AUTH_STATE: dict = {
    "idToken": None,
    "refreshToken": None,
    "uid": None,
    "email": None,
    "username": None,
    "expiresAt": 0,
}

# 1 instance FirestoreClient duy nhất, set token sau khi login.
FS = FirestoreClient()

# Cache file để nhớ login giữa các lần mở app
_TOKEN_CACHE = firebase_auth.TokenCache(store.DATA_DIR / "auth_creds.json")


def _set_auth(creds: dict, username: str | None = None) -> None:
    AUTH_STATE.update(creds)
    AUTH_STATE["uid"] = creds.get("localId") or AUTH_STATE.get("uid")
    if username:
        AUTH_STATE["username"] = username
    else:
        AUTH_STATE["username"] = firebase_config.email_to_username(creds.get("email", ""))
    FS.set_token(creds["idToken"])
    store.STORE.bind_firestore(FS, creds["localId"])
    _TOKEN_CACHE.save({**creds, "username": AUTH_STATE["username"]})


def _clear_auth() -> None:
    AUTH_STATE.update({"idToken": None, "refreshToken": None, "uid": None,
                       "email": None, "username": None, "expiresAt": 0})
    FS.set_token("")
    store.STORE.unbind_firestore()
    _TOKEN_CACHE.clear()


def _maybe_refresh_token() -> None:
    """Tự động refresh idToken nếu sắp hết hạn (~5 phút trước)."""
    if not AUTH_STATE.get("refreshToken"):
        return
    if int(time.time()) + 300 < AUTH_STATE.get("expiresAt", 0):
        return
    try:
        new = firebase_auth.refresh_id_token(AUTH_STATE["refreshToken"])
        AUTH_STATE.update(new)
        FS.set_token(new["idToken"])
        _TOKEN_CACHE.save({**AUTH_STATE})
    except Exception:
        pass


def _username_key(u: str) -> str:
    """Chuẩn hoá số quân để so khớp (vd: e141-001, e141001, E141 001 → e141001)."""
    return (u or "").strip().lower().replace(" ", "").replace("-", "")


def _is_super_admin_username(u: str) -> bool:
    """Tài khoản quản trị hệ thống — số quân e141001 hoặc 001 (mọi cách gõ)."""
    k = _username_key(u)
    return k in {"e141001", "001"}


async def _register_fcm_token_async(page: ft.Page, uid: str) -> None:
    """Đọc FCM token do Flutter firebase_init.dart ghi vào client_storage,
    rồi đăng ký lên server để server biết đường gửi push nền.
    Chỉ chạy được khi page đã sẵn sàng (sau show_app)."""
    try:
        from app import fcm as _fcm
        token = await page.client_storage.get_async("fcm_token")
        if token and uid:
            platform = str(getattr(page, "platform", "") or "").lower()
            if not platform:
                platform = "android"
            _fcm.register_token(FS, uid, token, platform=platform)
    except Exception:
        pass


def _looks_like_firebase_uid(uid: str | None) -> bool:
    if not uid or not isinstance(uid, str):
        return False
    u = uid.strip()
    return len(u) >= 18 and u.replace("_", "").isalnum()


def _hydrate_profile_after_login(creds: dict, login_username: str,
                                  _pre_purge_cache: dict | None = None) -> None:
    """Ghép profile từ Firestore users/{uid}, áp quyền admin 001, cập nhật soldiers.

    _pre_purge_cache: profile đã backup trước khi purge_keys — dùng làm fallback
    khi server trả về rỗng/lỗi (Render đang ngủ, mạng yếu).
    """
    uid = creds.get("localId") or ""
    uname_raw = (login_username or "").strip() or (AUTH_STATE.get("username") or "")

    profile_remote: dict = {}
    try:
        doc = FS.get_doc(f"users/{uid}")
        if doc:
            profile_remote = {k: v for k, v in doc.items() if not str(k).startswith("_")}
        # doc is None → tài khoản mới chưa có profile doc hoặc mạng gián đoạn
        # → dùng profile rỗng, KHÔNG auto-lock (tránh khoá oan do network lỗi)
    except Exception:
        profile_remote = {}

    # Fallback: nếu server trả về rỗng (mạng chậm / Render đang ngủ), dùng cache local
    # (backup trước purge hoặc lấy từ store hiện tại) để tránh mất tên/cấp bậc đã lưu.
    cached_profile = _pre_purge_cache or store.get("userProfile", {}) or {}
    for _field in ("name", "rank", "role", "unitId", "unitName", "phone", "photoUrl"):
        if not profile_remote.get(_field) and cached_profile.get(_field):
            profile_remote[_field] = cached_profile[_field]

    base = store.seed_user_profile()
    merged = {**base, **profile_remote}
    merged["id"] = uid  # đảm bảo luôn có id để show_app fetch đúng
    merged["username"] = uname_raw if uname_raw else merged.get("username", "")
    merged["email"] = creds.get("email") or merged.get("email", "")

    if _is_super_admin_username(uname_raw):
        merged["name"] = "admin"
        merged["rank"] = ""          # admin hệ thống không có quân hàm hiển thị
        merged["isAdmin"] = True
        merged["adminLevel"] = 5
        merged.setdefault("role", merged.get("role") or "Quản trị hệ thống")
        merged.setdefault("unitName", merged.get("unitName") or "Trung đoàn 141")

    soldiers = store.get("soldiers", store.seed_soldiers)
    idx = next(
        (
            i
            for i, s in enumerate(soldiers)
            if s.get("id") == uid
            or _username_key(str(s.get("username", ""))) == _username_key(uname_raw)
        ),
        None,
    )

    # Fallback: nếu server không trả về adminLevel/isAdmin đầy đủ,
    # dùng soldiers cache để giữ quyền không bị mất.
    if idx is not None:
        sol = soldiers[idx]
        existing_level = int(sol.get("adminLevel") or 0)
        current_level = int(merged.get("adminLevel") or 0)
        # Lấy mức cao hơn giữa profile server và soldiers cache
        if existing_level > current_level:
            merged["adminLevel"] = existing_level
        if sol.get("isAdmin") and not merged.get("isAdmin"):
            merged["isAdmin"] = True

    store.set_value("userProfile", merged)
    new_level = int(merged.get("adminLevel") or 1)
    existing_level = int((soldiers[idx].get("adminLevel") or 0) if idx is not None else 0)
    row = {
        "id": uid,
        "unitId": merged.get("unitId") or "",
        "name": merged.get("name") or "",
        "rank": merged.get("rank") or "",
        "role": merged.get("role") or "",
        "username": merged.get("username") or uname_raw,
        "phone": merged.get("phone") or "",
        "accountStatus": str(merged.get("accountStatus") or "active"),
        "isAdmin": bool(merged.get("isAdmin")) or (existing_level >= 3),
        "adminLevel": max(new_level, existing_level),
        "photoUrl": str(merged.get("photoUrl") or ""),
    }
    if idx is None:
        soldiers.append(row)
    else:
        soldiers[idx] = {**soldiers[idx], **row}
    store.set_value("soldiers", soldiers)

    try:
        # Chỉ ghi các trường không rỗng để tránh xoá tên/rank đang có trên server
        # khi GET trước đó thất bại (Render free tier đang ngủ).
        _update: dict = {
            "username": merged.get("username", ""),
            "email": merged.get("email", ""),
            "isAdmin": bool(merged.get("isAdmin")),
            "adminLevel": int(merged.get("adminLevel") or 1),
            "lastLoginAt": store.now_ms(),
        }
        for _f in ("name", "rank", "role", "unitId", "unitName", "phone", "photoUrl"):
            _v = merged.get(_f) or ""
            if _v:  # chỉ ghi khi có giá trị — tránh xoá dữ liệu cũ
                _update[_f] = _v
        FS.set_doc(f"users/{uid}", _update)
        # KHÔNG ghi accountStatus ở đây — tránh ghi đè trạng thái duyệt
    except Exception:
        pass

    # FCM token được đăng ký sau khi app load xong qua _register_fcm_token_async()


# ============================================================
# ===== HẰNG SỐ MÀU SẮC + STYLE                          =====
# ============================================================

CURRENT_THEME_MODE = ft.ThemeMode.LIGHT

GREEN_DARK = "#1a4731"
GREEN_MID = "#2e7d52"
GREEN_LIGHT = "#d4edda"
RED = "#e24b4a"
AMBER = "#ba7517"
BLUE = "#185fa5"
PURPLE = "#534ab7"
GOLD = "#f0b429"
TEXT = "#111111"
TEXT_MUTED = "#666666"
BORDER = "#e0e0e0"
BG = "#ffffff"
BG2 = "#f5f5f5"

def update_theme_colors(theme_mode: ft.ThemeMode) -> None:
    global GREEN_DARK, GREEN_MID, GREEN_LIGHT, RED, AMBER, BLUE, PURPLE, GOLD, TEXT, TEXT_MUTED, BORDER, BG, BG2, CURRENT_THEME_MODE
    CURRENT_THEME_MODE = theme_mode
    if theme_mode == ft.ThemeMode.DARK:
        GREEN_DARK = "#0b2015"
        GREEN_MID = "#1d5235"
        GREEN_LIGHT = "#152c1e"
        RED = "#ff6b6b"
        AMBER = "#ffaa3b"
        BLUE = "#4ea8ff"
        PURPLE = "#9b92ff"
        GOLD = "#ffd666"
        TEXT = "#e0e0e0"
        TEXT_MUTED = "#aaaaaa"
        BORDER = "#262626"
        BG = "#121212"
        BG2 = "#181818"
    else:
        GREEN_DARK = "#1a4731"
        GREEN_MID = "#2e7d52"
        GREEN_LIGHT = "#d4edda"
        RED = "#e24b4a"
        AMBER = "#ba7517"
        BLUE = "#185fa5"
        PURPLE = "#534ab7"
        GOLD = "#f0b429"
        TEXT = "#111111"
        TEXT_MUTED = "#666666"
        BORDER = "#e0e0e0"
        BG = "#ffffff"
        BG2 = "#f5f5f5"

def _load_theme_pref() -> ft.ThemeMode:
    try:
        p = store.DATA_DIR / "theme_pref.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            mode_str = data.get("theme_mode", "light")
            return ft.ThemeMode.DARK if mode_str == "dark" else ft.ThemeMode.LIGHT
    except Exception:
        pass
    return ft.ThemeMode.LIGHT

def _save_theme_pref(mode: ft.ThemeMode) -> None:
    try:
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        p = store.DATA_DIR / "theme_pref.json"
        mode_str = "dark" if mode == ft.ThemeMode.DARK else "light"
        p.write_text(json.dumps({"theme_mode": mode_str}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

# ── Dialog helpers (Flet >= 0.23 deprecates _dlg) ──────────────────
def _show_dialog(page: ft.Page, dlg: ft.AlertDialog) -> None:
    """Hiển thị dialog theo API mới (overlay)."""
    try:
        if dlg not in page.overlay:
            page.overlay.append(dlg)
    except Exception:
        pass
    dlg.open = True
    page.update()


def _close_dialog(page: ft.Page) -> None:
    """Đóng tất cả dialog đang mở trong overlay."""
    for ctrl in list(page.overlay):
        if isinstance(ctrl, ft.AlertDialog) and getattr(ctrl, "open", False):
            ctrl.open = False
    page.update()


def open_image_viewer(page: ft.Page, url: str, title: str = "") -> None:
    """Hiển thị ảnh toàn màn hình trong app, hỗ trợ zoom.
    Bấm nút X hoặc bấm ngoài ảnh để đóng."""
    overlay_ref: list = [None]

    def _close(e=None):
        ctrl = overlay_ref[0]
        if ctrl is not None:
            try:
                page.overlay.remove(ctrl)
                page.update()
            except Exception:
                pass

    # Kích thước màn hình
    pw = (getattr(page, "width", None) or
          getattr(getattr(page, "window", None), "width", None) or 400)
    ph = (getattr(page, "height", None) or
          getattr(getattr(page, "window", None), "height", None) or 700)
    pw = int(pw or 400)
    ph = int(ph or 700)

    # Ảnh vừa khung, dành phần trên cho nút đóng
    img_w = pw - 8
    img_h = ph - 60

    img = ft.Image(src=url, width=img_w, height=img_h, fit=ft.ImageFit.CONTAIN)

    # InteractiveViewer cho phép pinch-zoom (Flet ≥ 0.22)
    try:
        viewer = ft.InteractiveViewer(
            min_scale=0.5,
            max_scale=8.0,
            clip_behavior=ft.ClipBehavior.NONE,
            content=img,
            width=img_w,
            height=img_h,
        )
    except Exception:
        viewer = img

    ctrl = ft.Container(
        width=pw,
        height=ph,
        bgcolor="#EE000000",
        content=ft.Stack([
            # Nền — bấm để đóng
            ft.GestureDetector(
                on_tap=_close,
                content=ft.Container(width=pw, height=ph, bgcolor="transparent"),
            ),
            # Ảnh căn giữa
            ft.Container(
                content=viewer,
                width=pw,
                height=ph,
                alignment=ft.alignment.center,
                padding=ft.padding.only(top=46, bottom=4, left=4, right=4),
            ),
            # Nút đóng góc trên phải
            ft.Container(
                content=ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    icon_color=ft.Colors.WHITE,
                    icon_size=26,
                    on_click=_close,
                    tooltip="Đóng",
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.with_opacity(0.45, ft.Colors.BLACK),
                    ),
                ),
                alignment=ft.alignment.top_right,
                padding=ft.padding.only(top=6, right=6),
            ),
            # Tiêu đề (nếu có)
            *(
                [ft.Container(
                    content=ft.Text(title, color=ft.Colors.WHITE70, size=12,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    alignment=ft.alignment.top_center,
                    padding=ft.padding.only(top=14),
                    width=pw - 80,
                )]
                if title else []
            ),
        ]),
    )

    overlay_ref[0] = ctrl
    page.overlay.append(ctrl)
    page.update()
# ───────────────────────────────────────────────────────────────────────────

# Phiên bản hiển thị (đăng nhập + cài đặt)
APP_VERSION = "2.6.0"
LOGIN_CREDIT_LINE = "by Cường Cáa"

_REMEMBER_LOGIN_FILE = store.DATA_DIR / "remember_login.json"


def _remember_login_save(username: str, password: str) -> None:
    """Lưu mật khẩu đã nhớ (base64 — chỉ tránh lộ nhìn thấy ngay trong file)."""
    try:
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "remember_password": True,
            "username": username.strip(),
            "password_b64": base64.b64encode(password.encode("utf-8")).decode("ascii"),
        }
        _REMEMBER_LOGIN_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _remember_login_load() -> dict | None:
    try:
        if not _REMEMBER_LOGIN_FILE.exists():
            return None
        return json.loads(_REMEMBER_LOGIN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _remember_login_clear() -> None:
    try:
        _REMEMBER_LOGIN_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _refresh_biometric_button_state(btn: ft.Control) -> None:
    """Bật nút đăng nhập sinh trắc học khi đã có mật khẩu lưu (nhớ đăng nhập)."""
    try:
        d = _remember_login_load()
        ok = bool(d and d.get("remember_password") and d.get("password_b64"))
        btn.disabled = not ok
    except Exception:
        btn.disabled = True

# ============================================================
# ===== HELPER                                          =====
# ============================================================

def fmt_date(ts_ms: int) -> str:
    d = datetime.fromtimestamp(ts_ms / 1000)
    return f"{d.day:02d}/{d.month:02d}/{d.year}"


def fmt_time(ts_ms: int) -> str:
    d = datetime.fromtimestamp(ts_ms / 1000)
    return f"{d.hour:02d}:{d.minute:02d}"


def fmt_dt(ts_ms: int) -> str:
    return f"{fmt_time(ts_ms)} {fmt_date(ts_ms)}"


def time_ago(ts_ms: int) -> str:
    d = store.now_ms() - ts_ms
    if d < 60_000:
        return "Vừa xong"
    if d < 3600_000:
        return f"{d // 60_000} phút trước"
    if d < 86400_000:
        return f"{d // 3600_000} giờ trước"
    return fmt_date(ts_ms)


def initials(name: str, n: int = 2) -> str:
    parts = name.strip().split()
    return "".join(p[0] for p in parts[-n:]).upper() if parts else "?"


def greeting() -> str:
    h = datetime.now().hour
    if h < 5:
        return "Chúc ngủ ngon,"
    if h < 11:
        return "Chào buổi sáng,"
    if h < 13:
        return "Chào buổi trưa,"
    if h < 18:
        return "Chào buổi chiều,"
    return "Chào buổi tối,"


def dow_label() -> str:
    d = datetime.now()
    dows = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật"]
    return f"{dows[d.weekday()]}, {d.day:02d}/{d.month:02d}/{d.year}"


def make_hoverable_card(container: ft.Container) -> ft.Container:
    """Bổ sung hiệu ứng Hover Scale & Bóng mờ động cho Container."""
    container.animate = ft.animation.Animation(250, ft.AnimationCurve.DECELERATE)
    container.animate_scale = ft.animation.Animation(250, ft.AnimationCurve.DECELERATE)
    orig_shadow = container.shadow
    
    def on_hover(e):
        if e.data == "true":
            container.scale = 1.03
            container.shadow = ft.BoxShadow(
                blur_radius=15,
                spread_radius=1,
                color=ft.Colors.with_opacity(0.15, "#000000"),
                offset=ft.Offset(0, 6),
            )
        else:
            container.scale = 1.0
            container.shadow = orig_shadow
        try:
            container.update()
        except Exception:
            pass
            
    container.on_hover = on_hover
    return container


# ============================================================
# ===== APP CONTROLLER                                  =====
# ============================================================

class App:
    """Controller chính. Quản lý nav, render từng màn, lưu trạng thái."""

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.tab = "home"
        # Lọc trong tab chat: all | dm | group
        self.chat_filter = "all"
        # Tab con trong F47: campaigns | leaderboard
        self.f47_view = "campaigns"
        # Tab con trong Quản lý quân nhân: tree | accounts
        self.units_subtab = "tree"
        # Danh bạ: đang xem hồ sơ thành viên (overlay), giữ tab hiện tại
        self.overlay_soldier_id: str | None = None
        self.overlay_from_tab: str | None = None
        self.toast_snackbar: ft.SnackBar | None = None
        # cache "current view" để rebuild khi data đổi
        self.body = ft.Container(expand=True, bgcolor=BG2, clip_behavior=ft.ClipBehavior.HARD_EDGE)
        # Persistent nav/header boxes — chỉ update content, không rebuild frame
        self._header_box = ft.Container()
        self._nav_box = ft.Container()
        self._layout_mode: str | None = None  # "desktop" | "mobile"
        # Realtime sync: kéo dữ liệu từ Firestore mỗi 30s rồi refresh tab hiện tại
        self._realtime_stop = {"stop": False}
        self._start_realtime_sync()

    def _start_realtime_sync(self) -> None:
        """Background thread sync app_data từ Firestore mỗi 30s."""
        if not store.STORE.is_bound():
            return

        stop_event = threading.Event()
        self._realtime_stop["event"] = stop_event

        def loop():
            while not stop_event.wait(timeout=30.0):
                try:
                    store.STORE.sync_from_firestore()
                    store.STORE.flush_pending()
                    store.refresh_soldiers_from_users()
                except Exception:
                    pass

        threading.Thread(target=loop, daemon=True).start()

    def manual_refresh(self) -> None:
        """User chủ động refresh: kéo data + rebuild tab hiện tại."""
        try:
            store.STORE.sync_from_firestore()
        except Exception:
            pass
        oid = getattr(self, "overlay_soldier_id", None)
        if oid:
            try:
                self.body.content = self.view_member_profile(str(oid))
                self.refresh()
            except Exception:
                pass
            return
        try:
            self.set_tab(self.tab)
        except Exception:
            pass

    def stop_realtime_sync(self) -> None:
        self._realtime_stop["stop"] = True
        ev = self._realtime_stop.get("event")
        if ev is not None:
            ev.set()

    # ---- TOAST ----
    def toast(self, msg: str) -> None:
        sb = ft.SnackBar(
            content=ft.Text(msg, color=ft.Colors.WHITE, size=13, weight=ft.FontWeight.W_600),
            bgcolor=GREEN_DARK,
            duration=2200,
        )
        self.page.overlay.append(sb)
        sb.open = True
        self.page.update()

    def export_data_to_csv(self, filename_prefix: str, headers: list[str], rows: list[list]) -> None:
        import csv
        import datetime
        from pathlib import Path
        try:
            downloads_path = Path.home() / "Downloads"
            if not downloads_path.exists():
                downloads_path.mkdir(parents=True, exist_ok=True)
            now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{filename_prefix}_{now_str}.csv"
            file_path = downloads_path / filename
            with open(file_path, mode="w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)
            self.toast(f"📥 Đã xuất báo cáo tại: {file_path}")
        except Exception as e:
            self.toast(f"❌ Lỗi xuất báo cáo: {e}")

    # ---- HEADER ----
    def header(self, title: str, sub: str | None = None, show_back: bool = False, bg_color: str | None = None, actions: list[ft.Control] | None = None) -> ft.Container:
        notifs = self._my_notifs()
        unread = sum(1 for n in notifs if not n.get("read"))
        bell_stack = ft.Stack(
            [
                ft.Icon(ft.Icons.NOTIFICATIONS_OUTLINED, color=ft.Colors.WHITE, size=24),
                ft.Container(
                    content=ft.Text(str(min(unread, 99)) + ("+" if unread > 99 else ""),
                                    color=ft.Colors.WHITE, size=9, weight=ft.FontWeight.BOLD),
                    bgcolor=RED, border_radius=10, padding=ft.padding.symmetric(horizontal=4),
                    width=18, height=14, alignment=ft.alignment.center,
                    top=-2, right=-4,
                ) if unread else ft.Container(),
            ],
            width=28, height=28,
        )
        title_col = ft.Column(
            [ft.Text(title, color=ft.Colors.WHITE, size=16,
                     weight=ft.FontWeight.BOLD, no_wrap=True,
                     overflow=ft.TextOverflow.ELLIPSIS, max_lines=1)]
            + ([ft.Text(sub, color=ft.Colors.WHITE54, size=11,
                        no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                        max_lines=1)] if sub else []),
            spacing=2, tight=True,
        )
        children: list[ft.Control] = []
        if show_back:
            children.append(ft.IconButton(ft.Icons.ARROW_BACK, icon_color=ft.Colors.WHITE,
                                          on_click=lambda e: self.go_back()))
        children.append(ft.Container(content=title_col, expand=True, padding=ft.padding.only(left=4)))
        if actions:
            children.extend(actions)
        children.append(ft.IconButton(content=bell_stack, on_click=lambda e: self.open_notifs()))
        children.append(self._header_overflow_menu())
        base_color = bg_color or str(GREEN_DARK)
        return ft.Container(
            content=ft.Row(children, alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=ft.Colors.with_opacity(0.85, base_color),
            padding=ft.padding.symmetric(horizontal=12, vertical=8),
            height=56,
        )

    def _header_overflow_menu(self) -> ft.Control:
        return ft.PopupMenuButton(
            icon=ft.Icons.MORE_HORIZ,
            icon_color=ft.Colors.WHITE,
            icon_size=22,
            tooltip="Thêm",
            items=self._overflow_menu_items(),
        )

    def _overflow_menu_items(self) -> list:
        """Menu ... theo tab hoặc theo màn hồ sơ thành viên."""
        if getattr(self, "overlay_soldier_id", None):
            return self._overflow_items_member_detail()
        tab = self.tab
        if tab == "home":
            return [
                ft.PopupMenuItem(
                    text="🔄 Làm mới dữ liệu",
                    on_click=self._ov_refresh_toast,
                ),
                ft.PopupMenuItem(text="🔔 Thông báo", on_click=lambda e: self.open_notifs()),
                ft.PopupMenuItem(text="⚙️ Menu ứng dụng", on_click=lambda e: self.open_app_menu()),
            ]
        if tab == "chat":
            return [
                ft.PopupMenuItem(
                    text="✓ Đánh dấu đã đọc tất cả",
                    on_click=self._mark_all_chats_read_action,
                ),
                ft.PopupMenuItem(
                    text="👥 Tạo nhóm chat",
                    on_click=lambda e: self.open_create_group_dialog(),
                ),
                ft.PopupMenuItem(text="⚙️ Menu ứng dụng", on_click=lambda e: self.open_app_menu()),
            ]
        if tab == "util":
            return [
                ft.PopupMenuItem(
                    text="🔄 Làm mới dữ liệu",
                    on_click=self._ov_refresh_toast,
                ),
                ft.PopupMenuItem(text="⚙️ Menu ứng dụng", on_click=lambda e: self.open_app_menu()),
            ]
        if tab == "contacts":
            return [
                ft.PopupMenuItem(
                    text="🔄 Làm mới danh bạ",
                    on_click=self._ov_refresh_toast,
                ),
                ft.PopupMenuItem(text="⚙️ Menu ứng dụng", on_click=lambda e: self.open_app_menu()),
            ]
        if tab == "profile":
            return [
                ft.PopupMenuItem(text="🖼 Đổi ảnh đại diện", on_click=lambda e: self.open_change_avatar()),
                ft.PopupMenuItem(text="👤 Thông tin cá nhân", on_click=lambda e: self.open_profile_info()),
                ft.PopupMenuItem(text="🔒 Đổi mật khẩu", on_click=lambda e: self.open_change_password()),
                ft.PopupMenuItem(text="⚙️ Cài đặt ứng dụng", on_click=lambda e: self.open_app_settings()),
                ft.PopupMenuItem(text="⏻ Đăng xuất", on_click=lambda e: self.confirm_logout()),
            ]
        return [
            ft.PopupMenuItem(text="⚙️ Menu ứng dụng", on_click=lambda e: self.open_app_menu()),
        ]

    def _overflow_items_member_detail(self) -> list:
        sid = str(getattr(self, "overlay_soldier_id", "") or "")
        soldiers = store.get("soldiers", store.seed_soldiers)
        s = next((x for x in soldiers if str(x.get("id")) == sid), None)
        if not s:
            return [ft.PopupMenuItem(text="← Quay lại", on_click=lambda e: self.go_back())]

        items: list = [
            ft.PopupMenuItem(text="💬 Nhắn tin", on_click=lambda e, _s=s: self._open_dm(_s)),
        ]
        if self._is_admin():
            uname = str(s.get("username") or "")
            if not _is_super_admin_username(uname):
                items.insert(
                    0,
                    ft.PopupMenuItem(
                        text="🛡 Phân quyền",
                        on_click=lambda e, _sid=sid: self.open_units_assign_role_dialog(
                            _sid, return_to_member_view=True,
                        ),
                    ),
                )
                items.insert(
                    1,
                    ft.PopupMenuItem(
                        text="✏️ Sửa hồ sơ",
                        on_click=lambda e, _sid=sid: self.open_member_profile_edit_dialog(_sid),
                    ),
                )
                items.append(
                    ft.PopupMenuItem(
                        text="🗑️ Xóa khỏi danh sách",
                        on_click=lambda e, _sid=sid: self.confirm_delete_soldier(_sid),
                    ),
                )
        items.append(ft.PopupMenuItem(text="⚙️ Menu ứng dụng", on_click=lambda e: self.open_app_menu()))
        return items

    def _ov_refresh_toast(self, _e=None) -> None:
        self.manual_refresh()
        self.toast("Đã làm mới")

    def _mark_all_chats_read_action(self, _e=None) -> None:
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or store.get(
            "userProfile", store.seed_user_profile,
        ).get("username", "")
        try:
            store.mark_all_chats_read(my_uid)
            self.toast("Đã đánh dấu đọc tất cả")
            if self.tab == "chat":
                self.body.content = self.view_chat()
            self.refresh()
        except Exception:
            self.toast("Không cập nhật được trạng thái đọc")

    def go_back(self) -> None:
        if getattr(self, "overlay_soldier_id", None):
            self.overlay_soldier_id = None
            src = self.overlay_from_tab or "contacts"
            self.overlay_from_tab = None
            if src == "profile":
                self.body.content = self.view_profile()
            else:
                self.body.content = self.view_contacts()
            self.refresh()
            return
        # Đang ở sub-module (F47, CTĐ-CTCT, schedule...) → quay về Tiện ích
        if getattr(self, "current_module", None):
            self.current_module = None
            self.set_tab("util")
            return
        self.set_tab(self.tab)

    def mark_all_notifs_read(self) -> None:
        """Đánh dấu toàn bộ thông báo đã đọc để tắt badge nhanh."""
        notifs = self._my_notifs()
        changed = False
        for x in notifs:
            if not x.get("read"):
                x["read"] = True
                changed = True
        if changed:
            store.set_value("notifs", notifs)

    def open_notifs(self) -> None:
        # Bấm chuông/“Xem tất cả” => tắt badge bằng cách đánh dấu toàn bộ đã đọc
        self.mark_all_notifs_read()
        self.body.content = self.view_all_notifs()
        self.refresh()

    def open_app_menu(self) -> None:
        page = self.page

        def close_dialog():
            try:
                _dlg.open = False
            except Exception:
                pass
            try:
                page.update()
            except Exception:
                pass

        def open_and_close(fn):
            def handler(e=None):
                close_dialog()
                fn()
            return handler

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("⚙️ Menu ứng dụng", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.ElevatedButton(
                            "⚙️ Cài đặt ứng dụng",
                            on_click=open_and_close(lambda: self.open_app_settings()),
                            bgcolor=GREEN_MID,
                            color=ft.Colors.WHITE,
                        ),
                        ft.ElevatedButton(
                            "🔒 Đổi mật khẩu",
                            on_click=open_and_close(lambda: self.open_change_password()),
                            bgcolor="#fff4f4",
                            color=RED,
                        ),
                        ft.ElevatedButton(
                            "⏻ Đăng xuất",
                            on_click=open_and_close(lambda: self.confirm_logout()),
                            bgcolor="#fff4f4",
                            color=RED,
                        ),
                    ],
                    tight=True,
                    spacing=10,
                ),
                width=340,
            ),
            actions=[
                ft.TextButton("Đóng", on_click=lambda e: close_dialog()),
            ],
        )
        _show_dialog(self.page, _dlg)

    # ---- BOTTOM NAV ----
    def bottom_nav(self) -> ft.Container:
        items = [
            ("home", ft.Icons.HOME_FILLED, "Trang chủ"),
            ("chat", ft.Icons.CHAT_BUBBLE_OUTLINE, "Tin nhắn"),
            ("util", ft.Icons.GRID_VIEW, "Tiện ích"),
            ("contacts", ft.Icons.PEOPLE_OUTLINE, "Danh bạ"),
            ("profile", ft.Icons.PERSON_OUTLINE, "Cá nhân"),
        ]
        my_uid_nav = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or store.get(
            "userProfile", store.seed_user_profile,
        ).get("username", "")
        raw_nav = store.get("chat_rooms", store.seed_chat_rooms)
        total_unread = sum(store.chat_unread_for_user(r, my_uid_nav) for r in raw_nav)
        cols = []
        for key, icon, lbl in items:
            active = self.tab == key
            icon_ctrl: ft.Control = ft.Icon(
                icon, color=GREEN_DARK if active else TEXT_MUTED,
                size=22 if active else 20,
            )
            if key == "chat" and total_unread > 0:
                badge_txt = str(total_unread) if total_unread < 100 else "99+"
                icon_ctrl = ft.Stack(
                    [
                        ft.Container(
                            content=ft.Icon(
                                icon, color=GREEN_DARK if active else TEXT_MUTED,
                                size=24 if active else 22,
                            ),
                            width=32,
                            height=28,
                            alignment=ft.alignment.center,
                        ),
                        ft.Container(
                            content=ft.Text(
                                badge_txt, size=10, color=ft.Colors.WHITE,
                                weight=ft.FontWeight.BOLD,
                            ),
                            bgcolor=RED,
                            border_radius=12,
                            padding=ft.padding.symmetric(horizontal=6, vertical=2),
                            alignment=ft.alignment.center,
                            right=0,
                            top=-2,
                        ),
                    ],
                    width=36,
                    height=30,
                    clip_behavior=ft.ClipBehavior.NONE,
                )
            cols.append(
                ft.Container(
                    content=ft.Column(
                        [
                            icon_ctrl,
                            ft.Text(lbl,
                                    color=GREEN_DARK if active else TEXT_MUTED,
                                    size=10,
                                    weight=ft.FontWeight.BOLD if active else ft.FontWeight.W_500),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=3, tight=True,
                    ),
                    expand=True, alignment=ft.alignment.center,
                    padding=ft.padding.symmetric(vertical=8),
                    on_click=lambda e, k=key: self.set_tab(k),
                    ink=True,
                )
            )
        return ft.Container(
            content=ft.Row(cols, alignment=ft.MainAxisAlignment.SPACE_EVENLY),
            bgcolor=ft.Colors.with_opacity(0.85, str(BG)),
            border=ft.border.only(top=ft.BorderSide(1, BORDER)),
            height=64,
        )

    # ---- TAB SWITCHER ----
    def set_tab(self, tab: str) -> None:
        # Dọn listener chat trong background (tránh sio.disconnect() block UI thread)
        prev_stop = getattr(self, "_chat_listener_stop", None)
        self._chat_listener_stop = None
        if callable(prev_stop):
            threading.Thread(
                target=lambda fn=prev_stop: (lambda: (fn(),))(),
                daemon=True,
            ).start()
        self.overlay_soldier_id = None
        # Reset module mode khi bấm tab bottom nav
        self.current_module = None
        self.overlay_from_tab = None
        self.tab = tab
        if tab == "home":
            self.body.content = self.view_home()
        elif tab == "chat":
            self.body.content = self.view_chat()
        elif tab == "util":
            self.body.content = self.view_utilities()
        elif tab == "contacts":
            self.body.content = self.view_contacts()
        elif tab == "profile":
            self.body.content = self.view_profile()
        self.refresh()

    def sidebar_nav(self) -> ft.Container:
        """Icon rail rộng 128px kiểu Zalo Desktop (gấp đôi)."""
        items = [
            ("home", ft.Icons.HOME_FILLED, "Trang chủ"),
            ("chat", ft.Icons.CHAT_BUBBLE_OUTLINE, "Tin nhắn"),
            ("util", ft.Icons.GRID_VIEW, "Tiện ích"),
            ("contacts", ft.Icons.PEOPLE_OUTLINE, "Danh bạ"),
            ("profile", ft.Icons.PERSON_OUTLINE, "Cá nhân"),
        ]
        my_uid_nav = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or store.get(
            "userProfile", store.seed_user_profile,
        ).get("username", "")
        raw_nav = store.get("chat_rooms", store.seed_chat_rooms)
        total_unread = sum(store.chat_unread_for_user(r, my_uid_nav) for r in raw_nav)

        prof = store.get("userProfile", store.seed_user_profile)
        user_name = prof.get("name", "Người dùng")
        user_role = prof.get("role", "Thành viên")

        # ---- Build icon rail items ----
        assets_dir = Path(__file__).parent / "assets"
        has_logo = assets_dir.exists() and (assets_dir / "logo.png").exists()

        rail_items = []
        for key, icon, lbl in items:
            active = self.tab == key
            icon_ctrl = ft.Icon(
                icon,
                color=ft.Colors.WHITE if active else ft.Colors.WHITE54,
                size=36,
            )
            if key == "chat" and total_unread > 0:
                badge_txt = str(total_unread) if total_unread < 100 else "99+"
                icon_ctrl = ft.Stack(
                    [
                        ft.Icon(icon, color=ft.Colors.WHITE if active else ft.Colors.WHITE54, size=36),
                        ft.Container(
                            content=ft.Text(badge_txt, size=9, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                            bgcolor=RED,
                            border_radius=10,
                            padding=ft.padding.symmetric(horizontal=4, vertical=1),
                            right=0,
                            top=0,
                        ),
                    ],
                    width=44,
                    height=44,
                )

            rail_items.append(
                ft.Container(
                    content=ft.Column(
                        [
                            icon_ctrl,
                            ft.Text(
                                lbl,
                                color=ft.Colors.WHITE if active else ft.Colors.WHITE38,
                                size=12,
                                text_align=ft.TextAlign.CENTER,
                                weight=ft.FontWeight.BOLD if active else ft.FontWeight.W_400,
                                max_lines=1,
                                overflow=ft.TextOverflow.CLIP,
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=4,
                        tight=True,
                    ),
                    alignment=ft.alignment.center,
                    padding=ft.padding.symmetric(vertical=12),
                    border_radius=12,
                    bgcolor="rgba(255,255,255,0.12)" if active else ft.Colors.TRANSPARENT,
                    on_click=lambda e, k=key: self.set_tab(k),
                    ink=True,
                    width=114,
                    tooltip=lbl,
                )
            )

        # Logo icon ở đỉnh
        logo_widget = (
            ft.Image(src="assets/logo.png", width=72, height=72, fit=ft.ImageFit.COVER)
            if has_logo
            else ft.Icon(ft.Icons.SHIELD, color=ft.Colors.WHITE, size=52)
        )
        rail_logo = ft.Container(
            content=logo_widget,
            bgcolor="rgba(255,255,255,0.15)",
            width=80,
            height=80,
            border_radius=40,
            alignment=ft.alignment.center,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        # Avatar + nút logout ở đáy
        rail_avatar = ft.Container(
            content=ft.Column(
                [
                    ft.Container(
                        content=ft.Text(
                            initials(user_name),
                            color=GREEN_DARK,
                            size=16,
                            weight=ft.FontWeight.BOLD,
                        ),
                        bgcolor=ft.Colors.WHITE,
                        width=48,
                        height=48,
                        border_radius=24,
                        alignment=ft.alignment.center,
                        tooltip=f"{user_name}\n{user_role}",
                        on_click=lambda e: self.set_tab("profile"),
                        ink=True,
                    ),
                    ft.Container(
                        content=ft.Icon(ft.Icons.LOGOUT, color=ft.Colors.WHITE54, size=22),
                        tooltip="Đăng xuất",
                        on_click=lambda e: self.confirm_logout(),
                        ink=True,
                        border_radius=10,
                        padding=6,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
                tight=True,
            ),
            padding=ft.padding.symmetric(vertical=14),
        )

        return ft.Container(
            content=ft.Column(
                [
                    # Logo đỉnh
                    ft.Container(
                        content=rail_logo,
                        padding=ft.padding.symmetric(vertical=18),
                        alignment=ft.alignment.center,
                        border=ft.border.only(bottom=ft.BorderSide(1, "rgba(255,255,255,0.08)")),
                    ),
                    # Nav icons
                    ft.Container(
                        content=ft.Column(rail_items, spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                        expand=True,
                        padding=ft.padding.symmetric(vertical=10),
                        alignment=ft.alignment.top_center,
                    ),
                    # Avatar + logout
                    ft.Container(
                        content=rail_avatar,
                        border=ft.border.only(top=ft.BorderSide(1, "rgba(255,255,255,0.08)")),
                        alignment=ft.alignment.center,
                    ),
                ],
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=128,
            bgcolor=GREEN_DARK,
        )

    def _detect_layout(self) -> str:
        import os
        is_android = (os.environ.get("ANDROID_ROOT") is not None
                      or os.environ.get("ANDROID_DATA") is not None)
        return "mobile" if ((self.page.width or 0) < 800 or is_android) else "desktop"

    def _apply_nav(self, mode: str) -> None:
        """Cập nhật _nav_box in-place theo mode hiện tại."""
        if mode == "desktop":
            nav = self.sidebar_nav()
            self._nav_box.content = nav.content
            self._nav_box.bgcolor = nav.bgcolor
            self._nav_box.border = nav.border
            self._nav_box.width = nav.width
            self._nav_box.height = None
            self._nav_box.expand = False
        else:
            nav = self.bottom_nav()
            self._nav_box.content = nav.content
            self._nav_box.bgcolor = nav.bgcolor
            self._nav_box.border = nav.border
            self._nav_box.height = nav.height
            self._nav_box.width = None
            self._nav_box.expand = False

    def refresh(self) -> None:
        self.page.floating_action_button = None
        mode = self._detect_layout()

        if mode != self._layout_mode:
            # Lần đầu hoặc resize vượt breakpoint → rebuild frame structure
            self._layout_mode = mode
            if mode == "desktop":
                self.frame.controls = [
                    ft.Row(
                        [
                            self._nav_box,
                            ft.Column(
                                [
                                    self._header_box,
                                    # self.body đã có expand=True + clip_behavior — dùng thẳng
                                    self.body,
                                ],
                                spacing=0,
                                expand=True,
                            ),
                        ],
                        spacing=0,
                        expand=True,
                    )
                ]
            else:
                self.frame.controls = [
                    ft.Column(
                        [
                            # SafeArea chỉ bảo vệ top (status bar / notch)
                            ft.SafeArea(
                                content=self._header_box,
                                bottom=False, left=False, right=False,
                            ),
                            # self.body đã có expand=True + clip_behavior — dùng thẳng
                            self.body,
                            # Nav bar ở dưới, SafeArea bảo vệ bottom (home indicator)
                            ft.SafeArea(
                                content=self._nav_box,
                                top=False, left=False, right=False,
                            ),
                        ],
                        spacing=0,
                        expand=True,
                    )
                ]

        # Cập nhật header và nav in-place (không rebuild toàn bộ frame)
        self._header_box.content = self.header_for_tab()
        self._apply_nav(mode)
        self.page.update()

    def open_module_info(self, mod_id: str):
        info_data = {
            "f47": {
                "name": "Lực lượng 47",
                "func": "Theo dõi, chia sẻ, và lan toả các bài viết tích cực, báo cáo bài viết xấu độc trên không gian mạng.",
                "perms": "Tất cả thành viên có thể chia sẻ bài. Quản trị viên (Ban Chỉ huy, CQ Chính trị) có thể phát động chiến dịch và kiểm duyệt.",
                "rules": "Các bài viết phải có đường link hợp lệ. Hình ảnh đính kèm không vi phạm quy định bảo mật."
            },
            "ctdctct": {
                "name": "CTĐ-CTCT",
                "func": "Quản lý tiến độ chấm điểm thi đua, hoạt động sinh hoạt, và theo dõi tư tưởng quân nhân.",
                "perms": "Chỉ huy cấp Đại đội trở lên có thể nhập điểm. Cơ quan Chính trị tổng hợp và phê duyệt.",
                "rules": "Cập nhật dữ liệu hàng tuần. Mọi biến động tư tưởng phải báo cáo kịp thời."
            },
            "schedule": {
                "name": "Lịch gác - Trực ban",
                "func": "Lên danh sách và theo dõi lịch gác, trực ban của các đơn vị trực thuộc.",
                "perms": "Cơ quan Tham mưu giao lịch cho đơn vị. Chỉ huy đơn vị (Tiểu đoàn/Đại đội) phân công chi tiết đến từng quân nhân.",
                "rules": "Cán bộ, chiến sĩ không tự ý đổi gác. Mọi thay đổi phải báo cáo và được phê duyệt trên hệ thống."
            },
            "guests": {
                "name": "Thăm - Tiếp khách",
                "func": "Đăng ký và quản lý thân nhân lên thăm quân nhân tại đơn vị.",
                "perms": "Quân nhân tự đăng ký hoặc nhờ Chỉ huy đăng ký. Chỉ huy Đại đội và Trực ban phê duyệt.",
                "rules": "Đăng ký trước khi khách đến. Khách đến phải mang theo giấy tờ tuỳ thân và tuân thủ quy định."
            },
            "units": {
                "name": "Quản lý quân nhân",
                "func": "Quản lý danh sách, chức vụ, và phân quyền cho toàn bộ quân nhân trong hệ thống.",
                "perms": "Chỉ có Admin cấp 4, 5 (Chỉ huy Trung đoàn, Trợ lý chức năng) mới có quyền sửa đổi và xoá hồ sơ.",
                "rules": "Đảm bảo tính chính xác của chức danh để hệ thống tự động phân quyền đúng cấp bậc."
            },
            "exams": {
                "name": "Thi - Kiểm tra nhận thức",
                "func": "Tổ chức các đợt thi trắc nghiệm, kiểm tra nhận thức chính trị, quân sự.",
                "perms": "Cơ quan Chính trị / Tham mưu tạo đề thi. Quân nhân tham gia làm bài. Quản trị viên xem điểm tổng hợp.",
                "rules": "Mỗi đợt thi có thời hạn cụ thể. Không thoát ứng dụng trong lúc làm bài thi."
            },
            "hygiene": {
                "name": "Nội vụ vệ sinh",
                "func": "Kiểm tra, chấm điểm và đánh giá tình trạng nội vụ, vệ sinh của các bộ phận, đơn vị.",
                "perms": "Cán bộ kiểm tra thực hiện chấm điểm. Chỉ huy các cấp theo dõi và đôn đốc.",
                "rules": "Hình ảnh minh chứng phải rõ nét, đánh giá trung thực, khách quan."
            },
            "hcqs": {
                "name": "Hành chính - Quân sự",
                "func": "Quản lý công văn, giấy tờ, tiến độ xử lý công việc hành chính tại đơn vị.",
                "perms": "Cơ quan Tham mưu / Ban Hành chính cập nhật. Chỉ huy theo dõi tiến độ phê duyệt.",
                "rules": "Cập nhật kịp thời trạng thái văn bản để các đơn vị liên quan nắm bắt."
            },
            "pttd": {
                "name": "Phong trào thi đua",
                "func": "Theo dõi các phong trào thi đua quyết thắng, đánh giá và xếp loại thi đua.",
                "perms": "Cơ quan Chính trị phát động. Các đơn vị báo cáo thành tích. Hội đồng thi đua chấm điểm.",
                "rules": "Báo cáo thành tích phải đi kèm minh chứng cụ thể. Tuyệt đối không gian lận thành tích."
            }
        }
        
        data = info_data.get(mod_id)
        if not data:
            return
            
        page = self.page
        def close_dlg(_):
            _close_dialog(self.page)
            
        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"ℹ️ {data['name']}", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text("Mục đích & Chức năng", weight=ft.FontWeight.BOLD, size=13),
                        ft.Text(data["func"], size=12, color=TEXT_MUTED),
                        ft.Container(height=5),
                        ft.Text("Phân quyền", weight=ft.FontWeight.BOLD, size=13),
                        ft.Text(data["perms"], size=12, color=TEXT_MUTED),
                        ft.Container(height=5),
                        ft.Text("Quy định & Sử dụng", weight=ft.FontWeight.BOLD, size=13),
                        ft.Text(data["rules"], size=12, color=TEXT_MUTED),
                    ],
                    tight=True,
                ),
                width=350,
            ),
            actions=[ft.TextButton("Đã hiểu", on_click=close_dlg)],
        )
        _show_dialog(self.page, _dlg)

    def header_for_tab(self) -> ft.Control:
        oid = getattr(self, "overlay_soldier_id", None)
        if oid:
            soldiers = store.get("soldiers", store.seed_soldiers)
            s = next((x for x in soldiers if str(x.get("id")) == str(oid)), None)
            title = "Thông tin thành viên"
            sub = None
            return self.header(title, sub or None, show_back=True)

        # Đang ở sub-module (F47, CTĐ-CTCT, ...) → header hiện tên module + back arrow
        cur_mod = getattr(self, "current_module", None)
        if cur_mod:
            module_titles = {
                "f47": "🛡 Lực lượng 47",
                "ctdctct": "🎖 CTĐ-CTCT",
                "schedule": "📅 Lịch gác - Trực Ban",
                "guests": "🤝 Tiếp khách",
                "units": "👥 Quản lý quân nhân",
                "exams": "📝 Thi - Kiểm tra nhận thức",
                "hygiene": "🧹 Nội vụ vệ sinh",
                "hcqs": "⚔️ Hành chính - Quân sự",
                "pttd": "🚩 Phong trào thi đua",
            }
            mod_title = module_titles.get(cur_mod, cur_mod.title())
            # Lấy theme color: ưu tiên TASK_DOMAIN_CONFIG, rồi schedule, rồi mặc định
            if cur_mod == "schedule":
                theme_color = self.SCHEDULE_THEME
            elif hasattr(self, "TASK_DOMAIN_CONFIG") and cur_mod in self.TASK_DOMAIN_CONFIG:
                theme_color = self.TASK_DOMAIN_CONFIG[cur_mod].get("theme_color", GREEN_DARK)
            else:
                theme_color = GREEN_DARK
                
            info_btn = ft.IconButton(
                ft.Icons.INFO_OUTLINE, 
                icon_color=ft.Colors.WHITE, 
                tooltip="Thông tin tiện ích",
                on_click=lambda e, _mod=cur_mod: self.open_module_info(_mod)
            )
            return self.header(mod_title, None, show_back=True, bg_color=theme_color, actions=[info_btn])

        titles = {
            "home": ("Trang chủ", None),
            "chat": ("Tin nhắn", None),
            "util": ("Tiện ích", None),
            "contacts": ("Danh bạ", None),
            "profile": ("Cá nhân", None),
        }
        t, s = titles.get(self.tab, ("Quản lý LL47 e141", None))
        return self.header(t, s, show_back=False)

    # ============================================================
    # ===== VIEW: TRANG CHỦ                                   =====
    # ============================================================

    def view_home(self) -> ft.Control:
        profile = store.get("userProfile", store.seed_user_profile)
        soldiers = store.get("soldiers", store.seed_soldiers)
        reports = store.get("reports", store.seed_reports)
        notifs = self._my_notifs()[:4]
        f47 = store.get("f47Campaigns", store.seed_f47)
        live_f47 = sum(1 for c in f47 if c.get("status") == "live"
                       and c.get("deadline", 0) > store.now_ms())

        uname = str(profile.get("username") or "")
        _disp_name = (profile.get("name") or "").strip()
        _is_sys_admin = (
            _is_super_admin_username(uname)
            or _disp_name.lower() == "admin"
            or bool(profile.get("isAdmin"))
            or int(profile.get("adminLevel") or 0) >= 5
        )
        if _is_sys_admin:
            greet_main = "đ.c Admin 🎖"
        else:
            rank = (profile.get("rank") or "").strip()
            name = _disp_name or "..."
            rank_prefix = f"{rank} " if rank else ""
            greet_main = f"đ.c {rank_prefix}{name} 🫡"

        hero = ft.Container(
            content=ft.Column(
                [
                    ft.Text(greeting(), color=ft.Colors.WHITE70, size=13),
                    ft.Text(greet_main, color=ft.Colors.WHITE,
                            size=20, weight=ft.FontWeight.BOLD),
                    ft.Text(f"{dow_label()}   {profile.get('unitName') or ''}",
                            color=ft.Colors.WHITE54, size=11),
                ],
                spacing=2, tight=True,
            ),
            gradient=ft.LinearGradient(
                begin=ft.alignment.top_left, end=ft.alignment.bottom_right,
                colors=[GREEN_DARK, GREEN_MID],
            ),
            padding=ft.padding.symmetric(horizontal=16, vertical=18),
        )

        def stat_card(label: str, value: str, sub: str, color: str, icon: str,
                      on_click: Callable | None = None) -> ft.Container:
            return ft.Container(
                content=ft.Column(
                    [
                        ft.Text(f"{icon} {label}", color=TEXT_MUTED, size=11),
                        ft.Text(value, color=color, size=22, weight=ft.FontWeight.BOLD),
                        ft.Text(sub, color=TEXT_MUTED, size=10),
                    ],
                    spacing=2, tight=True,
                ),
                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
                padding=12, expand=True,
                on_click=lambda e: on_click() if on_click else None,
                ink=True,
            )

        # ===== Quân số theo đơn vị của mình + picker chọn cấp =====
        my_uid = str(profile.get("id") or AUTH_STATE.get("localId") or "")
        my_unit_id = profile.get("unitId") or ""
        units_tree = store.get("units", store.seed_units)

        # Build parent_map + name_map để xác định subtree
        _parent_map: dict[str, str | None] = {}
        _name_map: dict[str, str] = {}
        _node_by_id: dict[str, dict] = {}
        def _walk_um(n, p=None):
            if isinstance(n, dict):
                nid = n.get("id")
                if nid:
                    _parent_map[nid] = p
                    _name_map[nid] = n.get("name") or ""
                    _node_by_id[nid] = n
                for c in n.get("children") or []:
                    _walk_um(c, nid)
        _walk_um(units_tree)

        def _descendants_of(uid: str) -> set[str]:
            out = {uid}
            stack = [uid]
            while stack:
                cur = stack.pop()
                for cid, par in _parent_map.items():
                    if par == cur and cid not in out:
                        out.add(cid); stack.append(cid)
            return out

        # Phạm vi đơn vị mặc định: đơn vị của tôi (subtree)
        if not hasattr(self, "_home_scope_unit"):
            # Lấy đơn vị cấp 1 chứa user làm scope mặc định
            cur = my_unit_id
            while cur and _parent_map.get(cur) and _parent_map[cur] != "root":
                cur = _parent_map[cur]
            self._home_scope_unit = cur or my_unit_id or "root"
        scope_unit = self._home_scope_unit

        scope_ids = _descendants_of(scope_unit) if scope_unit and scope_unit != "root" \
                    else set(_parent_map.keys())
        # Loại super admin (e141001) khỏi đếm quân số vì đó là tài khoản hệ thống.
        # Admin cấp thấp hơn (cán bộ đơn vị) vẫn là quân nhân thật → được tính.
        scope_soldiers = [s for s in soldiers
                          if not _is_super_admin_username(str(s.get("username") or ""))
                          and (s.get("unitId") in scope_ids)]
        
        # Calculate active presence/absence
        total_count = len(scope_soldiers)
        present_count = sum(1 for s in scope_soldiers if (s.get("presence_status") or "Trực") == "Trực")
        
        absent_ra_ngoai = sum(1 for s in scope_soldiers if s.get("presence_status") == "Ra ngoài")
        absent_di_phep = sum(1 for s in scope_soldiers if s.get("presence_status") == "Đi phép")
        absent_tranh_thu = sum(1 for s in scope_soldiers if s.get("presence_status") == "Tranh thủ")
        
        absent_parts = []
        if absent_ra_ngoai: absent_parts.append(f"ngoài: {absent_ra_ngoai}")
        if absent_di_phep: absent_parts.append(f"phép: {absent_di_phep}")
        if absent_tranh_thu: absent_parts.append(f"tranh thủ: {absent_tranh_thu}")
        
        scope_label = store.canonical_unit_name(
            _node_by_id.get(scope_unit, {"id": scope_unit,
                                          "name": _name_map.get(scope_unit, "Toàn Trung đoàn")})
        ) if scope_unit != "root" else "Toàn Trung đoàn"
        
        if absent_parts:
            scope_sub = f"{scope_label} (Vắng {', '.join(absent_parts)})"
        else:
            scope_sub = f"{scope_label} (Đủ)"

        def _open_scope_picker():
            page = self.page
            # Build options từ flatten_units_for_select + thêm "Toàn Trung đoàn"
            opts = [ft.dropdown.Option("root", "🏛 Toàn Trung đoàn")]
            for uid, lbl in store.flatten_units_for_select(units_tree):
                opts.append(ft.dropdown.Option(uid, lbl))
            dd = ft.Dropdown(
                label="Chọn đơn vị xem quân số",
                options=opts, value=scope_unit,
                border_radius=8, dense=True,
            )
            def submit(_):
                self._home_scope_unit = dd.value or "root"
                _dlg.open = False
                self.body.content = self.view_home()
                self.refresh()
            _dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("Chọn đơn vị", size=15, weight=ft.FontWeight.BOLD),
                content=ft.Container(content=ft.Column([dd], tight=True), width=340),
                actions=[
                    ft.TextButton("Huỷ", on_click=lambda e: _close_dialog(self.page)),
                    ft.ElevatedButton("Áp dụng", on_click=submit,
                                      bgcolor=GREEN_MID, color=ft.Colors.WHITE),
                ],
            )
            _show_dialog(self.page, _dlg)

        # ===== Ca trực hôm nay — chỉ ca của cơ quan/đơn vị mình HOẶC cá nhân mình =====
        import datetime
        all_shifts = store.get("shifts", store.seed_shifts) or []
        today_d = datetime.date.today()
        d_start = int(datetime.datetime.combine(today_d, datetime.time.min).timestamp() * 1000)
        d_end = int(datetime.datetime.combine(today_d, datetime.time.max).timestamp() * 1000)
        # Lọc ca thuộc đơn vị mình hoặc gán cho mình
        my_unit_subtree = _descendants_of(my_unit_id) if my_unit_id else set()
        my_shifts = []
        for sh in all_shifts:
            sh_date = sh.get("date") or 0
            if not (d_start <= sh_date <= d_end):
                continue
            sh_unit = sh.get("unit_id") or sh.get("unitId") or ""
            sh_user = str(sh.get("user_id") or sh.get("creator_id") or "")
            if sh_user == my_uid:
                my_shifts.append(sh); continue
            if sh_unit and sh_unit in my_unit_subtree:
                my_shifts.append(sh); continue
            # Fallback: nếu shift không có unit_id (data cũ) → chỉ xem nếu là toàn trung đoàn
            # tạm thời bỏ qua để tránh hiển thị chéo
        # Hiển thị ca gần nhất
        if my_shifts:
            sh0 = my_shifts[0]
            sh_label = sh0.get("platoon") or sh0.get("location") or "Ca trực"
            sh_type = sh0.get("type", "")
            if sh_type == "night":
                sh_time = "18:00 – 06:00"
            elif sh_type == "day":
                sh_time = "06:00 – 18:00"
            else:
                sh_time = "—"
        else:
            sh_label = "Không có"
            sh_time = "Hôm nay nghỉ"

        # ===== Tính toán các nội dung chờ duyệt theo phân quyền =====
        reports_pending = sum(1 for r in reports if r.get("status") == "pending")

        guests_list = store.get("guests", [])
        guests_pending = sum(
            1 for g in guests_list
            if str(g.get("currentApproverId") or g.get("approverId") or "") == my_uid
            and g.get("status") in ("pending", "received", "forwarded")
        )

        schedules_list = store.get("schedule", [])
        schedules_pending = sum(
            1 for s in schedules_list
            if str(s.get("approver_id")) == my_uid
            and s.get("status") == "pending"
        )

        my_admin_level = int(profile.get("adminLevel") or 1)
        accounts_pending = sum(
            1 for s in soldiers
            if s.get("accountStatus") == "pending"
        ) if (my_admin_level >= 4 or profile.get("isAdmin")) else 0

        total_pending = reports_pending + guests_pending + schedules_pending + accounts_pending

        # Build sub-label for card
        pending_parts = []
        if guests_pending: pending_parts.append(f"Khách: {guests_pending}")
        if reports_pending: pending_parts.append(f"BC: {reports_pending}")
        if schedules_pending: pending_parts.append(f"Lịch: {schedules_pending}")
        if accounts_pending: pending_parts.append(f"Tài khoản: {accounts_pending}")

        if pending_parts:
            label_sub = " • ".join(pending_parts)
        else:
            label_sub = "Không có yêu cầu"

        def _open_approval_menu():
            page = self.page
            if total_pending == 0:
                self.toast("🎉 Bạn không có yêu cầu nào chờ phê duyệt!")
                return

            options_ctrls = []

            def make_opt(icon, text, count, on_click_action):
                def click_handler(e):
                    _close_dialog(self.page)
                    on_click_action()

                return ft.Container(
                    content=ft.Row([
                        ft.Text(f"{icon}  {text}", size=14, expand=True, weight=ft.FontWeight.W_500),
                        ft.Container(
                            content=ft.Text(str(count), size=11, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                            bgcolor=AMBER, border_radius=10, padding=ft.padding.symmetric(horizontal=8, vertical=2)
                        ),
                        ft.Icon(ft.Icons.CHEVRON_RIGHT, color=TEXT_MUTED, size=20)
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.padding.symmetric(horizontal=12, vertical=10),
                    bgcolor=BG2, border_radius=8, on_click=click_handler, ink=True
                )

            if guests_pending > 0:
                def go_guests():
                    setattr(self, "guests_view", "manage")
                    self.open_module("guests")
                options_ctrls.append(make_opt("🤝", "Đón tiếp khách", guests_pending, go_guests))

            if reports_pending > 0:
                options_ctrls.append(make_opt("📋", "Báo cáo nhanh", reports_pending, lambda: self.open_module("reports")))

            if schedules_pending > 0:
                options_ctrls.append(make_opt("🕐", "Lịch trực - Trực ban", schedules_pending, lambda: self.open_module("schedule")))

            if accounts_pending > 0:
                options_ctrls.append(make_opt("👤", "Tài khoản quân nhân", accounts_pending, lambda: self.open_module("units")))

            _dlg = ft.AlertDialog(
                modal=False,  # Cho phép click ngoài để đóng
                bgcolor=BG,
                title=ft.Text("Yêu cầu chờ phê duyệt", size=15, weight=ft.FontWeight.BOLD),
                content=ft.Container(
                    content=ft.Column(
                        options_ctrls,
                        spacing=8, tight=True
                    ),
                    width=320
                ),
                actions=[
                    ft.TextButton("Đóng", on_click=lambda e: _close_dialog(self.page))
                ]
            )
            _show_dialog(self.page, _dlg)

        stats = ft.Container(
            content=ft.Column(
                [
                    ft.Row([
                        stat_card("Quân số hôm nay",
                                  f"{present_count}/{total_count}",
                                  scope_sub,
                                  GREEN_DARK, "👥", _open_scope_picker),
                        stat_card("Ca trực hôm nay", sh_label, sh_time,
                                  GREEN_DARK, "🕐",
                                  lambda: self.open_module("schedule")),
                    ], spacing=8),
                    ft.Row([
                        stat_card("Chờ phê duyệt",
                                  str(total_pending),
                                  label_sub,
                                  AMBER if total_pending > 0 else GREEN_DARK, "📋",
                                  _open_approval_menu),
                        stat_card("F47 đang chạy", str(live_f47), f"{len(f47)} chiến dịch",
                                  RED, "🛡", lambda: self.open_module("f47")),
                    ], spacing=8),
                ],
                spacing=8,
            ),
            padding=12,
        )

        # Notifications block
        def notif_row(n: dict) -> ft.Container:
            color = {"urgent": RED, "f47": BLUE, "unit": AMBER, "success": GREEN_MID,
                     "ctdctct": GREEN_DARK, "guest": "#8855cc", "warning": AMBER}.get(n["type"], "#999")
            _sender = (n.get("senderName") or "").strip()
            _meta_parts = []
            if _sender:
                _meta_parts.append(f"👤 {_sender}")
            _meta_parts.append(time_ago(n["at"]))
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Container(width=8, height=8, bgcolor=color, border_radius=4,
                                     margin=ft.margin.only(top=6)),
                        ft.Column(
                            [
                                ft.Text(n["title"], size=13, color=TEXT,
                                        weight=ft.FontWeight.W_700 if not n["read"] else ft.FontWeight.W_500),
                                ft.Text(n["desc"], size=11, color=TEXT_MUTED, max_lines=2),
                                ft.Text("  •  ".join(_meta_parts), size=10, color=TEXT_MUTED),
                            ],
                            spacing=2, expand=True, tight=True,
                        ),
                    ],
                    spacing=10, vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=10,
                padding=11, margin=ft.margin.only(bottom=7),
                on_click=lambda e, _n=n: self.handle_notif_click(_n),
                ink=True,
            )

        notif_block = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("🔔 Thông báo mới", size=14, weight=ft.FontWeight.BOLD,
                                    expand=True),
                            ft.TextButton("Xem tất cả ›", on_click=lambda e: self.open_notifs(),
                                          style=ft.ButtonStyle(color=GREEN_MID)),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Column([notif_row(n) for n in notifs] if notifs else
                              [ft.Text("Không có thông báo", color=TEXT_MUTED, size=12)],
                              spacing=0),
                ],
                spacing=6,
            ),
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
        )

        # ===== Banner chiến dịch F47 đang live =====
        live_camps = [c for c in f47
                      if c.get("status") == "live"
                      and c.get("deadline", 0) > store.now_ms()]

        def _f47_banner(c: dict) -> ft.Container:
            now_ms_val = store.now_ms()
            left = c.get("deadline", 0) - now_ms_val
            h, rem = divmod(max(left, 0), 3600_000)
            m, _ = divmod(rem, 60_000)
            cd = f"{h:02d}h {m:02d}m" if left > 0 else "Hết giờ"
            done = len(c.get("submissions") or {})
            total = len(c.get("members") or [])
            pct = int(done / total * 100) if total else 0
            c_type = c.get("campaignType") or "Chiến dịch"
            return ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Container(
                            content=ft.Text(c_type, size=10, color=ft.Colors.WHITE,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=RED, border_radius=4,
                            padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        ),
                        ft.Container(
                            content=ft.Row([
                                ft.Container(width=6, height=6, bgcolor=RED,
                                             border_radius=3),
                                ft.Text("LIVE", size=10, color=RED,
                                        weight=ft.FontWeight.BOLD),
                            ], spacing=4, tight=True),
                        ),
                        ft.Container(expand=True),
                        ft.Text(f"⏱ {cd}", size=11, color=AMBER,
                                weight=ft.FontWeight.BOLD),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                    ft.Text(c.get("title", ""), size=14, weight=ft.FontWeight.BOLD,
                            color=TEXT, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(c.get("desc", ""), size=11, color=TEXT_MUTED,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Row([
                        ft.Column([
                            ft.ProgressBar(value=pct / 100, color=GREEN_MID,
                                           bgcolor=BORDER, height=6,
                                           border_radius=3, expand=True),
                        ], expand=True, tight=True),
                        ft.Text(f"{done}/{total}", size=11, color=TEXT_MUTED,
                                weight=ft.FontWeight.BOLD),
                    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Row([
                        ft.ElevatedButton(
                            "Xem chiến dịch →",
                            on_click=lambda e, _c=c: (
                                self.open_module("f47"),
                                self.f47_open_campaign_detail(_c),
                            ),
                            bgcolor=GREEN_DARK, color=ft.Colors.WHITE,
                            style=ft.ButtonStyle(
                                shape=ft.RoundedRectangleBorder(radius=8),
                                padding=ft.padding.symmetric(horizontal=16, vertical=8),
                            ),
                        ),
                    ], alignment=ft.MainAxisAlignment.END),
                ], spacing=8),
                bgcolor=BG,
                border=ft.border.all(1.5, RED),
                border_radius=12,
                padding=14,
                margin=ft.padding.symmetric(horizontal=12, vertical=4),
                on_click=lambda e, _c=c: (
                    self.open_module("f47"),
                    self.f47_open_campaign_detail(_c),
                ),
                ink=True,
            )

        f47_section = ft.Column(
            [
                ft.Container(
                    content=ft.Row([
                        ft.Text("🛡 Chiến dịch đang phát động",
                                size=13, weight=ft.FontWeight.BOLD, expand=True),
                        ft.TextButton(
                            "Xem tất cả ›",
                            on_click=lambda e: self.open_module("f47"),
                            style=ft.ButtonStyle(color=GREEN_MID),
                        ),
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    padding=ft.padding.only(left=12, right=4, top=10, bottom=2),
                ),
                *[_f47_banner(c) for c in live_camps[:3]],
            ],
            spacing=0,
        ) if live_camps else ft.Container()

        controls = [hero]
        if live_camps:
            controls.append(f47_section)
        controls += [stats, notif_block]

        return ft.ListView(controls=controls, expand=True, padding=0)

    def handle_notif_click(self, n: dict) -> None:
        # Mark read trên ALL notifs (không dùng filtered list để khỏi mất ngữ cảnh)
        notifs_all = store.get("notifs", store.seed_notifs)
        for x in notifs_all:
            if x.get("id") == n.get("id"):
                x["read"] = True
                break
        store.set_value("notifs", notifs_all)

        link = n.get("link") or ""
        # Fallback cho các thông báo cũ không có link
        if not link and n.get("type") == "unit" and "cần duyệt" in n.get("title", ""):
            link = "unit:accounts"

        # Hỗ trợ link dạng "f47:campId" hoặc "ctdctct:taskId" để mở thẳng chi tiết
        if ":" in link:
            kind, _, target_id = link.partition(":")
        else:
            kind, target_id = link, ""

        if not kind:
            kind = n.get("type", "")

        if kind in self.TASK_DOMAIN_CONFIG:
            self.set_tab("util")
            self.open_module(kind)
            if target_id:
                store_key = self.TASK_DOMAIN_CONFIG[kind].get("store_key")
                tasks = store.get(store_key, lambda: [])
                t = next((x for x in tasks if x.get("id") == target_id), None)
                if t:
                    self.task_open_task_detail(kind, t)
        elif kind == "f47":
            self.set_tab("util")
            self.open_module("f47")
            if target_id:
                camps = store.get("f47Campaigns", store.seed_f47)
                c = next((x for x in camps if x.get("id") == target_id), None)
                if c:
                    self.f47_open_campaign_detail(c)
        elif kind == "unit":
            self.set_tab("util")
            if target_id == "accounts":
                self.units_subtab = "accounts"
            self.open_module("units")
        elif kind == "profile":
            self.open_member_profile({"id": target_id})
        elif kind == "guest":
            # Mở tiện ích Thăm-Tiếp khách, nếu có target_id thì mở luôn dialog chi tiết
            self.set_tab("util")
            self.open_module("guests")
            if target_id:
                all_g = list(store.get("guests", []) or [])
                g = next((x for x in all_g if str(x.get("id")) == str(target_id)), None)
                if g:
                    # Nếu là chỉ huy được trình → mặc định vào tab "Quản lý"
                    my_profile = store.get("userProfile", store.seed_user_profile)
                    my_uid = str(my_profile.get("id") or AUTH_STATE.get("localId") or "")
                    cur_appr = str(g.get("currentApproverId") or g.get("approverId") or "")
                    if cur_appr == my_uid and int(my_profile.get("adminLevel") or 1) >= 2:
                        self.guests_view = "manage"
                    else:
                        self.guests_view = "my_guests"
                    # Mở dialog sau khi refresh module
                    self.refresh()
                    self.open_guest_details_dialog(g)
                    return
        elif kind in ("schedule", "reports", "medical", "finance"):
            self.open_module(kind)
        elif kind == "chat":
            self.set_tab("chat")
        else:
            self.toast(f"🔔 {n['title']}")
        self.refresh()

    # ============================================================
    # ===== VIEW: TIN NHẮN                                    =====
    # ============================================================

    def view_chat(self) -> ft.Control:
        # Lấy danh sách phòng chat thật từ Firestore (cache cục bộ qua store)
        raw = store.get("chat_rooms", store.seed_chat_rooms)
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or store.get(
            "userProfile", store.seed_user_profile,
        ).get("username", "")
        # Map uid → soldier (để tính tên DM động theo người kia, không dùng tên cached)
        soldiers_all = store.get("soldiers", store.seed_soldiers)
        soldier_by_uid = {str(s.get("id")): s for s in soldiers_all if s.get("id")}

        # Map uid → guest
        guests_all = store.get("guests", lambda: [])
        guest_by_uid = {}
        for g in guests_all:
            gid = str(g.get("id") or g.get("_id") or "")
            if gid:
                guest_by_uid[gid] = g

        def _dm_partner_name(rid: str, members: list, fallback: str) -> str:
            """Tên hiển thị của DM = TÊN người KHÁC mình (động, không cache)."""
            other_uid = ""
            if rid.startswith("dm-"):
                segs = rid[3:].split("-")
                if len(segs) >= 2:
                    other_uid = next((x for x in segs if x and x != str(my_uid)), "")
                elif len(segs) == 1:
                    # Format cũ
                    other_uid = segs[0] if segs[0] != str(my_uid) else ""
            if not other_uid:
                # Thử từ members list
                for x in members:
                    if str(x) != str(my_uid):
                        other_uid = str(x); break
            
            s = soldier_by_uid.get(other_uid) or guest_by_uid.get(other_uid)
            if s:
                rank = (s.get("rank") or "").strip()
                name = (s.get("name") or "").strip()
                full = f"{rank} {name}".strip()
                if full:
                    return full
            
            clean_fallback = fallback or "DM"
            if clean_fallback.startswith("dm-"):
                clean_fallback = "Hội thoại riêng"
            return clean_fallback

        # Normalize sang tuple cho UI hiện tại
        rooms_all = []
        for r in raw:
            rid = r.get("id", "") or ""
            # fallback để phân loại cho cache cũ (room DM trước đó có thể không có field "type")
            r_type = r.get("type")
            if not r_type:
                r_type = "dm" if str(rid).startswith("dm-") else "group"

            members = r.get("members") or []
            is_group = r_type == "group"

            # Tên hiển thị: group dùng tên cached, DM tính động theo người kia
            if is_group:
                display_name = r.get("name", "") or ""
                if not display_name or display_name.startswith("group-"):
                    display_name = "Nhóm thảo luận"
            else:
                display_name = _dm_partner_name(rid, members, r.get("name", ""))

            # Preview tin nhắn cuối — nếu có thì hiển thị, nếu chưa có thì
            # fallback sang số thành viên (group) / status (DM).
            last_msg = (r.get("lastMessage") or "").strip()
            if last_msg:
                # Cắt tin nhắn dài để vừa 1 dòng
                sub = last_msg if len(last_msg) <= 60 else last_msg[:57] + "..."
            elif is_group:
                sub = f"{len(members)} thành viên"
            else:
                sub = (r.get("status") or "").replace("🟢", "").strip() or "Bắt đầu trò chuyện"

            last_at = r.get("lastAt", store.now_ms())
            when = time_ago(last_at)
            unread = store.chat_unread_for_user(r, my_uid)
            online_dm = (not is_group) and (
                bool(r.get("online")) or ("🟢" in str(r.get("status") or ""))
            )

            # Lưu cờ "tôi có phải member" để filter
            # DM check phải là EXACT segment match (tránh substring trùng nhầm).
            dm_member = False
            if rid.startswith("dm-"):
                dm_segments = rid[3:].split("-")
                # Format mới: "dm-uidA-uidB" → 2 segment
                # Format cũ: "dm-uidX" → 1 segment (chỉ uid người kia)
                # Tôi là member nếu my_uid ∈ segments. Format cũ thì
                # KHÔNG biết được nên cho qua nếu phòng được upsert tới members.
                if str(my_uid) in dm_segments:
                    dm_member = True
                elif len(dm_segments) == 1:
                    # Format cũ — fallback: dùng members list
                    dm_member = str(my_uid) in [str(x) for x in members]

            is_member = (
                (is_group and not members)  # group không khai báo members → mở cho tất cả
                or str(my_uid) in [str(x) for x in members]
                or dm_member
            )

            rooms_all.append(
                (r.get("id", ""), display_name, sub, when, unread,
                 is_group, r_type, online_dm, is_member),
            )

        # Lọc: chỉ hiện phòng tôi là member (DM riêng tư cho người khác sẽ ẩn)
        rooms_all = [r for r in rooms_all if r[8]]

        # Ẩn các phòng user đã chọn ẩn (xoá khỏi danh sách).
        # Hidden = {rid: hiddenAt_ms}. Nếu phòng có tin mới sau hiddenAt thì tự hiện lại.
        hidden_raw = store.get("hiddenChatRooms", lambda: {})
        if isinstance(hidden_raw, list):
            hidden_raw = {x: 0 for x in hidden_raw}
        if hidden_raw:
            def _is_hidden(r) -> bool:
                rid_h = r[0]
                if rid_h not in hidden_raw:
                    return False
                # raw room data để xem lastAt
                room_obj = next((rr for rr in raw if rr.get("id") == rid_h), None)
                last_at = int((room_obj or {}).get("lastAt") or 0)
                return last_at <= int(hidden_raw[rid_h] or 0)

            rooms_all = [r for r in rooms_all if not _is_hidden(r)]

        # Áp dụng filter: all / dm / group
        current_filter = getattr(self, "chat_filter", "all")
        if current_filter == "dm":
            rooms = [r for r in rooms_all if r[6] != "group"]
        elif current_filter == "group":
            rooms = [r for r in rooms_all if r[6] == "group"]
        else:
            rooms = rooms_all

        # Desktop detection (dùng cho 2-pane layout)
        import os as _os_chat
        _is_android = (_os_chat.environ.get("ANDROID_ROOT") is not None
                       or _os_chat.environ.get("ANDROID_DATA") is not None)
        is_desktop = (self.page.width >= 800) and not _is_android

        # Phòng đang được chọn (chỉ dùng trên desktop)
        _sel_room = getattr(self, "chat_selected_room", None)
        _sel_rid = _sel_room[0] if _sel_room else None
        # Nếu phòng đang chọn đã bị ẩn/xóa → tự động bỏ chọn
        if _sel_rid and is_desktop:
            _sel_still_visible = any(r[0] == _sel_rid for r in rooms_all)
            if not _sel_still_visible:
                self.chat_selected_room = None
                _sel_room = None
                _sel_rid = None

        def chat_item(room) -> ft.Container:
            rid, name, sub, when, unread, is_group, _rtype = room[:7]
            online_dm = room[7] if len(room) > 7 else False
            init = initials(name, 2)
            badge = ft.Container(
                content=ft.Text(
                    str(unread) if unread < 100 else "99+",
                    color=ft.Colors.WHITE, size=10,
                    weight=ft.FontWeight.BOLD,
                ),
                bgcolor=RED, border_radius=10, padding=ft.padding.symmetric(horizontal=6, vertical=2),
                alignment=ft.alignment.center, height=18,
            ) if unread else ft.Container()
            avatar_circle = ft.Container(
                content=ft.Text(init, color=GREEN_DARK, size=14,
                                weight=ft.FontWeight.BOLD),
                bgcolor=GREEN_LIGHT,
                width=44, height=44,
                border_radius=12 if is_group else 22,
                alignment=ft.alignment.center,
            )
            avatar_stack = (
                ft.Stack(
                    [
                        avatar_circle,
                        ft.Container(
                            content=ft.Container(
                                width=10,
                                height=10,
                                bgcolor="#22c55e",
                                border_radius=5,
                                border=ft.border.all(2, BG),
                            ),
                            right=1,
                            bottom=1,
                        ),
                    ],
                    width=44,
                    height=44,
                )
                if (online_dm and not is_group)
                else avatar_circle
            )
            is_selected = is_desktop and (rid == _sel_rid)
            return ft.Container(
                content=ft.Row(
                    [
                        avatar_stack,
                        ft.Column(
                            [
                                ft.Row(
                                    [ft.Text(name, size=14, weight=ft.FontWeight.W_700, expand=True,
                                             max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                     ft.Text(when, size=11, color=TEXT_MUTED, no_wrap=True)],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                ),
                                ft.Row(
                                    [ft.Text(sub, size=12, color=TEXT_MUTED, expand=True,
                                             overflow=ft.TextOverflow.ELLIPSIS), badge],
                                ),
                            ],
                            expand=True, spacing=2, tight=True,
                        ),
                        ft.IconButton(
                            ft.Icons.MORE_VERT, icon_size=18,
                            tooltip="Tuỳ chọn",
                            on_click=lambda e, r=room: self.chat_room_menu(r),
                        ),
                    ],
                    spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                bgcolor="#e8f5e9" if is_selected else BG,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                on_click=lambda e, r=room: self.open_chat_room(r),
                ink=True,
            )

        # Thanh filter: Tất cả / Riêng / Nhóm
        def set_filter(kind: str):
            self.chat_filter = kind
            # rebuild lại view chat
            self.body.content = self.view_chat()
            self.refresh()

        tab_kinds = [("Tất cả", "all"), ("Riêng", "dm"), ("Nhóm", "group")]
        selected_idx = next((i for i, (_, k) in enumerate(tab_kinds) if k == self.chat_filter), 0)

        def on_chat_tab_changed(e):
            idx = None
            try:
                idx = e.control.selected_index
            except Exception:
                idx = getattr(e, "selected_index", None)
            if idx is None:
                return
            if 0 <= idx < len(tab_kinds):
                set_filter(tab_kinds[idx][1])

        # ---- Filter bar (tabs + icon kính lúp) ----
        search_open = getattr(self, "chat_search_open", False)
        search_query = getattr(self, "chat_search_query", "")

        def toggle_search(e=None):
            self.chat_search_open = not getattr(self, "chat_search_open", False)
            if not self.chat_search_open:
                self.chat_search_query = ""
            self.body.content = self.view_chat()
            self.refresh()

        def on_search_change(e):
            self.chat_search_query = (e.control.value or "").strip().lower()
            # rebuild để filter rooms
            self.body.content = self.view_chat()
            self.refresh()

        filter_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Tabs(
                            selected_index=selected_idx,
                            on_change=on_chat_tab_changed,
                            tabs=[ft.Tab(text=label) for label, _ in tab_kinds],
                            height=46,
                        ),
                        expand=True, height=46,
                    ),
                    ft.IconButton(
                        ft.Icons.SEARCH_OFF if search_open else ft.Icons.SEARCH,
                        tooltip="Đóng tìm kiếm" if search_open else "Tìm kiếm",
                        icon_color=GREEN_DARK,
                        on_click=toggle_search,
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.only(left=10, right=4, top=4, bottom=4),
            bgcolor=BG,
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
        )

        # Lọc rooms theo query nếu đang search
        if search_open and search_query:
            rooms = [r for r in rooms
                     if search_query in (r[1] or "").lower()
                     or search_query in (r[2] or "").lower()]

        # Ô input chỉ hiện khi đã mở search
        header_controls = [filter_bar]
        if search_open:
            search_input = ft.Container(
                content=ft.TextField(
                    prefix_icon=ft.Icons.SEARCH,
                    hint_text="Tìm cuộc trò chuyện...",
                    border=ft.InputBorder.NONE, height=44, dense=True,
                    filled=True, bgcolor=BG2,
                    border_radius=22,
                    value=search_query,
                    on_change=on_search_change,
                    autofocus=True,
                ),
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )
            header_controls.append(search_input)

        room_items = [chat_item(r) for r in rooms]

        if is_desktop:
            # ── Desktop: layout 2 cột kiểu Zalo ──────────────────────────
            # Cột trái: filter tabs + search + danh sách phòng
            left_header_controls: list[ft.Control] = []

            # Filter bar + nút tạo nhóm
            left_filter_bar = ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            content=ft.Tabs(
                                selected_index=selected_idx,
                                on_change=on_chat_tab_changed,
                                tabs=[ft.Tab(text=label) for label, _ in tab_kinds],
                                height=46,
                            ),
                            expand=True, height=46,
                        ),
                        ft.IconButton(
                            ft.Icons.SEARCH_OFF if search_open else ft.Icons.SEARCH,
                            tooltip="Đóng tìm kiếm" if search_open else "Tìm kiếm",
                            icon_color=GREEN_DARK,
                            on_click=toggle_search,
                        ),
                        ft.IconButton(
                            ft.Icons.GROUP_ADD if hasattr(ft.Icons, "GROUP_ADD") else ft.Icons.ADD,
                            tooltip="Tạo nhóm chat",
                            icon_color=GREEN_DARK,
                            on_click=lambda e: self.open_create_group_dialog(),
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.only(left=6, right=2, top=4, bottom=4),
                bgcolor=BG,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )
            left_header_controls.append(left_filter_bar)

            if search_open:
                left_header_controls.append(ft.Container(
                    content=ft.TextField(
                        prefix_icon=ft.Icons.SEARCH,
                        hint_text="Tìm cuộc trò chuyện...",
                        border=ft.InputBorder.NONE, height=44, dense=True,
                        filled=True, bgcolor=BG2, border_radius=22,
                        value=search_query,
                        on_change=on_search_change,
                        autofocus=True,
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    bgcolor=BG,
                    border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                ))

            if room_items:
                left_list_content = ft.Column(
                    controls=room_items,
                    scroll=ft.ScrollMode.AUTO,
                    spacing=0,
                    expand=True,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                )
            else:
                left_list_content = ft.Column(
                    controls=[
                        ft.Container(
                            content=ft.Text("Không có cuộc trò chuyện nào",
                                            color=TEXT_MUTED, size=13,
                                            text_align=ft.TextAlign.CENTER),
                            padding=ft.padding.symmetric(vertical=40),
                            alignment=ft.alignment.center,
                        )
                    ],
                    expand=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                )

            left_pane = ft.Container(
                content=ft.Column(
                    left_header_controls + [left_list_content],
                    spacing=0,
                    expand=True,
                ),
                width=340,
                bgcolor=BG,
                border=ft.border.only(right=ft.BorderSide(1, BORDER)),
            )

            # Cột phải: chat đang mở hoặc placeholder
            if _sel_room:
                right_pane = self.view_chat_detail(
                    _sel_room[0], _sel_room[1], _sel_room[2],
                    show_back=False,
                )
            else:
                right_pane = ft.Container(
                    content=ft.Column(
                        [
                            ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, size=64, color=TEXT_MUTED),
                            ft.Container(height=16),
                            ft.Text("Chọn cuộc trò chuyện để bắt đầu",
                                    size=15, color=TEXT_MUTED,
                                    text_align=ft.TextAlign.CENTER),
                            ft.Container(height=8),
                            ft.Text("Hoặc tạo nhóm mới bằng nút  +  ở trên",
                                    size=12, color=TEXT_MUTED,
                                    text_align=ft.TextAlign.CENTER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    expand=True,
                    alignment=ft.alignment.center,
                    bgcolor=BG2,
                )

            return ft.Row(
                [left_pane, right_pane],
                spacing=0,
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            )

        else:
            # ── Mobile: layout 1 cột (giữ nguyên) ────────────────────────
            list_view = ft.ListView(
                controls=header_controls + room_items,
                expand=True,
                padding=0,
            )
            fab = ft.Container(
                content=ft.FloatingActionButton(
                    icon=ft.Icons.GROUP_ADD if hasattr(ft.Icons, "GROUP_ADD") else ft.Icons.ADD,
                    bgcolor=RED,
                    foreground_color=ft.Colors.WHITE,
                    tooltip="Tạo nhóm chat",
                    on_click=lambda e: self.open_create_group_dialog(),
                ),
                right=16,
                bottom=16,
            )
            return ft.Stack([list_view, fab], expand=True)

    def open_chat_room(self, room) -> None:
        rid, name, sub, *_ = room
        import os as _os_ocr
        _is_android_ocr = (_os_ocr.environ.get("ANDROID_ROOT") is not None
                           or _os_ocr.environ.get("ANDROID_DATA") is not None)
        is_desktop = (self.page.width >= 800) and not _is_android_ocr
        if is_desktop:
            # Dừng listener cũ trong background (tránh block UI)
            prev_stop = getattr(self, "_chat_listener_stop", None)
            self._chat_listener_stop = None
            if callable(prev_stop):
                threading.Thread(
                    target=lambda fn=prev_stop: (lambda: (fn(),))(),
                    daemon=True,
                ).start()
            self.chat_selected_room = room
            self.body.content = self.view_chat()
            self.refresh()
        else:
            self.body.content = self.view_chat_detail(rid, name, sub)
            self.refresh()

    def open_create_group_dialog(self) -> None:
        """Tạo phòng chat dạng nhóm và đưa vào `chat_rooms` để tab Tin nhắn hiển thị."""
        page = self.page
        # Đồng bộ soldiers từ users/ (source of truth) trước khi mở dialog
        # → tránh hiển thị tài khoản ảo / đã xoá.
        try:
            store.refresh_soldiers_from_users()
        except Exception:
            pass
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or ""
        soldiers = [s for s in store.get("soldiers", store.seed_soldiers)
                    if not s.get("isAdmin") and str(s.get("id")) != str(my_uid)]

        name_input = ft.TextField(
            label="Tên nhóm *",
            border_radius=10,
            dense=True,
        )
        err_text = ft.Text("", color=RED, size=12)

        # Checkbox list chọn thành viên (dùng ID soldierId để lưu)
        check_items: list[tuple[str, ft.Checkbox]] = []
        for s in soldiers:
            sid = str(s.get("id") or "")
            if not sid:
                continue
            label = f"{(s.get('rank') or '').strip()} {(s.get('name') or '').strip()}".strip()
            if not label:
                label = s.get("name", sid) or sid
            check_items.append((sid, ft.Checkbox(label=label, value=False)))

        members_column = ft.Column(
            [cb for _, cb in check_items] if check_items else
            [ft.Text("Chưa có danh sách quân nhân. Hãy thêm quân nhân trước.", color=TEXT_MUTED, size=12)],
            spacing=2,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
            height=260,
        )

        def submit(e):
            group_name = (name_input.value or "").strip()
            selected_ids = [sid for sid, cb in check_items if cb.value]

            if not group_name:
                err_text.value = "⚠️ Nhập tên nhóm"
                page.update()
                return
            if not selected_ids:
                err_text.value = "⚠️ Chọn ít nhất 1 thành viên"
                page.update()
                return

            now = store.now_ms()
            rid = f"group-{now}"
            my_uid = AUTH_STATE.get("uid") or ""
            # Đảm bảo người tạo cũng là member
            members_with_me = list({*selected_ids, my_uid}) if my_uid else selected_ids
            room = {
                "id": rid,
                "name": group_name,
                "type": "group",
                "members": members_with_me,
                "createdBy": my_uid,  # nhóm trưởng = người tạo, có quyền xoá
                "createdAt": now,
                "lastMessage": "",
                "lastAt": now,
                "unread": 0,
                "status": "",
                "lastReadAt": {},
                "unreadByUser": {},
                "pinnedMessageIds": [],
            }
            store.upsert_chat_room(room)

            try:
                _dlg.open = False
            except Exception:
                pass

            self.open_chat_room(
                (rid, group_name, f"{len(selected_ids)} thành viên", "", 0, True, "group", False),
            )

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("＋ Tạo nhóm chat", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [
                        name_input,
                        ft.Text("Chọn thành viên *", size=12, color=TEXT_MUTED, weight=ft.FontWeight.W_500),
                        ft.Container(
                            content=members_column,
                            bgcolor=BG2,
                            border_radius=10,
                            padding=10,
                            border=ft.border.all(1, BORDER),
                        ),
                        err_text,
                    ],
                    spacing=10,
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
                width=420,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: (_close_dialog(self.page), page.update()),
                ),
                ft.ElevatedButton(
                    "Tạo nhóm",
                    on_click=submit,
                    bgcolor=GREEN_MID,
                    color=ft.Colors.WHITE,
                ),
            ],
        )
        _show_dialog(self.page, _dlg)

    def view_chat_detail(self, rid: str, name: str, sub: str, show_back: bool = True) -> ft.Control:
        # Huỷ listener cũ trong background (tránh sio.disconnect() block UI thread)
        prev_stop = getattr(self, "_chat_listener_stop", None)
        self._chat_listener_stop = None
        if callable(prev_stop):
            threading.Thread(
                target=lambda fn=prev_stop: (lambda: (fn(),))(),
                daemon=True,
            ).start()

        # ---- Bảo vệ: kiểm tra membership trước khi mở phòng ----
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or ""
        rooms_meta = store.get("chat_rooms", store.seed_chat_rooms)
        room_meta = next((r for r in rooms_meta if r.get("id") == rid), None) or {}
        is_group_room = (room_meta.get("type") or
                         ("dm" if rid.startswith("dm-") else "group")) == "group"
        room_members = [str(x) for x in (room_meta.get("members") or [])]

        # DM: kiểm tra qua segment id (deterministic)
        allowed = True
        if rid.startswith("dm-"):
            segs = rid[3:].split("-")
            if str(my_uid) in segs:
                allowed = True
            elif len(segs) == 1:
                # Format cũ — fallback dùng members
                allowed = str(my_uid) in room_members
            else:
                allowed = False
        elif is_group_room:
            # Group: nếu members rỗng → mở cho tất cả; nếu có members → phải nằm trong
            allowed = (not room_members) or (str(my_uid) in room_members)
        else:
            allowed = True

        # ===== Zalo-like Right Sidebar implementation =====
        self._chat_sidebar_open = getattr(self, "_chat_sidebar_open", False)

        avatar_initials = initials(name, 2) if not is_group_room else "👥"

        sb_avatar = ft.Container(
            content=ft.Text(avatar_initials, color=ft.Colors.WHITE, size=18, weight=ft.FontWeight.BOLD),
            bgcolor=GREEN_DARK, width=60, height=60, border_radius=30,
            alignment=ft.alignment.center,
        )

        sb_name = ft.Text(name, size=15, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER)
        sb_sub = ft.Text(sub, size=11, color=TEXT_MUTED, text_align=ft.TextAlign.CENTER)

        # Photo / Video section
        sb_media_title = ft.Text("🖼️  Ảnh / Video", size=13, weight=ft.FontWeight.BOLD, expand=True)
        sb_media_count = ft.Text("(0)", size=12, color=TEXT_MUTED)
        sb_media_grid = ft.Row(wrap=True, spacing=4, controls=[])

        # File section
        sb_files_title = ft.Text("📂  File / Tài liệu", size=13, weight=ft.FontWeight.BOLD, expand=True)
        sb_files_count = ft.Text("(0)", size=12, color=TEXT_MUTED)
        sb_files_col = ft.Column(spacing=4, tight=True, controls=[])

        sb_media_section = ft.Container(
            content=ft.Column([
                ft.Row([sb_media_title, sb_media_count], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Container(height=4),
                sb_media_grid
            ], spacing=2),
            padding=10,
            bgcolor=BG2,
            border_radius=10,
            border=ft.border.all(1, BORDER)
        )

        sb_files_section = ft.Container(
            content=ft.Column([
                ft.Row([sb_files_title, sb_files_count], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Container(height=4),
                sb_files_col
            ], spacing=2),
            padding=10,
            bgcolor=BG2,
            border_radius=10,
            border=ft.border.all(1, BORDER)
        )

        def clear_history(_):
            def confirm_clear(e):
                store.set_value(f"chat_messages_{rid}", [])
                _close_dialog(self.page)
                self.toast("🧹 Đã xóa lịch sử trò chuyện cục bộ!")
                render_messages([])

            _dlg = ft.AlertDialog(
                title=ft.Text("Xóa lịch sử?", size=15, weight=ft.FontWeight.BOLD),
                content=ft.Text("Bạn có chắc chắn muốn xóa toàn bộ lịch sử tin nhắn của cuộc trò chuyện này không?"),
                actions=[
                    ft.TextButton("Huỷ", on_click=lambda e: _close_dialog(self.page)),
                    ft.ElevatedButton("Xóa", on_click=confirm_clear, bgcolor=RED, color=ft.Colors.WHITE)
                ]
            )
            _show_dialog(self.page, _dlg)

        def leave_group(_):
            rooms = store.get("chat_rooms", store.seed_chat_rooms)
            rooms = [r for r in rooms if r.get("id") != rid]
            store.set_value("chat_rooms", rooms)
            self.toast("Đã rời/giải tán nhóm chat!")
            self.chat_selected_room = None
            self.set_tab("chat")

        # Settings buttons
        sb_settings_col = ft.Column([
            ft.TextButton(
                content=ft.Row([
                    ft.Icon(ft.Icons.DELETE_SWEEP_OUTLINED, color=RED, size=18),
                    ft.Text("Xóa lịch sử trò chuyện", color=RED, size=13),
                ], spacing=8),
                on_click=clear_history
            ),
        ], spacing=2, tight=True)

        if is_group_room:
            is_creator = str(room_meta.get("createdBy") or "") == str(my_uid)
            leave_text = "Giải tán nhóm" if is_creator else "Rời nhóm"
            sb_settings_col.controls.append(
                ft.TextButton(
                    content=ft.Row([
                        ft.Icon(ft.Icons.EXIT_TO_APP_OUTLINED, color=RED, size=18),
                        ft.Text(leave_text, color=RED, size=13),
                    ], spacing=8),
                    on_click=leave_group
                )
            )

        sb_settings_section = ft.Container(
            content=ft.Column([
                ft.Text("⚙️  Cài đặt cuộc trò chuyện", size=13, weight=ft.FontWeight.BOLD),
                ft.Container(height=4),
                sb_settings_col
            ], spacing=2),
            padding=10,
            bgcolor=BG2,
            border_radius=10,
            border=ft.border.all(1, BORDER)
        )

        def update_sidebar_attachments(items: list[dict]):
            images = []
            files = []
            for m in items:
                txt = m.get("text", "") or ""
                if txt.startswith("📎 [") and "]" in txt:
                    try:
                        parts = txt.split("] ", 1)
                        if len(parts) == 2:
                            file_name = parts[0][3:]
                            url = parts[1].strip()
                            ext = file_name.split(".")[-1].lower() if "." in file_name else ""
                            if ext in ("png", "jpg", "jpeg", "gif", "webp", "mp4", "mov", "avi", "webm"):
                                images.append({"name": file_name, "url": url, "is_video": ext not in ("png", "jpg", "jpeg", "gif", "webp")})
                            else:
                                files.append({"name": file_name, "url": url})
                    except Exception:
                        pass

            # Update Photo/Video grid
            sb_media_count.value = f"({len(images)})"
            sb_media_grid.controls.clear()
            if not images:
                sb_media_grid.controls.append(
                    ft.Text("Chưa có ảnh/video nào được gửi", size=11, color=TEXT_MUTED, italic=True)
                )
            else:
                for img in images[:6]:
                    img_url = img["url"]
                    is_vid = img["is_video"]
                    thumbnail = ft.Container(
                        width=80,
                        height=80,
                        bgcolor=BG,
                        border_radius=6,
                        border=ft.border.all(1, BORDER),
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                        on_click=lambda e, url=img_url, nm=img["name"]: open_image_viewer(self.page, url, nm),
                        tooltip=img["name"],
                        content=ft.Stack([
                            ft.Image(src=img_url, width=80, height=80, fit=ft.ImageFit.COVER),
                            ft.Container(
                                content=ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINE, color=ft.Colors.WHITE, size=24),
                                alignment=ft.alignment.center,
                                bgcolor=ft.Colors.with_opacity(0.4, ft.Colors.BLACK),
                            ) if is_vid else ft.Container()
                        ])
                    )
                    sb_media_grid.controls.append(thumbnail)

            # Update Files
            sb_files_count.value = f"({len(files)})"
            sb_files_col.controls.clear()
            if not files:
                sb_files_col.controls.append(
                    ft.Text("Chưa có tài liệu nào được gửi", size=11, color=TEXT_MUTED, italic=True)
                )
            else:
                for f in files[:5]:
                    file_url = f["url"]
                    file_name = f["name"]
                    file_row = ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.ATTACH_FILE, size=16, color=GREEN_MID),
                            ft.Text(file_name, size=12, expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.IconButton(
                                ft.Icons.DOWNLOAD,
                                icon_size=16,
                                icon_color=TEXT_MUTED,
                                tooltip="Tải về",
                                on_click=lambda e, url=file_url: self.page.launch_url(url)
                            )
                        ], spacing=6),
                        padding=ft.padding.symmetric(vertical=4),
                        border=ft.border.only(bottom=ft.BorderSide(1, BORDER))
                    )
                    sb_files_col.controls.append(file_row)

            try:
                self.page.update()
            except Exception:
                pass

        def toggle_sidebar(e):
            self._chat_sidebar_open = not self._chat_sidebar_open
            sidebar_container.width = 300 if self._chat_sidebar_open else 0
            sidebar_container.padding = 12 if self._chat_sidebar_open else 0
            self.page.update()

        sb_close_btn = ft.IconButton(
            ft.Icons.CHEVRON_RIGHT,
            tooltip="Đóng",
            on_click=lambda e: toggle_sidebar(None)
        )

        sb_header = ft.Row([
            ft.Text("Thông tin cuộc trò chuyện", size=14, weight=ft.FontWeight.BOLD, expand=True),
            sb_close_btn
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        sidebar_content = ft.Column([
            sb_header,
            ft.Divider(height=1, color=BORDER),
            ft.Container(height=10),
            sb_media_section,
            ft.Container(height=10),
            sb_files_section,
            ft.Container(height=10),
            sb_settings_section,
        ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=0, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        sidebar_container = ft.Container(
            width=300 if self._chat_sidebar_open else 0,
            bgcolor=BG,
            border=ft.border.only(left=ft.BorderSide(1, BORDER)),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            animate=ft.animation.Animation(250, ft.AnimationCurve.DECELERATE),
            padding=12 if self._chat_sidebar_open else 0,
            content=sidebar_content
        )

        if not allowed:
            return ft.Column([
                ft.Container(
                    content=ft.Row([
                        ft.IconButton(ft.Icons.ARROW_BACK,
                                      on_click=lambda e: self.set_tab("chat")),
                        ft.Text(name, size=15, weight=ft.FontWeight.BOLD,
                                expand=True),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    bgcolor=BG, padding=ft.padding.symmetric(horizontal=4, vertical=4),
                    border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                ),
                ft.Container(
                    content=ft.Column([
                        ft.Container(height=80),
                        ft.Icon(ft.Icons.LOCK, size=48, color=TEXT_MUTED),
                        ft.Container(height=10),
                        ft.Text("🔒 Bạn không có quyền xem cuộc trò chuyện này",
                                size=14, color=TEXT_MUTED,
                                text_align=ft.TextAlign.CENTER),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                    expand=True, alignment=ft.alignment.center,
                ),
            ], spacing=0, expand=True)

        def make_bubble_content(txt: str, text_color):
            if txt.startswith("📎 [") and "]" in txt:
                try:
                    parts = txt.split("] ", 1)
                    if len(parts) == 2:
                        file_name = parts[0][3:]
                        url = parts[1].strip()
                        ext = file_name.split(".")[-1].lower() if "." in file_name else ""
                        if ext in ("png", "jpg", "jpeg", "gif", "webp"):
                            return ft.Column([
                                ft.Text(f"📎 {file_name}", color=text_color, size=11, italic=True),
                                ft.GestureDetector(
                                    content=ft.Image(src=url, max_width=240, border_radius=8, fit=ft.ImageFit.CONTAIN),
                                    on_tap=lambda e, u=url, n=file_name: open_image_viewer(self.page, u, n)
                                )
                            ], spacing=4)
                        elif ext in ("mp4", "mov", "avi", "webm", "3gp"):
                            return ft.Column([
                                ft.TextButton(
                                    content=ft.Row([
                                        ft.Icon(ft.Icons.VIDEO_LIBRARY, size=16, color=text_color),
                                        ft.Text(f"🎬 {file_name}", color=text_color, size=13),
                                    ], tight=True),
                                    on_click=lambda e: self.page.launch_url(url)
                                )
                            ], spacing=4)
                        else:
                            return ft.TextButton(
                                content=ft.Row([
                                    ft.Icon(ft.Icons.ATTACH_FILE, size=16, color=text_color),
                                    ft.Text(file_name, color=text_color, size=13, weight=ft.FontWeight.BOLD),
                                ], tight=True),
                                on_click=lambda e: self.page.launch_url(url)
                            )
                except Exception:
                    pass
            return ft.Text(txt, color=text_color, size=13)

        def bubble_them_inner(txt: str, who: str, pinned: bool):
            border = ft.border.all(2, GOLD) if pinned else ft.border.all(1, BORDER)
            bubble_content = make_bubble_content(txt, TEXT)
            if isinstance(bubble_content, ft.Text):
                bubble_content.expand = True
            bubble_body = ft.Container(
                content=ft.Row(
                    [
                        ft.Text("📌", size=11) if pinned else ft.Container(width=0),
                        bubble_content,
                    ],
                    spacing=4, tight=True,
                ),
                bgcolor=BG,
                border=border,
                border_radius=14,
                padding=10,
            )
            return ft.Row(
                [
                    ft.Container(
                        content=ft.Text(initials(who, 2), color=ft.Colors.WHITE, size=10),
                        bgcolor=GREEN_DARK, width=28, height=28, border_radius=14,
                        alignment=ft.alignment.center,
                    ),
                    ft.Column(
                        [
                            ft.Text(who, size=10, color=TEXT_MUTED),
                            bubble_body,
                        ],
                        spacing=2, tight=True,
                    ),
                ],
                spacing=6, vertical_alignment=ft.CrossAxisAlignment.END,
            )

        def bubble_me_inner(txt: str, footer: str, pinned: bool):
            border = ft.border.all(2, GOLD) if pinned else None
            bubble_wrap = ft.Container(
                content=make_bubble_content(txt, ft.Colors.WHITE),
                bgcolor=GREEN_MID,
                border_radius=14,
                padding=10,
                border=border,
            )
            row_bubble = ft.Row(
                [
                    ft.Text("📌", size=11) if pinned else ft.Container(width=0),
                    bubble_wrap,
                    ft.Container(
                        content=ft.Text("Tôi", color=ft.Colors.WHITE, size=10),
                        bgcolor="#9fe1cb", width=28, height=28, border_radius=14,
                        alignment=ft.alignment.center,
                    ),
                ],
                alignment=ft.MainAxisAlignment.END,
                spacing=4, vertical_alignment=ft.CrossAxisAlignment.END,
            )
            if not footer:
                return row_bubble
            return ft.Column(
                [
                    row_bubble,
                    ft.Container(
                        content=ft.Text(footer, size=9, color=ft.Colors.WHITE54),
                        padding=ft.padding.only(right=34, top=1),
                        alignment=ft.alignment.center_right,
                    ),
                ],
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.END,
            )

        msgs = ft.ListView(
            controls=[
                ft.Container(content=ft.Text(f"Hôm nay {fmt_date(store.now_ms())}",
                                             size=10, color=TEXT_MUTED),
                             alignment=ft.alignment.center,
                             padding=ft.padding.symmetric(vertical=8)),
            ],
            spacing=10, padding=10, expand=True, auto_scroll=False,
        )

        msg_input = ft.TextField(
            hint_text="Nhắn tin...", border=ft.InputBorder.NONE, expand=True,
            dense=True, filled=True, bgcolor=BG2, border_radius=22,
            content_padding=ft.padding.symmetric(horizontal=14, vertical=10),
        )

        # Lấy thông tin user hiện tại (để biết tin nào là của mình)
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or store.get(
            "userProfile", store.seed_user_profile,
        ).get("username", "")
        my_name = store.get("userProfile", store.seed_user_profile).get("name", "Tôi")

        try:
            store.mark_chat_room_read(rid, str(my_uid))
        except Exception:
            pass

        # FilePicker cho chat đính kèm
        def on_chat_file_picked(e: ft.FilePickerResultEvent):
            if not e.files:
                return
            if not store.STORE.is_bound() or not AUTH_STATE.get("idToken"):
                self.toast("❌ Phải đăng nhập trước khi đính kèm")
                return
            self.toast(f"⏳ Đang gửi {len(e.files)} file đính kèm...")
            
            def worker():
                for f in e.files:
                    try:
                        folder = f"chat/{rid}/{my_uid}"
                        remote = fb_storage.make_remote_path(folder, f.name)
                        if f.path:
                            res = fb_storage.upload_file(f.path, remote, AUTH_STATE["idToken"])
                        elif f.bytes:
                            res = fb_storage.upload_data(remote, f.bytes, AUTH_STATE["idToken"], f.name)
                        else:
                            continue
                        url = res["downloadURL"]

                        msg_text = f"📎 [{f.name}] {url}"
                        store.send_chat_message(rid, my_uid, my_name, msg_text)
                    except Exception as ex_e:
                        self.toast(f"❌ Lỗi gửi file: {ex_e}")
            
            threading.Thread(target=worker, daemon=True).start()

        chat_picker = ft.FilePicker(on_result=on_chat_file_picked)
        if chat_picker not in self.page.overlay:
            self.page.overlay.append(chat_picker)

        def room_meta() -> tuple[list, dict]:
            raw = store.get("chat_rooms", store.seed_chat_rooms)
            row = next((x for x in raw if x.get("id") == rid), None)
            if not row:
                return [], {}
            return list(row.get("members") or []), dict(row.get("lastReadAt") or {})

        def room_pinned_ids() -> list[str]:
            raw = store.get("chat_rooms", store.seed_chat_rooms)
            row = next((x for x in raw if x.get("id") == rid), None)
            if not row:
                return []
            return [str(x) for x in (row.get("pinnedMessageIds") or [])]

        def on_pin_click(mid: str, currently_pinned: bool):
            def _handler(_):
                store.toggle_pin_chat_message(rid, mid)
                try:
                    store.refresh_chat_room_meta(rid)
                except Exception:
                    pass
                try:
                    fresh = store.fetch_chat_messages(rid, limit=50)
                    render_messages(fresh)
                except Exception:
                    pass
                self.toast("Đã bỏ ghim" if currently_pinned else "Đã ghim tin")

            return _handler

        def on_delete_click(mid: str):
            def _handler(_):
                if not store.delete_chat_message(rid, mid, str(my_uid)):
                    self.toast("Không xóa được tin nhắn")
                    return
                self.toast("Đã xóa tin nhắn")
                try:
                    fresh = store.fetch_chat_messages(rid, limit=50)
                    render_messages(fresh)
                except Exception:
                    pass

            return _handler

        def msg_menu(m: dict) -> ft.Control:
            mid = str(m.get("id") or m.get("_id") or "").strip()
            if not mid:
                return ft.Container(width=4)
            pins_l = room_pinned_ids()
            is_pinned = mid in pins_l
            is_mine = str(m.get("senderId") or "") == str(my_uid)
            menu_items: list = [
                ft.PopupMenuItem(
                    text="Bỏ ghim" if is_pinned else "Ghim tin nhắn",
                    on_click=on_pin_click(mid, is_pinned),
                ),
            ]
            if is_mine:
                menu_items.append(
                    ft.PopupMenuItem(
                        text="Xóa tin nhắn",
                        on_click=on_delete_click(mid),
                    ),
                )
            return ft.PopupMenuButton(
                icon=ft.Icons.MORE_HORIZ,
                icon_size=20,
                icon_color=TEXT_MUTED,
                tooltip="Thêm",
                items=menu_items,
            )

        def my_msg_footer(m: dict, members: list, last_read: dict) -> str:
            if str(m.get("senderId") or "") != str(my_uid):
                return ""
            others = [str(u) for u in members if u is not None and str(u) != str(my_uid)]
            if not others:
                return "Đã gửi"
            at = int(m.get("at") or 0)
            if at <= 0:
                return "Đã gửi"
            threshold = at - 500
            if all(int(last_read.get(str(u), 0)) >= threshold for u in others):
                return "Đã xem"
            return "Đã gửi"

        def dismiss_bg_delete() -> ft.Container:
            return ft.Container(
                bgcolor="#ef4444",
                alignment=ft.alignment.center_right,
                padding=ft.padding.only(right=16),
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.DELETE_OUTLINE, color=ft.Colors.WHITE, size=20),
                        ft.Text(
                            "Xóa",
                            color=ft.Colors.WHITE,
                            size=13,
                            weight=ft.FontWeight.W_600,
                        ),
                    ],
                    tight=True,
                    alignment=ft.MainAxisAlignment.END,
                ),
            )

        def dismiss_bg_pin() -> ft.Container:
            return ft.Container(
                bgcolor="#d97706",
                alignment=ft.alignment.center_left,
                padding=ft.padding.only(left=16),
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.PUSH_PIN_OUTLINED, color=ft.Colors.WHITE, size=20),
                        ft.Text(
                            "Ghim",
                            color=ft.Colors.WHITE,
                            size=13,
                            weight=ft.FontWeight.W_600,
                        ),
                    ],
                    tight=True,
                ),
            )

        def on_confirm_swipe(mid_sw: str, is_mine_sw: bool):
            def _h(e: ft.DismissibleDismissEvent):
                try:
                    dir_ = e.direction
                except Exception:
                    try:
                        e.control.confirm_dismiss(False)
                    except Exception:
                        pass
                    return
                if dir_ == ft.DismissDirection.START_TO_END:
                    try:
                        e.control.confirm_dismiss(False)
                    except Exception:
                        pass
                    pl = room_pinned_ids()
                    cur_pin = mid_sw in pl
                    store.toggle_pin_chat_message(rid, mid_sw)
                    try:
                        store.refresh_chat_room_meta(rid)
                    except Exception:
                        pass
                    try:
                        fr = store.fetch_chat_messages(rid, limit=50)
                        render_messages(fr)
                    except Exception:
                        pass
                    self.toast("Đã bỏ ghim" if cur_pin else "Đã ghim tin")
                    return
                if dir_ == ft.DismissDirection.END_TO_START:
                    if not is_mine_sw:
                        try:
                            e.control.confirm_dismiss(False)
                        except Exception:
                            pass
                        self.toast("Chỉ xóa được tin của bạn")
                        return
                    try:
                        e.control.confirm_dismiss(True)
                    except Exception:
                        pass

            return _h

        def on_dismiss_swipe(mid_sw: str, is_mine_sw: bool):
            def _h(e: ft.DismissibleDismissEvent):
                try:
                    if e.direction != ft.DismissDirection.END_TO_START:
                        return
                except Exception:
                    return
                if not is_mine_sw or not mid_sw:
                    return
                if not store.delete_chat_message(rid, mid_sw, str(my_uid)):
                    self.toast("Không xóa được tin nhắn")
                    try:
                        fr = store.fetch_chat_messages(rid, limit=50)
                        render_messages(fr)
                    except Exception:
                        pass
                    return
                self.toast("Đã xóa tin nhắn")
                try:
                    fr = store.fetch_chat_messages(rid, limit=50)
                    render_messages(fr)
                except Exception:
                    pass

            return _h

        # Theo dõi trạng thái render để tránh rebuild thừa
        _render_state: dict = {"ids": [], "pins": [], "sig": ""}

        def _build_msg_ctrl(m: dict, members: list, last_read: dict,
                            pins_l: list, is_last: bool) -> ft.Control:
            txt = m.get("text", "") or ""
            mid = str(m.get("id") or m.get("_id") or "").strip()
            pinned = bool(mid and mid in pins_l)
            if str(m.get("senderId") or "") == str(my_uid):
                inner = bubble_me_inner(txt, my_msg_footer(m, members, last_read), pinned)
                row_me = ft.Row(
                    [ft.Container(expand=True),
                     ft.Row([msg_menu(m), inner], spacing=2,
                            vertical_alignment=ft.CrossAxisAlignment.END)],
                    vertical_alignment=ft.CrossAxisAlignment.END,
                )
                ctrl = (ft.Dismissible(
                    content=ft.Container(content=row_me, expand=True),
                    background=dismiss_bg_delete(),
                    secondary_background=dismiss_bg_pin(),
                    dismiss_direction=ft.DismissDirection.HORIZONTAL,
                    data=mid,
                    on_confirm_dismiss=on_confirm_swipe(mid, True),
                    on_dismiss=on_dismiss_swipe(mid, True),
                ) if mid else row_me)
            else:
                inner = bubble_them_inner(txt, m.get("senderName", ""), pinned)
                row_them = ft.Row([inner, msg_menu(m)], spacing=2,
                                  vertical_alignment=ft.CrossAxisAlignment.END)
                ctrl = (ft.Dismissible(
                    content=ft.Container(content=row_them, expand=True),
                    secondary_background=dismiss_bg_pin(),
                    dismiss_direction=ft.DismissDirection.START_TO_END,
                    data=mid,
                    on_confirm_dismiss=on_confirm_swipe(mid, False),
                ) if mid else row_them)
            if is_last:
                ctrl.key = "last_msg"
            return ctrl

        def render_messages(items: list[dict], *, force_full: bool = False):
            members, last_read = room_meta()
            pins_l = room_pinned_ids()
            ordered = store.sort_chat_messages_with_pins(items, pins_l)
            new_ids = [str(m.get("id") or m.get("_id") or "") for m in ordered]

            # Xóa optimistic bubble nếu có (tránh hiện 2 lần)
            msgs.controls = [c for c in msgs.controls
                             if getattr(c, "key", None) != "optimistic_msg"]

            # Signature guard — skip rebuild if nothing changed
            sig = "|".join(new_ids) + "||" + ",".join(pins_l)
            if not force_full and sig == _render_state["sig"]:
                return
            _render_state["sig"] = sig

            prev_ids = _render_state["ids"]
            prev_pins = _render_state["pins"]
            pins_changed = pins_l != prev_pins

            # Incremental append: khi chỉ có tin mới thêm vào cuối, không cần rebuild toàn bộ
            can_append = (
                not force_full
                and not pins_changed
                and len(new_ids) > len(prev_ids)
                and new_ids[: len(prev_ids)] == prev_ids
            )

            if can_append:
                new_tail = ordered[len(prev_ids):]
                # Xóa key "last_msg" khỏi tin cuối cũ
                if msgs.controls:
                    try:
                        msgs.controls[-1].key = None
                    except Exception:
                        pass
                for i, m in enumerate(new_tail):
                    ctrl = _build_msg_ctrl(m, members, last_read, pins_l,
                                           is_last=(i == len(new_tail) - 1))
                    msgs.controls.append(ctrl)
            else:
                # Full rebuild
                header = msgs.controls[0] if msgs.controls else None
                new_controls = [header] if header else []
                for i, m in enumerate(ordered):
                    new_controls.append(
                        _build_msg_ctrl(m, members, last_read, pins_l,
                                        is_last=(i == len(ordered) - 1))
                    )
                msgs.controls = new_controls

            _render_state["ids"] = new_ids
            _render_state["pins"] = pins_l

            try:
                update_sidebar_attachments(items)
            except Exception:
                pass
            grew = len(new_ids) > len(prev_ids)
            try:
                self.page.update()
                if grew:
                    msgs.scroll_to(key="last_msg", duration=200)
            except Exception:
                pass

        # Tải tin nhắn ban đầu — ASYNC ngầm (không block UI khi mở chat)
        if store.STORE.is_bound():
            def _initial_load():
                try:
                    store.refresh_chat_room_meta(rid)
                    initial = store.fetch_chat_messages(rid, limit=50)
                    def _draw():
                        try:
                            render_messages(initial, force_full=True)
                        except Exception:
                            pass
                    if hasattr(self.page, "run_thread"):
                        self.page.run_thread(_draw)
                    else:
                        _draw()
                except Exception:
                    pass

            threading.Thread(target=_initial_load, daemon=True).start()

            # Đăng ký lắng nghe realtime trong background (tránh socketio connect block UI)
            def _start_listener():
                try:
                    def on_chat_messages(items: list[dict]):
                        render_messages(items)
                        if self.tab == "chat":
                            try:
                                self._apply_nav(self._detect_layout())
                                self.page.update()
                            except Exception:
                                pass

                    stop_fn = store.listen_chat_messages(
                        rid, on_chat_messages, interval=5.0,
                    )
                    self._chat_listener_stop = stop_fn
                except Exception:
                    self._chat_listener_stop = None

            threading.Thread(target=_start_listener, daemon=True).start()

        def send_msg(e):
            txt = (msg_input.value or "").strip()
            if not txt:
                return
            msg_input.value = ""
            # Hiển thị tin ngay lập tức (optimistic)
            if msgs.controls:
                try:
                    msgs.controls[-1].key = None
                except Exception:
                    pass
            opt_inner = bubble_me_inner(txt, "Đang gửi...", False)
            opt_row = ft.Row(
                [ft.Container(expand=True),
                 ft.Row([opt_inner], spacing=2,
                        vertical_alignment=ft.CrossAxisAlignment.END)],
                vertical_alignment=ft.CrossAxisAlignment.END,
                key="optimistic_msg",
            )
            msgs.controls.append(opt_row)
            try:
                self.page.update()
                msgs.scroll_to(key="optimistic_msg", duration=150)
            except Exception:
                pass

            # Gửi lên server trong nền — listener sẽ reconcile sau
            if store.STORE.is_bound():
                def worker():
                    try:
                        store.send_chat_message(rid, my_uid, my_name, txt)
                        # Xóa sig để lần poll tiếp theo sẽ rebuild full (xóa optimistic)
                        _render_state["sig"] = ""
                    except Exception:
                        pass
                threading.Thread(target=worker, daemon=True).start()

        send_btn = ft.IconButton(ft.Icons.SEND, icon_color=ft.Colors.WHITE,
                                 bgcolor=GREEN_MID, on_click=send_msg)
        msg_input.on_submit = send_msg

        chat_col = ft.Column(
            [
                ft.Container(
                    content=ft.Row(
                        [
                            *(
                                [ft.IconButton(ft.Icons.ARROW_BACK,
                                               on_click=lambda e: self.set_tab("chat"))]
                                if show_back else []
                            ),
                            ft.Column([ft.Text(name, size=15, weight=ft.FontWeight.BOLD),
                                       ft.Text(sub, size=11, color=TEXT_MUTED)],
                                      spacing=0, expand=True, tight=True),
                            *self._chat_header_call_btn(rid),
                            ft.IconButton(
                                ft.Icons.INFO_OUTLINE,
                                tooltip="Thông tin cuộc trò chuyện",
                                icon_color=TEXT_MUTED,
                                on_click=toggle_sidebar
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor=BG, padding=ft.padding.symmetric(horizontal=4, vertical=4),
                    border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                ),
                msgs,
                ft.Container(
                    content=ft.Row(
                        [ft.IconButton(ft.Icons.ATTACH_FILE,
                                       on_click=lambda e: chat_picker.pick_files(allow_multiple=True)),
                         msg_input, send_btn],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor=BG, padding=ft.padding.symmetric(horizontal=8, vertical=6),
                    border=ft.border.only(top=ft.BorderSide(1, BORDER)),
                ),
            ],
            spacing=0, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        return ft.Row(
            [
                chat_col,
                sidebar_container
            ],
            spacing=0, expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    # ============================================================
    # ===== VIEW: TIỆN ÍCH                                    =====
    # ============================================================

    def view_utilities(self) -> ft.Control:
        f47_camps = store.get("f47Campaigns", store.seed_f47) or []
        live_f47 = sum(1 for c in f47_camps if c.get("status") == "live")
        f47_badge = f"{live_f47} đang chạy" if live_f47 > 0 else None
        f47_badge_color = RED if live_f47 > 0 else None

        modules = [
            ("CHỨC NĂNG CHÍNH", [
                ("f47", "🛡", "Lực lượng 47", f47_badge, f47_badge_color),
                ("ctdctct", "📖", "CTĐ-CTCT", "Mới", GREEN_MID),
                ("hcqs", "⚔️", "HC - Quân sự", "Mới", GREEN_MID),
                ("pttd", "🚩", "Phong trào TĐ", None, None),
                ("units", "👥", "Quản lý quân nhân", "Cây đơn vị", GREEN_MID),
                ("schedule", "📅", "Lịch gác - Trực Ban", "Cập nhật", GREEN_MID),
                ("exams", "📝", "Thi - KT nhận thức", None, None),
                ("hygiene", "🧹", "Nội vụ vệ sinh", None, None),
                ("guests", "🤝", "Thăm - Tiếp khách", None, None),
            ])
        ]

        def util_cell(key, icon, lbl, badge, badge_color):
            controls = [
                ft.Text(icon, size=28),
                ft.Text(lbl, size=11, color=TEXT, weight=ft.FontWeight.W_600,
                        text_align=ft.TextAlign.CENTER, max_lines=2),
                ft.Container(
                    content=ft.Text(badge if badge else "X", size=9,
                                    color=ft.Colors.WHITE if badge else ft.Colors.TRANSPARENT,
                                    weight=ft.FontWeight.BOLD),
                    bgcolor=badge_color if badge else ft.Colors.TRANSPARENT,
                    border_radius=8,
                    padding=ft.padding.symmetric(horizontal=6, vertical=1),
                )
            ]
            return ft.Container(
                content=ft.Column(controls, spacing=4,
                                  horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
                bgcolor=BG, padding=ft.padding.symmetric(vertical=14, horizontal=6),
                expand=True, alignment=ft.alignment.center,
                on_click=lambda e, k=key: self.open_module(k),
                ink=True,
            )

        sections = []
        for title, items in modules:
            rows = []
            for i in range(0, len(items), 3):
                cells = items[i:i + 3]
                while len(cells) < 3:
                    cells.append(("", "", "", None, None))
                rows.append(
                    ft.Row([util_cell(*c) if c[0] else ft.Container(expand=True, bgcolor=BG) for c in cells],
                           spacing=1)
                )
            sections.append(
                ft.Container(
                    content=ft.Column(
                        [ft.Text(title, size=11, color=TEXT_MUTED, weight=ft.FontWeight.BOLD),
                         ft.Container(content=ft.Column(rows, spacing=1),
                                      bgcolor=BORDER, border_radius=12, clip_behavior=ft.ClipBehavior.HARD_EDGE)],
                        spacing=8,
                    ),
                    padding=ft.padding.symmetric(horizontal=8, vertical=8),
                )
            )
        return ft.ListView(controls=sections, expand=True, padding=8)

    def open_module(self, key: str) -> None:
        # Track current sub-module để header hiện tên đúng + back arrow
        self.current_module = key
        self.body.content = self.view_module(key)
        self.refresh()

    # ============================================================
    # ===== VIEW: MODULE GENERIC                              =====
    # ============================================================

    def view_module(self, key: str) -> ft.Control:
        if key == "schedule":
            return self.module_schedule()
        if key == "reports":
            return self.module_reports()
        if key == "awards":
            return self.module_awards()
        if key == "units":
            return self.module_units()
        if key == "f47":
            return self.module_f47()
        if key == "ctdctct":
            return self.module_ctdctct()
        if key == "hcqs":
            return self.module_hcqs()
        if key == "pttd":
            return self.module_pttd()
        if key == "guests":
            return self.module_guests()
        if key == "exams":
            return self.module_exams()
        if key == "notifs":
            return self.view_all_notifs()
        # default placeholder
        module_titles_fallback = {
            "guests": "Thăm - Tiếp khách",
            "exams": "Thi - Kiểm tra nhận thức",
            "hygiene": "Nội vụ vệ sinh",
            "hcqs": "Hành chính - Quân sự",
            "pttd": "Phong trào thi đua",
        }
        title_str = module_titles_fallback.get(key, f"Module: {key}")

        return ft.Container(
            content=ft.Column(
                [
                    ft.Container(
                        content=ft.Row([
                            ft.IconButton(ft.Icons.ARROW_BACK,
                                          on_click=lambda e: self.set_tab("util")),
                            ft.Text(title_str, size=16, weight=ft.FontWeight.BOLD),
                        ]),
                        bgcolor=BG, padding=8,
                        border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                    ),
                    ft.Container(
                        content=ft.Text(
                            f"Tiện ích {title_str} đang được triển khai. Xem mã nguồn để mở rộng.",
                            color=TEXT_MUTED, size=13,
                        ),
                        padding=20,
                    ),
                ],
                spacing=0,
            ),
            expand=True, bgcolor=BG2,
        )

    def module_back_bar(self, title: str, trailing: ft.Control | None = None) -> ft.Container:
        row_content = [
            ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: self.set_tab("util")),
            ft.Text(title, size=16, weight=ft.FontWeight.BOLD, expand=True)
        ]
        if trailing:
            row_content.append(trailing)
        return ft.Container(
            content=ft.Row(
                row_content,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=BG, padding=ft.padding.symmetric(horizontal=4, vertical=4),
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
        )

    # ---------- Module: Lịch gác & Trực ban ----------
    SCHEDULE_THEME = ft.Colors.TEAL_700

    def _sched_get_my_context(self):
        """Lấy thông tin phân cấp của user hiện tại."""
        profile = store.get("userProfile", store.seed_user_profile)
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or ""
        my_level = int(profile.get("adminLevel") or 0)
        my_unit = profile.get("unitId") or ""
        my_role = (profile.get("role") or "").lower()
        return my_uid, my_level, my_unit, my_role, profile

    def _sched_visible(self, entry: dict) -> bool:
        """Kiểm tra user hiện tại có quyền xem entry này không (phân cấp)."""
        my_uid, my_level, my_unit, my_role, _ = self._sched_get_my_context()
        if self._is_admin() or my_level >= 4:
            return True
        entry_unit = entry.get("unit_id", "")
        entry_level = entry.get("level", "")
        if entry.get("creator_id") == my_uid or entry.get("approver_id") == my_uid:
            return True
        if entry_level in ("trung_doan",):
            return True  # Lịch trung đoàn ai cũng xem được
        if my_unit and entry_unit and (my_unit == entry_unit or entry_unit.startswith(my_unit)):
            return True
        # Cấp tiểu đoàn trưởng thấy các đại đội con
        if my_level >= 3:
            return True
        return False

    def _sched_approver_candidates(self, level: str, unit_id: str) -> list[dict]:
        """Danh sách chỉ huy có thể phê duyệt tùy theo cấp."""
        soldiers = store.get("soldiers", store.seed_soldiers)
        out = []
        for s in soldiers:
            role = (s.get("role") or "").lower()
            s_level = int(s.get("adminLevel") or 0)
            if level == "trung_doan":
                if any(k in role for k in ["tham mưu trưởng", "phó tham mưu"]):
                    out.append(s)
            elif level == "tieu_doan":
                if s_level >= 3 and any(k in role for k in ["tiểu đoàn trưởng"]):
                    out.append(s)
            elif level == "dai_doi":
                if s_level >= 2 and any(k in role for k in ["đại đội trưởng", "phó đại đội"]):
                    out.append(s)
        return out

    def _sched_can_create(self, tab_key: str) -> bool:
        """Kiểm tra user có quyền tạo lịch gác/trực ban không."""
        _, my_level, _, my_role, _ = self._sched_get_my_context()
        if self._is_admin():
            return True
        if tab_key == "trucban":
            if any(k in my_role for k in ["trợ lý", "tham mưu", "tiểu đoàn trưởng",
                                           "phó tiểu đoàn", "đại đội trưởng", "phó đại đội",
                                           "trung đội trưởng"]):
                return True
        else:  # gac
            if my_level >= 1:
                return True
        return my_level >= 2

    def module_schedule(self) -> ft.Control:
        page = self.page
        tab_key = getattr(self, "_sched_tab", "gac")  # "gac" | "trucban"
        time_filter = getattr(self, "_sched_filter", "day")  # "day" | "week" | "month"
        all_entries = store.get("scheduleEntries", lambda: [])
        now = store.now_ms()
        today_date = datetime.fromtimestamp(now / 1000).date()

        # ---- Lọc theo tab & thời gian ----
        filtered = [e for e in all_entries if e.get("type") == tab_key and self._sched_visible(e)]
        if time_filter == "day":
            filtered = [e for e in filtered if e.get("date_str") == today_date.strftime("%Y-%m-%d")]
        elif time_filter == "week":
            from datetime import timedelta
            week_start = today_date - timedelta(days=today_date.weekday())
            week_end = week_start + timedelta(days=6)
            filtered = [e for e in filtered
                        if week_start.strftime("%Y-%m-%d") <= (e.get("date_str") or "") <= week_end.strftime("%Y-%m-%d")]
        elif time_filter == "month":
            m_prefix = today_date.strftime("%Y-%m")
            filtered = [e for e in filtered if (e.get("date_str") or "").startswith(m_prefix)]

        filtered.sort(key=lambda e: e.get("date_str", ""), reverse=True)

        # ---- Status badge ----
        def status_badge(status: str) -> ft.Container:
            cfg = {"approved": (GREEN_LIGHT, GREEN_DARK, "✅ Đã duyệt"),
                   "pending": ("#faeeda", "#633806", "⏳ Chờ duyệt"),
                   "rejected": ("#fcebeb", "#791f1f", "❌ Từ chối")}
            bg, fg, lbl = cfg.get(status, ("#eee", TEXT, status))
            return ft.Container(
                content=ft.Text(lbl, size=10, color=fg, weight=ft.FontWeight.BOLD),
                bgcolor=bg, border_radius=8,
                padding=ft.padding.symmetric(horizontal=8, vertical=3),
            )

        # ---- Level label ----
        def level_label(lvl: str) -> str:
            return {"trung_doan": "🏛 Trung đoàn", "tieu_doan": "🎖 Tiểu đoàn",
                    "dai_doi": "🛡 Đại đội"}.get(lvl, lvl)

        # ---- Entry card ----
        def entry_card(e: dict) -> ft.Container:
            soldiers = store.get("soldiers", store.seed_soldiers)
            creator = next((s for s in soldiers if str(s.get("id")) == str(e.get("creator_id"))), {})
            approver = next((s for s in soldiers if str(s.get("id")) == str(e.get("approver_id"))), {})
            data = e.get("data") or {}
            if e.get("type") == "gac" and isinstance(data.get("ca"), list):
                vong = data.get("vong", "")
                ca_list = data.get("ca", [])
                has_units = any(c.get("unit_name") for c in ca_list)
                if has_units:
                    unit_names = [c.get("unit_name","?") for c in ca_list if c.get("unit_name")]
                    unique = list(dict.fromkeys(unit_names))
                    filled = sum(1 for c in ca_list if c.get("people"))
                    detail_str = f"🛡 {vong} • {', '.join(unique[:4])}"
                    if filled:
                        detail_str += f" • {filled} ca đã phân người"
                else:
                    filled = sum(1 for c in ca_list if c.get("people"))
                    detail_str = f"🛡 {vong} • {filled}/{len(ca_list)} ca đã phân"
            else:
                detail_parts = []
                for k, v in data.items():
                    if v and isinstance(v, str):
                        detail_parts.append(f"{k}: {v}")
                detail_str = " • ".join(detail_parts[:3]) if detail_parts else "Chưa có chi tiết"
            reject_note = e.get("reject_reason") or ""

            my_uid = AUTH_STATE.get("uid") or ""
            is_approver = str(e.get("approver_id")) == my_uid and e.get("status") == "pending"

            # Kiểm tra nếu đơn vị mình được phân ca (để hiện nút Phân người)
            _, my_level_card, my_unit_card, _, _ = self._sched_get_my_context()
            ca_list_for_me = []
            if e.get("type") == "gac" and isinstance(data.get("ca"), list) and e.get("status") == "approved":
                for idx_ca, ca in enumerate(data.get("ca", [])):
                    if ca.get("unit_id") == my_unit_card and not ca.get("people"):
                        ca_list_for_me.append((idx_ca, ca))

            def _assign_people(ev):
                """Mở BottomSheet để chỉ huy đơn vị phân người cho các ca được giao."""
                my_soldiers_list = [s for s in soldiers if s.get("unitId") == my_unit_card and not s.get("isAdmin")]
                all_ca = data.get("ca", [])
                # Chỉ hiện ca chưa có người + thuộc đơn vị mình
                my_ca_indices = [i for i, c in enumerate(all_ca) if c.get("unit_id") == my_unit_card and not c.get("people")]
                if not my_ca_indices:
                    self.toast("Tất cả ca đã được phân người"); return

                # Tạo dict: ca_idx -> list of checkboxes
                ca_cbs: dict[int, list[ft.Checkbox]] = {}
                controls = []
                for ci in my_ca_indices:
                    ca = all_ca[ci]
                    cbs = []
                    for s in my_soldiers_list:
                        sid = str(s.get("id", ""))
                        cb = ft.Checkbox(label=f"{s.get('name','')} — {s.get('role','')}", data=sid)
                        cbs.append(cb)
                    ca_cbs[ci] = cbs
                    controls.append(ft.Text(f"Ca {ci+1} • {ca.get('time','')}", size=13,
                                            weight=ft.FontWeight.BOLD))
                    controls.extend(cbs)
                    controls.append(ft.Divider(height=1))

                _bs_ref1 = [None]

                def _close():
                    if _bs_ref1[0]:
                        _bs_ref1[0].open = False
                    page.update()

                def _save(ev2):
                    vong_name = data.get("vong", "")
                    date = e.get("date_str", "")
                    _assigner = store.get("userProfile", store.seed_user_profile).get("name") or "Chỉ huy"
                    for ci, cbs in ca_cbs.items():
                        selected = [cb.data for cb in cbs if cb.value]
                        if selected:
                            all_ca[ci]["people"] = selected
                            for pid in selected:
                                store.push_notif("unit", "🛡 Bạn được cắt gác",
                                                 f"{vong_name} • {all_ca[ci]['time']} • {date}",
                                                 "schedule", target_uid=pid,
                                                 sender_name=_assigner)
                    store.set_value("scheduleEntries", all_entries)
                    _close()
                    self.toast("✅ Đã phân người")
                    self.body.content = self.module_schedule()
                    self.refresh()

                _bs1 = ft.BottomSheet(
                    content=ft.Container(
                        content=ft.Column([
                            ft.Row([
                                ft.Text("Phân người gác", size=16, weight=ft.FontWeight.BOLD, expand=True),
                                ft.TextButton("Huỷ", on_click=lambda e: _close()),
                                ft.ElevatedButton("Lưu", bgcolor=self.SCHEDULE_THEME,
                                                  color=ft.Colors.WHITE, on_click=_save),
                            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                            ft.Divider(height=1),
                            ft.ListView(controls=controls, spacing=4, expand=True),
                        ], spacing=6, expand=True),
                        padding=16, height=480,
                    ),
                    open=True,
                )
                _bs_ref1[0] = _bs1
                page.overlay.append(_bs1)
                page.update()

            def _approve(ev):
                _approver_name = store.get("userProfile", store.seed_user_profile).get("name") or "Chỉ huy"
                e["status"] = "approved"
                store.set_value("scheduleEntries", all_entries)
                store.push_notif("success", "✅ Đã phê duyệt lịch",
                                 f"{e.get('date_str')} - {level_label(e.get('level',''))}",
                                 "schedule", target_uid=e.get("creator_id"),
                                 sender_name=_approver_name)
                self.toast("✅ Đã phê duyệt")
                self.body.content = self.module_schedule()
                self.refresh()

            def _reject(ev):
                reason_input = ft.TextField(label="Lý do từ chối *", border_radius=8,
                                            dense=True, multiline=True, min_lines=2)
                def do_reject(ev2):
                    reason = (reason_input.value or "").strip()
                    if not reason:
                        self.toast("⚠️ Vui lòng nhập lý do"); return
                    _rejecter_name = store.get("userProfile", store.seed_user_profile).get("name") or "Chỉ huy"
                    e["status"] = "rejected"
                    e["reject_reason"] = reason
                    store.set_value("scheduleEntries", all_entries)
                    store.push_notif("warning", "❌ Lịch bị từ chối",
                                     f"{e.get('date_str')} - {level_label(e.get('level',''))}: {reason[:50]}",
                                     "schedule", target_uid=e.get("creator_id"),
                                     sender_name=_rejecter_name)
                    _dlg.open = False; page.update()
                    self.toast("Đã từ chối")
                    self.body.content = self.module_schedule()
                    self.refresh()

                _dlg = ft.AlertDialog(
                    title=ft.Text("Từ chối phê duyệt", size=16, weight=ft.FontWeight.BOLD),
                    content=ft.Column([reason_input], tight=True),
                    actions=[
                        ft.TextButton("Huỷ", on_click=lambda e: _close_dialog(self.page)),
                        ft.ElevatedButton("Từ chối", bgcolor="#d32f2f", color=ft.Colors.WHITE, on_click=do_reject),
                    ],
                )
                _show_dialog(self.page, _dlg)

            action_row = []
            if is_approver:
                action_row = [
                    ft.ElevatedButton("Duyệt", bgcolor=GREEN_DARK, color=ft.Colors.WHITE, on_click=_approve, height=32),
                    ft.OutlinedButton("Từ chối", on_click=_reject, height=32),
                ]
            if ca_list_for_me:
                action_row.append(
                    ft.ElevatedButton("Phân người", bgcolor=self.SCHEDULE_THEME,
                                      color=ft.Colors.WHITE, on_click=_assign_people, height=32,
                                      icon=ft.Icons.PEOPLE_OUTLINE),
                )

            children = [
                ft.Row([
                    ft.Text(f"📅 {e.get('date_str','')}", size=13, weight=ft.FontWeight.BOLD, expand=True),
                    status_badge(e.get("status", "pending")),
                ]),
                ft.Text(f"{level_label(e.get('level',''))} • QS: {e.get('troop_count','--')}",
                        size=12, color=TEXT_MUTED),
                ft.Text(detail_str, size=11, color=TEXT_MUTED, max_lines=2),
                ft.Row([
                    ft.Text(f"Người cắt: đ.c {creator.get('name','?')}", size=11, color=TEXT_MUTED),
                    ft.Text(f"Duyệt: đ.c {approver.get('name','?')}", size=11, color=TEXT_MUTED),
                ], spacing=10),
            ]
            if reject_note:
                children.append(ft.Container(
                    content=ft.Text(f"📝 Ghi chú: {reject_note}", size=11, color="#791f1f"),
                    bgcolor="#fcebeb", border_radius=6, padding=6, margin=ft.margin.only(top=4),
                ))
            if action_row:
                children.append(ft.Row(action_row, spacing=8))

            return ft.Container(
                content=ft.Column(children, spacing=4),
                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
                padding=12, margin=ft.margin.only(bottom=8),
            )

        # ---- Tab change ----
        def on_tab_changed(e):
            try:
                idx = e.control.selected_index
            except Exception:
                idx = 0
            self._sched_tab = "gac" if idx == 0 else "trucban"
            self.body.content = self.module_schedule()
            self.refresh()

        # ---- Time filter change ----
        def on_filter(val):
            self._sched_filter = val
            self.body.content = self.module_schedule()
            self.refresh()

        # ---- Export Schedule ----
        def _export_schedule(e):
            headers = ["📅 Ngày", "Cấp", "Quân số", "Trạng thái", "Loại", "Chi tiết", "Người cắt", "Duyệt"]
            rows = []
            soldiers = store.get("soldiers", store.seed_soldiers)
            for item in filtered:
                creator = next((s for s in soldiers if str(s.get("id")) == str(item.get("creator_id"))), {})
                approver = next((s for s in soldiers if str(s.get("id")) == str(item.get("approver_id"))), {})
                data = item.get("data") or {}
                
                # Format detail_str
                if item.get("type") == "gac" and isinstance(data.get("ca"), list):
                    vong = data.get("vong", "")
                    ca_list = data.get("ca", [])
                    has_units = any(c.get("unit_name") for c in ca_list)
                    if has_units:
                        unit_names = [c.get("unit_name","?") for c in ca_list if c.get("unit_name")]
                        unique = list(dict.fromkeys(unit_names))
                        filled = sum(1 for c in ca_list if c.get("people"))
                        detail_str = f"🛡 {vong} • {', '.join(unique)}"
                        if filled:
                            detail_str += f" • {filled} ca đã phân người"
                    else:
                        filled = sum(1 for c in ca_list if c.get("people"))
                        detail_str = f"🛡 {vong} • {filled}/{len(ca_list)} ca đã phân"
                else:
                    detail_parts = []
                    for k, v in data.items():
                        if v and isinstance(v, str):
                            detail_parts.append(f"{k}: {v}")
                    detail_str = " • ".join(detail_parts) if detail_parts else "Chưa có chi tiết"
                    
                rows.append([
                    item.get("date_str", ""),
                    level_label(item.get("level", "")),
                    item.get("troop_count", "--"),
                    item.get("status", "pending"),
                    "Lịch gác" if item.get("type") == "gac" else "Trực ban",
                    detail_str,
                    f"đ.c {creator.get('name', '')}",
                    f"đ.c {approver.get('name', '')}"
                ])
            self.export_data_to_csv("BaoCao_LichTruc", headers, rows)

        sel_tab = 0 if tab_key == "gac" else 1
        tabs_bar = ft.Container(
            content=ft.Row([
                ft.Container(
                    content=ft.Tabs(
                        selected_index=sel_tab, on_change=on_tab_changed,
                        tabs=[ft.Tab(text="Lịch gác"), ft.Tab(text="Trực ban")],
                        height=46,
                    ), expand=True, height=46,
                ),
                ft.IconButton(
                    icon=ft.Icons.FILE_DOWNLOAD,
                    icon_color=GREEN_MID,
                    tooltip="Xuất Excel/CSV",
                    on_click=_export_schedule
                )
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
            bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            padding=ft.padding.only(left=2, right=4, top=2, bottom=2),
        )

        # Filter bar
        def _fbtn(label, key):
            is_sel = time_filter == key
            return ft.Container(
                content=ft.Text(label, size=12, color=ft.Colors.WHITE if is_sel else TEXT_MUTED,
                                weight=ft.FontWeight.BOLD if is_sel else ft.FontWeight.NORMAL),
                bgcolor=self.SCHEDULE_THEME if is_sel else BORDER,
                border_radius=16, padding=ft.padding.symmetric(horizontal=14, vertical=6),
                on_click=lambda e, k=key: on_filter(k),
            )

        filter_bar = ft.Container(
            content=ft.Row([_fbtn("Ngày", "day"), _fbtn("Tuần", "week"), _fbtn("Tháng", "month")],
                           spacing=6),
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
        )

        # List
        if filtered:
            list_content = ft.Column([entry_card(e) for e in filtered], spacing=0)
        else:
            type_name = "lịch gác" if tab_key == "gac" else "trực ban"
            time_name = {"day": "hôm nay", "week": "tuần này", "month": "tháng này"}.get(time_filter, "")
            list_content = ft.Column([
                ft.Container(height=60),
                ft.Text(f"📭 Chưa có {type_name} {time_name}", text_align=ft.TextAlign.CENTER,
                        color=TEXT_MUTED, size=14),
                ft.Container(height=10),
                ft.Text("Bấm nút + ở góc phải để cắt lịch", text_align=ft.TextAlign.CENTER,
                        color=TEXT_MUTED, size=12),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER)

        body = ft.ListView(controls=[ft.Container(content=list_content, padding=10)], expand=True)

        # FAB
        fab_label = "Cắt gác" if tab_key == "gac" else "Cắt trực ban"
        can_create = self._sched_can_create(tab_key)
        fab = None
        if can_create:
            fab = ft.Container(
                content=ft.FloatingActionButton(
                    icon=ft.Icons.ADD,
                    bgcolor=self.SCHEDULE_THEME,
                    foreground_color=ft.Colors.WHITE,
                    tooltip=fab_label,
                    on_click=lambda e: self._sched_open_create(tab_key),
                ),
                right=16, bottom=16,
            )

        main_col = ft.Column([tabs_bar, filter_bar, body], spacing=0, expand=True)
        if fab:
            return ft.Stack([main_col, fab], expand=True)
        return main_col

    # Cấu hình loại ca gác
    SHIFT_TYPES = {
        "2h": {"label": "2 tiếng / ca", "slots": [
            ("18:00–20:00",), ("20:00–22:00",), ("22:00–00:00",),
            ("00:00–02:00",), ("02:00–04:00",), ("04:00–06:00",),
            ("06:00–08:00",), ("08:00–10:00",), ("10:00–12:00",),
            ("12:00–14:00",), ("14:00–16:00",), ("16:00–18:00",),
        ]},
        "1.5h": {"label": "1,5 tiếng / ca", "slots": [
            ("18:00–19:30",), ("19:30–21:00",), ("21:00–22:30",),
            ("22:30–00:00",), ("00:00–01:30",), ("01:30–03:00",),
            ("03:00–04:30",), ("04:30–06:00",), ("06:00–07:30",),
            ("07:30–09:00",), ("09:00–10:30",), ("10:30–12:00",),
            ("12:00–13:30",), ("13:30–15:00",), ("15:00–16:30",),
            ("16:30–18:00",),
        ]},
        "3h": {"label": "3 tiếng / ca", "slots": [
            ("18:00–21:00",), ("21:00–00:00",), ("00:00–03:00",),
            ("03:00–06:00",), ("06:00–09:00",), ("09:00–12:00",),
            ("12:00–15:00",), ("15:00–18:00",),
        ]},
        "mixed": {"label": "Hỗn hợp (3h ngày, 1h đêm)", "slots": [
            ("06:00–09:00",), ("09:00–11:00",), ("11:00–14:00",),
            ("14:00–17:00",), ("17:00–19:00",), ("19:00–21:00",),
            ("21:00–23:00",), ("23:00–00:00",), ("00:00–01:00",),
            ("01:00–02:00",), ("02:00–03:00",), ("03:00–04:00",),
            ("04:00–05:00",), ("05:00–06:00",),
        ]},
    }

    def _sched_open_create(self, tab_key: str):
        """Dialog tạo lịch gác / trực ban mới."""
        page = self.page
        _, my_level, my_unit, my_role, profile = self._sched_get_my_context()
        my_uid = AUTH_STATE.get("uid") or ""
        type_name = "Cắt gác" if tab_key == "gac" else "Cắt trực ban"

        # Ngày mặc định = hôm nay
        today_str = datetime.fromtimestamp(store.now_ms() / 1000).strftime("%Y-%m-%d")
        date_input = ft.TextField(label="Ngày *", value=today_str,
                                  border_radius=8, dense=True, read_only=(tab_key == "gac"))

        # Cấp
        level_options = []
        if my_level >= 4 or self._is_admin():
            level_options.append(ft.dropdown.Option("trung_doan", "Cấp Trung đoàn"))
        if my_level >= 3 or self._is_admin():
            level_options.append(ft.dropdown.Option("tieu_doan", "Cấp Tiểu đoàn"))
        level_options.append(ft.dropdown.Option("dai_doi", "Cấp Đại đội"))
        default_lvl = level_options[0].key if level_options else "dai_doi"
        level_dd = ft.Dropdown(label="Cấp *", options=level_options, value=default_lvl,
                               border_radius=8, dense=True)

        # Quân số
        troop_input = ft.TextField(label="Quân số (VD: 85/90)", border_radius=8, dense=True)

        # Approver
        approver_dd = ft.Dropdown(label="Người phê duyệt *", options=[], border_radius=8, dense=True)

        def _refresh_approvers(e=None):
            lvl = level_dd.value or "dai_doi"
            candidates = self._sched_approver_candidates(lvl, my_unit)
            approver_dd.options = [
                ft.dropdown.Option(str(s.get("id")), f"{s.get('name','')} ({s.get('role','')})")
                for s in candidates
            ]
            approver_dd.value = str(candidates[0]["id"]) if candidates else None
            try:
                page.update()
            except Exception:
                pass

        level_dd.on_change = _refresh_approvers

        err_text = ft.Text("", color="red", size=12)

        if tab_key == "gac":
            # ---- CHẾ ĐỘ CẮT GÁC ----
            vong_input = ft.TextField(label="Tên vọng gác *", hint_text="VD: Vọng cổng chính",
                                      border_radius=8, dense=True)

            shift_type_dd = ft.Dropdown(
                label="Loại ca gác *",
                options=[ft.dropdown.Option(k, v["label"]) for k, v in self.SHIFT_TYPES.items()],
                value="2h", border_radius=8, dense=True,
            )

            slots_column = ft.Column([], spacing=6, scroll=ft.ScrollMode.AUTO)
            soldiers = store.get("soldiers", store.seed_soldiers)
            tree = store.seed_units()

            def _get_dau_moi(level_val: str) -> list[dict]:
                """Danh sách đầu mối đơn vị theo cấp."""
                result = []
                if level_val == "trung_doan":
                    for ch in tree.get("children", []):
                        ctype = ch.get("type", "")
                        if ctype in ("department", "company", "battalion"):
                            result.append({"id": ch["id"], "name": ch["name"]})
                elif level_val == "tieu_doan":
                    for ch in tree.get("children", []):
                        if ch.get("type") == "battalion":
                            if ch.get("id") == my_unit:
                                for sub in ch.get("children", []):
                                    if sub.get("type") == "company":
                                        result.append({"id": sub["id"], "name": sub["name"]})
                                break
                return result

            slot_selections: dict = {}

            def _open_unit_picker(slot_idx: int, time_range: str):
                lvl = level_dd.value or "dai_doi"
                dau_moi = _get_dau_moi(lvl)
                current = slot_selections.get(slot_idx, {})
                current_uid = current.get("unit_id", "") if isinstance(current, dict) else ""
                used_other = set()
                for idx, sel in slot_selections.items():
                    if idx != slot_idx and isinstance(sel, dict):
                        used_other.add(sel.get("unit_id", ""))

                radios = []
                for u in dau_moi:
                    is_used = u["id"] in used_other
                    lbl = u["name"] + ("  (đã cắt ca khác)" if is_used else "")
                    radios.append(ft.Radio(value=u["id"], label=lbl, disabled=is_used))

                rg = ft.RadioGroup(
                    content=ft.Column(radios, spacing=2, scroll=ft.ScrollMode.AUTO),
                    value=current_uid,
                )

                def _close():
                    if _bs_ref2[0]:
                        _bs_ref2[0].open = False
                    page.update()

                def _done(ev):
                    sel_id = rg.value or ""
                    sel_name = next((u["name"] for u in dau_moi if u["id"] == sel_id), "")
                    slot_selections[slot_idx] = {"unit_id": sel_id, "unit_name": sel_name}
                    _close()
                    _rebuild_slots()
                    page.update()

                _bs2 = ft.BottomSheet(
                    content=ft.Container(
                        content=ft.Column([
                            ft.Row([
                                ft.Text(f"Ca {slot_idx+1} • {time_range}", size=16,
                                        weight=ft.FontWeight.BOLD, expand=True),
                                ft.TextButton("Huỷ", on_click=lambda e: _close()),
                                ft.ElevatedButton("Xong", bgcolor=self.SCHEDULE_THEME,
                                                  color=ft.Colors.WHITE, on_click=_done),
                            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                            ft.Text("Chọn đơn vị gác ca này:", size=13, color=TEXT_MUTED),
                            ft.Divider(height=1),
                            rg,
                        ], spacing=6, expand=True),
                        padding=16, height=420,
                    ),
                    open=True,
                )
                page.overlay.append(_bs2)
                page.update()

            def _open_person_picker(slot_idx: int, time_range: str):
                current = list(slot_selections.get(slot_idx, []) if isinstance(slot_selections.get(slot_idx), list) else [])
                used_other = set()
                for idx, ids in slot_selections.items():
                    if idx != slot_idx and isinstance(ids, list):
                        used_other.update(ids)

                my_soldiers = [s for s in soldiers if s.get("unitId") == my_unit and not s.get("isAdmin")]
                cbs = []
                for s in my_soldiers:
                    sid = str(s.get("id", ""))
                    is_used = sid in used_other and sid not in current
                    lbl = f"{s.get('name','')} — {s.get('role','')}"
                    if is_used:
                        lbl += "  (đã cắt ca khác)"
                    cbs.append(ft.Checkbox(label=lbl, value=sid in current, disabled=is_used, data=sid))

                def _close():
                    page.bottom_sheet.open = False
                    page.update()

                def _done(ev):
                    slot_selections[slot_idx] = [cb.data for cb in cbs if cb.value and not cb.disabled]
                    _close()
                    _rebuild_slots()
                    page.update()

                _bs3 = ft.BottomSheet(
                    content=ft.Container(
                        content=ft.Column([
                            ft.Row([
                                ft.Text(f"Ca {slot_idx+1} • {time_range}", size=16,
                                        weight=ft.FontWeight.BOLD, expand=True),
                                ft.TextButton("Huỷ", on_click=lambda e: _close()),
                                ft.ElevatedButton("Xong", bgcolor=self.SCHEDULE_THEME,
                                                  color=ft.Colors.WHITE, on_click=_done),
                            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                            ft.Text("Chọn ít nhất 2 người:", size=13, color=TEXT_MUTED),
                            ft.Divider(height=1),
                            ft.ListView(controls=cbs, spacing=2, expand=True),
                        ], spacing=6, expand=True),
                        padding=16, height=450,
                    ),
                    open=True,
                )
                page.overlay.append(_bs3)
                page.update()

            def _rebuild_slots(e=None):
                st = shift_type_dd.value or "2h"
                lvl = level_dd.value or "dai_doi"
                is_unit_level = lvl in ("trung_doan", "tieu_doan")
                slot_defs = self.SHIFT_TYPES.get(st, self.SHIFT_TYPES["2h"])["slots"]
                slots_column.controls.clear()
                for i, (time_range,) in enumerate(slot_defs):
                    sel = slot_selections.get(i)
                    if is_unit_level:
                        if isinstance(sel, dict) and sel.get("unit_name"):
                            names_str = sel["unit_name"]
                            btn_color = self.SCHEDULE_THEME
                            count_str = "✓"
                        else:
                            names_str = "Chưa chọn đơn vị"
                            btn_color = TEXT_MUTED
                            count_str = ""
                    else:
                        if isinstance(sel, list) and sel:
                            names = [next((s.get("name","") for s in soldiers
                                           if str(s.get("id")) == sid), sid) for sid in sel]
                            names_str = ", ".join(names)
                            btn_color = self.SCHEDULE_THEME
                            count_str = f"({len(sel)} người)"
                        else:
                            names_str = "Chưa chọn"
                            btn_color = TEXT_MUTED
                            count_str = ""

                    picker_fn = _open_unit_picker if is_unit_level else _open_person_picker
                    slot_row = ft.Container(
                        content=ft.Column([
                            ft.Row([
                                ft.Container(
                                    content=ft.Text(f"Ca {i+1}", size=11,
                                                    weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                                    bgcolor=self.SCHEDULE_THEME, border_radius=6,
                                    padding=ft.padding.symmetric(horizontal=8, vertical=4),
                                ),
                                ft.Text(time_range, size=12, weight=ft.FontWeight.W_600),
                                ft.Text(count_str, size=11, color=btn_color),
                            ], spacing=8),
                            ft.Container(
                                content=ft.Text(names_str, size=11, color=btn_color, max_lines=2),
                                padding=ft.padding.only(left=8),
                            ),
                        ], spacing=2, tight=True),
                        border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                        padding=ft.padding.symmetric(vertical=6, horizontal=4),
                        on_click=lambda ev, idx=i, tr=time_range: picker_fn(idx, tr),
                    )
                    slots_column.controls.append(slot_row)
                try:
                    page.update()
                except Exception:
                    pass

            def _on_level_change(e=None):
                _refresh_approvers(e)
                slot_selections.clear()
                _rebuild_slots(e)

            level_dd.on_change = _on_level_change
            shift_type_dd.on_change = _rebuild_slots
            _rebuild_slots()
            _refresh_approvers()

            def do_save_gac(ev):
                vong = (vong_input.value or "").strip()
                date_str = (date_input.value or "").strip()
                approver_id = approver_dd.value or ""
                lvl = level_dd.value or "dai_doi"
                if not vong or not date_str or not approver_id:
                    err_text.value = "⚠️ Tên vọng, Ngày, Người duyệt là bắt buộc"
                    page.update(); return

                st = shift_type_dd.value or "2h"
                slot_defs = self.SHIFT_TYPES.get(st, self.SHIFT_TYPES["2h"])["slots"]
                is_unit_level = lvl in ("trung_doan", "tieu_doan")
                ca_data = []
                for i, (time_range,) in enumerate(slot_defs):
                    sel = slot_selections.get(i)
                    if is_unit_level and isinstance(sel, dict):
                        ca_data.append({"time": time_range,
                                        "unit_id": sel.get("unit_id", ""),
                                        "unit_name": sel.get("unit_name", ""),
                                        "people": []})
                    elif isinstance(sel, list):
                        ca_data.append({"time": time_range, "unit_id": "", "unit_name": "", "people": sel})
                    else:
                        ca_data.append({"time": time_range, "unit_id": "", "unit_name": "", "people": []})

                _sched_creator = profile.get("name") or profile.get("username") or "Chỉ huy"
                if is_unit_level:
                    notified = set()
                    for ca in ca_data:
                        uid = ca.get("unit_id", "")
                        if uid and uid not in notified:
                            notified.add(uid)
                            def _find(node, tid):
                                if node.get("id") == tid: return node
                                for ch in node.get("children", []):
                                    r = _find(ch, tid)
                                    if r: return r
                                return None
                            unode = _find(tree, uid)
                            cmd_id = unode.get("commanderId") if unode else None
                            if cmd_id:
                                store.push_notif("unit", "🛡 Đơn vị được cắt gác",
                                                 f"{ca.get('unit_name','')} • {vong} • {date_str}",
                                                 "schedule", target_uid=cmd_id,
                                                 sender_name=_sched_creator)
                else:
                    for ca in ca_data:
                        for pid in ca.get("people", []):
                            store.push_notif("unit", "🛡 Bạn được cắt gác",
                                             f"{vong} • {ca['time']} • {date_str}",
                                             "schedule", target_uid=pid,
                                             sender_name=_sched_creator)

                entry = {
                    "id": f"sc-{store.now_ms()}", "type": "gac", "level": lvl,
                    "unit_id": my_unit, "date_str": date_str,
                    "troop_count": troop_input.value or "",
                    "data": {"vong": vong, "shift_type": st, "ca": ca_data},
                    "creator_id": my_uid, "approver_id": approver_id,
                    "status": "pending", "reject_reason": "", "created_at": store.now_ms(),
                }
                entries = store.get("scheduleEntries", lambda: [])
                entries.append(entry)
                store.set_value("scheduleEntries", entries)
                store.log_activity(f"Cắt gác: {vong} - {date_str}")
                store.push_notif("unit", "📋 Lịch gác mới cần duyệt",
                                 f"{_sched_creator}: {vong} • {date_str}", "schedule",
                                 target_uid=approver_id, sender_name=_sched_creator)
                _dlg.open = False; page.update()
                self.toast("✅ Đã cắt gác")
                self.body.content = self.module_schedule()
                self.refresh()

            content_col = ft.Column([
                date_input, level_dd, troop_input, vong_input,
                ft.Divider(height=1),
                shift_type_dd,
                ft.Text("Phân ca:", size=13, weight=ft.FontWeight.BOLD),
                ft.Container(content=slots_column, height=250),
                ft.Divider(height=1),
                approver_dd, err_text,
            ], spacing=8, tight=True, scroll=ft.ScrollMode.AUTO, height=500)

            _dlg = ft.AlertDialog(
                title=ft.Text("Cắt gác", size=16, weight=ft.FontWeight.BOLD),
                content=content_col,
                actions=[
                    ft.TextButton("Huỷ", on_click=lambda e: _close_dialog(self.page)),
                    ft.ElevatedButton("Lưu & Gửi duyệt", bgcolor=self.SCHEDULE_THEME,
                                      color=ft.Colors.WHITE, on_click=do_save_gac),
                ],
            )

        else:
            # ---- CHẾ ĐỘ CẮT TRỰC BAN ----
            _refresh_approvers()
            field_labels = ["Trực ban nội vụ", "Đốc canh", "Trực nhật", "Ghi chú"]
            data_inputs = {lbl: ft.TextField(label=lbl, border_radius=8, dense=True) for lbl in field_labels}

            def do_save_tb(ev):
                date_str = (date_input.value or "").strip()
                approver_id = approver_dd.value or ""
                if not date_str or not approver_id:
                    err_text.value = "⚠️ Ngày và Người duyệt là bắt buộc"
                    page.update(); return

                data = {lbl: inp.value or "" for lbl, inp in data_inputs.items()}
                entry = {
                    "id": f"sc-{store.now_ms()}",
                    "type": "trucban",
                    "level": level_dd.value or "dai_doi",
                    "unit_id": my_unit,
                    "date_str": date_str,
                    "troop_count": troop_input.value or "",
                    "data": data,
                    "creator_id": my_uid,
                    "approver_id": approver_id,
                    "status": "pending",
                    "reject_reason": "",
                    "created_at": store.now_ms(),
                }
                entries = store.get("scheduleEntries", lambda: [])
                entries.append(entry)
                store.set_value("scheduleEntries", entries)
                store.log_activity(f"Cắt trực ban: {date_str}")
                _tb_creator = profile.get("name") or profile.get("username") or "Chỉ huy"
                store.push_notif("unit", "📋 Trực ban mới cần duyệt",
                                 f"{_tb_creator}: {date_str}", "schedule",
                                 target_uid=approver_id, sender_name=_tb_creator)
                _dlg.open = False; page.update()
                self.toast("✅ Đã cắt trực ban")
                self.body.content = self.module_schedule()
                self.refresh()

            content_col = ft.Column([
                date_input, level_dd, troop_input,
                ft.Divider(height=1),
                *data_inputs.values(),
                ft.Divider(height=1),
                approver_dd, err_text,
            ], spacing=8, tight=True, scroll=ft.ScrollMode.AUTO, height=400)

            _dlg = ft.AlertDialog(
                title=ft.Text("Cắt trực ban", size=16, weight=ft.FontWeight.BOLD),
                content=content_col,
                actions=[
                    ft.TextButton("Huỷ", on_click=lambda e: _close_dialog(self.page)),
                    ft.ElevatedButton("Lưu & Gửi duyệt", bgcolor=self.SCHEDULE_THEME,
                                      color=ft.Colors.WHITE, on_click=do_save_tb),
                ],
            )
        _show_dialog(self.page, _dlg)

    # ---------- Module: Báo cáo ----------
    def module_reports(self) -> ft.Control:
        reports = store.get("reports", store.seed_reports)

        def status_badge(status: str) -> ft.Container:
            colors = {"approved": (GREEN_LIGHT, GREEN_DARK, "✅ Đã duyệt"),
                      "pending": ("#faeeda", "#633806", "⏳ Chờ duyệt"),
                      "rejected": ("#fcebeb", "#791f1f", "❌ Từ chối")}
            bg, fg, lbl = colors.get(status, ("#eee", TEXT, status))
            return ft.Container(
                content=ft.Text(lbl, size=10, color=fg, weight=ft.FontWeight.BOLD),
                bgcolor=bg, border_radius=8,
                padding=ft.padding.symmetric(horizontal=8, vertical=3),
            )

        def report_row(r: dict) -> ft.Container:
            actions = []
            if r.get("status") == "pending":
                def approve(e, _r=r):
                    _r["status"] = "approved"
                    _r["approver"] = "Bùi Quang Thành"
                    store.set_value("reports", reports)
                    store.log_activity(f"Duyệt báo cáo: {_r['title']}")
                    store.push_notif("success", "✅ Báo cáo đã duyệt", _r["title"], "reports")
                    self.toast("✅ Đã duyệt")
                    self.body.content = self.module_reports()
                    self.refresh()

                def reject(e, _r=r):
                    _r["status"] = "rejected"
                    store.set_value("reports", reports)
                    self.toast("❌ Đã từ chối")
                    self.body.content = self.module_reports()
                    self.refresh()

                actions = [
                    ft.Row([
                        ft.ElevatedButton("✅ Duyệt", on_click=approve, expand=True,
                                          style=ft.ButtonStyle(bgcolor=GREEN_MID,
                                                               color=ft.Colors.WHITE)),
                        ft.OutlinedButton("❌ Từ chối", on_click=reject, expand=True,
                                          style=ft.ButtonStyle(color=RED)),
                    ], spacing=6),
                ]
            return ft.Container(
                content=ft.Column(
                    [
                        ft.Row([
                            ft.Column(
                                [ft.Text(r["title"], size=13, weight=ft.FontWeight.W_600),
                                 ft.Text(f"{r['author']} • {fmt_dt(r['date'])}",
                                         size=11, color=TEXT_MUTED)],
                                spacing=2, expand=True, tight=True,
                            ),
                            status_badge(r["status"]),
                        ], vertical_alignment=ft.CrossAxisAlignment.START),
                    ] + actions,
                    spacing=8,
                ),
                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=10,
                padding=12, margin=ft.margin.only(bottom=8),
                on_click=lambda e, _r=r: self.toast(f"📋 {_r['title']}\n\n{_r['content']}"),
                ink=True,
            )

        def create_report(e):
            new = {
                "id": f"r{store.now_ms()}",
                "title": "Báo cáo nhanh từ app Python",
                "author": "Bùi Quang Thành",
                "type": "general",
                "content": "Nội dung báo cáo demo",
                "date": store.now_ms(), "status": "pending",
            }
            reports.insert(0, new)
            store.set_value("reports", reports)
            store.log_activity(f"Tạo báo cáo: {new['title']}")
            store.push_notif("unit", "📋 Báo cáo mới chờ duyệt", new["title"], "reports")
            self.toast("✅ Đã tạo báo cáo")
            self.body.content = self.module_reports()
            self.refresh()

        def _export_reports(e):
            headers = ["📅 Ngày tạo", "Tiêu đề", "Tác giả", "Loại", "Trạng thái", "Nội dung"]
            rows = []
            for r in reports:
                rows.append([
                    fmt_dt(r.get("date", 0)),
                    r.get("title", ""),
                    r.get("author", ""),
                    r.get("type", ""),
                    r.get("status", ""),
                    r.get("content", "")
                ])
            self.export_data_to_csv("BaoCao_HanhChinh", headers, rows)

        export_btn = ft.IconButton(
            icon=ft.Icons.FILE_DOWNLOAD,
            icon_color=GREEN_MID,
            tooltip="Xuất Excel/CSV",
            on_click=_export_reports
        )

        return ft.Column(
            [
                self.module_back_bar("📋 Báo cáo", trailing=export_btn),
                ft.ListView(
                    controls=[
                        ft.Container(
                            content=ft.Column(
                                [report_row(r) for r in reports] + [
                                    ft.ElevatedButton(
                                        "＋ Tạo báo cáo mới", on_click=create_report,
                                        style=ft.ButtonStyle(bgcolor=GREEN_MID,
                                                             color=ft.Colors.WHITE,
                                                             padding=14,
                                                             shape=ft.RoundedRectangleBorder(radius=10)),
                                        width=10000,
                                    ),
                                ],
                                spacing=0,
                            ),
                            padding=10,
                        ),
                    ],
                    expand=True, padding=0,
                ),
            ],
            spacing=0, expand=True,
        )

    # ---------- Phân quyền thành viên (admin) ----------
    def open_units_assign_role_dialog(self, soldier_id: str, *, return_to_member_view: bool = False) -> None:
        if not self._is_admin():
            self.toast("Bạn không có quyền phân quyền")
            return
        page = self.page
        soldiers = store.get("soldiers", store.seed_soldiers)
        sid = str(soldier_id or "")
        row = next((x for x in soldiers if str(x.get("id")) == sid), None)
        if not row:
            self.toast("Không tìm thấy tài khoản")
            return
        uname = str(row.get("username") or "")
        if _is_super_admin_username(uname):
            self.toast("Không chỉnh quyền tài khoản quản trị hệ thống")
            return

        level_dd = ft.Dropdown(
            label="Cấp quản trị (adminLevel)",
            value=str(int(row.get("adminLevel") or 0)),
            options=[ft.dropdown.Option(str(i), f"Mức {i}") for i in range(0, 6)],
            border_radius=8, dense=True,
        )

        def _on_role_changed(e):
            """Tự gợi ý adminLevel khi chọn chức vụ."""
            selected_role = (role_tf.value or "").strip()
            suggested = store.get_admin_level_for_role(selected_role)
            level_dd.value = str(suggested)
            page.update()

        # Danh sách chức danh — lọc theo đơn vị
        units_tree_full = store.get("units", store.seed_units)
        cur_role = str(row.get("role") or "")
        cur_unit = str(row.get("unitId") or "")

        def _get_role_opts(unit_id: str) -> list:
            if unit_id:
                relevant = store.titles_for_unit(units_tree_full, unit_id)
                # Nếu role hiện tại không nằm trong list → vẫn thêm vào để không mất
                if cur_role and cur_role not in relevant:
                    relevant.insert(0, cur_role)
                return [ft.dropdown.Option(t, t) for t in relevant]
            return [ft.dropdown.Option(t, t) for t in store.TITLES]

        role_tf = ft.Dropdown(
            label="Chức vụ / vai trò",
            value=cur_role,
            options=_get_role_opts(cur_unit),
            border_radius=8, dense=True,
            on_change=_on_role_changed,
        )
        admin_sw = ft.Switch(label="Quản trị viên (isAdmin)", value=bool(row.get("isAdmin")))
        # Dropdown đơn vị
        unit_opts_full = store.flatten_units_for_select(units_tree_full)
        unit_dd = ft.Dropdown(
            label="Đơn vị",
            value=cur_unit if cur_unit else None,
            options=[ft.dropdown.Option(k, lbl) for k, lbl in unit_opts_full],
            border_radius=8, dense=True,
        )

        def _on_unit_dd_changed(e=None):
            """Khi admin đổi đơn vị → lọc lại chức danh phù hợp."""
            uid = unit_dd.value or ""
            role_tf.options = _get_role_opts(uid)
            if role_tf.value and role_tf.value not in [o.key for o in role_tf.options]:
                role_tf.value = role_tf.options[0].key if role_tf.options else ""
            _on_role_changed()  # cập nhật adminLevel gợi ý
            try:
                page.update()
            except Exception:
                pass

        unit_dd.on_change = _on_unit_dd_changed
        err_text = ft.Text("", color=RED, size=12)

        def close_dlg(_):
            _close_dialog(self.page)

        def save_assign(_):
            err_text.value = ""
            try:
                lvl = int(level_dd.value or "1")
            except (ValueError, TypeError):
                lvl = 1
            lvl = max(1, min(5, lvl))
            soldiers2 = store.get("soldiers", store.seed_soldiers)
            idx = next((i for i, x in enumerate(soldiers2) if str(x.get("id")) == sid), None)
            if idx is None:
                err_text.value = "⚠️ Bản ghi đã thay đổi, thử lại"
                page.update()
                return
            tgt = soldiers2[idx]
            if _is_super_admin_username(str(tgt.get("username") or "")):
                err_text.value = "⚠️ Không chỉnh quyền admin hệ thống"
                page.update()
                return
            new_unit_id = unit_dd.value or tgt.get("unitId", "")
            new_unit_name = next((lbl.strip() for k, lbl in unit_opts_full if k == new_unit_id), new_unit_id)
            soldiers2[idx] = {
                **tgt,
                "role": (role_tf.value or "").strip(),
                "isAdmin": bool(admin_sw.value),
                "adminLevel": lvl,
                "unitId": new_unit_id,
                "unitName": new_unit_name,
            }
            store.set_value("soldiers", soldiers2)
            uid = str(soldiers2[idx].get("id") or "")
            if _looks_like_firebase_uid(uid):
                try:
                    FS.set_doc(
                        f"users/{uid}",
                        {
                            "role": soldiers2[idx]["role"],
                            "isAdmin": soldiers2[idx]["isAdmin"],
                            "adminLevel": soldiers2[idx]["adminLevel"],
                            "unitId": soldiers2[idx].get("unitId", ""),
                            "unitName": soldiers2[idx].get("unitName", ""),
                        },
                    )
                except Exception:
                    pass
            store.log_activity(
                f"Phân quyền {soldiers2[idx].get('username')}: "
                f"admin={soldiers2[idx]['isAdmin']} level={lvl}",
            )
            close_dlg(None)
            self.toast("Đã cập nhật quyền")
            if return_to_member_view:
                self.body.content = self.view_member_profile(str(sid))
            else:
                self.body.content = self.module_units()
            page.update()

        _dlg = ft.AlertDialog(
            modal=True,
            bgcolor=BG,
            title=ft.Text(f"Phân quyền • {uname}", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(f"Họ tên: {row.get('name') or '—'}", size=12, color=TEXT_MUTED),
                        role_tf, level_dd, admin_sw, unit_dd, err_text,
                    ],
                    spacing=10, tight=True, scroll=ft.ScrollMode.AUTO,
                ),
                width=360, height=360,
            ),
            actions=[
                ft.TextButton("Huỷ", on_click=close_dlg),
                ft.ElevatedButton(
                    "Lưu", on_click=save_assign,
                    bgcolor=GREEN_MID, color=ft.Colors.WHITE,
                ),
            ],
        )
        _show_dialog(self.page, _dlg)

    # ---------- Module: Quản lý quân nhân + cây đơn vị ----------
    def module_units(self) -> ft.Control:
        tree = store.get("units", store.seed_units)
        soldiers = store.get("soldiers", store.seed_soldiers)

        # === Cây đơn vị ĐỒNG BỘ với dropdown đăng ký ===
        # Dùng chung hàm flatten_units_for_select để 2 nơi luôn khớp.
        unit_select_list = store.flatten_units_for_select(tree)

        # Bản đồ id → node (để lấy commanderTitle, type, abbr, ...)
        node_by_id: dict[str, dict] = {}
        def _build_index(n):
            if isinstance(n, dict) and n.get("id"):
                node_by_id[n["id"]] = n
                for c in n.get("children") or []:
                    _build_index(c)
        _build_index(tree)

        # Icon theo type
        TYPE_ICON = {
            "regiment":   "🏛",
            "command":    "🎖",
            "department": "🏢",
            "battalion":  "🪖",
            "company":    "⚔️",
            "platoon":    "🎯",
            "squad":      "·",
            "station":    "🏭",
        }

        def _count_members(node):
            ids = set(_all_unit_ids(node))
            return sum(1 for s in soldiers
                       if not s.get("isAdmin") and s.get("unitId") in ids)

        # Bản đồ tên chức danh assistant con của node → node assistant (để map role)
        def _child_assistant_ids(n: dict) -> dict[str, str]:
            """{role_name_lower: child_id} cho tất cả assistant children."""
            mp = {}
            for c in (n.get("children") or []):
                if c.get("type") == "assistant":
                    mp[(c.get("name") or "").strip().lower()] = c.get("id", "")
            return mp

        def _find_commander(node: dict):
            """Tìm chỉ huy của một đơn vị — fallback nhiều cấp:

            1) `node.commanderId` nếu trỏ tới soldier có thật.
            2) Soldier có unitId == node.id và role == commanderTitle.
            3) Soldier có unitId là một sub-node assistant trùng tên commanderTitle.
            4) Soldier cấp 4+ trong đơn vị có role chứa "trưởng/chủ nhiệm/chính uỷ".
            """
            cid = node.get("commanderId")
            if cid:
                m = next((s for s in soldiers if str(s.get("id")) == str(cid)), None)
                if m:
                    return m

            cmd_title = (node.get("commanderTitle") or "").strip().lower()
            n_id = node.get("id") or ""

            # 2) Khớp role với commanderTitle, cùng unitId
            if cmd_title and n_id:
                for s in soldiers:
                    if (s.get("unitId") == n_id
                            and (s.get("role") or "").strip().lower() == cmd_title
                            and not s.get("isAdmin")):
                        return s

            # 3) Sub-node assistant tên trùng commanderTitle (nếu user đăng ký bằng position-id)
            sub_ids = _child_assistant_ids(node)
            sub_id = sub_ids.get(cmd_title) if cmd_title else None
            if sub_id:
                for s in soldiers:
                    if s.get("unitId") == sub_id and not s.get("isAdmin"):
                        return s

            # 4) Fallback: trong đơn vị này, lấy người có adminLevel cao nhất + role có từ chỉ huy
            CMD_KEYS = ("trưởng", "chủ nhiệm", "chính uỷ", "chính ủy")
            EXCLUDE = ("phó",) if "phó" not in cmd_title else ()
            cands = []
            for s in soldiers:
                if s.get("unitId") != n_id or s.get("isAdmin"):
                    continue
                role = (s.get("role") or "").lower()
                if not any(k in role for k in CMD_KEYS):
                    continue
                if EXCLUDE and any(k in role for k in EXCLUDE):
                    continue
                cands.append(s)
            if cands:
                cands.sort(key=lambda x: -int(x.get("adminLevel") or 0))
                return cands[0]
            return None

        def _row_for(uid: str, indented_label: str) -> ft.Control:
            node = node_by_id.get(uid, {})
            ntype = node.get("type", "")
            icon = TYPE_ICON.get(ntype, "·")
            # Lấy độ thụt từ chuỗi (mỗi 4 space = 1 cấp)
            stripped = indented_label.lstrip(" ")
            depth = (len(indented_label) - len(stripped)) // 4
            # `stripped` đã là canonical name (do flatten_units_for_select đã chuẩn hoá)
            name = stripped or store.canonical_unit_name(node) or node.get("name", "?")
            abbr = node.get("abbr") or ""

            cmd = _find_commander(node) if node else None
            cmd_title = node.get("commanderTitle") or "Chỉ huy"
            if cmd:
                cmd_line = f"★ {cmd.get('rank','')} {cmd.get('name','')} — {cmd.get('role') or cmd_title}"
                cmd_color = TEXT_MUTED
            else:
                cmd_line = f"⚠️ Chưa có chỉ huy ({cmd_title})"
                cmd_color = RED
            cnt = _count_members(node) if node else 0

            title_row = [
                ft.Text(icon, size=14),
                ft.Text(name, size=13, weight=ft.FontWeight.BOLD),
            ]
            if abbr:
                title_row.append(
                    ft.Container(
                        content=ft.Text(abbr, size=9, color=GREEN_DARK,
                                        weight=ft.FontWeight.BOLD),
                        bgcolor="#e8f5e9", border_radius=6,
                        padding=ft.padding.symmetric(horizontal=6, vertical=1),
                    )
                )

            return ft.Container(
                content=ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Row(title_row, spacing=6,
                                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                ft.Text(cmd_line, size=10, color=cmd_color),
                            ],
                            spacing=2, expand=True, tight=True,
                        ),
                        ft.Container(
                            content=ft.Text(f"{cnt}/{cnt}" if cnt else "·",
                                            size=11, color=TEXT_MUTED,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=BG2 if cnt else ft.Colors.TRANSPARENT,
                            border_radius=10, width=45, height=22,
                            alignment=ft.alignment.center,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.only(left=10 + depth * 14, right=10, top=8, bottom=8),
                bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                on_click=lambda e, n=node: self._open_unit_members(n),
                ink=True,
            )

        total_members = sum(1 for s in soldiers if not s.get("isAdmin"))

        # Build rows từ unit_select_list (đúng thứ tự + filter của dropdown đăng ký)
        tree_rows = [_row_for(uid, label) for uid, label in unit_select_list]

        def tree_panel() -> ft.Control:
            return ft.ListView(
                controls=[
                    ft.Container(
                        content=ft.Column(
                            [
                                ft.Container(
                                    content=ft.Row(
                                        [
                                            ft.Text("🌳 Cây đơn vị", size=13,
                                                    weight=ft.FontWeight.BOLD, expand=True),
                                            ft.Text("(theo dropdown đăng ký)",
                                                    size=10, color=TEXT_MUTED),
                                            ft.Text(f"  {total_members} quân nhân",
                                                    size=10, color=TEXT_MUTED),
                                        ],
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    ),
                                    padding=ft.padding.symmetric(horizontal=12, vertical=8),
                                    bgcolor=BG,
                                    border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                                ),
                            ] + tree_rows,
                            spacing=0,
                        ),
                        bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
                        margin=10, clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    ),
                ],
                expand=True, padding=0,
            )

        def perm_chip(sol: dict) -> ft.Control:
            st = str(sol.get("accountStatus") or "")
            if st == "pending":
                return ft.Container(
                    content=ft.Text("⏳ Chờ", size=9, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                    bgcolor=AMBER, border_radius=6,
                    padding=ft.padding.symmetric(horizontal=6, vertical=1),
                )
            if st == "locked":
                return ft.Container(
                    content=ft.Text("🔒 Khoá", size=9, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                    bgcolor=RED, border_radius=6,
                    padding=ft.padding.symmetric(horizontal=6, vertical=1),
                )
            un = str(sol.get("username") or "")
            if _is_super_admin_username(un):
                return ft.Container(
                    content=ft.Text(
                        "SYS", size=9, color=ft.Colors.BLACK, weight=ft.FontWeight.BOLD,
                    ),
                    bgcolor=GOLD, border_radius=6,
                    padding=ft.padding.symmetric(horizontal=6, vertical=1),
                )
            if sol.get("isAdmin"):
                return ft.Container(
                    content=ft.Text(
                        f"A{int(sol.get('adminLevel') or 1)}",
                        size=9, color=ft.Colors.BLACK, weight=ft.FontWeight.BOLD,
                    ),
                    bgcolor=GREEN_LIGHT,
                    border_radius=6,
                    padding=ft.padding.symmetric(horizontal=6, vertical=1),
                )
            return ft.Text(f"L{int(sol.get('adminLevel') or 1)}", size=10, color=TEXT_MUTED)

        def accounts_panel() -> ft.Control:
            # Danh sách tài khoản đã duyệt (active)
            active_sol = [s for s in soldiers if s.get("accountStatus") != "pending"]
            sorted_sol = sorted(
                active_sol,
                key=lambda x: (
                    _username_key(str(x.get("username") or "")),
                    str(x.get("name") or ""),
                ),
            )
            
            def do_approve(target_sid: str):
                soldiers2 = store.get("soldiers", store.seed_soldiers)
                j = next((i for i, x in enumerate(soldiers2) if str(x.get("id")) == target_sid), None)
                if j is not None:
                    soldiers2[j]["accountStatus"] = "active"
                    # Override 600s để bảo vệ khỏi 30s sync ghi đè
                    store.set_account_status_override(target_sid, "active", ttl_seconds=600.0)
                    store.set_value("soldiers", soldiers2)
                    self.toast("✅ Đã duyệt tài khoản")
                    # Push DB trong background với retry
                    def _push_approve(sid=target_sid):
                        import time as _ta
                        for _att in range(3):
                            try:
                                if _looks_like_firebase_uid(sid):
                                    FS.set_doc(f"users/{sid}", {"accountStatus": "active"})
                                store.STORE.flush_pending()
                                break
                            except Exception:
                                _ta.sleep(5)
                    import threading as _th_a
                    _th_a.Thread(target=_push_approve, daemon=True).start()
                    self.body.content = self.module_units()
                    self.refresh()

            acc_rows: list[ft.Control] = []
            for s in sorted_sol:
                uname = str(s.get("username") or "").strip() or "—"
                disp_name = str(s.get("name") or "").strip() or "—"
                sid = str(s.get("id") or "")
                st = str(s.get("accountStatus") or "")
                btn: ft.Control | None = None
                if self._is_admin() and sid and not _is_super_admin_username(uname):
                    btn = ft.OutlinedButton(
                        "Phân quyền",
                        on_click=lambda e, _sid=sid: self.open_units_assign_role_dialog(_sid),
                        style=ft.ButtonStyle(
                            padding=ft.padding.symmetric(horizontal=12, vertical=8),
                        ),
                    )
                acc_rows.append(
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Row(
                                            [
                                                ft.Text(uname, size=13,
                                                        weight=ft.FontWeight.BOLD),
                                                perm_chip(s),
                                            ],
                                            spacing=8,
                                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        ),
                                        ft.Text(disp_name, size=11, color=TEXT_MUTED),
                                    ],
                                    spacing=2,
                                    expand=True,
                                    tight=True,
                                ),
                                btn if btn is not None else ft.Container(width=8),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.padding.symmetric(horizontal=12, vertical=10),
                        bgcolor=BG,
                        border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                    ),
                )
            return ft.ListView(
                controls=[
                    ft.Container(
                        content=ft.Column(
                            [
                                ft.Container(
                                    content=ft.Row(
                                        [
                                            ft.Text(
                                                "👤 Tài khoản & quyền",
                                                size=13,
                                                weight=ft.FontWeight.BOLD,
                                                expand=True,
                                            ),
                                            ft.Text(
                                                f"{len(sorted_sol)} tài khoản",
                                                size=10,
                                                color=TEXT_MUTED,
                                            ),
                                        ],
                                    ),
                                    padding=12,
                                    bgcolor=BG,
                                    border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                                ),
                            ]
                            + acc_rows,
                            spacing=0,
                        ),
                        bgcolor=BG,
                        border=ft.border.all(1, BORDER),
                        border_radius=12,
                        margin=10,
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    ),
                ],
                expand=True,
                padding=0,
            )

        def pending_panel() -> ft.Control:
            pending_sol = [s for s in soldiers if s.get("accountStatus") == "pending"]
            sorted_sol = sorted(
                pending_sol,
                key=lambda x: (_username_key(str(x.get("username") or "")), str(x.get("name") or "")),
            )

            # Bản đồ unitId → tên hiển thị
            _units_tree = store.get("units", store.seed_units)
            _unit_map = dict(store.flatten_units_for_select(_units_tree))

            def _info_row(label: str, value: str) -> ft.Control:
                return ft.Row(
                    [
                        ft.Text(label + ":", size=12, color=TEXT_MUTED, width=120),
                        ft.Text(value, size=12, weight=ft.FontWeight.W_500, expand=True),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.START,
                )

            def show_detail_dialog(s: dict):
                sid = str(s.get("id") or "")
                uname = str(s.get("username") or "").strip() or "—"
                disp_name = str(s.get("name") or "").strip() or "—"
                rank = str(s.get("rank") or "").strip() or "—"
                role = str(s.get("role") or "").strip() or "—"
                unit_id = str(s.get("unitId") or "")
                unit_name = _unit_map.get(unit_id, unit_id).strip() if unit_id else "—"
                phone = str(s.get("phone") or "").strip() or "—"
                hometown = str(s.get("hometown") or "").strip() or "—"

                # Thử lấy dữ liệu mới nhất từ DB (nếu có mạng)
                fresh: dict = {}
                if _looks_like_firebase_uid(sid):
                    try:
                        fresh = FS.get_doc(f"users/{sid}") or {}
                        if fresh:
                            phone = str(fresh.get("phone") or phone).strip() or "—"
                            hometown = str(fresh.get("hometown") or hometown).strip() or "—"
                    except Exception:
                        pass

                dlg = ft.AlertDialog(modal=True)

                def close_dlg():
                    dlg.open = False
                    try:
                        self.page.update()
                    except Exception:
                        pass

                def do_approve(e):
                    close_dlg()
                    self.approve_member_account(sid)

                def do_reject(e):
                    close_dlg()
                    self.confirm_delete_soldier(sid)

                can_act = self._is_admin() and sid and not _is_super_admin_username(uname)

                dlg.title = ft.Row(
                    [
                        ft.Text("Thông tin đăng ký", size=15, weight=ft.FontWeight.BOLD, expand=True),
                        perm_chip(s),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
                dlg.content = ft.Column(
                    [
                        _info_row("Số định danh", uname),
                        _info_row("Họ và tên", disp_name),
                        _info_row("Cấp bậc", rank),
                        _info_row("Chức danh", role),
                        _info_row("Đơn vị", unit_name),
                        _info_row("Số điện thoại", phone),
                        _info_row("Quê quán", hometown),
                    ],
                    spacing=10, tight=True,
                    width=300,
                )
                action_btns: list[ft.Control] = [
                    ft.TextButton("Đóng", on_click=lambda e: close_dlg()),
                ]
                if can_act:
                    action_btns += [
                        ft.OutlinedButton(
                            "❌ Từ chối", on_click=do_reject,
                            style=ft.ButtonStyle(color=RED),
                        ),
                        ft.ElevatedButton(
                            "✅ Duyệt", on_click=do_approve,
                            bgcolor=GREEN_MID, color=ft.Colors.WHITE,
                        ),
                    ]
                dlg.actions = action_btns
                dlg.actions_alignment = ft.MainAxisAlignment.END
                _show_dialog(self.page, dlg)

            acc_rows: list[ft.Control] = []
            for s in sorted_sol:
                uname = str(s.get("username") or "").strip() or "—"
                disp_name = str(s.get("name") or "").strip() or "—"
                rank = str(s.get("rank") or "").strip() or "—"
                unit_id = str(s.get("unitId") or "")
                unit_name = _unit_map.get(unit_id, unit_id).strip() if unit_id else "—"
                acc_rows.append(
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Row(
                                            [
                                                ft.Text(uname, size=13, weight=ft.FontWeight.BOLD),
                                                perm_chip(s),
                                            ],
                                            spacing=8,
                                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        ),
                                        ft.Text(disp_name, size=12),
                                        ft.Text(
                                            f"{rank}  •  {unit_name}",
                                            size=11, color=TEXT_MUTED,
                                        ),
                                    ],
                                    spacing=2, expand=True, tight=True,
                                ),
                                ft.Icon(ft.Icons.CHEVRON_RIGHT, size=18, color=TEXT_MUTED),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.padding.symmetric(horizontal=12, vertical=12),
                        bgcolor=BG,
                        border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                        ink=True,
                        on_click=lambda e, _s=s: show_detail_dialog(_s),
                    ),
                )

            if not acc_rows:
                acc_rows.append(
                    ft.Container(
                        content=ft.Text("Không có tài khoản nào chờ duyệt.", color=TEXT_MUTED, italic=True),
                        padding=20, alignment=ft.alignment.center,
                    )
                )

            return ft.ListView(
                controls=[
                    ft.Container(
                        content=ft.Column(
                            [
                                ft.Container(
                                    content=ft.Row(
                                        [
                                            ft.Text("⏳ Đăng ký mới (Chờ duyệt)", size=13, weight=ft.FontWeight.BOLD, expand=True),
                                            ft.Text(f"{len(sorted_sol)} tài khoản", size=10, color=TEXT_MUTED),
                                        ],
                                    ),
                                    padding=12, bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                                ),
                                ft.Container(
                                    content=ft.Text(
                                        "Bấm vào từng thành viên để xem thông tin đầy đủ trước khi duyệt.",
                                        size=11, color=TEXT_MUTED, italic=True,
                                    ),
                                    padding=ft.padding.symmetric(horizontal=12, vertical=6),
                                ),
                            ] + acc_rows,
                            spacing=0,
                        ),
                        bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12, margin=10,
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    ),
                ],
                expand=True, padding=0,
            )

        tab_defs = [("Cây đơn vị", "tree")]
        if self._is_admin():
            pending_count = sum(1 for s in soldiers if s.get("accountStatus") == "pending")
            tab_defs.append(("Tài khoản & quyền", "accounts"))
            tab_defs.append((f"Đăng ký mới ({pending_count})", "pending"))

        keys_sub = [k for _, k in tab_defs]
        current_sub = self.units_subtab if self.units_subtab in keys_sub else tab_defs[0][1]

        selected_idx = next(
            (i for i, (_, k) in enumerate(tab_defs) if k == current_sub),
            0,
        )

        def on_units_tab_changed(e):
            idx = None
            try:
                idx = e.control.selected_index
            except Exception:
                idx = getattr(e, "selected_index", None)
            if idx is None:
                return
            if 0 <= idx < len(tab_defs):
                self.units_subtab = tab_defs[idx][1]
                self.body.content = self.module_units()
                self.refresh()

        tabs_bar: ft.Control | None = None
        if len(tab_defs) > 1:
            tabs_bar = ft.Container(
                content=ft.Tabs(
                    selected_index=selected_idx,
                    on_change=on_units_tab_changed,
                    tabs=[ft.Tab(text=label) for label, _ in tab_defs],
                    height=46,
                ),
                bgcolor=BG,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                height=46,
            )

        if current_sub == "tree":
            main_body = tree_panel()
        elif current_sub == "pending":
            main_body = pending_panel()
        else:
            main_body = accounts_panel()

        # Bỏ module_back_bar — back arrow + tiêu đề đã có ở header app chính (tránh trùng)
        col_children: list[ft.Control] = []
        if tabs_bar is not None:
            col_children.append(tabs_bar)
        col_children.append(main_body)

        return ft.Column(col_children, spacing=0, expand=True)

    # ---------- Xem danh sách thành viên theo đơn vị ----------
    def _open_unit_members(self, node: dict) -> None:
        """Bấm vào node trong cây đơn vị → mở trang danh sách thành viên."""
        self.body.content = self.view_unit_members(node)
        self.refresh()

    def view_unit_members(self, node: dict) -> ft.Control:
        """Hiển thị danh sách quân nhân thuộc đơn vị (node) đã chọn."""
        soldiers = store.get("soldiers", store.seed_soldiers)
        unit_ids = set(_all_unit_ids(node))
        members = [s for s in soldiers if s.get("unitId") in unit_ids]
        # Sắp theo: ưu tiên chức danh (Trưởng/CTV → Phó → Trợ lý → TĐT → TĐ → NV → CS)
        # rồi adminLevel giảm dần, cuối cùng theo tên.
        members.sort(key=lambda x: (
            store.role_priority(x.get("role") or ""),
            -int(x.get("adminLevel") or 0),
            x.get("name") or "",
        ))

        my_profile = store.get("userProfile", store.seed_user_profile)
        my_admin_level = int(my_profile.get("adminLevel") or 1)
        can_manage = my_admin_level >= 4 or my_profile.get("isAdmin")

        unit_names = self._unit_name_map(store.get("units", store.seed_units))

        def back_to_tree(_):
            self.body.content = self.module_units()
            self.refresh()

        def member_row(s: dict) -> ft.Container:
            sid = str(s.get("id") or "")
            name = s.get("name") or "—"
            rank = s.get("rank") or ""
            role = s.get("role") or ""
            unit_lbl = unit_names.get(s.get("unitId"), s.get("unitName") or "")
            photo = str(s.get("photoUrl") or "")
            al = int(s.get("adminLevel") or 0)
            # Badge phân quyền
            badge: ft.Control
            if al >= 4:
                badge = ft.Container(
                    content=ft.Text(f"C{al}", size=9, color=ft.Colors.WHITE,
                                    weight=ft.FontWeight.BOLD),
                    bgcolor=RED, border_radius=6,
                    padding=ft.padding.symmetric(horizontal=5, vertical=1),
                )
            elif al >= 2:
                badge = ft.Container(
                    content=ft.Text(f"C{al}", size=9, color=ft.Colors.BLACK,
                                    weight=ft.FontWeight.BOLD),
                    bgcolor=GOLD, border_radius=6,
                    padding=ft.padding.symmetric(horizontal=5, vertical=1),
                )
            elif al == 1:
                badge = ft.Container(
                    content=ft.Text("C1", size=9, color=ft.Colors.BLACK,
                                    weight=ft.FontWeight.BOLD),
                    bgcolor=GREEN_LIGHT, border_radius=6,
                    padding=ft.padding.symmetric(horizontal=5, vertical=1),
                )
            else:
                badge = ft.Container(width=0)

            actions: list[ft.Control] = []
            if can_manage and not _is_super_admin_username(str(s.get("username") or "")):
                actions = [
                    ft.PopupMenuButton(
                        icon=ft.Icons.MORE_VERT, icon_size=18, icon_color=TEXT_MUTED,
                        items=[
                            ft.PopupMenuItem(
                                text="🛡 Phân quyền",
                                on_click=lambda e, _sid=sid: (
                                    self.open_units_assign_role_dialog(_sid),
                                ),
                            ),
                            ft.PopupMenuItem(
                                text="✏️ Sửa hồ sơ",
                                on_click=lambda e, _sid=sid: (
                                    self.open_member_profile_edit_dialog(_sid),
                                ),
                            ),
                            ft.PopupMenuItem(
                                text="🗑️ Xóa",
                                on_click=lambda e, _sid=sid: (
                                    self.confirm_delete_soldier(_sid),
                                ),
                            ),
                        ],
                    ),
                ]

            return ft.Container(
                content=ft.Row(
                    [
                        self._soldier_avatar(name, photo, 40),
                        ft.Column(
                            [
                                ft.Row(
                                    [ft.Text(f"{rank} {name}".strip(), size=13,
                                             weight=ft.FontWeight.W_600),
                                     badge],
                                    spacing=6,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                ft.Text(
                                    f"{role} • {unit_lbl}" if role else unit_lbl,
                                    size=11, color=TEXT_MUTED, max_lines=1,
                                ),
                            ],
                            spacing=2, expand=True, tight=True,
                        ),
                    ] + actions,
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                bgcolor=BG,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                on_click=lambda e, _s=s: self.open_member_profile(_s),
                ink=True,
            )

        # Header
        header_actions = [
            ft.IconButton(ft.Icons.ARROW_BACK, on_click=back_to_tree),
            ft.Column(
                [
                    ft.Text(node["name"], size=15, weight=ft.FontWeight.BOLD),
                    ft.Text(
                        f"{len(members)} quân nhân"
                        + (f" • {node.get('commanderTitle', '')}" if node.get('commanderTitle') else ""),
                        size=11, color=TEXT_MUTED,
                    ),
                ],
                spacing=2, expand=True, tight=True,
            ),
        ]
        if can_manage:
            header_actions.append(
                ft.IconButton(
                    ft.Icons.ADD_CIRCLE, icon_color=GREEN_MID, icon_size=26,
                    tooltip="Thêm chức danh vào đơn vị",
                    on_click=lambda e, n=node: self.open_add_position_dialog(n),
                )
            )
        header = ft.Container(
            content=ft.Row(
                header_actions,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=BG, padding=ft.padding.symmetric(horizontal=4, vertical=6),
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
        )

        # Danh sách thành viên
        if members:
            rows = [member_row(s) for s in members]
        else:
            rows = [
                ft.Container(
                    content=ft.Text("Chưa có quân nhân nào thuộc đơn vị này",
                                    size=13, color=TEXT_MUTED,
                                    text_align=ft.TextAlign.CENTER),
                    padding=40, alignment=ft.alignment.center,
                ),
            ]

        return ft.Column(
            [
                header,
                ft.ListView(
                    controls=[
                        ft.Container(
                            content=ft.Column(rows, spacing=0),
                            padding=0,
                        ),
                    ],
                    expand=True, padding=0,
                ),
            ],
            spacing=0, expand=True,
        )

    # ---------- Module: F47 ----------
    # Lựa chọn cố định cho dropdown
    F47_HOUR_OPTIONS = [
        ("1", "1 giờ"), ("3", "3 giờ"), ("6", "6 giờ"),
        ("12", "12 giờ"), ("24", "24 giờ (1 ngày)"),
        ("48", "48 giờ (2 ngày)"), ("72", "72 giờ (3 ngày)"),
        ("168", "1 tuần"), ("336", "2 tuần"),
    ]
    F47_PLATFORM_OPTIONS = ["Facebook", "TikTok", "Zalo", "YouTube",
                            "Instagram", "Twitter / X", "Threads"]
    F47_SCOPE_OPTIONS = [
        "Toàn Trung đoàn",
        "Cơ quan Tham mưu",
        "Cơ quan Chính Trị",
        "Cơ quan HC-KT",
        "Tiểu đoàn 7 (Đại đội 1-4)",
        "Tiểu đoàn 8 (Đại đội 5-8)",
        "Tiểu đoàn 9 (Đại đội 9-12)",
        "Đại đội 14",
        "Đại đội 15",
        "Đại đội 16",
        "Đại đội 17",
        "Đại đội 18",
        "Đại đội 20",
        "Đại đội 24",
        "Đại đội 25",
    ]

    @classmethod
    def _scope_unit_list(cls) -> list[str]:
        """Tất cả đơn vị (không gồm 'Toàn Trung đoàn' — đó là master select)."""
        return [s for s in cls.F47_SCOPE_OPTIONS if s != "Toàn Trung đoàn"]

    def _build_scope_picker(self, initial_scope) -> tuple[ft.Control, callable]:
        """Trả về (UI compact, getter()) cho multi-select scope picker.

        UI compact: 1 ô có chữ tóm tắt + icon mũi tên — bấm vào mở dialog
        với checkbox list (giống date picker). Tiết kiệm không gian dialog chính.
        """
        all_units = self._scope_unit_list()
        is_admin = self._is_admin()
        profile = store.get("userProfile", store.seed_user_profile)
        my_unit_id = profile.get("unitId") or ""
        units_tree = store.get("units", store.seed_units)
        unit_name_map = self._unit_name_map(units_tree)
        my_unit_name = unit_name_map.get(my_unit_id, "")

        if is_admin:
            units = all_units
        else:
            units = [my_unit_name] if my_unit_name in all_units else ([] if not my_unit_name else [my_unit_name])
        if isinstance(initial_scope, list):
            selected = set(initial_scope)
        elif isinstance(initial_scope, str) and initial_scope:
            if initial_scope == "Toàn Trung đoàn":
                selected = set(units)
            else:
                selected = {initial_scope}
        else:
            selected = set(units)

        # State holder (mutable)
        state = {"selected": set(selected)}

        def _summary_text() -> str:
            sel = state["selected"]
            if len(sel) == len(units) and len(units) > 1:
                return "🏛 Toàn Trung đoàn"
            if len(sel) == 0:
                return "(Chưa chọn đơn vị nào)"
            if len(sel) <= 2:
                return ", ".join(sorted(sel))
            return f"{len(sel)} đơn vị"

        summary_label = ft.Text(_summary_text(), size=13, expand=True,
                                weight=ft.FontWeight.W_500)

        def open_picker(e=None):
            page = self.page
            # Tạo checkbox list — dùng BottomSheet thay vì AlertDialog
            # để KHÔNG ghi đè dialog phát động đang mở.
            unit_cbs: list[ft.Checkbox] = []
            all_cb = ft.Checkbox(label="🏛 Toàn Trung đoàn (chọn tất cả)",
                                 value=(len(state["selected"]) == len(units)))

            def _on_unit(e=None):
                n = sum(1 for cb in unit_cbs if cb.value)
                all_cb.value = (n == len(unit_cbs))
                page.update()

            def _on_all(e=None):
                for cb in unit_cbs:
                    cb.value = bool(all_cb.value)
                page.update()

            for u in units:
                cb = ft.Checkbox(label=u, value=(u in state["selected"]),
                                 on_change=_on_unit)
                unit_cbs.append(cb)
            all_cb.on_change = _on_all

            def close_sheet():
                try:
                    _bs4.open = False
                except Exception:
                    pass
                page.update()

            def save_picker(_):
                state["selected"] = {cb.label for cb in unit_cbs if cb.value}
                summary_label.value = _summary_text()
                close_sheet()

            _bs4 = ft.BottomSheet(
                open=True,
                content=ft.Container(
                    content=ft.Column(
                        [
                            ft.Container(
                                content=ft.Row([
                                    ft.Text("Chọn phạm vi", size=15,
                                            weight=ft.FontWeight.BOLD, expand=True),
                                    ft.IconButton(ft.Icons.CLOSE,
                                                  on_click=lambda e: close_sheet(),
                                                  icon_size=20),
                                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                padding=ft.padding.only(left=16, right=4, top=8, bottom=0),
                            ),
                            ft.Divider(height=1),
                            ft.Container(
                                content=ft.Column(
                                    [all_cb, ft.Divider(height=1)] + unit_cbs,
                                    spacing=2, tight=True, scroll=ft.ScrollMode.AUTO,
                                ),
                                padding=ft.padding.symmetric(horizontal=16),
                                expand=True,
                            ),
                            ft.Container(
                                content=ft.Row([
                                    ft.TextButton("Huỷ", on_click=lambda e: close_sheet()),
                                    ft.ElevatedButton("Áp dụng", on_click=save_picker,
                                                      bgcolor=GREEN_MID, color=ft.Colors.WHITE),
                                ], alignment=ft.MainAxisAlignment.END, spacing=10),
                                padding=ft.padding.symmetric(horizontal=16, vertical=8),
                            ),
                        ],
                        spacing=0, tight=True,
                    ),
                    height=500,
                    bgcolor=BG,
                    border_radius=ft.border_radius.only(top_left=16, top_right=16),
                ),
            )
            page.overlay.append(_bs4)
            page.update()

        # Compact UI: clickable container giống date picker
        ui = ft.Container(
            content=ft.Column([
                ft.Text("Phạm vi *", size=12, color=TEXT_MUTED,
                        weight=ft.FontWeight.W_500),
                ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.GROUPS, color=GREEN_MID, size=20),
                        summary_label,
                        ft.Icon(ft.Icons.ARROW_DROP_DOWN, color=TEXT_MUTED, size=22),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                    bgcolor=BG2, border_radius=8,
                    padding=ft.padding.symmetric(horizontal=12, vertical=12),
                    border=ft.border.all(1, BORDER),
                    on_click=open_picker, ink=True,
                ),
            ], spacing=4, tight=True),
        )

        def getter() -> list[str]:
            return list(state["selected"])

        return ui, getter

    def f47_open_camp_menu(self, camp: dict) -> None:
        """Popup ✏️ Sửa / 🗑 Xoá cho 1 chiến dịch (admin)."""
        page = self.page

        def close_dlg():
            try:
                _dlg.open = False
            except Exception:
                pass
            page.update()

        def do_edit(e):
            close_dlg()
            self.f47_open_create(existing=camp)

        def do_delete(e):
            close_dlg()
            self.f47_confirm_delete(camp)

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Tuỳ chọn — {camp.get('title','')[:50]}",
                          size=14, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column([
                    ft.TextButton("✏️  Sửa chiến dịch", on_click=do_edit,
                                  style=ft.ButtonStyle(padding=12)),
                    ft.TextButton("🗑  Xoá chiến dịch", on_click=do_delete,
                                  style=ft.ButtonStyle(padding=12, color=RED)),
                ], spacing=4, tight=True),
                width=320,
            ),
            actions=[
                ft.TextButton("Đóng",
                              on_click=lambda e: close_dlg()),
            ],
        )
        _show_dialog(self.page, _dlg)

    def f47_confirm_delete(self, camp: dict) -> None:
        """Xác nhận trước khi xoá chiến dịch."""
        page = self.page
        cid = camp.get("id", "")
        title = camp.get("title", "")

        def do(e):
            try:
                _dlg.open = False
            except Exception:
                pass
            page.update()
            # Đánh dấu NGAY để sync không ghi đè trong khi đợi DB
            store.STORE.record_v2_delete("f47Campaigns", str(cid))
            # Cập nhật local cache ngay lập tức
            camps_now = store.get("f47Campaigns", store.seed_f47)
            camps_now = [c for c in camps_now if c.get("id") != cid]
            store.STORE.set_local("f47Campaigns", camps_now)
            # Xoá khỏi DB trong background
            def _delete_worker():
                import time as _t2
                deleted = False
                for _attempt in range(5):  # 5 lần retry, mỗi lần cách 6s
                    try:
                        FS.delete_doc(f"v2_f47Campaigns/e141/{cid}")
                        deleted = True
                        break
                    except Exception:
                        _t2.sleep(6)
                if deleted:
                    # Xoá thành công → xác nhận và sync_value để cập nhật các thiết bị khác
                    store.STORE.confirm_v2_delete("f47Campaigns", str(cid))
                    store.set_value("f47Campaigns", store.STORE._load().get("f47Campaigns", []))
                # Dù xoá được hay không, local đã filtered → hiển thị đúng
                store.log_activity(f"Xoá F47: {title[:40]}")
                def _go():
                    self.toast(f"🗑 Đã xoá: {title[:40]}")
                    self.body.content = self.module_f47()
                    self.refresh()
                try:
                    page.run_thread(_go)
                except Exception:
                    _go()
            import threading as _t
            _t.Thread(target=_delete_worker, daemon=True).start()
            self.toast("⏳ Đang xoá...")

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Xác nhận xoá", weight=ft.FontWeight.BOLD),
            content=ft.Text(
                f"Xoá chiến dịch '{title}'?\nTất cả minh chứng đã nộp sẽ mất.",
                size=12,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton("Xoá", on_click=do,
                                  bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def f47_open_create(self, existing: dict | None = None) -> None:
        """Dialog phát động chiến dịch F47 mới (hoặc sửa nếu existing != None)."""
        page = self.page
        is_edit = existing is not None
        ex = existing or {}

        # Tính thời hạn còn lại (giờ) để gợi ý mặc định cho dropdown khi edit
        if is_edit:
            try:
                left_h = max(1, (int(ex.get("deadline", 0)) - store.now_ms()) // 3600_000)
            except Exception:
                left_h = 24
            default_hours = str(left_h) if left_h > 0 else "24"
        else:
            default_hours = "24"

        type_dropdown = ft.Dropdown(
            label="Loại chiến dịch *",
            value=ex.get("campaignType", "Báo cáo"),
            options=[
                ft.dropdown.Option("Báo cáo", "Báo cáo"),
                ft.dropdown.Option("CMT", "CMT"),
                ft.dropdown.Option("Báo cáo khẩn", "Báo cáo khẩn"),
                ft.dropdown.Option("Khác", "Khác"),
            ],
            border_radius=8, dense=True,
        )

        title_input = ft.TextField(label="Tiêu đề chiến dịch *",
                                   value=ex.get("title", ""),
                                   border_radius=8, dense=True)
        desc_input = ft.TextField(label="Mô tả nội dung *",
                                  value=ex.get("desc", ""),
                                  border_radius=8, dense=True,
                                  multiline=True, min_lines=2, max_lines=4)
        target_link_input = ft.TextField(label="Link bài viết (nếu có)",
                                         value=ex.get("targetLink", ""),
                                         hint_text="https://facebook.com/...",
                                         border_radius=8, dense=True)

        hours_dropdown = ft.Dropdown(
            label="Thời hạn *",
            value=default_hours,
            options=[ft.dropdown.Option(key, text) for key, text in self.F47_HOUR_OPTIONS],
            border_radius=8, dense=True,
        )

        ex_platforms = set(ex.get("platforms") or [])
        if not ex_platforms:
            ex_platforms = {"Facebook", "TikTok"}
        platform_checkboxes = [
            ft.Checkbox(label=p, value=(p in ex_platforms))
            for p in self.F47_PLATFORM_OPTIONS
        ]
        platforms_section = ft.Container(
            content=ft.Column(
                [
                    ft.Text("Nền tảng *", size=12, color=TEXT_MUTED,
                            weight=ft.FontWeight.W_500),
                    ft.Container(
                        content=ft.Column(
                            [ft.Row([cb], wrap=True) for cb in platform_checkboxes],
                            spacing=2, tight=True,
                        ),
                        bgcolor=BG2, border_radius=8, padding=8,
                        border=ft.border.all(1, BORDER),
                    ),
                ],
                spacing=4, tight=True,
            ),
        )

        # Ph?m vi: multi-select checkbox cho F47
        scope_ui, get_scope = self._build_scope_picker(
            ex.get("scopeUnits") or ex.get("scope")
        )

        # FilePicker upload mẫu
        ex_sample_media = [ex.get("sampleMediaUrl") or ""]
        attached_label = ft.Text("📎 Đã đính kèm ảnh mẫu" if ex_sample_media[0] else "", size=11, color=GREEN_MID)

        def on_files_picked(e: ft.FilePickerResultEvent):
            if not e.files:
                return
            if not store.STORE.is_bound() or not AUTH_STATE.get("idToken"):
                attached_label.value = "❌ Phải đăng nhập để upload"
                page.update(); return
            attached_label.value = f"⏳ Đang upload ảnh mẫu..."
            page.update()

            def worker():
                f = e.files[0]
                try:
                    remote = fb_storage.make_remote_path(f"f47/samples/{AUTH_STATE['uid']}", f.name)
                    if f.path:
                        res = fb_storage.upload_file(f.path, remote, AUTH_STATE["idToken"])
                    elif f.bytes:
                        res = fb_storage.upload_data(remote, f.bytes, AUTH_STATE["idToken"], f.name)
                    else:
                        attached_label.value = "❌ Không đọc được file"
                        page.update(); return
                    ex_sample_media[0] = res["downloadURL"]
                    attached_label.value = f"📎 Đã tải lên mẫu"
                except Exception as ex_e:
                    attached_label.value = f"❌ Lỗi: {ex_e}"
                try:
                    page.update()
                except Exception:
                    pass

            import threading
            threading.Thread(target=worker, daemon=True).start()

        picker = ft.FilePicker(on_result=on_files_picked)
        if picker not in page.overlay:
            page.overlay.append(picker)

        sample_section = ft.Container(
            content=ft.Row([
                ft.ElevatedButton("📷 Up ảnh mẫu", on_click=lambda e: picker.pick_files(file_type=ft.FilePickerFileType.IMAGE)),
                attached_label,
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            margin=ft.margin.only(top=5, bottom=5)
        )

        err_text = ft.Text("", color=RED, size=12)

        def submit(e):
            title = (title_input.value or "").strip()
            desc = (desc_input.value or "").strip()
            target_link = (target_link_input.value or "").strip()
            try:
                hours = int(hours_dropdown.value or "24")
            except (ValueError, TypeError):
                hours = 24
            if not title or not desc:
                err_text.value = "⚠️ Tiêu đề và mô tả bắt buộc"
                page.update(); return
            platforms = [cb.label for cb in platform_checkboxes if cb.value]
            if not platforms:
                err_text.value = "⚠️ Chọn ít nhất 1 nền tảng"
                page.update(); return

            now = store.now_ms()
            profile = store.get("userProfile", store.seed_user_profile)
            soldiers = store.get("soldiers", store.seed_soldiers)
            is_admin = self._is_admin()
            selected_scopes = get_scope()
            if is_admin and len(selected_scopes) == len(self._scope_unit_list()):
                members = [s["id"] for s in soldiers if not s.get("isAdmin")]
            else:
                units_tree = store.get("units", store.seed_units)
                unit_name_map = self._unit_name_map(units_tree)
                allowed_unit_ids = [k for k, v in unit_name_map.items() if v in selected_scopes]
                members = [s["id"] for s in soldiers if not s.get("isAdmin") and s.get("unitId") in allowed_unit_ids]

            camps = store.get("f47Campaigns", store.seed_f47)
            if is_edit:
                # Update tại chỗ
                for c in camps:
                    if c.get("id") == ex.get("id"):
                        c["title"] = title
                        c["campaignType"] = type_dropdown.value
                        c["desc"] = desc
                        c["targetLink"] = target_link
                        c["deadline"] = now + hours * 3600_000
                        c["platforms"] = platforms
                        c["scopeUnits"] = get_scope()
                        c["scope"] = ("Toàn Trung đoàn"
                                      if len(get_scope()) == len(self._scope_unit_list())
                                      else (", ".join(get_scope()) or "Toàn Trung đoàn"))
                        c["sampleMediaUrl"] = ex_sample_media[0]
                        # GIỮ submissions cũ, GIỮ members cũ + bổ sung member mới
                        # (nếu có ai mới đăng ký sau khi tạo)
                        existing_members = c.get("members") or []
                        c["members"] = list({*existing_members, *members})
                        break
                store.set_value("f47Campaigns", camps)
                store.log_activity(f"Sửa F47: {title}")
                self.toast(f"✏️ Đã cập nhật: {title}")
            else:
                new_camp = {
                    "id": f"c_{now}",
                    "title": title,
                    "campaignType": type_dropdown.value,
                    "desc": desc,
                    "targetLink": target_link,
                    "creator": profile.get("name") or AUTH_STATE.get("username", ""),
                    "creatorRole": profile.get("role", ""),
                    "createdByUid": AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or "",
                    "deadline": now + hours * 3600_000,
                    "createdAt": now,
                    "platforms": platforms,
                    "scopeUnits": get_scope(),
                    "scope": ("Toàn Trung đoàn"
                              if len(get_scope()) == len(self._scope_unit_list())
                              else (", ".join(get_scope()) or "Toàn Trung đoàn")),
                    "sampleMediaUrl": ex_sample_media[0],
                    "members": members,
                    "submissions": {},
                    "status": "live",
                }
                camps.insert(0, new_camp)
                store.set_value("f47Campaigns", camps)
                store.log_activity(f"Phát động F47: {title}")
                # Notif TARGETED — chỉ gửi đến members của chiến dịch
                _link = f"f47:{new_camp['id']}"
                _creator_name = profile.get("name") or profile.get("username") or "Quản trị"
                for muid in members:
                    if muid:
                        store.push_notif(
                            "f47", "🛡 Chiến dịch F47 mới cho bạn",
                            f"{title} • Hạn {hours}h", _link,
                            target_uid=muid,
                            sender_name=_creator_name,
                        )
                self.toast(f"Đã phát động: {title}")
            try:
                _dlg.open = False
            except Exception:
                pass
            self.body.content = self.module_f47()
            self.page.update()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                "✏️ Sửa chiến dịch F47" if is_edit else "🛡 Phát động chiến dịch F47",
                size=15, weight=ft.FontWeight.BOLD,
            ),
            content=ft.Container(
                content=ft.Column(
                    [type_dropdown, title_input, desc_input, target_link_input, hours_dropdown,
                     platforms_section, scope_ui, sample_section, err_text],
                    spacing=10, tight=True, scroll=ft.ScrollMode.AUTO,
                ),
                width=380, height=580,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton(
                    "Lưu thay đổi" if is_edit else "Phát động",
                    on_click=submit,
                    bgcolor=GREEN_MID if is_edit else RED,
                    color=ft.Colors.WHITE,
                ),
            ],
        )
        _show_dialog(self.page, _dlg)

    def _my_notifs(self) -> list:
        all_my_notifs = store.filter_notifs_for_user(
            store.get("notifs", store.seed_notifs),
            AUTH_STATE.get("uid") or "",
        )
        if not self._is_admin():
            all_my_notifs = [
                n for n in all_my_notifs
                if not (n.get("type") == "unit" and "cần duyệt" in str(n.get("title") or "").lower())
            ]
        return all_my_notifs

    def _is_admin(self) -> bool:
        """Kiểm tra user hiện tại có quyền admin không (dựa trên adminLevel)."""
        profile = store.get("userProfile", store.seed_user_profile)
        return bool(profile.get("isAdmin")) or profile.get("adminLevel", 0) >= 3

    def f47_open_share(self) -> None:
        """Dialog chia sẻ bài viết hàng ngày — link + ảnh + nền tảng."""
        page = self.page
        link_input = ft.TextField(
            label="Link bài viết *",
            hint_text="https://facebook.com/...",
            border_radius=8, dense=True,
        )
        note_input = ft.TextField(
            label="Nội dung / ghi chú", border_radius=8, dense=True,
            multiline=True, min_lines=2, max_lines=4,
        )
        platform_dropdown = ft.Dropdown(
            label="Nền tảng *",
            value="Facebook",
            options=[ft.dropdown.Option(p) for p in self.F47_PLATFORM_OPTIONS],
            border_radius=8, dense=True,
        )
        status_text = ft.Text("", size=12, color=TEXT_MUTED)
        uploaded: list[dict] = []

        def on_files_picked(e: ft.FilePickerResultEvent):
            if not e.files:
                return
            if not store.STORE.is_bound() or not AUTH_STATE.get("idToken"):
                status_text.value = "❌ Phải đăng nhập trước khi upload"
                page.update(); return
            status_text.value = f"⏳ Đang upload {len(e.files)} ảnh..."
            page.update()

            def worker():
                ok = 0
                for f in e.files:
                    try:
                        remote = fb_storage.make_remote_path(
                            f"shares/{AUTH_STATE['uid']}", f.name
                        )
                        if f.path:
                            res = fb_storage.upload_file(f.path, remote, AUTH_STATE["idToken"])
                        elif f.bytes:
                            res = fb_storage.upload_data(remote, f.bytes, AUTH_STATE["idToken"], f.name)
                        else:
                            continue
                        uploaded.append(res)
                        ok += 1
                    except Exception as ex:
                        status_text.value = f"❌ Upload lỗi: {ex}"
                status_text.value = f"✅ Đã upload {ok}/{len(e.files)} ảnh"
                try:
                    page.update()
                except Exception:
                    pass

            threading.Thread(target=worker, daemon=True).start()

        picker = ft.FilePicker(on_result=on_files_picked)
        if picker not in page.overlay:
            page.overlay.append(picker)

        pick_btn = ft.ElevatedButton(
            "📷 Đính kèm ảnh",
            on_click=lambda e: picker.pick_files(
                allow_multiple=True, file_type=ft.FilePickerFileType.IMAGE,
            ),
        )

        def submit(e):
            link = (link_input.value or "").strip()
            if not link:
                status_text.value = "⚠️ Cần nhập link bài viết"
                page.update(); return
            now = store.now_ms()
            profile = store.get("userProfile", store.seed_user_profile)
            new_share = {
                "id": f"ds_{now}",
                "userId": AUTH_STATE.get("uid", ""),
                "userName": profile.get("name") or AUTH_STATE.get("username", ""),
                "platform": platform_dropdown.value or "Facebook",
                "links": [link],
                "images": [u["downloadURL"] for u in uploaded],
                "imageCount": len(uploaded),
                "note": (note_input.value or "").strip(),
                "at": now,
            }
            shares = store.get("dailyShares", store.seed_daily_shares)
            shares.insert(0, new_share)
            # Giới hạn 1000 bản ghi gần nhất để doc Firestore không quá to
            if len(shares) > 1000:
                del shares[1000:]
            store.set_value("dailyShares", shares)
            store.log_activity(f"Chia sẻ: {link[:60]}")
            try:
                _dlg.open = False
            except Exception:
                pass
            self.toast("✅ Đã đăng bài chia sẻ")
            self.body.content = self.module_f47()
            self.refresh()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("📤 Chia sẻ bài viết hàng ngày", size=15,
                          weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [link_input, platform_dropdown, note_input,
                     pick_btn, status_text],
                    spacing=10, tight=True, scroll=ft.ScrollMode.AUTO,
                ),
                width=380, height=420,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton("Đăng bài", on_click=submit,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def f47_open_campaign_detail(self, camp: dict) -> None:
        """Mở trang chi tiết chiến dịch — tỷ lệ theo đơn vị, người làm/chưa làm, giục."""
        camp_id = camp.get("id", "")
        # Refresh từ store để chắc chắn lấy submissions mới nhất
        camps = store.get("f47Campaigns", store.seed_f47)
        cur = next((c for c in camps if c.get("id") == camp_id), camp)

        soldiers = store.get("soldiers", store.seed_soldiers)
        soldier_by_id = {str(s.get("id")): s for s in soldiers if s.get("id")}
        units_tree = store.get("units", store.seed_units)
        unit_name_map = self._unit_name_map(units_tree)

        members = cur.get("members") or []
        subs = cur.get("submissions") or {}
        deadline = int(cur.get("deadline", 0) or 0)
        created = int(cur.get("createdAt", 0) or 0)
        now = store.now_ms()

        # Tách 2 nhóm
        done_list, pending_list = [], []
        for uid in members:
            sub = subs.get(uid)
            s = soldier_by_id.get(str(uid)) or {}
            if sub:
                at = int((sub or {}).get("at", 0) or 0)
                # Phân loại tốc độ: tính theo % thời gian dùng so với tổng thời gian
                if deadline > created and at > 0:
                    used = (at - created) / max(1, deadline - created)
                else:
                    used = 0.5
                speed = "fast" if used < 0.33 else ("normal" if used < 0.75 else "slow")
                done_list.append({
                    "uid": uid, "soldier": s, "sub": sub,
                    "at": at, "speed": speed,
                })
            else:
                pending_list.append({"uid": uid, "soldier": s})

        # Sort: done theo at tăng dần (làm sớm nhất ở trên), pending theo tên
        done_list.sort(key=lambda x: x["at"])
        pending_list.sort(key=lambda x: (x["soldier"].get("name") or "").lower())

        # Tỷ lệ theo đơn vị (group theo unitId)
        unit_stats: dict[str, dict] = {}
        for uid in members:
            s = soldier_by_id.get(str(uid)) or {}
            unit_id = s.get("unitId") or "unknown"
            it = unit_stats.setdefault(unit_id, {
                "name": unit_name_map.get(unit_id, "Khác"),
                "total": 0, "done": 0,
            })
            it["total"] += 1
            if uid in subs:
                it["done"] += 1
        unit_rows = sorted(unit_stats.values(),
                           key=lambda u: (-u["done"] / max(1, u["total"]), u["name"]))

        # === RBAC Lọc hiển thị ===
        my_uid = AUTH_STATE.get("uid") or ""
        profile = store.get("userProfile", store.seed_user_profile)
        my_unit_id = profile.get("unitId") or ""
        my_admin_level = int(profile.get("adminLevel") or 1)
        is_admin = self._is_admin()

        # Lưu lại tổng số cho thanh tiến độ
        campaign_done_n = len(done_list)
        campaign_total_n = len(members)

        if not is_admin:
            if my_admin_level >= 2:
                # Cấp cán bộ: thấy người cùng đơn vị
                done_list = [x for x in done_list if x["soldier"].get("unitId") == my_unit_id]
                pending_list = [x for x in pending_list if x["soldier"].get("unitId") == my_unit_id]
                unit_rows = [u for u in unit_rows if str(u.get("name")) == str(unit_name_map.get(my_unit_id, "Khác"))]
            else:
                # Cấp cá nhân: chỉ thấy bản thân, giấu bảng tỷ lệ đơn vị
                done_list = [x for x in done_list if str(x["uid"]) == str(my_uid)]
                pending_list = [x for x in pending_list if str(x["uid"]) == str(my_uid)]
                unit_rows = []

        # ============ Build UI ============
        def soldier_label(s: dict, uid: str) -> str:
            name_part = f"{(s.get('rank') or '').strip()} {(s.get('name') or '').strip()}".strip()
            if name_part:
                return f"đ.c {name_part}"
            return s.get("username") or uid

        # Header card
        deadline_left = deadline - now
        if deadline_left <= 0:
            cd_str = "⚠️ Hết giờ"
            cd_color = RED
        else:
            h, rem = divmod(deadline_left, 3600_000)
            m, _ = divmod(rem, 60_000)
            cd_str = f"⏱ Còn {h:02d}h {m:02d}m"
            cd_color = AMBER
        platforms = ", ".join(cur.get("platforms") or []) or "—"

        total_n = campaign_total_n
        done_n = campaign_done_n
        pct = int(done_n / total_n * 100) if total_n else 0

        header = ft.Container(
            content=ft.Column(
                [
                    ft.Text(cur.get("title", ""), size=15,
                            weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(cur.get("desc", ""), size=12, color=TEXT_MUTED),
                    ft.Container(height=4),
                    ft.Row([
                        ft.Text("📅 Phát động:", size=11, color=TEXT_MUTED, width=110),
                        ft.Text(fmt_dt(created), size=11),
                    ]),
                    ft.Row([
                        ft.Text("👤 Người phát động:", size=11, color=TEXT_MUTED, width=110),
                        ft.Text("đ.c {} – {}".format(*_resolve_creator(cur)),
                                size=11, expand=True),
                    ]),
                    ft.Row([
                        ft.Text("🎯 Phạm vi:", size=11, color=TEXT_MUTED, width=110),
                        ft.Text(cur.get("scope", ""), size=11),
                    ]),
                    ft.Row([
                        ft.Text("📱 Nền tảng:", size=11, color=TEXT_MUTED, width=110),
                        ft.Text(platforms, size=11),
                    ]),
                    *([
                        ft.Row([
                            ft.Text("🔗 Link bài viết:", size=11, color=TEXT_MUTED, width=110),
                            ft.TextButton(
                                content=ft.Row([
                                    ft.Text(cur["targetLink"], size=11, color=BLUE, weight=ft.FontWeight.BOLD, overflow=ft.TextOverflow.ELLIPSIS),
                                    ft.Icon(ft.Icons.OPEN_IN_NEW, size=12, color=BLUE)
                                ], spacing=4, tight=True),
                                on_click=lambda e, url=cur["targetLink"]: self.page.launch_url(url),
                                style=ft.ButtonStyle(padding=0),
                            )
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER)
                    ] if cur.get("targetLink") else []),
                    *(
                        [
                            ft.Container(height=4),
                            ft.Text("📸 Ảnh/Video Mẫu đính kèm:", size=11, color=TEXT_MUTED),
                            ft.Container(
                                content=ft.Image(src=cur["sampleMediaUrl"], border_radius=8,
                                                 width=300, fit=ft.ImageFit.CONTAIN),
                                on_click=lambda e, u=cur["sampleMediaUrl"]: open_image_viewer(self.page, u),
                                ink=True, border_radius=8,
                                tooltip="Bấm để xem ảnh",
                            )
                        ] if cur.get("sampleMediaUrl") else []
                    ),
                    ft.Container(
                        content=ft.Text(cd_str, size=14, weight=ft.FontWeight.BOLD,
                                        color=cd_color),
                        bgcolor="#fff8e1", border_radius=8, padding=10,
                        border=ft.border.all(1, "#f0c040"),
                        margin=ft.margin.only(top=6),
                    ),
                    ft.Row([
                        ft.Text("Tổng tiến độ", size=12, color=TEXT_MUTED, expand=True),
                        ft.Text(f"{done_n}/{total_n} ({pct}%)", size=12,
                                weight=ft.FontWeight.BOLD,
                                color=GREEN_MID if pct >= 50 else RED),
                    ]),
                    ft.ProgressBar(value=pct/100 if total_n else 0,
                                   color=GREEN_MID, bgcolor="#eee", height=10),
                ],
                spacing=4,
            ),
            bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
            padding=14, margin=ft.margin.only(bottom=10),
        )

        # Section: tỷ lệ theo đơn vị
        unit_section_items = []
        for idx, u in enumerate(unit_rows):
            up = int(u["done"] / max(1, u["total"]) * 100)
            color = GREEN_MID if up == 100 else (AMBER if up >= 50 else RED)
            medal = "🏆" if idx == 0 and up > 0 else ("🥈" if idx == 1 and up > 0 else ("🥉" if idx == 2 and up > 0 else ""))
            unit_section_items.append(
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Text(f"{medal} {u['name']}".strip(), size=12, weight=ft.FontWeight.W_600,
                                    expand=True),
                            ft.Text(f"{u['done']}/{u['total']} ({up}%)",
                                    size=11, color=color, weight=ft.FontWeight.BOLD),
                        ]),
                        ft.ProgressBar(value=up/100, color=color, bgcolor="#eee", height=6),
                    ], spacing=4),
                    padding=ft.padding.symmetric(vertical=6),
                    border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                )
            )
        unit_section = ft.Container(
            content=ft.Column(
                [
                    ft.Text("🏆 Bảng Xếp Hạng Đơn vị", size=13,
                            weight=ft.FontWeight.BOLD),
                    ft.Container(height=4),
                    *(unit_section_items if unit_section_items else
                      [ft.Text("Chưa có đơn vị/quân nhân nào.",
                               size=11, color=TEXT_MUTED)]),
                ],
                spacing=0,
            ),
            bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
            padding=14, margin=ft.margin.only(bottom=10),
        )

        # Section: người đã làm (sắp theo tốc độ)
        speed_badge = {
            "fast": ("⚡ Nhanh", GREEN_MID),
            "normal": ("🆗 Đúng giờ", AMBER),
            "slow": ("🐢 Chậm", RED),
        }

        def done_row(item: dict) -> ft.Container:
            s = item["soldier"]
            badge_text, badge_color = speed_badge[item["speed"]]
            sub = item["sub"] or {}
            note = sub.get("note") or ""
            links = sub.get("links") or []
            return ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Container(
                            content=ft.Text(initials(s.get("name") or item["uid"], 2),
                                            color=ft.Colors.WHITE, size=10,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=GREEN_DARK, width=28, height=28,
                            border_radius=14, alignment=ft.alignment.center,
                        ),
                        ft.Column([
                            ft.Text(soldier_label(s, item["uid"]),
                                    size=12, weight=ft.FontWeight.W_700),
                            ft.Text(f"Nộp lúc {fmt_dt(item['at'])}", size=10,
                                    color=TEXT_MUTED),
                        ], spacing=1, expand=True, tight=True),
                        ft.Container(
                            content=ft.Text(badge_text, size=10,
                                            color=ft.Colors.WHITE,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=badge_color, border_radius=10,
                            padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        ),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                    *([ft.Text(note, size=11, color=TEXT_MUTED,
                              max_lines=2)] if note else []),
                    *([ft.Text(f"🔗 {len(links)} link  •  📷 {sub.get('imageCount',0)} ảnh",
                              size=10, color=TEXT_MUTED)] if links or sub.get("imageCount") else []),
                ], spacing=4),
                padding=10,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )

        done_section = ft.Container(
            content=ft.Column(
                [
                    ft.Row([
                        ft.Text(f"🥇 Bảng Xếp Hạng Cá Nhân ({len(done_list)})", size=13,
                                weight=ft.FontWeight.BOLD, color=GREEN_DARK,
                                expand=True),
                    ]),
                    *(
                        [done_row(it) for it in done_list] if done_list else
                        [ft.Container(
                            content=ft.Text("Chưa có ai nộp.", size=11, color=TEXT_MUTED),
                            padding=10,
                        )]
                    ),
                ],
                spacing=0,
            ),
            bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
            margin=ft.margin.only(bottom=10),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        # Section: người chưa làm (kèm nút giục)
        def _do_remind_one(uid: str, name: str, refresh_at_end: bool = True):
            """Giục 1 người: notif targeted + fcm queue + lưu lịch sử."""
            reminders = store.get("f47Reminders", lambda: {})
            key = f"{camp_id}::{uid}"
            reminders[key] = {
                "by": AUTH_STATE.get("uid", ""),
                "byName": store.get("userProfile", store.seed_user_profile).get(
                    "name") or AUTH_STATE.get("username", ""),
                "at": store.now_ms(),
            }
            store.set_value("f47Reminders", reminders)
            # Notif CÓ targetUid → chỉ user uid mới thấy trong list thông báo
            _reminder_by = store.get("userProfile", store.seed_user_profile).get("name") or "Chỉ huy"
            store.push_notif(
                "f47", f"📢 Bạn được giục",
                f"Hãy làm chiến dịch '{cur.get('title','')[:60]}'",
                f"f47:{camp_id}",
                target_uid=uid,
                sender_name=_reminder_by,
            )
            store.log_activity(f"Giục F47 [{cur.get('title','')[:30]}]: {name}")
            if refresh_at_end:
                self.toast(f"✅ Đã giục {name}")
                self.f47_open_campaign_detail(cur)

        def remind_user(uid: str, name: str):
            _do_remind_one(uid, name)

        def remind_all():
            count = 0
            for it in pending_list:
                _do_remind_one(it["uid"],
                               soldier_label(it["soldier"], it["uid"]),
                               refresh_at_end=False)
                count += 1
            self.toast(f"✅ Đã giục {count} người")
            self.f47_open_campaign_detail(cur)

        reminders_log = store.get("f47Reminders", lambda: {})

        def pending_row(item: dict) -> ft.Container:
            s = item["soldier"]
            uid = item["uid"]
            name = soldier_label(s, uid)
            rkey = f"{camp_id}::{uid}"
            rec = reminders_log.get(rkey)
            if rec:
                btn = ft.Container(
                    content=ft.Text(f"Đã giục {time_ago(rec.get('at',0))}",
                                    size=10, color=TEXT_MUTED),
                    padding=ft.padding.symmetric(horizontal=8, vertical=4),
                )
            else:
                btn = ft.ElevatedButton(
                    "📢 Giục",
                    on_click=lambda e, _u=uid, _n=name: remind_user(_u, _n),
                    bgcolor=AMBER, color=ft.Colors.WHITE,
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=8),
                        padding=ft.padding.symmetric(horizontal=12, vertical=6),
                        text_style=ft.TextStyle(size=11, weight=ft.FontWeight.BOLD),
                    ),
                ) if is_admin else ft.Container()
            return ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Text(initials(s.get("name") or uid, 2),
                                        color=ft.Colors.WHITE, size=10,
                                        weight=ft.FontWeight.BOLD),
                        bgcolor="#9e9e9e", width=28, height=28,
                        border_radius=14, alignment=ft.alignment.center,
                    ),
                    ft.Column([
                        ft.Text(name, size=12, weight=ft.FontWeight.W_600),
                        ft.Text(s.get("role") or "Chiến sĩ", size=10,
                                color=TEXT_MUTED),
                    ], spacing=1, expand=True, tight=True),
                    btn,
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                padding=10,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )

        pending_n = len(pending_list)
        pending_section = ft.Container(
            content=ft.Column(
                [
                    ft.Row([
                        ft.Text(f"❌ Danh sách Chưa làm ({pending_n})", size=13,
                                weight=ft.FontWeight.BOLD, color=RED, expand=True),
                        (ft.ElevatedButton(
                            "📢 Giục tất cả",
                            on_click=lambda e: remind_all(),
                            bgcolor=RED, color=ft.Colors.WHITE,
                            style=ft.ButtonStyle(
                                shape=ft.RoundedRectangleBorder(radius=8),
                                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                                text_style=ft.TextStyle(size=11, weight=ft.FontWeight.BOLD),
                            ),
                        ) if (pending_list and is_admin) else ft.Container()),
                    ]),
                    *(
                        [pending_row(it) for it in pending_list] if pending_list else
                        [ft.Container(
                            content=ft.Text("🎉 Toàn đơn vị đã làm xong!",
                                            size=12, color=GREEN_MID,
                                            weight=ft.FontWeight.W_600),
                            padding=14,
                        )]
                    ),
                ],
                spacing=0,
            ),
            bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
            margin=ft.margin.only(bottom=10),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        # Nút "Nộp minh chứng" cho chính người đang xem (nếu chưa nộp)
        my_uid = AUTH_STATE.get("uid") or ""
        action_btn = None
        if my_uid in members and my_uid not in subs:
            action_btn = ft.Container(
                content=ft.ElevatedButton(
                    "📤 Tôi nộp minh chứng",
                    on_click=lambda e: self.f47_open_submit(cur),
                    bgcolor=GREEN_MID, color=ft.Colors.WHITE,
                    width=10000,
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=8),
                        padding=12,
                        text_style=ft.TextStyle(size=14, weight=ft.FontWeight.BOLD),
                    ),
                ),
                margin=ft.margin.only(bottom=10),
            )

        body_col = ft.Column(
            [
                ft.Container(
                    content=ft.Row([
                        ft.IconButton(ft.Icons.ARROW_BACK,
                                      on_click=lambda e: self._f47_back_to_list()),
                        ft.Text("Chi tiết chiến dịch", size=14,
                                weight=ft.FontWeight.BOLD, expand=True),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    bgcolor=BG, padding=ft.padding.symmetric(horizontal=4, vertical=4),
                    border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                ),
                ft.ListView(
                    controls=[ft.Container(
                        content=ft.Column(
                            [header, unit_section, done_section, pending_section]
                            + ([action_btn] if action_btn else []),
                            spacing=0,
                        ),
                        padding=10,
                    )],
                    expand=True,
                ),
            ],
            spacing=0,
            expand=True,
        )

        self.body.content = body_col
        self.refresh()

    # ============================================================
    # ===== MODULE: CTĐ-CTCT (Công tác Đảng - Công tác Chính trị) ===
    # ============================================================

    # Danh sách "ngành / chức vụ" có thể triển khai nhiệm vụ CTĐ-CTCT
    # — (tên đầy đủ, mã viết tắt). 4 chức vụ chỉ huy ở đầu để dễ chọn.
    TASK_DOMAIN_CONFIG = {
        "ctdctct": {
            "title": "CTĐ-CTCT",
            "store_key": "ctdctctTasks",
            "theme_color": ft.Colors.RED_700,
            "theme_icon": ft.Icons.BOOK_ONLINE,
            "nganh": [
                ("Chính uỷ", "CU"), ("Phó Chính uỷ", "PCU"),
                ("Chủ nhiệm Chính trị", "CNCT"), ("Phó Chủ nhiệm Chính trị", "PCNCT"),
                ("Cán bộ", "CB"), ("Tuyên huấn", "TH"), ("Tổ chức", "TC"),
                ("Công tác quần chúng", "QC"), ("Dân vận", "DV"), ("Chính sách", "CS"),
                ("Bảo vệ an ninh", "BV"), ("Uỷ ban kiểm tra", "UBKT"), ("Thống kê", "TK")
            ],
            "lead_codes": {"CU", "PCU", "CNCT", "PCNCT"},
            "lead_keywords": ["chính uỷ", "chủ nhiệm chính trị"],
            "follower_keywords": ["chính uỷ", "phó chính uỷ", "chủ nhiệm chính trị", "phó chủ nhiệm chính trị", "trợ lý"],
            "target_keywords": ["chính uỷ", "chính trị", "tuyên huấn", "cán bộ", "tổ chức", "quần chúng", "dân vận", "an ninh", "kiểm tra"]
        },
        "hcqs": {
            "title": "Hành chính - Quân sự",
            "store_key": "hcqsTasks",
            "theme_color": ft.Colors.INDIGO_700,
            "theme_icon": ft.Icons.SHIELD,
            "nganh": [
                ("Trung đoàn trưởng", "ET"), ("Phó Trung đoàn trưởng", "PET"),
                ("Tham mưu trưởng", "TMT"), ("Phó Tham mưu trưởng", "PTMT"),
                ("Tác huấn", "TH"), ("Quân lực", "QL"), ("Công binh", "CB"),
                ("Pháo binh", "PB"), ("Thông tin", "TT"), ("Phòng không", "PK"),
                ("Trinh sát", "TS"), ("Hoá học", "HH"), ("Hành chính", "HC"),
                ("Bảo mật", "BM"), ("Cơ yếu", "CY"), ("Tài chính", "TC")
            ],
            "lead_codes": {"ET", "PET", "TMT", "PTMT"},
            "lead_keywords": ["trung đoàn trưởng", "tham mưu trưởng", "phó trung đoàn trưởng", "phó tham mưu trưởng"],
            "follower_keywords": ["trung đoàn trưởng", "tham mưu trưởng", "trợ lý", "nhân viên"],
            "target_keywords": ["trung đoàn trưởng", "tham mưu trưởng", "tiểu đoàn trưởng", "đại đội trưởng", "tác huấn", "quân lực", "công binh", "pháo binh", "thông tin", "phòng không", "trinh sát", "hoá học", "hành chính", "bảo mật", "cơ yếu", "tài chính"]
        },
        "pttd": {
            "title": "Phong trào thi đua",
            "store_key": "pttdTasks",
            "theme_color": ft.Colors.ORANGE_700,
            "theme_icon": ft.Icons.FLAG,
            "nganh": [
                ("Thường trực Thi đua", "TT"), ("Khối Cơ quan", "CQ"), ("Khối Tiểu đoàn", "D")
            ],
            "lead_codes": {"TT"},
            "lead_keywords": ["thường trực thi đua"],
            "follower_keywords": ["trợ lý", "chính uỷ", "chủ nhiệm"],
            "target_keywords": ["đại đội trưởng", "chính trị viên", "tiểu đoàn trưởng", "trung đoàn trưởng", "chính uỷ"]
        }
    }

    def _task_lead_members(self, domain: str) -> list[str]:
        """Trả id list các 'đầu mối' nhận nhiệm vụ (đối tượng phân tách theo khối)."""
        out = []
        target_keywords = self.TASK_DOMAIN_CONFIG.get(domain, {}).get("target_keywords", [])
        
        for s in store.get("soldiers", store.seed_soldiers):
            if s.get("isAdmin"):
                continue
            role = (s.get("role") or "").lower()
            if any(k in role for k in target_keywords):
                out.append(s["id"])
        return out

    @classmethod
    def _task_code(cls, domain: str, full_name: str) -> str:
        for n, code in cls.TASK_DOMAIN_CONFIG.get(domain, {}).get('nganh', []):
            if n == full_name:
                return code
        return ""

    def _task_eligible_followers(self, domain: str) -> list[dict]:
        """Trả list quân nhân có thể là 'người theo dõi' khi xếp triển khai.

        Gồm: phó chính uỷ, chủ nhiệm chính trị, phó chủ nhiệm chính trị,
        và toàn bộ trợ lý các ngành (role chứa 'Trợ lý').
        Loại admin.
        """
        out = []
        keys = self.TASK_DOMAIN_CONFIG.get(domain, {}).get("follower_keywords", [])
        for s in store.get("soldiers", store.seed_soldiers):
            if s.get("isAdmin"):
                continue
            role = (s.get("role") or "").lower()
            if any(k in role for k in keys):
                out.append(s)
        return out

    def _task_can_approve(self, domain: str, task: dict, my_uid: str) -> bool:
        """Quyền duyệt báo cáo:
        - admin
        - createdBy (người triển khai)
        - followers (nếu task của chỉ huy)
        - trợ lý ngành tương ứng (vd ngành = Dân vận → trợ lý dân vận)
        """
        if not my_uid:
            return False
        if task.get("createdBy") == my_uid:
            return True
        if my_uid in (task.get("followers") or []):
            return True
        # Admin (qua profile)
        profile = store.get("userProfile", store.seed_user_profile)
        if profile.get("isAdmin"):
            return True
        # Trợ lý ngành: check role của profile có chứa 'trợ lý <ngành>'
        nganh = (task.get("nganh") or "").lower()
        role = (profile.get("role") or "").lower()
        if nganh and "trợ lý" in role and nganh in role:
            return True
        return False

    
    def module_ctdctct(self) -> ft.Control:
        return self._render_task_module("ctdctct")

    def module_hcqs(self) -> ft.Control:
        return self._render_task_module("hcqs")

    def module_pttd(self) -> ft.Control:
        return self._render_task_module("pttd")

    def _render_task_module(self, domain: str) -> ft.Control:
        """Module Task Tracker."""
        tasks = store.get(self.TASK_DOMAIN_CONFIG.get(domain, {}).get('store_key'), lambda: [])
        soldiers = store.get("soldiers", store.seed_soldiers)
        soldier_by_id = {str(s.get("id")): s for s in soldiers if s.get("id")}
        current_view = getattr(self, f"task_{domain}_view", "tasks")

        def cd_str(t) -> str:
            left = int(t.get("deadline", 0)) - store.now_ms()
            if left <= 0:
                return "Hết hạn"
            h, rem = divmod(left, 3600_000)
            m, _ = divmod(rem, 60_000)
            return f"{h:02d}h {m:02d}m"

        def card(t: dict) -> ft.Container:
            done = len(t.get("submissions", {}) or {})
            total = len(t.get("members", []) or [])
            pct = int(done / total * 100) if total else 0
            code = t.get("nganhCode") or self._task_code(domain, t.get("nganh") or "")
            full_title = f"[{code}] {t.get('title','')}"
            return ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(full_title, size=14, weight=ft.FontWeight.BOLD,
                                expand=True),
                        ft.Container(
                            content=ft.Text("🔴 Live" if t.get("status") == "live"
                                            else "Done",
                                            size=10, color=ft.Colors.WHITE,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=RED if t.get("status") == "live" else "#999",
                            border_radius=10,
                            padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        ),
                        *([ft.IconButton(
                            ft.Icons.MORE_VERT, icon_size=18,
                            tooltip="Tuỳ chọn",
                            on_click=lambda e, _t=t: self.task_open_task_menu(domain, _t),
                        )] if self._is_admin() else []),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Text(f"Người triển khai: đ.c {t.get('creator','')} – "
                            f"{t.get('creatorRole','')}", size=11, color=TEXT_MUTED),
                    ft.Text(t.get("desc", ""), size=12, color=TEXT_MUTED),
                    ft.Container(
                        content=ft.Row([
                            ft.Text("⏱", size=18),
                            ft.Column([
                                ft.Text("Thời gian còn lại", size=11, color="#633806"),
                                ft.Text(cd_str(t), size=16, color="#ba7517",
                                        weight=ft.FontWeight.BOLD),
                            ], spacing=0, tight=True),
                        ], spacing=10),
                        bgcolor="#fff8e1", border_radius=8, padding=10,
                        border=ft.border.all(1, "#f0c040"),
                    ),
                    ft.Row([
                        ft.Text("Tiến độ", size=11, color=TEXT_MUTED),
                        ft.Text(f"{done}/{total} ({pct}%)", size=11,
                                weight=ft.FontWeight.BOLD,
                                color=GREEN_MID if pct >= 50 else RED),
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.ProgressBar(value=(pct/100) if total else 0,
                                   color=GREEN_MID, bgcolor="#eee", height=8),
                ], spacing=8),
                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
                padding=14, margin=ft.margin.only(bottom=10),
                on_click=lambda e, _t=t: self.task_open_task_detail(domain, _t),
                ink=True,
            )

        # ---- Body theo tab ----
        if current_view == "ranking":
            # Bảng xếp hạng tương tự F47 (riêng cho CTĐ-CTCT)
            scores: dict[str, dict] = {}
            for t in tasks:
                subs = t.get("submissions") or {}
                if isinstance(subs, dict):
                    for uid, sub in subs.items():
                        if not uid:
                            continue
                        it = scores.setdefault(str(uid), {
                            "count": 0, "lastAt": 0, "name": ""
                        })
                        it["count"] += 1
                        try:
                            it["lastAt"] = max(it["lastAt"],
                                               int((sub or {}).get("at", 0) or 0))
                        except Exception:
                            pass
            ranked = sorted(scores.items(),
                            key=lambda kv: (kv[1]["count"], kv[1]["lastAt"]),
                            reverse=True)
            rows = []
            for i, (uid, info) in enumerate(ranked[:50]):
                s = soldier_by_id.get(uid) or {}
                display = f"{(s.get('rank') or '').strip()} {(s.get('name') or '').strip()}".strip() or uid
                medal = "🥇" if i == 0 else ("🥈" if i == 1 else ("🥉" if i == 2 else f"#{i+1}"))
                rows.append(ft.Container(
                    content=ft.Row([
                        ft.Text(medal, size=14, width=40),
                        ft.Column([
                            ft.Text(display, size=13, weight=ft.FontWeight.W_700),
                            ft.Text(f"{info['count']} báo cáo • Gần nhất {fmt_dt(info['lastAt'])}",
                                    size=10, color=TEXT_MUTED),
                        ], spacing=2, expand=True, tight=True),
                        ft.Container(
                            content=ft.Text(str(info["count"]), size=12,
                                            color=ft.Colors.WHITE,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=GREEN_MID, border_radius=12,
                            padding=ft.padding.symmetric(horizontal=10, vertical=6),
                        ),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    bgcolor=BG, border=ft.border.all(1, BORDER),
                    border_radius=12, padding=12,
                    margin=ft.margin.only(bottom=8),
                ))
            main_body = ft.Container(
                content=ft.Column(
                    controls=rows if rows else [
                        ft.Container(height=60),
                        ft.Text("Chưa có báo cáo nào",
                                 color=TEXT_MUTED, size=13,
                                 text_align=ft.TextAlign.CENTER),
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    spacing=0,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                expand=True,
                padding=ft.padding.all(10),
            )
        else:
            # Filter theo ngành — icon đã ở tabs_bar (hourglass), chỉ hiện dropdown khi mở
            ng_filter = getattr(self, f"task_{domain}_filter", "")
            filter_open = getattr(self, f"task_{domain}_filter_open", False)

            def on_filter_change(e):
                setattr(self, f"task_{domain}_filter", e.control.value or "")
                self.body.content = self._render_task_module(domain)
                self.refresh()

            filter_opts = [ft.dropdown.Option("", "🔎 Tất cả ngành")]
            filter_opts.extend(
                ft.dropdown.Option(name, f"[{code}] {name}")
                for name, code in self.TASK_DOMAIN_CONFIG.get(domain, {}).get('nganh', [])
            )

            if filter_open:
                filter_bar = ft.Container(
                    content=ft.Dropdown(
                        label="Chọn ngành / chức vụ",
                        value=ng_filter,
                        options=filter_opts,
                        on_change=on_filter_change,
                        border_radius=8, dense=True,
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=6),
                    bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                )
            else:
                filter_bar = ft.Container(visible=False)

            # Áp dụng filter
            if ng_filter:
                tasks_view = [t for t in tasks if t.get("nganh") == ng_filter]
                empty_msg = f"📭 Chưa có nhiệm vụ ngành '{ng_filter}'"
            else:
                tasks_view = tasks
                empty_msg = "📭 Chưa có nhiệm vụ nào"

            if tasks_view:
                task_list = ft.Container(
                    content=ft.Column(
                        controls=[card(t) for t in tasks_view],
                        scroll=ft.ScrollMode.AUTO,
                        spacing=0,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    ),
                    expand=True,
                    padding=ft.padding.all(10),
                )
            else:
                task_list = ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Container(height=80),
                            ft.Text(empty_msg, text_align=ft.TextAlign.CENTER,
                                    color=TEXT_MUTED, size=14),
                            ft.Container(height=10),
                            ft.Text("Bấm nút  +  ở góc phải để triển khai",
                                    text_align=ft.TextAlign.CENTER,
                                    color=TEXT_MUTED, size=12),
                        ],
                        scroll=ft.ScrollMode.AUTO,
                        spacing=0,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    expand=True,
                    padding=ft.padding.all(10),
                )

            main_body = ft.Column([filter_bar, task_list], spacing=0, expand=True)

        # ---- Tab bar (Nhiệm vụ / Xếp hạng) ----
        tab_defs = [("Nhiệm vụ", "tasks"), ("Xếp hạng", "ranking")]
        sel = next((i for i, (_, k) in enumerate(tab_defs) if k == current_view), 0)

        def on_tab_changed(e):
            try:
                idx = e.control.selected_index
            except Exception:
                idx = getattr(e, "selected_index", None)
            if idx is None:
                return
            if 0 <= idx < len(tab_defs):
                setattr(self, f"task_{domain}_view", tab_defs[idx][1])
                self.body.content = self._render_task_module(domain)
                self.refresh()

        # Top bar gộp: back arrow + title + tabs + (icon filter ngành nếu ở tab Nhiệm vụ)
        ng_filter_now = getattr(self, f"task_{domain}_filter", "")
        filter_open_now = getattr(self, f"task_{domain}_filter_open", False)

        def _toggle_filter_top(e=None):
            setattr(self, f"task_{domain}_filter_open", not getattr(self, f"task_{domain}_filter_open", False))
            self.body.content = self._render_task_module(domain)
            self.refresh()

        # Back arrow đã chuyển lên top header — tabs_bar chỉ giữ tabs + filter icon
        top_row_children = [
            ft.Container(
                content=ft.Tabs(
                    selected_index=sel,
                    on_change=on_tab_changed,
                    tabs=[ft.Tab(text=label) for label, _ in tab_defs],
                    height=46,
                ),
                expand=True,
                height=46,
            ),
        ]
        # Chỉ tab Nhiệm vụ mới hiện icon lọc ngành
        if current_view == "tasks":
            top_row_children.append(
                ft.IconButton(
                    ft.Icons.HOURGLASS_BOTTOM if filter_open_now
                    else ft.Icons.HOURGLASS_TOP,
                    icon_size=20,
                    icon_color=GREEN_DARK if ng_filter_now else TEXT_MUTED,
                    tooltip="Lọc theo ngành",
                    on_click=_toggle_filter_top,
                )
            )

        tabs_bar = ft.Container(
            content=ft.Row(top_row_children,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
            bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            padding=ft.padding.only(left=2, right=4, top=2, bottom=2),
        )

        # Bỏ module_back_bar — back arrow đã gộp vào tabs_bar
        body_col = ft.Column([
            tabs_bar,
            main_body,
        ], spacing=0, expand=True)

        # FAB triển khai (chỉ tab tasks và admin/chỉ huy mới có)
        profile = store.get("userProfile", store.seed_user_profile)
        my_admin_level = int(profile.get("adminLevel") or 1)
        can_create = self._is_admin() or my_admin_level >= 2

        if current_view == "tasks" and can_create:
            fab = ft.Container(
                content=ft.FloatingActionButton(
                    icon=ft.Icons.ADD,
                    bgcolor=self.TASK_DOMAIN_CONFIG.get(domain, {}).get("theme_color", GREEN_DARK), 
                    foreground_color=ft.Colors.WHITE,
                    tooltip=f"Triển khai nhiệm vụ {self.TASK_DOMAIN_CONFIG.get(domain, {}).get('title')}",
                    on_click=lambda e: self.task_open_create(domain),
                ),
                right=16, bottom=16,
            )
            return ft.Stack([body_col, fab], expand=True)
        return body_col

    def task_open_create(self, domain: str, existing: dict | None = None) -> None:
        """Dialog triển khai nhiệm vụ."""
        page = self.page
        is_edit = existing is not None
        ex = existing or {}

        if is_edit:
            try:
                left_h = max(1, (int(ex.get("deadline", 0)) - store.now_ms()) // 3600_000)
            except Exception:
                left_h = 24
            default_hours = str(left_h)
        else:
            default_hours = "24"

        title_input = ft.TextField(label="Tên nhiệm vụ *",
                                   value=ex.get("title", ""),
                                   border_radius=8, dense=True)
        desc_input = ft.TextField(label="Mô tả nội dung *",
                                  value=ex.get("desc", ""),
                                  border_radius=8, dense=True,
                                  multiline=True, min_lines=2, max_lines=4)

        nganh_dd = ft.Dropdown(
            label="Ngành *",
            value=ex.get("nganh") or "Dân vận",
            options=[ft.dropdown.Option(name, f"[{code}] {name}")
                     for name, code in self.TASK_DOMAIN_CONFIG.get(domain, {}).get('nganh', [])],
            border_radius=8, dense=True,
        )

        hours_dropdown = ft.Dropdown(
            label="Thời hạn *",
            value=default_hours,
            options=[ft.dropdown.Option(key, text) for key, text in self.F47_HOUR_OPTIONS],
            border_radius=8, dense=True,
        )

        # Phạm vi: multi-select checkbox (Toàn Trung đoàn auto-tick tất cả)
        scope_ui, get_scope = self._build_scope_picker(
            ex.get("scopeUnits") or ex.get("scope")
        )

        # ---- Loại nhiệm vụ ----
        task_type_dd = ft.Dropdown(
            label="Loại nhiệm vụ *",
            value=ex.get("taskType") or "notify",
            options=[
                ft.dropdown.Option("notify", "📄 Gửi văn bản (chỉ cần Đã nhận)"),
                ft.dropdown.Option("report", "📋 Yêu cầu báo cáo (đơn vị phải báo cáo)"),
            ],
            border_radius=8, dense=True,
        )

        # ---- Đính kèm ảnh văn bản ----
        ex_attachments = list(ex.get("attachments") or [])
        attached_label = ft.Text(
            f"📎 {len(ex_attachments)} ảnh đính kèm" if ex_attachments
            else "Chưa đính kèm",
            size=11, color=TEXT_MUTED,
        )

        def on_files_picked(e: ft.FilePickerResultEvent):
            if not e.files:
                return
            if not store.STORE.is_bound() or not AUTH_STATE.get("idToken"):
                attached_label.value = "❌ Phải đăng nhập để upload"
                page.update(); return
            attached_label.value = f"⏳ Upload {len(e.files)} ảnh..."
            page.update()

            def worker():
                ok = 0
                for f in e.files:
                    try:
                        remote = fb_storage.make_remote_path(
                            f"ctdctct/attach/{AUTH_STATE['uid']}", f.name
                        )
                        if f.path:
                            res = fb_storage.upload_file(f.path, remote, AUTH_STATE["idToken"])
                        elif f.bytes:
                            res = fb_storage.upload_data(remote, f.bytes, AUTH_STATE["idToken"], f.name)
                        else:
                            continue
                        ex_attachments.append(res["downloadURL"])
                        ok += 1
                    except Exception as ex_e:
                        attached_label.value = f"❌ {ex_e}"
                attached_label.value = (
                    f"📎 {len(ex_attachments)} ảnh đính kèm "
                    f"(+{ok} mới upload)"
                )
                try:
                    page.update()
                except Exception:
                    pass

            threading.Thread(target=worker, daemon=True).start()

        picker = ft.FilePicker(on_result=on_files_picked)
        if picker not in page.overlay:
            page.overlay.append(picker)

        attach_section = ft.Container(
            content=ft.Column([
                ft.Text("Đính kèm văn bản (ảnh chụp)", size=12, color=TEXT_MUTED,
                        weight=ft.FontWeight.W_500),
                ft.Row([
                    ft.ElevatedButton(
                        "📷 Chọn ảnh",
                        on_click=lambda e: picker.pick_files(
                            allow_multiple=True,
                            file_type=ft.FilePickerFileType.IMAGE,
                        ),
                    ),
                    attached_label,
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ], spacing=4, tight=True),
        )

        # ---- Người theo dõi: CHỈ hiện khi ngành thuộc nhóm chỉ huy ----
        eligible_followers = self._task_eligible_followers(domain)
        existing_followers = set(ex.get("followers") or [])
        follower_checkboxes = [
            ft.Checkbox(
                label=f"{(s.get('rank') or '').strip()} {(s.get('name') or '').strip()} "
                      f"• {s.get('role','')}".strip(),
                value=(s.get("id") in existing_followers),
                data=s.get("id"),
            )
            for s in eligible_followers
        ]

        # Bắt đầu ẩn nếu chưa phải leadership
        def _is_leadership(nganh_name: str) -> bool:
            return self._task_code(domain, nganh_name) in self.TASK_DOMAIN_CONFIG.get(domain, {}).get('lead_codes', set())

        followers_section = ft.Container(
            content=ft.Column([
                ft.Text("Người theo dõi (chọn để gửi thông báo & duyệt báo cáo)",
                        size=12, color=TEXT_MUTED, weight=ft.FontWeight.W_500),
                ft.Container(
                    content=ft.Column(
                        follower_checkboxes if follower_checkboxes
                        else [ft.Text("Chưa có cán bộ phó / trợ lý nào trong hệ thống.",
                                      size=11, color=TEXT_MUTED)],
                        spacing=2, tight=True, scroll=ft.ScrollMode.AUTO,
                    ),
                    bgcolor=BG2, border_radius=8, padding=8,
                    border=ft.border.all(1, BORDER),
                    height=180,
                ),
            ], spacing=4, tight=True),
            visible=_is_leadership(nganh_dd.value or ""),
        )

        # Khi đổi ngành, toggle hiển thị followers section
        def on_nganh_change(e):
            followers_section.visible = _is_leadership(nganh_dd.value or "")
            page.update()
        nganh_dd.on_change = on_nganh_change

        err_text = ft.Text("", color=RED, size=12)

        def submit(e):
            title = (title_input.value or "").strip()
            desc = (desc_input.value or "").strip()
            nganh = nganh_dd.value or ""
            try:
                hours = int(hours_dropdown.value or "24")
            except (ValueError, TypeError):
                hours = 24
            if not (title and desc and nganh):
                err_text.value = "⚠️ Tên, Mô tả, Ngành là bắt buộc"
                page.update(); return
            now = store.now_ms()
            profile = store.get("userProfile", store.seed_user_profile)
            # Lấy danh sách 'đầu mối' theo khối
            members = self._task_lead_members(domain)
            code = self._task_code(domain, nganh)
            tasks = store.get(self.TASK_DOMAIN_CONFIG.get(domain, {}).get('store_key'), lambda: [])
            task_type = task_type_dd.value or "notify"

            # Followers (chỉ áp dụng khi leadership)
            followers = []
            if _is_leadership(nganh):
                followers = [cb.data for cb in follower_checkboxes if cb.value]

            if is_edit:
                for t in tasks:
                    if t.get("id") == ex.get("id"):
                        t["title"] = title
                        t["desc"] = desc
                        t["nganh"] = nganh
                        t["nganhCode"] = code
                        t["deadline"] = now + hours * 3600_000
                        t["scopeUnits"] = get_scope()
                        t["scope"] = ("Toàn Trung đoàn"
                                      if len(get_scope()) == len(self._scope_unit_list())
                                      else (", ".join(get_scope()) or "Toàn Trung đoàn"))
                        t["followers"] = followers
                        t["taskType"] = task_type
                        t["attachments"] = list(ex_attachments)
                        em = t.get("members") or []
                        t["members"] = list({*em, *members})
                        break
                store.set_value("ctdctctTasks", tasks)
                store.log_activity(f"Sửa CTĐ-CTCT: [{code}] {title}")
                self.toast(f"✏️ Đã cập nhật: [{code}] {title}")
            else:
                new_task = {
                    "id": f"ct_{now}",
                    "title": title,
                    "desc": desc,
                    "nganh": nganh,
                    "nganhCode": code,
                    "taskType": task_type,
                    "creator": profile.get("name") or AUTH_STATE.get("username", ""),
                    "creatorRole": profile.get("role", ""),
                    "createdBy": AUTH_STATE.get("uid", ""),
                    "followers": followers,
                    "attachments": list(ex_attachments),
                    "deadline": now + hours * 3600_000,
                    "createdAt": now,
                    "scopeUnits": get_scope(),
                    "scope": "Toàn Trung đoàn" if len(get_scope()) == len(self._scope_unit_list())
                             else (", ".join(get_scope()) or "Toàn Trung đoàn"),
                    "members": members,
                    "submissions": {},
                    "receipts": {},
                    "status": "live",
                }
                tasks.insert(0, new_task)
                store.set_value("ctdctctTasks", tasks)
                store.log_activity(f"Triển khai CTĐ-CTCT: [{code}] {title}")
                # Notif: nếu có followers chỉ gửi targeted; nếu không gửi broadcast
                _creator_name = profile.get("name") or profile.get("username") or "Quản trị"
                if followers:
                    _link_f = f"ctdctct:{new_task['id']}"
                    for fuid in followers:
                        store.push_notif("ctdctct", f"🎖 [{code}] Bạn là người theo dõi",
                                         f"{title} • Hạn {hours}h", _link_f,
                                         target_uid=fuid, sender_name=_creator_name)
                else:
                    # Targeted: chỉ members + người triển khai (không broadcast)
                    _link = f"ctdctct:{new_task['id']}"
                    for muid in members:
                        if muid:
                            store.push_notif(
                                "ctdctct", f"🎖 [{code}] Nhiệm vụ mới cho bạn",
                                f"{title} • Hạn {hours}h", _link,
                                target_uid=muid, sender_name=_creator_name,
                            )
                self.toast(f"Đã triển khai: [{code}] {title}")
            try:
                _dlg.open = False
            except Exception:
                pass
            self.body.content = self._render_task_module(domain)
            self.refresh()

        domain_title = self.TASK_DOMAIN_CONFIG.get(domain, {}).get("title", "CTĐ-CTCT")
        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                f"✏️ Sửa nhiệm vụ {domain_title}" if is_edit
                else f"🎖 Triển khai nhiệm vụ {domain_title}",
                size=15, weight=ft.FontWeight.BOLD,
            ),
            content=ft.Container(
                content=ft.Column(
                    [nganh_dd, title_input, desc_input,
                     task_type_dd,
                     hours_dropdown, scope_ui,
                     attach_section,
                     followers_section, err_text],
                    spacing=10, tight=True, scroll=ft.ScrollMode.AUTO,
                ),
                width=380, height=620,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton(
                    "Lưu thay đổi" if is_edit else "Triển khai",
                    on_click=submit,
                    bgcolor=GREEN_MID if is_edit else GREEN_DARK,
                    color=ft.Colors.WHITE,
                ),
            ],
        )
        _show_dialog(self.page, _dlg)

    def task_open_task_menu(self, domain: str, task: dict) -> None:
        """Popup ✏️ Sửa / 🗑 Xoá cho 1 nhiệm vụ (admin)."""
        page = self.page

        def close_dlg():
            try:
                _dlg.open = False
            except Exception:
                pass
            page.update()

        def do_edit(e):
            close_dlg(); self.task_open_create(domain, existing=task)

        def do_delete(e):
            close_dlg(); self.task_confirm_delete(domain, task)

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Tuỳ chọn — {task.get('title','')[:50]}",
                          size=14, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column([
                    ft.TextButton("✏️  Sửa nhiệm vụ", on_click=do_edit,
                                  style=ft.ButtonStyle(padding=12)),
                    ft.TextButton("🗑  Xoá nhiệm vụ", on_click=do_delete,
                                  style=ft.ButtonStyle(padding=12, color=RED)),
                ], spacing=4, tight=True),
                width=320,
            ),
            actions=[ft.TextButton("Đóng", on_click=lambda e: close_dlg())],
        )
        _show_dialog(self.page, _dlg)

    def task_confirm_delete(self, domain: str, task: dict) -> None:
        page = self.page
        tid = task.get("id", "")
        title = task.get("title", "")

        def do(e):
            try:
                _dlg.open = False
            except Exception:
                pass
            store_key = self.TASK_DOMAIN_CONFIG.get(domain, {}).get('store_key', 'ctdctctTasks')
            tasks = store.get(store_key, lambda: [])
            tasks = [t for t in tasks if t.get("id") != tid]
            store.set_value(store_key, tasks)
            self.toast(f"🗑 Đã xoá: {title[:40]}")
            self.body.content = self._render_task_module(domain)
            self.refresh()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Xác nhận xoá", weight=ft.FontWeight.BOLD),
            content=ft.Text(f"Xoá nhiệm vụ '{title}'?\nBáo cáo đã nộp sẽ mất.", size=12),
            actions=[
                ft.TextButton("Huỷ",
                              on_click=lambda e: _close_dialog(self.page)),
                ft.ElevatedButton("Xoá", on_click=do,
                                  bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def task_open_task_detail(self, domain: str, task: dict) -> None:
        """Trang chi tiết nhiệm vụ — tỷ lệ đơn vị + người làm/chưa làm + giục."""
        # Re-use logic của f47 detail nhưng cho ctdctct
        tid = task.get("id", "")
        tasks = store.get(self.TASK_DOMAIN_CONFIG.get(domain, {}).get('store_key'), lambda: [])
        cur = next((t for t in tasks if t.get("id") == tid), task)

        soldiers = store.get("soldiers", store.seed_soldiers)
        soldier_by_id = {str(s.get("id")): s for s in soldiers if s.get("id")}
        units_tree = store.get("units", store.seed_units)
        unit_name_map = self._unit_name_map(units_tree)

        members = cur.get("members") or []
        subs = cur.get("submissions") or {}
        deadline = int(cur.get("deadline", 0) or 0)
        created = int(cur.get("createdAt", 0) or 0)
        now = store.now_ms()
        code = cur.get("nganhCode") or self._task_code(domain, cur.get("nganh") or "")
        full_title = f"[{code}] {cur.get('title','')}"

        # Chia 3 nhóm: approved (đã duyệt) / pending (chờ duyệt) / not_done (chưa báo cáo)
        done_list, await_list, pending_list = [], [], []
        for uid in members:
            sub = subs.get(uid)
            s = soldier_by_id.get(str(uid)) or {}
            if sub:
                at = int((sub or {}).get("at", 0) or 0)
                if deadline > created and at > 0:
                    used = (at - created) / max(1, deadline - created)
                else:
                    used = 0.5
                speed = "fast" if used < 0.33 else ("normal" if used < 0.75 else "slow")
                status = (sub or {}).get("status") or "approved"  # legacy cũ coi như approved
                row = {"uid": uid, "soldier": s, "sub": sub, "at": at, "speed": speed}
                if status == "approved":
                    done_list.append(row)
                elif status == "rejected":
                    pending_list.append({"uid": uid, "soldier": s,
                                         "rejected": True, "sub": sub})
                else:
                    await_list.append(row)
            else:
                pending_list.append({"uid": uid, "soldier": s})

        done_list.sort(key=lambda x: x["at"])
        await_list.sort(key=lambda x: x["at"])
        pending_list.sort(key=lambda x: (x["soldier"].get("name") or "").lower())

        unit_stats: dict[str, dict] = {}
        for uid in members:
            s = soldier_by_id.get(str(uid)) or {}
            unit_id = s.get("unitId") or "unknown"
            it = unit_stats.setdefault(unit_id, {
                "name": unit_name_map.get(unit_id, "Khác"),
                "total": 0, "done": 0,
            })
            it["total"] += 1
            sub = subs.get(uid)
            if sub and (sub.get("status") or "approved") == "approved":
                it["done"] += 1
        unit_rows = sorted(unit_stats.values(),
                           key=lambda u: (-u["done"]/max(1, u["total"]), u["name"]))

        # === RBAC Lọc hiển thị ===
        profile = store.get("userProfile", store.seed_user_profile)
        my_unit_id = profile.get("unitId") or ""
        my_admin_level = int(profile.get("adminLevel") or 1)
        is_admin = self._is_admin()

        campaign_done_n = len(done_list)
        campaign_await_n = len(await_list)
        campaign_total_n = len(members)

        if not is_admin:
            if my_admin_level >= 2:
                done_list = [x for x in done_list if x["soldier"].get("unitId") == my_unit_id]
                await_list = [x for x in await_list if x["soldier"].get("unitId") == my_unit_id]
                pending_list = [x for x in pending_list if x["soldier"].get("unitId") == my_unit_id]
                unit_rows = [u for u in unit_rows if str(u.get("name")) == str(unit_name_map.get(my_unit_id, "Khác"))]
            else:
                my_uid_filter = AUTH_STATE.get("uid", "")
                done_list = [x for x in done_list if str(x["uid"]) == str(my_uid_filter)]
                await_list = [x for x in await_list if str(x["uid"]) == str(my_uid_filter)]
                pending_list = [x for x in pending_list if str(x["uid"]) == str(my_uid_filter)]
                unit_rows = []

        def soldier_label(s, uid):
            name_part = f"{(s.get('rank') or '').strip()} {(s.get('name') or '').strip()}".strip()
            if name_part:
                return f"đ.c {name_part}"
            return s.get("username") or uid

        deadline_left = deadline - now
        if deadline_left <= 0:
            cd_str = "⚠️ Hết hạn"; cd_color = RED
        else:
            h, rem = divmod(deadline_left, 3600_000)
            m, _ = divmod(rem, 60_000)
            cd_str = f"⏱ Còn {h:02d}h {m:02d}m"; cd_color = AMBER

        total_n = campaign_total_n
        done_n = campaign_done_n
        await_n = campaign_await_n
        my_uid = AUTH_STATE.get("uid", "")
        can_approve = self._task_can_approve(domain, cur, my_uid)
        _task_type_now = cur.get("taskType") or "report"
        if _task_type_now == "notify":
            # Tiến độ = số người đã nhận
            receipts_now = cur.get("receipts") or {}
            received_n = sum(1 for u in members if u in receipts_now)
            done_n_display = received_n
            pct = int(received_n / total_n * 100) if total_n else 0
            progress_label = "Đã nhận"
        else:
            done_n_display = done_n
            pct = int(done_n / total_n * 100) if total_n else 0
            progress_label = "Đã duyệt"

        header = ft.Container(
            content=ft.Column([
                ft.Text(full_title, size=15, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Text(cur.get("desc",""), size=12, color=TEXT_MUTED),
                ft.Container(height=4),
                ft.Row([ft.Text("📅 Triển khai:", size=11, color=TEXT_MUTED, width=120),
                        ft.Text(fmt_dt(created), size=11)]),
                ft.Row([ft.Text("👤 Người triển khai:", size=11, color=TEXT_MUTED, width=120),
                        ft.Text(f"đ.c {cur.get('creator','')} – {cur.get('creatorRole','')}",
                                size=11, expand=True)]),
                ft.Row([ft.Text("🏷 Ngành:", size=11, color=TEXT_MUTED, width=120),
                        ft.Text(f"[{code}] {cur.get('nganh','')}", size=11)]),
                ft.Row([ft.Text("🎯 Phạm vi:", size=11, color=TEXT_MUTED, width=120),
                        ft.Text(cur.get("scope",""), size=11)]),
                *(
                    [
                        ft.Container(height=4),
                        ft.Text("📸 Ảnh/Video Mẫu đính kèm:", size=11, color=TEXT_MUTED),
                        ft.Container(
                            content=ft.Image(src=cur["attachments"][0], border_radius=8,
                                             width=300, fit=ft.ImageFit.CONTAIN),
                            on_click=lambda e, u=cur["attachments"][0]: open_image_viewer(self.page, u),
                            ink=True, border_radius=8,
                            tooltip="Bấm để xem ảnh",
                        )
                    ] if cur.get("attachments") and len(cur.get("attachments")) > 0 else []
                ),
                ft.Container(
                    content=ft.Text(cd_str, size=14, weight=ft.FontWeight.BOLD,
                                    color=cd_color),
                    bgcolor="#fff8e1", border_radius=8, padding=10,
                    border=ft.border.all(1, "#f0c040"),
                    margin=ft.margin.only(top=6),
                ),
                ft.Row([
                    ft.Text(f"Tiến độ ({progress_label})",
                            size=12, color=TEXT_MUTED, expand=True),
                    ft.Text(f"{done_n_display}/{total_n} ({pct}%)", size=12,
                            weight=ft.FontWeight.BOLD,
                            color=GREEN_MID if pct >= 50 else RED),
                ]),
                ft.ProgressBar(value=pct/100 if total_n else 0,
                               color=GREEN_MID, bgcolor="#eee", height=10),
            ], spacing=4),
            bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
            padding=14, margin=ft.margin.only(bottom=10),
        )

        # Tỷ lệ đơn vị
        unit_items = []
        for idx, u in enumerate(unit_rows):
            up = int(u["done"]/max(1, u["total"]) * 100)
            color = GREEN_MID if up == 100 else (AMBER if up >= 50 else RED)
            medal = "🏆" if idx == 0 and up > 0 else ("🥈" if idx == 1 and up > 0 else ("🥉" if idx == 2 and up > 0 else ""))
            unit_items.append(ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(f"{medal} {u['name']}".strip(), size=12, weight=ft.FontWeight.W_600,
                                expand=True),
                        ft.Text(f"{u['done']}/{u['total']} ({up}%)",
                                size=11, color=color, weight=ft.FontWeight.BOLD),
                    ]),
                    ft.ProgressBar(value=up/100, color=color, bgcolor="#eee", height=6),
                ], spacing=4),
                padding=ft.padding.symmetric(vertical=6),
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            ))
        unit_section = ft.Container(
            content=ft.Column(
                [ft.Text("🏆 Bảng Xếp Hạng Đơn vị", size=13, weight=ft.FontWeight.BOLD),
                 ft.Container(height=4),
                 *(unit_items if unit_items
                   else [ft.Text("Chưa có đơn vị/quân nhân nào.",
                                 size=11, color=TEXT_MUTED)])],
                spacing=0,
            ),
            bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
            padding=14, margin=ft.margin.only(bottom=10),
        )

        speed_badge = {
            "fast": ("⚡ Nhanh", GREEN_MID),
            "normal": ("🆗 Đúng giờ", AMBER),
            "slow": ("🐢 Chậm", RED),
        }

        def done_row(item):
            s = item["soldier"]
            badge_text, badge_color = speed_badge[item["speed"]]
            sub = item["sub"] or {}
            note = sub.get("note") or ""
            return ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Container(
                            content=ft.Text(initials(s.get("name") or item["uid"], 2),
                                            color=ft.Colors.WHITE, size=10,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=GREEN_DARK, width=28, height=28,
                            border_radius=14, alignment=ft.alignment.center,
                        ),
                        ft.Column([
                            ft.Text(soldier_label(s, item["uid"]),
                                    size=12, weight=ft.FontWeight.W_700),
                            ft.Text(f"Báo cáo lúc {fmt_dt(item['at'])}",
                                    size=10, color=TEXT_MUTED),
                        ], spacing=1, expand=True, tight=True),
                        ft.Container(
                            content=ft.Text(badge_text, size=10,
                                            color=ft.Colors.WHITE,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=badge_color, border_radius=10,
                            padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        ),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                    *([ft.Text(note, size=11, color=TEXT_MUTED,
                              max_lines=2)] if note else []),
                ], spacing=4),
                padding=10,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )

        done_section = ft.Container(
            content=ft.Column([
                ft.Text(f"🥇 Bảng Xếp Hạng Cá Nhân ({len(done_list)})", size=13,
                        weight=ft.FontWeight.BOLD, color=GREEN_DARK),
                *([done_row(it) for it in done_list] if done_list
                  else [ft.Container(
                      content=ft.Text("Chưa có báo cáo nào được duyệt.",
                                      size=11, color=TEXT_MUTED),
                      padding=10)]),
            ], spacing=0),
            bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
            margin=ft.margin.only(bottom=10),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        # ---- Section: Chờ duyệt ----
        def _approve(uid: str, approve: bool):
            tasks_now = store.get(self.TASK_DOMAIN_CONFIG.get(domain, {}).get('store_key'), lambda: [])
            for t in tasks_now:
                if t.get("id") == tid:
                    sub = (t.get("submissions") or {}).get(uid)
                    if not sub:
                        return
                    sub["status"] = "approved" if approve else "rejected"
                    sub["approvedBy"] = my_uid
                    sub["approvedAt"] = store.now_ms()
                    break
            store.set_value("ctdctctTasks", tasks_now)
            # Notif tới người báo cáo
            s_name = (soldier_by_id.get(uid) or {}).get("name") or uid
            _approver_name = profile.get("name") or profile.get("username") or "Chỉ huy"
            if approve:
                store.push_notif("ctdctct", "✅ Báo cáo đã được duyệt",
                                 f"Bạn được duyệt: {full_title[:60]}",
                                 f"ctdctct:{tid}", target_uid=uid,
                                 sender_name=_approver_name)
                self.toast(f"✅ Duyệt báo cáo của {s_name}")
            else:
                store.push_notif("ctdctct", "❌ Báo cáo bị từ chối",
                                 f"Cần làm lại: {full_title[:60]}",
                                 f"ctdctct:{tid}", target_uid=uid,
                                 sender_name=_approver_name)
                self.toast(f"❌ Từ chối báo cáo của {s_name}")
            self.task_open_task_detail(domain, cur)

        def await_row(item):
            s = item["soldier"]
            uid = item["uid"]
            sub = item["sub"] or {}
            note = sub.get("note") or ""
            links = sub.get("links") or []
            actions = []
            if can_approve:
                actions = [
                    ft.IconButton(
                        ft.Icons.CHECK_CIRCLE, icon_color=GREEN_MID, icon_size=22,
                        tooltip="Duyệt",
                        on_click=lambda e, _u=uid: _approve(_u, True),
                    ),
                    ft.IconButton(
                        ft.Icons.CANCEL, icon_color=RED, icon_size=22,
                        tooltip="Từ chối",
                        on_click=lambda e, _u=uid: _approve(_u, False),
                    ),
                ]
            ctrls = [
                ft.Row([
                    ft.Container(
                        content=ft.Text(initials(s.get("name") or uid, 2),
                                        color=ft.Colors.WHITE, size=10,
                                        weight=ft.FontWeight.BOLD),
                        bgcolor=AMBER, width=28, height=28,
                        border_radius=14, alignment=ft.alignment.center,
                    ),
                    ft.Column([
                        ft.Text(soldier_label(s, uid),
                                size=12, weight=ft.FontWeight.W_700),
                        ft.Text(f"Báo cáo lúc {fmt_dt(item['at'])}",
                                size=10, color=TEXT_MUTED),
                    ], spacing=1, expand=True, tight=True),
                    *actions,
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
            ]
            if note:
                ctrls.append(ft.Text(note, size=11, color=TEXT_MUTED))
            for url in links[:2]:
                ctrls.append(ft.Text(f"🔗 {url}", size=10, color=BLUE,
                                     overflow=ft.TextOverflow.ELLIPSIS))
            return ft.Container(
                content=ft.Column(ctrls, spacing=4),
                padding=10,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )

        await_section = ft.Container(
            content=ft.Column([
                ft.Text(f"⏳ Danh sách Chờ duyệt ({len(await_list)})", size=13,
                        weight=ft.FontWeight.BOLD, color=AMBER),
                *([await_row(it) for it in await_list] if await_list
                  else [ft.Container(
                      content=ft.Text("Không có báo cáo nào đang chờ.",
                                      size=11, color=TEXT_MUTED),
                      padding=10)]),
            ], spacing=0),
            bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
            margin=ft.margin.only(bottom=10),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        # Giục
        reminders_log = store.get("ctdctctReminders", lambda: {})

        def remind_one(uid, name, refresh=True):
            reminders = store.get("ctdctctReminders", lambda: {})
            reminders[f"{tid}::{uid}"] = {
                "by": AUTH_STATE.get("uid", ""),
                "byName": store.get("userProfile",
                                    store.seed_user_profile).get("name", ""),
                "at": store.now_ms(),
            }
            store.set_value("ctdctctReminders", reminders)
            _ctd_reminder_by = store.get("userProfile", store.seed_user_profile).get("name") or "Chỉ huy"
            store.push_notif("ctdctct", "📢 Bạn được giục",
                             f"Hãy báo cáo nhiệm vụ '{full_title[:60]}'",
                             f"ctdctct:{tid}", target_uid=uid,
                             sender_name=_ctd_reminder_by)
            if refresh:
                self.toast(f"✅ Đã giục {name}")
                self.task_open_task_detail(domain, cur)

        def remind_all():
            n = 0
            for it in pending_list:
                remind_one(it["uid"], soldier_label(it["soldier"], it["uid"]),
                           refresh=False)
                n += 1
            self.toast(f"✅ Đã giục {n} người")
            self.task_open_task_detail(domain, cur)

        def pending_row(item):
            s = item["soldier"]
            uid = item["uid"]
            name = soldier_label(s, uid)
            rec = reminders_log.get(f"{tid}::{uid}")
            if rec:
                btn = ft.Container(
                    content=ft.Text(f"Đã giục {time_ago(rec.get('at', 0))}",
                                    size=10, color=TEXT_MUTED),
                    padding=ft.padding.symmetric(horizontal=8, vertical=4),
                )
            else:
                btn = ft.ElevatedButton(
                    "📢 Giục",
                    on_click=lambda e, _u=uid, _n=name: remind_one(_u, _n),
                    bgcolor=AMBER, color=ft.Colors.WHITE,
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=8),
                        padding=ft.padding.symmetric(horizontal=12, vertical=6),
                        text_style=ft.TextStyle(size=11, weight=ft.FontWeight.BOLD),
                    ),
                ) if is_admin else ft.Container()
            return ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Text(initials(s.get("name") or uid, 2),
                                        color=ft.Colors.WHITE, size=10,
                                        weight=ft.FontWeight.BOLD),
                        bgcolor="#9e9e9e", width=28, height=28,
                        border_radius=14, alignment=ft.alignment.center,
                    ),
                    ft.Column([
                        ft.Text(name, size=12, weight=ft.FontWeight.W_600),
                        ft.Text(s.get("role") or "Chiến sĩ", size=10, color=TEXT_MUTED),
                    ], spacing=1, expand=True, tight=True),
                    btn,
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                padding=10,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )

        pending_n = len(pending_list)
        pending_section = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Text(f"❌ Danh sách Chưa báo cáo ({pending_n})", size=13,
                            weight=ft.FontWeight.BOLD, color=RED, expand=True),
                    (ft.ElevatedButton(
                        "📢 Giục tất cả",
                        on_click=lambda e: remind_all(),
                        bgcolor=RED, color=ft.Colors.WHITE,
                        style=ft.ButtonStyle(
                            shape=ft.RoundedRectangleBorder(radius=8),
                            padding=ft.padding.symmetric(horizontal=10, vertical=6),
                            text_style=ft.TextStyle(size=11, weight=ft.FontWeight.BOLD),
                        ),
                    ) if (pending_list and is_admin) else ft.Container()),
                ]),
                *([pending_row(it) for it in pending_list] if pending_list
                  else [ft.Container(
                      content=ft.Text("🎉 Toàn đơn vị đã báo cáo xong!",
                                      size=12, color=GREEN_MID,
                                      weight=ft.FontWeight.W_600),
                      padding=14)]),
            ], spacing=0),
            bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
            margin=ft.margin.only(bottom=10),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        task_type = (cur.get("taskType") or "report")  # default report cho cũ
        receipts = cur.get("receipts") or {}

        action_btn = None
        if my_uid in members:
            my_received = my_uid in receipts
            my_submitted = my_uid in subs

            if task_type == "notify":
                # Loại "Gửi văn bản" — chỉ cần nút Đã nhận
                if not my_received:
                    action_btn = ft.Container(
                        content=ft.ElevatedButton(
                            "✅ Đã nhận",
                            on_click=lambda e: self.task_mark_received(domain, cur),
                            bgcolor=GREEN_MID, color=ft.Colors.WHITE,
                            width=10000,
                            style=ft.ButtonStyle(
                                shape=ft.RoundedRectangleBorder(radius=8),
                                padding=12,
                                text_style=ft.TextStyle(size=14,
                                                        weight=ft.FontWeight.BOLD),
                            ),
                        ),
                        margin=ft.margin.only(bottom=10),
                    )
                else:
                    action_btn = ft.Container(
                        content=ft.Text("✅ Bạn đã xác nhận nhận văn bản",
                                        size=12, color=GREEN_DARK,
                                        weight=ft.FontWeight.BOLD,
                                        text_align=ft.TextAlign.CENTER),
                        padding=12, bgcolor="#e8f5e9", border_radius=8,
                        margin=ft.margin.only(bottom=10),
                    )
            else:
                # Loại "Yêu cầu báo cáo" — cần đã nhận trước, sau đó báo cáo
                btns = []
                if not my_received:
                    btns.append(ft.ElevatedButton(
                        "✅ Đã nhận",
                        on_click=lambda e: self.task_mark_received(domain, cur),
                        bgcolor=BLUE, color=ft.Colors.WHITE, expand=True,
                        style=ft.ButtonStyle(
                            shape=ft.RoundedRectangleBorder(radius=8),
                            padding=12,
                        ),
                    ))
                if not my_submitted:
                    btns.append(ft.ElevatedButton(
                        "📤 Tôi báo cáo",
                        on_click=lambda e: self.task_open_submit(domain, cur),
                        bgcolor=GREEN_MID, color=ft.Colors.WHITE, expand=True,
                        style=ft.ButtonStyle(
                            shape=ft.RoundedRectangleBorder(radius=8),
                            padding=12,
                        ),
                    ))
                if btns:
                    action_btn = ft.Container(
                        content=ft.Row(btns, spacing=6),
                        margin=ft.margin.only(bottom=10),
                    )

        body_col = ft.Column([
            ft.Container(
                content=ft.Row([
                    ft.IconButton(ft.Icons.ARROW_BACK,
                                  on_click=lambda e: self._task_back_to_list(domain)),
                    ft.Text("Chi tiết nhiệm vụ", size=14,
                            weight=ft.FontWeight.BOLD, expand=True),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=BG, padding=ft.padding.symmetric(horizontal=4, vertical=4),
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            ),
            ft.ListView(
                controls=[ft.Container(
                    content=ft.Column(
                        [header, unit_section, done_section,
                         await_section, pending_section]
                        + ([action_btn] if action_btn else []),
                        spacing=0,
                    ),
                    padding=10,
                )],
                expand=True,
            ),
        ], spacing=0, expand=True)

        self.body.content = body_col
        self.refresh()

    def task_mark_received(self, domain: str, task: dict) -> None:
        """Đánh dấu user hiện tại đã nhận nhiệm vụ này."""
        uid = AUTH_STATE.get("uid", "")
        if not uid:
            return
        tasks = store.get(self.TASK_DOMAIN_CONFIG.get(domain, {}).get('store_key'), lambda: [])
        title = ""
        creator_uid = ""
        for t in tasks:
            if t.get("id") == task.get("id"):
                t.setdefault("receipts", {})[uid] = {
                    "at": store.now_ms(),
                }
                title = t.get("title", "")
                creator_uid = t.get("createdBy") or ""
                break
        store.set_value("ctdctctTasks", tasks)
        # Notif về người triển khai để họ biết ai đã nhận
        if creator_uid and creator_uid != uid:
            profile = store.get("userProfile", store.seed_user_profile)
            _recv_name = profile.get("name") or profile.get("username") or uid
            store.push_notif(
                "ctdctct", "✅ Có người xác nhận nhận",
                f"{_recv_name} đã nhận: {title[:60]}",
                f"ctdctct:{task.get('id','')}", target_uid=creator_uid,
                sender_name=_recv_name,
            )
        self.toast("✅ Đã xác nhận nhận nhiệm vụ")
        self.task_open_task_detail(domain, task)

    def _task_back_to_list(self, domain: str) -> None:
        setattr(self, f"task_{domain}_view", "tasks")
        self.body.content = self._render_task_module(domain)
        self.refresh()

    def task_open_submit(self, domain: str, task: dict) -> None:
        """Dialog báo cáo nhiệm vụ — nội dung + đính kèm ảnh / video."""
        page = self.page
        note_input = ft.TextField(label="Nội dung báo cáo *", border_radius=8,
                                  dense=True, multiline=True,
                                  min_lines=3, max_lines=6)
        attached_images: list[str] = []  # urls download Firebase Storage
        attached_videos: list[str] = []
        status_text = ft.Text("", size=11, color=TEXT_MUTED)

        def _refresh_status():
            parts = []
            if attached_images:
                parts.append(f"📷 {len(attached_images)} ảnh")
            if attached_videos:
                parts.append(f"🎬 {len(attached_videos)} video")
            status_text.value = " • ".join(parts) or "Chưa đính kèm"
            try:
                page.update()
            except Exception:
                pass

        def on_files_picked(e: ft.FilePickerResultEvent):
            if not e.files:
                return
            if not store.STORE.is_bound() or not AUTH_STATE.get("idToken"):
                status_text.value = "❌ Phải đăng nhập để upload"
                page.update(); return
            status_text.value = f"⏳ Upload {len(e.files)} file..."
            page.update()

            def worker():
                ok = 0
                for f in e.files:
                    try:
                        name_low = (f.name or "").lower()
                        is_video = any(name_low.endswith(ext) for ext in
                                       (".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"))
                        folder = f"reports/task/{AUTH_STATE['uid']}"
                        remote = fb_storage.make_remote_path(folder, f.name)
                        if f.path:
                            res = fb_storage.upload_file(f.path, remote, AUTH_STATE["idToken"])
                        elif f.bytes:
                            res = fb_storage.upload_data(remote, f.bytes, AUTH_STATE["idToken"], f.name)
                        else:
                            continue
                        url = res["downloadURL"]
                        if is_video:
                            attached_videos.append(url)
                        else:
                            attached_images.append(url)
                        ok += 1
                    except Exception as ex_e:
                        status_text.value = f"❌ {ex_e}"
                _refresh_status()

            threading.Thread(target=worker, daemon=True).start()

        picker = ft.FilePicker(on_result=on_files_picked)
        if picker not in page.overlay:
            page.overlay.append(picker)

        pick_img_btn = ft.ElevatedButton(
            "📷 Ảnh",
            on_click=lambda e: picker.pick_files(
                allow_multiple=True,
                file_type=ft.FilePickerFileType.IMAGE,
            ),
        )
        pick_video_btn = ft.ElevatedButton(
            "🎬 Video",
            on_click=lambda e: picker.pick_files(
                allow_multiple=True,
                file_type=ft.FilePickerFileType.VIDEO,
            ),
        )
        pick_any_btn = ft.TextButton(
            "📁 File khác",
            on_click=lambda e: picker.pick_files(
                allow_multiple=True,
                file_type=ft.FilePickerFileType.ANY,
            ),
        )

        err_text = ft.Text("", color=RED, size=12)

        def submit(e):
            note = (note_input.value or "").strip()
            if not note:
                err_text.value = "⚠️ Cần nội dung báo cáo"
                page.update(); return
            uid = AUTH_STATE.get("uid", "")
            tasks = store.get(self.TASK_DOMAIN_CONFIG.get(domain, {}).get('store_key'), lambda: [])
            approvers = []
            for t in tasks:
                if t.get("id") == task.get("id"):
                    t.setdefault("submissions", {})[uid] = {
                        "at": store.now_ms(),
                        "note": note,
                        "images": list(attached_images),
                        "videos": list(attached_videos),
                        "imageCount": len(attached_images),
                        "videoCount": len(attached_videos),
                        "by": uid,
                        "status": "pending",
                    }
                    if t.get("createdBy"):
                        approvers.append(t["createdBy"])
                    approvers.extend(t.get("followers") or [])
                    break
            store.set_value("ctdctctTasks", tasks)
            store.log_activity(f"Báo cáo CTĐ-CTCT: {task.get('title','')[:40]}")
            profile = store.get("userProfile", store.seed_user_profile)
            sender_name = profile.get("name") or AUTH_STATE.get("username", "")
            _task_link = f"ctdctct:{task.get('id','')}"
            for au in set(approvers):
                if au and au != uid:
                    store.push_notif(
                        "ctdctct", "📤 Báo cáo CTĐ-CTCT cần duyệt",
                        f"{sender_name} đã báo cáo: {task.get('title','')[:50]}",
                        _task_link, target_uid=au,
                        sender_name=sender_name,
                    )
            try:
                _dlg.open = False
            except Exception:
                pass
            self.toast("✅ Đã gửi báo cáo, chờ duyệt")
            self.task_open_task_detail(domain, task)

        _refresh_status()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("📤 Báo cáo nhiệm vụ", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column([
                    note_input,
                    ft.Text("Đính kèm minh chứng:", size=12, color=TEXT_MUTED,
                            weight=ft.FontWeight.W_500),
                    ft.Row([pick_img_btn, pick_video_btn, pick_any_btn],
                           spacing=6, wrap=True),
                    ft.Container(
                        content=status_text,
                        bgcolor=BG2, border_radius=8,
                        padding=ft.padding.symmetric(horizontal=10, vertical=8),
                        border=ft.border.all(1, BORDER),
                    ),
                    err_text,
                ], spacing=10, tight=True, scroll=ft.ScrollMode.AUTO),
                width=380, height=440,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton("Gửi", on_click=submit,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def _f47_back_to_list(self) -> None:
        """Quay lại list chiến dịch trong module F47."""
        self.f47_view = "campaigns"
        self.body.content = self.module_f47()
        self.refresh()

    def f47_open_submit(self, camp: dict) -> None:
        """Mở sheet 'Nộp minh chứng F47' — nhập link + upload ảnh lên Storage."""
        page = self.page
        link_input = ft.TextField(
            label="Link bài viết (Facebook / TikTok / ...)",
            hint_text="https://facebook.com/...",
            border_radius=8, dense=True,
        )
        note_input = ft.TextField(
            label="Ghi chú (tuỳ chọn)", border_radius=8, dense=True,
            multiline=True, min_lines=2, max_lines=4,
        )
        status_text = ft.Text("", size=12, color=TEXT_MUTED)
        uploaded: list[dict] = []  # mỗi phần tử: {downloadURL, name, ...}

        # FilePicker để chọn ảnh
        def on_files_picked(e: ft.FilePickerResultEvent):
            if not e.files:
                return
            if not store.STORE.is_bound() or not AUTH_STATE.get("idToken"):
                status_text.value = "❌ Phải đăng nhập trước khi upload"
                page.update()
                return
            status_text.value = f"⏳ Đang upload {len(e.files)} ảnh..."
            page.update()

            def worker():
                ok = 0
                for f in e.files:
                    try:
                        remote = fb_storage.make_remote_path(
                            f"f47/{camp['id']}/{AUTH_STATE['uid']}", f.name
                        )
                        if f.path:
                            res = fb_storage.upload_file(f.path, remote, AUTH_STATE["idToken"])
                        elif f.bytes:
                            res = fb_storage.upload_data(remote, f.bytes, AUTH_STATE["idToken"], f.name)
                        else:
                            continue
                        uploaded.append(res)
                        ok += 1
                    except Exception as ex:
                        status_text.value = f"❌ Upload lỗi: {ex}"
                status_text.value = f"✅ Đã upload {ok}/{len(e.files)} ảnh"
                try:
                    page.update()
                except Exception:
                    pass

            threading.Thread(target=worker, daemon=True).start()

        picker = ft.FilePicker(on_result=on_files_picked)
        if picker not in page.overlay:
            page.overlay.append(picker)

        pick_btn = ft.ElevatedButton(
            "📷 Chọn ảnh minh chứng",
            on_click=lambda e: picker.pick_files(
                allow_multiple=True, file_type=ft.FilePickerFileType.IMAGE,
            ),
        )

        def submit(e):
            link = (link_input.value or "").strip()
            if not link and not uploaded:
                status_text.value = "⚠️ Cần ít nhất 1 link hoặc 1 ảnh"
                page.update(); return
            uid = AUTH_STATE.get("uid") or "unknown"
            sub = {
                "at": store.now_ms(),
                "links": [link] if link else [],
                "images": [u["downloadURL"] for u in uploaded],
                "imageCount": len(uploaded),
                "note": (note_input.value or "").strip(),
                "by": uid,
                "byName": store.get("userProfile", store.seed_user_profile).get("name", ""),
            }
            # Cập nhật trong store (sẽ tự push Firestore)
            camps = store.get("f47Campaigns", store.seed_f47)
            for c in camps:
                if c.get("id") == camp.get("id"):
                    subs = c.setdefault("submissions", {})
                    subs[uid] = sub
                    break
            store.set_value("f47Campaigns", camps)
            store.log_activity(f"Nộp F47: {camp.get('title','')[:40]}")
            store.push_notif("success", "✅ Đã nộp minh chứng F47",
                             camp.get("title", ""), "f47")
            try:
                _dlg.open = False
            except Exception:
                pass
            self.toast("Đã nộp minh chứng F47")
            self.set_tab("util")  # quay lại tiện ích / có thể đổi sang home

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Nộp minh chứng F47\n{camp.get('title','')[:60]}", size=14),
            content=ft.Container(
                content=ft.Column(
                    [link_input, note_input, pick_btn, status_text],
                    spacing=10, tight=True,
                ),
                width=380,
            ),
            actions=[
                ft.TextButton("Huỷ",
                              on_click=lambda e: _close_dialog(self.page)),
                ft.ElevatedButton("Gửi", on_click=submit,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    # ============================================================
    # ===== GUEST WORKFLOW: helpers chuỗi duyệt nhiều cấp     =====
    # ============================================================

    # Map status → (label, color attr name on this class)
    GUEST_STATUS = {
        "pending":          ("⏳ Chờ phê duyệt",       "AMBER"),
        "received":         ("📥 Chỉ huy đã nhận",     "BLUE"),
        "forwarded":        ("📤 Đã trình cấp trên",   "BLUE"),
        "review_requested": ("✏️ Cần kiểm tra lại",   "AMBER"),
        "approved":         ("✅ Đã duyệt",             "GREEN_MID"),
        "rejected":         ("❌ Từ chối",              "RED"),
        "checked_in":       ("🚪 Khách vào cổng",       "BLUE"),
        "completed":        ("🏁 Đã hoàn thành",        "GREY"),
        "checked_out":      ("🏁 Đã hoàn thành",        "GREY"),  # alias cũ
    }

    # Map vehicle key → label
    VEHICLE_OPTIONS = [
        ("walk",      "🚶 Đi bộ"),
        ("motorbike", "🏍 Xe máy"),
        ("car",       "🚗 Ô tô"),
    ]
    VEHICLE_LABEL = {k: v for k, v in VEHICLE_OPTIONS}

    def _list_higher_commanders(self, my_uid: str) -> list[tuple]:
        """Liệt kê các chỉ huy CẤP TRÊN hợp lệ có thể trình lên.

        Nguyên tắc tổ chức:
            - 3 cơ quan (CQTM, CQCT, HC-KT) NGANG CẤP — không trình ngang nhau,
              chỉ trình lên Ban chỉ huy Trung đoàn (u-bch).
            - Đại đội trực thuộc TĐ → trình lên Ban chỉ huy TĐ → trình tiếp lên BCH/e.
            - Đại đội trực thuộc Trung đoàn (u-c14..c25) → trình thẳng BCH/e.
            - BCH/e là cấp cao nhất, không còn cấp trên.

        Trả về list[(unit_label, soldier_dict)] đã sắp theo cấp gần → xa.
        """
        soldiers = store.get("soldiers", store.seed_soldiers)
        units = store.get("units", store.seed_units)
        me = next((s for s in soldiers if str(s.get("id")) == str(my_uid)), None)
        if not me:
            return []
        my_unit = me.get("unitId") or ""
        if not my_unit:
            return []

        # Build parent_map + name_map + node_by_id
        parent_map: dict[str, str | None] = {}
        name_map: dict[str, str] = {}
        node_by_id: dict[str, dict] = {}
        def walk(n, p=None):
            if isinstance(n, dict):
                nid = n.get("id")
                if nid:
                    parent_map[nid] = p
                    name_map[nid] = n.get("name") or ""
                    node_by_id[nid] = n
                for c in n.get("children") or []:
                    walk(c, nid)
        walk(units)

        # Tìm chuỗi tổ tiên hợp lệ (KHÔNG đi qua cơ quan ngang cấp)
        ancestors: list[str] = []
        # Bước 1: nếu đơn vị mình thuộc 1 trong các đơn vị trong BCH/e → cao nhất rồi
        cur = my_unit
        while cur and parent_map.get(cur) and parent_map[cur] != "root":
            cur = parent_map[cur]
        # cur giờ là đơn vị cấp 1 (trực tiếp con của root) chứa my_unit
        # Nếu cur == u-bch → tôi là cấp cao nhất, không trình lên ai
        if cur == "u-bch":
            return []

        # Nếu cur là cơ quan (u-tm/u-ct/u-hk) hoặc đại đội trực thuộc (u-c14..u-c25)
        # → cấp trên DUY NHẤT là u-bch (Ban chỉ huy Trung đoàn)
        if cur in ("u-tm", "u-ct", "u-hk") or (cur or "").startswith("u-c"):
            ancestors = ["u-bch"]
        else:
            # Tiểu đoàn (u-d7/u-d8/u-d9) → trình lên Ban chỉ huy TĐ trước, rồi BCH/e
            # Trừ khi mình ĐÃ ở BCH TĐ → bỏ qua cấp đó
            tdoan_id = cur  # u-d7
            tdoan_bch_id = f"{cur}-bch"
            # Nếu mình đang ở ngay trong BCH TĐ → bỏ qua BCH TĐ, đi thẳng BCH/e
            if my_unit == tdoan_bch_id or parent_map.get(my_unit) == tdoan_bch_id:
                ancestors = ["u-bch"]
            else:
                ancestors = [tdoan_bch_id, "u-bch"]

        # Với mỗi tổ tiên, gom các chỉ huy thuộc cây con của tổ tiên đó
        CMD_KEYS = ("trưởng", "chủ nhiệm", "chính uỷ", "chính ủy")
        EXCLUDE = ("trợ lý", "nhân viên", "chiến sĩ", "thủ kho")

        def is_in_subtree(s_uid: str, root_uid: str) -> bool:
            """s_uid có nằm trong cây con của root_uid không (kể cả equal)."""
            cur2 = s_uid
            while cur2:
                if cur2 == root_uid:
                    return True
                cur2 = parent_map.get(cur2)
            return False

        # Các cơ quan ngang cấp với mình → TUYỆT ĐỐI loại khỏi danh sách trình lên.
        # Khi cur là u-tm/u-ct/u-hk → 2 cơ quan còn lại là peer.
        # Khi cur là u-d7/u-d8/u-d9 → 2 tiểu đoàn còn lại + cả 3 cơ quan đều peer.
        peer_top_units: set[str] = set()
        if cur in ("u-tm", "u-ct", "u-hk"):
            peer_top_units = {"u-tm", "u-ct", "u-hk"} - {cur}
        elif cur in ("u-d7", "u-d8", "u-d9"):
            peer_top_units = {"u-d7", "u-d8", "u-d9"} - {cur}
            peer_top_units |= {"u-tm", "u-ct", "u-hk"}
        elif (cur or "").startswith("u-c"):
            # Đại đội trực thuộc — peer: các đại đội trực thuộc khác + 3 cơ quan + 3 tiểu đoàn
            top_peers = {"u-tm", "u-ct", "u-hk", "u-d7", "u-d8", "u-d9"}
            other_companies = {f"u-c{n}" for n in (14, 15, 16, 17, 18, 20, 24, 25) if f"u-c{n}" != cur}
            peer_top_units = top_peers | other_companies

        def _in_peer_subtree(s_uid: str) -> bool:
            cur2 = s_uid
            while cur2:
                if cur2 in peer_top_units:
                    return True
                cur2 = parent_map.get(cur2)
            return False

        results: list[tuple] = []
        seen_ids = {str(my_uid)}
        for anc_uid in ancestors:
            anc_label = store.canonical_unit_name(node_by_id.get(anc_uid, {"id": anc_uid,
                                                                            "name": name_map.get(anc_uid, "")}))
            candidates = []
            for s in soldiers:
                sid = str(s.get("id"))
                if sid in seen_ids or s.get("isAdmin"):
                    continue
                s_uid = s.get("unitId") or ""
                if not is_in_subtree(s_uid, anc_uid):
                    continue
                # Hard-block: bất kỳ ai thuộc cây con của cơ quan ngang cấp
                if _in_peer_subtree(s_uid):
                    continue
                role = (s.get("role") or "").lower()
                if not any(k in role for k in CMD_KEYS):
                    continue
                if any(k in role for k in EXCLUDE):
                    continue
                candidates.append(s)
                seen_ids.add(sid)
            candidates.sort(key=lambda x: (
                store.role_priority(x.get("role") or ""),
                -int(x.get("adminLevel") or 0),
                x.get("name") or "",
            ))
            for s in candidates:
                results.append((anc_label, s))
        return results

    def _find_commander_above(self, target_uid: str):
        """Tìm chỉ huy cấp TRÊN của một quân nhân theo cây đơn vị.

        Duyệt từ unitId của quân nhân, đi lên 1 cấp (parent unit) rồi tìm
        người có role chứa keyword chỉ huy ('trưởng', 'chủ nhiệm', 'chính uỷ').
        Nếu không tìm thấy ở cấp đó, tiếp tục đi lên.
        """
        soldiers = store.get("soldiers", store.seed_soldiers)
        units = store.get("units", store.seed_units)
        target = next((s for s in soldiers if str(s.get("id")) == str(target_uid)), None)
        if not target:
            return None
        my_unit = target.get("unitId") or ""
        if not my_unit:
            return None

        # Build parent map: child_id → parent_id
        parent_map: dict[str, str] = {}
        def walk(n, p=None):
            if not isinstance(n, dict):
                return
            nid = n.get("id")
            if nid:
                parent_map[nid] = p
            for c in n.get("children") or []:
                walk(c, nid)
        walk(units)

        CMD_KEYS = ("trưởng", "chủ nhiệm", "chính uỷ", "chính ủy")
        EXCLUDE = ("trợ lý", "nhân viên", "chiến sĩ")

        # Đi lên từng cấp, tìm chỉ huy ở cấp đó hoặc các đơn vị con trực tiếp của cấp đó
        curr = parent_map.get(my_unit)
        while curr:
            candidates = []
            for s in soldiers:
                if s.get("isAdmin") or str(s.get("id")) == str(target_uid):
                    continue
                s_uid = s.get("unitId") or ""
                if s_uid != curr and parent_map.get(s_uid) != curr:
                    continue
                role = (s.get("role") or "").lower()
                if not any(k in role for k in CMD_KEYS):
                    continue
                if any(k in role for k in EXCLUDE):
                    continue
                candidates.append(s)
            if candidates:
                # Ưu tiên role priority thấp nhất (= cấp trưởng), rồi adminLevel cao
                candidates.sort(key=lambda x: (
                    store.role_priority(x.get("role") or ""),
                    -int(x.get("adminLevel") or 0),
                ))
                return candidates[0]
            curr = parent_map.get(curr)
        return None

    def _is_top_commander(self, soldier_uid: str) -> bool:
        """Là chỉ huy cao nhất (Trung đoàn trưởng / Chính uỷ Trung đoàn)?"""
        if not soldier_uid:
            return False
        soldiers = store.get("soldiers", store.seed_soldiers)
        s = next((x for x in soldiers if str(x.get("id")) == str(soldier_uid)), None)
        if not s:
            return False
        role = (s.get("role") or "").lower().strip()
        unit = (s.get("unitId") or "")
        # Chính uỷ + Trung đoàn trưởng (không phải phó) — ở BCH/e
        if not unit.startswith("u-bch"):
            return False
        if "phó" in role:
            return False
        return any(k in role for k in ("trung đoàn trưởng", "chính uỷ", "chính ủy"))

    def _guest_append_chain(self, g: dict, action: str, note: str = "") -> dict:
        """Thêm 1 entry vào approvalChain của guest doc."""
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_uid = my_profile.get("id") or AUTH_STATE.get("localId") or ""
        entry = {
            "approverId": str(my_uid),
            "approverName": my_profile.get("name") or "",
            "approverRole": my_profile.get("role") or "",
            "approverUnit": my_profile.get("unitName") or "",
            "action": action,
            "note": note or "",
            "at": store.now_ms(),
        }
        chain = list(g.get("approvalChain") or [])
        chain.append(entry)
        return entry

    def module_guests(self) -> ft.Control:
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_uid = my_profile.get("id") or AUTH_STATE.get("localId")
        my_admin = int(my_profile.get("adminLevel") or 1)
        
        if not hasattr(self, "guests_view"):
            self.guests_view = "my_guests"
        current_view = self.guests_view
        is_commander = (my_admin >= 2) or self._is_top_commander(my_uid) or (my_profile.get("unitId") in ("u-bch", "u-tm", "u-ct", "u-hk"))

        # Helper rebuild — đặt content body mới + force update
        def _rebuild():
            self.body.content = self.module_guests()
            self.refresh()
        self._rebuild_guests_module = _rebuild

        # ===== Tabs (dùng ft.Tabs widget — proven pattern) =====
        tab_defs: list[tuple[str, str]] = [("Của tôi", "my_guests")]
        if is_commander:
            tab_defs.append(("Quản lý", "manage"))
            tab_defs.append(("Thống kê", "stats"))
        tab_keys = [k for _, k in tab_defs]
        try:
            selected_idx = tab_keys.index(current_view)
        except ValueError:
            selected_idx = 0
            self.guests_view = tab_keys[0]
            current_view = tab_keys[0]

        def on_guests_tab_changed(e):
            try:
                idx = e.control.selected_index
            except Exception:
                idx = getattr(e, "selected_index", 0)
            if idx is None or not (0 <= idx < len(tab_defs)):
                return
            new_key = tab_defs[idx][1]
            if new_key == self.guests_view:
                return
            self.guests_view = new_key
            self._guest_selected_ids = set()
            self.body.content = self.module_guests()
            self.refresh()

        def _manual_refresh(_):
            self.toast("⟳ Đang đồng bộ...")
            def _do():
                try:
                    store.refresh_guests()
                except Exception:
                    pass
                def _ui():
                    if hasattr(self, "_rebuild_guests_module"):
                        self._rebuild_guests_module()
                    else:
                        self.refresh()
                try:
                    if hasattr(self.page, "run_thread"):
                        self.page.run_thread(_ui)
                    else:
                        _ui()
                except Exception:
                    pass
            threading.Thread(target=_do, daemon=True).start()

        def _export_guests(e):
            import datetime
            headers = ["📅 Thời gian", "Người tiếp", "Đơn vị người tiếp", "Tên khách", "Số lượng", "Lý do", "Phương tiện", "BKS/Số hiệu", "Trạng thái"]
            rows = []
            for g in display_guests:
                sid = str(g.get("soldierId"))
                soldier = next((s for s in soldiers_all if str(s.get("id")) == sid), {})
                u_id = soldier.get("unitId") or ""
                u_label = store.canonical_unit_name({"id": u_id, "name": unit_name_map.get(u_id, "")}) \
                          or soldier.get("unitName") or "Khác"
                
                # Format time
                date_str = "Chưa rõ"
                if g.get("arrivalTimeMs"):
                    dt = datetime.datetime.fromtimestamp(g.get("arrivalTimeMs") / 1000)
                    date_str = dt.strftime("%H:%M %d/%m/%Y")
                elif g.get("visitDateMs"):
                    dt = datetime.datetime.fromtimestamp(g.get("visitDateMs") / 1000)
                    date_str = dt.strftime("%d/%m/%Y")
                
                # Vehicle format
                vh = g.get("vehicle") or "walk"
                vh_label = "Đi bộ" if vh == "walk" else "Xe máy" if vh == "motorbike" else "Ô tô" if vh == "car" else vh
                
                # Status format
                st = g.get("status", "pending")
                st_label = self.GUEST_STATUS.get(st, (st, "GREY"))[0]
                
                rows.append([
                    date_str,
                    soldier.get("name", "Chưa rõ"),
                    u_label,
                    g.get("guestName", ""),
                    g.get("guestCount", 1),
                    g.get("purpose", ""),
                    vh_label,
                    g.get("vehiclePlate", ""),
                    st_label
                ])
            self.export_data_to_csv("DanhSach_KhachTham", headers, rows)

        view_toggle = ft.Container(
            content=ft.Row([
                ft.Container(
                    content=ft.Tabs(
                        selected_index=selected_idx,
                        on_change=on_guests_tab_changed,
                        tabs=[ft.Tab(text=label) for label, _ in tab_defs],
                        height=46,
                    ),
                    expand=True, height=46,
                ),
                ft.IconButton(
                    ft.Icons.FILE_DOWNLOAD, icon_color=GREEN_MID, icon_size=20,
                    tooltip="Xuất Excel/CSV",
                    on_click=_export_guests,
                ),
                ft.IconButton(
                    ft.Icons.REFRESH, icon_color=GREEN_MID, icon_size=20,
                    tooltip="Đồng bộ lại",
                    on_click=_manual_refresh,
                ),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
            bgcolor=BG,
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            padding=ft.padding.symmetric(horizontal=10, vertical=4),
        )

        # Tab Thống kê — sớm thoát sang view riêng (cùng FAB)
        if current_view == "stats" and is_commander:
            stats_fab = ft.Container(
                content=ft.FloatingActionButton(
                    icon=ft.Icons.ADD, bgcolor=GREEN_MID,
                    tooltip="Đăng ký tiếp khách",
                    on_click=lambda e: self.open_guest_registration_dialog(),
                ),
                right=16, bottom=16,
            )
            stats_col = ft.Column(
                [view_toggle, self._guest_stats_view()],
                spacing=0, expand=True,
            )
            return ft.Container(content=ft.Stack([stats_col, stats_fab]), expand=True)

        all_guests = list(store.get("guests", []) or [])
        soldiers_all = store.get("soldiers", store.seed_soldiers)
        units_data = store.get("units", store.seed_units)

        # Bản đồ unit theo cây canonical để sắp xếp theo cây
        unit_order: dict[str, int] = {}
        unit_name_map: dict[str, str] = {}
        counter = [0]
        TOP_ORDER = ["u-bch","u-tm","u-ct","u-hk","u-d7","u-d8","u-d9",
                     "u-c14","u-c15","u-c16","u-c17","u-c18","u-c20","u-c24","u-c25"]
        def _walk_units(n):
            if not isinstance(n, dict): return
            nid = n.get("id") or ""
            if nid:
                unit_order[nid] = counter[0]; counter[0] += 1
                unit_name_map[nid] = n.get("name") or ""
            kids = list(n.get("children") or [])
            if n.get("id") == "root":
                idx = {u: i for i, u in enumerate(TOP_ORDER)}
                kids.sort(key=lambda c: idx.get(c.get("id"), len(TOP_ORDER)+1))
            for c in kids:
                _walk_units(c)
        _walk_units(units_data)

        # Define parent_map, descendants_of, and scope_soldier_ids to filter guests in the manage tab
        parent_map: dict[str, str] = {}
        name_map: dict[str, str] = {}
        def walk(n, p=None):
            if isinstance(n, dict):
                nid = n.get("id")
                if nid:
                    parent_map[nid] = p
                    name_map[nid] = n.get("name") or ""
                for c in n.get("children") or []:
                    walk(c, nid)
        walk(units_data)

        def descendants_of(uid: str) -> set[str]:
            out = {uid}
            stack = [uid]
            while stack:
                cur = stack.pop()
                for child_id, par in parent_map.items():
                    if par == cur and child_id not in out:
                        out.add(child_id)
                        stack.append(child_id)
            return out

        my_unit = my_profile.get("unitId") or ""
        is_super = bool(my_profile.get("isAdmin"))
        is_regiment_staff = my_unit in ("u-bch", "u-tm", "u-ct", "u-hk") or my_admin >= 3 or self._is_top_commander(my_uid)
        if is_super or is_regiment_staff:
            scope_unit_ids = set(parent_map.keys())
        else:
            scope_unit_ids = descendants_of(my_unit) if my_unit else set()
        scope_soldiers = [s for s in soldiers_all
                          if not s.get("isAdmin") and (s.get("unitId") in scope_unit_ids)]
        scope_soldier_ids = {str(s.get("id")) for s in scope_soldiers}

        my_uid_str = str(my_uid or "")
        if current_view == "my_guests":
            display_guests = [
                g for g in all_guests
                if str(g.get("soldierId") or "") == my_uid_str
                or str(g.get("createdBy") or "") == my_uid_str
            ]
        else:
            # Quản lý: chỉ huy nhìn thấy toàn bộ danh sách khách của các đơn vị cấp dưới trực thuộc
            display_guests = [
                g for g in all_guests
                if str(g.get("soldierId")) in scope_soldier_ids
            ]

        # Khởi tạo selection set cho bulk-forward
        if not hasattr(self, "_guest_selected_ids"):
            self._guest_selected_ids = set()
        # Loại bỏ id không còn trong list
        valid_ids = {g.get("id") for g in display_guests}
        self._guest_selected_ids &= valid_ids

        # Sắp xếp: theo đơn vị của người đăng ký (cây canonical), rồi theo thời gian đến
        soldier_unit_id = {str(s.get("id")): s.get("unitId") or "" for s in soldiers_all}
        def _sort_key(g):
            sid = str(g.get("soldierId"))
            u_id = soldier_unit_id.get(sid, "")
            return (unit_order.get(u_id, 10**9),
                    -(g.get("arrivalTimeMs") or g.get("createdAt") or 0))
        display_guests.sort(key=_sort_key)

        list_view = ft.ListView(expand=True, spacing=6, padding=ft.padding.only(top=8, bottom=80, left=8, right=8))

        # ===== Khung thông số luôn hiển thị ở đầu (cả khi rỗng) =====
        import datetime
        today_d = datetime.date.today()
        day_start = int(datetime.datetime.combine(today_d, datetime.time.min).timestamp() * 1000)
        day_end = int(datetime.datetime.combine(today_d, datetime.time.max).timestamp() * 1000)
        def _in_today(g):
            ms = g.get("arrivalTimeMs") or g.get("createdAt") or 0
            return day_start <= ms <= day_end
        today_in_tab = [g for g in display_guests if _in_today(g)]
        n_visits = len(today_in_tab)
        n_people = sum(int(g.get("guestCount") or 1) for g in today_in_tab)
        n_walk = sum(1 for g in today_in_tab if (g.get("vehicle") or "walk") == "walk")
        n_moto = sum(int(g.get("vehicleCount") or 1) for g in today_in_tab if (g.get("vehicle") or "") == "motorbike")
        n_car  = sum(int(g.get("vehicleCount") or 1) for g in today_in_tab if (g.get("vehicle") or "") == "car")
        n_pending = sum(1 for g in display_guests
                        if g.get("status") in ("pending", "received", "forwarded"))
        n_done = sum(1 for g in display_guests
                     if g.get("status") in ("approved", "checked_in", "completed", "checked_out"))

        def _mini(icon, label, value, color):
            return ft.Container(
                content=ft.Column([
                    ft.Text(icon, size=18),
                    ft.Text(str(value), size=16, weight=ft.FontWeight.BOLD, color=color),
                    ft.Text(label, size=9, color=TEXT_MUTED, text_align=ft.TextAlign.CENTER),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=1, tight=True),
                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=8,
                padding=ft.padding.symmetric(horizontal=6, vertical=8),
                expand=True,
            )

        stats_title = ("📊 Của tôi hôm nay" if current_view == "my_guests"
                       else "📊 Đơn cần duyệt hôm nay")
        list_view.controls.append(
            ft.Container(
                content=ft.Column([
                    ft.Text(stats_title, size=12, weight=ft.FontWeight.BOLD, color=GREEN_DARK),
                    ft.Row([
                        _mini("🤝", "Lượt", n_visits, GREEN_DARK),
                        _mini("👤", "Khách", n_people, RED),
                        _mini("⏳", "Chờ", n_pending, AMBER),
                        _mini("✅", "Đã xong", n_done, GREEN_MID),
                    ], spacing=6),
                    ft.Row([
                        _mini("🚶", "Đi bộ", n_walk, ft.Colors.GREY),
                        _mini("🏍", "Xe máy", n_moto, AMBER),
                        _mini("🚗", "Ô tô", n_car, GREEN_MID),
                    ], spacing=6),
                ], spacing=6),
                padding=ft.padding.symmetric(horizontal=4, vertical=4),
            )
        )

        if not display_guests:
            empty_msg = (
                "Bạn chưa đăng ký tiếp khách nào.\nBấm ➕ để đăng ký mới."
                if current_view == "my_guests"
                else "Chưa có yêu cầu nào cần bạn xử lý."
            )
            list_view.controls.append(
                ft.Container(
                    content=ft.Column([
                        ft.Text("📭", size=40, text_align=ft.TextAlign.CENTER),
                        ft.Text(empty_msg, color=TEXT_MUTED,
                                size=13, text_align=ft.TextAlign.CENTER),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                    alignment=ft.alignment.center, padding=40,
                )
            )
        else:
            current_unit_label = None
            for g in display_guests:
                sid = str(g.get("soldierId"))
                soldier = next((s for s in soldiers_all if str(s.get("id")) == sid), {})
                u_id = soldier.get("unitId") or ""
                u_label = store.canonical_unit_name({"id": u_id, "name": unit_name_map.get(u_id, "")}) \
                          or soldier.get("unitName") or "Khác"
                # Section header khi đổi đơn vị
                if current_view == "manage" and u_label != current_unit_label:
                    current_unit_label = u_label
                    list_view.controls.append(
                        ft.Container(
                            content=ft.Text(u_label, size=11, color=TEXT_MUTED,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=BG2,
                            padding=ft.padding.symmetric(horizontal=12, vertical=6),
                        )
                    )

                st = g.get("status", "pending")
                lbl, color_name = self.GUEST_STATUS.get(st, (st, "GREY"))
                color = {
                    "AMBER": AMBER, "RED": RED, "GREEN_MID": GREEN_MID,
                    "BLUE": ft.Colors.BLUE, "GREY": ft.Colors.GREY,
                }.get(color_name, ft.Colors.GREY)

                date_str = "Chưa rõ"
                if g.get("arrivalTimeMs"):
                    import datetime
                    dt = datetime.datetime.fromtimestamp(g.get("arrivalTimeMs") / 1000)
                    date_str = dt.strftime("%H:%M %d/%m/%Y")
                elif g.get("visitDateMs"):
                    import datetime
                    dt = datetime.datetime.fromtimestamp(g.get("visitDateMs") / 1000)
                    date_str = dt.strftime("%d/%m/%Y")

                g_count = g.get("guestCount", 1)
                title = g.get("guestName", "Khách")
                if g_count > 1:
                    title += f" (+{g_count - 1} người)"
                rank_str = (soldier.get('rank') or '').strip()
                rank_prefix = f"{rank_str} " if rank_str else ""
                requester_line = f"Người đăng ký: đ.c {rank_prefix}{soldier.get('name','—')}"

                # Checkbox cho bulk-forward (chỉ ở tab "manage" và status cho phép forward)
                allow_select = current_view == "manage" and st in ("received", "pending")
                checkbox = None
                if allow_select:
                    gid = g.get("id")
                    def _toggle(e, _id=gid):
                        if e.control.value:
                            self._guest_selected_ids.add(_id)
                        else:
                            self._guest_selected_ids.discard(_id)
                        # Rebuild để bulk_bar hiện/ẩn theo selection
                        self._rebuild_guests_module()
                    checkbox = ft.Checkbox(
                        value=g.get("id") in self._guest_selected_ids,
                        on_change=_toggle,
                    )

                status_desc = "Chờ phê duyệt"
                status_color = AMBER
                if st in ("approved", "checked_in", "completed", "checked_out"):
                    status_desc = "Đã phê duyệt"
                    status_color = GREEN_MID
                elif st == "rejected":
                    status_desc = "Bị từ chối"
                    status_color = RED

                nudge_card_text = None
                if g.get("nudgeStatus") == "asked":
                    nudge_card_text = ft.Text("🔔 Chỉ huy hỏi: Khách đã về chưa?", size=11, color=RED, weight=ft.FontWeight.BOLD)

                card_row = ft.Row(
                    ([checkbox] if checkbox else []) + [
                        ft.Container(
                            content=ft.Column([
                                ft.Row([
                                    ft.Text(title, size=14, weight=ft.FontWeight.BOLD, expand=True),
                                    ft.Container(
                                        content=ft.Text(lbl, size=10, color=ft.Colors.WHITE,
                                                        weight=ft.FontWeight.BOLD),
                                        bgcolor=color, border_radius=12,
                                        padding=ft.padding.symmetric(horizontal=8, vertical=2),
                                    ),
                                ]),
                                ft.Text(requester_line, size=11, color=TEXT_MUTED),
                                ft.Text(f"Quan hệ: {g.get('relationship', '')} • Dự kiến: {date_str}",
                                        size=11, color=TEXT_MUTED),
                                ft.Text(f"Trạng thái: {status_desc}", size=11, color=status_color, weight=ft.FontWeight.BOLD),
                            ] + ([nudge_card_text] if nudge_card_text else []), spacing=3),
                            bgcolor=BG, padding=10, border_radius=8,
                            border=ft.border.all(1, BORDER),
                            on_click=lambda e, _g=g: self.open_guest_details_dialog(_g),
                            ink=True, expand=True,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=6,
                )
                list_view.controls.append(card_row)

        # Bulk-forward bar (chỉ ở manage view)
        bulk_bar = None
        if current_view == "manage" and self._guest_selected_ids:
            count = len(self._guest_selected_ids)

            def _bulk_forward(_):
                self._do_bulk_forward_guests(list(self._guest_selected_ids))

            bulk_bar = ft.Container(
                content=ft.Row([
                    ft.Text(f"Đã chọn {count} yêu cầu", color=ft.Colors.WHITE,
                            weight=ft.FontWeight.BOLD, expand=True),
                    ft.ElevatedButton(
                        "📤 Trình cấp trên",
                        on_click=_bulk_forward,
                        bgcolor=ft.Colors.WHITE, color=GREEN_DARK,
                    ),
                    ft.TextButton(
                        "Bỏ chọn",
                        on_click=lambda e: (
                            self._guest_selected_ids.clear(),
                            self._rebuild_guests_module(),
                        ),
                        style=ft.ButtonStyle(color=ft.Colors.WHITE),
                    ),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=GREEN_DARK,
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
            )

        # FAB với position absolute (right + bottom) — đúng pattern Flet, không chặn click
        fab = ft.Container(
            content=ft.FloatingActionButton(
                icon=ft.Icons.ADD,
                bgcolor=GREEN_MID,
                tooltip="Đăng ký tiếp khách",
                on_click=lambda e: self.open_guest_registration_dialog(),
            ),
            right=16, bottom=16,
        )

        if not getattr(self, "_guests_synced", False):
            def bg_sync():
                try:
                    store.refresh_guests()
                except Exception:
                    pass
                self._guests_synced = True
                # Update UI phải gọi trên main thread → page.run_thread
                def _ui_update():
                    if hasattr(self, "_rebuild_guests_module"):
                        self._rebuild_guests_module()
                    else:
                        self.refresh()
                try:
                    if hasattr(self.page, "run_thread"):
                        self.page.run_thread(_ui_update)
                    else:
                        _ui_update()
                except Exception:
                    pass
            threading.Thread(target=bg_sync, daemon=True).start()

        col_children: list[ft.Control] = [view_toggle]
        if bulk_bar is not None:
            col_children.append(bulk_bar)
        col_children.append(list_view)
        main_col = ft.Column(col_children, spacing=0, expand=True)

        # FAB là child riêng của Stack, position absolute → KHÔNG chặn click
        return ft.Stack([main_col, fab], expand=True)

    def _guest_stats_view(self) -> ft.Control:
        """Tab Thống kê — chỉ huy nắm tình hình tiếp khách của cấp dưới."""
        import datetime
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_uid = str(my_profile.get("id") or AUTH_STATE.get("localId") or "")
        my_unit = my_profile.get("unitId") or ""
        my_admin = int(my_profile.get("adminLevel") or 1)
        is_super = bool(my_profile.get("isAdmin"))

        soldiers_all = store.get("soldiers", store.seed_soldiers)
        units_data = store.get("units", store.seed_units)
        all_guests = list(store.get("guests", []) or [])

        # Bản đồ parent + tên đơn vị + xác định subtree
        parent_map: dict[str, str] = {}
        name_map: dict[str, str] = {}
        node_by_id: dict[str, dict] = {}
        def walk(n, p=None):
            if isinstance(n, dict):
                nid = n.get("id")
                if nid:
                    parent_map[nid] = p
                    name_map[nid] = n.get("name") or ""
                    node_by_id[nid] = n
                for c in n.get("children") or []:
                    walk(c, nid)
        walk(units_data)

        def descendants_of(uid: str) -> set[str]:
            out = {uid}
            stack = [uid]
            while stack:
                cur = stack.pop()
                for child_id, par in parent_map.items():
                    if par == cur and child_id not in out:
                        out.add(child_id)
                        stack.append(child_id)
            return out

        # Phạm vi đơn vị cấp dưới
        is_regiment_staff = my_unit in ("u-bch", "u-tm", "u-ct", "u-hk") or my_admin >= 3 or self._is_top_commander(my_uid)
        if is_super or is_regiment_staff:
            scope_unit_ids = set(parent_map.keys())
        else:
            scope_unit_ids = descendants_of(my_unit) if my_unit else set()
        scope_soldiers = [s for s in soldiers_all
                          if not s.get("isAdmin") and (s.get("unitId") in scope_unit_ids)]
        scope_soldier_ids = {str(s.get("id")) for s in scope_soldiers}

        # Lọc guest trong ngày hôm nay
        today = datetime.date.today()
        day_start = int(datetime.datetime.combine(today, datetime.time.min).timestamp() * 1000)
        day_end = int(datetime.datetime.combine(today, datetime.time.max).timestamp() * 1000)

        def guest_in_today(g):
            ms = g.get("arrivalTimeMs") or g.get("visitDateMs") or g.get("createdAt") or 0
            return day_start <= ms <= day_end

        ACTIVE_STATUSES = ("pending", "received", "forwarded", "approved", "checked_in")
        today_guests = [g for g in all_guests
                        if str(g.get("soldierId")) in scope_soldier_ids
                        and guest_in_today(g)
                        and g.get("status") in ACTIVE_STATUSES + ("completed", "checked_out")]

        # Thống kê tổng
        unique_hosts = {str(g.get("soldierId")) for g in today_guests}
        total_guests_count = sum(int(g.get("guestCount") or 1) for g in today_guests)
        total_visits = len(today_guests)
        total_motorbike = sum(int(g.get("vehicleCount") or 1) for g in today_guests if (g.get("vehicle") or "") == "motorbike")
        total_car = sum(int(g.get("vehicleCount") or 1) for g in today_guests if (g.get("vehicle") or "") == "car")
        total_walk = sum(1 for g in today_guests if (g.get("vehicle") or "walk") == "walk")

        # Stat cards
        def _stat_card(icon, label, value, color):
            return ft.Container(
                content=ft.Column([
                    ft.Text(icon, size=24),
                    ft.Text(str(value), size=22, weight=ft.FontWeight.BOLD, color=color),
                    ft.Text(label, size=10, color=TEXT_MUTED, text_align=ft.TextAlign.CENTER),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
                bgcolor=BG, border_radius=10, padding=12,
                border=ft.border.all(1, BORDER),
                expand=True,
            )

        stat_row1 = ft.Row([
            _stat_card("👥", "QN có khách", f"{len(unique_hosts)}/{len(scope_soldiers)}", GREEN_DARK),
            _stat_card("🤝", "Lượt thăm", total_visits, ft.Colors.BLUE),
            _stat_card("👤", "Tổng khách", total_guests_count, RED),
        ], spacing=8)
        stat_row2 = ft.Row([
            _stat_card("🚶", "Đi bộ", total_walk, ft.Colors.GREY),
            _stat_card("🏍", "Xe máy", total_motorbike, AMBER),
            _stat_card("🚗", "Ô tô", total_car, GREEN_MID),
        ], spacing=8)

        # Danh sách: đơn vị nào — quân nhân nào tiếp khách
        # Gom theo đơn vị (canonical order)
        TOP_ORDER = ["u-bch","u-tm","u-ct","u-hk","u-d7","u-d8","u-d9",
                     "u-c14","u-c15","u-c16","u-c17","u-c18","u-c20","u-c24","u-c25"]
        unit_order: dict[str, int] = {}
        _c = [0]
        def _walk2(n):
            if isinstance(n, dict):
                nid = n.get("id") or ""
                if nid:
                    unit_order[nid] = _c[0]; _c[0] += 1
                kids = list(n.get("children") or [])
                if n.get("id") == "root":
                    idx = {u: i for i, u in enumerate(TOP_ORDER)}
                    kids.sort(key=lambda c: idx.get(c.get("id"), len(TOP_ORDER)+1))
                for c in kids:
                    _walk2(c)
        _walk2(units_data)

        soldier_by_id = {str(s.get("id")): s for s in scope_soldiers}
        guests_by_soldier: dict[str, list] = {}
        for g in today_guests:
            sid = str(g.get("soldierId"))
            guests_by_soldier.setdefault(sid, []).append(g)

        # Gom theo đơn vị
        unit_groups: dict[str, list] = {}
        for sid, gs in guests_by_soldier.items():
            s = soldier_by_id.get(sid, {})
            u_id = s.get("unitId") or ""
            unit_groups.setdefault(u_id, []).append((s, gs))
        sorted_uids = sorted(unit_groups.keys(),
                             key=lambda u: (unit_order.get(u, 10**9), name_map.get(u, "")))

        list_rows: list[ft.Control] = []
        list_rows.append(ft.Text("📋 Danh sách quân nhân tiếp khách hôm nay",
                                 size=13, weight=ft.FontWeight.BOLD, color=GREEN_DARK))
        if not sorted_uids:
            list_rows.append(ft.Container(
                content=ft.Text("Hôm nay chưa có ai tiếp khách.",
                                size=12, color=TEXT_MUTED,
                                text_align=ft.TextAlign.CENTER),
                padding=16, alignment=ft.alignment.center,
            ))
        for u_id in sorted_uids:
            u_label = store.canonical_unit_name(node_by_id.get(u_id, {"name": name_map.get(u_id,"")}))
            
            unit_people = 0
            unit_visits = 0
            for s, gs in unit_groups[u_id]:
                unit_visits += len(gs)
                unit_people += sum(int(g.get("guestCount") or 1) for g in gs)

            sub_unit_ids = descendants_of(u_id)
            total_soldiers_in_unit = sum(1 for s in soldiers_all if s.get("unitId") in sub_unit_ids and not s.get("isAdmin"))
            unique_hosts_in_unit = sum(1 for s in soldiers_all if s.get("unitId") in sub_unit_ids and str(s.get("id")) in unique_hosts)

            list_rows.append(ft.Container(
                content=ft.Row([
                    ft.Text(f"{u_label or '(không rõ)'} ({unique_hosts_in_unit}/{total_soldiers_in_unit} QN có khách)",
                            size=11, color=TEXT_MUTED, weight=ft.FontWeight.BOLD, expand=True),
                    ft.Text(f"📊 {unit_visits} lượt / {unit_people} người",
                            size=10, color=TEXT_MUTED, weight=ft.FontWeight.BOLD)
                ]),
                bgcolor=BG2,
                padding=ft.padding.symmetric(horizontal=12, vertical=6),
            ))
            for s, gs in unit_groups[u_id]:
                # Liệt kê các yêu cầu của quân nhân s
                rank = s.get("rank") or ""; nm = s.get("name") or ""
                rows_for_soldier = []
                for g in gs:
                    v_type = g.get("vehicle") or "walk"
                    veh = self.VEHICLE_LABEL.get(v_type, "")
                    if v_type != "walk":
                        v_cnt = int(g.get("vehicleCount") or 1)
                        veh += f" ({v_cnt} chiếc)"
                    st_lbl, _ = self.GUEST_STATUS.get(g.get("status","pending"), (g.get("status",""), ""))
                    cnt = int(g.get("guestCount") or 1)
                    rows_for_soldier.append(
                        ft.Container(
                            content=ft.Row([
                                ft.Text(f"• {g.get('guestName','Khách')} ({cnt} người, {veh})",
                                        size=12, expand=True),
                                ft.Text(st_lbl, size=10, color=TEXT_MUTED),
                            ]),
                            padding=ft.padding.only(left=20, right=12, top=4, bottom=4),
                            on_click=lambda e, _g=g: self.open_guest_details_dialog(_g),
                            ink=True,
                        )
                    )
                list_rows.append(ft.Container(
                    content=ft.Column([
                        ft.Text(f"  {rank} {nm}",
                                size=12, weight=ft.FontWeight.W_600),
                        *rows_for_soldier,
                    ], spacing=2),
                    padding=ft.padding.symmetric(vertical=4),
                ))

        # Vehicle Density PieChart
        pie_sections = []
        if total_walk == 0 and total_motorbike == 0 and total_car == 0:
            pie_sections.append(
                ft.PieChartSection(
                    value=1,
                    title="Chưa có phương tiện",
                    color=ft.Colors.GREY_400,
                    radius=30,
                    title_style=ft.TextStyle(size=10, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                )
            )
        else:
            if total_walk > 0:
                pie_sections.append(
                    ft.PieChartSection(
                        value=total_walk,
                        title=f"Bộ {total_walk}",
                        color=ft.Colors.GREY,
                        radius=30,
                        title_style=ft.TextStyle(size=9, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                    )
                )
            if total_motorbike > 0:
                pie_sections.append(
                    ft.PieChartSection(
                        value=total_motorbike,
                        title=f"Xe máy {total_motorbike}",
                        color=str(AMBER),
                        radius=30,
                        title_style=ft.TextStyle(size=9, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                    )
                )
            if total_car > 0:
                pie_sections.append(
                    ft.PieChartSection(
                        value=total_car,
                        title=f"Ô tô {total_car}",
                        color=str(GREEN_MID),
                        radius=30,
                        title_style=ft.TextStyle(size=9, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                    )
                )

        guest_pie_chart = ft.PieChart(
            sections=pie_sections,
            sections_space=2,
            center_space_radius=25,
            height=110,
        )

        chart_card = ft.Container(
            content=ft.Column([
                ft.Text("MẬT ĐỘ PHƯƠNG TIỆN RA VÀO", size=10, weight=ft.FontWeight.BOLD, color=TEXT_MUTED),
                ft.Container(content=guest_pie_chart, alignment=ft.alignment.center, height=110),
            ], spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=BG, border_radius=10, padding=12,
            border=ft.border.all(1, BORDER),
        )

        body = ft.ListView(
            controls=[
                ft.Container(
                    content=ft.Column([
                        ft.Text(f"📊 Hôm nay — {today.strftime('%d/%m/%Y')}",
                                size=14, weight=ft.FontWeight.BOLD),
                        stat_row1, stat_row2,
                        chart_card,
                    ], spacing=10),
                    padding=12,
                ),
                ft.Divider(height=1),
                ft.Container(
                    content=ft.Column(list_rows, spacing=4),
                    padding=ft.padding.symmetric(horizontal=8, vertical=8),
                ),
            ],
            expand=True, padding=0, spacing=0,
        )
        return body

    def _do_bulk_forward_guests(self, guest_ids: list) -> None:
        """Trình nhiều yêu cầu lên chỉ huy cấp trên (mở dialog chọn chỉ huy)."""
        if not guest_ids:
            return
        page = self.page
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_uid = str(my_profile.get("id") or AUTH_STATE.get("localId") or "")
        my_name = my_profile.get("name") or "Chỉ huy"

        candidates = self._list_higher_commanders(my_uid)
        if not candidates:
            self.toast("⚠️ Bạn đang ở cấp cao nhất — không còn cấp trên để trình")
            return

        options = []
        for unit_label, s in candidates:
            label = f"{s.get('rank','')} {s.get('name','—')} — {s.get('role','')} ({unit_label})".strip()
            options.append(ft.dropdown.Option(str(s.get("id")), label))

        target_dd = ft.Dropdown(
            label="Chọn chỉ huy cấp trên *",
            options=options, value=options[0].key,
            border_radius=8, dense=True,
        )
        fwd_note = ft.TextField(
            label="Ghi chú chung (tuỳ chọn)",
            multiline=True, min_lines=2, max_lines=4,
            border_radius=8, dense=True,
        )
        err = ft.Text("", color=RED, size=12)

        def submit_bulk(_):
            nxt_uid = target_dd.value or ""
            if not nxt_uid:
                err.value = "⚠️ Vui lòng chọn chỉ huy"
                page.update(); return
            nxt = next((s for _u, s in candidates if str(s.get("id")) == nxt_uid), None)
            if not nxt:
                err.value = "⚠️ Không tìm thấy chỉ huy đã chọn"
                page.update(); return
            note_v = (fwd_note.value or "").strip()
            all_guests = list(store.get("guests", []) or [])
            moved = 0
            for g in all_guests:
                if g.get("id") not in guest_ids:
                    continue
                if str(g.get("currentApproverId") or "") != my_uid:
                    continue
                chain = list(g.get("approvalChain") or [])
                chain.append({
                    "approverId": my_uid, "approverName": my_name,
                    "approverRole": my_profile.get("role") or "",
                    "approverUnit": my_profile.get("unitName") or "",
                    "action": "forwarded", "note": note_v, "at": store.now_ms(),
                })
                g["approvalChain"] = chain
                g["status"] = "forwarded"
                g["currentApproverId"] = nxt_uid
                try:
                    FS.set_doc(f"guests/{g.get('id')}", {
                        "status": "forwarded",
                        "currentApproverId": nxt_uid,
                        "approvalChain": chain,
                    })
                except Exception:
                    pass
                try:
                    store.push_notif(
                        "guest",
                        f"Yêu cầu tiếp khách cần duyệt ({g.get('guestName','')})",
                        f"{my_name} trình lên — vui lòng xem & quyết định.",
                        link=f"guest:{g.get('id')}",
                        target_uid=nxt_uid,
                        sender_name=my_name,
                    )
                except Exception:
                    pass
                moved += 1
            store.set_value("guests", all_guests)
            self._guest_selected_ids.clear()
            _dlg.open = False
            self.toast(f"✅ Đã trình {moved} yêu cầu lên {nxt.get('name')}")
            if hasattr(self, "_rebuild_guests_module"):
                self._rebuild_guests_module()
            else:
                self.refresh()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"📤 Trình {len(guest_ids)} yêu cầu lên cấp trên",
                          size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column([target_dd, fwd_note, err], spacing=10, tight=True),
                width=380,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton(
                    "📤 Trình",
                    on_click=submit_bulk,
                    bgcolor=GREEN_DARK, color=ft.Colors.WHITE,
                ),
            ],
        )
        _show_dialog(self.page, _dlg)

    def open_guest_registration_dialog(self) -> None:
        try:
            page = self.page
            my_profile = store.get("userProfile", store.seed_user_profile)
            my_uid = my_profile.get("id") or AUTH_STATE.get("localId")

            # ===== Tìm chỉ huy hợp lệ để trình (KHÔNG bao gồm cơ quan ngang cấp) =====
            # 1. Chỉ huy CÙNG đơn vị có role_priority < mình (cấp cao hơn trong cùng cơ quan)
            # 2. Chỉ huy CẤP TRÊN (theo _list_higher_commanders — đã loại peer cơ quan)
            soldiers = store.get("soldiers", store.seed_soldiers)
            units_data = store.get("units", store.seed_units)
            my_unit = my_profile.get("unitId") or ""
            my_role = my_profile.get("role") or ""
            my_priority = store.role_priority(my_role)

            # Build parent_map + name_map + node_by_id
            unit_map: dict[str, str | None] = {}
            unit_name_map: dict[str, str] = {}
            node_by_id: dict[str, dict] = {}
            def traverse(node, parent_id=None):
                if isinstance(node, dict):
                    n_id = node.get("id")
                    if n_id:
                        unit_map[n_id] = parent_id
                        unit_name_map[n_id] = node.get("name") or ""
                        node_by_id[n_id] = node
                    for child in node.get("children", []):
                        traverse(child, n_id)
            traverse(units_data)

            def _ancestors_of(uid: str) -> list[str]:
                chain = []
                cur = uid
                while cur:
                    chain.append(cur)
                    cur = unit_map.get(cur)
                return chain
            my_ancestors = set(_ancestors_of(my_unit))

            # Gom commanders: cùng đơn vị (priority cao hơn) + cấp trên hợp lệ
            CMD_KEYS = ("trưởng", "chủ nhiệm", "chính uỷ", "chính ủy")
            EXCLUDE_KEYS = ("trợ lý", "nhân viên", "chiến sĩ", "thủ kho")

            same_unit_supers = []
            for s in soldiers:
                if str(s.get("id")) == str(my_uid) or s.get("isAdmin"):
                    continue
                s_uid = s.get("unitId") or ""
                # Phải cùng cơ quan/đại đội với mình (s_uid là ancestor của my_unit, hoặc bằng my_unit)
                if s_uid not in my_ancestors and s_uid != my_unit:
                    continue
                role = (s.get("role") or "").lower()
                if not any(k in role for k in CMD_KEYS):
                    continue
                if any(k in role for k in EXCLUDE_KEYS):
                    continue
                pri = store.role_priority(s.get("role") or "")
                if pri >= my_priority:
                    continue  # cùng cấp hoặc thấp hơn — không phải chỉ huy trên mình
                same_unit_supers.append((unit_name_map.get(s_uid, ""), s))

            # Cấp trên hợp lệ (đã loại peer cơ quan, dùng helper canonical)
            higher = self._list_higher_commanders(str(my_uid))

            # Gộp + dedupe theo id
            commanders: list[tuple] = []
            seen = set()
            for u_lbl, s in same_unit_supers + higher:
                sid = str(s.get("id"))
                if sid in seen:
                    continue
                seen.add(sid)
                commanders.append((u_lbl, s))

            # Sort theo role priority (chỉ huy trực tiếp gần mình nhất hiện trước)
            commanders.sort(key=lambda t: (
                store.role_priority(t[1].get("role") or ""),
                -int(t[1].get("adminLevel") or 0),
                t[1].get("name") or "",
            ))

            cmd_options = []
            for u_lbl, c in commanders:
                u_disp = store.canonical_unit_name({"id": c.get("unitId",""), "name": u_lbl}) or u_lbl
                rank_str = (c.get('rank') or '').strip()
                rank_prefix = f"{rank_str} " if rank_str else ""
                label = f"đ.c {rank_prefix}{c.get('name','')} — {c.get('role','')} ({u_disp})".strip()
                cmd_options.append(ft.dropdown.Option(str(c.get("id")), label))

            if not cmd_options:
                cmd_options = [ft.dropdown.Option("auto", "Hệ thống (Chưa có Chỉ huy)")]

            approver_dropdown = ft.Dropdown(
                label="Trình phê duyệt (Chọn Chỉ huy)",
                dense=True, border_radius=8,
                options=cmd_options,
            )
            # Mặc định chọn người đầu tiên (chỉ huy trực tiếp nhất theo role priority)
            approver_dropdown.value = cmd_options[0].key

            REL_LABELS = ["Bố", "Mẹ", "Vợ", "Chồng", "Con", "Anh trai", "Chị gái", "Em trai", "Em gái", "Ông", "Bà", "Bạn bè", "Khác"]
            def _make_rel_options():
                return [ft.dropdown.Option(x) for x in REL_LABELS]
            
            name_input = ft.TextField(label="Họ và tên trưởng đoàn *", dense=True, border_radius=8)
            id_input = ft.TextField(label="Số CCCD *", dense=True, border_radius=8)
            rel_input = ft.Dropdown(label="Quan hệ với quân nhân *", dense=True, border_radius=8, options=_make_rel_options())
            
            import datetime
            now = datetime.datetime.now()
            date_str = now.strftime("%d/%m/%Y")
            time_str = now.strftime("%H:00")
            
            arr_date = ft.TextField(label="Ngày đến (DD/MM/YYYY)*", dense=True, border_radius=8, value=date_str, expand=True)
            arr_time = ft.TextField(label="Giờ đến*", dense=True, border_radius=8, value=time_str, width=80)
            
            dep_date = ft.TextField(label="Ngày về (DD/MM/YYYY)", dense=True, border_radius=8, value=date_str, expand=True)
            dep_time = ft.TextField(label="Giờ về", dense=True, border_radius=8, value="17:00", width=80)
            
            guest_count = ft.Dropdown(
                label="Tổng số khách (bao gồm trưởng đoàn)", 
                dense=True, border_radius=8, 
                options=[ft.dropdown.Option(str(i)) for i in range(1, 11)],
                value="1"
            )
            
            members_col = ft.Column(spacing=10, tight=True)
            member_inputs = []
            
            def on_count_change(e):
                members_col.controls.clear()
                member_inputs.clear()
                try:
                    count = int(guest_count.value)
                except:
                    count = 1
                    
                for i in range(1, count):
                    m_name = ft.TextField(label=f"Họ tên thành viên {i+1}", dense=True, border_radius=8, expand=True)
                    m_rel = ft.Dropdown(label="Quan hệ", dense=True, border_radius=8, options=_make_rel_options(), width=120)
                    member_inputs.append((m_name, m_rel))
                    members_col.controls.append(ft.Row([m_name, m_rel]))
                
                page.update()
                
            guest_count.on_change = on_count_change

            vehicle_dd = ft.Dropdown(
                label="Phương tiện đi lại *",
                value="walk",
                options=[ft.dropdown.Option(k, lbl) for k, lbl in self.VEHICLE_OPTIONS],
                border_radius=8, dense=True,
            )
            vehicle_count_dd = ft.Dropdown(
                label="Số lượng xe",
                value="1",
                options=[ft.dropdown.Option(str(i)) for i in range(1, 6)],
                border_radius=8, dense=True,
                visible=False,
            )
            def on_vehicle_change(e):
                vehicle_count_dd.visible = (vehicle_dd.value != "walk")
                page.update()
            vehicle_dd.on_change = on_vehicle_change

            note_input = ft.TextField(label="Ghi chú thêm", dense=True, border_radius=8, multiline=True, min_lines=2)

            error_text = ft.Text("", color=ft.Colors.RED, size=13, weight=ft.FontWeight.W_600)

            def do_save(_):
                try:
                    name = (name_input.value or "").strip()
                    id_num = (id_input.value or "").strip()
                    rel = rel_input.value
                    
                    a_date = (arr_date.value or "").strip()
                    a_time = (arr_time.value or "").strip()
                    d_date = (dep_date.value or "").strip()
                    d_time = (dep_time.value or "").strip()
                    
                    if not name or not id_num or not rel or not a_date or not a_time:
                        error_text.value = "❌ Vui lòng điền đủ thông tin bắt buộc (*)"
                        page.update()
                        self.toast("Vui lòng điền đủ thông tin trưởng đoàn và thời gian đến")
                        return
                        
                    if cmd_options and not approver_dropdown.value:
                        error_text.value = "❌ Vui lòng chọn Trình phê duyệt"
                        page.update()
                        self.toast("Vui lòng chọn người duyệt")
                        return
                        
                    try:
                        a_dt = datetime.datetime.strptime(f"{a_date} {a_time}", "%d/%m/%Y %H:%M")
                        arr_ms = int(a_dt.timestamp() * 1000)
                    except Exception as e:
                        print("Date parsing error:", e)
                        error_text.value = "❌ Ngày giờ đến không đúng định dạng DD/MM/YYYY HH:MM"
                        page.update()
                        self.toast("Ngày giờ đến không đúng định dạng DD/MM/YYYY HH:MM")
                        return
                        
                    dep_ms = 0
                    if d_date and d_time:
                        try:
                            d_dt = datetime.datetime.strptime(f"{d_date} {d_time}", "%d/%m/%Y %H:%M")
                            dep_ms = int(d_dt.timestamp() * 1000)
                        except Exception:
                            pass

                    members_data = []
                    for m_name_input, m_rel_input in member_inputs:
                        m_n = (m_name_input.value or "").strip()
                        if m_n:
                            members_data.append({"name": m_n, "relationship": m_rel_input.value or ""})
                    
                    import uuid
                    guest_id = uuid.uuid4().hex
                    
                    appr_id = approver_dropdown.value
                    if appr_id == "auto":
                        appr_id = my_uid

                    doc_data = {
                        "id": guest_id,
                        "soldierId": my_uid,
                        "guestName": name,
                        "guestIdCard": id_num,
                        "relationship": rel,
                        "arrivalTimeMs": arr_ms,
                        "departureTimeMs": dep_ms,
                        "guestCount": int(guest_count.value or 1),
                        "members": members_data,
                        "vehicle": (vehicle_dd.value or "walk"),
                        "vehicleCount": int(vehicle_count_dd.value or 1) if vehicle_dd.value != "walk" else 1,
                        "status": "pending",
                        "notes": (note_input.value or "").strip(),
                        "approverId": appr_id,
                        "currentApproverId": appr_id,
                        "approvalChain": [],
                        "feedback": "",
                        "createdAt": store.now_ms(),
                        "createdBy": my_uid
                    }

                    FS.set_doc(f"guests/{guest_id}", doc_data)
                    # Đẩy notification cho chỉ huy được trình
                    try:
                        my_name = my_profile.get("name") or "Đồng chí"
                        my_rank = (my_profile.get("rank") or "").strip()
                        my_rank_prefix = f"{my_rank} " if my_rank else ""
                        push_name = f"đ.c {my_rank_prefix}{my_name}"
                        store.push_notif(
                            "guest",
                            "Yêu cầu tiếp khách cần duyệt",
                            f"{push_name} đăng ký tiếp khách {name} — cần bạn xem & duyệt.",
                            link=f"guest:{guest_id}",
                            target_uid=appr_id,
                            sender_name=push_name,
                        )
                    except Exception:
                        pass
                    self.toast("✅ Đã trình chỉ huy duyệt")
                    store.refresh_guests()
                    _dlg.open = False
                    if hasattr(self, "_rebuild_guests_module"):
                        self._rebuild_guests_module()
                    else:
                        self.refresh()
                except Exception as e:
                    print("Lỗi khi lưu đăng ký khách:", e)
                    error_text.value = f"❌ Lỗi: {e}"
                    page.update()
                    self.toast(f"❌ Lỗi: {e}")

            dlg_content = ft.ListView(
                [
                    error_text,
                    ft.Text("Thông tin Trưởng đoàn", weight=ft.FontWeight.BOLD),
                    name_input, id_input, rel_input,
                    ft.Divider(height=1),
                    ft.Text("Trình phê duyệt", weight=ft.FontWeight.BOLD),
                    approver_dropdown,
                    ft.Divider(height=1),
                    ft.Text("Thời gian", weight=ft.FontWeight.BOLD),
                    ft.Row([arr_date, arr_time]),
                    ft.Row([dep_date, dep_time]),
                    ft.Divider(height=1),
                    ft.Text("Thành viên & phương tiện", weight=ft.FontWeight.BOLD),
                    guest_count,
                    members_col,
                    vehicle_dd,
                    vehicle_count_dd,
                    ft.Divider(height=1),
                    note_input
                ],
                spacing=10,
                height=450,
            )

            _dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("Đăng ký tiếp khách", size=16, weight=ft.FontWeight.BOLD),
                content=ft.Container(content=dlg_content, width=350),
                actions=[
                    ft.TextButton("Hủy", on_click=lambda e: _close_dialog(self.page)),
                    ft.ElevatedButton("Đăng ký", on_click=do_save, bgcolor=GREEN_MID, color=ft.Colors.WHITE)
                ],
            )
            _show_dialog(self.page, _dlg)
        except Exception as e:
            print("Lỗi mở form đăng ký:", e)
            self.toast(f"Lỗi hệ thống: {e}")

    def open_guest_details_dialog(self, g: dict) -> None:
        page = self.page
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_admin = int(my_profile.get("adminLevel") or 1)
        my_uid = str(my_profile.get("id") or AUTH_STATE.get("localId") or "")
        is_commander = (my_admin >= 2) or self._is_top_commander(my_uid) or ((my_profile.get("unitId") or "").startswith("u-bch")) or (my_profile.get("unitId") in ("u-bch", "u-tm", "u-ct", "u-hk"))

        st = g.get("status", "pending")
        cur_appr = str(g.get("currentApproverId") or g.get("approverId") or "")
        is_current_approver = (cur_appr == my_uid)

        soldiers = store.get("soldiers", store.seed_soldiers)
        soldier = next((s for s in soldiers if str(s.get("id")) == str(g.get("soldierId"))), {})
        rank_str = (soldier.get('rank') or '').strip()
        rank_prefix = f"{rank_str} " if rank_str else ""
        soldier_name = f"{rank_prefix}{soldier.get('name') or 'Không rõ'}"
        soldier_unit = soldier.get("unitName") or ""

        import datetime
        arr_str = "Chưa rõ"
        if g.get("arrivalTimeMs"):
            dt = datetime.datetime.fromtimestamp(g.get("arrivalTimeMs") / 1000)
            arr_str = dt.strftime("%H:%M %d/%m/%Y")
        dep_str = "Chưa rõ"
        if g.get("departureTimeMs"):
            dt = datetime.datetime.fromtimestamp(g.get("departureTimeMs") / 1000)
            dep_str = dt.strftime("%H:%M %d/%m/%Y")
        if not g.get("arrivalTimeMs") and g.get("visitDateMs"):
            dt = datetime.datetime.fromtimestamp(g.get("visitDateMs") / 1000)
            arr_str = dt.strftime("%d/%m/%Y")

        members_list = g.get("members", [])
        members_ui = []
        if members_list:
            members_ui.append(ft.Text("Danh sách đi cùng:", weight=ft.FontWeight.BOLD, size=13, color=GREEN_DARK))
            for m in members_list:
                members_ui.append(ft.Text(f"- {m.get('name')} ({m.get('relationship')})", size=13))

        # Lịch sử phê duyệt
        chain = g.get("approvalChain") or []
        chain_ui: list[ft.Control] = []
        if chain:
            chain_ui.append(ft.Divider(height=1))
            chain_ui.append(ft.Text("Lịch sử duyệt:", weight=ft.FontWeight.BOLD, size=13, color=GREEN_DARK))
            ACTION_ICON = {
                "received": "📥",
                "forwarded": "📤",
                "review_requested": "✏️",
                "rejected": "❌",
                "approved": "✅",
            }
            for entry in chain:
                t = datetime.datetime.fromtimestamp((entry.get("at") or 0)/1000).strftime("%d/%m %H:%M")
                ic = ACTION_ICON.get(entry.get("action") or "", "•")
                line = f"{ic} {t} — {entry.get('approverRole','')} đ.c {entry.get('approverName','')}"
                if entry.get("note"):
                    line += f"\n   📝 {entry['note']}"
                chain_ui.append(ft.Text(line, size=11, color=TEXT_MUTED))

        if g.get("feedback"):
            chain_ui.append(ft.Container(
                content=ft.Text(f"💬 Phản hồi chỉ huy: {g['feedback']}", size=12, color=AMBER),
                bgcolor="#fff8e1", border_radius=6, padding=8,
            ))

        def _close():
            _dlg.open = False
            if hasattr(self, "_rebuild_guests_module"):
                self._rebuild_guests_module()
            else:
                self.refresh()

        def _persist(updates: dict):
            try:
                FS.set_doc(f"guests/{g.get('id')}", updates)
            except Exception:
                pass
            # Cập nhật local store
            all_g = list(store.get("guests", []) or [])
            for i, x in enumerate(all_g):
                if x.get("id") == g.get("id"):
                    all_g[i] = {**x, **updates}
                    break
            store.set_value("guests", all_g)

        def _append_chain_local(action: str, note: str = "") -> list:
            chain_l = list(g.get("approvalChain") or [])
            chain_l.append({
                "approverId": my_uid,
                "approverName": my_profile.get("name") or "",
                "approverRole": my_profile.get("role") or "",
                "approverUnit": my_profile.get("unitName") or "",
                "action": action, "note": note or "", "at": store.now_ms(),
            })
            return chain_l

        def do_reply_nudge(reply_type):
            if reply_type == "left":
                new_chain = _append_chain_local("reply_left", "Báo cáo phản hồi: Khách đã về")
                _persist({
                    "nudgeStatus": "replied_left",
                    "status": "completed",
                    "departureTimeMs": store.now_ms(),
                    "approvalChain": new_chain
                })
                target = g.get("nudgeSenderId") or g.get("currentApproverId") or g.get("approverId")
                if target:
                    try:
                        my_rank = (my_profile.get("rank") or "").strip()
                        my_rank_prefix = f"{my_rank} " if my_rank else ""
                        push_name = f"đ.c {my_rank_prefix}{my_profile.get('name','Cấp dưới')}"
                        store.push_notif("guest",
                                         f"Phản hồi: Khách đã về ({g.get('guestName','')})",
                                         f"{push_name} báo cáo: Khách đã về, đơn đã hoàn thành.",
                                         link=f"guest:{g.get('id')}",
                                         target_uid=str(target),
                                         sender_name=push_name)
                    except Exception:
                        pass
                self.toast("✅ Đã báo cáo khách đã về!")
            else:
                new_chain = _append_chain_local("reply_still_here", "Báo cáo phản hồi: Khách vẫn đang ở đơn vị")
                _persist({
                    "nudgeStatus": "replied_still_here",
                    "approvalChain": new_chain
                })
                target = g.get("nudgeSenderId") or g.get("currentApproverId") or g.get("approverId")
                if target:
                    try:
                        my_rank = (my_profile.get("rank") or "").strip()
                        my_rank_prefix = f"{my_rank} " if my_rank else ""
                        push_name = f"đ.c {my_rank_prefix}{my_profile.get('name','Cấp dưới')}"
                        store.push_notif("guest",
                                         f"Phản hồi: Khách chưa về ({g.get('guestName','')})",
                                         f"{push_name} báo cáo: Khách vẫn đang ở đơn vị.",
                                         link=f"guest:{g.get('id')}",
                                         target_uid=str(target),
                                         sender_name=push_name)
                    except Exception:
                        pass
                self.toast("✅ Đã phản hồi khách chưa về")
            _close()

        nudge_banner = None
        if g.get("nudgeStatus") == "asked":
            sender_name = g.get("nudgeSenderName") or "Chỉ huy cấp trên"
            nudge_banner = ft.Container(
                content=ft.Column([
                    ft.Text(f"⚠️ {sender_name} nhắc nhở hỏi: Khách đã về chưa?", weight=ft.FontWeight.BOLD, size=11, color=RED),
                    ft.Row([
                        ft.ElevatedButton("🏁 Đã về", on_click=lambda e: do_reply_nudge("left"), bgcolor=GREEN_MID, color=ft.Colors.WHITE, height=26, style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=8))),
                        ft.ElevatedButton("Chưa về", on_click=lambda e: do_reply_nudge("still_here"), bgcolor=AMBER, color=ft.Colors.WHITE, height=26, style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=8)))
                    ], spacing=6)
                ], spacing=4),
                bgcolor="#ffebee", border_radius=8, padding=10, border=ft.border.all(1, RED)
            )

        v_type = g.get("vehicle") or "walk"
        vehicle_label = self.VEHICLE_LABEL.get(v_type, "🚶 Đi bộ")
        if v_type != "walk":
            v_cnt = int(g.get("vehicleCount") or 1)
            vehicle_label += f" ({v_cnt} chiếc)"
        
        info_col_items = []
        if nudge_banner:
            info_col_items.append(nudge_banner)
        info_col_items += [
            ft.Text(f"Trưởng đoàn: {g.get('guestName')}", size=16, weight=ft.FontWeight.BOLD),
            ft.Text(f"Số CCCD: {g.get('guestIdCard')}", size=13),
            ft.Text(f"Quan hệ: {g.get('relationship')} của đ.c {soldier_name}", size=13),
            ft.Text(f"Đơn vị: {soldier_unit}", size=13, color=TEXT_MUTED),
            ft.Text(f"Số lượng khách: {g.get('guestCount', 1)} người", size=13,
                    weight=ft.FontWeight.BOLD, color=RED),
            ft.Text(f"Phương tiện: {vehicle_label}", size=13),
            ft.Text(f"Dự kiến đến: {arr_str}", size=13),
            ft.Text(f"Dự kiến về: {dep_str}", size=13),
            ft.Text(f"Ghi chú: {g.get('notes') or 'Không'}", size=13, italic=True),
        ] + members_ui + chain_ui

        info_col = ft.ListView(info_col_items, spacing=5, height=400)

        # --- Action: Đã nhận ---
        def do_received(_):
            new_chain = _append_chain_local("received")
            _persist({"status": "received", "approvalChain": new_chain})
            try:
                my_rank = (my_profile.get("rank") or "").strip()
                my_rank_prefix = f"{my_rank} " if my_rank else ""
                push_name = f"đ.c {my_rank_prefix}{my_profile.get('name','Chỉ huy')}"
                store.push_notif("guest",
                                 f"Chỉ huy đã xem yêu cầu '{g.get('guestName')}'",
                                 f"{push_name} đã nhận yêu cầu của bạn.",
                                 link=f"guest:{g.get('id')}",
                                 target_uid=str(g.get("soldierId") or ""),
                                 sender_name=push_name)
            except Exception:
                pass
            self.toast("✅ Đã nhận yêu cầu")
            _close()

        # --- Action: Trình cấp trên (hiện picker để chọn chỉ huy hợp lệ) ---
        def do_forward(_):
            candidates = self._list_higher_commanders(my_uid)
            if not candidates:
                self.toast("⚠️ Bạn là cấp cao nhất — hãy chọn 'Đồng ý' để duyệt cuối")
                return

            options = []
            for unit_label, s in candidates:
                rank_str = (s.get('rank') or '').strip()
                rank_prefix = f"{rank_str} " if rank_str else ""
                label = f"đ.c {rank_prefix}{s.get('name','—')} — {s.get('role','')} ({unit_label})".strip()
                options.append(ft.dropdown.Option(str(s.get("id")), label))

            target_dd = ft.Dropdown(
                label="Chọn chỉ huy cấp trên *",
                options=options, value=options[0].key,
                border_radius=8, dense=True,
            )
            fwd_note = ft.TextField(
                label="Ghi chú (tuỳ chọn)",
                multiline=True, min_lines=2, max_lines=4,
                border_radius=8, dense=True,
            )
            err = ft.Text("", color=RED, size=12)

            def submit_fwd(_):
                nxt_uid = target_dd.value or ""
                if not nxt_uid:
                    err.value = "⚠️ Vui lòng chọn chỉ huy để trình"
                    page.update(); return
                nxt = next((s for _u, s in candidates if str(s.get("id")) == nxt_uid), None)
                if not nxt:
                    err.value = "⚠️ Không tìm thấy chỉ huy đã chọn"
                    page.update(); return
                note_v = (fwd_note.value or "").strip()
                new_chain = _append_chain_local("forwarded", note_v)
                _persist({
                    "status": "forwarded",
                    "currentApproverId": nxt_uid,
                    "approvalChain": new_chain,
                })
                try:
                    my_rank = (my_profile.get("rank") or "").strip()
                    my_rank_prefix = f"{my_rank} " if my_rank else ""
                    push_name = f"đ.c {my_rank_prefix}{my_profile.get('name','Chỉ huy')}"
                    store.push_notif(
                        "guest",
                        f"Yêu cầu tiếp khách cần duyệt ({g.get('guestName','')})",
                        f"{push_name} trình lên — vui lòng xem & quyết định.",
                        link=f"guest:{g.get('id')}",
                        target_uid=nxt_uid,
                        sender_name=push_name,
                    )
                except Exception:
                    pass
                self.toast(f"✅ Đã trình lên {nxt.get('name')}")
                _close()

            _dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("📤 Trình lên cấp trên", size=15, weight=ft.FontWeight.BOLD),
                content=ft.Container(
                    content=ft.Column([target_dd, fwd_note, err], spacing=10, tight=True),
                    width=380,
                ),
                actions=[
                    ft.TextButton("Huỷ", on_click=lambda e: self.open_guest_details_dialog(g)),
                    ft.ElevatedButton("📤 Trình", on_click=submit_fwd,
                                      bgcolor=GREEN_DARK, color=ft.Colors.WHITE),
                ],
            )
            _show_dialog(self.page, _dlg)

        # --- Action: Gửi kiểm tra lại (yêu cầu note) ---
        def do_review_request(_):
            note_tf = ft.TextField(label="Nội dung cần kiểm tra lại *",
                                   multiline=True, min_lines=3, max_lines=5,
                                   border_radius=8, dense=True, autofocus=True)
            err = ft.Text("", color=RED, size=12)

            def submit(_):
                n = (note_tf.value or "").strip()
                if not n:
                    err.value = "⚠️ Bắt buộc nhập nội dung"
                    page.update(); return
                new_chain = _append_chain_local("review_requested", n)
                _persist({
                    "status": "review_requested",
                    "feedback": n,
                    "currentApproverId": str(g.get("soldierId") or ""),
                    "approvalChain": new_chain,
                })
                try:
                    my_rank = (my_profile.get("rank") or "").strip()
                    my_rank_prefix = f"{my_rank} " if my_rank else ""
                    push_name = f"đ.c {my_rank_prefix}{my_profile.get('name','Chỉ huy')}"
                    store.push_notif("guest",
                                     "Yêu cầu tiếp khách cần kiểm tra lại",
                                     f"{push_name} có ghi chú — vui lòng xem.",
                                     link=f"guest:{g.get('id')}",
                                     target_uid=str(g.get("soldierId") or ""),
                                     sender_name=push_name)
                except Exception:
                    pass
                self.toast("✏️ Đã gửi lại để kiểm tra")
                _close()

            _dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("Gửi kiểm tra lại", size=15, weight=ft.FontWeight.BOLD),
                content=ft.Container(
                    content=ft.Column([note_tf, err], spacing=8, tight=True),
                    width=340,
                ),
                actions=[
                    ft.TextButton("Huỷ", on_click=lambda e: self.open_guest_details_dialog(g)),
                    ft.ElevatedButton("Gửi lại", on_click=submit, bgcolor=AMBER, color=ft.Colors.WHITE),
                ],
            )
            _show_dialog(self.page, _dlg)

        # --- Action: Từ chối (yêu cầu lý do) ---
        def do_reject(_):
            note_tf = ft.TextField(label="Lý do từ chối *",
                                   multiline=True, min_lines=2, max_lines=4,
                                   border_radius=8, dense=True, autofocus=True)
            err = ft.Text("", color=RED, size=12)

            def submit(_):
                n = (note_tf.value or "").strip()
                if not n:
                    err.value = "⚠️ Bắt buộc nhập lý do"
                    page.update(); return
                new_chain = _append_chain_local("rejected", n)
                _persist({
                    "status": "rejected",
                    "feedback": n,
                    "approvalChain": new_chain,
                })
                try:
                    my_rank = (my_profile.get("rank") or "").strip()
                    my_rank_prefix = f"{my_rank} " if my_rank else ""
                    push_name = f"đ.c {my_rank_prefix}{my_profile.get('name','Chỉ huy')}"
                    store.push_notif("guest",
                                     "Yêu cầu tiếp khách bị từ chối",
                                     f"{push_name} từ chối — Lý do: {n}",
                                     link=f"guest:{g.get('id')}",
                                     target_uid=str(g.get("soldierId") or ""),
                                     sender_name=push_name)
                except Exception:
                    pass
                self.toast("❌ Đã từ chối")
                _close()

            _dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("Từ chối yêu cầu", size=15, weight=ft.FontWeight.BOLD),
                content=ft.Container(
                    content=ft.Column([note_tf, err], spacing=8, tight=True),
                    width=340,
                ),
                actions=[
                    ft.TextButton("Huỷ", on_click=lambda e: self.open_guest_details_dialog(g)),
                    ft.ElevatedButton("Từ chối", on_click=submit, bgcolor=RED, color=ft.Colors.WHITE),
                ],
            )
            _show_dialog(self.page, _dlg)

        # --- Action: Đồng ý (chỉ ở cấp cao nhất hoặc khi chỉ huy quyết định duyệt thẳng) ---
        def do_approve(_):
            new_chain = _append_chain_local("approved")
            _persist({"status": "approved", "approvalChain": new_chain})
            try:
                my_rank = (my_profile.get("rank") or "").strip()
                my_rank_prefix = f"{my_rank} " if my_rank else ""
                push_name = f"đ.c {my_rank_prefix}{my_profile.get('name','Chỉ huy')}"
                store.push_notif("guest",
                                 f"Yêu cầu tiếp khách đã được duyệt",
                                 f"{push_name} đã đồng ý — khách có thể đến.",
                                 link=f"guest:{g.get('id')}",
                                 target_uid=str(g.get("soldierId") or ""),
                                 sender_name=push_name)
            except Exception:
                pass
            self.toast("✅ Đã duyệt yêu cầu")
            _close()

        def do_nudge(_):
            new_chain = _append_chain_local("nudge", "Gửi câu hỏi nhắc nhở: Khách đã về chưa?")
            _persist({
                "nudgeStatus": "asked",
                "nudgeSenderId": my_uid,
                "nudgeSenderName": my_profile.get("name") or "Chỉ huy",
                "approvalChain": new_chain
            })
            try:
                my_rank = (my_profile.get("rank") or "").strip()
                my_rank_prefix = f"{my_rank} " if my_rank else ""
                push_name = f"đ.c {my_rank_prefix}{my_profile.get('name','Chỉ huy')}"
                store.push_notif("guest",
                                 f"Chỉ huy hỏi: Khách đã về chưa? ({g.get('guestName','')})",
                                 f"{push_name} hỏi: Khách của đ.c đã về chưa? Vui lòng trả lời.",
                                 link=f"guest:{g.get('id')}",
                                 target_uid=str(g.get("soldierId") or ""),
                                 sender_name=push_name)
            except Exception:
                pass
            self.toast("🔔 Đã gửi nhắc nhở đến cấp dưới!")
            _close()

        # --- Action: Khách vào / ra (sau khi approved) ---
        def do_set_status(new_st):
            _persist({"status": new_st})
            self.toast("✅ Đã cập nhật")
            _close()

        # ===== Xây dựng actions theo role + status =====
        actions: list[ft.Control] = []
        if is_current_approver and st in ("pending", "received", "forwarded"):
            # Chỉ huy đang giữ yêu cầu này
            if st == "pending":
                actions.append(ft.ElevatedButton("✅ Đã nhận", on_click=do_received,
                                                 bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE))
            # Trình cấp trên (nếu có cấp trên)
            actions.append(ft.ElevatedButton("📤 Trình cấp trên", on_click=do_forward,
                                             bgcolor=GREEN_DARK, color=ft.Colors.WHITE))
            # Đồng ý (duyệt cuối, ưu tiên cấp eT/CUY)
            if self._is_top_commander(my_uid):
                actions.append(ft.ElevatedButton("✅ Đồng ý", on_click=do_approve,
                                                 bgcolor=GREEN_MID, color=ft.Colors.WHITE))
            actions.append(ft.ElevatedButton("✏️ Gửi kiểm tra lại", on_click=do_review_request,
                                             bgcolor=AMBER, color=ft.Colors.WHITE))
            actions.append(ft.ElevatedButton("❌ Từ chối", on_click=do_reject,
                                             bgcolor=RED, color=ft.Colors.WHITE))
        elif st == "approved":
            # Quân nhân đăng ký HOẶC chỉ huy có thể đánh dấu khách đã về
            allow_complete = (str(g.get("soldierId")) == my_uid) or is_commander
            if allow_complete:
                actions.append(ft.ElevatedButton(
                    "🚪 Khách vào cổng",
                    on_click=lambda e: do_set_status("checked_in"),
                    bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE,
                ))
                actions.append(ft.ElevatedButton(
                    "🏁 Đã hoàn thành",
                    on_click=lambda e: do_set_status("completed"),
                    bgcolor=ft.Colors.GREY, color=ft.Colors.WHITE,
                    tooltip="Khách đã về — đóng đơn",
                ))
        elif st == "checked_in":
            allow_complete = (str(g.get("soldierId")) == my_uid) or is_commander
            if allow_complete:
                actions.append(ft.ElevatedButton(
                    "🏁 Đã hoàn thành",
                    on_click=lambda e: do_set_status("completed"),
                    bgcolor=ft.Colors.GREY, color=ft.Colors.WHITE,
                    tooltip="Khách đã về — đóng đơn",
                ))

        if is_commander and str(g.get("soldierId")) != my_uid and st in ("approved", "checked_in") and g.get("nudgeStatus") != "asked":
            actions.append(ft.ElevatedButton(
                "🔔 Khách về chưa?",
                on_click=do_nudge,
                bgcolor=AMBER, color=ft.Colors.WHITE,
                tooltip="Gửi nhắc nhở hỏi cấp dưới xem khách đã về chưa"
            ))

        # Cho phép xoá đơn (chỉ huy hoặc người tạo đơn)
        if is_commander or (str(g.get("soldierId")) == my_uid):
            def do_delete(_):
                try:
                    FS.delete_doc(f"guests/{g.get('id')}")
                except Exception:
                    pass
                all_g = list(store.get("guests", []) or [])
                all_g = [x for x in all_g if x.get("id") != g.get("id")]
                store.set_value("guests", all_g)
                self.toast("🗑 Đã xoá yêu cầu thành công")
                _close()

            actions.append(ft.ElevatedButton(
                "🗑 Xoá đơn",
                on_click=do_delete,
                bgcolor=RED, color=ft.Colors.WHITE,
                tooltip="Xoá vĩnh viễn yêu cầu này"
            ))
        elif str(g.get("soldierId")) == my_uid and st in ("pending", "review_requested"):
            # Người đăng ký có thể huỷ
            actions.append(ft.ElevatedButton("🗑 Huỷ đăng ký",
                                             on_click=lambda e: do_set_status("rejected"),
                                             bgcolor=RED, color=ft.Colors.WHITE))

        actions.append(ft.TextButton("Đóng",
                                     on_click=lambda e: _close_dialog(self.page)))

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Chi tiết khách thăm", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Container(content=info_col, width=340),
            actions=actions,
            actions_alignment=ft.MainAxisAlignment.END,
        )
        _show_dialog(self.page, _dlg)

    def module_f47(self) -> ft.Control:
        camps = store.get("f47Campaigns", store.seed_f47) or []
        if not isinstance(camps, list):
            camps = []
        now_ms = store.now_ms()
        camps = sorted(camps, key=lambda c: (
            1 if int(c.get("deadline") or 0) <= now_ms else 0,
            -int(c.get("deadline") or 0)
        ))
        soldiers = store.get("soldiers", store.seed_soldiers)
        soldier_by_id = {str(s.get("id")): s for s in soldiers if s.get("id")}
        current_view = getattr(self, "f47_view", "campaigns")

        def _resolve_creator(c: dict) -> tuple[str, str]:
            """Trả về (tên hiện tại, chức danh hiện tại) của người tạo chiến dịch.
            Ưu tiên dữ liệu soldiers cache (tên đã cập nhật); fallback creator field cũ."""
            uid = str(c.get("createdByUid") or "")
            if uid and uid in soldier_by_id:
                s = soldier_by_id[uid]
                return str(s.get("name") or c.get("creator") or ""), str(s.get("role") or c.get("creatorRole") or "")
            return str(c.get("creator") or ""), str(c.get("creatorRole") or "")

        # ── helpers ──────────────────────────────────────────────
        def cd_str(c: dict) -> str:
            left = int(c.get("deadline") or 0) - store.now_ms()
            if left <= 0:
                return "Hết giờ"
            h, rem = divmod(left, 3600_000)
            m, _ = divmod(rem, 60_000)
            return f"{h:02d}h {m:02d}m"

        def switch_view(key: str):
            self.f47_view = key
            self.body.content = self.module_f47()
            self.refresh()

        # ── tab bar (custom, không dùng ft.Tabs) ─────────────────
        def _tab_btn(label: str, key: str) -> ft.Container:
            active = (current_view == key)
            return ft.Container(
                content=ft.Text(
                    label, size=13,
                    color=RED if active else TEXT_MUTED,
                    weight=ft.FontWeight.BOLD if active else ft.FontWeight.NORMAL,
                ),
                padding=ft.padding.symmetric(horizontal=16, vertical=10),
                border=ft.border.only(
                    bottom=ft.BorderSide(2, RED) if active else ft.BorderSide(0)
                ),
                on_click=lambda e, k=key: switch_view(k),
                ink=True,
            )

        tab_defs = [
            ("Chiến dịch", "campaigns"),
            ("Chia sẻ", "shares"),
            ("Xếp hạng", "leaderboard"),
        ]

        tabs_bar = ft.Container(
            content=ft.Row(
                [_tab_btn(lbl, key) for lbl, key in tab_defs],
                spacing=0,
            ),
            bgcolor=BG,
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            height=44,
        )

        # ── campaign card ─────────────────────────────────────────
        def card(c: dict) -> ft.Control:
            done = len(c.get("submissions") or {})
            total = len(c.get("members") or [])
            pct = int(done / total * 100) if total else 0
            c_type = c.get("campaignType") or "Khác"
            badge_map = {
                "Báo cáo khẩn": ("#ffebee", "#c62828"),
                "Báo cáo":      ("#e3f2fd", "#1565c0"),
                "CMT":          ("#e0f2f1", "#00695c"),
            }
            badge_bg, badge_fg = badge_map.get(c_type, ("#f5f5f5", "#616161"))

            return ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Container(
                            content=ft.Text(c_type, size=10, color=badge_fg,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=badge_bg, border_radius=4,
                            padding=ft.padding.symmetric(horizontal=6, vertical=2),
                        ),
                        ft.Text(c.get("title") or "", size=14,
                                weight=ft.FontWeight.BOLD, expand=True),
                        ft.Container(
                            content=ft.Text(
                                "🔴 Live" if c.get("status") == "live" else "Done",
                                size=10, color=ft.Colors.WHITE,
                                weight=ft.FontWeight.BOLD,
                            ),
                            bgcolor=RED if c.get("status") == "live" else "#999",
                            border_radius=10,
                            padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        ),
                        *([ft.IconButton(
                            ft.Icons.MORE_VERT, icon_size=18,
                            tooltip="Tuỳ chọn",
                            on_click=lambda e, _c=c: self.f47_open_camp_menu(_c),
                        )] if self._is_admin() else []),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),

                    ft.Text(
                        "Phát động bởi đ.c {} – {}".format(*_resolve_creator(c)),
                        size=11, color=TEXT_MUTED,
                    ),
                    ft.Text(c.get("desc") or "", size=12, color=TEXT_MUTED),

                    *([ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.LINK, size=14, color=BLUE),
                            ft.Text(c.get("targetLink",""), size=11, color=BLUE,
                                    overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                            ft.Icon(ft.Icons.OPEN_IN_NEW, size=12, color=BLUE),
                        ], spacing=6),
                        bgcolor="#eff6ff", border_radius=6,
                        padding=ft.padding.symmetric(horizontal=8, vertical=6),
                        on_click=lambda e, url=c.get("targetLink",""): self.page.launch_url(url),
                    )] if c.get("targetLink") else []),

                    ft.Container(
                        content=ft.Row([
                            ft.Text("⏱", size=18),
                            ft.Column([
                                ft.Text("Thời gian còn lại", size=11, color="#633806"),
                                ft.Text(cd_str(c), size=16, color="#ba7517",
                                        weight=ft.FontWeight.BOLD),
                            ], spacing=0, tight=True),
                        ], spacing=10),
                        bgcolor="#fff8e1", border_radius=8, padding=10,
                        border=ft.border.all(1, "#f0c040"),
                    ),

                    ft.Row([
                        ft.Text("Tiến độ", size=11, color=TEXT_MUTED),
                        ft.Text(f"{done}/{total} ({pct}%)", size=11,
                                weight=ft.FontWeight.BOLD,
                                color=GREEN_MID if pct >= 50 else RED),
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.ProgressBar(value=pct/100, color=GREEN_MID, bgcolor="#eee", height=8),

                    ft.Row([
                        ft.Container(
                            content=ft.Column([
                                ft.Text(str(done), size=18, weight=ft.FontWeight.BOLD,
                                        color=GREEN_MID),
                                ft.Text("✅ Đã làm", size=10, color=TEXT_MUTED),
                            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
                            bgcolor=BG2, border_radius=8, padding=8, expand=True,
                            alignment=ft.alignment.center,
                        ),
                        ft.Container(
                            content=ft.Column([
                                ft.Text(str(total - done), size=18,
                                        weight=ft.FontWeight.BOLD, color=RED),
                                ft.Text("❌ Chưa", size=10, color=TEXT_MUTED),
                            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
                            bgcolor=BG2, border_radius=8, padding=8, expand=True,
                            alignment=ft.alignment.center,
                        ),
                    ], spacing=6),

                    *([ft.ElevatedButton(
                        "📤 Nộp minh chứng",
                        on_click=lambda e, _c=c: self.f47_open_submit(_c),
                        bgcolor=GREEN_MID, color=ft.Colors.WHITE,
                        style=ft.ButtonStyle(
                            shape=ft.RoundedRectangleBorder(radius=8),
                            padding=10,
                        ),
                    )] if not self._is_admin() else []),
                ], spacing=8),
                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
                padding=14, margin=ft.margin.only(bottom=10),
                on_click=lambda e, _c=c: self.f47_open_campaign_detail(_c),
                ink=True,
            )

        # ── campaigns view ────────────────────────────────────────
        def campaigns_view() -> ft.Control:
            items: list[ft.Control] = []
            for c in camps:
                try:
                    items.append(card(c))
                except Exception:
                    pass
            if not items:
                items = [ft.Container(
                    content=ft.Column([
                        ft.Container(height=60),
                        ft.Text("📭 Chưa có chiến dịch nào",
                                text_align=ft.TextAlign.CENTER,
                                color=TEXT_MUTED, size=14),
                        ft.Container(height=8),
                        ft.Text("Bấm nút + ở góc phải để phát động",
                                text_align=ft.TextAlign.CENTER,
                                color=TEXT_MUTED, size=12),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=20,
                )]
            return ft.ListView(controls=items, expand=True, padding=10, spacing=0)

        # ── shares view ───────────────────────────────────────────
        def shares_view() -> ft.Control:
            shares = sorted(
                store.get("dailyShares", store.seed_daily_shares),
                key=lambda s: s.get("at", 0), reverse=True,
            )
            today_start = int(time.time() // 86400) * 86400 * 1000
            today_count = sum(1 for s in shares if s.get("at", 0) >= today_start)
            my_uid = AUTH_STATE.get("uid") or ""
            my_today = sum(1 for s in shares
                           if s.get("at", 0) >= today_start and s.get("userId") == my_uid)

            def share_item(s: dict) -> ft.Control:
                user_name = s.get("userName") or "?"
                platform = s.get("platform") or ""
                links = s.get("links") or []
                images = s.get("images") or []
                note = s.get("note") or ""
                at_txt = time_ago(s.get("at", 0))
                ctrls: list[ft.Control] = [
                    ft.Row([
                        ft.Container(
                            content=ft.Text(initials(user_name, 2),
                                            color=ft.Colors.WHITE, size=11,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=GREEN_DARK, width=32, height=32,
                            border_radius=16, alignment=ft.alignment.center,
                        ),
                        ft.Column([
                            ft.Text(user_name, size=13, weight=ft.FontWeight.W_700),
                            ft.Text(f"{platform} • {at_txt}", size=10, color=TEXT_MUTED),
                        ], spacing=2, expand=True, tight=True),
                    ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ]
                if note:
                    ctrls.append(ft.Text(note, size=12, color=TEXT))
                for url in links[:3]:
                    ctrls.append(ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.LINK, size=14, color=BLUE),
                            ft.Text(url, size=11, color=BLUE,
                                    overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                        ], spacing=6),
                        bgcolor="#eff6ff", border_radius=6,
                        padding=ft.padding.symmetric(horizontal=8, vertical=5),
                    ))
                if images:
                    ctrls.append(ft.Text(f"📎 {len(images)} ảnh", size=11, color=TEXT_MUTED))
                return ft.Container(
                    content=ft.Column(ctrls, spacing=6, tight=True),
                    bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=10,
                    padding=12, margin=ft.margin.only(bottom=8),
                )

            counter = ft.Container(
                content=ft.Row([
                    ft.Column([
                        ft.Text(str(my_today), size=22, weight=ft.FontWeight.BOLD,
                                color=GREEN_DARK),
                        ft.Text("Bài của tôi hôm nay", size=10, color=TEXT_MUTED),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                       spacing=2, tight=True, expand=True),
                    ft.VerticalDivider(width=1, color=BORDER),
                    ft.Column([
                        ft.Text(str(today_count), size=22, weight=ft.FontWeight.BOLD,
                                color=BLUE),
                        ft.Text("Toàn đơn vị hôm nay", size=10, color=TEXT_MUTED),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                       spacing=2, tight=True, expand=True),
                    ft.VerticalDivider(width=1, color=BORDER),
                    ft.Column([
                        ft.Text(str(len(shares)), size=22, weight=ft.FontWeight.BOLD,
                                color=AMBER),
                        ft.Text("Tổng bài chia sẻ", size=10, color=TEXT_MUTED),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                       spacing=2, tight=True, expand=True),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor="#f0f9f4", border_radius=10, padding=12,
                margin=ft.margin.only(bottom=10),
                border=ft.border.all(1, "#c8e6c9"),
            )

            items: list[ft.Control] = [counter]
            if shares:
                items += [share_item(s) for s in shares[:100]]
            else:
                items.append(ft.Container(
                    content=ft.Column([
                        ft.Text("📭 Chưa có bài chia sẻ nào",
                                text_align=ft.TextAlign.CENTER,
                                color=TEXT_MUTED, size=14),
                        ft.Text("Bấm nút + ở góc phải để chia sẻ",
                                text_align=ft.TextAlign.CENTER,
                                color=TEXT_MUTED, size=12),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                    padding=20,
                ))
            return ft.ListView(controls=items, expand=True, padding=10, spacing=0)

        # ── leaderboard view ──────────────────────────────────────
        def leaderboard_view() -> ft.Control:
            lb_source = getattr(self, "lb_source", "camp")
            lb_level  = getattr(self, "lb_level",  "person")
            unit_name_map = self._unit_name_map(store.get("units", store.seed_units))

            events: list[dict] = []
            if lb_source == "camp":
                for c in camps:
                    subs = c.get("submissions") or {}
                    if isinstance(subs, dict):
                        for uid, sub in subs.items():
                            s = soldier_by_id.get(str(uid)) or {}
                            events.append({
                                "uid": str(uid),
                                "unitId": s.get("unitId") or "",
                                "at": int((sub or {}).get("at", 0) or 0),
                                "name": s.get("name") or "",
                            })
            else:
                for sh in store.get("dailyShares", store.seed_daily_shares):
                    uid = str(sh.get("userId") or "")
                    s = soldier_by_id.get(uid) or {}
                    events.append({
                        "uid": uid,
                        "unitId": s.get("unitId") or "",
                        "at": int(sh.get("at", 0) or 0),
                        "name": sh.get("userName") or s.get("name") or "",
                    })

            scores: dict[str, dict] = {}
            for ev in events:
                key = ev["uid"] if lb_level == "person" else (ev.get("unitId") or "unknown")
                if not key:
                    continue
                label = (ev.get("name") or key) if lb_level == "person" \
                        else unit_name_map.get(key, "Khác")
                it = scores.setdefault(key, {"count": 0, "lastAt": 0, "name": label})
                it["count"] += 1
                if ev["at"] > it["lastAt"]:
                    it["lastAt"] = ev["at"]

            ranked = sorted(scores.items(),
                            key=lambda kv: (kv[1]["count"], kv[1]["lastAt"]),
                            reverse=True)

            def _chip(text: str, selected: bool, on_click) -> ft.Container:
                return ft.Container(
                    content=ft.Text(text, size=11,
                                    color=ft.Colors.WHITE if selected else TEXT,
                                    weight=ft.FontWeight.BOLD),
                    bgcolor=GREEN_MID if selected else BG2,
                    border_radius=14,
                    padding=ft.padding.symmetric(horizontal=12, vertical=6),
                    on_click=on_click, ink=True,
                )

            chip_bar = ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text("Nguồn:", size=11, color=TEXT_MUTED, width=50),
                        _chip("🛡 Chiến dịch", lb_source == "camp",
                              lambda e: (setattr(self, "lb_source", "camp"),
                                         switch_view("leaderboard"))),
                        _chip("📤 Chia sẻ", lb_source == "share",
                              lambda e: (setattr(self, "lb_source", "share"),
                                         switch_view("leaderboard"))),
                    ], spacing=6),
                    ft.Row([
                        ft.Text("Cấp:", size=11, color=TEXT_MUTED, width=50),
                        _chip("👤 Cá nhân", lb_level == "person",
                              lambda e: (setattr(self, "lb_level", "person"),
                                         switch_view("leaderboard"))),
                        _chip("🏢 Đơn vị", lb_level == "unit",
                              lambda e: (setattr(self, "lb_level", "unit"),
                                         switch_view("leaderboard"))),
                    ], spacing=6),
                ], spacing=6),
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                bgcolor=BG,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )

            if not ranked:
                return ft.Column([
                    chip_bar,
                    ft.Container(
                        content=ft.Text(
                            "Chưa có dữ liệu xếp hạng.",
                            size=13, color=TEXT_MUTED,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        padding=40, alignment=ft.alignment.center, expand=True,
                    ),
                ], spacing=0, expand=True)

            unit_label = "🛡 minh chứng" if lb_source == "camp" else "📤 chia sẻ"

            def rank_row(idx: int, key: str, info: dict) -> ft.Control:
                if lb_level == "person":
                    s = soldier_by_id.get(key) or {}
                    display = (
                        f"{(s.get('rank') or '').strip()} {(s.get('name') or '').strip()}".strip()
                        or info.get("name") or key
                    )
                else:
                    display = info.get("name") or key
                medal = "🥇" if idx == 0 else ("🥈" if idx == 1 else
                        ("🥉" if idx == 2 else f"#{idx+1}"))
                last_txt = fmt_dt(int(info.get("lastAt") or 0)) if info.get("lastAt") else "-"
                return ft.Container(
                    content=ft.Row([
                        ft.Text(str(medal), size=14, width=40),
                        ft.Column([
                            ft.Text(display, size=13, weight=ft.FontWeight.W_700),
                            ft.Text(f"{info.get('count',0)} {unit_label}",
                                    size=10, color=TEXT_MUTED),
                            ft.Text(f"Gần nhất: {last_txt}", size=10, color=TEXT_MUTED),
                        ], spacing=2, tight=True, expand=True),
                        ft.Container(
                            content=ft.Text(str(info.get("count", 0)), size=12,
                                            color=ft.Colors.WHITE,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=GREEN_MID, border_radius=12,
                            padding=ft.padding.symmetric(horizontal=10, vertical=6),
                        ),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    bgcolor=BG, border=ft.border.all(1, BORDER),
                    border_radius=12, padding=12, margin=ft.margin.only(bottom=8),
                )

            rows = [rank_row(i, k, info) for i, (k, info) in enumerate(ranked[:50])]

            return ft.Column([
                chip_bar,
                ft.ListView(controls=rows, expand=True, padding=10, spacing=0),
            ], spacing=0, expand=True)

        # ── assemble ──────────────────────────────────────────────
        if current_view == "campaigns":
            main_body = campaigns_view()
        elif current_view == "shares":
            main_body = shares_view()
        else:
            main_body = leaderboard_view()

        body_col = ft.Column([tabs_bar, main_body], spacing=0, expand=True)

        # FAB
        profile = store.get("userProfile", store.seed_user_profile)
        my_admin_level = int(profile.get("adminLevel") or 1)
        can_create = self._is_admin() or my_admin_level >= 2

        if current_view == "campaigns" and can_create:
            fab_ctrl = ft.FloatingActionButton(
                icon=ft.Icons.ADD,
                bgcolor=RED, foreground_color=ft.Colors.WHITE,
                tooltip="Phát động chiến dịch F47",
                on_click=lambda e: self.f47_open_create(),
            )
        elif current_view == "shares":
            fab_ctrl = ft.FloatingActionButton(
                icon=ft.Icons.SHARE,
                bgcolor=GREEN_MID, foreground_color=ft.Colors.WHITE,
                tooltip="Chia sẻ bài viết mới",
                on_click=lambda e: self.f47_open_share(),
            )
        else:
            fab_ctrl = None

        if fab_ctrl is None:
            return body_col
        fab = ft.Container(content=fab_ctrl, right=16, bottom=16)
        return ft.Container(content=ft.Stack([body_col, fab]), expand=True)



        def cd_str(c) -> str:
            left = int(c.get("deadline") or 0) - store.now_ms()
            if left <= 0:
                return "Hết giờ"
            h, rem = divmod(left, 3600_000)
            m, _ = divmod(rem, 60_000)
            return f"{h:02d}h {m:02d}m"

        def card(c: dict) -> ft.Container:
            done = len(c.get("submissions") or {})
            total = len(c.get("members") or [])
    # ============================================================
    # ===== VIEW: DANH BẠ                                     =====
    # ============================================================

    def _soldier_avatar(self, name: str, photo_url: str | None, size: float) -> ft.Control:
        url = (photo_url or "").strip()
        if url:
            return ft.Container(
                content=ft.Image(src=url, width=size, height=size, fit=ft.ImageFit.COVER),
                width=size,
                height=size,
                border_radius=size / 2,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                border=ft.border.all(2, ft.Colors.WHITE24),
            )
        return ft.Container(
            content=ft.Text(initials(name or "?", 2), color="#085041", size=max(12, size / 3),
                          weight=ft.FontWeight.BOLD),
            bgcolor="#9fe1cb",
            width=size,
            height=size,
            border_radius=size / 2,
            alignment=ft.alignment.center,
            border=ft.border.all(2, ft.Colors.WHITE24),
        )

    def open_member_profile(self, s: dict) -> None:
        sid = str(s.get("id") or "")
        if not sid:
            return
        my_uid = str(AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or "")
        if sid == my_uid:
            self.overlay_soldier_id = None
            self.set_tab("profile")
            return
        self.overlay_soldier_id = sid
        self.overlay_from_tab = self.tab
        self.body.content = self.view_member_profile(sid)
        self.refresh()

    def reset_member_password(self, username: str) -> None:
        try:
            firebase_auth.send_password_reset_email(firebase_config.username_to_email(username))
            self.toast("✅ Đã gửi email đặt lại mật khẩu")
        except Exception as ex:
            self.toast(f"❌ Lỗi: {ex}")

    def approve_member_account(self, soldier_id: str) -> None:
        soldiers = store.get("soldiers", store.seed_soldiers)
        j = next((i for i, x in enumerate(soldiers) if str(x.get("id")) == str(soldier_id)), None)
        if j is not None:
            soldiers[j]["accountStatus"] = "active"
            # Override 600s để bảo vệ khỏi 30s sync ghi đè (kể cả khi DB chậm)
            store.set_account_status_override(soldier_id, "active", ttl_seconds=600.0)
            # Lưu local ngay lập tức
            store.set_value("soldiers", soldiers)
            self.toast("✅ Đã duyệt tài khoản")
            # Refresh UI ngay
            if self.current_module == "units":
                self.body.content = self.module_units()
            self.refresh()
            # Push Firestore trong background với retry
            def _push():
                for _attempt in range(3):
                    try:
                        if _looks_like_firebase_uid(soldier_id):
                            FS.set_doc(f"users/{soldier_id}", {"accountStatus": "active"})
                        store.STORE.flush_pending()
                        break  # thành công, dừng retry
                    except Exception:
                        import time as _t
                        _t.sleep(5)  # chờ 5s rồi thử lại
            import threading
            threading.Thread(target=_push, daemon=True).start()

    def view_member_profile(self, soldier_id: str) -> ft.Control:
        soldiers = store.get("soldiers", store.seed_soldiers)
        s = next((x for x in soldiers if str(x.get("id")) == str(soldier_id)), None)
        if not s:
            return ft.Container(
                content=ft.Text("Không tìm thấy thành viên", color=TEXT_MUTED),
                alignment=ft.alignment.center,
                padding=40,
                expand=True,
            )
        unit_names = self._unit_name_map(store.get("units", store.seed_units))
        unit_lbl = unit_names.get(s.get("unitId"), s.get("unitName") or "")
        photo = str(s.get("photoUrl") or "")
        display = f"{s.get('rank', '').strip()} {s.get('name', '').strip()}".strip() or s.get("name", "")

        admin_bar: list[ft.Control] = []
        my_uid = str(AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or "")
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_admin_level = int(my_profile.get("adminLevel") or 1)
        if my_admin_level >= 4 or my_profile.get("isAdmin"):
            uname = str(s.get("username") or "")
            if not _is_super_admin_username(uname):
                st = str(s.get("accountStatus") or "")
                if st == "pending":
                    admin_bar = [
                        ft.Row(
                            [
                                ft.ElevatedButton(
                                    "✅ Duyệt tài khoản",
                                    bgcolor=GREEN_MID, color=ft.Colors.WHITE,
                                    on_click=lambda e, _sid=str(soldier_id): self.approve_member_account(_sid),
                                ),
                                ft.OutlinedButton(
                                    "❌ Từ chối (Xoá)",
                                    style=ft.ButtonStyle(color=RED),
                                    on_click=lambda e, _sid=str(soldier_id): self.confirm_delete_soldier(_sid),
                                ),
                            ],
                            spacing=6, wrap=True,
                        )
                    ]
                else:
                    admin_bar = [
                        ft.Row(
                            [
                                ft.IconButton(
                                    icon=ft.Icons.SHIELD_OUTLINED,
                                    icon_color=ft.Colors.WHITE70,
                                    tooltip="Phân quyền",
                                    on_click=lambda e, _sid=str(soldier_id): self.open_units_assign_role_dialog(
                                        _sid, return_to_member_view=True,
                                    ),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.EDIT_OUTLINED,
                                    icon_color=ft.Colors.WHITE70,
                                    tooltip="Sửa hồ sơ",
                                    on_click=lambda e, _sid=str(soldier_id): self.open_member_profile_edit_dialog(_sid),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.DELETE_OUTLINE,
                                    icon_color=ft.Colors.WHITE70,
                                    tooltip="Xóa thành viên",
                                    on_click=lambda e, _sid=str(soldier_id): self.confirm_delete_soldier(_sid),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.VPN_KEY_OUTLINED,
                                    icon_color=ft.Colors.WHITE70,
                                    tooltip="Đặt lại mật khẩu",
                                    on_click=lambda e, _sid=str(soldier_id), _uname=str(s.get("username") or ""): self.open_admin_reset_password_dialog(_sid, _uname),
                                ),
                            ],
                            spacing=2,
                        ),
                    ]

        hero = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._soldier_avatar(s.get("name") or "", photo, 72),
                            ft.Column(
                                [
                                    ft.Text(display, color=ft.Colors.WHITE, size=18,
                                            weight=ft.FontWeight.BOLD),
                                    ft.Text(f"{s.get('username') or '—'}", color=ft.Colors.WHITE, size=12),
                                    ft.Text(f"{s.get('rank', '')} • {s.get('role', '')}",
                                            color=ft.Colors.WHITE, size=12),
                                    ft.Text(unit_lbl or "—", color=ft.Colors.WHITE, size=12),
                                ],
                                expand=True,
                                spacing=2,
                                tight=True,
                            ),
                        ],
                        spacing=14,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(height=10),
                    ft.Row(
                        [
                            ft.ElevatedButton(
                                "Nhắn tin",
                                icon=ft.Icons.MESSAGE_OUTLINED,
                                on_click=lambda e, _s=dict(s): self._open_dm(_s),
                                bgcolor=BLUE,
                                color=ft.Colors.WHITE,
                            ),
                        ],
                    ),
                ]
                + admin_bar,
                spacing=8,
            ),
            gradient=ft.LinearGradient(
                begin=ft.alignment.top_left, end=ft.alignment.bottom_right,
                colors=[GREEN_DARK, GREEN_MID],
            ),
            padding=ft.padding.symmetric(horizontal=16, vertical=20),
        )

        def info_row(label: str, value: str) -> ft.Container:
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Text(label, size=12, color=TEXT_MUTED, width=110),
                        ft.Text(value or "—", size=13, expand=True),
                    ],
                ),
                padding=ft.padding.symmetric(horizontal=14, vertical=10),
                bgcolor=BG,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )

        # Get presence status
        p_status = s.get("presence_status") or "Trực"
        p_from = s.get("presence_from") or ""
        p_to = s.get("presence_to") or ""
        
        status_disp = p_status
        if p_status != "Trực" and (p_from or p_to):
            status_disp = f"{p_status} ({p_from} - {p_to})"

        can_edit_presence = (str(my_uid) == str(soldier_id)) or (my_admin_level >= 4 or my_profile.get("isAdmin"))

        def handle_complete():
            self.body.content = self.view_member_profile(soldier_id)
            self.refresh()

        presence_row = ft.Container(
            content=ft.Row(
                [
                    ft.Text("Tình trạng", size=12, color=TEXT_MUTED, width=110),
                    ft.Text(status_disp, size=13, weight=ft.FontWeight.W_600, color=GREEN_DARK if p_status == "Trực" else ft.Colors.ORANGE_800, expand=True),
                    *([ft.IconButton(
                        ft.Icons.EDIT_OUTLINED, 
                        icon_size=16, 
                        icon_color=GREEN_MID, 
                        tooltip="Cập nhật tình trạng quân số", 
                        on_click=lambda _: self.open_presence_dialog(soldier_id, handle_complete)
                    )] if can_edit_presence else [])
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER
            ),
            padding=ft.padding.symmetric(horizontal=14, vertical=6),
            bgcolor=BG,
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
        )

        details = ft.Container(
            content=ft.Column(
                [
                    info_row("Số hiệu", str(s.get("username") or "")),
                    info_row("Điện thoại", str(s.get("phone") or "")),
                    info_row("Đơn vị", unit_lbl),
                    presence_row,
                    info_row("Trạng thái", str(s.get("accountStatus") or "active")),
                    info_row(
                        "Quyền",
                        ("Quản trị" if s.get("isAdmin") else "Người dùng")
                        + f" • cấp {int(s.get('adminLevel') or 1)}",
                    ),
                ],
                spacing=0,
            ),
            border=ft.border.all(1, BORDER),
            border_radius=12,
            margin=10,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        return ft.ListView(
            controls=[hero, details],
            expand=True,
            padding=0,
        )

    def open_presence_dialog(self, soldier_id: str, on_complete: Callable | None = None) -> None:
        import datetime
        page = self.page
        soldiers = store.get("soldiers", store.seed_soldiers)
        s = next((x for x in soldiers if str(x.get("id")) == str(soldier_id)), None)
        if not s:
            self.toast("⚠️ Không tìm thấy quân nhân")
            return
            
        p_status = s.get("presence_status") or "Trực"
        p_from = s.get("presence_from") or ""
        p_to = s.get("presence_to") or ""
        
        status_dd = ft.Dropdown(
            label="Chọn tình trạng *",
            value=p_status,
            options=[
                ft.dropdown.Option("Trực", "Trực (Tại đơn vị)"),
                ft.dropdown.Option("Ra ngoài", "Ra ngoài (Đi tranh thủ ngắn)"),
                ft.dropdown.Option("Đi phép", "Đi phép (Nghỉ phép năm)"),
                ft.dropdown.Option("Tranh thủ", "Tranh thủ (Nghỉ cuối tuần)"),
            ],
            dense=True, border_radius=8
        )
        
        now_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        tomorrow_str = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%d/%m/%Y %H:%M")
        
        start_input = ft.TextField(
            label="Ngày giờ đi (bắt đầu) *",
            value=p_from or now_str,
            dense=True, border_radius=8,
            helper_text="Định dạng: DD/MM/YYYY HH:MM"
        )
        end_input = ft.TextField(
            label="Ngày giờ về (dự kiến) *",
            value=p_to or tomorrow_str,
            dense=True, border_radius=8,
            helper_text="Định dạng: DD/MM/YYYY HH:MM"
        )
        
        datetime_container = ft.Container(
            content=ft.Column([start_input, end_input], spacing=10),
            visible=(p_status != "Trực")
        )
        
        def on_status_change(e):
            datetime_container.visible = (status_dd.value != "Trực")
            page.update()
            
        status_dd.on_change = on_status_change
        
        presence_err = ft.Text("", color=RED, size=12)
        
        def save_presence(_):
            sel_status = status_dd.value
            from_val = ""
            to_val = ""
            
            if sel_status != "Trực":
                from_val = start_input.value.strip()
                to_val = end_input.value.strip()
                if not from_val or not to_val:
                    presence_err.value = "❌ Vui lòng nhập đầy đủ ngày giờ đi và về"
                    page.update()
                    return
            
            try:
                # Save changes
                soldiers_list = store.get("soldiers", store.seed_soldiers)
                for idx, sol in enumerate(soldiers_list):
                    if str(sol.get("id")) == str(soldier_id):
                        soldiers_list[idx] = {
                            **sol,
                            "presence_status": sel_status,
                            "presence_from": from_val,
                            "presence_to": to_val
                        }
                        break
                store.set_value("soldiers", soldiers_list)
                
                # Update local profile if it's the current user
                local_profile = store.get("userProfile", store.seed_user_profile)
                if str(local_profile.get("id")) == str(soldier_id):
                    local_profile.update({
                        "presence_status": sel_status,
                        "presence_from": from_val,
                        "presence_to": to_val
                    })
                    store.set_value("userProfile", local_profile)
                    
                # Firestore sync
                FS.set_doc(f"users/{soldier_id}", {
                    "presence_status": sel_status,
                    "presence_from": from_val,
                    "presence_to": to_val
                })
                
                self.toast("🎉 Đã cập nhật tình trạng quân số thành công!")
                _dlg.open = False
                
                if on_complete:
                    on_complete()
            except Exception as err:
                presence_err.value = f"❌ Lỗi: {err}"
                page.update()
                
        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Cập nhật tình trạng quân số", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column([
                    presence_err,
                    status_dd,
                    datetime_container
                ], spacing=12, tight=True),
                width=320
            ),
            actions=[
                ft.TextButton("Hủy", on_click=lambda _: _close_dialog(self.page)),
                ft.ElevatedButton("Lưu", on_click=save_presence, bgcolor=GREEN_MID, color=ft.Colors.WHITE)
            ]
        )
        _show_dialog(self.page, _dlg)

    def open_change_avatar(self) -> None:
        page = self.page
        if not store.STORE.is_bound() or not AUTH_STATE.get("idToken"):
            self.toast("Cần đăng nhập để đổi ảnh")
            return

        def on_pick(e: ft.FilePickerResultEvent):
            if not e.files:
                return
            f0 = e.files[0]
            fp = f0.path
            uid = str(AUTH_STATE.get("localId") or AUTH_STATE.get("uid") or "")
            if not uid:
                self.toast("⚠️ Chưa đăng nhập — thử login lại")
                return
            try:
                fname = Path(fp).name if fp else (f0.name or "avatar.jpg")
                remote = fb_storage.make_remote_path(f"avatars/{uid}", fname)
                if fp:
                    res = fb_storage.upload_file(fp, remote, AUTH_STATE["idToken"])
                elif f0.bytes:
                    res = fb_storage.upload_data(remote, f0.bytes, AUTH_STATE["idToken"], fname)
                else:
                    self.toast("Không đọc được file")
                    return
                url = str(res.get("downloadURL") or "")
                if not url:
                    self.toast("⚠️ Upload không trả về URL")
                    return
                p = store.get("userProfile", store.seed_user_profile)
                p["photoUrl"] = url
                store.set_value("userProfile", p)
                soldiers = store.get("soldiers", store.seed_soldiers)
                for i, sol in enumerate(soldiers):
                    if str(sol.get("id")) == uid:
                        soldiers[i] = {**sol, "photoUrl": url}
                        break
                store.set_value("soldiers", soldiers)
                try:
                    FS.set_doc(f"users/{uid}", {"photoUrl": url})
                except Exception:
                    pass
                self.body.content = self.view_profile()
                self.refresh()
                self.toast("✅ Đã cập nhật ảnh đại diện")
            except fb_storage.StorageError as se:
                msg = str(se)
                print("[upload avatar] StorageError:", msg)
                # Tóm tắt lỗi 403/permission/401
                if "403" in msg or "Permission" in msg or "denied" in msg.lower():
                    self.toast("❌ Storage từ chối: cần deploy storage.rules mới")
                elif "401" in msg or "Unauthenticated" in msg:
                    self.toast("❌ Token hết hạn — đăng nhập lại")
                else:
                    self.toast(f"❌ Upload lỗi: {msg[:80]}")
            except Exception as ex:
                import traceback
                print("[upload avatar] Exception:", repr(ex))
                traceback.print_exc()
                self.toast(f"❌ Upload thất bại: {type(ex).__name__}: {str(ex)[:80]}")

        picker = ft.FilePicker(on_result=on_pick)
        page.overlay.append(picker)
        page.update()
        picker.pick_files(allow_multiple=False, file_type=ft.FilePickerFileType.IMAGE)

    def open_member_profile_edit_dialog(self, soldier_id: str) -> None:
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_admin_level = int(my_profile.get("adminLevel") or 1)
        if my_admin_level < 4 and not my_profile.get("isAdmin"):
            self.toast("Chỉ quản trị cấp 4 trở lên mới sửa được hồ sơ")
            return
        page = self.page
        soldiers = store.get("soldiers", store.seed_soldiers)
        sid = str(soldier_id or "")
        idx = next((i for i, x in enumerate(soldiers) if str(x.get("id")) == sid), None)
        if idx is None:
            self.toast("Không tìm thấy thành viên")
            return
        row = dict(soldiers[idx])
        uname = str(row.get("username") or "")
        if _is_super_admin_username(uname):
            self.toast("Không sửa được tài khoản quản trị hệ thống")
            return

        # Helper: nếu giá trị hiện tại không có trong options thì thêm "(hiện tại)" vào đầu
        def _opts_with_current(options: list, current: str) -> list:
            current = (current or "").strip()
            if current and current not in options:
                return [current] + options
            return options

        name_tf = ft.TextField(label="Họ tên", value=str(row.get("name") or ""),
                               border_radius=8, dense=True)

        # Đơn vị: lấy từ cây
        units_tree = store.get("units", store.seed_units)
        unit_opts_full = store.flatten_units_for_select(units_tree)  # [(id, label_with_indent)]
        cur_unit_id = str(row.get("unitId") or "")
        cur_unit_name = str(row.get("unitName") or "")
        # Nếu unitId không có trong cây (đã đổi cây) thì giữ tạm bằng unitName
        if cur_unit_id and not any(k == cur_unit_id for k, _ in unit_opts_full):
            unit_opts_full = [(cur_unit_id, f"(cũ) {cur_unit_name or cur_unit_id}")] + unit_opts_full

        rank_dd = ft.Dropdown(
            label="Cấp bậc",
            value=str(row.get("rank") or "") or None,
            options=[ft.dropdown.Option(r) for r in _opts_with_current(
                list(store.RANKS), str(row.get("rank") or ""))],
            border_radius=8, dense=True,
        )

        # Chức vụ — filter theo đơn vị đã chọn (giống form đăng ký)
        cur_role = str(row.get("role") or "")
        if cur_unit_id:
            _initial_role_opts = store.titles_for_unit(units_tree, cur_unit_id)
        else:
            _initial_role_opts = list(store.TITLES)
        role_dd = ft.Dropdown(
            label="Chức danh" if cur_unit_id else "Chức danh (chọn đơn vị trước)",
            value=cur_role or None,
            options=[ft.dropdown.Option(t) for t in _opts_with_current(_initial_role_opts, cur_role)],
            border_radius=8, dense=True,
            disabled=not cur_unit_id,
        )

        def _on_unit_changed(e=None):
            """Khi đổi đơn vị → lọc lại danh sách chức danh phù hợp."""
            uid = unit_dd.value or ""
            if uid:
                relevant = store.titles_for_unit(units_tree, uid)
                # Giữ giá trị hiện tại nếu vẫn hợp lệ
                role_dd.options = [ft.dropdown.Option(t) for t in _opts_with_current(relevant, role_dd.value or "")]
                if role_dd.value not in relevant and role_dd.value not in (cur_role,):
                    role_dd.value = relevant[0] if relevant else None
                role_dd.label = "Chức danh"
                role_dd.disabled = False
            else:
                role_dd.options = []
                role_dd.value = None
                role_dd.label = "Chức danh (chọn đơn vị trước)"
                role_dd.disabled = True
            try:
                page.update()
            except Exception:
                pass

        unit_dd = ft.Dropdown(
            label="Đơn vị",
            value=cur_unit_id or None,
            options=[ft.dropdown.Option(k, lbl) for k, lbl in unit_opts_full],
            border_radius=8, dense=True,
            on_change=_on_unit_changed,
        )

        # Trạng thái tài khoản
        account_status_options = ["active", "pending", "locked", "reserve"]
        cur_status = str(row.get("accountStatus") or "active")
        status_dd = ft.Dropdown(
            label="Trạng thái tài khoản",
            value=cur_status,
            options=[
                ft.dropdown.Option("active", "✅ Hoạt động"),
                ft.dropdown.Option("pending", "⏳ Chờ duyệt"),
                ft.dropdown.Option("locked", "🔒 Khoá"),
                ft.dropdown.Option("reserve", "💤 Dự bị"),
            ],
            border_radius=8, dense=True,
        )

        # Cờ admin
        is_admin_cb = ft.Checkbox(
            label="Là quản trị (admin app)",
            value=bool(row.get("isAdmin")),
        )

        phone_tf = ft.TextField(label="Điện thoại",
                                value=str(row.get("phone") or ""),
                                border_radius=8, dense=True,
                                keyboard_type=ft.KeyboardType.PHONE)

        user_ro = ft.TextField(
            label="Số hiệu (không đổi)",
            value=str(row.get("username") or ""),
            border_radius=8, dense=True, read_only=True,
        )
        err_t = ft.Text("", color=RED, size=12)

        def save(_):
            n = (name_tf.value or "").strip()
            if not n:
                err_t.value = "⚠️ Họ tên bắt buộc"
                page.update()
                return
            soldiers2 = store.get("soldiers", store.seed_soldiers)
            j = next((i for i, x in enumerate(soldiers2) if str(x.get("id")) == sid), None)
            if j is None:
                err_t.value = "⚠️ Dữ liệu đã đổi, thử lại"
                page.update()
                return
            new_unit_id = unit_dd.value or ""
            new_unit_name = next((lbl.strip() for k, lbl in unit_opts_full
                                  if k == new_unit_id), new_unit_id)
            new_role = (role_dd.value or "").strip()
            soldiers2[j] = {
                **soldiers2[j],
                "name": n,
                "rank": (rank_dd.value or "").strip(),
                "role": new_role,
                "phone": (phone_tf.value or "").strip(),
                "unitId": new_unit_id,
                "unitName": new_unit_name,
                "accountStatus": status_dd.value or "active",
                "isAdmin": bool(is_admin_cb.value),
            }
            store.set_value("soldiers", soldiers2)
            uid_f = str(soldiers2[j].get("id") or "")
            if _looks_like_firebase_uid(uid_f):
                try:
                    FS.set_doc(
                        f"users/{uid_f}",
                        {
                            "name": soldiers2[j]["name"],
                            "rank": soldiers2[j]["rank"],
                            "role": soldiers2[j]["role"],
                            "phone": soldiers2[j]["phone"],
                            "unitId": soldiers2[j]["unitId"],
                            "unitName": soldiers2[j]["unitName"],
                            "accountStatus": soldiers2[j]["accountStatus"],
                            "isAdmin": soldiers2[j]["isAdmin"],
                        },
                    )
                except Exception:
                    pass
            store.log_activity(f"Admin sửa hồ sơ: {uname}")
            _close_dialog(self.page)
            self.toast("Đã lưu hồ sơ")
            if getattr(self, "overlay_soldier_id", None) == sid:
                self.body.content = self.view_member_profile(sid)
            self.refresh()

        _dlg = ft.AlertDialog(
            modal=True,
            bgcolor=BG,
            title=ft.Text(f"Sửa hồ sơ • {uname}", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [user_ro, name_tf, rank_dd, unit_dd, role_dd,
                     status_dd, is_admin_cb, phone_tf, err_t],
                    spacing=10, tight=True, scroll=ft.ScrollMode.AUTO,
                ),
                width=380, height=540,
            ),
            actions=[
                ft.TextButton("Huỷ", on_click=lambda e: _close_dialog(self.page)),
                ft.ElevatedButton("Lưu", on_click=save, bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def confirm_delete_soldier(self, soldier_id: str) -> None:
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_admin_level = int(my_profile.get("adminLevel") or 1)
        if my_admin_level < 4 and not my_profile.get("isAdmin"):
            self.toast("Không có quyền xóa")
            return
        page = self.page
        soldiers = store.get("soldiers", store.seed_soldiers)
        sid = str(soldier_id or "")
        row = next((x for x in soldiers if str(x.get("id")) == sid), None)
        if not row:
            self.toast("Không tìm thấy thành viên")
            return
        if _is_super_admin_username(str(row.get("username") or "")):
            self.toast("Không xóa được tài khoản quản trị hệ thống")
            return

        def do_delete(_):
            # Xoá ở Firestore trước (source of truth)
            if _looks_like_firebase_uid(sid):
                try:
                    FS.delete_doc(f"users/{sid}")
                except Exception:
                    pass
            # Refresh soldiers từ users/ để chắc chắn list mới
            try:
                store.refresh_soldiers_from_users()
            except Exception:
                # Fallback: xoá local
                soldiers2 = [x for x in store.get("soldiers", store.seed_soldiers)
                             if str(x.get("id")) != sid]
                store.set_value("soldiers", soldiers2)
            store.log_activity(f"Admin xóa khỏi danh sách: {row.get('username')}")
            _close_dialog(self.page)
            self.toast("Đã xóa khỏi danh sách")
            if getattr(self, "overlay_soldier_id", None) == sid:
                self.overlay_soldier_id = None
                self.overlay_from_tab = None
                self.body.content = self.view_contacts()
                self.tab = "contacts"
            self.refresh()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Xóa thành viên?", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Text(
                f"Xóa {row.get('name')} ({row.get('username')}) khỏi danh sách quân nhân?\n"
                "Thao tác này không xóa tài khoản Firebase (chỉ gỡ khỏi app).",
                size=13,
            ),
            actions=[
                ft.TextButton("Huỷ", on_click=lambda e: _close_dialog(self.page)),
                ft.ElevatedButton("Xóa", on_click=do_delete, bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def open_add_position_dialog(self, unit_node: dict) -> None:
        """Mở hộp thoại thêm chức danh mới vào một đơn vị (cây đơn vị)."""
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_admin_level = int(my_profile.get("adminLevel") or 1)
        if my_admin_level < 4 and not my_profile.get("isAdmin"):
            self.toast("Chỉ chỉ huy cấp 4+ mới được thêm chức danh")
            return

        page = self.page
        u_id = str(unit_node.get("id") or "")
        u_name = str(unit_node.get("name") or "")
        # Mặc định cấp 4 cho Ban chỉ huy / cơ quan; cấp 1 cho đơn vị khác
        default_level = 4 if (unit_node.get("type") in ("command", "department")
                              or "ban chỉ huy" in u_name.lower()) else 1

        name_in = ft.TextField(label="Tên chức danh *", dense=True, border_radius=8, autofocus=True)
        abbr_in = ft.TextField(label="Viết tắt", dense=True, border_radius=8)
        level_dd = ft.Dropdown(
            label="Phân quyền (cấp)",
            options=[
                ft.dropdown.Option("1", "Cấp 1 — Chiến sĩ / Nhân viên"),
                ft.dropdown.Option("2", "Cấp 2 — Tiểu/Trung đội trưởng"),
                ft.dropdown.Option("3", "Cấp 3 — Trợ lý / Đại đội"),
                ft.dropdown.Option("4", "Cấp 4 — Chỉ huy / Cơ quan"),
            ],
            value=str(default_level), dense=True, border_radius=8,
        )

        def close_dlg():
            try:
                _dlg.open = False; page.update()
            except Exception:
                pass

        def do_save(_):
            n = (name_in.value or "").strip()
            if not n:
                self.toast("Cần nhập tên chức danh"); return
            try:
                lvl = int(level_dd.value or default_level)
            except Exception:
                lvl = default_level

            # Tạo ID duy nhất dựa trên unit id và tên
            import re, time as _time
            slug = re.sub(r"[^a-z0-9]+", "", n.lower())[:8] or f"p{int(_time.time())%10000}"
            new_id = f"{u_id}-{slug}"
            # Đảm bảo không trùng id
            existing_ids = set()
            def _collect(node):
                existing_ids.add(node.get("id"))
                for c in node.get("children", []):
                    _collect(c)
            tree = store.get("units", store.seed_units)
            _collect(tree)
            base_id = new_id; i = 2
            while new_id in existing_ids:
                new_id = f"{base_id}{i}"; i += 1

            new_pos = {
                "id": new_id, "name": n, "type": "assistant",
                "commanderTitle": n.split(" ")[0] if " " not in n else n,
                "commanderId": None,
                "adminLevel": lvl,
                "abbr": (abbr_in.value or "").strip(),
                "children": [],
            }

            # Cập nhật cây
            def _find_and_add(node):
                if node.get("id") == u_id:
                    node.setdefault("children", []).append(new_pos)
                    return True
                for c in node.get("children", []):
                    if _find_and_add(c):
                        return True
                return False

            if not _find_and_add(tree):
                self.toast("Không tìm thấy đơn vị"); return
            store.set_value("units", tree)
            close_dlg()
            self.toast(f"✅ Đã thêm chức danh '{n}' vào {u_name}")
            self.body.content = self.view_unit_members(
                next((c for c in self._iter_units(tree) if c.get("id") == u_id), unit_node)
            )
            self.refresh()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"➕ Thêm chức danh vào: {u_name}", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column([
                    name_in, abbr_in, level_dd,
                    ft.Text("💡 Sau khi thêm, chức danh này sẽ xuất hiện trong dropdown đăng ký tài khoản.",
                            size=11, color=TEXT_MUTED),
                ], spacing=10, tight=True),
                width=380,
            ),
            actions=[
                ft.TextButton("Huỷ", on_click=lambda e: close_dlg()),
                ft.ElevatedButton("Thêm", on_click=do_save,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def _iter_units(self, node: dict):
        """Duyệt mọi node trong cây đơn vị (helper)."""
        yield node
        for c in node.get("children", []):
            yield from self._iter_units(c)

    def view_contacts(self) -> ft.Control:
        """Danh bạ — hiển thị theo thứ tự cây đơn vị, từ BCH Trung đoàn trở xuống."""
        # Loại admin + chính mình khỏi danh bạ
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or ""
        soldiers = [s for s in store.get("soldiers", store.seed_soldiers)
                    if not s.get("isAdmin") and str(s.get("id")) != str(my_uid)]

        tree = store.get("units", store.seed_units)
        unit_names = self._unit_name_map(tree)

        # Nhóm theo unitId (ID là khoá ổn định — tránh trùng tên giữa các nhánh con)
        groups_by_uid: dict[str, list[dict]] = {}
        for s in soldiers:
            uid = s.get("unitId") or ""
            groups_by_uid.setdefault(uid, []).append(s)

        # Thứ tự top-level theo cây canonical (BCH → cơ quan → tiểu đoàn → đại đội trực thuộc)
        TOP_ORDER = [
            "u-bch", "u-tm", "u-ct", "u-hk",
            "u-d7", "u-d8", "u-d9",
            "u-c14", "u-c15", "u-c16", "u-c17", "u-c18",
            "u-c20", "u-c24", "u-c25",
        ]

        # Duyệt DFS từ root, top-level theo TOP_ORDER, các cấp dưới giữ thứ tự khai báo
        order_by_uid: dict[str, int] = {}
        counter = 0

        def walk(node: dict):
            nonlocal counter
            n_id = node.get("id") or ""
            if n_id:
                order_by_uid[n_id] = counter
                counter += 1
            children = list(node.get("children") or [])
            if node.get("id") == "root":
                # Sắp xếp theo TOP_ORDER, các unit không có trong list thì xếp cuối theo thứ tự gốc
                idx = {uid: i for i, uid in enumerate(TOP_ORDER)}
                children.sort(key=lambda c: idx.get(c.get("id"), len(TOP_ORDER) + 1))
            for c in children:
                walk(c)

        walk(tree)

        # Đơn vị không có trong cây → đẩy xuống cuối
        FALLBACK = 10 ** 9

        # Sắp xếp các unitId có quân nhân theo vị trí trong cây
        sorted_uids = sorted(
            groups_by_uid.keys(),
            key=lambda u: (order_by_uid.get(u, FALLBACK), unit_names.get(u, "")),
        )

        # Safety: ép tên ngắn cũ về dạng canonical có "Cơ quan ..."
        def _canon(label: str) -> str:
            low = (label or "").strip().lower()
            if low in ("tham mưu",):
                return "Cơ quan Tham mưu"
            if low in ("chính trị",):
                return "Cơ quan Chính Trị"
            if low in ("hậu cần - kỹ thuật", "hậu cần – kỹ thuật", "hc-kt"):
                return "Cơ quan HC-KT"
            if low in ("chỉ huy trung đoàn", "ban chỉ huy trung đoàn"):
                return "Ban chỉ huy Trung đoàn"
            return label or "Khác"

        sections: list[ft.Control] = []
        for uid in sorted_uids:
            members = groups_by_uid[uid]
            if not members:
                continue
            # Trong cùng đơn vị: sắp theo cấp chức danh
            # (Trưởng ≡ CTV → Phó ≡ CTV phó → Trợ lý → TĐT → TĐ → NV → CS)
            # rồi adminLevel giảm dần, cuối cùng theo tên.
            members.sort(key=lambda x: (
                store.role_priority(x.get("role") or ""),
                -int(x.get("adminLevel") or 0),
                x.get("name", ""),
            ))
            unit_label = _canon(unit_names.get(uid) or (members[0].get("unitName") or "Khác"))

            sections.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Text(unit_label, size=11, weight=ft.FontWeight.BOLD,
                                    color=TEXT_MUTED, expand=True),
                            ft.Text(f"{len(members)}", size=10, color=TEXT_MUTED),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor=BG2, padding=ft.padding.symmetric(horizontal=12, vertical=6),
                )
            )
            for s in members:
                sections.append(self._contact_row(s))

        if not sections:
            sections = [
                ft.Container(
                    content=ft.Text("Chưa có quân nhân nào trong danh bạ",
                                    size=13, color=TEXT_MUTED,
                                    text_align=ft.TextAlign.CENTER),
                    padding=40, alignment=ft.alignment.center,
                ),
            ]

        return ft.ListView(controls=sections, expand=True, padding=0)

    def _unit_name_map(self, tree: dict) -> dict[str, str]:
        result = {}
        def walk(n):
            result[n["id"]] = n["name"]
            for c in n.get("children", []):
                walk(c)
        walk(tree)
        return result

    def _contact_row(self, s: dict) -> ft.Container:
        pic = str(s.get("photoUrl") or "")
        name_txt = ft.Text(s["name"], size=14, weight=ft.FontWeight.W_600)
        sub_txt = ft.Text(
            f"{s['rank']} • {s.get('role', '')} • {s.get('phone', '')}",
            size=11, color=TEXT_MUTED,
        )
        
        def on_info_click(e):
            self.open_member_profile(s)
            
        info_area = ft.Container(
            content=ft.Row(
                [
                    self._soldier_avatar(s.get("name") or "", pic, 44),
                    ft.Column(
                        [name_txt, sub_txt],
                        expand=True, spacing=2, tight=True,
                    ),
                ],
                spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            expand=True,
            on_click=on_info_click,
            bgcolor=ft.Colors.TRANSPARENT,
            ink=True,
        )

        return ft.Container(
            content=ft.Row(
                [
                    info_area,
                    ft.IconButton(ft.Icons.MESSAGE_OUTLINED,
                                  on_click=lambda e, _s=s: self._open_dm(_s),
                                  icon_color=BLUE),
                    ft.IconButton(ft.Icons.PHONE,
                                  on_click=lambda e, _s=s: self._dial_phone(_s),
                                  icon_color=GREEN_MID),
                ],
                spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=12, vertical=8),
            bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
        )

    def _chat_header_call_btn(self, rid: str) -> list:
        """Trả list (0 hoặc 1) IconButton 📞 cho header chat detail.

        Chỉ hiện ở DM. Tìm uid người kia (dạng id mới: dm-uidA-uidB sort,
        hoặc id cũ: dm-uid). Lấy SĐT trong soldiers, bấm là dial.
        """
        if not rid or not rid.startswith("dm-"):
            return []
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or ""
        rest = rid[3:]
        # Format mới: uidA-uidB (sort) → tìm phần KHÔNG phải my_uid
        target_id = rest
        if my_uid and rest.startswith(my_uid + "-"):
            target_id = rest[len(my_uid) + 1:]
        elif my_uid and rest.endswith("-" + my_uid):
            target_id = rest[: -(len(my_uid) + 1)]
        soldiers = store.get("soldiers", store.seed_soldiers)
        s = next((x for x in soldiers if str(x.get("id")) == target_id), None)
        if not s:
            return []
        return [
            ft.IconButton(
                ft.Icons.PHONE,
                tooltip=f"Gọi {s.get('name','')}",
                icon_color=GREEN_MID,
                on_click=lambda e, _s=s: self._dial_phone(_s),
            )
        ]

    def _dial_phone(self, s: dict) -> None:
        """Mở app điện thoại của thiết bị để gọi số của quân nhân s."""
        phone = (s.get("phone") or "").strip()
        if not phone:
            self.toast(f"⚠️ {s.get('name','Người này')} chưa có số điện thoại")
            return
        # Bỏ ký tự không cần (dấu chấm, gạch, khoảng trắng)
        clean = "".join(c for c in phone if c.isdigit() or c == "+")
        if not clean:
            self.toast(f"⚠️ Số không hợp lệ: {phone}")
            return
        try:
            self.page.launch_url(f"tel:{clean}")
        except Exception as ex:
            self.toast(f"❌ Không gọi được: {ex}")

    def chat_room_menu(self, room) -> None:
        """Popup tuỳ chọn cho 1 phòng chat trong list."""
        rid, name, *_ = room
        page = self.page

        def close_dlg():
            try:
                _dlg.open = False
            except Exception:
                pass
            page.update()

        def do_hide(_):
            close_dlg()
            self._chat_confirm_hide(rid, name)

        def do_delete_both(_):
            close_dlg()
            self._chat_confirm_delete_both(rid, name)

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"💬 {name[:50]}", size=14, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column([
                    ft.TextButton(
                        "🗑  Xoá khỏi danh sách (chỉ ẩn ở máy bạn)",
                        on_click=do_hide,
                        style=ft.ButtonStyle(padding=12, color=AMBER),
                    ),
                    ft.TextButton(
                        "🔥  Xoá hẳn cho cả 2 bên (xoá toàn bộ tin nhắn)",
                        on_click=do_delete_both,
                        style=ft.ButtonStyle(padding=12, color=RED),
                    ),
                ], spacing=4, tight=True),
                width=340,
            ),
            actions=[ft.TextButton("Đóng", on_click=lambda e: close_dlg())],
        )
        _show_dialog(self.page, _dlg)

    def _chat_confirm_delete_both(self, rid: str, name: str) -> None:
        """Xoá phòng cho cả 2 bên — DM thì ai cũng được; nhóm chỉ
        nhóm trưởng (createdBy) hoặc admin được."""
        page = self.page
        my_uid = AUTH_STATE.get("uid") or ""
        rooms = store.get("chat_rooms", store.seed_chat_rooms)
        room_obj = next((r for r in rooms if r.get("id") == rid), {}) or {}
        rtype = room_obj.get("type") or ("dm" if rid.startswith("dm-") else "group")

        # Check quyền
        if rtype == "group":
            created_by = room_obj.get("createdBy") or ""
            if not (self._is_admin() or (created_by and created_by == my_uid)):
                self.toast("⚠️ Chỉ nhóm trưởng hoặc admin mới xoá được nhóm")
                return

        def do(_):
            try:
                _dlg.open = False
            except Exception:
                pass

            def worker():
                try:
                    store.delete_chat_room(rid)
                except Exception:
                    pass
                def back():
                    self.toast(f"🔥 Đã xoá '{name[:30]}' cho cả 2 bên")
                    self.body.content = self.view_chat()
                    self.refresh()
                page.run_thread(back) if hasattr(page, "run_thread") else back()

            threading.Thread(target=worker, daemon=True).start()

        warn_text = (
            f"Xoá HẲN '{name}' cho cả 2 bên?\n"
            f"Toàn bộ tin nhắn sẽ mất, KHÔNG khôi phục được.\n"
            f"Người kia cũng sẽ không còn thấy."
            if rtype == "dm" else
            f"Xoá HẲN nhóm '{name}'?\n"
            f"Toàn bộ thành viên sẽ mất phòng và mọi tin nhắn.\n"
            f"KHÔNG khôi phục được."
        )

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("⚠️ Xác nhận xoá hẳn", weight=ft.FontWeight.BOLD,
                          color=RED),
            content=ft.Text(warn_text, size=12),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton("Xoá hẳn", on_click=do,
                                  bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def _chat_confirm_hide(self, rid: str, name: str) -> None:
        """Xác nhận trước khi ẩn phòng khỏi list."""
        page = self.page

        def do(_):
            try:
                _dlg.open = False
            except Exception:
                pass
            hidden = store.get("hiddenChatRooms", lambda: {})
            # Migrate format cũ (list) sang dict
            if isinstance(hidden, list):
                hidden = {x: 0 for x in hidden}
            hidden[rid] = store.now_ms()
            store.STORE.set_local("hiddenChatRooms", hidden)
            self.toast(f"🗑 Đã ẩn '{name[:30]}'. Sẽ tự hiện lại khi có tin mới.")
            self.body.content = self.view_chat()
            self.refresh()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Xác nhận", weight=ft.FontWeight.BOLD),
            content=ft.Text(
                f"Ẩn phòng '{name}' khỏi danh sách của bạn?\n"
                f"(Không ảnh hưởng người khác, có thể hiện lại bằng tin nhắn mới)",
                size=12,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton("Ẩn", on_click=do,
                                  bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def _open_dm(self, s: dict) -> None:
        # DM room id phải DETERMINISTIC giữa 2 người (sort uid) — tránh 2 phòng
        # khác nhau khi A nhắn B vs B nhắn A.
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or store.get(
            "userProfile", store.seed_user_profile,
        ).get("username", "")
        their_uid = str(s.get("id") or "")
        # Không cho nhắn cho chính mình
        if their_uid and str(my_uid or "") == their_uid:
            self.toast("⚠️ Không thể nhắn tin cho chính mình")
            return
        a, b = sorted([str(my_uid or ""), their_uid])
        rid = f"dm-{a}-{b}" if (a and b) else f"dm-{their_uid}"

        display_name = f"{s.get('rank', '').strip()} {s.get('name', '').strip()}".strip() or s.get("name", rid)
        role_txt = (s.get("role") or "").strip()
        store.upsert_chat_room({
            "id": rid,
            "name": display_name,
            "type": "dm",
            "members": [x for x in [my_uid, s.get("id")] if x],
            "lastAt": store.now_ms(),
            "lastMessage": "",
            "unread": 0,
            "status": role_txt,
            "online": True,
            "lastReadAt": {},
            "unreadByUser": {},
            "pinnedMessageIds": [],
        })

        self.open_chat_room((rid, display_name, role_txt, "", 0, False, "dm", True))

    # ============================================================
    # ===== VIEW: CÁ NHÂN                                     =====
    # ============================================================

    def view_profile(self) -> ft.Control:
        p = store.get("userProfile", store.seed_user_profile)
        soldiers = store.get("soldiers", store.seed_soldiers)
        reports = store.get("reports", store.seed_reports)
        my_reports = sum(1 for r in reports if r.get("author") == p.get("name"))

        hero = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._soldier_avatar(p.get("name") or "", str(p.get("photoUrl") or ""), 64),
                            ft.Column(
                                [ft.Text(p.get("name") or "Không tên",
                                         color=ft.Colors.WHITE, size=18,
                                         weight=ft.FontWeight.BOLD),
                                 ft.Text(
                                     " • ".join(filter(None, [
                                         (p.get("rank") or "").strip(),
                                         (p.get("role") or "").strip(),
                                     ])) or "—",
                                     color=ft.Colors.WHITE70, size=12,
                                 ),
                                 ft.Text(p.get("unitName") or "",
                                         color=ft.Colors.WHITE54, size=11)],
                                expand=True, spacing=2, tight=True,
                            ),
                        ],
                        spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(height=14),
                    ft.Row([
                        self._mini_stat(
                            (lambda _sl: f"{sum(1 for _s in _sl if (_s.get('accountStatus') or 'active') == 'active')}/{len(_sl)}")(
                                [_s for _s in soldiers if not _is_super_admin_username(str(_s.get("username") or ""))]
                            ),
                            "Quân số",
                        ),
                        self._mini_stat(str(my_reports), "Báo cáo"),
                        self._mini_stat(str(p.get("serviceYears", 0)), "Năm PV"),
                        self._mini_stat("5", "KT nhận"),
                    ], spacing=6),
                ],
                spacing=4,
            ),
            gradient=ft.LinearGradient(begin=ft.alignment.top_left, end=ft.alignment.bottom_right,
                                       colors=[GREEN_DARK, GREEN_MID]),
            padding=ft.padding.symmetric(horizontal=16, vertical=20),
        )

        def menu_item(icon, lbl, on_click) -> ft.Container:
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Container(content=ft.Text(icon, size=16),
                                     width=32, height=32, border_radius=8,
                                     bgcolor=BG2, alignment=ft.alignment.center),
                        ft.Text(lbl, size=14, expand=True),
                        ft.Icon(ft.Icons.CHEVRON_RIGHT, color=TEXT_MUTED, size=20),
                    ],
                    spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(horizontal=14, vertical=12),
                bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                on_click=lambda e: on_click(),
                ink=True,
            )

        # Get own presence
        my_presence = p.get("presence_status") or "Trực"
        my_p_from = p.get("presence_from") or ""
        my_p_to = p.get("presence_to") or ""
        
        my_disp = my_presence
        if my_presence != "Trực" and (my_p_from or my_p_to):
            my_disp = f"{my_presence} ({my_p_from} - {my_p_to})"

        def refresh_own_profile():
            self.body.content = self.view_profile()
            self.refresh()

        menu = ft.Container(
            content=ft.Column(
                [
                    menu_item("👤", "Thông tin cá nhân",
                              lambda: self.open_profile_info()),
                    menu_item("👥", f"Tình trạng quân số: {my_disp}",
                              lambda: self.open_presence_dialog(str(p.get("id") or AUTH_STATE.get("localId")), refresh_own_profile)),
                    menu_item("📅", "Lịch trực của tôi",
                              lambda: self.open_module("schedule")),
                    menu_item("🏅", "Thành tích – Khen thưởng",
                              lambda: self.open_module("awards")),
                    menu_item("🛡", "Hoạt động F47", lambda: self.open_module("f47")),
                    menu_item("🔒", "Bảo mật & Mật khẩu",
                              lambda: self.open_change_password()),
                    menu_item("🔔", "Cài đặt thông báo",
                              lambda: self.open_notif_settings()),
                    menu_item("⚙️", "Cài đặt ứng dụng",
                              lambda: self.open_app_settings()),
                    menu_item("⏻", "Đăng xuất", lambda: self.confirm_logout()),
                ],
                spacing=0,
            ),
            margin=ft.margin.only(top=10),
            border=ft.border.symmetric(vertical=ft.BorderSide(1, BORDER)),
        )

        return ft.ListView(
            controls=[hero, menu,
                      ft.Container(content=ft.Text(f"Quản lý LL47 e141 • v{APP_VERSION}",
                                                   color=TEXT_MUTED, size=11),
                                   alignment=ft.alignment.center, padding=14)],
            expand=True, padding=0,
        )

    def _mini_stat(self, value: str, label: str) -> ft.Container:
        return ft.Container(
            content=ft.Column(
                [ft.Text(value, color=ft.Colors.WHITE, size=18,
                         weight=ft.FontWeight.BOLD),
                 ft.Text(label, color=ft.Colors.WHITE70, size=9)],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=1, tight=True,
            ),
            bgcolor=ft.Colors.WHITE12, border_radius=10,
            padding=ft.padding.symmetric(vertical=8, horizontal=4),
            expand=True, alignment=ft.alignment.center,
        )

    def open_profile_info(self) -> None:
        """Mở dialog xem/sửa thông tin cá nhân."""
        page = self.page
        p = store.get("userProfile", store.seed_user_profile)
        _is_sa = _is_super_admin_username(str(p.get("username") or "")) or (p.get("name") or "").lower() == "admin"

        name_input = ft.TextField(label="Họ tên", value=p.get("name", ""), border_radius=8, dense=True,
                                  read_only=_is_sa)
        rank_input = ft.TextField(label="Cấp bậc", value=p.get("rank", ""), border_radius=8, dense=True,
                                  visible=not _is_sa)
        role_input = ft.TextField(label="Chức vụ", value=p.get("role", ""), border_radius=8, dense=True)
        phone_input = ft.TextField(label="Số điện thoại", value=p.get("phone", ""), border_radius=8, dense=True)
        unit_input = ft.TextField(label="Đơn vị", value=p.get("unitName", ""), border_radius=8, dense=True)
        hometown_input = ft.TextField(label="Quê quán", value=p.get("hometown", ""), border_radius=8, dense=True)
        err_text = ft.Text("", color=RED, size=12)

        def save(e):
            name = (name_input.value or "").strip()
            if not name:
                err_text.value = "⚠️ Họ tên không được để trống"
                page.update()
                return
            new_p = dict(p)
            updates: dict = {
                "role": (role_input.value or "").strip(),
                "phone": (phone_input.value or "").strip(),
                "unitName": (unit_input.value or "").strip(),
                "hometown": (hometown_input.value or "").strip(),
            }
            if _is_sa:
                # Super admin: giữ name="admin" và rank="" — không cho đổi
                updates["name"] = "admin"
                updates["rank"] = ""
            else:
                updates["name"] = name
                updates["rank"] = (rank_input.value or "").strip()
            new_p.update(updates)
            store.set_value("userProfile", new_p)
            # Đồng bộ lên DB trong background
            uid = str(p.get("id") or AUTH_STATE.get("localId") or "")
            if uid:
                def _push():
                    try:
                        _sync = {k: v for k, v in updates.items() if v or k == "rank"}
                        FS.set_doc(f"users/{uid}", _sync)
                    except Exception:
                        pass
                import threading as _th
                _th.Thread(target=_push, daemon=True).start()
            try:
                _dlg.open = False
            except Exception:
                pass
            self.toast("✅ Đã cập nhật thông tin cá nhân")
            self.body.content = self.view_profile()
            self.refresh()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("👤 Thông tin cá nhân", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [name_input, rank_input, role_input, phone_input, unit_input, hometown_input, err_text],
                    spacing=10,
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
                width=380,
            ),
            actions=[
                ft.TextButton("Huỷ", on_click=lambda e: _close_dialog(self.page)),
                ft.ElevatedButton("Lưu", on_click=save, bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def module_awards(self) -> ft.Control:
        """Màn Thành tích - Khen thưởng."""
        awards = store.get("awards", lambda: [])
        profile = store.get("userProfile", store.seed_user_profile)

        def award_row(a: dict) -> ft.Container:
            return ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Text(a.get("title", "Thành tích"), size=13, weight=ft.FontWeight.W_700, expand=True),
                                ft.Text(fmt_date(int(a.get("at", store.now_ms()))), size=10, color=TEXT_MUTED),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        ft.Text(a.get("desc", ""), size=11, color=TEXT_MUTED),
                    ],
                    spacing=4,
                    tight=True,
                ),
                bgcolor=BG,
                border=ft.border.all(1, BORDER),
                border_radius=10,
                padding=12,
                margin=ft.margin.only(bottom=8),
            )

        def add_sample(e):
            new = {
                "id": f"aw{store.now_ms()}",
                "title": "🏅 Hoàn thành xuất sắc nhiệm vụ",
                "desc": f"{profile.get('name', 'Cán bộ')} đạt kết quả tốt trong đợt công tác",
                "at": store.now_ms(),
            }
            awards.insert(0, new)
            store.set_value("awards", awards)
            self.toast("✅ Đã thêm thành tích")
            self.body.content = self.module_awards()
            self.refresh()

        return ft.Column(
            [
                self.module_back_bar("🏅 Thành tích – Khen thưởng"),
                ft.ListView(
                    controls=[
                        ft.Container(
                            content=ft.Column(
                                [award_row(a) for a in awards] if awards else [ft.Text("Chưa có thành tích nào", size=12, color=TEXT_MUTED)],
                                spacing=0,
                            ),
                            padding=10,
                        ),
                        ft.Container(
                            content=ft.ElevatedButton(
                                "＋ Thêm thành tích",
                                on_click=add_sample,
                                bgcolor=GREEN_MID,
                                color=ft.Colors.WHITE,
                                width=10000,
                            ),
                            padding=ft.padding.only(left=10, right=10, bottom=12),
                        ),
                    ],
                    expand=True,
                    padding=0,
                ),
            ],
            spacing=0,
            expand=True,
        )

    # ============================================================
    # ===== SETTINGS DIALOGS                                   =====
    # ============================================================

    def open_change_password(self) -> None:
        """Đổi mật khẩu (Firebase Auth thật)."""
        page = self.page
        old_input = ft.TextField(label="Mật khẩu hiện tại", password=True,
                                 can_reveal_password=True, border_radius=8, dense=True)
        new_input = ft.TextField(label="Mật khẩu mới (≥6 ký tự)", password=True,
                                 can_reveal_password=True, border_radius=8, dense=True)
        confirm_input = ft.TextField(label="Nhập lại mật khẩu mới", password=True,
                                     can_reveal_password=True, border_radius=8, dense=True)
        err_text = ft.Text("", color=RED, size=12)

        def do_change(e):
            old_pw = (old_input.value or "").strip()
            new_pw = (new_input.value or "").strip()
            confirm_pw = (confirm_input.value or "").strip()
            if not (old_pw and new_pw and confirm_pw):
                err_text.value = "⚠️ Điền đủ 3 ô"
                page.update(); return
            if new_pw != confirm_pw:
                err_text.value = "⚠️ Mật khẩu mới không khớp"
                page.update(); return
            if len(new_pw) < 6:
                err_text.value = "⚠️ Mật khẩu mới phải ≥ 6 ký tự"
                page.update(); return
            err_text.value = "⏳ Đang xử lý..."
            page.update()

            def worker():
                try:
                    # Verify mật khẩu cũ bằng cách đăng nhập lại
                    email = AUTH_STATE.get("email") or firebase_config.username_to_email(
                        AUTH_STATE.get("username", ""))
                    firebase_auth.sign_in_with_password(email, old_pw)
                    # Đổi mật khẩu mới
                    new_creds = firebase_auth.update_password(
                        AUTH_STATE["idToken"], new_pw
                    )
                    _set_auth(new_creds, username=AUTH_STATE.get("username"))
                    try:
                        FS.update_doc(f"users/{AUTH_STATE['localId']}", {"password_plain": new_pw})
                    except Exception:
                        pass
                    def ok():
                        try:
                            _dlg.open = False
                        except Exception:
                            pass
                        self.toast("✅ Đã đổi mật khẩu")
                        page.update()
                    page.run_thread(ok) if hasattr(page, "run_thread") else ok()
                except FirebaseAuthError as fe:
                    msg = friendly_error(fe)
                    if "INVALID_PASSWORD" in (fe.code or "") or "INVALID_LOGIN" in (fe.code or ""):
                        msg = "Mật khẩu hiện tại không đúng"
                    def fail():
                        err_text.value = f"❌ {msg}"
                        page.update()
                    page.run_thread(fail) if hasattr(page, "run_thread") else fail()
                except Exception as ex:
                    def fail():
                        err_text.value = f"❌ {ex}"
                        page.update()
                    page.run_thread(fail) if hasattr(page, "run_thread") else fail()

            threading.Thread(target=worker, daemon=True).start()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("🔒 Đổi mật khẩu", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [old_input, new_input, confirm_input, err_text],
                    spacing=10, tight=True,
                ),
                width=380,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton("Đổi mật khẩu", on_click=do_change,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def open_notif_settings(self) -> None:
        """Cài đặt thông báo (lưu cục bộ + Firestore)."""
        page = self.page
        prefs = store.get("notifPrefs", lambda: {
            "sound": True, "push": True, "urgent": True, "f47": True, "unit": True,
        })

        sw_sound = ft.Switch(label="🔊 Âm thanh thông báo", value=bool(prefs.get("sound", True)))
        sw_push = ft.Switch(label="📲 Thông báo đẩy (push)", value=bool(prefs.get("push", True)))
        sw_urgent = ft.Switch(label="🚨 Lệnh khẩn", value=bool(prefs.get("urgent", True)))
        sw_f47 = ft.Switch(label="🛡 Chiến dịch F47", value=bool(prefs.get("f47", True)))
        sw_unit = ft.Switch(label="📋 Thông báo đơn vị", value=bool(prefs.get("unit", True)))

        def save(e):
            store.set_value("notifPrefs", {
                "sound": sw_sound.value, "push": sw_push.value,
                "urgent": sw_urgent.value, "f47": sw_f47.value,
                "unit": sw_unit.value,
            })
            try:
                _dlg.open = False
            except Exception:
                pass
            self.toast("✅ Đã lưu cài đặt thông báo")
            page.update()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("🔔 Cài đặt thông báo", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [sw_sound, sw_push, ft.Divider(), sw_urgent, sw_f47, sw_unit],
                    spacing=6, tight=True,
                ),
                width=320,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.ElevatedButton("Lưu", on_click=save,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        _show_dialog(self.page, _dlg)

    def open_app_settings(self) -> None:
        """Cài đặt ứng dụng: thông tin user, đồng bộ thủ công, xoá cache."""
        page = self.page
        info_lines = [
            f"👤 {AUTH_STATE.get('username', '(chưa đăng nhập)')}",
            f"📧 {AUTH_STATE.get('email', '')}",
            f"🆔 UID: {AUTH_STATE.get('uid', '')[:16]}...",
            f"🔑 Token còn hạn: {max(0, AUTH_STATE.get('expiresAt', 0) - int(time.time()))}s",
            f"📦 Phiên bản: {APP_VERSION}",
        ]
        sync_status = ft.Text("", size=12, color=TEXT_MUTED)

        # Theme toggle control
        is_dark = page.theme_mode == ft.ThemeMode.DARK
        theme_toggle = ft.Switch(
            label="Chế độ Tối (Dark Mode)",
            value=is_dark,
        )

        def on_theme_change(e):
            new_mode = ft.ThemeMode.DARK if theme_toggle.value else ft.ThemeMode.LIGHT
            page.theme_mode = new_mode
            update_theme_colors(new_mode)
            _save_theme_pref(new_mode)
            page.update()
            # Force rebuild active tab to render the new colors properly
            self.set_tab(self.tab)

        theme_toggle.on_change = on_theme_change

        def do_sync(e):
            sync_status.value = "⏳ Đang đồng bộ..."
            page.update()
            def worker():
                try:
                    n = store.STORE.sync_from_firestore()
                    n_users = store.refresh_soldiers_from_users()
                    n2 = store.STORE.flush_pending()
                    msg = f"✅ Đồng bộ {n} mục, {n_users} quân nhân, đẩy {n2} pending"
                except Exception as ex:
                    msg = f"❌ {ex}"
                def show():
                    sync_status.value = msg
                    page.update()
                page.run_thread(show) if hasattr(page, "run_thread") else show()
            threading.Thread(target=worker, daemon=True).start()

        def do_clear_cache(e):
            try:
                store.STORE.reset()
                sync_status.value = "✅ Đã xoá cache. Login lại để tải dữ liệu."
            except Exception as ex:
                sync_status.value = f"❌ {ex}"
            page.update()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("⚙️ Cài đặt ứng dụng", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [
                        *(ft.Text(line, size=12) for line in info_lines),
                        ft.Divider(),
                        theme_toggle,
                        ft.Divider(),
                        ft.ElevatedButton("🔄 Đồng bộ ngay với Firestore",
                                          on_click=do_sync, width=10000),
                        ft.ElevatedButton("🗑 Xoá cache cục bộ",
                                          on_click=do_clear_cache, width=10000,
                                          bgcolor="#fff4f4", color=RED),
                        sync_status,
                    ],
                    spacing=8, tight=True,
                ),
                width=340,
            ),
            actions=[
                ft.TextButton(
                    "Đóng",
                    on_click=lambda e: _close_dialog(self.page),
                ),
            ],
        )
        _show_dialog(self.page, _dlg)

    def confirm_logout(self) -> None:
        page = self.page

        def do_logout(e):
            _dlg.open = False
            self.stop_realtime_sync()
            _clear_auth()
            show_login(self.page)

        _dlg = ft.AlertDialog(
            title=ft.Text("Đăng xuất"),
            content=ft.Text("Đăng xuất khỏi Quản lý LL47 e141?"),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: _close_dialog(self.page),
                ),
                ft.TextButton("Đăng xuất", on_click=do_logout),
            ],
        )
        _show_dialog(self.page, _dlg)

    # ============================================================
    # ===== VIEW: TẤT CẢ THÔNG BÁO                            =====
    # ============================================================

    def view_all_notifs(self) -> ft.Control:
        notifs = self._my_notifs()

        def delete_one(nid: str):
            all_notifs = store.get("notifs", store.seed_notifs)
            store.set_value("notifs", [x for x in all_notifs if x.get("id") != nid])
            self.body.content = self.view_all_notifs()
            self.refresh()

        def delete_all_clicked(e=None):
            my_uid = AUTH_STATE.get("uid") or ""
            all_notifs = store.get("notifs", store.seed_notifs)
            # Giữ lại notif KHÔNG thuộc user này
            keep = [x for x in all_notifs if (x.get("targetUid") or "").strip() and (x.get("targetUid") or "").strip() != my_uid]
            store.set_value("notifs", keep)
            self.toast("🗑 Đã xoá tất cả thông báo")
            self.body.content = self.view_all_notifs()
            self.refresh()

        def row(n: dict) -> ft.Container:
            color = {"urgent": RED, "f47": BLUE, "unit": AMBER,
                     "ctdctct": GREEN_DARK, "guest": "#8855cc", "warning": AMBER,
                     "success": GREEN_MID}.get(n["type"], "#999")
            _sender = (n.get("senderName") or "").strip()
            _meta_parts = []
            if _sender:
                _meta_parts.append(f"👤 {_sender}")
            _meta_parts.append(time_ago(n["at"]))
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Container(width=8, height=8, bgcolor=color, border_radius=4,
                                     margin=ft.margin.only(top=6)),
                        ft.Column(
                            [ft.Text(n["title"], size=13, color=TEXT,
                                     weight=ft.FontWeight.BOLD if not n["read"]
                                     else ft.FontWeight.W_500),
                             ft.Text(n["desc"], size=11, color=TEXT_MUTED, max_lines=2),
                             ft.Text("  •  ".join(_meta_parts), size=10, color=TEXT_MUTED)],
                            spacing=2, expand=True, tight=True,
                        ),
                        ft.IconButton(
                            ft.Icons.DELETE_OUTLINE, icon_size=18,
                            icon_color=TEXT_MUTED, tooltip="Xoá",
                            on_click=lambda e, _nid=n["id"]: delete_one(_nid),
                        ),
                    ],
                    spacing=10, vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                bgcolor="#f0f9f4" if not n["read"] else BG,
                border=ft.border.all(1, BORDER), border_radius=10,
                padding=11, margin=ft.margin.only(bottom=7),
                on_click=lambda e, _n=n: self.handle_notif_click(_n),
                ink=True,
            )

        def mark_all_clicked(e=None):
            self.mark_all_notifs_read()
            self.body.content = self.view_all_notifs()
            self.refresh()

        unread_cnt = sum(1 for n in notifs if not n.get("read"))

        top_bar = ft.Container(
            content=ft.Row(
                [
                    ft.TextButton(
                        "Đánh dấu tất cả đã đọc",
                        on_click=mark_all_clicked,
                        style=ft.ButtonStyle(color=GREEN_MID if unread_cnt else TEXT_MUTED),
                    ) if unread_cnt else ft.Text("Đã đọc hết", size=12, color=TEXT_MUTED),
                    ft.TextButton(
                        "🗑 Xoá tất cả",
                        on_click=delete_all_clicked,
                        style=ft.ButtonStyle(color=RED),
                    ) if notifs else ft.Container(),
                ],
                alignment=ft.MainAxisAlignment.SPACE_EVENLY,
            ),
            padding=ft.padding.symmetric(horizontal=12, vertical=6),
            bgcolor=BG,
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
        )

        return ft.Column(
            [
                self.module_back_bar("🔔 Thông báo"),
                top_bar,
                ft.ListView(
                    controls=[
                        ft.Container(
                            content=ft.Column(
                                [row(n) for n in notifs] if notifs
                                else [ft.Text("📭 Không có thông báo", color=TEXT_MUTED)],
                                spacing=0,
                            ),
                            padding=10,
                        )
                    ],
                    expand=True,
                ),
            ],
            spacing=0,
            expand=True,
        )

    def module_exams(self) -> ft.Control:
        # State management
        if not hasattr(self, "exams_view"):
            self.exams_view = "competitions"
        if not hasattr(self, "selected_exam_id"):
            self.selected_exam_id = None

        page = self.page
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_uid = my_profile.get("id") or AUTH_STATE.get("localId")
        my_admin = int(my_profile.get("adminLevel") or 0)
        is_commander = (my_admin >= 2) or self._is_top_commander(my_uid) or ((my_profile.get("unitId") or "").startswith("u-bch"))

        # Dùng cache — không block UI thread
        exams = store.get("_cache_exams", list) or []
        attempts = store.get("_cache_exam_attempts", list) or []

        # Load background nếu cache trống hoặc đã quá 60s
        import time as _t
        _last_fetch = getattr(self, "_exams_last_fetch", 0)
        _is_loading = getattr(self, "_exams_loading", False)
        if (not exams or _t.time() - _last_fetch > 60) and not _is_loading:
            self._exams_loading = True
            def _fetch():
                try:
                    _e = FS.list_collection("exams") or []
                    _a = FS.list_collection("exam_attempts") or []
                    store.set_value("_cache_exams", _e)
                    store.set_value("_cache_exam_attempts", _a)
                    self._exams_last_fetch = _t.time()
                except Exception:
                    pass
                finally:
                    self._exams_loading = False
                # Chỉ refresh UI nếu user vẫn đang ở module exams
                try:
                    if self.current_module == "exams":
                        self.body.content = self.module_exams()
                        self.page.update()
                except Exception:
                    pass
            threading.Thread(target=_fetch, daemon=True).start()

        exams = sorted(exams, key=lambda x: x.get("createdAt", 0), reverse=True)
        unit_names = self._unit_name_map(store.get("units", store.seed_units))

        # Rebuild helper
        def _rebuild():
            self.body.content = self.module_exams()
            self.refresh()

        # Tab Selection handler
        def set_exams_tab(tab_key: str):
            self.exams_view = tab_key
            _rebuild()

        # Export button only shown on leaderboard tab
        export_btn = None
        if self.exams_view == "leaderboard" and exams:
            def _export_exams(e):
                # Filter attempts for this exam
                exam_atts = [a for a in attempts if a.get("examId") == self.selected_exam_id]
                cur_exam = next((ex for ex in exams if ex.get("id") == self.selected_exam_id), exams[0]) if exams else None
                title_prefix = cur_exam.get('title', '') if cur_exam else ''
                
                # Check leaderboard_sub_tab
                if self.leaderboard_sub_tab == "individual":
                    headers = ["🏆 Hạng", "Họ và tên", "Đơn vị", "Điểm số", "Thời gian làm bài"]
                    rows = []
                    # Group by soldierId to get best attempt of each participant
                    best_atts = {}
                    for att in exam_atts:
                        sid = att.get("soldierId")
                        if not sid:
                            continue
                        sc = float(att.get("score", 0))
                        dur = int(att.get("durationSeconds", 999999))
                        if sid not in best_atts:
                            best_atts[sid] = att
                        else:
                            existing = best_atts[sid]
                            ex_sc = float(existing.get("score", 0))
                            ex_dur = int(existing.get("durationSeconds", 999999))
                            if sc > ex_sc or (sc == ex_sc and dur < ex_dur):
                                best_atts[sid] = att

                    filtered_atts = list(best_atts.values())
                    filtered_atts.sort(key=lambda x: (-float(x.get("score", 0)), int(x.get("durationSeconds", 99999))))
                    
                    for rank, att in enumerate(filtered_atts, 1):
                        name = att.get("soldierName", "Quân nhân")
                        rank_str = (att.get("soldierRank") or "").strip()
                        rank_prefix = f"{rank_str} " if rank_str else ""
                        disp_name = f"đ.c {rank_prefix}{name}"
                        u_name = att.get("soldierUnitName") or "Chưa rõ"
                        score = att.get("score", 0.0)
                        dur = att.get("durationSeconds", 0)
                        dur_str = f"{dur//60:02d}:{dur%60:02d}"
                        rows.append([rank, disp_name, u_name, score, dur_str])
                        
                    self.export_data_to_csv(f"DiemThi_CaNhan_{title_prefix}", headers, rows)
                else:
                    headers = ["🏆 Hạng", "Đơn vị", "Số người tham gia", "Điểm trung bình"]
                    rows = []
                    # Group by soldierId first to get best attempt of each participant
                    best_atts = {}
                    for att in exam_atts:
                        sid = att.get("soldierId")
                        if not sid:
                            continue
                        sc = float(att.get("score", 0))
                        dur = int(att.get("durationSeconds", 999999))
                        if sid not in best_atts:
                            best_atts[sid] = att
                        else:
                            existing = best_atts[sid]
                            ex_sc = float(existing.get("score", 0))
                            ex_dur = int(existing.get("durationSeconds", 999999))
                            if sc > ex_sc or (sc == ex_sc and dur < ex_dur):
                                best_atts[sid] = att

                    # Group by Unit ID using each participant's best attempt
                    unit_groups = {}
                    for att in best_atts.values():
                        uid = att.get("soldierUnitId") or "unknown"
                        if uid not in unit_groups:
                            unit_groups[uid] = []
                        unit_groups[uid].append(att)

                    # Compute stats
                    unit_stats = []
                    for uid, atts in unit_groups.items():
                        avg_score = sum(float(a.get("score", 0)) for a in atts) / len(atts)
                        u_name = atts[0].get("soldierUnitName") or unit_names.get(uid, "Khác")
                        unit_stats.append({
                            "unitId": uid,
                            "unitName": u_name,
                            "avgScore": round(avg_score, 2),
                            "count": len(atts)
                        })

                    # Sort by average score DESC
                    unit_stats.sort(key=lambda x: -x["avgScore"])
                    
                    for rank, stat in enumerate(unit_stats, 1):
                        rows.append([rank, stat["unitName"], stat["count"], stat["avgScore"]])
                        
                    self.export_data_to_csv(f"DiemThi_DonVi_{title_prefix}", headers, rows)

            export_btn = ft.IconButton(
                icon=ft.Icons.FILE_DOWNLOAD,
                icon_color=GREEN_MID,
                tooltip="Xuất Excel/CSV",
                on_click=_export_exams
            )

        # Tabs row
        tab_buttons = ft.Row(
            [
                ft.Container(
                    content=ft.Row([
                        ft.Text("📝", size=16),
                        ft.Text("Cuộc thi", size=13, weight=ft.FontWeight.BOLD if self.exams_view == "competitions" else ft.FontWeight.NORMAL)
                    ], spacing=6),
                    bgcolor=BG2 if self.exams_view == "competitions" else ft.Colors.TRANSPARENT,
                    padding=ft.padding.symmetric(horizontal=16, vertical=8),
                    border_radius=8,
                    on_click=lambda _: set_exams_tab("competitions")
                ),
                ft.Container(
                    content=ft.Row([
                        ft.Text("📊", size=16),
                        ft.Text("Bảng xếp hạng", size=13, weight=ft.FontWeight.BOLD if self.exams_view == "leaderboard" else ft.FontWeight.NORMAL)
                    ], spacing=6),
                    bgcolor=BG2 if self.exams_view == "leaderboard" else ft.Colors.TRANSPARENT,
                    padding=ft.padding.symmetric(horizontal=16, vertical=8),
                    border_radius=8,
                    on_click=lambda _: set_exams_tab("leaderboard")
                ),
            ],
            spacing=10,
            alignment=ft.MainAxisAlignment.START
        )

        if export_btn:
            tab_buttons.controls.append(ft.Container(expand=True))
            tab_buttons.controls.append(export_btn)

        content_list = ft.ListView(expand=True, spacing=12, padding=ft.padding.only(bottom=80, top=10))

        if self.exams_view == "competitions":
            # 1. Action bar for Commanders
            if is_commander:
                content_list.controls.append(
                    ft.Row([
                        ft.ElevatedButton(
                            "➕ Tạo cuộc thi mới",
                            on_click=lambda _: self.open_exam_creation_dialog(),
                            bgcolor=GREEN_MID,
                            color=ft.Colors.WHITE,
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8))
                        )
                    ], alignment=ft.MainAxisAlignment.END)
                )

            # 2. Exams List
            my_unit = my_profile.get("unitId") or ""
            # Filter exams based on scope
            visible_exams = []
            for ex in exams:
                sc = ex.get("scope") or "all"
                if isinstance(sc, list):
                    if "all" in sc or my_unit in sc or is_commander:
                        visible_exams.append(ex)
                else:
                    if sc == "all" or sc == my_unit or is_commander:
                        visible_exams.append(ex)

            if not visible_exams:
                content_list.controls.append(
                    ft.Container(
                        content=ft.Column([
                            ft.Text("📭 Chưa có cuộc thi nào diễn ra", size=14, color=TEXT_MUTED),
                            ft.Text("Các cuộc thi nhận thức sẽ xuất hiện tại đây khi Chỉ huy khởi tạo.", size=12, color=TEXT_MUTED)
                        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                        alignment=ft.alignment.center,
                        padding=ft.padding.symmetric(vertical=40)
                    )
                )
            else:
                for ex in visible_exams:
                    ex_id = ex.get("id")
                    title = ex.get("title", "Không có tiêu đề")
                    duration = ex.get("durationMinutes", 15)
                    q_count = len(ex.get("questions", []))
                    
                    # Resolve scope label for list or string
                    scopes = ex.get("scope", "all")
                    if isinstance(scopes, list):
                        if "all" in scopes:
                            sc_lbl = "Toàn trung đoàn"
                        else:
                            sc_lbl = ", ".join([unit_names.get(s, s) for s in scopes])
                    else:
                        sc_lbl = "Toàn trung đoàn" if scopes == "all" else unit_names.get(scopes, "Nội bộ")

                    # Check my attempts for this exam
                    my_atts = [a for a in attempts if a.get("examId") == ex_id and str(a.get("soldierId")) == str(my_uid)]
                    attempt_count = len(my_atts)
                    max_att = int(ex.get("maxAttempts", 1))
                    
                    creator_name = ex.get("creatorName", "")
                    creator_rank = ex.get("creatorRank", "")
                    creator_lbl = f"đ.c {creator_rank} {creator_name}" if creator_name else "Ban chỉ huy"
                    
                    max_att_lbl = "Không giới hạn" if max_att == 0 else f"{max_att} lượt"
                    
                    status_row = None
                    action_btn = None
                    
                    if my_atts:
                        best_att = max(my_atts, key=lambda x: x.get("score", 0))
                        best_score = best_att.get("score", 0)
                        status_row = ft.Row([
                            ft.Text("✅ Đã tham gia", size=12, color=GREEN_MID, weight=ft.FontWeight.W_600),
                            ft.Text(f"Điểm cao nhất: {best_score}/10 ({attempt_count}/{max_att_lbl if max_att > 0 else '∞'} lượt)", size=12, color=TEXT_MUTED)
                        ], spacing=10, wrap=True)
                        
                        if max_att == 0 or attempt_count < max_att:
                            action_btn = ft.ElevatedButton(
                                f"🔄 Thi lại ({attempt_count}/{max_att if max_att > 0 else '∞'})",
                                on_click=lambda e, exam=ex: self.open_exam_session_dialog(exam),
                                bgcolor=BG2,
                                color=ft.Colors.BLACK
                            )
                        else:
                            action_btn = ft.TextButton("Đã hết lượt thi", disabled=True)
                    else:
                        status_row = ft.Text(f"📝 Chưa tham gia (Tối đa: {max_att_lbl})", size=12, color=ft.Colors.ORANGE, weight=ft.FontWeight.W_600)
                        action_btn = ft.ElevatedButton(
                            "📝 Bắt đầu thi",
                            on_click=lambda e, exam=ex: self.open_exam_session_dialog(exam),
                            bgcolor=GREEN_MID,
                            color=ft.Colors.WHITE
                        )

                    # Delete action for Commanders
                    del_btn = None
                    if is_commander:
                        def make_del_cb(exam_id=ex_id):
                            def do_delete(e):
                                def confirm_del(_):
                                    try:
                                        FS.delete_doc(f"exams/{exam_id}")
                                        self.toast("🗑 Đã xoá cuộc thi thành công")
                                        _dlg.open = False
                                        _rebuild()
                                    except Exception as err:
                                        self.toast(f"Lỗi: {err}")
                                
                                _dlg = ft.AlertDialog(
                                    title=ft.Text("Xác nhận xoá"),
                                    content=ft.Text("Đồng chí có chắc chắn muốn xoá cuộc thi này vĩnh viễn không?"),
                                    actions=[
                                        ft.TextButton("Huỷ", on_click=lambda _: _close_dialog(self.page)),
                                        ft.ElevatedButton("Xoá", on_click=confirm_del, bgcolor=ft.Colors.RED, color=ft.Colors.WHITE)
                                    ]
                                )
                                _show_dialog(self.page, _dlg)
                            return do_delete

                        del_btn = ft.IconButton(
                            ft.Icons.DELETE_OUTLINE,
                            icon_color=ft.Colors.RED,
                            tooltip="Xoá cuộc thi này",
                            on_click=make_del_cb(ex_id)
                        )

                    card_content = ft.Container(
                        content=ft.Column([
                            ft.Row([
                                ft.Column([
                                    ft.Text(title, size=15, weight=ft.FontWeight.BOLD),
                                    ft.Text(f"⏱ Thời gian: {duration} phút  •  📝 Số câu: {q_count} câu  •  🌐 Phạm vi: {sc_lbl}  •  🔄 Lượt thi: {max_att_lbl}", size=11, color=TEXT_MUTED),
                                    ft.Text(f"✍️ Tạo bởi: {creator_lbl}", size=11, color=TEXT_MUTED)
                                ], expand=True),
                                *([del_btn] if del_btn else [])
                            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                            ft.Divider(height=1, color=ft.Colors.GREY_300),
                            ft.Row([
                                status_row,
                                action_btn
                            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True)
                        ], spacing=10),
                        bgcolor=BG2,
                        padding=14,
                        border_radius=10,
                        border=ft.border.all(1, ft.Colors.GREY_300)
                    )
                    content_list.controls.append(card_content)

        elif self.exams_view == "leaderboard":
            if not exams:
                content_list.controls.append(
                    ft.Container(
                        content=ft.Text("📭 Chưa có cuộc thi nào để xem bảng xếp hạng.", size=13, color=TEXT_MUTED),
                        alignment=ft.alignment.center,
                        padding=ft.padding.symmetric(vertical=40)
                    )
                )
            else:
                if not self.selected_exam_id or self.selected_exam_id not in [ex.get("id") for ex in exams]:
                    self.selected_exam_id = exams[0].get("id")

                selected_exam = next((ex for ex in exams if ex.get("id") == self.selected_exam_id), exams[0])

                # Exam selector Dropdown
                exam_options = [ft.dropdown.Option(ex.get("id"), ex.get("title")) for ex in exams]
                def on_exam_select_change(e):
                    self.selected_exam_id = exam_dd.value
                    _rebuild()

                exam_dd = ft.Dropdown(
                    label="Chọn cuộc thi",
                    value=self.selected_exam_id,
                    options=exam_options,
                    on_change=on_exam_select_change,
                    dense=True, border_radius=8
                )

                content_list.controls.append(exam_dd)

                # Filter attempts for this exam
                exam_atts = [a for a in attempts if a.get("examId") == self.selected_exam_id]

                # Tabs within Leaderboard: Cá nhân vs Đơn vị
                if not hasattr(self, "leaderboard_sub_tab"):
                    self.leaderboard_sub_tab = "individual"

                def set_sub_tab(sub_tab: str):
                    self.leaderboard_sub_tab = sub_tab
                    _rebuild()

                sub_tabs_row = ft.Row([
                    ft.TextButton(
                        "👤 Xếp hạng cá nhân",
                        on_click=lambda _: set_sub_tab("individual"),
                        style=ft.ButtonStyle(
                            color=GREEN_MID if self.leaderboard_sub_tab == "individual" else TEXT_MUTED,
                        )
                    ),
                    ft.TextButton(
                        "🏢 Xếp hạng đơn vị",
                        on_click=lambda _: set_sub_tab("unit"),
                        style=ft.ButtonStyle(
                            color=GREEN_MID if self.leaderboard_sub_tab == "unit" else TEXT_MUTED,
                        )
                    )
                ], spacing=10)

                content_list.controls.append(sub_tabs_row)

                if self.leaderboard_sub_tab == "individual":
                    # Group by soldierId to get best attempt of each participant (lấy điểm cao nhất)
                    best_atts = {}
                    for att in exam_atts:
                        sid = att.get("soldierId")
                        if not sid:
                            continue
                        sc = float(att.get("score", 0))
                        dur = int(att.get("durationSeconds", 999999))
                        if sid not in best_atts:
                            best_atts[sid] = att
                        else:
                            existing = best_atts[sid]
                            ex_sc = float(existing.get("score", 0))
                            ex_dur = int(existing.get("durationSeconds", 999999))
                            if sc > ex_sc or (sc == ex_sc and dur < ex_dur):
                                best_atts[sid] = att

                    filtered_atts = list(best_atts.values())
                    filtered_atts.sort(key=lambda x: (-float(x.get("score", 0)), int(x.get("durationSeconds", 99999))))

                    if not filtered_atts:
                        content_list.controls.append(
                            ft.Container(
                                content=ft.Text("📭 Chưa có ai tham gia cuộc thi này.", size=12, color=TEXT_MUTED),
                                alignment=ft.alignment.center,
                                padding=ft.padding.symmetric(vertical=20)
                            )
                        )
                    else:
                        table_rows = []
                        for rank, att in enumerate(filtered_atts, 1):
                            name = att.get("soldierName", "Quân nhân")
                            rank_str = (att.get("soldierRank") or "").strip()
                            rank_prefix = f"{rank_str} " if rank_str else ""
                            disp_name = f"đ.c {rank_prefix}{name}"
                            u_name = att.get("soldierUnitName") or "Chưa rõ"
                            score = att.get("score", 0.0)
                            dur = att.get("durationSeconds", 0)
                            dur_str = f"{dur//60:02d}:{dur%60:02d}"

                            rank_emoji = "🏆" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f" {rank} "

                            table_rows.append(
                                ft.Container(
                                    content=ft.Row([
                                        ft.Text(rank_emoji, size=13, weight=ft.FontWeight.BOLD, width=30),
                                        ft.Column([
                                            ft.Text(disp_name, size=13, weight=ft.FontWeight.W_600),
                                            ft.Text(u_name, size=11, color=TEXT_MUTED)
                                        ], expand=True, spacing=1),
                                        ft.Text(f"{score} điểm", size=13, weight=ft.FontWeight.BOLD, color=GREEN_MID, width=70, text_align=ft.TextAlign.RIGHT),
                                        ft.Text(dur_str, size=12, color=TEXT_MUTED, width=50, text_align=ft.TextAlign.RIGHT)
                                    ]),
                                    padding=ft.padding.symmetric(horizontal=10, vertical=6),
                                    bgcolor=BG2, border_radius=6
                                )
                            )
                        content_list.controls.extend(table_rows)

                elif self.leaderboard_sub_tab == "unit":
                    # Group by soldierId first to get best attempt of each participant
                    best_atts = {}
                    for att in exam_atts:
                        sid = att.get("soldierId")
                        if not sid:
                            continue
                        sc = float(att.get("score", 0))
                        dur = int(att.get("durationSeconds", 999999))
                        if sid not in best_atts:
                            best_atts[sid] = att
                        else:
                            existing = best_atts[sid]
                            ex_sc = float(existing.get("score", 0))
                            ex_dur = int(existing.get("durationSeconds", 999999))
                            if sc > ex_sc or (sc == ex_sc and dur < ex_dur):
                                best_atts[sid] = att

                    # Group by Unit ID using each participant's best attempt
                    unit_groups = {}
                    for att in best_atts.values():
                        uid = att.get("soldierUnitId") or "unknown"
                        if uid not in unit_groups:
                            unit_groups[uid] = []
                        unit_groups[uid].append(att)

                    # Compute stats
                    unit_stats = []
                    for uid, atts in unit_groups.items():
                        avg_score = sum(float(a.get("score", 0)) for a in atts) / len(atts)
                        u_name = atts[0].get("soldierUnitName") or unit_names.get(uid, "Khác")
                        unit_stats.append({
                            "unitId": uid,
                            "unitName": u_name,
                            "avgScore": round(avg_score, 2),
                            "count": len(atts)
                        })

                    # Sort by average score DESC
                    unit_stats.sort(key=lambda x: -x["avgScore"])

                    if not unit_stats:
                        content_list.controls.append(
                            ft.Container(
                                content=ft.Text("📭 Chưa có số liệu đơn vị.", size=12, color=TEXT_MUTED),
                                alignment=ft.alignment.center,
                                padding=ft.padding.symmetric(vertical=20)
                            )
                        )
                    else:
                        # Integrate BarChart for exams average scores
                        chart_container = []
                        chart_bars = []
                        legend_items = []
                        colors = [str(GREEN_DARK), str(GREEN_MID), str(AMBER), str(BLUE), str(PURPLE)]
                        for idx, stat in enumerate(unit_stats[:5]):
                            avg_score = stat["avgScore"]
                            col = colors[idx % len(colors)]
                            chart_bars.append(
                                ft.BarChartGroup(
                                    x=idx,
                                    bar_rods=[
                                        ft.BarChartRod(
                                            from_y=0,
                                            to_y=avg_score,
                                            width=18,
                                            color=col,
                                            border_radius=4,
                                        )
                                    ],
                                )
                            )
                            unit_short = stat["unitName"]
                            if len(unit_short) > 12:
                                unit_short = unit_short[:10] + ".."
                            legend_items.append(
                                ft.Row([
                                    ft.Container(width=10, height=10, border_radius=5, bgcolor=col),
                                    ft.Text(f"{unit_short} ({avg_score}đ)", size=10, weight=ft.FontWeight.W_500),
                                ], spacing=4, tight=True)
                            )
                        
                        bar_chart = ft.BarChart(
                            bar_groups=chart_bars,
                            height=120,
                            horizontal_grid_lines=ft.ChartGridLines(
                                color=ft.Colors.with_opacity(0.1, "#000000"),
                                width=1,
                                dash_pattern=[3, 3],
                            ),
                            max_y=10.0,
                        )
                        
                        chart_container.append(
                            ft.Container(
                                content=ft.Column([
                                    ft.Text("BIỂU ĐỒ ĐIỂM TRUNG BÌNH TOP 5 ĐƠN VỊ", size=10, weight=ft.FontWeight.BOLD, color=TEXT_MUTED),
                                    ft.Container(content=bar_chart, padding=ft.padding.symmetric(vertical=6)),
                                    ft.Row(legend_items, wrap=True, alignment=ft.MainAxisAlignment.CENTER, spacing=10),
                                ], spacing=6),
                                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
                                padding=12, margin=ft.margin.only(bottom=12),
                            )
                        )
                        
                        table_rows = []
                        for rank, stat in enumerate(unit_stats, 1):
                            rank_emoji = "🏆" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f" {rank} "
                            table_rows.append(
                                ft.Container(
                                    content=ft.Row([
                                        ft.Text(rank_emoji, size=13, weight=ft.FontWeight.BOLD, width=30),
                                        ft.Column([
                                            ft.Text(stat["unitName"], size=13, weight=ft.FontWeight.W_600),
                                            ft.Text(f"Số người tham gia: {stat['count']}", size=11, color=TEXT_MUTED)
                                        ], expand=True, spacing=1),
                                        ft.Text(f"TB: {stat['avgScore']} điểm", size=13, weight=ft.FontWeight.BOLD, color=GREEN_MID, width=120, text_align=ft.TextAlign.RIGHT)
                                    ]),
                                    padding=ft.padding.symmetric(horizontal=10, vertical=6),
                                    bgcolor=BG2, border_radius=6
                                )
                            )
                        content_list.controls.extend(chart_container)
                        content_list.controls.extend(table_rows)

        # Build final view container
        return ft.Container(
            content=ft.Column(
                [
                    tab_buttons,
                    content_list
                ],
                spacing=10,
                expand=True
            ),
            padding=ft.padding.symmetric(horizontal=12, vertical=8),
            expand=True
        )

    def open_exam_creation_dialog(self) -> None:
        import uuid
        page = self.page
        
        # Build unit list options (platoon level and above: exclude squads)
        unit_types = {}
        def walk_types(n):
            unit_types[n["id"]] = n.get("type", "")
            for c in n.get("children", []):
                walk_types(c)
        walk_types(store.get("units", store.seed_units))

        # Checkboxes for scopes
        scope_checkboxes = []
        all_cb = ft.Checkbox(label="Toàn trung đoàn", value=True)
        scope_checkboxes.append(all_cb)
        
        unit_checkboxes = []
        for uid, lbl in store.flatten_units_for_select(store.get("units", store.seed_units)):
            if unit_types.get(uid) != "squad":
                cb = ft.Checkbox(label=lbl.strip(), value=False, data=uid)
                unit_checkboxes.append(cb)
                scope_checkboxes.append(cb)

        def on_all_changed(e):
            if all_cb.value:
                for cb in unit_checkboxes:
                    cb.value = False
                    cb.disabled = True
            else:
                for cb in unit_checkboxes:
                    cb.disabled = False
            page.update()
            
        all_cb.on_change = on_all_changed

        # Initially disable other checkboxes because all_cb is True
        for cb in unit_checkboxes:
            cb.disabled = True

        scope_container = ft.Container(
            content=ft.Column(scope_checkboxes, spacing=4, scroll=ft.ScrollMode.ALWAYS),
            height=120,
            border=ft.border.all(1, ft.Colors.GREY_300),
            border_radius=8,
            padding=8
        )

        title_input = ft.TextField(label="Tên cuộc thi *", dense=True, border_radius=8)
        duration_input = ft.TextField(label="Thời gian thi (phút) *", value="15", keyboard_type=ft.KeyboardType.NUMBER, dense=True, border_radius=8)
        max_attempts_dd = ft.Dropdown(
            label="Số lần thi tối đa *",
            value="1",
            options=[
                ft.dropdown.Option("1", "1 lượt"),
                ft.dropdown.Option("2", "2 lượt"),
                ft.dropdown.Option("3", "3 lượt"),
                ft.dropdown.Option("5", "5 lượt"),
                ft.dropdown.Option("0", "Không giới hạn")
            ],
            dense=True, border_radius=8
        )

        local_questions = []

        added_questions_col = ft.Column(spacing=6)

        def rebuild_questions_ui():
            added_questions_col.controls.clear()
            if not local_questions:
                added_questions_col.controls.append(ft.Text("Chưa có câu hỏi nào được thêm.", size=11, color=TEXT_MUTED))
            else:
                for idx, q in enumerate(local_questions, 1):
                    def make_del_fn(index=idx-1):
                        def do_del(_):
                            local_questions.pop(index)
                            rebuild_questions_ui()
                        return do_del

                    added_questions_col.controls.append(
                        ft.Container(
                            content=ft.Row([
                                ft.Text(f"{idx}. {q['text'][:40]}...", size=12, expand=True),
                                ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_color=ft.Colors.RED, icon_size=16, on_click=make_del_fn(idx-1))
                            ]),
                            padding=4, bgcolor="#f5f5f5", border_radius=4
                        )
                    )
            page.update()

        rebuild_questions_ui()

        # Questions inputs
        q_text = ft.TextField(label="Nhập câu hỏi *", dense=True, border_radius=8, multiline=True, min_lines=1, max_lines=2)
        ans_correct = ft.TextField(label="Đáp án ĐÚNG (mặc định ô này) *", dense=True, border_radius=8)
        ans_wrong_1 = ft.TextField(label="Đáp án sai 1 *", dense=True, border_radius=8)
        ans_wrong_2 = ft.TextField(label="Đáp án sai 2 *", dense=True, border_radius=8)
        ans_wrong_3 = ft.TextField(label="Đáp án sai 3 *", dense=True, border_radius=8)

        def add_question(_):
            q_val = q_text.value.strip()
            correct_val = ans_correct.value.strip()
            w1 = ans_wrong_1.value.strip()
            w2 = ans_wrong_2.value.strip()
            w3 = ans_wrong_3.value.strip()

            if not q_val or not correct_val or not w1 or not w2 or not w3:
                self.toast("❌ Vui lòng điền đủ thông tin câu hỏi và 4 đáp án")
                return

            local_questions.append({
                "id": f"q_{len(local_questions)+1}",
                "text": q_val,
                "correctAnswer": correct_val,
                "wrongAnswers": [w1, w2, w3]
            })

            # Clear inputs
            q_text.value = ""
            ans_correct.value = ""
            ans_wrong_1.value = ""
            ans_wrong_2.value = ""
            ans_wrong_3.value = ""
            
            rebuild_questions_ui()

        add_q_btn = ft.ElevatedButton("➕ Thêm câu hỏi", on_click=add_question, bgcolor=GREEN_MID, color=ft.Colors.WHITE)

        error_text = ft.Text("", color=ft.Colors.RED, size=12)

        def save_exam(_):
            title = title_input.value.strip()
            duration_val = duration_input.value.strip()

            if not title or not duration_val:
                error_text.value = "❌ Vui lòng điền đủ Tên cuộc thi và Thời gian thi"
                page.update()
                return

            try:
                duration_min = int(duration_val)
            except ValueError:
                error_text.value = "❌ Thời gian thi phải là số nguyên"
                page.update()
                return

            if not local_questions:
                error_text.value = "❌ Cuộc thi phải có ít nhất 1 câu hỏi"
                page.update()
                return

            # Gather selected scopes
            selected_scopes = []
            if all_cb.value:
                selected_scopes = ["all"]
            else:
                selected_scopes = [cb.data for cb in unit_checkboxes if cb.value]
                if not selected_scopes:
                    error_text.value = "❌ Vui lòng chọn ít nhất một đơn vị phạm vi cuộc thi"
                    page.update()
                    return

            try:
                exam_id = uuid.uuid4().hex
                my_profile = store.get("userProfile", store.seed_user_profile)
                my_uid = my_profile.get("id") or AUTH_STATE.get("localId")

                doc_data = {
                    "id": exam_id,
                    "title": title,
                    "scope": selected_scopes,
                    "durationMinutes": duration_min,
                    "maxAttempts": int(max_attempts_dd.value),
                    "questions": local_questions,
                    "createdBy": my_uid,
                    "creatorName": my_profile.get("name", "Cán bộ"),
                    "creatorRank": my_profile.get("rank", "Chỉ huy"),
                    "createdAt": store.now_ms(),
                    "status": "active"
                }

                FS.set_doc(f"exams/{exam_id}", doc_data)
                self.toast("🎉 Đã tạo cuộc thi thành công!")
                _dlg.open = False
                
                # Rebuild current view
                self.body.content = self.module_exams()
                self.refresh()
            except Exception as err:
                error_text.value = f"❌ Lỗi: {err}"
                page.update()

        dlg_content = ft.ListView(
            [
                error_text,
                ft.Text("Thông tin chung", weight=ft.FontWeight.BOLD),
                title_input, duration_input, max_attempts_dd,
                ft.Text("Phạm vi cuộc thi (chọn nhiều) *", size=12, color=TEXT_MUTED),
                scope_container,
                ft.Divider(height=1),
                ft.Text("Danh sách câu hỏi đã thêm", weight=ft.FontWeight.BOLD),
                added_questions_col,
                ft.Divider(height=1),
                ft.Text("Tạo câu hỏi mới", weight=ft.FontWeight.BOLD),
                q_text, ans_correct, ans_wrong_1, ans_wrong_2, ans_wrong_3,
                add_q_btn
            ],
            spacing=10,
            height=450,
        )

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Tạo cuộc thi nhận thức mới", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Container(content=dlg_content, width=380),
            actions=[
                ft.TextButton("Hủy", on_click=lambda e: _close_dialog(self.page)),
                ft.ElevatedButton("Lưu cuộc thi", on_click=save_exam, bgcolor=GREEN_MID, color=ft.Colors.WHITE)
            ],
        )
        _show_dialog(self.page, _dlg)

    def open_exam_session_dialog(self, exam: dict) -> None:
        import random
        import uuid
        import threading
        import time

        page = self.page
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_uid = my_profile.get("id") or AUTH_STATE.get("localId")
        unit_names = self._unit_name_map(store.get("units", store.seed_units))

        exam_active = [False]
        remaining_seconds = exam.get("durationMinutes", 15) * 60
        answers_selected = {}

        # 1. Prep shuffled choices for all questions
        shuffled_choices = {}
        for q in exam.get("questions", []):
            choices = [q["correctAnswer"]] + q["wrongAnswers"]
            random.shuffle(choices)
            shuffled_choices[q["id"]] = choices

        # UI elements
        timer_text = ft.Text("⏱ Thời gian còn lại: --:--", size=15, weight=ft.FontWeight.BOLD, color=GREEN_MID)

        def do_submit():
            exam_active[0] = False
            # Close active session dialog
            try:
                _close_dialog(self.page)
            except:
                pass

            # Compute results
            questions = exam.get("questions", [])
            total = len(questions)
            correct_cnt = 0
            for q in questions:
                sel = answers_selected.get(q["id"])
                if sel == q["correctAnswer"]:
                    correct_cnt += 1

            score = 0.0
            if total > 0:
                score = (correct_cnt / total) * 10.0

            started_ms = getattr(page, "exam_started_at", store.now_ms())
            completed_ms = store.now_ms()
            duration_secs = (completed_ms - started_ms) // 1000

            # Write to Firestore
            attempt_doc = {}
            try:
                attempt_id = uuid.uuid4().hex
                attempt_doc = {
                    "id": attempt_id,
                    "examId": exam["id"],
                    "examTitle": exam["title"],
                    "soldierId": my_uid,
                    "soldierName": my_profile.get("name") or "Quân nhân",
                    "soldierRank": my_profile.get("rank") or "",
                    "soldierUnitId": my_profile.get("unitId") or "",
                    "soldierUnitName": unit_names.get(my_profile.get("unitId"), ""),
                    "correctCount": correct_cnt,
                    "totalQuestions": total,
                    "score": round(score, 1),
                    "startedAt": started_ms,
                    "completedAt": completed_ms,
                    "durationSeconds": int(duration_secs)
                }
                FS.set_doc(f"exam_attempts/{attempt_id}", attempt_doc)
                self.toast("🎉 Đã nộp bài thi thành công!")
            except Exception as err:
                self.toast(f"Lỗi lưu bài thi: {err}")

            # Calculate user's current rank in this exam session
            user_rank = 1
            total_participants = 1
            try:
                # Fetch all attempts for this exam
                all_attempts = FS.list_collection("exam_attempts") or []
                # Ensure the new attempt is in the list
                if attempt_doc and not any(a.get("id") == attempt_doc.get("id") for a in all_attempts):
                    all_attempts.append(attempt_doc)
                
                exam_attempts = [a for a in all_attempts if a.get("examId") == exam["id"]]
                
                # Group by soldierId, taking the highest score attempt of each participant
                best_attempts = {}
                for a in exam_attempts:
                    sid = a.get("soldierId")
                    if not sid:
                        continue
                    sc = float(a.get("score", 0))
                    dur = int(a.get("durationSeconds", 999999))
                    if sid not in best_attempts:
                        best_attempts[sid] = a
                    else:
                        existing = best_attempts[sid]
                        ex_sc = float(existing.get("score", 0))
                        ex_dur = int(existing.get("durationSeconds", 999999))
                        if sc > ex_sc or (sc == ex_sc and dur < ex_dur):
                            best_attempts[sid] = a
                
                leaderboard_list = list(best_attempts.values())
                leaderboard_list.sort(key=lambda a: (-float(a.get("score", 0)), int(a.get("durationSeconds", 999999))))
                
                total_participants = len(leaderboard_list)
                for i, a in enumerate(leaderboard_list):
                    if str(a.get("soldierId")) == str(my_uid):
                        user_rank = i + 1
                        break
            except Exception as err:
                pass

            # Rebuild view
            self.body.content = self.module_exams()
            self.refresh()

            # Show congratulations result dialog
            mins, secs = divmod(duration_secs, 60)
            res_content = ft.Column([
                ft.Text("🎉 ĐỒNG CHÍ ĐÃ HOÀN THÀNH BÀI THI", size=14, weight=ft.FontWeight.BOLD, color=GREEN_MID),
                ft.Text(f"• Số câu trả lời đúng: {correct_cnt} / {total}", size=13),
                ft.Text(f"• Điểm số: {score:.1f} điểm / 10.0", size=15, weight=ft.FontWeight.BOLD, color=GREEN_MID),
                ft.Text(f"• Thời gian làm bài: {mins:02d} phút {secs:02d} giây", size=13),
                ft.Divider(height=1),
                ft.Text(f"🏆 Thứ hạng của đồng chí: Hạng {user_rank} / {total_participants} trong cuộc thi này!", size=13, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE),
            ], spacing=10, tight=True)

            _dlg = ft.AlertDialog(
                title=ft.Text("Kết quả thi nhận thức"),
                content=ft.Container(content=res_content, padding=10),
                actions=[
                    ft.ElevatedButton("Xác nhận", on_click=lambda _: _close_dialog(self.page), bgcolor=GREEN_MID, color=ft.Colors.WHITE)
                ]
            )
            _show_dialog(self.page, _dlg)

        # Timer loop function running on background thread
        def timer_loop():
            nonlocal remaining_seconds
            while remaining_seconds > 0 and exam_active[0]:
                time.sleep(1)
                remaining_seconds -= 1
                try:
                    mins, secs = divmod(remaining_seconds, 60)
                    timer_text.value = f"⏱ Thời gian còn lại: {mins:02d}:{secs:02d}"
                    if remaining_seconds <= 120:
                        timer_text.color = ft.Colors.RED
                    page.update()
                except:
                    break
            if remaining_seconds <= 0 and exam_active[0]:
                do_submit()

        # Start Screen UI before doing exam
        info_col = ft.Column([
            ft.Text(exam.get("title"), size=15, weight=ft.FontWeight.BOLD),
            ft.Text(f"⏱ Thời gian làm bài: {exam.get('durationMinutes')} phút", size=13),
            ft.Text(f"📝 Số lượng câu hỏi: {len(exam.get('questions', []))} câu", size=13),
            ft.Text("⚠️ Lưu ý: Khi ấn bắt đầu, đồng hồ đếm ngược sẽ chạy ngay lập tức. Hết giờ hệ thống sẽ tự nộp bài thi.", size=12, color=ft.Colors.RED_ACCENT)
        ], spacing=10, tight=True)

        def start_exam_session(_):
            exam_active[0] = True
            page.exam_started_at = store.now_ms()

            # Build scrollable test sheet
            sheet_items = [timer_text, ft.Divider(height=1)]

            for idx, q in enumerate(exam.get("questions", []), 1):
                choices = shuffled_choices[q["id"]]
                
                # We can use ft.RadioGroup to choose answers!
                radio_options = []
                for c in choices:
                    radio_options.append(ft.Radio(value=c, label=c))

                def make_select_fn(question_id=q["id"]):
                    def on_choice_change(e):
                        answers_selected[question_id] = e.control.value
                    return on_choice_change

                rg = ft.RadioGroup(
                    content=ft.Column(radio_options, spacing=6),
                    on_change=make_select_fn(q["id"])
                )

                sheet_items.append(
                    ft.Container(
                        content=ft.Column([
                            ft.Text(f"Câu {idx}: {q['text']}", size=13, weight=ft.FontWeight.W_600),
                            rg
                        ], spacing=8),
                        bgcolor="#fafafa", border_radius=6, padding=10, border=ft.border.all(1, ft.Colors.GREY_300)
                    )
                )

            # Scrollable container for choices
            scroll_sheet = ft.ListView(sheet_items, spacing=12, height=450)

            # Replace dialog content with the live sheet
            _dlg.title = ft.Text("Bài thi Nhận thức đang diễn ra", size=15, weight=ft.FontWeight.BOLD)
            _dlg.content = ft.Container(content=scroll_sheet, width=380)
            _dlg.actions = [
                ft.ElevatedButton("Nộp bài thi", on_click=lambda _: do_submit(), bgcolor=GREEN_MID, color=ft.Colors.WHITE)
            ]
            page.update()

            # Start timer background thread
            threading.Thread(target=timer_loop, daemon=True).start()

        _dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Bắt đầu bài thi", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Container(content=info_col, width=350),
            actions=[
                ft.TextButton("Hủy", on_click=lambda e: _close_dialog(self.page)),
                ft.ElevatedButton("Bắt đầu làm bài", on_click=start_exam_session, bgcolor=GREEN_MID, color=ft.Colors.WHITE)
            ],
        )
        _show_dialog(self.page, _dlg)

    def mount(self) -> None:
        self.frame = ft.Column(spacing=0, expand=True)
        self.page.add(self.frame)
        self.page.on_resized = lambda e: self.refresh()
        self.set_tab("home")


def _all_unit_ids(node: dict) -> list[str]:
    ids = [node["id"]]
    for c in node.get("children", []):
        ids.extend(_all_unit_ids(c))
    return ids


# ============================================================
# ===== LOGIN + ROUTER                                    =====
# ============================================================

REMEMBER_FILE = store.DATA_DIR / "remember_login.json"


def _remember_login_save(username: str, password: str) -> None:
    """Lưu username + password (base64) để auto-fill / đăng nhập nhanh.

    LƯU Ý: password được base64 — KHÔNG phải mã hoá. Chỉ dùng cho convenience.
    File nằm trong app data dir của OS, chỉ user hiện tại đọc được.
    """
    import base64 as _b64
    import json as _json
    try:
        REMEMBER_FILE.write_text(_json.dumps({
            "username": username,
            "password_b64": _b64.b64encode(password.encode("utf-8")).decode("ascii"),
            "remember_password": True,
        }), encoding="utf-8")
    except Exception:
        pass


def _remember_login_load() -> dict | None:
    import base64 as _b64
    import json as _json
    if not REMEMBER_FILE.exists():
        return None
    try:
        d = _json.loads(REMEMBER_FILE.read_text(encoding="utf-8"))
        if d.get("password_b64"):
            try:
                d["password"] = _b64.b64decode(d["password_b64"].encode()).decode("utf-8")
            except Exception:
                d["password"] = ""
        return d
    except Exception:
        return None


def _remember_login_clear() -> None:
    try:
        REMEMBER_FILE.unlink()
    except FileNotFoundError:
        pass


def show_login(page: ft.Page) -> None:
    """Login + Signup form."""
    import random as _random
    page.controls.clear()
    page.bgcolor = GREEN_DARK

    mode = {"value": "login"}
    units_tree = store.get("units", store.seed_units)
    unit_options = store.flatten_units_for_select(units_tree)

    user_input = ft.TextField(label="Số định danh quân nhân (vd: e141009)", value="",
                              border_radius=10, dense=True,
                              on_submit=lambda e: do_submit(e))
    pass_input = ft.TextField(label="Mật khẩu", password=True, can_reveal_password=True,
                              border_radius=10, dense=True,
                              on_submit=lambda e: do_submit(e))

    name_input = ft.TextField(label="Họ và tên *", border_radius=10, dense=True,
                              visible=False, on_submit=lambda e: do_submit(e))
    unit_dd = ft.Dropdown(
        label="Đơn vị *",
        options=[ft.dropdown.Option(key, text) for key, text in unit_options],
        border_radius=10, dense=True, visible=False,
    )
    title_dd = ft.Dropdown(
        label="Chức danh * (chọn đơn vị trước)",
        options=[],
        value=None,
        border_radius=10, dense=True, visible=False,
        disabled=True,
    )

    def _on_unit_changed(e=None):
        """Khi chọn đơn vị → lọc danh sách chức danh phù hợp."""
        uid = unit_dd.value or ""
        if uid:
            relevant = store.titles_for_unit(units_tree, uid)
            title_dd.options = [ft.dropdown.Option(t) for t in relevant]
            title_dd.label = "Chức danh *"
            title_dd.disabled = False
            if title_dd.value not in relevant:
                title_dd.value = relevant[0] if relevant else "Chiến sĩ"
        else:
            title_dd.options = []
            title_dd.value = None
            title_dd.label = "Chức danh * (chọn đơn vị trước)"
            title_dd.disabled = True
        try:
            page.update()
        except Exception:
            pass

    unit_dd.on_change = _on_unit_changed

    rank_dd = ft.Dropdown(
        label="Cấp bậc *",
        options=[ft.dropdown.Option(r) for r in store.RANKS],
        value="Binh nhì",
        border_radius=10, dense=True, visible=False,
    )
    phone_input = ft.TextField(label="Số điện thoại", border_radius=10, dense=True,
                               visible=False, keyboard_type=ft.KeyboardType.PHONE,
                               on_submit=lambda e: do_submit(e))
    hometown_input = ft.TextField(label="Quê quán", border_radius=10, dense=True,
                                  visible=False, on_submit=lambda e: do_submit(e))
    gen_username = ft.Text("", size=14, weight=ft.FontWeight.BOLD,
                           color=GREEN_DARK)

    def reroll_username(e=None):
        n = _random.randint(2, 999)
        gen_username.value = f"e141{n:03d}"
        try:
            page.update()
        except Exception:
            pass

    gen_box = ft.Container(
        content=ft.Column([
            ft.Text("Số định danh được cấp:", size=11, color=TEXT_MUTED),
            ft.Row([
                gen_username,
                ft.IconButton(ft.Icons.REFRESH, tooltip="Tạo số khác",
                              icon_color=GREEN_MID, on_click=reroll_username),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
        ], spacing=2, tight=True),
        bgcolor="#f0f9f4", border_radius=8,
        padding=ft.padding.symmetric(horizontal=10, vertical=6),
        border=ft.border.all(1, "#c8e6c9"),
        visible=False,
    )

    def random_username() -> str:
        return f"e141{_random.randint(2, 999):03d}"

    err_text = ft.Text("", color=RED, size=12)
    busy = ft.ProgressRing(visible=False, width=18, height=18, stroke_width=2)

    remember_pw_cb = ft.Checkbox(label="Ghi nhớ mật khẩu", value=False)

    def do_biometric(e=None):
        """Đăng nhập nhanh: lấy creds đã lưu, tự submit."""
        prefs = _remember_login_load()
        if not prefs or not prefs.get("password"):
            err_text.value = "⚠️ Chưa có mật khẩu đã lưu. Hãy đăng nhập 1 lần và tích 'Ghi nhớ mật khẩu'."
            page.update(); return
        user_input.value = prefs.get("username") or ""
        pass_input.value = prefs.get("password") or ""
        remember_pw_cb.value = True
        page.update()
        do_submit(None)

    biometric_btn = ft.IconButton(
        ft.Icons.FINGERPRINT, icon_size=28, icon_color=GREEN_MID,
        tooltip="Đăng nhập nhanh (Face/Vân tay)",
        on_click=do_biometric,
    )

    submit_btn = ft.ElevatedButton(
        "🔐 Đăng nhập",
        on_click=lambda e: do_submit(e), width=220,
        style=ft.ButtonStyle(
            bgcolor=GREEN_MID, color=ft.Colors.WHITE,
            padding=14, text_style=ft.TextStyle(size=14, weight=ft.FontWeight.BOLD),
            shape=ft.RoundedRectangleBorder(radius=10),
        ),
    )
    toggle_link = ft.TextButton(
        "Chưa có tài khoản? Đăng ký",
        on_click=lambda e: toggle_mode(),
    )
    forgot_link = ft.TextButton(
        "Quên mật khẩu?",
        on_click=lambda e: do_forgot(),
    )

    def toggle_mode():
        if mode["value"] == "login":
            mode["value"] = "signup"
            submit_btn.text = "📝 Đăng ký"
            toggle_link.text = "Đã có tài khoản? Đăng nhập"
            user_input.visible = False
            for c in (name_input, rank_dd, title_dd, unit_dd, phone_input, hometown_input, gen_box):
                c.visible = True
            if not gen_username.value:
                gen_username.value = random_username()
        else:
            mode["value"] = "login"
            submit_btn.text = "🔐 Đăng nhập"
            toggle_link.text = "Chưa có tài khoản? Đăng ký"
            user_input.visible = True
            for c in (name_input, rank_dd, title_dd, unit_dd, phone_input, hometown_input, gen_box):
                c.visible = False
        err_text.value = ""
        page.update()

    def set_busy(b: bool):
        busy.visible = b
        submit_btn.disabled = b
        toggle_link.disabled = b
        forgot_link.disabled = b
        page.update()

    def do_submit(e):
        p = (pass_input.value or "").strip()
        if mode["value"] == "login":
            u = (user_input.value or "").strip()
            if not u or not p:
                err_text.value = "⚠️ Vui lòng nhập đầy đủ"
                page.update(); return
            if len(p) < 6:
                err_text.value = "⚠️ Mật khẩu tối thiểu 6 ký tự"
                page.update(); return
            err_text.value = ""
            set_busy(True)

            def login_worker():
                def _do_login_once() -> dict:
                    """Thực hiện 1 lần login, ném exception nếu lỗi."""
                    return firebase_auth.login_with_username(u, p)

                def _after_login(creds: dict):
                    """Xử lý sau khi đăng nhập thành công."""
                    _set_auth(creds, username=u)
                    try:
                        (store.DATA_DIR / "remember_user.txt").write_text(u)
                    except Exception:
                        pass
                    if remember_pw_cb.value:
                        _remember_login_save(u, p)
                    else:
                        _remember_login_clear()
                    # Backup profile TRƯỚC KHI purge để làm fallback nếu GET thất bại
                    _profile_bak: dict = {}
                    try:
                        _profile_bak = dict(store.STORE._load().get("userProfile") or {})
                    except Exception:
                        pass
                    try:
                        store.STORE.purge_keys(["soldiers", "units", "userProfile", "chat_rooms"])
                        store.STORE.sync_from_firestore()
                        store.refresh_soldiers_from_users()
                    except Exception:
                        pass
                    # _hydrate_profile_after_login đảm bảo:
                    #   • trường "id" luôn có trong userProfile
                    #   • isAdmin/adminLevel lấy từ MongoDB + fallback soldiers cache
                    #   • accountStatus đúng từ MongoDB (không ghi đè)
                    #   • ghi lastLoginAt lên server
                    _hydrate_profile_after_login(creds, u, _profile_bak)
                    def go():
                        set_busy(False); show_app(page)
                    page.run_thread(go) if hasattr(page, "run_thread") else go()

                # --- Thử lần 1 ---
                try:
                    creds = _do_login_once()
                    _after_login(creds)
                    return
                except FirebaseAuthError as fe:
                    # Server đang ngủ (Render free tier) → hiện countdown và retry
                    if "SERVER_WAKING" in (fe.code or ""):
                        WAIT_SECS = 40
                        for remaining in range(WAIT_SECS, 0, -1):
                            def _update_msg(r=remaining):
                                err_text.value = (
                                    f"⏳ Server đang khởi động, tự thử lại sau {r}s..."
                                )
                                try:
                                    page.update()
                                except Exception:
                                    pass
                            page.run_thread(_update_msg) if hasattr(page, "run_thread") else _update_msg()
                            time.sleep(1)
                        # --- Thử lần 2 sau khi chờ ---
                        try:
                            creds = _do_login_once()
                            _after_login(creds)
                            return
                        except FirebaseAuthError as fe2:
                            msg = friendly_error(fe2)
                            def fail2(m=msg):
                                set_busy(False)
                                err_text.value = f"❌ {m}"
                                page.update()
                            page.run_thread(fail2) if hasattr(page, "run_thread") else fail2()
                            return
                        except Exception as ex2:
                            def fail2(m=str(ex2)):
                                set_busy(False)
                                err_text.value = f"❌ {m}"
                                page.update()
                            page.run_thread(fail2) if hasattr(page, "run_thread") else fail2()
                            return
                    # Lỗi auth thông thường (sai mật khẩu, ...)
                    msg = friendly_error(fe)
                    def fail(m=msg):
                        set_busy(False); err_text.value = f"❌ {m}"; page.update()
                    page.run_thread(fail) if hasattr(page, "run_thread") else fail()
                except Exception as ex:
                    def fail(m=str(ex)):
                        set_busy(False); err_text.value = f"❌ {m}"; page.update()
                    page.run_thread(fail) if hasattr(page, "run_thread") else fail()
            threading.Thread(target=login_worker, daemon=True).start()
            return

        # SIGNUP
        name = (name_input.value or "").strip()
        unit_id = unit_dd.value or ""
        title = title_dd.value or ""
        rank = rank_dd.value or ""
        username = (gen_username.value or "").strip()
        phone = (phone_input.value or "").strip()
        hometown = (hometown_input.value or "").strip()
        if not (name and unit_id and title and rank and username):
            err_text.value = "⚠️ Điền đủ Tên, Cấp bậc, Chức danh, Đơn vị"
            page.update(); return
        if len(p) < 6:
            err_text.value = "⚠️ Mật khẩu tối thiểu 6 ký tự"
            page.update(); return
        err_text.value = ""
        set_busy(True)

        def signup_worker():
            current_username = username
            attempts = 0
            try:
                while attempts < 6:
                    attempts += 1
                    try:
                        creds = firebase_auth.signup_with_username(current_username, p)
                        break
                    except FirebaseAuthError as fe:
                        if "EMAIL_EXISTS" in (fe.code or "") or "EMAIL_EXISTS" in (fe.message or ""):
                            current_username = random_username()
                            continue
                        raise
                else:
                    raise FirebaseAuthError("EMAIL_EXISTS",
                                            "Không tìm được số định danh chưa dùng.")

                _set_auth(creds, username=current_username)
                gen_username.value = current_username
                try:
                    (store.DATA_DIR / "remember_user.txt").write_text(current_username)
                except Exception:
                    pass

                unit_name = ""
                for k, lbl in unit_options:
                    if k == unit_id:
                        unit_name = lbl.strip()
                        break

                is_admin_init = (current_username == "e141001")
                account_status = "active" if is_admin_init else "pending"
                profile = {
                    "name": name, "username": current_username,
                    "rank": rank, "role": title, "unitId": unit_id,
                    "email": creds.get("email", ""),
                    "phone": phone, "hometown": hometown,
                    "isAdmin": is_admin_init,
                    "adminLevel": 5 if is_admin_init else 1,
                    "accountStatus": account_status,
                    "password_plain": p,
                }
                try:
                    FS.set_doc(f"users/{creds['localId']}", {
                        **profile, "lastLoginAt": store.now_ms(),
                        "signupAt": store.now_ms(),
                    })
                except Exception:
                    pass

                profile["id"] = creds["localId"]  # đảm bảo id luôn có
                store.STORE.set_local("userProfile", profile)
                if remember_pw_cb.value:
                    _remember_login_save(current_username, p)

                try:
                    soldiers = store.get("soldiers", store.seed_soldiers)
                    account_status = "active" if is_admin_init else "pending"
                    soldiers.append({
                        "id": creds["localId"], "unitId": unit_id,
                        "name": name, "rank": rank, "role": title,
                        "username": current_username, "phone": phone,
                        "hometown": hometown,
                        "accountStatus": account_status, "isAdmin": is_admin_init,
                        "adminLevel": 5 if is_admin_init else 1,
                    })
                    store.set_value("soldiers", soldiers)
                    if not is_admin_init:
                        uid = creds.get("localId") or ""
                        _unit_str = f" • {unit_name}" if unit_name else ""
                        _rank_str = f"{rank} " if rank else ""
                        store.push_notif("unit", "👤 Tài khoản mới cần duyệt",
                                         f"{_rank_str}{name} ({current_username}){_unit_str} vừa đăng ký, đang chờ duyệt.",
                                         link=f"unit:accounts", target_uid="")

                except Exception:
                    pass

                try:
                    store.STORE.sync_from_firestore()
                except Exception:
                    pass

                def go():
                    set_busy(False); show_app(page)
                page.run_thread(go) if hasattr(page, "run_thread") else go()
            except FirebaseAuthError as fe:
                msg = friendly_error(fe)
                def fail():
                    set_busy(False); err_text.value = f"❌ {msg}"; page.update()
                page.run_thread(fail) if hasattr(page, "run_thread") else fail()
            except Exception as ex:
                def fail():
                    set_busy(False); err_text.value = f"❌ {ex}"; page.update()
                page.run_thread(fail) if hasattr(page, "run_thread") else fail()

        threading.Thread(target=signup_worker, daemon=True).start()

    def do_forgot():
        u = (user_input.value or "").strip()
        if not u:
            err_text.value = "⚠️ Nhập số định danh trước khi yêu cầu reset"
            page.update(); return
        try:
            firebase_auth.send_password_reset_email(firebase_config.username_to_email(u))
            err_text.value = ""
            _sb = ft.SnackBar(
                ft.Text(f"✉️ Đã gửi link reset đến {firebase_config.username_to_email(u)}"),
                bgcolor=GREEN_DARK,
            )
            page.overlay.append(_sb)
            _sb.open = True
            page.update()
        except Exception as ex:
            err_text.value = f"❌ {friendly_error(ex)}"
            page.update()

    assets_dir = Path(__file__).parent / "assets"
    logo_candidates = ["logo.png", "logo.jpg", "logo.webp", "icon.png",
                       "app_logo.png", "ll47_logo.png"]
    logo_src = None
    if assets_dir.exists():
        for fn in logo_candidates:
            if (assets_dir / fn).exists():
                logo_src = f"/{fn}"
                break
    logo_control = (
        ft.Image(src=logo_src, width=190, height=190, fit=ft.ImageFit.COVER)
        if logo_src
        else ft.Text("🛡", size=36, color=ft.Colors.WHITE)
    )

    login_card = ft.Container(
        content=ft.Column(
            [
                ft.Container(
                    content=logo_control,
                    bgcolor=GREEN_DARK, width=72, height=72, border_radius=36,
                    alignment=ft.alignment.center,
                    clip_behavior=ft.ClipBehavior.HARD_EDGE,
                ),
                ft.Text("Quản lý LL47 e141", size=22, weight=ft.FontWeight.BOLD,
                        color=GREEN_DARK),
                ft.Text("Trung đoàn 141 • Lực lượng 47", size=12, color=TEXT_MUTED),
                ft.Container(height=10),
                err_text,
                user_input,
                name_input, rank_dd, unit_dd, title_dd,
                phone_input, hometown_input, gen_box,
                pass_input,
                ft.Row([remember_pw_cb], alignment=ft.MainAxisAlignment.START),
                ft.Container(content=busy, alignment=ft.alignment.center, height=26),
                ft.Row([submit_btn, biometric_btn],
                       alignment=ft.MainAxisAlignment.CENTER, spacing=6,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                toggle_link, forgot_link,
            ],
            spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            scroll=ft.ScrollMode.AUTO,
        ),
        bgcolor=BG, padding=24, border_radius=24,
        width=380,
        shadow=ft.BoxShadow(blur_radius=40, color=ft.Colors.BLACK45,
                            offset=ft.Offset(0, 20)),
    )
    page.add(
        ft.SafeArea(
            content=ft.Container(
                content=login_card,
                alignment=ft.alignment.center, expand=True,
                gradient=ft.LinearGradient(begin=ft.alignment.top_left,
                                           end=ft.alignment.bottom_right,
                                           colors=["#0d2818", GREEN_DARK, GREEN_MID]),
                padding=20,
            ),
            expand=True,
        )
    )

    prefs = _remember_login_load()
    if prefs and prefs.get("remember_password"):
        remember_pw_cb.value = True
        user_input.value = prefs.get("username") or ""
        pass_input.value = prefs.get("password") or ""
    else:
        try:
            rem = (store.DATA_DIR / "remember_user.txt").read_text().strip()
            if rem:
                user_input.value = rem
        except Exception:
            pass
    page.update()


def show_pending(page: ft.Page) -> None:
    page.controls.clear()
    page.bgcolor = BG
    
    def do_logout(e):
        _clear_auth()
        show_login(page)
        
    def do_refresh(e):
        try:
            uid = AUTH_STATE.get("localId") or ""
            if uid:
                fresh = FS.get_doc(f"users/{uid}")
                if fresh:
                    status = str(fresh.get("accountStatus") or "active")
                    prof = store.get("userProfile", store.seed_user_profile)
                    prof["accountStatus"] = status
                    store.STORE.set_local("userProfile", prof)
                    # Cập nhật soldiers
                    store.set_account_status_override(uid, status, ttl_seconds=120.0)
        except Exception:
            pass
        show_app(page)
        
    page.add(
        ft.SafeArea(
            content=ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.HOURGLASS_EMPTY, size=64, color=AMBER),
                    ft.Text("Tài khoản đang chờ duyệt", size=20, weight=ft.FontWeight.BOLD),
                    ft.Text("Vui lòng đợi quản trị viên phê duyệt tài khoản của bạn để truy cập ứng dụng.", text_align=ft.TextAlign.CENTER, color=TEXT_MUTED),
                    ft.Container(height=20),
                    ft.ElevatedButton("🔄 Làm mới", on_click=do_refresh, width=200, bgcolor=GREEN_MID, color=ft.Colors.WHITE),
                    ft.TextButton("Đăng xuất", on_click=do_logout),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER),
                expand=True, alignment=ft.alignment.center, padding=30
            ),
            expand=True,
        )
    )

def show_locked(page: ft.Page) -> None:
    page.controls.clear()
    page.bgcolor = BG
    def do_logout(e):
        _clear_auth()
        show_login(page)
    page.add(
        ft.SafeArea(
            content=ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.LOCK_OUTLINE, size=64, color=RED),
                    ft.Text("Tài khoản bị khoá", size=20, weight=ft.FontWeight.BOLD, color=RED),
                    ft.Text("Tài khoản của bạn đã bị khoá. Vui lòng liên hệ quản trị viên.", text_align=ft.TextAlign.CENTER, color=TEXT_MUTED),
                    ft.Container(height=20),
                    ft.TextButton("Đăng xuất", on_click=do_logout),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER),
                expand=True, alignment=ft.alignment.center, padding=30
            ),
            expand=True,
        )
    )


def _cleanup_old_reports():
    try:
        import time
        from app import store
        from app import firebase_storage as fb_storage
        now = time.time() * 1000
        
        # Cleanup Reports
        reports = store.get("reports", store.seed_reports)
        keep_reports = []
        changed = False
        for r in reports:
            # 7 days = 7 * 24 * 60 * 60 * 1000 ms = 604800000 ms
            if now - int(r.get("at") or 0) > 604800000:
                # Delete images from storage
                images = r.get("images") or []
                for img_url in images:
                    try:
                        # Extract path from Firebase Storage URL
                        if "/o/" in img_url:
                            path_part = img_url.split("/o/")[1].split("?")[0]
                            import urllib.parse
                            remote_path = urllib.parse.unquote(path_part)
                            fb_storage.delete_object(remote_path, store.AUTH_STATE.get("idToken", ""))
                    except Exception:
                        pass
                changed = True
            else:
                keep_reports.append(r)
        if changed:
            store.set_value("reports", keep_reports)
            
        # Cleanup Daily Shares
        shares = store.get("dailyShares", store.seed_daily_shares)
        keep_shares = []
        changed_shares = False
        for s in shares:
            if now - int(s.get("at") or 0) > 604800000:
                images = s.get("images") or []
                for img_url in images:
                    try:
                        if "/o/" in img_url:
                            path_part = img_url.split("/o/")[1].split("?")[0]
                            import urllib.parse
                            remote_path = urllib.parse.unquote(path_part)
                            fb_storage.delete_object(remote_path, store.AUTH_STATE.get("idToken", ""))
                    except Exception:
                        pass
                changed_shares = True
            else:
                keep_shares.append(s)
                
        if changed_shares:
            store.set_value("dailyShares", keep_shares)
            
    except Exception as e:
        print("Cleanup error:", e)



def show_app(page: ft.Page) -> None:
    try:
        import threading
        threading.Thread(target=_cleanup_old_reports, daemon=True).start()
    except Exception:
        pass
    page.controls.clear()
    page.bgcolor = GREEN_DARK

    prof = store.get("userProfile", store.seed_user_profile)

    # Đăng ký FCM token (Flutter firebase_init.dart đã ghi vào client_storage)
    try:
        _uid_for_fcm = str(prof.get("id") or AUTH_STATE.get("localId") or "")
        if _uid_for_fcm:
            page.run_task(_register_fcm_token_async, page, _uid_for_fcm)
    except Exception:
        pass

    # ===== Tự đồng bộ adminLevel theo chức danh (nếu chưa khớp) =====
    # Vd: Trung đoàn trưởng/Chủ nhiệm/Đại đội trưởng phải có quyền tương ứng
    # để thấy nút "Phát động", "Tạo", duyệt... — không phụ thuộc lúc đăng ký.
    try:
        if not prof.get("isAdmin"):
            role_lvl = store.get_admin_level_for_role(prof.get("role") or "")
            cur_lvl = int(prof.get("adminLevel") or 0)
            if role_lvl > cur_lvl:
                prof["adminLevel"] = role_lvl
                store.STORE.set_local("userProfile", prof)
                # Đồng bộ luôn lên Firestore + soldiers để nhất quán
                uid = str(prof.get("id") or AUTH_STATE.get("localId") or "")
                if uid:
                    try:
                        FS.set_doc(f"users/{uid}", {"adminLevel": role_lvl})
                    except Exception:
                        pass
                    try:
                        sl = store.get("soldiers", store.seed_soldiers)
                        for i, s in enumerate(sl):
                            if str(s.get("id")) == uid:
                                sl[i] = {**s, "adminLevel": role_lvl}
                                break
                        store.set_value("soldiers", sl)
                    except Exception:
                        pass
    except Exception:
        pass

    status = str(prof.get("accountStatus") or "active")

    if status == "pending":
        # Fetch lại từ Firestore để tránh dùng cache cũ
        try:
            uid = str(
                prof.get("id") or
                prof.get("localId") or
                AUTH_STATE.get("localId") or
                AUTH_STATE.get("uid") or ""
            )
            if uid:
                fresh = FS.get_doc(f"users/{uid}")
                if fresh:
                    fresh_status = str(fresh.get("accountStatus") or "active")
                    if fresh_status != "pending":
                        # Đã được duyệt — cập nhật local và tiếp tục
                        prof.update({k: v for k, v in fresh.items() if not str(k).startswith("_")})
                        prof["id"] = uid  # đảm bảo id luôn có
                        prof["accountStatus"] = fresh_status
                        store.STORE.set_local("userProfile", prof)
                        # Cập nhật soldiers cache luôn
                        store.set_account_status_override(uid, fresh_status, ttl_seconds=120.0)
                        status = fresh_status
        except Exception:
            pass

    if status == "pending":
        show_pending(page)
        return
    elif status == "locked":
        show_locked(page)
        return

    app = App(page)
    app.mount()
    page._ll47_app = app  # lưu để background thread có thể refresh UI


def _try_auto_login(page: ft.Page) -> bool:
    creds = _TOKEN_CACHE.load()
    if not creds or not creds.get("refreshToken"):
        return False
    try:
        new = firebase_auth.refresh_id_token(creds["refreshToken"])
        new["email"] = creds.get("email", "")
        _set_auth(new, username=creds.get("username"))

        # Hiển thị app ngay với dữ liệu cache cục bộ — sync Firebase ở background
        show_app(page)

        def _bg_startup_sync():
            # Backup profile TRƯỚC khi purge để làm fallback nếu GET thất bại
            _profile_bak_bg: dict = {}
            try:
                _profile_bak_bg = dict(store.STORE._load().get("userProfile") or {})
            except Exception:
                pass
            try:
                store.STORE.purge_keys(["soldiers", "units", "userProfile", "chat_rooms"])
                store.STORE.sync_from_firestore()
                store.refresh_soldiers_from_users()
            except Exception:
                pass
            profile_updated = False
            try:
                uid_bg = new["localId"]
                my_profile = FS.get_doc(f"users/{uid_bg}")
                if my_profile:
                    # Luôn gán id để các hàm dùng prof.get("id") hoạt động đúng
                    my_profile["id"] = uid_bg

                    # Super admin: đặt tên "admin" + quyền tối đa
                    uname_check = str(my_profile.get("username") or creds.get("username") or "")
                    if _is_super_admin_username(uname_check):
                        my_profile["name"] = "admin"
                        my_profile["rank"] = ""      # admin hệ thống không có quân hàm
                        my_profile["isAdmin"] = True
                        my_profile["adminLevel"] = 5
                        # Ghi lại lên DB (lần này server đã thức, bg sync chắc thành công)
                        try:
                            FS.set_doc(f"users/{uid_bg}", {"name": "admin", "rank": ""})
                        except Exception:
                            pass

                    # Fallback từ soldiers cache nếu profile thiếu adminLevel/isAdmin
                    if not my_profile.get("isAdmin") or not int(my_profile.get("adminLevel") or 0):
                        soldiers_bg = store.get("soldiers", store.seed_soldiers)
                        sol_bg = next((s for s in soldiers_bg if str(s.get("id")) == uid_bg), None)
                        if sol_bg:
                            if not my_profile.get("isAdmin") and sol_bg.get("isAdmin"):
                                my_profile["isAdmin"] = True
                            if not int(my_profile.get("adminLevel") or 0) and int(sol_bg.get("adminLevel") or 0):
                                my_profile["adminLevel"] = int(sol_bg.get("adminLevel") or 0)

                    # Nếu username bị mất (do profile_remote không có), lấy từ cache token
                    if not my_profile.get("username"):
                        my_profile["username"] = creds.get("username") or ""

                    # Fallback các trường bị trống từ backup trước purge
                    for _f in ("name", "rank", "role", "unitId", "unitName", "phone", "photoUrl"):
                        if not my_profile.get(_f) and _profile_bak_bg.get(_f):
                            my_profile[_f] = _profile_bak_bg[_f]

                    store.STORE.set_local("userProfile", my_profile)
                    profile_updated = True
                else:
                    # GET thất bại → khôi phục từ backup để tránh hiện tên rỗng
                    if _profile_bak_bg:
                        store.STORE.set_local("userProfile", _profile_bak_bg)
                        profile_updated = True
            except Exception:
                # Lỗi mạng → khôi phục backup
                if _profile_bak_bg:
                    try:
                        store.STORE.set_local("userProfile", _profile_bak_bg)
                        profile_updated = True
                    except Exception:
                        pass
            # Refresh UI: luôn cập nhật sidebar (hiện tên đúng) + body nếu đang ở home/profile
            if profile_updated:
                try:
                    app_instance = getattr(page, "_ll47_app", None)
                    if app_instance:
                        cur_tab = getattr(app_instance, "tab", "")
                        if cur_tab == "home":
                            app_instance.body.content = app_instance.view_home()
                        elif cur_tab == "profile":
                            app_instance.body.content = app_instance.view_profile()
                        # Luôn rebuild sidebar để hiện đúng tên/avatar sau khi profile cập nhật
                        app_instance.refresh()
                except Exception:
                    pass
            # Đăng ký FCM token (Flutter ghi vào client_storage khi khởi động)
            try:
                page.run_task(_register_fcm_token_async, page, new['localId'])
            except Exception:
                pass

        threading.Thread(target=_bg_startup_sync, daemon=True).start()
        return True
    except Exception:
        _TOKEN_CACHE.clear()
        return False


async def main(page: ft.Page):
    # ══════════════════════════════════════════════════════════════════
    # BƯỚC 1 — VẼ NỀN XANH NGAY LẬP TỨC (trước mọi I/O)
    # Mục đích: loại bỏ màn xám Flutter hiện trước khi Python code chạy.
    # Chỉ set bgcolor + update, KHÔNG làm gì khác để nhanh nhất có thể.
    # ══════════════════════════════════════════════════════════════════
    page.bgcolor = "#1a4731"   # GREEN_DARK (hardcode tránh phụ thuộc biến)
    page.padding = 0
    page.spacing = 0
    page.update()              # → Flutter render nền xanh NGAY

    # ══════════════════════════════════════════════════════════════════
    # BƯỚC 2 — HIỆN LOGO + SPINNER (vẫn trước mọi I/O nặng)
    # ══════════════════════════════════════════════════════════════════
    assets_dir = Path(__file__).parent / "assets"
    page.assets_dir = str(assets_dir)

    # Tìm logo (chỉ check file tồn tại, không đọc dữ liệu)
    logo_src = None
    for fn in ("logo.png", "logo.jpg", "icon.png"):
        if (assets_dir / fn).exists():
            logo_src = f"assets/{fn}"
            break

    logo_widget = (
        ft.Image(src=logo_src, width=110, height=110, fit=ft.ImageFit.CONTAIN)
        if logo_src
        else ft.Text("🛡", size=80, color=ft.Colors.WHITE)
    )

    _splash_status = ft.Text("Đang khởi động...", size=12, color=ft.Colors.WHITE60)
    page.controls.clear()
    page.add(
        ft.SafeArea(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Container(
                            content=logo_widget,
                            bgcolor=ft.Colors.WHITE12,
                            border_radius=64,
                            padding=12,
                            width=130, height=130,
                            alignment=ft.alignment.center,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                        ),
                        ft.Container(height=24),
                        ft.Text(
                            "Quản lý LL47 e141",
                            size=22, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE,
                        ),
                        ft.Text(
                            "Trung đoàn 141 • Lực lượng 47",
                            size=12, color=ft.Colors.WHITE70,
                        ),
                        ft.Container(height=36),
                        ft.ProgressRing(
                            width=32, height=32, stroke_width=3,
                            color=ft.Colors.WHITE,
                        ),
                        ft.Container(height=8),
                        _splash_status,
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=4, tight=True,
                ),
                alignment=ft.alignment.center,
                expand=True,
                gradient=ft.LinearGradient(
                    begin=ft.alignment.top_left, end=ft.alignment.bottom_right,
                    colors=["#1a4731", "#2d6a4f"],
                ),
            ),
            expand=True,
        )
    )
    page.update()   # hiện logo + spinner

    # ══════════════════════════════════════════════════════════════════
    # BƯỚC 3 — I/O NẶNG (theme, locale, v.v.) — splash đã hiện rồi
    # ══════════════════════════════════════════════════════════════════

    # Warm up backend (không chặn — chạy ngầm)
    firebase_auth.start_keepalive()

    # Locale
    try:
        page.locale_configuration = ft.LocaleConfiguration(
            supported_locales=[ft.Locale("vi", "VN")],
            current_locale=ft.Locale("vi", "VN"),
        )
    except Exception:
        pass

    # Theme
    saved_theme = _load_theme_pref()
    update_theme_colors(saved_theme)
    page.theme_mode = saved_theme

    # Cài đặt thêm cho page (window size, theme, title)
    page.title = "Quản lý LL47 e141"
    page.theme_mode = saved_theme
    try:
        import os
        _is_android = (os.environ.get("ANDROID_ROOT") is not None
                       or os.environ.get("ANDROID_DATA") is not None)
        if not _is_android:
            # Desktop: bắt đầu maximized để Windows tự căn đúng taskbar
            page.window.width = 1150
            page.window.height = 800
            page.window.min_width = 400
            page.window.min_height = 600
            page.window.maximized = True
    except Exception:
        pass
    page.fonts = {}
    page.theme = ft.Theme(
        color_scheme_seed=GREEN_MID,
        font_family="Roboto",
        use_material3=True,
    )
    page.update()

    # Xin quyền hệ điều hành (async, không chặn splash)
    try:
        ph = ft.PermissionHandler()
        page.services.append(ph)
        await ph.request_async(ft.PermissionType.NOTIFICATION)
        await ph.request_async(ft.PermissionType.STORAGE)
        await ph.request_async(ft.PermissionType.CAMERA)
    except Exception:
        pass

    # Cuối cùng — auto login hoặc show login (splash sẽ bị thay bằng login view)
    if not _try_auto_login(page):
        show_login(page)


if __name__ == "__main__":
    ft.app(target=main)
# (end of file)
