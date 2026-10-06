"""FastAPI 服务层：只读查询 + 手动扫描触发。

API 清单（自动文档见 http://127.0.0.1:7952/docs）：
- GET  /api/status           状态总览（磁盘、快照数、DB 大小、最近扫描）
- GET  /api/snapshots        快照列表（含卷容量）
- GET  /api/volume-trend     卷容量趋势序列
- GET  /api/trees            某快照的目录树（旭日图数据）
- GET  /api/diff             两快照差分
- GET  /api/diff/children    绑定 a/b 区间的直属子目录差分（ISS-147）
- GET  /api/trend?path=      单目录历史大小序列
- GET  /api/bigfiles         近期大文件（?wait=false 立即返回 task_id 句柄）
- GET  /api/bigfiles/status?task_id=   大文件任务状态（running/五态/cancelled）
- POST /api/bigfiles/cancel  取消大文件任务（需写令牌；句柄即凭证）
- GET  /api/bootstrap        发放写令牌（同源受控，ISS-022）
- POST /api/scan             触发手动扫描（后台执行，状态入 scan_runs 表）
- GET  /api/scan/status      查询扫描任务状态（?history=N 附最近 N 条记录）
- POST /api/reveal           在 Finder 中显示根内路径（受 reveal 边界约束）
- GET  /api/config           当前生效用户设置（值/来源/默认值，ISS-016A）
- PUT  /api/config           保存用户设置（需写令牌；不注册/不重载 launchd）
- GET  /api/permissions      权限状态总览（FDA 只读探测三态 + 最近通知状态 +
                            最近快照受限/消失引用，ISS-111）
- POST /api/analysis/runtimes/detect   Runtime 能力检测（仅用户点击触发，ISS-035B）
- POST /api/analysis/previews          变化解读发送预览（不可变请求，TTL 5 分钟）
- POST /api/analysis/jobs              执行预览（只收 preview_id/digest/幂等键）
- GET  /api/analysis/jobs/{id}         查询 job 状态（纯读无副作用）
- GET  /api/analysis/jobs?a=&b=        按区间查在途 job（仅非终态，ISS-120）
- POST /api/analysis/jobs/{id}/cancel  取消（本次受理 200；到达前已终结 409）
- GET  /api/analyses?a=&b=             某 a→b 区间的历史解读（含过期原因）
- DELETE /api/analyses/{id}            撤销解读（删除正文与关联事实包）

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
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Optional, Sequence

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import SERVICE_IDENTITY, __version__, __protocol_version__
from . import agent_runtime
from . import analysis_manager
from . import bigfiles, config, db, hierarchy, launchd, reports, scan_coordinator

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
            confirmed_missing_count = latest_row.get("confirmed_missing_count")
            path_unverified_count = latest_row.get("path_unverified_count")
            latest_excludes = latest_row.get("exclude_names") or ""
        else:
            vanished_count = 0
            confirmed_missing_count = None
            path_unverified_count = None
            latest_excludes = ""
        st = os.statvfs(config.DEFAULT_ROOT)
        db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
        return {
            # ISS-111（用户走查缺陷追加）：版本在构建时已知，随状态总览下发，
            # 关于页不再依赖「检查更新」才显示版本号。
            "app_version": __version__,
            "root": str(config.DEFAULT_ROOT),
            "snapshot_count": count,
            "latest_snapshot": latest_row,
            "vanished_count": vanished_count,
            "confirmed_missing_count": confirmed_missing_count,
            "path_unverified_count": path_unverified_count,
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
                      s.confirmed_missing_count, s.path_unverified_count,
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

    ISS-155 审计返修 B1 —— **窗口限定 legacy**（``plan_id IS NULL``）：
    ISS-154 范围扫描写入的新身份快照会与 legacy 历史混成一条「可比」折线
    且不报错，与 /api/trend、/api/diff 的显式拒绝直接矛盾。

    选「限定 legacy」而非 409 拒绝的理由：本端点不接受任何快照/范围参数
    （无 a/b、无 anchor、无 path），调用方**没有可被拒绝的显式身份**，
    能拒绝的对象不存在。语义对齐 reports.find_same_dataset_snapshot_rows
    与 analysis_manager._evaluate_expiry 的既有先例：别的 plan 即便同根同
    阈值同排除同一天，也不混入 legacy 窗口。

    锚点同样必须取 legacy 最新行：若锚点取了新身份行，再用它的 root/min_kb
    去选窗口，会把一条 legacy 曲线挂到新身份计划的阈值上——那正是本次要
    消除的混读。响应仍是数组（旧 volume-trend 消费者兼容，ISS-157 要求），
    排除事实通过「新身份行不在结果里」直接可观测。
    """
    conn = _get_conn()
    try:
        latest = conn.execute(
            """SELECT * FROM snapshots
               WHERE plan_id IS NULL
               ORDER BY created_at DESC, id DESC LIMIT 1"""
        ).fetchone()
        if not latest:
            return []
        anchor = latest
        rows = conn.execute(
            """SELECT s.created_at, v.total_bytes, v.free_bytes
               FROM snapshots s JOIN volume_stats v ON v.snapshot_id = s.id
               WHERE s.root = ? AND s.min_kb IS ? AND s.plan_id IS NULL
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


# ---------- 新身份消费者的显式拒绝闸门（ISS-155 续作） ----------
#
# ISS-153 起的范围快照带 ``plan_id``（新身份），legacy 行为 NULL。跨快照
# 读取（diff / trend / children 树）依赖 ``(root, min_kb, exclude_names)``
# 三元组身份——三元组**无法区分同一目录在不同计划下的两次采集**，拿它读
# 新身份行会静默产出「看起来可比、实际口径不同」的折线/排名（AUD-05 同类）。
#
# 本闸门把该缺口从「隐性失真」变成「显式拒绝」：任一侧快照带 plan_id 即
# 409 + 稳定 code，**绝不**回落 legacy 三元组口径。legacy 行（plan_id
# 为 NULL，或窄行无该列）行为逐字节不变。
_NEW_IDENTITY_HTTP = 409
_NEW_IDENTITY_CODE = "plan_identity_unsupported"


def _row_plan_id(row: object) -> object:
    """读快照行的 plan_id；窄行（旧库/测试夹具）无该列时按 legacy 处理。"""
    try:
        return row["plan_id"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None


def _reject_new_plan_identity(
    rows: Sequence[tuple[str, object]],
) -> None:
    """任一快照行带新身份即 409 拒绝（ISS-155 消费者闸门）。

    ``rows`` 为 ``(标签, 快照行)`` 序列，标签只进消息（告诉调用方是哪个
    快照参数被拒），不参与判定。抛 ``HTTPException`` 而非静默降级：新身份
    快照在 ``/api/snapshots`` 与 status 仍可查（可观测），只是**跨快照
    口径读取**在范围能力落地前不支持。
    """
    offending = [label for label, row in rows if _row_plan_id(row) is not None]
    if not offending:
        return
    labels = "、".join(offending)
    raise HTTPException(
        _NEW_IDENTITY_HTTP,
        detail={
            "error": "plan_identity_unsupported",
            "code": _NEW_IDENTITY_CODE,
            "message": (
                f"{labels} 属于新身份范围数据集（带 plan_id）。跨快照的对比/趋势"
                "需要范围计划级的可比性判定，当前版本尚不支持按新身份解读，"
                "已明确拒绝而不是按旧单根口径混读。快照本身仍可在 "
                "/api/snapshots 与 /api/status 中查看。"
            ),
            "snapshots": offending,
        },
    )


def _new_identity_scope_active() -> bool:
    """已选范围是否为新身份（范围选择只在新身份版本下才能落盘）。"""
    return config.effective_scope_selection() is not None


def _reject_bigfiles_under_new_identity() -> None:
    """bigfiles 在新身份范围下显式拒绝（ISS-150 预留接缝，ISS-155 接线）。

    ISS-150 的 ``BigfilesManager`` 去重/缓存键只含「规范根 + 模式 + 参数 +
    范围配置版本」，**不含 scope_id/plan_id**（ISS-153 留下的预留缝）。范围
    启用后同一目录可能属于不同计划下的不同数据集，此时按旧键去重会把两个
    计划的结果互相复用——那正是 ISS-150 要避免的越界读取，只是方向反了。

    与其静默复用错误结果，本期直接拒绝并说明；同时明确 reveal（已接线，
    按已选规范根逐个放行）与 bigfiles 的差别：reveal 是逐路径判定，天然
    支持多范围；bigfiles 是**单根聚合查询**，没有计划身份就无法绑定口径。
    """
    if not _new_identity_scope_active():
        return
    selection = config.effective_scope_selection()
    roots = "、".join(selection.roots) if selection is not None else ""
    raise HTTPException(
        _NEW_IDENTITY_HTTP,
        detail={
            "error": "plan_identity_unsupported",
            "code": _NEW_IDENTITY_CODE,
            "message": (
                "已启用范围采集，但当前最大文件/近期大文件查询尚未绑定计划身份"
                "（去重与缓存键缺 scope_id）。为避免把不同计划下的结果互相复用，"
                f"已拒绝本次查询。已选范围：{roots}。单根旧口径不受影响。"
            ),
            "selected_roots": list(selection.roots) if selection is not None else [],
        },
    )


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
        # ISS-155：身份闸门先于同口径校验（新身份行只按三元组会得到失真的
        # dataset_mismatch，无法区分计划）。legacy 行行为不变。
        _reject_new_plan_identity(((f"快照 {a}", meta[a]), (f"快照 {b}", meta[b])))
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


@app.get("/api/diff/children")
def api_diff_children(
    a: int = Query(...),
    b: int = Query(...),
    path: str | None = None,
    cursor: str | None = None,
    limit: int = Query(hierarchy.CHILDREN_DEFAULT_LIMIT, ge=1,
                       le=hierarchy.CHILDREN_MAX_LIMIT),
    filter: str = Query("all"),
    sort: str = Query("delta"),
):
    """绑定历史区间的同级差分（ISS-147）：a→b 区间内某目录的直属子目录。

    与 /api/diff（Top25 折叠列表）和 /api/browse（恒绑定最新快照）互补，
    为树形展开提供真实数据，不要求从折叠列表猜树：

    - a/b 必填且严格绑定本次请求：old 取 a、new 取 b，不受更新快照影响；
    - a/b 必须同数据集（同根同阈值同排除掩码），否则 400；
    - path 按路径段落在数据集根内（默认根本身），越界/相对/含 `.`、`..`
      段 400；两侧既无直接记录也无任何已记录后代时 404（如实说明可能
      低于阈值或权限受限，不冒充空目录）；
    - 行状态：measured（两侧直接记录，delta 可为 0）/ first_recorded
      （b 首次入库，不代表文件系统新建）/ unrecorded（b 未记录，不是
      删除证据）/ structural（双侧无直接记录、仅有已记录后代的导航
      节点）；单侧缺测与结构节点 old/new/delta 为 null，不填 0；
    - filter=changed 保留自身命中或有命中后代的行——父净 0 且子 +20/-20
      抵消的整枝不被漏掉；排序只在同级内进行；
    - 分页显式可见（total/has_more/next_cursor），游标绑定
      a/b/path/filter/sort，任一变化即 400 需重新从首页请求；未展示分页
      不得当作「未细分变化」；不提供子树净增量合计（目录累计不可相加）。
    """
    if filter not in hierarchy.FILTER_VALUES:
        raise HTTPException(
            400, f"filter 必须是 {'/'.join(hierarchy.FILTER_VALUES)}：{filter!r}")
    if sort not in hierarchy.SORT_VALUES:
        raise HTTPException(
            400, f"sort 必须是 {'/'.join(hierarchy.SORT_VALUES)}：{sort!r}")
    conn = _get_conn()
    try:
        meta: dict[int, dict] = {}
        for sid in (a, b):
            row = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise HTTPException(404, f"快照 {sid} 不存在")
            meta[sid] = row
        # ISS-155：身份闸门先于同口径校验（新身份行只按三元组会得到失真的
        # dataset_mismatch，无法区分计划）。legacy 行行为不变。
        _reject_new_plan_identity(((f"快照 {a}", meta[a]), (f"快照 {b}", meta[b])))
        if not reports.same_dataset(meta[a], meta[b]):
            raise HTTPException(
                400,
                f"快照 {a} 与 {b} 不属于同一数据集（同根同口径），已拒绝跨数据集对比",
            )
        try:
            target = hierarchy.normalize_target(path, meta[a]["root"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        offset = 0
        if cursor is not None:
            payload = hierarchy.decode_cursor(cursor)
            if payload is None:
                raise HTTPException(400, "游标无效：无法解码")
            if (not hierarchy.cursor_matches(
                    payload, a=a, b=b, path=target, filter=filter, sort=sort)
                    or hierarchy.cursor_offset(payload) is None):
                raise HTTPException(
                    400, "游标与当前查询参数（a/b/path/filter/sort）不匹配，"
                         "请从首页重新请求")
            offset = payload["offset"]

        collected = hierarchy.collect_children(conn, a, b, target)
        parent = collected["parent"]
        if (not collected["children"]
                and parent.old_kb is None and parent.new_kb is None):
            raise HTTPException(
                404,
                f"路径 {target} 在快照 {a} 与 {b} 中均无记录"
                "（可能低于入库阈值或权限受限）",
            )

        rows = list(collected["children"].values())
        if filter == "changed":
            rows = [r for r in rows if r.self_changed or r.has_changed_descendants]
        rows = hierarchy.sort_rows(rows, sort)
        total = len(rows)
        page = rows[offset:offset + limit]
        has_more = offset + limit < total
        next_cursor = None
        if has_more:
            next_cursor = hierarchy.encode_cursor({
                "v": 1, "a": a, "b": b, "path": target,
                "filter": filter, "sort": sort, "offset": offset + limit,
            })

        return {
            "a": dict(meta[a]),
            "b": dict(meta[b]),
            "dataset": {
                "root": meta[a]["root"],
                "min_kb": meta[a]["min_kb"],
                # v5+ schema 恒有该列；NULL 不可能出现（NOT NULL DEFAULT ''）
                "exclude_names": meta[a]["exclude_names"] or "",
            },
            "path": target,
            "ancestors": [r.to_dict() for r in hierarchy.ancestor_rows(
                conn, a, b, meta[a]["root"], target)],
            "parent": parent.to_dict(),
            "children": [r.to_dict() for r in page],
            "counts": {"a_entries": collected["a_entries"],
                       "b_entries": collected["b_entries"]},
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(page),
                "total": total,
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
            "query": {"filter": filter, "sort": sort},
        }
    finally:
        conn.close()


@app.get("/api/trend")
def api_trend(path: str = Query(..., min_length=1),
              limit: int = Query(120, ge=2, le=2000),
              anchor_snapshot_id: Optional[int] = Query(None)):
    """单目录历史大小序列（ISS-149：显式锚定 + 缺测窗口；兼容旧形态）。

    两种形态由 anchor_snapshot_id 区分：

    - 锚定调用（anchor_snapshot_id 显式）：以该快照的**数据集身份**
      （root, min_kb, exclude_names，经 reports.dataset_identity，153 扩展
      身份后自动继承）取同数据集快照窗口，窗口内**每个快照一个点**（时间
      正序）：有记录 recorded=true 且 size_kb 为实测值；无条目 size_kb=null、
      recorded=false（缺测不补 0，前端以 connectNulls=false 断线呈现）。
      每点带 snapshot_id 与完整扫描时间 created_at，同日多次扫描可区分。
      limit 截"最新 N 个快照"再正序输出：数据集快照总数超过 limit 时
      truncated=true（截掉的是最早端，最新点必在）。响应含数据集身份
      dataset 与 total_snapshots/truncated 截断说明；锚快照不存在 → 404；
      路径在该数据集从未记录 → 200 + 全 null 点（"窗口在、点缺"，不是错误）。
    - 旧调用（无 anchor_snapshot_id，兼容）：以"最新一条记录该路径的快照"
      为锚，points 只含确有记录的点（{created_at, size_kb}），ISS-024 gap
      语义与响应形态不变（缺不补点、路径从未记录返回空 points）。

    数据集隔离（ISS-024 口径 + ISS-149 收紧）：嵌套监控根、改阈值或改排除
    掩码都会形成不同数据集，跨数据集混点会画出不可比的折线（AUD-09 同类
    反例、ISS-066 三元组身份）；两种形态的数据集谓词统一走 reports 辅助
    （find_same_dataset_snapshot_rows），不在本端点另写身份过滤。
    """
    conn = _get_conn()
    try:
        if anchor_snapshot_id is not None:
            anchor = conn.execute(
                "SELECT * FROM snapshots WHERE id=?", (anchor_snapshot_id,)
            ).fetchone()
            if anchor is None:
                raise HTTPException(404, f"快照 {anchor_snapshot_id} 不存在")
            # ISS-155：锚快照带新身份即拒绝，不按旧三元组窗口混读。
            _reject_new_plan_identity(((f"锚快照 {anchor_snapshot_id}", anchor),))
            identity = reports.dataset_identity(anchor)
            all_rows = reports.find_same_dataset_snapshot_rows(conn, identity)
            truncated = len(all_rows) > limit
            window_rows = all_rows[-limit:] if truncated else all_rows
            recorded = {
                r["snapshot_id"]: r["size_kb"]
                for r in conn.execute(
                    "SELECT snapshot_id, size_kb FROM entries WHERE path = ?", (path,))
            }
            points = [
                {"snapshot_id": row["id"], "created_at": row["created_at"],
                 "size_kb": recorded.get(row["id"]),
                 "recorded": row["id"] in recorded}
                for row in window_rows
            ]
            return {
                "path": path,
                "anchor_snapshot_id": anchor["id"],
                "dataset": {"root": identity[0], "min_kb": identity[1],
                            "exclude_names": identity[2]},
                "points": points,
                "total_snapshots": len(all_rows),
                "truncated": truncated,
            }
        anchor = conn.execute(
            """SELECT s.* FROM entries e
               JOIN snapshots s ON s.id = e.snapshot_id
               WHERE e.path = ?
               ORDER BY s.created_at DESC, s.id DESC LIMIT 1""",
            (path,),
        ).fetchone()
        if anchor is None:
            return {"path": path, "points": []}
        # ISS-155：旧形态（无显式锚）命中的新身份快照同样拒绝——否则它会
        # 悄悄用旧三元组窗口把不同计划的点连成一条「可比」折线。
        _reject_new_plan_identity(((f"路径 {path} 的最新记录快照 {anchor['id']}", anchor),))
        # 旧形态兼容：与旧实现逐点等价——数据集内该路径**有记录的点**按
        # (created_at, id) 倒序取最新 limit 条再正序输出（缺测快照本就不
        # 出现）。数据集身份（窗口）来自 reports 辅助，entries 只按 path 取。
        dataset_rows = reports.find_same_dataset_snapshot_rows(
            conn, reports.dataset_identity(anchor))
        size_by_sid = {
            r["snapshot_id"]: r["size_kb"]
            for r in conn.execute(
                "SELECT snapshot_id, size_kb FROM entries WHERE path = ?", (path,))
        }
        recorded_in_dataset = [
            (row["created_at"], row["id"], size_by_sid[row["id"]])
            for row in dataset_rows if row["id"] in size_by_sid
        ]
        recorded_in_dataset.sort(key=lambda t: (t[0], t[1]), reverse=True)
        legacy = [
            {"created_at": created_at, "size_kb": size_kb}
            for created_at, _sid, size_kb in recorded_in_dataset[:limit]
        ]
        legacy.reverse()
        return {"path": path, "points": legacy}
    finally:
        conn.close()


@app.get("/api/bigfiles")
def api_bigfiles(days: int = Query(7, ge=1, le=90),
                 min_mb: int = Query(100, ge=1, le=10240),
                 topn: int = Query(50, ge=1, le=200),
                 mode: str = Query("recent", pattern="^(recent|largest)$"),
                 path: Optional[str] = Query(None),
                 wait: bool = Query(True)):
    """近期大文件 / 当前最大文件查询（ISS-032；ISS-150 扩展 mode/path；
    ISS-164 扩展 task_id/wait）。

    显式触发语义：每次请求经 ``BigfilesManager.submit``；同参数并发请求自动
    去重；TTL 缓存可命中、过期可辨；find 进程组可被取消/超时回收。返回字段
    包含 ``state``（ok/no_match/permission_denied/failed/truncated/expired）、
    ``scope``、``stats``、``truncated``/``expired``/``cached``/``cache_age_s``
    /``error_message``/``raw_truncated``，前端据此展示进度、范围、失败、截断
    与过期缓存。失败/权限受限返回 200 + ``state`` 字段（语义可辨），仅坏参
    数由全局 ``RequestValidationError`` 处理器返回 400。

    ISS-150：
    - ``mode=largest`` 按当前 st_size 逻辑大小排序，不带 mtime 过滤；
      ``mode=recent``（缺省）保持既有行为，旧 GET 调用不变。
    - ``path`` 限定查询目录：仅接受监控根内的规范化目录（经
      ``bigfiles.resolve_query_root``，拒绝 ``..``/相似前缀根/符号链接越界，
      路径已移走返回 404）；不传即监控根。
    - ``incomplete=True`` 表示时间/输出预算提前截断，``files`` 只是
      「已检查文件中的较大项」而非目录的当前最大文件。
    - ``stats.started_at``/``stats.finished_at`` 为查询起止（epoch 秒）。

    ISS-164（任务句柄与取消的产品入口）：
    - 响应新增 ``task_id``：由去重键确定性派生（``bf-`` + sha256 前 16 位），
      同参并发去重与按句柄寻址因此落在同一条任务上；``task_id`` 不泄露
      完整 root 路径。选确定性哈希而非 UUID 的完整理由见
      ``bigfiles.BigfilesManager.task_id_for`` 的 docstring。
    - ``wait=false``（新增参数，缺省 ``true`` = 旧行为）立即返回 202 +
      ``task_id``/``state``/``scope``，**不阻塞**等 find：这是前端「离开页面
      / 改查询范围时取消本任务」能拿到句柄的前提——浏览器中止 HTTP 请求只
      断连接、不取消服务端 find，取消必须显式走 ``POST /api/bigfiles/cancel``。
      此时结果本体仍从本端点（``wait=true``）按原参数取。
    - ``wait=true`` 语义逐字段不变（仅新增 ``task_id``）：旧前端/CLI 零改动；
      被取消/超时时 409/504 响应体附增 ``task_id`` 供前端恢复句柄。
    """
    # ISS-155：新身份范围下显式拒绝（ISS-150 预留接缝未绑定 scope_id）。
    _reject_bigfiles_under_new_identity()
    try:
        resolved_root = bigfiles.resolve_query_root(path)
    except bigfiles.BigfilesScopeError as exc:
        raise HTTPException(exc.status, str(exc))
    timeout_s = (config.BIGFILE_LARGEST_TIMEOUT_S if mode == "largest"
                 else config.BIGFILE_FIND_TIMEOUT_S)
    manager = _get_bigfiles_manager()
    future = manager.submit(
        resolved_root, days=days, min_mb=min_mb, topn=topn, mode=mode,
        timeout=timeout_s,
    )
    task_id = future.task_id
    scope = {
        "root": str(config.DEFAULT_ROOT),
        "resolved_root": str(resolved_root),
        "requested_path": path,
        "mode": mode,
        "days": days,
        "min_mb": min_mb,
        "topn": topn,
    }
    if not wait:
        # 非阻塞提交：立刻交句柄（202），不碰结果本体。state 如实——缓存命中
        # 的同参请求此时已是终态（terminal=true），前端应改走 wait=true 取本体。
        submitted = manager.status_view(task_id)
        return JSONResponse(
            {"task_id": task_id, "state": submitted["state"],
             "terminal": submitted["terminal"], "submitted": True,
             "scope": scope},
            status_code=202,
        )
    try:
        result = future.result(timeout=timeout_s + 1.0)
    except concurrent.futures.CancelledError:
        return JSONResponse(
            {"detail": "大文件查询已取消", "task_id": task_id,
             "state": "cancelled"},
            status_code=409,
        )
    except concurrent.futures.TimeoutError:
        future.cancel()
        return JSONResponse(
            {"detail": "大文件查询超时，请稍后重试", "task_id": task_id},
            status_code=504,
        )
    return {
        "state": result.state.value,
        "task_id": task_id,
        "files": result.files,
        "scope": scope,
        "stats": {
            "wall_ms": result.stats.wall_ms,
            "peak_rss_bytes": result.stats.peak_rss_bytes,
            "find_output_lines": result.stats.find_output_lines,
            "find_exit_code": result.stats.find_exit_code,
            "find_stderr_lines": result.stats.find_stderr_lines,
            "permission_denied_lines": result.stats.permission_denied_lines,
            "started_at": result.stats.started_at,
            "finished_at": result.stats.finished_at,
        },
        "truncated": result.truncated,
        "raw_truncated": result.raw_truncated,
        "incomplete": result.incomplete,
        "expired": result.state == bigfiles.BigfilesState.EXPIRED,
        "cached": result.cached,
        "cache_age_s": result.cache_age_s,
        "error_message": result.error_message,
    }


@app.get("/api/bigfiles/status")
def api_bigfiles_status(task_id: str = Query(...)):
    """按 task_id 查大文件任务状态（ISS-164，纯读、无副作用、无需写令牌）。

    返回真实状态与既有五态对齐：在途 ``running``；终态为 ``BigfilesState``
    六态之一或 ``cancelled``。另含 ``terminal``/``cancel_requested``/
    ``created_at``/``finished_at``/``error_message``/``scope``（scope 不含
    requested_path——状态查询只从句柄知道查询本身，不回显调用方原始输入）。

    **不含结果本体**：``files``/``stats`` 等仍从 ``GET /api/bigfiles`` 按原
    参数按需取（命中缓存或在途去重），状态查询保持轻量、可高频轮询。
    未知或已超出保留期的 task_id → 404；缺参数 → 400。
    """
    try:
        return _get_bigfiles_manager().status_view(task_id)
    except bigfiles.BigfilesTaskNotFound:
        raise HTTPException(404, "未知或已过期的 task_id")


@app.post("/api/bigfiles/cancel")
def api_bigfiles_cancel(payload: Optional[dict] = Body(default=None)):
    """按 task_id 取消大文件任务（ISS-164，需写令牌 X-Fathom-Token）。

    守卫：POST 走既有 ``local_boundary_guard`` 写令牌闸门（缺/伪造令牌 403），
    与 ``POST /api/scan``/``/api/reveal``/``PUT /api/config`` 同一口径。

    只取消该 task_id 对应任务：触发既有取消路径（find 进程组 SIGTERM →
    回收窗口内升级 SIGKILL，只回收自有进程组），终态 ``cancelled``。取消受理
    与回收完成之间是异步的——响应里的 ``state``/``terminal`` 如实反映当下，
    前端可轮询 ``GET /api/bigfiles/status`` 确认收敛。

    幂等（不报错、状态如实）：
    - 已取消 → 200 + ``cancelled=true``；
    - 已完成/已过期（终态非 cancelled）→ 200 + ``cancelled=false`` + 真实
      终态，不谎称已取消、不复活终态；
    - 未知或已超出保留期的 task_id → 404。

    句柄即凭证：不校验「谁提交的」，也没有「他人任务」概念；越权面由写令牌
    闸门承担（ISS-022 同源）。
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - {"task_id"})
    if unknown:
        raise HTTPException(400, f"请求体含未知字段：{', '.join(unknown)}")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise HTTPException(400, "task_id 必须是非空字符串")
    try:
        return _get_bigfiles_manager().cancel_task(task_id)
    except bigfiles.BigfilesTaskNotFound:
        raise HTTPException(404, "未知或已过期的 task_id")


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


