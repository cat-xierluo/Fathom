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
        assert db.schema_version(migrated) == 2
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
