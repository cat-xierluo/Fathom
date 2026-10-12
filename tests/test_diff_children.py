"""ISS-147 绑定历史区间的同级差分 API（GET /api/diff/children）定向测试。

任务卡三反例（实现前先红测，docs/TESTING.md「修复首先补能失败的反例测试」）：
1. 旧 /api/diff 的 Top25 父子折叠无法还原「父+20 / 子+18 / 孙+2」的完整
   同级关系——折叠规则用子替换父后，父一级的净变化从列表里消失；
2. 父净 0（子 +20 / -20 抵消）在净变化筛选中被漏——新 API 的
   filter=changed 必须保留有命中后代的导航节点，不用父净变化判断整枝；
3. 历史 a/b 并非最新时 /api/browse 给错时点（browse 恒绑定最新快照与其
   同数据集前驱）；本 API 必须严格绑定请求里的 a/b。

数据不变量（docs/ARCHITECTURE）：缺失条目不是删除证据；单侧缺测与
结构节点不填 0；目录累计大小不可逐行相加当可回收空间。

全部合成数据（tmp_path 下的 /synthetic 根），显式设置 FATHOM_RUNTIME_DIR
与 FATHOM_SCAN_ROOT 指向测试临时目录，不触发真实 HOME 扫描或写生产库。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """FATHOM_RUNTIME_DIR / FATHOM_SCAN_ROOT 显式指向测试临时目录。

    与 test_reports_diff.py 同一隔离合同：环境变量是 helper/CLI/API 的
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
    denied: int = 0,
    collection_status: str | None = None,
    vanished_count: int = 0,
    confirmed_missing_count: int | None = None,
    path_unverified_count: int | None = None,
    exclude_names: str = "",
    hour: str = "12:00:00",
) -> int:
    """直接造表行（不经 du）。min_kb=None 表示 v3 之前的旧记录（NULL 不补造）。"""
    sizes = entries or {}
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status, vanished_count, exclude_names, "
        "confirmed_missing_count, path_unverified_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{day}T{hour}", root, len(sizes) + 1, denied, 0.0,
         max(sizes.values(), default=0), min_kb, collection_status, vanished_count,
         exclude_names, confirmed_missing_count, path_unverified_count),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
        [(sid, p, s) for p, s in sizes.items()],
    )
    conn.execute(
        "INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) VALUES (?,?,?)",
        (sid, 500 * 1024**3, 200 * 1024**3),
    )
    conn.commit()
    return sid


def _get_children(client: TestClient, a: int, b: int, path: str | None = None,
                   **params) -> dict:
    query = {"a": a, "b": b, **params}
    if path is not None:
        query["path"] = path
    r = client.get("/api/diff/children", params=query)
    assert r.status_code == 200, r.text
    return r.json()


