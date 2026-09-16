"""ISS-021 同口径差分与缺失语义：数据集身份、两根不混、四类缺失案例、
保留 horizon、topn=1 折叠与日报 a/b 标识。

反例来源 docs/plans/2026-09-12-project-review.md：
- AUD-04：entries 缺失被写成"消失"；11 MiB→9 MiB（跌破入库阈值）与真实
  移除无法区分；无基线的 browse 子目录 delta 冒充增长。
- AUD-05：两个根同一 ISO 周各一快照，prune 后仅剩一根；跨根报告错配。
- AUD-12：topn=1 单链折叠提前截断（已由 ISS-042 修复，本文件钉住不回归）。

数据集身份 = (root, min_kb)：更换监控根或阈值形成新数据集，差分/保留/
同日替换都按它分组；v3 之前的旧记录 min_kb 为 NULL，不补造未知元数据。

全部用合成目录（tmp_path / /synthetic 前缀），显式设置 FATHOM_RUNTIME_DIR
与 FATHOM_SCAN_ROOT 指向测试临时目录，绝不扫描真实 HOME 或写生产库
（docs/TESTING.md 环境与隔离）。真实 du 用例只扫 tmp_path 小根。
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db, reports, scanner

MIB_KB = 1024  # 1 MiB = 1024 KiB


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """FATHOM_RUNTIME_DIR / FATHOM_SCAN_ROOT 显式指向测试临时目录。

    环境变量是 helper/CLI/API 的共同入口（config 文档）；同时把 config
    兼容常量指到同一运行根，保证本进程内的 db/reports/api 读写全部留在
    临时目录。 monkeypatch 结束后自动还原，不影响其他测试文件。
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


@pytest.fixture
def client():
    """与真实客户端同一合同（ISS-022）：合法 Host + 写令牌，不走测试旁路。"""
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


def _write_file(path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size_bytes)


def _backdate(conn: sqlite3.Connection, sid: int, day: str) -> None:
    """把快照时间改成指定日期，绕开同日替换（与 test_scanner 同法）。"""
    conn.execute(
        "UPDATE snapshots SET created_at = ? WHERE id = ?", (f"{day}T08:00:00", sid)
    )
    conn.commit()


def _insert_snapshot(
    conn: sqlite3.Connection,
    day: str,
    root: str,
    *,
    min_kb: int | None = None,
    entries: dict[str, int] | None = None,
    denied: int = 0,
    collection_status: str | None = None,
    vanished_count: int = 0,
    hour: str = "12:00:00",
) -> int:
    """直接造表行（不经 du）。min_kb=None 表示 v3 之前的旧记录（NULL 不补造）。"""
    sizes = entries or {}
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status, vanished_count) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"{day}T{hour}", root, len(sizes) + 1, denied, 0.0,
         max(sizes.values(), default=0), min_kb, collection_status, vanished_count),
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


def _same_week_pair(base: dt.date) -> tuple[dt.date, dt.date]:
    """返回同一 ISO 周内的相邻两天（早, 晚），用于周内去重用例。"""
    if (base + dt.timedelta(days=1)).isocalendar()[:2] == base.isocalendar()[:2]:
        return base, base + dt.timedelta(days=1)
    return base - dt.timedelta(days=1), base


class TestIsolationContract:
    def test_env_vars_drive_runtime_config(self, tmp_path):
        """派工要求的隔离合同：显式设置的 FATHOM_* 环境变量真实生效。"""
        rc = config.RuntimeConfig.from_env()
        assert rc.runtime_dir == (tmp_path / "runtime").resolve()
        assert rc.scan_root == (tmp_path / "scanroot").resolve()
        assert rc.db_path == rc.runtime_dir / "data" / "fathom.db"
        assert config.DEFAULT_ROOT == tmp_path / "scanroot"


