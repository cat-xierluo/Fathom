"""ISS-048：cli.cmd_report 统一同数据集前驱选择。

反例来源：ISS-021 遗留登记（cli.py cmd_report 仍取全局最近两条，导致两根 /
双阈值场景下报告错配）。本文件钉住 cmd_report 与 API/日报共用
``reports.find_same_dataset_predecessor`` 的同数据集前驱选择，无同数据集前驱时
非零退出并不伪造报告。

全部用合成目录（tmp_path / /synthetic 前缀），显式设置 FATHOM_RUNTIME_DIR 与
FATHOM_SCAN_ROOT 指向测试临时目录，绝不扫描真实 HOME 或写生产库；快照/entries
行直接 INSERT，不经 du。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import sqlite3
import sys

import pytest

from fathom import cli, config, db


@contextlib.contextmanager
def _capture_io():
    """替换 sys.stdout / sys.stderr，捕获 cmd_report 的输出。"""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buf, err_buf
    try:
        yield out_buf, err_buf
    finally:
        sys.stdout, sys.stderr = real_out, real_err


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """FATHOM_RUNTIME_DIR / FATHOM_SCAN_ROOT 显式指向测试临时目录。

    与 tests/test_reports_diff.py 同源：环境变量 + config 兼容常量同时改写到
    同一运行根，保证 cmd_report 的 db/REPORTS_DIR 读写全部留在临时目录。
    monkeypatch 结束后自动还原，不影响其他测试文件。

    额外重建 ``config._ACTIVE``，否则 cli.main 内部 ``config.configure()``
    会保留模块导入时的生产默认，把 monkeypatch 的路径重新覆盖回生产 DB。
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


def _insert_snapshot(
    conn: sqlite3.Connection,
    day: str,
    root: str,
    *,
    min_kb: int | None = None,
    entries: dict[str, int] | None = None,
    denied: int = 0,
    hour: str = "12:00:00",
) -> int:
    """直接造表行（不经 du）；与 tests/test_reports_diff.py 同一口径。"""
    sizes = entries or {}
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status) VALUES (?,?,?,?,?,?,?,?)",
        (f"{day}T{hour}", root, len(sizes) + 1, denied, 0.0,
         max(sizes.values(), default=0), min_kb, None),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
        [(sid, p, s) for p, s in sizes.items()],
    )
    conn.execute(
        "INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) VALUES (?,?,?)",
        (sid, 500 * 1024**3, 200 * 1024**3),
    )
    conn.commit()
    return sid


def _run_report(argv: list[str]) -> tuple[int, str, str]:
    """调用 cli.main 跑 report 子命令并捕获 stdout/stderr。"""
    with _capture_io() as (out, err):
        rc = cli.main(["report", *argv])
    return rc, out.getvalue(), err.getvalue()


class TestSameRootPair:
    """单根同口径两条：cmd_report 默认应找到同数据集前驱并输出日报。"""

    def test_two_snapshots_same_dataset_default_uses_predecessor(self):
        conn = db.connect()
        try:
            root = "/synthetic/root-a"
            old = _insert_snapshot(
                conn, "2026-09-10", root, min_kb=1024,
                entries={f"{root}/x": 5_000},
            )
            new = _insert_snapshot(
                conn, "2026-09-12", root, min_kb=1024,
                entries={f"{root}/x": 8_000},
            )
        finally:
            conn.close()

        rc, out, err = _run_report([])
        assert rc == 0, err
        # a/b 必须显式出现在报头，且都来自同数据集（同根同口径）。
        assert f"#{old} 2026-09-10" in out
        assert f"#{new} 2026-09-12" in out
        assert "- 记录口径：仅入库 ≥1024 KiB 的目录" in out
        # 不混入其他根：默认只看同数据集前驱，不拿全局最近两条。
        assert "/synthetic/root-b" not in out

    def test_explicit_snapshot_id_picks_same_dataset_predecessor(self):
        conn = db.connect()
        try:
            root = "/synthetic/root-a"
            old = _insert_snapshot(
                conn, "2026-09-10", root, min_kb=1024,
                entries={f"{root}/x": 5_000},
            )
            new = _insert_snapshot(
                conn, "2026-09-12", root, min_kb=1024,
                entries={f"{root}/x": 8_000},
            )
        finally:
            conn.close()

        rc, out, err = _run_report(["--snapshot-id", str(new)])
        assert rc == 0, err
        assert f"#{old}" in out and f"#{new}" in out
        assert "（a→b）" in out


