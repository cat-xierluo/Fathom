"""ISS-201：现有三家 adapter / dispatch / manager 的真实消费者回归。

只用合成 CLI 和隔离 SQLite；版本检查不计入业务发送，真实业务进程
读取 stdin 后追加一次 sends。取消/撤权在真实 dispatch 返回后、提交前
设屏障，不依赖 sleep 猜竞争窗口。
"""
from __future__ import annotations

import json
import logging
import shlex
import threading

import pytest

from fathom import agent_runtime as ar, analysis_manager as am
from fathom import config, runtime_invocation as ri
from test_analysis_manager import (
    _INNER_RESULT, agent_rows, make_snapshots, run_row, wait_terminal,
)

RUNTIMES = ("claude-code", "codex-cli", "hermes-agent")
VERSIONS = {"claude-code": "2.1.237", "codex-cli": "0.147.0", "hermes-agent": "0.21.5"}
BANNERS = {"claude-code": "2.1.237 (Claude Code)",
           "codex-cli": "codex-cli 0.147.0",
           "hermes-agent": "Hermes Agent v0.21.5 (2026.09.30)"}


def info(rid, executable="/absolute/synthetic/engine"):
    meta = ar.get_candidate(rid)
    return ar.RuntimeInfo(
        id=rid, display_name=meta.display_name, identity="synthetic",
        identity_evidence="synthetic", official_docs="",
        availability=ar.Availability.READY, reason_code="synthetic", detail="",
        executable=str(executable), version=VERSIONS[rid],
    )


def events(rid, body=_INNER_RESULT):
    if rid == "claude-code":
        return [{"type": "result", "is_error": False, "result": body,
                 "future_normal_field": {"revision": 2}}]
    if rid == "codex-cli":
        return [{"type": "thread.started", "thread_id": "A"},
                {"type": "item.completed", "item": {"id": "f1", "type": "agent_message", "text": body}},
                {"type": "turn.completed", "future_normal_field": 2}]
    return [{"type": "system", "session_id": "A"},
            {"type": "text", "text": "进度，不是正文"},
            {"type": "result", "exit_code": 0, "text": body,
             "session_id": "A", "future_normal_field": 2}]


def encode(rid, evs):
    if rid == "claude-code":
        assert len(evs) == 1
        return json.dumps(evs[0], ensure_ascii=False, indent=2)
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in evs)


class CountingRunner:
    def __init__(self, rid, evs=None, outcome=ar.RunOutcome.OK):
        self.rid, self.evs, self.outcome, self.calls = rid, evs, outcome, 0

    def run(self, invocation, **kw):
        self.calls += 1
        assert invocation.stdin_bytes == "合成载荷".encode()
        assert set(invocation.env) <= {"PATH", "HOME"}
        return ar.RunResult(self.outcome, 0 if self.outcome is ar.RunOutcome.OK else None,
                            encode(self.rid, self.evs or events(self.rid)), "")


@pytest.mark.parametrize("rid", RUNTIMES)
def test_dispatch_consumes_unknown_fields_and_missing_optional_metadata(rid, tmp_path):
    runner = CountingRunner(rid)
    result = ar.dispatch_request(ar.get_adapter(info(rid)), runner, "合成载荷", cwd=tmp_path)
    assert result.ok and runner.calls == 1
    value = result.parse.value
    assert value["result"] == _INNER_RESULT
    assert value["model"] is None and value["usage"] is None
    assert value["normalization"]["unknown_field_count"] >= 1
    assert value["normalization"]["clean_success"] is True
    assert value["auth"]["state"] == "unknown"
    assert value["auth"]["method"] == {"claude-code": "bare", "codex-cli": "oauth", "hermes-agent": "api_key"}[rid]


FLAG_CASES = [(rid, f.name) for rid in RUNTIMES for f in ri.get_profile(rid).flags if f.required]


@pytest.mark.parametrize("rid,flag_name", FLAG_CASES)
def test_dispatch_missing_required_or_protocol_flag_never_calls_runner(rid, flag_name, tmp_path, monkeypatch):
    adapter = ar.get_adapter(info(rid))
    original = adapter.build_invocation
    tokens = ri.get_profile(rid).flag(flag_name).tokens

    def omit(*args, **kw):
        invocation = original(*args, **kw)
        offset = next(i for i in range(len(invocation.argv))
                      if tuple(invocation.argv[i:i + len(tokens)]) == tokens)
        del invocation.argv[offset:offset + len(tokens)]
        return invocation

    monkeypatch.setattr(adapter, "build_invocation", omit)
    runner = CountingRunner(rid)
    result = ar.dispatch_request(adapter, runner, "合成载荷", cwd=tmp_path)
    assert not result.ok and runner.calls == 0
    flag = ri.get_profile(rid).flag(flag_name)
    assert result.reason_code == ("profile_protocol_mismatch" if flag.kind is ri.FlagKind.PROTOCOL
                                  else "profile_required_flags_missing")
    assert result.parse.failure.dispatch_uncertain is False
    assert result.parse.failure.auto_retry_safe is False