class TestDatasetMetadata:
    """dataset/阈值/质量元数据持久化与旧库兼容路径。"""

    def test_scan_persists_threshold_and_full_status(self, tmp_path):
        tree = tmp_path / "tree"
        _write_file(tree / "big" / "data.bin", 2 * 1024 * 1024)
        conn = db.connect()
        try:
            sid = scanner.create_snapshot(conn, tree, min_kb=1024)
            row = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
            assert row["min_kb"] == 1024
            assert row["collection_status"] == "full"
        finally:
            conn.close()

    def test_partial_scan_persists_partial_status(self, tmp_path):
        tree = tmp_path / "tree"
        _write_file(tree / "ok" / "f.bin", 2 * 1024 * 1024)
        denied = tree / "secret"
        denied.mkdir()
        os.chmod(denied, 0)
        try:
            conn = db.connect()
            try:
                sid = scanner.create_snapshot(conn, tree, min_kb=1024)
                row = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
                assert row["denied_count"] >= 1
                assert row["collection_status"] == "partial"
            finally:
                conn.close()
        finally:
            os.chmod(denied, 0o755)

    def test_v2_database_migrates_rows_keep_null_metadata(self, tmp_path, monkeypatch):
        """v2→v3 事务迁移：旧行保留，min_kb/collection_status 保持 NULL 不补造。"""
        path = tmp_path / "v2.db"
        legacy = sqlite3.connect(path)
        try:
            for statement in db._SCHEMA_STATEMENTS:
                legacy.execute(statement)
            legacy.execute("PRAGMA user_version=2")
            legacy.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb) "
                "VALUES ('2026-09-01T12:00:00','/synthetic/root-a',1,0,0.1,42)"
            )
            legacy.execute(
                "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (1,'/synthetic/root-a',42)"
            )
            legacy.commit()
        finally:
            legacy.close()
        monkeypatch.setattr(config, "DB_PATH", path)

        conn = db.connect()
        try:
            assert db.schema_version(conn) == db.SCHEMA_VERSION
            row = conn.execute("SELECT * FROM snapshots").fetchone()
            assert row["total_kb"] == 42
            assert row["min_kb"] is None
            assert row["collection_status"] is None
        finally:
            conn.close()
        # 迁移前有一致备份；失败可回退（不删库重建）。
        assert len(list(tmp_path.glob("v2.db.backup-v2-*.sqlite3"))) == 1

    def test_same_day_replacement_scoped_to_dataset(self, tmp_path):
        """同日替换仅针对同数据集有效快照；换阈值形成新数据集，同日共存。"""
        tree = tmp_path / "sameday"
        _write_file(tree / "f.bin", 2 * 1024 * 1024)
        conn = db.connect()
        try:
            scanner.create_snapshot(conn, tree, min_kb=1024)
            scanner.create_snapshot(conn, tree, min_kb=1024)  # 同数据集同日 → 覆盖
            rows = conn.execute(
                "SELECT min_kb FROM snapshots ORDER BY min_kb"
            ).fetchall()
            assert [r["min_kb"] for r in rows] == [1024]

            scanner.create_snapshot(conn, tree, min_kb=2048)  # 新数据集 → 不覆盖
            rows = conn.execute(
                "SELECT min_kb FROM snapshots ORDER BY min_kb"
            ).fetchall()
            assert [r["min_kb"] for r in rows] == [1024, 2048]
        finally:
            conn.close()

    def test_same_day_replacement_keeps_legacy_null_rows(self, tmp_path):
        """旧记录（min_kb NULL）与新口径快照不同数据集，同日不被替换。"""
        tree = tmp_path / "legacy-day"
        _write_file(tree / "f.bin", 2 * 1024 * 1024)
        conn = db.connect()
        try:
            legacy_sid = _insert_snapshot(
                conn, dt.date.today().isoformat(), str(tree),
                entries={str(tree): 500},
            )
            new_sid = scanner.create_snapshot(conn, tree, min_kb=1024)
            ids = [r["id"] for r in conn.execute("SELECT id FROM snapshots ORDER BY id")]
            assert ids == [legacy_sid, new_sid]
        finally:
            conn.close()


