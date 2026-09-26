#!/usr/bin/env bash
# ISS-031 Cargo 锁定离线构建入口（CI 与本地同一命令口径）；
# ISS-100 追加单测接入——build 证明锁文件可复现，test 让 src 既有单测
# 真正随候选执行（不得用 build 冒充 test）。
#
# 本地等价命令（已验证通过，依赖本机 Cargo 缓存）：
#   cargo build --locked --offline --manifest-path apps/desktop/src-tauri/Cargo.toml
#   cargo test --locked --offline --manifest-path apps/desktop/src-tauri/Cargo.toml
# CI 冷缓存：设 CARGO_PREFETCH=1，先 cargo fetch --locked 把 Cargo.lock 的
# 精确版本（含 dev-dependencies）拉进本地缓存，再以 --offline 构建与
# 测试，证明两步都只依赖锁文件。
#
# 断言口径：Cargo.lock 必须入库存在；--locked 下锁与 Cargo.toml 不一致、
# 或离线缺 crate 时 cargo 以非零退出，脚本随之失败；test 任一用例失败
# 同样非零退出（pipefail 直通），通过数必须等于 EXPECTED_CARGO_PASSED
# （默认 60），空跑与静默漂移由计数门禁拦下。
set -euo pipefail
cd "$(dirname "$0")/.."

manifest="apps/desktop/src-tauri/Cargo.toml"
lock="apps/desktop/src-tauri/Cargo.lock"
[ -f "$manifest" ] || { echo "缺少 $manifest" >&2; exit 1; }
[ -f "$lock" ] || { echo "缺少 $lock（Cargo.lock 必须随仓库分发）" >&2; exit 1; }

if [ "${CARGO_PREFETCH:-0}" = "1" ]; then
  cargo fetch --locked --manifest-path "$manifest"
fi
cargo build --locked --offline --manifest-path "$manifest"
echo "cargo locked offline build: ok"

# ISS-100：单测接入。lib/bin/doctest 各段的 "test result:" 行求 passed
# 之和作为总数（只认该行首字样，避免误抓编译输出）；计数变化需同步
# EXPECTED_CARGO_PASSED 并在任务卡留证据。
expected_tests="${EXPECTED_CARGO_PASSED:-60}"
test_out="$(mktemp)"
trap 'rm -f "$test_out"' EXIT
cargo test --locked --offline --manifest-path "$manifest" | tee "$test_out"

passed="$(grep '^test result:' "$test_out" | grep -oE '[0-9]+ passed' | grep -oE '^[0-9]+' | awk '{s+=$1} END {print s+0}')"
if [ "$passed" != "$expected_tests" ]; then
  printf '%s\n' "cargo test 通过数 ${passed} != 期望 ${expected_tests}；计数变化需同步 EXPECTED_CARGO_PASSED 并在任务卡留证据" >&2
  exit 1
fi
printf 'cargo test: %s passed (expected %s)\n' "$passed" "$expected_tests"
