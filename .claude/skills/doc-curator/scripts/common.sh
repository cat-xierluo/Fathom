#!/usr/bin/env bash
# doc-curator 共享工具：运行环境、路径、配置、状态和结构化输出。

set -euo pipefail

if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
  printf 'doc-curator 需要 Bash 4+；当前版本：%s\n' "${BASH_VERSION:-unknown}" >&2
  printf 'macOS 可运行：brew install bash\n' >&2
  return 78 2>/dev/null || exit 78
fi

if [ -t 2 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

DOC_CURATOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$DOC_CURATOR_DIR/.." && pwd)"
REPO_ROOT="${DOC_CURATOR_REPO:-$PWD}"
if ! REPO_ROOT="$(cd "$REPO_ROOT" 2>/dev/null && pwd -P)"; then
  printf 'doc-curator: 被体检项目目录不存在或不可读：%s\n' "${DOC_CURATOR_REPO:-$PWD}" >&2
  return 66 2>/dev/null || exit 66
fi

print_header() { printf '%s== %s ==%s\n' "$C_BOLD" "$1" "$C_RESET" >&2; }
print_pass()   { printf '%s✓%s %s\n' "$C_GREEN" "$C_RESET" "$1" >&2; }
print_warn()   { printf '%s!%s %s\n' "$C_YELLOW" "$C_RESET" "$1" >&2; }
print_fail()   { printf '%s✗%s %s\n' "$C_RED" "$C_RESET" "$1" >&2; }
print_info()   { printf '%s·%s %s\n' "$C_BLUE" "$C_RESET" "$1" >&2; }

resolve_config() {
  local explicit="${DOC_CURATOR_CONFIG:-}" candidate
  if [ -n "$explicit" ]; then
    if [ ! -f "$explicit" ]; then
      printf 'doc-curator: 显式配置不存在：%s\n' "$explicit" >&2
      return 66
    fi
    printf '%s' "$explicit"
    return 0
  fi
  local repo_base
  repo_base="$(basename "$REPO_ROOT")"
  for candidate in \
    "$REPO_ROOT/doc-curator.yaml" \
    "$REPO_ROOT/.doc-curator.yaml" \
    "$SKILL_ROOT/config/${repo_base}.local.yaml" \
    "$SKILL_ROOT/config/${repo_base}.yaml" \
    "$SKILL_ROOT/config/default.yaml"; do
    if [ -f "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  printf 'doc-curator: 未找到配置，且内置 default.yaml 缺失\n' >&2
  return 66
}

CONFIG_FILE="$(resolve_config)"

config_source_name() {
  local selected="$1" repo_base
  repo_base="$(basename "$REPO_ROOT")"
  case "$selected" in
    "$REPO_ROOT/doc-curator.yaml") printf '%s' project-root ;;
    "$REPO_ROOT/.doc-curator.yaml") printf '%s' project-root-hidden ;;
    "$SKILL_ROOT/config/${repo_base}.local.yaml") printf '%s' skill-local-profile ;;
    "$SKILL_ROOT/config/${repo_base}.yaml") printf '%s' skill-profile ;;
    "$SKILL_ROOT/config/default.yaml") printf '%s' bundled-default ;;
    *) printf '%s' explicit ;;
  esac
}

# 被子 checker 消费；单文件 ShellCheck 无法观察跨脚本引用。
# shellcheck disable=SC2034
CONFIG_SOURCE="$(config_source_name "$CONFIG_FILE")"

sha256_text() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    printf 'doc-curator: 缺少 shasum/sha256sum，无法绑定项目状态\n' >&2
    return 69
  fi
}

sha256_file() {
  local file="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  else
    printf 'doc-curator: 缺少 shasum/sha256sum，无法计算文件哈希\n' >&2
    return 69
  fi
}

PROJECT_ID="$(printf '%s' "$REPO_ROOT" | sha256_text)"

# 状态文件收进 Skill 内部统一管理：按 project_id（项目绝对路径哈希）命名，
# 零 basename 碰撞，避免 --init-baseline 无条件覆盖冲掉同名项目基线。
STATE_FILE="${DOC_CURATOR_STATE_FILE:-$SKILL_ROOT/state/$PROJECT_ID.state.json}"

_CFG_FLAT=""
_CFG_LOADED=0

