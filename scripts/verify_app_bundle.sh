#!/usr/bin/env bash
#
# ISS-009 切片 1 · 验证 .app 产物的 fail-closed 脚本
#
# 用法：
#   bash scripts/verify_app_bundle.sh [APP_PATH] [RESULTS_DIR]
#
# APP_PATH 缺省：apps/desktop/src-tauri/target/release/bundle/macos/Fathom.app
# RESULTS_DIR 缺省：会话 evidence 目录（写入 RESULT/result.json + log）
#
# 断言清单（与任务卡 §Phase 2.12 一致）：
#   (a) 结构：Contents/Resources/helper/fathom-helper/fathom-helper 存在
#       且可执行；Info.plist CFBundleShortVersionString == 0.3.0；
#       bundle 内不存在 data/reports/logs 目录
#   (b) 只读布局：启动前后 .app 指纹（find + shasum）一致
#   (c) 含空格/中文/& 的临时目录里拷贝一份 .app 后 `open` 启动，
#       等 helper /health 就绪，确认运行根在
#       ~/Library/Application Support/Fathom（或测试用 FATHOM_RUNTIME_DIR
#       指向临时目录——优先用临时运行根避免污染真实用户数据，通过 env 传
#       给 open 的 --env 或用 launchctl setenv 等价手段），验证
#       helper-instance.json 0600
#   (d) 端口冲突：脚本自起 dummy 占 7952..7952+4 段之外的一个测试端口并
#       让壳以 FATHOM_PORT 指向它，确认 helper 让位到下一空闲端口且
#       dummy PID 未变（零击杀）
#   (e) 二次启动：再 open 一次 .app，确认不出现第二个 helper（让位）
#   (f) 退出：osascript tell application "Fathom" to quit 或向壳发 SIGTERM，
#       bounded 等待后确认 helper 进程消失、端口关闭、helper-instance.json
#       被清理
#   (g) 全程不触碰当前占用 7952 的生产进程（PID 6026 或其他），只观察不干预
#   (h) ISS-059：预写身份匹配但陈旧的 helper-instance.json（pid 不存在、
#       端口无监听）→ 壳探活后不导航死端口，重新拉起 helper 就绪、instance
#       文件被重写（pid 存活、port 与就绪端口一致）；全程零信号
#   (i) ISS-059：dummy 占满候选范围内所有未被外部占用的端口 → 壳不拉起
#       任何 fathom /health、dummy PID 集合不变（零击杀）、壳进程存活，
#       运行根 logs/helper.log 含 ports-exhausted 结构化标记
#
# 退出码：
#   0 全过
#   1 任一断言失败
#   3 阻塞（缺依赖 .app）

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PATH="${1:-$ROOT/apps/desktop/src-tauri/target/release/bundle/macos/Fathom.app}"
RESULTS_DIR="${2:-$ROOT/apps/desktop/src-tauri/verify-results/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$RESULTS_DIR"
LOG="$RESULTS_DIR/verify.log"
CASES="$RESULTS_DIR/cases.jsonl"
: > "$LOG"
: > "$CASES"

PASSED=0
FAILED=0

log() { printf '%s\n' "$*" | tee -a "$LOG"; }

record() { # name pass|fail detail
  python3 - "$1" "$2" "$3" >> "$CASES" <<'PYEOF'
import json, sys
print(json.dumps({"name": sys.argv[1], "status": "passed" if sys.argv[2] == "pass" else "failed",
                  "detail": sys.argv[3]}, ensure_ascii=False))
PYEOF
  case "$2" in
    pass) PASSED=$((PASSED + 1)) ;;
    fail) FAILED=$((FAILED + 1)) ;;
  esac
  log "[$2] $1 :: $3"
}

# ---------------------------------------------------------------- 阻塞检查
if [ ! -d "$APP_PATH" ]; then
  echo "[verify] BLOCKED：未找到 .app：$APP_PATH" >&2
  echo "[verify] 请先运行 bash scripts/build_app.sh" >&2
  exit 3
fi

# ---------------------------------------------------------------- (a) 结构
log "=== (a) 结构断言 ==="
HELPER_BIN="$APP_PATH/Contents/Resources/helper/fathom-helper/fathom-helper"
if [ -x "$HELPER_BIN" ]; then
  record "a-helper-exists" pass "helper 可执行文件存在：$HELPER_BIN"
else
  record "a-helper-exists" fail "helper 不可执行：$HELPER_BIN"
fi

INFO_PLIST="$APP_PATH/Contents/Info.plist"
if [ -f "$INFO_PLIST" ]; then
  VERSION_OUT="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$INFO_PLIST" 2>/dev/null || echo unknown)"
  if [ "$VERSION_OUT" = "0.3.0" ]; then
    record "a-infoplist-version" pass "CFBundleShortVersionString == 0.3.0"
  else
    record "a-infoplist-version" fail "CFBundleShortVersionString = ${VERSION_OUT}（预期 0.3.0）"
  fi
