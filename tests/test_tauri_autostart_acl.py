"""ISS-010B PM 收口清单③ · Tauri ACL autostart 授权的 fail-closed 守护。

反例（修复前现状，reviewer NB-1 / ISS-068 同型缺口）：
``lib.rs`` generate_handler 注册了四个 autostart 命令（autostart_status /
autostart_register_plan / autostart_register / autostart_unregister），
但 ``capabilities/default.json`` 未授权、``permissions/`` 目录无对应 TOML——
设置页（frontend/modules/pages/settings.js，经 127.0.0.1:7952 远程仪表盘
加载）invoke 会被 ACL 静默拒绝。ISS-010A 起就存在（当时 autostart_status
同样未授权），ISS-010B 合并后扩大到四条。

修复（本切片）：新增 ``permissions/autostart.toml`` 四条应用级权限（仿
update-tray-status.toml 的 ``[[permission]]`` 结构），capability 在
allow-update-tray-status 之后追加四个 identifier。tauri-build 在编译期校验
capability 引用的权限必须在 permissions/ 有定义（构建期防线，
``scripts/ci_cargo_locked.sh``），本测试是 pytest 侧的运行期防线。

本测试钉住（grep 断言风格，同 test_launchd_autostart.py 源码守护段）：
① 四条 autostart 权限都在 capability 的 permissions 数组里；
② autostart.toml 的 commands.allow 与 lib.rs generate_handler 接线的
   autostart 命令名一一对应（集合相等——删命令名/改名/漏权限都红）；
③ capability 里 autostart 相关权限恰为四条（防顺手放宽）；
④ update-tray-status 等既有权限不回退，remote urls 不扩域。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_TAURI = REPO_ROOT / "apps" / "desktop" / "src-tauri"
AUTOSTART_TOML = _SRC_TAURI / "permissions" / "autostart.toml"
TRAY_TOML = _SRC_TAURI / "permissions" / "update-tray-status.toml"
CAPABILITY_JSON = _SRC_TAURI / "capabilities" / "default.json"
LIB_RS = _SRC_TAURI / "src" / "lib.rs"

# 合同固定的四条权限：identifier 与命令名按下标一一配对。
EXPECTED_IDENTIFIERS = (
    "allow-autostart-status",
    "allow-autostart-register-plan",
    "allow-autostart-register",
    "allow-autostart-unregister",
)
EXPECTED_COMMANDS = (
    "autostart_status",
    "autostart_register_plan",
    "autostart_register",
    "autostart_unregister",
)

# ④ 既有权限基线（ISS-008/ISS-068 起）：本切片不得使其回退。
EXISTING_CAPABILITY_PERMISSIONS = (
    "core:default",
    "core:window:allow-show",
    "core:window:allow-hide",
    "core:window:allow-set-focus",
    "core:event:default",
    "allow-update-tray-status",
    "opener:allow-open-url",
)
EXISTING_REMOTE_URLS = {
    "http://127.0.0.1:7952",
    "http://localhost:7952",
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


def _autostart_toml_permissions() -> dict[str, list[str]]:
    """autostart.toml 的 identifier → commands.allow 映射。"""
    data = tomllib.loads(AUTOSTART_TOML.read_text(encoding="utf-8"))
    return {
        p["identifier"]: list(p.get("commands", {}).get("allow", []))
        for p in data["permission"]
    }


def _rust_code_without_line_comments(path: Path) -> str:
    """去掉 ``//`` 行注释后的代码文本（同 test_launchd_autostart.py 手法）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("//"))


def _wired_autostart_commands() -> set[str]:
    """lib.rs generate_handler 块内接线的 autostart 命令名（去模块前缀）。"""
    code = _rust_code_without_line_comments(LIB_RS)
    block = re.search(r"generate_handler!\[(.*?)\]", code, re.DOTALL)
    assert block, "lib.rs 必须存在 generate_handler 接线块"
    return set(re.findall(r"\bautostart::(\w+)", block.group(1)))


# ---- ① 四条权限在 capability 里 ----


def test_capability_grants_all_four_autostart_permissions():
    granted = _capability_permissions()
    for ident in EXPECTED_IDENTIFIERS:
        assert ident in granted, (
            f"capability 未授权 {ident}——设置页 invoke 会被 ACL 静默拒绝"
            "（ISS-068 同型缺口，ISS-010B PM 收口清单③）"
        )


# ---- ③ autostart 相关权限恰为四条（防顺手放宽） ----


