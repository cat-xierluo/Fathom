#!/usr/bin/env bash
# 配置合同：阻止旧项目配置用缺省值静默关闭 checker，报告配置来源与遮蔽漂移。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

# 本 checker 会直接遍历 _CFG_FLAT；不能依赖 command substitution 内的子进程加载。
_cfg_ensure_flat

EXPECTED_SCHEMA="2/3"
schema="$(cfg_scalar schema_version)"
if [ -z "$schema" ]; then
  emit_result "hard" "config-schema-missing" \
    "当前配置缺少 schema_version：$CONFIG_FILE" \
    "选择 schema 2（旧能力）或 schema 3（含 value_claims）；不要依赖缺省值"
elif [ "$schema" != "2" ] && [ "$schema" != "3" ]; then
  emit_result "hard" "config-schema-unsupported" \
    "当前配置 schema_version=${schema}，运行时仅支持 ${EXPECTED_SCHEMA}" \
    "按 SKILL.md 的配置迁移说明更新配置"
fi

# 新能力用 schema 3 让 v0.9 reader 明确拒绝，不能让旧 reader 忽略 value_claims 后假绿。
if printf '%s\n' "$_CFG_FLAT" | awk '/^context_truth\.value_claims([.=])/ {found=1} END {exit !found}' && [ "$schema" != "3" ]; then
  emit_result "hard" "config-schema-capability" \
    "context_truth.value_claims 要求 schema_version: 3，旧 reader 不支持该能力" \
    "升级 reader 至 v0.10.0+ 并显式迁移配置为 schema 3"
fi

validate_bool_key() {
  local key="$1" value
  value="$(cfg_scalar "$key")"
  [ -z "$value" ] && return 0
  case "$value" in
    true|false) ;;
    *)
      emit_result "hard" "config-boolean-invalid" \
        "$key 必须显式为 true/false，当前为：$value" \
        "修正配置后重试"
      ;;
  esac
}

for bool_key in \
  task_source_contract.enabled \
  context_sync.enabled \
  dec_ref_sync.enabled \
  markdown_link_broken.enabled \
  active_zone_residue.enabled \
  context_truth.enabled; do
  validate_bool_key "$bool_key"
done

# 一旦配置了 checker 的业务字段，enabled 就必须显式出现；缺失不是 false，也不是兼容成功。
require_enabled_when_configured() {
  local section="$1" key="$2" configured
  configured="$(printf '%s\n' "$_CFG_FLAT" | awk -F= -v prefix="$section." '!found && index($1,prefix)==1 && $1 != prefix "enabled" {print; found=1}')"
  if [ -n "$configured" ] && [ -z "$(cfg_scalar "$key")" ]; then
    emit_result "hard" "config-ambiguous-enable" \
      "${section} 已声明规则但缺少 ${key}，不能推断为启用或禁用" \
      "显式填写 $key: true/false"
  fi
}

require_enabled_when_configured task_source_contract task_source_contract.enabled
require_enabled_when_configured context_sync context_sync.enabled
require_enabled_when_configured dec_ref_sync dec_ref_sync.enabled
require_enabled_when_configured markdown_link_broken markdown_link_broken.enabled
require_enabled_when_configured active_zone_residue active_zone_residue.enabled
require_enabled_when_configured context_truth context_truth.enabled

checker_enable_key() {
  case "$1" in
    context-sync) printf '%s' context_sync.enabled ;;
    decision-sync) printf '%s' dec_ref_sync.enabled ;;
    markdown-link-broken) printf '%s' markdown_link_broken.enabled ;;
    active-zone-residue) printf '%s' active_zone_residue.enabled ;;
    context-truth) printf '%s' context_truth.enabled ;;
    *) return 1 ;;
  esac
}

required="$(cfg_scalar config_contract.required_checkers)"
for checker in $required; do
  if ! key="$(checker_enable_key "$checker")"; then
    emit_result "hard" "config-required-checker-unknown" \
      "config_contract.required_checkers 含未知或不可开关 checker：$checker" \
      "使用 context-sync、decision-sync、markdown-link-broken、active-zone-residue、context-truth"
    continue
  fi
  if [ "$(cfg_scalar "$key")" != "true" ]; then
    emit_result "hard" "config-required-checker-disabled" \
      "必需 checker $checker 未显式启用（$key != true）" \
      "启用该 checker，或从 required_checkers 移除并说明为何不要求验证"
  fi
done

# 自动发现时，多份候选配置若内容不同，较高优先级文件会静默遮蔽较新 profile。
if [ -z "${DOC_CURATOR_CONFIG:-}" ]; then
  repo_base="$(basename "$REPO_ROOT")"
  candidates=(
    "$REPO_ROOT/doc-curator.yaml"
    "$REPO_ROOT/.doc-curator.yaml"
    "$SKILL_ROOT/config/${repo_base}.local.yaml"
    "$SKILL_ROOT/config/${repo_base}.yaml"
  )
  selected_sha="$(sha256_file "$CONFIG_FILE")"
  for candidate in "${candidates[@]}"; do
    [ -f "$candidate" ] || continue
    [ "$candidate" = "$CONFIG_FILE" ] && continue
    candidate_sha="$(sha256_file "$candidate")"
    if [ "$candidate_sha" != "$selected_sha" ]; then
      emit_result "hard" "config-shadowed-divergence" \
        "已选 $CONFIG_SOURCE 配置，但另有内容不同的候选配置被遮蔽：$candidate" \
        "只保留一个项目配置权威源，或用 --config 显式选择并移除重复副本"
    fi
  done
fi

if finish_checker; then
  emit_result "ok" "config-contract" \
    "配置合同通过：schema=${schema}，source=${CONFIG_SOURCE}，file=$(basename "$CONFIG_FILE")"
  exit 0
fi
finish_checker
