"""ISS-077 · Tauri 壳裸 Escape 送达修复的 fail-closed 回归断言。

反例（修复前现状，ISS-028 三尺寸实机证据「结构层发现」①）：
macOS Tauri/WKWebView 壳内按 Esc 不产生页面 keydown（Tab/Enter/方向键均送达），
同页 Web/Playwright 下 Esc 可用——Escape 被壳层 AppKit 键等价路径吞掉，
前端 document 级 Esc 监听（changes.js）收不到事件。

修复（壳层转发，前端零改动）：默认菜单 Window 子菜单挂无修饰 Esc 加速键
菜单项（esc-forward），主菜单在键等价阶段认领裸 Esc（先于 keyDown 被吞点），
on_menu_event 路由到 forward_escape_to_page，经 eval 向页面投递合成
KeyboardEvent keydown(Escape)，复用前端既有监听。

本测试钉住（与 scripts/verify_tauri_esc_delivery.sh 同一合同，双入口）：
1. 真实仓库：脚本 static 模式退出 0（修复合同齐全、capability 无放宽）；
2. 临时副本反例漂移（真实文件不被修改）——删除挂载调用、删路由、改载荷
   keyCode、放宽 capability，脚本都必须退出 1（fail-closed 反例侧）；
3. 结构性失败（文件缺失）退出 3。

写成 unittest.TestCase（而非 pytest 函数）：python3 -m unittest discover
可发现执行，pytest 同样兼容（CI 走 ci_pytest.sh）。cargo 编译门在脚本
full 模式（PM/CI 执行）；实机按 Esc 行为属 NOT_VERIFIED-需前台（PM 亲自
验证，用户 2026-09-20 规则）。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify_tauri_esc_delivery.sh"

# 脚本读取的最小文件集（与脚本内路径一致）。
CHECKED_FILES = [
    "apps/desktop/src-tauri/src/lib.rs",
    "apps/desktop/src-tauri/capabilities/default.json",
]


def _make_copy(base: Path) -> Path:
    root = base / "repo"
    for rel in CHECKED_FILES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            (REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8"
        )
    return root


def _run_script(root: Path, mode: str = "static") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["FATHOM_ESC_VERIFY_ROOT"] = str(root)
    return subprocess.run(
        ["bash", str(SCRIPT), mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class EscDeliveryContract(unittest.TestCase):
    def test_current_repo_passes_static_contract(self):
        """真实仓库：修复合同齐全 + capability 基线一致 → 退出 0（绿侧）。"""
        result = _run_script(REPO_ROOT)
        self.assertEqual(
            result.returncode,
            0,
            f"真实仓库 Esc 送达合同应全过，实际退出 {result.returncode}：\n"
            f"{result.stdout}\n{result.stderr}",
        )

    def test_missing_attach_call_fails_closed(self):
        """反例：setup 不调用挂载函数（修复未接线）→ 退出 1。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_copy(Path(tmp))
            lib = root / CHECKED_FILES[0]
            lib.write_text(
                lib.read_text(encoding="utf-8").replace(
                    "attach_escape_forward_menu_item(handle);", ""
                ),
                encoding="utf-8",
            )
            result = _run_script(root)
            self.assertEqual(
                result.returncode,
                1,
                f"挂载调用被删后脚本必须红（fail-closed），实际退出 "
                f"{result.returncode}",
            )

    def test_missing_menu_routing_fails_closed(self):
        """反例：路由判等被短路（不再按 esc-forward 分发）→ 退出 1。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_copy(Path(tmp))
            lib = root / CHECKED_FILES[0]
            lib.write_text(
                lib.read_text(encoding="utf-8").replace(
                    "if event.id.as_ref() == ESC_FORWARD_MENU_ID {", "if false {"
                ),
                encoding="utf-8",
            )
            result = _run_script(root)
            self.assertEqual(
                result.returncode,
                1,
                f"路由判等被短路后脚本必须红，实际退出 {result.returncode}",
            )

    def test_payload_keycode_drift_fails_closed(self):
        """反例：载荷 keyCode 27 被改 → 退出 1（合成事件不再是 Escape 合同）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_copy(Path(tmp))
            lib = root / CHECKED_FILES[0]
            lib.write_text(
                lib.read_text(encoding="utf-8").replace("keyCode: 27", "keyCode: 13"),
                encoding="utf-8",
            )
            result = _run_script(root)
            self.assertEqual(
                result.returncode,
                1,
                f"载荷 keyCode 漂移后脚本必须红，实际退出 {result.returncode}",
            )

    def test_capability_drift_fails_closed(self):
        """反例：capability 新增宽泛事件授权 → 退出 1（修复不得放开授权）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_copy(Path(tmp))
            cap = root / CHECKED_FILES[1]
            cap.write_text(
                cap.read_text(encoding="utf-8").replace(
                    '"core:event:default",',
                    '"core:event:default",\n    "core:event:allow-listen",',
                ),
                encoding="utf-8",
            )
            result = _run_script(root)
            self.assertEqual(
                result.returncode,
                1,
                f"capability 放宽后脚本必须红，实际退出 {result.returncode}",
            )

    def test_missing_files_exit_blocked(self):
        """结构性失败：目标文件缺失 → 退出 3（BLOCKED，不是静默通过）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty"
            root.mkdir()
            result = _run_script(root)
            self.assertEqual(
                result.returncode,
                3,
                f"文件缺失应退出 3（BLOCKED），实际退出 {result.returncode}",
            )


if __name__ == "__main__":
    unittest.main()
