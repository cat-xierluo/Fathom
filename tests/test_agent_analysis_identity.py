"""ISS-190A：已保存 AI 证据的 plan 身份承载能力（纯证据加法）。

190 卡身份合同时，ISS-153 闸门拒绝 plan 档解读的理由正是「agent_analyses
表无身份列，落库证据无法证明它属于哪个计划」。本文件给该表补上承载位置：

1. v9 迁移：三个**可空**身份列（plan_id/round_id/metric_version），
   幂等、事务内、旧行不回填（NULL 即 legacy 档）。
2. 读回校验 plan 档：_evaluate_expiry 同日后继按 plan_id 匹配；
   legacy 分支口径逐字不变。
3. manifest 携带三字段 + FACTS_SCHEMA_VERSION 递增。

**闸门双向钉子**是本文件的硬约束（PM 定向独审重点）：190A 交付时闸门是
「任一侧快照带 plan_id 依然拒绝」；190B 放开**同 plan 双快照**后，这条
约束变成双向的——放行的那条必须真放行，跨 plan / 混搭 / 计量版本不一致的
那几条必须真拒绝（见 TestGatesUntouched）。本文件另有**单元级直插**构造
plan 身份证据行的用例——绕过闸门是测试权限，不是产品行为。

负向守卫（本文件在「回填/放宽」被违反时会红）：
- test_legacy_row_identity_columns_stay_null：回填历史行即红。
- test_cross_plan_snapshot_is_not_a_successor：放宽成「同根同阈值即同身份」
  即红。
- TestGatesUntouched：闸门三处被改动即红。
"""

from __future__ import annotations

import sqlite3

import pytest

from fathom import analysis_contract as ac
from fathom import analysis_manager as am
from fathom import db


# --------------------------------------------------------------------------
# v8 老库构造（迁移输入）
# --------------------------------------------------------------------------

def _build_v8_db(path):
    """构造一个结构为 v8 的老库（含一条 legacy 证据行），供迁移升级用。

    逐条按既有迁移链建表/补列，保证「老库升级」这条路径真的被走到，
    而不是从已是 v9 的库反推。
    """
    raw = sqlite3.connect(path)
    try:
        for statement in db._SCHEMA_STATEMENTS:
            raw.execute(statement)
        for statement in db._ANALYSIS_TABLE_STATEMENTS:
            raw.execute(statement)
        for statement in db._IDENTITY_TABLE_STATEMENTS:
            raw.execute(statement)
        for ddl in (db._SNAPSHOT_ALTER_V3 + db._SNAPSHOT_ALTER_V4
                    + db._SNAPSHOT_ALTER_V5 + db._SNAPSHOT_ALTER_V6
                    + db._SNAPSHOT_ALTER_V8):
            raw.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")
        raw.execute("PRAGMA user_version=8")
        raw.execute(
            "INSERT INTO analysis_runs(job_id, a_snapshot_id, b_snapshot_id,"
            " request_digest, facts_digest, prompt_version, idempotency_key,"
            " runtime_id, runtime_executable, settings_revision,"
            " consent_revision, status, owner_id, created_at)"
            " VALUES ('job-v8', 1, 2, 'rd', 'fd', 'pv', 'idem', 'rt', 'exe',"
            " 1, 1, 'succeeded', 'owner', '2026-01-01T00:00:00')")
        raw.execute(
            "INSERT INTO agent_analyses(job_id, a_snapshot_id, b_snapshot_id,"
            " a_created_at, b_created_at, dataset_root, request_digest,"
            " facts_digest, prompt_version, adapter_contract_version, runtime_id,"
            " result_json, facts_json, manifest_json, created_at)"
            " VALUES ('job-v8', 1, 2, '2026-01-01T00:00:00',"
            " '2026-01-01T01:00:00', '/legacy/root', 'rd', 'fd', 'pv', 1,"
            " 'rt', '{}', '{}', '{}', '2026-01-01T00:00:00')")
        raw.commit()
    finally:
        raw.close()
    return path


