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


def _remaining_names(directory: Path) -> set[str]:
    """用集合做断言，避免与目录实际排序耦合；只关心文件名是否还在。"""
    return {p.name for p in directory.iterdir()}


def _seed_snapshot(root: Path, *, created_at: str, min_kb: int = 0) -> int:
    """按 v3 schema 直接塞一份快照；用于让 ``find_same_dataset_predecessor``
    命中，``write_daily_report`` 不再因"至少需要两个快照"抛错。"""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status) "
            "VALUES (?, ?, 0, 0, 0.0, 0, ?, 'full')",
            (created_at, str(root), min_kb),
        )
        sid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?, ?, 0)",
            (sid, str(root)),
        )
        conn.commit()
        return sid
    finally:
        conn.close()


def _stub_create_snapshot(today: dt.date = FROZEN_TODAY):
    """返回 ``scanner.create_snapshot`` 的替身：直接 INSERT 一份 created_at
    为今天 08:00 的快照，跳过真实 du 与磁盘统计；不影响保留阶段逻辑。

    必须显式 ``conn.commit()``：scan_coordinator 在调用 create_snapshot 后
    立刻关闭 conn，未提交的 INSERT 会随 close 回滚，导致 write_daily_report
    看不到新快照（"快照不存在" ValueError，report_status=failed）。
    """

    def _stub(conn, root, *args, **kwargs):
        created_at = dt.datetime.combine(today, dt.time(8, 0)).isoformat()
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status) "
            "VALUES (?, ?, 0, 0, 0.0, 0, 0, 'full')",
            (created_at, str(root)),
        )
        sid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?, ?, 0)",
            (sid, str(root)),
        )
        conn.commit()
        return sid

    return _stub


# ---- prune_reports -----------------------------------------------------


def test_prune_reports_deletes_only_expired_parseable_files():
    """保留策略核心语义：过期且可解析日期 → 删；窗口内 / 不可解析 → 留。"""
    expired_old = _age(40)        # 早于默认 35 天
    expired_boundary = _age(36)   # 边界外 1 天
    recent_inside = _age(7)       # 仍在 35 天窗口内
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
    remaining = _remaining_names(config.REPORTS_DIR)
    # 集合断言不依赖文件系统排序；2026-09-14 与 2026-09-07 都必须在场
    assert remaining == {
        "2026-09-14.md",       # 今天：保留
        "2026-09-07.md",       # 窗口内：保留
        "2026-13-99.md",       # 非法日期：保留
        "README.md",           # 无日期：保留
        "summary-2026.md",     # 月份只有 1 位（regex 不匹配）：保留
    }


def test_prune_reports_uses_configured_retention_days():
    _touch(config.REPORTS_DIR / f"{_age(10).isoformat()}.md")
    _touch(config.REPORTS_DIR / f"{_age(3).isoformat()}.md")

    deleted, warnings = reports.prune_reports(retention_days=7, today=FROZEN_TODAY)

    assert deleted == 1
    assert warnings == []
    assert (config.REPORTS_DIR / f"{_age(10).isoformat()}.md").exists() is False
    assert (config.REPORTS_DIR / f"{_age(3).isoformat()}.md").exists() is True


def test_prune_reports_no_directory_is_noop(tmp_path):
    """全新空目录：deleted=0、warnings 空、不抛。"""
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
    """``prune_reports`` 只读 ``config.REPORTS_DIR``，不会跨目录误伤 logs/。"""
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
    """``prune_logs`` 走相同日期-早于-cutoff 才删的语义；固定名 + 非法日期保留。"""
    expired_old = _age(10)        # 默认 7 天窗口外
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
    assert _remaining_names(config.LOGS_DIR) == {
        "2026-09-14.log",          # 今天：保留
        "2026-09-11.log",          # 窗口内：保留
        "broken-2026-02-30.log",   # 非法日期：保留
        "launchd-scan.err.log",    # 固定名：保留
        "launchd-scan.out.log",    # 固定名：保留
        "notify.log",              # 固定名：保留
        "session-abc123.log",      # 无日期：保留
    }


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
    outside_in_logs = config.LOGS_DIR / "outside.log"
    outside_in_logs.symlink_to(outside)

    try:
        deleted, warnings = reports.prune_logs(today=FROZEN_TODAY)
    finally:
        if outside_in_logs.is_symlink() or outside_in_logs.exists():
            outside_in_logs.unlink()

    assert deleted == 0  # 子目录和 symlink 都未删除
    assert warnings == []
    assert (sub / f"{_age(60).isoformat()}.log").exists() is True


def test_prune_logs_reports_warning_when_unlink_fails(monkeypatch):
    """单文件 unlink 抛 OSError：不抛、不计入删除数、warning 携带原因。"""
    _touch(config.LOGS_DIR / f"{_age(60).isoformat()}.log")

    def _explode(self, *args, **kwargs):
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