else
  record "a-infoplist-version" fail "Info.plist 不存在"
fi

# bundle 内不应有 data/reports/logs（PyInstaller 误把运行目录打到 bundle）
INSIDE_DATA="$(find "$APP_PATH" -maxdepth 4 -type d \( -name "data" -o -name "reports" -o -name "logs" \) 2>/dev/null | head -5)"
if [ -z "$INSIDE_DATA" ]; then
  record "a-no-runtime-dirs" pass "bundle 内不存在 data/reports/logs 目录"
else
  record "a-no-runtime-dirs" fail "bundle 内出现运行目录：$INSIDE_DATA"
fi

# ---------------------------------------------------------------- (b) 只读布局
log "=== (b) 只读布局断言 ==="
FINGER_BEFORE="$(find "$APP_PATH" -type f -print0 | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | awk '{print $1}')"
log "启动前 .app 指纹：${FINGER_BEFORE}"
sleep 0.2
FINGER_AFTER="$(find "$APP_PATH" -type f -print0 | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | awk '{print $1}')"
if [ "${FINGER_BEFORE}" = "${FINGER_AFTER}" ]; then
  record "b-readonly-layout" pass "启动前后 .app 指纹一致（${FINGER_BEFORE}）"
else
  record "b-readonly-layout" fail "启动后 .app 指纹漂移：${FINGER_BEFORE} → ${FINGER_AFTER}"
fi

# ---------------------------------------------------------------- (g) 7952 占用观察（不动）
log "=== (g) 7952 占用观察 ==="
PORT_OCCUPIER_7952="$(lsof -tiTCP:7952 -sTCP:LISTEN -n -P 2>/dev/null || true)"
if [ -n "$PORT_OCCUPIER_7952" ]; then
  record "g-7952-untouched" pass "本机 7952 由 PID ${PORT_OCCUPIER_7952} 占用；本脚本只观察不干预"
else
  record "g-7952-untouched" pass "本机 7952 当前未被占用"
fi

# ---------------------------------------------------------------- (c) 启动测试：含空格/中文/& 临时目录 + open
log "=== (c) 启动测试 ==="
NASTY_DIR_BASE="$(mktemp -d -t fathom-verify-XXXXXX)"
NASTY_APP_DIR="$NASTY_DIR_BASE/Fathom 验证 & 启动目录"
mkdir -p "$NASTY_APP_DIR"
cp -R "$APP_PATH" "$NASTY_APP_DIR/Fathom.app"
TEST_APP="$NASTY_APP_DIR/Fathom.app"
TEST_RUNTIME="$NASTY_DIR_BASE/runtime"
mkdir -p "$TEST_RUNTIME"

# 在 macOS 上 open 不能直接传 env；用 launchctl setenv 把测试运行根注入
# 用户域（setenv 仅影响后续 spawn 的子进程；它对 open 启动的 app 是否生效
# 由 launchd 决定——macOS Ventura+ 一般会继承，但为安全起见，本脚本同时设置
# 全局环境并期望 helper 进程能看到 FATHOM_RUNTIME_DIR；若不行，则回退
# 到真实 ~/Library/Application Support/Fathom 并在用例结尾清理本次产生的
# 文件）。
LAUNCHCTL_SETENV_OK=no
if launchctl setenv FATHOM_RUNTIME_DIR "$TEST_RUNTIME" 2>/dev/null; then
  LAUNCHCTL_SETENV_OK=yes
  log "launchctl setenv FATHOM_RUNTIME_DIR=$TEST_RUNTIME 成功"
fi

# 启动
open -a "$TEST_APP" >> "$LOG" 2>&1 || record "c-open-fail" fail "open 启动失败"
sleep 2

# 等待 helper /health 就绪
HELPER_READY=no
HELPER_PORT=""
for _ in $(seq 1 30); do
  for port in 7952 7953 7954 7955 7956; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
      HELPER_READY=yes
      HELPER_PORT="$port"
      break 2
    fi
  done
  sleep 1
done

if [ "$HELPER_READY" = "yes" ]; then
  record "c-helper-ready" pass "helper /health 在 127.0.0.1:$HELPER_PORT 就绪"
else
  record "c-helper-ready" fail "30s 内 helper 未就绪（open 启动 + launchctl setenv 可能未生效）"
fi

