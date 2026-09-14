"""差分计算与 Markdown 日报生成。

核心问题："哪个文件夹冒出来了" —— 对比两个快照，给出：
- Top 增长 / 缩减目录（父子折叠，避免同一条链刷屏）
- 首次记录的大目录（基线中未记录，可能是越过阈值，不代表文件系统新建）
- 未记录的目录（本次未记录，可能是低于阈值/权限受限/已移除，不构成删除证据）

差分只比较同一数据集（同根同入库阈值口径）的有效快照（ISS-021）：
根或阈值不同的历史互不作为基线，避免错配。列表只按目录逐条呈现，
父子累计值不可求和，也不提供净增量合计。
"""

from __future__ import annotations

import datetime as dt
import re
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


def same_dataset(row_a, row_b) -> bool:
    """两快照是否属于同一数据集（同根同入库阈值口径）。

    数据集身份 = (root, min_kb)。v3 之前的旧记录未持久化阈值，min_kb 为
    NULL：同为 NULL 视为该根的"口径未知"数据集，彼此可比较（保持既有
    行为）；NULL 与已知阈值不可比——不能证明同口径，拒绝混用。
    """
    return row_a["root"] == row_b["root"] and row_a["min_kb"] == row_b["min_kb"]


def find_same_dataset_predecessor(
    conn: sqlite3.Connection, sid: int
) -> sqlite3.Row | None:
    """用传入 sid 的数据集身份找同数据集前一快照；无则返回 None。

    PR #25 已把日报基线从"全局最近两条"改为按传入 sid 查同根前驱；本函数
    在其上收紧为同数据集（ISS-021）：根或阈值口径不同的历史不进入对比，
    升级后首个新口径快照、新监控根的首扫都没有可比基线。
    """
    target = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
    if target is None:
        return None
    return conn.execute(
        "SELECT * FROM snapshots "
        "WHERE root = ? AND min_kb IS ? "
        "AND (created_at < ? OR (created_at = ? AND id < ?)) "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (target["root"], target["min_kb"],
         target["created_at"], target["created_at"], sid),
    ).fetchone()


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
    """渲染 Markdown 日报。

    快照 ID（a=基线，b=本次）写进报头，报告对自身依据一目了然（ISS-021）。
    跨阈值条目只说"首次记录/未记录"，不冒充文件系统事实：入库有阈值，
    entries 缺失可能是低于阈值、权限受限或已移除，数据库无法区分。
    """
    lines: list[str] = []
    lines.append(f"# Fathom 日报 · {new_meta['created_at'][:10]}")
    lines.append("")
    lines.append(
        f"- 对比快照（a→b）：#{old_meta['id']} {old_meta['created_at']} → "
        f"#{new_meta['id']} {new_meta['created_at']}（根：`{new_meta['root']}`）"
    )
    new_min_kb = new_meta["min_kb"] if "min_kb" in new_meta.keys() else None
    if new_min_kb is not None:
        lines.append(
            f"- 记录口径：仅入库 ≥{new_min_kb} KiB 的目录；未记录不等于不存在"
        )
    if old_vol and new_vol:
        free_delta = new_vol[1] - old_vol[1]
        lines.append(
            f"- 卷剩余空间：{human_kb(new_vol[1] // 1024)}（较上次 {free_delta / (1024**3):+.1f} GB）"
        )
    if new_meta["denied_count"]:
        lines.append(
            f"- 注意：本次采集为部分覆盖，有 {new_meta['denied_count']} 个目录因权限无法统计"
            "（如需覆盖 ~/Library 受保护区域，为运行终端授予「完全磁盘访问权限」），"
            "这些目录及其子目录本次未记录"
        )
    lines.append("")

    def section(title: str, items: list[DirChange], note: str | None = None) -> None:
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
        if note:
            lines.append(note)
            lines.append("")

    section("增长最多的目录", diff["grown"])
    section("缩减最多的目录", diff["shrunk"])
    section(
        "首次记录的大目录（≥100MB）",
        diff["added"],
        "上一快照未记录这些目录：可能是既有目录增长越过记录阈值，不代表文件系统新建。",
    )
    section(
        "未记录的目录",
        diff["removed"],
        "本次快照未记录这些目录：可能是已低于记录阈值、权限受限或已被移除；不构成删除证明。",
    )

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
    new_meta = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
    if new_meta is None:
        raise ValueError(f"快照 {sid} 不存在，无法生成对比报告")
    old_meta = find_same_dataset_predecessor(conn, sid)
    if old_meta is None:
        # 首扫、升级后首个新口径快照或新监控根首扫都没有同数据集基线；
        # 协调器以此消息把报告阶段记为 not_available 而不是失败。
        raise ValueError("至少需要两个快照（同根同口径）才能生成对比报告")

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
    """对比指定快照与同数据集（同根同口径）前一快照生成日报文件，返回路径。

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


# ISS-050 运行根文件保留策略：
# 日报以 created_at[:10] 即 ``YYYY-MM-DD.md`` 命名（见 write_daily_report），
# 与 ``/api/report`` 端点（``fathom/api.py``）共享同一约定。运行根的日志
# 文件目前命名固定（``notify.log``、``launchd-scan.out.log`` 等），暂不
# 涉及，但保留接口对 ``YYYY-MM-DD.log`` / ``prefix-YYYY-MM-DD.log`` 形式
# 同样适用——文件名中提取不出 ``YYYY-MM-DD`` 的文件一律保留，避免误删
# launchd 追加的固定日志与运维现场保留件。

_DATE_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _date_from_name(name: str) -> dt.date | None:
    """从文件名中提取 ``YYYY-MM-DD``；无日期或解析失败返回 None。"""
    match = _DATE_IN_NAME.search(name)
    if match is None:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        # 形如 2026-13-40 的非法日期不能假装可解析。
        return None


def _prune_files_by_date(
    directory: Path,
    *,
    retention_days: int,
    today: dt.date,
) -> tuple[int, list[str]]:
    """删除 ``directory`` 顶层、文件名含可解析日期且早于保留边界的文件。

    仅扫描顶层（不递归）；目录/非文件对象、非保留边界内的文件、文件名
    无法解析日期的文件全部保留；删除失败写入警告不抛出（ISS-050 实施边界：
    删除失败不能反向影响快照与日报）。
    """
    if not directory.exists() or not directory.is_dir():
        return 0, []
    cutoff = today - dt.timedelta(days=retention_days)
    deleted = 0
    warnings: list[str] = []
    for entry in directory.iterdir():
        if not entry.is_file() or entry.is_symlink():
            continue
        file_date = _date_from_name(entry.name)
        if file_date is None:
            continue  # 日期不可解析的一律保留
        if file_date >= cutoff:
            continue  # 仍在保留窗口内
        try:
            entry.unlink()
            deleted += 1
        except OSError as exc:
            warnings.append(f"删除 {directory.name}/{entry.name} 失败：{exc}")
    return deleted, warnings


def prune_reports(
    *,
    retention_days: int | None = None,
    today: dt.date | None = None,
) -> tuple[int, list[str]]:
    """清理运行根 ``reports/`` 下早于保留天数的日报与诊断报告。

    返回 ``(deleted_count, warnings)``。``retention_days`` 默认取
    ``config.BIGFILE_REPORT_RETENTION_DAYS``；``today`` 仅供测试冻结，
    生产调用留给 ``dt.date.today()``。
    """
    days = config.BIGFILE_REPORT_RETENTION_DAYS if retention_days is None else retention_days
    reference = today if today is not None else dt.date.today()
    return _prune_files_by_date(
        config.REPORTS_DIR, retention_days=days, today=reference,
    )


def prune_logs(
    *,
    retention_days: int | None = None,
    today: dt.date | None = None,
) -> tuple[int, list[str]]:
    """清理运行根 ``logs/`` 下早于保留天数且文件名含可解析日期的日志。

    ``notify.log`` / ``launchd-scan.out.log`` 等固定名称不含日期，保留；
    形如 ``YYYY-MM-DD.log`` / ``prefix-YYYY-MM-DD.log`` 的命名按保留天数
    清理。返回 ``(deleted_count, warnings)``。
    """
    days = config.BIGFILE_LOG_RETENTION_DAYS if retention_days is None else retention_days
    reference = today if today is not None else dt.date.today()
    return _prune_files_by_date(
        config.LOGS_DIR, retention_days=days, today=reference,
    )
