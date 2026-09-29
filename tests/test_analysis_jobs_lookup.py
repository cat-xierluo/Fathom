"""分析 job 按区间查询与 busy 载荷补充（ISS-120，035B 接缝补卡）。

合同（TASKS ISS-120）：

- ``GET /api/analysis/jobs?a=&b=`` 只返回非终态（starting/running/
  cancelling）job，返回体是既有 job 视图字段（同 GET jobs/{id}，不含
  prompt/正文）；终态不返回。
- 跨会话场景：新会话经查询端点发现他方在途 job → 按
  ``GET /api/analysis/jobs/{id}`` 恢复显示（无自动恢复/重派语义）。
- 409 ``analysis_busy`` 在可判定占用者时响应体补充 ``active_job_id``；
  判定不了不带该字段（可选，知道才带）；既有错误响应形状不变。
- 守卫口径随既有读端点：GET 免写令牌；analysis.disabled（403 语义）
  不挡生命周期事实查询（同 GET /api/analyses）。

全部经生产 AnalysisManager / API 与真实 SQLite；状态矩阵用直接落
analysis_runs 行锁定（同 test_analysis_manager.TestLifecycle 口径），
真实链路用「settings 指向合成 CLI 脚本」制造在途。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from fathom import analysis_manager as am
from fathom import api, config, db

from tests.test_analysis_manager import (enable_analysis, make_fake_claude,
                                         make_snapshots, wait_terminal)


@pytest.fixture(autouse=True)
def _isolated(isolated):  # noqa: F811 - isolated 定义于 tests/conftest.py
    return isolated


@pytest.fixture(autouse=True)
def _reset_singleton():
    yield
    api._ANALYSIS_MANAGER = None


@pytest.fixture
def client():
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


def _setup(isolated, *, mode: str = "ok") -> dict:
    bin_dir = isolated["runtime"] / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = make_fake_claude(bin_dir, mode=mode)
    enable_analysis(script)
    a, b = make_snapshots(isolated["scanroot"])
    api._ANALYSIS_MANAGER = None  # 每用例重建生产单例（含启动 reconcile）
    return {**isolated, "script": script, "a": a, "b": b, "bin_dir": bin_dir}


def _insert_run(job_id: str, status: str, a: int, b: int) -> None:
    """直接落一行生命周期记录（确定性锁定查询的状态矩阵；字段同
    test_analysis_manager.TestLifecycle._insert_run）。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO analysis_runs(job_id, a_snapshot_id, b_snapshot_id,"
            " request_digest, facts_digest, prompt_version, idempotency_key,"
            " runtime_id, runtime_executable, settings_revision,"
            " consent_revision, status, owner_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, a, b, "rd", "fd", "pv", f"ik-{job_id}",
             "claude-code", "/bin/x", 0, 0, status, "o",
             "2026-09-28T00:00:00"))
        conn.commit()
    finally:
        conn.close()


def _execute(client, setup, key: str) -> dict:
    preview = client.post("/api/analysis/previews",
                          json={"a": setup["a"], "b": setup["b"]}).json()
    r = client.post("/api/analysis/jobs", json={
        "preview_id": preview["preview_id"],
        "request_digest": preview["request_digest"],
        "idempotency_key": key,
    })
    assert r.status_code == 202, r.text
    return r.json()["job"]


