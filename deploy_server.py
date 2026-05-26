"""
Deploy server LL47 lên VPS 103.82.26.251.

Quy trình:
  1. SSH vào VPS
  2. Pull code mới từ GitHub (hoặc rsync nếu không có git trên VPS)
  3. npm install (cài dependencies mới nếu có)
  4. pm2 restart (hoặc node restart nếu dùng systemd)
  5. Kiểm tra /health endpoint
"""
import subprocess
import sys
import time
import urllib.request

HOST = "27.71.20.168"
USER = "root"
PASSWORD = "Zzxcvbnm12@"
REMOTE_DIR = "/root/ll47_v3"
REPO_URL = "https://github.com/sh13a2026-pixel/ll47-e141.git"
HEALTH_URL = f"http://{HOST}/health"

# ── Kiểm tra paramiko ────────────────────────────────────────────────────────
try:
    import paramiko
except ImportError:
    print("[i] Cai paramiko...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko", "--quiet"])
    import paramiko


def ssh_run(client: paramiko.SSHClient, cmd: str, check: bool = True) -> tuple[str, str]:
    """Chạy lệnh SSH, in output, trả (stdout, stderr)."""
    print(f"  $ {cmd}")
    _, stdout, stderr = client.exec_command(cmd, get_pty=True)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if out:
        for line in out.splitlines():
            print(f"    {line}")
    if err and "warning" not in err.lower():
        for line in err.splitlines():
            print(f"    [err] {line}")
    if check and stdout.channel.recv_exit_status() != 0 and not out:
        pass  # một số lệnh trả exit != 0 nhưng vẫn OK
    return out, err


def check_health() -> bool:
    """Ping /health endpoint, thử tối đa 10 lần."""
    print(f"\n[i] Kiem tra server tai {HEALTH_URL} ...")
    for i in range(10):
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=5) as r:
                body = r.read().decode()
                if '"ok":true' in body or '"ok": true' in body:
                    print(f"  ✅ Server OK: {body}")
                    return True
        except Exception as e:
            print(f"  [{i+1}/10] Chua phan hoi ({e}), thu lai sau 3s...")
            time.sleep(3)
    return False


def main():
    print(f"\n[1/5] Ket noi SSH toi {USER}@{HOST}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    except Exception as e:
        print(f"[X] Ket noi SSH that bai: {e}")
        sys.exit(1)
    print("  ✅ Da ket noi")

    # ── Bước 2: Kiểm tra thư mục, clone hoặc pull ───────────────────────────
    print(f"\n[2/5] Cap nhat code tren VPS ({REMOTE_DIR})...")
    out, _ = ssh_run(client, f"test -d {REMOTE_DIR}/.git && echo EXISTS || echo NOTFOUND", check=False)

    if "NOTFOUND" in out or "EXISTS" not in out:
        print("  [i] Chua co repo, dang clone...")
        ssh_run(client, f"mkdir -p {REMOTE_DIR}")
        ssh_run(client, f"git clone {REPO_URL} {REMOTE_DIR}")
    else:
        print("  [i] Da co repo, dang pull...")
        ssh_run(client, f"cd {REMOTE_DIR} && git fetch origin && git reset --hard origin/main")

    # ── Bước 3: npm install ──────────────────────────────────────────────────
    print(f"\n[3/5] Cai npm dependencies...")
    ssh_run(client, f"cd {REMOTE_DIR}/server && npm install --omit=dev 2>&1 | tail -5")

    # ── Bước 4: Đảm bảo .env tồn tại trên VPS ───────────────────────────────
    print(f"\n[4/5] Kiem tra .env...")
    out, _ = ssh_run(client, f"test -f {REMOTE_DIR}/server/.env && echo OK || echo MISSING", check=False)
    if "MISSING" in out:
        print("  [!] Chua co .env tren VPS — dang copy tu .env.example...")
        ssh_run(client, f"cp {REMOTE_DIR}/server/.env.example {REMOTE_DIR}/server/.env")
        print("  [!] Nho chinh sua .env tren VPS: MONGODB_URI, JWT_SECRET, PUBLIC_URL")

    # ── Bước 5: Restart server ───────────────────────────────────────────────
    print(f"\n[5/5] Restart server...")

    # Thử pm2 trước (process manager tốt nhất cho production)
    out_pm2, _ = ssh_run(client, "which pm2 2>/dev/null || echo NOPE", check=False)
    if "NOPE" not in out_pm2 and out_pm2.strip():
        print("  [i] Dung pm2...")
        # Kiểm tra xem process ll47 đã có chưa
        out_list, _ = ssh_run(client, "pm2 list 2>/dev/null | grep ll47 || echo NONE", check=False)
        if "NONE" in out_list:
            print("  [i] Khoi dong lan dau voi pm2...")
            ssh_run(client,
                f"cd {REMOTE_DIR}/server && pm2 start src/index.js --name ll47-backend "
                f"--env production 2>&1"
            )
        else:
            ssh_run(client, f"cd {REMOTE_DIR}/server && pm2 restart ll47-backend 2>&1")
        ssh_run(client, "pm2 save 2>&1 | tail -2")
    else:
        # Không có pm2 — cài rồi dùng
        print("  [i] pm2 chua co, dang cai...")
        ssh_run(client, "npm install -g pm2 2>&1 | tail -3")
        ssh_run(client,
            f"cd {REMOTE_DIR}/server && pm2 start src/index.js --name ll47-backend 2>&1"
        )
        ssh_run(client, "pm2 save && pm2 startup 2>&1 | tail -5")

    # Xem log 10 dòng cuối để kiểm tra
    print("\n  [i] Log gan nhat:")
    ssh_run(client, "pm2 logs ll47-backend --lines 10 --nostream 2>&1 | tail -15", check=False)

    client.close()

    # ── Health check ─────────────────────────────────────────────────────────
    if check_health():
        print("\n✅ Deploy thanh cong!")
        print(f"   Backend: http://{HOST}")
        print(f"   Health:  {HEALTH_URL}")
    else:
        print("\n[!] Server chua phan hoi sau 30s.")
        print("    Kiem tra log: ssh root@{HOST} 'pm2 logs ll47-backend'")
        sys.exit(1)


if __name__ == "__main__":
    main()
