#!/usr/bin/env bash
# ISS-037 单一版本源 fail-closed 校验器（CI 与本地同一命令口径）。
#
# 权威源：fathom/__init__.py 的 __version__。发行 tag、helper 合同、
# /api/status、FastAPI 文档版本、Tauri 与 Cargo 包版本全部以它为准。
#
# 比对对象与规则：
#   fathom/__init__.py        __version__ = "X.Y.Z"（唯一权威源）
#   fathom/api.py             FastAPI(version=...) 必须引用 __version__ 标识符，
#                             且全文件禁止硬编码语义化版本字面量（防旁路回潮）
#   tauri.conf.json           顶层 version 与所有嵌套 "version" 键（含 bundle/
#                             预发行配置未来新增处）必须等于权威源
#   Cargo.toml                [package] version 必须等于权威源
#   pyproject.toml            若声明 version 则必须等于权威源（当前 fathom
#                             非安装包、无该字段；规则保持 fail-closed 覆盖未来补充）
#   frontend/（UI）           无独立版本字面量，版本经 /api/status 从单一源
#                             读取——本脚本不扫 frontend，新增字面量时先登记规则
#
# 退出码合同（CI 直接以退出码判定，任一非零即红）：
#   0  全部一致；
#   1  版本漂移或单一源被旁路（stdout 打印差异表）；
#   2  结构性失败（文件缺失、__version__ 读不出、JSON 无法解析）。
#
# 测试（tests/test_version_consistency.py）通过 FATHOM_VERSION_CHECK_ROOT
# 指向临时副本制造漂移，不修改真实文件。
#
# 注意：不用 set -e——本脚本要收集全部来源的差异后统一打印差异表再退出，
# 每个失败分支显式处理；pipefail 保留以便捕获管道内 grep 的意外失败。
set -uo pipefail

root="${FATHOM_VERSION_CHECK_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
py_init="$root/fathom/__init__.py"
py_api="$root/fathom/api.py"
tauri_conf="$root/apps/desktop/src-tauri/tauri.conf.json"
cargo_toml="$root/apps/desktop/src-tauri/Cargo.toml"
pyproject="$root/pyproject.toml"

fail() {
  printf 'check_version_consistency: %s\n' "$1" >&2
  exit 2
}

rows=()
drift=0

# add_row <来源> <读到值> <ok|drift> <判定说明>
add_row() {
  rows+=("$(printf '%-42s %-12s %-6s %s' "$1" "$2" "$3" "$4")")
  if [ "$3" = "drift" ]; then drift=1; fi
  return 0
}

# ---- 0. 结构存在性（缺文件先于一切比对）----
for f in "$py_init" "$py_api" "$tauri_conf" "$cargo_toml"; do
  [ -f "$f" ] || fail "缺少 $f"
done

# ---- 1. 权威源：fathom/__init__.py ----
ref="$(grep -E '^__version__[[:space:]]*=[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"[[:space:]]*$' "$py_init" 2>/dev/null | head -1 | sed -E 's/.*"([0-9]+\.[0-9]+\.[0-9]+)".*/\1/')"
[ -n "$ref" ] || fail "无法从 $py_init 读出 __version__（合同格式：__version__ = \"X.Y.Z\" 单独一行）"
add_row "fathom/__init__.py __version__" "$ref" ok "单一版本源"

# ---- 2. fathom/api.py：FastAPI 引用单一源 + 无硬编码版本字面量 ----
# `version=` 前的字符排除下划线/字母数字，避免把 __version__ 赋值误判为硬编码。
api_hardcoded="$(grep -nE '(^|[^_[:alnum:]])version[[:space:]]*[:=][[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"' "$py_api" || true)"
if [ -n "$api_hardcoded" ]; then
  add_row "fathom/api.py 硬编码字面量" "见下" drift "出现 version=\"X.Y.Z\" 字面量，绕过单一源"
  rows+=("    $api_hardcoded")
else
  add_row "fathom/api.py 硬编码字面量" "无" ok "版本只能来自 __version__"
