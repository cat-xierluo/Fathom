"""FastAPI 服务层：只读查询 + 手动扫描触发。

API 清单（自动文档见 http://127.0.0.1:7952/docs）：
- GET  /api/status           状态总览（磁盘、快照数、DB 大小、最近扫描）
- GET  /api/snapshots        快照列表（含卷容量）
- GET  /api/volume-trend     卷容量趋势序列
- GET  /api/trees            某快照的目录树（旭日图数据）
- GET  /api/diff             两快照差分
- GET  /api/trend?path=      单目录历史大小序列
- GET  /api/bigfiles         近期大文件
- GET  /api/bootstrap        发放写令牌（同源受控，ISS-022）
- POST /api/scan             触发手动扫描（后台执行，状态入 scan_runs 表）
- GET  /api/scan/status      查询扫描任务状态（?history=N 附最近 N 条记录）
- POST /api/reveal           在 Finder 中显示根内路径（受 reveal 边界约束）
- GET  /api/config           当前生效用户设置（值/来源/默认值，ISS-016A）
- PUT  /api/config           保存用户设置（需写令牌；不注册/不重载 launchd）

本地边界合同（ISS-022，仅覆盖当前单实例 loopback 服务；发行端口/服务发现归 ISS-029）：

1. Host 校验：所有请求的 Host 必须是本服务 loopback 别名（127.0.0.1 / localhost
   + config.PORT），否则 403 —— DNS rebinding 防线。
2. Origin 校验：携带 Origin 的请求必须是同源或 Tauri loader 源，跨站 403。
   CORS 中间件只决定跨域响应“可读性”，不是写鉴权（写鉴权见下一条）。
3. 写令牌：副作用方法（POST 等）必须带 X-Fathom-Token，与进程内存中的随机
   令牌比对。令牌只经同源 GET /api/bootstrap 发放，不进 URL、日志或前端持久
   存储；进程重启即轮换。同源页面 / Tauri 主窗口（remote loopback，与浏览器
   同源）启动时自动 bootstrap；无 Origin 的机器请求（curl/CLI）按下法使用：

   TOKEN=$(curl -s http://127.0.0.1:7952/api/bootstrap | python3 -c \
       'import json,sys; print(json.load(sys.stdin)["token"])')
   curl -X POST -H "X-Fathom-Token: $TOKEN" http://127.0.0.1:7952/api/scan

不提供任意 shell 执行类 API；reveal 只接受监控根内的规范化存在路径。
"""

from __future__ import annotations

import concurrent.futures
import hmac
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import SERVICE_IDENTITY, __version__, __protocol_version__, bigfiles, config, db, reports, scan_coordinator

# version 只从单一版本源 fathom.__version__ 读取（ISS-037）；本文件内
# 禁止再出现硬编码语义化版本字面量，校验器会拦截。
app = FastAPI(title="Fathom", version=__version__)

# Tauri 壳的 loader 页（tauri://localhost）需要跨域探测本服务（只读）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["tauri://localhost", "http://tauri.localhost"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger(__name__)
_scan_lock = threading.Lock()
_active_scan: scan_coordinator.ScanSession | None = None
_active_scan_thread: threading.Thread | None = None

# ---------- 本地边界合同（ISS-022） ----------

# 写令牌：进程启动时随机生成，仅存内存；不写库、不写日志、不经 URL 传输。
# 发放渠道唯一：GET /api/bootstrap（见 guard 的 Host/Origin 校验）。
_WRITE_TOKEN = secrets.token_urlsafe(32)

# Tauri 壳本地 loader 页（只读探测）；发行模式主窗口直接加载 loopback URL，
# 与浏览器同源，不在此列。
_TAURI_LOADER_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost"})

# 无副作用方法之外的请求都需要写令牌（当前路由只有 POST /api/scan、
# /api/reveal 和 PUT /api/config）
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 静态前端与 API 响应统一安全头：前端无内联脚本，ECharts 只需内联样式
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'")

