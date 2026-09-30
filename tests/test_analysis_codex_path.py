"""Codex 多消息事件流经 manager 产品链路落库（ISS-131）。

Codex 会把同一轮的进度说明也发成独立 agent_message，实测 0.147.0 最多
三条且 item 无 phase 字段。这里用合成 CLI 走真实 dispatch → 适配器解析 →
结构验证 → 落库全链路，确认最终 JSON 不被进度消息污染。

放在独立文件是为了不与 tests/test_analysis_manager.py 的其他切片刻意
耦合；helper 沿用同一套隔离夹具约定。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from fathom import agent_runtime as ar
from fathom import analysis_manager as am
from fathom import config, db

FAKE_CODEX_VERSION = "0.147.0"

_INNER_RESULT = json.dumps({
    "schema_version": 1,
    "summary": "A 目录增长 2MB",
    "findings": [{"text": "A 增长", "evidence_ids": ["f-001"],
                  "certainty": "observed"}],
    "limitations": [],
    "inspect_next": [{"evidence_id": "f-001", "reason": "查看 A"}],
}, ensure_ascii=False)


def make_fake_codex(dir_path: Path, *, progress: list[str],
                    final: str | None = _INNER_RESULT,
                    name: str = "fake-codex") -> Path:
    """合成 codex 形态脚本：--version 过能力门；运行时发进度+最终输出。

    ``final=None`` 表示这一轮没有最终输出（只有进度消息）。
    """
    lines = [json.dumps({"type": "thread.started", "thread_id": "synthetic"}),
             json.dumps({"type": "turn.started"})]
    n = 0
    for text in progress:
        n += 1
        lines.append(json.dumps(
            {"type": "item.completed",
             "item": {"id": f"item_{n}", "type": "agent_message",
                      "text": text}},
            ensure_ascii=False))
    if final is not None:
        n += 1
        lines.append(json.dumps(
            {"type": "item.completed",
             "item": {"id": f"item_{n}", "type": "agent_message",
                      "text": final}},
            ensure_ascii=False))
    lines.append(json.dumps(
        {"type": "turn.completed",
         "usage": {"input_tokens": 1, "output_tokens": 1}}))
    body = "\n".join(lines)

    script = dir_path / name
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        f'  echo "codex-cli {FAKE_CODEX_VERSION}"; exit 0; fi\n'
        "cat > /dev/null\n"
        f"cat <<'EOS'\n{body}\nEOS\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def make_snapshots(scanroot: Path) -> tuple[int, int]:
    """同数据集两快照：A 目录 2048→4096 KiB（delta 越过 min_delta_kb）。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
            " du_seconds, total_kb, min_kb, collection_status, vanished_count,"
            " exclude_names, confirmed_missing_count, path_unverified_count)"
            " VALUES ('2026-09-28T08:00:00', ?, 2, 0, 0.1, 2048, 1024,"
            " 'full', 0, '', 0, 0)", (str(scanroot),))
        a = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO entries(snapshot_id, path, size_kb)"
                     " VALUES (?,?,?)", (a, str(scanroot / "A"), 2048))
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
            " du_seconds, total_kb, min_kb, collection_status, vanished_count,"
            " exclude_names, confirmed_missing_count, path_unverified_count)"
            " VALUES ('2026-09-28T09:00:00', ?, 2, 0, 0.1, 4096, 1024,"
            " 'full', 0, '', 0, 0)", (str(scanroot),))
        b = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO entries(snapshot_id, path, size_kb)"
                     " VALUES (?,?,?)", (b, str(scanroot / "A"), 4096))
        conn.commit()
        return a, b
    finally:
        conn.close()


def enable_codex(script: Path) -> None:
    config.update_user_settings({"analysis": {
        "enabled": True,
        "runtime": {"id": "codex-cli", "executable": str(script),
                    "version": FAKE_CODEX_VERSION},
    }})


def wait_terminal(manager: am.AnalysisManager, job_id: str,
                  timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    view = None
    while time.monotonic() < deadline:
        view = manager.job_view(job_id)
        if view["terminal"]:
            return view
        time.sleep(0.05)
    raise AssertionError(f"job 未在 {timeout}s 内到终态：{view}")


@pytest.fixture
def codex_setup(isolated):
    bin_dir = isolated["runtime"] / "bin"
    bin_dir.mkdir()
    script = make_fake_codex(bin_dir, progress=["我会先检查这些变化。"])
    enable_codex(script)
    a, b = make_snapshots(isolated["scanroot"])
    return {**isolated, "script": script, "a": a, "b": b}


def _run_and_read(manager, codex_setup, key):
    preview = manager.create_preview(codex_setup["a"], codex_setup["b"])
    job, replayed = manager.start_job(preview.preview_id,
                                     preview.request_digest, key)
    assert replayed is False
    return preview, wait_terminal(manager, job["job_id"])


class TestCodexProgressDoesNotCorruptResult:
    def test_progress_message_then_json_succeeds(self, codex_setup):
        """核心反例：进度消息 + 合法最终 JSON 必须经产品链路成功落库。"""
        manager = am.AnalysisManager(run_timeout_s=20.0)
        _, final = _run_and_read(manager, codex_setup, "codex-progress")
        assert final["status"] == "succeeded", final
        assert final["analysis_id"] is not None

        record = manager.get_analysis(final["analysis_id"])
        assert record["result"]["summary"] == "A 目录增长 2MB"
        assert record["runtime"]["id"] == "codex-cli"

    def test_three_progress_messages_then_json_succeeds(self, codex_setup):
        """e1d 实测的三条消息形态。"""
        script = make_fake_codex(
            codex_setup["runtime"] / "bin",
            progress=["我会分两步执行。", "第一步完成。", "第二步完成。"],
            name="fake-codex-three")
        config.update_user_settings({"analysis": {
            "enabled": True,
            "runtime": {"id": "codex-cli", "executable": str(script),
                        "version": FAKE_CODEX_VERSION},
        }})
        manager = am.AnalysisManager(run_timeout_s=20.0)
        _, final = _run_and_read(manager, codex_setup, "codex-three")
        assert final["status"] == "succeeded", final
        record = manager.get_analysis(final["analysis_id"])
        assert record["result"]["summary"] == "A 目录增长 2MB"

    def test_final_message_not_json_still_fails(self, codex_setup):
        """最终输出不是合法结构时仍须失败，不得因放宽而假成功。"""
        script = make_fake_codex(
            codex_setup["runtime"] / "bin", progress=["我会先检查这些变化。"],
            final="这不是 JSON。", name="fake-codex-bad")
        config.update_user_settings({"analysis": {
            "enabled": True,
            "runtime": {"id": "codex-cli", "executable": str(script),
                        "version": FAKE_CODEX_VERSION},
        }})
        manager = am.AnalysisManager(run_timeout_s=20.0)
        _, final = _run_and_read(manager, codex_setup, "codex-bad")
        assert final["status"] == "failed", final
        assert final["analysis_id"] is None

    def test_progress_only_never_succeeds(self, codex_setup):
        """只有进度消息、最终输出缺失时不得落库。"""
        script = make_fake_codex(
            codex_setup["runtime"] / "bin", progress=["我会先检查这些变化。"],
            final=None, name="fake-codex-progress-only")
        config.update_user_settings({"analysis": {
            "enabled": True,
            "runtime": {"id": "codex-cli", "executable": str(script),
                        "version": FAKE_CODEX_VERSION},
        }})
        manager = am.AnalysisManager(run_timeout_s=20.0)
        _, final = _run_and_read(manager, codex_setup, "codex-progress-only")
        assert final["status"] == "failed", final
        assert final["analysis_id"] is None
