#!/usr/bin/env bash
# 决策变更同步检测（v0.3.0 新增）：DECISIONS.md 中的 supersede/change 标记
# 触发的旧决策编号在项目其它文档中是否同步更新。
#
# 三步管道：
#   Step 1: awk 解析 DECISIONS.md，识别 ## D-XXX 段中含 "supersede D-YYY" 标记的行
#           → 提取 (新决策编号, 旧决策编号, 变更日期) 三元组
#   Step 2: grep -rn 旧决策编号，排除 DECISIONS.md 自身 / .git/ / exclude_paths
#   Step 3: git log --since=<变更日期> -- <引用文件> 是否非空 + 引用行是否变更
#           → hard(完全未同步) / adaptive(有 commit 但引用行未改) / ok(已同步或无引用)
#
# rule 前缀：dec-ref-sync-*；severity：hard / adaptive / soft / ok。
# 退出码：0=全部 ok，1=有 hard，2=有 adaptive。
# 复用 common.sh 的 emit_result / cfg_* / REPO_ROOT。
#
# 不动 git 状态、不加 hook/cron、不自动 commit。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 0. 解析参数 ──
while [ $# -gt 0 ]; do
  case "$1" in
    --repo|--config)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        printf 'doc-curator decision-sync: %s 缺少参数值\n' "$1" >&2
        exit 64
      fi
      if [ "$1" = "--repo" ]; then export DOC_CURATOR_REPO="$2"; else export DOC_CURATOR_CONFIG="$2"; fi
      shift 2 ;;
    *) printf 'doc-curator decision-sync: 未知参数 %s\n' "$1" >&2; exit 64 ;;
  esac
done

# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"
# 在条件/命令替换之前加载一次；读取失败不得被默认值或 process substitution 吞掉。
_cfg_ensure_flat

# ── 1. 开关 + 路径解析（config 驱动，带默认）──
ENABLED="$(cfg_scalar dec_ref_sync.enabled)"; ENABLED="${ENABLED:-true}"
if [ "${ENABLED}" != "true" ]; then
  emit_result "soft" "dec-ref-sync-disabled" \
    "dec_ref_sync 未启用，未验证决策变更同步" \
    "需要该证据时设置 dec_ref_sync.enabled: true"
  exit 0
fi

DECISIONS_REL="$(cfg_file_by_role decision-log)"; DECISIONS_REL="${DECISIONS_REL:-docs/DECISIONS.md}"
DECISIONS_FILE="$REPO_ROOT/${DECISIONS_REL}"
if [ ! -f "$DECISIONS_FILE" ]; then
  emit_result "soft" "dec-ref-sync-decisions-missing" \
    "${DECISIONS_REL} 不存在，跳过决策变更同步检测" \
    "在 config files 段修正 decision-log 路径或创建该文件"
  exit 0
fi

# 扫描配置（scan_extensions / exclude_paths 来自 config；带默认）
SCAN_EXTS_RAW="$(cfg_scalar dec_ref_sync.scan_extensions)"
SCAN_EXTS_DEFAULT="md yaml"
if [ -n "$SCAN_EXTS_RAW" ]; then
  SCAN_EXTS="${SCAN_EXTS_RAW}"
else
  SCAN_EXTS="${SCAN_EXTS_DEFAULT}"
fi

EXCLUDE_PATHS_DEFAULT=".git DECISIONS.md docs/plans node_modules .claude/agent-sessions"
EXCLUDE_RAW="$(cfg_scalar dec_ref_sync.exclude_paths)"
if [ -n "$EXCLUDE_RAW" ]; then
  EXCLUDE_PATHS="${EXCLUDE_RAW}"
else
  EXCLUDE_PATHS="${EXCLUDE_PATHS_DEFAULT}"
fi

# ── 2. Step 1 — awk 解析 DECISIONS.md，输出 supersede 元组 ──
# 输出格式：每行 "新决策编号|旧决策编号|变更日期"
# 匹配两种编号格式：
#   - D-YYYY-MM-DD-NN（spec 示例，如 D-2026-07-03-01）
#   - DEC-NNN（项目内编号，如 DEC-052）
SUPERSEDE_FILE="$(mktemp)"
trap 'rm -f "$SUPERSEDE_FILE"' EXIT