_cfg_ensure_flat() {
  if [ "$_CFG_LOADED" -eq 1 ]; then
    return 0
  fi
  if [ ! -r "$CONFIG_FILE" ]; then
    printf 'doc-curator: 配置不可读：%s\n' "$CONFIG_FILE" >&2
    return 66
  fi
  if grep -n $'\t' "$CONFIG_FILE" >/dev/null 2>&1; then
    printf 'doc-curator: YAML 不允许 Tab 缩进：%s\n' "$CONFIG_FILE" >&2
    return 65
  fi
  local flat duplicate_path
  if ! flat="$(awk -f "$DOC_CURATOR_DIR/yaml-flatten.awk" "$CONFIG_FILE")"; then
    printf 'doc-curator: YAML 解析失败：%s\n' "$CONFIG_FILE" >&2
    return 65
  fi
  if ! duplicate_path="$(printf '%s\n' "$flat" | awk -F= '
    NF {
      key=$1
      if (++seen[key] == 2 && !duplicate) duplicate=key
    }
    END {if (duplicate) print duplicate}
  ')"; then
    printf 'doc-curator: 配置路径唯一性检查执行失败：%s\n' "$CONFIG_FILE" >&2
    return 65
  fi
  if [ -n "$duplicate_path" ]; then
    printf 'doc-curator: YAML 含重复配置路径：%s（%s）\n' "$duplicate_path" "$CONFIG_FILE" >&2
    return 65
  fi
  # grep -q 提前退出会让大合法 flat 的 printf 收到 SIGPIPE（pipefail 下误判缺 fields）。
  if ! printf '%s\n' "$flat" | grep -E '^files\.[0-9]+\.path=.+$' >/dev/null; then
    printf 'doc-curator: 配置缺少非空 files[].path：%s\n' "$CONFIG_FILE" >&2
    return 65
  fi
  _CFG_FLAT="$flat"
  _CFG_LOADED=1
}

cfg_file_paths() {
  _cfg_ensure_flat || return $?
  printf '%s\n' "$_CFG_FLAT" | awk -F= '/^files\.[0-9]+\.path=/{sub(/^[^=]+=/, ""); print}'
}

cfg_file_by_role() {
  _cfg_ensure_flat || return $?
  local role="$1" idx
  idx="$(printf '%s\n' "$_CFG_FLAT" | awk -F'[.=]' -v role="$role" '!found && $1=="files" && $3=="role" && $4==role {print $2; found=1}')" || return $?
  if [ -n "$idx" ]; then
    printf '%s\n' "$_CFG_FLAT" | awk -F= -v key="files.$idx.path" '$1==key {sub(/^[^=]+=/, ""); print}'
  fi
}

cfg_rule_get() {
  _cfg_ensure_flat || return $?
  local section="$1" id="$2" field="$3" idx
  idx="$(printf '%s\n' "$_CFG_FLAT" | awk -F'[.=]' -v section="$section" -v id="$id" '!found && $1==section && $3=="id" && $4==id {print $2; found=1}')" || return $?
  if [ -n "$idx" ]; then
    printf '%s\n' "$_CFG_FLAT" | awk -F= -v key="$section.$idx.$field" '$1==key {sub(/^[^=]+=/, ""); print}'
  fi
}

cfg_rule_enabled() {
  local section="$1" id="$2" value
  value="$(cfg_rule_get "$section" "$id" id)" || return $?
  [ "$value" = "$id" ]
}

cfg_scalar() {
  _cfg_ensure_flat || return $?
  local key="$1"
  printf '%s\n' "$_CFG_FLAT" | awk -F= -v key="$key" '$1==key {sub(/^[^=]+=/, ""); print}'
}

cfg_change_type_indices() {
  _cfg_ensure_flat || return $?
  printf '%s\n' "$_CFG_FLAT" | awk -F'[.=]' '$1=="context_sync" && $2=="change_types" && $4=="pattern" {print $3}'
}

cfg_list_indices() {
  _cfg_ensure_flat || return $?
  local prefix="$1" field_name="${2:-path}"
  printf '%s\n' "$_CFG_FLAT" | awk -F= -v prefix="$prefix" -v field_name="$field_name" '
    {
      key=$1; start=prefix "."; suffix="." field_name
      if (index(key, start) != 1) next
      rest=substr(key, length(start)+1)
      if (length(rest) <= length(suffix)) next
      if (substr(rest, length(rest)-length(suffix)+1) != suffix) next
      idx=substr(rest, 1, length(rest)-length(suffix))
      if (idx ~ /^[0-9]+$/) print idx
    }
  '
}

cfg_list_field() {
  _cfg_ensure_flat || return $?
  local prefix="$1" idx="$2" field="$3"
  printf '%s\n' "$_CFG_FLAT" | awk -F= -v key="$prefix.$idx.$field" '$1==key {sub(/^[^=]+=/, ""); print}'
}

