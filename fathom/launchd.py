"""launchd 任务管理：生成并安装两个 plist。

- scan：每日 SCAN_HOUR 点执行扫描 + 日报（com.maoscripts.fathom-scan）
- web ：常驻 FastAPI 服务，崩溃自动拉起（com.maoscripts.fathom-web）

plist 中 ProgramArguments 使用当前 venv 的 python 绝对路径——
venv 绑定 homebrew Python 3.14，brew 大版本升级后需重建 venv 并重装（见 README 故障排查）。
"""

from __future__ import annotations

import plistlib
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


def _argv_xml(argv: list[str]) -> str:
    """ProgramArguments 数组段：dev 态与发行态（ISS-010B）共用同一发射器。"""
    return (
        "    <key>ProgramArguments</key>\n    <array>\n"
        + "".join(f"        <string>{arg}</string>\n" for arg in argv)
        + "    </array>\n"
    )


def _scan_plist_argv(argv: list[str], hour: int, minute: int, logs_dir: Path) -> str:
    """扫描计划 plist（argv/时间/日志目录参数化；dev 与发行态同源）。"""
    xml = _PLIST_HEAD
    xml += _pair("Label", config.SCAN_LABEL)
    xml += _argv_xml(argv)
    xml += (
        "    <key>StartCalendarInterval</key>\n    <dict>\n"
        f"        <key>Hour</key>\n        <integer>{hour}</integer>\n"
        f"        <key>Minute</key>\n        <integer>{minute}</integer>\n    </dict>\n"
    )
    xml += "    <key>ProcessType</key>\n    <string>Background</string>\n"
    xml += _pair("StandardOutPath", str(logs_dir / "launchd-scan.out.log"))
    xml += _pair("StandardErrorPath", str(logs_dir / "launchd-scan.err.log"))
    xml += _PLIST_TAIL
    return xml


def _web_plist_argv(argv: list[str], logs_dir: Path) -> str:
    """常驻 Web plist（argv/日志目录参数化；dev 与发行态同源）。"""
    xml = _PLIST_HEAD
    xml += _pair("Label", config.WEB_LABEL)
    xml += _argv_xml(argv)
    xml += "    <key>RunAtLoad</key>\n    <true/>\n"
    xml += "    <key>KeepAlive</key>\n    <true/>\n"
    xml += "    <key>ThrottleInterval</key>\n    <integer>30</integer>\n"
    xml += "    <key>ProcessType</key>\n    <string>Background</string>\n"
    xml += _pair("StandardOutPath", str(logs_dir / "launchd-web.out.log"))
    xml += _pair("StandardErrorPath", str(logs_dir / "launchd-web.err.log"))
    xml += _PLIST_TAIL
    return xml


def _scan_plist(main_py: Path, python: str) -> str:
    return _scan_plist_argv(
        [str(python), str(main_py), "scan", "--source", "scheduled"],
        config.SCAN_HOUR,
        0,
        config.LOGS_DIR,
    )


def _web_plist(main_py: Path, python: str) -> str:
    return _web_plist_argv([str(python), str(main_py), "serve"], config.LOGS_DIR)