def test_scan_coordinator_retention_phase_cleans_files_and_lists_count(monkeypatch):
    """集成：run_scan 的保留阶段清理过期报告与日志，warnings 文本暴露总数。"""
    root = config.DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    # 准备一份同数据集前驱，让 write_daily_report 不再因无基线抛错
    day_before = (FROZEN_TODAY - dt.timedelta(days=1)).isoformat()
    _seed_snapshot(root, created_at=f"{day_before}T08:00:00")

    # 写一份过期报告与一条过期日志，验证保留阶段清理
    expired_report_date = (FROZEN_TODAY - dt.timedelta(days=60)).isoformat()
    expired_log_date = (FROZEN_TODAY - dt.timedelta(days=20)).isoformat()
    _touch(config.REPORTS_DIR / f"{expired_report_date}.md")
    _touch(config.LOGS_DIR / f"{expired_log_date}.log")
    # 保留窗口内的：不动
    inside_report_date = (FROZEN_TODAY - dt.timedelta(days=3)).isoformat()
    _touch(config.REPORTS_DIR / f"{inside_report_date}.md")
    _touch(config.LOGS_DIR / "notify.log")  # 固定名永远保留

    # 短路真实 du：直接 INSERT 一份 created_at=今天 08:00 的快照
    monkeypatch.setattr(scanner, "create_snapshot", _stub_create_snapshot())
    # 通知不阻塞：不真实写 osascript
    monkeypatch.setattr(reports, "notify_for_snapshot", lambda conn, sid: True)

    _run_id, result = scan_coordinator.run_scan(source="cli", root=root)

    assert result["snapshot_id"] > 0
    assert result["report_status"] in {"written", "not_available"}
    warnings_text = "\n".join(result["warnings"])
    assert "清理运行根过期文件" in warnings_text
    assert "2 份" in warnings_text  # 1 报告 + 1 日志
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
            "SELECT pruned_count FROM scan_run_details ORDER BY run_id DESC LIMIT 1"
        ).fetchall()
    finally:
        conn.close()
    # 文件清理不污染快照 pruned_count：原 prune_snapshots 结果保留语义
    assert all(r[0] is None or r[0] == 0 for r in rows)


def test_scan_coordinator_retention_phase_survives_prune_logs_failure(monkeypatch):
    """``prune_logs`` 抛异常不应影响快照写入；warnings 文本携带原因。"""
    root = config.DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(scanner, "create_snapshot", _stub_create_snapshot())

    def _boom(*args, **kwargs):
        raise RuntimeError("logs oops")

    monkeypatch.setattr(reports, "prune_logs", _boom)
    monkeypatch.setattr(reports, "prune_reports", lambda **kwargs: (0, []))
    monkeypatch.setattr(reports, "notify_for_snapshot", lambda conn, sid: True)

    _rid, result = scan_coordinator.run_scan(source="cli", root=root)

    assert result["snapshot_id"] > 0
    warnings_text = "\n".join(result["warnings"])
    assert "logs 保留清理失败" in warnings_text
    assert "logs oops" in warnings_text
    # reports 清理仍成功（0 文件也算跑过），不要污染失败标记
    assert "reports 保留清理失败" not in warnings_text


def test_scan_coordinator_retention_phase_handles_deletion_warning(monkeypatch):
    """单文件 unlink 抛 OSError：保留阶段警告透明，不影响快照。"""
    root = config.DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)

    expired = (FROZEN_TODAY - dt.timedelta(days=60)).isoformat()
    _touch(config.REPORTS_DIR / f"{expired}.md")

    monkeypatch.setattr(scanner, "create_snapshot", _stub_create_snapshot())
    monkeypatch.setattr(reports, "notify_for_snapshot", lambda conn, sid: True)

    real_unlink = Path.unlink
    target_path = config.REPORTS_DIR / f"{expired}.md"

    def _explode(self, *args, **kwargs):
        # 只让这一份过期报告的删除失败，其它（数据库/快照相关）路径不受影响
        if str(self) == str(target_path):
            raise OSError("EACCES")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _explode)

    _rid, result = scan_coordinator.run_scan(source="cli", root=root)

    assert result["snapshot_id"] > 0
    warnings_text = "\n".join(result["warnings"])
    assert "EACCES" in warnings_text
    # 这份失败 → 总数 0 → 不出现"清理运行根过期文件 N 份"汇总
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

    monkeypatch.setattr(scanner, "create_snapshot", _stub_create_snapshot())
    monkeypatch.setattr(reports, "notify_for_snapshot", lambda conn, sid: True)

    _rid, result = scan_coordinator.run_scan(source="cli", root=root)

    payload = json.dumps(result, ensure_ascii=False)
    decoded = json.loads(payload)
    assert isinstance(decoded["warnings"], list)
    assert any("清理运行根过期文件" in w for w in decoded["warnings"])
