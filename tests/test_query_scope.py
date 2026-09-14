"""ISS-024 查询口径、最新窗口与树裁剪：趋势/目录视图有限、同数据集、可解释。

反例来源 docs/TASKS.md ISS-024（AUD-09 / AUD-04）与 docs/ARCHITECTURE.md：
- AUD-09：volume-trend / trend 用 ASC LIMIT，序列超过 limit 时取的是最早
  N 条，最新点反而被截掉；且不同数据集（换根或换 min_kb 口径）的历史
  混在同一条序列里。
- gap 语义：trend?path= 只返回确有记录的历史点，缺失不补点（钉住现状）。
- 树预算：节点上限必须在 SQL/构建前生效，截断事实（truncated/数量）可见，
  子树不重复归属、无孤儿丢失。
- 三类"没有数据"必须可区分：坏参数 400、快照不存在 404、有效快照但低于
  显示阈值 200 空结果、路径无记录 200 空点。
- root=/ 边界：根归一化为 "/"，browse/trees 正常返回，根不得成为自己的孩子。
- 单快照子目录不得被标成全量增长（AUD-04，ISS-021 无基线语义）。

全部合成数据（tmp_path 下的 /synthetic 根），显式设置 FATHOM_RUNTIME_DIR
与 FATHOM_SCAN_ROOT 指向测试临时目录，不触发真实 HOME 扫描或写生产库
（docs/TESTING.md 隔离要求）。大树压力样例直接向 entries 造行，不跑 du。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db

ROOT_A = "/synthetic/root-a"
ROOT_SLASH = "/"


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """FATHOM_RUNTIME_DIR / FATHOM_SCAN_ROOT 显式指向测试临时目录。

    环境变量是 helper/CLI/API 的共同入口（config 文档）；同时把 config
    兼容常量指到同一运行根，保证本进程内的 db/api 读写全部留在临时目录。
    monkeypatch 结束后自动还原，不影响其他测试文件。
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
    min_kb: int | None = None,
    entries: dict[str, int] | None = None,
    free_bytes: int = 200 * 1024**3,
    collection_status: str | None = None,
) -> int:
    """直接造表行（不经 du）。min_kb=None 表示 v3 之前的旧记录（NULL 不补造）。

    free_bytes 可逐快照变化，用于区分 volume-trend 返回的到底是哪些点。
    """
    sizes = entries or {}
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status) VALUES (?,?,?,?,?,?,?,?)",
        (f"{day}T12:00:00", root, len(sizes) + 1, 0, 0.0,
         max(sizes.values(), default=0), min_kb, collection_status),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
        [(sid, p, s) for p, s in sizes.items()],
    )
    conn.execute(
        "INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) VALUES (?,?,?)",
        (sid, 500 * 1024**3, free_bytes),
    )
    conn.commit()
    return sid


def _flatten(node: dict) -> list[dict]:
    """深度优先展平树节点；等价表格的一行 = 一个节点。"""
    out = [node]
    for child in node.get("children", []):
        out.extend(_flatten(child))
    return out


def _dates(rows: list[dict]) -> list[str]:
    return [r["created_at"][:10] for r in rows]


class TestIsolationContract:
    def test_env_vars_drive_runtime_config(self, tmp_path):
        """派工要求的隔离合同：显式设置的 FATHOM_* 环境变量真实生效。"""
        rc = config.RuntimeConfig.from_env()
        assert rc.runtime_dir == (tmp_path / "runtime").resolve()
        assert rc.scan_root == (tmp_path / "scanroot").resolve()
        assert str(config.DB_PATH).startswith(str((tmp_path / "runtime").resolve()))


# ---------- volume-trend：最新窗口 + 数据集隔离（AUD-09） ----------


