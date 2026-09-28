"""ISS-110 · 外部同服务实例环境下 prepare 的前置拒绝合同（聚焦测试）。

反例（用户实机，ISS-110 卡）：0.3.4 桌面壳 + launchd 常驻服务（同服务、
占服务端口）。点「确认下载安装」后 prepare 对外部常驻服务的实例记录不
做所有权协调——把外部服务 pid 当旧 helper 处理（发退出请求），或在退出
确认超时后于 journal 落盘之后才中止；升级入口对用户静默。

合同（本卡修订）：壳的生产调用形态总是声明本壳 helper pid
（``--expected-helper-pid``）；prepare 在**写 journal 之前**显式检测外部
同服务实例（instance 记录身份匹配的存活实例、pid 非本壳 helper 时），
以结构化错误（kind=external_service_instance）明确拒绝并给手动安装指引
——零信号、零副作用、零 journal。未声明期望 pid 的旧调用方（直接 CLI
诊断/既有测试）不做该检测，保持既有②语义（兼容合同）。

覆盖（全部隔离：tmp 运行根、假进程标记、注入探针、独立 sleep 子进程；
零生产触碰、零真实信号发给非自有进程）：

- 现状缺陷实证（未声明期望 pid 的兼容形态）：prepare 会把外部实例 pid
  当旧 helper 请求退出——本测试钉住兼容行为，作为反例证据与新合同的
  对照基线。
- 壳生产形态 A（expected=本壳已让位的死 pid）：外部实例在位 → journal
  之前明确拒绝，零信号、零痕迹、instance 原样、停写租约可再取。
- 壳生产形态 B（expected=0，本壳无 helper）：外部实例在位 → 同样拒绝。
- 本壳 helper 在跑（expected == instance pid）→ 放行，四步完成。
- 壳无 helper（expected=0）+ instance 为陈旧死 pid → 放行（由②既有
  分支清理，不发信号）。
- instance 损坏 → 前置检测不拦截（既有 internal 拒绝分支负责）。
- 生产 CLI 入口：--expected-helper-pid 经 fathom/__main__.py 传透，
  外部实例（真实存活子进程）在位时 exit 1 + kind=external_service_instance。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from fathom import SERVICE_IDENTITY, __protocol_version__, upgrade

import upgrade_fixture as uf

REPO_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------ 隔离环境工具
def _paths(env) -> upgrade.UpgradePaths:
    return upgrade.UpgradePaths(
        runtime_dir=env.runtime,
        db_path=env.db_path,
        lock_path=env.scan_lock_path,
        instance_path=env.instance_path,
        journal_path=env.journal_path,
    )


def _coordinator(env, *, expected_helper_pid=None, **overrides):
    """默认接 fake 环境模型的生产可注入动作（与 helper_exit 同款）。"""
    hooks = {
        "pid_alive": env.pid_alive,
        "port_open": lambda port: False,
        "request_helper_exit": lambda pid: env.stop_helper(pid),
        "start_helper": lambda: env.start_helper(env.n_version),
    }
    hooks.update(overrides.pop("hooks", {}))
    kwargs = dict(
        paths=_paths(env),
        from_version=env.n_version,
        to_version=env.n_plus_1_version,
        hooks=hooks,
        expected_helper_pid=expected_helper_pid,
    )
    kwargs.update(overrides)
    return upgrade.UpgradeCoordinator(**kwargs)


def _lease_acquirable(env) -> bool:
    """停写租约已释放：新扫描会话可立即取得。"""
    from fathom import scan_coordinator

    lease = scan_coordinator.ScanLease.acquire(env.scan_lock_path, source="test")
    lease.release()
    return True


def _write_instance(env, pid: int, port: int = 7952) -> None:
    """以本服务身份写 instance（模拟外部常驻 serve 的落盘形态）。"""
    uf._write_0600(
        env.instance_path,
        json.dumps({
            "pid": pid,
            "port": port,
            "service": SERVICE_IDENTITY,
            "protocol_version": __protocol_version__,
            "version": env.n_version,
            "instance_id": "external-launchd-serve",
            "runtime_mode": "release",
        }, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def _run_cli(runtime: Path, *args: str):
    """经生产 CLI 入口 fathom/__main__.py 子进程执行（与冻结 helper 同一入口）。"""
    env_vars = os.environ.copy()
    env_vars["PYTHONPATH"] = str(REPO_ROOT)
    env_vars.pop("FATHOM_RUNTIME_DIR", None)
    env_vars.pop("FATHOM_RUNTIME_MODE", None)
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "fathom" / "__main__.py"),
         "--runtime-dir", str(runtime), *args],
        capture_output=True, text=True, env=env_vars, timeout=120,
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


# ================================================ 现状缺陷实证（兼容形态对照）
def test_prepare_without_expected_pid_keeps_legacy_handling(tmp_path):
    """未声明期望实例（expected_helper_pid=None：旧调用方/直接 CLI）：
    不做外部实例检测，既有②语义照旧——会向 instance 记录的 pid 请求退出。
    本测试钉住兼容面，同时是反例对照：外部常驻服务的 pid 在该形态下会被
    当旧 helper 处理（这正是 ISS-110 修订要消灭的生产形态——壳自 ISS-110
    起总是传 --expected-helper-pid，检测由此生效）。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        external_pid = fake.start_helper(fake.n_version)  # 覆写 instance
        requests: list[int] = []

        def _record_and_stop(pid: int) -> None:
            requests.append(pid)
            fake.stop_helper(pid)

        coord = _coordinator(
            fake,
            hooks={
                "pid_alive": fake.pid_alive,
                "port_open": lambda port: False,
                "request_helper_exit": _record_and_stop,
            },
        )
        result = coord.run_prepare()
        assert result["ok"] is True
        assert requests == [external_pid]
    finally:
        fake.close()


