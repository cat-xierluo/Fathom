"""SQLite 连接、版本化 schema 与 fail-closed 迁移。"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable, Iterator

from . import config

SCHEMA_VERSION = 8

# v8 之后的纯性能增量（ISS-168）：entries 的主键是
# (snapshot_id, path) WITHOUT ROWID，path 不是主键前缀，因此 trend 的
# path 等值查询（fathom/api.py::_trend_points 的
# "SELECT snapshot_id, size_kb FROM entries WHERE path = ?"）用不上主键，
# 只能全表扫描——100 万档大库实测 252ms，索引后为 log N。
#
# 硬约束：纯索引——不加列、不重建表、不迁移数据、不改 SCHEMA_VERSION
# 语义。CREATE INDEX IF NOT EXISTS 命中即空操作，旧库打开时自动补建，
# 对既有数据与行内容零风险（不动任何一行业务行）。
_ENTRIES_PATH_INDEX = "idx_entries_path"
_INDEX_STATEMENTS = (
    f"CREATE INDEX IF NOT EXISTS {_ENTRIES_PATH_INDEX} ON entries(path)",
)

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
    """CREATE TABLE scan_run_details (
        run_id              INTEGER PRIMARY KEY REFERENCES scan_runs(id) ON DELETE CASCADE,
        source              TEXT NOT NULL,
        phase               TEXT NOT NULL,
        owner_id            TEXT NOT NULL,
        owner_pid           INTEGER NOT NULL,
        owner_started       TEXT NOT NULL,
        heartbeat_at        TEXT NOT NULL,
        snapshot_id         INTEGER,
        report_status       TEXT,
        report_path         TEXT,
        notification_status TEXT,
        pruned_count        INTEGER
    )""",
)

# v7（ISS-035B）：分析生命周期与可信解读两表。故意不建对 snapshots 的
# 外键——快照被保留策略/同日替换删除时，生命周期记录与已保存证据必须
# 存活，由读取层把「a/b 原依据缺失」评估为 expired（带原因），绝不级联
# 删除用户可撤销的证据。
#
# agent_analyses 只保存成功且验证通过的解读（失败/取消只有 run 行，
# 无半写正文）；可信正文与 succeeded 状态转换在同一事务提交（manager）。
_ANALYSIS_TABLE_STATEMENTS = (
    """CREATE TABLE analysis_runs (
        job_id             TEXT NOT NULL PRIMARY KEY,
        a_snapshot_id      INTEGER NOT NULL,
        b_snapshot_id      INTEGER NOT NULL,
        request_digest     TEXT NOT NULL,
        facts_digest       TEXT NOT NULL,
        prompt_version     TEXT NOT NULL,
        idempotency_key    TEXT NOT NULL UNIQUE,
        runtime_id         TEXT NOT NULL,
        runtime_executable TEXT NOT NULL,
        runtime_version    TEXT,
        settings_revision  INTEGER NOT NULL,
        consent_revision   INTEGER NOT NULL,
        status             TEXT NOT NULL,
        reason_code        TEXT,
        owner_id           TEXT NOT NULL,
        created_at         TEXT NOT NULL,
        started_at         TEXT,
        finished_at        TEXT,
        duration_ms        INTEGER,
        revoked_at         TEXT
    )""",
    """CREATE TABLE agent_analyses (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id                   TEXT NOT NULL UNIQUE REFERENCES analysis_runs(job_id),
        a_snapshot_id            INTEGER NOT NULL,
        b_snapshot_id            INTEGER NOT NULL,
        a_created_at             TEXT NOT NULL,
        b_created_at             TEXT NOT NULL,
        dataset_root             TEXT NOT NULL,
        dataset_min_kb           INTEGER,
        dataset_exclude_names    TEXT NOT NULL DEFAULT '',
        request_digest           TEXT NOT NULL,
        facts_digest             TEXT NOT NULL,
        prompt_version           TEXT NOT NULL,
        adapter_contract_version INTEGER NOT NULL,
        runtime_id               TEXT NOT NULL,
        runtime_version          TEXT,
        model                    TEXT,
        result_json              TEXT NOT NULL,
        facts_json               TEXT NOT NULL,
        manifest_json            TEXT NOT NULL,
        created_at               TEXT NOT NULL
    )""",
)

# Kept as a readable schema reference for tests and diagnostics.
# 语句保持 v2 形态：全新库由迁移链 0→…→SCHEMA_VERSION 逐级建表并补列，
# 保证"全新建库"与"旧库升级"到达完全相同的最终结构；v7 分析两表由
# _migrate_v6、v8 身份/轮次表由 _migrate_v7 在迁移链末端追加，
# 同样两条路径同构。
SCHEMA = ";\n\n".join(_SCHEMA_STATEMENTS) + ";\n"

# v3（ISS-021）为 snapshots 增加口径/质量元数据；两列均可空——
# 旧记录不补造未知元数据，NULL 表示"该快照未持久化阈值/采集状态"。
_SNAPSHOT_COLUMNS_V2 = (
    ("id", "INTEGER", 0, 1), ("created_at", "TEXT", 1, 0),
    ("root", "TEXT", 1, 0), ("dir_count", "INTEGER", 1, 0),
    ("denied_count", "INTEGER", 1, 0), ("du_seconds", "REAL", 1, 0),
    ("total_kb", "INTEGER", 1, 0),
)
# v3 的 snapshots 列结构：v2 + min_kb + collection_status；
# 用于 _expected_table_info(v=3) 的中间版本校验。
_SNAPSHOT_COLUMNS_V3 = _SNAPSHOT_COLUMNS_V2 + (
    ("min_kb", "INTEGER", 0, 0), ("collection_status", "TEXT", 0, 0),
)
# v4 中间态：v3 + vanished_count；用于迁移链上"已升 v4 但还没升 v5"的校验。
_SNAPSHOT_COLUMNS_V4 = _SNAPSHOT_COLUMNS_V3 + (
    ("vanished_count", "INTEGER", 1, 0),
)
_SNAPSHOT_ALTER_V3 = (
    # ALTER 追加列的 DDL 片段（幂等：迁移前检查列是否已存在）。
    "min_kb INTEGER",            # 入库阈值（KiB）：数据集口径的一部分
    "collection_status TEXT",    # 采集质量：full / partial
)
# v4（ISS-065）为 snapshots 增加 vanished_count：兼容的路径状态未确认总数；
# NOT NULL DEFAULT 0——旧记录通过默认值获得 0，不补造未知元数据，
# 与 v3 的 NULL 语义保持一致（NULL ≠ "0 个消失"，但该值在采集时即
# 固化，无需外部推断，故允许 NOT NULL）。
_SNAPSHOT_ALTER_V4 = ("vanished_count INTEGER NOT NULL DEFAULT 0",)
# v5（ISS-066）为 snapshots 增加 exclude_names：本次采集生效的 du -I 掩码
# 规范串（排序去重后 ``;`` 拼接）；持久化以便后续差分按 (root, min_kb,
# exclude_names) 分组——数据集身份升级为三元组。
# NOT NULL DEFAULT ''——旧行（v4 之前无此字段）通过默认获得空串，与
# 新写入的"无配置"快照同身份可比；默认路径零行为变化由测试钉住。
_SNAPSHOT_ALTER_V5 = ("exclude_names TEXT NOT NULL DEFAULT ''",)
_SNAPSHOT_COLUMNS_V5 = _SNAPSHOT_COLUMNS_V4 + (("exclude_names", "TEXT", 1, 0),)
# v6：NULL 代表旧快照的路径原因未知；新快照显式写入两个非负计数。
_SNAPSHOT_ALTER_V6 = (
    "confirmed_missing_count INTEGER",
    "path_unverified_count INTEGER",
)
# v8（ISS-153）为 snapshots 增加三个身份关联列，全部可空——
# 旧行不补造身份：plan_id NULL 表示 legacy 口径（按 (root, min_kb,
# exclude_names) 分组，不与新整盘身份混比）；round_id NULL 表示不属于
# 任何扫描轮次；metric_version NULL 表示计量版本未知。新身份由后续
# 接线（ISS-154）在采集时点显式提供，本版不改 API/协调器，零行为变化。
_SNAPSHOT_ALTER_V8 = (
    "plan_id TEXT",          # 所属规范根计划（scan_plans.plan_id）
    "round_id INTEGER",      # 所属扫描轮次（scan_rounds.id；无外键）
    "metric_version INTEGER",  # 计量版本（参与计划身份的显式列）
)

# v8（ISS-153）：卷/范围身份与扫描轮次兼容数据模型，五张新表。
# 快照仍是单测量来源（不改 entries/snapshots 的测量列）；本组表只承载
# 身份、轮次与整盘容量样本：
# - scan_scopes：范围/卷/容器稳定 ID（scope_id 直接采用 storage 发现的
#   稳定 ID，如 "apfs-volume:<uuid>" / "partition:<uuid>"）；legacy 路径
#   不登记本表——旧行身份保持 NULL，不补造真实卷 UUID。
# - scan_plans：规范根计划。数据集身份在新口径下 = 计划（范围 + 规范根 +
#   计量版本 + 阈值 + 排除掩码），内容派生稳定 ID，同计划幂等。
# - scan_rounds / scan_round_members：扫描轮次与成员关联，各自时间/状态。
#   成员对快照的引用 ON DELETE SET NULL：快照被保留策略/同日替换淘汰时
#   引用自动解除，历史行保留，由写入层把 snapshot_status 显式置
#   'expired'（不靠 join 失败隐式发现）。
# - container_capacity_samples：容器容量样本另存来源/时间——整盘容量
#   只能来自容器级发现，不借某目录根 statvfs 冒充（round_id 无外键：
#   诊断样本不阻塞任何轮次清理）。
# 本组新表没有任何外键指向 analysis_runs/agent_analyses；v7 起的保留
# 不变量不变：已保存 AI 证据不随快照淘汰级联删除。
_IDENTITY_TABLE_STATEMENTS = (
    """CREATE TABLE scan_scopes (
        scope_id        TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        container_id    TEXT,
        volume_group_id TEXT,
        device_id       TEXT,
        mount_path      TEXT,
        display_name    TEXT,
        created_at      TEXT NOT NULL,
        last_seen_at    TEXT
    )""",
    """CREATE TABLE scan_plans (
        plan_id         TEXT PRIMARY KEY,
        scope_id        TEXT NOT NULL REFERENCES scan_scopes(scope_id),
        canonical_root  TEXT NOT NULL,
        metric_version  INTEGER NOT NULL,
        min_kb          INTEGER NOT NULL,
        exclude_names   TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL,
        UNIQUE (scope_id, canonical_root, metric_version, min_kb, exclude_names)
    )""",
    """CREATE TABLE scan_rounds (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL,
        message     TEXT
    )""",
    """CREATE TABLE scan_round_members (
        round_id        INTEGER NOT NULL REFERENCES scan_rounds(id) ON DELETE CASCADE,
        seq             INTEGER NOT NULL,
        plan_id         TEXT,
        scope_id        TEXT,
        snapshot_id     INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
        snapshot_status TEXT,
        status          TEXT NOT NULL,
        started_at      TEXT,
        finished_at     TEXT,
        PRIMARY KEY (round_id, seq)
    )""",
    """CREATE TABLE container_capacity_samples (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        container_id TEXT NOT NULL,
        total_bytes  INTEGER,
        free_bytes   INTEGER,
        source       TEXT NOT NULL,
        sampled_at   TEXT NOT NULL,
        round_id     INTEGER
    )""",
)

# v6/v7 共享的快照列结构；v8 在其上追加三个可空身份关联列。
_SNAPSHOT_COLUMNS_V6 = _SNAPSHOT_COLUMNS_V5 + (
    ("confirmed_missing_count", "INTEGER", 0, 0),
    ("path_unverified_count", "INTEGER", 0, 0),
)
_SNAPSHOT_COLUMNS_V8 = _SNAPSHOT_COLUMNS_V6 + (
    ("plan_id", "TEXT", 0, 0), ("round_id", "INTEGER", 0, 0),
    ("metric_version", "INTEGER", 0, 0),
)

_EXPECTED_TABLE_INFO = {
    # (name, declared type, notnull, primary-key order)
    # v8 终态：snapshots 携带三个身份关联列；中间态由
    # _EXPECTED_TABLE_INFO_V6/_V7 覆盖 snapshots 列集。
    "snapshots": _SNAPSHOT_COLUMNS_V8,
    "entries": (
        ("snapshot_id", "INTEGER", 1, 1), ("path", "TEXT", 1, 2),
        ("size_kb", "INTEGER", 1, 0),
    ),
    "volume_stats": (
        ("snapshot_id", "INTEGER", 0, 1), ("total_bytes", "INTEGER", 1, 0),
        ("free_bytes", "INTEGER", 1, 0),
    ),
    "scan_runs": (
        ("id", "INTEGER", 0, 1), ("started_at", "TEXT", 1, 0),
        ("finished_at", "TEXT", 0, 0), ("status", "TEXT", 1, 0),
        ("message", "TEXT", 0, 0),
    ),
    "scan_run_details": (
        ("run_id", "INTEGER", 0, 1), ("source", "TEXT", 1, 0),
        ("phase", "TEXT", 1, 0), ("owner_id", "TEXT", 1, 0),
        ("owner_pid", "INTEGER", 1, 0), ("owner_started", "TEXT", 1, 0),
        ("heartbeat_at", "TEXT", 1, 0), ("snapshot_id", "INTEGER", 0, 0),
        ("report_status", "TEXT", 0, 0), ("report_path", "TEXT", 0, 0),
        ("notification_status", "TEXT", 0, 0), ("pruned_count", "INTEGER", 0, 0),
    ),
}

# v7 形态 = v6 基础 + 分析两表；v6 及更早版本的期望结构不含它们，
# 供迁移链中间状态校验（v7 表在 _migrate_v6 中创建）。
_ANALYSIS_TABLE_INFO = {
    "analysis_runs": (
        ("job_id", "TEXT", 1, 1), ("a_snapshot_id", "INTEGER", 1, 0),
        ("b_snapshot_id", "INTEGER", 1, 0), ("request_digest", "TEXT", 1, 0),
        ("facts_digest", "TEXT", 1, 0), ("prompt_version", "TEXT", 1, 0),
        ("idempotency_key", "TEXT", 1, 0), ("runtime_id", "TEXT", 1, 0),
        ("runtime_executable", "TEXT", 1, 0), ("runtime_version", "TEXT", 0, 0),
        ("settings_revision", "INTEGER", 1, 0), ("consent_revision", "INTEGER", 1, 0),
        ("status", "TEXT", 1, 0), ("reason_code", "TEXT", 0, 0),
        ("owner_id", "TEXT", 1, 0), ("created_at", "TEXT", 1, 0),
        ("started_at", "TEXT", 0, 0), ("finished_at", "TEXT", 0, 0),
        ("duration_ms", "INTEGER", 0, 0), ("revoked_at", "TEXT", 0, 0),
    ),
    "agent_analyses": (
        ("id", "INTEGER", 0, 1), ("job_id", "TEXT", 1, 0),
        ("a_snapshot_id", "INTEGER", 1, 0), ("b_snapshot_id", "INTEGER", 1, 0),
        ("a_created_at", "TEXT", 1, 0), ("b_created_at", "TEXT", 1, 0),
        ("dataset_root", "TEXT", 1, 0), ("dataset_min_kb", "INTEGER", 0, 0),
        ("dataset_exclude_names", "TEXT", 1, 0),
        ("request_digest", "TEXT", 1, 0), ("facts_digest", "TEXT", 1, 0),
        ("prompt_version", "TEXT", 1, 0),
        ("adapter_contract_version", "INTEGER", 1, 0),
        ("runtime_id", "TEXT", 1, 0), ("runtime_version", "TEXT", 0, 0),
        ("model", "TEXT", 0, 0), ("result_json", "TEXT", 1, 0),
        ("facts_json", "TEXT", 1, 0), ("manifest_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
}
# v7/v8 形态：v7 = v6 基础 + 分析两表（快照列仍为 v6 集合，v8 身份列由
# 7→8 步骤追加）；v8 = v7 + 五张身份/轮次表 + 快照三个身份列。
_EXPECTED_TABLE_INFO_V7 = dict(
    _EXPECTED_TABLE_INFO, snapshots=_SNAPSHOT_COLUMNS_V6, **_ANALYSIS_TABLE_INFO
)

# v8 身份/轮次表的期望列结构（由 _migrate_v7 创建）。
_IDENTITY_TABLE_INFO = {
    "scan_scopes": (
        # TEXT PRIMARY KEY 列的 notnull 与 snapshots.id 同口径报 0。
        ("scope_id", "TEXT", 0, 1), ("kind", "TEXT", 1, 0),
        ("container_id", "TEXT", 0, 0), ("volume_group_id", "TEXT", 0, 0),
        ("device_id", "TEXT", 0, 0), ("mount_path", "TEXT", 0, 0),
        ("display_name", "TEXT", 0, 0), ("created_at", "TEXT", 1, 0),
        ("last_seen_at", "TEXT", 0, 0),
    ),
    "scan_plans": (
        ("plan_id", "TEXT", 0, 1), ("scope_id", "TEXT", 1, 0),
        ("canonical_root", "TEXT", 1, 0), ("metric_version", "INTEGER", 1, 0),
        ("min_kb", "INTEGER", 1, 0), ("exclude_names", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "scan_rounds": (
        ("id", "INTEGER", 0, 1), ("started_at", "TEXT", 1, 0),
        ("finished_at", "TEXT", 0, 0), ("status", "TEXT", 1, 0),
        ("message", "TEXT", 0, 0),
    ),
    "scan_round_members": (
        ("round_id", "INTEGER", 1, 1), ("seq", "INTEGER", 1, 2),
        ("plan_id", "TEXT", 0, 0), ("scope_id", "TEXT", 0, 0),
        ("snapshot_id", "INTEGER", 0, 0), ("snapshot_status", "TEXT", 0, 0),
        ("status", "TEXT", 1, 0), ("started_at", "TEXT", 0, 0),
        ("finished_at", "TEXT", 0, 0),
    ),
    "container_capacity_samples": (
        ("id", "INTEGER", 0, 1), ("container_id", "TEXT", 1, 0),
        ("total_bytes", "INTEGER", 0, 0), ("free_bytes", "INTEGER", 0, 0),
        ("source", "TEXT", 1, 0), ("sampled_at", "TEXT", 1, 0),
        ("round_id", "INTEGER", 0, 0),
    ),
}
_IDENTITY_TABLES = frozenset(_IDENTITY_TABLE_INFO)
_EXPECTED_TABLE_INFO_V8 = dict(
    _EXPECTED_TABLE_INFO, **_ANALYSIS_TABLE_INFO, **_IDENTITY_TABLE_INFO
)


def _expected_table_info(
    version: int,
) -> dict[str, tuple[tuple[str, str, int, int], ...]]:
    """指定版本下每张表的期望列结构。

    v7 增加分析两表（由 6→7 步骤创建）；v8 增加身份/轮次五表与快照三个
    身份列（由 7→8 步骤创建）。v4/v5 保留各自结构，用于迁移链中间状态
    校验；v3 及更早同理。v6/v7 共享同一快照列集。
    """
    if version >= SCHEMA_VERSION:
        return _EXPECTED_TABLE_INFO_V8
    if version >= 7:
        return _EXPECTED_TABLE_INFO_V7
    if version >= 6:
        # v6 形态：v6/v7 快照结构 + 无分析两表（它们由 6→7 步骤创建）。
        return dict(_EXPECTED_TABLE_INFO, snapshots=_SNAPSHOT_COLUMNS_V6)
    if version >= 5:
        return dict(_EXPECTED_TABLE_INFO, snapshots=_SNAPSHOT_COLUMNS_V5)
    if version >= 4:
        return dict(_EXPECTED_TABLE_INFO, snapshots=_SNAPSHOT_COLUMNS_V4)
    if version >= 3:
        return dict(_EXPECTED_TABLE_INFO, snapshots=_SNAPSHOT_COLUMNS_V3)
    info = dict(_EXPECTED_TABLE_INFO, snapshots=_SNAPSHOT_COLUMNS_V2)
    if version <= 1:
        info = {t: cols for t, cols in info.items() if t in _V1_TABLES}
    return info

_EXPECTED_FOREIGN_KEYS = {
    "snapshots": (),
    "entries": (("snapshots", "snapshot_id", "id", "NO ACTION", "CASCADE", "NONE"),),
    "volume_stats": (("snapshots", "snapshot_id", "id", "NO ACTION", "CASCADE", "NONE"),),
    "scan_runs": (),
    "scan_run_details": (
        ("scan_runs", "run_id", "id", "NO ACTION", "CASCADE", "NONE"),
    ),
    "analysis_runs": (),
    "agent_analyses": (
        # 故意不级联：analysis_runs 行删除（保留策略）须先显式删除关联
        # agent_analyses 行，避免「清生命周期顺手删证据」的隐式通道。
        ("analysis_runs", "job_id", "job_id", "NO ACTION", "NO ACTION", "NONE"),
    ),
    # v8（ISS-153）：身份/轮次表外键。scan_round_members.snapshot_id 用
    # SET NULL——快照淘汰时引用自动解除，成员历史行保留；不设任何指向
    # 分析两表的外键（已保存 AI 证据无级联删除通道）。
    "scan_scopes": (),
    "scan_plans": (
        ("scan_scopes", "scope_id", "scope_id", "NO ACTION", "NO ACTION", "NONE"),
    ),
    "scan_rounds": (),
    "scan_round_members": (
        ("scan_rounds", "round_id", "id", "NO ACTION", "CASCADE", "NONE"),
        ("snapshots", "snapshot_id", "id", "NO ACTION", "SET NULL", "NONE"),
    ),
    "container_capacity_samples": (),
}

_EXPECTED_WITHOUT_ROWID = {"snapshots": 0, "entries": 1, "volume_stats": 0, "scan_runs": 0,
                           "scan_run_details": 0, "analysis_runs": 0, "agent_analyses": 0,
                           "scan_scopes": 0, "scan_plans": 0, "scan_rounds": 0,
                           "scan_round_members": 0, "container_capacity_samples": 0}
_EXPECTED_AUTOINCREMENT = {"snapshots", "scan_runs", "agent_analyses",
                           "scan_rounds", "container_capacity_samples"}

_V1_TABLES = frozenset({"snapshots", "entries", "volume_stats", "scan_runs"})


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


def _table_info(conn: sqlite3.Connection, table: str) -> tuple[tuple[str, str, int, int], ...]:
    quoted = _quote_identifier(table)
    return tuple(
        (row[1], str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in conn.execute(f"PRAGMA table_info({quoted})")
    )


def _foreign_keys(conn: sqlite3.Connection, table: str) -> tuple[tuple[str, ...], ...]:
    quoted = _quote_identifier(table)
    return tuple(sorted(
        (row[2], row[3], row[4], row[5], row[6], row[7])
        for row in conn.execute(f"PRAGMA foreign_key_list({quoted})")
    ))


def _without_rowid(conn: sqlite3.Connection, table: str) -> int:
    row = next(
        (row for row in conn.execute("PRAGMA table_list")
         if row[1] == table and row[2] == "table"),
        None,
    )
    if row is None:
        raise DatabaseOpenError(f"无法读取表 {table} 的 table_list 元数据")
    return int(row[4])


def _uses_autoincrement(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    sql = row[0] if row and row[0] else ""
    # 不比较 sqlite_master 的格式，只识别影响 id 不复用语义的 SQL 关键字。
    return re.search(r"\bAUTOINCREMENT\b", sql, re.IGNORECASE) is not None


def _check_integrity(conn: sqlite3.Connection, *, label: str) -> None:
    try:
        rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.DatabaseError as exc:
        raise DatabaseOpenError(f"{label} 无法读取或已损坏：{exc}") from exc
    if rows != ["ok"]:
        detail = "; ".join(str(value) for value in rows[:4])
        raise DatabaseOpenError(f"{label} 完整性检查失败：{detail}")


def _validate_schema(
    conn: sqlite3.Connection, *, allow_missing: bool, version: int = SCHEMA_VERSION
) -> None:
    tables = _user_tables(conn)
    expected_info = _expected_table_info(version)
    expected_tables = set(expected_info)
    unknown = tables - expected_tables
    if unknown:
        raise DatabaseOpenError(f"数据库包含未知未版本化表：{', '.join(sorted(unknown))}")
    if not allow_missing:
        missing = expected_tables - tables
        if missing:
            raise DatabaseOpenError(f"schema {version} 缺少表：{', '.join(sorted(missing))}")
    for table in tables:
        actual = _table_info(conn, table)
        expected = expected_info[table]
        if actual != expected:
            raise DatabaseOpenError(
                f"表 {table} 结构不兼容（expected={sorted(expected)}, actual={sorted(actual)}）"
            )
        actual_foreign_keys = _foreign_keys(conn, table)
        if actual_foreign_keys != _EXPECTED_FOREIGN_KEYS[table]:
            raise DatabaseOpenError(f"表 {table} 外键/级联结构不兼容")
        if _without_rowid(conn, table) != _EXPECTED_WITHOUT_ROWID[table]:
            raise DatabaseOpenError(f"表 {table} WITHOUT ROWID 结构不兼容")
        if _uses_autoincrement(conn, table) != (table in _EXPECTED_AUTOINCREMENT):
            raise DatabaseOpenError(f"表 {table} AUTOINCREMENT 结构不兼容")


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _create_missing_tables(conn: sqlite3.Connection) -> None:
    existing = _user_tables(conn)
    for table, statement in zip(_EXPECTED_TABLE_INFO, _SCHEMA_STATEMENTS, strict=True):
        if table not in existing:
            conn.execute(statement)


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """幂等补建查询用二级索引（ISS-168），不参与版本号语义。

    纯增量 DDL：只加索引，不加列、不重建表、不迁移数据。已存在时
    CREATE INDEX IF NOT EXISTS 是空操作，因此对「已是当前 schema」的
    库可在每次打开时无副作用地调用——connect() 快路径正是这样补建
    索引的，否则不跑迁移链的老库永远拿不到索引。
    """
    for statement in _INDEX_STATEMENTS:
        conn.execute(statement)


def _migrate_v0(conn: sqlite3.Connection) -> None:
    """把可识别的无版本开发库提升为 v1；不改写既有业务行。

    user_version=0 时，结构可能已是 v3/v4（用户手工改 user_version）；
    按实际列结构推断版本再校验，避免把 v3 结构错认为 v2 后漏 ALTER。
    """
    actual = _detect_schema_version(conn)
    _validate_schema(conn, allow_missing=True, version=actual)
    existing = _user_tables(conn)
    for table, statement in zip(_EXPECTED_TABLE_INFO, _SCHEMA_STATEMENTS, strict=True):
        if table in _V1_TABLES and table not in existing:
            conn.execute(statement)


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """为统一扫描生命周期增加一对一详情表，保留旧 scan_runs 合同。"""
    actual = _detect_schema_version(conn)
    if "scan_run_details" in _user_tables(conn):
        _validate_schema(conn, allow_missing=False, version=actual)
    else:
        _validate_schema(conn, allow_missing=False, version=1)
        conn.execute(_SCHEMA_STATEMENTS[-1])


def _snapshot_column_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute('PRAGMA table_info("snapshots")')
    }


def _detect_schema_version(conn: sqlite3.Connection) -> int:
    """根据实际结构推断 schema 版本。

    旧启发式「scan_run_details 存在即 v2」在后续版本引入新列后失效——
    snapshots 才是版本演化的承载列；v8 的承载结构是身份/轮次五表，
    v7 的承载结构是分析两表。判定顺序：
    身份五表齐全 → 8；analysis_runs+agent_analyses → 7；两分类列 → 6；
    exclude_names → 5；vanished_count → 4；min_kb+collection_status → 3；
    scan_run_details 表存在 → 2；只有 v1 表 → 1；空库 → 0。
    """
    tables = _user_tables(conn)
    if not tables:
        return 0
    if _IDENTITY_TABLES <= tables:
        return 8
    if {"analysis_runs", "agent_analyses"} <= tables:
        return 7
    if "snapshots" in tables:
        cols = _snapshot_column_names(conn)
        if {"confirmed_missing_count", "path_unverified_count"} <= cols:
            return 6
        if "exclude_names" in cols:
            return 5
        if "vanished_count" in cols:
            return 4
        if {"min_kb", "collection_status"} <= cols:
            return 3
    if "scan_run_details" in tables:
        return 2
    return 1


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """为同口径差分补快照口径/质量列（ISS-021）。

    只做幂等 ALTER，不改写任何既有行：旧快照的 min_kb/collection_status
    保持 NULL（未持久化过的事实不补造）。列已齐全时（例如 user_version 被
    手动回退）只做结构校验，不重复追加。
    """
    columns = _snapshot_column_names(conn)
    if {"min_kb", "collection_status"} <= columns:
        _validate_schema(conn, allow_missing=False, version=_detect_schema_version(conn))
        return
    _validate_schema(conn, allow_missing=False, version=2)
    for ddl in _SNAPSHOT_ALTER_V3:
        if ddl.split()[0] not in columns:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """为 vanished 路径补快照元数据列（ISS-065）。

    与 v2 迁移同口径：幂等 ALTER，不改写任何既有行。列已齐全时
    （user_version 被手动回退）只做结构校验，不重复追加。旧行
    vanished_count 通过列定义 NOT NULL DEFAULT 0 自动获得 0——
    旧快照当时未持久化 vanished 计数的事实不补造未知值，0
    表示"该快照未采集 vanished 信息"（与 v3 之前 NULL 语义同
    等）。新快照由 create_snapshot 在采集时点显式提供 vanished_count。
    """
    columns = _snapshot_column_names(conn)
    if "vanished_count" in columns:
        _validate_schema(conn, allow_missing=False, version=_detect_schema_version(conn))
        return
    _validate_schema(conn, allow_missing=False, version=3)
    for ddl in _SNAPSHOT_ALTER_V4:
        if ddl.split()[0] not in columns:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """为排除集纳入数据集身份补快照元数据列（ISS-066）。

    与 v3 迁移同口径：幂等 ALTER，不改写任何既有行。列已齐全时
    （user_version 被手动回退）只做结构校验，不重复追加。旧行
    exclude_names 通过列定义 NOT NULL DEFAULT '' 自动获得空串——
    "该快照未持久化 exclude_names"的事实由空串表达，与新写入
    的"无配置"快照同身份可比（same_dataset 三元组测试钉住）。
    新快照由 create_snapshot 在采集时点显式提供规范串（来自
    config.EXCLUDE_NAMES）。
    """
    columns = _snapshot_column_names(conn)
    if "exclude_names" in columns:
        _validate_schema(conn, allow_missing=False, version=_detect_schema_version(conn))
        return
    _validate_schema(conn, allow_missing=False, version=4)
    for ddl in _SNAPSHOT_ALTER_V5:
        if ddl.split()[0] not in columns:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """追加路径状态分类，旧行保持 NULL，不猜测历史原因。"""
    columns = _snapshot_column_names(conn)
    if {"confirmed_missing_count", "path_unverified_count"} <= columns:
        # 与 v2/v3/v4 的幂等分支同口径：按实际结构推断版本校验，不硬编码——
        # v7 起实际结构可能已是更高版本（如 user_version 被手动清零的 v7 库）。
        _validate_schema(conn, allow_missing=False, version=_detect_schema_version(conn))
        return
    _validate_schema(conn, allow_missing=False, version=5)
    for ddl in _SNAPSHOT_ALTER_V6:
        conn.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v6→v7（ISS-035B）：追加分析生命周期（analysis_runs）与可信解读
    （agent_analyses）两表。

    与既有迁移同口径：幂等——两表已齐全（如 user_version 被手动回退）时
    只做结构校验，不重复建表；CREATE TABLE 在迁移事务内执行，任何失败
    随事务回滚（含建到一半的表），既有数据不受影响。两表为空表创建，
    不迁移任何历史行——分析功能此前未上线，不存在待迁移数据。"""
    tables = _user_tables(conn)
    if {"analysis_runs", "agent_analyses"} <= tables:
        _validate_schema(conn, allow_missing=False,
                         version=_detect_schema_version(conn))
        return
    _validate_schema(conn, allow_missing=False, version=6)
    for statement in _ANALYSIS_TABLE_STATEMENTS:
        conn.execute(statement)


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """v7→v8（ISS-153）：卷/范围身份与扫描轮次兼容数据模型。

    snapshots 追加三个可空关联列（plan_id/round_id/metric_version）+
    五张身份/轮次新表。与既有迁移同口径：幂等——列与表已齐全（如
    user_version 被手动回退）时只做结构校验，不重复追加；全部 DDL 在
    迁移事务内执行，任何失败随事务回滚（含建到一半的表），既有数据
    不受影响。旧行三个新列保持 NULL——legacy 身份不补造（不造真实卷
    UUID，不与新整盘身份混比），新身份由后续接线在采集时点显式提供，
    本步不迁移、不改写任何历史行。"""
    tables = _user_tables(conn)
    columns = _snapshot_column_names(conn)
    if (_IDENTITY_TABLES <= tables
            and {"plan_id", "round_id", "metric_version"} <= columns):
        _validate_schema(conn, allow_missing=False,
                         version=_detect_schema_version(conn))
        return
    _validate_schema(conn, allow_missing=False, version=7)
    for ddl in _SNAPSHOT_ALTER_V8:
        if ddl.split()[0] not in columns:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {ddl}")
    for statement in _IDENTITY_TABLE_STATEMENTS:
        conn.execute(statement)


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    0: _migrate_v0,
    1: _migrate_v1,
    2: _migrate_v2,
    3: _migrate_v3,
    4: _migrate_v4,
    5: _migrate_v5,
    6: _migrate_v6,
    7: _migrate_v7,
}


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


