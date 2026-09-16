#!/usr/bin/env bash
# ISS-068 壳层注册断言：插件已注册 + 权限已声明（不依赖 .app bundle）。
#
# 背景：ISS-002A 的前端深链走 `plugin:opener|open_url`，但
# verify_frontend_refresh.cjs 走 mock Tauri 桥，只断言前端发出的 cmd/args，
# 结构上无法覆盖「运行时插件是否真的注册」——76/76 全绿掩盖了 ISS-068。
# scripts/verify_app_bundle.sh 依赖完整 .app 产物，本环境无 bundle，故本脚本
# 用「源码检查 + tauri-build 的 ACL 落盘产物」组合断言。
#
# 前置条件：必须先构建一次，使 tauri-build 依据 capabilities 生成 ACL：
#   cargo build --locked --offline --manifest-path apps/desktop/src-tauri/Cargo.toml
# CI 中由 scripts/ci_cargo_locked.sh 先跑（同一离线锁定口径），本脚本紧随其后。
# 离线要求：--locked --offline（依赖本机/Cargo.lock 精确版本缓存）。
#
# 断言口径（四条互补，任一失败即红）：
#   (a) 【源码层·**唯一的注册护栏**】src/lib.rs 的 run() 中必须出现
#       `.plugin(tauri_plugin_opener::init())`。这是本 issue 的根因面。
#   (b) 运行时 ACL：target/debug/build/*/out/acl-manifests.json 存在 opener 键
#       且其 permissions 含 allow-open-url（tauri-build 依据 capabilities 生成）。
#   (c) 入库能力清单：gen/schemas/capabilities.json 的 default.permissions
#       含 opener:allow-open-url，**且该条目必须携带 URL scope**
#       （对象形式 `{"identifier":..., "allow":[{"url":"x-apple.systempreferences:*"}]}`），
#       且 scope.allow.url 含该模式。
#   (d) 防漂移：lib.rs 的 SCOPE_PATTERN 字面量必须等于 (c) 的模式（两份不能各说各话）。
#
# 为什么 (c) 必须校验 scope 而不只是权限标识符（ISS-068 二轮，PM 复核发现）：
#   `opener:allow-open-url` 上游定义只有 `commands.allow=["open_url"]`，**无 scope**。
#   上游判定 `Scope::is_url_allowed`（tauri-plugin-opener src/scope.rs）在 URL 维度
#   用 `glob::Pattern::matches`，空 allow 列表恒返回 false，命令随后返回
#   `Error::ForbiddenUrl`。故只声明权限标识符 = 命令可达但每次打开都被拒，
#   功能仍不可用。必须带 scope 且模式覆盖前端具体深链。
#
# 为什么必须四条都断言（重要，勿简化）：
#   tauri-build 生成的 ACL 只由 Cargo.toml + capabilities/*.json 决定，
#   **无法**观察到 run() 是否真的调用 `.plugin()`。实测证据（ISS-068 证据段）：
#   删掉 `.plugin(tauri_plugin_opener::init())` 后重新 build，
#   acl-manifests.json 与 (c) 的 md5 逐字节不变，(b)(c)(d) 仍然全绿。
#   故 (b)(c)(d) 只能证明「权限 + scope 已声明且自洽」，**不能**证明
#   「插件已注册」。
#
#   (a) 因此是**本仓库唯一的注册护栏**。Rust 侧没有任何等价物：
#   lib.rs tests::opener_plugin_name_matches_frontend_command_prefix 只断言
#   上游插件名合同，删掉 `.plugin()` 后该测试仍 1 passed（2026-09-17 实测）。
#
# 另有 Rust 测试 tests::opener_url_scope_pattern_matches_frontend_deep_link
# 用与上游同一 glob crate 复算匹配（证明模式真能匹配前端深链的 `?query` 段）。
#
# 为什么 ACL 仍值得断言：capability 引用未知权限标识符（拼错或插件未加入
# Cargo.toml）时 tauri-build 解析直接失败、cargo build 非零退出，(b) 因此
# 能拦截「权限声明写错」这类漂移。
set -euo pipefail
cd "$(dirname "$0")/.."

