"""ISS-018 快照完整性：无效扫描必须被拒绝，当日有效快照/entries/volume_stats 原样保留。

反例来源 docs/plans/2026-09-12-project-review.md AUD-01：失败 du（非零退出/空
stdout）曾把当日 20000 KiB 有效快照静默替换为 0 KiB、du_seconds=0。

本文件的真实基线（本机 /usr/bin/du，只扫 tempfile 小根）：
- 正常根+000 子目录：exit=1 但根行存在（部分覆盖，可接受）
- 空根：exit=0，根行存在且为 0（有效全量）
- 根 000 / 缺根：exit=1 且无根行（无效，必须拒绝）
因此有效性锚点是"根目录记录存在"，退出码与 denied_count 表达采集质量。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess

import pytest

from fathom import config, db, reports, scanner


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """数据库与日报目录指到临时目录，避免污染真实 data/fathom.db。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")


def _write_file(path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size_bytes)


@pytest.fixture
def valid_tree(tmp_path):
    """≈20000 KiB 的小目录树（对齐 AUD-01 的有效快照量级）。"""
    _write_file(tmp_path / "big" / "data.bin", 20 * 1024 * 1024)
    return tmp_path


def _inject_du_process(monkeypatch, *, returncode=0, stdout="", stderr=""):
    """在子进程层注入 du 结果（AUD-01 的注入面，走 run_du 真实解析路径）。"""
    fake = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: fake)


def _db_state(conn: sqlite3.Connection) -> dict:
    """快照/条目/卷统计的完整可比较状态，用于"失败后一字不动"断言。"""
    return {
        "snapshots": [tuple(r) for r in conn.execute(
            "SELECT id, created_at, root, dir_count, denied_count, du_seconds, "
            "total_kb FROM snapshots ORDER BY id")],
        "entries": sorted(tuple(r) for r in conn.execute(
            "SELECT snapshot_id, path, size_kb FROM entries")),
        "volume_stats": [tuple(r) for r in conn.execute(
            "SELECT snapshot_id, total_bytes, free_bytes FROM volume_stats "
            "ORDER BY snapshot_id")],
    }


def _make_valid_snapshot(conn, tree, min_kb: int = 1024) -> int:
    sid = scanner.create_snapshot(conn, tree, min_kb=min_kb)
    row = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
    assert row["total_kb"] >= 19 * 1024, "先建立当日有效快照（≈20000 KiB）"
    assert row["du_seconds"] > 0
    return sid


