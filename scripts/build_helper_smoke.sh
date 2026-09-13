#!/usr/bin/env bash
#
# ISS-029 · 冻结冒烟（fail closed）
#
# 目标：在 PM 批准的 task-local venv 里用 pinned PyInstaller 冻结真实
# fathom helper（onedir），验证 Mach-O 架构、--version 缺口反例（G3）、
# uvicorn 字符串导入反例（G1）、含空格/中文/& 路径运行、SIGTERM 优雅退出，
# 以及运行时目录写入冻结树内的只读违规证据（G2）。
#
# 安装政策（deny_by_default）：未在 INSTALL_AUTHORIZATION.json 的
# authorized_commands 里精确授权时，本脚本【绝不安装任何依赖】，打印
# 精确请求块并以退出码 3（BLOCKED_DEPENDENCY）结束——这不是失败假象，
# 而是把阻塞作为可审证据落盘。
#
# 用法：bash scripts/build_helper_smoke.sh
# 退出码：0 冒烟全过；1 验证失败（fail closed）；3 依赖未授权（BLOCKED）。
# 产物：会话 evidence 目录下 smoke-<RUN_ID>.{json,log}；不进入 Git。
#       （构建产物在 apps/desktop/experiments/iss029/build/，已 gitignore）
#
# bash 3.2 兼容。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXP_DIR="$ROOT/apps/desktop/experiments/iss029"
RESULTS_DIR="${FATHOM_ISS029_EVIDENCE_DIR:-$ROOT/.claude/agent-sessions/fathom-release-iss-029/evidence/smoke}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
JSON_OUT="$RESULTS_DIR/smoke-$RUN_ID.json"
LOG_OUT="$RESULTS_DIR/smoke-$RUN_ID.log"
VENV="$EXP_DIR/.venv-build"
BUILD_DIR="$EXP_DIR/build"
AUTH_FILE="${WORKER_INSTALL_AUTH_FILE:-$ROOT/.claude/agent-sessions/fathom-release-iss-029/INSTALL_AUTHORIZATION.json}"
REQ_RUNTIME="$ROOT/requirements-runtime.txt"
REQ_BUILD="$ROOT/requirements-runtime-build.txt"

# 说明：生产 serve 固定绑定 config.PORT=7952（无参数/环境覆盖，缺口 G6），
# 冒烟无法选择独立端口——这正是被记录的发行阻塞之一。

PIN_PYINSTALLER="$(sed -n 's/^pyinstaller==//p' "$REQ_BUILD" | head -1)"
[ -n "$PIN_PYINSTALLER" ] || PIN_PYINSTALLER="UNPINNED"

mkdir -p "$RESULTS_DIR"
: > "$LOG_OUT"
CASES_JSONL="$(mktemp "${TMPDIR:-/tmp}/iss029-smoke-cases.XXXXXX")"
FAILED=0
PASSED=0
BLOCKED=0

log() { printf '%s\n' "$*" | tee -a "$LOG_OUT" >&2; }

record() { # record <name> <pass|fail|blocked> <detail> [assertion|preparation]
  python3 - "$1" "$2" "$3" "${4:-assertion}" >> "$CASES_JSONL" <<'PYEOF'
import json, sys
print(json.dumps({"name": sys.argv[1], "status": sys.argv[2],
                  "detail": sys.argv[3], "kind": sys.argv[4]}, ensure_ascii=False))
PYEOF
  case "$2" in
    pass) PASSED=$((PASSED + 1)) ;;
    fail) FAILED=$((FAILED + 1)) ;;
    blocked) BLOCKED=$((BLOCKED + 1)) ;;
    *) log "[internal] 未知状态：$2"; exit 70 ;;
  esac
  log "[$2] $1 :: $3"
}