# ==================================================== 新合同：journal 前明确拒绝
def test_prepare_refuses_external_instance_before_journal_with_dead_expected_pid(tmp_path):
    """壳生产形态 A（本壳 helper 已让位退出，expected=死 pid）：外部同服务
    实例在位 → 在写 journal 之前明确拒绝（kind=external_service_instance，
    文案带手动 DMG 安装指引）——零信号、零备份、零 journal、instance 原样、
    停写租约可再取。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        # 本壳 helper：先拉起（写 instance）再让位退出（marker 移除 = 死）。
        shell_pid = fake.start_helper(fake.n_version)
        fake.stop_helper(shell_pid)
        # 外部常驻服务随后占端口并覆写 instance（launchd serve 落盘形态）。
        external_pid = fake.start_helper(fake.n_version)
        requests: list[int] = []
        coord = _coordinator(
            fake,
            expected_helper_pid=shell_pid,
            hooks={
                "pid_alive": fake.pid_alive,
                "port_open": lambda port: False,
                "request_helper_exit": requests.append,
            },
        )
        result = coord.run_prepare()
        assert result["ok"] is False
        assert result["kind"] == "external_service_instance"
        assert "手动" in result["error"] and "DMG" in result["error"], result["error"]
        assert str(external_pid) in result["error"]
        # 零信号：外部常驻服务绝未被请求退出。
        assert requests == []
        # 零痕迹：journal 未写、无备份、外部 instance 原样。
        assert not fake.journal_path.exists()
        assert list(fake.data_dir.glob("*.backup-v*")) == []
        assert fake.read_instance()["pid"] == external_pid
        assert _lease_acquirable(fake)
    finally:
        fake.close()


def test_prepare_refuses_external_instance_when_shell_has_no_helper(tmp_path):
    """壳生产形态 B（本壳无 helper，expected=0）：任何身份匹配的存活实例
    都不是本壳 helper → 在写 journal 之前明确拒绝，零信号、零 journal。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        external_pid = fake.start_helper(fake.n_version)
        requests: list[int] = []
        coord = _coordinator(
            fake,
            expected_helper_pid=0,
            hooks={
                "pid_alive": fake.pid_alive,
                "port_open": lambda port: False,
                "request_helper_exit": requests.append,
            },
        )
        result = coord.run_prepare()
        assert result["ok"] is False
        assert result["kind"] == "external_service_instance"
        assert requests == []
        assert not fake.journal_path.exists()
        assert fake.read_instance()["pid"] == external_pid
    finally:
        fake.close()


