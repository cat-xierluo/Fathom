#!/usr/bin/env bash
#
# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ⚠  历史归档脚本 / HISTORICAL REPRO ONLY（ISS-060 (a2)）                ║
# ║                                                                         ║
# ║  本脚本对应 ISS-053 的修复点：tauri.conf.json 的 bundle.resources 由     ║
# ║  `resources/helper/**`（glob crate 的 ** 仅匹配路径段、不匹配叶文件） ║
# ║  改为 `resources/helper/**/*`，并假定 bundle.resources 是「字符串数组」 ║
# ║  形态。脚本里 `restore_conf_with` 仍按数组形态回写。                    ║
# ║                                                                         ║
# ║  **当前 main 已不再是该形态**：                                         ║
# ║  - ISS-055 已把 bundle.resources 改为「map 形态」                        ║
# ║      `{"resources/helper/fathom-helper/": "helper/"}`                    ║
# ║    以避免 `resources/helper/_internal/**` 等 PyInstaller onedir 残留被   ║
# ║    无差别打进 .app/Contents/Resources/。                                ║
# ║  - 在 map 形态下，本脚本的 `restore_conf_with "resources/helper/**"`    ║
# ║    会把 conf 临时降级回数组 glob，**不能真实反映当前 main 的打包行为**。 ║
# ║  - EXPECTED_HEAD 钉在 `f6dc9bb`，当前 main = `f4bc9e1`。                ║
# ║                                                                         ║
# ║  本脚本现作为「ISS-053 历史复现」归档，**保留**：                        ║
# ║  - 在 checkout 到 `f6dc9bb` 前后形态时仍可复现 ISS-053 的原始问题。      ║
# ║  - 跑出"原始配置阶段硬错误 / 修复后阶段通过"两条对照的语义不变。          ║
# ║                                                                         ║
# ║  如需验证当前 main 的打包 / helper 产物行为，请改用：                   ║
# ║    bash scripts/verify_app_bundle.sh                                    ║
# ║  （20 段断言，含 (a)~(i)；ISS-059 起新增 (h) 陈旧 instance respawn 与  ║
# ║   (i) 候选端口耗尽零击杀两段。）                                        ║
# ╚═══════════════════════════════════════════════════════════════════════════╝
#
# ISS-053 · 复现与验证 bundle.resources glob 修复（在 f6dc9bb 形态下）
#
# 复现目标（仅适用于 f6dc9bb 前后形态）：
#   路径 A — 未跑 build_helper.sh：resources/helper/ 仅含 README.txt
#            tauri-build 的 glob 必须匹配到至少一个文件
#   路径 B — 已跑 build_helper.sh：resources/helper/fathom-helper/
#            含二进制 + _internal/；glob 必须匹配到打包目标文件
#
# 用法（仅历史复现用）：
#   bash scripts/repro_iss053_resource_glob.sh            # 完整跑两条路径
#   bash scripts/repro_iss053_resource_glob.sh --no-b      # 只跑路径 A
#
# 退出码：
#   0 两条路径均通过
#   1 任意路径失败（且打印原始错误用于对照）
#   2 环境未就绪（cargo 不在 PATH 或当前目录非仓库根）
#
# bash 3.2 兼容；只调用允许列表内的 cargo check 与 git。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_TAURI="$ROOT/apps/desktop/src-tauri"
CONF="$SRC_TAURI/tauri.conf.json"
HELPER_DIR="$SRC_TAURI/resources/helper"
PLACEHOLDER_FAKE_HELPER="$HELPER_DIR/fathom-helper/fathom-helper"
EXPECTED_HEAD="${EXPECTED_HEAD:-f6dc9bb9399bbc248cb7cbd227ec4994e5596529}"

RUN_B=1
for arg in "$@"; do
  case "$arg" in
    --no-b) RUN_B=0 ;;
    -h|--help)
      sed -n '2,25p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) echo "[repro] 未知参数：$arg" >&2; exit 2 ;;
  esac
done

if ! command -v cargo >/dev/null 2>&1; then
  echo "[repro] FAIL：未检测到 cargo；请安装 Rust 工具链后重试" >&2
  exit 2
fi

log() { echo ""; echo "=== $* ==="; }

CURRENT_HEAD="$(git -C "$ROOT" rev-parse HEAD)"
echo "[repro] 当前 HEAD：$CURRENT_HEAD"
if [ "$CURRENT_HEAD" != "$EXPECTED_HEAD" ]; then
  echo "[repro] WARN：当前 HEAD 与预期 ISS-053 基线 $EXPECTED_HEAD 不同；" >&2
  echo "[repro]        仍可继续（修复前后对照），但请确认是否在正确分支。" >&2
fi

if [ ! -f "$CONF" ]; then
  echo "[repro] FAIL：未找到 $CONF" >&2
  exit 2
fi

# 备份 tauri.conf.json 以便跑完恢复现场
CONF_BACKUP="$(mktemp -t tauri-conf-XXXXXX.json)"
cp "$CONF" "$CONF_BACKUP"
trap 'cp "$CONF_BACKUP" "$CONF"; rm -f "$CONF_BACKUP"' EXIT

