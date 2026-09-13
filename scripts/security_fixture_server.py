"""ISS-022 安全验证夹具服务（唯一身份的隔离 Fathom API）。

由 scripts/verify_api_security.cjs 启动；也可独立运行手工排查：

    .runtime/bin/python scripts/security_fixture_server.py

隔离合同（全部满足，禁止触碰生产服务）：
- 数据库/日报/日志/扫描根全部位于 tempfile 临时目录，退出自动释放；
- 端口由 OS 随机分配（bind :0），不占用生产 7952；
- du 替换为合成数据（含恶意文件名），系统通知与 open/-R 仅记录不执行；
- stdout 首行输出 JSON 身份（fixture_id/pid/port/root），GET /__fixture 可复核；
- stdin 关闭或 SIGTERM 结束进程。

/__fixture（仅本夹具，产品 API 不含）：返回 fixture_id、pid、port、root、
reveal_calls（记录到的 open 命令）、snapshot_ids 与 scan_runs 计数。
"""

from __future__ import annotations

import datetime as dt
import json
import signal
import sys
import threading
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

# 从 scripts/ 直接运行时把仓库根（fathom 包所在处）加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# stdout 只输出身份 JSON 行与结束行，其余日志走 stderr
from fathom import api, config, db, reports, scanner

FIXTURE_ID = uuid.uuid4().hex

# 合成 du 输出：恶意文件名只是“扫描事实”，磁盘上并不存在同名目录
MARKER_IMG = '<img src=x onerror="window.__pwned=1">'
MARKER_SVG = '"><svg onload="window.__pwned2=1">'
MARKER_NEW = '<a href="javascript:window.__pwned3=1">report-link</a>'

DAY1_SIZES = {  # 昨天基线（两个恶意名目录已存在，今天增长 -> 进入 grown 图表）
    "base": 400000,
    "Archive": 300000,
    "Notes": 100000,
    MARKER_IMG: 40000,
    MARKER_SVG: 30000,
}
DAY2_SIZES = {  # 今天：恶意名增长 + 新增恶意名/中文目录 + 真实存在的 sub 目录
    "base": 550000,
    "Archive": 300000,
    "Notes": 100000,
    MARKER_IMG: 80000,
    MARKER_SVG: 60000,
    MARKER_NEW: 150000,  # ≥100MB 才会进入“新出现的大目录”表
    "普通目录-中文": 30000,
    "sub": 500000,
}


def _mk_sizes(root: str, table: dict[str, int]) -> dict[str, int]:
    return {root: table["base"],
            **{f"{root}/{name}": kb for name, kb in table.items() if name != "base"}}


def main() -> int:
    reveal_calls: list[list[str]] = []
    all_subprocess_calls: list[list[str]] = []

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        all_subprocess_calls.append(list(cmd))
        reveal_calls.append(list(cmd))  # 本夹具里唯一合法 subprocess 来源就是 reveal
        return _Ok()

    # 三处系统动作全部收口：du→合成数据，notify→no-op，open -R→仅记录
    api.subprocess.run = fake_run
    try:
        from fathom import notify
        notify.notify_scan_done = lambda *a, **k: None
    except ImportError:
        pass

    with TemporaryDirectory(prefix="fathom-sec-") as runtime:
        base = Path(runtime)
        config.DATA_DIR = base / "data"
        config.DB_PATH = config.DATA_DIR / "fixture.db"
        config.REPORTS_DIR = base / "reports"
        config.LOGS_DIR = base / "logs"
        config.DEFAULT_ROOT = base / "root"
        config.DEFAULT_ROOT.mkdir()
        root = str(config.DEFAULT_ROOT)
        config.ensure_runtime_dirs()

        # 前缀同名邻居根与越界符号链接：供 reveal 恶意案例使用（真实存在于磁盘）
        outside = base / "outside"
        outside.mkdir()
        (base / (config.DEFAULT_ROOT.name + "-evil")).mkdir()
        (config.DEFAULT_ROOT / "sub").mkdir()
        (config.DEFAULT_ROOT / "link-out").symlink_to(outside)

        def synth_du(_root, table=DAY2_SIZES):
            # 合成采集 = 干净完整（full）：退出码 0、stderr 无任何错误行，
            # 不伪造权限缺口；stub 不执行真实 du，elapsed_seconds 如实记 0.0。
            # 结构与 fathom.scanner.run_du 当前合同（DuResult）一致，
            # classify_collection 照常把关，不因 stub 而绕过。
            return scanner.DuResult(
                sizes=_mk_sizes(root, table), exit_code=0, denied_count=0,
                elapsed_seconds=0.0, stderr_tail=(), other_error_count=0)

        scanner.run_du = synth_du

        conn = db.connect()
        try:
            for ago, table in ((1, DAY1_SIZES), (0, DAY2_SIZES)):
                scanner.run_du = (lambda t: (lambda _r: synth_du(_r, t)))(table)
                sid = scanner.create_snapshot(conn)
                day = (dt.date.today() - dt.timedelta(days=ago)).isoformat()
                conn.execute("UPDATE snapshots SET created_at=? WHERE id=?",
                             (day + "T10:00:00", sid))
                conn.commit()
            # 之后的 API 扫描（UI/CLI）统一使用今天的合成输出
            scanner.run_du = synth_du
            # 用真实 reports 模块为今天生成一份日报（内容包含恶意文件名）
            reports.write_daily_report(conn, sid)
            sids = [r["id"] for r in conn.execute(
                "SELECT id FROM snapshots ORDER BY id")]
        finally:
            conn.close()

        import uvicorn
        from fastapi.responses import JSONResponse

        @api.app.middleware("http")
        async def fixture_identity(request, call_next):
            if request.url.path == "/__fixture":
                if request.query_params.get("reset_reveal") == "1":
                    reveal_calls.clear()
                conn2 = db.connect()
                try:
                    runs = conn2.execute(
                        "SELECT COUNT(*) c FROM scan_runs").fetchone()["c"]
                    ids = [r["id"] for r in conn2.execute(
                        "SELECT id FROM snapshots ORDER BY id")]
                finally:
                    conn2.close()
                return JSONResponse({
                    "fixture_id": FIXTURE_ID,
                    "pid": _pid(),
                    "root": root,
                    "reveal_calls": reveal_calls,
                    "snapshot_ids": ids,
                    "scan_runs_count": runs,
                })
            return await call_next(request)

        server = uvicorn.Server(uvicorn.Config(
            api.app, host="127.0.0.1", port=0, log_level="warning",
            access_log=False))
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        while not server.started:
            if not t.is_alive():
                print("fixture server thread died", file=sys.stderr)
                return 1
            threading.Event().wait(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        config.PORT = port  # 守卫在请求时读取，身份行输出前已生效

        print(json.dumps({
            "fixture_id": FIXTURE_ID,
            "pid": _pid(),
            "port": port,
            "root": root,
            "root_q": quote(root),
            "snapshot_ids": sids,
            "markers": {"img": MARKER_IMG, "svg": MARKER_SVG, "new": MARKER_NEW},
        }), flush=True)

        stop = threading.Event()

        def _stop(signum, frame):
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        stop.wait()
        server.should_exit = True
        t.join(timeout=10)
        print(json.dumps({"fixture_stopped": True, "port": port}), flush=True)
    return 0


def _pid() -> int:
    import os
    return os.getpid()


if __name__ == "__main__":
    sys.exit(main())
