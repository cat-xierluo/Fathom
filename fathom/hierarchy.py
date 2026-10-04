"""绑定历史区间的同级差分计算（ISS-147）。

/api/diff 的 Top25 折叠列表回答「哪些目录变了」，无法回答「某个目录的
直属子目录在 a→b 这段历史里各变了多少」——本模块为树形展开提供这一层
真实数据：给定两个同数据集快照与目标路径，产出祖先上下文、父行、直属
子行与稳定分页所需的全部事实。

诚实性边界（与 reports 既有口径一致，不因展开视图而放松）：
- 缺失条目不是删除证据：b 侧未记录只表达「本次未记录」，状态
  ``unrecorded``，new_kb/delta_kb 为 null，不填 0；
- b 侧首次入库（a 未直接记录）状态 ``first_recorded``，不代表文件系统
  新建，可能是越过入库阈值；
- 两侧都无直接记录、但存在已记录后代的结构节点状态 ``structural``，
  大小与差分全部为 null——它是导航节点，不是测量值；
- 不提供任何「子树净增量/合计」：目录累计大小不可逐行相加；
- 记录项总数（counts）与文件系统覆盖（快照采集状态）分开表达；分页
  截断通过 pagination 显式可见，未展示分页不得当作未细分变化。

查询形态：子树范围扫描走 entries 主键 (snapshot_id, path) 的 BINARY
字典序（``path >= prefix AND path < prefix`` 尾字符 +1），两侧各一条
有序游标，内存中归并——不把整快照载入内存，也不建临时树。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass

# 兄弟行分页：默认页大小与单页上限（>100 兄弟分页验收的合同边界）
CHILDREN_DEFAULT_LIMIT = 100
CHILDREN_MAX_LIMIT = 500

FILTER_VALUES = ("all", "changed")
SORT_VALUES = ("delta", "size", "name")


@dataclass
class NodeRow:
    """一个目录节点在 a→b 区间的双侧行。

    old_kb / new_kb 为 None 表示该侧无直接入库记录（不填 0）；
    has_children / has_changed_descendants 只对直属子行与父行有意义
    （祖先行不下发，避免为面包屑做额外子树扫描）。
    """

    path: str
    name: str
    old_kb: int | None = None
    new_kb: int | None = None
    has_children: bool = False
    has_changed_descendants: bool = False

    @property
    def status(self) -> str:
        if self.old_kb is not None and self.new_kb is not None:
            return "measured"
        if self.old_kb is None and self.new_kb is None:
            return "structural"
        return "first_recorded" if self.new_kb is not None else "unrecorded"

    @property
    def delta_kb(self) -> int | None:
        """两侧都有直接记录才有差分；单侧缺测与结构节点为 null。"""
        if self.old_kb is None or self.new_kb is None:
            return None
        return self.new_kb - self.old_kb

    @property
    def self_changed(self) -> bool:
        """本行自身是否命中「变化」：已测量且不等、单侧首次/未记录。

        结构节点自身永无测量值，不靠本属性命中——由 has_changed_descendants
        保留导航（父净 0 且后代 +20/-20 抵消的整枝不得被筛选漏掉）。
        """
        if self.old_kb is not None and self.new_kb is not None:
            return self.new_kb != self.old_kb
        return self.old_kb is not None or self.new_kb is not None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "old_kb": self.old_kb,
            "new_kb": self.new_kb,
            "delta_kb": self.delta_kb,
            "status": self.status,
            "has_children": self.has_children,
            "has_changed_descendants": self.has_changed_descendants,
        }


def _lookup(conn: sqlite3.Connection, sid: int, path: str) -> int | None:
    row = conn.execute(
        "SELECT size_kb FROM entries WHERE snapshot_id = ? AND path = ?",
        (sid, path),
    ).fetchone()
    return None if row is None else row["size_kb"]


def normalize_target(raw: str | None, root: str) -> str:
    """校验并归一目标路径：根内、绝对、无尾斜杠、无空/./.. 段。

    - ``raw`` 为 None/"" 时默认数据集根；
    - 必须按路径段落在根内（``/r2`` 不是 ``/r`` 的子路径，root=/ 时任何
      绝对路径都在根内）；
    - 拒绝 ``//``、``/./``、``/../`` 与相对路径——du 路径不会含这些形态，
      提前拒绝比自然 404 更可解释。
    """
    root_norm = root.rstrip("/") or "/"
    target = raw.rstrip("/") if raw else ""
    if not target:
        target = root_norm
    if not target.startswith("/"):
        raise ValueError("path 必须是绝对路径")
    if root_norm == "/":
        inside = True
    else:
        inside = target == root_norm or target.startswith(root_norm + "/")
    if not inside:
        raise ValueError(f"路径必须在数据集根 {root_norm} 之内")
    rel = target[1:] if root_norm == "/" else target[len(root_norm) + 1:]
    if rel:  # target == 根时 rel 为空，无段可查
        for seg in rel.split("/"):
            if seg in ("", ".", ".."):
                raise ValueError("path 含空段、`.` 或 `..` 段，已拒绝")
    return target


def ancestor_paths(root: str, target: str) -> list[str]:
    """target 的严格祖先链（含数据集根，不含 target），自顶向下。"""
    root_norm = root.rstrip("/") or "/"
    if target == root_norm:
        return []
    rel = target[1:] if root_norm == "/" else target[len(root_norm) + 1:]
    acc = root_norm
    out = [root_norm]  # 链顶永远是数据集根本身
    for seg in rel.split("/")[:-1]:
        acc = acc.rstrip("/") + "/" + seg  # root=/ 时 "" + "/seg"，无 "//"
        out.append(acc)
    return out


def _subtree_cursor(conn: sqlite3.Connection, sid: int, prefix: str):
    """有序子树游标：[prefix, prefix 尾字符+1) 恰好是前缀匹配集。"""
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    return conn.execute(
        "SELECT path, size_kb FROM entries "
        "WHERE snapshot_id = ? AND path >= ? AND path < ? ORDER BY path",
        (sid, prefix, upper),
    )


def _first_segment(rel: str) -> tuple[str, bool]:
    """返回 (直属子段, 是否恰好一层)；rel 为去前缀后的相对路径。"""
    parts = rel.split("/", 1)
    return parts[0], len(parts) == 1


def collect_children(
    conn: sqlite3.Connection, a_id: int, b_id: int, target: str
) -> dict:
    """归并扫描两侧子树，产出直属子行、父行与记录项计数。

    返回 ``{"children": {name: NodeRow}, "parent": NodeRow,
    "a_entries": int, "b_entries": int}``。子行按段聚合：
    - 恰好一层的相对路径写 old/new（主键保证每侧至多一次）；
    - 更深路径只置 has_children / has_changed_descendants，
      由此自然得到「父缺子有」的结构节点。
    """
    prefix = target.rstrip("/") + "/" if target != "/" else "/"
    children: dict[str, NodeRow] = {}
    any_changed = False
    a_entries = b_entries = 0

    def _node(name: str) -> NodeRow:
        row = children.get(name)
        if row is None:
            row = NodeRow(path=prefix + name, name=name)
            children[name] = row
        return row

    it_a = iter(_subtree_cursor(conn, a_id, prefix))
    it_b = iter(_subtree_cursor(conn, b_id, prefix))
    ra = next(it_a, None)
    rb = next(it_b, None)
    while ra is not None or rb is not None:
        if rb is None or (ra is not None and ra["path"] < rb["path"]):
            path, old, new = ra["path"], ra["size_kb"], None
            ra = next(it_a, None)
            a_entries += 1
        elif ra is None or rb["path"] < ra["path"]:
            path, old, new = rb["path"], None, rb["size_kb"]
            rb = next(it_b, None)
            b_entries += 1
        else:
            path, old, new = ra["path"], ra["size_kb"], rb["size_kb"]
            ra = next(it_a, None)
            rb = next(it_b, None)
            a_entries += 1
            b_entries += 1

        rel = path[len(prefix):]
        if not rel:  # 防御：du 不产尾斜杠路径，畸形行不参与聚合
            continue
        name, direct = _first_segment(rel)
        changed = (old != new) if (old is not None and new is not None) else True
        node = _node(name)
        if direct:
            if old is not None:
                node.old_kb = old
            if new is not None:
                node.new_kb = new
        else:
            node.has_children = True
            if changed:
                node.has_changed_descendants = True
        if changed:
            any_changed = True

    parent_name = target.rsplit("/", 1)[-1] or "/"
    parent = NodeRow(
        path=target, name=parent_name,
        old_kb=_lookup(conn, a_id, target), new_kb=_lookup(conn, b_id, target),
        has_children=bool(children), has_changed_descendants=any_changed,
    )
    return {"children": children, "parent": parent,
            "a_entries": a_entries, "b_entries": b_entries}


def ancestor_rows(conn: sqlite3.Connection, a_id: int, b_id: int,
                  root: str, target: str) -> list[NodeRow]:
    """祖先链各行（点查双侧大小；不下发布尔导航字段）。"""
    out = []
    for path in ancestor_paths(root, target):
        name = path.rsplit("/", 1)[-1] or "/"
        out.append(NodeRow(
            path=path, name=name,
            old_kb=_lookup(conn, a_id, path), new_kb=_lookup(conn, b_id, path),
        ))
    return out


# ---------- 同级排序（只在直属子行集合内生效；末位并列以 name 稳定） ----------

def _key_delta(row: NodeRow) -> tuple:
    d = row.delta_kb
    # 有差分行按 |delta| 降序；无差分行（单侧缺测/结构节点）整体靠后，
    # 组内按名——不在差分排序里混入大小语义，并列规则可解释。
    return (d is None, -abs(d) if d is not None else 0, row.name)


def _key_size(row: NodeRow) -> tuple:
    return (row.new_kb is None, -(row.new_kb or 0),
            row.old_kb is None, -(row.old_kb or 0), row.name)


SORT_KEYS = {"delta": _key_delta, "size": _key_size, "name": lambda r: (r.name,)}


def sort_rows(rows: list[NodeRow], sort: str) -> list[NodeRow]:
    """按声明的同级序排序；兄弟名在数据集内唯一，末位并列天然稳定。"""
    return sorted(rows, key=SORT_KEYS[sort])


# ---------- 分页游标：绑定 a/b/path/filter/sort，只携带偏移 ----------

def encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict | None:
    """解出游标载荷；任何解码/结构失败返回 None（由调用方 400）。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, UnicodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def cursor_offset(payload: dict) -> int | None:
    """载荷中的偏移量；非 int/负数/bool 一律视为无效。"""
    offset = payload.get("offset")
    if type(offset) is not int or offset < 0:
        return None
    return offset


def cursor_matches(payload: dict, *, a: int, b: int, path: str,
                   filter: str, sort: str) -> bool:
    """游标绑定校验：查询上下文任一变化即失效（需重新从首页请求）。"""
    return payload.get("v") == 1 and payload.get("a") == a \
        and payload.get("b") == b and payload.get("path") == path \
        and payload.get("filter") == filter and payload.get("sort") == sort
