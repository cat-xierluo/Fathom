#!/usr/bin/env bash
# test-spawn-worker.sh — spawn-worker settings 残留 receipt 接线契约 + dry-run 入口矩阵。
# A 部分：静态接线契约（确定性，基线上必须红）。
# B 部分：真实入口 --dry-run（依赖运行环境的 PM harness 可证明性；无法证明时按 SKIP
#         记录，不算绿证据）。dry-run 必须不写 receipt、不改 settings、正常退出。
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SPAWN="$SCRIPT_DIR/spawn-worker.sh"
RELEASE="$SCRIPT_DIR/release-settings-residue.sh"
MODULE="$SCRIPT_DIR/spawn-worker-settings-receipt.sh"
CLEAN="$SCRIPT_DIR/clean-worktree.sh"
PM_CLEANUP="$SCRIPT_DIR/pm-cleanup-worker.sh"
CASE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/spawn-worker-wiring.XXXXXX")
trap 'rm -rf "$CASE_ROOT"' EXIT

passed=0
failed=0
skipped=0
ok() { printf 'PASS: %s\n' "$1"; passed=$((passed + 1)); }
bad() { printf 'FAIL: %s\n' "$1" >&2; failed=$((failed + 1)); }
skip() { printf 'SKIP: %s\n' "$1" >&2; skipped=$((skipped + 1)); }
assert_true() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

for required in "$SPAWN" "$MODULE" "$RELEASE" "$CLEAN" "$PM_CLEANUP"; do
  if [ -f "$required" ]; then ok "required script exists: $(basename "$required")"; else
    bad "required script exists: $(basename "$required")"
  fi
done

# --- A. 静态接线契约 ---
# 语法门禁：本沙箱可能不放行 find|xargs bash -n，这里对全部接线脚本逐个 bash -n。
syntax_gate_failed=0
for syntax_target in "$SPAWN" "$MODULE" "$RELEASE" "$CLEAN" "$PM_CLEANUP" \
  "$SCRIPT_DIR/test-spawn-worker-settings-receipt.sh" \
  "$SCRIPT_DIR/test-settings-residue-release.sh" \
  "$SCRIPT_DIR/test-clean-worktree.sh" \
  "$SCRIPT_DIR/test-pm-cleanup-worker.sh" \
  "$SCRIPT_DIR/test-spawn-worker.sh"; do
  if bash -n "$syntax_target" 2>/dev/null; then
    ok "syntax ok: $(basename "$syntax_target")"
  else
    bad "syntax ok: $(basename "$syntax_target")"
    syntax_gate_failed=1
  fi
done

if grep -Fq 'source "$SCRIPT_DIR/spawn-worker-settings-receipt.sh"' "$SPAWN" \
  && ! grep -q '^settings_residue_capture_preimage() {' "$SPAWN"; then
  ok "entrypoint delegates receipt writing to the module"
else
  bad "entrypoint delegates receipt writing to the module"
fi

preimage_line=$(grep -n 'settings_residue_capture_preimage$' "$SPAWN" | head -1 | cut -d: -f1 || true)
guard_line=$(grep -n '^dependency_install_guard_setup$' "$SPAWN" | head -1 | cut -d: -f1 || true)
scope_line=$(grep -n '^scope_guard_setup$' "$SPAWN" | head -1 | cut -d: -f1 || true)
postimage_line=$(grep -n 'settings_residue_capture_postimage_and_write_receipt$' "$SPAWN" | head -1 | cut -d: -f1 || true)
if [ -n "$preimage_line" ] && [ -n "$guard_line" ] && [ "$preimage_line" -lt "$guard_line" ]; then
  ok "preimage is captured before any spawn settings write"
else
  bad "preimage is captured before any spawn settings write"
fi
if [ -n "$scope_line" ] && [ -n "$postimage_line" ] && [ "$scope_line" -lt "$postimage_line" ]; then
  ok "postimage is captured after the last spawn settings write"
else
  bad "postimage is captured after the last spawn settings write"
fi

