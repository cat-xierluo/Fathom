"""ISS-178（D1）：范围采集 UI 接线——设置页触发必须走多范围路径。

D1 实机现象：打包态桌面 app 点「开始首次采集」→ 快照 ``root`` 是隔离 HOME、
``plan_id=NULL``，``scan_plans``/``scan_rounds``/``scan_round_members`` 全空；
同一环境 CLI ``scan --scope A --scope-id a --scope B --scope-id b`` 则完全贯通。

根因：``POST /api/scan`` 恒调 ``start_scan(source="api")`` 且不传 ``scopes``，
于是 UI 走单根旧路径，与 CLI 的多范围执行体分叉。

本文件锁死三件事：
1. **UI 触发落库带 plan_id**，轮次/成员三表非空（先红后绿的反例）。
2. **未启用范围能力时逐字节不变**：仍单根、无轮次（legacy 口径不回退）。
3. **CLI 与 UI 共用同一段范围构造逻辑**（公共化不产生第二套判定）。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from fathom import api, cli, config, db, scan_coordinator


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """全部可写路径指向临时目录，扫描根为合成目录（沿用 test_scope_config 风格）。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    scanroot = tmp_path / "scanroot"
    scanroot.mkdir()
    cfg = config.RuntimeConfig.from_env(
        {"FATHOM_RUNTIME_DIR": str(runtime), "FATHOM_SCAN_ROOT": str(scanroot)},
        project_root=tmp_path, home=tmp_path / "home",
    )
    monkeypatch.setattr(config, "_ACTIVE", cfg)
    monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings())
    monkeypatch.setattr(config, "_CLI_SCAN_ROOT_PINNED", False)
    for name, value in (
        ("DATA_DIR", cfg.data_dir), ("REPORTS_DIR", cfg.reports_dir),
        ("LOGS_DIR", cfg.logs_dir), ("DB_PATH", cfg.db_path),
        ("FRONTEND_DIR", cfg.frontend_dir), ("DEFAULT_ROOT", cfg.scan_root),
        ("PORT", cfg.port), ("EXCLUDE_NAMES", []),
    ):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())


