"""FastAPI 服务层：只读查询 + 手动扫描触发。

API 清单（自动文档见 http://127.0.0.1:7952/docs）：
- GET  /api/status           状态总览（磁盘、快照数、DB 大小、最近扫描）
- GET  /api/snapshots        快照列表（含卷容量）
- GET  /api/volume-trend     卷容量趋势序列
- GET  /api/trees            某快照的目录树（旭日图数据）
- GET  /api/diff             两快照差分
- GET  /api/trend?path=      单目录历史大小序列
- GET  /api/bigfiles         近期大文件
- POST /api/scan             触发手动扫描（后台执行）
- GET  /api/scan/status      查询扫描任务状态
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import bigfiles, config, db, reports, scanner

app = FastAPI(title="Disk Sentinel", version="0.1.0")

_scan_lock = threading.Lock()
_scan_state: dict = {"running": False, "started_at": None, "finished_at": None,
                     "result": None, "error": None}

# 树接口单次返回的目录上限（防止极端情况下响应过大）
TREE_MAX_NODES = 20000


def _get_conn() -> sqlite3.Connection:
    return db.connect()


def _latest_snapshots(conn: sqlite3.Connection, n: int = 2) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshots ORDER BY created_at DESC, id DESC LIMIT ?", (n,)
    ).fetchall()


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
            "scan": dict(_scan_state),
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

    def _run():
        _scan_state.update(running=True, started_at=dt.datetime.now().isoformat(timespec="seconds"),
                           finished_at=None, result=None, error=None)
        conn = None
        try:
            conn = _get_conn()
            sid = scanner.create_snapshot(conn)
            report_path = reports.write_daily_report(conn, sid)
            pruned = scanner.prune_snapshots(conn)
            _scan_state.update(running=False,
                               finished_at=dt.datetime.now().isoformat(timespec="seconds"),
                               result={"snapshot_id": sid, "report": str(report_path),
                                       "pruned": pruned})
        except Exception as exc:  # noqa: BLE001 - 状态需如实回传前端
            _scan_state.update(running=False,
                               finished_at=dt.datetime.now().isoformat(timespec="seconds"),
                               error=str(exc))
        finally:
            if conn is not None:
                conn.close()
            _scan_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "扫描已启动"}


@app.get("/api/scan/status")
def api_scan_status():
    return dict(_scan_state)


# 前端静态资源挂载在最后，避免覆盖 /api 路由
if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