class TestCounterexampleTop25CannotRebuildSiblingChain:
    """反例 1：折叠 Top25 无法还原父+20/子+18/孙+2 的完整同级关系。"""

    ROOT = "/synthetic/iss147-chain"

    def _pair(self) -> tuple[int, int]:
        """父 +20000 / 子 +18000 / 孙 +2000 的三链；另加一个无关兄弟。"""
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 100_000,
                f"{self.ROOT}/child": 82_000,
                f"{self.ROOT}/child/grand": 2_000,
                f"{self.ROOT}/other": 5_000,
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 120_000,
                f"{self.ROOT}/child": 100_000,
                f"{self.ROOT}/child/grand": 4_000,
                f"{self.ROOT}/other": 5_000,
            })
            return s1, s2
        finally:
            conn.close()

    def test_old_top25_loses_parent_level(self, client):
        """文档化旧 API 的缺口：子替换父后，父 +20000 不在 grown 里。"""
        s1, s2 = self._pair()
        r = client.get(f"/api/diff?a={s1}&b={s2}&topn=25")
        assert r.status_code == 200
        grown_paths = [c["path"] for c in r.json()["grown"]]
        assert f"{self.ROOT}/child" in grown_paths
        assert self.ROOT not in grown_paths  # 旧列表缺失父一级
        # 孙虽在，但「父剩余 +2000」无法从任何行推出——同级关系不可还原。

    def test_children_endpoint_rebuilds_full_chain(self, client):
        """新 API 在根层给出父行净变化，子行可展开，孙行精确到行。"""
        s1, s2 = self._pair()
        body = _get_children(client, s1, s2, self.ROOT)
        # 父行（target 本身）净变化 +20000 可见
        assert body["parent"]["path"] == self.ROOT
        assert body["parent"]["old_kb"] == 100_000
        assert body["parent"]["new_kb"] == 120_000
        assert body["parent"]["delta_kb"] == 20_000
        assert body["parent"]["status"] == "measured"
        # 直属子行：child（+18000，有后代可展开）与 other（无变化）
        by_name = {c["name"]: c for c in body["children"]}
        assert by_name["child"]["old_kb"] == 82_000
        assert by_name["child"]["new_kb"] == 100_000
        assert by_name["child"]["delta_kb"] == 18_000
        assert by_name["child"]["has_children"] is True
        assert by_name["child"]["has_changed_descendants"] is True
        assert by_name["other"]["delta_kb"] == 0  # measured 且无变化，如实为 0
        assert by_name["other"]["has_children"] is False

    def test_deep_link_drills_into_grandchild(self, client):
        """深链直达孙层：祖先链完整，孙行 delta +2000。"""
        s1, s2 = self._pair()
        body = _get_children(client, s1, s2, f"{self.ROOT}/child")
        # 祖先上下文：根一级（target 的父）
        assert [a["path"] for a in body["ancestors"]] == [self.ROOT]
        assert body["ancestors"][0]["delta_kb"] == 20_000
        # 父行 = child 自身
        assert body["parent"]["delta_kb"] == 18_000
        # 直属子 = grand，精确到行
        assert [c["name"] for c in body["children"]] == ["grand"]
        grand = body["children"][0]
        assert (grand["old_kb"], grand["new_kb"], grand["delta_kb"]) == (2_000, 4_000, 2_000)
        assert grand["has_children"] is False
        assert grand["has_changed_descendants"] is False


class TestCounterexampleNetZeroParentHiddenByChangeFilter:
    """反例 2：父净 0（子 +20/-20 抵消）不得被净变化筛选漏掉。"""

    ROOT = "/synthetic/iss147-netzero"

    def _pair(self) -> tuple[int, int]:
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 60_000,
                f"{self.ROOT}/zero": 40_000,
                f"{self.ROOT}/zero/up": 10_000,
                f"{self.ROOT}/zero/down": 30_000,
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 60_000,
                f"{self.ROOT}/zero": 40_000,
                f"{self.ROOT}/zero/up": 30_000,
                f"{self.ROOT}/zero/down": 10_000,
            })
            return s1, s2
        finally:
            conn.close()

    def test_old_diff_lists_siblings_but_parent_level_nowhere(self, client):
        """文档化旧 API 的缺口：up/down 都在，zero（净 0）不在任何列表。"""
        s1, s2 = self._pair()
        r = client.get(f"/api/diff?a={s1}&b={s2}&topn=25")
        assert r.status_code == 200
        body = r.json()
        grown = [c["path"] for c in body["grown"]]
        shrunk = [c["path"] for c in body["shrunk"]]
        assert f"{self.ROOT}/zero/up" in grown
        assert f"{self.ROOT}/zero/down" in shrunk
        everywhere = set(grown) | set(shrunk) | {c["path"] for c in body["added"]} \
            | {c["path"] for c in body["removed"]}
        assert f"{self.ROOT}/zero" not in everywhere

    def test_changed_filter_keeps_net_zero_parent(self, client):
        """filter=changed：净 0 的父行因后代有命中而保留，且可展开。"""
        s1, s2 = self._pair()
        body = _get_children(client, s1, s2, self.ROOT, filter="changed")
        by_name = {c["name"]: c for c in body["children"]}
        assert "zero" in by_name, "净 0 父行不得被净变化筛选漏掉"
        zero = by_name["zero"]
        assert zero["delta_kb"] == 0
        assert zero["has_changed_descendants"] is True
        assert zero["has_children"] is True
        # 展开后 +20/-20 对冲可见
        drill = _get_children(client, s1, s2, f"{self.ROOT}/zero")
        inner = {c["name"]: c for c in drill["children"]}
        assert inner["up"]["delta_kb"] == 20_000
        assert inner["down"]["delta_kb"] == -20_000


