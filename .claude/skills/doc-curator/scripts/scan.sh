#!/usr/bin/env bash
# doc-curator 只读体检入口。stdout 只输出 JSONL，stderr 输出人类可读日志。
# 退出码：0=无阻断项，1=hard，2=adaptive（且无 hard），64/65/66/78=调用或环境错误。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASH_BIN="${BASH:-bash}"

usage() {
  cat >&2 <<'EOF'
用法：scan.sh [--repo PATH] [--config FILE] [--only CHECK]
               [--working-tree | --since REF | --range A..B]
               [--report FILE.md]
               [--profile merge-gate [--jsonl-out FILE]]
       scan.sh --init-baseline [--repo PATH] [--config FILE]

CHECK: config | tasks | decisions | files | context-sync | decision-sync |
       markdown-link-broken | active-zone-residue | context-truth | all

--report FILE.md：跑完后把 JSONL 结果渲染成人读 Markdown 报告写入 FILE。
                  stdout 仍为 JSONL（报告写文件，不改变输出契约）。
--profile merge-gate：单次 scan 输出一行紧凑 JSON 门禁摘要（固定 schema
                  doc-curator.merge-gate.v1：精确 base/head/range、配置来源、
                  hard/adaptive/soft/ok 计数、阻断 rule ID、next_action），
                  完整 JSONL 默认不进入 stdout；需要留档时用 --jsonl-out FILE
                  显式写入指定文件。退出码与 legacy 完全一致。
EOF
}

pre_json_escape() {
  local value="$1"
  value=${value//\\/\\\\}; value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}; value=${value//$'\r'/\\r}; value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

# merge-gate 模式的失败收口：与成功路径同一固定 schema，status=error，
# 退出码语义与 legacy 完全一致。可在 common.sh 之前调用（此时用原始参数兜底）。
gate_emit_error() {
  local rule_id="$1" message="$2" rc="$3"
  local repo_name config_name config_src scope_type scope_range
  repo_name="$(basename "${REPO_ROOT:-${REPO_IN:-}}" 2>/dev/null || true)"
  config_name="$(basename "${CONFIG_FILE:-${CONFIG_IN:-}}" 2>/dev/null || true)"
  config_src="${CONFIG_SOURCE:-unresolved}"
  if [ "${WORKING_TREE:-0}" -eq 1 ]; then
    scope_type="working-tree"; scope_range="working-tree"
  elif [ -n "${RANGE:-}" ]; then
    scope_type="range"; scope_range="$RANGE"
  elif [ -n "${SINCE:-}" ]; then
    scope_type="since"; scope_range="--since $SINCE"
  else
    scope_type="full"; scope_range=""
  fi
  printf '%s\n' "{\"summary_schema\":\"doc-curator.merge-gate.v1\",\"profile\":\"merge-gate\",\"status\":\"error\",\"repo\":\"$(pre_json_escape "$repo_name")\",\"config_file\":\"$(pre_json_escape "$config_name")\",\"config_source\":\"$(pre_json_escape "$config_src")\",\"scope_type\":\"$scope_type\",\"base\":\"\",\"head\":\"\",\"range\":\"$(pre_json_escape "$scope_range")\",\"hard_count\":0,\"adaptive_count\":0,\"soft_count\":0,\"ok_count\":0,\"blocking_rule_ids\":[\"$(pre_json_escape "$rule_id")\"],\"adaptive_rule_ids\":[],\"error\":\"$(pre_json_escape "$message")\",\"next_action\":\"fix-invocation\",\"exit_code\":$rc}"
  exit "$rc"
}

fatal_before_common() {
  local rule_id="$1" message="$2" rc="$3"
  if [ "${GATE_MODE:-0}" -eq 1 ]; then
    gate_emit_error "$rule_id" "$message" "$rc"
  fi
  printf '%s' '{"checker":"scan","severity":"hard","rule_id":"'
  printf '%s' "$(pre_json_escape "$rule_id")"
  printf '%s' '","message":"'
  printf '%s' "$(pre_json_escape "$message")"
  printf '%s\n' '","suggestion":""}'
  exit "$rc"
}

need_value() {
  [ "$#" -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ] || {
    usage
    fatal_before_common "invalid-arguments" "$1 缺少参数值" 64
  }
}

CONFIG_IN=""; REPO_IN=""; SINCE=""; RANGE=""; WORKING_TREE=0
ONLY="all"; INIT_BASELINE=0; REPORT_OUT=""
PROFILE=""; JSONL_OUT=""
# gate 模式预扫描：显式 --profile merge-gate 时，所有失败路径都以紧凑摘要行收口。
GATE_MODE=0
_gate_prev=""
for _gate_arg in "$@"; do
  if [ "$_gate_prev" = "--profile" ] && [ "$_gate_arg" = "merge-gate" ]; then
    GATE_MODE=1
  fi
  _gate_prev="$_gate_arg"
done
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) need_value "$@"; CONFIG_IN="$2"; shift 2 ;;
    --repo) need_value "$@"; REPO_IN="$2"; shift 2 ;;
    --since) need_value "$@"; SINCE="$2"; shift 2 ;;
    --range) need_value "$@"; RANGE="$2"; shift 2 ;;
    --only) need_value "$@"; ONLY="$2"; shift 2 ;;
    --report) need_value "$@"; REPORT_OUT="$2"; shift 2 ;;
    --profile) need_value "$@"; PROFILE="$2"; shift 2 ;;
    --jsonl-out) need_value "$@"; JSONL_OUT="$2"; shift 2 ;;
    --working-tree) WORKING_TREE=1; shift ;;
    --init-baseline) INIT_BASELINE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; fatal_before_common "invalid-arguments" "未知参数：$1" 64 ;;
  esac