fi
# FastAPI 构造行（单行合同）必须 version=__version__。
if grep -E 'FastAPI\([^)]*version[[:space:]]*=[[:space:]]*__version__' "$py_api" >/dev/null; then
  add_row "fathom/api.py FastAPI(version=)" "引用" ok "读取 fathom.__version__"
else
  add_row "fathom/api.py FastAPI(version=)" "未引用" drift "FastAPI 构造未从单一源读取（或构造拆成多行）"
fi

# ---- 3. tauri.conf.json：顶层 + 全部嵌套 "version" 键 ----
top_version="$(jq -r 'if has("version") and (.version | type) == "string" then .version else "" end' "$tauri_conf" 2>/dev/null)" || fail "tauri.conf.json 无法解析（jq 报错）"
[ -n "$top_version" ] || fail "tauri.conf.json 缺顶层 version（或不是字符串）"
[ "$top_version" = "$ref" ] \
  && add_row "tauri.conf.json version" "$top_version" ok "" \
  || add_row "tauri.conf.json version" "$top_version" drift "期望 $ref"

# 嵌套 "version" 键（bundle/预发行配置等）：逐行读出，任一不等于权威源即漂移。
nested="$(jq -r '.. | objects | select(has("version")) | (.version | tostring)' "$tauri_conf" 2>/dev/null)" || fail "tauri.conf.json 无法解析（jq 报错）"
nested_total=0
nested_drift=0
while IFS= read -r v; do
  [ -n "$v" ] || continue
  nested_total=$((nested_total + 1))
  if [ "$v" != "$ref" ]; then
    nested_drift=$((nested_drift + 1))
  fi
done <<<"$nested"
if [ "$nested_total" -eq 0 ]; then
  fail "tauri.conf.json 一个 version 键都没读到（jq 语义异常）"
fi
if [ "$nested_drift" -eq 0 ]; then
  add_row "tauri.conf.json 全部 version 键" "$ref" ok "${nested_total} 处（含顶层）"
else
  add_row "tauri.conf.json 全部 version 键" "不一致" drift "${nested_drift}/${nested_total} 处 ≠ $ref"
  while IFS= read -r line; do
    rows+=("    $line")
  done <<<"$(grep -n '"version"' "$tauri_conf" || true)"
fi

# ---- 4. Cargo.toml [package] version ----
cargo_version="$(awk '/^\[package\]/{in_pkg=1; next} /^\[/{in_pkg=0} in_pkg && /^version[[:space:]]*=/{sub(/^version[[:space:]]*=[[:space:]]*"/, ""); sub(/"[[:space:]]*$/, ""); print; exit}' "$cargo_toml")"
[ -n "$cargo_version" ] || fail "Cargo.toml [package] 段读不出 version"
[ "$cargo_version" = "$ref" ] \
  && add_row "Cargo.toml [package] version" "$cargo_version" ok "" \
  || add_row "Cargo.toml [package] version" "$cargo_version" drift "期望 $ref"

# ---- 5. pyproject.toml：有 version 声明则必须一致 ----
pyproject_versions="$(grep -E '^version[[:space:]]*=[[:space:]]*"[^"]+"' "$pyproject" 2>/dev/null | sed -E 's/.*"([^"]+)".*/\1/' || true)"
if [ -z "$pyproject_versions" ]; then
  add_row "pyproject.toml version" "—" ok "未声明（fathom 非安装包）"
else
  bad=0
  while IFS= read -r v; do
    [ -n "$v" ] || continue
    [ "$v" = "$ref" ] || bad=1
  done <<<"$pyproject_versions"
  [ "$bad" -eq 0 ] \
    && add_row "pyproject.toml version" "$ref" ok "" \
    || add_row "pyproject.toml version" "不一致" drift "声明值：$(echo "$pyproject_versions" | tr '\n' ' ')"
fi

# ---- 汇总 ----
printf '%-42s %-12s %-6s %s\n' "来源" "值" "判定" "说明"
printf '%s\n' "${rows[@]}"
if [ "$drift" -eq 1 ]; then
  printf 'check_version_consistency: 版本漂移（单一版本源 %s）；上表 ≠ 行即需同步\n' "$ref" >&2
  exit 1
fi
printf 'version consistency: ok（单一版本源 %s）\n' "$ref"
exit 0
