"""scan_runs 手动扫描状态持久化测试（ISS-007）。

覆盖：三态转换（running/done/failed）、重启模拟（进程内锁视为进程身份，换新锁
即模拟服务重启）、陈旧 running 超时收尾、旧库无损升级。
扫描内核（scanner / reports）全部 mock，不跑真 du。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db

EMPTY_STATE = {"id": None, "status": None, "running": False, "started_at": None,
               "finished_at": None, "result": None, "error": None}


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """数据库与日报目录指到临时目录，避免污染真实 data/fathom.db。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    # _scan_lock 是进程身份的化身：换新锁即模拟"服务重启"（表里的记录保留）
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())


@pytest.fixture
def client():
    return TestClient(api.app)


def _mock_scan_kernel(monkeypatch, blocker: threading.Event | None = None,
                      fail: bool = False) -> None:
    """替掉扫描内核三件套；blocker 用于观察 running 态，fail 模拟扫描失败。"""

    def fake_create_snapshot(conn, root=None, min_kb=None) -> int:
        if blocker is not None:
            blocker.wait(timeout=10)  # 兜底：测试异常时不让线程悬挂太久
        if fail:
            raise RuntimeError("du 模拟失败")
        return 4321

    monkeypatch.setattr(api.scanner, "create_snapshot", fake_create_snapshot)
    monkeypatch.setattr(api.reports, "write_daily_report",
                        lambda conn, sid: "/tmp/fake-report.md")
    monkeypatch.setattr(api.scanner, "prune_snapshots", lambda conn: 2)


