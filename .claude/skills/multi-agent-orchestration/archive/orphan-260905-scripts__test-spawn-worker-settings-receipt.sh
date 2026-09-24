#!/usr/bin/env bash
# test-spawn-worker-settings-receipt.sh — settings 残留 receipt 写入模块的确定性矩阵。
# 覆盖：tracked 精确快照、fresh 文件（preimage absent）、无增量不写 receipt、
# DRY_RUN 不写 receipt、轻量模式跳过、receipt 合同字段。
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RECEIPT_MODULE="$SCRIPT_DIR/spawn-worker-settings-receipt.sh"
CASE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/spawn-settings-receipt.XXXXXX")
trap 'rm -rf "$CASE_ROOT"' EXIT

passed=0
failed=0

ok() { printf 'PASS: %s\n' "$1"; passed=$((passed + 1)); }
bad() { printf 'FAIL: %s\n' "$1" >&2; failed=$((failed + 1)); }
assert_true() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

if [ -f "$RECEIPT_MODULE" ]; then
  ok "receipt module exists"
else
  bad "receipt module exists"
  printf 'spawn-worker settings receipt tests: %s passed, %s failed\n' "$passed" "$failed"
  exit 1
fi

sha256_of() {
  python3 - "$1" <<'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())
PY
}

make_fixture() {
  FIXTURE="$CASE_ROOT/$1"
  WT="$FIXTURE/wt"
  COMMON="$FIXTURE/common.git"
  git init -q --bare --initial-branch=main "$COMMON"
  git clone -q "$COMMON" "$WT"
  git -C "$WT" config user.name Test
  git -C "$WT" config user.email test@example.invalid
  printf 'base\n' > "$WT/base.txt"
  git -C "$WT" add base.txt
  git -C "$WT" commit -qm base
  mkdir -p "$COMMON/agent-authority"
}

reset_globals() {
  DRY_RUN=0
  WORKTREE="$WT"
  SESSION="$1"
  BRANCH="feat/residue"
  WORKER_BACKEND="claude-code"
  SETTINGS_RESIDUE_RECEIPT_FILE="$COMMON/agent-authority/$SESSION.settings-residue.json"
  SETTINGS_RESIDUE_PATHS=()
  SETTINGS_RESIDUE_PRE_STATE=()
  SETTINGS_RESIDUE_PRE_SHA=()
  SETTINGS_RESIDUE_PRE_B64=()
  SETTINGS_RESIDUE_POST_STATE=()
  SETTINGS_RESIDUE_POST_SHA=()
  SETTINGS_RESIDUE_COUNT=0
}

# shellcheck source=spawn-worker-settings-receipt.sh
source "$RECEIPT_MODULE"

# --- Case 1: tracked settings 文件，spawn 追加 hook 后 preimage/postimage 均精确入账 ---
make_fixture tracked-case
TRACKED="$WT/.claude/settings.local.json"
mkdir -p "$WT/.claude"
printf '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"orig"}]}]}}\n' > "$TRACKED"
git -C "$WT" add -f .claude/settings.local.json
git -C "$WT" commit -qm settings
PRE_SHA_EXPECTED=$(sha256_of "$TRACKED")

reset_globals tracked-session
settings_residue_capture_preimage
assert_true "preimage recorded for tracked settings" \
  test "$(printf '%s' "${SETTINGS_RESIDUE_PRE_SHA[0]}")" = "$PRE_SHA_EXPECTED"
assert_true "preimage state is present" \
  test "$(printf '%s' "${SETTINGS_RESIDUE_PRE_STATE[0]}")" = "present"

# 模拟 spawn 的 merge_pretool_hook 写入（幂等合并一条新 hook）
jq --arg matcher "Edit|Write" --arg command "bash guard.sh" \
  '(.hooks.PreToolUse // []) + [{matcher: $matcher, hooks: [{type: "command", command: $command}]}]' \
  "$TRACKED" > "$TRACKED.tmp" && mv "$TRACKED.tmp" "$TRACKED"
POST_SHA_EXPECTED=$(sha256_of "$TRACKED")

settings_residue_capture_postimage_and_write_receipt
assert_true "receipt file written after delta" test -f "$SETTINGS_RESIDUE_RECEIPT_FILE"
assert_true "receipt schema is declared" \
  jq -e '.schema == "multi-agent-orchestration.settings-residue-receipt.v1"' "$SETTINGS_RESIDUE_RECEIPT_FILE" >/dev/null
assert_true "receipt binds session and worktree" \
  jq -e --arg s "$SESSION" --arg w "$WT" '.session == $s and .worktree == $w' "$SETTINGS_RESIDUE_RECEIPT_FILE" >/dev/null
assert_true "receipt records exact preimage sha" \
  jq -e --arg sha "$PRE_SHA_EXPECTED" '.files[0].preimage.sha256 == $sha' "$SETTINGS_RESIDUE_RECEIPT_FILE" >/dev/null
assert_true "receipt records exact postimage sha" \
  jq -e --arg sha "$POST_SHA_EXPECTED" '.files[0].postimage.sha256 == $sha' "$SETTINGS_RESIDUE_RECEIPT_FILE" >/dev/null
