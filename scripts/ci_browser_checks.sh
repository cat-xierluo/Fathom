#!/usr/bin/env bash
# ISS-031 API/浏览器 39 项检查入口（CI 与本地同一断言口径）。
#
# 本地等价命令（已验证 39/39，复用本机已有 Playwright 与浏览器缓存）：
#   node scripts/verify_api_security.cjs
# CI 前置（workflow 内完成，此处只做兜底）：
#   - FATHOM_PYTHON 指向锁定 venv；脚本把它挂到 .runtime/bin/python
#     （verify_api_security.cjs 固定从该路径启动夹具服务，仓库内
#     .runtime 是本机符号链接，不入库，CI 克隆中不存在）；
#   - NODE_PATH 指向安装了 playwright@<锁定版本> 的 node_modules；
#   - PW_INSTALL=1 且提供 PLAYWRIGHT_BIN 时下载 chromium（仅 CI 冷环境）。
#
# 断言口径：结果 JSON 必须 ok=true、failed=0、passed==EXPECTED_BROWSER_PASSED
# （默认 39）；verify 脚本自身任一检查失败都会以非零退出（pipefail 兜底）。
set -euo pipefail
cd "$(dirname "$0")/.."

expected="${EXPECTED_BROWSER_PASSED:-39}"

if [ ! -x .runtime/bin/python ]; then
  target="${FATHOM_PYTHON:-python3}"
  mkdir -p .runtime/bin
  ln -s "$(command -v "$target")" .runtime/bin/python
fi

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