# ---------- 变化解读（ISS-035B：预览/派发/状态/取消/历史/撤销） ----------

_ANALYSIS_MANAGER: analysis_manager.AnalysisManager | None = None
_ANALYSIS_MANAGER_LOCK = threading.Lock()


def _get_analysis_manager() -> analysis_manager.AnalysisManager:
    """进程级单例 ``AnalysisManager``（生产路径，不注入测试参数）。

    首次创建时执行启动 reconcile：遗留 starting/running/cancelling 统一
    标 interrupted（不自动重派、不按持久化 PID 发信号）。"""
    global _ANALYSIS_MANAGER
    if _ANALYSIS_MANAGER is None:
        with _ANALYSIS_MANAGER_LOCK:
            if _ANALYSIS_MANAGER is None:
                manager = analysis_manager.AnalysisManager()
                manager.reconcile_startup()
                _ANALYSIS_MANAGER = manager
    return _ANALYSIS_MANAGER


@app.exception_handler(analysis_manager.AnalysisError)
async def _analysis_error_handler(request: Request,
                                  exc: analysis_manager.AnalysisError):
    """分析合同错误：稳定 reason_code + 可读说明，不透出 stderr/路径/凭据。

    403 未启用/未配置；409 busy/预览失效/升级停写/幂等冲突/终态竞争；
    404 未知资源；400 坏参数或数据集口径不一致。``extra`` 携带的附加字段
    （如 409 analysis_busy 判定到占用者时的 active_job_id，ISS-120）原样
    并入响应体；未携带时响应体保持原两键形状（既有错误合同不变）。"""
    payload = {"reason_code": exc.reason_code, "detail": str(exc)}
    if exc.extra:
        payload.update(exc.extra)
    return JSONResponse(payload, status_code=exc.status_code)


