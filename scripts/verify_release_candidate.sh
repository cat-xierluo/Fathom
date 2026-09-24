#!/usr/bin/env bash
#
# ISS-041A · 发行候选校验（下载侧可复跑，fail-closed）
#
# 用途：对本地 DMG 文件（或从 GitHub Release 资产下载后）核对
# sha256、DMG 结构与版本一致性。与 verify_app_bundle.sh 的分工：
# 后者面向构建机上的 .app 深度行为验证（启动/让位/退出）；本脚本
# 只面向「发行产物本身」，只读挂载、不启动任何进程、不触碰真实
# HOME 运行根，可在下载侧机器安全复跑。
#
# 用法：
#   bash scripts/verify_release_candidate.sh --dmg <path.dmg> \
#        [--checksums <checksums.txt>] [--tag vX.Y.Z]
#   bash scripts/verify_release_candidate.sh --release <tag> \
#        [--repo owner/name] [--dir <下载目录>]
#
# 模式说明：
#   --dmg       校验本地 DMG。--checksums 缺省时取 DMG 同目录的
#               checksums.txt；--tag 缺省时从 DMG 文件名解析期望版本。
#   --release   用 gh 从指定 Release（含 draft）下载 checksums.txt 与
#               *.dmg 到 --dir（缺省 mktemp 目录，保留供人工复查），
#               再走同一校验链。需要 GH_TOKEN 具备仓库读权限。
#
# 断言清单（任一失败即退出 1）：
#   1. checksums.txt 存在、格式为标准 `shasum -a 256` 输出
#      （<64 位 hex>  <文件名>），且恰好覆盖 DMG 文件名；
#   2. DMG 实测 sha256 与 checksums.txt 记录一致（等价 shasum -c，
#      显式比对便于打印差异）；
#   3. DMG 挂载后：Fathom.app 目录、Applications 符号链接、
#      .background/dmg-background.png（>1000 字节）在位——与
#      verify_app_bundle.sh (j) 段的 DMG 合同同口径（未签名放行
#      说明由背景图承载）；
#   4. 版本一致：期望版本（--tag 去前缀，或 DMG 文件名解析）==
#      Fathom.app/Contents/Info.plist CFBundleShortVersionString ==
#      DMG 文件名中的版本段；若脚本随仓库分发且能读到
#      fathom/__init__.py，还必须 == 单一版本源 __version__；
#   5. 主二进制为纯 arm64 Mach-O（首轮 arm64-only 合同；file(1) 断言）。
#
# 退出码：
#   0  全部通过
#   1  任一断言失败
#   2  用法错误
#   3  阻塞（文件不存在 / gh 下载失败 / 依赖工具缺失）
#
# 依赖：bash 3.2+、shasum、hdiutil、/usr/libexec/PlistBuddy、file、
#       find、grep/sed/awk；--release 模式另需 gh（仅下载，不发布）。
#
# bash 3.2 兼容。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE=""
DMG_PATH=""
CHECKSUMS_PATH=""
EXPECT_TAG=""
RELEASE_TAG_ARG=""
RELEASE_REPO=""
DOWNLOAD_DIR=""
KEEP_DIR=no

usage() {
  awk 'NR>1 && /^#/{sub(/^# ?/, ""); print; next} NR>1 && !/^#/{exit}' "${BASH_SOURCE[0]}" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dmg) MODE=dmg; DMG_PATH="${2:-}"; shift 2 ;;
    --checksums) CHECKSUMS_PATH="${2:-}"; shift 2 ;;
    --tag) EXPECT_TAG="${2:-}"; shift 2 ;;
    --release) MODE=release; RELEASE_TAG_ARG="${2:-}"; shift 2 ;;
    --repo) RELEASE_REPO="${2:-}"; shift 2 ;;
    --dir) DOWNLOAD_DIR="${2:-}"; shift 2 ;;
    --keep) KEEP_DIR=yes; shift ;;
    -h|--help) usage ;;
    *) echo "[verify-rc] 未知参数：$1" >&2; usage ;;
  esac
done

fail() { echo "[verify-rc] FAIL：$1" >&2; exit 1; }
blocked() { echo "[verify-rc] BLOCKED：$1" >&2; exit 3; }

# ---- 模式与输入归一 ----
[ -n "$MODE" ] || usage
if [ "$MODE" = "dmg" ]; then
  [ -n "$DMG_PATH" ] || usage
  [ -f "$DMG_PATH" ] || blocked "DMG 不存在：$DMG_PATH"
  [ -z "$RELEASE_TAG_ARG" ] || usage
