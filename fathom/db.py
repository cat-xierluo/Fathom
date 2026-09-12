"""SQLite 数据层：连接管理与 schema。

表结构（见 docs/ARCHITECTURE.md）：
- snapshots   一次扫描的元信息（时间、根路径、目录数、无权限数、耗时、总量）
- entries     每个快照下 >= MIN_DIR_KB 的目录大小（差分的核心数据）
- volume_stats 每个快照对应的卷容量（总量/剩余），用于整体趋势图
- scan_runs   API 触发的手动扫描任务状态
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,          -- ISO 本地时间，如 2026-09-12T12:00:03
    root         TEXT NOT NULL,
    dir_count    INTEGER NOT NULL,       -- du 输出的目录总数（含小目录）
    denied_count INTEGER NOT NULL,       -- 无权限/读取失败的目录数
    du_seconds   REAL NOT NULL,
    total_kb     INTEGER NOT NULL        -- 根目录总大小
);

CREATE TABLE IF NOT EXISTS entries (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    size_kb     INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, path)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS volume_stats (
    snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
    total_bytes INTEGER NOT NULL,
    free_bytes  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,           -- running / done / failed
    message     TEXT
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """打开数据库连接并确保 schema 存在（幂等）。"""
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
