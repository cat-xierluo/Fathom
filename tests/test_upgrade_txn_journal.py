"""ISS-097 · 升级事务持续停写、重入拒绝与 journal 所有权聚焦验。

合同来源：docs/TASKS.md 的 ISS-097 卡（唯一父卡 ISS-040）。与
ISS-040C（``tests/test_upgrade_production_wiring.py``）的分工：本文件钉
ISS-097 三条新不变量——

- **持续停写**：从 prepare 成功（journal=prepared）到成功 finalize 或显式
  rollback 之间，另一进程的扫描写入（``start_scan`` 生产入口，覆盖
  API/CLI/定时三来源）被 ``UpgradeWriteStopError`` 拒绝且零副作用（不建
  scan_runs 记录）；事务结束后恢复可写。判据是 journal **存在性**
  （损坏同样停写，fail-closed）。
- **journal 所有权**：prepare 重入被 ``half_upgraded_state`` 拒绝时，在位
  journal 的内容与 ``txn_id`` 字节级不变（自动回滚只清自己的 journal，
  ``left_foreign_journal`` 保守留存）；损坏/旧格式 journal 同样只有显式
  ``upgrade-rollback``（唯一恢复入口）能清。
- **重入拒绝（壳侧）**：lib.rs ``updater_install`` 经后端独占门
  （``try_begin_install`` CAS + Drop 守卫）保证重复 invoke 至多启动一个
  安装事务，不依赖前端按钮禁用；升级子命令等待有界
  （``UPGRADE_PHASE_TIMEOUT_S``，超时只终止自己 spawn 的子进程）。

环境隔离：030A 夹具搭 fake 升级环境，**同一运行根**同时作为扫描侧
config 指向（停写门经 ``config.DB_PATH`` 推导 journal 路径，与夹具
journal 同源）；扫描用合成根 + 假 du + 子进程内拦通知（绝不真弹横幅、
零生产触碰、零真实网络）。CLI 侧经 ``python -m fathom`` 子进程真实跨
进程验证。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from fathom import SERVICE_IDENTITY, __protocol_version__, api, config, db, notify, scan_coordinator, scanner

import upgrade_fixture as uf

from fathom import upgrade

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
LIB_RS = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "src" / "lib.rs"

# 死 pid（macOS pid 上限 99998）与永不被监听的回环端口：CLI 子进程里的
# 生产默认探针据此确定性确认「已退出、端口已释放」（与 production_wiring
# 同款手法，零真实信号、零真实网络）。
DEAD_PID = 4_000_000
DEAD_PORT = 1

# CLI scan 子进程载体：先拦掉真实通知横幅再进生产入口 cli.main（沿用
# test_scan_coordination.py 的 test_cli_sigterm 同款模式）。
_SCAN_CLI_CODE = """
import sys
from fathom import cli, notify
notify.send_notification = lambda *a, **kw: True
raise SystemExit(cli.main(['--runtime-dir', sys.argv[1], '--scan-root',
                           sys.argv[2], 'scan']))
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """统一隔离环境：030A 夹具运行根 + 扫描侧 config 指向同一根 + 合成扫描根。

    停写门与升级协调器因此共享同一 journal/锁路径（生产同构）。du 用假
    实现、通知全拦截；结束关闭夹具持有的 DB 连接。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    scan_root = tmp_path / "scan-root"
    scan_root.mkdir()
    (scan_root / "a.txt").write_text("a", encoding="utf-8")
    monkeypatch.setattr(config, "DB_PATH", fake.db_path)
    monkeypatch.setattr(config, "DATA_DIR", fake.data_dir)
    monkeypatch.setattr(config, "REPORTS_DIR", fake.reports_dir)
    monkeypatch.setattr(config, "LOGS_DIR", fake.logs_dir)
    monkeypatch.setattr(config, "DEFAULT_ROOT", scan_root)
    monkeypatch.setattr(notify, "send_notification", lambda *a, **kw: True)

    def _fake_du(target):
        return scanner.DuResult({str(target): 0}, 0, 0, 0.01)

    monkeypatch.setattr(scanner, "run_du", _fake_du)
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
    hooks = {
        "pid_alive": env.pid_alive,
        "port_open": lambda port: False,
        "request_helper_exit": lambda pid: env.stop_helper(pid),
        "start_helper": lambda: env.start_helper(env.n_version),
        "restore_old_version": lambda: None,
    }
    hooks.update(overrides)
    return hooks


def _coordinator(env, **hook_overrides) -> upgrade.UpgradeCoordinator:
    return upgrade.UpgradeCoordinator(
        _paths(env),
        from_version=env.n_version,
        to_version=env.n_plus_1_version,
        hooks=_hooks(env, **hook_overrides),
    )


def _cli_env() -> dict[str, str]:
    value = os.environ.copy()
    value["PYTHONPATH"] = str(REPO_ROOT)
    value.pop("FATHOM_RUNTIME_DIR", None)
    value.pop("FATHOM_RUNTIME_MODE", None)
    return value


def _run_cli(runtime: Path, *args: str, timeout: float = 120
             ) -> tuple[int, dict | None, str]:
    """经生产 CLI 入口子进程执行（与冻结 helper 同一入口 fathom/__main__.py）。"""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "fathom" / "__main__.py"),
         "--runtime-dir", str(runtime), *args],
        capture_output=True, text=True, env=_cli_env(), timeout=timeout,
        cwd=str(REPO_ROOT),
    )
    payload = None
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if lines:
        try:
            payload = json.loads(lines[-1])
        except ValueError:
            payload = None
    return proc.returncode, payload, proc.stderr


def _run_scan_cli(runtime: Path, scan_root: Path, timeout: float = 120
                  ) -> subprocess.CompletedProcess:
    """经 CLI scan 生产入口跑子进程（通知在子进程内拦截；真实 du 跑合成根）。"""
    return subprocess.run(
        [str(PYTHON), "-c", _SCAN_CLI_CODE, str(runtime), str(scan_root)],
        capture_output=True, text=True, env=_cli_env(), timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def _write_dead_instance(env) -> None:
    instance = {
        "pid": DEAD_PID, "port": DEAD_PORT,
        "service": SERVICE_IDENTITY,
        "protocol_version": __protocol_version__,
        "version": env.n_version,
        "instance_id": "iss097-test", "runtime_mode": "release",
    }
    uf._write_0600(
        env.instance_path,
        json.dumps(instance, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def _scan_runs_count() -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    finally:
        conn.close()


def _wait_scan_done(timeout: float = 8) -> None:
    deadline = __import__("time").monotonic() + timeout
    state: dict = {}
    while __import__("time").monotonic() < deadline:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is not None and row["status"] == "done":
            return
        __import__("time").sleep(0.03)
    raise AssertionError(f"未等到扫描 done：{state or row}")


# ==================================================== 反例 1：持续停写
def test_scan_writes_blocked_while_journal_present_after_prepare(env, tmp_path):
    """prepare 成功（half_upgraded journal 在位）后，另一进程的开始写入被拒：
    UpgradeWriteStopError、不建 scan_runs 记录、journal 字节不动；成功
    finalize 后恢复可写（真实完成一次扫描）。"""
    coord = _coordinator(env)
    prepared = coord.run_prepare()
    assert prepared["ok"], prepared
    assert upgrade.detect_upgrade_state(_paths(env)) == "half_upgraded"
    journal_before = env.journal_path.read_bytes()

    runs_before = _scan_runs_count()
    with pytest.raises(scan_coordinator.UpgradeWriteStopError) as excinfo:
        scan_coordinator.start_scan(source="cli")
    assert "升级事务进行中" in str(excinfo.value)
    # 诊断面：journal 的 phase/txn_id 只读附带（拒绝方不读改删 journal 本体）。
    assert excinfo.value.journal.get("phase") == "prepared"
    assert excinfo.value.journal.get("txn_id") == coord.txn_id
    assert env.journal_path.read_bytes() == journal_before
    # 零副作用：没有新的运行记录（拒绝发生在建 running 行之前）。
    assert _scan_runs_count() == runs_before

    # 成功收尾（owner 侧 finalize）→ journal 清除 → 恢复可写：真实跑完一次扫描。
    assert coord.finalize()["ok"]
    assert upgrade.detect_upgrade_state(_paths(env)) == "clean"
    _run_id, result = scan_coordinator.run_scan(source="cli")
    assert result["snapshot_id"] is not None


def test_scan_writes_blocked_during_install_and_verify_phases(env):
    """⑤安装/⑥核验期间同样停写：install 与身份核验回调里真实尝试
    start_scan 均被拒（记录后不中断事务）；事务成功结束后写入恢复。"""
    refused: dict[str, list[str]] = {"installing": [], "installed": []}

    def _try_write(phase_key: str) -> None:
        try:
            scan_coordinator.start_scan(source="api")
        except scan_coordinator.UpgradeWriteStopError as exc:
            refused[phase_key].append(str(exc.journal.get("txn_id")))
        else:
            raise AssertionError(f"{phase_key} 期间 start_scan 竟然成功——停写条件缺口")

    def _install(_ctx: dict) -> None:
        _try_write("installing")

    def _identity() -> dict:
        _try_write("installed")
        return {
            "service": SERVICE_IDENTITY,
            "protocol_version": __protocol_version__,
            "version": env.n_plus_1_version,
        }

    coord = _coordinator(env)
    result = coord.run_full(install=_install, new_helper_identity=_identity)
    assert result["ok"], result
    # 下载→安装→核验全程 journal 在位（停写凭据），两阶段拒绝都发生且带诊断。
    assert refused["installing"] == [coord.txn_id]
    assert refused["installed"] == [coord.txn_id]
    # 事务结束后恢复可写。
    _run_id, scan_result = scan_coordinator.run_scan(source="cli")
    assert scan_result["snapshot_id"] is not None


def test_api_scan_returns_409_during_upgrade_txn(env, monkeypatch):
    """API 入口停写：journal 在位时 POST /api/scan → 409 + 升级文案
    （区别于扫描进行中），不建运行记录、journal 不动；恢复后 200 且
    扫描真实完成。"""
    coord = _coordinator(env)
    assert coord.run_prepare()["ok"]
    journal_before = env.journal_path.read_bytes()
    runs_before = _scan_runs_count()

    def _fresh_client() -> TestClient:
        monkeypatch.setattr(api, "_active_scan", None)
        monkeypatch.setattr(api, "_active_scan_thread", None)
        monkeypatch.setattr(api, "_scan_lock", threading.Lock())
        client = TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}")
        client.headers["X-Fathom-Token"] = client.get("/api/bootstrap").json()["token"]
        return client

    with _fresh_client() as client:
        response = client.post("/api/scan")
        assert response.status_code == 409
        body = response.json()
        assert "升级事务进行中" in body["message"]
        assert body["upgrade"]["phase"] == "prepared"
        assert _scan_runs_count() == runs_before
    assert env.journal_path.read_bytes() == journal_before

    # 恢复（显式回滚入口）后 API 可再次启动扫描，并等到真实完成。
    assert coord.rollback(reason="test", own_journal_only=False)["ok"]
    with _fresh_client() as client:
        assert client.post("/api/scan").status_code == 200
    _wait_scan_done()


def test_cli_scan_exit_3_during_upgrade_txn(env, tmp_path):
    """CLI/定时入口停写：journal 在位时 ``fathom scan`` 子进程退出码 3
    （区别于 scan_busy 的 2）、文案明确、零副作用；rollback 后退出码 0。"""
    scan_root = tmp_path / "scan-root"
    _write_dead_instance(env)
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
    )
    assert code == 0 and payload["ok"], (code, payload, err)
    runs_before = _scan_runs_count()

    proc = _run_scan_cli(env.runtime, scan_root)
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert "升级事务进行中" in proc.stderr
    assert _scan_runs_count() == runs_before  # 拒绝先于任何写入

    code, payload, err = _run_cli(env.runtime, "upgrade-rollback")
    assert code == 0 and payload["ok"], (code, payload, err)
    (scan_root / "a.txt").write_text("a", encoding="utf-8")
    proc = _run_scan_cli(env.runtime, scan_root)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


# ==================================================== 反例 2：journal 所有权
def test_prepare_reentry_refusal_preserves_journal_bytes_and_txn(env):
    """已有 journal 导致拒绝时：内容/事务 ID 字节级不变；自动回滚只记
    left_foreign_journal（不 removed_journal、不恢复旧版、不重启 helper）。"""
    coord = _coordinator(env)
    prepared = coord.run_prepare()
    assert prepared["ok"], prepared
    journal_before = env.journal_path.read_bytes()
    assert json.loads(journal_before)["txn_id"] == coord.txn_id

    second = _coordinator(env)
    refused = second.run_prepare()
    assert refused["ok"] is False
    assert refused["kind"] == "half_upgraded_state"
    assert "upgrade-rollback" in refused["error"]
    # 所有权：拒绝方的自动回滚不动别人的 journal。
    assert "left_foreign_journal" in refused["rollback_actions"]
    assert "removed_journal" not in refused["rollback_actions"]
    assert "restored_old_version" not in refused["rollback_actions"]
    assert "restarted_old_helper" not in refused["rollback_actions"]
    assert env.journal_path.read_bytes() == journal_before
    assert upgrade.detect_upgrade_state(_paths(env)) == "half_upgraded"


def test_txn_id_present_and_unique_per_attempt(env):
    """事务 ID 合同：journal 落盘携带 txn_id/owner_pid；两次独立尝试的
    txn_id 不同；结果 JSON 的 txn_id 与 journal 一致。"""
    first = _coordinator(env)
    assert first.run_prepare()["ok"]
    journal_1 = upgrade.read_journal(_paths(env))
    assert journal_1["txn_id"] == first.txn_id
    assert journal_1["owner_pid"] == os.getpid()
    # 显式恢复入口清除后可开始新事务，txn_id 全新。
    assert first.rollback(reason="test", own_journal_only=False)["ok"]
    second = _coordinator(env)
    assert second.run_prepare()["ok"]
    journal_2 = upgrade.read_journal(_paths(env))
    assert journal_2["txn_id"] == second.txn_id
    assert journal_2["txn_id"] != journal_1["txn_id"]


def test_cli_prepare_reentry_and_detect_rollback_cycle(env):
    """跨进程重入链（真实 CLI 子进程）：prepare 成功 → 重入被拒且 journal
    字节不变 → upgrade-detect 附 txn_id/phase 诊断 → 显式 upgrade-rollback
    清除 → 新事务可开始。"""
    _write_dead_instance(env)
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
    )
    assert code == 0 and payload["ok"], (code, payload, err)
    txn_id = payload["txn_id"]
    journal_before = env.journal_path.read_bytes()

    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
    )
    assert code == 1
    assert payload["ok"] is False and payload["kind"] == "half_upgraded_state"
    assert "left_foreign_journal" in payload["rollback_actions"]
    assert env.journal_path.read_bytes() == journal_before  # 字节不变

    code, payload, err = _run_cli(env.runtime, "upgrade-detect")
    assert code == 0 and payload["state"] == "half_upgraded"
    assert payload["journal"]["txn_id"] == txn_id
    assert payload["journal"]["phase"] == "prepared"

    code, payload, err = _run_cli(env.runtime, "upgrade-rollback")
    assert code == 0 and payload["ok"] and "removed_journal" in payload["actions"]
    assert not env.journal_path.exists()

    # 恢复后新事务可开始，且 txn_id 不同。
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
    )
    assert code == 0 and payload["ok"]
    assert payload["txn_id"] != txn_id


def test_corrupt_journal_fail_closed_with_explicit_recovery_only(env):
    """损坏 journal：停写条件与 prepare 都保守拒绝（fail-closed，不解析
    内容不猜状态）；自动回滚留存；仅显式 rollback 能清，随后一切恢复。"""
    env.journal_path.write_bytes(b"{not-json")

    with pytest.raises(scan_coordinator.UpgradeWriteStopError) as excinfo:
        scan_coordinator.start_scan(source="cli")
    assert excinfo.value.journal == {}  # 损坏：无诊断可附，但仍拒绝

    refused = _coordinator(env).run_prepare()
    assert refused["ok"] is False
    assert refused["kind"] == "half_upgraded_state"
    assert "不可解析" in refused["error"]
    assert "left_foreign_journal" in refused["rollback_actions"]
    assert env.journal_path.read_bytes() == b"{not-json"

    code, payload, err = _run_cli(env.runtime, "upgrade-detect")
    assert code == 0 and payload["state"] == "half_upgraded"
    assert payload["journal"] == {"parse_error": True}

    code, payload, err = _run_cli(env.runtime, "upgrade-rollback")
    assert code == 0 and payload["ok"] and "removed_journal" in payload["actions"]
    assert not env.journal_path.exists()
    _run_id, result = scan_coordinator.run_scan(source="cli")
    assert result["snapshot_id"] is not None


def test_legacy_journal_without_txn_id_stopped_and_only_explicit_clears(env):
    """旧格式 journal（无 txn_id，向后兼容路径）：停写生效；自动回滚按
    「未知所有权遗留事务」留存（left_foreign_journal）；显式 rollback 可恢复。"""
    upgrade.write_journal(_paths(env), {
        "service": SERVICE_IDENTITY,
        "from_version": env.n_version,
        "to_version": env.n_plus_1_version,
        "phase": "prepared",
    })
    with pytest.raises(scan_coordinator.UpgradeWriteStopError):
        scan_coordinator.start_scan(source="cli")

    refused = _coordinator(env).run_prepare()
    assert refused["ok"] is False and refused["kind"] == "half_upgraded_state"
    assert "left_foreign_journal" in refused["rollback_actions"]
    assert upgrade.read_journal(_paths(env)).get("txn_id") is None  # 原样留存

    coord = _coordinator(env)
    recovered = coord.rollback(reason="test", own_journal_only=False)
    assert recovered["ok"] and "removed_journal" in recovered["actions"]
    assert not env.journal_path.exists()
    _run_id, result = scan_coordinator.run_scan(source="cli")
    assert result["snapshot_id"] is not None


# ==================================================== 中断/崩溃恢复路径
def test_interrupted_install_txn_detectable_then_recoverable(env):
    """各阶段中断（模拟壳在⑤安装中崩溃：journal=installing 残留）：停写
    持续、新升级被拒；经 upgrade-detect 检测 + 显式 upgrade-rollback 恢复
    后，写入与新事务都恢复正常。"""
    coord = _coordinator(env)
    assert coord.run_prepare()["ok"]
    # 模拟：壳在安装阶段死亡（无人回滚），journal 留在 installing。
    upgrade.write_journal(_paths(env), {
        "service": SERVICE_IDENTITY,
        "txn_id": coord.txn_id,
        "owner_pid": os.getpid(),
        "from_version": coord.from_version,
        "to_version": coord.to_version,
        "phase": "installing",
        "steps_done": ["1_quiesce", "2_old_helper_exit", "3_backup", "4_journal"],
        "backup_path": str(coord.backup_path) if coord.backup_path else None,
        "updated_at": "2026-09-26T00:00:00",
    })
    coord.dispose()  # 释放本进程持有的资源（journal 留在原地）

    with pytest.raises(scan_coordinator.UpgradeWriteStopError):
        scan_coordinator.start_scan(source="cli")
    refused = _coordinator(env).run_prepare()
    assert refused["kind"] == "half_upgraded_state"

    code, payload, err = _run_cli(env.runtime, "upgrade-detect")
    assert code == 0
    assert payload["state"] == "half_upgraded"
    assert payload["journal"]["phase"] == "installing"
    code, payload, err = _run_cli(env.runtime, "upgrade-rollback")
    assert code == 0 and payload["ok"] and "removed_journal" in payload["actions"]

    _run_id, result = scan_coordinator.run_scan(source="cli")
    assert result["snapshot_id"] is not None
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
    )
    assert code == 0 and payload["ok"]  # 新事务可开始


def test_prepare_failure_auto_rollback_clears_own_journal_only(env, monkeypatch):
    """自动回滚对自己的 journal 语义不变（ISS-040C 不回归）：失败→清自己的
    journal→clean；与「别人的 journal 不动」互为对照。"""
    def _broken_backup(_path):
        raise sqlite3.OperationalError("injected: 备份失败")

    monkeypatch.setattr(db, "consistent_backup", _broken_backup)
    coord = _coordinator(env)
    result = coord.run_prepare()
    assert result["ok"] is False and result["kind"] == "backup"
    assert "removed_journal" in result["rollback_actions"]
    assert not env.journal_path.exists()
    assert upgrade.detect_upgrade_state(_paths(env)) == "clean"


# ==================================================== 壳接线合同（Python 钉子）
def _librs_code_without_line_comments() -> str:
    lines = LIB_RS.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("//"))


def test_librs_updater_install_exclusive_gate_and_bounded_wait_wired():
    """lib.rs 壳接线合同（ISS-097）：updater_install 经后端独占门进入安装
    事务本体（不依赖前端按钮禁用）；busy 拒绝有明确文案；升级子命令与
    身份核验走有界等待（超时只终止自己 spawn 的子进程）；updater 命令面
    仍恰三条（恰 3 权限 ACL 合同不回退）。"""
    assert LIB_RS.is_file()
    code = _librs_code_without_line_comments()
    # 后端独占门：CAS 抢占 + RAII 守卫 + 事务本体拆分。
    assert "try_begin_install" in code, "updater_install 必须经后端独占门"
    assert "InstallActiveGuard" in code, "独占门必须经 Drop 守卫在任何退出路径释放"
    assert "updater_install_transaction" in code, "安装事务本体必须只经独占门进入"
    assert '"state": "busy"' in code and "独占门" in code, "busy 拒绝必须有明确文案"
    assert "install_active" in code
    # 有界等待：不无限挂起，超时只终止自己 spawn 的子进程。
    assert "UPGRADE_PHASE_TIMEOUT_S" in code and "wait_child_bounded" in code
    assert "child.kill()" in code, "超时路径必须只 kill 本次自己 spawn 的子进程"
    # updater 命令面仍恰三条。
    block = re.search(r"generate_handler!\[(.*?)\]", code, re.DOTALL)
    assert block, "generate_handler 接线块必须在位"
    wired = set(re.findall(r"\bupdater_[a-z_]+\b", block.group(1)))
    assert wired == {"updater_check", "updater_install", "updater_restart"}
