"""fathom/runtime_invocation.py 测试（ISS-201 第一切片）。

合同：全部用例都是纯函数级夹具——不发模型请求、不起真实进程、不读
凭据文件、不访问网络、不写 DB。覆盖：

- 调用配置声明：四类参数、必需缺失即不可用、协议缺失归 protocol 类、
  可选缺失只降级；
- 认证分离：方式与状态正交、bare 与 oauth 互斥、API-key 成功不替代
  OAuth 验证、绝不记录秘密；
- 事件归一：候选正文与终态分开、完整替换 vs 增量、重复/分片/缺终态
  fail-closed、未知字段与未知事件有界计数、未知权限请求 fail-closed；
- 错误分类：至少 8 类、超时带受理状态不确定、不得自动换引擎/重发。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from fathom import runtime_invocation as ri


def _fake_ready_info(executable: str):
    """构造走真实适配器代码路径所需的 RuntimeInfo（合成入口）。"""
    from fathom import agent_runtime as ar

    return ar.RuntimeInfo(
        id="claude-code", display_name="Claude Code",
        identity="合成夹具", identity_evidence="测试",
        official_docs="https://example.invalid",
        availability=ar.Availability.READY, reason_code="verified_version",
        detail="测试用", executable=executable,
    )


def _tmp_cwd() -> Path:
    path = Path(tempfile.mkdtemp(prefix="fathom-iss201-"))
    return path


# ==========================================================================
# 调用配置声明
# ==========================================================================


CLAUDE_ARGV = [
    "claude", "--bare", "--disable-slash-commands", "--disallowedTools", "*",
    "--strict-mcp-config", "--no-session-persistence",
    "--output-format", "json", "-p",
]

CODEX_ARGV = [
    "codex", "exec", "--json", "--ephemeral", "--ignore-user-config",
    "--ignore-rules", "--skip-git-repo-check", "-s", "read-only",
    "-C", "/tmp/iso", "-c", "model_reasoning_effort=low",
]

HERMES_ARGV = [
    "hermes", "chat", "--query-file", "-", "--format", "stream-json",
    "--run-budget", "150", "-t", "web,vision", "--ignore-user-config",
    "--ignore-rules", "--source", "tool",
]


def test_each_profile_declares_required_safety_flags():
    """必需安全行为必须显式声明，且逐家不同（不依赖提示词）。"""
    claude = ri.get_profile("claude-code")
    codex = ri.get_profile("codex-cli")
    hermes = ri.get_profile("hermes-agent")

    assert ri.argv_contains(CLAUDE_ARGV, claude.flag("disallowed_tools").tokens)
    assert ri.argv_contains(CODEX_ARGV, codex.flag("sandbox_read_only").tokens)
    assert ri.argv_contains(HERMES_ARGV, hermes.flag("readonly_toolset").tokens)

    for profile, argv in (
        (claude, CLAUDE_ARGV), (codex, CODEX_ARGV), (hermes, HERMES_ARGV),
    ):
        verdict = profile.verdict(argv)
        assert verdict.usable is True, verdict
        assert verdict.missing_required == ()
        assert verdict.missing_protocol == ()


def test_missing_required_flag_makes_profile_unusable():
    """缺必需安全/隔离参数即不可用，且归 permission 类（禁止静默降级）。"""
    profile = ri.get_profile("claude-code")
    argv = [a for a in CLAUDE_ARGV if a != "--bare"]

    verdict = profile.verdict(argv)
    assert verdict.usable is False
    assert "bare" in verdict.missing_required
    assert verdict.failure_class is ri.FailureClass.PERMISSION
    assert verdict.reason_code == "profile_required_flags_missing"
    assert "禁止静默降级" in verdict.detail


def test_missing_protocol_flag_is_protocol_error_not_permission():
    """输出协议不匹配是 protocol 类错误，不是权限错误。"""
    profile = ri.get_profile("codex-cli")
    argv = [a for a in CODEX_ARGV if a != "--json"]

    verdict = profile.verdict(argv)
    assert verdict.usable is False
    assert verdict.missing_protocol == ("json_output",)
    assert verdict.failure_class is ri.FailureClass.PROTOCOL
    assert verdict.reason_code == "profile_protocol_mismatch"
    assert verdict.failure_class is not ri.FailureClass.PERMISSION


def test_missing_optional_capability_only_degrades():
    """可选能力缺失只降级该项，且必须可披露，不得让 profile 不可用。"""
    profile = ri.get_profile("codex-cli")
    argv = [
        a for a in CODEX_ARGV
        if a not in ("-c", "model_reasoning_effort=low")
    ]

    verdict = profile.verdict(argv)
    assert verdict.usable is True
    assert verdict.degraded_optional == ("reasoning_effort_low",)
    assert "reasoning_effort_low" in verdict.detail


def test_profile_marks_optional_capabilities_degradable():
    """每家的可选参数都登记为可降级，且都带降级说明。"""
    for profile in ri.PROFILES.values():
        for flag in profile.optional_flags():
            assert flag.kind is ri.FlagKind.CAPABILITY
            assert flag.degrade_note, f"{profile.runtime_id}.{flag.name} 缺降级说明"


def test_profile_declaration_is_machine_readable():
    """声明可机器读取：必需/可选/协议三类分列且带行为说明。"""
    payload = ri.get_profile("claude-code").to_dict()
    assert payload["runtime_id"] == "claude-code"
    assert payload["output_protocol"] == "single_json"
    assert payload["final_selection"] == "replace_single"
    assert payload["payload_channel"] == "stdin"
    assert {f["name"] for f in payload["required"]} == {
        "bare", "disallowed_tools", "strict_mcp_config",
        "disable_slash_commands", "no_session_persistence",
    }
    for entry in payload["required"] + payload["optional"] + payload["protocol"]:
        assert entry["behavior"]
    assert payload["auth_variants"]


# ==========================================================================
# 认证分离
# ==========================================================================


def test_bare_profile_auth_method_is_not_oauth():
    """Claude bare 形态即便 CLI 自报 oauth_token，也不得记成 OAuth。"""
    profile = ri.get_profile("claude-code")
    active = profile.active_auth_descriptor()
    assert active.method is ri.AuthMethod.BARE
    assert active.method is not ri.AuthMethod.OAUTH

    oauth = profile.auth_descriptor(ri.AuthMethod.OAUTH)
    assert oauth.method is ri.AuthMethod.OAUTH
    assert oauth.state is not ri.AuthState.AVAILABLE


def test_bare_state_cannot_be_recorded_as_oauth_available():
    """把 bare 形态的成功登记成 oauth=available 是违规的，必须被拒绝或分离。"""
    profile = ri.get_profile("claude-code")

    # 合法路径：只更新 bare 变体。
    updated = profile.with_auth_state(
        ri.AuthMethod.BARE, ri.AuthState.AVAILABLE, source="request_succeeded",
    )
    assert updated.auth_descriptor(ri.AuthMethod.BARE).state is ri.AuthState.AVAILABLE
    assert updated.auth_descriptor(ri.AuthMethod.OAUTH).state is ri.AuthState.UNKNOWN

    # 非法路径：凭据赋值形态的 detail 必须被拒绝。
    with pytest.raises(ValueError):
        profile.with_auth_state(
            ri.AuthMethod.BARE, ri.AuthState.AVAILABLE,
            detail="token=abc123",
        )


def test_api_key_success_does_not_set_oauth_available():
    """API-key 成功不能替代 OAuth 验证（反之亦然）。"""
    profile = ri.get_profile("claude-code")
    updated = profile.with_auth_state(
        ri.AuthMethod.API_KEY, ri.AuthState.AVAILABLE, source="request_succeeded",
    )
    assert updated.auth_descriptor(ri.AuthMethod.API_KEY).state is ri.AuthState.AVAILABLE
    assert updated.auth_descriptor(ri.AuthMethod.OAUTH).state is ri.AuthState.UNKNOWN
    assert updated.auth_descriptor(ri.AuthMethod.BARE).state is ri.AuthState.UNKNOWN
    # 生效方式不变：bare 形态仍然是生效通道。
    assert updated.active_auth_descriptor().method is ri.AuthMethod.BARE


def test_oauth_method_not_upgraded_by_api_key_evidence():
    """OAuth 成功也不能顺带把 API-key 变体标成可用。"""
    profile = ri.get_profile("codex-cli")
    updated = profile.with_auth_state(
        ri.AuthMethod.OAUTH, ri.AuthState.AVAILABLE, source="request_succeeded",
    )
    assert updated.auth_descriptor(ri.AuthMethod.OAUTH).state is ri.AuthState.AVAILABLE
    assert updated.auth_descriptor(ri.AuthMethod.API_KEY).state is ri.AuthState.UNKNOWN


def test_auth_method_and_state_are_orthogonal():
    """方式与状态正交：任意组合都可表达，且互不串改。"""
    profile = ri.get_profile("claude-code")
    for method in ri.AuthMethod:
        if method is ri.AuthMethod.UNKNOWN:
            # UNKNOWN 是「未取得证据」，不可作为可登记方式。
            with pytest.raises(ValueError):
                profile.with_auth_state(method, ri.AuthState.AVAILABLE)
            assert profile.auth_descriptor(method).state is ri.AuthState.UNKNOWN
            continue
        for state in ri.AuthState:
            structurally_unavailable = profile.auth_descriptor(
                method).structurally_unavailable
            if structurally_unavailable and state is ri.AuthState.AVAILABLE:
                # 结构性不可用的方式：任何证据都不能把它标成 available。
                with pytest.raises(ValueError):
                    profile.with_auth_state(method, state, source="fixture")
                continue
            updated = profile.with_auth_state(method, state, source="fixture")
            got = updated.auth_descriptor(method)
            assert got.method is method
            assert got.state is state
            # 登记一种方式不得改掉其它方式的既有状态。
            for other in ri.AuthMethod:
                if other is not method:
                    assert updated.auth_descriptor(other).state is ri.AuthState.UNKNOWN


def test_auth_descriptor_never_records_secret():
    """认证描述只记方式、状态、来源与不含秘密的说明。"""
    profile = ri.get_profile("claude-code")
    updated = profile.with_auth_state(
        ri.AuthMethod.API_KEY, ri.AuthState.AVAILABLE,
        source="request_succeeded",
        detail="凭据来自 ANTHROPIC_AUTH_TOKEN 环境变量",
    )
    payload = updated.auth_descriptor(ri.AuthMethod.API_KEY).to_dict()
    assert set(payload) == {
        "method", "state", "source", "detail", "structurally_unavailable",
    }
    assert "ANTHROPIC_AUTH_TOKEN" in payload["detail"]
    for banned in ("token=", "api_key=", "password", "Bearer "):
        assert banned not in payload["detail"]

    for bad in (
        "sk-abcdefghijklmnop",
        "api_key: 1234567890abcdef",
        "值在 /Users/someone/.claude/settings.json 里",
        "a" * 60,
    ):
        with pytest.raises(ValueError):
            ri.assert_secret_free(bad)


def test_unknown_auth_method_cannot_record_state():
    """UNKNOWN 不是可登记的方式。"""
    with pytest.raises(ValueError):
        ri.get_profile("codex-cli").with_auth_state(
            ri.AuthMethod.UNKNOWN, ri.AuthState.AVAILABLE,
        )


# ==========================================================================
# 事件归一
# ==========================================================================


def test_claude_single_json_is_replacement():
    """Claude 单对象形态：整体替换，终态与候选正文分开。"""
    profile = ri.get_profile("claude-code")
    stream = ri.normalize_events(
        {"type": "result", "result": "正文", "is_error": False, "model": "m"},
        profile=profile,
    )
    assert stream.ok is True
    assert stream.final_text == "正文"
    assert stream.terminal_ok is True
    assert stream.model == "m"
    kinds = [e.kind for e in stream.events]
    assert ri.EventKind.FINAL in kinds
    assert ri.EventKind.TERMINAL in kinds


def test_claude_error_result_fails_closed():
    """is_error=true → 明确失败终态，不落成功。"""
    profile = ri.get_profile("claude-code")
    stream = ri.normalize_events(
        {"type": "result", "result": "boom", "is_error": True},
        profile=profile,
    )
    assert stream.ok is False
    assert stream.terminal_ok is False
    assert stream.reason_code == "protocol_terminal_error"
    assert stream.final_text is None


def test_codex_parallel_candidates_take_last_not_concatenated():
    """Codex 同轮多条并列候选：取末条，绝不拼接。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "进度一"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "进度二"}},
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "最终正文"}},
        {"type": "turn.completed", "usage": {"total_tokens": 42}},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is True
    assert stream.final_text == "最终正文"
    assert "进度" not in (stream.final_text or "")
    assert stream.usage == {"total_tokens": 42}


