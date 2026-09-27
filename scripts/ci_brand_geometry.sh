#!/usr/bin/env bash
#
# DEC-023 品牌资产一致性门禁（ISS-045，2026-09-19 起为双形态体系）
#
# 「层叠深潭」= App 图标（位图系：原稿 assets/brand/ + 抠图生成链）；
# 「深度环」= 界面小尺寸形态（矢量系：同一几何分布四处）。本脚本逐一
# grep 关键参数/资产，任一处缺失或漂移即非零退出（fail closed）：
#   深度环四处：
#     1. frontend/icons.js          brandRing（DOM 注入，stroke 2）
#     2. apps/desktop/src-tauri/icons/icon.svg  界面形态几何声明（2.4/2.2）
#     3. frontend/favicon.svg      浏览器标签页（同 icon.svg 几何）
#     4. scripts/make_tray_icon.py 菜单栏 template 距离场常量
#   深潭资产链：原稿/提示词/来源记录在位 + build_app_icon.py 引用原稿
#   favicon：apple-touch-icon.png 在位 + index.html 两处引用
#   ISS-105 主窗口品牌位 = 正式图标位图：frontend/assets/brand-icon-*.png
#   五个尺寸在位且尺寸正确（源自 icon.png 1024 派生，见 build_icons.sh
#   同款 sips 手法），icons.js brandImg() / app.js 侧栏注入 / index.html
#   关于区挂载引用在位；像素级渲染验证归 verify_frontend_refresh.cjs
#   （brand-sidebar / settings-about 位图采样检查）。
#
# 修改深度环几何时四处须同步并重跑 make_tray_icon.py；修改深潭 App 图标
# 时重跑 build_app_icon.py --force 与 build_icons.sh，并按同手法重派生
# frontend/assets/brand-icon-*.png。
#
# bash 3.2 兼容。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAIL=0

need() { # need <文件> <描述> <grep 模式>
  local f="$1" desc="$2" pat="$3"
  if [ ! -f "${f}" ]; then
    echo "[brand-geometry] FAIL：${desc} 文件缺失 ${f}" >&2
    FAIL=1
    return
  fi
  if ! grep -qE "${pat}" "${f}"; then
    echo "[brand-geometry] FAIL：${desc} 未命中模式「${pat}」（几何漂移或被改动）" >&2
    FAIL=1
  fi
}

# --- SVG 形态三处：环弧 / 探针 / 刻度 ---
# 弧命令前的空格在 SVG 中可选（icons.js 无空格、icon.svg/favicon.svg 有），渲染等价
SVG_ARC='M20 9\.1 ?A8\.5 8\.5 0 1 1 14\.9 4'
SVG_PROBE='x1="12" y1="7\.5" x2="12" y2="16\.5"'
SVG_TICK='x1="17\.2" y1="6\.8" x2="19\.3" y2="4\.7"'

need "$ROOT/frontend/icons.js" "icons.js brandRing 环弧" "${SVG_ARC}"
need "$ROOT/frontend/icons.js" "icons.js brandRing 探针" "${SVG_PROBE}"
need "$ROOT/frontend/icons.js" "icons.js brandRing 刻度" "${SVG_TICK}"

need "$ROOT/apps/desktop/src-tauri/icons/icon.svg" "canonical icon.svg 环弧" "${SVG_ARC}"
need "$ROOT/apps/desktop/src-tauri/icons/icon.svg" "canonical icon.svg 探针" "${SVG_PROBE}"
need "$ROOT/apps/desktop/src-tauri/icons/icon.svg" "canonical icon.svg 刻度" "${SVG_TICK}"
need "$ROOT/apps/desktop/src-tauri/icons/icon.svg" "canonical icon.svg 海沟蓝" 'fill="#345d7f"'
need "$ROOT/apps/desktop/src-tauri/icons/icon.svg" "canonical icon.svg 亮矿物青" 'stroke="#82c8c2"'

