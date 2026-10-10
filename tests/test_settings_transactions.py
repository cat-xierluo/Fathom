"""两个实际OS进程共享合成运行根的配置事务与失败恢复。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import select
import sys

import pytest

from fathom import config


REPO = Path(__file__).resolve().parents[1]


def env_for(runtime, root):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("FATHOM_"):
            env.pop(key)
    env.update(FATHOM_RUNTIME_DIR=str(runtime), FATHOM_SCAN_ROOT=str(root),
               FATHOM_RESOURCE_DIR=str(REPO), FATHOM_RUNTIME_MODE="development",
               HOME=str(runtime / "home"))
    return env


def start_writer(runtime, root, operation):
    code = """
import sys,json,os
from fathom import config
print(json.dumps({'ready':True,'pid':os.getpid()}),flush=True)
sys.stdin.readline()
try:
 OPERATION
 print(json.dumps({'status':'ok','pid':os.getpid(),'settings':config.get_user_settings().as_dict()}),flush=True)
except Exception as error:
 print(json.dumps({'status':type(error).__name__,'pid':os.getpid(),'settings':config.get_user_settings().as_dict()}),flush=True)
""".replace(" OPERATION", " " + operation)
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO,
                            env=env_for(runtime, root), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = read_ready(proc)
    assert ready["ready"] and ready["pid"] == proc.pid
    return proc


def read_ready(proc):
    if not select.select([proc.stdout], [], [], 15)[0]:
        stop(proc)
        pytest.fail("owned writer did not reach its ready barrier")
    line = proc.stdout.readline()
    if not line:
        _, err = proc.communicate(timeout=5)
        pytest.fail(f"owned writer exited before ready: {err}")
    return json.loads(line)


def release(proc):
    proc.stdin.write("go\n")
    proc.stdin.flush()


def finish(proc):
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 0, err
    return json.loads(out)


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=10)


def run_writer(runtime, root, operation):
    proc = start_writer(runtime, root, operation)
    try:
        release(proc)
        return finish(proc)
    finally:
        if proc.poll() is None:
            stop(proc)


@pytest.fixture
def roots(tmp_path):
    runtime, a, b = [tmp_path / name for name in ("runtime", "a", "b")]
    for path in (runtime, a, b):
        path.mkdir()
    return runtime, a, b


def selection(root):
    return f"config.ScopeSelection(mode=config.SCOPE_MODE_CUSTOM,roots=({str(root)!r},))"


def test_two_processes_same_revision_exactly_one_scope_save(roots):
    runtime, a, b = roots
    writers = [start_writer(runtime, a, f"config.save_scope_selection({selection(root)},expected_revision=0)") for root in (a, b)]
    try:
        for proc in writers:
            release(proc)
        result = [finish(proc) for proc in writers]
        assert sorted(item["status"] for item in result) == ["ScopeVersionConflict", "ok"], result
        assert len({item["pid"] for item in result}) == 2
        print(json.dumps({"evidence": "dual-process-cas", "results": result}))
        saved = json.loads((runtime / "settings.json").read_text())
        winner = next(item for item in result if item["status"] == "ok")
        assert saved["storage_scope"]["revision"] == 1
        assert saved["storage_scope"] == winner["settings"]["storage_scope"]
        loser = next(item for item in result if item["status"] != "ok")
        assert loser["settings"]["storage_scope"] == saved["storage_scope"]
    finally:
        for proc in writers:
            if proc.poll() is None:
                stop(proc)


def test_stale_process_normal_update_preserves_scope_and_other_fields(roots):
    runtime, a, b = roots
    stale = start_writer(runtime, a, "config.update_user_settings({'scan_time':'06:45'})")
    try:
        assert run_writer(runtime, a, f"config.save_scope_selection({selection(b)},expected_revision=0); config.update_user_settings({{'auto_download_updates':False,'min_kb':777}})")["status"] == "ok"
        release(stale)
        assert finish(stale)["status"] == "ok"
        disk = json.loads((runtime / "settings.json").read_text())
        assert disk["storage_scope"]["roots"] == [str(b)]
        assert disk["storage_scope"]["revision"] == 1
        assert disk["auto_download_updates"] is False and disk["min_kb"] == 777
        assert disk["scan_time"] == "06:45"
    finally:
        if stale.poll() is None:
            stop(stale)


def test_parallel_non_scope_patches_preserve_both_fields(roots):
    runtime, a, _ = roots
    writers = [start_writer(runtime, a, f"config.update_user_settings({changes!r})") for changes in ({"scan_time":"06:45"}, {"min_kb":777})]
    try:
        for proc in writers:
            release(proc)
        assert all(finish(proc)["status"] == "ok" for proc in writers)
        disk = json.loads((runtime / "settings.json").read_text())
        assert disk["scan_time"] == "06:45" and disk["min_kb"] == 777
    finally:
        for proc in writers:
            if proc.poll() is None:
                stop(proc)


@pytest.mark.parametrize("bad", [b'{"scan_time": "broken"}\n', b'{"scan_time":', b"\xff"])
def test_bad_disk_does_not_get_replaced_by_stale_memory(roots, bad):
    runtime, a, _ = roots
    proc = start_writer(runtime, a, "config.update_user_settings({'scan_time':'06:45'})")
    (runtime / "settings.json").write_bytes(bad)
    try:
        release(proc)
        result = finish(proc)
        assert result["status"] == "ConfigurationError", result
        assert (runtime / "settings.json").read_bytes() == bad
        assert result["settings"]["scan_time"] is None
    finally:
        if proc.poll() is None:
            stop(proc)


def test_write_failure_keeps_memory_and_disk_then_releases_lock(isolated, monkeypatch):
    config.update_user_settings({"scan_time":"06:45"})
    old = config.settings_path().read_bytes()
    previous = config.get_user_settings()
    original = config.os.replace
    lock_inode = (config.settings_path().parent / config.SETTINGS_LOCK_FILENAME).stat().st_ino
    monkeypatch.setattr(config.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("synthetic replace failure")))
    with pytest.raises(OSError):
        config.update_user_settings({"scan_time":"08:00"})
    assert config.settings_path().read_bytes() == old
    assert config.get_user_settings() == previous
    monkeypatch.setattr(config.os, "replace", original)
    assert config.update_user_settings({"min_kb":777}).min_kb == 777
    assert (config.settings_path().parent / config.SETTINGS_LOCK_FILENAME).stat().st_ino == lock_inode


def test_unavailable_existing_engine_can_be_disabled_without_replacing_identity(isolated, tmp_path):
    engine = tmp_path / "engine"
    engine.write_text("#!/bin/sh\nexit 0\n")
    engine.chmod(0o755)
    config.update_user_settings({"analysis":{"enabled":True,"runtime":{"id":"claude-code","executable":str(engine),"version":"synthetic"}}})
    old_runtime = config.get_user_settings().analysis.runtime_executable
    engine.unlink()
    config.update_user_settings({"analysis":{"enabled":False}, "min_kb":777})
    assert config.get_user_settings().analysis.enabled is False
    assert config.get_user_settings().analysis.runtime_executable == old_runtime
    assert json.loads(config.settings_path().read_text())["min_kb"] == 777


def test_lock_acquisition_failure_does_not_publish_or_write(isolated, monkeypatch):
    config.update_user_settings({"scan_time": "06:45"})
    old, previous = config.settings_path().read_bytes(), config.get_user_settings()
    monkeypatch.setattr(config.fcntl, "flock", lambda *args: (_ for _ in ()).throw(OSError("synthetic lock failure")))
    with pytest.raises(OSError, match="lock failure"):
        config.update_user_settings({"min_kb": 777})
    assert config.settings_path().read_bytes() == old
    assert config.get_user_settings() == previous


def test_effective_env_validation_precedes_persistence(isolated, monkeypatch):
    config.update_user_settings({"scan_time": "06:45"})
    old, previous = config.settings_path().read_bytes(), config.get_user_settings()
    monkeypatch.setenv("FATHOM_EXCLUDE_NAMES", "invalid/path")
    with pytest.raises(config.ConfigurationError):
        config.update_user_settings({"min_kb": 777})
    assert config.settings_path().read_bytes() == old
    assert config.get_user_settings() == previous


def test_interrupted_writer_keeps_old_json_and_releases_os_lock(roots):
    import time
    runtime, a, _ = roots
    run_writer(runtime, a, "config.update_user_settings({'scan_time':'06:45'})")
    old = (runtime / "settings.json").read_bytes()
    marker = runtime / "before-rename"
    operation = ("import time; original=config.os.replace; "
                 f"config.os.replace=lambda *args: (config.Path({str(marker)!r}).write_text('ready'),time.sleep(60)); "
                 "config.update_user_settings({'min_kb':777})")
    proc = start_writer(runtime, a, operation)
    try:
        release(proc)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "writer did not reach pre-rename interruption point"
        stop(proc)
        assert (runtime / "settings.json").read_bytes() == old
        assert run_writer(runtime, a, "config.update_user_settings({'min_kb':888})")["status"] == "ok"
        assert json.loads((runtime / "settings.json").read_text())["min_kb"] == 888
    finally:
        if proc.poll() is None:
            stop(proc)


@pytest.mark.parametrize("bad", [
    {"analysis": {"enabled": False, "runtime": {"id": "unregistered", "executable": "/missing"}}},
    {"storage_scope": {"mode": config.SCOPE_MODE_CUSTOM, "roots": ["relative"], "revision": 1}},
    {"min_kb": -1, "future_key": True},
])
def test_latest_disk_still_rejects_bad_shape_without_overwrite(roots, bad):
    runtime, a, _ = roots
    proc = start_writer(runtime, a, "config.update_user_settings({'scan_time':'06:45'})")
    raw = json.dumps(bad).encode()
    (runtime / "settings.json").write_bytes(raw)
    try:
        release(proc)
        result = finish(proc)
        assert result["status"] == "ConfigurationError"
        assert result["settings"]["scan_time"] is None
        assert (runtime / "settings.json").read_bytes() == raw
    finally:
        if proc.poll() is None:
            stop(proc)


def test_unavailable_scope_preserved_for_unrelated_patch_but_new_scope_rejected(roots):
    runtime, a, b = roots
    proc = start_writer(runtime, a, "config.update_user_settings({'min_kb':777})")
    try:
        assert run_writer(runtime, a, f"config.save_scope_selection({selection(b)},expected_revision=0)")["status"] == "ok"
        b.rmdir()
        release(proc)
        assert finish(proc)["status"] == "ok"
        disk = json.loads((runtime / "settings.json").read_text())
        assert disk["storage_scope"]["roots"] == [str(b)] and disk["min_kb"] == 777
        with pytest.raises(config.ConfigurationError):
            config._validated_storage_scope_whole(disk["storage_scope"])
    finally:
        if proc.poll() is None:
            stop(proc)


def test_env_exclude_override_preserves_latest_analysis_identity(roots):
    runtime, a, _ = roots
    engine = runtime / "engine"
    engine.write_text("#!/bin/sh\nexit 0\n")
    engine.chmod(0o755)
    patch = {"analysis": {"enabled": True, "runtime": {
        "id": "claude-code", "executable": str(engine), "version": "synthetic"}}}
    assert run_writer(runtime, a, f"config.update_user_settings({patch!r})")["status"] == "ok"
    result = run_writer(runtime, a, "os.environ['FATHOM_EXCLUDE_NAMES']='synthetic-mask'; config.update_user_settings({'min_kb':777})")
    assert result["status"] == "ok"
    assert result["settings"]["analysis"]["runtime"]["executable"] == str(engine)
    assert result["settings"]["exclude_names"] == "synthetic-mask"
    disk = json.loads((runtime / "settings.json").read_text())
    assert "exclude_names" not in disk and disk["analysis"]["enabled"] is True


@pytest.fixture
def live_service(roots):
    """实际 CLI serve/HTTP，固定自有 PID；退出后核端口释放，不读生产 HOME。"""
    import socket
    import time
    import urllib.error
    import urllib.request
    runtime, a, _ = roots
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = env_for(runtime, a)
    env["HOME"] = str(runtime / "home")
    env["FATHOM_PORT"] = str(port)
    env["FATHOM_PORT_RANGE"] = "0"
    log = (runtime / "service.log").open("w")
    proc = subprocess.Popen([sys.executable, "-m", "fathom", "--port-range", "0", "serve"],
                            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    token = None

    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {} if token is None else {"X-Fathom-Token": token}
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     headers={**headers, "Content-Type": "application/json"},
                                     method="GET" if body is None else "PUT")
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert proc.poll() is None, (runtime / "service.log").read_text()
            try:
                status, health = request("/health")
                if status == 200:
                    assert health["pid"] == proc.pid and health["port"] == port
                    print(json.dumps({"evidence": "owned-http-health", "health": health}))
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        else:
            pytest.fail("owned HTTP service not ready")
        token = request("/api/bootstrap")[1]["token"]
        yield request, proc.pid, port
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log.close()
        with socket.socket() as sock:
            assert sock.connect_ex(("127.0.0.1", port)) != 0, "owned service port remains open"
        print(json.dumps({"evidence": "owned-http-closed", "pid": proc.pid, "port": port, "returncode": proc.returncode}))


def test_actual_http_stale_cas_refreshes_get_and_preview_without_failed_patch(roots, live_service):
    runtime, a, b = roots
    request, service_pid, _ = live_service
    result = run_writer(runtime, a, f"config.save_scope_selection({selection(b)},expected_revision=0); config.update_user_settings({{'min_kb':777}})")
    assert result["pid"] != service_pid
    old = (runtime / "settings.json").read_bytes()
    status, failure = request("/api/storage/scope", {"mode": config.SCOPE_MODE_CUSTOM, "roots": [str(a)], "expected_revision": 0})
    assert status == 409, failure
    assert (runtime / "settings.json").read_bytes() == old
    status, view = request("/api/config")
    assert status == 200 and view["storage_scope"]["selection"]["revision"] == 1
    assert view["storage_scope"]["selection"]["roots"] == [str(b)]
    status, preview = request("/api/storage/plan/preview")
    assert status == 200 and preview["selection"]["revision"] == 1
    assert preview["selection"]["roots"] == [str(b)]
    print(json.dumps({"evidence": "http-cas", "pid": service_pid, "writer_pid": result["pid"], "status": 409, "revision": 1, "disk_unchanged": True}))


def test_actual_http_normal_patch_preserves_other_process_updates(roots, live_service):
    runtime, a, b = roots
    request, _, _ = live_service
    assert run_writer(runtime, a, f"config.save_scope_selection({selection(b)},expected_revision=0); config.update_user_settings({{'min_kb':777,'auto_download_updates':False}})")["status"] == "ok"
    assert request("/api/config", {"scan_time": "06:45"})[0] == 200
    disk = json.loads((runtime / "settings.json").read_text())
    assert disk["storage_scope"]["roots"] == [str(b)]
    assert disk["scan_time"] == "06:45" and disk["min_kb"] == 777
    assert disk["auto_download_updates"] is False


def test_actual_cli_scan_default_stale_cas_stops_before_scan(roots, live_service):
    runtime, a, b = roots
    request, _, _ = live_service
    # 仅替换磁盘发现和扫描执行边界；命令解析/默认计划/CAS/失败退出走实际 CLI。
    setup = f"""
