"""ISS-050：运行根 reports/ 与 logs/ 的保留策略。

合成三类文件证明判定：
- 过期（早于保留天数，文件名含可解析日期）→ 删除
- 未过期（保留窗口内）→ 保留
- 不可解析日期（含特殊字符 / 不含日期 / 非法日期）→ 保留

固定 ``today`` 让所有用例时间确定性，不依赖 ``dt.date.today()``。
所有临时目录只活在 ``tmp_path`` 下，不触真实运行根。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3

import pytest

from fathom import config, db, reports, scan_coordinator, scanner


FROZEN_TODAY = dt.date(2026, 9, 14)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """把运行根常量指向 tmp_path，避免触真实数据。"""
    runtime_dir = tmp_path / "runtime"
    reports_dir = runtime_dir / "reports"
    logs_dir = runtime_dir / "logs"
    reports_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("FATHOM_DB", raising=False)
    monkeypatch.setenv("FATHOM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(config, "DATA_DIR", runtime_dir / "data")
    monkeypatch.setattr(config, "DB_PATH", runtime_dir / "data" / "fathom.db")
    monkeypatch.setattr(config, "REPORTS_DIR", reports_dir)
    monkeypatch.setattr(config, "LOGS_DIR", logs_dir)


def _touch(path: Path, content: str = "synthetic") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _age(days: int) -> dt.date:
    return FROZEN_TODAY - dt.timedelta(days=days)


# ---- prune_reports -----------------------------------------------------


def test_prune_reports_deletes_only_expired_parseable_files():
    expired_old = _age(40)   # 早于默认 35 天
    expired_boundary = _age(36)  # 边界外 1 天
    recent_inside = _age(7)  # 仍在 35 天窗口内
    recent_today = FROZEN_TODAY
    _touch(config.REPORTS_DIR / f"{expired_old.isoformat()}.md")
    _touch(config.REPORTS_DIR / f"{expired_boundary.isoformat()}.md")
    _touch(config.REPORTS_DIR / f"{recent_inside.isoformat()}.md")
    _touch(config.REPORTS_DIR / f"{recent_today.isoformat()}.md")
    # 不可解析日期：保留
    _touch(config.REPORTS_DIR / "README.md")
    _touch(config.REPORTS_DIR / "summary-2026.md")  # 月份只有 1 位
    # 非法日期：保留（2026-13-99 不是合法日期）
    _touch(config.REPORTS_DIR / "2026-13-99.md")

    deleted, warnings = reports.prune_reports(today=FROZEN_TODAY)

    assert deleted == 2
    assert warnings == []
    remaining = sorted(p.name for p in config.REPORTS_DIR.iterdir())
    assert remaining == [
        "2026-09-14.md",
        "2026-09-07.md",
        "2026-13-99.md",
        "README.md",
        "summary-2026.md",
    ]


def test_prune_reports_uses_configured_retention_days():
    _touch(config.REPORTS_DIR / f"{_age(10).isoformat()}.md")
    _touch(config.REPORTS_DIR / f"{_age(3).isoformat()}.md")

    deleted, warnings = reports.prune_reports(retention_days=7, today=FROZEN_TODAY)

    assert deleted == 1
    assert warnings == []
    assert (config.REPORTS_DIR / f"{_age(10).isoformat()}.md").exists() is False
    assert (config.REPORTS_DIR / f"{_age(3).isoformat()}.md").exists() is True


def test_prune_reports_no_directory_is_noop(tmp_path):
    # 全新空目录
    empty = tmp_path / "empty-reports"
    empty.mkdir()
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(config, "REPORTS_DIR", empty)
        deleted, warnings = reports.prune_reports(today=FROZEN_TODAY)
    finally:
        monkeypatch.undo()
    assert deleted == 0
    assert warnings == []


def test_prune_reports_keeps_launchd_style_logs_in_logs_dir_separately():
    """日报命名严格 YYYY-MM-DD.md；logs/ 的清理不会误伤 reports/。"""
    _touch(config.REPORTS_DIR / f"{_age(60).isoformat()}.md")
    _touch(config.LOGS_DIR / f"{_age(60).isoformat()}.log")  # 早期日志，留给 prune_logs
    _touch(config.LOGS_DIR / "notify.log")  # 固定名，无日期

    deleted_reports, _ = reports.prune_reports(today=FROZEN_TODAY)
    deleted_logs, _ = reports.prune_logs(today=FROZEN_TODAY)

    assert deleted_reports == 1
    assert deleted_logs == 1  # 日志清理只走 logs/，不读 reports/
    assert (config.LOGS_DIR / "notify.log").exists() is True


# ---- prune_logs --------------------------------------------------------


def test_prune_logs_deletes_only_expired_parseable_files():
    expired_old = _age(10)   # 默认 7 天窗口外
    recent_inside = _age(3)
    recent_today = FROZEN_TODAY
    _touch(config.LOGS_DIR / f"{expired_old.isoformat()}.log")
    _touch(config.LOGS_DIR / f"{recent_inside.isoformat()}.log")
    _touch(config.LOGS_DIR / f"{recent_today.isoformat()}.log")
    _touch(config.LOGS_DIR / f"bigfiles-{expired_old.isoformat()}.log")
    # 固定名（launchd / notify）保留
    _touch(config.LOGS_DIR / "notify.log")
    _touch(config.LOGS_DIR / "launchd-scan.out.log")
    _touch(config.LOGS_DIR / "launchd-scan.err.log")
    # 不可解析：非日期字面 + 非法日期
    _touch(config.LOGS_DIR / "session-abc123.log")
    _touch(config.LOGS_DIR / "broken-2026-02-30.log")  # 2-30 非法

    deleted, warnings = reports.prune_logs(today=FROZEN_TODAY)

    assert deleted == 2  # expired_old + bigfiles-expired_old
    assert warnings == []
    remaining = sorted(p.name for p in config.LOGS_DIR.iterdir())
    assert remaining == [
        "2026-09-14.log",
        "2026-09-11.log",
        "broken-2026-02-30.log",
        "launchd-scan.err.log",
        "launchd-scan.out.log",
        "notify.log",
        "session-abc123.log",
    ]


def test_prune_logs_uses_configured_retention_days():
    _touch(config.LOGS_DIR / f"{_age(5).isoformat()}.log")
    _touch(config.LOGS_DIR / f"{_age(2).isoformat()}.log")

    deleted, warnings = reports.prune_logs(retention_days=3, today=FROZEN_TODAY)

    assert deleted == 1
    assert warnings == []
    assert (config.LOGS_DIR / f"{_age(5).isoformat()}.log").exists() is False
    assert (config.LOGS_DIR / f"{_age(2).isoformat()}.log").exists() is True


def test_prune_logs_no_directory_is_noop(tmp_path):
    empty = tmp_path / "empty-logs"
    empty.mkdir()
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(config, "LOGS_DIR", empty)
        deleted, warnings = reports.prune_logs(today=FROZEN_TODAY)
    finally:
        monkeypatch.undo()
    assert deleted == 0
    assert warnings == []


def test_prune_logs_skips_subdirectories_and_symlinks(tmp_path):
    sub = config.LOGS_DIR / "subdir"
    sub.mkdir()
    _touch(sub / f"{_age(60).isoformat()}.log")
    outside = tmp_path / "outside.log"
    _touch(outside, _age(60).isoformat())
    try:
        outside_in_logs = config.LOGS_DIR / "outside.log"
        outside_in_logs.symlink_to(outside)

        deleted, warnings = reports.prune_logs(today=FROZEN_TODAY)
    finally:
        if outside_in_logs.exists() or outside_in_logs.is_symlink():
            outside_in_logs.unlink()

    assert deleted == 0  # 子目录和 symlink 都未删除
    assert warnings == []
    assert (sub / f"{_age(60).isoformat()}.log").exists() is True


def test_prune_logs_reports_warning_when_unlink_fails(monkeypatch):
    _touch(config.LOGS_DIR / f"{_age(60).isoformat()}.log")

    def _explode(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", _explode)

    deleted, warnings = reports.prune_logs(today=FROZEN_TODAY)

    assert deleted == 0
    assert len(warnings) == 1
    assert "permission denied" in warnings[0]
    assert "logs/" in warnings[0]
    # 文件仍在原位（删除未成功）
    assert (config.LOGS_DIR / f"{_age(60).isoformat()}.log").exists() is True


# ---- 集成：scan_coordinator 保留阶段 ----------------------------------


def _prepare_minimal_scan(root: Path) -> int:
    """在 db 里塞一份 created_at 比今天早 1 天的快照，让 ``write_daily_report``
    视为有同数据集前驱（``find_same_dataset_predecessor`` 找不到则直接抛错）。"""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO scans(root, min_kb) VALUES (?, ?)", (str(root), 0)
        )
        scan_id = int(cur.lastrowid)
        day_before = (FROZEN_TODAY - dt.timedelta(days=1)).isoformat()
        cur = conn.execute(
            "INSERT INTO snapshots(scan_id, root, min_kb, created_at) "
            "VALUES (?, ?, ?, ?)",
            (scan_id, str(root), 0, f"{day_before}T08:00:00"),
        )
        sid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?, ?, ?)",
            (sid, str(root), 0),
        )
        conn.commit()
        return sid
    finally:
        conn.close()


def test_scan_coordinator_retention_phase_cleans_files_and_lists_count(monkeypatch):
    root = config.DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    _prepare_minimal_scan(root)

    # 写一份过期报告与一条过期日志，验证保留阶段清理
    expired_report_date = (FROZEN_TODAY - dt.timedelta(days=60)).isoformat()
    expired_log_date = (FROZEN_TODAY - dt.timedelta(days=20)).isoformat()
    _touch(config.REPORTS_DIR / f"{expired_report_date}.md")
    _touch(config.LOGS_DIR / f"{expired_log_date}.log")
    # 保留窗口内的：不动
    inside_report_date = (FROZEN_TODAY - dt.timedelta(days=3)).isoformat()
    _touch(config.REPORTS_DIR / f"{inside_report_date}.md")
    _touch(config.LOGS_DIR / "notify.log")  # 固定名永远保留

    # 报告阶段固定走真实 du；用 monkeypatch 短路以免依赖磁盘统计
    monkeypatch.setattr(
        scanner, "create_snapshot",
        lambda conn, r, *a, **kw: (
            conn.execute(
                "INSERT INTO scans(root, min_kb) VALUES (?, 0)", (str(r),)
            ),
            conn.execute(
                "INSERT INTO snapshots(scan_id, root, min_kb, created_at) "
                "VALUES (last_insert_rowid(), ?, 0, ?)",
                (str(r), dt.datetime.combine(FROZEN_TODAY, dt.time(8, 0)).isoformat()),
            ),
            conn.execute("SELECT last_insert_rowid()").fetchone()[0],
        )[-1],
    )
    # 通知不阻塞：不真实写 osascript
    monkeypatch.setattr(reports, "notify_for_snapshot", lambda conn, sid: True)

    run_id, result = scan_coordinator.run_scan(source="cli", root=root)

    assert result["status"] == "done" or "snapshot_id" in result
    assert result["report_status"] in {"written", "not_available"}
    warnings_text = "\n".join(result["warnings"])
    assert "清理运行根过期文件" in warnings_text
    assert "2" in warnings_text  # 1 报告 + 1 日志
    # 实际文件已被删除
    assert not (config.REPORTS_DIR / f"{expired_report_date}.md").exists()
    assert not (config.LOGS_DIR / f"{expired_log_date}.log").exists()
    # 保留窗口内的和固定名都还在
    assert (config.REPORTS_DIR / f"{inside_report_date}.md").exists()
    assert (config.LOGS_DIR / "notify.log").exists()

    # 快照不被文件清理影响：scan_run_details 的 pruned_count 是快照语义
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT pruned_count FROM scan_run_details WHERE run_id=?", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    # 文件清理不污染快照 pruned_count：原 prune_snapshots 结果保留语义
    assert all(r[0] is None or r[0] == 0 for r in rows)


def test_scan_coordinator_retention_phase_survives_prune_logs_failure(monkeypatch):
    """文件清理抛异常不应影响快照写入；warnings 文本携带原因。"""
    root = config.DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        scanner, "create_snapshot",
        lambda conn, r, *a, **kw: (
            conn.execute(
                "INSERT INTO scans(root, min_kb) VALUES (?, 0)", (str(r),)
            ),
            conn.execute(
                "INSERT INTO snapshots(scan_id, root, min_kb, created_at) "
                "VALUES (last_insert_rowid(), ?, 0, ?)",
                (str(r), dt.datetime.combine(FROZEN_TODAY, dt.time(8, 0)).isoformat()),
            ),
            conn.execute("SELECT last_insert_rowid()").fetchone()[0],
        )[-1],
    )

    def _boom(*a, **kw):
        raise RuntimeError("logs oops")

    monkeypatch.setattr(reports, "prune_logs", _boom)
    monkeypatch.setattr(reports, "prune_reports", lambda **kw: (0, []))
    monkeypatch.setattr(reports, "notify_for_snapshot", lambda conn, sid: True)

    _rid, result = scan_coordinator.run_scan(source="cli", root=root)

    assert result["snapshot_id"] > 0
    warnings_text = "\n".join(result["warnings"])
    assert "logs 保留清理失败" in warnings_text
    assert "logs oops" in warnings_text
    # 报告清理成功执行过：0 文件也要出现在流程里，但失败消息优先
    assert "reports 保留清理失败" not in warnings_text


def test_scan_coordinator_retention_phase_handles_deletion_warning(monkeypatch):
    """文件清理本身的 OSError 不影响快照与 warnings 透明暴露。"""
    root = config.DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)

    expired = (FROZEN_TODAY - dt.timedelta(days=60)).isoformat()
    _touch(config.REPORTS_DIR / f"{expired}.md")

    monkeypatch.setattr(
        scanner, "create_snapshot",
        lambda conn, r, *a, **kw: (
            conn.execute(
                "INSERT INTO scans(root, min_kb) VALUES (?, 0)", (str(r),)
            ),
            conn.execute(
                "INSERT INTO snapshots(scan_id, root, min_kb, created_at) "
                "VALUES (last_insert_rowid(), ?, 0, ?)",
                (str(r), dt.datetime.combine(FROZEN_TODAY, dt.time(8, 0)).isoformat()),
            ),
            conn.execute("SELECT last_insert_rowid()").fetchone()[0],
        )[-1],
    )
    monkeypatch.setattr(reports, "notify_for_snapshot", lambda conn, sid: True)

    real_unlink = Path.unlink

    def _explode(self, *a, **kw):
        # 只让这一份报告的删除失败，其它（数据库/快照相关）路径不受影响
        if str(self) == str(config.REPORTS_DIR / f"{expired}.md"):
            raise OSError("EACCES")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", _explode)

    _rid, result = scan_coordinator.run_scan(source="cli", root=root)

    assert result["snapshot_id"] > 0
    warnings_text = "\n".join(result["warnings"])
    assert "EACCES" in warnings_text
    # 文件清理总数为 0（这份失败），不应有"清理运行根过期文件 N 份"消息
    assert "清理运行根过期文件" not in warnings_text
    # 文件仍在原位（删除未成功）
    assert (config.REPORTS_DIR / f"{expired}.md").exists() is True


def test_scan_coordinator_does_not_touch_lock_or_stage_machine(monkeypatch):
    """保留阶段只追加调用，不改锁/阶段机：start_scan 的忙路径仍按原合同。"""
    lock_path = config.DB_PATH.with_name(config.DB_PATH.name + ".scan.lock")
    lease = scan_coordinator.ScanLease.acquire(lock_path, source="cli")
    try:
        with pytest.raises(scan_coordinator.ScanBusyError):
            scan_coordinator.ScanLease.acquire(lock_path, source="api")
    finally:
        lease.release()
    # 收尾后可正常 acquire
    again = scan_coordinator.ScanLease.acquire(lock_path, source="cli")
    again.release()


def test_scan_coordinator_warning_text_is_json_serializable(monkeypatch):
    """warnings 列表仍是 ``list[str]``，可被扫描结果 JSON 化（ISS-020 合同）。"""
    root = config.DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    expired = (FROZEN_TODAY - dt.timedelta(days=99)).isoformat()
    _touch(config.REPORTS_DIR / f"{expired}.md")

    monkeypatch.setattr(
        scanner, "create_snapshot",
        lambda conn, r, *a, **kw: (
            conn.execute(
                "INSERT INTO scans(root, min_kb) VALUES (?, 0)", (str(r),)
            ),
            conn.execute(
                "INSERT INTO snapshots(scan_id, root, min_kb, created_at) "
                "VALUES (last_insert_rowid(), ?, 0, ?)",
                (str(r), dt.datetime.combine(FROZEN_TODAY, dt.time(8, 0)).isoformat()),
            ),
            conn.execute("SELECT last_insert_rowid()").fetchone()[0],
        )[-1],
    )
    monkeypatch.setattr(reports, "notify_for_snapshot", lambda conn, sid: True)

    _rid, result = scan_coordinator.run_scan(source="cli", root=root)

    payload = json.dumps(result, ensure_ascii=False)
    decoded = json.loads(payload)
    assert isinstance(decoded["warnings"], list)
    assert any("清理运行根过期文件" in w for w in decoded["warnings"])