def _analysis_body(request_json: object, allowed: set[str]) -> dict:
    """分析端点请求体校验：必须是 JSON 对象且仅含声明字段（严格拒绝，
    不静默忽略——静默丢弃会让调用方误以为某个字段已生效）。"""
    if not isinstance(request_json, dict):
        raise analysis_manager.AnalysisError(
            "bad_request", "请求体必须是 JSON 对象", status_code=400)
    unknown = sorted(set(request_json) - allowed)
    if unknown:
        raise analysis_manager.AnalysisError(
            "bad_request", f"请求体含未知字段：{', '.join(unknown)}",
            status_code=400)
    return request_json


@app.post("/api/analysis/runtimes/detect")
async def api_analysis_detect(request: Request):
    """Runtime 能力检测（仅用户点击触发；POST 写令牌保护）。

    可选 ``{"runtime_id": ...}`` 只探测一家；缺省探测全部四家（共享
    20 秒预算）。探测只读：不发送业务数据、不改用户 CLI 配置；结果
    如实区分 not_found/broken/unsupported/ready，认证恒 unknown（
    ``--version`` 成功不解释为已登录）。分析未启用也可检测（检测是
    选择的前置）。"""
    body: dict = {}
    raw = await request.body()
    if raw.strip():
        try:
            body = json.loads(raw)
        except ValueError:
            raise analysis_manager.AnalysisError(
                "bad_request", "请求体必须是合法 JSON", status_code=400)
        body = _analysis_body(body, {"runtime_id"})
    runtime_id = body.get("runtime_id")
    if runtime_id is not None:
        if not isinstance(runtime_id, str) or runtime_id not in agent_runtime.CANDIDATES:
            raise analysis_manager.AnalysisError(
                "unknown_runtime", f"未知的 Runtime ID：{runtime_id!r}",
                status_code=400)
        infos = {runtime_id: agent_runtime.detect_runtime(
            agent_runtime.get_candidate(runtime_id))}
    else:
        infos = agent_runtime.probe_all()
    return {"runtimes": {cid: info.to_dict() for cid, info in infos.items()}}


