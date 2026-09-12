"""扫描器：调用系统 du 生成目录快照并写入 SQLite。

设计要点（见 docs/DECISIONS.md DEC-002 / DEC-005）：
- 引擎用系统 `du -xk`（BSD du，macOS 自带）：输出天然是"目录 -> 累计大小"，
  无需自己遍历文件系统；-x 不跨挂载点，避免容量虚增。
- 只持久化 >= min_kb 的目录：1100 万文件的盘中目录数以十万计，
  全存会让数据库在 23GB 剩余空间下成为新的负担；小目录对"哪个文件夹
  冒出来了"这一问题没有回答价值。
- 同一天重复扫描会覆盖当天的旧快照，保证"一天一行"的趋势语义。
- 采集有效性（ISS-018，反例 AUD-01）：du 输出里必须能找到根目录记录才算
  有效采集——本机实测 BSD du 对"部分权限失败"（根行存在，exit=1）与
  "致命失败"（根缺失，exit=1）都返回非零，退出码本身无法区分两者，
  根记录才是唯一可靠锚点。无效采集在进入同日替换事务前被拒绝并抛
  InvalidScanError，当日旧 snapshot/entries/volume_stats 原样保留。
- 采集质量的持久化边界：现有 schema 只有 denied_count/du_seconds 两列，
  退出码、stderr 摘要等质量细节只存在于当次 DuResult，不伪造未知元数据；
  完整质量元数据的持久化随 ISS-025 的 schema 工作补齐。
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

# BSD du 对文件名中的非打印字符输出八进制转义（如 \346\226\207），tab 也会被转义
_OCTAL_RE = re.compile(r"\\([0-7]{1,3})")


class InvalidScanError(RuntimeError):
    """du 采集无效（致命退出/空输出/缺根记录），本次扫描已被整体拒绝。

    抛出时数据库没有任何写入，当日旧快照不受影响。
    """


@dataclass(frozen=True)
class DuResult:
    """一次 du 采集的结构化结果：退出码、质量线索与真实耗时。

    sizes          目录路径 -> 累计大小 KB（含根记录）
    exit_code      du 进程退出码（部分权限失败也是 1，不能单独当致命判据）
    denied_count   stderr 中权限/读取失败行数
    elapsed_seconds 本次采集的实测耗时（time.monotonic 口径）
    stderr_tail    stderr 末尾若干行，仅用于失败诊断，不持久化
    """

    sizes: dict[str, int]
    exit_code: int
    denied_count: int
    elapsed_seconds: float
    stderr_tail: tuple[str, ...] = ()

    def stderr_hint(self) -> str:
        return self.stderr_tail[-1] if self.stderr_tail else ""


def unescape_du_path(raw: str) -> str:
    """还原 BSD du 输出路径中被转义的字符。

    连续的八进制转义（如 \\346\\226\\207）是一个多字节 UTF-8 字符被逐字节
    转义的结果，必须先收集字节再整体 decode，不能逐个 chr()。
    """
    out: list[str] = []
    pending_bytes: list[int] = []
    i = 0

    def _flush() -> None:
        if pending_bytes:
            out.append(bytes(pending_bytes).decode("utf-8", errors="replace"))
            pending_bytes.clear()

    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt in ("\\", "t", "n"):
                _flush()
                out.append({"t": "\t", "n": "\n", "\\": "\\"}[nxt])
                i += 2
                continue
            m = _OCTAL_RE.match(raw, i)
            if m:
                pending_bytes.append(int(m.group(1), 8))
                i = m.end()
                continue
        _flush()
        out.append(ch)
        i += 1
    _flush()
    return "".join(out)


def run_du(root: Path) -> DuResult:
    """执行 du -xk，返回结构化采集结果（大小表、退出码、质量线索、真实耗时）。"""
    started = time.monotonic()
    proc = subprocess.run(
        ["/usr/bin/du", "-xk", str(root)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    elapsed = time.monotonic() - started
    sizes: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        size_str, sep, path = line.partition("\t")
        if not sep or not path:
            continue
        try:
            size_kb = int(size_str)
        except ValueError:
            continue
        sizes[unescape_du_path(path)] = size_kb
    denied = sum(
        1 for l in proc.stderr.splitlines() if "Operation not permitted" in l or "Permission denied" in l
    )
    return DuResult(
        sizes=sizes,
        exit_code=proc.returncode,
        denied_count=denied,
        elapsed_seconds=elapsed,
        stderr_tail=tuple(proc.stderr.splitlines()[-4:]),
    )


def classify_collection(result: DuResult, root_str: str) -> str:
    """判定一次采集是否可用于建快照；返回 'full' 或 'partial'。

    有效性锚点是根目录记录：缺失（致命退出、空输出、被信号截断、根不可读、
    根不存在）即抛 InvalidScanError，绝不进入同日替换事务。根记录存在但
    退出码非零或存在权限缺口时，按"部分覆盖"接受——缺的子树不会出现在
    entries 里，数量经 denied_count 持久化，其余质量细节留在 DuResult。
    """
    if root_str not in result.sizes:
        hint = f"；du stderr：{result.stderr_hint()}" if result.stderr_hint() else ""
        raise InvalidScanError(
            f"du 采集无效：未返回根目录记录（退出码 {result.exit_code}）{hint}。"
            "已拒绝本次写入，当日旧快照保持不变"
        )
    if result.exit_code != 0 or result.denied_count > 0:
        return "partial"
    return "full"


def _volume_stat(root: Path) -> tuple[int, int]:
    """返回根路径所在卷的 (总字节, 剩余字节)。"""
    st = os.statvfs(root)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def _drop_same_day(conn: sqlite3.Connection, day: str, root: str) -> None:
    """删除同根路径同一天的旧快照，实现"一天一行"。"""
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM snapshots WHERE root = ? AND created_at LIKE ?",
            (root, f"{day}%"),
        )
    ]
    for sid in ids:
        conn.execute("DELETE FROM entries WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM volume_stats WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM snapshots WHERE id = ?", (sid,))


def create_snapshot(
    conn: sqlite3.Connection,
    root: Path | None = None,
    min_kb: int | None = None,
) -> int:
    """执行一次完整扫描并写入快照，返回快照 id。

    min_kb 可在测试中注入小值；生产使用 config.MIN_DIR_KB。

    采集无效（致命退出/空输出/缺根记录）时抛 InvalidScanError，数据库不做
    任何写入，当日旧快照原样保留；权限受限但根记录有效时按部分覆盖落库
    （denied_count 记录缺口数量）。du_seconds 记录本次采集实测耗时。
    """
    root = Path(root) if root else config.DEFAULT_ROOT
    min_kb = config.MIN_DIR_KB if min_kb is None else min_kb
    root_str = str(root)

    result = run_du(root)
    classify_collection(result, root_str)  # 无效采集在此被拒绝，事务尚未开始
    sizes = result.sizes
    total_kb = sizes[root_str]
    kept = [(p, s) for p, s in sizes.items() if s >= min_kb]

    now = dt.datetime.now()
    with conn:
        _drop_same_day(conn, now.strftime("%Y-%m-%d"), root_str)
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, total_kb) "
            "VALUES (?,?,?,?,?,?)",
            (now.isoformat(timespec="seconds"), root_str, len(sizes),
             result.denied_count, result.elapsed_seconds, total_kb),
        )
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
            [(sid, p, s) for p, s in kept],
        )
        total_b, free_b = _volume_stat(root)
        conn.execute(
            "INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) VALUES (?,?,?)",
            (sid, total_b, free_b),
        )
    assert sid is not None
    return sid


def prune_snapshots(
    conn: sqlite3.Connection,
    keep_daily_days: int | None = None,
    keep_weekly_weeks: int | None = None,
) -> int:
    """清理旧快照：近 N 天全保留，更早的每周保留最早一份，最多 M 周。

    返回删除的快照数。
    """
    keep_daily_days = config.KEEP_DAILY_DAYS if keep_daily_days is None else keep_daily_days
    keep_weekly_weeks = config.KEEP_WEEKLY_WEEKS if keep_weekly_weeks is None else keep_weekly_weeks

    today = dt.date.today()
    daily_cutoff = (today - dt.timedelta(days=keep_daily_days)).isoformat()
    weekly_cutoff = today - dt.timedelta(weeks=keep_weekly_weeks)

    rows = conn.execute(
        "SELECT id, created_at FROM snapshots ORDER BY created_at"
    ).fetchall()

    # 每个iso周保留最早一个快照（仅对超过每日保留期的部分）；
    # 用 id 锚定而非集合标记，同时间戳的两条也能正确只留一条。
    daily_cutoff_date = dt.date.fromisoformat(daily_cutoff)
    weekly_keep_id: dict[tuple, int] = {}
    for r in rows:
        created = dt.date.fromisoformat(r["created_at"][:10])
        if daily_cutoff_date > created >= weekly_cutoff:
            iso = created.isocalendar()[:2]  # (年, 周)
            if iso not in weekly_keep_id:
                weekly_keep_id[iso] = r["id"]

    to_delete: list[int] = []
    for r in rows:
        created = dt.date.fromisoformat(r["created_at"][:10])
        if created >= daily_cutoff_date:
            continue  # 近 N 天全保留
        if created < weekly_cutoff:
            to_delete.append(r["id"])  # 超过每周保留期
        elif r["id"] != weekly_keep_id.get(created.isocalendar()[:2]):
            to_delete.append(r["id"])  # 每周非首个快照
    for sid in to_delete:
        conn.execute("DELETE FROM entries WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM volume_stats WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM snapshots WHERE id = ?", (sid,))
    conn.commit()
    return len(to_delete)
