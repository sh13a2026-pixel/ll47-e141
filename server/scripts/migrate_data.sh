#!/usr/bin/env bash
# ============================================================================
# migrate_data.sh — Chuyển dữ liệu từ MongoDB Atlas sang MongoDB trên VPS
#
# Chạy trên VPS sau khi deploy_vps.sh đã chạy xong.
# ============================================================================
set -euo pipefail

ATLAS_URI="mongodb+srv://sh13a2026_db_user:uK1yOy3HvMt16het@cluster0.pgvgv4h.mongodb.net/?appName=Cluster0&retryWrites=true&w=majority"
DB_NAME="ll47"
BACKUP_DIR="/tmp/ll47_atlas_backup"

echo ""
echo "🔄 Migrate dữ liệu MongoDB Atlas → VPS local"
echo ""

# Cài mongodump/mongorestore nếu chưa có
if ! command -v mongodump &>/dev/null; then
  echo "📦 Cài mongodb-database-tools..."
  apt-get install -y -qq mongodb-database-tools
fi

# Dump từ Atlas
echo "📥 [1/2] Đang dump dữ liệu từ Atlas..."
rm -rf "${BACKUP_DIR}"
mongodump --uri="${ATLAS_URI}" --db="${DB_NAME}" --out="${BACKUP_DIR}"

echo "   ✅ Dump xong: $(du -sh ${BACKUP_DIR}/${DB_NAME} | cut -f1)"

# Restore vào local
echo "📤 [2/2] Đang restore vào MongoDB local..."
mongorestore --db="${DB_NAME}" --drop "${BACKUP_DIR}/${DB_NAME}"

echo ""
echo "✅ Migrate hoàn tất!"
echo "   Kiểm tra: mongosh ${DB_NAME} --eval 'db.getCollectionNames()'"
echo ""

# Dọn dẹp
rm -rf "${BACKUP_DIR}"
echo "🗑️  Đã xoá backup tạm."