# FastAPI 自动文档页（/docs /redoc）模板固定引用外部资源：jsdelivr 的
# Swagger UI / Redoc 脚本与样式、fastapi.tiangolo.com 图标、redoc 的 Google
# Fonts；初始化脚本是模板内联的，无 nonce 挂点。统一 _CSP 会把这些全部拒掉，
# 使文档页成为空页（F1 回归）。因此只对下面三个精确路径（不含任何用户输入）
# 放行这些固定来源 + 内联初始化；其余响应（前端静态页、/api、404 等）不变。
# worker-src blob: 供 Swagger UI 自带的语法高亮 Web Worker（页面内生成的
# 同源代码，非外部来源）。
_DOCS_CSP = ("default-src 'self'; "
             "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
             "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
             "https://fonts.googleapis.com; "
             "font-src 'self' https://fonts.gstatic.com; "
             "img-src 'self' data: https://fastapi.tiangolo.com "
             "https://cdn.redoc.ly; "
             "connect-src 'self'; worker-src 'self' blob:; "
             "object-src 'none'; base-uri 'self'")
_DOC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})


def _trusted_hosts() -> frozenset[str]:
    """本服务 loopback 别名（含端口）。Host 不在其中即视为 rebinding/误连。"""
    return frozenset((f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"))


def _trusted_origins() -> frozenset[str]:
    """允许携带的 Origin：自身 loopback 源 + Tauri loader 源。"""
    origins = {f"http://127.0.0.1:{config.PORT}", f"http://localhost:{config.PORT}"}
    return frozenset(origins | _TAURI_LOADER_ORIGINS)


@app.middleware("http")
async def local_boundary_guard(request: Request, call_next):
    """本地边界守卫：Host / Origin / 写令牌三重校验，先于任何 handler 副作用。

    - CORS 中间件在本守卫内层：跨站请求在这里被拒，与“响应是否可读”无关；
    - 无 Origin 的请求（curl/CLI/无 Origin 头的机器客户端）不受 Origin 规则
      限制，但写请求仍需令牌（先 GET /api/bootstrap）；
    - 文档页（精确 /docs /redoc /openapi.json）的 CSP 用 _DOCS_CSP（FastAPI
      模板固定 CDN + 内联初始化），其余响应仍用严格 _CSP；
    - 拒绝响应不含令牌或内部路径信息。
    """
    host = request.headers.get("host", "").lower()
    if host not in _trusted_hosts():
        return JSONResponse({"detail": "已拒绝：Host 不是本机服务地址"}, status_code=403)

    origin = request.headers.get("origin")
    if origin is not None and origin not in _trusted_origins():
        return JSONResponse({"detail": "已拒绝：跨站 Origin"}, status_code=403)

    if request.method not in _SAFE_METHODS:
        token = request.headers.get("x-fathom-token", "")
        ok = bool(token) and token.isascii() and hmac.compare_digest(token, _WRITE_TOKEN)
        if not ok:
            return JSONResponse(
                {"detail": "已拒绝：写请求需要 X-Fathom-Token（先 GET /api/bootstrap 获取）"},
                status_code=403)

    response = await call_next(request)
    # 文档页（精确匹配，路径不经用户输入）用文档兼容 CSP，其余一律严格 _CSP
    response.headers.setdefault(
        "Content-Security-Policy",
        _DOCS_CSP if request.url.path in _DOC_PATHS else _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.exception_handler(RequestValidationError)
async def _query_args_as_400(request: Request, exc: RequestValidationError):
    """查询参数校验失败统一 400（ISS-024：坏参数与 404/409 语义明确区分）。

    FastAPI 默认返回 422 + 机器 errors 数组；本项目其余入口（reveal、
    reports 日期）都以 400 + 中文 detail 表达坏参数。这里把全 API 的
    参数校验失败收敛到同一合同，避免前端/CLI 面对三种错误码。
    """
    parts = []
    for err in exc.errors():
        where = ".".join(str(loc) for loc in err.get("loc", [])[1:]) or "请求"
        parts.append(f"{where}：{err.get('msg', '无效')}")
    return JSONResponse({"detail": "请求参数无效：" + "；".join(parts)}, status_code=400)


# 树接口单次返回的目录上限（防止极端情况下响应过大）
TREE_MAX_NODES = 20000


def _get_conn() -> sqlite3.Connection:
    return db.connect()


def _latest_snapshots(conn: sqlite3.Connection, n: int = 2) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshots ORDER BY created_at DESC, id DESC LIMIT ?", (n,)
    ).fetchall()


def _latest_scan_state(conn: sqlite3.Connection) -> dict:
    """扫描状态来自跨入口生命周期详情；读取本身不推断 owner 已死亡。"""
    return scan_coordinator.latest_scan_state(conn)


@app.get("/health")
def api_health():
    """发行 helper 身份面（ISS-029 G4）。

    回环 Host 守卫沿用同源合同；不带 Origin/Token 也可读。仅返回身份与
    进程身份字段（service/version/protocol_version/pid/port/runtime_mode/
    status），不含令牌/凭据/真实路径（路径由 app 壳读运行根下的 port 文件
    获取，不进 /health）。同服务身份探测：cli.cmd_serve 在端口被占时
    比对此响应判让位。
    """
    return {
        "service": SERVICE_IDENTITY,
        "version": __version__,
        "protocol_version": __protocol_version__,
        "status": "ok",
        "pid": os.getpid(),
        "port": config.PORT,
        "runtime_mode": config.get_runtime_config().mode,
    }


@app.get("/api/status")
def api_status():
    conn = _get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
        latest = _latest_snapshots(conn, 1)
        latest_row = dict(latest[0]) if latest else None
        # ISS-066：把 vanished_count 和当前生效的 exclude_names 提升为顶层
        # 字段——前端覆盖说明页（ISS-002A）直接消费，不需要走 latest_snapshot
        # 表里再取；缺字段时（v4 之前旧库）以 0 / [] 兜底，与 DB 层 NOT NULL
        # DEFAULT 同语义。
        if latest_row is not None:
            vanished_count = latest_row.get("vanished_count") or 0
            latest_excludes = latest_row.get("exclude_names") or ""
        else:
            vanished_count = 0
            latest_excludes = ""
        st = os.statvfs(config.DEFAULT_ROOT)
        db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
        return {
            "root": str(config.DEFAULT_ROOT),
            "snapshot_count": count,
            "latest_snapshot": latest_row,
            "vanished_count": vanished_count,
            "exclude_names": config.EXCLUDE_NAMES,
            "disk": {
                "total_bytes": st.f_blocks * st.f_frsize,
                "free_bytes": st.f_bavail * st.f_frsize,
            },
            "db_bytes": db_size,
            "scan": _latest_scan_state(conn),
            "port": config.PORT,
            "runtime": {
                **config.get_runtime_config().public_values(),
                "schema_version": db.schema_version(conn),
            },
        }
    finally:
        conn.close()


@app.get("/api/snapshots")
def api_snapshots():
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT s.id, s.created_at, s.root, s.total_kb, s.dir_count, s.denied_count,
                      s.min_kb, s.collection_status,
                      s.vanished_count, s.exclude_names,
                      v.total_bytes, v.free_bytes
               FROM snapshots s LEFT JOIN volume_stats v ON v.snapshot_id = s.id
               ORDER BY s.created_at DESC, s.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/volume-trend")
def api_volume_trend(limit: int = Query(120, ge=2, le=2000)):
    """卷容量趋势：最新快照所属数据集（同根同 min_kb，ISS-021 口径）内
    先取最新 N 条，再正序输出（AUD-09：旧实现 ASC LIMIT 取的是最早 N 条，
    序列超过 limit 时最新点反而被截掉；跨数据集历史也不得混点）。
    """
    conn = _get_conn()
    try:
        latest = _latest_snapshots(conn, 1)
        if not latest:
            return []
        anchor = latest[0]
        rows = conn.execute(
            """SELECT s.created_at, v.total_bytes, v.free_bytes
               FROM snapshots s JOIN volume_stats v ON v.snapshot_id = s.id
               WHERE s.root = ? AND s.min_kb IS ?
               ORDER BY s.created_at DESC, s.id DESC
               LIMIT ?""",
            (anchor["root"], anchor["min_kb"], limit),
        ).fetchall()
        rows.reverse()  # 输出仍为时间正序，图表可直接渲染
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/trees")
def api_trees(snapshot_id: int | None = None, min_kb: int = Query(51200, ge=1)):
    """目录树（旭日图/矩形树图数据），默认最新快照、>=50MB 的目录。

    三类"没有数据"语义明确区分（ISS-024）：
    - 库里没有任何快照：200，snapshot_id=null（前端据此提示首扫）；
    - 指定 snapshot_id 不存在：404；
    - 有效快照但没有 >= min_kb 的目录：200 空 children（低于显示阈值
      不是错误，旧行为把这两种情况混在一个 404 里）。

    节点预算在 SQL 层生效（LIMIT），不再先取全部行；截断事实通过
    truncated/matched_count/node_count/node_limit 显式可见。
    """
    conn = _get_conn()
    try:
        if snapshot_id is None:
            latest = _latest_snapshots(conn, 1)
            if not latest:
                return {"snapshot_id": None, "root": None, "children": [],
                        "truncated": False, "matched_count": 0, "node_count": 0,
                        "node_limit": TREE_MAX_NODES}
            snapshot_id = latest[0]["id"]
        meta = conn.execute(
            "SELECT id, root FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if meta is None:
            raise HTTPException(404, f"快照 {snapshot_id} 不存在")
        # root=/ 时 rstrip("/") 会得到空串；统一归一成 "/"，避免把根
        # 当作 top 的孩子重复挂载、面包屑/父路径前缀错位。
        root_path = meta["root"].rstrip("/") or "/"

        # SQL 层预算：多取一行只为探测 truncated；非截断时不再付 COUNT 扫描。
        rows = conn.execute(
            "SELECT path, size_kb FROM entries WHERE snapshot_id = ? AND size_kb >= ? "
            "ORDER BY size_kb DESC LIMIT ?",
            (snapshot_id, min_kb, TREE_MAX_NODES + 1),
        ).fetchall()
        truncated = len(rows) > TREE_MAX_NODES
        if truncated:
            matched_count = conn.execute(
                "SELECT COUNT(*) c FROM entries WHERE snapshot_id = ? AND size_kb >= ?",
                (snapshot_id, min_kb),
            ).fetchone()["c"]
        else:
            matched_count = len(rows)
        rows = rows[:TREE_MAX_NODES]

        # 由扁平路径构建嵌套树；用字典登记路径 -> 节点。
        # du 的累计语义保证父目录大小 >= 子目录，因此浅层大目录几乎总在保留集内；
        # 个别中间层被 TREE_MAX_NODES 截断时，其子孙直接挂到顶层（旭日图可正常下钻）。
        # 每个节点只会挂到一个父节点上：子树不重复归属，无孤儿节点。
        nodes: dict[str, dict] = {}
        for r in rows:
            nodes[r["path"]] = {"name": r["path"].rsplit("/", 1)[-1], "value": r["size_kb"],
                                "path": r["path"], "children": {}}

        top: dict = {"name": root_path.rsplit("/", 1)[-1] or "/", "value": 0,
                     "path": root_path, "children": {}}
        for path in sorted(nodes, key=len):
            if path == root_path:
                continue  # 根节点本身作为 top，不再挂为自己的孩子
            parent = path.rsplit("/", 1)[0]
            if parent in nodes:
                nodes[parent]["children"][path] = nodes[path]
            else:
                top["children"][path] = nodes[path]
        if root_path in nodes:
            top["value"] = nodes[root_path]["value"]
            top["children"].update(nodes[root_path]["children"])

        def to_list(d: dict) -> dict:
            out = {"name": d["name"], "value": d["value"], "path": d["path"]}
            if d["children"]:
                out["children"] = [to_list(c) for c in
                                   sorted(d["children"].values(), key=lambda x: -x["value"])]
            return out

        return {"snapshot_id": snapshot_id, "root": root_path, "children": [to_list(top)],
                "truncated": truncated, "matched_count": matched_count,
                "node_count": len(nodes), "node_limit": TREE_MAX_NODES}
    finally:
        conn.close()


@app.get("/api/diff")
def api_diff(
    a: int | None = None,
    b: int | None = None,
    topn: int = Query(25, ge=1, le=100),
):
    """对比两个快照；默认 b=最新、a=其同数据集前驱。返回 b 相对 a 的变化。

    a/b 必须属于同一数据集（同根同入库阈值口径），否则 400 拒绝——跨根
    对比会错配基线（AUD-05）。默认选择与 write_daily_report 同一前驱逻辑。
    """
    conn = _get_conn()
    try:
        if b is None or a is None:
            latest = _latest_snapshots(conn, 1)
            if not latest:
                raise HTTPException(409, "至少需要两个快照才能对比")
            b = latest[0]["id"]
            predecessor = reports.find_same_dataset_predecessor(conn, b)
            if predecessor is None:
                raise HTTPException(409, "至少需要两个同数据集（同根同口径）快照才能对比")
            a = predecessor["id"]
        meta: dict[int, dict] = {}
        for sid in (a, b):
            row = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise HTTPException(404, f"快照 {sid} 不存在")
            meta[sid] = row
        if not reports.same_dataset(meta[a], meta[b]):
            raise HTTPException(
                400,
                f"快照 {a} 与 {b} 不属于同一数据集（同根同口径），已拒绝跨数据集对比",
            )
        old, new = reports.load_snapshot(conn, a), reports.load_snapshot(conn, b)
        diff = reports.compute_diff(old, new, topn=topn)
        def ser(items): return [
            {"path": c.path, "old_kb": c.old_kb, "new_kb": c.new_kb, "delta_kb": c.delta_kb}
            for c in items
        ]
        return {"a": dict(meta[a]), "b": dict(meta[b]),
                "grown": ser(diff["grown"]), "shrunk": ser(diff["shrunk"]),
                "added": ser(diff["added"]), "removed": ser(diff["removed"])}
    finally:
        conn.close()


@app.get("/api/trend")
def api_trend(path: str = Query(..., min_length=1),
              limit: int = Query(120, ge=2, le=2000)):
    """单目录历史大小序列。

    口径（ISS-024）：
    - 数据集隔离：以"最新一条记录该路径的快照"为锚，只返回锚快照同数据集
      （同根同 min_kb）内的点。嵌套监控根或改阈值后，同一绝对路径可能存在
      于多个数据集，跨数据集混点会画出不可比的折线（AUD-09 同类反例）。
    - 最新窗口：先取最新 N 条再正序输出，超过 limit 时最新点必须保留
      （旧实现 ASC LIMIT 截掉的是最新端）。
    - gap 语义：只返回确有记录的历史点；路径在中间某些快照缺失时缺不补点。
    - 路径从未有记录：200 + 空 points（是"无记录"不是错误，与 400/404 区分）。
    """
    conn = _get_conn()
    try:
        anchor = conn.execute(
            """SELECT s.id, s.root, s.min_kb FROM entries e
               JOIN snapshots s ON s.id = e.snapshot_id
               WHERE e.path = ?
               ORDER BY s.created_at DESC, s.id DESC LIMIT 1""",
            (path,),
        ).fetchone()
        if anchor is None:
            return {"path": path, "points": []}
        rows = conn.execute(
            """SELECT s.created_at, e.size_kb FROM entries e
               JOIN snapshots s ON s.id = e.snapshot_id
               WHERE e.path = ? AND s.root = ? AND s.min_kb IS ?
               ORDER BY s.created_at DESC, s.id DESC
               LIMIT ?""",
            (path, anchor["root"], anchor["min_kb"], limit),
        ).fetchall()
        rows.reverse()  # 输出时间正序
        return {"path": path, "points": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/bigfiles")
def api_bigfiles(days: int = Query(7, ge=1, le=90),
                 min_mb: int = Query(100, ge=1, le=10240),
                 topn: int = Query(50, ge=1, le=200)):
    """近期大文件查询（ISS-032）。

    显式触发语义：每次请求经 ``BigfilesManager.submit``；同参数并发请求自动
    去重；TTL 缓存可命中、过期可辨；find 进程组可被取消/超时回收。返回字段
    包含 ``state``（ok/no_match/permission_denied/failed/truncated/expired）、
    ``scope``、``stats``、``truncated``/``expired``/``cached``/``cache_age_s``
    /``error_message``/``raw_truncated``，前端据此展示进度、范围、失败、截断
    与过期缓存。失败/权限受限返回 200 + ``state`` 字段（语义可辨），仅坏参
    数由全局 ``RequestValidationError`` 处理器返回 400。
    """
    manager = _get_bigfiles_manager()
    future = manager.submit(
        config.DEFAULT_ROOT, days=days, min_mb=min_mb, topn=topn,
        timeout=config.BIGFILE_FIND_TIMEOUT_S,
    )
    try:
        result = future.result(timeout=config.BIGFILE_FIND_TIMEOUT_S + 1.0)
    except concurrent.futures.CancelledError:
        return JSONResponse(
            {"detail": "大文件查询已取消"},
            status_code=409,
        )
    except concurrent.futures.TimeoutError:
        future.cancel()
        return JSONResponse(
            {"detail": "大文件查询超时，请稍后重试"},
            status_code=504,
        )
    return {
        "state": result.state.value,
        "files": result.files,
        "scope": {
            "root": str(config.DEFAULT_ROOT),
            "days": days,
            "min_mb": min_mb,
            "topn": topn,
        },
        "stats": {
            "wall_ms": result.stats.wall_ms,
            "peak_rss_bytes": result.stats.peak_rss_bytes,
            "find_output_lines": result.stats.find_output_lines,
            "find_exit_code": result.stats.find_exit_code,
            "find_stderr_lines": result.stats.find_stderr_lines,
            "permission_denied_lines": result.stats.permission_denied_lines,
        },
        "truncated": result.truncated,
        "raw_truncated": result.raw_truncated,
        "expired": result.state == bigfiles.BigfilesState.EXPIRED,
        "cached": result.cached,
        "cache_age_s": result.cache_age_s,
        "error_message": result.error_message,
    }


_BIGFILES_MANAGER: bigfiles.BigfilesManager | None = None
_BIGFILES_MANAGER_LOCK = threading.Lock()


def _get_bigfiles_manager() -> bigfiles.BigfilesManager:
    """进程级单例 ``BigfilesManager``；预算与默认 TTL 取自 ``config``。"""
    global _BIGFILES_MANAGER
    if _BIGFILES_MANAGER is None:
        with _BIGFILES_MANAGER_LOCK:
            if _BIGFILES_MANAGER is None:
                _BIGFILES_MANAGER = bigfiles.BigfilesManager(
                    find_path="/usr/bin/find",
                    default_timeout_s=config.BIGFILE_FIND_TIMEOUT_S,
                    result_cap=config.BIGFILE_RESULT_CAP,
                    cache_ttl_s=config.BIGFILE_CACHE_TTL_S,
                )
    return _BIGFILES_MANAGER


@app.get("/api/bootstrap")
def api_bootstrap():
    """发放写令牌（ISS-022）。守卫已保证：Host 正确，且 Origin（若有）同源或
    Tauri loader 源——跨站脚本即使发起请求也无法读取本响应（同源策略）。

    前端把令牌保存在页面内存变量中，随写请求以 X-Fathom-Token 头回传；
    不写入 localStorage/URL。进程重启后令牌轮换，前端 403 时自动重新获取。
    """
    return {"token": _WRITE_TOKEN}


@app.get("/api/config")
def api_config_get():
    """当前生效的用户设置（ISS-016A）。

    返回四个可设置项的生效值、逐项来源（env/settings/default/cli，环境
    变量优先的依据见 fathom/config.py 模块 docstring）、恢复默认用的默认
    值，以及不入设置文件的只读策略（保留/超时/大文件默认）。只读无副作用。
    """
    return config.effective_settings_view()


@app.put("/api/config")
async def api_config_put(request: Request):
    """保存用户设置到运行根 settings.json 并在当前进程生效（ISS-016A）。

    - 守卫与既有写方法一致（Host/Origin/写令牌，见 local_boundary_guard）；
    - 校验失败 400 + 中文 detail，旧值不动（文件与进程内生效值都不变）；
    - 本切片不注册/不重载任何 launchd 服务：``service_reload`` 恒为
      ``requires_user_action``，前端如实展示“需重新安装计划才生效”。
    """
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "请求体必须是合法 JSON 对象")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    try:
        config.update_user_settings(body)
    except config.ConfigurationError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"settings.json 写入失败（旧文件未改动）：{exc}")
    return {
        "applied": True,
        "service_reload": "requires_user_action",
        "hint": ("已保存到 settings.json 并在当前服务进程生效；已安装的 "
                 "launchd 后台计划不受影响，需重新安装（main.py install）后"
                 "才按新计划时间运行。"),
        "config": config.effective_settings_view(),
    }


@app.post("/api/scan")
def api_scan():
    global _active_scan, _active_scan_thread
    if not _scan_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "message": "已有扫描在进行中"}, status_code=409)
    try:
        session = scan_coordinator.start_scan(source="api")
        _active_scan = session
        run_id = session.run_id
    except scan_coordinator.ScanBusyError as exc:
        _scan_lock.release()
        return JSONResponse(
            {"ok": False, "message": "已有扫描在进行中", "owner": {
                "source": exc.owner.get("source"),
                "started_at": exc.owner.get("started_at"),
            }},
            status_code=409,
        )
    except Exception:
        _scan_lock.release()
        raise

    def _run():
        global _active_scan, _active_scan_thread
        try:
            session.execute()
        except Exception as exc:  # noqa: BLE001 - 状态需如实回传前端
            logger.info("扫描结束：run_id=%s, result=%r", run_id, exc)
        finally:
            _active_scan = None
            _active_scan_thread = None
            _scan_lock.release()

    try:
        thread = threading.Thread(target=_run, daemon=False, name=f"fathom-scan-{run_id}")
        _active_scan_thread = thread
        thread.start()
    except Exception as exc:
        try:
            session._finish("failed", f"扫描线程启动失败：{exc}")
        except Exception:
            logger.exception("线程启动失败状态无法持久化：run_id=%s", run_id)
        finally:
            session.lease.release()
            _active_scan = None
            _active_scan_thread = None
            _scan_lock.release()
        return JSONResponse({"ok": False, "message": "扫描线程启动失败", "run_id": run_id},
                            status_code=503)
    return {"ok": True, "message": "扫描已启动", "run_id": run_id}


