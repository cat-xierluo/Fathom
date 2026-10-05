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

from fathom import analysis_contract as ac
from fathom import analysis_manager as am
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


# ==========================================================================
# 返修 2：AI 历史消费者的身份隔离
# ==========================================================================


def _enable_analysis_runtime(runtime_dir):
    """装一个只过能力门的合成解读引擎并开启授权（不派发，只为走通
    create_preview 的前置门：启用 → 升级停写 → Runtime 复核）。"""
    script = runtime_dir / "fake-runtime"
    script.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "--version" ]; then echo "2.1.237 (Claude Code)"; exit 0; fi\n'
        'cat > /dev/null; cat <<\'EOS\'\n'
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"{\\"schema_version\\":1,\\"summary\\":\\"s\\","'
        '\\"findings\\":[],\\"limitations\\":[],\\"inspect_next\\":[]}"}\n'
        'EOS\n',
        encoding="utf-8")
    script.chmod(0o755)
    config.update_user_settings({"analysis": {
        "enabled": True,
        "runtime": {"id": "claude-code", "executable": str(script),
                    "version": "2.1.237"},
    }})
    return script


def _insert_legacy_analysis(conn: sqlite3.Connection, *, a: int, b: int,
                            a_created: str, b_created: str,
                            root: str, min_kb: int | None = 1024,
                            exclude_names: str = "",
                            job_id: str = "job-legacy") -> int:
    """直接落一条 legacy 身份的已保存 AI 证据（生命周期行 + 证据行）。

    只为驱动读取层的过期评估，不经过派发。facts/result/manifest 用最小
    可解析 JSON 占位——本组用例只关心 expired/expired_reason。
    """
    conn.execute(
        "INSERT INTO analysis_runs(job_id, a_snapshot_id, b_snapshot_id,"
        " request_digest, facts_digest, prompt_version, idempotency_key,"
        " runtime_id, runtime_executable, runtime_version, settings_revision,"
        " consent_revision, status, owner_id, created_at, finished_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, a, b, "d" * 64, "f" * 64, "v1", f"idem-{job_id}",
         "claude-code", "/synthetic/runtime", "2.1.237", 1, 1,
         "succeeded", "test", "2026-09-28T10:00:00", "2026-09-28T10:00:01"),
    )
    cur = conn.execute(
        "INSERT INTO agent_analyses(job_id, a_snapshot_id, b_snapshot_id,"
        " a_created_at, b_created_at, dataset_root, dataset_min_kb,"
        " dataset_exclude_names, request_digest, facts_digest, prompt_version,"
        " adapter_contract_version, runtime_id, runtime_version, model,"
        " result_json, facts_json, manifest_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, a, b, a_created, b_created, root, min_kb, exclude_names,
         "d" * 64, "f" * 64, "v1", 1, "claude-code", "2.1.237", "synthetic",
         "{}", "{}", "{}", "2026-09-28T10:00:02"),
    )
    conn.commit()
    return int(cur.lastrowid)


