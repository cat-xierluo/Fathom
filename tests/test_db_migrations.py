"""ISS-025：schema 版本、WAL 一致备份与 fail-closed 迁移。"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

import pytest

from fathom import db, scanner


def _legacy_database(path: Path, *, only_snapshots: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    statements = db._SCHEMA_STATEMENTS[:1] if only_snapshots else db._SCHEMA_STATEMENTS
    for statement in statements:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, total_kb) "
        "VALUES ('2026-09-12T12:00:00', '/synthetic/root', 1, 0, 0.1, 42)"
    )
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    return conn


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".backup-v0-*.sqlite3"))


def test_unversioned_database_migrates_without_losing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = _legacy_database(path)
    try:
        wal_path = path.with_name(path.name + "-wal")
        assert wal_path.stat().st_size > 32, "迁移前提交行确实仍在 WAL"
        migrated = db.connect(path)
        try:
            assert db.schema_version(migrated) == db.SCHEMA_VERSION
            assert migrated.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
        finally:
            migrated.close()
    finally:
        legacy.close()

    backups = _backups(path)
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        # 该行提交后仍停留在 WAL；SQLite backup API 必须把它纳入一致备份。
        assert backup.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        backup.close()
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_partial_known_v0_schema_is_completed(tmp_path):
    path = tmp_path / "partial.db"
    legacy = _legacy_database(path, only_snapshots=True)
    legacy.close()

    conn = db.connect(path)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        # v7（ISS-035B）起含分析两表；v8（ISS-153）起再含身份/轮次五表
        # （迁移链末端追加），两条建库路径到达同一结构。
        assert tables == {"snapshots", "entries", "volume_stats", "scan_runs",
                          "scan_run_details", "analysis_runs", "agent_analyses",
                          "scan_scopes", "scan_plans", "scan_rounds",
                          "scan_round_members", "container_capacity_samples"}
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    finally:
        conn.close()


def test_migration_failure_rolls_back_and_leaves_recoverable_backup(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    legacy = _legacy_database(path, only_snapshots=True)
    legacy.close()

    def fail_after_write(conn):
        conn.execute("CREATE TABLE migration_should_rollback(value TEXT)")
        raise sqlite3.OperationalError("injected migration failure")

    monkeypatch.setitem(db._MIGRATIONS, 0, fail_after_write)
    with pytest.raises(db.MigrationError, match="原库已回滚"):
        db.connect(path)

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
        assert raw.execute(
            "SELECT 1 FROM sqlite_master WHERE name='migration_should_rollback'"
        ).fetchone() is None
        assert raw.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
    finally:
        raw.close()
    backups = _backups(path)
    assert len(backups) == 1
    recovery = sqlite3.connect(backups[0])
    try:
        assert recovery.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert recovery.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
    finally:
        recovery.close()


def test_incomplete_migration_cannot_commit_version_or_partial_schema(tmp_path, monkeypatch):
    path = tmp_path / "partial.db"
    legacy = _legacy_database(path, only_snapshots=True)
    legacy.close()
    monkeypatch.setitem(db._MIGRATIONS, 0, lambda conn: None)

    with pytest.raises(db.MigrationError, match="原库已回滚"):
        db.connect(path)

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
        tables = {r[0] for r in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables == {"snapshots"}
        assert raw.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
    finally:
        raw.close()

    backups = _backups(path)
    assert len(backups) == 1
    recovery = sqlite3.connect(backups[0])
    try:
        assert recovery.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert recovery.execute("PRAGMA user_version").fetchone()[0] == 0
        assert recovery.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
    finally:
        recovery.close()


def test_corrupt_database_is_not_deleted_or_rebuilt(tmp_path):
    path = tmp_path / "corrupt.db"
    original = b"not a sqlite database\x00private-data"
    path.write_bytes(original)

    with pytest.raises(db.DatabaseOpenError):
        db.connect(path)

    assert path.read_bytes() == original
    assert _backups(path) == []


def test_newer_schema_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE future_data(secret TEXT)")
    conn.execute("INSERT INTO future_data VALUES ('keep-me')")
    conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    before = path.read_bytes()

    with pytest.raises(db.UnsupportedSchemaVersion, match="拒绝降级"):
        db.connect(path)

    assert path.read_bytes() == before
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT secret FROM future_data").fetchone()[0] == "keep-me"
    finally:
        raw.close()


def test_current_version_with_missing_table_fails_closed(tmp_path):
    path = tmp_path / "invalid-v1.db"
    conn = sqlite3.connect(path)
    conn.execute(db._SCHEMA_STATEMENTS[0])
    conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION}")
    conn.commit()
    conn.close()

    with pytest.raises(db.DatabaseOpenError, match="缺少表"):
        db.connect(path)


def test_current_schema_reopen_does_not_scan_entire_database(tmp_path, monkeypatch):
    path = tmp_path / "current.db"
    created = db.connect(path)
    created.close()
    monkeypatch.setattr(
        db, "_check_integrity",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected full scan")),
    )

    reopened = db.connect(path)
    try:
        assert db.schema_version(reopened) == db.SCHEMA_VERSION
    finally:
        reopened.close()


def test_unconstrained_same_name_columns_are_not_a_trusted_v0_schema(tmp_path):
    path = tmp_path / "fake-v0.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE snapshots(id, created_at, root, dir_count, denied_count, "
        "du_seconds, total_kb)"
    )
    conn.execute(
        "INSERT INTO snapshots VALUES (1, '2026-09-12', '/keep', 1, 0, 0.1, 42)"
    )
    conn.commit()
    conn.close()

    with pytest.raises(db.DatabaseOpenError, match="结构不兼容"):
        db.connect(path)

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
        assert raw.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
    finally:
        raw.close()


@pytest.mark.parametrize("fault", ["foreign_key", "without_rowid"])
def test_current_schema_rejects_broken_entries_invariants(tmp_path, fault):
    path = tmp_path / f"broken-{fault}.db"
    conn = sqlite3.connect(path)
    for index, statement in enumerate(db._SCHEMA_STATEMENTS):
        if index == 1:
            statement = """CREATE TABLE entries (
                snapshot_id INTEGER NOT NULL%s,
                path TEXT NOT NULL,
                size_kb INTEGER NOT NULL,
                PRIMARY KEY (snapshot_id, path)
            )%s""" % (
                " REFERENCES snapshots(id) ON DELETE CASCADE"
                if fault != "foreign_key" else "",
                " WITHOUT ROWID" if fault != "without_rowid" else "",
            )
        conn.execute(statement)
    # v7 起当前结构含分析两表（ISS-035B）；v8 起含身份/轮次五表与
    # 快照身份列（ISS-153）。
    for statement in db._ANALYSIS_TABLE_STATEMENTS:
        conn.execute(statement)
    for ddl in db._SNAPSHOT_ALTER_V8:
        conn.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")
    for statement in db._IDENTITY_TABLE_STATEMENTS:
        conn.execute(statement)
    conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION}")
    conn.commit()
    conn.close()

    with pytest.raises(db.DatabaseOpenError, match="不兼容"):
        db.connect(path)


def test_v3_database_migrates_vanished_count_with_default_zero(tmp_path):
    """v3→v6 链式迁移保留旧快照的原有字段与未知分类。

    v3 库经 v3→v4→v5→v6 三次迁移。
    vanished_count 与 exclude_names 均 NOT NULL DEFAULT，旧行通过默认值
    获得 0 / ''，不补造未知元数据。
    """
    path = tmp_path / "v3.db"
    legacy = sqlite3.connect(path)
    try:
        for statement in db._SCHEMA_STATEMENTS:
            legacy.execute(statement)
        # v3 形态：额外加 min_kb / collection_status，但不带 vanished_count
        legacy.execute("ALTER TABLE snapshots ADD COLUMN min_kb INTEGER")
        legacy.execute("ALTER TABLE snapshots ADD COLUMN collection_status TEXT")
        legacy.execute("PRAGMA user_version=3")
        legacy.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
            "total_kb, min_kb, collection_status) "
            "VALUES ('2026-09-12T12:00:00', '/synthetic/root-a', 1, 0, 0.1, 42, 1024, 'full')"
        )
        legacy.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
            "total_kb, min_kb, collection_status) "
            "VALUES ('2026-09-13T12:00:00', '/synthetic/root-b', 1, 0, 0.1, 99, NULL, NULL)"
        )
        legacy.commit()
    finally:
        legacy.close()

    conn = db.connect(path)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION == 8
        rows = conn.execute(
            "SELECT id, vanished_count, exclude_names FROM snapshots ORDER BY id"
        ).fetchall()
        # 旧行通过 NOT NULL DEFAULT 0/'' 自动获得默认，不补造未知元数据。
        assert [(r["vanished_count"], r["exclude_names"]) for r in rows] == [
            (0, ""), (0, "")
        ]
        # 既有列未被改写：min_kb/collection_status 保持原值。
        originals = conn.execute(
            "SELECT min_kb, collection_status FROM snapshots ORDER BY id"
        ).fetchall()
        assert [(r["min_kb"], r["collection_status"]) for r in originals] == [
            (1024, "full"), (None, None),
        ]
        # 新写入可显式提供 vanished_count + exclude_names（采集时点固化）。
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status, "
            "vanished_count, exclude_names) "
            "VALUES ('2026-09-14T12:00:00', '/synthetic/root-c', 1, 0, 0.1, 7, "
            "1024, 'partial', 5, 'skip.noindex')"
        )
        conn.commit()
        last = conn.execute(
            "SELECT vanished_count, exclude_names FROM snapshots "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert (last["vanished_count"], last["exclude_names"]) == (5, "skip.noindex")
    finally:
        conn.close()
    # 链式迁移失败回退时 v3 备份仍可恢复（迁移前快照）。
    backups_v3 = sorted(path.parent.glob(path.name + ".backup-v3-*.sqlite3"))
    assert len(backups_v3) == 1
    backup = sqlite3.connect(backups_v3[0])
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
        # v3 备份里没有 vanished_count / exclude_names 列（迁移前快照）
        cols = {row[1] for row in backup.execute("PRAGMA table_info(snapshots)")}
        assert "vanished_count" not in cols
        assert "exclude_names" not in cols
        assert backup.execute(
            "SELECT total_kb FROM snapshots ORDER BY id"
        ).fetchall()[0][0] == 42
    finally:
        backup.close()


def test_v3_migration_failure_rolls_back_keeps_vanished_count_uncommitted(tmp_path, monkeypatch):
    """v3→v4 迁移注入失败 → 回滚，原库仍为 v3 且无 vanished_count 列。"""
    path = tmp_path / "v3-fail.db"
    legacy = sqlite3.connect(path)
    try:
        for statement in db._SCHEMA_STATEMENTS:
            legacy.execute(statement)
        legacy.execute("ALTER TABLE snapshots ADD COLUMN min_kb INTEGER")
        legacy.execute("ALTER TABLE snapshots ADD COLUMN collection_status TEXT")
        legacy.execute("PRAGMA user_version=3")
        legacy.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status) "
            "VALUES ('2026-09-12T12:00:00', '/synthetic/root', 1, 0, 0.1, 42, "
            "1024, 'full')"
        )
        legacy.commit()
    finally:
        legacy.close()

    def fail_v4(conn):
        raise sqlite3.OperationalError("injected v4 failure")

    monkeypatch.setitem(db._MIGRATIONS, 3, fail_v4)
    with pytest.raises(db.MigrationError, match="原库已回滚"):
        db.connect(path)

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
        cols = {row[1] for row in raw.execute("PRAGMA table_info(snapshots)")}
        assert "vanished_count" not in cols
        assert raw.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
    finally:
        raw.close()


class TestISS066ExcludeNamesMigration:
    """ISS-066：v4→v5 迁移给 snapshots 补 exclude_names 列（NOT NULL DEFAULT ''）。

    反例：旧库没 exclude_names 列时，create_snapshot 写入会报缺列；
    修后：迁移给所有既有行填 ''（规范空串），新写入由代码显式提供。
    旧行默认空串保证默认路径零行为变化：v4 旧行（exclude_names=''）与
    v5 新写入的无配置快照同身份可比（same_dataset 验收）。
    """

    def _legacy_v4(self, path: Path) -> None:
        legacy = sqlite3.connect(path)
        try:
            for statement in db._SCHEMA_STATEMENTS:
                legacy.execute(statement)
            legacy.execute(
                "ALTER TABLE snapshots ADD COLUMN min_kb INTEGER"
            )
            legacy.execute(
                "ALTER TABLE snapshots ADD COLUMN collection_status TEXT"
            )
            legacy.execute(
                "ALTER TABLE snapshots ADD COLUMN vanished_count INTEGER NOT NULL DEFAULT 0"
            )
            legacy.execute("PRAGMA user_version=4")
            legacy.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb, min_kb, collection_status, vanished_count) "
                "VALUES ('2026-09-12T12:00:00', '/synthetic/root-a', 1, 0, 0.1, "
                "42, 1024, 'full', 0)"
            )
            legacy.commit()
        finally:
            legacy.close()

    def test_v4_database_migrates_exclude_names_with_default_empty(self, tmp_path):
        path = tmp_path / "v4.db"
        self._legacy_v4(path)

        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == db.SCHEMA_VERSION == 8
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(snapshots)")}
            assert "exclude_names" in cols
            # 旧行通过 NOT NULL DEFAULT '' 自动获得空串。
            rows = conn.execute(
                "SELECT exclude_names FROM snapshots ORDER BY id"
            ).fetchall()
            assert [r["exclude_names"] for r in rows] == [""]
            # 既有列未被改写。
            originals = conn.execute(
                "SELECT min_kb, collection_status, vanished_count FROM snapshots "
                "ORDER BY id"
            ).fetchall()
            assert [(r["min_kb"], r["collection_status"], r["vanished_count"])
                    for r in originals] == [(1024, "full", 0)]
            # 新写入可显式提供 exclude_names（采集时点固化）。
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb, min_kb, collection_status, vanished_count, "
                "exclude_names) VALUES ('2026-09-14T12:00:00', '/synthetic/root-c', "
                "1, 0, 0.1, 7, 1024, 'partial', 0, 'skip.noindex')"
            )
            conn.commit()
            assert conn.execute(
                "SELECT exclude_names FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()["exclude_names"] == "skip.noindex"
        finally:
            conn.close()
        # v4→v5 失败回退时 v4 备份仍可恢复（迁移前快照）。
        backups_v4 = sorted(path.parent.glob(path.name + ".backup-v4-*.sqlite3"))
        assert len(backups_v4) == 1
        backup = sqlite3.connect(backups_v4[0])
        try:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 4
            cols = {row[1] for row in backup.execute("PRAGMA table_info(snapshots)")}
            assert "exclude_names" not in cols
        finally:
            backup.close()

    def test_v4_migration_failure_rolls_back_keeps_exclude_names_uncommitted(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "v4-fail.db"
        self._legacy_v4(path)

        def fail_v5(conn):
            raise sqlite3.OperationalError("injected v5 failure")

        monkeypatch.setitem(db._MIGRATIONS, 4, fail_v5)
        with pytest.raises(db.MigrationError, match="原库已回滚"):
            db.connect(path)

        raw = sqlite3.connect(path)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 4
            cols = {row[1] for row in raw.execute("PRAGMA table_info(snapshots)")}
            assert "exclude_names" not in cols
        finally:
            raw.close()

    def test_v4_to_v5_migration_is_idempotent(self, tmp_path, monkeypatch):
        """迁移幂等：v4 库重跑两次都是同一结果；列只追加一次。"""
        path = tmp_path / "v4-idempotent.db"
        self._legacy_v4(path)
        first = db.connect(path)
        first.close()
        second = db.connect(path)
        try:
            cols = [row[1] for row in second.execute(
                "PRAGMA table_info(snapshots)")]
            assert cols.count("exclude_names") == 1
            assert db.schema_version(second) == db.SCHEMA_VERSION == 8
        finally:
            second.close()


class TestISS116V6Migration:
    @staticmethod
    def _v5(path: Path, *, user_version: int = 5) -> None:
        conn = sqlite3.connect(path)
        try:
            for statement in db._SCHEMA_STATEMENTS:
                conn.execute(statement)
            for ddl in (*db._SNAPSHOT_ALTER_V3, *db._SNAPSHOT_ALTER_V4,
                        *db._SNAPSHOT_ALTER_V5):
                conn.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")
            conn.execute(f"PRAGMA user_version={user_version}")
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb, min_kb, collection_status, vanished_count) "
                "VALUES ('2026-09-28T12:00:00', '/synthetic/root', 2, 1, "
                "0.1, 42, 1024, 'partial', 4)"
            )
            conn.commit()
        finally:
            conn.close()

    def test_v5_to_v6_preserves_old_uncertainty_and_backup(self, tmp_path):
        path = tmp_path / "v5.db"
        self._v5(path)
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
            row = conn.execute("SELECT vanished_count, confirmed_missing_count, "
                               "path_unverified_count FROM snapshots").fetchone()
            assert tuple(row) == (4, None, None)
            assert db._detect_schema_version(conn) == 8
        finally:
            conn.close()
        backups = list(tmp_path.glob("v5.db.backup-v5-*.sqlite3"))
        assert len(backups) == 1
        raw = sqlite3.connect(backups[0])
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
            assert "confirmed_missing_count" not in {r[1] for r in raw.execute(
                "PRAGMA table_info(snapshots)")}
        finally:
            raw.close()

    def test_v5_migration_failure_rolls_back_both_columns(self, tmp_path, monkeypatch):
        path = tmp_path / "v5-fail.db"
        self._v5(path)

        def fail_after_first(conn):
            conn.execute("ALTER TABLE snapshots ADD COLUMN confirmed_missing_count INTEGER")
            raise sqlite3.OperationalError("injected v6 failure")

        monkeypatch.setitem(db._MIGRATIONS, 5, fail_after_first)
        with pytest.raises(db.MigrationError, match="原库已回滚"):
            db.connect(path)
        raw = sqlite3.connect(path)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
            assert "confirmed_missing_count" not in {r[1] for r in raw.execute(
                "PRAGMA table_info(snapshots)")}
            assert raw.execute("SELECT vanished_count FROM snapshots").fetchone()[0] == 4
        finally:
            raw.close()

    def test_v5_shape_with_zero_user_version_migrates_without_guessing(self, tmp_path):
        path = tmp_path / "mismatch.db"
        self._v5(path, user_version=0)
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
            row = conn.execute("SELECT confirmed_missing_count, path_unverified_count "
                               "FROM snapshots").fetchone()
            assert tuple(row) == (None, None)
        finally:
            conn.close()

    def test_v6_shape_with_v5_user_version_is_idempotent(self, tmp_path):
        path = tmp_path / "idempotent.db"
        self._v5(path)
        raw = sqlite3.connect(path)
        try:
            for ddl in db._SNAPSHOT_ALTER_V6:
                raw.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")
            raw.commit()
        finally:
            raw.close()
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
            names = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)")]
            assert names.count("confirmed_missing_count") == 1
            assert names.count("path_unverified_count") == 1
        finally:
            conn.close()


class TestISS035BV7Migration:
    """v6→v7（ISS-035B）：分析两表追加；v0–v6 旧库兼容与失败回滚不放松。"""

    @staticmethod
    def _v6(path: Path, *, user_version: int = 6) -> None:
        conn = sqlite3.connect(path)
        try:
            for statement in db._SCHEMA_STATEMENTS:
                conn.execute(statement)
            for ddl in (*db._SNAPSHOT_ALTER_V3, *db._SNAPSHOT_ALTER_V4,
                        *db._SNAPSHOT_ALTER_V5, *db._SNAPSHOT_ALTER_V6):
                conn.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")
            conn.execute(f"PRAGMA user_version={user_version}")
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb, min_kb, collection_status, vanished_count, "
                "exclude_names, confirmed_missing_count, path_unverified_count) "
                "VALUES ('2026-09-28T12:00:00', '/synthetic/root', 2, 1, 0.1, 42, "
                "1024, 'full', 4, 'skip.me', 0, 0)"
            )
            conn.commit()
        finally:
            conn.close()

    def test_v6_to_v7_appends_analysis_tables_with_backup(self, tmp_path):
        path = tmp_path / "v6.db"
        self._v6(path)
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
            assert db._detect_schema_version(conn) == 8
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")}
            assert {"analysis_runs", "agent_analyses"} <= tables
            # 既有行原样保留，不迁移不重写。
            row = conn.execute("SELECT total_kb, min_kb FROM snapshots").fetchone()
            assert tuple(row) == (42, 1024)
            # 空表创建：分析功能此前未上线，不存在待迁移数据。
            assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM agent_analyses").fetchone()[0] == 0
        finally:
            conn.close()
        backups = list(tmp_path.glob("v6.db.backup-v6-*.sqlite3"))
        assert len(backups) == 1
        raw = sqlite3.connect(backups[0])
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 6
            names = {r[0] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "analysis_runs" not in names and "agent_analyses" not in names
        finally:
            raw.close()

    def test_v6_migration_failure_rolls_back_partial_tables(self, tmp_path, monkeypatch):
        path = tmp_path / "v6-fail.db"
        self._v6(path)

        real = db._migrate_v6

        def fail_after_first(conn):
            conn.execute(db._ANALYSIS_TABLE_STATEMENTS[0])
            raise sqlite3.OperationalError("injected v7 failure")

        monkeypatch.setitem(db._MIGRATIONS, 6, fail_after_first)
        with pytest.raises(db.MigrationError, match="原库已回滚"):
            db.connect(path)
        raw = sqlite3.connect(path)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 6
            names = {r[0] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            # 建到一半的表随事务回滚，不留半成品。
            assert "analysis_runs" not in names and "agent_analyses" not in names
            assert raw.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
        finally:
            raw.close()
        # 修复后（真实迁移函数）可重试接续。
        monkeypatch.setitem(db._MIGRATIONS, 6, real)
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
        finally:
            conn.close()

    def test_v7_shape_with_v6_user_version_is_idempotent(self, tmp_path):
        path = tmp_path / "idempotent-v7.db"
        self._v6(path)
        raw = sqlite3.connect(path)
        try:
            for statement in db._ANALYSIS_TABLE_STATEMENTS:
                raw.execute(statement)
            raw.commit()
        finally:
            raw.close()
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
            names = [r[1] for r in conn.execute("PRAGMA table_info(analysis_runs)")]
            assert names.count("job_id") == 1
        finally:
            conn.close()

    def test_v7_shape_with_zero_user_version_is_detected_not_rebuilt(self, tmp_path):
        path = tmp_path / "mismatch-v7.db"
        self._v6(path, user_version=0)
        raw = sqlite3.connect(path)
        try:
            for statement in db._ANALYSIS_TABLE_STATEMENTS:
                raw.execute(statement)
            raw.commit()
        finally:
            raw.close()
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
            assert conn.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
        finally:
            conn.close()

    def test_agent_analyses_fk_not_cascaded(self, tmp_path):
        """删除 analysis_runs 行不级联删证据：显式先删关联行才可能删除。"""
        conn = db.connect(tmp_path / "fk.db")
        try:
            conn.execute(
                "INSERT INTO analysis_runs(job_id, a_snapshot_id, b_snapshot_id, "
                "request_digest, facts_digest, prompt_version, idempotency_key, "
                "runtime_id, runtime_executable, settings_revision, "
                "consent_revision, status, owner_id, created_at) "
                "VALUES ('j-1', 1, 2, 'rd', 'fd', 'pv', 'ik', 'claude-code', "
                "'/bin/x', 0, 0, 'succeeded', 'o', '2026-09-28T00:00:00')")
            conn.execute(
                "INSERT INTO agent_analyses(job_id, a_snapshot_id, b_snapshot_id, "
                "a_created_at, b_created_at, dataset_root, dataset_min_kb, "
                "dataset_exclude_names, request_digest, facts_digest, "
                "prompt_version, adapter_contract_version, runtime_id, "
                "result_json, facts_json, manifest_json, created_at) "
                "VALUES ('j-1', 1, 2, 't1', 't2', '/r', 1024, '', 'rd', 'fd', "
                "'pv', 1, 'claude-code', '{}', '{}', '{}', '2026-09-28T00:00:00')")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM analysis_runs WHERE job_id='j-1'")
                conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
            assert conn.execute(
                "SELECT COUNT(*) FROM agent_analyses").fetchone()[0] == 1
        finally:
            conn.close()


class TestISS153V8Migration:
    """v7→v8（ISS-153）：卷/范围身份与扫描轮次兼容数据模型。

    反例（先红测后实现）：
    - 全新库与 v7→v8 升级链必须到达相同结构（12 张表）；
    - 升级后条目/时间/旧报告/分析证据逐项保留，旧行三个身份列保持
      NULL（legacy 不补造身份，不与新整盘身份混比）；
    - 注入失败随事务回滚，无半成品列/表，原库可恢复；
    - 并发 connect 竞争迁移由跨进程锁串行、锁内重读版本。
    """

    IDENTITY_TABLES = {
        "scan_scopes", "scan_plans", "scan_rounds", "scan_round_members",
        "container_capacity_samples",
    }

    @staticmethod
    def _v7(path: Path, *, user_version: int = 7) -> None:
        conn = sqlite3.connect(path)
        try:
            for statement in db._SCHEMA_STATEMENTS:
                conn.execute(statement)
            for ddl in (*db._SNAPSHOT_ALTER_V3, *db._SNAPSHOT_ALTER_V4,
                        *db._SNAPSHOT_ALTER_V5, *db._SNAPSHOT_ALTER_V6):
                conn.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")
            for statement in db._ANALYSIS_TABLE_STATEMENTS:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={user_version}")
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb, min_kb, collection_status, vanished_count, "
                "exclude_names, confirmed_missing_count, path_unverified_count) "
                "VALUES ('2026-10-01T09:00:00', '/synthetic/root', 3, 1, 0.5, 4096, "
                "1024, 'partial', 4, 'skip.me', 1, 3)"
            )
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb, min_kb, collection_status, vanished_count, "
                "exclude_names, confirmed_missing_count, path_unverified_count) "
                "VALUES ('2026-10-02T09:00:00', '/synthetic/root', 2, 0, 0.4, 8192, "
                "1024, 'full', 0, 'skip.me', 0, 0)"
            )
            conn.executemany(
                "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
                [(1, "/synthetic/root/a", 2048), (1, "/synthetic/root/b", 1024),
                 (2, "/synthetic/root/a", 4096)],
            )
            conn.execute(
                "INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) "
                "VALUES (1, 500000000000, 123000000000)"
            )
            conn.execute(
                "INSERT INTO scan_runs(started_at, finished_at, status, message) "
                "VALUES ('2026-10-02T09:00:00', '2026-10-02T09:05:00', 'succeeded', NULL)"
            )
            conn.execute(
                "INSERT INTO scan_run_details(run_id, source, phase, owner_id, "
                "owner_pid, owner_started, heartbeat_at, snapshot_id, report_status, "
                "report_path, notification_status, pruned_count) "
                "VALUES (1, 'api', 'collect', 'owner-1', 4242, '2026-10-02T09:00:00', "
                "'2026-10-02T09:01:00', 2, 'written', '/synthetic/reports/2026-10-02.md', "
                "'sent', 0)"
            )
            conn.execute(
                "INSERT INTO analysis_runs(job_id, a_snapshot_id, b_snapshot_id, "
                "request_digest, facts_digest, prompt_version, idempotency_key, "
                "runtime_id, runtime_executable, settings_revision, "
                "consent_revision, status, owner_id, created_at) "
                "VALUES ('job-1', 1, 2, 'rd-1', 'fd-1', 'pv-1', 'ik-1', "
                "'claude-code', '/synthetic/runtime', 3, 5, 'succeeded', 'o-1', "
                "'2026-10-02T10:00:00')"
            )
            conn.execute(
                "INSERT INTO agent_analyses(job_id, a_snapshot_id, b_snapshot_id, "
                "a_created_at, b_created_at, dataset_root, dataset_min_kb, "
                "dataset_exclude_names, request_digest, facts_digest, "
                "prompt_version, adapter_contract_version, runtime_id, "
                "result_json, facts_json, manifest_json, created_at) "
                "VALUES ('job-1', 1, 2, '2026-10-01T09:00:00', "
                "'2026-10-02T09:00:00', '/synthetic/root', 1024, 'skip.me', "
                "'rd-1', 'fd-1', 'pv-1', 1, 'claude-code', '{}', '{}', '{}', "
                "'2026-10-02T10:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

    def test_fresh_database_reaches_v8_with_identity_tables(self, tmp_path):
        """全新库：迁移链 0→8 与 v7 升级链到达同一结构。"""
        path = tmp_path / "fresh.db"
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == db.SCHEMA_VERSION == 8
            assert db._detect_schema_version(conn) == 8
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")}
            assert tables == {
                "snapshots", "entries", "volume_stats", "scan_runs",
                "scan_run_details", "analysis_runs", "agent_analyses",
            } | self.IDENTITY_TABLES
            cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)")]
            assert {"plan_id", "round_id", "metric_version"} <= set(cols)
        finally:
            conn.close()

    def test_v7_upgrade_preserves_everything_item_by_item(self, tmp_path):
        """反例③：升级后条目/时间/旧报告/分析证据逐项保留；旧行身份列 NULL。"""
        path = tmp_path / "v7-upgrade.db"
        self._v7(path)
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        old_report = reports_dir / "2026-10-02.md"
        old_report.write_text("# Fathom 日报 · 2026-10-02\n", encoding="utf-8")

        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
            # 快照逐行逐列保留（含时间原值与 v3–v6 各口径列）。
            snaps = conn.execute(
                "SELECT * FROM snapshots ORDER BY id").fetchall()
            assert [tuple(r)[:7] for r in snaps] == [
                (1, "2026-10-01T09:00:00", "/synthetic/root", 3, 1, 0.5, 4096),
                (2, "2026-10-02T09:00:00", "/synthetic/root", 2, 0, 0.4, 8192),
            ]
            assert [(r["min_kb"], r["collection_status"], r["vanished_count"],
                     r["exclude_names"], r["confirmed_missing_count"],
                     r["path_unverified_count"]) for r in snaps] == [
                (1024, "partial", 4, "skip.me", 1, 3),
                (1024, "full", 0, "skip.me", 0, 0),
            ]
            # 旧行三个身份关联列保持 NULL：legacy 身份不补造，不与新整盘
            # 身份混比（也不补造真实卷 UUID）。
            assert all(r["plan_id"] is None and r["round_id"] is None
                       and r["metric_version"] is None for r in snaps)
            # 条目逐行保留。
            entries = conn.execute(
                "SELECT snapshot_id, path, size_kb FROM entries "
                "ORDER BY snapshot_id, path").fetchall()
            assert [tuple(r) for r in entries] == [
                (1, "/synthetic/root/a", 2048),
                (1, "/synthetic/root/b", 1024),
                (2, "/synthetic/root/a", 4096),
            ]
            assert conn.execute(
                "SELECT status FROM scan_runs").fetchone()[0] == "succeeded"
            assert conn.execute(
                "SELECT report_status FROM scan_run_details"
            ).fetchone()[0] == "written"
            # 分析生命周期与已保存证据逐行保留。
            assert tuple(conn.execute(
                "SELECT total_bytes, free_bytes FROM volume_stats"
            ).fetchone()) == (500000000000, 123000000000)
            assert tuple(conn.execute(
                "SELECT job_id, status FROM analysis_runs"
            ).fetchone()) == ("job-1", "succeeded")
            evidence = conn.execute(
                "SELECT job_id, dataset_root, result_json FROM agent_analyses"
            ).fetchone()
            assert tuple(evidence) == ("job-1", "/synthetic/root", "{}")
        finally:
            conn.close()
        # 旧报告文件原样保留：迁移只动库，不触碰 reports/。
        assert old_report.read_text(encoding="utf-8") == \
            "# Fathom 日报 · 2026-10-02\n"
        # v7 备份保留且为迁移前形态。
        backups = list(tmp_path.glob("v7-upgrade.db.backup-v7-*.sqlite3"))
        assert len(backups) == 1
        raw = sqlite3.connect(backups[0])
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 7
            names = {r[0] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert not (self.IDENTITY_TABLES & names)
            assert raw.execute(
                "SELECT total_kb FROM snapshots WHERE id=2").fetchone()[0] == 8192
        finally:
            raw.close()

    def test_v7_migration_failure_rolls_back_all_ddl(self, tmp_path, monkeypatch):
        """注入失败：三个 ALTER 与五张建表随事务回滚，无半成品；可重试接续。"""
        path = tmp_path / "v8-fail.db"
        self._v7(path)

        def fail_after_partial(conn):
            conn.execute("ALTER TABLE snapshots ADD COLUMN plan_id TEXT")
            conn.execute(db._IDENTITY_TABLE_STATEMENTS[0])
            raise sqlite3.OperationalError("injected v8 failure")

        monkeypatch.setitem(db._MIGRATIONS, 7, fail_after_partial)
        with pytest.raises(db.MigrationError, match="原库已回滚"):
            db.connect(path)

        raw = sqlite3.connect(path)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 7
            cols = {r[1] for r in raw.execute("PRAGMA table_info(snapshots)")}
            assert "plan_id" not in cols and "round_id" not in cols
            names = {r[0] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert not (self.IDENTITY_TABLES & names)
            assert raw.execute(
                "SELECT COUNT(*) FROM agent_analyses").fetchone()[0] == 1
        finally:
            raw.close()
        # 修复后（真实迁移函数）可重试接续到 v8。
        monkeypatch.setitem(db._MIGRATIONS, 7, db._migrate_v7)
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
        finally:
            conn.close()

    def test_concurrent_migration_is_serialized_by_lock(self, tmp_path,
                                                        monkeypatch):
        """并发 connect 竞争 v7→v8：跨进程锁串行，后到者锁内重读不再迁移。"""
        path = tmp_path / "race.db"
        self._v7(path)
        started = threading.Event()
        entered = threading.Event()
        real_v7 = db._migrate_v7

        def slow_v7(conn):
            started.set()
            entered.wait(timeout=5)
            time.sleep(0.2)  # 让第二连接有时间到达迁移锁前
            real_v7(conn)

        monkeypatch.setitem(db._MIGRATIONS, 7, slow_v7)
        results: dict[int, object] = {}

        def worker(tag: int) -> None:
            try:
                conn = db.connect(path)
                results[tag] = db.schema_version(conn)
                results[f"rows-{tag}"] = conn.execute(
                    "SELECT COUNT(*) FROM snapshots").fetchone()[0]
                conn.close()
            except Exception as exc:  # pragma: no cover - 失败时显式可见
                results[tag] = f"error: {exc!r}"

        first = threading.Thread(target=worker, args=(1,))
        first.start()
        assert started.wait(timeout=5)
        second = threading.Thread(target=worker, args=(2,))
        second.start()
        time.sleep(0.2)
        entered.set()
        first.join(timeout=30)
        second.join(timeout=30)
        assert results[1] == db.SCHEMA_VERSION, results
        assert results[2] == db.SCHEMA_VERSION, results
        assert results["rows-1"] == 2 and results["rows-2"] == 2
        # 迁移真实发生过且只发生一次：至少一份迁移前备份，且库内数据完整。
        backups = list(tmp_path.glob("race.db.backup-v7-*.sqlite3"))
        assert len(backups) >= 1

    def test_v8_shape_with_v7_user_version_is_idempotent(self, tmp_path):
        """v8 结构 + user_version=7（手动回退）：只校验不重复建列/表。"""
        path = tmp_path / "idempotent-v8.db"
        self._v7(path)
        raw = sqlite3.connect(path)
        try:
            raw.execute("ALTER TABLE snapshots ADD COLUMN plan_id TEXT")
            raw.execute("ALTER TABLE snapshots ADD COLUMN round_id INTEGER")
            raw.execute("ALTER TABLE snapshots ADD COLUMN metric_version INTEGER")
            for statement in db._IDENTITY_TABLE_STATEMENTS:
                raw.execute(statement)
            raw.commit()
        finally:
            raw.close()
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
            cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)")]
            assert cols.count("plan_id") == 1
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            assert names.count("scan_scopes") == 1
        finally:
            conn.close()

    def test_v8_shape_with_zero_user_version_is_detected_not_rebuilt(
        self, tmp_path
    ):
        path = tmp_path / "mismatch-v8.db"
        self._v7(path, user_version=0)
        raw = sqlite3.connect(path)
        try:
            raw.execute("ALTER TABLE snapshots ADD COLUMN plan_id TEXT")
            raw.execute("ALTER TABLE snapshots ADD COLUMN round_id INTEGER")
            raw.execute("ALTER TABLE snapshots ADD COLUMN metric_version INTEGER")
            for statement in db._IDENTITY_TABLE_STATEMENTS:
                raw.execute(statement)
            raw.commit()
        finally:
            raw.close()
        conn = db.connect(path)
        try:
            assert db.schema_version(conn) == 8
            assert conn.execute(
                "SELECT total_kb FROM snapshots WHERE id=2").fetchone()[0] == 8192
        finally:
            conn.close()

    def test_legacy_rows_keep_null_identity_after_upgrade(self, tmp_path):
        """升级后旧行不参与新身份分组：plan_id NULL 与任何 plan 都不可比。"""
        path = tmp_path / "legacy-null.db"
        self._v7(path)
        conn = db.connect(path)
        try:
            scope = scanner.ensure_scan_scope(
                conn, "apfs-volume:uuid-X", "startup_volume",
                container_id="apfs-container:C")
            plan = scanner.ensure_scan_plan(
                conn, scope, "/synthetic/root", 1, 1024, "skip.me")
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb, min_kb, collection_status, vanished_count, "
                "exclude_names, confirmed_missing_count, path_unverified_count, "
                "plan_id, metric_version) "
                "VALUES ('2026-10-03T09:00:00', '/synthetic/root', 1, 0, 0.1, "
                "100, 1024, 'full', 0, 'skip.me', 0, 0, ?, 1)", (plan,))
            conn.commit()
            legacy = conn.execute(
                "SELECT * FROM snapshots WHERE id=1").fetchone()
            branded = conn.execute(
                "SELECT * FROM snapshots WHERE id=3").fetchone()
            from fathom import reports
            # 同 root/min_kb/exclude_names 但身份维度不同：legacy 与新身份
            # 行不可比（NULL 不补造、不混比）。
            assert not reports.same_dataset(legacy, branded)
            rows = reports.find_same_dataset_snapshot_rows(
                conn, reports.dataset_identity(legacy))
            assert [r["id"] for r in rows] == [1, 2]
            assert reports.find_same_dataset_predecessor(conn, 3) is None
        finally:
            conn.close()