class TestTwoRootsNoMismatch:
    """AUD-05：两根混用被拒绝；两根同周历史各保留，报告不会错配。"""

    def _two_dataset_days(self, conn, root="/synthetic/root-a", min_kb=1024):
        first = _insert_snapshot(
            conn, "2026-09-10", root, min_kb=min_kb,
            entries={root: 10_000, f"{root}/x": 5_000},
        )
        second = _insert_snapshot(
            conn, "2026-09-12", root, min_kb=min_kb,
            entries={root: 12_000, f"{root}/x": 7_000},
        )
        return first, second

    def test_api_diff_rejects_cross_root_pair(self, client):
        conn = db.connect()
        try:
            a1, a2 = self._two_dataset_days(conn, root="/synthetic/root-a")
            b1 = _insert_snapshot(
                conn, "2026-09-13", "/synthetic/root-b",
                entries={"/synthetic/root-b": 9_000},
            )
        finally:
            conn.close()

        ok = client.get(f"/api/diff?a={a1}&b={a2}")
        assert ok.status_code == 200
        assert ok.json()["a"]["id"] == a1 and ok.json()["b"]["id"] == a2

        mixed = client.get(f"/api/diff?a={a2}&b={b1}")
        assert mixed.status_code == 400
        assert "数据集" in mixed.json()["detail"]

    def test_api_diff_rejects_cross_threshold_pair(self, client):
        conn = db.connect()
        try:
            low = _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000},
            )
            high = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=2048,
                entries={"/synthetic/root-a": 10_000},
            )
        finally:
            conn.close()
        r = client.get(f"/api/diff?a={low}&b={high}")
        assert r.status_code == 400
        assert "数据集" in r.json()["detail"]

    def test_api_diff_default_stays_within_latest_dataset(self, client):
        """默认对比 = 最新快照与其同数据集前驱；跨根最新不回拿他根基线。"""
        conn = db.connect()
        try:
            self._two_dataset_days(conn, root="/synthetic/root-a")  # 9-10, 9-12
            b1 = _insert_snapshot(
                conn, "2026-09-13", "/synthetic/root-b", min_kb=1024,
                entries={"/synthetic/root-b": 9_000},
            )
        finally:
            conn.close()

        # root-b 只有一个快照：没有同数据集前驱 → 409，而不是借用 root-a。
        alone = client.get("/api/diff")
        assert alone.status_code == 409

        conn = db.connect()
        try:
            b2 = _insert_snapshot(
                conn, "2026-09-14", "/synthetic/root-b", min_kb=1024,
                entries={"/synthetic/root-b": 15_000},
            )
        finally:
            conn.close()

        r = client.get("/api/diff")
        assert r.status_code == 200
        assert r.json()["a"]["id"] == b1 and r.json()["b"]["id"] == b2
        assert r.json()["a"]["root"] == "/synthetic/root-b"

    def test_write_daily_report_compares_same_dataset_only(self):
        conn = db.connect()
        try:
            a1, a2 = self._two_dataset_days(conn, root="/synthetic/root-a")
            b1 = _insert_snapshot(
                conn, "2026-09-13", "/synthetic/root-b", min_kb=1024,
                entries={"/synthetic/root-b": 9_000},
            )
            out = reports.write_daily_report(conn, a2, notify_after_write=False)
            md = out.read_text(encoding="utf-8")
            # 日报对自身 a/b 有明确 ID：基线是 root-a 的前一快照，不是 root-b。
            assert f"#{a1}" in md and f"#{a2}" in md
            assert "/synthetic/root-b" not in md

            # root-b 首扫没有同数据集基线：不是异常，是无可对比。
            with pytest.raises(ValueError, match="至少需要两个快照"):
                reports.write_daily_report(conn, b1, notify_after_write=False)
        finally:
            conn.close()

    def test_prune_keeps_both_roots_same_week_history(self):
        """AUD-05 复现：两根同一 ISO 周各一快照，保留策略按数据集分组后各留一份。"""
        conn = db.connect()
        try:
            today = dt.date.today()
            early, late = _same_week_pair(today - dt.timedelta(days=40))
            _insert_snapshot(
                conn, early.isoformat(), "/synthetic/root-a",
                entries={"/synthetic/root-a": 100},
            )
            _insert_snapshot(
                conn, late.isoformat(), "/synthetic/root-b",
                entries={"/synthetic/root-b": 100},
            )

            deleted = scanner.prune_snapshots(conn)  # 默认 35 天 / 12 周
            remaining = {
                r["root"]
                for r in conn.execute("SELECT root FROM snapshots")
            }
            assert deleted == 0
            assert remaining == {"/synthetic/root-a", "/synthetic/root-b"}
            assert conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"] == 2
        finally:
            conn.close()

    def test_prune_weekly_dedup_within_same_dataset(self):
        conn = db.connect()
        try:
            today = dt.date.today()
            early, late = _same_week_pair(today - dt.timedelta(days=40))
            keep = _insert_snapshot(
                conn, early.isoformat(), "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 100},
            )
            _insert_snapshot(
                conn, late.isoformat(), "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 200},
            )
            deleted = scanner.prune_snapshots(conn)
            ids = [r["id"] for r in conn.execute("SELECT id FROM snapshots")]
            assert deleted == 1
            assert ids == [keep]  # 同数据集同周只留最早一份
        finally:
            conn.close()

    def test_prune_keeps_both_datasets_same_week_same_root(self):
        """同根不同阈值口径 = 两个数据集，同周历史互不挤占。"""
        conn = db.connect()
        try:
            today = dt.date.today()
            early, late = _same_week_pair(today - dt.timedelta(days=40))
            _insert_snapshot(
                conn, early.isoformat(), "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 100},
            )
            _insert_snapshot(
                conn, late.isoformat(), "/synthetic/root-a", min_kb=2048,
                entries={"/synthetic/root-a": 100},
            )
            deleted = scanner.prune_snapshots(conn)
            thresholds = sorted(
                r["min_kb"] for r in conn.execute("SELECT min_kb FROM snapshots")
            )
            assert deleted == 0
            assert thresholds == [1024, 2048]
        finally:
            conn.close()


