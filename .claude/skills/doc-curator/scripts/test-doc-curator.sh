#!/usr/bin/env bash
# doc-curator 确定性回归。仅创建临时 fixture，不访问网络、不修改调用方仓库。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASH_BIN="${BASH:-bash}"
TEST_TMP="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP"; rm -f "$SKILL_ROOT/state/${PROJECT_A_ID:-}.state.json" "$SKILL_ROOT/state/${PROJECT_B_ID:-}.state.json" "$SKILL_ROOT/state/${SAME_BASE_A_ID:-}.state.json" "$SKILL_ROOT/state/${SAME_BASE_B_ID:-}.state.json"' EXIT

PASS=0
FAIL=0
LAST_RC=0
LAST_OUT="$TEST_TMP/out.jsonl"
LAST_ERR="$TEST_TMP/err.log"

pass() { printf 'PASS %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL %s\n' "$1" >&2; FAIL=$((FAIL + 1)); }

run_capture() {
  set +e
  "$@" > "$LAST_OUT" 2> "$LAST_ERR"
  LAST_RC=$?
  set -e
}

assert_rc() {
  local expected="$1" name="$2"
  if [ "$LAST_RC" -eq "$expected" ]; then pass "$name"; else
    fail "${name}（expected=$expected actual=${LAST_RC}）"
    sed -n '1,20p' "$LAST_OUT" >&2
    sed -n '1,20p' "$LAST_ERR" >&2
  fi
}

assert_rule() {
  local rule_id="$1" name="$2"
  if grep -q "\"rule_id\":\"$rule_id\"" "$LAST_OUT"; then pass "$name"; else
    fail "${name}（缺 rule_id=${rule_id}）"
    sed -n '1,20p' "$LAST_OUT" >&2
  fi
}

assert_not_rule() {
  local rule_id="$1" name="$2"
  if ! grep -q "\"rule_id\":\"$rule_id\"" "$LAST_OUT"; then pass "$name"; else
    fail "${name}（不应出现 rule_id=${rule_id}）"
    sed -n '1,20p' "$LAST_OUT" >&2
  fi
}

assert_err_contains() {
  local needle="$1" name="$2"
  if grep -qF "$needle" "$LAST_ERR"; then pass "$name"; else
    fail "${name}（stderr 未找到：${needle}）"
    sed -n '1,20p' "$LAST_ERR" >&2
  fi
}

assert_jsonl() {
  local name="$1" invalid=0 line
  [ -s "$LAST_OUT" ] || invalid=1
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "$line" | grep -Eq '^\{"checker":"([^"\\]|\\.)+","severity":"(ok|hard|adaptive|soft)","rule_id":"([^"\\]|\\.)+","message":"([^"\\]|\\.)*","suggestion":"([^"\\]|\\.)*"\}$' || invalid=1
  done < "$LAST_OUT"
  if [ "$invalid" -eq 0 ]; then pass "$name"; else fail "$name"; fi
}

make_fixture() {
  local root="$1"
  mkdir -p "$root/docs"
  printf '# 当前任务\n\n### ISS-1 示例任务\n' > "$root/docs/TASKS.md"
  printf '# 决策记录\n' > "$root/docs/DECISIONS.md"
  printf '# 变更记录\n\n## [0.1.0] - 2026-07-30\n' > "$root/CHANGELOG.md"
  printf '# 示例项目\n\n## Current Status\n\nReady. Inline example: `[text](path)`.\n' > "$root/README.md"
}

# 与 common.sh PROJECT_ID 一致：规范化绝对路径的 sha256。
project_id_of() {
  local norm
  norm="$(cd "$1" && pwd -P)"
  printf '%s' "$norm" | shasum -a 256 | awk '{print $1}'
}

PROJECT_A="$TEST_TMP/project-a"
PROJECT_B="$TEST_TMP/project-b"
make_fixture "$PROJECT_A"
make_fixture "$PROJECT_B"

run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --bogus
assert_rc 64 "未知参数返回 64"
assert_rule invalid-arguments "未知参数产生结构化 finding"
assert_jsonl "未知参数 stdout 为合法 JSONL"

run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --only tasks --since HEAD~1
assert_rc 64 "非 context checker 拒绝 Git 范围参数"
assert_rule invalid-scope "不适用范围参数产生结构化 finding"

run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A"
assert_rc 0 "默认只读扫描不阻断"
assert_jsonl "默认扫描 stdout 为合法 JSONL"

run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --init-baseline
assert_rc 0 "项目 A 基线初始化成功"
assert_rule baseline-initialized "基线初始化返回明确 rule"
PROJECT_A_ID="$(project_id_of "$PROJECT_A")"
if [ -f "$SKILL_ROOT/state/$PROJECT_A_ID.state.json" ] && grep -q '"project_id"' "$SKILL_ROOT/state/$PROJECT_A_ID.state.json"; then
  pass "基线写入 Skill 内状态目录（按 project_id 命名）"
else
  fail "基线写入 Skill 内状态目录（按 project_id 命名）"
fi

