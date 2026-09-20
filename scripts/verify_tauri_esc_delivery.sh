#!/usr/bin/env bash
#
# ISS-077 · Tauri 壳裸 Escape 送达修复的 fail-closed 回归断言（零打扰、headless）
#
# 用法：
#   bash scripts/verify_tauri_esc_delivery.sh            # static 模式（默认）
#   bash scripts/verify_tauri_esc_delivery.sh full       # 追加 cargo 编译门（PM/CI 用）
#
# 背景（docs/TASKS.md ISS-028 卡「结构层发现」①；ISS-077 卡）：
#   macOS Tauri/WKWebView 壳内按 Esc 不产生页面 keydown——同窗口会话里
#   Tab/Enter/方向键均送达（焦点在 WebView），同页 Web/Playwright 下 Esc 可用
#   （前端 handler 已绑，changes.js 全局 keydown），故缺口在壳层对裸 Esc 的
#   键等价分发路径。上游同类：tauri#5790、wry#801、wry#1711——
#   WryWebViewParent::keyDown: 把 keyDown 只转给 mainMenu.performKeyEquivalent
#   且不检查返回值，是已登记的吞键路径。
#
# 修复合同（本脚本逐条断言，任一缺失/漂移即红，fail-closed）：
#   1) lib.rs 定义 ESC_FORWARD_MENU_ID="esc-forward" 与
#      ESC_FORWARD_ACCELERATOR="Escape"（裸 Esc，无修饰，muda 只认领无修饰 Esc）；
#   2) attach_escape_forward_menu_item 把该菜单项挂进默认菜单 Window 子菜单
#      （WINDOW_SUBMENU_ID），setup 里真实调用；
#   3) Builder on_menu_event 路由 esc-forward → forward_escape_to_page；
#   4) 转发载荷为合成 KeyboardEvent keydown（key/code=Escape、keyCode=27、
#      bubbles、dispatchEvent 到 activeElement），复用前端既有 document 级
#      监听——前端零改动零双绑定；
#   5) capabilities/default.json 权限集合与修复前基线逐项一致（本修复不得
#      放开任何事件/能力授权；基线见下方 EXPECTED_PERMISSIONS）。
#
# full 模式（PM/CI）：追加 rustup run 1.88.0 cargo build/test --locked
#   --offline（与 ci_cargo_locked.sh 同口径），证明合同代码可编译、单测过。
#
# 退出码：0=可执行断言全过；1=任一断言失败；3=环境阻塞（文件缺失/语法）。
#
# 环境变量：FATHOM_ESC_VERIFY_ROOT 覆盖仓库根（tests/test_tauri_esc_delivery.py
#   用临时副本做反例漂移测试；正常使用无需设置）。
#
# 实机行为断言（NOT_VERIFIED-需前台，本脚本不做、PM 亲自执行，用户
#   2026-09-20 规则）：前台打开目录详情侧栏 → 按一次 Esc → 侧栏必须关闭；
#   Tab 焦点链不受影响。壳收到菜单认领时打日志行「[esc-forward] 菜单认领
#   裸 Esc」，可在实机壳日志 grep 复核认领确实发生。
#
# bash 3.2 兼容。fail-closed 自检：全文语法先过 bash -n。
bash -n "${BASH_SOURCE[0]}" || { echo "[esc-verify] BLOCKED：脚本自身语法检查失败" >&2; exit 3; }
set -uo pipefail

MODE="${1:-static}"
ROOT="${FATHOM_ESC_VERIFY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LIB="$ROOT/apps/desktop/src-tauri/src/lib.rs"
CAP="$ROOT/apps/desktop/src-tauri/capabilities/default.json"

PASSED=0
FAILED=0

pass() { PASSED=$((PASSED + 1)); echo "[pass] $1"; }
fail() { FAILED=$((FAILED + 1)); echo "[FAIL] $1 :: ${2:-}" >&2; }

[ -f "$LIB" ] || { echo "[esc-verify] BLOCKED：缺少 $LIB" >&2; exit 3; }
[ -f "$CAP" ] || { echo "[esc-verify] BLOCKED：缺少 $CAP" >&2; exit 3; }
case "$MODE" in
  static|full) ;;
  *) echo "[esc-verify] BLOCKED：未知模式 $MODE（可选 static|full）" >&2; exit 3 ;;
esac

# ---- S1 · lib.rs 壳层转发合同 ----
# 菜单项 id / 加速键常量（各应出现在：定义 + 挂载调用 + 路由/单测）
cnt_menu_id="$(grep -c 'ESC_FORWARD_MENU_ID' "$LIB" || true)"
cnt_accel="$(grep -c 'ESC_FORWARD_ACCELERATOR' "$LIB" || true)"
if [ "$cnt_menu_id" -ge 3 ]; then pass "S1a ESC_FORWARD_MENU_ID 合同存在（${cnt_menu_id} 处）"; else fail "S1a 缺 ESC_FORWARD_MENU_ID 合同（仅 ${cnt_menu_id} 处，需 >=3：定义/挂载/路由）"; fi
if [ "$cnt_accel" -ge 3 ]; then pass "S1b ESC_FORWARD_ACCELERATOR 合同存在（${cnt_accel} 处）"; else fail "S1b 缺 ESC_FORWARD_ACCELERATOR 合同（仅 ${cnt_accel} 处）"; fi
grep -q 'const ESC_FORWARD_MENU_ID: &str = "esc-forward"' "$LIB" \
  && pass "S1c 菜单项 id 字面量为 esc-forward" \
  || fail "S1c ESC_FORWARD_MENU_ID 字面量漂移"
