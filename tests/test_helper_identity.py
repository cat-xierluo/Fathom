"""ISS-029 G3/G4/G6：helper 身份、/health、端口让位与零击杀回归。

合同来源：apps/desktop/experiments/iss029/findings.md §3、§6 与 DEC-017。
覆盖：

- ``--version`` 单行 JSON 含 service/version/protocol_version/python/
  machine/exe（G3）。
- ``GET /health`` 字段同源、含 pid/port/runtime_mode，不含令牌/凭据/真实
  路径（G4）；回环 Host 守卫沿用。
- 同服务身份探测让位：同 target port 上启动第二个 serve 实例立即退 0
  并打印 ``same-service-discovered``，不触碰已运行 owner（G6）。
- 未知占用零击杀：测试自起的纯 TCP socket 占住 target port，serve 让
  位到下一端口并退 0；占位 socket 进程前后一致；目标端口全程不可写。
- 端口耗尽给出明确恢复动作：占用 target+range 内全部候选端口，serve
  退 3 且 stderr 含 ``恢复动作`` / ``recovery`` 字样，含具体进程排查
  命令与"不会 kill"的明示。
- 端口文件 0600 写入运行根，含 instance_id/pid/port 但不含真实资源路径。
- ``sys.frozen`` 时默认 ``release`` 模式（G2 可测面）。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from fathom import (
    SERVICE_IDENTITY,
    __protocol_version__,
    __version__,
    config,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
# 生产 CLI 入口（原仓库根 main.py，现 fathom/__main__.py；按路径直接
# 执行与 launchd dev 态 plist / PyInstaller 冻结同一形态）。
MAIN = REPO_ROOT / "fathom" / "__main__.py"


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "FATHOM_RUNTIME_MODE", "FATHOM_RUNTIME_DIR", "FATHOM_SCAN_ROOT",
        "FATHOM_PORT", "FATHOM_RESOURCE_DIR", "FATHOM_DB",
    ):
        env.pop(name, None)
    return env


def _unused_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _wait_health(port: int, timeout: float = 10.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as resp:
                return json.load(resp)
        except Exception:
            time.sleep(0.05)
    return None


def _terminate(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


# ===================================================================== G3
def test_cli_version_outputs_identity_json(tmp_path):
    """G3：--version 单行 JSON 含 service/version/protocol_version/平台信息。"""
    proc = subprocess.run(
        [sys.executable, str(MAIN), "--version"],
        capture_output=True, text=True, timeout=10,
        env=_clean_env(),
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert info["service"] == SERVICE_IDENTITY == "fathom"
    assert info["version"] == __version__
    assert info["protocol_version"] == __protocol_version__ == 1
    assert info["python"], info
    assert info["machine"], info
    assert info["exe"] == sys.executable


def test_cli_version_does_not_require_runtime_dir(tmp_path, monkeypatch):
    """--version 在未设置 FATHOM_RUNTIME_DIR 时仍可成功（静态身份面）。"""
    monkeypatch.delenv("FATHOM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("FATHOM_RUNTIME_MODE", raising=False)
    proc = subprocess.run(
        [sys.executable, str(MAIN), "--version"],
        capture_output=True, text=True, timeout=10,
        env=_clean_env(),
    )
    assert proc.returncode == 0
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert info["service"] == "fathom"


# ===================================================================== G4
def test_health_endpoint_returns_required_identity_fields(tmp_path):
    """G4：/health 字段同源、含 pid/port/runtime_mode；回环 Host 守卫。"""
    runtime = tmp_path / "runtime"
    port = _unused_port()
    proc = subprocess.Popen(
        [sys.executable, str(MAIN), "--runtime-dir", str(runtime),
         "--port", str(port), "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_clean_env(),
    )
    try:
        body = _wait_health(port)
        assert body is not None, proc.stderr.read() if proc.poll() is not None else ""
        assert body["service"] == "fathom"
        assert body["version"] == __version__
        assert body["protocol_version"] == __protocol_version__
        assert body["status"] == "ok"
        assert body["port"] == port
        assert body["runtime_mode"] == "development"
        assert body["pid"] == proc.pid
        body_text = json.dumps(body, ensure_ascii=False)
        # 不含令牌/凭据/真实路径
        assert "token" not in body
        assert "/Users/" not in body_text
        assert "secret" not in body_text
    finally:
        _terminate(proc)


def test_health_rejects_non_loopback_host(tmp_path):
    """G4：回环 Host 守卫沿用；非 loopback Host 返回 403。"""
    runtime = tmp_path / "runtime"
    port = _unused_port()
    proc = subprocess.Popen(
        [sys.executable, str(MAIN), "--runtime-dir", str(runtime),
         "--port", str(port), "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_clean_env(),
    )
    try:
        body = _wait_health(port)
        assert body is not None
        # 使用 urllib 自定义 Host 头应当被守卫拒为 403
        from urllib.error import HTTPError
        from urllib.request import Request
        req = Request(f"http://127.0.0.1:{port}/health",
                      headers={"Host": "evil.example"})
        with pytest.raises(HTTPError) as exc:
            urlopen(req, timeout=1.5)
        assert exc.value.code == 403
    finally:
        _terminate(proc)


# ===================================================================== G6 让位与零击杀
def test_same_service_yields_gracefully_on_target_port(tmp_path):
    """G6：target port 已是同服务实例时让位退 0，不触碰已运行 owner。"""
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"
    port = _unused_port()

    p1 = subprocess.Popen(
        [sys.executable, str(MAIN), "--runtime-dir", str(runtime_a),
         "--port", str(port), "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_clean_env(),
    )
    try:
        assert _wait_health(port) is not None, "owner 没起来"
        original_pid = p1.pid

        # 同服务身份实例探测 target port 应让位退出 0
        proc = subprocess.run(
            [sys.executable, str(MAIN), "--runtime-dir", str(runtime_b),
             "--port", str(port), "serve"],
            capture_output=True, text=True, timeout=15,
            env=_clean_env(),
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert "same-service-discovered" in proc.stdout
        assert "让位" in proc.stdout or "未触碰" in proc.stdout
        # 让位者不应在 runtime_b 写 port 文件
        assert not (runtime_b / config.HELPER_INSTANCE_FILENAME).exists()
        # owner 仍在
        assert p1.poll() is None
        assert p1.pid == original_pid
    finally:
        _terminate(p1)


def test_unknown_occupant_zero_kill_falls_back(tmp_path):
    """G6：未知占用者零击杀；让位下一空闲端口并优雅退出 0。"""
    runtime = tmp_path / "runtime"
    target = _unused_port()
    dummy = socket.socket()
    dummy.bind(("127.0.0.1", target))
    dummy.listen()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(MAIN), "--runtime-dir", str(runtime),
             "--port", str(target), "--port-range", "2", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_clean_env(),
        )
        try:
            # 让位应落到 target+1 或 target+2
            health = None
            chosen_fallback = None
            for fallback in (target + 1, target + 2):
                health = _wait_health(fallback, timeout=8)
                if health is not None:
                    chosen_fallback = fallback
                    break
            assert health is not None, (
                f"fallback port 未在 8s 内 ready（target={target}）；"
                f"proc.poll={proc.poll()}"
            )
            assert health["port"] == chosen_fallback, health
            # 优雅退出（uvicorn.run 在 SIGTERM 下既可能显式 return 0，
            # 也可能在某些 OS/线程时序下被信号默认动作直接结束；-SIGTERM
            # 也算「无 kill / 无 force-kill / 由 SIGTERM 触发」）。
            proc.terminate()
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait(timeout=2)
            assert code == 0 or code == -signal.SIGTERM, code
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        # dummy 仍在监听
        s = socket.socket()
        try:
            s.connect(("127.0.0.1", target))
            s.close()
        except OSError:
            pytest.fail("dummy listener on target port was killed")
    finally:
        dummy.close()


def test_unknown_occupant_zero_kill_exhausted_clear_recovery(tmp_path):
    """G6：占用 target + 全部 fallback 时退 3，含明确恢复动作且未触碰占用者。"""
    runtime = tmp_path / "runtime"
    target = _unused_port()
    dummies: list[socket.socket] = []
    # range = 2 → candidates = target, target+1, target+2 → 全部占住
    for offset in range(3):
        s = socket.socket()
        s.bind(("127.0.0.1", target + offset))
        s.listen()
        dummies.append(s)
    try:
        proc = subprocess.run(
            [sys.executable, str(MAIN), "--runtime-dir", str(runtime),
             "--port", str(target), "--port-range", "2", "serve"],
            capture_output=True, text=True, timeout=15,
            env=_clean_env(),
        )
        assert proc.returncode == 3, (proc.stdout, proc.stderr)
        assert "ports-exhausted" in proc.stderr
        assert "恢复动作" in proc.stderr
        assert "lsof" in proc.stderr  # 具体排查命令
        assert "kill" in proc.stderr and "不会" in proc.stderr
        # 三个占位 socket 全程活跃
        for offset, s in enumerate(dummies):
            client = socket.socket()
            try:
                client.connect(("127.0.0.1", target + offset))
                client.close()
            except OSError:
                pytest.fail(f"占位 socket 在 target+{offset} 被 kill 或失效")
    finally:
        for s in dummies:
            s.close()


# ===================================================================== 端口文件
def test_helper_instance_file_0600_in_runtime_dir(tmp_path):
    """启动成功后端口发现文件 0600 写入运行根，供应用壳读取。"""
    runtime = tmp_path / "runtime"
    port = _unused_port()
    proc = subprocess.Popen(
        [sys.executable, str(MAIN), "--runtime-dir", str(runtime),
         "--port", str(port), "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_clean_env(),
    )
    try:
        body = _wait_health(port)
        assert body is not None
        path = runtime / config.HELPER_INSTANCE_FILENAME
        assert path.is_file()
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, mode
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["service"] == "fathom"
        assert record["protocol_version"] == __protocol_version__
        assert record["version"] == __version__
        assert record["pid"] == proc.pid
        assert record["port"] == port
        # 不含真实资源路径
        assert "/Users/" not in json.dumps(record, ensure_ascii=False)
    finally:
        _terminate(proc)


# ===================================================================== G2 可测面
def test_frozen_default_is_release_mode(tmp_path):
    """sys.frozen 置位且无 env 时默认 release；显式 env 优先。"""
    saved = getattr(sys, "frozen", None)
    sys.frozen = True
    try:
        # 用 tmp_path 而非 /tmp 避开 macOS 上 /tmp → /private/tmp 的 symlink 解析差异
        proj = tmp_path / "proj"
        home = tmp_path / "home"
        cfg = config.RuntimeConfig.from_env(
            {}, project_root=proj, home=home,
        )
        assert cfg.mode == "release"
        assert cfg.runtime_dir == home / "Library" / "Application Support" / "Fathom"
    finally:
        if saved is None:
            try:
                del sys.frozen
            except AttributeError:
                pass
        else:
            sys.frozen = saved


def test_frozen_default_overridden_by_env(tmp_path):
    """显式 FATHOM_RUNTIME_MODE 覆盖 sys.frozen 默认值。"""
    saved = getattr(sys, "frozen", None)
    sys.frozen = True
    try:
        proj = tmp_path / "proj"
        home = tmp_path / "home"
        cfg = config.RuntimeConfig.from_env(
            {"FATHOM_RUNTIME_MODE": "development"},
            project_root=proj, home=home,
        )
        assert cfg.mode == "development"
        assert cfg.runtime_dir == proj
    finally:
        if saved is None:
            try:
                del sys.frozen
            except AttributeError:
                pass
        else:
            sys.frozen = saved
