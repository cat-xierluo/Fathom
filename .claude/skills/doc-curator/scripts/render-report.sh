#!/usr/bin/env bash
# render-report.sh — 把 doc-curator scan.sh 的 JSONL 输出渲染成人读的 Markdown 报告。
#
# 用法：
#   scan.sh ... | render-report.sh              # stdin
#   render-report.sh results.jsonl              # 文件参数
#   render-report.sh results.jsonl > report.md  # 重定向到文件
#
# 设计原则：
#   - 零外部依赖（纯 Bash + awk），与 doc-curator 整体风格一致
#   - 不改 scan.sh 的 stdout JSONL 契约（本脚本只消费 JSONL，产出 .md）
#   - 只报问题项（hard/adaptive/soft 逐条），ok 只给计数（报告聚焦「哪里要修」）
#   - 容错：非法 JSONL 行跳过并 stderr 警告，不崩溃；空输入生成「无结果」报告
#
# 退出码：0=报告生成成功（无论是否有 finding）；1=输入读取失败

set -euo pipefail

# ── 元信息（可选，由调用方通过环境变量传入，用于报告头）──
REPORT_PROJECT="${DOC_CURATOR_REPORT_PROJECT:-未指定}"
REPORT_CONFIG="${DOC_CURATOR_REPORT_CONFIG:-未指定}"

# ── JSONL 合法性校验（与 common.sh validate_result_line 同构，自带避免 source 副作用）──
is_valid_jsonl() {
  printf '%s\n' "$1" | awk '
    /^\{"checker":"([^"\\]|\\.)+","severity":"(ok|hard|adaptive|soft)","rule_id":"([^"\\]|\\.)+","message":"([^"\\]|\\.)*","suggestion":"([^"\\]|\\.)*"\}$/ { exit 0 }
    { exit 1 }
  '
}

# 解码 doc-curator 的 JSON 字符串。json_escape 只产生 \"、\\、\n、\r、\t；
# 兼容外部 JSONL 常见的 \/、\b、\f，未知转义原样保留，避免 printf %b 扩大解释面。
json_unescape() {
  local s="$1" out="" ch escaped
  local i=0 length="${#1}"
  while [ "$i" -lt "$length" ]; do
    ch="${s:$i:1}"
    if [ "$ch" != "\\" ]; then
      out+="$ch"
      i=$((i + 1))
      continue
    fi
    i=$((i + 1))
    if [ "$i" -ge "$length" ]; then
      out+="\\"
      break
    fi
    escaped="${s:$i:1}"
    case "$escaped" in
      '"') out+='"' ;;
      \\) out="${out}\\" ;;
      '/') out+='/' ;;
      b|f|n|r|t) out+=' ' ;;
      *) out+="\\${escaped}" ;;
    esac
    i=$((i + 1))
  done
  printf '%s' "$out"
}

# 按固定 JSONL schema 的相邻字段分隔符提取值，再做受控解码。
# 分隔符中的引号在字段值内一定被转义，因此不会与合法内容冲突。
extract_field() {
  local line="$1" field="$2" prefix suffix value
  case "$field" in
    checker)    prefix='{"checker":"';       suffix='","severity":"' ;;
    severity)   prefix='","severity":"';    suffix='","rule_id":"' ;;
    rule_id)    prefix='","rule_id":"';     suffix='","message":"' ;;
    message)    prefix='","message":"';     suffix='","suggestion":"' ;;
    suggestion) prefix='","suggestion":"';  suffix='"}' ;;
    *) return 1 ;;
  esac
  if [ "$field" = "checker" ]; then
    value="${line#"$prefix"}"
  else
    value="${line#*"$prefix"}"
  fi
  if [ "$field" = "suggestion" ]; then
    value="${value%"$suffix"}"
  else
    value="${value%%"$suffix"*}"
  fi
  json_unescape "$value"
}

# Markdown 表格转义：| → \|，换行 → 空格，首尾空白 trim
md_escape() {
  local s="$1"
  s="${s//|/\\|}"
  s="${s//$'\n'/ }"
  s="${s//$'\r'/ }"
  # trim 首尾空格
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# ── 读取输入到临时文件（统一处理 stdin / 文件参数）──
INPUT_FILE="$(mktemp)"
trap 'rm -f "$INPUT_FILE"' EXIT

if [ "$#" -ge 1 ] && [ -n "$1" ] && [ "$1" != "-" ]; then
  if [ ! -f "$1" ]; then
    printf 'render-report: 输入文件不存在：%s\n' "$1" >&2
    exit 1
  fi
  cp "$1" "$INPUT_FILE"
else
  cat > "$INPUT_FILE"
fi

# ── 校验 + 分类 + 计数 ──
# 过滤掉非法行（stderr 警告），把合法行按 severity 分到四个临时文件
HARD_FILE="$(mktemp)"; ADAPTIVE_FILE="$(mktemp)"
SOFT_FILE="$(mktemp)"; OK_FILE="$(mktemp)"
trap 'rm -f "$INPUT_FILE" "$HARD_FILE" "$ADAPTIVE_FILE" "$SOFT_FILE" "$OK_FILE"' EXIT

invalid_count=0
while IFS= read -r line || [ -n "$line" ]; do
  [ -z "$line" ] && continue
  if ! is_valid_jsonl "$line"; then
    invalid_count=$((invalid_count + 1))
    printf 'render-report: 跳过非法 JSONL 行（%d）：%.80s...\n' "$invalid_count" "$line" >&2
    continue
  fi
  sev="$(extract_field "$line" severity)"
  case "$sev" in
    hard)     printf '%s\n' "$line" >> "$HARD_FILE" ;;
    adaptive) printf '%s\n' "$line" >> "$ADAPTIVE_FILE" ;;
    soft)     printf '%s\n' "$line" >> "$SOFT_FILE" ;;
    ok)       printf '%s\n' "$line" >> "$OK_FILE" ;;
  esac