cfg_change_type_field() {
  cfg_scalar "context_sync.change_types.$1.$2"
}

json_escape() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

emit_result() {
  local severity="$1" rule_id="$2" message="$3" suggestion="${4:-}"
  local checker="${DOC_CURATOR_CHECKER_ID:-standalone}"
  case "$severity" in ok|hard|adaptive|soft) ;; *) severity="hard" ;; esac
  case "$severity" in
    hard) DOC_CURATOR_EMITTED_HARD=1 ;;
    adaptive) DOC_CURATOR_EMITTED_ADAPTIVE=1 ;;
  esac
  printf '%s' '{"checker":"'
  printf '%s' "$(json_escape "$checker")"
  printf '%s' '","severity":"'
  printf '%s' "$(json_escape "$severity")"
  printf '%s' '","rule_id":"'
  printf '%s' "$(json_escape "$rule_id")"
  printf '%s' '","message":"'
  printf '%s' "$(json_escape "$message")"
  printf '%s' '","suggestion":"'
  printf '%s' "$(json_escape "$suggestion")"
  printf '%s\n' '"}'
}

DOC_CURATOR_EMITTED_HARD=0
DOC_CURATOR_EMITTED_ADAPTIVE=0

finish_checker() {
  if [ "${DOC_CURATOR_EMITTED_HARD:-0}" -eq 1 ]; then
    return 1
  fi
  if [ "${DOC_CURATOR_EMITTED_ADAPTIVE:-0}" -eq 1 ]; then
    return 2
  fi
  return 0
}

validate_result_line() {
  printf '%s\n' "$1" | awk '
    BEGIN { ok=0 }
    /^\{"checker":"([^"\\]|\\.)+","severity":"(ok|hard|adaptive|soft)","rule_id":"([^"\\]|\\.)+","message":"([^"\\]|\\.)*","suggestion":"([^"\\]|\\.)*"\}$/ { ok=1 }
    END { exit(ok ? 0 : 1) }
  '
}

result_field() {
  local line="$1" field="$2"
  printf '%s\n' "$line" | sed -n "s/.*\"$field\":\"\([^\"]*\)\".*/\1/p"
}

validate_ere() {
  local pattern="$1" rc
  set +e
  printf '' | grep -E "$pattern" >/dev/null 2>&1
  rc=$?
  set -e
  [ "$rc" -le 1 ]
}

state_project_matches() {
  [ ! -f "$STATE_FILE" ] && return 1
  local recorded recorded_config current_config
  recorded="$(sed -n 's/.*"project_id":[[:space:]]*"\([^"]*\)".*/\1/p' "$STATE_FILE" | head -1)"
  recorded_config="$(sed -n 's/.*"config_sha256":[[:space:]]*"\([^"]*\)".*/\1/p' "$STATE_FILE" | head -1)"
  current_config="$(sha256_file "$CONFIG_FILE")"
  [ -n "$recorded" ] && [ "$recorded" = "$PROJECT_ID" ] && \
    [ -n "$recorded_config" ] && [ "$recorded_config" = "$current_config" ]
}

state_baseline_get() {
  local path="$1" metric="$2"
  state_project_matches || return 1
  awk -v target="$path" -v metric="$metric" '
    index($0, "\"" target "\"") {
      pattern = "\"" metric "\":[[:space:]]*[0-9.]+"
      if (match($0, pattern)) {
        value = substr($0, RSTART, RLENGTH)
        sub(/^.*:[[:space:]]*/, "", value)
        print value
        exit
      }
    }
  ' "$STATE_FILE"
}

adaptive_threshold() {
  local rule_id="$1" metric="$2" path="$3"
  local baseline multiplier
  baseline="$(state_baseline_get "$path" "$metric" || true)"
  if [ -z "$baseline" ]; then
    baseline="$(cfg_rule_get adaptive_rules "$rule_id" seed_value)"
  fi
  [ -z "$baseline" ] && return 1
  multiplier="$(cfg_rule_get adaptive_rules "$rule_id" multiplier)"
  multiplier="${multiplier:-1.5}"
  if ! printf '%s\n%s\n' "$baseline" "$multiplier" | awk 'NF!=1 || $1 !~ /^[0-9]+([.][0-9]+)?$/ {exit 1}'; then
    printf 'doc-curator: 非法 adaptive 数值：rule=%s baseline=%s multiplier=%s\n' "$rule_id" "$baseline" "$multiplier" >&2
    return 65
  fi
  awk -v b="$baseline" -v m="$multiplier" 'BEGIN { printf "%.6f", b*m }'
}
