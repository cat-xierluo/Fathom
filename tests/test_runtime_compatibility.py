"""Runtime updates keep working when the selected CLI still exposes its adapter flags."""
import os
import time
from unittest.mock import patch

import pytest

from fathom import agent_runtime as ar
from fathom import analysis_manager as am
from fathom import runtime_capabilities as rcap
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
    """P2-1（ISS-198 三审追加）：spawn 失败是永久失败。

    夹具必须**真正触发 SPAWN_FAILED**：文件存在且有执行位，但内容不是
    合法可执行格式（macOS execve → ENOEXEC → 分类为 spawn 失败）。
    早期夹具写的是合法 shell 脚本，根本没触发 spawn 失败，被「版本行缺失」
    路径吃掉了——用例名与实际测的东西不是一回事，属弱断言漏网。
    """
    script = tmp_path / 'runtime'
    script.write_bytes(b'\x00\x01\x02\x03not-an-executable')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    assert info.availability is ar.Availability.BROKEN
    # 永久失败：不得归为「暂时超时，可重试」
    assert info.reason_code == 'version_probe_failed'
    assert info.reason_code != 'version_probe_transient'
    assert 'spawn_failed' in info.detail


def test_precheck_not_executable_is_permanent_failure(tmp_path):
    """P2-1 附带：precheck「文件存在但不可执行」同样归永久失败。"""
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\necho hi\n')
    script.chmod(0o644)  # 有文件、无执行位
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    assert info.availability is ar.Availability.BROKEN
    assert info.reason_code == 'version_probe_failed'
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


# --- ISS-200：入口身份与能力快照 -------------------------------------------


def test_entry_identity_separates_wrapper_from_resolved_target(tmp_path):
    """wrapper 路径与 realpath 目标分层记录，内容摘要可识别同路径换文件。"""
    from fathom import runtime_capabilities as rcap
    target = tmp_path / 'real'
    target.write_text('#!/bin/sh\necho hi\n')
    target.chmod(0o755)
    wrapper = tmp_path / 'wrapper'
    wrapper.symlink_to(target)

    identity = rcap.identify_entry(wrapper)
    assert identity is not None
    assert identity.configured_path == str(wrapper)
    assert identity.resolved_path == str(target.resolve())
    assert identity.is_wrapper is True
    assert identity.size_bytes == target.stat().st_size
    assert identity.content_digest

    plain = rcap.identify_entry(target)
    assert plain is not None and plain.is_wrapper is False


def test_entry_identity_detects_same_path_replacement(tmp_path):
    """同一路径换内容后指纹变化 → 缓存失效条件成立。"""
    from fathom import runtime_capabilities as rcap
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\necho one\n')
    script.chmod(0o755)
    before = rcap.identify_entry(script)

    script.write_text('#!/bin/sh\necho two-longer\n')
    script.chmod(0o755)
    after = rcap.identify_entry(script)

    assert before is not None and after is not None
    assert rcap.entry_changed(before, after) is True
    assert rcap.entry_changed(before, before) is False


def test_capability_snapshot_required_and_optional_are_separated():
    """必需能力缺失 → 该入口不可用；可选能力缺失只降级该项。"""
    from fathom import runtime_capabilities as rcap
    snapshot = rcap.build_snapshot(
        'fake',
        identity=None,
        supported={'bounded_input': '受控实证'},
        unsupported={'session_resume': '该入口不提供'},
        evidence={'bounded_input': rcap.EvidenceStrength.HELP},
    )
    assert 'executable_identity' in snapshot.missing_required()
    assert 'session_resume' in snapshot.degraded_optional()
    assert snapshot.get('bounded_input').evidence is rcap.EvidenceStrength.HELP
    assert snapshot.get('unknown_thing') is None


def test_build_snapshot_rejects_supported_with_verified_evidence():
    """三审 N-2：探测期不得直接构造 supported/verified。

    VERIFIED 只能由 ``apply_call_evidence`` 回填（真实受控调用成功）；
    build_snapshot 若接受该组合，等于给「用静态文案冒充实测」留后门。
    """
    from fathom import runtime_capabilities as rcap
    with pytest.raises(ValueError):
        rcap.build_snapshot(
            'fake',
            identity=None,
            supported={'bounded_input': '本机受控调用实测'},
            evidence={'bounded_input': rcap.EvidenceStrength.VERIFIED},
        )