run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_B" --init-baseline
assert_rc 0 "项目 B 基线初始化成功"
PROJECT_B_ID="$(project_id_of "$PROJECT_B")"
project_a_id="$(sed -n 's/.*"project_id": "\([^"]*\)".*/\1/p' "$SKILL_ROOT/state/$PROJECT_A_ID.state.json")"
project_b_id="$(sed -n 's/.*"project_id": "\([^"]*\)".*/\1/p' "$SKILL_ROOT/state/$PROJECT_B_ID.state.json")"
if [ -n "$project_a_id" ] && [ -n "$project_b_id" ] && [ "$project_a_id" != "$project_b_id" ] && [ "$PROJECT_A_ID" != "$PROJECT_B_ID" ]; then
  pass "两个项目状态身份隔离（不同 project_id 文件）"
else
  fail "两个项目状态身份隔离（不同 project_id 文件）"
fi

# 同 basename、不同父目录的两个项目：project_id 必须不同，state 文件互不覆盖。
SAME_BASE_A="$TEST_TMP/samebase/dir-a/folia"
SAME_BASE_B="$TEST_TMP/samebase/dir-b/folia"
make_fixture "$SAME_BASE_A"
make_fixture "$SAME_BASE_B"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$SAME_BASE_A" --init-baseline
assert_rc 0 "同 basename 项目 A 基线初始化成功"
SAME_BASE_A_ID="$(project_id_of "$SAME_BASE_A")"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$SAME_BASE_B" --init-baseline
assert_rc 0 "同 basename 项目 B 基线初始化成功"
SAME_BASE_B_ID="$(project_id_of "$SAME_BASE_B")"
if [ -n "$SAME_BASE_A_ID" ] && [ "$SAME_BASE_A_ID" != "$SAME_BASE_B_ID" ] \
   && [ -f "$SKILL_ROOT/state/$SAME_BASE_A_ID.state.json" ] \
   && [ -f "$SKILL_ROOT/state/$SAME_BASE_B_ID.state.json" ]; then
  pass "同 basename 不同路径 project_id 隔离（state 文件互不覆盖）"
else
  fail "同 basename 不同路径 project_id 隔离（state 文件互不覆盖）"
fi

printf '\n### ISS-2 第二项\n' >> "$PROJECT_A/docs/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --only tasks
assert_rc 2 "超过 adaptive 基线返回 2"
assert_rule tasks-active-count "超过基线命中精确规则"
assert_jsonl "adaptive 输出为合法 JSONL"

BAD_CONFIG="$TEST_TMP/bad.yaml"
printf 'project: bad\n' > "$BAD_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$BAD_CONFIG"
assert_rc 1 "缺 files 配置失败闭合"
assert_rule config-invalid "非法配置产生结构化 finding"

TAB_CONFIG="$TEST_TMP/tab.yaml"
printf 'project: tab\nfiles:\n\t- path: docs/TASKS.md\n' > "$TAB_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$TAB_CONFIG"
assert_rc 1 "Tab YAML 失败闭合"
assert_rule config-invalid "Tab YAML 产生结构化 finding"

# v0.9.0 配置合同：schema、boolean、enabled、required checker 的每个 hard 分支均必须可达。
SCHEMA_MISSING_CONFIG="$TEST_TMP/schema-missing.yaml"
printf '%s\n' 'files:' '  - path: docs/TASKS.md' '    role: active-tasks' > "$SCHEMA_MISSING_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$SCHEMA_MISSING_CONFIG" --only config
assert_rc 1 "缺 schema_version 失败闭合"
assert_rule config-schema-missing "缺 schema_version 命中精确 rule"

SCHEMA_OLD_CONFIG="$TEST_TMP/schema-old.yaml"
printf '%s\n' 'schema_version: 1' 'files:' '  - path: docs/TASKS.md' '    role: active-tasks' > "$SCHEMA_OLD_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$SCHEMA_OLD_CONFIG" --only config
assert_rc 1 "旧 schema_version 失败闭合"
assert_rule config-schema-unsupported "旧 schema_version 命中精确 rule"

BOOLEAN_CONFIG="$TEST_TMP/boolean-invalid.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: docs/TASKS.md' '    role: active-tasks' \
  'context_truth:' '  enabled: yes' > "$BOOLEAN_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$BOOLEAN_CONFIG" --only config
assert_rc 1 "非 true/false boolean 失败闭合"
assert_rule config-boolean-invalid "非法 boolean 命中精确 rule"

AMBIGUOUS_CONFIG="$TEST_TMP/ambiguous-enable.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: docs/TASKS.md' '    role: active-tasks' \
  'context_truth:' '  index_claims:' '    - id: missing-enable' > "$AMBIGUOUS_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$AMBIGUOUS_CONFIG" --only config
assert_rc 1 "配业务字段但缺 enabled 失败闭合"
assert_rule config-ambiguous-enable "模糊 enabled 命中精确 rule"

REQUIRED_UNKNOWN_CONFIG="$TEST_TMP/required-unknown.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: docs/TASKS.md' '    role: active-tasks' \
  'config_contract:' '  required_checkers: unknown-checker' > "$REQUIRED_UNKNOWN_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$REQUIRED_UNKNOWN_CONFIG" --only config
assert_rc 1 "required checker 未知名称失败闭合"
assert_rule config-required-checker-unknown "未知 required checker 命中精确 rule"

REQUIRED_DISABLED_CONFIG="$TEST_TMP/required-disabled.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: docs/TASKS.md' '    role: active-tasks' \
  'config_contract:' '  required_checkers: context-truth' 'context_truth:' '  enabled: false' > "$REQUIRED_DISABLED_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$REQUIRED_DISABLED_CONFIG" --only config
assert_rc 1 "required checker 被关闭失败闭合"
assert_rule config-required-checker-disabled "关闭 required checker 命中精确 rule"

# flatten path 是配置语义身份；重复 schema/scalar/list field 都不得首值静默胜出。
for duplicate_case in schema enabled list-field; do
  duplicate_config="$TEST_TMP/duplicate-${duplicate_case}.yaml"
  case "$duplicate_case" in
    schema)
      printf '%s\n' 'schema_version: 2' 'schema_version: 1' 'files:' \
        '  - path: docs/TASKS.md' '    role: active-tasks' > "$duplicate_config" ;;
    enabled)
      printf '%s\n' 'schema_version: 2' 'files:' '  - path: docs/TASKS.md' '    role: active-tasks' \
        'context_truth:' '  enabled: false' '  enabled: true' > "$duplicate_config" ;;
    list-field)
      printf '%s\n' 'schema_version: 2' 'files:' '  - path: docs/TASKS.md' \
        '    path: docs/DECISIONS.md' '    role: active-tasks' > "$duplicate_config" ;;
  esac
  run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$duplicate_config" --only config
  assert_rc 1 "重复配置路径 ${duplicate_case} 失败闭合"
  assert_rule config-invalid "重复配置路径 ${duplicate_case} 转为结构化 hard"
  assert_err_contains "YAML 含重复配置路径" "重复配置路径 ${duplicate_case} 有可诊断 stderr"
done

# ── Bug 回归 ①：yaml-flatten 列表形态 ──
# 形态 1：list item 与父 key 同列（合法 YAML）。旧实现按 stack_spaces >= spaces 弹栈，
# 会把父级 section key 弹掉，item 路径逃逸为顶层（1.path=…），cfg_list_field 取空。
FLAT_SAME_INDENT="$TEST_TMP/same-indent.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '- path: docs/TASKS.md' '  role: active-tasks' \
  '- path: docs/DECISIONS.md' '  role: decision-log' > "$FLAT_SAME_INDENT"
run_capture awk -f "$SCRIPT_DIR/yaml-flatten.awk" "$FLAT_SAME_INDENT"
assert_rc 0 "同列列表项 flatten 成功"
if grep -q '^files\.1\.path=docs/TASKS\.md$' "$LAST_OUT" && \
   grep -q '^files\.1\.role=active-tasks$' "$LAST_OUT" && \
   grep -q '^files\.2\.path=docs/DECISIONS\.md$' "$LAST_OUT" && \
   grep -q '^files\.2\.role=decision-log$' "$LAST_OUT"; then
  pass "同列列表项归入父级路径（files.N.field）"
else
  fail "同列列表项归入父级路径（files.N.field）"
  sed -n '1,20p' "$LAST_OUT" >&2
fi
if grep -qE '^[0-9]+\.path=' "$LAST_OUT"; then
  fail "同列列表项不得逃逸为顶层路径"
else
  pass "同列列表项不得逃逸为顶层路径"
fi

# 形态 2：dash 后多空白（-  key: v）。旧实现 item_content 残留前导空格，
# 键正则失配，整段被当标量值输出（files.1=path: …），字段查找取空。
FLAT_DOUBLE_SPACE="$TEST_TMP/double-space.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  -  path: docs/TASKS.md' '    role: active-tasks' > "$FLAT_DOUBLE_SPACE"
run_capture awk -f "$SCRIPT_DIR/yaml-flatten.awk" "$FLAT_DOUBLE_SPACE"
assert_rc 0 "dash 后多空白 flatten 成功"
if grep -q '^files\.1\.path=docs/TASKS\.md$' "$LAST_OUT" && \
   grep -q '^files\.1\.role=active-tasks$' "$LAST_OUT"; then
  pass "dash 后多空白仍解析为 item 字段"
else
  fail "dash 后多空白仍解析为 item 字段"
  sed -n '1,20p' "$LAST_OUT" >&2
fi
if grep -q '^files\.1=' "$LAST_OUT"; then
  fail "dash 后多空白不得退化成整行标量值"
else
  pass "dash 后多空白不得退化成整行标量值"
fi

# 端到端：上述两种形态的配置此前会因缺 files[].path 被判 config-invalid。
SAME_INDENT_CONFIG="$TEST_TMP/same-indent-config.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '- path: docs/TASKS.md' '  role: active-tasks' > "$SAME_INDENT_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$SAME_INDENT_CONFIG" --only config
assert_rc 0 "同列列表项配置通过契约检查"

DOUBLE_SPACE_CONFIG="$TEST_TMP/double-space-config.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  -  path: docs/TASKS.md' '    role: active-tasks' > "$DOUBLE_SPACE_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$DOUBLE_SPACE_CONFIG" --only config
assert_rc 0 "dash 后多空白配置通过契约检查"

BAD_REGEX_CONFIG="$TEST_TMP/bad-regex.yaml"
printf '%s\n' \
  'schema_version: 2' \
  'project: bad-regex' \
  'files:' \
  '  - path: docs/TASKS.md' \
  '    role: active-tasks' \
  'adaptive_rules:' \
  '  - id: tasks-active-count' \
  '    seed_value: 1' \
  '    multiplier: 1.5' \
  '    active_count_pattern: "("' > "$BAD_REGEX_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --config "$BAD_REGEX_CONFIG" --only tasks
assert_rc 1 "非法 regex 失败闭合"
assert_rule config-regex-invalid "非法 regex 产生结构化 finding"

TASK_CONTRACT_CONFIG="$SKILL_ROOT/config/agentcmd-v4.example.yaml"
TASK_CONTRACT_PROJECT="$TEST_TMP/task-contract"
mkdir -p "$TASK_CONTRACT_PROJECT"
printf '# Decisions\n' > "$TASK_CONTRACT_PROJECT/DECISIONS.md"
printf '# Changelog\n' > "$TASK_CONTRACT_PROJECT/CHANGELOG.md"
printf '%s\n' '---' 'name: fixture' 'description: fixture' '---' > "$TASK_CONTRACT_PROJECT/SKILL.md"

printf '%s\n' \
  '# Tasks' \
  '' \
  '## 当前队列' \
  '' \
  '| Task | 状态 | 主结果 |' \
  '|---|---|---|' \
  '| `Task-001` | `READY` | 完成任务源合同回归 |' \
  '' \
  '## 当前任务卡' \
  '' \
  '### Task-001 — 合规任务卡' \
  '' \
  '- **状态**：`READY`' \
  '- **缘由与证据**：现有能力需要确定性回归。' \
  '- **主目标**：输出一份可复查结果。' \
  '- **非目标**：不修改目标目录。' \
  '- **输入与依赖**：输入 TASKS.md；依赖 Bash 4+。' \
  '- **允许范围**：只读任务文件。' \
  '- **禁止范围**：不写入 Git。' \
  '- **停止条件**：输入缺失时停止。' \
  '- **验收标准**：命令退出 0 且产生明确 rule。' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 0 "AgentCMD 合规 READY 任务卡通过"
assert_rule task-source-contract "AgentCMD 合规任务卡返回合同通过 rule"

printf '%s\n' \
  '# Tasks' \
  '' \
  '| Task | 状态 | 主结果 |' \
  '|---|---|---|' \
  '| `Task-001` | `DRAFT` | 尚未补齐的方向 |' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 0 "AgentCMD DRAFT 不完整不阻断"
assert_rule task-source-contract "AgentCMD DRAFT 仍返回合同检查结果"

printf '%s\n' \
  '# Tasks' \
  '' \
  '| Task | 状态 |' \
  '|---|---|' \
  '| `Task-001` | `READY` |' \
  '' \
  '### Task-001 — 缺字段' \
  '- **状态**：`READY`' \
  '- **主目标**：只有目标。' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 1 "AgentCMD READY 缺合同字段返回 1"
assert_rule task-source-card-incomplete "AgentCMD READY 缺字段被检出"

printf '%s\n' \
  '# Tasks' \
  '' \
  '| Task | 状态 |' \
  '|---|---|' \
  '| `Task-001` | `BLOCKED` |' \
  '' \
  '### Task-001 — 无原因阻塞' \
  '- **状态**：`BLOCKED`' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 1 "AgentCMD BLOCKED 缺原因返回 1"
assert_rule task-source-card-incomplete "AgentCMD BLOCKED 缺原因被检出"

printf '%s\n' \
  '# Tasks' \
  '' \
  '| Task | 状态 |' \
  '|---|---|' \
  '| `Task-001` | `DONE` |' \
  '' \
  '### Task-001 — 无证据完成' \
  '- **状态**：`DONE`' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 1 "AgentCMD DONE 缺证据返回 1"
assert_rule task-source-card-incomplete "AgentCMD DONE 缺证据被检出"

# 完成历史可以持续积累，不得再按 H3 总数冒充活跃任务触发 adaptive。
{
  printf '%s\n' '# Tasks' '' '| Task | 状态 |' '|---|---|'
  for task_id in Task-001 Task-002 Task-003 Task-004 Task-005; do
    printf '| `%s` | `DONE` |\n' "$task_id"
  done
  for task_id in Task-001 Task-002 Task-003 Task-004 Task-005; do
    printf '\n### %s — 已完成历史\n' "$task_id"
    printf '%s\n' '- **状态**：`DONE`' '- **验收证据**：历史任务已验证。'
  done
} > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 0 "AgentCMD 多条 DONE 历史不触发活跃任务告警"
assert_rule task-source-contract "AgentCMD DONE 历史仍完成合同审计"
if ! grep -q '"rule_id":"tasks-active-count"' "$LAST_OUT"; then
  pass "AgentCMD 启用时跳过旧 H3 活跃计数"
else
  fail "AgentCMD 启用时跳过旧 H3 活跃计数"
fi

printf '# Tasks\n\n当前无任务。\n' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 0 "AgentCMD 明确空队列通过"
assert_rule task-source-empty-queue "AgentCMD 空队列返回明确 rule"

printf '%s\n' \
  '# Tasks' \
  '' \
  '| Task | 状态 |' \
  '|---|---|' \
  '| `Task-001` | `READY` |' \
  '' \
  '### Task-001 — 状态冲突' \
  '- **状态**：`BLOCKED`' \
  '- **阻塞原因**：等待输入。' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 1 "AgentCMD 队列与任务卡状态冲突返回 1"
assert_rule task-source-status-conflict "AgentCMD 状态冲突被检出"

# ── Bug 回归 ③：合法状态行的演进叙述不得伪造 status conflict ──
# 旧实现 status_from_text 按固定优先级（IN_PROGRESS 最先）扫整行取词，
# “- **状态**：DONE（IN_PROGRESS 期间…）”这类合法声明会被叙述里的旧状态覆盖。
printf '%s\n' \
  '# Tasks' '' \
  '| Task | 状态 |' '|---|---|' \
  '| `Task-030` | `DONE` |' '' \
  '### Task-030 — 状态叙述行' \
  '- **状态**：`DONE`（IN_PROGRESS 期间发现的问题已全部清零）' \
  '- **验收证据**：回归全部通过。' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 0 "状态行叙述中的旧状态词不再触发冲突"
assert_not_rule task-source-status-conflict "DONE 声明携带 IN_PROGRESS 叙述不误报"
assert_rule task-source-contract "叙述行任务卡仍通过合同审计"

printf '%s\n' \
  '# Tasks' '' \
  '| Task | 状态 |' '|---|---|' \
  '| `Task-031` | `REVIEW` |' '' \
  '### Task-031 — REVIEW 附带演进叙述' \
  '- **状态**：`REVIEW`（曾以 IN_PROGRESS 推进两轮）' \
  '- **交付物**：评审记录。' \
  '- **验收证据**：双口径回放通过。' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 0 "REVIEW 声明附带旧状态叙述不阻断"
assert_not_rule task-source-status-conflict "REVIEW 声明不被 IN_PROGRESS 叙述覆盖"
assert_rule task-source-contract "REVIEW 叙述行仍通过合同审计"

# 摘要行（非状态标签）：只有状态词紧随任务 ID 才算显式声明；
# “由 READY 推进到 DONE”这类演进叙述不再被当成状态来源。
printf '%s\n' \
  '# Tasks' '' \
  '| Task | 状态 |' '|---|---|' \
  '| `Task-032` | `DONE` |' '' \
  '### Task-032 — 队列摘要演进叙述' \
  '- **状态**：`DONE`' \
  '- **验收证据**：摘要叙述不改变状态判定。' \
  '' \
  '- 进度备注：Task-032 由 READY 推进到 DONE。' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 0 "演进叙述摘要行不伪造状态"
assert_not_rule task-source-status-conflict "演进叙述不与队列声明冲突"

# 摘要行紧随 ID 的状态词仍是合法声明（DRAFT 无需合同字段，判漏报防护）。
printf '%s\n' \
  '# Tasks' '' \
  '| Task | 状态 |' '|---|---|' \
  '| `Task-033` | `DRAFT` |' '' \
  '- Task-033 `DRAFT` 草案：方向未定，暂不建卡。' > "$TASK_CONTRACT_PROJECT/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_PROJECT" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 0 "紧随 ID 的摘要状态词仍被识别"
assert_not_rule task-source-status-missing "摘要声明使任务源不判状态缺失"

TASK_CONTRACT_MISSING="$TEST_TMP/task-contract-missing"
mkdir -p "$TASK_CONTRACT_MISSING"
printf '# Decisions\n' > "$TASK_CONTRACT_MISSING/DECISIONS.md"
printf '# Changelog\n' > "$TASK_CONTRACT_MISSING/CHANGELOG.md"
printf '%s\n' '---' 'name: fixture' 'description: fixture' '---' > "$TASK_CONTRACT_MISSING/SKILL.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TASK_CONTRACT_MISSING" --config "$TASK_CONTRACT_CONFIG" --only tasks
assert_rc 1 "AgentCMD 启用时缺 TASKS 返回 1"
assert_rule task-source-file-missing "AgentCMD 缺任务源被检出"

printf '# Links\n\n[missing](missing.md)\n' > "$PROJECT_B/docs/links.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_B" --only markdown-link-broken
assert_rc 1 "真实相对断链返回 1"
assert_rule markdown-link-broken "真实相对断链被检出"

# v0.9.0：URL decode 后存在的链接不得误报；source severity 与 pseudo target 分流。
LINK_PROJECT="$TEST_TMP/link-policy"
mkdir -p "$LINK_PROJECT/docs" "$LINK_PROJECT/research"
cp "$SKILL_ROOT/tests/fixtures/markdown/docs/source.md" "$LINK_PROJECT/docs/source.md"
cp "$SKILL_ROOT/tests/fixtures/markdown/docs/target file.md" "$LINK_PROJECT/docs/target file.md"
printf '# Tasks\n' > "$LINK_PROJECT/docs/TASKS.md"
printf '%s\n' \
  'schema_version: 2' \
  'project: link-policy' \
  'files:' \
  '  - path: docs/TASKS.md' \
  '    role: active-tasks' \
  'markdown_link_broken:' \
  '  enabled: true' \
  '  check_code_paths: false' \
  '  default_severity: hard' \
  '  source_severity_paths:' \
  '    - path: "docs/**"' \
  '      severity: adaptive' \
  '  pseudo_target_patterns:' \
  '    - path: "M[0-9]-[0-9]*"' \
  '      severity: soft' > "$LINK_PROJECT/doc-curator.yaml"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$LINK_PROJECT" --only markdown-link-broken
assert_rc 2 "Markdown source severity 产生 adaptive 而非 hard"
assert_rule markdown-link-pseudo-target "伪链接按策略分流"
if ! grep -q 'target%20file.md' "$LAST_OUT"; then pass "URL encoded 已存在链接不误报"; else fail "URL encoded 已存在链接不误报"; fi
if grep -q '"severity":"adaptive".*research/missing.md' "$LAST_OUT"; then pass "source severity 按源文件匹配"; else fail "source severity 按源文件匹配"; fi

printf '\n[unsafe](bad%%0Aname.md)\n[outside](../../outside.md)\n' >> "$LINK_PROJECT/docs/source.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$LINK_PROJECT" --only markdown-link-broken
assert_rc 1 "URL 控制字符编码与字面逃逸路径失败闭合"
assert_rule markdown-link-unsafe-encoding "URL 控制字符编码被阻断"
assert_rule markdown-link-outside-repo "字面逃出仓库的路径被阻断"

# 物理路径边界：允许仓内 symlink，禁止仓内链接名穿透到仓外。
SYMLINK_PROJECT="$TEST_TMP/link-symlink"
mkdir -p "$SYMLINK_PROJECT/docs" "$SYMLINK_PROJECT/targets"
printf '# Tasks\n' > "$SYMLINK_PROJECT/docs/TASKS.md"
printf '# inside\n' > "$SYMLINK_PROJECT/targets/inside.md"
ln -s targets/inside.md "$SYMLINK_PROJECT/inside-link.md"
printf '# Links\n\n[inside](inside-link.md)\n' > "$SYMLINK_PROJECT/README.md"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: docs/TASKS.md' '    role: active-tasks' \
  'markdown_link_broken:' '  enabled: true' '  default_severity: hard' > "$SYMLINK_PROJECT/doc-curator.yaml"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$SYMLINK_PROJECT" --only markdown-link-broken
assert_rc 0 "指向仓内已存在文件的 symlink 通过"
assert_not_rule markdown-link-outside-repo "仓内 symlink 不被误报为逃逸"

ln -s /etc/passwd "$SYMLINK_PROJECT/outside-link"
printf '\n[outside](outside-link)\n' >> "$SYMLINK_PROJECT/README.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$SYMLINK_PROJECT" --only markdown-link-broken
assert_rc 1 "指向仓外已存在文件的 symlink 失败闭合"
assert_rule markdown-link-outside-repo "symlink 物理路径逃出仓库被阻断"

# ── Bug 回归 ④：非源码目录不得进入 Markdown 扫描 ──
# 旧实现排除表只命中嵌套 */node_modules/*：仓库顶层 node_modules/ 与
# tmp/、dist/、build/（含任意层级）全部漏排，依赖目录里的 README 断链
# 会被当成项目问题上报；且未配置 exclude_paths 时无任何默认防线。
NONSOURCE_PROJECT="$TEST_TMP/link-nonsource"
mkdir -p "$NONSOURCE_PROJECT/docs" "$NONSOURCE_PROJECT/node_modules/pkg" \
  "$NONSOURCE_PROJECT/tmp" "$NONSOURCE_PROJECT/dist" "$NONSOURCE_PROJECT/build" \
  "$NONSOURCE_PROJECT/docs/tmp" "$NONSOURCE_PROJECT/docs/dist"
printf '# Tasks\n' > "$NONSOURCE_PROJECT/docs/TASKS.md"
printf '# dep\n\n[dep](./missing.md)\n' > "$NONSOURCE_PROJECT/node_modules/pkg/README.md"
printf '# tmp\n\n[tmp](gone.md)\n' > "$NONSOURCE_PROJECT/tmp/notes.md"
printf '# dist\n\n[dist](gone.md)\n' > "$NONSOURCE_PROJECT/dist/x.md"
printf '# build\n\n[build](gone.md)\n' > "$NONSOURCE_PROJECT/build/y.md"
printf '# nested tmp\n\n[nt](gone.md)\n' > "$NONSOURCE_PROJECT/docs/tmp/nested.md"
printf '# nested dist\n\n[nd](gone.md)\n' > "$NONSOURCE_PROJECT/docs/dist/nested.md"
printf '# real\n\n[gone](gone.md)\n' > "$NONSOURCE_PROJECT/docs/real.md"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: docs/TASKS.md' '    role: active-tasks' \
  'markdown_link_broken:' '  enabled: true' '  default_severity: hard' > "$NONSOURCE_PROJECT/doc-curator.yaml"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$NONSOURCE_PROJECT" --only markdown-link-broken
assert_rc 1 "排除非源码目录后仍检出真实断链"
assert_rule markdown-link-broken "真实源码断链仍被上报"
if grep -qE 'node_modules/|(^|[/"])tmp/|(^|[/"])dist/|(^|[/"])build/' "$LAST_OUT"; then
  fail "node_modules/tmp/dist/build 下的 Markdown 不得进入扫描结果"
  sed -n '1,20p' "$LAST_OUT" >&2
else
  pass "node_modules/tmp/dist/build 下的 Markdown 不得进入扫描结果"
fi

# v0.9.0：只从决策标题建 ID 集合，正文引用不能掩盖重复与跳号。
DECISION_PROJECT="$TEST_TMP/decision-identity"
mkdir -p "$DECISION_PROJECT/docs"
cp "$SKILL_ROOT/tests/fixtures/decisions/DECISIONS.md" "$DECISION_PROJECT/docs/DECISIONS.md"
printf '%s\n' \
  'schema_version: 2' \
  'project: decision-identity' \
  'files:' \
  '  - path: docs/DECISIONS.md' \
  '    role: decision-log' \
  'hard_rules:' \
  '  - id: decisions-id-unique' \
  '    file: docs/DECISIONS.md' \
  '  - id: decisions-dec-numbering-continuous' \
  '    file: docs/DECISIONS.md' \
  'decision_log:' \
  '  heading_pattern: "^#{2,6}[[:space:]]+\[?DEC-[0-9]+\]?"' \
  '  id_pattern: "DEC-[0-9]+"' > "$DECISION_PROJECT/doc-curator.yaml"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$DECISION_PROJECT" --only decisions
assert_rc 1 "决策标题重复/跳号失败闭合"
assert_rule decisions-id-duplicate "重复决策标题被检出"
assert_rule decisions-dec-numbering-continuous "正文 DEC 引用不掩盖标题跳号"

# v0.9.0：声明式 context truth 同时给出同步与漂移证据。
TRUTH_PROJECT="$TEST_TMP/context-truth"
mkdir -p "$TRUTH_PROJECT/retrospective"
cp "$SKILL_ROOT/tests/fixtures/context-truth/PROBLEMS.md" "$TRUTH_PROJECT/retrospective/PROBLEMS.md"
cp "$SKILL_ROOT/tests/fixtures/context-truth/LESSONS.md" "$TRUTH_PROJECT/retrospective/LESSONS.md"
cp "$SKILL_ROOT/tests/fixtures/context-truth/README.md" "$TRUTH_PROJECT/retrospective/README.md"
printf '%s\n' \
  'schema_version: 2' \
  'project: context-truth' \
  'files:' \
  '  - path: retrospective/README.md' \
  '    role: readme' \
  'config_contract:' \
  '  required_checkers: "context-truth"' \
  'context_truth:' \
  '  enabled: true' \
  '  index_claims:' \
  '    - id: problems' \
  '      source_file: retrospective/PROBLEMS.md' \
  '      item_pattern: "^[|][[:space:]]*P[0-9]+"' \
  '      id_pattern: "P([0-9]+)"' \
  '      mirror_file: retrospective/README.md' \
  '      claim_pattern: "P1-P([0-9]+)"' \
  '      mode: max_numeric_suffix' \
  '      severity: hard' \
  '    - id: lessons' \
  '      source_file: retrospective/LESSONS.md' \
  '      item_pattern: "^[|][[:space:]]*L[0-9]+"' \
  '      id_pattern: "L([0-9]+)"' \
  '      mirror_file: retrospective/README.md' \
  '      claim_pattern: "L1-L([0-9]+)"' \
  '      mode: max_numeric_suffix' \
  '      severity: hard' > "$TRUTH_PROJECT/doc-curator.yaml"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TRUTH_PROJECT" --only context-truth
assert_rc 1 "context truth 漂移失败闭合"
assert_rule context-truth-index-drift "P 索引漂移被检出"
assert_rule context-truth-index-sync "L 索引一致被确认"

TRUTH_NO_CLAIMS_CONFIG="$TEST_TMP/context-truth-no-claims.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: retrospective/README.md' '    role: readme' \
  'config_contract:' '  required_checkers: context-truth' 'context_truth:' '  enabled: true' > "$TRUTH_NO_CLAIMS_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TRUTH_PROJECT" --config "$TRUTH_NO_CLAIMS_CONFIG" --only context-truth
assert_rc 1 "required/enabled context-truth 零 claims 失败闭合"
assert_rule context-truth-no-claims "零 claims 不再用 soft 假绿"

TRUTH_INVALID_CONFIG="$TEST_TMP/context-truth-invalid.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: retrospective/README.md' '    role: readme' \
  'context_truth:' '  enabled: true' '  index_claims:' '    - id: invalid' \
  '      source_file: retrospective/PROBLEMS.md' '      item_pattern: "^P"' '      id_pattern: "P[0-9]+"' \
  '      claim_pattern: "P1-P[0-9]+"' > "$TRUTH_INVALID_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TRUTH_PROJECT" --config "$TRUTH_INVALID_CONFIG" --only context-truth
assert_rc 1 "context-truth claim 缺必填字段失败闭合"
assert_rule context-truth-config-invalid "context-truth 非法 claim 命中精确 rule"

TRUTH_FILE_MISSING_CONFIG="$TEST_TMP/context-truth-file-missing.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: retrospective/README.md' '    role: readme' \
  'context_truth:' '  enabled: true' '  index_claims:' '    - id: missing-file' \
  '      source_file: retrospective/DOES-NOT-EXIST.md' '      item_pattern: "^P"' '      id_pattern: "P[0-9]+"' \
  '      mirror_file: retrospective/README.md' '      claim_pattern: "P1-P[0-9]+"' > "$TRUTH_FILE_MISSING_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TRUTH_PROJECT" --config "$TRUTH_FILE_MISSING_CONFIG" --only context-truth
assert_rc 1 "context-truth source/mirror 缺文件失败闭合"
assert_rule context-truth-file-missing "context-truth 缺文件命中精确 rule"

TRUTH_CLAIM_MISSING_CONFIG="$TEST_TMP/context-truth-claim-missing.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: retrospective/README.md' '    role: readme' \
  'context_truth:' '  enabled: true' '  index_claims:' '    - id: missing-claim' \
  '      source_file: retrospective/PROBLEMS.md' '      item_pattern: "^NEVER-MATCH"' '      id_pattern: "P[0-9]+"' \
  '      mirror_file: retrospective/README.md' '      claim_pattern: "P1-P[0-9]+"' > "$TRUTH_CLAIM_MISSING_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TRUTH_PROJECT" --config "$TRUTH_CLAIM_MISSING_CONFIG" --only context-truth
assert_rc 1 "context-truth 索引声明缺失闭合"
assert_rule context-truth-claim-missing "context-truth 声明缺失命中精确 rule"

printf 'Palpha\n' > "$TRUTH_PROJECT/retrospective/UNPARSE-SOURCE.md"
printf 'Pomega\n' > "$TRUTH_PROJECT/retrospective/UNPARSE-MIRROR.md"
TRUTH_UNPARSEABLE_CONFIG="$TEST_TMP/context-truth-unparseable.yaml"
printf '%s\n' 'schema_version: 2' 'files:' '  - path: retrospective/README.md' '    role: readme' \
  'context_truth:' '  enabled: true' '  index_claims:' '    - id: unparseable' \
  '      source_file: retrospective/UNPARSE-SOURCE.md' '      item_pattern: "^P"' '      id_pattern: "P[A-Za-z]+"' \
  '      mirror_file: retrospective/UNPARSE-MIRROR.md' '      claim_pattern: "P[A-Za-z]+"' > "$TRUTH_UNPARSEABLE_CONFIG"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TRUTH_PROJECT" --config "$TRUTH_UNPARSEABLE_CONFIG" --only context-truth
assert_rc 1 "context-truth 数字后缀无法解析时失败闭合"
assert_rule context-truth-claim-unparseable "context-truth 不可解析声明命中精确 rule"

printf '\n历史陈旧范围：L1-L11\n' >> "$TRUTH_PROJECT/retrospective/README.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$TRUTH_PROJECT" --only context-truth
assert_rc 1 "context-truth mirror 同时出现新旧范围失败闭合"
assert_rule context-truth-claim-ambiguous "mirror 多重范围不再由最大值掩盖"

# 显式请求被关闭 checker 不能再返回假绿。
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --only context-sync
assert_rc 1 "显式请求 disabled checker 失败闭合"
assert_rule checker-disabled "disabled checker 返回明确 hard"

PROJECT_GIT="$TEST_TMP/project-git"
make_fixture "$PROJECT_GIT"
printf '\nDecision DEC-001 remains referenced.\n' >> "$PROJECT_GIT/README.md"
GIT_CONFIG="$PROJECT_GIT/doc-curator.yaml"
printf '%s\n' \
  'schema_version: 2' \
  'project: git-fixture' \
  'files:' \
  '  - path: docs/TASKS.md' \
  '    role: active-tasks' \
  '  - path: docs/DECISIONS.md' \
  '    role: decision-log' \
  '  - path: CHANGELOG.md' \
  '    role: changelog' \
  '  - path: README.md' \
  '    role: readme' \
  'adaptive_rules:' \
  '  - id: tasks-active-count' \
  '    seed_value: 3' \
  '    multiplier: 1.5' \
  '    active_count_pattern: "^### ISS-[0-9]+"' \
  'context_sync:' \
  '  enabled: true' \
  '  local_reversible:' \
  '    max_files: 1' \
  '    max_insertions: 50' \
  '  systemic:' \
  '    min_files: 3' \
  '    min_insertions: 200' \
  '  change_types:' \
  '    - pattern: "**"' \
  '      expect_changelog: false' \
  '      expect_tasks: false' \
  '      expect_decision: systemic' \
  '      expect_figures: false' \
  '      expect_skill_internal: false' \
  'dec_ref_sync:' \
  '  enabled: true' \
  '  scan_extensions: md' \
  '  exclude_paths: .git DECISIONS.md node_modules' \
  'markdown_link_broken:' \
  '  enabled: false' \
  'active_zone_residue:' \
  '  enabled: false' > "$GIT_CONFIG"
git -C "$PROJECT_GIT" init -q
git -C "$PROJECT_GIT" config user.name doc-curator-test
git -C "$PROJECT_GIT" config user.email doc-curator-test@example.invalid
git -C "$PROJECT_GIT" add .
git -C "$PROJECT_GIT" commit -qm initial

printf '\nsmall change\n' >> "$PROJECT_GIT/README.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_GIT" --only context-sync --working-tree
assert_rc 0 "context-sync 局部可逆分支返回 0"
assert_rule context-sync-local-reversible "context-sync 局部可逆规则可达"
git -C "$PROJECT_GIT" restore README.md

printf '\nchange\n' >> "$PROJECT_GIT/README.md"
printf '\nchange\n' >> "$PROJECT_GIT/CHANGELOG.md"
printf '\nchange\n' >> "$PROJECT_GIT/docs/TASKS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_GIT" --only context-sync --working-tree
assert_rc 1 "context-sync 体系性缺决策返回 1"
assert_rule context-sync-decision "context-sync 体系性规则可达"
git -C "$PROJECT_GIT" restore README.md CHANGELOG.md docs/TASKS.md

run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_GIT" --only decision-sync
assert_rc 0 "decision-sync 无 marker 返回 0"
assert_rule dec-ref-sync-no-markers "decision-sync 无 marker 规则可达"

printf '# 决策记录\n\n## DEC-002\n- 日期: 2020-01-01\n- change: supersede DEC-001\n' > "$PROJECT_GIT/docs/DECISIONS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_GIT" --only decision-sync
assert_rc 0 "decision-sync 已更新引用返回 0"
assert_rule dec-ref-sync-synced "decision-sync synced 分支可达"

printf '# 决策记录\n\n## DEC-002\n- 日期: 2099-01-01\n- change: supersede DEC-001\n' > "$PROJECT_GIT/docs/DECISIONS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_GIT" --only decision-sync
assert_rc 1 "decision-sync stale 引用返回 1"
assert_rule dec-ref-sync-stale "decision-sync stale 分支可达"

# ── Bug 回归 ②：POSIX awk 区间量词 {N,M} 静默失配 ──
# 旧 BSD awk（macOS 自带）不支持 {4}/{2,3} 区间量词且不报错；D-YYYY-MM-DD-NN
# 形态的 heading 与 supersede marker 两处正则会同时静默失配 → no-markers 假绿。
printf '\nDecision D-2025-01-15-07 remains referenced.\n' >> "$PROJECT_GIT/README.md"
printf '# 决策记录\n\n## D-2026-07-03-01\n- 日期: 2099-01-01\n- change: supersede D-2025-01-15-07\n' > "$PROJECT_GIT/docs/DECISIONS.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_GIT" --only decision-sync
assert_rc 1 "decision-sync D 形态 stale 引用返回 1"
assert_rule dec-ref-sync-stale "decision-sync D 形态 supersede 被检出"
if grep -qE '\{[0-9]+(,[0-9]+)?\}' "$SCRIPT_DIR/check-dec-ref-sync.sh" "$SCRIPT_DIR/yaml-flatten.awk"; then
  fail "awk 程序禁用 {N,M} 区间量词（旧 BSD awk 静默失配）"
else
  pass "awk 程序禁用 {N,M} 区间量词（旧 BSD awk 静默失配）"
fi

FAULT_SKILL="$TEST_TMP/fault-skill"
mkdir -p "$FAULT_SKILL/config"
cp -R "$SKILL_ROOT/scripts" "$FAULT_SKILL/scripts"
cp "$SKILL_ROOT/config/default.yaml" "$FAULT_SKILL/config/default.yaml"

printf '#!/usr/bin/env bash\nexit 9\n' > "$FAULT_SKILL/scripts/check-files.sh"
chmod +x "$FAULT_SKILL/scripts/check-files.sh"
run_capture "$BASH_BIN" "$FAULT_SKILL/scripts/scan.sh" --repo "$PROJECT_B" --only files
assert_rc 1 "checker crash 被阻断"
assert_rule checker-execution-error "checker crash 有结构化错误"

printf '#!/usr/bin/env bash\nexit 0\n' > "$FAULT_SKILL/scripts/check-files.sh"
run_capture "$BASH_BIN" "$FAULT_SKILL/scripts/scan.sh" --repo "$PROJECT_B" --only files
assert_rc 1 "checker 空输出被阻断"
assert_rule checker-empty-output "checker 空输出有结构化错误"

printf '#!/usr/bin/env bash\nprintf "not-json\\n"\n' > "$FAULT_SKILL/scripts/check-files.sh"
run_capture "$BASH_BIN" "$FAULT_SKILL/scripts/scan.sh" --repo "$PROJECT_B" --only files
assert_rc 1 "checker 非法输出被阻断"
assert_rule checker-invalid-output "checker 非法输出有结构化错误"

printf '#!/usr/bin/env bash\nprintf '\''{"checker":"files","severity":"hard","rule_id":"fault-hard","message":"fault","suggestion":""}\\n'\''\nexit 0\n' > "$FAULT_SKILL/scripts/check-files.sh"
run_capture "$BASH_BIN" "$FAULT_SKILL/scripts/scan.sh" --repo "$PROJECT_B" --only files
assert_rc 1 "checker hard finding 配 rc0 被阻断"
assert_rule checker-exit-mismatch "checker rc0/hard 严重度错配可见"

printf '#!/usr/bin/env bash\nprintf '\''{"checker":"tasks","severity":"ok","rule_id":"wrong-id","message":"fault","suggestion":""}\\n'\''\nexit 0\n' > "$FAULT_SKILL/scripts/check-files.sh"
run_capture "$BASH_BIN" "$FAULT_SKILL/scripts/scan.sh" --repo "$PROJECT_B" --only files
assert_rc 1 "checker 身份错配被阻断"
assert_rule checker-identity-mismatch "checker 身份错配有结构化 hard"

printf '#!/usr/bin/env bash\nprintf '\''{"checker":"files","severity":"adaptive","rule_id":"fault-adaptive","message":"fault","suggestion":""}\\n'\''\nexit 0\n' > "$FAULT_SKILL/scripts/check-files.sh"
run_capture "$BASH_BIN" "$FAULT_SKILL/scripts/scan.sh" --repo "$PROJECT_B" --only files
assert_rc 1 "checker adaptive finding 配 rc0 被阻断"
assert_rule checker-exit-mismatch "checker rc0/adaptive 严重度错配可见"

printf '#!/usr/bin/env bash\nprintf '\''{"checker":"files","severity":"soft","rule_id":"fault-soft","message":"fault","suggestion":""}\\n'\''\nexit 2\n' > "$FAULT_SKILL/scripts/check-files.sh"
run_capture "$BASH_BIN" "$FAULT_SKILL/scripts/scan.sh" --repo "$PROJECT_B" --only files
assert_rc 1 "checker soft finding 配 rc2 被阻断"
assert_rule checker-exit-mismatch "checker rc2/soft 严重度错配可见"

printf '#!/usr/bin/env bash\nprintf '\''{"checker":"files","severity":"hard","rule_id":"fault-hard","message":"fault","suggestion":""}\\n'\''\nexit 2\n' > "$FAULT_SKILL/scripts/check-files.sh"
run_capture "$BASH_BIN" "$FAULT_SKILL/scripts/scan.sh" --repo "$PROJECT_B" --only files
assert_rc 1 "checker hard finding 配 rc2 被阻断"
assert_rule checker-exit-mismatch "checker rc2/hard 严重度错配可见"

REPORT_FAULT_SKILL="$TEST_TMP/report-fault-skill"
mkdir -p "$REPORT_FAULT_SKILL/config"
cp -R "$SKILL_ROOT/scripts" "$REPORT_FAULT_SKILL/scripts"
cp "$SKILL_ROOT/config/default.yaml" "$REPORT_FAULT_SKILL/config/default.yaml"

printf '#!/usr/bin/env bash\nexit 9\n' > "$REPORT_FAULT_SKILL/scripts/render-report.sh"
chmod +x "$REPORT_FAULT_SKILL/scripts/render-report.sh"
run_capture "$BASH_BIN" "$REPORT_FAULT_SKILL/scripts/scan.sh" --repo "$SAME_BASE_A" --report "$TEST_TMP/render-crash.md"
assert_rc 1 "report renderer 崩溃失败闭合"
assert_rule report-generation-error "report renderer 崩溃转为结构化 hard"

printf '#!/usr/bin/env bash\nexit 0\n' > "$REPORT_FAULT_SKILL/scripts/render-report.sh"
chmod +x "$REPORT_FAULT_SKILL/scripts/render-report.sh"
run_capture "$BASH_BIN" "$REPORT_FAULT_SKILL/scripts/scan.sh" --repo "$SAME_BASE_A" --report "$TEST_TMP/render-empty.md"
assert_rc 1 "report renderer 空产物失败闭合"
assert_rule report-generation-error "report renderer 空产物转为结构化 hard"

cp "$SKILL_ROOT/scripts/render-report.sh" "$REPORT_FAULT_SKILL/scripts/render-report.sh"
LOCKED_REPORT_DIR="$TEST_TMP/locked-report-target"
mkdir -p "$LOCKED_REPORT_DIR"
chmod 500 "$LOCKED_REPORT_DIR"
run_capture "$BASH_BIN" "$REPORT_FAULT_SKILL/scripts/scan.sh" --repo "$SAME_BASE_A" --report "$LOCKED_REPORT_DIR"
chmod 700 "$LOCKED_REPORT_DIR"
assert_rc 1 "report 原子替换 mv 失败时闭合"
assert_rule report-generation-error "report mv 失败转为结构化 hard"

# 自动发现到多份不同配置时，必须报告遮蔽漂移。
SHADOW_PROJECT="$TEST_TMP/shadow-project"
make_fixture "$SHADOW_PROJECT"
cp "$SKILL_ROOT/config/default.yaml" "$SHADOW_PROJECT/doc-curator.yaml"
cp "$SKILL_ROOT/config/agentcmd-v4.example.yaml" "$FAULT_SKILL/config/shadow-project.yaml"
run_capture "$BASH_BIN" "$FAULT_SKILL/scripts/scan.sh" --repo "$SHADOW_PROJECT" --only config
assert_rc 1 "配置遮蔽漂移失败闭合"
assert_rule config-shadowed-divergence "配置来源漂移被检出"

# ── render-report.sh 测试 ──
# 构造一份覆盖四档 severity 的合法 JSONL fixture
REPORT_FIXTURE="$TEST_TMP/results.jsonl"
{
  printf '{"checker":"tasks","severity":"hard","rule_id":"r-hard","message":"hard 问题","suggestion":"修 hard"}\n'
  printf '{"checker":"files","severity":"adaptive","rule_id":"r-adaptive","message":"adaptive 问题","suggestion":"看 adaptive"}\n'
  printf '{"checker":"decisions","severity":"soft","rule_id":"r-soft","message":"soft 提示","suggestion":""}\n'
  printf '{"checker":"tasks","severity":"ok","rule_id":"r-ok-1","message":"通过 1","suggestion":""}\n'
  printf '{"checker":"tasks","severity":"ok","rule_id":"r-ok-2","message":"通过 2","suggestion":""}\n'
} > "$REPORT_FIXTURE"

# 文件断言 helper：文件非空
assert_nonempty() {
  local file="$1" name="$2"
  if [ -s "$file" ]; then pass "$name"; else fail "${name}（文件为空或不存在：${file}）"; fi
}
# 文件断言 helper：文件含字串
assert_contains() {
  local file="$1" needle="$2" name="$3"
  if grep -qF "$needle" "$file"; then pass "$name"; else fail "${name}（未找到：${needle}）"; fi
}
# 文件断言 helper：文件不含字串
assert_not_contains() {
  local file="$1" needle="$2" name="$3"
  if ! grep -qF "$needle" "$file"; then pass "$name"; else fail "${name}（不应包含：${needle}）"; fi
}

# 用例1：合法 JSONL → 含汇总表 + 各档分组
REPORT_OUT="$TEST_TMP/report.md"
REPORT_RC=0
"$BASH_BIN" "$SCRIPT_DIR/render-report.sh" "$REPORT_FIXTURE" > "$REPORT_OUT" 2>"$TEST_TMP/render.err" || REPORT_RC=$?
if [ "$REPORT_RC" -eq 0 ]; then pass "render 合法 JSONL 返回 0"; else fail "render 合法 JSONL 返回 0（rc=${REPORT_RC}）"; fi
assert_nonempty "$REPORT_OUT" "render 合法 JSONL 生成非空报告"
assert_contains "$REPORT_OUT" "| 严重度 | 数量 |" "render 报告含汇总表头"
assert_contains "$REPORT_OUT" "| 🔴 hard（必须处理） | 1 |" "render 汇总表 hard 计数正确"
assert_contains "$REPORT_OUT" "| 🟡 adaptive（建议处理） | 1 |" "render 汇总表 adaptive 计数正确"
assert_contains "$REPORT_OUT" "| 🔵 soft（软提示） | 1 |" "render 汇总表 soft 计数正确"
assert_contains "$REPORT_OUT" "| ✅ ok（通过） | 2 |" "render 汇总表 ok 计数正确"
assert_contains "$REPORT_OUT" "## 🔴 hard（必须处理）" "render 含 hard 分组表"
assert_contains "$REPORT_OUT" "r-hard" "render hard 表含规则名"
assert_contains "$REPORT_OUT" "## 🟡 adaptive（建议处理）" "render 含 adaptive 分组表"
assert_contains "$REPORT_OUT" "## 🔵 soft（软提示）" "render 含 soft 分组表"
assert_contains "$REPORT_OUT" "ok 档共 2 条" "render ok 档只给计数说明"

# 用例2：空输入 → 生成「无结果」报告（非空文件，不崩溃）
: > "$TEST_TMP/empty.jsonl"
EMPTY_RC=0
"$BASH_BIN" "$SCRIPT_DIR/render-report.sh" "$TEST_TMP/empty.jsonl" > "$REPORT_OUT" 2>/dev/null || EMPTY_RC=$?
if [ "$EMPTY_RC" -eq 0 ]; then pass "render 空输入返回 0"; else fail "render 空输入返回 0（rc=${EMPTY_RC}）"; fi
assert_nonempty "$REPORT_OUT" "render 空输入生成非空报告"
assert_contains "$REPORT_OUT" "未收到任何检查结果" "render 空输入含无结果说明"

# 用例3：含非法行 → 跳过非法行、stderr 警告、不崩溃、合法行仍渲染
{
  printf 'this is not jsonl\n'
  printf '{"checker":"tasks","severity":"hard","rule_id":"valid","message":"合法行","suggestion":""}\n'
  printf '\n'
  printf '{broken\n'
} > "$TEST_TMP/mixed.jsonl"
MIXED_RC=0
"$BASH_BIN" "$SCRIPT_DIR/render-report.sh" "$TEST_TMP/mixed.jsonl" > "$REPORT_OUT" 2>"$TEST_TMP/mixed.err" || MIXED_RC=$?
if [ "$MIXED_RC" -eq 0 ]; then pass "render 含非法行返回 0"; else fail "render 含非法行返回 0（rc=${MIXED_RC}）"; fi
assert_contains "$REPORT_OUT" "valid" "render 非法输入仍渲染合法行"
if grep -q "跳过非法 JSONL" "$TEST_TMP/mixed.err"; then pass "render 非法行产生 stderr 警告"; else fail "render 非法行产生 stderr 警告"; fi
assert_contains "$REPORT_OUT" "| 🔴 hard（必须处理） | 1 |" "render 非法输入 hard 计数仍正确"

# 用例4：message 含 | 字符 → 正确转义不破坏表格
{
  printf '{"checker":"tasks","severity":"hard","rule_id":"pipe","message":"a|b|c","suggestion":"x|y"}\n'
} > "$TEST_TMP/pipe.jsonl"
"$BASH_BIN" "$SCRIPT_DIR/render-report.sh" "$TEST_TMP/pipe.jsonl" > "$REPORT_OUT" 2>/dev/null
# 表格行应为 | tasks | pipe | a\|b\|c | x\|y |（4 列，| 已转义）
if grep -qE '\| tasks \| pipe \| a\\|b\\|c \| x\\|y \|' "$REPORT_OUT"; then
  pass "render message 含 | 字符正确转义"
else
  fail "render message 含 | 字符正确转义"
  sed -n '1,20p' "$REPORT_OUT" >&2
fi

# 用例5：合法 JSON 转义 → 引号/反斜线不截断，控制字符转为空格
{
  printf '%s\n' '{"checker":"tasks","severity":"hard","rule_id":"escaped","message":"含\"引号、反斜线\\与换行\n下一行","suggestion":"检查\t制表"}'
} > "$TEST_TMP/escaped.jsonl"
ESCAPED_RC=0
"$BASH_BIN" "$SCRIPT_DIR/render-report.sh" "$TEST_TMP/escaped.jsonl" > "$REPORT_OUT" 2>/dev/null || ESCAPED_RC=$?
if [ "$ESCAPED_RC" -eq 0 ]; then pass "render 合法转义 JSONL 返回 0"; else fail "render 合法转义 JSONL 返回 0（rc=${ESCAPED_RC}）"; fi
assert_contains "$REPORT_OUT" '含"引号、反斜线\与换行 下一行' "render 转义引号和反斜线不截断"
assert_contains "$REPORT_OUT" "检查 制表" "render 换行和制表转为空格"

# 用例6：scan.sh --report 端到端 → 初始化和扫描退出码均显式断言
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --init-baseline
assert_rc 0 "report 端到端基线初始化返回 0"
assert_rule baseline-initialized "report 端到端基线初始化返回明确 rule"
E2E_REPORT="$TEST_TMP/e2e-report.md"
rm -f "$E2E_REPORT"
set +e
"$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --report "$E2E_REPORT" > "$TEST_TMP/e2e-out.jsonl" 2>"$TEST_TMP/e2e.err"
E2E_RC=$?
set -e
if [ "$E2E_RC" -eq 0 ]; then pass "scan --report 端到端返回 0"; else fail "scan --report 端到端返回 0（rc=${E2E_RC}）"; fi
assert_nonempty "$E2E_REPORT" "scan --report 生成非空报告"
assert_contains "$E2E_REPORT" "# doc-curator 体检报告" "scan --report 报告含标题"
assert_contains "$E2E_REPORT" "project-a" "scan --report 报告含项目目录名"
assert_contains "$E2E_REPORT" "default.yaml" "scan --report 报告含配置文件名"
assert_not_contains "$E2E_REPORT" "$TEST_TMP" "scan --report 不泄露临时绝对路径"
# stdout 仍为 JSONL（第一行是合法 JSON 对象）
if head -1 "$TEST_TMP/e2e-out.jsonl" 2>/dev/null | grep -qE '^\{"checker":"'; then
  pass "scan --report stdout 仍为 JSONL"
else
  fail "scan --report stdout 仍为 JSONL"
fi

# 用例7：scan.sh --report 与 --init-baseline 合用 → 拒绝（64）
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$PROJECT_A" --report "$TEST_TMP/x.md" --init-baseline
assert_rc 64 "scan --report 与 --init-baseline 合用返回 64"
assert_rule invalid-arguments "scan --report 与 --init-baseline 合用产生结构化 finding"

REPORT_FAIL_TARGET="$TEST_TMP/missing-report-dir/report.md"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$SAME_BASE_A" --report "$REPORT_FAIL_TARGET"
assert_rc 1 "显式报告生成失败会阻断"
assert_rule report-generation-error "报告失败进入结构化结果"

# ═══ DOC-006：--profile merge-gate 单次紧凑门禁 ═══
# 代表性 merge-review fixture：文档驱动项目的一次 base..head 变更，
# 含断链（hard+adaptive）、上下文不同步（adaptive）与可通过的 ok/soft 档。

dir_manifest() {
  find "$1" \( -path "$1/.git" -prune \) -o \( -type f -exec shasum -a 256 {} + \) 2>/dev/null | sort
}

sev_rule_lines() { # file severity
  grep "\"severity\":\"$2\"" "$1" | sed -n 's/.*"rule_id":"\([^"]*\)".*/\1/p' | sort | uniq
}

GATE_DIR="$TEST_TMP/merge-gate"
GATE_REPO="$GATE_DIR/fixture-repo"
mkdir -p "$GATE_REPO/docs"
git -C "$GATE_REPO" init -q
git -C "$GATE_REPO" config user.name doc-curator-gate-test
git -C "$GATE_REPO" config user.email doc-curator-gate-test@example.invalid
printf '%s\n' '# 当前任务' '' '### ISS-1 任务一' '### ISS-2 任务二' '### ISS-3 任务三' > "$GATE_REPO/docs/TASKS.md"
printf '%s\n' '# 决策记录' '' '## DEC-001' '- 日期: 2026-08-01' > "$GATE_REPO/docs/DECISIONS.md"
printf '%s\n' '# 变更记录' '' '## [0.1.0] - 2026-08-01' > "$GATE_REPO/CHANGELOG.md"
printf '%s\n' '# 门禁样例项目' > "$GATE_REPO/README.md"
git -C "$GATE_REPO" add .
git -C "$GATE_REPO" commit -qm base

printf '%s\n' '# 指南' '' '参考 [矩阵](docs/audit-matrix.md)、[旧计划](docs/old-plan.md)、[术语](docs/glossary.md)、[规格](docs/spec-draft.md)、[清单](docs/checklist.md)。' > "$GATE_REPO/docs/guide.md"
printf '%s\n' '# 审计矩阵' '' '关联 [指南](docs/guide.md)、[旧计划](docs/old-plan.md) 与 [已删除规格](docs/spec-draft.md)。' > "$GATE_REPO/docs/audit-matrix.md"
printf '%s\n' '# 术语表' '' '引用 [源文件](../README.md)。' > "$GATE_REPO/docs/glossary.md"
printf '%s\n' '' '### ISS-4 门禁任务' >> "$GATE_REPO/docs/TASKS.md"
git -C "$GATE_REPO" add .
git -C "$GATE_REPO" commit -qm feature
GATE_BASE="$(git -C "$GATE_REPO" rev-parse HEAD~1)"
GATE_HEAD="$(git -C "$GATE_REPO" rev-parse HEAD)"

cat > "$GATE_REPO/doc-curator.yaml" <<'GATE_YAML'
schema_version: 2
project: fixture-repo

files:
  - path: docs/TASKS.md
    role: active-tasks
  - path: docs/DECISIONS.md
    role: decision-log
  - path: CHANGELOG.md
    role: changelog
  - path: README.md
    role: readme

adaptive_rules:
  - id: tasks-active-count
    file: docs/TASKS.md
    metric: active_task_count
    seed_value: 3
    multiplier: 1.5
    active_count_pattern: "^### ISS-[0-9]+"

soft_rules:
  - id: changelog-recent-release-entry
    file: CHANGELOG.md
  - id: readme-current-status
    file: README.md

context_sync:
  enabled: true
  local_reversible:
    max_files: 1
    max_insertions: 50
  systemic:
    min_files: 3
    min_insertions: 200
  change_types:
    - pattern: "**"
      expect_changelog: true
      expect_tasks: false
      expect_decision: false
      expect_figures: false
      expect_skill_internal: false

dec_ref_sync:
  enabled: true
  scan_extensions: md
  exclude_paths: .git node_modules

markdown_link_broken:
  enabled: true
  check_code_paths: false
  default_severity: adaptive
  source_severity_paths:
    - path: "docs/audit-*.md"
      severity: hard

task_source_contract:
  enabled: false
GATE_YAML

# 影子 Skill：check-*.sh 包一层调用计数 wrapper，统计一次 scan 的 checker 调用次数。
GATE_SHADOW="$TEST_TMP/gate-shadow-skill"
mkdir -p "$GATE_SHADOW/config" "$GATE_SHADOW/scripts"
cp "$SKILL_ROOT/scripts/"* "$GATE_SHADOW/scripts/"
cp "$SKILL_ROOT/config/default.yaml" "$GATE_SHADOW/config/default.yaml"
GATE_LEGACY_CALLS="$TEST_TMP/gate-legacy-calls.log"
GATE_COMPACT_CALLS="$TEST_TMP/gate-compact-calls.log"
GATE_CALL_LOG="$TEST_TMP/gate-calls.log"
: > "$GATE_CALL_LOG"
for gate_ck in "$GATE_SHADOW"/scripts/check-*.sh; do
  gate_name="$(basename "$gate_ck")"
  mv "$gate_ck" "$GATE_SHADOW/scripts/.real-$gate_name"
  cat > "$gate_ck" <<GATE_WRAPPER
#!/usr/bin/env bash
printf '%s\n' "$gate_name" >> "$GATE_CALL_LOG"
exec "$BASH_BIN" "$GATE_SHADOW/scripts/.real-$gate_name" "\$@"
GATE_WRAPPER
  chmod +x "$gate_ck"
done

# legacy 基线：全量 JSONL stdout。
run_capture "$BASH_BIN" "$GATE_SHADOW/scripts/scan.sh" --repo "$GATE_REPO" --range "$GATE_BASE..$GATE_HEAD"
assert_rc 1 "legacy 全量扫描返回 1"
cp "$GATE_CALL_LOG" "$GATE_LEGACY_CALLS"
: > "$GATE_CALL_LOG"
cp "$LAST_OUT" "$GATE_DIR/legacy.jsonl"
GATE_LEGACY_BYTES="$(wc -c < "$GATE_DIR/legacy.jsonl" | tr -d ' ')"
GATE_LEGACY_LINES="$(wc -l < "$GATE_DIR/legacy.jsonl" | tr -d ' ')"
LEGACY_HARD_LINES="$(sev_rule_lines "$GATE_DIR/legacy.jsonl" hard)"
LEGACY_ADAPTIVE_LINES="$(sev_rule_lines "$GATE_DIR/legacy.jsonl" adaptive)"
printf 'INFO legacy_bytes=%s legacy_lines=%s legacy_hard=%s legacy_adaptive=%s legacy_soft=%s legacy_ok=%s\n' \
  "$GATE_LEGACY_BYTES" "$GATE_LEGACY_LINES" \
  "$(grep -c '"severity":"hard"' "$GATE_DIR/legacy.jsonl" || true)" \
  "$(grep -c '"severity":"adaptive"' "$GATE_DIR/legacy.jsonl" || true)" \
  "$(grep -c '"severity":"soft"' "$GATE_DIR/legacy.jsonl" || true)" \
  "$(grep -c '"severity":"ok"' "$GATE_DIR/legacy.jsonl" || true)"
if [ -n "$LEGACY_HARD_LINES" ] && [ -n "$LEGACY_ADAPTIVE_LINES" ]; then
  pass "legacy fixture 命中 hard 与 adaptive 阻断项"
else
  fail "legacy fixture 命中 hard 与 adaptive 阻断项"
fi

# gate 断言 helper：从紧凑摘要行提取数组字段并排序。
gate_array_lines() { # file key
  sed -n "s/.*\"$2\":\[\([^]]*\)\].*/\1/p" "$1" | tr ',' '\n' | tr -d '"' | awk 'NF' | sort
}
gate_count_field() { # file key
  sed -n "s/.*\"$2\":\([0-9][0-9]*\).*/\1/p" "$1" | head -1
}

# ── compact 门禁主路径：写边界快照 + 单次调用 + 等价性 + 50% 带宽 ──
GATE_WATCH="$TEST_TMP/gate-tmpwatch"
mkdir -p "$GATE_WATCH"
dir_manifest "$GATE_REPO" > "$GATE_DIR/repo.before"
dir_manifest "$GATE_SHADOW" > "$GATE_DIR/shadow.before"
run_capture env TMPDIR="$GATE_WATCH" "$BASH_BIN" "$GATE_SHADOW/scripts/scan.sh" \
  --repo "$GATE_REPO" --range "$GATE_BASE..$GATE_HEAD" --profile merge-gate
cp "$LAST_OUT" "$GATE_DIR/summary.json"
cp "$GATE_CALL_LOG" "$GATE_COMPACT_CALLS"
: > "$GATE_CALL_LOG"
assert_rc 1 "compact 门禁与 legacy 退出码一致（同 fixture）"
if [ "$(wc -l < "$GATE_DIR/summary.json" | tr -d ' ')" -eq 1 ]; then
  pass "compact 门禁 stdout 恰好一行紧凑 JSON"
else
  fail "compact 门禁 stdout 恰好一行紧凑 JSON"
fi
if grep -qF '"summary_schema":"doc-curator.merge-gate.v1"' "$GATE_DIR/summary.json" \
  && grep -qF '"profile":"merge-gate"' "$GATE_DIR/summary.json" \
  && grep -qF '"status":"complete"' "$GATE_DIR/summary.json"; then
  pass "compact 摘要含固定 schema/profile/status"
else
  fail "compact 摘要含固定 schema/profile/status"
fi
if grep -qF '"config_source":"project-root"' "$GATE_DIR/summary.json" \
  && grep -qF '"config_file":"doc-curator.yaml"' "$GATE_DIR/summary.json"; then
  pass "compact 摘要含 config provenance"
else
  fail "compact 摘要含 config provenance"
fi
if grep -qF '"scope_type":"range"' "$GATE_DIR/summary.json" \
  && grep -qF "\"base\":\"$GATE_BASE\"" "$GATE_DIR/summary.json" \
  && grep -qF "\"head\":\"$GATE_HEAD\"" "$GATE_DIR/summary.json" \
  && grep -qF "\"range\":\"$GATE_BASE..$GATE_HEAD\"" "$GATE_DIR/summary.json"; then
  pass "compact 摘要含精确 base/head/range"
else
  fail "compact 摘要含精确 base/head/range"
fi
for gate_sev in hard adaptive soft ok; do
  gate_n="$(gate_count_field "$GATE_DIR/summary.json" "${gate_sev}_count")"
  legacy_n="$(grep -c "\"severity\":\"$gate_sev\"" "$GATE_DIR/legacy.jsonl" || true)"
  if [ -n "$gate_n" ] && [ "$gate_n" = "$legacy_n" ]; then
    pass "compact ${gate_sev}_count 与 legacy 一致（${gate_n}）"
  else
    fail "compact ${gate_sev}_count 与 legacy 一致（compact=${gate_n} legacy=${legacy_n}）"
  fi
