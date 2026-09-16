"""扫描结果通知（ISS-003 / ISS-003A）。

扫描落库、日报写完后，用 `osascript display notification` 弹一条 macOS
通知中心横幅。通知按扫描结果分四态（ISS-003A 语义统一）：

- 首扫（无同数据集基线）："首次快照已建立，下次扫描起可比较"，
  不出现 0 变化 / 空差分式误导；
- 完成对比：今日 Top1 增长目录 + 卷剩余 GB；零变化明说"与上次相比无变化"；
- 部分覆盖（collection_status 非 full）：正文注明缺口，不夸大也不隐藏；
- 中断/超时：绝不发"完成"文案，只发"扫描已中断，保留上次快照"。

卷剩余低于 config.FREE_ALERT_GB（唯一阈值来源）时标题换告警并带声音。

设计要点：
- 通知是"锦上添花"，不是扫描链路的一环：任何异常（osascript 缺失、
  被系统通知策略拦截、内容构造失败）只追加写 logs/notify.log，绝不
  上抛——通知失败不能影响快照与日报（ISS-003 验收第 3 条）。
- launchd 任务与登录会话同用户，通常可直接弹；若被系统策略拦截，
  osascript 的 stderr 现象留在 notify.log，便于对照授权指引排查。
- macOS 会截断过长正文：BODY_MAX_CHARS 是最终正文（含"剩余 X GB"
  后缀）的硬上限，截断以省略号标记且保留剩余空间信息完整（ISS-003A）。
"""

from __future__ import annotations

import datetime as dt
import subprocess

from . import config

TITLE_DONE = "Fathom 扫描完成"
TITLE_FIRST = "Fathom 首次快照已建立"
TITLE_INTERRUPTED = "Fathom 扫描已中断"
TITLE_ALERT = "Fathom 剩余空间告警"

# macOS 自带提示音，低容量告警时使用
ALERT_SOUND = "Sosumi"

# macOS 通知正文超过约 200 字符会被系统截断，因此这是最终正文的
# 硬上限（含"剩余 X GB"后缀）。
BODY_MAX_CHARS = 200


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


def _clip(text: str, limit: int) -> str:
    """超长截断：保留前 limit-1 字符并以省略号结尾；不超长则原样。"""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def _compose_body(main: str, free_bytes: int | None) -> str:
    """折叠空白并限长，有卷数据时追加剩余空间后缀。

    截断只作用于主文案：剩余空间是低空间告警的核心事实，先按后缀
    长度预留额度再截断，保证最终正文（含后缀）不超过 BODY_MAX_CHARS。
    """
    body = " ".join(main.split())
    if free_bytes is None:
        return _clip(body, BODY_MAX_CHARS)
    suffix = f"，剩余 {free_bytes / 1024**3:.1f} GB"
    return _clip(body, BODY_MAX_CHARS - len(suffix)) + suffix


def _partial_note(collection_status: str | None, denied_count: int) -> str:
    """partial 采集的正文注明；full 与 NULL（v3 前旧口径）不注明。"""
    if not collection_status or collection_status == "full":
        return ""
    if denied_count:
        return f"部分覆盖（{denied_count} 处权限受限）"
    return "部分覆盖（瞬时读取错误）"


def _finish(
    title_normal: str, main: str, free_bytes: int | None
) -> tuple[str, str, str | None]:
    """拼正文并在低空间时换告警标题；阈值只读 config.FREE_ALERT_GB。"""
    body = _compose_body(main, free_bytes)
    if free_bytes is not None and free_bytes / 1024**3 < config.FREE_ALERT_GB:
        return TITLE_ALERT, body, ALERT_SOUND
    return title_normal, body, None


def build_notification(
    diff: dict,
    free_bytes: int | None,
    *,
    collection_status: str | None = "full",
    denied_count: int = 0,
) -> tuple[str, str, str | None]:
    """由差分结果构造 (标题, 正文, 声音或 None)。

    - 正文：今日 Top1 增长目录（路径 + 增量）+ 卷剩余 GB；grown 与
      added 均为空时明说"与上次相比无变化"（ISS-003A）；
    - partial 采集（collection_status 非 full）注明覆盖缺口（ISS-003A）；
    - 卷剩余低于 config.FREE_ALERT_GB：标题换告警并返回声音名。
    """
    from .reports import human_kb  # 局部导入：reports 顶部 import 本模块，避免循环

    grown = diff.get("grown") or []
    added = diff.get("added") or []
    parts: list[str] = []
    if grown:
        top = grown[0]  # fold_changes 已按 |delta| 降序输出
        parts.append(f"增长最多：{top.path}（+{human_kb(top.delta_kb)}）")
    elif added:
        parts.append("已记录目录无 1MB 以上增长")
    else:
        parts.append("与上次相比无变化：已记录目录无 1MB 以上增长")
    if added:
        top_added = max(added, key=lambda item: item.delta_kb)
        parts.append(f"首次记录大目录：{top_added.path}（{human_kb(top_added.new_kb)}）")
    partial = _partial_note(collection_status, denied_count)
    if partial:
        parts.append(partial)
    return _finish(TITLE_DONE, "；".join(parts), free_bytes)


def build_first_notification(
    free_bytes: int | None,
    *,
    collection_status: str | None = "full",
    denied_count: int = 0,
) -> tuple[str, str, str | None]:
    """首扫（无同数据集基线）通知：明说这是首次快照，下次起才可比较。"""
    parts = ["首次快照已建立，下次扫描起可比较"]
    partial = _partial_note(collection_status, denied_count)
    if partial:
        parts.append(partial)
    return _finish(TITLE_FIRST, "；".join(parts), free_bytes)


def build_interrupted_notification(reason: str | None) -> tuple[str, str, str | None]:
    """中断/超时通知：绝不使用"完成"文案，说明已保留上次快照。"""
    main = "扫描已中断，保留上次快照"
    if reason:
        main = f"{main}：{reason}"
    # 换行不能伪造另一段状态；中断原因同样受正文上限约束。
    return TITLE_INTERRUPTED, _clip(" ".join(main.split()), BODY_MAX_CHARS), None


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
    _log(f"通知命令已提交（显示结果由系统决定）：{title} | {body}" + (f" | 声音 {sound}" if sound else ""))
    return True


def notify_scan_done(
    diff: dict,
    free_bytes: int | None,
    *,
    collection_status: str | None = "full",
    denied_count: int = 0,
) -> bool:
    """扫描完成后调用：构造内容并弹通知。永不抛出。"""
    try:
        title, body, sound = build_notification(
            diff, free_bytes,
            collection_status=collection_status, denied_count=denied_count,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"通知构造异常：{exc!r}")
        return False
    return send_notification(title, body, sound)


def notify_first_snapshot(
    free_bytes: int | None,
    *,
    collection_status: str | None = "full",
    denied_count: int = 0,
) -> bool:
    """首扫（无同数据集基线）完成后调用：发"首次快照"通知。永不抛出。"""
    try:
        title, body, sound = build_first_notification(
            free_bytes,
            collection_status=collection_status, denied_count=denied_count,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"通知构造异常：{exc!r}")
        return False
    return send_notification(title, body, sound)


def notify_scan_interrupted(reason: str | None = None) -> bool:
    """扫描被取消/超时后调用：只发"已中断"通知，绝不发"完成"。永不抛出。"""
    try:
        title, body, sound = build_interrupted_notification(reason)
    except Exception as exc:  # noqa: BLE001
        _log(f"通知构造异常：{exc!r}")
        return False
    return send_notification(title, body, sound)