elif [ "$MODE" = "release" ]; then
  [ -n "$RELEASE_TAG_ARG" ] || usage
  [ -z "$DMG_PATH" ] || usage
  command -v gh >/dev/null 2>&1 || blocked "--release 模式需要 gh CLI"
  if [ -z "$DOWNLOAD_DIR" ]; then
    DOWNLOAD_DIR="$(mktemp -d -t fathom-verify-rc-XXXXXX)"
    KEEP_DIR=yes   # mktemp 目录默认保留，便于人工复查下载资产
  fi
  mkdir -p "$DOWNLOAD_DIR"
  REPO_FLAG=""
  [ -n "$RELEASE_REPO" ] && REPO_FLAG="--repo $RELEASE_REPO"
  # shellcheck disable=SC2086
  if ! gh release download "$RELEASE_TAG_ARG" $REPO_FLAG \
        --pattern 'checksums.txt' --pattern '*.dmg' \
        --dir "$DOWNLOAD_DIR" --clobber; then
    blocked "gh release download 失败（tag ${RELEASE_TAG_ARG}；draft 需要 GH_TOKEN 读权限）"
  fi
  DMG_PATH="$(find "$DOWNLOAD_DIR" -maxdepth 1 -name '*.dmg' | head -1)"
  [ -n "$DMG_PATH" ] && [ -f "$DMG_PATH" ] || blocked "下载目录未找到 DMG：$DOWNLOAD_DIR"
  CHECKSUMS_PATH="$DOWNLOAD_DIR/checksums.txt"
  [ -z "$EXPECT_TAG" ] && EXPECT_TAG="$RELEASE_TAG_ARG"
else
  usage
fi
DMG_PATH="$(cd "$(dirname "$DMG_PATH")" && pwd)/$(basename "$DMG_PATH")"
DMG_NAME="$(basename "$DMG_PATH")"
[ -n "$CHECKSUMS_PATH" ] || CHECKSUMS_PATH="$(dirname "$DMG_PATH")/checksums.txt"
[ -f "$CHECKSUMS_PATH" ] || blocked "checksums.txt 不存在：${CHECKSUMS_PATH}（--release 资产应包含它）"

# ---- 期望版本 ----
if [ -n "$EXPECT_TAG" ]; then
  case "$EXPECT_TAG" in
    v*) EXPECT_VERSION="${EXPECT_TAG#v}" ;;
    *) fail "--tag 必须带 v 前缀（收到 '$EXPECT_TAG'）" ;;
  esac
  printf '%s' "$EXPECT_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
    || fail "tag '$EXPECT_TAG' 去前缀后 '$EXPECT_VERSION' 非严格 X.Y.Z"
else
  EXPECT_VERSION="$(printf '%s' "$DMG_NAME" | sed -nE 's/^Fathom_([0-9]+\.[0-9]+\.[0-9]+)_.+\.dmg$/\1/p')"
  [ -n "$EXPECT_VERSION" ] || fail "无法从 DMG 文件名解析版本（期望 Fathom_X.Y.Z_<arch>.dmg 形态，收到 '$DMG_NAME'）；或用 --tag 显式给定"
  echo "[verify-rc] 未给 --tag，采用文件名版本：$EXPECT_VERSION"
fi

PASS=0
ok() { PASS=$((PASS + 1)); echo "[verify-rc] PASS：$1"; }

# ---- 0. 依赖工具 ----
command -v shasum >/dev/null 2>&1 || blocked "缺 shasum"
command -v hdiutil >/dev/null 2>&1 || blocked "缺 hdiutil"
command -v file >/dev/null 2>&1 || blocked "缺 file"

# ---- 1. checksums.txt 格式与覆盖 ----
# v0.3.1 起 Release 含 updater 产物，checksums 允许 1..N 条：每条格式
# 合法（64 位 hex + 文件名两列），且必须覆盖 DMG（核心资产）。
CS_COUNT="$(grep -cvE '^(#|$)' "$CHECKSUMS_PATH" || true)"
[ "$CS_COUNT" -ge 1 ] || fail "checksums.txt 无记录（非注释行）"
while IFS= read -r CS_LINE; do
  [ -n "$CS_LINE" ] || continue
  CS_SHA="$(printf '%s' "$CS_LINE" | awk '{print $1}')"
  CS_NAME="$(printf '%s' "$CS_LINE" | awk '{print $2}')"
  printf '%s' "$CS_SHA" | grep -Eq '^[0-9a-f]{64}$' || fail "checksums.txt 首列不是 64 位小写 hex：$CS_SHA"
  [ -n "$CS_NAME" ] || fail "checksums.txt 记录缺文件名：$CS_LINE"
done < <(grep -vE '^(#|$)' "$CHECKSUMS_PATH")
grep -vE '^(#|$)' "$CHECKSUMS_PATH" | awk '{print $2}' | grep -qx "$DMG_NAME" \
  || fail "checksums.txt 未覆盖 DMG $DMG_NAME"
ok "checksums.txt 格式合法（${CS_COUNT} 条）且覆盖 $DMG_NAME"

