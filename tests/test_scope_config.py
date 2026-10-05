"""ISS-155：范围配置、扫描计划预览和新基线 API 的合同测试。

覆盖任务卡「先复现/验收」中本卡负责的反例：

1. **新能力不自动启用**：无 settings.json / 旧 settings.json（无
   ``storage_scope`` 键）时 ``enabled=False``，``scan_root`` 与
   ``FATHOM_SCAN_ROOT``/CLI 覆盖的生产行为逐字节不变；旧库零迁移零报错。
2. **新模式保存 + 回读 + 重启后保留**；仅改下一轮计划、不触发扫描。
3. **预览→保存版本冲突**：``expected_revision`` 不匹配即 409 且旧值不动。
4. **换卷/未选卷守卫**：reveal 只放行已选择规范根；旧单根口径下行为不变。
5. **坏配置 fail-closed**：未知 mode/相对路径/不存在目录/身份版本不匹配/
   scope_ids 错位 → 400 或 ConfigurationError，旧值保留。
6. **写令牌闸门**：PUT /api/storage/scope 无令牌 403（对齐既有安全合同）；
   读端点（discovery/preview）免令牌。
7. **环境变量覆盖排除集时不得静默清空已选范围**。

夹具风格沿用 tests/test_api_config.py：全部可写路径指向临时目录，扫描根
为合成目录，绝不触碰真实 HOME。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, storage


@dataclass(frozen=True)
class _FakeDiscovery:
    """合成发现结果：测试绝不派发真实 ``diskutil``（TESTING 隔离合同）。"""

    status: str = "ok"
    generated_at: str = "2026-10-05T00:00:00+00:00"
    volume_id: str = "apfs-volume:fake"
    container_id: str = "apfs-container:fake"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": storage.DISCOVERY_SCHEMA,
            "version": storage.DISCOVERY_VERSION,
            "status": self.status,
            "generated_at": self.generated_at,
            "startup_container": {"container_id": self.container_id,
                                  "shared_free_bytes": 42},
            "startup_volumes": [{"volume_id": self.volume_id,
                                 "container_id": self.container_id}],
            "errors": [],
        }


@pytest.fixture(autouse=True)
def _fake_discovery(monkeypatch):
    """把发现适配器替换为合成结果（默认 ok）。

    ``api._discovery_payload`` 每次调用都现取 ``storage.discover_startup``，
    因此按模块属性注入即可生效；需要降级的用例用 ``_boom`` 覆盖。
    """
    monkeypatch.setattr(storage, "discover_startup",
                        lambda *a, **k: _FakeDiscovery())
    yield


@pytest.fixture
def _boom(monkeypatch):
    """让发现抛错（验证失败可观测、不静默乐观）。"""

    def _raise(*_a, **_k):
        raise RuntimeError("discovery boom")

    monkeypatch.setattr(storage, "discover_startup", _raise)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
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
    monkeypatch.setattr(config, "_CLI_SCAN_ROOT_PINNED", False)
    for name, value in (
        ("DATA_DIR", cfg.data_dir), ("REPORTS_DIR", cfg.reports_dir),
        ("LOGS_DIR", cfg.logs_dir), ("DB_PATH", cfg.db_path),
        ("FRONTEND_DIR", cfg.frontend_dir), ("DEFAULT_ROOT", cfg.scan_root),
        ("PORT", cfg.port), ("EXCLUDE_NAMES", []),
    ):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())


@pytest.fixture
def client():
    with TestClient(api.app,
                    base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


@pytest.fixture
def bare_client():
    """未持令牌的客户端（守卫反例用）。"""
    with TestClient(api.app,
                    base_url=f"http://127.0.0.1:{config.PORT}") as c:
        yield c


def _select(client, roots, mode="custom_directory", scope_ids=None, **extra):
    body = {"mode": mode, "roots": [str(r) for r in roots]}
    if scope_ids is not None:
        body["scope_ids"] = list(scope_ids)
    body.update(extra)
    return client.put("/api/storage/scope", json=body)


# ---------- 1. 新能力不自动启用 ----------

class TestCapabilityNotAutoEnabled:
    def test_no_settings_file_leaves_scope_disabled(self, client):
        view = client.get("/api/config").json()
        assert view["storage_scope"]["enabled"] is False
        assert view["storage_scope"]["selection"] is None
        assert view["storage_scope"]["identity_version"] == \
            config.SCOPE_IDENTITY_VERSION
        # 旧读面逐字保留：未启用时 scan_root 仍是 FATHOM_SCAN_ROOT 解析值
        assert view["scan_root"] == str(config.DEFAULT_ROOT)

    def test_legacy_settings_file_has_no_migration_and_no_error(self, client,
                                                               tmp_path):
        """旧 settings.json（无 storage_scope 键）零迁移零报错，范围仍未启用。"""
        path = config.settings_path()
        path.write_text(json.dumps({"scan_root": str(tmp_path / "scanroot")}),
                        encoding="utf-8")
        config.refresh_user_settings()
        view = client.get("/api/config").json()
        assert view["storage_scope"]["enabled"] is False
        assert config.effective_scope_selection() is None
        # 旧键照旧生效（生产扫描根行为不变）
        assert view["scan_root"] == str(tmp_path / "scanroot")
        # 磁盘上不得凭空长出 storage_scope
        assert "storage_scope" not in json.loads(path.read_text(encoding="utf-8"))

    def test_discovery_does_not_enable_scope(self, client):
        """发现 ≠ 已监控：只读发现端点不改变任何生效配置。"""
        before = client.get("/api/config").json()["storage_scope"]
        assert client.get("/api/storage/discovery").status_code == 200
        after = client.get("/api/config").json()["storage_scope"]
        assert before == after
        assert after["enabled"] is False

    def test_preview_without_args_reports_disabled_and_no_plan(self, client):
        payload = client.get("/api/storage/plan/preview").json()
        assert payload["enabled"] is False
        assert payload["plan"] is None
        assert config.effective_scope_selection() is None


# ---------- 2. 新模式保存 / 回读 / 重启 ----------

class TestScopeSelectionPersistence:
    def test_save_then_read_back(self, client, tmp_path):
        a = tmp_path / "vol-a"
        b = tmp_path / "vol-b"
        a.mkdir()
        b.mkdir()
        resp = _select(client, [a, b], scope_ids=["apfs-volume:aaa", "apfs-volume:bbb"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] is True
        assert body["triggers_scan"] is False
        # 保存只改下一轮计划，绝不触发扫描
        assert body["scope"]["selection"]["revision"] == 1
        assert body["scope"]["selection"]["roots"] == [str(a), str(b)]
        plan = body["plan"]
        assert [p["scope_id"] for p in plan["plans"]] == \
            ["apfs-volume:aaa", "apfs-volume:bbb"]
        assert plan["plans"][0]["root"] == str(a)
        assert "du_timeout_s" in plan["read_limits"]
        assert "min_kb" in plan["budget"] and "exclude_names" in plan["budget"]
        # GET 回读同值
        assert client.get("/api/config").json()["storage_scope"]["enabled"] is True

    def test_restart_preserves_selection(self, client, tmp_path):
        a = tmp_path / "vol-a"
        a.mkdir()
        assert _select(client, [a]).status_code == 200
        # 模拟重启：新进程按运行根重读 settings.json
        reloaded = config.load_user_settings(config.settings_path())
        assert reloaded.storage_scope is not None
        assert reloaded.storage_scope.roots == (str(a),)
        assert reloaded.storage_scope.revision == 1

    def test_second_save_bumps_revision(self, client, tmp_path):
        a, b = tmp_path / "vol-a", tmp_path / "vol-b"
        a.mkdir()
        b.mkdir()
        assert _select(client, [a]).status_code == 200
        second = client.put("/api/storage/scope", json={
            "mode": "custom_directory", "roots": [str(b)],
            "expected_revision": 1})
        assert second.status_code == 200
        assert second.json()["scope"]["selection"]["revision"] == 2

    def test_startup_mode_requires_container_identity_shape(self, client,
                                                           tmp_path):
        root = tmp_path / "startup"
        root.mkdir()
        resp = _select(client, [root], mode="startup_storage",
                       container_id="apfs-container:xyz")
        assert resp.status_code == 200
        assert resp.json()["scope"]["selection"]["mode"] == "startup_storage"
        assert resp.json()["plan"]["container_id"] == "apfs-container:xyz"


# ---------- 3. 预览 → 保存版本冲突 ----------

class TestVersionConflict:
    def test_conflict_returns_409_and_keeps_old_value(self, client, tmp_path):
        a, b = tmp_path / "vol-a", tmp_path / "vol-b"
        a.mkdir()
        b.mkdir()
        assert _select(client, [a]).status_code == 200
        stale = client.put("/api/storage/scope", json={
            "mode": "custom_directory", "roots": [str(b)],
            "expected_revision": 0})  # 预览后已被他人改动
        assert stale.status_code == 409
        assert "版本冲突" in stale.json()["detail"]
        current = config.effective_scope_selection()
        assert current.roots == (str(a),)     # 旧值不动
        assert current.revision == 1

    def test_matching_revision_succeeds(self, client, tmp_path):
        a, b = tmp_path / "vol-a", tmp_path / "vol-b"
        a.mkdir()
        b.mkdir()
        assert _select(client, [a]).status_code == 200
        ok = client.put("/api/storage/scope", json={
            "mode": "custom_directory", "roots": [str(b)],
            "expected_revision": 1})
        assert ok.status_code == 200
        assert config.effective_scope_selection().roots == (str(b),)

    def test_bad_config_keeps_old_value(self, client, tmp_path):
        a = tmp_path / "vol-a"
        a.mkdir()
        assert _select(client, [a]).status_code == 200
        bad = client.put("/api/storage/scope", json={
            "mode": "custom_directory", "roots": [str(a / "nope")]})
        assert bad.status_code == 400
        assert config.effective_scope_selection().roots == (str(a),)

    def test_unknown_field_rejected(self, client, tmp_path):
        a = tmp_path / "vol-a"
        a.mkdir()
        resp = _select(client, [a], surprise=1)
        assert resp.status_code == 400
        assert "未知字段" in resp.json()["detail"]


# ---------- 4. 范围守卫（reveal 只放行已选根） ----------

class TestRevealScopeGuard:
    def test_unselected_volume_rejected_when_enabled(self, client, tmp_path,
                                                     monkeypatch):
        selected = tmp_path / "vol-a"
        other = tmp_path / "vol-b"
        selected.mkdir()
        other.mkdir()
        (other / "file.txt").write_text("x", encoding="utf-8")
        assert _select(client, [selected]).status_code == 200
        resp = client.post("/api/reveal", json={"path": str(other / "file.txt")})
        assert resp.status_code == 400
        assert "已选择范围" in resp.json()["detail"]

    def test_similar_prefix_root_rejected(self, client, tmp_path):
        selected = tmp_path / "vol-a"
        evil = tmp_path / "vol-a-evil"
        selected.mkdir()
        evil.mkdir()
        target = evil / "f.txt"
        target.write_text("x", encoding="utf-8")
        assert _select(client, [selected]).status_code == 200
        resp = client.post("/api/reveal", json={"path": str(target)})
        assert resp.status_code == 400

    def test_legacy_mode_uses_old_single_root_message(self, client, tmp_path,
                                                      monkeypatch):
        """未启用范围能力时，reveal 错误文案与边界回落旧单根口径。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "f.txt"
        target.write_text("x", encoding="utf-8")
        resp = client.post("/api/reveal", json={"path": str(target)})
        assert resp.status_code == 400
        assert "监控根" in resp.json()["detail"]


