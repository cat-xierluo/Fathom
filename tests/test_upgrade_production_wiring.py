"""ISS-040C · 生产更新协调接线集成验（经生产入口，外部动作注入）。

合同来源：docs/TASKS.md 的 ISS-040C 卡「接口合同」六条与验收四框。与
ISS-030A（``tests/test_upgrade_coordination.py``，夹具）不同：本文件驱动
**生产**升级协调入口——``fathom/upgrade.py`` 的 ``UpgradeCoordinator`` 与
``fathom.cli`` 的 upgrade-* 子命令（与冻结 helper 同入口 ``main.py``，
即 lib.rs ``updater_install`` 壳接线调用的同一命令行合同），外部下载与
系统动作一律注入 fake；030A 夹具（``tests/upgrade_fixture.py``）只用于
搭隔离环境（fake N/N+1 形态、真实 WAL 库、假 pid 标记），不替代生产
协议实现。

覆盖（全部隔离在 tmp 运行根，零生产触碰、零真实网络）：

- **prepare 四步**：①停写（真实 ``scan_coordinator.ScanLease`` flock；
  在途扫描→明确拒绝、绝不终止）→ ②旧 helper 优雅退出（pid/端口确认，
  超时→中止→回滚）→ ③一致备份（``db.consistent_backup`` 公共入口：
  checkpoint + backup API + 完整性校验，禁止文件拷贝）→ ④journal 落盘
  （半升级态可检测）。
- **五场景反例**（验收框 1）：正在扫描、旧 helper 不退出、备份失败、
  安装失败、新 helper 握手失败——全部回到可运行旧版与旧数据，不留
  半升级态。
- **CLI 子命令**（验收框 3）：经 ``main.py --runtime-dir <tmp>`` 子进程
  真实执行 upgrade-prepare/detect/rollback/finalize（含跨进程真实 flock
  拒绝、schema 拒绝原库字节不变）。
- **壳接线合同**（验收框 2/3 的 Python 侧钉子）：lib.rs 真实调用
  upgrade-prepare/finalize/rollback 子命令、取消事件通道不新增 ACL 权限、
  downloading 事件含 downloaded/total、安装不可取消文案、updater 命令面
  仍恰三条（恰 3 权限 ACL 不回退）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from fathom import SERVICE_IDENTITY, __protocol_version__, __version__, db, scan_coordinator

import upgrade_fixture as uf

from fathom import upgrade

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
LIB_RS = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "src" / "lib.rs"

# 死 pid（macOS pid 上限 99998，4000000 必不存在——helper.rs 同款判定）与
# 永不被用户态监听的回环端口：CLI 子进程里的生产默认探针（ps -p / TCP
# connect）据此确定性地判定「已退出、端口已释放」，零真实信号、零真实网络。
DEAD_PID = 4_000_000
DEAD_PORT = 1


@pytest.fixture
def env(tmp_path):
    """每个测试独立 fake 运行根（030A 夹具搭环境）；结束关闭持有的 DB 连接。"""
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


def _hooks(env, **overrides):
    """把生产可注入系统动作接到 fake 环境模型（外部动作注入，合同要求）。"""
    hooks = {
        "pid_alive": env.pid_alive,
        "port_open": lambda port: False,
        "request_helper_exit": lambda pid: env.stop_helper(pid),
        "start_helper": lambda: env.start_helper(env.n_version),
        "restore_old_version": lambda: _restore_old_app(env),
    }
    hooks.update(overrides)
    return hooks


def _restore_old_app(env) -> None:
    """030A 语义的旧版恢复（生产由安装器域承担；测试注入）。"""
    import shutil

    current = None
    if env.installed_app.exists():
        try:
            current = env.installed_version()
        except Exception:
            current = None
    if env.rollback_app.exists() and (
        not env.installed_app.exists() or current == env.n_plus_1_version
    ):
        if env.installed_app.exists():
            shutil.rmtree(env.installed_app)
        os.replace(env.rollback_app, env.installed_app)


def _coordinator(env, **hook_overrides) -> upgrade.UpgradeCoordinator:
    return upgrade.UpgradeCoordinator(
        _paths(env),
        from_version=env.n_version,
        to_version=env.n_plus_1_version,
        hooks=_hooks(env, **hook_overrides),
    )


def _fake_install(env, *, swap: bool = True):
    """注入的外部安装动作：swap=True 模拟 N+1 就位（030A 替换语义）。"""
    import shutil

    def _install(ctx: dict) -> None:
        if not swap:
            raise RuntimeError("injected: 下载/安装失败（未触碰任何文件）")
        if env.rollback_app.exists():
            raise RuntimeError("rollback 位非空，拒绝二次替换")
        shutil.rmtree(env.rollback_app, ignore_errors=True)
        os.replace(env.installed_app, env.rollback_app)
        os.replace(env.staged_app, env.installed_app)
        env.start_helper(env.n_plus_1_version)

    return _install


def _new_helper_identity(env, version: str | None = None):
    return lambda: {
        "service": SERVICE_IDENTITY,
        "protocol_version": __protocol_version__,
        "version": version if version is not None else env.read_instance()["version"],
    }


def _lease_acquirable(env) -> bool:
    """停写租约已释放：新扫描会话可立即取得（可重新写入）。"""
    lease = scan_coordinator.ScanLease.acquire(env.scan_lock_path, source="cli")
    lease.release()
    return True


def _old_version_runnable(env) -> None:
    """「回到可运行旧版与旧数据」的公共断言（五场景共用）。"""
    assert env.installed_version() == __version__
    instance = env.read_instance()
    assert instance["version"] == __version__
    assert env.pid_alive(int(instance["pid"]))
    assert env.snapshot_roots() == env.expected_roots()
    assert not env.journal_path.exists()
    assert upgrade.detect_upgrade_state(_paths(env)) == "clean"
    assert _lease_acquirable(env)


# ====================================================== db 公共一致备份入口
def test_db_consistent_backup_public_api_captures_wal_rows(env):
    """db.consistent_backup（ISS-040C 公共化）：checkpoint + backup API +
    完整性校验——驻留 WAL 的已提交行全部进入备份（非文件拷贝的反例证明）。"""
    backup = db.consistent_backup(env.db_path)
    assert backup.is_file()
    # 备份独立打开、无 -wal 伴随、0600、完整性 ok、含全部已提交行。
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


# ================================================== 半升级态检测（④生产化）
def test_detect_upgrade_state_production_semantics(env):
    """detect_upgrade_state：journal 在位（或安装缺失）→ half_upgraded；
    否则 clean（030A 语义生产化）。"""
    assert upgrade.detect_upgrade_state(_paths(env)) == "clean"
    upgrade.write_journal(
        _paths(env),
        {"service": SERVICE_IDENTITY, "from_version": "0.3.0",
         "to_version": "0.3.1", "phase": "prepared"},
    )
    assert upgrade.detect_upgrade_state(_paths(env)) == "half_upgraded"
    assert upgrade.read_journal(_paths(env))["phase"] == "prepared"
    upgrade.clear_journal(_paths(env))
    # 可选安装目录缺失同样视为半升级态（030A 语义）。
    assert upgrade.detect_upgrade_state(
        _paths(env), app_path=env.installed_app
    ) == "clean"
    assert upgrade.detect_upgrade_state(
        _paths(env), app_path=env.applications_dir / "Fathom-missing.app"
    ) == "half_upgraded"


# ================================================== prepare 四步（①-④）
def test_prepare_happy_path_writes_journal_and_releases_lease(env):
    """prepare happy path：四步按序、journal 在位（prepared）、一致备份落盘、
    旧 helper 退出、租约释放；DB 数据行不动。"""
    old_pid = int(env.read_instance()["pid"])
    coord = _coordinator(env)
    result = coord.run_prepare()
    assert result["ok"], result
    assert result["kind"] == "prepared"
    assert result["steps_done"] == [
        "1_quiesce", "2_old_helper_exit", "3_backup", "4_journal",
    ]
    # ②旧 helper 已退出（标记消失）且 instance 清理。
    assert not env.pid_alive(old_pid)
    assert not env.instance_path.exists()
    # ③一致备份存在且可独立打开。
    backup = Path(result["backup_path"])
    assert backup.is_file() and backup.parent == env.data_dir
    conn = sqlite3.connect(backup)
    try:
        roots = sorted(str(r[0]) for r in conn.execute("SELECT root FROM snapshots"))
    finally:
        conn.close()
    assert roots == env.expected_roots()
    # ④journal 在位：半升级态可检测，phase=prepared。
    journal = upgrade.read_journal(_paths(env))
    assert journal["phase"] == "prepared"
    assert journal["from_version"] == __version__
    assert upgrade.detect_upgrade_state(_paths(env)) == "half_upgraded"
    # ①租约已释放：可重新写入。
    assert _lease_acquirable(env)
    assert env.snapshot_roots() == env.expected_roots()


def test_prepare_refused_while_scan_in_flight_never_kills_scan(env):
    """场景①正在扫描：非阻塞租约被在途扫描持有→明确拒绝（可读失败+重试
    提示）；绝不终止在途扫描，helper/数据/journal 全不动。"""
    lease = scan_coordinator.ScanLease.acquire(
        env.scan_lock_path, source="scheduled"
    )
    owner_before = dict(lease.owner)
    try:
        coord = _coordinator(env)
        result = coord.run_prepare()
    finally:
        pass
    assert result["ok"] is False
    assert result["kind"] == "scan_busy"
    assert "扫描" in result["error"] and "重试" in result["error"]
    # 在途扫描租约原样持有（owner 未被改写），未被终止。
    assert lease.owner == owner_before
    lease.release()
    # 零副作用：无 journal、无备份、helper 存活、数据不动。
    assert not env.journal_path.exists()
    assert list(env.data_dir.glob("*.backup-v*")) == []
    assert env.pid_alive(int(env.read_instance()["pid"]))
    assert env.snapshot_roots() == env.expected_roots()
    assert _lease_acquirable(env)


def test_prepare_helper_not_exiting_times_out_and_keeps_old_running(env):
    """场景②旧 helper 不退出：有界确认超时→中止→回滚；旧 helper 仍可运行
    （未退出也未误杀）、旧数据完整、无半升级态。"""
    coord = _coordinator(
        env,
        request_helper_exit=lambda pid: None,  # 模拟忽略退出请求
        pid_alive=lambda pid: True,            # 模拟永不退出
    )
    result = coord.run_prepare()
    assert result["ok"] is False
    assert result["kind"] == "helper_exit_timeout"
    assert "中止" in result["error"] or "退出" in result["error"]
    # 旧 helper 从未退出：标记仍在、instance 完好——「回到可运行旧版」。
    assert env.pid_alive(int(env.read_instance()["pid"]))
    assert env.read_instance()["version"] == __version__
    # 未走到备份（步骤②失败先于③）。
    assert list(env.data_dir.glob("*.backup-v*")) == []
    assert env.snapshot_roots() == env.expected_roots()
    assert not env.journal_path.exists()
    assert _lease_acquirable(env)


def test_prepare_backup_failure_rolls_back_old_helper_and_data(
    env, monkeypatch
):
    """场景③备份失败：中止→回滚——旧 helper 恢复运行、旧数据不动、
    journal 清除、无临时残留。"""

    def _broken_backup(_path):
        raise sqlite3.OperationalError("injected: 备份失败")

    monkeypatch.setattr(db, "consistent_backup", _broken_backup)
    coord = _coordinator(env)
    result = coord.run_prepare()
    assert result["ok"] is False
    assert result["kind"] == "backup"
    assert "restarted_old_helper" in result["rollback_actions"]
    # 回滚后旧 helper 恢复运行（新 pid、N 版本）。
    instance = env.read_instance()
    assert instance["version"] == __version__
    assert env.pid_alive(int(instance["pid"]))
    assert env.snapshot_roots() == env.expected_roots()
    assert not env.journal_path.exists()
    assert list(env.data_dir.glob("*.tmp")) == []
    assert _lease_acquirable(env)


# ==================================================== run_full（⑤-⑥注入）
def test_run_full_happy_path_installs_and_finalizes(env):
    """全链（外部动作注入）：⑤安装就位+新 helper 启动 → ⑥握手一致 →
    journal 清除；N 备份保留、数据行不动、租约释放。"""
    coord = _coordinator(env)
    result = coord.run_full(
        install=_fake_install(env),
        new_helper_identity=_new_helper_identity(env),
    )
    assert result["ok"] and result["finalized"], result
    assert env.installed_version() == uf._next_patch_version(__version__)
    instance = env.read_instance()
    assert instance["version"] == uf._next_patch_version(__version__)
    assert env.pid_alive(int(instance["pid"]))
    assert env.snapshot_roots() == env.expected_roots()
    assert not env.journal_path.exists()
    assert upgrade.detect_upgrade_state(_paths(env)) == "clean"
    assert _lease_acquirable(env)
    # 备份与 N 版回滚位保留（升级证据不因成功删除）。
    assert coord.backup_path is not None and coord.backup_path.is_file()
    assert uf._read_app_version(env.rollback_app) == __version__


def test_run_full_install_failure_rolls_back_keeps_candidate(env):
    """场景④安装失败：外部安装动作抛错→回滚——旧版可运行、旧数据完整、
    候选（staged N+1）保留供重试、journal 清除。"""
    coord = _coordinator(env)
    result = coord.run_full(
        install=_fake_install(env, swap=False),
        new_helper_identity=_new_helper_identity(env),
    )
    assert result["ok"] is False
    assert result["kind"] == "install"
    _old_version_runnable(env)
    # 候选保留：staged N+1 原样在位。
    assert env.staged_app.is_dir()
    assert uf._read_app_version(env.staged_app) == uf._next_patch_version(__version__)
    # 备份文件保留（失败重试/取证），无临时残留。
    assert coord.backup_path is not None and coord.backup_path.is_file()
    assert list(env.data_dir.glob("*.tmp")) == []


def test_run_full_handshake_failure_rolls_back_via_journal_path(env):
    """场景⑤新 helper 握手失败：新 helper 上报旧版本→HandshakeError→经
    journal 判定路径回滚——旧版恢复运行、旧数据完整、无半升级态。"""
    coord = _coordinator(env)
    result = coord.run_full(
        install=_fake_install(env),
        new_helper_identity=_new_helper_identity(env, version=__version__),
    )
    assert result["ok"] is False
    assert result["kind"] == "handshake"
    assert "握手失败" in result["error"]
    # 回滚经 journal 判定：检测到 half_upgraded 才执行恢复动作。
    assert result["detected_state"] == "half_upgraded"
    assert "restored_old_version" in result["rollback_actions"]
    assert "restarted_old_helper" in result["rollback_actions"]
    assert "removed_journal" in result["rollback_actions"]
    _old_version_runnable(env)
    assert not env.rollback_app.exists()


# ==================================================== CLI 子命令（生产入口）
def _run_cli(runtime: Path, *args: str) -> tuple[subprocess.CompletedProcess, dict | None]:
    """经生产 CLI 入口 main.py 子进程执行（与冻结 helper 同一入口）。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("FATHOM_RUNTIME_DIR", None)
    env.pop("FATHOM_RUNTIME_MODE", None)
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"),
         "--runtime-dir", str(runtime), *args],
        capture_output=True, text=True, env=env, timeout=120,
        cwd=str(REPO_ROOT),
    )
    payload = None
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if lines:
        try:
            payload = json.loads(lines[-1])
        except ValueError:
            payload = None
    return proc, payload


