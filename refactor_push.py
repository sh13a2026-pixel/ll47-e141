import re

with open('app/store.py', 'r', encoding='utf-8') as f:
    content = f.read()

push_code = '''    def _push(self, key: str, value: Any) -> None:
        if not self._fs_client:
            return
        if key in self.PRIVATE_KEYS:
            return
        self._pending_writes.add(key)

        client = self._fs_client
        path = f"{self.APP_DATA_COLLECTION}/{key}"
        
        # V2 Collections logic: push items individually if they belong to v2
        v2_collections = {
            "soldiers": "v2_soldiers",
            "reports": "v2_reports",
            "notifs": "v2_notifs",
            "f47Campaigns": "v2_f47Campaigns"
        }
        
        def bg_push():
            try:
                if key in v2_collections and isinstance(value, list):
                    col_name = v2_collections[key]
                    for item in value:
                        item_id = str(item.get("id") or item.get("_id") or "")
                        if item_id:
                            # Push individual document
                            client.set_doc(f"{col_name}/e141/{item_id}", item)
                else:
                    client.set_doc(path, {"value": value})
                self._pending_writes.discard(key)
            except Exception as e:
                pass

        # Push directly in background without waiting
        import threading
        t = threading.Thread(target=bg_push, daemon=True)
        t.start()'''

content = re.sub(r'    def _push\(self, key: str, value: Any\) -> None:.*?        t\.start\(\)', push_code, content, flags=re.DOTALL)

with open('app/store.py', 'w', encoding='utf-8') as f:
    f.write(content)
