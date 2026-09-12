#!/usr/bin/env python3
"""生成菜单栏 tray 图标（纯 stdlib PNG 编码，无需 PIL）。

图案：44x44 template 风格（透明底 + 白色图形，macOS 自动适配深浅菜单栏）——
外环雷达 + 中心点，呼应"哨兵"。
输出：apps/desktop/src-tauri/icons/tray.png
"""

import struct
import zlib
from pathlib import Path

SIZE = 44
CX = CY = SIZE / 2
R_OUT, R_IN = 19.5, 15.5   # 外环
R_DOT = 4.5                # 中心点
ARC = 1.0                  # 简易抗锯齿半径容差


def _alpha(dist: float, r: float) -> int:
    """距环中心线 r 的点，按距离容差给出 0-255 抗锯齿 alpha。"""
    d = abs(dist - r)
    if d >= ARC:
        return 0
    return round(255 * (1 - d / ARC))


def build_pixels() -> bytearray:
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)  # filter: None
        for x in range(SIZE):
            dist = ((x - CX + 0.5) ** 2 + (y - CY + 0.5) ** 2) ** 0.5
            a = max(_alpha(dist, (R_OUT + R_IN) / 2), _alpha(dist, 0) if R_DOT == 0 else 0)
            # 中心点
            if dist <= R_DOT + ARC:
                dot_a = round(255 * min(1, (R_DOT + ARC - dist) / (2 * ARC)))
                a = max(a, dot_a)
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
