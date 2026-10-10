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
    monkeypatch.setattr(config, "EXCLUDE_NAMES", [])
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
    monkeypatch.setattr(api, "_discovery_payload", lambda: {"ok": True, "discovery": discovered})
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
