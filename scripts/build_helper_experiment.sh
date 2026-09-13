#!/usr/bin/env bash
#
# ISS-029 · 自包含运行时技术验证 —— 无安装实验（fail closed）
#
# 在当前宿主架构上验证发行 helper 的「身份/版本/health/回环端口/退出」合同
# 原型（apps/desktop/experiments/iss029/helper_contract.py，纯 stdlib）：
# 不安装任何第三方依赖、不注册 launchd/SMAppService、不改 TCC、不扫描 HOME、
# 不触碰生产端口 7952。一切数据只写 mktemp 合成临时根。
# 任一必选用例失败即以非零码退出（fail closed）。
#
# 用法：bash scripts/build_helper_experiment.sh
# 产物：apps/desktop/experiments/iss029/results/experiment-<RUN_ID>.{json,log}
#
# bash 3.2（macOS 自带）兼容；不依赖 bash 4+ 特性。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXP_DIR="$ROOT/apps/desktop/experiments/iss029"
RESULTS_DIR="$EXP_DIR/results"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
JSON_OUT="$RESULTS_DIR/experiment-$RUN_ID.json"
LOG_OUT="$RESULTS_DIR/experiment-$RUN_ID.log"
HELPER_SRC="$EXP_DIR/helper_contract.py"

# 实验端口段：避开生产 7952；独立、可重复
PORT_BASE=17963
PORT_RANGE=5
PORT_LAST=$((PORT_BASE + PORT_RANGE - 1))

WORK="$(mktemp -d "${TMPDIR:-/tmp}/fathom-iss029.XXXXXX")"
# 「安装路径」反例目录：空格 + 中文 + &。helper 的可执行副本只放在这里（只读），
# 断言 helper 运行期间零写入自身所在目录。
RES_DIR="$WORK/app/Fathom 实验 & Helper 目录"
DATA_A="$WORK/data-A"          # 主合同用例
DATA_CRASH="$WORK/data-crash"  # 崩溃/接管用例

mkdir -p "$RESULTS_DIR" "$RES_DIR"
: > "$LOG_OUT"

CASES_JSONL="$WORK/cases.jsonl"
: > "$CASES_JSONL"
ENV_JSON="$WORK/env.json"
PIDS_FILE="$WORK/pids"
: > "$PIDS_FILE"
FAILED=0
PASSED=0
WAIT_CODE=""
HELPER_PID=""
DUMMY_PID=""

log() { printf '%s\n' "$*" | tee -a "$LOG_OUT" >&2; }

record() { # record <name> <status: pass|fail> <detail>
  python3 - "$1" "$2" "$3" >> "$CASES_JSONL" <<'PYEOF'
import json, sys
print(json.dumps({"name": sys.argv[1], "status": sys.argv[2],
                  "detail": sys.argv[3]}, ensure_ascii=False))
PYEOF
  if [ "$2" = "fail" ]; then FAILED=$((FAILED + 1)); else PASSED=$((PASSED + 1)); fi
  log "[$2] $1 :: $3"
}

summarize() { # summarize <verdict-override> —— 汇总 env + 用例为结果 JSON
  local override="${1:-}"
  python3 - "$ENV_JSON" "$CASES_JSONL" "$JSON_OUT" "$RUN_ID" "$FAILED" "$PASSED" "$override" <<'PYEOF'
import json, sys
try:
    env = json.load(open(sys.argv[1]))
except Exception:
    env = {}
cases = [json.loads(l) for l in open(sys.argv[2]) if l.strip()]
failed, passed = int(sys.argv[5]), int(sys.argv[6])
verdict = sys.argv[7] or ("PASS" if failed == 0 else "FAIL")
out = {
    "schema": "fathom.iss029.experiment-results.v1",
    "run_id": sys.argv[4],
    "verdict": verdict,
    "failed": failed,
    "passed": passed,
    "host_note": "仅证明当前 arm64 宿主；x86_64 待 ISS-041 原生 runner 复验",
    "no_install_note": "本实验零第三方依赖安装；冻结冒烟见 build_helper_smoke.sh",
    "env": env,
    "cases": cases,
}
with open(sys.argv[3], "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(json.dumps({"verdict": verdict, "passed": passed, "failed": failed,
                  "results_file": sys.argv[3]}, ensure_ascii=False))
PYEOF
}

