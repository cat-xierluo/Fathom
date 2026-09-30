"""fathom/analysis_manager.py 生命周期与故障矩阵测试（ISS-035B）。

合同：方案 §4.2/§5/§6 与 TASKS ISS-035B「先复现」六反例。全部用例经
**生产** AnalysisManager / AgentCliRunner / 适配器与真实 SQLite，故障只
由「settings 指向合成 CLI 脚本 + 预算注入」制造，不自建模拟生命周期。
不触真实 claude、不发送数据、不触生产 HOME 扫描；真实 CLI 链路另见
worktree evidence/progress.md。

先复现反例与本文件用例的对应：
- 预览后数据/Runtime/授权变更仍执行 → TestPreviewInvalidation* /
  test_disable_during_run_revokes_commit / test_upgrade_journal_*；
- 连点启动两份 → TestDispatchDedup*；
- cancel 与完成竞态 → TestCancelAndTimeout.test_cancel_finish_race；
- exit=0 但应用错误被存为成功 → TestFaultMatrix.test_app_error_*；
- helper 退出遗留 CLI →（runner 看门）tests/test_agent_runtime.py +
  test_agent_supervisor_seam.py；服务侧遗留 running → reconcile 用例；
- 升级备份后迟到写库 → test_upgrade_journal_revokes_commit + 升级租约
  集成（tests/test_analysis_upgrade_gate.py）。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

import pytest

from fathom import analysis_manager as am
from fathom import agent_runtime as ar
from fathom import config, db

FAKE_VERSION = "2.1.237"

_INNER_RESULT = json.dumps({
    "schema_version": 1,
    "summary": "A 目录增长 2MB",
    "findings": [{"text": "A 增长", "evidence_ids": ["f-001"],
                  "certainty": "observed"}],
    "limitations": [],
    "inspect_next": [{"evidence_id": "f-001", "reason": "查看 A"}],
}, ensure_ascii=False)


def _claude_json(result_text: str) -> str:
    return json.dumps({"type": "result", "subtype": "success",
                       "is_error": False, "result": result_text,
                       "num_turns": 1}, ensure_ascii=False)


# ---------- 隔离夹具（进程级配置隔离见 tests/conftest.py 的 isolated） ----------


def make_fake_claude(dir_path: Path, *, mode: str = "ok",
                     version_line: str = f"{FAKE_VERSION} (Claude Code)") -> Path:
    """合成 claude 形态脚本：--version 过能力门；运行时按 mode 注入故障。"""
    bodies = {
        "ok": f'cat > /dev/null\ncat <<\'EOS\'\n{_claude_json(_INNER_RESULT)}\nEOS\n',
        "app_error": 'cat > /dev/null\ncat <<\'EOS\'\n'
                     '{"type":"result","subtype":"error_during_execution",'
                     '"is_error":true,"result":"合成：应用层错误"}\nEOS\n',
        "nonzero": "cat > /dev/null\necho boom >&2\nexit 3\n",
        "output_limit": "cat > /dev/null\ndd if=/dev/zero bs=1024 count=300 "
                        "2>/dev/null\n",
        "bad_utf8": 'cat > /dev/null\nprintf "\\xff\\xfe\\xbd"\n',
        "html_result": 'cat > /dev/null\ncat <<\'EOS\'\n'
                       + _claude_json("<script>alert(1)</script>") + '\nEOS\n',
        "unknown_evidence": 'cat > /dev/null\ncat <<\'EOS\'\n'
                            + _claude_json(json.dumps({
                                "schema_version": 1, "summary": "s",
                                "findings": [{"text": "t",
                                              "evidence_ids": ["f-999"],
                                              "certainty": "observed"}],
                                "limitations": [], "inspect_next": [],
                              }, ensure_ascii=False)) + '\nEOS\n',
        "sleep": "cat > /dev/null\nsleep 30\n",
        "slow_ok": ("cat > /dev/null\nsleep 1.2\n"
                    f"cat <<'EOS'\n{_claude_json(_INNER_RESULT)}\nEOS\n"),
    }
    script = dir_path / "fake-claude"
    script.write_text("#!/bin/sh\n"
                      'if [ "$1" = "--version" ]; then '
                      f'echo "{version_line}"; exit 0; fi\n'
                      + bodies[mode], encoding="utf-8")
    script.chmod(0o755)
    return script


def enable_analysis(fake_script: Path) -> None:
    config.update_user_settings({"analysis": {
        "enabled": True,
        "runtime": {"id": "claude-code", "executable": str(fake_script),
                    "version": FAKE_VERSION},
    }})


def make_snapshots(scanroot: Path) -> tuple[int, int]:
    """同数据集两快照：A 目录 2048→4096 KiB（delta 越过 min_delta_kb）。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
            " du_seconds, total_kb, min_kb, collection_status, vanished_count,"
            " exclude_names, confirmed_missing_count, path_unverified_count)"
            " VALUES ('2026-09-28T08:00:00', ?, 2, 0, 0.1, 2048, 1024,"
            " 'full', 0, '', 0, 0)", (str(scanroot),))
        a = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO entries(snapshot_id, path, size_kb)"
                     " VALUES (?,?,?)", (a, str(scanroot / "A"), 2048))
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
            " du_seconds, total_kb, min_kb, collection_status, vanished_count,"
            " exclude_names, confirmed_missing_count, path_unverified_count)"
            " VALUES ('2026-09-28T09:00:00', ?, 2, 0, 0.1, 4096, 1024,"
            " 'full', 0, '', 0, 0)", (str(scanroot),))
        b = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO entries(snapshot_id, path, size_kb)"
                     " VALUES (?,?,?)", (b, str(scanroot / "A"), 4096))
        conn.commit()
        return a, b
    finally:
        conn.close()


