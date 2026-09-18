#!/usr/bin/env python3
"""生成 DMG 安装窗口背景图（未签名提示，ISS-009 安装引导）。

用户 2026-09-18 决策：v0.3.0 不做 Developer ID 签名/公证（参照 Folia
分发模式），改为在 DMG 安装界面提示首次打开的 Gatekeeper 放行方法。

背景图承载三类内容：
1. 标题区：Fathom + 一句话说明；
2. 拖拽区：app 与 Applications 快捷方式的摆放坐标由 tauri.conf.json 的
   ``dmg.appPosition/linkPosition`` 决定，图上只画箭头与引导文字；
3. 提示区：未签名说明 + 右键打开 / 隐私与安全性放行两步指引。

依赖：宿主 python3 + Pillow（仅本脚本再生成时需要；生成产物
``dmg-background.png`` 已入库，日常构建不触发本脚本——Tauri 直接引用
该 PNG）。字体用系统自带 Hiragino Sans GB（PingFang.ttc 无法被
Pillow 打开）。

用法：
  python3 scripts/build_dmg_background.py [--force]
退出码：0 成功；3 缺 Pillow；4 字体缺失。
产物已存在时默认跳过；若脚本文件比背景图新（文案/坐标可能已改）则打印
再生成警告——确认后用 --force 重生成并将新 PNG 一并提交入库。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "apps/desktop/src-tauri/icons/dmg-background.png"

W, H = 660, 480
BG = (28, 30, 36)
INK = (240, 242, 245)
INK_DIM = (168, 174, 184)
INK_ACCENT = (120, 190, 160)
RULE = (56, 60, 68)

FONT_CANDIDATES = [
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/STHeiti Light.ttc", 0),
]

# 与 tauri.conf.json 的 dmg.appPosition/linkPosition 保持一致（图标左上角）。
APP_POS = (170, 170)
LINK_POS = (360, 170)
ICON = 128  # Finder 图标视觉边长
DRAG_CY = APP_POS[1] + ICON // 2  # 拖拽轴线 y


def main() -> int:
    force = "--force" in sys.argv
    if OUT.exists() and not force:
        # reviewer 护栏：脚本比产物新时提示再生成，避免改文案后旧图静默入库
        if Path(__file__).stat().st_mtime > OUT.stat().st_mtime:
            print("[dmg-bg] 警告：脚本比背景图新，文案/坐标可能已变——"
                  "请 python3 scripts/build_dmg_background.py --force 重生成并把新 PNG 一并提交")
        else:
            print(f"[dmg-bg] 已存在 {OUT}（--force 重生成）")
        return 0

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[dmg-bg] 缺 Pillow：pip3 install Pillow 后重试（仅再生成需要）", file=sys.stderr)
        return 3

    def font(size: int):
        for path, idx in FONT_CANDIDATES:
            if Path(path).exists():
                return ImageFont.truetype(path, size, index=idx)
        print("[dmg-bg] 系统无可用中文字体（Hiragino/STHeiti）", file=sys.stderr)
        return None  # type: ignore[return-value]

    f_title = font(34)
    f_sub = font(15)
    f_drag = font(14)
    f_note_title = font(14)
    f_note = font(13)
    if f_title is None:
        return 4

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # ---- 标题区 ----
    d.text((32, 36), "Fathom", font=f_title, fill=INK)
    d.text((34, 88), "本机目录容量历史追踪（macOS）", font=f_sub, fill=INK_DIM)

    # ---- 拖拽区：箭头 + 引导文字（图标本体由 Finder 摆放）----
    x0 = APP_POS[0] + ICON + 12   # 箭头起点：app 图标右侧
    x1 = LINK_POS[0] - 12          # 箭头终点：Applications 左侧
    ay = DRAG_CY
    d.line((x0, ay, x1 - 14, ay), fill=INK_ACCENT, width=4)
    d.polygon([(x1, ay), (x1 - 16, ay - 9), (x1 - 16, ay + 9)], fill=INK_ACCENT)
    label = "按住 Fathom 拖到右侧 Applications 文件夹"
    lw = d.textlength(label, font=f_drag)
    d.text(((W - lw) / 2, APP_POS[1] + ICON + 18), label, font=f_drag, fill=INK_DIM)

    # ---- 提示区（用户决策：未签名提示放安装界面）----
    ry = 336
    d.line((32, ry, W - 32, ry), fill=RULE, width=1)
    d.text((32, ry + 16), "首次打开提示", font=f_note_title, fill=INK_ACCENT)
    lines = [
        "本应用未经 Apple 签名与公证，首次打开可能被 macOS 阻止并提示「无法验证开发者」。",
        "放行方法：右键点按 Applications 里的 Fathom，在菜单中选「打开」；",
        "或打开 系统设置 → 隐私与安全性，在列表下方点「仍要打开」。",
    ]
    y = ry + 42
    for line in lines:
        d.text((32, y), line, font=f_note, fill=INK_DIM)
        y += 24

    img.save(OUT, "PNG")
    print(f"[dmg-bg] 已生成 {OUT}（{W}x{H}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
