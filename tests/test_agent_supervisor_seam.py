"""看门 shim 冻结包接缝合同（ISS-035B）。

合同：PyInstaller 冻结 helper 下 ``sys.executable -c`` 不可用，看门 shim
经 CLI 隐藏子命令 ``_agent-supervisor`` 启动；该子命令与开发态 ``-c``
形态执行**同一份** ``agent_runtime._SUPERVISOR_SOURCE``（单一实现）。
本文件用真实子进程验证子命令转发退出码、看门到期有界回收，以及注入
子命令形态 factory 的生产 runner 行为等价。全部目标都是合成脚本。

冻结包内的实测（真实 onedir helper）不在本卡验证范围，登记 NOT_VERIFIED。
注意：不得把 stdin(0) 等无关 fd 当 pipe_fd 传给子命令——EOF 即触发组级
回收（设计行为）；必须像 runner 一样传真实 ``os.pipe()`` 读端。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fathom import agent_runtime as ar

ROOT = Path(__file__).resolve().parent.parent


def _run_subcommand(deadline: str, grace: str, target: list[str]) -> int:
    """runner 同款方式调用 CLI 隐藏子命令（真实管道 + 独立进程组）。"""
    r_fd, w_fd = os.pipe()
    os.set_inheritable(r_fd, True)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "fathom", "_agent-supervisor",
             deadline, grace, str(r_fd), "--", *target],
            pass_fds=(r_fd,), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=dict(os.environ, PYTHONPATH=str(ROOT)),
            cwd=str(ROOT),
        )
    finally:
        os.close(r_fd)
    try:
        proc.wait(timeout=15)
    finally:
        os.close(w_fd)
    return proc.returncode


def test_single_implementation_source_shared():
    """子命令与默认 -c 形态共用同一份源码：单一实现，禁止漂移。"""
    default_argv = ar.AgentCliRunner._default_shim_argv("1", "0.5", "7", ["x"])
    assert default_argv[1] == "-c" and default_argv[2] == ar._SUPERVISOR_SOURCE
    assert "os._exit" in ar._SUPERVISOR_SOURCE  # 恒以 _exit 终止，不返回


def test_subcommand_forwards_exit_code():
    assert _run_subcommand("30", "1", ["/bin/sh", "-c", "exit 5"]) == 5
    assert _run_subcommand("30", "1", ["/bin/sh", "-c", "exit 0"]) == 0


def test_subcommand_watchdog_kills_group_bounded(tmp_path):
    """看门到期：TERM-免疫目标在期限+宽限+余量内随组消失（不依赖父进程）。"""
    pidfile = tmp_path / "pid"
    stubborn = tmp_path / "stubborn.sh"
    stubborn.write_text(f"#!/bin/sh\necho $$ > {pidfile}\ntrap '' TERM\n"
                        "while true; do sleep 0.1; done\n")
    stubborn.chmod(0o755)
    t0 = time.monotonic()
    rc = _run_subcommand("0.6", "0.3", [str(stubborn)])
    wall = time.monotonic() - t0
    child_pid = int(pidfile.read_text().strip())
    gone = False
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.05)
    # shim 自身也在组内：SIGKILL 兜底会连带自己，退出码为 -9（被 KILL）
    # 或目标先退时的 137/143；合同关注组内有界消失而非具体码。
    assert rc in (-9, 137, 143)
    assert gone
    # ISS-169：期限 0.6s + 宽限 0.3s 的上界原为 6s（比同文件 proc.wait 的 15s
    # 窄 2.5x），冷启动/高负载下假翻红；对齐 :45 的 15s 口径。
    assert wall < 15


def test_runner_with_subcommand_factory_equivalent(tmp_path):
    """生产 runner 注入子命令形态 factory：行为与默认 -c 形态等价。"""
    script = tmp_path / "exit5.sh"
    script.write_text("#!/bin/sh\nexit 5\n")
    script.chmod(0o755)

    def subcmd_factory(deadline, grace, fd, target):
        return [sys.executable, "-m", "fathom", "_agent-supervisor",
                deadline, grace, fd, "--", *target]

    runner = ar.AgentCliRunner(liveness_watch=True, term_grace_s=0.3,
                               shim_argv_factory=subcmd_factory)
    # 冻结形态下 sys.executable（fathom-helper）自带包，无需 PYTHONPATH；
    # 开发态注入验证管道时显式携带，不改变 runner 的 env 白名单合同。
    res = runner.run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp",
             "PYTHONPATH": str(ROOT)}))
    assert res.outcome is ar.RunOutcome.NONZERO_EXIT
    assert res.exit_code == 5
    assert res.group_reaped is True


def test_subcommand_hidden_from_usage():
    """隐藏子命令不进用法说明（非产品功能面）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "fathom", "--help"],
        capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=str(ROOT))
    assert "_agent-supervisor" not in proc.stdout
    assert "_agent-supervisor" not in proc.stderr


def test_run_supervisor_command_never_returns_normally():
    """进程内入口以 os._exit 终止：用子进程证明其不可正常返回。

    harness 以 ``start_new_session`` 自起进程组：os._exit 前的组级回收
    （EOF 兜底）只作用于它自己的组，绝不触碰 pytest/测试 shell。
    """
    harness = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from fathom import agent_runtime\n"
        "agent_runtime.run_supervisor_command(['30', '1', '0', '--',\n"
        "                                      '/bin/sh', '-c', 'exit 5'])\n"
        "print('UNREACHABLE')\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", harness],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=str(ROOT),
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    out, err = proc.communicate(timeout=15)
    # 目标 exit 5 被转发；随后 os._exit 使 print('UNREACHABLE') 不可达。
    assert proc.returncode == 5
    assert b"UNREACHABLE" not in out


@pytest.mark.parametrize("argv", [["30", "1", "not-an-int", "--", "/bin/true"]])
def test_subcommand_bad_fd_is_contained(argv):
    """坏 pipe_fd 不放大成无界行为：子进程以非零有界退出。"""
    r_fd, w_fd = os.pipe()
    os.set_inheritable(r_fd, True)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "fathom", "_agent-supervisor", *argv],
            pass_fds=(r_fd,), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=str(ROOT))
    finally:
        os.close(r_fd)
    try:
        proc.wait(timeout=10)
    finally:
        os.close(w_fd)
    assert proc.returncode != 0
