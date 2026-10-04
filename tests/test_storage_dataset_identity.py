"""ISS-153：卷/范围身份与扫描轮次兼容数据模型——身份隔离定向测试。

任务卡反例（实现前先红测）：
1. 同路径换卷 / 换 plan 不可比：同一 root 路径在不同范围（卷）或不同
   规范根计划（阈值/排除/计量版本变化）下的历史互不作为基线；
2. 同容器不同卷可分组：同一 APFS 容器的两个卷各自成组，不互相混入；
3. legacy 根保持原口径：旧行（plan_id NULL）不补造真实卷 UUID，不与新
   整盘身份混比，旧 API 读法（同数据集前驱/窗口/差分）不变；
4. 同日替换/保留/淘汰按新身份隔离：不同 plan 同日不互相替换；保留策略
   周分组按 plan 独立；关联快照淘汰后轮次成员引用明确 expired，已保存
   AI 证据不被级联删除。

全部合成数据（tmp_path 下的 /synthetic 根），经 conftest.isolated 隔离
运行根，不触发真实 HOME 扫描或写生产库。
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from fathom import config, db, reports, scanner


@pytest.fixture
def conn(isolated):
    """v8 全新库连接（结构就位、无业务行）。"""
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


def _insert_snapshot(
    conn: sqlite3.Connection,
    created_at: str,
    *,
    root: str = "/synthetic/root",
    min_kb: int | None = 1024,
    exclude_names: str = "",
    plan_id: str | None = None,
    round_id: int | None = None,
    metric_version: int | None = None,
    entry: tuple[str, int] | None = ("/synthetic/root", 4096),
) -> int:
    """直接构造快照行（绕过 du 采集），返回 id。"""
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
        "du_seconds, total_kb, min_kb, collection_status, vanished_count, "
        "exclude_names, confirmed_missing_count, path_unverified_count, "
        "plan_id, round_id, metric_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (created_at, root, 1, 0, 0.1, 4096, min_kb, "full", 0,
         exclude_names, 0, 0, plan_id, round_id, metric_version),
    )
    sid = int(cur.lastrowid)
    if entry is not None:
        conn.execute(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
            (sid, entry[0], entry[1]),
        )
    conn.commit()
    return sid


def _row(conn: sqlite3.Connection, sid: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()


class TestPlanIdentityHelpers:
    """范围/计划身份登记与内容派生稳定 ID。"""

    def test_scope_roundtrip_and_last_seen_refresh(self, conn):
        seen = "2026-10-04T08:00:00"
        scope_id = scanner.ensure_scan_scope(
            conn, "apfs-volume:uuid-A", "startup_volume",
            container_id="apfs-container:C1", volume_group_id="apfs-vg:G",
            mount_path="/Volumes/A", display_name="A", seen_at=seen)
        assert scope_id == "apfs-volume:uuid-A"
        again = scanner.ensure_scan_scope(
            conn, "apfs-volume:uuid-A", "startup_volume",
            container_id="apfs-container:C1",
            mount_path="/Volumes/A2", display_name="A2",
            seen_at="2026-10-04T09:00:00")
        assert again == scope_id  # 同身份重复登记幂等
        row = conn.execute(
            "SELECT * FROM scan_scopes WHERE scope_id=?",
            (scope_id,)).fetchone()
        assert row["kind"] == "startup_volume"
        assert row["container_id"] == "apfs-container:C1"
        assert row["volume_group_id"] == "apfs-vg:G"
        assert row["created_at"] == seen  # 首见时间不改写
        assert row["last_seen_at"] == "2026-10-04T09:00:00"  # 只刷新观察时间
        assert row["mount_path"] == "/Volumes/A2"  # 诊断字段刷新

    def test_plan_id_is_content_derived_and_stable(self, conn):
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-A",
                                  "startup_volume")
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-B",
                                  "startup_volume")
        p1 = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-A",
                                      "/synthetic/root", 1, 1024, "")
        p2 = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-A",
                                      "/synthetic/root", 1, 1024, "")
        assert p1 == p2  # 同计划重复登记返回同一 ID
        assert p1.startswith("plan:")
        # 任一身份维度变化都形成新计划。
        assert scanner.ensure_scan_plan(
            conn, "apfs-volume:uuid-A", "/synthetic/root", 1, 2048, "") != p1
        assert scanner.ensure_scan_plan(
            conn, "apfs-volume:uuid-A", "/synthetic/root", 2, 1024, "") != p1
        assert scanner.ensure_scan_plan(
            conn, "apfs-volume:uuid-A", "/synthetic/root", 1, 1024,
            "skip.noindex") != p1
        assert scanner.ensure_scan_plan(
            conn, "apfs-volume:uuid-B", "/synthetic/root", 1, 1024, "") != p1
        row = conn.execute("SELECT COUNT(*) FROM scan_plans").fetchone()
        assert row[0] == 5  # 一次登记一条，不因重复登记膨胀


class TestRoundLifecycle:
    """扫描轮次与成员关联：各自时间/状态；容量样本另存来源/时间。"""

    def test_round_and_member_roundtrip(self, conn):
        started = "2026-10-04T08:00:00"
        rid = scanner.begin_scan_round(conn, started_at=started)
        scanner.add_round_member(
            conn, rid, seq=1, plan_id="plan:aa", scope_id="apfs-volume:uuid-A",
            status="succeeded", started_at=started,
            finished_at="2026-10-04T08:01:00")
        scanner.finish_scan_round(conn, rid, "succeeded",
                                  finished_at="2026-10-04T08:05:00",
                                  message="ok")
        round_row = conn.execute(
            "SELECT * FROM scan_rounds WHERE id=?", (rid,)).fetchone()
        assert (round_row["started_at"], round_row["status"],
                round_row["finished_at"]) == (
            started, "succeeded", "2026-10-04T08:05:00")
        member = conn.execute(
            "SELECT * FROM scan_round_members WHERE round_id=? AND seq=1",
            (rid,)).fetchone()
        assert member["plan_id"] == "plan:aa"
        assert member["scope_id"] == "apfs-volume:uuid-A"
        assert member["snapshot_id"] is None
        assert member["snapshot_status"] is None  # 未关联快照
        assert member["status"] == "succeeded"

    def test_member_with_snapshot_is_active(self, conn):
        sid = _insert_snapshot(conn, "2026-10-04T08:00:00")
        rid = scanner.begin_scan_round(conn)
        scanner.add_round_member(conn, rid, seq=1, snapshot_id=sid,
                                 status="succeeded")
        member = conn.execute(
            "SELECT snapshot_id, snapshot_status FROM scan_round_members "
            "WHERE round_id=?", (rid,)).fetchone()
        assert member["snapshot_id"] == sid
        assert member["snapshot_status"] == "active"

    def test_container_capacity_sample_keeps_source_and_time(self, conn):
        sampled = "2026-10-04T08:00:00"
        scanner.record_container_capacity_sample(
            conn, "apfs-container:C1", total_bytes=500_000_000_000,
            free_bytes=123_000_000_000, source="storage-discovery",
            sampled_at=sampled)
        scanner.record_container_capacity_sample(
            conn, "apfs-container:C1", total_bytes=None, free_bytes=None,
            source="statvfs")
        rows = conn.execute(
            "SELECT * FROM container_capacity_samples "
            "ORDER BY id").fetchall()
        assert [(r["container_id"], r["total_bytes"], r["free_bytes"],
                 r["source"], r["sampled_at"]) for r in rows] == [
            ("apfs-container:C1", 500_000_000_000, 123_000_000_000,
             "storage-discovery", sampled),
            # statvfs 来源如实记录——不冒充整盘发现样本。
            ("apfs-container:C1", None, None, "statvfs", rows[1]["sampled_at"]),
        ]


class TestDatasetIdentityIsolation:
    """反例①②：身份维度变化不可比；同容器不同卷可分组。"""

    def test_same_path_different_volume_not_comparable(self, conn):
        """反例①：同一路径换卷后历史互不为基线。"""
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-OLD",
                                  "startup_volume",
                                  container_id="apfs-container:C1")
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-NEW",
                                  "startup_volume",
                                  container_id="apfs-container:C1")
        old_plan = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-OLD",
                                            "/Volumes/Data", 1, 1024, "")
        new_plan = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-NEW",
                                            "/Volumes/Data", 1, 1024, "")
        assert old_plan != new_plan  # 换卷 → 新计划
        a = _insert_snapshot(conn, "2026-10-01T09:00:00",
                             root="/Volumes/Data", plan_id=old_plan,
                             metric_version=1)
        b = _insert_snapshot(conn, "2026-10-02T09:00:00",
                             root="/Volumes/Data", plan_id=new_plan,
                             metric_version=1)
        assert not reports.same_dataset(_row(conn, a), _row(conn, b))
        # 换卷后的新快照在同路径同口径下仍无基线：旧卷历史不可混入。
        assert reports.find_same_dataset_predecessor(conn, b) is None
        rows = reports.find_same_dataset_snapshot_rows(
            conn, reports.dataset_identity(_row(conn, b)))
        assert [r["id"] for r in rows] == [b]

    def test_same_scope_changed_plan_not_comparable(self, conn):
        """反例①：同卷换计划（阈值/计量版本/排除）不可比。"""
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-A",
                                  "startup_volume")
        p_kb = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-A",
                                        "/synthetic/root", 1, 1024, "")
        p_mv = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-A",
                                        "/synthetic/root", 2, 1024, "")
        a = _insert_snapshot(conn, "2026-10-01T09:00:00", plan_id=p_kb,
                             metric_version=1)
        b = _insert_snapshot(conn, "2026-10-02T09:00:00", plan_id=p_mv,
                             metric_version=2)
        assert not reports.same_dataset(_row(conn, a), _row(conn, b))
        assert reports.find_same_dataset_predecessor(conn, b) is None

    def test_same_container_different_volumes_group_separately(self, conn):
        """反例②：同容器两卷各自成组，趋势窗口互不混点。"""
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-SYS",
                                  "startup_volume",
                                  container_id="apfs-container:C1")
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-DATA",
                                  "startup_volume",
                                  container_id="apfs-container:C1")
        p_sys = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-SYS", "/", 1,
                                         1024, "")
        p_data = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-DATA",
                                          "/System/Volumes/Data", 1, 1024, "")
        s1 = _insert_snapshot(conn, "2026-10-01T09:00:00", root="/",
                              plan_id=p_sys, metric_version=1)
        s2 = _insert_snapshot(conn, "2026-10-02T09:00:00", root="/",
                              plan_id=p_sys, metric_version=1)
        d1 = _insert_snapshot(conn, "2026-10-01T09:00:00",
                              root="/System/Volumes/Data", plan_id=p_data,
                              metric_version=1)
        d2 = _insert_snapshot(conn, "2026-10-02T09:00:00",
                              root="/System/Volumes/Data", plan_id=p_data,
                              metric_version=1)
        sys_rows = reports.find_same_dataset_snapshot_rows(
            conn, reports.dataset_identity(_row(conn, s2)))
        data_rows = reports.find_same_dataset_snapshot_rows(
            conn, reports.dataset_identity(_row(conn, d2)))
        assert [r["id"] for r in sys_rows] == [s1, s2]
        assert [r["id"] for r in data_rows] == [d1, d2]
        # 同容器不合并身份：跨卷前驱不存在。
        assert reports.find_same_dataset_predecessor(conn, d2)["id"] == d1
        assert not reports.same_dataset(_row(conn, s2), _row(conn, d2))

    def test_legacy_rows_never_mixed_with_branded_rows(self, conn):
        """反例③（读法面）：legacy 行不与新身份行混比，旧 API 读法不变。"""
        a = _insert_snapshot(conn, "2026-10-01T09:00:00")  # legacy：无 plan
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-A",
                                  "startup_volume")
        p = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-A",
                                     "/synthetic/root", 1, 1024, "")
        b = _insert_snapshot(conn, "2026-10-02T09:00:00", plan_id=p,
                             metric_version=1)
        identity = reports.dataset_identity(_row(conn, a))
        # 4 元组：前三位仍是 (root, min_kb, exclude_names)——api.py 的
        # identity[0..2] 读法保持兼容。
        assert identity[:3] == ("/synthetic/root", 1024, "")
        assert identity[3] is None
        rows = reports.find_same_dataset_snapshot_rows(conn, identity)
        assert [r["id"] for r in rows] == [a]  # 新身份行不混入 legacy 窗口
        assert reports.find_same_dataset_predecessor(conn, b) is None
        # legacy 前驱不受新身份行影响。
        assert reports.find_same_dataset_predecessor(conn, a) is None


class TestSameDayReplacementAndRetentionIsolation:
    """反例④：同日替换/保留/淘汰按新身份隔离；AI 证据不被级联删除。"""

    @staticmethod
    def _freeze_today(monkeypatch, today: dt.date) -> None:
        """把 dt.date.today() 冻结到指定日期（timedelta/isoformat 不变）。"""

        class _FrozenDate(dt.date):
            @classmethod
            def today(cls) -> dt.date:  # type: ignore[override]
                return today

        monkeypatch.setattr(dt, "date", _FrozenDate)

    def test_same_day_replacement_isolated_by_plan(self, conn, isolated):
        """同日重复扫描：同 plan 一天一行；不同 plan/legacy 互不替换。"""
        scanroot = isolated["scanroot"] / "data"
        scanroot.mkdir()
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-A",
                                  "startup_volume")
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-B",
                                  "startup_volume")
        p1 = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-A",
                                      str(scanroot), 1, 1, "")
        # p2 = 同一路径但另一个卷（换卷即新计划）。实测口径与登记完全
        # 一致，故写入合法；当日不互替只因计划身份不同——不是靠口径
        # 不一致换取的隔离（口径不一致会被写入闸门直接拒绝，见
        # TestPlanIdentityEnforcement）。
        p2 = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-B",
                                      str(scanroot), 1, 1, "")
        assert p1 != p2
        first = scanner.create_snapshot(conn, root=scanroot, min_kb=1,
                                        plan_id=p1, metric_version=1)
        other = scanner.create_snapshot(conn, root=scanroot, min_kb=1,
                                        plan_id=p2, metric_version=1)
        legacy = scanner.create_snapshot(conn, root=scanroot, min_kb=1)
        # 三种身份同日共存：互不替换。
        for sid in (first, other, legacy):
            assert conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE id=?",
                (sid,)).fetchone()[0] == 1
        # 同 plan 同日重扫才触发"一天一行"替换。
        again = scanner.create_snapshot(conn, root=scanroot, min_kb=1,
                                        plan_id=p1, metric_version=1)
        assert conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE id=?",
            (first,)).fetchone()[0] == 0  # p1 旧行被替换
        assert conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE id=?",
            (again,)).fetchone()[0] == 1
        # 其他身份的当日行原样保留。
        for sid in (other, legacy):
            assert conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE id=?",
                (sid,)).fetchone()[0] == 1

    def test_prune_week_groups_are_isolated_by_plan(self, conn, isolated,
                                                    monkeypatch):
        """保留策略周分组按 plan 独立：两个 plan 同周各留一份，不互挤。"""
        # 冻结 today=2026-10-04：daily_cutoff=10-01，weekly_cutoff=09-06；
        # 9 月第二周（ISO week 37：9-07..9-09）处于周保留窗口内。
        self._freeze_today(monkeypatch, dt.date(2026, 10, 4))
        old_a1 = _insert_snapshot(conn, "2026-09-07T09:00:00", plan_id="plan:aa")
        old_b1 = _insert_snapshot(conn, "2026-09-08T09:00:00", plan_id="plan:bb")
        old_a2 = _insert_snapshot(conn, "2026-09-09T09:00:00", plan_id="plan:aa")
        old_b2 = _insert_snapshot(conn, "2026-09-09T10:00:00", plan_id="plan:bb")
        deleted = scanner.prune_snapshots(conn, keep_daily_days=3,
                                          keep_weekly_weeks=4)
        # 两个 plan 的同一 ISO 周各自保留最早一份（a1、b1），其余淘汰。
        surviving = {r["id"] for r in conn.execute("SELECT id FROM snapshots")}
        assert surviving == {old_a1, old_b1}
        assert deleted == 2

    def test_pruned_snapshot_marks_member_expired_keeps_ai_evidence(
        self, conn, isolated, monkeypatch
    ):
        """关联快照淘汰：轮次成员引用明确 expired；已保存 AI 证据存活。"""
        scanroot = isolated["scanroot"] / "data"
        scanroot.mkdir()
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-A",
                                  "startup_volume")
        plan = scanner.ensure_scan_plan(conn, "apfs-volume:uuid-A",
                                        str(scanroot), 1, 1, "")
        sid = scanner.create_snapshot(conn, root=scanroot, min_kb=1,
                                      plan_id=plan, metric_version=1)
        rid = scanner.begin_scan_round(conn)
        scanner.add_round_member(conn, rid, seq=1, snapshot_id=sid,
                                 plan_id=plan,
                                 scope_id="apfs-volume:uuid-A",
                                 status="succeeded")
        scanner.finish_scan_round(conn, rid, "succeeded")
        # 一条已保存分析证据引用该快照（v7 合同：无外键、不级联）。
        conn.execute(
            "INSERT INTO analysis_runs(job_id, a_snapshot_id, b_snapshot_id, "
            "request_digest, facts_digest, prompt_version, idempotency_key, "
            "runtime_id, runtime_executable, settings_revision, "
            "consent_revision, status, owner_id, created_at) "
            "VALUES ('job-e', ?, ?, 'rd', 'fd', 'pv', 'ik-e', 'rt', '/bin/x', "
            "0, 0, 'succeeded', 'o', '2026-10-04T08:00:00')", (sid, sid))
        conn.execute(
            "INSERT INTO agent_analyses(job_id, a_snapshot_id, b_snapshot_id, "
            "a_created_at, b_created_at, dataset_root, dataset_min_kb, "
            "dataset_exclude_names, request_digest, facts_digest, "
            "prompt_version, adapter_contract_version, runtime_id, "
            "result_json, facts_json, manifest_json, created_at) "
            "VALUES ('job-e', ?, ?, 't1', 't2', '/r', 1, '', 'rd', 'fd', "
            "'pv', 1, 'rt', '{}', '{}', '{}', '2026-10-04T08:00:00')",
            (sid, sid))
        conn.commit()

        # 冻结 today=次日：keep_daily_days=0/keep_weekly_weeks=0 时当日
        # 采集的快照（created_at=真实今天）整体越过保留窗口被淘汰。
        # 次日由真实今天推得——硬编码日期会让本用例在 authored 当天之后
        # 变成时间炸弹（created 与 daily_cutoff 同日 → 落在「近 N 天全
        # 保留」分支，淘汰断言必然失败）。
        self._freeze_today(monkeypatch,
                           dt.date.today() + dt.timedelta(days=1))
        scanner.prune_snapshots(conn, keep_daily_days=0, keep_weekly_weeks=0)

        member = conn.execute(
            "SELECT * FROM scan_round_members WHERE round_id=?",
            (rid,)).fetchone()
        assert member is not None  # 成员历史保留
        assert member["snapshot_id"] is None  # 引用被显式解除
        assert member["snapshot_status"] == "expired"  # 明确过期，可查询区分
        assert conn.execute(
            "SELECT status FROM scan_rounds WHERE id=?",
            (rid,)).fetchone()[0] == "succeeded"  # 轮次记录不受影响
        # 已保存 AI 证据不被级联删除。
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_analyses WHERE job_id='job-e'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM analysis_runs WHERE job_id='job-e'"
        ).fetchone()[0] == 1

    def test_drop_same_day_leaves_other_plan_rows_alone(self, conn):
        """_drop_same_day 直接验证：plan 隔离不误删其他身份当日行。"""
        p1, p2 = "plan:aa", "plan:bb"
        a = _insert_snapshot(conn, "2026-10-04T09:00:00", plan_id=p1)
        b = _insert_snapshot(conn, "2026-10-04T09:30:00", plan_id=p2)
        rid = scanner.begin_scan_round(conn)
        conn.execute(
            "INSERT INTO scan_round_members(round_id, seq, plan_id, "
            "snapshot_id, snapshot_status, status) "
            "VALUES (?, 1, ?, ?, 'active', 'succeeded')", (rid, p1, a))
        conn.commit()
        scanner._drop_same_day(conn, "2026-10-04", "/synthetic/root", 1024,
                               "", plan_id=p1)
        assert conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE id=?", (b,)).fetchone()[0] == 1
        member = conn.execute(
            "SELECT snapshot_id, snapshot_status FROM scan_round_members"
        ).fetchone()
        assert member["snapshot_id"] is None
        assert member["snapshot_status"] == "expired"


class TestPlanIdentityEnforcement:
    """ISS-153 审计返修：快照写入前必须校验 plan 身份 vs 实际测量口径。

    Codex 审计 blocking 反例：把已登记的 A 目录 plan_id 传给 B 目录（或
    改了阈值/排除/计量版本）的实际测量，持久化路径仍接受写入；且按 plan
    分组的同日替换会**删掉 A 当天原有快照**——跨口径写入即身份污染，
    违反 153 卡身份合同（plan 身份含规范根/阈值/排除/计量版本）。

    合同：写入前校验一致；不一致**明确拒绝**（抛明确异常），不静默重映射、
    不静默换 plan、**不删任何既有快照**；错误信息如实且不泄漏内部路径细节。
    """

    @staticmethod
    def _make_roots(isolated) -> tuple:
        """两个真实存在的合成根（A = 计划登记口径，B = 跨口径实际测量根）。"""
        root_a = isolated["scanroot"] / "plan_a"
        root_b = isolated["scanroot"] / "plan_b"
        for r in (root_a, root_b):
            r.mkdir()
            (r / "payload.bin").write_bytes(b"x" * 2048)
        return root_a, root_b

    @staticmethod
    def _fingerprint(conn: sqlite3.Connection, sid: int):
        """快照本体 + 条目 + 卷容量的完整指纹，用于断言「原样完好」。"""
        row = conn.execute("SELECT * FROM snapshots WHERE id=?",
                           (sid,)).fetchone()
        entries = [tuple(r) for r in conn.execute(
            "SELECT path, size_kb FROM entries WHERE snapshot_id=? "
            "ORDER BY path", (sid,))]
        vol = conn.execute(
            "SELECT total_bytes, free_bytes FROM volume_stats "
            "WHERE snapshot_id=?", (sid,)).fetchone()
        return (dict(row) if row is not None else None, entries,
                tuple(vol) if vol is not None else None)

    def _seed_plan_a(self, conn, root_a, *, min_kb: int = 1,
                     metric_version: int = 1,
                     exclude_names: str = ""):
        """登记 A 目录 plan 并落一条 A 当天快照，返回 (plan_id, sid_a)。"""
        scanner.ensure_scan_scope(conn, "apfs-volume:uuid-A",
                                  "startup_volume")
        plan_a = scanner.ensure_scan_plan(
            conn, "apfs-volume:uuid-A", str(root_a), metric_version,
            min_kb, exclude_names)
        sid_a = scanner.create_snapshot(conn, root=root_a, min_kb=min_kb,
                                        plan_id=plan_a,
                                        metric_version=metric_version)
        return plan_a, sid_a

    def _assert_rejected_and_a_intact(self, conn, sid_a, before, call, *,
                                      leaked: tuple = (),
                                      mentions: str = ""):
        """被明确拒绝 + 不写库 + A 快照逐字段完好 + 信息不泄漏路径。"""
        count_before = conn.execute(
            "SELECT COUNT(*) FROM snapshots").fetchone()[0]
        with pytest.raises(scanner.InvalidScanError) as exc:
            call()
        message = str(exc.value)
        assert message.strip()  # 错误信息如实非空
        if mentions:
            assert mentions in message  # 指名不匹配的维度
        for secret in leaked:
            assert secret not in message  # 不泄漏内部路径细节
        # 未写入任何新快照。
        assert conn.execute(
            "SELECT COUNT(*) FROM snapshots").fetchone()[0] == count_before
        # A 当天原有快照逐字段完好（未被同日替换误删、未被改写）。
        assert self._fingerprint(conn, sid_a) == before

    def test_rejects_other_root_measurement(self, conn, isolated):
        """反例①：根不同——以 B 目录实测 + A 的 plan_id 写入被拒。"""
        root_a, root_b = self._make_roots(isolated)
        plan_a, sid_a = self._seed_plan_a(conn, root_a)
        before = self._fingerprint(conn, sid_a)
        self._assert_rejected_and_a_intact(
            conn, sid_a, before,
            lambda: scanner.create_snapshot(conn, root=root_b, min_kb=1,
                                            plan_id=plan_a,
                                            metric_version=1),
            leaked=(str(root_a), str(root_b)),
            mentions="规范根",
        )
        # A 的计划归属未被污染：没有出现 root=B 却挂 plan_A 的行。
        assert conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE plan_id=? AND root=?",
            (plan_a, str(root_b))).fetchone()[0] == 0

    def test_rejects_threshold_mismatch(self, conn, isolated):
        """反例②：阈值不同——同根同计划身份但 min_kb 不符，写入被拒。"""
        root_a, _ = self._make_roots(isolated)
        plan_a, sid_a = self._seed_plan_a(conn, root_a, min_kb=1)
        before = self._fingerprint(conn, sid_a)
        self._assert_rejected_and_a_intact(
            conn, sid_a, before,
            lambda: scanner.create_snapshot(conn, root=root_a, min_kb=2,
                                            plan_id=plan_a,
                                            metric_version=1),
            leaked=(str(root_a),),
            mentions="阈值",
        )

    def test_rejects_exclude_mismatch(self, conn, isolated, monkeypatch):
        """反例③：排除掩码不同——实测排除集与计划登记不符，写入被拒。"""
        root_a, _ = self._make_roots(isolated)
        plan_a, sid_a = self._seed_plan_a(conn, root_a, exclude_names="")
        before = self._fingerprint(conn, sid_a)
        # 实测口径改用另一套排除掩码，计划身份未随之更新。
        monkeypatch.setattr(config, "EXCLUDE_NAMES", ["skip.noindex"])
        self._assert_rejected_and_a_intact(
            conn, sid_a, before,
            lambda: scanner.create_snapshot(conn, root=root_a, min_kb=1,
                                            plan_id=plan_a,
                                            metric_version=1),
            leaked=(str(root_a),),
            mentions="排除",
        )

    def test_rejects_metric_version_mismatch(self, conn, isolated):
        """反例④：计量版本不同——写库时口径版本与计划登记不符，写入被拒。"""
        root_a, _ = self._make_roots(isolated)
        plan_a, sid_a = self._seed_plan_a(conn, root_a, metric_version=1)
        before = self._fingerprint(conn, sid_a)
        self._assert_rejected_and_a_intact(
            conn, sid_a, before,
            lambda: scanner.create_snapshot(conn, root=root_a, min_kb=1,
                                            plan_id=plan_a,
                                            metric_version=2),
            leaked=(str(root_a),),
            mentions="计量版本",
        )

    def test_rejects_missing_metric_version_for_registered_plan(
        self, conn, isolated
    ):
        """反例⑤：已登记计划却漏传计量版本（None），不得静默补全身份。"""
        root_a, _ = self._make_roots(isolated)
        plan_a, sid_a = self._seed_plan_a(conn, root_a, metric_version=1)
        before = self._fingerprint(conn, sid_a)
        self._assert_rejected_and_a_intact(
            conn, sid_a, before,
            lambda: scanner.create_snapshot(conn, root=root_a, min_kb=1,
                                            plan_id=plan_a,
                                            metric_version=None),
            leaked=(str(root_a),),
            mentions="计量版本",
        )

    def test_rejects_unregistered_plan_id(self, conn, isolated):
        """反例⑥：plan_id 从未登记——无法核验身份，必须拒绝而非照写。"""
        root_a, _ = self._make_roots(isolated)
        ghost = scanner.plan_identity_id("apfs-volume:uuid-A", str(root_a),
                                         1, 1, "")
        sid_a = scanner.create_snapshot(conn, root=root_a, min_kb=1)
        before = self._fingerprint(conn, sid_a)
        self._assert_rejected_and_a_intact(
            conn, sid_a, before,
            lambda: scanner.create_snapshot(conn, root=root_a, min_kb=1,
                                            plan_id=ghost,
                                            metric_version=1),
            leaked=(str(root_a),),
            mentions="未登记",
        )

    def test_mismatch_error_is_distinguishable_subclass(self):
        """拒绝异常须可与「du 采集无效」区分，同时保持既有捕获面。"""
        assert issubclass(scanner.PlanIdentityMismatchError,
                          scanner.InvalidScanError)
        assert scanner.PlanIdentityMismatchError is not scanner.InvalidScanError

    def test_legacy_null_plan_path_unchanged(self, conn, isolated):
        """零行为变化：默认参数（plan_id=None）路径照常写入并按 legacy 分组。"""
        root_a, root_b = self._make_roots(isolated)
        sid_a = scanner.create_snapshot(conn, root=root_a, min_kb=1)
        sid_b = scanner.create_snapshot(conn, root=root_b, min_kb=1)
        for sid, root in ((sid_a, root_a), (sid_b, root_b)):
            row = _row(conn, sid)
            assert row["plan_id"] is None
            assert row["round_id"] is None
            assert row["metric_version"] is None
            assert row["root"] == str(root)
        # legacy 同日重复仍是一天一行（按 root/min_kb/exclude 分组）。
        again = scanner.create_snapshot(conn, root=root_a, min_kb=1)
        assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE id=?",
                            (sid_a,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE id=?",
                            (again,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE id=?",
                            (sid_b,)).fetchone()[0] == 1
