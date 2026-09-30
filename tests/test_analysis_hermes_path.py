"""Hermes 报告的模型身份经 manager 产品链路落库（ISS-133 验收补腿）。

卡片验收写的是「Claude/Hermes 显式报告模型名时存储与历史展示一致」。
独立审查指出：适配器级已有 Hermes 的 model 用例，但**持久化**那一段当时
只由 Claude 形态的假 CLI 走过，Hermes 是靠「管道共用」推断而非实测。
这里用合成 hermes CLI 走真实 dispatch → 解析 → 结构验证 → 落库全链路。

放在独立文件以免与 tests/test_analysis_manager.py 的其他切片刻意耦合。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from fathom import analysis_manager as am
from fathom import config, db

FAKE_HERMES_VERSION = "0.21.5"

_INNER_RESULT = json.dumps({
    "schema_version": 1,
    "summary": "A 目录增长 2MB",
    "findings": [{"text": "A 增长", "evidence_ids": ["f-001"],
                  "certainty": "observed"}],
    "limitations": [],
    "inspect_next": [{"evidence_id": "f-001", "reason": "查看 A"}],
}, ensure_ascii=False)


def make_fake_hermes(dir_path: Path, *, model: str | None,
                     name: str = "fake-hermes") -> Path:
    """合成 hermes 形态脚本：--version 过能力门；运行时发 stream-json。

    ``model=None`` 表示 init 事件不带 model 字段（``--ignore-user-config``
    下 init model 常为空，见 test_agent_runtime 的既有用例）。
    """
    init = {"type": "system", "subtype": "init", "session_id": "s-1"}
    if model is not None:
        init["model"] = model
    lines = [json.dumps(init, ensure_ascii=False),
             json.dumps({"type": "tool_use", "name": "web_search",
                         "input": {"query": "Fathom"}}, ensure_ascii=False),
             json.dumps({"type": "tool_result", "name": "web_search",
                         "output": "{...}", "is_error": False},
                        ensure_ascii=False),
             json.dumps({"type": "result", "session_id": "s-1",
                         "exit_code": 0, "text": _INNER_RESULT,
                         "tokens": {"input": 10, "output": 5}},
                        ensure_ascii=False)]
    body = "\n".join(lines)

    script = dir_path / name
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        # 版本行须匹配注册表的 version_pattern（Hermes Agent vX.Y.Z+git (date)），
        # 否则能力门会判「引擎不可用」，根本走不到落库那一步。
        f'  echo "Hermes Agent v{FAKE_HERMES_VERSION}+3840.g9a0a162 (2026.9.24)";'
        " exit 0; fi\n"
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


def enable_hermes(script: Path) -> None:
    config.update_user_settings({"analysis": {
        "enabled": True,
        "runtime": {"id": "hermes-agent", "executable": str(script),
                    "version": FAKE_HERMES_VERSION},
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


def _run(manager, a, b, key):
    preview = manager.create_preview(a, b)
    job, replayed = manager.start_job(preview.preview_id,
                                     preview.request_digest, key)
    assert replayed is False
    return wait_terminal(manager, job["job_id"])


class TestHermesModelPersistence:
    def test_reported_model_persisted(self, isolated):
        """Hermes 显式报告模型名 → 落库与读取一致（卡片验收的后半句）。"""
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        enable_hermes(make_fake_hermes(bin_dir, model="glm-5.3"))
        a, b = make_snapshots(isolated["scanroot"])

        manager = am.AnalysisManager(run_timeout_s=20.0)
        final = _run(manager, a, b, "hermes-model")
        assert final["status"] == "succeeded", final

        record = manager.get_analysis(final["analysis_id"])
        assert record["runtime"]["id"] == "hermes-agent"
        assert record["runtime"]["model"] == "glm-5.3", record["runtime"]
        # 历史列表走的是同一个 _analysis_view，展示层同样应一致
        listed = manager.list_analyses(a, b)
        assert any(x["runtime"]["model"] == "glm-5.3" for x in listed), listed

    def test_unreported_model_stays_none(self, isolated):
        """Hermes 未报告 model 时保持 None，不推断。"""
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        enable_hermes(make_fake_hermes(bin_dir, model=None))
        a, b = make_snapshots(isolated["scanroot"])

        manager = am.AnalysisManager(run_timeout_s=20.0)
        final = _run(manager, a, b, "hermes-nomodel")
        assert final["status"] == "succeeded", final
        record = manager.get_analysis(final["analysis_id"])
        assert record["runtime"]["model"] is None, record["runtime"]

    def test_model_absent_from_validated_body(self, isolated):
        """model 只作元数据，正文里不得出现该键。"""
        bin_dir = isolated["runtime"] / "bin"
        bin_dir.mkdir()
        enable_hermes(make_fake_hermes(bin_dir, model="glm-5.3"))
        a, b = make_snapshots(isolated["scanroot"])

        manager = am.AnalysisManager(run_timeout_s=20.0)
        final = _run(manager, a, b, "hermes-body")
        assert final["status"] == "succeeded", final
        record = manager.get_analysis(final["analysis_id"])
        assert "model" not in record["result"], record["result"]
