#!/usr/bin/env python3
"""ISS-029 合同原型 helper（纯 stdlib，冻结前置验证用）。

这不是生产代码：生产 helper 仍是 fathom/ 的 FastAPI 服务。本文件只用于在
当前 arm64 宿主、零第三方依赖安装的前提下，把发行 helper 必须满足的
「身份 / 版本 / health / 回环端口 / 退出」合同语义做成可执行、可断言的
原型，供 scripts/build_helper_experiment.sh 逐条验证。生产接入时把同一
合同套到 fathom.api 上（见 experiments/iss029/findings.md 的缺口清单）。

合同摘要：

- 身份/版本：``--version`` 输出单行 JSON（service/protocol_version/
  helper_version/python/machine/exe），退出码 0。这是 app 壳在启动 helper
  前后唯一依赖的静态身份面。
- 回环端口：默认端口 7963，``--port-range N`` 依序尝试 N 个候选端口；
  只绑定 127.0.0.1，绝不绑定 0.0.0.0。
- 未知占用不杀：候选端口被「非本服务」进程占用时只记录并让位到下一端口；
  全部被占时退出码 3，不向占用进程发任何信号。
- 单一所有者：同一数据根由长期持有的 ``fcntl.flock`` 串行化检查、绑定与
  服务生命周期；第二个实例退出码 4，绝不并行第二份。
- health：GET /health 返回 200 JSON（含 service/protocol_version/
  helper_version/pid/port/uptime_s）；Host 头不是本机 loopback 别名时 403
  （与 fathom/api.py 的本地边界合同同构）。
- 受控停机：POST /shutdown 必须带 discovery 文件里的随机 control token；
  令牌错误 403。SIGTERM/SIGINT 优雅退出码 0，并删除 discovery 文件。
- 崩溃（SIGKILL）由内核释放锁并留下 stale discovery；下一次锁持有者
  接管。优雅退出只在 pid+instance_id+token 仍匹配时删除自身记录。
- 退出码合同：0 优雅停止；2 CLI 用法错误（argparse）；3 候选端口全部
  被未知进程占用；4 同服务健康实例已在运行（单一所有者）；70 内部错误；
  被信号杀死时 shell 报 128+signum（如 SIGKILL=137）。
- 数据只写 ``--data-dir`` 指定的显式目录（实验用合成临时根）；除该目录
  与 stderr 外零写入，不写自身所在目录。

Python 3.10+ 兼容（与 requirements-runtime.txt 的发行下限一致）。
"""

from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 实验身份常量：独立于生产 com.maoscripts.fathom-*，不触碰生产 launchd/端口。
SERVICE_ID = "dev.fathom.helper-experiment.iss029"
PROTOCOL_VERSION = 1
HELPER_VERSION = "0.0.1-iss029"

# 实验默认端口：避开生产 7952；发行默认端口策略见 findings.md §6。
DEFAULT_PORT = 7963
DEFAULT_PORT_RANGE = 5          # 候选端口 7963..7967
DISCOVERY_FILENAME = "helper-instance.json"
LOCK_FILENAME = "helper-instance.lock"

# 退出码合同（结构化退出）
EXIT_OK = 0
EXIT_USAGE = 2                  # argparse 默认
EXIT_PORTS_EXHAUSTED = 3        # 候选端口全部被未知进程占用
EXIT_SINGLE_OWNER = 4           # 同服务健康实例已在运行
EXIT_INTERNAL = 70              # EX_SOFTWARE

HEALTH_TIMEOUT_S = 1.0
TOKEN_HEADER = "X-Fathom-Helper-Token"


def _emit(obj: dict) -> None:
    """单行 JSON 输出（--version / 诊断信息统一格式）。"""
    print(json.dumps(obj, ensure_ascii=False))


def cmd_version(_: argparse.Namespace) -> int:
    _emit({
        "service": SERVICE_ID,
        "protocol_version": PROTOCOL_VERSION,
        "helper_version": HELPER_VERSION,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "exe": sys.executable,
    })
    return EXIT_OK


def probe_health(port: int):
    """探测 127.0.0.1:port 的 /health。

    返回 (kind, info)：
    - ("ours", info_dict)      本服务同协议版本的健康实例
    - ("mismatch", raw)        有响应但身份/协议不符（含本服务旧协议）
    - ("refused", None)        端口无 HTTP 服务（连接被拒/超时/非 HTTP）
    """
    url = "http://127.0.0.1:%d/health" % port
    req = urllib.request.Request(url, headers={"Host": "127.0.0.1:%d" % port})
    try:
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_S) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return "mismatch", "http-%d" % exc.code
    except Exception:  # noqa: BLE001 - 连接被拒/超时/协议错误一律未知
        return "refused", None
    try:
        info = json.loads(body)
    except ValueError:
        return "mismatch", "non-json"
    if (isinstance(info, dict) and info.get("service") == SERVICE_ID
            and info.get("protocol_version") == PROTOCOL_VERSION
            and info.get("status") == "ok"):
        return "ours", info
    return "mismatch", info if isinstance(info, dict) else "non-dict"