class TestVolumeTrend:
    def _seed_series(self, days: list[str], free_base: int) -> None:
        conn = db.connect()
        try:
            for i, day in enumerate(days):
                _insert_snapshot(
                    conn, day, ROOT_A, min_kb=1024,
                    entries={ROOT_A: 10_000 + i},
                    free_bytes=free_base + i,
                )
        finally:
            conn.close()

    def test_over_limit_series_keeps_latest_point(self, client):
        """>limit 的序列返回最新 N 条：最新点必须在，最早端被截（AUD-09）。"""
        self._seed_series(
            ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
             "2026-09-05", "2026-09-06"],
            free_base=1000,
        )
        r = client.get("/api/volume-trend?limit=4")
        assert r.status_code == 200
        body = r.json()
        # 最新窗口：d3..d6，正序输出；旧实现 ASC LIMIT 会给 d1..d4 丢掉最新点
        assert _dates(body) == ["2026-09-03", "2026-09-04",
                                "2026-09-05", "2026-09-06"]
        assert [b["free_bytes"] for b in body] == [1002, 1003, 1004, 1005]

    def test_output_is_ascending_for_full_history(self, client):
        """未超 limit 时也保持时间正序（图表可直接渲染）。"""
        self._seed_series(["2026-09-01", "2026-09-02", "2026-09-03"], free_base=7)
        body = client.get("/api/volume-trend").json()
        assert _dates(body) == ["2026-09-01", "2026-09-02", "2026-09-03"]
        # 表格化：行结构稳定
        assert set(body[0]) == {"created_at", "total_bytes", "free_bytes"}

    def test_no_cross_root_mixing(self, client):
        """换根后旧根历史不混入：只返回最新快照所属数据集的点。"""
        conn = db.connect()
        try:
            for day in ("2026-09-01", "2026-09-02"):
                _insert_snapshot(conn, day, ROOT_A, min_kb=1024,
                                 entries={ROOT_A: 10_000}, free_bytes=11)
            for day in ("2026-09-03", "2026-09-04"):
                _insert_snapshot(conn, day, "/synthetic/root-b", min_kb=1024,
                                 entries={"/synthetic/root-b": 5_000}, free_bytes=99)
        finally:
            conn.close()
        body = client.get("/api/volume-trend").json()
        assert _dates(body) == ["2026-09-03", "2026-09-04"]
        assert all(b["free_bytes"] == 99 for b in body)

    def test_no_cross_threshold_mixing(self, client):
        """同根换 min_kb 口径 = 新数据集，旧口径历史不混点（ISS-021 口径）。"""
        conn = db.connect()
        try:
            for day in ("2026-09-01", "2026-09-02"):
                _insert_snapshot(conn, day, ROOT_A, min_kb=1024,
                                 entries={ROOT_A: 10_000}, free_bytes=11)
            _insert_snapshot(conn, "2026-09-03", ROOT_A, min_kb=4096,
                             entries={ROOT_A: 8_000}, free_bytes=77)
        finally:
            conn.close()
        body = client.get("/api/volume-trend").json()
        assert _dates(body) == ["2026-09-03"]

    def test_empty_db_returns_empty_list(self, client):
        r = client.get("/api/volume-trend")
        assert r.status_code == 200
        assert r.json() == []

    def test_bad_limit_rejected_400(self, client):
        for bad in ("limit=1", "limit=0", "limit=9999", "limit=abc"):
            r = client.get(f"/api/volume-trend?{bad}")
            assert r.status_code == 400, bad


# ---------- trend?path=：最新窗口、gap 语义、数据集隔离 ----------


