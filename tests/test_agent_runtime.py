"""fathom/agent_runtime.py 测试（ISS-035A 第二阶段）。

合同：全部用例使用 tmp_path 内的合成 CLI 脚本夹具，不依赖真实 claude、
不触真实 HOME 探测、不发送任何数据；真实 CLI 的受控验证另见 worktree
``evidence/implementation-verification.md``（不入库）。覆盖：
- runner：启动/编码/stdout-stderr 分离/输出限额/应用错误/超时/取消/
  进程组 TERM→KILL 回收/坏 UTF-8/spawn 失败/父进程存活看门；
- 探测：绝对路径/空格/符号链接/失效 wrapper/非执行文件/重名/超时/
  未知版本/登录 shell 回退与预算；
- 适配器解析与调用构建：合法 JSON/is_error/坏 JSON/事件残留拒绝/
  非拒绝能力门；
- 注册表：未知 ID 拒绝、能力上限与 unsupported 家不可构建调用。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fathom import agent_runtime as ar

ROOT = Path(__file__).resolve().parent.parent


# ---------- 合成夹具辅助 ----------


def make_script(tmp_path: Path, name: str, body: str) -> Path:
    """生成可执行 /bin/sh 脚本（模拟第三方 CLI 的各故障形态）。"""
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


def fake_runtime_info(executable: str, availability: ar.Availability = ar.Availability.READY) -> ar.RuntimeInfo:
    """构造走真实适配器代码路径所需的 RuntimeInfo（合成 CLI 充当 claude）。"""
    return ar.RuntimeInfo(
        id="claude-code", display_name="Claude Code",
        identity="合成夹具", identity_evidence="测试", official_docs="https://example.invalid",
        availability=availability, reason_code="verified_version" if availability is ar.Availability.READY else "x",
        detail="测试用", executable=executable,
    )


def make_meta(tmp_path: Path, *, locations: tuple[str, ...] = (), version_line: str | None = None) -> ar.CandidateMeta:
    """合成注册表条目（不触碰真实 CANDIDATES 的政策字段）。"""
    return ar.CandidateMeta(
        id="fake", display_name="Fake CLI", binary_name="fakecli",
        identity="合成 CLI", identity_evidence="测试", official_docs="https://example.invalid",
        known_locations=locations,
        version_pattern=r"^\s*(\d+\.\d+\.\d+)\s+\(Fake\)\s*$",
        capability_cap=ar.Availability.READY, cap_reason_code="verified_version",
        verified_versions=frozenset({"1.0.0"}),
    )


def version_script(tmp_path: Path, name: str, out: str, *, delay_s: float = 0, exit_code: int = 0) -> Path:
    delay = f"sleep {delay_s}" if delay_s else ""
    return make_script(tmp_path, name, f'''
{delay}
cat <<'EOS'
{out}
EOS
exit {exit_code}
''')


# ==========================================================================
# runner
# ==========================================================================


def make_runner(**kw) -> ar.AgentCliRunner:
    defaults = dict(liveness_watch=False, term_grace_s=0.3)
    defaults.update(kw)
    return ar.AgentCliRunner(**defaults)


def test_runner_success_and_stream_separation(tmp_path):
    """exit 0 + stdout/stderr 分离收集 + 正常结局。"""
    script = make_script(tmp_path, "ok.sh",
                         'echo "OUT-内容"; echo "ERR-内容" >&2; exit 0')
    r = make_runner().run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}))
    assert r.outcome is ar.RunOutcome.OK
    assert r.exit_code == 0
    assert "OUT-内容" in r.stdout_text
    assert "ERR-内容" in r.stderr_text
    assert "OUT-内容" not in r.stderr_text and "ERR-内容" not in r.stdout_text
    assert r.group_reaped is True


def test_runner_nonzero_exit_distinguishable(tmp_path):
    script = make_script(tmp_path, "fail.sh", 'echo "boom" >&2; exit 3')
    r = make_runner().run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}))
    assert r.outcome is ar.RunOutcome.NONZERO_EXIT
    assert r.exit_code == 3
    assert "boom" in r.stderr_text


def test_runner_stdin_payload_delivered(tmp_path):
    """stdin 是首选载荷通道：cat 回显验证逐字节到达。"""
    payload = "合成载荷 FATHOM-PAYLOAD-丙\n" * 10
    script = make_script(tmp_path, "cat.sh", "cat")
    r = make_runner().run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        stdin_bytes=payload.encode("utf-8")))
    assert r.outcome is ar.RunOutcome.OK
    assert payload in r.stdout_text


def test_runner_stdout_limit_kills_and_marks(tmp_path):
    """stdout 边读边限额：超限即终止进程组，结局 OUTPUT_LIMIT 可辨。"""
    script = make_script(tmp_path, "big.sh",
                         'dd if=/dev/zero bs=1024 count=512 2>/dev/null')
    t0 = time.monotonic()
    r = make_runner(stdout_max_bytes=8192).run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}))
    assert r.outcome is ar.RunOutcome.OUTPUT_LIMIT
    assert r.stdout_truncated is True
    assert len(r.stdout_text.encode("utf-8")) < 64 * 1024  # 缓冲确实被限住
    assert time.monotonic() - t0 < 10  # 不等脚本自然结束


def test_runner_stderr_limit_distinguishable(tmp_path):
    script = make_script(tmp_path, "noisy.sh",
                         'dd if=/dev/zero bs=1024 count=256 1>&2 2>/dev/null')
    r = make_runner(stderr_max_bytes=4096).run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}))
    assert r.outcome is ar.RunOutcome.OUTPUT_LIMIT
    assert r.stderr_truncated is True


def test_runner_bad_utf8_fails_not_replaced(tmp_path):
    """UTF-8 严格解码：坏字节即 DECODE_ERROR，绝不静默替换成正文。"""
    script = make_script(tmp_path, "bad.sh", r'printf "\xff\xfe\xbd"')
    r = make_runner().run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}))
    assert r.outcome is ar.RunOutcome.DECODE_ERROR
    assert "UTF-8" in r.detail


def test_runner_timeout_term_then_kill_and_reap(tmp_path):
    """超时对进程组 SIGTERM，宽限后 SIGKILL，终态前 reap。"""
    pidfile = tmp_path / "pid"
    script = make_script(tmp_path, "stubborn.sh", f'''
echo $$ > {pidfile}
trap '' TERM
while true; do sleep 0.1; done
''')
    t0 = time.monotonic()
    r = make_runner(term_grace_s=0.4).run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}),
        timeout_s=0.5)
    wall = time.monotonic() - t0
    assert r.outcome is ar.RunOutcome.TIMED_OUT
    assert r.exit_code == 137  # 128+SIGKILL
    assert 0.5 <= wall < 5
    assert r.group_reaped is True
    # 进程组确已消失
    with pytest.raises(ProcessLookupError):
        os.killpg(int(pidfile.read_text()), 0)


def test_runner_term_kills_cooperative_child_quickly(tmp_path):
    """正常子进程在 TERM 即退：超时回收不依赖 KILL。"""
    script = make_script(tmp_path, "polite.sh", 'trap "exit 7" TERM; while true; do sleep 0.1; done')
    r = make_runner(term_grace_s=2.0).run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}),
        timeout_s=0.4)
    assert r.outcome is ar.RunOutcome.TIMED_OUT
    assert r.exit_code == 7  # 转发了 TERM 处理后的退出码


def test_runner_cancel_event(tmp_path):
    script = make_script(tmp_path, "slow.sh", 'sleep 30')
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    try:
        r = make_runner(term_grace_s=0.3).run(ar.Invocation(
            argv=[str(script)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}),
            timeout_s=30, cancel_event=cancel)
    finally:
        timer.join()
    assert r.outcome is ar.RunOutcome.CANCELLED
    assert r.wall_ms < 5000


def test_runner_spawn_failure(tmp_path):
    r = make_runner().run(ar.Invocation(
        argv=[str(tmp_path / "不存在")], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}))
    assert r.outcome is ar.RunOutcome.SPAWN_FAILED
    assert r.exit_code is None


def test_runner_argv_validation():
    runner = make_runner()
    with pytest.raises(ValueError):
        runner.run(ar.Invocation(argv=[], cwd=None, env={}))
    with pytest.raises(ValueError):
        runner.run(ar.Invocation(argv=["x", 1], cwd=None, env={}))
    with pytest.raises(ValueError):
        runner.run(ar.Invocation(argv=["x"], cwd=None, env={}), timeout_s=0)


def test_liveness_shim_runs_target_and_forwards_exit(tmp_path):
    """看门 shim 在位：目标退出码被转发且进程组被 reap。"""
    script = make_script(tmp_path, "exit5.sh", "exit 5")
    runner = ar.AgentCliRunner(liveness_watch=True, term_grace_s=0.3)
    r = runner.run(ar.Invocation(
        argv=[str(script)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}))
    assert r.outcome is ar.RunOutcome.NONZERO_EXIT
    assert r.exit_code == 5
    assert r.group_reaped is True


def test_parent_death_bounds_child(tmp_path):
    """异常父进程消失（SIGKILL）→ 看门经存活管道触发组级有界回收。

    harness 子进程持生产 runner 真实跑一个 TERM-免疫脚本；测试 SIGKILL
    harness 后，脚本必须在一伐宽限+余量内消失（不依赖 shutdown hook）。
    """
    pidfile = tmp_path / "pid"
    script = make_script(tmp_path, "orphan.sh", f'''
echo $$ > {pidfile}
trap '' TERM
while true; do sleep 0.1; done
''')
    harness = tmp_path / "harness.py"
    harness.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from fathom.agent_runtime import AgentCliRunner, Invocation\n"
        "runner = AgentCliRunner(liveness_watch=True, term_grace_s=0.6)\n"
        f"runner.run(Invocation(argv=[{str(script)!r}], cwd={str(tmp_path)!r},\n"
        "    env={'PATH': '/usr/bin:/bin', 'HOME': '/tmp'}), timeout_s=60)\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    parent = subprocess.Popen([sys.executable, str(harness)], env=env)
    try:
        for _ in range(100):
            if pidfile.exists():
                break
            time.sleep(0.05)
        assert pidfile.exists(), "harness 未能启动脚本"
        child_pid = int(pidfile.read_text().strip() or "0")
        assert child_pid > 0
        parent.kill()  # 异常父进程消失（SIGKILL，非优雅退出）
        parent.wait(timeout=10)
        t0 = time.monotonic()
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() - t0 > 8:
                pytest.fail("父进程死后子任务 8 秒仍未退出：有界退出失效")
            time.sleep(0.05)
        assert time.monotonic() - t0 < 6  # 宽限 0.6s + 余量；远小于 60s 任务时长
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


# ==========================================================================
# 探测
# ==========================================================================


def _detect(meta, **kw) -> ar.RuntimeInfo:
    return ar.detect_runtime(meta, **kw)


def test_probe_absolute_path_ready(tmp_path):
    script = version_script(tmp_path, "fakecli", "1.0.0 (Fake)")
    info = _detect(make_meta(tmp_path), path_env=str(tmp_path),
                   extra_locations=[script], login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.READY
    assert info.version == "1.0.0"
    assert info.executable == str(script)
    assert info.auth_status is ar.AuthStatus.UNKNOWN  # 版本成功不解释为已登录


def test_probe_space_path(tmp_path):
    spaced = tmp_path / "my tools dir"
    spaced.mkdir()
    script = version_script(spaced, "fakecli", "1.0.0 (Fake)")
    info = _detect(make_meta(tmp_path), path_env="", extra_locations=[script],
                   login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.READY
    assert "my tools dir" in info.executable


def test_probe_symlink_resolved(tmp_path):
    real = version_script(tmp_path, "real-binary", "1.0.0 (Fake)")
    link = tmp_path / "fakecli"
    link.symlink_to(real)
    info = _detect(make_meta(tmp_path), path_env="", extra_locations=[link],
                   login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.READY
    assert info.resolved_target == str(real)
    assert info.executable == str(link)


def test_probe_broken_wrapper_falls_to_next_candidate(tmp_path):
    """失效 wrapper 不掩盖后继候选；全部失效才 broken。"""
    wrapper = make_script(tmp_path, "fakecli", 'exec /nonexistent/target "$@"\nexit 127')
    real = version_script(tmp_path, "real-fakecli", "1.0.0 (Fake)")
    info = _detect(make_meta(tmp_path), path_env="",
                   extra_locations=[wrapper, real], login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.READY
    assert info.executable == str(real)


def test_probe_all_candidates_broken(tmp_path):
    wrapper = make_script(tmp_path, "fakecli", 'exec /nonexistent/target "$@"')
    info = _detect(make_meta(tmp_path), path_env="", extra_locations=[wrapper],
                   login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.BROKEN
    assert info.reason_code == "version_probe_failed"


def test_probe_non_executable_file_reported(tmp_path):
    not_exec = tmp_path / "fakecli"
    not_exec.write_text("#!/bin/sh\necho 1.0.0 (Fake)\n")
    info = _detect(make_meta(tmp_path), path_env="", extra_locations=[not_exec],
                   login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.BROKEN
    assert "不可执行" in info.detail


def test_probe_duplicate_names_keep_path_order(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    version_script(dir_a, "fakecli", "1.0.0 (Fake)")
    version_script(dir_b, "fakecli", "1.0.0 (Fake)")
    info = _detect(make_meta(tmp_path), path_env=f"{dir_a}:{dir_b}",
                   login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.READY
    assert info.executable == str(dir_a / "fakecli")  # PATH 顺序优先，不静默换家


def test_probe_unverified_version_unsupported(tmp_path):
    script = version_script(tmp_path, "fakecli", "8.8.8 (Fake)")
    info = _detect(make_meta(tmp_path), path_env="", extra_locations=[script],
                   login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.UNSUPPORTED
    assert info.reason_code == "version_unverified"
    assert info.version == "8.8.8"


def test_probe_unrecognized_version_unsupported(tmp_path):
    """可执行但版本输出无法识别：身份未确认 → unsupported，不是 broken。"""
    script = version_script(tmp_path, "fakecli", "h_store_v3 树莓派固件 2024")
    info = _detect(make_meta(tmp_path), path_env="", extra_locations=[script],
                   login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.UNSUPPORTED
    assert info.reason_code == "version_unrecognized"
    assert info.version is None


def test_probe_per_command_timeout_honored(tmp_path):
    script = version_script(tmp_path, "fakecli", "1.0.0 (Fake)", delay_s=30)
    budget = ar.ProbeBudget(per_command_timeout_s=0.4, total_timeout_s=10)
    t0 = time.monotonic()
    info = _detect(make_meta(tmp_path), budget=budget, path_env="",
                   extra_locations=[script], login_shell_cmd=["/bin/echo"])
    assert time.monotonic() - t0 < 6
    assert info.availability is ar.Availability.BROKEN
    assert "timed_out" in info.detail


def test_probe_output_cap_honored(tmp_path):
    """单命令输出上限有效：1MB 噪声在 16KiB 预算下不拖垮探测。"""
    script = make_script(tmp_path, "fakecli", '''
dd if=/dev/zero bs=1024 count=1024 2>/dev/null
echo "1.0.0 (Fake)"
''')
    budget = ar.ProbeBudget(per_command_output_cap=16 * 1024, per_command_timeout_s=3)
    t0 = time.monotonic()
    info = _detect(make_meta(tmp_path), budget=budget, path_env="",
                   extra_locations=[script], login_shell_cmd=["/bin/echo"])
    assert time.monotonic() - t0 < 6
    assert info.availability is ar.Availability.BROKEN
    assert "output_limit" in info.detail


def test_probe_login_shell_fallback(tmp_path):
    """PATH 未命中时按需登录 shell 探测；输出仅当绝对路径才被接受。"""
    script = version_script(tmp_path, "fakecli", "1.0.0 (Fake)")
    fake_sh = make_script(tmp_path, "fake-shell.sh", f'echo "{script}"\n')
    info = _detect(make_meta(tmp_path), path_env=str(tmp_path / "empty"),
                   login_shell_cmd=[str(fake_sh)])
    assert info.availability is ar.Availability.READY


def test_probe_login_shell_relative_output_ignored(tmp_path):
    fake_sh = make_script(tmp_path, "fake-shell.sh", 'echo "fakecli"\n')
    info = _detect(make_meta(tmp_path), path_env=str(tmp_path / "empty"),
                   login_shell_cmd=[str(fake_sh)])
    assert info.availability is ar.Availability.NOT_FOUND
    assert any("eval" in n or "绝对路径" in n for n in info.notes)


def test_probe_login_shell_timeout_bounded(tmp_path):
    fake_sh = make_script(tmp_path, "fake-shell.sh", "sleep 30\n")
    budget = ar.ProbeBudget(per_command_timeout_s=0.4, total_timeout_s=10)
    t0 = time.monotonic()
    info = _detect(make_meta(tmp_path), budget=budget,
                   path_env=str(tmp_path / "empty"), login_shell_cmd=[str(fake_sh)])
    assert time.monotonic() - t0 < 6
    assert info.availability is ar.Availability.NOT_FOUND


def test_probe_shared_deadline_respected(tmp_path):
    """probe_all 共享总预算：传入已到期的 deadline 时全部快速返回。"""
    script = version_script(tmp_path, "fakecli", "1.0.0 (Fake)", delay_s=30)
    metas = {
        "x1": make_meta(tmp_path, locations=(str(script),)),
        "x2": make_meta(tmp_path, locations=(str(script),)),
    }
    original = dict(ar.CANDIDATES)
    ar.CANDIDATES.clear()
    ar.CANDIDATES.update(metas)
    try:
        t0 = time.monotonic()
        infos = ar.probe_all(
            budget=ar.ProbeBudget(total_timeout_s=0.2),
            path_env=str(tmp_path),
        )
        assert time.monotonic() - t0 < 5
        assert set(infos) == {"x1", "x2"}
        for info in infos.values():
            assert info.reason_code in ("probe_budget_exhausted", "version_probe_failed")
    finally:
        ar.CANDIDATES.clear()
        ar.CANDIDATES.update(original)


def test_probe_nothing_found(tmp_path):
    info = _detect(make_meta(tmp_path), path_env=str(tmp_path / "missing"),
                   login_shell_cmd=["/bin/echo"])
    assert info.availability is ar.Availability.NOT_FOUND
    assert info.reason_code == "not_found"


# ==========================================================================
# 适配器：调用构建与输出解析
# ==========================================================================


def _result_json(**over) -> str:
    base = {"type": "result", "subtype": "success", "is_error": False,
            "result": "42", "num_turns": 1, "session_id": "s-1"}
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def _run_ok(stdout: str) -> ar.RunResult:
    return ar.RunResult(outcome=ar.RunOutcome.OK, exit_code=0,
                        stdout_text=stdout, stderr_text="")


def test_adapter_build_invocation_stdin_mode(tmp_path):
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "claude")))
    inv = adapter.build_invocation("合成问题", cwd=tmp_path)
    assert inv.argv[0] == str(tmp_path / "claude")
    assert inv.argv[-1] == "-p"                       # -p 置尾：payload 走 stdin
    assert "合成问题" not in " ".join(inv.argv)        # 载荷不进 argv（对同用户进程不可见）
    for flag in ("--bare", "--disable-slash-commands", "--strict-mcp-config",
                 "--no-session-persistence", "--output-format", "json"):
        assert flag in inv.argv
    assert inv.argv[inv.argv.index("--disallowedTools") + 1] == "*"
    assert inv.stdin_bytes == "合成问题".encode("utf-8")
    assert set(inv.env) == {"PATH", "HOME"}           # 环境白名单最小化；无凭据字段


def test_adapter_build_invocation_argv_mode_discloses(tmp_path):
    """argv 回退形态：载荷可见性以结构披露（stdin_bytes 为空即 argv 形态）。"""
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "claude")))
    inv = adapter.build_invocation("合成问题", cwd=tmp_path, payload_mode="argv")
    assert inv.stdin_bytes is None
    assert "合成问题" in inv.argv  # argv 载荷对本机同用户进程可见（代码注释已披露）
    idx = inv.argv.index("-p")
    assert inv.argv[idx + 1] == "合成问题"


def test_adapter_build_invocation_rejects_non_ready(tmp_path):
    info = fake_runtime_info(str(tmp_path / "claude"), ar.Availability.UNSUPPORTED)
    with pytest.raises(ar.UnsupportedRuntimeError):
        ar.ClaudeCodeAdapter(info)
    info2 = fake_runtime_info(str(tmp_path / "claude"))
    info2.id = "zcode"
    with pytest.raises(ar.UnsupportedRuntimeError):
        ar.ClaudeCodeAdapter(info2)


def test_adapter_build_invocation_rejects_empty_payload(tmp_path):
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "claude")))
    with pytest.raises(ValueError):
        adapter.build_invocation("  ", cwd=tmp_path)
    with pytest.raises(ValueError):
        adapter.build_invocation("x", cwd=tmp_path, payload_mode="shell")


def test_parse_valid_result(tmp_path):
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "c")))
    p = adapter.parse_output(_run_ok(_result_json()))
    assert p.ok is True
    assert p.value["result"] == "42"
    assert p.value["subtype"] == "success"


def test_parse_app_error_distinguishable(tmp_path):
    """exit=0 但 is_error=true：应用错误，独立于非零退出与解析失败。"""
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "c")))
    p = adapter.parse_output(_run_ok(_result_json(is_error=True, result="认证失效")))
    assert p.ok is False
    assert p.reason_code == "app_error"
    assert "认证失效" in p.value["result"]


def test_parse_bad_json_rejected(tmp_path):
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "c")))
    p = adapter.parse_output(_run_ok("这不是 JSON"))
    assert p.ok is False
    assert p.reason_code == "parse_failed_bad_json"


def test_parse_stream_json_residue_rejected(tmp_path):
    """未来误用 stream-json：逐行事件结构必须整体拒绝，不提取正文。"""
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "c")))
    lines = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "tools": []}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read"}]}}),
        _result_json(),
    ])
    p = adapter.parse_output(_run_ok(lines))
    assert p.ok is False
    assert p.reason_code in ("parse_failed_bad_json", "parse_failed_not_result")


def test_parse_non_result_type_rejected(tmp_path):
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "c")))
    p = adapter.parse_output(_run_ok(json.dumps({"type": "system", "subtype": "init"})))
    assert p.ok is False
    assert p.reason_code == "parse_failed_not_result"


def test_parse_missing_result_field_rejected(tmp_path):
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "c")))
    p = adapter.parse_output(_run_ok(_result_json(result=None)))
    assert p.ok is False
    assert p.reason_code == "parse_failed_missing_field"


def test_parse_runner_failure_passthrough(tmp_path):
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(tmp_path / "c")))
    r = ar.RunResult(outcome=ar.RunOutcome.TIMED_OUT, exit_code=143,
                     stdout_text="", stderr_text="")
    p = adapter.parse_output(r)
    assert p.ok is False
    assert p.reason_code == "runner_timed_out"


# ==========================================================================
# 派发三关（合成 CLI 走真实适配器 + 生产 runner）
# ==========================================================================


def test_dispatch_success_three_gates(tmp_path):
    """三关全过：exit 0 + 应用无错误 + 结构验证通过。"""
    script = make_script(tmp_path, "claude", '''
cat > /dev/null
echo '{"type":"result","subtype":"success","is_error":false,"result":"合成正文","num_turns":1}'
''')
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(script)))
    result = ar.dispatch_request(adapter, make_runner(), "问题", cwd=tmp_path)
    assert result.ok is True
    assert result.parse.value["result"] == "合成正文"


def test_dispatch_exit0_but_app_error(tmp_path):
    """exit=0 但应用错误：不得判成功（三关缺一不可）。"""
    script = make_script(tmp_path, "claude", '''
cat > /dev/null
echo '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"额度耗尽"}'
''')
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(script)))
    result = ar.dispatch_request(adapter, make_runner(), "问题", cwd=tmp_path)
    assert result.ok is False
    assert result.reason_code == "app_error"


def test_dispatch_nonzero_exit(tmp_path):
    script = make_script(tmp_path, "claude", "exit 9")
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(script)))
    result = ar.dispatch_request(adapter, make_runner(), "问题", cwd=tmp_path)
    assert result.ok is False
    assert result.reason_code == "runner_nonzero_exit"


def test_dispatch_bad_payload_json(tmp_path):
    script = make_script(tmp_path, "claude", "cat > /dev/null; echo '半截 JSON {'")
    adapter = ar.ClaudeCodeAdapter(fake_runtime_info(str(script)))
    result = ar.dispatch_request(adapter, make_runner(), "问题", cwd=tmp_path)
    assert result.ok is False
    assert result.reason_code == "parse_failed_bad_json"


# ==========================================================================
# 注册表与最小环境
# ==========================================================================


def test_registry_unknown_id_rejected():
    with pytest.raises(ar.UnknownCandidateError):
        ar.get_candidate("claude")          # 相似名不接受
    with pytest.raises(ar.UnknownCandidateError):
        ar.get_candidate("codebuddy")       # CodeBuddy 不是注册表成员，禁止与 ZCode 混同


def test_registry_records_four_candidates_with_caps():
    assert set(ar.CANDIDATES) == {"claude-code", "zcode", "codex-cli", "hermes-agent"}
    claude = ar.get_candidate("claude-code")
    assert claude.capability_cap is ar.Availability.READY
    assert claude.verified_versions                     # 版本收敛：仅已验证集合可 ready
    for cid in ("zcode", "codex-cli", "hermes-agent"):
        meta = ar.get_candidate(cid)
        assert meta.capability_cap is ar.Availability.UNSUPPORTED
        assert meta.cap_reason_code
        assert meta.cap_reason
    for meta in ar.CANDIDATES.values():
        assert meta.identity and meta.identity_evidence and meta.official_docs
        assert meta.known_locations


def test_get_adapter_rejects_unsupported(tmp_path):
    info = ar.RuntimeInfo(
        id="zcode", display_name="ZCode", identity="x", identity_evidence="x",
        official_docs="x", availability=ar.Availability.UNSUPPORTED,
        reason_code="tool_disable_unverifiable", detail="",
    )
    with pytest.raises(ar.UnsupportedRuntimeError):
        ar.get_adapter(info)


def test_build_minimal_env_whitelist(monkeypatch):
    env = ar.build_minimal_env(path="/usr/bin:/bin", home="/Users/x")
    assert env == {"PATH": "/usr/bin:/bin", "HOME": "/Users/x"}
    monkeypatch.delenv("HOME", raising=False)
    with pytest.raises(ar.EnvError):
        ar.build_minimal_env(path="/usr/bin:/bin")
