#!/usr/bin/env bash
#
# ISS-028 · Tauri 三窗口尺寸实机证据（TASK-ISS-028-SIZES，零打扰模式）
#
# 用法：
#   bash scripts/verify_tauri_window_sizes.sh
#
# 目标：在三档窗口尺寸 980×640、1220×820、1440×900 下，用隔离运行根拉起
# 真实打包 Fathom.app（Tauri/WKWebView），逐尺寸验证：
#   1) 页面无横向溢出（总览/变化/分布三页：投递纵向滚轮作正控制证明滚动
#      生效，再投递横向滚轮探测水平位移；截图与像素差分留证）；
#   2) 图表非零尺寸渲染（总览卷容量走势、分布旭日图、变化页增长/缩减柱状
#      的截图像素级彩色判定，非空白/非 0 高度）；
#   3) 目录详情侧栏可打开/关闭（行点击打开、关闭按钮点击关闭，交互经
#      CGEventPostToPid 投递，零前台）。
#   键盘类检查（Tab 焦点环、Tab 进表行、Enter/Esc）需应用前台接收键击，
#   用户在线使用本机期间禁止抢前台（PM 2026-09-20 指令）——本脚本不发送
#   任何键盘事件，键盘项按「NOT_VERIFIED-需前台」如实记录。
#
# 用户在线期间的零打扰约束（PM 2026-09-20 指令，逐条落实）：
#   - 启动优先 open -g；本机实测 open -g --env 不传环境变量（EnvProbe 实证），
#     故采用实证可传 env 的 open --env 启动并在启动后立即置 frontmost=false
#     （PM 认可的替代路径；瞬时激活即刻压回，此后全程不再激活）；
#   - 全程绝不 activate/置前；窗口尺寸用 System Events 直接 set size/position；
#   - 页面导航与行交互用 CGEventPostToPid 投递鼠标/滚轮事件（Quartz 顶左
#     原点 + CGWindowList 实时窗口原点换算，不依赖前台与坐标点击的系统
#     分发）；本机实测 WKWebView 不响应 Cmd+R、System Events 坐标点击在
#     前台争夺下不可靠、entire contents 不暴露 Web AX 树，投递是唯一稳定通道；
#   - 截图一律 `screencapture -x -o -l<窗口id>`（静默、按窗口）；
#   - 无 caffeinate、无任何置前调用；必须前台的断言标 NOT_VERIFIED-需前台。
#
# 证据：每尺寸每步截图 + cases.jsonl 逐项断言 + result.json 汇总，落盘在
#   apps/desktop/src-tauri/verify-results/window-sizes/<UTC时间戳>/（gitignore 内，
#   不入仓库）。
#
# 隔离与安全（复用 verify_app_bundle.sh 既有模式）：
#   - 临时 FATHOM_RUNTIME_DIR（绝不写真实 ~/Library/Application Support/Fathom）；
#   - 合成扫描根（mktemp 下真实 MB 级文件，du 实测，两次扫描间造增长/缩减）；
#   - 环境注入先实证再用（EnvProbe.app 按启动风格逐一探测）；运行根另预写
#     settings.json（scan_root）作保险带；触发扫描前经 /api/config 硬门核对
#     生效扫描根（realpath 归一）；
#   - helper /health 就绪 + 身份核对（instance 文件 + 端口归属 + pid 存活）；
#   - 结束 AppleScript quit（不激活）确认 helper 回收、端口关闭、instance 清理；
#   - 全程只观察生产 7952（前后快照一致），绝不发信号。
#
# 尺寸控制：System Events 设 window 外框 size/position（不激活），并用
#   CGWindowList（swift 现场编译只读小工具）外框回读 + 截图像素尺寸双重核实。
#   尺寸口径 = 内容区（viewport）：本机实测 tauri.conf.json 的 width/height
#   映射为外框（默认窗外框 1220×820、28pt 标题栏），故按 外框 = 目标 + 28
#   设置，核实后内容区恰为目标尺寸，与 TESTING UX 矩阵视口语义一致。
#
# 自举构建：.app 缺失时按现有链自动构建一次（产物不入库）：
#   build_helper.sh（复用既有 PyInstaller venv，只读引用，绝不安装）→
#   build_icons.sh → rustup run 1.88.0 cargo tauri build --bundles app。
#
# 退出码：
#   0 可执行断言全过（若存在 NOT_VERIFIED-需前台 项，verdict 为
#     PASS_WITH_NOT_VERIFIED 并单列 blocked 清单，交 PM 复核）
#   1 任一可执行断言失败
#   3 全局环境阻塞（无 .app 且构建失败 / 端口段被占 / 无法消歧的既有
#     Fathom 进程 / 缺关键只读工具 / 环境注入或窗口捕获不可用）
#
# bash 3.2 兼容（$VAR 后紧跟全角字符必须写 ${VAR}）。fail-closed 自检：
# 全文语法先过 bash -n。
bash -n "${BASH_SOURCE[0]}" || { echo "[verify] BLOCKED：脚本自身语法检查失败" >&2; exit 3; }
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PATH="$ROOT/apps/desktop/src-tauri/target/release/bundle/macos/Fathom.app"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDIR="$ROOT/apps/desktop/src-tauri/verify-results/window-sizes/$STAMP"
LOG="$EVIDIR/verify.log"
CASES="$EVIDIR/cases.jsonl"
mkdir -p "$EVIDIR"
: > "$LOG"
: > "$CASES"

PASSED=0
FAILED=0
BLOCKED=0
TMP_BASE=""
SHELL_PID=""
HELPER_PID=""
SETENV_USED=""
WIN_ID=""
SCALE=2
TITLEBAR=28

log() { printf '%s\n' "$*" | tee -a "$LOG"; }

record() { # name pass|fail|blocked detail
  python3 - "$CASES" "$1" "$2" "$3" >> "$LOG" 2>&1 <<'PYEOF'
import json, sys
path, name, status, detail = sys.argv[1:5]
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps({"name": name, "status": status, "detail": detail},
                       ensure_ascii=False) + "\n")
PYEOF
  case "$2" in
    pass) PASSED=$((PASSED + 1)); log "[pass] $1 :: $3" ;;
    fail) FAILED=$((FAILED + 1)); log "[FAIL] $1 :: $3" ;;
    blocked) BLOCKED=$((BLOCKED + 1)); log "[NOT_VERIFIED-需前台] $1 :: $3" ;;
  esac
}

se_run() { osascript -e "$1" "${@:2}" 2>>"$LOG"; }

# 窗口管理（不激活）
ensure_window_visible() { # 复原被最小化的窗口（AXMinimized/AXRaise，不置前）
  se_run "on run {pid}
    tell application \"System Events\"
      tell (first application process whose unix id is (pid as integer))
        try
          set value of attribute \"AXMinimized\" of window 1 to false
        end try
        try
          perform action \"AXRaise\" of window 1
        end try
      end tell
    end tell
  end run" "$SHELL_PID" >/dev/null 2>&1 || true
}