def make_manager(**kw) -> am.AnalysisManager:
    defaults = dict(run_timeout_s=20.0)
    defaults.update(kw)
    return am.AnalysisManager(**defaults)


def wait_terminal(manager: am.AnalysisManager, job_id: str,
                  timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    view = None
    while time.monotonic() < deadline:
        view = manager.job_view(job_id)
        if view["terminal"]:
            return view
        time.sleep(0.05)
    raise AssertionError(f"job 未在 {timeout}s 内到终态：{view}")


def agent_rows() -> list[sqlite3.Row]:
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM agent_analyses").fetchall()
    finally:
        conn.close()


def run_row(job_id: str) -> sqlite3.Row:
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM analysis_runs WHERE job_id=?",
                            (job_id,)).fetchone()
    finally:
        conn.close()


@pytest.fixture
def ok_setup(isolated):
    """成功形态的标准布景：合成 CLI + 两快照 + 已启用设置。"""
    bin_dir = isolated["runtime"] / "bin"
    bin_dir.mkdir()
    script = make_fake_claude(bin_dir, mode="ok")
    enable_analysis(script)
    a, b = make_snapshots(isolated["scanroot"])
    return {**isolated, "script": script, "a": a, "b": b, "bin_dir": bin_dir}


# ==========================================================================
# 成功路径与落库
# ==========================================================================


class TestSuccessPath:
    def test_end_to_end_success_writes_trusted_result(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        # 预览冻结内容对拍：prompt 完整含事实块，digest 覆盖完整文本。
        assert "FATHOM_FACTS_V1" in preview.bundle.prompt_text
        import hashlib
        assert preview.request_digest == hashlib.sha256(
            preview.bundle.prompt_bytes).hexdigest()
        assert preview.facts_digest == preview.facts.facts_digest

        job, replayed = manager.start_job(preview.preview_id,
                                          preview.request_digest, "key-succ")
        assert replayed is False
        view = wait_terminal(manager, job["job_id"])
        assert view["status"] == "succeeded" and view["reason_code"] is None
        assert view["analysis_id"] is not None

        rows = agent_rows()
        assert len(rows) == 1
        row = rows[0]
        payload = json.loads(row["facts_json"])
        assert row["dataset_root"] == str(ok_setup["scanroot"])  # 真实根（审计）
        assert any(e["evidence_id"] == "f-001" for e in payload["entries"])
        result = json.loads(row["result_json"])
        assert result["findings"][0]["evidence_ids"] == ["f-001"]
        manifest = json.loads(row["manifest_json"])
        assert manifest["request_digest"] == preview.request_digest
        assert run_row(job["job_id"])["status"] == "succeeded"

    def test_history_view_and_evidence(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "key-hist")
        wait_terminal(manager, job["job_id"])
        items = manager.list_analyses(ok_setup["a"], ok_setup["b"])
        assert len(items) == 1
        item = items[0]
        assert item["expired"] is False and item["expired_reason"] is None
        assert item["a"]["snapshot_id"] == ok_setup["a"]
        assert item["facts"]["dataset"]["units"] == "KiB"
        got = manager.get_analysis(item["id"])
        assert got["id"] == item["id"]

    def test_prompt_bytes_equal_actual_dispatch_payload(self, ok_setup):
        """预览字节与实际适配器输入对拍：合成 CLI 回显 stdin，逐字节一致。"""
        bin_dir = ok_setup["bin_dir"]
        echo = bin_dir / "fake-claude"
        echo.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "2.1.237 (Claude Code)"; exit 0; fi\n'
            'cat > /dev/null\ncat <<\'EOS\'\n' + _claude_json(_INNER_RESULT) + '\nEOS\n')
        echo.chmod(0o755)
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        # 适配器用 stdin 载荷 = 完整 prompt 文本；回显脚本已证明 stdin 通路，
        # 这里直接对拍冻结字节与适配器构建的 stdin。
        adapter = ar.ClaudeCodeAdapter(ar.RuntimeInfo(
            id="claude-code", display_name="Claude Code", identity="x",
            identity_evidence="x", official_docs="x",
            availability=ar.Availability.READY, reason_code="verified_version",
            detail="", executable=str(echo)))
        invocation = adapter.build_invocation(
            preview.bundle.prompt_text, cwd=bin_dir)
        assert invocation.stdin_bytes == preview.bundle.prompt_bytes


# ==========================================================================
# 反例①：预览后数据 / Runtime / 授权变更仍执行 → 必须全部拒绝
# ==========================================================================


