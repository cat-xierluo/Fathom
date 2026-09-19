#!/usr/bin/env bash
#
# DEC-023 品牌几何一致性门禁（ISS-045 对齐收尾）
#
# 深度环 Logo 的同一几何以五种表示分布五处，本脚本逐一 grep 关键参数，
# 任一处缺失/漂移即非零退出（fail closed），防止只改一处造成视觉漂移：
#   1. frontend/icons.js            brandRing（DOM 注入，stroke 2）
#   2. apps/desktop/src-tauri/icons/icon.svg   canonical（stroke 2.4/2.2）
#   3. frontend/favicon.svg        浏览器标签页（同 canonical）
#   4. scripts/build_app_icon.py   1024 位图源渲染常量
#   5. scripts/make_tray_icon.py   菜单栏 template 距离场常量
#
# 修改几何时五处须同步，并重跑本脚本与三个生成脚本
# （build_app_icon.py --force / build_icons.sh / make_tray_icon.py）。
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

# --- Python 常量形态两处 ---
need "$ROOT/scripts/build_app_icon.py" "build_app_icon.py 弧界" 'ARC_START, ARC_END = -20, 290'
need "$ROOT/scripts/build_app_icon.py" "build_app_icon.py 环半径" 'RING_R = 8\.5 \* SCALE'
need "$ROOT/scripts/build_app_icon.py" "build_app_icon.py 海沟蓝" '\(52, 93, 127, 255\)'
need "$ROOT/scripts/build_app_icon.py" "build_app_icon.py 亮矿物青" '\(130, 200, 194, 255\)'
need "$ROOT/scripts/build_app_icon.py" "build_app_icon.py 探针端点" 'pt\(12, 7\.5\), pt\(12, 16\.5\)'
need "$ROOT/scripts/build_app_icon.py" "build_app_icon.py 刻度端点" 'pt\(17\.2, 6\.8\), pt\(19\.3, 4\.7\)'

need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 环半径" 'R_MID = 8\.5'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 弧界" 'ARC_START, ARC_END = -20\.0, 290\.0'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 弧端点" 'X1, Y1 = 20\.0, 9\.1'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 弧端点2" 'X2, Y2 = 14\.9, 4\.0'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 探针/刻度" '_dist_to_seg\(gx, gy, 12\.0, 7\.5, 12\.0, 16\.5\)'
need "$ROOT/scripts/make_tray_icon.py" "make_tray_icon.py 刻度端点" '17\.2, 6\.8, 19\.3, 4\.7'

# --- favicon 资产在位与引用 ---
need "$ROOT/frontend/apple-touch-icon.png" "apple-touch-icon.png 在位" '.'
need "$ROOT/frontend/index.html" "index.html favicon 引用" 'rel="icon" type="image/svg\+xml" href="favicon\.svg"'
need "$ROOT/frontend/index.html" "index.html touch icon 引用" 'rel="apple-touch-icon" href="apple-touch-icon\.png"'

if [ "${FAIL}" -ne 0 ]; then
  echo "[brand-geometry] FAIL：品牌几何不一致（见上）" >&2
  exit 1
fi
echo "[brand-geometry] OK：五处深度环几何与 favicon 资产一致（DEC-023）"
