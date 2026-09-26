"""ISS-098 · 安装后失败的真实旧 bundle 与数据恢复聚焦验。

合同来源：docs/TASKS.md 的 ISS-098 卡（唯一父卡 ISS-030；依赖 ISS-097
事务合同）。与既有四个升级测试文件的分工：本文件钉「生产恢复入口必须
真的恢复旧版文件并重启旧 helper、旧数据可用」——不再仅由测试注入
``restore_old_version`` 钩子证明恢复。

覆盖（全部隔离：tmp 运行根、自建「冻结形态」helper onedir、随机回环
端口、零生产触碰、零真实网络）：

- **恢复材料合同**：prepare（③一致备份之后、N+1 替换之前）把现役
  helper 副本 + manifest 落盘运行根 ``upgrade-restore/``（journal 记
  restore_dir）；磁盘预算不足在替换前拒绝（disk_full，零副作用）；
  ``--helper-dir`` 缺省（开发态 CLI）跳过保存、四步合同不回归。
- **修前反例转正**：N+1 替换后（updater 临时旧包已析构的生产前提）
  显式 ``upgrade-rollback`` 从恢复区恢复旧版文件、核验身份与旧库，
  全过才清 journal（先红证据见
  verify-results/iss-098/rollback-false-success-before.json）。
- **三类故障注入**（实际隔离 .app/冻结 helper 更新入口）：安装后启动
  失败（N+1 入口不可执行）、身份不符（--version 报告错误身份）、迁移
  失败（库被迁移污染 → finalize 的 db_verify_failed → 恢复链停 N+1、
  从一致备份恢复库）——旧版可运行（--version/真实 serve /health）、
  旧库 integrity 与历史可读。
- **边界**：恢复失败明确报错并保留材料（停写持续、修复后可接续）；
  恢复途中再次中断可重试接续；正常 N→N+1 在健康与数据核验完成前
  保留材料、finalize 成功才清恢复区（SQLite 备份保留）。
- **壳接线**（lib.rs Python 钉子）：prepare 附带 --helper-dir、finalize
  数据库校验失败（db_verify_failed）走恢复链、恢复失败不重启未核验
  helper。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from fathom import SERVICE_IDENTITY, __protocol_version__, __version__, db, scan_coordinator

import upgrade_fixture as uf

from fathom import upgrade

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
LIB_RS = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "src" / "lib.rs"

# 死 pid（macOS pid 上限 99998）与永不监听的回环端口：CLI 子进程里的
# 生产探针据此确定性确认「已退出、端口已释放」（与 production_wiring
# 同款手法，零真实信号、零真实网络）。
DEAD_PID = 4_000_000
DEAD_PORT = 1

# 自建「冻结形态」helper shim（任务卡许可：测试自建冻结形态）：入口
# fathom-helper + _internal/version.txt，支持 --version（单行 JSON 身份
# 面，ISS-029 G3 语义）与 serve（最小 /health 应答器——恢复后旧 helper
# 「健康检查」的真实可测面）。只用 stdlib，#!/usr/bin/env python3 保证
# shebang 不受本仓 .venv 路径空格影响。
SHIM_SOURCE = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

version = Path(__file__).resolve().parent.joinpath(
    "_internal", "version.txt").read_text(encoding="utf-8").strip()
args = sys.argv[1:]
if "--version" in args:
    print(json.dumps({
        "service": "fathom",
        "protocol_version": 1,
        "version": version,
        "python": "shim",
    }, ensure_ascii=False))
    raise SystemExit(0)
if "serve" in args:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = json.dumps({
                    "service": "fathom",
                    "protocol_version": 1,
                    "status": "ok",
                    "version": version,
                    "pid": os.getpid(),
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_args):
            pass

    port = int(os.environ.get("FATHOM_SHIM_PORT", "0"))
    # double-fork：serve 子进程脱离本测试进程（过继给 launchd）——SIGTERM
    # 后被立刻收尸，生产判活探针（ps -p）不会像对待僵尸进程那样误判存活。
    if os.fork() == 0:
        HTTPServer(("127.0.0.1", port), Handler).serve_forever()
raise SystemExit(0)
"""


