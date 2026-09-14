"""ISS-047 瞬时系统错误语义：du stderr 的 EINTR 类瞬时错误不再判为致命无效采集。

生产实证（docs/TASKS.md ISS-047，2026-09-13 12:00）：launchd 定时扫描的
stderr 出现 ``du: .../MessageTemp/...: Interrupted system call``，run_du 按
ISS-018 口径把它计入 other_error_count，classify_collection 以"非权限错误"
整体拒绝，当日快照未写入——定时每日快照因单次瞬时内核失败丢失。

语义合同（本文件钉住）：
- 瞬时类 errno 消息段（与权限类同口径：只认行内最后一个 ": " 之后消息段的
  精确相等）：EINTR ``Interrupted system call``、EAGAIN/EWOULDBLOCK
  ``Resource temporarily unavailable``。
- 单独计数 DuResult.transient_error_count，不与 denied_count 混同。
- 诚实归类：瞬时错误行虽指名出错路径，但 du 输出是累计大小——被中断子树
  之上的祖先累计值同样可能偏低，无法从错误行反推哪些子树未受影响，因此
  瞬时计数非零的采集只归 partial、永不 full，不声称任何子树数据完整。
- 组合语义：瞬时+权限 → partial；瞬时+真实致命错误（非权限非瞬时 errno、
  信号终止、负数大小、缺根/空输出、路径歧义）→ 仍整体拒绝（ISS-018 口径
  不变）；退出码非零且无任何权限/瞬时证据 → 仍拒绝。
- 路径文本含瞬时措辞不得冒充瞬时证据（ISS-018 R2 BLK-1 同口径）。

本文件不触发真实 du 或真实 HOME：stderr 注入在 monkeypatch 的子进程层
（走 run_du 真实解析/分类代码，路径用 tempfile 合成目录满足校验），采集
归类语义用纯构造 DuResult 钉住。
"""

from __future__ import annotations

import subprocess

import pytest

