"""差分计算与 Markdown 日报生成。

核心问题："哪个文件夹冒出来了" —— 对比两个快照，给出：
- Top 增长 / 缩减目录（父子折叠，避免同一条链刷屏）
- 新增目录（基线中不存在）
- 消失目录（当前不存在）
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config, notify


@dataclass
class DirChange:
    path: str
    old_kb: int | None   # None 表示新增
    new_kb: int | None   # None 表示消失
    delta_kb: int        # new - old（消失时为 -old）


def load_snapshot(conn: sqlite3.Connection, sid: int) -> dict[str, int]:
    return {
        r["path"]: r["size_kb"]
        for r in conn.execute("SELECT path, size_kb FROM entries WHERE snapshot_id = ?", (sid,))
    }


def _is_ancestor(a: str, b: str) -> bool:
    """a 是否为 b 的祖先目录（同一根路径下按字符串前缀判断即可）。"""
    return b.startswith(a.rstrip("/") + "/")


class _FoldNode:
    """路径前缀节点，并缓存已选后代的同号变化量。"""

    __slots__ = ("children", "entry", "positive_kb", "negative_kb")

    def __init__(self) -> None:
        self.children: dict[str, _FoldNode] = {}
        self.entry: DirChange | None = None
        self.positive_kb = 0
        self.negative_kb = 0


class _FoldIndex:
    """支持按目录边界查询最近祖先和后代覆盖量的字符 trie。"""

    def __init__(self) -> None:
        self.root = _FoldNode()
        self.selected: dict[str, DirChange] = {}

    @staticmethod
    def _key(path: str) -> str:
        # 与 _is_ancestor 的 a.rstrip("/") 语义保持一致。
        return path.rstrip("/")

    def _find(self, prefix: str) -> _FoldNode | None:
        node = self.root
        for char in prefix:
            node = node.children.get(char)
            if node is None:
                return None
        return node

    def nearest_ancestor(self, path: str) -> DirChange | None:
        node = self.root
        nearest = None
        # 仅在下一个字符是 / 时认定目录边界，避免 /r 误匹配 /result。
        for char in path:
            if char == "/" and node.entry is not None:
                nearest = node.entry
            node = node.children.get(char)
            if node is None:
                break
        return nearest

    def descendant_covered(self, path: str, positive: bool) -> int:
        # 后代必须从 path.rstrip("/") + "/" 开始，与 _is_ancestor 完全一致。
        node = self._find(self._key(path) + "/")
        if node is None:
            return 0
        return node.positive_kb if positive else node.negative_kb

    def add(self, entry: DirChange) -> None:
        key = self._key(entry.path)
        node = self.root
        nodes = [node]
        for char in key:
            node = node.children.setdefault(char, _FoldNode())
            nodes.append(node)
        node.entry = entry
        self.selected[key] = entry
        amount = abs(entry.delta_kb)
        field = "positive_kb" if entry.delta_kb > 0 else "negative_kb"
        for current in nodes:
            setattr(current, field, getattr(current, field) + amount)

    def remove(self, entry: DirChange) -> None:
        key = self._key(entry.path)
        node = self.root
        nodes = [node]
        for char in key:
            node = node.children[char]
            nodes.append(node)
        node.entry = None
        self.selected.pop(key)
        amount = abs(entry.delta_kb)
        field = "positive_kb" if entry.delta_kb > 0 else "negative_kb"
        for current in nodes:
            setattr(current, field, getattr(current, field) - amount)


def fold_changes(changes: list[DirChange], topn: int = 25) -> list[DirChange]:
    """按 |delta| 降序选择，避免同一条链的父子重复刷屏（DEC-005）。

    - 候选有已入选祖先、且变化量达到祖先的 90%：说明变化几乎全在这条单链上，
      用候选**替换**祖先（更深层更精确）；
    - 候选有已入选祖先但变化量不足 90%：属于兄弟分支，**保留**（父 +100 由
      a +33 / b +33 / 其他 +34 构成时，四个都有定位价值）；
    - 候选无入选祖先、但有入选后代：残余量（自身变化减去同向后代已覆盖部分）
      不足 10% 或 1MB 时不入选（变化已被后代表达）。

    必须完整折叠全部候选，后续更精确的子目录才有机会替换先入选的父目录。
    内部路径 trie 用前缀和维护同号后代覆盖量，避免完整结果导致两两扫描；
    ``topn`` 只在折叠完成、稳定排序后限制最终输出。
    """
    if topn <= 0:
        return []

    index = _FoldIndex()
    for c in sorted(changes, key=lambda x: abs(x.delta_kb), reverse=True):
        if c.delta_kb == 0:
            continue
        ancestor = index.nearest_ancestor(c.path)
        if ancestor is not None:
            if abs(c.delta_kb) >= abs(ancestor.delta_kb) * 0.9:
                index.remove(ancestor)
                index.add(c)
            else:
                index.add(c)
        else:
            covered = index.descendant_covered(c.path, positive=c.delta_kb > 0)
            residual = abs(c.delta_kb) - covered
            if covered == 0 or residual >= max(1024, abs(c.delta_kb) * 0.1):
                index.add(c)
    return sorted(
        index.selected.values(), key=lambda x: abs(x.delta_kb), reverse=True
    )[:topn]


def compute_diff(
    old: dict[str, int],
    new: dict[str, int],
    topn: int = 25,
    min_delta_kb: int = 1024,        # 默认只关心 >= 1MB 的变化
    added_min_kb: int = 100 * 1024,  # 新增目录默认 >= 100MB 才列出
) -> dict:
    all_paths = set(old) | set(new)
    grown, shrunk, added, removed = [], [], [], []
    for p in all_paths:
        o, n = old.get(p), new.get(p)
        if o is None:
            if (n or 0) >= added_min_kb:
                added.append(DirChange(p, None, n, n or 0))
        elif n is None:
            removed.append(DirChange(p, o, None, -o))
        else:
            d = n - o
            if abs(d) < min_delta_kb:
                continue
            (grown if d > 0 else shrunk).append(DirChange(p, o, n, d))

    return {
        "grown": fold_changes(grown, topn),
        "shrunk": fold_changes(shrunk, topn),
        "added": sorted(added, key=lambda x: x.delta_kb, reverse=True)[:topn],
        "removed": sorted(removed, key=lambda x: x.delta_kb)[:topn],
    }


def human_kb(kb: int | None) -> str:
    if kb is None:
        return "-"
    for unit in ("KB", "MB", "GB", "TB"):
        if abs(kb) < 1024:
            return f"{kb:.1f} {unit}"
        kb /= 1024
    return f"{kb:.1f} PB"


def render_markdown(
    diff: dict,
    old_meta: sqlite3.Row,
    new_meta: sqlite3.Row,
    old_vol: tuple[int, int] | None,
    new_vol: tuple[int, int] | None,
    bigfiles: list[dict] | None = None,
) -> str:
    """渲染 Markdown 日报。"""
    lines: list[str] = []
    lines.append(f"# Fathom 日报 · {new_meta['created_at'][:10]}")
    lines.append("")
    lines.append(
        f"- 对比快照：{old_meta['created_at']} → {new_meta['created_at']}（根：`{new_meta['root']}`）"
    )
    if old_vol and new_vol:
        free_delta = new_vol[1] - old_vol[1]
        lines.append(
            f"- 卷剩余空间：{human_kb(new_vol[1] // 1024)}（较上次 {free_delta / (1024**3):+.1f} GB）"
        )
    if new_meta["denied_count"]:
        lines.append(
            f"- 注意：有 {new_meta['denied_count']} 个目录因权限无法统计（如需覆盖 ~/Library 受保护区域，"
            f"为运行终端授予「完全磁盘访问权限」）"
        )
    lines.append("")

    def section(title: str, items: list[DirChange]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("无")
            lines.append("")
            return
        lines.append("| 目录 | 变化 | 之前 | 现在 |")
        lines.append("|------|------|------|------|")
        for c in items:
            lines.append(
                f"| `{c.path}` | {human_kb(c.delta_kb)} | {human_kb(c.old_kb)} | {human_kb(c.new_kb)} |"
            )
        lines.append("")

    section("增长最多的目录", diff["grown"])
    section("缩减最多的目录", diff["shrunk"])
    section("新出现的大目录（≥100MB）", diff["added"])
    section("消失的目录", diff["removed"])

    if bigfiles is not None:
        lines.append(f"## 近期新增/修改的大文件（≥{config.BIGFILE_DEFAULT_MB}MB）")
        lines.append("")
        if not bigfiles:
            lines.append("无")
        else:
            lines.append("| 大小 | 修改时间 | 文件 |")
            lines.append("|------|----------|------|")
            for f in bigfiles:
                lines.append(f"| {human_kb(f['size'] // 1024)} | {f['mtime']} | `{f['path']}` |")
        lines.append("")

    lines.append("---")
    lines.append("*由 fathom 自动生成*")
    return "\n".join(lines) + "\n"


def _report_inputs(conn: sqlite3.Connection, sid: int):
    snaps = conn.execute(
        "SELECT * FROM snapshots WHERE root=(SELECT root FROM snapshots WHERE id=?) "
        "AND (created_at < (SELECT created_at FROM snapshots WHERE id=?) "
        "OR (created_at = (SELECT created_at FROM snapshots WHERE id=?) AND id <= ?)) "
        "ORDER BY created_at DESC, id DESC LIMIT 2",
        (sid, sid, sid, sid),
    ).fetchall()
    if len(snaps) < 2 or snaps[0]["id"] != sid:
        raise ValueError("至少需要两个快照才能生成对比报告")

    new_meta, old_meta = snaps[0], snaps[1]
    diff = compute_diff(load_snapshot(conn, old_meta["id"]), load_snapshot(conn, new_meta["id"]))

    def vol(sid_: int) -> tuple[int, int] | None:
        r = conn.execute(
            "SELECT total_bytes, free_bytes FROM volume_stats WHERE snapshot_id = ?", (sid_,)
        ).fetchone()
        return (r["total_bytes"], r["free_bytes"]) if r else None

    new_vol = vol(new_meta["id"])
    return diff, old_meta, new_meta, vol(old_meta["id"]), new_vol


def write_daily_report(
    conn: sqlite3.Connection, sid: int, *, notify_after_write: bool = True
) -> Path:
    """对比指定快照与同根前一快照生成日报文件，返回路径。

    ``notify_after_write=False`` 供统一协调器把报告和通知分阶段记录；默认值
    保持既有直接调用合同。
    """
    diff, old_meta, new_meta, old_vol, new_vol = _report_inputs(conn, sid)
    md = render_markdown(diff, old_meta, new_meta, old_vol, new_vol)
    out = config.REPORTS_DIR / f"{new_meta['created_at'][:10]}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    # 通知放在日报落盘之后，且 notify 自吞全部异常：通知失败不影响日报（ISS-003）。
    # CLI scan 与 API 手动扫描都经过本函数，两路自动覆盖。
    if notify_after_write:
        notify.notify_scan_done(diff, new_vol[1] if new_vol else None)
    return out


def notify_for_snapshot(conn: sqlite3.Connection, sid: int) -> bool:
    """为已成功写入日报的指定快照尝试通知，不修改报告或快照。"""
    diff, _old_meta, _new_meta, _old_vol, new_vol = _report_inputs(conn, sid)
    return notify.notify_scan_done(diff, new_vol[1] if new_vol else None)
