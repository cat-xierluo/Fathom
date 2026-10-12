"""ISS-168：entries(path) 二级索引 —— /api/trend 路径等值查询索引化。

反例来源（perf 标定事实，M Code 审计线 100 万档大库实测）：trend 的
path 等值查询在无二级索引时对 entries 全表扫描 252ms，索引后为 log N，
差约 3 个数量级。查询点在 fathom/api.py::_trend_points：

    SELECT snapshot_id, size_kb FROM entries WHERE path = ?

entries 的主键是 (snapshot_id, path) WITHOUT ROWID —— path 不是主键前缀，
等值谓词用不上主键，只能全表扫描。

硬约束（测试同时钉住）：纯索引 —— 不加列、不重建表、不迁移数据、
不改 schema 版本号语义。SCHEMA_VERSION 保持 8，索引靠幂等
CREATE INDEX IF NOT EXISTS 在「新建库」与「先建旧库再打开」两条路径
上自动补建，旧库零风险。

全部合成数据 + 隔离临时目录，隔离边界同 tests/test_db_migrations.py。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import pytest

from fathom import config, db

ROOT = "/synthetic/trend-path-index-root"
TARGET = ROOT + "/a/big.bin"
SQL = "SELECT snapshot_id, size_kb FROM entries WHERE path = ?"
INDEX_NAME = "idx_entries_path"


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """与 tests/test_trend_anchor.py 同口径：运行根/扫描根一律隔离，
    绝不触碰真实 HOME（红线：不触发真实扫描、不碰生产 data/）。"""
    runtime_dir = tmp_path / "runtime"
    scan_root = tmp_path / "scanroot"
    scan_root.mkdir(parents=True)
    monkeypatch.delenv("FATHOM_DB", raising=False)
    monkeypatch.setenv("FATHOM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("FATHOM_SCAN_ROOT", str(scan_root))
    monkeypatch.setattr(config, "DATA_DIR", runtime_dir / "data")
    monkeypatch.setattr(config, "DB_PATH", runtime_dir / "data" / "fathom.db")
    monkeypatch.setattr(config, "DEFAULT_ROOT", scan_root)


@pytest.fixture(autouse=True)
def _reset_pending_index_registry():
    """待补建登记簿是进程内全局状态；逐例隔离，避免跨例污染与顺序依赖。"""
    with db._PENDING_INDEX_LOCK:
        db._PENDING_INDEXES.clear()
        db._PENDING_INDEX_LOGGED.clear()
    yield
    with db._PENDING_INDEX_LOCK:
        db._PENDING_INDEXES.clear()
        db._PENDING_INDEX_LOGGED.clear()


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='entries'")
    }


def _plan(conn: sqlite3.Connection, target: str = TARGET) -> str:
    return " | ".join(
        str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + SQL, (target,))
    )


def _seed(conn: sqlite3.Connection, *, snapshots: int = 3) -> None:
    """造多快照 + 多路径的 entries 行（直连写表，不经 du）。"""
    for day in range(snapshots):
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb) VALUES (?,?,?,?,?,?)",
            (f"2026-10-0{day + 1}T12:00:00", ROOT, 4, 0, 0.0, 1024),
        )
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
            [
                (sid, TARGET, 100 + day),
                (sid, ROOT + f"/a/other-{day}.bin", 10 + day),
                (sid, ROOT + f"/c/other-{day}.bin", 20 + day),
            ],
        )
    conn.commit()


def _drop_index(conn: sqlite3.Connection) -> None:
    """模拟「索引引入之前的老库」：v8 结构齐全但没有该索引。"""
    conn.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    conn.commit()


def test_new_database_has_entries_path_index(tmp_path):
    """路径①：全新建库即带索引（不依赖任何迁移步骤）。"""
    conn = db.connect(tmp_path / "fresh.db")
    try:
        assert INDEX_NAME in _index_names(conn)
        assert db.schema_version(conn) == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_existing_v8_database_gains_path_index_on_open(tmp_path):
    """路径②：先建 v8 旧库（无索引）再打开 → 自动补建，且不动版本号。

    这是本卡最关键的一条：connect() 对已是 SCHEMA_VERSION 的库走快路径
    （不跑迁移链），若索引只挂在迁移链上，生产 100 万档老库永远不会补建。
    """
    path = tmp_path / "existing-v8.db"
    conn = db.connect(path)
    try:
        _drop_index(conn)
        assert INDEX_NAME not in _index_names(conn)
    finally:
        conn.close()

    reopened = db.connect(path)
    try:
        assert INDEX_NAME in _index_names(reopened), "旧库打开必须自动补建纯索引"
        assert db.schema_version(reopened) == db.SCHEMA_VERSION, "纯索引不改版本号语义"
    finally:
        reopened.close()


def test_legacy_v0_database_migration_creates_path_index(tmp_path):
    """路径③：v0 无版本老库经迁移链升到 v8 → 索引同样到位，数据不丢。"""
    path = tmp_path / "legacy-v0.db"
    legacy = sqlite3.connect(path)
    try:
        legacy.execute("PRAGMA journal_mode=WAL")
        for statement in db._SCHEMA_STATEMENTS:
            legacy.execute(statement)
        legacy.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb) VALUES ('2026-10-01T12:00:00', ?, 1, 0, 0.1, 42)",
            (ROOT,),
        )
        legacy.commit()
        assert legacy.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        legacy.close()

    conn = db.connect(path)
    try:
        assert INDEX_NAME in _index_names(conn)
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        # 纯索引不迁移、不改写任何既有行
        assert conn.execute("SELECT total_kb FROM snapshots").fetchone()[0] == 42
    finally:
        conn.close()


def test_trend_query_plan_uses_path_index(tmp_path):
    """红断言：EXPLAIN QUERY PLAN 必须命中 idx_entries_path。"""
    conn = db.connect(tmp_path / "plan.db")
    try:
        _seed(conn)
        plan = _plan(conn)
        assert "USING INDEX " + INDEX_NAME in plan, (
            f"trend path 等值查询未走 {INDEX_NAME}，实际计划：{plan}"
        )
        assert "SCAN entries" not in plan, f"仍为全表扫描：{plan}"
    finally:
        conn.close()


def test_trend_query_plan_uses_path_index_after_legacy_open(tmp_path):
    """旧库补建后，计划器同样改用索引（不是只在新库生效）。"""
    path = tmp_path / "plan-legacy.db"
    conn = db.connect(path)
    try:
        _seed(conn)
        _drop_index(conn)
    finally:
        conn.close()

    reopened = db.connect(path)
    try:
        _seed(reopened)
        plan = _plan(reopened)
        assert "USING INDEX " + INDEX_NAME in plan, f"实际计划：{plan}"
    finally:
        reopened.close()


def test_trend_query_plan_without_index_is_full_scan(tmp_path):
    """反例自证：索引缺失时计划确为全表扫描 —— 上一条断言不是恒真。

    这是红阶段的可复现证据：删掉索引后同一断言必须失败。
    """
    conn = db.connect(tmp_path / "counterexample.db")
    try:
        _seed(conn)
        _drop_index(conn)
        plan = _plan(conn)
        assert "USING INDEX " + INDEX_NAME not in plan, "索引已删，计划不应再命中"
        assert "SCAN entries" in plan, f"缺索引时应为全表扫描，实际：{plan}"
    finally:
        conn.close()


def test_index_lookup_returns_same_rows_with_or_without_index(tmp_path):
    """行为回归：索引只影响取数方式，查询语义与返回行完全一致。"""
    path = tmp_path / "rows.db"
    conn = db.connect(path)
    try:
        _seed(conn, snapshots=3)
        indexed = conn.execute(SQL, (TARGET,)).fetchall()
        _drop_index(conn)
        scanned = conn.execute(SQL, (TARGET,)).fetchall()
        assert [tuple(r) for r in indexed] == [tuple(r) for r in scanned]
        assert sorted(int(r[1]) for r in scanned) == [100, 101, 102]
    finally:
        conn.close()


def test_ensure_index_is_idempotent_across_repeated_opens(tmp_path):
    """重复打开/重复补建不报错、不重复建、不产生第二个同名索引。"""
    path = tmp_path / "idempotent.db"
    for _ in range(3):
        conn = db.connect(path)
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON entries(path)")
            names = [
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='entries' AND name=?", (INDEX_NAME,))
            ]
            assert names == [INDEX_NAME]
        finally:
            conn.close()
    assert db.schema_version(db.connect(path)) == db.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# ISS-173：建索引 DDL 的写锁竞争收口
#
# 反例来源（上游终审百万 entries 实测）：旧库缺 idx_entries_path 且另一连接
# 持 BEGIN IMMEDIATE 写锁时，connect() 快路径上的 CREATE INDEX 要抢写锁，
# 10.39s 后以 "database is locked" 失败——扫描写锁被转化为用户可见的打开
# 失败。修法：快路径先纯读廉价检查，真要建时才给 250ms 短预算试探；抢不到
# 就跳过本次补建、登记待办、记一次日志，让打开照常成功，下次连接重试。
# ---------------------------------------------------------------------------


def _legacy_db_without_index(tmp_path, name: str = "legacy-locked.db") -> Path:
    """造「结构是当前 schema 但缺索引」的老库：快路径的唯一补建入口。"""
    path = tmp_path / name
    conn = db.connect(path)
    try:
        _seed(conn)
        _drop_index(conn)
    finally:
        conn.close()
    return path


def _hold_write_lock(path: Path):
    """持 BEGIN IMMEDIATE 写锁的独立连接，模拟扫描协调器的写事务。"""
    holder = sqlite3.connect(path, timeout=10)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO scan_runs(started_at, status) VALUES (?, ?)",
        ("2026-10-06T12:00:00", "running"),
    )
    return holder


def test_bare_index_ddl_under_write_lock_is_blocked(tmp_path):
    """反例自证：写锁持锁时裸 DDL 确实抢不到写锁 —— 让步分支不是防御性空转。

    不给断言设时间上限（那会让慢机偶发红），只断言 DDL 确实因写锁失败。
    """
    path = _legacy_db_without_index(tmp_path)
    holder = _hold_write_lock(path)
    try:
        victim = sqlite3.connect(path, timeout=1)
        try:
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                victim.execute(f"CREATE INDEX {INDEX_NAME} ON entries(path)")
            assert "is locked" in str(excinfo.value).lower()
        finally:
            victim.close()
    finally:
        holder.rollback()
        holder.close()


def test_connect_succeeds_quickly_when_index_build_loses_write_lock(tmp_path):
    """核心断言：持写锁 + 缺索引 → connect() 快速成功，绝不 10s 后失败。

    这是 ISS-173 的红线：打开成败不得取决于后台索引补建能否抢到写锁。
    """
    path = _legacy_db_without_index(tmp_path)
    holder = _hold_write_lock(path)
    try:
        started = time.perf_counter()
        conn = db.connect(path)          # 旧实现在这里 10.39s 后抛 DatabaseOpenError
        elapsed = time.perf_counter() - started
        try:
            assert elapsed < 3.0, (
                f"让步路径必须在短预算内返回，实际 {elapsed:.2f}s"
                f"（阈值取 3s 兼顾慢机，仍远低于 10s busy_timeout）"
            )
            # 真的让步了：本次没有建索引
            assert INDEX_NAME not in _index_names(conn)
            # 让步后连接依然可用：查询照常返回正确行
            rows = conn.execute(SQL, (TARGET,)).fetchall()
            assert [int(r[1]) for r in rows] == [100, 101, 102]
        finally:
            conn.close()
    finally:
        holder.rollback()
        holder.close()


def test_lost_index_build_is_logged_once_with_diagnosis(tmp_path, caplog):
    """失败诊断：让步必须留一条 warning，含索引名与原始错误，且只记一次。"""
    path = _legacy_db_without_index(tmp_path)
    holder = _hold_write_lock(path)
    try:
        with caplog.at_level(logging.WARNING, logger="fathom.db"):
            for _ in range(3):
                db.connect(path).close()  # 连续三次抢不到写锁
    finally:
        holder.rollback()
        holder.close()

    messages = [r.getMessage() for r in caplog.records
                if r.name == "fathom.db" and INDEX_NAME in r.getMessage()]
    assert len(messages) == 1, f"让步诊断只应记一次，实际 {len(messages)} 次"
    assert "写锁" in messages[0]
    assert "database is locked" in messages[0].lower()


def test_pending_index_build_retried_and_cleared_after_unlock(tmp_path):
    """待办语义：让步登记入册，解锁后的下一次连接自动补建并清空登记。"""
    path = _legacy_db_without_index(tmp_path)
    holder = _hold_write_lock(path)
    try:
        db.connect(path).close()
        assert INDEX_NAME in db.pending_index_builds(), "抢不到写锁应登记待办"
    finally:
        holder.rollback()
        holder.close()

    conn = db.connect(path)
    try:
        assert INDEX_NAME in _index_names(conn), "解锁后重连必须补建待办索引"
        assert db.pending_index_builds() == frozenset(), "补建成功后登记簿清空"
        assert "USING INDEX " + INDEX_NAME in _plan(conn)
    finally:
        conn.close()


def test_normal_open_never_registers_pending_build(tmp_path):
    """无竞争时不得留待办：普通打开仍应当场建好索引（ISS-168 语义不退化）。"""
    path = _legacy_db_without_index(tmp_path)
    conn = db.connect(path)
    try:
        assert INDEX_NAME in _index_names(conn)
        assert db.pending_index_builds() == frozenset()
    finally:
        conn.close()


def test_non_lock_errors_are_not_swallowed_as_deferral(tmp_path):
    """fail-closed：让步只对写锁竞争生效，其余 DDL 失败必须原样上抛。"""
    path = _legacy_db_without_index(tmp_path)
    conn = db.connect(path)
    try:
        _drop_index(conn)
        conn.execute("DROP TABLE entries")          # 表没了 → 建索引必失败
        with pytest.raises(sqlite3.Error):
            db._ensure_indexes(conn)  # 默认严格模式：写事务路径语义
        assert db.pending_index_builds() == frozenset(), "非竞争失败不得登记待办"
    finally:
        conn.close()


def test_migration_path_still_builds_index_under_write_lock(tmp_path):
    """写事务路径仍严格：迁移链内的补建不认让步语义（写锁已自持）。

    迁移在 BEGIN IMMEDIATE 内补建索引，不受外部写锁影响，因此 ISS-173 的
    让步不得放宽这条路径 —— 否则迁移完成却可能留下缺索引的库。
    """
    path = tmp_path / "migrate-strict.db"
    legacy = sqlite3.connect(path)
    try:
        legacy.execute("PRAGMA journal_mode=WAL")
        for statement in db._SCHEMA_STATEMENTS:
            legacy.execute(statement)
        legacy.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb) VALUES ('2026-10-01T12:00:00', ?, 1, 0, 0.1, 42)",
            (ROOT,),
        )
        legacy.commit()
    finally:
        legacy.close()

    conn = db.connect(path)
    try:
        assert INDEX_NAME in _index_names(conn)
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        assert db.pending_index_builds() == frozenset()
    finally:
        conn.close()
