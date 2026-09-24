#!/usr/bin/env bash
# TASKS.md 体检：AgentCMD 任务源合同、活跃任务数、进度日志 trim、归档指针。
# 文件路径、状态合同、字段语义与阈值均从 config 读取。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"
# 在条件/命令替换之前加载一次；读取失败不得被默认值或 process substitution 吞掉。
_cfg_ensure_flat

TASKS_REL="$(cfg_file_by_role active-tasks)"
TASKS_FILE="$REPO_ROOT/${TASKS_REL:-docs/TASKS.md}"
DECISIONS_REL="$(cfg_file_by_role decision-log)"
DECISIONS_BASE="$(basename "${DECISIONS_REL:-DECISIONS.md}")"
TASK_SOURCE_ENABLED="$(cfg_scalar task_source_contract.enabled)"
TASK_SOURCE_ENABLED="${TASK_SOURCE_ENABLED:-false}"

if [ ! -f "$TASKS_FILE" ]; then
  if [ "$TASK_SOURCE_ENABLED" = "true" ]; then
    emit_result "hard" "task-source-file-missing" \
      "已启用任务源合同，但 ${TASKS_REL:-docs/TASKS.md} 不存在" \
      "修正 files 中 active-tasks 路径，或关闭 task_source_contract"
    exit 1
  else
    emit_result "soft" "tasks-file-missing" \
      "${TASKS_REL:-docs/TASKS.md} 不存在" \
      "在 config files 段修正 active-tasks 路径或创建该文件"
    exit 0
  fi
fi

