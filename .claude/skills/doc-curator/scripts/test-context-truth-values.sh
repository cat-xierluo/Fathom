#!/usr/bin/env bash
# Narrow, offline behavioral regressions. Fixtures and scan output live only in a fresh temp dir.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASH_BIN="${BASH:-bash}"
TEST_TMP="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP"' EXIT
PROJECT="$TEST_TMP/project"
mkdir -p "$PROJECT" "$TEST_TMP/runtime"
CONFIG="$PROJECT/doc-curator.yaml"
OUT="$TEST_TMP/out.jsonl"
ERR="$TEST_TMP/err.log"
PASS=0; FAIL=0; LAST_RC=0
pass() { printf 'PASS %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL %s\n' "$1" >&2; FAIL=$((FAIL + 1)); sed -n '1,8p' "$OUT" >&2; }
assert_rc() { if [ "$LAST_RC" -eq "$1" ]; then pass "$2"; else fail "$2 (expected=$1 actual=$LAST_RC)"; fi; }
assert_rule() { if grep -qF "\"rule_id\":\"$1\"" "$OUT"; then pass "$2"; else fail "$2 (missing $1)"; fi; }
scan() {
  if TMPDIR="$TEST_TMP/runtime" "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT" --config "$CONFIG" "$@" > "$OUT" 2> "$ERR"; then
    LAST_RC=0
  else LAST_RC=$?; fi
}
reset_fixture() {
  cp "$SKILL_ROOT/config/context-value-claims.example.yaml" "$CONFIG"
  printf 'Role: question-draft\n' > "$PROJECT/interview.md"
  printf 'Status: READY\n' > "$PROJECT/task.md"
  printf 'Interview role: question-draft\nTask status: READY\n' > "$PROJECT/README.md"
}
replace_config() {
  sed "$1" "$CONFIG" > "$TEST_TMP/replaced.yaml"
  mv "$TEST_TMP/replaced.yaml" "$CONFIG"
}
manifest() {
  (cd "$1" && find . -type f -exec shasum -a 256 {} \; | LC_ALL=C sort)
}

reset_fixture
manifest "$PROJECT" > "$TEST_TMP/project.before"
manifest "$SKILL_ROOT" > "$TEST_TMP/skill.before"
scan --only context-truth
assert_rc 0 'question-draft and READY agree'
assert_rule context-truth-value-sync 'value claims execute'
manifest "$PROJECT" > "$TEST_TMP/project.after"
manifest "$SKILL_ROOT" > "$TEST_TMP/skill.after"
if cmp -s "$TEST_TMP/project.before" "$TEST_TMP/project.after" && cmp -s "$TEST_TMP/skill.before" "$TEST_TMP/skill.after" && [ -z "$(ls -A "$TEST_TMP/runtime")" ]; then
  pass 'read-only project/skill manifests and temp cleanup'
else fail 'read-only project/skill manifests and temp cleanup'; fi

printf 'Interview role: answers\nTask status: READY\n' > "$PROJECT/README.md"
scan --only context-truth
assert_rc 1 'question draft falsely mirrored as answers fails'
assert_rule context-truth-value-drift 'role mismatch is exact drift'
printf 'Interview role: question-draft\nTask status: DONE\n' > "$PROJECT/README.md"
scan --only context-truth
assert_rc 1 'READY falsely mirrored as DONE fails'
replace_config 's/severity: hard/severity: adaptive/g'
scan --only context-truth
assert_rc 2 'configured drift severity is adaptive'
reset_fixture
printf 'Status: DONE\n' > "$PROJECT/task.md"
printf 'Interview role: question-draft\nTask status: DONE\n' > "$PROJECT/README.md"
scan --only context-truth
assert_rc 0 'matching DONE is label consistency, not evidence validation'

for side in interview.md README.md; do
  reset_fixture
  : > "$PROJECT/$side"
  scan --only context-truth
  assert_rc 1 "$side missing declaration fails"
  assert_rule context-truth-claim-missing "$side missing declaration diagnostic"
  reset_fixture
  cp "$PROJECT/$side" "$TEST_TMP/duplicate"
  cat "$TEST_TMP/duplicate" >> "$PROJECT/$side"
  scan --only context-truth
  assert_rc 1 "$side duplicate declaration fails"
  assert_rule context-truth-claim-ambiguous "$side duplicate declaration diagnostic"
