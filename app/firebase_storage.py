"""
LL47 e141 — Storage client (gọi backend Node.js + GridFS, thay Firebase Storage).

GIỮ NGUYÊN API công khai để main.py không phải đổi:
    upload_bytes / upload_file / delete_object / make_remote_path / StorageError
"""
from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import firebase_config as fc


class StorageError(Exception):
    pass


def _guess_content_type(filename: str) -> str:
    ct, _ = mimetypes.guess_type(filename)
    return ct or "application/octet-stream"


def upload_bytes(remote_path: str, data: bytes, id_token: str,
                 content_type: str | None = None) -> dict:
    """Upload bytes lên backend (GridFS).

    Trả về dict gồm 'name', 'downloadURL', 'token', 'size', 'contentType'.
    """
    if content_type is None:
        content_type = _guess_content_type(remote_path)
    qs = urllib.parse.quote(remote_path, safe="")
    url = f"{fc.STORAGE_BASE}/upload?path={qs}"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {id_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise StorageError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise StorageError(f"Network error: {e}") from e


from . import image_utils


def _compress_if_image(data: bytes, filename: str) -> tuple[bytes, str]:
    """Nén ảnh nếu cần, trả về (data, content_type) đã xử lý."""
    content_type = _guess_content_type(filename)
    if content_type.startswith("image/"):
        data, did_convert = image_utils.compress_image_bytes(data)
        if did_convert:
            content_type = "image/jpeg"
    return data, content_type


def upload_file(local_path: str | Path, remote_path: str, id_token: str) -> dict:
    p = Path(local_path)
    data, content_type = _compress_if_image(p.read_bytes(), p.name)
    return upload_bytes(remote_path, data, id_token, content_type=content_type)


def upload_data(remote_path: str, raw_bytes: bytes, id_token: str, filename: str) -> dict:
    """Upload từ bytes (dùng cho mobile/web khi không có f.path).
    Tự động nén ảnh giống upload_file — dùng chung 1 nơi để đồng nhất."""
    data, content_type = _compress_if_image(raw_bytes, filename)
    return upload_bytes(remote_path, data, id_token, content_type=content_type)


def delete_object(remote_path: str, id_token: str) -> None:
    encoded = urllib.parse.quote(remote_path, safe="/")
    url = f"{fc.STORAGE_BASE}/file/{encoded}"
    req = urllib.request.Request(
        url, method="DELETE",
        headers={"Authorization": f"Bearer {id_token}"},
    )
    try:
        urllib.request.urlopen(req, timeout=15.0).read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise StorageError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise StorageError(f"Network error: {e}") from e


def make_remote_path(folder: str, original_filename: str) -> str:
    """Sinh remote path không trùng: folder/uuid_filename.ext."""
    safe = original_filename.replace("/", "_").replace("\\", "_")
    return f"{folder.strip('/')}/{uuid.uuid4().hex}_{safe}"