# ==================================================== 放行分支（不误伤正常路径）
def test_prepare_allows_own_helper_and_completes_four_steps(tmp_path):
    """本壳 helper 在跑（expected == instance pid）→ 放行：四步照常完成
    （不误伤 0.3.5+ 正常自更新路径）。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        shell_pid = fake.start_helper(fake.n_version)
        requests: list[int] = []

        def _record_and_stop(pid: int) -> None:
            requests.append(pid)
            fake.stop_helper(pid)

        coord = _coordinator(
            fake,
            expected_helper_pid=shell_pid,
            hooks={
                "pid_alive": fake.pid_alive,
                "port_open": lambda port: False,
                "request_helper_exit": _record_and_stop,
            },
        )
        result = coord.run_prepare()
        assert result["ok"] is True, result
        assert result["kind"] == "prepared"
        assert result["steps_done"] == [
            "1_quiesce", "2_old_helper_exit", "3_backup", "4_journal",
        ]
        assert requests == [shell_pid]
    finally:
        fake.close()


def test_prepare_allows_stale_instance_when_expected_pid_absent(tmp_path):
    """壳无 helper（expected=0）+ instance 是陈旧死 pid → 前置检测放行
    （死实例不是外部占用），由②既有分支处理：不发信号、四步完成。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        stale_pid = fake.start_helper(fake.n_version)
        fake.stop_helper(stale_pid)  # 陈旧：记录在、进程已死
        requests: list[int] = []
        coord = _coordinator(
            fake,
            expected_helper_pid=0,
            hooks={
                "pid_alive": fake.pid_alive,
                "port_open": lambda port: False,
                "request_helper_exit": requests.append,
            },
        )
        result = coord.run_prepare()
        assert result["ok"] is True, result
        assert result["kind"] == "prepared"
        assert requests == []
    finally:
        fake.close()


def test_prepare_keeps_corrupted_instance_semantics_with_expected_pid(tmp_path):
    """instance 损坏 + expected 声明在位：前置检测不拦截（损坏不等于外部
    占用），既有 internal 拒绝分支负责——语义与既有测试一致。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        uf._write_0600(fake.instance_path, b"{not-json")
        coord = _coordinator(fake, expected_helper_pid=0)
        result = coord.run_prepare()
        assert result["ok"] is False
        assert result["kind"] == "internal"
        assert result["steps_done"] == ["1_quiesce"]
        assert not fake.journal_path.exists()
    finally:
        fake.close()


# ==================================================== 生产 CLI 入口（真实探针）
def test_cli_prepare_refuses_external_instance_with_expected_pid_flag(tmp_path):
    """生产 CLI：--expected-helper-pid 传透——外部实例（真实存活子进程，
    生产 ps 探针确认存活）在位时 exit 1 + kind=external_service_instance，
    journal 零痕迹。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert sleeper.pid > 0
        _write_instance(fake, sleeper.pid)
        proc, payload = _run_cli(
            fake.runtime, "upgrade-prepare",
            "--from", fake.n_version, "--to", fake.n_plus_1_version,
            "--expected-helper-pid", "0",
        )
        assert proc.returncode == 1, proc.stdout
        assert payload is not None, proc.stdout
        assert payload["ok"] is False
        assert payload["kind"] == "external_service_instance"
        assert str(sleeper.pid) in payload["error"]
        assert not fake.journal_path.exists()
        assert json.loads(fake.instance_path.read_text(encoding="utf-8"))["pid"] == sleeper.pid
    finally:
        sleeper.terminate()
        try:
            sleeper.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sleeper.kill()
            sleeper.wait(timeout=5)
        fake.close()


def test_cli_prepare_without_expected_pid_flag_keeps_legacy_behavior(tmp_path):
    """生产 CLI 兼容面：不带 --expected-helper-pid（旧调用形态）→ 不检测，
    既有②语义照旧（死 pid + 生产探针确认退出 → 四步完成）。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        _write_instance(fake, 4_000_000, port=1)  # 死 pid + 死端口（生产探针可确认）
        proc, payload = _run_cli(
            fake.runtime, "upgrade-prepare",
            "--from", fake.n_version, "--to", fake.n_plus_1_version,
        )
        assert proc.returncode == 0, proc.stderr
        assert payload["ok"] is True
        assert payload["kind"] == "prepared"
    finally:
        fake.close()
