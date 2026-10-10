"""默认启动盘候选、手动采集才启用、显式隔离与旧历史兼容。"""
from dataclasses import replace
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fathom import api, cli, config, storage, scan_coordinator, db, notify

_REAL_DISCOVERY_PAYLOAD = api._discovery_payload


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "legacy"
    root.mkdir()
    cfg = config.RuntimeConfig.from_env(
        {"FATHOM_RUNTIME_MODE": "release", "FATHOM_RUNTIME_DIR": str(tmp_path / "runtime")},
        project_root=tmp_path, home=root)
    monkeypatch.setattr(config, "_ACTIVE", cfg)
    monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings())
    monkeypatch.setattr(config, "_CLI_SCAN_ROOT_PINNED", False)
    monkeypatch.delenv("FATHOM_SCAN_ROOT", raising=False)
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())
    monkeypatch.setattr(config, "DEFAULT_ROOT", cfg.scan_root)
    monkeypatch.setattr(config, "PORT", cfg.port)
    monkeypatch.setattr(config, "DB_PATH", cfg.db_path)
    monkeypatch.setattr(config, "DATA_DIR", cfg.data_dir)
    monkeypatch.setattr(config, "REPORTS_DIR", cfg.reports_dir)
    monkeypatch.setattr(config, "LOGS_DIR", cfg.logs_dir)
    monkeypatch.setattr(config, "FRONTEND_DIR", cfg.frontend_dir)
    monkeypatch.setattr(config, "EXCLUDE_NAMES", [])
    for name in ("MIN_DIR_KB", "FREE_ALERT_GB", "SCAN_HOUR", "SCAN_MINUTE"):
        monkeypatch.setattr(config, name, getattr(config, name))
    system, data = tmp_path / "system", tmp_path / "data"
    system.mkdir()
    data.mkdir()
    discovered = {
        "startup_container": {"container_id": "apfs-container:boot"},
        "startup_volumes": [
            {"volume_id": "apfs-volume:system", "container_id": "apfs-container:boot",
             "roles": ["system"], "status": "unmounted", "mount_point": None},
            {"volume_id": "apfs-volume:data", "container_id": "apfs-container:boot",
             "roles": ["data"], "status": "accessible", "mount_point": str(data)},
            {"volume_id": "apfs-volume:locked", "container_id": "apfs-container:boot",
             "roles": [], "status": "locked", "mount_point": str(root)},
        ],
        "visible_entries": [{"volume_id": "apfs-volume:system",
                             "visible_path": str(system), "via": "snapshot_mount"}],
        "other_devices": [{"mount_point": str(root), "device_id": "external"}],
    }
    container_id = discovered["startup_container"]["container_id"]
    container = storage.DiscoveredContainer(storage.CONTAINER_SCHEMA,
        storage.DISCOVERY_VERSION, container_id, "disk9", 1024**3, 512*1024**2,
        (), ("synthetic",))
    volumes = tuple(storage.DiscoveredVolume(storage.VOLUME_SCHEMA,
        storage.DISCOVERY_VERSION, v["volume_id"], v["container_id"], None,
        "Synthetic", tuple(v["roles"]), "disk9s1", v["mount_point"], v["status"],
        False, 128*1024**2, ("synthetic",)) for v in discovered["startup_volumes"])
    entries = tuple(storage.VisibleEntry(storage.VISIBLE_ENTRY_SCHEMA,
        storage.DISCOVERY_VERSION, e["visible_path"], e["volume_id"], container_id,
        e["via"], None, ("synthetic",)) for e in discovered["visible_entries"])
    discovery = storage.StorageDiscovery(storage.DISCOVERY_SCHEMA,
        storage.DISCOVERY_VERSION, storage.DiscoveryStatus.OK, "2026-10-10", "darwin",
        container, volumes, entries, (), (), ())
    monkeypatch.setattr(storage, "discover_startup", lambda *a, **k: discovery)
    return cfg, system, data


@pytest.fixture
def client(isolated):
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


def test_startup_candidate_needs_no_hand_entered_roots(client, isolated):
    cfg, system, data = isolated
    body = client.get("/api/storage/plan/preview?mode=startup_storage").json()
    assert [p["root"] for p in body["plan"]["plans"]] == [str(system), str(data)]
    assert [p["scope_id"] for p in body["plan"]["plans"]] == ["apfs-volume:system", "apfs-volume:data"]
    assert body["plan"]["container_id"] == "apfs-container:boot"
    assert not config.settings_path().exists()
    assert config.effective_scope_selection() is None


