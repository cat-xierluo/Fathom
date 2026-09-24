#!/usr/bin/env bash
# test-clean-worktree.sh — clean-worktree.sh 的 settings 残留 release 接线矩阵。
# 覆盖：有 receipt 且严格匹配时自动恢复并继续精确清理；用户二次修改/其他脏文件/
# 无 receipt 均 fail-closed 保留；dry-run 只出计划；无残留的干净 worktree 行为不变。
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CLEAN="$SCRIPT_DIR/clean-worktree.sh"
RELEASE="$SCRIPT_DIR/release-settings-residue.sh"
CASE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/clean-worktree-residue.XXXXXX")
trap 'rm -rf "$CASE_ROOT"' EXIT

passed=0
failed=0
ok() { printf 'PASS: %s\n' "$1"; passed=$((passed + 1)); }
bad() { printf 'FAIL: %s\n' "$1" >&2; failed=$((failed + 1)); }
assert_true() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

for required in "$CLEAN" "$RELEASE"; do
  if [ -f "$required" ]; then ok "required script exists: $(basename "$required")"; else
    bad "required script exists: $(basename "$required")"
  fi
done
if grep -Fq 'release-settings-residue.sh' "$CLEAN"; then
  ok "clean-worktree wires the release helper"
else
  bad "clean-worktree wires the release helper"
fi

sha256_of() {
  python3 - "$1" <<'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())
PY
}

PRE_CONTENT='{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"orig"}]}]}}
'
POST_CONTENT='{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"orig"}]},{"matcher":"Edit|Write","hooks":[{"type":"command","command":"bash guard.sh"}]}]}}
'