class TestFourMissingCases:
    """AUD-04 四类案例：11→9 MiB、9→101+ MiB、权限缩小、真实移除。

    用真实 du 扫 tmp_path 小根、生产默认阈值（min_kb=10 MiB）：
    - 11→9 MiB：目录仍在但跌破入库阈值 → 未记录（不是"消失"）。
    - 9→110 MiB：目录增长越过阈值 → 首次记录（不代表文件系统新建）。
    - 权限缩小：子树不可读 → 未记录 + 部分覆盖提示（denied/partial）。
    - 真实移除：目录确实删除 → 未记录；措辞不冒充"已删除"的文件系统事实。
    """

    MIN_KB = 10 * MIB_KB  # 与生产 config.MIN_DIR_KB 相同口径

    def _scan_pair(self, tmp_path, mutate, initial_bytes=11 * 1024 * 1024):
        """扫两次（第一次回退到昨天），中间执行 mutate；返回 (sid1, sid2, conn)。

        initial_bytes 决定基线日载荷：9 MiB（低于 10 MiB 阈值，目录未记录）
        或 11 MiB（已入库）。
        """
        tree = tmp_path / "case"
        _write_file(tree / "payload.bin", initial_bytes)
        conn = db.connect()
        sid1 = scanner.create_snapshot(conn, tree, min_kb=self.MIN_KB)
        _backdate(conn, sid1, (dt.date.today() - dt.timedelta(days=1)).isoformat())
        mutate(tree)
        sid2 = scanner.create_snapshot(conn, tree, min_kb=self.MIN_KB)
        return sid1, sid2, conn

    def _report(self, conn, sid) -> str:
        out = reports.write_daily_report(conn, sid, notify_after_write=False)
        assert out.parent == config.REPORTS_DIR  # 日报只写隔离运行根
        return out.read_text(encoding="utf-8")

    def test_shrink_below_threshold_is_unrecorded(self, tmp_path):
        def shrink(tree):
            _write_file(tree / "payload.bin", 9 * 1024 * 1024)

        sid1, sid2, conn = self._scan_pair(tmp_path, shrink)
        try:
            old = reports.load_snapshot(conn, sid1)
            new = reports.load_snapshot(conn, sid2)
            case = f"{tmp_path}/case"
            assert case in old and case not in new
            diff = reports.compute_diff(old, new)
            removed_paths = [c.path for c in diff["removed"]]
            assert case in removed_paths
            entry = next(c for c in diff["removed"] if c.path == case)
            assert entry.old_kb is not None and 10_000 < entry.old_kb < 12_500
            assert entry.new_kb is None

            md = self._report(conn, sid2)
            assert "## 未记录的目录" in md and case in md
            assert "消失的目录" not in md
            assert "已删除" not in md
            assert "不构成删除证明" in md
        finally:
            conn.close()

    def test_growth_across_threshold_is_first_recorded(self, tmp_path):
        def grow(tree):
            _write_file(tree / "payload.bin", 110 * 1024 * 1024)

        # 基线日 9 MiB：低于 10 MiB 入库阈值，目录在旧快照中本就未记录。
        sid1, sid2, conn = self._scan_pair(tmp_path, grow, initial_bytes=9 * 1024 * 1024)
        try:
            old = reports.load_snapshot(conn, sid1)
            new = reports.load_snapshot(conn, sid2)
            case = f"{tmp_path}/case"
            assert case not in old and case in new
            assert new[case] >= 100 * MIB_KB  # 达到日报"首次记录"列出阈值
            diff = reports.compute_diff(old, new)
            assert case in [c.path for c in diff["added"]]

            md = self._report(conn, sid2)
            assert "## 首次记录的大目录（≥100MB）" in md and case in md
            assert "新出现" not in md
            assert "不代表文件系统新建" in md
        finally:
            conn.close()

    def test_permission_narrowing_is_unrecorded_with_partial_note(self, tmp_path):
        """子树权限缩小（可读 → 000）：部分覆盖快照，子树条目未记录。"""
        tree = tmp_path / "case"
        _write_file(tree / "payload.bin", 11 * 1024 * 1024)
        secret = tree / "secret"
        _write_file(secret / "hidden.bin", 11 * 1024 * 1024)
        conn = db.connect()
        try:
            sid1 = scanner.create_snapshot(conn, tree, min_kb=self.MIN_KB)
            _backdate(conn, sid1, (dt.date.today() - dt.timedelta(days=1)).isoformat())
            os.chmod(secret, 0)
            try:
                sid2 = scanner.create_snapshot(conn, tree, min_kb=self.MIN_KB)
            finally:
                os.chmod(secret, 0o755)

            row = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid2,)).fetchone()
            assert row["collection_status"] == "partial"
            assert row["denied_count"] >= 1

            old = reports.load_snapshot(conn, sid1)
            new = reports.load_snapshot(conn, sid2)
            assert str(secret) in old and str(secret) not in new

            md = self._report(conn, sid2)
            assert "## 未记录的目录" in md and str(secret) in md
            assert "部分覆盖" in md and "权限无法统计" in md
            assert "已删除" not in md
        finally:
            conn.close()

    def test_real_removal_is_unrecorded_not_claimed_deleted(self, tmp_path):
        tree = tmp_path / "case"
        _write_file(tree / "payload.bin", 11 * 1024 * 1024)
        gone = tree / "gone"
        _write_file(gone / "junk.bin", 11 * 1024 * 1024)
        conn = db.connect()
        sid1 = scanner.create_snapshot(conn, tree, min_kb=self.MIN_KB)
        _backdate(conn, sid1, (dt.date.today() - dt.timedelta(days=1)).isoformat())
        shutil.rmtree(gone)
        sid2 = scanner.create_snapshot(conn, tree, min_kb=self.MIN_KB)
        try:
            old = reports.load_snapshot(conn, sid1)
            new = reports.load_snapshot(conn, sid2)
            assert str(gone) in old and str(gone) not in new
            diff = reports.compute_diff(old, new)
            assert str(gone) in [c.path for c in diff["removed"]]

            md = self._report(conn, sid2)
            assert "## 未记录的目录" in md and str(gone) in md
            # 与 11→9 MiB 同一措辞：数据库无法区分移除与跌破阈值，
            # 不冒充文件系统事实。
            assert "不构成删除证明" in md
            assert "已删除" not in md and "消失的目录" not in md
        finally:
            conn.close()


