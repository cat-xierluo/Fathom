"""ISS-016B：服务重载一致性（只读漂移检测）的端到端合同。

覆盖：
1. 纯函数 ``launchd.service_reload_state``：in_sync / drift（差 1 分钟也
   必须抓到）/ not_registered / unknown 四态与「绝不猜」边界（registered
   形状异常、current_scan_time 非法 → unknown）；
2. 只读解析 ``launchd.read_registered_scan_time``：fake plist 文件注入
   （本切片自己的 plist 生成器同源产物）覆盖 ok / 文件缺失 not_registered /
   损坏 plist、缺 StartCalendarInterval、缺 Hour、Hour 越界或类型异常、
   读取抛异常 → unknown；且全程零写入零命令执行（monkeypatch 守卫）；
3. ``GET/PUT /api/config`` 的 ``service_reload_state`` 结构升级 + 向后兼容：
   旧字段（``service_reload`` 字符串、``hint``、``applied``、ISS-016A 的
   全部 GET 键）不破坏——旧断言全绿由本文件的兼容测试复刻证明；
4. grep 守卫：本切片 diff（vs main 基线）在 ``fathom/`` 与 ``frontend/``
   生产代码中新增的行不含 launchctl/bootstrap/bootout/SMAppService——
   零新增系统调用点（写路径只经 010B 桥的既有 confirmed 流）。

测试隔离沿用 test_api_config.py 的夹具风格：全部可写路径指向临时目录，
``config.LAUNCHAGENTS_DIR`` 一并指向临时目录（绝不读写本机
~/Library/LaunchAgents 与真实系统命令）。
"""

from __future__ import annotations

import inspect
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, launchd

# git-diff 守卫只看生产代码：tests/ 自身含有被禁词的字面量（探针需要），
# scripts/ 的浏览器夹具含桥数据形状（launchctl 命令字符串是 mock 数据，
# 不是调用点）。
_GUARD_PATHS = ("fathom", "frontend")
_GUARD_FORBIDDEN_TOKENS = ("launchctl", "bootstrap", "bootout", "SMAppService")

