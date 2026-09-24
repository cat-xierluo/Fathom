#!/usr/bin/env bash
# 配置读取窄回归：大合法标量、错误传播、checker 读失败和基线 schema 合同。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASH_BIN="${BASH:-bash}"
TEST_TMP="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP"' EXIT
PROJECT="$TEST_TMP/project"
mkdir -p "$PROJECT"
CONFIG="$PROJECT/doc-curator.yaml"
OUT="$TEST_TMP/out"; ERR="$TEST_TMP/err"
PASS=0; FAIL=0; LAST_RC=0
pass() { printf 'PASS %s\n' "$1"; PASS=$((PASS+1)); }
fail() { printf 'FAIL %s\n' "$1" >&2; FAIL=$((FAIL+1)); sed -n '1,8p' "$OUT" >&2; sed -n '1,8p' "$ERR" >&2; }
assert_rc() { if [ "$LAST_RC" -eq "$1" ]; then pass "$2"; else fail "$2 (expected=$1 actual=$LAST_RC)"; fi; }
assert_rule() { if grep -qF "\"rule_id\":\"$1\"" "$OUT"; then pass "$2"; else fail "$2"; fi; }
capture() { if "$@" > "$OUT" 2> "$ERR"; then LAST_RC=0; else LAST_RC=$?; fi; }
scan() { capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT" --config "$CONFIG" "$@"; }
reset_fixture() {
  printf '# Readme\n' > "$PROJECT/README.md"
  printf 'schema_version: 2\nfiles:\n  - path: README.md\n    role: readme\ncontext_sync:\n  enabled: true\n  change_types:\n    - pattern: "**"\n      expect_tasks: true\n' > "$CONFIG"
}

reset_fixture
# 有效 YAML 子集中的单行标量；旧 grep -q 的早退让 printf 以 SIGPIPE=141 退出。
printf 'diagnostic_padding: "' >> "$CONFIG"
printf '%200000s' x | tr ' ' x >> "$CONFIG"
printf '"\n' >> "$CONFIG"
for ((i=1;i<=3;i++)); do
  scan --only config
  assert_rc 0 "large valid configuration round $i"
  assert_rule config-contract 'large config contract actually ran'
  if [ -z "$(grep -E '配置缺少|YAML 解析失败' "$ERR" || true)" ]; then pass 'no contradictory parse warning'; else fail 'no contradictory parse warning'; fi
done
capture env DOC_CURATOR_REPO="$PROJECT" DOC_CURATOR_CONFIG="$CONFIG" "$BASH_BIN" -c \
  '. "$1"; _cfg_ensure_flat; printf "role=%s value=%s index=%s\n" "$(cfg_file_by_role readme)" "$(cfg_scalar context_sync.enabled)" "$(cfg_change_type_indices)"' _ "$SCRIPT_DIR/common.sh"
assert_rc 0 'large config lookups consume their full input'
if grep -qFx 'role=README.md value=true index=1' "$OUT"; then pass 'large config lookups retain values'; else fail 'large config lookups retain values'; fi

# 与原事故不同：这里故意缺 files 来验证 helper 的非零返回不被 cmdsub 吞掉。
BAD_CONFIG="$TEST_TMP/bad.yaml"
printf 'schema_version: 2\ncontext_sync:\n  enabled: true\n' > "$BAD_CONFIG"
for spec in 'cfg_scalar context_sync.enabled' 'cfg_file_paths' 'cfg_file_by_role readme' \
  'cfg_rule_get hard_rules x id' 'cfg_rule_enabled hard_rules x' 'cfg_change_type_indices' \
  'cfg_list_indices files path' 'cfg_list_field files 1 path' 'cfg_change_type_field 1 pattern'; do
  read -r -a args <<< "$spec"
  capture env DOC_CURATOR_REPO="$PROJECT" DOC_CURATOR_CONFIG="$BAD_CONFIG" "$BASH_BIN" -c \
    '. "$1"; shift; if value="$("$@")"; then query_rc=0; else query_rc=$?; fi; printf "query_rc=%s bytes=%s\n" "$query_rc" "${#value}"' _ "$SCRIPT_DIR/common.sh" "${args[@]}"
  assert_rc 0 "diagnostic harness executes $spec"
  if grep -qFx 'query_rc=65 bytes=0' "$OUT"; then pass "$spec propagates load failure"; else fail "$spec propagates load failure"; fi
done

# 真实子 checker 读取阶段故障，父 scan/config checker 可正常读取。
reset_fixture
mkdir "$TEST_TMP/bin"
REAL_AWK="$(command -v awk)"
printf '#!/usr/bin/env bash\nif [[ "${DOC_CURATOR_CHECKER_ID:-}" = context-sync && "${1:-}" = -f && "${2:-}" = */yaml-flatten.awk ]]; then exit 2; fi\nexec "%s" "$@"\n' "$REAL_AWK" > "$TEST_TMP/bin/awk"
chmod +x "$TEST_TMP/bin/awk"
PATH="$TEST_TMP/bin:$PATH" scan --only context-sync --working-tree --profile merge-gate
assert_rc 1 'child config read failure cannot produce compact pass'
if grep -qF '"checker-execution-error"' "$OUT" && ! grep -qF '"next_action":"pass"' "$OUT"; then
  pass 'child load error is a hard result, not silent disabled/default'
else fail 'child load error is a hard result, not silent disabled/default'; fi

for schema in 2 3; do
  reset_fixture
  if [ "$schema" -eq 3 ]; then
    cp "$SKILL_ROOT/config/context-value-claims.example.yaml" "$CONFIG"
    printf 'Role: question-draft\n' > "$PROJECT/interview.md"
    printf 'Status: READY\n' > "$PROJECT/task.md"
  fi
  state="$TEST_TMP/schema-$schema.state.json"
  DOC_CURATOR_STATE_FILE="$state" scan --init-baseline
  assert_rc 0 "schema $schema can initialize a baseline"
  assert_rule config-contract 'baseline passes the real configuration contract'
  assert_rule baseline-initialized 'baseline actually produced'
  if [ -s "$state" ] && grep -qF '"line_count"' "$state"; then pass "schema $schema state exists"; else fail "schema $schema state exists"; fi
done

# schema 3 是 reader 能力，不是绕过合同的许可：非法 boolean 不得写状态。
printf 'schema_version: 3\nfiles:\n  - path: README.md\ncontext_truth:\n  enabled: yes\n' > "$CONFIG"
BAD_STATE="$TEST_TMP/bad.state.json"
DOC_CURATOR_STATE_FILE="$BAD_STATE" scan --init-baseline
assert_rc 1 'invalid schema-3 contract blocks baseline write'
assert_rule config-boolean-invalid 'baseline reuses boolean contract'
if [ ! -e "$BAD_STATE" ]; then pass 'invalid baseline leaves no state'; else fail 'invalid baseline leaves no state'; fi

printf '\nTOTAL config-read pass=%s fail=%s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
