import re

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# We will inject the cleanup worker at the top of show_app
worker_code = '''
def _cleanup_old_reports():
    try:
        import time
        from app import store
        from app import firebase_storage as fb_storage
        now = time.time() * 1000
        
        # Cleanup Reports
        reports = store.get("reports", store.seed_reports)
        keep_reports = []
        changed = False
        for r in reports:
            # 7 days = 7 * 24 * 60 * 60 * 1000 ms = 604800000 ms
            if now - int(r.get("at") or 0) > 604800000:
                # Delete images from storage
                images = r.get("images") or []
                for img_url in images:
                    try:
                        # Extract path from Firebase Storage URL
                        if "/o/" in img_url:
                            path_part = img_url.split("/o/")[1].split("?")[0]
                            import urllib.parse
                            remote_path = urllib.parse.unquote(path_part)
                            fb_storage.delete_object(remote_path, store.AUTH_STATE.get("idToken", ""))
                    except Exception:
                        pass
                changed = True
            else:
                keep_reports.append(r)
        if changed:
            store.set_value("reports", keep_reports)
            
        # Cleanup Daily Shares
        shares = store.get("dailyShares", store.seed_daily_shares)
        keep_shares = []
        changed_shares = False
        for s in shares:
            if now - int(s.get("at") or 0) > 604800000:
                images = s.get("images") or []
                for img_url in images:
                    try:
                        if "/o/" in img_url:
                            path_part = img_url.split("/o/")[1].split("?")[0]
                            import urllib.parse
                            remote_path = urllib.parse.unquote(path_part)
                            fb_storage.delete_object(remote_path, store.AUTH_STATE.get("idToken", ""))
                    except Exception:
                        pass
                changed_shares = True
            else:
                keep_shares.append(s)
                
        if changed_shares:
            store.set_value("dailyShares", keep_shares)
            
    except Exception as e:
        print("Cleanup error:", e)

'''

if '_cleanup_old_reports()' not in content:
    content = content.replace('def show_app(page: ft.Page) -> None:', worker_code + '\n\n' + 'def show_app(page: ft.Page) -> None:\n    try:\n        import threading\n        threading.Thread(target=_cleanup_old_reports, daemon=True).start()\n    except:\n        pass')

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)
