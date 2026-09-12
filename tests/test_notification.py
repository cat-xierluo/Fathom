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

    @staticmethod
    def _insert_snapshot(conn, day: str, entries: dict[str, int], free_bytes: int) -> int:
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, total_kb)"
            " VALUES (?,?,?,?,?,?)",
            (f"{day}T12:00:00", "/tmp/x", len(entries), 0, 0.0, sum(entries.values())),
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