make_fixture() {
  local name=$1
  ORIGIN="$CASE_ROOT/$name-origin.git"
  PROJECT="$CASE_ROOT/$name-project"
  WT="$CASE_ROOT/$name-wt"
  BRANCH="feat/$name"
  SESSION="worker-a"
  RECEIPT=""
  git init -q --bare --initial-branch=main "$ORIGIN"
  git clone -q "$ORIGIN" "$PROJECT"
  git -C "$PROJECT" config user.name Test
  git -C "$PROJECT" config user.email test@example.invalid
  printf 'base\n' > "$PROJECT/base.txt"
  printf '.claude/agent-sessions/\n' > "$PROJECT/.gitignore"
  git -C "$PROJECT" add base.txt .gitignore
  git -C "$PROJECT" commit -qm base
  git -C "$PROJECT" push -q -u origin main
  git -C "$PROJECT" worktree add -q -b "$BRANCH" "$WT" main
  # spawn 工具生成的 settings 残留
  mkdir -p "$WT/.claude"
  printf '%s' "$PRE_CONTENT" > "$WT/.claude/settings.local.json"
  git -C "$WT" add -f .claude/settings.local.json
  git -C "$WT" commit -qm settings
  # 分支已合入 main，令 --delete-branch 的安全 -d 可通过（本测试不覆盖 delivery 门禁）
  git -C "$PROJECT" merge -q "$BRANCH"
  git -C "$PROJECT" push -q origin main
  printf '%s' "$POST_CONTENT" > "$WT/.claude/settings.local.json"
  mkdir -p "$WT/.claude/agent-sessions/$SESSION"
  jq -n --arg project "$PROJECT" --arg worktree "$WT" --arg branch "$BRANCH" --arg session "$SESSION" \
    '{project:$project,worktree:$worktree,branch:$branch,branch_lifecycle:"ephemeral-worker",base_ref:"origin/main",session:{id:$session},runtime:{provider_lease:{file:""}}}' \
    > "$WT/.claude/agent-sessions/$SESSION/METADATA.json"
  if [ "${2:-with-receipt}" = "with-receipt" ]; then
    COMMON=$(git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir)
    RECEIPT="$COMMON/agent-authority/$SESSION.settings-residue.json"
    mkdir -p "$COMMON/agent-authority"
    PRE_SHA=$(printf '%s' "$PRE_CONTENT" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
    POST_SHA=$(printf '%s' "$POST_CONTENT" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
    jq -n \
      --arg schema "multi-agent-orchestration.settings-residue-receipt.v1" \
      --arg session "$SESSION" --arg worktree "$WT" --arg branch "$BRANCH" \
      --arg pre_sha "$PRE_SHA" --arg post_sha "$POST_SHA" \
      --arg pre_b64 "$(printf '%s' "$PRE_CONTENT" | base64 | tr -d '\n')" \
      '{schema: $schema, created_at: "2026-09-05T00:00:00Z", session: $session, worktree: $worktree, branch: $branch,
        files: [{path: ".claude/settings.local.json", tracked: true,
          preimage: {state: "present", sha256: $pre_sha, content_base64: $pre_b64},
          postimage: {state: "present", sha256: $post_sha}}]}' > "$RECEIPT"
  fi
}

run_clean() {
  set +e
  bash "$CLEAN" --project "$PROJECT" --branch "$BRANCH" --session "$SESSION" --worktree "$WT" "$@" > "$CASE_ROOT/last.out" 2>&1
  LAST_RC=$?
  set -e
}

# --- 1: 精确匹配 → dry-run 出计划，不改文件 ---
make_fixture ok-case
run_clean
assert_true "dry-run succeeds with releasable residue" test "$LAST_RC" -eq 0
assert_true "dry-run plans residue release" grep -q "SETTINGS_RESIDUE_PLAN" "$CASE_ROOT/last.out"
assert_true "dry-run keeps worktree" test -d "$WT"
assert_true "dry-run does not restore" test "$(sha256_of "$WT/.claude/settings.local.json")" = "$(printf '%s' "$POST_CONTENT" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"

# --- 2: execute → 恢复 preimage 并继续精确清理 ---
run_clean --execute --delete-branch
assert_true "execute cleanup succeeds" test "$LAST_RC" -eq 0
assert_true "worktree removed after residue release" test ! -d "$WT"
if git -C "$PROJECT" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  bad "local branch deleted"
else
  ok "local branch deleted"
fi

# --- 3: 用户二次修改 → fail-closed 保留 ---
make_fixture user-edit
printf '%s' "$POST_CONTENT" | sed 's/guard.sh/guard-user.sh/' > "$WT/.claude/settings.local.json"
run_clean --execute --delete-branch
assert_true "user-edited cleanup refuses (rc=2)" test "$LAST_RC" -eq 2
assert_true "user-edited cleanup retains worktree" test -d "$WT"
assert_true "user edit preserved" grep -q "guard-user.sh" "$WT/.claude/settings.local.json"

# --- 4: 其他脏文件 → fail-closed 保留 ---
make_fixture other-dirty
printf 'note\n' > "$WT/stray.txt"
run_clean --execute
assert_true "other-dirty cleanup refuses (rc=2)" test "$LAST_RC" -eq 2
assert_true "other-dirty retains worktree" test -d "$WT"
assert_true "other-dirty file preserved" test "$(cat "$WT/stray.txt")" = "note"
assert_true "residue untouched by refused cleanup" test -f "$WT/.claude/settings.local.json"

# --- 5: 无 receipt → 原 dirty 门禁保留 ---
make_fixture no-receipt-case without-receipt
run_clean --execute
assert_true "no-receipt cleanup refuses (rc=2)" test "$LAST_RC" -eq 2
assert_true "no-receipt retains worktree" test -d "$WT"

# --- 6: 干净 worktree（无任何残留）行为不变 ---
make_fixture clean-case
git -C "$WT" checkout -q -- .claude/settings.local.json
run_clean --execute --delete-branch
assert_true "clean worktree cleanup succeeds" test "$LAST_RC" -eq 0
assert_true "clean worktree removed" test ! -d "$WT"

printf 'clean-worktree tests: %s passed, %s failed\n' "$passed" "$failed"
[ "$failed" -eq 0 ]
