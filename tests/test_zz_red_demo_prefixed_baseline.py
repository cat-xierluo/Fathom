"""ISS-077 · 修复前基线的红灯钉住（先红证据的可执行化）。

对「修复前真实基线」（固定提交 PREFIX_BASE_SHA 的 lib.rs + capabilities）
运行 scripts/verify_tauri_esc_delivery.sh，断言脚本必须红（exit 1）——
证明回归断言真的能抓住 ISS-077 的未修复状态，而不是恒绿摆设。

PREFIX_BASE_SHA 是本修复落地前的分支基线（1537ceb，提交信息含 ISS-077
派发登记），在完整克隆历史中恒可达；若仓库改用浅克隆导致 SHA 不可达，
本测试失败并明示原因（不静默跳过）。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify_tauri_esc_delivery.sh"
# 修复前基线：分支 iss-077-esc-keydown 派发时的 HEAD（1537ceb，含 ISS-077
# 派发登记的 docs(pm) 提交）。修复提交是其子提交，此 SHA 恒指向未修复状态。
PREFIX_BASE_SHA = "1537ceb8bfca20c6b7f9f60545f4c8b13de879eb"
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