def test_capability_autostart_permissions_exactly_four():
    drift = sorted(p for p in _capability_permissions() if "autostart" in p)
    assert drift == sorted(EXPECTED_IDENTIFIERS), (
        f"capability 里 autostart 相关权限必须恰为合同四条，实际：{drift}。"
        " 新增 autostart 权限需另立任务显式扩合同，不允许顺手放宽"
    )


def test_autostart_toml_defines_exactly_four_permissions():
    """TOML 侧恰四条 [[permission]]，identifier 与合同一致、每条恰授权一个命令。"""
    toml_perms = _autostart_toml_permissions()
    assert sorted(toml_perms) == sorted(EXPECTED_IDENTIFIERS), (
        f"autostart.toml 必须且只能定义合同四条权限，实际："
        f"{sorted(toml_perms)}"
    )
    for ident, cmd in zip(EXPECTED_IDENTIFIERS, EXPECTED_COMMANDS):
        assert toml_perms[ident] == [cmd], (
            f"{ident} 的 commands.allow 必须恰为 [{cmd!r}]，"
            f"实际 {toml_perms[ident]}"
        )


# ---- ② TOML commands.allow ↔ generate_handler 一一对应（两边漂移必红） ----


def test_autostart_toml_commands_match_generate_handler():
    """删命令名/改名/漏权限都红：三集合（TOML/接线/合同）必须相等。"""
    toml_cmds = sorted(
        c for allow in _autostart_toml_permissions().values() for c in allow
    )
    wired = sorted(_wired_autostart_commands())
    assert toml_cmds == wired == sorted(EXPECTED_COMMANDS), (
        f"autostart 授权漂移：TOML 授权 {toml_cmds}、lib.rs 接线 {wired}、"
        f"合同 {sorted(EXPECTED_COMMANDS)}——三者必须一一对应"
    )


def test_capability_autostart_identifiers_defined_in_toml():
    """capability 引用的 autostart 权限必须在本仓 permissions/autostart.toml
    有定义（漏写 TOML 时 tauri-build 编译期会拦，这里是 pytest 侧先红）。"""
    defined = set(_autostart_toml_permissions())
    for ident in EXPECTED_IDENTIFIERS:
        assert ident in defined, (
            f"capability 引用了 {ident}，但 permissions/autostart.toml 未定义"
            "（tauri-build 将编译失败）"
        )


def test_autostart_identifier_command_pairing_canonical():
    """identifier ↔ 命令名按约定配对（allow-autostart-x ↔ autostart_x），
    交叉错配（如 register 权限授权 unregister 命令）必红。"""
    toml_perms = _autostart_toml_permissions()
    for ident, cmd in zip(EXPECTED_IDENTIFIERS, EXPECTED_COMMANDS):
        assert ident == f"allow-{cmd.replace('_', '-')}", (
            f"命名约定漂移：{ident} 应对应命令 {cmd}"
        )
        assert toml_perms[ident] == [cmd], f"{ident} 应授权 {cmd}"


# ---- ④ 既有权限不回退、remote urls 不扩域 ----


def test_existing_tray_status_permission_not_regressed():
    """ISS-008 的 allow-update-tray-status 授权链三件套不回退。"""
    assert "allow-update-tray-status" in _capability_permissions()
    data = tomllib.loads(TRAY_TOML.read_text(encoding="utf-8"))
    tray = {p["identifier"]: p.get("commands", {}).get("allow", []) for p in data["permission"]}
    assert tray.get("allow-update-tray-status") == ["update_tray_status"], (
        "update-tray-status.toml 的授权内容被改动（回退）"
    )
    code = _rust_code_without_line_comments(LIB_RS)
    block = re.search(r"generate_handler!\[(.*?)\]", code, re.DOTALL)
    assert block and "update_tray_status" in block.group(1), (
        "lib.rs generate_handler 不再接线 update_tray_status（回退）"
    )


def test_existing_core_permission_baseline_not_regressed():
    """core/opener 既有权限与 remote urls 基线不变（本切片只加四条 autostart）。"""
    granted = _capability_permissions()
    for perm in EXISTING_CAPABILITY_PERMISSIONS:
        assert perm in granted, f"既有权限 {perm} 缺失（回退）"
    assert _capability_remote_urls() == EXISTING_REMOTE_URLS, (
        "remote urls 基线漂移——本切片绝不扩域（合同边界）"
    )