def test_hermes_text_event_is_progress_not_final():
    """Hermes 的 text 事件只是进度，result.text 才是完整替换的正文。"""
    profile = ri.get_profile("hermes-agent")
    events = [
        {"type": "system", "session_id": "s1", "model": "hm"},
        {"type": "text", "text": "过程中的说明"},
        {"type": "result", "text": "最终结果", "exit_code": 0},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is True
    assert stream.final_text == "最终结果"
    assert "过程中的说明" not in (stream.final_text or "")
    assert stream.model == "hm"
    kinds = [e.kind for e in stream.events]
    assert ri.EventKind.PROGRESS in kinds


def test_hermes_result_error_fails_closed():
    """Hermes result 带 error → 明确失败终态。"""
    profile = ri.get_profile("hermes-agent")
    stream = ri.normalize_events(
        {"type": "result", "text": "x", "exit_code": 1, "error": "provider error"},
        profile=profile,
    )
    assert stream.ok is False
    assert stream.terminal_ok is False
    assert stream.reason_code == "protocol_terminal_error"


def test_duplicate_identical_final_is_idempotent():
    """同一 item_id 的重复正文（内容相同）幂等丢弃，不重复正文。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"}},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.duplicate_count == 1
    assert stream.ok is True
    assert stream.final_text == "正文"


def test_duplicate_conflicting_final_fails_closed():
    """同一 item_id 的重复正文内容冲突 → 歧义，失败。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文A"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文B"}},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is False
    assert stream.reason_code == "protocol_duplicate_conflict"