stop_all() { # 停止本实验登记过的全部进程（幂等，不针对未知进程）
  local p
  if [ -s "$PIDS_FILE" ]; then
    while read -r p; do kill -TERM "$p" 2>/dev/null || true; done < "$PIDS_FILE"
    sleep 1
    while read -r p; do kill -KILL "$p" 2>/dev/null || true; done < "$PIDS_FILE"
  fi
}

reclaim_check() { # 资源回收断言：本实验启动的进程与端口不得残留（在 summarize 前调用）
  local still=0 p port
  if pgrep -f "$WORK" >/dev/null 2>&1; then
    still=1; log "[reclaim] 残留进程（pgrep -f ${WORK}）"
  fi
  if pgrep -f "iss029-dummy-$RUN_ID" >/dev/null 2>&1; then
    still=1; log "[reclaim] 残留 dummy 占用进程"
  fi
  for port in $(seq "$PORT_BASE" "$PORT_LAST"); do
    p="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$p" ]; then still=1; log "[reclaim] 端口 $port 仍被 pid=$p 占用"; fi
  done
  if [ "$still" -ne 0 ]; then
    record "Z1-resource-reclaim" fail "存在残留进程或端口占用"
  else
    record "Z1-resource-reclaim" pass "无残留进程、实验端口全部释放"
  fi
}

final_cleanup() { # EXIT 兜底：只做静默回收，不再记账（reclaim_check 已在主流程断言）
  stop_all
  rm -rf "$WORK"
}
trap final_cleanup EXIT

wait_http() { # wait_http <url> [timeout_s] —— 轮询至 HTTP 200
  local url="$1" t="${2:-10}" i code
  for ((i = 0; i < t * 4; i++)); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then return 0; fi
    sleep 0.25
  done
  return 1
}

wait_exit() { # wait_exit <pid> [timeout_s] —— 等退出（僵尸态视作已退出）；WAIT_CODE=退出码
  local pid="$1" t="${2:-10}" i stat
  for ((i = 0; i < t * 4; i++)); do
    stat="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ -z "$stat" ] || [ "${stat:0:1}" = "Z" ]; then
      # 不能用 `wait ... || true`：那会把 WAIT_CODE 固定成 0
      set +e
      wait "$pid" 2>/dev/null
      WAIT_CODE=$?
      set -e
      return 0
    fi
    sleep 0.25
  done
  return 1
}

json_get() { # json_get <file> <pyexpr over d>
  python3 - "$1" "$2" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
print(eval(sys.argv[2], {"d": d}))
PYEOF
}

health_assert() { # health_assert <port> —— /health 身份字段全量断言
  local port="$1" out
  out="$(curl -s --max-time 3 "http://127.0.0.1:$port/health" || true)"
  python3 - "$out" "$port" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1]); port = int(sys.argv[2])
assert d["status"] == "ok", d
assert d["service"] == "dev.fathom.helper-experiment.iss029", d
assert d["protocol_version"] == 1, d
assert d["port"] == port, d
assert isinstance(d["pid"], int) and d["pid"] > 1, d
assert isinstance(d["uptime_s"], (int, float)), d
PYEOF
}

start_helper() { # start_helper <data_dir> [extra...] —— 从含空格/中文/& 的资源目录后台启动
  local data_dir="$1"; shift
  (
    cd "$RES_DIR" || exit 1
    exec python3 "$RES_DIR/helper_contract.py" serve --data-dir "$data_dir" "$@"
  ) >> "$LOG_OUT" 2>&1 &
  HELPER_PID=$!
  echo "$HELPER_PID" >> "$PIDS_FILE"
}

start_dummy() { # start_dummy <port> —— 「未知进程」HTTP 占位（非 Fathom 语义）
  local port="$1"
  python3 - "$port" "iss029-dummy-$RUN_ID" >> "$LOG_OUT" 2>&1 <<'PYEOF' &
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[1])
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        body = b"<html><body>some other service</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
PYEOF
  DUMMY_PID=$!
  echo "$DUMMY_PID" >> "$PIDS_FILE"
}

# ================================================================ 环境门禁
log "== ISS-029 无安装实验 RUN_ID=$RUN_ID work=$WORK =="

