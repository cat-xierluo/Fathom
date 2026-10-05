"""ISS-159 /api/browse 显式快照绑定定向测试。

任务卡反例（实现前先红测，docs/TESTING.md「修复首先补能失败的反例测试」）：
1. 旧库与 latest 容量不同：无 snapshot_id 的 browse 恒绑最新快照；显式
   snapshot_id 必须返回该快照的实点分布，不被最新覆盖（分布页历史实点）；
2. 单快照库可用，且前驱不可比时差分未知（comparison=null、delta=null），
   不冒充基线；
3. 前一可比快照的差分仅为次级且区间明确（comparison 显式携带基线快照身份）。

边界：多卷/同路径不同身份（按所选快照 root 约束路径）、缺父结构导航
（子有记录父无直接记录）、稳定分页（游标绑定校验）、root=/、含 HTML
字符路径原样透传、快照淘汰/不存在 404、旧行为（无 snapshot_id）响应
形态与口径不变。

数据不变量：缺失条目不是删除证据（结构节点 size 为 null 不填 0）；
目录累计大小不可逐行相加。

全部合成数据（tmp_path 下的 /synthetic 根），显式设置 FATHOM_RUNTIME_DIR
与 FATHOM_SCAN_ROOT 指向测试临时目录，不触发真实 HOME 扫描或写生产库。
"""

from __future__ import annotations

import base64
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db
from fathom import hierarchy

ROOT = "/synthetic/iss159"


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """FATHOM_RUNTIME_DIR / FATHOM_SCAN_ROOT 显式指向测试临时目录。

    与 test_diff_children.py 同一隔离合同：环境变量是 helper/CLI/API 的
    共同入口；config 兼容常量指到同一运行根，db/api 读写全部留在临时
    目录，monkeypatch 结束后自动还原。
    """
    runtime_dir = tmp_path / "runtime"
    scan_root = tmp_path / "scanroot"
    scan_root.mkdir(parents=True)
    monkeypatch.delenv("FATHOM_DB", raising=False)  # 旧入口不得劫持运行根
    monkeypatch.setenv("FATHOM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("FATHOM_SCAN_ROOT", str(scan_root))
    monkeypatch.setattr(config, "DATA_DIR", runtime_dir / "data")
    monkeypatch.setattr(config, "DB_PATH", runtime_dir / "data" / "fathom.db")
    monkeypatch.setattr(config, "REPORTS_DIR", runtime_dir / "reports")
    monkeypatch.setattr(config, "LOGS_DIR", runtime_dir / "logs")
    monkeypatch.setattr(config, "DEFAULT_ROOT", scan_root)


@pytest.fixture
def client():
    """与真实客户端同一合同（ISS-022）：合法 Host + 写令牌，不走测试旁路。"""
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


def _insert_snapshot(
    conn: sqlite3.Connection,
    day: str,
    root: str,
    *,
    min_kb: int | None = 1024,
    entries: dict[str, int] | None = None,
    collection_status: str | None = None,
    vanished_count: int = 0,
    denied: int = 0,
    hour: str = "12:00:00",
) -> int:
    """直接造表行（不经 du）。min_kb=None 表示 v3 之前的旧记录（NULL 不补造）。"""
    sizes = entries or {}
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status, vanished_count, exclude_names) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"{day}T{hour}", root, len(sizes) + 1, denied, 0.0,
         max(sizes.values(), default=0), min_kb, collection_status, vanished_count, ""),
    )
    sid = cur.lastrowid
    for path, size in sizes.items():
        conn.execute(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
            (sid, path, size),
        )
    conn.commit()
    return sid


def _trio():
    """三个同数据集快照：s1→s2 温和增长，s3 容量显著不同（latest 诱惑）。"""
    conn = db.connect()
    try:
        s1 = _insert_snapshot(conn, "2026-09-01", ROOT, entries={
            ROOT: 10_000, f"{ROOT}/x": 6_000,
        })
        s2 = _insert_snapshot(conn, "2026-09-02", ROOT, entries={
            ROOT: 11_000, f"{ROOT}/x": 7_000,
        })
        s3 = _insert_snapshot(conn, "2026-09-03", ROOT, entries={
            ROOT: 99_000, f"{ROOT}/x": 95_000,
        })
        return s1, s2, s3
    finally:
        conn.close()


