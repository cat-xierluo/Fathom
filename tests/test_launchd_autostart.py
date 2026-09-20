"""ISS-010A 只读桥 + ISS-010B 发行态注册桥的测试。

覆盖：
- ``fathom.launchd.status`` 三态映射矩阵（fake ``run`` 注入输出）
- 异常路径（超时、命令不存在、id -u 失败）→ unknown
- ``fathom.launchd.dry_run_plan`` / ``release_plan`` 纯展示不变量（与
  ``_scan_plist/_web_plist`` 同源、fake run 从未被调用）
- ISS-010B ``register_release``/``unregister_release``：fake ``run`` + 临时
  目录全覆盖（拒绝未确认、成功写 plist + bootstrap、bootstrap 失败回滚、
  写文件失败零 bootstrap、注销幂等、unlink 失败如实报错）
- grep 式守护（ISS-010B 升级为「写路径仅存在于注册命令模块」）：
  - Python 侧：launchctl 写动词（bootstrap/bootout/load/unload/enable/
    kickstart）字面量只允许出现在白名单函数（dev 态 ``_bootstrap``/
    ``install``/``uninstall`` + 发行态 ``register_release``/
    ``unregister_release`` + 纯展示 ``dry_run_plan``/``release_plan``）；
    其中真正执行（``run(``/``subprocess.run(``/``_write_plist(``/``_bootstrap(``）
    仅限注册命令模块；展示函数必须零执行
  - Rust 侧：``apps/desktop/src-tauri/src/*.rs`` 中写动词与 SMAppService
    调用仅允许出现在 ``autostart.rs`` 注册命令段；``lib.rs`` 必须 wiring
    register/unregister 命令；capabilities 必须授权四个 autostart 命令
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import textwrap
from pathlib import Path as _Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fathom import config, launchd

REPO_ROOT = _Path(__file__).resolve().parent.parent


# ----- 测试工具 ---------------------------------------------------------

class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run_factory(planned_outputs):
    """``planned_outputs`` 是 ``cmd_prefix`` -> ``_FakeCompletedProcess`` 映射。
    每次调用按 ``cmd[0]`` 命中；未命中则抛 ``FileNotFoundError``。
    ``calls`` 列表记录所有调用的 ``cmd`` 元组（argv list）。
    """

    calls: list[tuple] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(tuple(cmd))
        first = cmd[0] if cmd else ""
        if first not in planned_outputs:
            raise FileNotFoundError(f"fake_run: 未注册命令 {cmd!r}")
        return planned_outputs[first]

    return fake_run, calls


# ----- _parse_launchctl_print 三态矩阵 ----------------------------------

def test_parse_running_state_is_enabled():
    assert launchd._parse_launchctl_print(
        "section system\n\tstate = running\n\tpid = 12345\n", "", 0
    ) == "enabled"


def test_parse_pid_only_is_enabled():
    assert launchd._parse_launchctl_print("details:\n\tpid = 999\n", "", 0) == "enabled"


def test_parse_could_not_find_service_is_disabled():
    assert launchd._parse_launchctl_print(
        "", 'Could not find service: "gui/501/com.maoscripts.fathom-scan"', 3
    ) == "disabled"


def test_parse_permission_error_is_unknown():
    assert launchd._parse_launchctl_print(
        "Permission denied", "Permission denied: cannot access gui/501/x", 1
    ) == "unknown"


def test_parse_empty_zero_is_unknown():
    assert launchd._parse_launchctl_print("", "", 0) == "unknown"


def test_parse_command_not_found_is_unknown():
    assert launchd._parse_launchctl_print(
        "", "/bin/sh: launchctl: command not found", 127
    ) == "unknown"


def test_parse_disabled_keyword_with_exit_zero_is_unknown():
    # 关键词出现但 exit 0 → 必须保守为 unknown，不判 disabled。
    assert launchd._parse_launchctl_print(
        "Could not find service", "", 0
    ) == "unknown"


# ----- status() 集成测试（fake run） ------------------------------------

def test_status_both_enabled_with_realistic_output():
    run, calls = _fake_run_factory({
        "id": _FakeCompletedProcess(0, stdout="501\n"),
        "launchctl": _FakeCompletedProcess(
            0,
            stdout=(
                "section system\n"
                "\tstate = running\n"
                "\tpid = 9876\n"
                "\tlast exit code: 0\n"
            ),
        ),
    })
    result = launchd.status(run=run)
    assert set(result.keys()) == {config.SCAN_LABEL, config.WEB_LABEL}
    for label, record in result.items():
        assert record["state"] == "enabled", label
        assert record["pid"] == 9876, label
        assert record["last_exit"] == 0, label
    # 两标签各一次 launchctl + 一次 id -u，共 3 次调用。
    assert len(calls) == 3, calls
    assert calls[0][0] == "id"
    assert calls[1][0] == "launchctl"
    assert calls[2][0] == "launchctl"


def test_status_missing_label_is_disabled():
    run, _calls = _fake_run_factory({
        "id": _FakeCompletedProcess(0, stdout="501\n"),
        "launchctl": _FakeCompletedProcess(
            3,
            stderr='Could not find service: "gui/501/com.maoscripts.fathom-scan"',
        ),
    })
    result = launchd.status(run=run)
    for label, record in result.items():
        assert record["state"] == "disabled", label
        assert record["pid"] is None, label


def test_status_permission_error_is_unknown():
    run, _calls = _fake_run_factory({
        "id": _FakeCompletedProcess(0, stdout="501\n"),
        "launchctl": _FakeCompletedProcess(
            1,
            stdout="Permission denied",
            stderr="Permission denied: cannot access gui/501/x",
        ),
    })
    result = launchd.status(run=run)
    for record in result.values():
        assert record["state"] == "unknown"
        assert record["pid"] is None


def test_status_id_failure_makes_all_unknown_without_calling_launchctl():
    run, calls = _fake_run_factory({
        "id": _FakeCompletedProcess(1, stderr="id: illegal option"),
    })
    result = launchd.status(run=run)
    for record in result.values():
        assert record["state"] == "unknown"
        assert record["pid"] is None
    # 关键：id 失败时绝不再调 launchctl，避免无谓副作用。
    assert all(call[0] != "launchctl" for call in calls), calls


def test_status_launchctl_timeout_collapses_to_unknown():
    def hanging_run(cmd, *args, **kwargs):
        if cmd[0] == "id":
            return _FakeCompletedProcess(0, stdout="501\n")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 2.0))

    result = launchd.status(run=hanging_run)
    for record in result.values():
        assert record["state"] == "unknown", record


def test_status_launchctl_missing_collapses_to_unknown():
    def missing_run(cmd, *args, **kwargs):
        if cmd[0] == "id":
            return _FakeCompletedProcess(0, stdout="501\n")
        raise FileNotFoundError(2, "No such file", cmd[0])

    result = launchd.status(run=missing_run)
    for record in result.values():
        assert record["state"] == "unknown", record


def test_status_does_not_invoke_install_or_uninstall():
    """守护：``status`` 是只读查询，绝不能误调 ``install``/``uninstall``。
    通过监控 install/uninstall 的 fake 引用是否被触发实现。
    """
    install_called = MagicMock()
    uninstall_called = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(launchd, "install", install_called)
        mp.setattr(launchd, "uninstall", uninstall_called)
        run, _calls = _fake_run_factory({
            "id": _FakeCompletedProcess(0, stdout="501\n"),
            "launchctl": _FakeCompletedProcess(
                0, stdout="\tstate = running\n\tpid = 1\n"
            ),
        })
        launchd.status(run=run)
        install_called.assert_not_called()
        uninstall_called.assert_not_called()


# ----- dry_run_plan() --------------------------------------------------

def test_dry_run_plan_uses_same_plist_generators():
    plan = launchd.dry_run_plan()
    assert isinstance(plan, dict)
    plists = plan["plist_files"]
    assert isinstance(plists, list) and len(plists) == 2
    by_label = {p["label"]: p for p in plists}

    # 与 ``_scan_plist`` / ``_web_plist`` 同源：手动复算并断言相等。
    main_py = config.PROJECT_ROOT / "main.py"
    python = launchd.sys.executable
    assert by_label[config.SCAN_LABEL]["content"] == launchd._scan_plist(main_py, python)
    assert by_label[config.WEB_LABEL]["content"] == launchd._web_plist(main_py, python)


def test_dry_run_plan_paths_inside_launchagents_dir():
    plan = launchd.dry_run_plan()
    for entry in plan["plist_files"]:
        path = _Path(entry["path"]).resolve()
        launch_root = config.LAUNCHAGENTS_DIR.resolve()
        assert path.is_relative_to(launch_root), (
            f"plist 路径必须在 config.LAUNCHAGENTS_DIR 下：{path} vs {launch_root}"
        )
        assert path.name in {f"{config.SCAN_LABEL}.plist", f"{config.WEB_LABEL}.plist"}


def test_dry_run_plan_command_list_mentions_bootstrap_and_is_strings_only():
    plan = launchd.dry_run_plan()
    cmds = plan["commands"]
    assert isinstance(cmds, list) and len(cmds) >= 1
    # 所有命令必须是 list[str]（可序列化、可直接展示给用户审批）。
    for cmd in cmds:
        assert isinstance(cmd, list)
        assert all(isinstance(c, str) for c in cmd), cmd
    flat = " ".join(" ".join(c) for c in cmds)
    assert "bootstrap" in flat, "dry-run 计划必须包含 install() 将执行的 bootstrap 命令"
    assert "id" in flat, "应包含 id -u 占位"


def test_dry_run_plan_is_json_serializable_and_does_not_touch_fs():
    """dry-run 不允许写文件、不允许真执行命令。``_write_plist``/``_bootstrap``
    与 ``subprocess.run`` 都必须不被调用。"""
    import json

    with pytest.MonkeyPatch.context() as mp:
        write_called = MagicMock()
        bootstrap_called = MagicMock()
        run_called = MagicMock(side_effect=AssertionError("dry_run_plan 不应调用 subprocess"))
        mp.setattr(launchd, "_write_plist", write_called)
        mp.setattr(launchd, "_bootstrap", bootstrap_called)
        mp.setattr(launchd.subprocess, "run", run_called)

        plan = launchd.dry_run_plan()
        # 仍可 JSON 序列化
        json.dumps(plan, ensure_ascii=False)
        write_called.assert_not_called()
        bootstrap_called.assert_not_called()
        run_called.assert_not_called()


def test_dry_run_plan_does_not_call_install_or_uninstall():
    install_called = MagicMock()
    uninstall_called = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(launchd, "install", install_called)
        mp.setattr(launchd, "uninstall", uninstall_called)
        launchd.dry_run_plan()
        install_called.assert_not_called()
        uninstall_called.assert_not_called()


# ----- ISS-010B：发行态注册桥（fake run + 临时目录全覆盖） --------------
#
# 注：本节所有测试用 tmp_path 注入 launchagents 目录、fake run 注入命令
# 执行，绝不触碰本机 ~/Library/LaunchAgents 与真实 launchctl（ISS-010B
# 硬边界 1）。


def _fake_argv_run(planned, default_rc=0):
    """按完整 argv 前缀（cmd[0] + 关键词）命中计划结果的 fake run。

    ``planned``：``matcher``（取 argv tuple，返回 bool）-> (rc, stderr) 。
    未命中的 launchctl 调用默认 rc=0；``id -u`` 恒成功返回 501。
    所有调用记录在 ``calls``。
    """

    calls: list[tuple] = []

    def run(cmd, *args, **kwargs):
        argv = tuple(cmd)
        calls.append(argv)
        if argv[0] == "id":
            return _FakeCompletedProcess(0, stdout="501\n")
        for matcher, result in planned:
            if matcher(argv):
                rc, stderr = result
                return _FakeCompletedProcess(rc, stderr=stderr)
        return _FakeCompletedProcess(default_rc)

    return run, calls


def test_release_plan_is_pure_display_and_shows_plist_summary(tmp_path):
    """release_plan：纯展示——plist 内容与命令清单可审，但零执行零写。"""
    helper_bin = tmp_path / "Fathom.app" / "Contents" / "MacOS" / "helper"
    with pytest.MonkeyPatch.context() as mp:
        write_called = MagicMock()
        bootstrap_called = MagicMock()
        run_called = MagicMock(side_effect=AssertionError("release_plan 不应执行命令"))
        mp.setattr(launchd, "_write_plist", write_called)
        mp.setattr(launchd, "_bootstrap", bootstrap_called)
        mp.setattr(launchd.subprocess, "run", run_called)

        plan = launchd.release_plan(
            str(helper_bin),
            scan_hour=13,
            scan_minute=30,
            launchagents_dir=tmp_path / "LaunchAgents",
            logs_dir=tmp_path / "logs",
        )
        write_called.assert_not_called()
        bootstrap_called.assert_not_called()
        run_called.assert_not_called()

    json.dumps(plan, ensure_ascii=False)
    plists = {p["label"]: p for p in plan["plist_files"]}
    assert set(plists) == {config.SCAN_LABEL, config.WEB_LABEL}
    scan_content = plists[config.SCAN_LABEL]["content"]
    web_content = plists[config.WEB_LABEL]["content"]
    # ProgramArguments 是 helper 二进制 + 子命令（发行态），不再是 dev 态的
    # sys.executable + main.py。
    assert f"<string>{helper_bin}</string>" in scan_content
    assert "<string>scan</string>" in scan_content
    assert "<string>--source</string>" in scan_content
    assert "<string>scheduled</string>" in scan_content
    assert f"<integer>13</integer>" in scan_content  # scan_hour 注入生效
    assert f"<integer>30</integer>" in scan_content  # scan_minute 注入生效
    assert f"<string>{helper_bin}</string>" in web_content
    assert "<string>serve</string>" in web_content
    # 与 dev 态 plist 同源的结构键（RunAtLoad/KeepAlive 语义不因发行态丢失）
    assert "<key>RunAtLoad</key>" in web_content
    assert "<key>KeepAlive</key>" in web_content
    assert "<key>StartCalendarInterval</key>" in scan_content
    # 路径在注入的 launchagents 目录下
    for entry in plan["plist_files"]:
        assert _Path(entry["path"]).is_relative_to(tmp_path / "LaunchAgents")


def test_release_plan_commands_match_execution_argv(tmp_path):
    """release_plan 的命令清单 = register_release 将真实执行的 argv（含 uid 占位）。"""
    helper_bin = tmp_path / "helper"
    plan = launchd.release_plan(
        str(helper_bin), launchagents_dir=tmp_path / "LaunchAgents", logs_dir=tmp_path / "logs"
    )
    cmds = plan["commands"]
    assert all(isinstance(c, list) and all(isinstance(x, str) for x in c) for c in cmds)
    flat = [" ".join(c) for c in cmds]
    assert any(c.startswith("id -u") for c in flat)
    scan_path = tmp_path / "LaunchAgents" / f"{config.SCAN_LABEL}.plist"
    web_path = tmp_path / "LaunchAgents" / f"{config.WEB_LABEL}.plist"
    assert f"launchctl bootout gui/<uid> {scan_path}" in flat
    assert f"launchctl bootstrap gui/<uid> {scan_path}" in flat
    assert f"launchctl bootout gui/<uid> {web_path}" in flat
    assert f"launchctl bootstrap gui/<uid> {web_path}" in flat


def test_register_release_requires_confirmation(tmp_path):
    """confirmed=False：拒绝且零副作用（无文件、零命令）。"""
    run, calls = _fake_argv_run([])
    outcome = launchd.register_release(
        str(tmp_path / "helper"),
        confirmed=False,
        launchagents_dir=tmp_path / "LaunchAgents",
        logs_dir=tmp_path / "logs",
        run=run,
    )
    assert outcome["ok"] is False
    assert outcome.get("error")
    assert calls == [], calls
    assert not (tmp_path / "LaunchAgents").exists()


def test_register_release_uid_failure_refuses(tmp_path):
    uid_calls: list[tuple] = []

    def uid_run(cmd, *a, **k):
        uid_calls.append(tuple(cmd))
        return _FakeCompletedProcess(1, stderr="id: failed")

    outcome = launchd.register_release(
        str(tmp_path / "helper"),
        confirmed=True,
        launchagents_dir=tmp_path / "LaunchAgents",
        logs_dir=tmp_path / "logs",
        run=uid_run,
    )
    assert outcome["ok"] is False
    # id 失败后绝不能再碰 launchctl、不写任何文件
    assert all(c[0] != "launchctl" for c in uid_calls), uid_calls
    assert not (tmp_path / "LaunchAgents" / f"{config.SCAN_LABEL}.plist").exists()


def test_register_release_writes_plists_and_bootstraps_both(tmp_path):
    """成功路径：写两份发行态 plist + 每标签 bootout 清理 + bootstrap。"""
    helper_bin = tmp_path / "helper"
    agents = tmp_path / "LaunchAgents"
    run, calls = _fake_argv_run([])
    outcome = launchd.register_release(
        str(helper_bin),
        confirmed=True,
        scan_hour=13,
        scan_minute=30,
        launchagents_dir=agents,
        logs_dir=tmp_path / "logs",
        run=run,
    )
    assert outcome["ok"] is True, outcome
    assert outcome.get("rolled_back") is not True
    scan_path = agents / f"{config.SCAN_LABEL}.plist"
    web_path = agents / f"{config.WEB_LABEL}.plist"
    assert scan_path.is_file() and web_path.is_file()
    scan_content = scan_path.read_text(encoding="utf-8")
    assert f"<string>{helper_bin}</string>" in scan_content
    assert "<string>scan</string>" in scan_content
    assert "<integer>13</integer>" in scan_content
    # 命令序列：id -u → (bootout, bootstrap)×scan → (bootout, bootstrap)×web
    assert calls[0] == ("id", "-u"), calls
    launchctl_calls = [c for c in calls if c[0] == "launchctl"]
    assert launchctl_calls == [
        ("launchctl", "bootout", "gui/501", str(scan_path)),
        ("launchctl", "bootstrap", "gui/501", str(scan_path)),
        ("launchctl", "bootout", "gui/501", str(web_path)),
        ("launchctl", "bootstrap", "gui/501", str(web_path)),
    ], launchctl_calls


def test_register_release_bootstrap_failure_rolls_back_completely(tmp_path):
    """web bootstrap 失败：两标签都回滚（bootout + 删 plist），不留半注册态。"""
    helper_bin = tmp_path / "helper"
    agents = tmp_path / "LaunchAgents"
    web_path = agents / f"{config.WEB_LABEL}.plist"
    scan_path = agents / f"{config.SCAN_LABEL}.plist"

    def web_bootstrap_fails(argv):
        return argv[0] == "launchctl" and argv[1] == "bootstrap" and argv[3] == str(web_path)

    run, calls = _fake_argv_run([(web_bootstrap_fails, (5, "Bootstrap failed: 5: Input/output error"))])
    outcome = launchd.register_release(
        str(helper_bin),
        confirmed=True,
        launchagents_dir=agents,
        logs_dir=tmp_path / "logs",
        run=run,
    )
    assert outcome["ok"] is False, outcome
    assert outcome.get("rolled_back") is True
    assert "bootstrap" in outcome.get("error", "").lower()
    # 回滚后：目录里不留任何 plist
    assert not scan_path.exists()
    assert not web_path.exists()
    bootouts = [c for c in calls if c[:2] == ("launchctl", "bootout")]
    # 清理 bootout×2 + 回滚 bootout×2（两标签都撤销）
    assert len(bootouts) == 4, calls


def test_register_release_dir_creation_failure_bootstraps_nothing(tmp_path):
    """目标目录被同名文件占用（mkdir 失败）：零 launchctl 调用。"""
    agents = tmp_path / "LaunchAgents"
    agents.write_text("不是目录", encoding="utf-8")
    run, calls = _fake_argv_run([])
    outcome = launchd.register_release(
        str(tmp_path / "helper"),
        confirmed=True,
        launchagents_dir=agents,
        logs_dir=tmp_path / "logs",
        run=run,
    )
    assert outcome["ok"] is False
    assert all(c[0] != "launchctl" for c in calls), calls


def test_register_release_second_write_failure_cleans_first(tmp_path):
    """第二份 plist 写失败：第一份已写的必须清掉，零 bootstrap。"""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(parents=True)
    # 预置一个同名目录占住 web plist 路径 → write_text 必失败
    (agents / f"{config.WEB_LABEL}.plist").mkdir()
    run, calls = _fake_argv_run([])
    outcome = launchd.register_release(
        str(tmp_path / "helper"),
        confirmed=True,
        launchagents_dir=agents,
        logs_dir=tmp_path / "logs",
        run=run,
    )
    assert outcome["ok"] is False
    assert all(c[0] != "launchctl" for c in calls), calls
    # scan plist 已写入但被回滚清理
    assert not (agents / f"{config.SCAN_LABEL}.plist").exists()


def test_unregister_release_requires_confirmation(tmp_path):
    run, calls = _fake_argv_run([])
    outcome = launchd.unregister_release(
        confirmed=False, launchagents_dir=tmp_path / "LaunchAgents", run=run
    )
    assert outcome["ok"] is False
    assert calls == [], calls


def test_unregister_release_bootouts_and_unlinks(tmp_path):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in (config.SCAN_LABEL, config.WEB_LABEL):
        (agents / f"{label}.plist").write_text("<plist/>", encoding="utf-8")
    run, calls = _fake_argv_run([])
    outcome = launchd.unregister_release(confirmed=True, launchagents_dir=agents, run=run)
    assert outcome["ok"] is True, outcome
    assert calls[0] == ("id", "-u"), calls
    bootouts = [c for c in calls if c[:2] == ("launchctl", "bootout")]
    assert sorted(c[3] for c in bootouts) == sorted(
        [config.SCAN_LABEL, config.WEB_LABEL]
    ), bootouts
    # bootout 按 label（与 dev uninstall 同形）
    assert all(c[3] in (config.SCAN_LABEL, config.WEB_LABEL) for c in bootouts)
    assert not any((agents / f"{label}.plist").exists() for label in (config.SCAN_LABEL, config.WEB_LABEL))


def test_unregister_release_missing_plists_is_idempotent_ok(tmp_path):
    run, calls = _fake_argv_run([])
    outcome = launchd.unregister_release(
        confirmed=True, launchagents_dir=tmp_path / "LaunchAgents", run=run
    )
    assert outcome["ok"] is True, outcome
    bootouts = [c for c in calls if c[:2] == ("launchctl", "bootout")]
    assert len(bootouts) == 2  # 文件不在也照样 bootout（幂等清理）


def test_unregister_release_unlink_failure_reports_not_ok(tmp_path):
    """web plist 路径被目录占用（unlink 失败）：如实 ok=False，scan 仍被清。"""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{config.SCAN_LABEL}.plist").write_text("<plist/>", encoding="utf-8")
    (agents / f"{config.WEB_LABEL}.plist").mkdir()
    run, _calls = _fake_argv_run([])
    outcome = launchd.unregister_release(confirmed=True, launchagents_dir=agents, run=run)
    assert outcome["ok"] is False, outcome
    assert not (agents / f"{config.SCAN_LABEL}.plist").exists()
    assert (agents / f"{config.WEB_LABEL}.plist").is_dir()  # 占位目录不是我们的，保留


# ----- 静态不变量：grep 式守护 -----------------------------------------

def _function_code(name: str) -> str:
    """取 ``launchd.<name>`` 的完整函数源码（含嵌套 def）。

    ISS-010B 修复：旧实现逐行扫描、以「列 0 非注释行」为终止条件，会把
    多行签名里列 0 的 ``) -> dict:`` 误当函数结束，只抽到签名（ISS-010A
    的 status/dry_run_plan 均为多行签名，旧探针因此在半个函数体上空转）。
    改用 ``inspect.getsource`` 按函数对象精确取源；函数不存在返回空串，
    由调用方的 ``startswith`` 断言捕获（反例先行不允许安静通过）。"""
    fn = getattr(launchd, name, None)
    if fn is None:
        return ""
    try:
        return inspect.getsource(fn)
    except OSError:
        return ""


def _strip_comments_and_docstrings(src: str) -> str:
    """去掉 ``# ...`` 行注释与三引号字符串，剩余视为「可执行代码」。"""
    out_lines: list[str] = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out_lines.append(line)
    text = "\n".join(out_lines)
    # 去掉三引号 docstring。
    for quote in ('"""', "'''"):
        while True:
            start = text.find(quote)
            if start == -1:
                break
            end = text.find(quote, start + 3)
            if end == -1:
                # 未闭合 docstring：截到末尾。
                text = text[:start]
                break
            text = text[:start] + text[end + 3 :]
    return text