class TestPathTrend:
    def _seed_path_history(self) -> None:
        conn = db.connect()
        try:
            sizes = {"2026-09-01": 100, "2026-09-02": 200, "2026-09-03": 300,
                     "2026-09-04": 400, "2026-09-05": 500, "2026-09-06": 600}
            for day, size in sizes.items():
                _insert_snapshot(conn, day, ROOT_A, min_kb=1024,
                                 entries={ROOT_A: 10_000, f"{ROOT_A}/app": size})
        finally:
            conn.close()

    def test_over_limit_series_keeps_latest_point(self, client):
        path = f"{ROOT_A}/app"
        self._seed_path_history()
        body = client.get(f"/api/trend?path={path}&limit=4").json()
        assert body["path"] == path
        # 最新窗口 d3..d6 正序；旧实现 ASC LIMIT 给 d1..d4，最新点被截掉
        assert _dates(body["points"]) == ["2026-09-03", "2026-09-04",
                                          "2026-09-05", "2026-09-06"]
        assert [p["size_kb"] for p in body["points"]] == [300, 400, 500, 600]

    def test_gap_semantics_missing_days_not_filled(self, client):
        """路径缺失的快照不补点：d3 无记录，序列里没有 d3（现状钉住）。"""
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000, f"{ROOT_A}/x": 100})
            _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000, f"{ROOT_A}/x": 200})
            # d3：x 低于阈值未入库（无 entries 行）
            _insert_snapshot(conn, "2026-09-03", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000})
            _insert_snapshot(conn, "2026-09-04", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000, f"{ROOT_A}/x": 400})
        finally:
            conn.close()
        body = client.get(f"/api/trend?path={ROOT_A}/x").json()
        assert _dates(body["points"]) == ["2026-09-01", "2026-09-02", "2026-09-04"]
        assert set(body["points"][0]) == {"created_at", "size_kb"}

    def test_no_cross_threshold_mixing(self, client):
        """换口径后同一路径的新数据集点不与旧口径历史混点。"""
        path = f"{ROOT_A}/x"
        conn = db.connect()
        try:
            for day, size in (("2026-09-01", 100), ("2026-09-02", 200)):
                _insert_snapshot(conn, day, ROOT_A, min_kb=1024,
                                 entries={ROOT_A: 10_000, path: size})
            _insert_snapshot(conn, "2026-09-03", ROOT_A, min_kb=4096,
                             entries={ROOT_A: 8_000, path: 900})
        finally:
            conn.close()
        body = client.get(f"/api/trend?path={path}").json()
        assert _dates(body["points"]) == ["2026-09-03"]
        assert body["points"][0]["size_kb"] == 900

    def test_no_cross_root_mixing_nested_root(self, client):
        """嵌套监控根：同一路径在父根与子根各是不同数据集，不混点。"""
        path = f"{ROOT_A}/sub/x"
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000, path: 100})
            _insert_snapshot(conn, "2026-09-02", f"{ROOT_A}/sub", min_kb=1024,
                             entries={f"{ROOT_A}/sub": 5_000, path: 300})
        finally:
            conn.close()
        body = client.get(f"/api/trend?path={path}").json()
        assert _dates(body["points"]) == ["2026-09-02"]

    def test_unknown_path_empty_points_not_error(self, client):
        """路径从未记录 = 无记录（200 空点），不是 404/400。"""
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000})
        finally:
            conn.close()
        r = client.get(f"/api/trend?path={ROOT_A}/never-recorded")
        assert r.status_code == 200
        assert r.json() == {"path": f"{ROOT_A}/never-recorded", "points": []}

    def test_bad_params_rejected_400(self, client):
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000})
        finally:
            conn.close()
        base = f"/api/trend?path={ROOT_A}/x"
        for bad in ("&limit=1", "&limit=0", "&limit=abc"):
            assert client.get(base + bad).status_code == 400, bad
        assert client.get("/api/trend?limit=10").status_code == 400  # 缺 path
        assert client.get(f"/api/trend?path=&limit=10").status_code == 400  # 空 path


# ---------- trees：预算前置、截断可见、错误语义、root=/ ----------


class TestTreesSemantics:
    def _seed(self, entries: dict[str, int], *, root: str = ROOT_A,
              min_kb: int = 1024, day: str = "2026-09-01") -> int:
        conn = db.connect()
        try:
            return _insert_snapshot(conn, day, root, min_kb=min_kb, entries=entries)
        finally:
            conn.close()

    def test_no_snapshots_returns_null_snapshot_id(self, client):
        r = client.get("/api/trees")
        assert r.status_code == 200
        body = r.json()
        assert body["snapshot_id"] is None
        assert body["children"] == []
        assert body["truncated"] is False

    def test_nonexistent_snapshot_404(self, client):
        self._seed({ROOT_A: 10_000})
        r = client.get("/api/trees?snapshot_id=999")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_below_threshold_returns_empty_not_404(self, client):
        """有效快照但全部目录 < 显示阈值：空结果，不再与"快照不存在"混 404。"""
        self._seed({ROOT_A: 10_000, f"{ROOT_A}/small": 100})
        r = client.get("/api/trees?min_kb=51200")
        assert r.status_code == 200
        body = r.json()
        assert body["snapshot_id"] is not None
        assert body["matched_count"] == 0
        assert body["node_count"] == 0
        assert body["truncated"] is False
        assert body["children"][0].get("children", []) == []

    def test_bad_min_kb_rejected_400(self, client):
        self._seed({ROOT_A: 10_000})
        for bad in ("min_kb=0", "min_kb=-5", "min_kb=abc"):
            r = client.get(f"/api/trees?{bad}")
            assert r.status_code == 400, bad

    def test_bad_snapshot_id_rejected_400(self, client):
        r = client.get("/api/trees?snapshot_id=abc")
        assert r.status_code == 400

    def test_normal_tree_shape_table_renderable(self, client):
        """常规树：嵌套正确、每个路径恰好出现一次、行字段稳定可表格化。"""
        self._seed({
            ROOT_A: 100_000,
            f"{ROOT_A}/a": 60_000,
            f"{ROOT_A}/a/b": 30_000,
            f"{ROOT_A}/c": 20_000,
        })
        body = client.get("/api/trees?min_kb=1").json()
        assert body["root"] == ROOT_A
        nodes = _flatten(body["children"][0])
        paths = [n["path"] for n in nodes]
        assert len(paths) == len(set(paths)) == 4  # 无重复归属
        assert set(paths) == {ROOT_A, f"{ROOT_A}/a", f"{ROOT_A}/a/b", f"{ROOT_A}/c"}
        by_path = {n["path"]: n for n in nodes}
        assert [c["path"] for c in by_path[f"{ROOT_A}/a"]["children"]] == [f"{ROOT_A}/a/b"]
        for n in nodes:  # 表格化合同：每行都有可展示字段
            assert {"name", "value", "path"} <= set(n)
        assert body["truncated"] is False
        assert body["node_count"] == 4

    def test_root_slash_returns_normal_tree(self, client):
        """root=/ 边界：根归一化为 "/"，不得成为自己的孩子，层级正常。"""
        self._seed({
            ROOT_SLASH: 1_000_000,
            "/Users": 500_000,
            "/Users/home": 200_000,
            "/etc": 50_000,
        }, root=ROOT_SLASH)
        body = client.get("/api/trees?min_kb=1").json()
        assert body["root"] == ROOT_SLASH
        top = body["children"][0]
        assert top["path"] == ROOT_SLASH
        assert top["value"] == 1_000_000
        child_paths = [c["path"] for c in top.get("children", [])]
        assert set(child_paths) == {"/Users", "/etc"}  # "/" 不在自己的 children 里
        nodes = _flatten(top)
        assert len(nodes) == len(set(n["path"] for n in nodes)) == 4


