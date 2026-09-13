#!/usr/bin/env python3
"""ISS-029 冻结冒烟入口：把生产 fathom CLI 包成 PyInstaller 目标。

仅实验用。生产入口仍是仓库根 main.py；本文件让 smoke 脚本在不改生产代码的
前提下冻结真实 fathom 包（含 fathom.api 的 uvicorn 字符串导入路径，
见 findings.md 缺口 G1 的 hidden-import 反例）。
"""

import sys

from fathom.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