def test_detect_runtime_exposes_capability_snapshot(tmp_path):
    """探测结果携带可消费的能力快照；旧字段语义不变。"""
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n*) echo "'
                    + ('help text ' * 2200) + FLAGS['claude-code'] + '";;\nesac\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    assert info.capability_snapshot is not None
    assert info.capability_snapshot.runtime_id == 'claude-code'
    payload = info.to_dict()
    assert payload['capability_snapshot']['runtime_id'] == 'claude-code'
    # 既有键语义不变
    assert payload['availability'] == 'ready'
    assert payload['reason_code'] == 'compatible_capabilities'
    # 三审 N-2：探测期没有受控调用，必需能力不得声称已支持/已实测。
    snapshot = info.capability_snapshot
    for name in ('bounded_input', 'executable_identity', 'terminal_state',
                 'final_result', 'cancellable', 'no_duplicate_send'):
        capability = snapshot.get(name)
        assert capability is not None, name
        assert capability.state is not rcap.CapabilityState.SUPPORTED, name
        assert capability.evidence is not rcap.EvidenceStrength.VERIFIED, name
    assert snapshot.missing_required()  # 未取得证据 → 必需能力缺口非空


def test_scoped_negative_blocks_only_that_entry(tmp_path):
    """品牌负例绑定具体入口与范围，不无条件封禁该品牌其他入口。"""
    script = tmp_path / 'runtime'
    # This scope fixture uses only statically proven builtins. Complex shell case
    # dispatch is deliberately incomplete under the F2-R identity grammar.
    script.write_text('#!/bin/sh\necho "2.1.303 (Claude Code)"\necho "'
                    + ('help text ' * 2200) + FLAGS['claude-code'] + '"\n')
    script.chmod(0o755)
    meta = replace(
        ar.get_candidate('claude-code'), known_locations=(),
        capability_cap=ar.Availability.UNSUPPORTED,
        cap_reason_code='no_readonly_enforcement',
        cap_reason='合成负例：该入口无只读执行边界',
        cap_observed_version='2.1.303', cap_observed_profile='claude-code',
        cap_entry_fingerprint=rcap.identify_entry(script).fingerprint(),
    )
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])
    assert info.availability is ar.Availability.UNSUPPORTED
    assert info.reason_code == 'no_readonly_enforcement'
    assert info.capability_snapshot is not None
    blockers = info.capability_snapshot.scoped_blockers
    assert blockers
    assert 'version=2.1.303' in blockers[0].scope
    # 三审 N-4：capability 必须是**真实能力名**，reason_code 才是原因码，
    # 两者语义分离（首审把 reason_code 冒充能力名，本用例曾把错误语义固化）。
    assert blockers[0].capability in rcap.REQUIRED_CAPABILITIES | rcap.OPTIONAL_CAPABILITIES
    assert blockers[0].capability == 'bounded_input'
    assert blockers[0].reason_code == 'no_readonly_enforcement'
    assert blockers[0].capability != blockers[0].reason_code
    # P0-1：负例入口不得声称必需能力全部 verified
    assert info.capability_snapshot.missing_required()
    # 负例对应的那个能力必须记 unsupported（不是 unknown）
    failed = info.capability_snapshot.get('bounded_input')
    assert failed is not None
    assert failed.state is rcap.CapabilityState.UNSUPPORTED


def test_recheck_runtime_is_read_only_probe(tmp_path):
    """显式重测只做只读探测：不发模型请求、不改配置。"""
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n*) echo "'
                    + ('help text ' * 2200) + FLAGS['claude-code'] + '";;\nesac\n')
    script.chmod(0o755)
    info = ar.recheck_runtime(ar.get_candidate('claude-code'), str(script))
    assert info.availability is ar.Availability.READY
    assert info.version == '2.1.303'
    assert info.capability_snapshot is not None


# --- ISS-200 返修：独审点名的缺失反例 ---------------------------------------


def test_missing_required_capability_blocks_entry(tmp_path):
    """反例 1/7：必需参数缺失 → 必需能力 unsupported → missing_required 非空。"""
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n*) echo "Usage: runtime";;\nesac\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    assert info.availability is ar.Availability.UNSUPPORTED
    assert info.reason_code == 'required_capabilities_missing'
    snapshot = info.capability_snapshot
    assert snapshot is not None
    assert snapshot.missing_required() == tuple(sorted(snapshot.missing_required()))
    assert 'bounded_input' in snapshot.missing_required()


def test_help_text_alone_does_not_prove_optional_capabilities(tmp_path):
    """反例 3：帮助文本不含 stream/usage/model/schema 时可选能力不得 supported。"""
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n*) echo "'
                    + ('help text ' * 2200) + FLAGS['claude-code'] + '";;\nesac\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    snapshot = info.capability_snapshot
    assert snapshot is not None
    for name in ('streaming', 'usage_reporting', 'model_reporting', 'native_schema'):
        capability = snapshot.get(name)
        assert capability is not None
        assert capability.state is not rc_capability_state('supported'), name


def rc_capability_state(value):
    from fathom import runtime_capabilities as rcap
    return rcap.CapabilityState(value)