class TestISS065VanishedInReport:
    """ISS-065：日报对 vanished 如实呈现（不进 denied、不冒充完整覆盖）。

    vanished 与 denied/transient 并列为部分覆盖的一种，日报顶部说明行
    须独立显示消失目录数；零时不再显示（避免空话）。日报 ID 与基线
    关系不动，ISS-021 数据集身份约定保持（不变 root/min_kb 口径）。
    """

    def test_vanished_count_in_markdown_explains_scan_coverage(self):
        """vanished_count > 0：日报注明消失目录数与原因，不冒充完整覆盖。"""
        conn = db.connect()
        try:
            a1 = _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000},
            )
            a2 = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 12_000},
                collection_status="partial", vanished_count=7,
            )
            out = reports.write_daily_report(conn, a2, notify_after_write=False)
            md = out.read_text(encoding="utf-8")
            assert "另有 7 个目录在扫描期间已消失" in md
            assert "记录时存在、校验时不在" in md
            assert "本次采集为完整覆盖" not in md
        finally:
            conn.close()

    def test_zero_vanished_count_omits_note(self):
        """vanished_count=0：日报不显示消失目录说明（避免空话）。"""
        conn = db.connect()
        try:
            a1 = _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000},
            )
            a2 = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000},
                collection_status="full",
            )
            out = reports.write_daily_report(conn, a2, notify_after_write=False)
            md = out.read_text(encoding="utf-8")
            assert "扫描期间已消失" not in md
            assert "扫描期间消失" not in md
        finally:
            conn.close()

    def test_vanished_count_zero_still_partial_due_to_denied(self):
        """vanished_count=0 但 denied>0：只显示权限受限说明，不混入 vanished。"""
        conn = db.connect()
        try:
            a1 = _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000},
            )
            a2 = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000},
                collection_status="partial", denied=3,
            )
            out = reports.write_daily_report(conn, a2, notify_after_write=False)
            md = out.read_text(encoding="utf-8")
            assert "3 个目录因权限无法统计" in md
            assert "扫描期间已消失" not in md
        finally:
            conn.close()

    def test_vanished_and_denied_coexist_in_markdown(self):
        """vanished 与 denied 同时非零：日报两条说明独立呈现。"""
        conn = db.connect()
        try:
            a1 = _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000},
            )
            a2 = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000},
                collection_status="partial", denied=2, vanished_count=5,
            )
            out = reports.write_daily_report(conn, a2, notify_after_write=False)
            md = out.read_text(encoding="utf-8")
            assert "2 个目录因权限无法统计" in md
            assert "另有 5 个目录在扫描期间已消失" in md
        finally:
            conn.close()


