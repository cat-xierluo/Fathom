"""分析 × 升级停写协调集成（ISS-035B，经生产 UpgradeCoordinator）。

合同（方案 §5 运行合同最后一条 + TASKS ISS-035B 验收框 7）：
- 升级 prepare ①在扫描租约后取分析租约：分析在途 → 明确拒绝升级
  （analysis_busy），零信号、无 journal、无备份，绝不终止在途分析；
- journal 建立后（prepare 成功）新分析预览/派发一律 409
  upgrade_in_progress（租约外粗检 + 租约内复检，manager 侧）；
- 分析在途不持扫描 flock、不持长 DB 事务：扫描租约可独立取得，
  并发短写可提交（后台扫描不受分析影响）。

环境用 030A 夹具（``tests/upgrade_fixture.py``）搭建，协议走**生产**
``fathom/upgrade.py``（与 test_upgrade_production_wiring 同款注入模式）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from fathom import analysis_manager as am
from fathom import config, db, scan_coordinator, upgrade

import upgrade_fixture as uf

from tests.test_analysis_manager import (FAKE_VERSION, enable_analysis,
                                         make_fake_claude, make_snapshots,
                                         wait_terminal)


@pytest.fixture
def env(tmp_path):
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        yield fake
    finally:
        fake.close()


def _paths(env) -> upgrade.UpgradePaths:
    return upgrade.UpgradePaths(
        runtime_dir=env.runtime,
        db_path=env.db_path,
        lock_path=env.scan_lock_path,
        instance_path=env.instance_path,
        journal_path=env.journal_path,
    )


def _coordinator(env) -> upgrade.UpgradeCoordinator:
    return upgrade.UpgradeCoordinator(
        _paths(env),
        from_version=env.n_version,
        to_version=env.n_plus_1_version,
        hooks={
            "pid_alive": env.pid_alive,
            "port_open": lambda port: False,
            "request_helper_exit": lambda pid: env.stop_helper(pid),
        },
    )


def test_prepare_refused_while_analysis_in_flight(env):
    """分析在途（持有分析租约）：升级明确拒绝，零副作用。"""
    lease = am.AnalysisLease.acquire(
        am.analysis_lock_path(env.db_path), source="analysis")
    try:
        coord = _coordinator(env)
        result = coord.run_prepare()
        assert result["ok"] is False
        assert result["kind"] == "analysis_busy"
        assert "变化解读" in result["error"]
        # 零副作用：无 journal、无备份、在途分析不受影响
        assert not env.journal_path.exists()
        assert result.get("backup_path") is None
        assert coord.backup_path is None
    finally:
        lease.release()
    # 释放后可重试并成功
    coord = _coordinator(env)
    result = coord.run_prepare()
    assert result["ok"] is True, result


def test_prepared_upgrade_blocks_analysis_preview_and_start(env, monkeypatch):
    """journal 建立后：新预览/派发全部 409 upgrade_in_progress。"""
    monkeypatch.setattr(config, "DB_PATH", env.db_path)
    bin_dir = env.runtime / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = make_fake_claude(bin_dir, mode="ok")
    a, b = make_snapshots(_scanroot(env))
    settings = config.UserSettings(analysis=config.AnalysisSettings(
        enabled=True, runtime_id="claude-code",
        runtime_executable=str(fake), runtime_version=FAKE_VERSION))
    monkeypatch.setattr(config, "_USER_SETTINGS", settings)

    # journal 建立前：预览与派发正常可用
    manager = am.AnalysisManager(db_path=env.db_path, run_timeout_s=10)
    preview = manager.create_preview(a, b)

    coord = _coordinator(env)
    result = coord.run_prepare()
    assert result["ok"] is True, result

    with pytest.raises(am.AnalysisError) as ei:
        manager.create_preview(a, b)
    assert ei.value.reason_code == "upgrade_in_progress"
    with pytest.raises(am.AnalysisError) as ei2:
        manager.start_job(preview.preview_id, preview.request_digest, "key-upg")
    assert ei2.value.reason_code == "upgrade_in_progress"
    # 清理 journal（模拟 finalize），恢复可写
    upgrade.clear_journal(_paths(env))
    preview2 = manager.create_preview(a, b)
    job, _ = manager.start_job(preview2.preview_id, preview2.request_digest,
                               "key-after-upg")
    wait_terminal(manager, job["job_id"])


def _scanroot(env) -> Path:
    root = env.runtime / "scanroot"
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    return root


def test_analysis_running_scan_flock_and_short_writes_free(env, monkeypatch):
    """分析在途：扫描租约可独立取得（不长期持扫描 flock），并发短写可提交。"""
    monkeypatch.setattr(config, "DB_PATH", env.db_path)
    bin_dir = env.runtime / "bin"
    bin_dir.mkdir(exist_ok=True)
    settings = config.UserSettings(analysis=config.AnalysisSettings(
        enabled=True, runtime_id="claude-code",
        runtime_executable=str(make_fake_claude(bin_dir, mode="sleep")),
        runtime_version=FAKE_VERSION))
    monkeypatch.setattr(config, "_USER_SETTINGS", settings)
    a, b = make_snapshots(_scanroot(env))
    manager = am.AnalysisManager(db_path=env.db_path, run_timeout_s=10)
    preview = manager.create_preview(a, b)
    job, _ = manager.start_job(preview.preview_id, preview.request_digest,
                               "key-scan-free")
    try:
        # 分析在跑：扫描租约独立可取（独立锁文件，互不阻塞）
        lease = scan_coordinator.ScanLease.acquire(env.scan_lock_path,
                                                   source="cli")
        # 并发短写可提交：分析不持有长 DB 事务
        conn = db.connect(env.db_path)
        try:
            conn.execute("INSERT INTO scan_runs(started_at, status)"
                         " VALUES (?, 'done')", ("2026-09-28T00:00:00",))
            conn.commit()
        finally:
            conn.close()
        lease.release()
    finally:
        manager.cancel_job(job["job_id"])
        final = wait_terminal(manager, job["job_id"])
    assert final["status"] == "cancelled"