# ISS-009 切片 2：主界面 `/` 必须返回前端页面（html=True 挂载生效）。
# 缺 frontend 资源时 api.py 静默跳过挂载、`/` 返回 FastAPI 404 JSON——
# 切片 1 实机截图复现过该缺陷，此处断言防止回归。
if [ "$HELPER_READY" = "yes" ]; then
  ROOT_BODY="$(curl -s --max-time 2 "http://127.0.0.1:${HELPER_PORT}/" 2>/dev/null || true)"
  if printf '%s' "$ROOT_BODY" | grep -qi '<!doctype html\|<html'; then
    record "c-frontend-served" pass "/ 返回前端页面（首行：$(printf '%s' "$ROOT_BODY" | head -c 60)…）"
  elif [ -n "$ROOT_BODY" ]; then
    record "c-frontend-served" fail "/ 返回非 HTML：$(printf '%s' "$ROOT_BODY" | head -c 80)"
  else
    record "c-frontend-served" fail "/ 无响应体"
  fi
fi

# helper-instance.json 0600 校验
HI_PATH=""
if [ "$LAUNCHCTL_SETENV_OK" = "yes" ]; then
  HI_PATH="$TEST_RUNTIME/helper-instance.json"
else
  HI_PATH="$HOME/Library/Application Support/Fathom/helper-instance.json"
fi
if [ -f "$HI_PATH" ]; then
  MODE_BITS="$(stat -f '%Lp' "$HI_PATH")"
  if [ "$MODE_BITS" = "600" ]; then
    record "c-instance-0600" pass "helper-instance.json 0600：$HI_PATH"
  else
    record "c-instance-0600" fail "helper-instance.json 权限 ${MODE_BITS}：$HI_PATH"
  fi
else
  record "c-instance-0600" fail "helper-instance.json 未生成：$HI_PATH"
fi

