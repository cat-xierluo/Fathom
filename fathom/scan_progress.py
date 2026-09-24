"""扫描进度实时反馈（ISS-090）：du 流式计数与跨进程状态文件。

用户反馈（2026-09-24）：点「立即扫描」后没有任何运行进展的显示，只能干等。
du 的 stdout 是流式的（每条 "大小\\t路径" 记录实时产出），行数与已见字节
在扫描进行中即可得——本模块把这两个事实计数以低频节流写入运行根下的
``scan-progress.json`` 状态文件，API 进程（浏览器态同进程、打包态另一个
helper 进程）只读该文件即可向前端提供 live 进度，不依赖扫描进程本身。

选型（对比 scan_run_details 加列，理由详见 ISS-090 PR）：
- 状态文件 + 原子替换写（tmp + os.replace，与 settings.json /
  ScanLease 锁文件同模式）避免每 2 秒一次 UPDATE 的 SQLite 写放大，
  也不需要 v5→v6 迁移链；旧运行根没有该文件即「无进行中进度」，
  天然兼容（schema 零变化）。
- 判活由读取方按 heartbeat 时效完成：扫描进程崩溃残留的 stale 文件
  不会冒充进行中进度（超时即视为无 live）；正常结束/中断/失败由
  ProgressReporter.close 删除文件。

du 无总量分母——不做百分比，只做「进行中的事实计数」：
已扫目录数（完整记录条数）、已见累计大小（已结清目录的净值 KB，
见 DuStreamCounter）、已运行时长。du 输出是目录累计大小，逐行相加
会把父子重复计算（AGENTS「目录累计大小不可逐行相加」同源约束），
净值结清算法保证终值恰等于根的累计大小。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config

# 状态文件名（位于当前运行根下，与 settings.json / helper-instance.json 同层）。
SCAN_PROGRESS_FILENAME = "scan-progress.json"

# 写节流：du 轮询每 0.2s 醒一次（scanner.run_du），进度写盘至多该频率。
WRITE_INTERVAL_S = 2.0

# 判活阈值：heartbeat 距读取时点超过该秒数即视为 stale（扫描进程已死或
# 僵死）。写节流 2s，正常运行时 heartbeat 每次轮询超时都会刷新（时间驱动，
# 与 du 是否产出新行无关——du 卡在慢速卷上时进程仍活着，心跳不该停）；
# 30s 阈值给出一个数量级的余量，覆盖调度饥饿与短暂 GC 停顿。
STALE_AFTER_S = 30.0


def progress_path() -> Path:
    """状态文件规范位置：当前运行根下。"""
    return config.get_runtime_config().runtime_dir / SCAN_PROGRESS_FILENAME


class DuStreamCounter:
    """du 流式输出的事实计数器：完整记录条数与已结清净值 KB。

    du（BSD，-xk）输出严格后序：目录的子孙全部先于该目录输出。据此每条
    记录出现时其全部直接孩子必然已经输出并被弹出——每条记录可立即结清
    「净值 = 自身累计大小 − 直接孩子累计大小之和」，全部结清的净值相加
    恰等于根的累计大小（telescoping），中途值单调不减且绝不重复计算
    父子重叠（du 的累计语义下逐行相加会数出 2-3 倍总量，不可用）。

    被跳过的子树（du -I 掩码）缺少孩子记录：父结清时把缺口的量并入父
    自身净值，总量仍然精确；硬链接去重等 du 语义不影响目录级 telescoping。
    """

    def __init__(self) -> None:
        self._stack: list[tuple[int, int, int, int]] = []  # (depth, subtree_kb, own_children, carried)
        self._dirs = 0
        self._bytes_kb = 0

    @property
    def dirs_scanned(self) -> int:
        return self._dirs

    @property
    def bytes_seen_kb(self) -> int:
        return self._bytes_kb

    def feed_record(self, path: str, size_kb: int) -> None:
        """消费一条完整 du 记录（已按 "大小\\t路径" 拆分）。"""
        depth = path.rstrip("/").count("/")
        own = 0      # 新记录的直接孩子 subtree 之和（结清用）
        carried = 0  # 新记录代持的旁系 subtree（等待共同父认领）
        while self._stack and self._stack[-1][0] >= depth:
            item_depth, subtree, own_children, item_carried = self._stack.pop()
            self._bytes_kb += subtree - own_children
            total = subtree + item_carried
            if item_depth == depth + 1:
                own += total
            else:
                carried += total
        self._stack.append((depth, size_kb, own, carried))
        self._dirs += 1

    def flush(self) -> None:
        """du 退出后结清栈内剩余记录（含根，终值在此闭合）。"""
        while self._stack:
            _depth, subtree, own_children, _carried = self._stack.pop()
            self._bytes_kb += subtree - own_children


class ProgressReporter:
    """du 轮询驱动的进度写入器：增量喂数 + 时间节流原子写状态文件。

    生命周期（由 scan_coordinator.ScanSession 持有）：
    ``start()``（写初值）→ ``feed_chunk()``（run_du 轮询循环每 0.2s 调，
    参数是**累计** stdout 快照，内部维护已消费 offset 增量解析）→
    ``finish()``（du 退出：喂尾段并写终值）→ ``close()``（扫描结束/
    中断/失败时删除文件）。

    一切写/删失败都被吞掉（进度是尽力而为的诊断信息，绝不让扫描因
    进度通道故障而失败）。``clock`` 可注入假时钟供测试节流断言。
    """

    def __init__(
        self,
        *,
        run_id: int,
        path: Path | None = None,
        interval_s: float | None = None,
        clock=time.monotonic,
    ) -> None:
        self._path = Path(path) if path is not None else progress_path()
        self._run_id = run_id
        # None → 模块常量现值（测试可 monkeypatch WRITE_INTERVAL_S 缩短节流，
        # 让集成测试在秒级 du 窗口内观测到中途 live 写入）。
        self._interval_s = WRITE_INTERVAL_S if interval_s is None else interval_s
        self._clock = clock
        self._counter = DuStreamCounter()
        self._offset = 0            # 已从累计快照消费到的字节位置
        self._pending = b""         # 尚无换行结尾的残片（下次续拼）
        self._started_epoch: float | None = None
        self._last_write: float | None = None
        self._closed = False

    # ---- 计数（测试与诊断可读） ----
    @property
    def dirs_scanned(self) -> int:
        return self._counter.dirs_scanned

    @property
    def bytes_seen_kb(self) -> int:
        return self._counter.bytes_seen_kb

    # ---- 生命周期 ----
    def start(self) -> None:
        """扫描进入 du 阶段：落初值文件（dirs=0 / bytes=0 / 心跳=now）。"""
        self._started_epoch = time.time()
        self._last_write = self._clock()
        self._write()

    def feed_chunk(self, accumulated: bytes) -> None:
        """消费 run_du 轮询给出的累计 stdout 快照；按节流写状态文件。

        时间驱动：即使 du 卡住没有新输出，本方法仍会被轮询循环周期调用，
        心跳照常刷新（证明扫描进程活着）；写盘只按 interval_s 节流。
        """
        if self._closed or self._started_epoch is None:
            return
        data = bytes(accumulated)
        if len(data) > self._offset:
            chunk = self._pending + data[self._offset:]
            # 只消费到最后一个完整换行；残片留待下轮续拼。
            end = chunk.rfind(b"\n")
            if end >= 0:
                self._feed_lines(chunk[: end + 1])
                self._pending = chunk[end + 1:]
            else:
                self._pending = chunk
            self._offset = len(data)
        self._maybe_write()

    def finish(self, final: bytes = b"") -> None:
        """du 退出：喂入剩余输出（含无换行结尾的尾行）并写终值。

        ``final`` 是 du 的完整最终 stdout（与轮询期累计快照同源同口径）；
        内部只消费 ``self._offset`` 之后的未消费尾段，不重复计数。
        """
        if self._closed or self._started_epoch is None:
            return
        data = bytes(final)
        remainder = data[self._offset:] if len(data) > self._offset else b""
        tail = self._pending + remainder
        if tail.strip():
            self._feed_lines(tail)
        self._pending = b""
        self._counter.flush()
        self._last_write = None  # 终值必须落盘，绕过节流
        self._write()

    def close(self) -> None:
        """扫描结束（成功/中断/失败）：删除状态文件。幂等。"""
        self._closed = True
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    # ---- 内部 ----
    def _feed_lines(self, blob: bytes) -> None:
        """解析并消费一批以换行分隔的完整记录；坏行跳过（进度尽力而为，
        采集有效性仍由 _parse_du_stdout 的正式判定负责，不受此处影响）。"""
        for raw in blob.split(b"\n"):
            if not raw:
                continue
            size_raw, sep, path_raw = raw.partition(b"\t")
            if not sep or not path_raw:
                continue
            try:
                size_kb = int(size_raw)
                path = path_raw.decode("utf-8", errors="strict")
            except (ValueError, UnicodeDecodeError):
                continue
            self._counter.feed_record(path, size_kb)

    def _maybe_write(self) -> None:
        if self._last_write is None:
            self._write()
            return
        if self._clock() - self._last_write >= self._interval_s:
            self._write()

    def _write(self) -> None:
        assert self._started_epoch is not None
        now_epoch = time.time()
        self._last_write = self._clock()
        payload = {
            "schema": 1,
            "run_id": self._run_id,
            "phase": "du",
            "started_epoch_s": self._started_epoch,
            "dirs_scanned": self._counter.dirs_scanned,
            "bytes_seen_kb": self._counter.bytes_seen_kb,
            "elapsed_s": max(0.0, round(now_epoch - self._started_epoch, 3)),
            "heartbeat_epoch_s": now_epoch,
        }
        _atomic_write_json(self._path, payload)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """临时文件 + rename 原子写（与 config._atomic_write_text 同模式）。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)  # 进度含扫描根规模信息，与 settings.json 同级私密
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_progress(path: Path | None = None) -> dict | None:
    """读取状态文件原始内容；缺失/损坏返回 None（不抛错）。"""
    target = Path(path) if path is not None else progress_path()
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def live_progress(
    path: Path | None = None,
    *,
    now_epoch: float | None = None,
    stale_after_s: float = STALE_AFTER_S,
) -> dict | None:
    """读取方可判活的 live 进度视图（API /api/status 的 scan.live 数据源）。

    返回 None 的情形：文件不存在/损坏（旧运行根、扫描未运行）、或
    heartbeat 距读取时点超过 stale_after_s（扫描进程崩溃残留的 stale
    文件不冒充进行中进度）。新鲜时返回
    ``{active, run_id, dirs_scanned, bytes_seen_kb, started_epoch_s,
    elapsed_s, heartbeat_epoch_s, stale_after_s}``。
    """
    data = read_progress(path)
    if data is None:
        return None
    try:
        heartbeat = float(data["heartbeat_epoch_s"])
        dirs = int(data["dirs_scanned"])
        bytes_kb = int(data["bytes_seen_kb"])
        run_id = int(data["run_id"])
        started = float(data["started_epoch_s"])
        elapsed = float(data["elapsed_s"])
    except (KeyError, TypeError, ValueError):
        return None
    now = time.time() if now_epoch is None else now_epoch
    if now - heartbeat > stale_after_s:
        return None
    return {
        "active": True,
        "run_id": run_id,
        "dirs_scanned": dirs,
        "bytes_seen_kb": bytes_kb,
        "started_epoch_s": started,
        "elapsed_s": elapsed,
        "heartbeat_epoch_s": heartbeat,
        "stale_after_s": stale_after_s,
    }