summarize() { # summarize <verdict>
  python3 - "$CASES_JSONL" "$JSON_OUT" "$RUN_ID" "$FAILED" "$PASSED" "$BLOCKED" "$1" "$PIN_PYINSTALLER" <<'PYEOF'
import json, sys
cases = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
verdict = sys.argv[7] or ("PASS" if int(sys.argv[4]) == 0 and int(sys.argv[6]) == 0 else "FAIL")
out = {
    "schema": "fathom.iss029.smoke-results.v1",
    "run_id": sys.argv[3],
    "verdict": verdict,
    "failed": int(sys.argv[4]),
    "passed": int(sys.argv[5]),
    "blocked": int(sys.argv[6]),
    "pin_pyinstaller": sys.argv[8],
    "host_note": "仅证明当前 arm64 宿主；x86_64 待 ISS-041 原生 runner 复验",
    "cases": cases,
}
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(json.dumps({"verdict": verdict, "passed": out["passed"],
                  "failed": out["failed"], "blocked": out["blocked"],
                  "results_file": sys.argv[2]},
                 ensure_ascii=False))
PYEOF
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/fathom-iss029-smoke.XXXXXX")"
PIDS_FILE="$WORK/processes"
: > "$PIDS_FILE"
process_identity() { ps -o lstart= -o command= -p "$1" 2>/dev/null | sed 's/^ *//;s/ *$//' || true; }
stable_process_identity() {
  local pid="$1" previous="" current="" i
  for i in $(seq 1 30); do
    current="$(process_identity "$pid")"
    if [ -n "$current" ] && [ "$current" = "$previous" ]; then printf '%s' "$current"; return 0; fi
    previous="$current"; sleep 0.05
  done
  return 1
}
register_process() {
  local identity
  identity="$(stable_process_identity "$1")" || return 1
  printf '%s|%s\n' "$1" "$identity" >> "$PIDS_FILE"
}
forget_process() {
  awk -F '|' -v target="$1" '$1 != target { print }' "$PIDS_FILE" > "$PIDS_FILE.next"
  mv "$PIDS_FILE.next" "$PIDS_FILE"
}
signal_tracked() {
  local wanted_pid="$1" wanted_signal="$2" line tracked_pid tracked_identity current_identity
  line="$(awk -F '|' -v target="$wanted_pid" '$1 == target { print; exit }' "$PIDS_FILE")"
  [ -n "$line" ] || return 1
  tracked_pid="${line%%|*}"; tracked_identity="${line#*|}"
  current_identity="$(process_identity "$tracked_pid")"
  [ -n "$current_identity" ] && [ "$current_identity" = "$tracked_identity" ] || return 1
  kill -"$wanted_signal" "$tracked_pid" 2>/dev/null
}
cleanup() {
  local line p
  if [ -s "$PIDS_FILE" ]; then
    while IFS= read -r line; do p="${line%%|*}"; signal_tracked "$p" TERM || true; done < "$PIDS_FILE"
    sleep 1
    while IFS= read -r line; do p="${line%%|*}"; signal_tracked "$p" KILL || true; done < "$PIDS_FILE"
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

wait_http() {
  local url="$1" t="${2:-20}" i code
  for ((i = 0; i < t * 4; i++)); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then return 0; fi
    sleep 0.25
  done
  return 1
}

wait_exit() { # wait_exit <pid> [timeout_s] → WAIT_CODE
  local pid="$1" t="${2:-15}" i stat
  for ((i = 0; i < t * 4; i++)); do
    stat="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ -z "$stat" ] || [ "${stat:0:1}" = "Z" ]; then
      set +e
      wait "$pid" 2>/dev/null
      WAIT_CODE=$?
      set -e
      forget_process "$pid"
      return 0
    fi
    sleep 0.25
  done
  return 1
}

log "== ISS-029 冻结冒烟 RUN_ID=${RUN_ID} =="

# ---------------------------------------------------------------- 授权检查
VENV_READY=no
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import PyInstaller' >/dev/null 2>&1; then
  GOT="$("$VENV/bin/python" -c 'import PyInstaller; print(PyInstaller.__version__)' 2>/dev/null || true)"
  if [ "$GOT" = "$PIN_PYINSTALLER" ]; then
    VENV_READY=yes
    record "smoke-venv-ready" pass "task-local venv 已含 pinned PyInstaller，跳过安装" preparation
  else
    record "smoke-venv-ready" fail "venv 内 pyinstaller=${GOT} 与 pin ${PIN_PYINSTALLER} 不一致"
  fi
fi

CMD_VENV="python3 -m venv $VENV"
CMD_PIP="$VENV/bin/pip install --no-input -r $REQ_BUILD -r $REQ_RUNTIME"
AUTHORIZED=no
if [ "$VENV_READY" != "yes" ]; then
  if [ ! -f "$AUTH_FILE" ]; then
    log "授权文件不存在：$AUTH_FILE"
  else
    AUTHORIZED="$(python3 - "$AUTH_FILE" "$CMD_VENV" "$CMD_PIP" <<'PYEOF'
import json, sys
try:
    auth = json.load(open(sys.argv[1]))
except Exception:
    print("no"); raise SystemExit
cmds = auth.get("authorized_commands") or []
want = [sys.argv[2], sys.argv[3]]
print("yes" if all(any(w == c for c in cmds) for w in want) else "no")
PYEOF
)"
  fi
fi

if [ "$VENV_READY" != "yes" ] && [ "$AUTHORIZED" != "yes" ]; then
  # ------------------------------------------------------------ 阻塞反例（可审证据）
  record "smoke-install-authorization" blocked \
    "policy=deny_by_default 且 authorized_commands 未含本脚本所需精确命令；按合同不安装、不冒充自包含"
  log ""
  log "==================== 向 PM 申请的精确安装授权（REQUEST） ===================="
  log "工具/版本      : pyinstaller==${PIN_PYINSTALLER}（见 requirements-runtime-build.txt）"
  log "运行时依赖     : requirements-runtime.txt（fastapi==0.141.1, uvicorn==0.52.4）"
  log "官方兼容依据   : PyPI 官方元数据（2026-09-13 查询）：pyinstaller 6.22.3 requires_python '>=3.8,<3.16'，"
  log "                 官方描述支持 macOS 10.15+、Python 3.8-3.15，非交叉编译器（目标架构原生构建）"
  log "目标环境       : task-local venv ${VENV}（不写全局、不进 PATH；已 gitignore）"
  log "磁盘预估       : venv+构建 ≈ 400-700MB，全部位于 ${EXP_DIR} 下"
  log "精确命令 1     : ${CMD_VENV}"
  log "精确命令 2     : ${CMD_PIP}"
  log "清理           : rm -rf '${VENV}' '${BUILD_DIR}'（可完全回滚，不影响仓库与系统）"
  log "授权方式       : 把上述两条精确命令写入 ${AUTH_FILE} 的 authorized_commands，"
  log "                 并将 policy 改离 deny_by_default 后重跑本脚本"
  log "=========================================================================="
  summarize "BLOCKED_DEPENDENCY"
  exit 3
fi

# ---------------------------------------------------------------- 安装（仅授权后到达这里）
if [ "$VENV_READY" != "yes" ]; then
  log "[authorized] 创建 task-local venv 并安装 pinned 依赖……"
  $CMD_VENV >> "$LOG_OUT" 2>&1
  $CMD_PIP >> "$LOG_OUT" 2>&1
  record "smoke-install" pass "pinned 安装完成（本脚本执行）"
fi
# 依赖锁快照（只读，不升级任何包）：ISS-031 CI lock 的对照证据
"$VENV/bin/pip" freeze > "$RESULTS_DIR/smoke-$RUN_ID.freeze.lock" 2>>"$LOG_OUT" || true
record "smoke-freeze-lock" pass "已生成依赖锁快照（准备证据，不代表运行时行为）" preparation

# ---------------------------------------------------------------- 冻结 A：无 hidden-import（G1 反例）
mkdir -p "$BUILD_DIR"
log "[freeze A] 不带 --hidden-import fathom.api（预期 serve 反例）……"
"$VENV/bin/pyinstaller" --noconfirm --clean --onedir \
  --paths "$ROOT" \
  --name fathom-helper-exp-a \
  --distpath "$BUILD_DIR/distA" --workpath "$BUILD_DIR/workA" --specpath "$BUILD_DIR" \
  "$EXP_DIR/freeze_entry.py" >> "$LOG_OUT" 2>&1
BIN_A="$BUILD_DIR/distA/fathom-helper-exp-a/fathom-helper-exp-a"
record "freeze-a-built" pass "对照 onedir 构建完成（准备步骤）" preparation

CODE=0
(cd "$WORK" && FATHOM_DB="$WORK/smoke-db/fathom.db" "$BIN_A" serve) >> "$LOG_OUT" 2>&1 || CODE=$?
sleep 1
# uvicorn 加载失败的标准输出是 "Could not import module \"fathom.api\"."；
# 直接 importlib 场景才吐 ModuleNotFoundError——两种形态都算实证
if grep -qE "Could not import module .?fathom\.api|ModuleNotFoundError: No module named .?fathom\.api" "$LOG_OUT"; then
  record "freeze-a-g1-counterexample" pass \
    "无 hidden-import 时冻结产物 serve 失败：uvicorn 的 \"fathom.api:app\" 字符串导入不被静态分析（缺口 G1 实证，exit=${CODE}）"
else
  record "freeze-a-g1-counterexample" fail "未捕获预期的 fathom.api 导入失败（exit=${CODE}）"
fi

# ---------------------------------------------------------------- 冻结 B：--hidden-import fathom.api
log "[freeze B] 带 --hidden-import fathom.api ……"
"$VENV/bin/pyinstaller" --noconfirm --clean --onedir \
  --paths "$ROOT" \
  --name fathom-helper-exp-b \
  --hidden-import fathom.api \
  --distpath "$BUILD_DIR/distB" --workpath "$BUILD_DIR/workB" --specpath "$BUILD_DIR" \
  "$EXP_DIR/freeze_entry.py" >> "$LOG_OUT" 2>&1
BIN_B="$BUILD_DIR/distB/fathom-helper-exp-b/fathom-helper-exp-b"
record "freeze-b-built" pass "带显式导入的 onedir 构建完成（准备步骤）" preparation

# ---------------------------------------------------------------- Mach-O / otool 证据
FILE_OUT="$(file "$BIN_B")"
if printf '%s' "$FILE_OUT" | grep -q "arm64" && ! printf '%s' "$FILE_OUT" | grep -q "x86_64"; then
  record "smoke-file-arm64" pass "${FILE_OUT}"
else
  record "smoke-file-arm64" fail "${FILE_OUT}"
fi
otool -L "$BIN_B" > "$RESULTS_DIR/smoke-$RUN_ID.otool-L.txt" 2>&1 || true
record "smoke-otool-L" pass "已生成动态依赖清单（准备证据；无交叉架构声明）" preparation

# ---------------------------------------------------------------- G3 反例：生产 CLI 无 --version
CODE=0
"$BIN_B" --version >> "$LOG_OUT" 2>&1 || CODE=$?
if [ "$CODE" -eq 2 ]; then
  record "smoke-g3-version-gap" pass \
    "冻结产物 --version 退出码 2（argparse 拒绝）：生产 CLI 无身份/版本面（缺口 G3 实证）"
else
  record "smoke-g3-version-gap" fail "--version 退出码 ${CODE}（预期 2）"
fi

# ---------------------------------------------------------------- 含空格/中文/& 路径 + serve + G2 证据
# 生产 serve 只会绑定 config.PORT=7952（无参数/环境覆盖）。若该端口已被
# 任何进程占用（含本机开发 launchd web 服务），绝不杀占用者：
# 记为 G6（固定端口无让位/身份探测）实证，serve 类用例转 blocked。
PORT_OCCUPIER="$(lsof -tiTCP:7952 -sTCP:LISTEN 2>/dev/null || true)"
PORT_OCCUPIER_BEFORE="$PORT_OCCUPIER"

NASTY="$WORK/app/Fathom 冒烟 & Helper 目录"
mkdir -p "$NASTY"
cp -R "$BUILD_DIR/distB/fathom-helper-exp-b/." "$NASTY/"
BIN_N="$NASTY/fathom-helper-exp-b"
mkdir -p "$WORK/smoke-db"

if [ -n "$PORT_OCCUPIER" ]; then
  record "smoke-g6-fixed-port-collision" pass \
    "实证 G6：生产 serve 固定绑定 7952 且无端口让位/身份探测；按合同不杀占用者"
  # 即便绑定失败，cmd_serve 的 ensure_runtime_dirs() 也先于 uvicorn.run 执行：
  # 仍可无侵入地取得 G2（冻结树内建运行时目录）与 G6（绑定失败路径）实证
  CODE=0
  (cd "$NASTY" && FATHOM_DB="$WORK/smoke-db/fathom.db" "$BIN_N" serve) \
    >> "$LOG_OUT" 2>&1 || CODE=$?
  sleep 1
  OCC_STILL="$(lsof -tiTCP:7952 -sTCP:LISTEN 2>/dev/null || true)"
  if grep -qE "Address already in use|Errno 48|\[Errno 48\]" "$LOG_OUT" \
     && [ "$OCC_STILL" = "$PORT_OCCUPIER_BEFORE" ]; then
    record "smoke-g6-runtime-bind-failure" pass \
      "冻结 serve 在 7952 绑定失败退出（exit=${CODE}）；占用者 PID 集合前后完全一致"
  else
    record "smoke-g6-runtime-bind-failure" fail "bind 失败日志或占用者 PID 前后一致断言未满足（exit=${CODE}）"
  fi
  RUNTIME_DIRS="$(find "$NASTY" -maxdepth 2 -type d \( -name data -o -name reports -o -name logs \) | sort | tr '\n' ' ')"
  if [ -n "$RUNTIME_DIRS" ]; then
    record "smoke-g2-readonly-violation" pass \
      "实证 G2：绑定失败路径仍在冻结树内创建运行时目录（${RUNTIME_DIRS}）——生产 config 需冻结感知数据根"
  else
    record "smoke-g2-readonly-violation" fail "未观测到 G2 预期（运行时目录未出现在冻结树内）——请人工复核"
  fi
  record "smoke-serve-nasty-path" blocked "7952 被占用，健康 serve 用例无法在不影响占用者的前提下执行（ nasty 目录可执行性已由 G2/G6 运行证明）"
  record "smoke-host-guard" blocked "需健康 serve 才能验证；7952 被占用"
  record "smoke-sigterm-frozen" blocked "需健康 serve 才能验证；7952 被占用"
  summarize "PARTIAL_BLOCKED_PORT_7952"
  log "RESULT=PARTIAL（7952 被占用；不杀未知占用进程）"
  exit 3
fi

CODE=0
(cd "$NASTY" && FATHOM_DB="$WORK/smoke-db/fathom.db" "$BIN_N" serve) \
  >> "$LOG_OUT" 2>&1 &
SERVE_PID=$!
register_process "$SERVE_PID"

if wait_http "http://127.0.0.1:7952/api/status" 30; then
  record "smoke-serve-nasty-path" pass "含空格/中文/& 目录下冻结 serve 就绪（127.0.0.1:7952/api/status 200）"
else
  record "smoke-serve-nasty-path" fail "30s 内 /api/status 未就绪"
fi

BAD_HOST="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -H 'Host: evil.example' \
  "http://127.0.0.1:7952/api/status" || true)"
if [ "$BAD_HOST" = "403" ]; then
  record "smoke-host-guard" pass "生产 API Host 守卫在冻结产物中仍生效（403）"
else
  record "smoke-host-guard" fail "Host 守卫返回 ${BAD_HOST}"
fi

# G2：运行时目录被写进冻结树（资源应只读）
RUNTIME_DIRS="$(find "$NASTY" -maxdepth 2 -type d \( -name data -o -name reports -o -name logs \) | sort)"
if [ -n "$RUNTIME_DIRS" ]; then
  record "smoke-g2-readonly-violation" pass \
    "实证 G2：冻结树内出现运行时目录（${RUNTIME_DIRS}）——生产 config.PROJECT_ROOT 需改为冻结感知数据根"
else
  record "smoke-g2-readonly-violation" fail "未观测到 G2 预期（运行时目录未出现在冻结树内）——请人工复核"
fi

# SIGTERM 优雅退出
signal_tracked "$SERVE_PID" TERM || true
if wait_exit "$SERVE_PID" 15 && [ "$WAIT_CODE" -eq 0 ]; then
  record "smoke-sigterm-frozen" pass "冻结 serve 对 SIGTERM 优雅退出 0（uvicorn 停机路径）"
else
  record "smoke-sigterm-frozen" fail "SIGTERM 后退出码异常：${WAIT_CODE}"
fi

# ---------------------------------------------------------------- 汇总
summarize ""
if [ "$FAILED" -gt 0 ]; then
  log "RESULT=FAIL（fail closed）"
  exit 1
fi
log "RESULT=PASS"
