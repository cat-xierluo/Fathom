#!/usr/bin/env python3
"""生成 Fathom 正式应用图标（ISS-045：层叠深潭，1024 源）。

用户 2026-09-19 在 Logo 设计会话选中第二轮「层叠深潭」并要求只取核心
作为 Logo（来源与取舍见 assets/brand/README.md、DEC-023）：App 图标 =
原稿去掉外部暖灰环境背景后的完整底板构图（象牙白 squircle 底板 + 四层
蓝青等深阶地 + 午夜蓝中心 + 顶部测深刻痕），不是线条深度环——后者保留
在界面小尺寸场景（侧栏字标/加载指示/favicon，见 frontend/icons.js 与
frontend/favicon.svg）。

处理链（纯 Pillow 像素操作，无生成模型调用，可重复）：
  1. 读 assets/brand/fathom-approved-concept.png（1254x1254 展示稿）；
  2. 从四角采样暖灰背景色，BFS flood fill 清除背景（容差 28）；
  3. 取非透明像素 bbox 居中方形裁剪，缩放到 1024（底板满幅、四角透明，
     圆角约 17%，接近 macOS 图标网格）；
  4. 清理边缘白雾（alpha<36 → 0）。

依赖：宿主 python3 + Pillow（仅再生成需要；产物 icon.png 入库，日常构建
不触发本脚本）。原稿是内置 image_gen 的生成物，权利说明见
assets/brand/README.md。

用法：
  python3 scripts/build_app_icon.py [--force]
退出码：0 成功；3 缺 Pillow；4 原稿缺失。
"""

import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets/brand/fathom-approved-concept.png"
OUT = ROOT / "apps/desktop/src-tauri/icons/icon.png"

SIZE = 1024
BG_TOL = 28          # 背景 flood fill 容差（欧氏距离平方阈值 = tol²）
ALPHA_MIN = 36       # 低于此 alpha 的边缘白雾清零


def main() -> int:
    if OUT.exists() and "--force" not in sys.argv:
        print(f"[app-icon] 已存在 {OUT}（--force 重生成）")
        return 0
    if not SRC.exists():
        print(f"[app-icon] FAIL：原稿缺失 {SRC}", file=sys.stderr)
        return 4
    try:
        from PIL import Image
    except ImportError:
        print("[app-icon] 缺 Pillow：pip3 install Pillow 后重试（仅再生成需要）", file=sys.stderr)
        return 3

    src = Image.open(SRC).convert("RGBA")
    W, H = src.size
    px = src.load()

    # 1. 背景色 = 四角均值
    corners = [px[5, 5], px[W - 6, 5], px[5, H - 6], px[W - 6, H - 6]]
    bg = tuple(sum(c[i] for c in corners) // 4 for i in range(3))

    def bg_like(c):
        r, g, b = c[:3]
        return (r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2 < BG_TOL ** 2

    # 2. BFS flood fill 自边界清背景
    visited = bytearray(W * H)
    q = deque()
    for x in range(W):
        q.append((x, 0)); q.append((x, H - 1))
    for y in range(H):
        q.append((0, y)); q.append((W - 1, y))
    cleared = 0
    while q:
        x, y = q.popleft()
        if x < 0 or y < 0 or x >= W or y >= H or visited[y * W + x]:
            continue
        visited[y * W + x] = 1
        if not bg_like(px[x, y]):
            continue
        px[x, y] = (0, 0, 0, 0)
        cleared += 1
        q.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

    # 3. bbox 方形裁剪 → 1024
    xs, ys, xe, ye = W, H, 0, 0
    for y in range(H):
        for x in range(W):
            if px[x, y][3] > 8:
                if x < xs: xs = x
                if x > xe: xe = x
                if y < ys: ys = y
                if y > ye: ye = y
    side = max(xe - xs + 1, ye - ys + 1)
    cx, cy = (xs + xe) // 2, (ys + ye) // 2
    half = side // 2
    icon = src.crop((cx - half, cy - half, cx - half + side, cy - half + side))
    icon = icon.resize((SIZE, SIZE), Image.LANCZOS)

    # 4. 边缘白雾清理
    p2 = icon.load()
    fog = 0
    for y in range(SIZE):
        for x in range(SIZE):
            r, g, b, a = p2[x, y]
            if 0 < a < ALPHA_MIN:
                p2[x, y] = (r, g, b, 0)
                fog += 1

    icon.save(OUT, "PNG")
    print(
        f"[app-icon] 已生成 {OUT}（{SIZE}x{SIZE}，层叠深潭/象牙白底板；"
        f"背景清除 {cleared} 像素，白雾清理 {fog} 像素）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
