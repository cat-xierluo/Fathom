#!/usr/bin/env bash
# 上下文同步检测（v0.2.0 新增）：合并到 main 的改动是否同步更新了对应上下文文档。
# 按 config context_sync.change_types 动态选查（不机械全查，省上下文）。
# rule 前缀 context-sync-*；severity：decision=hard，changelog/tasks/figures/skill=adaptive，
# 局部可逆改动 = ok，不强制同步。
#
# 注意：message/suggestion 里变量一律用 ${var} 显式界定——bash 在 $var 后紧跟中文字节
# 时会把多字节字符误纳入变量名（rel<中文>→ unbound variable），${var} 杜绝此问题。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"
# 在条件/命令替换之前加载一次；读取失败不得被默认值或 process substitution 吞掉。
_cfg_ensure_flat

# ── 1. 解析参数、定扫描范围 ──
SINCE=""; RANGE=""; WORKING_TREE=0; HISTORY_TREE_REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --since)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        emit_result "hard" "context-sync-invalid-scope" "--since 缺少参数值" "传入有效 Git revision"
        exit 1
      fi
      SINCE="$2"; shift 2 ;;
    --range)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        emit_result "hard" "context-sync-invalid-scope" "--range 缺少参数值" "传入 A..B 或 A...B"
        exit 1
      fi
      RANGE="$2"; shift 2 ;;
    --working-tree) WORKING_TREE=1; shift ;;
    *)
      emit_result "hard" "context-sync-invalid-scope" "未知参数：$1" "从 scan.sh 调用 checker"
      exit 1 ;;
  esac
done

ENABLED="$(cfg_scalar context_sync.enabled)"; ENABLED="${ENABLED:-false}"
if [ "$ENABLED" != "true" ]; then
  emit_result "soft" "context-sync-disabled" \
    "context_sync 未启用，未验证 Git 改动与上下文同步" \
    "需要该证据时设置 context_sync.enabled: true"
  exit 0
fi

if [ "${WORKING_TREE}" -eq 1 ] && { [ -n "${RANGE}" ] || [ -n "${SINCE}" ]; }; then
  emit_result "hard" "context-sync-invalid-scope" \
    "--working-tree 不可与 --range/--since 合用；工作树与历史范围必须分开验收" \
    "预提交检查只传 --working-tree；历史检查只传 --range 或 --since"
  exit 1
fi

if [ "${WORKING_TREE}" -eq 1 ]; then
  DIFF_RANGE="working-tree"
elif [ -n "${RANGE}" ]; then
  DIFF_RANGE="${RANGE}"
  if [[ "${RANGE}" == *...* ]]; then
    HISTORY_TREE_REF="${RANGE##*...}"
  elif [[ "${RANGE}" == *..* ]]; then
    HISTORY_TREE_REF="${RANGE##*..}"
  else
    emit_result "hard" "context-sync-scan-error" \
      "无法解析历史范围 ${RANGE} 的右端 revision" \
      "--range 必须使用 A..B 或 A...B；预提交检查改用 --working-tree"
    exit 1
  fi
elif [ -n "${SINCE}" ]; then
  DIFF_RANGE="${SINCE}..HEAD"
  HISTORY_TREE_REF="HEAD"
elif git -C "${REPO_ROOT}" rev-parse --verify -q origin/main >/dev/null 2>&1; then
  DIFF_RANGE="origin/main..HEAD"
  HISTORY_TREE_REF="HEAD"
else
  DIFF_RANGE="HEAD~1..HEAD"
  HISTORY_TREE_REF="HEAD"
fi

# ── 2. 采改动文件 + 统计 ──
if [ "${WORKING_TREE}" -eq 1 ]; then
  if ! STAGED_CHANGED="$(git -C "${REPO_ROOT}" -c core.quotepath=false diff --cached --name-only 2>/dev/null)"; then
    emit_result "hard" "context-sync-scan-error" "无法读取 staged 改动" "确认 --repo 指向有效 Git 仓库后重试"
    exit 1
  fi
  if ! UNSTAGED_CHANGED="$(git -C "${REPO_ROOT}" -c core.quotepath=false diff --name-only 2>/dev/null)"; then
    emit_result "hard" "context-sync-scan-error" "无法读取 unstaged 改动" "确认 --repo 指向有效 Git 仓库后重试"
    exit 1
  fi
  if ! UNTRACKED_CHANGED="$(git -C "${REPO_ROOT}" -c core.quotepath=false ls-files --others --exclude-standard 2>/dev/null)"; then
    emit_result "hard" "context-sync-scan-error" "无法读取 untracked 改动" "确认 --repo 指向有效 Git 仓库后重试"
    exit 1
  fi
  CHANGED="$(printf '%s\n%s\n%s\n' "${STAGED_CHANGED}" "${UNSTAGED_CHANGED}" "${UNTRACKED_CHANGED}" | awk 'NF && !seen[$0]++')"
