import main
from app.store import STORE

STORE.sync_from_firestore()

soldiers = STORE.get('soldiers', [])
print(f'Total soldiers from Firestore: {len(soldiers)}')

for s in soldiers:
    print(f"- {s.get('name')}: {s.get('unitId')} ({s.get('unitName')}) AdminLevel: {s.get('adminLevel')}")
