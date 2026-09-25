"""ISS-040B · Tauri ACL updater 授权的 fail-closed 守护（010B-ACL 同风格）。

设计（与 test_tauri_autostart_acl.py 同型，差异点见各断言）：
``lib.rs`` generate_handler 注册三个 updater 协调命令（updater_check /
updater_install / updater_restart），本切片新增 ``permissions/updater.toml``
三条应用级权限并在 capability 追加三个 identifier。tauri-build 在编译期校验
capability 引用的权限必须在 permissions/ 有定义（构建期防线，
``scripts/ci_cargo_locked.sh``），本测试是 pytest 侧的运行期防线。

权限面语义（逐条，与 permissions/updater.toml 内注释一致）：
- allow-updater-check：设置页「检查更新」按钮 → updater_check（只读检查，
  不下载不安装；返回结构化状态 JSON）。
- allow-updater-install：「下载并安装」确认层 → updater_install（必须
  confirmed=true 才动手；下载+验签+安装，失败可恢复）。
- allow-updater-restart：「重启以完成」确认层 → updater_restart（必须
  confirmed=true；relaunch 走 request_restart，退出钩子幂等回收 helper）。

**为什么是应用级权限而不是插件权限**（ISS-040 父卡边界）：更新动作只在可信
Rust 壳内执行，前端经本壳命令协调；capability 服务于本地页与 127.0.0.1:7952
远程仪表盘两类上下文，若改授 ``updater:allow-check`` /
``updater:allow-download-and-install`` / ``process:allow-restart`` 等插件
权限，等于把宽泛 updater 能力交给回环远程页面，且绕开 confirmed 确认层。
故本测试同时断言 capability **不含**任何 updater:/process: 前缀的插件权限。

本测试钉住（grep 断言风格，同 test_launchd_autostart.py 源码守护段）：
① 三条 updater 权限都在 capability 的 permissions 数组里；
② updater.toml 的 commands.allow 与 lib.rs generate_handler 接线的
   updater 命令名一一对应（集合相等——删命令名/改名/漏权限都红）；
③ capability 里 updater 相关应用权限恰为三条（防顺手放宽）；
④ 不出现 updater:/process: 插件级权限（更新留在可信壳内）；
⑤ 既有权限（tray/autostart/opener/core）不回退，remote urls 不扩域。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_TAURI = REPO_ROOT / "apps" / "desktop" / "src-tauri"
UPDATER_TOML = _SRC_TAURI / "permissions" / "updater.toml"
CAPABILITY_JSON = _SRC_TAURI / "capabilities" / "default.json"
LIB_RS = _SRC_TAURI / "src" / "lib.rs"

# 合同固定的三条权限：identifier 与命令名按下标一一配对。
EXPECTED_IDENTIFIERS = (
    "allow-updater-check",
    "allow-updater-install",
    "allow-updater-restart",
)
EXPECTED_COMMANDS = (
    "updater_check",
    "updater_install",
    "updater_restart",
)

# ⑤ 既有权限基线（ISS-008/068/010B 起 + 本切片三条）：后续切片不得使其回退。
EXISTING_CAPABILITY_PERMISSIONS = (
    "core:default",
    "core:window:allow-show",
    "core:window:allow-hide",
    "core:window:allow-set-focus",
    "core:event:default",
    "allow-update-tray-status",
    "allow-autostart-status",
    "allow-autostart-register-plan",
    "allow-autostart-register",
    "allow-autostart-unregister",
    "allow-updater-check",
    "allow-updater-install",
    "allow-updater-restart",
    "opener:allow-open-url",
)
EXISTING_REMOTE_URLS = {
    "http://127.0.0.1:*",
    "http://localhost:*",
}


def _capability_permissions() -> list[str]:
    """capability permissions 数组条目（裸字符串原样，对象条目取 identifier）。"""
    data = json.loads(CAPABILITY_JSON.read_text(encoding="utf-8"))
    return [
        item if isinstance(item, str) else item["identifier"]
        for item in data["permissions"]
    ]


def _capability_remote_urls() -> set[str]:
    data = json.loads(CAPABILITY_JSON.read_text(encoding="utf-8"))
    return set(data.get("remote", {}).get("urls", []))


def _updater_toml_permissions() -> dict[str, list[str]]:
    """updater.toml 的 identifier → commands.allow 映射。"""
    data = tomllib.loads(UPDATER_TOML.read_text(encoding="utf-8"))
    return {
        p["identifier"]: list(p.get("commands", {}).get("allow", []))
        for p in data["permission"]
    }


def _rust_code_without_line_comments(path: Path) -> str:
    """去掉 ``//`` 行注释后的代码文本（同 test_launchd_autostart.py 手法）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("//"))