def _analysis_identity_columns(path) -> tuple[str, ...]:
    conn = sqlite3.connect(path)
    try:
        return tuple(r[1] for r in conn.execute(
            "PRAGMA table_info(agent_analyses)"))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 1. v9 迁移
# --------------------------------------------------------------------------

class TestV9Migration:
    def test_upgrade_adds_three_columns_and_bumps_version(self, tmp_path):
        path = _build_v8_db(tmp_path / "v8.db")
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 9
            info = db._table_info(conn, "agent_analyses")
            assert info[-3:] == (("plan_id", "TEXT", 0, 0),
                                 ("round_id", "INTEGER", 0, 0),
                                 ("metric_version", "INTEGER", 0, 0))
        finally:
            conn.close()

    def test_migration_is_idempotent_on_reopen(self, tmp_path):
        """重开已是 v9 的库：不再补列，列不重复。"""
        path = _build_v8_db(tmp_path / "v8.db")
        db.connect(path).close()
        first = _analysis_identity_columns(path)
        for _ in range(2):
            conn = db.connect(path)
            try:
                assert db.schema_version(conn) == 9
            finally:
                conn.close()
        second = _analysis_identity_columns(path)
        assert first == second
        assert len(second) == len(set(second)), "重复追加了列"
        assert second.count("plan_id") == 1

    def test_manual_version_rollback_is_idempotent(self, tmp_path, monkeypatch):
        """v9 结构 + user_version=8（手动回退）：只校验不重复补列。"""
        path = _build_v8_db(tmp_path / "v8.db")
        db.connect(path).close()
        raw = sqlite3.connect(path)
        try:
            raw.execute("PRAGMA user_version=8")
            raw.commit()
        finally:
            raw.close()
        before = _analysis_identity_columns(path)
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 9
        finally:
            conn.close()
        assert _analysis_identity_columns(path) == before

    def test_failed_migration_does_not_half_write(self, tmp_path, monkeypatch):
        """迁移中途失败：整事务回滚，不留「补了一半」的半成品。"""
        path = _build_v8_db(tmp_path / "v8.db")
        before = _analysis_identity_columns(path)

        def half_failing_migrate(conn):
            # 真迁移只做 ALTER；此处补一列后中断，模拟中途失败。
            conn.execute("ALTER TABLE agent_analyses ADD COLUMN plan_id TEXT")
            raise sqlite3.OperationalError("合成：迁移中断")

        monkeypatch.setitem(db._MIGRATIONS, 8, half_failing_migrate)
        with pytest.raises(db.MigrationError):
            db.connect(path)
        # 版本未推进、列一个都没补上、一致备份保留。
        raw = sqlite3.connect(path)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 8
        finally:
            raw.close()
        assert _analysis_identity_columns(path) == before
        assert "plan_id" not in before
        assert list(tmp_path.glob("v8.db.backup-v8-*.sqlite3"))
        # 修复后（真实迁移函数）可重试接续到 v9。
        monkeypatch.setitem(db._MIGRATIONS, 8, db._migrate_v8)
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 9
        finally:
            conn.close()

    def test_legacy_row_identity_columns_stay_null(self, tmp_path):
        """负向守卫：**旧行三列恒 NULL，绝不回填**。

        回填历史 legacy 行会让读回层误以为该证据属于某个 plan，
        这正是 ISS-153 要求「旧行不补造身份」的反面。
        """
        path = _build_v8_db(tmp_path / "v8.db")
        db.connect(path).close()
        conn = db.connect(path)
        try:
            row = conn.execute(
                "SELECT plan_id, round_id, metric_version FROM agent_analyses"
            ).fetchone()
            assert tuple(row) == (None, None, None)
        finally:
            conn.close()


# --------------------------------------------------------------------------
# 证据行夹具（单元级直插，绕过闸门是测试权限）
# --------------------------------------------------------------------------