@pytest.mark.parametrize("rid", ("codex-cli", "hermes-agent"))
def test_optional_flag_missing_only_degrades(rid, tmp_path, monkeypatch):
    adapter = ar.get_adapter(info(rid))
    original = adapter.build_invocation
    flag = ri.get_profile(rid).optional_flags()[0]

    def omit(*args, **kw):
        invocation = original(*args, **kw)
        i = invocation.argv.index(flag.tokens[0])
        del invocation.argv[i:i + len(flag.tokens)]
        return invocation

    monkeypatch.setattr(adapter, "build_invocation", omit)
    runner = CountingRunner(rid)
    result = ar.dispatch_request(adapter, runner, "合成载荷", cwd=tmp_path)
    assert result.ok and runner.calls == 1
    assert flag.name in result.parse.value["profile"]["degraded_optional"]


@pytest.mark.parametrize("rid", RUNTIMES)
def test_timeout_classification_is_consumed_and_never_retried(rid, tmp_path):
    runner = CountingRunner(rid, outcome=ar.RunOutcome.TIMED_OUT)
    result = ar.dispatch_request(ar.get_adapter(info(rid)), runner, "合成载荷", cwd=tmp_path)
    assert not result.ok and runner.calls == 1
    failure = result.parse.failure
    assert failure.failure_class is ri.FailureClass.TIMEOUT
    assert failure.dispatch_uncertain and not failure.auto_retry_safe
    assert not failure.engine_switch_allowed


def fake_cli(base, rid, raw, *, sleep=False):
    script, sends = base / "synthetic-cli", base / "sends"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo ' + shlex.quote(BANNERS[rid]) + "; exit 0; fi\n"
        "cat > /dev/null\n"
        + "echo sent >> " + shlex.quote(str(sends)) + "\n"
        + ("sleep 30\n" if sleep else "")
        + "printf '%s\\n' " + shlex.quote(raw) + "\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, sends


def start_manager(isolated, rid, raw, *, timeout=10, sleep=False):
    script, sends = fake_cli(isolated["runtime"], rid, raw, sleep=sleep)
    config.update_user_settings({"analysis": {"enabled": True, "runtime": {
        "id": rid, "executable": str(script), "version": VERSIONS[rid],
    }}})
    a, b = make_snapshots(isolated["scanroot"])
    manager = am.AnalysisManager(run_timeout_s=timeout,
        env_factory=lambda: ar.build_minimal_env(home=str(isolated["home"])))
    preview = manager.create_preview(a, b)
    job, replayed = manager.start_job(preview.preview_id, preview.request_digest, "consumer")
    assert not replayed
    return manager, job, sends


@pytest.mark.parametrize("rid", RUNTIMES)
def test_real_cli_manager_saves_optional_metadata_absent(rid, isolated):
    manager, job, sends = start_manager(isolated, rid, encode(rid, events(rid)))
    try:
        view = wait_terminal(manager, job["job_id"])
        assert view["status"] == "succeeded", view
        assert len(agent_rows()) == 1
        assert json.loads(agent_rows()[0]["result_json"]) == json.loads(_INNER_RESULT)
        assert agent_rows()[0]["model"] is None
        assert sends.read_text().splitlines() == ["sent"]
    finally:
        manager.shutdown()


INVALIDS = [
    ("claude-code", "bad_bool"), ("claude-code", "tool_stream"),
    ("codex-cli", "cross_thread"), ("codex-cli", "cross_turn"),
    ("codex-cli", "failed_then_success"), ("codex-cli", "missing_terminal"),
    ("codex-cli", "fragment"), ("codex-cli", "duplicate_conflict"),
    ("codex-cli", "permission_request"), ("codex-cli", "tool_rejected"),
    ("hermes-agent", "boolean_exit"), ("hermes-agent", "string_exit"),
    ("hermes-agent", "duplicate_result"), ("hermes-agent", "permission_request"),
    ("hermes-agent", "tool_rejected"),
] + [(rid, bad) for rid in RUNTIMES for bad in ("bad_business_json", "schema_extra", "bad_reference")]