class TestCounterexampleBrowseWrongTimePointForHistoricalPair:
    """反例 3：browse 恒绑定最新快照；历史 a/b 区间必须由新 API 严格绑定。"""

    ROOT = "/synthetic/iss147-hist"

    def _trio(self) -> tuple[int, int, int]:
        """s1→s2 小变化；s2→s3 巨变化（制造「最新时点」干扰）。"""
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 10_000, f"{self.ROOT}/x": 6_000,
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 11_000, f"{self.ROOT}/x": 7_000,
            })
            s3 = _insert_snapshot(conn, "2026-09-03", self.ROOT, entries={
                self.ROOT: 99_000, f"{self.ROOT}/x": 95_000,
            })
            return s1, s2, s3
        finally:
            conn.close()

    def test_browse_is_bound_to_latest_pair(self, client):
        """文档化旧入口的时点缺口：browse 只能看 s2→s3，无法回看 s1→s2。"""
        s1, s2, s3 = self._trio()
        assert (s1, s2, s3) == (s1, s2, s3)
        r = client.get("/api/browse", params={"path": self.ROOT})
        assert r.status_code == 200
        body = r.json()
        assert body["snapshot_at"].startswith("2026-09-03")
        assert body["delta_kb"] == 88_000  # s3 - s2，不是 s2 - s1

    def test_children_binds_to_requested_pair(self, client):
        """新 API 严格绑定 a/b：old 取 s1、new 取 s2，不受 s3 存在影响。"""
        s1, s2, s3 = self._trio()
        body = _get_children(client, s1, s2, self.ROOT)
        assert body["a"]["id"] == s1 and body["b"]["id"] == s2
        assert body["a"]["created_at"].startswith("2026-09-01")
        assert body["b"]["created_at"].startswith("2026-09-02")
        assert body["parent"]["old_kb"] == 10_000
        assert body["parent"]["new_kb"] == 11_000
        assert body["parent"]["delta_kb"] == 1_000
        by_name = {c["name"]: c for c in body["children"]}
        assert (by_name["x"]["old_kb"], by_name["x"]["new_kb"],
                by_name["x"]["delta_kb"]) == (6_000, 7_000, 1_000)
        # 逆序（b 为较早一方）也按请求绑定，不静默换向
        rev = _get_children(client, s2, s1, self.ROOT)
        assert rev["parent"]["old_kb"] == 11_000
        assert rev["parent"]["new_kb"] == 10_000
        assert rev["parent"]["delta_kb"] == -1_000


class TestPaginationStableAcrossPages:
    """>100 兄弟：分页无重复漏项；未展示分页不得当作未细分变化。"""

    ROOT = "/synthetic/iss147-pages"
    N = 130

    def _pair(self):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                **{f"{self.ROOT}/c{i:03d}": 1_000 + i for i in range(self.N)},
                self.ROOT: 300_000,
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                **{f"{self.ROOT}/c{i:03d}": 1_000 + 2 * i for i in range(self.N)},
                self.ROOT: 300_000 + self.N,
            })
            return s1, s2
        finally:
            conn.close()

    def test_default_limit_pages_without_dup_or_gap(self, client):
        s1, s2 = self._pair()
        first = _get_children(client, s1, s2, self.ROOT)
        assert first["pagination"]["limit"] == 100
        assert first["pagination"]["total"] == self.N
        assert first["pagination"]["returned"] == 100
        assert first["pagination"]["has_more"] is True
        assert first["pagination"]["next_cursor"] is not None
        assert first["pagination"]["offset"] == 0

        second = _get_children(client, s1, s2, self.ROOT,
                               cursor=first["pagination"]["next_cursor"])
        assert second["pagination"]["offset"] == 100
        assert second["pagination"]["returned"] == self.N - 100
        assert second["pagination"]["has_more"] is False
        assert second["pagination"]["next_cursor"] is None

        names_p1 = [c["name"] for c in first["children"]]
        names_p2 = [c["name"] for c in second["children"]]
        assert len(set(names_p1) | set(names_p2)) == self.N  # 无重复
        assert set(names_p1) & set(names_p2) == set()
        expected = {f"c{i:03d}" for i in range(self.N)}
        assert set(names_p1) | set(names_p2) == expected  # 无漏项

    def test_explicit_limit_and_final_short_page(self, client):
        s1, s2 = self._pair()
        page = _get_children(client, s1, s2, self.ROOT, limit=125)
        assert page["pagination"]["returned"] == 125
        assert page["pagination"]["has_more"] is True
        nxt = _get_children(client, s1, s2, self.ROOT, limit=125,
                            cursor=page["pagination"]["next_cursor"])
        assert nxt["pagination"]["returned"] == 5
        assert nxt["pagination"]["has_more"] is False

    def test_stable_order_across_identical_requests(self, client):
        """同参数两次请求顺序一致（稳定排序，分页可安全重放）。"""
        s1, s2 = self._pair()
        r1 = _get_children(client, s1, s2, self.ROOT, limit=10)
        r2 = _get_children(client, s1, s2, self.ROOT, limit=10)
        assert [c["path"] for c in r1["children"]] == [c["path"] for c in r2["children"]]


