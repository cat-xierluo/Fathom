"""ISS-090：扫描进度实时反馈——du 流式计数、状态文件与 live 判活。

覆盖任务卡三类测试：
1. 计数正确性：合成 du 流与真实 du 输出对拍——已扫目录数 == 完整记录数、
   已见累计 == 根累计大小（telescoping 净值结清，绝不逐行相加父子重复）；
   中途计数单调不减。
2. 集成：慢速回放 du 输出的假 du（真实 Popen + 真实 communicate 轮询循环）
   下，扫描期间状态文件的 live 计数被轮询观测到单调增长；run_scan 结束后
   流式终值与 snapshots.dir_count / total_kb 一致，状态文件被清理。
3. 判活与兼容：伪造 stale heartbeat 后 live 视图不再报告进行中；无状态
   文件（旧运行根）/坏文件时 API 正常返回 live=None，schema 零变化。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db, scan_coordinator, scan_progress, scanner


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """全部可写路径（含运行根状态文件）与扫描根指到临时目录。

    与 test_cli_report.py 同源：环境变量 + 兼容常量 + ``config._ACTIVE``
    三处同改，scan_progress.progress_path()（读 get_runtime_config()）
    与 db/api（读模块常量）落在同一隔离运行根。
    """
    runtime_dir = tmp_path / "runtime"
    scan_root = tmp_path / "scanroot"
    scan_root.mkdir(parents=True)
    monkeypatch.delenv("FATHOM_DB", raising=False)
    monkeypatch.setenv("FATHOM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("FATHOM_SCAN_ROOT", str(scan_root))
    monkeypatch.setattr(config, "DATA_DIR", runtime_dir / "data")
    monkeypatch.setattr(config, "DB_PATH", runtime_dir / "data" / "fathom.db")
    monkeypatch.setattr(config, "REPORTS_DIR", runtime_dir / "reports")
    monkeypatch.setattr(config, "LOGS_DIR", runtime_dir / "logs")
    monkeypatch.setattr(config, "DEFAULT_ROOT", scan_root)
    monkeypatch.setattr(config, "_ACTIVE", config.RuntimeConfig.from_env())
    config._publish_compatibility_values(config._ACTIVE)


# 后序 du 流（孩子先于父，KB 与路径来自 /tmp 实测形态）：
# 根 12 = a 8（x 4（y 4）+ b 4）+ c 4。
_ORDERED_DU_LINES = [
    b"4\t/root/a/x/y",
    b"4\t/root/a/x",
    b"4\t/root/a/b",
    b"8\t/root/a",
    b"4\t/root/c",
    b"12\t/root",
]


class TestDuStreamCounter:
    def test_totals_match_root_subtree_and_monotonic(self):
        counter = scan_progress.DuStreamCounter()
        seen_bytes = []
        for line in _ORDERED_DU_LINES:
            size_raw, _, path_raw = line.partition(b"\t")
            counter.feed_record(path_raw.decode(), int(size_raw))
            seen_bytes.append(counter.bytes_seen_kb)
        assert counter.dirs_scanned == 6
        assert seen_bytes == sorted(seen_bytes)  # 单调不减（净值结清）
        counter.flush()
        # 终值闭合：根记录净值 = 12 −（8+4），此前已结清 8，合计 12。
        assert counter.bytes_seen_kb == 12
        assert counter.dirs_scanned == 6

    def test_totals_match_real_du_output(self, tmp_path):
        """与真实 /usr/bin/du -xk 对拍：终值恰为根累计 KB 与行数。"""
        root = tmp_path / "duroot"
        (root / "a" / "x" / "y").mkdir(parents=True)
        (root / "a" / "b").mkdir(parents=True)
        (root / "c").mkdir(parents=True)
        for i, sub in enumerate(("a/x/y", "a/b", "c")):
            (root / sub / f"f{i}").write_bytes(b"x" * 4096)
        completed = subprocess.run(
            ["/usr/bin/du", "-xk", str(root)], capture_output=True, check=True
        )
        lines = [ln for ln in completed.stdout.split(b"\n") if ln]
        root_line = next(ln for ln in lines if ln.endswith(str(root).encode()))
        root_kb = int(root_line.partition(b"\t")[0])

        counter = scan_progress.DuStreamCounter()
        snapshots = []
        for ln in lines:
            size_raw, _, path_raw = ln.partition(b"\t")
            counter.feed_record(path_raw.decode(), int(size_raw))
            snapshots.append(counter.bytes_seen_kb)
        counter.flush()
        assert counter.dirs_scanned == len(lines)
        assert counter.bytes_seen_kb == root_kb
        assert snapshots == sorted(snapshots)

    def test_skipped_subtree_still_totals_root(self):
        """du -I 掩码跳过子树（孩子记录缺失）：缺口归入父净值，总量仍闭合。"""
        counter = scan_progress.DuStreamCounter()
        for line in (b"8\t/root/a", b"4\t/root/c", b"12\t/root"):
            size_raw, _, path_raw = line.partition(b"\t")
            counter.feed_record(path_raw.decode(), int(size_raw))
        counter.flush()
        assert counter.bytes_seen_kb == 12  # a 的 8 全部按净值归 a
        assert counter.dirs_scanned == 3


class _FakeClock:
    """手动推进的 monotonic 时钟（节流断言确定性）。"""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _chunked(blob: bytes, size: int) -> list[bytes]:
    """把累计快照序列按 size 切成「增量前缀」口径（run_du 每次给全量累计）。"""
    return [blob[: i + size] for i in range(0, len(blob), size)]


class TestProgressReporter:
    def _payload(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_start_writes_initial_payload(self, tmp_path):
        path = tmp_path / "scan-progress.json"
        reporter = scan_progress.ProgressReporter(run_id=7, path=path)
        reporter.start()
        data = self._payload(path)
        assert data["run_id"] == 7
        assert data["dirs_scanned"] == 0
        assert data["bytes_seen_kb"] == 0
        assert data["phase"] == "du"
        assert data["schema"] == 1
        assert path.stat().st_mode & 0o777 == 0o600
        assert list(tmp_path.glob("scan-progress.json.tmp-*")) == []

    def test_feed_chunk_throttled_by_interval(self, tmp_path):
        clock = _FakeClock()
        path = tmp_path / "scan-progress.json"
        reporter = scan_progress.ProgressReporter(
            run_id=1, path=path, interval_s=2.0, clock=clock
        )
        reporter.start()
        first_heartbeat = self._payload(path)["heartbeat_epoch_s"]

        blob = b"".join(line + b"\n" for line in _ORDERED_DU_LINES[:3])
        # 时钟未推进：节流期内多次喂入只应零次额外写盘。
        for snap in _chunked(blob, 8):
            reporter.feed_chunk(snap)
        assert self._payload(path)["dirs_scanned"] == 0

        clock.now += 2.5
        reporter.feed_chunk(blob)  # 时钟越过 interval：写盘一次
        data = self._payload(path)
        assert data["dirs_scanned"] == 3
        assert data["bytes_seen_kb"] == 4  # y 与 x 已结清，b 尚未
        assert data["heartbeat_epoch_s"] >= first_heartbeat

    def test_heartbeat_refreshes_without_new_data(self, tmp_path):
        """du 卡住（无新输出）时心跳仍按时间驱动刷新——证明进程活着。"""
        clock = _FakeClock()
        path = tmp_path / "scan-progress.json"
        reporter = scan_progress.ProgressReporter(
            run_id=1, path=path, interval_s=2.0, clock=clock
        )
        reporter.start()
        blob = b"4\t/root/a\n"
        reporter.feed_chunk(blob)   # 计数进内存（节流期内不写盘）
        clock.now += 3.0
        reporter.feed_chunk(blob)   # 写盘 #1：dirs=1 落盘
        before = self._payload(path)
        time.sleep(0.01)  # heartbeat 用墙钟，跨毫秒保证可分辨
        clock.now += 3.0
        reporter.feed_chunk(blob)  # 同一累计快照，无新字节
        after = self._payload(path)
        assert after["dirs_scanned"] == before["dirs_scanned"] == 1
        assert after["heartbeat_epoch_s"] > before["heartbeat_epoch_s"]

    def test_feed_chunk_partial_lines_and_finish_consistent(self, tmp_path):
        """残片跨块 + finish(完整 stdout) 与一次性全量喂入计数一致。"""
        path_a = tmp_path / "a.json"
        path_b = tmp_path / "b.json"
        reporter_a = scan_progress.ProgressReporter(run_id=1, path=path_a)
        reporter_b = scan_progress.ProgressReporter(run_id=2, path=path_b)
        full = b"".join(line + b"\n" for line in _ORDERED_DU_LINES)

        reporter_a.start()
        seen = []
        # 按 5 字节步进喂累计快照（制造行残片跨快照），记录每次写盘计数。
        for snap in _chunked(full, 5):
            reporter_a.feed_chunk(snap)
            seen.append(reporter_a.dirs_scanned)
        reporter_a.finish(full)

        reporter_b.start()
        reporter_b.feed_chunk(full)
        reporter_b.finish(full)

        assert seen == sorted(seen)  # 中途目录数单调不减
        assert reporter_a.dirs_scanned == reporter_b.dirs_scanned == 6
        assert reporter_a.bytes_seen_kb == reporter_b.bytes_seen_kb == 12

    def test_close_removes_file_idempotent(self, tmp_path):
        path = tmp_path / "scan-progress.json"
        reporter = scan_progress.ProgressReporter(run_id=1, path=path)
        reporter.start()
        assert path.exists()
        reporter.close()
        reporter.close()  # 幂等
        assert not path.exists()
        # close 后再喂数据不复活文件。
        reporter.feed_chunk(b"4\t/root\n")
        reporter.finish(b"4\t/root\n")
        assert not path.exists()

    def test_write_failure_swallowed(self, tmp_path):
        """进度通道故障绝不影响扫描本体：写失败只丢进度，不抛错。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        reporter = scan_progress.ProgressReporter(
            run_id=1, path=blocker / "runtime" / "scan-progress.json"
        )
        reporter.start()  # parent.mkdir 失败 → 自吞
        reporter.feed_chunk(b"4\t/root\n")
        reporter.finish(b"4\t/root\n")
        reporter.close()
        assert blocker.read_text() == "not a dir"


