import re

def refactor_main():
    with open("main.py", "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Replace the CTDCTCT constants with the generic DOMAIN_CONFIG
    constants_pattern = r"    CTDCTCT_NGANH: list\[tuple\[str, str\]\] = \[\s*.*?\s*\]\n\n    # Các code.*?CTDCTCT_LEADERSHIP_CODES = \{.*?\}\n\n    # Các chức vụ.*?CTDCTCT_LEAD_TITLE_KEYWORDS = \(.*?\n    \)"
    
    new_constants = """    TASK_DOMAIN_CONFIG = {
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
    )"""
    content = re.sub(constants_pattern, new_constants, content, flags=re.DOTALL)

    # 2. Refactor helper methods
    content = content.replace("def _ctdctct_lead_members(self) -> list[str]:", "def _task_lead_members(self) -> list[str]:")
    content = content.replace("self.CTDCTCT_LEAD_TITLE_KEYWORDS", "self.TASK_LEAD_TITLE_KEYWORDS")

    content = content.replace("def _ctdctct_code(cls, full_name: str) -> str:", "def _task_code(cls, domain: str, full_name: str) -> str:")
    content = re.sub(r"for n, code in cls\.CTDCTCT_NGANH:\s+if n == full_name:\s+return code", r"for n, code in cls.TASK_DOMAIN_CONFIG.get(domain, {}).get('nganh', []):\n            if n == full_name:\n                return code", content)

    content = content.replace("def _ctdctct_eligible_followers(self) -> list[dict]:", "def _task_eligible_followers(self, domain: str) -> list[dict]:")
    content = content.replace('keys = ["chính uỷ", "phó chính uỷ", "chủ nhiệm chính trị",\n                "phó chủ nhiệm chính trị", "trợ lý"]', 'keys = self.TASK_DOMAIN_CONFIG.get(domain, {}).get("follower_keywords", [])')

    content = content.replace("def _ctdctct_can_approve(self, task: dict, my_uid: str) -> bool:", "def _task_can_approve(self, domain: str, task: dict, my_uid: str) -> bool:")

    # 3. Refactor main methods
    methods_to_refactor = [
        "module_ctdctct",
        "ctdctct_open_create",
        "ctdctct_open_task_menu",
        "ctdctct_confirm_delete",
        "ctdctct_open_task_detail",
        "ctdctct_mark_received",
        "ctdctct_open_submit",
        "_ctdctct_back_to_list"
    ]

    for m in methods_to_refactor:
        if m == "module_ctdctct":
            content = content.replace("def module_ctdctct(self) -> ft.Control:", "def _render_task_module(self, domain: str) -> ft.Control:")
        else:
            content = content.replace(f"def {m}(self,", f"def {m.replace('ctdctct_', 'task_')}(self, domain: str,")
            content = content.replace(f"def {m}(self)", f"def {m.replace('ctdctct_', 'task_')}(self, domain: str)")

    # 4. Replace variable and method usages
    content = content.replace("store.get(\"ctdctctTasks\", lambda: [])", "store.get(self.TASK_DOMAIN_CONFIG.get(domain, {}).get('store_key'), lambda: [])")
    content = content.replace("self.CTDCTCT_NGANH", "self.TASK_DOMAIN_CONFIG.get(domain, {}).get('nganh', [])")
    content = content.replace("self.CTDCTCT_LEADERSHIP_CODES", "self.TASK_DOMAIN_CONFIG.get(domain, {}).get('lead_codes', set())")
    
    # Method calls
    content = content.replace("self._ctdctct_lead_members()", "self._task_lead_members()")
    content = content.replace("self._ctdctct_code(", "self._task_code(domain, ")
    content = content.replace("self._ctdctct_eligible_followers()", "self._task_eligible_followers(domain)")
    content = content.replace("self._ctdctct_can_approve(cur, my_uid)", "self._task_can_approve(domain, cur, my_uid)")
    content = content.replace("self.ctdctct_open_task_menu(_t)", "self.task_open_task_menu(domain, _t)")
    content = content.replace("self.ctdctct_open_task_detail(_t)", "self.task_open_task_detail(domain, _t)")
    content = content.replace("self.ctdctct_open_task_detail(cur)", "self.task_open_task_detail(domain, cur)")
    content = content.replace("self.ctdctct_open_task_detail(task)", "self.task_open_task_detail(domain, task)")
    content = content.replace("self.ctdctct_open_create()", "self.task_open_create(domain)")
    content = content.replace("self.ctdctct_open_create(existing=task)", "self.task_open_create(domain, existing=task)")
    content = content.replace("self.ctdctct_confirm_delete(task)", "self.task_confirm_delete(domain, task)")
    content = content.replace("self.ctdctct_mark_received(cur)", "self.task_mark_received(domain, cur)")
    content = content.replace("self.ctdctct_open_submit(cur)", "self.task_open_submit(domain, cur)")
    content = content.replace("self._ctdctct_back_to_list()", "self._task_back_to_list(domain)")

    # 5. Fix getattr string attributes (ctdctct_view -> task_{domain}_view)
    content = re.sub(r'getattr\(self, "ctdctct_view"', r'getattr(self, f"task_{domain}_view"', content)
    content = re.sub(r'self\.ctdctct_view = (.*?)$', r'setattr(self, f"task_{domain}_view", \1)', content, flags=re.MULTILINE)
    
    content = re.sub(r'getattr\(self, "ctdctct_filter"', r'getattr(self, f"task_{domain}_filter"', content)
    content = re.sub(r'self\.ctdctct_filter = (.*?)$', r'setattr(self, f"task_{domain}_filter", \1)', content, flags=re.MULTILINE)
    
    content = re.sub(r'getattr\(self, "ctdctct_filter_open"', r'getattr(self, f"task_{domain}_filter_open"', content)
    content = re.sub(r'self\.ctdctct_filter_open = (.*?)$', r'setattr(self, f"task_{domain}_filter_open", \1)', content, flags=re.MULTILINE)

    # 6. Replace string "CTĐ-CTCT" with dynamic domain title
    content = content.replace('title="Module CTĐ-CTCT"', 'title=self.TASK_DOMAIN_CONFIG.get(domain, {}).get("title")')
    content = content.replace('ft.Text("Nhiệm vụ CTĐ-CTCT"', 'ft.Text(f"Nhiệm vụ {self.TASK_DOMAIN_CONFIG.get(domain, {}).get(\'title\')}"')
    content = content.replace('ft.Text("Nhiệm vụ CTĐ-CTCT: "', 'ft.Text(f"Nhiệm vụ {self.TASK_DOMAIN_CONFIG.get(domain, {}).get(\'title\')}: "')
    content = content.replace('title=ft.Text("Triển khai nhiệm vụ CTĐ-CTCT"', 'title=ft.Text(f"Triển khai nhiệm vụ {self.TASK_DOMAIN_CONFIG.get(domain, {}).get(\'title\')}"')
    content = content.replace('Dialog triển khai nhiệm vụ CTĐ-CTCT', 'Dialog triển khai nhiệm vụ')
    content = content.replace('Module CTĐ-CTCT — list nhiệm vụ + tab xếp hạng', 'Module Task Tracker')

    # Add the dispatchers back for module_ctdctct, module_hcqs, module_pttd
    dispatchers = """
    def module_ctdctct(self) -> ft.Control:
        return self._render_task_module("ctdctct")

    def module_hcqs(self) -> ft.Control:
        return self._render_task_module("hcqs")

    def module_pttd(self) -> ft.Control:
        return self._render_task_module("pttd")

    def _render_task_module(self, domain: str) -> ft.Control:"""
    content = content.replace("def _render_task_module(self, domain: str) -> ft.Control:", dispatchers)

    with open("main_refactored.py", "w", encoding="utf-8") as f:
        f.write(content)

if __name__ == "__main__":
    refactor_main()