done
if diff <(printf '%s\n' "$LEGACY_HARD_LINES") <(gate_array_lines "$GATE_DIR/summary.json" blocking_rule_ids) >/dev/null \
  && diff <(printf '%s\n' "$LEGACY_ADAPTIVE_LINES") <(gate_array_lines "$GATE_DIR/summary.json" adaptive_rule_ids) >/dev/null; then
  pass "compact hard/adaptive 阻断 rule IDs 与 legacy 完全一致"
else
  fail "compact hard/adaptive 阻断 rule IDs 与 legacy 完全一致"
fi
if grep -qF '"next_action":"resolve-hard"' "$GATE_DIR/summary.json" \
  && grep -qF '"exit_code":1}' "$GATE_DIR/summary.json"; then
  pass "compact next_action/exit_code 路由一致"
else
  fail "compact next_action/exit_code 路由一致"
fi
GATE_COMPACT_BYTES="$(wc -c < "$GATE_DIR/summary.json" | tr -d ' ')"
if [ -n "$GATE_COMPACT_BYTES" ] && [ "$((GATE_COMPACT_BYTES * 2))" -le "$GATE_LEGACY_BYTES" ]; then
  pass "compact stdout bytes ≤ legacy 的 50%"
else
  fail "compact stdout bytes ≤ legacy 的 50%（compact=${GATE_COMPACT_BYTES} legacy=${GATE_LEGACY_BYTES}）"