class TestAudit01InvalidScanRejected:
    """AUD-01 反例钉住：致命退出/空输出/缺根记录一律拒绝，当日旧数据原样保留。"""

    def test_fatal_exit_preserves_same_day_snapshot(self, valid_tree, monkeypatch):
        conn = db.connect()
        try:
            sid1 = _make_valid_snapshot(conn, valid_tree)
            before = _db_state(conn)

            _inject_du_process(
                monkeypatch, returncode=1, stdout="",
                stderr="du: /x: Operation not permitted")

            with pytest.raises(scanner.InvalidScanError) as ei:
                scanner.create_snapshot(conn, valid_tree, min_kb=1024)
            assert "未返回根目录记录" in str(ei.value)
            assert "退出码 1" in str(ei.value)
            # 旧 snapshot/entries/volume_stats 一字不动，也没有新增行
            assert _db_state(conn) == before
            assert _db_state(conn)["snapshots"][0][0] == sid1
        finally:
            conn.close()

    def test_empty_output_preserves_same_day_snapshot(self, valid_tree, monkeypatch):
        """空 stdout 即使退出码为 0（异常截断/工具异常）也是无效采集。"""
        conn = db.connect()
        try:
            _make_valid_snapshot(conn, valid_tree)
            before = _db_state(conn)

            _inject_du_process(monkeypatch, returncode=0, stdout="", stderr="")

            with pytest.raises(scanner.InvalidScanError):
                scanner.create_snapshot(conn, valid_tree, min_kb=1024)
            assert _db_state(conn) == before
        finally:
            conn.close()

    def test_missing_root_line_preserves_same_day_snapshot(self, valid_tree, monkeypatch):
        """有子目录输出但缺根行：根记录是有效性锚点，必须拒绝。"""
        conn = db.connect()
        try:
            _make_valid_snapshot(conn, valid_tree)
            before = _db_state(conn)

            _inject_du_process(
                monkeypatch, returncode=0,
                stdout=f"128\t{valid_tree}/big\n")

            with pytest.raises(scanner.InvalidScanError):
                scanner.create_snapshot(conn, valid_tree, min_kb=1024)
            assert _db_state(conn) == before
        finally:
            conn.close()

    def test_signal_killed_partial_stdout_preserves(self, valid_tree, monkeypatch):
        """du 被信号杀死（负返回码）时根行最后打印、必然缺失：按无效拒绝。"""
        conn = db.connect()
        try:
            _make_valid_snapshot(conn, valid_tree)
            before = _db_state(conn)

            _inject_du_process(
                monkeypatch, returncode=-9,
                stdout=f"128\t{valid_tree}/big\n")

            with pytest.raises(scanner.InvalidScanError):
                scanner.create_snapshot(conn, valid_tree, min_kb=1024)
            assert _db_state(conn) == before
        finally:
            conn.close()

    def test_missing_root_path_rejected(self, tmp_path):
        """真实 du 扫不存在的根：拒绝且库中不产生任何快照。"""
        conn = db.connect()
        try:
            with pytest.raises(scanner.InvalidScanError):
                scanner.create_snapshot(conn, tmp_path / "no_such_root", min_kb=1024)
            assert _db_state(conn)["snapshots"] == []
        finally:
            conn.close()

    def test_real_denied_root_preserves_same_day_snapshot(self, valid_tree):
        """零注入版本：真实 du 扫 000 根被拒绝，当日旧快照原样保留。"""
        conn = db.connect()
        try:
            _make_valid_snapshot(conn, valid_tree)
            before = _db_state(conn)

            os.chmod(valid_tree, 0)
            try:
                with pytest.raises(scanner.InvalidScanError):
                    scanner.create_snapshot(conn, valid_tree, min_kb=1024)
            finally:
                os.chmod(valid_tree, 0o755)

            assert _db_state(conn) == before
        finally:
            conn.close()


class TestRealDuSemantics:
    """真实 /usr/bin/du 行为回归（只扫 tempfile 小根），锚定有效性判定依据。"""

    def test_normal_tree_full_collection(self, valid_tree):
        result = scanner.run_du(valid_tree)
        assert result.exit_code == 0
        assert str(valid_tree) in result.sizes
        assert result.sizes[str(valid_tree)] >= 19 * 1024
        assert result.denied_count == 0
        assert result.elapsed_seconds > 0

    def test_empty_root_is_valid_full(self, tmp_path):
        empty = tmp_path / "empty_root"
        empty.mkdir()
        result = scanner.run_du(empty)
        assert result.exit_code == 0
        assert result.sizes[str(empty)] == 0
        assert result.denied_count == 0
        # 正常空根是有效全量采集，可以建快照
        conn = db.connect()
        try:
            sid = scanner.create_snapshot(conn, empty, min_kb=1024)
            row = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
            assert row["total_kb"] == 0
            assert row["denied_count"] == 0
        finally:
            conn.close()

    def test_denied_subdir_is_partial_with_root_record(self, tmp_path):
        ok = tmp_path / "sub_ok"
        _write_file(ok / "f.bin", 4096)
        denied = tmp_path / "sub_denied"
        denied.mkdir()
        os.chmod(denied, 0)
        try:
            result = scanner.run_du(tmp_path)
            assert result.exit_code == 1          # BSD du 部分权限失败也非零退出
            assert str(tmp_path) in result.sizes  # 但根行存在 → 部分覆盖而非无效
            assert result.denied_count == 1
            assert result.stderr_tail
            assert "Permission denied" in result.stderr_tail[-1]
        finally:
            os.chmod(denied, 0o755)

    def test_denied_root_yields_no_root_record(self, tmp_path):
        denied_root = tmp_path / "denied_root"
        denied_root.mkdir()
        _write_file(denied_root / "f.txt", 8)
        os.chmod(denied_root, 0)
        try:
            result = scanner.run_du(denied_root)
            assert result.exit_code == 1
            assert result.sizes == {}
        finally:
            os.chmod(denied_root, 0o755)


