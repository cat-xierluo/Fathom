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
#
# 修改深度环几何时四处须同步并重跑 make_tray_icon.py；修改深潭 App 图标
# 时重跑 build_app_icon.py --force 与 build_icons.sh。
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

if [ "${FAIL}" -ne 0 ]; then
  echo "[brand-geometry] FAIL：品牌资产/几何不一致（见上）" >&2
  exit 1
fi
echo "[brand-geometry] OK：深度环四处几何 + 层叠深潭资产链 + favicon 引用一致（DEC-023 双形态体系）"