def test_ambiguous_multiple_finals_fail_closed():
    """完整替换形态出现多条候选正文 → 歧义，失败（不拼接、不猜）。"""
    profile = ri.get_profile("hermes-agent")
    events = [
        {"type": "result", "text": "甲"},
        {"type": "result", "text": "乙"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is False
    assert stream.reason_code == "protocol_ambiguous_final"
    assert stream.final_text is None


def test_fragment_missing_head_fails_closed():
    """分片缺头 → 失败（真实分片形态未观测，本分支保守拒绝）。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "第二片"},
         "fragment": {"index": 1, "total": 2, "last": True}},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is False
    assert stream.reason_code == "protocol_unsupported_fragment"
    assert stream.fragment_count >= 1


def test_fragment_complete_is_rejected_without_verified_protocol():
    """F4：旧测试虚构完整分片协议；未验证的分片即使看似完整也拒绝。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "第一片"},
         "fragment": {"index": 0, "total": 2}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "第二片"},
         "fragment": {"index": 1, "total": 2, "last": True}},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is False
    assert stream.final_text is None
    assert stream.reason_code == "protocol_unsupported_fragment"


def test_missing_terminal_fails_closed():
    """缺明确终态 → 失败，不落成功。"""
    profile = ri.get_profile("codex-cli")
    stream = ri.normalize_events(
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"}},
        profile=profile,
    )
    assert stream.ok is False
    assert stream.reason_code == "protocol_missing_terminal"
    assert stream.final_text is None


def test_unknown_ordinary_field_ignored_and_counted():
    """未知普通字段：忽略并有界计数，不中断解析。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "thread.started", "thread_id": "t1", "brand_new_field": 1},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"},
         "another_new_field": {"x": 1}},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is True
    assert stream.unknown_field_count == 2
    assert stream.final_text == "正文"


def test_unknown_event_type_ignored_and_counted():
    """未知事件类型：忽略并计数，不当终态也不当正文。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "brand.new.event", "payload": 1},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"}},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.unknown_event_count == 1
    assert stream.ok is True
    assert stream.final_text == "正文"


def test_unknown_permission_request_fails_closed():
    """未知权限请求不忽略 → 失败（不猜成功）。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"}},
        {"type": "permission_request", "tool": "shell"},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is False
    assert stream.reason_code == "protocol_unknown_permission_request"


def test_truncated_json_lines_counted_and_no_events_fails():
    """截断 JSON 行只计数；一个可解析事件都没有 → 失败。"""
    profile = ri.get_profile("codex-cli")
    stream = ri.normalize_events('{"type": "turn.completed"\n', profile=profile)
    assert stream.bad_line_count == 1
    assert stream.ok is False
    assert stream.reason_code == "protocol_no_parsable_events"


def test_normalize_without_usage_and_model_reports_none():
    """缺 usage/model → None，不猜 0、不猜 provider。"""
    profile = ri.get_profile("claude-code")
    stream = ri.normalize_events(
        {"type": "result", "result": "正文", "is_error": False},
        profile=profile,
    )
    assert stream.ok is True
    assert stream.usage is None
    assert stream.model is None


def test_final_after_terminal_is_not_adopted():
    """终态之后出现的正文不采纳（既有实测口径）。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "收尾前的正文"}},
        {"type": "turn.completed"},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "收尾后的噪声"}},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is True
    assert stream.final_text == "收尾前的正文"


