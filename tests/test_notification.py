"""扫描完成通知测试（ISS-003）：subprocess 全部 mock，绝不真弹横幅。

覆盖三类验收：通知内容构造（Top1 增长 + 剩余）、低容量阈值分支（告警标题
+ 声音）、通知异常不阻断（osascript 抛错/非零退出时日报照常落盘）。
"""

from __future__ import annotations

import subprocess

import pytest

from fathom import config, db, notify, reports


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """数据库/日报/日志全部指向临时目录，避免污染真实运行时产物。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")


@pytest.fixture
def osascript(monkeypatch):
    """拦截 fathom.notify 里的 subprocess.run：记录调用、绝不真弹。"""
    calls: list[list[str]] = []

    def _fake_run(argv, *args, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(notify.subprocess, "run", _fake_run)
    return calls


def _diff(grown: list[reports.DirChange] | None = None) -> dict:
    return {"grown": grown or [], "shrunk": [], "added": [], "removed": []}


def _insert_snapshot(
    conn, day: str, entries: dict[str, int], free_bytes: int,
    *, denied_count: int = 0, collection_status: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, total_kb,"
        " collection_status) VALUES (?,?,?,?,?,?,?)",
        (f"{day}T12:00:00", "/tmp/x", len(entries), denied_count, 0.0,
         sum(entries.values()), collection_status),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
        [(sid, p, s) for p, s in entries.items()],
    )
    conn.execute(
        "INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) VALUES (?,?,?)",
        (sid, 500 * 1024**3, free_bytes),
    )
    conn.commit()
    return sid


class TestBuildNotification:
    def test_body_contains_top1_and_free(self):
        diff = _diff([reports.DirChange("/Users/x/下载", 1024, 3 * 1024 * 1024 + 1024, 3 * 1024 * 1024)])
        title, body, sound = notify.build_notification(diff, 50 * 1024**3)
        assert title == notify.TITLE_DONE
        assert "/Users/x/下载" in body and "+3.0 GB" in body
        assert "剩余 50.0 GB" in body
        assert sound is None  # 50GB 高于默认阈值 10GB，不带声音

    def test_low_free_alert(self):
        diff = _diff([reports.DirChange("/a", 0, 1024, 1024)])
        title, body, sound = notify.build_notification(diff, 8 * 1024**3)
        assert title == notify.TITLE_ALERT
        assert sound == notify.ALERT_SOUND
        assert "剩余 8.0 GB" in body

    def test_low_free_simulated_by_threshold(self, monkeypatch):
        """验收第 2 条的模拟方式：临时调高阈值让正常剩余也触发告警。"""
        monkeypatch.setattr(config, "FREE_ALERT_GB", 9999)
        title, _, sound = notify.build_notification(_diff(), 50 * 1024**3)
        assert title == notify.TITLE_ALERT and sound == notify.ALERT_SOUND

    def test_no_growth_body(self):
        _, body, _ = notify.build_notification(_diff(), None)
        assert "无" in body and "剩余" not in body


class TestSendNotification:
    def test_osascript_args_and_escaping(self, osascript):
        ok = notify.send_notification('标题"x', '正文 "引号" 与\\反斜杠', "Sosumi")
        assert ok and len(osascript) == 1
        argv = osascript[0]
        assert argv[0] == "/usr/bin/osascript" and argv[1] == "-e"
        assert 'display notification "正文 \\"引号\\" 与\\\\反斜杠"' in argv[2]
        assert 'with title "标题\\"x"' in argv[2]
        assert 'sound name "Sosumi"' in argv[2]

    def test_no_sound_arg_when_none(self, osascript):
        notify.send_notification("t", "b", None)
        assert "sound name" not in osascript[0][2]

    def test_exception_logged_not_raised(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("osascript disappeared")

        monkeypatch.setattr(notify.subprocess, "run", _boom)
        assert notify.send_notification("t", "b") is False
        log = (config.LOGS_DIR / "notify.log").read_text(encoding="utf-8")
        assert "通知发送异常" in log and "osascript disappeared" in log

    def test_nonzero_exit_logged(self, monkeypatch):
        proc = subprocess.CompletedProcess([], 1, "", "not authorized to send notifications")
        monkeypatch.setattr(notify.subprocess, "run", lambda *a, **kw: proc)
        assert notify.send_notification("t", "b") is False
        log = (config.LOGS_DIR / "notify.log").read_text(encoding="utf-8")
        assert "通知被系统拒绝" in log and "not authorized" in log


class TestNotifyScanDone:
    def test_build_failure_swallowed(self, monkeypatch):
        def _boom(diff, free_bytes):
            raise ValueError("bad diff")

        monkeypatch.setattr(notify, "build_notification", _boom)
        assert notify.notify_scan_done(_diff(), 1024) is False  # 不上抛即通过

    def test_writes_success_log(self, osascript):
        notify.notify_scan_done(_diff([reports.DirChange("/a/b", 0, 2048, 2048)]), 20 * 1024**3)
        log = (config.LOGS_DIR / "notify.log").read_text(encoding="utf-8")
        assert "通知命令已提交" in log and "/a/b" in log


class TestWiring:
    """write_daily_report 尾部接线：CLI scan 与 API 手动扫描都经此函数。"""

    _insert_snapshot = staticmethod(_insert_snapshot)

    def test_report_written_and_notification_sent(self, osascript):
        conn = db.connect()
        try:
            self._insert_snapshot(conn, "2026-09-11", {"/tmp/x/a": 100 * 1024, "/tmp/x/b": 50 * 1024}, 200 * 1024**3)
            sid2 = self._insert_snapshot(
                conn, "2026-09-12", {"/tmp/x/a": 300 * 1024, "/tmp/x/b": 50 * 1024}, 200 * 1024**3
            )
            out = reports.write_daily_report(conn, sid2)
        finally:
            conn.close()
        assert out.exists() and out.name == "2026-09-12.md"
        assert len(osascript) == 1
        script = osascript[0][2]
        assert "/tmp/x/a" in script and "+200.0 MB" in script and "剩余 200.0 GB" in script
        assert "sound name" not in script  # 200GB 远高于阈值，普通通知

    def test_low_free_report_gets_sound_alert(self, osascript):
        conn = db.connect()
        try:
            self._insert_snapshot(conn, "2026-09-11", {"/tmp/x/a": 100 * 1024}, 200 * 1024**3)
            sid2 = self._insert_snapshot(conn, "2026-09-12", {"/tmp/x/a": 300 * 1024}, 5 * 1024**3)
            reports.write_daily_report(conn, sid2)
        finally:
            conn.close()
        script = osascript[0][2]
        assert "剩余 5.0 GB" in script and 'sound name "Sosumi"' in script

    def test_notification_failure_does_not_break_report(self, monkeypatch):
        """验收第 3 条：osascript 彻底失败时日报照常生成。"""
        def _boom(*a, **kw):
            raise RuntimeError("no osascript here")

        monkeypatch.setattr(notify.subprocess, "run", _boom)
        conn = db.connect()
        try:
            self._insert_snapshot(conn, "2026-09-11", {"/tmp/x/a": 100 * 1024}, 200 * 1024**3)
            sid2 = self._insert_snapshot(conn, "2026-09-12", {"/tmp/x/a": 300 * 1024}, 200 * 1024**3)
            out = reports.write_daily_report(conn, sid2)
        finally:
            conn.close()
        assert out.exists() and "增长最多的目录" in out.read_text(encoding="utf-8")
        log = (config.LOGS_DIR / "notify.log").read_text(encoding="utf-8")
        assert "通知发送异常" in log


def test_first_observed_directory_is_not_hidden():
    diff = reports.compute_diff({"/tmp/a": 10240},
                                {"/tmp/a": 10240, "/tmp/new": 204800})
    _, body, _ = notify.build_notification(diff, 20 * 1024**3)
    assert "首次记录大目录：/tmp/new（200.0 MB）" in body
    assert "今日无" not in body

def test_summary_bounds_paths_but_keeps_capacity():
    diff = _diff([reports.DirChange("/tmp/" + "很长\n" * 200, 0, 2048, 2048)])
    _, body, _ = notify.build_notification(diff, 5 * 1024**3)
    assert "\n" not in body and len(body) < 240
    assert "剩余 5.0 GB" in body


class TestISS003AFourStateCopy:
    """ISS-003A：首扫/零变化/partial/中断四态文案与长度上限（构造层）。"""

    def test_zero_change_body_says_no_change(self):
        _, body, _ = notify.build_notification(_diff(), None)
        assert "与上次相比无变化" in body

    def test_partial_denied_note(self):
        diff = _diff([reports.DirChange("/a", 0, 2048, 2048)])
        _, body, _ = notify.build_notification(
            diff, 50 * 1024**3, collection_status="partial", denied_count=3
        )
        assert "部分覆盖（3 处权限受限）" in body
        assert "完整" not in body

    def test_partial_transient_note(self):
        # ISS-047：瞬时错误非零也归 partial，但缺口不是权限，不能谎称权限受限。
        _, body, _ = notify.build_notification(
            _diff(), None, collection_status="partial", denied_count=0
        )
        assert "部分覆盖（瞬时读取错误）" in body
        assert "权限受限" not in body

    def test_full_and_legacy_null_have_no_partial_note(self):
        diff = _diff([reports.DirChange("/a", 0, 2048, 2048)])
        for status in ("full", None):  # None = v3 前旧口径，未知不冒充 partial
            _, body, _ = notify.build_notification(
                diff, None, collection_status=status, denied_count=0
            )
            assert "部分覆盖" not in body

    def test_first_snapshot_copy(self):
        title, body, sound = notify.build_first_notification(50 * 1024**3)
        assert title == notify.TITLE_FIRST
        assert "首次快照已建立" in body and "下次扫描起可比较" in body
        # 首扫无基线，不得出现对比类文案。
        assert "无变化" not in body and "无 1MB 以上增长" not in body
        assert "剩余 50.0 GB" in body and sound is None

    def test_first_snapshot_low_free_alerts(self):
        title, _, sound = notify.build_first_notification(5 * 1024**3)
        assert title == notify.TITLE_ALERT and sound == notify.ALERT_SOUND

    def test_interrupted_copy_with_reason(self):
        title, body, sound = notify.build_interrupted_notification("du 超过 14400 秒安全时限")
        assert title == notify.TITLE_INTERRUPTED
        assert "扫描已中断，保留上次快照" in body
        assert "du 超过 14400 秒安全时限" in body
        assert sound is None

    def test_interrupted_copy_without_reason(self):
        title, body, sound = notify.build_interrupted_notification(None)
        assert title == notify.TITLE_INTERRUPTED
        assert body == "扫描已中断，保留上次快照"
        assert sound is None

    def test_interrupted_never_uses_done_title(self):
        for reason in (None, "", "du 超过 0.1 秒安全时限", "扫描在启动前被取消"):
            title, _, _ = notify.build_interrupted_notification(reason)
            assert title == notify.TITLE_INTERRUPTED != notify.TITLE_DONE


class TestISS065VanishedInNotify:
    """ISS-065：通知正文对 vanished 如实呈现（与 denied/transient 并列）。

    vanished 与 denied/transient 并列为部分覆盖的一种，partial_note
    须能容纳三者同时存在的形态，且不冒充完整覆盖也不夸大。零时
    不显示（避免空话）。
    """

    def test_vanished_count_alone_emits_partial_note(self):
        """仅 vanished（无 denied、无 transient）：归 partial 并注明。"""
        _, body, _ = notify.build_notification(
            _diff(), None, collection_status="partial",
            denied_count=0, vanished_count=4,
        )
        assert "部分覆盖（另有 4 个目录在扫描期间已消失）" in body
        assert "权限受限" not in body
        assert "瞬时读取错误" not in body

    def test_vanished_count_zero_omits_vanished_clause(self):
        """vanished=0、denied>0：partial 注只显示权限受限（不混入 vanished）。"""
        _, body, _ = notify.build_notification(
            _diff(), None, collection_status="partial",
            denied_count=3, vanished_count=0,
        )
        assert "部分覆盖（3 处权限受限）" in body
        assert "扫描期间" not in body

    def test_vanished_and_denied_together_listed_separately(self):
        """denied 与 vanished 同时非零：partial 注两句并列展示。"""
        _, body, _ = notify.build_notification(
            _diff(), None, collection_status="partial",
            denied_count=2, vanished_count=5,
        )
        assert "部分覆盖（2 处权限受限；另有 5 个目录在扫描期间已消失）" in body

    def test_first_snapshot_with_vanished_carries_note(self):
        """首扫通知含 vanished_count：与 ISS-003A partial 机制联动。"""
        _, body, _ = notify.build_first_notification(
            50 * 1024**3, collection_status="partial",
            denied_count=0, vanished_count=3,
        )
        assert "首次快照已建立" in body
        assert "部分覆盖（另有 3 个目录在扫描期间已消失）" in body

    def test_full_status_never_includes_vanished_clause(self):
        """full 不加 partial 说明；vanished_count 即使非零也不冒充缺口。"""
        _, body, _ = notify.build_notification(
            _diff(), None, collection_status="full",
            denied_count=0, vanished_count=10,
        )
        assert "部分覆盖" not in body
        assert "扫描期间" not in body


class TestISS003ABodyCap:
    """ISS-003A：最终正文（含剩余空间后缀）≤ 200 字符的可解释截断。"""

    def test_long_body_with_free_suffix_capped_and_suffix_kept(self):
        diff = _diff([reports.DirChange("/tmp/" + "长" * 300, 0, 2048, 2048)])
        _, body, _ = notify.build_notification(diff, 5 * 1024**3)
        assert len(body) <= notify.BODY_MAX_CHARS
        assert body.endswith("剩余 5.0 GB")  # 低空间事实不截断
        assert "…" in body  # 截断有省略号标记

    def test_long_body_without_free_capped(self):
        diff = _diff([reports.DirChange("/tmp/" + "长" * 300, 0, 2048, 2048)])
        _, body, _ = notify.build_notification(diff, None)
        assert len(body) <= notify.BODY_MAX_CHARS and body.endswith("…")

    def test_short_body_not_truncated(self):
        diff = _diff([reports.DirChange("/a", 0, 2048, 2048)])
        _, body, _ = notify.build_notification(diff, 20 * 1024**3)
        assert "…" not in body

    def test_interrupted_reason_truncated(self):
        _, body, _ = notify.build_interrupted_notification("长" * 500)
        assert len(body) <= notify.BODY_MAX_CHARS and body.endswith("…")
        assert "扫描已中断，保留上次快照" in body

    def test_first_snapshot_long_partial_note_capped(self):
        _, body, _ = notify.build_first_notification(
            50 * 1024**3, collection_status="partial", denied_count=10**9
        )
        assert len(body) <= notify.BODY_MAX_CHARS
        assert body.endswith("剩余 50.0 GB")


class TestISS003AWiringStates:
    """ISS-003A：reports 调用点把快照采集状态传进通知（partials 不被吞掉）。"""

    _insert_snapshot = staticmethod(_insert_snapshot)

    def test_write_daily_report_passes_partial_state(self, osascript):
        conn = db.connect()
        try:
            self._insert_snapshot(conn, "2026-09-11", {"/tmp/x/a": 100 * 1024}, 200 * 1024**3)
            sid2 = self._insert_snapshot(
                conn, "2026-09-12", {"/tmp/x/a": 300 * 1024}, 200 * 1024**3,
                denied_count=7, collection_status="partial",
            )
            reports.write_daily_report(conn, sid2)
        finally:
            conn.close()
        script = osascript[0][2]
        assert "部分覆盖（7 处权限受限）" in script

    def test_notify_for_snapshot_passes_partial_state(self, osascript):
        conn = db.connect()
        try:
            self._insert_snapshot(conn, "2026-09-11", {"/tmp/x/a": 100 * 1024}, 200 * 1024**3)
            sid2 = self._insert_snapshot(
                conn, "2026-09-12", {"/tmp/x/a": 100 * 1024}, 200 * 1024**3,
                denied_count=0, collection_status="partial",
            )
            assert reports.notify_for_snapshot(conn, sid2) is True
        finally:
            conn.close()
        assert "部分覆盖（瞬时读取错误）" in osascript[0][2]

    def test_notify_first_snapshot_for_sends_first_copy(self, osascript):
        conn = db.connect()
        try:
            sid = self._insert_snapshot(
                conn, "2026-09-11", {"/tmp/x/a": 100 * 1024}, 200 * 1024**3,
                denied_count=2, collection_status="partial",
            )
            assert reports.notify_first_snapshot_for(conn, sid) is True
        finally:
            conn.close()
        script = osascript[0][2]
        assert notify.TITLE_FIRST in script
        assert "首次快照已建立" in script and "部分覆盖（2 处权限受限）" in script

    def test_notify_first_snapshot_failure_is_silent(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("no osascript")

        monkeypatch.setattr(notify.subprocess, "run", _boom)
        conn = db.connect()
        try:
            sid = self._insert_snapshot(conn, "2026-09-11", {"/tmp/x/a": 1}, 200 * 1024**3)
            assert reports.notify_first_snapshot_for(conn, sid) is False
        finally:
            conn.close()
        log = (config.LOGS_DIR / "notify.log").read_text(encoding="utf-8")
        assert "通知发送异常" in log