class TestAiConsumerIdentityIsolation:
    """ISS-153 返修 2：AI 历史消费者必须与新身份隔离。

    Codex 审计 blocking 反例（两路）：
    ① 新 plan 身份的快照仍被接受构造 AI 事实包并可落库——但已保存证据的
       身份只扩到 (root, min_kb, exclude_names)，未含 plan_id，读回时无法
       证明它属于哪个计划；
    ② legacy 快照被删后，只要**其他 plan** 在同一天有同数据集后继，
       ``_evaluate_expiry()`` 就返回 ``snapshot_replaced``——把别的数据集
       当成本数据集的替换证据。

    合同：新身份范围本期**明确拒绝** AI 解读（不静默混读、不落库、错误
    如实说明）；legacy 过期评估只承认 legacy 快照作为 legacy 的替换/后继
    证据；legacy→legacy 行为逐字不变。
    """

    # ---------------------------------------------------------------- 反例 ①

    def test_facts_package_seam_rejects_new_plan_snapshots(self, conn):
        """接缝级：新身份快照构造事实包 → 明确拒绝，纯读且零写入。"""
        a = _insert_snapshot(conn, "2026-09-28T08:00:00", plan_id="plan:A",
                             round_id=1, metric_version=1,
                             entry=("/synthetic/root", 2048))
        b = _insert_snapshot(conn, "2026-09-28T09:00:00", plan_id="plan:A",
                             round_id=1, metric_version=1,
                             entry=("/synthetic/root", 4096))
        before = conn.total_changes
        with pytest.raises(ac.AnalysisContractError) as ei:
            ac.build_facts_package(conn, a, b)
        assert ei.value.reason_code == "plan_identity_unsupported"
        msg = str(ei.value)
        # 错误如实说明「本期不支持」，不谎称快照缺失或口径不一致。
        assert "AI" in msg and "plan" in msg.lower()
        assert "不支持" in msg
        assert conn.total_changes == before  # 纯读拒绝：无任何写入

    def test_create_preview_rejects_new_plan_and_persists_nothing(
            self, isolated, conn):
        """端到端：走通 create_preview 的前置门后，新身份被拒且零落库。"""
        _enable_analysis_runtime(isolated["runtime"])
        a = _insert_snapshot(conn, "2026-09-28T08:00:00", plan_id="plan:A",
                             entry=("/synthetic/root", 2048))
        b = _insert_snapshot(conn, "2026-09-28T09:00:00", plan_id="plan:A",
                             entry=("/synthetic/root", 4096))
        manager = am.AnalysisManager(run_timeout_s=20.0)
        with pytest.raises(am.AnalysisError) as ei:
            manager.create_preview(a, b)
        # 400 明确拒绝（不是 404 快照缺失、不是 403 授权问题）。
        assert ei.value.status_code == 400
        assert ei.value.reason_code == "plan_identity_unsupported"
        assert "不支持" in str(ei.value)
        # 不落库：既无证据行，也无生命周期行。
        assert conn.execute("SELECT COUNT(*) c FROM agent_analyses"
                            ).fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM analysis_runs"
                            ).fetchone()["c"] == 0
        # 失败不留可复用预览。
        assert manager._previews == {}

    @pytest.mark.parametrize("plan_side", ["a", "b"])
    def test_either_side_with_new_plan_is_rejected(self, conn, plan_side):
        """隔离是对称的：任一侧带新身份即整体拒绝（不只查 a）。"""
        a = _insert_snapshot(conn, "2026-09-28T08:00:00",
                             plan_id="plan:A" if plan_side == "a" else None,
                             entry=("/synthetic/root", 2048))
        b = _insert_snapshot(conn, "2026-09-28T09:00:00",
                             plan_id="plan:A" if plan_side == "b" else None,
                             entry=("/synthetic/root", 4096))
        with pytest.raises(ac.AnalysisContractError) as ei:
            ac.build_facts_package(conn, a, b)
        assert ei.value.reason_code == "plan_identity_unsupported"

    def test_legacy_snapshots_still_build_facts_package(self, conn):
        """零行为变化：legacy（plan_id IS NULL）路径照常构造事实包。"""
        a = _insert_snapshot(conn, "2026-09-28T08:00:00",
                             entry=("/synthetic/root", 2048))
        b = _insert_snapshot(conn, "2026-09-28T09:00:00",
                             entry=("/synthetic/root", 4096))
        pkg = ac.build_facts_package(conn, a, b)
        assert pkg.payload["a"]["snapshot_id"] == a
        assert pkg.payload["b"]["snapshot_id"] == b
        assert pkg.payload["entries"]

    # ---------------------------------------------------------------- 反例 ②

    def _legacy_pair_and_analysis(self, conn, root: str):
        """两枚 legacy 快照 + 一条以它们为 a/b 的已保存证据。"""
        a = _insert_snapshot(conn, "2026-09-28T08:00:00", root=root,
                             entry=(f"{root}/A", 2048))
        b = _insert_snapshot(conn, "2026-09-28T09:00:00", root=root,
                             entry=(f"{root}/A", 4096))
        _insert_legacy_analysis(conn, a=a, b=b,
                                a_created="2026-09-28T08:00:00",
                                b_created="2026-09-28T09:00:00", root=root)
        return a, b

    def test_other_plan_same_day_successor_is_not_replacement(
            self, isolated, conn):
        """legacy b 被删，仅存**其他 plan** 的同日后继 → 不得说 replaced。

        该后继属于另一个数据集（新身份），拿它当 legacy 的替换证据就是
        跨身份混读。快照确已消失，按既有枚举如实落 snapshot_pruned。
        """
        root = str(isolated["scanroot"])
        _a, b = self._legacy_pair_and_analysis(conn, root)
        conn.execute("DELETE FROM entries WHERE snapshot_id=?", (b,))
        conn.execute("DELETE FROM snapshots WHERE id=?", (b,))
        _insert_snapshot(conn, "2026-09-28T09:40:00", root=root,
                         plan_id="plan:other", round_id=9, metric_version=1,
                         entry=(f"{root}/A", 6000))
        conn.commit()
        items = am.AnalysisManager().list_analyses(_a, b)
        assert len(items) == 1
        assert items[0]["expired"] is True
        assert items[0]["expired_reason"] != "snapshot_replaced"
        assert items[0]["expired_reason"] == "snapshot_pruned"

    def test_legacy_to_legacy_same_day_successor_still_replaced(
            self, isolated, conn):
        """回归钉：legacy→legacy 同日后继仍如实判为 replaced（行为不变）。"""
        root = str(isolated["scanroot"])
        _a, b = self._legacy_pair_and_analysis(conn, root)
        conn.execute("DELETE FROM entries WHERE snapshot_id=?", (b,))
        conn.execute("DELETE FROM snapshots WHERE id=?", (b,))
        _insert_snapshot(conn, "2026-09-28T09:40:00", root=root,
                         entry=(f"{root}/A", 6000))
        conn.commit()
        items = am.AnalysisManager().list_analyses(_a, b)
        assert items[0]["expired"] is True
        assert items[0]["expired_reason"] == "snapshot_replaced"

    def test_cross_plan_successor_does_not_expire_untouched_legacy(
            self, isolated, conn):
        """只有新身份后继、本体仍在 → 仍是未过期（新增快照不过期旧报告）。"""
        root = str(isolated["scanroot"])
        a, b = self._legacy_pair_and_analysis(conn, root)
        _insert_snapshot(conn, "2026-09-28T11:00:00", root=root,
                         plan_id="plan:other", round_id=9, metric_version=1,
                         entry=(f"{root}/A", 6000))
        conn.commit()
        items = am.AnalysisManager().list_analyses(a, b)
        assert items[0]["expired"] is False
        assert items[0]["expired_reason"] is None


