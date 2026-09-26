"""生产升级协调器（ISS-040C）：``updater_install`` 六步协议的 Python 侧。

分工合同（ISS-040C 接口合同，实现不得偏离）：

- 本模块拥有**协议状态**：①非阻塞停写（真实 ``scan_coordinator.ScanLease``
  flock，被在途扫描持有→明确拒绝，绝不终止在途扫描）→ ②旧 helper 优雅
  退出确认（helper-instance pid + 端口释放，超时→中止→回滚）→ ③SQLite
  一致备份（经 ``fathom.db.consistent_backup`` 公共入口：checkpoint +
  backup API + 完整性校验，禁止文件拷贝）→ ④升级 journal 落盘/检测/清除
  （半升级态可检测，030A ``detect_upgrade_state`` 语义生产化），以及文件
  级回滚。
- Rust 壳（``apps/desktop/src-tauri/src/lib.rs::updater_install``）拥有
  **进程生命周期与真实下载安装**：确认层（confirmed=true）后先经冻结
  helper 子命令 ``upgrade-prepare`` 执行①-④，再 ``download_and_install``
  （进度映射 ``UPDATER_EVENT`` downloading 含 downloaded/total；下载阶段
  可取消、进入安装后不可取消且文案明确），新 helper 身份核验（``--version``
  身份面）后经 ``upgrade-finalize`` 清 journal；任一步失败经
  ``upgrade-rollback`` 回滚并由壳重启旧 helper。子命令与生产 CLI 同入口
  ``fathom/__main__.py``，随打包自然携带。
- **进程重启单属主**：旧 helper「恢复运行」的重启动作归 Rust 壳（helper
  进程属主，见 lib.rs 模块说明）；本模块的回滚只负责协议状态（journal、
  陈旧 instance 清理、数据不动、候选/备份保留），CLI 侧无重启钩子时记
  ``helper_restart_deferred_to_shell``。绝不以 030A 夹具
  （``tests/upgrade_fixture.py``）替代生产入口——夹具语义仅作协议参照。

六步合同（与 030A 夹具语义对齐、生产化）：
①停写；②旧 helper 退出；③一致备份；④journal；⑤下载安装（壳侧
download_and_install；本模块经注入的 install 钩子建模，供集成测试注入
fake 验证协议语义）；⑥成功：journal 清除、候选清空（壳侧 UpdaterState）、
重启仍走 ``updater_restart`` 独立确认（不静默、不自动重启）。新版本启动
后握手失败的检测与恢复经 journal 判定路径（``detect_upgrade_state`` +
``rollback``）。任一步失败回滚：旧 helper 恢复运行（钩子/壳）、旧数据
不动、候选保留、journal 清除。

ISS-097 升级事务合同（持续停写 / 重入拒绝 / journal 所有权）：

- **事务 ID**：每个 ``UpgradeCoordinator`` 实例（= 一次 prepare 尝试）持有
  ``txn_id``（uuid4 hex，进程内外全局唯一），随每次 journal 写入落盘；
  ``owner_pid`` 仅为诊断，不作为所有权判据（进程退出后 flock 失效，journal
  仍是事务凭据）。
- **互斥持有者**：事务 owner 是发起 ``updater_install`` 的 Rust 壳；本模块
  是 owner 在单个子命令进程内的协议执行者。prepare ①-④ 期间互斥 =
  ScanLease flock（进程级）+ journal（跨进程持久）；prepare 子进程退出后
  到 finalize/rollback 前，journal 是唯一停写凭据（租约按既有合同释放，
  不回归 ISS-040C）。同壳并发 invoke 由壳侧 ``UpdaterInstallCtl`` 原子
  独占位拒绝（见 lib.rs）。
- **停写语义**：「写入」= 新扫描会话（``scan_coordinator.start_scan``，
  覆盖 API/CLI/定时三入口）。拒绝条件 = journal 文件**存在**（不解析内容，
  损坏同样停写，fail-closed）。次序不变量（防 TOCTOU）：写入方先取租约
  后查 journal，升级方先取租约、查残留事务、再写 journal——「写入已开始」
  与「事务已建立」不可能同时成立。事务结束（finalize 成功或显式 rollback）
  后恢复可写。
- **journal 所有权**：自动回滚（run_prepare/run_full 失败路径）仅当 journal
  的 ``txn_id`` 等于本次尝试时才清除并执行恢复动作；否则记
  ``left_foreign_journal``、journal 字节不动。拒绝方（prepare 重入、被
  停写的写入方）绝不读改删 journal。唯一清除通道 = 显式
  ``upgrade-rollback``（恢复入口，owner/操作者决策，可清损坏与旧格式
  journal）与 ``upgrade-finalize``（owner 侧成功收尾，仅由壳在核验通过
  后调用）。
- **异常恢复**：prepare 子进程或壳在任一阶段崩溃 → journal 残留 → 停写
  持续、新升级尝试被 ``half_upgraded_state`` 拒绝并指向恢复入口
  （``upgrade-detect`` 只读检测 + ``upgrade-rollback`` 恢复）。全部等待
  有界：helper 退出确认 ≤ 10s；壳侧子命令/身份核验 ≤ 120s（超时只终止
  自己 spawn 的子进程）；停写检查零等待。
- **兼容**：journal 新字段（txn_id/owner_pid）为增量；旧格式 journal（无
  txn_id）按「未知所有权遗留事务」处理——停写生效、自动路径不清、显式
  rollback 可恢复。schema/配置零改动。
"""

