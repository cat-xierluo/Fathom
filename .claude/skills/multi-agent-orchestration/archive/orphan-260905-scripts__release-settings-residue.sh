#!/usr/bin/env bash
# release-settings-residue.sh — spawn 工具生成 settings 残留的精确恢复（fail-closed）。
#
# 仅当同时满足以下全部条件时，才把 worktree 内的 settings 文件恢复为 spawn 前的
# 精确 preimage，然后让调用方继续既有精确清理：
#   1. receipt 存在且身份匹配（session + worktree 真实路径一致）；
#   2. 当前文件字节 sha256 严格等于 receipt 记录的工具生成 postimage；
#   3. worktree 的全部 dirty path 都被 receipt 覆盖且满足条件 2（不存在其他脏文件）。
# 用户/worker 后续编辑、哈希不一致、未知来源 dirty path、无 receipt/无 preimage、
# preimage 内容损坏：一律 fail-closed 保留，不做任何修改。
#
# 本脚本只恢复字节；绝不删除 worktree、分支或任何远端 ref。默认 dry-run。
#
# Usage:
#   release-settings-residue.sh --project PATH --worktree PATH --session NAME \
#     [--receipt PATH] [--execute]
#
# Exit codes:
#   0  已恢复（--execute）或可恢复（dry-run）/ 无需恢复
#   2  用法错误或 receipt JSON 非法
#   3  SETTINGS_RESIDUE_NO_RECEIPT（无 receipt / 无 preimage）
#   4  SETTINGS_RESIDUE_POSTIMAGE_MISMATCH / IDENTITY_MISMATCH（身份或字节不匹配）
#   5  SETTINGS_RESIDUE_PREIMAGE_CORRUPT（preimage 内容与 sha 不一致）
#   6  SETTINGS_RESIDUE_OTHER_DIRTY（存在 receipt 未覆盖的脏路径）

set -euo pipefail

PROJECT=""
WORKTREE=""
SESSION=""
RECEIPT_OVERRIDE=""
EXECUTE=0

usage() {
  cat >&2 <<'USAGE'
Usage:
  release-settings-residue.sh --project PATH --worktree PATH --session NAME \
    [--receipt PATH] [--execute]

Default is dry-run. Prints SETTINGS_RESIDUE_* evidence lines; restores the exact
spawn preimage only when every dirty path is receipt-covered and byte-identical
to the recorded tool-generated postimage.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --worktree) WORKTREE="$2"; shift 2 ;;
    --session) SESSION="$2"; shift 2 ;;
    --receipt) RECEIPT_OVERRIDE="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "SETTINGS_RESIDUE_USAGE: unknown argument $1" >&2; usage; exit 64 ;;
  esac
done

[ -n "$WORKTREE" ] && [ -n "$SESSION" ] || { usage; exit 64; }
[ -n "$PROJECT" ] || [ -n "$RECEIPT_OVERRIDE" ] || { usage; exit 64; }
for dependency in git jq python3; do
  command -v "$dependency" >/dev/null 2>&1 || {
    echo "SETTINGS_RESIDUE_DEPENDENCY_MISSING: $dependency" >&2
    exit 64
  }
done

WORKTREE=$(cd "$WORKTREE" 2>/dev/null && pwd -P) || {
  echo "SETTINGS_RESIDUE_WORKTREE_MISSING: $WORKTREE" >&2
  exit 2
}
git -C "$WORKTREE" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "SETTINGS_RESIDUE_NOT_GIT_WORKTREE: $WORKTREE" >&2
  exit 2
}

receipt=""
if [ -n "$RECEIPT_OVERRIDE" ]; then
  receipt="$RECEIPT_OVERRIDE"
else
  common_dir=$(git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
    echo "SETTINGS_RESIDUE_PROJECT_COMMON_DIR_MISSING: $PROJECT" >&2
    exit 2
  }
  receipt="$common_dir/agent-authority/$SESSION.settings-residue.json"
fi
[ -f "$receipt" ] || {
  echo "SETTINGS_RESIDUE_NO_RECEIPT: $receipt (fail-closed; preimage unknown)" >&2
  exit 3
}

jq -e 'type == "object" and .schema == "multi-agent-orchestration.settings-residue-receipt.v1"' "$receipt" >/dev/null 2>&1 || {
  echo "SETTINGS_RESIDUE_RECEIPT_INVALID: $receipt" >&2
  exit 2
}
receipt_session=$(jq -r '.session // ""' "$receipt")
receipt_worktree=$(jq -r '.worktree // ""' "$receipt")
[ "$receipt_session" = "$SESSION" ] || {
  echo "SETTINGS_RESIDUE_IDENTITY_MISMATCH: session receipt=$receipt_session arg=$SESSION (fail-closed)" >&2
  exit 4
}
receipt_worktree_real=$(cd "$receipt_worktree" 2>/dev/null && pwd -P || true)
[ -n "$receipt_worktree_real" ] && [ "$receipt_worktree_real" = "$WORKTREE" ] || {
  echo "SETTINGS_RESIDUE_IDENTITY_MISMATCH: worktree receipt=$receipt_worktree arg=$WORKTREE (fail-closed)" >&2
  exit 4
}

dirty_paths=()
unresolvable=0
while IFS= read -r -d '' entry; do
  path="${entry:3}"
  case "$path" in
    *" -> "*)
      unresolvable=$((unresolvable + 1))
      ;;
    *)
      dirty_paths+=("$path")
      ;;
  esac
done < <(git -C "$WORKTREE" status --porcelain=v1 -z --untracked-files=all 2>/dev/null)

