#!/usr/bin/env bash
# docs/DECISIONS.md 体检：ISS 归档条目升序、决策标题 ID 唯一且连续。
# 文件路径从 config 读取（v0.2.0）。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"
# 在条件/命令替换之前加载一次；读取失败不得被默认值或 process substitution 吞掉。
_cfg_ensure_flat

DECISIONS_REL="$(cfg_file_by_role decision-log)"
DECISIONS_FILE="$REPO_ROOT/${DECISIONS_REL:-docs/DECISIONS.md}"
if [ ! -f "$DECISIONS_FILE" ]; then
  if cfg_rule_enabled hard_rules decisions-id-unique || \
     cfg_rule_enabled hard_rules decisions-dec-numbering-continuous || \
     cfg_rule_enabled hard_rules decisions-iss-archive-ascending; then
    emit_result "hard" "decisions-file-missing" \
      "配置启用了决策硬规则，但 ${DECISIONS_REL:-docs/DECISIONS.md} 不存在" \
      "修正 decision-log 路径或恢复该文件"
    exit 1
  fi
  emit_result "soft" "decisions-file-missing" "${DECISIONS_REL:-docs/DECISIONS.md} 不存在"
  exit 0
fi

DEC_HEADING_PATTERN="$(cfg_scalar decision_log.heading_pattern)"
[ -n "$DEC_HEADING_PATTERN" ] || DEC_HEADING_PATTERN='^#{2,6}[[:space:]]+.*DEC-[0-9]+'
DEC_ID_PATTERN="$(cfg_scalar decision_log.id_pattern)"
DEC_ID_PATTERN="${DEC_ID_PATTERN:-DEC-[0-9]+}"
if ! validate_ere "$DEC_HEADING_PATTERN" || ! validate_ere "$DEC_ID_PATTERN"; then
  emit_result "hard" "config-regex-invalid" \
    "decision_log.heading_pattern 或 id_pattern 不是合法扩展正则" \
    "修正决策标题配置"
  exit 1
fi

# 只从决策标题建立 ID 集合；正文引用不得填平跳号或掩盖重复标题。
declare -A dec_counts=() dec_lines=()
decision_heading_count=0
while IFS=: read -r line_no heading; do
  [ -n "$line_no" ] || continue
  id="$(printf '%s\n' "$heading" | grep -oE "$DEC_ID_PATTERN" | head -1 || true)"
  [ -n "$id" ] || continue
  decision_heading_count=$((decision_heading_count + 1))
  dec_counts["$id"]=$(( ${dec_counts[$id]:-0} + 1 ))
  dec_lines["$id"]="${dec_lines[$id]:+${dec_lines[$id]},}$line_no"
done < <(grep -nE "$DEC_HEADING_PATTERN" "$DECISIONS_FILE" || true)

if cfg_rule_enabled hard_rules decisions-id-unique; then
  duplicate_count=0
  for id in "${!dec_counts[@]}"; do
    if [ "${dec_counts[$id]}" -gt 1 ]; then
      emit_result "hard" "decisions-id-duplicate" \
        "$id 出现 ${dec_counts[$id]} 个决策标题（行 ${dec_lines[$id]}）" \
        "保留一个权威 ID；为其它真实决策分配新编号并修正引用"
      duplicate_count=$((duplicate_count + 1))
    fi
  done
  if [ "$duplicate_count" -eq 0 ]; then
    emit_result "ok" "decisions-id-unique" \
      "$decision_heading_count 个决策标题 ID 均唯一"
  fi
fi

# 1. ISS 归档条目升序（硬性）
if cfg_rule_enabled hard_rules decisions-iss-archive-ascending; then
  iss_numbers=$(grep -oE 'ISS-[0-9]+' "$DECISIONS_FILE" | grep -oE '[0-9]+' | sort -n -u || true)
  if [ -n "$iss_numbers" ]; then
    in_archive=$(awk '
      /^## ISS 任务归档/ { in_arc=1; next }
      /^## / && in_arc { in_arc=0 }
      in_arc && /ISS-[0-9]+/ { print }
    ' "$DECISIONS_FILE" | grep -oE 'ISS-[0-9]+' | grep -oE '[0-9]+' || true)
    if [ -n "$in_archive" ]; then
      sorted=$(printf '%s\n' "$in_archive" | sort -n)
      if [ "$in_archive" = "$sorted" ]; then
        emit_result "ok" "decisions-iss-archive-ascending" "ISS 归档条目按编号升序"
      else
        emit_result "hard" "decisions-iss-archive-ascending" "ISS 归档条目未按升序排列" \
          "按 ISS-XXX 编号重新排序归档区条目"
      fi
    else
      emit_result "ok" "decisions-iss-archive-ascending" "归档区暂无条目，跳过"
    fi
  else
    emit_result "ok" "decisions-iss-archive-ascending" "无 ISS 编号"
  fi
fi

# 2. DEC 编号连续（硬性）
if cfg_rule_enabled hard_rules decisions-dec-numbering-continuous; then
  dec_numbers=""
  for id in "${!dec_counts[@]}"; do
    number="$(printf '%s\n' "$id" | grep -oE '[0-9]+$' || true)"
    [ -z "$number" ] || dec_numbers="${dec_numbers}${dec_numbers:+$'\n'}${number}"
  done
  dec_numbers="$(printf '%s\n' "$dec_numbers" | awk 'NF' | sort -n -u)"
  if [ -n "$dec_numbers" ]; then
    gaps=$(printf '%s\n' "$dec_numbers" | awk '
      NR>1 && $1 != prev+1 { print prev "→" $1 }
      { prev=$1 }
    ')
    if [ -z "$gaps" ]; then
      emit_result "ok" "decisions-dec-numbering-continuous" "DEC 编号连续无跳号"
    else
      emit_result "hard" "decisions-dec-numbering-continuous" "DEC 编号跳号：$gaps" \
        "补齐缺失的 DEC-XXX 条目或修正编号"
    fi
  else
    emit_result "ok" "decisions-dec-numbering-continuous" "无 DEC 编号"
  fi
fi

# 3. 总行数（自适应）
line_count=$(wc -l < "$DECISIONS_FILE" | tr -d ' ')
if line_threshold="$(adaptive_threshold decisions-line-count line_count "${DECISIONS_REL:-docs/DECISIONS.md}")"; then
  if awk -v current="$line_count" -v threshold="$line_threshold" 'BEGIN { exit(current > threshold ? 0 : 1) }'; then
    emit_result "adaptive" "decisions-line-count" "总行数 ${line_count}，超过阈值 $line_threshold" \
      "归档过期细节或重新初始化基线"
  else
    emit_result "ok" "decisions-line-count" "总行数 ${line_count}，未超过阈值 $line_threshold"
  fi
else
  emit_result "soft" "decisions-line-count-baseline-missing" \
    "未找到 ${DECISIONS_REL:-docs/DECISIONS.md} 的 line_count 基线" \
    "运行 scan.sh --init-baseline"
fi

finish_checker
