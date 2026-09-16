#!/usr/bin/env bash
# ISS-068 壳层注册断言：插件已注册 + 权限已声明（不依赖 .app bundle）。
#
# 背景：ISS-002A 的前端深链走 `plugin:opener|open_url`，但
# verify_frontend_refresh.cjs 走 mock Tauri 桥，只断言前端发出的 cmd/args，
# 结构上无法覆盖「运行时插件是否真的注册」——76/76 全绿掩盖了 ISS-068。
# scripts/verify_app_bundle.sh 依赖完整 .app 产物，本环境无 bundle，故本脚本
# 用 tauri-build 的 ACL 落盘产物做等价断言。
#
# 前置条件：必须先构建一次，使 tauri-build 依据 capabilities 生成 ACL：
#   cargo build --locked --offline --manifest-path apps/desktop/src-tauri/Cargo.toml
# CI 中由 scripts/ci_cargo_locked.sh 先跑（同一离线锁定口径），本脚本紧随其后。
# 离线要求：--locked --offline（依赖本机/Cargo.lock 精确版本缓存）。
#
# 断言口径（三条互补，任一失败即红）：
#   (a) 【源码层·唯一能抓 .plugin() 缺失】src/lib.rs 的 run() 中必须出现
#       `.plugin(tauri_plugin_opener::init())`。这是本 issue 的根因面。
#   (b) 运行时 ACL：target/debug/build/*/out/acl-manifests.json 存在 opener 键
#       且其 permissions 含 allow-open-url（tauri-build 依据 capabilities 生成）。
#   (c) 入库能力清单：gen/schemas/capabilities.json 的 default.permissions
#       含 opener:allow-open-url（该文件已入库，可检出与 capabilities/default.json 漂移）。
#
# 为什么必须三条都断言（重要，勿简化）：
#   tauri-build 生成的 ACL 只由 Cargo.toml + capabilities/*.json 决定，
#   **无法**观察到 run() 是否真的调用 `.plugin()`。实测证据（ISS-068 证据段）：
#   删掉 `.plugin(tauri_plugin_opener::init())` 后重新 build，
#   acl-manifests.json 与 (c) 的 md5 逐字节不变，仅 (a)(b)(c) 全绿。
#   故 (b)(c) 只能证明「权限已声明 + 插件依赖在锁文件中」，**不能**证明
#   「插件已注册」；(a) 才是注册断言。Rust 侧另有等价强断言：
#   lib.rs tests::opener_plugin_name_matches_frontend_command_prefix
#   （绑定真实插件实例的 Plugin::name()，随 cargo test 门禁运行）。
#
# 为什么 ACL 仍值得断言：capability 引用未知权限标识符（拼错或插件未加入
# Cargo.toml）时 tauri-build 解析直接失败、cargo build 非零退出，(b) 因此
# 能拦截「权限声明写错」这类漂移。
set -euo pipefail
cd "$(dirname "$0")/.."

tauri_dir="apps/desktop/src-tauri"
expected_perm="opener:allow-open-url"
py="${FATHOM_PYTHON:-.runtime/bin/python}"

if [ ! -x "$py" ]; then
  echo "缺少可执行解释器: ${py}（本地用 .runtime/bin/python，CI 设 FATHOM_PYTHON）" >&2
  exit 1
fi

# (a) 源码层注册断言：唯一能抓 `.plugin()` 缺失的检查（见脚本头说明）。
lib_rs="apps/desktop/src-tauri/src/lib.rs"
[ -f "$lib_rs" ] || { echo "缺少 $lib_rs" >&2; exit 1; }
# 匹配 `.plugin(tauri_plugin_opener::init())`，容忍空白差异。
if ! grep -Eq '\.plugin\(\s*tauri_plugin_opener::init\(\)\s*\)' "$lib_rs"; then
  {
    echo "FAIL: ${lib_rs} 未见 .plugin(tauri_plugin_opener::init())"
    echo "      前端 plugin:opener|open_url 会因插件未注册而 invok 失败（ISS-068 根因）"
    echo "      注意：ACL/capabilities 断言抓不到此缺失，见脚本头说明。"
  } >&2
  exit 1
fi
echo "source check: run() 已注册 tauri_plugin_opener::init()"

# (b) 运行时 ACL：定位 tauri-build 输出的 acl-manifests.json（可能多份）。
acl_manifests="$(find "$tauri_dir/target/debug/build" -path '*fathom-desktop*/out/acl-manifests.json' 2>/dev/null || true)"
if [ -z "$acl_manifests" ]; then
  echo "未找到 acl-manifests.json；请先跑 cargo build --locked --offline（见脚本头前置条件）" >&2
  exit 1
fi

# (b) 入库能力清单。
schema="apps/desktop/src-tauri/gen/schemas/capabilities.json"
[ -f "$schema" ] || { echo "缺少 $schema（应由 tauri-build 生成并入库）" >&2; exit 1; }

# 单解释器断言两条，任一失败非零退出（-c 内 assert，失败即 SystemExit 非零）。
# 注意 PYTHONPATH：以 cwd=worktree 运行，避免主仓代码遮蔽（见 ISS-066 教训）。
ACL_MANIFESTS="$acl_manifests" SCHEMA="$schema" EXPECTED_PERM="$expected_perm" \
PYTHONPATH="$(pwd)" "$py" - <<'PY'
import json, os, sys

expected = os.environ["EXPECTED_PERM"]
failures = []

# ---- (b) 运行时 ACL ----
# find 可能返回多行（多个 hash 目录），逐行取路径。
manifests = [p for p in os.environ["ACL_MANIFESTS"].splitlines() if p.strip()]
acl_ok = False
acl_detail = []
for path in manifests:
    with open(path, encoding="utf-8") as fh:
        acl = json.load(fh)
    opener = acl.get("opener")
    if opener is None:
        acl_detail.append(f"{path}: 无 opener 键")
        continue
    perms = sorted(opener.get("permissions", {}))
    if "allow-open-url" in perms:
        acl_ok = True
        acl_detail.append(f"{path}: opener.permissions 含 allow-open-url（共 {len(perms)} 项）")
        break
    acl_detail.append(f"{path}: opener 存在但 permissions 无 allow-open-url -> {perms}")
if not acl_ok:
    failures.append("运行时 ACL 断言失败（权限未声明）：\n  " + "\n  ".join(acl_detail))

# ---- (c) 入库能力清单 ----
with open(os.environ["SCHEMA"], encoding="utf-8") as fh:
    schema = json.load(fh)
entries = schema if isinstance(schema, list) else schema.get("capabilities", [schema])
# gen/schemas/capabilities.json 顶层直接以 capability identifier 为键（非数组、
# 无 capabilities 包裹层），故先按 identifier 字段找，再回退按键名取值。
default = None
if isinstance(schema, dict):
    default = schema.get("default")
for entry in entries if isinstance(entries, list) else []:
    if isinstance(entry, dict) and entry.get("identifier") == "default":
        default = entry
        break
if default is None:
    failures.append(f"{os.environ['SCHEMA']}: 未找到 identifier=default 的 capability")
else:
    perms = default.get("permissions", [])
    if expected not in perms:
        failures.append(
            f"{os.environ['SCHEMA']}: default.permissions 不含 {expected} -> {perms}"
        )

if failures:
    for item in failures:
        print(f"FAIL: {item}", file=sys.stderr)
    sys.exit(1)

print(f"tauri opener registered: ok（源码注册 + ACL opener/allow-open-url + schema {expected}）")
PY
