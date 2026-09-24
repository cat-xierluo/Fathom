#!/usr/bin/env bash
# Markdown 本地链接检查：解析、URL decode 后按 repo-relative 路径检查存在性与严重度。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"
# 在条件/命令替换之前加载一次；读取失败不得被默认值或 process substitution 吞掉。
_cfg_ensure_flat

ENABLED="$(cfg_scalar markdown_link_broken.enabled)"
if [ "$ENABLED" != "true" ]; then
  emit_result "soft" "markdown-link-broken-disabled" \
    "markdown_link_broken 未显式启用，未验证 Markdown 本地链接" \
    "需要该证据时设置 markdown_link_broken.enabled: true"
  exit 0
fi

CHECK_CODE_PATHS="$(cfg_scalar markdown_link_broken.check_code_paths)"
CHECK_CODE_PATHS="${CHECK_CODE_PATHS:-false}"
DEFAULT_SEVERITY="$(cfg_scalar markdown_link_broken.default_severity)"
DEFAULT_SEVERITY="${DEFAULT_SEVERITY:-hard}"

validate_severity() { case "$1" in hard|adaptive|soft) return 0 ;; *) return 1 ;; esac; }
if ! validate_severity "$DEFAULT_SEVERITY"; then
  emit_result "hard" "config-severity-invalid" \
    "markdown_link_broken.default_severity 非法：$DEFAULT_SEVERITY" "使用 hard、adaptive 或 soft"
fi

# 配置策略只解析一次；大型文档仓中逐链接重跑 YAML 查询会把线性扫描放大成分钟级。
declare -a TARGET_PATTERNS=() TARGET_SEVERITIES=()
declare -a SOURCE_PATTERNS=() SOURCE_SEVERITIES=()
declare -a PSEUDO_PATTERNS=() PSEUDO_SEVERITIES=()
declare -a EXCLUDE_PATTERNS=()
for idx in $(cfg_list_indices markdown_link_broken.severity_paths path); do
  TARGET_PATTERNS+=("$(cfg_list_field markdown_link_broken.severity_paths "$idx" path)")
  severity="$(cfg_list_field markdown_link_broken.severity_paths "$idx" severity)"
  TARGET_SEVERITIES+=("${severity:-$DEFAULT_SEVERITY}")
done
for idx in $(cfg_list_indices markdown_link_broken.source_severity_paths path); do
  SOURCE_PATTERNS+=("$(cfg_list_field markdown_link_broken.source_severity_paths "$idx" path)")
  severity="$(cfg_list_field markdown_link_broken.source_severity_paths "$idx" severity)"
  SOURCE_SEVERITIES+=("${severity:-$DEFAULT_SEVERITY}")
done
for idx in $(cfg_list_indices markdown_link_broken.pseudo_target_patterns path); do
  PSEUDO_PATTERNS+=("$(cfg_list_field markdown_link_broken.pseudo_target_patterns "$idx" path)")
  severity="$(cfg_list_field markdown_link_broken.pseudo_target_patterns "$idx" severity)"
  PSEUDO_SEVERITIES+=("${severity:-$DEFAULT_SEVERITY}")
done
for idx in $(cfg_list_indices markdown_link_broken.exclude_paths path); do
  EXCLUDE_PATTERNS+=("$(cfg_list_field markdown_link_broken.exclude_paths "$idx" path)")
done
for severity in "${TARGET_SEVERITIES[@]}" "${SOURCE_SEVERITIES[@]}" "${PSEUDO_SEVERITIES[@]}"; do
  [ -z "$severity" ] && continue
  if ! validate_severity "$severity"; then
    emit_result "hard" "config-severity-invalid" \
      "markdown_link_broken policy severity 非法：$severity" "使用 hard、adaptive 或 soft"
  fi
done

url_decode() {
  local rest="$1" out="" prefix hex byte
  while [[ "$rest" =~ ^([^%]*)%([0-9A-Fa-f]{2})(.*)$ ]]; do
    prefix="${BASH_REMATCH[1]}"; hex="${BASH_REMATCH[2]}"; rest="${BASH_REMATCH[3]}"
    printf -v byte '%b' "\\x$hex"
    out+="$prefix$byte"
  done
  printf '%s' "$out$rest"
}

normalize_rel() {
  awk -v input="$1" 'BEGIN {
    n=split(input, parts, "/"); top=0
    for (i=1; i<=n; i++) {
      if (parts[i]=="" || parts[i]==".") continue
      if (parts[i]=="..") {
        if (top>0 && stack[top]!="..") top--; else stack[++top]=".."
      } else stack[++top]=parts[i]
    }
    for (i=1; i<=top; i++) printf "%s%s", (i>1?"/":""), stack[i]
  }'
}

