"""ISS-096 · 升级准备兼容 helper 正常退出后的实例文件清理（聚焦退出测试）。

合同来源：docs/TASKS.md ISS-096 卡。修正 ISS-040C 已合并实现的真实退出
反例：旧 helper 优雅退出并自清 ``helper-instance.json`` 后，
``upgrade.read_instance`` 未按其 docstring 合同把「缺失」返回 None，而是抛
``FileNotFoundError`` → ``run_prepare`` 以 ``kind=internal`` 失败退出。

覆盖（全部隔离：tmp 运行根、合成扫描根、随机回环端口；零生产触碰）：

- **起始无实例**（生产入口 CLI）：instance 文件从不存在 → prepare 视为
  无旧 helper 记录，四步照常完成、一致备份可读。
- **退出时自清**（真实 serve 子进程，测试自拥有、自回收）：prepare 生产
  默认对身份核验通过的旧 helper pid 发 SIGTERM → serve 自清 instance 并以
  0 退出 → 退出确认后再次读取实例不再失败，四步完成。
- **已退出自清后再 prepare**（真实 serve 子进程）：helper 先行优雅退出并
  自清，prepare 起始即无实例文件，同样完成四步。
- **损坏实例 JSON**：仍保守拒绝（kind=internal），不误宣告成功、不误清理
  未知状态。
- **helper 未退出 / pid 已退出但端口未释放**：仍有界超时拒绝，不放宽
  退出确认；pid 判活为假时绝不发信号。
- **身份不符**：绝不向非本次进程发信号（request_helper_exit 未被调用）。

边界：真实 serve 场景以 Python 父进程回收自有子进程，只覆盖协议语义；
Tauri 壳持有子进程句柄的原生退出/回收时序（ISS-096 验收框 3）无法在
Python 测试环境取证，按任务卡标 NOT_VERIFIED，不以本文件外推。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from fathom import __version__, scan_coordinator, upgrade
from fathom import SERVICE_IDENTITY, __protocol_version__

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


def _coordinator(env, **overrides) -> upgrade.UpgradeCoordinator:
    """默认接 fake 环境模型的生产可注入动作（与 production_wiring 同款）。"""
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
    )
    kwargs.update(overrides)
    return upgrade.UpgradeCoordinator(**kwargs)


def _lease_acquirable(env) -> bool:
    """停写租约已释放：新扫描会话可立即取得。"""
    lease = scan_coordinator.ScanLease.acquire(env.scan_lock_path, source="test")
    lease.release()
    return True


def _assert_backup_readable(backup_path: str, expected_roots: list[str]) -> None:
    """一致备份可独立打开、完整性 ok、含全部已提交行（验收框 1）。"""
    backup = Path(backup_path)
    assert backup.is_file()
    conn = sqlite3.connect(backup)
    try:
        roots = sorted(str(r[0]) for r in conn.execute("SELECT root FROM snapshots"))
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert roots == sorted(expected_roots)


def _run_cli(env: dict, *args: str) -> tuple[subprocess.CompletedProcess, dict | None]:
    """经生产 CLI 入口 fathom/__main__.py 子进程执行（与冻结 helper 同入口）。"""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "fathom" / "__main__.py"), *args],
        capture_output=True, text=True, env=env, timeout=120, cwd=str(REPO_ROOT),
    )
    payload = None
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if lines:
        try:
            payload = json.loads(lines[-1])
        except ValueError:
            payload = None
    return proc, payload


def _fake_cli_env() -> dict[str, str]:
    """CLI 子进程环境：PYTHONPATH 指向仓库根，清掉宿主 FATHOM_*（夹具环境
    经 --runtime-dir 参数传入，等价于壳的调用形态）。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("FATHOM_RUNTIME_DIR", None)
    env.pop("FATHOM_RUNTIME_MODE", None)
    return env