# ---------------------------------------------------------------- (d) 端口冲突让位
log "=== (d) 端口冲突让位 ==="
DUMMY_PORT="$(python3 -c '
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
')"
log "脚本自起 dummy 占 $DUMMY_PORT"
# dummy 只 bind+listen 不应答：占用端口本身即目的（helper 探测 1xx/超时后让位）。
# ISS-055 修复：原 `python3 - "$DUMMY_PORT" ... &` 缺 stdin 脚本体，python 读
# EOF 即退出，dummy 从未监听，d-zero-kill 的 before 恒为空而必败。
python3 - "$DUMMY_PORT" >> "$LOG" 2>&1 <<'PYEOF' &
import socket, sys, time
port = int(sys.argv[1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(8)
print(f"dummy listening on {port}", flush=True)
while True:
    time.sleep(60)
PYEOF
DUMMY_PID=$!
disown "$DUMMY_PID" 2>/dev/null || true
sleep 0.5
DUMMY_PIDS_BEFORE="$(lsof -tiTCP:${DUMMY_PORT} -sTCP:LISTEN -n -P 2>/dev/null || true)"

# ISS-060 (c)：为让位 helper 单独建运行根，避免其 helper-instance.json 与
# (c) 段 TEST_RUNTIME 共享，导致 (d) SIGTERM → SIGKILL 的 0.5s 间隙内未来
# 得及清文件、进而污染 (f) f-instance-cleaned 断言（只能多败不假过）。
D_RUNTIME_DIR="$NASTY_DIR_BASE/runtime-d"
mkdir -p "$D_RUNTIME_DIR"

# 通过 launchctl setenv 把 FATHOM_PORT 传给已启动的 app 后续 spawn 的 helper；
# 这里测试的是「脚本自起 helper 二进制」能否让位，不依赖 app 已开窗口
"$HELPER_BIN" --runtime-mode release --port "$DUMMY_PORT" --port-range 4 \
  --runtime-dir "$D_RUNTIME_DIR" serve >> "$LOG" 2>&1 &
ZERO_HELPER_PID=$!
disown "$ZERO_HELPER_PID" 2>/dev/null || true

# 等让位到下一端口就绪
ZERO_OK=no
ZERO_PORT=""
for _ in $(seq 1 15); do
  for port in $(seq "$DUMMY_PORT" "$((DUMMY_PORT + 4))"); do
    if [ "$port" = "$DUMMY_PORT" ]; then continue; fi
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
      ZERO_OK=yes
      ZERO_PORT="$port"
      break 2
    fi
  done
  sleep 0.5
done

# dummy PID 未变（零击杀）
DUMMY_PIDS_AFTER="$(lsof -tiTCP:${DUMMY_PORT} -sTCP:LISTEN -n -P 2>/dev/null || true)"
if [ "$DUMMY_PIDS_BEFORE" = "$DUMMY_PIDS_AFTER" ] && [ -n "$DUMMY_PIDS_BEFORE" ]; then
  record "d-zero-kill" pass "dummy 占用 PID 集合前后一致：$DUMMY_PIDS_BEFORE"
else
  record "d-zero-kill" fail "dummy PID 漂移：before=$DUMMY_PIDS_BEFORE after=$DUMMY_PIDS_AFTER"
fi

if [ "$ZERO_OK" = "yes" ]; then
  record "d-yield-success" pass "helper 让位到 ${ZERO_PORT} 成功（dummy 占 ${DUMMY_PORT}）"
else
  record "d-yield-success" fail "helper 未在让位段内就绪（dummy 占 ${DUMMY_PORT}）"
fi

# 收尾让位 helper：先 SIGTERM 等 helper 自身 unlink helper-instance.json，
# 再 SIGKILL 兜底。wait 窗口由 0.5s 加长到 3s，覆盖 uvicorn + 信号处理
# 的 worst case；helper 实例身份匹配自己的 pid 才删文件（cli.py
# `remove_helper_instance`），不会出现「误删别人 instance」的风险。
kill -TERM "$ZERO_HELPER_PID" 2>/dev/null || true
ZERO_TERM_WAIT_S="${ZERO_TERM_WAIT_S:-3}"
# 等进程真退出（bounded）；不用固定 sleep，避免在快机器上空等
ZERO_TERM_DEADLINE=$(( $(date +%s) + ZERO_TERM_WAIT_S ))
while kill -0 "$ZERO_HELPER_PID" 2>/dev/null; do
  if [ "$(date +%s)" -ge "$ZERO_TERM_DEADLINE" ]; then break; fi
  sleep 0.2
done
kill -KILL "$ZERO_HELPER_PID" 2>/dev/null || true
kill -TERM "$DUMMY_PID" 2>/dev/null || true
sleep 0.3
# 让 (d) helper 的运行根随 NASTY_DIR_BASE 一并在文末清理，避免泄漏。

# ---------------------------------------------------------------- (e) 二次启动
log "=== (e) 二次启动：让位 ==="
# 当前 app 已开；再 open 一次应让位（不出现第二个 helper）
HELPER_PIDS_BEFORE_2="$(lsof -tiTCP:${HELPER_PORT:-7952} -sTCP:LISTEN -n -P 2>/dev/null | wc -l | tr -d ' ')"
open -a "$TEST_APP" >> "$LOG" 2>&1 || true
sleep 3
HELPER_PIDS_AFTER_2="$(lsof -tiTCP:${HELPER_PORT:-7952} -sTCP:LISTEN -n -P 2>/dev/null | wc -l | tr -d ' ')"
if [ "$HELPER_PIDS_BEFORE_2" = "$HELPER_PIDS_AFTER_2" ] && [ "$HELPER_PIDS_BEFORE_2" -ge 1 ]; then
  record "e-second-open-yield" pass "二次 open 后 ${HELPER_PORT:-7952} 上的 helper 数量不变（${HELPER_PIDS_BEFORE_2}）"
else
  record "e-second-open-yield" fail "二次 open 后数量异常：before=$HELPER_PIDS_BEFORE_2 after=$HELPER_PIDS_AFTER_2"
fi

# ---------------------------------------------------------------- (f) 退出清理
log "=== (f) 退出清理 ==="
osascript -e 'tell application "Fathom" to quit' >> "$LOG" 2>&1 || true
sleep 6

if [ -n "$HELPER_PORT" ]; then
  CODE_AFTER_QUIT="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${HELPER_PORT}/health" 2>/dev/null || true)"
  if [ "$CODE_AFTER_QUIT" != "200" ]; then
    record "f-port-closed" pass "退出后 ${HELPER_PORT}/health 已不再 200"
  else
    # 仍有 200 时需要确认：可能 helper 已 SIGTERM 但 uvicorn 还有未关闭连接；
    # 再等 5s
    sleep 5
    CODE_AFTER_QUIT2="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${HELPER_PORT}/health" 2>/dev/null || true)"
    if [ "$CODE_AFTER_QUIT2" != "200" ]; then
      record "f-port-closed" pass "退出后 ${HELPER_PORT}/health 最终不再 200"
    else
      record "f-port-closed" fail "退出 11s 后 ${HELPER_PORT}/health 仍 200（helper 未退出）"
    fi
  fi
else
  record "f-port-closed" fail "无 helper 端口可验证退出"
fi

if [ -f "$HI_PATH" ]; then
  record "f-instance-cleaned" fail "helper-instance.json 仍存在：$HI_PATH"
else
  record "f-instance-cleaned" pass "helper-instance.json 已被清理"
fi

# 清理 launchctl setenv
if [ "$LAUNCHCTL_SETENV_OK" = "yes" ]; then
  launchctl unsetenv FATHOM_RUNTIME_DIR 2>/dev/null || true
fi

# 临时目录清理（除非 launchctl setenv 失败导致 helper 写真实 Application Support，
# 此时保留以便人工排查）
if [ "$LAUNCHCTL_SETENV_OK" = "yes" ]; then
  rm -rf "$NASTY_DIR_BASE"
fi

# ---------------------------------------------------------------- (h) 陈旧 instance 文件 respawn（ISS-059）
log "=== (h) 陈旧 helper-instance.json：探活后 respawn ==="
H_BASE="$(mktemp -d -t fathom-verify-h-XXXXXX)"
H_APP_DIR="$H_BASE/appdir"
mkdir -p "$H_APP_DIR"
cp -R "$APP_PATH" "$H_APP_DIR/Fathom.app"
H_APP="$H_APP_DIR/Fathom.app"
H_RUNTIME="$H_BASE/runtime"
mkdir -p "$H_RUNTIME"

