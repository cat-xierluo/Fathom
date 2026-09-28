"""共享夹具：进程级配置隔离（分析套件使用；非 autouse，按名引用）。

把可写路径全部指到临时目录，避免触碰生产 data/reports/logs/HOME；
模块常量经 monkeypatch 注册，测试结束后自动恢复。
"""

from __future__ import annotations

import pytest

from fathom import config


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    scanroot = tmp_path / "scanroot"
    scanroot.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cfg = config.RuntimeConfig.from_env(
        {"FATHOM_RUNTIME_DIR": str(runtime), "FATHOM_SCAN_ROOT": str(scanroot)},
        project_root=tmp_path, home=home,
    )
    monkeypatch.setattr(config, "_ACTIVE", cfg)
    monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings())
    monkeypatch.setattr(config, "_CLI_SCAN_ROOT_PINNED", False)
    module_attrs = {
        "DATA_DIR": cfg.data_dir, "REPORTS_DIR": cfg.reports_dir,
        "LOGS_DIR": cfg.logs_dir, "DB_PATH": cfg.db_path,
        "FRONTEND_DIR": cfg.frontend_dir, "DEFAULT_ROOT": cfg.scan_root,
        "PORT": cfg.port,
    }
    for name, value in module_attrs.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "EXCLUDE_NAMES", [])
    return {"cfg": cfg, "scanroot": scanroot, "runtime": runtime,
            "home": home}
