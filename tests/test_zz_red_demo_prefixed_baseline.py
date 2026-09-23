"""ISS-077 · 修复前基线的红灯钉住（先红证据的可执行化）。

对「修复前真实基线」（固定提交 PREFIX_BASE_SHA 的 lib.rs + capabilities）
运行 scripts/verify_tauri_esc_delivery.sh，断言脚本必须红（exit 1）——
证明回归断言真的能抓住 ISS-077 的未修复状态，而不是恒绿摆设。

PREFIX_BASE_SHA 是「ISS-077 修复前基线」的固定提交（lib.rs + capabilities
均为未修复态）。2026-09-23 历史改写（DEC-025：内部文档路径移出+提交者统
一）后，原基线提交 1537ceb（docs(pm) 纯文档提交）被空化丢弃；本测试改指
修复提交 d6b5c6c（#130）的父提交——拓扑等价，树中 lib.rs 同为未修复态。
若仓库改用浅克隆导致 SHA 不可达，本测试失败并明示原因（不静默跳过）。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify_tauri_esc_delivery.sh"
# 修复前基线：ISS-077 修复提交 d6b5c6c（#130）的父提交。修复提交是其子
# 提交，此 SHA 恒指向未修复状态。（2026-09-23 历史改写后自 1537ceb 迁移，
# 旧提交见 .git/filter-repo/commit-map，已随纯文档空化被丢弃。）
PREFIX_BASE_SHA = "12bd8407de01b3dc24275b997fd1ac0c1c53bd72"
BASE_FILES = [
    "apps/desktop/src-tauri/src/lib.rs",
    "apps/desktop/src-tauri/capabilities/default.json",
]


def _git_show(path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{PREFIX_BASE_SHA}:{path}"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"git show {PREFIX_BASE_SHA}:{path} 失败：{result.stderr}"
        "（若为浅克隆缺历史，需完整克隆或调整 PREFIX_BASE_SHA 指向最近的"
        "未修复提交）"
    )
    return result.stdout


class PrefixedBaselineRedDemo(unittest.TestCase):
    def test_prefixed_baseline_is_red(self):
        """修复前基线（未含本次改动）上脚本必须 exit 1（先红钉住）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo-prefix"
            for rel in BASE_FILES:
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_git_show(rel), encoding="utf-8")
            env = dict(os.environ)
            env["FATHOM_ESC_VERIFY_ROOT"] = str(root)
            result = subprocess.run(
                ["bash", str(SCRIPT), "static"],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(
                result.returncode,
                1,
                "修复前基线上回归脚本必须红（exit 1），实际 "
                f"exit {result.returncode}——脚本抓不住未修复状态，不能作为"
                "回归断言。\nstdout:\n"
                f"{result.stdout}\nstderr:\n{result.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
