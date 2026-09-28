"""分析端点 HTTP 合同测试（ISS-035B）。

覆盖：Host/Origin/写令牌边界沿用全局守卫；未启用 403；未知资源 404；
202/409/400 语义；GET 无副作用；detect 只 POST（GET 无该路由）；错误体
携带稳定 reason_code 且不透出 stderr/路径/凭据。派发链路走生产
AnalysisManager + 合成 CLI（同 test_analysis_manager.py 夹具）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fathom import agent_runtime
from fathom import analysis_manager as am
from fathom import api, config, db

from tests.test_analysis_manager import (FAKE_VERSION, _INNER_RESULT,
                                         enable_analysis, make_fake_claude,
                                         make_snapshots, wait_terminal)


@pytest.fixture(autouse=True)
def _isolated(isolated):  # noqa: F811 - isolated 定义于 tests/conftest.py
    return isolated


@pytest.fixture
def client():
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


@pytest.fixture
def api_ok_setup(isolated):
    bin_dir = isolated["runtime"] / "bin"
    bin_dir.mkdir()
    script = make_fake_claude(bin_dir, mode="ok")
    enable_analysis(script)
    a, b = make_snapshots(isolated["scanroot"])
    api._ANALYSIS_MANAGER = None  # 每用例重建生产单例（含启动 reconcile）
    return {**isolated, "script": script, "a": a, "b": b, "bin_dir": bin_dir}


@pytest.fixture(autouse=True)
def _reset_singleton():
    yield
    api._ANALYSIS_MANAGER = None


def _new_preview(client, setup, key: str):
    r = client.post("/api/analysis/previews",
                    json={"a": setup["a"], "b": setup["b"]})
    assert r.status_code == 200, r.text
    body = r.json()
    r2 = client.post("/api/analysis/jobs", json={
        "preview_id": body["preview_id"],
        "request_digest": body["request_digest"],
        "idempotency_key": key,
    })
    assert r2.status_code == 202, r2.text
    return body, r2.json()


# ---------- 边界与语义 ----------


class TestBoundary:
    def test_write_endpoints_require_token(self, api_ok_setup):
        with TestClient(api.app,
                        base_url=f"http://127.0.0.1:{config.PORT}") as c:
            for path, payload in (
                ("/api/analysis/runtimes/detect", {}),
                ("/api/analysis/previews", {"a": 1, "b": 2}),
                ("/api/analysis/jobs", {"preview_id": "x", "request_digest": "y",
                                        "idempotency_key": "k"}),
                ("/api/analysis/jobs/x/cancel", None),
            ):
                r = c.post(path, json=payload)
                assert r.status_code == 403, (path, r.text)
            r = c.delete("/api/analyses/1")
            assert r.status_code == 403

    def test_evil_host_rejected(self, client):
        r = client.get("/api/analysis/jobs/nope", headers={"host": "evil.example"})
        assert r.status_code == 403

    def test_get_has_no_detect_route(self):
        paths = {r.path for r in api.app.routes
                 if getattr(r, "methods", None) and "GET" in r.methods}
        assert "/api/analysis/runtimes/detect" not in paths  # 仅用户点击（POST）

    def test_unknown_resources_404_with_reason(self, client):
        r = client.get("/api/analysis/jobs/no-such-job")
        assert r.status_code == 404
        assert r.json()["reason_code"] == "job_not_found"
        r = client.post("/api/analysis/jobs/no-such/cancel")
        assert r.status_code == 404
        r = client.delete("/api/analyses/987654")
        assert r.status_code == 404
        assert r.json()["reason_code"] == "analysis_not_found"
        r = client.get("/api/analyses?a=1&b=2")
        assert r.status_code == 200 and r.json()["analyses"] == []

    def test_bad_request_bodies_400(self, client, api_ok_setup):
        r = client.post("/api/analysis/previews", json={"a": "x", "b": 2})
        assert r.status_code == 400
        r = client.post("/api/analysis/previews", json={"a": 1})
        assert r.status_code == 400  # 缺 b
        r = client.post("/api/analysis/previews", json={"a": 1, "b": 2, "c": 3})
        assert r.status_code == 400  # 未知字段
        r = client.post("/api/analysis/jobs", json={"preview_id": "p"})
        assert r.status_code == 400  # 缺 digest/幂等键

    def test_missing_query_args_400(self, client):
        r = client.get("/api/analyses")
        assert r.status_code == 400


class TestDisabled:
    def test_previews_403_when_disabled(self, client, api_ok_setup):
        config.update_user_settings({"analysis": {"enabled": False}})
        r = client.post("/api/analysis/previews",
                        json={"a": api_ok_setup["a"], "b": api_ok_setup["b"]})
        assert r.status_code == 403
        assert r.json()["reason_code"] == "analysis_disabled"

    def test_jobs_403_when_disabled(self, client, api_ok_setup):
        config.update_user_settings({"analysis": {"enabled": False}})
        r = client.post("/api/analysis/jobs", json={
            "preview_id": "p", "request_digest": "d", "idempotency_key": "k"})
        assert r.status_code == 403
        assert r.json()["reason_code"] == "analysis_disabled"


class TestDetect:
    def test_detect_all_shape(self, client, monkeypatch):
        def fake_probe_all(*a, **kw):
            return {"claude-code": agent_runtime.RuntimeInfo(
                id="claude-code", display_name="Claude Code", identity="i",
                identity_evidence="e", official_docs="o",
                availability=agent_runtime.Availability.READY,
                reason_code="verified_version", detail="d", version="2.1.237")}
        monkeypatch.setattr(api.agent_runtime, "probe_all", fake_probe_all)
        r = client.post("/api/analysis/runtimes/detect")
        assert r.status_code == 200
        runtimes = r.json()["runtimes"]
        assert set(runtimes) == {"claude-code"}
        assert runtimes["claude-code"]["availability"] == "ready"
        assert runtimes["claude-code"]["auth_status"] == "unknown"

    def test_detect_unknown_id_400(self, client):
        r = client.post("/api/analysis/runtimes/detect",
                        json={"runtime_id": "codebuddy"})
        assert r.status_code == 400

    def test_detect_works_when_analysis_disabled(self, client, monkeypatch):
        monkeypatch.setattr(api.agent_runtime, "probe_all",
                            lambda *a, **kw: {})
        r = client.post("/api/analysis/runtimes/detect")
        assert r.status_code == 200  # 检测是选择的前置，不需要先启用


class TestPreviewAndJobs:
    def test_preview_shape_and_full_prompt(self, client, api_ok_setup):
        r = client.post("/api/analysis/previews",
                        json={"a": api_ok_setup["a"], "b": api_ok_setup["b"]})
        assert r.status_code == 200
        body = r.json()
        assert body["expires_in_s"] <= am.PREVIEW_TTL_S
        assert len(body["request_digest"]) == 64
        assert "FATHOM_FACTS_V1" in body["prompt_text"]
        assert body["runtime"]["id"] == "claude-code"
        assert body["runtime"]["version"] == FAKE_VERSION
        assert body["manifest"]["request_digest"] == body["request_digest"]

    def test_full_job_lifecycle_via_api(self, client, api_ok_setup):
        preview, started = _new_preview(client, api_ok_setup, "api-key-1")
        job = started["job"]
        assert started["replayed"] is False
        assert job["status"] in ("starting", "running")
        # 幂等重放同 job
        r2 = client.post("/api/analysis/jobs", json={
            "preview_id": preview["preview_id"],
            "request_digest": preview["request_digest"],
            "idempotency_key": "api-key-1",
        })
        assert r2.status_code == 202 and r2.json()["replayed"] is True
        assert r2.json()["job"]["job_id"] == job["job_id"]

        deadline = time.monotonic() + 20
        view = None
        while time.monotonic() < deadline:
            r = client.get(f"/api/analysis/jobs/{job['job_id']}")
            assert r.status_code == 200
            view = r.json()["job"]
            if view["terminal"]:
                break
            time.sleep(0.05)
        assert view["status"] == "succeeded"
        assert view["analysis_id"] is not None

        # 历史与撤销
        r = client.get(f"/api/analyses?a={api_ok_setup['a']}&b={api_ok_setup['b']}")
        items = r.json()["analyses"]
        assert len(items) == 1 and items[0]["expired"] is False
        rid = items[0]["id"]
        r = client.delete(f"/api/analyses/{rid}")
        assert r.status_code == 200 and r.json()["ok"] is True
        r = client.get(f"/api/analyses?a={api_ok_setup['a']}&b={api_ok_setup['b']}")
        assert r.json()["analyses"] == []
        r = client.get(f"/api/analysis/jobs/{job['job_id']}")
        assert r.json()["job"]["revoked"] is True

    def test_cancel_via_api_and_terminal_409(self, client, api_ok_setup):
        api_ok_setup["script"].write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "2.1.237 (Claude Code)"; exit 0; fi\n'
            "cat > /dev/null\nsleep 30\n", encoding="utf-8")
        _, started = _new_preview(client, api_ok_setup, "api-cancel")
        job = started["job"]
        r = client.post(f"/api/analysis/jobs/{job['job_id']}/cancel")
        assert r.status_code == 200
        assert r.json()["job"]["status"] in ("cancelling", "cancelled")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            view = client.get(f"/api/analysis/jobs/{job['job_id']}").json()["job"]
            if view["terminal"]:
                break
            time.sleep(0.05)
        assert view["status"] == "cancelled"
        r = client.post(f"/api/analysis/jobs/{job['job_id']}/cancel")
        assert r.status_code == 409
        assert r.json()["reason_code"] == "job_terminal"
        assert r.json()["job"]["status"] == "cancelled"

    def test_get_job_is_side_effect_free(self, client, api_ok_setup):
        r1 = client.get("/api/analysis/jobs/nope")
        r2 = client.get("/api/analysis/jobs/nope")
        assert r1.status_code == r2.status_code == 404
        assert r1.json() == r2.json()

    def test_digest_mismatch_409(self, client, api_ok_setup):
        preview = client.post(
            "/api/analysis/previews",
            json={"a": api_ok_setup["a"], "b": api_ok_setup["b"]}).json()
        r = client.post("/api/analysis/jobs", json={
            "preview_id": preview["preview_id"],
            "request_digest": "0" * 64,
            "idempotency_key": "api-bad-digest"})
        assert r.status_code == 409
        assert r.json()["reason_code"] == "request_digest_mismatch"

    def test_consent_revision_change_409(self, client, api_ok_setup):
        preview = client.post(
            "/api/analysis/previews",
            json={"a": api_ok_setup["a"], "b": api_ok_setup["b"]}).json()
        config.update_user_settings({"analysis": {"consent_revision": 7}})
        r = client.post("/api/analysis/jobs", json={
            "preview_id": preview["preview_id"],
            "request_digest": preview["request_digest"],
            "idempotency_key": "api-consent"})
        assert r.status_code == 409
        assert r.json()["reason_code"] == "preview_stale"

    def test_error_body_has_no_paths_or_stderr(self, client, api_ok_setup):
        """错误响应不透出可执行路径/stderr（方案 §5 错误卫生）。"""
        api_ok_setup["script"].unlink()
        r = client.post("/api/analysis/previews",
                        json={"a": api_ok_setup["a"], "b": api_ok_setup["b"]})
        assert r.status_code == 403
        text = r.text
        assert str(api_ok_setup["script"]) not in text
        assert "stderr" not in text.lower()