def invalid_raw(rid, case):
    evs = events(rid)
    if case == "bad_bool":
        evs[0]["is_error"] = "false"
    elif case == "tool_stream":
        return json.dumps({"type": "tool_use", "name": "Read"}) + "\n" + encode(rid, evs)
    elif case == "cross_thread":
        evs[1]["thread_id"] = "B"
    elif case == "cross_turn":
        evs.insert(1, {"type": "turn.started", "turn_id": "turn-A"})
        evs[-1]["turn_id"] = "turn-B"
    elif case == "failed_then_success":
        evs.insert(-1, {"type": "turn.failed", "error": {"message": "failure"}})
    elif case == "missing_terminal":
        evs.pop()
    elif case == "fragment":
        evs[1]["item"]["fragment"] = 0
    elif case == "duplicate_conflict":
        evs.insert(-1, {"type": "item.completed", "item": {
            "id": "f1", "type": "agent_message", "text": "different"}})
    elif case == "permission_request":
        evs.insert(-1, {"type": "permission.request", "status": "denied"})
    elif case == "tool_rejected":
        evs.insert(-1, {"type": "tool_result", "status": "denied"})
    elif case == "boolean_exit":
        evs[-1]["exit_code"] = False
    elif case == "string_exit":
        evs[-1]["exit_code"] = "0"
    elif case == "duplicate_result":
        evs.append(dict(evs[-1]))
    else:
        body = json.loads(_INNER_RESULT)
        if case == "schema_extra":
            body["future_unsafe_business_field"] = 1
        elif case == "bad_reference":
            body["findings"][0]["evidence_ids"] = ["f-999"]
        evs = events(rid, "{" if case == "bad_business_json" else json.dumps(body))
    return encode(rid, evs)


@pytest.mark.parametrize("rid,case", INVALIDS)
def test_real_cli_manager_invalid_output_never_saves_success(rid, case, isolated):
    manager, job, sends = start_manager(isolated, rid, invalid_raw(rid, case))
    try:
        view = wait_terminal(manager, job["job_id"])
        assert view["status"] == "failed", view
        assert view["reason_code"] != "internal_error", view
        if case == "tool_rejected":
            assert view["reason_code"] == "protocol_tool_rejected"
        if case in {"bad_business_json", "schema_extra", "bad_reference"}:
            assert view["reason_code"].startswith("result_")
        assert agent_rows() == [] and view["analysis_id"] is None
        assert run_row(job["job_id"])["status"] == "failed"
        assert sends.read_text().splitlines() == ["sent"]
    finally:
        manager.shutdown()


@pytest.mark.parametrize("variant", ("warning", "duplicate"))
def test_codex_benign_diagnostics_still_save_success(variant, isolated):
    evs = events("codex-cli")
    if variant == "warning":
        evs.insert(1, {"type": "item.completed", "item": {
            "type": "error", "message": "Falling back from WebSockets to HTTPS transport."}})
    else:
        evs.insert(-1, dict(evs[1]))
    parsed = ar.get_adapter(info("codex-cli")).parse_output(
        ar.RunResult(ar.RunOutcome.OK, 0, encode("codex-cli", evs), ""))
    assert parsed.ok and not parsed.value["normalization"]["clean_success"]
    manager, job, sends = start_manager(isolated, "codex-cli", encode("codex-cli", evs))
    try:
        assert wait_terminal(manager, job["job_id"])["status"] == "succeeded"
        assert len(agent_rows()) == 1 and sends.read_text().splitlines() == ["sent"]
    finally:
        manager.shutdown()


@pytest.mark.parametrize("rid", RUNTIMES)
def test_real_cli_timeout_logs_uncertainty_and_sends_once(rid, isolated, caplog):
    caplog.set_level(logging.INFO, logger="fathom.analysis_manager")
    manager, job, sends = start_manager(isolated, rid, encode(rid, events(rid)), timeout=0.6, sleep=True)
    try:
        view = wait_terminal(manager, job["job_id"])
        assert view["status"] == "timed_out"
        assert view["reason_code"] == "runner_timed_out"
        assert agent_rows() == []
        assert sends.read_text().splitlines() == ["sent"]
        assert "class=timeout dispatch_uncertain=True auto_retry_safe=False engine_switch_allowed=False" in caplog.text
    finally:
        manager.shutdown()


@pytest.mark.parametrize("rid", RUNTIMES)
@pytest.mark.parametrize("action", ("cancel", "revoke"))
def test_real_cli_late_success_cannot_revive_cancelled_or_revoked(rid, action, isolated, monkeypatch):
    returned, release = threading.Event(), threading.Event()
    original = ar.dispatch_request

    def pause(*args, **kw):
        result = original(*args, **kw)
        assert result.ok
        returned.set()
        assert release.wait(10), "测试未释放真实 dispatch 后的提交屏障"
        return result

    monkeypatch.setattr(ar, "dispatch_request", pause)
    manager, job, sends = start_manager(isolated, rid, encode(rid, events(rid)))
    try:
        assert returned.wait(10)
        if action == "cancel":
            assert manager.cancel_job(job["job_id"])["status"] == "cancelling"
        else:
            config.update_user_settings({"analysis": {"enabled": False}})
            manager.refresh_policy()
        release.set()
        view = wait_terminal(manager, job["job_id"])
        assert view["status"] == "cancelled" and agent_rows() == []
        assert sends.read_text().splitlines() == ["sent"]
        assert view["analysis_id"] is None
        assert run_row(job["job_id"])["status"] == "cancelled"
    finally:
        release.set()
        manager.shutdown()