class TestTwoRootsNoMismatch:
    """两根混用：默认应取 root-b 最新，没有同数据集前驱则非零退出、不伪造报告。"""

    def test_latest_from_other_root_has_no_predecessor(self):
        conn = db.connect()
        try:
            root_a = "/synthetic/root-a"
            _insert_snapshot(
                conn, "2026-09-10", root_a, min_kb=1024,
                entries={f"{root_a}/x": 5_000},
            )
            _insert_snapshot(
                conn, "2026-09-11", root_a, min_kb=1024,
                entries={f"{root_a}/x": 7_000},
            )
            # root-b 更"新"且只有一个快照：按全局最近两条会错配，
            # cmd_report 必须拒绝并明确说明数据集差异。
            _insert_snapshot(
                conn, "2026-09-13", "/synthetic/root-b", min_kb=1024,
                entries={"/synthetic/root-b": 9_000},
            )
        finally:
            conn.close()

        rc, out, err = _run_report([])
        assert rc != 0
        assert out == ""  # 不输出 Markdown
        # 错误信息必须说清楚：哪个快照、属于哪个数据集、缺同数据集前驱。
        assert "没有同数据集" in err
        assert "/synthetic/root-b" in err
        assert "min_kb=1024" in err
        # 明确提示这是首扫/新根/换阈值等正常情况，不是异常退出。
        assert "无法生成对比日报" in err

    def test_explicit_old_root_snapshot_uses_same_dataset_predecessor(self):
        conn = db.connect()
        try:
            root_a = "/synthetic/root-a"
            old = _insert_snapshot(
                conn, "2026-09-10", root_a, min_kb=1024,
                entries={f"{root_a}/x": 5_000},
            )
            new = _insert_snapshot(
                conn, "2026-09-11", root_a, min_kb=1024,
                entries={f"{root_a}/x": 7_000},
            )
            # root-b 更"新"也不应成为 a；显式指定 b=root-a 的最新。
            _insert_snapshot(
                conn, "2026-09-13", "/synthetic/root-b", min_kb=1024,
                entries={"/synthetic/root-b": 9_000},
            )
        finally:
            conn.close()

        rc, out, err = _run_report(["--snapshot-id", str(new)])
        assert rc == 0, err
        assert f"#{old}" in out and f"#{new}" in out
        # 不混入 root-b 的快照：基线严格走同数据集前驱。
        assert "/synthetic/root-b" not in out


class TestDualThresholdNoMismatch:
    """同根双阈值（= 两个数据集）：跨阈值不回拿他阈值基线。"""

    def test_newer_threshold_lacks_predecessor(self):
        conn = db.connect()
        try:
            root = "/synthetic/root-a"
            # min_kb=1024 数据集两条
            low_old = _insert_snapshot(
                conn, "2026-09-10", root, min_kb=1024,
                entries={f"{root}/x": 5_000},
            )
            _insert_snapshot(
                conn, "2026-09-11", root, min_kb=1024,
                entries={f"{root}/x": 7_000},
            )
            # min_kb=2048 数据集一条更新：默认应选它，无同数据集前驱 → 拒绝。
            _insert_snapshot(
                conn, "2026-09-12", root, min_kb=2048,
                entries={f"{root}/x": 6_000},
            )
        finally:
            conn.close()

        rc, out, err = _run_report([])
        assert rc != 0
        assert out == ""
        assert "没有同数据集" in err
        assert "/synthetic/root-a" in err
        assert "min_kb=2048" in err  # 明确写出是哪一阈值无前驱

        # 验证 low_old 仍能被显式 --snapshot-id 用作 b 的基线：
        # 选 min_kb=1024 数据集中较新的那一条（_insert 按顺序递增 id）。
        rc2, out2, err2 = _run_report(["--snapshot-id", str(low_old + 1)])
        assert rc2 == 0, err2
        assert f"#{low_old}" in out2
        assert "- 记录口径：仅入库 ≥1024 KiB 的目录" in out2
        # 报头里不允许出现 min_kb=2048 的快照 ID。
        assert "min_kb=2048" not in out2


class TestNoSnapshotsAtAll:
    """空库下 cmd_report 必须明确说明，不能借默认两条判断。"""

    def test_empty_db_no_snapshots(self):
        # 不插入任何快照：默认应给出明确文案而非伪造报告。
        rc, out, err = _run_report([])
        assert rc != 0
        assert out == ""
        assert "没有任何快照" in err

    def test_single_snapshot_no_predecessor(self):
        conn = db.connect()
        try:
            _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a/x": 5_000},
            )
        finally:
            conn.close()

        rc, out, err = _run_report([])
        assert rc != 0
        assert out == ""
        assert "没有同数据集" in err


class TestMissingSnapshotId:
    """--snapshot-id 不存在时明确报错，不静默回退到全局最近。"""

    def test_unknown_snapshot_id(self):
        conn = db.connect()
        try:
            _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a/x": 5_000},
            )
            _insert_snapshot(
                conn, "2026-09-13", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a/x": 7_000},
            )
        finally:
            conn.close()

        rc, out, err = _run_report(["--snapshot-id", "9999"])
        assert rc != 0
        assert out == ""
        assert "9999" in err and "不存在" in err


class TestLegacyNullThreshold:
    """v3 之前的旧记录（min_kb NULL）：同根同 NULL 视为同一数据集可比较，
    与已知阈值不可比（ISS-021）。"""

    def test_legacy_null_pair_still_comparable(self):
        conn = db.connect()
        try:
            root = "/synthetic/root-a"
            old = _insert_snapshot(
                conn, "2026-09-10", root,
                entries={f"{root}/x": 5_000},
            )
            new = _insert_snapshot(
                conn, "2026-09-12", root,
                entries={f"{root}/x": 7_000},
            )
        finally:
            conn.close()

        rc, out, err = _run_report([])
        assert rc == 0, err
        assert f"#{old}" in out and f"#{new}" in out
        # 旧记录无阈值元数据：报头不伪造"记录口径"行。
        assert "记录口径" not in out

    def test_legacy_null_never_pairs_with_known_threshold(self):
        conn = db.connect()
        try:
            root = "/synthetic/root-a"
            _insert_snapshot(
                conn, "2026-09-10", root,
                entries={f"{root}/x": 5_000},
            )
            # 已知阈值的快照更新但属另一数据集：无同数据集前驱。
            _insert_snapshot(
                conn, "2026-09-12", root, min_kb=1024,
                entries={f"{root}/x": 7_000},
            )
        finally:
            conn.close()

        rc, out, err = _run_report([])
        assert rc != 0
        assert out == ""
        assert "没有同数据集" in err
        assert "min_kb=1024" in err
