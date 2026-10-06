"""ISS-176：新 plan 身份数据在四个基本排查消费者上的贯通回归。

上游终审反例（真实 HTTP 实测）：「不只是 /api/snapshots 缺少 plan_id。
两个**同 plan**快照仍被 diff、children、trend 拒绝；生效范围下的大文件
查询也被拒（409）。summary 可以成功，但之后的针对性排查链无法继续。
ISS-160 的整盘数据集夹具没写 plan 身份，其通过不能证明生产链已接通。」

本文件按**真实 plan 身份数据**（v8 schema 真列 `plan_id`）把四个消费者
钉成回归，三组身份各跑一遍：

1. **同 plan 两快照** → diff 可比、children 可下钻、trend 可出点、
   bigfiles 可查（`/api/bigfiles` 独立于快照身份，走范围身份键）；
2. **跨 plan**（同根同阈值同排除，仅 plan_id 不同）→ 仍 409 拒绝，
   绝不按三元组混读；
3. **legacy↔plan 混搭** → 仍 409 拒绝（旧行不补造真实卷 UUID，
   身份不可证同源）。

既有 legacy 行为零回归：三方 legacy 快照组三个读端点全 200，
同根异阈值仍按 ISS-021/ISS-066 既有 400 语义拒绝（闸门断言不许消失）。

隔离合同沿用 test_diff_children.py：FATHOM_RUNTIME_DIR / FATHOM_SCAN_ROOT
指向 tmp_path 合成根，绝不触碰真实 HOME 或生产库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db


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
    # 进程级持久化设置必须逐测试归零，且保存不得落到真实 settings.json：
    # 本文件用 PUT /api/storage/scope 真实保存范围选择，若沿用进程全局
    # _USER_SETTINGS 与真实 settings 路径，保存结果会**跨文件泄漏**给随后运行
    # 的用例（实测污染 test_bigfiles_scoped 的 scope.root 与路径守卫）。
    monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings())
    monkeypatch.setattr(config, "settings_path",
                        lambda: runtime_dir / "settings.json")


@pytest.fixture
def client():
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


def _insert(conn: sqlite3.Connection, *, root: str, plan_id: str | None,
            created_at: str, min_kb: int | None = 1024,
            exclude_names: str = "", size_kb: int = 1024) -> int:
    """插入带**真实 plan 身份**的快照 + 一条 entries（不经 du，不走扫描）。"""
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, exclude_names, plan_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (created_at, root, 1, 0, 0.0, size_kb, min_kb, exclude_names, plan_id),
    )
    sid = cur.lastrowid
    conn.execute(
        "INSERT INTO entries (snapshot_id, path, size_kb) VALUES (?, ?, ?)",
        (sid, f"{root}/dir", size_kb),
    )
    conn.execute(
        "INSERT INTO entries (snapshot_id, path, size_kb) VALUES (?, ?, ?)",
        (sid, f"{root}/dir/child", size_kb // 2),
    )
    # 必须提交：API 端点每次 _get_conn() 开**新连接**，未提交的写对它不可见。
    conn.commit()
    return sid


@pytest.fixture
def identity_pairs(tmp_path):
    """三组真实 plan 身份数据：(组名, root, a_sid, b_sid) 列表。

    - same_plan：同一 plan_id 的两次采集（同根同阈值同排除）
    - cross_plan：同根同阈值同排除，plan_id 不同（换卷/换计划）
    - mixed：legacy 行与新身份行
    - legacy：三方 legacy（既有行为反例对照）
    """
    root = str(tmp_path / "scanroot")
    conn = db.connect()
    try:
        same_a = _insert(conn, root=root, plan_id="plan-1",
                         created_at="2026-10-01T10:00:00+00:00", size_kb=1024)
        same_b = _insert(conn, root=root, plan_id="plan-1",
                         created_at="2026-10-02T10:00:00+00:00", size_kb=2048)
        cross_a = _insert(conn, root=root, plan_id="plan-1",
                          created_at="2026-10-03T10:00:00+00:00")
        cross_b = _insert(conn, root=root, plan_id="plan-2",
                          created_at="2026-10-04T10:00:00+00:00")
        mixed_a = _insert(conn, root=root, plan_id=None,
                          created_at="2026-10-05T10:00:00+00:00")
        mixed_b = _insert(conn, root=root, plan_id="plan-1",
                          created_at="2026-10-06T10:00:00+00:00")
        legacy_a = _insert(conn, root=root, plan_id=None,
                           created_at="2026-10-07T10:00:00+00:00")
        legacy_b = _insert(conn, root=root, plan_id=None,
                           created_at="2026-10-08T10:00:00+00:00")
        other_min = _insert(conn, root=root, plan_id=None,
                            created_at="2026-10-09T10:00:00+00:00", min_kb=2048)
    finally:
        conn.close()
    return {
        "root": root,
        "same_plan": (same_a, same_b),
        "cross_plan": (cross_a, cross_b),
        "mixed": (mixed_a, mixed_b),
        "legacy": (legacy_a, legacy_b),
        "other_min_kb": other_min,
    }


class TestSamePlanConsumers:
    """组 1：同 plan 两快照 —— 四个消费者按完整身份工作（本卡核心反例）。"""

    def test_diff_comparable(self, client, identity_pairs):
        a, b = identity_pairs["same_plan"]
        resp = client.get("/api/diff", params={"a": a, "b": b})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # 真实差分数据出得来：b 侧比 a 侧大 1024 KB
        assert payload["a"]["plan_id"] == "plan-1"
        assert payload["b"]["plan_id"] == "plan-1"
        grown = {c["path"]: c for c in payload["grown"]}
        assert f"{identity_pairs['root']}/dir" in grown
        assert grown[f"{identity_pairs['root']}/dir"]["delta_kb"] == 1024

    def test_children_drilldown(self, client, identity_pairs):
        a, b = identity_pairs["same_plan"]
        resp = client.get("/api/diff/children", params={"a": a, "b": b})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["dataset"]["plan_id"] == "plan-1"
        names = {row["name"]: row for row in payload["children"]}
        assert "dir" in names
        assert names["dir"]["status"] == "measured"
        assert names["dir"]["delta_kb"] == 1024

    def test_trend_points(self, client, identity_pairs):
        a, b = identity_pairs["same_plan"]
        resp = client.get("/api/trend", params={
            "path": f"{identity_pairs['root']}/dir", "anchor_snapshot_id": a})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["dataset"]["plan_id"] == "plan-1"
        # 窗口按 plan 档取：plan-1 共 4 个快照（同 plan 的 2 个 + cross_a +
        # mixed_b），异 plan 与 legacy 都不入窗
        assert [p["snapshot_id"] for p in payload["points"]] == [1, 2, 3, 6]
        assert payload["points"][0]["size_kb"] == 1024
        assert payload["points"][1]["size_kb"] == 2048
        assert payload["total_snapshots"] == 4

    def test_trend_window_excludes_other_plans(self, client, identity_pairs):
        """plan 档窗口不得混入异 plan（plan-2）/ legacy 快照的同路径记录点。"""
        a, _b = identity_pairs["same_plan"]
        _cross_a, cross_b = identity_pairs["cross_plan"]
        resp = client.get("/api/trend", params={
            "path": f"{identity_pairs['root']}/dir", "anchor_snapshot_id": a})
        assert resp.status_code == 200, resp.text
        ids = [p["snapshot_id"] for p in resp.json()["points"]]
        assert cross_b not in ids  # 异 plan 不入 plan-1 窗口

    def test_bigfiles_query_allowed_under_scope(self, client, tmp_path):
        """组 1 的 bigfiles 侧：生效范围下可查（此前一律 409）。

        ISS-177 升级：只断言状态码会漏掉「查的其实是旧根」——必须核对
        ``resolved_root`` 落在已选范围内，且返回的文件确实属于该根
        （夹具在旧根 scanroot 与新根 vol-a 各放一个同名可区分文件）。
        """
        vol = tmp_path / "vol-a"
        (vol / "inside").mkdir(parents=True)
        (vol / "inside" / "vol_a_only.bin").write_bytes(b"a" * (3 * 1024 * 1024))
        # 旧根（fixture 的 FATHOM_SCAN_ROOT）里放一个不该被查到的大文件。
        old_root = Path(config.DEFAULT_ROOT)
        (old_root / "old_root_only.bin").write_bytes(b"b" * (9 * 1024 * 1024))
        save = client.put("/api/storage/scope", json={
            "mode": "custom_directory", "roots": [str(vol)],
            "expected_revision": config.effective_scope_selection().revision
            if config.effective_scope_selection() else 0,
        }, headers={"X-Fathom-Token": client.headers["X-Fathom-Token"]})
        assert save.status_code == 200, save.text

        # ① 显式 path 指向已选新根：可查，且 resolved_root 就是该根。
        explicit = client.get("/api/bigfiles", params={
            "mode": "largest", "path": str(vol), "min_mb": 1, "topn": 10})
        assert explicit.status_code == 200, explicit.text
        body = explicit.json()
        assert body["scope"]["resolved_root"] == str(vol.resolve())
        names = {Path(f["path"]).name for f in body["files"]}
        assert names == {"vol_a_only.bin"}, f"查到了范围外的文件：{names}"

        # ② 无 path：resolved_root 落在已选范围内（不得静默继续查旧根）。
        resp = client.get("/api/bigfiles", params={
            "mode": "largest", "min_mb": 1, "topn": 10})
        assert resp.status_code in (200, 202), resp.text
        body = resp.json()
        assert body["scope"]["resolved_root"] == str(vol.resolve())
        assert str(vol.resolve()) in body["scope"]["scope_roots"]
        assert body["scope"]["resolved_root"] != str(old_root.resolve())


class TestCrossPlanStillRejected:
    """组 2：跨 plan（同根同阈值同排除）仍 409 —— 不按三元组混读。"""

    def test_diff_rejected(self, client, identity_pairs):
        a, b = identity_pairs["cross_plan"]
        resp = client.get("/api/diff", params={"a": a, "b": b})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "plan_identity_unsupported"

    def test_children_rejected(self, client, identity_pairs):
        a, b = identity_pairs["cross_plan"]
        resp = client.get("/api/diff/children", params={"a": a, "b": b})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "plan_identity_unsupported"


class TestLegacyPlanMixedStillRejected:
    """组 3：legacy↔plan 混搭仍 409 —— 旧行不补造卷 UUID，身份不可证同源。"""

    def test_diff_rejected(self, client, identity_pairs):
        a, b = identity_pairs["mixed"]
        resp = client.get("/api/diff", params={"a": a, "b": b})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "plan_identity_unsupported"

    def test_children_rejected(self, client, identity_pairs):
        a, b = identity_pairs["mixed"]
        resp = client.get("/api/diff/children", params={"a": a, "b": b})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "plan_identity_unsupported"

    def test_trend_anchor_not_treated_as_legacy_window(self, client,
                                                        identity_pairs):
        """锚在 plan 行上：窗口按 plan 档取，不吞 legacy 同根历史。"""
        _a, b = identity_pairs["mixed"]
        resp = client.get("/api/trend", params={
            "path": f"{identity_pairs['root']}/dir", "anchor_snapshot_id": b})
        assert resp.status_code == 200, resp.text
        # 该数据集内 plan-1 共有 4 个快照（同 plan 的三次 + mixed 的 b）
        assert resp.json()["total_snapshots"] == 4


class TestLegacyZeroRegression:
    """组 4：legacy 既有行为零回归。"""

    def test_diff_and_children_and_trend_ok(self, client, identity_pairs):
        a, b = identity_pairs["legacy"]
        assert client.get("/api/diff", params={"a": a, "b": b}).status_code == 200
        assert client.get("/api/diff/children",
                          params={"a": a, "b": b}).status_code == 200
        trend = client.get("/api/trend", params={
            "path": f"{identity_pairs['root']}/dir", "anchor_snapshot_id": a})
        assert trend.status_code == 200
        assert trend.json()["dataset"]["plan_id"] is None

    def test_legacy_threshold_mismatch_still_400(self, client, identity_pairs):
        """同根异阈值仍按 ISS-021/ISS-066 既有 400 语义拒绝（闸门不消失）。"""
        a, _b = identity_pairs["legacy"]
        other = identity_pairs["other_min_kb"]
        resp = client.get("/api/diff", params={"a": a, "b": other})
        assert resp.status_code == 400
        assert "同一数据集" in resp.json()["detail"]

    def test_legacy_window_excludes_plan_rows(self, client, identity_pairs):
        """legacy 窗口不得混入新身份行，也不得混入异阈值 legacy 行。

        legacy 档按 (root, min_kb, exclude_names) 取且只取 plan_id IS NULL：
        本夹具同根同 min_kb 的 legacy 行共 3 个（mixed_a + legacy 两行），
        other_min_kb（min_kb=2048）属另一数据集，同样不入窗。
        """
        a, _b = identity_pairs["legacy"]
        mixed_a, _ = identity_pairs["mixed"]
        resp = client.get("/api/trend", params={
            "path": f"{identity_pairs['root']}/dir", "anchor_snapshot_id": a})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["total_snapshots"] == 3
        ids = [p["snapshot_id"] for p in payload["points"]]
        assert mixed_a in ids  # 同一 legacy 数据集内的行在窗
        assert identity_pairs["other_min_kb"] not in ids  # 异阈值不在窗


class TestSnapshotsCarriesPlanId:
    """/api/snapshots 下发 plan_id（前端两档收敛的数据来源）。"""

    def test_plan_and_legacy_rows(self, client, identity_pairs):
        same_a, _ = identity_pairs["same_plan"]
        mixed_a, _ = identity_pairs["mixed"]
        rows = {r["id"]: r for r in client.get("/api/snapshots").json()}
        assert rows[same_a]["plan_id"] == "plan-1"
        assert rows[mixed_a]["plan_id"] is None
        # 既有字段零变化
        assert {"id", "created_at", "root", "total_kb", "min_kb",
                "exclude_names", "vanished_count"} <= set(rows[same_a])