class TestFactsRowCoreIdentityGate:
    """ISS-153 审计返修 3：身份闸门必须下沉到行构造核心本身。

    上游第三轮 blocking 反例（独立对照实证）：**同一对**带 ``plan:A`` 的真实
    SQLite 快照，``build_facts_package()`` 拒绝（plan_identity_unsupported），
    而 ``build_facts_from_rows()`` 却接受并生成**不含 plan 身份**的事实包
    ——共享构造入口违反「新 plan 不支持事实包构造」合同。闸门只挂在 conn 入口
    等于可绕：夹具、单测或任何持有行的调用方都能直调核心拿到无身份事实包。

    合同：闸门覆盖 ``build_facts_from_rows()``，任一侧快照 ``plan_id`` 非 NULL
    即拒绝（错误口径与既有 ``plan_identity_unsupported`` 一致，且必须先于同口径
    校验，不能退化成失真的 ``dataset_mismatch``）；两侧 ``plan_id`` 均为 NULL
    的 legacy 直调行为不变。
    """

    def _pair(self, conn, plan_a, plan_b):
        """两枚真实 SQLite 快照行 + entries 映射（复刻 conn 入口的入参形态）。

        刻意从库里取真实 ``sqlite3.Row``、用 ``reports.load_snapshot`` 取映射，
        而不是构造 dict 夹具：上游 blocking 反例正是这对真实行的**直调绕过**。
        """
        def _snap(created_at, plan_id, size_kb):
            sid = _insert_snapshot(
                conn, created_at, plan_id=plan_id,
                round_id=1 if plan_id is not None else None,
                metric_version=1 if plan_id is not None else None,
                entry=("/synthetic/root", size_kb),
            )
            return sid, _row(conn, sid), reports.load_snapshot(conn, sid)

        a, row_a, old_map = _snap("2026-09-28T08:00:00", plan_a, 2048)
        b, row_b, new_map = _snap("2026-09-28T09:00:00", plan_b, 4096)
        return a, row_a, old_map, b, row_b, new_map

    # ------------------------------------------------------------ 四路拒绝

    def test_from_rows_rejects_same_plan_on_both_sides(self, conn):
        """① 同 plan 双侧：直调核心曾**照常出包**，核心自身必须拒绝。"""
        _a, row_a, old_map, _b, row_b, new_map = self._pair(
            conn, "plan:A", "plan:A")
        with pytest.raises(ac.AnalysisContractError) as ei:
            ac.build_facts_from_rows(row_a, row_b, old_map, new_map)
        assert ei.value.reason_code == "plan_identity_unsupported"
        msg = str(ei.value)
        # 错误如实说明「本期不支持」新身份，不谎称口径不一致或快照缺失。
        assert "AI" in msg and "plan" in msg.lower() and "不支持" in msg

    @pytest.mark.parametrize("plan_side", ["a", "b"])
    def test_from_rows_rejects_one_sided_plan(self, conn, plan_side):
        """② 单侧带新身份：核心也曾报 dataset_mismatch（失真理由），须改口径。"""
        _a, row_a, old_map, _b, row_b, new_map = self._pair(
            conn, "plan:A" if plan_side == "a" else None,
            "plan:A" if plan_side == "b" else None)
        with pytest.raises(ac.AnalysisContractError) as ei:
            ac.build_facts_from_rows(row_a, row_b, old_map, new_map)
        assert ei.value.reason_code == "plan_identity_unsupported"

    @pytest.mark.parametrize("empty_side", ["both", "a", "b"])
    def test_from_rows_rejects_empty_string_plan_id(self, conn, empty_side):
        """③ ``plan_id`` 空串按「声称了新身份」拒绝，不归一成 legacy 读法。

        口径依据（与既有闸门一致，不另立规则）：判据只有「plan_id 是否为
        NULL」，任何非 NULL 值（含空串）都算声称了新身份
        （``_reject_new_plan_identity``）；``reports.same_dataset`` 同样把空串
        归新身份档——``_row_plan_id`` 只把 NULL 兜底成 None，空串原样留存。
        空串绝不可被读成 legacy：那等于把「声称有身份但身份为空」静默降级成
        旧口径解读并落库。两侧空串是四路里最危险的一路：同身份档内可比，
        核心曾直接接受出包。
        """
        _a, row_a, old_map, _b, row_b, new_map = self._pair(
            conn,
            "" if empty_side in ("both", "a") else None,
            "" if empty_side in ("both", "b") else None)
        with pytest.raises(ac.AnalysisContractError) as ei:
            ac.build_facts_from_rows(row_a, row_b, old_map, new_map)
        assert ei.value.reason_code == "plan_identity_unsupported"

    # ---------------------------------------------- ④ legacy 直调零行为变化

    def test_from_rows_accepts_legacy_null_plan_id(self, conn):
        """④ 两侧 legacy（plan_id IS NULL）：直调核心照常构造事实包。"""
        _a, row_a, old_map, _b, row_b, new_map = self._pair(conn, None, None)
        pkg = ac.build_facts_from_rows(row_a, row_b, old_map, new_map)
        assert pkg.payload["a"]["snapshot_id"] == row_a["id"]
        assert pkg.payload["b"]["snapshot_id"] == row_b["id"]
        assert len(pkg.payload["entries"]) == 1
        assert pkg.payload["entries"][0]["delta_kb"] == 2048  # 2048→4096
        # 事实包身份块仍只有 legacy 三元组：无 plan 身份列（本期不支持）。
        assert "plan_id" not in pkg.payload["dataset"]
        assert pkg.payload["net_delta_kb"] == 0

    # ------------------------------------------------------------ 两入口一致

    def test_package_entry_and_row_core_agree_on_new_plan(self, conn):
        """独立对照：同一对 plan 快照，conn 入口与行构造核心口径必须一致。

        闸门挂在任一入口单侧都会留下旁路——本用例把两个入口钉在同一
        reason_code 上，任一侧回退即红。
        """
        a, row_a, old_map, b, row_b, new_map = self._pair(
            conn, "plan:A", "plan:A")
        with pytest.raises(ac.AnalysisContractError) as via_conn:
            ac.build_facts_package(conn, a, b)
        with pytest.raises(ac.AnalysisContractError) as via_rows:
            ac.build_facts_from_rows(row_a, row_b, old_map, new_map)
        assert via_conn.value.reason_code == "plan_identity_unsupported"
        assert via_rows.value.reason_code == via_conn.value.reason_code
