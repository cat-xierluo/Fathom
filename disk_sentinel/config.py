"""disk-sentinel 全局配置。

所有路径、阈值、端口集中在此，便于调整与测试注入。
数据库路径可用环境变量 DISK_SENTINEL_DB 覆盖（冒烟测试/多实例场景）。
"""

import os
from pathlib import Path

# 项目根目录（disk_sentinel/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 运行时目录（均已 gitignore，见 DEC-004）
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOGS_DIR = PROJECT_ROOT / "logs"
DB_PATH = Path(os.environ.get("DISK_SENTINEL_DB") or (DATA_DIR / "disk.db"))
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# Web 服务
HOST = "127.0.0.1"
PORT = 7952

# 扫描
DEFAULT_ROOT = Path.home()          # 默认扫描整个用户主目录
MIN_DIR_KB = 10 * 1024              # 只记录 >= 10MB 的目录（DEC-005）

# 快照保留策略：近 35 天保留每日快照，更早的每周保留 1 份，最多 12 周（DEC-006）
KEEP_DAILY_DAYS = 35
KEEP_WEEKLY_WEEKS = 12

# 大文件查询默认值
BIGFILE_DEFAULT_DAYS = 7
BIGFILE_DEFAULT_MB = 100

# launchd
LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SCAN_LABEL = "com.maoscripts.disk-sentinel-scan"
WEB_LABEL = "com.maoscripts.disk-sentinel-web"
SCAN_HOUR = 12                      # 每日 12:00 扫描（避开开机早高峰）


def ensure_runtime_dirs() -> None:
    """创建运行时目录（幂等）。"""
    for d in (DATA_DIR, REPORTS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
