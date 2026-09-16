"""ISS-010A：登录项 / 后台计划只读状态桥 + dry-run 的测试。

覆盖：
- ``fathom.launchd.status`` 三态映射矩阵（fake ``run`` 注入输出）
- 异常路径（超时、命令不存在、id -u 失败）→ unknown
- ``fathom.launchd.dry_run_plan`` 内容与 ``_scan_plist/_web_plist`` 同源、路径
  在 ``config.LAUNCHAGENTS_DIR`` 下、命令列表含 bootstrap 字样但 fake run
  从未被调用
- grep 式断言：本测试文件 + ``launchd.py`` ``status``/``dry_run_plan`` 函数
  体内不出现 ``_bootstrap(``/``_write_plist(`` 调用（守护「dry-run 不写」的
  不变量）
"""

from __future__ import annotations

import inspect
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


# ----- 静态不变量：grep 式守护 -----------------------------------------

def _function_code(name: str) -> str:
    """取 ``launchd.<name>`` 的源码（def 行及以下到下一 def 前）。"""
    src = inspect.getsource(launchd)
    lines = src.splitlines()
    body_lines: list[str] = []
    in_target = False
    for line in lines:
        if line.startswith(f"def {name}("):
            in_target = True
            body_lines.append(line)
            continue
        if in_target:
            # 下一顶层 ``def`` / ``class`` / 模块级语句开头 → 终止。
            if (
                line.startswith("def ")
                or line.startswith("class ")
                or line.startswith("@")
                or (line and not line.startswith((" ", "\t", "#")))
            ):
                break
            body_lines.append(line)
    return "\n".join(body_lines)


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


@pytest.mark.parametrize("fn_name", ["status", "dry_run_plan"])
def test_new_function_bodies_do_not_call_write_or_bootstrap(fn_name: str):
    body = _function_code(fn_name)
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