tauri_dir="apps/desktop/src-tauri"
expected_perm="opener:allow-open-url"
# 前端 settings.js 的 PREFS_DEEP_LINK；scope 白名单必须覆盖它，否则运行期
# is_url_allowed 返回 false -> ForbiddenUrl（ISS-068 二轮接缝）。
expected_url="x-apple.systempreferences:*"
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
ACL_MANIFESTS="$acl_manifests" SCHEMA="$schema" LIB_RS="$lib_rs" EXPECTED_PERM="$expected_perm" EXPECTED_URL="$expected_url" \
PYTHONPATH="$(pwd)" "$py" - <<'PY'
import json, os, sys

expected = os.environ["EXPECTED_PERM"]
expected_url = os.environ["EXPECTED_URL"]
failures = []

# ---- (b) 运行时 ACL：命令级授权 ----
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

# ---- (c) 入库能力清单：命令级 + URL scope 级 ----
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
    # 条目可能是字符串 "opener:allow-open-url"，也可能是带 scope 的对象
    # {"identifier": "opener:allow-open-url", "allow": [{"url": ...}]}。
    # 只有后者才携带 URL scope——这正是 ISS-068 第二轮接缝（见脚本头）。
    entry = next(
        (p for p in perms
         if (isinstance(p, str) and p == expected)
         or (isinstance(p, dict) and p.get("identifier") == expected)),
        None,
    )
    if entry is None:
        failures.append(
            f"{os.environ['SCHEMA']}: default.permissions 不含 {expected} -> {perms}"
        )
    elif isinstance(entry, str):
        # 命令已授权，但 URL scope 为空 -> 运行期 is_url_allowed 恒 false
        # -> 每次 open_url 返回 ForbiddenUrl。ISS-068 二轮实测接缝。
        failures.append(
            f"{os.environ['SCHEMA']}: {expected} 是裸字符串，**未携带 URL scope**；\n"
            f"      上游 is_url_allowed 对空 allow 列表恒返回 false，"
            f"open_url 会被 ForbiddenUrl 拒绝。\n"
            f"      需写成 {{\"identifier\": \"{expected}\", \"allow\": [{{\"url\": \"{expected_url}\"}}]}}"
        )
    else:
        allow = entry.get("allow") or []
        urls = [a.get("url") for a in allow if isinstance(a, dict)]
        if expected_url not in urls:
            failures.append(
                f"{os.environ['SCHEMA']}: {expected} 的 scope.allow.url 不含 {expected_url} -> {urls}"
            )

if failures:
    for item in failures:
        print(f"FAIL: {item}", file=sys.stderr)
    sys.exit(1)

# ---- (d) 防漂移：Rust 测试里的 SCOPE_PATTERN 必须与 capabilities 声明一致 ----
# 上面 (c) 只校验 JSON 内的 scope；Rust 测试（glob 语义验证）用的是源码里
# 另一份字面量。两份若漂移，(c) 绿而 Rust 测试验证的却是别的模式——假绿。
# 故在此交叉比对两者字符串相等。
import re
with open(os.environ["LIB_RS"], encoding="utf-8") as fh:
    lib_src = fh.read()
m = re.search(r'const SCOPE_PATTERN:\s*&str\s*=\s*"([^"]+)"', lib_src)
if not m:
    failures.append(f"{os.environ['LIB_RS']}: 未找到 const SCOPE_PATTERN 字面量")
elif m.group(1) != expected_url:
    failures.append(
        f"scope 模式漂移：lib.rs SCOPE_PATTERN={m.group(1)!r} != "
        f"capabilities/default.json 的 {expected_url!r}（两者必须一致）"
    )
else:
    print(f"cross-check: lib.rs SCOPE_PATTERN == {expected_url}")

if failures:
    for item in failures:
        print(f"FAIL: {item}", file=sys.stderr)
    sys.exit(1)

print(
    f"tauri opener registered: ok（源码注册 + ACL opener/allow-open-url "
    f"+ schema {expected} scope.url={expected_url}）"
)
PY