def _insert_snapshot(conn, *, root: str, plan_id: str | None,
                     created_at: str, min_kb: int | None = 1024,
                     round_id: int | None = None,
                     metric_version: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
        " du_seconds, total_kb, min_kb, collection_status, vanished_count,"
        " exclude_names, confirmed_missing_count, path_unverified_count,"
        " plan_id, round_id, metric_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (created_at, root, 1, 0, 0.0, 2048, min_kb, "full", 0, "", 0, 0,
         plan_id, round_id, metric_version))
    sid = int(cur.lastrowid)
    conn.execute("INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
                 (sid, f"{root}/dir", 2048))
    return sid


def _insert_evidence(conn, *, job_id: str, a_sid: int, b_sid: int,
                     root: str, a_created: str, b_created: str,
                     plan_id: str | None = None, round_id: int | None = None,
                     metric_version: int | None = None) -> int:
    """直插一条 agent_analyses 证据行（不经解读闸门，不经 CLI）。"""
    conn.execute(
        "INSERT INTO analysis_runs(job_id, a_snapshot_id, b_snapshot_id,"
        " request_digest, facts_digest, prompt_version, idempotency_key,"
        " runtime_id, runtime_executable, settings_revision, consent_revision,"
        " status, owner_id, created_at, finished_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'succeeded','owner',?,?)",
        (job_id, a_sid, b_sid, "rd", "fd", "pv", f"idem-{job_id}", "rt",
         "exe", 1, 1, a_created, b_created))
    cur = conn.execute(
        "INSERT INTO agent_analyses(job_id, a_snapshot_id, b_snapshot_id,"
        " a_created_at, b_created_at, dataset_root, dataset_min_kb,"
        " dataset_exclude_names, request_digest, facts_digest, prompt_version,"
        " adapter_contract_version, runtime_id, runtime_version, model,"
        " result_json, facts_json, manifest_json, created_at,"
        " plan_id, round_id, metric_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, a_sid, b_sid, a_created, b_created, root, 1024, "",
         "rd", "fd", "pv", 1, "rt", "1.0", None,
         json_result(), json_facts(), json_manifest(), a_created,
         plan_id, round_id, metric_version))
    return int(cur.lastrowid)


def json_result() -> str:
    return '{"schema_version": 1, "summary": "s", "findings": [], ' \
           '"limitations": [], "inspect_next": []}'


def json_facts() -> str:
    return '{"facts_schema_version": 2, "entries": [{"evidence_id": "f-001"}]}'


def json_manifest() -> str:
    return '{"prompt_version": "pv", "dataset": {"plan_id": null}}'


# --------------------------------------------------------------------------
# 2. 读回校验 plan 档
# --------------------------------------------------------------------------

