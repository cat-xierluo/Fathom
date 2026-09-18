#!/usr/bin/env python3
"""生成菜单栏 tray 图标（纯 stdlib PNG 编码，无需 PIL）。

ISS-045：图案升级为与 App 图标/brandRing 同构的「深度环」（开放圆环 +
中心探针 + 跨缺口短刻度），替代早期雷达环占位。template 风格：透明底 +
单色白图形，macOS 自动适配深浅菜单栏，不缩小彩色 App 图标。

几何：在 24 坐标系（中心 12,12，环半径 8.5，缺口 -20°~290° 之外即右上
50°，探针 (12,7.5)-(12,16.5)，刻度 (17.2,6.8)-(19.3,4.7)）上计算距离场，
与 apps/desktop/src-tauri/icons/icon.svg 的 canonical 声明逐参数对应；
整体缩放 0.94 留出边缘空白。距离场统一抗锯齿，无锯齿阶梯。

线宽：环/探针 4.0px、刻度 3.5px（44px 画布，对应 22pt 菜单栏 @2x 约
2pt 实线；发丝线在浅色菜单栏下近乎不可见）。
输出：apps/desktop/src-tauri/icons/tray.png（44x44）
"""

import math
import struct
import zlib
from pathlib import Path

SIZE = 44
SCALE = SIZE / 24.0 * 0.94          # 图形整体略缩，外缘留白
CX = CY = 12.0                       # 以下几何均在 24 坐标系
R_MID = 8.5                          # 环中线半径（brandRing 同源）
ARC_START, ARC_END = -20.0, 290.0    # 顺时针弧界（-20°=340°，与 canonical SVG 一致）
RING_W_PX = 4.0                      # 像素线宽（抗锯齿前的实心半宽基准）
PROBE_W_PX = 4.0
TICK_W_PX = 3.5
ARC_AA = 1.0                         # 距离场抗锯齿半宽（像素）

# 24 坐标系关键点（与 canonical SVG / frontend/icons.js brandRing 一致）
X1, Y1 = 20.0, 9.1                   # 弧起点（-20°/340°）
X2, Y2 = 14.9, 4.0                   # 弧终点（290°）


def _dist_to_arc(x: float, y: float) -> float:
    """点到开放圆环中线的最短距离（24 坐标系）。"""
    dx, dy = x - CX, y - CY
    dist = math.hypot(dx, dy)
    a = math.degrees(math.atan2(dy, dx))
    if a < 0:
        a += 360.0
    # 弧覆盖 [340,360)∪[0,290]；角度在内取径向差，在外取到端点距离
    if a <= ARC_END or a >= 360.0 + ARC_START:
        return abs(dist - R_MID)
    return min(math.hypot(x - X1, y - Y1), math.hypot(x - X2, y - Y2))


def _dist_to_seg(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """点到线段的最短距离（24 坐标系）。"""
    vx, vy = x2 - x1, y2 - y1
    wx, wy = px - x1, py - y1
    denom = vx * vx + vy * vy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    return math.hypot(px - (x1 + t * vx), py - (y1 + t * vy))


def _alpha(d24: float, width_px: float) -> int:
    """距离场 → 抗锯齿 alpha（实心半宽 width_px/2，边缘 ARC_AA 过渡）。"""
    d_px = d24 * SCALE
    half = width_px / 2.0
    if d_px <= half - ARC_AA:
        return 255
    if d_px >= half + ARC_AA:
        return 0
    return round(255 * (half + ARC_AA - d_px) / (2.0 * ARC_AA))


def build_pixels() -> bytearray:
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)  # filter: None
        for x in range(SIZE):
            gx = (x + 0.5) / SCALE
            gy = (y + 0.5) / SCALE
            d_arc = _dist_to_arc(gx, gy)
            d_probe = _dist_to_seg(gx, gy, 12.0, 7.5, 12.0, 16.5)
            d_tick = _dist_to_seg(gx, gy, 17.2, 6.8, 19.3, 4.7)
            a = max(
                _alpha(d_arc, RING_W_PX),
                _alpha(d_probe, PROBE_W_PX),
                _alpha(d_tick, TICK_W_PX),
            )
            raw += bytes((255, 255, 255, a))
    return raw


def write_png(path: Path) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8bit RGBA
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(build_pixels()), 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    print(f"已生成 {path}（{SIZE}x{SIZE}，深度环 template）")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    write_png(root / "apps" / "desktop" / "src-tauri" / "icons" / "tray.png")