run_task_source_contract() {
  [ "$TASK_SOURCE_ENABLED" = "true" ] || return 0

  local task_id_pattern task_id_exclude_pattern card_heading_pattern empty_queue_pattern status_label_pattern
  local draft_tokens ready_tokens in_progress_tokens blocked_tokens review_tokens
  local done_tokens cancelled_tokens ready_required review_required done_required
  local blocked_required cancelled_required idx field_id field_pattern field_label
  local all_required="" missing_config="" line clean cell row_id row_status
  local current_card_id="" status id previous_status card_content required missing rest remainder
  local label start end i j in_progress_count=0 checked_count=0 hard_count=0

  task_id_pattern="$(cfg_scalar task_source_contract.task_id_pattern)"
  task_id_pattern="${task_id_pattern:-(Task-[0-9]+|[A-Z][A-Z0-9_-]*-[0-9]+)}"
  task_id_exclude_pattern="$(cfg_scalar task_source_contract.task_id_exclude_pattern)"
  task_id_exclude_pattern="${task_id_exclude_pattern:-^(DEC|ADR)-}"
  card_heading_pattern="$(cfg_scalar task_source_contract.card_heading_pattern)"
  card_heading_pattern="${card_heading_pattern:-^###[[:space:]]}"
  empty_queue_pattern="$(cfg_scalar task_source_contract.empty_queue_pattern)"
  empty_queue_pattern="${empty_queue_pattern:-(当前无任务|无可执行任务|No active tasks)}"
  status_label_pattern="$(cfg_scalar task_source_contract.status_label_pattern)"
  status_label_pattern="${status_label_pattern:-(^[[:space:]]*[-*][[:space:]]+[*]*(状态|Status)[*]*[：:]|^[[:space:]]*[|][[:space:]]*(状态|Status)[[:space:]]*[|])}"

  draft_tokens="$(cfg_scalar task_source_contract.status_tokens.draft)"
  draft_tokens="${draft_tokens:-DRAFT 草案}"
  ready_tokens="$(cfg_scalar task_source_contract.status_tokens.ready)"
  ready_tokens="${ready_tokens:-READY 就绪}"
  in_progress_tokens="$(cfg_scalar task_source_contract.status_tokens.in_progress)"
  in_progress_tokens="${in_progress_tokens:-IN_PROGRESS 进行中}"
  blocked_tokens="$(cfg_scalar task_source_contract.status_tokens.blocked)"
  blocked_tokens="${blocked_tokens:-BLOCKED 阻塞}"
  review_tokens="$(cfg_scalar task_source_contract.status_tokens.review)"
  review_tokens="${review_tokens:-REVIEW 待验收}"
  done_tokens="$(cfg_scalar task_source_contract.status_tokens.done)"
  done_tokens="${done_tokens:-DONE 已完成}"
  cancelled_tokens="$(cfg_scalar task_source_contract.status_tokens.cancelled)"
  cancelled_tokens="${cancelled_tokens:-CANCELLED 已取消}"

  ready_required="$(cfg_scalar task_source_contract.requirements.ready)"
  ready_required="${ready_required:-rationale goal non_goal inputs_dependencies scope stop acceptance}"
  review_required="$(cfg_scalar task_source_contract.requirements.review)"
  review_required="${review_required:-deliverables evidence}"
  done_required="$(cfg_scalar task_source_contract.requirements.done)"
  done_required="${done_required:-evidence}"
  blocked_required="$(cfg_scalar task_source_contract.requirements.blocked)"
  blocked_required="${blocked_required:-blocked_reason}"
  cancelled_required="$(cfg_scalar task_source_contract.requirements.cancelled)"
  cancelled_required="${cancelled_required:-cancellation_reason}"
  all_required="$ready_required $review_required $done_required $blocked_required $cancelled_required"

  if ! validate_ere "$task_id_pattern" || ! validate_ere "$task_id_exclude_pattern" || \
     ! validate_ere "$card_heading_pattern" || \
     ! validate_ere "$empty_queue_pattern" || ! validate_ere "$status_label_pattern"; then
    emit_result "hard" "task-source-config-invalid" \
      "task_source_contract 的任务 ID、任务卡标题、空队列或状态标签正则非法" \
      "修正配置中的扩展正则后重试"
    return 1
  fi

  declare -A field_patterns=() field_labels=()
  while IFS= read -r idx; do
    [ -n "$idx" ] || continue
    field_id="$(cfg_list_field task_source_contract.fields "$idx" id)"
    field_pattern="$(cfg_list_field task_source_contract.fields "$idx" pattern)"
    field_label="$(cfg_list_field task_source_contract.fields "$idx" label)"
    if [ -z "$field_id" ] || [ -z "$field_pattern" ]; then
      missing_config="fields[$idx] 缺 id 或 pattern"
      break
    fi
    if ! validate_ere "$field_pattern"; then
      missing_config="字段 $field_id 的 pattern 非法"
      break
    fi
    field_patterns["$field_id"]="$field_pattern"
    field_labels["$field_id"]="${field_label:-$field_id}"
  done < <(cfg_list_indices task_source_contract.fields id)

  for field_id in $all_required; do
    if [ -z "${field_patterns[$field_id]:-}" ]; then
      missing_config="requirements 引用了未定义字段 $field_id"
      break
    fi
  done
  if [ -n "$missing_config" ]; then
    emit_result "hard" "task-source-config-invalid" "$missing_config" \
      "在 task_source_contract.fields 中补齐可机判字段定义"
    return 1
  fi

  mapfile -t task_lines < "$TASKS_FILE"
  declare -A card_starts=() card_ends=() task_statuses=() seen_ids=()
  declare -a ordered_ids=()

  # 先定位 H3 任务卡边界。字段标题可以自由命名，只有任务卡起点由配置约束。
  current_card_id=""
  for ((i = 0; i < ${#task_lines[@]}; i++)); do
    line="${task_lines[$i]}"
    if [[ "$line" =~ $card_heading_pattern ]]; then
      if [ -n "$current_card_id" ]; then
        card_ends["$current_card_id"]=$((i - 1))
      fi
      current_card_id=""
      if [[ "$line" =~ $task_id_pattern ]]; then
        id="${BASH_REMATCH[0]}"
        if ! [[ "$id" =~ $task_id_exclude_pattern ]]; then
          current_card_id="$id"
          card_starts["$current_card_id"]="$i"
        fi
      fi
    fi
  done
  if [ -n "$current_card_id" ]; then
    card_ends["$current_card_id"]=$((${#task_lines[@]} - 1))
  fi

  normalize_cell() {
    local value="$1"
    value="${value//\`/}"
    value="${value//\*/}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
  }

  status_from_cell() {
    local value="$1" candidate
    for candidate in $in_progress_tokens; do [ "$value" = "$candidate" ] && { printf 'IN_PROGRESS'; return 0; }; done
    for candidate in $blocked_tokens; do [ "$value" = "$candidate" ] && { printf 'BLOCKED'; return 0; }; done
    for candidate in $cancelled_tokens; do [ "$value" = "$candidate" ] && { printf 'CANCELLED'; return 0; }; done
    for candidate in $review_tokens; do [ "$value" = "$candidate" ] && { printf 'REVIEW'; return 0; }; done
    for candidate in $done_tokens; do [ "$value" = "$candidate" ] && { printf 'DONE'; return 0; }; done
    for candidate in $ready_tokens; do [ "$value" = "$candidate" ] && { printf 'READY'; return 0; }; done
    for candidate in $draft_tokens; do [ "$value" = "$candidate" ] && { printf 'DRAFT'; return 0; }; done
    return 1
  }

  # 行内最早出现的状态词。合法状态行允许携带演进叙述（“DONE（IN_PROGRESS 期间…）”），
  # 旧实现按固定优先级取词，叙述里的旧状态会覆盖声明，制造 status-conflict 误报。
  status_first_in() {
    local value="$1" candidate boundary head best="" best_off=-1 off
    for candidate in $in_progress_tokens $blocked_tokens $cancelled_tokens $review_tokens $done_tokens $ready_tokens $draft_tokens; do
      [ -n "$candidate" ] || continue
      if [[ "$candidate" =~ ^[A-Z_]+$ ]]; then
        boundary="(^|[^A-Z_])${candidate}([^A-Z_]|$)"
        [[ "$value" =~ $boundary ]] || continue
      else
        [[ "$value" == *"$candidate"* ]] || continue
      fi
      head="${value%%"$candidate"*}"
      off="${#head}"
      if [ "$best_off" -lt 0 ] || [ "$off" -lt "$best_off" ]; then
        best_off="$off"; best="$candidate"
      fi
    done
    [ -n "$best" ] || return 1
    status_from_cell "$best"
  }

  # 任务摘要行：仅状态词紧随任务 ID 才算显式声明；“由 READY 推进到 DONE”
  # 这类演进叙述不再被当成状态来源。
  status_prefix_of() {
    local value="$1" candidate boundary
    [ -n "$value" ] || return 1
    for candidate in $in_progress_tokens $blocked_tokens $cancelled_tokens $review_tokens $done_tokens $ready_tokens $draft_tokens; do
      [ -n "$candidate" ] || continue
      [[ "$value" == "$candidate"* ]] || continue
      if [[ "$candidate" =~ ^[A-Z_]+$ ]]; then
        boundary="^${candidate}([^A-Z_]|$)"
        [[ "$value" =~ $boundary ]] || continue
      fi
      status_from_cell "$candidate"
      return 0
    done
    return 1
  }

  record_status() {
    local record_id="$1" record_status="$2"
    [ -n "$record_id" ] && [ -n "$record_status" ] || return 0
    [[ "$record_id" =~ $task_id_exclude_pattern ]] && return 0
    previous_status="${task_statuses[$record_id]:-}"
    if [ -n "$previous_status" ] && [ "$previous_status" != "$record_status" ]; then
      emit_result "hard" "task-source-status-conflict" \
        "$record_id 同时声明 $previous_status 与 $record_status" \
        "统一当前队列与任务卡中的状态"
      hard_count=$((hard_count + 1))
      return 0
    fi
    task_statuses["$record_id"]="$record_status"
    if [ -z "${seen_ids[$record_id]:-}" ]; then
      ordered_ids+=("$record_id")
      seen_ids["$record_id"]=1
    fi
  }

  # 从队列表格、带状态的任务摘要及任务卡状态字段收集显式状态。
  current_card_id=""
  for line in "${task_lines[@]}"; do
    if [[ "$line" =~ $card_heading_pattern ]]; then
      current_card_id=""
      if [[ "$line" =~ $task_id_pattern ]]; then
        id="${BASH_REMATCH[0]}"
        if ! [[ "$id" =~ $task_id_exclude_pattern ]]; then current_card_id="$id"; fi
      fi
    fi

    if [[ "$line" == *"|"* ]]; then
      row_id=""; row_status=""
      IFS='|' read -r -a cells <<< "$line"
      for cell in "${cells[@]}"; do
        clean="$(normalize_cell "$cell")"
        if [[ "$clean" =~ ^${task_id_pattern}$ ]]; then row_id="${BASH_REMATCH[0]}"; fi
        if status="$(status_from_cell "$clean" 2>/dev/null)"; then row_status="$status"; fi
      done
      [ -z "$row_id" ] || [ -z "$row_status" ] || record_status "$row_id" "$row_status"
    fi

    if [[ "$line" =~ ^[[:space:]]*[-*] ]] && [[ "$line" =~ $task_id_pattern ]]; then
      id="${BASH_REMATCH[0]}"
      # 只认紧随任务 ID 的状态词；行内其余位置的状态词可能是演进叙述。
      rest="${line#*"$id"}"
      rest="${rest//\`/}"
      while [ -n "$rest" ]; do
        # ASCII 括号不能写进 case 方括号：解析器遇到 [[:space:]] 类自身的 ] 会提前
        # 终止括号跟踪，其后未引用的 ( 直接报语法错误（整脚本无法解析）。改为
        # 引号包裹的替代模式；全角括号是普通字节，可留在括号内。
        case "$rest" in
          [[:space:]:：、，*（）-]*|"("*|")"*) rest="${rest:1}" ;;
          *) break ;;
        esac
      done
      if status="$(status_prefix_of "$rest" 2>/dev/null)"; then record_status "$id" "$status"; fi
    fi

    if [ -n "$current_card_id" ] && [[ "$line" =~ $status_label_pattern ]]; then
      # 只在状态标签之后找状态词，且取最早出现者；标签行允许携带演进叙述。
      remainder="${line#"${BASH_REMATCH[0]}"}"
      if status="$(status_first_in "$remainder" 2>/dev/null)"; then record_status "$current_card_id" "$status"; fi
    fi
  done

  if [ "${#ordered_ids[@]}" -eq 0 ]; then
    if grep -qE "$empty_queue_pattern" "$TASKS_FILE"; then
      emit_result "ok" "task-source-empty-queue" \
        "任务源明确声明当前无任务" \
        ""
    else
      emit_result "hard" "task-source-status-missing" \
        "任务源未发现可识别的显式任务状态，也未声明空队列" \
        "为当前任务声明 DRAFT、READY、IN_PROGRESS、BLOCKED、REVIEW、DONE 或 CANCELLED"
      hard_count=$((hard_count + 1))
    fi
    return 0
  fi

  for id in "${ordered_ids[@]}"; do
    status="${task_statuses[$id]}"
    required=""
    case "$status" in
      READY|IN_PROGRESS) required="$ready_required" ;;
      REVIEW) required="$review_required" ;;
      DONE) required="$done_required" ;;
      BLOCKED) required="$blocked_required" ;;
      CANCELLED) required="$cancelled_required" ;;
      DRAFT) required="" ;;
    esac
    [ "$status" != "IN_PROGRESS" ] || in_progress_count=$((in_progress_count + 1))
    [ -n "$required" ] || continue
    checked_count=$((checked_count + 1))

    if [ -z "${card_starts[$id]:-}" ]; then
      emit_result "hard" "task-source-card-missing" \
        "${id} 状态为 ${status}，但没有对应 H3 任务卡" \
        "补充包含 ${id} 的完整任务卡，或将未成熟条目退回 DRAFT"
      hard_count=$((hard_count + 1))
      continue
    fi

    start="${card_starts[$id]}"; end="${card_ends[$id]}"; card_content=""
    for ((j = start; j <= end; j++)); do card_content+="${task_lines[$j]}"$'\n'; done
    missing=""
    for field_id in $required; do
      if ! printf '%s' "$card_content" | grep -qE "${field_patterns[$field_id]}"; then
        label="${field_labels[$field_id]}"
        if [ -z "$missing" ]; then missing="$label"; else missing="${missing}、${label}"; fi
      fi
    done
    if [ -n "$missing" ]; then
      emit_result "hard" "task-source-card-incomplete" \
        "${id}（${status}）缺少：${missing}" \
        "补齐任务卡后再进入或维持 ${status}"
      hard_count=$((hard_count + 1))
    fi
  done

  if [ "$in_progress_count" -gt 1 ]; then
    emit_result "adaptive" "task-source-multiple-in-progress" \
      "同时存在 $in_progress_count 个 IN_PROGRESS 任务" \
      "确认是否确需并行；否则只保留当前主任务为 IN_PROGRESS"
  fi
  if [ "$hard_count" -eq 0 ]; then
    emit_result "ok" "task-source-contract" \
      "识别 ${#ordered_ids[@]} 个有状态任务，检查 $checked_count 张需完整合同的任务卡" \
      ""
  fi
}