@pytest.mark.parametrize("fn_name", ["status", "dry_run_plan", "release_plan"])
def test_new_function_bodies_do_not_call_write_or_bootstrap(fn_name: str):
    body = _function_code(fn_name)
    # 空体 = 函数不存在：不允许「探针安静通过」（ISS-010B 反例先行要求探针
    # 对缺失的注册模块真实报红）。
    assert body.startswith(f"def {fn_name}("), f"launchd.{fn_name} 必须存在"
    code_only = _strip_comments_and_docstrings(body)
    # 「调用」指可执行代码中含 ``_write_plist(`` / ``_bootstrap(``；注释与
    # docstring 里出现的提及（仅文档说明）不计入。
    assert "_write_plist(" not in code_only, (
        f"{fn_name} 体内禁止直接调用 _write_plist"
    )
    assert "_bootstrap(" not in code_only, (
        f"{fn_name} 体内禁止直接调用 _bootstrap"
    )


def test_launchd_module_does_not_call_smappservice():
    """守护：SMAppService / objc 是父卡 ISS-010 范围，本切片不实现。
    模块级提及（注释/docstring）允许，但禁止出现 ``SMAppService.register``
    / ``SMAppService.unregister`` 之类的方法调用与 ``import objc``。"""
    src = inspect.getsource(launchd)
    code_only = _strip_comments_and_docstrings(src)
    for forbidden in ("SMAppService.register", "SMAppService.unregister", "import objc", "from objc"):
        assert forbidden not in code_only, f"代码中禁止出现 {forbidden}"


