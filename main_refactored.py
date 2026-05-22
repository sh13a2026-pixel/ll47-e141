"""
LL47 e141 — Ứng dụng Quản lý Lực lượng 47 Trung đoàn 141.
Viết bằng Flet (Flutter for Python). Build được Android APK + iOS IPA + Web + Desktop.

Chạy thử:    flet run
Build APK:   flet build apk
Build iOS:   flet build ipa  (cần macOS + Xcode)
Build Web:   flet build web
"""
from __future__ import annotations

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
    """Tài khoản quản trị hệ thống — số quân e141001 (mọi cách gõ)."""
    return _username_key(u) == "e141001"


def _looks_like_firebase_uid(uid: str | None) -> bool:
    if not uid or not isinstance(uid, str):
        return False
    u = uid.strip()
    return len(u) >= 18 and u.replace("_", "").isalnum()


def _hydrate_profile_after_login(creds: dict, login_username: str) -> None:
    """Ghép profile từ Firestore users/{uid}, áp quyền admin e141001, cập nhật soldiers."""
    uid = creds.get("localId") or ""
    uname_raw = (login_username or "").strip() or (AUTH_STATE.get("username") or "")

    profile_remote: dict = {}
    try:
        doc = FS.get_doc(f"users/{uid}")
        if doc is None and not _is_super_admin_username(uname_raw):
            profile_remote = {"accountStatus": "locked"}
        elif doc:
            profile_remote = {k: v for k, v in doc.items() if not str(k).startswith("_")}
    except Exception:
        profile_remote = {}

    base = store.seed_user_profile()
    merged = {**base, **profile_remote}
    merged["username"] = uname_raw if uname_raw else merged.get("username", "")
    merged["email"] = creds.get("email") or merged.get("email", "")

    if _is_super_admin_username(uname_raw):
        merged["name"] = "admin"
        merged["isAdmin"] = True
        merged["adminLevel"] = 5
        merged.setdefault("role", merged.get("role") or "Quản trị hệ thống")
        merged.setdefault("unitName", merged.get("unitName") or "Trung đoàn 141")

    store.set_value("userProfile", merged)

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
    row = {
        "id": uid,
        "unitId": merged.get("unitId") or "",
        "name": merged.get("name") or "",
        "rank": merged.get("rank") or "",
        "role": merged.get("role") or "",
        "username": merged.get("username") or uname_raw,
        "phone": merged.get("phone") or "",
        "accountStatus": str(merged.get("accountStatus") or "active"),
        "isAdmin": bool(merged.get("isAdmin")),
        "adminLevel": int(merged.get("adminLevel") or 1),
        "photoUrl": str(merged.get("photoUrl") or ""),
    }
    if idx is None:
        soldiers.append(row)
    else:
        soldiers[idx] = {**soldiers[idx], **row}
    store.set_value("soldiers", soldiers)

    try:
        FS.set_doc(
            f"users/{uid}",
            {
                "username": merged.get("username", ""),
                "email": merged.get("email", ""),
                "name": merged.get("name", ""),
                "rank": merged.get("rank", ""),
                "role": merged.get("role", ""),
                "unitId": merged.get("unitId", ""),
                "unitName": merged.get("unitName", ""),
                "phone": merged.get("phone", ""),
                "isAdmin": bool(merged.get("isAdmin")),
                "adminLevel": int(merged.get("adminLevel") or 1),
                "lastLoginAt": store.now_ms(),
                "photoUrl": str(merged.get("photoUrl") or ""),
            },
        )
    except Exception:
        pass


# ============================================================
# ===== HẰNG SỐ MÀU SẮC + STYLE                          =====
# ============================================================

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

# Phiên bản hiển thị (đăng nhập + cài đặt)
APP_VERSION = "1.0.1"
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


# ============================================================
# ===== APP CONTROLLER                                  =====
# ============================================================