# ------------------------------------------------------- 真实 serve（自有回收）
def _free_loopback_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class RealServe:
    """测试自有的真实 serve 子进程：tmp 运行根 + 合成扫描根 + 随机回环端口；
    测试结束由测试回收（terminate→wait→kill 兜底），绝不遗留进程。"""

    def __init__(self, tmp_path: Path):
        self.runtime = tmp_path / "runtime"
        self.scan_root = tmp_path / "synthetic-root"
        self.scan_root.mkdir(parents=True, exist_ok=True)
        self.port = _free_loopback_port()
        self.env = {
            k: v for k, v in os.environ.items() if not k.startswith("FATHOM_")
        }
        self.env.update(
            FATHOM_RUNTIME_DIR=str(self.runtime),
            FATHOM_SCAN_ROOT=str(self.scan_root),
            FATHOM_RESOURCE_DIR=str(REPO_ROOT),
            FATHOM_PORT=str(self.port),
            FATHOM_RUNTIME_MODE="release",
            PYTHONPATH=str(REPO_ROOT),
        )
        self.instance_path = self.runtime / "helper-instance.json"
        self.log_path = tmp_path / "serve.log"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        with self.log_path.open("w") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "fathom", "serve"],
                cwd=str(REPO_ROOT), env=self.env, stdout=log, stderr=log,
            )
        # 立即起 reaper 线程回收自有子进程：serve 退出后不留僵尸——僵尸在
        # ``ps -p`` 下仍被判活，会干扰生产探针的退出确认（与审查复现脚本
        # 同款形态；CPython Popen.wait 以 _waitpid_lock 保证并发 wait 安全）。
        self._reaper = threading.Thread(
            target=self.proc.wait, name="serve-reaper", daemon=True
        )
        self._reaper.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "隔离 serve 在健康检查前退出（code="
                    f"{self.proc.returncode}）；日志尾："
                    + self.log_path.read_text(errors="replace")[-2000:]
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/health", timeout=0.5
                ) as response:
                    if response.status == 200:
                        return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("隔离 serve 健康检查超时")

    def instance(self) -> dict:
        return json.loads(self.instance_path.read_text(encoding="utf-8"))

    def wait_self_cleaned_exit(self, timeout: float = 15.0) -> int:
        """等待优雅退出（SIGTERM 处理器自清 instance 后以 0 退出）。"""
        code = self.proc.wait(timeout=timeout)
        assert code == 0, f"serve 退出码 {code} 非 0；日志：{self.log_path.read_text(errors='replace')[-2000:]}"
        assert not self.instance_path.exists(), "serve 退出后未自清 helper-instance.json"
        return code

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


@pytest.fixture
def serve(tmp_path):
    rs = RealServe(tmp_path)
    rs.start()
    try:
        yield rs
    finally:
        rs.stop()


# ================================================== 起始无实例（生产入口 CLI）
def test_cli_prepare_succeeds_without_instance_file_from_start(tmp_path):
    """起始即无 instance 文件：prepare 视为无旧 helper 记录（不触碰任何
    进程），四步完成、一致备份可读、journal prepared（修前 kind=internal）。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        fake.instance_path.unlink()  # 起始即无实例文件
        assert not fake.instance_path.exists()
        proc, payload = _run_cli(
            _fake_cli_env(), "--runtime-dir", str(fake.runtime),
            "upgrade-prepare", "--from", fake.n_version, "--to", fake.n_plus_1_version,
        )
        assert proc.returncode == 0, proc.stderr
        assert payload is not None and payload["ok"] is True, proc.stdout
        assert payload["kind"] == "prepared"
        assert payload["steps_done"] == [
            "1_quiesce", "2_old_helper_exit", "3_backup", "4_journal",
        ]
        _assert_backup_readable(payload["backup_path"], fake.expected_roots())
        # 无实例文件时依旧不凭空造出实例；journal 在位可检测。
        assert not fake.instance_path.exists()
        assert fake.journal_path.is_file()
        assert upgrade.read_journal(_paths(fake))["phase"] == "prepared"
        assert _lease_acquirable(fake)
    finally:
        fake.close()


# ================================================== 退出时自清（真实 serve）
def test_cli_prepare_requests_exit_and_real_serve_self_cleans(serve):
    """prepare 生产默认向身份核验通过的真实 serve pid 发 SIGTERM → serve
    自清 instance 并以 0 退出 → 退出确认后的再次实例读取不再失败
    （修前此路径抛 FileNotFoundError → kind=internal）。"""
    instance = serve.instance()
    assert instance["pid"] == serve.proc.pid
    assert instance["port"] == serve.port
    proc, payload = _run_cli(
        serve.env, "upgrade-prepare",
        "--from", instance["version"], "--to", uf._next_patch_version(instance["version"]),
    )
    assert proc.returncode == 0, proc.stderr
    assert payload is not None and payload["ok"] is True, proc.stdout
    assert payload["kind"] == "prepared"
    assert payload["steps_done"] == [
        "1_quiesce", "2_old_helper_exit", "3_backup", "4_journal",
    ]
    # 真实 serve 已优雅退出（0）并自清实例文件。
    assert serve.proc.poll() == 0
    assert not serve.instance_path.exists()
    # 一致备份可读（验收框 1：修后 prepare 完成预期准备步骤且一致备份可读）。
    conn = sqlite3.connect(payload["backup_path"])
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert Path(payload["backup_path"]).is_file()
    # journal 在位（prepared）：半升级态可检测；serve 自清的实例不复活。
    journal_path = serve.runtime / "data" / "fathom.db.upgrade-journal.json"
    assert journal_path.is_file()
    assert not serve.instance_path.exists()


def test_cli_prepare_succeeds_after_real_serve_already_exited_and_self_cleaned(serve):
    """helper 先行优雅退出并自清实例文件，prepare 起始即无 instance →
    仍完成四步（修前起始读取即抛 FileNotFoundError）。"""
    instance = serve.instance()
    serve.proc.terminate()  # SIGTERM → 优雅退出自清
    serve.wait_self_cleaned_exit()
    assert not serve.instance_path.exists()
    proc, payload = _run_cli(
        serve.env, "upgrade-prepare",
        "--from", instance["version"], "--to", uf._next_patch_version(instance["version"]),
    )
    assert proc.returncode == 0, proc.stderr
    assert payload is not None and payload["ok"] is True, proc.stdout
    assert payload["kind"] == "prepared"
    assert payload["steps_done"] == [
        "1_quiesce", "2_old_helper_exit", "3_backup", "4_journal",
    ]
    _assert_backup_readable(payload["backup_path"], expected_roots=[])
    # 起始无实例 → 结束也不应出现实例文件；journal 在位。
    assert not serve.instance_path.exists()
    journal_path = serve.runtime / "data" / "fathom.db.upgrade-journal.json"
    assert journal_path.is_file()


# ================================================== 损坏实例 JSON（保守拒绝）
def test_prepare_refused_on_corrupted_instance_json(tmp_path):
    """损坏的 instance JSON：仍保守拒绝（kind=internal），不误宣告成功、
    不误清理未知状态、无备份产生。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        uf._write_0600(fake.instance_path, b"{not-json")
        coord = upgrade.UpgradeCoordinator(
            _paths(fake),
            from_version=fake.n_version, to_version=fake.n_plus_1_version,
        )  # 生产默认钩子；解析在探针之前即失败，不触碰任何进程
        result = coord.run_prepare()
        assert result["ok"] is False
        assert result["kind"] == "internal"
        assert result["error"]  # 错误可读（JSON 解析错误原文），非静默成功
        assert result["steps_done"] == ["1_quiesce"]
        # 保守：损坏文件原样保留（不误清理），无备份、journal 已随回滚清除。
        assert fake.instance_path.read_bytes() == b"{not-json"
        assert list(fake.data_dir.glob("*.backup-v*")) == []
        assert not fake.journal_path.exists()
        assert _lease_acquirable(fake)
    finally:
        fake.close()


