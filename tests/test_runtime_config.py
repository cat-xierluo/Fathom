"""ISS-025：单一运行配置、CLI/API 隔离及端口冲突。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

from fathom import config, db

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN = REPO_ROOT / "main.py"


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


def test_explicit_runtime_root_derives_every_writable_path(tmp_path):
    runtime = tmp_path / "runtime"
    scan_root = tmp_path / "synthetic-root"
    resources = tmp_path / "read-only-resources"
    value = config.RuntimeConfig.from_env({
        "FATHOM_RUNTIME_DIR": str(runtime),
        "FATHOM_SCAN_ROOT": str(scan_root),
        "FATHOM_RESOURCE_DIR": str(resources),
        "FATHOM_PORT": "18799",
    }, project_root=tmp_path / "source", home=tmp_path / "fake-home")

    assert value.runtime_dir == runtime
    assert value.data_dir == runtime / "data"
    assert value.db_path == runtime / "data" / "fathom.db"
    assert value.reports_dir == runtime / "reports"
    assert value.logs_dir == runtime / "logs"
    assert value.scan_root == scan_root
    assert value.frontend_dir == resources / "frontend"
    assert value.port == 18799
    assert value.public_values()["runtime_dir"] == str(runtime)


def test_release_default_uses_application_support_not_resources(tmp_path):
    home = tmp_path / "home"
    resources = tmp_path / "Fathom.app" / "Contents" / "Resources"
    value = config.RuntimeConfig.from_env({
        "FATHOM_RUNTIME_MODE": "release",
        "FATHOM_RESOURCE_DIR": str(resources),
    }, project_root=tmp_path / "source", home=home)

    assert value.runtime_dir == home / "Library" / "Application Support" / "Fathom"
    assert value.resource_dir == resources
    assert not str(value.db_path).startswith(str(resources))


def test_mode_override_changes_default_runtime_root(tmp_path):
    home = tmp_path / "home"
    value = config.RuntimeConfig.from_env(
        {}, project_root=tmp_path / "source", home=home
    ).with_overrides(mode="release")
    assert value.runtime_dir == home / "Library" / "Application Support" / "Fathom"
    assert value.db_path == value.runtime_dir / "data" / "fathom.db"


def test_legacy_fathom_db_now_isolates_all_writes(tmp_path):
    legacy_db = tmp_path / "isolated" / "custom.db"
    value = config.RuntimeConfig.from_env(
        {"FATHOM_DB": str(legacy_db)},
        project_root=tmp_path / "source",
        home=tmp_path / "home",
    )

    assert value.runtime_dir == legacy_db.parent
    assert value.db_path == legacy_db
    for path in (value.data_dir, value.reports_dir, value.logs_dir):
        assert path.is_relative_to(value.runtime_dir)
    assert value.with_overrides().db_path == legacy_db


def test_invalid_relative_path_port_and_split_db_fail_closed(tmp_path):
    with pytest.raises(config.ConfigurationError, match="绝对路径"):
        config.RuntimeConfig.from_env(
            {"FATHOM_RUNTIME_DIR": "relative"}, project_root=tmp_path, home=tmp_path
        )
    with pytest.raises(config.ConfigurationError, match="1..65535"):
        config.RuntimeConfig.from_env(
            {"FATHOM_PORT": "0"}, project_root=tmp_path, home=tmp_path
        )
    with pytest.raises(config.ConfigurationError, match="必须是"):
        config.RuntimeConfig.from_env({
            "FATHOM_RUNTIME_DIR": str(tmp_path / "runtime"),
            "FATHOM_DB": str(tmp_path / "outside.db"),
        }, project_root=tmp_path, home=tmp_path)
    for conflict in ("", "data", "reports", "logs"):
        with pytest.raises(config.ConfigurationError, match="目录冲突"):
            config.RuntimeConfig.from_env({
                "FATHOM_RUNTIME_DIR": str(tmp_path / "runtime"),
                "FATHOM_DB": str(tmp_path / "runtime" / conflict),
            }, project_root=tmp_path, home=tmp_path)


def test_runtime_directory_creation_never_touches_resource_tree(tmp_path):
    runtime = tmp_path / "runtime"
    resources = tmp_path / "readonly"
    resources.mkdir()
    resources.chmod(0o555)
    value = config.RuntimeConfig.from_env({
        "FATHOM_RUNTIME_DIR": str(runtime),
        "FATHOM_RESOURCE_DIR": str(resources),
    }, project_root=tmp_path / "source", home=tmp_path / "home")
    before = list(resources.iterdir())

    config.ensure_runtime_dirs(value)

    assert [value.data_dir, value.reports_dir, value.logs_dir] == [
        runtime / "data", runtime / "reports", runtime / "logs"
    ]
    assert all(path.is_dir() for path in (value.data_dir, value.reports_dir, value.logs_dir))
    assert list(resources.iterdir()) == before


def test_cli_scan_from_unrelated_cwd_stays_inside_explicit_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    scan_root = tmp_path / "synthetic-root"
    unrelated = tmp_path / "unrelated-cwd"
    scan_root.mkdir()
    unrelated.mkdir()
    (scan_root / "small.txt").write_text("synthetic", encoding="utf-8")
    port = _unused_port()

    proc = subprocess.run(
        [sys.executable, str(MAIN), "--runtime-dir", str(runtime),
         "--scan-root", str(scan_root), "--port", str(port), "scan"],
        cwd=unrelated,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    db_path = runtime / "data" / "fathom.db"
    assert db_path.is_file()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT root FROM snapshots").fetchone()[0] == str(scan_root)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()
    assert not list(unrelated.iterdir())
    assert not (scan_root / "data").exists()
    assert not (scan_root / "reports").exists()
    assert not (scan_root / "logs").exists()


def test_real_api_reports_effective_runtime_values_from_unrelated_cwd(tmp_path):
    runtime = tmp_path / "runtime"
    scan_root = tmp_path / "synthetic-root"
    unrelated = tmp_path / "unrelated-cwd"
    resources = tmp_path / "read-only-resources"
    frontend = resources / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>isolated</title>")
    frontend.chmod(0o555)
    resources.chmod(0o555)
    scan_root.mkdir()
    unrelated.mkdir()
    port = _unused_port()
    proc = subprocess.Popen(
        [sys.executable, str(MAIN), "--runtime-dir", str(runtime),
         "--scan-root", str(scan_root), "--port", str(port),
         "--resource-dir", str(resources), "serve"],
        cwd=unrelated,
        env=_clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        body = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.5) as response:
                    body = json.load(response)
                    break
            except OSError:
                time.sleep(0.05)
        assert body is not None, (proc.poll(), proc.stderr.read() if proc.poll() is not None else "")
        assert body["root"] == str(scan_root)
        assert body["port"] == port
        assert body["runtime"] == {
            "mode": "development",
            "runtime_dir": str(runtime),
            "data_dir": str(runtime / "data"),
            "reports_dir": str(runtime / "reports"),
            "logs_dir": str(runtime / "logs"),
            "db_path": str(runtime / "data" / "fathom.db"),
            "scan_root": str(scan_root),
            "resource_dir": str(resources),
            "frontend_dir": str(frontend),
            "host": "127.0.0.1",
            "port": port,
            "schema_version": db.SCHEMA_VERSION,
        }
        with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            assert b"isolated" in response.read()
        assert not list(unrelated.iterdir())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_occupied_port_exits_nonzero_without_touching_owner(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        proc = subprocess.run(
            [sys.executable, str(MAIN), "--runtime-dir", str(tmp_path / "runtime"),
             "--scan-root", str(tmp_path), "--port", str(port), "serve"],
            cwd=tmp_path,
            env=_clean_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 3
        assert "已被占用" in proc.stderr and "未触碰占用进程" in proc.stderr
        assert listener.getsockname()[1] == port
    finally:
        listener.close()


class TestDuTimeoutConfig:
    """ISS-061：du 超时上限由 FATHOM_DU_TIMEOUT_S 配置；默认值与拒绝坏值
    在 config 模块加载时点完成，扫描器和协调器随后只读这一常量。

    实现说明：加载时点的判定用**子进程**执行（每次 import fathom.config 都是
    干净进程）。理由：在同一进程里连续 importlib.reload 一个会在加载期抛异常的
    模块，会把模块对象留在半初始化状态——既可能让 pytest.raises 取到与抛出方
    不同一的异常类而漏捕，也会污染后续用例（实测表现为本文件与
    test_scan_coordination 同跑时才失败）。子进程隔离同时覆盖“值生效”与
    “坏值 fail-closed”两条，且不留下任何跨用例状态。
    """

    @staticmethod
    def _load_config_probe(env_value: str | None) -> subprocess.CompletedProcess:
        """在干净子进程里 import fathom.config 并打印 DU_TIMEOUT_S。"""
        env = dict(os.environ)
        if env_value is None:
            env.pop("FATHOM_DU_TIMEOUT_S", None)
        else:
            env["FATHOM_DU_TIMEOUT_S"] = env_value
        code = (
            "import json;"
            "from fathom import config;"
            "print(json.dumps({'du_timeout_s': config.DU_TIMEOUT_S}))"
        )
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
        )

    def test_default_when_env_unset(self):
        """未设 FATHOM_DU_TIMEOUT_S 时使用文档化的 14400s 默认值。
        反例：原硬编码 3600 在生产 /Users/maoking（~11M 文件）上无解释地
        截断扫描；现默认 4 小时为兼容基线，运维可显式覆盖。"""
        proc = self._load_config_probe(None)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout.strip().splitlines()[-1])["du_timeout_s"] == 14400.0

    def test_non_finite_values_fail_closed(self):
        """ISS-061 审查观察：nan/inf 会绕过单纯的大小比较而静默解除安全时限。
        nan 与任何值比较均为 False，inf > 0 为真——两者都必须 fail closed，
        否则等于把超时防护关掉。1e400 溢出为 inf，同样必须拒绝。"""
        for raw in ("nan", "inf", "-inf", "1e400"):
            proc = self._load_config_probe(raw)
            assert proc.returncode != 0, f"FATHOM_DU_TIMEOUT_S={raw} 必须 fail closed"
            assert "有限" in proc.stderr or "正数" in proc.stderr, proc.stderr

    def test_env_override_takes_precedence(self):
        """合法 FATHOM_DU_TIMEOUT_S 覆盖默认值；非法值必须 fail closed。"""
        ok = self._load_config_probe("5.5")
        assert ok.returncode == 0, ok.stderr
        assert json.loads(ok.stdout.strip().splitlines()[-1])["du_timeout_s"] == 5.5

        zero = self._load_config_probe("0")
        assert zero.returncode != 0, "FATHOM_DU_TIMEOUT_S=0 必须 fail closed"
        assert "正" in zero.stderr  # 消息为“必须是正的有限浮点数…”

        bad = self._load_config_probe("not-a-number")
        assert bad.returncode != 0, "非数字值必须 fail closed"
        assert "正浮点数" in bad.stderr

    def test_negative_value_fail_closed(self):
        """ISS-062：-1 是可解析的有限浮点数，必须被 ``<= 0`` 分支拒绝。

        依据 fathom/config.py 的 FATHOM_DU_TIMEOUT_S 段：非空白先 float()
        解析，再要求 ``math.isfinite`` 且 ``> 0``——负数落入「正的有限
        浮点数」错误，不能因为可解析而静默生效（负时限等于关掉防护）。"""
        negative = self._load_config_probe("-1")
        assert negative.returncode != 0, "FATHOM_DU_TIMEOUT_S=-1 必须 fail closed"
        assert "正" in negative.stderr  # 消息为“必须是正的有限浮点数…”

    def test_empty_string_falls_back_to_default(self):
        """ISS-062：空串按 unset 回落默认 14400（断言现状语义，不改 config.py）。

        依据 fathom/config.py 加载段 ``if _raw is None or not _raw.strip()``：
        strip 后为空的值视同未设置，先于 float() 解析短路，不进入坏值拒绝。"""
        blank = self._load_config_probe("")
        assert blank.returncode == 0, blank.stderr
        assert (
            json.loads(blank.stdout.strip().splitlines()[-1])["du_timeout_s"]
            == 14400.0
        )

    def test_whitespace_only_falls_back_to_default(self):
        """ISS-062：纯空白与空串同语义——strip 后为空即 unset，回落默认。

        依据同上：``"  ".strip()`` 为空串，走同一 unset 分支而不是
        ``float("  ")`` 的 ValueError 路径。"""
        blank = self._load_config_probe("  ")
        assert blank.returncode == 0, blank.stderr
        assert (
            json.loads(blank.stdout.strip().splitlines()[-1])["du_timeout_s"]
            == 14400.0
        )
