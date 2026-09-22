"""ISS-030A 升级协调协议夹具：fake 环境、六步协议与失败注入。

本模块是**测试夹具**，不是生产代码（ISS-030A 范围：不写壳/助手生产代码；
协议缺陷若实测暴露，登记给父卡 ISS-030，不在本切片硬修）。它把父卡
ISS-030 验收框 1/2/3 的「更新前停写 → 旧 helper 退出 → SQLite 一致备份
→ 替换 → 新 helper 启动 → 版本握手 → 失败回滚」协议固化为可重复的
隔离自动验（076 清单 §6-F1）。

设计与实施边界（与任务卡 ISS-030A 一致）：

- **全程 fake 注入**：tmp 目录模拟运行根（ISS-025 形态：runtime/data、
  reports、logs）与 N/N+1 两版 app/helper 目录形态（Info.plist 版本文件、
  假 helper 二进制标记）、假 DB（真实 sqlite3 + WAL）、假
  helper-instance.json（0600，helper.rs 语义：pid/port/service/
  protocol_version/version）、假 LaunchAgents plist（复用 ``launchd``
  同源发射器，010B 合同形态）。假进程用标记文件表示——绝不 spawn 真实
  helper/GUI，不读写生产库/生产 PID/真实 ``~/Library``。
- **停写**直接复用 ``scan_coordinator.ScanLease`` 的真实 flock（ISS-020
  合同：持锁期间新写入会话被拒），不在夹具里重实现互斥语义。
- **SQLite 一致备份**用真实连接：``PRAGMA wal_checkpoint(TRUNCATE)`` +
  ``sqlite3`` backup API（非文件拷贝），并校验备份可独立打开且含全部
  已提交数据行。
- **schema 拒绝**引用 ``fathom.db`` 既有 ``SCHEMA_VERSION`` 常量与迁移
  守卫（``UnsupportedSchemaVersion`` / ``DatabaseOpenError``），发生在
  任何停写/备份/替换之前（拒绝危险操作）。
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import time
import uuid

from fathom import (
    SERVICE_IDENTITY,
    __protocol_version__,
    __version__,
    config,
    db,
    launchd,
    scan_coordinator,
)


def _next_patch_version(version: str) -> str:
    """N → N+1：补丁号 +1（"0.3.0" → "0.3.1"），仅用于夹具的 staged 新版。"""
    major, minor, patch = (int(piece) for piece in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


class UpgradeError(RuntimeError):
    """升级协议失败（可回滚）。"""


class HandshakeError(UpgradeError):
    """新 helper / app 版本握手不一致。"""


class SchemaRefused(UpgradeError):
    """数据库 schema 不可信（较新或不可识别），拒绝任何危险操作。"""


class UpgradeAborted(RuntimeError):
    """模拟升级中途进程退出：不执行回滚、不清 journal，状态留在原地。"""


def _write_0600(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_app_bundle(parent: Path, version: str) -> Path:
    """fake app 目录形态：Contents/{Info.plist, MacOS/{Fathom, helper}}。

    Info.plist 是版本文件（CFBundleShortVersionString）；helper 二进制是
    带版本标记的假文件（从不被执行）。
    """
    app = parent / "Fathom.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "Fathom").write_text(
        f"#!/bin/sh\n# fake app shell marker version={version}\n", encoding="utf-8"
    )
    (macos / "helper").write_text(
        f"fake-helper version={version}\n", encoding="utf-8"
    )
    info = {
        "CFBundleIdentifier": "com.maoscripts.fathom",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return app


def _read_app_version(app_dir: Path) -> str:
    with (app_dir / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    return str(info["CFBundleShortVersionString"])


def _snapshot_roots(conn: sqlite3.Connection) -> list[str]:
    return sorted(str(row[0]) for row in conn.execute("SELECT root FROM snapshots"))


class FakeUpgradeEnv:
    """升级协调协议的 fake 运行环境（全部位于 tmp 目录，零生产触碰）。

    布局::

        base/
          runtime/                     # 运行根（ISS-025 形态）
            data/fathom.db             # 真实 sqlite3，WAL 模式，含已知数据行
            data/fathom.db-wal         # 种子行刻意驻留 WAL（证明备份非文件拷贝）
            data/fakeup-procs/<pid>.marker   # 假进程标记（存在 = pid 存活）
            helper-instance.json       # 0600（helper.rs 语义字段）
            reports/  logs/
          applications/Fathom.app      # 当前安装（稳定路径：替换只换内容）
          staging/Fathom.app           # 就位的 N+1 候选
          rollback/                    # 替换时保留的 N 版备份
          LaunchAgents/                # 010B 合同形态的两份 plist
          runtime/data/fathom.db.upgrade-journal.json   # 升级进行中标记
    """

    #: 种子数据行（base 行落主文件，wal-only 行驻留 WAL）
    BASE_ROOT = "/synthetic/030a/base"
    WAL_ONLY_ROOT = "/synthetic/030a/wal-only"
    EXPECTED_ROOTS = (BASE_ROOT, WAL_ONLY_ROOT)

    def __init__(self, base: Path):
        self.base = base
        self.runtime = base / "runtime"
        self.data_dir = self.runtime / "data"
        self.logs_dir = self.runtime / "logs"
        self.reports_dir = self.runtime / "reports"
        self.db_path = self.data_dir / "fathom.db"
        self.scan_lock_path = self.db_path.with_name(
            self.db_path.name + ".scan.lock"
        )
        self.instance_path = self.runtime / config.HELPER_INSTANCE_FILENAME
        self.fake_proc_dir = self.data_dir / "fakeup-procs"
        self.journal_path = self.data_dir / (self.db_path.name
                                              + ".upgrade-journal.json")
        self.applications_dir = base / "applications"
        self.installed_app = self.applications_dir / "Fathom.app"
        self.staging_dir = base / "staging"
        self.staged_app = self.staging_dir / "Fathom.app"
        self.rollback_dir = base / "rollback"
        self.rollback_app = self.rollback_dir / "Fathom.app"
        self.launchagents_dir = base / "LaunchAgents"
        self.n_version = __version__
        self.n_plus_1_version = _next_patch_version(__version__)
        self._db_conn: sqlite3.Connection | None = None
        self._pid_counter = 4241

    # ---------------------------------------------------------------- build
    @classmethod
    def build(
        cls,
        base: Path,
        *,
        db_builder=None,
    ) -> "FakeUpgradeEnv":
        """构建 fake 环境。

        ``db_builder``：可选的替代建库函数（写坏 schema 库用）；默认种出
        健康 WAL 库并保持连接打开（WAL 驻留行不因关闭连接被 checkpoint
        回主文件）。
        """
        env = cls(base)
        for directory in (
            env.data_dir, env.logs_dir, env.reports_dir, env.fake_proc_dir,
            env.applications_dir, env.staging_dir, env.rollback_dir,
            env.launchagents_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        _write_app_bundle(env.applications_dir, env.n_version)
        _write_app_bundle(env.staging_dir, env.n_plus_1_version)
        if db_builder is None:
            env._seed_database()
        else:
            db_builder(env.db_path)
        env.start_helper(env.n_version)
        env._write_launchagents_plists()
        return env

    def _seed_database(self) -> None:
        conn = db.connect(self.db_path)
        # 第一批（base 行）：提交后主动 checkpoint，落进主库文件。
        self._insert_snapshot(conn, self.BASE_ROOT)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # 第二批（wal-only 行）：提交后不再 checkpoint，驻留 WAL——
        # 只拷主文件的"备份"会丢掉这批行（非文件拷贝的反例证据）。
        self._insert_snapshot(conn, self.WAL_ONLY_ROOT)
        conn.commit()
        # 连接保持打开：避免最后一个连接关闭时 SQLite 自动 checkpoint。
        self._db_conn = conn

    @staticmethod
    def _insert_snapshot(conn: sqlite3.Connection, root: str) -> int:
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("2026-09-20T08:00:00", root, 3, 0, 12.0, 1024,
             config.MIN_DIR_KB, "full"),
        )
        snapshot_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
            (snapshot_id, f"{root}/Archive", 512),
        )
        conn.execute(
            "INSERT INTO scan_runs(started_at, status) VALUES (?, 'done')",
            ("2026-09-20T08:00:00",),
        )
        return snapshot_id

    def _write_launchagents_plists(self) -> None:
        """010B 合同形态：复用 launchd 同源发射器，指向当前安装的 helper。"""
        helper_bin = self.installed_app / "Contents" / "MacOS" / "helper"
        scan = launchd._scan_plist_argv(
            [str(helper_bin), "scan", "--source", "scheduled"],
            hour=config.SCAN_HOUR, minute=0, logs_dir=self.logs_dir,
        )
        web = launchd._web_plist_argv(
            [str(helper_bin), "serve"], self.logs_dir
        )
        (self.launchagents_dir / f"{config.SCAN_LABEL}.plist").write_text(
            scan, encoding="utf-8"
        )
        (self.launchagents_dir / f"{config.WEB_LABEL}.plist").write_text(
            web, encoding="utf-8"
        )

    # ------------------------------------------------------------- helpers
    def expected_roots(self) -> list[str]:
        return sorted(self.EXPECTED_ROOTS)

    def snapshot_roots(self) -> list[str]:
        conn = sqlite3.connect(self.db_path)
        try:
            return _snapshot_roots(conn)
        finally:
            conn.close()

    def installed_version(self) -> str:
        return _read_app_version(self.installed_app)

    def staged_version(self) -> str:
        return _read_app_version(self.staged_app)

    def helper_binary_version(self, app_dir: Path) -> str:
        """假 helper 二进制里的版本标记（``--version`` 语义的可测面）。"""
        first = (app_dir / "Contents" / "MacOS" / "helper").read_text(
            encoding="utf-8"
        ).strip()
        return first.rsplit("version=", 1)[1]

    # ------------------------------------------------- 假 helper 进程模型
    def pid_alive(self, pid: int) -> bool:
        """假进程存活判定：标记文件存在（绝不触碰真实系统进程）。"""
        return (self.fake_proc_dir / f"{pid}.marker").exists()

    def start_helper(self, version: str) -> int:
        """启动假 helper：新 pid 标记 + 重写 helper-instance.json（0600）。"""
        self._pid_counter += 1
        pid = self._pid_counter
        _write_0600(
            self.fake_proc_dir / f"{pid}.marker",
            f"fake-helper pid={pid} version={version}\n".encode("utf-8"),
        )
        instance = {
            "pid": pid,
            "port": 7952,
            "service": SERVICE_IDENTITY,
            "protocol_version": __protocol_version__,
            "version": version,
            "instance_id": uuid.uuid4().hex,
            "runtime_mode": "release",
        }
        _write_0600(
            self.instance_path,
            json.dumps(instance, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            ),
        )
        return pid

    def stop_helper(self, pid: int) -> None:
        """假进程退出：移除标记文件（此后 ``pid_alive`` 为 False）。"""
        (self.fake_proc_dir / f"{pid}.marker").unlink(missing_ok=True)

    def read_instance(self) -> dict:
        return json.loads(self.instance_path.read_text(encoding="utf-8"))

    def close(self) -> None:
        if self._db_conn is not None:
            self._db_conn.close()
            self._db_conn = None

    def __enter__(self) -> "FakeUpgradeEnv":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class UpgradeResult:
    """一次协调器运行的结果与回滚证据。"""

    def __init__(
        self,
        *,
        ok: bool,
        steps_done: list[str],
        error: str | None = None,
        error_kind: str | None = None,
        rolled_back: bool = False,
        finalized: bool = False,
        backup_path: Path | None = None,
        rollback_actions: list[str] | None = None,
    ):
        self.ok = ok
        self.steps_done = steps_done
        self.error = error
        self.error_kind = error_kind
        self.rolled_back = rolled_back
        self.finalized = finalized
        self.backup_path = backup_path
        self.rollback_actions = rollback_actions or []


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, SchemaRefused):
        return "schema_refused"
    if isinstance(exc, HandshakeError):
        return "handshake"
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "disk_full"
    if isinstance(exc, sqlite3.Error):
        return "backup"
    return "internal"


class UpgradeCoordinator:
    """六步升级协议（夹具实现，供测试逐步断言与注入失败）。

    步骤：①停写（真实 ScanLease flock）→ ②旧 helper 退出（进程标记确认
    后清 instance 文件）→ ③SQLite 一致备份（checkpoint + backup API +
    独立打开校验）→ ④替换（N 挪入 rollback 保留，N+1 就位）→ ⑤新 helper
    启动（instance 文件重建）→ ⑥版本握手（app/helper/协议版本一致）。

    失败注入（``hooks``）：

    - ``checkpoint``：步骤③ checkpoint 前调用，抛错即备份失败（异常注入）。
    - ``backup_write``：备份目标写入时调用，抛 ``OSError(ENOSPC)`` 模拟
      磁盘写满。
    - ``new_helper_version``：值或 callable，步骤⑤新 helper 上报的版本
      （注入旧版本即握手不匹配）。
    - ``abort_after``：整数 k，步骤 k 完成后模拟进程退出（不回滚、
      不清 journal、锁随进程消失）。
    """

    STEPS = (
        ("1_quiesce", "step1_quiesce"),
        ("2_old_helper_exit", "step2_old_helper_exit"),
        ("3_backup", "step3_backup"),
        ("4_replace", "step4_replace"),
        ("5_new_helper", "step5_start_new_helper"),
        ("6_handshake", "step6_handshake"),
    )

    def __init__(self, env: FakeUpgradeEnv, *, hooks: dict | None = None):
        self.env = env
        self.hooks = hooks or {}
        self.steps_done: list[str] = []
        self.backup_path: Path | None = None
        self.rollback_actions: list[str] = []
        self.new_helper_pid: int | None = None
        self.checkpoint_result: tuple[int, int, int] | None = None
        self._lease: scan_coordinator.ScanLease | None = None

    # -------------------------------------------------------------- journal
    def _journal_write(self, phase: str) -> None:
        payload = {
            "from_version": self.env.n_version,
            "to_version": self.env.n_plus_1_version,
            "phase": phase,
            "steps_done": list(self.steps_done),
        }
        _write_0600(
            self.env.journal_path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            ),
        )

    # ------------------------------------------------------------- preflight
    def _preflight(self) -> None:
        """既有 schema 守卫：较新/不可识别 schema 在任何停写/备份/替换前拒绝。"""
        try:
            conn = db.connect(self.env.db_path)
        except db.DatabaseOpenError as exc:
            raise SchemaRefused(f"数据库 schema 拒绝升级：{exc}") from exc
        conn.close()
        if not self.env.staged_app.exists():
            raise UpgradeError(f"N+1 就位包缺失：{self.env.staged_app}")
        if not self.env.installed_app.exists():
            raise UpgradeError(f"当前安装缺失：{self.env.installed_app}")

    # ----------------------------------------------------------------- 步骤
    def step1_quiesce(self) -> None:
        """①停写：取真实扫描租约（ISS-020 flock），持锁期间拒绝新写入会话。"""
        self._lease = scan_coordinator.ScanLease.acquire(
            self.env.scan_lock_path, source="cli"
        )

    def step2_old_helper_exit(self) -> None:
        """②旧 helper 退出：先确认进程退出（标记消失），再清 instance 文件。"""
        instance = self.env.read_instance()
        pid = int(instance["pid"])
        if self.env.pid_alive(pid):
            self.env.stop_helper(pid)
        # 进程退出确认（标记消失）后才清 instance 文件——文件清理语义。
        if self.env.pid_alive(pid):
            raise UpgradeError(f"旧 helper pid={pid} 仍存活，拒绝继续")
        self.env.instance_path.unlink(missing_ok=True)

    def step3_backup(self) -> None:
        """③SQLite 一致备份：真实 WAL checkpoint + backup API（非文件拷贝）。"""
        self._call_hook("checkpoint")
        conn = sqlite3.connect(self.env.db_path, timeout=10)
        try:
            self.checkpoint_result = tuple(
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            )
            target = self.env.db_path.with_name(
                f"{self.env.db_path.name}.upgrade-backup-"
                f"{self.env.n_version}-{time.time_ns()}.sqlite3"
            )
            temporary = target.with_name(target.name + ".tmp")
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            os.close(fd)
            backup = sqlite3.connect(temporary)
            try:
                self._call_hook("backup_write")
                conn.backup(backup)
                roots_expected = _snapshot_roots(conn)
                roots_backup = _snapshot_roots(backup)
                if roots_backup != roots_expected:
                    raise UpgradeError(
                        f"备份校验失败：备份行 {roots_backup} != 源行 "
                        f"{roots_expected}"
                    )
            except BaseException:
                backup.close()
                temporary.unlink(missing_ok=True)
                raise
            backup.close()
            os.replace(temporary, target)
            self.backup_path = target
        finally:
            conn.close()

    def step4_replace(self) -> None:
        """④替换：N 挪入 rollback 保留，N+1 就位（安装路径保持稳定）。"""
        if self.env.rollback_app.exists():
            raise UpgradeError(f"rollback 位非空：{self.env.rollback_app}")
        os.replace(self.env.installed_app, self.env.rollback_app)
        os.replace(self.env.staged_app, self.env.installed_app)

    def step5_start_new_helper(self) -> None:
        """⑤新 helper 启动：新假进程标记 + 重建 helper-instance.json。"""
        version = self.hooks.get(
            "new_helper_version", self.env.n_plus_1_version
        )
        if callable(version):
            version = version()
        self.new_helper_pid = self.env.start_helper(version)

    def step6_handshake(self) -> None:
        """⑥版本握手：app 版本、helper 上报版本与协议身份必须一致为 N+1。"""
        expected = self.env.n_plus_1_version
        app_version = self.env.installed_version()
        instance = self.env.read_instance()
        problems: list[str] = []
        if app_version != expected:
            problems.append(f"app={app_version} != {expected}")
        if instance.get("version") != expected:
            problems.append(
                f"helper={instance.get('version')} != {expected}"
            )
        if instance.get("service") != SERVICE_IDENTITY:
            problems.append(f"service={instance.get('service')!r}")
        if instance.get("protocol_version") != __protocol_version__:
            problems.append(
                f"protocol={instance.get('protocol_version')} != "
                f"{__protocol_version__}"
            )
        if not self.env.pid_alive(int(instance["pid"])):
            problems.append(f"helper pid={instance['pid']} 未存活")
        if problems:
            raise HandshakeError("版本握手失败：" + "; ".join(problems))

    # -------------------------------------------------------------- 编排
    def _call_hook(self, name: str) -> None:
        hook = self.hooks.get(name)
        if hook is not None:
            hook()

    def _release_lease(self) -> None:
        if self._lease is not None:
            self._lease.release()
            self._lease = None

    def dispose(self) -> None:
        """幂等收尾：释放尚未释放的停写租约（测试 finally 用）。"""
        self._release_lease()

    def _rollback(self, exc: BaseException) -> list[str]:
        """失败回滚：回到可运行旧版与旧数据（绝不动 DB 数据行）。"""
        actions: list[str] = []
        installed = self.env.installed_app
        rollback = self.env.rollback_app
        current: str | None = None
        if installed.exists():
            try:
                current = self.env.installed_version()
            except Exception:
                current = None
        if (
            rollback.exists()
            and (not installed.exists() or current == self.env.n_plus_1_version)
        ):
            if installed.exists():
                shutil.rmtree(installed)
            os.replace(rollback, installed)
            actions.append("restored_app_from_rollback")
        if not self.env.instance_path.exists():
            self.env.start_helper(self.env.n_version)
            actions.append("restarted_old_helper")
        if self.env.journal_path.exists():
            self.env.journal_path.unlink()
            actions.append("removed_journal")
        if self._lease is not None:
            self._release_lease()
            actions.append("released_quiesce_lease")
        return actions

    def run(self, *, through: int | None = None) -> UpgradeResult:
        """执行协议；``through=k`` 只跑到步骤 k（逐步断言用，不收尾）。

        失败（除模拟退出外）自动回滚并以 ``ok=False`` 结果返回；
        ``UpgradeAborted`` 模拟进程死亡，向上抛出且不留回滚。
        """
        try:
            self._preflight()
            self._journal_write("started")
            for index, (label, method_name) in enumerate(self.STEPS, start=1):
                getattr(self, method_name)()
                self.steps_done.append(label)
                self._journal_write(label)
                if self.hooks.get("abort_after") == index:
                    # 模拟进程退出：锁随 fd 关闭（内核语义）消失，不回滚。
                    self._release_lease()
                    raise UpgradeAborted(
                        f"模拟中途退出：步骤 {index}（{label}）完成后"
                    )
                if through == index:
                    self._release_lease()
                    return UpgradeResult(
                        ok=True,
                        steps_done=list(self.steps_done),
                        finalized=False,
                        backup_path=self.backup_path,
                    )
            self.env.journal_path.unlink()
            self._release_lease()
            return UpgradeResult(
                ok=True,
                steps_done=list(self.steps_done),
                finalized=True,
                backup_path=self.backup_path,
            )
        except UpgradeAborted:
            raise
        except Exception as exc:
            self.rollback_actions = self._rollback(exc)
            return UpgradeResult(
                ok=False,
                steps_done=list(self.steps_done),
                error=str(exc),
                error_kind=_classify_error(exc),
                rolled_back=bool(self.rollback_actions),
                backup_path=self.backup_path,
                rollback_actions=list(self.rollback_actions),
            )


def detect_upgrade_state(env: FakeUpgradeEnv) -> str:
    """半升级态检测：journal 在位（或安装撕裂）即可检测、可恢复。"""
    if env.journal_path.exists():
        return "half_upgraded"
    if not env.installed_app.exists():
        return "half_upgraded"
    return "clean"


def recover_from_abort(env: FakeUpgradeEnv) -> dict:
    """从中途退出的半升级态恢复到可运行旧版（不动 DB 数据行）。"""
    state = detect_upgrade_state(env)
    if state != "half_upgraded":
        raise UpgradeError(f"无可恢复的半升级态：{state}")
    actions: list[str] = []
    installed = env.installed_app
    rollback = env.rollback_app
    current: str | None = None
    if installed.exists():
        try:
            current = env.installed_version()
        except Exception:
            current = None
    if rollback.exists() and (
        not installed.exists() or current == env.n_plus_1_version
    ):
        if installed.exists():
            shutil.rmtree(installed)
        os.replace(rollback, installed)
        actions.append("restored_app_from_rollback")
    if not env.instance_path.exists():
        env.start_helper(env.n_version)
        actions.append("restarted_old_helper")
    env.journal_path.unlink()
    actions.append("removed_journal")
    return {
        "state_before": state,
        "actions": actions,
        "recovered_version": env.installed_version(),
    }


# ------------------------------------------------------------- schema 反例
def write_newer_schema_db(path: Path) -> None:
    """较新 schema 库：user_version = SCHEMA_VERSION + 1（回滚 journal 模式，
    避免 WAL 残留干扰"字节未变"断言）。"""
    conn = sqlite3.connect(path)
    try:
        for statement in db._SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb) VALUES ('2026-09-20T08:00:00', "
            "'/synthetic/030a/from-future', 1, 0, 1.0, 16)"
        )
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()


def write_unrecognized_old_schema_db(path: Path) -> None:
    """不可识别的旧 schema 库：v1 形态 + 未知未版本化表（守卫必须拒绝）。"""
    conn = sqlite3.connect(path)
    try:
        for statement in db._SCHEMA_STATEMENTS[:4]:  # v1 的四张表
            conn.execute(statement)
        conn.execute("CREATE TABLE legacy_notes (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO legacy_notes(note) VALUES ('未知旧工具写入')")
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb) VALUES ('2026-09-20T08:00:00', "
            "'/synthetic/030a/legacy', 1, 0, 1.0, 16)"
        )
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    finally:
        conn.close()


def file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