class TestTreesBudget:
    """节点预算在 SQL 层生效：截断可见、无孤儿丢失、无重复归属。"""

    def _seed_flat(self, count: int) -> None:
        conn = db.connect()
        try:
            entries = {ROOT_A: (count + 1) * 10_000}
            entries.update(
                {f"{ROOT_A}/d{i}": i for i in range(1, count + 1)}
            )
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1, entries=entries)
        finally:
            conn.close()

    def test_budget_truncation_visible_small(self, client, monkeypatch):
        """用小预算钉住截断合同：truncated/matched/node_count 可解释。"""
        self._seed_flat(5)  # root + 5 个目录 = 6 行
        monkeypatch.setattr(api, "TREE_MAX_NODES", 3)
        body = client.get(f"/api/trees?min_kb=1").json()
        assert body["truncated"] is True
        assert body["matched_count"] == 6      # 达阈值目录总数
        assert body["node_count"] == 3         # 实际进入树构建的节点
        assert body["node_limit"] == 3
        nodes = _flatten(body["children"][0])
        assert len(nodes) == len(set(n["path"] for n in nodes)) == 3
        # root 最大必在保留集；保留的是 size 最大的 3 行
        assert body["children"][0]["path"] == ROOT_A

    def test_budget_truncation_promotes_orphans_without_duplicates(
            self, client, monkeypatch):
        """中间层被截断时，其子孙挂到顶层：不丢失、不重复。"""
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1, entries={
                ROOT_A: 10_000,
                f"{ROOT_A}/big1": 9_000,
                f"{ROOT_A}/big2": 8_000,
                f"{ROOT_A}/mid": 5_000,          # 预算 4 时被截断
                f"{ROOT_A}/mid/leaf": 6_000,     # 比父目录大（合成数据）
                f"{ROOT_A}/tiny": 1_000,
            })
        finally:
            conn.close()
        monkeypatch.setattr(api, "TREE_MAX_NODES", 4)
        body = client.get("/api/trees?min_kb=1").json()
        assert body["matched_count"] == 6
        assert body["node_count"] == 4
        nodes = _flatten(body["children"][0])
        paths = [n["path"] for n in nodes]
        # 保留集 = size 前 4：root/big1/big2/mid-leaf；mid 被截，leaf 提升到顶层
        assert set(paths) == {ROOT_A, f"{ROOT_A}/big1", f"{ROOT_A}/big2",
                              f"{ROOT_A}/mid/leaf"}
        assert len(paths) == len(set(paths))
        assert f"{ROOT_A}/mid" not in paths  # 被截断的中间层不出现
        # leaf 直接挂在 top 下（无孤儿、无重复归属）
        top_children = [c["path"] for c in body["children"][0]["children"]]
        assert f"{ROOT_A}/mid/leaf" in top_children

    def test_real_budget_stress_20k(self, client):
        """大树压力样例（真实 TREE_MAX_NODES=20000，不 monkeypatch）。

        20005 个达阈值目录：SQL 层截到 20000，响应节点数有明确上限，
        每个路径恰好出现一次（无孤儿重复），截断数量可解释。
        """
        self._seed_flat(20004)  # + root = 20005 行
        r = client.get("/api/trees?min_kb=1")
        assert r.status_code == 200
        body = r.json()
        assert body["truncated"] is True
        assert body["matched_count"] == 20005
        assert body["node_count"] == 20000
        assert body["node_limit"] == 20000
        nodes = _flatten(body["children"][0])
        assert len(nodes) == 20000
        paths = [n["path"] for n in nodes]
        assert len(set(paths)) == 20000  # 无重复归属
        # 保留集是 size 最大的 20000 行：root(200050000) + d20004..d19999...
        # 被截掉的是最小的 d1..d5
        for gone in (f"{ROOT_A}/d1", f"{ROOT_A}/d2", f"{ROOT_A}/d5"):
            assert gone not in set(paths)
        assert f"{ROOT_A}/d20004" in set(paths)
        # 旭日图仍可直接消费：top 的直接子节点按 value 降序
        kids = body["children"][0]["children"]
        values = [k["value"] for k in kids]
        assert values == sorted(values, reverse=True)


