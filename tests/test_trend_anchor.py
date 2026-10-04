"""ISS-149 /api/trend 锚定与缺测窗口：显式 anchor_snapshot_id + null gap。

反例来源 ISS-149 任务卡（先复现红测）：
- 反例①：同一路径改排除项（exclude_names）后历史点混入同一条曲线——
  旧实现数据集谓词只有 (root, min_kb)，exclude_names 维度缺失（ISS-066
  已把数据集身份升级为三元组，trend 未跟上）。
- 反例②：变化页选择旧 a/b（旧数据集）后，详情趋势却锚到"最新记录该路径
  的快照"所属的新数据集——跨数据集混点，且 /api/diff/children 的区间
  读数与趋势曲线口径脱节。
- 反例③：同数据集中间快照无该路径条目（低于阈值/权限/移除均不可区分）
  时，旧形态只返回有记录的点，前端只能画连续实线跨过缺测——把"缺测"
  冒充成"实测相等"。

锚定合同（新）：anchor_snapshot_id 显式给定时按同数据集快照窗口逐点展开
（缺条目 size_kb=null、recorded=false，不补 0），每点带 snapshot_id 与完整
扫描时间，响应含数据集身份与截断说明；limit 截"最新 N 个快照"，最新端必在。
兼容合同（旧）：不带 anchor_snapshot_id 的调用维持既有响应形态
（{path, points:[{created_at, size_kb}]}，只含有记录的点），仅数据集谓词
统一走 reports 辅助补齐 exclude_names 维度。

全部合成数据（直连隔离库，不经 du），隔离边界同 tests/test_query_scope.py。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db, reports

ROOT_A = "/synthetic/root-a"


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    scan_root = tmp_path / "scanroot"
    scan_root.mkdir(parents=True)
    monkeypatch.delenv("FATHOM_DB", raising=False)
    monkeypatch.setenv("FATHOM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("FATHOM_SCAN_ROOT", str(scan_root))
    monkeypatch.setattr(config, "DATA_DIR", runtime_dir / "data")
    monkeypatch.setattr(config, "DB_PATH", runtime_dir / "data" / "fathom.db")
    monkeypatch.setattr(config, "REPORTS_DIR", runtime_dir / "reports")
    monkeypatch.setattr(config, "LOGS_DIR", runtime_dir / "logs")
    monkeypatch.setattr(config, "DEFAULT_ROOT", scan_root)


@pytest.fixture
def client():
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


def _insert_snapshot(
    conn: sqlite3.Connection,
    day: str,
    root: str,
    *,
    min_kb: int | None = None,
    exclude_names: str = "",
    entries: dict[str, int] | None = None,
    hour: int = 12,
) -> int:
    """直接造表行（不经 du）；exclude_names 默认空串（无掩码口径）。"""
    sizes = entries or {}
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status, exclude_names) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"{day}T{hour:02d}:00:00", root, len(sizes) + 1, 0, 0.0,
         max(sizes.values(), default=0), min_kb, "full", exclude_names),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
        [(sid, p, s) for p, s in sizes.items()],
    )
    conn.commit()
    return sid


def _days(points: list[dict]) -> list[str]:
    return [p["created_at"][:10] for p in points]


# ---------- 反例①：排除项变化混入曲线（旧调用 + 谓词统一走辅助） ----------


class TestExcludeNamesIsolation:
    def test_legacy_call_does_not_mix_excluded_dataset_points(self, client):
        """同路径同根同阈值但 exclude_names 不同：旧调用不得混点（反例①）。

        无掩码数据集（d1/d2）与 node_modules 掩码数据集（d3）里路径都有
        记录；旧行为谓词缺 exclude_names 会把 d3 混入曲线。
        """
        path = f"{ROOT_A}/x"
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000, path: 100})
            _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000, path: 200})
            _insert_snapshot(conn, "2026-09-03", ROOT_A, min_kb=1024,
                             exclude_names="node_modules",
                             entries={ROOT_A: 10_000, path: 900})
        finally:
            conn.close()
        body = client.get(f"/api/trend?path={path}").json()
        # 锚定最新记录点的数据集 = node_modules 掩码数据集：只有 d3
        assert _days(body["points"]) == ["2026-09-03"]
        assert body["points"][0]["size_kb"] == 900

    def test_anchored_call_historical_dataset_window_excludes_new_dataset(
            self, client):
        """锚定旧数据集快照：窗口只含该数据集快照，不混后续新数据集（反例②）。"""
        path = f"{ROOT_A}/x"
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000, path: 100})
            s2 = _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000, path: 200})
            _insert_snapshot(conn, "2026-09-03", ROOT_A, min_kb=1024,
                             exclude_names="node_modules",
                             entries={ROOT_A: 10_000, path: 900})
            _insert_snapshot(conn, "2026-09-04", ROOT_A, min_kb=1024,
                             exclude_names="node_modules",
                             entries={ROOT_A: 10_000, path: 950})
        finally:
            conn.close()
        body = client.get(
            f"/api/trend?path={path}&anchor_snapshot_id={s1}").json()
        assert [p["snapshot_id"] for p in body["points"]] == [s1, s2]
        assert _days(body["points"]) == ["2026-09-01", "2026-09-02"]
        assert [p["size_kb"] for p in body["points"]] == [100, 200]
        assert body["dataset"]["exclude_names"] == ""

    def test_reports_helper_rows_follow_identity(self, client):
        """reports 同数据集序列辅助：身份三元组过滤 + 时间正序 + limit 截最新端。"""
        path = f"{ROOT_A}/x"
        conn = db.connect()
        try:
            a = _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                                 entries={ROOT_A: 10_000, path: 100})
            b = _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                                 entries={ROOT_A: 10_000, path: 200})
            c = _insert_snapshot(conn, "2026-09-03", ROOT_A, min_kb=1024,
                                 entries={ROOT_A: 10_000, path: 300})
            _insert_snapshot(conn, "2026-09-04", ROOT_A, min_kb=4096,
                             entries={ROOT_A: 8_000, path: 999})
            _insert_snapshot(conn, "2026-09-05", f"{ROOT_A}/sub", min_kb=1024,
                             entries={f"{ROOT_A}/sub": 5_000})
            anchor = conn.execute("SELECT * FROM snapshots WHERE id=?", (a,)).fetchone()
            identity = reports.dataset_identity(anchor)
            rows = reports.find_same_dataset_snapshot_rows(conn, identity)
            assert [r["id"] for r in rows] == [a, b, c]  # 正序、只含同数据集
            rows2 = reports.find_same_dataset_snapshot_rows(conn, identity, limit=2)
            assert [r["id"] for r in rows2] == [b, c]  # limit 截最新端
        finally:
            conn.close()


# ---------- 锚定形态：窗口逐点展开、缺测 null、截断与身份说明 ----------


class TestAnchoredWindow:
    def _seed_gap_series(self):
        """三快照中间缺测：d2 无该路径条目（反例③的 API 侧形态）。"""
        path = f"{ROOT_A}/leaf"
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000, path: 100})
            s2 = _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000})  # 缺测
            s3 = _insert_snapshot(conn, "2026-09-03", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000, path: 300})
        finally:
            conn.close()
        return path, s1, s2, s3

    def test_anchor_window_expands_gaps_as_null(self, client):
        """锚定窗口逐快照一个点：中间缺测 size_kb=null、recorded=false。"""
        path, s1, s2, s3 = self._seed_gap_series()
        body = client.get(
            f"/api/trend?path={path}&anchor_snapshot_id={s2}").json()
        assert body["path"] == path
        assert body["anchor_snapshot_id"] == s2
        pts = body["points"]
        assert [p["snapshot_id"] for p in pts] == [s1, s2, s3]
        assert _days(pts) == ["2026-09-01", "2026-09-02", "2026-09-03"]
        assert [p["size_kb"] for p in pts] == [100, None, 300]
        assert [p["recorded"] for p in pts] == [True, False, True]
        # 每点带完整扫描时间（同日多次扫描可区分）
        assert pts[0]["created_at"] == "2026-09-01T12:00:00"

    def test_anchor_dataset_identity_and_truncation_fields(self, client):
        """响应含数据集身份（三元组）与窗口截断说明。"""
        path, s1, s2, s3 = self._seed_gap_series()
        body = client.get(
            f"/api/trend?path={path}&anchor_snapshot_id={s3}").json()
        assert body["dataset"] == {"root": ROOT_A, "min_kb": 1024,
                                   "exclude_names": ""}
        assert body["total_snapshots"] == 3
        assert body["truncated"] is False

    def test_anchor_missing_snapshot_404(self, client):
        """锚快照不存在 → 404（与 trees 的 404 语义一致）。"""
        path, *_ = self._seed_gap_series()
        r = client.get(f"/api/trend?path={path}&anchor_snapshot_id=999")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_anchor_limit_keeps_latest_window_and_marks_truncated(self, client):
        """limit 截"最新 N 个快照"：最早端被截、truncated=true、最新点必在。"""
        path = f"{ROOT_A}/x"
        conn = db.connect()
        try:
            for i in range(1, 6):
                _insert_snapshot(conn, f"2026-09-0{i}", ROOT_A, min_kb=1024,
                                 entries={ROOT_A: 10_000, path: 100 * i})
        finally:
            conn.close()
        body = client.get(f"/api/trend?path={path}&anchor_snapshot_id=5&limit=3").json()
        assert _days(body["points"]) == ["2026-09-03", "2026-09-04", "2026-09-05"]
        assert [p["size_kb"] for p in body["points"]] == [300, 400, 500]
        assert body["total_snapshots"] == 5
        assert body["truncated"] is True

    def test_anchor_path_never_recorded_all_null_not_error(self, client):
        """路径在该数据集从未记录：200 + 全 null 点（窗口在、点缺），不是 404。"""
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000})
            s2 = _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000})
        finally:
            conn.close()
        body = client.get(
            f"/api/trend?path={ROOT_A}/never&anchor_snapshot_id={s1}").json()
        assert [p["size_kb"] for p in body["points"]] == [None, None]
        assert all(not p["recorded"] for p in body["points"])

    def test_anchor_same_day_snapshots_distinguishable(self, client):
        """同日两次扫描（同 created_at 不同 id）：两点都在、按 id 区分。"""
        path = f"{ROOT_A}/x"
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                                  hour=10, entries={ROOT_A: 10_000, path: 100})
            s2 = _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                                  hour=18, entries={ROOT_A: 10_000, path: 200})
        finally:
            conn.close()
        body = client.get(
            f"/api/trend?path={path}&anchor_snapshot_id={s2}").json()
        assert [p["snapshot_id"] for p in body["points"]] == [s1, s2]
        assert [p["created_at"] for p in body["points"]] == [
            "2026-09-02T10:00:00", "2026-09-02T18:00:00"]
        assert [p["size_kb"] for p in body["points"]] == [100, 200]

    def test_anchor_min_kb_isolation(self, client):
        """锚定旧阈值数据集：新阈值同路径点不混入（身份含 min_kb）。"""
        path = f"{ROOT_A}/x"
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000, path: 100})
            s2 = _insert_snapshot(conn, "2026-09-02", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000, path: 200})
            _insert_snapshot(conn, "2026-09-03", ROOT_A, min_kb=4096,
                             entries={ROOT_A: 8_000, path: 900})
        finally:
            conn.close()
        body = client.get(
            f"/api/trend?path={path}&anchor_snapshot_id={s1}").json()
        assert [p["snapshot_id"] for p in body["points"]] == [s1, s2]
        assert body["dataset"]["min_kb"] == 1024

    def test_anchor_bad_limit_rejected_400(self, client):
        conn = db.connect()
        try:
            s1 = _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                                  entries={ROOT_A: 10_000})
        finally:
            conn.close()
        for bad in ("&limit=1", "&limit=0", "&limit=abc"):
            r = client.get(f"/api/trend?path={ROOT_A}/x&anchor_snapshot_id={s1}{bad}")
            assert r.status_code == 400, bad


# ---------- 兼容合同：不带 anchor 的旧调用形态钉住 ----------


class TestLegacyCompat:
    def test_legacy_shape_unchanged(self, client):
        """旧调用响应仍为 {path, points:[{created_at, size_kb}]}，只含有记录点。"""
        path, s1, s2, s3 = TestAnchoredWindow()._seed_gap_series()
        body = client.get(f"/api/trend?path={path}").json()
        assert set(body) == {"path", "points"}
        assert _days(body["points"]) == ["2026-09-01", "2026-09-03"]
        assert set(body["points"][0]) == {"created_at", "size_kb"}
        assert body["points"][0]["size_kb"] == 100

    def test_legacy_unknown_path_empty_points_not_error(self, client):
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-01", ROOT_A, min_kb=1024,
                             entries={ROOT_A: 10_000})
        finally:
            conn.close()
        r = client.get(f"/api/trend?path={ROOT_A}/never-recorded")
        assert r.status_code == 200
        assert r.json() == {"path": f"{ROOT_A}/never-recorded", "points": []}

    def test_legacy_no_cross_threshold_mixing_kept(self, client):
        """既有 ISS-024 隔离行为保持（min_kb 维度不回退）。"""
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
        assert _days(body["points"]) == ["2026-09-03"]