# ----- ISS-010B grep 守护升级：真实写路径仅存在于注册命令模块 -----------

# launchctl 写动词的**字符串字面量**（可执行代码中出现即视为「构造了写命令」）。
_WRITE_VERB_LITERALS = ('"bootstrap"', '"bootout"', '"load"', '"unload"', '"enable"', '"kickstart"')

# 允许出现写动词字面量的函数：dev 态写路径（_bootstrap/install/uninstall，
# main.py install|uninstall 显式入口）+ 发行态注册命令模块
# （register_release/unregister_release）+ 纯展示（dry_run_plan/release_plan，
# 命令以字符串形式呈现给用户审批）。
_VERB_ALLOWED_FUNCS = {
    "_bootstrap",
    "install",
    "uninstall",
    "register_release",
    "unregister_release",
    "dry_run_plan",
    "release_plan",
}

# 其中允许**真正执行**写命令（run/subprocess.run/_write_plist/_bootstrap 调用）
# 的函数——「注册命令模块」的 Python 侧定义。展示函数零执行。
_WRITE_EXECUTOR_FUNCS = {
    "_bootstrap",
    "install",
    "uninstall",
    "register_release",
    "unregister_release",
}

_EXECUTION_TOKENS = ("subprocess.run(", "_write_plist(", "_bootstrap(", "run(")