from __future__ import annotations

import datetime as dt
import errno
import json
import os
import signal
import socket
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import SERVICE_IDENTITY, __protocol_version__, config, db, scan_coordinator

#: 旧 helper 优雅退出的确认时限（对齐壳侧 STOP_TIMEOUT_S=10）。
HELPER_EXIT_TIMEOUT_S = 10.0
#: 退出确认轮询间隔。
HELPER_EXIT_POLL_INTERVAL_S = 0.1
#: 端口释放探测超时（仅回环 connect，不发送数据）。
PORT_PROBE_TIMEOUT_S = 0.3
#: 升级 journal 文件名后缀（与 db 同目录：``<db 名>.upgrade-journal.json``）。
JOURNAL_SUFFIX = ".upgrade-journal.json"


class UpgradeError(RuntimeError):
    """升级协议失败（可回滚）。"""


class UpgradeRefused(UpgradeError):
    """前置条件不满足：可恢复、可重试（扫描进行中/半升级态残留/schema 拒绝）。
    绝不终止在途扫描、不触碰任何进程。"""

    def __init__(self, message: str, *, kind: str = "refused"):
        super().__init__(message)
        self.kind = kind


class HelperExitTimeout(UpgradeError):
    """旧 helper 未在时限内确认退出；已中止并回滚。"""


class HandshakeError(UpgradeError):
    """新 helper 身份/版本握手不一致。"""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _classify(exc: BaseException) -> str:
    """协议失败分类（prepare 阶段；install 阶段固定 kind=install）。"""
    if isinstance(exc, UpgradeRefused):
        return exc.kind
    if isinstance(exc, HelperExitTimeout):
        return "helper_exit_timeout"
    if isinstance(exc, HandshakeError):
        return "handshake"
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "disk_full"
    if isinstance(exc, sqlite3.Error):
        return "backup"
    return "internal"


# ---------------------------------------------------------------- 路径集合
@dataclass(frozen=True)
class UpgradePaths:
    """升级协议涉及的运行根文件集合（生产由 config 派生；测试可显式指定）。"""

    runtime_dir: Path
    db_path: Path
    lock_path: Path
    instance_path: Path
    journal_path: Path

    @classmethod
    def from_config(cls) -> "UpgradePaths":
        """生产派生：与 scan_coordinator 的锁路径推导完全一致。"""
        runtime = config.get_runtime_config()
        db_path = Path(config.DB_PATH)
        return cls(
            runtime_dir=runtime.runtime_dir,
            db_path=db_path,
            lock_path=db_path.with_name(db_path.name + ".scan.lock"),
            instance_path=runtime.runtime_dir / config.HELPER_INSTANCE_FILENAME,
            journal_path=journal_path_from_db(db_path),
        )


