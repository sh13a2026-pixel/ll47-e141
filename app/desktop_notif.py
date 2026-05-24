"""
LL47 e141 — Desktop Notification (Windows toast + cross-platform fallback).

Hiện thông báo hệ thống khi app đang chạy nhưng cửa sổ bị thu nhỏ / mất focus.

Ưu tiên:
  1. winotify  — Windows 10/11 native toast (đẹp nhất, có icon, click mở app)
  2. plyer     — cross-platform (Windows/macOS/Linux)
  3. Bỏ qua   — nếu không có thư viện nào (Android/iOS không cần)

API công khai:
    notify(title, message, app_name="LL47 e141", icon_path=None)
    notify_chat(sender_name, message, room_name=None, icon_path=None)
    notify_system(title, message, icon_path=None)
    is_supported() -> bool
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

# Phát hiện nền tảng
_IS_ANDROID = (
    os.environ.get("ANDROID_ROOT") is not None
    or os.environ.get("ANDROID_DATA") is not None
)
_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux") and not _IS_ANDROID

# Cache trạng thái thư viện
_backend: str | None = None   # "winotify" | "plyer" | "none"
_winotify_toast_cls = None


def _detect_backend() -> str:
    global _backend, _winotify_toast_cls
    if _backend is not None:
        return _backend

    if _IS_ANDROID:
        _backend = "none"
        return _backend

    if _IS_WINDOWS:
        try:
            from winotify import Notification as _WN
            _winotify_toast_cls = _WN
            _backend = "winotify"
            return _backend
        except ImportError:
            pass

    try:
        import plyer  # noqa: F401
        _backend = "plyer"
        return _backend
    except ImportError:
        pass

    _backend = "none"
    return _backend


def is_supported() -> bool:
    return _detect_backend() != "none"


def _default_icon() -> str | None:
    """Tìm icon app để hiện trong toast."""
    candidates = [
        Path(__file__).parent.parent / "assets" / "logo.ico",
        Path(__file__).parent.parent / "assets" / "logo.png",
        Path(__file__).parent.parent / "assets" / "icon.png",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def _send_winotify(title: str, message: str, app_name: str, icon_path: str | None) -> None:
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id=app_name,
            title=title,
            msg=message[:200],
            icon=icon_path or "",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass


def _send_plyer(title: str, message: str, app_name: str, icon_path: str | None) -> None:
    try:
        from plyer import notification as _pn
        _pn.notify(
            title=title,
            message=message[:200],
            app_name=app_name,
            app_icon=icon_path or "",
            timeout=6,
        )
    except Exception:
        pass


def notify(
    title: str,
    message: str,
    app_name: str = "LL47 e141",
    icon_path: str | None = None,
) -> None:
    """Gửi desktop notification trong background thread (không block UI)."""
    backend = _detect_backend()
    if backend == "none":
        return

    icon = icon_path or _default_icon()

    def _run():
        if backend == "winotify":
            _send_winotify(title, message, app_name, icon)
        elif backend == "plyer":
            _send_plyer(title, message, app_name, icon)

    threading.Thread(target=_run, daemon=True).start()


def notify_chat(
    sender_name: str,
    message: str,
    room_name: str | None = None,
    icon_path: str | None = None,
) -> None:
    """Thông báo tin nhắn mới."""
    if room_name and room_name != sender_name:
        title = f"💬 {sender_name} ({room_name})"
    else:
        title = f"💬 {sender_name}"

    # Rút gọn nội dung
    body = message if len(message) <= 80 else message[:77] + "..."
    notify(title, body, icon_path=icon_path)


def notify_system(
    title: str,
    message: str,
    icon_path: str | None = None,
) -> None:
    """Thông báo hệ thống (F47, duyệt tài khoản, v.v.)."""
    notify(f"🔔 {title}", message, icon_path=icon_path)