def test_python_write_verbs_confined_to_register_command_module():
    """launchd.py：写动词字面量只允许出现在白名单函数；展示函数零执行。"""
    src = inspect.getsource(launchd)
    top_level_fns = re.findall(r"^def ([A-Za-z_]\w*)\(", src, re.MULTILINE)
    assert top_level_fns, "launchd.py 应存在顶层函数"
    for name in top_level_fns:
        code_only = _strip_comments_and_docstrings(_function_code(name))
        has_verb = any(verb in code_only for verb in _WRITE_VERB_LITERALS)
        if not has_verb:
            continue
        assert name in _VERB_ALLOWED_FUNCS, (
            f"launchd.{name} 构造了 launchctl 写命令但不属于注册命令模块或"
            f" dev 态安装路径/纯展示白名单：{_VERB_ALLOWED_FUNCS}"
        )
        if name not in _WRITE_EXECUTOR_FUNCS:
            # 纯展示函数（dry_run_plan/release_plan）：只许字符串、不许执行。
            for token in _EXECUTION_TOKENS:
                assert token not in code_only, (
                    f"launchd.{name} 是展示函数，禁止出现执行调用 {token}"
                )


def test_register_release_is_the_unique_release_write_module():
    """反例先行的探针主体：发行态写模块必须存在且确有写路径（唯一命中）。
    实现缺失时本测试红——证明守护真的拦得住「写路径漂移到别处」。"""
    assert hasattr(launchd, "register_release"), (
        "ISS-010B：launchd.register_release（发行态注册写模块）必须存在"
    )
    assert hasattr(launchd, "unregister_release"), (
        "ISS-010B：launchd.unregister_release（发行态注销写模块）必须存在"
    )
    reg = _strip_comments_and_docstrings(_function_code("register_release"))
    assert '"bootstrap"' in reg, "register_release 必须真实构造 bootstrap 命令"
    assert any(token in reg for token in _EXECUTION_TOKENS), (
        "register_release 必须经 run/subprocess 真实执行写命令"
    )
    unreg = _strip_comments_and_docstrings(_function_code("unregister_release"))
    assert '"bootout"' in unreg, "unregister_release 必须真实构造 bootout 命令"
    assert any(token in unreg for token in _EXECUTION_TOKENS), (
        "unregister_release 必须经 run/subprocess 真实执行写命令"
    )


