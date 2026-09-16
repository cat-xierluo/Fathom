"""ISS-025：schema 版本、WAL 一致备份与 fail-closed 迁移。"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat

import pytest

from fathom import db


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
        assert tables == {"snapshots", "entries", "volume_stats", "scan_runs",
                          "scan_run_details"}
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
    conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION}")
    conn.commit()
    conn.close()

    with pytest.raises(db.DatabaseOpenError, match="不兼容"):
        db.connect(path)


def test_v3_database_migrates_vanished_count_with_default_zero(tmp_path):
    """ISS-065 + ISS-066：v3→v5 链式迁移给旧快照补 vanished_count + exclude_names。

    ISS-066 落地后 SCHEMA_VERSION=5，v3 库需经 v3→v4→v5 两次迁移。
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
        assert db.schema_version(conn) == db.SCHEMA_VERSION == 5
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
    # v3→v4→v5 失败回退时 v3 备份仍可恢复（迁移前快照）。
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
            assert db.schema_version(conn) == db.SCHEMA_VERSION == 5
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
            assert db.schema_version(second) == db.SCHEMA_VERSION == 5
        finally:
            second.close()