class TestRetentionHorizon:
    """保留 horizon 与文案一致（ARCHITECTURE：近 35 天每日，更早每周一份，
    weekly_cutoff 从今天向前 12 周，非"35 天加 12 周"）。"""

    def test_horizon_constants_match_documented_policy(self):
        assert config.KEEP_DAILY_DAYS == 35
        assert config.KEEP_WEEKLY_WEEKS == 12

    def test_retention_boundaries_per_dataset(self):
        conn = db.connect()
        try:
            today = dt.date.today()
            daily_day = today - dt.timedelta(days=34)      # 每日保留区内
            week_early, week_late = _same_week_pair(today - dt.timedelta(days=40))
            far_kept = today - dt.timedelta(days=76)       # 周保留区内、独立一周
            far_gone = today - dt.timedelta(days=86)       # 超过 12 周边界
            root = "/synthetic/root-a"
            for day in (daily_day, week_early, week_late, far_kept, far_gone):
                _insert_snapshot(conn, day.isoformat(), root, min_kb=10240,
                                 entries={root: 100}, hour="12:00:00")

            deleted = scanner.prune_snapshots(conn)
            remaining = sorted(
                r["created_at"][:10] for r in conn.execute("SELECT created_at FROM snapshots")
            )
            assert deleted == 2  # 同周较晚一条 + 超过 12 周一条
            assert remaining == sorted([
                daily_day.isoformat(), week_early.isoformat(), far_kept.isoformat(),
            ])
        finally:
            conn.close()


class TestFoldingTopn1:
    """ISS-042 不回归：topn 只截最终输出，单链折叠选最具体贡献者；
    列表逐条呈现，不提供净增量求和。"""

    def test_single_chain_topn1_picks_most_specific(self):
        changes = [
            reports.DirChange("/r", 0, 100_000, 100_000),
            reports.DirChange("/r/a", 0, 99_000, 99_000),
            reports.DirChange("/r/a/deep", 0, 98_000, 98_000),
        ]
        assert [c.path for c in reports.fold_changes(changes, topn=1)] == ["/r/a/deep"]

    def test_independent_sibling_survives_replacement(self):
        changes = [
            reports.DirChange("/r", 0, 100_000, 100_000),
            reports.DirChange("/r/a", 0, 99_000, 99_000),
            reports.DirChange("/r/b", 0, 5_000, 5_000),
        ]
        out = reports.fold_changes(changes, topn=10)
        assert [c.path for c in out] == ["/r/a", "/r/b"]

    def test_diff_lists_carry_no_net_sum(self):
        old = {"/keep": 100 * MIB_KB}
        new = {"/keep": 200 * MIB_KB, "/fresh": 300 * MIB_KB}
        diff = reports.compute_diff(old, new, added_min_kb=100 * MIB_KB)
        # 四个列表逐目录呈现，没有净增量/合计字段（父子累计不可相加）。
        assert set(diff.keys()) == {"grown", "shrunk", "added", "removed"}
        grown = diff["grown"]
        assert len(grown) == 1
        assert grown[0].path == "/keep"
        assert grown[0].delta_kb == 100 * MIB_KB  # 逐条 delta 保留，不求和
        assert [c.path for c in diff["added"]] == ["/fresh"]

    def test_report_has_no_net_sum_wording(self):
        conn = db.connect()
        try:
            _insert_snapshot(conn, "2026-09-10", "/synthetic/root-a", min_kb=1024,
                             entries={"/synthetic/root-a": 10_000})
            sid2 = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000, "/synthetic/root-a/x": 5_000},
            )
            out = reports.write_daily_report(conn, sid2, notify_after_write=False)
            md = out.read_text(encoding="utf-8")
            # 目录累计值不可相加：报告不提供任何净增量/合计措辞
            assert "净" not in md and "合计" not in md
        finally:
            conn.close()