fi
GATE_LEGACY_CALL_N="$(wc -l < "$GATE_LEGACY_CALLS" | tr -d ' ')"
if cmp -s "$GATE_LEGACY_CALLS" "$GATE_COMPACT_CALLS" \
  && [ "$GATE_LEGACY_CALL_N" -eq 9 ] \
  && [ "$(wc -l < "$GATE_COMPACT_CALLS" | tr -d ' ')" -eq 9 ]; then
  pass "单次 scan：各 checker 恰好调用一次且与 legacy 调用序列一致（9 个）"
else
  fail "单次 scan：各 checker 恰好调用一次且与 legacy 调用序列一致（legacy=${GATE_LEGACY_CALL_N}）"
fi
dir_manifest "$GATE_REPO" > "$GATE_DIR/repo.after"
dir_manifest "$GATE_SHADOW" > "$GATE_DIR/shadow.after"
if diff "$GATE_DIR/repo.before" "$GATE_DIR/repo.after" >/dev/null \
  && diff "$GATE_DIR/shadow.before" "$GATE_DIR/shadow.after" >/dev/null \
  && [ -z "$(ls -A "$GATE_WATCH")" ]; then
  pass "扫描不在 repo/worktree 外写文件（repo/shadow 无变化 + 受控 TMPDIR 无残留）"
