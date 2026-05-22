"""
LL47 e141 — Doc-store client (gọi backend Node.js, thay Firestore REST).

GIỮ NGUYÊN class FirestoreClient + chữ ký method để main.py / store.py không
phải đổi:
    get_doc / set_doc / delete_doc / list_collection / query / listen_collection
    + set_token

Khác biệt nội bộ: thay vì gọi Firestore REST, gọi các endpoint của backend:
    GET    /doc/<path>            -> get_doc
    PATCH  /doc/<path>           -> set_doc  (body {data, merge})
    DELETE /doc/<path>           -> delete_doc
    GET    /collection/<path>    -> list_collection
    POST   /query/<collection>   -> query

listen_collection: ưu tiên Socket.io realtime; nếu không có thư viện
python-socketio hoặc kết nối lỗi -> tự fallback sang polling như trước.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import firebase_config as fc


class FirestoreError(Exception):
    pass


def _quote(path: str) -> str:
    """Giữ nguyên dấu '/' giữa các segment, encode phần còn lại."""
    return urllib.parse.quote(path, safe="/")


class FirestoreClient:
    """Client gọi backend doc-store, authenticated bằng idToken (Bearer)."""

    def __init__(self, id_token: str | None = None):
        self.id_token = id_token

    def set_token(self, id_token: str) -> None:
        self.id_token = id_token

    # ---- HTTP helpers ----
    def _headers(self, json_body: bool = False) -> dict:
        h = {}
        if json_body:
            h["Content-Type"] = "application/json"
        if self.id_token:
            h["Authorization"] = f"Bearer {self.id_token}"
        return h

    def _request(self, method: str, url: str, body: dict | None = None,
                 timeout: float = 15.0) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers=self._headers(json_body=body is not None)
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                txt = resp.read().decode("utf-8")
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # document/collection không tồn tại
            try:
                err = json.loads(e.read().decode("utf-8"))
                msg = err.get("error", {}).get("message", str(e))
            except Exception:
                msg = str(e)
            raise FirestoreError(f"HTTP {e.code}: {msg}") from e
        except urllib.error.URLError as e:
            raise FirestoreError(f"Network error: {e}") from e

    # ---- CRUD ----
    def get_doc(self, path: str) -> dict | None:
        url = f"{fc.DOC_BASE}/{_quote(path)}"
        res = self._request("GET", url)
        if res is None:
            return None
        return res if isinstance(res, dict) else {}

    def set_doc(self, path: str, data: dict, merge: bool = True) -> dict:
        url = f"{fc.DOC_BASE}/{_quote(path)}"
        res = self._request("PATCH", url, {"data": data, "merge": merge})
        return res if isinstance(res, dict) else {}

    def delete_doc(self, path: str) -> None:
        url = f"{fc.DOC_BASE}/{_quote(path)}"
        self._request("DELETE", url)

    def list_collection(self, collection: str, page_size: int = 1000) -> list[dict]:
        url = f"{fc.COLLECTION_BASE}/{_quote(collection)}?pageSize={int(page_size)}"
        res = self._request("GET", url)
        return res if isinstance(res, list) else []

    def query(self, collection: str, where: list[tuple] | None = None,
              order_by: str | None = None, limit: int | None = None) -> list[dict]:
        url = f"{fc.QUERY_BASE}/{_quote(collection)}"
        body: dict[str, Any] = {}
        if where:
            body["where"] = [list(w) for w in where]
        if order_by:
            body["orderBy"] = order_by
        if limit:
            body["limit"] = int(limit)
        res = self._request("POST", url, body)
        return res if isinstance(res, list) else []

    # ---- Realtime ----
    def listen_collection(self, collection: str, callback: Callable[[list[dict]], None],
                          interval: float = 3.0) -> Callable[[], None]:
        """Ưu tiên Socket.io; lỗi/không có lib -> fallback polling."""
        try:
            return self._listen_socketio(collection, callback, interval)
        except Exception:
            return self._listen_polling(collection, callback, interval)

    def _listen_socketio(self, collection: str, callback: Callable[[list[dict]], None],
                         interval: float) -> Callable[[], None]:
        import socketio  # ImportError -> caller fallback sang polling

        sio = socketio.Client(reconnection=True, logger=False, engineio_logger=False)

        def _refresh():
            try:
                callback(self.list_collection(collection))
            except Exception:
                pass

        @sio.event
        def connect():
            try:
                sio.emit("subscribe", {"collection": collection})
            except Exception:
                pass
            _refresh()

        @sio.on("change")
        def _on_change(data):
            try:
                if (data or {}).get("collection") == collection:
                    _refresh()
            except Exception:
                pass

        # Có thể raise -> caller fallback polling
        sio.connect(fc.API_BASE_URL, transports=["websocket", "polling"], wait_timeout=8)

        def stop():
            try:
                sio.emit("unsubscribe", {"collection": collection})
            except Exception:
                pass
            try:
                sio.disconnect()
            except Exception:
                pass

        return stop

    def _listen_polling(self, collection: str, callback: Callable[[list[dict]], None],
                        interval: float) -> Callable[[], None]:
        stop_flag = {"stop": False}
        last_sig = {"sig": ""}

        def loop():
            while not stop_flag["stop"]:
                try:
                    items = self.list_collection(collection)
                    sig = "|".join(f"{i.get('_id')}:{i.get('_updateTime')}" for i in items)
                    if sig != last_sig["sig"]:
                        last_sig["sig"] = sig
                        try:
                            callback(items)
                        except Exception:
                            pass
                except Exception:
                    pass
                t = 0.0
                while t < interval and not stop_flag["stop"]:
                    time.sleep(0.2)
                    t += 0.2

        threading.Thread(target=loop, daemon=True).start()

        def stop():
            stop_flag["stop"] = True

        return stop
