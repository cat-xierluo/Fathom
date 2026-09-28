"""分析派发、预览、状态与生命周期（ISS-035B）。

合同来源：docs/plans/2026-09-28-agent-diff-interpretation-design.md
§4.2（预览=不可变请求）、§5（后端派发/设置/生命周期）、§6（存储/历史/隐私）。

职责边界：

- **预览**：带 TTL 的不可变请求（内存最多 8 份）。冻结 canonical 字节、
  完整 prompt、prompt_version、Runtime 身份（含探测所得版本与适配器合同
  版本）、settings/consent revision 与 facts/request 双 digest。执行只收
  ``preview_id + request_digest + idempotency_key``，绝不收浏览器任意
  prompt/命令/cwd/executable。
- **派发**：先原子占位（analysis_runs 插入 starting）再 spawn；同运行根
  全进程最多 1 个在途（独立 ``AnalysisLease`` flock，不长期持扫描
  flock）；重复 execute（同 idempotency_key + 同 digest）返回同一 job，
  不同请求忙时 409，不排队、不自动重试、不换 Runtime。
- **状态机**：``starting → running → cancelling? → succeeded|failed|
  cancelled|timed_out|interrupted``；终态经
  ``UPDATE … WHERE status NOT IN (终态集)`` 只写一次，迟到回调不复活。
  成功三关复用 035A 的 ``dispatch_request``（exit 0 + 应用无错误 + 完整
  结果验证），再过 035D 的 ``validate_analysis_result``；可信正文与
  succeeded 状态转换在同一 SQLite 事务提交，失败/取消不留半写正文。
- **取消/超时**：SIGTERM → 3 秒 → SIGKILL 回收自建进程组（复用 035A
  runner，绝不扫描杀用户独立 CLI）；墙钟默认 120 秒。
- **升级停写协调**（与 scan_coordinator 同款次序不变量）：分析方先取
  租约、后查升级 journal；升级方（fathom.upgrade）先取扫描租约、再取
  分析租约、查残留事务、写 journal——「分析在跑」与「升级事务已建立」
  不可能同时成立，journal 存在性即可在租约内可靠拒绝新派发；结果提交
  线性化点另查 revision/journal/enabled，与完成竞争只有一个确定结果。
- **生命周期维护**：启动把遗留 starting/running/cancelling 标
  interrupted（不自动重派、不按持久化 PID 发信号）；成功/失败记录最多
  保留 35 天且最多 100 条（不裁在途），只在分析生命周期维护，不改扫描
  快照保留。历史过期在读取层评估（新增快照不使旧 a→b 报告过期；a/b
  被替换/淘汰按原因标 expired），已保存证据仍可展示原日期。

日志只记 job ID、reason_code、耗时、字节数、版本与 digest；不记
prompt/正文/路径/凭据。fake 只用于故障矩阵（经生产 runner/manager），
不构成生产默认。
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import agent_runtime as ar
from . import analysis_contract as ac
from . import config, db

_LOG = logging.getLogger("fathom.analysis_manager")

# --------------------------------------------------------------------------
# 预算与策略常量（方案 §4.2/§5/§6；变更须有测量证据并回写任务）
# --------------------------------------------------------------------------

PREVIEW_TTL_S = 300.0
PREVIEW_MAX_COUNT = 8
ANALYSIS_RUN_TIMEOUT_S = ar.RUN_DEFAULT_TIMEOUT_S      # 默认 120 秒
ANALYSIS_RETENTION_DAYS = 35
ANALYSIS_RETENTION_MAX_ROWS = 100
#: 终态集合；进入即不可复活（interrupted 仅由启动 reconcile 写入）。
TERMINAL_STATUSES = frozenset({
    "succeeded", "failed", "cancelled", "timed_out", "interrupted",
})
#: 可以被取消请求转入 cancelling 的非终态。
CANCELLABLE_STATUSES = frozenset({"starting", "running"})
#: 启动 reconcile 时视为遗留的在途状态。
LEFTOVER_ACTIVE_STATUSES = ("starting", "running", "cancelling")

_LOCK_SUFFIX = ".analysis.lock"


class AnalysisError(RuntimeError):
    """分析功能错误；``reason_code`` 是稳定机读码，``message`` 不含
    stderr/路径/凭据（生产 handler 据此构造响应）。"""

    def __init__(self, reason_code: str, message: str,
                 *, status_code: int = 409) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status_code = status_code


def analysis_lock_path(db_path: Path | None = None) -> Path:
    """分析租约锁文件路径（单一来源）：与 db 同目录 ``<名>.analysis.lock``。

    供 manager 与 fathom.upgrade（prepare ①取分析租约）共用，避免两处
    拼后缀漂移。独立于扫描锁：分析不长期持扫描 flock，扫描也不因分析
    在途而拒绝。"""
    path = Path(db_path or config.DB_PATH).expanduser().resolve(strict=False)
    return path.with_name(path.name + _LOCK_SUFFIX)


def refuse_if_upgrade_txn() -> None:
    """升级停写条件（ISS-097 同款）：journal 在位即拒绝新分析（409）。

    判据是 journal 文件**存在性**（损坏同样停写，fail-closed）；内容仅作
    只读诊断。调用次序由调用方保证在取得分析租约之后（manager 内部两处：
    预览/派发的租约外粗检 + 派发租约内复检）。延迟导入 upgrade 防环。"""
    from . import upgrade
    journal_path = upgrade.journal_path_from_db(Path(config.DB_PATH))
    if not journal_path.exists():
        return
    journal = upgrade.peek_journal(journal_path)
    raise AnalysisError(
        "upgrade_in_progress",
        "升级事务进行中，分析已被暂停；请待升级完成后重试",
    )


def production_shim_argv_factory() -> Callable[[str, str, str, list[str]], list[str]] | None:
    """冻结包接缝（ISS-035B）：``sys.frozen`` 时以 CLI 隐藏子命令启动
    看门（PyInstaller 冻结解释器不支持 ``sys.executable -c``）；开发态
    返回 None（runner 用默认 ``-c`` 形态）。冻结包内实测 NOT_VERIFIED。"""
    if getattr(sys, "frozen", False):
        def factory(deadline_s: str, grace_s: str, pipe_fd: str,
                   target: list[str]) -> list[str]:
            return [sys.executable, "_agent-supervisor", deadline_s, grace_s,
                    pipe_fd, "--", *target]
        return factory
    return None


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 跨进程单在途租约
# --------------------------------------------------------------------------


class AnalysisBusy(RuntimeError):
    """另一进程（或本进程另一线程）持有分析租约。"""

    def __init__(self, owner: dict | None = None) -> None:
        super().__init__("已有分析在进行中")
        self.owner = owner or {}


class AnalysisLease:
    """由 flock 保证真实性的跨进程分析租约（语义对齐 ScanLease）。

    fd 不继承给 CLI 子进程：owner 崩溃时锁由内核立即释放，遗留任务由
    下次启动 reconcile 标 interrupted，而不是由陈旧元数据推断。
    """

    def __init__(self, path: Path, fd: int, owner: dict) -> None:
        self.path = path
        self.fd = fd
        self.owner = owner
        self._released = False

    @classmethod
    def acquire(cls, path: Path, *, source: str) -> "AnalysisLease":
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AnalysisBusy() from exc
        os.set_inheritable(fd, False)
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

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


# --------------------------------------------------------------------------
# 预览与 Runtime 选择
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeSelection:
    """一次真实探测复核过的 Runtime 身份（预览冻结它，不冻结探测过程）。"""

    id: str
    display_name: str
    executable: str
    version: str | None
    adapter_contract_version: int
    model: str | None = None  # 调用前未知，恒 None；成功后取结果字段

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "version": self.version,
            "adapter_contract_version": self.adapter_contract_version,
        }


def resolve_runtime(executable: str | None = None,
                    runtime_id: str | None = None,
                    version: str | None = None) -> RuntimeSelection:
    """对 settings 保存的 Runtime 做真实探测复核（能力门在调用侧，不在
    settings 层背书）：仅探测**保存的那个路径**（不做 PATH 枚举、不做
    登录 shell 探测），要求 READY、路径一致、版本与已验证集合一致；
    路径移除、指向改变或升级到未验证版本一律失效，绝不静默换家。"""
    if not runtime_id or not executable:
        raise AnalysisError(
            "runtime_not_configured",
            "尚未选择可用的解读引擎；请先在设置中完成检测与选择",
            status_code=403,
        )
    try:
        meta = ar.get_candidate(runtime_id)
    except ar.UnknownCandidateError:
        raise AnalysisError(
            "runtime_unknown",
            "已保存的解读引擎不再是受支持的候选，请重新检测并选择",
            status_code=403,
        ) from None
    if meta.capability_cap is not ar.Availability.READY:
        raise AnalysisError(
            "runtime_unsupported",
            f"{meta.display_name} 未通过能力门，暂不可用",
            status_code=403,
        )
    info = ar.detect_runtime(
        meta, path_env="", extra_locations=[executable],
        login_shell_cmd=["/bin/echo"],  # 复核不做登录 shell 探测：只认保存的路径
    )
    if info.availability is not ar.Availability.READY or not info.executable \
            or Path(info.executable).resolve(strict=False) \
            != Path(executable).resolve(strict=False):
        raise AnalysisError(
            "runtime_unavailable",
            "已选择的解读引擎当前不可用（已移动、损坏或版本变化）；"
            "请重新检测并选择",
            status_code=403,
        )
    if version and info.version != version:
        raise AnalysisError(
            "runtime_version_changed",
            "解读引擎已升级到未验证版本；请重新检测并确认",
            status_code=403,
        )
    return RuntimeSelection(
        id=info.id,
        display_name=info.display_name,
        executable=info.executable,
        version=info.version,
        adapter_contract_version=info.adapter_contract_version,
    )


@dataclass(frozen=True, slots=True)
class AnalysisPreview:
    """冻结的不可变请求（方案 §4.2）。只存内存，TTL 过期即丢弃。"""

    preview_id: str
    created_at: str
    created_monotonic: float
    ttl_s: float
    a_snapshot_id: int
    b_snapshot_id: int
    facts: ac.FactsPackage
    bundle: ac.RequestBundle
    runtime: RuntimeSelection
    settings_revision: int
    consent_revision: int
    redact_tokens: tuple[str, ...]
    diff_limits: tuple[int, int]  # (min_delta_kb, added_min_kb)

    @property
    def request_digest(self) -> str:
        return self.bundle.request_digest

    @property
    def facts_digest(self) -> str:
        return self.bundle.facts_digest

    def expired(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return current - self.created_monotonic >= self.ttl_s

    def public_dict(self) -> dict:
        """预览响应体（含完整发送文本，供展开查看；不含内部进程信息）。"""
        a_payload = self.facts.payload["a"]
        b_payload = self.facts.payload["b"]
        return {
            "preview_id": self.preview_id,
            "expires_in_s": max(0, int(self.ttl_s - (time.monotonic()
                                                     - self.created_monotonic))),
            "a": {"snapshot_id": self.a_snapshot_id,
                  "created_at": a_payload["created_at"]},
            "b": {"snapshot_id": self.b_snapshot_id,
                  "created_at": b_payload["created_at"]},
            "request_digest": self.request_digest,
            "facts_digest": self.facts_digest,
            "prompt_version": self.bundle.prompt_version,
            "manifest": self.bundle.manifest,
            "runtime": self.runtime.public_dict(),
            "settings_revision": self.settings_revision,
            "consent_revision": self.consent_revision,
            "prompt_text": self.bundle.prompt_text,
            "truncated": self.facts.truncated,
        }


# --------------------------------------------------------------------------
# 作业记录
# --------------------------------------------------------------------------


@dataclass(slots=True)
class JobRecord:
    """在途作业的进程内镜像；权威状态在 analysis_runs。"""

    job_id: str
    preview: AnalysisPreview
    idempotency_key: str
    cancel_event: threading.Event
    lease: AnalysisLease | None = None
    started_monotonic: float = 0.0
    cwd: Path | None = None


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------


class AnalysisManager:
    """预览/派发/状态/取消/落库/租约/升级停写协调的唯一入口。

    注入面（测试用，生产默认即下列值）：``db_path``/``lock_path`` 指向
    隔离根；``env_factory`` 提供 CLI 环境（默认最小白名单 PATH+HOME）；
    ``run_timeout_s`` 覆盖墙钟预算（默认 120 秒）；``redact_tokens_factory``
    提供路径脱敏 token（默认取运行配置 home 目录名）。
    fake 故障矩阵只经「settings 指向合成 CLI 脚本 + 预算覆盖」进入生产
    路径，不自建模拟生命周期。
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        lock_path: Path | None = None,
        env_factory: Callable[[], dict[str, str]] | None = None,
        run_timeout_s: float = ANALYSIS_RUN_TIMEOUT_S,
        redact_tokens_factory: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path else Path(config.DB_PATH)
        self._lock_path = Path(lock_path) if lock_path else analysis_lock_path(self._db_path)
        self._env_factory = env_factory or ar.build_minimal_env
        self._run_timeout_s = float(run_timeout_s)
        self._redact_tokens_factory = redact_tokens_factory or self._default_redact_tokens
        self._previews: dict[str, AnalysisPreview] = {}
        self._preview_order: list[str] = []
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()   # 预览表与 jobs 表互斥
        self._commit_lock = threading.Lock()  # 成功提交线性化点
        self._policy_epoch = 0
        self._shutdown = False

    # ------------------------------------------------------------ 基础

    @staticmethod
    def _default_redact_tokens() -> tuple[str, ...]:
        home = config.get_runtime_config().home_dir
        return (home.name,) if home.name else ()

    def _analysis_settings(self) -> config.AnalysisSettings:
        return config.effective_analysis_settings()

    def _enabled_or_403(self) -> config.AnalysisSettings:
        settings = self._analysis_settings()
        if not settings.enabled:
            raise AnalysisError(
                "analysis_disabled",
                "变化解读功能未启用；请先在设置中开启并完成授权",
                status_code=403,
            )
        return settings

    # ------------------------------------------------------------ 预览

    def create_preview(self, a_snapshot_id: int, b_snapshot_id: int) -> AnalysisPreview:
        """构造并冻结一份不可变请求（方案 §4.2）。

        复查顺序：启用 → 升级停写 → Runtime 真实探测复核 → 快照存在 →
        同数据集 → 同事务构造事实包。预览是纯读操作，不派发不外传。"""
        settings = self._enabled_or_403()
        refuse_if_upgrade_txn()
        runtime = resolve_runtime(
            settings.runtime_executable, settings.runtime_id,
            settings.runtime_version,
        )
        conn = db.connect(self._db_path)
        try:
            facts = self._build_facts(conn, a_snapshot_id, b_snapshot_id)
        finally:
            conn.close()
        bundle = ac.build_request_bundle(
            facts,
            runtime_display_name=runtime.display_name,
            runtime_version=runtime.version,
        )
        preview = AnalysisPreview(
            preview_id=uuid.uuid4().hex,
            created_at=_now(),
            created_monotonic=time.monotonic(),
            ttl_s=PREVIEW_TTL_S,
            a_snapshot_id=a_snapshot_id,
            b_snapshot_id=b_snapshot_id,
            facts=facts,
            bundle=bundle,
            runtime=runtime,
            settings_revision=settings.settings_revision,
            consent_revision=settings.consent_revision,
            redact_tokens=self._redact_tokens_factory(),
            diff_limits=(1024, 100 * 1024),
        )
        with self._lock:
            self._previews[preview.preview_id] = preview
            self._preview_order.append(preview.preview_id)
            while len(self._preview_order) > PREVIEW_MAX_COUNT:
                oldest = self._preview_order.pop(0)
                self._previews.pop(oldest, None)
        return preview

    def _build_facts(self, conn: sqlite3.Connection, a: int, b: int) -> ac.FactsPackage:
        try:
            return ac.build_facts_package(
                conn, a, b, redact_tokens=self._redact_tokens_factory(),
            )
        except ac.AnalysisContractError as exc:
            if exc.reason_code == "snapshot_not_found":
                raise AnalysisError(
                    "snapshot_not_found", str(exc), status_code=404) from exc
            raise AnalysisError(
                exc.reason_code, str(exc), status_code=400) from exc

    def _get_preview(self, preview_id: str) -> AnalysisPreview:
        with self._lock:
            preview = self._previews.get(preview_id)
        if preview is None:
            raise AnalysisError(
                "preview_not_found",
                "预览不存在或已过期（服务重启后必须重新预览）",
                status_code=404,
            )
        if preview.expired():
            self._discard_preview(preview_id)
            raise AnalysisError(
                "preview_expired",
                "预览已过期（TTL 5 分钟）；请重新生成预览",
            )
        return preview

    def _discard_preview(self, preview_id: str) -> None:
        with self._lock:
            self._previews.pop(preview_id, None)
            if preview_id in self._preview_order:
                self._preview_order.remove(preview_id)

    def _revalidate_preview(self, preview: AnalysisPreview) -> None:
        """派发前的完整复查：revision/Runtime/数据指纹/升级/启用（方案
        §4.2「执行前复查授权、Runtime、数据指纹及升级状态」）。任何变化
        都 409 要求重新预览，禁止重建后自动发送。"""
        settings = self._enabled_or_403()
        if (settings.settings_revision != preview.settings_revision
                or settings.consent_revision != preview.consent_revision):
            raise AnalysisError(
                "preview_stale",
                "分析设置或授权已变化，请重新生成预览",
            )
        runtime = resolve_runtime(
            settings.runtime_executable, settings.runtime_id,
            settings.runtime_version,
        )
        if (runtime.id != preview.runtime.id
                or runtime.executable != preview.runtime.executable
                or runtime.version != preview.runtime.version):
            raise AnalysisError(
                "preview_stale",
                "解读引擎已变化，请重新生成预览",
            )
        conn = db.connect(self._db_path)
        try:
            try:
                facts = self._build_facts(
                    conn, preview.a_snapshot_id, preview.b_snapshot_id)
            except AnalysisError as exc:
                if exc.reason_code == "snapshot_not_found":
                    # 所选 a/b 已被替换/淘汰：对预览而言是「范围失效」，
                    # 不是快照查询错误——要求重新预览。
                    raise AnalysisError(
                        "preview_stale",
                        "所选区间快照已被替换或淘汰，请重新生成预览",
                    ) from exc
                raise
        finally:
            conn.close()
        if facts.facts_digest != preview.facts_digest:
            raise AnalysisError(
                "preview_stale",
                "所选区间数据已变化，请重新生成预览",
            )

    # ------------------------------------------------------------ 派发

    def start_job(self, preview_id: str, request_digest: str,
                  idempotency_key: str) -> tuple[dict, bool]:
        """执行预览：只收 preview_id + request_digest + idempotency_key。

        返回 ``(job 视图, 是否幂等重放)``。重复 execute（同 key 同 digest）
        返回同一 job（含完成后重放与并发双击的短暂重查）；不同请求忙时
        409；先原子占位再 spawn。
        """
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
                or len(idempotency_key) > 200:
            raise AnalysisError(
                "bad_idempotency_key",
                "idempotency_key 必须是 1..200 字符的字符串",
                status_code=400,
            )
        idempotency_key = idempotency_key.strip()

        # 先答「功能是否开放」（403），再答「资源是否存在」（404）。
        self._enabled_or_403()

        existing = self._find_job_by_idempotency(idempotency_key)
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise AnalysisError(
                    "idempotency_conflict",
                    "该幂等键已被不同请求使用",
                )
            return existing, True

        preview = self._get_preview(preview_id)
        if request_digest != preview.request_digest:
            raise AnalysisError(
                "request_digest_mismatch",
                "request_digest 与预览不符；请以最新预览的 digest 重新提交",
            )

        # 租约外粗检（快速失败）；租约内复检才是权威（防 TOCTOU）。
        refuse_if_upgrade_txn()
        try:
            lease = AnalysisLease.acquire(self._lock_path, source="analysis")
        except AnalysisBusy:
            # 并发双击：胜者取得租约后毫秒级插入 starting 行；败者短暂
            # 重查幂等表即可返回同一 job。有界 1 秒，超时按忙 409（不排队）。
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                time.sleep(0.05)
                existing = self._find_job_by_idempotency(idempotency_key)
                if existing is not None:
                    if existing["request_digest"] != request_digest:
                        raise AnalysisError(
                            "idempotency_conflict", "该幂等键已被不同请求使用")
                    return existing, True
            raise AnalysisError(
                "analysis_busy",
                "已有分析在进行中；请等待其结束，不会排队或自动重试",
            ) from None
        try:
            return self._start_job_under_lease(lease, preview, idempotency_key,
                                               request_digest)
        except BaseException:
            lease.release()
            raise

    def _start_job_under_lease(self, lease: AnalysisLease,
                               preview: AnalysisPreview,
                               idempotency_key: str,
                               request_digest: str) -> tuple[dict, bool]:
        # 同 key 竞争双击：胜者插入后，败者在租约内重查 → 返回同一 job。
        existing = self._find_job_by_idempotency(idempotency_key)
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise AnalysisError(
                    "idempotency_conflict", "该幂等键已被不同请求使用")
            return existing, True

        refuse_if_upgrade_txn()
        self._revalidate_preview(preview)

        owner_id = lease.owner["owner_id"]
        job_id = uuid.uuid4().hex
        rec = JobRecord(
            job_id=job_id, preview=preview, idempotency_key=idempotency_key,
            cancel_event=threading.Event(), lease=lease,
            started_monotonic=time.monotonic(),
        )
        # 先原子占位再 spawn：starting 行（含幂等唯一键）是跨进程事实。
        conn = db.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO analysis_runs("
                " job_id, a_snapshot_id, b_snapshot_id, request_digest,"
                " facts_digest, prompt_version, idempotency_key, runtime_id,"
                " runtime_executable, runtime_version, settings_revision,"
                " consent_revision, status, owner_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'starting', ?, ?)",
                (job_id, preview.a_snapshot_id, preview.b_snapshot_id,
                 preview.request_digest, preview.facts_digest,
                 preview.bundle.prompt_version, idempotency_key,
                 preview.runtime.id, preview.runtime.executable,
                 preview.runtime.version, preview.settings_revision,
                 preview.consent_revision, owner_id, _now()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            existing = self._find_job_by_idempotency(idempotency_key)
            if existing is not None and existing["request_digest"] == request_digest:
                return existing, True
            raise AnalysisError(
                "idempotency_conflict", "该幂等键已被并发请求占用") from None
        finally:
            conn.close()

        rec.cwd = Path(tempfile.mkdtemp(prefix="fathom-analysis-"))
        with self._lock:
            self._jobs[job_id] = rec
        self._discard_preview(preview.preview_id)  # 单次消费
        thread = threading.Thread(
            target=self._execute_job, args=(rec,),
            name=f"fathom-analysis-{job_id[:8]}", daemon=False,
        )
        thread.start()
        return self.job_view(job_id), False

    def _find_job_by_idempotency(self, key: str) -> dict | None:
        conn = db.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT job_id, request_digest, status FROM analysis_runs "
                "WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None:
                return None
            view = self.job_view(row["job_id"])
            view["request_digest"] = row["request_digest"]
            return view
        finally:
            conn.close()

    # ------------------------------------------------------------ 执行

    def _execute_job(self, rec: JobRecord) -> None:
        """工作线程：running → dispatch → 终态（三关 + 035D 验证 + 线性化提交）。

        本方法拥有 rec.lease 与 rec.cwd 的清理责任；任何路径都保证释放。
        """
        preview = rec.preview
        duration_ms = 0
        try:
            self._mark_running(rec)
            if rec.cancel_event.is_set():
                # 派发后、执行前已撤销（refresh_policy/取消竞争）。
                self._finish(rec, "cancelled", "cancelled_before_start",
                             duration_ms)
                return

            info = ar.RuntimeInfo(
                id=preview.runtime.id, display_name=preview.runtime.display_name,
                identity=f"{preview.runtime.display_name}（预览冻结身份）",
                identity_evidence="预览时探测复核", official_docs="",
                availability=ar.Availability.READY,
                reason_code="verified_version", detail="",
                executable=preview.runtime.executable, version=preview.runtime.version,
                adapter_contract_version=preview.runtime.adapter_contract_version,
            )
            if info.adapter_contract_version != ar.ADAPTER_CONTRACT_VERSION:
                self._finish(rec, "failed", "adapter_contract_changed", duration_ms)
                return
            adapter = ar.get_adapter(info)
            runner = ar.AgentCliRunner(
                liveness_watch=True,
                shim_argv_factory=production_shim_argv_factory(),
            )
            dispatch = ar.dispatch_request(
                adapter, runner, preview.bundle.prompt_text,
                cwd=rec.cwd, env=self._env_factory(),
                timeout_s=self._run_timeout_s,
                cancel_event=rec.cancel_event,
            )
            duration_ms = int((time.monotonic() - rec.started_monotonic) * 1000)
            self._record_run_stats(rec, dispatch)
            self._settle(rec, dispatch, duration_ms)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要落到确定性终态
            _LOG.exception("分析执行线程异常 job_id=%s", rec.job_id)
            duration_ms = int((time.monotonic() - rec.started_monotonic) * 1000)
            self._finish(rec, "failed", "internal_error", duration_ms)
        finally:
            if rec.cwd is not None:
                shutil.rmtree(rec.cwd, ignore_errors=True)
            with self._lock:
                self._jobs.pop(rec.job_id, None)
            if rec.lease is not None:
                rec.lease.release()
                rec.lease = None

    def _mark_running(self, rec: JobRecord) -> None:
        conn = db.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE analysis_runs SET status='running', started_at=? "
                "WHERE job_id=? AND status='starting'",
                (_now(), rec.job_id),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _record_run_stats(rec: JobRecord, dispatch: "ar.DispatchResult") -> None:
        """日志只记 job ID、结局、耗时、字节量、版本与 digest（方案 §6）。"""
        _LOG.info(
            "analysis dispatch job_id=%s outcome=%s exit=%s wall_ms=%s "
            "stdout_bytes=%d stderr_bytes=%d runtime=%s adapter_contract=%s "
            "request_digest=%s",
            rec.job_id, dispatch.run.outcome.value, dispatch.run.exit_code,
            dispatch.run.wall_ms, len(dispatch.run.stdout_text.encode("utf-8")),
            len(dispatch.run.stderr_text.encode("utf-8")),
            rec.preview.runtime.id, rec.preview.runtime.adapter_contract_version,
            rec.preview.request_digest,
        )

    def _settle(self, rec: JobRecord, dispatch: "ar.DispatchResult",
                duration_ms: int) -> None:
        """把 dispatch 结论落成确定性终态；成功需再过 035D 验证与提交门。"""
        preview = rec.preview
        if dispatch.ok:
            value = dispatch.parse.value or {}
            validated = ac.validate_analysis_result(value.get("result", ""),
                                                    preview.facts)
            if not validated.ok:
                self._finish(rec, "failed",
                             f"result_{validated.reason_code}", duration_ms)
                return
            self._commit_success(rec, validated.value, duration_ms)
            return
        if dispatch.run.outcome is ar.RunOutcome.CANCELLED:
            self._finish(rec, "cancelled", "cancelled", duration_ms)
            return
        if dispatch.run.outcome is ar.RunOutcome.TIMED_OUT:
            self._finish(rec, "timed_out", "runner_timed_out", duration_ms)
            return
        self._finish(rec, "failed", dispatch.reason_code, duration_ms)

    def _commit_success(self, rec: JobRecord, result: dict, duration_ms: int) -> None:
        """成功提交（线性化点）：提交门通过才允许可信正文 + succeeded
        同事务落库；门未过（授权撤销/切 Runtime/升级停写）或已被取消/
        完成，则只有竞争的另一方生效，绝不复活、绝不双写。"""
        with self._commit_lock:
            gate = self._commit_gate(rec)
            conn = db.connect(self._db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status FROM analysis_runs WHERE job_id=?",
                    (rec.job_id,)).fetchone()
                if row is None or row["status"] in TERMINAL_STATUSES:
                    # 取消/终态已先行：本次成功迟到达，丢弃（终态只写一次）。
                    conn.rollback()
                    _LOG.info("analysis success dropped job_id=%s status=%s",
                              rec.job_id, row["status"] if row else None)
                    return
                if gate is not None:
                    # 提交资格已撤销：记 cancelled（带原因），不落正文。
                    conn.execute(
                        "UPDATE analysis_runs SET status='cancelled',"
                        " reason_code=?, finished_at=?, duration_ms=? "
                        "WHERE job_id=?",
                        (gate, _now(), duration_ms, rec.job_id))
                    conn.commit()
                    _LOG.info("analysis success revoked job_id=%s gate=%s",
                              rec.job_id, gate)
                    return
                analysis_id = self._insert_analysis(conn, rec, result)
                conn.execute(
                    "UPDATE analysis_runs SET status='succeeded',"
                    " reason_code=NULL, finished_at=?, duration_ms=? "
                    "WHERE job_id=?",
                    (_now(), duration_ms, rec.job_id))
                conn.commit()
                _LOG.info("analysis succeeded job_id=%s analysis_id=%s",
                          rec.job_id, analysis_id)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            try:
                self.prune_history()
            except Exception:  # noqa: BLE001 - 保留清理失败不影响终态
                _LOG.exception("analysis 保留清理失败 job_id=%s", rec.job_id)

    def _commit_gate(self, rec: JobRecord) -> str | None:
        """提交资格门：返回 None 表示可提交，否则返回撤销原因码。"""
        try:
            refuse_if_upgrade_txn()
        except AnalysisError:
            return "upgrade_in_progress"
        settings = self._analysis_settings()
        preview = rec.preview
        if not settings.enabled:
            return "analysis_disabled"
        if (settings.settings_revision != preview.settings_revision
                or settings.consent_revision != preview.consent_revision):
            return "policy_changed"
        if (settings.runtime_id != preview.runtime.id
                or settings.runtime_executable != preview.runtime.executable):
            return "runtime_changed"
        return None

    def _insert_analysis(self, conn: sqlite3.Connection, rec: JobRecord,
                         result: dict) -> int:
        preview = rec.preview
        payload = preview.facts.payload
        # dataset_root 存**真实根**（本地审计字段，不进模型输入——事实包
        # 内是脱敏别名）：历史过期评估要靠它与 snapshots 行对口径。
        root_row = conn.execute("SELECT root FROM snapshots WHERE id=?",
                                (preview.a_snapshot_id,)).fetchone()
        real_root = root_row["root"] if root_row is not None else ""
        cur = conn.execute(
            "INSERT INTO agent_analyses("
            " job_id, a_snapshot_id, b_snapshot_id, a_created_at, b_created_at,"
            " dataset_root, dataset_min_kb, dataset_exclude_names,"
            " request_digest, facts_digest, prompt_version,"
            " adapter_contract_version, runtime_id, runtime_version, model,"
            " result_json, facts_json, manifest_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.job_id, preview.a_snapshot_id, preview.b_snapshot_id,
             payload["a"]["created_at"], payload["b"]["created_at"],
             real_root, payload["dataset"]["min_kb"],
             ";".join(payload["dataset"]["exclude_names"]),
             preview.request_digest, preview.facts_digest,
             preview.bundle.prompt_version,
             preview.runtime.adapter_contract_version, preview.runtime.id,
             preview.runtime.version,
             result.get("model") if isinstance(result.get("model"), str) else None,
             json.dumps(result, ensure_ascii=False),
             preview.facts.canonical_json.decode("utf-8"),
             json.dumps(preview.bundle.manifest, ensure_ascii=False),
             _now()),
        )
        return int(cur.lastrowid)

    def _finish(self, rec: JobRecord, status: str, reason_code: str | None,
                duration_ms: int) -> None:
        """终态只写一次：``WHERE status NOT IN (终态集)`` 竞争失败即放弃。"""
        assert status in TERMINAL_STATUSES
        conn = db.connect(self._db_path)
        try:
            cur = conn.execute(
                "UPDATE analysis_runs SET status=?, reason_code=?,"
                " finished_at=?, duration_ms=? WHERE job_id=? AND"
                " status NOT IN "
                "('succeeded','failed','cancelled','timed_out','interrupted')",
                (status, reason_code, _now(), duration_ms, rec.job_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                _LOG.info("analysis terminal already set job_id=%s (wanted %s)",
                          rec.job_id, status)
            else:
                _LOG.info("analysis terminal job_id=%s status=%s reason=%s",
                          rec.job_id, status, reason_code)
        finally:
            conn.close()
        try:
            self.prune_history()
        except Exception:  # noqa: BLE001
            _LOG.exception("analysis 保留清理失败 job_id=%s", rec.job_id)

    # ------------------------------------------------------------ 取消

    def cancel_job(self, job_id: str) -> dict:
        """请求取消：置 cancel_event 并转 cancelling；终态不复活。

        与完成竞争只有一个确定结果：先到终态者胜，本方法看到终态即返回
        当前视图（``terminal=True``，409 语义由 API 层映射），绝不改写
        终态。本进程没有该在途任务时（如另一进程持有租约）不按 PID 发
        信号，仅在状态仍可取消时标记 cancelling，由持有方收敛终态。"""
        with self._lock:
            rec = self._jobs.get(job_id)
        if rec is None:
            row = self._job_row(job_id)
            if row is None:
                raise AnalysisError("job_not_found", "未知分析任务",
                                    status_code=404)
            if row["status"] in TERMINAL_STATUSES:
                return self.job_view(job_id)
        self._mark_cancelling(job_id)
        if rec is not None:
            rec.cancel_event.set()
        return self.job_view(job_id)

    def _mark_cancelling(self, job_id: str) -> None:
        conn = db.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE analysis_runs SET status='cancelling' WHERE "
                "job_id=? AND status IN ('starting','running')",
                (job_id,))
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ 查询

    def _job_row(self, job_id: str) -> sqlite3.Row | None:
        conn = db.connect(self._db_path)
        try:
            return conn.execute(
                "SELECT * FROM analysis_runs WHERE job_id=?", (job_id,)
            ).fetchone()
        finally:
            conn.close()

    def job_view(self, job_id: str) -> dict:
        """job 状态视图（GET jobs/{id} 数据源；纯读，无副作用）。"""
        row = self._job_row(job_id)
        if row is None:
            raise AnalysisError("job_not_found", "未知分析任务", status_code=404)
        analysis = None
        revoked = row["revoked_at"] is not None
        conn = db.connect(self._db_path)
        try:
            arow = conn.execute(
                "SELECT id FROM agent_analyses WHERE job_id=?", (job_id,)
            ).fetchone()
            analysis = arow["id"] if arow else None
        finally:
            conn.close()
        return {
            "job_id": row["job_id"],
            "status": row["status"],
            "reason_code": row["reason_code"],
            "a_snapshot_id": row["a_snapshot_id"],
            "b_snapshot_id": row["b_snapshot_id"],
            "request_digest": row["request_digest"],
            "facts_digest": row["facts_digest"],
            "prompt_version": row["prompt_version"],
            "idempotency_key": row["idempotency_key"],
            "runtime": {"id": row["runtime_id"], "version": row["runtime_version"]},
            "settings_revision": row["settings_revision"],
            "consent_revision": row["consent_revision"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "duration_ms": row["duration_ms"],
            "analysis_id": analysis,
            "revoked": revoked,
            "terminal": row["status"] in TERMINAL_STATUSES,
        }

    # ------------------------------------------------------------ 历史

    def list_analyses(self, a_snapshot_id: int, b_snapshot_id: int) -> list[dict]:
        """某 a→b 区间的历史解读（读取层评估过期；新增快照不使旧报告过期）。"""
        conn = db.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT x.*, r.revoked_at FROM agent_analyses x "
                "LEFT JOIN analysis_runs r ON r.job_id = x.job_id "
                "WHERE x.a_snapshot_id=? AND x.b_snapshot_id=? "
                "ORDER BY x.created_at DESC, x.id DESC",
                (a_snapshot_id, b_snapshot_id),
            ).fetchall()
            return [self._analysis_view(conn, dict(r)) for r in rows]
        finally:
            conn.close()

    def get_analysis(self, analysis_id: int) -> dict:
        conn = db.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT x.*, r.revoked_at FROM agent_analyses x "
                "LEFT JOIN analysis_runs r ON r.job_id = x.job_id "
                "WHERE x.id=?", (analysis_id,)).fetchone()
            if row is None:
                raise AnalysisError("analysis_not_found", "未知解读记录",
                                    status_code=404)
            return self._analysis_view(conn, dict(row))
        finally:
            conn.close()

    def revoke_analysis(self, analysis_id: int) -> dict:
        """撤销（DELETE）：删除本应用的当前正文与关联事实包；不声称消除
        SQLite 旧页/WAL、旧备份或第三方 CLI/模型服务留存。生命周期行
        保留（revoked_at 审计），job 的 analysis_id 变为空。"""
        conn = db.connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT job_id FROM agent_analyses WHERE id=?",
                (analysis_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise AnalysisError("analysis_not_found", "未知解读记录",
                                    status_code=404)
            conn.execute("DELETE FROM agent_analyses WHERE id=?", (analysis_id,))
            conn.execute(
                "UPDATE analysis_runs SET revoked_at=? WHERE job_id=? "
                "AND revoked_at IS NULL", (_now(), row["job_id"]))
            conn.commit()
            _LOG.info("analysis revoked analysis_id=%s job_id=%s",
                      analysis_id, row["job_id"])
            return {"ok": True, "job_id": row["job_id"]}
        except AnalysisError:
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _analysis_view(self, conn: sqlite3.Connection, row: dict) -> dict:
        expired, expired_reason = self._evaluate_expiry(conn, row)
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "a": {"snapshot_id": row["a_snapshot_id"],
                  "created_at": row["a_created_at"]},
            "b": {"snapshot_id": row["b_snapshot_id"],
                  "created_at": row["b_created_at"]},
            "dataset": {
                "root": row["dataset_root"],
                "min_kb": row["dataset_min_kb"],
                "exclude_names": [
                    s for s in str(row["dataset_exclude_names"] or "").split(";") if s
                ],
            },
            "request_digest": row["request_digest"],
            "facts_digest": row["facts_digest"],
            "prompt_version": row["prompt_version"],
            "runtime": {"id": row["runtime_id"], "version": row["runtime_version"],
                        "model": row["model"]},
            "created_at": row["created_at"],
            "result": json.loads(row["result_json"]),
            "facts": json.loads(row["facts_json"]),
            "manifest": json.loads(row["manifest_json"]),
            "expired": expired,
            "expired_reason": expired_reason,
            "revoked": row.get("revoked_at") is not None,
        }

    @staticmethod
    def _dataset_matches(snap: sqlite3.Row, row: dict) -> bool:
        saved_min = row["dataset_min_kb"]
        snap_min = snap["min_kb"] if "min_kb" in snap.keys() else None
        saved_excludes = str(row["dataset_exclude_names"] or "")
        snap_excludes = snap["exclude_names"] if "exclude_names" in snap.keys() else ""
        return (snap["root"] == row["dataset_root"]
                and snap_min == saved_min
                and str(snap_excludes or "") == saved_excludes)

    def _evaluate_expiry(self, conn: sqlite3.Connection,
                         row: dict) -> tuple[bool, str | None]:
        """历史语义（方案 §6）：新增快照不使不变的历史 a→b 报告过期；
        a/b 被同日替换/被淘汰、或原口径不能再验证时，读取层标 expired
        并返回原因；已保存证据仍展示原日期与依据。"""
        for sid_column, created_column in (("a_snapshot_id", "a_created_at"),
                                           ("b_snapshot_id", "b_created_at")):
            sid = row[sid_column]
            snap = conn.execute("SELECT * FROM snapshots WHERE id=?",
                                (sid,)).fetchone()
            if snap is None:
                # 同日**后继**（created_at 晚于被删快照、同数据集）存在
                # → 该位置被同日替换（create_snapshot 同日落盘行为）；
                # 无后继 → 按保留策略淘汰。基线早于被删快照，不算替换。
                same_day = conn.execute(
                    "SELECT COUNT(*) c FROM snapshots WHERE root IS ? "
                    "AND min_kb IS ? AND exclude_names IS ? "
                    "AND substr(created_at,1,10) = substr(?,1,10) "
                    "AND (created_at > ? OR (created_at = ? AND id > ?))",
                    (row["dataset_root"], row["dataset_min_kb"],
                     str(row["dataset_exclude_names"] or ""),
                     str(row[created_column]),
                     str(row[created_column]), str(row[created_column]),
                     sid),
                ).fetchone()["c"]
                reason = "snapshot_replaced" if same_day else "snapshot_pruned"
                return True, reason
            if not self._dataset_matches(snap, row):
                return True, "dataset_unverifiable"
        return False, None

    # ------------------------------------------------------------ 生命周期维护

    def prune_history(self, *, now: dt.datetime | None = None) -> int:
        """分析保留策略（方案 §6）：成功/失败记录最多 35 天且最多 100 条；
        不裁在途任务；只在分析生命周期维护，不改扫描快照保留。

        删除次序：先删关联 agent_analyses（证据），再删 analysis_runs 行
        （FK 故意不级联，见 db.py）。返回删除的生命周期行数。"""
        current = now or dt.datetime.now()
        cutoff = (current - dt.timedelta(days=ANALYSIS_RETENTION_DAYS)).isoformat(
            timespec="seconds")
        conn = db.connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            stale = conn.execute(
                "SELECT job_id FROM analysis_runs WHERE created_at < ? AND "
                "status NOT IN ('starting','running','cancelling')",
                (cutoff,)).fetchall()
            for r in stale:
                conn.execute("DELETE FROM agent_analyses WHERE job_id=?",
                             (r["job_id"],))
                conn.execute("DELETE FROM analysis_runs WHERE job_id=?",
                             (r["job_id"],))
            keep = conn.execute(
                "SELECT job_id FROM analysis_runs WHERE status NOT IN "
                "('starting','running','cancelling') "
                "ORDER BY created_at DESC, job_id DESC "
                "LIMIT ?",
                (ANALYSIS_RETENTION_MAX_ROWS,),
            ).fetchall()
            kept_ids = [r["job_id"] for r in keep]
            overflow: list[str] = []
            if len(kept_ids) == ANALYSIS_RETENTION_MAX_ROWS:
                rows = conn.execute(
                    "SELECT job_id FROM analysis_runs WHERE status NOT IN "
                    "('starting','running','cancelling') "
                    "ORDER BY created_at DESC, job_id DESC "
                    "LIMIT -1 OFFSET ?",
                    (ANALYSIS_RETENTION_MAX_ROWS,),
                ).fetchall()
                overflow = [r["job_id"] for r in rows]
            for job_id in overflow:
                conn.execute("DELETE FROM agent_analyses WHERE job_id=?", (job_id,))
                conn.execute("DELETE FROM analysis_runs WHERE job_id=?", (job_id,))
            conn.commit()
            removed = len(stale) + len(overflow)
            if removed:
                _LOG.info("analysis retention pruned runs=%d", removed)
            return removed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reconcile_startup(self) -> int:
        """启动 reconcile：遗留 starting/running/cancelling 标 interrupted
        （不自动重派、不按持久化 PID 发信号）。本进程自己的在途任务不受
        影响（服务重启后进程内表为空，因此实际是全部遗留行）。"""
        conn = db.connect(self._db_path)
        try:
            cur = conn.execute(
                "UPDATE analysis_runs SET status='interrupted',"
                " reason_code='owner_exit', finished_at=? "
                "WHERE status IN ('starting','running','cancelling')",
                (_now(),),
            )
            conn.commit()
            if cur.rowcount:
                _LOG.info("analysis reconcile marked interrupted n=%d",
                          cur.rowcount)
            return int(cur.rowcount)
        finally:
            conn.close()

    def refresh_policy(self) -> None:
        """授权关闭/切 Runtime：原子撤销提交资格（revision 门 + 立即
        取消在途）、失效全部预览。与完成竞争只有一个确定结果：先到终态
        者胜（见 _commit_success/_finish）。"""
        with self._commit_lock:
            self._policy_epoch += 1
        with self._lock:
            self._previews.clear()
            self._preview_order.clear()
            recs = list(self._jobs.values())
        for rec in recs:
            conn = db.connect(self._db_path)
            try:
                conn.execute(
                    "UPDATE analysis_runs SET status='cancelling' WHERE "
                    "job_id=? AND status IN ('starting','running')",
                    (rec.job_id,))
                conn.commit()
            finally:
                conn.close()
            rec.cancel_event.set()
        if recs:
            _LOG.info("analysis policy refresh cancelled n=%d", len(recs))

    def active_job_id(self) -> str | None:
        with self._lock:
            recs = list(self._jobs)
        return recs[0] if recs else None

    def shutdown(self, *, wait_s: float = 8.0) -> None:
        """服务正常退出：回收自有分析（cancel + 有界等待终态）。

        异常退出的有界回收不依赖本方法——runner 的父进程存活看门在
        helper 进程死亡后使子任务有界退出（合同测试见
        tests/test_agent_runtime.py / test_agent_supervisor_seam.py）。"""
        self._shutdown = True
        with self._lock:
            recs = list(self._jobs.values())
        for rec in recs:
            rec.cancel_event.set()
        deadline = time.monotonic() + wait_s
        for rec in recs:
            while time.monotonic() < deadline:
                with self._lock:
                    if rec.job_id not in self._jobs:
                        break
                time.sleep(0.05)
