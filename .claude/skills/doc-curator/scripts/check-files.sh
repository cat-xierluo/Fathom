#!/usr/bin/env bash
# 通用文件行数体检：遍历 config files 段（排除已有专门 check 的 active-tasks /
# decision-log）报行数，并对 CHANGELOG / README 做软提示。
# 文件清单从 config 读取（v0.2.0），不再写死 DESIGN/ARCHITECTURE——避免本项目误报。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"
# 在条件/命令替换之前加载一次；读取失败不得被默认值或 process substitution 吞掉。
_cfg_ensure_flat

TASKS_REL="$(cfg_file_by_role active-tasks)"
DECISIONS_REL="$(cfg_file_by_role decision-log)"

# 遍历 files 段，报行数（adaptive）；不存在的报 soft。
while IFS= read -r rel; do
  if [ -z "$rel" ]; then continue; fi
  if [ "$rel" = "$TASKS_REL" ] || [ "$rel" = "$DECISIONS_REL" ]; then
    continue  # 由 check-tasks / check-decisions 专门查
  fi
  file="$REPO_ROOT/$rel"
  rule_id="$(basename "$rel" .md | tr 'A-Z-' 'a-z-')-line-count"
  if [ -f "$file" ]; then
    line_count=$(wc -l < "$file" | tr -d ' ')
    if line_threshold="$(adaptive_threshold "$rule_id" line_count "$rel")"; then
      if awk -v current="$line_count" -v threshold="$line_threshold" 'BEGIN { exit(current > threshold ? 0 : 1) }'; then
        emit_result "adaptive" "$rule_id" "$rel 总行数 ${line_count}，超过阈值 $line_threshold" \
          "归档历史细节或重新初始化基线"
      else
        emit_result "ok" "$rule_id" "$rel 总行数 ${line_count}，未超过阈值 $line_threshold"
      fi
    else
      emit_result "soft" "$rule_id-baseline-missing" "$rel 缺少 line_count 基线" \
        "运行 scan.sh --init-baseline"
    fi
  else
    emit_result "soft" "$rule_id" "$rel 不存在"
  fi
done < <(cfg_file_paths)

# 软提示：CHANGELOG.md 最近 release entry
CHANGELOG_REL="$(cfg_file_by_role changelog)"
CHANGELOG="$REPO_ROOT/${CHANGELOG_REL:-CHANGELOG.md}"
if [ -f "$CHANGELOG" ]; then
  if grep -qE '^##.*[0-9]{4}-[0-9]{2}-[0-9]{2}|^## v[0-9]' "$CHANGELOG"; then
    emit_result "ok" "changelog-recent-release-entry" \
      "CHANGELOG.md 含最近 release entry"
  else
    emit_result "soft" "changelog-recent-release-entry" \
      "CHANGELOG.md 缺少日期或版本号段标题" \
      "在 CHANGELOG.md 顶部补充最近 release 段"
  fi
fi

finish_checker

# 软提示：README.md 当前状态段
README_REL="$(cfg_file_by_role readme)"
README="$REPO_ROOT/${README_REL:-README.md}"
if [ -f "$README" ]; then
  if grep -qE '当前状态|Current Status' "$README"; then
    emit_result "ok" "readme-current-status" \
      "README.md 含「当前状态」段"
  else
    emit_result "soft" "readme-current-status" \
      "README.md 缺少「当前状态」段" \
      "在 README.md 补充「当前状态」或「Current Status」段"
  fi
fi