done

reset_fixture
printf 'Role: abandoned\n' > "$PROJECT/interview.md"
scan --only context-truth
assert_rc 1 'out-of-set source value fails'
assert_rule context-truth-value-invalid 'allowed values apply to source'
reset_fixture
printf 'Interview role: abandoned\nTask status: READY\n' > "$PROJECT/README.md"
scan --only context-truth
assert_rc 1 'out-of-set mirror value fails'
assert_rule context-truth-value-invalid 'allowed values apply to mirror'

for replacement in \
  '/allowed_values:/d' \
  's/allowed_values: .*/allowed_values: ""/' \
  's/mode: exact_value/mode: semantic_truth/' \
  's/id: task-status/id: interview-role/' \
  's/id: task-status/id: ""/' \
  's/source_pattern: .*/source_pattern: "^[($"/' \
  's/source_pattern: .*/source_pattern: "Role: ([a-z-]+)"/' \
  's/allowed_values:/allowd_values:/'; do
  reset_fixture
  replace_config "$replacement"
  scan --only context-truth
  assert_rc 1 "malformed config fails: $replacement"
  assert_rule context-truth-config-invalid 'configuration error is explicit'
done
for pattern in '^Role: [a-z-]+$' '^Role: (([a-z-]+))$' '^Role: ()[a-z-]+$'; do
  reset_fixture
  replace_config "s/source_pattern: .*/source_pattern: \"$pattern\"/"
  scan --only context-truth
  assert_rc 1 "extraction requires one nonempty capture: $pattern"
  assert_rule context-truth-claim-unparseable 'extraction failure diagnostic'
done

for pattern in '^Role: ([a-z-]+)|trailer$' '^Role: ([a-z-]+)\$'; do
  reset_fixture
  # sed replacement needs an escaped backslash to retain the literal-dollar ERE.
  if [[ "$pattern" = *'\$' ]]; then
    replace_config 's/source_pattern: .*/source_pattern: "^Role: ([a-z-]+)\\$"/'
    printf 'Role: question-draft$ ignored\n' > "$PROJECT/interview.md"
  else
    replace_config "s/source_pattern: .*/source_pattern: \"$pattern\"/"
    printf 'Role: question-draft ignored\n' > "$PROJECT/interview.md"
  fi
  scan --only context-truth
  assert_rc 1 "partial-line capture cannot pass: $pattern"
  assert_rule context-truth-claim-unparseable 'match must equal the entire selected line'
done

reset_fixture
replace_config 's/schema_version: 3/schema_version: 2/'
scan --only context-truth
assert_rc 1 'new capability cannot masquerade as schema 2'
assert_rule config-schema-capability 'version fence blocks silent old-reader ignore'
reset_fixture
replace_config '/  value_claims:/,$d'
scan --only context-truth
assert_rc 1 'enabled empty claims fail'
assert_rule context-truth-no-claims 'empty claims are NOT_VERIFIED'
replace_config 's/enabled: true/enabled: false/; /required_checkers:/d'
scan --only context-truth
assert_rc 1 'explicit disabled checker does not pass'
assert_rule checker-disabled 'disabled checker diagnostic'
scan
assert_rc 0 'full default scan may leave optional checker disabled'
assert_rule context-truth-disabled 'disabled full scan is soft NOT_VERIFIED'

