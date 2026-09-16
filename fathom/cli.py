"""命令行入口。

用法（main.py <子命令>）：
  scan       执行一次扫描 + 生成日报 + 清理旧快照（launchd 每日调用）
  report     对比最近两个快照，输出 Markdown 到 stdout
  bigfiles   近期大文件清单（stdout）
  status     状态总览
  serve      启动 Web 服务（launchd 常驻）
  install    安装 launchd 定时任务与常驻 Web 服务
  uninstall  卸载 launchd 任务

版本与身份（ISS-029 G3）：``--version`` 输出单行 JSON 含 service/version/
protocol_version/python/machine/exe，应用壳以此做启动前握手。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import (
    SERVICE_IDENTITY,
    __protocol_version__,
    __version__,
    bigfiles,
    config,
    db,
    launchd,
    reports,
    scan_coordinator,
)
# G1：对象导入位于 cmd_serve 内部（延迟导入），原因有二：
#  1. PyInstaller 静态分析仍可跟踪 from . import api；
#  2. api.app 在 import 时会按当时 config 挂载静态目录，延迟导入可确保
#     config.configure(...) 已被 main() 调用后再加载 api.app，使
#     frontend mount 跟随 CLI/resource-dir 覆盖，而不是父进程环境默认值。



def _last_measured_du_seconds(root: Path) -> float | None:
    """只读查询同一 root 上次成功快照的 du_seconds；无有效记录返回 None。

    只服务扫描开场提示（ISS-062），绝不影响扫描本身：库不存在、表缺失、
    读失败、du_seconds 缺失或非正数一律按「无实测记录」处理，不编造时长。
    以 mode=ro 只读打开，不建库、不触发迁移、不写任何数据。
    """
    try:
        uri = f"{Path(config.DB_PATH).as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT du_seconds FROM snapshots WHERE root = ? ORDER BY id DESC LIMIT 1",
                (str(root),),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row[0] is None:
            return None
        seconds = float(row[0])
        # 库中现存 inf/nan 时 int(minutes+0.5) 会 OverflowError；按 ISS-063 卫生波
        # 把守卫同时收紧到 isfinite（产品唯一写路径是 scanner 实测墙钟，本不应写入
        # inf/nan；此处只兜底不改变正常行为）。
        return seconds if math.isfinite(seconds) and seconds > 0 else None
    except Exception:  # noqa: BLE001 - 提示逻辑绝不让扫描失败
        return None


def _scan_duration_hint(root: Path) -> str:
    """拼开场提示的时间段文案；分钟数取整，不足 1 分钟如实标注。"""
    limit_note = (
        f"本次安全时限 {config.DU_TIMEOUT_S:g} 秒，可用 FATHOM_DU_TIMEOUT_S 调整"
    )
    seconds = _last_measured_du_seconds(root)
    if seconds is None:
        return f"首次或无实测记录；{limit_note}"
    minutes = seconds / 60
    if minutes < 1:
        return f"上次实测 du 不到 1 分钟；{limit_note}"
    return f"上次实测 du 约 {int(minutes + 0.5)} 分钟；{limit_note}"


def cmd_scan(args: argparse.Namespace) -> int:
    config.ensure_runtime_dirs()
    root = Path(args.root).expanduser() if args.root else config.DEFAULT_ROOT
    source = args.source
    if source is None:
        source = (
            "scheduled"
            if os.environ.get("XPC_SERVICE_NAME") == config.SCAN_LABEL
            else "cli"
        )
    # 固定「5-15 分钟」与生产实测相悖（09-12 首扫约 47 分钟，09-14/15 超
    # 60 分钟被时限中断）：改为基于上次成功快照的实测 du_seconds 与配置上限。
    print(f"开始扫描 {root} ……（{_scan_duration_hint(root)}）")
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    def _cancel_on_sigterm(_signum, _frame):
        raise scan_coordinator.ScanCancelledError("收到 SIGTERM，扫描已取消")
    signal.signal(signal.SIGTERM, _cancel_on_sigterm)
    try:
        _run_id, result = scan_coordinator.run_scan(source=source, root=root)
    except scan_coordinator.ScanBusyError:
        print("已有扫描在进行中，本次未进入 du", file=sys.stderr)
        return 2
    except (scan_coordinator.ScanCancelledError, KeyboardInterrupt):
        print("扫描已取消，子进程与扫描锁已回收", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"扫描失败：{exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)

    conn = db.connect()
    try:
        sid = result["snapshot_id"]
        snap = conn.execute("SELECT * FROM snapshots WHERE id = ?", (sid,)).fetchone()
        print(
            f"完成：快照 #{sid}，目录 {snap['dir_count']} 个"
            f"（无权限 {snap['denied_count']}），总量 {snap['total_kb'] // 1024 // 1024} GB"
        )
        if result["pruned"]:
            print(f"已清理 {result['pruned']} 个过期快照")
        if result["report"]:
            print(f"日报已生成：{result['report']}")
        elif result["report_status"] == "not_available":
            print("当前只有 1 个快照，明天此时可生成首份对比日报")
        for warning in result["warnings"]:
            print(f"提示：{warning}", file=sys.stderr)
    finally:
        conn.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """对比并输出 Markdown 日报。

    与 API/日报一致（ISS-021/ISS-048）：选择基线 = ``reports.find_same_dataset_predecessor``，
    不再取全局最近两条。``--snapshot-id`` 指定 b（默认最新快照）；b 没有同数据集
    （同根同口径）前驱时输出明确文案并不伪造报告，非零退出。
    """
    conn = db.connect()
    try:
        if args.snapshot_id is not None:
            new_meta = conn.execute(
                "SELECT * FROM snapshots WHERE id = ?", (args.snapshot_id,)
            ).fetchone()
            if new_meta is None:
                print(f"快照 #{args.snapshot_id} 不存在", file=sys.stderr)
                return 1
        else:
            new_meta = conn.execute(
                "SELECT * FROM snapshots ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if new_meta is None:
                print("当前没有任何快照，无法生成对比日报", file=sys.stderr)
                return 1
        old_meta = reports.find_same_dataset_predecessor(conn, new_meta["id"])
        if old_meta is None:
            # 首扫、升级后首个新口径快照或新监控根首扫都没有可比基线：
            # 协调器以此语义把报告阶段记为 not_available（ISS-021）。
            print(
                f"快照 #{new_meta['id']}（根 `{new_meta['root']}`，"
                f"min_kb={new_meta['min_kb']}）没有同数据集（同根同口径）前驱快照，"
                "无法生成对比日报",
                file=sys.stderr,
            )
            return 1
        diff = reports.compute_diff(
            reports.load_snapshot(conn, old_meta["id"]),
            reports.load_snapshot(conn, new_meta["id"]),
        )

        def vol(sid: int):
            r = conn.execute(
                "SELECT total_bytes, free_bytes FROM volume_stats WHERE snapshot_id = ?", (sid,)
            ).fetchone()
            return (r["total_bytes"], r["free_bytes"]) if r else None

        md = reports.render_markdown(
            diff, old_meta, new_meta,
            vol(old_meta["id"]), vol(new_meta["id"]),
            bigfiles.find_big_files() if args.with_bigfiles else None,
        )
        print(md)
    finally:
        conn.close()
    return 0


def cmd_bigfiles(args: argparse.Namespace) -> int:
    files = bigfiles.find_big_files(days=args.days, min_mb=args.min_mb, topn=args.topn)
    for f in files:
        size_gb = f["size"] / 1024**3
        print(f"{size_gb:8.2f} GB  {f['mtime']}  {f['path']}")
    if not files:
        print(f"近 {args.days} 天没有 >= {args.min_mb}MB 的文件修改")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    import os

    conn = db.connect()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
        latest = conn.execute(
            "SELECT * FROM snapshots ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        st = os.statvfs(config.DEFAULT_ROOT)
        free_gb = st.f_bavail * st.f_frsize / 1024**3
        total_gb = st.f_blocks * st.f_frsize / 1024**3
        print(f"监控根目录：{config.DEFAULT_ROOT}")
        print(f"磁盘剩余：{free_gb:.1f} GB / {total_gb:.1f} GB")
        print(f"快照数量：{count}")
        if latest:
            print(
                f"最近快照：#{latest['id']} {latest['created_at']}"
                f"（总量 {latest['total_kb'] // 1024 // 1024} GB）"
            )
        db_size = config.DB_PATH.stat().st_size / 1024 / 1024 if config.DB_PATH.exists() else 0
        print(f"数据库大小：{db_size:.1f} MB（{config.DB_PATH}）")
        print(f"Web 界面：http://{config.HOST}:{config.PORT}")
        print(f"运行根：{config.get_runtime_config().runtime_dir}")
    finally:
        conn.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """启动 Web 服务；G6 端口策略与零击杀见模块 docstring。

    流程：
    1. ensure_runtime_dirs()：数据根按 release 模式落 Application Support；
       冻结产物下不写只读 bundle。
    2. 候选端口 [config.PORT, config.PORT+1, ..., config.PORT+port_range]：
       - 试 bind；成功则在该端口起 uvicorn 并写 port 文件。
       - 失败时 GET /health 探测身份：是同服务（service+protocol_version
         匹配）则让位退出 0；非本服务则跳过该端口继续候选。
       - 全部失败：输出结构化 ports-exhausted 事件和恢复动作，退 3。
    3. 永不发信号、永不杀进程、永不向占用进程发送任何请求。
    """
    config.ensure_runtime_dirs()
    target = config.PORT
    range_size = max(0, getattr(args, "port_range", config.PORT_RANGE))
    candidates = list(range(target, target + range_size + 1))

    blocked: list[dict] = []
    chosen: int | None = None
    for port in candidates:
        if _try_bind(config.HOST, port):
            chosen = port
            break
        # 端口被占用；探测身份，仅在「同服务」时让位退出 0
        kind, body = _probe_health(config.HOST, port)
        if kind == "ours":
            print(json.dumps({
                "event": "same-service-discovered",
                "service": SERVICE_IDENTITY,
                "port": port,
                "version": (body or {}).get("version"),
                "protocol_version": (body or {}).get("protocol_version"),
                "pid": (body or {}).get("pid"),
                "message": (
                    f"端口 {config.HOST}:{port} 已被同服务身份实例占用；"
                    "让位退出（exit 0），未触碰占用进程"
                ),
            }, ensure_ascii=False))
            return 0
        blocked.append({
            "port": port,
            "reason": "occupied-by-unknown",
            "probe": kind,
            "occupied_pid": _port_pids(port),
        })

    if chosen is None:
        first, last = candidates[0], candidates[-1]
        recovery = (
            f"端口 {config.HOST}:{first}..{last} 已被占用，未触碰占用进程；"
            "恢复动作："
            f"1) 检查端口 {first}..{last} 的占用进程"
            f"（lsof -tiTCP:<port> -sTCP:LISTEN -n -P），"
            "确认是否可安全停止；"
            "2) 若是其他服务，请释放端口或用 --port 指定其他起始端口；"
            "3) 若是旧 Fathom 实例，请通过应用壳的正常退出流程停止它；"
            "4) 本实例不会 kill 任何进程，未触碰任何占用者。"
        )
        print(json.dumps({
            "event": "ports-exhausted",
            "service": SERVICE_IDENTITY,
            "target_port": target,
            "candidates": candidates,
            "blocked": blocked,
            "recovery": recovery,
        }, ensure_ascii=False), file=sys.stderr)
        return 3

    write_helper_instance(chosen)
    # 实际绑定端口可能与 config.PORT（用户传入的目标端口）不同：让位后
    # _trusted_hosts() 与 /health.port 都必须反映真实监听端口，否则
    # 回环 Host 守卫会把 fallback 端口的 /health 拒为 403。
    if config.PORT != chosen:
        config.configure(port=chosen)

    def _exit_on_signal(signum, _frame):
        # uvicorn 自身的 should_exit 信号处理器在 0.52 版本下某些时序
        # 下不会让 uvicorn.run 干净返回（exit=143）。这里显式把
        # SIGTERM/SIGINT 转成 SystemExit，确保 finally 清理逻辑运行
        # 且进程以 0 退出——退出码合同：「优雅」即 0。
        raise SystemExit(0)

    # 仅在主线程注册；子线程下 Python 默认就会传播 SystemExit。
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _exit_on_signal)
        signal.signal(signal.SIGINT, _exit_on_signal)

    try:
        from . import api as _api  # 延迟导入（见模块顶部说明）
        import uvicorn
        uvicorn.run(_api.app, host=config.HOST, port=chosen,
                    log_level="info", reload=False)
    finally:
        remove_helper_instance(chosen)
    return 0


def _try_bind(host: str, port: int) -> bool:
    """无副作用占用检查；只在本进程内开闭 socket，绝不向占用者发任何信号。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _probe_health(host: str, port: int) -> tuple[str, dict | None]:
    """探测 127.0.0.1:port 的 /health；与 /health 端点同源身份判定。

    返回 ``("ours", body)``：本服务同协议版本的健康实例
           ``("mismatch", body_or_none)``：有响应但身份不符（含旧协议/非 Fathom）
           ``("refused", None)``：连接被拒/超时/非 HTTP
    """
    url = "http://127.0.0.1:%d/health" % port
    req = urllib.request.Request(url, headers={"Host": "%s:%d" % (host, port)})
    try:
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            text = resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError:
        return "mismatch", None
    except Exception:  # noqa: BLE001 - 连接被拒/超时/协议错误一律 unknown
        return "refused", None
    try:
        info = json.loads(text)
    except ValueError:
        return "mismatch", None
    if not isinstance(info, dict):
        return "mismatch", None
    if (info.get("service") == SERVICE_IDENTITY
            and info.get("protocol_version") == __protocol_version__
            and info.get("status") == "ok"):
        return "ours", info
    return "mismatch", info