run_task_source_contract

PROGRESS_LIMIT="$(cfg_rule_get hard_rules tasks-progress-log-trim limit)"
PROGRESS_LIMIT="${PROGRESS_LIMIT:-5}"

# 1. 进度日志条数（仅在 config 显式启用时检查）
if cfg_rule_enabled hard_rules tasks-progress-log-trim; then
  progress_count=$(grep -cE '^- [0-9]{4}-[0-9]{2}-[0-9]{2}：' "$TASKS_FILE" || true)
  if [ "${progress_count:-0}" -gt "$PROGRESS_LIMIT" ]; then
    emit_result "hard" "tasks-progress-log-trim" \
      "进度日志有 $progress_count 条，超过硬性上限 $PROGRESS_LIMIT 条" \
      "将最早的条目迁移到 ${DECISIONS_REL:-docs/DECISIONS.md} 工作日志"
  else
    emit_result "ok" "tasks-progress-log-trim" \
      "进度日志 $progress_count 条，符合 ≤ $PROGRESS_LIMIT"
  fi
fi

# 2. 旧格式活跃任务卡数（自适应基线 × 1.5）。
# 启用 task_source_contract 时，状态合同已区分开放/草案/完成任务；
# 不再用 H3 标题总数冒充活跃任务数，避免 DONE 历史积累触发误报。
# v0.5.1：pattern 改读 config adaptive_rules.tasks-active-count.active_count_pattern。
#   默认值扩展为兼容 ISS-N + Task#N + Task TN/TNN 三类编号（修复 CHANGELOG 0.3.1 已知盲区）。
#   config 未配置时使用扩展默认值，兼容 ISS-N、Task#N 与 Task TN。
if [ "$TASK_SOURCE_ENABLED" != "true" ]; then
  ACTIVE_PATTERN="$(cfg_rule_get adaptive_rules tasks-active-count active_count_pattern)"
  ACTIVE_PATTERN="${ACTIVE_PATTERN:-^### (ISS-[0-9]+|Task[# ]+T?[0-9A-Za-z-]+)}"
  if ! validate_ere "$ACTIVE_PATTERN"; then
    emit_result "hard" "config-regex-invalid" \
      "tasks-active-count.active_count_pattern 不是合法扩展正则" \
      "修正 config 后重试"
    exit 1
  fi
  set +e
  active_count=$(grep -cE "$ACTIVE_PATTERN" "$TASKS_FILE")
  grep_rc=$?
  set -e
  if [ "$grep_rc" -gt 1 ]; then
    emit_result "hard" "tasks-read-error" "无法读取或匹配 $TASKS_REL" "检查文件权限与配置正则"
    exit 1
  fi
  active_count="${active_count:-0}"
  if active_threshold="$(adaptive_threshold tasks-active-count active_task_count "$TASKS_REL")"; then
    if awk -v current="$active_count" -v threshold="$active_threshold" 'BEGIN { exit(current > threshold ? 0 : 1) }'; then
      emit_result "adaptive" "tasks-active-count" \
        "活跃任务卡 $active_count 个，超过阈值 $active_threshold" \
        "拆分任务或归档已完成项；必要时重新初始化基线"
    else
      emit_result "ok" "tasks-active-count" \
        "活跃任务卡 $active_count 个，未超过阈值 $active_threshold"
    fi
  else
    emit_result "soft" "tasks-active-count-baseline-missing" \
      "未找到 $TASKS_REL 的 active_task_count 基线" \
      "运行 scan.sh --init-baseline"
  fi