@app.post("/api/analysis/previews")
async def api_analysis_preview(request: Request):
    """生成发送预览（方案 §4.2：预览=不可变请求，TTL 5 分钟，内存最多 8 份）。

    请求体 ``{"a": int, "b": int}``。响应含完整发送文本（prompt_text）、
    双 digest、请求范围 manifest 与 Runtime 身份；本端点零外传、零派发。"""
    body = _analysis_body(await _request_json(request), {"a", "b"})
    a, b = body.get("a"), body.get("b")
    for name, value in (("a", a), ("b", b)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise analysis_manager.AnalysisError(
                "bad_request", f"{name} 必须是快照 ID 整数", status_code=400)
    preview = _get_analysis_manager().create_preview(a, b)
    return preview.public_dict()


async def _request_json(request: Request) -> object:
    try:
        return await request.json()
    except ValueError:
        raise analysis_manager.AnalysisError(
            "bad_request", "请求体必须是合法 JSON", status_code=400)


@app.post("/api/analysis/jobs")
async def api_analysis_jobs(request: Request):
    """执行预览（只收 preview_id + request_digest + idempotency_key）。

    202 返回 job；幂等重放同 job；不同请求忙时 409（不排队、不自动
    重试、不换 Runtime）；预览过期/数据或设置变化 409 要求重新预览；
    未启用 403。请求体不接受任意 prompt/命令/cwd/executable。"""
    body = _analysis_body(await _request_json(request),
                          {"preview_id", "request_digest", "idempotency_key"})
    for name in ("preview_id", "request_digest", "idempotency_key"):
        if not isinstance(body.get(name), str) or not body.get(name).strip():
            raise analysis_manager.AnalysisError(
                "bad_request", f"{name} 必须是非空字符串", status_code=400)
    job, replayed = _get_analysis_manager().start_job(
        body["preview_id"], body["request_digest"], body["idempotency_key"])
    return JSONResponse({"job": job, "replayed": replayed}, status_code=202)


@app.get("/api/analysis/jobs/{job_id}")
def api_analysis_job(job_id: str):
    """查询 job 状态（纯读，无派发/探测/外传副作用）。"""
    return {"job": _get_analysis_manager().job_view(job_id)}


@app.get("/api/analysis/jobs")
def api_analysis_jobs_lookup(a: int = Query(...), b: int = Query(...)):
    """按 a→b 快照区间查在途分析 job（ISS-120，035B 接缝补卡）。

    只返回非终态（starting/running/cancelling）的既有 job 视图（字段同
    GET /api/analysis/jobs/{id}，不含 prompt/正文）；终态不返回，历史归
    GET /api/analyses、按 ID 归 GET /api/analysis/jobs/{id}。用途：页面
    刷新/新会话丢失 job_id 后重新发现他方在途任务，再按 job_id 恢复
    显示。纯读无副作用，不引入自动恢复/重派。守卫口径随既有读端点：
    GET 免写令牌；不设 analysis.enabled 403 门——启用门只挡新派发，
    不挡生命周期事实查询（与 GET /api/analyses 完全一致）。"""
    return {"a": a, "b": b,
            "jobs": _get_analysis_manager().find_active_jobs(a, b)}


@app.post("/api/analysis/jobs/{job_id}/cancel")
def api_analysis_job_cancel(job_id: str):
    """请求取消：本次受理 → 200；到达前已终结 → 具名 409（ISS-175）。

    两种响应各自对应一个确定事实，判据来自 ``cancel_job`` 的受理标记而**不是**
    「返回时是否终态」：

    - **200**：本次调用受理了取消（到达时非终态，且取消已生效）。响应里的
      ``job.status`` 如实反映当下，**可以是终态 cancelled**——那是 worker 在
      本次取消驱动下于毫秒内收敛的结果，属取消成功，不是取消失败。
    - **409 + reason_code=job_terminal**：请求到达前任务已是终态，或到达后完成
      抢先写入终态。取消确实没生效；响应携带真实终态，绝不复活终态。

    终态不复活、取消先到即取消胜的不变量不变（见
    ``analysis_manager.cancel_job`` / ``_commit_success``）。
    """
    view = _get_analysis_manager().cancel_job(job_id)
    if view.get("cancel_accepted"):
        return {"job": view}
    return JSONResponse(
        {"reason_code": "job_terminal",
         "detail": "任务已结束，取消不再生效",
         "job": view},
        status_code=409,
    )


@app.get("/api/analyses")
def api_analyses(a: int = Query(...), b: int = Query(...)):
    """某 a→b 区间的历史解读（读取层评估过期；新增快照不使旧报告过期）。

    每条含已保存的结构化结果与脱敏事实包（证据可回溯）；expired=true
    附原因（snapshot_replaced/snapshot_pruned/dataset_unverifiable）。"""
    return {"a": a, "b": b,
            "analyses": _get_analysis_manager().list_analyses(a, b)}


@app.delete("/api/analyses/{analysis_id}")
def api_analysis_revoke(analysis_id: int):
    """撤销（删除正文与关联事实包；生命周期行保留 revoked_at 审计）。

    不能承诺清除 SQLite 旧页/WAL、旧备份或第三方 CLI/模型服务留存。"""
    return _get_analysis_manager().revoke_analysis(analysis_id)


@app.get("/api/bootstrap")
def api_bootstrap():
    """发放写令牌（ISS-022）。守卫已保证：Host 正确，且 Origin（若有）同源或
    Tauri loader 源——跨站脚本即使发起请求也无法读取本响应（同源策略）。

    前端把令牌保存在页面内存变量中，随写请求以 X-Fathom-Token 头回传；
    不写入 localStorage/URL。进程重启后令牌轮换，前端 403 时自动重新获取。
    """
    return {"token": _WRITE_TOKEN}


def _config_view_with_reload_state() -> dict:
    """effective_settings_view + 只读漂移检测（ISS-016B）。

    解析已注册 scan plist 的计划时间并与生效 scan_time 比对，结果挂在
    ``service_reload_state`` 键上。读取异常一律降级 ``state="unknown"``
    （绝不猜、绝不让 /api/config 因漂移检测而 5xx）；全程零写入零命令
    执行。
    """
    view = config.effective_settings_view()
    try:
        registered = launchd.read_registered_scan_time()
    except Exception:  # noqa: BLE001 - 漂移检测失败只降级，不影响配置读取
        registered = {"read": "unknown", "scan_time": None}
    view["service_reload_state"] = launchd.service_reload_state(
        view.get("scan_time"), registered
    )
    return view


def _service_reload_hint(reload_state: dict) -> str:
    """PUT 反馈文案：保留 ISS-016A 的基础提示（兼容旧消费方），按四态追加说明。"""
    base = ("已保存到 settings.json 并在当前服务进程生效；已安装的 "
            "launchd 后台计划不受影响，需重新安装（python -m fathom install）后"
            "才按新计划时间运行。")
    state = reload_state.get("state") if isinstance(reload_state, dict) else None
    if state == "in_sync":
        return base + "当前已注册计划时间与生效设置一致，无需重装。"
    if state == "drift":
        return base + (
            f"检测到计划时间不一致（已注册 "
            f"{reload_state.get('registered_scan_time')}，当前设置 "
            f"{reload_state.get('current_scan_time')}）；请在设置页经确认后"
            "重新安装计划。"
        )
    if state == "not_registered":
        return base + "当前尚未注册后台计划。"
    return base + "无法读取已注册计划（一致性未知）。"


@app.get("/api/config")
def api_config_get():
    """当前生效的用户设置（ISS-016A）+ 计划一致性（ISS-016B）。

    返回四个可设置项的生效值、逐项来源（env/settings/default/cli，环境
    变量优先的依据见 fathom/config.py 模块 docstring）、恢复默认用的默认
    值、不入设置文件的只读策略（保留/超时/大文件默认），以及
    ``service_reload_state``（只读漂移检测：in_sync/drift/not_registered/
    unknown，见 fathom/launchd.py）。只读无副作用。
    """
    return _config_view_with_reload_state()


@app.put("/api/config")
async def api_config_put(request: Request):
    """保存用户设置到运行根 settings.json 并在当前进程生效（ISS-016A）。

    - 守卫与既有写方法一致（Host/Origin/写令牌，见 local_boundary_guard）；
    - 校验失败 400 + 中文 detail，旧值不动（文件与进程内生效值都不变）；
    - 服务自身仍不注册/不重载任何 launchd 服务：``service_reload`` 恒为
      ``requires_user_action``（ISS-016A 字符串合同，向后兼容保留）；
      ISS-016B 起新增 ``service_reload_state`` 四态结构与按态说明的
      ``hint``——重装本身只经设置页确认层（010B 桥）执行。
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
    if "analysis" in body:
        # ISS-035B：授权关闭/切 Runtime → 原子撤销提交资格、失效预览并
        # 取消在途（与完成竞争只有一个确定结果，见 AnalysisManager）。
        try:
            _get_analysis_manager().refresh_policy()
        except Exception:  # noqa: BLE001 - 撤销失败不阻塞设置保存的成功反馈
            logger.exception("analysis 策略刷新失败（设置已保存）")
    config_view = _config_view_with_reload_state()
    return {
        "applied": True,
        "service_reload": "requires_user_action",
        "service_reload_state": config_view["service_reload_state"],
        "hint": _service_reload_hint(config_view["service_reload_state"]),
        "config": config_view,
    }


# ---------- ISS-155：范围发现 / 计划预览 / 范围选择保存 ----------
#
# 合同要点（任务卡 ISS-155）：
# - **新能力不自动启用**：``config.scope_settings_view()["enabled"]`` 为 False
#   时（用户从未显式选择），本组端点只读回显发现结果，生产扫描范围与旧口径
#   逐字节不变。发现 ≠ 已监控。
# - 读端点（discovery/preview）无副作用、无需写令牌；写端点（PUT scope）经
#   local_boundary_guard 的 Host/Origin/写令牌三重闸门，与既有写方法同一合同。
# - 保存**只改下一轮计划**，不触发扫描、不注册/重载任何调度。
# - 身份/计划未接线的消费者（trend/AI 等跨口径聚合）由调用方显式拒绝，本卡
#   不把新身份静默塞进旧口径读取路径。

def _discovery_payload() -> dict:
    """启动盘容器/卷发现（ISS-152 只读适配器的 HTTP 转交，零持久化）。

    发现失败/非 APFS/非 macOS 一律返回 storage.py 的降级态 + errors，
    本层不猜、不补造身份，也不回退成「全盘可读」的乐观结论。
    """
    from . import storage  # 延迟导入：发现适配器只在被请求时才载入
    try:
        discovery = storage.discover_startup(config.DEFAULT_ROOT)
    except Exception as exc:  # noqa: BLE001 - 发现失败必须可观测，不 5xx 崩服务
        logger.exception("存储发现失败（范围预览不可用）")
        return {"ok": False, "error": "discovery-failed",
                "detail": type(exc).__name__}
    return {"ok": True, "discovery": discovery.as_dict()}


@app.get("/api/storage/discovery")
def api_storage_discovery():
    """启动盘容器/卷发现结果（只读，无副作用、无需写令牌）。"""
    return _discovery_payload()


@app.get("/api/storage/summary")
def api_storage_summary(limit: int = Query(120, ge=2, le=2000)):
    """容器容量与目录归因摘要（ISS-157，只读持久化事实）。

    只返回**已落库**的容量样本、轮次成员与快照测量：请求本身不触发任何
    du/statvfs/发现调用，更不隐式全盘扫描。

    契约要点（每条都对应一条不可越过的红线）：

    - **共享 free 只计一次**：同容器的多卷共享剩余空间，剩余空间取容器级
      读数一次（``storage.shared_free_once``），卷级 free 绝不相加。
    - **目录归因父子不可加**：归因只用互不重叠的测量根
      （``storage.non_overlapping_roots``），子根被显式列出为已吸收。
    - **差额只在同主体/同计划且两侧有效可比较时显示**，并带
      「尚无法由目录变化解释」限制；有符号，不是垃圾/可回收量；不可比
      则为 ``null`` 且给出原因，缺失读数不补 0。
    - **失败成员的旧有效值标 stale**，不当本轮已测贡献。
    - **整轮跨时间**：给出成员各自时刻与显式跨度，不压成单一时点。
    - **不计算伪覆盖率**：阈值/权限不参与覆盖率计算，摘要里没有该字段。
    """
    from . import storage
    conn = _get_conn()
    try:
        return _storage_summary_payload(conn, limit=limit)
    finally:
        conn.close()


def _storage_summary_payload(conn: sqlite3.Connection, *, limit: int) -> dict:
    """组装摘要负载（纯读库；不调用任何采集/发现入口）。"""
    from . import storage

    selection = None
    try:
        selection = config.effective_scope_selection()
    except Exception:  # noqa: BLE001 - 配置不可读不应让摘要 500
        selection = None
    container_id = getattr(selection, "container_id", None) if selection else None

    samples = _capacity_sample_rows(conn, limit=limit,
                                    container_id=container_id)
    free_view = storage.shared_free_once(samples, container_id)

    rounds = reports.latest_rounds_with_members(conn, limit=2)
    latest = rounds[0] if rounds else None
    previous = rounds[1] if len(rounds) > 1 else None

    attribution = reports.round_attribution(conn, latest["round_id"]) \
        if latest else None
    prev_attribution = reports.round_attribution(conn, previous["round_id"]) \
        if previous else None

    # 跨轮身份核对（ISS-157 返修 B1）：先查两轮是否同主体/同计划/同归因根
    # 集合，再叠加**两侧各自**的质量与 stale 条件。任一不满足即不可比 →
    # 差额为 null，绝不因为「本轮是 full」就放行。
    identity_reasons: list[str] = []
    if attribution and prev_attribution:
        identity_reasons = reports.cross_round_identity_reasons(
            attribution, prev_attribution)
    # 质量门两侧对称（R1 返修）：前轮不合格同样让基线失效，且原因文本
    # 指明是哪一轮——「本轮 full」不能为「前轮 partial」背书。
    own_reasons: list[str] = (
        reports.own_round_reasons(attribution, label="本轮")
        if attribution is not None else [])
    previous_reasons: list[str] = (
        reports.own_round_reasons(prev_attribution, label="前轮")
        if prev_attribution is not None else [])

    # 容量主体与生效 scope 绑定（R2 返修）：生效容器与轮次成员容器不一致时，
    # 容量读数说的不是同一件事 → 明确不可比，不输出数值差额。
    scope_reasons: list[str] = []
    if latest and previous:
        if not container_id:
            scope_reasons.append(
                "缺容量主体身份：无生效容器，容量读数无法与轮次绑定。")
        for rnd, att in ((latest, attribution), (previous, prev_attribution)):
            if att is None:
                continue
            rnd_containers = set(att.get("container_ids") or [])
            if container_id and container_id not in rnd_containers:
                scope_reasons.append(
                    f"容量主体不匹配：生效容器 {container_id} 不在该轮"
                    f"（round_id={rnd['round_id']}）归因容器"
                    f" {sorted(rnd_containers)} 内。")

    # 成员级容器绑定（B3 返修）：上面的「所选容器 ∈ 轮次容器集合」是包含
    # 判断，混容器成员与 container_id=NULL 的成员都能绕过它。参与求和的
    # 成员必须逐个属于生效容器，否则 measured_kb 的口径就不是所选容器。
    member_reasons: list[str] = []
    if latest and previous:
        for rnd, att in ((latest, attribution), (previous, prev_attribution)):
            if att is None:
                continue
            member_reasons += reports.member_container_reasons(
                att, container_id=container_id,
                label="本轮" if rnd is latest else "前轮")

    comparable = bool(attribution and prev_attribution and not identity_reasons
                      and not own_reasons and not previous_reasons
                      and not scope_reasons and not member_reasons)
    all_reasons = identity_reasons + scope_reasons + member_reasons \
        + own_reasons + previous_reasons
    if latest and previous and not comparable:
        block_reason = "；".join(all_reasons) or None
    else:
        block_reason = None

    # P3：归因内的 comparable_to_previous 只看**本轮**质量门，会与顶层跨轮
    # 结论矛盾（跨计划/跨根/成员容器不符时它仍为 true）。顶层 comparability
    # 是跨轮结论的唯一权威，故在组装处把该字段对齐到顶层判定，避免同一个
    # payload 里出现两个互相打架的可比性答案。
    if attribution is not None:
        if latest and previous:
            attribution["comparable_to_previous"] = comparable
        attribution["comparable_note"] = (
            "需要同主体、同计划、两侧都有效（无失败成员、无子根吸收）"
            "且参与归因的成员逐一属于所选容器才可比；否则差额为 null。"
            "本字段已对齐顶层 comparability（跨轮结论的唯一权威）。")

    payload = {
        "scope": {
            "container_id": container_id,
            "mode": getattr(selection, "mode", None) if selection else None,
            "roots": list(getattr(selection, "roots", ()) or ()) if selection else [],
            "identity_version": getattr(selection, "identity_version", None)
            if selection else None,
        },
        "capacity": {
            "subject": "container" if container_id else None,
            "container_id": container_id,
            "unit": "bytes",
            **free_view,
            "samples": len(samples),
        },
        "round": latest,
        "previous_round": previous,
        "attribution": attribution,
        "comparability": {
            "comparable": comparable,
            "identity_reasons": identity_reasons,
            "scope_reasons": scope_reasons,
            "member_reasons": member_reasons,
            "own_reasons": own_reasons,
            "previous_reasons": previous_reasons,
        },
        "unexplained": storage.difference_view(
            comparable=comparable,
            free_before=reports.round_free_bytes(
                conn, previous["round_id"], container_id=container_id)
            if previous else None,
            free_after=reports.round_free_bytes(
                conn, latest["round_id"], container_id=container_id)
            if latest else None,
            # 目录测量侧是 KB（snapshots.total_kb），容量侧是 bytes；
            # 差额必须同单位，显式换算到 bytes，绝不混算。
            measured_before=(_kb_to_bytes(prev_attribution["measured_kb"])
                             if prev_attribution
                             and prev_attribution.get("measured_kb") is not None
                             else None),
            measured_after=(_kb_to_bytes(attribution["measured_kb"])
                            if attribution
                            and attribution.get("measured_kb") is not None
                            else None),
            reason=block_reason if (latest and previous)
            else "只有一个轮次，缺同计划前一轮可比基线。",
        ) if (latest and previous) else storage.difference_view(
            comparable=False, free_before=None, free_after=None,
            measured_before=None, measured_after=None,
            reason="尚无完整轮次对（需同主体、同计划的两轮）。"),
    }
    return payload


def _kb_to_bytes(kb: int | None) -> int | None:
    """KB → bytes（显式换算；None 透传，不用 0 冒充缺失）。"""
    return None if kb is None else int(kb) * 1024


def _capacity_sample_rows(conn: sqlite3.Connection, *, limit: int,
                          container_id: str | None = None) -> list[dict]:
    """读最近的**所选容器**容量样本（时间正序；limit 只截最新端）。

    ISS-157 返修 R2：``container_capacity_samples`` 无外键，可混有其他容器
    的样本。若全局取最近 N 条，别容器的较新样本会把当前容器的样本挤出窗口
    （甚至让读数整体落到别的容器上）。故先按 ``container_id`` 绑定主体再
    截断；无容器身份时不猜主体，返回空窗口（下游如实报「没有容量样本」）。

    与既有序列查询同一处 AUD-09 修正：先 DESC 取最新 N 条再正序输出，
    避免「取最早 N 条、最新点被截掉」。
    """
    if not container_id:
        return []
    rows = conn.execute(
        "SELECT id, container_id, total_bytes, free_bytes, source, "
        "sampled_at, round_id FROM container_capacity_samples "
        "WHERE container_id=? "
        "ORDER BY sampled_at DESC, id DESC LIMIT ?", (container_id, limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _plan_preview(selection: config.ScopeSelection) -> dict:
    """把已校验的范围选择解析为「下一轮计划」预览（不落盘、不扫描）。

    预览给出范围根与其稳定身份、读限（每范围 du 时限）、资源预算
    （min_kb / 排除掩码）和 identity 版本——即任务卡要求的「计划根 + 读取
    限制 + 排除/资源预算 + identity 版本」。此处**不**打开数据库、不登记
    scan_scopes/scan_plans：登记只发生在真实扫描轮次（ISS-154）。
    """
    from . import scan_coordinator, scanner
    pinned = scanner.PinnedScanConfig.capture(
        metric_version=scan_coordinator.METRIC_VERSION)
    plans = []
    for index, root in enumerate(selection.roots):
        scope_id = selection.scope_ids[index] if index < len(selection.scope_ids) \
            else scan_coordinator.derived_scope_id(root)
        plans.append({
            "scope_id": scope_id,
            "root": root,
            "display_name": Path(root).name or root,
            "plan_id": scanner.plan_identity_id(
                scope_id, root, int(pinned.metric_version),
                int(pinned.min_kb), pinned.exclude_names_canonical),
        })
    return {
        "mode": selection.mode,
        "container_id": selection.container_id,
        "identity_version": selection.identity_version,
        "revision": selection.revision,
        "plans": plans,
        "read_limits": {"du_timeout_s": pinned.du_timeout_s,
                        "per_root": True},
        "budget": {"min_kb": int(pinned.min_kb),
                   "exclude_names": pinned.exclude_names_canonical},
    }


@app.get("/api/storage/plan/preview")
def api_storage_plan_preview(mode: Optional[str] = Query(None),
                              roots: Optional[str] = Query(None)):
    """范围计划预览（只读）：校验给定选择并回显下一轮计划，不保存任何东西。

    - 未显式启用范围能力（``enabled=False``）时只回显当前生效选择与「旧口径
      单根」提示，不猜测、不替用户选择启动盘。
    - 给定 ``mode``/``roots``（分号分隔）时按候选预览，供 UI「预览→保存」；
      校验失败 400 且不落盘。
    """
    view = config.scope_settings_view()
    if not mode and not roots:
        current = config.effective_scope_selection()
        return {"enabled": view["enabled"], "identity_version":
                view["identity_version"],
                "selection": view["selection"],
                "plan": None if current is None else _plan_preview(current),
                "hint": ("未启用范围能力：扫描仍按旧单根口径运行；"
                         "选择范围只改下一轮计划。")}
    candidate = _scope_candidate_from_query(mode, roots)
    return {"enabled": view["enabled"],
            "identity_version": view["identity_version"],
            "selection": view["selection"],
            "plan": _plan_preview(candidate),
            "hint": "预览不改任何配置；保存只影响下一轮计划，不触发扫描。"}


def _scope_candidate_from_query(mode: Optional[str],
                                roots: Optional[str]) -> config.ScopeSelection:
    """把查询参数装配成候选范围选择（fail-closed 校验）。"""
    if not mode:
        raise HTTPException(400, "预览候选必须显式给出 mode")
    raw_roots = [item for item in (roots or "").split(";") if item]
    try:
        fields = config._validated_storage_scope(  # noqa: SLF001 - 同包内共用校验层
            {"mode": mode, "roots": raw_roots}, partial=False)
        return config.ScopeSelection(
            mode=str(fields["mode"]),
            roots=tuple(fields.get("roots", ())),  # type: ignore[arg-type]
            identity_version=config.SCOPE_IDENTITY_VERSION)
    except config.ConfigurationError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/storage/scope")
async def api_storage_scope_put(request: Request):
    """保存范围选择（需写令牌；原子写 + 版本冲突保护；不触发扫描）。

    请求体 ``{"mode": ..., "roots": [...], "scope_ids": [...],
    "container_id": ..., "expected_revision": int}``。

    - ``expected_revision`` 与当前生效版本不一致 → **409** 且旧值不动
      （预览→保存之间的并发改动必须显式重预览）；
    - 校验失败 400、写失败 500，两者都不产生半更新状态；
    - 服务自身不注册/不重载 launchd、不触发扫描。
    """
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "请求体必须是合法 JSON 对象")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    unknown = sorted(set(body) - {
        "mode", "roots", "scope_ids", "container_id", "expected_revision",
        "selected_at"})
    if unknown:
        raise HTTPException(400, f"请求体含未知字段：{', '.join(unknown)}")
    expected = body.get("expected_revision")
    if expected is not None and (isinstance(expected, bool)
                                 or not isinstance(expected, int)
                                 or expected < 0):
        raise HTTPException(400, f"expected_revision 必须是非负整数：{expected!r}")
    payload = {key: value for key, value in body.items()
               if key != "expected_revision"}
    try:
        saved = config.save_scope_selection(
            config._validated_storage_scope_whole(payload),  # noqa: SLF001
            expected_revision=expected)
    except config.ScopeVersionConflict as exc:
        raise HTTPException(409, str(exc))
    except config.ConfigurationError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"settings.json 写入失败（旧文件未改动）：{exc}")
    return {
        "applied": True,
        "triggers_scan": False,
        "scope": config.scope_settings_view(),
        "plan": _plan_preview(saved),
        "hint": ("范围选择已保存，仅影响下一轮计划；扫描需另行显式触发"
                 "（旧 HOME 历史快照保留为 legacy 口径，不与新基线混比）。"),
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
    except scan_coordinator.UpgradeWriteStopError as exc:
        # ISS-097 停写条件：升级事务进行中（journal 在位），拒绝开始写入。
        _scan_lock.release()
        return JSONResponse(
            {"ok": False, "message": str(exc), "upgrade": {
                "phase": exc.journal.get("phase"),
                "txn_id": exc.journal.get("txn_id"),
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
            runs = []
            for r in rows:
                run = dict(r)
                # ISS-109：done 行的 message 在 DB 是 result JSON，展示层转人话摘要
                run["message"] = scan_coordinator.humanize_run_message(
                    run.get("message"), status=run.get("status"),
                    snapshot_id=run.get("snapshot_id"),
                    report_status=run.get("report_status"),
                    notification_status=run.get("notification_status"),
                    pruned_count=run.get("pruned_count"),
                )
                runs.append(run)
            state["runs"] = runs
        return state
    finally:
        conn.close()


# ---------- 权限状态（ISS-111：设置「权限」分区唯一数据源） ----------

def _fda_scandir(path: Path) -> int:
    """对 FDA 探测路径做一次只读目录列举，返回条目数。

    独立成函数仅为契约测试可注入（真实 os.scandir 无法在隔离环境稳定
    构造 PermissionError）；生产路径不做任何注入。
    """
    with os.scandir(path) as it:
        return sum(1 for _ in it)


def _probe_fda_status() -> dict:
    """完全磁盘访问（TCC）只读探测：对已知受保护目录列举一次。

    三态合同（不伪造）：
    - granted：能列出条目——当前进程可读取该受保护位置；
    - denied ：PermissionError——macOS TCC 拦截只读列举的典型表现；
    - unknown：其余任何异常（路径不存在/IO 错误/平台差异），如实上报
      异常类别，不猜成 granted/denied。
    探测是纯只读 os.scandir：无写入、无提权、不调用系统设置；本应用
    不代改系统权限（macOS 也不允许应用自提权）。
    """
    probe_path = Path.home() / "Library" / "Containers"
    try:
        entries = _fda_scandir(probe_path)
        return {"status": "granted", "probe_path": str(probe_path), "entries": entries}
    except PermissionError:
        return {"status": "denied", "probe_path": str(probe_path), "entries": None}
    except Exception as exc:  # noqa: BLE001 - 任何探测异常都降级 unknown，不让端点 5xx
        return {"status": "unknown", "probe_path": str(probe_path), "entries": None,
                "detail": type(exc).__name__}


@app.get("/api/permissions")
def api_permissions():
    """权限状态总览（ISS-111）：设置页「权限」分区数据源。

    - fda：TCC 保护路径只读探测三态（见 _probe_fda_status）；
    - notification：最近一次扫描运行的 notification_status（scan_runs /
      scan_run_details 既有数据原样转出，无记录为 null=未登记，不推断）；
    - coverage：最近快照的受限/消失计数引用（ISS-091 数据迁入「权限」
      分区）；只引用计数与快照元数据，占比由前端按 ISS-095 口径计算。
    Host/Origin/写边界约束由 local_boundary_guard 全局中间件统一施加，
    与 /api/status 完全相同（GET 只读，无需写令牌）。深链 URL 不经 API
    下发：由前端持有（与 ISS-002A/091 既有 opener 常量同源）。
    """
    conn = _get_conn()
    try:
        notif = conn.execute(
            "SELECT r.id AS run_id, r.status AS run_status, r.finished_at, "
            "d.notification_status FROM scan_runs r "
            "LEFT JOIN scan_run_details d ON d.run_id = r.id "
            "ORDER BY r.id DESC LIMIT 1"
        ).fetchone()
        latest = _latest_snapshots(conn, 1)
        snap = latest[0] if latest else None
        return {
            "fda": _probe_fda_status(),
            "notification": {
                "run_id": notif["run_id"] if notif else None,
                "run_status": notif["run_status"] if notif else None,
                "finished_at": notif["finished_at"] if notif else None,
                "notification_status": notif["notification_status"] if notif else None,
            },
            "coverage": {
                "snapshot_id": snap["id"] if snap else None,
                "created_at": snap["created_at"] if snap else None,
                "dir_count": snap["dir_count"] if snap else None,
                "denied_count": snap["denied_count"] if snap else None,
                "vanished_count": snap["vanished_count"] if snap else None,
                "confirmed_missing_count": snap["confirmed_missing_count"] if snap else None,
                "path_unverified_count": snap["path_unverified_count"] if snap else None,
            },
        }
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


def _shutdown_analysis() -> None:
    """helper 正常退出回收自有分析（ISS-035B）：cancel + 有界等待终态。

    异常退出（SIGKILL 等）不依赖本钩子：runner 的父进程存活看门使 CLI
    子任务有界退出；遗留 running 行由下次启动 reconcile 标 interrupted。"""
    manager = _ANALYSIS_MANAGER
    if manager is not None:
        try:
            manager.shutdown()
        except Exception:  # noqa: BLE001 - 退出路径不因回收失败而挂起
            logger.exception("analysis 关闭回收失败")


app.router.add_event_handler("shutdown", _shutdown_scan)
app.router.add_event_handler("shutdown", _shutdown_analysis)


# ---------- v0.2：目录浏览器 / 日报档案 / Finder 打开 ----------

@app.get("/api/browse")
def api_browse(
    path: str | None = None,
    snapshot_id: int | None = None,
    cursor: str | None = None,
    limit: int = Query(hierarchy.CHILDREN_DEFAULT_LIMIT, ge=1,
                       le=hierarchy.CHILDREN_MAX_LIMIT),
):
    """目录浏览器：指定目录的直接子目录 + 与前一可比快照的差值 + 自身趋势。

    对应 DESIGN.md 分布页合同：面包屑下钻 + 行级 Finder 打开。两种形态由
    snapshot_id 区分（ISS-159）：

    - 旧调用（无 snapshot_id，兼容）：恒绑定最新快照与其同数据集前驱，
      响应形态与差值口径不变；差值基线取同数据集前驱（与日报/diff 同一
      选择逻辑），没有可比基线时 delta_kb 为 null——无基线不伪造"增长"
      （AUD-04）。
    - 显式快照（ISS-159）：分布主读数只指所选快照——路径按该快照 root
      约束；行状态 measured（直接入库记录）/ structural（仅已记录后代、
      可导航，size 为 null 不填 0）；与前一可比快照的差分仅为次级且基线
      区间显式可见（comparison 携带基线快照 id/时点；单快照或前驱跨口径
      时为 null——差分未知，不冒充基线）；直属子目录稳定分页（游标绑定
      快照/路径，未展示分页不得当作无子目录）；质量字段（采集状态、
      消失/拒绝计数）随所选快照返回，不与最新混用；趋势点带 snapshot_id
      （历史序列明确非本快照专属）。快照不存在 → 404（可能已被保留策略
      淘汰）；路径无直接记录也无任何已记录后代 → 404，不冒充空目录。
    """
    conn = _get_conn()
    try:
        if snapshot_id is None:
            return _browse_legacy(conn, path)

        snap = conn.execute(
            "SELECT * FROM snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if snap is None:
            raise HTTPException(
                404,
                f"快照 {snapshot_id} 不存在（可能已被保留策略淘汰），请改选其他快照")
        # 路径按所选快照的身份约束：多卷/同路径不同身份各读各的事实。
        root_path = snap["root"].rstrip("/") or "/"
        try:
            target = hierarchy.normalize_target(path, root_path)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        offset = 0
        if cursor is not None:
            payload = hierarchy.decode_cursor(cursor)
            if payload is None:
                raise HTTPException(400, "游标无效：无法解码")
            if (not hierarchy.cursor_matches_snapshot(
                    payload, s=snapshot_id, path=target, sort="size")
                    or hierarchy.cursor_offset(payload) is None):
                raise HTTPException(
                    400, "游标与当前查询参数（快照/路径）不匹配，"
                         "请从首页重新请求")
            offset = payload["offset"]

        collected = hierarchy.collect_children_snapshot(conn, snapshot_id, target)
        if not collected["children"] and collected["parent_size"] is None:
            raise HTTPException(
                404,
                f"路径 {target} 在快照 {snapshot_id} 中无记录"
                "（可能低于入库阈值、权限受限或路径不存在）",
            )

        # 次级差分：仅与同数据集前驱可比，基线区间显式可见；无前驱或前驱
        # 跨口径（不同数据集）时 comparison 为 null——差分未知。
        predecessor = reports.find_same_dataset_predecessor(conn, snapshot_id)
        comparison = None
        prev_sizes: dict[str, int] = {}
        prev_parent: int | None = None
        if predecessor is not None:
            comparison = {"snapshot_id": predecessor["id"],
                          "created_at": predecessor["created_at"]}
            prev = hierarchy.collect_children_snapshot(
                conn, predecessor["id"], target)
            prev_sizes = {name: n.size_kb
                          for name, n in prev["children"].items()
                          if n.size_kb is not None}
            prev_parent = prev["parent_size"]

        rows = hierarchy.sort_snapshot_rows(list(collected["children"].values()))
        total = len(rows)
        page = rows[offset:offset + limit]
        has_more = offset + limit < total
        next_cursor = None
        if has_more:
            next_cursor = hierarchy.encode_cursor({
                "v": 2, "s": snapshot_id, "path": target,
                "sort": "size", "offset": offset + limit,
            })

        def _secondary(size_kb: int | None, name: str | None = None):
            """次级差分读数：两侧都有直接测量才可比；基线缺测不冒充 0。"""
            if comparison is None or size_kb is None:
                return None, None
            old = prev_sizes.get(name) if name is not None else prev_parent
            if old is None:
                return None, True   # 前驱无直接记录：以 is_new 表达首次入库
            return size_kb - old, False

        parent_delta, _ = _secondary(collected["parent_size"])
        children_out = []
        for node in page:
            delta, is_new = _secondary(node.size_kb, node.name)
            row = node.to_dict()
            row["delta_kb"] = delta
            row["is_new"] = bool(is_new)
            children_out.append(row)

        return {
            "path": target,
            "size_kb": collected["parent_size"],
            "status": "measured" if collected["parent_size"] is not None
                      else "structural",
            "delta_kb": parent_delta,
            "children": children_out,
            "trend": _trend_points(conn, target, snap),
            "crumbs": _browse_crumbs(root_path, target),
            "snapshot_at": snap["created_at"],
            "snapshot": {
                "id": snap["id"],
                "created_at": snap["created_at"],
                "root": snap["root"],
                "min_kb": snap["min_kb"],
                "exclude_names": snap["exclude_names"] or "",
                "collection_status": snap["collection_status"],
                "vanished_count": snap["vanished_count"],
                "denied_count": snap["denied_count"],
            },
            "comparison": comparison,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(page),
                "total": total,
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
        }
    finally:
        conn.close()


def _browse_legacy(conn, path: str | None):
    """旧形态（无 snapshot_id）：恒绑定最新快照与其同数据集前驱。

    行为自 ISS-024 起不变，本卡不改动其任何读数与响应键；历史实点由
    显式 snapshot_id 形态承接（ISS-159）。
    """
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

    return {
        "path": target,
        "size_kb": new_entries.get(target, 0),
        "delta_kb": (new_entries.get(target, 0) - old_entries.get(target, 0))
                    if old_sid and target in old_entries else None,
        "children": children,
        "trend": points,
        "crumbs": _browse_crumbs(root_path, target),
        "snapshot_at": snaps[0]["created_at"],
    }


def _trend_points(conn, target: str, snap) -> list[dict]:
    """所选快照同数据集内该路径的已记录点（时间正序，点带快照身份）。

    与旧行为同一「记录点」口径（缺测窗口由 /api/trend 锚定形态承接），
    数据集身份统一走 reports 辅助（legacy/plan 分档），每点追加
    snapshot_id：趋势是历史序列，明确不是所选快照的专属读数。
    """
    size_by_sid = {
        r["snapshot_id"]: r["size_kb"]
        for r in conn.execute(
            "SELECT snapshot_id, size_kb FROM entries WHERE path = ?", (target,))
    }
    # find_same_dataset_snapshot_rows 恒为时间正序（reports 合同），直接输出。
    rows = reports.find_same_dataset_snapshot_rows(
        conn, reports.dataset_identity(snap))
    return [
        {"snapshot_id": row["id"], "created_at": row["created_at"],
         "size_kb": size_by_sid[row["id"]]}
        for row in rows if row["id"] in size_by_sid
    ]


def _browse_crumbs(root_path: str, target: str) -> list[dict]:
    """面包屑：数据集根 → … → 当前路径（root=/ 时根名为 "/"）。"""
    parts = (target[len(root_path):].strip("/").split("/")
             if target != root_path else [])
    crumbs = [{"name": root_path.rsplit("/", 1)[-1] or "/", "path": root_path}]
    acc = root_path
    for seg in filter(None, parts):
        acc = acc + "/" + seg
        crumbs.append({"name": seg, "path": acc})
    return crumbs


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

    # ISS-155：范围启用后，reveal 只放行**已选择的规范根**内路径；未选中的
    # 卷/发现结果一律不放行（发现 ≠ 已授权）。未启用时回落旧单根口径，
    # 行为逐字节不变。
    selection = config.effective_scope_selection()
    allowed_roots = ([str(root) for root in selection.roots]
                     if selection is not None
                     else [str(config.DEFAULT_ROOT).rstrip("/") or "/"])
    if not any(raw == root or raw.startswith(root + "/")
               for root in allowed_roots):
        # 字符串层先拒绝相对路径与前缀同名根（/scanroot-evil 不是 /scanroot）
        if selection is None:
            # 未启用范围能力：文案与边界逐字节保持旧单根口径（ISS-022 合同）
            raise HTTPException(400, f"路径必须是监控根 {allowed_roots[0]} 之内的绝对路径")
        listed = "、".join(allowed_roots)
        raise HTTPException(400, f"路径必须是已选择范围 {listed} 之内的绝对路径")

    try:
        resolved = Path(raw).resolve()
        root_reals = {Path(root).resolve() for root in allowed_roots}
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(400, "路径无法规范化")
    if not any(resolved == root_real or root_real in resolved.parents
               for root_real in root_reals):
        # 规范化层拒绝 .. 折叠与符号链接越界（逐个已选根判定，ISS-155）
        raise HTTPException(400, "路径规范化后位于已选范围之外，已拒绝")
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
