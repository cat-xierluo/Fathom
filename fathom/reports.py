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