grep -q 'const ESC_FORWARD_ACCELERATOR: &str = "Escape"' "$LIB" \
  && pass "S1d 加速键字面量为裸 Escape" \
  || fail "S1d ESC_FORWARD_ACCELERATOR 字面量漂移（必须是 Escape，无修饰）"

# 挂载：Window 子菜单 + setup 真实调用（#[cfg(target_os = "macos")]）
cnt_win="$(grep -c 'WINDOW_SUBMENU_ID' "$LIB" || true)"
if [ "$cnt_win" -ge 2 ]; then pass "S1e Window 子菜单挂载点存在（${cnt_win} 处）"; else fail "S1e 缺 WINDOW_SUBMENU_ID 挂载（仅 ${cnt_win} 处，需 import + get）"; fi
grep -Eq 'fn attach_escape_forward_menu_item' "$LIB" \
  && pass "S1f attach_escape_forward_menu_item 已定义" \
  || fail "S1f 缺 attach_escape_forward_menu_item 定义"
grep -q 'attach_escape_forward_menu_item(handle)' "$LIB" \
  && pass "S1g setup 真实调用挂载函数" \
  || fail "S1g setup 未调用 attach_escape_forward_menu_item（挂载是死代码）"

# 路由：应用级 on_menu_event 按 id 分发到 Esc 转发（注意不能用裸
# '\.on_menu_event(' ——tray 菜单也有同名调用，会假绿）
grep -q 'event.id.as_ref() == ESC_FORWARD_MENU_ID' "$LIB" \
  && pass "S1h 应用菜单事件按 esc-forward id 路由" \
  || fail "S1h 缺 esc-forward 菜单事件路由（event.id.as_ref() == ESC_FORWARD_MENU_ID）"
cnt_fwd="$(grep -c 'forward_escape_to_page' "$LIB" || true)"
if [ "$cnt_fwd" -ge 2 ]; then pass "S1i Esc 转发函数已定义并被路由调用（${cnt_fwd} 处）"; else fail "S1i forward_escape_to_page 缺失或未接线（仅 ${cnt_fwd} 处，需定义+调用）"; fi

# 载荷：合成 keydown Escape 合同（与 lib.rs 单测同锚点）
for marker in 'new KeyboardEvent("keydown"' 'key: "Escape"' 'code: "Escape"' 'keyCode: 27' 'bubbles: true' 'dispatchEvent'; do
  if grep -qF "$marker" "$LIB"; then pass "S1j 载荷合同片段 ${marker}"; else fail "S1j 载荷缺合同片段 ${marker}"; fi
done

# ---- S2 · capability 不放宽（与修复前基线逐项一致）----
py_out="$(python3 - "$CAP" <<'PYEOF'
import json, sys
expected = [
    "core:default",
    "core:window:allow-show",
    "core:window:allow-hide",
    "core:window:allow-set-focus",
    "core:event:default",
    "allow-update-tray-status",
    "opener:allow-open-url",
]
with open(sys.argv[1], encoding="utf-8") as f:
    got = []
    for p in json.load(f)["permissions"]:
        got.append(p if isinstance(p, str) else p["identifier"])
extra = sorted(set(got) - set(expected))
missing = sorted(set(expected) - set(got))
wild = sorted(p for p in got if "*" in p and p != "opener:allow-open-url")
if extra or missing or wild:
    print("DRIFT extra=%s missing=%s wildcard=%s" % (extra, missing, wild))
    sys.exit(1)
print("OK %d 项权限与基线一致（无新增/删除/通配）" % len(got))
PYEOF
)" ; rc=$?
if [ $rc -eq 0 ]; then pass "S2 ${py_out}"; else fail "S2 capability 漂移：${py_out}（ISS-077 修复不得改权限集；若为其他任务的合法变更，请同步更新本脚本基线并说明）"; fi

# ---- S3 · full 模式：cargo 编译门（PM/CI；worker 沙箱无 cargo 时不适用）----
if [ "$MODE" = "full" ]; then
  MANIFEST="$ROOT/apps/desktop/src-tauri/Cargo.toml"
  if rustup run 1.88.0 cargo build --locked --offline --manifest-path "$MANIFEST"; then
    pass "S3a cargo build --locked --offline"
  else
    fail "S3a cargo build --locked --offline 失败"
  fi
  if rustup run 1.88.0 cargo test --locked --offline --manifest-path "$MANIFEST"; then
    pass "S3b cargo test --locked --offline（含 esc_forward 载荷合同单测）"
  else
    fail "S3b cargo test --locked --offline 失败"
  fi
fi

echo "----"
echo "[esc-verify] mode=$MODE passed=$PASSED failed=$FAILED"
echo "[esc-verify] NOT_VERIFIED-需前台：壳内真实按 Esc 关闭目录详情侧栏、Tab 焦点链不受影响（PM 亲自执行；壳日志 grep '[esc-forward] 菜单认领'）"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
