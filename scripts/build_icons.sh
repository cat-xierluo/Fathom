#!/usr/bin/env bash
#
# ISS-045 · 正式图标集生成（深度环 Logo，用户 2026-09-19 确认）
#
# 用 sips / iconutil 把 ``apps/desktop/src-tauri/icons/icon.png``（1024
# 深度环源，由 scripts/build_app_icon.py 从 icons/icon.svg 的 canonical
# 几何渲染）缩放成 Tauri 期望的 iconset（16/32/64/128/256/512），再合成
# ``icon.icns``。
#
# 输出：
# - apps/desktop/src-tauri/icons/icon.iconset/icon_*.png
# - apps/desktop/src-tauri/icons/icon.icns
# - apps/desktop/src-tauri/icons/32x32.png 等单文件（tauri 引用）
#
# bash 3.2 兼容。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/apps/desktop/src-tauri/icons/icon.png"
ICON_DIR="$ROOT/apps/desktop/src-tauri/icons"
ICONSET_DIR="$ICON_DIR/icon.iconset"

if [ ! -f "$SRC" ]; then
  echo "[build_icons] FAIL：未找到源 $SRC" >&2
  exit 1
fi

mkdir -p "$ICONSET_DIR"

# 清理旧产物（除 .gitkeep 外）
rm -f "$ICONSET_DIR"/icon_*.png

# 用 sips 放大生成所有需要的尺寸；PNG 保留 alpha
for size in 16 32 64 128 256 512 1024; do
  sips -z "$size" "$size" "$SRC" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null 2>&1 || {
    echo "[build_icons] FAIL：sips 生成 ${size}x${size} 失败" >&2
    exit 1
  }
done
# @2x 图标
sips -z 64 64 "$SRC" --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null 2>&1
sips -z 256 256 "$SRC" --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null 2>&1
sips -z 512 512 "$SRC" --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null 2>&1

# 合成 icns
iconutil -c icns "$ICONSET_DIR" -o "$ICON_DIR/icon.icns"

# 同时输出 Tauri 期望的单文件图标（cargo tauri build 不依赖 iconset，
# 但 tauri.conf.json bundle.icon 列表里需要 32x32.png / 128x128.png /
# 128x128@2x.png / icon.icns）
cp "$ICONSET_DIR/icon_32x32.png" "$ICON_DIR/32x32.png"
cp "$ICONSET_DIR/icon_128x128.png" "$ICON_DIR/128x128.png"
cp "$ICONSET_DIR/icon_128x128@2x.png" "$ICON_DIR/128x128@2x.png"

# ICO（Windows 占位，避免 tauri 缺资源报错；macOS 构建实际不读）
if command -v python3 >/dev/null 2>&1; then
  python3 - "$ICONSET_DIR/icon_32x32.png" "$ICON_DIR/icon.ico" <<'PYEOF'
import struct, sys
from pathlib import Path
src = Path(sys.argv[1])
dst = Path(sys.argv[2])
png_bytes = src.read_bytes()
# ICONDIR (6) + ICONDIRENTRY (16) + PNG data
header = struct.pack("<HHH", 0, 1, 1)
entry = struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png_bytes), 22)
dst.write_bytes(header + entry + png_bytes)
PYEOF
fi

echo "[build_icons] OK：$ICON_DIR/icon.icns（正式，ISS-045）"