# ------------------------------------------------------------- 生产探针
def _ps_pid_alive(pid: int) -> bool:
    """只读判活：``ps -p``（零信号合同，与壳 helper.rs::pid_is_alive 同
    语义——包括信号 0 也不使用）。ps 不可用时保守返回 True（视为存活）。"""
    if pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid="],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:  # noqa: BLE001 - 判活失败宁可保守：视为存活
        return True
    return out.returncode == 0 and bool(out.stdout.strip())


def _loopback_port_open(port: int) -> bool:
    """端口释放探测：仅向 127.0.0.1:port 发起 connect（不发送数据、不
    绑定、不向占用者发任何信号）。连接失败即视为已释放。"""
    if port <= 0:
        return False
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=PORT_PROBE_TIMEOUT_S
        ):
            return True
    except OSError:
        return False


def _request_helper_graceful_exit(pid: int) -> None:
    """生产默认的退出请求：对身份核验通过的旧 helper pid 发 SIGTERM。
    ``cli.cmd_serve`` 的 SIGTERM 处理器会清理 helper-instance.json 后以 0
    退出（优雅路径）；本模块绝不对身份不符或未判活的 pid 发信号。"""
    os.kill(pid, signal.SIGTERM)


# --------------------------------------------------------- journal 与检测
def journal_path_from_db(db_path: Path) -> Path:
    """journal 路径推导（单一来源）：与 db 同目录 ``<db 名>.upgrade-journal.json``。

    供 ``UpgradePaths.from_config`` 与 ``scan_coordinator`` 的停写条件共用，
    避免两处各自拼后缀漂移。"""
    return db_path.with_name(db_path.name + JOURNAL_SUFFIX)