class TestExplicitSnapshotBinding:
    """显式 snapshot_id：实点历史分布不被最新快照覆盖。"""

    def test_old_snapshot_distribution_stays_historical(self, client):
        """反例 1：old snapshot 与 latest 容量不同，实点分布不变最新。"""
        s1, _s2, _s3 = _trio()
        r = client.get("/api/browse",
                       params={"snapshot_id": s1, "path": ROOT})
        assert r.status_code == 200
        body = r.json()
        assert body["path"] == ROOT
        assert body["size_kb"] == 10_000          # s1 的实测，不是 s3 的 99_000
        assert body["snapshot_at"].startswith("2026-09-01")
        assert body["snapshot"]["id"] == s1
        by_name = {c["name"]: c for c in body["children"]}
        assert by_name["x"]["size_kb"] == 6_000   # 不是 latest 的 95_000

    def test_comparison_is_secondary_but_explicit(self, client):
        """反例 3：差分为次级且区间明确——comparison 显式携带基线快照。"""
        s1, s2, s3 = _trio()
        body = client.get("/api/browse",
                          params={"snapshot_id": s2, "path": ROOT}).json()
        assert body["comparison"] is not None
        # comparison 基线就是 s2 的同数据集前驱（s1），不是 latest(s3)
        assert body["comparison"]["snapshot_id"] == s1
        assert body["comparison"]["snapshot_id"] != s3
        assert body["delta_kb"] == 1_000          # s2 - s1，不是 s3 - s2
        by_name = {c["name"]: c for c in body["children"]}
        assert by_name["x"]["delta_kb"] == 1_000
        assert body["comparison"]["created_at"].startswith("2026-09-01")


class TestLegacyCompatibility:
    """旧调用（无 snapshot_id）：行为与响应口径不变。"""

    def test_no_snapshot_id_binds_latest_pair(self, client):
        s1, s2, s3 = _trio()
        r = client.get("/api/browse", params={"path": ROOT})
        assert r.status_code == 200
        body = r.json()
        assert body["snapshot_at"].startswith("2026-09-03")
        assert body["delta_kb"] == 88_000          # s3 - s2（旧行为口径）
        # 旧分支不携带新形态字段（新字段只出现在显式 snapshot_id 调用中）
        assert "snapshot" not in body
        assert "comparison" not in body
        assert "pagination" not in body
        assert (s1, s2, s3) == (s1, s2, s3)

    def test_no_snapshot_id_root_slash_edge(self, client):
        """root=/ 边界保持旧行为（ISS-024）。"""
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", "/", entries={
                "/d": 100, "/d/e": 60,
            })
        finally:
            conn.close()
        assert client.get("/api/browse?path=/").json()["path"] == "/"


class TestSingleSnapshotAndIncomparable:
    """单快照可用；前驱不可比时差分未知，不冒充基线。"""

    def test_single_snapshot_usable_delta_unknown(self, client):
        """反例 2：单快照可用、comparison=null、delta=null。"""
        conn = db.connect()
        try:
            sid = _insert_snapshot(conn, "2026-09-01", ROOT, entries={
                ROOT: 10_000, f"{ROOT}/x": 6_000,
            })
        finally:
            conn.close()
        body = client.get("/api/browse",
                          params={"snapshot_id": sid, "path": ROOT}).json()
        assert body["size_kb"] == 10_000
        assert body["comparison"] is None
        assert body["delta_kb"] is None
        by_name = {c["name"]: c for c in body["children"]}
        assert by_name["x"]["delta_kb"] is None
        assert by_name["x"]["size_kb"] == 6_000

    def test_predecessor_different_dataset_not_comparable(self, client):
        """前一快照换了阈值口径（不同数据集）：差分未知，不跨数据集配基线。"""
        conn = db.connect()
        try:
            old = _insert_snapshot(conn, "2026-09-01", ROOT, min_kb=512,
                                   entries={ROOT: 10_000, f"{ROOT}/x": 6_000})
            new = _insert_snapshot(conn, "2026-09-02", ROOT, min_kb=1024,
                                   entries={ROOT: 11_000, f"{ROOT}/x": 7_000})
        finally:
            conn.close()
        body = client.get("/api/browse",
                          params={"snapshot_id": new, "path": ROOT}).json()
        assert body["comparison"] is None
        assert body["delta_kb"] is None
        # 旧快照仍在库里可查（历史实点独立成立）
        old_body = client.get("/api/browse",
                              params={"snapshot_id": old, "path": ROOT}).json()
        assert old_body["size_kb"] == 10_000
        assert old_body["comparison"] is None