class TestCursorBinding:
    """游标绑定 a/b/path/filter/sort；错配与坏游标 400。"""

    ROOT = "/synthetic/iss147-cursor"

    def _pair(self):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 10_000,
                **{f"{self.ROOT}/d{i:02d}": 1_000 + 10 * i for i in range(6)},
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 10_000,
                **{f"{self.ROOT}/d{i:02d}": 1_000 + 20 * i for i in range(6)},
            })
            return s1, s2
        finally:
            conn.close()

    def _first_cursor(self, client, s1, s2, **params) -> str:
        body = _get_children(client, s1, s2, self.ROOT, limit=2, **params)
        assert body["pagination"]["next_cursor"]
        return body["pagination"]["next_cursor"]

    def test_cursor_rejects_other_path(self, client):
        s1, s2 = self._pair()
        cursor = self._first_cursor(client, s1, s2)
        r = client.get("/api/diff/children", params={
            "a": s1, "b": s2, "path": f"{self.ROOT}/d01", "cursor": cursor})
        assert r.status_code == 400
        assert "游标" in r.json()["detail"]

    def test_cursor_rejects_other_filter_or_sort(self, client):
        s1, s2 = self._pair()
        cursor = self._first_cursor(client, s1, s2)
        for extra in ({"filter": "changed"}, {"sort": "name"}):
            r = client.get("/api/diff/children", params={
                "a": s1, "b": s2, "path": self.ROOT, "cursor": cursor, **extra})
            assert r.status_code == 400, extra

    def test_cursor_rejects_other_snapshot_pair(self, client):
        s1, s2 = self._pair()
        conn = db.connect()
        try:
            s3 = _insert_snapshot(conn, "2026-09-03", self.ROOT, entries={
                self.ROOT: 11_000,
                **{f"{self.ROOT}/d{i:02d}": 1_000 + 30 * i for i in range(6)},
            })
        finally:
            conn.close()
        cursor = self._first_cursor(client, s1, s2)
        r = client.get("/api/diff/children", params={
            "a": s2, "b": s3, "path": self.ROOT, "cursor": cursor})
        assert r.status_code == 400

    def test_garbage_cursor_rejected(self, client):
        s1, s2 = self._pair()
        for bad in ("!!!not-base64!!!", "eyJ2IjogMQ", ""):
            r = client.get("/api/diff/children", params={
                "a": s1, "b": s2, "path": self.ROOT, "cursor": bad})
            assert r.status_code == 400, bad
            assert "游标" in r.json()["detail"]