def write_shim_helper(helper_root: Path, version: str) -> None:
    """写入「冻结形态」helper onedir：入口 + _internal/{version.txt,资产}。"""
    helper_root.mkdir(parents=True)
    internal = helper_root / "_internal"
    internal.mkdir()
    (internal / "version.txt").write_text(version + "\n", encoding="utf-8")
    (internal / "placeholder.dat").write_text("payload" * 8, encoding="utf-8")
    entry = helper_root / "fathom-helper"
    entry.write_text(SHIM_SOURCE, encoding="utf-8")
    entry.chmod(0o755)


def write_broken_helper(helper_root: Path) -> None:
    """「安装后启动失败」形态：入口存在但内容损坏（exec 即 ENOEXEC）。"""
    helper_root.mkdir(parents=True)
    internal = helper_root / "_internal"
    internal.mkdir()
    (internal / "version.txt").write_text("broken\n", encoding="utf-8")
    entry = helper_root / "fathom-helper"
    entry.write_bytes(b"\x00\x01\x02 not-a-mach-o binary garbage")
    entry.chmod(0o755)


def helper_identity(helper_root: Path) -> dict:
    """运行 helper --version 并解析身份面（与壳 probe_helper_identity 同款）。"""
    proc = subprocess.run(
        [str(helper_root / "fathom-helper"), "--version"],
        capture_output=True, text=True, timeout=30,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return json.loads(lines[-1])


def freed_port() -> int:
    """曾有监听、现已无监听的临时端口（bind :0 后立即关闭）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def spawn_shim_serve(helper_root: Path, port: int) -> subprocess.Popen:
    """spawn shim serve（shim 内部 double-fork：Popen 的直接子进程立即退出，
    真正的 serve 进程过继给 launchd、/health 的 pid 字段如实上报）。"""
    env = os.environ.copy()
    env["FATHOM_SHIM_PORT"] = str(port)
    child = subprocess.Popen(
        [str(helper_root / "fathom-helper"), "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    child.wait(timeout=10)  # 直接子进程退出 = serve 已 double-fork
    return child


def wait_health(port: int, timeout: float = 10.0) -> dict:
    """轮询 127.0.0.1:port/health 至就绪；超时断言失败。"""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=0.5
            ) as response:
                return json.load(response)
        except Exception as exc:  # noqa: BLE001 - 轮询至超时
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"shim /health 未在 {timeout}s 内就绪：{last_error}")


def _pid_gone(pid: int, timeout: float = 10.0) -> bool:
    """只读等待 pid 从进程表消失（ps -p，与生产探针同款；零信号）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid="],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return True
        time.sleep(0.05)
    return False


def stop_shim_serve(pid: int) -> None:
    """回收测试自拥有、自启动的 serve 进程（SIGTERM → 有界等待 → SIGKILL
    兜底；只对本测试 spawn 出的 pid 发信号）。"""
    os.kill(pid, signal.SIGTERM)
    if not _pid_gone(pid):
        os.kill(pid, signal.SIGKILL)
        assert _pid_gone(pid), f"shim serve pid={pid} 无法回收"


@pytest.fixture
def env(tmp_path):
    """每个测试独立 fake 运行根（030A 夹具搭环境）；结束关闭持有的 DB 连接。"""
    fake = uf.FakeUpgradeEnv.build(tmp_path / "upgrade-env")
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def install_area(tmp_path, env) -> Path:
    """隔离「安装区」：N 版冻结形态 helper（生产 = .app 资源目录内 onedir）。"""
    helper_root = tmp_path / "install-root" / "fathom-helper"
    write_shim_helper(helper_root, env.n_version)
    return helper_root


def _paths(env) -> upgrade.UpgradePaths:
    return upgrade.UpgradePaths(
        runtime_dir=env.runtime,
        db_path=env.db_path,
        lock_path=env.scan_lock_path,
        instance_path=env.instance_path,
        journal_path=env.journal_path,
    )


def _hooks(env, **overrides):
    """生产可注入系统动作接到 fake 环境模型（绝不注入 restore_old_version
    ——本文件的恢复能力断言只认生产恢复链）。"""
    hooks = {
        "pid_alive": env.pid_alive,
        "port_open": lambda port: False,
        "request_helper_exit": lambda pid: env.stop_helper(pid),
    }
    hooks.update(overrides)
    return hooks


def _coordinator(env, helper_dir=None, **hook_overrides) -> upgrade.UpgradeCoordinator:
    return upgrade.UpgradeCoordinator(
        _paths(env),
        from_version=env.n_version,
        to_version=env.n_plus_1_version,
        helper_dir=helper_dir,
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


def _write_dead_instance(env, *, version: str | None = None) -> None:
    instance = {
        "pid": DEAD_PID, "port": DEAD_PORT,
        "service": SERVICE_IDENTITY,
        "protocol_version": __protocol_version__,
        "version": version if version is not None else env.n_version,
        "instance_id": "iss098-test", "runtime_mode": "release",
    }
    uf._write_0600(
        env.instance_path,
        json.dumps(instance, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def _replace_with_n_plus_1(helper_root: Path, env) -> None:
    """模拟 tauri updater install 返回后的安装区形态：helper 换 N+1
    （旧 N 已随 updater 临时目录析构——ISS-098 的缺口前提）。"""
    shutil.rmtree(helper_root)
    write_shim_helper(helper_root, env.n_plus_1_version)


# ==================================================== 恢复材料合同（③b）
def test_prepare_saves_old_bundle_into_restore_area(env, install_area):
    """prepare ③b：③一致备份之后把现役 helper 副本 + manifest 落盘恢复区
    （N+1 替换之前）；journal 记 restore_dir；steps_done 含 3b。"""
    coord = _coordinator(env, helper_dir=install_area)
    result = coord.run_prepare()
    assert result["ok"], result
    assert result["steps_done"] == [
        "1_quiesce", "2_old_helper_exit", "3_backup", "3b_save_old_bundle",
        "4_journal",
    ]
    restore_dir = upgrade.restore_dir_from_runtime(env.runtime)
    assert restore_dir.is_dir()
    manifest = upgrade.read_restore_manifest(_paths(env))
    assert manifest is not None
    assert manifest["from_version"] == env.n_version
    assert manifest["to_version"] == env.n_plus_1_version
    assert manifest["txn_id"] == coord.txn_id
    assert manifest["helper_dir"] == str(install_area)
    assert manifest["db_backup_path"] == str(coord.backup_path)
    # 副本完整：文件树与源一致（内容级）。
    saved_entry = restore_dir / "helper" / "fathom-helper"
    assert saved_entry.read_bytes() == (install_area / "fathom-helper").read_bytes()
    files, size = upgrade._tree_stats(restore_dir / "helper")
    assert files == manifest["helper_files"] and size == manifest["helper_bytes"]
    # manifest 是 0600。
    mode = (restore_dir / upgrade.RESTORE_MANIFEST_FILENAME).stat().st_mode & 0o777
    assert mode == 0o600
    # journal 增量字段：restore_dir 在位。
    journal = upgrade.read_journal(_paths(env))
    assert journal["restore_dir"] == str(restore_dir)
    assert journal["helper_dir"] == str(install_area)


def test_prepare_without_helper_dir_skips_save_and_keeps_contract(env):
    """``--helper-dir`` 缺省（开发态 CLI）：不建恢复区、四步合同与
    steps_done 不变（既有调用方零回归），journal 如实记无材料。"""
    _write_dead_instance(env)
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
    )
    assert code == 0 and payload["ok"], (code, payload, err)
    assert payload["steps_done"] == [
        "1_quiesce", "2_old_helper_exit", "3_backup", "4_journal",
    ]
    assert not upgrade.restore_dir_from_runtime(env.runtime).exists()
    journal = upgrade.read_journal(_paths(env))
    assert journal["restore_dir"] is None and journal["helper_dir"] is None


def test_prepare_disk_budget_refusal_happens_before_replacement(env, install_area):
    """磁盘预算不足：prepare 在 N+1 替换前拒绝（disk_full、零副作用——
    无 journal、无恢复区、旧 helper 不动、数据不动），绝不赌磁盘。"""
    tiny = 1024
    coord = _coordinator(env, helper_dir=install_area, disk_free_bytes=lambda: tiny)
    result = coord.run_prepare()
    assert result["ok"] is False
    assert result["kind"] == "disk_full"
    assert "拒绝升级" in result["error"]
    # 零替换副作用：无 journal、无恢复区（③b 的预算闸先于任何替换动作，
    # 也先于材料落盘）、数据完整。②旧 helper 退出与③一致备份按协议正常
    # 发生（instance 已自清、备份按 ISS-040C 合同保留——可独立打开，
    # 无害残留）。
    assert not env.journal_path.exists()
    assert not upgrade.restore_dir_from_runtime(env.runtime).exists()
    assert result["backup_path"] is not None and Path(result["backup_path"]).is_file()
    assert env.snapshot_roots() == env.expected_roots()


# ==================================================== 修前反例转正（恢复链）
def test_cli_rollback_restores_old_bundle_after_replacement(env, install_area):
    """修前反例（rollback 报成功但安装区仍 N+1，见
    verify-results/iss-098/rollback-false-success-before.json）转正：
    N+1 替换后显式 upgrade-rollback 真的恢复旧版文件并核验——helper
    身份面回到 N、旧库校验通过、journal 才清除。"""
    _write_dead_instance(env)
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
        "--helper-dir", str(install_area),
    )
    assert code == 0 and payload["ok"], (code, payload, err)

    _replace_with_n_plus_1(install_area, env)
    assert helper_identity(install_area)["version"] == env.n_plus_1_version

    code, rolled, err = _run_cli(env.runtime, "upgrade-rollback")
    assert code == 0, err
    assert rolled["ok"] is True, rolled
    for action in (
        "restored_old_bundle_from_restore_area",
        "verified_restored_helper_identity",
        "verified_old_database",
        "removed_journal",
    ):
        assert action in rolled["actions"], (action, rolled["actions"])
    # 安装区回到 N：真实执行恢复后 helper 的 --version。
    assert helper_identity(install_area)["version"] == env.n_version
    assert not env.journal_path.exists()
    # 恢复材料保留（清理只发生在下次 finalize 成功或 prepare 重建）。
    assert upgrade.restore_dir_from_runtime(env.runtime).is_dir()


def test_restore_material_absent_keeps_legacy_semantics(env):
    """材料缺失（旧事务/未保存）：显式 rollback 维持 ISS-097 前语义——
    journal 清除 + 重启归壳，不误报恢复失败（兼容钉子）。"""
    _write_dead_instance(env)
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
    )
    assert code == 0 and payload["ok"], (code, payload, err)
    code, rolled, err = _run_cli(env.runtime, "upgrade-rollback")
    assert code == 0 and rolled["ok"] is True
    assert "restore_material_absent" in rolled["actions"]
    assert "removed_journal" in rolled["actions"]
    assert "helper_restart_deferred_to_shell" in rolled["actions"]
    assert not env.journal_path.exists()


def test_rollback_restore_failure_reports_and_keeps_material(env, install_area):
    """恢复失败必须明确报错并保留材料：两层拦截（形态校验——副本与清单
    字节数不符；身份核验——副本版本标记被同长度篡改为未知版本）→
    ok=false/restore_failed、材料与 journal 保留、停写持续；修复材料后
    同一入口重试成功（接续）。"""
    from fathom import config, notify, scanner  # 停写断言需要 config 指向

    old_config = (config.DB_PATH, config.DATA_DIR, config.DEFAULT_ROOT)
    config.DB_PATH = env.db_path
    config.DATA_DIR = env.data_dir
    config.DEFAULT_ROOT = env.runtime / "synthetic-root"
    config.DEFAULT_ROOT.mkdir(exist_ok=True)
    notify.send_notification = lambda *a, **kw: True
    scanner.run_du = lambda target: scanner.DuResult({str(target): 0}, 0, 0, 0.01)
    marker = (upgrade.restore_dir_from_runtime(env.runtime) / "helper"
              / "_internal" / "version.txt")
    try:
        _write_dead_instance(env)
        code, payload, err = _run_cli(
            env.runtime, "upgrade-prepare",
            "--from", env.n_version, "--to", env.n_plus_1_version,
            "--helper-dir", str(install_area),
        )
        assert code == 0 and payload["ok"], (code, payload, err)
        _replace_with_n_plus_1(install_area, env)

        # 拦截层一（形态校验）：副本字节数被篡改（与清单不符）。
        marker.write_text("9.9.9-tampered-long\n", encoding="utf-8")
        code, rolled, err = _run_cli(env.runtime, "upgrade-rollback")
        assert code == 1, (rolled, err)
        assert rolled["ok"] is False and rolled["kind"] == "restore_failed"
        assert "形态与清单不符" in rolled["error"]

        # 拦截层二（身份核验）：同长度篡改（字节数与清单一致、版本标记为
        # 未知值）——形态校验放行、身份核验捕获。
        marker.write_text("9.9.9\n", encoding="utf-8")
        code, rolled, err = _run_cli(env.runtime, "upgrade-rollback")
        assert code == 1, (rolled, err)
        assert rolled["ok"] is False and rolled["kind"] == "restore_failed"
        assert "身份核验失败" in rolled["error"]
        # 材料与 journal 保留：停写持续（写入方仍被拒）。
        assert env.journal_path.exists()
        assert upgrade.restore_dir_from_runtime(env.runtime).is_dir()
        with pytest.raises(scan_coordinator.UpgradeWriteStopError):
            scan_coordinator.start_scan(source="cli")

        # 接续：修复材料（版本标记改回 N）后同一入口重试成功。
        marker.write_text(env.n_version + "\n", encoding="utf-8")
        code, rolled, err = _run_cli(env.runtime, "upgrade-rollback")
        assert code == 0 and rolled["ok"] is True, (rolled, err)
        assert helper_identity(install_area)["version"] == env.n_version
        assert not env.journal_path.exists()
        _run_id, result = scan_coordinator.run_scan(source="cli")
        assert result["snapshot_id"] is not None
    finally:
        (config.DB_PATH, config.DATA_DIR, config.DEFAULT_ROOT) = old_config


def test_rollback_interrupted_midway_resumes(env, install_area, monkeypatch):
    """恢复途中再次中断的接续：文件复制中途失败（模拟恢复进程死亡后的
    半恢复态）→ 报错保留材料；中断解除后重跑同一入口即接续成功，安装区
    既不留 N+1 也不留半成品。"""
    _write_dead_instance(env)
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
        "--helper-dir", str(install_area),
    )
    assert code == 0 and payload["ok"], (code, payload, err)
    _replace_with_n_plus_1(install_area, env)

    coord = _coordinator(env, helper_dir=install_area)
    real_copytree = shutil.copytree
    calls = {"n": 0}

    def _interrupting_copytree(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # 恢复链的第一次复制（材料 → 安装区）中断
            raise OSError("injected: 恢复复制中途死亡")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(upgrade.shutil, "copytree", _interrupting_copytree)
    interrupted = coord.rollback(reason="test interrupt", own_journal_only=False)
    assert interrupted["ok"] is False and interrupted["kind"] == "restore_failed"
    # journal 与材料保留（可接续）。
    assert env.journal_path.exists()
    assert upgrade.restore_dir_from_runtime(env.runtime).is_dir()

    monkeypatch.setattr(upgrade.shutil, "copytree", real_copytree)
    coord2 = _coordinator(env, helper_dir=install_area)
    resumed = coord2.rollback(reason="resume", own_journal_only=False)
    assert resumed["ok"] is True, resumed
    assert helper_identity(install_area)["version"] == env.n_version
    assert not env.journal_path.exists()
    # 无半成品残留（trash 已随成功恢复清理）。
    assert not install_area.with_name(install_area.name + ".pre-restore").exists()


# ==================================================== finalize 与成功链
def test_run_full_keeps_material_until_finalize_clears(env, install_area):
    """正常 N→N+1：健康与数据核验完成前（⑤安装/⑥核验进行中）恢复材料
    保留；finalize 成功后清恢复区、SQLite 备份保留、journal 清除。"""
    material_seen = {}

    def _install(_ctx: dict) -> None:
        # ⑤进行中：N+1 已就位、健康与数据核验未完成——材料必须在位。
        _replace_with_n_plus_1(install_area, env)
        material_seen["installing"] = (
            upgrade.restore_dir_from_runtime(env.runtime).is_dir()
        )

    def _identity() -> dict:
        material_seen["verifying"] = (
            upgrade.restore_dir_from_runtime(env.runtime).is_dir()
        )
        return {
            "service": SERVICE_IDENTITY,
            "protocol_version": __protocol_version__,
            "version": env.n_plus_1_version,
        }

    coord = _coordinator(env, helper_dir=install_area)
    result = coord.run_full(install=_install, new_helper_identity=_identity)
    assert result["ok"] and result["finalized"], result
    assert material_seen == {"installing": True, "verifying": True}
    # finalize 后：恢复区清除（含 manifest）、journal 清除、备份保留。
    assert not upgrade.restore_dir_from_runtime(env.runtime).exists()
    assert not env.journal_path.exists()
    assert coord.backup_path is not None and coord.backup_path.is_file()
    assert helper_identity(install_area)["version"] == env.n_plus_1_version
    assert env.snapshot_roots() == env.expected_roots()


def test_run_full_finalize_db_verify_failure_rolls_back(env, install_area):
    """run_full 协议建模与壳行为一致：finalize 的数据库校验失败
    （install 回调后库被迁移污染）→ ok=False/db_verify_failed、不伪装
    成功，自动走恢复链（恢复旧版文件 + 从备份恢复库）。"""
    import sqlite3 as _sqlite3

    def _install(_ctx: dict) -> None:
        _replace_with_n_plus_1(install_area, env)
        env.close()  # 释放夹具 WAL 连接（库即将被污染）
        conn = _sqlite3.connect(env.db_path)
        try:
            conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
            conn.commit()
        finally:
            conn.close()

    def _identity() -> dict:
        return {
            "service": SERVICE_IDENTITY,
            "protocol_version": __protocol_version__,
            "version": env.n_plus_1_version,
        }

    coord = _coordinator(env, helper_dir=install_area)
    result = coord.run_full(install=_install, new_helper_identity=_identity)
    assert result["ok"] is False, result
    assert result["kind"] == "db_verify_failed"
    assert "restored_old_bundle_from_restore_area" in result["rollback_actions"]
    assert "restored_database_from_backup" in result["rollback_actions"]
    # 恢复链已把安装区与库都回到可用旧版。
    assert helper_identity(install_area)["version"] == env.n_version
    verify = upgrade.verify_database_usable(env.db_path)
    assert verify["ok"] and verify["snapshot_count"] == len(env.expected_roots())
    assert not env.journal_path.exists()


def test_finalize_db_verify_failure_triggers_restore_chain(env, install_area):
    """迁移失败闭环（验收 2 之三）：新 helper 就位后库被迁移污染
    （user_version 高于当前程序支持）→ upgrade-finalize 拒绝收口
    （db_verify_failed、journal/材料保留）→ upgrade-rollback 恢复旧版
    文件并从一致备份恢复库（历史可读回到备份基线）。"""
    _write_dead_instance(env)
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
        "--helper-dir", str(install_area),
    )
    assert code == 0 and payload["ok"], (code, payload, err)
    backup_path = Path(payload["backup_path"])
    _replace_with_n_plus_1(install_area, env)

    # 模拟 N+1 迁移半途污染：库被标成更高 schema（旧版/当前版打不开）。
    conn = __import__("sqlite3").connect(env.db_path)
    try:
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()
    env.close()  # 释放夹具持有的 WAL 连接，避免污染库判定被干扰

    code, finalized, err = _run_cli(env.runtime, "upgrade-finalize")
    assert code == 1, (finalized, err)
    assert finalized["ok"] is False and finalized["kind"] == "db_verify_failed"
    assert "升级未收口" in finalized["error"]
    # journal 与材料保留（停写持续，可走恢复链）。
    assert env.journal_path.exists()
    assert upgrade.restore_dir_from_runtime(env.runtime).is_dir()

    code, rolled, err = _run_cli(env.runtime, "upgrade-rollback")
    assert code == 0 and rolled["ok"] is True, (rolled, err)
    assert "restored_database_from_backup" in rolled["actions"]
    # 旧版文件恢复 + 库回到备份基线（历史可读）。
    assert helper_identity(install_area)["version"] == env.n_version
    verify = upgrade.verify_database_usable(env.db_path)
    assert verify["ok"], verify
    assert verify["snapshot_count"] == len(env.expected_roots())
    # 污染库残骸留证（随恢复区清理一并移除）。
    assert backup_path.is_file()
    leftovers = list(env.data_dir.glob(
        env.db_path.name + upgrade.CORRUPT_DB_INFIX + "*"))
    assert leftovers, "被迁移污染的库应移走留证而非删除"
    assert not env.journal_path.exists()


