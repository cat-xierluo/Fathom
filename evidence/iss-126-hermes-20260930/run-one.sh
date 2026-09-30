#!/usr/bin/env bash
# ISS-126 hermes 轮单次实验驱动：隔离 mktemp cwd + stream-json + 运行前后文件对照。
# 用法: run-one.sh <实验编号> <超时秒> <payload文件> [额外 hermes 参数...]
# 环境固定为最小 {PATH, HOME}（与产品 runner 的 build_minimal_env 同构）。
# 外部看门狗仅兜底；hermes --run-budget 150 是主要运行时预算。
set -u
EV="$(cd "$(dirname "$0")" && pwd)"
ID="$1"; TIMEOUT="$2"; PAYLOAD_FILE="$3"; shift 3

TMPDIR_RUN=$(mktemp -d "/tmp/fathom-hermes-iss126-${ID}.XXXXXX")
echo "$TMPDIR_RUN" > "${EV}/tmpdir-${ID}.txt"

export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="$HOME"  # 保持真实 HOME：hermes 自身读 ~/.hermes/.env 认证，产品同形态

START=$(date +%s)
cd "$TMPDIR_RUN"  # 进程 cwd = 隔离目录：系统提示词中的 workspace 与任何写副作用都落在 tmpdir
/Users/maoking/.local/bin/hermes chat \
  -q "$(cat "$PAYLOAD_FILE")" \
  --format stream-json \
  --run-budget 150 \
  "$@" > "${EV}/${ID}.stdout" 2> "${EV}/${ID}.stderr" &
HPID=$!
# 看门狗：超时杀整组
(
  WAITED=0
  while kill -0 "$HPID" 2>/dev/null; do
    sleep 1; WAITED=$((WAITED+1))
    if [ "$WAITED" -ge "$TIMEOUT" ]; then
      kill -TERM -"$HPID" 2>/dev/null; sleep 5; kill -KILL -"$HPID" 2>/dev/null
      echo "watchdog_timeout=${TIMEOUT}s" >> "${EV}/${ID}.stdout.meta"
      exit 0
    fi
  done
) &
WATCHDOG=$!
wait "$HPID"; RC=$?
kill "$WATCHDOG" 2>/dev/null
END=$(date +%s)

{
  echo "exit=$RC elapsed=$((END-START))s tmpdir=${TMPDIR_RUN}"
  echo "--- flags after payload: $*"
  echo "--- cwd files AFTER run:"
  ls -la "$TMPDIR_RUN"
} >> "${EV}/${ID}.stdout.meta"
echo "exit=$RC elapsed=$((END-START))s tmpdir=${TMPDIR_RUN}"