else
  if ! RESOLVED_HISTORY_TREE="$(git -C "${REPO_ROOT}" rev-parse --verify "${HISTORY_TREE_REF}^{commit}" 2>/dev/null)"; then
    emit_result "hard" "context-sync-scan-error" \
      "无法解析历史范围 ${DIFF_RANGE} 的右端 revision ${HISTORY_TREE_REF}" \
      "确认 --range/--since 引用存在且可由当前仓库解析"
    exit 1
  fi
  HISTORY_TREE_REF="${RESOLVED_HISTORY_TREE}"
  if ! CHANGED="$(git -C "${REPO_ROOT}" -c core.quotepath=false diff --name-only "${DIFF_RANGE}" 2>/dev/null)"; then
    emit_result "hard" "context-sync-scan-error" \
      "无法读取历史范围 ${DIFF_RANGE} 的改动文件" \
      "确认 --range/--since 两端 revision 存在且 Git diff 可执行"
    exit 1
  fi
fi
if [ -z "${CHANGED}" ]; then
  emit_result "ok" "context-sync-no-changes" \
    "改动范围 ${DIFF_RANGE} 无文件改动"
  exit 0
fi

if [ "${WORKING_TREE}" -eq 1 ]; then
  NFILES="$(printf '%s\n' "${CHANGED}" | awk 'NF { count++ } END { print count+0 }')"
  if ! TRACKED_NUMSTAT="$(git -C "${REPO_ROOT}" -c core.quotepath=false diff HEAD --numstat 2>/dev/null)"; then
    emit_result "hard" "context-sync-scan-error" \
      "无法读取 working-tree tracked 改动统计（git diff HEAD --numstat）" \
      "确认工作树与索引状态可读后重试"
    exit 1
  fi
  TRACKED_INS="$(printf '%s\n' "${TRACKED_NUMSTAT}" | awk '$1 ~ /^[0-9]+$/ { total += $1 } END { print total+0 }')"
  TRACKED_DEL="$(printf '%s\n' "${TRACKED_NUMSTAT}" | awk '$2 ~ /^[0-9]+$/ { total += $2 } END { print total+0 }')"
  UNTRACKED_INS=0
  while IFS= read -r f; do
    [ -z "${f}" ] && continue
    full_path="${REPO_ROOT}/${f}"
    if [ -L "${full_path}" ]; then
      if ! readlink "${full_path}" >/dev/null 2>&1; then
        emit_result "hard" "context-sync-scan-error" \
          "无法读取 untracked 符号链接 ${f}" \
          "修复或移除不可读路径后重试"
        exit 1
      fi
      file_lines=1
    elif [ -f "${full_path}" ]; then
      if ! file_lines="$(awk 'END { print NR+0 }' "${full_path}" 2>/dev/null)"; then
        emit_result "hard" "context-sync-scan-error" \
          "无法读取 untracked 文件 ${f}" \
          "修复文件权限或损坏后重试"
        exit 1
      fi
    else
      emit_result "hard" "context-sync-scan-error" \
        "untracked 路径 ${f} 在统计时不存在或不是常规文件" \
        "确认工作树在扫描期间未发生并发删除/替换后重试"
      exit 1
    fi
    UNTRACKED_INS=$((UNTRACKED_INS + file_lines))
  done <<< "${UNTRACKED_CHANGED}"
  NINS=$(( ${TRACKED_INS:-0} + UNTRACKED_INS ))
  NDEL="${TRACKED_DEL:-0}"
