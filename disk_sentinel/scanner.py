"""扫描器：调用系统 du 生成目录快照并写入 SQLite。

设计要点（见 docs/DECISIONS.md DEC-002 / DEC-005）：
- 引擎用系统 `du -xk`（BSD du，macOS 自带）：输出天然是"目录 -> 累计大小"，
  无需自己遍历文件系统；-x 不跨挂载点，避免容量虚增。
- 只持久化 >= min_kb 的目录：1100 万文件的盘中目录数以十万计，
  全存会让数据库在 23GB 剩余空间下成为新的负担；小目录对"哪个文件夹
  冒出来了"这一问题没有回答价值。
- 同一天重复扫描会覆盖当天的旧快照，保证"一天一行"的趋势语义。
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from . import config

# BSD du 对文件名中的非打印字符输出八进制转义（如 \346\226\207），tab 也会被转义
_OCTAL_RE = re.compile(r"\\([0-7]{1,3})")


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


def run_du(root: Path) -> tuple[dict[str, int], int]:
    """执行 du -xk，返回 (目录路径 -> 大小KB, 无权限/失败目录数)。"""
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
    _ = elapsed  # du_seconds 由调用方统计
    return sizes, denied


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
    """
    root = Path(root) if root else config.DEFAULT_ROOT
    min_kb = config.MIN_DIR_KB if min_kb is None else min_kb
    root_str = str(root)

    sizes, denied = run_du(root)
    total_kb = sizes.get(root_str, 0)
    kept = [(p, s) for p, s in sizes.items() if s >= min_kb]

    now = dt.datetime.now()
    with conn:
        _drop_same_day(conn, now.strftime("%Y-%m-%d"), root_str)
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, total_kb) "
            "VALUES (?,?,?,?,?,?)",
            (now.isoformat(timespec="seconds"), root_str, len(sizes), denied, 0.0, total_kb),
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