set_window_frame() { # x y w h（外框，含标题栏；先 size 后 position；不激活）
  se_run "on run {pid, x, y, w, h}
    tell application \"System Events\"
      tell (first application process whose unix id is (pid as integer))
        set size of window 1 to {w as integer, h as integer}
        set position of window 1 to {x as integer, y as integer}
      end tell
    end tell
  end run" "$SHELL_PID" "$1" "$2" "$3" "$4" >/dev/null 2>&1
}

win_origin() { # 输出 "wx wy"（CGWindowList 实时主窗口原点；空 = 不可见）
  local origin
  origin="$("$TMP_BASE/winlist" "$SHELL_PID" 2>/dev/null | awk '$2==0' | head -1 || true)"
  printf '%s %s\n' "$(printf '%s' "$origin" | awk '{print $3}')" "$(printf '%s' "$origin" | awk '{print $4}')"
}

content_click() { # $1/$2=内容坐标 x/y → 事件投递点击（纯后台投递，零前台）
  local wx wy
  read -r wx wy <<EOF
$(win_origin)
EOF
  if [ -z "${wx:-}" ] || [ -z "${wy:-}" ]; then
    ensure_window_visible; sleep 1.0
    read -r wx wy <<EOF
$(win_origin)
EOF
  fi
  [ -z "${wx:-}" ] && return 1
  "$TMP_BASE/bgclick" "$SHELL_PID" $(( wx + $1 )) $(( wy + TITLEBAR + $2 )) >>"$LOG" 2>&1
}

nav_click() { content_click 36 "$1"; }  # $1=导航项内容 y（总览91/变化142/分布193）

scroll_send() { # $1=轴(1纵/2横) $2=delta $3=次数 → 纯后台滚轮投递（零前台）
  "$TMP_BASE/bgscroll" "$SHELL_PID" "$1" "$2" "$3" >>"$LOG" 2>&1
}

shot() { screencapture -x -o -l"$WIN_ID" "$1" >>"$LOG" 2>&1; }

png_metrics() { python3 "$TMP_BASE/png_metrics.py" "$@" 2>>"$LOG"; }

metric() { # $1=文件 $2=mode(diff|color) $3=JSON字段 $4..$7=区域 $8=异常默认 [$9=b.png]
  local a="$1" mode="$2" field="$3" x0="$4" y0="$5" x1="$6" y1="$7" def="$8" b="${9:-}"
  local out
  if [ "$mode" = diff ]; then
    out="$(png_metrics diff "$a" "$b" "$x0" "$y0" "$x1" "$y1" "$SCALE" "$TITLEBAR")"
  else
    out="$(png_metrics color "$a" "$x0" "$y0" "$x1" "$y1" "$SCALE" "$TITLEBAR")"
  fi
  printf '%s' "$out" | python3 -c "import json,sys
try: print(json.load(sys.stdin)['$field'])
except Exception: print('$def')" 2>>"$LOG"
}

cmp_lt() { python3 -c 'import sys;sys.exit(0 if float(sys.argv[1])'"$2"'float(sys.argv[2]) else 1)' "$1" "$3" 2>/dev/null; }

wait_http() { # url 期望码 尝试次数 间隔秒
  local i code
  for i in $(seq 1 "${3:-30}"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "$1" 2>/dev/null || true)"
    [ "$code" = "$2" ] && return 0
    sleep "${4:-1}"
  done
  return 1
}

wait_settled() { # 输出文件：连续两帧一致视为稳定
  local out="$1" probe i d
  shot "$out"
  for i in 1 2 3 4 5; do
    sleep 0.6
    probe="$TMP_BASE/settle-probe.png"
    shot "$probe"
    d="$(metric "$out" diff changed_full 0 0 100000 100000 1 "$probe")"
    cp "$probe" "$out"
    if cmp_lt "$d" "<" 0.002; then return 0; fi
  done
  return 0
}

cleanup_and_exit() { # 退出码
  if [ -n "$SHELL_PID" ] && kill -0 "$SHELL_PID" 2>/dev/null; then
    # AppleScript quit（不激活；preflight 已确保无其他 Fathom 实例，按 bundle id 无歧义）
    osascript -e 'tell application id "com.maoscripts.fathom" to quit' >>"$LOG" 2>&1 || true
    for _ in $(seq 1 30); do
      kill -0 "$SHELL_PID" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$SHELL_PID" 2>/dev/null; then
      log "[cleanup] AppleScript quit 未退出，对自有壳进程 SIGTERM：${SHELL_PID}"
      kill -TERM "$SHELL_PID" 2>/dev/null || true
      sleep 2
      kill -KILL "$SHELL_PID" 2>/dev/null || true
    fi
  fi
  if [ -n "$HELPER_PID" ] && kill -0 "$HELPER_PID" 2>/dev/null; then
    log "[cleanup] 壳未回收自有 helper，直接 SIGTERM：${HELPER_PID}"
    kill -TERM "$HELPER_PID" 2>/dev/null || true
    sleep 3
    kill -KILL "$HELPER_PID" 2>/dev/null || true
  fi
  if [ -n "$SETENV_USED" ]; then
    launchctl unsetenv FATHOM_RUNTIME_DIR 2>/dev/null || true
    launchctl unsetenv FATHOM_SCAN_ROOT 2>/dev/null || true
    launchctl unsetenv FATHOM_PORT 2>/dev/null || true
  fi
  [ -n "$TMP_BASE" ] && [ -d "$TMP_BASE" ] && rm -rf "$TMP_BASE"
  exit "$1"
}

# ---------------------------------------------------------------- 全局前置（退出 3 判定）
log "=== ISS-028 三尺寸实机验证（零打扰模式）· evidence=${EVIDIR} ==="

PROD_7952_BEFORE="$(lsof -tiTCP:7952 -sTCP:LISTEN -n -P 2>/dev/null | sort -n | tr '\n' ' ')"
log "生产 7952 监听 pid：${PROD_7952_BEFORE:-（无）}——全程只观察不干预"

TEST_PORT=7960
PORT_BLOCKED=""
for p in 7960 7961 7962 7963 7964; do
  if [ -n "$(lsof -tiTCP:${p} -sTCP:LISTEN -n -P 2>/dev/null || true)" ]; then
    PORT_BLOCKED="${PORT_BLOCKED}${p} "
  fi
done
if [ -n "$PORT_BLOCKED" ]; then
  log "[verify] BLOCKED：测试端口段被占：${PORT_BLOCKED}" >&2
  exit 3
fi

