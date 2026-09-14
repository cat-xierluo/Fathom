#!/usr/bin/env bash
#
# ISS-009 切片 1 · 打包流水线
#
# 串联：
#   1. build_helper.sh（生产 helper 冻结到 resources/helper/fathom-helper/）
#   2. build_icons.sh（占位 icon.icns；NOT_VERIFIED）
#   3. cargo tauri build --bundles app,dmg（未签名）
#
# 输出：
#   apps/desktop/src-tauri/target/release/bundle/macos/Fathom.app
#   apps/desktop/src-tauri/target/release/bundle/dmg/Fathom_0.3.0_*.dmg（可能失败）
#   apps/desktop/src-tauri/target/release/bundle/checksums.txt
#
# 失败语义：
#   - helper 冻结失败 → 整体退出 1
#   - icon 生成失败 → 整体退出 1
#   - cargo tauri build 整体失败 → 整体退出 1
#   - app 成功但 dmg 失败 → 退出 2，但 app 产物保留并在 RESULT 中标注
#
# bash 3.2 兼容。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_DIR="$ROOT/apps/desktop/src-tauri/target/release/bundle"
LOG_DIR="$ROOT/apps/desktop/src-tauri/build-logs"
mkdir -p "$LOG_DIR"

OVERALL_RC=0
APP_RC=0
DMG_RC=0

log_step() { echo ""; echo "=== $* ==="; }

log_step "1/3 helper 冻结"
bash "$ROOT/scripts/build_helper.sh" 2>&1 | tee "$LOG_DIR/helper.log"
HELPER_RC=${PIPESTATUS[0]}
if [ "$HELPER_RC" -ne 0 ]; then
  echo "[build_app] FAIL：helper 冻结退出码 $HELPER_RC" >&2
  exit 1
fi

log_step "2/3 占位 icon.icns（NOT_VERIFIED）"
bash "$ROOT/scripts/build_icons.sh" 2>&1 | tee "$LOG_DIR/icons.log"
ICONS_RC=${PIPESTATUS[0]}
if [ "$ICONS_RC" -ne 0 ]; then
  echo "[build_app] FAIL：icon 生成退出码 $ICONS_RC" >&2
  exit 1
fi

log_step "3/3 cargo tauri build --bundles app,dmg"
cd "$ROOT/apps/desktop/src-tauri"
cargo tauri build --bundles app,dmg 2>&1 | tee "$LOG_DIR/tauri-build.log" || OVERALL_RC=$?
cd "$ROOT"

# 即便 tauri build 整体失败，app 子产物可能已生成；分别检查
APP_BUNDLE="$BUNDLE_DIR/macos/Fathom.app"
DMG_BUNDLE_GLOB="$(find "$BUNDLE_DIR/dmg" -maxdepth 1 -name 'Fathom_0.3.0*.dmg' 2>/dev/null | head -1 || true)"

if [ -d "$APP_BUNDLE" ]; then
  APP_RC=0
  echo "[build_app] app 产物存在：$APP_BUNDLE"
else
  APP_RC=1
fi

if [ -n "$DMG_BUNDLE_GLOB" ] && [ -f "$DMG_BUNDLE_GLOB" ]; then
  DMG_RC=0
  echo "[build_app] dmg 产物存在：$DMG_BUNDLE_GLOB"
else
  DMG_RC=1
fi

# 写 checksums.txt（只对存在的产物）
CHECKSUMS="$BUNDLE_DIR/checksums.txt"
{
  echo "# ISS-009 切片 1 · 产物 SHA256"
  echo "# 生成时间：$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# 宿主架构：$(uname -m)"
  if [ -d "$APP_BUNDLE" ]; then
    echo "# --- .app 内容指纹 ---"
    find "$APP_BUNDLE" -type f -print0 | xargs -0 shasum -a 256 2>/dev/null | head -200
    echo "# --- .app 总指纹 ---"
    find "$APP_BUNDLE" -type f -print0 | xargs -0 shasum -a 256 2>/dev/null \
      | shasum -a 256 | awk '{print $1"  Fathom.app/"}'
  fi
  if [ -n "$DMG_BUNDLE_GLOB" ] && [ -f "$DMG_BUNDLE_GLOB" ]; then
    shasum -a 256 "$DMG_BUNDLE_GLOB"
  fi
  if [ -f "$ROOT/apps/desktop/src-tauri/resources/helper/fathom-helper/fathom-helper" ]; then
    echo "# --- helper 冻结产物 ---"
    shasum -a 256 "$ROOT/apps/desktop/src-tauri/resources/helper/fathom-helper/fathom-helper/fathom-helper"
  fi
} > "$CHECKSUMS"
echo "[build_app] checksums: $CHECKSUMS"

echo ""
echo "=========================================="
echo "[build_app] tauri build 退出码：$OVERALL_RC"
echo "[build_app] app 产物：$APP_BUNDLE (rc=$APP_RC)"
echo "[build_app] dmg 产物：${DMG_BUNDLE_GLOB:-(none)} (rc=$DMG_RC)"
echo "=========================================="

# 退出码语义：
#   - 整体 0：app 至少成功
#   - 整体 2：app 成功但 dmg 失败（保持 .app 可用）
#   - 整体 1：app 失败
if [ "$APP_RC" -ne 0 ]; then
  exit 1
fi
if [ "$DMG_RC" -ne 0 ]; then
  exit 2
fi
exit 0
