#!/usr/bin/env bash
# 声明式跨文档一致性：索引上界与显式单值；不裁决声明的语义真实性。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"
_cfg_ensure_flat

ENABLED="$(cfg_scalar context_truth.enabled)"
if [ "$ENABLED" != "true" ]; then
  emit_result "soft" "context-truth-disabled" \
    "context_truth.enabled!=true，跨文档声明未验证" \
    "需要跨文档索引或显式单值一致性时启用"
  exit 0
fi

# 枚举所有条目，而非仅枚举 id 字段，避免混合配置中缺 id 的坏条目被忽略。
truth_indices() {
  printf '%s\n' "$_CFG_FLAT" | awk -F'[.=]' -v section="$1" \
    '$1=="context_truth" && $2==section && $3 ~ /^[0-9]+$/ && !seen[$3]++ {print $3}'
}

declare -A truth_ids=()
truth_register_id() {
  local id="$1"
  if [[ ! "$id" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
    emit_result "hard" "context-truth-config-invalid" "claim id 缺失或非法" \
      "使用唯一的字母、数字、连字符或下划线 id"
    return 1
  fi
  if [ -n "${truth_ids[$id]:-}" ]; then
    emit_result "hard" "context-truth-config-invalid" "重复的 claim id：$id" \
      "index_claims 与 value_claims 的 id 也必须互不重复"
    return 1
  fi
  truth_ids[$id]=1
}

# 严格限制 context_truth 字段，拼写错误/错误嵌套不能与有效条目一起静默通过。
while IFS='=' read -r key _; do
  case "$key" in
    context_truth.enabled) ;;
    context_truth.index_claims.*|context_truth.value_claims.*)
      if [[ "$key" =~ ^context_truth\.index_claims\.[0-9]+\.(id|source_file|item_pattern|id_pattern|mirror_file|claim_pattern|mode|severity)$ ]] ||
         [[ "$key" =~ ^context_truth\.value_claims\.[0-9]+\.(id|source_file|source_pattern|mirror_file|mirror_pattern|allowed_values|mode|severity)$ ]]; then
        continue
      fi
      emit_result "hard" "context-truth-config-invalid" "未知或格式错误的字段：$key" "按配置示例声明 claim 字段" ;;
    context_truth.*)
      emit_result "hard" "context-truth-config-invalid" "未知或格式错误的字段：$key" "使用 index_claims 或 value_claims 条目列表" ;;
  esac
done <<< "$_CFG_FLAT"

