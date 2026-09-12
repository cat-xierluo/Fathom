"""命令行入口。

用法（main.py <子命令>）：
  scan       执行一次扫描 + 生成日报 + 清理旧快照（launchd 每日调用）
  report     对比最近两个快照，输出 Markdown 到 stdout
  bigfiles   近期大文件清单（stdout）
  status     状态总览
  serve      启动 Web 服务（launchd 常驻）
  install    安装 launchd 定时任务与常驻 Web 服务
  uninstall  卸载 launchd 任务
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import bigfiles, config, db, launchd, reports, scanner


def cmd_scan(args: argparse.Namespace) -> int:
    config.ensure_runtime_dirs()
    conn = db.connect()
    try:
        root = Path(args.root).expanduser() if args.root else config.DEFAULT_ROOT
        print(f"开始扫描 {root} ……（1100 万文件量级可能需要 5-15 分钟）")
        sid = scanner.create_snapshot(conn, root)
        pruned = scanner.prune_snapshots(conn)
        snap = conn.execute("SELECT * FROM snapshots WHERE id = ?", (sid,)).fetchone()
        print(
            f"完成：快照 #{sid}，目录 {snap['dir_count']} 个"
            f"（无权限 {snap['denied_count']}），总量 {snap['total_kb'] // 1024 // 1024} GB"
        )
        if pruned:
            print(f"已清理 {pruned} 个过期快照")
        try:
            path = reports.write_daily_report(conn, sid)
            print(f"日报已生成：{path}")
        except ValueError:
            print("当前只有 1 个快照，明天此时可生成首份对比日报")
    finally:
        conn.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        snaps = conn.execute(
            "SELECT * FROM snapshots ORDER BY created_at DESC, id DESC LIMIT 2"
        ).fetchall()
        if len(snaps) < 2:
            print("至少需要两个快照才能对比", file=sys.stderr)
            return 1
        new_meta, old_meta = snaps[0], snaps[1]
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
    finally:
        conn.close()
    return 0


def cmd_serve(_: argparse.Namespace) -> int:
    import uvicorn

    config.ensure_runtime_dirs()
    uvicorn.run(
        "fathom.api:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        reload=False,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fathom", description="Fathom ：目录大小历史追踪")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="扫描一次并生成日报")
    p.add_argument("--root", help="扫描根路径（默认 $HOME）")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("report", help="对比最近两个快照输出日报")
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
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