else
  if ! SHORTSTAT="$(git -C "${REPO_ROOT}" -c core.quotepath=false diff --shortstat "${DIFF_RANGE}" 2>/dev/null)"; then
    emit_result "hard" "context-sync-scan-error" \
      "无法读取历史范围 ${DIFF_RANGE} 的改动统计" \
      "确认 --range/--since 两端 revision 存在且 Git diff 可执行"
    exit 1
  fi
  NFILES="$(printf '%s\n' "${CHANGED}" | awk 'NF { count++ } END { print count+0 }')"
  NINS="$(printf '%s' "${SHORTSTAT}" | grep -oE '[0-9]+ insertion' | head -1 | grep -oE '[0-9]+' || true)"
  NDEL="$(printf '%s' "${SHORTSTAT}" | grep -oE '[0-9]+ deletion' | head -1 | grep -oE '[0-9]+' || true)"
fi
NFILES="${NFILES:-0}"; NINS="${NINS:-0}"; NDEL="${NDEL:-0}"
NCHURN=$((NINS + NDEL))
print_info "context-sync: range=${DIFF_RANGE} files=${NFILES} insertions=${NINS} deletions=${NDEL} churn=${NCHURN}"

# ── 3. 阈值（config 读取，带默认）──
LR_MAX_FILES="$(cfg_scalar context_sync.local_reversible.max_files)"; LR_MAX_FILES="${LR_MAX_FILES:-1}"
LR_MAX_INS="$(cfg_scalar context_sync.local_reversible.max_insertions)"; LR_MAX_INS="${LR_MAX_INS:-50}"
SYS_MIN_FILES="$(cfg_scalar context_sync.systemic.min_files)"; SYS_MIN_FILES="${SYS_MIN_FILES:-3}"
SYS_MIN_INS="$(cfg_scalar context_sync.systemic.min_insertions)"; SYS_MIN_INS="${SYS_MIN_INS:-200}"

# ── 4. 识别 Skill 根、figures 与体系性改动 ──
# Skill 根以最近祖先 SKILL.md 为准，兼容 .claude/skills/<name>/ 与 monorepo <name>/。
# 若 config 明确 expect_skill_internal=true、但找不到 SKILL.md，则保留为未知布局并 adaptive。
_matching_change_type() {
  local f="$1" idx pat pat_match
  for idx in $(cfg_change_type_indices); do
    pat="$(cfg_change_type_field "${idx}" pattern)"
    [ -z "${pat}" ] && continue
    pat_match="${pat//\*\*/*}"
    # shellcheck disable=SC2053 # 右侧来自受控 config，必须保留 glob 语义。
    if [[ "${f}" == ${pat_match} ]]; then
      printf '%s' "${idx}"
      return 0
    fi
  done
  return 1
}

_resolve_skill_root() {
  local f="$1" dir marker
  if [[ "${f}" == */* ]]; then dir="${f%/*}"; else dir="."; fi
  while :; do
    if [ "${dir}" = "." ]; then marker="SKILL.md"; else marker="${dir}/SKILL.md"; fi
    if [ "${WORKING_TREE}" -eq 1 ]; then
      if [ -f "${REPO_ROOT}/${marker}" ] || git -C "${REPO_ROOT}" cat-file -e "HEAD:${marker}" 2>/dev/null; then
        printf '%s' "${dir}"
        return 0
      fi
    elif git -C "${REPO_ROOT}" cat-file -e "${HISTORY_TREE_REF}:${marker}" 2>/dev/null; then
      printf '%s' "${dir}"
      return 0
    fi
    [ "${dir}" = "." ] && break
    if [[ "${dir}" == */* ]]; then dir="${dir%/*}"; else dir="."; fi
  done
  return 1
}

SKILL_ROOTS=""; UNKNOWN_SKILL_PATHS=""
while IFS= read -r f; do
  [ -z "${f}" ] && continue
  skill_root="$(_resolve_skill_root "${f}" || true)"
  if [ -n "${skill_root}" ]; then
    SKILL_ROOTS="${SKILL_ROOTS}${SKILL_ROOTS:+$'\n'}${skill_root}"
    continue
  fi
  matched_idx="$(_matching_change_type "${f}" || true)"
  if [ -n "${matched_idx}" ] && [ "$(cfg_change_type_field "${matched_idx}" expect_skill_internal)" = "true" ]; then
    UNKNOWN_SKILL_PATHS="${UNKNOWN_SKILL_PATHS}${UNKNOWN_SKILL_PATHS:+$'\n'}${f}"
  fi
