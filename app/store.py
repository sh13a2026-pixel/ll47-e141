"""
LL47 e141 — Lớp lưu trữ dữ liệu (cache cục bộ + sync Firestore).

API công khai giữ nguyên với phiên bản trước:
    STORE.get(key, factory) -> value
    STORE.set(key, value)
    store.get(key, factory)            # alias module-level
    store.set_value(key, value)        # alias module-level
    store.log_activity(text)
    store.push_notif(...)

Khi chưa bind Firestore (chưa đăng nhập): hoạt động hoàn toàn cục bộ — đọc/ghi
file JSON trong thư mục app data. Phù hợp cho lúc app chạy lần đầu / mất mạng.

Sau khi bind Firestore (sau login):
- get(): trả từ cache. Nếu key chưa có → seed từ default_factory + push lên
  Firestore (chỉ làm 1 lần đầu cho dữ liệu seed).
- set(): ghi cache + push lên Firestore + ghi file local làm cache offline.
- sync_from_firestore(): kéo toàn bộ dữ liệu từ Firestore về cache, gọi sau
  khi login để đồng bộ giữa các thiết bị.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


def _data_dir() -> Path:
    """Trả về thư mục lưu trữ phù hợp với từng nền tảng."""
    p = os.environ.get("FLET_APP_STORAGE_DATA")
    if p:
        return Path(p)
    
    # Phát hiện nền tảng Android
    is_android = os.environ.get("ANDROID_ROOT") is not None or os.environ.get("ANDROID_DATA") is not None
    if is_android:
        try:
            cwd = Path(os.getcwd())
            if "files/flet" in str(cwd).replace("\\", "/"):
                # CWD: /data/user/0/vn.mil.e141.ll47_e141/files/flet/app -> /data/user/0/vn.mil.e141.ll47_e141/files
                return cwd.parent.parent
            return cwd
        except Exception:
            return Path(".")

    try:
        home = Path.home()
    except Exception:
        return Path(".")

    if os.name == "nt":
        return home / "AppData" / "Local" / "LL47e141"
    try:
        if os.uname().sysname == "Darwin":
            return home / "Library" / "Application Support" / "LL47e141"
    except AttributeError:
        pass
    return home / ".ll47e141"


DATA_DIR = _data_dir()
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path(".")
DATA_FILE = DATA_DIR / "ll47_data.json"


# ============================================================================
# Store class
# ============================================================================

class Store:
    """Wrapper đọc/ghi JSON cục bộ + push lên Firestore khi đã đăng nhập."""

    # Path Firestore: mọi key cấp 1 lưu thành document trong collection "app_data".
    # Mỗi document có 1 field "value" chứa dữ liệu (dict / list / scalar) đầy đủ.
    APP_DATA_COLLECTION = "app_data"

    # Các key RIÊNG cho từng user — KHÔNG đẩy lên app_data (chung) và
    # KHÔNG sync xuống từ app_data. Mỗi user load profile riêng từ users/{uid}.
    PRIVATE_KEYS = {"userProfile", "notifPrefs", "hiddenChatRooms"}

    def __init__(self, path: Path = DATA_FILE) -> None:
        self.path = path
        self._cache: dict[str, Any] | None = None
        self._lock = threading.RLock()
        # Firestore binding (lazy, set sau khi login)
        self._fs_client = None  # type: ignore
        self._fs_uid: str | None = None
        # Hàng đợi key cần re-sync khi mạng có lại
        self._pending_writes: set[str] = set()

    # ---- Local file IO ----
    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if self.path.exists():
            try:
                self._cache = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}
        else:
            self._cache = {}
        return self._cache

    def save(self) -> None:
        """Ghi cache xuống file JSON cục bộ."""
        with self._lock:
            self.path.write_text(
                json.dumps(self._cache or {}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # ---- Public API ----
    def get(self, key: str, default_factory: Callable[[], Any] | Any) -> Any:
        with self._lock:
            d = self._load()
            if key not in d:
                value = default_factory() if callable(default_factory) else default_factory
                d[key] = value
                self.save()
                # Lần đầu seed → đẩy lên Firestore (nếu đã bind) để mọi thiết
                # bị khác cũng có dữ liệu seed.
                self._push(key, value)
            return d[key]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._load()[key] = value
            self.save()
            self._push(key, value)

    def reset(self) -> None:
        with self._lock:
            self._cache = {}
            self.save()

    def purge_keys(self, keys: list[str]) -> None:
        """Xóa một số key khỏi cache local (file JSON).

        Mục tiêu: tránh hiển thị dữ liệu "demo/stale" khi đăng nhập xong mà
        Firestore không có document tương ứng cho key đó.
        """
        if not keys:
            return
        with self._lock:
            d = self._load()
            for k in keys:
                d.pop(k, None)
            self.save()

    # ---- Firestore binding ----
    def bind_firestore(self, fs_client, uid: str) -> None:
        """Liên kết với 1 FirestoreClient đã có idToken hợp lệ."""
        self._fs_client = fs_client
        self._fs_uid = uid

    def unbind_firestore(self) -> None:
        self._fs_client = None
        self._fs_uid = None
        self._pending_writes.clear()

    def is_bound(self) -> bool:
        return self._fs_client is not None

    def sync_from_firestore(self) -> int:
        if not self._fs_client:
            return 0
        try:
            # Sync standard keys
            docs = self._fs_client.list_collection(self.APP_DATA_COLLECTION)
        except Exception:
            docs = []
            
        with self._lock:
            d = self._load()
            count = 0
            for doc in docs:
                key = doc.get("_id")
                if not key or key in self.PRIVATE_KEYS:
                    continue
                if "value" in doc:
                    val = doc["value"]
                    # Lọc room-all cũ (đã xoá) khỏi chat_rooms nếu còn sót trong DB
                    if key == "chat_rooms" and isinstance(val, list):
                        val = [r for r in val if r.get("id") != "room-all"]
                    d[key] = val
                    count += 1
            
            # V2 Collections Migration
            v2_collections = {
                "soldiers": "v2_soldiers",
                "reports": "v2_reports",
                "notifs": "v2_notifs",
                "f47Campaigns": "v2_f47Campaigns"
            }
            for key, col_name in v2_collections.items():
                try:
                    col_docs = self._fs_client.list_collection(f"{col_name}/e141")
                    if col_docs:
                        d[key] = col_docs
                        count += len(col_docs)
                except Exception:
                    pass
            
            self.save()
        return count

    def set_local(self, key: str, value: Any) -> None:
        """Lưu chỉ local — KHÔNG push lên Firestore. Dùng cho key cá nhân."""
        with self._lock:
            self._load()[key] = value
            self.save()

    def flush_pending(self) -> int:
        """Thử push lại các key bị fail trước đó (vd: do mất mạng)."""
        if not self._fs_client or not self._pending_writes:
            return 0
        with self._lock:
            d = self._load()
            keys = list(self._pending_writes)
        ok = 0
        for k in keys:
            try:
                self._fs_client.set_doc(
                    f"{self.APP_DATA_COLLECTION}/{k}",
                    {"value": d.get(k)},
                )
                self._pending_writes.discard(k)
                ok += 1
            except Exception:
                pass
        return ok

    # ---- Internal ----
    def _push(self, key: str, value: Any) -> None:
        """Đẩy 1 key lên Firestore FIRE-AND-FORGET (không block UI thread).

        UI gọi STORE.set(...) → trả về NGAY LẬP TỨC. HTTP request đi ngầm
        trong background thread. Nếu lỗi → để vào pending để flush_pending() retry.
        Skip các PRIVATE_KEYS — chúng không thuộc app_data global.
        """
        if not self._fs_client:
            return
        if key in self.PRIVATE_KEYS:
            return
        # Đánh dấu pending ngay (để flush_pending có thể retry nếu cần)
        self._pending_writes.add(key)

        client = self._fs_client
        path = f"{self.APP_DATA_COLLECTION}/{key}"
        # Deep-ish snapshot để tránh race khi caller mutate value sau khi gọi set()
        snapshot = json.loads(json.dumps(value, ensure_ascii=False, default=str))

        def _worker():
            try:
                v2_collections = {
                    "soldiers": "v2_soldiers",
                    "reports": "v2_reports",
                    "notifs": "v2_notifs",
                    "f47Campaigns": "v2_f47Campaigns"
                }
                if key in v2_collections and isinstance(snapshot, list):
                    col_name = v2_collections[key]
                    new_ids = {
                        str(item.get("id") or item.get("_id") or "")
                        for item in snapshot
                    }
                    new_ids.discard("")

                    # Xoá các doc không còn trong snapshot (đã bị delete local)
                    try:
                        existing_docs = client.list_collection(f"{col_name}/e141")
                        for ex in existing_docs:
                            ex_id = str(ex.get("_id") or ex.get("id") or "")
                            if ex_id and ex_id not in new_ids:
                                try:
                                    client.delete_doc(f"{col_name}/e141/{ex_id}")
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    # Upsert các item còn lại
                    for item in snapshot:
                        item_id = str(item.get("id") or item.get("_id") or "")
                        if item_id:
                            client.set_doc(f"{col_name}/e141/{item_id}", item)
                else:
                    client.set_doc(path, {"value": snapshot})
                self._pending_writes.discard(key)
            except Exception:
                pass  # giữ lại trong pending để retry sau

        threading.Thread(target=_worker, daemon=True).start()


STORE = Store()


# ============================================================================
# SEED DATA — dữ liệu khởi tạo cho Firestore khi project lần đầu chạy
# ============================================================================

def seed_units() -> dict:
    """Cây đơn vị Trung đoàn 141 chi tiết (theo thiết kế mới)."""

    def _squad(sid: str, name: str) -> dict:
        return {
            "id": sid, "name": name, "type": "squad", "adminLevel": 0,
            "commanderId": None, "commanderTitle": "Tiểu đội trưởng", "children": []
        }

    def _platoon(pid: str, name: str, with_squads: bool = True) -> dict:
        children = [
            _squad(f"{pid}-a1", "Tiểu đội 1"),
            _squad(f"{pid}-a2", "Tiểu đội 2"),
            _squad(f"{pid}-a3", "Tiểu đội 3"),
        ] if with_squads else []
        return {
            "id": pid, "name": name, "type": "platoon", "adminLevel": 1,
            "commanderId": None, "commanderTitle": "Trung đội trưởng", "children": children
        }

    def _company(cid: str, name: str, with_platoons: bool = True) -> dict:
        # 3 chức danh cơ bản (cấp 2): Phó Đại đội trưởng, Chính trị viên, Chính trị viên phó
        positions = [
            {"name": "Phó Đại đội trưởng", "type": "assistant",
             "commanderTitle": "Phó Đại đội trưởng", "commanderId": None,
             "adminLevel": 2, "id": f"{cid}-pdt", "abbr": "PĐĐT", "children": []},
            {"name": "Chính trị viên Đại đội", "type": "assistant",
             "commanderTitle": "Chính trị viên", "commanderId": None,
             "adminLevel": 2, "id": f"{cid}-ctv", "abbr": "CTV", "children": []},
            {"name": "Chính trị viên phó Đại đội", "type": "assistant",
             "commanderTitle": "Chính trị viên phó", "commanderId": None,
             "adminLevel": 2, "id": f"{cid}-ctvp", "abbr": "CTVP", "children": []},
        ]
        platoons = [
            _platoon(f"{cid}-b1", "Trung đội 1"),
            _platoon(f"{cid}-b2", "Trung đội 2"),
            _platoon(f"{cid}-b3", "Trung đội 3"),
        ] if with_platoons else []
        return {
            "id": cid, "name": name, "type": "company", "adminLevel": 3,
            "commanderId": None, "commanderTitle": "Đại đội trưởng",
            "children": positions + platoons,
        }

    def _aux(aid: str, name: str, role_level: int = 0) -> dict:
        """Trợ lý / Nhân viên."""
        return {
            "id": aid, "name": name, "type": "assistant", "adminLevel": role_level,
            "commanderId": None, "commanderTitle": name.split(' ')[0],
            "children": [],
        }

    def _battalion(bid: str, name: str, c_start: int) -> dict:
        return {
            "id": bid, "name": name, "type": "battalion", "adminLevel": 4,
            "commanderId": None, "commanderTitle": "Tiểu đoàn trưởng",
            "children": [
                {"id": f"{bid}-bch", "name": f"Ban chỉ huy {name}", "type": "command", "adminLevel": 4, "commanderId": None, "commanderTitle": "Tiểu đoàn trưởng", "children": [
                    _aux(f"{bid}-dt", "Tiểu đoàn trưởng", 4),
                    _aux(f"{bid}-ctv", "Chính trị viên Tiểu đoàn", 3),
                    _aux(f"{bid}-pdt", "Phó Tiểu đoàn trưởng", 3),
                    _aux(f"{bid}-ctvp", "Chính trị viên phó Tiểu đoàn", 3),
                ]},
                _aux(f"{bid}-tltm", "Trợ lý Tham mưu", 3),
                _aux(f"{bid}-tlhc", "Trợ lý Hậu cần", 3),
                _aux(f"{bid}-nvqy", "Nhân viên Quân y", 1),
                _aux(f"{bid}-nvqn", "Nhân viên Quân nhu", 1),
                _aux(f"{bid}-nvqk", "Nhân viên Quân khí", 1),
                _company(f"{bid}-c{c_start}", f"Đại đội {c_start}"),
                _company(f"{bid}-c{c_start+1}", f"Đại đội {c_start+1}"),
                _company(f"{bid}-c{c_start+2}", f"Đại đội {c_start+2}"),
                _company(f"{bid}-c{c_start+3}", f"Đại đội {c_start+3}"),
                _platoon(f"{bid}-bTT", "Trung đội Thông tin", False),
                _platoon(f"{bid}-bDKZ", "Trung đội ĐKZ", False),
                _platoon(f"{bid}-b127", "Trung đội 12,7mm", False),
            ]
        }

    return {
        "id": "root",
        "name": "Trung đoàn 141",
        "type": "regiment",
        "adminLevel": 5,
        "commanderId": None,
        "commanderTitle": "Trung đoàn trưởng",
        "children": [
            {"id": "u-bch", "name": "Ban chỉ huy Trung đoàn", "type": "command",
             "adminLevel": 4, "commanderId": None,
             "commanderTitle": "Trung đoàn trưởng", "children": [
                 _aux("u-bch-et", "Trung đoàn trưởng", 4),
                 _aux("u-bch-cuy", "Chính uỷ", 4),
                 _aux("u-bch-pet-tmt", "Phó Trung đoàn trưởng – Tham mưu trưởng", 4),
                 _aux("u-bch-pet", "Phó Trung đoàn trưởng", 4),
                 _aux("u-bch-pcu", "Phó Chính uỷ", 4),
             ]},
            {"id": "u-tm", "name": "Cơ quan Tham mưu", "type": "department", "adminLevel": 4,
             "commanderId": None, "commanderTitle": "Phó Tham mưu trưởng",
             "children": [
                 _aux("u-tm-ptmt",  "Phó Tham mưu trưởng", 4),
                 _aux("u-tm-th",    "Trợ lý Tác huấn",     3),
                 _aux("u-tm-ql",    "Trợ lý Quân lực",     3),
                 _aux("u-tm-cob",   "Trợ lý Công binh",    3),
                 _aux("u-tm-pb",    "Trợ lý Pháo binh",    3),
                 _aux("u-tm-tt",    "Trợ lý Thông tin",    3),
                 _aux("u-tm-pk",    "Trợ lý Phòng không",  3),
                 _aux("u-tm-ts",    "Trợ lý Trinh sát",    3),
                 _aux("u-tm-hh",    "Trợ lý Hoá học",      3),
                 _aux("u-tm-tc",    "Trợ lý Tài chính",    3),
                 _aux("u-tm-hc",    "Trợ lý Hành chính",   3),
                 _aux("u-tm-bm",    "Nhân viên Bảo mật",   1),
                 _aux("u-tm-cy",    "Nhân viên Cơ yếu",    1),
                 _aux("u-tm-nvtc",  "Nhân viên Tài chính", 1),
                 _aux("u-tm-nvql",  "Nhân viên Quản lý",   1),
                 _aux("u-tm-nvql2", "Nhân viên Quân lực",  1),
                 # Sub-units thuộc CQTM
                 {"id": "u-tm-b35", "name": "Bếp 35", "type": "station", "adminLevel": 2,
                  "commanderId": None, "commanderTitle": "Quản lý Bếp 35",
                  "children": [
                      _aux("u-tm-b35-ql", "Quản lý Bếp 35", 2),
                      _aux("u-tm-b35-cs", "Chiến sĩ Bếp 35", 1),
                  ]},
                 {"id": "u-tm-vbtd", "name": "Trung đội Vệ binh", "type": "platoon", "adminLevel": 2,
                  "commanderId": None, "commanderTitle": "Trung đội trưởng",
                  "children": [
                      _aux("u-tm-vbtd-tdt", "Trung đội trưởng Trung đội Vệ binh", 2),
                      _aux("u-tm-vbtd-cs",  "Chiến sĩ Trung đội Vệ binh",         1),
                  ]},
             ]},
            {"id": "u-ct", "name": "Cơ quan Chính Trị", "type": "department", "adminLevel": 4,
             "commanderId": None, "commanderTitle": "Chủ nhiệm Chính trị",
             "children": [
                 _aux("u-ct-cnct", "Chủ nhiệm Chính trị", 4),
                 _aux("u-ct-pcnct", "Phó Chủ nhiệm Chính trị", 4),
                 _aux("u-ct-th", "Trợ lý Tuyên huấn", 4),
                 _aux("u-ct-cb", "Trợ lý Cán bộ", 3),
                 _aux("u-ct-tc", "Trợ lý Tổ chức kiêm thống kê", 3),
                 _aux("u-ct-qc", "Trợ lý Công tác quần chúng", 3),
                 _aux("u-ct-cs", "Trợ lý Chính sách", 3),
                 _aux("u-ct-dv", "Trợ lý Dân vận", 3),
                 _aux("u-ct-ba", "Trợ lý Bảo vệ An ninh", 3),
                 _aux("u-ct-ubkt", "Phó chủ nhiệm Uỷ ban kiểm tra", 3),
                 _aux("u-ct-clb", "Nhân viên Câu lạc bộ", 1),
                 _aux("u-ct-tk", "Nhân viên Thống kê", 1),
                 _aux("u-ct-csclb", "Chiến sĩ CLB", 1),
             ]},
            {"id": "u-hk", "name": "Cơ quan HC-KT", "type": "department",
             "adminLevel": 4, "commanderId": None,
             "commanderTitle": "Chủ nhiệm Hậu cần – Kỹ thuật",
              "children": [
                  _aux("u-hk-cnhckt", "Chủ nhiệm Hậu cần – Kỹ thuật", 4),
                  _aux("u-hk-pcnhckt", "Phó Chủ nhiệm Hậu cần – Kỹ thuật", 4),
                  _aux("u-hk-xm", "Trợ lý Xe máy", 3),
                  _aux("u-hk-qn", "Trợ lý Quân nhu", 3),
                  _aux("u-hk-dt", "Trợ lý Doanh trại", 3),
                  _aux("u-hk-qk", "Trợ lý Quân khí", 3),
                  _aux("u-hk-nvqn", "Nhân viên Quân nhu", 1),
                  {"id": "u-hk-kqn", "name": "Kho quân nhu", "type": "station", "adminLevel": 2,
                   "commanderId": None, "commanderTitle": "Kho trưởng", "children": [
                       _aux("u-hk-kqn-kt", "Kho trưởng", 2),
                       _aux("u-hk-kqn-tk", "Thủ kho", 1),
                   ]},
                  {"id": "u-hk-kqk", "name": "Kho quân khí", "type": "station", "adminLevel": 2,
                   "commanderId": None, "commanderTitle": "Kho trưởng", "children": [
                       _aux("u-hk-kqk-kt", "Kho trưởng", 2),
                       _aux("u-hk-kqk-tk", "Thủ kho", 1),
                   ]},
                  {"id": "u-hk-tcblt", "name": "Trạm chế biến lương thực", "type": "station", "adminLevel": 2,
                   "commanderId": None, "commanderTitle": "Trạm trưởng", "children": [
                       _aux("u-hk-tcblt-tt", "Trạm trưởng", 2),
                       _aux("u-hk-tcblt-nv", "Nhân viên nấu ăn", 1),
                   ]},
              ]},
            # Các đơn vị trực thuộc
            _company("u-c14", "Đại đội 14"),
            _company("u-c15", "Đại đội 15"),
            _company("u-c16", "Đại đội 16"),
            _company("u-c17", "Đại đội 17"),
            _company("u-c18", "Đại đội 18"),
            _company("u-c20", "Đại đội 20"),
            _company("u-c24", "Đại đội 24"),
            _company("u-c25", "Đại đội 25"),

            # Tiểu đoàn
            _battalion("u-d7", "Tiểu đoàn 7", 1),
            _battalion("u-d8", "Tiểu đoàn 8", 5),
            _battalion("u-d9", "Tiểu đoàn 9", 9),
        ],
    }


# Danh sách cấp bậc quân đội
RANKS: list[str] = [
    # Binh sĩ
    "Binh nhì",
    "Binh nhất",
    # Hạ sĩ quan
    "Hạ sĩ",
    "Trung sĩ",
    "Thượng sĩ",
    # Quân nhân chuyên nghiệp (QNCN)
    "Thiếu úy QNCN",
    "Trung úy QNCN",
    "Thượng úy QNCN",
    "Đại úy QNCN",
    "Thiếu tá QNCN",
    # Sĩ quan
    "Thiếu úy",
    "Trung úy",
    "Thượng úy",
    "Đại úy",
    "Thiếu tá",
    "Trung tá",
    "Thượng tá",
    "Đại tá",
]


# Danh sách chức danh có thể chọn khi đăng ký / admin bổ nhiệm
TITLES: list[str] = [
    "Chiến sĩ", "Chiến sĩ CLB",
    "Tiểu đội trưởng",
    "Trung đội trưởng",
    "Phó đại đội trưởng", "Đại đội trưởng",
    "Chính trị viên phó đại đội", "Chính trị viên đại đội",
    "Phó tiểu đoàn trưởng", "Tiểu đoàn trưởng",
    "Chính trị viên phó tiểu đoàn", "Chính trị viên tiểu đoàn",
    "Nhân viên", "Nhân viên Bảo mật", "Nhân viên Cơ yếu", "Nhân viên Tài chính", "Nhân viên Quản lý", "Nhân viên Quân lực", "Nhân viên Câu lạc bộ", "Nhân viên Thống kê", "Nhân viên Quân y", "Nhân viên Quân nhu", "Nhân viên Quân khí",
    "Trợ lý", "Trợ lý Tác huấn", "Trợ lý Quân lực", "Trợ lý Công binh", "Trợ lý Pháo binh", "Trợ lý Thông tin", "Trợ lý Phòng không", "Trợ lý Trinh sát", "Trợ lý Hoá học", "Trợ lý Tài chính", "Trợ lý Hành chính", "Trợ lý Tuyên huấn", "Trợ lý Cán bộ", "Trợ lý Tổ chức kiêm thống kê", "Trợ lý Công tác quần chúng", "Trợ lý Chính sách", "Trợ lý Dân vận", "Trợ lý Bảo vệ An ninh", "Trợ lý Tham mưu", "Trợ lý Hậu cần", "Trợ lý Xe máy", "Trợ lý Quân nhu", "Trợ lý Doanh trại", "Trợ lý Quân khí",
    "Phó chủ nhiệm Uỷ ban kiểm tra",
    "Phó chủ nhiệm Hậu cần - Kỹ thuật", "Chủ nhiệm Hậu cần - Kỹ thuật",
    "Phó chủ nhiệm Chính trị", "Chủ nhiệm Chính trị",
    "Phó Tham mưu trưởng", "Tham mưu trưởng",
    "Phó Chính uỷ", "Chính uỷ",
    "Phó Trung đoàn trưởng", "Trung đoàn trưởng",
    "Kho trưởng", "Thủ kho", "Trạm trưởng", "Nhân viên nấu ăn", "Quản trị hệ thống",
]


# Bảng ánh xạ chức vụ → adminLevel mặc định
# Khi admin gán chức vụ, hệ thống tự gợi ý mức phân quyền phù hợp.
ROLE_ADMIN_LEVEL: dict[str, int] = {
    # Cấp 4 — Ban chỉ huy Trung đoàn & Chủ nhiệm/Phó các cơ quan
    "Trung đoàn trưởng": 4, "Chính uỷ": 4,
    "Phó Trung đoàn trưởng": 4, "Phó Trung đoàn trưởng – Tham mưu trưởng": 4,
    "Phó Chính uỷ": 4, "Tham mưu trưởng": 4, "Phó Tham mưu trưởng": 4,
    "Chủ nhiệm Chính trị": 4, "Phó Chủ nhiệm Chính trị": 4,
    "Chủ nhiệm Hậu cần – Kỹ thuật": 4, "Phó Chủ nhiệm Hậu cần – Kỹ thuật": 4,
    "Trợ lý Tuyên huấn": 4,
    # Cấp 3 — Chỉ huy Tiểu đoàn & Trợ lý Chính trị cấp Trung đoàn
    "Tiểu đoàn trưởng": 3, "Phó Tiểu đoàn trưởng": 3,
    "Chính trị viên tiểu đoàn": 3, "Chính trị viên phó tiểu đoàn": 3,
    "Trợ lý Cán bộ": 3, "Trợ lý Tổ chức kiêm thống kê": 3,
    "Trợ lý Công tác quần chúng": 3, "Trợ lý Chính sách": 3,
    "Trợ lý Dân vận": 3, "Trợ lý Bảo vệ An ninh": 3,
    "Phó chủ nhiệm Uỷ ban kiểm tra": 3,
    # Cấp 2 — Chỉ huy Đại đội
    "Đại đội trưởng": 2, "Phó đại đội trưởng": 2,
    "Chính trị viên đại đội": 2, "Chính trị viên phó đại đội": 2,
    # Cấp 1 — Chỉ huy Trung đội
    "Trung đội trưởng": 1,
    # Cấp 0 — Mặc định (không cần liệt kê, hàm trả 0 nếu không tìm thấy)
}


def get_admin_level_for_role(role: str) -> int:
    """Trả về adminLevel gợi ý cho chức vụ. Mặc định 0 nếu không có trong bảng."""
    return ROLE_ADMIN_LEVEL.get((role or "").strip(), 0)


# Các type thuộc đơn vị tổ chức (KHÔNG phải chức danh cá nhân)
_ORG_UNIT_TYPES = {"regiment", "command", "department", "company", "battalion",
                   "platoon", "squad", "station"}


def role_priority(role: str) -> int:
    """Trả về thứ tự ưu tiên của chức danh trong đơn vị (số càng nhỏ càng cao).

    Theo nguyên tắc:
        0  Trưởng đơn vị (eT / TĐT / ĐĐT) ≡ Chính uỷ ≡ Chính trị viên (cùng cấp)
            ≡ Chủ nhiệm cơ quan / Tham mưu trưởng
        1  Phó (Phó eT / Phó TĐT / Phó ĐĐT) ≡ Phó Chính uỷ ≡ Chính trị viên phó
            ≡ Phó Chủ nhiệm / Phó Tham mưu trưởng
        2  Trợ lý (chuyên ngành trong cơ quan / tiểu đoàn)
        3  Trung đội trưởng
        4  Tiểu đội trưởng
        5  Nhân viên (chuyên môn)
        6  Chiến sĩ
        10 Không xác định
    """
    r = (role or "").strip().lower()
    if not r:
        return 10

    is_pho = ("phó" in r) or ("p." in r) or r.startswith("phó")
    is_cs_phu = "chính trị viên phó" in r or "ctv phó" in r

    # ---- Cấp 0: Trưởng đơn vị + Chính uỷ + Chính trị viên (không phải phó) + Chủ nhiệm + Tham mưu trưởng
    if not is_pho:
        if any(k in r for k in (
            "trung đoàn trưởng", "tiểu đoàn trưởng", "đại đội trưởng",
            "chính uỷ", "chính ủy",
            "chủ nhiệm", "tham mưu trưởng",
        )):
            return 0
        # Chính trị viên (không có "phó")
        if "chính trị viên" in r and not is_cs_phu:
            return 0

    # ---- Cấp 1: Phó các loại
    if is_pho or is_cs_phu:
        if any(k in r for k in (
            "trung đoàn trưởng", "tiểu đoàn trưởng", "đại đội trưởng",
            "chính uỷ", "chính ủy",
            "chủ nhiệm", "tham mưu trưởng",
            "chính trị viên",
        )):
            return 1

    # ---- Cấp 2: Trợ lý / Kho trưởng / Trạm trưởng / Quản lý
    if "trợ lý" in r:
        return 2
    if any(k in r for k in ("kho trưởng", "trạm trưởng", "quản lý bếp", "quản lý")):
        return 2

    # ---- Cấp 3: Trung đội trưởng
    if "trung đội trưởng" in r:
        return 3

    # ---- Cấp 4: Tiểu đội trưởng
    if "tiểu đội trưởng" in r:
        return 4

    # ---- Cấp 5: Nhân viên / Thủ kho
    if "nhân viên" in r or "thủ kho" in r:
        return 5

    # ---- Cấp 6: Chiến sĩ
    if "chiến sĩ" in r:
        return 6

    return 10


# Thứ tự canonical cho các đơn vị cấp 1 (trực thuộc Trung đoàn).
# Bất kỳ unit nào có id ngoài danh sách này sẽ xếp cuối theo thứ tự khai báo.
TOP_LEVEL_ORDER = [
    "u-bch", "u-tm", "u-ct", "u-hk",
    "u-d7", "u-d8", "u-d9",
    "u-c14", "u-c15", "u-c16", "u-c17", "u-c18",
    "u-c20", "u-c24", "u-c25",
]


# Ép tên canonical cho các đơn vị cấp 1 (kể cả khi data còn lưu tên cũ).
# Khoá là id (ổn định) → tên canonical.
_CANON_NAME_BY_ID = {
    "u-bch": "Ban chỉ huy Trung đoàn",
    "u-chy": "Ban chỉ huy Trung đoàn",   # id cũ nhưng bản chất là BCH/e
    "u-tm":  "Cơ quan Tham mưu",
    "u-ct":  "Cơ quan Chính Trị",
    "u-hk":  "Cơ quan HC-KT",
}

# Fallback theo tên ngắn (nếu id không match) — phòng trường hợp tên gốc bị đổi
_CANON_NAME_BY_LOW = {
    "tham mưu":             "Cơ quan Tham mưu",
    "chính trị":            "Cơ quan Chính Trị",
    "hậu cần - kỹ thuật":   "Cơ quan HC-KT",
    "hậu cần – kỹ thuật":   "Cơ quan HC-KT",
    "chỉ huy trung đoàn":   "Ban chỉ huy Trung đoàn",
}


def canonical_unit_name(node: dict) -> str:
    """Trả về tên canonical của 1 node (đảm bảo CQ/BCH có prefix đầy đủ)."""
    if not isinstance(node, dict):
        return ""
    name = (node.get("name") or "").strip()
    n_id = node.get("id") or ""
    if n_id in _CANON_NAME_BY_ID:
        return _CANON_NAME_BY_ID[n_id]
    low = name.lower()
    if low in _CANON_NAME_BY_LOW:
        return _CANON_NAME_BY_LOW[low]
    return name


def flatten_units_for_select(tree: dict) -> list[tuple]:
    """Flatten cây đơn vị thành list (id, hiển_thị_với_indent) để dùng cho Dropdown.

    Bỏ qua node "root" (Trung đoàn) và bỏ qua node type="assistant"
    (chức danh cá nhân như Trợ lý, Nhân viên — KHÔNG phải đơn vị tổ chức).

    Top-level (con trực tiếp của Trung đoàn) sắp theo TOP_LEVEL_ORDER:
    Ban chỉ huy → CQTM → CQCT → HC-KT → TĐ7/8/9 → các Đại đội trực thuộc.
    """
    out: list[tuple] = []

    def walk(node: dict, depth: int):
        ntype = node.get("type", "")
        # Bỏ qua node gốc và các node chức danh cá nhân
        if node.get("id") != "root" and ntype in _ORG_UNIT_TYPES:
            indent = "    " * (depth - 1)
            display_name = canonical_unit_name(node)
            out.append((node["id"], f"{indent}{display_name}"))
        children = list(node.get("children") or [])
        if node.get("id") == "root":
            # u-chy (id cũ "Chỉ huy trung đoàn") cũng được pin lên đầu giống u-bch
            order = list(TOP_LEVEL_ORDER)
            if "u-chy" not in order:
                order.insert(order.index("u-bch") + 1 if "u-bch" in order else 0, "u-chy")
            idx = {uid: i for i, uid in enumerate(order)}
            children.sort(key=lambda c: idx.get(c.get("id"), len(order) + 1))
        for c in children:
            walk(c, depth + 1)

    walk(tree, 0)
    return out


def titles_for_unit(tree: dict, unit_id: str) -> list[str]:
    """Trả về danh sách chức danh phù hợp cho đơn vị được chọn.

    Duyệt cây để tìm node có id == unit_id, sau đó thu thập tên các node
    con có type="assistant" (chức danh cá nhân nằm trong đơn vị đó).
    Luôn thêm "Chiến sĩ" ở đầu danh sách.
    """
    def _find(node: dict, target: str) -> dict | None:
        if node.get("id") == target:
            return node
        for c in node.get("children", []):
            r = _find(c, target)
            if r:
                return r
        return None

    def _collect_titles(node: dict) -> list[str]:
        titles = []
        for c in node.get("children", []):
            if c.get("type") == "assistant":
                titles.append(c["name"])
        return titles

    unit_node = _find(tree, unit_id)
    if not unit_node:
        return list(TITLES)  # fallback: toàn bộ danh sách

    # Thu thập từ node và node cha (nếu có commanderTitle), tránh trùng lặp
    result = []
    seen = set()
    def _add(name: str):
        if not name:
            return
        key = name.strip().lower()
        if key in seen:
            return
        seen.add(key)
        result.append(name)

    _add(unit_node.get("commanderTitle"))
    for t in _collect_titles(unit_node):
        _add(t)

    # Cơ quan (department), Ban chỉ huy (command), Kho/Trạm (station)
    # KHÔNG có Tiểu đội trưởng / Trung đội trưởng / Chiến sĩ
    # — chỉ có các chức danh trợ lý / nhân viên / chỉ huy thuộc đơn vị đó.
    u_type = (unit_node.get("type") or "").lower()
    if u_type not in ("department", "command", "station"):
        # Đơn vị chiến đấu (đại đội/tiểu đoàn/trung đội/tiểu đội): có Chiến sĩ + chỉ huy đội/trung đội
        for b in ("Chiến sĩ", "Tiểu đội trưởng", "Trung đội trưởng"):
            _add(b)

    return result


def seed_soldiers() -> list[dict]:
    """Không seed quân nhân demo — admin sẽ thêm thật qua app."""
    return []


def seed_user_profile() -> dict:
    """Profile rỗng — sẽ được điền sau khi user login bằng số quân thật."""
    return {
        "name": "", "username": "", "rank": "",
        "role": "", "unitId": "", "unitName": "",
        "dob": "", "hometown": "", "phone": "",
        "enlistYear": "", "party": "", "email": "",
        "serviceYears": 0,
        "photoUrl": "",
    }


def now_ms() -> int:
    return int(time.time() * 1000)


def seed_notifs() -> list[dict]:
    """Không seed thông báo demo."""
    return []


def seed_shifts() -> list[dict]:
    """Không seed ca trực demo."""
    return []


def seed_reports() -> list[dict]:
    """Không seed báo cáo demo."""
    return []


def seed_f47() -> list[dict]:
    """Seed dữ liệu mẫu cho chiến dịch F47."""
    return [
        {
            "id": "f47_001",
            "title": "Chiến dịch Xuân 2026",
            "desc": "Chiến dịch tuyên truyền Tết Nguyên Đán",
            "creator": "Nguyễn Văn A",
            "creatorRole": "Chính uỷ",
            "scope": "Toàn Trung đoàn",
            "scopeUnits": ["Toàn Trung đoàn"],
            "platforms": ["Facebook", "Zalo"],
            "targetLink": "https://facebook.com/example",
            "deadline": now_ms() + 7 * 24 * 3600_000,  # 7 ngày từ bây giờ
            "status": "live",
            "campaignType": "CMT",
            "members": ["001", "002", "003"],
            "submissions": {},
            "createdAt": now_ms() - 24 * 3600_000,  # 1 ngày trước
            "sampleMediaUrl": "",
        },
        {
            "id": "f47_002",
            "title": "Chiến dịch Hè 2026",
            "desc": "Hoạt động tình nguyện mùa hè",
            "creator": "Trần Văn B",
            "creatorRole": "Chủ nhiệm Chính trị",
            "scope": "Toàn Trung đoàn",
            "scopeUnits": ["Toàn Trung đoàn"],
            "platforms": ["TikTok", "YouTube"],
            "targetLink": "https://youtube.com/example",
            "deadline": now_ms() + 30 * 24 * 3600_000,  # 30 ngày từ bây giờ
            "status": "live",
            "campaignType": "Báo cáo",
            "members": ["001", "002"],
            "submissions": {},
            "createdAt": now_ms() - 2 * 24 * 3600_000,  # 2 ngày trước
            "sampleMediaUrl": "",
        }
    ]


def seed_daily_shares() -> list[dict]:
    """Bài chia sẻ cá nhân hàng ngày — mặc định trống."""
    return []


def seed_chat_rooms() -> list[dict]:
    """Không seed phòng chat mặc định — người dùng tự tạo nhóm."""
    return []


# ============================================================================
# Helper module-level
# ============================================================================

def get(key: str, default_factory):
    return STORE.get(key, default_factory)


def set_value(key: str, value):
    STORE.set(key, value)


def log_activity(text: str) -> None:
    arr = STORE.get("activity", lambda: [])
    arr.insert(0, {"at": now_ms(), "text": text})
    if len(arr) > 200:
        del arr[200:]
    STORE.set("activity", arr)


def push_notif(ntype: str, title: str, desc: str, link: str | None = None,
               target_uid: str | None = None, sender_name: str = "") -> None:
    """Đẩy 1 notif vào danh sách chung.

    Nếu `target_uid` được set, notif này CHỈ hiển thị cho user đó (FE filter).
    Nếu không set → broadcast cho tất cả user.
    `sender_name`: tên người gửi/tạo (hiển thị trong card thông báo).
    """
    arr = STORE.get("notifs", seed_notifs)
    arr.insert(0, {
        "id": "n" + str(now_ms()),
        "type": ntype, "title": title, "desc": desc,
        "at": now_ms(), "read": False, "link": link,
        "targetUid": target_uid or "",
        "senderName": sender_name,
    })
    if len(arr) > 200:
        del arr[200:]
    STORE.set("notifs", arr)
    # Tự động queue FCM push notification cho targeted notifs
    if target_uid and STORE.is_bound():
        try:
            from . import fcm as _fcm
            _fcm.queue_notification(
                STORE._fs_client, target_uid,
                title=title, body=desc, link=link,
                data={"type": ntype, "link": link or "", "senderName": sender_name},
            )
        except Exception:
            pass


def filter_notifs_for_user(notifs: list, my_uid: str) -> list:
    """Chỉ trả về notif không có target (broadcast) HOẶC target == my_uid."""
    out = []
    for n in notifs or []:
        t = (n.get("targetUid") or "").strip()
        if not t or t == my_uid:
            out.append(n)
    return out


# ============================================================================
# Chat helpers (Firestore-backed real-time, dùng collection riêng)
# ============================================================================

def chat_messages_collection(room_id: str) -> str:
    return f"chat_rooms/{room_id}/messages"


def upsert_chat_room(room: dict) -> dict:
    rid = (room or {}).get("id") or ""
    if not rid:
        return room
    now = now_ms()
    rooms = STORE.get("chat_rooms", seed_chat_rooms)
    prev = next((dict(r) for r in rooms if r.get("id") == rid), {}) or {}
    
    # If not in local cache, fetch from Firestore to preserve existing fields
    if not prev and STORE.is_bound():
        try:
            remote_doc = STORE._fs_client.get_doc(f"chat_rooms/{rid}")
            if remote_doc:
                prev = remote_doc
        except Exception:
            pass
            
    merged = {**prev, **(room or {})}
    normalized = {
        "id": rid,
        "name": merged.get("name") or room.get("name") or rid,
        "type": merged.get("type") or room.get("type") or "group",
        "members": merged.get("members") or room.get("members") or [],
        "lastMessage": (merged.get("lastMessage") or "")[:120],
        "lastAt": int(merged.get("lastAt") or now),
        "unread": int(merged.get("unread") or 0),
        "status": merged.get("status", "") or "",
        "lastReadAt": dict(merged.get("lastReadAt") or {}),
        "unreadByUser": dict(merged.get("unreadByUser") or {}),
        "pinnedMessageIds": list(merged.get("pinnedMessageIds") or []),
    }

    found = False
    for i, r in enumerate(list(rooms)):
        if r.get("id") == rid:
            rooms[i] = {**r, **normalized}
            found = True
            break
    if not found:
        rooms.insert(0, normalized)
    rooms.sort(key=lambda x: int(x.get("lastAt") or 0), reverse=True)
    STORE.set("chat_rooms", rooms)

    if STORE.is_bound():
        try:
            # Firestore: map field values phải là integer hợp lệ cho unreadByUser
            ub_clean = {
                str(k): int(v) for k, v in normalized["unreadByUser"].items()
            }
            lr_clean = {
                str(k): int(v) for k, v in normalized["lastReadAt"].items()
            }
            STORE._fs_client.set_doc(
                f"chat_rooms/{rid}",
                {
                    "name": normalized["name"],
                    "type": normalized["type"],
                    "members": normalized["members"],
                    "lastMessage": normalized["lastMessage"],
                    "lastAt": normalized["lastAt"],
                    "lastReadAt": lr_clean,
                    "unreadByUser": ub_clean,
                    "pinnedMessageIds": list(normalized["pinnedMessageIds"]),
                },
            )
        except Exception:
            pass
    return normalized


def chat_unread_for_user(room: dict, user_id: str) -> int:
    """Số tin chưa đọc của user trong một phòng (từ unreadByUser)."""
    if not user_id:
        return int(room.get("unread") or 0)
    ub = room.get("unreadByUser") or {}
    v = ub.get(str(user_id))
    if v is not None:
        return max(0, int(v))
    return max(0, int(room.get("unread") or 0))


def mark_all_chats_read(user_id: str) -> None:
    """Đánh dấu đã đọc mọi phòng chat cho user (badge về 0)."""
    uid = str(user_id or "").strip()
    if not uid:
        return
    now = now_ms()
    rooms = STORE.get("chat_rooms", seed_chat_rooms)
    for r in rooms:
        rid = r.get("id")
        if not rid:
            continue
        lr = dict(r.get("lastReadAt") or {})
        lr[uid] = now
        ub = dict(r.get("unreadByUser") or {})
        ub[uid] = 0
        upsert_chat_room({"id": rid, "lastReadAt": lr, "unreadByUser": ub})


def mark_chat_room_read(room_id: str, user_id: str) -> None:
    """Đánh dấu user đã đọc tới thời điểm hiện tại; xoá unread của user."""
    uid = str(user_id or "").strip()
    if not uid or not room_id:
        return
    now = now_ms()
    rooms = STORE.get("chat_rooms", seed_chat_rooms)
    prev = next((dict(r) for r in rooms if r.get("id") == room_id), {}) or {}
    lr = dict(prev.get("lastReadAt") or {})
    lr[uid] = now
    ub = dict(prev.get("unreadByUser") or {})
    ub[uid] = 0
    upsert_chat_room({"id": room_id, "lastReadAt": lr, "unreadByUser": ub})


def refresh_chat_room_meta(room_id: str) -> None:
    """Kéo lastReadAt / unreadByUser / lastMessage từ Firestore vào cache cục bộ."""
    if not STORE.is_bound() or not room_id:
        return
    try:
        doc = STORE._fs_client.get_doc(f"chat_rooms/{room_id}")
    except Exception:
        return
    if not doc:
        return
    rooms = STORE.get("chat_rooms", seed_chat_rooms)
    idx = next((i for i, r in enumerate(rooms) if r.get("id") == room_id), None)
    if idx is None:
        return
    ub_raw = doc.get("unreadByUser") or {}
    lr_raw = doc.get("lastReadAt") or {}
    ub = {str(k): int(v) for k, v in ub_raw.items()}
    lr = {str(k): int(v) for k, v in lr_raw.items()}
    pins_raw = doc.get("pinnedMessageIds") or []
    pins = [str(x) for x in pins_raw]
    rooms[idx] = {
        **rooms[idx],
        "lastMessage": doc.get("lastMessage", rooms[idx].get("lastMessage", "")) or "",
        "lastAt": int(doc.get("lastAt") or rooms[idx].get("lastAt") or 0),
        "unreadByUser": ub,
        "lastReadAt": lr,
        "pinnedMessageIds": pins,
    }
    STORE.set("chat_rooms", rooms)


def _msg_doc_id(m: dict) -> str:
    return str(m.get("id") or m.get("_id") or "").strip()


def sort_chat_messages_with_pins(items: list[dict], pinned_ids: list[str]) -> list[dict]:
    """Tin đã ghim lên trên (theo thứ tự ghim), còn lại sắp xếp theo thời gian."""
    order = [str(x) for x in (pinned_ids or [])]
    by_id = {_msg_doc_id(m): m for m in items if _msg_doc_id(m)}
    pinned_block = [by_id[pid] for pid in order if pid in by_id]
    pset = set(order)
    rest = sorted(
        [m for m in items if _msg_doc_id(m) not in pset],
        key=lambda m: int(m.get("at") or 0),
    )
    return pinned_block + rest


def toggle_pin_chat_message(room_id: str, msg_id: str) -> None:
    """Ghim / bỏ ghim tin trong phòng (lưu trên doc phòng, tối đa 30 tin)."""
    mid = str(msg_id or "").strip()
    if not room_id or not mid:
        return
    rooms = STORE.get("chat_rooms", seed_chat_rooms)
    prev = next((dict(r) for r in rooms if r.get("id") == room_id), {}) or {}
    pins = [str(x) for x in (prev.get("pinnedMessageIds") or [])]
    if mid in pins:
        pins = [p for p in pins if p != mid]
    else:
        pins = [p for p in pins if p != mid]
        pins.append(mid)
        if len(pins) > 30:
            pins = pins[-30:]
    upsert_chat_room({"id": room_id, "pinnedMessageIds": pins})


def delete_chat_message(room_id: str, msg_id: str, user_id: str) -> bool:
    """Xóa tin nhắn (chỉ người gửi; Firestore rules)."""
    mid = str(msg_id or "").strip()
    uid = str(user_id or "").strip()
    if not STORE.is_bound() or not room_id or not mid or not uid:
        return False
    path = f"{chat_messages_collection(room_id)}/{mid}"
    try:
        doc = STORE._fs_client.get_doc(path)
        if not doc or str(doc.get("senderId") or "") != uid:
            return False
        STORE._fs_client.delete_doc(path)
    except Exception:
        return False
    rooms = STORE.get("chat_rooms", seed_chat_rooms)
    prev = next((dict(r) for r in rooms if r.get("id") == room_id), {}) or {}
    pins = [str(x) for x in (prev.get("pinnedMessageIds") or []) if str(x) != mid]
    upsert_chat_room({"id": room_id, "pinnedMessageIds": pins})
    return True


def send_chat_message(room_id: str, sender_id: str, sender_name: str,
                      text: str) -> dict | None:
    if not STORE.is_bound():
        return None
    msg_id = f"m_{now_ms()}_{sender_id[:8]}"
    msg = {
        "id": msg_id, "senderId": sender_id, "senderName": sender_name,
        "text": text, "at": now_ms(),
    }
    path_room = f"chat_rooms/{room_id}"
    try:
        STORE._fs_client.set_doc(
            f"{chat_messages_collection(room_id)}/{msg_id}", msg
        )
        remote = STORE._fs_client.get_doc(path_room)
        members = list((remote or {}).get("members") or [])
        if not members:
            rooms_local = STORE.get("chat_rooms", seed_chat_rooms)
            rloc = next((x for x in rooms_local if x.get("id") == room_id), None)
            if rloc:
                members = list(rloc.get("members") or [])
        unread_by = dict((remote or {}).get("unreadByUser") or {})
        sid = str(sender_id)
        for m in members:
            if m is None:
                continue
            if str(m) == sid:
                continue
            k = str(m)
            unread_by[k] = int(unread_by.get(k) or 0) + 1
        patch = {
            "lastMessage": text[:120],
            "lastAt": msg["at"],
            "unreadByUser": unread_by,
        }
        STORE._fs_client.set_doc(path_room, patch)
        upsert_chat_room({"id": room_id, **patch})
    except Exception:
        return None
    return msg


def fetch_chat_messages(room_id: str, limit: int = 100) -> list[dict]:
    if not STORE.is_bound():
        return []
    try:
        items = STORE._fs_client.list_collection(chat_messages_collection(room_id))
    except Exception:
        return []
    items.sort(key=lambda m: m.get("at", 0))
    return items[-limit:]




def refresh_soldiers_from_users() -> int:
    """Đồng bộ list 'soldiers' từ Firestore collection 'users/' — SOURCE OF TRUTH.

    Mỗi user thật có doc users/{uid}. Hàm này pull hết về, map thành soldier dict
    và ghi đè app_data.soldiers. Như vậy:
    - Tài khoản đã xoá khỏi Firebase Auth / users → không còn trong soldiers
    - Mọi tiện ích (tạo nhóm chat, F47, CTĐ-CTCT, danh bạ...) đều thấy cùng 1 list
    Trả về số soldier đã sync.
    """
    if not STORE.is_bound() or not STORE._fs_client:
        return 0
    try:
        users = STORE._fs_client.list_collection("users")
    except Exception:
        return 0

    # Giữ lại các thay đổi local chưa kịp sync lên Firestore
    # (vd: vừa duyệt tài khoản nhưng Firestore chưa cập nhật kịp)
    import time as _time
    _local_overrides = getattr(STORE, "_account_status_overrides", {})
    _override_expiry = getattr(STORE, "_account_status_expiry", {})
    now_ts = _time.time()

    soldiers: list[dict] = []
    for u in users or []:
        uid = u.get("_id")
        if not uid:
            continue
        # Nếu có override local còn hiệu lực (trong 30 giây), dùng nó thay vì Firestore
        remote_status = u.get("accountStatus") or "active"
        if uid in _local_overrides and now_ts < _override_expiry.get(uid, 0):
            account_status = _local_overrides[uid]
        else:
            account_status = remote_status
            # Xóa override đã hết hạn
            _local_overrides.pop(uid, None)
            _override_expiry.pop(uid, None)

        soldiers.append({
            "id": uid,
            "unitId": u.get("unitId") or "",
            "unitName": u.get("unitName") or "",
            "name": u.get("name") or u.get("username") or uid,
            "rank": u.get("rank") or "",
            "role": u.get("role") or "",
            "username": u.get("username") or "",
            "phone": u.get("phone") or "",
            "email": u.get("email") or "",
            "accountStatus": account_status,
            "isAdmin": bool(u.get("isAdmin")),
            "adminLevel": int(u.get("adminLevel") or 0),
            "password_plain": u.get("password_plain") or "",
        })
    # Set local + push lên app_data để các thiết bị khác cũng có
    STORE.set("soldiers", soldiers)
    return len(soldiers)


def set_account_status_override(uid: str, status: str, ttl_seconds: float = 30.0) -> None:
    """Đặt override tạm thời cho accountStatus của một user.
    Dùng sau khi duyệt/khóa tài khoản để tránh bị sync Firestore ghi đè lại.
    """
    import time as _time
    if not hasattr(STORE, "_account_status_overrides"):
        STORE._account_status_overrides = {}
        STORE._account_status_expiry = {}
    STORE._account_status_overrides[uid] = status
    STORE._account_status_expiry[uid] = _time.time() + ttl_seconds


def delete_chat_room(room_id: str) -> bool:
    """Xoá phòng chat ở cả 2 phía: xoá toàn bộ tin nhắn trong Firestore +
    metadata phòng + xoá khỏi cache app_data.chat_rooms.

    Trả True nếu xoá thành công. Best-effort - bỏ qua lỗi từng bước.
    """
    if not room_id:
        return False
    if STORE.is_bound() and STORE._fs_client:
        try:
            msgs = STORE._fs_client.list_collection(
                chat_messages_collection(room_id)
            )
            for m in msgs:
                mid = m.get("_id")
                if mid:
                    try:
                        STORE._fs_client.delete_doc(
                            f"{chat_messages_collection(room_id)}/{mid}"
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            STORE._fs_client.delete_doc(f"chat_rooms/{room_id}")
        except Exception:
            pass
    rooms = STORE.get("chat_rooms", seed_chat_rooms)
    new_rooms = [r for r in rooms if r.get("id") != room_id]
    STORE.set("chat_rooms", new_rooms)
    return True


def listen_chat_messages(room_id: str, callback, interval: float = 2.0):
    if not STORE.is_bound():
        return lambda: None
    return STORE._fs_client.listen_collection(
        chat_messages_collection(room_id), callback, interval=interval
    )

def refresh_guests() -> int:
    """Đồng bộ danh sách khách từ Firestore collection 'guests'."""
    if not STORE.is_bound() or not STORE._fs_client:
        return 0
    try:
        guests = STORE._fs_client.list_collection("guests")
    except Exception:
        return 0
    STORE.set("guests", guests)
    return len(guests)
