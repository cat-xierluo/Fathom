"""API、CLI 与定时入口共用的扫描生命周期和跨进程互斥。"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import threading
import uuid

from . import config, db, reports, scanner


class ScanBusyError(RuntimeError):
    """另一个进程（或其仍运行的 du 子进程）持有扫描租约。"""

    def __init__(self, owner: dict | None = None):
        super().__init__("已有扫描在进行中")
        self.owner = owner or {}


class ScanCancelledError(RuntimeError):
    """本次扫描被自己的调用方取消。"""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class ScanLease:
    """由 flock 保证真实性、JSON 只供诊断的跨进程扫描租约。

    锁 fd 会传给本任务创建的 ``du``。因此 owner 在扫描中崩溃时，锁会由
    仍运行的 du 持有至其退出；新进程只会得到 busy，不会按元数据 PID 猜测
    或杀死任何进程。du 退出后内核释放锁，陈旧 JSON 不具有所有权语义。
    """

    def __init__(self, path: Path, fd: int, owner: dict):
        self.path = path
        self.fd = fd
        self.owner = owner
        self._released = False

    @classmethod
    def acquire(cls, path: Path, *, source: str) -> "ScanLease":
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = cls._read_owner(fd)
            os.close(fd)
            raise ScanBusyError(owner) from exc
        os.set_inheritable(fd, True)
        owner = {
            "owner_id": uuid.uuid4().hex,
            "pid": os.getpid(),
            "started_at": _now(),
            "source": source,
        }
        payload = json.dumps(owner, ensure_ascii=False, sort_keys=True).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)
        return cls(path, fd, owner)

    @staticmethod
    def _read_owner(fd: int) -> dict:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096)
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, ValueError):
            return {}

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


class ScanSession:
    """已取得租约、已持久化 running 状态的一次扫描。"""

    def __init__(self, lease: ScanLease, run_id: int, source: str, root: Path):
        self.lease = lease
        self.run_id = run_id
        self.source = source
        self.root = root
        self.cancel_event = threading.Event()
        self.du_timeout_seconds = 3600.0
        self._finished = False

    def cancel(self) -> None:
        self.cancel_event.set()

    def _update(self, *, phase: str, **values: object) -> None:
        allowed = {
            "snapshot_id", "report_status", "report_path",
            "notification_status", "pruned_count",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"未知扫描状态字段：{sorted(unknown)}")
        assignments = ["phase=?", "heartbeat_at=?"]
        params: list[object] = [phase, _now()]
        for key, value in values.items():
            assignments.append(f"{key}=?")
            params.append(value)
        params.append(self.run_id)
        conn = db.connect()
        try:
            conn.execute(
                f"UPDATE scan_run_details SET {', '.join(assignments)} WHERE run_id=?",
                params,
            )
            conn.commit()
        finally:
            conn.close()

    def _finish(self, status: str, result: dict | str) -> None:
        conn = db.connect()
        try:
            message = (
                json.dumps(result, ensure_ascii=False)
                if isinstance(result, dict) else result
            )
            conn.execute(
                "UPDATE scan_runs SET status=?, message=?, finished_at=? WHERE id=?",
                (status, message, _now(), self.run_id),
            )
            conn.execute(
                "UPDATE scan_run_details SET phase=?, heartbeat_at=? WHERE run_id=?",
                ("completed" if status == "done" else status, _now(), self.run_id),
            )
            conn.commit()
        finally:
            conn.close()
        self._finished = True

    def execute(self) -> dict:
        conn = None
        sid: int | None = None
        try:
            if self.cancel_event.is_set():
                raise ScanCancelledError("扫描在启动前被取消")
            self._update(phase="snapshot")
            conn = db.connect()
            with scanner.du_process_context(
                inherited_fd=self.lease.fd, cancel_event=self.cancel_event,
                timeout_seconds=self.du_timeout_seconds,
            ):
                sid = scanner.create_snapshot(conn, self.root)
            conn.close()
            conn = None
            self._update(phase="report", snapshot_id=sid)

            report_path: Path | None = None
            report_status = "not_available"
            warnings: list[str] = []
            try:
                conn = db.connect()
                report_path = reports.write_daily_report(
                    conn, sid, notify_after_write=False
                )
                report_status = "written"
            except ValueError as exc:
                # 有效首扫没有对比基线，是成功快照而不是运行失败。
                if "至少需要两个快照" in str(exc):
                    warnings.append(str(exc))
                else:
                    report_status = "failed"
                    warnings.append(f"日报生成失败：{exc}")
            except Exception as exc:  # 报告失败不反向撤销已提交快照
                report_status = "failed"
                warnings.append(f"日报生成失败：{exc}")
                if conn is not None:
                    conn.rollback()
            finally:
                if conn is not None:
                    conn.close()
                conn = None
            self._update(
                phase="notification",
                report_status=report_status,
                report_path=str(report_path) if report_path else None,
            )

            notification_status = "not_applicable"
            if report_status == "written":
                try:
                    conn = db.connect()
                    notification_status = (
                        "submitted" if reports.notify_for_snapshot(conn, sid) else "failed"
                    )
                    if notification_status == "failed":
                        warnings.append("系统通知未提交；快照与日报不受影响")
                except Exception as exc:
                    notification_status = "failed"
                    warnings.append(f"系统通知失败：{exc}")
                finally:
                    if conn is not None:
                        conn.close()
                    conn = None
            self._update(phase="retention", notification_status=notification_status)

            pruned = 0
            try:
                conn = db.connect()
                pruned = scanner.prune_snapshots(conn)
            except Exception as exc:
                if conn is not None:
                    conn.rollback()
                warnings.append(f"保留策略执行失败：{exc}")
            finally:
                if conn is not None:
                    conn.close()
                conn = None
            result = {
                "snapshot_id": sid,
                "report": str(report_path) if report_path else None,
                "report_status": report_status,
                "notification_status": notification_status,
                "pruned": pruned,
                "warnings": warnings,
                "source": self.source,
            }
            self._update(phase="finalizing", pruned_count=pruned)
            self._finish("done", result)
            return result
        except (KeyboardInterrupt, ScanCancelledError, scanner.ScanInterruptedError) as exc:
            if conn is not None:
                conn.rollback()
            self._finish("interrupted", str(exc) or "扫描被取消")
            raise
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            self._finish("failed", str(exc))
            raise
        finally:
            if conn is not None:
                conn.close()
            self.lease.release()


def _lock_path() -> Path:
    return config.DB_PATH.with_name(config.DB_PATH.name + ".scan.lock")


def start_scan(*, source: str, root: Path | None = None) -> ScanSession:
    """非阻塞取得全局租约并落 running；busy 时不新建运行记录。"""
    if source not in {"api", "cli", "scheduled"}:
        raise ValueError(f"未知扫描来源：{source}")
    # RuntimeConfig 已负责生产配置规范化；这里保留调用方路径字符串身份，避免
    # /var 与 /private/var 等系统别名导致 du 根记录和 snapshot.root 不一致。
    target_root = Path(root or config.DEFAULT_ROOT).expanduser()
    lease = ScanLease.acquire(_lock_path(), source=source)
    conn = None
    try:
        conn = db.connect()
        now = _now()
        # 能取得 flock 即证明不存在仍活跃的 owner/继承锁 du。此时才收尾遗留行。
        conn.execute(
            "UPDATE scan_runs SET status='interrupted', finished_at=?, message=? "
            "WHERE status='running'",
            (now, "owner 已退出，扫描租约由新进程安全接管"),
        )
        cur = conn.execute(
            "INSERT INTO scan_runs(started_at, status) VALUES (?, 'running')", (now,)
        )
        run_id = int(cur.lastrowid)
        owner = lease.owner
        conn.execute(
            "INSERT INTO scan_run_details("
            "run_id, source, phase, owner_id, owner_pid, owner_started, heartbeat_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, source, "starting", owner["owner_id"], owner["pid"],
             owner["started_at"], now),
        )
        conn.commit()
        return ScanSession(lease, run_id, source, target_root)
    except Exception:
        if conn is not None:
            conn.rollback()
        lease.release()
        raise
    finally:
        if conn is not None:
            conn.close()


def run_scan(*, source: str, root: Path | None = None) -> tuple[int, dict]:
    session = start_scan(source=source, root=root)
    return session.run_id, session.execute()


def latest_scan_state(conn) -> dict:
    row = conn.execute(
        "SELECT r.*, d.source, d.phase, d.snapshot_id, d.report_status, "
        "d.report_path, d.notification_status, d.pruned_count "
        "FROM scan_runs r LEFT JOIN scan_run_details d ON d.run_id=r.id "
        "ORDER BY r.id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"id": None, "status": None, "running": False, "started_at": None,
                "finished_at": None, "result": None, "error": None,
                "source": None, "phase": None}
    result = None
    error = None
    if row["status"] == "done" and row["message"]:
        try:
            result = json.loads(row["message"])
        except ValueError:
            pass
    elif row["status"] in {"failed", "interrupted"}:
        error = row["message"]
    return {
        "id": row["id"], "status": row["status"],
        "running": row["status"] == "running", "started_at": row["started_at"],
        "finished_at": row["finished_at"], "result": result, "error": error,
        "source": row["source"], "phase": row["phase"],
    }