fi

# 3. 归档指针（硬性）：归档段必须存在且指向 DECISIONS.md
# 检测归档段标题（## …归档…）+ DECISIONS 引用，不依赖正文固定措辞。
if cfg_rule_enabled hard_rules tasks-archived-iss-pointer; then
  archive_section_count=$(grep -cE '^##[^#].*归档' "$TASKS_FILE" || true)
  if [ "${archive_section_count:-0}" -gt 0 ] && grep -qE "$DECISIONS_BASE" "$TASKS_FILE"; then
    emit_result "ok" "tasks-archived-iss-pointer" \
      "归档段存在（${archive_section_count} 处 ## 归档标题）且引用 $DECISIONS_BASE"
  else
    emit_result "hard" "tasks-archived-iss-pointer" \
      "归档段缺失或未引用 ${DECISIONS_BASE}（## 归档标题数=${archive_section_count:-0}）" \
      "在归档区段（## …归档…，如「归档区」「已归档索引」「归档任务」）补充 $DECISIONS_BASE 链接"
  fi
fi

# 4. 总行数（自适应）
line_count=$(wc -l < "$TASKS_FILE" | tr -d ' ')
if line_threshold="$(adaptive_threshold tasks-line-count line_count "$TASKS_REL")"; then
  if awk -v current="$line_count" -v threshold="$line_threshold" 'BEGIN { exit(current > threshold ? 0 : 1) }'; then
    emit_result "adaptive" "tasks-line-count" "总行数 ${line_count}，超过阈值 $line_threshold" \
      "归档历史细节或重新初始化基线"
  else
    emit_result "ok" "tasks-line-count" "总行数 ${line_count}，未超过阈值 $line_threshold"
  fi
else
  emit_result "soft" "tasks-line-count-baseline-missing" "未找到 $TASKS_REL 的 line_count 基线" \
    "运行 scan.sh --init-baseline"
fi

finish_checker