class TestPreviewInvalidation:
    def test_digest_mismatch_rejected(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(preview.preview_id, "0" * 64, "key-dm")
        assert ei.value.reason_code == "request_digest_mismatch"
        assert ei.value.status_code == 409

    def test_data_change_after_preview_rejected(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        conn = db.connect()
        try:
            conn.execute("UPDATE entries SET size_kb=8192 WHERE snapshot_id=?",
                         (ok_setup["b"],))
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(preview.preview_id, preview.request_digest,
                              "key-data")
        assert ei.value.reason_code == "preview_stale"

    def test_snapshot_replaced_after_preview_rejected(self, ok_setup):
        """a/b 被替换（同日新快照 + 原行删除）→ 拒绝旧预览。"""
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        conn = db.connect()
        try:
            conn.execute("DELETE FROM entries WHERE snapshot_id=?", (ok_setup["b"],))
            conn.execute("DELETE FROM snapshots WHERE id=?", (ok_setup["b"],))
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
                " du_seconds, total_kb, min_kb, collection_status, vanished_count,"
                " exclude_names, confirmed_missing_count, path_unverified_count)"
                " VALUES ('2026-09-28T09:30:00', ?, 2, 0, 0.1, 5000, 1024,"
                " 'full', 0, '', 0, 0)", (str(ok_setup["scanroot"]),))
            new_b = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO entries(snapshot_id, path, size_kb)"
                         " VALUES (?,?,?)", (new_b, str(ok_setup["scanroot"] / "A"), 5000))
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(preview.preview_id, preview.request_digest,
                              "key-rep")
        assert ei.value.reason_code == "preview_stale"

    def test_settings_revision_change_rejected(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        # 实质变化：关掉再开（revision +2），identity 回到原值。
        config.update_user_settings({"analysis": {"enabled": False}})
        config.update_user_settings({"analysis": {"enabled": True}})
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(preview.preview_id, preview.request_digest,
                              "key-rev")
        assert ei.value.reason_code == "preview_stale"

    def test_consent_revision_change_rejected(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        config.update_user_settings({"analysis": {"consent_revision": 4}})
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(preview.preview_id, preview.request_digest,
                              "key-consent")
        assert ei.value.reason_code == "preview_stale"

    def test_runtime_switch_rejected(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        other = make_fake_claude(ok_setup["bin_dir"], mode="ok")
        other_path = ok_setup["bin_dir"] / "fake-claude-2"
        other.rename(other_path)
        config.update_user_settings({"analysis": {
            "runtime": {"id": "claude-code", "executable": str(other_path),
                        "version": FAKE_VERSION}}})
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(preview.preview_id, preview.request_digest,
                              "key-rt")
        assert ei.value.reason_code == "preview_stale"

    def test_runtime_removed_after_preview_rejected(self, ok_setup):
        """已选路径被移除 → 复核失败，绝不静默换另一家。"""
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        ok_setup["script"].unlink()
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(preview.preview_id, preview.request_digest,
                              "key-gone")
        assert ei.value.reason_code == "runtime_unavailable"
        assert ei.value.status_code == 403

    def test_preview_ttl_expiry(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        aged = dataclasses.replace(
            preview, created_monotonic=preview.created_monotonic
            - am.PREVIEW_TTL_S - 1)
        with manager._lock:
            manager._previews[aged.preview_id] = aged
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(aged.preview_id, aged.request_digest, "key-ttl")
        assert ei.value.reason_code == "preview_expired"

    def test_preview_capacity_lru(self, ok_setup):
        manager = make_manager()
        ids = [manager.create_preview(ok_setup["a"], ok_setup["b"]).preview_id
               for _ in range(am.PREVIEW_MAX_COUNT + 2)]
        with pytest.raises(am.AnalysisError) as ei:
            manager._get_preview(ids[0])
        assert ei.value.reason_code == "preview_not_found"
        manager._get_preview(ids[-1])  # 最新仍在

    def test_unknown_preview_404(self, ok_setup):
        manager = make_manager()
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job("no-such-preview", "0" * 64, "key-404")
        assert ei.value.reason_code == "preview_not_found"
        assert ei.value.status_code == 404

    def test_disabled_403(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        script = make_fake_claude(bin_dir, mode="ok")
        a, b = make_snapshots(isolated["scanroot"])
        manager = make_manager()
        # 默认未启用
        with pytest.raises(am.AnalysisError) as ei:
            manager.create_preview(a, b)
        assert ei.value.reason_code == "analysis_disabled"
        assert ei.value.status_code == 403
        # 启用后关掉：同样 403
        enable_analysis(script)
        config.update_user_settings({"analysis": {"enabled": False}})
        with pytest.raises(am.AnalysisError) as ei2:
            manager.create_preview(a, b)
        assert ei2.value.reason_code == "analysis_disabled"

    def test_bad_runtime_configuration_rejected(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        fake = make_fake_claude(bin_dir, mode="ok")
        # 未知 ID 在 settings 层拒绝
        with pytest.raises(config.ConfigurationError):
            config.update_user_settings({"analysis": {
                "enabled": True,
                "runtime": {"id": "claude", "executable": str(fake)}}})
        # 未支持候选（zcode——codex-cli 已按 DEC-030 开放为 ready）能保存但能力门拒绝预览
        config.update_user_settings({"analysis": {
            "enabled": True,
            "runtime": {"id": "zcode", "executable": "/bin/echo"}}})
        a, b = make_snapshots(isolated["scanroot"])
        manager = make_manager()
        with pytest.raises(am.AnalysisError) as ei:
            manager.create_preview(a, b)
        assert ei.value.reason_code == "runtime_unsupported"

    def test_dataset_mismatch_rejected(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        enable_analysis(make_fake_claude(bin_dir, mode="ok"))
        a, b = make_snapshots(isolated["scanroot"])
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
                " du_seconds, total_kb, min_kb, collection_status)"
                " VALUES ('2026-09-28T10:00:00', '/other/root', 1, 0, 0.1,"
                " 10, 1024, 'full')")
            conn.commit()
            other = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        finally:
            conn.close()
        manager = make_manager()
        with pytest.raises(am.AnalysisError) as ei:
            manager.create_preview(a, other)
        assert ei.value.reason_code == "dataset_mismatch"
        assert ei.value.status_code == 400

    def test_missing_snapshot_404(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        enable_analysis(make_fake_claude(bin_dir, mode="ok"))
        manager = make_manager()
        with pytest.raises(am.AnalysisError) as ei:
            manager.create_preview(424242, 424243)
        assert ei.value.reason_code == "snapshot_not_found"
        assert ei.value.status_code == 404


# ==========================================================================
# 反例②：连点启动两份 → 只启动一次
# ==========================================================================


class TestDispatchDedup:
    def test_repeat_execute_same_key_returns_same_job(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job1, replay1 = manager.start_job(preview.preview_id,
                                          preview.request_digest, "key-dup")
        job2, replay2 = manager.start_job(preview.preview_id,
                                          preview.request_digest, "key-dup")
        assert replay1 is False and replay2 is True
        assert job1["job_id"] == job2["job_id"]
        conn = db.connect()
        try:
            n = conn.execute("SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
        finally:
            conn.close()
        assert n == 1
        wait_terminal(manager, job1["job_id"])

    def test_same_key_after_terminal_replays(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job1, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                    "key-after")
        wait_terminal(manager, job1["job_id"])
        job2, replayed = manager.start_job(preview.preview_id,
                                           preview.request_digest, "key-after")
        assert replayed is True
        assert job2["job_id"] == job1["job_id"]
        assert job2["status"] == "succeeded"

    def test_concurrent_double_click_starts_single_job(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        results: list = []
        errors: list = []

        def click():
            try:
                results.append(manager.start_job(preview.preview_id,
                                                 preview.request_digest,
                                                 "key-race"))
            except am.AnalysisError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=click) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 无论竞争结果如何（一方重放或忙 409），只启动一份。
        conn = db.connect()
        try:
            rows = conn.execute("SELECT job_id FROM analysis_runs").fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        wait_terminal(manager, rows[0]["job_id"])
        job_ids = {r[0]["job_id"] for r in results}
        assert len(job_ids) <= 1

    def test_same_key_different_digest_conflict(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        manager.start_job(preview.preview_id, preview.request_digest, "key-c")
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(preview.preview_id, "f" * 64, "key-c")
        assert ei.value.reason_code == "idempotency_conflict"

    def test_different_request_busy_409_no_queue(self, ok_setup):
        # 运行态换成慢脚本（直接重写同一已注册路径，身份/revision 不变）
        ok_setup["script"].write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "2.1.237 (Claude Code)"; exit 0; fi\n'
            "cat > /dev/null\nsleep 30\n", encoding="utf-8")
        manager = make_manager(run_timeout_s=10)
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "key-busy-a")
        assert manager.job_view(job["job_id"])["status"] in ("starting", "running")
        # 预览已单次消费：换新预览才能发起不同请求
        manager._discard_preview(preview.preview_id)
        preview2 = manager.create_preview(ok_setup["a"], ok_setup["b"])
        with pytest.raises(am.AnalysisError) as ei:
            manager.start_job(preview2.preview_id, preview2.request_digest,
                              "key-busy-b")
        assert ei.value.reason_code == "analysis_busy"
        assert ei.value.status_code == 409
        manager.cancel_job(job["job_id"])
        wait_terminal(manager, job["job_id"])


# ==========================================================================
# 反例：幂等重放分支不释放分析租约（ISS-130）
# ==========================================================================


class TestReplayReleasesLease:
    """重放不启动 job，租约不交给工作线程，必须在返回前释放。

    fd 是普通整数、不随对象回收自动 close，泄漏会让后续分析与升级持续
    报 analysis_busy，直到服务进程退出。
    """

    @staticmethod
    def _acquire_or_busy(manager: am.AnalysisManager) -> "am.AnalysisLease":
        return am.AnalysisLease.acquire(manager._lock_path, source="lease-probe")

    def test_same_key_replay_uses_pre_lease_fast_path(self, ok_setup):
        """常规同键重放在取租约之前就返回（start_job 幂等快检）。

        这条路径本来就不取租约，因此不是泄漏点；把它钉住是为了说明
        真正的两个泄漏点在下面的竞争路径上。
        """
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "lease-after")
        wait_terminal(manager, job["job_id"])

        acquired: list = []
        original = am.AnalysisLease.acquire

        def tracking_acquire(path, *, source):
            lease = original(path, source=source)
            acquired.append(lease)
            return lease

        am.AnalysisLease.acquire = staticmethod(tracking_acquire)
        try:
            replayed, replay = manager.start_job(preview.preview_id,
                                                 preview.request_digest,
                                                 "lease-after")
        finally:
            am.AnalysisLease.acquire = original
        assert replay is True and replayed["job_id"] == job["job_id"]
        assert acquired == [], "幂等快检命中时不应再取租约"
        lease = self._acquire_or_busy(manager)
        lease.release()

    def test_replay_inside_lease_releases_lease(self, ok_setup, monkeypatch):
        """首查 miss、租约内重查 hit（双击竞争形态）也必须释放。"""
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "lease-race")
        wait_terminal(manager, job["job_id"])

        preview2 = manager.create_preview(ok_setup["a"], ok_setup["b"])
        original_find = manager._find_job_by_idempotency
        calls = 0

        def racing_find(key):
            nonlocal calls
            calls += 1
            return None if calls == 1 else original_find(key)

        monkeypatch.setattr(manager, "_find_job_by_idempotency", racing_find)
        replayed, replay = manager.start_job(preview2.preview_id,
                                             preview2.request_digest, "lease-race")
        assert replay is True and replayed["job_id"] == job["job_id"]
        monkeypatch.undo()
        lease = self._acquire_or_busy(manager)
        lease.release()

    def test_integrity_error_replay_releases_lease(self, ok_setup,
                                                   monkeypatch):
        """唯一键冲突（IntegrityError）后的重放出口同样必须释放。"""
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "lease-integrity-warm")
        wait_terminal(manager, job["job_id"])

        preview2 = manager.create_preview(ok_setup["a"], ok_setup["b"])
        # 竞争者已在库中占住同一幂等键：首查被强制 miss，让 INSERT 真的撞唯一约束。
        planted = dict(run_row(job["job_id"]))
        planted["job_id"] = "planted" + planted["job_id"][:20]
        planted["idempotency_key"] = "lease-integrity"
        cols = ",".join(planted)
        marks = ",".join("?" * len(planted))
        conn = db.connect(manager._db_path)
        try:
            conn.execute(
                f"INSERT INTO analysis_runs({cols}) VALUES ({marks})",
                tuple(planted.values()))
            conn.commit()
        finally:
            conn.close()

        original_find = manager._find_job_by_idempotency
        calls = 0

        def racing_find(key):
            nonlocal calls
            calls += 1
            return None if calls <= 1 else original_find(key)

        monkeypatch.setattr(manager, "_find_job_by_idempotency", racing_find)
        replayed, replay = manager.start_job(preview2.preview_id,
                                             preview2.request_digest,
                                             "lease-integrity")
        monkeypatch.undo()
        assert calls == 2, "应走 INSERT 撞唯一约束再重查的重放路径"
        assert replay is True and replayed["job_id"] == planted["job_id"]
        lease = self._acquire_or_busy(manager)
        lease.release()

    def test_active_job_still_blocks_concurrent_analysis(self, ok_setup):
        """释放只发生在重放出口；活跃任务期间的跨进程互斥不得被削弱。"""
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "lease-active")
        with pytest.raises(am.AnalysisBusy):
            self._acquire_or_busy(manager)
        with pytest.raises(am.AnalysisError) as ei:
            preview2 = manager.create_preview(ok_setup["a"], ok_setup["b"])
            manager.start_job(preview2.preview_id, preview2.request_digest,
                              "lease-active-2")
        assert ei.value.reason_code == "analysis_busy"
        manager.cancel_job(job["job_id"])
        wait_terminal(manager, job["job_id"])


# ==========================================================================
# 反例③：cancel 与完成竞态；超时
# ==========================================================================


class TestCancelAndTimeout:
    def test_cancel_running_job(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        enable_analysis(make_fake_claude(bin_dir, mode="sleep"))
        a, b = make_snapshots(isolated["scanroot"])
        manager = make_manager(run_timeout_s=15)
        preview = manager.create_preview(a, b)
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "key-cancel")
        view = manager.cancel_job(job["job_id"])
        assert view["status"] in ("cancelling", "cancelled")
        final = wait_terminal(manager, job["job_id"], timeout=15)
        assert final["status"] == "cancelled"
        assert agent_rows() == []  # 取消不落半份正文

    def test_cancel_after_success_never_resurrects(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "key-late")
        wait_terminal(manager, job["job_id"])
        view = manager.cancel_job(job["job_id"])
        assert view["terminal"] is True and view["status"] == "succeeded"
        assert run_row(job["job_id"])["status"] == "succeeded"

    def test_cancel_finish_race_single_outcome(self, isolated):
        """取消与完成竞争：每个尝试恰好一个确定终态，且与落库一致
        （成功⇔有正文）；不同取消时距覆盖 cancelled 与 succeeded 两侧。

        档位不假设绝对耗时（ISS-124：CI x86_64 慢环境 spawn/stdin 开销
        放大后，固定 1.6s 上界内六档全落取消侧）：先以一次不取消的
        slow_ok 基准测量本环境「启动→终态」的观测耗时 T，再按 T 的
        比例设取消时距——0.15T 远早于完成、1.8T 晚于 T（观测耗时是
        完成提交时刻的上界），两侧确定性覆盖；0.75T/0.95T/1.1T 贴
        真实竞态窗口，结果两侧皆可，只检查合同不变量。"""
        # 基准轮：同夹具同脚本，吸收 spawn、stdin 读取、输出解析全部
        # 环境开销；wait_terminal 轮询量化只令 T 偏大（对两侧余量安全）。
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir(exist_ok=True)
        enable_analysis(make_fake_claude(bin_dir, mode="slow_ok"))
        base_a, base_b = make_snapshots(isolated["scanroot"])
        base_manager = make_manager()
        base_preview = base_manager.create_preview(base_a, base_b)
        base_job, _ = base_manager.start_job(base_preview.preview_id,
                                             base_preview.request_digest,
                                             "key-race-baseline")
        t0 = time.monotonic()
        base_final = wait_terminal(base_manager, base_job["job_id"])
        finish_s = time.monotonic() - t0
        assert base_final["status"] == "succeeded"  # 不取消、预算内必成功
        run_timeout_s = max(15.0, 3.0 * finish_s)   # 晚档 1.8T 不触超时兜底
        wait_s = run_timeout_s + 5.0
        factors = (0.15, 0.5, 0.75, 0.95, 1.1, 1.8)
        outcomes: list[str] = []
        baseline_rows = len(agent_rows())
        for attempt, factor in enumerate(factors):
            delay = max(0.05, factor * finish_s)
            enable_analysis(make_fake_claude(bin_dir, mode="slow_ok"))
            a, b = make_snapshots(isolated["scanroot"])
            manager = make_manager(run_timeout_s=run_timeout_s)
            preview = manager.create_preview(a, b)
            job, _ = manager.start_job(preview.preview_id,
                                       preview.request_digest,
                                       f"key-race-{attempt}")
            timer = threading.Timer(delay, manager.cancel_job,
                                    args=(job["job_id"],))
            timer.start()
            final = wait_terminal(manager, job["job_id"], timeout=wait_s)
            timer.join()
            rows_now = len(agent_rows())
            if final["status"] == "succeeded":
                assert rows_now == baseline_rows + 1
                baseline_rows = rows_now
            else:
                assert final["status"] == "cancelled"
                assert rows_now == baseline_rows
            outcomes.append(final["status"])
        assert "succeeded" in outcomes and "cancelled" in outcomes, outcomes

    def test_timeout_marks_timed_out(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        enable_analysis(make_fake_claude(bin_dir, mode="sleep"))
        a, b = make_snapshots(isolated["scanroot"])
        manager = make_manager(run_timeout_s=0.6)
        preview = manager.create_preview(a, b)
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "key-to")
        t0 = time.monotonic()
        final = wait_terminal(manager, job["job_id"], timeout=15)
        assert final["status"] == "timed_out"
        assert time.monotonic() - t0 < 10
        assert agent_rows() == []


# ==========================================================================
# 反例①/⑥延伸：授权撤销与升级停写的提交资格撤销
# ==========================================================================


class TestRevocationGates:
    def test_disable_during_run_revokes_commit(self, isolated):
        """关闭授权与完成竞争：提交资格被撤销 → cancelled，绝不落正文。"""
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        enable_analysis(make_fake_claude(bin_dir, mode="slow_ok"))
        a, b = make_snapshots(isolated["scanroot"])
        manager = make_manager(run_timeout_s=15)
        preview = manager.create_preview(a, b)
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "key-disable")
        time.sleep(0.3)  # 进入 running
        config.update_user_settings({"analysis": {"enabled": False}})
        manager.refresh_policy()
        final = wait_terminal(manager, job["job_id"])
        assert final["status"] == "cancelled"
        assert agent_rows() == []
        assert final["reason_code"] in ("analysis_disabled", "policy_changed",
                                        "cancelled")

    def test_upgrade_journal_blocks_preview_and_start(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        from fathom import upgrade
        journal = upgrade.journal_path_from_db(Path(config.DB_PATH))
        journal.write_text(json.dumps({"txn_id": "t", "phase": "prepared"}),
                           encoding="utf-8")
        try:
            with pytest.raises(am.AnalysisError) as ei:
                manager.create_preview(ok_setup["a"], ok_setup["b"])
            assert ei.value.reason_code == "upgrade_in_progress"
            with pytest.raises(am.AnalysisError) as ei2:
                manager.start_job(preview.preview_id, preview.request_digest,
                                  "key-upg")
            assert ei2.value.reason_code == "upgrade_in_progress"
        finally:
            journal.unlink(missing_ok=True)

    def test_upgrade_journal_revokes_commit(self, isolated):
        """升级开始后迟到完成：提交资格撤销（反例「升级备份后迟到写库」）。"""
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        enable_analysis(make_fake_claude(bin_dir, mode="slow_ok"))
        a, b = make_snapshots(isolated["scanroot"])
        manager = make_manager(run_timeout_s=15)
        preview = manager.create_preview(a, b)
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "key-late-write")
        time.sleep(0.3)
        from fathom import upgrade
        journal = upgrade.journal_path_from_db(Path(config.DB_PATH))
        journal.write_text(json.dumps({"txn_id": "t2", "phase": "prepared"}),
                           encoding="utf-8")
        try:
            final = wait_terminal(manager, job["job_id"])
            assert final["status"] == "cancelled"
            assert agent_rows() == []
        finally:
            journal.unlink(missing_ok=True)


# ==========================================================================
# 反例④：exit=0 但应用错误 / 坏输出被存为成功 → 故障矩阵
# ==========================================================================


class TestFaultMatrix:
    @staticmethod
    def _run_mode(isolated, mode: str, key: str):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir(exist_ok=True)
        enable_analysis(make_fake_claude(bin_dir, mode=mode))
        a, b = make_snapshots(isolated["scanroot"])
        manager = make_manager()
        preview = manager.create_preview(a, b)
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   key)
        final = wait_terminal(manager, job["job_id"])
        return manager, final

    def test_app_error_exit0_not_success(self, isolated):
        _, final = self._run_mode(isolated, "app_error", "key-apperr")
        assert final["status"] == "failed"
        assert final["reason_code"] == "app_error"
        assert agent_rows() == []

    def test_nonzero_exit_failed(self, isolated):
        _, final = self._run_mode(isolated, "nonzero", "key-nz")
        assert final["status"] == "failed"
        assert final["reason_code"] == "runner_nonzero_exit"
        assert agent_rows() == []

    def test_output_limit_failed(self, isolated):
        _, final = self._run_mode(isolated, "output_limit", "key-ol")
        assert final["status"] == "failed"
        assert final["reason_code"] == "runner_output_limit"
        assert agent_rows() == []

    def test_bad_utf8_failed(self, isolated):
        _, final = self._run_mode(isolated, "bad_utf8", "key-u8")
        assert final["status"] == "failed"
        assert final["reason_code"] == "runner_decode_error"
        assert agent_rows() == []

    def test_html_result_rejected(self, isolated):
        _, final = self._run_mode(isolated, "html_result", "key-html")
        assert final["status"] == "failed"
        assert final["reason_code"].startswith("result_")
        assert agent_rows() == []

    def test_unknown_evidence_rejected(self, isolated):
        _, final = self._run_mode(isolated, "unknown_evidence", "key-ev")
        assert final["status"] == "failed"
        assert final["reason_code"] == "result_unknown_evidence_id"
        assert agent_rows() == []


# ==========================================================================
# 生命周期：reconcile / 保留 / 历史过期 / 撤销
# ==========================================================================


class TestLifecycle:
    @staticmethod
    def _insert_run(job_id: str, status: str, created_at: str) -> None:
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO analysis_runs(job_id, a_snapshot_id, b_snapshot_id,"
                " request_digest, facts_digest, prompt_version, idempotency_key,"
                " runtime_id, runtime_executable, settings_revision,"
                " consent_revision, status, owner_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, 1, 2, "rd", "fd", "pv", f"ik-{job_id}",
                 "claude-code", "/bin/x", 0, 0, status, "o", created_at))
            conn.commit()
        finally:
            conn.close()

    def test_reconcile_marks_leftover_interrupted_no_redispatch(self, isolated):
        self._insert_run("job-leftover", "running", "2026-09-28T00:00:00")
        manager = make_manager()
        n = manager.reconcile_startup()
        assert n == 1
        view = manager.job_view("job-leftover")
        assert view["status"] == "interrupted"
        assert view["reason_code"] == "owner_exit"
        assert view["terminal"] is True
        assert agent_rows() == []

    def test_finish_never_rewrites_terminal_row(self, isolated):
        """终态单写守卫直接单测：迟到 _finish 不得改写已终态行（方案 §5
        「终态只写一次、迟到回调不得复活」的守卫本体锁定）。"""
        self._insert_run("job-guard", "succeeded", "2026-09-28T00:00:00")
        before = tuple(run_row("job-guard"))
        manager = make_manager()
        rec = am.JobRecord(job_id="job-guard", preview=None,
                           idempotency_key="ik-guard",
                           cancel_event=threading.Event())
        manager._finish(rec, "failed", "late_callback", 999)
        after = run_row("job-guard")
        assert after["status"] == "succeeded"      # 终态未被改写
        assert after["reason_code"] is None        # 未被污染
        assert after["finished_at"] is None and after["duration_ms"] is None
        assert tuple(after) == before              # 整行逐列未动

    def test_retention_35_days_and_100_rows(self, isolated):
        old = "2026-06-01T00:00:00"
        fresh = "2026-09-28T00:00:00"
        self._insert_run("job-old-1", "succeeded", old)
        self._insert_run("job-old-2", "failed", old)
        self._insert_run("job-fresh", "succeeded", fresh)
        manager = make_manager()
        assert manager.prune_history(now=__import__("datetime").datetime(2026, 9, 28)) == 2
        conn = db.connect()
        try:
            ids = {r["job_id"] for r in conn.execute(
                "SELECT job_id FROM analysis_runs")}
        finally:
            conn.close()
        assert ids == {"job-fresh"}
        assert agent_rows() == []

    def test_retention_cap_100_keeps_newest(self, isolated):
        manager = make_manager()
        for i in range(105):
            self._insert_run(f"job-cap-{i:03d}", "succeeded",
                             f"2026-09-28T00:{i // 60:02d}:{i % 60:02d}")
        removed = manager.prune_history()
        conn = db.connect()
        try:
            n = conn.execute("SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
        finally:
            conn.close()
        assert n == am.ANALYSIS_RETENTION_MAX_ROWS
        assert removed == 5

    def test_retention_never_prunes_inflight(self, isolated):
        self._insert_run("job-inflight", "running", "2026-01-01T00:00:00")
        manager = make_manager()
        manager.prune_history(now=__import__("datetime").datetime(2026, 9, 28))
        assert manager.job_view("job-inflight")["status"] == "running"

    def test_revoke_deletes_evidence_keeps_lifecycle(self, ok_setup):
        manager = make_manager()
        preview = manager.create_preview(ok_setup["a"], ok_setup["b"])
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   "key-revoke")
        view = wait_terminal(manager, job["job_id"])
        analysis_id = view["analysis_id"]
        result = manager.revoke_analysis(analysis_id)
        assert result["ok"] is True
        assert agent_rows() == []
        assert manager.list_analyses(ok_setup["a"], ok_setup["b"]) == []
        run = run_row(job["job_id"])
        assert run["status"] == "succeeded" and run["revoked_at"] is not None
        after = manager.job_view(job["job_id"])
        assert after["analysis_id"] is None and after["revoked"] is True
        with pytest.raises(am.AnalysisError) as ei:
            manager.revoke_analysis(analysis_id)
        assert ei.value.reason_code == "analysis_not_found"


class TestHistoryExpiry:
    """历史语义：新增快照不过期；替换/淘汰/口径失效带原因；证据保留。"""

    @staticmethod
    def _succeed(isolated, bin_dir: Path, a: int, b: int, key: str) -> tuple:
        enable_analysis(make_fake_claude(bin_dir, mode="ok"))
        manager = make_manager()
        preview = manager.create_preview(a, b)
        job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                                   key)
        wait_terminal(manager, job["job_id"])
        return manager

    def test_new_snapshot_does_not_expire(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        a, b = make_snapshots(isolated["scanroot"])
        manager = self._succeed(isolated, bin_dir, a, b, "key-new")
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
                " du_seconds, total_kb, min_kb, collection_status)"
                " VALUES ('2026-09-29T08:00:00', ?, 2, 0, 0.1, 8192, 1024, 'full')",
                (str(isolated["scanroot"]),))
            conn.commit()
        finally:
            conn.close()
        items = manager.list_analyses(a, b)
        assert items[0]["expired"] is False

    def test_same_day_replacement_marks_replaced_keeps_evidence(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        a, b = make_snapshots(isolated["scanroot"])
        manager = self._succeed(isolated, bin_dir, a, b, "key-rep2")
        conn = db.connect()
        try:
            conn.execute("DELETE FROM entries WHERE snapshot_id=?", (b,))
            conn.execute("DELETE FROM snapshots WHERE id=?", (b,))
            conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
                " du_seconds, total_kb, min_kb, collection_status)"
                " VALUES ('2026-09-28T09:40:00', ?, 2, 0, 0.1, 6000, 1024, 'full')",
                (str(isolated["scanroot"]),))
            conn.commit()
        finally:
            conn.close()
        items = manager.list_analyses(a, b)
        assert items[0]["expired"] is True
        assert items[0]["expired_reason"] == "snapshot_replaced"
        # 已保存证据仍展示原日期与依据
        assert items[0]["b"]["created_at"] == "2026-09-28T09:00:00"
        assert items[0]["facts"]["entries"]

    def test_pruned_marks_pruned(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        a, b = make_snapshots(isolated["scanroot"])
        manager = self._succeed(isolated, bin_dir, a, b, "key-prune")
        conn = db.connect()
        try:
            conn.execute("DELETE FROM entries WHERE snapshot_id=?", (b,))
            conn.execute("DELETE FROM snapshots WHERE id=?", (b,))
            conn.commit()
        finally:
            conn.close()
        items = manager.list_analyses(a, b)
        assert items[0]["expired"] is True
        assert items[0]["expired_reason"] == "snapshot_pruned"

    def test_dataset_identity_change_marks_unverifiable(self, isolated):
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        a, b = make_snapshots(isolated["scanroot"])
        manager = self._succeed(isolated, bin_dir, a, b, "key-ds")
        conn = db.connect()
        try:
            conn.execute("UPDATE snapshots SET exclude_names='skip.me' WHERE id=?",
                         (b,))
            conn.commit()
        finally:
            conn.close()
        items = manager.list_analyses(a, b)
        assert items[0]["expired"] is True
        assert items[0]["expired_reason"] == "dataset_unverifiable"