if [ "$unresolvable" -gt 0 ]; then
  echo "SETTINGS_RESIDUE_OTHER_DIRTY: unresolvable rename entries=$unresolvable (fail-closed)" >&2
  exit 6
fi

sha256_of() {
  python3 - "$1" <<'PY'
import hashlib, sys
try:
    print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())
except FileNotFoundError:
    print("")
PY
}

current_state_of() {
  [ -f "$1" ] && printf 'present' || printf 'absent'
}

releasable_count=0
for dirty_path in "${dirty_paths[@]}"; do
  entry=$(jq -c --arg p "$dirty_path" '(.files // []) | map(select(.path == $p)) | .[0] // empty' "$receipt")
  if [ -z "$entry" ]; then
    echo "SETTINGS_RESIDUE_OTHER_DIRTY: path not covered by receipt: $dirty_path (fail-closed)" >&2
    exit 6
  fi
  post_state=$(printf '%s' "$entry" | jq -r '.postimage.state // ""')
  post_sha=$(printf '%s' "$entry" | jq -r '.postimage.sha256 // ""')
  target="$WORKTREE/$dirty_path"
  current_state=$(current_state_of "$target")
  if [ "$current_state" != "$post_state" ]; then
    echo "SETTINGS_RESIDUE_POSTIMAGE_MISMATCH: $dirty_path state current=$current_state postimage=$post_state (fail-closed)" >&2
    exit 4
  fi
  if [ "$current_state" = "present" ]; then
    current_sha=$(sha256_of "$target")
    [ "$current_sha" = "$post_sha" ] || {
      echo "SETTINGS_RESIDUE_POSTIMAGE_MISMATCH: $dirty_path bytes edited after spawn (fail-closed)" >&2
      exit 4
    }
  fi
  releasable_count=$((releasable_count + 1))
done

if [ "${#dirty_paths[@]}" -eq 0 ]; then
  echo "SETTINGS_RESIDUE_NOTHING_TO_RELEASE: worktree is clean"
  exit 0
fi

restore_entry() {
  local dirty_path="$1" entry target dir tmp pre_state pre_sha pre_b64
  entry=$(jq -c --arg p "$dirty_path" '(.files // []) | map(select(.path == $p)) | .[0] // empty' "$receipt")
  pre_state=$(printf '%s' "$entry" | jq -r '.preimage.state // ""')
  pre_sha=$(printf '%s' "$entry" | jq -r '.preimage.sha256 // ""')
  pre_b64=$(printf '%s' "$entry" | jq -r '.preimage.content_base64 // ""')
  target="$WORKTREE/$dirty_path"
  if [ "$pre_state" = "absent" ]; then
    if [ "$EXECUTE" -eq 1 ]; then
      rm -f "$target"
      echo "SETTINGS_RESIDUE_RESTORED: $dirty_path -> absent (deleted tool-generated file)"
    else
      echo "SETTINGS_RESIDUE_PLAN: $dirty_path -> delete (preimage absent)"
    fi
    return 0
  fi
  if [ "$pre_state" != "present" ] || [ -z "$pre_b64" ] || [ -z "$pre_sha" ]; then
    echo "SETTINGS_RESIDUE_NO_RECEIPT: $dirty_path preimage missing in receipt (fail-closed)" >&2
    exit 3
  fi
  if [ "$EXECUTE" -eq 0 ]; then
    echo "SETTINGS_RESIDUE_PLAN: $dirty_path -> restore preimage sha256=$pre_sha"
    return 0
  fi
  dir=$(dirname "$target")
  tmp="$dir/.settings-residue.$$"
  set +e
  python3 - "$tmp" "$pre_b64" "$pre_sha" <<'PY'
import base64, hashlib, sys
tmp, b64, expect = sys.argv[1], sys.argv[2], sys.argv[3]
data = base64.b64decode(b64)
if hashlib.sha256(data).hexdigest() != expect:
    raise SystemExit(5)
with open(tmp, "wb") as fh:
    fh.write(data)
PY
  restore_rc=$?
  set -e
  if [ "$restore_rc" -ne 0 ]; then
    rm -f "$tmp"
    echo "SETTINGS_RESIDUE_PREIMAGE_CORRUPT: $dirty_path preimage bytes fail sha check (fail-closed)" >&2
    exit 5
  fi
  mv "$tmp" "$target"
  restored_sha=$(sha256_of "$target")
  [ "$restored_sha" = "$pre_sha" ] || {
    echo "SETTINGS_RESIDUE_PREIMAGE_CORRUPT: $dirty_path post-restore sha mismatch (fail-closed)" >&2
    exit 5
  }
  echo "SETTINGS_RESIDUE_RESTORED: $dirty_path -> preimage sha256=$pre_sha"
}

for dirty_path in "${dirty_paths[@]}"; do
  restore_entry "$dirty_path"
done

if [ "$EXECUTE" -eq 1 ]; then
  remaining=$(git -C "$WORKTREE" status --porcelain=v1 --untracked-files=all 2>/dev/null | wc -l | tr -d ' ')
  [ "$remaining" = "0" ] || {
    echo "SETTINGS_RESIDUE_OTHER_DIRTY: $remaining dirty path(s) remain after restore (fail-closed)" >&2
    exit 6
  }
  echo "SETTINGS_RESIDUE_RELEASED: entries=$releasable_count worktree=$WORKTREE"
else
  echo "SETTINGS_RESIDUE_PLAN_COMPLETE: entries=$releasable_count mode=dry-run"
fi