def consistent_backup(db_path: Path | None = None) -> Path:
    """公共一致备份入口（ISS-040C）：checkpoint + backup API + 完整性校验。

    供生产升级协调器（``fathom.upgrade``）在停写与旧 helper 退出之后调用：
    独立连接上先 ``PRAGMA wal_checkpoint(TRUNCATE)`` 把已提交 WAL 落进主
    文件，再复用 ``_consistent_backup``（SQLite backup API 捕获主库与 WAL
    的同一一致视图 + ``PRAGMA integrity_check`` 校验；禁止文件拷贝语义）。
    返回备份文件路径（0600，与库同目录）；失败时临时文件已被清理并向上
    抛出，原库不受影响。
    """
    path = Path(db_path or config.DB_PATH).expanduser().resolve(strict=False)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return _consistent_backup(conn, path, schema_version(conn))
    finally:
        conn.close()


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
            _ensure_indexes(conn)
        elif version < SCHEMA_VERSION:
            _check_integrity(conn, label="数据库")
            tables = _user_tables(conn)
            # 空文件/新库不含用户数据，无需生成无意义的迁移备份。
            if tables:
                # 以实际列结构推断版本（user_version 可能与结构不同步），
                # 旧启发式「scan_run_details 在即 v2」在 v3/v4 引入新列后
                # 不再适用——snapshots 才是版本演化的承载列。
                preflight_version = _detect_schema_version(conn)
                _validate_schema(conn, allow_missing=(version == 0),
                                 version=preflight_version)
                _consistent_backup(conn, path, version)
            try:
                conn.execute("BEGIN IMMEDIATE")
                # 获取写锁后重新读取；另一进程可能已在等待期间完成迁移。
                locked_version = schema_version(conn)
                if locked_version < SCHEMA_VERSION:
                    while locked_version < SCHEMA_VERSION:
                        _MIGRATIONS[locked_version](conn)
                        locked_version += 1
                        conn.execute(f"PRAGMA user_version={locked_version}")
                    # 结构和版本是同一迁移单元：必须在 commit 前验证，
                    # 否则失败会留下“标成 v1 但缺表”且不可重试的半成品。
                    _validate_schema(conn, allow_missing=False)
                    # 纯索引在同一事务内幂等补建：新建库与旧库升级两条
                    # 路径到达完全相同的最终结构（ISS-168）。
                    _ensure_indexes(conn)
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
                    f"数据库 v{version}→v{SCHEMA_VERSION} 迁移失败；"
                    f"原库已回滚，备份已保留：{exc}"
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
                # 已是当前 schema 也不跑迁移链，老库仍可能缺 ISS-168 的
                # 纯索引；CREATE INDEX IF NOT EXISTS 命中即空操作，
                # 因此快路径上无副作用，且不必串行化所有读连接。
                _ensure_indexes(current)
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