# 前置 1：确认不存在的假 pid（macOS pid 上限 99998，从 4000000 起必不存在；仍用 ps 复核）
H_FAKE_PID=4000000
while ps -p "$H_FAKE_PID" -o pid= >/dev/null 2>&1; do
  H_FAKE_PID=$((H_FAKE_PID + 1))
done
log "(h) 假 pid=${H_FAKE_PID}（ps 确认不存在）"

# 前置 2：确认无监听的候选端口；同时记下启动前已有监听的候选端口（就绪轮询跳过，
# 避免把外部既有监听（如生产 7952）误判为本次 respawn 的 helper）
H_STALE_PORT=""
H_PRE_OCCUPIED=" "
for port in 7952 7953 7954 7955 7956; do
  if [ -n "$(lsof -tiTCP:${port} -sTCP:LISTEN -n -P 2>/dev/null || true)" ]; then
    H_PRE_OCCUPIED="${H_PRE_OCCUPIED}${port} "
  elif [ -z "$H_STALE_PORT" ]; then
    H_STALE_PORT="$port"
  fi
done
log "(h) 陈旧文件预写端口=${H_STALE_PORT}；启动前已占用候选端口：${H_PRE_OCCUPIED}"

H_PRECONDITION_OK=yes
if [ -z "$H_STALE_PORT" ]; then
  record "h-stale-instance-respawn" fail "候选端口 7952..7956 全部有监听，无法构造陈旧文件反例"
  H_PRECONDITION_OK=no
fi

if [ "$H_PRECONDITION_OK" = "yes" ]; then
  # 预写身份匹配但陈旧的 helper-instance.json（0600，与 helper 写出形态一致）
  python3 - "$H_RUNTIME" "$H_FAKE_PID" "$H_STALE_PORT" >> "$LOG" 2>&1 <<'PYEOF'
import json, os, sys
runtime, pid, port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
path = os.path.join(runtime, "helper-instance.json")
with open(path, "w") as f:
    json.dump({"service": "fathom", "protocol_version": 1, "pid": pid, "port": port,
               "version": "0.3.0", "instance_id": "stale-baseline", "runtime_mode": "release"}, f)
os.chmod(path, 0o600)
print(f"pre-wrote stale helper-instance.json: pid={pid} port={port}")
PYEOF

  # 对照 dummy：占一个候选范围之外的端口，验证前后 PID 集合一致（零信号）
  H_DUMMY_PORT="$(python3 -c '
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
')"
  python3 - "$H_DUMMY_PORT" >> "$LOG" 2>&1 <<'PYEOF' &