else
  fail "扫描不在 repo/worktree 外写文件（repo/shadow 无变化 + 受控 TMPDIR 无残留）"
fi
printf 'INFO compact_bytes=%s legacy_bytes=%s ratio_pct=%s compact_lines=1\n' \
  "$GATE_COMPACT_BYTES" "$GATE_LEGACY_BYTES" \
  "$(awk -v c="$GATE_COMPACT_BYTES" -v l="$GATE_LEGACY_BYTES" 'BEGIN { if (l > 0) printf "%.1f", c * 100 / l; else printf "n/a" }')"

# ── --jsonl-out：完整 JSONL 显式落盘且与 legacy 逐字节一致，stdout 仍单行摘要 ──
GATE_WATCH2="$TEST_TMP/gate-tmpwatch2"
mkdir -p "$GATE_WATCH2"
GATE_JSONL_OUT="$GATE_DIR/full.jsonl"
rm -f "$GATE_JSONL_OUT"
run_capture env TMPDIR="$GATE_WATCH2" "$BASH_BIN" "$GATE_SHADOW/scripts/scan.sh" \
  --repo "$GATE_REPO" --range "$GATE_BASE..$GATE_HEAD" --profile merge-gate --jsonl-out "$GATE_JSONL_OUT"
assert_rc 1 "jsonl-out 模式退出码不受落盘影响"
if [ -f "$GATE_JSONL_OUT" ] && cmp -s "$GATE_DIR/legacy.jsonl" "$GATE_JSONL_OUT" \
  && [ -z "$(ls -A "$GATE_WATCH2")" ]; then
  pass "完整 JSONL 落盘与 legacy stdout 逐字节一致且临时目录无残留"