uname -a > "$WORK/env_uname" 2>&1 || true
uname -m > "$WORK/env_machine" 2>/dev/null || true
sw_vers -productVersion > "$WORK/env_macver" 2>/dev/null || true
sw_vers -buildVersion > "$WORK/env_macbuild" 2>/dev/null || true
sysctl -n machdep.cpu.brand_string > "$WORK/env_cpu" 2>/dev/null || true
command -v python3 > "$WORK/env_pypath" 2>/dev/null || echo missing > "$WORK/env_pypath"
python3 --version > "$WORK/env_pyver" 2>&1 || true
python3 -c 'import sys; print(sys.base_prefix)' > "$WORK/env_pybase" 2>/dev/null \
  || echo missing > "$WORK/env_pybase"
python3 -c 'import venv; print("ok")' > "$WORK/env_venv" 2>/dev/null \
  || echo missing > "$WORK/env_venv"
python3 -c 'import PyInstaller; print(PyInstaller.__version__)' > "$WORK/env_pyi" 2>/dev/null \
  || echo not-installed > "$WORK/env_pyi"
python3 -c 'import Nuitka; print("present")' > "$WORK/env_nuitka" 2>/dev/null \
  || echo not-installed > "$WORK/env_nuitka"
for t in file curl lsof codesign otool pgrep shasum seq; do
  command -v "$t" > "$WORK/env_tool_$t" 2>/dev/null || echo missing > "$WORK/env_tool_$t"
done

python3 - "$WORK" <<'PYEOF' > "$ENV_JSON"
import json, os, sys
w = sys.argv[1]
def rd(name, default=""):
    try:
        return open(os.path.join(w, "env_" + name)).read().strip() or default
    except OSError:
        return default
env = {
    "uname": rd("uname"), "machine": rd("machine"),
    "macos_product_version": rd("macver"), "macos_build": rd("macbuild"),
    "cpu": rd("cpu"),
    "python3_path": rd("pypath"), "python3_version": rd("pyver"),
    "python3_base_prefix": rd("pybase"), "venv_module": rd("venv"),
    "pyinstaller_import": rd("pyi"), "nuitka_import": rd("nuitka"),
    "tools": {t: rd("tool_" + t) for t in
              ["file", "curl", "lsof", "codesign", "otool", "pgrep", "shasum", "seq"]},
}
json.dump(env, sys.stdout, ensure_ascii=False, indent=2)
PYEOF

MACHINE="$(json_get "$ENV_JSON" 'd["machine"]')"
if [ "$MACHINE" != "arm64" ]; then
  record "env-gate-arm64" fail "宿主架构 $MACHINE ≠ arm64；实验仅授权当前 arm64 宿主"
  summarize "FAILED_ENV_GATE"
  exit 1
fi
record "env-gate-arm64" pass "uname -m = arm64"

PYV="$(json_get "$ENV_JSON" 'd["python3_version"]')"
PYP="$(json_get "$ENV_JSON" 'd["python3_path"]')"
if [ "$PYP" = "missing" ]; then
  record "env-gate-python3" fail "python3 不可用"
  summarize "FAILED_ENV_GATE"
  exit 1
fi
record "env-gate-python3" pass "${PYV} @ ${PYP}（base_prefix=$(json_get "$ENV_JSON" 'd["python3_base_prefix"]')）"
record "env-installer-absence" pass \
  "PyInstaller=$(json_get "$ENV_JSON" 'd["pyinstaller_import"]')、Nuitka=$(json_get "$ENV_JSON" 'd["nuitka_import"]')：本实验零安装；冻结冒烟由 build_helper_smoke.sh fail-closed 处理"

# ================================================================ 准备资源目录
cp "$HELPER_SRC" "$RES_DIR/helper_contract.py"
RES_BEFORE="$(cd "$RES_DIR" && find . -type f -exec shasum {} + | sort)"
record "env-synthetic-root" pass "全部数据根位于合成临时根 ${WORK}；资源目录：${RES_DIR}"

# ================================================================ C0 用法错误 → 退出码 2
CODE=0
(cd "$RES_DIR" && python3 "$RES_DIR/helper_contract.py" serve) >> "$LOG_OUT" 2>&1 \
  || CODE=$?
if [ "$CODE" -eq 2 ]; then
  record "C0-usage-exit-2" pass "缺 --data-dir → argparse 退出码 2"
else
  record "C0-usage-exit-2" fail "期望退出码 2，实际 $CODE"
fi

