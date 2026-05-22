#!/usr/bin/env bash
#
# Build APK tự động trên Linux / macOS / WSL
# Chạy: bash build_apk.sh
#
set -e

cd "$(dirname "$0")"

echo "=========================================="
echo " Quản lý LL47 e141 — Build APK"
echo "=========================================="

# 1. Kiểm tra Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Cần Python 3.10+. Tải: https://www.python.org/"
  exit 1
fi
echo "✓ Python: $(python3 --version)"

# 2. Kiểm tra Java
if ! command -v java &>/dev/null; then
  echo "❌ Cần Java JDK 17. Tải: https://adoptium.net/"
  echo "  Sau khi cài, chạy: export JAVA_HOME=/path/to/jdk-17"
  exit 1
fi
java_ver=$(java -version 2>&1 | head -1 | awk -F\" '{print $2}' | cut -d. -f1)
if [ "$java_ver" -lt 17 ]; then
  echo "⚠️  Java hiện tại: $(java -version 2>&1 | head -1)"
  echo "   Khuyến nghị JDK 17. Có thể vẫn build được nhưng có thể gặp warning."
fi
echo "✓ Java: $(java -version 2>&1 | head -1)"

# 3. Kiểm tra Android SDK
if [ -z "$ANDROID_HOME" ] && [ -z "$ANDROID_SDK_ROOT" ]; then
  echo "⚠️  Chưa có ANDROID_HOME. Flet sẽ tự kéo Android SDK (mất ~5 phút lần đầu)."
fi

# 4. Tạo virtualenv
if [ ! -d ".venv" ]; then
  echo ""
  echo "📦 Tạo virtualenv..."
  python3 -m venv .venv
fi
source .venv/bin/activate

# 5. Cài Flet
echo "📦 Cài flet..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

# 6. Build
echo ""
echo "🚀 Bắt đầu build APK (lần đầu mất 10-20 phút)..."
echo "----------------------------------------------"
flet build apk \
  --project ll47_e141 \
  --org vn.mil.e141 \
  --product "Quản lý LL47 e141"

echo ""
echo "=========================================="
echo "✅ Build xong!"
echo "📦 File APK: $(pwd)/build/apk/app-release.apk"
echo "📱 Copy file này vào điện thoại và cài"
echo "=========================================="