class TestParameterValidation:
    """错误参数 400：缺 a/b、非整数、非法 filter/sort、limit 越界。"""

    ROOT = "/synthetic/iss147-params"

    def _one_snapshot(self):
        conn = db.connect()
        try:
            return _insert_snapshot(conn, "2026-09-01", self.ROOT,
                                    entries={self.ROOT: 1_000})
        finally:
            conn.close()

    def test_missing_a_or_b_is_400(self, client):
        sid = self._one_snapshot()
        for params in ({"b": sid}, {"a": sid}, {}):
            r = client.get("/api/diff/children", params=params)
            assert r.status_code == 400, params

    def test_non_integer_a_is_400(self, client):
        sid = self._one_snapshot()
        r = client.get("/api/diff/children", params={"a": "abc", "b": sid})
        assert r.status_code == 400

    def test_bad_filter_and_sort_are_400(self, client):
        sid = self._one_snapshot()
        for extra in ({"filter": "bogus"}, {"filter": ""}, {"sort": "bogus"},
                      {"sort": ""}):
            r = client.get("/api/diff/children", params={
                "a": sid, "b": sid, **extra})
            assert r.status_code == 400, extra

    def test_limit_out_of_range_is_400(self, client):
        sid = self._one_snapshot()
        for bad in ("0", "-1", "501", "9999"):
            r = client.get("/api/diff/children", params={
                "a": sid, "b": sid, "limit": bad})
            assert r.status_code == 400, bad


class TestErrorSemantics:
    """快照缺失 404、跨数据集 400、路径越界 400、无记录路径 404。"""

    ROOT = "/synthetic/root-a"

    def test_missing_snapshot_404(self, client):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT,
                                  entries={self.ROOT: 1_000})
        finally:
            conn.close()
        r = client.get("/api/diff/children", params={"a": s1, "b": 99999})
        assert r.status_code == 404
        assert "99999" in r.json()["detail"]
        r = client.get("/api/diff/children", params={"a": 99999, "b": s1})
        assert r.status_code == 404

    def test_cross_root_dataset_rejected(self, client):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT,
                                  entries={self.ROOT: 1_000})
            s2 = _insert_snapshot(conn, "2026-09-02", "/synthetic/root-b",
                                  entries={"/synthetic/root-b": 1_000})
        finally:
            conn.close()
        r = client.get("/api/diff/children", params={"a": s1, "b": s2})
        assert r.status_code == 400
        assert "数据集" in r.json()["detail"]

    def test_cross_threshold_and_excludes_rejected(self, client):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, min_kb=1024,
                                  entries={self.ROOT: 1_000})
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, min_kb=2048,
                                  entries={self.ROOT: 1_000})
            s3 = _insert_snapshot(conn, "2026-09-03", self.ROOT, min_kb=1024,
                                  exclude_names="skip.noindex",
                                  entries={self.ROOT: 1_000})
        finally:
            conn.close()
        for pair in ({"a": s1, "b": s2}, {"a": s1, "b": s3}):
            r = client.get("/api/diff/children", params=pair)
            assert r.status_code == 400, pair
            assert "数据集" in r.json()["detail"]

    def test_path_outside_root_by_segment(self, client):
        """相似前缀根：/synthetic/root-ab 不是 /synthetic/root-a 的子路径。"""
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 1_000, f"{self.ROOT}/x": 500})
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 1_000, f"{self.ROOT}/x": 600})
        finally:
            conn.close()
        r = client.get("/api/diff/children", params={
            "a": s1, "b": s2, "path": "/synthetic/root-ab"})
        assert r.status_code == 400
        r = client.get("/api/diff/children", params={
            "a": s1, "b": s2, "path": "relative/path"})
        assert r.status_code == 400
        r = client.get("/api/diff/children", params={
            "a": s1, "b": s2, "path": f"{self.ROOT}/../escape"})
        assert r.status_code == 400

    def test_path_with_no_records_on_either_side_404(self, client):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 1_000, f"{self.ROOT}/x": 500})
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 1_000, f"{self.ROOT}/x": 600})
        finally:
            conn.close()
        r = client.get("/api/diff/children", params={
            "a": s1, "b": s2, "path": f"{self.ROOT}/never-recorded"})
        assert r.status_code == 404
        detail = r.json()["detail"]
        assert "无记录" in detail and "阈值" in detail  # 不冒充空目录

    def test_trailing_slash_normalized(self, client):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 1_000, f"{self.ROOT}/x": 500})
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 1_000, f"{self.ROOT}/x": 600})
        finally:
            conn.close()
        body = _get_children(client, s1, s2, f"{self.ROOT}/x/")
        assert body["path"] == f"{self.ROOT}/x"
        assert body["parent"]["delta_kb"] == 100