# ==================================================== 三类故障注入（真实形态）
def _fault_chain_prepare(env, install_area) -> None:
    _write_dead_instance(env)
    code, payload, err = _run_cli(
        env.runtime, "upgrade-prepare",
        "--from", env.n_version, "--to", env.n_plus_1_version,
        "--helper-dir", str(install_area),
    )
    assert code == 0 and payload["ok"], (code, payload, err)


def _assert_old_version_healthy(env, install_area) -> None:
    """恢复后旧版「可运行 + 数据可用」公共断言。"""
    identity = helper_identity(install_area)
    assert identity["service"] == SERVICE_IDENTITY
    assert identity["protocol_version"] == __protocol_version__
    assert identity["version"] == env.n_version
    verify = upgrade.verify_database_usable(env.db_path)
    assert verify["ok"], verify
    assert verify["snapshot_count"] == len(env.expected_roots())


def test_fault_new_helper_launch_failure_restores_old(env, install_area):
    """故障注入·安装后启动失败：N+1 入口损坏（exec 即失败）→ 壳等效
    核验（--version）确实失败 → 显式 rollback 恢复旧版并核验。"""
    _fault_chain_prepare(env, install_area)
    shutil.rmtree(install_area)
    write_broken_helper(install_area)
    # 壳等效核验：verify_new_helper 语义（probe --version）在坏入口上失败。
    broken_check = upgrade.verify_restored_helper(install_area, env.n_plus_1_version)
    assert broken_check["ok"] is False

    code, rolled, err = _run_cli(env.runtime, "upgrade-rollback")
    assert code == 0 and rolled["ok"] is True, (rolled, err)
    assert "restored_old_bundle_from_restore_area" in rolled["actions"]
    _assert_old_version_healthy(env, install_area)