done < "$INPUT_FILE"

hard_count="$(grep -c . "$HARD_FILE" 2>/dev/null || true)"; hard_count="${hard_count:-0}"
adaptive_count="$(grep -c . "$ADAPTIVE_FILE" 2>/dev/null || true)"; adaptive_count="${adaptive_count:-0}"
soft_count="$(grep -c . "$SOFT_FILE" 2>/dev/null || true)"; soft_count="${soft_count:-0}"
ok_count="$(grep -c . "$OK_FILE" 2>/dev/null || true)"; ok_count="${ok_count:-0}"
# 兜底：确保是纯数字（grep -c 在某些场景可能返回空或多行）
case "$hard_count" in ''|*[!0-9]*) hard_count=0 ;; esac
case "$adaptive_count" in ''|*[!0-9]*) adaptive_count=0 ;; esac
case "$soft_count" in ''|*[!0-9]*) soft_count=0 ;; esac
case "$ok_count" in ''|*[!0-9]*) ok_count=0 ;; esac
total=$((hard_count + adaptive_count + soft_count + ok_count))

# ── 生成 Markdown ──
GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 报告头
printf '# doc-curator 体检报告\n\n'
printf -- '- **项目**：%s\n' "$(md_escape "$REPORT_PROJECT")"
printf -- '- **生成时间**：%s\n' "$GENERATED_AT"
printf -- '- **配置**：%s\n' "$(md_escape "$REPORT_CONFIG")"
printf -- '- **检查项总数**：%s\n\n' "$total"

# 汇总表
printf '## 汇总\n\n'
printf '| 严重度 | 数量 |\n'
printf '|---|---|\n'
printf '| 🔴 hard（必须处理） | %s |\n' "$hard_count"
printf '| 🟡 adaptive（建议处理） | %s |\n' "$adaptive_count"
printf '| 🔵 soft（软提示） | %s |\n' "$soft_count"
printf '| ✅ ok（通过） | %s |\n\n' "$ok_count"

# 结论
if [ "$total" -eq 0 ]; then
  printf '**结论**：⚠️ 未收到任何检查结果（scan 可能未运行或输出为空）。\n\n'
elif [ "$hard_count" -eq 0 ] && [ "$adaptive_count" -eq 0 ] && [ "$soft_count" -eq 0 ]; then
  printf '**结论**：✅ 全部检查通过（%s 项 ok）。\n\n' "$ok_count"
else
  parts=""
  [ "$hard_count" -gt 0 ] && parts="${parts}🔴 ${hard_count} 项 hard"
  [ "$adaptive_count" -gt 0 ] && parts="${parts:+、}🟡 ${adaptive_count} 项 adaptive"
  [ "$soft_count" -gt 0 ] && parts="${parts:+、}🔵 ${soft_count} 项 soft"
  printf '**结论**：需处理 %s。详见下方分组。\n\n' "$parts"
fi

# 渲染某档 finding 表格（参数：文件 标题 emoji+中文名）
render_section() {
  local file="$1" title="$2"
  local count line checker rule_id message suggestion
  count="$(grep -c . "$file" 2>/dev/null || true)"
  case "$count" in ''|*[!0-9]*) count=0 ;; esac
  [ "$count" -eq 0 ] && return 0
  printf '## %s\n\n' "$title"
  printf '| 检查项 | 规则 | 问题 | 建议 |\n'
  printf '|---|---|---|---|\n'
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    checker="$(md_escape "$(extract_field "$line" checker)")"
    rule_id="$(md_escape "$(extract_field "$line" rule_id)")"
    message="$(md_escape "$(extract_field "$line" message)")"
    suggestion="$(md_escape "$(extract_field "$line" suggestion)")"
    printf '| %s | %s | %s | %s |\n' "$checker" "$rule_id" "$message" "$suggestion"
  done < "$file"
  printf '\n'
}

render_section "$HARD_FILE" "🔴 hard（必须处理）"
render_section "$ADAPTIVE_FILE" "🟡 adaptive（建议处理）"
render_section "$SOFT_FILE" "🔵 soft（软提示）"

# ok 档只给计数说明（不逐条列）
if [ "$ok_count" -gt 0 ]; then
  printf '> ℹ️ ok 档共 %s 条（已通过，本报告不逐条列出）。如需完整输出请直接查看 scan.sh 的 JSONL。\n' "$ok_count"
fi