else
  fail "完整 JSONL 落盘与 legacy stdout 逐字节一致且临时目录无残留"
fi

# ── config-required：bundled-default 兜底在门禁模式失败闭合，不自动生成配置 ──
GATE_NOCONF="$TEST_TMP/merge-gate-noconfig"
mkdir -p "$GATE_NOCONF/docs"
printf '%s\n' '# 当前任务' '' '### ISS-1 任务一' > "$GATE_NOCONF/docs/TASKS.md"
printf '%s\n' '# 决策记录' > "$GATE_NOCONF/docs/DECISIONS.md"
printf '%s\n' '# 变更记录' '' '## [0.1.0] - 2026-08-01' > "$GATE_NOCONF/CHANGELOG.md"
printf '%s\n' '# 无配置样例' > "$GATE_NOCONF/README.md"
run_capture "$BASH_BIN" "$GATE_SHADOW/scripts/scan.sh" --repo "$GATE_NOCONF" --profile merge-gate
assert_rc 1 "门禁缺项目配置失败闭合（config-required）"
if grep -qF '"config_source":"bundled-default"' "$LAST_OUT" \
  && grep -qF '"next_action":"provide-config"' "$LAST_OUT" \
  && grep -qF '"config-required"' "$LAST_OUT"; then
  pass "config-required 摘要给出 provide-config 路由"
