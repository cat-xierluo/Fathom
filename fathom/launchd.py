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
        "        <string>scan</string>\n"
        "        <string>--source</string>\n"
        "        <string>scheduled</string>\n    </array>\n"
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


# ISS-010A：登录项与后台计划的只读状态桥 + dry-run。
# 硬边界（与父卡 ISS-010 一致）：本节只做只读查询与 dry-run 计划；
# 绝不执行 ``launchctl bootstrap/bootout``、绝不写文件。既有 ``install`` /
# ``uninstall``/``_write_plist``/``_bootstrap`` 保持原样且不在 ``status`` /
# ``dry_run_plan`` 中调用——这是测试用 grep 断言守护的不变量。
# SMAppService（登录项）留父卡 ISS-010 后续补齐；本切片不实现。

import os as _os_for_status  # noqa: E402  -- 局部别名避免改顶部 import 顺序
import json as _json_for_dry_run  # noqa: E402
from typing import Callable as _Callable, Optional as _Optional  # noqa: E402

_STATE_RUNNING_TOKENS = ("state = running", "pid =")
_DISABLED_TOKEN = "Could not find service"


def _parse_launchctl_print(stdout: str, stderr: str, exit_code: int) -> str:
    """与 Rust 侧 ``parse_launchctl_print`` 同语义的纯函数三态映射。"""
    combined = f"{stdout}{stderr}"
    if exit_code == 0:
        if any(tok in combined for tok in _STATE_RUNNING_TOKENS):
            return "enabled"
        return "unknown"
    if _DISABLED_TOKEN in combined:
        return "disabled"
    return "unknown"


def _extract_pid(stdout: str) -> _Optional[int]:
    """从 ``launchctl print`` 输出粗略抓 ``pid = <int>``，无则 None。"""
    for line in stdout.splitlines():
        line = line.strip()
        # 形如 ``pid = 12345`` 或带缩进；只接受纯数字 pid（排除 ``pid = 0`` 等占位）。
        if line.startswith("pid =") or "\tpid =" in line or "  pid =" in line:
            tail = line.split("pid =", 1)[1].strip().split()
            if tail and tail[0].isdigit():
                pid = int(tail[0])
                if pid > 0:
                    return pid
    return None


def _extract_last_exit(stdout: str) -> _Optional[int]:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("last exit code:") or "last exit code:" in line:
            tail = line.split("last exit code:", 1)[1].strip().split()
            if tail and tail[0].lstrip("-").isdigit():
                return int(tail[0])
    return None


def status(
    run: _Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    timeout_s: float = 2.0,
) -> dict[str, dict]:
    """对两标签各做一次 ``launchctl print gui/<uid>/<label>`` 只读查询。

    返回 ``{label: {"state": "enabled|disabled|unknown", "pid": int|None,
    "last_exit": int|None, "raw_tail": str}}``。任何异常（超时、命令缺失、
    解析失败）→ 整个标签 ``state="unknown"``，绝不映射为 enabled。

    ``run`` 参数便于测试注入假实现；生产路径直接使用 ``subprocess.run``。
    """
    try:
        uid_proc = run(["id", "-u"], capture_output=True, text=True, timeout=timeout_s)
    except Exception:
        uid_proc = None
    if uid_proc is None or uid_proc.returncode != 0:
        uid = ""
    else:
        uid = (uid_proc.stdout or "").strip()

    out: dict[str, dict] = {}
    for label in (config.SCAN_LABEL, config.WEB_LABEL):
        record: dict = {
            "state": "unknown",
            "pid": None,
            "last_exit": None,
            "raw_tail": "",
        }
        if not uid:
            out[label] = record
            continue
        try:
            proc = run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            record["state"] = _parse_launchctl_print(stdout, stderr, proc.returncode)
            record["pid"] = _extract_pid(stdout)
            record["last_exit"] = _extract_last_exit(stdout)
            # 仅保留尾部以避免结果过大；前段对运维帮助有限。
            record["raw_tail"] = "\n".join(stdout.splitlines()[-20:])
        except Exception:
            # 超时 / FileNotFoundError / 其它任何异常 → 保守为 unknown。
            record["state"] = "unknown"
        out[label] = record
    return out


def dry_run_plan() -> dict:
    """dry-run：列出 ``install()`` 即将生成与执行的 plist 与命令，但不实际执行。

    返回结构（JSON 可序列化）：
    - ``plist_files``：``[{label, path, content}]``（路径与 content 与
      ``install()`` 同源——分别复用 ``_scan_plist`` / ``_web_plist``）
    - ``commands``：``[[str, ...], ...]``（``install()`` 将通过
      ``subprocess.run`` 调用的 launchctl / id 命令完整列表字符串形式）
    - ``uid``：当前 uid（``id -u`` 输出）；调用方可在审批 UI 中展示
    - ``warning``：固定文案，提醒调用方本函数不执行任何命令、不写文件

    **不变量**：本函数体内不调用 ``_bootstrap``/``_write_plist``；既有
    ``install()`` 的写入路径在 dry-run 中以字符串形式呈现，审批通过后再
    由用户显式调用 ``install()``。这是测试用 grep 守护的硬约束。
    """
    main_py = config.PROJECT_ROOT / "main.py"
    python = sys.executable
    scan_content = _scan_plist(main_py, python)
    web_content = _web_plist(main_py, python)
    scan_path = config.LAUNCHAGENTS_DIR / f"{config.SCAN_LABEL}.plist"
    web_path = config.LAUNCHAGENTS_DIR / f"{config.WEB_LABEL}.plist"

    # uid 用与 ``install()`` 相同的 ``id -u`` 命令表示（不实际执行）。
    commands: list[list[str]] = [
        ["id", "-u"],
        # _bootstrap(scan) 内部：bootout + bootstrap
        ["launchctl", "bootout", "gui/<uid>", str(scan_path)],
        ["launchctl", "bootstrap", "gui/<uid>", str(scan_path)],
        # _bootstrap(web) 内部：bootout + bootstrap
        ["launchctl", "bootout", "gui/<uid>", str(web_path)],
        ["launchctl", "bootstrap", "gui/<uid>", str(web_path)],
    ]
    return {
        "plist_files": [
            {"label": config.SCAN_LABEL, "path": str(scan_path), "content": scan_content},
            {"label": config.WEB_LABEL, "path": str(web_path), "content": web_content},
        ],
        "commands": commands,
        "uid_placeholder": "<uid>",
        "warning": (
            "dry_run_plan 仅展示将写入的 plist 与 launchctl 命令清单，"
            "不执行任何命令、不创建/修改任何文件。请用户审批后再调用 install()。"
        ),
    }


__all__ = [
    "_scan_plist",
    "_web_plist",
    "install",
    "uninstall",
    "status",
    "dry_run_plan",
]