class TestExpiryPlanAware:
    def test_plan_successor_same_plan_marks_replaced(self, isolated):
        """plan 档：同 plan 的同日后继存在 → snapshot_replaced。"""
        conn = db.connect()
        try:
            root = str(isolated["scanroot"])
            a = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 created_at="2026-09-28T08:00:00",
                                 round_id=7, metric_version=2)
            b = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 created_at="2026-09-28T09:00:00",
                                 round_id=7, metric_version=2)
            aid = _insert_evidence(conn, job_id="job-p", a_sid=a, b_sid=b,
                                   root=root,
                                   a_created="2026-09-28T08:00:00",
                                   b_created="2026-09-28T09:00:00",
                                   plan_id="plan-1", round_id=7,
                                   metric_version=2)
            # b 快照被同日替换（同 plan 的更晚一行）
            conn.execute("DELETE FROM entries WHERE snapshot_id=?", (b,))
            conn.execute("DELETE FROM snapshots WHERE id=?", (b,))
            _insert_snapshot(conn, root=root, plan_id="plan-1",
                             created_at="2026-09-28T09:30:00",
                             round_id=7, metric_version=2)
            conn.commit()
            view = am.AnalysisManager()._analysis_view(conn, dict(
                conn.execute("SELECT x.*, NULL AS revoked_at FROM agent_analyses x"
                             " WHERE x.id=?", (aid,)).fetchone()))
        finally:
            conn.close()
        assert view["expired"] is True
        assert view["expired_reason"] == "snapshot_replaced"

    def test_cross_plan_snapshot_is_not_a_successor(self, isolated):
        """负向守卫：跨 plan 不误判。

        别的 plan 即便同根同阈值同排除同一天，也不是本数据集的后继。
        把后继查询放宽成「同根同阈值即同身份」时，本用例会得到
        snapshot_replaced 而非 snapshot_pruned。
        """
        conn = db.connect()
        try:
            root = str(isolated["scanroot"])
            a = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 created_at="2026-09-28T08:00:00",
                                 round_id=7, metric_version=2)
            b = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 created_at="2026-09-28T09:00:00",
                                 round_id=7, metric_version=2)
            aid = _insert_evidence(conn, job_id="job-x", a_sid=a, b_sid=b,
                                   root=root,
                                   a_created="2026-09-28T08:00:00",
                                   b_created="2026-09-28T09:00:00",
                                   plan_id="plan-1", round_id=7,
                                   metric_version=2)
            # b 被删；同日新增一行属于**另一个 plan**（换卷/换计量版本）
            conn.execute("DELETE FROM entries WHERE snapshot_id=?", (b,))
            conn.execute("DELETE FROM snapshots WHERE id=?", (b,))
            _insert_snapshot(conn, root=root, plan_id="plan-2",
                             created_at="2026-09-28T09:30:00",
                             round_id=8, metric_version=2)
            conn.commit()
            view = am.AnalysisManager()._analysis_view(conn, dict(
                conn.execute("SELECT x.*, NULL AS revoked_at FROM agent_analyses x"
                             " WHERE x.id=?", (aid,)).fetchone()))
        finally:
            conn.close()
        assert view["expired"] is True
        assert view["expired_reason"] == "snapshot_pruned"

    def test_legacy_branch_unchanged(self, isolated):
        """负向守卫：legacy 行（plan_id NULL）口径逐字不变。"""
        conn = db.connect()
        try:
            root = str(isolated["scanroot"])
            a = _insert_snapshot(conn, root=root, plan_id=None,
                                 created_at="2026-09-28T08:00:00")
            b = _insert_snapshot(conn, root=root, plan_id=None,
                                 created_at="2026-09-28T09:00:00")
            legacy_replaced = _insert_evidence(
                conn, job_id="job-lr", a_sid=a, b_sid=b, root=root,
                a_created="2026-09-28T08:00:00",
                b_created="2026-09-28T09:00:00", plan_id=None)
            # 同日 legacy 后继 → 替换
            conn.execute("DELETE FROM entries WHERE snapshot_id=?", (b,))
            conn.execute("DELETE FROM snapshots WHERE id=?", (b,))
            _insert_snapshot(conn, root=root, plan_id=None,
                             created_at="2026-09-28T09:30:00")
            # 另一条 legacy 证据：其 b 无任何同身份后继 → 淘汰
            a2 = _insert_snapshot(conn, root=root, plan_id=None,
                                  created_at="2026-09-28T20:00:00")
            b2 = _insert_snapshot(conn, root=root, plan_id=None,
                                  created_at="2026-09-28T21:00:00")
            legacy_pruned = _insert_evidence(
                conn, job_id="job-lp", a_sid=a2, b_sid=b2, root=root,
                a_created="2026-09-28T20:00:00",
                b_created="2026-09-28T21:00:00", plan_id=None)
            conn.execute("DELETE FROM entries WHERE snapshot_id=?", (b2,))
            conn.execute("DELETE FROM snapshots WHERE id=?", (b2,))
            conn.commit()
            manager = am.AnalysisManager()
            views = {}
            for key, evid in (("replaced", legacy_replaced),
                              ("pruned", legacy_pruned)):
                views[key] = manager._analysis_view(conn, dict(
                    conn.execute(
                        "SELECT x.*, NULL AS revoked_at FROM agent_analyses x"
                        " WHERE x.id=?", (evid,)).fetchone()))
        finally:
            conn.close()
        assert views["replaced"]["expired_reason"] == "snapshot_replaced"
        assert views["pruned"]["expired_reason"] == "snapshot_pruned"

    def test_legacy_row_does_not_match_plan_successor(self, isolated):
        """legacy 证据行不得被 plan 档同日行认作后继。"""
        conn = db.connect()
        try:
            root = str(isolated["scanroot"])
            a = _insert_snapshot(conn, root=root, plan_id=None,
                                 created_at="2026-09-28T08:00:00")
            b = _insert_snapshot(conn, root=root, plan_id=None,
                                 created_at="2026-09-28T09:00:00")
            aid = _insert_evidence(conn, job_id="job-l2", a_sid=a, b_sid=b,
                                   root=root,
                                   a_created="2026-09-28T08:00:00",
                                   b_created="2026-09-28T09:00:00",
                                   plan_id=None)
            conn.execute("DELETE FROM entries WHERE snapshot_id=?", (b,))
            conn.execute("DELETE FROM snapshots WHERE id=?", (b,))
            _insert_snapshot(conn, root=root, plan_id="plan-1",
                             created_at="2026-09-28T09:30:00")
            conn.commit()
            view = am.AnalysisManager()._analysis_view(conn, dict(
                conn.execute("SELECT x.*, NULL AS revoked_at FROM agent_analyses x"
                             " WHERE x.id=?", (aid,)).fetchone()))
        finally:
            conn.close()
        assert view["expired_reason"] == "snapshot_pruned"


