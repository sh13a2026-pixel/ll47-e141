#!/usr/bin/env bash
# ============================================================================
# update_vps.sh — Cập nhật code LL47 Backend trên VPS
#
# Chạy khi bạn push code mới lên GitHub và muốn VPS lấy code mới:
#   ssh root@27.71.20.168 "bash /opt/ll47-backend/server/scripts/update_vps.sh"
# ============================================================================
set -euo pipefail

APP_DIR="/opt/ll47-backend"

echo "🔄 Cập nhật LL47 Backend..."

cd "${APP_DIR}"

# Pull code mới
echo "📥 Pull code từ GitHub..."
git pull origin main

# Cài dependency mới (nếu có)
echo "📦 Cài dependencies..."
cd server
npm install --omit=dev --silent

# Restart backend
echo "🔄 Restart backend..."
pm2 restart ll47-backend

echo ""
echo "✅ Cập nhật xong!"
pm2 status
echo ""
echo "Xem log: pm2 logs ll47-backend --lines 20"