def _write_dead_instance(env) -> None:
    """写入死 pid/死端口的 instance（生产默认探针可确定性确认已退出）。"""
    instance = {
        "pid": DEAD_PID,
        "port": DEAD_PORT,
        "service": SERVICE_IDENTITY,
        "protocol_version": __protocol_version__,
        "version": env.n_version,
        "instance_id": "cli-wiring-test",
        "runtime_mode": "release",
    }
    uf._write_0600(
        env.instance_path,
        json.dumps(instance, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def test_cli_upgrade_prepare_end_to_end_tmp_runtime(env):
    """CLI upgrade-prepare 端到端：死 pid/端口被生产探针确认退出→四步完成，
    journal/备份落盘；随后 upgrade-detect→half、finalize→clean。"""
    _write_dead_instance(env)
    proc, payload = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
    )
    assert proc.returncode == 0, proc.stderr
    assert payload is not None and payload["ok"] is True, proc.stdout
    assert payload["kind"] == "prepared"
    assert payload["steps_done"] == [
        "1_quiesce", "2_old_helper_exit", "3_backup", "4_journal",
    ]
    # 陈旧 instance 已被生产退出确认路径清理；journal 与备份在位。
    assert not env.instance_path.exists()
    assert env.journal_path.is_file()
    assert Path(payload["backup_path"]).is_file()
    assert env.snapshot_roots() == env.expected_roots()

    proc, payload = _run_cli(env.runtime, "upgrade-detect")
    assert proc.returncode == 0 and payload["state"] == "half_upgraded"

    proc, payload = _run_cli(env.runtime, "upgrade-finalize")
    assert proc.returncode == 0 and payload["ok"] is True
    assert not env.journal_path.exists()
    proc, payload = _run_cli(env.runtime, "upgrade-detect")
    assert payload["state"] == "clean"


def test_cli_upgrade_prepare_refused_when_scan_in_flight(env):
    """CLI 拒绝路径（跨进程真实 flock）：在途扫描持有租约→子进程明确拒绝，
    零副作用（helper/数据/journal 不动）。"""
    lease = scan_coordinator.ScanLease.acquire(
        env.scan_lock_path, source="scheduled"
    )
    try:
        proc, payload = _run_cli(
            env.runtime, "upgrade-prepare",
            "--from", env.n_version, "--to", env.n_plus_1_version,
        )
    finally:
        lease.release()
    assert proc.returncode == 1
    assert payload is not None and payload["ok"] is False
    assert payload["kind"] == "scan_busy"
    assert "重试" in payload["error"]
    assert not env.journal_path.exists()
    assert list(env.data_dir.glob("*.backup-v*")) == []
    assert env.pid_alive(int(env.read_instance()["pid"]))
    assert env.snapshot_roots() == env.expected_roots()


def test_cli_upgrade_prepare_schema_refused_untouched_bytes(tmp_path):
    """CLI schema 拒绝：较新 schema 库→prepare 在任何停写/备份前拒绝，
    原库字节不变。"""
    fake = uf.FakeUpgradeEnv.build(
        tmp_path / "upgrade-env", db_builder=uf.write_newer_schema_db
    )
    try:
        before = uf.file_sha256(fake.db_path)
        proc, payload = _run_cli(
            fake.runtime, "upgrade-prepare",
            "--from", fake.n_version, "--to", fake.n_plus_1_version,
        )
        assert proc.returncode == 1
        assert payload is not None and payload["ok"] is False
        assert payload["kind"] == "schema_refused"
        assert not fake.journal_path.exists()
        assert list(fake.data_dir.glob("*.backup-v*")) == []
        assert uf.file_sha256(fake.db_path) == before
        # 安装与旧 helper 全程未被触碰。
        assert fake.installed_version() == __version__
        assert fake.pid_alive(int(fake.read_instance()["pid"]))
    finally:
        fake.close()


def test_cli_upgrade_detect_rollback_cycle(env):
    """CLI upgrade-rollback：从半升级态恢复——journal 清除、幂等可重复。"""
    _write_dead_instance(env)
    proc, payload = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
    )
    assert payload["ok"] is True
    proc, payload = _run_cli(env.runtime, "upgrade-rollback")
    assert proc.returncode == 0 and payload["ok"] is True
    assert "removed_journal" in payload["actions"]
    assert "helper_restart_deferred_to_shell" in payload["actions"]
    assert not env.journal_path.exists()
    proc, payload = _run_cli(env.runtime, "upgrade-detect")
    assert payload["state"] == "clean"
    # 幂等：clean 态再次 rollback 不报错。
    proc, payload = _run_cli(env.runtime, "upgrade-rollback")
    assert proc.returncode == 0 and payload["ok"] is True


