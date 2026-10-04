"""近期大文件查询：find 近 N 天修改过的大文件。

合同（ISS-032）：
- 显式触发：调用方主动 ``submit`` 才会启动 find，不再每次请求实时遍历。
- 单参数去重：相同 (root, days, min_mb, topn) 的并发请求只启动一次 find，
  等待者共享同一 ``BigfilesFuture``。
- 取消/超时：调用 ``future.cancel()`` 或超过 ``timeout`` 时，向 find 进程组
  发送 SIGTERM，超时回收窗口内仍存活则升级为 SIGKILL；所有等待者同步收到
  ``CancelledError`` / 超时状态。取消落在 Popen 返回到 ``task.proc`` 赋值
  之间的 spawn 窗口时（ISS-123），进程创建后立即补投组信号——``cancel()``
  返回 ``True`` 即代表取消必然生效。
- TTL 缓存：成功结果缓存 ``cache_ttl_s``；TTL 内同参数请求直接命中缓存。
- 过期即刷新：TTL 之外的首个同参数调用丢弃过期条目并启动新 find（仍遵守
  并发去重），返回新结果；``submit`` 不再返回携带旧数据的 ``state=expired``
  终态（枚举保留以兼容 API 状态字段合同，``find_big_files`` 对该状态抛错
  而非静默返回旧列表）。
- 结果上限截断可辨：find 实际输出命中 ``result_cap`` 或请求 ``topn`` 时，结果
  携带 ``truncated=True`` 与未截断的原始计数。
- 五态区分（不含 OK 共六态）：OK / NO_MATCH / PERMISSION_DENIED / FAILED /
  TRUNCATED / EXPIRED。find 的 stderr 含 Permission denied / Operation not
  permitted 视为权限受限；其他非零退出或 stderr 报错视为 FAILED。
- 原始路径只进本地必要日志（``fathom.bigfiles`` logger），日志形如
  ``<sha8>.../<basename>``，完整路径永不进入日志或返回值之外。
- 任务句柄与取消入口（ISS-164）：每次查询（含缓存命中）都在管理器内登记一条
  ``BigfilesTask`` 记录，句柄 ``task_id`` 由去重键确定性派生，因此「同参并发
  去重」与「按句柄寻址/取消」指向同一条任务。``status_view`` 返回与既有五态
  对齐的真实状态（在途 ``running``、取消 ``cancelled`` + 既有六态终态），
  ``cancel_task`` 只取消该句柄对应任务的进程组；终态句柄在 ``task_retention_s``
  内保留以支撑幂等取消，超出保留期按未知句柄处理。浏览器中止 HTTP 请求不写
  任何服务端状态，取消必须走此入口（API 层 ``POST /api/bigfiles/cancel``）。

实现层面：
- ``BigfilesManager`` 维护进程级单例：参数 -> in-flight Future 与缓存；
  测试可通过 ``popen_factory`` 注入可控 Popen。
- find 通过 ``start_new_session=True`` 单独进程组启动，便于回收。
- ``resource.getrusage(RUSAGE_CHILDREN)`` 给出子进程峰值 RSS（macOS/Linux
  均为字节）；与 ``time.monotonic`` 共同记录墙钟。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import enum
import hashlib
import logging
import os
import resource
import selectors
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from . import config

_LOG = logging.getLogger("fathom.bigfiles")


class BigfilesState(str, enum.Enum):
    """大文件查询的可辨状态。"""

    OK = "ok"
    NO_MATCH = "no_match"
    PERMISSION_DENIED = "permission_denied"
    FAILED = "failed"
    TRUNCATED = "truncated"
    EXPIRED = "expired"


class BigfilesTaskState(str, enum.Enum):
    """任务生命周期状态（ISS-164），与 ``BigfilesState`` 终态对齐。

    只新增查询在途与被取消两态；``ok``/``no_match``/``permission_denied``/
    ``failed``/``truncated``/``expired`` 六态仍由 ``BigfilesState`` 给出，
    两个枚举不在查询结果里混用：``BigfilesResult.state`` 永不为本枚举值。
    """

    RUNNING = "running"
    CANCELLED = "cancelled"


_BIGFILE_MODES = ("recent", "largest")

# 句柄前缀：只用于让 task_id 在日志/前端一眼可辨（不含任何路径信息）
_TASK_ID_PREFIX = "bf-"


@dataclass(frozen=True, slots=True)
class BigfilesQuery:
    """一次大文件查询的参数。

    ``mode``（ISS-150）：``recent`` 保持既有行为（近 N 天修改，mtime 过滤）；
    ``largest`` 按当前 st_size 逻辑大小排序、不带 mtime 过滤。
    ``scope_version`` 是允许范围配置版本的接缝（155 引入 allowlisted scope
    身份时递增即可使旧缓存/去重失效）；缓存与并发去重键由
    (规范根, mode, days, min_mb, topn, scope_version) 组成。
    """

    root: Path
    days: int
    min_mb: int
    topn: int
    mode: str = "recent"
    scope_version: int = 0

    @property
    def key(self) -> tuple:
        return (str(self.root), str(self.mode), int(self.days), int(self.min_mb),
                int(self.topn), int(self.scope_version))


@dataclass(slots=True)
class BigfilesStats:
    """单次 find 资源/输出统计。"""

    wall_ms: int = 0
    peak_rss_bytes: int = 0          # 子进程峰值 RSS（字节）
    find_output_lines: int = 0
    find_exit_code: int = 0
    find_stderr_lines: int = 0
    permission_denied_lines: int = 0
    started_at: float = 0.0          # 查询开始（epoch 秒）
    finished_at: float = 0.0         # 查询结束（epoch 秒）


@dataclass(slots=True)
class BigfilesResult:
    state: BigfilesState
    files: list[dict] = field(default_factory=list)
    stats: BigfilesStats = field(default_factory=BigfilesStats)
    error_message: Optional[str] = None
    cached: bool = False
    cache_age_s: Optional[float] = None
    truncated: bool = False
    raw_truncated: bool = False
    # ISS-150：find 未完成全量遍历（时间/输出预算提前截断）时为 True；
    # 此时结果只代表「已检查文件中的较大项」，不代表目录的当前最大文件。
    incomplete: bool = False


# find stderr 中“权限拒绝”行的关键消息段（与 ISS-018 同源语义：仅 stderr 每行
# 最后一段精确等于下列之一视为权限受限；路径文字中的同名片段不冒充证据）
_PERMISSION_TOKENS = frozenset({"Permission denied", "Operation not permitted"})


def _stderr_is_permission_only(stderr: str) -> bool:
    """stderr 中非空行是否全部可解释为权限受限。"""
    non_empty = [ln for ln in stderr.splitlines() if ln.strip()]
    if not non_empty:
        return False
    perm = 0
    other = 0
    for line in non_empty:
        msg = line.rsplit(":", 1)[-1].strip()
        if msg in _PERMISSION_TOKENS:
            perm += 1
        else:
            other += 1
    return other == 0 and perm > 0


def _classify_find(exit_code: int, stderr: str, raw_lines: int, result_cap: int):
    """根据 find 退出码 / stderr / 输出量给出主状态。

    返回 ``(state, permission_denied_lines, stderr_lines)``。
    """
    stderr_lines = sum(1 for ln in stderr.splitlines() if ln.strip())
    non_empty = [ln for ln in stderr.splitlines() if ln.strip()]
    perm_lines = 0
    for line in non_empty:
        msg = line.rsplit(":", 1)[-1].strip()
        if msg in _PERMISSION_TOKENS:
            perm_lines += 1
    if exit_code != 0 and _stderr_is_permission_only(stderr):
        return BigfilesState.PERMISSION_DENIED, perm_lines, stderr_lines
    if exit_code != 0 and stderr_lines > 0:
        return BigfilesState.FAILED, perm_lines, stderr_lines
    if exit_code != 0 and stderr_lines == 0:
        # 无 stderr 但非零退出：BSD find 在不可访问目录通常仍输出 root 并以非零
        # 退出；视为失败以避免把真实异常当作空结果
        return BigfilesState.FAILED, perm_lines, stderr_lines
    if raw_lines >= result_cap:
        return BigfilesState.TRUNCATED, perm_lines, stderr_lines
    if raw_lines == 0:
        return BigfilesState.NO_MATCH, perm_lines, stderr_lines
    return BigfilesState.OK, perm_lines, stderr_lines


class BigfilesScopeError(ValueError):
    """查询目录越界/不可用（ISS-150）。

    ``status`` 指示 API 应映射的 HTTP 状态码：400 = 越界/坏参数，
    404 = 路径不存在或不是目录（可能已被移动或删除）。错误消息只包含
    监控根本身（调用方已知），不回显解析后的用户路径。
    """

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def resolve_query_root(raw: Optional[str], *,
                       default_root: Optional[Path] = None) -> Path:
    """把用户请求的查询目录解析为允许范围内的规范目录（ISS-150）。

    合同：
    - ``raw=None`` / 未显式请求 → ``default_root`` 或 ``config.DEFAULT_ROOT``；
    - 字符串层拒绝相对路径与前缀同名根（``/root-evil`` 不是 ``/root``）；
    - resolve 层拒绝 ``..`` 折叠与符号链接越界（与 /api/reveal 同源语义）；
    - 目标不存在或不是目录 → ``status=404``（路径可能已被移动或删除），
      不与越界 400 混淆，也不产生空结果；
    - 返回 ``resolve()`` 后的规范路径；不因历史路径存在而允许读取任意盘。
    """
    base = Path(default_root) if default_root is not None else Path(config.DEFAULT_ROOT)
    if raw is None:
        return base
    if not isinstance(raw, str) or not raw:
        raise BigfilesScopeError("path 参数必须是非空字符串")
    root_str = str(base).rstrip("/") or "/"
    # 根为 / 时前缀即 "/" 本身，不得拼成 "//"（ISS-150 审计返修）；
    # root_str 经 rstrip 后只可能是 "/" 或无尾斜杠路径，此写法对任何
    # 输入都不会产生 "//"。
    prefix = "/" if root_str == "/" else root_str + "/"
    if not (raw == root_str or raw.startswith(prefix)):
        raise BigfilesScopeError(
            f"path 参数必须是监控根 {root_str} 之内的绝对路径")
    try:
        root_real = base.resolve()
        resolved = Path(raw).resolve()
    except (OSError, ValueError, RuntimeError):
        raise BigfilesScopeError("path 参数无法规范化") from None
    if resolved != root_real and root_real not in resolved.parents:
        raise BigfilesScopeError("path 参数规范化后位于监控根之外，已拒绝")
    if not resolved.is_dir():
        raise BigfilesScopeError(
            "path 参数不存在或不是目录（可能已被移动或删除）", status=404)
    return resolved


def _sanitize_path_for_log(path: str) -> str:
    """日志中以 ``<sha8>.../<basename>`` 形式呈现路径，不暴露完整路径。"""
    h = hashlib.sha256(path.encode("utf-8", errors="replace")).hexdigest()[:8]
    base = os.path.basename(path.rstrip("/")) or path
    return f"{h}.../{base}"


def _sanitize_key_for_log(key: tuple) -> str:
    """日志中以 ``(<sanitized_root>, days, min_mb, topn)`` 形式呈现
    ``BigfilesQuery.key``，根路径走 ``_sanitize_path_for_log``，其他字段保留
    可读性。ISS-049 钉死：完整 root 路径不进入任何日志级别。
    """
    sanitized_root = _sanitize_path_for_log(key[0])
    return f"({sanitized_root}, {key[1]}, {key[2]}, {key[3]})"


def _format_mtime(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


class BigfilesFuture:
    """单次大文件查询的可等待句柄。

    - ``cancel()``：取消本次查询（包括所有共享等待者），返回 ``True`` 表示
      取消请求已发出；底层 find 进程组同步收到 SIGTERM。
    - ``result(timeout=None)``：阻塞等待结果；返回 ``BigfilesResult``。
    - ``state()``：返回当前状态字符串。
    """

    __slots__ = ("_inner", "_manager", "_key", "_query", "_cancel_event")

    def __init__(self, manager: "BigfilesManager", key: tuple, query: BigfilesQuery) -> None:
        self._manager = manager
        self._key = key
        self._query = query
        self._inner: concurrent.futures.Future = concurrent.futures.Future()
        self._cancel_event = threading.Event()

    @property
    def query(self) -> BigfilesQuery:
        return self._query

    @property
    def task_id(self) -> str:
        """产品入口句柄（ISS-164）：由去重键确定性派生，见
        ``BigfilesManager.task_id_for`` 的选型理由。"""
        return self._manager.task_id_for(self._key)

    def cancel(self) -> bool:
        if self._inner.done():
            return False
        self._cancel_event.set()
        # 取消受理即在句柄表留痕（ISS-164）：无论取消来自产品入口
        # （cancel_task）、超时回收还是内部调用方，状态查询都能看到
        # 「取消已受理但仍在回收」的中间态。
        self._manager._mark_cancel_requested(self._key, self)
        return self._manager._request_cancel(self._key)

    def cancelled(self) -> bool:
        return self._cancel_event.is_set() or self._inner.cancelled()

    def running(self) -> bool:
        return self._inner.running()

    def done(self) -> bool:
        return self._inner.done()

    def result(self, timeout: Optional[float] = None) -> BigfilesResult:
        return self._inner.result(timeout=timeout)

    def _set_result(self, result: BigfilesResult) -> None:
        if not self._inner.done():
            self._inner.set_result(result)

    def _set_exception(self, exc: BaseException) -> None:
        if not self._inner.done():
            self._inner.set_exception(exc)


class _RunningTask:
    """正在执行的 find 任务（持有进程组句柄与取消事件）。"""

    __slots__ = ("proc", "cancel_event", "wait_thread", "key")

    def __init__(self, proc: subprocess.Popen, cancel_event: threading.Event,
                 key: tuple) -> None:
        self.proc = proc
        self.cancel_event = cancel_event
        self.wait_thread: Optional[threading.Thread] = None
        self.key = key


class BigfilesTaskNotFound(LookupError):
    """未知或已超出保留期的 task_id（ISS-164）；API 层映射 404。

    句柄即凭证：不存在「他人任务」概念，也不区分 404 的两种成因（从未
    登记 / 保留期已过），避免把句柄空间变成可枚举的存在性预言机。
    """

    def __init__(self, task_id: str) -> None:
        super().__init__("未知或已过期的 task_id")
        self.task_id = task_id


@dataclass(slots=True)
class BigfilesTask:
    """一次查询的产品入口句柄记录（ISS-164）。

    ``task_id`` 由去重键派生，故同键的并发去重与按句柄寻址落在同一条任务
    上。终态记录在 ``task_retention_s`` 内保留，使「已完成/已取消任务的
    取消」可以幂等且如实回答；``finished_at`` 同时是终态判据与剪枝依据。
    """

    task_id: str
    key: tuple
    query: BigfilesQuery
    future: BigfilesFuture
    state: str = BigfilesTaskState.RUNNING.value
    created_at: float = 0.0
    finished_at: Optional[float] = None
    error_message: Optional[str] = None
    cancel_requested: bool = False

    def view(self) -> dict:
        """API 状态视图：真实状态 + 生命周期事实；不含结果本体。"""
        return {
            "task_id": self.task_id,
            "state": self.state,
            "terminal": self.finished_at is not None,
            "cancel_requested": self.cancel_requested,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "scope": {
                "root": str(self.query.root),
                "mode": self.query.mode,
                "days": self.query.days,
                "min_mb": self.query.min_mb,
                "topn": self.query.topn,
            },
        }


class BigfilesManager:
    """进程级大文件查询管理器：去重、缓存、取消/超时、find 资源回收。

    参数：
        find_path: find 可执行路径；默认 ``/usr/bin/find``。
        default_timeout_s: 单次 find 默认超时（秒）；``None`` 表示不设超时。
        result_cap: find 输出行的硬上限；超过即视为截断，避免极端目录树把
            内存撑爆。
        cache_ttl_s: 成功结果的有效期；超过即视为 expired。
        task_retention_s: 终态句柄在管理器内的保留时长（秒）；超时后
            状态/取消按未知句柄处理（404），避免句柄表无限增长。
        popen_factory: 注入 ``subprocess.Popen`` 以便测试；签名兼容。
        stat_fn: 注入 ``os.stat`` 以便测试；签名兼容。
    """

    # 类属性默认：允许测试通过 monkeypatch.setattr(BigfilesManager, "_popen_factory", ...)
    # 整体替换，便于在并发去重/取消测试中观察启动次数。
    _popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen

    def __init__(
        self,
        *,
        find_path: str | os.PathLike = "/usr/bin/find",
        default_timeout_s: Optional[float] = 15.0,
        result_cap: int = 1000,
        cache_ttl_s: float = 30.0,
        popen_factory: Optional[Callable[..., subprocess.Popen]] = None,
        stat_fn: Callable[[str], os.stat_result] = os.stat,
        task_retention_s: float = 300.0,
    ) -> None:
        self._find_path = os.fspath(find_path)
        self._default_timeout_s = default_timeout_s
        self._result_cap = max(1, int(result_cap))
        self._cache_ttl_s = max(0.0, float(cache_ttl_s))
        self._task_retention_s = max(0.0, float(task_retention_s))
        if popen_factory is not None:
            self._popen_factory = popen_factory
        self._stat_fn = stat_fn

        self._lock = threading.Lock()
        self._inflight: dict[tuple, BigfilesFuture] = {}
        self._tasks: dict[tuple, _RunningTask] = {}
        self._cache: dict[tuple, tuple[float, BigfilesResult]] = {}
        # 句柄表：task_id -> 任务记录（ISS-164）。键与去重键同源，故同一
        # key 的最新一次运行覆盖旧记录——与「同键同任务」语义一致。
        self._registry: dict[str, BigfilesTask] = {}

    # ---------- 句柄与任务入口（ISS-164） ----------

    def task_id_for(self, key: tuple) -> str:
        """按去重键派生确定性 ``task_id``。

        选确定性哈希而非 UUID 的理由：
        - 去重键 (规范根, mode, days, min_mb, topn, scope_version) 已经唯一
          标识一次查询的语义身份，用它派生句柄可让「同参并发去重」与「按句柄
          寻址/取消」落在同一条任务上：两个去重请求拿到同一句柄，取消该句柄
          回收的正是那条共享 find。UUID 方案要达到同一效果还得额外维护
          key→id 映射，并引入生成与登记之间的竞态窗口（此刻按句柄取消会 404）。
        - 取消因此可以精确绑定「本次运行」：句柄相同即任务相同，前端无需区分
          「我提交的」与「别人共享的」。
        - 只对键的拼接串做 sha256（截断 16 位十六进制）并加 ``bf-`` 前缀，不做
          路径可逆编码，句柄不泄露完整 root 路径（ISS-049 同源口径）。
        """
        raw = "\x1f".join(str(part) for part in key)
        digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()
        return _TASK_ID_PREFIX + digest[:16]

    def status_view(self, task_id: str) -> dict:
        """按句柄返回真实状态（纯读，无副作用）。

        未知或已超出保留期的句柄抛 ``BigfilesTaskNotFound``（API → 404）。
        返回只含生命周期事实与 scope，**不含结果本体**——结果仍从
        ``GET /api/bigfiles`` 按原参数取（命中缓存或在途去重）。
        """
        with self._lock:
            record = self._registry.get(task_id)
        if record is None:
            raise BigfilesTaskNotFound(task_id)
        return record.view()

    def cancel_task(self, task_id: str) -> dict:
        """按句柄取消任务：只作用于该任务的 find 进程组（ISS-164）。

        - 在途：置位 ``cancel_requested`` 并走既有取消路径
          （``BigfilesFuture.cancel`` → 进程组 SIGTERM，回收窗口内升级
          SIGKILL；spawn 窗口由 ISS-123 补投保证「取消受理即生效」），终态由
          runner 落为 ``cancelled``；
        - 已取消：幂等返回 ``cancelled``；
        - 已完成/已过期：幂等返回真实终态，不谎称已取消、不报错；
        - 未知/超出保留期：抛 ``BigfilesTaskNotFound``（API → 404）。

        句柄即凭证：没有「他人任务」概念，也不做调用方归属校验——取消端点的
        越权面由 API 层既有 ``X-Fathom-Token`` 写令牌闸门承担（ISS-022）。
        不在锁内调用 ``future.cancel()``：其内部会取同一把锁。
        """
        with self._lock:
            record = self._registry.get(task_id)
        if record is None:
            raise BigfilesTaskNotFound(task_id)
        if not record.future.done():
            record.future.cancel()
        view = record.view()
        view["cancelled"] = view["state"] == BigfilesTaskState.CANCELLED.value
        if view["cancelled"]:
            view["detail"] = "任务已取消"
        elif view["cancel_requested"]:
            view["detail"] = "取消已受理，find 进程组正在回收"
        else:
            view["detail"] = "任务已结束，取消不再生效（状态如实返回）"
        return view

    # ---------- 公共入口 ----------

    def submit(
        self,
        root: Path | str | None = None,
        days: int | None = None,
        min_mb: int | None = None,
        topn: int = 30,
        *,
        mode: str = "recent",
        timeout: Optional[float] = None,
        force_refresh: bool = False,
        scope_version: Optional[int] = None,
    ) -> BigfilesFuture:
        """显式触发一次查询；并发同参数请求共享同一 ``BigfilesFuture``。

        ``root=None`` 使用 ``config.DEFAULT_ROOT``；``timeout=None`` 使用
        ``default_timeout_s``；``force_refresh=True`` 跳过缓存直接启动新 find
        （仍遵守同参数去重）。

        ``mode``（ISS-150）：``recent``（默认，保持既有行为）或 ``largest``
        （按当前 st_size 逻辑大小排序，不带 mtime 过滤）。``scope_version``
        缺省取 ``config.BIGFILE_SCOPE_VERSION``；缓存/去重键由
        (规范根, mode, days, min_mb, topn, scope_version) 组成，不同模式或
        不同范围配置版本互不共享。

        过期语义（ISS-032 修复）：缓存条目超过 ``cache_ttl_s`` 即视为不存在
        ——首个这样的调用在锁内丢弃过期条目，随后与缓存未命中完全一致地走
        in-flight 去重并启动新 find，返回新任务的 ``BigfilesFuture``（等待
        完成即得新数据，缓存由 ``_runner`` 写入替换）。不再返回携带旧数据的
        ``state=expired`` 终态 future；``EXPIRED`` 枚举仅为兼容 API 状态
        字段合同而保留。
        """
        if mode not in _BIGFILE_MODES:
            raise ValueError(f"mode 必须是 {' 或 '.join(_BIGFILE_MODES)}，收到 {mode!r}")
        root_path = Path(root) if root is not None else config.DEFAULT_ROOT
        eff_days = config.BIGFILE_DEFAULT_DAYS if days is None else int(days)
        eff_mb = config.BIGFILE_DEFAULT_MB if min_mb is None else int(min_mb)
        eff_scope = (config.BIGFILE_SCOPE_VERSION if scope_version is None
                     else int(scope_version))
        query = BigfilesQuery(root=root_path, days=eff_days,
                              min_mb=eff_mb, topn=int(topn),
                              mode=mode, scope_version=eff_scope)
        key = query.key
        effective_timeout = self._default_timeout_s if timeout is None else timeout

        with self._lock:
            if not force_refresh:
                cached = self._cache.get(key)
                if cached is not None:
                    ts, result = cached
                    age = max(0.0, time.monotonic() - ts)
                    if age < self._cache_ttl_s:
                        fresh = BigfilesResult(
                            state=result.state,
                            files=list(result.files),
                            stats=result.stats,
                            error_message=result.error_message,
                            cached=True,
                            cache_age_s=age,
                            truncated=result.truncated,
                            raw_truncated=result.raw_truncated,
                        )
                        future = BigfilesFuture(self, key, query)
                        future._set_result(fresh)
                        # 缓存命中同样登记句柄（终态）：前端可按同一 task_id
                        # 查状态/幂等取消，不会拿到 404。
                        self._register_task_locked(
                            key, query, future, state=fresh.state.value,
                            error_message=fresh.error_message)
                        _LOG.debug("命中缓存：key=%s age=%.2fs",
                                   _sanitize_key_for_log(key), age)
                        return future
                    # 过期条目瞬态处理（ISS-032 修复）：锁内丢弃后继续走下方
                    # in-flight 去重与新 find 启动，本次返回新任务的 future。
                    # 旧实现在此直接 return expired 终态，控制流永远到不了
                    # 启动段，缓存既不删除也不刷新，同参数在进程生命周期内
                    # 拿不到新数据。
                    self._cache.pop(key, None)
                    _LOG.debug("缓存过期，丢弃并启动新查询：key=%s age=%.2fs",
                               _sanitize_key_for_log(key), age)

            existing = self._inflight.get(key)
            if existing is not None:
                return existing

            future = BigfilesFuture(self, key, query)
            self._inflight[key] = future
            task = _RunningTask(proc=None, cancel_event=threading.Event(), key=key)  # type: ignore[arg-type]
            self._tasks[key] = task
            self._register_task_locked(key, query, future)

        thread = threading.Thread(
            target=self._runner,
            name=f"fathom-bigfiles-{'-'.join(str(p) for p in key)}",
            args=(future, task, query, effective_timeout),
            daemon=True,
        )
        task.wait_thread = thread
        thread.start()
        return future

    def cancel(self, key: tuple) -> bool:
        """取消指定参数键的查询（公开入口，方便按 key 取消）。"""
        return self._request_cancel(key)

    def cancel_all(self) -> int:
        """取消全部进行中查询；返回受影响任务数。"""
        with self._lock:
            keys = list(self._tasks.keys())
        count = 0
        for k in keys:
            if self._request_cancel(k):
                count += 1
        return count

    def invalidate_cache(self, key: Optional[tuple] = None) -> None:
        """主动失效缓存；``key=None`` 清空全部。"""
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)

    # ---------- 内部：执行 find ----------

    def _register_task_locked(self, key: tuple, query: BigfilesQuery,
                              future: BigfilesFuture, *,
                              state: Optional[str] = None,
                              error_message: Optional[str] = None) -> BigfilesTask:
        """登记/覆盖句柄记录（调用方持锁）。同键的更新一次运行即覆盖旧记录。"""
        self._prune_tasks_locked()
        now = time.time()
        terminal = state is not None
        record = BigfilesTask(
            task_id=self.task_id_for(key), key=key, query=query, future=future,
            state=state or BigfilesTaskState.RUNNING.value,
            created_at=now, finished_at=now if terminal else None,
            error_message=error_message,
        )
        self._registry[record.task_id] = record
        return record

    def _prune_tasks_locked(self) -> None:
        """丢弃超出保留期的终态句柄（调用方持锁）。在途任务永不剪枝。"""
        if not self._registry:
            return
        now = time.time()
        stale = [tid for tid, rec in self._registry.items()
                 if rec.finished_at is not None
                 and (now - rec.finished_at) > self._task_retention_s]
        for tid in stale:
            self._registry.pop(tid, None)

    def _mark_cancel_requested(self, key: tuple, future: BigfilesFuture) -> None:
        """在句柄表标记「取消已受理」（不代投信号，信号仍走 ``_request_cancel``）。

        ``future is record.future`` 保证只标记本次运行：同键的新一次运行已
        接管句柄时，旧运行的取消不得把新任务标成「取消中」。
        """
        with self._lock:
            record = self._registry.get(self.task_id_for(key))
            if record is not None and record.future is future:
                record.cancel_requested = True

    def _request_cancel(self, key: tuple) -> bool:
        with self._lock:
            task = self._tasks.get(key)
        if task is None or task.cancel_event.is_set():
            return False
        task.cancel_event.set()
        proc = task.proc
        if proc is None or proc.poll() is not None:
            return True
        try:
            os.killpg(proc.pid, 15)  # SIGTERM
        except (ProcessLookupError, PermissionError):
            pass
        return True

    def _runner(self, future: BigfilesFuture, task: _RunningTask,
                query: BigfilesQuery, timeout: Optional[float]) -> None:
        result: BigfilesResult
        try:
            result = self._run_find(query, task, timeout)
        except _Cancelled:
            self._finalize(future, task, cancelled=True)
            return
        except _FindFailure as exc:
            result = BigfilesResult(
                state=BigfilesState.FAILED,
                error_message=str(exc),
                stats=exc.stats,
            )
        except Exception as exc:  # noqa: BLE001
            result = BigfilesResult(
                state=BigfilesState.FAILED,
                error_message=f"内部错误：{exc.__class__.__name__}",
            )
            _LOG.exception("bigfiles 查询异常：key=%s",
                           _sanitize_key_for_log(task.key))

        if result.state != BigfilesState.FAILED:
            with self._lock:
                self._cache[task.key] = (time.monotonic(), result)

        self._finalize(future, task, result=result)

    def _finalize(self, future: BigfilesFuture, task: _RunningTask,
                  *, result: Optional[BigfilesResult] = None,
                  cancelled: bool = False) -> None:
        with self._lock:
            self._inflight.pop(task.key, None)
            self._tasks.pop(task.key, None)
            # 终态先落句柄表再投递 future：状态查询不会看到「future 已完成
            # 但状态仍 running」的窗口。``future is record.future`` 保证只更新
            # 本次运行的记录——同键的新一次运行已接管句柄时，旧运行不得覆盖
            # 新任务的状态（否则会谎报取消/完成）。
            record = self._registry.get(self.task_id_for(task.key))
            if record is not None and record.future is future:
                if cancelled:
                    record.state = BigfilesTaskState.CANCELLED.value
                    record.error_message = None
                else:
                    assert result is not None
                    record.state = result.state.value
                    record.error_message = result.error_message
                record.finished_at = time.time()
        if cancelled:
            # 始终向 future 投递 CancelledError；_inner 尚未 done 时 set_exception
            # 才会被 future.result() 正确捕获。
            future._set_exception(concurrent.futures.CancelledError())
        else:
            assert result is not None
            future._set_result(result)

    def _run_find(self, query: BigfilesQuery, task: _RunningTask,
                  timeout: Optional[float]) -> BigfilesResult:
        """按查询模式分派执行（ISS-150）：recent 走既有路径，largest 走
        增量读取 + 时间/输出预算路径。"""
        if query.mode == "largest":
            return self._run_find_largest(query, task, timeout)
        return self._run_find_recent(query, task, timeout)

    def _run_find_recent(self, query: BigfilesQuery, task: _RunningTask,
                         timeout: Optional[float]) -> BigfilesResult:
        root = query.root
        min_bytes = query.min_mb * 1024 * 1024
        args = [
            self._find_path,
            str(root),
            "-xdev",
            "-type", "f",
            "-size", f"+{min_bytes}c",
            "-mtime", f"-{query.days}",
            "-print0",
        ]
        _LOG.info(
            "bigfiles 启动：sanitized_root=%s mode=%s days=%d min_mb=%d topn=%d timeout=%s",
            _sanitize_path_for_log(str(root)), query.mode, query.days, query.min_mb,
            query.topn, timeout,
        )

        started = time.time()
        start = time.monotonic()
        try:
            proc = self._popen_factory(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise _FindFailure(f"未找到 find：{self._find_path}")
        except OSError as exc:
            raise _FindFailure(f"无法启动 find：{exc}")
        task.proc = proc
        if task.cancel_event.is_set():
            # spawn 窗口取消补投（ISS-123）：_request_cancel 在 task.proc 赋值
            # 前受理取消时进程尚不存在，无法投递组信号却已向调用方返回 True；
            # 此刻进程已创建，立即补投组信号，使“取消已受理”必然生效。仅投
            # SIGTERM，升级回收仍走下方 cancel_event 检测路径与超时路径的
            # _terminate_group——取消/超时/回收合同不变。可见性：cancel 方先
            # 置位 cancel_event 再读 task.proc，本方先赋值 task.proc 再读
            # cancel_event，Event 的 happens-before 保证任一侧都不会双向错过。
            try:
                os.killpg(proc.pid, 15)  # SIGTERM
            except (ProcessLookupError, PermissionError):
                pass

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            elapsed_ms = int((time.monotonic() - start) * 1000)
            rusage = resource.getrusage(resource.RUSAGE_CHILDREN)
            stats = BigfilesStats(
                wall_ms=elapsed_ms,
                peak_rss_bytes=rusage.ru_maxrss,
                find_output_lines=0,
                find_exit_code=proc.returncode if proc.returncode is not None else -1,
                find_stderr_lines=sum(1 for ln in stderr.splitlines() if ln.strip()) if stderr else 0,
                permission_denied_lines=0,
                started_at=started,
                finished_at=time.time(),
            )
            raise _FindFailure(f"find 超时（>{timeout}s）", stats=stats)

        if task.cancel_event.is_set():
            try:
                os.killpg(proc.pid, 0)
                cancelled_alive = True
            except (ProcessLookupError, PermissionError):
                cancelled_alive = False
            elapsed_ms = int((time.monotonic() - start) * 1000)
            rusage = resource.getrusage(resource.RUSAGE_CHILDREN)
            stats = BigfilesStats(
                wall_ms=elapsed_ms,
                peak_rss_bytes=rusage.ru_maxrss,
                find_output_lines=0,
                find_exit_code=proc.returncode if proc.returncode is not None else -1,
                find_stderr_lines=sum(1 for ln in stderr.splitlines() if ln.strip()) if stderr else 0,
                permission_denied_lines=0,
                started_at=started,
                finished_at=time.time(),
            )
            if cancelled_alive:
                self._terminate_group(proc)
            _LOG.info("bigfiles 已取消：wall_ms=%d exit=%s",
                      stats.wall_ms, stats.find_exit_code)
            raise _Cancelled(stats=stats)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        finished = time.time()
        rusage = resource.getrusage(resource.RUSAGE_CHILDREN)
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        stdout_text = stdout or b""

        raw_paths = stdout_text.split(b"\0")
        # find 末尾以 \0 结尾；split 产生空尾段；过滤
        raw_count = sum(1 for r in raw_paths if r)
        raw_truncated = raw_count >= self._result_cap

        files = self._stat_and_rank(raw_paths, query, extended=False)
        truncated = len(files) > query.topn
        files = files[:query.topn]

        state, perm_lines, stderr_lines = _classify_find(
            proc.returncode or 0, stderr_text, raw_count, self._result_cap,
        )

        stats = BigfilesStats(
            wall_ms=elapsed_ms,
            peak_rss_bytes=rusage.ru_maxrss,
            find_output_lines=raw_count,
            find_exit_code=proc.returncode if proc.returncode is not None else -1,
            find_stderr_lines=stderr_lines,
            permission_denied_lines=perm_lines,
            started_at=started,
            finished_at=finished,
        )

        if state == BigfilesState.OK and truncated:
            state = BigfilesState.TRUNCATED
        elif state == BigfilesState.NO_MATCH and stderr_lines > 0 and perm_lines > 0:
            state = BigfilesState.PERMISSION_DENIED

        error_message: Optional[str] = None
        if state == BigfilesState.PERMISSION_DENIED:
            error_message = f"find 退出 {stats.find_exit_code}，{perm_lines} 行权限受限"
        elif state == BigfilesState.FAILED:
            sample = stderr_text.splitlines()[0] if stderr_text else ""
            error_message = f"find 退出 {stats.find_exit_code}：{sample[:160]}"

        _LOG.info(
            "bigfiles 完成：state=%s files=%d wall_ms=%d rss_bytes=%d "
            "exit=%d stderr_lines=%d perm_lines=%d",
            state.value, len(files), stats.wall_ms, stats.peak_rss_bytes,
            stats.find_exit_code, stats.find_stderr_lines, stats.permission_denied_lines,
        )

        return BigfilesResult(
            state=state,
            files=files,
            stats=stats,
            error_message=error_message,
            truncated=truncated or raw_truncated,
            raw_truncated=raw_truncated,
        )

    def _stat_and_rank(self, raw_paths: list[bytes], query: BigfilesQuery,
                       *, extended: bool) -> list[dict]:
        """对 find 输出路径做 stat 并按当前 st_size 逻辑大小降序排列。

        ``extended=True``（largest）时条目附 ``blocks``（物理占用字节）与
        ``sparse``（物理占用小于逻辑大小的稀疏标识）；recent 保持
        ``{path, size, mtime}`` 三字段合同不变。stat 失败（路径在遍历与
        stat 之间被移动/删除）的条目跳过。返回列表未做 topn 截断。
        """
        stat_limit = min(len(raw_paths), max(query.topn, self._result_cap))
        files: list[dict] = []
        for raw in raw_paths[:stat_limit]:
            if not raw:
                continue
            path = os.fsdecode(raw)
            try:
                st = self._stat_fn(path)
            except OSError:
                continue
            entry = {
                "path": path,
                "size": st.st_size,
                "mtime": _format_mtime(st.st_mtime),
            }
            if extended:
                blocks = st.st_blocks * 512
                entry["blocks"] = blocks
                entry["sparse"] = st.st_size > 0 and blocks < st.st_size
            files.append(entry)
        files.sort(key=lambda x: x["size"], reverse=True)
        return files

    def _run_find_largest(self, query: BigfilesQuery, task: _RunningTask,
                          timeout: Optional[float]) -> BigfilesResult:
        """largest 模式（ISS-150）：当前逻辑大小最大的文件，不带 mtime 过滤。

        与 recent 的差别：
        - find 不带 ``-mtime``，其余过滤参数（-xdev/-type/-size/-print0）
          一致；find 默认不跟随目录符号链接（-P），配合调用方的
          ``resolve_query_root`` 范围解析，遍历不会越出规范根。
        - 增量读取 stdout/stderr（selectors，bufsize=0 直读）：时间预算
          ``timeout`` 到点或输出预算 ``BIGFILE_LARGEST_MAX_OUTPUT_BYTES``
          触顶时回收进程，返回已解析部分并标记 ``incomplete=True``——
          按 ISS-150 合同仅称「已检查文件中的较大项」，不作为 FAILED
          丢弃已收集数据。
        - 完整遍历（双管道 EOF + find 自然退出）才允许 OK / NO_MATCH 语义。
        - 取消合同与 recent 相同：spawn 窗口补投组信号（ISS-123）、
          取消只作用于本请求 key 的进程。
        """
        root = query.root
        min_bytes = query.min_mb * 1024 * 1024
        args = [
            self._find_path,
            str(root),
            "-xdev",
            "-type", "f",
            "-size", f"+{min_bytes}c",
            "-print0",
        ]
        _LOG.info(
            "bigfiles 启动：sanitized_root=%s mode=%s days=%d min_mb=%d topn=%d timeout=%s",
            _sanitize_path_for_log(str(root)), query.mode, query.days, query.min_mb,
            query.topn, timeout,
        )

        started = time.time()
        start = time.monotonic()
        try:
            proc = self._popen_factory(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                bufsize=0,  # raw 流，配合 os.read 增量读取
            )
        except FileNotFoundError:
            raise _FindFailure(f"未找到 find：{self._find_path}")
        except OSError as exc:
            raise _FindFailure(f"无法启动 find：{exc}")
        task.proc = proc
        if task.cancel_event.is_set():
            # spawn 窗口取消补投（ISS-123）：与 recent 同一合同，取消已受理
            # 即必然生效。cancel 方先置位 cancel_event 再读 task.proc，本方
            # 先赋值 task.proc 再读 cancel_event，Event 的 happens-before
            # 保证任一侧都不会双向错过。
            try:
                os.killpg(proc.pid, 15)  # SIGTERM
            except (ProcessLookupError, PermissionError):
                pass

        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        partial_reason: Optional[str] = None  # None | "time" | "output"
        deadline = (start + timeout) if timeout is not None else None
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, "out")
        sel.register(proc.stderr, selectors.EVENT_READ, "err")
        open_streams = 2
        try:
            while open_streams:
                if task.cancel_event.is_set():
                    try:
                        os.killpg(proc.pid, 0)
                        alive = True
                    except (ProcessLookupError, PermissionError):
                        alive = False
                    if alive:
                        self._terminate_group(proc)
                    raise _Cancelled(stats=BigfilesStats(
                        wall_ms=int((time.monotonic() - start) * 1000),
                        started_at=started, finished_at=time.time(),
                    ))
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    partial_reason = "time"
                    self._terminate_group(proc)
                    break
                if deadline is None:
                    sel_timeout = 0.5
                else:
                    sel_timeout = max(0.001, min(0.5, deadline - now))
                events = sel.select(timeout=sel_timeout)
                for key, _mask in events:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        key.fileobj.close()
                        open_streams -= 1
                        continue
                    if key.data == "out":
                        out_chunks.append(chunk)
                        if (sum(len(c) for c in out_chunks)
                                >= config.BIGFILE_LARGEST_MAX_OUTPUT_BYTES):
                            partial_reason = "output"
                    else:
                        err_chunks.append(chunk)
                if partial_reason == "output":
                    self._terminate_group(proc)
                    break
        finally:
            sel.close()
        # 收尸：EOF / 预算路径之后确保进程对象退出（正常路径 wait 立即返回）
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        # EOF 退出也可能恰逢取消（SIGTERM 使管道 EOF 先于循环内检查）：
        # 与 recent 合同一致，cancel() 已受理（返回 True）的查询一律投递
        # CancelledError，不把残缺输出当正常结果返回。
        if task.cancel_event.is_set():
            raise _Cancelled(stats=BigfilesStats(
                wall_ms=int((time.monotonic() - start) * 1000),
                started_at=started, finished_at=time.time(),
            ))
        finished = time.time()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        rusage = resource.getrusage(resource.RUSAGE_CHILDREN)

        stdout_bytes = b"".join(out_chunks)
        stderr_text = b"".join(err_chunks).decode("utf-8", errors="replace")
        raw_paths = stdout_bytes.split(b"\0")
        raw_count = sum(1 for r in raw_paths if r)
        raw_truncated = raw_count >= self._result_cap

        files = self._stat_and_rank(raw_paths, query, extended=True)
        truncated = len(files) > query.topn
        files = files[:query.topn]

        stderr_lines = sum(1 for ln in stderr_text.splitlines() if ln.strip())
        perm_lines = sum(
            1 for line in stderr_text.splitlines()
            if line.strip() and line.rsplit(":", 1)[-1].strip() in _PERMISSION_TOKENS
        )
        exit_code = proc.returncode if proc.returncode is not None else -1

        incomplete = partial_reason is not None or raw_truncated
        if partial_reason is not None:
            # 预算提前截断：仅称「已检查文件中的较大项」，披露不完整
            state = BigfilesState.TRUNCATED
            reason_cn = "时间" if partial_reason == "time" else "输出"
            error_message = (f"{reason_cn}预算内未完成全量遍历，仅返回已检查"
                             "文件中的较大项，结果不完整")
            if perm_lines > 0:
                error_message += f"；{perm_lines} 行权限受限"
        else:
            state, perm_cls, stderr_cls = _classify_find(
                exit_code, stderr_text, raw_count, self._result_cap,
            )
            if state == BigfilesState.OK and truncated:
                state = BigfilesState.TRUNCATED
            elif state == BigfilesState.NO_MATCH and stderr_cls > 0 and perm_lines > 0:
                state = BigfilesState.PERMISSION_DENIED
            error_message = None
            if state == BigfilesState.PERMISSION_DENIED:
                error_message = f"find 退出 {exit_code}，{perm_lines} 行权限受限"
            elif state == BigfilesState.FAILED:
                sample = stderr_text.splitlines()[0] if stderr_text else ""
                error_message = f"find 退出 {exit_code}：{sample[:160]}"
            elif raw_truncated:
                error_message = ("输出预算内未完成全量遍历，仅返回已检查文件中"
                                 "的较大项，结果不完整")

        stats = BigfilesStats(
            wall_ms=elapsed_ms,
            peak_rss_bytes=rusage.ru_maxrss,
            find_output_lines=raw_count,
            find_exit_code=exit_code,
            find_stderr_lines=stderr_lines,
            permission_denied_lines=perm_lines,
            started_at=started,
            finished_at=finished,
        )
        _LOG.info(
            "bigfiles 完成：state=%s files=%d incomplete=%s wall_ms=%d "
            "rss_bytes=%d exit=%d stderr_lines=%d perm_lines=%d",
            state.value, len(files), incomplete, stats.wall_ms,
            stats.peak_rss_bytes, exit_code, stderr_lines, perm_lines,
        )
        return BigfilesResult(
            state=state,
            files=files,
            stats=stats,
            error_message=error_message,
            truncated=truncated or raw_truncated,
            raw_truncated=raw_truncated,
            incomplete=incomplete,
        )

    def _terminate_group(self, proc: subprocess.Popen) -> None:
        """先 SIGTERM 再升级 SIGKILL，确保 find 子进程组被回收。"""
        try:
            os.killpg(proc.pid, 15)  # SIGTERM
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, 9)  # SIGKILL
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


class _Cancelled(Exception):
    def __init__(self, stats: Optional[BigfilesStats] = None) -> None:
        super().__init__("bigfiles 查询已取消")
        self.stats = stats or BigfilesStats()


class _FindFailure(Exception):
    def __init__(self, message: str, stats: Optional[BigfilesStats] = None) -> None:
        super().__init__(message)
        self.stats = stats or BigfilesStats()


# ---------- 进程级单例与向后兼容包装 ----------


_DEFAULT_MANAGER: Optional[BigfilesManager] = None
_DEFAULT_LOCK = threading.Lock()


def _get_default_manager() -> BigfilesManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_MANAGER is None:
                _DEFAULT_MANAGER = BigfilesManager()
    return _DEFAULT_MANAGER


def configure_default_manager(manager: Optional[BigfilesManager]) -> BigfilesManager:
    """注入默认管理器（测试或配置加载使用）；``None`` 重置。"""
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = manager
    return _DEFAULT_MANAGER if manager is not None else BigfilesManager()


def submit(root: Path | str | None = None, days: int | None = None,
           min_mb: int | None = None, topn: int = 30, *,
           mode: str = "recent",
           timeout: Optional[float] = None,
           force_refresh: bool = False) -> BigfilesFuture:
    """便捷入口：使用模块默认管理器提交一次查询。"""
    root_path = Path(root) if root else config.DEFAULT_ROOT
    eff_days = config.BIGFILE_DEFAULT_DAYS if days is None else int(days)
    eff_mb = config.BIGFILE_DEFAULT_MB if min_mb is None else int(min_mb)
    return _get_default_manager().submit(
        root_path, eff_days, eff_mb, topn,
        mode=mode, timeout=timeout, force_refresh=force_refresh,
    )


def find_big_files(
    root: Path | None = None,
    days: int | None = None,
    min_mb: int | None = None,
    topn: int = 30,
    *,
    mode: str = "recent",
    timeout: Optional[float] = 30.0,
) -> list[dict]:
    """向后兼容同步接口（api.py 现存调用方；CLI ``cmd_bigfiles`` 与
    ``--with-bigfiles`` 日报路径同样经此取列表）。

    行为：
    - 阻塞等待至完成、取消或超时；
    - find 异常时抛出 ``BigfilesError``；
    - ``topn`` 截断与缓存均沿用管理器语义（过期即刷新，见 ``submit``）；
    - 缓存命中或正常返回仅取 ``files`` 字段（与旧合同一致）；
    - 返回值始终是 ``list[dict]``：``EXPIRED`` 状态一律抛 ``BigfilesError``
      而非静默返回旧 ``files``（ISS-032 修复合同；修复后 ``submit`` 不再
      产生该状态，此守卫保证同步/报告路径永不无辨析地嵌入过期列表）；
    - ``mode``（ISS-150）：``recent``（默认，保持既有行为）或 ``largest``。
    """
    future = submit(root=root, days=days, min_mb=min_mb, topn=topn,
                    mode=mode, timeout=timeout)
    try:
        result = future.result(timeout=timeout)
    except concurrent.futures.CancelledError as exc:
        raise BigfilesError("大文件查询已取消") from exc
    except concurrent.futures.TimeoutError as exc:
        raise BigfilesError("大文件查询超时") from exc
    if result.state == BigfilesState.FAILED:
        raise BigfilesError(result.error_message or "find 失败")
    if result.state == BigfilesState.PERMISSION_DENIED:
        raise BigfilesError(result.error_message or "find 权限受限")
    if result.state == BigfilesState.EXPIRED:
        raise BigfilesError("大文件缓存已过期：结果不可静默使用，请重新查询")
    return list(result.files)


class BigfilesError(RuntimeError):
    """``find_big_files`` 同步包装在失败/权限/取消时抛出的对外错误。"""


__all__ = [
    "BigfilesState",
    "BigfilesTaskState",
    "BigfilesQuery",
    "BigfilesStats",
    "BigfilesResult",
    "BigfilesFuture",
    "BigfilesTask",
    "BigfilesTaskNotFound",
    "BigfilesManager",
    "BigfilesError",
    "BigfilesScopeError",
    "submit",
    "find_big_files",
    "resolve_query_root",
    "configure_default_manager",
]