# ================================================================ C1 --version 身份（nasty 路径下）
VERSION_OUT="$(cd "$RES_DIR" && python3 "$RES_DIR/helper_contract.py" --version 2>>"$LOG_OUT")" || true
if printf '%s' "$VERSION_OUT" | python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
assert d["service"] == "dev.fathom.helper-experiment.iss029", d
assert d["protocol_version"] == 1 and d["helper_version"] == "0.0.1-iss029", d
assert d["machine"] == "arm64", d
assert d["python"] and d["exe"], d
' >> "$LOG_OUT" 2>&1; then
  record "C1-version-identity" pass "含空格/中文/& 目录下 --version 身份 JSON 正确（machine=arm64）"
else
  record "C1-version-identity" fail "输出不合规：$VERSION_OUT"
fi

# ================================================================ C2 serve/health/discovery/Host/回环
start_helper "$DATA_A" --port "$PORT_BASE" --port-range "$PORT_RANGE"
H1="$HELPER_PID"
if wait_http "http://127.0.0.1:$PORT_BASE/health" 10 && health_assert "$PORT_BASE"; then
  record "C2-health-identity" pass "/health 200 且身份/协议/端口/pid 字段正确"
else
  record "C2-health-identity" fail "/health 未就绪或断言失败"
fi

DISC="$DATA_A/helper-instance.json"
if [ -f "$DISC" ] && [ "$(json_get "$DISC" 'd["port"]')" = "$PORT_BASE" ] \
   && [ "$(json_get "$DISC" 'd["pid"]')" = "$H1" ]; then
  record "C2-discovery-file" pass "discovery 原子写入：port=$PORT_BASE pid=$H1"
else
  record "C2-discovery-file" fail "discovery 缺失或字段异常：$DISC"
fi

BAD_HOST="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -H 'Host: evil.example' "http://127.0.0.1:$PORT_BASE/health" || true)"
GOOD_HOST="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -H "Host: localhost:$PORT_BASE" "http://127.0.0.1:$PORT_BASE/health" || true)"
if [ "$BAD_HOST" = "403" ] && [ "$GOOD_HOST" = "200" ]; then
  record "C2-host-guard" pass "非 loopback Host=403；localhost 别名=200"
else
  record "C2-host-guard" fail "bad=$BAD_HOST good=$GOOD_HOST"
fi

LISTEN_LINE="$(lsof -nP -iTCP:"$PORT_BASE" -sTCP:LISTEN 2>/dev/null | tail -1 || true)"
if printf '%s' "$LISTEN_LINE" | grep -q "127.0.0.1:$PORT_BASE"; then
  record "C2-loopback-only" pass "仅监听 127.0.0.1:${PORT_BASE}（${LISTEN_LINE}）"
else
  record "C2-loopback-only" fail "监听地址异常：$LISTEN_LINE"
fi

NOTFOUND="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT_BASE/nope" || true)"
if [ "$NOTFOUND" = "404" ]; then
  record "C2-404-unknown-path" pass "未知路径 404"
else
  record "C2-404-unknown-path" fail "未知路径返回 $NOTFOUND"
fi

# ================================================================ C3 停机令牌
if [ -f "$DISC" ]; then
  TOK="$(json_get "$DISC" 'd["control_token"]')"
  WRONG="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -X POST \
    -H "X-Fathom-Helper-Token: deadbeef" "http://127.0.0.1:$PORT_BASE/shutdown" || true)"
  RIGHT="$(curl -s --max-time 3 -X POST -H "X-Fathom-Helper-Token: $TOK" \
    "http://127.0.0.1:$PORT_BASE/shutdown" || true)"
  if wait_exit "$H1" 8; then
    if [ "$WRONG" = "403" ] && [ "$WAIT_CODE" -eq 0 ] && [ ! -f "$DISC" ]; then
      record "C3-shutdown-token" pass "错令牌 403；正确令牌优雅退出 0 且 discovery 已清理"
    else
      record "C3-shutdown-token" fail "wrong=$WRONG exit=$WAIT_CODE disc=$([ -f "$DISC" ] && echo exists || echo gone)"
    fi
  else
    record "C3-shutdown-token" fail "shutdown 后 8s 未退出"
  fi
else
  record "C3-shutdown-token" fail "无 discovery 可取令牌"
fi

