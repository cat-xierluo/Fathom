"""ISS-030A：升级协调协议夹具自动验（N→N+1 六步 + 四类失败 + schema 拒绝）。

合同来源：docs/TASKS.md 的 ISS-030A 卡与父卡 ISS-030 验收框 1/2/3、
076 清单 §6-F1。夹具实现见 ``tests/upgrade_fixture.py``（fake 环境、
六步协议、失败注入、半升级态检测与恢复）。

覆盖（每项独立断言，全部隔离在 tmp 目录，零生产触碰）：

- **fake 环境**：N/N+1 app 目录形态（Info.plist 版本文件）、真实
  sqlite3 WAL 库（含已知数据行，部分行刻意驻留 WAL）、假
  helper-instance.json（0600）、010B 合同形态的 LaunchAgents plist。
- **六步 happy path**：①停写（真实 ScanLease flock，ISS-020 合同）
  → ②旧 helper 退出（进程标记确认后清 instance 文件）→ ③SQLite
  一致备份（真实 WAL checkpoint + backup API，非文件拷贝）→ ④替换
  （N+1 就位、N 备份保留）→ ⑤新 helper 启动（instance 重建）→
  ⑥版本握手一致；全程每步可独立断言。
- **四类失败反例**：备份失败（checkpoint 异常注入）/ 新 helper 握手
  失败（版本不匹配）/ 中途退出（步骤间模拟中断，可检测可恢复）/
  磁盘不足（ENOSPC 写满注入）——全部回滚到可运行旧版与旧数据，
  不留半升级态。
- **schema 拒绝**：较新 schema（user_version > SCHEMA_VERSION）与
  不可识别的旧 schema（未知未版本化表）→ 引用 ``fathom.db`` 既有
  迁移守卫，在任何停写/备份/替换之前拒绝危险操作，原库字节不变。
"""

from __future__ import annotations

import errno
import shutil
import sqlite3
from pathlib import Path

import pytest

from fathom import __protocol_version__, __version__, config, db, scan_coordinator

import upgrade_fixture as uf


STEP_LABELS = [
    "1_quiesce", "2_old_helper_exit", "3_backup",
    "4_replace", "5_new_helper", "6_handshake",
]