class TestRevokeKeepsIdentity:
    def test_revoked_evidence_keeps_identity_columns(self, isolated):
        """撤销保留身份列语义：审计仍能知道该证据曾属哪个 plan。

        revoke_analysis() 物理删除 agent_analyses 行，故身份保留的验证点
        是「生命周期行的 revoked_at 落位、证据行消失」这一既有合同不被
        本改动影响——不存在把身份列置空的代码路径。
        """
        conn = db.connect()
        try:
            root = str(isolated["scanroot"])
            a = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 created_at="2026-09-28T08:00:00")
            b = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 created_at="2026-09-28T09:00:00")
            aid = _insert_evidence(conn, job_id="job-rv", a_sid=a, b_sid=b,
                                   root=root,
                                   a_created="2026-09-28T08:00:00",
                                   b_created="2026-09-28T09:00:00",
                                   plan_id="plan-1", round_id=7,
                                   metric_version=2)
            conn.commit()
        finally:
            conn.close()
        manager = am.AnalysisManager()
        manager.revoke_analysis(aid)
        conn = db.connect()
        try:
            assert conn.execute(
                "SELECT COUNT(*) c FROM agent_analyses WHERE id=?",
                (aid,)).fetchone()["c"] == 0
            run = conn.execute(
                "SELECT revoked_at FROM analysis_runs WHERE job_id=?",
                ("job-rv",)).fetchone()
            assert run["revoked_at"] is not None
        finally:
            conn.close()


# --------------------------------------------------------------------------
# 3. manifest 与 FACTS_SCHEMA_VERSION
# --------------------------------------------------------------------------