# ================================================================ C4 SIGTERM / SIGINT
start_helper "$DATA_A" --port "$PORT_BASE" --port-range "$PORT_RANGE"
H2="$HELPER_PID"
wait_http "http://127.0.0.1:$PORT_BASE/health" 10 || true
kill -TERM "$H2" 2>/dev/null || true
if wait_exit "$H2" 8; then
  if [ "$WAIT_CODE" -eq 0 ] && [ ! -f "$DISC" ]; then
    record "C4-sigterm" pass "SIGTERM 优雅退出 0，discovery 清理"
  else
    record "C4-sigterm" fail "exit=$WAIT_CODE disc=$([ -f "$DISC" ] && echo exists || echo gone)"
  fi
else
  record "C4-sigterm" fail "SIGTERM 后 8s 未退出"
fi

start_helper "$DATA_A" --port "$PORT_BASE" --port-range "$PORT_RANGE"
H3="$HELPER_PID"
wait_http "http://127.0.0.1:$PORT_BASE/health" 10 || true
kill -INT "$H3" 2>/dev/null || true
if wait_exit "$H3" 8; then
  if [ "$WAIT_CODE" -eq 0 ] && [ ! -f "$DISC" ]; then
    record "C4-sigint" pass "SIGINT 优雅退出 0，discovery 清理"
  else
    record "C4-sigint" fail "exit=$WAIT_CODE disc=$([ -f "$DISC" ] && echo exists || echo gone)"
  fi
else
  record "C4-sigint" fail "SIGINT 后 8s 未退出"
fi

# ================================================================ C5 未知占用 → 顺位让位，不杀占用者
start_dummy "$PORT_BASE"
D1="$DUMMY_PID"
wait_http "http://127.0.0.1:$PORT_BASE/" 10 || true
start_helper "$DATA_A" --port "$PORT_BASE" --port-range "$PORT_RANGE"
H4="$HELPER_PID"
FALLBACK_PORT=none
for i in $(seq 1 40); do
  if [ -f "$DATA_A/helper-instance.json" ]; then
    FALLBACK_PORT="$(json_get "$DATA_A/helper-instance.json" 'd["port"]' 2>/dev/null || echo none)"
    [ "$FALLBACK_PORT" = "$((PORT_BASE + 1))" ] && break
  fi
  sleep 0.25
done
D1_ALIVE=no; kill -0 "$D1" 2>/dev/null && D1_ALIVE=yes
if [ "$FALLBACK_PORT" = "$((PORT_BASE + 1))" ] && health_assert "$((PORT_BASE + 1))" \
   && [ "$D1_ALIVE" = "yes" ]; then
  record "C5-unknown-occupier-fallback" pass "端口 ${PORT_BASE} 被未知进程占用 → 让位 ${FALLBACK_PORT}；占用进程存活未受任何信号"
else
  record "C5-unknown-occupier-fallback" fail "fallback=$FALLBACK_PORT dummy_alive=$D1_ALIVE"
fi
kill -TERM "$H4" 2>/dev/null || true
wait_exit "$H4" 8 || true
kill -TERM "$D1" 2>/dev/null || true
wait_exit "$D1" 8 || true

# ================================================================ C6 全端口被未知占用 → 退出码 3，零击杀
DUMMY_PIDS=""
for port in $(seq "$PORT_BASE" "$PORT_LAST"); do
  start_dummy "$port"
  wait_http "http://127.0.0.1:$port/" 10 || true
  DUMMY_PIDS="$DUMMY_PIDS $DUMMY_PID"
done
CODE=0
(cd "$RES_DIR" && python3 "$RES_DIR/helper_contract.py" serve \
   --data-dir "$DATA_A" --port "$PORT_BASE" --port-range "$PORT_RANGE") >> "$LOG_OUT" 2>&1 \
  || CODE=$?
ALL_ALIVE=1
for p in $DUMMY_PIDS; do kill -0 "$p" 2>/dev/null || ALL_ALIVE=0; done
DISC6_GONE=$([ -f "$DATA_A/helper-instance.json" ] && echo exists || echo gone)
if [ "$CODE" -eq 3 ] && [ "$ALL_ALIVE" -eq 1 ] && [ "$DISC6_GONE" = "gone" ]; then
  record "C6-ports-exhausted-exit-3" pass "$PORT_RANGE 个候选端口全被未知进程占用 → 退出码 3；占用者全部存活；未写 discovery"
else
  record "C6-ports-exhausted-exit-3" fail "exit=$CODE all_alive=$ALL_ALIVE disc=$DISC6_GONE"
