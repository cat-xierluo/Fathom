#!/usr/bin/env bash
# ISS-031 API/浏览器 39 项检查入口（CI 与本地同一断言口径）。
#
# 本地等价命令（已验证 39/39，复用本机已有 Playwright 与浏览器缓存）：
#   node scripts/verify_api_security.cjs
# CI 前置（workflow 内完成，此处只做兜底）：
#   - FATHOM_PYTHON 指向锁定 venv；脚本用一个 exec 包装器把它桥接到
#     .runtime/bin/python
#     （verify_api_security.cjs 固定从该路径启动夹具服务，仓库内
#     .runtime 是本机符号链接，不入库，CI 克隆中不存在）。不能把 venv
#     的 python 二进制再软链到该路径：CPython 会按软链位置寻找
#     pyvenv.cfg，进而退回 runner 系统环境；
#   - NODE_PATH 指向安装了 playwright@<锁定版本> 的 node_modules；
#   - PW_INSTALL=1 且提供 PLAYWRIGHT_BIN 时下载 chromium（仅 CI 冷环境）。
#
# 断言口径：结果 JSON 必须 ok=true、failed=0、passed==EXPECTED_BROWSER_PASSED
# （默认 39）；verify 脚本自身任一检查失败都会以非零退出（pipefail 兜底）。
set -euo pipefail
cd "$(dirname "$0")/.."

expected="${EXPECTED_BROWSER_PASSED:-39}"

if [ ! -x .runtime/bin/python ]; then
  requested_python="${FATHOM_PYTHON:-python3}"
  target="$(command -v "$requested_python")" || {
    echo "缺少可执行解释器: $requested_python" >&2
    exit 1
  }
  mkdir -p .runtime/bin
  export FATHOM_PYTHON="$target"
  cat > .runtime/bin/python <<'EOF'
#!/bin/sh
exec "$FATHOM_PYTHON" "$@"
EOF
  chmod 755 .runtime/bin/python
fi

# 回归门禁：隔离 PATH，确认硬编码的 .runtime 入口仍使用目标 venv，且
# fastapi 确实从该 venv 加载。旧的二次软链实现会在这里显示系统 prefix，
# 即使开发机的系统 Python 碰巧装了 fastapi 也不能通过。
target="${FATHOM_PYTHON:-.runtime/bin/python}"
target_prefix="$("$target" -c 'import os, sys; print(os.path.realpath(sys.prefix))')"
FATHOM_EXPECTED_PREFIX="$target_prefix" PATH="/usr/bin:/bin" \
  .runtime/bin/python -c '
import os
import sys
from pathlib import Path
import fastapi

expected = Path(os.environ["FATHOM_EXPECTED_PREFIX"]).resolve()
actual = Path(sys.prefix).resolve()
module = Path(fastapi.__file__).resolve()
assert actual == expected, f"fixture Python prefix 漂移: {actual} != {expected}"
assert module.is_relative_to(actual), f"fastapi 未从目标 venv 加载: {module}"
'

if [ "${PW_INSTALL:-0}" = "1" ]; then
  if [ -z "${PLAYWRIGHT_BIN:-}" ]; then
    echo "PW_INSTALL=1 需要同时设置 PLAYWRIGHT_BIN" >&2
    exit 1
  fi
  "$PLAYWRIGHT_BIN" install chromium
fi

out="$(mktemp)"
trap 'rm -f "$out"' EXIT
node scripts/verify_api_security.cjs | tee "$out"

node -e '
  const fs = require("fs");
  const j = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
  const expected = Number(process.argv[2]);
  if (j.ok !== true || j.failed !== 0 || j.passed !== expected) {
    console.error(`期望 ${expected} 项通过且 0 失败，实际 ok=${j.ok} passed=${j.passed} failed=${j.failed}`);
    process.exit(1);
  }
  console.log(`browser checks: ${j.passed} passed (expected ${expected})`);
' "$out" "$expected"