@app.get("/api/scan/status")
def api_scan_status(history: int = Query(0, ge=0, le=100)):
    """最新一次扫描状态；?history=N 附带最近 N 条运行记录（含失败）。"""
    conn = _get_conn()
    try:
        state = _latest_scan_state(conn)
        if history:
            rows = conn.execute(
                "SELECT r.id, r.started_at, r.finished_at, r.status, r.message, "
                "d.source, d.phase, d.snapshot_id, d.report_status, "
                "d.notification_status, d.pruned_count FROM scan_runs r "
                "LEFT JOIN scan_run_details d ON d.run_id=r.id "
                "ORDER BY r.id DESC LIMIT ?",
                (history,),
            ).fetchall()
            state["runs"] = [dict(r) for r in rows]
        return state
    finally:
        conn.close()


def _shutdown_scan() -> None:
    """服务只取消并回收自己创建的 du；从不读取/终止外部 owner PID。"""
    session = _active_scan
    thread = _active_scan_thread
    if session is not None:
        session.cancel()
    if thread is not None and thread.is_alive():
        thread.join(timeout=8)


app.router.add_event_handler("shutdown", _shutdown_scan)


# ---------- v0.2：目录浏览器 / 日报档案 / Finder 打开 ----------

@app.get("/api/browse")
def api_browse(path: str | None = None):
    """目录浏览器：指定目录的直接子目录（最新快照）+ 与同数据集前一快照的差值 + 自身趋势。

    对应 DESIGN.md 分布页合同：面包屑下钻 + 行级 Finder 打开。差值基线取
    最新快照的同数据集前驱（与日报/ diff 同一选择逻辑）；没有可比基线时
    delta_kb 为 null——无基线不伪造"增长"（AUD-04）。
    """
    conn = _get_conn()
    try:
        snaps = _latest_snapshots(conn, 1)
        if not snaps:
            raise HTTPException(409, "尚无快照，请先扫描")
        new_sid = snaps[0]["id"]
        predecessor = reports.find_same_dataset_predecessor(conn, new_sid)
        old_sid = predecessor["id"] if predecessor else None
        # root=/ 时 rstrip("/") 得空串：归一成 "/"，否则 target/prefix/面包屑
        # 全部错位（ISS-024 root=/ 边界）。
        root_path = snaps[0]["root"].rstrip("/") or "/"
        target = (path.rstrip("/") or root_path) if path else root_path
        # root=/ 时任何绝对路径都在根内（root_path + "/" 会拼出 "//" 误拒）。
        inside = target.startswith("/") if root_path == "/" else (
            target == root_path or target.startswith(root_path + "/"))
        if not inside:
            raise HTTPException(400, f"路径必须在监控根 {root_path} 之内")

        new_entries = reports.load_snapshot(conn, new_sid)
        old_entries = reports.load_snapshot(conn, old_sid) if old_sid else {}

        prefix = target.rstrip("/") + "/"  # root=/ 时 target+"/" 会拼出 "//"
        children = []
        for p, size in new_entries.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            # rest 必须是非空单段：root=/ 时根条目 "/" 自身会以空前缀命中
            if rest and "/" not in rest:  # 直接子目录
                old_size = old_entries.get(p)
                children.append({
                    "name": rest,
                    "path": p,
                    "size_kb": size,
                    # 无同数据集基线或该目录基线中未记录：差值不可知（None），
                    # 不把当前大小冒充为增量；是否首次记录由 is_new 表达。
                    "delta_kb": (size - old_size) if old_size is not None else None,
                    "is_new": old_sid is not None and p not in old_entries,
                })
        children.sort(key=lambda c: c["size_kb"], reverse=True)

        # 侧栏趋势与子目录差值同一数据集口径（ISS-024）：只取当前快照同
        # 数据集（同根同 min_kb）内该路径的记录点，跨数据集不混点。
        points = [
            dict(r)
            for r in conn.execute(
                """SELECT s.created_at, e.size_kb FROM entries e
                   JOIN snapshots s ON s.id = e.snapshot_id
                   WHERE e.path = ? AND s.root = ? AND s.min_kb IS ?
                   ORDER BY s.created_at ASC""",
                (target, snaps[0]["root"], snaps[0]["min_kb"]),
            )
        ]
        # 面包屑
        parts = target[len(root_path):].strip("/").split("/") if target != root_path else []
        crumbs = [{"name": root_path.rsplit("/", 1)[-1] or "/", "path": root_path}]
        acc = root_path
        for seg in filter(None, parts):
            acc = acc + "/" + seg
            crumbs.append({"name": seg, "path": acc})

        return {
            "path": target,
            "size_kb": new_entries.get(target, 0),
            "delta_kb": (new_entries.get(target, 0) - old_entries.get(target, 0))
                        if old_sid and target in old_entries else None,
            "children": children,
            "trend": points,
            "crumbs": crumbs,
            "snapshot_at": snaps[0]["created_at"],
        }
    finally:
        conn.close()


