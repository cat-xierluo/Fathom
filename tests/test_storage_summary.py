"""ISS-157：容器容量与目录归因摘要 API 的语义红线测试。

每组用例对应任务卡「先复现/验收」的一格。核心是证明摘要**不**把不确定的
东西说成确定的：共享空间不翻倍、父子不可加、差额只在可比较时出现且带
限制、不可比时为 null、失败成员的旧值不当本轮贡献、不算伪覆盖率。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db, storage

CONTAINER = "apfs-container:1111-2222"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
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


def _select_container(monkeypatch, container_id=CONTAINER):
    """让生效范围选择带上容器身份（不写盘，走 monkeypatch）。"""
    selection = config.ScopeSelection(
        mode="custom_directory", roots=("/scanroot",), scope_ids=("s1",),
        container_id=container_id,
        identity_version=config.SCOPE_IDENTITY_VERSION)
    monkeypatch.setattr(config, "effective_scope_selection",
                        lambda: selection)


def _seed(conn, *, roots, total_kb, plan_ids, container_id=CONTAINER,
          free_bytes=0, source="storage-discovery", quality="full",
          round_prefix="2026-10-0", status="full"):
    """落两轮事实：轮次 + 成员 + 计划/范围 + 快照 + 容器容量样本。

    返回 [(round_id, {root: total_kb})]，按时间正序。
    """
    out = []
    for idx in range(2):
        rid = conn.execute(
            "INSERT INTO scan_rounds(started_at, finished_at, status) "
            "VALUES (?,?,?)",
            (f"{round_prefix}{idx+1}T01:00:00",
             f"{round_prefix}{idx+1}T01:10:00", status),
        ).lastrowid
        for seq, (root, plan_id) in enumerate(zip(roots, plan_ids)):
            # 范围与计划跨轮**稳定**（同主体同计划正是差额可比较的前提），
            # 故按根复用而非每轮新造。
            conn.execute(
                "INSERT OR IGNORE INTO scan_scopes(scope_id, kind, "
                "container_id, mount_path, display_name, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (f"scope-{seq}", "apfs_volume", container_id,
                 root, f"v{seq}", "2026-10-01T00:00:00"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO scan_plans(plan_id, scope_id, "
                "canonical_root, metric_version, min_kb, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (plan_id, f"scope-{seq}", root, 1, 512,
                 "2026-10-01T00:00:00"),
            )
            sid = conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, "
                "denied_count, du_seconds, total_kb, plan_id, "
                "collection_status) VALUES (?,?,1,0,0.1,?,?,?)",
                (f"{round_prefix}{idx+1}T01:0{seq}:00", root,
                 total_kb[idx][seq], plan_id, quality),
            ).lastrowid
            conn.execute(
                "INSERT INTO scan_round_members(round_id, seq, plan_id, "
                "scope_id, snapshot_id, snapshot_status, status, started_at, "
                "finished_at) VALUES (?,?,?,?,?,'active','done',?,?)",
                (rid, seq, plan_id, f"scope-{seq}", sid,
                 f"{round_prefix}{idx+1}T01:0{seq}:00",
                 f"{round_prefix}{idx+1}T01:0{seq}:30"),
            )
        conn.execute(
            "INSERT INTO container_capacity_samples(container_id, "
            "total_bytes, free_bytes, source, sampled_at, round_id) "
            "VALUES (?,?,?,?,?,?)",
            (container_id, 100 * 1024 ** 3, free_bytes[idx], source,
             f"{round_prefix}{idx+1}T01:09:00", rid),
        )
        out.append(rid)
    conn.commit()
    return out


class TestCrossRoundComparability:
    """B1 返修：可比性必须核对「同主体 + 同计划」，只查质量不够。

    独立审查活反例：两轮分别用 p1 与 p9（不同计划）却判 comparable=True
    并给出 51,404,800 bytes 差额。计划不同 → 目录测量口径不同 → 不可比。
    """

    def test_cross_plan_rounds_not_comparable(self, client, tmp_path,
                                              monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            rids = _seed(conn, roots=("/scanroot",),
                         total_kb=([0], [20 * 1024]),
                         plan_ids=("p1",), free_bytes=(100, 74))
            # 第二轮换计划（换 UUID 身份）：同根同阈值，但计划不同
            conn.execute("UPDATE scan_round_members SET plan_id='p9' "
                         "WHERE round_id=?", (rids[-1],))
            conn.execute(
                "INSERT INTO scan_scopes(scope_id, kind, container_id, "
                "mount_path, display_name, created_at) "
                "VALUES ('scope-p9','apfs_volume',?,'/scanroot','v0','2026')",
                (CONTAINER,))
            conn.execute(
                "INSERT INTO scan_plans(plan_id, scope_id, canonical_root, "
                "metric_version, min_kb, created_at) "
                "VALUES ('p9','scope-p9','/scanroot',1,512,'2026')")
            conn.execute("UPDATE snapshots SET plan_id='p9' WHERE id=("
                         "SELECT snapshot_id FROM scan_round_members "
                         "WHERE round_id=?)", (rids[-1],))
            conn.commit()
        finally:
            conn.close()
        data = client.get("/api/storage/summary").json()
        assert data["comparability"]["comparable"] is False
        assert data["comparability"]["identity_reasons"], "应给出跨计划/跨主体原因"
        assert data["unexplained"]["comparable"] is False
        assert data["unexplained"]["bytes"] is None
        assert "计划" in data["unexplained"]["reason"]

    def test_cross_root_set_not_comparable(self, client, tmp_path,
                                           monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            rids = _seed(conn, roots=("/scanroot",),
                         total_kb=([0], [20 * 1024]),
                         plan_ids=("p1",), free_bytes=(100, 74))
            # 第二轮换测量根集合：同计划 ID，但计划落在另一个根上
            conn.execute("UPDATE scan_round_members SET scope_id='scope-b' "
                         "WHERE round_id=?", (rids[-1],))
            conn.execute(
                "INSERT INTO scan_scopes(scope_id, kind, container_id, "
                "mount_path, display_name, created_at) "
                "VALUES ('scope-b','apfs_volume',?,'/other','v1','2026')",
                (CONTAINER,))
            conn.execute(
                "INSERT INTO scan_plans(plan_id, scope_id, canonical_root, "
                "metric_version, min_kb, created_at) "
                "VALUES ('p1b','scope-b','/other',1,512,'2026')")
            conn.execute("UPDATE scan_round_members SET plan_id='p1b' "
                         "WHERE round_id=?", (rids[-1],))
            conn.commit()
        finally:
            conn.close()
        data = client.get("/api/storage/summary").json()
        assert data["comparability"]["comparable"] is False
        assert data["comparability"]["identity_reasons"], "应给出跨计划/跨主体原因"
        assert data["unexplained"]["bytes"] is None
        assert data["unexplained"]["reason"]


class TestSharedFreeCountedOnce:
    """同容器共享 free 只计一次：两卷不得翻倍。"""

    def test_two_volumes_do_not_double_free(self, client, tmp_path,
                                            monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            rid = conn.execute(
                "INSERT INTO scan_rounds(started_at, finished_at, status) "
                "VALUES ('2026-10-01T01:00:00','2026-10-01T01:10:00','full')"
            ).lastrowid
            # 同一容器的两条卷级样本：共享同一份剩余空间
            for seq, subj in enumerate((
                    "apfs-volume:aaaa", "apfs-volume:bbbb")):
                conn.execute(
                    "INSERT INTO container_capacity_samples(container_id, "
                    "total_bytes, free_bytes, source, sampled_at, round_id) "
                    "VALUES (?,?,?,'storage-discovery',?,?)",
                    (CONTAINER, 100 * 1024 ** 3, 30 * 1024 ** 3,
                     "2026-10-01T01:09:00", rid),
                )
            conn.commit()
        finally:
            conn.close()
        data = client.get("/api/storage/summary").json()
        cap = data["capacity"]
        assert cap["free_bytes"] == 30 * 1024 ** 3
        # 同容器两条样本，只取一条，另一条被忽略（不翻倍）——具体数值断言
        assert cap["ignored_shared_samples"] == 1
        # B2：schema 无 subject_kind 列，摘要不得凭空声称主体类型
        assert "sample_kind" not in cap
        assert "共享" in "".join(cap["note"])

    def test_pure_helper_counts_shared_free_once(self):
        samples = [
            {"container_id": CONTAINER, "free_bytes": 10, "total_bytes": 100,
             "source": "storage-discovery", "sampled_at": "t1",
             "subject_kind": "container"},
            {"container_id": CONTAINER, "free_bytes": 10, "total_bytes": 100,
             "source": "storage-discovery", "sampled_at": "t1",
             "subject_kind": "volume"},
        ]
        view = storage.shared_free_once(samples, CONTAINER)
        assert view["free_bytes"] == 10          # 不是 20
        assert view["ignored_shared_samples"] == 1


class TestOverlappingRootsNotSummed:
    """父子测量根不可加：子根被吸收。"""

    def test_child_root_absorbed(self, client, tmp_path, monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            _seed(conn, roots=("/scanroot", "/scanroot/Downloads"),
                  total_kb=([1000, 5000], [1200, 5000]),
                  plan_ids=("p1", "p2"), free_bytes=(0, 0))
        finally:
            conn.close()
        data = client.get("/api/storage/summary").json()
        attribution = data["attribution"]
        assert attribution["attribution_roots"] == ["/scanroot"]
        assert attribution["absorbed_roots"] == ["/scanroot/Downloads"]

    def test_helper_prefix_boundary(self):
        kept, absorbed = storage.non_overlapping_roots(
            ["/Users/me", "/Users/me/Downloads", "/Users/melissa"])
        # /Users/melissa 不是 /Users/me 的子目录，不能被误吞
        assert kept == ("/Users/me", "/Users/melissa")
        assert absorbed == ("/Users/me/Downloads",)


class TestUnexplainedDifference:
    """差额：26 vs 20 → 未知 6，不是可清理量。"""

    def test_positive_unexplained_is_not_reclaimable(self, client, tmp_path,
                                                     monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            # 容器 free 少了 26MB；可比较目录只涨了 20MB
            _seed(conn, roots=("/scanroot",),
                  total_kb=([0], [20 * 1024]),
                  plan_ids=("p1", "p1"),
                  free_bytes=(100 * 1024 ** 2, 74 * 1024 ** 2))
        finally:
            conn.close()
        unexp = client.get("/api/storage/summary").json()["unexplained"]
        assert unexp["comparable"] is True
        assert unexp["bytes"] == 6 * 1024 ** 2
        assert unexp["limitation"] == "尚无法由目录变化解释"
        assert "不代表垃圾量" in unexp["sign_semantics"]

    def test_negative_difference_keeps_sign(self, client, tmp_path,
                                            monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            # free 增加 10MB（用户删了东西）→ 负差额，不得称可回收
            _seed(conn, roots=("/scanroot",),
                  total_kb=([5 * 1024], [5 * 1024]),
                  plan_ids=("p1", "p1"),
                  free_bytes=(80 * 1024 ** 2, 90 * 1024 ** 2))
        finally:
            conn.close()
        unexp = client.get("/api/storage/summary").json()["unexplained"]
        assert unexp["bytes"] == -10 * 1024 ** 2

    def test_incomparable_returns_null_with_reason(self):
        view = storage.difference_view(
            comparable=False, free_before=1, free_after=2,
            measured_before=1, measured_after=2)
        assert view["bytes"] is None
        assert view["comparable"] is False
        assert "不可比" in view["reason"]

    def test_missing_readings_not_zero_filled(self):
        view = storage.difference_view(
            comparable=True, free_before=100, free_after=None,
            measured_before=0, measured_after=10)
        assert view["bytes"] is None
        assert "缺失" in view["reason"]


class TestFailedMembersAndStale:
    """失败成员旧有效值标 stale，不当本轮贡献。"""

    def test_failed_member_marked_stale(self, client, tmp_path, monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            rids = _seed(conn, roots=("/scanroot",), total_kb=([10], [20]),
                         plan_ids=("p1",), free_bytes=(90, 80))
            latest = rids[-1]
            # 本轮追加一个失败成员：无快照引用
            conn.execute(
                "INSERT INTO scan_round_members(round_id, seq, plan_id, "
                "scope_id, snapshot_id, snapshot_status, status) "
                "VALUES (?,1,'p1','scope-x',NULL,NULL,'failed')", (latest,))
            conn.commit()
        finally:
            conn.close()
        data = client.get("/api/storage/summary").json()
        attribution = data["attribution"]
        assert len(attribution["stale_members"]) == 1
        assert attribution["stale_members"][0]["stale"] is True
        assert attribution["comparable_to_previous"] is False
        assert data["unexplained"]["bytes"] is None

    def test_expired_snapshot_visible(self, client, tmp_path, monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            rids = _seed(conn, roots=("/scanroot",), total_kb=([10], [20]),
                         plan_ids=("p1",), free_bytes=(90, 80))
            conn.execute(
                "UPDATE scan_round_members SET snapshot_status='expired' "
                "WHERE round_id=?", (rids[-1],))
            conn.commit()
        finally:
            conn.close()
        members = client.get("/api/storage/summary").json()["attribution"][
            "members"]
        assert members[0]["snapshot_status"] == "expired"


class TestHistoryAndScopeBinding:
    """真实 HTTP：历史区间、scope 绑定、limit 截断、旧 volume-trend 兼容。"""

    def test_scope_binding_returned(self, client, tmp_path, monkeypatch):
        _select_container(monkeypatch)
        data = client.get("/api/storage/summary").json()
        assert data["scope"]["container_id"] == CONTAINER
        assert data["capacity"]["container_id"] == CONTAINER
        assert data["capacity"]["unit"] == "bytes"

    def test_limit_truncates_newest_end(self, client, tmp_path, monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO scan_rounds(started_at, finished_at, status) "
                "VALUES ('2026-10-01T00:00:00',NULL,'full')")
            for day in range(1, 6):
                conn.execute(
                    "INSERT INTO container_capacity_samples(container_id, "
                    "total_bytes, free_bytes, source, sampled_at) "
                    "VALUES (?,?,?,'storage-discovery',?)",
                    (CONTAINER, 100, 100 - day,
                     f"2026-10-0{day}T00:00:00"))
            conn.commit()
        finally:
            conn.close()
        data = client.get("/api/storage/summary?limit=2").json()
        assert data["capacity"]["samples"] == 2
        assert data["capacity"]["sampled_at"] == "2026-10-05T00:00:00"

    def test_legacy_volume_trend_still_array(self, client):
        """ISS-155 窗口限定 legacy 的行为不得被摘要改动。"""
        resp = client.get("/api/volume-trend")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_legacy_statvfs_only_sample_labeled(self, client, tmp_path,
                                                monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            _seed(conn, roots=("/scanroot",), total_kb=([1], [2]),
                  plan_ids=("p1",), free_bytes=(50, 40),
                  source="statvfs")
        finally:
            conn.close()
        cap = client.get("/api/storage/summary").json()["capacity"]
        assert cap["source"] == "statvfs"
        assert cap["free_bytes"] == 40


class TestNoFakeCoverage:
    """阈值/权限不计算伪覆盖率。"""

    def test_no_coverage_field(self, client, tmp_path, monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            _seed(conn, roots=("/scanroot",), total_kb=([10], [20]),
                  plan_ids=("p1",), free_bytes=(90, 80))
        finally:
            conn.close()
        raw = client.get("/api/storage/summary").text
        assert "coverage" not in raw
        assert "覆盖率" not in raw

    def test_no_implicit_scan_on_request(self, client, tmp_path, monkeypatch):
        """请求摘要不得触发任何采集入口。"""
        def _boom(*_a, **_k):
            raise AssertionError("摘要端点不得触发扫描/发现")

        monkeypatch.setattr(storage, "discover_startup", _boom)
        from fathom import scan_coordinator
        monkeypatch.setattr(scan_coordinator, "read_capacity_readings", _boom)
        _select_container(monkeypatch)
        assert client.get("/api/storage/summary").status_code == 200

    def test_empty_database_is_not_500(self, client):
        data = client.get("/api/storage/summary").json()
        assert data["round"] is None
        assert data["unexplained"]["bytes"] is None
        assert data["unexplained"]["comparable"] is False


class TestRoundTimeSpan:
    """整轮跨时间必须显式披露，不压成单一时点。"""

    def test_round_not_atomic(self, client, tmp_path, monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            _seed(conn, roots=("/scanroot", "/other"), total_kb=([1, 2],
                                                                [3, 4]),
                  plan_ids=("p1", "p2"), free_bytes=(90, 80))
        finally:
            conn.close()
        rnd = client.get("/api/storage/summary").json()["round"]
        assert rnd["atomic"] is False
        assert "不是单一原子时点" in rnd["time_note"]
        assert rnd["started_at"] != rnd["finished_at"]


class TestPreviousRoundQualityGate:
    """R1 返修：可比性必须**两侧**都过质量门。

    独立 HTTP 反例：同主体同计划、**前轮 partial、本轮 full**，旧实现只查
    本轮质量 → 仍判 comparable=true 并给出 −101400 bytes 差额。前轮测量
    不完整，前一轮基线就不是有效读数，差额没有意义。
    """

    def test_previous_partial_blocks_difference(self, client, tmp_path,
                                                monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            # 前轮 partial（100KB），本轮 full（+100KB）；身份完全一致
            rids = _seed(conn, roots=("/scanroot",),
                         total_kb=([100], [200]),
                         plan_ids=("p1", "p1"),
                         free_bytes=(100 * 1024 ** 2, 100 * 1024 ** 2))
            conn.execute("UPDATE snapshots SET collection_status='partial' "
                         "WHERE id=(SELECT snapshot_id FROM "
                         "scan_round_members WHERE round_id=?)", (rids[0],))
            conn.commit()
        finally:
            conn.close()
        data = client.get("/api/storage/summary").json()
        assert data["comparability"]["comparable"] is False, \
            "前轮 partial 时不得判可比"
        assert data["comparability"]["previous_reasons"], \
            "必须给出前轮不可比原因"
        assert "前轮" in data["comparability"]["previous_reasons"][0]
        assert data["unexplained"]["comparable"] is False
        assert data["unexplained"]["bytes"] is None
        assert "前轮" in data["unexplained"]["reason"]

    def test_previous_failed_member_blocks_difference(self, client,
                                                      tmp_path, monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            rids = _seed(conn, roots=("/scanroot",), total_kb=([10], [20]),
                         plan_ids=("p1",), free_bytes=(90, 80))
            # 前轮追加一个失败成员：无快照引用 → 该轮有 stale 成员
            conn.execute(
                "INSERT INTO scan_round_members(round_id, seq, plan_id, "
                "scope_id, snapshot_id, snapshot_status, status) "
                "VALUES (?,1,'p1','scope-x',NULL,NULL,'failed')", (rids[0],))
            conn.commit()
        finally:
            conn.close()
        data = client.get("/api/storage/summary").json()
        assert data["comparability"]["comparable"] is False
        assert any("前轮" in r for r in
                   data["comparability"]["previous_reasons"])
        assert data["unexplained"]["bytes"] is None


class TestCapacityBoundToSelectedContainer:
    """R2 返修：容量读数必须绑定所选容器，且与生效 scope 一起贯穿。

    反例①：前轮混入**其他容器**的较新样本 → 旧实现按 round_id 盲取，
    当前容器的差额被算成 888599 bytes 仍称可比。
    反例②：切换生效容器后所选容器容量为 null，旧实现却仍用**旧容器**
    的轮次容量输出数值差额。
    """

    def test_foreign_container_sample_not_used_as_difference(self, client,
                                                            tmp_path,
                                                            monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            _seed(conn, roots=("/scanroot",), total_kb=([0], [20 * 1024]),
                  plan_ids=("p1", "p1"),
                  free_bytes=(100 * 1024 ** 2, 74 * 1024 ** 2))
            # 前轮混入其他容器的较新样本：旧实现会按 round_id 盲取到它
            conn.execute(
                "INSERT INTO container_capacity_samples(container_id, "
                "total_bytes, free_bytes, source, sampled_at, round_id) "
                "VALUES ('apfs-container:other', 100, ?, "
                "'storage-discovery', '2026-10-02T01:09:30', "
                "(SELECT MIN(id) FROM scan_rounds))",
                (94 * 1024 ** 2 + 888599,))
            conn.commit()
        finally:
            conn.close()
        unexp = client.get("/api/storage/summary").json()["unexplained"]
        # 当前容器自身样本齐全：free 100MB→74MB，目录 +20MB → 未知 6MB
        assert unexp["bytes"] == 6 * 1024 ** 2, \
            "其他容器的样本不得进入当前容器的差额"
        assert unexp["bytes"] != 888599

    def test_capacity_window_bound_to_selected_container(self, client,
                                                         tmp_path,
                                                         monkeypatch):
        _select_container(monkeypatch)
        conn = db.connect()
        try:
            # 其他容器的样本时间更新；若不按容器过滤再截断，当前容器的
            # 最新样本会被挤出窗口
            for day in range(6, 10):
                conn.execute(
                    "INSERT INTO container_capacity_samples(container_id, "
                    "total_bytes, free_bytes, source, sampled_at) "
                    "VALUES ('apfs-container:other', 100, 1, "
                    "'storage-discovery', ?)",
                    (f"2026-10-0{day}T00:00:00",))
            conn.execute(
                "INSERT INTO container_capacity_samples(container_id, "
                "total_bytes, free_bytes, source, sampled_at) "
                "VALUES (?,100,42,'storage-discovery','2026-10-05T00:00:00')",
                (CONTAINER,))
            conn.commit()
        finally:
            conn.close()
        cap = client.get("/api/storage/summary?limit=2").json()["capacity"]
        assert cap["container_id"] == CONTAINER
        assert cap["free_bytes"] == 42, "容量窗口必须绑定所选容器"
        assert cap["sampled_at"] == "2026-10-05T00:00:00"

    def test_switched_container_blocks_numeric_difference(self, client,
                                                          tmp_path,
                                                          monkeypatch):
        # 生效容器已切到 other，但库里两轮都属于 CONTAINER
        _select_container(monkeypatch, container_id="apfs-container:other")
        conn = db.connect()
        try:
            _seed(conn, roots=("/scanroot",), total_kb=([0], [20 * 1024]),
                  plan_ids=("p1", "p1"),
                  free_bytes=(100 * 1024 ** 2, 74 * 1024 ** 2),
                  container_id=CONTAINER)
        finally:
            conn.close()
        data = client.get("/api/storage/summary").json()
        assert data["capacity"]["free_bytes"] is None, \
            "所选容器没有容量样本时不得补值"
        assert data["comparability"]["comparable"] is False, \
            "生效容器与轮次成员容器不一致时明确不可比"
        assert data["comparability"]["scope_reasons"], "应给出 scope 不匹配原因"
        assert data["unexplained"]["comparable"] is False
        assert data["unexplained"]["bytes"] is None
        assert data["unexplained"]["reason"]