class TestRootSlashDataset:
    """root=/ 数据集：根归一化为 "/"，前缀不产生 "//"，根不是自己的孩子。"""

    def _pair(self):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", "/", entries={
                "/": 100_000, "/a": 60_000, "/a/inner": 30_000, "/ab": 10_000,
            })
            s2 = _insert_snapshot(conn, "2026-09-02", "/", entries={
                "/": 110_000, "/a": 66_000, "/a/inner": 33_000, "/ab": 10_000,
            })
            return s1, s2
        finally:
            conn.close()

    def test_root_children_and_prefix_bounds(self, client):
        s1, s2 = self._pair()
        body = _get_children(client, s1, s2, "/")
        assert body["path"] == "/"
        assert body["parent"]["old_kb"] == 100_000
        assert body["parent"]["delta_kb"] == 10_000
        by_name = {c["name"] for c in body["children"]}
        assert by_name == {"a", "ab"}  # 根不是自己的孩子；前缀段不互吞
        # /a 的直属子只有 /a/inner；/ab 不进 /a 的子树
        drill = _get_children(client, s1, s2, "/a")
        assert [c["name"] for c in drill["children"]] == ["inner"]
        assert drill["ancestors"][0]["path"] == "/"
        # 相似前缀路径 /ab 不被当成 /a 的孩子
        r = client.get("/api/diff/children", params={
            "a": s1, "b": s2, "path": "/ab"})
        assert r.status_code == 200
        assert [c["name"] for c in r.json()["children"]] == []


class TestSimilarPrefixSiblings:
    """/a 与 /ab 互为兄弟：段边界决定父子，不用字符串前缀猜。"""

    ROOT = "/synthetic/iss147-prefix"

    def test_a_children_do_not_include_ab_subtree(self, client):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                f"{self.ROOT}/a": 5_000, f"{self.ROOT}/a/x": 3_000,
                f"{self.ROOT}/ab": 8_000, f"{self.ROOT}/ab/y": 7_000,
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                f"{self.ROOT}/a": 5_000, f"{self.ROOT}/a/x": 4_000,
                f"{self.ROOT}/ab": 9_000, f"{self.ROOT}/ab/y": 7_000,
            })
        finally:
            conn.close()
        body = _get_children(client, s1, s2, f"{self.ROOT}/a")
        assert [c["name"] for c in body["children"]] == ["x"]
        assert body["children"][0]["delta_kb"] == 1_000
        # 根层看到 a 与 ab 两个独立兄弟
        root_body = _get_children(client, s1, s2, self.ROOT)
        by_name = {c["name"]: c for c in root_body["children"]}
        assert by_name["a"]["delta_kb"] == 0
        assert by_name["ab"]["delta_kb"] == 1_000


class TestWeirdChildNames:
    """HTML/换行/引号/Unicode 目录名：JSON 精确转义，无注入面。"""

    ROOT = "/synthetic/iss147-weird"

    def test_names_roundtrip_exactly(self, client):
        names = ["<img src=x onerror=alert(1)>", "line1\nline2", "tab\tname",
                 'quo"te', "中文目录", "semi;colon"]
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                **{f"{self.ROOT}/{n}": 1_000 for n in names}})
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                **{f"{self.ROOT}/{n}": 2_000 for n in names}})
        finally:
            conn.close()
        r = client.get("/api/diff/children", params={"a": s1, "b": s2,
                                                     "path": self.ROOT})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        body = r.json()
        got = {c["name"] for c in body["children"]}
        assert got == set(names)
        for c in body["children"]:
            assert c["delta_kb"] == 1_000 and c["status"] == "measured"
        # 深链到含特殊字符的子目录
        weird = f"{self.ROOT}/line1\nline2"
        r2 = client.get("/api/diff/children", params={"a": s1, "b": s2,
                                                      "path": weird})
        assert r2.status_code == 200
        assert r2.json()["parent"]["delta_kb"] == 1_000


