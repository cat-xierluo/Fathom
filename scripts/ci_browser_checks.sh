#!/usr/bin/env bash
# ISS-031 API/浏览器 39 项检查入口（CI 与本地同一断言口径）；
# ISS-100 追加前端功能回归（verify_frontend_refresh，默认 160 项）——
# 同一 job 内两套检查依次执行，日志各自可辨认，任一失败 job 即红。
#
# 本地等价命令（39/39 沿用 ISS-031 已验证口径；refresh 基线 160 项为
# ISS-102 后的本机已验证通过数，CI 首跑以计数门禁实测对齐）：
#   node scripts/verify_api_security.cjs
#   node scripts/verify_frontend_refresh.cjs
# CI 前置（workflow 内完成，此处只做兜底）：
#   - FATHOM_PYTHON 指向锁定 venv；脚本用一个 exec 包装器把它桥接到
#     .runtime/bin/python
#     （verify_api_security.cjs 固定从该路径启动夹具服务，仓库内
#     .runtime 是本机符号链接，不入库，CI 克隆中不存在）。不能把 venv
#     的 python 二进制再软链到该路径：CPython 会按软链位置寻找
#     pyvenv.cfg，进而退回 runner 系统环境。verify_frontend_refresh.cjs
#     夹具为纯 Node（无 Python 依赖），不需要该包装器；
#   - NODE_PATH 指向安装了 playwright@<锁定版本> 的 node_modules（两套
#     检查共用）；
#   - PW_INSTALL=1 且提供 PLAYWRIGHT_BIN 时下载 chromium（仅 CI 冷环境）。
#
# 断言口径：两套结果 JSON 均必须 ok=true、failed=0、passed 等于各自期望
# （EXPECTED_BROWSER_PASSED 默认 39；EXPECTED_REFRESH_PASSED 默认 160）；
# verify 脚本自身任一检查失败都会以非零退出（pipefail 直通，不走门禁
# 兜底），计数门禁只拦空跑与静默漂移。
set -euo pipefail
cd "$(dirname "$0")/.."

expected="${EXPECTED_BROWSER_PASSED:-39}"
expected_refresh="${EXPECTED_REFRESH_PASSED:-160}"

if [ ! -x .runtime/bin/python ]; then
  requested_python="${FATHOM_PYTHON:-python3}"
  target="$(command -v "$requested_python")" || {
    echo "缺少可执行解释器: $requested_python" >&2
    exit 1
  }
  mkdir -p .runtime/bin
  {
    printf '#!/bin/bash\n'
    printf 'exec %q "$@"\n' "$target"
  } > .runtime/bin/python
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
refresh_out="$(mktemp)"
trap 'rm -f "$out" "$refresh_out"' EXIT
node scripts/verify_api_security.cjs | tee "$out"

# 两套检查同一门禁：结果 JSON 必须 ok=true、failed=0、passed==期望，
# 日志行带各自标签（browser checks / frontend refresh）便于 CI 辨认。
assert_result_json() {
  node -e '
    const fs = require("fs");
    const j = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    const expected = Number(process.argv[2]);
    if (j.ok !== true || j.failed !== 0 || j.passed !== expected) {
      console.error(`期望 ${expected} 项通过且 0 失败，实际 ok=${j.ok} passed=${j.passed} failed=${j.failed}`);
      process.exit(1);
    }
    console.log(`${process.argv[3]}: ${j.passed} passed (expected ${expected})`);
  ' "$1" "$2" "$3"
}
assert_result_json "$out" "$expected" "browser checks"

# ISS-100：前端功能回归接入。verify_frontend_refresh.cjs 自带纯 Node 合成
# 夹具与前端静态文件服务，随机端口、无 Python 依赖；playwright 走同一
# NODE_PATH，chromium 复用 PW_INSTALL 已装好的那份。任一检查失败脚本
# 自身非零退出，pipefail 直通判红。
node scripts/verify_frontend_refresh.cjs | tee "$refresh_out"
assert_result_json "$refresh_out" "$expected_refresh" "frontend refresh"
