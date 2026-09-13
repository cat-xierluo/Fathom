"""SQLite 连接、版本化 schema 与 fail-closed 迁移。"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterator

from . import config

SCHEMA_VERSION = 1

_SCHEMA_STATEMENTS = (
    """CREATE TABLE snapshots (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at   TEXT NOT NULL,
        root         TEXT NOT NULL,
        dir_count    INTEGER NOT NULL,
        denied_count INTEGER NOT NULL,
        du_seconds   REAL NOT NULL,
        total_kb     INTEGER NOT NULL
    )""",
    """CREATE TABLE entries (
        snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
        path        TEXT NOT NULL,
        size_kb     INTEGER NOT NULL,
        PRIMARY KEY (snapshot_id, path)
    ) WITHOUT ROWID""",
    """CREATE TABLE volume_stats (
        snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
        total_bytes INTEGER NOT NULL,
        free_bytes  INTEGER NOT NULL
    )""",
    """CREATE TABLE scan_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL,
        message     TEXT
    )""",
)

# Kept as a readable schema reference for tests and diagnostics.
SCHEMA = ";\n\n".join(_SCHEMA_STATEMENTS) + ";\n"

_EXPECTED_COLUMNS = {
    "snapshots": {
        "id", "created_at", "root", "dir_count", "denied_count", "du_seconds", "total_kb"
    },
    "entries": {"snapshot_id", "path", "size_kb"},
    "volume_stats": {"snapshot_id", "total_bytes", "free_bytes"},
    "scan_runs": {"id", "started_at", "finished_at", "status", "message"},
}


class DatabaseOpenError(RuntimeError):
    """数据库损坏或 schema 不可信，Fathom 拒绝继续。"""


class UnsupportedSchemaVersion(DatabaseOpenError):
    """数据库由更高版本 Fathom 创建，当前程序不能安全打开。"""


class MigrationError(DatabaseOpenError):
    """迁移失败；原库事务已回滚，一致备份保留。"""


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    quoted = _quote_identifier(table)
    return {row[1] for row in conn.execute(f"PRAGMA table_info({quoted})")}


def _check_integrity(conn: sqlite3.Connection, *, label: str) -> None:
    try:
        rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.DatabaseError as exc:
        raise DatabaseOpenError(f"{label} 无法读取或已损坏：{exc}") from exc
    if rows != ["ok"]:
        detail = "; ".join(str(value) for value in rows[:4])
        raise DatabaseOpenError(f"{label} 完整性检查失败：{detail}")


def _validate_schema(conn: sqlite3.Connection, *, allow_missing: bool) -> None:
    tables = _user_tables(conn)
    unknown = tables - set(_EXPECTED_COLUMNS)
    if unknown:
        raise DatabaseOpenError(f"数据库包含未知未版本化表：{', '.join(sorted(unknown))}")
    if not allow_missing:
        missing = set(_EXPECTED_COLUMNS) - tables
        if missing:
            raise DatabaseOpenError(f"schema {SCHEMA_VERSION} 缺少表：{', '.join(sorted(missing))}")
    for table in tables:
        actual = _table_columns(conn, table)
        expected = _EXPECTED_COLUMNS[table]
        if actual != expected:
            raise DatabaseOpenError(
                f"表 {table} 结构不兼容（expected={sorted(expected)}, actual={sorted(actual)}）"
            )


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _create_missing_tables(conn: sqlite3.Connection) -> None:
    existing = _user_tables(conn)
    for table, statement in zip(_EXPECTED_COLUMNS, _SCHEMA_STATEMENTS, strict=True):
        if table not in existing:
            conn.execute(statement)


def _migrate_v0(conn: sqlite3.Connection) -> None:
    """把可识别的无版本开发库提升为 v1；不改写既有业务行。"""
    _validate_schema(conn, allow_missing=True)
    _create_missing_tables(conn)


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {0: _migrate_v0}


@contextmanager
def _migration_lock(path: Path) -> Iterator[None]:
    """跨进程串行 schema 检查/迁移，锁文件与数据库位于同一运行根。"""
    lock_path = path.with_name(path.name + ".migration.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _backup_path(path: Path, version: int) -> Path:
    return path.with_name(f"{path.name}.backup-v{version}-{time.time_ns()}.sqlite3")


def _consistent_backup(conn: sqlite3.Connection, path: Path, version: int) -> Path:
    """用 SQLite backup API 捕获主库及已提交 WAL 的同一一致视图。"""
    target = _backup_path(path, version)
    temporary = target.with_suffix(target.suffix + ".tmp")
    # sqlite3.connect 自建文件会受 umask 影响；先以 0600 排他创建，
    # 避免私人目录历史在备份中短暂可被其他本机用户读取。
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    backup = sqlite3.connect(temporary)
    try:
        conn.backup(backup)
        _check_integrity(backup, label="迁移备份")
    except Exception:
        backup.close()
        temporary.unlink(missing_ok=True)
        raise
    backup.close()
    os.replace(temporary, target)
    return target


def _prepare_database(path: Path) -> sqlite3.Connection:
    # timeout applies while another legitimate process holds a short SQLite write lock.
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        version = schema_version(conn)
        if version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"数据库 schema={version}，当前程序只支持到 {SCHEMA_VERSION}；已拒绝降级打开"
            )
        if version == SCHEMA_VERSION:
            _validate_schema(conn, allow_missing=False)
        elif version == 0:
            _check_integrity(conn, label="数据库")
            tables = _user_tables(conn)
            # 空文件/新库不含用户数据，无需生成无意义的迁移备份。
            if tables:
                _validate_schema(conn, allow_missing=True)
                _consistent_backup(conn, path, version)
            try:
                conn.execute("BEGIN IMMEDIATE")
                # 获取写锁后重新读取；另一进程可能已在等待期间完成迁移。
                locked_version = schema_version(conn)
                if locked_version == 0:
                    _MIGRATIONS[0](conn)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    # 结构和版本是同一迁移单元：必须在 commit 前验证，
                    # 否则失败会留下“标成 v1 但缺表”且不可重试的半成品。
                    _validate_schema(conn, allow_missing=False)
                elif locked_version != SCHEMA_VERSION:
                    raise UnsupportedSchemaVersion(
                        f"迁移竞争后 schema={locked_version}，当前支持 {SCHEMA_VERSION}"
                    )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                if isinstance(exc, UnsupportedSchemaVersion):
                    raise
                raise MigrationError(
                    f"数据库 v0→v{SCHEMA_VERSION} 迁移失败；原库已回滚，备份已保留：{exc}"
                ) from exc
        else:  # pragma user_version 不会为负，保留 fail-closed 防御。
            raise UnsupportedSchemaVersion(f"不支持的数据库 schema={version}")

        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except Exception:
        conn.close()
        raise


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """打开数据库；必要时先做一致备份与事务迁移，失败绝不删库重建。"""
    path = Path(db_path or config.DB_PATH).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 已是当前 schema 时走快路径：API 每次查询都会新建连接，
        # 不能为此扫全库 integrity_check 或串行所有读连接。
        current = sqlite3.connect(path, timeout=10)
        current.row_factory = sqlite3.Row
        try:
            version = schema_version(current)
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    f"数据库 schema={version}，当前程序只支持到 {SCHEMA_VERSION}；已拒绝降级打开"
                )
            if version == SCHEMA_VERSION:
                _validate_schema(current, allow_missing=False)
                current.execute("PRAGMA journal_mode=WAL")
                current.execute("PRAGMA foreign_keys=ON")
                return current
        except Exception:
            current.close()
            raise
        current.close()

        # 只有待迁移/新建库需要跨进程串行；锁内重读版本处理竞争。
        with _migration_lock(path):
            return _prepare_database(path)
    except sqlite3.DatabaseError as exc:
        raise DatabaseOpenError(f"数据库无法安全打开：{exc}") from exc