def test_script_wrapper_reports_underlying_engine(tmp_path):
    """反例 4：shell wrapper 必须解析出底层引擎层级。"""
    from fathom import runtime_capabilities as rcap
    engine = tmp_path / 'engine'
    engine.write_text('#!/bin/sh\necho engine\n')
    engine.chmod(0o755)
    wrapper = tmp_path / 'wrapper'
    wrapper.write_text('#!/usr/bin/env bash\nAPP="%s"\nexec "$APP" "$@"\n' % engine)
    wrapper.chmod(0o755)

    identity = rcap.identify_entry(wrapper)
    assert identity is not None
    assert identity.wrapper_kind == 'script'
    assert identity.is_wrapper is True
    assert str(engine) in identity.layers


def test_tail_change_is_detected_even_with_same_size_and_mtime(tmp_path):
    """反例 5：后段字节变化 + 长度不变 + mtime 还原，摘要必须仍能发现。"""
    from fathom import runtime_capabilities as rcap
    script = tmp_path / 'runtime'
    script.write_bytes(b'A' * (64 * 1024) + b'B' * 1000)
    script.chmod(0o755)
    before = rcap.identify_entry(script)
    stat_before = script.stat()

    script.write_bytes(b'A' * (64 * 1024) + b'C' * 1000)
    script.chmod(0o755)
    os.utime(script, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    after = rcap.identify_entry(script)

    assert before is not None and after is not None
    assert before.size_bytes == after.size_bytes
    assert before.mtime_ns == after.mtime_ns
    assert rcap.entry_changed(before, after) is True


def test_relative_and_absolute_paths_share_fingerprint(tmp_path):
    """反例 6：相对路径与绝对路径指向同一文件时指纹必须稳定。"""
    from fathom import runtime_capabilities as rcap
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\necho hi\n')
    script.chmod(0o755)
    absolute = rcap.identify_entry(str(script))
    # 相对路径以脚本所在目录为基准，指向同一个文件
    relative = rcap.identify_entry(os.path.join(str(tmp_path), '.', script.name))
    assert absolute is not None and relative is not None
    assert rcap.entry_changed(absolute, relative) is False


def test_failure_paths_still_produce_snapshot(tmp_path):
    """反例 7：版本不可识别 / 探测失败两条路径都必须带快照。"""
    unrecognized = tmp_path / 'weird'
    unrecognized.write_text('#!/bin/sh\necho "not-a-version"\n')
    unrecognized.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[unrecognized], login_shell_cmd=['/bin/echo'])
    assert info.reason_code == 'version_unrecognized'
    assert info.capability_snapshot is not None
    assert info.capability_snapshot.missing_required()
    assert info.capability_snapshot.scoped_blockers
    assert info.capability_snapshot.scoped_blockers[0].capability == 'executable_identity'

    broken = tmp_path / 'broken'
    broken.write_text('#!/bin/sh\nexit 7\n')
    broken.chmod(0o755)
    info2 = ar.detect_runtime(
        meta, path_env='', extra_locations=[broken], login_shell_cmd=['/bin/echo'])
    assert info2.availability is ar.Availability.BROKEN
    assert info2.capability_snapshot is not None


def test_scoped_blocker_binds_capability_and_scope(tmp_path):
    """反例 8：scoped_blockers 必须绑定失败能力 + 版本范围，而非品牌常量。"""
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "3.14.3 (Claude Code)";;\n*) echo "usage";;\nesac\n')
    script.chmod(0o755)
    meta = replace(
        ar.get_candidate('claude-code'), known_locations=(),
        capability_cap=ar.Availability.UNSUPPORTED,
        cap_reason_code='no_readonly_enforcement',
        cap_reason='合成负例',
    )
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])
    blockers = info.capability_snapshot.scoped_blockers
    assert blockers
    # 三审 N-4：capability 是真实能力名；reason_code 独立表达原因。
    assert blockers[0].capability in rcap.REQUIRED_CAPABILITIES | rcap.OPTIONAL_CAPABILITIES
    assert blockers[0].capability == 'bounded_input'
    assert blockers[0].reason_code == 'no_readonly_enforcement'
    assert 'version=unknown' in blockers[0].scope
    assert info.reason_code == 'capability_review_required'
    assert info.capability_snapshot.get('bounded_input').state is rcap.CapabilityState.UNKNOWN
    assert info.capability_snapshot.missing_required()


# --- ISS-200 三审返修：逐条 N-x 的反例钉子 ---------------------------------


def test_probe_failure_snapshot_does_not_claim_required_capabilities(tmp_path):
    """N-1：探测失败路径的快照不得谎报「必需能力已受控实测」。

    反例：合成 ``exit 7`` 的可执行文件 → 版本探测失败 → 快照曾把六项必需
    能力全写成 supported/verified，missing_required() 为空。
    """
    broken = tmp_path / 'broken'
    broken.write_text('#!/bin/sh\nexit 7\n')
    broken.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[broken], login_shell_cmd=['/bin/echo'])

    assert info.availability is ar.Availability.BROKEN
    snapshot = info.capability_snapshot
    assert snapshot is not None
    # 必须真的缺证据，而不是「全绿」
    assert snapshot.missing_required()
    for name in rcap.REQUIRED_CAPABILITIES:
        capability = snapshot.get(name)
        assert capability is not None, name
        assert capability.state is not rcap.CapabilityState.SUPPORTED, name
        assert capability.evidence is not rcap.EvidenceStrength.VERIFIED, name


