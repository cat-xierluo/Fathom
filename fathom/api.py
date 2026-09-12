"""FastAPI 服务层：只读查询 + 手动扫描触发。

API 清单（自动文档见 http://127.0.0.1:7952/docs）：
- GET  /api/status           状态总览（磁盘、快照数、DB 大小、最近扫描）
- GET  /api/snapshots        快照列表（含卷容量）
- GET  /api/volume-trend     卷容量趋势序列
- GET  /api/trees            某快照的目录树（旭日图数据）
- GET  /api/diff             两快照差分
- GET  /api/trend?path=      单目录历史大小序列
- GET  /api/bigfiles         近期大文件
- POST /api/scan             触发手动扫描（后台执行，状态入 scan_runs 表）
- GET  /api/scan/status      查询扫描任务状态（?history=N 附最近 N 条记录）
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import bigfiles, config, db, reports, scanner

app = FastAPI(title="Fathom", version="0.2.0")

# Tauri 壳的 loader 页（tauri://localhost）需要跨域探测本服务
app.add_middleware(
    CORSMiddleware,
    allow_origins=["tauri://localhost", "http://tauri.localhost"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_scan_lock = threading.Lock()

# 扫描状态持久化（ISS-007）：三态入 scan_runs 表，/api/scan/status 与 /api/status
# 改查表，服务重启后状态不丢。陈旧 running 判定：du 全盘实测 5-15 分钟（DEC-002），
# 超过 1 小时仍未结束即视为上次进程中断，在状态读取时收尾为 failed。
SCAN_STALE_SECONDS = 3600

# 树接口单次返回的目录上限（防止极端情况下响应过大）
TREE_MAX_NODES = 20000


def _get_conn() -> sqlite3.Connection:
    return db.connect()


def _latest_snapshots(conn: sqlite3.Connection, n: int = 2) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshots ORDER BY created_at DESC, id DESC LIMIT ?", (n,)
    ).fetchall()


def _finalize_running_runs(
    conn: sqlite3.Connection, max_age_seconds: float | None = None
) -> int:
    """把遗留的 running 记录收尾为 failed，返回收尾条数（不提交，由调用方 commit）。

    - max_age_seconds=None：收尾全部。仅限调用方已持有 _scan_lock 的路径——
      锁在手即无存活扫描线程，表中 running 必属上次进程；
    - 否则只收尾 started_at 早于该时长的（状态读取路径的超时判定）。
    """
    now = dt.datetime.now().isoformat(timespec="seconds")
    if max_age_seconds is None:
        cur = conn.execute(
            "UPDATE scan_runs SET status='failed', finished_at=?, "
            "message='服务退出，扫描中断' WHERE status='running'",
            (now,),
        )
    else:
        cutoff = (
            dt.datetime.now() - dt.timedelta(seconds=max_age_seconds)
        ).isoformat(timespec="seconds")
        cur = conn.execute(
            "UPDATE scan_runs SET status='failed', finished_at=?, message=? "
            "WHERE status='running' AND started_at <= ?",
            (now, f"扫描超时：超过 {int(max_age_seconds) // 60} 分钟未完成，视为服务中断", cutoff),
        )
    return cur.rowcount


def _latest_scan_state(conn: sqlite3.Connection) -> dict:
    """scan_runs 最新一条 -> 扫描状态（原内存态形状，新增 id/status 字段）。

    本进程持有 _scan_lock 时扫描确在进行，running 如实上报；锁空闲而表中仍有
    running 属上次进程遗留，超过 SCAN_STALE_SECONDS 即收尾（重启后"如实报告或
    超时收尾"的判定就在这里）。
    """
    if not _scan_lock.locked():
        if _finalize_running_runs(conn, SCAN_STALE_SECONDS):
            conn.commit()
    row = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"id": None, "status": None, "running": False, "started_at": None,
                "finished_at": None, "result": None, "error": None}
    result, error = None, None
    if row["status"] == "done":
        if row["message"]:
            try:
                result = json.loads(row["message"])
            except ValueError:
                result = None  # 异常数据不让状态接口 500
    elif row["status"] == "failed":
        error = row["message"]
    return {
        "id": row["id"],
        "status": row["status"],
        "running": row["status"] == "running",
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "result": result,
        "error": error,
    }


def _set_run_status(run_id: int, status: str, message: str | None) -> None:
    """更新一条 scan_runs 的结束态（done/failed），独立连接，供后台线程收尾。"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE scan_runs SET status=?, message=?, finished_at=? WHERE id=?",
            (status, message, dt.datetime.now().isoformat(timespec="seconds"), run_id),
        )
        conn.commit()
    finally:
        conn.close()


