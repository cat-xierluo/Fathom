#!/usr/bin/env bash
# spawn-worker-settings-receipt.sh — spawn 工具生成 settings 写入的残留 receipt 模块。
# 本文件被 spawn-worker.sh source；依赖其全局变量：DRY_RUN / WORKTREE / SESSION /
# BRANCH / WORKER_BACKEND / SETTINGS_RESIDUE_RECEIPT_FILE。
#
# 背景（v2.19.0）：spawn 的 merge_pretool_hook 会向 worktree 内「被 git 跟踪」的
# .claude/settings.local.json 等文件追加 PreToolUse hook。这份工具生成的改动让已合并
# worker worktree 永远命中 PM_CLEANUP_DIRTY，被迫人工保留。本模块在 spawn 写入前
# 捕获精确 preimage（字节 + sha256），写入后捕获 postimage sha256，把两者与目标
# worktree/session 所有权绑定写入 receipt（git common-dir agent-authority 根，worker
# cwd 之外）。release-settings-residue.sh 只在「当前字节严格等于 postimage、receipt
# 身份匹配且不存在其他 dirty path」时恢复 preimage；用户/worker 二次编辑、哈希不一致、
# 未知来源、无 preimage 一律 fail-closed 保留。
#
# receipt 不存在时（无增量 / dry-run / 轻量模式 / 非 git 仓）不产生任何副作用，
# 清理按既有 dirty 门禁行为执行。

SETTINGS_RESIDUE_PATHS=()
SETTINGS_RESIDUE_PRE_STATE=()
SETTINGS_RESIDUE_PRE_SHA=()
SETTINGS_RESIDUE_PRE_B64=()
SETTINGS_RESIDUE_POST_STATE=()
SETTINGS_RESIDUE_POST_SHA=()
SETTINGS_RESIDUE_COUNT=0

settings_residue_sha256() {
  python3 - "$1" <<'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())
PY
}

settings_residue_b64() {
  base64 < "$1" | tr -d '\n'
}

# 本 backend 的 settings 写入目标（与 dependency_install_guard_setup /
# scope_guard_setup 的路由保持一致）。
settings_residue_settings_paths() {
  case "$WORKER_BACKEND" in
    claude-code|claude_code) printf '%s\n' ".claude/settings.local.json" ;;
    codebuddy) printf '%s\n' ".codebuddy/settings.local.json" ;;
    qoderwork-cn|qoderclicn) printf '%s\n' ".qoder/settings.local.json" ;;
    *) return 1 ;;
  esac
}

settings_residue_record() {
  local rel="$1" abs="$2" state="" sha="" b64=""
  if [ -f "$abs" ]; then
    state="present"
    sha=$(settings_residue_sha256 "$abs")
    b64=$(settings_residue_b64 "$abs")
  else
    state="absent"
    sha=""
    b64=""
  fi
  SETTINGS_RESIDUE_PATHS+=("$rel")
  SETTINGS_RESIDUE_PRE_STATE+=("$state")
  SETTINGS_RESIDUE_PRE_SHA+=("$sha")
  SETTINGS_RESIDUE_PRE_B64+=("$b64")
  SETTINGS_RESIDUE_COUNT=$((SETTINGS_RESIDUE_COUNT + 1))
}

# 在任何 spawn settings 写入之前调用：记录精确 preimage。
settings_residue_capture_preimage() {
  if [ -z "$SETTINGS_RESIDUE_RECEIPT_FILE" ]; then
    echo "SPAWN_WORKER_SETTINGS_RECEIPT: lightweight mode (no git common dir), receipt skipped"
    return 0
  fi
  if [ ! -d "$WORKTREE" ]; then
    echo "SPAWN_WORKER_SETTINGS_RECEIPT: worktree missing, receipt skipped"
    return 0
  fi
  local rel abs index
  while IFS= read -r rel; do
    abs="$WORKTREE/$rel"
    settings_residue_record "$rel" "$abs"
  done < <(settings_residue_settings_paths)
  index=0
  while [ "$index" -lt "$SETTINGS_RESIDUE_COUNT" ]; do
    echo "SPAWN_WORKER_SETTINGS_RECEIPT_PRE: ${SETTINGS_RESIDUE_PATHS[$index]} state=${SETTINGS_RESIDUE_PRE_STATE[$index]} sha256=${SETTINGS_RESIDUE_PRE_SHA[$index]:-none}"
    index=$((index + 1))
  done
}