awk '
  function find_dref(s,    tmp) {
    # 在 s 中找 "supersede<空白>D-YYYY-MM-DD-NN" 或 "supersede<空白>DEC-NNN"
    # 区间量词 {N,M} 一律展开为重复字符类：旧 BSD awk（macOS 自带）不支持区间量词
    # 且不报错，正则会静默失配（D 形态 marker 全部丢失 → no-markers 假绿）。
    if (match(s, /supersede[[:space:]]+D-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9]?/)) {
      tmp = substr(s, RSTART, RLENGTH)
      sub(/^supersede[[:space:]]+/, "", tmp)
      return tmp
    }
    if (match(s, /supersede[[:space:]]+DEC-[0-9]+/)) {
      tmp = substr(s, RSTART, RLENGTH)
      sub(/^supersede[[:space:]]+/, "", tmp)
      return tmp
    }
    return ""
  }
  BEGIN { in_d = 0; d_id = ""; d_date = ""; sup_old = "" }
  $0 ~ /^##[[:space:]]+(D-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9]?|DEC-[0-9]+)([[:space:]]|$)/ {
    if (in_d && sup_old != "") {
      printf "%s|%s|%s\n", d_id, sup_old, d_date
    }
    in_d = 1
    line = $0
    sub(/^##[[:space:]]+/, "", line)
    d_id = line
    sub(/[[:space:]].*$/, "", d_id)
    d_date = ""
    sup_old = ""
    next
  }
  in_d && /^-[[:space:]]*日期:[[:space:]]*/ {
    d_date = $0
    sub(/^-[[:space:]]*日期:[[:space:]]*/, "", d_date)
    sub(/[[:space:]]*$/, "", d_date)
    next
  }
  in_d && /supersede[[:space:]]+D-|supersede[[:space:]]+DEC-/ {
    found = find_dref($0)
    if (found != "") sup_old = found
    next
  }
  in_d && /^---[[:space:]]*$/ {
    if (sup_old != "") {
      printf "%s|%s|%s\n", d_id, sup_old, d_date
    }
    in_d = 0; d_id = ""; d_date = ""; sup_old = ""
    next
  }
  END {
    if (in_d && sup_old != "") {
      printf "%s|%s|%s\n", d_id, sup_old, d_date
    }
  }
' "$DECISIONS_FILE" > "$SUPERSEDE_FILE" 2>/dev/null || {
  emit_result "hard" "dec-ref-sync-scan-error" "无法解析 ${DECISIONS_REL}" "检查文件权限与决策格式"
  exit 1
}

# 空扫描快速退出（用例 6）
if [ ! -s "$SUPERSEDE_FILE" ]; then
  emit_result "ok" "dec-ref-sync-no-markers" \
    "decision-sync: no supersede markers found in ${DECISIONS_REL}"
  exit 0
fi

print_info "decision-sync: $(wc -l < "$SUPERSEDE_FILE" | tr -d ' ') supersede marker(s) found"

# ── 3. Step 2 + 3 — 对每条 supersede 记录 grep + git log 比对 ──
# 构造 grep --include 列表
INCLUDE_ARGS=()
for ext in ${SCAN_EXTS}; do
  case "$ext" in
    *[!A-Za-z0-9]*)
      emit_result "hard" "dec-ref-sync-config-invalid" "非法扩展名：$ext" "scan_extensions 只使用字母和数字"
      exit 1 ;;
  esac
  INCLUDE_ARGS+=("--include=*.${ext}")
done