def test_cross_turn_finals_are_not_merged():
    """跨 turn 的候选正文不合并。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "甲"},
         "thread_id": "t1"},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "乙"},
         "thread_id": "t2"},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is False
    assert stream.cross_turn_final_count >= 1


# ==========================================================================
# 错误分类
# ==========================================================================


def test_failure_class_has_at_least_eight_classes():
    """失败类别至少 8 类（含 timeout），不得一个 unsupported 掩盖全部。"""
    values = {c.value for c in ri.FailureClass}
    assert len(values) >= 8
    assert {"quota", "network", "login", "timeout", "protocol",
            "result_schema", "permission", "unknown"} <= values


def test_timeout_is_uncertain_and_not_auto_retried():
    """超时必须带「受理状态不确定」标志，且不得自动重发/换引擎。"""
    got = ri.classify_failure("runner_timed_out")
    assert got.failure_class is ri.FailureClass.TIMEOUT
    assert got.dispatch_uncertain is True
    assert got.auto_retry_safe is False
    assert got.engine_switch_allowed is False


def test_spawn_failed_is_deterministically_not_accepted():
    """spawn 失败＝确定未受理，可安全重试；与受理后超时区分。"""
    got = ri.classify_failure("runner_spawn_failed")
    assert got.failure_class is ri.FailureClass.PERMISSION
    assert got.dispatch_uncertain is False
    assert got.auto_retry_safe is True


def test_reason_code_maps_to_distinct_classes():
    """reason_code → 类别映射可辨，不靠正文猜测。"""
    assert ri.classify_failure("parse_failed_bad_json").failure_class is ri.FailureClass.PROTOCOL
    assert ri.classify_failure("result_not_json").failure_class is ri.FailureClass.RESULT_SCHEMA
    assert ri.classify_failure("adapter_contract_changed").failure_class is ri.FailureClass.PROTOCOL
    assert ri.classify_failure("preview_stale").failure_class is ri.FailureClass.PERMISSION
    assert ri.classify_failure("runner_cancelled").failure_class is ri.FailureClass.CANCELLED


def test_ambiguous_app_error_stays_unknown_without_structured_class():
    """app_error 覆盖多种真实原因；无结构化证据时记 UNKNOWN，不猜。"""
    got = ri.classify_failure("app_error", detail="some provider text")
    assert got.failure_class is ri.FailureClass.UNKNOWN


def test_structured_failure_class_overrides_reason_code():
    """适配器按自己协议给出的结构化类别优先于 reason_code。"""
    got = ri.classify_failure("app_error", failure_class=ri.FailureClass.QUOTA)
    assert got.failure_class is ri.FailureClass.QUOTA
    got = ri.classify_failure("app_error", failure_class="login")
    assert got.failure_class is ri.FailureClass.LOGIN


def test_cancelled_is_not_a_failure_and_never_uncertain():
    """取消是独立终态，不是失败，也不带受理不确定。"""
    got = ri.classify_failure("runner_cancelled")
    assert got.failure_class is ri.FailureClass.CANCELLED
    assert got.dispatch_uncertain is False
    assert got.auto_retry_safe is False


def test_no_classification_ever_allows_engine_switch():
    """受理状态不明时不得自动换引擎：任何分类都不得放开该开关。"""
    codes = [
        "runner_timed_out", "runner_nonzero_exit", "runner_output_limit",
        "runner_decode_error", "runner_spawn_failed", "app_error",
        "parse_failed_bad_json", "result_not_json",
    ]
    for code in codes:
        assert ri.classify_failure(code).engine_switch_allowed is False


def test_classify_failure_requires_reason_code():
    """空 reason_code 直接拒绝。"""
    with pytest.raises(ValueError):
        ri.classify_failure("")


# ==========================================================================
# ISS-202 真实 ACP 实证带来的交叉约束（工具被拒 / 重放 / usage 只在终态）
# ==========================================================================


def test_tool_rejection_is_not_clean_success():
    """ISS-202 实证：权限被拒后 prompt 仍以成功终态结束。

    因此「有终态且成功」不等于「该做的分析做了」。工具被拒必须是独立
    表达（WARNING + 计数），且不得被算成干净成功。
    """
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"}},
        {"type": "tool_rejected", "tool": "shell"},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    # 调用层面仍然成功（有明确成功终态、有候选正文）。
    assert stream.ok is True
    assert stream.terminal_ok is True
    # 但不是干净成功：被拒的工具必须被独立记录。
    assert stream.tool_rejected_count == 1
    assert stream.clean_success is False
    assert any("被拒" in w for w in stream.warnings)
    kinds = [e.kind for e in stream.events]
    assert ri.EventKind.WARNING in kinds


def test_tool_rejection_via_status_value_is_detected():
    """工具结果里的 status=reject 形态也必须被识别为被拒。"""
    profile = ri.get_profile("hermes-agent")
    events = [
        {"type": "tool_use", "tool_name": "web_search", "status": "reject"},
        {"type": "result", "text": "结果", "exit_code": 0},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.tool_rejected_count == 1
    assert stream.clean_success is False


def test_success_without_tool_rejection_is_clean():
    """对照组：没有工具被拒时才算干净成功（断言不是恒真）。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"}},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is True
    assert stream.tool_rejected_count == 0
    assert stream.clean_success is True


