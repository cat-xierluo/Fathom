#!/usr/bin/env bash
# ISS-031 可复现 pytest 入口（CI 与本地同一断言口径）。
#
# 本地等价命令（已验证 209 passed）：
#   .runtime/bin/python -m pytest tests -q
# CI：FATHOM_PYTHON 指向 setup-python 锁定版本创建的 venv 解释器。
#
# 断言口径：
#   - fathom 源码必须来自当前工作区（TESTING 的防误测要求）；
#   - pytest 非零退出（failed/error/收集失败）经 pipefail 直接判失败；
#   - 通过数必须等于 EXPECTED_PYTEST_PASSED（默认 209）；计数变化必须
#     显式同步本默认值与任务证据，不允许静默漂移。
set -euo pipefail
cd "$(dirname "$0")/.."

expected="${EXPECTED_PYTEST_PASSED:-209}"
py="${FATHOM_PYTHON:-.runtime/bin/python}"

if [ ! -x "$py" ]; then
  echo "缺少可执行解释器: ${py}（本地用 .runtime/bin/python，CI 设 FATHOM_PYTHON）" >&2
  exit 1
fi

"$py" -c 'import os, fathom; src = os.path.realpath(fathom.__file__); here = os.path.realpath(os.getcwd()); assert src.startswith(here + os.sep), f"误测别处源码: {src}"'

out="$(mktemp)"
trap 'rm -f "$out"' EXIT
# pipefail：pytest 自身非零退出（失败/错误/收集失败）在此处直接判失败。
"$py" -m pytest tests -q | tee "$out"

# 只认最终摘要行的通过数；空库误跑（"no tests ran"）会因拿不到计数而失败。
passed="$(grep -oE '[0-9]+ passed' "$out" | tail -1 | grep -oE '[0-9]+')"
if [ "$passed" != "$expected" ]; then
  printf '%s\n' "pytest 通过数 ${passed} != 期望 ${expected}；计数变化需同步 EXPECTED_PYTEST_PASSED 并在任务卡留证据" >&2
  exit 1
fi
printf 'pytest: %s passed (expected %s)\n' "$passed" "$expected"
