import sys
import re

path = 'd:/Projects/ll47_python/app/store.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

new_seed = '''def seed_units() -> dict:
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
        children = [
            _platoon(f"{cid}-b1", "Trung đội 1"),
            _platoon(f"{cid}-b2", "Trung đội 2"),
            _platoon(f"{cid}-b3", "Trung đội 3"),
        ] if with_platoons else []
        return {
            "id": cid, "name": name, "type": "company", "adminLevel": 2,
            "commanderId": None, "commanderTitle": "Đại đội trưởng",
            "children": children,
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
            "id": bid, "name": name, "type": "battalion", "adminLevel": 3,
            "commanderId": None, "commanderTitle": "Tiểu đoàn trưởng",
            "children": [
                {"id": f"{bid}-bch", "name": f"Ban chỉ huy {name}", "type": "command", "adminLevel": 3, "commanderId": None, "commanderTitle": "Tiểu đoàn trưởng", "children": []},
                _aux(f"{bid}-tltm", "Trợ lý Tham mưu"),
                _aux(f"{bid}-tlhc", "Trợ lý Hậu cần"),
                _aux(f"{bid}-nvqy", "Nhân viên Quân y"),
                _aux(f"{bid}-nvqn", "Nhân viên Quân nhu"),
                _aux(f"{bid}-nvqk", "Nhân viên Quân khí"),
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
             "commanderTitle": "Trung đoàn trưởng", "children": []},
            {"id": "u-tm", "name": "Cơ quan Tham mưu", "type": "department", "adminLevel": 4,
             "commanderId": None, "commanderTitle": "Tham mưu trưởng",
             "children": [
                 _aux("u-tm-th", "Trợ lý Tác huấn"),
                 _aux("u-tm-ql", "Trợ lý Quân lực"),
                 _aux("u-tm-cb", "Trợ lý Công binh"),
                 _aux("u-tm-pb", "Trợ lý Pháo binh"),
                 _aux("u-tm-tt", "Trợ lý Thông tin"),
                 _aux("u-tm-pk", "Trợ lý Phòng không"),
                 _aux("u-tm-ts", "Trợ lý Trinh sát"),
                 _aux("u-tm-hh", "Trợ lý Hoá học"),
                 _aux("u-tm-tc", "Trợ lý Tài chính"),
                 _aux("u-tm-hc", "Trợ lý Hành chính"),
                 _aux("u-tm-bm", "Nhân viên Bảo mật"),
                 _aux("u-tm-cy", "Nhân viên Cơ yếu"),
                 _aux("u-tm-nvtc", "Nhân viên Tài chính"),
                 _aux("u-tm-nvql", "Nhân viên Quản lý"),
                 _aux("u-tm-nvql2", "Nhân viên Quân lực"),
                 _platoon("u-tm-vb", "Trung đội Vệ binh", False),
             ]},
            {"id": "u-ct", "name": "Cơ quan Chính Trị", "type": "department", "adminLevel": 4,
             "commanderId": None, "commanderTitle": "Chủ nhiệm Chính trị",
             "children": [
                 _aux("u-ct-th", "Trợ lý Tuyên huấn", 4),
                 _aux("u-ct-cb", "Trợ lý Cán bộ", 3),
                 _aux("u-ct-tc", "Trợ lý Tổ chức kiêm thống kê", 3),
                 _aux("u-ct-qc", "Trợ lý Công tác quần chúng", 3),
                 _aux("u-ct-cs", "Trợ lý Chính sách", 3),
                 _aux("u-ct-dv", "Trợ lý Dân vận", 3),
                 _aux("u-ct-an", "Trợ lý Bảo vệ An ninh", 3),
                 _aux("u-ct-ubkt", "Phó chủ nhiệm Uỷ ban kiểm tra", 3),
                 _aux("u-ct-clb", "Nhân viên Câu lạc bộ"),
                 _aux("u-ct-tk", "Nhân viên Thống kê"),
                 _aux("u-ct-csclb", "Chiến sĩ CLB"),
             ]},
            {"id": "u-hk", "name": "Cơ quan HC-KT", "type": "department",
             "adminLevel": 4, "commanderId": None,
             "commanderTitle": "Chủ nhiệm Hậu cần - Kỹ thuật",
             "children": [
                 _aux("u-hk-xm", "Trợ lý Xe máy"),
                 _aux("u-hk-qn", "Trợ lý Quân nhu"),
                 _aux("u-hk-dt", "Trợ lý Doanh trại"),
                 _aux("u-hk-qk", "Trợ lý Quân khí"),
                 _aux("u-hk-nvqn", "Nhân viên Quân nhu"),
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
            {"id": "u-kqn", "name": "Kho quân nhu", "type": "station", "adminLevel": 1, "commanderId": None, "commanderTitle": "Kho trưởng", "children": []},
            {"id": "u-kqk", "name": "Kho quân khí", "type": "station", "adminLevel": 1, "commanderId": None, "commanderTitle": "Kho trưởng", "children": []},
            {"id": "u-tcb", "name": "Trạm chế biến lương thực", "type": "station", "adminLevel": 1, "commanderId": None, "commanderTitle": "Trạm trưởng", "children": []},
            # Tiểu đoàn
            _battalion("u-d7", "Tiểu đoàn 7", 1),
            _battalion("u-d8", "Tiểu đoàn 8", 5),
            _battalion("u-d9", "Tiểu đoàn 9", 9),
        ],
    }'''

new_titles = '''TITLES: list[str] = [
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
    "Kho trưởng", "Thủ kho", "Trạm trưởng", "Quản trị hệ thống",
]'''

content = re.sub(r'def seed_units\(\) -> dict:.*?(?=\n# Danh sách cấp bậc)', new_seed + '\n\n', content, flags=re.DOTALL)
content = re.sub(r'TITLES: list\[str\] = \[.*?(?=\ndef flatten)', new_titles + '\n\n', content, flags=re.DOTALL)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated store.py successfully.")