from fathom import config, db, scanner


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """数据库指到临时目录，避免污染真实 data/fathom.db。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")


def _inject_du_process(monkeypatch, *, returncode=0, stdout="", stderr=""):
    """在子进程层注入 du 结果（不执行真实 du，走 run_du 真实分类路径）。"""
    fake = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: fake)


@pytest.fixture
def injectable_tree(tmp_path):
    """只 mkdir 的合成小树：给注入 stdout 提供可通过目录校验的路径。"""
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "sub").mkdir()
    return tmp_path


def _ok_stdout(root) -> str:
    """根记录存在、大小非负、路径都在扫描根内的干净 stdout。"""
    return f"64\t{root}/big\n32\t{root}/big/sub\n8192\t{root}\n"


def _du_result(*, sizes, exit_code=0, denied=0, transient=0, other=0, path_error=0):
    """纯构造 DuResult：钉 classify_collection 的组合归类语义。"""
    return scanner.DuResult(
        sizes=sizes, exit_code=exit_code, denied_count=denied,
        transient_error_count=transient, other_error_count=other,
        path_error_count=path_error,
        elapsed_seconds=0.01, stderr_tail=())


class TestTransientCounterexample:
    """反例先红后绿：仅含瞬时错误行的采集不再整体拒绝。"""

    def test_production_eintr_shape_no_longer_rejects_collection(
            self, injectable_tree, monkeypatch):
        """对齐生产实证形态：exit=1、根记录存在、stderr 两条 EINTR 行——
        修复前 classify_collection 以"非权限错误"整体拒绝（当日快照丢失），
        修复后按 partial 接受，瞬时行单独计数。"""
        root = injectable_tree
        stderr = (
            f"du: {root}/Mail/MessageTemp/1/E.eml: Interrupted system call\n"
            f"du: {root}/Mail/MessageTemp/2/E.eml: Interrupted system call\n"
        )
        _inject_du_process(
            monkeypatch, returncode=1, stdout=_ok_stdout(root), stderr=stderr)

        result = scanner.run_du(root)

        assert result.transient_error_count == 2   # 瞬时行单独计数
        assert result.denied_count == 0            # 不与权限计数混同
        assert result.other_error_count == 0       # 不再计为非权限错误
        assert result.transient_error_sample.startswith(f"du: {root}/Mail")
        assert scanner.classify_collection(result, str(root)) == "partial"

    def test_eintr_collection_still_writes_daily_snapshot(
            self, injectable_tree, monkeypatch):
        """目标行为：定时每日快照不因单次瞬时错误丢失——瞬时采集按
        partial 落库，denied_count 不混入瞬时计数。"""
        root = injectable_tree
        monkeypatch.setattr(scanner, "run_du", lambda _root: scanner.DuResult(
            sizes={str(root): 8192, f"{root}/big": 64},
            exit_code=1, denied_count=0,
            transient_error_count=2,
            transient_error_sample=f"du: {root}/Mail: Interrupted system call",
            elapsed_seconds=0.01,
            stderr_tail=(f"du: {root}/Mail: Interrupted system call",)))
        conn = db.connect()
        try:
            sid = scanner.create_snapshot(conn, root, min_kb=1024)
            row = conn.execute(
                "SELECT collection_status, denied_count FROM snapshots WHERE id=?",
                (sid,)).fetchone()
            assert row["collection_status"] == "partial"
            assert row["denied_count"] == 0   # 瞬时缺口不冒充权限缺口
        finally:
            conn.close()


class TestTransientMessageSet:
    """run_du 对 stderr 全量逐行三分类：权限 / 瞬时 / 其他（真实致命）。"""

    @pytest.mark.parametrize("message", [
        "Interrupted system call",            # EINTR（生产实证）
        "Resource temporarily unavailable",   # EAGAIN/EWOULDBLOCK
    ])
    def test_transient_messages_counted_separately(
            self, injectable_tree, monkeypatch, message):
        root = injectable_tree
        stderr = f"du: {root}/a: {message}\ndu: {root}/b: {message}\n"
        _inject_du_process(
            monkeypatch, returncode=1, stdout=_ok_stdout(root), stderr=stderr)

        result = scanner.run_du(root)

        assert result.transient_error_count == 2
        assert result.denied_count == 0
        assert result.other_error_count == 0
        assert result.transient_error_sample == f"du: {root}/a: {message}"
        assert scanner.classify_collection(result, str(root)) == "partial"

    def test_lookalike_message_is_not_transient(
            self, injectable_tree, monkeypatch):
        """精确相等口径：措辞相近但不相等的消息段保守计为非权限错误。"""
        root = injectable_tree
        stderr = f"du: {root}/a: Interrupted system call while reading\n"
        _inject_du_process(
            monkeypatch, returncode=1, stdout=_ok_stdout(root), stderr=stderr)

        result = scanner.run_du(root)

        assert result.transient_error_count == 0
        assert result.other_error_count == 1
        with pytest.raises(scanner.InvalidScanError, match="非权限错误"):
            scanner.classify_collection(result, str(root))

    @pytest.mark.parametrize("message", [
        "No such file or directory",
        "Input/output error",
    ])
    def test_unknown_errno_still_fatal(self, injectable_tree, monkeypatch, message):
        root = injectable_tree
        stderr = f"du: {root}/a: {message}\n"
        _inject_du_process(
            monkeypatch, returncode=1, stdout=_ok_stdout(root), stderr=stderr)

        result = scanner.run_du(root)

        assert result.transient_error_count == 0
        assert result.other_error_count == 1
        with pytest.raises(scanner.InvalidScanError):
            scanner.classify_collection(result, str(root))


class TestHonestPartialSemantics:
    """诚实归类：瞬时计数非零只归 partial，永不 full，不声称子树完整。"""

    def test_transient_only_is_partial(self):
        r = _du_result(sizes={"/r": 10}, exit_code=1, transient=2)
        assert scanner.classify_collection(r, "/r") == "partial"

    def test_transient_with_zero_exit_still_partial(self):
        """即使退出码为 0（合成形态），有瞬时错误证据就不声称完整采集。"""
        r = _du_result(sizes={"/r": 10}, exit_code=0, transient=1)
        assert scanner.classify_collection(r, "/r") == "partial"

    def test_transient_and_permission_coexist_partial(self):
        """瞬时+权限并存：两类都可解释地受限，仍按 partial 接受。"""
        r = _du_result(sizes={"/r": 10}, exit_code=1, denied=2, transient=1)
        assert scanner.classify_collection(r, "/r") == "partial"

    def test_clean_collection_is_still_full(self):
        """无任何错误证据的干净采集仍归 full，不被瞬时语义误伤。"""
        r = _du_result(sizes={"/r": 10})
        assert scanner.classify_collection(r, "/r") == "full"

    def test_mixed_stderr_counts_not_conflated(
            self, injectable_tree, monkeypatch):
        """权限/瞬时/真实致命三类并存：各计各数，整体仍拒绝。"""
        root = injectable_tree
        stderr = (
            f"du: {root}/hidden: Permission denied\n"
            f"du: {root}/a: Interrupted system call\n"
            f"du: {root}/b: Resource temporarily unavailable\n"
            f"du: {root}/gone: No such file or directory\n"
        )
        _inject_du_process(
            monkeypatch, returncode=1, stdout=_ok_stdout(root), stderr=stderr)

        result = scanner.run_du(root)

        assert result.denied_count == 1
        assert result.transient_error_count == 2
        assert result.other_error_count == 1
        with pytest.raises(scanner.InvalidScanError, match="非权限错误"):
            scanner.classify_collection(result, str(root))


class TestFatalStillRejectedWithTransient:
    """不放松 ISS-018 致命分类：瞬时证据不能豁免任何既有拒绝分支。"""

    def test_transient_plus_real_fatal_errno_rejected(self):
        """瞬时+真实致命 errno 并存：仍整体拒绝（ISS-018 口径不变）。"""
        r = _du_result(sizes={"/r": 10}, exit_code=1, transient=1, other=1)
        with pytest.raises(scanner.InvalidScanError, match="非权限错误"):
            scanner.classify_collection(r, "/r")

    def test_transient_with_signal_exit_rejected(self):
        r = _du_result(sizes={"/r": 10}, exit_code=-9, transient=2)
        with pytest.raises(scanner.InvalidScanError, match="信号终止"):
            scanner.classify_collection(r, "/r")

    def test_transient_with_negative_size_rejected(self):
        r = _du_result(sizes={"/r": 10, "/r/a": -1}, transient=1)
        with pytest.raises(scanner.InvalidScanError):
            scanner.classify_collection(r, "/r")

    def test_transient_missing_root_rejected(self):
        r = _du_result(sizes={"/r/sub": 5}, exit_code=1, transient=1)
        with pytest.raises(scanner.InvalidScanError, match="未返回根目录记录"):
            scanner.classify_collection(r, "/r")

    def test_transient_empty_output_rejected(self):
        r = _du_result(sizes={}, exit_code=1, transient=1)
        with pytest.raises(scanner.InvalidScanError):
            scanner.classify_collection(r, "/r")

    def test_transient_with_path_error_rejected(self):
        r = _du_result(sizes={"/r": 10}, transient=1, path_error=1)
        with pytest.raises(scanner.InvalidScanError, match="stdout"):
            scanner.classify_collection(r, "/r")

    def test_nonzero_exit_without_any_evidence_still_rejected(self):
        """退出码非零且无任何权限/瞬时证据：无法证明受限，仍拒绝。"""
        r = _du_result(sizes={"/r": 10}, exit_code=1)
        with pytest.raises(scanner.InvalidScanError, match="无权限受限证据"):
            scanner.classify_collection(r, "/r")


class TestPathTextCannotSpoofTransientEvidence:
    """R2 BLK-1 同口径：出错路径文本含瞬时措辞不得冒充瞬时证据。"""

    def test_transient_wording_in_path_with_fatal_errno(
            self, injectable_tree, monkeypatch):
        """目录名恰含 "Interrupted system call"，真实 errno=ENOENT：
        必须计为非权限错误并整体拒绝，不得因措辞冒充而放行。"""
        root = injectable_tree
        line = (
            f"du: {root}/dir: Interrupted system call/child:"
            " No such file or directory\n"
        )
        _inject_du_process(
            monkeypatch, returncode=1, stdout=_ok_stdout(root), stderr=line)

        result = scanner.run_du(root)

        assert result.transient_error_count == 0
        assert result.other_error_count == 1
        with pytest.raises(scanner.InvalidScanError):
            scanner.classify_collection(result, str(root))

    def test_real_eintr_with_wording_in_path_still_transient(
            self, injectable_tree, monkeypatch):
        """反向安全网：真实 EINTR 行即使路径文本也含瞬时措辞，消息段精确
        匹配仍计为瞬时 → partial。"""
        root = injectable_tree
        line = (
            f"du: {root}/x: Interrupted system call/child:"
            " Interrupted system call\n"
        )
        _inject_du_process(
            monkeypatch, returncode=1, stdout=_ok_stdout(root), stderr=line)

        result = scanner.run_du(root)

        assert result.transient_error_count == 1
        assert result.other_error_count == 0
        assert scanner.classify_collection(result, str(root)) == "partial"
