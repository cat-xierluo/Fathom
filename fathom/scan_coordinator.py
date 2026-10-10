"""API、CLI 与定时入口共用的扫描生命周期和跨进程互斥。

ISS-154 在 ISS-153 的身份/轮次模型上把入口从「单 root」升级为「一轮多
范围」：一轮内多个成员各采各的、各提交各的快照，轮次状态由成员阶段同源
推导（见 ``derive_round_status``）。legacy 单根路径（``start_scan(root=…)``）
逐字节保留 v8 行为——多范围只在显式给出范围规格时启用。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
import uuid

from . import config, db, notify, reports, scan_progress, scanner, storage


class ScanBusyError(RuntimeError):
    """另一个进程（或其仍运行的 du 子进程）持有扫描租约。"""

    def __init__(self, owner: dict | None = None):
        super().__init__("已有扫描在进行中")
        self.owner = owner or {}


class ScanCancelledError(RuntimeError):
    """本次扫描被自己的调用方取消。"""


class ScanRoundTimeoutError(scanner.ScanInterruptedError):
    """整轮有界时限到（ISS-154）：按取消收尾，已提交成员各自保留。"""


class ScanScopeError(ValueError):
    """范围规格不合法（调用方/CLI 参数层，采集尚未开始）。"""


class UpgradeWriteStopError(RuntimeError):
    """升级事务进行中（journal 在位）：停写条件拒绝新扫描会话（ISS-097）。

    判据是 journal 文件**存在性**（不解析内容——损坏同样停写，fail-closed）；
    ``journal`` 仅携带只读诊断（phase/txn_id 等，损坏时为空 dict）。
    API/CLI/定时三入口统一映射为明确文案，事务结束（finalize 成功或显式
    upgrade-rollback）后自动恢复可写。"""

    def __init__(self, journal: dict | None = None):
        super().__init__(
            "升级事务进行中，扫描写入已被拒绝；请待升级完成后重试，"
            "或先经 upgrade-detect 检测并 upgrade-rollback 恢复后再扫描"
        )
        self.journal = journal if isinstance(journal, dict) else {}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ── ISS-154 一轮多范围：规格、计划与轮次状态 ──

#: 本卡的计量版本；参与计划身份（scope + 规范根 + 计量版本 + 阈值 + 掩码）。
METRIC_VERSION = 1

#: 成员阶段词表。running/cancelled/skipped 是本卡新增；done/failed 与快照
#: 有无一一对应（done 必有 snapshot_id，failed 必无）。
MEMBER_PENDING = "pending"
MEMBER_RUNNING = "running"
MEMBER_DONE = "done"
MEMBER_FAILED = "failed"
MEMBER_CANCELLED = "cancelled"
MEMBER_SKIPPED = "skipped"
MEMBER_STATUSES = (
    MEMBER_PENDING, MEMBER_RUNNING, MEMBER_DONE,
    MEMBER_FAILED, MEMBER_CANCELLED, MEMBER_SKIPPED,
)

#: 轮次终态词表。full/partial/failed/cancelled 全部由成员阶段推导，
#: 不接受调用方直接指定（避免「全局状态」与「成员阶段」两个事实源）。
ROUND_FULL = "full"
ROUND_PARTIAL = "partial"
ROUND_FAILED = "failed"
ROUND_CANCELLED = "cancelled"
ROUND_TERMINAL_STATUSES = (
    ROUND_FULL, ROUND_PARTIAL, ROUND_FAILED, ROUND_CANCELLED,
)


def derived_scope_id(canonical_root: str) -> str:
    """路径范围的内容派生稳定 ID（``"path:"`` + sha256 截断）。

    明确**不是**卷身份：显式范围在没有真实发现结果时登记 path 型范围，
    绝不就地伪造 ``apfs-volume:<uuid>``（ISS-153 身份合同）。同一规范根
    重复登记恒得同一 ID，因此重复 --scope 自然落到同一范围。
    """
    return "path:" + hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ScopeSpec:
    """一个待采集范围的规格（采集前的全部输入，尚未落库）。

    ``scope_id`` 缺省时按规范根派生 path 型 ID；``kind`` 缺省 ``path``。
    真实卷/容器范围由发现结果给出显式 ``scope_id`` 与 ``kind``，本卡不
    猜测卷身份。
    """

    root: Path
    scope_id: str | None = None
    kind: str = "path"
    container_id: str | None = None
    volume_group_id: str | None = None
    device_id: str | None = None
    display_name: str | None = None
    min_kb: int | None = None
    exclude_names: str | None = None
    metric_version: int | None = None
    cross_volume: bool = True
    timeout_seconds: float | None = None
    ordinal: int | None = None

    @classmethod
    def from_path(
        cls, path: str | Path, *, scope_id: str | None = None,
        kind: str | None = None, **overrides,
    ) -> "ScopeSpec":
        """由路径构造范围规格（规范化为 realpath 绝对路径）。

        realpath 归一是必要的：``/var`` 与 ``/private/var`` 是同一目录，
        不归一会把同一范围登记成两个数据集。
        """
        raw = Path(path).expanduser()
        if not raw.is_absolute():
            raise ScanScopeError(f"范围路径必须是绝对路径：{path}")
        canonical = str(Path(os.path.realpath(raw)))
        return cls(
            root=Path(canonical),
            scope_id=scope_id or derived_scope_id(canonical),
            kind=kind or "path",
            **overrides,
        )

    def resolved(self, pinned: scanner.PinnedScanConfig) -> "ScopeSpec":
        """补齐缺省口径（钉住值），得到可登记/可采集的完整规格。"""
        return dataclasses.replace(
            self,
            min_kb=config.MIN_DIR_KB if pinned is None else pinned.min_kb
            if self.min_kb is None else int(self.min_kb),
            exclude_names=(
                ";".join(config.EXCLUDE_NAMES) if pinned is None
                else pinned.exclude_names_canonical
            ) if self.exclude_names is None else self.exclude_names,
            metric_version=(
                METRIC_VERSION if pinned is None else pinned.metric_version
            ) if self.metric_version is None else int(self.metric_version),
            timeout_seconds=(
                config.DU_TIMEOUT_S if pinned is None else pinned.du_timeout_s
            ) if self.timeout_seconds is None else float(self.timeout_seconds),
        )


@dataclass(frozen=True)
class RoundMember:
    """轮次里一个已去重、已登记的成员（= 一个数据集计划的一次采集）。"""

    seq: int
    spec: ScopeSpec
    scope_id: str
    plan_id: str
    min_kb: int
    exclude_names: str
    metric_version: int
    cross_volume: bool
    timeout_seconds: float

    @property
    def root_str(self) -> str:
        return str(self.spec.root)


@dataclass(frozen=True)
class RoundPlan:
    """一轮的范围计划（去重后、已开启轮次）。"""

    round_id: int
    started_at: str
    pinned: scanner.PinnedScanConfig
    members: tuple[RoundMember, ...]
    #: 范围输入顺序 -> 成员 seq。**范围顺序即采集顺序**；同一规范根的多个
    #: 身份保持各自输入次序（身份不同即不同数据集，见 ISS-153 合同）。
    ordinal_of: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.members)


def derive_round_status(counts: dict[str, int]) -> str:
    """由成员阶段计数推导轮次终态——全局状态与成员阶段同源。

    规则（顺序即优先级）：

    1. 有成员被取消（含整轮时限到）→ ``cancelled``：取消优先于任何成功，
       因为剩余成员根本没采，不存在「本轮成功」这回事。
    2. 全部成员成功（无失败/取消/跳过）→ ``full``。
    3. 零成功 → ``failed``（全灭，或空计划）。
    4. 其余（有成功也有失败/跳过）→ ``partial``。

    刻意**不产出**「本轮整体用量百分比」一类聚合口径：成员是在不同时刻
    采的，跨时刻求和/占比都是伪造的原子时点。
    """
    cancelled = counts.get(MEMBER_CANCELLED, 0) + counts.get(MEMBER_SKIPPED, 0)
    done = counts.get(MEMBER_DONE, 0)
    failed = counts.get(MEMBER_FAILED, 0)
    if cancelled:
        return ROUND_CANCELLED
    if failed == 0 and done > 0:
        return ROUND_FULL
    if done == 0:
        return ROUND_FAILED
    return ROUND_PARTIAL


def dedupe_scope_specs(
    specs: list[ScopeSpec], pinned: scanner.PinnedScanConfig,
) -> list[ScopeSpec]:
    """按计划身份五元组去重，保留首次出现的位置。

    身份 = (scope_id, canonical_root, metric_version, min_kb, exclude_names)
    ——与 ISS-153 ``plan_identity_id`` 完全同维。重复项只采一次；顺序即
    首次出现顺序，因此范围顺序（采集顺序）不被去重打乱。
    """
    seen: set[tuple] = set()
    unique: list[ScopeSpec] = []
    for spec in specs:
        resolved = spec.resolved(pinned)
        key = (resolved.scope_id, str(resolved.root), resolved.metric_version,
               resolved.min_kb, resolved.exclude_names)
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _validate_scope_registrations(conn, specs: list[ScopeSpec]) -> None:
    """首见身份不可覆写；旧错误登记必须明确停止，而非伪称已修复。"""
    seen = {}
    for spec in specs:
        identity = (spec.kind, spec.container_id, spec.volume_group_id, spec.device_id)
        if spec.scope_id in seen and seen[spec.scope_id] != identity:
            raise ScanScopeError("同一范围 ID 的身份冲突，未开始采集。")
        seen[spec.scope_id] = identity
        row = conn.execute(
            "SELECT kind, container_id, volume_group_id, device_id FROM scan_scopes WHERE scope_id=?",
            (spec.scope_id,),
        ).fetchone()
        if row is not None and tuple(row) != identity:
            raise ScanScopeError(
                "范围首见身份与本次发现不一致，未开始采集；旧历史保留，需另行处理身份迁移。")


def build_round_plan(
    conn, specs: list[ScopeSpec], *, pinned: scanner.PinnedScanConfig | None = None,
    round_id: int | None = None, started_at: str | None = None,
) -> RoundPlan:
    """登记去重后的范围计划并开启一轮，返回 ``RoundPlan``。

    身份登记复用 ISS-153 的 ``ensure_scan_scope`` / ``ensure_scan_plan``：
    本函数不新造身份语义（同 plan 重复登记幂等），只负责把一轮的范围顺序
    固化成成员序列。``round_id`` 为 None 时新开一轮。

    调用方（协调器）必须已持有扫描租约——轮次与单根扫描共用同一把
    跨进程 flock 互斥。
    """
    pinned = pinned or scanner.PinnedScanConfig.capture(
        metric_version=METRIC_VERSION
    )
    _validate_scope_registrations(conn, specs)
    unique = dedupe_scope_specs(list(specs), pinned)
    stamp = started_at or _now()
    ordinal_of: dict[str, int] = {}
    for index, spec in enumerate(unique):
        ordinal_of.setdefault(str(spec.root), index)
    rid = (round_id if round_id is not None
           else scanner.begin_scan_round(conn, stamp))
    members: list[RoundMember] = []
    for seq, spec in enumerate(unique):
        scanner.ensure_scan_scope(
            conn, spec.scope_id, spec.kind,
            container_id=spec.container_id,
            volume_group_id=spec.volume_group_id,
            device_id=spec.device_id,
            mount_path=str(spec.root),
            display_name=spec.display_name,
            seen_at=stamp,
        )
        plan_id = scanner.ensure_scan_plan(
            conn, spec.scope_id, str(spec.root), spec.metric_version,
            spec.min_kb, spec.exclude_names, created_at=stamp,
        )
        scanner.add_round_member(
            conn, rid, seq=seq, status=MEMBER_PENDING,
            plan_id=plan_id, scope_id=spec.scope_id, started_at=None,
        )
        members.append(RoundMember(
            seq=seq, spec=spec, scope_id=spec.scope_id, plan_id=plan_id,
            min_kb=int(spec.min_kb), exclude_names=spec.exclude_names,
            metric_version=int(spec.metric_version),
            cross_volume=bool(spec.cross_volume),
            timeout_seconds=float(spec.timeout_seconds),
        ))
    conn.commit()
    return RoundPlan(round_id=rid, started_at=stamp, pinned=pinned,
                     members=tuple(members), ordinal_of=ordinal_of)


def round_member_statuses(conn, round_id: int) -> dict[str, int]:
    """读取一轮的成员阶段计数（状态词表外的取值归入 failed 之外的原样键）。"""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM scan_round_members "
        "WHERE round_id=? GROUP BY status", (round_id,),
    ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def round_member_rows(conn, round_id: int) -> list[dict]:
    """按 seq 读出一轮全部成员（含计划/范围/快照引用与各自起止时刻）。"""
    # 采集口径列（目录数/用量/质量/时刻）一律取自 snapshots 侧的 sn 快照
    # 行；scan_scopes 侧的 s 只有身份列。失败成员没有快照引用，因此这些列
    # 为 NULL 就是「本轮无新数据」，报告如实显示「—」。
    rows = conn.execute(
        "SELECT m.*, p.canonical_root, p.metric_version, p.min_kb, "
        "p.exclude_names, s.display_name, s.kind AS scope_kind, "
        "s.container_id, sn.created_at AS snapshot_created_at, "
        "sn.dir_count, sn.denied_count, sn.total_kb, sn.collection_status, "
        "sn.du_seconds "
        "FROM scan_round_members m "
        "LEFT JOIN scan_plans p ON p.plan_id = m.plan_id "
        "LEFT JOIN scan_scopes s ON s.scope_id = m.scope_id "
        "LEFT JOIN snapshots sn ON sn.id = m.snapshot_id "
        "WHERE m.round_id=? ORDER BY m.seq", (round_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def round_capacity_samples(conn, round_id: int) -> list[dict]:
    """读出一轮落下的容量样本（诊断用：谁、何时、哪条来源、什么值）。"""
    rows = conn.execute(
        "SELECT * FROM container_capacity_samples WHERE round_id=? "
        "ORDER BY id", (round_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def round_time_span(conn, round_id: int) -> dict:
    """本轮成员各自起止时刻所覆盖的时间跨度（**不是**一个原子时点）。

    成员是顺序采的，时间各不相同：把跨度压成一个「本轮时点」就是把不同时刻
    的数据说成同一瞬间。因此这里如实给出最早开始、最晚结束、成员各自
    时刻，以及 ``atomic: False`` 这样的显式标记，供报告与 API 如实披露。
    """
    rows = conn.execute(
        "SELECT started_at, finished_at FROM scan_round_members "
        "WHERE round_id=? ORDER BY seq", (round_id,),
    ).fetchall()
    starts = [r["started_at"] for r in rows if r["started_at"]]
    ends = [r["finished_at"] for r in rows if r["finished_at"]]
    snap_times = [
        r["snapshot_created_at"] for r in round_member_rows(conn, round_id)
        if r.get("snapshot_created_at")
    ]
    return {
        "started_at": min(starts) if starts else None,
        "finished_at": max(ends) if ends else None,
        "snapshot_times": snap_times,
        "atomic": False,
        "note": "成员按顺序采集，时间各不相同；不可当作同一时点的原子快照",
    }


def round_container_free_bytes(conn, round_id: int) -> int | None:
    """本轮容器样本里的共享剩余空间（轮次通知的容量主体）。

    取本轮**最后一条容器级**样本；一轮内没有容器级样本就返回 None
    （未知就是不阈值化，不拿目录 statvfs 冒充整盘主体）。
    """
    row = conn.execute(
        "SELECT free_bytes FROM container_capacity_samples "
        "WHERE round_id=? AND source=? AND free_bytes IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (round_id, "storage-discovery"),
    ).fetchone()
    return None if row is None else int(row["free_bytes"])


def _round_reason(exc: BaseException) -> str:
    """成员失败原因文本（截尾、限长；不泄露无关内部细节）。"""
    text = " ".join((str(exc) or exc.__class__.__name__).split())
    return text[:200]


_ROUND_STATUS_TEXT = {
    ROUND_FULL: "本轮全部范围完成",
    ROUND_PARTIAL: "本轮部分范围完成",
    ROUND_FAILED: "本轮全部范围失败",
    ROUND_CANCELLED: "本轮已取消",
}


def _round_summary_text(status: str, members: list[dict], span: dict,
                        capacity: dict) -> str:
    """轮次 message 文本：状态 + 成功/失败计数 + 时间跨度 + 容量是否已知。

    只报事实计数，不报「整体用量百分比」：成员在不同时刻采，聚合百分比
    会把不同时间的数据说成一个原子时点的结论。
    """
    done = sum(1 for m in members if m["status"] == MEMBER_DONE)
    failed = sum(1 for m in members if m["status"] == MEMBER_FAILED)
    parts = [
        _ROUND_STATUS_TEXT.get(status, status),
        f"成功 {done} 个范围",
    ]
    if failed:
        parts.append(f"失败 {failed} 个")
    started, finished = span.get("started_at"), span.get("finished_at")
    if started and finished:
        parts.append(f"跨度 {started} → {finished}（各范围采集时刻不同）")
    parts.append(
        "整盘容量已更新" if capacity.get("status") == "sampled"
        else "整盘容量保持上次已知值"
    )
    return "；".join(parts)


# 容量样本来源常量。ISS-153 合同：整盘容量只能来自容器级发现；statvfs
# 来源只用于快照级 volume_stats，绝不冒充容器/整盘样本。
CAPACITY_SOURCE_DISCOVERY = "storage-discovery"


@dataclass(frozen=True)
class CapacityReading:
    """一条待落库的容量读数（容器 / 卷 / 其他设备分区）。

    ``free_bytes`` 只在容器级有值：同容器全部卷共享剩余空间，卷级 free
    是错误口径（两卷相加会重复计账），故卷读数一律 free=None。
    """

    subject_id: str
    kind: str                  # container | volume | device
    total_bytes: int | None
    free_bytes: int | None
    source: str
    label: str | None = None


def capacity_readings_from_discovery(
    discovery: storage.StorageDiscovery,
) -> list[CapacityReading]:
    """把 ISS-152 的发现结果读成容量口径（storage.py 的只读消费）。

    - 容器：total 取 capacity_ceiling_bytes、free 取 shared_free_bytes
      （唯一权威 free）；两项任一为 None 就留空，不互相推算。
    - 卷：只取 capacity_in_use_bytes 作为「在用」，free 恒 None。
    - 其他设备分区：只取 capacity_bytes，free 恒 None。

    发现不可用（无启动容器）时返回空列表——调用方据此保持未知/旧时间。
    """
    readings: list[CapacityReading] = []
    container = discovery.startup_container
    if container is not None:
        readings.append(CapacityReading(
            subject_id=container.container_id, kind="container",
            total_bytes=container.capacity_ceiling_bytes,
            free_bytes=container.shared_free_bytes,
            source=CAPACITY_SOURCE_DISCOVERY, label=container.container_reference,
        ))
    for volume in discovery.startup_volumes:
        readings.append(CapacityReading(
            subject_id=volume.volume_id, kind="volume",
            total_bytes=volume.capacity_in_use_bytes, free_bytes=None,
            source=CAPACITY_SOURCE_DISCOVERY,
            label=volume.name or volume.device_identifier,
        ))
    for device in discovery.other_devices:
        readings.append(CapacityReading(
            subject_id=device.device_id, kind="device",
            total_bytes=device.capacity_bytes, free_bytes=None,
            source=CAPACITY_SOURCE_DISCOVERY,
            label=device.name or device.device_identifier,
        ))
    return readings


def read_capacity_readings() -> list[CapacityReading]:
    """执行一次只读发现并读出容量口径。

    任何失败（命令缺失、超时、解析不了）都**不**退回目录 statvfs：返回
    空列表即「本轮无新的整盘容量样本」，历史样本与时间原样保留。
    """
    try:
        discovery = storage.discover_startup()
    except Exception:  # noqa: BLE001 - 发现不可用不是扫描失败
        return []
    return capacity_readings_from_discovery(discovery)


def sample_round_capacity(conn, round_id: int) -> dict:
    """轮末落一组独立的容器/卷容量样本，返回本轮容量摘要。

    「至少轮末有一条独立容量样本」是硬保证：只要发现读到任何一条读数就
    落库（来源与时间显式），读到零条/失败则一条都不落，并把摘要如实记为
    unavailable——**不拿目录 statvfs 替补整盘容量**。
    """
    summary = {"round_id": round_id, "status": "unavailable",
               "samples": 0, "container_id": None, "free_bytes": None,
               "total_bytes": None, "sampled_at": None}
    try:
        readings = read_capacity_readings()
    except Exception:  # noqa: BLE001 - 诊断通道故障不改变轮次成败
        readings = []
    if not readings:
        return summary
    sampled_at = _now()
    for reading in readings:
        scanner.record_container_capacity_sample(
            conn, reading.subject_id, source=reading.source,
            total_bytes=reading.total_bytes, free_bytes=reading.free_bytes,
            sampled_at=sampled_at, round_id=round_id,
        )
    conn.commit()
    container = next((r for r in readings if r.kind == "container"), None)
    summary.update({
        "status": "sampled", "samples": len(readings),
        "container_id": container.subject_id if container else None,
        "free_bytes": container.free_bytes if container else None,
        "total_bytes": container.total_bytes if container else None,
        "sampled_at": sampled_at,
    })
    return summary


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
    """已取得租约、已持久化 running 状态的一次扫描。

    ISS-154：``scopes`` 非空时本会话执行**一轮多范围**（成员逐个采集、
    各自提交快照、轮次状态由成员阶段推导）；``scopes`` 为空/None 时走
    原单根路径，行为与 v8 逐字节一致。两种形态共用同一把跨进程租约与
    同一套停写条件——互斥与停写覆盖的是整个会话（整轮），不是单个成员。
    """

    def __init__(self, lease: ScanLease, run_id: int, source: str, root: Path,
                 scopes: list["ScopeSpec"] | None = None,
                 round_timeout_seconds: float | None = None):
        self.lease = lease
        self.run_id = run_id
        self.source = source
        self.root = root
        self.scopes = list(scopes) if scopes else []
        self.round_timeout_seconds = round_timeout_seconds
        self.cancel_event = threading.Event()
        # ISS-061：du 超时由 FATHOM_DU_TIMEOUT_S（默认 14400s）覆盖，
        # 不再硬编码 3600。超时会作为 ScanInterruptedError 冒到 execute()
        # 外层，扫描记为 status=interrupted 且保留上次有效快照。
        # ISS-154：多范围形态下这是**每范围**时限，整轮另有有界时限。
        self.du_timeout_seconds = config.DU_TIMEOUT_S
        self._finished = False
        # ISS-090：du 流式进度写入器（跨进程状态文件）。start/close 的
        # 一切失败都被写入器自身吞掉——进度通道故障绝不影响扫描本体。
        self._progress = scan_progress.ProgressReporter(run_id=run_id)

    @property
    def is_round(self) -> bool:
        return bool(self.scopes)

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
        if self.is_round:
            return self.execute_round()
        return self.execute_single_root()

    # ── ISS-154 一轮多范围 ──

    def _round_budget_seconds(self, plan: RoundPlan) -> float:
        """整轮有界时限：显式值 > 「每范围时限 × 成员数」。

        缺省给每个成员一份完整的 du 时限（真实生产：一次 du 要几十分钟，
        不能因为多几个范围就被整体截断），因此整轮默认不会比单根更早
        放弃；同时它仍是**有界**的——成员数再大也有上限。
        """
        if self.round_timeout_seconds is not None:
            return float(self.round_timeout_seconds)
        return sum(m.timeout_seconds for m in plan.members) or 1.0

    def _update_member(
        self, round_id: int, seq: int, *, status: str | None = None,
        snapshot_id: int | None = None, keep_snapshot: bool = False,
        started_at: str | None = None, finished_at: str | None = None,
    ) -> None:
        """更新一个成员行（状态/快照引用/各自起止时刻）。"""
        if status is not None and status not in MEMBER_STATUSES:
            raise ValueError(f"未知成员阶段：{status}")
        assignments: list[str] = []
        params: list[object] = []
        if status is not None:
            assignments.append("status=?")
            params.append(status)
        if snapshot_id is not None:
            assignments.append("snapshot_id=?")
            params.append(snapshot_id)
            assignments.append("snapshot_status='active'")
        if not keep_snapshot:
            # done 之外的终态一律不挂快照引用：失败成员的「本轮无新数据」
            # 必须可从行本身读出，而不是靠 snapshot_id 为 NULL 的巧合。
            if status is not None and status != MEMBER_DONE:
                assignments.append("snapshot_id=NULL")
        if started_at is not None:
            assignments.append("started_at=?")
            params.append(started_at)
        if finished_at is not None:
            assignments.append("finished_at=?")
            params.append(finished_at)
        if not assignments:
            return
        params.extend([round_id, seq])
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE scan_round_members SET "
                + ", ".join(assignments) + " WHERE round_id=? AND seq=?",
                params,
            )
            conn.commit()
        finally:
            conn.close()

    def _cancel_point(self, where: str) -> None:
        """协作式取消点：范围之间与收尾阶段都会经过这里。"""
        if self.cancel_event.is_set():
            raise ScanCancelledError(f"扫描在{where}被取消")

    def _collect_member(self, plan: RoundPlan, member: RoundMember) -> dict:
        """采一个范围并提交它自己的快照，返回该成员的结果记录。

        成功：提交新快照（成员计划身份 + 轮次 id），返回 snapshot_id。
        失败：抛出的采集错误由调用方记账；**这里不做任何补救写入**——
        失败范围的旧样本与旧时间原样保留（不是本轮成功）。
        """
        self._update_member(plan.round_id, member.seq,
                            status=MEMBER_RUNNING, started_at=_now())
        pinned = dataclasses.replace(
            plan.pinned,
            min_kb=member.min_kb,
            exclude_names=tuple(m for m in member.exclude_names.split(";") if m),
            metric_version=member.metric_version,
            cross_volume=member.cross_volume,
            du_timeout_s=member.timeout_seconds,
        )
        conn = db.connect()
        try:
            with scanner.pinned_scan_config(pinned):
                with scanner.du_process_context(
                    inherited_fd=self.lease.fd, cancel_event=self.cancel_event,
                    timeout_seconds=member.timeout_seconds,
                    progress=self._progress,
                ):
                    # 采集进程上下文就绪后再确认一次取消：取消请求可能正好
                    # 落在「本成员已登记 running、du 子进程刚就位」这个窗口，
                    # 此时必须**不**再启动这次 du（本轮自有进程一个都不多生）。
                    self._cancel_point("范围采集前")
                    sid = scanner.create_snapshot(
                        conn, member.spec.root, member.min_kb,
                        plan_id=member.plan_id, round_id=plan.round_id,
                        metric_version=member.metric_version,
                    )
            return {"snapshot_id": int(sid)}
        finally:
            conn.close()

    def _mark_remaining(self, plan: RoundPlan, from_seq: int,
                        status: str) -> None:
        """把尚未开始的成员一次性标成 skipped（保留其 seq 与计划身份）。"""
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE scan_round_members SET status=?, snapshot_id=NULL, "
                "finished_at=? WHERE round_id=? AND seq>=? AND status IN (?,?)",
                (status, _now(), plan.round_id, from_seq,
                 MEMBER_PENDING, MEMBER_RUNNING),
            )
            conn.commit()
        finally:
            conn.close()

    def _run_round_members(self, plan: RoundPlan, deadline: float,
                           warnings: list[str]) -> list[dict]:
        """按范围顺序逐个采集，成员失败不拖垮整轮。"""
        results: list[dict] = []
        for member in plan.members:
            if self.cancel_event.is_set():
                # 取消落在两个范围之间：本成员及之后都**没开始采**，一律记
                # skipped（不是 failed——它们不是采集失败，是本轮不采了）。
                self._mark_remaining(plan, member.seq, MEMBER_SKIPPED)
                raise ScanCancelledError("扫描在范围开始前被取消")
            if time.monotonic() >= deadline:
                # 整轮有界时限到：未开始的成员不算失败（本轮不采），
                # 轮次按 cancelled 收尾，已提交成员各自保留。
                self._mark_remaining(plan, member.seq, MEMBER_SKIPPED)
                raise ScanRoundTimeoutError(
                    f"整轮扫描超出有界时限（{len(plan.members)} 个范围，"
                    f"第 {member.seq + 1} 个未开始）"
                )
            record: dict = {
                "seq": member.seq, "scope_id": member.scope_id,
                "plan_id": member.plan_id, "root": member.root_str,
                "status": MEMBER_FAILED, "snapshot_id": None, "reason": None,
            }
            try:
                collected = self._collect_member(plan, member)
            except (KeyboardInterrupt, ScanCancelledError,
                    scanner.ScanInterruptedError) as exc:
                # 取消/时限是整轮级终态：当前成员记 cancelled、未开始的记
                # skipped，已提交的成员各自保留，然后向上冒泡收尾。
                self._update_member(plan.round_id, member.seq,
                                    status=MEMBER_CANCELLED,
                                    finished_at=_now())
                self._mark_remaining(plan, member.seq + 1, MEMBER_SKIPPED)
                raise
            except Exception as exc:  # noqa: BLE001 - 单范围失败不拖垮整轮
                reason = _round_reason(exc)
                self._update_member(plan.round_id, member.seq,
                                    status=MEMBER_FAILED, finished_at=_now())
                record["reason"] = reason
                warnings.append(f"范围 {member.root_str} 采集失败：{reason}")
            else:
                record["status"] = MEMBER_DONE
                record["snapshot_id"] = collected["snapshot_id"]
                self._update_member(plan.round_id, member.seq,
                                    status=MEMBER_DONE,
                                    snapshot_id=collected["snapshot_id"],
                                    finished_at=_now())
            results.append(record)
            self._cancel_point("范围结束后")
        return results

    def _finish_round(self, plan: RoundPlan, status: str,
                      message: str | None = None) -> None:
        """写轮次终态（由成员阶段推导得出，不另设事实源）。"""
        conn = db.connect()
        try:
            scanner.finish_scan_round(
                conn, plan.round_id, status, message=message
            )
            conn.commit()
        finally:
            conn.close()

    def execute_round(self) -> dict:
        """一轮多范围：采集 → 容量采样 → 每范围报告 → 轮次汇总与通知。

        与单根路径共享租约/停写/进度/保留清理；差异在「成员各自提交 +
        轮次状态同源推导 + 部分成功不发全完成通知」。
        """
        conn = None
        plan: RoundPlan | None = None
        try:
            self._cancel_point("启动前")
            self._update(phase="planning")
            self._progress.start()
            conn = db.connect()
            plan = build_round_plan(conn, self.scopes)
            members = plan.members
            if not members:
                raise ScanScopeError("范围计划去重后为空；本轮没有任何范围可采")
            round_timeout = self._round_budget_seconds(plan)
            deadline = time.monotonic() + round_timeout
            self._update(phase="snapshot")

            warnings: list[str] = []
            try:
                member_results = self._run_round_members(
                    plan, deadline, warnings
                )
            except (KeyboardInterrupt, ScanCancelledError,
                    scanner.ScanInterruptedError) as exc:
                return self._round_cancelled(exc, plan)

            # 轮末独立容量样本（ISS-154 合同 3）：只读消费 storage 发现。
            self._cancel_point("容量采样前")
            conn.close()
            conn = db.connect()
            capacity = sample_round_capacity(conn, plan.round_id)
            if capacity["status"] != "sampled":
                warnings.append("本轮未取得新的整盘容量样本；整盘容量保持上次已知值")

            conn.close()
            conn = db.connect()
            counts = round_member_statuses(conn, plan.round_id)
            status = derive_round_status(counts)
            span = round_time_span(conn, plan.round_id)
            report_paths: list[str] = []
            self._update(phase="report")
            for record in member_results:
                if record["status"] != MEMBER_DONE:
                    continue
                outcome = self._write_member_report(record, warnings)
                if outcome:
                    report_paths.append(outcome)

            round_report = self._write_round_report(
                conn, plan, status, member_results, capacity, span, warnings
            )
            if round_report:
                report_paths.append(str(round_report))
            self._update(
                phase="notification",
                report_status="written" if report_paths else "not_available",
                report_path=str(round_report) if round_report else None,
            )

            notification_status = self._notify_round(
                conn, plan, status, member_results, capacity
            )
            self._update(phase="retention", notification_status=notification_status)

            pruned = 0
            try:
                conn.close()
                conn = db.connect()
                pruned = scanner.prune_snapshots(conn)
            except Exception as exc:  # noqa: BLE001
                if conn is not None:
                    conn.rollback()
                warnings.append(f"保留策略执行失败：{exc}")
            finally:
                if conn is not None:
                    conn.close()
                conn = None

            file_pruned_total = 0
            for kind, prune_fn in (("reports", reports.prune_reports),
                                   ("logs", reports.prune_logs)):
                try:
                    deleted, file_warnings = prune_fn()
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{kind} 保留清理失败：{exc}")
                    continue
                file_pruned_total += deleted
                warnings.extend(file_warnings)
            if file_pruned_total:
                warnings.append(f"清理运行根过期文件 {file_pruned_total} 份")

            result = {
                "round_id": plan.round_id,
                "round_status": status,
                "scopes": len(plan.members),
                "snapshot_ids": [r["snapshot_id"] for r in member_results
                                 if r["status"] == MEMBER_DONE],
                "members": [
                    {k: v for k, v in record.items() if k != "seq"}
                    for record in member_results
                ],
                "time_span": span,
                "capacity": capacity,
                "report": str(round_report) if round_report else None,
                "reports": report_paths,
                "notification_status": notification_status,
                "pruned": pruned,
                "warnings": warnings,
                "source": self.source,
            }
            self._update(phase="finalizing", pruned_count=pruned)
            if status == ROUND_CANCELLED:
                # 轮次级取消（如收尾阶段被取消）仍按非成功收尾。
                self._finish_round(plan, status, "本轮在收尾阶段被取消")
                self._finish("interrupted", result)
                return result
            self._finish_round(plan, status, _round_summary_text(
                status, member_results, span, capacity))
            self._finish("done", result)
            return result
        except (KeyboardInterrupt, ScanCancelledError,
                scanner.ScanInterruptedError) as exc:
            if conn is not None:
                conn.rollback()
            if plan is not None:
                try:
                    conn = db.connect()
                    self._finish_round(
                        plan, ROUND_CANCELLED, str(exc) or "扫描被取消"
                    )
                finally:
                    if conn is not None:
                        conn.close()
                        conn = None
            try:
                notify.notify_scan_interrupted(str(exc) or "扫描被取消")
            except Exception:  # noqa: BLE001
                pass
            self._finish("interrupted", str(exc) or "扫描被取消")
            raise
        except Exception as exc:  # noqa: BLE001
            if conn is not None:
                conn.rollback()
            if plan is not None:
                try:
                    conn2 = db.connect()
                    self._finish_round(plan, ROUND_FAILED, str(exc))
                    conn2.close()
                except Exception:  # noqa: BLE001
                    pass
            self._finish("failed", str(exc))
            raise
        finally:
            if conn is not None:
                conn.close()
            self._progress.close()
            self.lease.release()

    def _round_cancelled(self, exc: BaseException, plan: RoundPlan) -> dict:
        """采集阶段被取消/超时的收尾：已提交成员各自保留，轮次记 cancelled。"""
        conn = db.connect()
        try:
            span = round_time_span(conn, plan.round_id)
            self._finish_round(plan, ROUND_CANCELLED, str(exc) or "扫描被取消")
        finally:
            conn.close()
        try:
            notify.notify_scan_interrupted(str(exc) or "扫描被取消")
        except Exception:  # noqa: BLE001
            pass
        self._finish("interrupted", str(exc) or "扫描被取消")
        raise exc

    def _write_member_report(self, record: dict, warnings: list[str]) -> str | None:
        """为成功的成员各写一份同计划对比日报（每范围可比依据）。"""
        sid = record["snapshot_id"]
        try:
            conn = db.connect()
            try:
                return str(reports.write_daily_report(
                    conn, sid, notify_after_write=False
                ))
            except ValueError as exc:
                # 有效首扫没有同数据集基线，是成功快照而不是运行失败。
                if "至少需要两个快照" in str(exc):
                    warnings.append(f"范围 {record['root']}：{exc}")
                else:
                    warnings.append(f"范围 {record['root']} 日报生成失败：{exc}")
                return None
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"范围 {record['root']} 日报生成失败：{exc}")
                return None
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"范围 {record['root']} 日报生成失败：{exc}")
            return None

    def _write_round_report(
        self, conn, plan: RoundPlan, status: str, member_results: list[dict],
        capacity: dict, span: dict, warnings: list[str],
    ) -> Path | None:
        try:
            return reports.write_round_report(
                conn, plan.round_id, round_status=status,
                members=round_member_rows(conn, plan.round_id),
                capacity=capacity, time_span=span,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"轮次汇总生成失败：{exc}")
            return None

    def _notify_round(
        self, conn, plan: RoundPlan, status: str,
        member_results: list[dict], capacity: dict,
    ) -> str:
        """轮次通知：只有 full 才逐范围发「完成」，其余只发一条轮次通知。

        部分成功绝不发「整盘扫描全部完成」类文案——那句文案会让用户以为
        本轮所有范围都拿到了当前数据。轮次通知的容量主体取本轮容器样本
        （未知则不阈值化，不拿目录 statvfs 冒充）。
        """
        if status == ROUND_FULL:
            for record in member_results:
                if record["status"] != MEMBER_DONE:
                    continue
                try:
                    reports.notify_for_snapshot(conn, record["snapshot_id"])
                except Exception:  # noqa: BLE001
                    continue
        free_bytes = round_container_free_bytes(conn, plan.round_id)
        try:
            return ("submitted" if notify.notify_scan_round(
                status=status,
                members=[
                    {"root": r["root"], "scope_id": r["scope_id"],
                     "status": r["status"], "reason": r.get("reason")}
                    for r in member_results
                ],
                free_bytes=free_bytes,
                capacity_known=capacity.get("status") == "sampled",
            ) else "failed")
        except Exception:  # noqa: BLE001
            return "failed"

    def execute_single_root(self) -> dict:
        """原单根扫描路径（ISS-154 未改其任何一步）。"""
        conn = None
        sid: int | None = None
        try:
            if self.cancel_event.is_set():
                raise ScanCancelledError("扫描在启动前被取消")
            self._update(phase="snapshot")
            self._progress.start()  # ISS-090：落 live 初值（写失败自吞）
            conn = db.connect()
            with scanner.du_process_context(
                inherited_fd=self.lease.fd, cancel_event=self.cancel_event,
                timeout_seconds=self.du_timeout_seconds,
                progress=self._progress,
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
            elif report_status == "not_available" and sid is not None:
                # ISS-003A：首扫没有同数据集基线，不发对比/完成文案，
                # 改发"首次快照已建立"；失败同样不影响快照。
                try:
                    conn = db.connect()
                    notification_status = (
                        "submitted"
                        if reports.notify_first_snapshot_for(conn, sid) else "failed"
                    )
                    if notification_status == "failed":
                        warnings.append("首次快照通知未提交；快照不受影响")
                except Exception as exc:
                    notification_status = "failed"
                    warnings.append(f"首次快照通知失败：{exc}")
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

            # ISS-050：保留阶段顺手清理运行根 reports/ 与 logs/ 内的过期
            # 文件。快照与日报字段不动；删除数通过 warnings 文本可见，避免
            # 与 ``pruned_count`` 的快照语义混淆。
            file_pruned_total = 0
            for kind, prune_fn in (
                ("reports", reports.prune_reports),
                ("logs", reports.prune_logs),
            ):
                try:
                    deleted, file_warnings = prune_fn()
                except Exception as exc:
                    warnings.append(f"{kind} 保留清理失败：{exc}")
                    continue
                file_pruned_total += deleted
                warnings.extend(file_warnings)
            if file_pruned_total:
                warnings.append(f"清理运行根过期文件 {file_pruned_total} 份")
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
            # ISS-003A：中断/超时绝不发"完成"通知，只发标题明确"已中断"
            # 的通知（notify 自吞全部异常；不改锁与扫描语义）。
            try:
                notify.notify_scan_interrupted(str(exc) or "扫描被取消")
            except Exception:
                pass  # 双保险：通知路径任何失败都不改变中断收尾
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
            # ISS-090：扫描结束（成功/中断/失败）统一清理 live 进度文件；
            # 进程崩溃残留的 stale 文件由读取方按 heartbeat 判活兜底。
            self._progress.close()
            self.lease.release()


def _lock_path() -> Path:
    return config.DB_PATH.with_name(config.DB_PATH.name + ".scan.lock")


def _refuse_writes_during_upgrade_txn() -> None:
    """ISS-097 停写条件：升级事务 journal 在位时拒绝新扫描会话。

    次序不变量（防 TOCTOU）：本检查在**取得 ScanLease 之后**执行，升级方
    （fathom.upgrade）则在取得租约之后、写 journal 之前先查残留事务——
    「写入已开始」与「事务已建立」不可能同时成立。判据是文件存在性
    （损坏同样停写，fail-closed）；内容只读附带给异常作诊断。延迟导入
    upgrade 是为避免环（upgrade 模块级反向依赖本模块）。"""
    from . import upgrade
    journal_path = upgrade.journal_path_from_db(config.DB_PATH)
    if not journal_path.exists():
        return
    raise UpgradeWriteStopError(upgrade.peek_journal(journal_path))


def start_scan(
    *, source: str, root: Path | None = None,
    scopes: list["ScopeSpec"] | None = None,
    round_timeout_seconds: float | None = None,
) -> ScanSession:
    """非阻塞取得全局租约并落 running；busy 时不新建运行记录。

    ISS-097：取得租约后先过停写条件——升级事务 journal 在位即拒绝
    （``UpgradeWriteStopError``，不建运行记录、不留 running 残行）。

    ISS-154：``scopes`` 非空时本会话执行一轮多范围。互斥与停写仍然
    覆盖**整个会话（整轮所有范围）**——不会因为多范围而让并发扫描或
    升级事务插进中间。
    """
    if source not in {"api", "cli", "scheduled"}:
        raise ValueError(f"未知扫描来源：{source}")
    # RuntimeConfig 已负责生产配置规范化；这里保留调用方路径字符串身份，避免
    # /var 与 /private/var 等系统别名导致 du 根记录和 snapshot.root 不一致。
    target_root = Path(root or config.DEFAULT_ROOT).expanduser()
    if scopes and root is not None:
        raise ScanScopeError("显式范围与 --root 不能同时给出；二者都是采集入口")
    lease = ScanLease.acquire(_lock_path(), source=source)
    conn = None
    try:
        _refuse_writes_during_upgrade_txn()
        conn = db.connect()
        if scopes:
            _validate_scope_registrations(conn, scopes)
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
        return ScanSession(lease, run_id, source, target_root, scopes=scopes,
                           round_timeout_seconds=round_timeout_seconds)
    except Exception:
        if conn is not None:
            conn.rollback()
        lease.release()
        raise
    finally:
        if conn is not None:
            conn.close()


def run_scan(
    *, source: str, root: Path | None = None,
    scopes: list["ScopeSpec"] | None = None,
    round_timeout_seconds: float | None = None,
) -> tuple[int, dict]:
    session = start_scan(source=source, root=root, scopes=scopes,
                         round_timeout_seconds=round_timeout_seconds)
    return session.run_id, session.execute()


def humanize_run_message(message: str | None, *, status: str | None,
                         snapshot_id: object = None,
                         report_status: str | None = None,
                         notification_status: str | None = None,
                         pruned_count: object = None) -> str:
    """把 scan_runs.message 转为可读摘要，仅供 API 展示（ISS-109）。

    done 行的 message 在 DB 中是 result JSON（latest_scan_state 依赖该合同），
    直接展示会成为原始 JSON dump。本函数在 API 输出层转换：done 且能解析出
    结构化字段时拼接事实摘要（快照/日报/通知/清理条数），否则原样返回——
    interrupted/failed 的 message 本就是人话。不改 DB 内容。
    """
    if status != "done" or not message:
        return message or ""
    result: object = None
    try:
        result = json.loads(message)
    except ValueError:
        result = None
    if not isinstance(result, dict):
        # 旧记录无 JSON message 时退回行内结构化列
        result = {
            "snapshot_id": snapshot_id,
            "report_status": report_status,
            "notification_status": notification_status,
            "pruned": pruned_count,
        }
    parts = []
    snap = result.get("snapshot_id")
    if snap is not None:
        parts.append(f"快照 #{snap}")
    report = result.get("report_status")
    if report == "written":
        parts.append("日报已写入")
    elif report:
        parts.append(f"日报状态 {report}")
    notification = result.get("notification_status")
    if notification == "submitted":
        parts.append("通知已提交")
    elif notification:
        parts.append(f"通知状态 {notification}")
    pruned = result.get("pruned")
    if pruned:
        parts.append(f"清理 {pruned} 条旧快照")
    warnings = result.get("warnings") or []
    if warnings:
        parts.append(f"警告 {len(warnings)} 条")
    if not parts:
        return message
    return "；".join(parts)


def latest_scan_state(conn) -> dict:
    """最近一次扫描状态 + live 进度（ISS-090）。

    DB 部分只读 scan_runs；``live`` 来自运行根状态文件（scan_progress.
    live_progress 已含 heartbeat 判活：stale/缺失/损坏 → None，前端据此
    回退到无计数的「扫描进行中…」文案）。文件通道与 DB 通道相互独立：
    任一失败都不影响另一路。
    """
    row = conn.execute(
        "SELECT r.*, d.source, d.phase, d.snapshot_id, d.report_status, "
        "d.report_path, d.notification_status, d.pruned_count "
        "FROM scan_runs r LEFT JOIN scan_run_details d ON d.run_id=r.id "
        "ORDER BY r.id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"id": None, "status": None, "running": False, "started_at": None,
                "finished_at": None, "result": None, "error": None,
                "source": None, "phase": None, "live": _live_view()}
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
        "source": row["source"], "phase": row["phase"], "live": _live_view(),
    }


def _live_view() -> dict | None:
    """live_progress 的异常安全包装：状态文件通道任何故障都不让查询 5xx。"""
    try:
        return scan_progress.live_progress()
    except Exception:
        return None