class TestLiveProgress:
    def test_missing_file_returns_none(self):
        assert scan_progress.live_progress() is None

    def _write_raw_progress(self, heartbeat_epoch: float, **overrides) -> None:
        """直接手写状态文件（伪造任意时态的扫描进度）。"""
        path = scan_progress.progress_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1, "run_id": 9, "phase": "du",
            "started_epoch_s": heartbeat_epoch - 60,
            "dirs_scanned": 12345, "bytes_seen_kb": 6789,
            "elapsed_s": 60.0, "heartbeat_epoch_s": heartbeat_epoch,
        }
        payload.update(overrides)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_fresh_file_active_with_fields(self):
        reporter = scan_progress.ProgressReporter(run_id=9, interval_s=0.0)
        reporter.start()
        reporter.feed_chunk(b"4\t/root/a\n8\t/root\n")
        try:
            live = scan_progress.live_progress()
        finally:
            reporter.close()
        assert live is not None
        assert live["active"] is True
        assert live["run_id"] == 9
        assert live["dirs_scanned"] == 2
        assert live["bytes_seen_kb"] == 4  # 根未 flush，只结清 a
        assert live["elapsed_s"] >= 0

    def test_stale_heartbeat_returns_none(self):
        """任务卡判活合同：伪造 stale heartbeat → 不再报告进行中。"""
        self._write_raw_progress(
            time.time() - (scan_progress.STALE_AFTER_S + 5)
        )
        assert scan_progress.live_progress() is None

    def test_stale_boundary_is_inclusive(self):
        now = time.time()
        self._write_raw_progress(now - (scan_progress.STALE_AFTER_S - 1))
        assert scan_progress.live_progress(now_epoch=now) is not None
        self._write_raw_progress(now - (scan_progress.STALE_AFTER_S + 1))
        assert scan_progress.live_progress(now_epoch=now) is None

    def test_corrupt_file_returns_none(self):
        path = scan_progress.progress_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        assert scan_progress.live_progress() is None

    def test_bad_types_return_none(self):
        path = scan_progress.progress_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "run_id": "x", "dirs_scanned": [], "bytes_seen_kb": {},
            "heartbeat_epoch_s": time.time(), "started_epoch_s": time.time(),
            "elapsed_s": 0,
        }), encoding="utf-8")
        assert scan_progress.live_progress() is None