else
  fail "config-required 摘要给出 provide-config 路由"
fi
if [ "$(ls "$GATE_SHADOW/config")" = "default.yaml" ]; then
  pass "reviewer 未在 Skill 安装目录自动生成项目配置"
else
  fail "reviewer 未在 Skill 安装目录自动生成项目配置"
fi
run_capture "$BASH_BIN" "$GATE_SHADOW/scripts/scan.sh" --repo "$GATE_NOCONF"
assert_rc 0 "legacy 模式不受 config-required 影响（向后兼容）"

# ── 失败路径：参数与范围错误在 compact 模式下仍单行 fail-closed ──
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$GATE_NOCONF" --profile merge-gate --init-baseline
assert_rc 64 "merge-gate 与 --init-baseline 合用返回 64"
if grep -qF '"status":"error"' "$LAST_OUT" && grep -qF '"exit_code":64}' "$LAST_OUT" \
  && grep -qF '"blocking_rule_ids":["invalid-arguments"]' "$LAST_OUT"; then
  pass "merge-gate 参数错误输出紧凑错误摘要"
else
  fail "merge-gate 参数错误输出紧凑错误摘要"
  sed -n '1,5p' "$LAST_OUT" >&2
fi
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$GATE_NOCONF" --profile merge-gate --report "$GATE_DIR/x.md"
assert_rc 64 "merge-gate 与 --report 合用返回 64"
if [ ! -f "$GATE_DIR/x.md" ]; then pass "被拒绝的 --report 不产生文件"; else fail "被拒绝的 --report 不产生文件"; fi
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$GATE_NOCONF" --profile nonsense
assert_rc 64 "未知 profile 返回 64"
assert_rule invalid-arguments "未知 profile 走 legacy 结构化错误"
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$GATE_NOCONF" --jsonl-out "$GATE_DIR/y.jsonl"
assert_rc 64 "无 merge-gate 时 --jsonl-out 返回 64"
if [ ! -f "$GATE_DIR/y.jsonl" ]; then pass "被拒绝的 --jsonl-out 不产生文件"; else fail "被拒绝的 --jsonl-out 不产生文件"; fi
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$GATE_NOCONF" --profile merge-gate --bogus
assert_rc 64 "merge-gate 模式未知参数返回 64"
if grep -qF '"status":"error"' "$LAST_OUT" && grep -qF '"next_action":"fix-invocation"' "$LAST_OUT"; then
  pass "merge-gate 模式未知参数输出紧凑错误摘要"