if grep -Fq 'SETTINGS_RESIDUE_RECEIPT_FILE="$git_common_dir/agent-authority/$SESSION.settings-residue.json"' "$SPAWN"; then
  ok "receipt lives in the git common-dir authority root (outside worker cwd)"
else
  bad "receipt lives in the git common-dir authority root (outside worker cwd)"
fi

if grep -Fq 'release-settings-residue.sh' "$CLEAN" && grep -Fq 'release-settings-residue.sh' "$PM_CLEANUP"; then
  ok "both cleanup entrypoints wire the release helper"
else
  bad "both cleanup entrypoints wire the release helper"
fi

# release helper 的职责边界：只恢复字节，不做任何 worktree/分支删除。
if grep -qE 'git (-C )?"?\$?[A-Za-z_]*"? worktree (remove|add)|branch -D|worktree remove' "$RELEASE"; then
  bad "release helper never mutates worktrees or branches"
else
  ok "release helper never mutates worktrees or branches"
fi

# release helper 默认 dry-run：没有 --execute 时禁止恢复写动作。
if grep -qF 'EXECUTE=0' "$RELEASE" && grep -qF -- '--execute' "$RELEASE"; then
  ok "release helper defaults to dry-run with explicit --execute channel"
else
  bad "release helper defaults to dry-run with explicit --execute channel"
fi

# --- B. 真实入口 dry-run ---
PROJ="$CASE_ROOT/proj"
WT="$CASE_ROOT/proj-wt"
BRANCH="feat/spawn-residue"
SESSION="spawn-residue-s"
git init -q --bare --initial-branch=main "$CASE_ROOT/origin.git"
git clone -q "$CASE_ROOT/origin.git" "$PROJ"
git -C "$PROJ" config user.name Test
git -C "$PROJ" config user.email test@example.invalid
printf 'base\n' > "$PROJ/base.txt"
git -C "$PROJ" add base.txt
git -C "$PROJ" commit -qm base
git -C "$PROJ" worktree add -q -b "$BRANCH" "$WT" main
mkdir -p "$WT/.claude"
SETTINGS_BEFORE='{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"orig"}]}]}}
'
printf '%s' "$SETTINGS_BEFORE" > "$WT/.claude/settings.local.json"
git -C "$WT" add -f .claude/settings.local.json
git -C "$WT" commit -qm settings
common_dir=$(git -C "$PROJ" rev-parse --path-format=absolute --git-common-dir)
receipt_path="$common_dir/agent-authority/$SESSION.settings-residue.json"

set +e
MULTI_AGENT_ORCHESTRATION_PERSONAL_CONFIG="$CASE_ROOT/no-personal-config.json" \
  bash "$SPAWN" \
  --project "$PROJ" --branch "$BRANCH" --session "$SESSION" \
  --worker-backend claude-code --no-orca-mode --dry-run \
  > "$CASE_ROOT/spawn-dry-run.out" 2>&1
SPAWN_RC=$?
set -e

if grep -q "cannot prove the current PM harness" "$CASE_ROOT/spawn-dry-run.out"; then
  skip "entrypoint dry-run (ambient shell cannot prove PM harness; module/cleanup suites carry the green evidence)"
else
  assert_true "dry-run spawn exits 0" test "$SPAWN_RC" -eq 0
  assert_true "dry-run spawn plans hook install" grep -q "SPAWN_WORKER_HOOK_DRY_RUN" "$CASE_ROOT/spawn-dry-run.out"
  assert_true "dry-run spawn writes no settings residue receipt" test ! -e "$receipt_path"
  assert_true "dry-run spawn leaves tracked settings untouched" \
    test -z "$(git -C "$WT" status --porcelain)"
  if grep -q "SPAWN_WORKER_SETTINGS_RECEIPT" "$CASE_ROOT/spawn-dry-run.out"; then
    ok "dry-run spawn reports the settings receipt boundary"
  else
    bad "dry-run spawn reports the settings receipt boundary"
  fi
fi

printf 'spawn-worker wiring tests: %s passed, %s failed, %s skipped\n' "$passed" "$failed" "$skipped"
[ "$failed" -eq 0 ]