@pytest.fixture
def env(tmp_path):
    """每个测试独立 fake 运行根；结束时关闭持有的 DB 连接。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        yield fake
    finally:
        fake.close()


def _new_scan_lease_ok(env) -> None:
    """停写租约已释放：新进程可立即取得扫描租约（可重新写入）。"""
    lease = scan_coordinator.ScanLease.acquire(env.scan_lock_path, source="cli")
    lease.release()


# ================================================================ fake 环境
def test_fake_env_matches_release_layout_and_versions(env):
    """夹具环境形态：两版 app、WAL 库、0600 instance 文件、010B plist。"""
    # N 版已安装、N+1 在 staging 就位（版本文件可读）。
    assert env.installed_version() == __version__
    assert env.staged_version() == uf._next_patch_version(__version__)
    assert env.helper_binary_version(env.installed_app) == __version__
    # 真实 sqlite3 WAL 库：主文件 + 驻留 WAL 的已提交行。
    assert env.db_path.is_file()
    wal = env.db_path.with_name(env.db_path.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0
    assert env.snapshot_roots() == env.expected_roots()
    # 假 helper-instance.json：0600，helper.rs 语义字段，pid 假进程存活。
    assert env.instance_path.stat().st_mode & 0o777 == 0o600
    instance = env.read_instance()
    assert instance["service"] == "fathom"
    assert instance["protocol_version"] == __protocol_version__
    assert instance["version"] == __version__
    assert env.pid_alive(int(instance["pid"]))
    # LaunchAgents plist：010B 合同形态，指向当前安装的 helper。
    import plistlib

    scan_plist = plistlib.loads(
        (env.launchagents_dir / f"{config.SCAN_LABEL}.plist").read_bytes()
    )
    web_plist = plistlib.loads(
        (env.launchagents_dir / f"{config.WEB_LABEL}.plist").read_bytes()
    )
    helper_bin = str(env.installed_app / "Contents" / "MacOS" / "helper")
    assert scan_plist["Label"] == config.SCAN_LABEL
    assert scan_plist["ProgramArguments"][0] == helper_bin
    assert scan_plist["ProgramArguments"][1:] == ["scan", "--source", "scheduled"]
    assert web_plist["Label"] == config.WEB_LABEL
    assert web_plist["ProgramArguments"] == [helper_bin, "serve"]
    # 初始无升级 journal：不在半升级态。
    assert not env.journal_path.exists()
    assert uf.detect_upgrade_state(env) == "clean"


# ============================================================ 六步 happy path
def test_step1_quiesce_blocks_new_scan_sessions_under_real_flock(env):
    """①停写：升级持锁期间，新写入会话（扫描租约）被真实 flock 拒绝。"""
    coord = uf.UpgradeCoordinator(env)
    try:
        coord.step1_quiesce()
        # ISS-020 合同：持锁期间新 ScanLease.acquire 必须 busy，不猜 PID、
        # 不发信号——「停写」即拒绝新的写入会话。
        with pytest.raises(scan_coordinator.ScanBusyError):
            scan_coordinator.ScanLease.acquire(
                env.scan_lock_path, source="scheduled"
            )
    finally:
        coord.dispose()
    # 停写结束（锁释放）后，新会话立即可以取得租约。
    _new_scan_lease_ok(env)


def test_step2_old_helper_exit_confirms_process_then_clears_instance(env):
    """②旧 helper 退出：先确认进程退出（标记消失），再清 instance 文件。"""
    old_pid = int(env.read_instance()["pid"])
    coord = uf.UpgradeCoordinator(env)
    try:
        result = coord.run(through=2)
        assert result.ok, result.error
        assert coord.steps_done == STEP_LABELS[:2]
    finally:
        coord.dispose()
    # 进程已退出（标记消失）且 instance 文件已清理。
    assert not env.pid_alive(old_pid)
    assert not env.instance_path.exists()

    # 清理语义反例：instance 文件残留但 pid 已死（陈旧文件）时，
    # 步骤②幂等清掉文件，不把陈旧 JSON 当作存活的 helper。
    stale_pid = env.start_helper(env.n_version)
    env.stop_helper(stale_pid)
    assert not env.pid_alive(stale_pid)
    coord2 = uf.UpgradeCoordinator(env)
    try:
        coord2.step1_quiesce()
        coord2.step2_old_helper_exit()  # pid 已死：不报错、不等待
    finally:
        coord2.dispose()
    assert not env.instance_path.exists()


def test_step3_backup_uses_wal_checkpoint_and_backup_api_not_file_copy(env):
    """③备份：真实 checkpoint + backup API——驻留 WAL 的已提交行全部进入
    备份库，而只拷主文件会丢行（证明不是文件拷贝）。"""
    # 反例证据先行：只拷主文件（不带 -wal）的"备份"缺少 WAL 驻留行。
    plain_copy = env.data_dir / "plain-copy-probe.db"
    shutil.copyfile(env.db_path, plain_copy)
    probe = sqlite3.connect(plain_copy)
    try:
        plain_roots = sorted(
            str(row[0]) for row in probe.execute("SELECT root FROM snapshots")
        )
    finally:
        probe.close()
    assert plain_roots == [uf.FakeUpgradeEnv.BASE_ROOT]
    assert uf.FakeUpgradeEnv.WAL_ONLY_ROOT not in plain_roots

    coord = uf.UpgradeCoordinator(env)
    try:
        result = coord.run(through=3)
        assert result.ok, result.error
        assert coord.steps_done == STEP_LABELS[:3]
    finally:
        coord.dispose()
    # checkpoint 真实执行过（返回 busy/log/checkpointed 三元组）。
    assert coord.checkpoint_result is not None
    assert len(coord.checkpoint_result) == 3
    # 备份库独立打开（无 -wal 伴随文件），含 N 版全部已提交数据行。
    backup = coord.backup_path
    assert backup is not None and backup.is_file()
    assert not backup.with_name(backup.name + "-wal").exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    conn = sqlite3.connect(backup)
    try:
        roots = sorted(str(r[0]) for r in conn.execute("SELECT root FROM snapshots"))
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert roots == env.expected_roots()
    assert uf.FakeUpgradeEnv.WAL_ONLY_ROOT in roots


def test_step4_replace_installs_new_version_and_preserves_n_backup(env):
    """④替换：N+1 就位、N 版备份保留、DB 数据行原样不动。"""
    coord = uf.UpgradeCoordinator(env)
    try:
        result = coord.run(through=4)
        assert result.ok, result.error
        assert coord.steps_done == STEP_LABELS[:4]
    finally:
        coord.dispose()
    n1 = uf._next_patch_version(__version__)
    assert env.installed_version() == n1
    assert env.helper_binary_version(env.installed_app) == n1
    assert not env.staged_app.exists()  # 候选已就位，staging 清空
    # N 版备份保留在 rollback 位，且其内容仍是 N 版形态。
    assert env.rollback_app.is_dir()
    assert uf._read_app_version(env.rollback_app) == __version__
    # 替换不触碰 DB：数据行与来源不变。
    assert env.snapshot_roots() == env.expected_roots()


def test_step5_new_helper_rebuilds_instance_file(env):
    """⑤新 helper 启动：instance 文件重建（0600、新 pid、N+1 版本）。"""
    old_pid = int(env.read_instance()["pid"])
    coord = uf.UpgradeCoordinator(env)
    try:
        result = coord.run(through=5)
        assert result.ok, result.error
        assert coord.steps_done == STEP_LABELS[:5]
    finally:
        coord.dispose()
    instance = env.read_instance()
    new_pid = int(instance["pid"])
    assert new_pid != old_pid
    assert env.pid_alive(new_pid)
    assert instance["version"] == uf._next_patch_version(__version__)
    assert instance["service"] == "fathom"
    assert instance["protocol_version"] == __protocol_version__
    assert env.instance_path.stat().st_mode & 0o777 == 0o600


def test_step6_handshake_requires_matching_versions(env):
    """⑥版本握手：app/helper/协议身份一致为 N+1 且进程存活才算成功。"""
    coord = uf.UpgradeCoordinator(env)
    try:
        result = coord.run()
        assert result.ok and result.finalized, result.error
        assert coord.steps_done == STEP_LABELS
        # 握手通过意味着所有版本来源一致。
        assert env.installed_version() == uf._next_patch_version(__version__)
        assert env.read_instance()["version"] == uf._next_patch_version(__version__)
        # 失败分支：任何来源不一致都拒绝。
        env.instance_path.write_text(
            env.instance_path.read_text(encoding="utf-8").replace(
                uf._next_patch_version(__version__), "9.9.9"
            ),
            encoding="utf-8",
        )
        with pytest.raises(uf.HandshakeError, match="版本握手失败"):
            coord.step6_handshake()
    finally:
        coord.dispose()


def test_full_happy_path_completes_six_steps_with_clean_finalize(env):
    """六步全链：步骤按序全过、journal 清除、锁释放、N 备份保留。"""
    coord = uf.UpgradeCoordinator(env)
    try:
        result = coord.run()
        assert result.ok and result.finalized, result.error
    finally:
        coord.dispose()
    assert result.steps_done == STEP_LABELS
    assert coord.steps_done == STEP_LABELS
    assert not env.journal_path.exists()  # 不留半升级态标记
    assert uf.detect_upgrade_state(env) == "clean"
    _new_scan_lease_ok(env)  # 停写租约已释放
    # 终态：安装 N+1、新 helper 存活、DB 全部数据行、N 备份保留。
    assert env.installed_version() == uf._next_patch_version(__version__)
    assert env.pid_alive(int(env.read_instance()["pid"]))
    assert env.snapshot_roots() == env.expected_roots()
    assert result.backup_path is not None and result.backup_path.is_file()
    assert uf._read_app_version(env.rollback_app) == __version__
    # 升级后 LaunchAgents plist 指向的稳定安装路径依然有效。
    helper_bin = env.installed_app / "Contents" / "MacOS" / "helper"
    assert helper_bin.is_file()
    assert env.helper_binary_version(env.installed_app) == (
        uf._next_patch_version(__version__)
    )


# ============================================================ 四类失败反例
def test_backup_checkpoint_failure_leaves_old_version_runnable(env):
    """备份失败（checkpoint 异常注入）→ 旧版原样可运行、旧数据完整。"""
    def injected_checkpoint():
        raise sqlite3.OperationalError("injected: checkpoint 失败")

    coord = uf.UpgradeCoordinator(env, hooks={"checkpoint": injected_checkpoint})
    try:
        result = coord.run()
    finally:
        coord.dispose()
    assert result.ok is False
    assert result.error_kind == "backup"
    assert result.rolled_back is True
    assert "restarted_old_helper" in result.rollback_actions
    # 旧版原样可运行：安装仍为 N，旧 helper 已回滚重启并存活。
    assert env.installed_version() == __version__
    instance = env.read_instance()
    assert instance["version"] == __version__
    assert env.pid_alive(int(instance["pid"]))
    # 旧数据完整；无半升级态；不留备份临时文件。
    assert env.snapshot_roots() == env.expected_roots()
    assert not env.journal_path.exists()
    assert uf.detect_upgrade_state(env) == "clean"
    assert list(env.data_dir.glob("*.upgrade-backup-*")) == []
    _new_scan_lease_ok(env)


def test_handshake_mismatch_rolls_back_to_n_with_intact_history(env):
    """新 helper 握手失败（上报旧版本）→ 回滚 N 版且旧数据完整。"""
    coord = uf.UpgradeCoordinator(
        env, hooks={"new_helper_version": lambda: __version__}
    )
    try:
        result = coord.run()
    finally:
        coord.dispose()
    assert result.ok is False
    assert result.error_kind == "handshake"
    assert result.rolled_back is True
    assert result.steps_done == STEP_LABELS[:5]  # 步骤⑥才发现失败
    # 回滚后：安装恢复 N、rollback 位清空、旧 helper 以 N 版存活。
    assert env.installed_version() == __version__
    assert not env.rollback_app.exists()
    instance = env.read_instance()
    assert instance["version"] == __version__
    assert env.pid_alive(int(instance["pid"]))
    # 旧数据完整；无半升级态；备份文件仍在（历史证据不因回滚删除）。
    assert env.snapshot_roots() == env.expected_roots()
    assert not env.journal_path.exists()
    assert result.backup_path is not None and result.backup_path.is_file()
    _new_scan_lease_ok(env)


@pytest.mark.parametrize("abort_after", [2, 3, 4])
def test_mid_upgrade_abort_is_detectable_and_recoverable(env, abort_after):
    """中途退出（步骤间模拟中断）→ 半升级态可检测，恢复旧版后可运行。"""
    coord = uf.UpgradeCoordinator(env, hooks={"abort_after": abort_after})
    try:
        with pytest.raises(uf.UpgradeAborted, match="模拟中途退出"):
            coord.run()
    finally:
        coord.dispose()
    # 中断后不回滚：journal 在位 → 半升级态可检测。
    assert uf.detect_upgrade_state(env) == "half_upgraded"
    if abort_after >= 4:
        # 替换已完成但新 helper 未启动/未握手：最坏形态的半升级态。
        assert env.installed_version() == uf._next_patch_version(__version__)
    # 恢复流程真实执行：回到可运行旧版与旧数据。
    recovery = uf.recover_from_abort(env)
    assert recovery["state_before"] == "half_upgraded"
    assert recovery["recovered_version"] == __version__
    if abort_after >= 4:
        assert "restored_app_from_rollback" in recovery["actions"]
    assert env.installed_version() == __version__
    instance = env.read_instance()
    assert instance["version"] == __version__
    assert env.pid_alive(int(instance["pid"]))
    assert env.snapshot_roots() == env.expected_roots()
    # 恢复后无半升级态，且停写锁已随"崩溃进程"消失，可重新写入。
    assert not env.journal_path.exists()
    assert uf.detect_upgrade_state(env) == "clean"
    _new_scan_lease_ok(env)


def test_disk_full_injection_fails_without_losing_old_data(env):
    """磁盘不足（ENOSPC 写满注入）→ 失败且旧数据完整、旧版可运行。"""
    def injected_disk_full():
        raise OSError(errno.ENOSPC, "No space left on device (injected)")

    coord = uf.UpgradeCoordinator(env, hooks={"backup_write": injected_disk_full})
    try:
        result = coord.run()
    finally:
        coord.dispose()
    assert result.ok is False
    assert result.error_kind == "disk_full"
    assert "No space left" in (result.error or "")
    assert result.rolled_back is True
    # 旧版原样可运行、旧 helper 存活、旧数据完整。
    assert env.installed_version() == __version__
    instance = env.read_instance()
    assert instance["version"] == __version__
    assert env.pid_alive(int(instance["pid"]))
    assert env.snapshot_roots() == env.expected_roots()
    # 无半升级态、无备份残留（临时文件已清理）。
    assert not env.journal_path.exists()
    assert list(env.data_dir.glob("*.tmp")) == []
    assert list(env.data_dir.glob("*.upgrade-backup-*")) == []
    _new_scan_lease_ok(env)


# ================================================================ schema 拒绝
def test_newer_schema_rejected_before_any_dangerous_operation(tmp_path):
    """较新 schema：db.connect 拒绝降级打开，协调器在第一步之前拒绝；
    原库字节不变（未迁移、未重写）。"""
    fake = uf.FakeUpgradeEnv.build(
        tmp_path / "upgrade-env", db_builder=uf.write_newer_schema_db
    )
    try:
        before = uf.file_sha256(fake.db_path)
        # 既有守卫语义：更高版本的库拒绝打开（不做降级迁移）。
        with pytest.raises(db.UnsupportedSchemaVersion):
            db.connect(fake.db_path)
        assert uf.file_sha256(fake.db_path) == before
        # 协调器预检拒绝：零步骤、零回滚动作、无 journal、无备份。
        coord = uf.UpgradeCoordinator(fake)
        try:
            result = coord.run()
        finally:
            coord.dispose()
        assert result.ok is False
        assert result.error_kind == "schema_refused"
        assert result.steps_done == []
        assert result.rolled_back is False
        assert not fake.journal_path.exists()
        assert list(fake.data_dir.glob("*.upgrade-backup-*")) == []
        # 安装与旧 helper 全程未被触碰。
        assert fake.installed_version() == __version__
        assert fake.pid_alive(int(fake.read_instance()["pid"]))
        assert uf.file_sha256(fake.db_path) == before
    finally:
        fake.close()


def test_unrecognized_old_schema_rejected_before_any_dangerous_operation(tmp_path):
    """不可识别的旧 schema（未知未版本化表）：守卫拒绝，协调器零副作用，
    不做迁移、不生成迁移备份，原库字节不变。"""
    fake = uf.FakeUpgradeEnv.build(
        tmp_path / "upgrade-env",
        db_builder=uf.write_unrecognized_old_schema_db,
    )
    try:
        before = uf.file_sha256(fake.db_path)
        with pytest.raises(db.DatabaseOpenError, match="未知未版本化表"):
            db.connect(fake.db_path)
        assert uf.file_sha256(fake.db_path) == before
        # 迁移守卫先于一致备份：拒绝时不得生成迁移备份文件。
        assert list(fake.data_dir.glob("*.backup-v*")) == []
        coord = uf.UpgradeCoordinator(fake)
        try:
            result = coord.run()
        finally:
            coord.dispose()
        assert result.ok is False
        assert result.error_kind == "schema_refused"
        assert result.steps_done == []
        assert result.rolled_back is False
        assert not fake.journal_path.exists()
        assert fake.installed_version() == __version__
        assert fake.pid_alive(int(fake.read_instance()["pid"]))
        assert uf.file_sha256(fake.db_path) == before
        assert list(fake.data_dir.glob("*.backup-v*")) == []
    finally:
        fake.close()