done <<< "${CHANGED}"
SKILL_ROOTS="$(printf '%s\n' "${SKILL_ROOTS}" | awk 'NF && !seen[$0]++')"
UNKNOWN_SKILL_PATHS="$(printf '%s\n' "${UNKNOWN_SKILL_PATHS}" | awk 'NF && !seen[$0]++')"

HAS_FIGURES=0; HAS_SKILL=0
if printf '%s\n' "${CHANGED}" | grep -qE '^figures/'; then HAS_FIGURES=1; fi
if [ -n "${SKILL_ROOTS}" ] || [ -n "${UNKNOWN_SKILL_PATHS}" ]; then HAS_SKILL=1; fi
IS_SYSTEMIC=0
if [ "${NFILES}" -ge "${SYS_MIN_FILES}" ] || [ "${NCHURN}" -ge "${SYS_MIN_INS}" ]; then
  IS_SYSTEMIC=1
fi

# ── 5. 局部可逆二分 ──
if [ "${NFILES}" -le "${LR_MAX_FILES}" ] && [ "${NCHURN}" -le "${LR_MAX_INS}" ] \
   && [ "${HAS_FIGURES}" -eq 0 ] && [ "${HAS_SKILL}" -eq 0 ]; then
  emit_result "ok" "context-sync-local-reversible" \
    "局部可逆改动（${NFILES} 文件 / ${NCHURN} 改动行），不强制同步上下文"
  exit 0
fi

# ── 6. 聚合 expect：遍历改动文件 × change_types（第一个匹配的 change_type）──
EXP_CHANGELOG=false; EXP_TASKS=false; EXP_FIGURES=false; EXP_SKILL=false
EXP_DECISION=never   # never < systemic < always

_changed_in() { printf '%s\n' "${CHANGED}" | grep -qxF "$1" || false; }

while IFS= read -r f; do
  if [ -z "${f}" ]; then continue; fi
  idx="$(_matching_change_type "${f}" || true)"
  skill_root="$(_resolve_skill_root "${f}" || true)"

  # 自动发现的 monorepo Skill 若只命中通用 fallback，由内部三件套自行闭环，
  # 不把它误归类为仓库级体系性改动、要求顶层 docs/DECISIONS.md。
  if [ -n "${skill_root}" ] && { [ -z "${idx}" ] || [ "$(cfg_change_type_field "${idx}" expect_skill_internal)" != "true" ]; }; then
    EXP_SKILL=true
    continue
  fi

  [ -z "${idx}" ] && continue
  if [ "$(cfg_change_type_field "${idx}" expect_changelog)" = "true" ]; then EXP_CHANGELOG=true; fi
  if [ "$(cfg_change_type_field "${idx}" expect_tasks)" = "true" ]; then EXP_TASKS=true; fi
  if [ "$(cfg_change_type_field "${idx}" expect_figures)" = "true" ]; then EXP_FIGURES=true; fi
  if [ "$(cfg_change_type_field "${idx}" expect_skill_internal)" = "true" ]; then EXP_SKILL=true; fi
  ed="$(cfg_change_type_field "${idx}" expect_decision)"
  if [ "${ed}" = "always" ]; then
    EXP_DECISION=always
  elif [ "${ed}" = "systemic" ] && [ "${EXP_DECISION}" != "always" ]; then
    EXP_DECISION=systemic
  fi
done <<< "${CHANGED}"

# ── 7. 逐 rule emit（仅 emit 命中的 expect）──
ANY_EXPECT=false

emit_changelog() {
  ANY_EXPECT=true
  local rel file
  rel="$(cfg_file_by_role changelog)"; rel="${rel:-CHANGELOG.md}"
  file="${REPO_ROOT}/${rel}"
  if _changed_in "${rel}"; then
    if [ -f "${file}" ] && grep -qE '^## [0-9]{4}-[0-9]{2}-[0-9]{2}' "${file}"; then
      emit_result "ok" "context-sync-changelog-section" "${rel} 已含 ## 日期 详细段"
    else
      emit_result "adaptive" "context-sync-changelog-section" \
        "${rel} 已改但缺 ## YYYY-MM-DD 详细段（可能只改了版本表行）" \
        "补本次改动对应日期的详细变更段"
    fi
  else
    emit_result "adaptive" "context-sync-changelog-section" \
      "改动未同步到 ${rel}" \
      "在 ${rel} 补本次改动的 ## 日期 详细段"
  fi
}