done

case "$PROFILE" in
  ""|merge-gate) ;;
  *) fatal_before_common "invalid-arguments" "未知 profile：${PROFILE}（仅支持 merge-gate）" 64 ;;
esac
if [ "$PROFILE" = "merge-gate" ]; then
  if [ "$INIT_BASELINE" -eq 1 ] || [ -n "$REPORT_OUT" ]; then
    fatal_before_common "invalid-arguments" "--profile merge-gate 不可与 --init-baseline/--report 合用" 64
  fi
fi
if [ -n "$JSONL_OUT" ] && [ "$GATE_MODE" -ne 1 ]; then
  fatal_before_common "invalid-arguments" "--jsonl-out 只能与 --profile merge-gate 合用" 64
fi

case "$ONLY" in
  config|tasks|decisions|files|context-sync|decision-sync|markdown-link-broken|active-zone-residue|context-truth|all) ;;
  *) fatal_before_common "invalid-checker" "未知 checker：$ONLY" 64 ;;
esac

if [ -n "$CONFIG_IN" ] && [ ! -f "$CONFIG_IN" ]; then
  fatal_before_common "config-not-found" "显式配置不存在：$CONFIG_IN" 66
fi
if [ -n "$REPO_IN" ] && [ ! -d "$REPO_IN" ]; then
  fatal_before_common "repo-not-found" "被体检项目不存在：$REPO_IN" 66
fi
if [ "$WORKING_TREE" -eq 1 ] && { [ -n "$SINCE" ] || [ -n "$RANGE" ]; }; then
  fatal_before_common "invalid-scope" "--working-tree 不可与 --since/--range 合用" 64
fi
if [ -n "$SINCE" ] && [ -n "$RANGE" ]; then
  fatal_before_common "invalid-scope" "--since 不可与 --range 合用" 64
fi
if [ "$ONLY" != "all" ] && [ "$ONLY" != "context-sync" ] && \
   { [ "$WORKING_TREE" -eq 1 ] || [ -n "$SINCE" ] || [ -n "$RANGE" ]; }; then
  fatal_before_common "invalid-scope" "Git 范围参数只适用于 context-sync 或 all" 64