class TestIdentityConstraints:
    """多卷/同路径不同身份：路径以所选快照的身份（root）约束。"""

    VOL_A = "/syn/vol-a"
    VOL_B = "/syn/vol-b"

    def _two_volumes(self):
        conn = db.connect()
        try:
            sa = _insert_snapshot(conn, "2026-09-01", self.VOL_A, entries={
                self.VOL_A: 5_000, f"{self.VOL_A}/data": 4_000,
            })
            sb = _insert_snapshot(conn, "2026-09-01", self.VOL_B, entries={
                self.VOL_B: 50_000, f"{self.VOL_B}/data": 40_000,
            })
            return sa, sb
        finally:
            conn.close()

    def test_path_constrained_to_snapshot_root(self, client):
        sa, sb = self._two_volumes()
        # vol-b 的路径在 vol-a 的快照下越界：400，不静默给出 vol-b 的数据
        r = client.get("/api/browse",
                       params={"snapshot_id": sa, "path": self.VOL_B})
        assert r.status_code == 400
        body = client.get("/api/browse",
                          params={"snapshot_id": sa, "path": self.VOL_A}).json()
        assert body["size_kb"] == 5_000

    def test_same_path_distinct_identity_distinct_readings(self, client):
        """同路径 /data 在两卷是不同数据集的事实：各按所选快照读数。"""
        sa, sb = self._two_volumes()
        a = client.get("/api/browse",
                       params={"snapshot_id": sa,
                               "path": f"{self.VOL_A}/data"}).json()
        b = client.get("/api/browse",
                       params={"snapshot_id": sb,
                               "path": f"{self.VOL_B}/data"}).json()
        assert a["size_kb"] == 4_000
        assert b["size_kb"] == 40_000
        # 两卷同时扫描时互不为前驱（同日同时间戳也不跨身份比较）
        assert a["comparison"] is None and b["comparison"] is None

    def test_relative_and_dotdot_rejected(self, client):
        sa, _sb = self._two_volumes()
        assert client.get("/api/browse", params={
            "snapshot_id": sa, "path": f"{self.VOL_A}/../vol-b"}).status_code == 400
        assert client.get("/api/browse", params={
            "snapshot_id": sa, "path": "syn/vol-a"}).status_code == 400


class TestStructuralNavigation:
    """缺父结构：子有记录、父无直接记录时父为结构节点，仍可导航。"""

    def test_missing_parent_structural_child_measured(self, client):
        conn = db.connect()
        try:
            sid = _insert_snapshot(conn, "2026-09-01", ROOT, entries={
                ROOT: 1_000, f"{ROOT}/a/b": 500,
            })
        finally:
            conn.close()
        # /a 无直接入库记录（低于阈值），但有已记录后代 b
        body = client.get("/api/browse",
                          params={"snapshot_id": sid,
                                  "path": f"{ROOT}/a"}).json()
        assert body["status"] == "structural"
        assert body["size_kb"] is None            # 不填 0
        by_name = {c["name"]: c for c in body["children"]}
        assert by_name["b"]["status"] == "measured"
        assert by_name["b"]["size_kb"] == 500

    def test_fully_unrecorded_path_404(self, client):
        """路径无直接记录也无任何已记录后代：404 如实说明，不冒充空目录。"""
        conn = db.connect()
        try:
            sid = _insert_snapshot(conn, "2026-09-01", ROOT, entries={
                ROOT: 1_000,
            })
        finally:
            conn.close()
        r = client.get("/api/browse",
                       params={"snapshot_id": sid, "path": f"{ROOT}/ghost"})
        assert r.status_code == 404

    def test_snapshot_missing_404(self, client):
        """快照不存在（可能已被保留策略淘汰）：404，不是空结果。"""
        r = client.get("/api/browse",
                       params={"snapshot_id": 999999, "path": ROOT})
        assert r.status_code == 404


