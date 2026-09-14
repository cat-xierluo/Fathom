"""近期大文件查询：find 近 N 天修改过的大文件。

合同（ISS-032）：
- 显式触发：调用方主动 ``submit`` 才会启动 find，不再每次请求实时遍历。
- 单参数去重：相同 (root, days, min_mb, topn) 的并发请求只启动一次 find，
  等待者共享同一 ``BigfilesFuture``。
- 取消/超时：调用 ``future.cancel()`` 或超过 ``timeout`` 时，向 find 进程组
  发送 SIGTERM，超时回收窗口内仍存活则升级为 SIGKILL；所有等待者同步收到
  ``CancelledError`` / 超时状态。
- TTL 缓存：成功结果缓存 ``cache_ttl_s``；TTL 内同参数请求直接命中缓存。
- 过期可辨：TTL 之外调用获得 ``state=expired`` 的缓存结果，缓存仍可用于对比。
- 结果上限截断可辨：find 实际输出命中 ``result_cap`` 或请求 ``topn`` 时，结果
  携带 ``truncated=True`` 与未截断的原始计数。
- 五态区分（不含 OK 共六态）：OK / NO_MATCH / PERMISSION_DENIED / FAILED /
  TRUNCATED / EXPIRED。find 的 stderr 含 Permission denied / Operation not
  permitted 视为权限受限；其他非零退出或 stderr 报错视为 FAILED。
- 原始路径只进本地必要日志（``fathom.bigfiles`` logger），日志形如
  ``<sha8>.../<basename>``，完整路径永不进入日志或返回值之外。

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


@dataclass(frozen=True, slots=True)
class BigfilesQuery:
    root: Path
    days: int
    min_mb: int
    topn: int

    @property
    def key(self) -> tuple:
        return (str(self.root), int(self.days), int(self.min_mb), int(self.topn))


@dataclass(slots=True)
class BigfilesStats:
    """单次 find 资源/输出统计。"""

    wall_ms: int = 0
    peak_rss_bytes: int = 0          # 子进程峰值 RSS（字节）
    find_output_lines: int = 0
    find_exit_code: int = 0
    find_stderr_lines: int = 0
    permission_denied_lines: int = 0


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


def _sanitize_path_for_log(path: str) -> str:
    """日志中以 ``<sha8>.../<basename>`` 形式呈现路径，不暴露完整路径。"""
    h = hashlib.sha256(path.encode("utf-8", errors="replace")).hexdigest()[:8]
    base = os.path.basename(path.rstrip("/")) or path
    return f"{h}.../{base}"


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

    def cancel(self) -> bool:
        if self._inner.done():
            return False
        self._cancel_event.set()
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


class BigfilesManager:
    """进程级大文件查询管理器：去重、缓存、取消/超时、find 资源回收。

    参数：
        find_path: find 可执行路径；默认 ``/usr/bin/find``。
        default_timeout_s: 单次 find 默认超时（秒）；``None`` 表示不设超时。
        result_cap: find 输出行的硬上限；超过即视为截断，避免极端目录树把
            内存撑爆。
        cache_ttl_s: 成功结果的有效期；超过即视为 expired。
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
    ) -> None:
        self._find_path = os.fspath(find_path)
        self._default_timeout_s = default_timeout_s
        self._result_cap = max(1, int(result_cap))
        self._cache_ttl_s = max(0.0, float(cache_ttl_s))
        if popen_factory is not None:
            self._popen_factory = popen_factory
        self._stat_fn = stat_fn

        self._lock = threading.Lock()
        self._inflight: dict[tuple, BigfilesFuture] = {}
        self._tasks: dict[tuple, _RunningTask] = {}
        self._cache: dict[tuple, tuple[float, BigfilesResult]] = {}

    # ---------- 公共入口 ----------

    def submit(
        self,
        root: Path | str | None = None,
        days: int | None = None,
        min_mb: int | None = None,
        topn: int = 30,
        *,
        timeout: Optional[float] = None,
        force_refresh: bool = False,
    ) -> BigfilesFuture:
        """显式触发一次查询；并发同参数请求共享同一 ``BigfilesFuture``。

        ``root=None`` 使用 ``config.DEFAULT_ROOT``；``timeout=None`` 使用
        ``default_timeout_s``；``force_refresh=True`` 跳过缓存直接启动新 find
        （仍遵守同参数去重）。
        """
        root_path = Path(root) if root is not None else config.DEFAULT_ROOT
        eff_days = config.BIGFILE_DEFAULT_DAYS if days is None else int(days)
        eff_mb = config.BIGFILE_DEFAULT_MB if min_mb is None else int(min_mb)
        query = BigfilesQuery(root=root_path, days=eff_days,
                              min_mb=eff_mb, topn=int(topn))
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
                        _LOG.debug("命中缓存：key=%s age=%.2fs", key, age)
                        return future
                    expired = BigfilesResult(
                        state=BigfilesState.EXPIRED,
                        files=list(result.files),
                        stats=result.stats,
                        error_message=result.error_message,
                        cached=True,
                        cache_age_s=age,
                        truncated=result.truncated,
                        raw_truncated=result.raw_truncated,
                    )
                    future = BigfilesFuture(self, key, query)
                    future._set_result(expired)
                    _LOG.debug("缓存过期：key=%s age=%.2fs", key, age)
                    return future

            existing = self._inflight.get(key)
            if existing is not None:
                return existing

            future = BigfilesFuture(self, key, query)
            self._inflight[key] = future
            task = _RunningTask(proc=None, cancel_event=threading.Event(), key=key)  # type: ignore[arg-type]
            self._tasks[key] = task

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
            _LOG.exception("bigfiles 查询异常：key=%s", task.key)

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
        if cancelled:
            # 始终向 future 投递 CancelledError；_inner 尚未 done 时 set_exception
            # 才会被 future.result() 正确捕获。
            future._set_exception(concurrent.futures.CancelledError())
        else:
            assert result is not None
            future._set_result(result)

    def _run_find(self, query: BigfilesQuery, task: _RunningTask,
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
            "bigfiles 启动：sanitized_root=%s days=%d min_mb=%d topn=%d timeout=%s",
            _sanitize_path_for_log(str(root)), query.days, query.min_mb,
            query.topn, timeout,
        )

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
            )
            if cancelled_alive:
                self._terminate_group(proc)
            _LOG.info("bigfiles 已取消：wall_ms=%d exit=%s",
                      stats.wall_ms, stats.find_exit_code)
            raise _Cancelled(stats=stats)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        rusage = resource.getrusage(resource.RUSAGE_CHILDREN)
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        stdout_text = stdout or b""

        raw_paths = stdout_text.split(b"\0")
        # find 末尾以 \0 结尾；split 产生空尾段；过滤
        raw_count = sum(1 for r in raw_paths if r)
        raw_truncated = raw_count >= self._result_cap

        # 限制 stat 调用次数：topn 决定返回数量，但若 result_cap 截断，仍按
        # topn 取前 N 个 stat
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
            files.append({
                "path": path,
                "size": st.st_size,
                "mtime": _format_mtime(st.st_mtime),
            })
        files.sort(key=lambda x: x["size"], reverse=True)
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
           timeout: Optional[float] = None,
           force_refresh: bool = False) -> BigfilesFuture:
    """便捷入口：使用模块默认管理器提交一次查询。"""
    root_path = Path(root) if root else config.DEFAULT_ROOT
    eff_days = config.BIGFILE_DEFAULT_DAYS if days is None else int(days)
    eff_mb = config.BIGFILE_DEFAULT_MB if min_mb is None else int(min_mb)
    return _get_default_manager().submit(
        root_path, eff_days, eff_mb, topn,
        timeout=timeout, force_refresh=force_refresh,
    )


def find_big_files(
    root: Path | None = None,
    days: int | None = None,
    min_mb: int | None = None,
    topn: int = 30,
    *,
    timeout: Optional[float] = 30.0,
) -> list[dict]:
    """向后兼容同步接口（api.py 现存调用方）。

    行为：
    - 阻塞等待至完成、取消或超时；
    - find 异常时抛出 ``BigfilesError``；
    - ``topn`` 截断与缓存均沿用管理器语义；
    - 缓存命中或正常返回仅取 ``files`` 字段（与旧合同一致）。
    """
    future = submit(root=root, days=days, min_mb=min_mb, topn=topn, timeout=timeout)
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
    return list(result.files)


class BigfilesError(RuntimeError):
    """``find_big_files`` 同步包装在失败/权限/取消时抛出的对外错误。"""


__all__ = [
    "BigfilesState",
    "BigfilesQuery",
    "BigfilesStats",
    "BigfilesResult",
    "BigfilesFuture",
    "BigfilesManager",
    "BigfilesError",
    "submit",
    "find_big_files",
    "configure_default_manager",
]