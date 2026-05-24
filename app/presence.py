"""
LL47 e141 — Presence Manager (chấm xanh online + lastSeen).

Dùng Socket.io để:
  - Gửi user_online sau khi login
  - Nhận presence_list (danh sách uid đang online khi mới kết nối)
  - Nhận presence_update (uid vừa online/offline + lastSeen timestamp ms)
  - Gửi user_offline khi logout / app đóng

API công khai:
    start(uid)                      — gọi sau login
    stop()                          — gọi khi logout
    is_online(uid) -> bool          — kiểm tra uid có online không
    get_online_uids() -> set        — tập uid đang online
    get_last_seen(uid) -> int|None  — timestamp ms lần cuối online (None nếu chưa biết)
    last_seen_label(uid) -> str     — "Đang online" / "X phút trước" / "Hôm nay HH:MM" / ...
    on_change(callback)             — đăng ký callback(uid, online: bool) khi có thay đổi
    off_change(callback)            — huỷ đăng ký
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable, Set

from . import firebase_config as fc

# ── Trạng thái toàn cục ──────────────────────────────────────────────────────
_online_uids: Set[str] = set()
_last_seen: dict[str, int] = {}   # uid -> timestamp ms
_lock = threading.Lock()
_callbacks: list[Callable[[str, bool], None]] = []
_sio = None
_my_uid: str | None = None
_running = False


def _notify(uid: str, online: bool) -> None:
    for cb in list(_callbacks):
        try:
            cb(uid, online)
        except Exception:
            pass


def is_online(uid: str) -> bool:
    with _lock:
        return uid in _online_uids


def get_online_uids() -> set:
    with _lock:
        return set(_online_uids)


def get_last_seen(uid: str) -> int | None:
    with _lock:
        return _last_seen.get(uid)


def last_seen_label(uid: str) -> str:
    """Trả chuỗi hiển thị trạng thái online."""
    with _lock:
        online = uid in _online_uids
        ts = _last_seen.get(uid)

    if online:
        return "Đang online"
    if not ts:
        return ""

    now_ms = int(time.time() * 1000)
    diff_ms = now_ms - ts
    diff_s = diff_ms // 1000

    if diff_s < 60:
        return "Vừa xong"
    if diff_s < 3600:
        mins = diff_s // 60
        return f"{mins} phút trước"
    if diff_s < 86400:
        hours = diff_s // 3600
        return f"{hours} giờ trước"

    dt = datetime.fromtimestamp(ts / 1000)
    today = datetime.now()
    if dt.date() == today.date():
        return f"Hôm nay {dt.hour:02d}:{dt.minute:02d}"
    if (today.date() - dt.date()).days == 1:
        return f"Hôm qua {dt.hour:02d}:{dt.minute:02d}"
    return f"{dt.day:02d}/{dt.month:02d}"


def on_change(callback: Callable[[str, bool], None]) -> None:
    if callback not in _callbacks:
        _callbacks.append(callback)


def off_change(callback: Callable[[str, bool], None]) -> None:
    try:
        _callbacks.remove(callback)
    except ValueError:
        pass


def start(uid: str) -> None:
    """Kết nối Socket.io và đăng ký presence. Gọi sau khi login thành công."""
    global _sio, _my_uid, _running
    if _running:
        if _my_uid != uid:
            _my_uid = uid
            try:
                _sio.emit("user_online", {"uid": uid})
            except Exception:
                pass
        return

    _my_uid = uid
    _running = True

    def _connect_loop():
        global _sio, _running
        try:
            import socketio as _sio_lib
        except ImportError:
            _running = False
            return

        sio = _sio_lib.Client(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=3,
            logger=False,
            engineio_logger=False,
        )
        _sio = sio

        @sio.event
        def connect():
            try:
                sio.emit("user_online", {"uid": _my_uid})
            except Exception:
                pass

        @sio.on("presence_list")
        def on_presence_list(data):
            if not isinstance(data, dict):
                return
            presences = data.get("presences") or []
            uids = data.get("uids") or []
            with _lock:
                _online_uids.clear()
                if presences:
                    for p in presences:
                        u = str(p.get("uid", ""))
                        if u:
                            _online_uids.add(u)
                            ls = p.get("lastSeen")
                            if ls:
                                _last_seen[u] = int(ls)
                else:
                    _online_uids.update(str(u) for u in uids)
            for u in _online_uids:
                _notify(str(u), True)

        @sio.on("presence_update")
        def on_presence_update(data):
            if not isinstance(data, dict):
                return
            uid_upd = str(data.get("uid", ""))
            online = bool(data.get("online", False))
            ls = data.get("lastSeen")
            if not uid_upd:
                return
            with _lock:
                if online:
                    _online_uids.add(uid_upd)
                else:
                    _online_uids.discard(uid_upd)
                if ls:
                    _last_seen[uid_upd] = int(ls)
            _notify(uid_upd, online)

        @sio.event
        def disconnect():
            pass

        try:
            sio.connect(
                fc.API_BASE_URL,
                transports=["websocket", "polling"],
                wait_timeout=5,
            )
            sio.wait()
        except Exception:
            pass
        finally:
            _running = False

    t = threading.Thread(target=_connect_loop, daemon=True, name="presence-sio")
    t.start()


def stop() -> None:
    """Gửi user_offline và ngắt kết nối. Gọi khi logout."""
    global _sio, _my_uid, _running
    _running = False
    uid = _my_uid
    _my_uid = None
    with _lock:
        _online_uids.clear()
    sio = _sio
    _sio = None
    if sio is not None:
        try:
            if uid:
                sio.emit("user_offline", {"uid": uid})
        except Exception:
            pass
        try:
            sio.disconnect()
        except Exception:
            pass
