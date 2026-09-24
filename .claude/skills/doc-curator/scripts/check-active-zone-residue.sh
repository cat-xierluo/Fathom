#!/usr/bin/env bash
# doc-curator check: active-zone-residue
# 扫描 docs/TASKS.md 活跃区与归档/待核实/暂缓区 同号 Task 条目;
# 同号项视为「活跃区残留已归档条目」或「合理交叉引用 / 编号撞号 / 多批次」,
# emit adaptive (PM 人工判别合理性,不阻断)。
#
# v0.5.1: 段标题与编号 pattern 改读 config active_zone_residue.zone_headings / residue_pattern。
#   默认 pattern 扩展为兼容 ISS-N + Task#N/TN/TNN（修复 CHANGELOG 0.4.0 已知盲区）。
#
# severity: adaptive / ok
# 输出 JSON 行 (与 scan.sh 兼容)
#
# 解析策略:
#   - 用 H2 段(## 活跃区 / ## 待核实区 / ## 暂缓区 / ## 归档区)划分四区
#   - 提取 H3 标题, 用 residue_pattern 提取编号
#   - 活跃区 vs 其它三区同号项集合差

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"
# 在条件/命令替换之前加载一次；读取失败不得被默认值或 process substitution 吞掉。
_cfg_ensure_flat

ENABLED="$(cfg_scalar active_zone_residue.enabled)"; ENABLED="${ENABLED:-true}"
if [ "$ENABLED" != "true" ]; then
  emit_result "soft" "active-zone-residue-disabled" \
    "active_zone_residue 未启用，未验证活跃区残留" \
    "需要该证据时设置 active_zone_residue.enabled: true"
  exit 0
fi

TASKS_REL="$(cfg_file_by_role active-tasks)"
if [ -z "$TASKS_REL" ] || [ ! -f "$REPO_ROOT/$TASKS_REL" ]; then
  emit_result "ok" "active-zone-residue" \
    "未配置 active-tasks 文件,跳过"
  exit 0
fi
tasks_file="$REPO_ROOT/$TASKS_REL"

# v0.5.1: 读 config 段标题 + 编号 pattern
# zone_headings: 四个段标题, 缺省时用中文默认值
H_ACTIVE="$(cfg_scalar active_zone_residue.zone_headings.active)";   H_ACTIVE="${H_ACTIVE:-## 活跃区}"
H_PENDING="$(cfg_scalar active_zone_residue.zone_headings.pending)"; H_PENDING="${H_PENDING:-## 待核实区}"
H_DEFERRED="$(cfg_scalar active_zone_residue.zone_headings.deferred)"; H_DEFERRED="${H_DEFERRED:-## 暂缓区}"
H_ARCHIVE="$(cfg_scalar active_zone_residue.zone_headings.archive)"; H_ARCHIVE="${H_ARCHIVE:-## 归档区}"
# residue_pattern: 描述编号形态（无 ^### 前缀，无捕获组要求）。
# 默认兼容 ISS-N + Task#N + Task TN/TNN。
# BSD awk 不支持 match() 第三参捕获组，故 pattern 直接匹配编号整体，
# 命中后用 substr(title, RSTART, RLENGTH) 取整段作为编号。
RESIDUE_PATTERN="$(cfg_scalar active_zone_residue.residue_pattern)"
RESIDUE_PATTERN="${RESIDUE_PATTERN:-(ISS-[0-9]+|Task[#　 ]+T?[0-9A-Za-z-]+)}"
if ! validate_ere "$RESIDUE_PATTERN"; then
  emit_result "hard" "config-regex-invalid" "active_zone_residue.residue_pattern 不是合法扩展正则" \
    "修正 config 后重试"
  exit 1
fi

# 使用 awk 划分 H2 段 + 提取 H3 标题
# 输出格式: zone<TAB>number<TAB>title
# 段标题与 pattern 通过环境变量传入(避免 awk 嵌套 config 解析)

export H_ACTIVE H_PENDING H_DEFERRED H_ARCHIVE RESIDUE_PATTERN
set +e
entries_output="$(awk '
  function trim(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return s }
  BEGIN {
    ha = ENVIRON["H_ACTIVE"]
    hp = ENVIRON["H_PENDING"]
    hd = ENVIRON["H_DEFERRED"]
    hc = ENVIRON["H_ARCHIVE"]
    pat = ENVIRON["RESIDUE_PATTERN"]
  }
  /^## / {
    h = trim($0)
    if (h == ha)      { zone = "active";   next }
    if (h == hp)      { zone = "pending";  next }
    if (h == hd)      { zone = "deferred"; next }
    if (h == hc)      { zone = "archive";  next }
    # 其它 H2 段忽略(关闭所有区域)
    zone = ""
    next
  }
  /^### / {
    if (zone == "") next
    title = trim($0)
    # 提取编号: match() 命中后取整段 (BSD/gawk 兼容, 不用第三参捕获组)
    n = ""
    if (match(title, pat)) {
      n = substr(title, RSTART, RLENGTH)
    }
    if (n != "") print zone "\t" n "\t" title
  }
' "$tasks_file")"
awk_rc=$?
set -e
if [ "$awk_rc" -ne 0 ]; then
  emit_result "hard" "active-zone-scan-error" "无法解析 $TASKS_REL" "检查文件权限与配置正则"
  exit 1
fi
entries=()
if [ -n "$entries_output" ]; then
  mapfile -t entries <<< "$entries_output"
fi

if [ "${#entries[@]}" -eq 0 ]; then
  emit_result "ok" "active-zone-residue" \
    "TASKS.md 无 H3 Task 条目,跳过"
  exit 0
fi

# 收集 active zone 与 non-active zone 编号映射
declare -A active_map
declare -A non_active_map
for entry in "${entries[@]}"; do
  zone=$(printf '%s' "$entry" | cut -f1)
  num=$(printf '%s' "$entry" | cut -f2)
  title=$(printf '%s' "$entry" | cut -f3-)
  if [ "$zone" = "active" ]; then
    active_map["$num"]="$title"
  else
    non_active_map["$num"]="${non_active_map[$num]:-}|$title"
  fi
done

residue_count=0
for num in "${!active_map[@]}"; do
  if [ -n "${non_active_map[$num]:-}" ]; then
    non_active_titles="${non_active_map[$num]#|}"
    emit_result "adaptive" "active-zone-residue" \
      "活跃区与归档/待核实/暂缓区同号: [活跃] ${active_map[$num]} || [其它] $non_active_titles" \
      "PM 人工判别: 真残留 / 合理交叉引用 / 编号撞号 / 多批次?"
    residue_count=$((residue_count + 1))
  fi
done

if [ "$residue_count" -eq 0 ]; then
  emit_result "ok" "active-zone-residue" \
    "TASKS.md 活跃区与归档/待核实/暂缓区无同号 Task 条目"
fi

finish_checker