_PLIST_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0">\n<dict>\n{body}</dict>\n</plist>\n'
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """运行根/扫描根/DB/LaunchAgents 全部指向临时目录（test_api_config 同款）。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    scanroot = tmp_path / "scanroot"
    scanroot.mkdir()
    agents = tmp_path / "LaunchAgents"
    cfg = config.RuntimeConfig.from_env(
        {"FATHOM_RUNTIME_DIR": str(runtime), "FATHOM_SCAN_ROOT": str(scanroot)},
        project_root=tmp_path, home=tmp_path / "home",
    )
    monkeypatch.setattr(config, "_ACTIVE", cfg)
    monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings())
    monkeypatch.setattr(config, "_CLI_SCAN_ROOT_PINNED", False)
    monkeypatch.setattr(config, "DATA_DIR", cfg.data_dir)
    monkeypatch.setattr(config, "REPORTS_DIR", cfg.reports_dir)
    monkeypatch.setattr(config, "LOGS_DIR", cfg.logs_dir)
    monkeypatch.setattr(config, "DB_PATH", cfg.db_path)
    monkeypatch.setattr(config, "FRONTEND_DIR", cfg.frontend_dir)
    monkeypatch.setattr(config, "DEFAULT_ROOT", cfg.scan_root)
    monkeypatch.setattr(config, "PORT", cfg.port)
    for name in ("MIN_DIR_KB", "FREE_ALERT_GB", "SCAN_HOUR", "SCAN_MINUTE"):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(config, "EXCLUDE_NAMES", [])
    # ISS-016B 关键隔离：漂移检测读取的 plist 目录指向临时目录。
    monkeypatch.setattr(config, "LAUNCHAGENTS_DIR", agents)
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())


@pytest.fixture
def agents_dir(tmp_path) -> Path:
    return tmp_path / "LaunchAgents"


@pytest.fixture
def client():
    """与真实客户端同一合同：合法 Host（base_url）+ 写令牌（bootstrap 后注入）。"""
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


def _write_registered_plist(agents: Path, hour: int, minute: int) -> Path:
    """用本仓自己的 scan plist 生成器写入已注册计划（与生产同源）。"""
    agents.mkdir(parents=True, exist_ok=True)
    content = launchd._scan_plist_argv(
        ["/fake/helper", "scan", "--source", "scheduled"], hour, minute,
        agents.parent / "logs",
    )
    path = agents / f"{config.SCAN_LABEL}.plist"
    path.write_text(content, encoding="utf-8")
    return path


def _write_raw_plist(agents: Path, body: str) -> None:
    """手写畸形/缺字段的 plist（构造 unknown 分支用）。"""
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{config.SCAN_LABEL}.plist").write_text(
        _PLIST_TEMPLATE.format(body=body), encoding="utf-8")


# ---------- 纯函数 service_reload_state：四态与边界 ----------

def test_state_in_sync_when_times_equal():
    result = launchd.service_reload_state(
        "09:30", {"read": "ok", "scan_time": "09:30"})
    assert result == {
        "state": "in_sync",
        "registered_scan_time": "09:30",
        "current_scan_time": "09:30",
    }


def test_state_drift_catches_one_minute_difference():
    """差 1 分钟也必须判 drift——不能只比小时。"""
    result = launchd.service_reload_state(
        "09:30", {"read": "ok", "scan_time": "09:31"})
    assert result["state"] == "drift"
    assert result["registered_scan_time"] == "09:31"
    assert result["current_scan_time"] == "09:30"


def test_state_drift_across_hour_boundary():
    result = launchd.service_reload_state(
        "10:00", {"read": "ok", "scan_time": "09:59"})
    assert result["state"] == "drift"


def test_state_not_registered_maps_through():
    result = launchd.service_reload_state(
        "09:30", {"read": "not_registered", "scan_time": None})
    assert result == {
        "state": "not_registered",
        "registered_scan_time": None,
        "current_scan_time": "09:30",
    }


def test_state_unknown_when_read_unknown():
    result = launchd.service_reload_state(
        "09:30", {"read": "unknown", "scan_time": None})
    assert result["state"] == "unknown"
    assert result["registered_scan_time"] is None


@pytest.mark.parametrize("registered", [
    None,
    {},
    {"read": "ok"},
    {"read": "ok", "scan_time": None},
    {"read": "ok", "scan_time": "garbage"},
    {"read": "weird-value"},
    "not-a-dict",
    42,
])
def test_state_unknown_for_malformed_registered_shapes(registered):
    """registered 形状异常 → unknown，绝不从残缺数据猜一个时间。"""
    result = launchd.service_reload_state("09:30", registered)
    assert result["state"] == "unknown"
    assert result["registered_scan_time"] is None


@pytest.mark.parametrize("current", [
    "9:00",      # 缺前导零（config 校验会拒，纯函数同样不猜）
    "25:00",     # 小时越界
    "12:60",     # 分钟越界
    "0930",
    "",
    None,
    930,
    "09:30:00",
])
def test_state_unknown_when_current_scan_time_invalid(current):
    result = launchd.service_reload_state(
        current, {"read": "ok", "scan_time": "09:30"})
    assert result["state"] == "unknown"
    assert result["registered_scan_time"] == "09:30"


def test_state_boundary_midnight_and_last_minute():
    assert launchd.service_reload_state(
        "00:00", {"read": "ok", "scan_time": "00:00"})["state"] == "in_sync"
    assert launchd.service_reload_state(
        "23:59", {"read": "ok", "scan_time": "23:59"})["state"] == "in_sync"
    assert launchd.service_reload_state(
        "00:00", {"read": "ok", "scan_time": "23:59"})["state"] == "drift"


# ---------- 只读解析 read_registered_scan_time（fake plist 注入） ----------

def test_read_ok_from_same_source_plist_generator(agents_dir):
    path = _write_registered_plist(agents_dir, hour=9, minute=30)
    result = launchd.read_registered_scan_time(launchagents_dir=agents_dir)
    assert result["read"] == "ok"
    assert result["scan_time"] == "09:30"
    assert result["plist_path"] == str(path)


def test_read_not_registered_when_plist_missing(agents_dir):
    agents_dir.mkdir(parents=True, exist_ok=True)
    result = launchd.read_registered_scan_time(launchagents_dir=agents_dir)
    assert result == {
        "read": "not_registered",
        "scan_time": None,
        "plist_path": str(agents_dir / f"{config.SCAN_LABEL}.plist"),
    }


def test_read_not_registered_when_dir_missing(agents_dir):
    result = launchd.read_registered_scan_time(launchagents_dir=agents_dir)
    assert result["read"] == "not_registered"


def test_read_unknown_when_plist_corrupt(agents_dir):
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{config.SCAN_LABEL}.plist").write_text(
        "这不是 plist（损坏内容）", encoding="utf-8")
    result = launchd.read_registered_scan_time(launchagents_dir=agents_dir)
    assert result["read"] == "unknown"
    assert result["scan_time"] is None


def test_read_unknown_when_binary_garbage(agents_dir):
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{config.SCAN_LABEL}.plist").write_bytes(b"\x00\x01bplistXX")
    assert launchd.read_registered_scan_time(
        launchagents_dir=agents_dir)["read"] == "unknown"


def test_read_unknown_when_start_calendar_interval_missing(agents_dir):
    """web 形 plist（无 StartCalendarInterval）不能被当成计划读出。"""
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{config.SCAN_LABEL}.plist").write_text(
        launchd._web_plist_argv(["/fake/helper", "serve"], agents_dir.parent / "logs"),
        encoding="utf-8")
    result = launchd.read_registered_scan_time(launchagents_dir=agents_dir)
    assert result["read"] == "unknown"


@pytest.mark.parametrize("interval_body", [
    # 缺 Hour（只有 Minute）
    "    <key>StartCalendarInterval</key>\n    <dict>\n"
    "        <key>Minute</key>\n        <integer>30</integer>\n    </dict>\n",
    # 缺 Minute（只有 Hour）
    "    <key>StartCalendarInterval</key>\n    <dict>\n"
    "        <key>Hour</key>\n        <integer>9</integer>\n    </dict>\n",
    # Hour 越界
    "    <key>StartCalendarInterval</key>\n    <dict>\n"
    "        <key>Hour</key>\n        <integer>24</integer>\n"
    "        <key>Minute</key>\n        <integer>0</integer>\n    </dict>\n",
    # Minute 越界（负）
    "    <key>StartCalendarInterval</key>\n    <dict>\n"
    "        <key>Hour</key>\n        <integer>9</integer>\n"
    "        <key>Minute</key>\n        <integer>-1</integer>\n    </dict>\n",
    # Hour 是字符串（类型异常）
    "    <key>StartCalendarInterval</key>\n    <dict>\n"
    "        <key>Hour</key>\n        <string>9</string>\n"
    "        <key>Minute</key>\n        <integer>30</integer>\n    </dict>\n",
    # Hour 是布尔（plistlib 会解析 <true/> 为 True，是 int 子类，必须显式拒）
    "    <key>StartCalendarInterval</key>\n    <dict>\n"
    "        <key>Hour</key>\n        <true/>\n"
    "        <key>Minute</key>\n        <integer>30</integer>\n    </dict>\n",
    # StartCalendarInterval 是数组（launchd 允许数组形态，本切片不猜语义）
    "    <key>StartCalendarInterval</key>\n    <array>\n"
    "        <dict>\n            <key>Hour</key>\n            <integer>9</integer>\n"
    "            <key>Minute</key>\n            <integer>30</integer>\n        </dict>\n"
    "    </array>\n",
])
def test_read_unknown_for_missing_or_bad_interval_fields(agents_dir, interval_body):
    _write_raw_plist(agents_dir, interval_body)
    result = launchd.read_registered_scan_time(launchagents_dir=agents_dir)
    assert result["read"] == "unknown"
    assert result["scan_time"] is None


def test_read_unknown_when_file_read_raises(agents_dir, monkeypatch):
    _write_registered_plist(agents_dir, hour=9, minute=30)

    def broken_read_bytes(self):
        raise OSError("合成权限故障：拒绝读取")

    monkeypatch.setattr(Path, "read_bytes", broken_read_bytes)
    result = launchd.read_registered_scan_time(launchagents_dir=agents_dir)
    assert result["read"] == "unknown"


def test_read_defaults_to_config_launchagents_dir(agents_dir, monkeypatch):
    """不注入目录时用 config.LAUNCHAGENTS_DIR（夹具已指向临时目录）。"""
    result = launchd.read_registered_scan_time()
    assert result["plist_path"] == str(agents_dir / f"{config.SCAN_LABEL}.plist")
    assert result["read"] == "not_registered"


def test_read_and_state_never_write_or_execute(agents_dir, monkeypatch):
    """漂移检测是只读的：零命令执行、零 plist 写入（ISS-016B 硬边界）。"""
    _write_registered_plist(agents_dir, hour=9, minute=30)
    run_called = MagicMock(side_effect=AssertionError("漂移检测不允许执行命令"))
    write_called = MagicMock(side_effect=AssertionError("漂移检测不允许写 plist"))
    bootstrap_called = MagicMock(side_effect=AssertionError("漂移检测不允许注册"))
    monkeypatch.setattr(launchd.subprocess, "run", run_called)
    monkeypatch.setattr(launchd, "_write_plist", write_called)
    monkeypatch.setattr(launchd, "_write_plist_in", write_called)
    monkeypatch.setattr(launchd, "_bootstrap", bootstrap_called)

    registered = launchd.read_registered_scan_time(launchagents_dir=agents_dir)
    state = launchd.service_reload_state("09:30", registered)
    assert registered["read"] == "ok"
    assert state["state"] == "in_sync"
    run_called.assert_not_called()
    write_called.assert_not_called()
    bootstrap_called.assert_not_called()


# ---------- GET /api/config：service_reload_state + 兼容 ----------

def test_get_config_drift_state_with_fake_plist(client, agents_dir):
    _write_registered_plist(agents_dir, hour=12, minute=0)
    assert client.put("/api/config", json={"scan_time": "13:30"}).status_code == 200
    body = client.get("/api/config").json()
    assert body["service_reload_state"] == {
        "state": "drift",
        "registered_scan_time": "12:00",
        "current_scan_time": "13:30",
    }


def test_get_config_in_sync_state(client, agents_dir):
    _write_registered_plist(agents_dir, hour=9, minute=30)
    assert client.put("/api/config", json={"scan_time": "09:30"}).status_code == 200
    assert client.get("/api/config").json()["service_reload_state"] == {
        "state": "in_sync",
        "registered_scan_time": "09:30",
        "current_scan_time": "09:30",
    }


def test_get_config_in_sync_catches_one_minute_drift(client, agents_dir):
    _write_registered_plist(agents_dir, hour=9, minute=31)
    assert client.put("/api/config", json={"scan_time": "09:30"}).status_code == 200
    state = client.get("/api/config").json()["service_reload_state"]
    assert state["state"] == "drift"
    assert state["registered_scan_time"] == "09:31"


def test_get_config_not_registered_without_plist(client, agents_dir):
    assert client.put("/api/config", json={"scan_time": "13:30"}).status_code == 200
    state = client.get("/api/config").json()["service_reload_state"]
    assert state["state"] == "not_registered"
    assert state["registered_scan_time"] is None
    assert state["current_scan_time"] == "13:30"


def test_get_config_unknown_with_corrupt_plist(client, agents_dir):
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{config.SCAN_LABEL}.plist").write_text(
        "损坏 plist", encoding="utf-8")
    assert client.put("/api/config", json={"scan_time": "13:30"}).status_code == 200
    state = client.get("/api/config").json()["service_reload_state"]
    assert state["state"] == "unknown"
    assert state["registered_scan_time"] is None


def test_get_config_keeps_all_legacy_iss016a_fields(client):
    """兼容：ISS-016A 的 GET 键一个不少（旧消费方不破坏）。"""
    body = client.get("/api/config").json()
    for key in ("scan_root", "scan_time", "min_kb", "free_alert_gb",
                "exclude_names", "sources", "defaults", "policies",
                "settings_path"):
        assert key in body, f"GET /api/config 丢了 ISS-016A 既有键 {key}"
    assert body["scan_time"] == "12:00"
    assert body["sources"]["scan_time"] == "default"


def test_get_config_reload_read_failure_degrades_to_unknown(client, monkeypatch):
    """漂移检测自身抛异常 → state=unknown，GET 绝不 5xx、绝不猜。"""
    def broken_read(launchagents_dir=None):
        raise RuntimeError("合成未预期故障")

    monkeypatch.setattr(launchd, "read_registered_scan_time", broken_read)
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["service_reload_state"]["state"] == "unknown"


def test_get_config_without_state_field_shape_defended(client):
    """monkeypatch 返回畸形结构 → unknown（形状校验，不冒充）。"""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(launchd, "read_registered_scan_time", lambda **kw: None)
        state = client.get("/api/config").json()["service_reload_state"]
        assert state["state"] == "unknown"
    finally:
        monkeypatch.undo()


# ---------- PUT /api/config：新结构 + 旧合同兼容 ----------

def test_put_config_keeps_legacy_string_contract(client, agents_dir):
    """兼容测试（复刻 test_api_config 旧断言）：旧字符串消费方不破坏。"""
    _write_registered_plist(agents_dir, hour=12, minute=0)
    response = client.put("/api/config", json={"scan_time": "13:30"})
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["service_reload"] == "requires_user_action"
    assert "重新安装" in body["hint"]
    assert "settings.json" in body["hint"]


def test_put_config_returns_reload_state_after_apply(client, agents_dir):
    _write_registered_plist(agents_dir, hour=12, minute=0)
    body = client.put("/api/config", json={"scan_time": "13:30"}).json()
    assert body["service_reload_state"] == {
        "state": "drift",
        "registered_scan_time": "12:00",
        "current_scan_time": "13:30",
    }
    # 嵌套 config 视图同样携带（前端保存后直接渲染）
    assert body["config"]["service_reload_state"]["state"] == "drift"
    # 再保存成与注册一致的时间 → in_sync
    body2 = client.put("/api/config", json={"scan_time": "12:00"}).json()
    assert body2["service_reload_state"]["state"] == "in_sync"
    assert body2["config"]["service_reload_state"]["state"] == "in_sync"


def test_put_config_not_registered_and_unknown_states(client, agents_dir):
    assert client.put(
        "/api/config", json={"scan_time": "13:30"}
    ).json()["service_reload_state"]["state"] == "not_registered"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{config.SCAN_LABEL}.plist").write_bytes(b"\x00corrupt")
    assert client.put(
        "/api/config", json={"scan_time": "13:30"}
    ).json()["service_reload_state"]["state"] == "unknown"


def test_put_config_hint_describes_drift_with_both_times(client, agents_dir):
    _write_registered_plist(agents_dir, hour=12, minute=0)
    hint = client.put("/api/config", json={"scan_time": "13:30"}).json()["hint"]
    assert "重新安装" in hint  # 基础提示保留（旧断言）
    assert "不一致" in hint
    assert "12:00" in hint and "13:30" in hint


def test_put_config_hint_says_no_reinstall_needed_when_in_sync(client, agents_dir):
    _write_registered_plist(agents_dir, hour=12, minute=0)
    hint = client.put("/api/config", json={"scan_time": "12:00"}).json()["hint"]
    assert "无需重装" in hint


# ---------- grep 守卫：本切片零新增系统调用点 ----------

def _diff_added_lines_against_main() -> list[str]:
    """本切片 diff（vs origin/main，含工作区未提交改动）中新增的行。

    非仓库环境（无 git 或无基线引用）跳过：该守卫服务于切片评审，
    不作为离线运行 pytest 的硬依赖。"""
    for ref in ("origin/main", "main"):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            diff = subprocess.run(
                ["git", "diff", "-U0", ref, "--", *_GUARD_PATHS],
                capture_output=True, text=True,
            )
            if diff.returncode != 0:
                pytest.skip(f"git diff 不可用：{diff.stderr.strip()}")
            return [line[1:] for line in diff.stdout.splitlines()
                    if line.startswith("+") and not line.startswith("+++")]
    pytest.skip("无 git 基线引用可做 diff 守卫（非仓库环境）")


def test_slice_diff_adds_no_new_system_call_sites():
    """grep 断言：diff 引入的新系统调用点 = 0。

    ISS-016B 硬边界——零真实注册/重载：本切片不新增任何 launchctl/
    bootstrap/bootout/SMAppService 调用点；写路径只经 010B 桥的既有
    confirmed 流（其代码先于本切片存在，不在新增行中）。"""
    added = _diff_added_lines_against_main()
    offenders = [
        (i, line) for i, line in enumerate(added)
        if any(token in line for token in _GUARD_FORBIDDEN_TOKENS)
    ]
    assert offenders == [], (
        f"本切片在 fathom/ 与 frontend/ 生产代码中新增了系统调用点：{offenders[:5]}"
    )


def test_new_launchd_functions_are_read_only_and_exist():
    """反例先行：两个新函数必须存在，且体内零写动词、零执行调用。"""
    for name in ("read_registered_scan_time", "service_reload_state"):
        fn = getattr(launchd, name, None)
        assert fn is not None, f"launchd.{name} 必须存在（ISS-016B）"
        src = inspect.getsource(fn)
        # 去掉注释与 docstring 后的「可执行代码」
        code_lines = [ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#")]
        code = "\n".join(code_lines)
        for quote in ('"""', "'''"):
            while True:
                start = code.find(quote)
                if start == -1:
                    break
                end = code.find(quote, start + 3)
                code = code[:start] if end == -1 else code[:start] + code[end + 3:]
        for verb in ('"bootstrap"', '"bootout"', '"launchctl"', "SMAppService"):
            assert verb not in code, f"launchd.{name} 体内禁止出现 {verb}"
        for token in ("subprocess.run(", "run(", "_write_plist(", "_bootstrap("):
            assert token not in code, f"launchd.{name} 是只读函数，禁止执行调用 {token}"