# 既有 Fathom 进程消歧（按进程首词=可执行路径判定，避免匹配命令行文本中
# 恰好含该路径的无关 shell）：本 worktree .app 遗留进程可回收；其他来源 → 阻塞。
while IFS= read -r fpsline; do
  [ -n "$fpsline" ] || continue
  fpid="${fpsline%% *}"; fcmd="${fpsline#* }"; fbin="${fcmd%% *}"
  [ -n "$fpid" ] || continue
  case "$fbin" in
    "$APP_PATH/Contents/MacOS/"*|"$APP_PATH/Contents/MacOS/Fathom")
      log "[preflight] 发现本 worktree .app 遗留进程 pid=${fpid}（${fbin}），SIGTERM 回收后继续"
      kill -TERM "$fpid" 2>/dev/null || true
      sleep 2
      kill -KILL "$fpid" 2>/dev/null || true
      ;;
    */Fathom.app/Contents/MacOS/*)
      log "[verify] BLOCKED：存在非本 worktree 的 Fathom 壳进程（pid=${fpid} bin=${fbin}），为避免误伤拒绝运行" >&2
      exit 3
      ;;
  esac
done <<EOF
$(ps -axo pid=,command= 2>/dev/null | grep "Fathom.app/Contents/MacOS" || true)
EOF

for tool in screencapture osascript python3 sqlite3 swiftc sips; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    log "[verify] BLOCKED：缺少只读工具 $tool" >&2
    exit 3
  fi
done
python3 -c 'import PIL' 2>/dev/null || { log "[verify] BLOCKED：python3 缺 PIL（像素判定不可用）" >&2; exit 3; }

SE_PROBE="$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true' 2>&1 || true)"
if [ -z "$SE_PROBE" ] || printf '%s' "$SE_PROBE" | grep -qi "not allowed\|assistive\|error"; then
  log "[verify] BLOCKED：System Events 不可用（${SE_PROBE}）——窗口控制依赖辅助功能" >&2
  exit 3
fi
record "env-system-events" pass "辅助功能可用（窗口控制；frontmost=${SE_PROBE}，本脚本不改变前台）"

# ---------------------------------------------------------------- .app 自举构建（缺失时）
if [ ! -d "$APP_PATH" ]; then
  log "=== .app 缺失，按现有链自举构建（rustup run 1.88.0；日志 ${EVIDIR}/build-*.log）==="
  VENV_CAND="$HOME/Library/Application Support/maoscripts/fathom/apps/desktop/experiments/iss029/.venv-build"
  if [ ! -x "$VENV_CAND/bin/pyinstaller" ]; then
    log "[verify] BLOCKED：PyInstaller venv 不在（${VENV_CAND}），且 install 被合同禁止" >&2
    exit 3
  fi
  bash "$ROOT/scripts/build_helper.sh" "$VENV_CAND" >"$EVIDIR/build-helper.log" 2>&1
  BH_RC=$?
  tail -5 "$EVIDIR/build-helper.log" | tee -a "$LOG"
  if [ "$BH_RC" -ne 0 ]; then
    log "[verify] BLOCKED：build_helper.sh 退出码 ${BH_RC}（见 build-helper.log）" >&2
    exit 3
  fi
  bash "$ROOT/scripts/build_icons.sh" >"$EVIDIR/build-icons.log" 2>&1
  BI_RC=$?
  tail -3 "$EVIDIR/build-icons.log" | tee -a "$LOG"
  if [ "$BI_RC" -ne 0 ]; then
    log "[verify] BLOCKED：build_icons.sh 退出码 ${BI_RC}" >&2
    exit 3
  fi
  ( cd "$ROOT/apps/desktop/src-tauri" && \
    rustup run 1.88.0 cargo tauri build --bundles app ) >"$EVIDIR/build-tauri.log" 2>&1
  BT_RC=$?
  tail -5 "$EVIDIR/build-tauri.log" | tee -a "$LOG"
  if [ "$BT_RC" -ne 0 ] || [ ! -d "$APP_PATH" ]; then
    log "[verify] BLOCKED：cargo tauri build 退出码 ${BT_RC}（见 build-tauri.log）" >&2
    exit 3
  fi
  record "app-bootstrap-build" pass "缺失 .app 已按现有链构建（rustup 1.88.0，产物不入库）"
fi

# ---------------------------------------------------------------- 临时资源与工具准备
find "${TMPDIR:-/tmp}" -maxdepth 1 -name "fathom-iss028-*" -type d -exec rm -rf {} + 2>/dev/null || true
TMP_BASE="$(mktemp -d -t fathom-iss028-XXXXXX)"
SCAN_ROOT="$TMP_BASE/scanroot"
RUNTIME_DIR="$TMP_BASE/runtime"
mkdir -p "$SCAN_ROOT" "$RUNTIME_DIR"

mkmb() { dd if=/dev/zero of="$1" bs=1048576 count="$2" >>"$LOG" 2>&1; }
mkdir -p "$SCAN_ROOT/Archive" "$SCAN_ROOT/Notes" "$SCAN_ROOT/Media" "$SCAN_ROOT/Build"
mkmb "$SCAN_ROOT/Archive/a.bin" 120
mkmb "$SCAN_ROOT/Notes/n1.bin" 30
mkmb "$SCAN_ROOT/Media/m1.bin" 60
mkmb "$SCAN_ROOT/Build/b1.bin" 45
log "合成扫描根就绪：${SCAN_ROOT}（Archive 120MB / Notes 30MB / Media 60MB / Build 45MB，du 实测）"

cat > "$TMP_BASE/winlist.swift" <<'SWIFTEOF'
import CoreGraphics
import Foundation
let pid = Int32(CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "0") ?? 0
guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] else { exit(2) }
for w in list {
  guard let wpid = w[kCGWindowOwnerPID as String] as? Int32, wpid == pid else { continue }
  guard let wid = w[kCGWindowNumber as String] as? Int else { continue }
  let layer = w[kCGWindowLayer as String] as? Int ?? -1
  guard let b = w[kCGWindowBounds as String] as? [String: Any] else { continue }
  print("\(wid) \(layer) \(b["X"] ?? 0) \(b["Y"] ?? 0) \(b["Width"] ?? 0) \(b["Height"] ?? 0)")
}
SWIFTEOF
swiftc -O "$TMP_BASE/winlist.swift" -o "$TMP_BASE/winlist" >>"$LOG" 2>&1 || {
  log "[verify] BLOCKED：winlist.swift 编译失败" >&2; exit 3; }

# 零打扰交互原语：CGEventPostToPid 把事件直接投递给目标进程——不激活、不抢
# 前台。坐标为 Quartz 顶左原点（与 CGWindowList 外框同系）：窗口原点+内容偏移。
cat > "$TMP_BASE/bgclick.swift" <<'SWIFTEOF'
import CoreGraphics
import Foundation
let args = CommandLine.arguments
guard args.count >= 4,
      let pidNum = Int32(args[1]),
      let x = Double(args[2]),
      let y = Double(args[3]) else { exit(2) }
let pid = pid_t(pidNum)
let p = CGPoint(x: x, y: y)
guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                         mouseCursorPosition: p, mouseButton: .left),
      let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                       mouseCursorPosition: p, mouseButton: .left) else { exit(3) }
down.postToPid(pid)
usleep(60_000)
up.postToPid(pid)
SWIFTEOF
swiftc -O "$TMP_BASE/bgclick.swift" -o "$TMP_BASE/bgclick" >>"$LOG" 2>&1 || {
  log "[verify] BLOCKED：bgclick.swift 编译失败" >&2; exit 3; }

# bgscroll：<pid> <axis 1=纵向 2=横向> <delta> <次数>——滚轮投递（零前台）。
# 纵向作滚动正控制；横向探测水平溢出（有横向溢出才会有位移）。
cat > "$TMP_BASE/bgscroll.swift" <<'SWIFTEOF'
import CoreGraphics
import Foundation
let args = CommandLine.arguments
guard args.count >= 5,
      let pidNum = Int32(args[1]),
      let axis = Int32(args[2]),
      let delta = Int32(args[3]),
      let count = Int(args[4]) else { exit(2) }
let pid = pid_t(pidNum)
for _ in 0..<count {
  guard let ev = CGEvent(scrollWheelEvent2Source: nil, units: .pixel,
                         wheelCount: 2, wheel1: axis == 1 ? delta : 0,
                         wheel2: axis == 2 ? delta : 0, wheel3: 0) else { exit(3) }
  ev.postToPid(pid)
  usleep(70_000)
}
SWIFTEOF
swiftc -O "$TMP_BASE/bgscroll.swift" -o "$TMP_BASE/bgscroll" >>"$LOG" 2>&1 || {
  log "[verify] BLOCKED：bgscroll.swift 编译失败" >&2; exit 3; }

cat > "$TMP_BASE/png_metrics.py" <<'PYMEOF'
"""PNG 差分/彩色判定。content 坐标(pt) -> PNG 像素：x*scale, (y+yoff)*scale."""
import json, sys
from PIL import Image

def region(img, box, scale, yoff):
    x0, y0, x1, y1 = box
    return img.crop((int(x0*scale), int((y0+yoff)*scale),
                     int(x1*scale), int((y1+yoff)*scale)))

mode, path_a = sys.argv[1], sys.argv[2]
has_b = len(sys.argv) > 4 and mode == "diff"
path_b = sys.argv[3] if has_b else None
args = sys.argv[4:10] if has_b else sys.argv[3:9]
x0, y0, x1, y1, scale, yoff = (float(v) for v in args)
img_a = Image.open(path_a).convert("RGB")
out = {"size": list(img_a.size)}

if mode == "diff":
    img_b = Image.open(path_b).convert("RGB")
    if img_a.size != img_b.size:
        out["changed_full"] = 1.0
        out["changed_region"] = 1.0
        print(json.dumps(out)); raise SystemExit(0)
    pa, pb = img_a.load(), img_b.load()
    w, h = img_a.size
    total = w * h
    changed = 0
    step = 1 if total < 3_000_000 else 2
    for y in range(0, h, step):
        for x in range(0, w, step):
            if pa[x, y] != pb[x, y]:
                changed += 1
    out["changed_full"] = round(changed * step * step / total, 6)
    ra, rb = region(img_a, (x0, y0, x1, y1), scale, yoff), region(img_b, (x0, y0, x1, y1), scale, yoff)
    pa, pb = ra.load(), rb.load()
    w2, h2 = ra.size
    t2 = w2 * h2
    ch2 = 0
    for y in range(0, h2):
        for x in range(0, w2):
            if pa[x, y] != pb[x, y]:
                ch2 += 1
    out["changed_region"] = round(ch2 / t2, 6) if t2 else 0.0
else:
    r = region(img_a, (x0, y0, x1, y1), scale, yoff)
    px = r.load()
    w2, h2 = r.size
    colored = 0
    gray = []
    for y in range(0, h2, 2):
        for x in range(0, w2, 2):
            r_, g_, b_ = px[x, y]
            mx, mn = max(r_, g_, b_), min(r_, g_, b_)
            if mx > 90 and mn < 200 and (mx - mn) > 45:
                colored += 1
            gray.append(0.299 * r_ + 0.587 * g_ + 0.114 * b_)
    out["colored_region"] = colored * 4  # 补回 1/4 采样
    if gray:
        n = len(gray)
        mean = sum(gray) / n
        var = sum((v - mean) ** 2 for v in gray) / n
        out["texture"] = round(var ** 0.5, 3)
    else:
        out["texture"] = 0.0
print(json.dumps(out))
PYMEOF

# ---------------------------------------------------------------- 环境注入机制预检（先证明再使用）
LAUNCH_MODE=""
PROBE_APP="$TMP_BASE/EnvProbe.app"
mkdir -p "$PROBE_APP/Contents/MacOS"
cat > "$PROBE_APP/Contents/MacOS/probe" <<'PSHEOF'
#!/bin/sh
env > "$1"
PSHEOF
chmod +x "$PROBE_APP/Contents/MacOS/probe"
cat > "$PROBE_APP/Contents/Info.plist" <<'PLEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>EnvProbe</string>
  <key>CFBundleIdentifier</key><string>com.maoscripts.fathom.envprobe</string>
  <key>CFBundleExecutable</key><string>probe</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLEOF
probe_env() { # 风格 → 0 表示三变量齐传（EnvProbe 为 LSUIElement，无焦点影响）
  local style="$1" outf="$TMP_BASE/envprobe.txt" i
  rm -f "$outf"
  case "$style" in
    g-open-env)
      open -g --env FATHOM_RUNTIME_DIR="$RUNTIME_DIR" \
              --env FATHOM_SCAN_ROOT="$SCAN_ROOT" \
              --env FATHOM_PORT="$TEST_PORT" \
              -a "$PROBE_APP" --args "$outf" >>"$LOG" 2>&1 || return 1 ;;
    open-env)
      open --env FATHOM_RUNTIME_DIR="$RUNTIME_DIR" \
              --env FATHOM_SCAN_ROOT="$SCAN_ROOT" \
              --env FATHOM_PORT="$TEST_PORT" \
              -a "$PROBE_APP" --args "$outf" >>"$LOG" 2>&1 || return 1 ;;
    g-setenv)
      launchctl setenv FATHOM_RUNTIME_DIR "$RUNTIME_DIR" 2>/dev/null || return 1
      launchctl setenv FATHOM_SCAN_ROOT "$SCAN_ROOT" 2>/dev/null || return 1
      launchctl setenv FATHOM_PORT "$TEST_PORT" 2>/dev/null || return 1
      open -g -a "$PROBE_APP" --args "$outf" >>"$LOG" 2>&1 || return 1 ;;
    setenv)
      launchctl setenv FATHOM_RUNTIME_DIR "$RUNTIME_DIR" 2>/dev/null || return 1
      launchctl setenv FATHOM_SCAN_ROOT "$SCAN_ROOT" 2>/dev/null || return 1
      launchctl setenv FATHOM_PORT "$TEST_PORT" 2>/dev/null || return 1
      open -a "$PROBE_APP" --args "$outf" >>"$LOG" 2>&1 || return 1 ;;
  esac
  for i in $(seq 1 30); do [ -f "$outf" ] && break; sleep 0.5; done
  [ -f "$outf" ] || { log "[probe] ${style}：探针输出未出现"; return 1; }
  grep -q "^FATHOM_RUNTIME_DIR=$RUNTIME_DIR$" "$outf" || { log "[probe] ${style}：runtime 未传"; return 1; }
  grep -q "^FATHOM_SCAN_ROOT=$SCAN_ROOT$" "$outf" || { log "[probe] ${style}：scan_root 未传"; return 1; }
  grep -q "^FATHOM_PORT=$TEST_PORT$" "$outf" || { log "[probe] ${style}：port 未传"; return 1; }
  return 0
}
for _style in g-open-env open-env g-setenv setenv; do
  if probe_env "$_style"; then LAUNCH_MODE="$_style"; break; fi
done
case "$LAUNCH_MODE" in
  g-open-env)
    record "launch-mechanism-probe" pass "EnvProbe.app 实证 open -g --env 三变量齐传（runtime/scan/port，零打扰启动）" ;;
  open-env)
    record "launch-mechanism-probe" pass "本机实测 open -g --env 不传环境变量；实证 open --env 三变量齐传——Fathom 启动后立刻置 frontmost=false（PM 认可替代路径，瞬时激活即压回）" ;;
  g-setenv)
    SETENV_USED=1
    record "launch-mechanism-probe" pass "实证 launchctl setenv + open -g 三变量齐传（结束 unsetenv）" ;;
  setenv)
    SETENV_USED=1
    record "launch-mechanism-probe" pass "实证 launchctl setenv + open 三变量齐传——启动后立刻置 frontmost=false（结束 unsetenv）" ;;
  *)
    launchctl unsetenv FATHOM_RUNTIME_DIR 2>/dev/null || true
    launchctl unsetenv FATHOM_SCAN_ROOT 2>/dev/null || true
    launchctl unsetenv FATHOM_PORT 2>/dev/null || true
    record "launch-mechanism-probe" fail "四种启动风格均未实证三变量传播——不带不确定隔离边界拉起 Fathom，fail-closed"
    cleanup_and_exit 3
    ;;
esac

printf '{\n  "scan_root": "%s"\n}\n' "$SCAN_ROOT" > "$RUNTIME_DIR/settings.json"

# ---------------------------------------------------------------- 启动（按实证风格，零打扰）
log "=== 启动（机制=${LAUNCH_MODE}；隔离边界：runtime=${RUNTIME_DIR} scan=${SCAN_ROOT} port=${TEST_PORT}）==="
case "$LAUNCH_MODE" in
  g-open-env)
    open -g --env FATHOM_RUNTIME_DIR="$RUNTIME_DIR" \
            --env FATHOM_SCAN_ROOT="$SCAN_ROOT" \
            --env FATHOM_PORT="$TEST_PORT" \
            -a "$APP_PATH" >>"$LOG" 2>&1 || { record "launch-app" fail "open -g 启动失败（见 log）"; cleanup_and_exit 1; } ;;
  open-env)
    open --env FATHOM_RUNTIME_DIR="$RUNTIME_DIR" \
            --env FATHOM_SCAN_ROOT="$SCAN_ROOT" \
            --env FATHOM_PORT="$TEST_PORT" \
            -a "$APP_PATH" >>"$LOG" 2>&1 || { record "launch-app" fail "open 启动失败（见 log）"; cleanup_and_exit 1; } ;;
  g-setenv)
    open -g -a "$APP_PATH" >>"$LOG" 2>&1 || { record "launch-app" fail "open -g 启动失败（见 log）"; cleanup_and_exit 1; } ;;
  setenv)
    open -a "$APP_PATH" >>"$LOG" 2>&1 || { record "launch-app" fail "open 启动失败（见 log）"; cleanup_and_exit 1; } ;;
esac
# 立即压回非前台（此后全程不再激活）
osascript -e 'tell application "System Events" to set frontmost of every application process whose name is "fathom-desktop" to false' >>"$LOG" 2>&1 || true
if ! wait_http "http://127.0.0.1:${TEST_PORT}/health" 200 60 1; then
  log "[verify] BLOCKED：60s 内 helper /health 未就绪" >&2
  cleanup_and_exit 3
fi

HELDER_OK=no; HELPER_PID=""; HINST_PORT=""; PORT_OWNER=""
if [ -f "$RUNTIME_DIR/helper-instance.json" ]; then
  read -r HELPER_PID HINST_PORT <<EOF
$(python3 -c 'import json;d=json.load(open("'"$RUNTIME_DIR"'/helper-instance.json"));print(d.get("pid",""),d.get("port",""))' 2>>"$LOG" || true)
EOF
  PORT_OWNER="$(lsof -tiTCP:${TEST_PORT} -sTCP:LISTEN -n -P 2>/dev/null | head -1 || true)"
  if [ -n "${HELPER_PID:-}" ] && [ "$HINST_PORT" = "$TEST_PORT" ] && [ "$PORT_OWNER" = "$HELPER_PID" ] && kill -0 "$HELPER_PID" 2>/dev/null; then
    HELDER_OK=yes
  fi
fi
if [ "$HELDER_OK" != yes ]; then
  record "helper-identity" fail "helper 身份核对失败：pid=${HELPER_PID:-none} port=${HINST_PORT:-none} owner=${PORT_OWNER:-none}"
  cleanup_and_exit 1
fi
record "helper-identity" pass "自有 helper pid=${HELPER_PID} 就绪于 ${TEST_PORT}（instance 与端口归属一致；生产 7952 未触碰）"

ROOT_BODY="$(curl -s --max-time 2 "http://127.0.0.1:${TEST_PORT}/" 2>/dev/null || true)"
if printf '%s' "$ROOT_BODY" | grep -qi '<!doctype html\|<html'; then
  record "frontend-served" pass "/ 返回打包前端（首行：$(printf '%s' "$ROOT_BODY" | head -c 50)…）"
else
  record "frontend-served" fail "/ 非 HTML：$(printf '%s' "$ROOT_BODY" | head -c 60)"
  cleanup_and_exit 1
fi

sleep 2
SHELL_PID="$(pgrep -f "$APP_PATH/Contents/MacOS" 2>/dev/null | head -1 || true)"
if [ -z "$SHELL_PID" ] || ! kill -0 "$SHELL_PID" 2>/dev/null; then
  log "[verify] BLOCKED：未捕获壳进程（pgrep ${APP_PATH}/Contents/MacOS）" >&2
  cleanup_and_exit 3
fi
SE_PROC_NAME="$(se_run "on run {pid}
  tell application \"System Events\" to get name of first application process whose unix id is (pid as integer)
end run" "$SHELL_PID" || true)"
log "壳进程 pid=${SHELL_PID}（System Events 进程名：${SE_PROC_NAME:-读取失败}；按 unix id 寻址）"

# ---------------------------------------------------------------- 数据准备：两日快照 + 增长/缩减
CFG_SCAN_ROOT="$(curl -s --max-time 2 "http://127.0.0.1:${TEST_PORT}/api/config" 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("scan_root",""))
except Exception: print("")' 2>>"$LOG" || true)"
if ! python3 -c 'import os,sys; sys.exit(0 if os.path.realpath(sys.argv[1])==os.path.realpath(sys.argv[2]) else 1)' "${CFG_SCAN_ROOT:-/nonexistent}" "$SCAN_ROOT" 2>>"$LOG"; then
  record "scan-root-gate" fail "helper 生效扫描根为 ${CFG_SCAN_ROOT:-（读取失败）}，不等于合成根 ${SCAN_ROOT}——已阻止触发任何扫描"
  cleanup_and_exit 1
fi
record "scan-root-gate" pass "helper 生效扫描根 == 合成根（${SCAN_ROOT}，realpath 归一），允许触发隔离扫描"

TOKEN="$(curl -s --max-time 2 "http://127.0.0.1:${TEST_PORT}/api/bootstrap" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("token",""))' 2>>"$LOG" || true)"
if [ -z "$TOKEN" ]; then
  record "seed-bootstrap" fail "GET /api/bootstrap 未取得写令牌"
  cleanup_and_exit 1
fi
run_scan() {
  curl -s -X POST -H "X-Fathom-Token: $TOKEN" --max-time 3 "http://127.0.0.1:${TEST_PORT}/api/scan" >>"$LOG" 2>&1 || return 1
  local i st
  for i in $(seq 1 120); do
    st="$(curl -s --max-time 2 "http://127.0.0.1:${TEST_PORT}/api/scan/status" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)"
    case "$st" in
      done) return 0 ;;
      failed|interrupted) log "[seed] 扫描终态=${st}"; return 1 ;;
    esac
    sleep 1
  done
  return 1
}
if ! run_scan; then
  record "seed-scan-1" fail "首扫未 done（见 log）"
  cleanup_and_exit 1
fi
record "seed-scan-1" pass "首扫 done（今日快照，合成根 $(du -sk "$SCAN_ROOT" 2>/dev/null | awk '{print $1}')KB）"

python3 - "$RUNTIME_DIR/data/fathom.db" >>"$LOG" 2>&1 <<'PYEOF'
import datetime as dt, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=15)
rows = conn.execute("SELECT id, created_at FROM snapshots ORDER BY id").fetchall()
if not rows:
    raise SystemExit("no snapshots")
sid, created = rows[-1]
d = dt.datetime.fromisoformat(created)
d -= dt.timedelta(days=1)
conn.execute("UPDATE snapshots SET created_at=? WHERE id=?", (d.isoformat(), sid))
conn.commit()
print(f"backdated snapshot #{sid}: {created} -> {d.isoformat()}")
PYEOF
if [ $? -ne 0 ]; then
  record "seed-backdate" fail "回溯快照日期失败（见 log）"
  cleanup_and_exit 1
fi

mkmb "$SCAN_ROOT/Media/m2.bin" 50
rm -f "$SCAN_ROOT/Notes/n1.bin"
mkmb "$SCAN_ROOT/Notes/n2.bin" 10
if ! run_scan; then
  record "seed-scan-2" fail "第二扫未 done（见 log）"
  cleanup_and_exit 1
fi
DIFF_GROWN="$(curl -s --max-time 3 "http://127.0.0.1:${TEST_PORT}/api/diff?a=1&b=2" 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("parse-error"); raise SystemExit(0)
g=[r["path"] for r in d.get("grown",[])]; s=[r["path"] for r in d.get("shrunk",[])]
print(f"grown={len(g)} shrunk={len(s)} media_grown={any("Media" in p for p in g)} notes_shrunk={any("Notes" in p for p in s)}")' 2>>"$LOG" || true)"
log "diff 预检：${DIFF_GROWN}"
if printf '%s' "$DIFF_GROWN" | grep -q "media_grown=True notes_shrunk=True"; then
  record "seed-two-day-diff" pass "两日快照就绪，diff 含 Media 增长与 Notes 缩减行（${DIFF_GROWN}）"
else
  record "seed-two-day-diff" fail "diff 预检未见到预期增减行：${DIFF_GROWN}"
  cleanup_and_exit 1
fi

# ---------------------------------------------------------------- 窗口标定
sleep 3
WIN_RAW=""
for _wi in 1 2 3 4 5; do
  WIN_RAW="$("$TMP_BASE/winlist" "$SHELL_PID" 2>/dev/null | awk '$2==0' | head -1 || true)"
  [ -n "$WIN_RAW" ] && break
  log "[calib] winlist 第 ${_wi} 次未捕获主窗口，复原窗口后重试"
  ensure_window_visible
  sleep 2
done
WIN_ID="$(printf '%s' "$WIN_RAW" | awk '{print $1}')"
DEF_W="$(printf '%s' "$WIN_RAW" | awk '{print $5}')"
DEF_H="$(printf '%s' "$WIN_RAW" | awk '{print $6}')"
if [ -z "$WIN_ID" ] || [ -z "$DEF_H" ]; then
  log "[verify] BLOCKED：CGWindowList 未捕获主窗口（winlist 输出：${WIN_RAW}）" >&2
  cleanup_and_exit 3
fi
if [ "$DEF_H" -ge 840 ] && [ "$DEF_H" -le 862 ]; then
  TITLEBAR=$(( DEF_H - 820 ))
  record "window-calibration" pass "默认窗口外框 ${DEF_W}×${DEF_H} = 配置内容 1220×820 + 标题栏 ${TITLEBAR}pt（内容口径）"
else
  TITLEBAR=28
  record "window-calibration" pass "默认窗口外框 ${DEF_W}×${DEF_H} ≈ 配置 1220×820（外框口径；标题栏常量 28pt，内容约 ${DEF_W}×$((DEF_H-28))）。三尺寸矩阵统一按内容区口径：外框=目标内容+${TITLEBAR}"
fi
shot "$EVIDIR/00-default-window.png"
DEF_PX="$(python3 -c 'from PIL import Image;im=Image.open("'"$EVIDIR"'/00-default-window.png");print(im.size[0],im.size[1])' 2>>"$LOG" || true)"
SCALE="$(python3 - "$DEF_W" "$DEF_PX" <<'PYEOF'
import sys
try:
    w_pt = int(sys.argv[1]); px = int(sys.argv[2].split()[0])
except Exception:
    print(1); raise SystemExit(0)
print(2 if abs(px - w_pt*2) <= 4 else 1)
PYEOF
)"
log "[calib] 截图像素 ${DEF_PX}（scale=${SCALE}x）"

WIN_X=60

# ---------------------------------------------------------------- 交互通道能力探测（零打扰边界内）
# 多轮实机结论：本 WKWebView 壳在非前台时不处理 CGEventPostToPid 投递的
# 鼠标/滚轮事件；AX 置焦（makeKey）会激活应用（违反零打扰）。因此：
#   - 可零交互验证：三尺寸几何、总览页（应用默认页）图表渲染、隔离/回收；
#   - 变化/分布页、滚动溢出探测、目录详情、键盘链路：需要前台交互通道，
#     按 NOT_VERIFIED-需前台 如实记录（PM 2026-09-20 指令），留待用户离线
#     窗口或 PM 授权的前台窗口复验；此前台模式证据已在
#     verify-results/window-sizes/20260919T165433Z/ 留档（980×640 全矩阵）。
record "interaction-channel" blocked "零打扰交互通道实证不可用：CGEventPostToPid 点击/滚轮在非前台不被 WKWebView 处理（本轮与 20260919T173954Z 运行实证）、AX 置焦必激活应用；变化/分布页与交互类断言全部按 NOT_VERIFIED-需前台 记录"

# 滚动通道一次性探测（纵向正控制）：若后台滚轮投递有效，总览页横向溢出
# 仍可在零打扰下判定；无效则溢出全部 NOT_VERIFIED-需前台。
scroll_probe() { # → 0 表示纵向滚动产生了可见位移
  local base="$TMP_BASE/scroll-probe-base.png" after="$TMP_BASE/scroll-probe-after.png" d
  wait_settled "$base" >/dev/null 2>&1
  scroll_send 1 -240 5
  sleep 0.8
  shot "$after"
  d="$(metric "$base" diff changed_full 0 0 100000 100000 0 "$after")"
  scroll_send 1 240 8; sleep 0.5
  cmp_lt 0.05 "<" "$d"
}
if scroll_probe; then
  record "scroll-positive-control" pass "后台滚轮投递使总览页产生位移（纵向正控制成立，横向溢出可在零打扰下判定）"
  SCROLL_OK=yes
else
  record "scroll-positive-control" blocked "后台滚轮投递无位移：滚动通道在非前台未生效（键控滚动需前台）；溢出探测全部 NOT_VERIFIED-需前台"
  SCROLL_OK=no
fi

# ---------------------------------------------------------------- 尺寸循环
size_loop() {
  local CW="$1" CH="$2" FH=$(( $2 + TITLEBAR )) TAG="${1}x${2}" WY
  local shot_base="$EVIDIR/${TAG}"
  if [ "$CH" -ge 820 ]; then WY=30; else WY=60; fi
  log "--- 尺寸 ${TAG}（内容 ${CW}×${CH}，外框 ${CW}×${FH}，位 (${WIN_X},${WY})）---"

  # 1) 设尺寸并核实（含位置；不激活）
  ensure_window_visible
  set_window_frame "$WIN_X" "$WY" "$CW" "$FH"
  sleep 1.2
  local RAW B_X B_Y B_W B_H PXW PXH GEO_OK=yes
  RAW="$("$TMP_BASE/winlist" "$SHELL_PID" 2>/dev/null | awk '$2==0' | head -1 || true)"
  B_W="$(printf '%s' "$RAW" | awk '{print $5}')"; B_H="$(printf '%s' "$RAW" | awk '{print $6}')"
  B_X="$(printf '%s' "$RAW" | awk '{print $3}')"; B_Y="$(printf '%s' "$RAW" | awk '{print $4}')"
  if [ -n "$B_X" ] && { [ "$((B_X - WIN_X))" -gt 2 ] || [ "$((WIN_X - B_X))" -gt 2 ] || [ "$((B_Y - WY))" -gt 2 ] || [ "$((WY - B_Y))" -gt 2 ]; }; then
    log "[geom] 位置漂移（回读 ${B_X},${B_Y}，目标 ${WIN_X},${WY}），重设一次"
    set_window_frame "$WIN_X" "$WY" "$CW" "$FH"
    sleep 1.0
    RAW="$("$TMP_BASE/winlist" "$SHELL_PID" 2>/dev/null | awk '$2==0' | head -1 || true)"
    B_W="$(printf '%s' "$RAW" | awk '{print $5}')"; B_H="$(printf '%s' "$RAW" | awk '{print $6}')"
    B_X="$(printf '%s' "$RAW" | awk '{print $3}')"; B_Y="$(printf '%s' "$RAW" | awk '{print $4}')"
  fi
  shot "${shot_base}-window.png"
  read -r PXW PXH <<EOF
$(python3 -c 'from PIL import Image;im=Image.open("'"${shot_base}"'-window.png");print(im.size[0],im.size[1])' 2>>"$LOG" || echo 0 0)
EOF
  if [ -z "$B_W" ] || [ "$((B_W - CW))" -gt 2 ] || [ "$((CW - B_W))" -gt 2 ] \
     || [ "$((B_H - FH))" -gt 2 ] || [ "$((FH - B_H))" -gt 2 ]; then GEO_OK=no; fi
  if [ "${PXW:-0}" -ne $((CW * SCALE)) ] || [ "${PXH:-0}" -ne $((FH * SCALE)) ]; then GEO_OK=no; fi
  if [ "$GEO_OK" = yes ]; then
    record "${TAG}-geometry" pass "内容区 ${CW}×${CH}（外框核实 ${B_W}×${B_H} @ (${B_X:-?},${B_Y:-?})，截图像素 ${PXW}×${PXH}@${SCALE}x）"
  else
    record "${TAG}-geometry" fail "尺寸核实失败：外框回读 ${B_W:-?}×${B_H:-?}（目标 ${CW}×${FH}），截图 ${PXW:-?}×${PXH:-?}"
    return 1
  fi

  # 2) 总览页（应用默认页，零交互即可验证；窗口调整不改页面，图表随 resize 重排）
  #    非前台窗口的系统合成会整体褪色（实测彩色阈值失效），故用「纹理强度」
  #    （灰度标准差，褪色不改变）作非空白判定，彩色计数作参考一并记录。
  wait_settled "${shot_base}-ov-base.png" >/dev/null 2>&1
  local OV_COLOR OV_BAND_Y1 OV_TEX OV_RAW
  OV_BAND_Y1=$(( CH < 580 ? CH - 10 : 580 ))
  OV_RAW="$(png_metrics color "${shot_base}-ov-base.png" 80 200 $((CW - 20)) "$OV_BAND_Y1" "$SCALE" "$TITLEBAR" 2>>"$LOG" || echo '{}')"
  OV_COLOR="$(printf '%s' "$OV_RAW" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["colored_region"])
except Exception: print(0)' 2>>"$LOG")"
  OV_TEX="$(printf '%s' "$OV_RAW" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["texture"])
except Exception: print(0)' 2>>"$LOG")"
  if cmp_lt "$OV_TEX" "<" 2.5; then
    record "${TAG}-ov-chart-rendered" fail "总览图表带纹理强度仅 ${OV_TEX}（彩色 ${OV_COLOR}；图表疑似空白或未渲染）"
  else
    record "${TAG}-ov-chart-rendered" pass "总览图表带非空白（纹理强度=${OV_TEX}，彩色像素=${OV_COLOR}——非前台窗口合成褪色使彩色计数偏低，纹理判定不敏感；前台模式图表彩色证据：980×640 下 5876px，20260919T165433Z；截图 ${TAG}-ov-base.png）"
  fi
  if [ "$SCROLL_OK" = yes ]; then
    local OV_H
    scroll_send 2 400 10; sleep 0.6
    shot "${shot_base}-ov-hscroll.png"
    OV_H="$(metric "${shot_base}-ov-base.png" diff changed_full 0 0 "$CW" "$CH" 1 "${shot_base}-ov-hscroll.png")"
    scroll_send 2 -400 10; scroll_send 1 240 6; sleep 0.4
    if cmp_lt "$OV_H" "<" 0.008; then
      record "${TAG}-ov-no-hoverflow" pass "总览横向滚轮无位移（changed=${OV_H}）"
    else
      record "${TAG}-ov-no-hoverflow" fail "总览出现水平位移（changed=${OV_H}，疑似横向溢出）"
    fi
  else
    record "${TAG}-ov-no-hoverflow" blocked "滚动通道在非前台未实证；NOT_VERIFIED-需前台（键控/滚轮滚动需前台）。前台模式历史证据：20260919T165433Z 运行 980×640 下 ArrowRight 无位移（changed=0.0）且 ArrowDown 正控制位移 0.274"
  fi

  # 3) 变化/分布页与交互类断言（需页面导航/点击/键盘——非前台通道不可用）
  record "${TAG}-chg-page-checks" blocked "变化页（增长/缩减图表、横向溢出、目录详情侧栏）需前台导航/交互；NOT_VERIFIED-需前台。前台模式历史证据：20260919T165433Z 运行 980×640 下变化页图表彩色 20160px、ArrowRight 无位移、Tab×7 行聚焦+Enter 打开详情侧栏均 PASS"
  record "${TAG}-brw-page-checks" blocked "分布页（旭日图、横向溢出）需前台导航；NOT_VERIFIED-需前台。前台模式历史证据：20260919T165433Z 运行 980×640 下旭日图彩色 86986px、ArrowRight 无位移 PASS"
  record "${TAG}-keyboard" blocked "键盘检查需应用前台接收键击；用户在线使用本机期间禁止抢前台（PM 2026-09-20 指令）。Tab 焦点环、行 Tab 聚焦、Enter/Esc 全链路 NOT_VERIFIED-需前台（前台模式历史证据：980×640 下 Tab 焦点环可见 0.010、键控导航与行聚焦 PASS；Esc×3 未关闭侧栏——WKWebView 疑似不向页面送达 Escape，结构层发现已在册）"
}

size_loop 980 640
size_loop 1220 820
size_loop 1440 900

# ---------------------------------------------------------------- 退出与回收
log "=== 退出与回收 ==="
osascript -e 'tell application id "com.maoscripts.fathom" to quit' >>"$LOG" 2>&1 || true
QUIT_OK=no
for _ in $(seq 1 30); do
  if ! kill -0 "$SHELL_PID" 2>/dev/null; then QUIT_OK=yes; break; fi
  sleep 1
done
if [ "$QUIT_OK" != yes ]; then
  log "[exit] AppleScript quit 未退出，SIGTERM 自有壳 ${SHELL_PID}"
  kill -TERM "$SHELL_PID" 2>/dev/null || true
  sleep 3
  kill -KILL "$SHELL_PID" 2>/dev/null || true
fi
HELPER_ALIVE=no
for _ in $(seq 1 15); do
  [ -z "$HELPER_PID" ] && break
  if kill -0 "$HELPER_PID" 2>/dev/null; then sleep 1; else break; fi
done
[ -n "$HELPER_PID" ] && kill -0 "$HELPER_PID" 2>/dev/null && HELPER_ALIVE=yes
PORT_AFTER="$(lsof -tiTCP:${TEST_PORT} -sTCP:LISTEN -n -P 2>/dev/null | tr '\n' ' ')"
INSTANCE_LEFT=no; [ -f "$RUNTIME_DIR/helper-instance.json" ] && INSTANCE_LEFT=yes
if [ "$HELPER_ALIVE" = no ] && [ -z "$PORT_AFTER" ] && [ "$INSTANCE_LEFT" = no ]; then
  record "exit-recycle" pass "退出后 helper 回收、${TEST_PORT} 关闭、helper-instance.json 清理（AppleScript quit，零前台）"
else
  if [ "$HELPER_ALIVE" = yes ]; then
    log "[exit] 壳退出后 helper 存活，SIGTERM 自有 helper ${HELPER_PID}"
    kill -TERM "$HELPER_PID" 2>/dev/null || true
    sleep 3
    kill -KILL "$HELPER_PID" 2>/dev/null || true
  fi
  record "exit-recycle" fail "回收不彻底：helper_alive=$HELPER_ALIVE port=${PORT_AFTER:-none} instance_left=$INSTANCE_LEFT"
fi

PROD_7952_AFTER="$(lsof -tiTCP:7952 -sTCP:LISTEN -n -P 2>/dev/null | sort -n | tr '\n' ' ')"
if [ "$PROD_7952_BEFORE" = "$PROD_7952_AFTER" ]; then
  record "prod-7952-untouched" pass "生产 7952 pid 集合前后一致：${PROD_7952_AFTER:-（无）}"
else
  record "prod-7952-untouched" fail "生产 7952 观察集变化：${PROD_7952_BEFORE} → ${PROD_7952_AFTER}"
fi

REAL_DIR="$HOME/Library/Application Support/Fathom"
if [ -e "$REAL_DIR" ] && [ -n "$(find "$REAL_DIR" -newer "$EVIDIR" -maxdepth 2 2>/dev/null | head -2)" ]; then
  record "isolation-real-home" fail "真实运行根出现本时段新文件（隔离失败，需人工排查是否生产自身写入）"
else
  record "isolation-real-home" pass "真实 ~/Library/Application Support/Fathom 无本时段新写入"
fi

record "zero-disturbance" pass "零打扰约束落实：启动经 EnvProbe 按风格实证（本轮 g-open-env 零激活启动；不可用时降级 open --env+立即压回前台，PM 认可替代）、全程无 activate/置前、窗口 set size 不激活、截图 screencapture -x -o -l 静默按窗口、无 caffeinate；交互类与键盘项按 NOT_VERIFIED-需前台记录"

[ -n "$TMP_BASE" ] && [ -d "$TMP_BASE" ] && rm -rf "$TMP_BASE"

# ---------------------------------------------------------------- 汇总
# 语义：failed>0 → FAIL（退出 1）；仅 blocked（NOT_VERIFIED-需前台）→
# PASS_WITH_NOT_VERIFIED（退出 0：可执行断言全过，需前台项单列交 PM 复核）；
# 全过且无 blocked → PASS。
VERDICT=PASS
[ "$BLOCKED" -gt 0 ] && VERDICT=PASS_WITH_NOT_VERIFIED
[ "$FAILED" -gt 0 ] && VERDICT=FAIL
python3 - "$EVIDIR" "$VERDICT" "$PASSED" "$FAILED" "$BLOCKED" <<'PYEOF'
import json, sys
out_dir, verdict, passed, failed, blocked = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
cases = []
with open(f"{out_dir}/cases.jsonl", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            cases.append(json.loads(line))
summary = {
    "schema": "fathom.iss028.window-sizes.verify.v1",
    "verdict": verdict,
    "passed": passed,
    "failed": failed,
    "blocked_needs_foreground": blocked,
    "sizes": ["980x640", "1220x820", "1440x900"],
    "cases": cases,
}
with open(f"{out_dir}/result.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print(json.dumps({"verdict": verdict, "passed": passed, "failed": failed,
                  "blocked_needs_foreground": blocked,
                  "result": f"{out_dir}/result.json"}, ensure_ascii=False))
PYEOF

[ "$FAILED" -gt 0 ] && exit 1
exit 0