def test_probe_failure_snapshot_missing_required_is_nonempty(tmp_path):
    """N-1：聚合失败路径（多候选全失败）同样不得谎报 supported。"""
    broken = tmp_path / 'broken'
    broken.write_text('#!/bin/sh\nexit 9\n')
    broken.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[str(broken), str(broken) + '2'],
        login_shell_cmd=['/bin/echo'])
    snapshot = info.capability_snapshot
    assert snapshot is not None
    assert snapshot.missing_required()
    for name in rcap.REQUIRED_CAPABILITIES:
        capability = snapshot.get(name)
        assert capability is not None, name
        assert capability.state is not rcap.CapabilityState.SUPPORTED, name
        assert capability.evidence is not rcap.EvidenceStrength.VERIFIED, name


def test_probe_period_never_marks_required_capability_verified(tmp_path):
    """N-2：探测期（只跑 --version/--help）绝不允许出现 VERIFIED。

    这是首审 P0-2 的实质：把 ``gate_summary`` 换成另一句没证据的文案
    不算修复。探测期最强只到 HELP（参数存在性）。
    """
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n*) echo "'
                    + ('help text ' * 2200) + FLAGS['claude-code'] + '";;\nesac\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    snapshot = info.capability_snapshot
    assert snapshot is not None
    for capability in snapshot.capabilities:
        assert capability.evidence is not rcap.EvidenceStrength.VERIFIED, capability.name
    for name in rcap.REQUIRED_CAPABILITIES:
        assert snapshot.get(name).state is rcap.CapabilityState.UNKNOWN


def _call_fixture(tmp_path, capability="bounded_input"):
    entry = tmp_path / "entry"
    entry.write_bytes(b"synthetic executable identity")
    snapshot = rcap.build_snapshot(
        "fake", identity=rcap.identify_entry(entry), version="1.0.0",
        invocation_profile="fake:readonly")
    binding = rcap.bind_call(snapshot, [str(entry), "--readonly"], "call_0123456789abcdef")
    evidence = rcap.CallEvidence(
        capability=capability, argv_digest=binding.argv_digest,
        version=binding.version, profile=binding.profile, runtime_id=binding.runtime_id,
        entry_fingerprint=binding.entry_fingerprint, call_id=binding.call_id,
        succeeded=True, result_validated=True)
    return snapshot, binding, evidence


def test_apply_call_evidence_is_the_only_path_to_verified(tmp_path):
    """真实消费者未来须同时交出预先保管的调用记录和成功证据。"""
    snapshot, binding, evidence = _call_fixture(tmp_path)
    updated = rcap.apply_call_evidence(snapshot, evidence, expected_call=binding)
    assert updated.get("bounded_input").state is rcap.CapabilityState.SUPPORTED
    assert updated.get("bounded_input").evidence is rcap.EvidenceStrength.VERIFIED
    assert "version=1.0.0" in updated.get("bounded_input").scope
    assert evidence.call_id in updated.get("bounded_input").scope
    assert snapshot.get("bounded_input").state is rcap.CapabilityState.UNKNOWN


@pytest.mark.parametrize("capability", ["bounded_input", "streaming"])
@pytest.mark.parametrize("field,value", [
    ("runtime_id", "foreign"), ("version", "9.9.9"), ("profile", "unrestricted"),
    ("entry_fingerprint", "f" * 64), ("call_id", "call_another_0123456789"),
    ("argv_digest", "sha256:" + "f" * 64), ("argv_digest", "not-a-digest"),
    ("version", None), ("profile", ""), ("succeeded", False),
    ("result_validated", False),
])
def test_apply_call_evidence_fails_closed_on_incomplete_binding(tmp_path, capability, field, value):
    snapshot, binding, evidence = _call_fixture(tmp_path, capability)
    with pytest.raises(ValueError):
        rcap.apply_call_evidence(snapshot, replace(evidence, **{field: value}), expected_call=binding)


def test_call_binding_must_match_target_and_be_independent(tmp_path):
    snapshot, binding, evidence = _call_fixture(tmp_path)
    with pytest.raises(ValueError):
        rcap.apply_call_evidence(snapshot, evidence)
    for other in (replace(snapshot, runtime_id="foreign"), replace(snapshot, version="2.0.0"),
                  replace(snapshot, invocation_profile="unrestricted"),
                  replace(snapshot, identity=replace(snapshot.identity, content_digest="f" * 64))):
        with pytest.raises(ValueError):
            rcap.apply_call_evidence(other, evidence, expected_call=binding)
    with pytest.raises(ValueError):
        rcap.bind_call(snapshot, ["/foreign/entry"], binding.call_id)
    with pytest.raises(ValueError):
        rcap.apply_call_evidence(snapshot, replace(evidence, capability="unknown"), expected_call=binding)