# ============================================ helper 未退出 / 端口未释放
def test_prepare_times_out_when_helper_never_exits(tmp_path):
    """helper 未退出（pid 存活 + 端口未释放，忽略退出请求）：有界超时拒绝，
    旧 helper 记录保留、无备份、不误宣告成功。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        coord = _coordinator(
            fake,
            hooks={
                "pid_alive": lambda pid: True,     # 永不退出
                "port_open": lambda port: True,    # 端口未释放
                "request_helper_exit": lambda pid: None,  # 忽略退出请求
            },
            helper_exit_timeout_s=0.6,
        )
        result = coord.run_prepare()
        assert result["ok"] is False
        assert result["kind"] == "helper_exit_timeout"
        assert fake.instance_path.exists()
        assert list(fake.data_dir.glob("*.backup-v*")) == []
        assert not fake.journal_path.exists()
        assert _lease_acquirable(fake)
    finally:
        fake.close()


def test_prepare_times_out_when_port_not_released_after_pid_exit(tmp_path):
    """pid 已退出但端口未释放：退出确认（pid+端口双确认）不放宽 → 仍有界
    超时拒绝；pid 判活为假时绝不发信号；instance 保留（未确认不清理）。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        requests: list[int] = []
        coord = _coordinator(
            fake,
            hooks={
                "pid_alive": lambda pid: False,    # pid 已退出
                "port_open": lambda port: True,    # 端口仍被占用
                "request_helper_exit": lambda pid: requests.append(pid),
            },
            helper_exit_timeout_s=0.6,
        )
        result = coord.run_prepare()
        assert result["ok"] is False
        assert result["kind"] == "helper_exit_timeout"
        assert "已退出=True" in result["error"] and "已释放=False" in result["error"]
        assert requests == []  # 判活为假 → 无信号
        assert fake.instance_path.exists()  # 退出未双确认 → 不清理
        assert list(fake.data_dir.glob("*.backup-v*")) == []
    finally:
        fake.close()


# ================================================== 身份不符（不发信号）
def test_prepare_never_signals_process_with_mismatched_identity(tmp_path):
    """instance 身份不符（service 未知）：拒绝触碰该进程——退出请求从未
    发出（不向非本次进程发信号），kind=internal 不误宣告成功。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        instance = fake.read_instance()
        instance["service"] = "com.example.unknown"
        instance["protocol_version"] = "0.0.0-unknown"
        uf._write_0600(
            fake.instance_path,
            json.dumps(instance, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        requests: list[int] = []
        coord = _coordinator(
            fake,
            hooks={
                "pid_alive": fake.pid_alive,
                "port_open": lambda port: False,
                "request_helper_exit": lambda pid: requests.append(pid),
            },
        )
        result = coord.run_prepare()
        assert result["ok"] is False
        assert result["kind"] == "internal"
        assert "身份不符" in result["error"]
        assert requests == []
        # 原始 instance 原样保留（不误清理）。
        assert fake.read_instance()["service"] == "com.example.unknown"
    finally:
        fake.close()
