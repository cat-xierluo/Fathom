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


def _row_exclude_names(row) -> str:
    """读取快照行的 exclude_names；旧行（v4 之前不存在该列）以 '' 兜底。"""
    try:
        value = row["exclude_names"]
    except (IndexError, KeyError):
        return ""
    return "" if value is None else str(value)


def _row_plan_id(row) -> str | None:
    """读取快照行的 plan_id；旧行/窄行（无该列）以 None 兜底（legacy 口径）。"""
    try:
        value = row["plan_id"]
    except (IndexError, KeyError):
        return None
    return None if value is None else str(value)


def same_dataset(row_a, row_b) -> bool:
    """两快照是否属于同一数据集（同身份口径）。

    数据集身份分两档（ISS-153）：
    - v8 新身份（plan_id 非空）：规范根计划已包含范围/卷稳定 ID、规范根、
      计量版本、阈值与排除掩码，plan_id 相等即可比——同路径换卷或换
      计划都形成新计划，不可比；
    - legacy（plan_id 为 NULL）：仍按三元组 (root, min_kb, exclude_names)
      （ISS-021/ISS-066 口径不变），且只与同为 legacy 的行比较。

    旧 NULL 身份不补造真实卷 UUID，legacy 行与新身份行永不混比——不能
    证明同源。legacy 档内部：min_kb 为 NULL 视为该根的"口径未知"数据集，
    彼此可比较（保持既有行为）；NULL 与已知阈值不可比。exclude_names：
    v5 之前的旧记录没有该列，_row_exclude_names 兜底返回 '' 与新写入的
    "无配置"快照同身份——默认路径零行为变化。
    """
    plan_a, plan_b = _row_plan_id(row_a), _row_plan_id(row_b)
    if plan_a is not None or plan_b is not None:
        # 任一行携带新身份：仅当双方是同一计划才可比。
        return plan_a is not None and plan_a == plan_b
    return (row_a["root"] == row_b["root"]
            and row_a["min_kb"] == row_b["min_kb"]
            and _row_exclude_names(row_a) == _row_exclude_names(row_b))


def dataset_identity(row: sqlite3.Row) -> tuple[str, int | None, str, str | None]:
    """快照行的数据集身份 (root, min_kb, exclude_names, plan_id)。

    与 same_dataset 同一口径的显式化（ISS-149）：趋势等序列查询以身份
    取同数据集窗口，不再各自另写 root/min_kb 谓词。ISS-153 在三元组后
    追加第四位 plan_id：legacy 行为 None，新身份行为其计划 ID；前三位
    位置不变——/api/trend 等既有消费者的 identity[0..2] 读法自动兼容，
    身份谓词统一由 find_same_dataset_snapshot_rows /
    find_same_dataset_predecessor 按 plan_id 分档处理。
    """
    return (row["root"], row["min_kb"], _row_exclude_names(row),
            _row_plan_id(row))