# ---------- 集成：真实 Popen + 真实 communicate 轮询 ----------

def _slow_replay_argv(root: Path, *, lines_per_sleep: int, sleep_s: float) -> list[str]:
    """构造「先取真实 du 输出、再逐行慢速回放」的假 du argv。

    输出与真实 du 完全同格式（后序、累计 KB），du 行为差异只有速度——
    run_du 的 Popen/communicate 轮询/进度喂入全链路真实执行。
    """
    script = (
        "import subprocess, sys, time\n"
        "root = sys.argv[1]\n"
        "n = int(sys.argv[2]); delay = float(sys.argv[3])\n"
        "out = subprocess.run(['/usr/bin/du', '-xk', root],"
        " capture_output=True, check=True).stdout\n"
        "lines = out.splitlines(keepends=True)\n"
        "for i, line in enumerate(lines):\n"
        "    sys.stdout.write(line.decode() if isinstance(line, bytes) else line)\n"
        "    sys.stdout.flush()\n"
        "    if (i + 1) % n == 0:\n"
        "        time.sleep(delay)\n"
    )
    return [sys.executable, "-c", script, str(root), str(lines_per_sleep), str(sleep_s)]


def _make_tree(root: Path, top_dirs: int = 40, depth: int = 2) -> int:
    """合成根：top_dirs × 嵌套子目录（共数百目录），返回目录总数（含根）。"""
    root.mkdir(parents=True)
    count = 0
    for i in range(top_dirs):
        node = root
        for d in range(depth):
            node = node / f"d{i}_l{d}"
            node.mkdir()
            (node / "payload.bin").write_bytes(b"z" * 512)
            count += 1
    return count + 1