# ---------- 5. 坏配置 fail-closed ----------

class TestValidationFailClosed:
    @pytest.mark.parametrize("payload", [
        {"mode": "everything", "roots": ["/tmp"]},
        {"mode": "custom_directory", "roots": []},
        {"mode": "custom_directory", "roots": ["relative/dir"]},
        {"mode": "custom_directory", "roots": ["/definitely/missing/xyz"]},
        {"mode": "custom_directory", "roots": ["/tmp"], "identity_version": 99},
    ])
    def test_api_rejects_bad_payload(self, client, payload):
        resp = client.put("/api/storage/scope", json=payload)
        assert resp.status_code == 400
        assert config.effective_scope_selection() is None

    def test_duplicate_roots_rejected(self, client, tmp_path):
        a = tmp_path / "vol-a"
        a.mkdir()
        resp = _select(client, [a, a])
        assert resp.status_code == 400
        assert "重复" in resp.json()["detail"]

    def test_misaligned_scope_ids_rejected(self, client, tmp_path):
        a, b = tmp_path / "vol-a", tmp_path / "vol-b"
        a.mkdir()
        b.mkdir()
        resp = _select(client, [a, b], scope_ids=["apfs-volume:only-one"])
        assert resp.status_code == 400
        assert "一一对应" in resp.json()["detail"]

    def test_file_not_directory_rejected(self, client, tmp_path):
        f = tmp_path / "plain.txt"
        f.write_text("x", encoding="utf-8")
        resp = _select(client, [f])
        assert resp.status_code == 400

    def test_bad_json_body(self, client):
        resp = client.put(
            "/api/storage/scope", content=b"not-json",
            headers={"content-type": "application/json"})
        assert resp.status_code == 400

    def test_preview_candidate_invalid_is_400(self, client):
        resp = client.get("/api/storage/plan/preview?mode=nonsense&roots=/tmp")
        assert resp.status_code == 400


