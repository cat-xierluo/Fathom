#!/usr/bin/env python3
"""生成 Fathom 正式应用图标（ISS-045：深度环 App 图标，1024 源）。

用户 2026-09-18 确认 Logo 方向 = R3 深度环（与 frontend/icons.js 的
brandRing 同构）：开放圆环（右上缺口）+ 中心探针 + 跨缺口短刻度。

几何与 brandRing（24 viewBox，中心 12,12，环半径 8.5，stroke 2）逐参数
对应，本脚本按 scale = 1024/24 映射；canonical 声明在同目录 icon.svg。

构图：海沟蓝（#345d7f）squircle 底 + 白色环与探针 + 亮矿物青刻度
（#3e7e7c 在深底上对比不足，图标用亮阶 #82c8c2）。小尺寸下仅环+探针
可辨，属预期层级。

依赖：宿主 python3 + Pillow（同 build_dmg_background.py，仅再生成需要；
产物 icon.png 入库，日常构建不触发本脚本）。

用法：
  python3 scripts/build_app_icon.py [--force]
退出码：0 成功；3 缺 Pillow。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "apps/desktop/src-tauri/icons/icon.png"

SIZE = 1024
SCALE = SIZE / 24.0            # brandRing 的 24 viewBox → 1024 画布
TRENCH = (52, 93, 127, 255)    # #345d7f
WHITE = (255, 255, 255, 255)
MINERAL_BRIGHT = (130, 200, 194, 255)  # #82c8c2（#3e7e7c 图标亮阶）

# squircle 圆角：macOS 图标网格 ≈ 18% 边长；透明四角（iconutil 不加 mask）
RADIUS = int(SIZE * 0.180)
MARGIN = 0                      # 底满幅；图形区域由 brandRing 几何自带留白

# brandRing 几何（度数以图像坐标系 atan2 计算，Pillow 角度自 3 点钟顺时针）：
# 环起 (20,9.1)=-20°，环终 (14.9,4)≈-70°，SVG sweep=1 大弧顺时针跨 310°
ARC_START, ARC_END = -20, 290
RING_R = 8.5 * SCALE
RING_W = int(2.4 * SCALE)       # stroke 2 → 图标加粗到 2.4 提升小尺寸可辨性


def main() -> int:
    if OUT.exists() and "--force" not in sys.argv:
        print(f"[app-icon] 已存在 {OUT}（--force 重生成）")
        return 0
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("[app-icon] 缺 Pillow：pip3 install Pillow 后重试（仅再生成需要）", file=sys.stderr)
        return 3

    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # squircle 底（透明角）
    d.rounded_rectangle(
        [MARGIN, MARGIN, SIZE - 1 - MARGIN, SIZE - 1 - MARGIN],
        radius=RADIUS, fill=TRENCH,
    )

    cx = cy = SIZE / 2
    ring_bbox = [cx - RING_R, cy - RING_R, cx + RING_R, cy + RING_R]

    def pt(x24, y24):
        return (x24 * SCALE, y24 * SCALE)

    # 开放圆环（右上缺口，方向与 brandRing 一致）
    d.arc(ring_bbox, start=ARC_START, end=ARC_END, fill=WHITE, width=RING_W)

    # 中心探针 (12,7.5)-(12,16.5)
    d.line([pt(12, 7.5), pt(12, 16.5)], fill=WHITE, width=int(2.4 * SCALE))

    # 跨缺口短刻度 (17.2,6.8)-(19.3,4.7)，亮矿物青
    d.line([pt(17.2, 6.8), pt(19.3, 4.7)], fill=MINERAL_BRIGHT, width=int(2.2 * SCALE))

    img.save(OUT, "PNG")
    print(f"[app-icon] 已生成 {OUT}（{SIZE}x{SIZE}，深度环/海沟蓝底）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
