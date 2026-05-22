"""
LL47 e141 — Firebase Cloud Messaging (FCM) helper.

LƯU Ý: Trong Flet app, để LẤY được FCM device token thật bạn cần native plugin
(Flutter firebase_messaging) — Flet 0.25 chưa expose API này thẳng. Tạm thời
module này:
  1. Cho phép app GHI token vào Firestore tại 'users/{uid}/fcm_tokens/{token}'.
  2. Cung cấp helper gửi push qua FCM HTTP v1 (cần service account, dùng phía
     server, KHÔNG dùng trên client).

Hướng tiếp theo: thêm một custom Flet plugin hoặc viết một backend nhỏ
(FastAPI / Cloud Functions) để gửi push thật.
"""
from __future__ import annotations

import time
from typing import Any

from .firestore_client import FirestoreClient


def register_token(client: FirestoreClient, uid: str, token: str,
                   platform: str = "android") -> None:
    """Ghi token thiết bị của user vào Firestore để server biết đường gửi push (chạy background)."""
    if not (uid and token):
        return
    import threading
    def run():
        try:
            path = f"users/{uid}/fcm_tokens/{token}"
            client.set_doc(path, {
                "token": token,
                "platform": platform,
                "registeredAt": int(time.time() * 1000),
            })
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def list_tokens(client: FirestoreClient, uid: str) -> list[str]:
    docs = client.list_collection(f"users/{uid}/fcm_tokens")
    return [d.get("token", "") for d in docs if d.get("token")]


def remove_token(client: FirestoreClient, uid: str, token: str) -> None:
    if not (uid and token):
        return
    client.delete_doc(f"users/{uid}/fcm_tokens/{token}")


def queue_notification(client: FirestoreClient, target_uid: str, title: str,
                       body: str, link: str | None = None,
                       data: dict[str, Any] | None = None) -> None:
    """Tạo document trong 'fcm_queue' ở background để Cloud Function / server gửi push."""
    import threading
    def run():
        try:
            payload = {
                "to": target_uid,
                "title": title,
                "body": body,
                "link": link or "",
                "data": data or {},
                "createdAt": int(time.time() * 1000),
                "sent": False,
            }
            doc_id = f"q_{int(time.time() * 1000)}_{target_uid[:8]}"
            client.set_doc(f"fcm_queue/{doc_id}", payload)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