class TestQualityClassification:
    """classify_collection：根记录缺失抛错；退化信号决定 full/partial。"""

    def _du_result(self, *, sizes, exit_code=0, denied=0):
        return scanner.DuResult(
            sizes=sizes, exit_code=exit_code, denied_count=denied,
            elapsed_seconds=0.01, stderr_tail=())

    def test_full_when_clean(self):
        r = self._du_result(sizes={"/r": 10})
        assert scanner.classify_collection(r, "/r") == "full"

    def test_partial_when_nonzero_exit_with_root(self):
        r = self._du_result(sizes={"/r": 10}, exit_code=1)
        assert scanner.classify_collection(r, "/r") == "partial"

    def test_partial_when_denied_with_zero_exit(self):
        r = self._du_result(sizes={"/r": 10}, denied=3)
        assert scanner.classify_collection(r, "/r") == "partial"

    def test_invalid_when_root_record_missing(self):
        r = self._du_result(sizes={"/r/sub": 5}, exit_code=0)
        with pytest.raises(scanner.InvalidScanError):
            scanner.classify_collection(r, "/r")

    def test_invalid_when_output_empty(self):
        r = self._du_result(sizes={}, exit_code=0)
        with pytest.raises(scanner.InvalidScanError):
            scanner.classify_collection(r, "/r")