def find_same_dataset_snapshot_rows(
    conn: sqlite3.Connection,
    identity: tuple[str, int | None, str, str | None],
    *,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """按数据集身份取快照行序列：时间正序，limit 只截最新端。

    身份分档（ISS-153）：identity 第 4 位 plan_id 非 None 时按计划取
    （同计划即可比，与 same_dataset 新身份口径一致）；None（或调用方
    仍传旧三元组）时按 (root, min_kb, exclude_names) 取且只取同为
    legacy（plan_id IS NULL）的行——新身份行不混入 legacy 窗口。
    供 /api/trend 等序列查询复用："最新窗口"必须在同数据集序列上截取
    （AUD-09 同类反例），跨数据集历史不得混入（ISS-149）。
    """
    plan_id = identity[3] if len(identity) > 3 else None
    if plan_id is not None:
        sql = ("SELECT * FROM snapshots WHERE plan_id = ? "
               "ORDER BY created_at DESC, id DESC")
        params: list[object] = [plan_id]
    else:
        sql = ("SELECT * FROM snapshots "
               "WHERE root = ? AND min_kb IS ? AND exclude_names IS ? "
               "AND plan_id IS NULL "
               "ORDER BY created_at DESC, id DESC")
        params = [identity[0], identity[1], identity[2]]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    rows.reverse()  # 输出时间正序，序列消费方可直接渲染
    return rows


def find_same_dataset_predecessor(
    conn: sqlite3.Connection, sid: int
) -> sqlite3.Row | None:
    """用传入 sid 的数据集身份找同数据集前一快照；无则返回 None。

    PR #25 已把日报基线从"全局最近两条"改为按传入 sid 查同根前驱；本函数
    在其上收紧为同数据集（ISS-021/ISS-066），v8 再按 plan_id 分档
    （ISS-153）：新身份行只认同计划前驱；legacy 行按三元组找且只认同为
    legacy 的行。根、阈值口径、排除掩码或计划不同的历史不进入对比，
    升级后首个新口径快照、新监控根的首扫、首次启用排除集的快照、
    换卷/换计划后的首扫都没有可比基线。
    """
    target = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
    if target is None:
        return None
    target_plan = _row_plan_id(target)
    if target_plan is not None:
        return conn.execute(
            "SELECT * FROM snapshots WHERE plan_id = ? "
            "AND (created_at < ? OR (created_at = ? AND id < ?)) "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (target_plan, target["created_at"], target["created_at"], sid),
        ).fetchone()
    target_excludes = _row_exclude_names(target)
    return conn.execute(
        "SELECT * FROM snapshots "
        "WHERE root = ? AND min_kb IS ? AND exclude_names IS ? "
        "AND plan_id IS NULL "
        "AND (created_at < ? OR (created_at = ? AND id < ?)) "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (target["root"], target["min_kb"], target_excludes,
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
            f"- 注意：本次采集为部分覆盖，du 输出 {new_meta['denied_count']} 条读取受限记录；"
            "同一路径可能产生多条错误，无法据此判断未统计目录数量或空间大小"
        )
    vanished_count = new_meta["vanished_count"] if "vanished_count" in new_meta.keys() else 0
    confirmed = (new_meta["confirmed_missing_count"]
                 if "confirmed_missing_count" in new_meta.keys() else None)
    unverified = (new_meta["path_unverified_count"]
                  if "path_unverified_count" in new_meta.keys() else None)
    if confirmed is not None and unverified is not None:
        if confirmed:
            lines.append(
                f"- 注意：{confirmed} 个目录在 du 输出后校验时路径不存在；"
                "保留 du 当时的测量值，不能据此推断删除原因或未统计空间"
            )
        if unverified:
            lines.append(
                f"- 注意：{unverified} 个目录在 du 输出后校验时状态无法确认；"
                "可能因权限或其他读取错误无法访问，保留 du 当时的测量值，"
                "不能认定已删除或推算未统计空间"
            )
    elif vanished_count:
        lines.append(
            f"- 注意：另有 {vanished_count} 个目录状态未确认：du 输出后校验时"
            "未能确认路径仍存在（可能移动、被清理或无法访问）；保留 du 当时的测量值，"
            "不能据此认定已删除或推算未统计空间"
        )
    # ISS-066：本次采集生效的 du -I 排除掩码（非空时如实列出）。
    exclude_names = _row_exclude_names(new_meta)
    if exclude_names:
        masks = "、".join(exclude_names.split(";"))
        lines.append(
            f"- 排除掩码：{masks}（本次扫描按这些名字跳过整棵子树；"
            "数据集身份 (root, min_kb, exclude_names) 不同则不与历史可比）"
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


_UNSAFE_NAME_CHARS = re.compile(r"[^0-9A-Za-z._-]+")


def scope_report_suffix(conn: sqlite3.Connection, plan_id: str) -> str:
    """从计划/范围取出日报文件名的范围后缀（无范围时为空串）。

    只用于**文件命名**，不参与任何身份或可比性判断。范围 ID（卷 UUID /
    派生 path ID）里的冒号等字符会被规整成 ``-``，保证跨平台文件名安全。
    """
    row = conn.execute(
        "SELECT p.scope_id, s.display_name FROM scan_plans p "
        "LEFT JOIN scan_scopes s ON s.scope_id = p.scope_id "
        "WHERE p.plan_id = ?", (plan_id,),
    ).fetchone()
    if row is None or not row["scope_id"]:
        return ""
    slug = _UNSAFE_NAME_CHARS.sub("-", str(row["scope_id"])).strip("-")
    return slug or "scope"


def report_file_name(conn: sqlite3.Connection, sid: int) -> str:
    """日报文件名：legacy 仍是 ``YYYY-MM-DD.md``，新身份带范围后缀。

    ISS-154 反例：一轮里多个范围各有快照，若都写成 ``YYYY-MM-DD.md``，
    后写的会覆盖先写的，同日别的范围日报就没了。因此带 plan_id 的快照
    命名加 ``-scope-<范围>`` 段（保留 ``YYYY-MM-DD`` 前缀，保留期解析与
    ``/api/report`` 的日期约定都照旧工作），legacy 命名逐字节不变。
    """
    meta = conn.execute(
        "SELECT created_at, plan_id FROM snapshots WHERE id=?", (sid,)
    ).fetchone()
    if meta is None:
        raise ValueError(f"快照 {sid} 不存在，无法命名日报")
    day = str(meta["created_at"])[:10]
    plan_id = _row_plan_id(meta)
    if plan_id is None:
        return f"{day}.md"
    return f"{day}-scope-{scope_report_suffix(conn, plan_id)}.md"


def write_daily_report(
    conn: sqlite3.Connection, sid: int, *, notify_after_write: bool = True
) -> Path:
    """对比指定快照与同数据集（同根同口径）前一快照生成日报文件，返回路径。

    ``notify_after_write=False`` 供统一协调器把报告和通知分阶段记录；默认值
    保持既有直接调用合同。文件名按快照身份决定（见 ``report_file_name``）：
    legacy 单根仍是 ``YYYY-MM-DD.md``，新身份带范围后缀，一轮多范围之间
    互不覆盖。
    """
    diff, old_meta, new_meta, old_vol, new_vol = _report_inputs(conn, sid)
    md = render_markdown(diff, old_meta, new_meta, old_vol, new_vol)
    out = config.REPORTS_DIR / report_file_name(conn, sid)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    # 通知放在日报落盘之后，且 notify 自吞全部异常：通知失败不影响日报（ISS-003）。
    # CLI scan 与 API 手动扫描都经过本函数，两路自动覆盖。快照采集状态
    # （partial/denied_count/vanished_count）一并传入，通知正文据此注明
    # 覆盖缺口（ISS-003A + ISS-065）。
    if notify_after_write:
        notify.notify_scan_done(
            diff, new_vol[1] if new_vol else None,
            collection_status=new_meta["collection_status"],
            denied_count=new_meta["denied_count"] or 0,
            vanished_count=new_meta["vanished_count"] if "vanished_count" in new_meta.keys() else 0,
            confirmed_missing_count=(new_meta["confirmed_missing_count"]
                                     if "confirmed_missing_count" in new_meta.keys() else None),
            path_unverified_count=(new_meta["path_unverified_count"]
                                   if "path_unverified_count" in new_meta.keys() else None),
        )
    return out


def round_report_file_name(conn: sqlite3.Connection, round_id: int) -> str:
    """轮次汇总文件名 ``YYYY-MM-DD-round-<id>.md``。

    与按范围的 ``-scope-<范围>`` 段互不撞名：两段前缀不同，范围名再怪
    也只会多出 ``scope-scope-…`` 之类的叠加，不会覆盖别人的日报。
    """
    row = conn.execute(
        "SELECT started_at FROM scan_rounds WHERE id=?", (round_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"轮次 {round_id} 不存在，无法命名汇总")
    return f"{str(row['started_at'])[:10]}-round-{int(round_id)}.md"


def _round_status_label(status: str) -> str:
    return {
        "full": "全部完成",
        "partial": "部分完成",
        "failed": "全部失败",
        "cancelled": "已取消",
    }.get(status, status)


def _member_status_label(status: str | None) -> str:
    return {
        "done": "成功",
        "failed": "失败",
        "cancelled": "已取消",
        "skipped": "未开始",
        "running": "进行中",
        "pending": "待开始",
    }.get(status or "", status or "—")


def render_round_markdown(
    round_id: int, round_status: str, members: list[dict],
    capacity: dict, time_span: dict,
) -> str:
    """轮次汇总正文：逐范围事实 + 时间跨度 + 容量来源。

    三条诚实性约束直接体现在正文里：

    1. 逐范围列出各自快照时刻，**不做跨范围求和**（成员是不同时刻采的，
       求和就是把不同时间的数据说成一个原子时点）；
    2. 明确写出时间跨度与非原子提示，不冒充「本轮时点」；
    3. 容量来源与时间如实标注；未知就写未知，不拿目录 statvfs 顶替整盘。
    """
    lines = [
        f"# 扫描轮次 #{round_id} 汇总",
        "",
        f"- 轮次状态：**{_round_status_label(round_status)}**",
        f"- 范围数：{len(members)}",
    ]
    started, finished = time_span.get("started_at"), time_span.get("finished_at")
    if started or finished:
        lines.append(f"- 时间跨度：{started or '—'} → {finished or '—'}")
    lines.append(
        f"- 说明：{time_span.get('note') or '成员按顺序采集，采集时刻各不相同'}"
    )
    lines.append("")

    lines.append("## 各范围")
    lines.append("")
    lines.append("| 范围 | 规范根 | 阶段 | 快照 | 采集时刻 | 目录数 | 用量 | 采集质量 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for m in members:
        sid = m.get("snapshot_id")
        total_kb = m.get("total_kb")
        lines.append(
            "| {scope} | `{root}` | {status} | {snap} | {when} | {dirs} | {size} | {quality} | {note} |".format(
                scope=m.get("scope_id") or "—",
                root=m.get("canonical_root") or "—",
                status=_member_status_label(m.get("status")),
                snap=f"#{sid}" if sid else "—（沿用上次样本）",
                when=m.get("snapshot_created_at") or "—",
                dirs=m.get("dir_count") if m.get("dir_count") is not None else "—",
                size=human_kb(total_kb) if total_kb is not None else "—",
                quality=m.get("collection_status") or "—",
                note=("引用已过期" if m.get("snapshot_status") == "expired"
                      else ""),
            )
        )
    lines.append("")
    lines.append(
        "> 各范围的采集时刻不同，行与行之间**不可**横向相加，也不可折算成"
        "一个统一的本轮口径。"
    )
    lines.append("")

    lines.append("## 整盘容量")
    lines.append("")
    if capacity.get("status") == "sampled":
        free = capacity.get("free_bytes")
        total = capacity.get("total_bytes")
        lines.append(f"- 采样时间：{capacity.get('sampled_at')}")
        lines.append(f"- 容量来源：容器级发现（{capacity.get('container_id') or '—'}）")
        lines.append(f"- 总量：{human_kb(total // 1024) if total else '未知'}")
        lines.append(f"- 共享剩余：{human_kb(free // 1024) if free else '未知'}")
        lines.append(f"- 样本条数：{capacity.get('samples')}")
    else:
        lines.append("- 本轮未取得新的整盘容量样本；整盘容量保持上次已知值。")
        lines.append("- 未用任何目录 statvfs 结果代替整盘容量。")
    lines.append("")
    return "\n".join(lines)


def write_round_report(
    conn: sqlite3.Connection, round_id: int, *,
    round_status: str, members: list[dict], capacity: dict,
    time_span: dict,
) -> Path:
    """写一轮的汇总报告，返回路径（每范围对比日报之外的整体依据）。"""
    md = render_round_markdown(round_id, round_status, members, capacity,
                               time_span)
    out = config.REPORTS_DIR / round_report_file_name(conn, round_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return out


def notify_for_snapshot(conn: sqlite3.Connection, sid: int) -> bool:
    """为已成功写入日报的指定快照尝试通知，不修改报告或快照。"""
    diff, _old_meta, new_meta, _old_vol, new_vol = _report_inputs(conn, sid)
    return notify.notify_scan_done(
        diff, new_vol[1] if new_vol else None,
        collection_status=new_meta["collection_status"],
        denied_count=new_meta["denied_count"] or 0,
        vanished_count=new_meta["vanished_count"] if "vanished_count" in new_meta.keys() else 0,
        confirmed_missing_count=(new_meta["confirmed_missing_count"]
                                 if "confirmed_missing_count" in new_meta.keys() else None),
        path_unverified_count=(new_meta["path_unverified_count"]
                               if "path_unverified_count" in new_meta.keys() else None),
    )


def notify_first_snapshot_for(conn: sqlite3.Connection, sid: int) -> bool:
    """为首扫（无同数据集基线）的指定快照尝试"首次快照"通知（ISS-003A）。

    与 notify_for_snapshot 对称，但不需要差分：首扫没有可比基线，通知
    只说明快照已建立与覆盖/剩余状态，不出现 0 变化式误导文案。
    """
    row = conn.execute(
        "SELECT collection_status, denied_count, vanished_count, "
        "confirmed_missing_count, path_unverified_count FROM snapshots WHERE id=?", (sid,)
    ).fetchone()
    free_row = conn.execute(
        "SELECT free_bytes FROM volume_stats WHERE snapshot_id = ?", (sid,)
    ).fetchone()
    if row is None:
        return notify.notify_first_snapshot(None)
    return notify.notify_first_snapshot(
        free_row["free_bytes"] if free_row else None,
        collection_status=row["collection_status"],
        denied_count=row["denied_count"] or 0,
        vanished_count=row["vanished_count"] or 0,
        confirmed_missing_count=row["confirmed_missing_count"],
        path_unverified_count=row["path_unverified_count"],
    )


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


# ---------------------------------------------------------------------------
# ISS-157：容量/轮次/归因摘要查询（只读；不触发任何扫描）
# ---------------------------------------------------------------------------

def latest_rounds_with_members(conn: sqlite3.Connection, *, limit: int = 2
                               ) -> list[dict]:
    """读最近若干轮的基本事实 + 成员阶段计数（最新在前）。

    只回显已落库的轮次/成员事实，**不**补算、不**触发**任何采集。整轮
    跨时间如实给出：started_at/finished_at 与成员各自时刻分开呈现，绝不
    压成一个「本轮时点」。
    """
    rows = conn.execute(
        "SELECT id AS round_id, started_at, finished_at, status, message "
        "FROM scan_rounds ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()
    out = []
    for row in rows:
        counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM scan_round_members "
            "WHERE round_id=? GROUP BY status", (row["round_id"],),
        ).fetchall()
        out.append({
            "round_id": int(row["round_id"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "message": row["message"],
            "member_status_counts": {c["status"]: int(c["n"]) for c in counts},
            "atomic": False,
            "time_note": ("成员顺序采集，各成员时刻不同；本轮不是单一原子时点。"),
        })
    return out


def round_attribution(conn: sqlite3.Connection, round_id: int) -> dict:
    """一轮的目录归因：互不重叠测量根、成员质量、stale 标记与可比较性。

    红线逐条落地：

    - 父子测量根不可加：``non_overlapping_roots`` 保留最外层根，子根进
      ``absorbed_roots`` 且不参与求和；
    - 失败成员本轮没有快照 → 其**旧有效值**只作为 ``stale`` 参考列出，
      既不计入 ``measured_kb``，也不被当成已测；
    - ``collection_status``（full/partial）如实透出，partial 目录不能
      与 full 目录一样当成完整可比；
    - 阈值/权限不产生覆盖率字段——摘要里不存在「已测占比」。
    """
    from . import storage

    members = conn.execute(
        "SELECT m.seq, m.plan_id, m.scope_id, m.snapshot_id, m.snapshot_status, "
        "m.status AS member_status, m.started_at, m.finished_at, "
        "p.canonical_root, s.display_name, s.container_id, s.kind AS scope_kind, "
        "sn.created_at AS snapshot_created_at, sn.total_kb, "
        "sn.collection_status, sn.dir_count, sn.denied_count "
        "FROM scan_round_members m "
        "LEFT JOIN scan_plans p ON p.plan_id = m.plan_id "
        "LEFT JOIN scan_scopes s ON s.scope_id = m.scope_id "
        "LEFT JOIN snapshots sn ON sn.id = m.snapshot_id "
        "WHERE m.round_id=? ORDER BY m.seq", (round_id,),
    ).fetchall()

    measured_members, stale_members = [], []
    for row in members:
        root = row["canonical_root"]
        entry = {
            "seq": int(row["seq"]),
            "plan_id": row["plan_id"],
            "scope_id": row["scope_id"],
            "root": root,
            "display_name": row["display_name"],
            "container_id": row["container_id"],
            "member_status": row["member_status"],
            "snapshot_id": row["snapshot_id"],
            "snapshot_status": row["snapshot_status"],
            "snapshot_created_at": row["snapshot_created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "quality": row["collection_status"],
            "total_kb": row["total_kb"],
        }
        if row["snapshot_id"] is None:
            entry["stale"] = True
            entry["stale_note"] = ("本轮无新快照：该成员未计入本轮已测；"
                                   "若有旧有效值只作历史参考，不当本轮贡献。")
            stale_members.append(entry)
        else:
            entry["stale"] = False
            measured_members.append(entry)

    roots = [m["root"] for m in measured_members if m["root"]]
    kept, absorbed = storage.non_overlapping_roots(roots)
    absorbed_set = set(absorbed)
    counted = [m for m in measured_members
               if m["root"] in set(kept) and not m["stale"]]
    measured_kb = None
    if counted and len(counted) == len(measured_members) - len(
            [m for m in measured_members if m["root"] in absorbed_set]):
        measured_kb = sum(int(m["total_kb"] or 0) for m in counted)
    else:
        measured_kb = None

    plan_ids = {m["plan_id"] for m in measured_members if m["plan_id"]}
    container_ids = {m["container_id"] for m in measured_members
                     if m["container_id"]}
    all_full = bool(measured_members) and all(
        m["quality"] == "full" for m in measured_members)

    return {
        "round_id": int(round_id),
        "attribution_roots": list(kept),
        "absorbed_roots": list(absorbed),
        "absorb_note": ("父子测量根不可加：被吸收的子根不参与求和，"
                        "避免同一份空间数数两遍。"),
        "members": [dict(row) for row in members],
        "measured_members": measured_members,
        "stale_members": stale_members,
        "measured_kb": measured_kb,
        "measured_kb_note": ("只含互不重叠测量根的本轮新快照；"
                             "存在子根被吸收时不可简单相加。"),
        "plan_ids": sorted(plan_ids),
        "container_ids": sorted(container_ids),
        "all_roots_full_quality": all_full,
        "comparable_to_previous": all_full and not stale_members,
        "comparable_note": ("需要同主体、同计划、两侧都有效（无失败成员、"
                            "无子根吸收）才可比；否则差额为 null。"),
    }


def cross_round_identity_reasons(current: dict, previous: dict) -> list[str]:
    """比对两轮的**同主体 + 同计划**身份，返回不可比原因列表。

    ISS-157 返修 B1：只查质量与 stale 是不够的——两轮即便都「全部 full、
    无失败成员」，只要**计划集合**或**归因根集合**不同，目录测量的口径
    就不同（换 plan/换 UUID/换根），差额没有意义。独立审查活反例：两轮
    分别用 p1 与 p9 仍判 comparable 并给出 51,404,800 bytes 差额。

    归因根集合取自**计划所挂的范围身份**（container_ids）与计划集合本身，
    两者都必须逐项相等；任一不等即不可比并给出可读原因。
    """
    reasons: list[str] = []
    cur_plans = set(current.get("plan_ids") or [])
    prev_plans = set(previous.get("plan_ids") or [])
    if not cur_plans or not prev_plans:
        reasons.append("缺计划身份：跨计划快照不可比。")
    elif cur_plans != prev_plans:
        reasons.append(
            "跨计划快照不可比：两轮计划集合不同"
            f"（本轮 {sorted(cur_plans)}，前轮 {sorted(prev_plans)}）。")
    cur_scopes = set(current.get("container_ids") or [])
    prev_scopes = set(previous.get("container_ids") or [])
    if cur_scopes != prev_scopes:
        reasons.append(
            "跨主体不可比：两轮归因范围主体不同"
            f"（本轮 {sorted(cur_scopes)}，前轮 {sorted(prev_scopes)}）。")
    cur_roots = set(current.get("attribution_roots") or [])
    prev_roots = set(previous.get("attribution_roots") or [])
    if cur_roots != prev_roots:
        reasons.append(
            "跨测量根集合不可比：本轮归因根与前轮不同"
            f"（本轮 {sorted(cur_roots)}，前轮 {sorted(prev_roots)}）。")
    return reasons


def own_round_reasons(attribution: dict, *, label: str) -> list[str]:
    """一轮自身的质量门原因（失败成员 / 非 full 质量 / 父子重叠根）。

    ISS-157 返修 R1：质量门必须**两侧都跑**。只查本轮会让「前轮 partial、
    本轮 full」这种最危险的组合放行——本轮质量好并不能让前一轮的基线读数
    变有效。故提取为按轮次复用的对称检查，由调用方分别以 ``本轮`` /
    ``前轮`` 标注，原因文本明确指出是哪一轮不合格。

    ``attribution`` 为 None（该轮无归因事实）时返回一条缺事实的原因：
    拿不到一轮的质量事实 = 该轮不可比，而不是「默认可比」。
    """
    if not attribution:
        return [f"{label}无归因事实，无法确认该轮测量质量。"]
    reasons: list[str] = []
    if attribution.get("stale_members"):
        reasons.append(f"{label}存在失败成员（无新快照），缺有效值。")
    if not attribution.get("all_roots_full_quality"):
        reasons.append(f"{label}存在非 full 质量的目录测量。")
    if attribution.get("absorbed_roots"):
        reasons.append(f"{label}存在父子重叠测量根，归因不可简单相加。")
    return reasons


def member_container_reasons(attribution: dict, *, container_id: str | None,
                             label: str) -> list[str]:
    """成员级容器绑定：参与归因求和的成员必须**逐一**属于所选容器。

    ISS-157 返修 B3：只做「所选容器 ∈ 该轮容器集合」的包含判断有两条旁路。
    ① 成员横跨 C1+C2、生效容器为 C1 时 ``C1 in {C1, C2}`` 成立，但
    ``measured_kb`` 里混着 C2 的目录测量，差额说的不是所选容器的变化；
    ② 某成员 ``container_id`` 为 NULL 时被集合构造过滤掉，
    ``{C1} in {C1}`` 同样成立——身份缺失被静默当成「不属于别人」而放行。

    故这里改为**逐成员**核对：``measured_members``（真正参与求和的那批）
    每个成员的 ``container_id`` 必须非 NULL 且等于生效容器，否则不可比。
    身份为 NULL 是「说不清属于谁」，比「明确属于别的容器」更危险，单独点名。
    """
    if not attribution:
        return [f"{label}无归因事实，无法确认成员所属容器。"]
    if not container_id:
        return [f"{label}缺生效容器身份，成员容器无法核对。"]
    reasons: list[str] = []
    members = attribution.get("measured_members") or []
    if not members:
        return [f"{label}无参与归因的成员测量，成员容器无法核对。"]
    foreign, missing = [], []
    for m in members:
        mid = m.get("container_id")
        if not mid:
            missing.append(f"{m.get('display_name') or m.get('root') or m.get('seq')}")
        elif mid != container_id:
            foreign.append(f"{m.get('display_name') or m.get('root') or m.get('seq')}"
                           f"→{mid}")
    if missing:
        reasons.append(
            f"{label}有 {len(missing)} 个参与归因的成员缺容器身份（container_id 为"
            f" NULL）：{missing}；无法确认其测量属于容器 {container_id}。")
    if foreign:
        reasons.append(
            f"{label}有 {len(foreign)} 个参与归因的成员不属于所选容器"
            f" {container_id}：{sorted(foreign)}；目录测量口径与容量主体不一致。")
    return reasons


def round_free_bytes(conn: sqlite3.Connection, round_id: int, *,
                     container_id: str | None = None) -> int | None:
    """取一轮的容器级 free（共享空间只计一次；无容器样本则 None）。

    ISS-157 返修 R2：``container_capacity_samples.round_id`` 无外键，
    同一 round_id 可能挂着**其他容器**的样本（旧实现只按 round_id 盲取，
    会把别的容器的读数当成当前容器的）。故按传入的 ``container_id`` 过滤，
    绑定所选容器；未给容器身份时不猜主体，返回 None（缺失不补值）。
    """
    if not container_id:
        return None
    row = conn.execute(
        "SELECT free_bytes FROM container_capacity_samples "
        "WHERE round_id=? AND free_bytes IS NOT NULL AND container_id=? "
        "ORDER BY sampled_at DESC, id DESC LIMIT 1", (round_id, container_id),
    ).fetchone()
    return None if row is None else int(row["free_bytes"])