class TestPagination:
    """稳定分页：>100 直属子目录无重复漏项，游标绑定查询上下文。"""

    N = 130

    def _snapshot_with_many_children(self):
        conn = db.connect()
        try:
            sid = _insert_snapshot(conn, "2026-09-01", ROOT, entries={
                **{f"{ROOT}/c{i:03d}": 1_000 + i for i in range(self.N)},
                ROOT: 300_000,
            })
            return sid
        finally:
            conn.close()

    def test_pages_cover_all_children_without_overlap(self, client):
        sid = self._snapshot_with_many_children()
        p1 = client.get("/api/browse",
                        params={"snapshot_id": sid, "path": ROOT}).json()
        assert p1["pagination"]["total"] == self.N
        assert p1["pagination"]["returned"] == 100
        assert p1["pagination"]["has_more"] is True
        assert p1["pagination"]["next_cursor"]
        seen = {c["name"] for c in p1["children"]}

        cur = p1["pagination"]["next_cursor"]
        p2 = client.get("/api/browse",
                        params={"snapshot_id": sid, "path": ROOT,
                                "cursor": cur}).json()
        assert p2["pagination"]["returned"] == self.N - 100
        assert p2["pagination"]["has_more"] is False
        assert p2["pagination"]["next_cursor"] is None
        seen |= {c["name"] for c in p2["children"]}
        assert len(seen) == self.N                 # 无重复无漏项

    def test_cursor_bound_to_query_context(self, client):
        sid = self._snapshot_with_many_children()
        p1 = client.get("/api/browse",
                        params={"snapshot_id": sid, "path": ROOT}).json()
        cur = p1["pagination"]["next_cursor"]
        # 换 path 使用旧游标：400，必须从首页重新请求
        r = client.get("/api/browse",
                       params={"snapshot_id": sid, "path": f"{ROOT}/c000",
                               "cursor": cur})
        assert r.status_code == 400
        # 原样重放旧页游标：稳定分页——同一游标重复请求得到同一页
        page2a = client.get("/api/browse",
                            params={"snapshot_id": sid, "path": ROOT,
                                    "cursor": cur}).json()
        page2b = client.get("/api/browse",
                            params={"snapshot_id": sid, "path": ROOT,
                                    "cursor": cur}).json()
        assert page2a["pagination"]["offset"] == 100
        assert {c["name"] for c in page2a["children"]} == \
            {c["name"] for c in page2b["children"]}
        # 伪造游标：400
        forged = base64.urlsafe_b64encode(json.dumps(
            {"v": 1, "s": sid, "path": ROOT, "sort": "size", "offset": 7}
        ).encode()).decode().rstrip("=")
        assert client.get("/api/browse", params={
            "snapshot_id": sid, "path": ROOT,
            "cursor": forged + "x"}).status_code == 400


