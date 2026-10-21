#!/usr/bin/env bash
# build.sh — dựng ~/Applications/Văn phòng Claude.app (cửa sổ riêng của Văn phòng Claude).
#
#   bash build.sh            # build + mở app
#   bash build.sh --no-open  # chỉ build
#
# App mang theo bản sao server.py + web/ trong Resources, nên chạy được kể cả khi thư mục mã bị dời.
# Sửa code xong thì build lại để app nhận bản mới. Cần: swiftc (Xcode CLT), python3 có Pillow (vẽ biểu tượng).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${VAN_PHONG_APP:-$HOME/Applications/Văn phòng Claude.app}"
OPEN=1
for a in "$@"; do
  case "$a" in
    --no-open) OPEN=0 ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "build.sh: tuỳ chọn lạ: $a" >&2; exit 2 ;;
  esac
done

BUILD="$(mktemp -d "${TMPDIR:-/tmp}/van-phong-build.XXXXXX")"
trap 'rm -rf "$BUILD"' EXIT
swiftc -O -parse-as-library -target arm64-apple-macos14.0 -o "$BUILD/VanPhong" "$HERE/VanPhong.swift"

# Biểu tượng: drone trợ lý trên nền bạc hà bo góc.
python3 - "$HERE/web/assets/tro-ly.png" "$BUILD/icon.png" <<'PY'
import sys
from PIL import Image, ImageDraw
src, out = sys.argv[1], sys.argv[2]
S = 1024
icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(icon)
d.rounded_rectangle((80, 80, S - 80, S - 80), radius=190, fill=(223, 245, 234, 255), outline=(107, 79, 58, 255), width=26)
bot = Image.open(src).convert("RGBA")
k = 640 / max(bot.size)
bot = bot.resize((round(bot.width * k), round(bot.height * k)), Image.LANCZOS)
icon.alpha_composite(bot, ((S - bot.width) // 2, (S - bot.height) // 2 + 20))
icon.save(out)
PY
mkdir -p "$BUILD/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon.png" --out "$BUILD/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/icon.png" --out "$BUILD/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$BUILD/icon.iconset" -o "$BUILD/AppIcon.icns"

pkill -f "Contents/MacOS/VanPhong$" 2>/dev/null || true
pkill -f "Contents/Resources/server.py --port" 2>/dev/null || true  # máy chủ bản cũ trong gói
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/VanPhong" "$APP/Contents/MacOS/VanPhong"
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$HERE/server.py" "$APP/Contents/Resources/server.py"
cp "$HERE/collect.py" "$APP/Contents/Resources/collect.py"   # bộ gom của tab Quota
mkdir -p "$APP/Contents/Resources/tools"
cp "$HERE/tools/gui-link-toi-pid.js" "$APP/Contents/Resources/tools/"
rsync -a --exclude raw "$HERE/web/" "$APP/Contents/Resources/web/"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Văn phòng Claude</string>
  <key>CFBundleDisplayName</key><string>Văn phòng Claude</string>
  <key>CFBundleIdentifier</key><string>local.van-phong-claude</string>
  <key>CFBundleExecutable</key><string>VanPhong</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSAppleEventsUsageDescription</key><string>Mở đúng phiên trong đúng bản app Claude khi bạn bấm "Mở trong app".</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "Đã dựng: $APP"
[ "$OPEN" -eq 1 ] && open "$APP"
exit 0