def _port_pids(port: int) -> list[int]:
    """只读查询端口占用 PID；lsof 失败/缺失返回空表，绝不杀进程。"""
    try:
        out = __import__("subprocess").run(
            ["/usr/sbin/lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def write_helper_instance(port: int) -> Path:
    """以 0600 原子写 helper-instance.json 到运行根，供应用壳读取。

    不含令牌/凭据/真实资源路径；只携带身份、进程与绑定端口。
    """
    record = {
        "service": SERVICE_IDENTITY,
        "protocol_version": __protocol_version__,
        "version": __version__,
        "instance_id": uuid.uuid4().hex[:12],
        "pid": os.getpid(),
        "port": port,
        "runtime_mode": config.get_runtime_config().mode,
        "started_at": round(time.time(), 3),
    }
    runtime_dir = config.get_runtime_config().runtime_dir
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / config.HELPER_INSTANCE_FILENAME
    tmp = target.with_name(".%s.%d.tmp" % (target.name, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        return target
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def remove_helper_instance(port: int) -> None:
    """优雅退出时清理自身 helper-instance.json（仅匹配本实例身份时）。"""
    runtime_dir = config.get_runtime_config().runtime_dir
    path = runtime_dir / config.HELPER_INSTANCE_FILENAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return
    if not isinstance(record, dict):
        return
    if (record.get("service") == SERVICE_IDENTITY
            and record.get("pid") == os.getpid()
            and record.get("port") == port):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def cmd_version(_: argparse.Namespace) -> int:
    """ISS-029 G3：单行 JSON 身份面（service/version/protocol_version/...）。"""
    print(json.dumps({
        "service": SERVICE_IDENTITY,
        "version": __version__,
        "protocol_version": __protocol_version__,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "exe": sys.executable,
    }, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fathom", description="Fathom ：目录大小历史追踪")
    parser.add_argument("--version", action="store_true",
                        help="输出身份/版本 JSON 并退出（ISS-029 G3）")
    parser.add_argument("--runtime-dir", help="可写运行根（data/reports/logs 均由此派生）")
    parser.add_argument("--scan-root", help="默认受监控根（API/status/scan 共用）")
    parser.add_argument("--port", type=int, help="回环 HTTP 端口")
    parser.add_argument("--port-range", type=int, default=config.PORT_RANGE,
                        help="G6：候选端口让位段大小（target..target+range）")
    parser.add_argument("--resource-dir", help="只读前端资源根")
    parser.add_argument("--runtime-mode", choices=("development", "release"))
    # 子命令可选；只有 ``--version`` / 子命令 二选一合法；两者皆缺时报用法。
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("scan", help="扫描一次并生成日报")
    p.add_argument("--root", help="扫描根路径（默认 $HOME）")
    p.add_argument("--source", choices=("cli", "scheduled"),
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("report", help="对比快照输出日报（默认最新；同数据集前驱）")
    p.add_argument("--snapshot-id", type=int,
                   help="作为 b 的快照 ID；缺省取最新快照。基线仍按同数据集前驱选")
    p.add_argument("--with-bigfiles", action="store_true", help="附加近期大文件清单")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("bigfiles", help="近期大文件清单")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--min-mb", type=int, default=100)
    p.add_argument("--topn", type=int, default=30)
    p.set_defaults(func=cmd_bigfiles)

    p = sub.add_parser("status", help="状态总览")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("serve", help="启动 Web 服务")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("install", help="安装 launchd 任务")
    p.set_defaults(func=lambda a: (launchd.install(), 0)[1])

    p = sub.add_parser("uninstall", help="卸载 launchd 任务")
    p.set_defaults(func=lambda a: (launchd.uninstall(), 0)[1])

    args = parser.parse_args(argv)
    if args.version:
        # --version 不需要先 configure 任何运行时：单一静态身份面
        return cmd_version(args)
    if not getattr(args, "cmd", None):
        parser.error("需要子命令（scan/report/bigfiles/status/serve/install/uninstall）或 --version")
    config.configure(
        runtime_dir=args.runtime_dir,
        scan_root=args.scan_root,
        port=args.port,
        resource_dir=args.resource_dir,
        mode=args.runtime_mode,
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