def test_startup_save_resolves_and_persists_real_identity(client, isolated):
    response = client.put("/api/storage/scope", json={"mode": "startup_storage", "roots": [], "expected_revision": 0})
    assert response.status_code == 200, response.text
    assert response.json()["triggers_scan"] is False
    stored = config.load_user_settings(config.settings_path()).storage_scope
    assert stored.scope_ids == ("apfs-volume:system", "apfs-volume:data")
    assert not config.DB_PATH.exists()


def test_reading_default_does_not_enable_or_write(client, isolated):
    assert client.get("/api/config").json()["next_scan_default"] == "startup_storage"
    assert "默认采集内置启动盘" in client.get("/api/storage/plan/preview").json()["hint"]
    assert config.effective_scope_selection() is None
    assert not config.settings_path().exists()
    assert cli.scope_specs_from_effective_selection() == []


def test_first_manual_scan_uses_startup_and_retains_legacy_root(isolated):
    cfg, system, data = isolated
    specs = cli.scope_specs_from_effective_selection(apply_default=True)
    assert [str(s.root) for s in specs] == [str(system), str(data)]
    assert config.DEFAULT_ROOT == cfg.scan_root  # 不改旧历史的根/身份
    assert config.load_user_settings(config.settings_path()).storage_scope.roots == (str(system), str(data))


def test_manual_api_scan_creates_new_plans_and_keeps_old_history(client, isolated, monkeypatch):
    cfg, system, data = isolated
    monkeypatch.setattr(scan_coordinator, "read_capacity_readings", lambda: [])
    monkeypatch.setattr(notify, "notify_scan_round", lambda *args, **kwargs: False)
    conn = db.connect()
    conn.execute("INSERT INTO snapshots(created_at,root,dir_count,denied_count,du_seconds,total_kb,min_kb,collection_status) VALUES('2026-10-09T12:00:00',?,1,0,1,0,10240,'complete')", (str(cfg.scan_root),))
    conn.commit()
    old_id = conn.execute("SELECT id FROM snapshots").fetchone()["id"]
    conn.close()
    response = client.post("/api/scan")
    assert response.status_code == 200, response.text
    assert response.json()["scopes"] == 2
    deadline = time.monotonic() + 30
    while api._scan_lock.locked() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not api._scan_lock.locked()
    conn = db.connect()
    try:
        old = conn.execute("SELECT root,plan_id FROM snapshots WHERE id=?", (old_id,)).fetchone()
        assert old["root"] == str(cfg.scan_root) and old["plan_id"] is None
        plans = conn.execute("SELECT s.root,p.scope_id FROM snapshots s JOIN scan_plans p ON s.plan_id=p.plan_id").fetchall()
        assert {(p["root"], p["scope_id"]) for p in plans} == {
            (str(system), "apfs-volume:system"), (str(data), "apfs-volume:data")}
    finally:
        conn.close()


@pytest.mark.parametrize("override", ["env", "cli", "settings", "development"])
def test_explicit_root_or_development_keeps_legacy(isolated, monkeypatch, override):
    cfg, _, _ = isolated
    if override == "env": monkeypatch.setenv("FATHOM_SCAN_ROOT", str(cfg.scan_root))
    elif override == "cli": monkeypatch.setattr(config, "_CLI_SCAN_ROOT_PINNED", True)
    elif override == "settings": monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings(scan_root=str(cfg.scan_root)))
    else: monkeypatch.setattr(config, "_ACTIVE", replace(cfg, mode="development"))
    assert not config.default_startup_scope_requested()
    assert cli.scope_specs_from_effective_selection(apply_default=True) == []
    assert not config.settings_path().exists()


def test_discovery_failure_never_silently_scans_home(isolated, monkeypatch):
    monkeypatch.setattr(api, "_discovery_payload", lambda: {"ok": True, "discovery": {"startup_container": None}})
    with pytest.raises(scan_coordinator.ScanScopeError, match="无法发现内置启动盘"):
        cli.scope_specs_from_effective_selection(apply_default=True)
    assert config.effective_scope_selection() is None
    assert not config.settings_path().exists()


def test_no_readable_mount_fails_closed(client, monkeypatch):
    monkeypatch.setattr(api, "_discovery_payload", lambda: {"ok": True, "discovery": {
        "startup_container": {"container_id": "boot"}, "startup_volumes": []}})
    assert client.get("/api/storage/plan/preview?mode=startup_storage").status_code == 503
    assert config.effective_scope_selection() is None