class TestReportAbIdsAndFirstScan:
    """日报对自身有明确 a/b ID；首扫（或新数据集首扫）无报告不是异常。"""

    def test_report_header_carries_ab_snapshot_ids(self):
        conn = db.connect()
        try:
            sid1 = _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a/x": 5_000},
            )
            sid2 = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a/x": 8_000},
            )
            out = reports.write_daily_report(conn, sid2, notify_after_write=False)
            md = out.read_text(encoding="utf-8")
            assert out.name == "2026-09-12.md"
            assert "（a→b）" in md
            assert f"#{sid1} 2026-09-10T12:00:00" in md
            assert f"#{sid2} 2026-09-12T12:00:00" in md
            assert "- 记录口径：仅入库 ≥1024 KiB 的目录" in md
        finally:
            conn.close()

    def test_first_scan_no_report_is_expected_not_available(self):
        """单快照：协调器靠这条消息把报告阶段记为 not_available 而非失败。"""
        conn = db.connect()
        try:
            sid = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a/x": 5_000},
            )
            with pytest.raises(ValueError, match="至少需要两个快照"):
                reports.write_daily_report(conn, sid, notify_after_write=False)
            assert not (config.REPORTS_DIR / "2026-09-12.md").exists()
        finally:
            conn.close()

    def test_first_snapshot_of_new_threshold_has_no_report(self):
        """升级/换阈值后首个新口径快照没有可比基线：同首扫语义。"""
        conn = db.connect()
        try:
            _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a/x": 5_000},
            )
            sid2 = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=2048,
                entries={"/synthetic/root-a/x": 5_000},
            )
            with pytest.raises(ValueError, match="至少需要两个快照"):
                reports.write_daily_report(conn, sid2, notify_after_write=False)
        finally:
            conn.close()

    def test_legacy_null_pair_same_root_still_comparable(self):
        """v3 之前同一根的旧记录彼此仍可比较（兼容路径，不因缺元数据全废）。"""
        conn = db.connect()
        try:
            _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a",
                entries={"/synthetic/root-a/x": 5_000},
            )
            sid2 = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a",
                entries={"/synthetic/root-a/x": 7_000},
            )
            out = reports.write_daily_report(conn, sid2, notify_after_write=False)
            assert "增长最多的目录" in out.read_text(encoding="utf-8")
            # 旧记录无阈值元数据：报头不伪造"记录口径"行。
            assert "记录口径" not in out.read_text(encoding="utf-8")
        finally:
            conn.close()

    def test_legacy_null_never_compares_with_known_threshold(self):
        conn = db.connect()
        try:
            _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a",
                entries={"/synthetic/root-a/x": 5_000},
            )
            sid2 = _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a/x": 5_000},
            )
            with pytest.raises(ValueError, match="至少需要两个快照"):
                reports.write_daily_report(conn, sid2, notify_after_write=False)
        finally:
            conn.close()


