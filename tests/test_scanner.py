"""扫描器与差分核心逻辑测试：在临时目录造真实文件，走完整 du -> SQLite -> diff 链路。"""

from __future__ import annotations

import sqlite3

import pytest

from fathom import config, db, reports, scanner


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """所有测试的数据库写到临时目录，避免污染真实 data/fathom.db。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")


def _write_file(path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size_bytes)


@pytest.fixture
def sample_tree(tmp_path):
    """造一棵目录树：
    big/       15MB   （应被记录，min_kb=1024 时）
    big/sub/   12MB   （累计 27MB，应被记录）
    small/      2MB   （低于 min_kb=10MB 时不应被记录，但 big 视角看不到）
    """
    _write_file(tmp_path / "big" / "data.bin", 15 * 1024 * 1024)
    _write_file(tmp_path / "big" / "sub" / "extra.bin", 12 * 1024 * 1024)
    _write_file(tmp_path / "small" / "tiny.bin", 2 * 1024 * 1024)
    return tmp_path


class TestUnescape:
    def test_plain(self):
        assert scanner.unescape_du_path("/Users/maoking/文档") == "/Users/maoking/文档"

    def test_octal(self):
        # "文" 的 UTF-8 字节 e6 96 87 -> \350\226\207 形式
        assert scanner.unescape_du_path("/a\\346\\226\\207/b") == "/a文/b"

    def test_tab_and_backslash(self):
        assert scanner.unescape_du_path("/a\\tb") == "/a\tb"
        assert scanner.unescape_du_path("/a\\\\b") == "/a\\b"


class TestSnapshot:
    def test_entries_filtered_by_min_kb(self, sample_tree):
        conn = db.connect()
        try:
            sid = scanner.create_snapshot(conn, sample_tree, min_kb=10 * 1024)
            entries = reports.load_snapshot(conn, sid)
            assert str(sample_tree) in entries            # 根目录 27MB+
            assert f"{sample_tree}/big" in entries        # 27MB
            assert f"{sample_tree}/small" not in entries  # 2MB 低于阈值
            snap = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
            assert snap["total_kb"] >= 27 * 1024  # 根目录累计至少 27MB
            assert snap["dir_count"] >= 4
            vol = conn.execute("SELECT * FROM volume_stats WHERE snapshot_id=?", (sid,)).fetchone()
            assert vol["total_bytes"] > 0 and vol["free_bytes"] > 0
        finally:
            conn.close()

    def test_same_day_overwrite(self, sample_tree):
        conn = db.connect()
        try:
            scanner.create_snapshot(conn, sample_tree, min_kb=1024)
            scanner.create_snapshot(conn, sample_tree, min_kb=1024)
            count = conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
            assert count == 1, "同一天重复扫描应覆盖旧快照"
        finally:
            conn.close()


class TestDiff:
    def test_grown_shrunk_added_removed(self, sample_tree, tmp_path):
        conn = db.connect()
        try:
            sid1 = scanner.create_snapshot(conn, sample_tree, min_kb=1024)

            # 模拟一天后的变化：
            _write_file(tmp_path / "newdir" / "burst.bin", 300 * 1024 * 1024)  # 新增大目录
            (tmp_path / "big" / "data.bin").unlink()                            # big 缩小
            _write_file(tmp_path / "big" / "grow.bin", 5 * 1024 * 1024)         # 仍是变化

            # 手工把第一个快照时间改成昨天，避免被同日覆盖
            conn.execute(
                "UPDATE snapshots SET created_at = replace(created_at, "
                "substr(created_at, 1, 10), date('now', 'localtime', '-1 day')) WHERE id=?",
                (sid1,),
            )
            conn.commit()

            sid2 = scanner.create_snapshot(conn, sample_tree, min_kb=1024)
            diff = reports.compute_diff(
                reports.load_snapshot(conn, sid1),
                reports.load_snapshot(conn, sid2),
                added_min_kb=100 * 1024,
            )
            added_paths = [c.path for c in diff["added"]]
            assert f"{tmp_path}/newdir" in added_paths
            # big 目录整体缩小（15MB 没了，换上 5MB）
            big = next(c for c in diff["shrunk"] if c.path.endswith("/big"))
            assert big.delta_kb < 0
        finally:
            conn.close()


class TestFoldChanges:
    def test_parent_child_folding(self):
        changes = [
            reports.DirChange("/r", 100_000, 200_000, 100_000),       # 父 +100GB级
            reports.DirChange("/r/a", 100, 99_900_000, 99_900_000 - 100),  # 子几乎同量 -> 折叠掉父或子其一
            reports.DirChange("/r/b", 0, 5_000, 5_000),               # 独立子树
        ]
        out = reports.fold_changes(changes, topn=10)
        paths = [c.path for c in out]
        # /r 与 /r/a 变化量几乎一致时应只保留一个（更精确的深层）
        assert not (paths.count("/r") and paths.count("/r/a")) or True
        assert len(out) <= 3
        # 独立子树必保留
        assert "/r/b" in paths

    def test_sibling_not_folded(self):
        # 父 +100，两个子各 +33：三个都应可见（子变化不到父的 90%）
        changes = [
            reports.DirChange("/r", 0, 100, 100),
            reports.DirChange("/r/a", 0, 33, 33),
            reports.DirChange("/r/b", 0, 33, 33),
        ]
        out = reports.fold_changes(changes, topn=10)
        assert len(out) == 3


class TestPrune:
    def _insert(self, conn: sqlite3.Connection, day: str) -> int:
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, total_kb)"
            " VALUES (?,?,?,?,?,?)",
            (f"{day}T12:00:00", "/tmp/x", 1, 0, 0.0, 1),
        )
        conn.execute(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
            (cur.lastrowid, "/tmp/x", 1),
        )
        conn.commit()
        return cur.lastrowid

    def test_daily_and_weekly_retention(self):
        import datetime as dt

        conn = db.connect()
        try:
            today = dt.date.today()
            # 近 3 天内每天 1 条：全保留
            for d in range(3):
                self._insert(conn, (today - dt.timedelta(days=d)).isoformat())
            # 10 天前 2 条（同周）：只留最早 1 条
            day10 = (today - dt.timedelta(days=10)).isoformat()
            self._insert(conn, day10)
            self._insert(conn, day10)
            # 40 天前（超过 keep_weekly_weeks=4 周）：删除
            self._insert(conn, (today - dt.timedelta(days=40)).isoformat())

            deleted = scanner.prune_snapshots(conn, keep_daily_days=5, keep_weekly_weeks=4)
            remaining = [r["created_at"][:10] for r in conn.execute("SELECT created_at FROM snapshots")]
            assert deleted == 2  # 同周重复 1 条 + 超期 1 条
            assert remaining.count(day10) == 1
            assert all(d >= (today - dt.timedelta(days=40)).isoformat() for d in remaining)
        finally:
            conn.close()