def occupier_pids(port: int):
    """只读查询端口占用进程 pid（lsof 缺失/失败返回空表，绝不杀进程）。"""
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class HelperState:
    """serve 进程共享状态；handler 线程只读，discovery 由主线程维护。"""

    def __init__(self, port: int, data_dir: Path):
        self.port = port
        self.data_dir = data_dir
        self.control_token = uuid.uuid4().hex
        self.started_monotonic = time.monotonic()
        self.instance_id = uuid.uuid4().hex[:12]
        self.discovery_path = data_dir / DISCOVERY_FILENAME


class OwnershipLock:
    """由 serve 全生命周期持有的进程锁，关闭 discovery 检查/绑定竞争。"""

    def __init__(self, fd: int, path: Path):
        self.fd = fd
        self.path = path

    @classmethod
    def acquire(cls, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = data_dir / LOCK_FILENAME
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return cls(fd, path)

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = -1


def write_discovery(state: HelperState) -> None:
    """以 0600 原子写 discovery，且不把控制令牌写到 stdout/stderr。"""
    record = {
        "service": SERVICE_ID,
        "protocol_version": PROTOCOL_VERSION,
        "helper_version": HELPER_VERSION,
        "instance_id": state.instance_id,
        "pid": os.getpid(),
        "port": state.port,
        "control_token": state.control_token,
        "started_at": time.time(),
    }
    state.data_dir.mkdir(parents=True, exist_ok=True)
    tmp = state.data_dir / (".%s.%d.%s.tmp" % (
        DISCOVERY_FILENAME, os.getpid(), uuid.uuid4().hex))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, state.discovery_path)
        os.chmod(state.discovery_path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def remove_discovery(state: HelperState) -> None:
    """只删除仍属于当前实例的 discovery，避免退出时删掉接管者记录。"""
    try:
        record = json.loads(state.discovery_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return
    if not isinstance(record, dict):
        return
    expected = {
        "pid": os.getpid(),
        "instance_id": state.instance_id,
        "control_token": state.control_token,
    }
    if all(record.get(key) == value for key, value in expected.items()):
        try:
            state.discovery_path.unlink()
        except FileNotFoundError:
            pass


def make_handler(state: HelperState, server_ref: dict):
    """构造绑定到 state 的 HTTP handler；server_ref 由调用方在 bind 后注入。"""

    loopback_hosts = frozenset({
        "127.0.0.1:%d" % state.port, "localhost:%d" % state.port,
        "[::1]:%d" % state.port,
    })

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "").lower() in loopback_hosts

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
            if not self._host_ok():
                self._json(403, {"detail": "已拒绝：Host 不是本机服务地址"})
                return
            if self.path == "/health":
                self._json(200, {
                    "status": "ok",
                    "service": SERVICE_ID,
                    "protocol_version": PROTOCOL_VERSION,
                    "helper_version": HELPER_VERSION,
                    "instance_id": state.instance_id,
                    "pid": os.getpid(),
                    "port": state.port,
                    "uptime_s": round(time.monotonic() - state.started_monotonic, 3),
                })
                return
            self._json(404, {"detail": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                self._json(403, {"detail": "已拒绝：Host 不是本机服务地址"})
                return
            if self.path != "/shutdown":
                self._json(404, {"detail": "not found"})
                return
            token = self.headers.get(TOKEN_HEADER, "")
            if not token or not hmac.compare_digest(token, state.control_token):
                self._json(403, {"detail": "已拒绝：停机令牌错误"})
                return
            self._json(200, {"ok": True, "bye": state.instance_id})
            # 先让响应落地，再触发优雅停机（shutdown 须在其他线程调用）
            threading.Timer(0.1, _request_stop, args=(server_ref["server"],)).start()

        def log_message(self, fmt, *args):  # 日志只进 stderr，不写文件
            sys.stderr.write("[helper] %s\n" % (fmt % args))

    return Handler


def _request_stop(server) -> None:
    threading.Thread(target=server.shutdown, daemon=True).start()


def check_stale_ownership(data_dir: Path) -> int | None:
    """启动前的 stale discovery 检查。

    本函数只在进程持有长期锁时调用：记录属于仍健康的旧版无锁实例时返回
    EXIT_SINGLE_OWNER；pid 已死或记录损坏则安全接管。
    """
    stale = data_dir / DISCOVERY_FILENAME
    if not stale.exists():
        return None
    record = None
    try:
        record = json.loads(stale.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("discovery root is not an object")
        old_pid = int(record.get("pid", 0))
        old_port = int(record.get("port", 0))
        live = (record.get("service") == SERVICE_ID and old_pid > 0
                and pid_alive(old_pid) and old_port > 0
                and probe_health(old_port)[0] == "ours")
    except Exception:  # noqa: BLE001 - 坏记录视作崩溃残留
        live = False
    if live and isinstance(record, dict):
        # discovery 含控制令牌；只输出公开身份字段。
        public = {key: record.get(key) for key in (
            "service", "protocol_version", "helper_version", "instance_id",
            "pid", "port")}
        _emit({"event": "single-owner-stale-check", "already_running": public})
        return EXIT_SINGLE_OWNER
    print("[helper] 接管无活动锁的 stale/malformed discovery", file=sys.stderr)
    return None


def cmd_serve(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).expanduser()
    if data_dir.exists() and not data_dir.is_dir():
        _emit({"error": "data-dir 不是目录", "path": str(data_dir)})
        return EXIT_INTERNAL

    if args.start_gate:
        deadline = time.monotonic() + 10
        gate = Path(args.start_gate)
        while not gate.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not gate.exists():
            _emit({"error": "实验并发起跑门未打开", "exit": EXIT_INTERNAL})
            return EXIT_INTERNAL

    try:
        owner_lock = OwnershipLock.acquire(data_dir)
    except OSError as exc:
        _emit({"error": "ownership lock 创建失败", "errno": exc.errno})
        return EXIT_INTERNAL
    if owner_lock is None:
        _emit({"event": "single-owner-lock-held", "already_running": True})
        return EXIT_SINGLE_OWNER

    try:
        return _serve_with_lock(args, data_dir)
    except Exception as exc:  # noqa: BLE001 - 原型也必须结构化 fail closed
        print(json.dumps({"error": "helper internal error",
                          "type": type(exc).__name__, "exit": EXIT_INTERNAL},
                         ensure_ascii=False), file=sys.stderr)
        return EXIT_INTERNAL
    finally:
        owner_lock.close()


def _serve_with_lock(args: argparse.Namespace, data_dir: Path) -> int:
    """在长期 flock 持有期间完成检查、绑定、服务和清理。"""
    verdict = check_stale_ownership(data_dir)
    if verdict is not None:
        return verdict

    blocked = []
    server = None
    state = None
    server_ref = {}
    for port in range(args.port, args.port + max(1, args.port_range)):
        kind, info = probe_health(port)
        if kind == "ours":
            # 单一所有者：健康同服务实例在跑，绝不并行第二份、不换端口绕行
            _emit({"event": "single-owner", "already_running": info})
            return EXIT_SINGLE_OWNER
        if kind == "mismatch":
            blocked.append({"port": port, "reason": "unknown-http",
                            "pids": occupier_pids(port)})
            continue
        st = HelperState(port, data_dir)
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port),
                                      make_handler(st, server_ref))
        except OSError as exc:
            blocked.append({"port": port, "reason": "bind-failed-errno-%s" % exc.errno,
                            "pids": occupier_pids(port)})
            continue
        server_ref["server"] = srv
        server, state = srv, st
        break
    if server is None:
        print(json.dumps({"event": "ports-exhausted", "blocked": blocked},
                         ensure_ascii=False), file=sys.stderr)
        return EXIT_PORTS_EXHAUSTED

    try:
        write_discovery(state)
    except OSError as exc:
        _emit({"error": "discovery 写入失败", "detail": str(exc)})
        server.server_close()
        return EXIT_INTERNAL
    print("[helper] listening 127.0.0.1:%d instance=%s data=%s"
          % (state.port, state.instance_id, data_dir), file=sys.stderr)

    stop_event = threading.Event()

    def _on_signal(_signum, _frame):
        stop_event.set()
        _request_stop(server)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if args.crash_after is not None:
        def _crash():
            time.sleep(args.crash_after)
            os.kill(os.getpid(), signal.SIGKILL)  # 崩溃语义：无清理、文件残留
        threading.Thread(target=_crash, daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        # 只有优雅路径会走到这里（SIGKILL 直接终止进程）
        server.server_close()
        remove_discovery(state)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="helper_contract",
        description="ISS-029 合同原型 helper（实验用，非生产代码）")
    p.add_argument("--version", action="store_true",
                   help="输出身份/版本 JSON 并退出")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="启动合同原型服务")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--port-range", type=int, default=DEFAULT_PORT_RANGE,
                   help="候选端口数量（port..port+N-1 依序尝试）")
    s.add_argument("--data-dir", required=True,
                   help="合成数据根（discovery 文件唯一写入目录；无默认值，防误写 HOME）")
    s.add_argument("--crash-after", type=float, default=None,
                   help="N 秒后 SIGKILL 自身（崩溃语义实验钩子）")
    s.add_argument("--start-gate", default=None,
                   help=argparse.SUPPRESS)  # 仅供并发竞争反例同步起跑
    s.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        return cmd_version(args)
    if getattr(args, "func", None) is None:
        parser.error("需要子命令（serve）或 --version")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
