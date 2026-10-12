"""ISS-154：一轮多范围扫描协调与部分成功恢复——定向测试。

任务卡五份合同，每份先红后绿：

1. 一轮多范围：同一 owner/跨进程 flock 协调一轮**去重**范围计划；默认按
   范围顺序采集；保留 ``-x`` 跨卷隔离与每范围时限；整轮有界时限 + 取消点；
   CLI 显式多范围消费者与旧 ``--root`` 兼容；**运行中换配置只影响下一轮**。
2. 部分成功语义：每成功范围提交自己的新有效快照；失败范围保留旧样本和
   旧时间但**不当本轮成功**；轮次状态 full/partial/failed/cancelled 与成员
   阶段同源；披露时间跨度；不把不同时间成员拼成原子时点、不伪造百分比。
3. 容量采样接线：每轮实际调用 ISS-152 的容量读取（storage.py 只读消费），
   按 ISS-153 样本表存容器/卷样本与来源/时间；轮末至少一条独立容量样本；
   采样失败保持未知/旧时间，**不拿目录 statvfs 替补**。
4. 报告/通知：轮次汇总 + 每范围可比依据；部分成功**不得**发「整盘扫描全部
   完成」类通知；报告命名/关联不覆盖同日期别的范围；通知阈值针对正确容量
   主体；升级停写/跨进程互斥仍覆盖整轮。
5. 取消/竞态：SIGTERM/超时只回收本轮自有进程；取消落在首/中/末范围各态
   正确；spawn 竞态沿用 ``tests/test_scan_coordination.py`` 的注入手法
   （替 ``scanner.subprocess.Popen``）。

全部合成数据（tmp_path 下的 /synthetic 根），经 conftest.isolated 隔离
运行根；不触发真实 HOME 扫描、不写生产库。容量发现用合成 ``StorageDiscovery``
对象注入，**不**在测试里真调 diskutil。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import pathlib
import sqlite3
import subprocess

import pytest

from fathom import (
    config, db, notify, reports, scan_coordinator, scanner, storage,
)


# ── 合成夹具 ──


def _make_root(base: pathlib.Path, name: str, *, dirs: int = 2,
               files: int = 3) -> pathlib.Path:
    """建一个小的合成根（有真实内容，让真实 du 采得到）。"""
    root = base / name
    (root / "sub").mkdir(parents=True)
    for index in range(dirs):
        sub = root / f"d{index}"
        sub.mkdir()
        for file_index in range(files):
            (sub / f"f{file_index}.bin").write_bytes(b"x" * 2048)
    (root / "top.bin").write_bytes(b"y" * 4096)
    return root


def _synthetic_discovery(
    *, free_bytes: int | None, total_bytes: int | None = 500 * 1024**3,
    with_container: bool = True,
) -> storage.StorageDiscovery:
    """合成一次发现结果（ISS-152 v1 对象），不触碰真机 diskutil。

    卷级**不**给 free（同容器共享 free 是唯一权威）；容器给 shared_free。
    """
    container = None
    volumes: tuple = ()
    if with_container:
        container = storage.DiscoveredContainer(
            schema=storage.CONTAINER_SCHEMA, version=storage.DISCOVERY_VERSION,
            container_id="apfs-container:11111111-2222-3333-4444-555555555555",
            container_reference="disk9",
            capacity_ceiling_bytes=total_bytes,
            shared_free_bytes=free_bytes,
            physical_store_ids=("disk9s1",),
            source=("diskutil-apfs-list",),
        )
        volumes = (
            storage.DiscoveredVolume(
                schema=storage.VOLUME_SCHEMA, version=storage.DISCOVERY_VERSION,
                volume_id="apfs-volume:AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
                container_id=container.container_id,
                volume_group_id="apfs-vg:9999", name="FathomSynth", roles=("data",),
                device_identifier="disk9s2", mount_point="/synthetic/root",
                status="accessible", filevault=False,
                capacity_in_use_bytes=120 * 1024**3, sources=("diskutil-apfs-list",),
            ),
        )
    return storage.StorageDiscovery(
        schema=storage.DISCOVERY_SCHEMA, version=storage.DISCOVERY_VERSION,
        status=storage.DiscoveryStatus.OK, generated_at="2026-10-05T00:00:00+00:00",
        platform="darwin", startup_container=container, startup_volumes=volumes,
        visible_entries=(), other_devices=(), errors=(), source_commands=(),
    )


@pytest.fixture(autouse=True)
def _no_notifications(monkeypatch):
    """记录通知调用（不真的弹 osascript），并对合成根用小阈值。"""
    sent: list[tuple[str, str, str | None]] = []

    def _record(title, body, sound=None):
        sent.append((title, body, sound))
        return True

    monkeypatch.setattr(notify, "send_notification", _record)
    monkeypatch.setattr(config, "MIN_DIR_KB", 1)
    return sent


@pytest.fixture
def roots(tmp_path):
    """两个合成根 A / B（供多范围轮次使用）。"""
    return {
        "a": _make_root(tmp_path / "synthetic", "root_a"),
        "b": _make_root(tmp_path / "synthetic", "root_b"),
    }


@pytest.fixture
def round_env(isolated, monkeypatch):
    """多范围轮的公共环境：钉住超时、容量发现注入。

    注入点选在 **``storage.discover_startup``**（发现层）而不是
    ``scan_coordinator.read_capacity_readings``（消费层）：这样每个用例
    都真正走过生产接线 read_capacity_readings → storage.discover_startup
    → capacity_readings_from_discovery，而不是把消费层整个替掉。
    """
    monkeypatch.setattr(config, "DU_TIMEOUT_S", 60.0)
    monkeypatch.setattr(config, "FREE_ALERT_GB", 10)
    monkeypatch.setattr(
        storage, "discover_startup",
        lambda *a, **k: _synthetic_discovery(free_bytes=90 * 1024**3),
    )
    return isolated


def _specs(roots: dict, **kwargs) -> list[scan_coordinator.ScopeSpec]:
    """两个合成 path 型范围规格（明确不是卷身份）。"""
    return [
        scan_coordinator.ScopeSpec.from_path(roots["a"], **kwargs),
        scan_coordinator.ScopeSpec.from_path(roots["b"], **kwargs),
    ]


def _round_row(conn, round_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM scan_rounds WHERE id=?", (round_id,)
    ).fetchone()


def _members(conn, round_id: int) -> dict[int, sqlite3.Row]:
    return {
        row["seq"]: row for row in conn.execute(
            "SELECT * FROM scan_round_members WHERE round_id=? ORDER BY seq",
            (round_id,),
        )
    }


def _snapshots_of(conn, root: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshots WHERE root=? ORDER BY id", (root,)
    ).fetchall()


def _failing_roots(monkeypatch, targets: set[str]):
    """让指定根的采集确定性失败，其余走真实 du（沿用既有注入手法）。"""
    real_run_du = scanner.run_du

    def shim(root):
        if str(root) in targets:
            raise scanner.InvalidScanError("合成故障：该范围采集无效")
        return real_run_du(root)

    monkeypatch.setattr(scanner, "run_du", shim)


# ── 合同 1：一轮多范围 ──


class TestOneRoundManyScopes:
    def test_round_collects_each_scope_and_commits_own_snapshot(self, round_env, roots):
        run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        conn = db.connect()
        try:
            round_id = result["round_id"]
            assert result["round_status"] == "full"
            assert result["scopes"] == 2
            assert len(result["snapshot_ids"]) == 2
            for key in ("a", "b"):
                rows = _snapshots_of(conn, str(roots[key]))
                assert len(rows) == 1, f"{key} 应有自己的新快照"
                assert rows[0]["round_id"] == round_id
                assert rows[0]["plan_id"], "成员快照必须带计划身份"
                assert rows[0]["metric_version"] == scan_coordinator.METRIC_VERSION
            members = _members(conn, round_id)
            assert set(members) == {0, 1}
            assert all(m["status"] == "done" for m in members.values())
            assert all(m["snapshot_id"] is not None for m in members.values())
            assert all(m["snapshot_status"] == "active" for m in members.values())
        finally:
            conn.close()

    def test_scopes_collected_in_given_order(self, round_env, roots):
        ordered = [
            scan_coordinator.ScopeSpec.from_path(roots["b"]),
            scan_coordinator.ScopeSpec.from_path(roots["a"]),
        ]
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=ordered)
        conn = db.connect()
        try:
            members = _members(conn, result["round_id"])
            roots_by_seq = {
                seq: conn.execute(
                    "SELECT canonical_root FROM scan_plans WHERE plan_id=?",
                    (row["plan_id"],),
                ).fetchone()["canonical_root"]
                for seq, row in members.items()
            }
            assert list(roots_by_seq.values()) == [str(roots["b"]), str(roots["a"])]
        finally:
            conn.close()

    def test_duplicate_scope_deduped_into_one_member(self, round_env, roots):
        specs = _specs(roots) + [scan_coordinator.ScopeSpec.from_path(roots["a"])]
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=specs)
        assert result["scopes"] == 2, "重复范围只采一次"
        conn = db.connect()
        try:
            conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE root=?", (str(roots["a"]),)
            ).fetchone()
            assert len(_snapshots_of(conn, str(roots["a"]))) == 1
        finally:
            conn.close()

    def test_same_root_two_identities_stay_separate_datasets(self, round_env, roots):
        """同路径不同范围身份 = 不同数据集（ISS-153 合同），不能被去重合并。"""
        specs = [
            scan_coordinator.ScopeSpec.from_path(roots["a"], scope_id="fixture-a"),
            scan_coordinator.ScopeSpec.from_path(roots["a"], scope_id="fixture-b"),
        ]
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=specs)
        assert result["scopes"] == 2
        conn = db.connect()
        try:
            plans = conn.execute(
                "SELECT plan_id, scope_id FROM scan_plans ORDER BY scope_id"
            ).fetchall()
            assert {p["scope_id"] for p in plans} == {"fixture-a", "fixture-b"}
            assert len({p["plan_id"] for p in plans}) == 2
        finally:
            conn.close()

    def test_each_scope_keeps_cross_volume_flag(self, round_env, roots):
        """``-x`` 跨卷隔离必须逐范围保留：默认 argv 仍含 -xk。"""
        captured: list[list[str]] = []
        real_argv = scanner._du_argv

        def spy(root):
            argv = real_argv(root)
            captured.append(argv)
            return argv

        scanner._du_argv = spy
        try:
            scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        finally:
            scanner._du_argv = real_argv
        assert len(captured) == 2
        for argv in captured:
            assert "-xk" in argv, f"du argv 必须保留 -x 跨卷隔离：{argv}"

    def test_per_scope_timeout_applies_to_each_scope(self, round_env, roots, monkeypatch):
        """每范围时限逐个生效：首个范围超时不吃掉后一个范围的时限。"""
        seen: list[float] = []
        real_ctx = scanner.du_process_context

        def spy_ctx(**kwargs):
            seen.append(kwargs["timeout_seconds"])
            return real_ctx(**kwargs)

        monkeypatch.setattr(scanner, "du_process_context", spy_ctx)
        scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert len(seen) == 2
        assert all(value == 60.0 for value in seen)

    def test_round_deadline_marks_unstarted_scopes_skipped(self, round_env, roots):
        """整轮有界时限到：未开始成员记 skipped，轮次 cancelled，已提交保留。"""
        calls: list[str] = []
        real_run_du = scanner.run_du

        def shim(root):
            calls.append(str(root))
            return real_run_du(root)

        scanner.run_du = shim
        try:
            with pytest.raises(scan_coordinator.ScanRoundTimeoutError):
                scan_coordinator.run_scan(
                    source="cli", scopes=_specs(roots), round_timeout_seconds=0.0,
                )
        finally:
            scanner.run_du = real_run_du
        assert calls == [], "时限为 0 时不应进入任何范围的采集"
        conn = db.connect()
        try:
            round_id = conn.execute(
                "SELECT id FROM scan_rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            assert _round_row(conn, round_id)["status"] == "cancelled"
            statuses = [m["status"] for m in _members(conn, round_id).values()]
            assert statuses == ["skipped", "skipped"]
            assert conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE round_id=?", (round_id,)
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_config_change_mid_round_only_affects_next_round(self, round_env, roots, monkeypatch):
        """运行中换配置只影响下一轮：本轮成员的阈值/掩码/argv 不变。"""
        seen_argv: list[list[str]] = []
        real_argv = scanner._du_argv
        real_run_du = scanner.run_du

        def spy_argv(root):
            argv = real_argv(root)
            # 只比较与根无关的开关部分：argv 里本来就含各自不同的根路径，
            # 比整条 argv 必然不等，测不出「配置有没有被中途改写」。
            seen_argv.append([a for a in argv if a != str(root)])
            return argv

        def shim(root):
            result = real_run_du(root)
            # 第一个范围采完后「运行中」改配置：改阈值、改掩码。
            config.MIN_DIR_KB = 999_999_999
            config.EXCLUDE_NAMES = ["*.MIDROUND"]
            return result

        monkeypatch.setattr(scanner, "_du_argv", spy_argv)
        monkeypatch.setattr(scanner, "run_du", shim)
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert result["round_status"] == "full"
        # 两个范围的 argv 完全一致（掩码没被中途改写）。
        assert len(seen_argv) == 2
        assert seen_argv[0] == seen_argv[1]
        assert not any("MIDROUND" in arg for arg in seen_argv)
        conn = db.connect()
        try:
            plans = conn.execute(
                "SELECT min_kb, exclude_names FROM scan_plans"
            ).fetchall()
            assert {p["min_kb"] for p in plans} == {1}, "本轮阈值钉住为 1"
            assert {p["exclude_names"] for p in plans} == {""}
            snaps = conn.execute(
                "SELECT DISTINCT min_kb FROM snapshots WHERE round_id=?",
                (result["round_id"],),
            ).fetchall()
            assert {s["min_kb"] for s in snaps} == {1}
        finally:
            conn.close()

    def test_legacy_single_root_path_untouched_by_round_changes(self, round_env, roots, monkeypatch):
        """旧 ``--root`` 单范围入口不受多范围影响：不产轮次行，命名仍是日期。"""
        run_id, result = scan_coordinator.run_scan(source="cli", root=roots["a"])
        assert "round_id" not in result
        assert result["snapshot_id"] > 0
        conn = db.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM scan_rounds").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM scan_round_members"
            ).fetchone()[0] == 0
            snap = conn.execute(
                "SELECT * FROM snapshots WHERE id=?", (result["snapshot_id"],)
            ).fetchone()
            assert snap["plan_id"] is None, "legacy 路径身份列保持 NULL"
            assert snap["round_id"] is None
            if result["report"]:
                assert result["report"].name == f"{snap['created_at'][:10]}.md"
        finally:
            conn.close()

    def test_scopes_and_root_together_is_rejected(self, round_env, roots):
        with pytest.raises(scan_coordinator.ScanScopeError):
            scan_coordinator.start_scan(
                source="cli", root=roots["a"], scopes=_specs(roots)
            )

    def test_relative_scope_path_is_rejected(self, round_env):
        with pytest.raises(scan_coordinator.ScanScopeError):
            scan_coordinator.ScopeSpec.from_path("relative/path")

    def test_duplicate_spec_keeps_ordinal_of_root(self, round_env, roots):
        specs = _specs(roots)
        conn = db.connect()
        try:
            plan = scan_coordinator.build_round_plan(
                conn, specs,
                pinned=scanner.PinnedScanConfig.capture(
                    metric_version=scan_coordinator.METRIC_VERSION),
            )
            assert plan.ordinal_of[str(roots["a"])] == 0
            assert plan.ordinal_of[str(roots["b"])] == 1
        finally:
            conn.close()

    def test_cross_process_lease_still_guards_whole_round(self, round_env, roots, tmp_path):
        """互斥覆盖整轮：轮次运行期间另一个进程拿不到租约。"""
        lock = scan_coordinator._lock_path()
        holder = scan_coordinator.ScanLease.acquire(lock, source="cli")
        try:
            with pytest.raises(scan_coordinator.ScanBusyError):
                scan_coordinator.run_scan(source="api", scopes=_specs(roots))
            conn = db.connect()
            try:
                assert conn.execute(
                    "SELECT COUNT(*) FROM scan_rounds"
                ).fetchone()[0] == 0
            finally:
                conn.close()
        finally:
            holder.release()

    def test_upgrade_write_stop_refuses_round_before_any_scope(self, round_env, roots, monkeypatch):
        """停写条件覆盖整轮：不建轮次行、不进任何范围。"""
        from fathom import upgrade
        runtime = pathlib.Path(config.DB_PATH).parent
        runtime.mkdir(parents=True, exist_ok=True)
        journal = upgrade.journal_path_from_db(config.DB_PATH)
        journal.write_text('{"phase": "prepare", "txn_id": "t1"}')
        try:
            with pytest.raises(scan_coordinator.UpgradeWriteStopError):
                scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
            conn = db.connect()
            try:
                assert conn.execute(
                    "SELECT COUNT(*) FROM scan_rounds"
                ).fetchone()[0] == 0
            finally:
                conn.close()
        finally:
            journal.unlink()


# ── 合同 2：部分成功语义 ──


class TestPartialSuccess:
    def test_failed_scope_keeps_old_sample_and_time_not_counted_as_success(
        self, round_env, roots, monkeypatch
    ):
        """任务卡反例：A 成功 B 失败 → B 旧数据不覆盖、时间不冒充当前。"""
        conn = db.connect()
        try:
            # 先给 B 种一份**昨天**的样本（昨天时间必须保持）。
            scanner.ensure_scan_scope(
                conn, scan_coordinator.derived_scope_id(str(roots["b"])), "path",
                mount_path=str(roots["b"]),
            )
            plan_b = scanner.ensure_scan_plan(
                conn, scan_coordinator.derived_scope_id(str(roots["b"])),
                str(roots["b"]), scan_coordinator.METRIC_VERSION, 1, "",
            )
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb, min_kb, collection_status, "
                "vanished_count, exclude_names, confirmed_missing_count, "
                "path_unverified_count, plan_id, round_id, metric_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("2026-09-01T10:00:00", str(roots["b"]), 2, 0, 0.5, 4096, 1,
                 "full", 0, "", 0, 0, plan_b, None,
                 scan_coordinator.METRIC_VERSION),
            )
            conn.commit()
        finally:
            conn.close()

        _failing_roots(monkeypatch, {str(roots["b"])})
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))

        assert result["round_status"] == "partial"
        conn = db.connect()
        try:
            # A：有自己的新快照（今天的）。
            rows_a = _snapshots_of(conn, str(roots["a"]))
            assert len(rows_a) == 1
            assert rows_a[0]["created_at"][:10] != "2026-09-01"
            # B：旧样本仍在，且**时间仍是昨天**（没有被冒充成当前）。
            rows_b = _snapshots_of(conn, str(roots["b"]))
            assert len(rows_b) == 1
            assert rows_b[0]["created_at"] == "2026-09-01T10:00:00"
            # 失败成员不挂快照引用。
            members = _members(conn, result["round_id"])
            assert members[0]["status"] == "done"
            assert members[1]["status"] == "failed"
            assert members[1]["snapshot_id"] is None
        finally:
            conn.close()

    def test_all_scopes_failed_yields_failed_round(self, round_env, roots, monkeypatch):
        _failing_roots(monkeypatch, {str(roots["a"]), str(roots["b"])})
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert result["round_status"] == "failed"
        conn = db.connect()
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE round_id=?",
                (result["round_id"],),
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_round_status_is_derived_from_member_phases(self, round_env, roots, monkeypatch):
        """全局状态与成员阶段同源：改成员行，轮次状态推导随之改变。"""
        _failing_roots(monkeypatch, {str(roots["b"])})
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        conn = db.connect()
        try:
            counts = scan_coordinator.round_member_statuses(
                conn, result["round_id"]
            )
            assert counts == {"done": 1, "failed": 1}
            assert scan_coordinator.derive_round_status(counts) == "partial"
            assert _round_row(conn, result["round_id"])["status"] == "partial"
            # 阶段与全局状态确实同源：把 failed 改成 done 之后推导变 full。
            conn.execute(
                "UPDATE scan_round_members SET status='done' WHERE round_id=? "
                "AND seq=1", (result["round_id"],),
            )
            conn.commit()
            counts2 = scan_coordinator.round_member_statuses(
                conn, result["round_id"]
            )
            assert scan_coordinator.derive_round_status(counts2) == "full"
        finally:
            conn.close()

    @pytest.mark.parametrize("counts,expected", [
        ({"done": 2}, "full"),
        ({"done": 1, "failed": 1}, "partial"),
        ({"failed": 2}, "failed"),
        ({}, "failed"),
        ({"done": 1, "cancelled": 1}, "cancelled"),
        ({"done": 1, "skipped": 1}, "cancelled"),
        ({"failed": 1, "cancelled": 1}, "cancelled"),
    ])
    def test_derive_round_status_truth_table(self, counts, expected):
        assert scan_coordinator.derive_round_status(counts) == expected

    def test_time_span_disclosed_and_not_an_atomic_instant(self, round_env, roots):
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        span = result["time_span"]
        assert span["atomic"] is False
        assert span["started_at"] and span["finished_at"]
        assert "不可当作同一时点" in span["note"]
        assert len(span["snapshot_times"]) == 2
        report = pathlib.Path(result["report"])
        text = report.read_text(encoding="utf-8")
        assert "时间跨度" in text
        assert "不可" in text and "横向相加" in text

    def test_no_aggregate_percentage_in_round_output(self, round_env, roots, monkeypatch):
        """不伪造百分比：轮次结果/报告里没有任何聚合占比或整体总量口径。"""
        _failing_roots(monkeypatch, {str(roots["b"])})
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        text = pathlib.Path(result["report"]).read_text(encoding="utf-8")
        for banned in ("%", "占比", "整体用量", "完成度"):
            assert banned not in text, f"轮次报告不得出现聚合口径：{banned}"
        summary = _round_message(result["round_id"])
        assert "%" not in summary

    def test_scope_failure_does_not_stop_later_scopes(self, round_env, roots, monkeypatch):
        """失败范围之后的范围仍要采（部分成功不是「遇错即停」）。"""
        specs = [
            scan_coordinator.ScopeSpec.from_path(roots["a"]),
            scan_coordinator.ScopeSpec.from_path(roots["b"]),
            scan_coordinator.ScopeSpec.from_path(tmp_path_third()),
        ]
        _failing_roots(monkeypatch, {str(roots["a"])})
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=specs)
        statuses = [m["status"] for m in result["members"]]
        assert statuses == ["failed", "done", "done"]


def _round_message(round_id: int) -> str:
    conn = db.connect()
    try:
        return _round_row(conn, round_id)["message"] or ""
    finally:
        conn.close()


_THIRD_ROOT: list[pathlib.Path] = []


def tmp_path_third() -> pathlib.Path:
    """第三个合成根（懒建，供「失败后仍继续」用例复用）。"""
    if not _THIRD_ROOT:
        base = _THIRD_ROOT[0].parent
        _THIRD_ROOT.append(_make_root(base, "root_c"))
    return _THIRD_ROOT[0]


@pytest.fixture(autouse=True)
def _third_root_base(tmp_path):
    _THIRD_ROOT.clear()
    _THIRD_ROOT.append(tmp_path / "synthetic")
    _THIRD_ROOT[0].mkdir(parents=True, exist_ok=True)
    yield
    _THIRD_ROOT.clear()


# ── 合同 3：容量采样接线 ──


class TestCapacitySampling:
    def test_round_end_records_independent_capacity_sample(self, round_env, roots):
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        conn = db.connect()
        try:
            samples = scan_coordinator.round_capacity_samples(
                conn, result["round_id"]
            )
            assert samples, "轮末至少一条独立容量样本"
            container = [s for s in samples if s["container_id"].startswith("apfs-container:")]
            assert container, "必须有容器级样本"
            assert container[0]["source"] == "storage-discovery"
            assert container[0]["sampled_at"]
            assert container[0]["total_bytes"] == 500 * 1024**3
            assert container[0]["free_bytes"] == 90 * 1024**3
            assert result["capacity"]["status"] == "sampled"
            assert result["capacity"]["samples"] == len(samples)
        finally:
            conn.close()

    def test_volume_sample_never_carries_free(self, round_env, roots):
        """同容器共享 free 是唯一权威：卷样本 free 必须为空。"""
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        conn = db.connect()
        try:
            samples = scan_coordinator.round_capacity_samples(
                conn, result["round_id"]
            )
            volumes = [s for s in samples if s["container_id"].startswith("apfs-volume:")]
            assert volumes
            assert all(s["free_bytes"] is None for s in volumes)
        finally:
            conn.close()

    def test_discovery_unavailable_keeps_old_time_and_no_statvfs_substitute(
        self, round_env, roots, monkeypatch
    ):
        """采样失败：保持未知/旧时间，绝不拿目录 statvfs 冒充整盘容量。"""
        conn = db.connect()
        try:
            # 先落一条昨天的容器样本作为「上次已知值」。
            scanner.record_container_capacity_sample(
                conn, "apfs-container:OLD", source="storage-discovery",
                total_bytes=100, free_bytes=7,
                sampled_at="2026-09-01T00:00:00", round_id=None,
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(
            scan_coordinator, "read_capacity_readings", lambda: []
        )
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        conn = db.connect()
        try:
            new_samples = scan_coordinator.round_capacity_samples(
                conn, result["round_id"]
            )
            assert new_samples == [], "失败时本轮不落任何新容量样本"
            old = conn.execute(
                "SELECT * FROM container_capacity_samples WHERE round_id IS NULL"
            ).fetchall()
            assert len(old) == 1
            assert old[0]["sampled_at"] == "2026-09-01T00:00:00"
            assert old[0]["free_bytes"] == 7
            # 不得出现任何 statvfs 来源的容器样本。
            assert conn.execute(
                "SELECT COUNT(*) FROM container_capacity_samples "
                "WHERE source='statvfs'"
            ).fetchone()[0] == 0
        finally:
            conn.close()
        assert result["capacity"]["status"] == "unavailable"
        assert result["capacity"]["free_bytes"] is None

    def test_discovery_exception_is_contained(self, round_env, roots, monkeypatch):
        def boom():
            raise RuntimeError("diskutil 不可用")

        monkeypatch.setattr(scan_coordinator, "read_capacity_readings", boom)
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert result["round_status"] == "full", "诊断通道故障不改轮次成败"
        assert result["capacity"]["status"] == "unavailable"

    def test_real_storage_call_is_actually_invoked(self, round_env, roots, monkeypatch):
        """每轮**实际调用** ISS-152 的容量读取（不是自己算 statvfs）。"""
        calls: list[str] = []
        real = storage.discover_startup

        def spy(*args, **kwargs):
            calls.append("called")
            return real(*args, **kwargs)

        monkeypatch.setattr(storage, "discover_startup", spy)
        scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert calls, "轮次必须实际走 storage.discover_startup"

    def test_capacity_readings_consumer_reads_only_v1_objects(self):
        """只读消费：容器 free 取 shared_free，卷 free 恒 None。"""
        readings = scan_coordinator.capacity_readings_from_discovery(
            _synthetic_discovery(free_bytes=42, total_bytes=99)
        )
        by_kind = {r.kind: r for r in readings}
        assert by_kind["container"].free_bytes == 42
        assert by_kind["container"].total_bytes == 99
        assert by_kind["volume"].free_bytes is None
        assert by_kind["volume"].total_bytes == 120 * 1024**3
        assert all(r.source == "storage-discovery" for r in readings)

    def test_unavailable_discovery_yields_no_container_reading(self):
        empty = storage.StorageDiscovery(
            schema=storage.DISCOVERY_SCHEMA, version=storage.DISCOVERY_VERSION,
            status=storage.DiscoveryStatus.UNAVAILABLE,
            generated_at="2026-10-05T00:00:00+00:00", platform="darwin",
            startup_container=None, startup_volumes=(), visible_entries=(),
            other_devices=(), errors=("diskutil 缺失",), source_commands=(),
        )
        assert scan_coordinator.capacity_readings_from_discovery(empty) == []


# ── 合同 4：报告 / 通知 ──


class TestRoundReportsAndNotifications:
    def test_partial_round_never_says_whole_disk_complete(self, round_env, roots, monkeypatch, _no_notifications):
        """任务卡硬约束：部分成功不得发「整盘扫描全部完成」类通知。"""
        _failing_roots(monkeypatch, {str(roots["b"])})
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert result["round_status"] == "partial"
        assert _no_notifications, "本轮应发出轮次级通知"
        titles = [title for title, _body, _sound in _no_notifications]
        bodies = " ".join(body for _t, body, _s in _no_notifications)
        assert notify.TITLE_ROUND_FULL not in titles
        assert notify.TITLE_DONE not in titles, "部分成功不得发单范围「扫描完成」"
        for title in titles:
            assert "完成" not in title.replace("部分完成", ""), \
                f"部分成功通知标题不得声称完成：{title}"
        assert "失败" in bodies and "沿用上次样本" in bodies

    def test_full_round_sends_completion_and_no_failed_claim(self, round_env, roots, _no_notifications):
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert result["round_status"] == "full"
        titles = [title for title, _b, _s in _no_notifications]
        assert notify.TITLE_ROUND_FULL in titles
        bodies = " ".join(body for _t, body, _s in _no_notifications)
        assert "失败" not in bodies

    def test_all_failed_round_says_failed_not_complete(self, round_env, roots, monkeypatch, _no_notifications):
        _failing_roots(monkeypatch, {str(roots["a"]), str(roots["b"])})
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert result["round_status"] == "failed"
        titles = [title for title, _b, _s in _no_notifications]
        assert notify.TITLE_ROUND_FAILED in titles
        assert notify.TITLE_ROUND_FULL not in titles

    def test_report_names_do_not_collide_across_scopes(self, round_env, roots):
        """一轮两个范围 → 两份各自命名的日报 + 一份汇总，互不覆盖。"""
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        names = sorted(p.name for p in pathlib.Path(config.REPORTS_DIR).iterdir())
        round_reports = [n for n in names if "-round-" in n]
        scope_reports = [n for n in names if "-scope-" in n]
        assert len(round_reports) == 1, round_reports
        assert len(set(names)) == len(names)
        # 首轮没有同数据集基线，因此每范围日报可能缺席；关键是不覆盖。
        assert len(round_reports) + len(scope_reports) == len(names)

    def test_same_day_second_scope_report_not_overwritten(self, round_env, roots, monkeypatch):
        """同日两个范围各写一份日报，**互不覆盖**（命名/关联隔离）。

        前提要如实，不能硬造：``scanner._drop_same_day`` 实行「一天一行」，
        同一计划同一天的旧快照会被顶掉，因此**同一天连扫两轮拿不到基线**
        （第二轮无对比日报是正确行为，不是缺陷）。要让每范围日报真的写出，
        基线必须来自更早的一天——这里把首轮快照回填成昨天，等价于真实的
        「昨天扫过、今天再扫」。于是今天两个范围各有一份日报，断言它们
        命名不同、内容不同，且首轮那份轮次汇总不被覆盖。
        """
        _run_id, first = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert first["round_status"] == "full"
        assert not [p for p in pathlib.Path(config.REPORTS_DIR).iterdir()
                    if "-scope-" in p.name], "首轮无基线，就不该有对比日报"

        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE snapshots SET created_at=? WHERE round_id=?",
                (f"{yesterday}T03:00:00", first["round_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        _run_id, second = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert second["round_status"] == "full"
        conn = db.connect()
        try:
            by_key = {
                sid: config.REPORTS_DIR / reports.report_file_name(conn, sid)
                for sid in second["snapshot_ids"]
            }
        finally:
            conn.close()

        # 两个范围两个互不相同的文件，且都真实存在、都带范围段。
        assert len(set(by_key.values())) == 2
        for path in by_key.values():
            assert path.exists(), f"{path.name} 应存在（今天已有昨天基线）"
            assert "-scope-" in path.name
        # 内容各自独立：A 的报告没有被 B 的内容覆盖。
        items = list(by_key.values())
        assert items[0].read_text(encoding="utf-8") != items[1].read_text(encoding="utf-8")
        # 首轮的轮次汇总仍在（按轮次 id 分文件，后一轮不覆盖前一轮）。
        round_files = sorted(
            p.name for p in pathlib.Path(config.REPORTS_DIR).iterdir()
            if "-round-" in p.name
        )
        assert len(round_files) == 2, round_files

    def test_report_file_name_is_scope_scoped(self, round_env, roots):
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        conn = db.connect()
        try:
            names = set()
            for sid in result["snapshot_ids"]:
                name = reports.report_file_name(conn, sid)
                names.add(name)
                assert "-scope-" in name
                assert name.startswith(_day_prefix(conn, sid))
            assert len(names) == 2, "两个范围两个文件名"
        finally:
            conn.close()

    def test_round_report_lists_per_scope_basis_and_capacity(self, round_env, roots, monkeypatch):
        _failing_roots(monkeypatch, {str(roots["b"])})
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        text = pathlib.Path(result["report"]).read_text(encoding="utf-8")
        assert "各范围" in text
        assert str(roots["a"]) in text and str(roots["b"]) in text
        assert "部分完成" in text
        assert "沿用上次样本" in text
        assert "采样时间" in text and "共享剩余" in text

    def test_round_report_states_capacity_unknown_without_substitute(self, round_env, roots, monkeypatch):
        monkeypatch.setattr(scan_coordinator, "read_capacity_readings", lambda: [])
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        text = pathlib.Path(result["report"]).read_text(encoding="utf-8")
        assert "未取得新的整盘容量样本" in text
        assert "未用任何目录 statvfs" in text

    def test_threshold_uses_container_free_not_directory_statvfs(
        self, round_env, roots, monkeypatch, _no_notifications
    ):
        """通知阈值针对正确容量主体：本轮容器样本的共享剩余。"""
        monkeypatch.setattr(
            scan_coordinator, "read_capacity_readings",
            lambda: scan_coordinator.capacity_readings_from_discovery(
                _synthetic_discovery(free_bytes=3 * 1024**3)
            ),
        )
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        titles = [title for title, _b, _s in _no_notifications]
        assert notify.TITLE_ALERT in titles, "容器共享剩余低于阈值应换告警标题"
        bodies = " ".join(body for _t, body, _s in _no_notifications)
        assert "剩余 3.0 GB" in bodies

    def test_threshold_not_applied_when_capacity_unknown(
        self, round_env, roots, monkeypatch, _no_notifications
    ):
        """容量未知不做阈值化，也不借用任何目录 statvfs 的 free。"""
        monkeypatch.setattr(scan_coordinator, "read_capacity_readings", lambda: [])
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        titles = [title for title, _b, _s in _no_notifications]
        assert notify.TITLE_ALERT not in titles
        bodies = " ".join(body for _t, body, _s in _no_notifications)
        assert "整盘容量未更新" in bodies
        assert "剩余" not in bodies

    def test_high_container_free_uses_normal_title(self, round_env, roots, _no_notifications):
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        titles = [title for title, _b, _s in _no_notifications]
        assert notify.TITLE_ALERT not in titles


def _day_prefix(conn, sid: int) -> str:
    return conn.execute(
        "SELECT created_at FROM snapshots WHERE id=?", (sid,)
    ).fetchone()["created_at"][:10]


# ── 合同 5：取消 / 竞态 ──


class TestRoundCancellation:
    def test_cancel_before_first_scope(self, round_env, roots):
        session = scan_coordinator.start_scan(source="cli", scopes=_specs(roots))
        session.cancel()
        with pytest.raises(scan_coordinator.ScanCancelledError):
            session.execute()
        conn = db.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM scan_rounds WHERE status!='cancelled'"
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_cancel_during_first_scope_reaps_only_own_du(self, round_env, roots, monkeypatch):
        """首范围取消：只回收本轮自己的 du 子进程，锁随之释放。"""
        child_pids: list[int] = []
        real_popen = subprocess.Popen
        session = scan_coordinator.start_scan(source="cli", scopes=_specs(roots))

        def spawn_then_cancel(*args, **kwargs):
            proc = real_popen(["/bin/sleep", "30"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True,
                              pass_fds=kwargs.get("pass_fds", ()))
            child_pids.append(proc.pid)
            session.cancel()
            return proc

        monkeypatch.setattr(scanner.subprocess, "Popen", spawn_then_cancel)
        with pytest.raises(scanner.ScanInterruptedError):
            session.execute()
        assert child_pids, "本轮应当只 spawn 一次 du"
        with pytest.raises(ProcessLookupError):
            os.kill(child_pids[0], 0)
        # 锁已释放：能重新取得即证明没有残留 owner 持有。
        again = scan_coordinator.ScanLease.acquire(
            scan_coordinator._lock_path(), source="cli"
        )
        again.release()

    def test_cancel_during_middle_scope_keeps_earlier_commit(self, round_env, roots, monkeypatch):
        """中范围取消：已提交的前序范围各自保留，后续成员 skipped。"""
        seen: list[str] = []
        real_run_du = scanner.run_du
        session = scan_coordinator.start_scan(source="cli", scopes=_specs(roots))

        def shim(root):
            seen.append(str(root))
            return real_run_du(root)

        monkeypatch.setattr(scanner, "run_du", shim)
        # 在第一个范围采集完后（run_du 返回时）请求取消 → 第二个范围进入
        # 采集进程上下文时取消点生效，本次 du 不再启动。
        real_ctx = scanner.du_process_context

        @contextlib.contextmanager
        def ctx_and_cancel(**kwargs):
            with real_ctx(**kwargs):
                if seen and str(roots["b"]) not in seen:
                    session.cancel()
                yield

        monkeypatch.setattr(scanner, "du_process_context", ctx_and_cancel)
        with pytest.raises(scan_coordinator.ScanCancelledError):
            session.execute()
        assert str(roots["b"]) not in seen, "取消后不得再为该范围启动 du"
        conn = db.connect()
        try:
            round_id = conn.execute(
                "SELECT id FROM scan_rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            assert _round_row(conn, round_id)["status"] == "cancelled"
            members = _members(conn, round_id)
            assert members[0]["status"] == "done"
            assert members[0]["snapshot_id"] is not None
            assert members[1]["status"] in {"skipped", "cancelled"}
            assert conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE root=?", (str(roots["a"]),)
            ).fetchone()[0] == 1, "已提交范围的新快照保留"
        finally:
            conn.close()

    def test_cancel_during_last_scope(self, round_env, roots, monkeypatch):
        """末范围取消：前序全成功，末成员 cancelled，轮次 cancelled。"""
        session = scan_coordinator.start_scan(source="cli", scopes=_specs(roots))
        real_ctx = scanner.du_process_context
        calls: list[str] = []

        @contextlib.contextmanager
        def ctx_and_cancel(**kwargs):
            with real_ctx(**kwargs):
                calls.append("entered")
                if len(calls) == 2:
                    session.cancel()
                yield

        monkeypatch.setattr(scanner, "du_process_context", ctx_and_cancel)
        with pytest.raises(scan_coordinator.ScanCancelledError):
            session.execute()
        conn = db.connect()
        try:
            round_id = conn.execute(
                "SELECT id FROM scan_rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            members = _members(conn, round_id)
            assert members[0]["status"] == "done"
            assert members[1]["status"] in {"cancelled", "skipped"}
            assert _round_row(conn, round_id)["status"] == "cancelled"
            assert conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE root=?", (str(roots["a"]),)
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_per_scope_timeout_marks_that_scope_and_keeps_others(
        self, round_env, roots, monkeypatch
    ):
        """每范围时限到：该范围 interrupted，整轮 interrupted，锁释放。"""
        real_popen = subprocess.Popen
        spawned: list[str] = []

        def popen_for_b(*args, **kwargs):
            root_arg = args[0] if args else ""
            argv = args[0] if args and isinstance(args[0], list) else []
            if any(str(roots["b"]) in str(a) for a in (argv or args)):
                spawned.append("b")
                return real_popen(["/bin/sleep", "30"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  start_new_session=True,
                                  pass_fds=kwargs.get("pass_fds", ()))
            spawned.append("real")
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(config, "DU_TIMEOUT_S", 30.0)
        monkeypatch.setattr(scanner.subprocess, "Popen", popen_for_b)
        with pytest.raises(scanner.ScanInterruptedError):
            scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert "real" in spawned and "b" in spawned
        conn = db.connect()
        try:
            round_id = conn.execute(
                "SELECT id FROM scan_rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            assert _round_row(conn, round_id)["status"] == "cancelled"
            assert conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE root=?", (str(roots["a"]),)
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_spawn_race_reports_honestly(self, round_env, roots, monkeypatch):
        """spawn 竞态：进程根本没起来时报可诊断错误，不静默当成功。"""
        def refuse(*args, **kwargs):
            raise OSError("合成竞态：无法 spawn")

        monkeypatch.setattr(scanner.subprocess, "Popen", refuse)
        _run_id, result = scan_coordinator.run_scan(source="cli", scopes=_specs(roots))
        assert result["round_status"] == "failed"
        reasons = [m.get("reason") for m in result["members"]]
        assert all(reason and "竞态" in reason for reason in reasons)

    def test_other_process_lease_not_killed_by_round_cancel(self, round_env, roots, monkeypatch):
        """取消只回收本轮自有进程：另一个 owner 持有的锁不被破坏。"""
        other_lock = pathlib.Path(config.DB_PATH).with_name("other.lock")
        holder = scan_coordinator.ScanLease.acquire(other_lock, source="api")
        child_pids: list[int] = []
        real_popen = subprocess.Popen
        session = scan_coordinator.start_scan(source="cli", scopes=_specs(roots))

        def spawn_then_cancel(*args, **kwargs):
            proc = real_popen(["/bin/sleep", "30"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True,
                              pass_fds=kwargs.get("pass_fds", ()))
            child_pids.append(proc.pid)
            session.cancel()
            return proc

        monkeypatch.setattr(scanner.subprocess, "Popen", spawn_then_cancel)
        with pytest.raises(scanner.ScanInterruptedError):
            session.execute()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pids[0], 0)
        # 别人的锁仍被持有（未被本轮动过）。
        with pytest.raises(scan_coordinator.ScanBusyError):
            scan_coordinator.ScanLease.acquire(other_lock, source="api")
        holder.release()

    def test_interrupted_round_sends_interrupted_not_done_notification(
        self, round_env, roots, monkeypatch, _no_notifications
    ):
        session = scan_coordinator.start_scan(source="cli", scopes=_specs(roots))
        session.cancel()
        with pytest.raises(scan_coordinator.ScanCancelledError):
            session.execute()
        titles = [title for title, _b, _s in _no_notifications]
        assert notify.TITLE_INTERRUPTED in titles
        assert notify.TITLE_ROUND_FULL not in titles
        assert notify.TITLE_DONE not in titles


# ── CLI 消费者 ──


class TestCliMultiScope:
    def _args(self, monkeypatch, *argv):
        from fathom import cli
        parser_args = argv
        return parser_args

    def test_cli_scope_args_build_ordered_specs(self, isolated, monkeypatch):
        from fathom import cli
        args = _Namespace(scope=["/tmp/a", "/tmp/b"], scope_id=None,
                          root=None, round_timeout_s=None)
        specs = cli._scan_scopes(args)
        # 规范根按 realpath 归一（/tmp → /private/tmp 是同一目录），
        # 断言也必须比规范根，否则测的是符号链接而不是顺序。
        assert [s.root for s in specs] == [
            pathlib.Path(os.path.realpath("/tmp/a")),
            pathlib.Path(os.path.realpath("/tmp/b")),
        ]
        assert all(s.kind == "path" for s in specs)
        assert specs[0].scope_id == scan_coordinator.derived_scope_id(
            os.path.realpath("/tmp/a")
        )

    def test_cli_scope_ids_pair_positionally(self, isolated):
        from fathom import cli
        args = _Namespace(scope=["/tmp/a", "/tmp/b"],
                          scope_id=["fixture-a", "fixture-b"],
                          root=None, round_timeout_s=None)
        specs = cli._scan_scopes(args)
        assert [s.scope_id for s in specs] == ["fixture-a", "fixture-b"]

    def test_cli_scope_id_count_mismatch_is_rejected(self, isolated):
        from fathom import cli
        args = _Namespace(scope=["/tmp/a", "/tmp/b"], scope_id=["only-one"],
                          root=None, round_timeout_s=None)
        with pytest.raises(scan_coordinator.ScanScopeError):
            cli._scan_scopes(args)

    def test_cli_without_scope_is_legacy(self, isolated):
        from fathom import cli
        args = _Namespace(scope=None, scope_id=None, root=None,
                          round_timeout_s=None)
        assert cli._scan_scopes(args) == []

    def test_cli_parser_accepts_repeatable_scope(self):
        from fathom import cli
        parser = cli._build_parser()
        # 显式多范围消费者：--scope 可重复、顺序即采集顺序。
        args = parser.parse_args(
            ["scan", "--scope", "/tmp/a", "--scope", "/tmp/b",
             "--round-timeout-s", "90"]
        )
        assert args.scope == ["/tmp/a", "/tmp/b"]
        assert args.round_timeout_s == 90.0
        assert args.func is cli.cmd_scan
        # 旧 --root 单范围入口保持可用（与 --scope 互斥由 cmd_scan 层拒绝）。
        legacy = parser.parse_args(["scan", "--root", "/tmp/a"])
        assert legacy.root == "/tmp/a" and legacy.scope is None
        assert legacy.round_timeout_s is None

    def test_cli_scan_rejects_scope_with_root(self, isolated, capsys, monkeypatch):
        from fathom import cli
        args = _Namespace(scope=["/tmp/a"], scope_id=None, root="/tmp/b",
                          round_timeout_s=None, source="cli")
        assert cli.cmd_scan(args) == 1
        assert "不能同时给出" in capsys.readouterr().err


class _Namespace:
    """最小 argparse.Namespace 替身（避免为每个用例拼全量参数）。"""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