decoded_preimage_sha=$(jq -r '.files[0].preimage.content_base64' "$SETTINGS_RESIDUE_RECEIPT_FILE" | python3 -c \
  'import base64, hashlib, sys
print(hashlib.sha256(base64.b64decode(sys.stdin.read().strip())).hexdigest())')
assert_true "preimage content_base64 decodes to original bytes" test "$decoded_preimage_sha" = "$PRE_SHA_EXPECTED"

# --- Case 2: settings 文件 spawn 前不存在（fresh worktree），preimage.state=absent ---
make_fixture fresh-case
reset_globals fresh-session
mkdir -p "$WT/.claude"
settings_residue_capture_preimage
printf '{"hooks":{"PreToolUse":[]}}\n' > "$WT/.claude/settings.local.json"
settings_residue_capture_postimage_and_write_receipt
assert_true "fresh file receipt written" test -f "$SETTINGS_RESIDUE_RECEIPT_FILE"
assert_true "fresh file preimage state is absent" \
  jq -e '.files[0].preimage.state == "absent"' "$SETTINGS_RESIDUE_RECEIPT_FILE" >/dev/null
assert_true "fresh file preimage content is null" \
  jq -e '.files[0].preimage.content_base64 == null' "$SETTINGS_RESIDUE_RECEIPT_FILE" >/dev/null

# --- Case 3: 无增量（pre==post）不写 receipt ---
make_fixture no-delta-case
mkdir -p "$WT/.claude"
printf '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"existing"}]}]}}\n' > "$WT/.claude/settings.local.json"
git -C "$WT" add -f .claude/settings.local.json
git -C "$WT" commit -qm settings
reset_globals no-delta-session
settings_residue_capture_preimage
# 模拟 merge_pretool_hook 去重后无需变更（post 字节与 pre 完全一致）
settings_residue_capture_postimage_and_write_receipt
assert_true "no-delta spawn writes no receipt" test ! -e "$SETTINGS_RESIDUE_RECEIPT_FILE"

# --- Case 4: DRY_RUN 不写 receipt ---
make_fixture dryrun-case
mkdir -p "$WT/.claude"
printf '{"hooks":{}}\n' > "$WT/.claude/settings.local.json"
git -C "$WT" add -f .claude/settings.local.json
git -C "$WT" commit -qm settings
reset_globals dryrun-session
DRY_RUN=1
settings_residue_capture_preimage
printf '{"hooks":{"PreToolUse":[]}}\n' > "$WT/.claude/settings.local.json"
settings_residue_capture_postimage_and_write_receipt
assert_true "dry-run writes no receipt" test ! -e "$SETTINGS_RESIDUE_RECEIPT_FILE"

# --- Case 5: 轻量模式（receipt 文件为空）整体跳过 ---
make_fixture lightweight-case
reset_globals lightweight-session
SETTINGS_RESIDUE_RECEIPT_FILE=""
settings_residue_capture_preimage
settings_residue_capture_postimage_and_write_receipt
ok "lightweight mode skips receipt without error"

# --- Case 6: worktree 不存在（dry-run 新建路径）不崩溃 ---
make_fixture missing-wt-case
reset_globals missing-wt-session
WORKTREE="$CASE_ROOT/missing-wt-case/does-not-exist"
settings_residue_capture_preimage
settings_residue_capture_postimage_and_write_receipt
assert_true "missing worktree writes no receipt" test ! -e "$COMMON/agent-authority/missing-wt-session.settings-residue.json"

# --- Case 7: 入口接线存在（source + 捕获顺序：preimage 在 guard 前，postimage 在 scope 后）---
if grep -Fq 'source "$SCRIPT_DIR/spawn-worker-settings-receipt.sh"' "$SCRIPT_DIR/spawn-worker.sh"; then
  ok "entrypoint sources the receipt module"
else
  bad "entrypoint sources the receipt module"
fi
preimage_line=$(grep -n 'settings_residue_capture_preimage$' "$SCRIPT_DIR/spawn-worker.sh" | head -1 | cut -d: -f1 || true)
guard_line=$(grep -n '^dependency_install_guard_setup$' "$SCRIPT_DIR/spawn-worker.sh" | head -1 | cut -d: -f1 || true)
scope_line=$(grep -n '^scope_guard_setup$' "$SCRIPT_DIR/spawn-worker.sh" | head -1 | cut -d: -f1 || true)
postimage_line=$(grep -n 'settings_residue_capture_postimage_and_write_receipt$' "$SCRIPT_DIR/spawn-worker.sh" | head -1 | cut -d: -f1 || true)
if [ -n "$preimage_line" ] && [ -n "$guard_line" ] && [ "$preimage_line" -lt "$guard_line" ]; then
  ok "preimage captured before install guard setup"
else
  bad "preimage captured before install guard setup"
fi
if [ -n "$scope_line" ] && [ -n "$postimage_line" ] && [ "$scope_line" -lt "$postimage_line" ]; then
  ok "postimage captured after scope guard setup"
else
  bad "postimage captured after scope guard setup"
fi
if grep -Fq 'SETTINGS_RESIDUE_RECEIPT_FILE="$git_common_dir/agent-authority/$SESSION.settings-residue.json"' "$SCRIPT_DIR/spawn-worker.sh"; then
  ok "receipt path shares the authority receipt root"
else
  bad "receipt path shares the authority receipt root"
fi

printf 'spawn-worker settings receipt tests: %s passed, %s failed\n' "$passed" "$failed"
[ "$failed" -eq 0 ]