class TestPartialCoverage:
    """权限受限但根记录有效：按部分覆盖落库，质量经 denied_count 表达。"""

    def test_partial_scan_persists_with_denied_count(self, tmp_path):
        ok = tmp_path / "ok"
        _write_file(ok / "f.bin", 2 * 1024 * 1024)
        denied = tmp_path / "sub_denied"
        denied.mkdir()
        os.chmod(denied, 0)
        try:
            conn = db.connect()
            try:
                sid = scanner.create_snapshot(conn, tmp_path, min_kb=1024)
                row = conn.execute(
                    "SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
                assert row["denied_count"] == 1   # 权限缺口以现有列持久化
                assert row["total_kb"] > 0
                assert row["du_seconds"] > 0
                entries = reports.load_snapshot(conn, sid)
                assert str(tmp_path) in entries
                assert f"{tmp_path}/ok" in entries
                assert f"{tmp_path}/sub_denied" not in entries  # 无法读取的子树无记录
            finally:
                conn.close()
        finally:
            os.chmod(denied, 0o755)

    def test_partial_and_empty_root_distinguishable(self, tmp_path):
        """正常空根（denied=0）与权限受限根（denied>0）落库后可区分。"""
        empty = tmp_path / "empty"
        empty.mkdir()
        denied_root_sub = tmp_path / "partial"
        denied_root_sub.mkdir()
        (denied_root_sub / "keep").mkdir()
        hidden = denied_root_sub / "hidden"
        hidden.mkdir()
        os.chmod(hidden, 0)
        try:
            conn = db.connect()
            try:
                sid_empty = scanner.create_snapshot(conn, empty, min_kb=1024)
                sid_part = scanner.create_snapshot(conn, denied_root_sub, min_kb=1024)

                def denied_of(sid):
                    return conn.execute(
                        "SELECT denied_count FROM snapshots WHERE id=?", (sid,)
                    ).fetchone()["denied_count"]

                assert denied_of(sid_empty) == 0
                assert denied_of(sid_part) >= 1
            finally:
                conn.close()
        finally:
            os.chmod(hidden, 0o755)


class TestDuSeconds:
    """du_seconds 必须是本次采集的真实耗时，失败扫描不得改写历史值。"""

    def test_persisted_seconds_equal_measured(self, valid_tree, monkeypatch):
        captured = []
        real_run_du = scanner.run_du

        def spy(root):
            result = real_run_du(root)   # 仍跑真实 du，只旁路记录结构化结果
            captured.append(result)
            return result

        monkeypatch.setattr(scanner, "run_du", spy)
        conn = db.connect()
        try:
            sid = scanner.create_snapshot(conn, valid_tree, min_kb=1024)
            row = conn.execute(
                "SELECT du_seconds FROM snapshots WHERE id=?", (sid,)).fetchone()
            assert len(captured) == 1
            assert row["du_seconds"] == captured[0].elapsed_seconds
            assert 0 < row["du_seconds"] < 60
        finally:
            conn.close()

    def test_failed_scan_keeps_old_du_seconds(self, valid_tree, monkeypatch):
        conn = db.connect()
        try:
            sid1 = _make_valid_snapshot(conn, valid_tree)
            old = conn.execute(
                "SELECT du_seconds FROM snapshots WHERE id=?", (sid1,)).fetchone()
            assert old["du_seconds"] > 0

            _inject_du_process(
                monkeypatch, returncode=1, stdout="",
                stderr="du: /x: Operation not permitted")
            with pytest.raises(scanner.InvalidScanError):
                scanner.create_snapshot(conn, valid_tree, min_kb=1024)

            after = conn.execute(
                "SELECT du_seconds FROM snapshots WHERE id=?", (sid1,)).fetchone()
            assert after["du_seconds"] == old["du_seconds"]
        finally:
            conn.close()


class TestWriteInterruption:
    """写入中断：事务回滚，当日旧快照/entries/volume_stats 完整保留。"""

    def test_volume_stat_failure_rolls_back(self, valid_tree, monkeypatch):
        def broken_stat(root):
            raise OSError("模拟 volume_stats 写入前的 I/O 中断")

        conn = db.connect()
        try:
            sid1 = _make_valid_snapshot(conn, valid_tree)
            before = _db_state(conn)
            monkeypatch.setattr(scanner, "_volume_stat", broken_stat)
            with pytest.raises(OSError):
                scanner.create_snapshot(conn, valid_tree, min_kb=1024)
            assert _db_state(conn) == before
            assert _db_state(conn)["snapshots"][0][0] == sid1
        finally:
            conn.close()

    def test_disk_full_rolls_back(self, valid_tree, monkeypatch):
        """真实 SQLite 写入失败（max_page_count 模拟盘满）也必须回滚干净。"""
        conn = db.connect()
        try:
            sid1 = _make_valid_snapshot(conn, valid_tree)
            before = _db_state(conn)

            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
            conn.execute(f"PRAGMA max_page_count={pages}")
            conn.execute("PRAGMA wal_autocheckpoint=1")

            # 注入一次"有效但体量大"的采集：通过闸门后必然撑爆页数上限
            big_sizes = {str(valid_tree): 1}
            big_sizes.update(
                {f"{valid_tree}/d{i}": 1 for i in range(4000)})
            monkeypatch.setattr(scanner, "run_du", lambda root: scanner.DuResult(
                sizes=big_sizes, exit_code=0, denied_count=0,
                elapsed_seconds=0.01, stderr_tail=()))

            with pytest.raises(sqlite3.OperationalError):
                scanner.create_snapshot(conn, valid_tree, min_kb=0)
            conn.rollback()

            # 同连接与新连接看到的都是旧状态（真实回滚，非进程内假象）
            assert _db_state(conn) == before
            fresh = db.connect()
            try:
                assert _db_state(fresh) == before
                assert _db_state(fresh)["snapshots"][0][0] == sid1
            finally:
                fresh.close()
        finally:
            conn.close()


class TestValidScanStillReplaces:
    """有效扫描的同日替换语义不变（AUD-01 修复不得误伤正常覆盖）。"""

    def test_valid_rescan_replaces_same_day(self, valid_tree):
        conn = db.connect()
        try:
            sid1 = scanner.create_snapshot(conn, valid_tree, min_kb=1024)
            sid2 = scanner.create_snapshot(conn, valid_tree, min_kb=1024)
            rows = conn.execute("SELECT id, du_seconds FROM snapshots").fetchall()
            assert len(rows) == 1, "同一天有效重扫仍只保留一行"
            assert rows[0]["id"] == sid2
            assert rows[0]["id"] != sid1
            assert rows[0]["du_seconds"] > 0
            n_entries = conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
            assert n_entries >= 2  # 根 + big 至少各一条
        finally:
            conn.close()


class TestReportFailureKeepsFacts:
    """报告错误不改写扫描事实：日报失败时快照行与条目保持原样。"""

    def test_single_snapshot_report_error_keeps_facts(self, valid_tree):
        conn = db.connect()
        try:
            sid = _make_valid_snapshot(conn, valid_tree)
            before = _db_state(conn)

            with pytest.raises(ValueError):
                reports.write_daily_report(conn, sid)  # 单快照：AUD-03 已知报错

            assert _db_state(conn) == before
        finally:
            conn.close()