class App:
    """Controller chính. Quản lý nav, render từng màn, lưu trạng thái."""

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
        self.body = ft.Container(expand=True, bgcolor=BG2)
        # Realtime sync: kéo dữ liệu từ Firestore mỗi 10s rồi refresh tab hiện tại
        self._realtime_stop = {"stop": False}
        self._start_realtime_sync()

    def _start_realtime_sync(self) -> None:
        """Background thread sync app_data từ Firestore mỗi 10s.

        CHỈ kéo dữ liệu vào cache, KHÔNG rebuild UI tự động (vì sẽ làm văng
        người dùng khỏi sub-module / dialog đang mở). Khi user chuyển tab
        hoặc mở module sẽ thấy data mới ngay.
        """
        if not store.STORE.is_bound():
            return

        def loop():
            while not self._realtime_stop["stop"]:
                t = 0.0
                while t < 30.0 and not self._realtime_stop["stop"]:
                    time.sleep(0.5)
                    t += 0.5
                if self._realtime_stop["stop"]:
                    return
                try:
                    store.STORE.sync_from_firestore()
                    store.STORE.flush_pending()
                    # Refresh soldiers từ users/ — đảm bảo các tiện ích thấy
                    # đúng danh sách quân nhân (loại bỏ acc đã xoá).
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

    # ---- TOAST ----
    def toast(self, msg: str) -> None:
        self.page.snack_bar = ft.SnackBar(
            content=ft.Text(msg, color=ft.Colors.WHITE, size=13, weight=ft.FontWeight.W_600),
            bgcolor=GREEN_DARK,
            duration=2200,
        )
        self.page.snack_bar.open = True
        self.page.update()

    # ---- HEADER ----
    def header(self, title: str, sub: str | None = None, show_back: bool = False) -> ft.Container:
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
            [ft.Text(title, color=ft.Colors.WHITE, size=17, weight=ft.FontWeight.BOLD)]
            + ([ft.Text(sub, color=ft.Colors.WHITE54, size=11)] if sub else []),
            spacing=2, tight=True,
        )
        children: list[ft.Control] = []
        if show_back:
            children.append(ft.IconButton(ft.Icons.ARROW_BACK, icon_color=ft.Colors.WHITE,
                                          on_click=lambda e: self.go_back()))
        children.append(ft.Container(content=title_col, expand=True, padding=ft.padding.only(left=4)))
        children.append(ft.IconButton(content=bell_stack, on_click=lambda e: self.open_notifs()))
        children.append(self._header_overflow_menu())
        return ft.Container(
            content=ft.Row(children, alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=GREEN_DARK,
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
                page.dialog.open = False
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

        page.dialog = ft.AlertDialog(
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
        page.dialog.open = True
        page.update()

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
            bgcolor=BG, border=ft.border.only(top=ft.BorderSide(1, BORDER)),
            height=64,
        )

    # ---- TAB SWITCHER ----
    def set_tab(self, tab: str) -> None:
        # Dọn listener chat khi rời màn detail
        prev_stop = getattr(self, "_chat_listener_stop", None)
        if callable(prev_stop):
            try:
                prev_stop()
            except Exception:
                pass
            self._chat_listener_stop = None
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

    def refresh(self) -> None:
        # Re-render frame
        self.frame.controls = [self.header_for_tab(), self.body, self.bottom_nav()]
        self.page.update()

    def header_for_tab(self) -> ft.Control:
        oid = getattr(self, "overlay_soldier_id", None)
        if oid:
            soldiers = store.get("soldiers", store.seed_soldiers)
            s = next((x for x in soldiers if str(x.get("id")) == str(oid)), None)
            title = (s or {}).get("name") or "Hồ sơ"
            sub = None
            if s:
                sub = f"{s.get('rank', '')} • {s.get('role', '')}".strip(" •")
            return self.header(title, sub or None, show_back=True)

        # Đang ở sub-module (F47, CTĐ-CTCT, ...) → header hiện tên module + back arrow
        cur_mod = getattr(self, "current_module", None)
        if cur_mod:
            module_titles = {
                "f47": "🛡 Lực lượng 47",
                "ctdctct": "🎖 CTĐ-CTCT",
                "schedule": "📅 Lịch gác - Trực Ban",
                "guests": "🤝 Thăm - Tiếp khách",
                "units": "👥 Quản lý quân nhân",
                "exams": "📝 Thi - Kiểm tra nhận thức",
                "hygiene": "🧹 Nội vụ vệ sinh",
                "hcqs": "⚔️ Hành chính - Quân sự",
                "pttd": "🚩 Phong trào thi đua",
            }
            mod_title = module_titles.get(cur_mod, cur_mod.title())
            return self.header(mod_title, None, show_back=True)

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
        if _is_super_admin_username(uname) or bool(profile.get("isAdmin")):
            greet_main = "đ.c Admin 🎖"
        else:
            rank_str = (profile.get('rank') or '').strip()
            rank_prefix = f"{rank_str} " if rank_str else ""
            greet_main = f"đ.c {rank_prefix}{profile.get('name') or 'Không tên'} 🫡"

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

        my_uid = str(profile.get("id") or "")

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
            if total_pending == 0:
                self.toast("🎉 Bạn không có yêu cầu nào chờ phê duyệt!")
                return

            options_ctrls = []

            def make_opt(icon, text, count, on_click_action):
                def click_handler(e):
                    self.page.dialog.open = False
                    self.page.update()
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

            self.page.dialog = ft.AlertDialog(
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
                    ft.TextButton("Đóng", on_click=lambda e: setattr(self.page.dialog, "open", False) or self.page.update())
                ]
            )
            self.page.dialog.open = True
            self.page.update()

        # Calculate active presence/absence
        total_soldiers_cnt = sum(1 for _s in soldiers if not _s.get('isAdmin'))
        present_soldiers_cnt = sum(1 for _s in soldiers if not _s.get('isAdmin') and (_s.get('presence_status') or 'Trực') == 'Trực')
        
        absent_ra_ngoai = sum(1 for _s in soldiers if not _s.get('isAdmin') and _s.get('presence_status') == 'Ra ngoài')
        absent_di_phep = sum(1 for _s in soldiers if not _s.get('isAdmin') and _s.get('presence_status') == 'Đi phép')
        absent_tranh_thu = sum(1 for _s in soldiers if not _s.get('isAdmin') and _s.get('presence_status') == 'Tranh thủ')
        
        absent_parts = []
        if absent_ra_ngoai: absent_parts.append(f"ngoài: {absent_ra_ngoai}")
        if absent_di_phep: absent_parts.append(f"phép: {absent_di_phep}")
        if absent_tranh_thu: absent_parts.append(f"tranh thủ: {absent_tranh_thu}")
        
        if absent_parts:
            presence_sub = f"Vắng {', '.join(absent_parts)}"
        else:
            presence_sub = "✓ Đủ quân số"

        stats = ft.Container(
            content=ft.Column(
                [
                    ft.Row([
                        stat_card("Quân số hôm nay",
                                  f"{present_soldiers_cnt}/{total_soldiers_cnt}",
                                  presence_sub,
                                  GREEN_DARK, "👥", lambda: self.set_tab("contacts")),
                        stat_card("Ca trực hôm nay", "Tr.đội 2", "18:00 – 06:00",
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
            color = {"urgent": RED, "f47": BLUE, "unit": AMBER, "success": GREEN_MID}.get(n["type"], "#999")
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
                                ft.Text(time_ago(n["at"]), size=10, color=TEXT_MUTED),
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

        return ft.ListView(controls=[hero, stats, notif_block], expand=True, padding=0)

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
        elif kind in ("guest", "guests"):
            self.set_tab("util")
            self.open_module("guests")
            if target_id:
                all_g = list(store.get("guests", []) or [])
                g = next((x for x in all_g if str(x.get("id")) == str(target_id)), None)
                if g:
                    my_profile = store.get("userProfile", store.seed_user_profile)
                    my_uid = str(my_profile.get("id") or AUTH_STATE.get("localId") or "")
                    cur_appr = str(g.get("currentApproverId") or g.get("approverId") or "")
                    if cur_appr == my_uid and int(my_profile.get("adminLevel") or 1) >= 2:
                        self.guests_view = "manage"
                    else:
                        self.guests_view = "my_guests"
                    self.refresh()
                    self.open_guest_details_dialog(g)
                    return
        elif kind == "profile":
            self.open_member_profile({"id": target_id})
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
            return ft.Container(
                content=ft.Row(
                    [
                        avatar_stack,
                        ft.Column(
                            [
                                ft.Row(
                                    [ft.Text(name, size=14, weight=ft.FontWeight.W_700, expand=True),
                                     ft.Text(when, size=11, color=TEXT_MUTED)],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                ),
                                ft.Row(
                                    [ft.Text(sub, size=12, color=TEXT_MUTED, expand=True,
                                             overflow=ft.TextOverflow.ELLIPSIS), badge],
                                ),
                            ],
                            expand=True, spacing=2, tight=True,
                            # Vùng cột giữa = vùng click mở chat
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
                bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
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
                        ),
                        expand=True,
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

        list_view = ft.ListView(
            controls=header_controls + [chat_item(r) for r in rooms],
            expand=True,
            padding=0,
        )

        # FAB nhỏ góc phải dưới (chỉ trong tab tin nhắn)
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
                page.dialog.open = False
            except Exception:
                pass

            self.open_chat_room(
                (rid, group_name, f"{len(selected_ids)} thành viên", "", 0, True, "group", False),
            )

        page.dialog = ft.AlertDialog(
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
                    on_click=lambda e: (setattr(page.dialog, "open", False), page.update()),
                ),
                ft.ElevatedButton(
                    "Tạo nhóm",
                    on_click=submit,
                    bgcolor=GREEN_MID,
                    color=ft.Colors.WHITE,
                ),
            ],
        )
        page.dialog.open = True
        page.update()

    def view_chat_detail(self, rid: str, name: str, sub: str) -> ft.Control:
        # Huỷ listener cũ nếu có
        prev_stop = getattr(self, "_chat_listener_stop", None)
        if callable(prev_stop):
            try:
                prev_stop()
            except Exception:
                pass
            self._chat_listener_stop = None

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
                self.page.dialog.open = False
                self.page.update()
                self.toast("🧹 Đã xóa lịch sử trò chuyện cục bộ!")
                render_messages([])

            self.page.dialog = ft.AlertDialog(
                title=ft.Text("Xóa lịch sử?", size=15, weight=ft.FontWeight.BOLD),
                content=ft.Text("Bạn có chắc chắn muốn xóa toàn bộ lịch sử tin nhắn của cuộc trò chuyện này không?"),
                actions=[
                    ft.TextButton("Huỷ", on_click=lambda e: setattr(self.page.dialog, "open", False) or self.page.update()),
                    ft.ElevatedButton("Xóa", on_click=confirm_clear, bgcolor=RED, color=ft.Colors.WHITE)
                ]
            )
            self.page.dialog.open = True
            self.page.update()

        def leave_group(_):
            rooms = store.get("chat_rooms", store.seed_chat_rooms)
            rooms = [r for r in rooms if r.get("id") != rid]
            store.set_value("chat_rooms", rooms)
            self.toast("Đã rời/giải tán nhóm chat!")
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
                        on_click=lambda e, url=img_url: self.page.launch_url(url),
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
                                    on_tap=lambda e: self.page.launch_url(url)
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
            spacing=10, padding=10, expand=True, auto_scroll=True,
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
                        if not f.path:
                            continue
                        folder = f"chat/{rid}/{my_uid}"
                        remote = fb_storage.make_remote_path(folder, f.name)
                        res = fb_storage.upload_file(f.path, remote, AUTH_STATE["idToken"])
                        url = res["downloadURL"]
                        
                        msg_text = f"📎 [{f.name}] {url}"
                        store.send_chat_message(rid, my_uid, my_name, msg_text)
                    except Exception as ex_e:
                        self.toast(f"❌ Lỗi gửi file: {ex_e}")
            
            threading.Thread(target=worker, daemon=True).start()

        chat_picker = ft.FilePicker(on_result=on_chat_file_picked)
        if chat_picker not in self.page.overlay:
            self.page.overlay.append(chat_picker)
            try:
                self.page.update()
            except Exception:
                pass

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
                    fresh = store.fetch_chat_messages(rid, limit=200)
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
                    fresh = store.fetch_chat_messages(rid, limit=200)
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
                        fr = store.fetch_chat_messages(rid, limit=200)
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
                        fr = store.fetch_chat_messages(rid, limit=200)
                        render_messages(fr)
                    except Exception:
                        pass
                    return
                self.toast("Đã xóa tin nhắn")
                try:
                    fr = store.fetch_chat_messages(rid, limit=200)
                    render_messages(fr)
                except Exception:
                    pass

            return _h

        def render_messages(items: list[dict]):
            # Giữ phần header "Hôm nay ..."
            header = msgs.controls[0] if msgs.controls else None
            new_controls = [header] if header else []
            members, last_read = room_meta()
            pins_l = room_pinned_ids()
            ordered = store.sort_chat_messages_with_pins(items, pins_l)
            for m in ordered:
                txt = m.get("text", "") or ""
                mid = str(m.get("id") or m.get("_id") or "").strip()
                pinned = bool(mid and mid in pins_l)
                if str(m.get("senderId") or "") == str(my_uid):
                    inner = bubble_me_inner(
                        txt, my_msg_footer(m, members, last_read), pinned,
                    )
                    row_me = ft.Row(
                        [
                            ft.Container(expand=True),
                            ft.Row(
                                [msg_menu(m), inner],
                                spacing=2,
                                vertical_alignment=ft.CrossAxisAlignment.END,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.END,
                    )
                    if mid:
                        new_controls.append(
                            ft.Dismissible(
                                content=ft.Container(content=row_me, expand=True),
                                background=dismiss_bg_delete(),
                                secondary_background=dismiss_bg_pin(),
                                dismiss_direction=ft.DismissDirection.HORIZONTAL,
                                data=mid,
                                on_confirm_dismiss=on_confirm_swipe(mid, True),
                                on_dismiss=on_dismiss_swipe(mid, True),
                            ),
                        )
                    else:
                        new_controls.append(row_me)
                else:
                    inner = bubble_them_inner(
                        txt, m.get("senderName", ""), pinned,
                    )
                    row_them = ft.Row(
                        [inner, msg_menu(m)],
                        spacing=2,
                        vertical_alignment=ft.CrossAxisAlignment.END,
                    )
                    if mid:
                        new_controls.append(
                            ft.Dismissible(
                                content=ft.Container(content=row_them, expand=True),
                                secondary_background=dismiss_bg_pin(),
                                dismiss_direction=ft.DismissDirection.START_TO_END,
                                data=mid,
                                on_confirm_dismiss=on_confirm_swipe(mid, False),
                            ),
                        )
                    else:
                        new_controls.append(row_them)
            msgs.controls = new_controls
            try:
                update_sidebar_attachments(items)
            except Exception:
                pass
            try:
                self.page.update()
            except Exception:
                pass

        # Tải tin nhắn ban đầu — ASYNC ngầm (không block UI khi mở chat)
        if store.STORE.is_bound():
            def _initial_load():
                try:
                    store.refresh_chat_room_meta(rid)
                    initial = store.fetch_chat_messages(rid, limit=200)
                    def _draw():
                        try:
                            render_messages(initial)
                        except Exception:
                            pass
                    if hasattr(self.page, "run_thread"):
                        self.page.run_thread(_draw)
                    else:
                        _draw()
                except Exception:
                    pass

            threading.Thread(target=_initial_load, daemon=True).start()

            # Đăng ký lắng nghe realtime (polling 2s)
            try:
                def on_chat_messages(items: list[dict]):
                    try:
                        store.refresh_chat_room_meta(rid)
                    except Exception:
                        pass
                    render_messages(items)
                    if self.tab == "chat" and len(self.frame.controls) > 2:
                        try:
                            self.frame.controls[2] = self.bottom_nav()
                            self.page.update()
                        except Exception:
                            pass

                self._chat_listener_stop = store.listen_chat_messages(
                    rid, on_chat_messages, interval=2.0,
                )
            except Exception:
                self._chat_listener_stop = None

        def send_msg(e):
            txt = (msg_input.value or "").strip()
            if not txt:
                return
            msg_input.value = ""
            # Hiển thị optimistic ngay
            msgs.controls.append(
                ft.Row(
                    [
                        ft.Container(expand=True),
                        bubble_me_inner(txt, "Đang gửi…", False),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.END,
                ),
            )
            self.page.update()
            # Gửi lên Firestore
            if store.STORE.is_bound():
                def worker():
                    try:
                        store.send_chat_message(rid, my_uid, my_name, txt)
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
                            ft.IconButton(ft.Icons.ARROW_BACK,
                                          on_click=lambda e: self.set_tab("chat")),
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
        modules = [
            ("CHỨC NĂNG CHÍNH", [
                ("f47", "🛡", "Lực lượng 47", "2 đang chạy", RED),
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
        return ft.ListView(controls=sections, expand=True, padding=0)

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
        if key == "notifs":
            return self.view_all_notifs()
        if key == "guests":
            return self.module_guests()
        if key == "exams":
            return self.module_exams()
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

    def module_back_bar(self, title: str) -> ft.Container:
        return ft.Container(
            content=ft.Row(
                [ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: self.set_tab("util")),
                 ft.Text(title, size=16, weight=ft.FontWeight.BOLD, expand=True)],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=BG, padding=ft.padding.symmetric(horizontal=4, vertical=4),
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
        )

    # ---------- Module: Lịch trực ----------
    def module_schedule(self) -> ft.Control:
        shifts = sorted(store.get("shifts", store.seed_shifts), key=lambda s: s["date"])
        now = store.now_ms()

        def shift_row(s) -> ft.Container:
            is_night = s["type"] == "night"
            time_lbl = "18:00 – 06:00" if is_night else "06:00 – 18:00"
            icon = "🌙" if is_night else "☀️"
            status_lbl = {"done": "✓ Xong", "active": "Đang trực",
                          "pending": "Sắp trực"}.get(s["status"], s["status"])
            status_bg = {"done": GREEN_LIGHT, "active": "#fcebeb",
                         "pending": "#faeeda"}.get(s["status"], "#eee")
            status_color = {"done": GREEN_DARK, "active": "#791f1f",
                            "pending": "#633806"}.get(s["status"], TEXT)
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            content=ft.Text(icon, size=18),
                            width=40, height=40, border_radius=10,
                            bgcolor="#d0e8fb" if is_night else "#faeeda",
                            alignment=ft.alignment.center,
                        ),
                        ft.Column(
                            [ft.Text(s["platoon"], size=13, weight=ft.FontWeight.W_600),
                             ft.Text(f"{time_lbl} • {s['location']} • {fmt_date(s['date'])}",
                                     size=11, color=TEXT_MUTED)],
                            expand=True, spacing=2, tight=True,
                        ),
                        ft.Container(
                            content=ft.Text(status_lbl, size=10, color=status_color,
                                            weight=ft.FontWeight.BOLD),
                            bgcolor=status_bg, border_radius=10,
                            padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        ),
                    ],
                    spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(vertical=10, horizontal=2),
                border=ft.border.only(top=ft.BorderSide(1, BORDER)),
            )

        def section(title: str, items: list) -> ft.Container:
            children = [ft.Text(title, size=13, weight=ft.FontWeight.BOLD)]
            children += [shift_row(s) for s in items] if items \
                else [ft.Container(content=ft.Text("Không có dữ liệu", size=12, color=TEXT_MUTED),
                                   padding=14, alignment=ft.alignment.center,
                                   border=ft.border.only(top=ft.BorderSide(1, BORDER)))]
            return ft.Container(
                content=ft.Column(children, spacing=0),
                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
                padding=12, margin=ft.margin.only(bottom=10),
            )

        today = [s for s in shifts if datetime.fromtimestamp(s["date"] / 1000).date() ==
                 datetime.fromtimestamp(now / 1000).date()]
        upcoming = [s for s in shifts if s["date"] > now and s not in today][:7]
        past = [s for s in shifts if s["date"] < now and s not in today][-5:]

        def add_shift(e):
            from datetime import timedelta
            shifts_lst = store.get("shifts", store.seed_shifts)
            new = {
                "id": f"sh{store.now_ms()}",
                "date": store.now_ms() + 86400_000,
                "type": "day", "platoon": "Trung đội mới",
                "location": "Cổng chính", "status": "pending",
            }
            shifts_lst.append(new)
            store.set_value("shifts", shifts_lst)
            store.log_activity("Thêm ca trực qua app Python")
            store.push_notif("unit", "📅 Ca trực mới",
                             f"{new['platoon']} – {fmt_date(new['date'])}", "schedule")
            self.toast("✅ Đã thêm ca trực")
            self.body.content = self.module_schedule()
            self.refresh()

        return ft.Column(
            [
                self.module_back_bar("📅 Lịch trực đơn vị"),
                ft.ListView(
                    controls=[
                        ft.Container(
                            content=ft.Column([
                                section("⭐ Hôm nay", today),
                                section("📆 Sắp tới", upcoming),
                                section("📋 Đã hoàn thành", past),
                                ft.ElevatedButton(
                                    "＋ Thêm ca trực mới",
                                    on_click=add_shift,
                                    style=ft.ButtonStyle(
                                        bgcolor=GREEN_MID, color=ft.Colors.WHITE,
                                        shape=ft.RoundedRectangleBorder(radius=10),
                                        padding=14,
                                    ),
                                    width=10000,
                                ),
                            ], spacing=0),
                            padding=10,
                        )
                    ],
                    expand=True, padding=0,
                ),
            ],
            spacing=0, expand=True,
        )

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

        return ft.Column(
            [
                self.module_back_bar("📋 Báo cáo"),
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

        role_tf = ft.Dropdown(
            label="Chức vụ / vai trò",
            value=str(row.get("role") or ""),
            options=[ft.dropdown.Option(t, t) for t in store.TITLES],
            border_radius=8, dense=True,
            on_change=_on_role_changed,
        )
        admin_sw = ft.Switch(label="Quản trị viên (isAdmin)", value=bool(row.get("isAdmin")))
        # Dropdown đơn vị
        unit_opts_full = store.flatten_units_for_select(store.get("units", store.seed_units))
        cur_unit = str(row.get("unitId") or "")
        unit_dd = ft.Dropdown(
            label="Đơn vị",
            value=cur_unit if cur_unit else None,
            options=[ft.dropdown.Option(k, lbl) for k, lbl in unit_opts_full],
            border_radius=8, dense=True,
        )
        err_text = ft.Text("", color=RED, size=12)

        def close_dlg(_):
            page.dialog.open = False
            page.update()

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

        page.dialog = ft.AlertDialog(
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
        page.dialog.open = True
        page.update()

    # ---------- Module: Quản lý quân nhân + cây đơn vị ----------
    def module_units(self) -> ft.Control:
        tree = store.get("units", store.seed_units)
        soldiers = store.get("soldiers", store.seed_soldiers)

        def unit_node(node: dict, depth: int = 0) -> list[ft.Control]:
            children_ctrls = []
            cmd = next((s for s in soldiers if s["id"] == node.get("commanderId")), None)
            cmd_line = (
                f"★ {cmd['rank']} {cmd['name']} — {node.get('commanderTitle') or 'Chỉ huy'}"
                if cmd else f"⚠️ Chưa có chỉ huy ({node.get('commanderTitle') or 'Chỉ huy'})"
            )
            cmd_color = TEXT_MUTED if cmd else RED
            cnt = sum(1 for s in soldiers
                      if any(s["unitId"] == u for u in _all_unit_ids(node)))
            is_root = node["id"] == "root"
            children_ctrls.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Row(
                                        [ft.Text(node["name"], size=13,
                                                 weight=ft.FontWeight.BOLD)] +
                                        ([ft.Container(
                                            content=ft.Text("ADMIN", size=9,
                                                            color=ft.Colors.BLACK,
                                                            weight=ft.FontWeight.BOLD),
                                            bgcolor=GOLD, border_radius=6,
                                            padding=ft.padding.symmetric(horizontal=6, vertical=1),
                                        )] if is_root else []),
                                        spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    ),
                                    ft.Text(cmd_line, size=10, color=cmd_color),
                                ],
                                spacing=2, expand=True, tight=True,
                            ),
                            ft.Text(f"{cnt}/{cnt}", size=12, color=TEXT_MUTED,
                                    weight=ft.FontWeight.BOLD),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.padding.only(left=10 + depth * 16, right=12, top=10, bottom=10),
                    bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                    on_click=lambda e, n=node: self._open_unit_members(n),
                    ink=True,
                )
            )
            for child in node.get("children", []):
                children_ctrls.extend(unit_node(child, depth + 1))
            return children_ctrls

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
                                            ft.Text(f"{sum(1 for _s in soldiers if not _s.get('isAdmin'))} quân nhân",
                                                    size=10, color=TEXT_MUTED),
                                        ],
                                    ),
                                    padding=12, bgcolor=BG,
                                    border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                                ),
                            ] + unit_node(tree),
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
                    store.set_value("soldiers", soldiers2)
                    if _looks_like_firebase_uid(target_sid):
                        try:
                            FS.set_doc(f"users/{target_sid}", {"accountStatus": "active"})
                        except Exception:
                            pass
                    self.toast("✅ Đã duyệt tài khoản")
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

            acc_rows: list[ft.Control] = []
            for s in sorted_sol:
                uname = str(s.get("username") or "").strip() or "—"
                disp_name = str(s.get("name") or "").strip() or "—"
                sid = str(s.get("id") or "")
                btn: ft.Control | None = None
                if self._is_admin() and sid and not _is_super_admin_username(uname):
                    btn = ft.Row(
                        [
                            ft.ElevatedButton(
                                "✅ Duyệt",
                                on_click=lambda e, _sid=sid: self.approve_member_account(_sid),
                                bgcolor=GREEN_MID, color=ft.Colors.WHITE,
                                style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=12, vertical=8)),
                            ),
                            ft.OutlinedButton(
                                "❌ Từ chối",
                                on_click=lambda e, _sid=sid: self.confirm_delete_soldier(_sid),
                                style=ft.ButtonStyle(color=RED, padding=ft.padding.symmetric(horizontal=12, vertical=8)),
                            ),
                        ],
                        spacing=6, tight=True,
                    )
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
                                        ft.Text(disp_name, size=11, color=TEXT_MUTED),
                                    ],
                                    spacing=2, expand=True, tight=True,
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
                ),
                bgcolor=BG,
                border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                padding=ft.padding.symmetric(horizontal=10, vertical=4),
            )

        if current_sub == "tree":
            main_body = tree_panel()
        elif current_sub == "pending":
            main_body = pending_panel()
        else:
            main_body = accounts_panel()

        col_children: list[ft.Control] = [self.module_back_bar("👥 Quản lý quân nhân")]
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
        members.sort(key=lambda x: (-int(x.get("adminLevel") or 0), x.get("name") or ""))

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
        header = ft.Container(
            content=ft.Row(
                [
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
                ],
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
                    page.bottom_sheet.open = False
                except Exception:
                    pass
                page.update()

            def save_picker(_):
                state["selected"] = {cb.label for cb in unit_cbs if cb.value}
                summary_label.value = _summary_text()
                close_sheet()

            page.bottom_sheet = ft.BottomSheet(
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
                page.dialog.open = False
            except Exception:
                pass
            page.update()

        def do_edit(e):
            close_dlg()
            self.f47_open_create(existing=camp)

        def do_delete(e):
            close_dlg()
            self.f47_confirm_delete(camp)

        page.dialog = ft.AlertDialog(
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
        page.dialog.open = True
        page.update()

    def f47_confirm_delete(self, camp: dict) -> None:
        """Xác nhận trước khi xoá chiến dịch."""
        page = self.page
        cid = camp.get("id", "")
        title = camp.get("title", "")

        def do(e):
            try:
                page.dialog.open = False
            except Exception:
                pass
            camps = store.get("f47Campaigns", store.seed_f47)
            camps = [c for c in camps if c.get("id") != cid]
            store.set_value("f47Campaigns", camps)
            store.log_activity(f"Xoá F47: {title[:40]}")
            self.toast(f"🗑 Đã xoá: {title[:40]}")
            self.body.content = self.module_f47()
            self.refresh()

        page.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Xác nhận xoá", weight=ft.FontWeight.BOLD),
            content=ft.Text(
                f"Xoá chiến dịch '{title}'?\nTất cả minh chứng đã nộp sẽ mất.",
                size=12,
            ),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
                ft.ElevatedButton("Xoá", on_click=do,
                                  bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

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
                    if f.path:
                        remote = fb_storage.make_remote_path(f"f47/samples/{AUTH_STATE['uid']}", f.name)
                        res = fb_storage.upload_file(f.path, remote, AUTH_STATE["idToken"])
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
                for muid in members:
                    if muid:
                        store.push_notif(
                            "f47", "🛡 Chiến dịch F47 mới cho bạn",
                            f"{title} • Hạn {hours}h", _link,
                            target_uid=muid,
                        )
                self.toast(f"Đã phát động: {title}")
            try:
                page.dialog.open = False
            except Exception:
                pass
            self.body.content = self.module_f47()
            self.page.update()

        page.dialog = ft.AlertDialog(
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
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
                ft.ElevatedButton(
                    "Lưu thay đổi" if is_edit else "Phát động",
                    on_click=submit,
                    bgcolor=GREEN_MID if is_edit else RED,
                    color=ft.Colors.WHITE,
                ),
            ],
        )
        page.dialog.open = True
        page.update()

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
                        if not f.path:
                            continue
                        remote = fb_storage.make_remote_path(
                            f"shares/{AUTH_STATE['uid']}", f.name
                        )
                        res = fb_storage.upload_file(
                            f.path, remote, AUTH_STATE["idToken"]
                        )
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
                page.dialog.open = False
            except Exception:
                pass
            self.toast("✅ Đã đăng bài chia sẻ")
            self.body.content = self.module_f47()
            self.refresh()

        page.dialog = ft.AlertDialog(
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
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
                ft.ElevatedButton("Đăng bài", on_click=submit,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

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
                        ft.Text(f"đ.c {cur.get('creator','')} – {cur.get('creatorRole','')}",
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
                            ft.Image(src=cur["sampleMediaUrl"], border_radius=8, width=300, fit=ft.ImageFit.CONTAIN)
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
            store.push_notif(
                "f47", f"📢 Bạn được giục",
                f"Hãy làm chiến dịch '{cur.get('title','')[:60]}'",
                "f47",
                target_uid=uid,
            )
            try:
                fcm.queue_notification(
                    FS, uid,
                    title="📢 Giục F47",
                    body=f"Bạn cần làm: {cur.get('title','')[:60]}",
                    link="f47",
                )
            except Exception:
                pass
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
            "nganh": [
                ("Chính uỷ", "CU"), ("Phó Chính uỷ", "PCU"),
                ("Chủ nhiệm Chính trị", "CNCT"), ("Phó Chủ nhiệm Chính trị", "PCNCT"),
                ("Cán bộ", "CB"), ("Tuyên huấn", "TH"), ("Tổ chức", "TC"),
                ("Công tác quần chúng", "QC"), ("Dân vận", "DV"), ("Chính sách", "CS"),
                ("Bảo vệ an ninh", "BV"), ("Uỷ ban kiểm tra", "UBKT"), ("Thống kê", "TK")
            ],
            "lead_codes": {"CU", "PCU", "CNCT", "PCNCT"},
            "lead_keywords": ["chính uỷ", "chủ nhiệm chính trị"],
            "follower_keywords": ["chính uỷ", "phó chính uỷ", "chủ nhiệm chính trị", "phó chủ nhiệm chính trị", "trợ lý"]
        },
        "hcqs": {
            "title": "Hành chính - Quân sự",
            "store_key": "hcqsTasks",
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
            "follower_keywords": ["trung đoàn trưởng", "tham mưu trưởng", "trợ lý", "nhân viên"]
        },
        "pttd": {
            "title": "Phong trào thi đua",
            "store_key": "pttdTasks",
            "nganh": [
                ("Thường trực Thi đua", "TT"), ("Khối Cơ quan", "CQ"), ("Khối Tiểu đoàn", "D")
            ],
            "lead_codes": {"TT"},
            "lead_keywords": ["thường trực thi đua"],
            "follower_keywords": ["trợ lý", "chính uỷ", "chủ nhiệm"]
        }
    }

    TASK_LEAD_TITLE_KEYWORDS = (
        "đại đội", "tiểu đoàn", "chính uỷ", "chủ nhiệm",
        "trưởng ban", "tham mưu", "trung đoàn",
    )

    def _task_lead_members(self) -> list[str]:
        """Trả id list các 'đầu mối' nhận nhiệm vụ CTĐ-CTCT — đại đội trưởng
        và cấp trên (trừ admin)."""
        out = []
        for s in store.get("soldiers", store.seed_soldiers):
            if s.get("isAdmin"):
                continue
            role = (s.get("role") or "").lower()
            if any(k in role for k in self.TASK_LEAD_TITLE_KEYWORDS):
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
            main_body = ft.ListView(
                controls=[ft.Container(
                    content=ft.Column(
                        rows if rows else [ft.Container(
                            height=60), ft.Text("Chưa có báo cáo nào",
                                                color=TEXT_MUTED, size=13,
                                                text_align=ft.TextAlign.CENTER)],
                        spacing=0,
                    ),
                    padding=10,
                )],
                expand=True,
            )
        else:
            # Filter theo ngành — icon đã ở tabs_bar (hourglass), chỉ hiện dropdown khi mở
            ng_filter = getattr(self, f"task_{domain}_filter", "")
            filter_open = getattr(self, f"task_{domain}_filter_open", False)

            def on_filter_change(e):
                setattr(self, f"task_{domain}_filter", e.control.value or "")
                self.body.content = self.module_ctdctct()
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

            task_list = ft.ListView(
                controls=[ft.Container(
                    content=ft.Column([card(t) for t in tasks_view], spacing=0)
                            if tasks_view else
                            ft.Column([
                                ft.Container(height=80),
                                ft.Text(empty_msg, text_align=ft.TextAlign.CENTER,
                                        color=TEXT_MUTED, size=14),
                                ft.Container(height=10),
                                ft.Text("Bấm nút  +  ở góc phải để triển khai",
                                        text_align=ft.TextAlign.CENTER,
                                        color=TEXT_MUTED, size=12),
                            ]),
                    padding=10,
                )],
                expand=True,
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
                self.body.content = self.module_ctdctct()
                self.refresh()

        # Top bar gộp: back arrow + title + tabs + (icon filter ngành nếu ở tab Nhiệm vụ)
        ng_filter_now = getattr(self, f"task_{domain}_filter", "")
        filter_open_now = getattr(self, f"task_{domain}_filter_open", False)

        def _toggle_filter_top(e=None):
            setattr(self, f"task_{domain}_filter_open", not getattr(self, f"task_{domain}_filter_open", False))
            self.body.content = self.module_ctdctct()
            self.refresh()

        # Back arrow đã chuyển lên top header — tabs_bar chỉ giữ tabs + filter icon
        top_row_children = [
            ft.Container(
                content=ft.Tabs(
                    selected_index=sel,
                    on_change=on_tab_changed,
                    tabs=[ft.Tab(text=label) for label, _ in tab_defs],
                ),
                expand=True,
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
                    bgcolor=GREEN_DARK, foreground_color=ft.Colors.WHITE,
                    tooltip="Triển khai nhiệm vụ CTĐ-CTCT",
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
                        if not f.path:
                            continue
                        remote = fb_storage.make_remote_path(
                            f"ctdctct/attach/{AUTH_STATE['uid']}", f.name
                        )
                        res = fb_storage.upload_file(f.path, remote,
                                                    AUTH_STATE["idToken"])
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
            # CTĐ-CTCT: đầu mối = chỉ huy đại đội trở lên (không phải toàn quân nhân)
            members = self._task_lead_members()
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
                if followers:
                    _link_f = f"ctdctct:{new_task['id']}"
                    for fuid in followers:
                        store.push_notif("ctdctct", f"🎖 [{code}] Bạn là người theo dõi",
                                         f"{title} • Hạn {hours}h", _link_f,
                                         target_uid=fuid)
                else:
                    # Targeted: chỉ members + người triển khai (không broadcast)
                    _link = f"ctdctct:{new_task['id']}"
                    for muid in members:
                        if muid:
                            store.push_notif(
                                "ctdctct", f"🎖 [{code}] Nhiệm vụ mới cho bạn",
                                f"{title} • Hạn {hours}h", _link,
                                target_uid=muid,
                            )
                self.toast(f"Đã triển khai: [{code}] {title}")
            try:
                page.dialog.open = False
            except Exception:
                pass
            self.body.content = self.module_ctdctct()
            self.refresh()

        page.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                "✏️ Sửa nhiệm vụ CTĐ-CTCT" if is_edit
                else "🎖 Triển khai nhiệm vụ CTĐ-CTCT",
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
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
                ft.ElevatedButton(
                    "Lưu thay đổi" if is_edit else "Triển khai",
                    on_click=submit,
                    bgcolor=GREEN_MID if is_edit else GREEN_DARK,
                    color=ft.Colors.WHITE,
                ),
            ],
        )
        page.dialog.open = True
        page.update()

    def task_open_task_menu(self, domain: str, task: dict) -> None:
        """Popup ✏️ Sửa / 🗑 Xoá cho 1 nhiệm vụ (admin)."""
        page = self.page

        def close_dlg():
            try:
                page.dialog.open = False
            except Exception:
                pass
            page.update()

        def do_edit(e):
            close_dlg(); self.task_open_create(domain, existing=task)

        def do_delete(e):
            close_dlg(); self.task_confirm_delete(domain, task)

        page.dialog = ft.AlertDialog(
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
        page.dialog.open = True
        page.update()

    def task_confirm_delete(self, domain: str, task: dict) -> None:
        page = self.page
        tid = task.get("id", "")
        title = task.get("title", "")

        def do(e):
            try:
                page.dialog.open = False
            except Exception:
                pass
            tasks = store.get(self.TASK_DOMAIN_CONFIG.get(domain, {}).get('store_key'), lambda: [])
            tasks = [t for t in tasks if t.get("id") != tid]
            store.set_value("ctdctctTasks", tasks)
            self.toast(f"🗑 Đã xoá: {title[:40]}")
            self.body.content = self.module_ctdctct()
            self.refresh()

        page.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Xác nhận xoá", weight=ft.FontWeight.BOLD),
            content=ft.Text(f"Xoá nhiệm vụ '{title}'?\nBáo cáo đã nộp sẽ mất.", size=12),
            actions=[
                ft.TextButton("Huỷ",
                              on_click=lambda e: setattr(page.dialog, "open", False)
                                                  or page.update()),
                ft.ElevatedButton("Xoá", on_click=do,
                                  bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

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
                        ft.Image(src=cur["attachments"][0], border_radius=8, width=300, fit=ft.ImageFit.CONTAIN)
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
            if approve:
                store.push_notif("ctdctct", "✅ Báo cáo đã được duyệt",
                                 f"Bạn được duyệt: {cur.get('title','')[:60]}",
                                 "ctdctct", target_uid=uid)
                self.toast(f"✅ Duyệt báo cáo của {s_name}")
            else:
                store.push_notif("ctdctct", "❌ Báo cáo bị từ chối",
                                 f"Cần làm lại: {cur.get('title','')[:60]}",
                                 "ctdctct", target_uid=uid)
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
            store.push_notif("ctdctct", "📢 Bạn được giục",
                             f"Hãy báo cáo nhiệm vụ '{full_title[:60]}'",
                             "ctdctct", target_uid=uid)
            try:
                fcm.queue_notification(FS, uid, title="📢 Giục CTĐ-CTCT",
                                       body=f"Bạn cần báo cáo: {full_title[:60]}",
                                       link="ctdctct")
            except Exception:
                pass
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
            store.push_notif(
                "ctdctct", "✅ Có người xác nhận nhận",
                f"{profile.get('name', uid)} đã nhận: {title[:60]}",
                "ctdctct", target_uid=creator_uid,
            )
        self.toast("✅ Đã xác nhận nhận nhiệm vụ")
        self.task_open_task_detail(domain, task)

    def _task_back_to_list(self, domain: str) -> None:
        setattr(self, f"task_{domain}_view", "tasks")
        self.body.content = self.module_ctdctct()
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
                        if not f.path:
                            continue
                        name_low = (f.name or "").lower()
                        is_video = any(name_low.endswith(ext) for ext in
                                       (".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"))
                        folder = f"reports/task/{AUTH_STATE['uid']}"
                        remote = fb_storage.make_remote_path(folder, f.name)
                        res = fb_storage.upload_file(f.path, remote,
                                                    AUTH_STATE["idToken"])
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
            for au in set(approvers):
                if au and au != uid:
                    store.push_notif(
                        "ctdctct", "📤 Báo cáo CTĐ-CTCT cần duyệt",
                        f"{sender_name} đã báo cáo: {task.get('title','')[:50]}",
                        "ctdctct", target_uid=au,
                    )
            try:
                page.dialog.open = False
            except Exception:
                pass
            self.toast("✅ Đã gửi báo cáo, chờ duyệt")
            self.task_open_task_detail(domain, task)

        _refresh_status()

        page.dialog = ft.AlertDialog(
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
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
                ft.ElevatedButton("Gửi", on_click=submit,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

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
                            res = fb_storage.upload_file(
                                f.path, remote, AUTH_STATE["idToken"]
                            )
                        else:
                            # Web platform: dữ liệu trong bytes (Flet trả qua e.files)
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
                page.dialog.open = False
            except Exception:
                pass
            self.toast("Đã nộp minh chứng F47")
            self.set_tab("util")  # quay lại tiện ích / có thể đổi sang home

        page.dialog = ft.AlertDialog(
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
                              on_click=lambda e: setattr(page.dialog, "open", False)
                                                  or page.update()),
                ft.ElevatedButton("Gửi", on_click=submit,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

    def module_f47(self) -> ft.Control:
        camps = store.get("f47Campaigns", store.seed_f47)
        now_ms = store.now_ms()
        camps = sorted(camps, key=lambda c: (1 if c.get("deadline", 0) <= now_ms else 0, -c.get("deadline", 0)))
        soldiers = store.get("soldiers", store.seed_soldiers)
        soldier_by_id = {str(s.get("id")): s for s in soldiers if s.get("id")}
        current_view = getattr(self, "f47_view", "campaigns")

        def cd_str(c) -> str:
            left = c["deadline"] - store.now_ms()
            if left <= 0:
                return "Hết giờ"
            h, rem = divmod(left, 3600_000)
            m, _ = divmod(rem, 60_000)
            return f"{h:02d}h {m:02d}m"

        def card(c: dict) -> ft.Container:
            done = len(c.get("submissions", {}))
            total = len(c.get("members", []))
            pct = int(done / total * 100) if total else 0

            c_type = c.get("campaignType") or "Khác"
            if c_type == "Báo cáo khẩn":
                badge_bg = "#ffebee"
                badge_fg = "#c62828"
            elif c_type == "Báo cáo":
                badge_bg = "#e3f2fd"
                badge_fg = "#1565c0"
            elif c_type == "CMT":
                badge_bg = "#e0f2f1"
                badge_fg = "#00695c"
            else:
                badge_bg = "#f5f5f5"
                badge_fg = "#616161"

            type_badge = ft.Container(
                content=ft.Text(c_type, size=10, color=badge_fg, weight=ft.FontWeight.BOLD),
                bgcolor=badge_bg,
                border_radius=4,
                padding=ft.padding.symmetric(horizontal=6, vertical=2),
                margin=ft.margin.only(right=6),
            )

            return ft.Container(
                content=ft.Column(
                    [
                        ft.Row([
                            type_badge,
                            ft.Text(c["title"], size=14, weight=ft.FontWeight.BOLD,
                                    expand=True),
                            ft.Container(
                                content=ft.Text("🔴 Live" if c["status"] == "live" else "Done",
                                                size=10, color=ft.Colors.WHITE,
                                                weight=ft.FontWeight.BOLD),
                                bgcolor=RED if c["status"] == "live" else "#999",
                                border_radius=10,
                                padding=ft.padding.symmetric(horizontal=8, vertical=3),
                            ),
                            *([ft.IconButton(
                                ft.Icons.MORE_VERT,
                                icon_size=18,
                                tooltip="Tuỳ chọn",
                                on_click=lambda e, _c=c: self.f47_open_camp_menu(_c),
                            )] if self._is_admin() else []),
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        ft.Text(f"Phát động bởi đ.c {c['creator']} – {c['creatorRole']}",
                                size=11, color=TEXT_MUTED),
                        ft.Text(c["desc"], size=12, color=TEXT_MUTED),
                        *([
                            ft.Container(
                                content=ft.Row([
                                    ft.Icon(ft.Icons.LINK, size=14, color=BLUE),
                                    ft.Text(
                                        c["targetLink"],
                                        size=11,
                                        color=BLUE,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                        expand=True
                                    ),
                                    ft.Icon(ft.Icons.OPEN_IN_NEW, size=12, color=BLUE)
                                ], spacing=6),
                                bgcolor="#eff6ff",
                                border_radius=6,
                                padding=ft.padding.symmetric(horizontal=8, vertical=6),
                                on_click=lambda e, url=c["targetLink"]: self.page.launch_url(url),
                            )
                        ] if c.get("targetLink") else []),
                        ft.Container(
                            content=ft.Row([
                                ft.Text("⏱", size=18),
                                ft.Column([ft.Text("Thời gian còn lại", size=11, color="#633806"),
                                           ft.Text(cd_str(c), size=16, color="#ba7517",
                                                   weight=ft.FontWeight.BOLD)],
                                          spacing=0, tight=True),
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
                        ft.ProgressBar(value=pct / 100, color=GREEN_MID, bgcolor="#eee", height=8),
                        ft.Row([
                            ft.Container(content=ft.Column(
                                [ft.Text(str(done), size=18, weight=ft.FontWeight.BOLD,
                                         color=GREEN_MID),
                                 ft.Text("✅ Đã làm", size=10, color=TEXT_MUTED)],
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
                                bgcolor=BG2, border_radius=8, padding=8, expand=True,
                                alignment=ft.alignment.center),
                            ft.Container(content=ft.Column(
                                [ft.Text(str(total - done), size=18, weight=ft.FontWeight.BOLD,
                                         color=RED),
                                 ft.Text("❌ Chưa", size=10, color=TEXT_MUTED)],
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
                                bgcolor=BG2, border_radius=8, padding=8, expand=True,
                                alignment=ft.alignment.center),
                        ], spacing=6),
                        # Admin / chỉ huy KHÔNG nộp minh chứng — họ là người duyệt
                        *([ft.ElevatedButton(
                            "📤 Nộp minh chứng",
                            on_click=lambda e, _c=c: self.f47_open_submit(_c),
                            bgcolor=GREEN_MID, color=ft.Colors.WHITE,
                            width=10000,
                            style=ft.ButtonStyle(
                                shape=ft.RoundedRectangleBorder(radius=8),
                                padding=10,
                            ),
                        )] if not self._is_admin() else []),
                    ],
                    spacing=8,
                ),
                bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=12,
                padding=14, margin=ft.margin.only(bottom=10),
                on_click=lambda e, _c=c: self.f47_open_campaign_detail(_c),
                ink=True,
            )

        def leaderboard_view() -> ft.Control:
            # 2 trục lọc: nguồn (camp/share) × cấp (person/unit)
            lb_source = getattr(self, "lb_source", "camp")  # "camp" hoặc "share"
            lb_level = getattr(self, "lb_level", "person")  # "person" hoặc "unit"
            unit_name_map = self._unit_name_map(store.get("units", store.seed_units))

            # Build danh sách item thô (mỗi minh chứng / mỗi share là 1 row)
            events: list[dict] = []  # {uid, unitId, at, name}
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
                shares = store.get("dailyShares", store.seed_daily_shares)
                for sh in shares:
                    uid = str(sh.get("userId") or "")
                    s = soldier_by_id.get(uid) or {}
                    events.append({
                        "uid": uid,
                        "unitId": s.get("unitId") or "",
                        "at": int(sh.get("at", 0) or 0),
                        "name": sh.get("userName") or s.get("name") or "",
                    })

            # Tổng hợp theo person hoặc unit
            scores: dict[str, dict] = {}
            for ev in events:
                if lb_level == "person":
                    key = ev["uid"]
                    if not key:
                        continue
                    label = ev.get("name") or key
                else:
                    key = ev.get("unitId") or "unknown"
                    label = unit_name_map.get(key, "Khác")
                it = scores.setdefault(key, {
                    "count": 0, "lastAt": 0, "name": label,
                })
                it["count"] += 1
                if ev["at"] > it["lastAt"]:
                    it["lastAt"] = ev["at"]
                if not it.get("name"):
                    it["name"] = label

            ranked = sorted(scores.items(),
                            key=lambda kv: (kv[1]["count"], kv[1]["lastAt"]),
                            reverse=True)
            rows = []

            # Header bar: chip filter
            def _chip(text: str, selected: bool, on_click) -> ft.Container:
                return ft.Container(
                    content=ft.Text(text, size=11,
                                    color=ft.Colors.WHITE if selected else TEXT,
                                    weight=ft.FontWeight.BOLD),
                    bgcolor=GREEN_MID if selected else BG2,
                    border_radius=14,
                    padding=ft.padding.symmetric(horizontal=12, vertical=6),
                    on_click=on_click,
                    ink=True,
                )

            def _set_source(v):
                self.lb_source = v
                self.body.content = self.module_f47()
                self.refresh()

            def _set_level(v):
                self.lb_level = v
                self.body.content = self.module_f47()
                self.refresh()

            chip_bar = ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text("Nguồn:", size=11, color=TEXT_MUTED, width=50),
                        _chip("🛡 Chiến dịch", lb_source == "camp",
                              lambda e: _set_source("camp")),
                        _chip("📤 Chia sẻ", lb_source == "share",
                              lambda e: _set_source("share")),
                    ], spacing=6),
                    ft.Row([
                        ft.Text("Cấp:", size=11, color=TEXT_MUTED, width=50),
                        _chip("👤 Cá nhân", lb_level == "person",
                              lambda e: _set_level("person")),
                        _chip("🏢 Đơn vị", lb_level == "unit",
                              lambda e: _set_level("unit")),
                    ], spacing=6),
                ], spacing=6),
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                bgcolor=BG, border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            )

            if not ranked:
                empty_msg = (
                    "Chưa có ai nộp minh chứng nào." if lb_source == "camp"
                    else "Chưa có bài chia sẻ nào."
                )
                return ft.Column([
                    chip_bar,
                    ft.Container(
                        content=ft.Column([
                            ft.Container(height=40),
                            ft.Text(empty_msg, size=13, color=TEXT_MUTED,
                                    text_align=ft.TextAlign.CENTER),
                        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                        padding=20, expand=True,
                    ),
                ], spacing=0, expand=True)

            if not ranked:
                return ft.Container(
                    content=ft.Column(
                        [
                            ft.Container(height=60),
                            ft.Text("Chưa có dữ liệu xếp hạng", size=14, color=TEXT_MUTED,
                                    text_align=ft.TextAlign.CENTER),
                            ft.Text("Hãy nộp minh chứng trong một chiến dịch để lên bảng xếp hạng.",
                                    size=12, color=TEXT_MUTED, text_align=ft.TextAlign.CENTER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=8,
                    ),
                    padding=20,
                )

            unit_label = "🛡 minh chứng" if lb_source == "camp" else "📤 chia sẻ"

            def rank_row(idx: int, key: str, info: dict) -> ft.Container:
                if lb_level == "person":
                    s = soldier_by_id.get(key) or {}
                    display = (
                        f"{(s.get('rank') or '').strip()} {(s.get('name') or '').strip()}".strip()
                        or info.get("name") or key
                    )
                else:
                    display = info.get("name") or key
                medal = "🥇" if idx == 0 else ("🥈" if idx == 1 else ("🥉" if idx == 2 else f"#{idx+1}"))
                last = info.get("lastAt", 0) or 0
                last_txt = fmt_dt(int(last)) if last else "-"
                breakdown = f"{info.get('count', 0)} {unit_label}"
                return ft.Container(
                    content=ft.Row(
                        [
                            ft.Text(str(medal), size=14, width=40),
                            ft.Column(
                                [
                                    ft.Text(display, size=13, weight=ft.FontWeight.W_700),
                                    ft.Text(breakdown, size=10, color=TEXT_MUTED),
                                    ft.Text(f"Hoạt động gần nhất: {last_txt}",
                                            size=10, color=TEXT_MUTED),
                                ],
                                spacing=2, tight=True, expand=True,
                            ),
                            ft.Container(
                                content=ft.Text(str(info.get("count", 0)), size=12,
                                                color=ft.Colors.WHITE,
                                                weight=ft.FontWeight.BOLD),
                                bgcolor=GREEN_MID, border_radius=12,
                                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor=BG, border=ft.border.all(1, BORDER),
                    border_radius=12, padding=12,
                    margin=ft.margin.only(bottom=8),
                )

            for i, (key, info) in enumerate(ranked[:50]):
                rows.append(rank_row(i, key, info))

            title_txt = (
                f"🏆 Xếp hạng "
                + ("Chiến dịch" if lb_source == "camp" else "Chia sẻ")
                + " — "
                + ("Cá nhân" if lb_level == "person" else "Đơn vị")
            )

            return ft.Column([
                chip_bar,
                ft.ListView(
                    controls=[ft.Container(
                        content=ft.Column(
                            [
                                ft.Container(
                                    content=ft.Text(title_txt, size=13,
                                                    weight=ft.FontWeight.BOLD),
                                    padding=ft.padding.only(bottom=8),
                                ),
                                *rows,
                            ],
                            spacing=0,
                        ),
                        padding=10,
                    )],
                    expand=True, padding=0,
                ),
            ], spacing=0, expand=True)

        # ---- VIEW: Chia sẻ ----
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

            def share_item(s: dict) -> ft.Container:
                user_name = s.get("userName") or "?"
                platform = s.get("platform") or ""
                links = s.get("links") or []
                images = s.get("images") or []
                note = s.get("note") or ""
                at_txt = time_ago(s.get("at", 0))
                ctrls = [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Text(initials(user_name, 2),
                                                color=ft.Colors.WHITE, size=11,
                                                weight=ft.FontWeight.BOLD),
                                bgcolor=GREEN_DARK, width=32, height=32,
                                border_radius=16, alignment=ft.alignment.center,
                            ),
                            ft.Column(
                                [
                                    ft.Text(user_name, size=13, weight=ft.FontWeight.W_700),
                                    ft.Text(f"{platform} • {at_txt}", size=10, color=TEXT_MUTED),
                                ],
                                spacing=2, expand=True, tight=True,
                            ),
                        ],
                        spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ]
                if note:
                    ctrls.append(ft.Text(note, size=12, color=TEXT))
                for url in links[:3]:
                    ctrls.append(
                        ft.Container(
                            content=ft.Row(
                                [ft.Icon(ft.Icons.LINK, size=14, color=BLUE),
                                 ft.Text(url, size=11, color=BLUE,
                                         overflow=ft.TextOverflow.ELLIPSIS, expand=True)],
                                spacing=6,
                            ),
                            bgcolor="#eff6ff", border_radius=6,
                            padding=ft.padding.symmetric(horizontal=8, vertical=5),
                        )
                    )
                if images:
                    ctrls.append(ft.Text(f"📎 {len(images)} ảnh đính kèm",
                                         size=11, color=TEXT_MUTED))
                return ft.Container(
                    content=ft.Column(ctrls, spacing=6, tight=True),
                    bgcolor=BG, border=ft.border.all(1, BORDER), border_radius=10,
                    padding=12, margin=ft.margin.only(bottom=8),
                )

            counter_card = ft.Container(
                content=ft.Row(
                    [
                        ft.Column(
                            [ft.Text(str(my_today), size=22,
                                     weight=ft.FontWeight.BOLD, color=GREEN_DARK),
                             ft.Text("Bài của tôi hôm nay", size=10, color=TEXT_MUTED)],
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=2, tight=True, expand=True,
                        ),
                        ft.VerticalDivider(width=1, color=BORDER),
                        ft.Column(
                            [ft.Text(str(today_count), size=22,
                                     weight=ft.FontWeight.BOLD, color=BLUE),
                             ft.Text("Toàn đơn vị hôm nay", size=10, color=TEXT_MUTED)],
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=2, tight=True, expand=True,
                        ),
                        ft.VerticalDivider(width=1, color=BORDER),
                        ft.Column(
                            [ft.Text(str(len(shares)), size=22,
                                     weight=ft.FontWeight.BOLD, color=AMBER),
                             ft.Text("Tổng bài chia sẻ", size=10, color=TEXT_MUTED)],
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=2, tight=True, expand=True,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor="#f0f9f4", border_radius=10,
                padding=12, margin=ft.margin.only(bottom=10),
                border=ft.border.all(1, "#c8e6c9"),
            )

            list_items = [counter_card]
            if shares:
                list_items.extend(share_item(s) for s in shares[:100])
            else:
                list_items.append(ft.Container(
                    content=ft.Column(
                        [
                            ft.Container(height=20),
                            ft.Text("📭 Chưa có bài chia sẻ nào",
                                    text_align=ft.TextAlign.CENTER,
                                    color=TEXT_MUTED, size=14),
                            ft.Text("Bấm nút  +  ở góc phải để chia sẻ bài đầu tiên",
                                    text_align=ft.TextAlign.CENTER,
                                    color=TEXT_MUTED, size=12),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6,
                    ),
                    padding=20,
                ))

            return ft.ListView(
                controls=[ft.Container(
                    content=ft.Column(list_items, spacing=0),
                    padding=10,
                )],
                expand=True,
            )

        # Nội dung chính (back bar + list)
        tab_defs = [
            ("Chiến dịch", "campaigns"),
            ("Chia sẻ", "shares"),
            ("Xếp hạng", "leaderboard"),
        ]
        selected_idx = next((i for i, (_, k) in enumerate(tab_defs) if k == current_view), 0)

        def on_tab_changed(e):
            idx = None
            try:
                idx = e.control.selected_index
            except Exception:
                idx = getattr(e, "selected_index", None)
            if idx is None:
                return
            if 0 <= idx < len(tab_defs):
                self.f47_view = tab_defs[idx][1]
                self.body.content = self.module_f47()
                self.refresh()

        # Tabs only — back arrow đã chuyển lên top header chính
        tabs_bar = ft.Container(
            content=ft.Tabs(
                selected_index=selected_idx,
                on_change=on_tab_changed,
                tabs=[ft.Tab(text=label) for label, _ in tab_defs],
            ),
            bgcolor=BG,
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            padding=ft.padding.symmetric(horizontal=10, vertical=4),
        )

        campaigns_list = ft.ListView(
            controls=[ft.Container(
                content=ft.Column([card(c) for c in camps], spacing=0)
                        if camps else
                        ft.Column([
                            ft.Container(height=80),
                            ft.Text("📭 Chưa có chiến dịch nào",
                                    text_align=ft.TextAlign.CENTER,
                                    color=TEXT_MUTED, size=14),
                            ft.Container(height=10),
                            ft.Text("Bấm nút  +  ở góc phải dưới để phát động",
                                    text_align=ft.TextAlign.CENTER,
                                    color=TEXT_MUTED, size=12),
                        ]),
                padding=10,
            )],
            expand=True,
        )

        if current_view == "campaigns":
            main_body = campaigns_list
        elif current_view == "shares":
            main_body = shares_view()
        else:
            main_body = leaderboard_view()

        # Bỏ module_back_bar — back arrow đã gộp vào tabs_bar để tiết kiệm không gian
        body_col = ft.Column(
            [
                tabs_bar,
                main_body,
            ],
            spacing=0,
            expand=True,
        )

        # FAB ở góc phải dưới — đổi icon + tooltip + on_click theo tab hiện tại
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
        return ft.Stack([body_col, fab], expand=True)

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
            store.set_value("soldiers", soldiers)
            if _looks_like_firebase_uid(soldier_id):
                try:
                    FS.set_doc(f"users/{soldier_id}", {"accountStatus": "active"})
                except Exception:
                    pass
            self.toast("✅ Đã duyệt tài khoản")
            # Refresh currently viewing screen
            if self.tab == "profile" or self.overlay_from_tab == "profile":
                self.body.content = self.view_member_profile(soldier_id)
            elif self.tab == "unit" or self.tab == "util":
                if self.current_module == "units":
                    self.body.content = self.module_units()
            self.refresh()

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
                                ft.OutlinedButton(
                                    "🛡 Phân quyền",
                                    on_click=lambda e, _sid=str(soldier_id): self.open_units_assign_role_dialog(
                                        _sid, return_to_member_view=True,
                                    ),
                                ),
                                ft.OutlinedButton(
                                    "✏️ Sửa",
                                    on_click=lambda e, _sid=str(soldier_id): self.open_member_profile_edit_dialog(_sid),
                                ),
                                ft.OutlinedButton(
                                    "🗑️ Xóa",
                                    style=ft.ButtonStyle(color=RED),
                                    on_click=lambda e, _sid=str(soldier_id): self.confirm_delete_soldier(_sid),
                                ),
                                ft.OutlinedButton(
                                    "🔑 Đặt lại MK",
                                    style=ft.ButtonStyle(color=AMBER),
                                    on_click=lambda e, _u=str(s.get("username") or ""): self.reset_member_password(_u),
                                ),
                            ],
                            spacing=6,
                            wrap=True,
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
                                    ft.Text(f"{s.get('username') or '—'}", color=ft.Colors.WHITE70, size=12),
                                    ft.Text(f"{s.get('rank', '')} • {s.get('role', '')}",
                                            color=ft.Colors.WHITE70, size=12),
                                    ft.Text(unit_lbl or "—", color=ft.Colors.WHITE54, size=11),
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
                                "💬 Nhắn tin",
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
                page.dialog.open = False
                
                if on_complete:
                    on_complete()
            except Exception as err:
                presence_err.value = f"❌ Lỗi: {err}"
                page.update()
                
        page.dialog = ft.AlertDialog(
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
                ft.TextButton("Hủy", on_click=lambda _: setattr(page.dialog, "open", False) or page.update()),
                ft.ElevatedButton("Lưu", on_click=save_presence, bgcolor=GREEN_MID, color=ft.Colors.WHITE)
            ]
        )
        page.dialog.open = True
        page.update()

    def open_change_avatar(self) -> None:
        page = self.page
        if not store.STORE.is_bound() or not AUTH_STATE.get("idToken"):
            self.toast("Cần đăng nhập để đổi ảnh")
            return

        def on_pick(e: ft.FilePickerResultEvent):
            if not e.files:
                return
            fp = e.files[0].path
            if not fp:
                self.toast("Không đọc được file")
                return
            uid = str(AUTH_STATE.get("localId") or AUTH_STATE.get("uid") or "")
            if not uid:
                return
            try:
                remote = fb_storage.make_remote_path(f"avatars/{uid}", Path(fp).name)
                res = fb_storage.upload_file(fp, remote, AUTH_STATE["idToken"])
                url = str(res.get("downloadURL") or "")
                if not url:
                    self.toast("Upload không trả về URL")
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
                self.toast("Đã cập nhật ảnh đại diện")
            except Exception:
                self.toast("Upload ảnh thất bại")

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
        unit_dd = ft.Dropdown(
            label="Đơn vị",
            value=cur_unit_id or None,
            options=[ft.dropdown.Option(k, lbl) for k, lbl in unit_opts_full],
            border_radius=8, dense=True,
        )

        rank_dd = ft.Dropdown(
            label="Cấp bậc",
            value=str(row.get("rank") or "") or None,
            options=[ft.dropdown.Option(r) for r in _opts_with_current(
                list(store.RANKS), str(row.get("rank") or ""))],
            border_radius=8, dense=True,
        )

        role_dd = ft.Dropdown(
            label="Chức vụ",
            value=str(row.get("role") or "") or None,
            options=[ft.dropdown.Option(t) for t in _opts_with_current(
                list(store.TITLES), str(row.get("role") or ""))],
            border_radius=8, dense=True,
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
            page.dialog.open = False
            page.update()
            self.toast("Đã lưu hồ sơ")
            if getattr(self, "overlay_soldier_id", None) == sid:
                self.body.content = self.view_member_profile(sid)
            self.refresh()

        page.dialog = ft.AlertDialog(
            modal=True,
            bgcolor=BG,
            title=ft.Text(f"Sửa hồ sơ • {uname}", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [user_ro, name_tf, rank_dd, role_dd, unit_dd,
                     status_dd, is_admin_cb, phone_tf, err_t],
                    spacing=10, tight=True, scroll=ft.ScrollMode.AUTO,
                ),
                width=380, height=540,
            ),
            actions=[
                ft.TextButton("Huỷ", on_click=lambda e: setattr(page.dialog, "open", False) or page.update()),
                ft.ElevatedButton("Lưu", on_click=save, bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

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
            page.dialog.open = False
            page.update()
            self.toast("Đã xóa khỏi danh sách")
            if getattr(self, "overlay_soldier_id", None) == sid:
                self.overlay_soldier_id = None
                self.overlay_from_tab = None
                self.body.content = self.view_contacts()
                self.tab = "contacts"
            self.refresh()

        page.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Xóa thành viên?", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Text(
                f"Xóa {row.get('name')} ({row.get('username')}) khỏi danh sách quân nhân?\n"
                "Thao tác này không xóa tài khoản Firebase (chỉ gỡ khỏi app).",
                size=13,
            ),
            actions=[
                ft.TextButton("Huỷ", on_click=lambda e: setattr(page.dialog, "open", False) or page.update()),
                ft.ElevatedButton("Xóa", on_click=do_delete, bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

    def view_contacts(self) -> ft.Control:
        # Loại admin + chính mình khỏi danh bạ
        my_uid = AUTH_STATE.get("uid") or AUTH_STATE.get("localId") or ""
        soldiers = [s for s in store.get("soldiers", store.seed_soldiers)
                    if not s.get("isAdmin") and str(s.get("id")) != str(my_uid)]
        groups: dict[str, list[dict]] = {}
        unit_names = self._unit_name_map(store.get("units", store.seed_units))
        for s in soldiers:
            unit = unit_names.get(s.get("unitId", ""), "Khác")
            groups.setdefault(unit, []).append(s)

        sections: list[ft.Control] = []
        for unit, members in groups.items():
            sections.append(
                ft.Container(
                    content=ft.Text(unit, size=11, weight=ft.FontWeight.BOLD,
                                    color=TEXT_MUTED),
                    bgcolor=BG2, padding=ft.padding.symmetric(horizontal=12, vertical=6),
                )
            )
            for s in members:
                sections.append(self._contact_row(s))

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
        name_txt = ft.Container(
            content=ft.Text(s["name"], size=14, weight=ft.FontWeight.W_600),
            on_click=lambda e, _s=s: self.open_member_profile(_s),
            ink=True,
        )
        sub_txt = ft.Text(
            f"{s['rank']} • {s.get('role', '')} • {s.get('phone', '')}",
            size=11, color=TEXT_MUTED,
        )
        return ft.Container(
            content=ft.Row(
                [
                    self._soldier_avatar(s.get("name") or "", pic, 44),
                    ft.Column(
                        [name_txt, sub_txt],
                        expand=True, spacing=2, tight=True,
                    ),
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
                page.dialog.open = False
            except Exception:
                pass
            page.update()

        def do_hide(_):
            close_dlg()
            self._chat_confirm_hide(rid, name)

        def do_delete_both(_):
            close_dlg()
            self._chat_confirm_delete_both(rid, name)

        page.dialog = ft.AlertDialog(
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
        page.dialog.open = True
        page.update()

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
                page.dialog.open = False
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

        page.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("⚠️ Xác nhận xoá hẳn", weight=ft.FontWeight.BOLD,
                          color=RED),
            content=ft.Text(warn_text, size=12),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
                ft.ElevatedButton("Xoá hẳn", on_click=do,
                                  bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

    def _chat_confirm_hide(self, rid: str, name: str) -> None:
        """Xác nhận trước khi ẩn phòng khỏi list."""
        page = self.page

        def do(_):
            try:
                page.dialog.open = False
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

        page.dialog = ft.AlertDialog(
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
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
                ft.ElevatedButton("Ẩn", on_click=do,
                                  bgcolor=RED, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

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
                                [ft.Text(p.get("name") or "Không tên", color=ft.Colors.WHITE, size=18,
                                         weight=ft.FontWeight.BOLD),
                                 ft.Text(f"{p.get('rank') or ''} • {p.get('role') or ''}",
                                         color=ft.Colors.WHITE70, size=12),
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
                            f"{sum(1 for _s in soldiers if not _s.get('isAdmin'))}/{sum(1 for _s in soldiers if not _s.get('isAdmin'))}",
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
                      ft.Container(content=ft.Text("Quản lý LL47 e141 • v1.0",
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

        name_input = ft.TextField(label="Họ tên", value=p.get("name", ""), border_radius=8, dense=True)
        rank_input = ft.TextField(label="Cấp bậc", value=p.get("rank", ""), border_radius=8, dense=True)
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
            new_p.update({
                "name": name,
                "rank": (rank_input.value or "").strip(),
                "role": (role_input.value or "").strip(),
                "phone": (phone_input.value or "").strip(),
                "unitName": (unit_input.value or "").strip(),
                "hometown": (hometown_input.value or "").strip(),
            })
            store.set_value("userProfile", new_p)
            try:
                page.dialog.open = False
            except Exception:
                pass
            self.toast("✅ Đã cập nhật thông tin cá nhân")
            self.body.content = self.view_profile()
            self.refresh()

        page.dialog = ft.AlertDialog(
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
                ft.TextButton("Huỷ", on_click=lambda e: setattr(page.dialog, "open", False) or page.update()),
                ft.ElevatedButton("Lưu", on_click=save, bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

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
    # ===== GUEST WORKFLOW: helpers chuỗi duyệt nhiều cấp     =====
    # ============================================================

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
        cur = my_unit
        while cur and parent_map.get(cur) and parent_map[cur] != "root":
            cur = parent_map[cur]
        # cur giờ là đơn vị cấp 1 (trực tiếp con của root) chứa my_unit
        if cur == "u-bch":
            return []

        # Nếu cur là cơ quan (u-tm/u-ct/u-hk) hoặc đại đội trực thuộc (u-c14..u-c25)
        # → cấp trên DUY NHẤT là u-bch (Ban chỉ huy Trung đoàn)
        if cur in ("u-tm", "u-ct", "u-hk") or (cur or "").startswith("u-c"):
            ancestors = ["u-bch"]
        else:
            # Tiểu đoàn (u-d7/u-d8/u-d9) → trình lên Ban chỉ huy TĐ trước, rồi BCH/e
            tdoan_id = cur  # u-d7
            tdoan_bch_id = f"{cur}-bch"
            if my_unit == tdoan_bch_id or parent_map.get(my_unit) == tdoan_bch_id:
                ancestors = ["u-bch"]
            else:
                ancestors = [tdoan_bch_id, "u-bch"]

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
        peer_top_units: set[str] = set()
        if cur in ("u-tm", "u-ct", "u-hk"):
            peer_top_units = {"u-tm", "u-ct", "u-hk"} - {cur}
        elif cur in ("u-d7", "u-d8", "u-d9"):
            peer_top_units = {"u-d7", "u-d8", "u-d9"} - {cur}
            peer_top_units |= {"u-tm", "u-ct", "u-hk"}
        elif (cur or "").startswith("u-c"):
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
            anc_label = store.canonical_unit_name(node_by_id.get(anc_uid, {"id": anc_uid, "name": name_map.get(anc_uid, "")}))
            candidates = []
            for s in soldiers:
                sid = str(s.get("id"))
                if sid in seen_ids or s.get("isAdmin"):
                    continue
                s_uid = s.get("unitId") or ""
                if not is_in_subtree(s_uid, anc_uid):
                    continue
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
        """Tìm chỉ huy cấp TRÊN của một quân nhân theo cây đơn vị."""
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
        if not unit.startswith("u-bch"):
            return False
        if "phó" in role:
            return False
        return any(k in role for k in ("trung đoàn trưởng", "chính uỷ", "chính ủy"))

    def _guest_append_chain(self, g: dict, action: str, note: str = "") -> dict:
        """Thêm 1 entry vào approvalChain của guest doc."""
        my_profile = store.get("userProfile", store.seed_user_profile)
        entry = {
            "approverId": my_profile.get("id") or AUTH_STATE.get("localId", "unknown"),
            "approverName": my_profile.get("name") or "Cán bộ",
            "approverRole": my_profile.get("role") or "",
            "action": action,
            "note": note,
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

        def _rebuild():
            self.body.content = self.module_guests()
            self.refresh()
        self._rebuild_guests_module = _rebuild

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

        view_toggle = ft.Container(
            content=ft.Row([
                ft.Container(
                    content=ft.Tabs(
                        selected_index=selected_idx,
                        on_change=on_guests_tab_changed,
                        tabs=[ft.Tab(text=label) for label, _ in tab_defs],
                    ),
                    expand=True,
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
            return ft.Stack([stats_col, stats_fab], expand=True)

        all_guests = list(store.get("guests", []) or [])
        soldiers_all = store.get("soldiers", store.seed_soldiers)
        units_data = store.get("units", store.seed_units)

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

        if not hasattr(self, "_guest_selected_ids"):
            self._guest_selected_ids = set()
        valid_ids = {g.get("id") for g in display_guests}
        self._guest_selected_ids &= valid_ids

        soldier_unit_id = {str(s.get("id")): s.get("unitId") or "" for s in soldiers_all}
        def _sort_key(g):
            sid = str(g.get("soldierId"))
            u_id = soldier_unit_id.get(sid, "")
            return (unit_order.get(u_id, 10**9),
                    -(g.get("arrivalTimeMs") or g.get("createdAt") or 0))
        display_guests.sort(key=_sort_key)

        list_view = ft.ListView(expand=True, spacing=6, padding=ft.padding.only(top=8, bottom=80, left=8, right=8))

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

                allow_select = current_view == "manage" and st in ("received", "pending")
                checkbox = None
                if allow_select:
                    gid = g.get("id")
                    def _toggle(e, _id=gid):
                        if e.control.value:
                            self._guest_selected_ids.add(_id)
                        else:
                            self._guest_selected_ids.discard(_id)
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

        return ft.Stack([main_col, fab], expand=True)

    def _guest_stats_view(self) -> ft.Control:
        """Tab Thống kê — chỉ huy nắm tình hình tiếp khách của cấp dưới."""
        import datetime
        my_profile = store.get("userProfile", store.seed_user_profile)
        my_uid = str(my_profile.get("id") or AUTH_STATE.get("localId") or "")
        my_unit = my_profile.get("unitId") or ""
        is_super = bool(my_profile.get("isAdmin"))

        soldiers_all = store.get("soldiers", store.seed_soldiers)
        units_data = store.get("units", store.seed_units)
        all_guests = list(store.get("guests", []) or [])

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

        is_regiment_staff = my_unit in ("u-bch", "u-tm", "u-ct", "u-hk") or my_admin >= 3 or self._is_top_commander(my_uid)
        if is_super or is_regiment_staff:
            scope_unit_ids = set(parent_map.keys())
        else:
            scope_unit_ids = descendants_of(my_unit) if my_unit else set()
        scope_soldiers = [s for s in soldiers_all
                          if not s.get("isAdmin") and (s.get("unitId") in scope_unit_ids)]
        scope_soldier_ids = {str(s.get("id")) for s in scope_soldiers}

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

        unique_hosts = {str(g.get("soldierId")) for g in today_guests}
        total_guests_count = sum(int(g.get("guestCount") or 1) for g in today_guests)
        total_visits = len(today_guests)
        total_motorbike = sum(int(g.get("vehicleCount") or 1) for g in today_guests if (g.get("vehicle") or "") == "motorbike")
        total_car = sum(int(g.get("vehicleCount") or 1) for g in today_guests if (g.get("vehicle") or "") == "car")
        total_walk = sum(1 for g in today_guests if (g.get("vehicle") or "walk") == "walk")

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
                rank = s.get("rank") or ""; nm = s.get("name") or ""
                rank_str = rank.strip()
                rank_prefix = f"{rank_str} " if rank_str else ""
                
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
                        ft.Text(f"  đ.c {rank_prefix}{nm}",
                                size=12, weight=ft.FontWeight.W_600),
                        *rows_for_soldier,
                    ], spacing=2),
                    padding=ft.padding.symmetric(vertical=4),
                ))

        body = ft.ListView(
            controls=[
                ft.Container(
                    content=ft.Column([
                        ft.Text(f"📊 Hôm nay — {today.strftime('%d/%m/%Y')}",
                                size=14, weight=ft.FontWeight.BOLD),
                        stat_row1, stat_row2,
                    ], spacing=8),
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
                    my_rank = (my_profile.get("rank") or "").strip()
                    my_rank_prefix = f"{my_rank} " if my_rank else ""
                    push_name = f"đ.c {my_rank_prefix}{my_name}"
                    store.push_notif(
                        "guest",
                        f"Yêu cầu tiếp khách cần duyệt ({g.get('guestName','')})",
                        f"{push_name} trình lên — vui lòng xem & quyết định.",
                        link=f"guest:{g.get('id')}",
                        target_uid=nxt_uid,
                    )
                except Exception:
                    pass
                moved += 1
            store.set_value("guests", all_guests)
            self._guest_selected_ids.clear()
            page.dialog.open = False
            self.toast(f"✅ Đã trình {moved} yêu cầu lên {nxt.get('name')}")
            if hasattr(self, "_rebuild_guests_module"):
                self._rebuild_guests_module()
            else:
                self.refresh()

        page.dialog = ft.AlertDialog(
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
                    on_click=lambda e: setattr(page.dialog, "open", False) or page.update(),
                ),
                ft.ElevatedButton(
                    "📤 Trình",
                    on_click=submit_bulk,
                    bgcolor=GREEN_DARK, color=ft.Colors.WHITE,
                ),
            ],
        )
        page.dialog.open = True
        page.update()

    def open_guest_registration_dialog(self) -> None:
        try:
            page = self.page
            my_profile = store.get("userProfile", store.seed_user_profile)
            my_uid = my_profile.get("id") or AUTH_STATE.get("localId")

            soldiers = store.get("soldiers", store.seed_soldiers)
            units_data = store.get("units", store.seed_units)
            my_unit = my_profile.get("unitId") or ""
            my_role = my_profile.get("role") or ""
            my_priority = store.role_priority(my_role)

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

            CMD_KEYS = ("trưởng", "chủ nhiệm", "chính uỷ", "chính ủy")
            EXCLUDE_KEYS = ("trợ lý", "nhân viên", "chiến sĩ", "thủ kho")

            same_unit_supers = []
            for s in soldiers:
                if str(s.get("id")) == str(my_uid) or s.get("isAdmin"):
                    continue
                s_uid = s.get("unitId") or ""
                if s_uid not in my_ancestors and s_uid != my_unit:
                    continue
                role = (s.get("role") or "").lower()
                if not any(k in role for k in CMD_KEYS):
                    continue
                if any(k in role for k in EXCLUDE_KEYS):
                    continue
                pri = store.role_priority(s.get("role") or "")
                if pri >= my_priority:
                    continue
                same_unit_supers.append((unit_name_map.get(s_uid, ""), s))

            higher = self._list_higher_commanders(str(my_uid))

            commanders: list[tuple] = []
            seen = set()
            for u_lbl, s in same_unit_supers + higher:
                sid = str(s.get("id"))
                if sid in seen:
                    continue
                seen.add(sid)
                commanders.append((u_lbl, s))

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
                        )
                    except Exception:
                        pass
                    self.toast("✅ Đã trình chỉ huy duyệt")
                    store.refresh_guests()
                    page.dialog.open = False
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

            page.dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text("Đăng ký tiếp khách", size=16, weight=ft.FontWeight.BOLD),
                content=ft.Container(content=dlg_content, width=350),
                actions=[
                    ft.TextButton("Hủy", on_click=lambda e: setattr(page.dialog, "open", False) or page.update()),
                    ft.ElevatedButton("Đăng ký", on_click=do_save, bgcolor=GREEN_MID, color=ft.Colors.WHITE)
                ],
            )
            page.dialog.open = True
            page.update()
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
            page.dialog.open = False
            if hasattr(self, "_rebuild_guests_module"):
                self._rebuild_guests_module()
            else:
                self.refresh()

        def _persist(updates: dict):
            try:
                FS.set_doc(f"guests/{g.get('id')}", updates)
            except Exception:
                pass
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
                                         target_uid=str(target))
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
                                         target_uid=str(target))
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
            ft.Text(f"Quan hệ: {g.get('relationship')} của đồng chí {soldier_name}", size=13),
            ft.Text(f"Đơn vị: {soldier_unit}", size=13, color=TEXT_MUTED),
            ft.Text(f"Số lượng khách: {g.get('guestCount', 1)} người", size=13,
                    weight=ft.FontWeight.BOLD, color=RED),
            ft.Text(f"Phương tiện: {vehicle_label}", size=13),
            ft.Text(f"Dự kiến đến: {arr_str}", size=13),
            ft.Text(f"Dự kiến về: {dep_str}", size=13),
            ft.Text(f"Ghi chú: {g.get('notes') or 'Không'}", size=13, italic=True),
        ] + members_ui + chain_ui

        info_col = ft.ListView(info_col_items, spacing=5, height=400)

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
                                 target_uid=str(g.get("soldierId") or ""))
            except Exception:
                pass
            self.toast("✅ Đã nhận yêu cầu")
            _close()

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
                    )
                except Exception:
                    pass
                self.toast(f"✅ Đã trình lên {nxt.get('name')}")
                _close()

            page.dialog = ft.AlertDialog(
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
            page.dialog.open = True
            page.update()

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
                                     target_uid=str(g.get("soldierId") or ""))
                except Exception:
                    pass
                self.toast("✏️ Đã gửi lại để kiểm tra")
                _close()

            page.dialog = ft.AlertDialog(
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
            page.dialog.open = True
            page.update()

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
                                     target_uid=str(g.get("soldierId") or ""))
                except Exception:
                    pass
                self.toast("❌ Đã từ chối")
                _close()

            page.dialog = ft.AlertDialog(
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
            page.dialog.open = True
            page.update()

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
                                 target_uid=str(g.get("soldierId") or ""))
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
                                 target_uid=str(g.get("soldierId") or ""))
            except Exception:
                pass
            self.toast("🔔 Đã gửi nhắc nhở đến cấp dưới!")
            _close()

        def do_set_status(new_st):
            _persist({"status": new_st})
            self.toast("✅ Đã cập nhật")
            _close()

        actions: list[ft.Control] = []
        if is_current_approver and st in ("pending", "received", "forwarded"):
            if st == "pending":
                actions.append(ft.ElevatedButton("✅ Đã nhận", on_click=do_received,
                                                 bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE))
            actions.append(ft.ElevatedButton("📤 Trình cấp trên", on_click=do_forward,
                                             bgcolor=GREEN_DARK, color=ft.Colors.WHITE))
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
                                     on_click=lambda e: setattr(page.dialog, "open", False) or page.update()))

        page.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Chi tiết khách thăm", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Container(content=info_col, width=340),
            actions=actions,
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.dialog.open = True
        page.update()

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
                    def ok():
                        try:
                            page.dialog.open = False
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

        page.dialog = ft.AlertDialog(
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
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
                ft.ElevatedButton("Đổi mật khẩu", on_click=do_change,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

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
                page.dialog.open = False
            except Exception:
                pass
            self.toast("✅ Đã lưu cài đặt thông báo")
            page.update()

        page.dialog = ft.AlertDialog(
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
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
                ft.ElevatedButton("Lưu", on_click=save,
                                  bgcolor=GREEN_MID, color=ft.Colors.WHITE),
            ],
        )
        page.dialog.open = True
        page.update()

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

        page.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("⚙️ Cài đặt ứng dụng", size=15, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [
                        *(ft.Text(line, size=12) for line in info_lines),
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
                    on_click=lambda e: setattr(page.dialog, "open", False)
                                       or page.update(),
                ),
            ],
        )
        page.dialog.open = True
        page.update()

    def confirm_logout(self) -> None:
        def do_logout(e):
            self.page.dialog.open = False
            self.stop_realtime_sync()
            _clear_auth()
            show_login(self.page)

        self.page.dialog = ft.AlertDialog(
            title=ft.Text("Đăng xuất"),
            content=ft.Text("Đăng xuất khỏi Quản lý LL47 e141?"),
            actions=[
                ft.TextButton(
                    "Huỷ",
                    on_click=lambda e: setattr(self.page.dialog, "open", False)
                                       or self.page.update(),
                ),
                ft.TextButton("Đăng xuất", on_click=do_logout),
            ],
        )
        self.page.dialog.open = True
        self.page.update()

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
                     "ctdctct": GREEN_DARK,
                     "success": GREEN_MID}.get(n["type"], "#999")
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
                             ft.Text(time_ago(n["at"]), size=10, color=TEXT_MUTED)],
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

        # Fetch data
        try:
            exams = FS.list_collection("exams") or []
        except Exception:
            exams = []
        exams.sort(key=lambda x: x.get("createdAt", 0), reverse=True)

        try:
            attempts = FS.list_collection("exam_attempts") or []
        except Exception:
            attempts = []

        unit_names = self._unit_name_map(store.get("units", store.seed_units))

        # Rebuild helper
        def _rebuild():
            self.body.content = self.module_exams()
            self.refresh()

        # Tab Selection handler
        def set_exams_tab(tab_key: str):
            self.exams_view = tab_key
            _rebuild()

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
                        ], spacing=10)
                        
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
                                        page.dialog.open = False
                                        _rebuild()
                                    except Exception as err:
                                        self.toast(f"Lỗi: {err}")
                                
                                page.dialog = ft.AlertDialog(
                                    title=ft.Text("Xác nhận xoá"),
                                    content=ft.Text("Đồng chí có chắc chắn muốn xoá cuộc thi này vĩnh viễn không?"),
                                    actions=[
                                        ft.TextButton("Huỷ", on_click=lambda _: setattr(page.dialog, "open", False) or page.update()),
                                        ft.ElevatedButton("Xoá", on_click=confirm_del, bgcolor=ft.Colors.RED, color=ft.Colors.WHITE)
                                    ]
                                )
                                page.dialog.open = True
                                page.update()
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
                            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
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
                page.dialog.open = False
                
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

        page.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Tạo cuộc thi nhận thức mới", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Container(content=dlg_content, width=380),
            actions=[
                ft.TextButton("Hủy", on_click=lambda e: setattr(page.dialog, "open", False) or page.update()),
                ft.ElevatedButton("Lưu cuộc thi", on_click=save_exam, bgcolor=GREEN_MID, color=ft.Colors.WHITE)
            ],
        )
        page.dialog.open = True
        page.update()

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
                page.dialog.open = False
                page.update()
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

            page.dialog = ft.AlertDialog(
                title=ft.Text("Kết quả thi nhận thức"),
                content=ft.Container(content=res_content, padding=10),
                actions=[
                    ft.ElevatedButton("Xác nhận", on_click=lambda _: setattr(page.dialog, "open", False) or page.update(), bgcolor=GREEN_MID, color=ft.Colors.WHITE)
                ]
            )
            page.dialog.open = True
            page.update()

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
            page.dialog.title = ft.Text("Bài thi Nhận thức đang diễn ra", size=15, weight=ft.FontWeight.BOLD)
            page.dialog.content = ft.Container(content=scroll_sheet, width=380)
            page.dialog.actions = [
                ft.ElevatedButton("Nộp bài thi", on_click=lambda _: do_submit(), bgcolor=GREEN_MID, color=ft.Colors.WHITE)
            ]
            page.update()

            # Start timer background thread
            threading.Thread(target=timer_loop, daemon=True).start()

        page.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Bắt đầu bài thi", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Container(content=info_col, width=350),
            actions=[
                ft.TextButton("Hủy", on_click=lambda e: setattr(page.dialog, "open", False) or page.update()),
                ft.ElevatedButton("Bắt đầu làm bài", on_click=start_exam_session, bgcolor=GREEN_MID, color=ft.Colors.WHITE)
            ],
        )
        page.dialog.open = True
        page.update()

    def mount(self) -> None:
        self.frame = ft.Column([self.header_for_tab(), self.body, self.bottom_nav()],
                               spacing=0, expand=True)
        self.page.add(self.frame)
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
    page.dialog = None

    mode = {"value": "login"}
    units_tree = store.get("units", store.seed_units)
    unit_options = store.flatten_units_for_select(units_tree)

    user_input = ft.TextField(label="Số quân (vd: e141009)", value="",
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
        label="Chức danh *",
        options=[ft.dropdown.Option(t) for t in store.TITLES],
        value="Chiến sĩ",
        border_radius=10, dense=True, visible=False,
    )
    rank_dd = ft.Dropdown(
        label="Cấp bậc *",
        options=[ft.dropdown.Option(r) for r in store.RANKS],
        value="Binh nhì",
        border_radius=10, dense=True, visible=False,
    )
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
            ft.Text("Số quân được cấp:", size=11, color=TEXT_MUTED),
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
            for c in (name_input, rank_dd, title_dd, unit_dd, gen_box):
                c.visible = True
            if not gen_username.value:
                gen_username.value = random_username()
        else:
            mode["value"] = "login"
            submit_btn.text = "🔐 Đăng nhập"
            toggle_link.text = "Chưa có tài khoản? Đăng ký"
            user_input.visible = True
            for c in (name_input, rank_dd, title_dd, unit_dd, gen_box):
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
                try:
                    creds = firebase_auth.login_with_username(u, p)
                    _set_auth(creds, username=u)
                    try:
                        (store.DATA_DIR / "remember_user.txt").write_text(u)
                    except Exception:
                        pass
                    # Lưu (hoặc xoá) password tuỳ checkbox
                    if remember_pw_cb.value:
                        _remember_login_save(u, p)
                    else:
                        _remember_login_clear()
                    try:
                        store.STORE.purge_keys(["soldiers", "units", "userProfile", "chat_rooms"])
                        store.STORE.sync_from_firestore()
                        store.refresh_soldiers_from_users()
                    except Exception:
                        pass
                    # Load profile RIÊNG của user này từ users/{uid}
                    try:
                        my_profile = FS.get_doc(f"users/{creds['localId']}")
                        if my_profile:
                            store.STORE.set_local("userProfile", my_profile)
                    except Exception:
                        pass
                    try:
                        FS.set_doc(f"users/{creds['localId']}", {
                            "username": u, "email": creds.get("email", ""),
                            "lastLoginAt": store.now_ms(),
                        })
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
            threading.Thread(target=login_worker, daemon=True).start()
            return

        # SIGNUP
        name = (name_input.value or "").strip()
        unit_id = unit_dd.value or ""
        title = title_dd.value or ""
        rank = rank_dd.value or ""
        username = (gen_username.value or "").strip()
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
                                            "Không tìm được số quân chưa dùng.")

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
                    "isAdmin": is_admin_init,
                    "adminLevel": 5 if is_admin_init else 1,
                    "accountStatus": account_status,
                }
                try:
                    FS.set_doc(f"users/{creds['localId']}", {
                        **profile, "lastLoginAt": store.now_ms(),
                        "signupAt": store.now_ms(),
                    })
                except Exception:
                    pass

                store.STORE.set_local("userProfile", profile)
                if remember_pw_cb.value:
                    _remember_login_save(current_username, p)

                try:
                    soldiers = store.get("soldiers", store.seed_soldiers)
                    account_status = "active" if is_admin_init else "pending"
                    soldiers.append({
                        "id": creds["localId"], "unitId": unit_id,
                        "name": name, "rank": rank, "role": title,
                        "username": current_username, "phone": "",
                        "accountStatus": account_status, "isAdmin": is_admin_init,
                        "adminLevel": 5 if is_admin_init else 1,
                    })
                    store.set_value("soldiers", soldiers)
                    if not is_admin_init:
                        uid = creds.get("localId") or ""
                        store.push_notif("unit", "Tài khoản mới cần duyệt", f"Tài khoản {name} ({current_username}) vừa đăng ký và đang chờ duyệt.", link=f"profile:{uid}", target_uid="")

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
            err_text.value = "⚠️ Nhập số quân trước khi yêu cầu reset"
            page.update(); return
        try:
            firebase_auth.send_password_reset_email(firebase_config.username_to_email(u))
            err_text.value = ""
            page.snack_bar = ft.SnackBar(
                ft.Text(f"✉️ Đã gửi link reset đến {firebase_config.username_to_email(u)}"),
                bgcolor=GREEN_DARK,
            )
            page.snack_bar.open = True
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
                logo_src = f"assets/{fn}"
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
                name_input, rank_dd, title_dd, unit_dd, gen_box,
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
        ft.Container(
            content=login_card,
            alignment=ft.alignment.center, expand=True,
            gradient=ft.LinearGradient(begin=ft.alignment.top_left,
                                       end=ft.alignment.bottom_right,
                                       colors=["#0d2818", GREEN_DARK, GREEN_MID]),
            padding=20,
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
            store.STORE.sync_from_firestore()
            store.refresh_soldiers_from_users()
            new_prof = FS.get_doc(f"users/{AUTH_STATE.get('localId')}")
            if new_prof:
                store.STORE.set_local("userProfile", new_prof)
        except Exception:
            pass
        show_app(page)
        
    page.add(
        ft.Container(
            content=ft.Column([
                ft.Icon(ft.Icons.HOURGLASS_EMPTY, size=64, color=AMBER),
                ft.Text("Tài khoản đang chờ duyệt", size=20, weight=ft.FontWeight.BOLD),
                ft.Text("Vui lòng đợi quản trị viên phê duyệt tài khoản của bạn để truy cập ứng dụng.", text_align=ft.TextAlign.CENTER, color=TEXT_MUTED),
                ft.Container(height=20),
                ft.ElevatedButton("🔄 Làm mới", on_click=do_refresh, width=200, bgcolor=GREEN_MID, color=ft.Colors.WHITE),
                ft.TextButton("Đăng xuất", on_click=do_logout),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER),
            expand=True, alignment=ft.alignment.center, padding=30
        )
    )

def show_locked(page: ft.Page) -> None:
    page.controls.clear()
    page.bgcolor = BG
    def do_logout(e):
        _clear_auth()
        show_login(page)
    page.add(
        ft.Container(
            content=ft.Column([
                ft.Icon(ft.Icons.LOCK_OUTLINE, size=64, color=RED),
                ft.Text("Tài khoản bị khoá", size=20, weight=ft.FontWeight.BOLD, color=RED),
                ft.Text("Tài khoản của bạn đã bị khoá. Vui lòng liên hệ quản trị viên.", text_align=ft.TextAlign.CENTER, color=TEXT_MUTED),
                ft.Container(height=20),
                ft.TextButton("Đăng xuất", on_click=do_logout),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER),
            expand=True, alignment=ft.alignment.center, padding=30
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
    except:
        pass
    page.controls.clear()
    page.bgcolor = BG
    
    prof = store.get("userProfile", store.seed_user_profile)
    status = str(prof.get("accountStatus") or "active")
    
    if status == "pending":
        show_pending(page)
        return
    elif status == "locked":
        show_locked(page)
        return

    app = App(page)
    app.mount()


def _try_auto_login(page: ft.Page) -> bool:
    creds = _TOKEN_CACHE.load()
    if not creds or not creds.get("refreshToken"):
        return False
    try:
        new = firebase_auth.refresh_id_token(creds["refreshToken"])
        new["email"] = creds.get("email", "")
        _set_auth(new, username=creds.get("username"))
        try:
            store.STORE.purge_keys(["soldiers", "units", "userProfile", "chat_rooms"])
            store.STORE.sync_from_firestore()
            store.refresh_soldiers_from_users()
        except Exception:
            pass
        try:
            my_profile = FS.get_doc(f"users/{new['localId']}")
            if my_profile:
                store.STORE.set_local("userProfile", my_profile)
        except Exception:
            pass
        show_app(page)
        return True
    except Exception:
        _TOKEN_CACHE.clear()
        return False


def main(page: ft.Page) -> None:
    page.title = "Quản lý LL47 e141"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 0
    page.spacing = 0
    try:
        page.window.width = 420
        page.window.height = 800
        page.window.min_width = 360
        page.window.min_height = 640
    except Exception:
        pass
    page.fonts = {}
    page.theme = ft.Theme(
        color_scheme_seed=GREEN_MID,
        font_family="Roboto",
        use_material3=True,
    )
    if not _try_auto_login(page):
        show_login(page)


if __name__ == "__main__":
    ft.app(target=main)