emit_tasks() {
  ANY_EXPECT=true
  local rel
  rel="$(cfg_file_by_role active-tasks)"; rel="${rel:-docs/TASKS.md}"
  if _changed_in "${rel}"; then
    emit_result "ok" "context-sync-tasks-status" "${rel} 已更新"
  else
    emit_result "adaptive" "context-sync-tasks-status" \
      "改动未同步到 ${rel}（任务状态未更新）" \
      "更新 ${rel} 对应任务状态（完成/收口/待核实）"
  fi
}

emit_decision() {
  ANY_EXPECT=true
  local rel
  rel="$(cfg_file_by_role decision-log)"; rel="${rel:-docs/DECISIONS.md}"
  if _changed_in "${rel}"; then
    emit_result "ok" "context-sync-decision" "${rel} 已记决策"
  else
    emit_result "hard" "context-sync-decision" \
      "体系性改动未同步到 ${rel}（缺新 DEC）" \
      "在 ${rel} 补 DEC 段（背景/决策/影响）"
  fi
}

emit_figures() {
  ANY_EXPECT=true
  local rel
  rel="$(cfg_file_by_role figures-outline)"
  if [ -n "${rel}" ] && _changed_in "${rel}"; then
    emit_result "ok" "context-sync-figures-outline" "${rel} 已更新"
  else
    emit_result "adaptive" "context-sync-figures-outline" \
      "figures 改动未同步到 ${rel:-figures/FIGURES-OUTLINE.md}" \
      "更新配图进度总览/各章登记的采集状态与图片路径"
  fi
}

emit_skill() {
  ANY_EXPECT=true
  local skill_missing="" skill_unknown="" skill_root doc sc
  while IFS= read -r skill_root; do
    if [ -z "${skill_root}" ]; then continue; fi
    # skill 内部三件套（feedback_legal_skill_internal_docs）：改 skill 须同步 CHANGELOG/DECISIONS/TASKS
    for doc in CHANGELOG.md DECISIONS.md TASKS.md; do
      if [ "${skill_root}" = "." ]; then sc="${doc}"; else sc="${skill_root}/${doc}"; fi
      if ! _changed_in "${sc}"; then
        skill_missing="${skill_missing} ${sc}"
      fi
    done
  done <<< "${SKILL_ROOTS}"
  if [ -n "${UNKNOWN_SKILL_PATHS}" ]; then
    skill_unknown="$(printf '%s' "${UNKNOWN_SKILL_PATHS}" | tr '\n' ' ')"
  fi
  if [ -z "${skill_missing}" ] && [ -z "${skill_unknown}" ]; then
    emit_result "ok" "context-sync-skill-internal" "改动 skill 的内部三件套（CHANGELOG/DECISIONS/TASKS）均已更新"
  else
    emit_result "adaptive" "context-sync-skill-internal" \
      "skill 改动未闭环；缺内部三件套：${skill_missing:-无}；无法定位 SKILL.md 的路径：${skill_unknown:-无}" \
      "确认 Skill 根含 SKILL.md，并更新同一根下 CHANGELOG/DECISIONS/TASKS（feedback_legal_skill_internal_docs）"
  fi
}

if [ "${EXP_CHANGELOG}" = "true" ]; then emit_changelog; fi
if [ "${EXP_TASKS}" = "true" ]; then emit_tasks; fi
if [ "${EXP_DECISION}" = "always" ]; then
  emit_decision
elif [ "${EXP_DECISION}" = "systemic" ] && [ "${IS_SYSTEMIC}" -eq 1 ]; then
  emit_decision
fi
if [ "${EXP_FIGURES}" = "true" ]; then emit_figures; fi
if [ "${EXP_SKILL}" = "true" ]; then emit_skill; fi

# 无任何 expect 命中（改动类型未配 expect）
if [ "${ANY_EXPECT}" = "false" ]; then
  emit_result "ok" "context-sync-not-applicable" \
    "改动类型无对应上下文同步规则（${NFILES} 文件 / ${NCHURN} 改动行）"
fi

finish_checker
