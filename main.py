#!/usr/bin/env python3
"""Fathom入口：python main.py <scan|report|bigfiles|status|serve|install|uninstall>"""

from fathom.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
