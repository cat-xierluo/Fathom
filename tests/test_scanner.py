"""扫描器与差分核心逻辑测试：在临时目录造真实文件，走完整 du -> SQLite -> diff 链路。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

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


class TestBSDDuPaths:
    def test_real_du_round_trips_supported_special_names(self, tmp_path):
        names = [
            r"literal\t",
            "actual\ttab",
            "中文",
            r"octal\123",
            r"utf8-look\346\226\207",
        ]
        for name in names:
            (tmp_path / name).mkdir()

        result = scanner.run_du(tmp_path)

        assert result.exit_code == 0
        assert result.path_error_count == 0
        assert len(result.sizes) == len(names) + 1
        for name in names:
            assert str(tmp_path / name) in result.sizes
        assert str(tmp_path / "literal\t") not in result.sizes
        assert str(tmp_path / "octalS") not in result.sizes

    def test_real_du_newline_name_is_rejected_as_ambiguous(self, tmp_path):
        (tmp_path / "ordinary").mkdir()
        conn = db.connect()
        try:
            sid = scanner.create_snapshot(conn, tmp_path, min_kb=0)
            before = [tuple(row) for row in conn.execute(
                "SELECT id, created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb FROM snapshots ORDER BY id"
            )]
            before_entries = [tuple(row) for row in conn.execute(
                "SELECT snapshot_id, path, size_kb FROM entries ORDER BY path"
            )]

            (tmp_path / "line\nbreak").mkdir()
            result = scanner.run_du(tmp_path)

            assert result.exit_code == 0
            assert result.path_error_count >= 1
            with pytest.raises(scanner.InvalidScanError, match="不可无歧义解析"):
                scanner.classify_collection(result, str(tmp_path))
            with pytest.raises(scanner.InvalidScanError, match="不可无歧义解析"):
                scanner.create_snapshot(conn, tmp_path, min_kb=0)
            assert [tuple(row) for row in conn.execute(
                "SELECT id, created_at, root, dir_count, denied_count, "
                "du_seconds, total_kb FROM snapshots ORDER BY id"
            )] == before
            assert [tuple(row) for row in conn.execute(
                "SELECT snapshot_id, path, size_kb FROM entries ORDER BY path"
            )] == before_entries
            assert conn.execute("SELECT id FROM snapshots").fetchone()[0] == sid
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "stdout, reason",
        [
            (b"4\t/tmp/ok\ncontinuation\n8\t/tmp\n", "记录缺少"),
            (b"4\t/tmp/\xff\n8\t/tmp\n", "UTF-8"),
            (b"4\t/tmp/same\n8\t/tmp/same\n12\t/tmp\n", "重复"),
            (b"4\t/escaped-root\n8\t/tmp\n", "越出"),
        ],
    )
    def test_malformed_or_undecodable_output_is_structured_quality_error(
        self, monkeypatch, stdout, reason
    ):
        fake = scanner.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=b""
        )
        monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: fake)

        result = scanner.run_du(Path("/tmp"))

        assert result.path_error_count >= 1
        assert reason in result.path_error_sample
        with pytest.raises(scanner.InvalidScanError, match="stdout"):
            scanner.classify_collection(result, "/tmp")

    def test_record_shaped_newline_continuation_cannot_inject_file_path(
        self, tmp_path, monkeypatch
    ):
        decoy = tmp_path / "not-a-directory"
        decoy.write_bytes(b"x")
        fake = scanner.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"4\t{decoy}\n8\t{tmp_path}\n".encode(),
            stderr=b"",
        )
        monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: fake)

        result = scanner.run_du(tmp_path)

        assert result.path_error_count == 1
        assert "无法确认为目录" in result.path_error_sample
        with pytest.raises(scanner.InvalidScanError, match="不可无歧义解析"):
            scanner.classify_collection(result, str(tmp_path))


class TestISS066ExcludeNamesArgv:
    """ISS-066：du argv 注入 -I <mask> 来自配置；无配置时 argv 与现状完全一致。

    设计要求（PM 已实测）：``du -I mask`` 按名字匹配并跳过整棵子树；
    两处 du 调用点（库直调 + 协调器入口）都要按当前配置注入 -I <mask>。
    无配置时（默认空）argv 与现状逐项相同——零行为变化证明。
    """

    @staticmethod
    def _capture_du_argv(monkeypatch, *, exclude_names: list[str] | None = None):
        """替换 du 为 fake，捕获 run_du 实际下发的 argv（含 -I）。"""
        captured: dict[str, object] = {}

        def fake_subprocess_run(*args, **kwargs):
            captured["args"] = list(args[0]) if args else []
            return scanner.subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=f"0\t{args[0][-1]}\n".encode(),
                stderr=b"",
            )

        monkeypatch.setattr(scanner.subprocess, "run", fake_subprocess_run)
        if exclude_names is not None:
            monkeypatch.setattr(config, "EXCLUDE_NAMES", exclude_names)
        return captured

    def test_default_argv_has_no_exclude_flags(self, tmp_path, monkeypatch):
        """无配置时（EXCLUDE_NAMES 默认空）argv 与现状一致——零行为变化证明。"""
        captured = self._capture_du_argv(monkeypatch, exclude_names=[])
        (tmp_path / "ok").mkdir()
        scanner.run_du(tmp_path)
        # 必须包含 -xk 与根路径，但不得注入 -I。
        argv = captured["args"]
        assert "-I" not in argv
        assert "-xk" in argv
        assert str(tmp_path) in argv

    def test_exclude_names_injected_as_repeated_I_flag(self, tmp_path, monkeypatch):
        """配置 exclude_names 后 fake du 须收到 -I <每项>。"""
        captured = self._capture_du_argv(
            monkeypatch, exclude_names=["skip.noindex", "*.noindex"]
        )
        (tmp_path / "ok").mkdir()
        scanner.run_du(tmp_path)
        argv = captured["args"]
        # BSD du -I 可重复；每项必须出现紧邻的 -I <mask>。
        i_indices = [i for i, a in enumerate(argv) if a == "-I"]
        assert len(i_indices) == 2
        masks = [argv[i + 1] for i in i_indices]
        assert masks == ["skip.noindex", "*.noindex"]

    def test_exclude_names_canonical_order(self, tmp_path, monkeypatch):
        """config 层保证 EXCLUDE_NAMES 是规范排序的；扫描器按此顺序注入 argv。"""
        captured = self._capture_du_argv(
            monkeypatch, exclude_names=["a", "b", "c"]  # canonical form
        )
        (tmp_path / "ok").mkdir()
        scanner.run_du(tmp_path)
        argv = captured["args"]
        i_indices = [i for i, a in enumerate(argv) if a == "-I"]
        assert [argv[i + 1] for i in i_indices] == ["a", "b", "c"]


class TestISS065VanishedPath:
    """ISS-065：扫描期间消失的目录不使整次采集无效（vanishing path）。

    生产反例（PM 只读证据 2026-09-16 scan_run 3，8 小时 du 跑完）：
    30 个云同步缓存目录在校验时已被系统清理，旧代码把 vanished 与
    解析歧义混为一类（path_error_count），导致整次采集被判 failed
    当日快照丢弃，约 93.7 万行有效事实被 30 行干掉。

    设计要求：根内 + du 输出时存在 + 校验时不在 → vanished 单独
    计数（不进 path_error_count），classification 归 partial，snapshot
    写入并记 vanished_count；根外/无法解析/非目录路径仍 fail-closed。
    """

    def test_vanished_path_is_counted_separately_not_as_path_error(
        self, tmp_path, monkeypatch
    ):
        """红→绿 pin：vanished 不进 path_error_count，分类为 partial。

        真实创建再删除：模拟 du 扫到它时存在、校验时不在。旧代码把
        vanished 与解析歧义混同（path_error_count > 0），整次采集被
        拒；修复后 vanished 单独计数，path_error_count=0 且
        classification=partial，du 给出的 KB 数（测量期事实）保留。
        """
        root = tmp_path / "root"
        root.mkdir()
        still_here = root / "still-here"
        still_here.mkdir()
        vanished = root / "vanished"
        vanished.mkdir()
        vanished_path = str(vanished)
        vanished.rmdir()  # 校验时已被系统清理
        fake = scanner.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                f"4\t{still_here}\n"
                f"4\t{vanished_path}\n"
                f"8\t{root}\n"
            ).encode("utf-8"),
            stderr=b"",
        )
        monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: fake)

        result = scanner.run_du(root)

        assert result.path_error_count == 0, (
            "vanished 与解析歧义必须分离；path_error 只收含换行/根外/非目录"
        )
        assert result.vanished_count == 1
        assert result.vanished_sample == vanished_path
        assert vanished_path in result.sizes  # du 数据是测量期事实，保留
        assert scanner.classify_collection(result, str(root)) == "partial"

    def test_vanished_path_persists_in_snapshot(self, tmp_path, monkeypatch):
        """vanished 的快照仍写入库并如实标注：vanished_count=1、status=partial。"""
        root = tmp_path / "root"
        root.mkdir()
        (root / "ok").mkdir()
        vanished = root / "vanished"
        vanished.mkdir()
        vanished_path = str(vanished)
        vanished.rmdir()
        fake = scanner.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(f"4\t{root}/ok\n4\t{vanished_path}\n8\t{root}\n").encode("utf-8"),
            stderr=b"",
        )
        monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: fake)

        conn = db.connect()
        try:
            sid = scanner.create_snapshot(conn, root, min_kb=0)
            row = conn.execute(
                "SELECT vanished_count, collection_status, dir_count FROM snapshots "
                "WHERE id=?", (sid,)
            ).fetchone()
            assert row["vanished_count"] == 1
            assert row["collection_status"] == "partial"
            assert row["dir_count"] == 3  # 根 + ok + vanished（事实保留）
            # vanished 的条目按 KB 进入 entries（vanished 不删事实）
            assert conn.execute(
                "SELECT size_kb FROM entries WHERE snapshot_id=? AND path=?",
                (sid, vanished_path),
            ).fetchone()["size_kb"] == 4
        finally:
            conn.close()

    def test_path_outside_root_still_fail_closed(self, tmp_path, monkeypatch):
        """ISS-065 不放松根外路径：vanished 仅适用于"根内且曾存在"。"""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        fake = scanner.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"4\t{outside}\n8\t{root}\n".encode(),
            stderr=b"",
        )
        monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: fake)

        result = scanner.run_du(root)
        assert result.path_error_count == 1
        assert result.vanished_count == 0
        with pytest.raises(scanner.InvalidScanError, match="不可无歧义解析"):
            scanner.classify_collection(result, str(root))

    def test_existing_file_path_still_fail_closed(self, tmp_path, monkeypatch):
        """ISS-065 不放松"存在但非目录"：仍是 path_error（注入/解析失败）。"""
        root = tmp_path / "root"
        root.mkdir()
        decoy = root / "not-a-directory"
        decoy.write_bytes(b"x")
        fake = scanner.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"4\t{decoy}\n8\t{root}\n".encode(),
            stderr=b"",
        )
        monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: fake)

        result = scanner.run_du(root)
        assert result.path_error_count == 1
        assert result.vanished_count == 0
        with pytest.raises(scanner.InvalidScanError, match="不可无歧义解析"):
            scanner.classify_collection(result, str(root))


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
    @staticmethod
    def _parent_child_changes():
        """父目录先排序，随后子目录应以更精确路径替换它。"""
        return [
            reports.DirChange("/r", 0, 100_000, 100_000),
            reports.DirChange("/r/a", 0, 99_800, 99_800),
            reports.DirChange("/r/b", 0, 5_000, 5_000),
        ]

    def test_parent_child_folding_respects_topn_after_replacement(self):
        out = reports.fold_changes(self._parent_child_changes(), topn=1)

        assert [c.path for c in out] == ["/r/a"]

    def test_parent_child_folding_preserves_independent_sibling(self):
        out = reports.fold_changes(self._parent_child_changes(), topn=10)

        paths = [c.path for c in out]
        # /r 与 /r/a 变化量几乎一致时应只保留一个（更精确的深层）
        assert "/r" not in paths
        assert "/r/a" in paths
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

    def test_negative_parent_child_replacement_and_zero_ignored(self):
        changes = [
            reports.DirChange("/r", 100_000, 0, -100_000),
            reports.DirChange("/r/a", 99_800, 0, -99_800),
            reports.DirChange("/zero", 1, 1, 0),
        ]

        out = reports.fold_changes(changes, topn=1)

        assert [c.path for c in out] == ["/r/a"]

    def test_topn_bounds_final_output(self):
        changes = [
            reports.DirChange(f"/r-{index}", 0, 100 - index, 100 - index)
            for index in range(20)
        ]

        out = reports.fold_changes(changes, topn=3)

        assert [c.path for c in out] == ["/r-0", "/r-1", "/r-2"]

    def test_nearest_ancestor_is_replaced_in_multilevel_chain(self):
        changes = [
            reports.DirChange("/r", 0, 100_000, 100_000),
            reports.DirChange("/r/a", 0, 80_000, 80_000),
            reports.DirChange("/r/a/deep", 0, 75_000, 75_000),
        ]

        out = reports.fold_changes(changes, topn=10)

        assert [c.path for c in out] == ["/r", "/r/a/deep"]

    def test_replacement_preserves_final_delta_order(self):
        changes = [
            reports.DirChange("/r", 0, 100_000, 100_000),
            reports.DirChange("/other", 0, 99_900, 99_900),
            reports.DirChange("/r/a", 0, 99_800, 99_800),
        ]

        out = reports.fold_changes(changes, topn=2)

        assert [(c.path, c.delta_kb) for c in out] == [
            ("/other", 99_900),
            ("/r/a", 99_800),
        ]

    def test_replacement_does_not_discard_higher_ranked_independent_path(self):
        changes = [
            reports.DirChange("/r", 0, 100_000, 100_000),
            reports.DirChange("/independent", 0, 95_000, 95_000),
            reports.DirChange("/r/a", 0, 91_000, 91_000),
        ]

        out = reports.fold_changes(changes, topn=1)

        assert [c.path for c in out] == ["/independent"]

    def test_descendant_covered_sum_preserves_residual_folding(self):
        changes = [
            reports.DirChange("/r/a", 0, 2_000, 2_000),
            reports.DirChange("/r", 0, 1_900, 1_900),
        ]

        out = reports.fold_changes(changes, topn=10)

        assert [c.path for c in out] == ["/r/a"]

    def test_directory_prefix_does_not_match_similar_name(self):
        changes = [
            reports.DirChange("/r", 0, 100_000, 100_000),
            reports.DirChange("/result", 0, 99_800, 99_800),
        ]

        out = reports.fold_changes(changes, topn=10)

        assert [c.path for c in out] == ["/r", "/result"]

    def test_root_path_can_be_replaced_by_precise_child(self):
        changes = [
            reports.DirChange("/", 0, 100_000, 100_000),
            reports.DirChange("/r", 0, 99_800, 99_800),
        ]

        out = reports.fold_changes(changes, topn=1)

        assert [c.path for c in out] == ["/r"]

    def test_large_input_avoids_pairwise_ancestor_checks(self, monkeypatch):
        original_is_ancestor = reports._is_ancestor
        ancestor_checks = 0

        def counted_is_ancestor(a, b):
            nonlocal ancestor_checks
            ancestor_checks += 1
            return original_is_ancestor(a, b)

        monkeypatch.setattr(reports, "_is_ancestor", counted_is_ancestor)
        changes = [
            reports.DirChange(f"/independent-{index}", 0, 8_000 - index, 8_000 - index)
            for index in range(8_000)
        ]

        out = reports.fold_changes(changes, topn=3)

        assert [c.path for c in out] == [
            "/independent-0",
            "/independent-1",
            "/independent-2",
        ]
        assert ancestor_checks == 0


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


# 睡眠模拟钟（ISS-064）：只在 scanner 命名空间内替换 time 绑定，
# subprocess 轮询内部仍用真实 monotonic，communicate 会照常按时超时
# 返回，让旧实现的反例等待保持有界而不是挂起。
_REAL_TIME = time


class _SleepSimClock:
    """macOS 睡眠模拟：monotonic 冻结（mach_absolute_time 睡眠不前进），
    墙钟照常前进。"""

    @staticmethod
    def monotonic() -> float:
        return 12345.0

    @staticmethod
    def time() -> float:
        return _REAL_TIME.time()


class TestISS064WallClockDeadline:
    """ISS-064：du 安全时限须以墙钟计，超时报文留阻塞路径线索。

    生产反例（PM 只读证据 2026-09-16）：du 阻塞在 WPS 容器内 open() 上
    无输出、机器下午反复 maintenance sleep；旧实现 deadline 只算
    time.monotonic()（睡眠期间不前进），把"14400 秒安全时限"变成
    "14400 清醒秒"，扫描挂起 7h41m 仍 running 并持锁，次日定时扫描被
    ScanBusyError 拒绝。
    """

    @staticmethod
    def _fake_du_popen(monkeypatch, child_pids: list[int], child_code: str) -> None:
        """把 run_du 启动的 du 换成给定 python 子进程（独立进程组）。"""
        real_popen = subprocess.Popen

        def fake_popen(*args, **kwargs):
            proc = real_popen(
                [sys.executable, "-c", child_code],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, pass_fds=kwargs.get("pass_fds", ()),
            )
            child_pids.append(proc.pid)
            return proc

        monkeypatch.setattr(scanner.subprocess, "Popen", fake_popen)

    def test_wall_clock_deadline_fires_when_monotonic_frozen(
        self, tmp_path, monkeypatch
    ):
        """睡眠模拟（墙钟前进、monotonic 冻结）下超时必须触发（红→绿 pin）。

        旧实现 deadline 只算 monotonic：冻结后 remaining 永大于 0，永不
        超时。用 3 秒兜底取消让旧实现以"扫描已取消"失败而不是无限挂起；
        修复后墙钟 0.5 秒即触发"du 超过 0.5 秒安全时限"。
        """
        root = tmp_path / "root"
        root.mkdir()
        fd = os.open(tmp_path / "scan.lock", os.O_CREAT | os.O_RDWR, 0o600)
        cancel = threading.Event()
        cancel_net = threading.Timer(3.0, cancel.set)  # 旧实现的兜底退出
        cancel_net.daemon = True
        cancel_net.start()
        monkeypatch.setattr(scanner, "time", _SleepSimClock)
        child_pids: list[int] = []
        self._fake_du_popen(monkeypatch, child_pids, "import signal; signal.pause()")
        # 本用例只钉时钟语义，lsof 线索路径返回空保持报文确定。
        monkeypatch.setattr(scanner, "_lsof_du_cwd", lambda pid: "", raising=False)
        started = _REAL_TIME.monotonic()
        try:
            with scanner.du_process_context(
                inherited_fd=fd, cancel_event=cancel, timeout_seconds=0.5
            ):
                with pytest.raises(
                    scanner.ScanInterruptedError, match="du 超过 0.5 秒安全时限"
                ):
                    scanner.run_du(root)
        finally:
            cancel_net.cancel()
            os.close(fd)
        # 墙钟 0.5s 触发 + 回收，全程有界；远早于 3s 兜底取消。
        assert _REAL_TIME.monotonic() - started < 2.5
        with pytest.raises(ProcessLookupError):
            os.kill(child_pids[0], 0)

    def test_timeout_message_carries_last_output_path(self, tmp_path, monkeypatch):
        """du 已有输出后阻塞：超时报文须带最后一条输出记录的路径线索。"""
        root = tmp_path / "root"
        root.mkdir()
        fd = os.open(tmp_path / "scan.lock", os.O_CREAT | os.O_RDWR, 0o600)
        cancel = threading.Event()
        child_pids: list[int] = []
        child_code = (
            "import signal, sys\n"
            "sys.stdout.write('4096\\t/synthetic/du-last-output-dir\\n')\n"
            "sys.stdout.flush()\n"
            "signal.pause()\n"
        )
        self._fake_du_popen(monkeypatch, child_pids, child_code)
        lsof_calls: list[int] = []
        monkeypatch.setattr(
            scanner, "_lsof_du_cwd",
            lambda pid: lsof_calls.append(pid) or "", raising=False,
        )
        try:
            with scanner.du_process_context(
                inherited_fd=fd, cancel_event=cancel, timeout_seconds=0.3
            ):
                with pytest.raises(scanner.ScanInterruptedError) as excinfo:
                    scanner.run_du(root)
        finally:
            os.close(fd)
        message = str(excinfo.value)
        assert "du 超过 0.3 秒安全时限" in message
        assert "du 最后输出路径：/synthetic/du-last-output-dir" in message
        # 已有输出线索时不得再花 lsof 的 2 秒预算。
        assert lsof_calls == []
        with pytest.raises(ProcessLookupError):
            os.kill(child_pids[0], 0)

    def test_timeout_message_falls_back_to_lsof_cwd(self, tmp_path, monkeypatch):
        """du 无输出阻塞：报文退回 lsof 只读查询的当前目录线索。"""
        root = tmp_path / "root"
        root.mkdir()
        fd = os.open(tmp_path / "scan.lock", os.O_CREAT | os.O_RDWR, 0o600)
        cancel = threading.Event()
        child_pids: list[int] = []
        self._fake_du_popen(monkeypatch, child_pids, "import signal; signal.pause()")
        fake_lsof = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                b"COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
                b"du 9999 someone cwd DIR 1,7 4 2 /synthetic/wps/container\n"
            ),
        )
        monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: fake_lsof)
        try:
            with scanner.du_process_context(
                inherited_fd=fd, cancel_event=cancel, timeout_seconds=0.3
            ):
                with pytest.raises(scanner.ScanInterruptedError) as excinfo:
                    scanner.run_du(root)
        finally:
            os.close(fd)
        message = str(excinfo.value)
        assert "du 超过 0.3 秒安全时限" in message
        assert "du 当前目录（lsof）：/synthetic/wps/container" in message
        with pytest.raises(ProcessLookupError):
            os.kill(child_pids[0], 0)

    def test_timeout_message_omits_clue_when_lsof_fails(self, tmp_path, monkeypatch):
        """线索不可得（lsof 超时/失败）时整体省略，报文保持基线文案。"""
        root = tmp_path / "root"
        root.mkdir()
        fd = os.open(tmp_path / "scan.lock", os.O_CREAT | os.O_RDWR, 0o600)
        cancel = threading.Event()
        child_pids: list[int] = []
        self._fake_du_popen(monkeypatch, child_pids, "import signal; signal.pause()")

        def lsof_times_out(*args, **kwargs):
            raise subprocess.TimeoutExpired(["/usr/sbin/lsof"], 2)

        monkeypatch.setattr(scanner.subprocess, "run", lsof_times_out)
        try:
            with scanner.du_process_context(
                inherited_fd=fd, cancel_event=cancel, timeout_seconds=0.3
            ):
                with pytest.raises(scanner.ScanInterruptedError) as excinfo:
                    scanner.run_du(root)
        finally:
            os.close(fd)
        # ISS-070：线索全不可得时报文只保留基线 + 进度条数（此处 du 未产出
        # 任何记录，故为 0——正是"卡住不推进"的形态）。
        assert str(excinfo.value) == "du 超过 0.3 秒安全时限；已产出 0 条记录"
        with pytest.raises(ProcessLookupError):
            os.kill(child_pids[0], 0)

    def test_timeout_reaps_sigterm_ignoring_du_via_sigkill(
        self, tmp_path, monkeypatch
    ):
        """du 阻塞在不可中断 syscall（忽略 SIGTERM）时仍须被回收：
        SIGTERM 3 秒宽限后 SIGKILL 生效，全程有界、无孤儿。

        生产反例形态：du 卡在 open$NOCANCEL 上不退出也不响应 TERM——
        回收链必须是 TERM → wait(3s) → KILL → wait，不得挂起等待。
        """
        root = tmp_path / "root"
        root.mkdir()
        fd = os.open(tmp_path / "scan.lock", os.O_CREAT | os.O_RDWR, 0o600)
        cancel = threading.Event()
        child_pids: list[int] = []
        self._fake_du_popen(
            monkeypatch, child_pids,
            "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "signal.pause()",
        )
        # 本用例只钉回收链，线索路径返回空避免真实 lsof 干扰计时。
        monkeypatch.setattr(scanner, "_lsof_du_cwd", lambda pid: "")
        started = _REAL_TIME.monotonic()
        try:
            with scanner.du_process_context(
                inherited_fd=fd, cancel_event=cancel, timeout_seconds=0.3
            ):
                with pytest.raises(
                    scanner.ScanInterruptedError, match="du 超过 0.3 秒安全时限"
                ):
                    scanner.run_du(root)
        finally:
            os.close(fd)
        elapsed = _REAL_TIME.monotonic() - started
        # TERM 被忽略 → 3 秒宽限 → KILL；下界证明确实走了宽限路径，
        # 上界证明回收有界完成（没有无限等待 du 退出）。
        assert 3.0 <= elapsed < 6.0
        with pytest.raises(ProcessLookupError):
            os.kill(child_pids[0], 0)  # 无孤儿：进程组已彻底回收