# ==================================================== 壳接线合同（Python 钉子）
def _librs_code_without_line_comments() -> str:
    lines = LIB_RS.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("//"))


def test_librs_wires_production_upgrade_contract():
    """lib.rs 壳接线合同：真实调用冻结 helper 的 upgrade-* 子命令（非仅
    Python 层 fake）；取消走已授权事件通道（不新增 ACL 权限）；downloading
    事件含 downloaded/total；安装不可取消文案明确；updater 命令面恰三条。"""
    assert LIB_RS.is_file(), "lib.rs 必须在位"
    code = _librs_code_without_line_comments()
    # 壳侧真实调用三个协调子命令（argv 字面量，与 fathom.cli 定义一致）。
    for sub in ("upgrade-prepare", "upgrade-finalize", "upgrade-rollback"):
        assert f'"{sub}"' in code, f"lib.rs 未接线 {sub}（生产入口未被壳调用）"
    # 取消通道：复用 core:event:default 事件，不新增命令/ACL 权限。
    assert '"updater-cancel-requested"' in code
    # 进度事件合同：downloading 态含 downloaded/total。
    assert '"downloading"' in code
    assert '"downloaded"' in code and '"total"' in code
    # 安装阶段不可取消文案明确。
    assert "不可取消" in code
    # updater 命令面仍恰三条（恰 3 权限 ACL 合同不因本切片回退）。
    block = re.search(r"generate_handler!\[(.*?)\]", code, re.DOTALL)
    assert block, "generate_handler 接线块必须在位"
    wired = set(re.findall(r"\bupdater_[a-z_]+\b", block.group(1)))
    assert wired == {"updater_check", "updater_install", "updater_restart"}
