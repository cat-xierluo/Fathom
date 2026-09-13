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
        assert db.schema_version(conn) == 1
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables == {"snapshots", "entries", "volume_stats", "scan_runs"}
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