# 在最后一个 spawn settings 写入之后调用：记录 postimage；有增量时写 receipt。
settings_residue_capture_postimage_and_write_receipt() {
  if [ -z "$SETTINGS_RESIDUE_RECEIPT_FILE" ]; then
    return 0
  fi
  if [ "$SETTINGS_RESIDUE_COUNT" -eq 0 ]; then
    return 0
  fi
  local index changed=0 rel abs state sha
  index=0
  while [ "$index" -lt "$SETTINGS_RESIDUE_COUNT" ]; do
    rel="${SETTINGS_RESIDUE_PATHS[$index]}"
    abs="$WORKTREE/$rel"
    if [ -f "$abs" ]; then
      state="present"
      sha=$(settings_residue_sha256 "$abs")
    else
      state="absent"
      sha=""
    fi
    SETTINGS_RESIDUE_POST_STATE+=("$state")
    SETTINGS_RESIDUE_POST_SHA+=("$sha")
    if [ "$state" != "${SETTINGS_RESIDUE_PRE_STATE[$index]}" ] || [ "$sha" != "${SETTINGS_RESIDUE_PRE_SHA[$index]}" ]; then
      changed=$((changed + 1))
    fi
    index=$((index + 1))
  done
  if [ "$changed" -eq 0 ]; then
    echo "SPAWN_WORKER_SETTINGS_RECEIPT: no spawn-generated settings delta, receipt skipped"
    return 0
  fi
  echo "SPAWN_WORKER_SETTINGS_RECEIPT: $SETTINGS_RESIDUE_RECEIPT_FILE delta=$changed"
  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi
  local paths_json pre_state_json pre_sha_json pre_b64_json post_state_json post_sha_json tracked_json files_json
  paths_json=$(printf '%s\n' "${SETTINGS_RESIDUE_PATHS[@]}" | jq -R . | jq -s .)
  pre_state_json=$(printf '%s\n' "${SETTINGS_RESIDUE_PRE_STATE[@]}" | jq -R . | jq -s .)
  pre_sha_json=$(printf '%s\n' "${SETTINGS_RESIDUE_PRE_SHA[@]}" | jq -R . | jq -s .)
  pre_b64_json=$(printf '%s\n' "${SETTINGS_RESIDUE_PRE_B64[@]}" | jq -R . | jq -s .)
  post_state_json=$(printf '%s\n' "${SETTINGS_RESIDUE_POST_STATE[@]}" | jq -R . | jq -s .)
  post_sha_json=$(printf '%s\n' "${SETTINGS_RESIDUE_POST_SHA[@]}" | jq -R . | jq -s .)
  # tracked 判定以 spawn 时 git 索引为准（untracked 新文件同理入账，恢复语义是删除）。
  tracked_json=$(printf '%s\n' "${SETTINGS_RESIDUE_PATHS[@]}" | while IFS= read -r rel; do
    if git -C "$WORKTREE" ls-files --error-unmatch "$rel" >/dev/null 2>&1; then
      printf 'true\n'
    else
      printf 'false\n'
    fi
  done | jq -R 'if . == "true" then true else false end' | jq -s .)
  files_json=$(jq -n \
    --argjson paths "$paths_json" \
    --argjson tracked "$tracked_json" \
    --argjson pre_state "$pre_state_json" \
    --argjson pre_sha "$pre_sha_json" \
    --argjson pre_b64 "$pre_b64_json" \
    --argjson post_state "$post_state_json" \
    --argjson post_sha "$post_sha_json" \
    '[range(0; ($paths | length)) as $i | {
      path: $paths[$i],
      tracked: $tracked[$i],
      preimage: {
        state: $pre_state[$i],
        sha256: $pre_sha[$i],
        content_base64: (if $pre_state[$i] == "present" and $pre_b64[$i] != "" then $pre_b64[$i] else null end)
      },
      postimage: {
        state: $post_state[$i],
        sha256: $post_sha[$i]
      }
    }]')
  local receipt_dir receipt_tmp created_at
  receipt_dir=$(dirname "$SETTINGS_RESIDUE_RECEIPT_FILE")
  mkdir -p "$receipt_dir"
  [ ! -e "$SETTINGS_RESIDUE_RECEIPT_FILE" ] || {
    echo "ERROR: settings residue receipt already exists for session $SESSION; choose a unique session id (fail-closed)" >&2
    return 1
  }
  created_at=$(date -u "+%Y-%m-%dT%H:%M:%SZ")
  receipt_tmp="$SETTINGS_RESIDUE_RECEIPT_FILE.tmp.$$"
  umask 077
  jq -n \
    --arg schema "multi-agent-orchestration.settings-residue-receipt.v1" \
    --arg created_at "$created_at" \
    --arg session "$SESSION" \
    --arg worktree "$WORKTREE" \
    --arg branch "$BRANCH" \
    --argjson files "$files_json" \
    '{
      schema: $schema,
      created_at: $created_at,
      session: $session,
      worktree: $worktree,
      branch: $branch,
      files: $files
    }' > "$receipt_tmp"
  if ! ln "$receipt_tmp" "$SETTINGS_RESIDUE_RECEIPT_FILE" 2>/dev/null; then
    rm -f "$receipt_tmp"
    echo "ERROR: could not atomically create settings residue receipt: $SETTINGS_RESIDUE_RECEIPT_FILE" >&2
    return 1
  fi
  rm -f "$receipt_tmp"
  echo "SPAWN_WORKER_SETTINGS_RECEIPT_WRITTEN: $SETTINGS_RESIDUE_RECEIPT_FILE"
}