def test_discovery_uses_boot_mount_not_scan_root(isolated, monkeypatch):
    # 去掉夹具的 API 桩，只替换磁盘适配器；核真实 HTTP 消费函数传参。
    observed = []
    class Result:
        def as_dict(self): return {}
    monkeypatch.setattr(storage, "discover_startup", lambda root: observed.append(root) or Result())
    _REAL_DISCOVERY_PAYLOAD()
    assert observed == ["/"]


@pytest.fixture
def saved_selection(isolated):
    cfg, system, _ = isolated
    selection = config.save_scope_selection(config.ScopeSelection(
        mode="custom_directory", roots=(str(system),),
        scope_ids=("path:saved",),
    ), expected_revision=0)
    return cfg, selection


@pytest.mark.parametrize("override", ["env", "cli"])
def test_saved_selection_yields_to_process_scan_root(saved_selection, monkeypatch, override):
    cfg, selection = saved_selection
    if override == "env":
        monkeypatch.setenv("FATHOM_SCAN_ROOT", str(cfg.scan_root))
    else:
        config.configure(scan_root=cfg.scan_root)
    settings_before = config.settings_path().read_bytes()
    assert cli.scope_specs_from_effective_selection() == []
    assert cli.scope_specs_from_effective_selection(apply_default=True) == []
    assert config.effective_scope_selection() == selection
    assert config.settings_path().read_bytes() == settings_before


@pytest.mark.parametrize("override", [
    "none", "settings", "env", "cli", "command_root", "scope_env", "scope_cli",
])
def test_cli_saved_selection_priority(saved_selection, monkeypatch, override):
    """真实 main 参数路由；只在执行边界截取根/范围，绝不启动 du。"""
    cfg, selection = saved_selection
    argv = ["--runtime-dir", str(cfg.runtime_dir)]
    if override in {"env", "scope_env"}:
        monkeypatch.setenv("FATHOM_SCAN_ROOT", str(cfg.scan_root))
    elif override in {"cli", "scope_cli"}:
        argv.extend(["--scan-root", str(cfg.scan_root)])
    elif override == "settings":
        config.update_user_settings({"scan_root": str(cfg.scan_root)})
    argv.append("scan")
    if override == "command_root":
        argv.extend(["--root", str(cfg.scan_root)])
    elif override.startswith("scope_"):
        argv.extend(["--scope", str(cfg.scan_root), "--scope-id", "path:explicit"])
    settings_before = config.settings_path().read_bytes()
    observed = []
    def capture_scan(**kwargs):
        observed.append(kwargs)
        raise scan_coordinator.ScanCancelledError("test execution boundary")
    monkeypatch.setattr(scan_coordinator, "run_scan", capture_scan)
    assert cli.main(argv) == 130
    assert len(observed) == 1
    called = observed[0]
    if override in {"env", "cli", "command_root"}:
        assert called["scopes"] == []
        assert called["root"] == cfg.scan_root
    elif override.startswith("scope_"):
        assert called["root"] is None
        assert [(s.root, s.scope_id) for s in called["scopes"]] == [
            (cfg.scan_root, "path:explicit")]
    else:
        assert called["root"] is None
        assert [(str(s.root), s.scope_id) for s in called["scopes"]] == list(
            zip(selection.roots, selection.scope_ids))
    assert config.effective_scope_selection() == selection
    assert config.settings_path().read_bytes() == settings_before


@pytest.mark.parametrize("override", ["none", "env", "cli"])
def test_api_saved_selection_priority(client, saved_selection, monkeypatch, override):
    """真实 POST /api/scan 在共用构造入口遵守进程覆盖，保存选择仍保留。"""
    cfg, selection = saved_selection
    if override == "env":
        monkeypatch.setenv("FATHOM_SCAN_ROOT", str(cfg.scan_root))
    elif override == "cli":
        config.configure(scan_root=cfg.scan_root)
    settings_before = config.settings_path().read_bytes()
    observed = []
    def capture_scan(**kwargs):
        observed.append(kwargs)
        raise scan_coordinator.ScanBusyError({"source": "test"})
    monkeypatch.setattr(scan_coordinator, "start_scan", capture_scan)
    response = client.post("/api/scan")
    assert response.status_code == 409, response.text
    assert len(observed) == 1
    assert observed[0]["source"] == "api"
    if override == "none":
        assert [(str(s.root), s.scope_id) for s in observed[0]["scopes"]] == list(
            zip(selection.roots, selection.scope_ids))
    else:
        assert observed[0]["scopes"] == []
        assert config.DEFAULT_ROOT == cfg.scan_root
    assert config.effective_scope_selection() == selection
    assert config.settings_path().read_bytes() == settings_before


