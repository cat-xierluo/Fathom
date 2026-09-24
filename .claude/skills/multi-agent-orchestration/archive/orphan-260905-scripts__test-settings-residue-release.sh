#!/usr/bin/env bash
# test-settings-residue-release.sh — release-settings-residue.sh 的 failure-first 矩阵。
# 覆盖：精准恢复成功（dry-run + execute + 幂等）、用户二次修改拒绝、其他脏文件拒绝、
# 无 receipt/无 preimage 拒绝、假脏旧路径（receipt 未覆盖的 dirty path）拒绝、
# receipt 身份不匹配拒绝、preimage 内容损坏拒绝、preimage absent 恢复为删除。
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RELEASE="$SCRIPT_DIR/release-settings-residue.sh"
CASE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/settings-residue-release.XXXXXX")
trap 'rm -rf "$CASE_ROOT"' EXIT

passed=0
failed=0
ok() { printf 'PASS: %s\n' "$1"; passed=$((passed + 1)); }
bad() { printf 'FAIL: %s\n' "$1" >&2; failed=$((failed + 1)); }
assert_true() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

if [ -f "$RELEASE" ]; then
  ok "release helper exists"
else
  bad "release helper exists"
  printf 'settings residue release tests: %s passed, %s failed\n' "$passed" "$failed"
  exit 1
fi

sha256_of() {
  python3 - "$1" <<'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())
PY
}

b64_of() {
  base64 < "$1" | tr -d '\n'
}

PRE_CONTENT='{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"orig"}]}]}}
'
POST_CONTENT='{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"orig"}]},{"matcher":"Edit|Write","hooks":[{"type":"command","command":"bash guard.sh"}]}]}}
'

# jq 不提供 sha256，用 python 计算后以参数传入 receipt。
write_receipt_exact() {
  local receipt=$1 worktree=$2 session=$3 branch=$4 pre_sha=$5 post_sha=$6
  jq -n \
    --arg schema "multi-agent-orchestration.settings-residue-receipt.v1" \
    --arg created_at "2026-09-05T00:00:00Z" \
    --arg session "$session" \
    --arg worktree "$worktree" \
    --arg branch "$branch" \
    --arg pre_sha "$pre_sha" \
    --arg post_sha "$post_sha" \
    --arg pre_b64 "$(printf '%s' "$PRE_CONTENT" | base64 | tr -d '\n')" \
    '{schema: $schema, created_at: $created_at, session: $session, worktree: $worktree, branch: $branch,
      files: [{
        path: ".claude/settings.local.json",
        tracked: true,
        preimage: {state: "present", sha256: $pre_sha, content_base64: $pre_b64},
        postimage: {state: "present", sha256: $post_sha}
      }]}' > "$receipt"
}

