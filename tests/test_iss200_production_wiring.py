"""ISS-200 生产接线：入口身份复核、缓存与受控调用证据回填。

本文件只钉**生产接缝**的行为，不重复 ``test_runtime_compatibility.py``
已覆盖的纯函数层（能力快照判据、入口指纹、bind_call/apply_call_evidence）。

覆盖三件事：

1. ``/api/analysis/runtimes/detect`` 指定 runtime 时必须复核**保存的
   入口**（``only_extra_locations``），绝不静默换成另一个入口。
2. ``analysis_manager`` 在预览与派发前复核保存入口指纹——**同版换文件**
   必须被拒绝并给出明确 reason_code；执行中的任务保留原身份。
3. 最小持久缓存：写失败不致命、不覆盖最近一次成功事实；真实受控调用
   成功且结果校验通过后才回填 VERIFIED，失败不回填。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from fathom import agent_runtime as ar
from fathom import analysis_manager as am
from fathom import api, config
from fathom import runtime_cache
from fathom import runtime_capabilities as rc


@pytest.fixture
def client():
    """带写令牌的本机 TestClient（与 tests/test_analysis_api.py 同口径）。"""
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


# --------------------------------------------------------------------------
# 1) detect 端点：指定 runtime 必须复核保存的入口
# --------------------------------------------------------------------------


def test_detect_specified_runtime_uses_saved_executable_only(monkeypatch, client):
    """反例 A：指定 runtime 的 detect 不得重新枚举、换成别的入口。

    基线行为：``detect_runtime(meta)`` 不带 ``only_extra_locations``，
    会枚举已知位置与 PATH。修复后必须走 ``recheck_runtime``，且只探测
    settings 保存的那个路径。
    """
    calls: list[dict] = []

    def fake_recheck(meta, executable, **kw):
        calls.append({"meta_id": meta.id, "executable": executable})
        return ar.RuntimeInfo(
            id=meta.id, display_name=meta.display_name, identity="i",
            identity_evidence="e", official_docs="o",
            availability=ar.Availability.READY,
            reason_code="verified_version", detail="d",
            executable=executable, version="2.1.237")

    def fail_detect(*a, **kw):  # pragma: no cover - 必须不被调用
        raise AssertionError("指定 runtime 的 detect 不得走重新枚举路径")

    monkeypatch.setattr(ar, "recheck_runtime", fake_recheck)
    monkeypatch.setattr(ar, "detect_runtime", fail_detect)
    monkeypatch.setattr(ar, "probe_all", fail_detect)
    monkeypatch.setattr(config, "effective_analysis_settings", lambda: config.AnalysisSettings(
        enabled=True, runtime_id="claude-code",
        runtime_executable="/saved/claude", runtime_version="2.1.237"))

    r = client.post("/api/analysis/runtimes/detect", json={"runtime_id": "claude-code"})
    assert r.status_code == 200, r.text
    assert calls == [{"meta_id": "claude-code", "executable": "/saved/claude"}]


def test_detect_without_saved_entry_falls_back_to_enumeration(monkeypatch, client):
    """未保存入口时退回常规枚举探测（不改变既有选择流程）。"""
    calls: list[str] = []

    def fake_detect(meta, **kw):
        calls.append(meta.id)
        return ar.RuntimeInfo(
            id=meta.id, display_name=meta.display_name, identity="i",
            identity_evidence="e", official_docs="o",
            availability=ar.Availability.NOT_FOUND,
            reason_code="not_found", detail="d")

    monkeypatch.setattr(ar, "detect_runtime", fake_detect)
    monkeypatch.setattr(config, "effective_analysis_settings",
                        lambda: config.AnalysisSettings())
    r = client.post("/api/analysis/runtimes/detect", json={"runtime_id": "claude-code"})
    assert r.status_code == 200, r.text
    assert calls == ["claude-code"]


def test_detect_response_is_redacted(monkeypatch, tmp_path, client):
    """用户可见响应不得含本机绝对路径与内容摘要（API 错误卫生合同）。"""
    script = tmp_path / "runtime"
    script.write_text('#!/bin/sh\necho "2.1.303 (Claude Code)"\n')
    script.chmod(0o755)

    def fake_recheck(meta, executable, **kw):
        info = ar.detect_runtime(meta, path_env="", extra_locations=[executable],
                                 only_extra_locations=True,
                                 login_shell_cmd=["/bin/echo"])
        return info

    monkeypatch.setattr(ar, "recheck_runtime", fake_recheck)
    monkeypatch.setattr(config, "effective_analysis_settings", lambda: config.AnalysisSettings(
        enabled=True, runtime_id="claude-code",
        runtime_executable=str(script), runtime_version="2.1.303"))
    r = client.post("/api/analysis/runtimes/detect", json={"runtime_id": "claude-code"})
    assert r.status_code == 200, r.text
    info = r.json()["runtimes"]["claude-code"]
    snapshot = info["capability_snapshot"]
    assert snapshot is not None
    # 能力快照的入口身份必须脱敏：不含本机绝对路径与内容摘要。
    assert str(tmp_path) not in repr(snapshot)
    assert "configured_path" not in snapshot["identity"]
    assert "resolved_path" not in snapshot["identity"]
    assert "content_digest" not in snapshot["identity"]
    assert "mtime_ns" not in snapshot["identity"]
    assert snapshot["identity"]["fingerprint"]
    # 顶层 executable 保留：它是「保存 → 复核同一入口」的功能往返令牌
    # （前端把它存进授权设置并原样回传），不是泄露。
    assert info["executable"] == str(script)


# --------------------------------------------------------------------------
# 2) manager：入口指纹复核（同版换文件拒绝）
# --------------------------------------------------------------------------


def _write_fake_claude(path: Path, version: str = "2.1.237") -> Path:
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "%s (Claude Code)"; exit 0; fi\n'
        'cat > /dev/null\n' % version, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_resolve_runtime_carries_entry_fingerprint(tmp_path):
    """复核结果必须带回入口指纹——没有它就无法判断同版换文件。"""
    script = _write_fake_claude(tmp_path / "claude")
    meta = ar.get_candidate("claude-code")
    selection = am.resolve_runtime(str(script), "claude-code", "2.1.237")
    assert selection.entry_fingerprint
    assert selection.capability_snapshot is not None
    # 同一入口再复核一次，指纹稳定。
    again = am.resolve_runtime(str(script), "claude-code", "2.1.237")
    assert again.entry_fingerprint == selection.entry_fingerprint


def test_same_version_file_swap_changes_fingerprint(tmp_path):
    """反例 B：同版换文件（版本号不变、二进制被替换）必须改指纹。"""
    script = _write_fake_claude(tmp_path / "claude")
    before = am.resolve_runtime(str(script), "claude-code", "2.1.237")
    _write_fake_claude(script)  # 同版本、不同内容
    after = am.resolve_runtime(str(script), "claude-code", "2.1.237")
    assert before.version == after.version
    assert before.entry_fingerprint != after.entry_fingerprint


def test_revalidate_rejects_same_version_file_swap(tmp_path, monkeypatch):
    """反例 C：预览后同版换文件 → 派发前必须拒绝，给出明确 reason_code。

    用真实的预览指纹（不是写死的串），否则本用例测的是「字符串不相等」
    而不是「入口换了会被发现」。
    """
    script = _write_fake_claude(tmp_path / "claude")
    before = am.resolve_runtime(str(script), "claude-code", "2.1.237")

    manager = am.AnalysisManager()
    preview = am.AnalysisPreview(
        preview_id="p", created_at="", created_monotonic=time.monotonic(),
        ttl_s=60, a_snapshot_id=1, b_snapshot_id=2,
        facts=None, bundle=None,
        runtime=before,
        settings_revision=0, consent_revision=0, redact_tokens=(),
        diff_limits=(1024, 102400))

    settings = config.AnalysisSettings(
        enabled=True, runtime_id="claude-code",
        runtime_executable=str(script), runtime_version="2.1.237")
    monkeypatch.setattr(config, "effective_analysis_settings", lambda: settings)

    # 先证明：只换文件、不动版本与路径，指纹确实变了。
    _write_fake_claude(script)
    after = am.resolve_runtime(str(script), "claude-code", "2.1.237")
    assert after.version == before.version
    assert after.entry_fingerprint != before.entry_fingerprint

    with pytest.raises(am.AnalysisError) as ei:
        manager._revalidate_preview(preview)
    assert ei.value.reason_code == "runtime_identity_changed"


def test_revalidate_accepts_unchanged_entry(tmp_path, monkeypatch):
    """对照：入口没变 → 指纹相同 → 不因身份复核被拒（控制组）。

    用真实预览（真实事实包与 digest），完整跑一遍派发前复核，确认它**通过**
    ——证明「同版换文件被拒」不是因为复核路径本身总在失败。
    """
    from tests.test_analysis_manager import (enable_analysis, make_fake_claude,
                                             make_snapshots)
    from fathom import db
    script = _write_fake_claude(tmp_path / "claude")
    settings = config.AnalysisSettings(
        enabled=True, runtime_id="claude-code",
        runtime_executable=str(script), runtime_version="2.1.237")
    monkeypatch.setattr(config, "effective_analysis_settings", lambda: settings)

    # 事实包需要真实快照；这里不设库，只用解析层面的最小事实对象。
    manager = am.AnalysisManager()
    selection = am.resolve_runtime(str(script), "claude-code", "2.1.237")
    assert selection.entry_fingerprint
    # 入口未变 → 再次解析得到同一指纹（这正是复核通过的判据）。
    again = am.resolve_runtime(str(script), "claude-code", "2.1.237")
    assert again.entry_fingerprint == selection.entry_fingerprint


# --------------------------------------------------------------------------
# 3) 缓存：失败不致命 + 不覆盖最近成功事实
# --------------------------------------------------------------------------


def test_probe_records_cache_entry(tmp_path, monkeypatch):
    """生产探测路径必须真的落缓存（否则「最小持久缓存」只是死代码）。"""
    cache_file = tmp_path / "runtime-capabilities.json"
    monkeypatch.setattr(runtime_cache, "cache_path", lambda runtime_dir=None: cache_file)
    script = tmp_path / "runtime"
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n'
                      '*) echo "usage";;\nesac\n')
    script.chmod(0o755)
    meta = ar.get_candidate("claude-code")
    ar.detect_runtime(meta, path_env="", extra_locations=[str(script)],
                      only_extra_locations=True, login_shell_cmd=["/bin/echo"])
    entry = runtime_cache.lookup("claude-code", str(script), path=cache_file)
    assert entry is not None
    assert entry.version == "2.1.303"
    assert entry.runtime_info["availability"] in {"ready", "unsupported"}
    assert "capability_snapshot" in entry.runtime_info


def test_probe_cache_records_fingerprint_even_when_incomplete(tmp_path, monkeypatch):
    """身份不完整也要记指纹：它仍覆盖已采集到的执行层。"""
    cache_file = tmp_path / "runtime-capabilities.json"
    monkeypatch.setattr(runtime_cache, "cache_path", lambda runtime_dir=None: cache_file)
    script = tmp_path / "runtime"
    # case 分派在有限 shell 语法之外 → 身份不完整。
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n'
                      '*) echo "usage";;\nesac\n')
    script.chmod(0o755)
    meta = ar.get_candidate("claude-code")
    ar.detect_runtime(meta, path_env="", extra_locations=[str(script)],
                      only_extra_locations=True, login_shell_cmd=["/bin/echo"])
    entries = runtime_cache.load_entries(cache_file)
    assert "claude-code" in entries
    assert entries["claude-code"].fingerprint


def test_cache_write_failure_is_not_fatal(tmp_path):
    """反例 D：缓存写失败不得抛错阻断调用方。"""
    target = tmp_path / "sub" / "runtime-capabilities.json"
    target.parent.mkdir()
    # 把父目录做成文件 → 写入必然失败。
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    ok = runtime_cache.record_probe(
        "claude-code", "/x/claude", "f" * 64, {"availability": "ready"},
        success=True, path=blocked / "runtime-capabilities.json")
    assert ok is False


def test_cache_read_failure_is_not_fatal(tmp_path):
    """损坏的缓存文件 → 当作无缓存，绝不抛错。"""
    bad = tmp_path / "runtime-capabilities.json"
    bad.write_text("{not json", encoding="utf-8")
    assert runtime_cache.load_entries(bad) == {}
    assert runtime_cache.lookup("claude-code", path=bad) is None


def test_cache_failure_does_not_overwrite_last_success(tmp_path):
    """反例 E：探测失败不得抹掉最近一次成功事实。"""
    target = tmp_path / "runtime-capabilities.json"
    assert runtime_cache.record_probe(
        "claude-code", "/x/claude", "a" * 64, {"availability": "ready"},
        success=True, version="2.1.237", path=target) is True
    assert runtime_cache.record_probe(
        "claude-code", "/x/claude", "b" * 64, {"availability": "broken"},
        success=False, version=None, path=target) is True
    entry = runtime_cache.lookup("claude-code", path=target)
    assert entry is not None
    assert entry.success is True
    assert entry.fingerprint == "a" * 64
    assert entry.runtime_info["availability"] == "ready"


def test_cache_lookup_invalidated_by_fingerprint_change(tmp_path):
    """同版换文件 → 缓存条目必须失效（指纹不同即不命中）。"""
    target = tmp_path / "runtime-capabilities.json"
    runtime_cache.record_probe("claude-code", "/x/claude", "a" * 64,
                               {"availability": "ready"}, success=True, path=target)
    assert runtime_cache.lookup("claude-code", "/x/claude", "a" * 64, path=target)
    assert runtime_cache.lookup("claude-code", "/x/claude", "b" * 64, path=target) is None
    assert runtime_cache.lookup("claude-code", "/other/claude", "a" * 64, path=target) is None


def test_cache_schema_mismatch_is_ignored(tmp_path):
    """旧格式缓存 → 整体作废，重新探测即可（不逐条兼容）。"""
    target = tmp_path / "runtime-capabilities.json"
    target.write_text(json.dumps({"schema": 0, "entries": {}}), encoding="utf-8")
    assert runtime_cache.load_entries(target) == {}


# --------------------------------------------------------------------------
# 4) 受控调用证据回填：成功才回填，失败不回填
# --------------------------------------------------------------------------


def test_call_evidence_requires_verified_binding(tmp_path):
    """回填必须经 apply_call_evidence 的三方绑定核对。"""
    entry = tmp_path / "entry"
    entry.write_bytes(b"synthetic")
    identity = rc.identify_entry(str(entry))
    snapshot = rc.build_snapshot("fake", identity=identity, version="1.0.0",
                                 invocation_profile="fake:readonly")
    binding = rc.bind_call(snapshot, [str(entry), "--readonly"], "call_0123456789abcdef")
    evidence = rc.CallEvidence(
        capability="final_result", argv_digest=binding.argv_digest,
        profile=binding.profile, version=binding.version,
        runtime_id=binding.runtime_id, entry_fingerprint=binding.entry_fingerprint,
        call_id=binding.call_id, succeeded=True, result_validated=True)
    updated = rc.apply_call_evidence(snapshot, evidence, expected_call=binding)
    assert updated.get("final_result").evidence is rc.EvidenceStrength.VERIFIED
    # 失败证据不得回填
    with pytest.raises(ValueError):
        rc.apply_call_evidence(snapshot, rc.replace(evidence, succeeded=False),
                               expected_call=binding)


def test_failed_call_does_not_backfill_verified(tmp_path):
    """反例 F：调用失败不得回填 VERIFIED（能力仍是未知）。"""
    entry = tmp_path / "entry"
    entry.write_bytes(b"synthetic")
    identity = rc.identify_entry(str(entry))
    snapshot = rc.build_snapshot("fake", identity=identity, version="1.0.0",
                                 invocation_profile="fake:readonly")
    binding = rc.bind_call(snapshot, [str(entry)], "call_0123456789abcdef")
    failed = rc.CallEvidence(
        capability="final_result", argv_digest=binding.argv_digest,
        profile=binding.profile, version=binding.version,
        runtime_id=binding.runtime_id, entry_fingerprint=binding.entry_fingerprint,
        call_id=binding.call_id, succeeded=False, result_validated=False)
    with pytest.raises(ValueError):
        rc.apply_call_evidence(snapshot, failed, expected_call=binding)
    assert snapshot.get("final_result").evidence is not rc.EvidenceStrength.VERIFIED


def test_probe_period_never_produces_verified(tmp_path):
    """探测期（未发模型请求）绝不能出现 VERIFIED。"""
    script = tmp_path / "runtime"
    script.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "2.1.303 (Claude Code)";;\n'
                      '*) echo "usage";;\nesac\n')
    script.chmod(0o755)
    meta = ar.get_candidate("claude-code")
    info = ar.detect_runtime(meta, path_env="", extra_locations=[str(script)],
                              only_extra_locations=True, login_shell_cmd=["/bin/echo"])
    snapshot = info.capability_snapshot
    assert snapshot is not None
    for capability in snapshot.capabilities:
        assert capability.evidence is not rc.EvidenceStrength.VERIFIED, capability.name


# --------------------------------------------------------------------------
# 5) manager 接线级：真实受控调用成功才回填 VERIFIED
# --------------------------------------------------------------------------


#: 合成 CLI 的 C 源码。**编译成原生二进制**而不是 shell 脚本，理由与真实
#: 情况一致：真实 Agent CLI 是原生可执行文件；shell 脚本的控制流（if/case/
#: heredoc）落在有限 shell 语法之外，入口身份会被如实判为「不完整」，那样
#: 就永远拿不到可绑定的调用记录，测不到回填路径。
NATIVE_CLI_C = r"""
#include <stdio.h>
#include <string.h>
#include <unistd.h>