class TestStatusesAndHonesty:
    """四态行、父缺子有的结构节点、单侧缺测不填 0、覆盖与计数分开。"""

    ROOT = "/synthetic/iss147-status"

    def test_four_statuses_in_one_response(self, client):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 10_000,
                f"{self.ROOT}/keep": 5_000,       # measured（无变化）
                f"{self.ROOT}/grow": 5_000,       # measured（+1k）
                f"{self.ROOT}/gone": 5_000,       # a-only → unrecorded
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 12_000,
                f"{self.ROOT}/keep": 5_000,
                f"{self.ROOT}/grow": 6_000,
                f"{self.ROOT}/fresh": 4_000,      # b-only → first_recorded
            })
        finally:
            conn.close()
        body = _get_children(client, s1, s2, self.ROOT)
        by = {c["name"]: c for c in body["children"]}
        assert by["keep"]["status"] == "measured"
        assert by["keep"]["delta_kb"] == 0  # measured 无变化如实为 0
        assert by["grow"]["delta_kb"] == 1_000
        assert by["gone"]["status"] == "unrecorded"
        assert by["gone"]["new_kb"] is None and by["gone"]["delta_kb"] is None
        assert by["gone"]["old_kb"] == 5_000
        assert by["fresh"]["status"] == "first_recorded"
        assert by["fresh"]["old_kb"] is None and by["fresh"]["delta_kb"] is None
        assert by["fresh"]["new_kb"] == 4_000
        # 记录项计数与覆盖分开：子树记录项数（不含 target 本身）
        assert body["counts"]["a_entries"] == 3
        assert body["counts"]["b_entries"] == 3

    def test_structural_parent_missing_children_present(self, client):
        """父缺子有：中间两级无直接记录，仅靠已记录后代成为结构节点。"""
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                f"{self.ROOT}/s/big/deep": 5_000,
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                f"{self.ROOT}/s/big/deep": 8_000,
            })
        finally:
            conn.close()
        body = _get_children(client, s1, s2, self.ROOT)
        s_row = body["children"][0]
        assert s_row["name"] == "s"
        assert s_row["status"] == "structural"
        assert s_row["old_kb"] is None and s_row["new_kb"] is None
        assert s_row["delta_kb"] is None  # 结构节点不填 0
        assert s_row["has_children"] is True
        assert s_row["has_changed_descendants"] is True
        # 层层下钻直到 measured
        mid = _get_children(client, s1, s2, f"{self.ROOT}/s/big")
        assert mid["parent"]["status"] == "structural"
        deep = mid["children"][0]
        assert deep["name"] == "deep"
        assert deep["status"] == "measured"
        assert deep["delta_kb"] == 3_000

    def test_changed_filter_keeps_structural_only_with_hits(self, client):
        """结构节点仅在后代有命中时被 filter=changed 保留。"""
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                f"{self.ROOT}/hot/deep": 5_000, f"{self.ROOT}/cold/deep": 5_000,
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                f"{self.ROOT}/hot/deep": 8_000, f"{self.ROOT}/cold/deep": 5_000,
            })
        finally:
            conn.close()
        body = _get_children(client, s1, s2, self.ROOT, filter="changed")
        assert [c["name"] for c in body["children"]] == ["hot"]

    def test_partial_coverage_and_threshold_surfaced(self, client):
        """阈值/排除/采集状态随响应下发；受限快照不被冒充完整。"""
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 10_000})
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 11_000}, denied=3, collection_status="partial",
                vanished_count=2, confirmed_missing_count=1,
                path_unverified_count=1)
        finally:
            conn.close()
        body = _get_children(client, s1, s2, self.ROOT)
        assert body["dataset"]["min_kb"] == 1024
        assert body["dataset"]["exclude_names"] == ""
        assert body["dataset"]["root"] == self.ROOT
        assert body["a"]["collection_status"] is None
        assert body["b"]["collection_status"] == "partial"
        assert body["b"]["denied_count"] == 3
        assert body["b"]["vanished_count"] == 2
        assert body["b"]["confirmed_missing_count"] == 1
        assert body["b"]["path_unverified_count"] == 1

    def test_a_only_leaf_dir_returns_empty_children_not_404(self, client):
        """a 有直接记录、b 无任何记录的叶子：200 空子行（unrecorded 父行）。"""
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 10_000, f"{self.ROOT}/leaf": 2_000})
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 10_000})
        finally:
            conn.close()
        body = _get_children(client, s1, s2, f"{self.ROOT}/leaf")
        assert body["parent"]["status"] == "unrecorded"
        assert body["parent"]["old_kb"] == 2_000
        assert body["parent"]["new_kb"] is None
        assert body["children"] == []

    def test_same_snapshot_yields_zero_deltas(self, client):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 10_000, f"{self.ROOT}/x": 5_000})
        finally:
            conn.close()
        body = _get_children(client, s1, s1, self.ROOT)
        assert body["parent"]["delta_kb"] == 0
        assert all(c["delta_kb"] == 0 for c in body["children"])
        changed = _get_children(client, s1, s1, self.ROOT, filter="changed")
        assert changed["children"] == []  # 无任何命中，空页是事实

    def test_legacy_null_min_kb_pair_comparable(self, client):
        """v3 之前旧记录（min_kb NULL）同根彼此可比，dataset.min_kb=null。"""
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, min_kb=None,
                                  entries={self.ROOT: 1_000, f"{self.ROOT}/x": 500})
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, min_kb=None,
                                  entries={self.ROOT: 1_000, f"{self.ROOT}/x": 700})
        finally:
            conn.close()
        body = _get_children(client, s1, s2, self.ROOT)
        assert body["dataset"]["min_kb"] is None
        by = {c["name"]: c for c in body["children"]}
        assert by["x"]["delta_kb"] == 200


