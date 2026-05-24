#!/usr/bin/env bash
# ============================================================================
# deploy_vps.sh — Triển khai LL47 Backend lên VPS Ubuntu 24.04 (một lần)
#
# Cách dùng:
#   1. SSH vào VPS:  ssh root@103.82.26.251
#   2. Chạy:
#      curl -sL https://raw.githubusercontent.com/sh13a2026-pixel/ll47-e141/main/server/scripts/deploy_vps.sh | bash
#      HOẶC copy file này lên VPS rồi chạy:  bash deploy_vps.sh
# ============================================================================
set -euo pipefail

# ---- Cấu hình ----
APP_USER="ll47"
APP_DIR="/opt/ll47-backend"
REPO_URL="https://github.com/sh13a2026-pixel/ll47-e141.git"
NODE_MAJOR=20
MONGO_VERSION="7.0"
VPS_IP="103.82.26.251"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║    LL47 Backend — Triển khai lên VPS Ubuntu 24.04       ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 0. Kiểm tra root ─────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Chạy script này với quyền root (sudo bash deploy_vps.sh)"
  exit 1
fi

# ── 1. Cập nhật hệ thống ────────────────────────────────────
echo "🔄 [1/8] Cập nhật hệ thống..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq curl wget gnupg2 ca-certificates lsb-release git ufw

# ── 2. Cài Node.js 20 ───────────────────────────────────────
echo "🟢 [2/8] Cài Node.js ${NODE_MAJOR}..."
if ! command -v node &>/dev/null || [[ "$(node -v)" != v${NODE_MAJOR}* ]]; then
  curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -
  apt-get install -y -qq nodejs
fi
echo "   Node $(node -v) — npm $(npm -v)"

# ── 3. Cài MongoDB 7 ────────────────────────────────────────
echo "🍃 [3/8] Cài MongoDB ${MONGO_VERSION}..."
if ! command -v mongod &>/dev/null; then
  # Import MongoDB GPG key
  curl -fsSL https://www.mongodb.org/static/pgp/server-${MONGO_VERSION}.asc | \
    gpg --dearmor -o /usr/share/keyrings/mongodb-server-${MONGO_VERSION}.gpg

  # Thêm repo (Ubuntu 24.04 = noble, 22.04 = jammy)
  CODENAME=$(lsb_release -cs)
  # MongoDB 7.0 chưa có repo cho noble, fallback sang jammy
  if [ "$CODENAME" = "noble" ]; then
    CODENAME="jammy"
  fi
  echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-${MONGO_VERSION}.gpg ] \
https://repo.mongodb.org/apt/ubuntu ${CODENAME}/mongodb-org/${MONGO_VERSION} multiverse" \
    > /etc/apt/sources.list.d/mongodb-org-${MONGO_VERSION}.list

  apt-get update -qq
  apt-get install -y -qq mongodb-org
fi
systemctl enable mongod
systemctl start mongod
echo "   MongoDB $(mongod --version | head -1)"

# ── 4. Cài Nginx ────────────────────────────────────────────
echo "🌐 [4/8] Cài Nginx..."
apt-get install -y -qq nginx
systemctl enable nginx

# ── 5. Cài PM2 ──────────────────────────────────────────────
echo "⚡ [5/8] Cài PM2..."
npm install -g pm2 --silent
pm2 startup systemd -u root --hp /root --silent || true

# ── 6. Tạo user + Clone repo ────────────────────────────────
echo "📦 [6/8] Clone code & cài dependencies..."

# Tạo user ll47 nếu chưa có
id -u ${APP_USER} &>/dev/null || useradd -r -m -s /bin/bash ${APP_USER}

# Clone hoặc pull
if [ -d "${APP_DIR}" ]; then
  echo "   Thư mục ${APP_DIR} đã tồn tại, pull code mới..."
  cd "${APP_DIR}"
  git pull origin main
else
  git clone "${REPO_URL}" "${APP_DIR}"
fi

cd "${APP_DIR}/server"
npm install --omit=dev --silent

# ── 7. Tạo file .env ────────────────────────────────────────
echo "🔧 [7/8] Cấu hình .env..."
JWT_SECRET=$(openssl rand -hex 32)

cat > "${APP_DIR}/server/.env" << ENVEOF
PORT=8080
PUBLIC_URL=http://${VPS_IP}
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=ll47
JWT_SECRET=${JWT_SECRET}
CORS_ORIGIN=*
MAX_UPLOAD_MB=25

# ---- FCM push nền (tuỳ chọn) ----
# Nếu muốn push notification, copy service-account.json vào thư mục server/
# rồi bỏ comment dòng dưới:
# FIREBASE_SERVICE_ACCOUNT_PATH=./service-account.json
ENVEOF

echo "   ✅ .env đã tạo tại ${APP_DIR}/server/.env"
echo "   🔑 JWT_SECRET = ${JWT_SECRET}"
echo "   ⚠️  Lưu lại JWT_SECRET này! Nếu đổi, tất cả user phải đăng nhập lại."

# ── 8. Cấu hình Nginx ───────────────────────────────────────
echo "🌐 [8/8] Cấu hình Nginx reverse proxy..."

cat > /etc/nginx/sites-available/ll47-backend << 'NGINXEOF'
server {
    listen 80;
    server_name _;

    client_max_body_size 30M;

    # Tắt buffer để streaming / WebSocket mượt
    proxy_buffering off;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;

        # WebSocket support (Socket.io)
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Forward thông tin client thật
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Timeout cho WebSocket (giữ kết nối lâu)
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
NGINXEOF

# Enable site, bỏ default
ln -sf /etc/nginx/sites-available/ll47-backend /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

nginx -t && systemctl reload nginx

# ── 9. Firewall ──────────────────────────────────────────────
echo "🔥 Cấu hình firewall (UFW)..."
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP
ufw allow 443/tcp   # HTTPS (dự phòng)
ufw --force enable

# ── 10. Khởi chạy backend bằng PM2 ──────────────────────────
echo "🚀 Khởi chạy backend..."
cd "${APP_DIR}/server"
pm2 delete ll47-backend 2>/dev/null || true
pm2 start src/index.js --name ll47-backend --cwd "${APP_DIR}/server"
pm2 save

# ── Xong ─────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ✅  TRIỂN KHAI THÀNH CÔNG!                             ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║                                                        ║"
echo "║  Backend:  http://${VPS_IP}                       ║"
echo "║  Health:   http://${VPS_IP}/health                ║"
echo "║                                                        ║"
echo "║  Quản lý:                                              ║"
echo "║    pm2 status          — xem trạng thái                ║"
echo "║    pm2 logs            — xem log                       ║"
echo "║    pm2 restart all     — restart backend               ║"
echo "║                                                        ║"
echo "║  MongoDB:                                              ║"
echo "║    mongosh ll47        — vào shell MongoDB             ║"
echo "║                                                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "⏭️  Bước tiếp theo:"
echo "   1. Kiểm tra: curl http://${VPS_IP}/health"
echo "   2. Migrate dữ liệu từ Atlas (nếu cần) — chạy script migrate_data.sh"
echo "   3. Copy service-account.json lên VPS nếu muốn push notification"
echo ""