class TestEdgeShapes:
    """root=/、HTML 字符路径、质量字段、趋势点身份。"""

    def test_root_slash_snapshot(self, client):
        conn = db.connect()
        try:
            sid = _insert_snapshot(conn, "2026-09-01", "/", entries={
                "/d": 100, "/d/e": 60,
            })
        finally:
            conn.close()
        body = client.get("/api/browse",
                          params={"snapshot_id": sid}).json()
        assert body["path"] == "/"
        by_name = {c["name"]: c for c in body["children"]}
        assert by_name["d"]["size_kb"] == 100

    def test_html_char_path_passthrough(self, client):
        """API 原样透传特殊字符路径（转义是渲染层责任），不截断不改写。"""
        tricky = "<b>&\"x"
        conn = db.connect()
        try:
            sid = _insert_snapshot(conn, "2026-09-01", ROOT, entries={
                ROOT: 1_000, f"{ROOT}/{tricky}": 800,
            })
        finally:
            conn.close()
        body = client.get("/api/browse",
                          params={"snapshot_id": sid, "path": ROOT}).json()
        assert body["children"][0]["name"] == tricky
        child = client.get("/api/browse",
                           params={"snapshot_id": sid,
                                   "path": f"{ROOT}/{tricky}"}).json()
        assert child["size_kb"] == 800

    def test_snapshot_quality_fields_exposed(self, client):
        """质量字段：部分采集/消失计数随快照身份返回，不与最新混用。"""
        conn = db.connect()
        try:
            ok = _insert_snapshot(conn, "2026-09-01", ROOT, entries={
                ROOT: 10_000,
            })
            partial = _insert_snapshot(conn, "2026-09-02", ROOT, entries={
                ROOT: 11_000,
            }, collection_status="partial", vanished_count=3, denied=2)
        finally:
            conn.close()
        body = client.get("/api/browse",
                          params={"snapshot_id": partial, "path": ROOT}).json()
        assert body["snapshot"]["collection_status"] == "partial"
        assert body["snapshot"]["vanished_count"] == 3
        assert body["snapshot"]["denied_count"] == 2
        clean = client.get("/api/browse",
                           params={"snapshot_id": ok, "path": ROOT}).json()
        assert clean["snapshot"]["collection_status"] is None
        assert clean["snapshot"]["vanished_count"] == 0

    def test_trend_points_carry_snapshot_identity(self, client):
        """趋势是历史序列（全同数据集记录点，含所选快照之后的点），
        每点带快照身份——与旧行为同一口径，不冒充所选快照专属。"""
        s1, s2, s3 = _trio()
        body = client.get("/api/browse",
                          params={"snapshot_id": s2, "path": ROOT}).json()
        assert [p["size_kb"] for p in body["trend"]] == [10_000, 11_000, 99_000]
        assert [p["snapshot_id"] for p in body["trend"]] == [s1, s2, s3]
        assert all(p["created_at"] for p in body["trend"])

    def test_sort_size_desc_stable(self, client):
        conn = db.connect()
        try:
            sid = _insert_snapshot(conn, "2026-09-01", ROOT, entries={
                ROOT: 10_000,
                f"{ROOT}/big": 5_000, f"{ROOT}/mid": 3_000,
                f"{ROOT}/small": 1_000,
            })
        finally:
            conn.close()
        body = client.get("/api/browse",
                          params={"snapshot_id": sid, "path": ROOT}).json()
        assert [c["name"] for c in body["children"]] == ["big", "mid", "small"]


class TestCursorHelpers:
    """hierarchy 游标辅助：单快照绑定校验与偏移解析。"""

    def test_cursor_matches_snapshot(self):
        payload = {"v": 2, "s": 3, "path": "/r", "sort": "size", "offset": 0}
        assert hierarchy.cursor_matches_snapshot(
            payload, s=3, path="/r", sort="size")
        assert not hierarchy.cursor_matches_snapshot(
            payload, s=4, path="/r", sort="size")
        assert not hierarchy.cursor_matches_snapshot(
            payload, s=3, path="/r2", sort="size")
        assert not hierarchy.cursor_matches_snapshot(
            payload, s=3, path="/r", sort="name")

    def test_decode_and_offset(self):
        cur = hierarchy.encode_cursor(
            {"v": 1, "s": 3, "path": "/r", "sort": "size", "offset": 120})
        payload = hierarchy.decode_cursor(cur)
        assert payload is not None
        assert hierarchy.cursor_offset(payload) == 120
