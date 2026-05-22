with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

import re

# Replace the specific filter pattern
pattern = r'store\.filter_notifs_for_user\(\s*store\.get\(\"notifs\", store\.seed_notifs\),\s*AUTH_STATE\.get\(\"uid\"\) or \"\",\s*\)'

content = re.sub(pattern, 'self._my_notifs()', content)

# Inject the _my_notifs method right before _is_admin
method_code = '''    def _my_notifs(self) -> list:
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

    def _is_admin(self) -> bool:'''

content = content.replace('    def _is_admin(self) -> bool:', method_code)

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)
