#!/usr/bin/env python3
"""生成菜单栏 tray 图标（纯 stdlib PNG 编码，无需 PIL）。

图案：44x44 template 风格（透明底 + 白色图形，macOS 自动适配深浅菜单栏）——
外环雷达 + 中心点，呼应"哨兵"。
线宽/留白：环为实心带（非发丝线），44px 画布在菜单栏 22pt 高度下约 2pt 线宽，
外缘各留 2.5px 空白，保证深浅色下均清晰。
输出：apps/desktop/src-tauri/icons/tray.png
"""

import struct
import zlib
from pathlib import Path

SIZE = 44
CX = CY = SIZE / 2
R_OUT, R_IN = 18.5, 14.5   # 外环实心带（浅色菜单栏下发丝线近乎不可见，需足够线宽）
R_DOT = 5.0                # 中心点
ARC = 1.0                  # 边缘抗锯齿半宽


def _band_alpha(dist: float, lo: float, hi: float) -> int:
    """实心环带 [lo, hi]，内外边缘各 ARC 半宽线性抗锯齿。"""
    if dist <= lo - ARC or dist >= hi + ARC:
        return 0
    if dist < lo:
        return round(255 * (dist - (lo - ARC)) / (2 * ARC))
    if dist > hi:
        return round(255 * ((hi + ARC) - dist) / (2 * ARC))
    return 255


def build_pixels() -> bytearray:
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)  # filter: None
        for x in range(SIZE):
            dist = ((x - CX + 0.5) ** 2 + (y - CY + 0.5) ** 2) ** 0.5
            a = max(_band_alpha(dist, R_IN, R_OUT), _band_alpha(dist, 0.0, R_DOT))
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
    print(f"已生成 {path}（{SIZE}x{SIZE}）")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    write_png(root / "apps" / "desktop" / "src-tauri" / "icons" / "tray.png")