class TestRunDuStreamsProgress:
    def test_live_counts_grow_during_du_and_close_on_total(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "duroot"
        total_dirs = _make_tree(root, top_dirs=40, depth=2)
        fd = os.open(tmp_path / "scan.lock", os.O_CREAT | os.O_RDWR, 0o600)
        # 缩短节流：du 秒级窗口内多次中途写盘，watcher 线程得以观测增长序列。
        monkeypatch.setattr(scan_progress, "WRITE_INTERVAL_S", 0.1)
        monkeypatch.setattr(
            scanner, "_du_argv",
            lambda r: _slow_replay_argv(root, lines_per_sleep=5, sleep_s=0.05),
        )
        reporter = scan_progress.ProgressReporter(run_id=1)
        observed: list[int] = []
        try:
            reporter.start()
            stop = threading.Event()

            def _watch() -> None:
                while not stop.is_set():
                    data = scan_progress.read_progress()
                    if data is not None:
                        observed.append(int(data["dirs_scanned"]))
                    time.sleep(0.02)

            watcher = threading.Thread(target=_watch, daemon=True)
            watcher.start()
            with scanner.du_process_context(
                inherited_fd=fd, cancel_event=threading.Event(),
                timeout_seconds=60, progress=reporter,
            ):
                result = scanner.run_du(root)
            stop.set()
            watcher.join(timeout=2)
        finally:
            os.close(fd)
            reporter.close()

        assert len(result.sizes) == total_dirs
        assert reporter.dirs_scanned == total_dirs  # 流式终值 == 完整记录数
        root_kb = result.sizes[str(root)]
        assert reporter.bytes_seen_kb == root_kb     # 终值闭合 == 根累计
        # 中途观测：单调不减，且确实观测到 du 进行中的非零计数（而非只有
        # start 的 0 初值）。
        assert observed == sorted(observed)
        assert observed and max(observed) > 0


class TestRunScanIntegration:
    def test_run_scan_live_progress_then_cleanup_and_final_matches_snapshot(
        self, tmp_path, monkeypatch
    ):
        """任务卡集成合同：扫描期间 live 单调增长；结束后终值与 dir_count/
        total_kb 一致；状态文件清理；live 视图归 None。"""
        root = tmp_path / "scanroot2"
        total_dirs = _make_tree(root, top_dirs=50, depth=2)
        monkeypatch.setattr(scanner, "_du_argv",
                            lambda r: _slow_replay_argv(r, lines_per_sleep=4,
                                                        sleep_s=0.04))
        # 缩短写节流：秒级 du 窗口内也要有多次中途写盘可观测。
        monkeypatch.setattr(scan_progress, "WRITE_INTERVAL_S", 0.3)

        close_payloads: list[dict] = []
        original_close = scan_progress.ProgressReporter.close

        def _spy_close(self):
            data = scan_progress.read_progress(self._path)
            if data is not None:
                close_payloads.append(data)
            original_close(self)

        monkeypatch.setattr(scan_progress.ProgressReporter, "close", _spy_close)

        observed: list[int] = []
        stop = threading.Event()

        def _watch() -> None:
            while not stop.is_set():
                live = scan_progress.live_progress()
                if live is not None:
                    observed.append(live["dirs_scanned"])
                time.sleep(0.02)

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
        try:
            run_id, result = scan_coordinator.run_scan(source="cli", root=root)
        finally:
            stop.set()
            watcher.join(timeout=2)

        assert observed == sorted(observed)
        assert max(observed or [0]) > 0
        # 扫描结束：状态文件被清理，live 视图不再报告进行中。
        assert not scan_progress.progress_path().exists()
        assert scan_progress.live_progress() is None
        # close 时捕获的流式终值与落库快照事实一致。
        assert close_payloads, "close 前应能读到终值文件"
        final = close_payloads[-1]
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT dir_count, total_kb FROM snapshots WHERE id=?",
                (result["snapshot_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert row["dir_count"] == total_dirs
        assert final["dirs_scanned"] == row["dir_count"]
        assert final["bytes_seen_kb"] == row["total_kb"]


class TestApiLiveStatus:
    @pytest.fixture
    def client(self):
        with TestClient(
            api.app, base_url=f"http://127.0.0.1:{config.PORT}"
        ) as c:
            yield c

    def test_status_includes_live_when_fresh(self, client):
        reporter = scan_progress.ProgressReporter(run_id=3, interval_s=0.0)
        reporter.start()
        reporter.feed_chunk(b"4\t/root/a\n8\t/root\n")
        try:
            payload = client.get("/api/status").json()
        finally:
            reporter.close()
        live = payload["scan"]["live"]
        assert live is not None and live["active"] is True
        assert live["dirs_scanned"] == 2
        assert live["run_id"] == 3
        # 既有字段不受新通道影响。
        assert payload["scan"]["running"] is False

    def test_status_live_none_when_stale_heartbeat(self, client):
        """伪造 stale heartbeat：API 不再报告进行中（回退由前端完成）。"""
        path = scan_progress.progress_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": 1, "run_id": 1, "phase": "du",
            "started_epoch_s": time.time() - 999,
            "dirs_scanned": 42, "bytes_seen_kb": 4096,
            "elapsed_s": 999.0,
            "heartbeat_epoch_s": time.time() - (scan_progress.STALE_AFTER_S + 60),
        }), encoding="utf-8")
        payload = client.get("/api/status").json()
        assert payload["scan"]["live"] is None

    def test_status_live_none_without_file_and_schema_unchanged(self, client):
        """旧运行根兼容：无状态文件时 API 正常，schema 仍为当前版本。"""
        payload = client.get("/api/status").json()
        assert payload["scan"]["live"] is None
        assert payload["runtime"]["schema_version"] == db.SCHEMA_VERSION
        # 状态文件不是 schema 的一部分：全新库照常建表（v5 迁移链零变化）。
        assert db.schema_version(db.connect()) == db.SCHEMA_VERSION

    def test_scan_status_endpoint_also_carries_live(self, client):
        reporter = scan_progress.ProgressReporter(run_id=5)
        reporter.start()
        try:
            payload = client.get("/api/scan/status").json()
        finally:
            reporter.close()
        assert payload["live"] is not None and payload["live"]["run_id"] == 5
