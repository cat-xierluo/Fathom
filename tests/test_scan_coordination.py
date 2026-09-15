"""ISS-020：真实进程 flock、首扫和统一入口生命周期反例。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import socket
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient
import pytest

from fathom import api, config, db, launchd, reports, scan_coordinator, scanner


PYTHON = Path(sys.executable)


def _env() -> dict[str, str]:
    value = os.environ.copy()
    value["PYTHONPATH"] = str(Path(__file__).parents[1])
    return value


def _wait_status(client: TestClient, expected: str, timeout: float = 8) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/scan/status").json()
        if state["status"] == expected:
            return state
        time.sleep(0.03)
    raise AssertionError(f"未等到 {expected}: {state}")


def test_two_real_processes_only_one_enters_protected_scan(tmp_path):
    runtime = tmp_path / "runtime"
    root = tmp_path / "root"
    root.mkdir()
    marker = tmp_path / "entered"
    code = textwrap.dedent("""
        import sys, time
        from pathlib import Path
        from fathom import config, scan_coordinator, scanner
        runtime, root, marker = map(Path, sys.argv[1:])
        config.configure(runtime_dir=runtime, scan_root=root)
        try:
            session = scan_coordinator.start_scan(source='cli')
        except scan_coordinator.ScanBusyError:
            print('busy', flush=True)
            raise SystemExit(2)
        def fake_du(target):
            with marker.open('a') as out:
                out.write(f"{session.lease.owner['owner_id']}\\n")
            time.sleep(0.8)
            return scanner.DuResult({str(target): 0}, 0, 0, 0.8)
        scanner.run_du = fake_du
        print('entered', flush=True)
        session.execute()
    """)
    argv = [str(PYTHON), "-c", code, str(runtime), str(root), str(marker)]
    p1 = subprocess.Popen(argv, cwd=Path(__file__).parents[1], env=_env(),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert p1.stdout.readline().strip() == "entered"
    p2 = subprocess.run(argv, cwd=Path(__file__).parents[1], env=_env(),
                        capture_output=True, text=True, timeout=5)
    out1, err1 = p1.communicate(timeout=5)
    assert p1.returncode == 0, (out1, err1)
    assert p2.returncode == 2 and p2.stdout.strip() == "busy"
    assert len(marker.read_text().splitlines()) == 1


def test_v1_database_migrates_to_lifecycle_details_without_losing_history(tmp_path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    try:
        for statement in db._SCHEMA_STATEMENTS[:-1]:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=1")
        conn.execute(
            "INSERT INTO scan_runs(started_at, finished_at, status, message) "
            "VALUES ('2026-09-12T08:00:00','2026-09-12T08:01:00','done','{}')"
        )
        conn.commit()
    finally:
        conn.close()
    migrated = db.connect(path)
    try:
        assert db.schema_version(migrated) == db.SCHEMA_VERSION
        assert migrated.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 1
        assert migrated.execute("SELECT COUNT(*) FROM scan_run_details").fetchone()[0] == 0
    finally:
        migrated.close()
    backups = list(tmp_path.glob("v1.db.backup-v1-*.sqlite3"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600


def test_owner_crash_leaves_inherited_child_lock_without_pid_kill(tmp_path):
    """owner os._exit 后，自己创建的 child 仍持锁；新服务只报 busy。"""
    lock_path = tmp_path / "scan.lock"
    ready = tmp_path / "ready"
    code = textwrap.dedent("""
        import os, subprocess, sys
        from pathlib import Path
        from fathom.scan_coordinator import ScanLease
        lock, ready = map(Path, sys.argv[1:])
        lease = ScanLease.acquire(lock, source='api')
        child = subprocess.Popen(['/bin/sleep', '1.2'], pass_fds=(lease.fd,))
        ready.write_text(str(child.pid))
        os._exit(0)
    """)
    owner = subprocess.Popen([str(PYTHON), "-c", code, str(lock_path), str(ready)],
                             cwd=Path(__file__).parents[1], env=_env())
    owner.wait(timeout=5)
    child_pid = int(ready.read_text())
    with pytest.raises(scan_coordinator.ScanBusyError):
        scan_coordinator.ScanLease.acquire(lock_path, source="cli")
    # busy 判定不向元数据 PID/child PID 发送信号，child 仍自然存活。
    os.kill(child_pid, 0)
    time.sleep(1.35)
    lease = scan_coordinator.ScanLease.acquire(lock_path, source="cli")
    lease.release()


def test_real_api_first_scan_succeeds_without_report_then_second_day_reports(tmp_path):
    root = tmp_path / "synthetic-root"
    root.mkdir()
    (root / "tiny.txt").write_text("synthetic", encoding="utf-8")
    runtime = tmp_path / "runtime"
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    serve_code = (
        "import sys; from fathom import cli,reports; "
        "reports.notify_for_snapshot=lambda conn,sid: True; "
        "raise SystemExit(cli.main(sys.argv[1:]))"
    )
    proc = subprocess.Popen(
        [str(PYTHON), "-c", serve_code, "--runtime-dir", str(runtime),
         "--scan-root", str(root), "--port", str(port), "serve"],
        cwd=tmp_path, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    base = f"http://127.0.0.1:{port}"

    def get_json(path: str) -> dict:
        with urlopen(base + path, timeout=1) as response:
            return json.load(response)

    def post_scan(token: str) -> dict:
        request = Request(base + "/api/scan", data=b"", method="POST",
                          headers={"X-Fathom-Token": token})
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def wait_done() -> dict:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = get_json("/api/scan/status")
            if state["status"] != "running":
                return state
            time.sleep(0.03)
        raise AssertionError("真实 API 扫描未结束")

    try:
        deadline = time.monotonic() + 10
        token = None
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                token = get_json("/api/bootstrap")["token"]
                break
            except OSError:
                time.sleep(0.03)
        assert token is not None, proc.stderr.read() if proc.poll() is not None else ""
        assert post_scan(token)["ok"] is True
        state1 = wait_done()
        assert state1["status"] == "done"
        assert state1["result"]["snapshot_id"] > 0
        assert state1["result"]["report_status"] == "not_available"
        assert state1["result"]["report"] is None

        path = runtime / "data" / "fathom.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE snapshots SET created_at='2026-09-12T08:00:00' WHERE id=?",
            (state1["result"]["snapshot_id"],),
        )
        conn.commit()
        conn.close()

        assert post_scan(token)["ok"] is True
        state2 = wait_done()
        assert state2["status"] == "done"
        assert state2["result"]["report_status"] == "written"
        assert Path(state2["result"]["report"]).is_file()
        assert state2["source"] == "api"
    finally:
        proc.terminate()
        proc.communicate(timeout=8)


def test_cli_and_scheduled_sources_use_same_contract_with_real_du(tmp_path):
    runtime = tmp_path / "runtime"
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_text("x", encoding="utf-8")
    main = Path(__file__).parents[1] / "main.py"
    base = [str(PYTHON), str(main), "--runtime-dir", str(runtime),
            "--scan-root", str(root), "scan"]
    first = subprocess.run(base + ["--source", "cli"], cwd=tmp_path, env=_env(),
                           capture_output=True, text=True, timeout=15)
    assert first.returncode == 0, first.stderr
    path = runtime / "data" / "fathom.db"
    conn = sqlite3.connect(path)
    conn.execute("UPDATE snapshots SET created_at='2026-09-12T08:00:00'")
    conn.commit()
    conn.close()
    second = subprocess.run(base + ["--source", "scheduled"], cwd=tmp_path, env=_env(),
                            capture_output=True, text=True, timeout=15)
    assert second.returncode == 0, second.stderr
    conn = sqlite3.connect(path)
    try:
        sources = [r[0] for r in conn.execute(
            "SELECT source FROM scan_run_details ORDER BY run_id")]
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM scan_runs ORDER BY id")]
    finally:
        conn.close()
    assert sources == ["cli", "scheduled"]
    assert statuses == ["done", "done"]


def test_launchd_timer_marks_scan_source_scheduled():
    payload = plistlib.loads(
        launchd._scan_plist(Path("/tmp/main.py"), "/tmp/python").encode("utf-8")
    )
    assert payload["ProgramArguments"] == [
        "/tmp/python", "/tmp/main.py", "scan", "--source", "scheduled"
    ]


def test_report_and_notification_failures_keep_snapshot(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(config, "DB_PATH", runtime / "data" / "fathom.db")
    monkeypatch.setattr(config, "REPORTS_DIR", runtime / "reports")
    monkeypatch.setattr(config, "LOGS_DIR", runtime / "logs")
    monkeypatch.setattr(config, "DEFAULT_ROOT", root)

    real_write_report = reports.write_daily_report
    monkeypatch.setattr(reports, "write_daily_report",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    _rid, result = scan_coordinator.run_scan(source="cli", root=root)
    assert result["report_status"] == "failed"
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
        conn.execute("UPDATE snapshots SET created_at='2026-09-12T08:00:00'")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(reports, "write_daily_report", real_write_report)
    monkeypatch.setattr(reports, "notify_for_snapshot", lambda conn, sid: False)
    _rid2, result2 = scan_coordinator.run_scan(source="scheduled", root=root)
    assert result2["report_status"] == "written"
    assert result2["notification_status"] == "failed"
    assert Path(result2["report"]).is_file()
    conn = db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        conn.close()

    monkeypatch.setattr(
        scanner, "create_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("write failed")),
    )
    with pytest.raises(sqlite3.OperationalError):
        scan_coordinator.run_scan(source="cli", root=root)
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == before
        row = conn.execute("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "failed"
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_cancel_or_timeout_reaps_owned_du_and_releases_lock(tmp_path, monkeypatch, mode):
    root = tmp_path / "root"
    root.mkdir()
    lock = tmp_path / "lock"
    lease = scan_coordinator.ScanLease.acquire(lock, source="api")
    cancel = threading.Event()
    real_popen = subprocess.Popen
    child_pid: list[int] = []

    def sleeping_popen(*args, **kwargs):
        proc = real_popen(["/bin/sleep", "30"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True,
                          pass_fds=(lease.fd,))
        child_pid.append(proc.pid)
        return proc

    monkeypatch.setattr(scanner.subprocess, "Popen", sleeping_popen)
    errors: list[BaseException] = []
    def invoke():
        try:
            with scanner.du_process_context(inherited_fd=lease.fd, cancel_event=cancel):
                if mode == "timeout":
                    with scanner.du_process_context(
                        inherited_fd=lease.fd, cancel_event=cancel, timeout_seconds=0.1
                    ):
                        scanner.run_du(root)
                else:
                    scanner.run_du(root)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    while not child_pid:
        time.sleep(0.01)
    if mode == "cancel":
        cancel.set()
    thread.join(timeout=5)
    assert isinstance(errors[0], scanner.ScanInterruptedError)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid[0], 0)
    lease.release()
    again = scan_coordinator.ScanLease.acquire(lock, source="cli")
    again.release()


def test_api_shutdown_cancels_its_du_and_marks_interrupted(tmp_path, monkeypatch):
    root = tmp_path / "root"
    runtime = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr(config, "DB_PATH", runtime / "data" / "fathom.db")
    monkeypatch.setattr(config, "REPORTS_DIR", runtime / "reports")
    monkeypatch.setattr(config, "LOGS_DIR", runtime / "logs")
    monkeypatch.setattr(config, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "_active_scan", None)
    monkeypatch.setattr(api, "_active_scan_thread", None)
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())
    real_popen = subprocess.Popen
    child_pid: list[int] = []

    def sleeping_popen(*args, **kwargs):
        proc = real_popen(["/bin/sleep", "30"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True,
                          pass_fds=kwargs.get("pass_fds", ()))
        child_pid.append(proc.pid)
        return proc

    monkeypatch.setattr(scanner.subprocess, "Popen", sleeping_popen)
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as client:
        client.headers["X-Fathom-Token"] = client.get("/api/bootstrap").json()["token"]
        assert client.post("/api/scan").status_code == 200
        deadline = time.monotonic() + 3
        while not child_pid and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid[0], 0)
    conn = db.connect()
    try:
        row = conn.execute("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "interrupted"
    finally:
        conn.close()
    lease = scan_coordinator.ScanLease.acquire(
        config.DB_PATH.with_name(config.DB_PATH.name + ".scan.lock"), source="cli"
    )
    lease.release()


def test_cli_sigterm_reaps_its_du_and_releases_lock(tmp_path):
    runtime = tmp_path / "runtime"
    root = tmp_path / "root"
    root.mkdir()
    child_file = tmp_path / "child-pid"
    code = textwrap.dedent("""
        import subprocess, sys
        from pathlib import Path
        from fathom import cli, scanner
        runtime, root, child_file = map(Path, sys.argv[1:])
        real_popen = subprocess.Popen
        def sleeping_popen(*args, **kwargs):
            proc = real_popen(['/bin/sleep', '30'], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True,
                              pass_fds=kwargs.get('pass_fds', ()))
            child_file.write_text(str(proc.pid))
            return proc
        scanner.subprocess.Popen = sleeping_popen
        raise SystemExit(cli.main(['--runtime-dir', str(runtime), '--scan-root',
                                   str(root), 'scan']))
    """)
    proc = subprocess.Popen(
        [str(PYTHON), "-c", code, str(runtime), str(root), str(child_file)],
        cwd=Path(__file__).parents[1], env=_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 5
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_file.exists()
    child_pid = int(child_file.read_text())
    proc.terminate()
    stdout, stderr = proc.communicate(timeout=8)
    assert proc.returncode == 130, (stdout, stderr)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    conn = sqlite3.connect(runtime / "data" / "fathom.db")
    try:
        assert conn.execute(
            "SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0] == "interrupted"
    finally:
        conn.close()
    lock_path = runtime / "data" / "fathom.db.scan.lock"
    lease = scan_coordinator.ScanLease.acquire(lock_path, source="cli")
    lease.release()


class TestScanTimeoutConfigurable:
    """ISS-061：du 超时可配置（环境/默认），超时后可解释且保留上次有效快照。

    生产证据（PM 只读实测）：run 1/2 状态=interrupted、message="du 超过
    3600 秒安全时限"，快照停在 2026-09-12；硬编码 3600 在 ~11M 文件 /
    937k 目录的 /Users/maoking 上无解释地截断每日扫描。新合同：超时上限
    由 config.DU_TIMEOUT_S（默认 14400s；FATHOM_DU_TIMEOUT_S 覆盖）提供，
    触发后 ScanInterruptedError 冒到外层，scan_runs 落 status=interrupted
    + 含秒数与单位的消息，**不写新快照**，旧有效快照原样保留。
    """

    @staticmethod
    def _insert_valid_snapshot(conn, *, when: str, root: str) -> int:
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (when, root, 3, 0, 12.0, 1024, config.MIN_DIR_KB, "full"),
        )
        conn.commit()
        return cur.lastrowid

    def test_session_du_timeout_reads_from_config_not_hardcoded(
        self, tmp_path, monkeypatch
    ):
        """ScanSession.du_timeout_seconds 必须等于 config.DU_TIMEOUT_S。

        反例：把 DU_TIMEOUT_S 改到 12.5 后 start_scan() 拿到的会话必须读
        12.5；旧硬编码 3600.0 在生产根 ~11M 文件上无解释地截断扫描。
        """
        root = tmp_path / "root"
        root.mkdir()
        runtime = tmp_path / "runtime"
        monkeypatch.setattr(config, "DB_PATH", runtime / "data" / "fathom.db")
        monkeypatch.setattr(config, "REPORTS_DIR", runtime / "reports")
        monkeypatch.setattr(config, "LOGS_DIR", runtime / "logs")
        monkeypatch.setattr(config, "DEFAULT_ROOT", root)
        monkeypatch.setattr(config, "DU_TIMEOUT_S", 12.5)
        session = scan_coordinator.start_scan(source="cli")
        try:
            assert session.du_timeout_seconds == 12.5
            # 关键不变量：永远不是原硬编码 3600.0（除非配置显式设回）。
            assert session.du_timeout_seconds != 3600.0
        finally:
            session.lease.release()

    def test_timeout_marks_interrupted_preserves_last_snapshot_and_releases_lock(
        self, tmp_path, monkeypatch
    ):
        """du 超过配置上限 → status=interrupted，旧快照原样保留，租约释放。

        不真跑 1 小时：把 DU_TIMEOUT_S 调到 0.1s，把 Popen 替成 /bin/sleep 30，
        让协调器真触达 du_process_context 里的 deadline 检查并冒
        ScanInterruptedError。须同时满足三条 ISS-018/ISS-020 不变量：
          (1) 扫描事实：scan_runs.status='interrupted' 且 message 含 "du 超过
              0.1 秒安全时限"（运行可解释）。
          (2) 写库边界：当日不写新 snapshots/entries/volume_stats；旧快照
              的 id、root、total_kb 等关键列原样保留（ISS-018 R1）。
          (3) 资源回收：本进程创建的 child du 被回收（SIGTERM 路径），租约
              锁立即可被新进程取走。
        """
        runtime = tmp_path / "runtime"
        root = tmp_path / "root"
        root.mkdir()
        (root / "x.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(config, "DB_PATH", runtime / "data" / "fathom.db")
        monkeypatch.setattr(config, "REPORTS_DIR", runtime / "reports")
        monkeypatch.setattr(config, "LOGS_DIR", runtime / "logs")
        monkeypatch.setattr(config, "DEFAULT_ROOT", root)
        monkeypatch.setattr(config, "DU_TIMEOUT_S", 0.1)

        # 注入一次旧有效快照（生产实证：2026-09-12），新扫描若成功本应
        # 替换当日；超时路径必须**不动**这条旧快照。用 db.connect() 建库
        # 让 schema 自动初始化，避免 sqlite3 直连时缺表（ISS-025 合同）。
        config.ensure_runtime_dirs()
        seed_conn = db.connect()
        try:
            old_id = self._insert_valid_snapshot(
                seed_conn, when="2026-09-12T08:00:00", root=str(root),
            )
            old_total = seed_conn.execute(
                "SELECT total_kb FROM snapshots WHERE id=?", (old_id,)
            ).fetchone()[0]
        finally:
            seed_conn.close()

        # 替 Popen 为 /bin/sleep 30，让 du 实际"超过" 0.1s 阈值。
        real_popen = subprocess.Popen
        child_pid: list[int] = []

        def sleeping_popen(*args, **kwargs):
            proc = real_popen(
                ["/bin/sleep", "30"], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
                pass_fds=kwargs.get("pass_fds", ()),
            )
            child_pid.append(proc.pid)
            return proc

        monkeypatch.setattr(scanner.subprocess, "Popen", sleeping_popen)

        # 触发扫描并断言 ScanInterruptedError 冒到 run_scan 外层。
        with pytest.raises(scanner.ScanInterruptedError, match="du 超过 0.1 秒安全时限"):
            scan_coordinator.run_scan(source="cli", root=root)

        # (1) 扫描事实可解释。
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status, message, started_at, finished_at FROM scan_runs "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row["status"] == "interrupted"
            assert row["message"] is not None and "du 超过 0.1 秒安全时限" in row["message"]
            assert row["started_at"] is not None and row["finished_at"] is not None
            # 状态机：interrupted 不算 done/failed，且没残留 running。
            statuses = [r[0] for r in conn.execute("SELECT status FROM scan_runs ORDER BY id")]
            assert "running" not in statuses

            # (2) 写库边界：仅保留旧快照；当日未写新快照/entries/volume_stats。
            snap_rows = list(conn.execute(
                "SELECT id, created_at, total_kb, collection_status "
                "FROM snapshots ORDER BY id"
            ))
            assert [(r["id"], r["created_at"]) for r in snap_rows] == [
                (old_id, "2026-09-12T08:00:00")
            ]
            assert snap_rows[0]["total_kb"] == old_total
            assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM volume_stats").fetchone()[0] == 0
            # scan_run_details 的 phase 不应为半写的 snapshot。
            detail = conn.execute(
                "SELECT phase, snapshot_id FROM scan_run_details "
                "WHERE run_id=(SELECT MAX(id) FROM scan_runs)"
            ).fetchone()
            # 走到 du 阶段才超时；snapshot_id 必须保持 NULL（未提交）。
            assert detail["snapshot_id"] is None
        finally:
            conn.close()

        # (3) 资源回收：子进程 du 已被回收 + 锁立即可被新进程取走。
        assert child_pid, "sleeping_popen 没有被实际调用，测试未达超时路径"
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid[0], 0)
        lock_path = runtime / "data" / "fathom.db.scan.lock"
        new_lease = scan_coordinator.ScanLease.acquire(lock_path, source="cli")
        new_lease.release()

    def test_eintr_path_remains_independent_from_timeout(
        self, tmp_path, monkeypatch
    ):
        """ISS-047（瞬时 EINTR）与 ISS-061（du 超时）是两条独立路径，
        不得互相掩盖：瞬时错误走 classify_collection 的 partial 分支
        并落 snapshot，超时路径不写 snapshot 并落 status=interrupted。

        反例：两次相邻扫描——第一次让 ``run_du`` 返回瞬时计数非零的
        DuResult（ISS-047 合同：归 partial、status=done）；第二次让
        ``run_du`` 抛 ``ScanInterruptedError("du 超过 0.1 秒安全时限")``
        模拟 ISS-061 超时。两次扫描必须分别落不同 status，不可被 transient
        path 的 partial 行为掩盖；锁必须仍可被新进程取走。
        """
        runtime = tmp_path / "runtime"
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("a", encoding="utf-8")
        monkeypatch.setattr(config, "DB_PATH", runtime / "data" / "fathom.db")
        monkeypatch.setattr(config, "REPORTS_DIR", runtime / "reports")
        monkeypatch.setattr(config, "LOGS_DIR", runtime / "logs")
        monkeypatch.setattr(config, "DEFAULT_ROOT", root)
        # 给超时一个足够大的值，确保 EINTR 路径先于超时触发。
        monkeypatch.setattr(config, "DU_TIMEOUT_S", 60.0)

        # 用闭包变量切换 run_du 的行为，避免 monkeypatch 覆盖不到第二次。
        mode = {"kind": "eintr"}

        def fake_run_du(target):
            if mode["kind"] == "eintr":
                return scanner.DuResult(
                    sizes={str(target): 4096},
                    exit_code=1,
                    denied_count=0,
                    transient_error_count=2,
                    transient_error_sample=(
                        f"du: {target}/Mail/MessageTemp/1/E.eml: "
                        "Interrupted system call"
                    ),
                    elapsed_seconds=0.05,
                    stderr_tail=(
                        f"du: {target}/Mail/MessageTemp/1/E.eml: "
                        "Interrupted system call",
                        f"du: {target}/Mail/MessageTemp/2/E.eml: "
                        "Interrupted system call",
                    ),
                )
            # 第二次：模拟 ISS-061 超时（与 run_du 真实 timeout 分支同形态）。
            raise scanner.ScanInterruptedError(
                f"du 超过 {config.DU_TIMEOUT_S:g} 秒安全时限"
            )

        monkeypatch.setattr(scan_coordinator.scanner, "run_du", fake_run_du)
        # 报告/通知在临时目录上跑会失败，但不会反向撤销快照（ISS-020）。
        monkeypatch.setattr(scan_coordinator.reports, "notify_for_snapshot",
                            lambda conn, sid: True)

        # 第一次：EINTR 路径 → status=done，snapshot collection_status=partial。
        _rid, result = scan_coordinator.run_scan(source="cli", root=root)
        assert result["report_status"] in {"not_available", "written"}
        conn = db.connect()
        try:
            run = conn.execute(
                "SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert run["status"] == "done"
            snap = conn.execute(
                "SELECT collection_status, denied_count, total_kb "
                "FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            # ISS-047 合同：瞬时错误非零永不 full，归 partial；denied_count
            # 单独计数、不与 transient 混同。
            assert snap["collection_status"] == "partial"
            assert snap["denied_count"] == 0
            assert snap["total_kb"] >= 0
        finally:
            conn.close()

        # 反例二：把 DU_TIMEOUT_S 调成 0.1s + 让 run_du 抛超时异常。
        # 锁路径与进程回收由 ScanInterruptedError 处理器负责（与 ISS-020
        # 既有的 cancel 路径共用），故不需再注入 Popen。
        monkeypatch.setattr(config, "DU_TIMEOUT_S", 0.1)
        mode["kind"] = "timeout"
        with pytest.raises(scanner.ScanInterruptedError, match="du 超过 0.1 秒安全时限"):
            scan_coordinator.run_scan(source="cli", root=root)

        conn = db.connect()
        try:
            statuses = [r[0] for r in conn.execute(
                "SELECT status FROM scan_runs ORDER BY id"
            )]
            # 第一次 EINTR 路径 = done；第二次超时 = interrupted。两条独立。
            assert statuses == ["done", "interrupted"]
            # 超时后快照数不再增长（partial 那条 + 0 新增）。
            assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
        finally:
            conn.close()
        # 锁仍可被新进程取走。
        lock_path = runtime / "data" / "fathom.db.scan.lock"
        new_lease = scan_coordinator.ScanLease.acquire(lock_path, source="cli")
        new_lease.release()