@app.get("/api/reports")
def api_reports():
    """历史日报档案列表（reports/*.md，新在前）。"""
    out = []
    if config.REPORTS_DIR.exists():
        for f in sorted(config.REPORTS_DIR.glob("*.md"), reverse=True):
            out.append({"date": f.stem, "bytes": f.stat().st_size})
    return {"reports": out}


@app.get("/api/reports/{date}")
def api_report(date: str):
    # 完整 YYYY-MM-DD 格式校验：日期是唯一进入文件名的用户输入片段
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    path = config.REPORTS_DIR / f"{date}.md"
    if not path.is_file():
        raise HTTPException(404, f"{date} 无日报")
    return {"date": date, "content": path.read_text(encoding="utf-8")}


@app.post("/api/reveal")
async def api_reveal(request: Request):
    """在 Finder 中显示指定路径（open -R）。仅接受监控根内真实存在、规范化后
    仍在根内的路径（ISS-022：拒绝 ..、越界符号链接、前缀同名根、相对路径、
    非对象请求）。实际交给 open -R 的是解析后的规范路径。
    """
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "请求体必须是合法 JSON 对象")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    raw = body.get("path")
    if not isinstance(raw, str) or not raw:
        raise HTTPException(400, "path 必须是非空字符串")

    root_str = str(config.DEFAULT_ROOT).rstrip("/") or "/"
    if not (raw == root_str or raw.startswith(root_str + "/")):
        # 字符串层先拒绝相对路径与前缀同名根（/scanroot-evil 不是 /scanroot）
        raise HTTPException(400, f"路径必须是监控根 {root_str} 之内的绝对路径")

    try:
        root_real = Path(root_str).resolve()
        resolved = Path(raw).resolve()
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(400, "路径无法规范化")
    if resolved != root_real and root_real not in resolved.parents:
        # 规范化层拒绝 .. 折叠与符号链接越界
        raise HTTPException(400, "路径规范化后位于监控根之外，已拒绝")
    if not resolved.exists():
        raise HTTPException(404, "路径不存在（可能已被移动或删除）")

    result = subprocess.run(["/usr/bin/open", "-R", str(resolved)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(500, f"打开失败：{result.stderr.strip()}")
    return {"ok": True, "path": str(resolved)}


# 前端静态资源挂载在最后，避免覆盖 /api 路由
if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