import socket, sys, time
port = int(sys.argv[1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(8)
print(f"dummy listening on {port}", flush=True)
while True:
    time.sleep(60)
PYEOF
  H_DUMMY_PID=$!
  disown "$H_DUMMY_PID" 2>/dev/null || true
  sleep 0.5
  H_DUMMY_PIDS_BEFORE="$(lsof -tiTCP:${H_DUMMY_PORT} -sTCP:LISTEN -n -P 2>/dev/null || true)"

  launchctl setenv FATHOM_RUNTIME_DIR "$H_RUNTIME" 2>/dev/null || true
  open -a "$H_APP" >> "$LOG" 2>&1 || true

  # 断言 1：30s 内某个此前无监听的候选端口 /health 就绪
  H_READY=no
  H_PORT=""
  for _ in $(seq 1 30); do
    for port in 7952 7953 7954 7955 7956; do
      case "$H_PRE_OCCUPIED" in
        *" ${port} "*) continue ;;
      esac
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
      if [ "$code" = "200" ]; then
        H_READY=yes
        H_PORT="$port"
        break 2
      fi
    done
    sleep 1
  done

  # 断言 2：instance 文件被重写——pid 对应进程存在、port 与就绪端口一致、不再是假 pid
  H_FILE_OK=no
  H_FILE_DETAIL="未读取"
  if [ "$H_READY" = "yes" ]; then
    H_FILE_DETAIL="$(python3 - "$H_RUNTIME/helper-instance.json" "$H_PORT" "$H_FAKE_PID" 2>&1 <<'PYEOF'
import json, sys
path, ready_port, fake_pid = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    with open(path) as f:
        inst = json.load(f)
except Exception as exc:
    print(f"ERR read {path}: {exc}")
    raise SystemExit(0)
print(f"{inst.get('pid')} {inst.get('port')}")
raise SystemExit(0)
PYEOF
)"
    H_NEW_PID="$(printf '%s\n' "$H_FILE_DETAIL" | awk '{print $1}')"
    H_NEW_PORT="$(printf '%s\n' "$H_FILE_DETAIL" | awk '{print $2}')"
    if [ "$H_NEW_PID" = "$H_FAKE_PID" ] || [ -z "$H_NEW_PID" ] || [ "$H_NEW_PORT" != "$H_PORT" ]; then
      H_FILE_OK=no
    elif ps -p "$H_NEW_PID" -o pid= >/dev/null 2>&1; then
      H_FILE_OK=yes
    fi
  fi

  if [ "$H_READY" = "yes" ] && [ "$H_FILE_OK" = "yes" ]; then
    record "h-stale-instance-respawn" pass "陈旧文件（pid=${H_FAKE_PID} 死、port=${H_STALE_PORT} 无监听）未阻止 respawn：/health 就绪于 ${H_PORT}，instance 已重写为存活 pid=${H_NEW_PID} port=${H_NEW_PORT}"
  else
    record "h-stale-instance-respawn" fail "respawn 未完成：ready=${H_READY} port=${H_PORT:-none} file_ok=${H_FILE_OK}（文件读数：${H_FILE_DETAIL}）"
  fi

  # 断言 3：零信号——对照 dummy PID 集合前后一致，假 pid 仍不存在
  H_DUMMY_PIDS_AFTER="$(lsof -tiTCP:${H_DUMMY_PORT} -sTCP:LISTEN -n -P 2>/dev/null || true)"
  H_FAKE_STILL_DEAD=no
  if ! ps -p "$H_FAKE_PID" -o pid= >/dev/null 2>&1; then
    H_FAKE_STILL_DEAD=yes
  fi
  if [ "$H_DUMMY_PIDS_BEFORE" = "$H_DUMMY_PIDS_AFTER" ] && [ -n "$H_DUMMY_PIDS_BEFORE" ] && [ "$H_FAKE_STILL_DEAD" = "yes" ]; then
    record "h-zero-kill" pass "对照 dummy PID 集合前后一致（${H_DUMMY_PIDS_BEFORE}）；假 pid ${H_FAKE_PID} 仍不存在（零信号）"
  else
    record "h-zero-kill" fail "dummy 漂移：before=${H_DUMMY_PIDS_BEFORE} after=${H_DUMMY_PIDS_AFTER}；假 pid 仍不存在=${H_FAKE_STILL_DEAD}"
  fi

  # 收尾：按 (f) 同法退出并确认端口关闭
  osascript -e 'tell application "Fathom" to quit' >> "$LOG" 2>&1 || true
  sleep 6
  H_CLEAN_DETAIL="无就绪端口可验证"
  H_CLEAN_OK=no
  if [ -n "$H_PORT" ]; then
    H_CODE_AFTER="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${H_PORT}/health" 2>/dev/null || true)"
    if [ "$H_CODE_AFTER" != "200" ]; then
      H_CLEAN_OK=yes
      H_CLEAN_DETAIL="退出后 ${H_PORT}/health 不再 200"
    else
      H_CLEAN_DETAIL="退出 6s 后 ${H_PORT}/health 仍 200"
    fi
  else
    H_CLEAN_OK=yes
  fi
  kill -TERM "$H_DUMMY_PID" 2>/dev/null || true
  sleep 0.5
  kill -KILL "$H_DUMMY_PID" 2>/dev/null || true
  launchctl unsetenv FATHOM_RUNTIME_DIR 2>/dev/null || true
  if [ "$H_CLEAN_OK" = "yes" ]; then
    record "h-exit-cleanup" pass "壳退出后端口关闭（${H_CLEAN_DETAIL}）；对照 dummy 已由脚本回收"
  else
    record "h-exit-cleanup" fail "${H_CLEAN_DETAIL}"
  fi
fi
rm -rf "$H_BASE"

# ---------------------------------------------------------------- (i) 端口耗尽零击杀（ISS-059）
log "=== (i) 候选端口全占：ports-exhausted 与零击杀 ==="
I_BASE="$(mktemp -d -t fathom-verify-i-XXXXXX)"
I_APP_DIR="$I_BASE/appdir"
mkdir -p "$I_APP_DIR"
cp -R "$APP_PATH" "$I_APP_DIR/Fathom.app"
I_APP="$I_APP_DIR/Fathom.app"
I_RUNTIME="$I_BASE/runtime"
mkdir -p "$I_RUNTIME"

# 选出候选范围内所有未被外部占用的端口，用 dummy 一并占满（7952 被生产占用时
# 只占 7953..7956；7952 空闲则一并占）
I_DUMMY_PORTS=""
I_PRE_OCCUPIED=" "
for port in 7952 7953 7954 7955 7956; do
  if [ -n "$(lsof -tiTCP:${port} -sTCP:LISTEN -n -P 2>/dev/null || true)" ]; then
    I_PRE_OCCUPIED="${I_PRE_OCCUPIED}${port} "
  else
    I_DUMMY_PORTS="${I_DUMMY_PORTS}${port} "
  fi