@pytest.mark.parametrize("changes", [
    {"min_kb": 512}, {"exclude_names": ["cache"]},
    {"auto_download_updates": False}, {"analysis": {"enabled": True}},
])
def test_http_regular_save_preserves_scope_and_analysis(client, saved_selection, monkeypatch, changes):
    _, selection = saved_selection
    settings = replace(config.load_user_settings(config.settings_path()),
                       analysis=config.AnalysisSettings(settings_revision=2))
    config.save_user_settings(config.settings_path(), settings)
    config.refresh_user_settings(settings)
    class Manager:
        def refresh_policy(self): pass
    monkeypatch.setattr(api, "_get_analysis_manager", lambda: Manager())
    def forbidden_scan(**kwargs):
        pytest.fail("保存配置不得触发扫描")
    monkeypatch.setattr(scan_coordinator, "start_scan", forbidden_scan)
    response = client.put("/api/config", json=changes)
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    stored = config.load_user_settings(config.settings_path())
    assert stored.storage_scope == selection
    assert isinstance(config._USER_SETTINGS.storage_scope, config.ScopeSelection)
    assert isinstance(config._USER_SETTINGS.analysis, config.AnalysisSettings)
    assert stored.analysis.enabled == ("analysis" in changes)
    assert stored.analysis.settings_revision == (3 if "analysis" in changes else 2)
    assert not config.DB_PATH.exists()


@pytest.mark.parametrize("changes", [{"min_kb": 512}, {"scan_time": "09:30"}])
def test_http_non_root_save_keeps_release_startup_default(client, changes):
    response = client.put("/api/config", json=changes)
    assert response.status_code == 200, response.text
    assert response.json()["config"]["next_scan_default"] == "startup_storage"
    stored = config.load_user_settings(config.settings_path())
    assert stored.scan_root is None and stored.storage_scope is None
    assert not config.DB_PATH.exists()


@pytest.mark.parametrize("override", ["none", "env", "cli", "cli_over_env"])
def test_scope_presentation_matches_actual_process_scan(client, saved_selection, isolated, monkeypatch, override):
    cfg, selection = saved_selection
    _, _, other = isolated
    if override in {"env", "cli_over_env"}:
        monkeypatch.setenv("FATHOM_SCAN_ROOT", str(other if override == "cli_over_env" else cfg.scan_root))
    if override in {"cli", "cli_over_env"}:
        config.configure(scan_root=cfg.scan_root)
    source = "cli" if override.startswith("cli") else "env"
    expected = None if override == "none" else {"source": source, "root": str(cfg.scan_root)}
    before = config.settings_path().read_bytes()
    current = client.get("/api/storage/plan/preview").json()
    assert current["scan_override"] == expected
    assert current["selection"]["roots"] == list(selection.roots)
    if expected:
        assert current["plan"] is None  # 不能把未消费的保存范围当本轮计划
        assert current["plan_source"] == "process_override"
        assert str(cfg.scan_root) in current["hint"] and "不使用已保存范围" in current["hint"]
        assert client.get("/api/config").json()["sources"]["scan_root"] == source
        assert cli.scope_specs_from_effective_selection() == []
    else:
        assert current["plan_source"] == "saved_selection"
        assert [p["root"] for p in current["plan"]["plans"]] == list(selection.roots)
    candidate = client.get("/api/storage/plan/preview", params={
        "mode": "custom_directory", "roots": str(other)}).json()
    assert candidate["plan_source"] == "candidate"
    assert candidate["scan_override"] == expected
    assert [p["root"] for p in candidate["plan"]["plans"]] == [str(other)]
    assert "候选" in candidate["hint"]
    assert config.settings_path().read_bytes() == before
    response = client.put("/api/storage/scope", json={
        "mode": "custom_directory", "roots": [str(other)], "expected_revision": 1})
    assert response.status_code == 200, response.text
    assert response.json()["scope"]["scan_override"] == expected
    assert response.json()["scope"]["selection"]["revision"] == 2
    if expected:
        assert "不使用已保存范围" in response.json()["hint"]
    assert not config.DB_PATH.exists()