class TestISS066ExcludeNamesIdentity:
    """ISS-066：数据集身份从 (root, min_kb) 升级为 (root, min_kb, exclude_names)。

    设计取舍（用户可否决）：排除集变化如实形成新数据集，diff 报「无基线」；
    同排除集可比；旧行（exclude_names=''）与新无配置快照（规范串也是 ''）
    仍同身份——保证默认路径零行为变化。
    """

    def _v5_insert(self, conn, day, root, *, min_kb, exclude_names, entries,
                   collection_status="full"):
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status, "
            "vanished_count, exclude_names) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{day}T12:00:00", root, len(entries) + 1, 0, 0.0,
             max(entries.values(), default=0), min_kb, collection_status, 0,
             exclude_names),
        )
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
            [(sid, p, s) for p, s in entries.items()],
        )
        conn.commit()
        return sid

    def test_different_exclude_names_not_same_dataset(self):
        """同 root / 同 min_kb / 不同 exclude_names 不互为前驱。"""
        conn = db.connect()
        try:
            a1 = self._v5_insert(
                conn, "2026-09-10", "/synthetic/root-a",
                min_kb=1024, exclude_names="",
                entries={"/synthetic/root-a": 5_000},
            )
            a2 = self._v5_insert(
                conn, "2026-09-12", "/synthetic/root-a",
                min_kb=1024, exclude_names="skip.noindex",
                entries={"/synthetic/root-a": 6_000},
            )
            # a2 没有同数据集前驱：find_same_dataset_predecessor 返回 None。
            assert reports.find_same_dataset_predecessor(conn, a2) is None
            # 反之，a1 看到 a2 是另一个数据集：仍不互为前驱。
            assert reports.find_same_dataset_predecessor(conn, a1) is None
        finally:
            conn.close()

    def test_same_exclude_names_comparable(self):
        """同 root / 同 min_kb / 同 exclude_names 可比。"""
        conn = db.connect()
        try:
            a1 = self._v5_insert(
                conn, "2026-09-10", "/synthetic/root-a",
                min_kb=1024, exclude_names="skip.noindex;*.tmp",
                entries={"/synthetic/root-a": 5_000},
            )
            a2 = self._v5_insert(
                conn, "2026-09-12", "/synthetic/root-a",
                min_kb=1024, exclude_names="skip.noindex;*.tmp",
                entries={"/synthetic/root-a": 6_000},
            )
            pred = reports.find_same_dataset_predecessor(conn, a2)
            assert pred is not None and pred["id"] == a1
        finally:
            conn.close()

    def test_default_path_zero_behavior_change_old_empty_vs_new_empty(self):
        """旧行（exclude_names=''）与新无配置快照同身份可比——零行为变化。"""
        conn = db.connect()
        try:
            a1 = self._v5_insert(
                conn, "2026-09-10", "/synthetic/root-a",
                min_kb=1024, exclude_names="",
                entries={"/synthetic/root-a": 5_000},
            )
            a2 = self._v5_insert(
                conn, "2026-09-12", "/synthetic/root-a",
                min_kb=1024, exclude_names="",
                entries={"/synthetic/root-a": 6_000},
            )
            pred = reports.find_same_dataset_predecessor(conn, a2)
            assert pred is not None and pred["id"] == a1
        finally:
            conn.close()

    def test_exclude_names_appears_in_report_header_when_nonzero(self):
        """排除掩码非空时日报头部注明掩码清单（中文提示）。

        两个快照使用相同 exclude_names（同一数据集身份），a2 才有
        前驱可对比，日报能写出。
        """
        conn = db.connect()
        try:
            self._v5_insert(
                conn, "2026-09-10", "/synthetic/root-a",
                min_kb=1024, exclude_names="skip.noindex",
                entries={"/synthetic/root-a": 5_000},
            )
            a2 = self._v5_insert(
                conn, "2026-09-12", "/synthetic/root-a",
                min_kb=1024, exclude_names="skip.noindex",
                entries={"/synthetic/root-a": 6_000},
            )
            out = reports.write_daily_report(conn, a2, notify_after_write=False)
            md = out.read_text(encoding="utf-8")
            assert "排除掩码" in md and "skip.noindex" in md
        finally:
            conn.close()

    def test_exclude_names_absent_in_report_when_empty(self):
        """排除掩码空时日报不显示该行（避免空话）。"""
        conn = db.connect()
        try:
            self._v5_insert(
                conn, "2026-09-10", "/synthetic/root-a",
                min_kb=1024, exclude_names="",
                entries={"/synthetic/root-a": 5_000},
            )
            a2 = self._v5_insert(
                conn, "2026-09-12", "/synthetic/root-a",
                min_kb=1024, exclude_names="",
                entries={"/synthetic/root-a": 6_000},
            )
            out = reports.write_daily_report(conn, a2, notify_after_write=False)
            md = out.read_text(encoding="utf-8")
            assert "排除掩码" not in md
        finally:
            conn.close()


class TestBrowseBaseline:
    """/api/browse 与 diff/日报共用同数据集前驱；无基线不冒充增量（AUD-04）。"""

    def test_browse_without_baseline_reports_null_delta(self, client):
        conn = db.connect()
        try:
            _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000, "/synthetic/root-a/x": 5_000},
            )
        finally:
            conn.close()

        r = client.get("/api/browse")
        assert r.status_code == 200
        body = r.json()
        assert body["delta_kb"] is None  # 无基线：不把当前大小冒充增量
        child = next(c for c in body["children"] if c["name"] == "x")
        assert child["delta_kb"] is None
        assert child["is_new"] is False

    def test_browse_delta_uses_same_dataset_predecessor(self, client):
        conn = db.connect()
        try:
            _insert_snapshot(
                conn, "2026-09-10", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 10_000, "/synthetic/root-a/x": 5_000},
            )
            # 同根不同阈值（另一数据集）更"新"：不得作为 root-a 的基线。
            _insert_snapshot(
                conn, "2026-09-11", "/synthetic/root-a", min_kb=2048,
                entries={"/synthetic/root-a": 1},
            )
            _insert_snapshot(
                conn, "2026-09-12", "/synthetic/root-a", min_kb=1024,
                entries={"/synthetic/root-a": 12_000, "/synthetic/root-a/x": 8_000,
                         "/synthetic/root-a/y": 4_000},
            )
        finally:
            conn.close()

        r = client.get("/api/browse")
        body = r.json()
        assert body["delta_kb"] == 2_000  # 12_000 - 10_000（同数据集 9-10 基线）
        by_name = {c["name"]: c for c in body["children"]}
        assert by_name["x"]["delta_kb"] == 3_000
        assert by_name["y"]["delta_kb"] is None  # 基线未记录：增量不可知
        assert by_name["y"]["is_new"] is True
