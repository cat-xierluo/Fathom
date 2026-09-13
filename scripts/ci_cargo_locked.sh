#!/usr/bin/env bash
# ISS-031 Cargo 锁定离线构建入口（CI 与本地同一命令口径）。
#
# 本地等价命令（已验证通过，依赖本机 Cargo 缓存）：
#   cargo build --locked --offline --manifest-path apps/desktop/src-tauri/Cargo.toml
# CI 冷缓存：设 CARGO_PREFETCH=1，先 cargo fetch --locked 把 Cargo.lock 的
# 精确版本拉进本地缓存，再以 --offline 构建，证明构建只依赖锁文件。
#
# 断言口径：Cargo.lock 必须入库存在；--locked 下锁与 Cargo.toml 不一致、
# 或离线缺 crate 时 cargo 以非零退出，脚本随之失败。
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