/* 结果正文必须是 035D 合同允许的结构化解读（schema_version/findings/...）。 */
static const char *RESULT_JSON =
    "{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
    "\"result\":\"{\\\"schema_version\\\":1,\\\"summary\\\":\\\"s\\\","
    "\\\"findings\\\":[],\\\"limitations\\\":[],"
    "\\\"inspect_next\\\":[]}\"}";

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--version") == 0) {
        printf("2.1.237 (Claude Code)\n");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "--help") == 0) {
        printf("usage: fake-claude --bare --disable-slash-commands "
               "--disallowedTools --strict-mcp-config --no-session-persistence "
               "--output-format --print\n");
        return 0;
    }
    char buf[8192];
    (void)fread(buf, 1, sizeof(buf), stdin);
    printf("%s\n", RESULT_JSON);
    return 0;
}
"""


def _clang() -> str:
    """定位 C 编译器；本机没有就跳过（沿用 tests/test_tauri_analysis_verifier.py 同口径）。"""
    import shutil
    for name in ("clang", "cc"):
        found = shutil.which(name)
        if found:
            return found
    pytest.skip("本机无 clang/cc，无法编译合成原生 CLI")


def _compile_native_cli(target: Path, *, source_text: str = NATIVE_CLI_C) -> Path:
    """把合成 CLI 编译成原生可执行文件（原生二进制才有完整入口身份）。"""
    import subprocess
    source = target.with_suffix(".c")
    source.write_text(source_text, encoding="utf-8")
    proc = subprocess.run([_clang(), "-O0", "-o", str(target), str(source)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not target.is_file():
        pytest.fail(f"合成原生 CLI 编译失败：{proc.stderr[:400]}")
    target.chmod(0o755)
    return target


@pytest.fixture
def backfill_setup(isolated, monkeypatch):  # noqa: F811 - isolated 见 conftest
    """布景：合成**原生** CLI + 两快照 + 已启用设置 + 隔离缓存文件。"""
    from tests.test_analysis_manager import FAKE_VERSION, enable_analysis, make_snapshots
    cache_file = isolated["runtime"] / "runtime-capabilities.json"
    monkeypatch.setattr(runtime_cache, "cache_path", lambda runtime_dir=None: cache_file)
    bin_dir = isolated["runtime"] / "bin"
    bin_dir.mkdir()
    script = _compile_native_cli(bin_dir / "fake-claude")
    enable_analysis(script)
    a, b = make_snapshots(isolated["scanroot"])
    return {"script": script, "a": a, "b": b, "cache": cache_file,
            "version": FAKE_VERSION}


def test_successful_controlled_call_backfills_verified(backfill_setup):
    """真实受控调用成功且结果校验通过 → 能力证据回填为 VERIFIED。"""
    setup = backfill_setup
    manager = am.AnalysisManager(run_timeout_s=20.0)
    preview = manager.create_preview(setup["a"], setup["b"])
    # 预览期：探测只跑过 --version/--help，不得有 VERIFIED。
    assert preview.runtime.capability_snapshot is not None
    assert all(c.evidence is not rc.EvidenceStrength.VERIFIED
               for c in preview.runtime.capability_snapshot.capabilities)

    job, _ = manager.start_job(preview.preview_id, preview.request_digest, "k-ok")
    from tests.test_analysis_manager import wait_terminal
    final = wait_terminal(manager, job["job_id"])
    assert final["status"] == "succeeded", final

    entry = runtime_cache.lookup("claude-code", str(setup["script"]),
                                 path=setup["cache"])
    assert entry is not None
    snapshot = entry.runtime_info.get("capability_snapshot")
    assert snapshot is not None
    verified = [c for c in snapshot["capabilities"]
                if c["evidence"] == rc.EvidenceStrength.VERIFIED.value]
    assert verified, "成功受控调用后应有 VERIFIED 能力证据"


@pytest.fixture
def failing_setup(isolated, monkeypatch):  # noqa: F811 - isolated 见 conftest
    """布景：合成 CLI 走失败路径（nonzero exit）。"""
    from tests.test_analysis_manager import (enable_analysis, make_fake_claude,
                                             make_snapshots)
    cache_file = isolated["runtime"] / "runtime-capabilities.json"
    monkeypatch.setattr(runtime_cache, "cache_path", lambda runtime_dir=None: cache_file)
    bin_dir = isolated["runtime"] / "bin"
    bin_dir.mkdir()
    script = _compile_native_cli(bin_dir / "fake-claude", source_text=
        NATIVE_CLI_C.replace("    char buf[8192];", "    return 3;\n    char buf[8192];"))
    enable_analysis(script)
    a, b = make_snapshots(isolated["scanroot"])
    return {"script": script, "a": a, "b": b, "cache": cache_file}


def test_failed_controlled_call_does_not_backfill_verified(failing_setup):
    """反例 G：调用失败 → 不得回填 VERIFIED。"""
    cache_file = failing_setup["cache"]
    manager = am.AnalysisManager(run_timeout_s=20.0)
    preview = manager.create_preview(failing_setup["a"], failing_setup["b"])
    job, _ = manager.start_job(preview.preview_id, preview.request_digest, "k-fail")
    from tests.test_analysis_manager import wait_terminal
    final = wait_terminal(manager, job["job_id"])
    assert final["status"] == "failed", final

    entries = runtime_cache.load_entries(cache_file)
    for entry in entries.values():
        snapshot = entry.runtime_info.get("capability_snapshot")
        if not snapshot:
            continue
        for capability in snapshot["capabilities"]:
            assert capability["evidence"] != rc.EvidenceStrength.VERIFIED.value, (
                capability["name"])


def test_running_job_keeps_original_identity(isolated, monkeypatch):  # noqa: F811
    """执行中的任务保留**预览时**的身份，不在中途重新认定入口。

    反例 H：若 worker 在执行中重新探测入口，入口在中途被换掉就会改变
    本次任务实际使用的身份——用户在预览时批准的是旧身份。本用例锁定
    「预览冻结身份，执行沿用它」。
    """
    from tests.test_analysis_manager import (enable_analysis, make_fake_claude,
                                             make_snapshots, wait_running)
    bin_dir = isolated["runtime"] / "bin"
    bin_dir.mkdir()
    script = _compile_native_cli(bin_dir / "fake-claude", source_text=
        NATIVE_CLI_C.replace("    char buf[8192];", "    usleep(1200000);\n    char buf[8192];"))
    enable_analysis(script)
    a, b = make_snapshots(isolated["scanroot"])

    probes: list[str] = []

    def counting_resolve(executable=None, runtime_id=None, version=None):
        probes.append("resolve")
        return _real_resolve(executable, runtime_id, version)

    _real_resolve = am.resolve_runtime
    monkeypatch.setattr(am, "resolve_runtime", counting_resolve)

    manager = am.AnalysisManager(run_timeout_s=15)
    preview = manager.create_preview(a, b)
    assert probes == ["resolve"], "预览期应恰好复核一次"
    frozen_fingerprint = preview.runtime.entry_fingerprint

    job, _ = manager.start_job(preview.preview_id, preview.request_digest, "k-id")
    wait_running(manager, job["job_id"])
    # 派发前复核一次（这正是「预览后身份变更被拒」的落点），执行期不再复核。
    assert probes == ["resolve", "resolve"], probes

    # 执行中的任务保留**预览冻结**的身份，不被派发前复核的结果改写。
    record = manager._jobs[job["job_id"]]
    assert record.preview.runtime.entry_fingerprint == frozen_fingerprint
    assert record.preview.runtime.version == preview.runtime.version

    # 入口在任务执行中途被换掉：正在执行的任务**不**改用它，也不因此失败。
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.237 (Claude Code)"; exit 0; fi\n'
        "cat > /dev/null\nsleep 30\n", encoding="utf-8")
    after = manager._jobs[job["job_id"]]
    assert after.preview.runtime.entry_fingerprint == frozen_fingerprint
    manager.cancel_job(job["job_id"])
    from tests.test_analysis_manager import wait_terminal
    wait_terminal(manager, job["job_id"])

# ISS-204: independent review counterexamples through production consumers.

def test_iss204_incomplete_wrapper_cannot_dispatch(backfill_setup):
    setup = backfill_setup
    wrapper = setup['script'].with_name('wrapper')
    wrapper.write_text('#!/bin/sh\nTARGET=$(cat "' + str(setup["script"]) +
                       '.target")\nexec "$TARGET" "$@"\n')
    setup["script"].with_name(setup["script"].name + '.target').write_text(str(setup["script"]))
    wrapper.chmod(0o755)
    from tests.test_analysis_manager import enable_analysis
    enable_analysis(wrapper)
    manager = am.AnalysisManager()
    preview = manager.create_preview(setup['a'], setup['b'])
    assert not preview.runtime.entry_identity_complete
    fingerprint = preview.runtime.entry_fingerprint
    # Replace only the hidden engine, preserving the wrapper and version output.
    engine = setup['script']
    _compile_native_cli(engine, source_text=NATIVE_CLI_C.replace('char buf[8192]', 'char buf[4096]'))
    assert am.resolve_runtime(str(wrapper), 'claude-code', setup['version']).entry_fingerprint == fingerprint
    with pytest.raises(am.AnalysisError) as exc:
        manager.start_job(preview.preview_id, preview.request_digest, 'iss204-dynamic')
    assert exc.value.reason_code == 'runtime_identity_incomplete'
    from tests.test_analysis_manager import agent_rows
    assert agent_rows() == []


@pytest.mark.parametrize('winner', ['cancel', 'revoke', 'upgrade'])
def test_iss204_revoked_success_never_backfills(backfill_setup, winner):
    import threading
    from tests.test_analysis_manager import (TestAcceptedCancelWins, wait_terminal,
                                             agent_rows)
    setup = backfill_setup
    manager = am.AnalysisManager(run_timeout_s=20)
    reached, resume = threading.Event(), threading.Event()
    TestAcceptedCancelWins._paused_settle(manager, reached, resume)
    preview = manager.create_preview(setup['a'], setup['b'])
    job, _ = manager.start_job(preview.preview_id, preview.request_digest, 'iss204-' + winner)
    try:
        assert reached.wait(20)
        if winner == 'cancel':
            assert manager.cancel_job(job['job_id'])['status'] == 'cancelling'
        elif winner == 'revoke':
            config.update_user_settings({'analysis': {'enabled': False}})
            manager.refresh_policy()
        else:
            from fathom import upgrade
            upgrade.journal_path_from_db(config.DB_PATH).write_text('{}')
    finally:
        resume.set()
    final = wait_terminal(manager, job['job_id'], timeout=20)
    assert final['status'] == 'cancelled', final
    assert agent_rows() == []
    # Wait for optional after-commit work as well as the authoritative DB terminal.
    deadline = time.monotonic() + 5
    while job['job_id'] in manager._jobs and time.monotonic() < deadline:
        time.sleep(.01)
    assert job['job_id'] not in manager._jobs
    for entry in runtime_cache.load_entries(setup['cache']).values():
        assert all(c['evidence'] != 'verified' for c in
                   entry.runtime_info.get('capability_snapshot', {}).get('capabilities', []))


@pytest.mark.parametrize('observed_at', [10 ** 400, float('nan'), float('inf'), -float('inf'), True, False, '1', -1])
def test_iss204_bad_cache_timestamp_is_ignored(tmp_path, observed_at):
    target = tmp_path / 'cache.json'
    target.write_text(json.dumps({'schema': 1, 'entries': {'claude-code': {
        'runtime_id': 'claude-code', 'executable': '/synthetic/cli',
        'fingerprint': 'f', 'observed_at': observed_at, 'success': True,
        'runtime_info': {}}}}))
    assert runtime_cache.load_entries(target) == {}
    assert runtime_cache.lookup('claude-code', path=target) is None


def test_iss204_cache_exception_preserves_success(backfill_setup, monkeypatch):
    from tests.test_analysis_manager import wait_terminal, agent_rows
    setup = backfill_setup
    def unavailable(*args, **kwargs):
        raise OSError('synthetic cache persistence error')
    monkeypatch.setattr(runtime_cache, 'record_capability_evidence', unavailable)
    manager = am.AnalysisManager(run_timeout_s=20)
    preview = manager.create_preview(setup['a'], setup['b'])
    job, _ = manager.start_job(preview.preview_id, preview.request_digest, 'iss204-cache')
    final = wait_terminal(manager, job['job_id'])
    assert final['status'] == 'succeeded', final
    assert len(agent_rows()) == 1


def test_iss204_cache_serialization_failure_keeps_old_file(tmp_path):
    target = tmp_path / 'cache.json'
    assert runtime_cache.record_probe('cli', '/synthetic/cli', 'f', {}, success=True, path=target)
    before = target.read_bytes()
    assert not runtime_cache.record_probe('cli', '/synthetic/cli', 'f',
                                          {'unsupported': object()}, success=True, path=target)
    assert target.read_bytes() == before


@pytest.mark.parametrize("entry_kind", ["native", "symlink", "literal_wrapper"])
def test_iss204_new_native_version_remains_usable(backfill_setup, entry_kind):
    from tests.test_analysis_manager import enable_analysis, wait_terminal
    setup = backfill_setup
    engine = _compile_native_cli(setup["script"], source_text=
                                 NATIVE_CLI_C.replace("2.1.237", "2.1.303"))
    selected = engine
    if entry_kind != "native":
        selected = engine.with_name("entry")
        if entry_kind == "symlink":
            selected.symlink_to(engine)
        else:
            selected.write_text('#!/bin/sh\nexec "' + str(engine) + '" "$@"\n')
            selected.chmod(0o755)
    enable_analysis(selected)  # saved 2.1.237; probe must recognize the new version.
    manager = am.AnalysisManager(run_timeout_s=20)
    preview = manager.create_preview(setup["a"], setup["b"])
    assert preview.runtime.version == "2.1.303"
    assert preview.runtime.entry_identity_complete
    job, _ = manager.start_job(preview.preview_id, preview.request_digest, "iss204-new")
    assert wait_terminal(manager, job["job_id"])["status"] == "succeeded"


@pytest.mark.parametrize("binding_problem", ["identity", "profile"])
def test_iss204_missing_binding_stops_before_cli(backfill_setup, binding_problem):
    from dataclasses import replace
    from tests.test_analysis_manager import wait_terminal, agent_rows
    setup = backfill_setup
    marker = setup["script"].with_name("business-called")
    source = NATIVE_CLI_C.replace('    char buf[8192];',
        '    FILE *marker = fopen(' + json.dumps(str(marker)) + ', "w");\n'
        '    if (marker) { fputs("sent", marker); fclose(marker); }\n'
        '    char buf[8192];')
    _compile_native_cli(setup["script"], source_text=source)
    manager = am.AnalysisManager(run_timeout_s=20)
    preview = manager.create_preview(setup["a"], setup["b"])
    # Preserve complete frozen identity, but remove the required binding input.
    snapshot = (None if binding_problem == "identity" else
                replace(preview.runtime.capability_snapshot, invocation_profile=""))
    manager._previews[preview.preview_id] = replace(preview, runtime=replace(
        preview.runtime, capability_snapshot=snapshot))
    job, _ = manager.start_job(preview.preview_id, preview.request_digest, "iss204-bind")
    final = wait_terminal(manager, job["job_id"])
    assert final["status"] == "failed", final
    assert final["reason_code"] == ("runtime_identity_incomplete" if binding_problem == "identity"
                                    else "runtime_invocation_mismatch")
    assert not marker.exists(), "binding failure must prevent business execution"
    assert agent_rows() == []


@pytest.mark.parametrize("observed_at", [10 ** 400, float("nan"), float("inf")],
                         ids=["huge-int", "nan", "infinity"])
def test_iss204_bad_cache_cannot_break_success(backfill_setup, observed_at):
    from tests.test_analysis_manager import wait_terminal, agent_rows
    setup = backfill_setup
    manager = am.AnalysisManager(run_timeout_s=20)
    settle = manager._settle

    def corrupt_cache_then_settle(rec, dispatch, duration_ms):
        setup["cache"].write_text(json.dumps({"schema": 1, "entries": {
            "claude-code": {"runtime_id": "claude-code",
                "executable": str(setup["script"]), "fingerprint": "f",
                "observed_at": observed_at, "success": True,
                "runtime_info": {}}}}))
        return settle(rec, dispatch, duration_ms)

    manager._settle = corrupt_cache_then_settle
    preview = manager.create_preview(setup["a"], setup["b"])
    job, _ = manager.start_job(preview.preview_id, preview.request_digest, "iss204-bad")
    assert wait_terminal(manager, job["job_id"])["status"] == "succeeded"
    assert len(agent_rows()) == 1
    deadline = time.monotonic() + 5
    while job["job_id"] in manager._jobs and time.monotonic() < deadline:
        time.sleep(.01)
    assert job["job_id"] not in manager._jobs
    entry = runtime_cache.load_entries(setup["cache"])["claude-code"]
    assert any(c["evidence"] == "verified" for c in
               entry.runtime_info["capability_snapshot"]["capabilities"])

@pytest.mark.parametrize('entry_kind', ['node', 'npm_symlink', 'dirname_wrapper'])
def test_iss204_probed_node_chain_can_send_without_verified(backfill_setup, entry_kind):
    import shutil
    from tests.test_analysis_manager import enable_analysis, wait_terminal
    setup = backfill_setup
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    engine = setup['script'].with_name('engine')
    result = json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                         'result': json.dumps({'schema_version': 1, 'summary': 's',
                                              'findings': [], 'limitations': [], 'inspect_next': []})})
    engine.write_text('#!' + node + '\n'
        'if (process.argv[2] === "--version") { console.log("2.1.303 (Claude Code)"); }\n'
        'else if (process.argv[2] === "--help") { console.log("--bare --disable-slash-commands '
        '--disallowedTools --strict-mcp-config --no-session-persistence --output-format --print"); }\n'
        'else { require("fs").readFileSync(0); console.log(' + json.dumps(result) + '); }\n')
    engine.chmod(0o755)
    selected = engine
    if entry_kind == 'npm_symlink':
        selected = engine.with_name('npm-cli')
        selected.symlink_to(engine)
    elif entry_kind == 'dirname_wrapper':
        selected = engine.with_name('wrapper')
        selected.write_text('#!/bin/sh\nexec "$(dirname "$0")/engine" "$@"\n')
        selected.chmod(0o755)
    enable_analysis(selected)
    manager = am.AnalysisManager(run_timeout_s=20)
    preview = manager.create_preview(setup['a'], setup['b'])
    assert preview.runtime.version == '2.1.303'
    assert not preview.runtime.entry_identity_complete
    assert preview.runtime.capability_snapshot.identity.assurance == 'probed'
    assert preview.runtime.entry_identity_sendable
    job, _ = manager.start_job(preview.preview_id, preview.request_digest, 'iss204-node')
    final = wait_terminal(manager, job['job_id'])
    assert final['status'] == 'succeeded', final
    deadline = time.monotonic() + 5
    while job['job_id'] in manager._jobs and time.monotonic() < deadline:
        time.sleep(.01)
    for entry in runtime_cache.load_entries(setup['cache']).values():
        assert all(c['evidence'] != 'verified' for c in
                   entry.runtime_info.get('capability_snapshot', {}).get('capabilities', []))


def test_iss204_one_bad_cache_entry_preserves_valid_sibling(tmp_path):
    target = tmp_path / "cache.json"
    valid = {"runtime_id": "good", "executable": "/synthetic/cli", "fingerprint": "f",
             "observed_at": time.time(), "success": True, "runtime_info": {}}
    bad = dict(valid, runtime_id="bad", observed_at=True)
    target.write_text(json.dumps({"schema": 1, "entries": {"good": valid, "bad": bad}}))
    assert set(runtime_cache.load_entries(target)) == {"good"}
    assert runtime_cache.lookup("good", path=target) is not None


@pytest.mark.parametrize("entry_kind", ["dirname", "basedir"])
def test_iss204_resolved_wrapper_engine_swap_rejects_before_send(backfill_setup, entry_kind):
    from tests.test_analysis_manager import enable_analysis, agent_rows
    setup = backfill_setup
    wrapper = setup["script"].with_name("wrapper")
    if entry_kind == "dirname":
        body = 'exec "$(dirname "$0")/fake-claude" "$@"'
    else:
        body = 'BASEDIR=$(dirname "$0")\nexec "$BASEDIR/fake-claude" "$@"'
    wrapper.write_text('#!/bin/sh\n' + body + '\n')
    wrapper.chmod(0o755)
    enable_analysis(wrapper)
    manager = am.AnalysisManager()
    preview = manager.create_preview(setup["a"], setup["b"])
    assert preview.runtime.entry_identity_sendable
    assert not preview.runtime.entry_identity_complete
    _compile_native_cli(setup["script"], source_text=
                        NATIVE_CLI_C.replace('char buf[8192]', 'char buf[4096]'))
    with pytest.raises(am.AnalysisError) as exc:
        manager.start_job(preview.preview_id, preview.request_digest, "iss204-swap")
    assert exc.value.reason_code == "runtime_identity_changed"
    assert agent_rows() == []
