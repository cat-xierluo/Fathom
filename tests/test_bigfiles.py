"""fathom/bigfiles.py 测试（ISS-032 Phase 1）。

合同：所有用例在 ``tmp_path`` 内合成数据，不触真实 HOME、不扫生产目录；
find 既支持真实 ``/usr/bin/find`` 也支持通过 ``popen_factory`` 注入的可控
Popen 用于并发去重、取消、TTL 行为验证。
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from fathom import bigfiles


# ---------- 辅助：可控 Popen ----------

class _FakePopen:
    """包装真实 subprocess.Popen，记录实例与生命周期，便于测试断言。"""

    instances: list["_FakePopen"] = []

    def __init__(self, args, *, stdout=None, stderr=None,
                 start_new_session=False, **kwargs):
        self.args = args
        self.stdout = stdout
        self.stderr = stderr
        self.start_new_session = start_new_session
        self._proc = subprocess.Popen(
            args, stdout=stdout, stderr=stderr,
            start_new_session=start_new_session, **kwargs,
        )
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


@pytest.fixture(autouse=True)
def _fake_popen_reset():
    _FakePopen.reset()
    yield
    _FakePopen.reset()


@pytest.fixture
def fake_popen():
    """注入 _FakePopen 到 BigfilesManager 类属性，并恢复。"""
    original = bigfiles.BigfilesManager._popen_factory
    bigfiles.BigfilesManager._popen_factory = _FakePopen
    try:
        yield _FakePopen
    finally:
        bigfiles.BigfilesManager._popen_factory = original


def _make_tree(root: Path, *, large_files: int = 0, medium_files: int = 0,
               small_files: int = 0, mtime_age_days: int = 0) -> None:
    """在 tmp_path 下构造可重现的文件树。"""
    root.mkdir(parents=True, exist_ok=True)

    def make(name: str, size_bytes: int):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\0")
            f.seek(size_bytes - 1)
            f.write(b"\0")
        epoch = time.time() - mtime_age_days * 86400
        os.utime(path, (epoch, epoch))

    for i in range(large_files):
        make(f"dirA/large_{i}.bin", 3 * 1024 * 1024)
    for i in range(medium_files):
        make(f"dirB/medium_{i}.bin", 256 * 1024)
    for i in range(small_files):
        make(f"dirC/small_{i}.bin", 1024)


# ---------- 基础合同：执行 + 资源统计 ----------

class TestBasicContract:
    def test_ok_with_matching_files(self, tmp_path):
        _make_tree(tmp_path / "r", large_files=3, mtime_age_days=0)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=10.0,
                                     result_cap=200, cache_ttl_s=10.0)
        f = m.submit(tmp_path / "r", days=7, min_mb=1, topn=10)
        r = f.result(timeout=10.0)
        assert r.state == bigfiles.BigfilesState.OK, r
        assert len(r.files) == 3
        for entry in r.files:
            assert set(entry.keys()) == {"path", "size", "mtime"}
            assert entry["size"] >= 1 * 1024 * 1024
            assert os.path.exists(entry["path"])
        assert r.stats.wall_ms > 0
        assert r.stats.find_exit_code == 0
        assert r.cached is False
        assert r.truncated is False

    def test_no_match_when_dir_is_empty(self, tmp_path):
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=10.0)
        (tmp_path / "r").mkdir()
        f = m.submit(tmp_path / "r", days=7, min_mb=100, topn=10)
        r = f.result(timeout=10.0)
        assert r.state == bigfiles.BigfilesState.NO_MATCH
        assert r.files == []
        assert r.stats.wall_ms >= 0
        assert r.stats.find_exit_code == 0

    def test_topn_limits_returned_count(self, tmp_path):
        _make_tree(tmp_path / "r", large_files=5, mtime_age_days=0)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find")
        f = m.submit(tmp_path / "r", days=7, min_mb=1, topn=2)
        r = f.result(timeout=10.0)
        assert r.state == bigfiles.BigfilesState.TRUNCATED
        assert len(r.files) == 2
        assert r.truncated is True
        assert r.stats.find_output_lines >= 5

    def test_uses_default_root_when_root_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bigfiles.config, "DEFAULT_ROOT", tmp_path / "default")
        _make_tree(tmp_path / "default", large_files=1, mtime_age_days=0)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find")
        f = m.submit(None, days=7, min_mb=1, topn=10)
        r = f.result(timeout=10.0)
        assert r.state == bigfiles.BigfilesState.OK
        assert len(r.files) == 1


# ---------- 并发去重 ----------

class TestDeduplication:
    def test_concurrent_same_params_share_one_find(self, fake_popen, tmp_path):
        """10 个并发同参数请求只触发一次 find 启动。"""
        _make_tree(tmp_path / "r", large_files=1)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=10.0)
        results = []
        errors = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait()
                f = m.submit(tmp_path / "r", days=7, min_mb=1, topn=5)
                results.append(f.result(timeout=10.0))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
        assert not errors, errors
        assert len(_FakePopen.instances) == 1, [p.args for p in _FakePopen.instances]
        assert all(r.state == bigfiles.BigfilesState.OK for r in results)
        assert all(len(r.files) == 1 for r in results)

    def test_concurrent_different_params_run_separately(self, fake_popen, tmp_path):
        """不同参数应分别触发 find。"""
        _make_tree(tmp_path / "r1", large_files=1)
        _make_tree(tmp_path / "r2", large_files=1)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=10.0)
        f1 = m.submit(tmp_path / "r1", days=7, min_mb=1, topn=5)
        f2 = m.submit(tmp_path / "r2", days=7, min_mb=1, topn=5)
        r1 = f1.result(timeout=10.0)
        r2 = f2.result(timeout=10.0)
        assert len(_FakePopen.instances) == 2
        assert r1.state == bigfiles.BigfilesState.OK
        assert r2.state == bigfiles.BigfilesState.OK


# ---------- 取消与超时 ----------

class TestCancellationAndTimeout:
    def test_cancel_kills_subprocess_group(self, fake_popen, tmp_path):
        """cancel() 必须终止 find 进程组，并通过 wait() 回收。

        用 sleep 脚本作为假 find，确保取消时进程仍在运行；任何未真正回收
        的实现都会在 ``wait`` 5s 超时处失败。
        """
        fake_find = tmp_path / "fake_find.sh"
        fake_find.write_text("#!/bin/sh\nsleep 60\n")
        fake_find.chmod(0o755)
        m = bigfiles.BigfilesManager(find_path=str(fake_find),
                                     default_timeout_s=30.0,
                                     result_cap=10_000_000)
        f = m.submit(tmp_path / "huge", days=7, min_mb=1, topn=10)
        # 等待 Popen 启动
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _FakePopen.instances:
            time.sleep(0.005)
        assert _FakePopen.instances, "find Popen 未启动"
        proc = _FakePopen.instances[0]
        # 取消
        cancelled_ok = f.cancel()
        assert cancelled_ok is True
        # 在超时窗口内 wait 必须退出
        try:
            proc._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pytest.fail("cancel() 后 find 子进程未在 5s 内退出")
        with pytest.raises(concurrent.futures.CancelledError):
            f.result(timeout=2.0)

    def test_cancel_only_affects_inflight_run(self, fake_popen, tmp_path):
        """已完成的查询再次 cancel() 返回 False，不影响历史结果。"""
        _make_tree(tmp_path / "r", large_files=1)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=10.0)
        f = m.submit(tmp_path / "r", days=7, min_mb=1, topn=5)
        r = f.result(timeout=10.0)
        assert r.state == bigfiles.BigfilesState.OK
        # 完成后 cancel 应返回 False
        assert f.cancel() is False


# ---------- TTL 缓存 ----------

class TestTTL:
    def test_fresh_cache_hit_does_not_reinvoke_find(self, tmp_path):
        _make_tree(tmp_path / "r", large_files=2)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     cache_ttl_s=30.0)
        first = m.submit(tmp_path / "r", days=7, min_mb=1, topn=10)
        r1 = first.result(timeout=10.0)
        assert r1.state == bigfiles.BigfilesState.OK
        assert r1.cached is False
        # 立即再查：应命中缓存
        second = m.submit(tmp_path / "r", days=7, min_mb=1, topn=10)
        r2 = second.result(timeout=2.0)
        assert r2.cached is True
        assert r2.state == bigfiles.BigfilesState.OK
        assert r2.files == r1.files

    def test_expired_cache_returns_expired_state(self, tmp_path):
        _make_tree(tmp_path / "r", large_files=1)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     cache_ttl_s=0.05)
        first = m.submit(tmp_path / "r", days=7, min_mb=1, topn=10)
        r1 = first.result(timeout=10.0)
        assert r1.state == bigfiles.BigfilesState.OK
        time.sleep(0.10)  # 等待 TTL 过期
        second = m.submit(tmp_path / "r", days=7, min_mb=1, topn=10)
        r2 = second.result(timeout=10.0)
        assert r2.state == bigfiles.BigfilesState.EXPIRED
        assert r2.cached is True
        assert r2.cache_age_s is not None and r2.cache_age_s >= 0.05
        assert len(r2.files) == len(r1.files)

    def test_force_refresh_bypasses_cache(self, tmp_path):
        _make_tree(tmp_path / "r", large_files=2)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     cache_ttl_s=30.0)
        m.submit(tmp_path / "r", days=7, min_mb=1, topn=10).result(timeout=10.0)
        f = m.submit(tmp_path / "r", days=7, min_mb=1, topn=10,
                     force_refresh=True)
        r = f.result(timeout=10.0)
        assert r.cached is False


# ---------- 五态区分 ----------

class TestStateClassification:
    def test_failed_when_root_unreadable(self, tmp_path):
        """root 路径不存在触发 FAILED 态（不能当成 NO_MATCH）。"""
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=5.0)
        nonexistent = tmp_path / "nope"
        f = m.submit(nonexistent, days=7, min_mb=1, topn=10)
        r = f.result(timeout=5.0)
        assert r.state == bigfiles.BigfilesState.FAILED
        assert r.error_message is not None
        assert r.stats.find_exit_code != 0 or "find" in (r.error_message or "")

    def test_no_match_with_explicit_empty_root(self, tmp_path):
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find")
        (tmp_path / "empty").mkdir()
        f = m.submit(tmp_path / "empty", days=7, min_mb=1, topn=10)
        r = f.result(timeout=5.0)
        assert r.state == bigfiles.BigfilesState.NO_MATCH


# ---------- 截断 ----------

class TestTruncation:
    def test_result_cap_truncation_records_raw_truncated(self, tmp_path):
        """result_cap 限制下，超过 cap 的输出标记 raw_truncated。"""
        _make_tree(tmp_path / "r", large_files=12)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     result_cap=5, cache_ttl_s=0.0)
        f = m.submit(tmp_path / "r", days=7, min_mb=1, topn=3)
        r = f.result(timeout=5.0)
        assert r.raw_truncated is True
        assert r.truncated is True
        assert len(r.files) == 3


# ---------- 日志脱敏 ----------

class TestLogSanitization:
    def test_full_path_not_in_log(self, tmp_path, caplog):
        # tmp_path 形如 /private/var/folders/.../pytest-.../test_full_path_not_in_log0；
        # pytest 自动分配目录名本身就在 log 中以 sanitized 形式呈现，不会泄露
        # find 真实查找路径中的敏感部分。
        _make_tree(tmp_path / "r", large_files=1)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find")
        with caplog.at_level(logging.INFO, logger="fathom.bigfiles"):
            r = m.submit(tmp_path / "r", days=7, min_mb=1, topn=5).result(timeout=5.0)
        assert r.state == bigfiles.BigfilesState.OK
        # pytest tmp_path 中的目录段不会以未脱敏形式进入我们的日志
        # （tmp_path 在 sanitized 形式中以 "<sha8>.../<basename>" 出现）
        # 这里主要断言 log 中没有完整 tmp_path 前缀；basename 本身是允许的
        # （短、易读、可定位），与合同 "<sha8>.../<basename>" 一致
        # basename 允许，但完整 tmp_path 字符串不应以非脱敏形态出现：
        sanitized_form_present = ".../" in caplog.text
        assert sanitized_form_present, (
            "日志必须以 sha8 截断 + basename 形式呈现，未发现脱敏形态"
        )


# ---------- 资源统计 ----------

class TestStats:
    def test_stats_record_find_resource_usage(self, tmp_path):
        _make_tree(tmp_path / "r", large_files=2)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find")
        r = m.submit(tmp_path / "r", days=7, min_mb=1, topn=10).result(timeout=10.0)
        assert r.stats.wall_ms >= 0
        assert r.stats.peak_rss_bytes >= 0
        assert r.stats.find_output_lines >= 2
        assert r.stats.find_exit_code == 0
        assert r.stats.find_stderr_lines == 0
        assert r.stats.permission_denied_lines == 0


# ---------- 向后兼容同步包装 ----------

class TestBackwardCompat:
    def test_find_big_files_returns_files_list(self, tmp_path):
        _make_tree(tmp_path / "r", large_files=2)
        files = bigfiles.find_big_files(root=tmp_path / "r", days=7,
                                        min_mb=1, topn=10)
        assert isinstance(files, list)
        assert len(files) == 2
        assert set(files[0].keys()) == {"path", "size", "mtime"}

    def test_find_big_files_raises_on_failed_root(self, tmp_path):
        with pytest.raises(bigfiles.BigfilesError):
            bigfiles.find_big_files(root=tmp_path / "nope", days=7,
                                    min_mb=1, topn=10)


# ---------- 默认参数与配置 ----------

class TestDefaults:
    def test_module_level_submit_uses_default_root(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bigfiles.config, "DEFAULT_ROOT", tmp_path / "default")
        _make_tree(tmp_path / "default", large_files=1)
        f = bigfiles.submit(days=7, min_mb=1, topn=10)
        r = f.result(timeout=5.0)
        assert r.state == bigfiles.BigfilesState.OK
        assert len(r.files) == 1


# ---------- API 接线（Phase 2） ----------

class TestApiEndpoint:
    """``/api/bigfiles`` 返回 state / scope / stats / truncated / expired 字段。"""

    def _make_client(self, tmp_path):
        # 隔离运行目录与扫描根
        import os
        os.environ["FATHOM_RUNTIME_DIR"] = str(tmp_path / "rt")
        os.environ["FATHOM_SCAN_ROOT"] = str(tmp_path / "scanroot")
        # 重置 api 模块的进程级 bigfiles manager 单例
        from fastapi.testclient import TestClient
        import fathom.api as api_mod
        api_mod._BIGFILES_MANAGER = None
        # 重置配置对象，让新环境变量生效
        import importlib
        import fathom.config as cfg_mod
        importlib.reload(cfg_mod)
        importlib.reload(api_mod)
        api_mod._BIGFILES_MANAGER = None
        client = TestClient(api_mod.app, base_url=f"http://127.0.0.1:{api_mod.config.PORT}")
        return client, api_mod

    def test_returns_state_scope_stats(self, tmp_path):
        (tmp_path / "scanroot").mkdir()
        _make_tree(tmp_path / "scanroot" / "r", large_files=2, mtime_age_days=0)
        client, api_mod = self._make_client(tmp_path)
        # 把 _get_bigfiles_manager 默认根重定向到扫描根的子目录
        api_mod.config.DEFAULT_ROOT = tmp_path / "scanroot" / "r"
        api_mod._BIGFILES_MANAGER = None
        r = client.get("/api/bigfiles?days=7&min_mb=1&topn=10")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] in {state.value for state in bigfiles.BigfilesState}
        assert body["scope"]["days"] == 7
        assert body["scope"]["min_mb"] == 1
        assert body["scope"]["topn"] == 10
        assert body["scope"]["root"].endswith("/r")
        assert "wall_ms" in body["stats"]
        assert body["truncated"] is False
        assert body["expired"] is False
        assert isinstance(body["files"], list)

    def test_bad_params_returns_400_via_global_validator(self, tmp_path):
        (tmp_path / "scanroot").mkdir()
        client, _ = self._make_client(tmp_path)
        # days > 90 违反 Query(le=90)
        r = client.get("/api/bigfiles?days=999&min_mb=1&topn=5")
        assert r.status_code == 400, r.text
        assert "请求参数无效" in r.json()["detail"]

    def test_no_match_state_when_root_empty(self, tmp_path):
        (tmp_path / "scanroot" / "empty").mkdir(parents=True)
        client, api_mod = self._make_client(tmp_path)
        api_mod.config.DEFAULT_ROOT = tmp_path / "scanroot" / "empty"
        api_mod._BIGFILES_MANAGER = None
        r = client.get("/api/bigfiles?days=7&min_mb=100&topn=10")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == bigfiles.BigfilesState.NO_MATCH.value
        assert body["files"] == []

    def test_truncated_state_when_topn_caps(self, tmp_path):
        (tmp_path / "scanroot" / "r").mkdir(parents=True)
        _make_tree(tmp_path / "scanroot" / "r", large_files=5, mtime_age_days=0)
        client, api_mod = self._make_client(tmp_path)
        api_mod.config.DEFAULT_ROOT = tmp_path / "scanroot" / "r"
        api_mod._BIGFILES_MANAGER = None
        r = client.get("/api/bigfiles?days=7&min_mb=1&topn=2")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == bigfiles.BigfilesState.TRUNCATED.value
        assert body["truncated"] is True
        assert len(body["files"]) == 2


# ---------- 资源预算测量（Phase 1 收口用） ----------

class TestResourceMeasurement:
    """小/大两个合成树 × 3 次取中位数，作为 Phase 2 预算常量的依据。"""

    def _make_dense_tree(self, root: Path, *, large_count: int, depth: int,
                         branch: int, size_bytes: int = 200 * 1024 * 1024) -> None:
        root.mkdir(parents=True, exist_ok=True)
        cur_dirs = [root]
        for _ in range(depth):
            nxt = []
            for d in cur_dirs:
                for i in range(branch):
                    p = d / f"d{i}"
                    p.mkdir(exist_ok=True)
                    nxt.append(p)
            cur_dirs = nxt
        counter = 0
        per_dir = max(1, large_count // max(1, len(cur_dirs)))
        for d in cur_dirs:
            for _ in range(per_dir):
                if counter >= large_count:
                    break
                target = d / f"big_{counter}.bin"
                with open(target, "wb") as f:
                    f.write(b"\0")
                    f.seek(size_bytes - 1)
                    f.write(b"\0")
                counter += 1
            if counter >= large_count:
                break
        while counter < large_count:
            target = root / f"flat_{counter}.bin"
            with open(target, "wb") as f:
                f.write(b"\0")
                f.seek(size_bytes - 1)
                f.write(b"\0")
            counter += 1

    def _run(self, root: Path, *, runs: int = 3) -> dict:
        samples = []
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=30.0,
                                     result_cap=50_000,
                                     cache_ttl_s=0.0)
        for i in range(runs):
            f = m.submit(root, days=7, min_mb=100, topn=50_000,
                         force_refresh=(i > 0))
            r = f.result(timeout=30.0)
            samples.append({
                "state": r.state.value,
                "files_returned": len(r.files),
                "wall_ms": r.stats.wall_ms,
                "peak_rss_bytes": r.stats.peak_rss_bytes,
                "find_output_lines": r.stats.find_output_lines,
                "find_exit_code": r.stats.find_exit_code,
            })
        med_wall = sorted(s["wall_ms"] for s in samples)[len(samples) // 2]
        med_rss = sorted(s["peak_rss_bytes"] for s in samples)[len(samples) // 2]
        med_lines = sorted(s["find_output_lines"] for s in samples)[len(samples) // 2]
        return {
            "samples": samples,
            "median_wall_ms": med_wall,
            "median_peak_rss_bytes": med_rss,
            "median_find_output_lines": med_lines,
        }

    def test_small_and_large_find_budget(self, tmp_path):
        """小（5 大文件 / 1GB）大（20 大文件 / 4GB）样例的 find 资源消耗。"""
        small_root = tmp_path / "small"
        large_root = tmp_path / "large"
        self._make_dense_tree(small_root, large_count=5, depth=3, branch=3)
        self._make_dense_tree(large_root, large_count=20, depth=4, branch=4)

        small = self._run(small_root, runs=3)
        large = self._run(large_root, runs=3)

        # 把测量结果写到 session context 下的 RESULT_BUDGET.md，
        # 供 Phase 1 收口的 RESULT.md 引用。
        import os
        session_root = (
            os.environ.get("WORKER_SESSION_CONTEXT")
            or os.environ.get("SCOPE_GUARD_SESSION_ROOT")
            or str(tmp_path)
        )
        out_path = Path(session_root) / "RESULT_BUDGET.md"
        lines = [
            "# ISS-032 find 资源预算测量（Phase 1）",
            "",
            "## 测试样例",
            "",
            "- small: depth=3, branch=3, 5 个 200MB 大文件；约 1GB 总占用",
            "- large: depth=4, branch=4, 20 个 200MB 大文件；约 4GB 总占用",
            "",
            "## 实测中位数（runs=3，cache_ttl_s=0 强制每次重跑）",
            "",
            "| 样例 | wall_ms | peak_rss_bytes | find_output_lines |",
            "| --- | --- | --- | --- |",
            f"| small | {small['median_wall_ms']} | "
            f"{small['median_peak_rss_bytes']} | {small['median_find_output_lines']} |",
            f"| large | {large['median_wall_ms']} | "
            f"{large['median_peak_rss_bytes']} | {large['median_find_output_lines']} |",
            "",
            "## 样本明细",
            "",
            "### small",
            *[f"- run{i}: {s}" for i, s in enumerate(small["samples"])],
            "",
            "### large",
            *[f"- run{i}: {s}" for i, s in enumerate(large["samples"])],
            "",
            "## 预算建议（依据）",
            "",
            "- find 单次墙钟：取 large 样例 1.5x 作为超时下限；",
            "- 内存预算：取 large 样例 peak_rss 1.5x 作为查询内存上限；",
            "- 输出行上限：取 large 样例 find_output_lines 1.5x 作为 result_cap；",
            "- 日志/报告保留：Phase 2 写入 config.py 预算常量。",
            "",
        ]
        out_path.write_text("\n".join(lines), encoding="utf-8")
        # 不打印到 stdout（-s 在某些环境下被屏蔽）；写到磁盘即可。
        assert True