"""ISS-062：scan 开场提示基于上次实测 du_seconds 与配置上限。

反例：旧固定文案「1100 万文件量级可能需要 5-15 分钟」——生产实测 09-12
首扫约 47 分钟、09-14/15 超 60 分钟被时限中断（logs/launchd-scan.out.log
三行固定提示 vs scan_runs 实测）。本文件钉住开场提示三条语义：有同根成功
快照时分钟数来自 snapshots.du_seconds（取整，不足 1 分钟如实标注）；无库/
无快照/du_seconds 为 0 时不出现任何编造的分钟数；两种分支都携带
FATHOM_DU_TIMEOUT_S 安全上限秒数。

全部用合成运行根（tmp_path）与直接 INSERT 的快照行，绝不读写生产
data/fathom.db；run_scan 以 stub 打断，不触发真实 du。
"""

from __future__ import annotations

import argparse

import pytest

from fathom import cli, config, db


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """FATHOM_RUNTIME_DIR / FATHOM_SCAN_ROOT 显式指向测试临时目录。

    与 tests/test_cli_report.py 同源：环境变量 + config 兼容常量同时改写到
    同一运行根，保证提示的只读 DB 查询留在临时目录；重建 ``config._ACTIVE``
    避免 cli 侧沿用导入时的生产默认。DU_TIMEOUT_S 钉在文档默认 14400，
    下文断言可以写死秒数。
    """
    runtime_dir = tmp_path / "runtime"
    scan_root = tmp_path / "scanroot"
    scan_root.mkdir(parents=True)
    monkeypatch.delenv("FATHOM_DB", raising=False)  # 旧入口不得劫持运行根
    monkeypatch.setenv("FATHOM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("FATHOM_SCAN_ROOT", str(scan_root))
    monkeypatch.setattr(config, "DATA_DIR", runtime_dir / "data")
    monkeypatch.setattr(config, "DB_PATH", runtime_dir / "data" / "fathom.db")
    monkeypatch.setattr(config, "REPORTS_DIR", runtime_dir / "reports")
    monkeypatch.setattr(config, "LOGS_DIR", runtime_dir / "logs")
    monkeypatch.setattr(config, "DEFAULT_ROOT", scan_root)
    monkeypatch.setattr(config, "_ACTIVE", config.RuntimeConfig.from_env())
    config._publish_compatibility_values(config._ACTIVE)
    monkeypatch.setattr(config, "DU_TIMEOUT_S", 14400.0)


def _insert_snapshot(root: str, du_seconds: float) -> None:
    """在隔离运行根的库里直接造一行快照（不经 du）。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status) VALUES (?,?,?,?,?,?,?,?)",
            ("2026-09-12T12:47:00", root, 10, 0, du_seconds, 12345, 1024, "full"),
        )
        conn.commit()
    finally:
        conn.close()


class TestHintContent:
    """_scan_duration_hint 的两个分支与边界值。"""

    def test_no_database_at_all(self):
        # 运行根存在但从未建库：只读打开失败必须走无记录分支，且不得建库。
        hint = cli._scan_duration_hint(config.DEFAULT_ROOT)
        assert "首次或无实测记录" in hint
        assert "分钟" not in hint  # 不出现任何编造的分钟数
        assert "本次安全时限 14400 秒" in hint
        assert "FATHOM_DU_TIMEOUT_S" in hint
        assert not config.DB_PATH.exists()

    def test_empty_database_no_snapshots(self):
        db.connect().close()  # 建库（schema 就绪）但无任何快照
        hint = cli._scan_duration_hint(config.DEFAULT_ROOT)
        assert "首次或无实测记录" in hint
        assert "分钟" not in hint
        assert "本次安全时限 14400 秒" in hint

    def test_synthetic_du_seconds_renders_measured_minutes(self):
        # 2850s = 47.5 分钟 → 四舍五入 48（合同钉住值）。
        _insert_snapshot(str(config.DEFAULT_ROOT), 2850.0)
        hint = cli._scan_duration_hint(config.DEFAULT_ROOT)
        assert "上次实测 du 约 48 分钟" in hint
        assert "本次安全时限 14400 秒" in hint
        assert "FATHOM_DU_TIMEOUT_S" in hint

    def test_other_root_measurement_not_reused(self):
        # 只取同一 root 的实测；他根记录不得成为本根提示依据。
        _insert_snapshot("/synthetic/other-root", 2850.0)
        hint = cli._scan_duration_hint(config.DEFAULT_ROOT)
        assert "首次或无实测记录" in hint
        assert "分钟" not in hint

    def test_zero_du_seconds_falls_back_to_no_record(self):
        _insert_snapshot(str(config.DEFAULT_ROOT), 0.0)
        hint = cli._scan_duration_hint(config.DEFAULT_ROOT)
        assert "首次或无实测记录" in hint
        assert "分钟" not in hint
        assert "本次安全时限 14400 秒" in hint

    def test_sub_minute_measurement_stated_as_such(self):
        _insert_snapshot(str(config.DEFAULT_ROOT), 30.0)
        hint = cli._scan_duration_hint(config.DEFAULT_ROOT)
        assert "上次实测 du 不到 1 分钟" in hint

    def test_inf_du_seconds_falls_back_to_no_record(self):
        # ISS-063：库中 du_seconds=inf 时不能 OverflowError（int(minutes+0.5)
        # 会抛），必须按"无实测记录"分支走。SQLite REAL 接受 inf 真值（Python
        # float 与 SQLite REAL 都按 IEEE 754 编码），可以直接走 _insert_snapshot。
        import math

        _insert_snapshot(str(config.DEFAULT_ROOT), math.inf)
        hint = cli._scan_duration_hint(config.DEFAULT_ROOT)
        assert "首次或无实测记录" in hint
        assert "分钟" not in hint
        assert "本次安全时限 14400 秒" in hint

    def test_nan_du_seconds_falls_back_to_no_record(self, monkeypatch):
        # ISS-063：库中 du_seconds=nan 时浮点比较全为 False，必须按"无实测记录"
        # 回落，且永不抛（即使在 try 之外的 int(minutes+0.5)）。
        #
        # Python sqlite3 适配层把 math.nan 绑为 NULL → du_seconds REAL NOT NULL
        # 触发 IntegrityError，因此 _insert_snapshot 无法复现 nan 真值；
        # monkeypatch sqlite3.connect 直接以 SQL 字面量 'nan' 注入（SQLite 3.24+
        # 接受 nan 作为 numeric literal），保留从 DB row → isfinite 守卫的
        # 整条代码路径——old guard 仍 OverflowError（红），new guard 返回 None（绿）。
        import sqlite3

        real_connect = sqlite3.connect

        def fake_connect(uri, *args, **kwargs):
            conn = real_connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE snapshots ("
                "id INTEGER PRIMARY KEY, root TEXT, du_seconds REAL)"
            )
            conn.execute(
                "INSERT INTO snapshots(root, du_seconds) VALUES (?, 'nan')",
                (str(config.DEFAULT_ROOT),),
            )
            conn.commit()
            return conn

        monkeypatch.setattr(cli.sqlite3, "connect", fake_connect)
        hint = cli._scan_duration_hint(config.DEFAULT_ROOT)
        assert "首次或无实测记录" in hint
        assert "分钟" not in hint
        assert "本次安全时限 14400 秒" in hint


class TestCmdScanWiring:
    """cmd_scan 的开场行确实携带该提示（打印发生在 run_scan 之前）。"""

    def test_cmd_scan_prints_hint_line(self, monkeypatch, capsys):
        def _stub_run_scan(**kwargs):
            raise cli.scan_coordinator.ScanCancelledError("stub：不进入真实 du")

        monkeypatch.setattr(cli.scan_coordinator, "run_scan", _stub_run_scan)
        rc = cli.cmd_scan(
            argparse.Namespace(root=str(config.DEFAULT_ROOT), source="cli")
        )
        out = capsys.readouterr().out
        assert rc == 130
        assert out.startswith(f"开始扫描 {config.DEFAULT_ROOT} ……（")
        assert "首次或无实测记录" in out
        assert "本次安全时限 14400 秒" in out