else
  fail "merge-gate 模式未知参数输出紧凑错误摘要"
fi

# ── 范围解析失败失败闭合；working-tree 摘要带精确 head ──
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$GATE_REPO" --profile merge-gate --range "bogus-ref..HEAD"
assert_rc 1 "gate range 无法解析精确 base/head 时失败闭合"
if grep -qF '"merge-gate-range-unresolved"' "$LAST_OUT" && grep -qF '"base":""' "$LAST_OUT"; then
  pass "range-unresolved 记入 blocking 且 base 置空"
else
  fail "range-unresolved 记入 blocking 且 base 置空"
fi
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$GATE_REPO" --profile merge-gate --working-tree
if grep -qF '"scope_type":"working-tree"' "$LAST_OUT" && grep -qF "\"head\":\"$GATE_HEAD\"" "$LAST_OUT"; then
  pass "working-tree 摘要携带精确 HEAD"
else
  fail "working-tree 摘要携带精确 HEAD"
fi

# ── 候选 Skill 自扫：AgentCMD 档案审计 doc-curator 自身任务源合同（只读）──
run_capture "$BASH_BIN" "$SCRIPT_DIR/scan.sh" --repo "$SKILL_ROOT" --config "$SKILL_ROOT/config/agentcmd-v4.example.yaml" --only tasks
assert_rc 0 "候选 Skill 自扫任务源合同通过（含 DOC-006 REVIEW 卡）"

printf '\nTOTAL pass=%s fail=%s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

# Exact-value contract has a standalone entry for focused iteration; the full suite includes it.
"$BASH_BIN" "$SCRIPT_DIR/test-context-truth-values.sh"
"$BASH_BIN" "$SCRIPT_DIR/test-config-read.sh"
