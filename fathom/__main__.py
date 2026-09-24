#!/usr/bin/env python3
"""Fathom 入口：python -m fathom <scan|report|bigfiles|status|serve|install|uninstall>

也支持按路径直接执行（``python <repo>/fathom/__main__.py``）——launchd
开发态 plist 与 PyInstaller 冻结（scripts/build_helper.sh）即该形态，
此时仓库根不在 ``sys.path``，由下方引导补上。"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # 直接以脚本路径执行时仓库根不在 sys.path，补上以便导入 fathom 包；
    # python -m fathom 不经过该分支；PyInstaller 冻结态经过也无害
    # （frozen importer 优先，不走 sys.path 查找）。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fathom.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