# ---------- 6. 写令牌闸门 ----------

class TestWriteTokenGate:
    def test_put_without_token_403(self, bare_client, tmp_path):
        a = tmp_path / "vol-a"
        a.mkdir()
        resp = bare_client.put("/api/storage/scope", json={
            "mode": "custom_directory", "roots": [str(a)]})
        assert resp.status_code == 403
        assert config.effective_scope_selection() is None

    def test_read_endpoints_need_no_token(self, bare_client):
        assert bare_client.get("/api/storage/discovery").status_code == 200
        assert bare_client.get("/api/storage/plan/preview").status_code == 200

    def test_evil_origin_rejected(self, bare_client, tmp_path):
        a = tmp_path / "vol-a"
        a.mkdir()
        resp = bare_client.put(
            "/api/storage/scope", json={"mode": "custom_directory",
                                        "roots": [str(a)]},
            headers={"Origin": "http://evil.example",
                     "X-Fathom-Token": "whatever"})
        assert resp.status_code == 403


# ---------- 7. 旧覆盖链与已选范围共存 ----------

class TestLegacyOverridesCoexist:
    def test_env_exclude_override_does_not_clear_selection(self, client,
                                                           tmp_path,
                                                           monkeypatch):
        a = tmp_path / "vol-a"
        a.mkdir()
        assert _select(client, [a]).status_code == 200
        monkeypatch.setenv("FATHOM_EXCLUDE_NAMES", "node_modules")
        reloaded = config.refresh_user_settings(
            config.load_user_settings(config.settings_path()))
        assert reloaded.exclude_names == "node_modules"
        assert reloaded.storage_scope is not None
        assert reloaded.storage_scope.roots == (str(a),)

    def test_scan_root_env_still_wins_for_legacy_reads(self, client,
                                                       tmp_path,
                                                       monkeypatch):
        """FATHOM_SCAN_ROOT 仍是旧读面权威；范围选择不篡改它。"""
        a = tmp_path / "vol-a"
        a.mkdir()
        assert _select(client, [a]).status_code == 200
        monkeypatch.setenv("FATHOM_SCAN_ROOT", str(tmp_path / "scanroot"))
        view = config.effective_settings_view()
        assert view["scan_root"] == str(tmp_path / "scanroot")
        assert view["sources"]["scan_root"] == "env"