fi
if [ "$INIT_BASELINE" -eq 1 ] && { [ "$ONLY" != "all" ] || [ "$WORKING_TREE" -eq 1 ] || [ -n "$SINCE" ] || [ -n "$RANGE" ]; }; then
  fatal_before_common "invalid-arguments" "--init-baseline 只能与 --repo/--config 合用" 64
fi
if [ -n "$REPORT_OUT" ] && [ "$INIT_BASELINE" -eq 1 ]; then
  fatal_before_common "invalid-arguments" "--report 不可与 --init-baseline 合用" 64
fi

[ -n "$CONFIG_IN" ] && export DOC_CURATOR_CONFIG="$CONFIG_IN"
[ -n "$REPO_IN" ] && export DOC_CURATOR_REPO="$REPO_IN"

# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"
export DOC_CURATOR_CHECKER_ID=scan

if ! _cfg_ensure_flat; then
  if [ "$GATE_MODE" -eq 1 ]; then
    gate_emit_error "config-invalid" "配置无法按 doc-curator YAML 子集解析：$CONFIG_FILE" 1
  fi
  emit_result "hard" "config-invalid" "配置无法按 doc-curator YAML 子集解析：$CONFIG_FILE" \
    "从 config/doc-curator.example.yaml 复制后再修改"
  exit 1
fi

print_header "doc-curator"
print_info "repo: $REPO_ROOT"
print_info "config: $CONFIG_FILE"
print_info "state: $STATE_FILE"

initialize_baseline() {
  local state_dir state_tmp measured_at config_sha first=1 rel file line_count
  local tasks_rel active_pattern active_count grep_rc baseline_error=0
  state_dir="$(dirname "$STATE_FILE")"
  mkdir -p "$state_dir"
  state_tmp="$(mktemp "$state_dir/.state.json.XXXXXX")"
  measured_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  config_sha="$(sha256_file "$CONFIG_FILE")"
  tasks_rel="$(cfg_file_by_role active-tasks)"
  active_pattern="$(cfg_rule_get adaptive_rules tasks-active-count active_count_pattern)"
  active_pattern="${active_pattern:-^### (ISS-[0-9]+|Task[# ]+T?[0-9A-Za-z-]+)}"
  if ! validate_ere "$active_pattern"; then
    rm -f "$state_tmp"
    emit_result "hard" "config-regex-invalid" "active_count_pattern 不是合法扩展正则" \
      "修正 adaptive_rules.tasks-active-count.active_count_pattern"
    return 1
  fi

  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "project_id": "%s",\n' "$(json_escape "$PROJECT_ID")"
    printf '  "config_sha256": "%s",\n' "$(json_escape "$config_sha")"
    printf '  "measured_at": "%s",\n' "$measured_at"
    printf '  "baselines": {\n'
    while IFS= read -r rel; do
      [ -n "$rel" ] || continue
      file="$REPO_ROOT/$rel"
      [ -f "$file" ] || continue
      line_count="$(wc -l < "$file" | tr -d ' ')"
      if [ "$first" -eq 0 ]; then printf ',\n'; fi
      first=0
      printf '    "'
      printf '%s' "$(json_escape "$rel")"
      printf '": {"line_count": '
      printf '%s' "$line_count"
      if [ "$rel" = "$tasks_rel" ]; then
        set +e
        active_count="$(grep -cE "$active_pattern" "$file")"
        grep_rc=$?
        set -e
        if [ "$grep_rc" -gt 1 ]; then
          baseline_error=1
          break
        fi
        printf ', "active_task_count": %s' "${active_count:-0}"
      fi
      printf '}'
    done < <(cfg_file_paths)
    printf '\n  }\n}\n'
  } > "$state_tmp"
  if [ "$baseline_error" -eq 1 ]; then
    rm -f "$state_tmp"
    emit_result "hard" "baseline-read-error" "建立基线时无法读取任务文件" "检查文件权限后重试"
    return 1
  fi
  if ! grep -q '"line_count"' "$state_tmp"; then
    rm -f "$state_tmp"
    emit_result "hard" "baseline-empty" "配置中的监控文件均不存在，未写入空基线" \
      "修正 files[].path 后重试"
    return 1
  fi
  mv "$state_tmp" "$STATE_FILE"
  emit_result "ok" "baseline-initialized" "已为当前项目建立独立基线：$STATE_FILE"
}