from fathom import cli,api,scan_coordinator
os.environ.pop('FATHOM_SCAN_ROOT')
config.configure(mode='release',runtime_dir=config.get_runtime_config().runtime_dir)
api._default_startup_selection=lambda: config.ScopeSelection(mode=config.SCOPE_MODE_STARTUP,roots=({str(a)!r},),scope_ids=('apfs-volume:synthetic-a',),container_id='apfs-container:synthetic')
scan_coordinator.run_scan=scan_coordinator.start_scan=lambda *a,**k: (_ for _ in ()).throw(AssertionError('scan must not start'))
"""
    operation = "exit_code=cli.main(['scan']); print(json.dumps({'cli_exit':exit_code,'revision':config.effective_scope_selection().revision}),file=sys.stderr)"
    # main.configure 默认会重读磁盘，因此在命令参数配置完成后等另一 writer。
    setup += "original_configure=config.configure\ndef wait_after_configure(**kwargs):\n original_configure(**kwargs)\n print(json.dumps({'ready':True,'pid':os.getpid()}),flush=True)\n sys.stdin.readline()\nconfig.configure=wait_after_configure\n"
    code = "import sys,json,os\nfrom fathom import config\n" + setup + "\n" + operation
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO,
                            env=env_for(runtime, a), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert read_ready(proc)["pid"] == proc.pid
        assert request("/api/storage/scope", {"mode": config.SCOPE_MODE_CUSTOM, "roots": [str(b)], "expected_revision": 0})[0] == 200
        old = (runtime / "settings.json").read_bytes()
        release(proc)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0 and '"cli_exit": 1' in err and '版本冲突' in err, (out, err)
        assert '"revision": 1' in err
        assert (runtime / "settings.json").read_bytes() == old
        assert not (runtime / "data" / "fathom.db").exists(), "conflict must precede opening scan DB"
        print(json.dumps({"evidence": "cli-cas", "pid": proc.pid, "cli_exit": 1,
                          "revision": 1, "disk_unchanged": True, "db_created": False}))
    finally:
        if proc.poll() is None:
            stop(proc)


def test_stale_analysis_patch_uses_latest_identity_and_revision(roots):
    runtime, a, _ = roots
    engine = runtime / "engine"
    engine.write_text("#!/bin/sh\nexit 0\n")
    engine.chmod(0o755)
    stale = start_writer(runtime, a, "config.update_user_settings({'analysis':{'enabled':False}})")
    patch = {"analysis": {"enabled": True, "runtime": {
        "id": "claude-code", "executable": str(engine), "version": "synthetic"}}}
    try:
        assert run_writer(runtime, a, f"config.update_user_settings({patch!r})")["settings"]["analysis"]["settings_revision"] == 1
        release(stale)
        result = finish(stale)
        assert result["status"] == "ok"
        analysis = result["settings"]["analysis"]
        assert analysis["runtime"]["executable"] == str(engine)
        assert analysis["enabled"] is False and analysis["settings_revision"] == 2
        assert json.loads((runtime / "settings.json").read_text())["analysis"] == analysis
    finally:
        if stale.poll() is None:
            stop(stale)
