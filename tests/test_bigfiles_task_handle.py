"""ISS-164：大文件查询的任务句柄与取消产品入口。

合同（上游审计 P2：ISS-150 的 ``GET /api/bigfiles`` 阻塞等 Future、无句柄、
无取消入口；浏览器中止请求不能冒充服务端任务取消）：
- 提交即得句柄：``GET /api/bigfiles?wait=false`` 立即返回 ``task_id``（不阻塞），
  ``wait=true``（缺省，旧调用）的响应也带 ``task_id``，其余逐字段不变。
- task_id 确定性：同参数（去重键）恒等同一句柄，同键并发去重仍是同一任务。
- 取消：``POST /api/bigfiles/cancel``（需既有 ``X-Fathom-Token``）按 task_id
  只取消该任务（进程组 SIGTERM→SIGKILL、只回收自有进程），终态 cancelled；
  已完成/已取消/已过期任务幂等返回（不报错、状态如实）；未知 task_id → 404。
  句柄即凭证（无「他人任务」概念），写令牌是取消端点沿用的既有安全门。
- 状态：``GET /api/bigfiles/status?task_id=`` 返回真实状态，与既有五态对齐
  （ok/no_match/permission_denied/failed/truncated/expired）+ running/cancelled；
  结果本体仍从原端点按需取。
- 全部用例在 tmp_path 合成数据 + 注入 popen 工厂，不触真实 HOME/生产目录。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import subprocess
import time
from pathlib import Path

import pytest

from fathom import bigfiles, config


# ---------- 辅助（注入模式沿用 tests/test_bigfiles_scoped.py） ----------

class _FakeStat:
    """duck-typed stat 结果，供 stat_fn 注入（慢速假 find 不落盘）。"""

    def __init__(self, size: int) -> None:
        self.st_size = size
        self.st_blocks = size // 512
        self.st_mtime = time.time()


class _FakePopen:
    """包装真实 subprocess.Popen，记录实例与生命周期。"""

    instances: list["_FakePopen"] = []

    def __init__(self, args, *, stdout=None, stderr=None,
                 start_new_session=False, **kwargs):
        self.args = args
        self._proc = subprocess.Popen(
            args, stdout=stdout, stderr=stderr,
            start_new_session=start_new_session, **kwargs)
        self.stdout = self._proc.stdout
        self.stderr = self._proc.stderr
        self.pid = self._proc.pid
        self.returncode = self._proc.returncode
        _FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        try:
            out, err = self._proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            out, err = self._proc.communicate()
            self.returncode = self._proc.returncode
            raise
        self.returncode = self._proc.returncode
        return out, err

    def poll(self):
        rc = self._proc.poll()
        if rc is not None:
            self.returncode = rc
        return rc

    def wait(self, timeout=None):
        rc = self._proc.wait(timeout=timeout)
        self.returncode = rc
        return rc

    def kill(self):
        self._proc.kill()
        self.returncode = self._proc.returncode

    @classmethod
    def reset(cls):
        cls.instances = []


def _explicit_popen_factory(real_argv0: str):
    """把 find_path 视为脚本、以显式解释器启动的 popen_factory（同既有测试）。"""
    def factory(args, **kwargs):
        return _FakePopen([real_argv0, *args], **kwargs)
    return factory


def _slow_find_script(tmp_path: Path, name: str, *, rounds: int = 0,
                      interval: float = 0.02) -> Path:
    """慢速假 find：周期性输出带 NUL 的路径；rounds=0 表示无限输出。"""
    script = tmp_path / name
    base = str(tmp_path / "virtual")
    cond = f"$i < {rounds}" if rounds else "1"
    q_open, q_close = chr(123), chr(125)
    body = (f"$|=1; $i=0; while ({cond}) {{ "
            f"print qq{q_open}{base}/big_$i.bin\\0{q_close}; "
            f"select(undef, undef, undef, {interval}); $i++; }}\n")
    script.write_text(f"#!/usr/bin/perl\n{body}")
    script.chmod(0o755)
    return script


def _make_file(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        chunk = b"\xa5" * (1024 * 1024)
        remaining = size_bytes
        while remaining > 0:
            f.write(chunk[: min(remaining, len(chunk))])
            remaining -= min(remaining, len(chunk))


def _wait_instances(n: int, deadline_s: float = 5.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end and len(_FakePopen.instances) < n:
        time.sleep(0.005)
    assert len(_FakePopen.instances) >= n, (
        f"期望至少 {n} 个 find 进程，实际 {len(_FakePopen.instances)}")


def _wait_proc_reaped(proc: _FakePopen, deadline_s: float = 10.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end and proc.poll() is None:
        time.sleep(0.01)
    assert proc.poll() is not None, "被取消任务的 find 进程未被回收"


def _poll_state(client, task_id: str, wanted: set[str],
                deadline_s: float = 10.0) -> dict:
    end = time.monotonic() + deadline_s
    body: dict = {}
    while time.monotonic() < end:
        r = client.get("/api/bigfiles/status", params={"task_id": task_id})
        assert r.status_code == 200, r.text
        body = r.json()
        if body["state"] in wanted:
            return body
        time.sleep(0.02)
    raise AssertionError(f"状态未在 {deadline_s}s 内到达 {sorted(wanted)}，最后={body}")


# 旧 GET 响应的逐字段合同（ISS-150 冻结面），本卡只允许新增 task_id
_LEGACY_FIELDS = {
    "state", "files", "scope", "stats", "truncated", "raw_truncated",
    "incomplete", "expired", "cached", "cache_age_s", "error_message",
}


@pytest.fixture(autouse=True)
def _reset_fake_popen():
    _FakePopen.reset()
    yield
    _FakePopen.reset()


# ---------- manager 句柄语义 ----------

class TestManagerTaskHandle:
    def test_task_id_deterministic_per_key(self, tmp_path):
        """同参数（去重键）恒等同一 task_id；不同键不同。"""
        m = bigfiles.BigfilesManager(cache_ttl_s=0.0)
        k1 = bigfiles.BigfilesQuery(root=tmp_path, days=7, min_mb=100,
                                    topn=50).key
        k2 = bigfiles.BigfilesQuery(root=tmp_path, days=7, min_mb=100,
                                    topn=51).key
        assert m.task_id_for(k1) == m.task_id_for(k1)
        assert m.task_id_for(k1) != m.task_id_for(k2)

    def test_task_id_opaque_no_path_leak(self, tmp_path):
        """task_id 不含完整 root 路径（ISS-049 隐私口径同源）。"""
        m = bigfiles.BigfilesManager(cache_ttl_s=0.0)
        key = bigfiles.BigfilesQuery(root=tmp_path / "secret-dir", days=7,
                                     min_mb=100, topn=50).key
        task_id = m.task_id_for(key)
        assert "secret-dir" not in task_id
        assert str(tmp_path) not in task_id

    def test_concurrent_same_key_shares_one_future_and_one_process(
            self, tmp_path):
        """同参并发去重语义保持：同键同任务、只启动一个 find。"""
        script = _slow_find_script(tmp_path, "slow.sh", rounds=0)
        m = bigfiles.BigfilesManager(
            find_path=str(script), default_timeout_s=30.0, cache_ttl_s=0.0,
            popen_factory=_explicit_popen_factory("/usr/bin/perl"),
            stat_fn=lambda p: _FakeStat(1024 * 1024))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(m.submit, tmp_path / "a", 7, 1, 5).result()
            f2 = pool.submit(m.submit, tmp_path / "a", 7, 1, 5).result()
        assert f1 is f2, "同键并发必须共享同一 Future"
        task_id = m.task_id_for(f1.query.key)
        assert m.status_view(task_id)["state"] == "running"
        _wait_instances(1)
        assert f1.cancel() is True
        with pytest.raises(concurrent.futures.CancelledError):
            f1.result(timeout=10.0)
        _wait_proc_reaped(_FakePopen.instances[0])
        view = m.status_view(task_id)
        assert view["state"] == "cancelled"
        assert view["terminal"] is True
        assert view["cancel_requested"] is True

    def test_unknown_task_id_raises_not_found(self, tmp_path):
        m = bigfiles.BigfilesManager(cache_ttl_s=0.0)
        with pytest.raises(bigfiles.BigfilesTaskNotFound):
            m.status_view("bf-0000000000000000")
        with pytest.raises(bigfiles.BigfilesTaskNotFound):
            m.cancel_task("bf-0000000000000000")


# ---------- API：句柄、取消、状态 ----------

class TestTaskHandleApi:
    @contextlib.contextmanager
    def _client(self, tmp_path, monkeypatch, *, rounds: int = 0,
                cache_ttl_s: float = 0.0, with_token: bool = True):
        """真实守卫口径的 TestClient：bootstrap 取写令牌 + 注入可控 manager。

        ``rounds=0`` 的假 find 无限输出、只由取消/预算终止（用于取消类用例）；
        ``rounds=1`` 自行完成（用于终态类用例）。
        """
        from fastapi.testclient import TestClient
        import fathom.api as api_mod
        root = tmp_path / "scanroot"
        root.mkdir(exist_ok=True)
        monkeypatch.setattr(config, "DEFAULT_ROOT", root)
        script = _slow_find_script(tmp_path, "slow.sh", rounds=rounds)
        manager = bigfiles.BigfilesManager(
            find_path=str(script), default_timeout_s=30.0,
            cache_ttl_s=cache_ttl_s,
            popen_factory=_explicit_popen_factory("/usr/bin/perl"),
            stat_fn=lambda p: _FakeStat(1024 * 1024))
        api_mod._BIGFILES_MANAGER = manager
        with TestClient(api_mod.app,
                        base_url=f"http://127.0.0.1:{config.PORT}") as c:
            if with_token:
                c.headers["X-Fathom-Token"] = c.get(
                    "/api/bootstrap").json()["token"]
            yield c, api_mod, manager, root, script

    # ---------- 提交即得句柄 ----------

    def test_blocking_get_keeps_legacy_fields_plus_task_id(self, tmp_path, monkeypatch):
        """兼容：旧 GET 逐字段不变，只新增 task_id。"""
        with self._client(tmp_path, monkeypatch, rounds=2) as (c, _api, _m, root, _s):
            _make_file(root / "big.bin", 2 * 1024 * 1024)
            r = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5})
            assert r.status_code == 200, r.text
            body = r.json()
            assert set(body) == _LEGACY_FIELDS | {"task_id"}, sorted(body)
            assert isinstance(body["task_id"], str) and body["task_id"]
            assert body["scope"]["mode"] == "recent"
            assert body["scope"]["root"] == str(root)
            # 假 find 输出 2 条 virtual 路径 + 注入 stat_fn → 完整走通 recent 合同
            assert body["state"] == "ok"
            assert [set(f) for f in body["files"]] == [
                {"path", "size", "mtime"}] * 2
            assert all("virtual" in f["path"] for f in body["files"])
            assert body["expired"] is False and body["cached"] is False
            assert body["truncated"] is False and body["incomplete"] is False
            assert body["error_message"] is None
            assert body["stats"]["find_output_lines"] == 2
            assert set(body["stats"]) == {
                "wall_ms", "peak_rss_bytes", "find_output_lines",
                "find_exit_code", "find_stderr_lines", "permission_denied_lines",
                "started_at", "finished_at"}

    def test_nonblocking_submit_returns_task_id_without_waiting(
            self, tmp_path, monkeypatch):
        """wait=false 立即返回句柄（202），不阻塞到 find 完成。"""
        with self._client(tmp_path, monkeypatch) as (c, _api, _m, _root, _s):
            r = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5,
                                               "wait": "false"})
            assert r.status_code == 202, r.text
            body = r.json()
            assert body["task_id"]
            assert body["state"] == "running"
            assert body["scope"]["topn"] == 5
            assert "files" not in body, "非阻塞提交不返回结果本体"
            _wait_instances(1)

    def test_same_params_share_one_task_handle(self, tmp_path, monkeypatch):
        """同参提交同键同任务：task_id 相同、只启动一个 find。"""
        with self._client(tmp_path, monkeypatch) as (c, _api, _m, _root, _s):
            params = {"min_mb": 1, "topn": 5, "wait": "false"}
            t1 = c.get("/api/bigfiles", params=params).json()["task_id"]
            t2 = c.get("/api/bigfiles", params=params).json()["task_id"]
            assert t1 == t2
            time.sleep(0.2)
            assert len(_FakePopen.instances) == 1, "同参去重只应启动一个 find"

    def test_blocking_get_409_carries_task_id(self, tmp_path, monkeypatch):
        """被取消的阻塞式旧调用：409 语义不变，附增 task_id 供前端恢复句柄。"""
        with self._client(tmp_path, monkeypatch) as (c, api, manager, root, _s):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(c.get, "/api/bigfiles",
                                  params={"min_mb": 1, "topn": 5})
                _wait_instances(1)
                key = bigfiles.BigfilesQuery(
                    root=root.resolve(), days=7, min_mb=1, topn=5,
                    mode="recent",
                    scope_version=config.BIGFILE_SCOPE_VERSION).key
                tid = manager.task_id_for(key)
                r_cancel = c.post("/api/bigfiles/cancel", json={"task_id": tid})
                assert r_cancel.status_code == 200, r_cancel.text
                r = fut.result(timeout=20.0)
            assert r.status_code == 409, r.text
            body = r.json()
            assert "已取消" in body["detail"]
            assert body["task_id"] == tid
            assert body["state"] == "cancelled"

    # ---------- 取消 ----------

    def test_cancel_running_task_ends_cancelled_and_reaps_process(
            self, tmp_path, monkeypatch):
        """提交→取消→cancelled + find 进程组确实被回收。"""
        with self._client(tmp_path, monkeypatch) as (c, _api, _m, _root, _s):
            tid = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5,
                                                  "wait": "false"}).json()["task_id"]
            _wait_instances(1)
            proc = _FakePopen.instances[0]
            r = c.post("/api/bigfiles/cancel", json={"task_id": tid})
            assert r.status_code == 200, r.text
            assert r.json()["task_id"] == tid
            assert r.json()["cancel_requested"] is True
            body = _poll_state(c, tid, {"cancelled"})
            assert body["terminal"] is True
            assert body["state"] == "cancelled"
            _wait_proc_reaped(proc)
            assert proc.returncode != 0, "被取消的 find 应由信号终止"

    def test_cancel_only_own_task_two_concurrent(self, tmp_path, monkeypatch):
        """双任务并发：取消其一，另一任务不受波及（各自进程独立回收）。"""
        with self._client(tmp_path, monkeypatch) as (c, _api, _m, root, _s):
            (root / "a").mkdir()
            (root / "b").mkdir()
            ta = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5,
                                                 "path": str(root / "a"),
                                                 "wait": "false"}).json()["task_id"]
            tb = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5,
                                                 "path": str(root / "b"),
                                                 "wait": "false"}).json()["task_id"]
            assert ta != tb
            _wait_instances(2)
            proc_a, proc_b = _FakePopen.instances[0], _FakePopen.instances[1]
            assert c.post("/api/bigfiles/cancel",
                          json={"task_id": ta}).status_code == 200
            assert _poll_state(c, ta, {"cancelled"})["state"] == "cancelled"
            # 未被取消的任务：取消请求不得把它变成 cancelled
            st_b = c.get("/api/bigfiles/status", params={"task_id": tb}).json()
            assert st_b["state"] == "running", "取消 A 不得波及 B"
            assert st_b["cancel_requested"] is False
            # B 仍可自行被取消（进程组互不干扰，两个 find 都已被回收）
            assert c.post("/api/bigfiles/cancel",
                          json={"task_id": tb}).status_code == 200
            assert _poll_state(c, tb, {"cancelled"})["state"] == "cancelled"
            _wait_proc_reaped(proc_a)
            _wait_proc_reaped(proc_b)
            assert proc_a.returncode != 0 and proc_b.returncode != 0

    def test_cancel_unknown_task_id_404(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch, rounds=1) as (c, _api, _m, _r, _s):
            r = c.post("/api/bigfiles/cancel", json={"task_id": "bf-0000nope"})
            assert r.status_code == 404, r.text
            assert "task_id" in r.json()["detail"]

    def test_cancel_is_idempotent(self, tmp_path, monkeypatch):
        """重复取消已取消任务：幂等 200，状态如实 cancelled，不报错。"""
        with self._client(tmp_path, monkeypatch) as (c, _api, _m, _root, _s):
            tid = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5,
                                                  "wait": "false"}).json()["task_id"]
            _wait_instances(1)
            first = c.post("/api/bigfiles/cancel", json={"task_id": tid})
            assert first.status_code == 200
            _poll_state(c, tid, {"cancelled"})
            _wait_proc_reaped(_FakePopen.instances[0])
            for _ in range(2):
                again = c.post("/api/bigfiles/cancel", json={"task_id": tid})
                assert again.status_code == 200, again.text
                assert again.json()["state"] == "cancelled"
                assert again.json()["cancelled"] is True

    def test_cancel_finished_task_returns_truthful_state(self, tmp_path, monkeypatch):
        """已完成任务的取消：幂等 200 且状态如实（不谎称已取消）。"""
        with self._client(tmp_path, monkeypatch, rounds=1) as (c, _api, _m, _r, _s):
            body = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5})
            assert body.status_code == 200, body.text
            tid = body.json()["task_id"]
            r = c.post("/api/bigfiles/cancel", json={"task_id": tid})
            assert r.status_code == 200, r.text
            after = r.json()
            assert after["cancelled"] is False
            assert after["state"] in {s.value for s in bigfiles.BigfilesState}
            assert after["terminal"] is True
            assert after["cancel_requested"] is False

    def test_cancel_after_ttl_retention_is_404(self, tmp_path, monkeypatch):
        """终态句柄过保留期即 404（不冒充「他人任务」，句柄即凭证）。"""
        with self._client(tmp_path, monkeypatch, rounds=1,
                          cache_ttl_s=0.0) as (c, api, manager, _r, _s):
            manager._task_retention_s = 0.0
            tid = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5}).json()["task_id"]
            assert c.get("/api/bigfiles/status",
                         params={"task_id": tid}).status_code == 200
            # 另一组参数触发剪枝（保留期为 0 → 终态条目即刻失效）
            assert c.get("/api/bigfiles", params={"min_mb": 1,
                                                   "topn": 6}).status_code == 200
            r = c.post("/api/bigfiles/cancel", json={"task_id": tid})
            assert r.status_code == 404, r.text
            assert c.get("/api/bigfiles/status",
                         params={"task_id": tid}).status_code == 404

    # ---------- 状态 ----------

    def test_status_reports_running_then_cancelled(self, tmp_path, monkeypatch):
        """status 与既有五态对齐：running → 取消后 cancelled。"""
        with self._client(tmp_path, monkeypatch) as (c, _api, _m, _root, _s):
            tid = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5,
                                                  "wait": "false"}).json()["task_id"]
            st = c.get("/api/bigfiles/status", params={"task_id": tid})
            assert st.status_code == 200, st.text
            sbody = st.json()
            assert sbody["state"] == "running"
            assert sbody["terminal"] is False
            assert sbody["cancel_requested"] is False
            assert sbody["task_id"] == tid
            assert sbody["scope"]["min_mb"] == 1 and sbody["scope"]["topn"] == 5
            assert "files" not in sbody, "结果本体仍从原端点按需取"
            c.post("/api/bigfiles/cancel", json={"task_id": tid})
            assert _poll_state(c, tid, {"cancelled"})["terminal"] is True

    def test_status_of_completed_task_reports_enum_state(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch, rounds=1) as (c, _api, _m, root, _s):
            body = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5}).json()
            st = c.get("/api/bigfiles/status",
                       params={"task_id": body["task_id"]}).json()
            assert st["state"] in {s.value for s in bigfiles.BigfilesState}
            assert st["terminal"] is True
            assert st["finished_at"] >= st["created_at"]

    def test_status_unknown_task_id_404_and_missing_param_400(
            self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch, rounds=1) as (c, _api, _m, _r, _s):
            r = c.get("/api/bigfiles/status", params={"task_id": "bf-nope-nope"})
            assert r.status_code == 404, r.text
            assert c.get("/api/bigfiles/status").status_code == 400

    # ---------- 安全合同 ----------

    def test_cancel_requires_write_token(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import fathom.api as api_mod
        root = tmp_path / "scanroot"
        root.mkdir()
        monkeypatch.setattr(config, "DEFAULT_ROOT", root)
        monkeypatch.setattr(api_mod, "_BIGFILES_MANAGER", None)
        tid = "bf-0000000000000000"
        with TestClient(api_mod.app,
                        base_url=f"http://127.0.0.1:{config.PORT}") as c:
            r = c.post("/api/bigfiles/cancel", json={"task_id": tid})
            assert r.status_code == 403, r.text
            assert "X-Fathom-Token" in r.json()["detail"]
            assert c.post("/api/bigfiles/cancel", json={"task_id": tid},
                          headers={"X-Fathom-Token": ""}).status_code == 403
            assert c.post("/api/bigfiles/cancel", json={"task_id": tid},
                          headers={"X-Fathom-Token": "forged"}).status_code == 403
            # 拒绝发生在守卫层：未知 task_id 也不会走到 handler（403 而非 404）
            assert api_mod._WRITE_TOKEN not in r.text

    def test_cancel_bad_body_400(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch, rounds=1) as (c, _api, _m, _r, _s):
            for payload in ({}, {"task_id": ""}, {"task_id": 7},
                            {"task_id": "x", "extra": 1}):
                r = c.post("/api/bigfiles/cancel", json=payload)
                assert r.status_code == 400, (payload, r.status_code, r.text)

    def test_read_endpoints_stay_token_free(self, tmp_path, monkeypatch):
        """读端点（提交/状态）沿用既有守卫口径：GET 免写令牌。"""
        from fastapi.testclient import TestClient
        import fathom.api as api_mod
        root = tmp_path / "scanroot"
        root.mkdir()
        monkeypatch.setattr(config, "DEFAULT_ROOT", root)
        monkeypatch.setattr(api_mod, "_BIGFILES_MANAGER", None)
        with TestClient(api_mod.app,
                        base_url=f"http://127.0.0.1:{config.PORT}") as c:
            r = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5})
            assert r.status_code == 200, r.text
            tid = r.json()["task_id"]
            assert c.get("/api/bigfiles/status",
                         params={"task_id": tid}).status_code == 200