@app.get("/api/status")
def api_status():
    conn = _get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
        latest = _latest_snapshots(conn, 1)
        latest_row = dict(latest[0]) if latest else None
        st = os.statvfs(config.DEFAULT_ROOT)
        db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
        return {
            "root": str(config.DEFAULT_ROOT),
            "snapshot_count": count,
            "latest_snapshot": latest_row,
            "disk": {
                "total_bytes": st.f_blocks * st.f_frsize,
                "free_bytes": st.f_bavail * st.f_frsize,
            },
            "db_bytes": db_size,
            "scan": _latest_scan_state(conn),
            "port": config.PORT,
        }
    finally:
        conn.close()


@app.get("/api/snapshots")
def api_snapshots():
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT s.id, s.created_at, s.root, s.total_kb, s.dir_count, s.denied_count,
                      v.total_bytes, v.free_bytes
               FROM snapshots s LEFT JOIN volume_stats v ON v.snapshot_id = s.id
               ORDER BY s.created_at DESC, s.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/volume-trend")
def api_volume_trend(limit: int = Query(120, ge=2, le=2000)):
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT s.created_at, v.total_bytes, v.free_bytes
               FROM snapshots s JOIN volume_stats v ON v.snapshot_id = s.id
               ORDER BY s.created_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/trees")
def api_trees(snapshot_id: int | None = None, min_kb: int = 51200):
    """目录树（旭日图/矩形树图数据），默认最新快照、>=50MB 的目录。"""
    conn = _get_conn()
    try:
        if snapshot_id is None:
            latest = _latest_snapshots(conn, 1)
            if not latest:
                return {"snapshot_id": None, "root": None, "children": []}
            snapshot_id = latest[0]["id"]
        rows = conn.execute(
            "SELECT path, size_kb FROM entries WHERE snapshot_id = ? AND size_kb >= ? "
            "ORDER BY size_kb DESC",
            (snapshot_id, min_kb),
        ).fetchall()
        if not rows:
            raise HTTPException(404, f"快照 {snapshot_id} 不存在或无 >= {min_kb}KB 的目录")

        meta = conn.execute("SELECT root FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        root_path = meta["root"].rstrip("/")

        # 由扁平路径构建嵌套树；用字典登记路径 -> 节点。
        # du 的累计语义保证父目录大小 >= 子目录，因此浅层大目录几乎总在保留集内；
        # 个别中间层被 TREE_MAX_NODES 截断时，其子孙直接挂到顶层（旭日图可正常下钻）。
        nodes: dict[str, dict] = {}
        for r in rows[:TREE_MAX_NODES]:
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

        return {"snapshot_id": snapshot_id, "root": root_path, "children": [to_list(top)]}
    finally:
        conn.close()


@app.get("/api/diff")
def api_diff(
    a: int | None = None,
    b: int | None = None,
    topn: int = Query(25, ge=1, le=100),
):
    """对比两个快照；默认 a=倒数第二个, b=最新。返回 b 相对 a 的变化。"""
    conn = _get_conn()
    try:
        if b is None or a is None:
            snaps = _latest_snapshots(conn, 2)
            if len(snaps) < 2:
                raise HTTPException(409, "至少需要两个快照才能对比")
            b, a = snaps[0]["id"], snaps[1]["id"]
        for sid in (a, b):
            if not conn.execute("SELECT 1 FROM snapshots WHERE id=?", (sid,)).fetchone():
                raise HTTPException(404, f"快照 {sid} 不存在")
        old, new = reports.load_snapshot(conn, a), reports.load_snapshot(conn, b)
        diff = reports.compute_diff(old, new, topn=topn)
        def ser(items): return [
            {"path": c.path, "old_kb": c.old_kb, "new_kb": c.new_kb, "delta_kb": c.delta_kb}
            for c in items
        ]
        meta = {sid: dict(conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone())
                for sid in (a, b)}
        return {"a": meta[a], "b": meta[b],
                "grown": ser(diff["grown"]), "shrunk": ser(diff["shrunk"]),
                "added": ser(diff["added"]), "removed": ser(diff["removed"])}
    finally:
        conn.close()


@app.get("/api/trend")
def api_trend(path: str, limit: int = Query(120, ge=2, le=2000)):
    """单目录历史大小序列。"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT s.created_at, e.size_kb FROM entries e
               JOIN snapshots s ON s.id = e.snapshot_id
               WHERE e.path = ? ORDER BY s.created_at ASC LIMIT ?""",
            (path, limit),
        ).fetchall()
        return {"path": path, "points": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/bigfiles")
def api_bigfiles(days: int = Query(7, ge=1, le=90),
                 min_mb: int = Query(100, ge=1, le=10240),
                 topn: int = Query(50, ge=1, le=200)):
    return {"files": bigfiles.find_big_files(days=days, min_mb=min_mb, topn=topn)}


@app.post("/api/scan")
def api_scan():
    if not _scan_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "message": "已有扫描在进行中"}, status_code=409)

    conn = None
    try:
        conn = _get_conn()
        # 已持有锁 -> 表中残留的 running 必属上次进程，收尾与本次启动同事务提交
        _finalize_running_runs(conn)
        cur = conn.execute(
            "INSERT INTO scan_runs(started_at, status) VALUES (?, 'running')",
            (dt.datetime.now().isoformat(timespec="seconds"),),
        )
        run_id = cur.lastrowid
        conn.commit()
    except Exception:
        _scan_lock.release()
        raise
    finally:
        if conn is not None:
            conn.close()

    def _run():
        conn = None
        try:
            conn = _get_conn()
            sid = scanner.create_snapshot(conn)
            report_path = reports.write_daily_report(conn, sid)
            pruned = scanner.prune_snapshots(conn)
            _set_run_status(run_id, "done", json.dumps(
                {"snapshot_id": sid, "report": str(report_path), "pruned": pruned},
                ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 状态需如实回传前端
            try:
                _set_run_status(run_id, "failed", str(exc))
            except Exception:
                pass  # 收尾失败只丢状态记录，不掩盖扫描异常本身
        finally:
            if conn is not None:
                conn.close()
            _scan_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "扫描已启动", "run_id": run_id}


@app.get("/api/scan/status")
def api_scan_status(history: int = Query(0, ge=0, le=100)):
    """最新一次扫描状态；?history=N 附带最近 N 条运行记录（含失败）。"""
    conn = _get_conn()
    try:
        state = _latest_scan_state(conn)
        if history:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, status, message FROM scan_runs "
                "ORDER BY id DESC LIMIT ?",
                (history,),
            ).fetchall()
            state["runs"] = [dict(r) for r in rows]
        return state
    finally:
        conn.close()