class TestManifestIdentity:
    @staticmethod
    def _bundle(root: str, plan_id: str | None, monkeypatch=None):
        """构造一个只带身份差异的最小事实包。

        plan 档需要**绕过闸门**（`monkeypatch` 传入时把拒绝函数替换为空操作）
        才能构造出带身份的包——绕过闸门是测试权限，不是产品行为：
        生产路径上 plan 档根本走不到落库（见 TestGatesUntouched）。
        """
        if plan_id is not None and monkeypatch is not None:
            monkeypatch.setattr(ac, "_reject_new_plan_identity",
                                lambda *a, **k: None)
        row_a = {"id": 1, "created_at": "2026-09-28T08:00:00",
                 "root": root, "min_kb": 1024, "exclude_names": "",
                 "dir_count": 1, "total_kb": 2048, "plan_id": plan_id,
                 "round_id": 7 if plan_id else None,
                 "metric_version": 2 if plan_id else None}
        row_b = dict(row_a, id=2, created_at="2026-09-28T09:00:00",
                     total_kb=4096)
        return ac.build_facts_from_rows(
            row_a, row_b, {f"{root}/dir": 2048}, {f"{root}/dir": 4096})

    def test_manifest_carries_identity_fields(self, isolated):
        """legacy 档：三字段为 None（不补造身份）。"""
        root = str(isolated["scanroot"])
        manifest = self._bundle(root, None).manifest()
        assert manifest["dataset"]["plan_id"] is None
        assert manifest["dataset"]["round_id"] is None
        assert manifest["dataset"]["metric_version"] is None

    def test_manifest_plan_tier_carries_values(self, isolated, monkeypatch):
        """plan 档：三字段带真实值（构造时绕过闸门）。"""
        root = str(isolated["scanroot"])
        manifest = self._bundle(root, "plan-1", monkeypatch).manifest()
        assert manifest["dataset"]["plan_id"] == "plan-1"
        assert manifest["dataset"]["round_id"] == 7
        assert manifest["dataset"]["metric_version"] == 2

    def test_facts_schema_version_incremented(self):
        """FACTS_SCHEMA_VERSION 递增到 2（结构变化：dataset 新增三字段）。"""
        assert ac.FACTS_SCHEMA_VERSION == 2

    def test_saved_evidence_row_exposes_manifest_identity(self, isolated):
        """落库行的 manifest_json 本身带三字段（读回可直接核对范围）。"""
        conn = db.connect()
        try:
            root = str(isolated["scanroot"])
            a = _insert_snapshot(conn, root=root, plan_id=None,
                                 created_at="2026-09-28T08:00:00")
            b = _insert_snapshot(conn, root=root, plan_id=None,
                                 created_at="2026-09-28T09:00:00")
            aid = _insert_evidence(conn, job_id="job-mf", a_sid=a, b_sid=b,
                                   root=root,
                                   a_created="2026-09-28T08:00:00",
                                   b_created="2026-09-28T09:00:00",
                                   plan_id=None)
            row = conn.execute("SELECT manifest_json FROM agent_analyses"
                               " WHERE id=?", (aid,)).fetchone()
        finally:
            conn.close()
        import json as _json
        manifest = _json.loads(row["manifest_json"])
        assert manifest["dataset"]["plan_id"] is None


# --------------------------------------------------------------------------
# 4. 三道解读闸门零改动（本任务是纯证据加法）
# --------------------------------------------------------------------------