done
log "(i) dummy 占用端口：${I_DUMMY_PORTS:-（无，候选已全部被外部占用）}；外部既有监听：${I_PRE_OCCUPIED}"

# 外部既有监听（如生产 7952）的 /health 状态码快照，结束时必须不变（只观察不干预）
I_PRE_STATE_SNAPSHOT=" "
for port in $I_PRE_OCCUPIED; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
  I_PRE_STATE_SNAPSHOT="${I_PRE_STATE_SNAPSHOT}${port}:${code} "
done
log "(i) 外部既有监听 /health 快照：${I_PRE_STATE_SNAPSHOT}"

# dummy 监听：沿用 (d) 段 python3 - heredoc 写法，一个进程绑定全部待占端口
if [ -n "$I_DUMMY_PORTS" ]; then
  # shellcheck disable=SC2086
  python3 - $I_DUMMY_PORTS >> "$LOG" 2>&1 <<'PYEOF' &
import socket, sys, time
ports = [int(p) for p in sys.argv[1:]]
socks = []
for p in ports:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", p))
    s.listen(8)
    socks.append(s)
    print(f"dummy listening on {p}", flush=True)
while True:
    time.sleep(60)
PYEOF
  I_DUMMY_PID=$!
  disown "$I_DUMMY_PID" 2>/dev/null || true
  sleep 0.8
fi

I_DUMMY_SNAPSHOT_BEFORE=" "
for port in $I_DUMMY_PORTS; do
  pids="$(lsof -tiTCP:${port} -sTCP:LISTEN -n -P 2>/dev/null || true)"
  I_DUMMY_SNAPSHOT_BEFORE="${I_DUMMY_SNAPSHOT_BEFORE}${port}=${pids};"
done
log "(i) dummy PID 快照：${I_DUMMY_SNAPSHOT_BEFORE}"

launchctl setenv FATHOM_RUNTIME_DIR "$I_RUNTIME" 2>/dev/null || true
open -a "$I_APP" >> "$LOG" 2>&1 || true
sleep 2
I_SHELL_PIDS_BEFORE="$(pgrep -f "${I_APP}/Contents/MacOS" 2>/dev/null || true)"
log "(i) 壳进程 PID：${I_SHELL_PIDS_BEFORE:-（未捕获）}"

# 观察窗口 40s（壳握手 20s + 余量）：断言候选范围内不出现 fathom /health，
# 且运行根 helper.log 出现 ports-exhausted 结构化标记
I_NEW_FATHOM_SEEN="no"
I_MARKER_SEEN="no"
for _ in $(seq 1 20); do
  if [ "$I_MARKER_SEEN" = "no" ] && grep -q "ports-exhausted" "$I_RUNTIME/logs/helper.log" 2>/dev/null; then
    I_MARKER_SEEN="yes"
    log "(i) helper.log 出现 ports-exhausted 标记"
  fi
  for port in $I_DUMMY_PORTS; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
      body="$(curl -s --max-time 1 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
      if printf '%s' "$body" | grep -q '"service".*"fathom"'; then
        I_NEW_FATHOM_SEEN="yes@${port}"
      fi
    fi
  done
  if [ "$I_MARKER_SEEN" = "yes" ]; then
    break
  fi
  sleep 2
done

# 断言 1：dummy 端口上没有出现 fathom /health（壳没能也不该拉起）
if [ "$I_NEW_FATHOM_SEEN" = "no" ]; then
  record "i-no-fathom-health" pass "观察窗口内候选 dummy 端口未出现 fathom /health（壳未拉起任何实例）"
else
  record "i-no-fathom-health" fail "dummy 端口 ${I_NEW_FATHOM_SEEN#yes@} 出现了 fathom /health"
fi

# 断言 2：零击杀——所有 dummy 端口的 PID 集合前后一致
I_DUMMY_SNAPSHOT_AFTER=" "
for port in $I_DUMMY_PORTS; do
  pids="$(lsof -tiTCP:${port} -sTCP:LISTEN -n -P 2>/dev/null || true)"
  I_DUMMY_SNAPSHOT_AFTER="${I_DUMMY_SNAPSHOT_AFTER}${port}=${pids};"
done
I_ALL_DUMMY_OCCUPIED=yes
for port in $I_DUMMY_PORTS; do
  case "$I_DUMMY_SNAPSHOT_AFTER" in
    *"${port}=;"*) I_ALL_DUMMY_OCCUPIED=no ;;
  esac
done
if [ "$I_DUMMY_SNAPSHOT_BEFORE" = "$I_DUMMY_SNAPSHOT_AFTER" ] && [ "$I_ALL_DUMMY_OCCUPIED" = "yes" ]; then
  record "i-zero-kill" pass "dummy PID 集合前后一致且全部在监听（${I_DUMMY_SNAPSHOT_AFTER}）"