# 构造 exclude 目录（grep --exclude-dir）
EXCLUDE_ARGS=()
for ex in ${EXCLUDE_PATHS}; do
  case "$ex" in
    -*|*..*)
      emit_result "hard" "dec-ref-sync-config-invalid" "非法排除路径：$ex" "使用项目内相对路径"
      exit 1 ;;
  esac
  if [ -d "$REPO_ROOT/$ex" ] || [[ "$ex" == */* ]] || [[ "$ex" != *.* ]]; then
    EXCLUDE_ARGS+=("--exclude-dir=${ex##*/}")
  else
    EXCLUDE_ARGS+=("--exclude=${ex##*/}")
  fi
done

ANY_HARD=0
ANY_ADAPTIVE=0

epoch_to_iso() {
  local epoch="$1" iso
  if iso="$(date -u -r "$epoch" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null)"; then
    printf '%s' "$iso"
    return 0
  fi
  if iso="$(date -u -d "@$epoch" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null)"; then
    printf '%s' "$iso"
    return 0
  fi
  return 1
}

while IFS='|' read -r new_id old_id dec_date; do
  [ -z "${old_id}" ] && continue
  [ -z "${dec_date}" ] && dec_date="$(git -C "${REPO_ROOT}" log -1 --format='%aI' -- "${DECISIONS_REL}" 2>/dev/null | cut -c1-10)"
  [ -z "${dec_date}" ] && dec_date="1970-01-01"

  # grep 旧决策编号引用（不区分大小写）
  set +e
  REF_HITS="$(grep -rn -I "${INCLUDE_ARGS[@]}" "${EXCLUDE_ARGS[@]}" -F "${old_id}" "$REPO_ROOT" 2>/dev/null)"
  grep_rc=$?
  set -e
  if [ "$grep_rc" -gt 1 ]; then
    emit_result "hard" "dec-ref-sync-scan-error" "搜索 ${old_id} 引用失败" "检查项目目录权限"
    ANY_HARD=1
    continue
  fi

  # 过滤掉 DECISIONS.md 自身行（自引用不计入漂移）
  if [ -n "${REF_HITS}" ]; then
    REF_HITS="$(printf '%s\n' "${REF_HITS}" | grep -v "^${REPO_ROOT}/${DECISIONS_REL}:" || true)"
    REF_HITS="$(printf '%s\n' "${REF_HITS}" | grep -vE "^${REPO_ROOT}/(.+)/?${DECISIONS_REL##*/}:" || true)"
  fi

  if [ -z "${REF_HITS}" ]; then
    # 用例 4 — 无引用方 → ok
    emit_result "ok" "dec-ref-sync-clean" \
      "${old_id} (superseded by ${new_id} on ${dec_date}) — no remaining references"
    continue
  fi

  # 有引用方：按文件聚合（同一文件可能多行）
  REF_FILES="$(printf '%s\n' "${REF_HITS}" | sed -E "s|^${REPO_ROOT}/([^:]+):.*|\1|" | sort -u)"

  while IFS= read -r ref_file; do
    [ -z "${ref_file}" ] && continue
    # 1) git log --since=<变更日期> -- <引用文件> 是否非空
    HAS_COMMIT_SINCE="$(git -C "${REPO_ROOT}" log -1 --since="${dec_date}T00:00:00Z" --format='%H' -- "${ref_file}" 2>/dev/null || true)"

    if [ -z "${HAS_COMMIT_SINCE}" ]; then
      # 用例 1 — hard：引用方文件在决策变更后无任何 commit
      emit_result "hard" "dec-ref-sync-stale" \
        "${old_id} referenced in ${ref_file} (superseded by ${new_id} on ${dec_date}) — file not updated since supersede" \
        "在 ${ref_file} 中将 ${old_id} 引用更新为 ${new_id} 或调整引用内容"
      ANY_HARD=1
      continue
    fi

    # 2) 引用行在变更后是否被 touch：grep 该行号取最新 commit date
    # 取该文件中所有含 old_id 的行号（不限定文件已变更时哪些行号被改——若文件被改但
    # 引用行未被 touch，git blame 该行最后 commit 必然早于 dec_date）
    TOUCHED_LINE=0
    while IFS= read -r hit; do
      [ -z "${hit}" ] && continue
      rel_path="${hit#"${REPO_ROOT}"/}"
      # hit 形如 "$REPO_ROOT/path/file.md:42:content"，grep 输出是 path:line:content。
      # strip REPO_ROOT/ 后还含 ":line:content"，git blame 用作 path 会报错找不到文件 → BLAME_TS 空 → 误报 adaptive。
      # 修：先切到第一个 ":"，得到纯相对路径。
      rel_path="${rel_path%%:*}"
      line_no="$(printf '%s' "${hit}" | sed -E "s|^[^:]+:([0-9]+):.*|\1|")"
      [ -z "${line_no}" ] && continue
      # 若 rel_path 含残余 separator（如上游 grep 输出缺最后一段），git blame 静默返回空 — 不要因此报 hard，留空走 fallthrough。
      BLAME_TS="$(git -C "${REPO_ROOT}" blame -L "${line_no},${line_no}" --porcelain -- "${rel_path}" 2>/dev/null | grep -m1 '^author-time' | awk '{print $2}' || true)"
      if [ -n "${BLAME_TS}" ]; then
        # git blame author-time 是 epoch 秒
        BLAME_ISO="$(epoch_to_iso "${BLAME_TS}" || true)"
        if [ -n "${BLAME_ISO}" ] && [ "${BLAME_ISO}" \> "${dec_date}T00:00:00Z" ]; then
          TOUCHED_LINE=1
          break
        fi
      fi
    done <<EOF_HITS
${REF_HITS}
EOF_HITS

    if [ "${TOUCHED_LINE}" = "1" ]; then
      # 用例 2 — ok：引用行已更新
      emit_result "ok" "dec-ref-sync-synced" \
        "${old_id} referenced in ${ref_file} — reference line updated after ${new_id} supersede on ${dec_date}"
    else
      # 用例 3 — adaptive：文件有 commit 但引用行未改
      emit_result "adaptive" "dec-ref-sync-stale" \
        "${old_id} referenced in ${ref_file} (superseded by ${new_id} on ${dec_date}) — file has commits but reference line untouched" \
        "在 ${ref_file} 中将 ${old_id} 引用更新为 ${new_id} 或调整引用内容"
      ANY_ADAPTIVE=1
    fi
  done <<EOF_FILES
${REF_FILES}
EOF_FILES
done < "$SUPERSEDE_FILE"

# ── 4. 退出码 ──
[ "${ANY_HARD}" -eq 1 ] && exit 1
[ "${ANY_ADAPTIVE}" -eq 1 ] && exit 2
exit 0