def test_script_layers_does_not_read_whole_large_binary(tmp_path):
    """N-3：非脚本大文件不得被整读进内存。

    反例：260 MiB 级 Mach-O 若被 read_text() 整读，单次探测要 5 s 且峰值
    RSS 2.3 GiB，会吃穿 20 s 总预算。本用例用小阈值夹具证明「先读头部
    判 shebang，非脚本立即返回」，不真造大文件。
    """
    from fathom import runtime_capabilities as rcap
    binary = tmp_path / 'big'
    # Mach-O 魔数开头（非 #!），故意不含换行与 shebang
    binary.write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 4096)
    binary.chmod(0o755)

    assert rcap._script_layers(binary) == ()
    # 大于脚本解析上限时也不整读
    original_limit = rcap._SCRIPT_PARSE_MAX_BYTES
    try:
        rcap._SCRIPT_PARSE_MAX_BYTES = 16
        assert rcap._script_layers(binary) == ()
    finally:
        rcap._SCRIPT_PARSE_MAX_BYTES = original_limit


def test_script_layers_still_parses_real_wrapper(tmp_path):
    """N-3：性能修复不得牺牲 wrapper 解析能力。"""
    from fathom import runtime_capabilities as rcap
    engine = tmp_path / 'engine'
    engine.write_text('#!/bin/sh\necho engine\n')
    engine.chmod(0o755)
    wrapper = tmp_path / 'wrapper'
    wrapper.write_text('#!/usr/bin/env bash\nAPP="%s"\nexec "$APP" "$@"\n' % engine)
    wrapper.chmod(0o755)
    layers = rcap._script_layers(wrapper)
    assert layers
    assert str(engine) in layers


def test_blocker_capability_and_reason_code_are_separated():
    """N-4：capability 是真实能力名，reason_code 是原因码，二者不得同值。"""
    from fathom import runtime_capabilities as rcap
    for meta in ar.CANDIDATES.values():
        assert meta.cap_failed_capability in (
            rcap.REQUIRED_CAPABILITIES | rcap.OPTIONAL_CAPABILITIES), meta.id
        assert meta.cap_reason_code != meta.cap_failed_capability, meta.id
        assert meta.cap_reason_code not in (
            rcap.REQUIRED_CAPABILITIES | rcap.OPTIONAL_CAPABILITIES), meta.id


