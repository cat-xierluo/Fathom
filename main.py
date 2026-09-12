#!/usr/bin/env python3
"""容量哨兵入口：python main.py <scan|report|bigfiles|status|serve|install|uninstall>"""

from disk_sentinel.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
