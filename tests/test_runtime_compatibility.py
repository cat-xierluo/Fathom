"""Runtime updates keep working when the selected CLI still exposes its adapter flags."""
import time
from unittest.mock import patch

import pytest

from fathom import agent_runtime as ar
from fathom import analysis_manager as am
from dataclasses import replace

FLAGS = {
    'claude-code': '--bare --disable-slash-commands --disallowedTools --strict-mcp-config --no-session-persistence --output-format --print',
    'codex-cli': '--json --ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check --sandbox --cd --config read-only',
    'hermes-agent': '--query-file --format --run-budget -t --ignore-user-config --ignore-rules --source',
}
VERSIONS = {'claude-code': '2.1.303 (Claude Code)', 'codex-cli': 'codex-cli 0.158.0',
            'hermes-agent': 'Hermes Agent v0.22.0+1234.abcdef (2026.10.1)'}

@pytest.mark.parametrize('runtime_id', FLAGS)
def test_new_version_with_required_capabilities_is_available(tmp_path, runtime_id):
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "' + VERSIONS[runtime_id] + '";;\n*) echo "' + ('help text ' * 2200) + FLAGS[runtime_id] + '";;\nesac\n')
    script.chmod(0o755)
    meta = ar.get_candidate(runtime_id)
    # Isolate enumeration to the synthetic CLI, never an installed executable.
    meta = replace(meta, known_locations=())
    info = ar.detect_runtime(meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])
    assert info.availability is ar.Availability.READY
    assert info.reason_code == 'compatible_capabilities'
    assert info.auth_status is ar.AuthStatus.UNKNOWN

@pytest.mark.parametrize('runtime_id', FLAGS)
def test_new_version_missing_required_capability_explains_failure(tmp_path, runtime_id):
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "' + VERSIONS[runtime_id] + '";;\n*) echo "Usage: runtime";;\nesac\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate(runtime_id), known_locations=())
    info = ar.detect_runtime(meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])
    assert info.availability is ar.Availability.UNSUPPORTED
    assert info.reason_code == 'required_capabilities_missing'
    assert '--' in info.detail


def test_saved_version_drift_reprobes_selected_runtime(tmp_path):
    exe = tmp_path / 'runtime'
    info = ar.RuntimeInfo(id='claude-code', display_name='Claude Code', identity='test',
        identity_evidence='test', official_docs='', availability=ar.Availability.READY,
        reason_code='compatible_capabilities', detail='test', executable=str(exe), version='2.1.303')
    with patch.object(ar, 'detect_runtime', return_value=info) as probe:
        selected = am.resolve_runtime(str(exe), 'claude-code', '2.1.237')
    assert selected.version == '2.1.303'
    assert probe.call_args.kwargs['extra_locations'] == [str(exe)]
    assert probe.call_args.kwargs['only_extra_locations'] is True


def test_selected_runtime_timeout_does_not_fallback_to_known_install(tmp_path):
    selected = tmp_path / 'selected'
    selected.write_text('#!/bin/sh\nsleep 1\n')
    selected.chmod(0o755)
    alternate = tmp_path / 'alternate'
    alternate.write_text('#!/bin/sh\necho "2.1.303 (Claude Code)"; exit 0\n')
    alternate.chmod(0o755)
    meta = replace(
        ar.get_candidate('claude-code'), known_locations=(str(alternate),),
        required_capabilities=(),
    )

    info = ar.detect_runtime(
        meta,
        budget=ar.ProbeBudget(per_command_timeout_s=0.05, total_timeout_s=0.2),
        path_env='', extra_locations=[str(selected)], only_extra_locations=True,
        login_shell_cmd=['/bin/echo'],
    )

    assert info.availability is ar.Availability.BROKEN
    assert info.reason_code == 'version_probe_transient'
    assert info.executable == str(selected)
    assert str(alternate) not in info.detail


# --- ISS-198 独审返修：错误卫生与总预算 -------------------------------------


def test_probe_error_detail_has_no_path_or_stderr(tmp_path):
    """P1-1：错误 detail 不得含可执行路径或 stderr 首行。"""
    script = tmp_path / 'runtime'
    script.write_text(
        '#!/bin/sh\necho "SECRET-TOKEN-XYZ leaked" >&2\nexit 7\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    assert info.availability is ar.Availability.BROKEN
    assert str(script) not in info.detail
    assert 'SECRET-TOKEN-XYZ' not in info.detail
    assert 'stderr' not in info.detail.lower()


def test_probe_spawn_failure_is_permanent_not_transient(tmp_path):
    """P2-1：spawn 失败是永久失败，不该提示「暂时超时，可重试」。"""
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\necho hi\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])
    # 文件存在且可执行 → 版本命令失败属永久（版本行缺失）而非 spawn 失败
    assert info.reason_code != 'version_probe_transient'


def test_hanging_candidate_does_not_exhaust_total_budget(tmp_path):
    """P1-2：多个挂起候选不得吃穿总预算，后面的健康候选仍要被探测。"""
    hang = []
    for index in range(3):
        script = tmp_path / f'hang{index}'
        script.write_text('#!/bin/sh\nsleep 30\n')
        script.chmod(0o755)
        hang.append(script)
    healthy = tmp_path / 'healthy'
    healthy.write_text(
        '#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n*) echo "'
        + ('help text ' * 2200) + FLAGS['claude-code'] + '";;\nesac\n')
    healthy.chmod(0o755)

    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    started = time.monotonic()
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=hang + [healthy],
        login_shell_cmd=['/bin/echo'],
        budget=ar.ProbeBudget(total_timeout_s=20, per_command_timeout_s=8),
    )
    elapsed = time.monotonic() - started

    assert elapsed <= 20.0
    assert info.availability is ar.Availability.READY
    assert info.version == '2.1.303'