def test_large_file_middle_change_is_detected(tmp_path):
    """N-5：大文件中段改动必须被发现（长度不变、mtime 还原）。

    反例：300 KiB 文件改第 150 KiB 处，头尾采样完全看不到 → 曾判
    ``entry_changed=False``。现改为分块全文件原始哈希。
    """
    from fathom import runtime_capabilities as rcap
    script = tmp_path / 'runtime'
    payload = bytearray(b'A' * (300 * 1024))
    script.write_bytes(bytes(payload))
    script.chmod(0o755)
    before = rcap.identify_entry(script)
    stat_before = script.stat()

    payload[150 * 1024:150 * 1024 + 16] = b'C' * 16
    script.write_bytes(bytes(payload))
    script.chmod(0o755)
    os.utime(script, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    after = rcap.identify_entry(script)

    assert before is not None and after is not None
    assert before.size_bytes == after.size_bytes
    assert before.mtime_ns == after.mtime_ns
    assert before.digest_scope == 'full_sha256'
    assert rcap.entry_changed(before, after) is True


def test_large_file_crc_collision_cannot_preserve_content_identity(tmp_path):
    """F4：真实9MiB、同头尾/长度/mtime/CRC32，中段碰撞仍必须失效。"""
    import hashlib
    import zlib
    payload = bytearray(b"A" * (9 * 1024 * 1024))
    entry = tmp_path / "large"
    entry.write_bytes(payload)
    before = rcap.identify_entry(entry)
    stat = entry.stat()
    original_sha = hashlib.sha256(payload).hexdigest()
    original_crc = zlib.crc32(payload)
    payload[4 * 1024 * 1024] ^= 1
    offset = 4 * 1024 * 1024 + 100
    baseline_crc = zlib.crc32(payload)
    basis = {}
    for i in range(32):
        payload[offset + i // 8] ^= 1 << (i % 8)
        value = zlib.crc32(payload) ^ baseline_crc
        payload[offset + i // 8] ^= 1 << (i % 8)
        mask = 1 << i
        while value:
            pivot = value.bit_length() - 1
            if pivot not in basis:
                basis[pivot] = (value, mask)
                break
            row, bits = basis[pivot]
            value ^= row
            mask ^= bits
    target = original_crc ^ baseline_crc
    solution = 0
    while target:
        row, bits = basis[target.bit_length() - 1]
        target ^= row
        solution ^= bits
    for i in range(32):
        if solution & (1 << i):
            payload[offset + i // 8] ^= 1 << (i % 8)
    assert zlib.crc32(payload) == original_crc
    assert hashlib.sha256(payload).hexdigest() != original_sha
    entry.write_bytes(payload)
    os.utime(entry, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = rcap.identify_entry(entry)
    assert before.digest_scope == after.digest_scope == "full_sha256"
    assert before.size_bytes == after.size_bytes and before.mtime_ns == after.mtime_ns
    assert before.content_digest != after.content_digest
    assert rcap.entry_changed(before, after)


def test_parent_directory_symlink_is_not_a_wrapper_layer(tmp_path):
    """N-6：父目录含符号链接时，文件本身不是 wrapper。"""
    from fathom import runtime_capabilities as rcap
    real_dir = tmp_path / 'real'
    real_dir.mkdir()
    target = real_dir / 'tool'
    target.write_text('#!/bin/sh\necho hi\n')
    target.chmod(0o755)
    link_dir = tmp_path / 'linkdir'
    link_dir.symlink_to(real_dir, target_is_directory=True)

    linked = link_dir / 'tool'
    identity = rcap.identify_entry(linked)
    assert identity is not None
    assert identity.wrapper_kind == 'direct'
    assert identity.is_wrapper is False
    # 文件本身是链接时仍应识别为 symlink wrapper
    link_file = tmp_path / 'linkfile'
    link_file.symlink_to(target)
    sym = rcap.identify_entry(link_file)
    assert sym is not None
    assert sym.wrapper_kind == 'symlink'
    assert sym.is_wrapper is True


def test_snapshot_public_dict_hides_local_paths(tmp_path):
    """P2-3：用户可见响应不得含本机绝对路径与内容摘要。"""
    from fathom import runtime_capabilities as rcap
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\necho hi\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    public = info.public_dict()
    blob = repr(public['capability_snapshot'])
    assert str(tmp_path) not in blob
    assert 'configured_path' not in public['capability_snapshot']['identity']
    assert 'resolved_path' not in public['capability_snapshot']['identity']
    assert 'content_digest' not in public['capability_snapshot']['identity']
    assert 'mtime_ns' not in public['capability_snapshot']['identity']
    # 脱敏后仍保留可判定的结构信息
    assert public['capability_snapshot']['identity']['wrapper_kind']
    assert public['capability_snapshot']['identity']['fingerprint']


def test_snapshot_carries_registry_evidence(tmp_path):
    """P2-2：注册表里最扎实的证据要结构化进快照。"""
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n*) echo "'
                    + ('help text ' * 2200) + FLAGS['claude-code'] + '";;\nesac\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    snapshot = info.capability_snapshot
    assert snapshot is not None
    assert '--bare' in snapshot.declared_required_flags
    assert snapshot.verified_versions == tuple(sorted(meta.verified_versions))
    assert snapshot.capability_cap == meta.capability_cap.value


def test_capability_detail_is_filled(tmp_path):
    """P2-1：Capability.detail 不得恒空。"""
    script = tmp_path / 'runtime'
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n*) echo "'
                    + ('help text ' * 2200) + FLAGS['claude-code'] + '";;\nesac\n')
    script.chmod(0o755)
    meta = replace(ar.get_candidate('claude-code'), known_locations=())
    info = ar.detect_runtime(
        meta, path_env='', extra_locations=[script], login_shell_cmd=['/bin/echo'])

    snapshot = info.capability_snapshot
    assert snapshot is not None
    for capability in snapshot.capabilities:
        assert capability.detail, capability.name


@pytest.mark.parametrize("kind", ["engine", "interpreter", "engine_symlink", "env_interpreter"])
def test_every_resolved_execution_layer_invalidates_on_replacement(tmp_path, kind):
    engine = tmp_path / "engine"
    engine.write_text("#!/bin/sh\necho behavior-1\n")
    engine.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(engine)
    entry = tmp_path / "wrapper"
    if kind == "engine":
        entry.write_text(f'#!/bin/sh\nexec {engine} "$@"\n')
    elif kind == "engine_symlink":
        entry.write_text(f'#!/bin/sh\nexec {alias} "$@"\n')
    elif kind == "env_interpreter":
        entry.write_text('#!/usr/bin/env -S engine --synthetic\necho same\n')
    else:
        entry.write_text(f'#!{engine}\necho same\n')
    before = rcap.identify_entry(entry, path_env=str(tmp_path))
    assert str(engine) in before.layers
    assert any(layer[1] == str(engine) for layer in before.layer_identities)
    oldstat = engine.stat()
    engine.write_text("#!/bin/sh\necho behavior-2\n")
    os.utime(engine, ns=(oldstat.st_atime_ns, oldstat.st_mtime_ns))
    after = rcap.identify_entry(entry, path_env=str(tmp_path))
    assert before.fingerprint() != after.fingerprint()
    assert rcap.entry_changed(before, after)
    if kind == "engine_symlink":
        other = tmp_path / "other"
        other.write_text(engine.read_text())
        alias.unlink()
        alias.symlink_to(other)
        redirected = rcap.identify_entry(entry)
        assert rcap.entry_changed(after, redirected)


@pytest.mark.parametrize("command", ['exec "$DYNAMIC_TARGET" "$@"',
                                     'exec /usr/bin/env "$DYNAMIC_TARGET" "$@"'])
def test_dynamic_wrapper_identity_is_incomplete_and_cannot_bind(tmp_path, command):
    entry = tmp_path / "dynamic"
    entry.write_text('#!/bin/sh\n' + command + '\n')
    identity = rcap.identify_entry(entry)
    assert not identity.identity_complete
    assert "dynamic_or_unresolved_layer" in identity.identity_issues
    assert rcap.entry_changed(identity, rcap.identify_entry(entry))
    snapshot = rcap.build_snapshot("fake", identity=identity, version="1.0.0", invocation_profile="p")
    with pytest.raises(ValueError):
        rcap.bind_call(snapshot, [str(entry)], "call_0123456789abcdef")


def test_new_zcode_keeps_original_historical_scope_without_verified(tmp_path):
    entry = tmp_path / "zcode"
    entry.write_text('#!/bin/sh\necho 999.0.0\n')
    entry.chmod(0o755)
    meta = replace(ar.get_candidate("zcode"), known_locations=())
    info = ar.detect_runtime(meta, path_env="", extra_locations=[entry], only_extra_locations=True)
    assert info.availability is ar.Availability.UNSUPPORTED
    assert info.reason_code == "capability_review_required"
    snap = info.capability_snapshot
    assert snap.version == "999.0.0"
    blocker = snap.scoped_blockers[0]
    assert blocker.version == "0.16.9"
    assert blocker.profile == "zcode-plan-mcp"
    assert "999.0.0" not in blocker.scope
    assert snap.get("bounded_input").state is rcap.CapabilityState.UNKNOWN
    assert all(c.evidence is not rcap.EvidenceStrength.VERIFIED for c in snap.capabilities)


def test_historical_blocker_only_applies_to_exact_observed_scope(tmp_path):
    entry = tmp_path / "zcode"
    entry.write_bytes(b"synthetic")
    identity = rcap.identify_entry(entry)
    blocker = rcap.ScopedBlocker("bounded_input", "known_negative",
        "version=0.16.9;profile=zcode-plan-mcp", "observed failure", "zcode", "0.16.9",
        "zcode-plan-mcp", identity.fingerprint())
    meta = replace(ar.get_candidate("zcode"), known_locations=())
    exact = ar._capability_snapshot_for(meta, str(entry), "0.16.9", blocker=blocker,
                                      invocation_profile="zcode-plan-mcp")
    assert exact.get("bounded_input").state is rcap.CapabilityState.UNSUPPORTED
    for version, profile in (("999.0.0", "zcode-plan-mcp"), ("0.16.9", "zcode")):
        other = ar._capability_snapshot_for(meta, str(entry), version, blocker=blocker,
                                          invocation_profile=profile)
        assert other.get("bounded_input").state is rcap.CapabilityState.UNKNOWN
    assert all(c.evidence is not rcap.EvidenceStrength.VERIFIED for c in exact.capabilities)


@pytest.mark.parametrize("outcome", [ar.RunOutcome.TIMED_OUT, ar.RunOutcome.NONZERO_EXIT])
def test_help_failure_has_truthful_snapshot(tmp_path, outcome):
    entry = tmp_path / "entry"
    entry.write_bytes(b"synthetic")
    class Runner:
        def run(self, invocation, **kwargs):
            if "--version" in invocation.argv:
                return ar.RunResult(ar.RunOutcome.OK, 0, "2.1.999 (Claude Code)", "")
            return ar.RunResult(outcome, 7, "", "SYNTHETIC_SECRET")
    info, _ = ar._version_probe(str(entry), ar.get_candidate("claude-code"), ar.ProbeBudget(),
                               Runner(), time.monotonic() + 5, [])
    assert info.reason_code == "capability_probe_failed"
    assert info.capability_snapshot is not None
    assert info.capability_snapshot.missing_required()
    assert all(c.evidence is not rcap.EvidenceStrength.VERIFIED
               for c in info.capability_snapshot.capabilities)
    assert "SYNTHETIC_SECRET" not in info.detail


def test_expired_probe_returns_snapshot_without_reading_files(tmp_path):
    entry = tmp_path / "entry"
    from pathlib import Path
    with patch.object(Path, "open", side_effect=AssertionError("expired identity read")):
        info, _ = ar._version_probe(str(entry), ar.get_candidate("claude-code"), ar.ProbeBudget(),
                                   ar.AgentCliRunner(), time.monotonic() - 1, [])
    assert info.reason_code == "probe_budget_exhausted"
    assert info.capability_snapshot is not None
    assert not info.capability_snapshot.identity.identity_complete
    assert info.capability_snapshot.missing_required()


def test_budget_expired_after_version_returns_snapshot_without_hashing(tmp_path):
    entry = tmp_path / "entry"
    class Runner:
        def run(self, *args, **kwargs):
            time.sleep(.02)
            return ar.RunResult(ar.RunOutcome.OK, 0, "2.1.999 (Claude Code)", "")
    from pathlib import Path
    with patch.object(Path, "open", side_effect=AssertionError("expired identity read")):
        info, _ = ar._version_probe(str(entry), ar.get_candidate("claude-code"), ar.ProbeBudget(),
                                   Runner(), time.monotonic() + .01, [])
    assert info.reason_code == "probe_budget_exhausted"
    assert info.capability_snapshot is not None
    assert info.capability_snapshot.missing_required()
    assert not info.capability_snapshot.identity.identity_complete


# F2-R: lack of a recognized exec is not proof of a dependency-free shell.
@pytest.mark.parametrize("command", [
    '{engine} "$@"',
    'if true; then exec {engine} "$@"; fi',
    'true; exec {engine} "$@"',
    'true && exec {engine} "$@"',
    '{engine} "$@" | cat',
    'forward() {{ exec {engine} "$@"; }}\nforward "$@"',
    'if true\nthen\nexec {engine} "$@"\nfi',
    'case "$1" in *) exec {engine} "$@";; esac',
    '"$DYNAMIC_TARGET" "$@"',
    'command {engine} "$@"',
    'eval "{engine}"',
    'exec {engine} "$@"; {engine} --extra',
    'APP={engine} OTHER=ignored\nexec "$APP" "$@"',
    'APP={engine}\nAPP=\nexec "$APP" "$@"',
    'APP={engine}\nexec $APP "$@"',
    'APP={engine}\nprintf -v APP other\nexec "$APP" "$@"',
])
def test_unproven_shell_family_cannot_bind_after_same_version_engine_change(tmp_path, command):
    """Real files, unchanged wrapper/version/engine length/mtime; no script execution."""
    engine = tmp_path / "engine"
    engine.write_text("#!/bin/sh\necho 1.0.0 behavior-1\n")
    entry = tmp_path / "wrapper"
    entry.write_text("#!/bin/sh\n" + command.format(engine=engine) + "\n")
    before = rcap.identify_entry(entry)
    oldstat = engine.stat()
    engine.write_text("#!/bin/sh\necho 1.0.0 behavior-2\n")
    os.utime(engine, ns=(oldstat.st_atime_ns, oldstat.st_mtime_ns))
    after = rcap.identify_entry(entry)
    assert engine.stat().st_size == oldstat.st_size
    assert engine.stat().st_mtime_ns == oldstat.st_mtime_ns
    assert not before.identity_complete
    assert not after.identity_complete
    assert rcap.entry_changed(before, after)
    snapshot = rcap.build_snapshot("fake", identity=after, version="1.0.0", invocation_profile="p")
    with pytest.raises(ValueError):
        rcap.bind_call(snapshot, [str(entry)], "call_0123456789abcdef")


@pytest.mark.parametrize("command", [
    'exec "{engine}" "$@"',
    'APP="{engine}"\nexec "$APP" "$@"',
])
def test_proven_direct_exec_hashes_each_layer_and_can_bind(tmp_path, command):
    engine = tmp_path / "engine with space"
    engine.write_text("#!/bin/sh\necho 1.0.0 behavior-1\n")
    entry = tmp_path / "wrapper"
    entry.write_text("#!/bin/sh\n" + command.format(engine=engine) + "\n")
    before = rcap.identify_entry(entry)
    assert before.identity_complete
    assert str(engine) in before.layers
    assert all(len(record[4]) == 64 for record in before.layer_identities)
    snapshot = rcap.build_snapshot("fake", identity=before, version="1.0.0", invocation_profile="p")
    rcap.bind_call(snapshot, [str(entry)], "call_0123456789abcdef")
    oldstat = engine.stat()
    engine.write_text("#!/bin/sh\necho 1.0.0 behavior-2\n")
    os.utime(engine, ns=(oldstat.st_atime_ns, oldstat.st_mtime_ns))
    after = rcap.identify_entry(entry)
    assert after.identity_complete
    assert rcap.entry_changed(before, after)