def _wired_updater_commands() -> set[str]:
    """lib.rs generate_handler 块内接线的 updater 命令名（本模块裸名）。"""
    code = _rust_code_without_line_comments(LIB_RS)
    block = re.search(r"generate_handler!\[(.*?)\]", code, re.DOTALL)
    assert block, "lib.rs 必须存在 generate_handler 接线块"
    # 裸名 updater_*；autostart:: 前缀命令不含 "updater_" 子串，无串扰。
    return set(re.findall(r"\bupdater_[a-z_]+\b", block.group(1)))


# ---- ① 三条权限在 capability 里 ----


def test_capability_grants_all_updater_permissions():
    granted = _capability_permissions()
    for ident in EXPECTED_IDENTIFIERS:
        assert ident in granted, (
            f"capability 未授权 {ident}——设置页 invoke 会被 ACL 静默拒绝"
            "（ISS-068 同型缺口，ISS-040B）"
        )


# ---- ③ updater 相关应用权限恰为三条（防顺手放宽） ----


def test_capability_updater_permissions_exactly_three():
    drift = sorted(p for p in _capability_permissions() if "updater" in p)
    assert drift == sorted(EXPECTED_IDENTIFIERS), (
        f"capability 里 updater 相关权限必须恰为合同三条，实际：{drift}。"
        " 新增 updater 权限需另立任务显式扩合同，不允许顺手放宽"
    )


def test_updater_toml_defines_exactly_three_permissions():
    """TOML 侧恰三条 [[permission]]，identifier 与合同一致、每条恰授权一个命令。"""
    toml_perms = _updater_toml_permissions()
    assert sorted(toml_perms) == sorted(EXPECTED_IDENTIFIERS), (
        f"updater.toml 必须且只能定义合同三条权限，实际：{sorted(toml_perms)}"
    )
    for ident, cmd in zip(EXPECTED_IDENTIFIERS, EXPECTED_COMMANDS):
        assert toml_perms[ident] == [cmd], (
            f"{ident} 的 commands.allow 必须恰为 [{cmd!r}]，"
            f"实际 {toml_perms[ident]}"
        )


# ---- ② TOML commands.allow ↔ generate_handler 一一对应（两边漂移必红） ----


def test_updater_toml_commands_match_generate_handler():
    """删命令名/改名/漏权限都红：三集合（TOML/接线/合同）必须相等。"""
    toml_cmds = sorted(
        c for allow in _updater_toml_permissions().values() for c in allow
    )
    wired = sorted(_wired_updater_commands())
    assert toml_cmds == wired == sorted(EXPECTED_COMMANDS), (
        f"updater 授权漂移：TOML 授权 {toml_cmds}、lib.rs 接线 {wired}、"
        f"合同 {sorted(EXPECTED_COMMANDS)}——三者必须一一对应"
    )


def test_capability_updater_identifiers_defined_in_toml():
    """capability 引用的 updater 权限必须在本仓 permissions/updater.toml
    有定义（漏写 TOML 时 tauri-build 编译期会拦，这里是 pytest 侧先红）。"""
    defined = set(_updater_toml_permissions())
    for ident in EXPECTED_IDENTIFIERS:
        assert ident in defined, (
            f"capability 引用了 {ident}，但 permissions/updater.toml 未定义"
            "（tauri-build 将编译失败）"
        )


def test_updater_identifier_command_pairing_canonical():
    """identifier ↔ 命令名按约定配对（allow-updater-x ↔ updater_x），
    交叉错配（如 check 权限授权 install 命令）必红。"""
    toml_perms = _updater_toml_permissions()
    for ident, cmd in zip(EXPECTED_IDENTIFIERS, EXPECTED_COMMANDS):
        assert ident == f"allow-{cmd.replace('_', '-')}", (
            f"命名约定漂移：{ident} 应对应命令 {cmd}"
        )
        assert toml_perms[ident] == [cmd], f"{ident} 应授权 {cmd}"


# ---- ④ 不出现插件级 updater/process 权限（更新留在可信 Rust 壳） ----


def test_no_plugin_level_updater_or_process_permissions():
    """ISS-040 父卡边界：capability 服务于本地页与 127.0.0.1:7952 远程仪表盘，
    授插件级 updater:/process: 权限会把宽泛更新能力交给远程页面并绕开
    confirmed 确认层。前端只经本壳三个命令协调。"""
    offenders = sorted(
        p for p in _capability_permissions()
        if p.startswith("updater:") or p.startswith("process:")
    )
    assert not offenders, (
        f"capability 出现插件级更新权限 {offenders}——更新动作必须经本壳"
        " updater_check/updater_install/updater_restart 命令（可信壳边界）"
    )


# ---- ⑤ 既有权限不回退、remote urls 不扩域 ----


def test_existing_permissions_baseline_not_regressed():
    """core/opener/autostart/tray 既有权限与 remote urls 基线不变。"""
    granted = _capability_permissions()
    for perm in EXISTING_CAPABILITY_PERMISSIONS:
        assert perm in granted, f"既有权限 {perm} 缺失（回退）"
    assert _capability_remote_urls() == EXISTING_REMOTE_URLS, (
        "remote urls 基线漂移——本切片绝不扩域（合同边界）"
    )
