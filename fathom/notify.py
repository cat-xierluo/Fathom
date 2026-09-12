"""扫描完成通知（ISS-003）。

每次扫描落库、日报写完后，用 `osascript display notification` 弹一条
macOS 通知中心横幅：正文 = 今日 Top1 增长目录 + 卷剩余 GB；卷剩余低于
config.FREE_ALERT_GB 时标题换为告警并带声音。

设计要点：
- 通知是"锦上添花"，不是扫描链路的一环：任何异常（osascript 缺失、
  被系统通知策略拦截、内容构造失败）只追加写 logs/notify.log，绝不
  上抛——通知失败不能影响快照与日报（ISS-003 验收第 3 条）。
- launchd 任务与登录会话同用户，通常可直接弹；若被系统策略拦截，
  osascript 的 stderr 现象留在 notify.log，便于对照授权指引排查。
"""

from __future__ import annotations

import datetime as dt
import subprocess

from . import config

TITLE_DONE = "Fathom 扫描完成"
TITLE_ALERT = "Fathom 剩余空间告警"

# macOS 自带提示音，低容量告警时使用
ALERT_SOUND = "Sosumi"


def _log(msg: str) -> None:
    """追加一行到 logs/notify.log；日志本身失败也静默（不能反向影响调用方）。"""
    try:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LOGS_DIR / "notify.log", "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def _osa_quote(s: str) -> str:
    """转义 AppleScript 字符串字面量中的反斜杠与双引号。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def build_notification(diff: dict, free_bytes: int | None) -> tuple[str, str, str | None]:
    """由差分结果构造 (标题, 正文, 声音或 None)。

    - 正文：今日 Top1 增长目录（路径 + 增量）+ 卷剩余 GB；
    - 卷剩余低于 config.FREE_ALERT_GB：标题换告警并返回声音名。
    """
    from .reports import human_kb  # 局部导入：reports 顶部 import 本模块，避免循环

    grown = diff.get("grown") or []
    if grown:
        top = grown[0]  # fold_changes 已按 |delta| 降序输出
        body = f"增长最多：{top.path}（+{human_kb(top.delta_kb)}）"
    else:
        body = "今日无 1MB 以上的目录增长"
    if free_bytes is not None:
        free_gb = free_bytes / 1024**3
        body += f"，剩余 {free_gb:.1f} GB"
        if free_gb < config.FREE_ALERT_GB:
            return TITLE_ALERT, body, ALERT_SOUND
    return TITLE_DONE, body, None


def send_notification(title: str, body: str, sound: str | None = None) -> bool:
    """执行 osascript 弹通知；成功返回 True，任何失败只记日志返回 False。"""
    script = f'display notification "{_osa_quote(body)}" with title "{_osa_quote(title)}"'
    if sound:
        script += f' sound name "{sound}"'
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不上抛
        _log(f"通知发送异常：{exc!r}")
        return False
    if proc.returncode != 0:
        _log(f"通知被系统拒绝（exit={proc.returncode}）：{proc.stderr.strip()[:300]}")
        return False
    _log(f"已弹通知：{title} | {body}" + (f" | 声音 {sound}" if sound else ""))
    return True


def notify_scan_done(diff: dict, free_bytes: int | None) -> bool:
    """扫描完成后调用：构造内容并弹通知。永不抛出。"""
    try:
        title, body, sound = build_notification(diff, free_bytes)
    except Exception as exc:  # noqa: BLE001
        _log(f"通知构造异常：{exc!r}")
        return False
    return send_notification(title, body, sound)