need "$ROOT/frontend/favicon.svg" "favicon.svg 环弧" "${SVG_ARC}"
need "$ROOT/frontend/favicon.svg" "favicon.svg 探针" "${SVG_PROBE}"
need "$ROOT/frontend/favicon.svg" "favicon.svg 刻度" "${SVG_TICK}"

# --- Python 常量形态（tray 距离场）---
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 环半径" 'R_MID = 8\.5'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 弧界" 'ARC_START, ARC_END = -20\.0, 290\.0'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 弧端点" 'X1, Y1 = 20\.0, 9\.1'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 弧端点2" 'X2, Y2 = 14\.9, 4\.0'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 探针/刻度" '_dist_to_seg\(gx, gy, 12\.0, 7\.5, 12\.0, 16\.5\)'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 刻度端点" '17\.2, 6\.8, 19\.3, 4\.7'

# --- App 图标（层叠深潭位图系，DEC-023）：原稿与生成链在位 ---
need "$ROOT/assets/brand/fathom-approved-concept.png" "深潭原稿在位" '.'
need "$ROOT/assets/brand/generation-prompt.md" "深潭生成提示词在位" '.'
need "$ROOT/assets/brand/README.md" "深潭来源记录在位" '.'
need "$ROOT/scripts/build_app_icon.py" "深潭生成脚本原稿路径" 'assets/brand/fathom-approved-concept\.png'
need "$ROOT/scripts/build_app_icon.py" "深潭生成脚本尺寸" 'SIZE = 1024'

# --- favicon 资产在位与引用 ---
need "$ROOT/frontend/apple-touch-icon.png" "apple-touch-icon.png 在位" '.'
need "$ROOT/frontend/index.html" "index.html favicon 引用" 'rel="icon" type="image/svg\+xml" href="favicon\.svg"'
need "$ROOT/frontend/index.html" "index.html touch icon 引用" 'rel="apple-touch-icon" href="apple-touch-icon\.png"'

# --- ISS-105：主窗口品牌位 = 正式图标位图（frontend/assets，随 frontend/ 进打包链）---
# 侧栏 24 / 关于 64 显示位 + 2x srcset 资产；尺寸以 sips 实测核对，
# 防错尺寸文件顶替。像素级渲染验证在 verify_frontend_refresh.cjs。
for SZ in 24 48 64 128 256; do
  IMG="$ROOT/frontend/assets/brand-icon-${SZ}.png"
  if [ ! -f "${IMG}" ]; then
    echo "[brand-geometry] FAIL：品牌位图 ${SZ}px 缺失 ${IMG}" >&2
    FAIL=1
    continue
  fi
  W=$(sips -g pixelWidth "${IMG}" 2>/dev/null | awk '/pixelWidth/{print $2}')
  H=$(sips -g pixelHeight "${IMG}" 2>/dev/null | awk '/pixelHeight/{print $2}')
  if [ "${W}" != "${SZ}" ] || [ "${H}" != "${SZ}" ]; then
    echo "[brand-geometry] FAIL：品牌位图 ${SZ}px 实测 ${W}x${H}（应为 ${SZ}x${SZ}）" >&2
    FAIL=1
  fi
done
need "$ROOT/frontend/icons.js" "icons.js 品牌位图出口 brandImg" 'export function brandImg'
need "$ROOT/frontend/icons.js" "icons.js 品牌位图资产表" 'assets/brand-icon-24\.png'
need "$ROOT/frontend/app.js" "app.js 侧栏品牌位图注入" 'brandImg\(24'
need "$ROOT/frontend/index.html" "index.html 关于区品牌位图挂载" 'data-brand-img data-brand-size="64"'

if [ "${FAIL}" -ne 0 ]; then
  echo "[brand-geometry] FAIL：品牌资产/几何不一致（见上）" >&2
  exit 1
fi
echo "[brand-geometry] OK：深度环四处几何 + 层叠深潭资产链 + favicon 引用 + 主窗口品牌位图（ISS-105）一致（DEC-023 双形态体系）"