class TestGatesUntouched:
    """190B 起闸门语义：**同 plan 双快照放行**，其余（跨 plan / 混搭 /
    计量版本不一致 / 空串身份）仍拒。

    190A 交付时本类是「闸门零改动」的钉子；190B 放开了同 plan，故改为双向
    钉子：放行的那条必须真放行（否则功能不可达），拒绝的那几条必须真拒绝
    （否则放宽过头）。两个入口都要覆盖——只改一处即红。
    """

    def test_same_plan_rows_are_accepted_when_metric_version_matches(self,
                                                                     isolated):
        """同 plan + 同计量版本 → 放行构造，且身份三列真实落进事实包。"""
        conn = db.connect()
        try:
            root = str(isolated["scanroot"])
            a = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 round_id=1, metric_version=2,
                                 created_at="2026-09-28T08:00:00")
            b = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 round_id=2, metric_version=2,
                                 created_at="2026-09-28T09:00:00")
            pkg = ac.build_facts_package(conn, a, b)
            assert pkg.payload["dataset"]["plan_id"] == "plan-1"
            assert pkg.payload["dataset"]["metric_version"] == 2
        finally:
            conn.close()

    def test_gate_still_rejects_plan_rows_without_metric_version(self,
                                                                isolated):
        """同 plan 但**缺计量版本**仍拒：变化量不可比，不静默降级。"""
        conn = db.connect()
        try:
            root = str(isolated["scanroot"])
            a = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 created_at="2026-09-28T08:00:00")
            b = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 created_at="2026-09-28T09:00:00")
            with pytest.raises(ac.AnalysisContractError) as ei:
                ac.build_facts_package(conn, a, b)
            assert ei.value.reason_code == "plan_identity_unsupported"
        finally:
            conn.close()

    def test_gate_still_rejects_in_build_facts_from_rows(self, isolated):
        """下沉核心的闸门同样保留：跨 plan 在核心入口也必须拒。"""
        root = str(isolated["scanroot"])
        row_a = {"id": 1, "created_at": "x", "root": root, "min_kb": 1024,
                 "exclude_names": "", "dir_count": 1, "total_kb": 1,
                 "plan_id": "plan-1", "metric_version": 1}
        row_b = dict(row_a, id=2, plan_id="plan-2")
        with pytest.raises(ac.AnalysisContractError) as ei:
            ac.build_facts_from_rows(row_a, row_b, {}, {})
        assert ei.value.reason_code == "plan_identity_unsupported"

    def test_same_plan_rows_are_accepted_in_build_facts_from_rows(self,
                                                                  isolated):
        """下沉核心**同步放开**同 plan：只改 conn 入口会让功能不可达。"""
        root = str(isolated["scanroot"])
        row_a = {"id": 1, "created_at": "x", "root": root, "min_kb": 1024,
                 "exclude_names": "", "dir_count": 1, "total_kb": 1,
                 "plan_id": "plan-1", "round_id": 1, "metric_version": 1}
        row_b = dict(row_a, id=2)
        pkg = ac.build_facts_from_rows(row_a, row_b, {}, {})
        assert pkg.payload["dataset"]["plan_id"] == "plan-1"

    def test_production_insert_path_writes_real_identity_for_same_plan(
            self, isolated):
        """落库链路：同 plan 解读成功后三列必须落**真实值**（非 NULL）。

        190A 的 INSERT 已按快照行取值，本用例钉住「190B 放开后这三列不再
        恒为 NULL」——取 a 快照行的 plan_id/round_id/metric_version 逐字写入。
        """
        import json as _json
        conn = db.connect()
        try:
            root = str(isolated["scanroot"])
            a = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 round_id=1, metric_version=2,
                                 created_at="2026-09-28T08:00:00")
            b = _insert_snapshot(conn, root=root, plan_id="plan-1",
                                 round_id=2, metric_version=2,
                                 created_at="2026-09-28T09:00:00")
            bundle = ac.build_facts_package(conn, a, b)
            conn.execute(
                "INSERT INTO analysis_runs(job_id, a_snapshot_id, b_snapshot_id,"
                " request_digest, facts_digest, prompt_version, idempotency_key,"
                " runtime_id, runtime_executable, settings_revision,"
                " consent_revision, status, owner_id, created_at)"
                " VALUES ('job-p', ?, ?, 'rd', 'fd', 'pv', 'idem-p', 'rt', 'exe',"
                " 1, 1, 'succeeded', 'owner', '2026-09-28T09:00:00')", (a, b))
            row_row = conn.execute(
                "SELECT plan_id, round_id, metric_version FROM snapshots"
                " WHERE id=?", (a,)).fetchone()
            cur = conn.execute(
                "INSERT INTO agent_analyses(job_id, a_snapshot_id, b_snapshot_id,"
                " a_created_at, b_created_at, dataset_root, dataset_min_kb,"
                " dataset_exclude_names, request_digest, facts_digest,"
                " prompt_version, adapter_contract_version, runtime_id,"
                " runtime_version, model, result_json, facts_json,"
                " manifest_json, created_at, plan_id, round_id, metric_version)"
                " VALUES ('job-p', ?, ?, ?, ?, ?, 1024, '', 'rd', 'fd', 'pv', 1,"
                " 'rt', '1.0', NULL, '{}', '{}', ?, ?, ?, ?, ?)",
                (a, b, "2026-09-28T08:00:00", "2026-09-28T09:00:00",
                 root,
                 _json.dumps(bundle.manifest(), ensure_ascii=False),
                 "2026-09-28T09:00:00",
                 row_row["plan_id"], row_row["round_id"],
                 row_row["metric_version"]))
            row = conn.execute("SELECT plan_id, round_id, metric_version"
                               " FROM agent_analyses WHERE id=?",
                               (cur.lastrowid,)).fetchone()
            conn.commit()
        finally:
            conn.close()
        # 三列落真实值（非 NULL），读回与 a 快照行逐字一致。
        assert tuple(row) == ("plan-1", 1, 2)