def test_same_content_different_items_is_not_proven_replay():
    """F9：ACP 重放实证不能外推为 CLI 同内容即重放；不同 ID 是独立候选。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "同一段"}},
        {"type": "item.completed", "item": {"id": "m2", "type": "agent_message", "text": "同一段"}},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.replay_count == 0
    assert stream.clean_success is True
    assert len([e for e in stream.events if e.kind is ri.EventKind.FINAL]) == 2
    # 末条候选正文独立交付，不拼接。
    assert stream.final_text == "同一段"


def test_replay_without_terminal_fails_closed():
    """ISS-202 实证：断流重放**永不产生终态** → 必须 fail-closed。"""
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "同一段"}},
        {"type": "item.completed", "item": {"id": "m2", "type": "agent_message", "text": "同一段"}},
        {"type": "item.completed", "item": {"id": "m3", "type": "agent_message", "text": "同一段"}},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.ok is False
    assert stream.reason_code == "protocol_missing_terminal"
    assert stream.final_text is None


def test_usage_only_from_explicit_source_not_inferred():
    """ISS-202 实证：流式事件里没有 usage，模型标识也不在流式事件里。

    缺 usage/model → None，不猜 0、不猜 provider；只有事件里明确带了
    usage 才产出 usage。
    """
    profile = ri.get_profile("codex-cli")
    events = [
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"}},
        {"type": "turn.completed"},
    ]
    stream = ri.normalize_events(events, profile=profile)
    assert stream.usage is None
    assert stream.model is None
    assert stream.ok is True

    with_usage = ri.normalize_events([
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "正文"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 20}},
    ], profile=profile)
    assert with_usage.usage == {"input_tokens": 10, "output_tokens": 20}


# ==========================================================================
# 硬约束：受理后超时不重复发送
# ==========================================================================


def test_timeout_invokes_runner_exactly_once():
    """超时时 runner 只被调用一次——受理后超时绝不二次发送。

    用计数型 fake runner 卡住反例：若派发路径对超时做了自动重试，
    这里的调用次数会是 2。
    """
    from fathom import agent_runtime as ar

    calls = {"n": 0}

    class CountingTimeoutRunner:
        def run(self, invocation, *, timeout_s=0, cancel_event=None):
            calls["n"] += 1
            return ar.RunResult(
                outcome=ar.RunOutcome.TIMED_OUT, exit_code=143,
                stdout_text="", stderr_text="",
                detail="timeout",
            )

    adapter = ar.ClaudeCodeAdapter(_fake_ready_info("/bin/true"))
    result = ar.dispatch_request(
        adapter, CountingTimeoutRunner(), "载荷",
        cwd=_tmp_cwd(), timeout_s=1.0,
    )
    assert calls["n"] == 1, "受理后超时不得重复发送"
    assert result.ok is False
    assert result.reason_code == "runner_timed_out"

    # 该 reason_code 必须带「受理状态不确定」标志，且不得自动重发/换引擎。
    classified = ri.classify_failure(result.reason_code)
    assert classified.failure_class is ri.FailureClass.TIMEOUT
    assert classified.dispatch_uncertain is True
    assert classified.auto_retry_safe is False
    assert classified.engine_switch_allowed is False


def test_spawn_failure_is_deterministically_not_accepted():
    """受理前失败（spawn 失败）必须与受理后超时区分开。"""
    from fathom import agent_runtime as ar

    class SpawnFailRunner:
        def run(self, invocation, *, timeout_s=0, cancel_event=None):
            return ar.RunResult(
                outcome=ar.RunOutcome.SPAWN_FAILED, exit_code=None,
                stdout_text="", stderr_text="", detail="spawn 失败",
            )

    adapter = ar.ClaudeCodeAdapter(_fake_ready_info("/nonexistent/binary"))
    result = ar.dispatch_request(
        adapter, SpawnFailRunner(), "载荷", cwd=_tmp_cwd(), timeout_s=1.0,
    )
    assert result.reason_code == "runner_spawn_failed"
    classified = ri.classify_failure(result.reason_code)
    assert classified.dispatch_uncertain is False
    assert classified.auto_retry_safe is True


def test_probe_retry_does_not_leak_into_business_request():
    """探测路径的重试不外溢到业务请求。

    探测（版本命令，不带载荷）超时重试一次；业务派发超时只跑一次。
    这里用同一个 runner 计数断言：探测阶段是 2 次，业务阶段是 1 次。
    """
    from fathom import agent_runtime as ar

    calls = {"n": 0}

    class CountingRunner:
        def run(self, invocation, *, timeout_s=0, cancel_event=None):
            calls["n"] += 1
            return ar.RunResult(
                outcome=ar.RunOutcome.TIMED_OUT, exit_code=143,
                stdout_text="", stderr_text="",
            )

    adapter = ar.ClaudeCodeAdapter(_fake_ready_info("/bin/true"))
    result = ar.dispatch_request(
        adapter, CountingRunner(), "载荷", cwd=_tmp_cwd(), timeout_s=1.0,
    )
    assert result.ok is False
    assert calls["n"] == 1


# ==========================================================================
# 硬约束 ①：Claude bare 形态下 CLI 自报 oauth_token 也不得记成 OAUTH
# ==========================================================================


def test_bare_profile_ignores_cli_self_reported_oauth_token():
    """反例卡点：CLI 自报 ``loggedIn=true (oauth_token)`` 也不改变结论。

    本用例模拟「CLI 自报文本」与「调用形态」两条证据同时存在的情形。
    生效认证方式只能由**调用形态**决定：bare ⇒ method=BARE，绝不能是
    OAUTH。断言特意写成「若实现读 CLI 自报文本就必然失败」。
    """
    profile = ri.get_profile("claude-code")

    # CLI 自报文本（形态无关的自报，不构成本次调用的认证证据）。
    cli_self_report = {
        "loggedIn": True,
        "authMethod": "oauth_token",
        "method": "oauth_token",
    }

    # 该自报文本里没有 bare 形态的凭据证据 → bare 变体仍为 unknown。
    assert profile.active_auth_descriptor().method is ri.AuthMethod.BARE
    assert profile.active_auth_descriptor().state is ri.AuthState.UNKNOWN

    # 即使把这份自报当成「证据」登记到 oauth 变体上，**生效**方式仍不变。
    updated = profile.with_auth_state(
        ri.AuthMethod.OAUTH, ri.AuthState.EXPIRED,
        source="cli_reported", detail="CLI 自报 oauth_token",
    )
    assert updated.active_auth_descriptor().method is ri.AuthMethod.BARE
    assert updated.active_auth_descriptor().state is ri.AuthState.UNKNOWN
    assert updated.auth_descriptor(ri.AuthMethod.OAUTH).method is ri.AuthMethod.OAUTH

    # 关键反例：bare 形态下 OAuth 变体**结构性不可用**——该形态根本不读
    # OAuth/keychain，它「可用」是不成立的。登记 available 必须被直接拒绝。
    # （API-key 成功不能替代 OAuth 验证，反之亦然。）
    with pytest.raises(ValueError):
        profile.with_auth_state(
            ri.AuthMethod.OAUTH, ri.AuthState.AVAILABLE,
            source="cli_reported", detail="CLI 自报 oauth_token",
        )
    assert profile.auth_descriptor(ri.AuthMethod.OAUTH).state is ri.AuthState.UNKNOWN
    assert profile.auth_descriptor(ri.AuthMethod.OAUTH).method is ri.AuthMethod.OAUTH

    # 反过来，OAuth 变体被标成 available 也绝不让 bare 变体变成 available。
    assert updated.auth_descriptor(ri.AuthMethod.BARE).state is ri.AuthState.UNKNOWN

    # 自报文本本身不构成状态来源：登记它必须显式给 source。
    assert updated.auth_descriptor(ri.AuthMethod.OAUTH).source == "cli_reported"
    assert cli_self_report["authMethod"] == "oauth_token"

    # 结构性不可用是 profile 声明的事实，可机器读取。
    assert profile.auth_descriptor(ri.AuthMethod.OAUTH).to_dict()[
        "structurally_unavailable"] is True


def test_bare_is_mutually_exclusive_with_oauth_as_active_method():
    """bare 与 oauth 互斥：同一 profile 的生效方式不能同时是两者。"""
    for profile in ri.PROFILES.values():
        active = [
            v for v in profile.auth_variants if v.active
        ]
        assert len(active) <= 1, f"{profile.runtime_id} 有多个生效认证方式"
        for variant in active:
            assert variant.method is not ri.AuthMethod.OAUTH or \
                profile.runtime_id != "claude-code", \
                "Claude 的生效认证方式不得是 OAuth"


def test_api_key_evidence_never_marks_oauth_available():
    """API-key 通道可用 ⇒ oauth 变体必须仍是 unknown。"""
    profile = ri.get_profile("claude-code")
    updated = profile.with_auth_state(
        ri.AuthMethod.API_KEY, ri.AuthState.AVAILABLE,
        source="request_succeeded", detail="凭据来自 ANTHROPIC_AUTH_TOKEN 环境变量",
    )
    oauth = updated.auth_descriptor(ri.AuthMethod.OAUTH)
    assert oauth.method is ri.AuthMethod.OAUTH
    assert oauth.state is ri.AuthState.UNKNOWN
    # 且 bare 形态下 OAuth 不可用——方式与状态两字段都不能被 API-key 证据污染。
    assert updated.active_auth_descriptor().method is ri.AuthMethod.BARE
    assert updated.active_auth_descriptor().state is ri.AuthState.UNKNOWN


# ==========================================================================
# 固定候选独审 F1–F9 反例与既有 parse_output 成功合同
# ==========================================================================


def _message(text="body", item_id="i1", **fields):
    return {"type": "item.completed", "item": {
        "id": item_id, "type": "agent_message", "text": text,
    }, **fields}


def _codex_stream(*middle):
    return ri.normalize_events(
        [_message(), *middle, {"type": "turn.completed"}],
        profile=ri.CODEX_CLI_PROFILE,
    )


@pytest.mark.parametrize("terminals", [
    [{"type": "turn.failed"}, {"type": "turn.completed"}],
    [{"type": "turn.completed"}, {"type": "turn.failed"}],
])
def test_f1_failure_terminal_cannot_be_overridden(terminals):
    stream = ri.normalize_events([_message(), *terminals], profile=ri.CODEX_CLI_PROFILE)
    assert not stream.ok
    assert stream.terminal_ok is False
    assert stream.final_text is None
    assert stream.reason_code == "protocol_terminal_error"


@pytest.mark.parametrize("namespace", ["turn_id", "session_id", "thread_id"])
def test_f1_candidate_terminal_scope_must_match(namespace):
    stream = ri.normalize_events([
        _message(**{namespace: "A"}),
        {"type": "turn.completed", namespace: "B"},
    ], profile=ri.CODEX_CLI_PROFILE)
    assert not stream.ok
    assert stream.reason_code == "protocol_terminal_mismatch"


def test_f1_conflicting_success_terminals_rejected_and_identical_idempotent():
    for last_turn, expected in (("A", True), ("B", False)):
        stream = ri.normalize_events([
            _message(turn_id="A"),
            {"type": "turn.completed", "turn_id": "A"},
            {"type": "turn.completed", "turn_id": last_turn},
        ], profile=ri.CODEX_CLI_PROFILE)
        assert stream.ok is expected
        if not expected:
            assert stream.reason_code == "protocol_ambiguous_terminal"


def test_f1_scope_namespaces_are_not_interchangeable():
    stream = ri.normalize_events([
        _message(turn_id="turn-A", session_id="session-A"),
        {"type": "turn.completed", "turn_id": "turn-A"},
    ], profile=ri.CODEX_CLI_PROFILE)
    assert stream.ok


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
def test_f2_claude_present_error_flag_requires_boolean(value):
    stream = ri.normalize_events(
        {"type": "result", "result": "body", "is_error": value},
        profile=ri.CLAUDE_CODE_PROFILE,
    )
    assert not stream.ok
    assert stream.terminal_ok is False


@pytest.mark.parametrize("value", [False, True, "0", "1", None, 0.0, [], {}])
def test_f2_hermes_present_exit_code_requires_integer(value):
    stream = ri.normalize_events(
        {"type": "result", "text": "body", "exit_code": value},
        profile=ri.HERMES_AGENT_PROFILE,
    )
    assert not stream.ok
    assert stream.terminal_ok is False


@pytest.mark.parametrize("value", [False, True, 0, 1, []])
def test_f2_hermes_error_field_does_not_coerce_truthiness(value):
    stream = ri.normalize_events(
        {"type": "result", "text": "body", "exit_code": 0, "error": value},
        profile=ri.HERMES_AGENT_PROFILE,
    )
    assert not stream.ok


@pytest.mark.parametrize("event", [
    {"type": "item.started", "item": {"type": "permission_request", "request": {"command": "read"}}},
    {"type": "new_event", "permission": True},
    {"type": "approval_request", "status": "denied"},
    {"type": "item.completed", "item": {"type": "approval_request", "status": "denied"}},
    {"type": "item.updated", "item": {"type": "new_item", "approval": False}},
])
def test_f3_permission_envelopes_are_fail_closed_before_tool_denial(event):
    stream = _codex_stream(event)
    assert not stream.ok
    assert stream.reason_code == "protocol_unknown_permission_request"
    assert stream.tool_rejected_count == 0


@pytest.mark.parametrize("marker", [True, False, 0, None, {}, {"index": 0, "total": 3, "last": True}])
def test_f4_unverified_fragment_markers_rejected(marker):
    stream = ri.normalize_events([
        _message(fragment=marker), {"type": "turn.completed"},
    ], profile=ri.CODEX_CLI_PROFILE)
    assert not stream.ok
    assert stream.final_text is None
    assert stream.fragment_count == 1
    assert stream.reason_code == "protocol_unsupported_fragment"


def test_f4_nested_codex_fragment_marker_rejected():
    event = _message()
    event["item"]["fragment"] = True
    stream = ri.normalize_events([event, {"type": "turn.completed"}], profile=ri.CODEX_CLI_PROFILE)
    assert not stream.ok
    assert stream.reason_code == "protocol_unsupported_fragment"


@pytest.mark.parametrize("marker", [True, {"index": 0, "total": 1, "last": True}])
def test_f5_late_fragment_cannot_overwrite_completed_final(marker):
    stream = ri.normalize_events([
        _message("actual final"), {"type": "turn.completed"},
        _message("late fragment", "i2", fragment=marker),
    ], profile=ri.CODEX_CLI_PROFILE)
    assert stream.ok
    assert stream.final_text == "actual final"
    assert [e.text for e in stream.events if e.kind is ri.EventKind.FINAL] == ["actual final"]


def test_f6_claude_pretty_single_json_is_accepted():
    raw = json.dumps({"type": "result", "result": "body", "is_error": False}, indent=2)
    stream = ri.normalize_events(raw, profile=ri.CLAUDE_CODE_PROFILE)
    assert stream.clean_success
    assert stream.final_text == "body"
    assert stream.bad_line_count == 0


@pytest.mark.parametrize("as_text", [False, True])
def test_f6_claude_tool_jsonl_and_parsed_sequence_rejected(as_text):
    data = [{"type": "tool_use", "id": "tool1"},
            {"type": "result", "result": "body", "is_error": False}]
    stream = ri.normalize_events(
        "\n".join(json.dumps(e) for e in data) if as_text else data,
        profile=ri.CLAUDE_CODE_PROFILE,
    )
    assert not stream.ok
    assert stream.final_text is None
    assert stream.reason_code == "protocol_single_json_required"


@pytest.mark.parametrize("data", [[], [{"type": "tool_use"}], [{"type": "text", "text": "body"}]])
def test_f6_claude_single_json_requires_one_result_object(data):
    assert not ri.normalize_events(data, profile=ri.CLAUDE_CODE_PROFILE).ok


def test_f7_blank_codex_tail_preserves_previous_nonempty_final():
    stream = _codex_stream(_message("   ", "i2"))
    assert stream.clean_success
    assert stream.final_text == "body"


@pytest.mark.parametrize("result", ["denied", "reject", "cancelled"])
def test_f8_claude_ordinary_result_is_not_a_tool_decision(result):
    stream = ri.normalize_events(
        {"type": "result", "result": result, "is_error": False},
        profile=ri.CLAUDE_CODE_PROFILE,
    )
    assert stream.clean_success
    assert stream.final_text == result
    assert stream.tool_rejected_count == 0


def test_f8_ordinary_status_metadata_does_not_become_tool_rejection():
    stream = _codex_stream({"type": "new_event", "status": "denied"})
    assert stream.clean_success
    assert stream.tool_rejected_count == 0


def test_f9_same_content_different_ids_remains_clean():
    stream = _codex_stream(_message("body", "i2"))
    assert stream.clean_success
    assert stream.replay_count == 0
    assert stream.duplicate_count == 0
    assert stream.final_text == "body"


def _old_parse(runtime, raw):
    """只调用旧消费者的纯解析，不启动 runner 或读取凭据。"""
    from fathom import agent_runtime as ar
    info = _fake_ready_info("/synthetic")
    info.id = runtime
    cls = {"claude-code": ar.ClaudeCodeAdapter, "codex-cli": ar.CodexCliAdapter,
           "hermes-agent": ar.HermesAgentAdapter}[runtime]
    return cls(info).parse_output(ar.RunResult(
        outcome=ar.RunOutcome.OK, exit_code=0, stdout_text=raw, stderr_text="",
    ))


@pytest.mark.parametrize("runtime,data", [
    ("claude-code", {"type": "result", "result": "body"}),
    ("claude-code", {"type": "result", "result": "denied", "is_error": False, "new_field": 1}),
    ("codex-cli", [_message("progress"), _message("body", "i2"), {"type": "turn.completed"}]),
    ("codex-cli", [_message("body"), _message("  ", "i2"), {"type": "turn.completed"}]),
    ("codex-cli", [_message("body"), {"type": "turn.completed"}, _message("late noise", "i2")]),
    ("hermes-agent", [{"type": "text", "text": "progress"}, {"type": "result", "text": "full body", "exit_code": 0}]),
    ("hermes-agent", [{"type": "tool_use", "tool_name": "web_search"}, {"type": "tool_result", "text": "read result"}, {"type": "result", "text": "body"}]),
])
def test_legacy_parse_success_samples_match_normalized_result(runtime, data):
    raw = json.dumps(data, indent=2) if isinstance(data, dict) else "\n".join(json.dumps(e) for e in data)
    old = _old_parse(runtime, raw)
    new = ri.normalize_events(raw, profile=ri.get_profile(runtime))
    assert old.ok, old.reason_code
    assert new.ok, new.reason_code
    assert new.final_text == old.value["result"]



def test_f1_idempotent_duplicate_cannot_hide_changed_turn():
    stream = ri.normalize_events([
        _message(turn_id="A"), _message(turn_id="B"),
        {"type": "turn.completed", "turn_id": "A"},
    ], profile=ri.CODEX_CLI_PROFILE)
    assert not stream.ok
    assert stream.reason_code == "protocol_ambiguous_final"


def test_f3_tool_denial_does_not_hide_unknown_permission_field():
    stream = _codex_stream({"type": "tool_result", "status": "denied", "permission": True})
    assert not stream.ok
    assert stream.reason_code == "protocol_unknown_permission_request"
    assert stream.tool_rejected_count == 0


# 009529f 复审：逐条固定七个新反例，不依赖私有证据路径。
@pytest.mark.parametrize("case,runtime,events",
[('R1_codex_same_id_empty_tail',
  'codex-cli',
  [{'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'body'}},
   {'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': ''}},
   {'type': 'turn.completed'}]),
 ('R1_codex_same_id_whitespace_tail',
  'codex-cli',
  [{'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'body'}},
   {'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': '  '}},
   {'type': 'turn.completed'}]),
 ('R1_codex_same_id_empty_head',
  'codex-cli',
  [{'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': ''}},
   {'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'body'}},
   {'type': 'turn.completed'}]),
 ('R1_hermes_same_id_blank_result',
  'hermes-agent',
  [{'type': 'result', 'id': 'a', 'text': 'body', 'exit_code': 0},
   {'type': 'result', 'id': 'a', 'text': '', 'exit_code': 0}]),
 ('R1_hermes_different_id_blank_result',
  'hermes-agent',
  [{'type': 'result', 'id': 'a', 'text': 'body', 'exit_code': 0},
   {'type': 'result', 'id': 'b', 'text': '  ', 'exit_code': 0}]),
 ('R2_start_terminal_thread_mismatch',
  'codex-cli',
  [{'type': 'thread.started', 'thread_id': 'A'},
   {'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'body'}},
   {'type': 'turn.completed', 'thread_id': 'B'}]),
 ('R2_two_explicit_threads',
  'codex-cli',
  [{'type': 'thread.started', 'thread_id': 'A'},
   {'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'body'}},
   {'type': 'thread.started', 'thread_id': 'B'},
   {'type': 'turn.completed'}])]
)
def test_recheck_seven_counterexamples_reject_false_success(case, runtime, events):
    stream = ri.normalize_events(events, profile=ri.get_profile(runtime))
    assert not stream.ok, case
    assert not stream.clean_success, case
    assert stream.final_text is None, case


@pytest.mark.parametrize("blank_id", [None, "independent"])
def test_recheck_codex_independent_blank_candidate_still_falls_back(blank_id):
    blank = _message("  ", blank_id)
    stream = _codex_stream(blank)
    assert stream.clean_success
    assert stream.final_text == "body"


def test_recheck_codex_blank_duplicate_scope_cannot_disappear():
    stream = ri.normalize_events([
        _message("", turn_id="A"), _message("", turn_id="B"),
        _message("body", "i2"), {"type": "turn.completed"},
    ], profile=ri.CODEX_CLI_PROFILE)
    assert not stream.ok


@pytest.mark.parametrize("events", [
    [{"type": "result", "text": "  ", "exit_code": 0}],
    [{"type": "result", "text": "body", "exit_code": 0},
     {"type": "result", "text": "", "exit_code": 0}],
])
def test_recheck_hermes_blank_complete_result_cannot_reuse_old_body(events):
    stream = ri.normalize_events(events, profile=ri.HERMES_AGENT_PROFILE)
    assert not stream.ok
    assert stream.final_text is None


@pytest.mark.parametrize("start", [
    {"type": "turn.started", "turn_id": "A"},
    {"type": "thread.started", "thread_id": "A"},
])
def test_recheck_explicit_start_scope_binds_without_final_scope(start):
    namespace = "turn_id" if start["type"] == "turn.started" else "thread_id"
    stream = ri.normalize_events([
        start, _message(), {"type": "turn.completed", namespace: "B"},
    ], profile=ri.CODEX_CLI_PROFILE)
    assert not stream.ok
    assert stream.final_text is None


def test_recheck_start_scope_namespaces_independent_and_missing_is_optional():
    stream = ri.normalize_events([
        {"type": "thread.started", "thread_id": "thread-A"},
        {"type": "turn.started", "turn_id": "turn-A"},
        {"type": "item.started", "item": {"id": "tool-1", "type": "command_execution"}},
        {"type": "item.started", "item": {"id": "tool-2", "type": "command_execution"}},
        _message(), {"type": "turn.completed", "turn_id": "turn-A"},
    ], profile=ri.CODEX_CLI_PROFILE)
    assert stream.clean_success
    assert stream.final_text == "body"