@pytest.fixture
def client():
    with TestClient(api.app,
                    base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


def _wait_idle(timeout: float = 30.0) -> None:
    """等后台扫描线程真实收尾（不靠 sleep 猜）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not api._scan_lock.locked():
            return
        time.sleep(0.02)
    raise AssertionError("扫描未在超时内完成")


def _select(client, roots, scope_ids=None):
    body = {"mode": "custom_directory", "roots": [str(r) for r in roots]}
    if scope_ids is not None:
        body["scope_ids"] = list(scope_ids)
    resp = client.put("/api/storage/scope", json=body)
    assert resp.status_code == 200, resp.text
    return resp


def _table_counts() -> dict:
    conn = db.connect()
    try:
        out = {}
        for table in ("snapshots", "scan_plans", "scan_rounds", "scan_round_members"):
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out
    finally:
        conn.close()


# ---------- 1. UI 触发走多范围路径（D1 核心反例） ----------

class TestApiScanUsesMultiScopePath:
    def test_api_scan_with_scope_selection_lands_plan_and_round(self, client, tmp_path):
        """D1：已启用范围能力时 POST /api/scan 走多范围执行体。

        先红后绿：修复前快照 plan_id 为 NULL、轮次/成员表全空。
        """
        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir()
        b.mkdir()
        _select(client, [a, b], scope_ids=["apfs-volume:a", "apfs-volume:b"])

        resp = client.post("/api/scan")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        # 响应如实带出本轮范围数，UI 可据此区分单根/多范围
        assert body["scopes"] == 2
        _wait_idle()

        conn = db.connect()
        try:
            snaps = conn.execute(
                "SELECT plan_id FROM snapshots WHERE plan_id IS NOT NULL"
            ).fetchall()
            rounds = conn.execute("SELECT * FROM scan_rounds").fetchall()
            members = conn.execute("SELECT * FROM scan_round_members").fetchall()
            plans = conn.execute("SELECT plan_id, scope_id FROM scan_plans").fetchall()
        finally:
            conn.close()

        # 快照带计划身份——D1 的失败点正是这里
        assert len(snaps) == 2, f"期望 2 个带 plan_id 的快照，实得 {len(snaps)}"
        assert len(rounds) == 1, f"期望 1 个轮次，实得 {len(rounds)}"
        assert len(members) == 2, f"期望 2 个成员，实得 {len(members)}"
        assert {p["scope_id"] for p in plans} == {"apfs-volume:a", "apfs-volume:b"}
        # 成员都真的挂上了快照，且轮次状态如实
        assert all(m["snapshot_id"] is not None for m in members)
        assert rounds[0]["status"] in {"full", "partial"}

    def test_api_scan_uses_derived_scope_id_when_ids_absent(self, client, tmp_path):
        """未给 scope_ids 时按规范根派生 path 型 ID，绝不伪造卷身份（ISS-153）。"""
        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir()
        b.mkdir()
        _select(client, [a, b])

        assert client.post("/api/scan").status_code == 200
        _wait_idle()

        conn = db.connect()
        try:
            ids = [r["scope_id"] for r in
                   conn.execute("SELECT scope_id FROM scan_scopes").fetchall()]
        finally:
            conn.close()
        assert all(i.startswith("path:") for i in ids), ids

    def test_scope_ids_mismatch_returns_400_not_silent_legacy(self, client, tmp_path):
        """范围 ID 与范围错位 → 400 如实报错，绝不静默回落到单根旧路径。"""
        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir()
        b.mkdir()
        # 绕过保存端校验，直接构造错位的生效选择
        selection = config.ScopeSelection(
            mode="custom_directory", roots=(str(a), str(b)),
            scope_ids=("apfs-volume:only-one",),
        )
        config._USER_SETTINGS = config.UserSettings(storage_scope=selection)

        resp = client.post("/api/scan")
        assert resp.status_code == 400, resp.text
        assert "一一对应" in resp.json()["message"]
        assert not api._scan_lock.locked()
        assert _table_counts()["scan_rounds"] == 0


# ---------- 2. 未启用范围能力：逐字节不变（legacy 不回退） ----------

class TestLegacySingleRootUnchanged:
    def test_no_scope_selection_keeps_single_root_legacy_path(self, client):
        """未启用范围能力 → 仍走单根旧口径：快照 plan_id 为 NULL、无轮次。"""
        resp = client.post("/api/scan")
        assert resp.status_code == 200, resp.text
        assert resp.json()["scopes"] == 0
        _wait_idle()

        conn = db.connect()
        try:
            snaps = conn.execute("SELECT plan_id, root FROM snapshots").fetchall()
            rounds = conn.execute("SELECT COUNT(*) c FROM scan_rounds").fetchone()["c"]
        finally:
            conn.close()
        assert len(snaps) == 1
        assert snaps[0]["plan_id"] is None, "未启用范围能力时不得凭空出现计划身份"
        assert rounds == 0, "未启用范围能力时不得凭空出现轮次"

    def test_legacy_snapshot_root_is_default_root(self, client):
        """legacy 单根采集的根仍是 FATHOM_SCAN_ROOT 解析值。"""
        client.post("/api/scan")
        _wait_idle()
        conn = db.connect()
        try:
            root = conn.execute("SELECT root FROM snapshots").fetchone()["root"]
        finally:
            conn.close()
        assert root == str(config.DEFAULT_ROOT)


# ---------- 3. CLI/UI 共用同一段构造逻辑（公共化不产生第二套判定） ----------

class TestSharedScopeConstruction:
    def test_cli_and_api_share_one_builder(self, tmp_path):
        """同一组 (paths, ids) 经 CLI 与 API 入口得到完全相同的范围规格。"""
        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir()
        b.mkdir()

        class _Args:
            scope = [str(a), str(b)]
            scope_id = ["apfs-volume:a", "apfs-volume:b"]

        from_cli = cli._scan_scopes(_Args())
        from_shared = cli.scope_specs_from_paths([str(a), str(b)],
                                                 ["apfs-volume:a", "apfs-volume:b"])
        assert [(s.root, s.scope_id) for s in from_cli] == \
               [(s.root, s.scope_id) for s in from_shared]

    def test_effective_selection_builder_matches_selection(self, tmp_path, monkeypatch):
        """生效选择构造出的规格与选择内容一致（顺序即采集顺序）。"""
        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir()
        b.mkdir()
        monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings(
            storage_scope=config.ScopeSelection(
                mode="custom_directory", roots=(str(a), str(b)),
                scope_ids=("apfs-volume:a", "apfs-volume:b"),
            )))
        specs = cli.scope_specs_from_effective_selection()
        assert [s.scope_id for s in specs] == ["apfs-volume:a", "apfs-volume:b"]
        assert [str(s.root) for s in specs] == [str(a), str(b)]

    def test_no_selection_yields_empty_scopes(self):
        """用户从未选择范围 → 空规格列表（调用方回落单根口径）。"""
        assert cli.scope_specs_from_effective_selection() == []

    def test_preserves_collection_order_of_selection(self, tmp_path, monkeypatch):
        """选择顺序即采集顺序，不被去重/构造打乱。"""
        a, b, c = tmp_path / "A", tmp_path / "B", tmp_path / "C"
        for d in (a, b, c):
            d.mkdir()
        monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings(
            storage_scope=config.ScopeSelection(
                mode="custom_directory", roots=(str(c), str(a), str(b)),
                scope_ids=("v:c", "v:a", "v:b"),
            )))
        specs = cli.scope_specs_from_effective_selection()
        assert [s.scope_id for s in specs] == ["v:c", "v:a", "v:b"]

    def test_id_count_mismatch_raises_scope_error(self, tmp_path):
        """ID 数量错位 → ScanScopeError（CLI 与 UI 同一判定）。"""
        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir()
        b.mkdir()
        with pytest.raises(scan_coordinator.ScanScopeError):
            cli.scope_specs_from_paths([str(a), str(b)], ["only-one"])


# ---------- 4. 端点合同未漂移（白名单/互斥/来源） ----------

class TestEndpointContractPreserved:
    def test_scan_status_endpoint_still_polls(self, client):
        """既有扫描状态轮询端点不受范围接线影响（ISS-161 兼容面）。"""
        assert client.get("/api/scan/status").status_code == 200

    def test_api_scan_source_stays_api(self, client):
        """来源仍登记为 api（定时/CLI 语义不受影响）。"""
        client.post("/api/scan")
        _wait_idle()
        conn = db.connect()
        try:
            src = conn.execute("SELECT source FROM scan_run_details").fetchone()["source"]
        finally:
            conn.close()
        assert src == "api"
