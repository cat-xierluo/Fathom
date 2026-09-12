"""launchd 任务管理：生成并安装两个 plist。

- scan：每日 SCAN_HOUR 点执行扫描 + 日报（com.maoscripts.fathom-scan）
- web ：常驻 FastAPI 服务，崩溃自动拉起（com.maoscripts.fathom-web）

plist 中 ProgramArguments 使用当前 venv 的 python 绝对路径——
venv 绑定 homebrew Python 3.14，brew 大版本升级后需重建 venv 并重装（见 README 故障排查）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import config

_PLIST_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
"""

_PLIST_TAIL = "</dict>\n</plist>\n"


def _pair(key: str, value: str) -> str:
    return f"    <key>{key}</key>\n    <string>{value}</string>\n"


def _scan_plist(main_py: Path, python: str) -> str:
    xml = _PLIST_HEAD
    xml += _pair("Label", config.SCAN_LABEL)
    xml += (
        "    <key>ProgramArguments</key>\n    <array>\n"
        f"        <string>{python}</string>\n"
        f"        <string>{main_py}</string>\n"
        "        <string>scan</string>\n    </array>\n"
    )
    xml += (
        "    <key>StartCalendarInterval</key>\n    <dict>\n"
        f"        <key>Hour</key>\n        <integer>{config.SCAN_HOUR}</integer>\n"
        "        <key>Minute</key>\n        <integer>0</integer>\n    </dict>\n"
    )
    xml += "    <key>ProcessType</key>\n    <string>Background</string>\n"
    xml += _pair("StandardOutPath", str(config.LOGS_DIR / "launchd-scan.out.log"))
    xml += _pair("StandardErrorPath", str(config.LOGS_DIR / "launchd-scan.err.log"))
    xml += _PLIST_TAIL
    return xml


def _web_plist(main_py: Path, python: str) -> str:
    xml = _PLIST_HEAD
    xml += _pair("Label", config.WEB_LABEL)
    xml += (
        "    <key>ProgramArguments</key>\n    <array>\n"
        f"        <string>{python}</string>\n"
        f"        <string>{main_py}</string>\n"
        "        <string>serve</string>\n    </array>\n"
    )
    xml += "    <key>RunAtLoad</key>\n    <true/>\n"
    xml += "    <key>KeepAlive</key>\n    <true/>\n"
    xml += "    <key>ThrottleInterval</key>\n    <integer>30</integer>\n"
    xml += "    <key>ProcessType</key>\n    <string>Background</string>\n"
    xml += _pair("StandardOutPath", str(config.LOGS_DIR / "launchd-web.out.log"))
    xml += _pair("StandardErrorPath", str(config.LOGS_DIR / "launchd-web.err.log"))
    xml += _PLIST_TAIL
    return xml


def _write_plist(label: str, content: str) -> Path:
    config.LAUNCHAGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.LAUNCHAGENTS_DIR / f"{label}.plist"
    path.write_text(content, encoding="utf-8")
    return path


def _bootstrap(path: Path) -> None:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    # 先 bootout 幂等清理旧版本
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(path)],
        capture_output=True, text=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"launchctl bootstrap 输出：{result.stderr.strip()}")


def install() -> None:
    config.ensure_runtime_dirs()
    main_py = config.PROJECT_ROOT / "main.py"
    python = sys.executable

    scan_path = _write_plist(config.SCAN_LABEL, _scan_plist(main_py, python))
    web_path = _write_plist(config.WEB_LABEL, _web_plist(main_py, python))
    _bootstrap(scan_path)
    _bootstrap(web_path)
    print(f"已安装定时扫描（每日 {config.SCAN_HOUR}:00）：{scan_path}")
    print(f"已安装常驻 Web 服务：{web_path}")
    print(f"界面地址：http://{config.HOST}:{config.PORT}")


def uninstall() -> None:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    for label in (config.SCAN_LABEL, config.WEB_LABEL):
        path = config.LAUNCHAGENTS_DIR / f"{label}.plist"
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", label], capture_output=True, text=True
        )
        if path.exists():
            path.unlink()
        print(f"已卸载：{label}")