# ---------- v0.2：目录浏览器 / 日报档案 / Finder 打开 ----------

@app.get("/api/browse")
def api_browse(path: str | None = None):
    """目录浏览器：指定目录的直接子目录（最新快照）+ 与前一快照的差值 + 自身趋势。

    对应 DESIGN.md 分布页合同：面包屑下钻 + 行级 Finder 打开。
    """
    conn = _get_conn()
    try:
        snaps = _latest_snapshots(conn, 2)
        if not snaps:
            raise HTTPException(409, "尚无快照，请先扫描")
        new_sid, old_sid = snaps[0]["id"], (snaps[1]["id"] if len(snaps) > 1 else None)
        root_path = snaps[0]["root"].rstrip("/")
        target = path.rstrip("/") if path else root_path
        if not (target == root_path or target.startswith(root_path + "/")):
            raise HTTPException(400, f"路径必须在监控根 {root_path} 之内")

        new_entries = reports.load_snapshot(conn, new_sid)
        old_entries = reports.load_snapshot(conn, old_sid) if old_sid else {}

        prefix = target + "/"
        children = []
        for p, size in new_entries.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if "/" not in rest:  # 直接子目录
                old_size = old_entries.get(p)
                children.append({
                    "name": rest,
                    "path": p,
                    "size_kb": size,
                    "delta_kb": (size - old_size) if old_size is not None
                                else (size if p not in old_entries else None),
                    "is_new": old_sid is not None and p not in old_entries,
                })
        children.sort(key=lambda c: c["size_kb"], reverse=True)

        points = [
            dict(r)
            for r in conn.execute(
                """SELECT s.created_at, e.size_kb FROM entries e
                   JOIN snapshots s ON s.id = e.snapshot_id
                   WHERE e.path = ? ORDER BY s.created_at""",
                (target,),
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
    if not (len(date) == 10 and date[4] == "-" and date[:4].isdigit()):
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    path = config.REPORTS_DIR / f"{date}.md"
    if not path.is_file():
        raise HTTPException(404, f"{date} 无日报")
    return {"date": date, "content": path.read_text(encoding="utf-8")}


@app.post("/api/reveal")
async def api_reveal(request: Request):
    """在 Finder 中显示指定路径（open -R）。仅允许监控根内的路径。"""
    body = await request.json()
    path = str(body.get("path", ""))
    root = str(config.DEFAULT_ROOT)
    if not (path == root or path.startswith(root + "/")):
        raise HTTPException(400, "路径必须在监控根之内")
    if not os.path.exists(path):
        raise HTTPException(404, "路径不存在（可能已被移动或删除）")
    result = subprocess.run(["/usr/bin/open", "-R", path], capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(500, f"打开失败：{result.stderr.strip()}")
    return {"ok": True}


# 前端静态资源挂载在最后，避免覆盖 /api 路由
if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