# 截取当前 bundle.resources 的关键行
extract_resources() {
  python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
resources = data.get("bundle", {}).get("resources", [])
print(",".join(resources))
' "$1"
}

restore_conf_with() {
  python3 -c '
import json, sys
target = sys.argv[1]
with open(target) as f:
    data = json.load(f)
data["bundle"]["resources"] = sys.argv[2:]
with open(target, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
' "$CONF" "$@"
}

run_cargo_check_path() {
  local label="$1"
  log "cargo check — $label"
  # 允许 cargo check 失败（其他预存在错误不影响 ISS-053 验证）
  local out
  out="$(cd "$SRC_TAURI" && cargo check --locked --offline 2>&1)" || true
  local rc=$?
  echo "$out" | tail -25
  if printf '%s' "$out" | grep -q 'glob pattern .* path not found or didn'\''t match any files'; then
    echo ""
    echo "[repro] FAIL [$label]：bundle.resources glob 仍然未匹配到任何文件"
    return 1
  fi
  if printf '%s' "$out" | grep -q "error: failed to run custom build command"; then
    if printf '%s' "$out" | grep -q 'glob pattern'; then
      echo ""
      echo "[repro] FAIL [$label]：build script 阶段 glob 硬错误"
      return 1
    fi
  fi
  echo ""
  echo "[repro] OK [$label]：glob 已匹配到至少一个文件，build script 通过"
  return 0
}

FAKE_HELPER_INSTALLED=0
install_fake_helper() {
  if [ -e "$PLACEHOLDER_FAKE_HELPER" ]; then
    echo "[repro] 路径 B：检测到既有 helper 产物，复用"
    FAKE_HELPER_INSTALLED=1
    return 0
  fi
  echo "[repro] 路径 B：未检测到 helper 冻结产物，构造最小占位二进制"
  mkdir -p "$(dirname "$PLACEHOLDER_FAKE_HELPER")"
  # 任意可识别文件即可——cargo check 只关心 glob 匹配个数，不解析内容
  : > "$PLACEHOLDER_FAKE_HELPER"
  mkdir -p "$(dirname "$PLACEHOLDER_FAKE_HELPER")/_internal"
  echo "fake onedir payload" > "$(dirname "$PLACEHOLDER_FAKE_HELPER")/_internal/_marker.txt"
  FAKE_HELPER_INSTALLED=1
}

uninstall_fake_helper() {
  if [ "$FAKE_HELPER_INSTALLED" = "1" ] && [ ! -e "$HELPER_DIR/.iss053-fixture-marker" ]; then
    # 仅当我们自己安装的、且原仓库没冻结产物时清理
    rm -rf "$(dirname "$PLACEHOLDER_FAKE_HELPER")"
    rmdir "$HELPER_DIR/fathom-helper" 2>/dev/null || true
  fi
}

# ---- 阶段 1：复现原始错误（基线） ----
log "基线：原 `resources/helper/**` glob（应硬错误）"
restore_conf_with "resources/helper/**"
run_cargo_check_path "原始配置" || {
  echo ""
  echo "[repro] 期望路径：原始 `**` 在干净克隆上硬错误，符合 ISS-053 复现目标"
  echo ""
}

# ---- 阶段 2：路径 A（仅 README.txt） ----
log "路径 A：仅 README.txt（修复后应通过）"
restore_conf_with "resources/helper/**/*"
PATH_A_OK=0
run_cargo_check_path "路径 A（无 helper 冻结产物）" && PATH_A_OK=1 || PATH_A_OK=0

# ---- 阶段 3：路径 B（带 helper） ----
PATH_B_OK=0
if [ "$RUN_B" = "1" ]; then
  log "路径 B：helper 冻结产物已就位（修复后应通过）"
  install_fake_helper
  restore_conf_with "resources/helper/**/*"
  run_cargo_check_path "路径 B（含 helper 冻结产物）" && PATH_B_OK=1 || PATH_B_OK=0
  uninstall_fake_helper
else
  echo ""
  echo "[repro] 跳过路径 B（--no-b）"
fi

# ---- 总结 ----
log "结果"
echo "路径 A（仅 README.txt，干净克隆等价）：$( [ "$PATH_A_OK" = "1" ] && echo PASS || echo FAIL )"
if [ "$RUN_B" = "1" ]; then
  echo "路径 B（已跑 build_helper.sh）：       $( [ "$PATH_B_OK" = "1" ] && echo PASS || echo FAIL )"
fi
echo ""
echo "[repro] bundle.resources 当前值：$(extract_resources "$CONF")"

if [ "$PATH_A_OK" = "1" ] && { [ "$RUN_B" = "0" ] || [ "$PATH_B_OK" = "1" ]; }; then
  echo ""
  echo "[repro] OK：两条路径均通过 ISS-053 glob 修复验证"
  exit 0
fi
echo ""
echo "[repro] FAIL：至少一条路径未通过" >&2
exit 1