fi
for p in $DUMMY_PIDS; do kill -TERM "$p" 2>/dev/null || true; done
sleep 1
for p in $DUMMY_PIDS; do kill -KILL "$p" 2>/dev/null || true; done

# ================================================================ C7 单一所有者：第二实例退出码 4
start_helper "$DATA_A" --port "$PORT_BASE" --port-range "$PORT_RANGE"
H5="$HELPER_PID"
wait_http "http://127.0.0.1:$PORT_BASE/health" 10 || true
CODE=0
(cd "$RES_DIR" && python3 "$RES_DIR/helper_contract.py" serve \
   --data-dir "$DATA_A" --port "$PORT_BASE" --port-range "$PORT_RANGE") >> "$LOG_OUT" 2>&1 \
  || CODE=$?
H5_STILL="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT_BASE/health" || true)"
if [ "$CODE" -eq 4 ] && [ "$H5_STILL" = "200" ]; then
  record "C7-single-owner-exit-4" pass "同服务健康实例在跑 → 第二实例退出码 4；原实例不受影响"
else
  record "C7-single-owner-exit-4" fail "exit=$CODE 首实例健康=$H5_STILL"
fi
kill -TERM "$H5" 2>/dev/null || true
wait_exit "$H5" 8 || true

# ================================================================ C8 崩溃（SIGKILL）→ stale discovery → 接管
rm -rf "$DATA_CRASH"
start_helper "$DATA_CRASH" --port "$PORT_BASE" --port-range "$PORT_RANGE" --crash-after 2
H6="$HELPER_PID"
wait_http "http://127.0.0.1:$PORT_BASE/health" 10 || true
OLD_INSTANCE="$(json_get "$DATA_CRASH/helper-instance.json" 'd["instance_id"]' 2>/dev/null || echo none)"
if wait_exit "$H6" 12; then
  STALE=$([ -f "$DATA_CRASH/helper-instance.json" ] && echo yes || echo no)
  if [ "$WAIT_CODE" -eq 137 ] && [ "$STALE" = "yes" ]; then
    record "C8a-crash-sigkill" pass "SIGKILL 崩溃 shell 报 137；discovery 残留为 stale"
  else
    record "C8a-crash-sigkill" fail "exit=$WAIT_CODE stale=$STALE"
  fi
else
  record "C8a-crash-sigkill" fail "--crash-after 未在 12s 内生效"
fi
start_helper "$DATA_CRASH" --port "$PORT_BASE" --port-range "$PORT_RANGE"
H7="$HELPER_PID"
if wait_http "http://127.0.0.1:$PORT_BASE/health" 10 && health_assert "$PORT_BASE"; then
  NEW_INSTANCE="$(json_get "$DATA_CRASH/helper-instance.json" 'd["instance_id"]' 2>/dev/null || echo none)"
  if [ -n "$NEW_INSTANCE" ] && [ "$NEW_INSTANCE" != "none" ] && [ "$NEW_INSTANCE" != "$OLD_INSTANCE" ]; then
    record "C8b-stale-takeover" pass "检测 stale(pid 已死)后接管同端口并改写 discovery（instance 轮换）"
  else
    record "C8b-stale-takeover" fail "old=$OLD_INSTANCE new=$NEW_INSTANCE"
  fi
else
  record "C8b-stale-takeover" fail "接管后 /health 不健康"
fi
kill -TERM "$H7" 2>/dev/null || true
wait_exit "$H7" 8 || true

# ================================================================ C9 资源目录只读复核
RES_AFTER="$(cd "$RES_DIR" && find . -type f -exec shasum {} + | sort)"
if [ "$RES_BEFORE" = "$RES_AFTER" ]; then
  record "C9-resources-readonly" pass "含空格/中文/& 的可执行目录全文指纹未变化（helper 零写入自身目录）"
else
  record "C9-resources-readonly" fail "资源目录在实验期间被改动"
fi

# ================================================================ 回收断言 + 汇总
stop_all
reclaim_check
summarize ""
log "== 汇总：PASS=$PASSED FAIL=$FAILED → $JSON_OUT =="

if [ "$FAILED" -gt 0 ]; then
  log "RESULT=FAIL（fail closed）"
  exit 1
fi
log "RESULT=PASS（含 cleanup 回收断言；若 cleanup 失败将以非零码退出）"
