"""近期大文件查询：find 近 N 天修改过的大文件。

用 mtime 近似"新增/最近写入"——对"哪个文件夹冒出来了"的定位来说足够。
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

from . import config


def find_big_files(
    root: Path | None = None,
    days: int | None = None,
    min_mb: int | None = None,
    topn: int = 30,
) -> list[dict]:
    """返回近 N 天 mtime 的大文件列表，按大小降序。

    [{path, size, mtime}]，size 为字节，mtime 为 'YYYY-MM-DD HH:MM'。
    """
    root = Path(root) if root else config.DEFAULT_ROOT
    days = config.BIGFILE_DEFAULT_DAYS if days is None else days
    min_mb = config.BIGFILE_DEFAULT_MB if min_mb is None else min_mb
    min_bytes = min_mb * 1024 * 1024

    proc = subprocess.run(
        [
            "/usr/bin/find",
            str(root),
            "-xdev",                       # 不跨挂载点，与 du -x 一致
            "-type", "f",
            "-size", f"+{min_bytes}c",
            "-mtime", f"-{days}",
            "-print0",
        ],
        capture_output=True,
        text=False,
    )

    out: list[dict] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        path = os.fsdecode(raw)
        try:
            st = os.stat(path)
        except OSError:
            continue
        mtime = dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        out.append({"path": path, "size": st.st_size, "mtime": mtime})
    out.sort(key=lambda x: x["size"], reverse=True)
    return out[:topn]