class TestSiblingOrdering:
    """sort 只在同级内生效：delta/size/name 三序 + 并列稳定。"""

    ROOT = "/synthetic/iss147-sort"

    def _pair(self):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", self.ROOT, entries={
                self.ROOT: 100_000,
                f"{self.ROOT}/big": 50_000, f"{self.ROOT}/aaa": 20_000,
                f"{self.ROOT}/tiny": 1_000,
            })
            s2 = _insert_snapshot(conn, "2026-09-02", self.ROOT, entries={
                self.ROOT: 100_000,
                f"{self.ROOT}/big": 52_000,    # +2000（measured）
                f"{self.ROOT}/aaa": 18_000,    # -2000（measured）
                f"{self.ROOT}/tiny": 1_000,    # 0（measured）
                f"{self.ROOT}/zz": 9_000,      # first_recorded（delta null）
            })
            return s1, s2
        finally:
            conn.close()

    def test_sort_delta_orders_by_abs_delta_desc(self, client):
        s1, s2 = self._pair()
        body = _get_children(client, s1, s2, self.ROOT, sort="delta")
        names = [c["name"] for c in body["children"]]
        # |±2000| 并列按名稳定（aaa < big）；0 之后；first_recorded（delta
        # null）再后——单侧缺测不与有差分行混排
        assert names == ["aaa", "big", "tiny", "zz"]

    def test_sort_size_orders_by_new_kb_desc(self, client):
        s1, s2 = self._pair()
        body = _get_children(client, s1, s2, self.ROOT, sort="size")
        assert [c["name"] for c in body["children"]] == ["big", "aaa", "zz",
                                                         "tiny"]

    def test_sort_name_orders_lexically(self, client):
        s1, s2 = self._pair()
        body = _get_children(client, s1, s2, self.ROOT, sort="name")
        assert [c["name"] for c in body["children"]] == ["aaa", "big", "tiny",
                                                         "zz"]

    def test_default_sort_is_delta(self, client):
        s1, s2 = self._pair()
        default = _get_children(client, s1, s2, self.ROOT)
        explicit = _get_children(client, s1, s2, self.ROOT, sort="delta")
        assert ([c["path"] for c in default["children"]]
                == [c["path"] for c in explicit["children"]])
        assert default["query"] == {"filter": "all", "sort": "delta"}