def _write_plist_in(directory: Path, label: str, content: str) -> Path:
    """把 plist 写入指定目录（dev 态 = ``config.LAUNCHAGENTS_DIR``，发行态 =
    调用方注入目录；ISS-010B 注册命令路径专用）。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}.plist"
    path.write_text(content, encoding="utf-8")
    return path


def _write_plist(label: str, content: str) -> Path:
    return _write_plist_in(config.LAUNCHAGENTS_DIR, label, content)


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


# ---------------------------------------------------------------------------
# ISS-010B：发行态后台注册桥（用户同意流 + launchd 写路径）。
#
# 硬边界（与父卡 ISS-010 / 切片 ISS-010B 合同一致）：
# - 真实写路径（写 plist + ``launchctl bootstrap/bootout``）**仅存在于本节**
#   的 ``register_release`` / ``unregister_release``（外加上面 dev 态的
#   ``_bootstrap``/``install``/``uninstall``，它们是 ``main.py install|
#   uninstall`` 的显式开发态入口）。这是 tests/test_launchd_autostart.py
#   grep 守护钉住的不变量：写动词出现在任何其它函数 = 探针红。
# - ``release_plan`` 与 ``dry_run_plan`` 一样是纯展示：只构造字符串清单，
#   不执行命令、不写文件。
# - 两个写函数都必须拿到 ``confirmed=True`` 才动手；``confirmed=False`` 时
#   零副作用（不建目录、零命令）。失败回滚不留半注册态：任一步失败都会
#   撤销已做的写入与 bootstrap。
# - 所有路径与执行器都可注入（``launchagents_dir``/``logs_dir``/``run``），
#   测试用 fake run + 临时目录全覆盖，绝不在测试里触碰本机
#   ~/Library/LaunchAgents 或真实 launchctl。
# - 生产入口是桌面壳 ``autostart.rs`` 的 Tauri 命令（经用户确认后调用同名
#   语义）；本节是 Python 侧同源实现，供 CLI/调试与跨语言一致性测试使用。
# ---------------------------------------------------------------------------


def _release_plist_specs(
    helper_bin: str,
    scan_hour: int,
    scan_minute: int,
    launchagents_dir: Path,
    logs_dir: Path,
) -> list[dict]:
    """两份发行态 plist 的 {label, path, content}（与 dev 态生成器同源）。"""
    scan_content = _scan_plist_argv(
        [helper_bin, "scan", "--source", "scheduled"], scan_hour, scan_minute, logs_dir
    )
    web_content = _web_plist_argv([helper_bin, "serve"], logs_dir)
    return [
        {
            "label": config.SCAN_LABEL,
            "path": launchagents_dir / f"{config.SCAN_LABEL}.plist",
            "content": scan_content,
        },
        {
            "label": config.WEB_LABEL,
            "path": launchagents_dir / f"{config.WEB_LABEL}.plist",
            "content": web_content,
        },
    ]


def _resolve_release_dirs(launchagents_dir, logs_dir) -> tuple[Path, Path]:
    """目录参数默认值与 dev 态同源（config 的 LAUNCHAGENTS_DIR/LOGS_DIR）。"""
    agents = Path(launchagents_dir) if launchagents_dir is not None else config.LAUNCHAGENTS_DIR
    logs = Path(logs_dir) if logs_dir is not None else config.LOGS_DIR
    return agents, logs


def release_plan(
    helper_bin: str,
    *,
    scan_hour: int | None = None,
    scan_minute: int = 0,
    launchagents_dir=None,
    logs_dir=None,
) -> dict:
    """发行态注册的纯展示计划：列出将写入的 plist 与将执行的命令。

    与 ``dry_run_plan`` 同形（plist_files/commands/uid_placeholder/warning），
    区别在 ProgramArguments 指向发行 helper 二进制（不再依赖 dev venv 的
    python + main.py）。**本函数不执行任何命令、不写任何文件**——审批 UI
    把内容展示给用户，确认后由 ``register_release``（经桌面壳注册命令）
    执行。
    """
    agents, logs = _resolve_release_dirs(launchagents_dir, logs_dir)
    hour = config.SCAN_HOUR if scan_hour is None else scan_hour
    specs = _release_plist_specs(helper_bin, hour, scan_minute, agents, logs)
    commands: list[list[str]] = [
        ["id", "-u"],
    ]
    for spec in specs:
        commands.append(["launchctl", "bootout", "gui/<uid>", str(spec["path"])])
        commands.append(["launchctl", "bootstrap", "gui/<uid>", str(spec["path"])])
    return {
        "plist_files": [
            {"label": s["label"], "path": str(s["path"]), "content": s["content"]} for s in specs
        ],
        "commands": commands,
        "uid_placeholder": "<uid>",
        "helper_bin": str(helper_bin),
        "warning": (
            "release_plan 仅展示将写入的 plist 与 launchctl 命令清单，"
            "不执行任何命令、不创建/修改任何文件。需用户明确确认后才会经"
            "注册命令路径执行 register_release。"
        ),
    }


def _read_uid_via(run) -> str:
    """经注入的 run 取 uid；失败返回空串（调用方据此 fail-closed）。"""
    try:
        proc = run(["id", "-u"], capture_output=True, text=True)
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def register_release(
    helper_bin: str,
    *,
    confirmed: bool,
    scan_hour: int | None = None,
    scan_minute: int = 0,
    launchagents_dir=None,
    logs_dir=None,
    run=subprocess.run,
) -> dict:
    """发行态注册写路径（唯一命中之一；需用户确认）。

    步骤：``id -u`` → 建目录 → 写两份发行态 plist → 每标签
    ``bootout``（幂等清理旧版本，忽略退出码）+ ``bootstrap``（必须 0）。
    任一步失败 → 回滚（两标签 bootout + 删除已写 plist），返回
    ``ok=False, rolled_back=True``，不留半注册态。
    """
    agents, logs = _resolve_release_dirs(launchagents_dir, logs_dir)
    hour = config.SCAN_HOUR if scan_hour is None else scan_hour
    specs = _release_plist_specs(helper_bin, hour, scan_minute, agents, logs)
    outcome: dict = {
        "ok": False,
        "action": "register",
        "confirmed": bool(confirmed),
        "plist_paths": {s["label"]: str(s["path"]) for s in specs},
        "commands": [],
        "rolled_back": False,
        "warnings": [],
    }

    def fail(error: str, *, rolled_back: bool) -> dict:
        outcome["error"] = error
        outcome["rolled_back"] = rolled_back
        return outcome

    if not confirmed:
        return fail("已拒绝：注册需要用户明确确认（confirmed=True）", rolled_back=False)

    uid = _read_uid_via(run)
    if not uid:
        return fail("无法解析当前 uid（id -u 失败），拒绝注册", rolled_back=False)

    written: list[Path] = []
    try:
        for spec in specs:
            _write_plist_in(agents, spec["label"], spec["content"])
            written.append(spec["path"])
    except OSError as exc:
        # 写文件失败：清掉本次已写的部分，零 bootstrap。
        for path in written:
            try:
                path.unlink()
            except OSError as cleanup_exc:
                outcome["warnings"].append(f"回滚清理 {path} 失败：{cleanup_exc}")
        return fail(f"写入 plist 失败：{exc}", rolled_back=bool(written))

    def launchctl(*argv: str):
        cmd = ["launchctl", *argv]
        outcome["commands"].append(cmd)
        return run(cmd, capture_output=True, text=True)

    for spec in specs:
        # 先 bootout 幂等清理旧版本（与 dev 态 _bootstrap 同形；退出码忽略）。
        launchctl("bootout", f"gui/{uid}", str(spec["path"]))
        result = launchctl("bootstrap", f"gui/{uid}", str(spec["path"]))
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            # 失败回滚：两标签都 bootout + 删两份 plist，不留半注册态。
            for rollback_spec in specs:
                launchctl("bootout", f"gui/{uid}", str(rollback_spec["path"]))
            for spec_path in (s["path"] for s in specs):
                try:
                    spec_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as cleanup_exc:
                    outcome["warnings"].append(
                        f"回滚删除 {spec_path} 失败：{cleanup_exc}"
                    )
            return fail(
                f"launchctl bootstrap 失败（{spec['label']}）：{stderr or '退出码非 0'}，已回滚",
                rolled_back=True,
            )

    outcome["ok"] = True
    return outcome


def unregister_release(
    *,
    confirmed: bool,
    launchagents_dir=None,
    run=subprocess.run,
) -> dict:
    """发行态注销写路径（唯一命中之一；需用户确认）。

    每标签：``bootout gui/<uid>/<label>``（幂等；退出码记入 warnings 不判
    失败——服务本就不存在是注销的正常态）→ 删除 plist（missing 容忍）。
    删除失败（如被目录占位）→ ``ok=False`` 并指名标签：残留 plist 会在下次
    登录被 launchd 重新加载，必须让 UI 如实显示未注销成功。
    """
    agents, _logs = _resolve_release_dirs(launchagents_dir, None)
    outcome: dict = {
        "ok": False,
        "action": "unregister",
        "confirmed": bool(confirmed),
        "plist_paths": {
            label: str(agents / f"{label}.plist")
            for label in (config.SCAN_LABEL, config.WEB_LABEL)
        },
        "commands": [],
        "rolled_back": False,
        "warnings": [],
    }

    if not confirmed:
        outcome["error"] = "已拒绝：注销需要用户明确确认（confirmed=True）"
        return outcome

    uid = _read_uid_via(run)
    if not uid:
        outcome["error"] = "无法解析当前 uid（id -u 失败），拒绝注销"
        return outcome

    unlink_errors: list[str] = []
    for label in (config.SCAN_LABEL, config.WEB_LABEL):
        outcome["commands"].append(["launchctl", "bootout", f"gui/{uid}", label])
        result = run(
            ["launchctl", "bootout", f"gui/{uid}", label],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "Could not find service" not in stderr:
                outcome["warnings"].append(
                    f"bootout {label} 退出码 {result.returncode}：{stderr or '无输出'}"
                )
        path = agents / f"{label}.plist"
        try:
            path.unlink()
        except FileNotFoundError:
            pass  # 本就没装：幂等成功
        except OSError as exc:
            unlink_errors.append(f"{label}: {exc}")

    if unlink_errors:
        outcome["error"] = (
            "注销未完成（plist 删除失败，残留文件会在下次登录被 launchd 重新加载）："
            + "；".join(unlink_errors)
        )
        return outcome
    outcome["ok"] = True
    return outcome


# ---------------------------------------------------------------------------
# ISS-016B：服务重载一致性（只读漂移检测）。
#
# 硬边界（与切片合同一致）：
# - 本节**只读**：解析已注册 scan plist 的计划时间、比较生效值；绝不写
#   文件、绝不执行任何命令（tests/test_config_service_reload.py 的静态
#   探针与 monkeypatch 守卫钉住）。
# - 不确定就报 unknown，绝不猜：plist 缺失 = not_registered（确定态）；
#   读取/解析失败、缺 StartCalendarInterval、Hour/Minute 缺失或越界 =
#   unknown。真实重装只经 010B 桥的既有 confirmed 流，本节不新增任何
#   系统写入口。
# ---------------------------------------------------------------------------


def read_registered_scan_time(launchagents_dir=None) -> dict:
    """只读解析已注册 scan plist 的计划时间（HH:MM）。

    返回 ``{"read": "ok"|"not_registered"|"unknown", "scan_time": "HH:MM"|None,
    "plist_path": str}``：

    - plist 文件不存在 → ``read="not_registered"``（未注册是确定状态）；
    - 读取失败 / XML 解析失败 / 缺 StartCalendarInterval / Hour、Minute
      缺失、类型异常或越界 / StartCalendarInterval 为数组形态（launchd
      允许但本切片不猜语义）→ ``read="unknown"``，``scan_time=None``。

    目录默认 ``config.LAUNCHAGENTS_DIR``（dev 与发行态同一路径约定）；
    可注入目录供测试隔离。任何异常都被吞掉并降级为 unknown——本函数
    绝不向调用方抛错，也绝不产生任何写入。
    """
    agents = Path(launchagents_dir) if launchagents_dir is not None else config.LAUNCHAGENTS_DIR
    path = agents / f"{config.SCAN_LABEL}.plist"
    result: dict = {"read": "unknown", "scan_time": None, "plist_path": str(path)}
    if not path.exists():
        result["read"] = "not_registered"
        return result
    try:
        parsed = plistlib.loads(path.read_bytes())
    except Exception:
        # 损坏 plist / 权限受限 / 编码异常 → unknown（绝不猜）。
        return result
    if not isinstance(parsed, dict):
        return result
    interval = parsed.get("StartCalendarInterval")
    if not isinstance(interval, dict):
        return result
    hour = interval.get("Hour")
    minute = interval.get("Minute")
    # bool 是 int 的子类（plistlib 会把 <true/> 解析成 True），必须显式拒。
    if isinstance(hour, bool) or isinstance(minute, bool):
        return result
    if not isinstance(hour, int) or not isinstance(minute, int):
        return result
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return result
    result["read"] = "ok"
    result["scan_time"] = f"{hour:02d}:{minute:02d}"
    return result


def service_reload_state(current_scan_time, registered) -> dict:
    """纯函数：当前生效计划时间 vs 已注册计划时间的一致性判定。

    ``registered`` 是 ``read_registered_scan_time`` 的返回值（或同形
    dict）。返回 ``{"state", "registered_scan_time", "current_scan_time"}``，
    state ∈ ``in_sync`` / ``drift`` / ``not_registered`` / ``unknown``：

    - ``read="not_registered"`` → not_registered；
    - ``read!="ok"`` 或 registered 形状异常 → unknown；
    - ``current_scan_time`` 不是合法 HH:MM（config 校验链的同一模式，
      见 ``config._SCAN_TIME_PATTERN``）→ unknown（绝不猜）；
    - 两者时分完全一致 → in_sync；任何不一致（含差 1 分钟）→ drift。
    """
    result: dict = {
        "state": "unknown",
        "registered_scan_time": None,
        "current_scan_time": current_scan_time if isinstance(current_scan_time, str) else None,
    }
    read = registered.get("read") if isinstance(registered, dict) else None
    if read == "not_registered":
        result["state"] = "not_registered"
        return result
    if read != "ok":
        return result  # unknown（含形状异常）：绝不猜
    registered_time = registered.get("scan_time")
    if not isinstance(registered_time, str):
        return result
    if config._SCAN_TIME_PATTERN.fullmatch(registered_time) is None:
        return result  # 注册时间本身不合法 → unknown（绝不猜）
    result["registered_scan_time"] = registered_time
    if not isinstance(current_scan_time, str):
        return result  # 生效值不是字符串（None/数值等）→ unknown
    match = config._SCAN_TIME_PATTERN.fullmatch(current_scan_time)
    if match is None:
        return result  # 生效值非法 → unknown（正常路径不会发生，边界防御）
    normalized_current = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    result["state"] = "in_sync" if normalized_current == registered_time else "drift"
    return result


__all__ = [
    "_scan_plist",
    "_web_plist",
    "install",
    "uninstall",
    "status",
    "dry_run_plan",
    "release_plan",
    "register_release",
    "unregister_release",
    "read_registered_scan_time",
    "service_reload_state",
]