CTX_ARGS=()
[ "$WORKING_TREE" -eq 1 ] && CTX_ARGS+=(--working-tree)
if [ -n "$RANGE" ]; then
  CTX_ARGS+=(--range "$RANGE")
elif [ -n "$SINCE" ]; then
  CTX_ARGS+=(--since "$SINCE")
fi

RESULTS_FILE="$(mktemp)"
TEMP_FILES=("$RESULTS_FILE")
cleanup() { local f; for f in "${TEMP_FILES[@]}"; do rm -f "$f"; done; }
trap cleanup EXIT

run_check() {
  local checker="$1" script out err rc line actual_checker has_hard has_adaptive expected_rc
  script="$SCRIPT_DIR/check-$checker.sh"
  case "$checker" in
    config) script="$SCRIPT_DIR/check-config-contract.sh" ;;
    decision-sync) script="$SCRIPT_DIR/check-dec-ref-sync.sh" ;;
  esac
  out="$(mktemp)"; err="$(mktemp)"; TEMP_FILES+=("$out" "$err")
  set +e
  if [ "$checker" = "context-sync" ]; then
    DOC_CURATOR_CHECKER_ID="$checker" "$BASH_BIN" "$script" "${CTX_ARGS[@]}" >"$out" 2>"$err"
  else
    DOC_CURATOR_CHECKER_ID="$checker" "$BASH_BIN" "$script" >"$out" 2>"$err"
  fi
  rc=$?
  set -e
  [ ! -s "$err" ] || cat "$err" >&2

  if [ "$rc" -gt 2 ]; then
    emit_result "hard" "checker-execution-error" "$checker 异常退出（code=${rc}）" \
      "单独运行对应 checker 并检查 stderr" >> "$RESULTS_FILE"
    return 0
  fi
  if [ ! -s "$out" ]; then
    emit_result "hard" "checker-empty-output" "$checker 未输出结构化结果" \
      "修复 checker，确保每次至少输出一条 JSONL" >> "$RESULTS_FILE"
    return 0
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    if ! validate_result_line "$line"; then
      emit_result "hard" "checker-invalid-output" "$checker 输出了非法 JSONL" \
        "checker 的 stdout 只能使用 emit_result" >> "$RESULTS_FILE"
      return 0
    fi
    actual_checker="$(result_field "$line" checker)"
    if [ "$actual_checker" != "$checker" ]; then
      emit_result "hard" "checker-identity-mismatch" "$checker 返回了错误的 checker 身份：$actual_checker" \
        "由 scan.sh 注入 DOC_CURATOR_CHECKER_ID，不要覆盖" >> "$RESULTS_FILE"
      return 0
    fi
  done < "$out"

  cat "$out" >> "$RESULTS_FILE"
  has_hard=0; has_adaptive=0; expected_rc=0
  grep -q '"severity":"hard"' "$out" && has_hard=1
  grep -q '"severity":"adaptive"' "$out" && has_adaptive=1
  if [ "$has_hard" -eq 1 ]; then expected_rc=1
  elif [ "$has_adaptive" -eq 1 ]; then expected_rc=2
  fi
  if [ "$rc" -ne "$expected_rc" ]; then
    emit_result "hard" "checker-exit-mismatch" \
      "$checker finding 严重度要求退出 ${expected_rc}，实际退出 $rc" \
      "checker 必须 hard→1、仅 adaptive→2、否则→0" >> "$RESULTS_FILE"
  fi
}

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