make_fixture() {
  local name=$1
  FIXTURE="$CASE_ROOT/$name"
  WT="$FIXTURE/wt"
  COMMON="$FIXTURE/common.git"
  RECEIPT="$COMMON/agent-authority/worker-a.settings-residue.json"
  git init -q --bare --initial-branch=main "$COMMON"
  git clone -q "$COMMON" "$WT"
  git -C "$WT" config user.name Test
  git -C "$WT" config user.email test@example.invalid
  printf 'base\n' > "$WT/base.txt"
  mkdir -p "$WT/.claude"
  printf '%s' "$PRE_CONTENT" > "$WT/.claude/settings.local.json"
  git -C "$WT" add base.txt
  git -C "$WT" add -f .claude/settings.local.json
  git -C "$WT" commit -qm base
  mkdir -p "$COMMON/agent-authority"
  # 复现 spawn 工具生成 postimage
  printf '%s' "$POST_CONTENT" > "$WT/.claude/settings.local.json"
  PRE_SHA=$(printf '%s' "$PRE_CONTENT" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
  POST_SHA=$(printf '%s' "$POST_CONTENT" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
  write_receipt_exact "$RECEIPT" "$WT" worker-a feat/residue "$PRE_SHA" "$POST_SHA"
}

run_release() {
  set +e
  bash "$RELEASE" --project "$COMMON" --worktree "$WT" --session worker-a "$@" > "$CASE_ROOT/last.out" 2>&1
  LAST_RC=$?
  set -e
}

# --- 场景 1a: 精准恢复成功（dry-run 只出计划不改文件）---
make_fixture release-ok
run_release
assert_true "dry-run releasable residue exits 0" test "$LAST_RC" -eq 0
assert_true "dry-run prints restore plan" grep -q "SETTINGS_RESIDUE_PLAN" "$CASE_ROOT/last.out"
assert_true "dry-run does not restore" test "$(sha256_of "$WT/.claude/settings.local.json")" = "$POST_SHA"

# --- 场景 1b: execute 恢复 preimage，porcelain 干净 ---
run_release --execute
assert_true "execute release exits 0" test "$LAST_RC" -eq 0
assert_true "execute restores preimage bytes" test "$(sha256_of "$WT/.claude/settings.local.json")" = "$PRE_SHA"
assert_true "worktree porcelain clean after restore" test -z "$(git -C "$WT" status --porcelain)"

# --- 场景 1c: 幂等（恢复后再跑，nothing to release，仍退出 0）---
run_release --execute
assert_true "idempotent re-release exits 0" test "$LAST_RC" -eq 0
assert_true "idempotent re-release reports nothing to release" grep -q "SETTINGS_RESIDUE_NOTHING_TO_RELEASE" "$CASE_ROOT/last.out"

# --- 场景 2: 用户二次修改后拒绝（postimage 不匹配，不恢复、不删除）---
make_fixture user-edit
printf '%s' "$POST_CONTENT" | sed 's/guard.sh/guard-user-edit.sh/' > "$WT/.claude/settings.local.json"
run_release --execute
assert_true "user-edited residue is refused (rc=4)" test "$LAST_RC" -eq 4
assert_true "refusal reports postimage mismatch" grep -q "SETTINGS_RESIDUE_POSTIMAGE_MISMATCH" "$CASE_ROOT/last.out"
assert_true "user edit is preserved verbatim" grep -q "guard-user-edit.sh" "$WT/.claude/settings.local.json"
assert_true "worktree retained on refusal" test -d "$WT"

# --- 场景 3: 其他脏文件存在时拒绝（恢复前 fail-closed）---
make_fixture other-dirty
printf 'worker note\n' > "$WT/stray.txt"
run_release --execute
assert_true "other dirty path refuses release (rc=6)" test "$LAST_RC" -eq 6
assert_true "refusal reports other dirty path" grep -q "SETTINGS_RESIDUE_OTHER_DIRTY" "$CASE_ROOT/last.out"
assert_true "refusal happens before restore (settings untouched)" test "$(sha256_of "$WT/.claude/settings.local.json")" = "$POST_SHA"
assert_true "other dirty file preserved" test "$(cat "$WT/stray.txt")" = "worker note"

# --- 场景 4: 无 receipt（无 preimage）拒绝 ---
make_fixture no-receipt
rm -f "$RECEIPT"
run_release --execute
assert_true "missing receipt is refused (rc=3)" test "$LAST_RC" -eq 3
assert_true "missing receipt reports NO_RECEIPT" grep -q "SETTINGS_RESIDUE_NO_RECEIPT" "$CASE_ROOT/last.out"
assert_true "missing refusal keeps tool postimage untouched" test "$(sha256_of "$WT/.claude/settings.local.json")" = "$POST_SHA"

# --- 场景 5: 假脏旧路径——dirty path 不在 receipt 覆盖内（未知来源）拒绝 ---
make_fixture fake-path
run_release --execute
assert_true "baseline releasable before fake path case" test "$LAST_RC" -eq 0
mkdir -p "$WT/.codebuddy"
printf 'x\n' > "$WT/.codebuddy/settings.local.json"
run_release --execute
assert_true "uncovered dirty path refuses (rc=6)" test "$LAST_RC" -eq 6
assert_true "uncovered path reported as other dirty" grep -q "SETTINGS_RESIDUE_OTHER_DIRTY" "$CASE_ROOT/last.out"
assert_true "uncovered file preserved" test -f "$WT/.codebuddy/settings.local.json"

# --- 场景 6: receipt 身份不匹配（另一 worktree/session）拒绝 ---
make_fixture identity
OTHER_WT="$FIXTURE/other-wt"
git clone -q "$COMMON" "$OTHER_WT" 2>/dev/null || true
mkdir -p "$OTHER_WT/.claude"
printf '%s' "$POST_CONTENT" > "$OTHER_WT/.claude/settings.local.json"
run_release --worktree "$OTHER_WT" --execute
assert_true "worktree identity mismatch refuses (rc=4)" test "$LAST_RC" -eq 4
assert_true "identity mismatch reported" grep -q "SETTINGS_RESIDUE_IDENTITY_MISMATCH" "$CASE_ROOT/last.out"
assert_true "identity mismatch preserves other worktree state" test -f "$OTHER_WT/.claude/settings.local.json"

# --- 场景 7: preimage 内容损坏（base64 与 sha 不一致）拒绝 ---
make_fixture corrupt-preimage
write_receipt_exact "$RECEIPT" "$WT" worker-a feat/residue "$PRE_SHA" "$POST_SHA"
python3 - "$RECEIPT" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["files"][0]["preimage"]["content_base64"] = "bnVsbC1iYWQ="
json.dump(data, open(path, "w"))
PY
run_release --execute
assert_true "corrupt preimage content refuses (rc=5)" test "$LAST_RC" -eq 5
assert_true "corrupt preimage reported" grep -q "SETTINGS_RESIDUE_PREIMAGE_CORRUPT" "$CASE_ROOT/last.out"

# --- 场景 8: 非法 receipt JSON → 用法级失败 ---
make_fixture bad-json
printf '{not json' > "$RECEIPT"
run_release --execute
assert_true "invalid receipt JSON fails (rc=2)" test "$LAST_RC" -eq 2

# --- 场景 9: preimage absent 恢复为删除文件（spawn 新建的未跟踪文件）---
make_fixture absent-preimage
git -C "$WT" checkout -q -- .claude/settings.local.json
printf 'tool generated\n' > "$WT/spawn-created.json"
CREATED_SHA=$(sha256_of "$WT/spawn-created.json")
jq -n \
  --arg schema "multi-agent-orchestration.settings-residue-receipt.v1" \
  --arg session "worker-a" --arg worktree "$WT" --arg branch "feat/residue" \
  --arg post_sha "$CREATED_SHA" \
  '{schema: $schema, created_at: "2026-09-05T00:00:00Z", session: $session, worktree: $worktree, branch: $branch,
    files: [{path: "spawn-created.json", tracked: false,
      preimage: {state: "absent", sha256: "", content_base64: null},
      postimage: {state: "present", sha256: $post_sha}}]}' > "$RECEIPT"
run_release --execute
assert_true "absent-preimage release exits 0" test "$LAST_RC" -eq 0
assert_true "absent preimage restore deletes the file" test ! -e "$WT/spawn-created.json"
assert_true "porcelain clean after absent restore" test -z "$(git -C "$WT" status --porcelain)"

# --- 场景 10: 当前文件缺失但 postimage=present → postimage 不匹配拒绝 ---
make_fixture deleted-post
rm -f "$WT/.claude/settings.local.json"
run_release --execute
assert_true "deleted tool file is a postimage mismatch (rc=4)" test "$LAST_RC" -eq 4

printf 'settings residue release tests: %s passed, %s failed\n' "$passed" "$failed"
[ "$failed" -eq 0 ]
