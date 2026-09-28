"""ISS-113 · ``auto_download_updates`` 配置项（应用更新无感化）。

合同（与 fathom/config.py、apps/desktop/src-tauri/src/lib.rs 对齐）：
- 存储 = 运行根 settings.json（既有 ISS-016A 链路）：前端经 PUT /api/config
  写入，桌面壳（Rust）以 std 文件 I/O **只读**同一文件——单一设置源，不引
  第二份壳侧存储，不新增 Tauri 命令（恰 3 updater 命令 ACL 合同不破）。
- 默认 true：未持久化（None）/缺键/无文件都按默认开；仅显式 false 关闭
  （关闭 = 回 ISS-040B 现状：发现可用只提示，下载留在安装确认事务内）。
- 严格布尔：``1``/``0``/``"false"`` 等 JSON 可表达的近亲一律 fail-closed
  （开关语义只有真开/真关两态，宽接收会静默改变用户意图）。
- 排除集环境变量覆盖（FATHOM_EXCLUDE_NAMES）只影响 exclude_names；其余已
  持久化项（含本项）必须原样透传，不得静默回落默认。
- Rust 壳侧合同钉子（grep 断言，同 test_upgrade_txn_journal.py 的 lib.rs
  钉子风格）：lib.rs 必须读 settings.json 的 auto_download_updates 键、
  必须发射 downloaded（ready）事件；安装仍必须 confirmed 确认——无感化
  （自动下载）不得越过六步安装合同的确认边界。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fathom import config

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_RS = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "src" / "lib.rs"


def _reset_user_settings(**kwargs):
    """把进程内生效设置显式重置为给定 UserSettings（避免前序用例残留）。"""
    return config.refresh_user_settings(settings=config.UserSettings(**kwargs))


# ---------- 校验与默认 ----------


@pytest.mark.parametrize("bad", [1, 0, "false", "true", [True], {"v": True}])
def test_auto_download_updates_rejects_non_bool_shapes(bad):
    with pytest.raises(config.ConfigurationError):
        config.parse_user_settings({"auto_download_updates": bad})


def test_auto_download_updates_accepts_both_bools():
    assert config.parse_user_settings({"auto_download_updates": True}).auto_download_updates is True
    assert config.parse_user_settings({"auto_download_updates": False}).auto_download_updates is False
    # 未持久化 = None（内置默认 true 由 effective 视图给出，不落盘）
    assert config.parse_user_settings({}).auto_download_updates is None


def test_auto_download_updates_default_true_in_effective_view():
    try:
        _reset_user_settings()
        view = config.effective_settings_view()
        assert view["auto_download_updates"] is True
        assert view["sources"]["auto_download_updates"] == "default"
        assert view["defaults"]["auto_download_updates"] is True
    finally:
        _reset_user_settings()


def test_auto_download_updates_roundtrip_persists_false(tmp_path):
    path = tmp_path / "settings.json"
    settings = config.parse_user_settings({"auto_download_updates": False})
    config.save_user_settings(path, settings)
    # 只落已持久化键：显式 false 落盘，其余默认键不伪造
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"auto_download_updates": False}
    loaded = config.load_user_settings(path)
    assert loaded.auto_download_updates is False
    try:
        merged = config.refresh_user_settings(settings=loaded)
        view = config.effective_settings_view()
        assert merged.auto_download_updates is False
        assert view["auto_download_updates"] is False
        assert view["sources"]["auto_download_updates"] == "settings"
    finally:
        _reset_user_settings()


def test_auto_download_updates_survives_exclude_env_override():
    """FATHOM_EXCLUDE_NAMES 只覆盖排除集；已持久化的 auto_download_updates
    必须原样透传（构造分支漏传会把显式关闭静默改回默认开）。"""
    settings = config.parse_user_settings({"auto_download_updates": False})
    refreshed = config.refresh_user_settings(
        settings=settings,
        environ={"FATHOM_EXCLUDE_NAMES": "*.tmp"},
    )
    assert refreshed.exclude_names == "*.tmp"
    assert refreshed.auto_download_updates is False
    _reset_user_settings()


def test_update_user_settings_merges_auto_download_without_touching_others(tmp_path, monkeypatch):
    """部分更新只动出现的键：开关切换不改扫描根等其余设置。"""
    monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings())
    monkeypatch.setattr(
        config, "settings_path", lambda: tmp_path / "settings.json")
    current = config.update_user_settings({"auto_download_updates": False})
    assert current.auto_download_updates is False
    assert current.scan_root is None
    # 部分更新再切回 true：其余键保持
    current = config.update_user_settings({"scan_time": "09:30"})
    assert current.auto_download_updates is False
    assert current.scan_time == "09:30"


# ---------- Rust 壳侧合同钉子（grep，跨语言合同互为证据链） ----------


def _lib_rs_code() -> str:
    return LIB_RS.read_text(encoding="utf-8")


def test_lib_rs_reads_auto_download_key_from_settings_json():
    """壳侧只读同一 settings.json：键名常量 + settings.json 读取必须在场。"""
    code = _lib_rs_code()
    assert '"auto_download_updates"' in code, (
        "lib.rs 必须引用 auto_download_updates 键（ISS-113 合同：壳读 Python "
        "config.py 持久化的同一 settings.json，单一设置源）"
    )
    assert 'runtime_dir.join("settings.json")' in code, (
        "lib.rs 必须经运行根 settings.json 读取设置（std 文件 I/O，不引 HTTP）"
    )
    # 宽读 fail-safe：只有显式布尔 false 才视为关闭（缺键/损坏 → 默认开）
    assert "Some(serde_json::Value::Bool(false))" in code


def test_lib_rs_emits_downloaded_ready_state_and_prefetch_entry():
    code = _lib_rs_code()
    assert 'fn maybe_spawn_updater_prefetch' in code, "自动下载入口函数必须在场"
    assert "fn updater_prefetch_ready_json" in code, "ready 事件载荷函数必须在场"
    assert '"state": "downloaded"' in code, (
        "后台下载完成必须发射 state=downloaded（前端 ready 态的唯一事实源）"
    )
    # 复用既有下载路径与独占门（不新增命令；与安装事务互斥）
    assert "fn download_update_with_progress" in code
    assert "try_begin_install" in code


def test_lib_rs_install_still_requires_confirmation():
    """无感化边界钉子：安装仍必须 confirmed=true——自动下载不得越过六步
    安装合同的确认层（不做静默安装）。"""
    code = _lib_rs_code()
    assert "updater_install 需经用户确认（confirmed=true）；本壳不做静默安装" in code, (
        "updater_install 的 confirmed 确认门文案必须在场（静默安装回退守卫）"
    )
    assert "重启需经用户确认（confirmed=true）；未重启前保持当前版本运行" in code, (
        "updater_restart 的 confirmed 确认门文案必须在场（静默重启回退守卫）"
    )