def _wait_until(pred, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _insert_run(status: str, started_at: str,
                finished_at: str | None = None, message: str | None = None) -> int:
    """绕过 API 直接造表记录（模拟上次进程写下的状态）。"""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO scan_runs(started_at, finished_at, status, message) "
            "VALUES (?,?,?,?)",
            (started_at, finished_at, status, message),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _iso(age_seconds: float = 0) -> str:
    return (dt.datetime.now() - dt.timedelta(seconds=age_seconds)).isoformat(
        timespec="seconds")


class TestLifecycle:
    """三态转换：running -> done / failed。"""

    def test_running_then_done(self, client, monkeypatch):
        release = threading.Event()
        _mock_scan_kernel(monkeypatch, blocker=release)

        r = client.post("/api/scan")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["run_id"] is not None

        # POST 返回即应能在表里看到 running（开始态在请求线程内落库）
        st = client.get("/api/scan/status").json()
        assert st["running"] is True
        assert st["status"] == "running"
        assert st["started_at"] and st["finished_at"] is None
        # /api/status 的 scan 字段与 /api/scan/status 同源
        assert client.get("/api/status").json()["scan"]["running"] is True

        release.set()
        assert _wait_until(
            lambda: client.get("/api/scan/status").json()["status"] == "done")

        st = client.get("/api/scan/status").json()
        assert st["running"] is False
        assert st["finished_at"] and st["error"] is None
        assert st["result"] == {"snapshot_id": 4321,
                                "report": "/tmp/fake-report.md", "pruned": 2}

    def test_failed(self, client, monkeypatch):
        _mock_scan_kernel(monkeypatch, fail=True)

        assert client.post("/api/scan").json()["ok"] is True
        assert _wait_until(
            lambda: client.get("/api/scan/status").json()["status"] == "failed")

        st = client.get("/api/scan/status").json()
        assert st["running"] is False
        assert st["result"] is None
        assert "du 模拟失败" in st["error"]
        assert st["finished_at"]

    def test_second_scan_rejected_while_running(self, client, monkeypatch):
        release = threading.Event()
        _mock_scan_kernel(monkeypatch, blocker=release)

        assert client.post("/api/scan").json()["ok"] is True
        assert _wait_until(
            lambda: client.get("/api/scan/status").json()["running"])

        r2 = client.post("/api/scan")
        assert r2.status_code == 409

        release.set()
        assert _wait_until(
            lambda: client.get("/api/scan/status").json()["status"] == "done")


class TestRestartRecovery:
    """重启模拟：_isolated_db 换了新锁（新进程），scan_runs 记录仍在。"""

    def test_recent_running_reported_as_is(self, client):
        """重启后 running 记录未超时：如实报告 running（状态不因重启丢失）。"""
        _insert_run("running", _iso(60))

        st = client.get("/api/scan/status").json()
        assert st["running"] is True and st["status"] == "running"

    def test_stale_running_finalized_on_read(self, client):
        """started_at 超过上限：读取时收尾为 failed，且落库可查。"""
        rid = _insert_run("running", _iso(api.SCAN_STALE_SECONDS + 60))

        st = client.get("/api/scan/status").json()
        assert st["running"] is False
        assert st["status"] == "failed"
        assert "中断" in st["error"]
        assert st["finished_at"]

        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM scan_runs WHERE id=?", (rid,)).fetchone()
            assert row["status"] == "failed"  # 收尾已持久化，历次记录可查
        finally:
            conn.close()

    def test_new_scan_finalizes_remnant(self, client, monkeypatch):
        """未超时的遗留 running 被新扫描收编：收尾 + 新纪录，历史完整。"""
        _insert_run("running", _iso(30))  # 未超时，但已属死进程
        _mock_scan_kernel(monkeypatch)

        assert client.post("/api/scan").json()["ok"] is True
        assert _wait_until(
            lambda: client.get("/api/scan/status").json()["status"] == "done")

        runs = client.get("/api/scan/status?history=10").json()["runs"]
        assert len(runs) == 2
        assert runs[0]["status"] == "done"    # 新扫描（id 大在前）
        assert runs[1]["status"] == "failed"  # 遗留 running 被收尾


class TestHistoryAndRobustness:
    def test_history_param(self, client):
        _insert_run("done", _iso(3600), _iso(3500),
                    json.dumps({"snapshot_id": 1}))
        _insert_run("failed", _iso(60), _iso(50), "boom")

        r = client.get("/api/scan/status").json()
        assert "runs" not in r  # 默认不带历史

        runs = client.get("/api/scan/status?history=10").json()["runs"]
        assert [x["status"] for x in runs] == ["failed", "done"]
        assert "started_at" in runs[0] and "message" in runs[0]

    def test_done_message_not_json_degrades_gracefully(self, client):
        _insert_run("done", _iso(60), _iso(30), "不是 JSON")
        st = client.get("/api/scan/status").json()
        assert st["status"] == "done"
        assert st["result"] is None  # 异常数据不致 500

    def test_empty_db_state(self, client):
        assert client.get("/api/scan/status").json() == EMPTY_STATE


class TestLegacyUpgrade:
    """旧库无损升级：无 scan_runs 表的库经 db.connect 幂等补齐，旧数据不动。"""

    LEGACY_SCHEMA = """
        CREATE TABLE snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT NOT NULL,
            root         TEXT NOT NULL,
            dir_count    INTEGER NOT NULL,
            denied_count INTEGER NOT NULL,
            du_seconds   REAL NOT NULL,
            total_kb     INTEGER NOT NULL
        );
        CREATE TABLE entries (
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            path        TEXT NOT NULL,
            size_kb     INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, path)
        ) WITHOUT ROWID;
        CREATE TABLE volume_stats (
            snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
            total_bytes INTEGER NOT NULL,
            free_bytes  INTEGER NOT NULL
        );
    """

    def test_legacy_db_upgraded_in_place(self, tmp_path, monkeypatch, client):
        legacy = tmp_path / "legacy.db"
        conn = sqlite3.connect(legacy)
        try:
            conn.executescript(self.LEGACY_SCHEMA)  # 旧版库：没有 scan_runs
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb) VALUES ('2026-09-01T12:00:00', '/Users/x', "
                "10, 0, 5.0, 100)")
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(config, "DB_PATH", legacy)

        conn = db.connect()  # 幂等补 schema
        try:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "scan_runs" in tables
            # 旧快照无损
            assert conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"] == 1
        finally:
            conn.close()

        # 补齐后 API 直接可用：空态 -> 触发一次（mock）扫描正常走完三态
        assert client.get("/api/scan/status").json() == EMPTY_STATE
        _mock_scan_kernel(monkeypatch)
        assert client.post("/api/scan").json()["ok"] is True
        assert _wait_until(
            lambda: client.get("/api/scan/status").json()["status"] == "done")