def _wait_terminal_via_api(client, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    view = None
    while time.monotonic() < deadline:
        view = client.get(f"/api/analysis/jobs/{job_id}").json()["job"]
        if view["terminal"]:
            return view
        time.sleep(0.05)
    raise AssertionError(f"job 未在 {timeout}s 内到终态：{view}")


def _stable(view: dict) -> dict:
    """去掉两次读取间可合法翻转的在途字段（starting→running 会变
    status/started_at），剩余字段逐键可比。"""
    return {k: v for k, v in view.items() if k not in ("status", "started_at")}


# ==========================================================================
# 验收①：查询合同——三在途态返回，五终态不返回
# ==========================================================================


class TestManagerQueryContract:
    def test_status_matrix_active_returned_terminal_excluded(self, isolated):
        a, b = make_snapshots(isolated["scanroot"])
        for status in ("starting", "running", "cancelling",
                       "succeeded", "failed", "cancelled",
                       "timed_out", "interrupted"):
            _insert_run(f"job-{status}", status, a, b)
        manager = am.AnalysisManager()
        found = manager.find_active_jobs(a, b)
        assert {v["job_id"] for v in found} == {
            "job-starting", "job-running", "job-cancelling"}
        assert len(found) == 3
        for view in found:
            assert view["terminal"] is False
            assert view["a_snapshot_id"] == a
            assert view["b_snapshot_id"] == b

    def test_interval_filter_isolates_pairs(self, isolated):
        a, b = make_snapshots(isolated["scanroot"])
        _insert_run("job-mine", "running", a, b)
        _insert_run("job-other", "starting", a, b + 100)
        manager = am.AnalysisManager()
        assert [v["job_id"] for v in manager.find_active_jobs(a, b)] \
            == ["job-mine"]
        assert [v["job_id"] for v in manager.find_active_jobs(a, b + 100)] \
            == ["job-other"]

    def test_no_active_returns_empty(self, isolated):
        a, b = make_snapshots(isolated["scanroot"])
        assert am.AnalysisManager().find_active_jobs(a, b) == []

    def test_real_inflight_found_and_terminal_excluded(self, isolated):
        """真实链路：生产 manager + sleep 脚本在途可查；取消到终态后
        同一查询返回空（终态不返回）。"""
        setup = _setup(isolated, mode="sleep")
        a, b = setup["a"], setup["b"]
        manager = am.AnalysisManager(run_timeout_s=20)
        preview = manager.create_preview(a, b)
        job, replayed = manager.start_job(preview.preview_id,
                                          preview.request_digest, "key-real")
        assert replayed is False
        try:
            deadline = time.monotonic() + 5
            views: list[dict] = []
            while time.monotonic() < deadline:
                views = manager.find_active_jobs(a, b)
                if views:
                    break
                time.sleep(0.05)
            assert [v["job_id"] for v in views] == [job["job_id"]]
            assert views[0]["status"] in ("starting", "running")
        finally:
            manager.cancel_job(job["job_id"])
            wait_terminal(manager, job["job_id"])
        assert manager.find_active_jobs(a, b) == []


# ==========================================================================
# 验收②：跨会话——新会话经查询发现他方在途 job，按 ID 恢复显示
# ==========================================================================


class TestCrossSessionDiscovery:
    def test_new_manager_instance_discovers_foreign_active_job(self, isolated):
        """会话 A（持租约、有内存镜像）在途；会话 B 是全新 manager 实例
        （内存镜像为空，仅共享权威表）：按区间查询发现 job_id，再经
        job_view（GET jobs/{id} 数据源）恢复完整状态显示。"""
        setup = _setup(isolated, mode="sleep")
        a, b = setup["a"], setup["b"]
        session_a = am.AnalysisManager(run_timeout_s=20)
        preview = session_a.create_preview(a, b)
        job, _ = session_a.start_job(preview.preview_id,
                                     preview.request_digest, "key-cross")
        try:
            session_b = am.AnalysisManager()
            deadline = time.monotonic() + 5
            views: list[dict] = []
            while time.monotonic() < deadline:
                views = session_b.find_active_jobs(a, b)
                if views:
                    break
                time.sleep(0.05)
            assert len(views) == 1
            discovered = views[0]
            assert discovered["job_id"] == job["job_id"]
            assert discovered["terminal"] is False
            recovered = session_b.job_view(job["job_id"])
            assert _stable(recovered) == _stable(
                session_a.job_view(job["job_id"]))
        finally:
            session_a.cancel_job(job["job_id"])
            wait_terminal(session_a, job["job_id"])

    def test_new_http_session_discovers_via_endpoint(self, client, isolated):
        """HTTP 层同场景：先行的在途 job 对后续请求（新页面会话）可经
        查询端点重新发现，再按 GET jobs/{id} 恢复显示。"""
        setup = _setup(isolated, mode="sleep")
        a, b = setup["a"], setup["b"]
        job = _execute(client, setup, "key-http-cross")
        try:
            r = client.get(f"/api/analysis/jobs?a={a}&b={b}")
            assert r.status_code == 200
            body = r.json()
            assert body["a"] == a and body["b"] == b
            assert job["job_id"] in [j["job_id"] for j in body["jobs"]]
            recovered = client.get(
                f"/api/analysis/jobs/{job['job_id']}")
            assert recovered.status_code == 200
            assert recovered.json()["job"]["terminal"] is False
        finally:
            client.post(f"/api/analysis/jobs/{job['job_id']}/cancel")
            _wait_terminal_via_api(client, job["job_id"])


# ==========================================================================
# 端点合同：形状、副作用、守卫口径
# ==========================================================================


class TestLookupEndpoint:
    def test_view_fields_match_single_job_and_no_prompt(self, client, isolated):
        """返回体复用既有 job 视图：与 GET jobs/{id} 逐字段一致；不含
        prompt/正文（prompt_text/result/facts 均不出现）。"""
        setup = _setup(isolated, mode="sleep")
        a, b = setup["a"], setup["b"]
        job = _execute(client, setup, "key-shape")
        try:
            body = client.get(f"/api/analysis/jobs?a={a}&b={b}").json()
            found = next(j for j in body["jobs"]
                         if j["job_id"] == job["job_id"])
            single = client.get(
                f"/api/analysis/jobs/{job['job_id']}").json()["job"]
            assert _stable(found) == _stable(single)
            for forbidden in ("prompt_text", "result", "facts"):
                assert forbidden not in found
        finally:
            client.post(f"/api/analysis/jobs/{job['job_id']}/cancel")
            _wait_terminal_via_api(client, job["job_id"])

    def test_empty_after_terminal_and_missing_args_400(self, client, isolated):
        setup = _setup(isolated)
        a, b = setup["a"], setup["b"]
        assert client.get(
            f"/api/analysis/jobs?a={a}&b={b}").json()["jobs"] == []
        # 参数校验随既有读端点：缺参/坏参 400（ISS-024 合同）
        assert client.get("/api/analysis/jobs").status_code == 400
        assert client.get(f"/api/analysis/jobs?a={a}").status_code == 400
        assert client.get(f"/api/analysis/jobs?a={a}&b=x").status_code == 400

    def test_read_semantics_no_token_and_disabled_403_not_applied(
            self, client, isolated):
        """守卫口径随既有读端点：GET 免写令牌；analysis.disabled 不挡
        查询（403 启用门只挡新派发，同 GET /api/analyses）。"""
        setup = _setup(isolated)
        config.update_user_settings({"analysis": {"enabled": False}})
        with TestClient(api.app,
                        base_url=f"http://127.0.0.1:{config.PORT}") as c:
            r = c.get(f"/api/analysis/jobs?a={setup['a']}&b={setup['b']}")
            assert r.status_code == 200
            assert r.json()["jobs"] == []
        # 对照：同状态下新派发仍 403（启用门语义未变；带令牌才到启用门，
        # 无令牌写请求先被全局边界守卫拒绝，那是既有合同）
        r2 = client.post("/api/analysis/jobs", json={
            "preview_id": "p", "request_digest": "d", "idempotency_key": "k"})
        assert r2.status_code == 403
        assert r2.json()["reason_code"] == "analysis_disabled"

    def test_lookup_is_side_effect_free(self, client, isolated):
        setup = _setup(isolated)
        a, b = setup["a"], setup["b"]
        url = f"/api/analysis/jobs?a={a}&b={b}"
        r1 = client.get(url)
        r2 = client.get(url)
        assert r1.status_code == r2.status_code == 200
        assert r1.json() == r2.json()
        conn = db.connect()
        try:
            n = conn.execute(
                "SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
        finally:
            conn.close()
        assert n == 0  # 查询不落任何生命周期行


# ==========================================================================
# 验收③：409 analysis_busy 载荷补充 active_job_id（可选，知道才带）
# ==========================================================================


class TestBusyPayload:
    def test_busy_409_carries_active_job_id(self, client, isolated):
        """不同请求忙时 409 analysis_busy，响应体带占用者 job_id。"""
        setup = _setup(isolated, mode="sleep")
        a, b = setup["a"], setup["b"]
        holder = _execute(client, setup, "key-holder")
        try:
            p2 = client.post("/api/analysis/previews",
                             json={"a": a, "b": b}).json()
            loser = client.post("/api/analysis/jobs", json={
                "preview_id": p2["preview_id"],
                "request_digest": p2["request_digest"],
                "idempotency_key": "key-loser",
            })
            assert loser.status_code == 409, loser.text
            body = loser.json()
            assert body["reason_code"] == "analysis_busy"
            assert body["active_job_id"] == holder["job_id"]
            assert set(body) == {"reason_code", "detail", "active_job_id"}
        finally:
            client.post(f"/api/analysis/jobs/{holder['job_id']}/cancel")
            _wait_terminal_via_api(client, holder["job_id"])

    def test_manager_busy_error_extra(self, isolated):
        setup = _setup(isolated, mode="sleep")
        a, b = setup["a"], setup["b"]
        manager = am.AnalysisManager(run_timeout_s=20)
        preview = manager.create_preview(a, b)
        job, _ = manager.start_job(preview.preview_id,
                                   preview.request_digest, "k-holder")
        manager._discard_preview(preview.preview_id)  # 预览单次消费
        preview2 = manager.create_preview(a, b)
        try:
            with pytest.raises(am.AnalysisError) as ei:
                manager.start_job(preview2.preview_id,
                                  preview2.request_digest, "k-loser")
            assert ei.value.reason_code == "analysis_busy"
            assert ei.value.status_code == 409
            assert ei.value.extra == {"active_job_id": job["job_id"]}
        finally:
            manager.cancel_job(job["job_id"])
            wait_terminal(manager, job["job_id"])

    def test_busy_without_active_row_omits_field(self, isolated):
        """占用方无在途行（终态/尚未落行的毫秒窗口）→ extra 为空，
        响应体不带 active_job_id（不猜）。用直接占租约模拟他方持锁。"""
        setup = _setup(isolated)
        a, b = setup["a"], setup["b"]
        manager = am.AnalysisManager()
        lease = am.AnalysisLease.acquire(manager._lock_path, source="test")
        try:
            preview = manager.create_preview(a, b)
            with pytest.raises(am.AnalysisError) as ei:
                manager.start_job(preview.preview_id,
                                  preview.request_digest, "k-nobody")
            assert ei.value.reason_code == "analysis_busy"
            assert ei.value.extra == {}
        finally:
            lease.release()

    def test_existing_error_payload_shape_unchanged(self, client, isolated):
        """加性合同：不带 extra 的既有错误仍是两键形状（无 active_job_id）。"""
        _setup(isolated)
        config.update_user_settings({"analysis": {"enabled": False}})
        r = client.post("/api/analysis/jobs", json={
            "preview_id": "p", "request_digest": "d", "idempotency_key": "k"})
        assert r.status_code == 403
        assert set(r.json()) == {"reason_code", "detail"}