reset_fixture
printf '\n  index_claims:\n    - id: items\n      source_file: items.md\n      item_pattern: "^P[0-9]+$"\n      id_pattern: "P[0-9]+"\n      mirror_file: README.md\n      claim_pattern: "P1-P[0-9]+"\n      mode: max_numeric_suffix\n' >> "$CONFIG"
printf 'P1\nP2\n' > "$PROJECT/items.md"
printf 'P1-P2\n' >> "$PROJECT/README.md"
scan --only context-truth
assert_rc 0 'mixed legacy index and value claims pass'
assert_rule context-truth-index-sync 'legacy index actually executed'
replace_config 's/id: items/id: interview-role/'
scan --only context-truth
assert_rc 1 'duplicate IDs across claim kinds fail'
reset_fixture
replace_config '/  value_claims:/,$d; s/schema_version: 3/schema_version: 2/'
printf '\n  index_claims:\n    - id: items\n      source_file: items.md\n      item_pattern: "^P[0-9]+$"\n      id_pattern: "P[0-9]+"\n      mirror_file: items.md\n      claim_pattern: "P2"\n' >> "$CONFIG"
scan --only context-truth
assert_rc 0 'unchanged schema 2 legacy index remains supported'

# All paths are resolved before content access, in both modes and both directions.
printf 'Role: question-draft\nP2\n' > "$TEST_TMP/outside.md"
ln -s "$TEST_TMP/outside.md" "$PROJECT/outside-link.md"
ln -s "$TEST_TMP" "$PROJECT/outside-dir"
ln -s interview.md "$PROJECT/inside-link.md"
ln -s loop-link.md "$PROJECT/loop-link.md"
for field in source_file mirror_file; do
  for path in ../outside.md "$TEST_TMP/outside.md" outside-link.md outside-dir/outside.md loop-link.md; do
    reset_fixture
    replace_config "s|$field: .*|$field: $path|"
    scan --only context-truth
    assert_rc 1 "$field path boundary fails: $path"
    assert_rule context-truth-path-unsafe 'unsafe path diagnostic'
  done
done
# Legacy claims share the same read boundary, not merely the value branch.
for field in source_file mirror_file; do
  for path in ../outside.md "$TEST_TMP/outside.md" outside-link.md outside-dir/outside.md loop-link.md; do
    reset_fixture
    replace_config '/  value_claims:/,$d; s/schema_version: 3/schema_version: 2/'
    printf '\n  index_claims:\n    - id: items\n      source_file: items.md\n      item_pattern: "^P[0-9]+$"\n      id_pattern: "P[0-9]+"\n      mirror_file: items.md\n      claim_pattern: "P2"\n' >> "$CONFIG"
    replace_config "s|$field: .*|$field: $path|"
    scan --only context-truth
    assert_rc 1 "index $field path boundary fails: $path"
    assert_rule context-truth-path-unsafe 'legacy unsafe path diagnostic'
  done
done
reset_fixture
replace_config 's/source_file: interview.md/source_file: inside-link.md/'
scan --only context-truth
assert_rc 0 'repo-internal symlink remains readable'
replace_config 's/source_file: inside-link.md/source_file: missing.md/'
scan --only context-truth
assert_rc 1 'missing file fails'
assert_rule context-truth-file-missing 'missing file diagnostic'

# Inject a genuine reader exit (not an invented finding) via the standard grep executable.
reset_fixture
mkdir "$TEST_TMP/bin"
REAL_GREP="$(command -v grep)"
printf '#!/usr/bin/env bash\nfor arg in "$@"; do\n  if [[ "$arg" = */interview.md ]]; then exit 2; fi\ndone\nexec "%s" "$@"\n' "$REAL_GREP" > "$TEST_TMP/bin/grep"
chmod +x "$TEST_TMP/bin/grep"
PATH="$TEST_TMP/bin:$PATH" scan --only context-truth
assert_rc 1 'reader failure cannot become a green comparison'
assert_rule context-truth-scan-error 'reader failure remains explicit'

# Optional real old-reader smoke; pass an unmodified v0.9 checkout, not a mocked implementation.
if [ -n "${DOC_CURATOR_LEGACY_ROOT:-}" ]; then
  if "$BASH_BIN" "$DOC_CURATOR_LEGACY_ROOT/scripts/scan.sh" --repo "$PROJECT" --config "$CONFIG" --only context-truth > "$OUT" 2> "$ERR"; then LAST_RC=0; else LAST_RC=$?; fi
  assert_rc 1 'actual v0.9 reader rejects schema 3'
  assert_rule config-schema-unsupported 'old reader rejects capability config explicitly'
fi
printf '\nTOTAL exact-value pass=%s fail=%s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
