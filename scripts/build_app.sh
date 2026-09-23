#!/usr/bin/env bash
#
# ISS-009 切片 1 · 打包流水线
#
# 串联：
#   1. build_helper.sh（生产 helper 冻结到 resources/helper/fathom-helper/）
#   2. build_icons.sh（ISS-045 深度环正式 icon.icns）
#   3. cargo tauri build --bundles app,dmg（未签名）
#
# 输出：
#   apps/desktop/src-tauri/target/release/bundle/macos/Fathom.app
#   apps/desktop/src-tauri/target/release/bundle/dmg/Fathom_<version>_*.dmg（可能失败）
#   apps/desktop/src-tauri/target/release/bundle/checksums.txt
#
# 可选环境变量（ISS-041A，默认 unset 时行为与历史版本完全一致）：
#   FATHOM_TAURI_BUILD_ARGS : 追加到 `cargo tauri build` 的额外参数
#     （空格分隔）。发行 CI 用它传 `--config <json>` 把
#     bundle.createUpdaterArtifacts 覆盖为 false——首轮 draft Release
#     无 updater 签名私钥（DEC-022 + G10 用户决策门未过），保留 true 会让
#     tauri build 在生成 .sig 时因缺 TAURI_SIGNING_PRIVATE_KEY 失败。
#     恢复 updater 产物时移除该覆盖（见 .github/workflows/release.yml）。
#   FATHOM_BUILD_VENV : 非空时作为 build_helper.sh 的第一参数（构建 venv
#     根）传入。发行 CI 用它把 build_app.sh 内部对 build_helper.sh 的
#     第二次调用指向前置「冻结 helper」步骤已就绪的
#     $RUNNER_TEMP/fathom-venv——此前内部调用无参，会退回仅存在于本机
#     的缺省 apps/desktop/experiments/iss029/.venv-build（不在 git，
#     干净 runner 上必 BLOCKED exit 3，reviewer B-1 / ISS-041A repair1）。
#     unset 时内部调用保持无参（缺省路径），本地既有工作流零变化。
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
# ISS-041A repair1：FATHOM_BUILD_VENV 透传。非空时作为第一参数传给
# build_helper.sh（CI 已就绪的构建 venv），避免干净 runner 上无参调用
# 退回不在 git 的 iss029 缺省 venv 而 BLOCKED exit 3（reviewer B-1）；
# unset 时保持无参调用，缺省行为与历史版本逐字一致（本地工作流零变化）。
if [ -n "${FATHOM_BUILD_VENV:-}" ]; then
  bash "$ROOT/scripts/build_helper.sh" "$FATHOM_BUILD_VENV" 2>&1 | tee "$LOG_DIR/helper.log"
else
  bash "$ROOT/scripts/build_helper.sh" 2>&1 | tee "$LOG_DIR/helper.log"
fi
HELPER_RC=${PIPESTATUS[0]}
if [ "$HELPER_RC" -ne 0 ]; then
  echo "[build_app] FAIL：helper 冻结退出码 $HELPER_RC" >&2
  exit 1
fi

log_step "2/3 正式 icon.icns（ISS-045 深度环）"
bash "$ROOT/scripts/build_icons.sh" 2>&1 | tee "$LOG_DIR/icons.log"
ICONS_RC=${PIPESTATUS[0]}
if [ "$ICONS_RC" -ne 0 ]; then
  echo "[build_app] FAIL：icon 生成退出码 $ICONS_RC" >&2
  exit 1
fi

log_step "3/3 cargo tauri build --bundles app,dmg"
cd "$ROOT/apps/desktop/src-tauri"
# ISS-055：tauri-build 的 copy_resources 不清理 target 下旧映射产物（tauri-build
# 2.6.3 lib.rs copy_resources 无 remove_dir_all），resources 映射变更后旧布局
# 会在新目标路径留下同名目录，令 fs::copy 报 "Is a directory (os error 21)"
# 并使 build script 失败。构建前清掉两个 profile 的资源拷贝目录（tauri-build
# 会按当前 tauri.conf.json 重新生成，仅删副本，安全）。
rm -rf target/release/helper target/debug/helper
# ISS-041A：FATHOM_TAURI_BUILD_ARGS 不做引号展开以外的任何解析——上层
# 调用方（release workflow / 本地复跑）自行保证参数合法；默认空。
# shellcheck disable=SC2086
cargo tauri build --bundles app,dmg ${FATHOM_TAURI_BUILD_ARGS:-} 2>&1 | tee "$LOG_DIR/tauri-build.log" || OVERALL_RC=$?
cd "$ROOT"

# 即便 tauri build 整体失败，app 子产物可能已生成；分别检查
APP_BUNDLE="$BUNDLE_DIR/macos/Fathom.app"
# ISS-041A：DMG 文件名含发行版本（Fathom_0.3.0_aarch64.dmg 形态）。原先
# 硬编码 0.3.0 会在版本 bump 后 glob 落空、脚本恒报 dmg 失败；改为从单一
# 版本源 fathom/__init__.py 读取（与 check_version_consistency.sh 同源）。
APP_VERSION="$(grep -E '^__version__[[:space:]]*=[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"[[:space:]]*$' "$ROOT/fathom/__init__.py" | head -1 | sed -E 's/.*"([0-9]+\.[0-9]+\.[0-9]+)".*/\1')"
[ -n "$APP_VERSION" ] || { echo "[build_app] FAIL：读不出 fathom/__init__.py 的 __version__" >&2; exit 1; }
DMG_BUNDLE_GLOB="$(find "$BUNDLE_DIR/dmg" -maxdepth 1 -name "Fathom_${APP_VERSION}*.dmg" 2>/dev/null | head -1 || true)"

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
# ISS-055：cargo tauri build 自身失败时必须 exit 1（头注合同语义），否则
# target/ 里残留的旧 .app/.dmg 会让本脚本假报成功（曾掩盖 EISDIR 失败）。
if [ "$OVERALL_RC" -ne 0 ]; then
  echo "[build_app] FAIL：cargo tauri build 退出码 ${OVERALL_RC}，残留产物不可作为成功证据" >&2
  exit 1
fi
if [ "$APP_RC" -ne 0 ]; then
  exit 1
fi
if [ "$DMG_RC" -ne 0 ]; then
  exit 2
fi
exit 0