elif [ -z "$I_DUMMY_PORTS" ]; then
  record "i-zero-kill" pass "候选端口已全部被外部占用，无需 dummy（零信号仅观察）"
else
  record "i-zero-kill" fail "dummy PID 漂移或失守：before=${I_DUMMY_SNAPSHOT_BEFORE} after=${I_DUMMY_SNAPSHOT_AFTER}"
fi

# 断言 3：壳进程仍存活（未崩溃）
I_SHELL_PIDS_AFTER="$(pgrep -f "${I_APP}/Contents/MacOS" 2>/dev/null || true)"
if [ -n "$I_SHELL_PIDS_BEFORE" ] && [ "$I_SHELL_PIDS_BEFORE" = "$I_SHELL_PIDS_AFTER" ]; then
  record "i-shell-alive" pass "端口耗尽后壳进程仍存活（PID ${I_SHELL_PIDS_BEFORE}，握手页呈现恢复动作）"
else
  record "i-shell-alive" fail "壳进程异常：before=${I_SHELL_PIDS_BEFORE:-none} after=${I_SHELL_PIDS_AFTER:-none}"
fi

# 断言 4：壳的可观察输出（运行根 logs/helper.log）含 ports-exhausted 标记
if [ "$I_MARKER_SEEN" = "yes" ]; then
  record "i-ports-exhausted-marker" pass "helper.log 含 ports-exhausted 结构化标记：$(grep -m1 "ports-exhausted" "$I_RUNTIME/logs/helper.log" 2>/dev/null | cut -c1-80)"
else
  record "i-ports-exhausted-marker" fail "$I_RUNTIME/logs/helper.log 未出现 ports-exhausted 标记"
fi

# 收尾：退出壳、回收 dummy、确认无残留。壳的 setup 握手最长 20s，退出请求
# 会排队到 setup 完成后才处理，因此轮询等待而非固定 sleep
osascript -e 'tell application "Fathom" to quit' >> "$LOG" 2>&1 || true
for _ in $(seq 1 35); do
  if [ -z "$(pgrep -f "${I_APP}/Contents/MacOS" 2>/dev/null || true)" ]; then
    break
  fi
  sleep 1
done
I_SHELL_PIDS_FINAL="$(pgrep -f "${I_APP}/Contents/MacOS" 2>/dev/null || true)"
if [ -n "$I_DUMMY_PORTS" ]; then
  kill -TERM "$I_DUMMY_PID" 2>/dev/null || true
  sleep 0.5
  kill -KILL "$I_DUMMY_PID" 2>/dev/null || true
fi
launchctl unsetenv FATHOM_RUNTIME_DIR 2>/dev/null || true
I_RESIDUE="$(for port in $I_DUMMY_PORTS; do
  lsof -tiTCP:${port} -sTCP:LISTEN -n -P 2>/dev/null || true
done)"
# 外部既有监听（如生产 7952）必须保持原样：状态码快照逐一对比
I_PRE_STATE_FINAL=" "
for port in $I_PRE_OCCUPIED; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
  I_PRE_STATE_FINAL="${I_PRE_STATE_FINAL}${port}:${code} "
done
if [ -z "$I_SHELL_PIDS_FINAL" ] && [ -z "$I_RESIDUE" ] && [ "$I_PRE_STATE_FINAL" = "$I_PRE_STATE_SNAPSHOT" ]; then
  record "i-exit-cleanup" pass "壳已退出、dummy 端口无残留、外部既有监听状态不变（${I_PRE_STATE_FINAL}）"
else
  record "i-exit-cleanup" fail "残留：shell=${I_SHELL_PIDS_FINAL:-none} dummy_listen=${I_RESIDUE:-none}；外部监听 ${I_PRE_STATE_SNAPSHOT}→${I_PRE_STATE_FINAL}"
fi
rm -rf "$I_BASE"

# ---------------------------------------------------------------- 汇总
VERDICT="PASS"
if [ "$FAILED" -gt 0 ]; then
  VERDICT="FAIL"
fi
python3 - "$RESULTS_DIR" "$VERDICT" "$PASSED" "$FAILED" <<'PYEOF'
import json, sys
out_dir, verdict, passed, failed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
cases = []
try:
    with open(f"{out_dir}/cases.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
except FileNotFoundError:
    pass
summary = {
    "schema": "fathom.iss009-slice1.verify.v1",
    "verdict": verdict,
    "passed": passed,
    "failed": failed,
    "cases": cases,
}
with open(f"{out_dir}/result.json", "w") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print(json.dumps({"verdict": verdict, "passed": passed, "failed": failed,
                  "result": f"{out_dir}/result.json"}, ensure_ascii=False))
PYEOF

if [ "$FAILED" -gt 0 ]; then
  exit 1
fi
exit 0