def test_fault_new_helper_identity_mismatch_restores_old(env, install_area):
    """故障注入·身份不符：N+1 上报错误身份（version=未知值）→ 壳等效
    核验失败 → rollback 恢复旧版。"""
    _fault_chain_prepare(env, install_area)
    _replace_with_n_plus_1(install_area, env)
    (install_area / "_internal" / "version.txt").write_text(
        "9.9.9-unknown\n", encoding="utf-8")
    mismatch = upgrade.verify_restored_helper(install_area, env.n_plus_1_version)
    assert mismatch["ok"] is False and "身份不符" in mismatch["error"]

    code, rolled, err = _run_cli(env.runtime, "upgrade-rollback")
    assert code == 0 and rolled["ok"] is True, (rolled, err)
    _assert_old_version_healthy(env, install_area)


def test_fault_migration_failure_stops_new_helper_and_restores(env, install_area):
    """故障注入·迁移失败（真实进程链）：N+1 serve 真实启动（instance 记录
    N+1 pid/端口）→ 库被迁移污染 → finalize 拒绝 → rollback 停滞留 N+1
    （真实 SIGTERM + 有界确认）、恢复旧版文件、从备份恢复库；恢复后旧版
    真实 serve /health 健康。"""
    _fault_chain_prepare(env, install_area)
    _replace_with_n_plus_1(install_area, env)
    env.close()  # 释放夹具 WAL 连接（库即将被污染替换）

    port = freed_port()
    spawn_shim_serve(install_area, port)
    serve_pid = None
    try:
        health = wait_health(port)
        assert health["version"] == env.n_plus_1_version
        serve_pid = int(health["pid"])
        # N+1 运行中：instance 如实记录（生产中由 helper 自写）。
        instance = {
            "pid": serve_pid, "port": port,
            "service": SERVICE_IDENTITY,
            "protocol_version": __protocol_version__,
            "version": env.n_plus_1_version,
            "instance_id": "iss098-n-plus-1", "runtime_mode": "release",
        }
        uf._write_0600(
            env.instance_path,
            json.dumps(instance, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

        # 迁移失败形态：库被标成更高 schema。
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(env.db_path)
        try:
            conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
            conn.commit()
        finally:
            conn.close()

        code, finalized, err = _run_cli(env.runtime, "upgrade-finalize")
        assert code == 1 and finalized["kind"] == "db_verify_failed"

        code, rolled, err = _run_cli(env.runtime, "upgrade-rollback")
        assert code == 0 and rolled["ok"] is True, (rolled, err)
        assert "stopped_new_version_helper" in rolled["actions"]
        assert "restored_old_bundle_from_restore_area" in rolled["actions"]
        assert "restored_database_from_backup" in rolled["actions"]
        # N+1 已被停滞留：instance 记录清除、进程从进程表消失。
        assert _pid_gone(serve_pid)
        assert not env.instance_path.exists()
        # 旧版文件恢复 + 身份面正确。
        assert helper_identity(install_area)["version"] == env.n_version
        serve_pid = None  # 已被恢复链停止，无需测试清理

    finally:
        if serve_pid is not None:
            stop_shim_serve(serve_pid)

    # 恢复后旧版真实 serve：/health 健康（旧版版本）+ 旧库历史可读。
    old_port = freed_port()
    spawn_shim_serve(install_area, old_port)
    try:
        health = wait_health(old_port)
        assert health["version"] == env.n_version
        assert health["service"] == SERVICE_IDENTITY
    finally:
        stop_shim_serve(int(health["pid"]))
    verify = upgrade.verify_database_usable(env.db_path)
    assert verify["ok"] and verify["snapshot_count"] == len(env.expected_roots())
    assert not env.journal_path.exists()


# ==================================================== 壳接线合同（Python 钉子）
def _librs_code_without_line_comments() -> str:
    lines = LIB_RS.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("//"))


def test_librs_restore_contract_wired():
    """lib.rs 壳接线合同（ISS-098）：prepare 附带 --helper-dir（现役
    helper onedir 根，恢复材料来源）；finalize 结果分级——数据库校验失败
    （db_verify_failed）走恢复链回滚；回滚子命令未如实报告成功时不重启
    未核验的安装区 helper；updater 命令面仍恰三条。"""
    assert LIB_RS.is_file()
    code = _librs_code_without_line_comments()
    assert '"--helper-dir"' in code, "prepare 必须附带 --helper-dir（恢复材料来源）"
    assert "helper_source_dir" in code, "helper 目录来自 locate_helper 结果的父目录"
    assert '"db_verify_failed"' in code, "finalize 数据库校验失败必须成态可辨"
    assert "rollback_upgrade_and_restart_helper" in code
    assert "should_restart" in code, "恢复失败时不得重启未核验的安装区 helper"
    # updater 命令面仍恰三条（恰 3 权限 ACL 合同不回退）。
    block = re.search(r"generate_handler!\[(.*?)\]", code, re.DOTALL)
    assert block, "generate_handler 接线块必须在位"
    wired = set(re.findall(r"\bupdater_[a-z_]+\b", block.group(1)))
    assert wired == {"updater_check", "updater_install", "updater_restart"}
