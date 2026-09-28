"""ISS-091：GET /api/status 的 latest_snapshot 权限/覆盖字段契约。

设置页「权限与覆盖」事实卡（监控分区末尾）直接消费
``latest_snapshot`` 的覆盖字段。本文件锁定新旧快照的可空分类口径，
不从权限错误行数和目录数推导无意义的比例。字段自 ISS-021/024/066 起随 ``SELECT *``
整行返回——只把消费契约锁进回归：未来任何人改 ``_latest_snapshots``
的列选择或快照 schema 时，删列会在这里先红，而不是静默让设置页
显示 0。

覆盖：
1. denied>0 的合成快照：三个旧字段名与数值逐项一致；
2. 空库：latest_snapshot 为 null、vanished_count 顶层兜底 0（前端
   「尚未扫描」分支依赖）；
3. v4 之前的旧列缺失由 db 迁移保证 NOT NULL DEFAULT，这里不再用
   手工删列模拟（迁移链已有 tests/test_db_migrations.py 覆盖）。

测试隔离沿用 test_api_config.py 的夹具风格：全部可写路径指向临时目录。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """运行根/扫描根/DB 全部指向临时目录（与 test_api_config.py 同口径）。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    scanroot = tmp_path / "scanroot"
    scanroot.mkdir()
    cfg = config.RuntimeConfig.from_env(
        {"FATHOM_RUNTIME_DIR": str(runtime), "FATHOM_SCAN_ROOT": str(scanroot)},
        project_root=tmp_path, home=tmp_path / "home",
    )
    monkeypatch.setattr(config, "_ACTIVE", cfg)
    monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings())
    monkeypatch.setattr(config, "DATA_DIR", cfg.data_dir)
    monkeypatch.setattr(config, "DB_PATH", cfg.db_path)
    monkeypatch.setattr(config, "DEFAULT_ROOT", cfg.scan_root)
    monkeypatch.setattr(config, "PORT", cfg.port)
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())


@pytest.fixture
def client():
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        yield c


def _seed_snapshot(root: str, *, dir_count: int, denied_count: int,
                   vanished_count: int) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status, vanished_count) "
            "VALUES (?, ?, ?, ?, 1.0, 100, 10240, 'partial', ?)",
            ("2026-09-24T12:00:00", root, dir_count, denied_count, vanished_count),
        )
        conn.commit()
    finally:
        conn.close()


def test_status_latest_snapshot_carries_coverage_fields(client):
    """denied>0 的快照：dir/denied/vanished 三字段名与数值逐项可取。"""
    _seed_snapshot(str(config.DEFAULT_ROOT), dir_count=1200,
                   denied_count=6, vanished_count=4)
    body = client.get("/api/status").json()
    latest = body["latest_snapshot"]
    assert latest is not None
    # 设置页事实卡的三个直接依赖（ISS-091 契约）：字段缺失会让前端 ?? 0
    # 静默吞掉受限事实，这里按名逐项断言，改列名先红测试。
    assert latest["dir_count"] == 1200
    assert latest["denied_count"] == 6
    assert latest["vanished_count"] == 4
    assert latest["confirmed_missing_count"] is None
    assert latest["path_unverified_count"] is None


def test_status_empty_db_returns_null_latest_snapshot(client):
    """空库：latest_snapshot=null + 顶层 vanished_count 兜底 0，不 500。"""
    body = client.get("/api/status").json()
    assert body["snapshot_count"] == 0
    assert body["latest_snapshot"] is None
    assert body["vanished_count"] == 0
    assert body["confirmed_missing_count"] is None
    assert body["path_unverified_count"] is None


def test_classified_counts_flow_through_all_snapshot_apis(client):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status, vanished_count, "
            "confirmed_missing_count, path_unverified_count) "
            "VALUES ('2026-09-28T12:00:00', ?, 5, 6, 1.0, 100, 0, "
            "'partial', 4, 1, 3)", (str(config.DEFAULT_ROOT),)
        )
        conn.commit()
    finally:
        conn.close()
    status = client.get("/api/status").json()
    latest = status["latest_snapshot"]
    assert (latest["vanished_count"], latest["confirmed_missing_count"],
            latest["path_unverified_count"]) == (4, 1, 3)
    assert (status["confirmed_missing_count"], status["path_unverified_count"]) == (1, 3)
    row = client.get("/api/snapshots").json()[0]
    assert (row["confirmed_missing_count"], row["path_unverified_count"]) == (1, 3)
    coverage = client.get("/api/permissions").json()["coverage"]
    assert (coverage["confirmed_missing_count"], coverage["path_unverified_count"]) == (1, 3)


def test_legacy_snapshot_stays_unclassified_in_all_apis(client):
    _seed_snapshot(str(config.DEFAULT_ROOT), dir_count=5,
                   denied_count=6, vanished_count=4)
    latest = client.get("/api/status").json()["latest_snapshot"]
    row = client.get("/api/snapshots").json()[0]
    coverage = client.get("/api/permissions").json()["coverage"]
    for payload in (latest, row, coverage):
        assert payload["vanished_count"] == 4
        assert payload["confirmed_missing_count"] is None
        assert payload["path_unverified_count"] is None