def _write_0600(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_journal(paths: UpgradePaths, payload: dict) -> None:
    """journal 落盘（④）：0600、原子替换，半升级态在进程死亡后仍可检测。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    tmp = paths.journal_path.with_name(
        f".{paths.journal_path.name}.{os.getpid()}.tmp"
    )
    _write_0600(tmp, body)
    os.replace(tmp, paths.journal_path)


def peek_journal(path: Path) -> dict | None:
    """按 journal 文件路径只读探测（停写条件等只读路径使用）。

    缺失或不可解析（损坏）都返回 None——调用方以**文件存在性**为停写/半
    升级判据（fail-closed），内容仅作诊断。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_journal(paths: UpgradePaths) -> dict | None:
    return peek_journal(paths.journal_path)


def clear_journal(paths: UpgradePaths) -> bool:
    """清除 journal；返回是否真实清除（幂等）。"""
    try:
        paths.journal_path.unlink()
        return True
    except FileNotFoundError:
        return False


def read_instance(paths: UpgradePaths) -> dict | None:
    """读 helper-instance.json；缺失返回 None，不可解析抛错（fail-closed：
    未知状态不做任何危险动作）。

    「缺失」仅指预期的 ``FileNotFoundError``（helper 正常退出后的自清、
    或本就无实例，ISS-096）：视为无实例记录，调用方按既有分支处理，不
    据此对未知进程做任何推断。损坏 JSON（``ValueError``）与权限/I-O 等
    其他 ``OSError`` 仍抛出，保守拒绝。"""
    try:
        raw = paths.instance_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("helper-instance.json 不是 JSON object")
    return value


def detect_upgrade_state(
    paths: UpgradePaths, *, app_path: Path | None = None
) -> str:
    """半升级态检测（030A ``detect_upgrade_state`` 语义生产化）：
    journal 在位（或可选的安装目录缺失）→ ``half_upgraded``；否则 ``clean``。"""
    if paths.journal_path.exists():
        return "half_upgraded"
    if app_path is not None and not app_path.exists():
        return "half_upgraded"
    return "clean"


# ---------------------------------------------------------------- 协调器
class UpgradeCoordinator:
    """六步协议的 Python 侧编排（生产入口；系统动作可注入）。

    ``hooks``（均可选；生产默认见各回退函数）：

    - ``pid_alive(pid)``：只读判活（默认 ``ps -p``）。
    - ``port_open(port)``：端口释放探测（默认回环 connect）。
    - ``request_helper_exit(pid)``：优雅退出请求（默认 SIGTERM）。
    - ``start_helper()``：回滚时恢复旧 helper 运行（生产无默认——进程
      属主是 Rust 壳，见模块说明；集成测试注入 fake）。
    - ``restore_old_version()``：回滚时恢复旧版安装（生产无默认——安装
      器域动作，由壳/安装器承担；集成测试注入 030A 语义 fake）。
    """

    PREPARE_STEPS = ("1_quiesce", "2_old_helper_exit", "3_backup", "4_journal")

    def __init__(
        self,
        paths: UpgradePaths,
        *,
        from_version: str = "",
        to_version: str = "",
        hooks: dict | None = None,
        helper_exit_timeout_s: float = HELPER_EXIT_TIMEOUT_S,
    ):
        self.paths = paths
        self.from_version = str(from_version)
        self.to_version = str(to_version)
        self.hooks = dict(hooks or {})
        self.helper_exit_timeout_s = float(helper_exit_timeout_s)
        # ISS-097：本实例（一次 prepare 尝试）的事务 ID——journal 所有权判据。
        self.txn_id = uuid.uuid4().hex
        self.steps_done: list[str] = []
        self.backup_path: Path | None = None
        self._lease: scan_coordinator.ScanLease | None = None

    # ------------------------------------------------- 可注入系统动作
    def _pid_alive(self, pid: int) -> bool:
        hook = self.hooks.get("pid_alive")
        return _ps_pid_alive(pid) if hook is None else bool(hook(pid))

    def _port_open(self, port: int) -> bool:
        hook = self.hooks.get("port_open")
        return _loopback_port_open(port) if hook is None else bool(hook(port))

    def _request_exit(self, pid: int) -> None:
        hook = self.hooks.get("request_helper_exit")
        if hook is None:
            _request_helper_graceful_exit(pid)
        else:
            hook(pid)

    # --------------------------------------------------------- journal
    def _journal_write(self, phase: str) -> None:
        write_journal(self.paths, {
            "service": SERVICE_IDENTITY,
            "txn_id": self.txn_id,
            "owner_pid": os.getpid(),
            "from_version": self.from_version,
            "to_version": self.to_version,
            "phase": phase,
            "steps_done": list(self.steps_done),
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "updated_at": _now(),
        })

    # ------------------------------------------------------------ 步骤
    def _preflight_schema(self) -> None:
        """schema 守卫：任何危险动作之前拒绝（只查库，不查事务状态）。"""
        try:
            conn = db.connect(self.paths.db_path)
        except db.DatabaseOpenError as exc:
            raise UpgradeRefused(
                f"数据库 schema 拒绝升级：{exc}", kind="schema_refused"
            ) from exc
        conn.close()

    def _refuse_if_txn_active(self) -> None:
        """ISS-097 事务互斥：残留/在途升级 journal 在位时拒绝重入。

        只在**已取得停写租约后**调用（次序不变量：升级方先取租约、查残留
        事务、再写自己的 journal——保证任一时刻至多一个写 journal 的进程，
        两次 prepare 竞争不会互相覆盖 journal）。损坏 journal 同样拒绝
        （fail-closed），文案指向唯一恢复入口 upgrade-rollback。"""
        if not self.paths.journal_path.exists():
            return
        journal = read_journal(self.paths)
        if journal is None:
            raise UpgradeRefused(
                "检测到未收口的半升级态（升级 journal 在位但不可解析/损坏）；"
                "请先经 upgrade-detect 检测并 upgrade-rollback 恢复后再升级",
                kind="half_upgraded_state",
            )
        raise UpgradeRefused(
            "检测到未收口的半升级态（升级 journal 在位，txn_id="
            f"{journal.get('txn_id') or '旧格式无事务ID'}，phase="
            f"{journal.get('phase')}）；请先经 upgrade-detect 检测并 "
            "upgrade-rollback 恢复后再升级",
            kind="half_upgraded_state",
        )

    def _step1_quiesce(self) -> None:
        """①停写：非阻塞取得真实扫描租约；busy→明确拒绝，绝不终止在途扫描。"""
        try:
            self._lease = scan_coordinator.ScanLease.acquire(
                self.paths.lock_path, source="upgrader"
            )
        except scan_coordinator.ScanBusyError as exc:
            raise UpgradeRefused(
                "已有扫描在进行中；为不终止在途扫描，本次升级已被拒绝。"
                "请等扫描完成后重试",
                kind="scan_busy",
            ) from exc

    def _step2_old_helper_exit(self) -> None:
        """②旧 helper 优雅退出：经 helper-instance pid 请求退出，有界确认
        pid 退出 + 端口释放；超时→中止（由上层回滚）。

        无实例记录（``read_instance`` 返回 None：helper 已正常退出并自清
        实例文件，或本就无实例——ISS-096）视为无旧 helper 可确认，直接
        通过；不据此对未知进程做推断，也不放宽存在记录时的 pid/身份/端口
        核查。"""
        instance = read_instance(self.paths)
        if instance is None:
            return  # 无旧 helper 记录：视为已退出
        pid = int(instance.get("pid") or 0)
        port = int(instance.get("port") or 0)
        if (instance.get("service") != SERVICE_IDENTITY
                or instance.get("protocol_version") != __protocol_version__):
            raise UpgradeError(
                "helper-instance.json 身份不符（service="
                f"{instance.get('service')!r}, protocol="
                f"{instance.get('protocol_version')!r}）；拒绝触碰该进程"
            )
        if pid <= 0:
            raise UpgradeError(f"helper-instance.json pid 非法：{pid}")
        if self._pid_alive(pid):
            self._request_exit(pid)
        deadline = time.monotonic() + self.helper_exit_timeout_s
        while True:
            exited = not self._pid_alive(pid)
            released = not self._port_open(port)
            if exited and released:
                break
            if time.monotonic() >= deadline:
                raise HelperExitTimeout(
                    f"旧 helper pid={pid} 未在 {self.helper_exit_timeout_s:g}s "
                    f"内确认退出（pid 已退出={exited}，端口 {port} "
                    f"已释放={released}）；升级中止"
                )
            time.sleep(HELPER_EXIT_POLL_INTERVAL_S)
        # 退出确认后清理残留 instance（优雅退出通常由 helper 自清；陈旧则在此清理）。
        current = read_instance(self.paths)
        if current is not None and int(current.get("pid") or 0) == pid:
            self.paths.instance_path.unlink(missing_ok=True)

    def _step3_backup(self) -> None:
        """③SQLite 一致备份：db.consistent_backup（checkpoint + backup API
        + 完整性校验，禁止文件拷贝）。失败由上层中止并回滚。"""
        self.backup_path = db.consistent_backup(self.paths.db_path)

    def _release_lease(self) -> None:
        if self._lease is not None:
            self._lease.release()
            self._lease = None

    def dispose(self) -> None:
        """幂等收尾：释放尚未释放的停写租约（测试 finally 用）。"""
        self._release_lease()

    # ------------------------------------------------------------- 编排
    def run_prepare(self) -> dict:
        """执行①-④（停写→旧 helper 退出→一致备份→journal 落盘）。

        ISS-097 次序合同：schema 预检 → 取停写租约 → 残留事务互斥检查 →
        写自己的 journal（txn_id）→ ②③④。协议失败不抛异常：自动回滚并以
        ``{"ok": False, ...}`` 结果返回（kind 分类见 ``_classify``）；只有
        用法错误（参数非法）才抛。自动回滚只清**自己的** journal
        （``left_foreign_journal``：别人的事务凭据字节不动）。
        """
        self.steps_done = []
        try:
            self._preflight_schema()
            self._step1_quiesce()
            self._refuse_if_txn_active()
            self.steps_done.append("1_quiesce")
            self._journal_write("started")
            for label, step in (
                ("2_old_helper_exit", self._step2_old_helper_exit),
                ("3_backup", self._step3_backup),
            ):
                step()
                self.steps_done.append(label)
                self._journal_write(label)
            self.steps_done.append("4_journal")
            self._journal_write("prepared")
            self._release_lease()
            return {
                "ok": True,
                "kind": "prepared",
                "steps_done": list(self.steps_done),
                "backup_path": str(self.backup_path) if self.backup_path else None,
                "journal_path": str(self.paths.journal_path),
                "txn_id": self.txn_id,
            }
        except Exception as exc:  # noqa: BLE001 - 协议失败统一回滚为结果
            reason = str(exc)
            rollback = self.rollback(reason=reason)
            return {
                "ok": False,
                "kind": _classify(exc),
                "error": reason,
                "steps_done": list(self.steps_done),
                "backup_path": str(self.backup_path) if self.backup_path else None,
                "rollback_actions": rollback["actions"],
                "detected_state": rollback["detected_state"],
            }

    def run_full(self, *, install, new_helper_identity=None) -> dict:
        """⑤-⑥ 的协议建模（外部动作注入）：生产安装/核验由 Rust 壳执行
        ``download_and_install`` + 新 helper ``--version``；本入口供集成测试
        以注入的 fake 安装/身份动作验证协议与回滚语义。

        - ``install(ctx)``：外部安装动作（ctx 含 from/to/backup/journal）；
          抛错→kind=install→回滚。
        - ``new_helper_identity()``：返回新 helper ``--version`` 身份面 dict；
          缺省视为壳侧已核验（跳过 Python 层握手，仅收尾）。
        """
        prepared = self.run_prepare()
        if not prepared["ok"]:
            return prepared
        try:
            self._journal_write("installing")
            install({
                "from_version": self.from_version,
                "to_version": self.to_version,
                "backup_path": str(self.backup_path) if self.backup_path else None,
                "journal_path": str(self.paths.journal_path),
            })
            self._journal_write("installed")
            if new_helper_identity is not None:
                self._verify_handshake(new_helper_identity())
        except Exception as exc:  # noqa: BLE001 - 统一回滚为结果
            reason = str(exc)
            rollback = self.rollback(reason=reason)
            return {
                "ok": False,
                "kind": (
                    "handshake" if isinstance(exc, HandshakeError) else "install"
                ),
                "error": reason,
                "steps_done": list(self.steps_done),
                "backup_path": str(self.backup_path) if self.backup_path else None,
                "rollback_actions": rollback["actions"],
                "detected_state": rollback["detected_state"],
            }
        finalized = self.finalize()
        return {
            "ok": True,
            "kind": "finalized",
            "finalized": True,
            "steps_done": list(self.steps_done) + ["5_install", "6_handshake"],
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "actions": finalized["actions"],
        }

    def _verify_handshake(self, identity: dict) -> None:
        """⑥握手（030A step6 语义生产化）：service/protocol/version 三方一致。"""
        problems: list[str] = []
        if identity.get("service") != SERVICE_IDENTITY:
            problems.append(f"service={identity.get('service')!r}")
        if identity.get("protocol_version") != __protocol_version__:
            problems.append(
                f"protocol={identity.get('protocol_version')!r} != "
                f"{__protocol_version__}"
            )
        if str(identity.get("version")) != self.to_version:
            problems.append(
                f"version={identity.get('version')!r} != {self.to_version!r}"
            )
        if problems:
            raise HandshakeError("新 helper 握手失败：" + "; ".join(problems))

    def rollback(self, *, reason: str, own_journal_only: bool = True) -> dict:
        """失败回滚（合同：回到可运行旧版与旧数据，不留半升级态）。

        ISS-097 所有权语义：

        - ``own_journal_only=True``（默认，run_prepare/run_full 的自动回滚
          走此语义）：仅当在位 journal 的 ``txn_id`` 等于本次尝试时才执行
          清除与恢复动作；别人的事务（含损坏与旧格式无 txn_id 的 journal）
          记 ``left_foreign_journal``，journal 字节不动、不恢复旧版、不重
          启 helper——拒绝方不得动别的事务的恢复依据。
        - ``own_journal_only=False``：显式恢复入口（CLI ``upgrade-rollback``，
          owner/操作者决策）——允许清除在位 journal（含损坏/旧格式），其余
          恢复语义不变。

        恢复动作（仅对自己拥有的 journal 或显式恢复时执行）：

        - 先经 ``detect_upgrade_state`` 判定（journal 路径），再执行恢复；
        - 旧版安装恢复：``restore_old_version`` 钩子（生产无默认——安装器
          域动作；集成测试注入）；
        - 旧 helper 恢复运行：instance 缺失或 pid 已死时经 ``start_helper``
          钩子重启；CLI 生产路径无钩子→记 ``helper_restart_deferred_to_shell``
          （进程属主是 Rust 壳，壳的失败路径负责重启）；
        - 旧数据不动：本函数绝不读写任何业务数据行；备份与候选保留；
        - journal 清除 + 停写租约释放。
        """
        detected = detect_upgrade_state(self.paths)
        actions: list[str] = []
        journal = read_journal(self.paths)
        journal_is_ours = (
            journal is not None
            and str(journal.get("txn_id") or "") == self.txn_id
        )
        if detected == "half_upgraded" and not journal_is_ours and own_journal_only:
            # 保守：非本次事务的 journal（在途/残留/损坏/旧格式）绝不清除，
            # 也不执行任何恢复动作——那是事务 owner 或显式恢复入口的职权。
            if self._lease is not None:
                self._release_lease()
                actions.append("released_quiesce_lease")
            actions.append("left_foreign_journal")
            return {
                "ok": True,
                "detected_state": detected,
                "actions": actions,
                "reason": reason,
                "journal_preserved": True,
            }
        restore = self.hooks.get("restore_old_version")
        if restore is not None:
            restore()
            actions.append("restored_old_version")
        helper_running = False
        try:
            instance = read_instance(self.paths)
        except (OSError, ValueError):
            instance = None
        if instance is not None:
            pid = int(instance.get("pid") or 0)
            # 运行中的必须是**旧版** helper：升级中途启动的 N+1 helper 即使
            # 存活也不算「旧 helper 已恢复」，需经钩子重启旧版。
            version_matches_old = (
                not self.from_version
                or str(instance.get("version")) == self.from_version
            )
            helper_running = pid > 0 and version_matches_old and self._pid_alive(pid)
        if not helper_running:
            start = self.hooks.get("start_helper")
            if start is not None:
                start()
                actions.append("restarted_old_helper")
            else:
                actions.append("helper_restart_deferred_to_shell")
        if clear_journal(self.paths):
            actions.append("removed_journal")
        if self._lease is not None:
            self._release_lease()
            actions.append("released_quiesce_lease")
        return {
            "ok": True,
            "detected_state": detected,
            "actions": actions,
            "reason": reason,
        }

    def finalize(self) -> dict:
        """⑥成功收尾：journal 清除（幂等）。候选清空在壳侧 UpdaterState；
        重启仍走 ``updater_restart`` 独立确认（不静默、不自动重启）。"""
        actions: list[str] = []
        if clear_journal(self.paths):
            actions.append("removed_journal")
        self._release_lease()
        return {"ok": True, "actions": actions or ["already_clean"]}