# ---- 2. sha256 实测比对（按 DMG 文件名取对应记录，多资产安全） ----
CS_SHA="$(awk -v n="$DMG_NAME" '$2==n{print $1}' "$CHECKSUMS_PATH")"
[ -n "$CS_SHA" ] || fail "checksums.txt 中未找到 $DMG_NAME 的记录"
ACTUAL_SHA="$(shasum -a 256 "$DMG_PATH" | awk '{print $1}')"
[ "$ACTUAL_SHA" = "$CS_SHA" ] \
  || fail "sha256 不一致：实测 $ACTUAL_SHA != 记录 $CS_SHA"
ok "sha256 一致：$ACTUAL_SHA"

# ---- 3. 挂载与结构断言 ----
# 与 verify_app_bundle.sh (j) 段同做法：mktemp 空目录直接作为 -mountpoint。
MNT="$(mktemp -d -t fathom-rc-mnt-XXXXXX)"
if ! hdiutil attach -nobrowse -mountpoint "$MNT" "$DMG_PATH" >/dev/null 2>&1; then
  rm -rf "$MNT" 2>/dev/null || true
  fail "DMG 挂载失败：$DMG_PATH"
fi
DETACH_OK=no
detach_dmg() { hdiutil detach "$MNT" -quiet >/dev/null 2>&1 && DETACH_OK=yes || true; }

BG="$MNT/.background/dmg-background.png"
BG_SIZE="$(stat -f '%z' "$BG" 2>/dev/null || echo 0)"
if [ -d "$MNT/Fathom.app" ] && [ -L "$MNT/Applications" ] && [ "$BG_SIZE" -gt 1000 ]; then
  ok "DMG 结构：Fathom.app + Applications 链接 + 背景图（${BG_SIZE}B）在位"
else
  detach_dmg
  fail "DMG 内容缺失：app=$([ -d "$MNT/Fathom.app" ] && echo ok || echo missing) link=$([ -L "$MNT/Applications" ] && echo ok || echo missing) background=${BG_SIZE}B"
fi

# ---- 4. 版本一致 ----
PLIST_VER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$MNT/Fathom.app/Contents/Info.plist" 2>/dev/null || echo unknown)"
case "$DMG_NAME" in
  "Fathom_${EXPECT_VERSION}"_*) FILENAME_OK=yes ;;
  *) FILENAME_OK=no ;;
esac
if [ "$PLIST_VER" != "$EXPECT_VERSION" ] || [ "$FILENAME_OK" != "yes" ]; then
  detach_dmg
  fail "版本不一致：期望 ${EXPECT_VERSION}，Info.plist=${PLIST_VER}，文件名携带=${FILENAME_OK}（${DMG_NAME}）"
fi
ok "版本一致：tag/文件名/Info.plist == $EXPECT_VERSION"

PY_INIT="$REPO_ROOT/fathom/__init__.py"
if [ -f "$PY_INIT" ]; then
  SRC_VER="$(grep -E '^__version__[[:space:]]*=[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"[[:space:]]*$' "$PY_INIT" | head -1 | sed -E 's/.*"([0-9]+\.[0-9]+\.[0-9]+)".*/\1/')"
  if [ -n "$SRC_VER" ] && [ "$SRC_VER" != "$EXPECT_VERSION" ]; then
    detach_dmg
    fail "随仓库分发的单一版本源 __version__=$SRC_VER != 候选版本 ${EXPECT_VERSION}（下载侧复跑可用 --dir 指向仓库外副本绕开此比对，但应先怀疑产物版本错误）"
  fi
  [ -n "$SRC_VER" ] && ok "单一版本源 __version__ == $EXPECT_VERSION"
fi

# ---- 5. 主二进制纯 arm64 ----
# 二进制名来自 Cargo [package] name（fathom-desktop），非 productName：
# 2026-09-23 对真实 DMG（Sep 19 基线）实测 Contents/MacOS/ 下为
# fathom-desktop，file(1) 报 Mach-O 64-bit executable arm64。
MAIN_BIN="$MNT/Fathom.app/Contents/MacOS/fathom-desktop"
if [ ! -f "$MAIN_BIN" ]; then
  detach_dmg
  fail "主二进制缺失：$MAIN_BIN"
fi
FILE_OUT="$(file "$MAIN_BIN")"
if printf '%s' "$FILE_OUT" | grep -q "arm64" && ! printf '%s' "$FILE_OUT" | grep -q "x86_64"; then
  ok "主二进制为纯 arm64 Mach-O（首轮 arm64-only 合同）"
else
  detach_dmg
  fail "主二进制架构断言失败：$FILE_OUT"
fi

detach_dmg
[ "$DETACH_OK" = "yes" ] || echo "[verify-rc] WARN：hdiutil detach 未确认成功，请手动检查挂载点" >&2
rm -rf "$MNT" 2>/dev/null || true

echo "[verify-rc] 发行候选校验通过：${DMG_NAME}（${PASS} 项断言，期望版本 ${EXPECT_VERSION}）"
if [ "$MODE" = "release" ] && [ "$KEEP_DIR" = "yes" ]; then
  echo "[verify-rc] 下载资产保留在：$DOWNLOAD_DIR"
fi
exit 0