# merge-gate 摘要用：按严重度去重提取 rule ID，保持首现顺序并转 JSON 数组。
gate_json_ids() {
  local severity="$1" out="" rule_id
  while IFS= read -r rule_id; do
    [ -n "$rule_id" ] || continue
    out="${out},\"$(json_escape "$rule_id")\""
  done < <(grep "\"severity\":\"$severity\"" "$RESULTS_FILE" | sed -n 's/.*"rule_id":"\([^"]*\)".*/\1/p' | awk '!seen[$0]++')
  printf '[%s]' "${out#,}"
}

# 单次 scan 收口为紧凑门禁摘要：不再向 stdout 输出完整 JSONL（可选 --jsonl-out 落盘），
# 退出码仍由 hard/adaptive 计数决定，与 legacy 完全一致。
run_merge_gate_summary() {
  local ok_count="$1" hard_count="$2" adaptive_count="$3" soft_count="$4"
  local next_action blocking_json adaptive_json gate_exit=0

  # 门禁缺项目配置时失败闭合：bundled-default 兜底不构成 merge gate 证据，
  # 也不自动生成任何 *.local.yaml，交回调用方显式提供。
  if [ "$CONFIG_SOURCE" = "bundled-default" ]; then
    DOC_CURATOR_CHECKER_ID=scan emit_result "hard" "config-required" \
      "merge-gate 缺少项目配置：已自动回落 bundled-default（NOT_VERIFIED，不作为门禁通过证据）" \
      "显式传 --config，或提供项目根/Skill 内 schema v2 项目配置；不要自动生成 *.local.yaml" >> "$RESULTS_FILE"
    hard_count=$((hard_count + 1))
  fi

  # 精确 base/head：range/since 解析失败必须阻断，不得以未解析范围报告门禁结论。
  GATE_SCOPE_TYPE="full"; GATE_SCOPE_BASE=""; GATE_SCOPE_HEAD=""; GATE_SCOPE_RANGE=""
  if [ "$WORKING_TREE" -eq 1 ]; then
    GATE_SCOPE_TYPE="working-tree"; GATE_SCOPE_RANGE="working-tree"
    GATE_SCOPE_HEAD="$(git -C "$REPO_ROOT" rev-parse --verify -q HEAD 2>/dev/null || true)"
  elif [ -n "$RANGE" ]; then
    GATE_SCOPE_TYPE="range"; GATE_SCOPE_RANGE="$RANGE"
    GATE_SCOPE_BASE="$(git -C "$REPO_ROOT" rev-parse --verify -q "${RANGE%%..*}^{commit}" 2>/dev/null || true)"
    GATE_SCOPE_HEAD="$(git -C "$REPO_ROOT" rev-parse --verify -q "${RANGE#*..}^{commit}" 2>/dev/null || true)"
  elif [ -n "$SINCE" ]; then
    GATE_SCOPE_TYPE="since"; GATE_SCOPE_RANGE="--since $SINCE"
    GATE_SCOPE_BASE="$(git -C "$REPO_ROOT" rev-parse --verify -q "${SINCE}^{commit}" 2>/dev/null || true)"
    GATE_SCOPE_HEAD="$(git -C "$REPO_ROOT" rev-parse --verify -q "HEAD^{commit}" 2>/dev/null || true)"
  fi
  if { [ "$GATE_SCOPE_TYPE" = "range" ] || [ "$GATE_SCOPE_TYPE" = "since" ]; } \
     && { [ -z "$GATE_SCOPE_BASE" ] || [ -z "$GATE_SCOPE_HEAD" ]; }; then
    DOC_CURATOR_CHECKER_ID=scan emit_result "hard" "merge-gate-range-unresolved" \
      "无法解析精确 base/head：${GATE_SCOPE_RANGE}（base=${GATE_SCOPE_BASE:-未解析} head=${GATE_SCOPE_HEAD:-未解析}）" \
      "用可解析的 Git revision 组成 A..B；预提交验收改用 --working-tree" >> "$RESULTS_FILE"
    hard_count=$((hard_count + 1))
  fi

  # 完整 JSONL 是显式请求的旁路留档，不进入主 Agent 上下文；写失败按 hard 闭合。
  if [ -n "$JSONL_OUT" ]; then
    local jsonl_tmp jsonl_ok=0
    if jsonl_tmp="$(mktemp "$JSONL_OUT.tmp.XXXXXX" 2>/dev/null)"; then
      if cat "$RESULTS_FILE" > "$jsonl_tmp" && [ -s "$jsonl_tmp" ] && mv "$jsonl_tmp" "$JSONL_OUT"; then
        jsonl_ok=1
        print_info "jsonl: $JSONL_OUT"
      else
        rm -f "$jsonl_tmp"
      fi
    fi
    if [ "$jsonl_ok" -ne 1 ]; then
      DOC_CURATOR_CHECKER_ID=scan emit_result "hard" "jsonl-output-error" \
        "完整 JSONL 写入失败：$JSONL_OUT" "确认目标目录可写后重试" >> "$RESULTS_FILE"
      hard_count=$((hard_count + 1))
    fi
  fi

  blocking_json="$(gate_json_ids hard)"
  adaptive_json="$(gate_json_ids adaptive)"

  if printf '%s' "$blocking_json" | grep -q '"config-required"'; then
    next_action="provide-config"
  elif [ "$hard_count" -gt 0 ]; then
    next_action="resolve-hard"
  elif [ "$adaptive_count" -gt 0 ]; then
    next_action="review-adaptive"
  else
    next_action="pass"
  fi

  [ "$hard_count" -gt 0 ] && gate_exit=1
  if [ "$gate_exit" -eq 0 ] && [ "$adaptive_count" -gt 0 ]; then gate_exit=2; fi

  printf '%s\n' "{\"summary_schema\":\"doc-curator.merge-gate.v1\",\"profile\":\"merge-gate\",\"status\":\"complete\",\"repo\":\"$(json_escape "$(basename "$REPO_ROOT")")\",\"config_file\":\"$(json_escape "$(basename "$CONFIG_FILE")")\",\"config_source\":\"$(json_escape "$CONFIG_SOURCE")\",\"scope_type\":\"$GATE_SCOPE_TYPE\",\"base\":\"$GATE_SCOPE_BASE\",\"head\":\"$GATE_SCOPE_HEAD\",\"range\":\"$(json_escape "$GATE_SCOPE_RANGE")\",\"hard_count\":$hard_count,\"adaptive_count\":$adaptive_count,\"soft_count\":$soft_count,\"ok_count\":$ok_count,\"blocking_rule_ids\":$blocking_json,\"adaptive_rule_ids\":$adaptive_json,\"next_action\":\"$next_action\",\"exit_code\":$gate_exit}"
  print_info "summary: ok=$ok_count hard=$hard_count adaptive=$adaptive_count soft=$soft_count"
  exit "$gate_exit"
}

# 配置合同总是第一个执行；旧 schema 或遮蔽漂移时不运行后续 checker。
run_check config
if [ "$INIT_BASELINE" -eq 1 ]; then
  cat "$RESULTS_FILE"
  # 使用相同合同确认 schema 2/3、能力声明与配置有效性，验证失败不写状态。
  if grep -q '"severity":"hard"' "$RESULTS_FILE"; then exit 1; fi
  initialize_baseline
  exit $?
fi
if ! grep -q '"severity":"hard"' "$RESULTS_FILE"; then
  if [ "$ONLY" = "all" ]; then
    CHECKERS=(tasks decisions files context-sync decision-sync markdown-link-broken active-zone-residue context-truth)
  elif [ "$ONLY" = "config" ]; then
    CHECKERS=()
  else
    CHECKERS=("$ONLY")
    if enable_key="$(checker_enable_key "$ONLY")" && [ "$(cfg_scalar "$enable_key")" != "true" ]; then
      DOC_CURATOR_CHECKER_ID=scan emit_result "hard" "checker-disabled" \
        "显式请求的 checker $ONLY 未启用（$enable_key != true）" \
        "在配置中显式启用后再把它作为验收证据" >> "$RESULTS_FILE"
      CHECKERS=()
    fi
  fi
  for checker in "${CHECKERS[@]}"; do run_check "$checker"; done
fi

# 报告是显式请求的交付物；生成失败必须进入 JSONL 并使 scan fail-closed。
if [ -n "$REPORT_OUT" ]; then
  REPORT_RENDER="$SCRIPT_DIR/render-report.sh"
  report_err="$(mktemp)"; TEMP_FILES+=("$report_err")
  report_tmp=""
  if [ ! -f "$REPORT_RENDER" ]; then
    DOC_CURATOR_CHECKER_ID=scan emit_result "hard" "report-generation-error" \
      "render-report.sh 不存在：$REPORT_RENDER" "恢复报告渲染器后重试" >> "$RESULTS_FILE"
  elif ! report_tmp="$(mktemp "${REPORT_OUT}.tmp.XXXXXX" 2>/dev/null)"; then
    DOC_CURATOR_CHECKER_ID=scan emit_result "hard" "report-generation-error" \
      "无法在报告目标目录创建临时文件：$REPORT_OUT" "创建目标目录或修正 --report 路径" >> "$RESULTS_FILE"
  else
    TEMP_FILES+=("$report_tmp")
    set +e
    DOC_CURATOR_REPORT_PROJECT="$(basename "$REPO_ROOT")" \
    DOC_CURATOR_REPORT_CONFIG="$(basename "$CONFIG_FILE")" \
      "$BASH_BIN" "$REPORT_RENDER" "$RESULTS_FILE" > "$report_tmp" 2>"$report_err"
    render_rc=$?
    set -e
    if [ "$render_rc" -ne 0 ] || [ ! -s "$report_tmp" ] || ! mv "$report_tmp" "$REPORT_OUT"; then
      DOC_CURATOR_CHECKER_ID=scan emit_result "hard" "report-generation-error" \
        "报告生成失败（rc=${render_rc}）：$REPORT_OUT" "检查目标目录权限和 render-report.sh stderr" >> "$RESULTS_FILE"
      [ ! -s "$report_err" ] || cat "$report_err" >&2
    else
      print_info "report: $REPORT_OUT"
    fi
  fi
fi

ok_count="$(grep -c '"severity":"ok"' "$RESULTS_FILE" || true)"; ok_count="${ok_count:-0}"
hard_count="$(grep -c '"severity":"hard"' "$RESULTS_FILE" || true)"; hard_count="${hard_count:-0}"
adaptive_count="$(grep -c '"severity":"adaptive"' "$RESULTS_FILE" || true)"; adaptive_count="${adaptive_count:-0}"
soft_count="$(grep -c '"severity":"soft"' "$RESULTS_FILE" || true)"; soft_count="${soft_count:-0}"

# 显式 merge-gate 模式：单行紧凑摘要收口（不打印完整 JSONL）。
if [ "$GATE_MODE" -eq 1 ]; then
  run_merge_gate_summary "$ok_count" "$hard_count" "$adaptive_count" "$soft_count"
fi

cat "$RESULTS_FILE"
print_info "summary: ok=$ok_count hard=$hard_count adaptive=$adaptive_count soft=$soft_count"

[ "$hard_count" -gt 0 ] && exit 1
[ "$adaptive_count" -gt 0 ] && exit 2
exit 0