# Rust 侧：src-tauri 源码的写动词 / SMAppService 守护。
_SRC_TAURI_DIR = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "src"


def _rust_code_without_line_comments(path: _Path) -> str:
    """去掉 ``//`` 行注释（含 ``//!``/``///`` 文档注释）后的代码文本。

    本仓 Rust 源码不使用 ``/* */`` 块注释（基线 grep 零命中），此处不处理。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("//"))


def test_rust_write_verbs_only_in_autostart_module():
    """src-tauri：launchctl 写动词与 SMAppService 调用只允许出现在 autostart.rs。"""
    rust_files = sorted(_SRC_TAURI_DIR.glob("*.rs"))
    assert rust_files, f"未找到 Rust 源码目录：{_SRC_TAURI_DIR}"
    for path in rust_files:
        code = _rust_code_without_line_comments(path)
        if path.name == "autostart.rs":
            # 注册命令模块本体：必须确有写路径（缺失即红——反例先行）。
            assert '"bootstrap"' in code and '"bootout"' in code, (
                "autostart.rs 必须是唯一承载真实写路径的注册命令模块"
            )
            assert "SMAppService" not in code, (
                "autostart.rs 代码中禁止 SMAppService 调用（需 objc 桥，本切片不引入）"
            )
            continue
        for verb in _WRITE_VERB_LITERALS:
            assert verb not in code, (
                f"{path.name} 不允许出现 launchctl 写动词 {verb}："
                "真实写路径只允许存在于 autostart.rs 注册命令模块"
            )
        assert "SMAppService" not in code, (
            f"{path.name} 代码中禁止 SMAppService 调用（需 objc 桥，本切片不引入）"
        )


def test_rust_register_commands_take_confirmation_param():
    """注册命令必须接受 confirmed 参数（同意流的代码侧闸门）。"""
    code = _rust_code_without_line_comments(_SRC_TAURI_DIR / "autostart.rs")
    for piece in (
        "pub fn autostart_register",
        "pub fn autostart_unregister",
    ):
        assert piece in code, f"autostart.rs 缺少命令 {piece}"
    for m in re.finditer(r"pub fn autostart_(register|unregister)\(([^)]*)\)", code):
        assert "confirmed" in m.group(2), (
            f"autostart_{m.group(1)} 必须接受 confirmed 参数（用户同意闸门），"
            f"当前形参：{m.group(2)}"
        )


def test_lib_rs_wires_autostart_register_commands():
    """lib.rs 的 invoke_handler 必须接线发行态注册命令（前端可达）。

    注：Tauri ACL（capabilities/default.json）对四个 autostart 命令的授权
    **不在本切片文件范围**（worker 写入被 scope guard 阻断，属 PM 收口项：
    需增加 allow-autostart-status / -register-plan / -register /
    -unregister 四条权限并重建 gen/schemas）。在此以注释留痕，不做断言。"""
    code = _rust_code_without_line_comments(_SRC_TAURI_DIR / "lib.rs")
    handler_block = re.search(r"generate_handler!\[(.*?)\]", code, re.DOTALL)
    assert handler_block, "lib.rs 必须存在 generate_handler 接线块"
    wired = handler_block.group(1)
    for cmd in (
        "autostart::autostart_status",
        "autostart::autostart_register_plan",
        "autostart::autostart_register",
        "autostart::autostart_unregister",
    ):
        assert cmd in wired, f"lib.rs generate_handler 未接线 {cmd}"