# 只解析元数据而不读取文件内容。拒绝任何 .. 组件；允许最终仍在仓内的 symlink。
# 使用 pwd -P/readlink 兼容没有 realpath 的 macOS。TRUTH_PATH 只在成功时赋值。
truth_file() {
  local id="$1" rel="$2" path dir target hops=0
  TRUTH_PATH=""
  case "$rel" in
    ''|/*|..|../*|*/../*|*/..)
      emit_result "hard" "context-truth-path-unsafe" "$id source/mirror 路径不是安全仓内相对路径" \
        "禁止绝对路径及 .. 组件"
      return 1 ;;
  esac
  path="$REPO_ROOT/$rel"
  while :; do
    if ! dir="$(cd "$(dirname "$path")" 2>/dev/null && pwd -P)"; then
      emit_result "hard" "context-truth-file-missing" "$id 无法定位文件：$rel" "检查路径、权限及 symlink"
      return 1
    fi
    path="$dir/$(basename "$path")"
    case "$path" in "$REPO_ROOT"/*) ;;
      *)
        emit_result "hard" "context-truth-path-unsafe" "$id 文件解析到仓库之外：$rel" "恢复仓内权威源或镜像"
        return 1 ;;
    esac
    [ -L "$path" ] || break
    hops=$((hops + 1))
    if [ "$hops" -gt 40 ] || ! target="$(readlink "$path")"; then
      emit_result "hard" "context-truth-path-unsafe" "$id 无法安全解析 symlink：$rel" "修正循环或不可读 symlink"
      return 1
    fi
    if [[ "$target" = /* ]]; then path="$target"; else path="$dir/$target"; fi
  done
  if [ ! -f "$path" ]; then
    emit_result "hard" "context-truth-file-missing" "$id 文件不存在或不是普通文件：$rel" "恢复权威源/镜像文件"
    return 1
  fi
  if [ ! -r "$path" ]; then
    emit_result "hard" "context-truth-scan-error" "$id 文件不可读：$rel" "检查文件权限"
    return 1
  fi
  TRUTH_PATH="$path"
}

claim_count=0
while IFS= read -r idx; do
  [ -n "$idx" ] || continue
  claim_count=$((claim_count + 1))
  id="$(cfg_list_field context_truth.index_claims "$idx" id)"
  source_file="$(cfg_list_field context_truth.index_claims "$idx" source_file)"
  item_pattern="$(cfg_list_field context_truth.index_claims "$idx" item_pattern)"
  id_pattern="$(cfg_list_field context_truth.index_claims "$idx" id_pattern)"
  mirror_file="$(cfg_list_field context_truth.index_claims "$idx" mirror_file)"
  claim_pattern="$(cfg_list_field context_truth.index_claims "$idx" claim_pattern)"
  mode="$(cfg_list_field context_truth.index_claims "$idx" mode)"
  severity="$(cfg_list_field context_truth.index_claims "$idx" severity)"
  severity="${severity:-hard}"

  truth_register_id "$id" || continue

  if [ -z "$id" ] || [ -z "$source_file" ] || [ -z "$item_pattern" ] || \
     [ -z "$id_pattern" ] || [ -z "$mirror_file" ] || [ -z "$claim_pattern" ]; then
    emit_result "hard" "context-truth-config-invalid" \
      "context_truth.index_claims[$idx] 缺少必填字段" \
      "补齐 id/source_file/item_pattern/id_pattern/mirror_file/claim_pattern"
    continue
  fi
  if [ "${mode:-max_numeric_suffix}" != "max_numeric_suffix" ]; then
    emit_result "hard" "context-truth-config-invalid" \
      "$id 使用不支持的 mode：$mode" \
      "index_claims 仅支持 max_numeric_suffix；单值比较使用 value_claims"
    continue
  fi
  case "$severity" in hard|adaptive) ;;
    *)
      emit_result "hard" "context-truth-config-invalid" "$id severity 非法：$severity" \
        "使用 hard 或 adaptive"
      continue ;;
  esac
  if ! validate_ere "$item_pattern" || ! validate_ere "$id_pattern" || ! validate_ere "$claim_pattern"; then
    emit_result "hard" "context-truth-config-invalid" "$id 含非法扩展正则" \
      "修正 index claim regex"
    continue
  fi
  truth_file "$id" "$source_file" || continue
  source_path="$TRUTH_PATH"
  truth_file "$id" "$mirror_file" || continue
  mirror_path="$TRUTH_PATH"

  set +e
  source_ids="$(grep -E -- "$item_pattern" "$source_path" | grep -oE -- "$id_pattern")"
  source_rc=$?
  mirror_claims="$(grep -oE -- "$claim_pattern" "$mirror_path")"
  mirror_rc=$?
  set -e
  if [ "$source_rc" -gt 1 ] || [ "$mirror_rc" -gt 1 ]; then
    emit_result "hard" "context-truth-scan-error" "$id 无法读取或匹配声明" \
      "检查文件权限与正则"
    continue
  fi
  if [ -z "$source_ids" ] || [ -z "$mirror_claims" ]; then
    emit_result "hard" "context-truth-claim-missing" \
      "$id 未在 source 或 mirror 找到可比较的索引声明" \
      "确认权威条目与镜像范围均存在"
    continue
  fi

  mirror_claim_count="$(printf '%s\n' "$mirror_claims" | awk 'NF { count++ } END { print count+0 }')"
  if [ "$mirror_claim_count" -ne 1 ]; then
    emit_result "hard" "context-truth-claim-ambiguous" \
      "$id 在 $mirror_file 命中 ${mirror_claim_count} 条范围声明，无法确定哪一条是权威镜像" \
      "收敛为唯一范围声明，或收窄 claim_pattern 到唯一可审计位置"
    continue
  fi

  source_max="$(printf '%s\n' "$source_ids" | grep -oE '[0-9]+$' | sort -n | tail -1 || true)"
  mirror_max="$(printf '%s\n' "$mirror_claims" | grep -oE '[0-9]+' | sort -n | tail -1 || true)"
  if [ -z "$source_max" ] || [ -z "$mirror_max" ]; then
    emit_result "hard" "context-truth-claim-unparseable" \
      "$id 无法从声明中提取数字后缀" \
      "让 id_pattern/claim_pattern 都包含数字编号"
  elif [ "$source_max" != "$mirror_max" ]; then
    emit_result "$severity" "context-truth-index-drift" \
      "${id} 漂移：${source_file} 权威最大编号=${source_max}，${mirror_file} 声称=${mirror_max}" \
      "更新镜像范围；不要复制维护权威条目正文"
  else
    emit_result "ok" "context-truth-index-sync" \
      "${id} 一致：最大编号=${source_max}（${source_file} ↔ ${mirror_file}）"
  fi
done < <(truth_indices index_claims)

# 返回值通过 TRUTH_VALUE 传递；finding 保留在 checker 进程中以参与退出码计算。
truth_extract_value() {
  local id="$1" path="$2" pattern="$3" matches rc count
  TRUTH_VALUE=""
  if matches="$(grep -E -- "$pattern" "$path")"; then rc=0; else rc=$?; fi
  if [ "$rc" -gt 1 ]; then
    emit_result "hard" "context-truth-scan-error" "$id 读取或提取声明失败" "检查读取错误与正则"
    return 1
  fi
  if [ "$rc" -eq 1 ] || [ -z "$matches" ]; then
    emit_result "hard" "context-truth-claim-missing" "$id 缺少可提取的唯一声明" "检查声明及 pattern 的锚定范围"
    return 1
  fi
  count="$(printf '%s\n' "$matches" | awk 'END {print NR}')"
  if [ "$count" -ne 1 ]; then
    emit_result "hard" "context-truth-claim-ambiguous" "$id 命中 $count 行声明" "收窄 pattern 或移除冲突镜像"
    return 1
  fi
  if [[ "$matches" =~ $pattern ]] && [ "${BASH_REMATCH[0]}" = "$matches" ] &&
     [ "${#BASH_REMATCH[@]}" -eq 2 ] && [ -n "${BASH_REMATCH[1]}" ]; then
    TRUTH_VALUE="${BASH_REMATCH[1]}"
  else
    emit_result "hard" "context-truth-claim-unparseable" "$id 必须由唯一捕获组提取一个非空值" "检查 pattern 捕获组与原始声明"
    return 1
  fi
}

while IFS= read -r idx; do
  [ -n "$idx" ] || continue
  claim_count=$((claim_count + 1))
  id="$(cfg_list_field context_truth.value_claims "$idx" id)"
  truth_register_id "$id" || continue
  source_file="$(cfg_list_field context_truth.value_claims "$idx" source_file)"
  source_pattern="$(cfg_list_field context_truth.value_claims "$idx" source_pattern)"
  mirror_file="$(cfg_list_field context_truth.value_claims "$idx" mirror_file)"
  mirror_pattern="$(cfg_list_field context_truth.value_claims "$idx" mirror_pattern)"
  allowed_values="$(cfg_list_field context_truth.value_claims "$idx" allowed_values)"
  mode="$(cfg_list_field context_truth.value_claims "$idx" mode)"
  severity="$(cfg_list_field context_truth.value_claims "$idx" severity)"
  severity="${severity:-hard}"
  if [ "$(cfg_scalar schema_version)" != "3" ] || [ "$mode" != "exact_value" ] ||
     [ -z "$source_file" ] || [ -z "$mirror_file" ] || [ -z "$allowed_values" ] ||
     [[ "$source_pattern" != ^*\$ ]] || [[ "$mirror_pattern" != ^*\$ ]]; then
    emit_result "hard" "context-truth-config-invalid" "$id 单值声明配置不完整或模式错误" \
      "schema 3 下补齐 exact_value/source_file/source_pattern/mirror_file/mirror_pattern/allowed_values；pattern 须用 ^ 与 $ 整行锚定"
    continue
  fi
  case "$severity" in hard|adaptive) ;;
    *) emit_result "hard" "context-truth-config-invalid" "$id severity 非法" "使用 hard 或 adaptive"; continue ;;
  esac
  if ! validate_ere "$source_pattern" || ! validate_ere "$mirror_pattern"; then
    emit_result "hard" "context-truth-config-invalid" "$id 含非法扩展正则" "修正 source_pattern/mirror_pattern"
    continue
  fi
  # 每个值是不含空白的 token；允许中文等非 ASCII 值，不做大小写/同义词归一化。
  read -r -a allowed_tokens <<< "$allowed_values"
  if [ "${#allowed_tokens[@]}" -eq 0 ]; then
    emit_result "hard" "context-truth-config-invalid" "$id allowed_values 为空" "填写空格分隔的允许值"
    continue
  fi
  truth_file "$id" "$source_file" || continue
  source_path="$TRUTH_PATH"
  truth_file "$id" "$mirror_file" || continue
  mirror_path="$TRUTH_PATH"
  truth_extract_value "$id source" "$source_path" "$source_pattern" || continue
  source_value="$TRUTH_VALUE"
  truth_extract_value "$id mirror" "$mirror_path" "$mirror_pattern" || continue
  mirror_value="$TRUTH_VALUE"
  source_allowed=false; mirror_allowed=false
  for token in "${allowed_tokens[@]}"; do
    [ "$source_value" != "$token" ] || source_allowed=true
    [ "$mirror_value" != "$token" ] || mirror_allowed=true
  done
  if [ "$source_allowed" != true ] || [ "$mirror_allowed" != true ]; then
    emit_result "hard" "context-truth-value-invalid" "$id 声明值不在 allowed_values 中" "核对声明及允许值；不做同义词推断"
  elif [ "$source_value" != "$mirror_value" ]; then
    emit_result "$severity" "context-truth-value-drift" \
      "$id 漂移：${source_file}=${source_value}；${mirror_file}=${mirror_value}" \
      "核对权威声明和执行证据，再同步镜像；不要自动改状态"
  else
    emit_result "ok" "context-truth-value-sync" \
      "$id 声明一致：${source_value}；仅验证标签相等，不证明工作已发生"
  fi
done < <(truth_indices value_claims)

if [ "$claim_count" -eq 0 ]; then
  emit_result "hard" "context-truth-no-claims" \
    "context_truth 已启用但没有 claims，不能作为验收证据" \
    "按项目 SSoT 关系配置 index_claims 或 value_claims"
fi

finish_checker
