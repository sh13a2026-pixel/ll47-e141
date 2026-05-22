import re

with open('app/store.py', 'r', encoding='utf-8') as f:
    content = f.read()

sync_code = '''    def sync_from_firestore(self) -> int:
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
                    d[key] = doc["value"]
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
        return count'''

content = re.sub(r'    def sync_from_firestore\(self\) -> int:.*?        return count', sync_code, content, flags=re.DOTALL)

with open('app/store.py', 'w', encoding='utf-8') as f:
    f.write(content)
