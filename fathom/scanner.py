"""扫描器：调用系统 du 生成目录快照并写入 SQLite。

设计要点（见 docs/DECISIONS.md DEC-002 / DEC-005）：
- 引擎用系统 `du -xk`（BSD du，macOS 自带）：输出天然是"目录 -> 累计大小"，
  无需自己遍历文件系统；-x 不跨挂载点，避免容量虚增。
- 只持久化 >= min_kb 的目录：1100 万文件的盘中目录数以十万计，
  全存会让数据库在 23GB 剩余空间下成为新的负担；小目录对"哪个文件夹
  冒出来了"这一问题没有回答价值。
- 同一天重复扫描会覆盖当天的旧快照，保证"一天一行"的趋势语义。
- 采集有效性（ISS-018，反例 AUD-01；合同修订 2026-09-13 保守收敛）：进入
  同日替换事务的只有两类采集——(a) 干净完整采集（退出码 0 且 stderr 无错误行），
  或 (b) 可证明仅权限受限的部分采集（根记录存在、大小非负、非信号终止，
  且 stderr 全量错误行均为权限类；兼容 BSD du 对 000 子目录的 exit=1 部分覆盖
  与空根 0）。信号终止（负退出码）、非权限/混合错误、退出码非零但无权限证据、
  负数/无效大小、歧义或不可解码路径等解析无效证据，不能因根记录存在而
  豁免：一律在进入事务前抛 InvalidScanError，当日旧
  snapshot/entries/volume_stats 原样保留，错误不吞。
  错误分类判据是 run_du 时点对 stderr 全量的逐行计数（存于 DuResult），
  stderr_tail 只是截尾显示，不得作为判据（会遗漏前部错误）；逐行只认
  errno 消息段（行内最后一个 ": " 之后）与权限文案的精确相等，出错
  路径文本含权限措辞不得冒充权限证据（R2 BLK-1），无法证明权限类的
  行保守计为非权限错误。
- 瞬时系统错误（ISS-047，生产实证 2026-09-13）：du stderr 中可证明为
  瞬时类的 errno 行（EINTR "Interrupted system call"、EAGAIN/EWOULDBLOCK
  "Resource temporarily unavailable"；launchd 定时扫描被信号打断目录读）
  不再判为致命无效采集：与权限类同口径单独计数
  （DuResult.transient_error_count，不与 denied_count 混同），仅瞬时或
  瞬时+权限受限且根记录有效时归 partial；瞬时与真实致命错误并存仍整体
  拒绝（ISS-018 口径不变）。诚实性约束：du 输出是累计大小，瞬时错误行
  虽指名出错路径，却无法证明任何子树（含其祖先）数据完整，故瞬时计数
  非零的采集只归 partial、永不 full；瞬时计数经 DuResult 返回值表达，
  不落快照 schema（持久化留后续卡，见任务卡 ISS-047 实施边界）。
- 扫描期间消失的目录（ISS-065，生产实证 2026-09-16 scan_run 3）：
  du 列出该目录时它存在（云同步缓存/临时/系统清理），但 8 小时后
  校验时已不在——这条记录既不是路径歧义、也不是非目录，也不是根外，
  本质上是测量期与校验期之间的竞态。把它与解析歧义混为一类会让约
  93.7 万行有效事实被 30 行干掉、整次采集被判 failed、当日快照丢弃。
  修法：在 _validate_du_paths 里把「根内 + os.path.isdir=False +
  os.path.exists=False」单独计为 vanished_count（DuResult 字段，进
  snapshots.vanished_count），不进 path_error_count；du 给出的 KB
  数是测量期事实，按现状保留进 entries；vanished 与 denied/transient
  并列为部分覆盖的一种，collection_status=partial，与 denied/transient
  一起如实呈现给日报与通知（不冒充完整覆盖）。根外路径、含换行/无法
  解析的行、仍存在但非目录（文件）的路径维持既有 fail-closed 语义。
- 采集质量的持久化（ISS-021）：schema v3 起，快照与 min_kb（入库阈值，
  数据集口径的一部分）和 collection_status（full/partial，来自
  classify_collection）一并落库；v3 之前的旧行两列为 NULL，不补造未知
  元数据。退出码、stderr 摘要等更细的质量细节仍只存在于当次 DuResult。
- du 安全时限的时钟（ISS-064，生产实证 2026-09-16）：deadline 以墙钟
  （time.time()）为主、monotonic 为第二轨，任一到期即超时——macOS 的
  time.monotonic() 基于 mach_absolute_time，系统睡眠期间不前进，单轨
  会让时限变成"清醒秒"，合盖即暂停计时、扫描无限挂起并持锁；墙钟被
  人为回拨时由 monotonic 轨兜底。超时报文附 du 阻塞位置的只读线索
  （最后输出路径或 lsof 当前目录，取不到则省略，线索采集本身至多
  约 2 秒），SIGTERM 3 秒后 SIGKILL 的既有回收语义不变。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import config

# stderr 权限类错误消息（BSD du errno 文案，英文、大小写敏感）。
# 分类判据是每行最后一个 ": " 之后的 errno 消息段与集合的精确相等，
# 不是整行子串匹配：出错路径的文本本身可能含权限措辞（如以报错文案
# 命名的目录），整行子串会把 ENOENT/ENAMETOOLONG 等非权限 errno 行
# 误判为权限证据（R2 BLK-1）。errno 消息由 du 的 strerror 生成、不含
# ": "，行内最后一个 ": " 恰是路径与消息的边界；消息段不是已知权限
# 文案的行一律保守计为非权限错误。
_PERMISSION_MESSAGES = frozenset({"Operation not permitted", "Permission denied"})

# stderr 瞬时系统错误消息（ISS-047）。EINTR 的 strerror 文案是
# "Interrupted system call"（macOS/BSD du 实证，launchd 定时扫描被信号
# 打断目录读），EAGAIN/EWOULDBLOCK 是 "Resource temporarily unavailable"；
# 两者都是可自行恢复的内核瞬时失败，重跑即可消除，不该让当日快照整体
# 丢失。分类沿用权限类口径：只认行内最后一个 ": " 之后 errno 消息段与
# 集合的精确相等，措辞相近（或出错路径文本含瞬时措辞）的行保守计为
# 非权限错误、维持整体拒绝。
_TRANSIENT_MESSAGES = frozenset(
    {"Interrupted system call", "Resource temporarily unavailable"}
)


def _errno_message_segment(line: str) -> str:
    """取 du stderr 错误行最后一个 ": " 之后的 errno 消息段（去尾部空白）。

    BSD du 错误行形态是 "du: <path>: <errno message>"，errno 消息不含
    ": "，故最后一个 ": " 总是路径与消息的边界——消息段不含路径文本，
    路径里逐字包含权限措辞也无法冒充权限证据。
    """
    return line.rstrip().rsplit(": ", 1)[-1]


class InvalidScanError(RuntimeError):
    """du 采集无效（缺根记录/空输出、信号终止、非权限且非瞬时的真实
    错误或其与权限/瞬时的混合、负数大小、退出码非零但无权限/瞬时证据），
    本次扫描已被整体拒绝。

    抛出时数据库没有任何写入，当日旧快照不受影响。
    """


class ScanInterruptedError(RuntimeError):
    """扫描被自己的调用方取消或超过 du 安全时限。"""


_DU_CONTEXT = threading.local()


@contextmanager
def du_process_context(
    *, inherited_fd: int, cancel_event: threading.Event,
    timeout_seconds: float | None = None,
):
    """只把本次扫描锁传给本任务创建的 du，并提供协作式取消。

    ``timeout_seconds=None``（默认值）时按 ``config.DU_TIMEOUT_S`` 取
    （ISS-061：FATHOM_DU_TIMEOUT_S，默认 14400s）。原 3600s 硬编码已
    删除——生产 /Users/maoking 一次扫描远超 1 小时，硬编码会无解释地
    截断并把当日快照丢失为 status=interrupted。调用方（scan_coordinator）
    总是显式传入，故此默认只影响把扫描器当库直接调用的场景。
    """
    effective = config.DU_TIMEOUT_S if timeout_seconds is None else timeout_seconds
    previous = getattr(_DU_CONTEXT, "value", None)
    _DU_CONTEXT.value = (inherited_fd, cancel_event, effective)
    try:
        yield
    finally:
        _DU_CONTEXT.value = previous


@dataclass(frozen=True)
class DuResult:
    """一次 du 采集的结构化结果：退出码、质量线索与真实耗时。

    sizes          目录路径 -> 累计大小 KB（含根记录）
    exit_code      du 进程退出码（负值=信号终止；部分权限失败也是 1）
    denied_count   stderr 全量中权限/读取失败行数
    elapsed_seconds 本次采集的实测耗时（time.monotonic 口径）
    stderr_tail    stderr 末尾若干行，仅用于失败诊断显示，不持久化；
                   截尾会遗漏前部错误，不得作为错误判据
    other_error_count  stderr 全量中非权限且非瞬时的错误行数（错误分类
                       判据，ISS-018 合同修订新增；默认 0 兼容既有构造方）
    other_error_sample 首条非权限且非瞬时的错误行，仅诊断用
    transient_error_count  stderr 全量中瞬时系统错误行数（ISS-047；
                       EINTR/EAGAIN 等 errno 消息段精确匹配瞬时文案），
                       单独计数、不与 denied_count 混同；非零时采集只归
                       partial（不持久化，经本返回值表达）
    transient_error_sample 首条瞬时错误行，仅诊断用
    path_error_count   stdout 中无法无歧义解析的路径记录数；非零时采集无效
    path_error_sample  首条路径解析错误，仅诊断用
    vanished_count     根内但校验时已不在的目录数（ISS-065：扫描期间被
                       系统清理的缓存/临时目录）；不进 path_error_count，
                       与 denied/transient 并列为部分覆盖的一种；du 给
                       出的 KB 数是测量期事实，按现状保留进 entries
    vanished_sample    首条消失的目录路径，仅诊断用
    """

    sizes: dict[str, int]
    exit_code: int
    denied_count: int
    elapsed_seconds: float
    stderr_tail: tuple[str, ...] = ()
    other_error_count: int = 0
    other_error_sample: str = ""
    transient_error_count: int = 0
    transient_error_sample: str = ""
    path_error_count: int = 0
    path_error_sample: str = ""
    vanished_count: int = 0
    vanished_sample: str = ""

    def stderr_hint(self) -> str:
        return self.stderr_tail[-1] if self.stderr_tail else ""


def _as_bytes(stream: bytes | str) -> bytes:
    """规范 subprocess 输出类型；str 分支只兼容既有测试注入。"""
    return stream if isinstance(stream, bytes) else stream.encode("utf-8")


def _parse_du_stdout(stdout: bytes | str) -> tuple[dict[str, int], int, str]:
    """解析 BSD ``du -xk`` 的原始字节输出。

    macOS 15 的 BSD du 会逐字节输出路径，并不会把反斜杠、tab 或 UTF-8
    字符做 shell/C 风格转义。因此路径必须原样严格解码，不能猜测反转义。
    首个 tab 是大小与路径的分隔符，后续 tab 属于合法文件名；换行同时是
    du 唯一的记录分隔符，文件名中的换行无法无歧义表达，会产生不完整的
    物理行并记为 path_error，交给采集质量判定整体拒绝。
    """
    sizes: dict[str, int] = {}
    error_count = 0
    error_sample = ""

    def reject(record: bytes, reason: str) -> None:
        nonlocal error_count, error_sample
        error_count += 1
        if not error_sample:
            error_sample = f"{reason}: {record[:160]!r}"

    raw = _as_bytes(stdout)
    records = raw.split(b"\n")
    if records and records[-1] == b"":
        records.pop()
    for record in records:
        size_raw, sep, path_raw = record.partition(b"\t")
        if not sep or not path_raw:
            reject(record, "记录缺少大小/路径分隔")
            continue
        try:
            size_kb = int(size_raw)
        except ValueError:
            reject(record, "大小不是整数")
            continue
        try:
            path = path_raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            reject(record, "路径不是有效 UTF-8")
            continue
        if path in sizes:
            reject(record, "路径记录重复")
            continue
        sizes[path] = size_kb
    return sizes, error_count, error_sample


def _validate_du_paths(
    sizes: dict[str, int], root: Path
) -> tuple[int, int, str, str]:
    """对解析结果做三类分流：fail-closed / 接受 / vanished。

    这层校验使含换行的恶意/巧合路径即使后半段长得像另一条合法 du 记录，
    也不能静默注入根外路径或文件路径。扫描期间恰好消失的目录
    （du 列到它时存在、校验时已被系统清理；生产实证 2026-09-16 scan_run 3
    的云同步缓存）不再与解析歧义混为一类——它们是测量期与校验期之间的
    竞态事实，du 给出的 KB 数属于测量期事实，按现状保留进快照元数据
    （entries 与 snapshots.vanished_count）；仅在覆盖语义上如实标注
    部分覆盖（partial），不冒充完整覆盖也不夸大。

    返回 (path_error_count, vanished_count, path_error_sample, vanished_sample)：
    - path_error_count/path_error_sample  须整体拒绝的根外/非目录路径，
                                          任何一条都使采集无效（fail-closed）；
    - vanished_count/vanished_sample     根内 + 校验时不在的目录数与首条样本，
                                          不使采集无效，仅参与 partial 标注。
    """
    root_str = str(root)
    root_prefix = root_str.rstrip("/")
    if not root_prefix:
        root_prefix = "/"
    error_count = 0
    error_sample = ""
    vanished_count = 0
    vanished_sample = ""
    for path in sizes:
        within_root = (
            path.startswith("/") if root_prefix == "/"
            else path == root_prefix or path.startswith(root_prefix + "/")
        )
        if not within_root:
            reason = "解析路径越出扫描根"
        elif os.path.isdir(path):
            continue
        elif os.path.exists(path):
            reason = "解析路径无法确认为目录"
        else:
            # 根内 + du 当时存在 + 校验时已被系统清理：
            # vanished 是测量期与校验期之间的竞态事实，du 的 KB 数保留。
            vanished_count += 1
            if not vanished_sample:
                vanished_sample = path
            continue
        error_count += 1
        if not error_sample:
            error_sample = f"{reason}: {path!r}"
    return error_count, vanished_count, error_sample, vanished_sample


def _du_argv(root: str) -> list[str]:
    """构造 du argv：基线 + 当前配置层生效的 -I <每项>。

    ISS-066：``du -I mask`` 按名字匹配并跳过整棵子树（PM 已实测）。
    掩码语义按 fnmatch（与 BSD du 实现一致）：``*.noindex`` / ``skip.noindex``
    都能匹配；配置层已做排序去重（canonical form），此处仅按序展开。
    无配置时（EXCLUDE_NAMES 为空）argv 与现状逐项相同——零行为变化证明。
    """
    argv: list[str] = ["/usr/bin/du", "-xk"]
    for mask in config.EXCLUDE_NAMES:
        argv.extend(["-I", mask])
    argv.append(root)
    return argv


def _last_du_output_path(partial_output: bytes) -> str:
    """从 communicate 轮询累计的部分 stdout 取最后一条完整 du 记录的路径。

    只取最后一个换行之前最近的一条完整记录（"大小\\t路径"）；尾部没有
    换行的残余字节是 du 写到一半的碎片，不能当作路径线索。这是超时报文
    的阻塞位置线索之一，解析失败只意味着线索不可得，不影响超时本身。
    """
    if not partial_output:
        return ""
    end = partial_output.rfind(b"\n")
    if end <= 0:
        return ""
    start = partial_output.rfind(b"\n", 0, end)
    record = partial_output[start + 1:end]
    _size, sep, path_raw = record.partition(b"\t")
    if not sep or not path_raw:
        return ""
    return path_raw.decode("utf-8", errors="replace").strip()


def _count_du_records(partial_output: bytes) -> int:
    """数部分 stdout 里的**完整** du 记录条数（ISS-070 进度线索）。

    只数以换行结尾的完整记录（"大小\\t路径"）；尾部没有换行的残余字节是
    du 写到一半的碎片，不计入。0 是合法取值（超时时一条都没产出），
    用来区分"一直在推进但量大跑不完"与"卡住几乎不推进"两种故障。
    """
    if not partial_output:
        return 0
    complete = partial_output.rfind(b"\n")
    if complete < 0:
        return 0
    return partial_output[: complete + 1].count(b"\n")


def _lsof_du_cwd(pid: int) -> str:
    """只读查询 du（PID）当前所在目录：lsof -p 输出的 cwd 行。

    ISS-064 生产证据链即用此法定位到 du 阻塞在 WPS 容器内的目录。
    任何失败（无 lsof、权限、超时）都返回空串并让报文省略该线索；
    timeout=2 保证线索采集不会明显延长超时回收路径的阻塞。
    """
    try:
        completed = subprocess.run(
            ["/usr/sbin/lsof", "-p", str(pid)],
            capture_output=True, timeout=2,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        # BSD lsof 列：COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME；
        # NAME 是最后一列，路径本身可含空格，故按前 8 个空白字段切分。
        fields = line.split(None, 8)
        if len(fields) == 9 and fields[3] == "cwd":
            return fields[8].strip()
    return ""


def _timeout_message(
    timeout_seconds: float, proc: "subprocess.Popen[bytes]", partial_output: bytes
) -> str:
    """构造 du 超时报文：基线文案 + 阻塞位置线索（可得时）。

    线索优先级：du 最后一条输出记录的路径（更精确），否则 lsof 只读
    查询的当前目录；两者都取不到则保持基线文案。lsof 须在 du 仍存活
    时调用，故本函数只在 raise 之前、回收之前调用一次。

    ISS-070：报文同时携带**进度条数**（超时时 du 已产出的完整记录数）。
    生产实证（2026-09-18 run 5）暴露旧报文只有"最后路径"、无法区分两种
    处置完全不同的故障——一直在推进只是量大跑不完 vs 卡在某个目录几乎
    不推进。进度条数是纯只读线索、不影响超时本身。
    """
    message = f"du 超过 {timeout_seconds:g} 秒安全时限"
    records = _count_du_records(partial_output)
    message = f"{message}；已产出 {records} 条记录"
    last_path = _last_du_output_path(partial_output)
    if last_path:
        return f"{message}；du 最后输出路径：{last_path}"
    cwd = _lsof_du_cwd(proc.pid)
    if cwd:
        return f"{message}；du 当前目录（lsof）：{cwd}"
    return message


def run_du(root: Path) -> DuResult:
    """执行 du -xk，返回结构化采集结果（大小表、退出码、质量线索、真实耗时）。

    错误分类在采集时点对 stderr 全量逐行进行（权限类 / 瞬时类 / 其他真实
    错误）并固化到 DuResult——每行只认最后一个 ": " 之后 errno 消息段与
    各类文案集合的精确相等，路径文本不参与判据；有效性判据后续只读这些
    全量计数，不重新看 stderr_tail 截尾。
    """
    started = time.monotonic()
    inherited_fd = None
    cancel_event = None
    context = getattr(_DU_CONTEXT, "value", None)
    if context is not None:
        inherited_fd, cancel_event, timeout_seconds = context
    if context is None:
        # 保留扫描器作为库被直接调用时的原合同；产品入口全部经协调器进入下支。
        completed = subprocess.run(
            _du_argv(str(root)), capture_output=True
        )
        stdout, stderr_raw, returncode = (
            completed.stdout, completed.stderr, completed.returncode
        )
    else:
        proc = subprocess.Popen(
            _du_argv(str(root)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(inherited_fd,),
        )
        # ISS-064：deadline 以墙钟为主（time.time() 睡眠期间照常前进），
        # monotonic 作第二轨——macOS 的 time.monotonic() 基于
        # mach_absolute_time，系统睡眠期间不前进，单轨 monotonic 会把
        # "N 秒安全时限"变成"N 清醒秒"，合盖即暂停计时；墙钟被人为回拨
        # 时则由 monotonic 轨兜底。两轨任一到期即超时。
        mono_deadline = started + timeout_seconds
        wall_deadline = time.time() + timeout_seconds
        partial_output = b""
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise ScanInterruptedError("扫描已取消")
                mono_remaining = mono_deadline - time.monotonic()
                wall_remaining = wall_deadline - time.time()
                if mono_remaining <= 0 or wall_remaining <= 0:
                    raise ScanInterruptedError(
                        _timeout_message(timeout_seconds, proc, partial_output)
                    )
                try:
                    stdout, stderr_raw = proc.communicate(
                        timeout=min(0.2, min(mono_remaining, wall_remaining))
                    )
                    break
                except subprocess.TimeoutExpired as exc:
                    # communicate 轮询超时时已读输出经 exc.output 携带
                    # （累计口径：Popen 内部输出缓冲跨重试保留），留作
                    # 超时报文的阻塞位置线索。
                    partial_output = exc.output or b""
                    continue
        except BaseException:
            # start_new_session=True 使 pgid 只属于本任务创建的 du；绝不按外部 PID 杀进程。
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=3)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
            raise
        returncode = proc.returncode
    elapsed = time.monotonic() - started
    sizes, path_error_count, path_error_sample = _parse_du_stdout(stdout)
    (
        invalid_path_count, vanished_count,
        invalid_path_sample, vanished_sample,
    ) = _validate_du_paths(sizes, root)
    if invalid_path_count:
        path_error_count += invalid_path_count
        if not path_error_sample:
            path_error_sample = invalid_path_sample
    stderr = _as_bytes(stderr_raw).decode("utf-8", errors="replace")
    denied = 0
    transient = 0
    transient_sample = ""
    other_error_count = 0
    other_error_sample = ""
    for line in stderr.splitlines():
        if not line.strip():
            continue
        segment = _errno_message_segment(line)
        if segment in _PERMISSION_MESSAGES:
            denied += 1
        elif segment in _TRANSIENT_MESSAGES:
            transient += 1
            if not transient_sample:
                transient_sample = line
        else:
            other_error_count += 1
            if not other_error_sample:
                other_error_sample = line
    return DuResult(
        sizes=sizes,
        exit_code=returncode,
        denied_count=denied,
        elapsed_seconds=elapsed,
        stderr_tail=tuple(stderr.splitlines()[-4:]),
        other_error_count=other_error_count,
        other_error_sample=other_error_sample,
        transient_error_count=transient,
        transient_error_sample=transient_sample,
        path_error_count=path_error_count,
        path_error_sample=path_error_sample,
        vanished_count=vanished_count,
        vanished_sample=vanished_sample,
    )


def classify_collection(result: DuResult, root_str: str) -> str:
    """判定一次采集是否可用于建快照；返回 'full' 或 'partial'。

    ISS-018 修复合同（保守收敛）：进入同日替换事务的只有两类采集——
    1. full：干净完整采集（退出码 0 且无任何错误行）；
    2. partial：根记录存在、大小非负、非信号终止，且 stderr 全量错误行均为
       权限类或瞬时系统错误类（denied_count/transient_error_count 单独
       计数、other_error_count=0）。权限类兼容 BSD du 对 000 子目录 exit=1
       的部分覆盖与空根 0；瞬时类（ISS-047：EINTR/EAGAIN，launchd 定时
       扫描被信号打断目录读的生产实证）不再整体拒绝，当日快照不因单次
       瞬时内核失败丢失。
    以下情形不能因根记录存在而豁免，一律抛 InvalidScanError：路径输出歧义、
    解码失败或越界，根记录缺失，任意负数大小（含根），信号终止（负退出码），
    非权限且非瞬时的真实错误（或其与权限/瞬时的混合），退出码非零但无
    任何权限/瞬时证据。错误分类取自 run_du 时点的结构化计数，不使用截尾
    stderr_tail。

    瞬时错误下的诚实归类（ISS-047）：du 输出是累计大小，瞬时错误行虽指名
    出错路径，却无法证明任何子树（含出错路径的所有祖先）数据完整——被
    中断子树之上的累计值同样可能偏低。因此瞬时计数非零的采集一律只归
    partial、永不 full，不声称任何子树数据完整；瞬时缺口数量经
    DuResult.transient_error_count 返回值表达，不与权限缺口
    （denied_count）混同，也不落快照 schema。

    扫描期间消失（ISS-065）：du 列到时存在、校验时不在的目录（云同步
    缓存/临时被系统清理）单独计 vanished_count，与 denied/transient 并列
    为部分覆盖的一种并归 partial。vanished 行的 KB 数是测量期事实，
    按现状保留进 entries 与 snapshots.vanished_count——既不删也不改
    du 的原始数值；vanished_count 经 DuResult 返回值表达，再由
    create_snapshot 透传到 snapshots 表。
    """
    if root_str not in result.sizes:
        hint = f"；du stderr：{result.stderr_hint()}" if result.stderr_hint() else ""
        raise InvalidScanError(
            f"du 采集无效：未返回根目录记录（退出码 {result.exit_code}）{hint}。"
            "已拒绝本次写入，当日旧快照保持不变"
        )
    root_kb = result.sizes[root_str]
    if root_kb < 0:
        raise InvalidScanError(
            f"du 采集无效：根目录大小为负数（{root_kb} KiB）。"
            "已拒绝本次写入，当日旧快照保持不变"
        )
    negative = next((p for p, s in result.sizes.items() if s < 0), None)
    if negative is not None:
        raise InvalidScanError(
            f"du 采集无效：存在负数大小记录（{negative} = {result.sizes[negative]} KiB）。"
            "已拒绝本次写入，当日旧快照保持不变"
        )
    if result.exit_code < 0:
        raise InvalidScanError(
            f"du 采集无效：du 被信号终止（退出码 {result.exit_code}），"
            "根记录存在不能豁免。已拒绝本次写入，当日旧快照保持不变"
        )
    if result.other_error_count > 0:
        sample = f"（如 {result.other_error_sample!r}）" if result.other_error_sample else ""
        raise InvalidScanError(
            f"du 采集无效：stderr 含非权限错误 {result.other_error_count} 行{sample}"
            "（瞬时错误已单独归类，不计入此处），"
            "只有可证明仅权限或瞬时受限的部分采集才被接受。"
            "已拒绝本次写入，当日旧快照保持不变"
        )
    if result.path_error_count > 0:
        sample = f"（如 {result.path_error_sample}）" if result.path_error_sample else ""
        raise InvalidScanError(
            f"du 采集无效：stdout 含不可无歧义解析的路径记录 "
            f"{result.path_error_count} 行{sample}。"
            "已拒绝本次写入，当日旧快照保持不变"
        )
    if (
        result.exit_code != 0
        and result.denied_count == 0
        and result.transient_error_count == 0
    ):
        raise InvalidScanError(
            f"du 采集无效：退出码 {result.exit_code} 但无权限受限证据，"
            "也无瞬时错误证据，无法证明仅权限或瞬时限制。"
            "已拒绝本次写入，当日旧快照保持不变"
        )
    if (
        result.exit_code != 0
        or result.denied_count > 0
        or result.transient_error_count > 0
        or result.vanished_count > 0
    ):
        return "partial"
    return "full"


def _volume_stat(root: Path) -> tuple[int, int]:
    """返回根路径所在卷的 (总字节, 剩余字节)。"""
    st = os.statvfs(root)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def _drop_same_day(
    conn: sqlite3.Connection, day: str, root: str, min_kb: int,
    exclude_names: str = "",
) -> None:
    """删除同数据集（同根同阈值同排除掩码口径）同一天的旧快照，实现"一天一行"。

    阈值口径不同的快照属于另一数据集，同日不替换（ISS-021）；更换根同理。
    排除掩码口径不同的快照同样属于另一数据集（ISS-066）。
    """
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM snapshots WHERE root = ? AND min_kb = ? "
            "AND exclude_names IS ? AND created_at LIKE ?",
            (root, min_kb, exclude_names, f"{day}%"),
        )
    ]
    for sid in ids:
        conn.execute("DELETE FROM entries WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM volume_stats WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM snapshots WHERE id = ?", (sid,))


def create_snapshot(
    conn: sqlite3.Connection,
    root: Path | None = None,
    min_kb: int | None = None,
) -> int:
    """执行一次完整扫描并写入快照，返回快照 id。

    min_kb 可在测试中注入小值；生产使用 config.MIN_DIR_KB。快照与
    min_kb、采集质量（full/partial）一并持久化——数据集身份是
    (root, min_kb)，差分/保留/同日替换都以它分组（ISS-021）。

    采集无效（歧义/不可解码路径、缺根记录/空输出、信号终止、非权限且
    非瞬时的真实错误或其与权限/瞬时的混合、负数大小、退出码非零但无
    权限/瞬时证据）时抛 InvalidScanError，数据库不做任何写入，
    当日旧快照原样保留；可证明仅权限或瞬时受限且根记录有效时按部分
    覆盖落库（denied_count 记录权限缺口数量；瞬时缺口不落 schema，经
    DuResult 返回值表达，ISS-047）。du_seconds 记录本次采集实测耗时。

    扫描期间消失的目录（ISS-065）记 vanished_count：du 列出时存在、
    校验时不在的目录数；不进 path_error_count，du 给出的 KB 数保留进
    entries（测量期事实），collection_status=partial，与 denied/transient
    并列为部分覆盖的一种并如实呈现给日报与通知。
    """
    root = Path(root) if root else config.DEFAULT_ROOT
    min_kb = config.MIN_DIR_KB if min_kb is None else min_kb
    root_str = str(root)

    result = run_du(root)
    collection_status = classify_collection(result, root_str)  # 无效采集在此被拒绝
    sizes = result.sizes
    total_kb = sizes[root_str]
    kept = [(p, s) for p, s in sizes.items() if s >= min_kb]

    now = dt.datetime.now()
    exclude_names = ";".join(config.EXCLUDE_NAMES)  # 规范串：已排序去重
    with conn:
        _drop_same_day(conn, now.strftime("%Y-%m-%d"), root_str, min_kb,
                       exclude_names)
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, total_kb, "
            "min_kb, collection_status, vanished_count, exclude_names) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now.isoformat(timespec="seconds"), root_str, len(sizes),
             result.denied_count, result.elapsed_seconds, total_kb,
             min_kb, collection_status, result.vanished_count, exclude_names),
        )
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
            [(sid, p, s) for p, s in kept],
        )
        total_b, free_b = _volume_stat(root)
        conn.execute(
            "INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) VALUES (?,?,?)",
            (sid, total_b, free_b),
        )
    assert sid is not None
    return sid


def prune_snapshots(
    conn: sqlite3.Connection,
    keep_daily_days: int | None = None,
    keep_weekly_weeks: int | None = None,
) -> int:
    """清理旧快照：每个数据集（同根同口径）近 N 天全保留，更早的每周
    保留最早一份，最多 M 周（weekly_cutoff 从今天向前 M 周，非 N 天加 M 周）。

    周分组按数据集独立进行：两个根（或同根不同阈值口径）在同一 ISO 周的
    历史各自保留一份，不会互相挤掉（ISS-021，AUD-05）。旧记录 min_kb 为
    NULL，与已知阈值一样按 (root, min_kb) 分组，NULL 只与 NULL 同组。

    返回删除的快照数。
    """
    keep_daily_days = config.KEEP_DAILY_DAYS if keep_daily_days is None else keep_daily_days
    keep_weekly_weeks = config.KEEP_WEEKLY_WEEKS if keep_weekly_weeks is None else keep_weekly_weeks

    today = dt.date.today()
    daily_cutoff = (today - dt.timedelta(days=keep_daily_days)).isoformat()
    weekly_cutoff = today - dt.timedelta(weeks=keep_weekly_weeks)

    rows = conn.execute(
        "SELECT id, created_at, root, min_kb, exclude_names FROM snapshots "
        "ORDER BY created_at, id"
    ).fetchall()

    # 每个数据集的每个 ISO 周保留最早一个快照（仅对超过每日保留期的部分）；
    # 用 id 锚定而非集合标记，同时间戳的两条也能正确只留一条。
    # 数据集身份从 (root, min_kb) 升级为 (root, min_kb, exclude_names)（ISS-066）。
    daily_cutoff_date = dt.date.fromisoformat(daily_cutoff)
    weekly_keep_id: dict[tuple, int] = {}
    for r in rows:
        created = dt.date.fromisoformat(r["created_at"][:10])
        excludes = r["exclude_names"] if "exclude_names" in r.keys() else ""
        if daily_cutoff_date > created >= weekly_cutoff:
            key = (r["root"], r["min_kb"], excludes, created.isocalendar()[:2])
            if key not in weekly_keep_id:
                weekly_keep_id[key] = r["id"]

    to_delete: list[int] = []
    for r in rows:
        created = dt.date.fromisoformat(r["created_at"][:10])
        if created >= daily_cutoff_date:
            continue  # 近 N 天全保留
        excludes = r["exclude_names"] if "exclude_names" in r.keys() else ""
        key = (r["root"], r["min_kb"], excludes, created.isocalendar()[:2])
        if created < weekly_cutoff:
            to_delete.append(r["id"])  # 超过每周保留期
        elif r["id"] != weekly_keep_id.get(key):
            to_delete.append(r["id"])  # 该数据集每周非首个快照
    for sid in to_delete:
        conn.execute("DELETE FROM entries WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM volume_stats WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM snapshots WHERE id = ?", (sid,))
    conn.commit()
    return len(to_delete)