# macOS 没有系统 realpath/readlink -f；用 pwd -P + readlink 解开已存在目标的物理路径。
# 返回 0 并输出物理路径；循环/过长 symlink 链返回 1。
physical_existing_path() {
  local path="$1" dir leaf target hops=0
  dir="$(cd "$(dirname "$path")" 2>/dev/null && pwd -P)" || return 1
  path="$dir/$(basename "$path")"
  while [ -L "$path" ]; do
    hops=$((hops + 1))
    [ "$hops" -le 40 ] || return 1
    target="$(readlink "$path")" || return 1
    if [[ "$target" = /* ]]; then path="$target"; else path="$(dirname "$path")/$target"; fi
    dir="$(cd "$(dirname "$path")" 2>/dev/null && pwd -P)" || return 1
    path="$dir/$(basename "$path")"
  done
  if [ -d "$path" ]; then
    (cd "$path" 2>/dev/null && pwd -P)
  elif [ -e "$path" ]; then
    dir="$(cd "$(dirname "$path")" 2>/dev/null && pwd -P)" || return 1
    leaf="$(basename "$path")"
    printf '%s/%s' "$dir" "$leaf"
  else
    return 1
  fi
}

severity_from_list() {
  local list="$1" value="$2" i pattern severity length
  case "$list" in
    severity_paths) length="${#TARGET_PATTERNS[@]}" ;;
    source_severity_paths) length="${#SOURCE_PATTERNS[@]}" ;;
    pseudo_target_patterns) length="${#PSEUDO_PATTERNS[@]}" ;;
    *) return 1 ;;
  esac
  for ((i=0; i<length; i++)); do
    case "$list" in
      severity_paths) pattern="${TARGET_PATTERNS[$i]}"; severity="${TARGET_SEVERITIES[$i]}" ;;
      source_severity_paths) pattern="${SOURCE_PATTERNS[$i]}"; severity="${SOURCE_SEVERITIES[$i]}" ;;
      pseudo_target_patterns) pattern="${PSEUDO_PATTERNS[$i]}"; severity="${PSEUDO_SEVERITIES[$i]}" ;;
    esac
    [ -n "$pattern" ] || continue
    # shellcheck disable=SC2254 # 项目配置声明 bash glob。
    case "$value" in $pattern) printf '%s' "$severity"; return 0 ;; esac
  done
  return 1
}

source_excluded() {
  local source="$1" pattern
  for pattern in "${EXCLUDE_PATTERNS[@]}"; do
    [ -n "$pattern" ] || continue
    # shellcheck disable=SC2254 # 项目配置声明 bash glob。
    case "$source" in $pattern) return 0 ;; esac
  done
  return 1
}

severity_for_link() {
  local source="$1" target="$2" severity
  if severity="$(severity_from_list source_severity_paths "$source")"; then
    printf '%s' "$severity"
  elif severity="$(severity_from_list severity_paths "$target")"; then
    printf '%s' "$severity"
  else
    printf '%s' "$DEFAULT_SEVERITY"
  fi
}

broken_count=0; scanned_files=0; total_links=0
files_list="$(mktemp)"
trap 'rm -f "$files_list"' EXIT
# 非源码目录不进入扫描枚举；-mindepth 1 防止仓库根目录本身恰好叫这些名字时被整仓 prune。
# tmp/dist/build/node_modules/.git 里的断链不是项目文档问题（旧实现只排嵌套 */node_modules/*）。
if ! find "$REPO_ROOT" -mindepth 1 \
    \( -type d \( -name node_modules -o -name .git -o -name tmp -o -name dist -o -name build \) -prune \) \
    -o -type f -name '*.md' -print0 > "$files_list" 2>/dev/null; then
  emit_result "hard" "markdown-link-scan-error" "无法枚举 Markdown 文件" "检查项目目录权限"
  finish_checker
fi

check_target() {
  local source_rel="$1" raw="$2" decoded base normalized severity candidate physical
  case "$raw" in http:*|https:*|//*|\#*|mailto:*|tel:*|javascript:*|data:*) return 0 ;; esac
  raw="${raw%%#*}"; raw="${raw%%\?*}"
  [ -n "$raw" ] || return 0
  if [[ "$raw" =~ %([0-1][0-9A-Fa-f]|7[fF]) ]]; then
    emit_result "hard" "markdown-link-unsafe-encoding" \
      "$source_rel: 链接含控制字符百分号编码 → $raw" "移除控制字符或修正链接编码"
    return 0
  fi
  decoded="$(url_decode "$raw")"
  total_links=$((total_links + 1))

  if severity="$(severity_from_list pseudo_target_patterns "$decoded")"; then
    emit_result "$severity" "markdown-link-pseudo-target" \
      "$source_rel: 非文件型链接目标 → $decoded" \
      "若它是概念编号请改成纯文本；若它是文件请补全相对路径"
    return 0
  fi

  if [[ "$decoded" = /* ]]; then base="${decoded#/}"; else base="$(dirname "$source_rel")/$decoded"; fi
  normalized="$(normalize_rel "$base")"
  if [ -z "$normalized" ] || [[ "$normalized" = .. || "$normalized" = ../* ]]; then
    emit_result "hard" "markdown-link-outside-repo" \
      "$source_rel: 本地链接逃出仓库 → $decoded" "改用仓库内可追踪的相对路径"
    return 0
  fi
  candidate="$REPO_ROOT/$normalized"
  if [ -e "$candidate" ]; then
    if ! physical="$(physical_existing_path "$candidate")"; then
      emit_result "hard" "markdown-link-scan-error" \
        "$source_rel: 无法解析本地链接的物理路径 → $decoded" \
        "检查循环符号链接、目标权限或路径结构"
      return 0
    fi
    case "$physical" in
      "$REPO_ROOT"|"$REPO_ROOT"/*) ;;
      *)
        emit_result "hard" "markdown-link-outside-repo" \
          "$source_rel: 本地链接经符号链接逃出仓库 → $decoded" \
          "改用仓库内可追踪的目标，或移除指向仓外的符号链接"
        return 0 ;;
    esac
  else
    severity="$(severity_for_link "$source_rel" "$normalized")"
    emit_result "$severity" "markdown-link-broken" \
      "${source_rel}: broken link → ${decoded}（normalized: ${normalized}）" \
      "删除链接、修正大小写/编码或恢复目标文件"
    broken_count=$((broken_count + 1))
  fi
}

# 非源码目录（node_modules/tmp/.git/dist/build/vendor）任何层级下的 Markdown
# 都不作为扫描源；这是配置 exclude_paths 之外的默认防线。
is_non_source_path() {
  local rel="$1" rest comp
  case "$rel" in .claude/skills/*) return 0 ;; esac
  rest="$rel"
  while [ -n "$rest" ]; do
    comp="${rest%%/*}"
    case "$comp" in node_modules|.git|tmp|dist|build|vendor) return 0 ;; esac
    if [ "$comp" = "$rest" ]; then break; fi
    rest="${rest#*/}"
  done
  return 1
}

while IFS= read -r -d '' md_file; do
  rel_path="${md_file#"$REPO_ROOT"/}"
  is_non_source_path "$rel_path" && continue
  source_excluded "$rel_path" && continue
  scanned_files=$((scanned_files + 1))
  set +e
  markdown_text="$(awk '
    /^[[:space:]]*(```|~~~)/ { in_fence = !in_fence; next }
    !in_fence { line=$0; gsub(/`[^`]*`/, "", line); print line }
  ' "$md_file" 2>/dev/null)"; awk_rc=$?
  set -e
  if [ "$awk_rc" -ne 0 ]; then
    emit_result "hard" "markdown-link-scan-error" "$rel_path 无法解析" "检查文件权限"
    continue
  fi

  set +e
  markdown_links="$(printf '%s\n' "$markdown_text" | grep -oE '\[[^][]*\]\([^)]*\)')"; grep_rc=$?
  set -e
  if [ "$grep_rc" -gt 1 ]; then
    emit_result "hard" "markdown-link-scan-error" "$rel_path 无法读取" "检查文件权限"
    continue
  fi
  while IFS= read -r raw_link; do
    [ -n "$raw_link" ] || continue
    target="${raw_link#*](}"; target="${target%)}"
    target="${target#${target%%[![:space:]]*}}"
    if [[ "$target" = \<*\>* ]]; then
      target="${target#<}"; target="${target%%>*}"
    else
      target="${target%%[[:space:]]*}"
    fi
    check_target "$rel_path" "$target"
  done <<< "$markdown_links"

  if [ "$CHECK_CODE_PATHS" = "true" ]; then
    set +e
    code_paths="$(grep -ohE '`[a-zA-Z0-9_./ -]+\.[a-z]+`' "$md_file" 2>/dev/null | tr -d '`')"; grep_rc=$?
    set -e
    if [ "$grep_rc" -gt 1 ]; then
      emit_result "hard" "markdown-link-scan-error" "$rel_path 无法读取" "检查文件权限"
      continue
    fi
    while IFS= read -r code_path; do
      [ -n "$code_path" ] || continue
      case "$code_path" in */*) check_target "$rel_path" "$code_path" ;; esac
    done <<< "$code_paths"
  fi
done < "$files_list"

if [ "$broken_count" -eq 0 ] && [ "${DOC_CURATOR_EMITTED_HARD:-0}" -eq 0 ] && \
   [ "${DOC_CURATOR_EMITTED_ADAPTIVE:-0}" -eq 0 ]; then
  emit_result "ok" "markdown-link-broken" \
    "扫描 $scanned_files 个 Markdown 文件，共 $total_links 个本地链接，0 broken"
fi
finish_checker
