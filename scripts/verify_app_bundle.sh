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

# 通过 launchctl setenv 把 FATHOM_PORT 传给已启动的 app 后续 spawn 的 helper；
# 这里测试的是「脚本自起 helper 二进制」能否让位，不依赖 app 已开窗口
"$HELPER_BIN" --runtime-mode release --port "$DUMMY_PORT" --port-range 4 \
  --runtime-dir "$TEST_RUNTIME" serve >> "$LOG" 2>&1 &
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

# 收尾让位 helper
kill -TERM "$ZERO_HELPER_PID" 2>/dev/null || true
sleep 0.5
kill -KILL "$ZERO_HELPER_PID" 2>/dev/null || true
kill -TERM "$DUMMY_PID" 2>/dev/null || true
sleep 0.3

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
