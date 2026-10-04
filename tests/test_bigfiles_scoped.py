"""ISS-150：限定目录的当前最大文件查询（largest 模式与范围约束）。

合同（父卡 ISS-144）：
- 显式用户请求传 path 与 mode=largest/recent；largest 按当前 st_size 逻辑大小
  排序、不带 mtime 过滤，recent 保留现有行为（含 files 字段集合合同）。
- 查询目录限制到有效配置允许的根/已选择子目录；``..``、相似前缀根、
  符号链接越界一律拒绝；路径已移走返回 404 语义；不因历史路径存在而允许
  读取任意盘。
- 缓存/并发去重键含规范根、模式、参数及范围配置版本（scope_version 接缝，
  本卡不实现卷身份）。
- 完整遍历后才称「当前最大」；输出/时间预算提前截断时 state=TRUNCATED 且
  ``incomplete=True``，仅称「已检查文件中的较大项」。取消绑定本请求 key，
  只回收自有进程。
- 特殊文件名经 ``-print0`` 完整保留；稀疏文件带逻辑大小/物理占用标识。
- 全部用例在 tmp_path 合成数据，不触真实 HOME、不扫生产目录。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fathom import bigfiles, cli, config


# ---------- 辅助 ----------

def _make_file(path: Path, size_bytes: int, *, mtime_age_days: float = 0.0,
               sparse: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        if not sparse:
            f.write(b"\0")
            f.seek(size_bytes - 1)
            f.write(b"\0")
        else:
            # 稀疏：truncate 产生洞（APFS 实测 seek+write 不省块；truncate 块数为 0）
            f.truncate(size_bytes)
    epoch = time.time() - mtime_age_days * 86400
    os.utime(path, (epoch, epoch))


class _FakeStat:
    """duck-typed stat 结果，供 stat_fn 注入（只提供大文件路径用到的属性）。"""

    def __init__(self, size: int, blocks: int | None = None) -> None:
        self.st_size = size
        self.st_blocks = (size // 512) if blocks is None else blocks
        self.st_mtime = time.time()


class _FakePopen:
    """包装真实 subprocess.Popen，记录实例与生命周期。"""

    instances: list["_FakePopen"] = []

    def __init__(self, args, *, stdout=None, stderr=None,
                 start_new_session=False, **kwargs):
        self.args = args
        self._proc = subprocess.Popen(
            args, stdout=stdout, stderr=stderr,
            start_new_session=start_new_session, **kwargs,
        )
        # 与真实 Popen 同形：stdout/stderr 是流对象（PIPE 哨兵是 -1，
        # 直接暴露会让 largest 的 selectors 读到 fd -1）
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


@pytest.fixture(autouse=True)
def _fake_popen_reset():
    _FakePopen.reset()
    yield
    _FakePopen.reset()


@pytest.fixture
def fake_popen():
    original = bigfiles.BigfilesManager._popen_factory
    bigfiles.BigfilesManager._popen_factory = _FakePopen
    try:
        yield _FakePopen
    finally:
        bigfiles.BigfilesManager._popen_factory = original


def _wait_instances(deadline_s: float = 5.0) -> list[_FakePopen]:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end and not _FakePopen.instances:
        time.sleep(0.005)
    return _FakePopen.instances


def _explicit_popen_factory(real_argv0: str):
    """把 find_path 视为脚本、以显式解释器启动的 popen_factory。

    pytest 的 fd capture 环境下，shebang 方式（argv[0]=脚本）执行会静默
    失败（无输出、无 stderr），显式解释器正常；生产 find 是 Mach-O 二进制
    不经 shebang，不受影响。本工厂仅服务测试夹具。
    """
    def factory(args, **kwargs):
        return _FakePopen([real_argv0, *args], **kwargs)
    return factory


def _slow_find_script(tmp_path: Path, name: str, *, rounds: int = 0,
                      interval: float = 0.02) -> Path:
    """慢速假 find：周期性输出带 NUL 的路径；rounds=0 表示无限输出。

    用 perl（``$|=1`` 逐条 flush，毫秒级首输出）写 ``<base>/big_N.bin\0``
    形式路径（真实子进程、真实管道）；sh + /usr/bin/printf 每轮 fork 开销
    约 0.18s，会吞掉小时间预算，故不采用。配合 stat_fn 注入即可在不落盘的
    情况下驱动 largest 的增量读取路径。
    """
    script = tmp_path / name
    base = str(tmp_path / "virtual")
    if rounds:
        cond = f"$i < {rounds}"
    else:
        cond = "1"
    q_open, q_close = chr(123), chr(125)  # perl qq 定界符，避开 f-string 花括号转义
    body = (f"$|=1; $i=0; while ({cond}) {{ "
            f"print qq{q_open}{base}/big_$i.bin\\0{q_close}; "
            f"select(undef, undef, undef, {interval}); $i++; }}\n")
    script.write_text(f"#!/usr/bin/perl\n{body}")
    script.chmod(0o755)
    return script


# ---------- 反例复现：largest 必须看到 recent 看不到的旧大文件 ----------

class TestCounterexamples:
    def test_largest_sees_old_large_file_invisible_to_recent(self, tmp_path):
        """反例 1：最大文件 mtime 早于 90 天——recent 看不到，largest 必须看到。"""
        root = tmp_path / "r"
        _make_file(root / "old_huge.bin", 5 * 1024 * 1024, mtime_age_days=100)
        _make_file(root / "new_small.bin", 512 * 1024, mtime_age_days=0)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=15.0, cache_ttl_s=0.0)
        recent = m.submit(root, days=90, min_mb=1, topn=10,
                          mode="recent").result(timeout=15.0)
        assert recent.state == bigfiles.BigfilesState.NO_MATCH
        assert recent.files == []
        largest = m.submit(root, days=90, min_mb=1, topn=10,
                           mode="largest").result(timeout=15.0)
        assert largest.state == bigfiles.BigfilesState.OK, largest
        names = [Path(f["path"]).name for f in largest.files]
        assert "old_huge.bin" in names
        assert largest.incomplete is False

    def test_nested_query_excludes_sibling_files(self, tmp_path, monkeypatch):
        """反例 2：查询嵌套子目录时不得包含父级兄弟目录的文件。"""
        monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "r")
        root = tmp_path / "r"
        _make_file(root / "sub1" / "mine.bin", 3 * 1024 * 1024)
        _make_file(root / "sub2" / "sibling.bin", 4 * 1024 * 1024)
        resolved = bigfiles.resolve_query_root(str(root / "sub1"))
        assert resolved == (root / "sub1").resolve()
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=15.0, cache_ttl_s=0.0)
        r = m.submit(resolved, days=90, min_mb=1, topn=10,
                     mode="largest").result(timeout=15.0)
        assert r.state == bigfiles.BigfilesState.OK, r
        names = {Path(f["path"]).name for f in r.files}
        assert names == {"mine.bin"}


# ---------- largest 语义 ----------

class TestLargestSemantics:
    def test_sorted_by_logical_size_desc(self, tmp_path):
        root = tmp_path / "r"
        _make_file(root / "s1.bin", 1 * 1024 * 1024 + 512 * 1024)
        _make_file(root / "s3.bin", 3 * 1024 * 1024)
        _make_file(root / "s2.bin", 2 * 1024 * 1024)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=15.0, cache_ttl_s=0.0)
        r = m.submit(root, days=90, min_mb=1, topn=10,
                     mode="largest").result(timeout=15.0)
        assert r.state == bigfiles.BigfilesState.OK, r
        sizes = [f["size"] for f in r.files]
        assert sizes == sorted(sizes, reverse=True)
        assert [Path(f["path"]).name for f in r.files] == ["s3.bin", "s2.bin", "s1.bin"]

    def test_sparse_file_logical_size_flag(self, tmp_path):
        """稀疏文件以 st_size 逻辑大小参与排序，并带物理占用/稀疏标识。"""
        root = tmp_path / "r"
        _make_file(root / "sparse.bin", 8 * 1024 * 1024, sparse=True)
        _make_file(root / "dense.bin", 2 * 1024 * 1024)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=15.0, cache_ttl_s=0.0)
        r = m.submit(root, days=90, min_mb=1, topn=10,
                     mode="largest").result(timeout=15.0)
        assert r.state == bigfiles.BigfilesState.OK, r
        by_name = {Path(f["path"]).name: f for f in r.files}
        sp = by_name["sparse.bin"]
        assert sp["size"] == 8 * 1024 * 1024  # 逻辑大小
        assert sp["sparse"] is True
        assert sp["blocks"] < sp["size"]
        dn = by_name["dense.bin"]
        assert dn["sparse"] is False
        assert dn["blocks"] >= dn["size"]

    def test_special_filenames_preserved(self, tmp_path):
        """空格/引号/换行等特殊文件名经 -print0 完整保留。"""
        root = tmp_path / "r"
        names = ["a b's \"q\".bin", "new\nline.bin", "中文 大.bin"]
        for name in names:
            _make_file(root / name, 2 * 1024 * 1024)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=15.0, cache_ttl_s=0.0)
        r = m.submit(root, days=90, min_mb=1, topn=10,
                     mode="largest").result(timeout=15.0)
        assert r.state == bigfiles.BigfilesState.OK, r
        got = {Path(f["path"]).name for f in r.files}
        assert got == set(names)

    def test_largest_entry_keys_extended(self, tmp_path):
        """largest 条目扩展 keys；recent 条目 keys 必须保持 {path,size,mtime}。"""
        root = tmp_path / "r"
        _make_file(root / "a.bin", 2 * 1024 * 1024)
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=15.0, cache_ttl_s=0.0)
        largest = m.submit(root, days=90, min_mb=1, topn=10,
                           mode="largest").result(timeout=15.0)
        assert set(largest.files[0].keys()) == {"path", "size", "mtime",
                                                "blocks", "sparse"}
        recent = m.submit(root, days=90, min_mb=1, topn=10,
                          mode="recent").result(timeout=15.0)
        assert set(recent.files[0].keys()) == {"path", "size", "mtime"}


# ---------- 范围解析（resolve_query_root） ----------

class TestScopeResolution:
    def test_default_root_when_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "r")
        (tmp_path / "r").mkdir()
        assert bigfiles.resolve_query_root(None) == (tmp_path / "r").resolve()

    def test_accepts_subdir_inside_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "r")
        (tmp_path / "r" / "sub").mkdir(parents=True)
        got = bigfiles.resolve_query_root(str(tmp_path / "r" / "sub"))
        assert got == (tmp_path / "r" / "sub").resolve()

    def test_rejects_prefix_sibling_root(self, tmp_path, monkeypatch):
        """相似前缀根（/r-evil 不是 /r）必须拒绝。"""
        monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "r")
        (tmp_path / "r").mkdir()
        (tmp_path / "r-evil").mkdir()
        with pytest.raises(bigfiles.BigfilesScopeError) as ei:
            bigfiles.resolve_query_root(str(tmp_path / "r-evil"))
        assert ei.value.status == 400

    def test_rejects_dotdot_escape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "r")
        (tmp_path / "r").mkdir()
        (tmp_path / "outside").mkdir()
        with pytest.raises(bigfiles.BigfilesScopeError) as ei:
            bigfiles.resolve_query_root(str(tmp_path / "r" / ".." / "outside"))
        assert ei.value.status == 400

    def test_rejects_relative_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "r")
        (tmp_path / "r").mkdir()
        with pytest.raises(bigfiles.BigfilesScopeError) as ei:
            bigfiles.resolve_query_root("sub")
        assert ei.value.status == 400

    def test_rejects_symlink_escape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "r")
        (tmp_path / "r").mkdir()
        (tmp_path / "outside").mkdir()
        (tmp_path / "r" / "link").symlink_to(tmp_path / "outside")
        with pytest.raises(bigfiles.BigfilesScopeError) as ei:
            bigfiles.resolve_query_root(str(tmp_path / "r" / "link"))
        assert ei.value.status == 400

    def test_moved_away_is_404_semantics(self, tmp_path, monkeypatch):
        """路径已移走（不存在）→ 404 语义，且不是空结果/越界混淆。"""
        monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "r")
        (tmp_path / "r" / "gone").mkdir(parents=True)
        import shutil
        shutil.rmtree(tmp_path / "r" / "gone")
        with pytest.raises(bigfiles.BigfilesScopeError) as ei:
            bigfiles.resolve_query_root(str(tmp_path / "r" / "gone"))
        assert ei.value.status == 404

    def test_error_message_does_not_leak_resolved_path(self, tmp_path, monkeypatch):
        """越界错误消息不得回显解析后的具体路径。"""
        monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "r")
        (tmp_path / "r").mkdir()
        (tmp_path / "outside").mkdir()
        with pytest.raises(bigfiles.BigfilesScopeError) as ei:
            bigfiles.resolve_query_root(str(tmp_path / "r" / ".." / "outside"))
        assert "outside" not in str(ei.value)


# ---------- 键与去重（mode / scope_version 接缝） ----------

class TestModeKeySemantics:
    def test_key_contains_mode_and_scope_version(self, tmp_path):
        q_recent = bigfiles.BigfilesQuery(root=tmp_path / "r", days=7,
                                          min_mb=1, topn=10, mode="recent",
                                          scope_version=1)
        q_largest = bigfiles.BigfilesQuery(root=tmp_path / "r", days=7,
                                           min_mb=1, topn=10, mode="largest",
                                           scope_version=1)
        q_scope2 = bigfiles.BigfilesQuery(root=tmp_path / "r", days=7,
                                          min_mb=1, topn=10, mode="recent",
                                          scope_version=2)
        keys = {q_recent.key, q_largest.key, q_scope2.key}
        assert len(keys) == 3
        assert q_recent.key[0] == str(tmp_path / "r")  # 日志清洗仍取 key[0]

    def test_invalid_mode_rejected(self, tmp_path):
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find")
        with pytest.raises(ValueError):
            m.submit(tmp_path / "r", days=7, min_mb=1, topn=10, mode="bogus")

    def test_concurrent_same_largest_params_share_one_find(self, fake_popen,
                                                           tmp_path):
        (tmp_path / "r").mkdir()
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=15.0, cache_ttl_s=0.0)
        f1 = m.submit(tmp_path / "r", days=90, min_mb=1, topn=5, mode="largest")
        f2 = m.submit(tmp_path / "r", days=90, min_mb=1, topn=5, mode="largest")
        r1 = f1.result(timeout=15.0)
        r2 = f2.result(timeout=15.0)
        assert len(_FakePopen.instances) == 1
        assert r2.cached is False  # 共享 in-flight，不是缓存命中

    def test_different_scope_version_not_shared(self, fake_popen, tmp_path):
        """范围配置版本不同 → 缓存/去重不共享（同参仅版本异视为不同查询）。"""
        (tmp_path / "r").mkdir()
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=15.0, cache_ttl_s=60.0)
        r1 = m.submit(tmp_path / "r", days=90, min_mb=1, topn=5, mode="largest",
                      scope_version=1).result(timeout=15.0)
        r2 = m.submit(tmp_path / "r", days=90, min_mb=1, topn=5, mode="largest",
                      scope_version=2).result(timeout=15.0)
        assert r1.cached is False
        assert r2.cached is False
        assert len(_FakePopen.instances) == 2

    def test_largest_cache_hit_same_params(self, tmp_path):
        (tmp_path / "r").mkdir()
        m = bigfiles.BigfilesManager(find_path="/usr/bin/find",
                                     default_timeout_s=15.0, cache_ttl_s=60.0)
        first = m.submit(tmp_path / "r", days=90, min_mb=1, topn=5,
                         mode="largest").result(timeout=15.0)
        second = m.submit(tmp_path / "r", days=90, min_mb=1, topn=5,
                          mode="largest").result(timeout=15.0)
        assert first.cached is False
        assert second.cached is True
        assert second.cache_age_s is not None

    def test_mode_default_is_recent(self, tmp_path):
        """不传 mode 的 submit 行为（键结构）与 recent 一致。"""
        q = bigfiles.BigfilesQuery(root=tmp_path / "r", days=7, min_mb=1, topn=10)
        assert q.mode == "recent"


# ---------- 预算与取消（真实子进程） ----------

class TestLargestBudgetAndCancel:
    def test_timeout_returns_partial_disclosure(self, tmp_path):
        """时间预算到点：回收进程、返回已检查部分并披露不完整。"""
        script = _slow_find_script(tmp_path, "slow_find.sh", interval=0.02)
        m = bigfiles.BigfilesManager(find_path=str(script),
                                     default_timeout_s=0.5, cache_ttl_s=0.0,
                                     popen_factory=_explicit_popen_factory("/usr/bin/perl"),
                                     stat_fn=lambda p: _FakeStat(3 * 1024 * 1024))
        f = m.submit(tmp_path / "r", days=90, min_mb=1, topn=50, mode="largest")
        r = f.result(timeout=10.0)
        assert r.state == bigfiles.BigfilesState.TRUNCATED, r
        assert r.incomplete is True
        assert r.error_message is not None and "较大项" in r.error_message
        assert r.error_message is not None and "不完整" in r.error_message
        assert len(r.files) > 0
        sizes = [x["size"] for x in r.files]
        assert sizes == sorted(sizes, reverse=True)
        # 进程确实被回收
        time.sleep(0.1)
        assert len(_FakePopen.instances) == 1
        assert _FakePopen.instances[0].poll() is not None

    def test_spawn_window_cancel_largest(self, tmp_path):
        """ISS-123 对齐：largest 的 spawn 窗口取消必须补投组信号。"""
        script = tmp_path / "sleep_find.sh"
        script.write_text("#!/bin/sh\nexec sleep 60\n")
        script.chmod(0o755)
        gate = threading.Event()

        def window_popen(args, **kwargs):
            # pytest fd capture 下 shebang 静默失败，显式解释器执行（见工厂注释）
            proc = _FakePopen(["/bin/sh", *args], **kwargs)
            assert gate.wait(timeout=10.0), "gate 未按预期释放"
            return proc

        m = bigfiles.BigfilesManager(find_path=str(script),
                                     default_timeout_s=30.0,
                                     popen_factory=window_popen)
        f = m.submit(tmp_path / "huge", days=90, min_mb=1, topn=10, mode="largest")
        assert _wait_instances(), "find Popen 未启动"
        proc = _FakePopen.instances[0]
        task = m._tasks[f.query.key]
        assert task.proc is None, "cancel 未注入到 spawn 窗口"
        assert f.cancel() is True
        gate.set()
        try:
            proc._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pytest.fail("spawn 窗口内 cancel() 后 find 子进程未在 5s 内退出")
        with pytest.raises(concurrent.futures.CancelledError):
            f.result(timeout=2.0)

    def test_cancel_only_own_process(self, tmp_path):
        """两个并发 largest 任务：取消其一，只回收本请求进程，另一任务正常完成。"""
        # 无限输出脚本作 find：两个任务不同 root（不同 key）各起一个进程
        script = _slow_find_script(tmp_path, "slow.sh", rounds=0)
        m = bigfiles.BigfilesManager(find_path=str(script),
                                     default_timeout_s=30.0, cache_ttl_s=0.0,
                                     popen_factory=_explicit_popen_factory("/usr/bin/perl"),
                                     stat_fn=lambda p: _FakeStat(1024 * 1024))
        f1 = m.submit(tmp_path / "a", days=90, min_mb=1, topn=5, mode="largest",
                      timeout=30.0)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(_FakePopen.instances) < 1:
            time.sleep(0.005)
        # f2 用短时间预算：预算到点走 partial 路径自行完成，无需等 30s
        f2 = m.submit(tmp_path / "b", days=90, min_mb=1, topn=5, mode="largest",
                      timeout=0.5)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(_FakePopen.instances) < 2:
            time.sleep(0.005)
        assert len(_FakePopen.instances) == 2, "两个 find 进程未并发启动"
        proc_a, proc_b = _FakePopen.instances[0], _FakePopen.instances[1]
        assert str(script) in " ".join(proc_a.args), "实例 0 应以注入脚本执行"
        assert str(script) in " ".join(proc_b.args), "实例 1 应以注入脚本执行"
        assert f1.cancel() is True
        with pytest.raises(concurrent.futures.CancelledError):
            f1.result(timeout=5.0)
        r2 = f2.result(timeout=10.0)  # f2 不受影响，按自身预算完成
        assert r2.state in {bigfiles.BigfilesState.OK,
                            bigfiles.BigfilesState.NO_MATCH,
                            bigfiles.BigfilesState.TRUNCATED}
        # 被 cancel 的任务进程已终止（SIGTERM）；f2 走自身时间预算路径，
        # 其进程由预算回收（SIGTERM）——关键合同是 f1 的取消没有波及 f2
        # 的执行与结果，r2 正常返回即证明两个任务的进程组互不干扰。
        assert proc_a.poll() is not None, "被取消任务的 find 进程未被回收"
        assert proc_a.returncode != 0, "被取消任务的 find 进程应被信号终止"
        assert proc_b.poll() is not None, "f2 的 find 进程应已按预算回收"
        assert r2.incomplete is True and r2.state == bigfiles.BigfilesState.TRUNCATED, \
            "f2 应按自身时间预算完成（partial 披露），而非被 f1 的取消波及"


# ---------- API 端点（范围与兼容） ----------

class TestScopedApiEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import fathom.api as api_mod
        root = tmp_path / "scanroot"
        root.mkdir()
        monkeypatch.setattr(config, "DEFAULT_ROOT", root)
        monkeypatch.setattr(api_mod, "_BIGFILES_MANAGER", None)
        with TestClient(api_mod.app,
                        base_url=f"http://127.0.0.1:{config.PORT}") as c:
            yield c, api_mod, root

    def test_api_largest_with_subdir_scope(self, client, tmp_path):
        c, api_mod, root = client
        _make_file(root / "sub" / "big.bin", 3 * 1024 * 1024)
        _make_file(root / "sibling.bin", 9 * 1024 * 1024)
        r = c.get("/api/bigfiles", params={"mode": "largest", "path": str(root / "sub"),
                                           "min_mb": 1, "topn": 10})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "ok"
        assert body["scope"]["mode"] == "largest"
        assert body["scope"]["requested_path"] == str(root / "sub")
        assert body["scope"]["resolved_root"].endswith("/sub")
        names = {Path(f["path"]).name for f in body["files"]}
        assert names == {"big.bin"}
        assert body["incomplete"] is False
        assert body["stats"]["started_at"] > 0
        assert body["stats"]["finished_at"] >= body["stats"]["started_at"]

    def test_api_out_of_scope_paths_rejected(self, client, tmp_path):
        c, api_mod, root = client
        (root / "sub").mkdir()
        (tmp_path / "outside").mkdir()
        (root / "link").symlink_to(tmp_path / "outside")
        (tmp_path / f"{root.name}-evil").mkdir()
        cases = [
            str(root / ".." / "outside"),   # .. 越界
            str(tmp_path / f"{root.name}-evil"),  # 相似前缀根
            str(root / "link"),             # symlink 出根
            "relative/child",               # 相对路径
            str(tmp_path / "elsewhere"),    # 任意其他盘路径
        ]
        for raw in cases:
            r = c.get("/api/bigfiles", params={"mode": "largest", "path": raw})
            assert r.status_code == 400, (raw, r.status_code, r.text)
            assert "参数" in r.json()["detail"], raw

    def test_api_moved_path_404(self, client, tmp_path):
        c, api_mod, root = client
        gone = root / "gone"
        gone.mkdir()
        import shutil
        shutil.rmtree(gone)
        r = c.get("/api/bigfiles", params={"mode": "largest", "path": str(gone)})
        assert r.status_code == 404, r.text
        assert "移动" in r.json()["detail"] or "不存在" in r.json()["detail"]

    def test_api_bad_mode_400(self, client):
        c, api_mod, root = client
        r = c.get("/api/bigfiles", params={"mode": "bogus"})
        assert r.status_code == 400, r.text
        assert "参数" in r.json()["detail"]

    def test_api_backward_compatible_defaults(self, client):
        """无新参数的旧 GET：mode=recent、root=监控根、旧字段齐全。"""
        c, api_mod, root = client
        r = c.get("/api/bigfiles", params={"min_mb": 1, "topn": 5})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["scope"]["mode"] == "recent"
        assert body["scope"]["root"] == str(root)
        assert body["scope"]["resolved_root"] == str(root.resolve())
        assert body["scope"]["requested_path"] is None
        for legacy in ("state", "files", "scope", "stats", "truncated",
                       "expired", "cached", "cache_age_s", "error_message",
                       "raw_truncated"):
            assert legacy in body, legacy


# ---------- CLI ----------

class TestScopedCli:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        """与 tests/test_cli_report.py 同源：环境变量 + config 兼容常量 +
        重建 ``config._ACTIVE``，保证 cli.main 内部 configure() 不把路径
        覆盖回生产默认。"""
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        scan_root = tmp_path / "scanroot"
        scan_root.mkdir()
        monkeypatch.delenv("FATHOM_DB", raising=False)
        monkeypatch.setenv("FATHOM_RUNTIME_DIR", str(runtime))
        monkeypatch.setenv("FATHOM_SCAN_ROOT", str(scan_root))
        monkeypatch.setattr(config, "DEFAULT_ROOT", scan_root)
        monkeypatch.setattr(config, "_ACTIVE", config.RuntimeConfig.from_env())
        config._publish_compatibility_values(config._ACTIVE)

    @contextlib.contextmanager
    def _capture_io(self):
        out_buf, err_buf = io.StringIO(), io.StringIO()
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out_buf, err_buf
        try:
            yield out_buf, err_buf
        finally:
            sys.stdout, sys.stderr = real_out, real_err

    def test_cli_largest_with_path(self, tmp_path):
        root = tmp_path / "scanroot"
        _make_file(root / "sub" / "big_old.bin", 4 * 1024 * 1024,
                   mtime_age_days=100)
        _make_file(root / "sibling.bin", 9 * 1024 * 1024, mtime_age_days=100)
        argv = ["bigfiles", "--mode", "largest", "--min-mb", "1",
                "--path", str(root / "sub"), "--topn", "10"]
        with self._capture_io() as (out, err):
            rc = cli.main(argv)
        assert rc == 0, err.getvalue()
        text = out.getvalue()
        assert "big_old.bin" in text
        assert "sibling.bin" not in text

    def test_cli_default_mode_still_recent(self, tmp_path):
        """旧 CLI 调用（无 --mode/--path）保持 recent 行为。"""
        root = tmp_path / "scanroot"
        _make_file(root / "old_big.bin", 4 * 1024 * 1024, mtime_age_days=100)
        argv = ["bigfiles", "--min-mb", "1", "--topn", "10"]
        with self._capture_io() as (out, err):
            rc = cli.main(argv)
        assert rc == 0, err.getvalue()
        assert "近" in out.getvalue()

    def test_cli_scope_error_exit_code(self, tmp_path):
        argv = ["bigfiles", "--mode", "largest", "--path", "/nonexistent-xyz"]
        with self._capture_io() as (out, err):
            rc = cli.main(argv)
        assert rc == 2
        assert "监控根" in err.getvalue() or "参数" in err.getvalue()

    def test_cli_largest_empty_hint(self, tmp_path):
        root = tmp_path / "scanroot" / "empty"
        root.mkdir(parents=True)
        argv = ["bigfiles", "--mode", "largest", "--min-mb", "100",
                "--path", str(root)]
        with self._capture_io() as (out, err):
            rc = cli.main(argv)
        assert rc == 0, err.getvalue()
        assert "当前逻辑大小" in out.getvalue()