# ---------- browse：无基线语义（AUD-04）与 root=/ 边界 ----------


class TestBrowseEdges:
    def test_single_snapshot_children_not_full_growth(self, client):
        """AUD-04 钉住：单快照无基线，子目录 delta_kb=null、is_new=false，
        不得把当前大小冒充成"全量增长"。"""
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000, f"{ROOT_A}/x": 5_000,
                                      f"{ROOT_A}/y": 3_000})
        finally:
            conn.close()
        r = client.get("/api/browse")
        assert r.status_code == 200
        body = r.json()
        assert body["delta_kb"] is None
        for child in body["children"]:
            assert child["delta_kb"] is None
            assert child["is_new"] is False
            assert set(child) == {"name", "path", "size_kb", "delta_kb", "is_new"}

    def test_browse_root_slash(self, client):
        """root=/ 边界：根大小取 entries["/"]（不再是 0/空 target）。"""
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_SLASH, min_kb=1024,
                             entries={ROOT_SLASH: 900_000, "/Users": 500_000,
                                      "/Users/home": 200_000})
        finally:
            conn.close()
        body = client.get("/api/browse").json()
        assert body["path"] == ROOT_SLASH
        assert body["size_kb"] == 900_000
        assert body["delta_kb"] is None  # 单快照
        assert [c["name"] for c in body["children"]] == ["Users"]
        assert body["crumbs"][0]["name"] == ROOT_SLASH
        # 显式 path=/ 与缺省等价
        assert client.get("/api/browse?path=/").json()["path"] == ROOT_SLASH
        # 下钻到 /Users：根内路径不被 "//" 前缀校验误拒
        sub = client.get("/api/browse?path=/Users").json()
        assert sub["size_kb"] == 500_000
        assert [c["path"] for c in sub["children"]] == ["/Users/home"]
        assert [c["name"] for c in sub["crumbs"]] == [ROOT_SLASH, "Users"]

    def test_browse_trend_same_dataset_only(self, client):
        """目录视图的侧栏趋势同数据集：换口径后的 browse 不读旧口径历史。"""
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000})
            _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 12_000})
            _insert_snapshot(conn, "2026-09-03", ROOT_A, min_kb=4096,
                             entries={ROOT_A: 8_000})
        finally:
            conn.close()
        body = client.get("/api/browse").json()
        assert _dates(body["trend"]) == ["2026-09-03"]
        assert body["delta_kb"] is None  # 新口径首扫无同数据集基线

    def test_browse_rejects_path_outside_root(self, client):
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000})
        finally:
            conn.close()
        r = client.get("/api/browse?path=/synthetic/elsewhere")
        assert r.status_code == 400

    def test_browse_no_snapshots_409(self, client):
        r = client.get("/api/browse")
        assert r.status_code == 409


# ---------- 错误码矩阵：400（坏参数）/404（无快照）/200 空结果 可区分 ----------


class TestErrorSemantics:
    def test_bad_param_is_400_not_404_or_409(self, client):
        """全 API 参数校验失败统一 400（含 FastAPI 原 422 场景）。"""
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000})
        finally:
            conn.close()
        cases = [
            "/api/trees?min_kb=0",
            "/api/trees?snapshot_id=abc",
            "/api/volume-trend?limit=0",
            "/api/trend?path=x&limit=0",
            "/api/bigfiles?days=0",
            "/api/scan/status?history=999",
        ]
        for url in cases:
            r = client.get(url)
            assert r.status_code == 400, url
            assert "参数" in r.json()["detail"], url

    def test_missing_snapshot_404_vs_no_snapshot_200(self, client):
        """404 只表达"指定快照不存在"；空库是 200 + snapshot_id=null。"""
        assert client.get("/api/trees?snapshot_id=1").status_code == 404
        empty = client.get("/api/trees").json()
        assert empty["snapshot_id"] is None
