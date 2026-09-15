"""Fathom 运行配置。

所有可写路径从一个运行根派生，避免发行后把数据写进只读的
``.app``/PyInstaller 资源目录。环境变量是 helper、CLI 和 API 的共同入口：

``FATHOM_RUNTIME_MODE``
    ``development``（默认，运行根为源码根）或 ``release``（默认运行
    根为 ``~/Library/Application Support/Fathom``）。
``FATHOM_RUNTIME_DIR``
    显式运行根；``data/``、``reports/``、``logs/`` 全部由此派生。
``FATHOM_SCAN_ROOT`` / ``FATHOM_PORT``
    受监控根和回环 HTTP 端口。
``FATHOM_RESOURCE_DIR``
    只读应用资源根；默认为 PyInstaller ``_MEIPASS`` 或源码根。

``FATHOM_DB`` 仅作旧入口兼容：未指定运行根时，它的父目录成为运行
根，不再出现“只隔离 DB，其他仍写源码树”的半隔离状态。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import sys
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"


class ConfigurationError(ValueError):
    """运行配置无效；调用方应终止启动，不用隐式默认继续。"""


def _absolute_path(raw: str | os.PathLike[str], *, name: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{name} 必须是绝对路径：{path}")
    return path.resolve(strict=False)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _port(raw: str | int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"FATHOM_PORT 必须是整数：{raw!r}") from exc
    if not 1 <= value <= 65535:
        raise ConfigurationError(f"FATHOM_PORT 必须在 1..65535：{value}")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """单一运行配置快照；所有路径均为规范化绝对路径。"""

    mode: str
    project_root: Path
    home_dir: Path
    resource_dir: Path
    frontend_dir: Path
    runtime_dir: Path
    data_dir: Path
    reports_dir: Path
    logs_dir: Path
    db_path: Path
    scan_root: Path
    host: str
    port: int

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        project_root: Path = PROJECT_ROOT,
        home: Path | None = None,
    ) -> "RuntimeConfig":
        env = os.environ if environ is None else environ
        project_root = _absolute_path(project_root, name="project_root")
        home = _absolute_path(home or Path.home(), name="home")
        # 模式判定（ISS-029 G2）：显式 env 优先；冻结产物（sys.frozen）默认
        # release 以避免数据被写入只读 bundle；源码 dev 模式行为不变。
        raw_mode = env.get("FATHOM_RUNTIME_MODE")
        if raw_mode:
            mode = raw_mode.strip().lower()
        elif getattr(sys, "frozen", False):
            mode = "release"
        else:
            mode = "development"
        if mode not in {"development", "release"}:
            raise ConfigurationError(
                "FATHOM_RUNTIME_MODE 只能是 development 或 release"
            )

        legacy_db_raw = env.get("FATHOM_DB")
        runtime_raw = env.get("FATHOM_RUNTIME_DIR")
        if runtime_raw:
            runtime_dir = _absolute_path(runtime_raw, name="FATHOM_RUNTIME_DIR")
        elif legacy_db_raw:
            # 旧变量升级为完整隔离语义：所有运行写入都收敛到 DB 父目录。
            runtime_dir = _absolute_path(legacy_db_raw, name="FATHOM_DB").parent
        elif mode == "release":
            runtime_dir = home / "Library" / "Application Support" / "Fathom"
        else:
            runtime_dir = project_root

        data_dir = runtime_dir / "data"
        if legacy_db_raw:
            db_path = _absolute_path(legacy_db_raw, name="FATHOM_DB")
            reserved_directories = {
                runtime_dir, data_dir, runtime_dir / "reports", runtime_dir / "logs"
            }
            if db_path in reserved_directories or not _is_within(db_path, runtime_dir):
                raise ConfigurationError(
                    "FATHOM_DB 必须是 FATHOM_RUNTIME_DIR 内且不与"
                    f" runtime/data/reports/logs 目录冲突的文件路径：{db_path}"
                )
        else:
            db_path = data_dir / "fathom.db"

        frozen_resource = getattr(sys, "_MEIPASS", None)
        resource_dir = _absolute_path(
            env.get("FATHOM_RESOURCE_DIR") or frozen_resource or project_root,
            name="FATHOM_RESOURCE_DIR",
        )
        scan_root = _absolute_path(
            env.get("FATHOM_SCAN_ROOT") or home,
            name="FATHOM_SCAN_ROOT",
        )
        return cls(
            mode=mode,
            project_root=project_root,
            home_dir=home,
            resource_dir=resource_dir,
            frontend_dir=resource_dir / "frontend",
            runtime_dir=runtime_dir,
            data_dir=data_dir,
            reports_dir=runtime_dir / "reports",
            logs_dir=runtime_dir / "logs",
            db_path=db_path,
            scan_root=scan_root,
            host=HOST,
            port=_port(env.get("FATHOM_PORT", "7952")),
        )

    def with_overrides(
        self,
        *,
        runtime_dir: Path | str | None = None,
        scan_root: Path | str | None = None,
        port: int | str | None = None,
        resource_dir: Path | str | None = None,
        mode: str | None = None,
    ) -> "RuntimeConfig":
        target_mode = self.mode if mode is None else mode.strip().lower()
        if target_mode not in {"development", "release"}:
            raise ConfigurationError("mode 只能是 development 或 release")
        mode_changes_default = mode is not None and target_mode != self.mode
        if runtime_dir is not None:
            target_runtime = _absolute_path(runtime_dir, name="runtime_dir")
        elif mode_changes_default:
            target_runtime = (
                self.home_dir / "Library" / "Application Support" / "Fathom"
                if target_mode == "release"
                else self.project_root
            )
        else:
            target_runtime = self.runtime_dir
        target_resource = (
            self.resource_dir
            if resource_dir is None
            else _absolute_path(resource_dir, name="resource_dir")
        )
        # 运行根/模式未改时保留 from_env 已解析的兼容 DB 路径；
        # 否则 CLI 的空覆盖会意外丢掉 FATHOM_DB。
        preserve_paths = runtime_dir is None and not mode_changes_default
        data_dir = self.data_dir if preserve_paths else target_runtime / "data"
        reports_dir = self.reports_dir if preserve_paths else target_runtime / "reports"
        logs_dir = self.logs_dir if preserve_paths else target_runtime / "logs"
        db_path = self.db_path if preserve_paths else data_dir / "fathom.db"
        return replace(
            self,
            mode=target_mode,
            runtime_dir=target_runtime,
            data_dir=data_dir,
            reports_dir=reports_dir,
            logs_dir=logs_dir,
            db_path=db_path,
            scan_root=(
                self.scan_root
                if scan_root is None
                else _absolute_path(scan_root, name="scan_root")
            ),
            resource_dir=target_resource,
            frontend_dir=target_resource / "frontend",
            port=self.port if port is None else _port(port),
        )

    def public_values(self) -> dict[str, str | int]:
        """返回可供 API 查询的实际值（不含令牌或凭据）。"""
        return {
            "mode": self.mode,
            "runtime_dir": str(self.runtime_dir),
            "data_dir": str(self.data_dir),
            "reports_dir": str(self.reports_dir),
            "logs_dir": str(self.logs_dir),
            "db_path": str(self.db_path),
            "scan_root": str(self.scan_root),
            "resource_dir": str(self.resource_dir),
            "frontend_dir": str(self.frontend_dir),
            "host": self.host,
            "port": self.port,
        }


_ACTIVE = RuntimeConfig.from_env()


def _publish_compatibility_values(value: RuntimeConfig) -> None:
    """更新历史模块常量；生产配置真值仍是 ``_ACTIVE``。"""
    global DATA_DIR, REPORTS_DIR, LOGS_DIR, DB_PATH, FRONTEND_DIR, DEFAULT_ROOT, PORT
    DATA_DIR = value.data_dir
    REPORTS_DIR = value.reports_dir
    LOGS_DIR = value.logs_dir
    DB_PATH = value.db_path
    FRONTEND_DIR = value.frontend_dir
    DEFAULT_ROOT = value.scan_root
    PORT = value.port


_publish_compatibility_values(_ACTIVE)


def get_runtime_config() -> RuntimeConfig:
    return _ACTIVE


def configure(
    *,
    runtime_dir: Path | str | None = None,
    scan_root: Path | str | None = None,
    port: int | str | None = None,
    resource_dir: Path | str | None = None,
    mode: str | None = None,
) -> RuntimeConfig:
    """在打开任何运行资源前应用显式的进程级 CLI/helper 覆盖。"""
    global _ACTIVE
    _ACTIVE = _ACTIVE.with_overrides(
        runtime_dir=runtime_dir,
        scan_root=scan_root,
        port=port,
        resource_dir=resource_dir,
        mode=mode,
    )
    _publish_compatibility_values(_ACTIVE)
    return _ACTIVE


def ensure_runtime_dirs(value: RuntimeConfig | None = None) -> None:
    """创建可写运行目录；绝不创建或修改资源目录。"""
    current = value or _ACTIVE
    for directory in (current.data_dir, current.reports_dir, current.logs_dir):
        directory.mkdir(parents=True, exist_ok=True)


# 快照保留策略与通知阈值（非运行路径）
MIN_DIR_KB = 10 * 1024
KEEP_DAILY_DAYS = 35
KEEP_WEEKLY_WEEKS = 12
BIGFILE_DEFAULT_DAYS = 7
BIGFILE_DEFAULT_MB = 100
FREE_ALERT_GB = 10

# 大文件查询预算（ISS-032 Phase 2 写入）。
# 依据：tests/test_bigfiles.py::TestResourceMeasurement 实测
#   small (5×200MB / 1GB)  p50 wall 4ms  / rss 1.39MB / 5 行
#   large (20×200MB / 4GB)  p50 wall 21ms / rss 1.75MB / 20 行
# 真实 HOME（百 GB 级）按 1.5x 余量提升。
BIGFILE_FIND_TIMEOUT_S = 30.0      # find 单次墙钟上限（远超 large 21ms）
BIGFILE_RESULT_CAP = 1000         # find 输出行硬上限（远超 large 20 行）
BIGFILE_CACHE_TTL_S = 30.0        # 成功结果缓存 TTL（可分辨 expired）
BIGFILE_LOG_RETENTION_DAYS = 7     # 大文件查询相关本地日志保留天数
BIGFILE_REPORT_RETENTION_DAYS = 35 # 大文件查询产生的诊断报告保留天数

# du 采集超时（ISS-061）。原 3600s 硬编码在 2026-09-14/15 把生产扫描
# 截断为 status=interrupted / message="du 超过 3600 秒安全时限"（生产根
# /Users/maoking 含 ~11M 文件、937k 目录，一次扫描远超 1 小时），当日
# 快照因此被安全保留为 2026-09-12。默认上调至 14400s（4 小时）作为
# 兼容 + 留出余量的基线；测试与运维可通过环境变量覆盖，无需改代码。
_DU_TIMEOUT_S_DEFAULT = 14400.0
_raw_du_timeout = os.environ.get("FATHOM_DU_TIMEOUT_S")
if _raw_du_timeout is None or not _raw_du_timeout.strip():
    DU_TIMEOUT_S = _DU_TIMEOUT_S_DEFAULT
else:
    try:
        DU_TIMEOUT_S = float(_raw_du_timeout)
    except ValueError as exc:
        raise ConfigurationError(
            f"FATHOM_DU_TIMEOUT_S 必须是正浮点数：{_raw_du_timeout!r}"
        ) from exc
    # 注意：nan 与任何值比较均为 False，inf > 0 为真——两者都会绕过单纯
    # 的 `<= 0` 检查，从而静默解除安全时限（等于把超时防护关掉）。
    # 因此必须同时要求有限且为正数。
    if not math.isfinite(DU_TIMEOUT_S) or DU_TIMEOUT_S <= 0:
        raise ConfigurationError(
            f"FATHOM_DU_TIMEOUT_S 必须是正的有限浮点数：{DU_TIMEOUT_S}"
        )
del _raw_du_timeout

# launchd 仍是开发版遗留入口；ISS-025 不安装/卸载它。
LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SCAN_LABEL = "com.maoscripts.fathom-scan"
WEB_LABEL = "com.maoscripts.fathom-web"
SCAN_HOUR = 12

# 端口策略（ISS-029 G6）：默认绑定 7952；占用时按 PORT_RANGE 个候选
# 端口依序让位（7952..7952+PORT_RANGE）。零击杀：从不向任何进程发信号；
# 同服务身份实例在目标端口时让位退出 0；未知占用者落 exhausted 退 3。
# 默认 0（不主动让位）以保留 ISS-025 「端口冲突退 3」原合同；应用壳可
# 通过 ``--port-range N`` 显式启用让位。
PORT_RANGE = 0
# helper 进程发现文件（0600）由 cli.cmd_serve 写入运行根供应用壳读取。
HELPER_INSTANCE_FILENAME = "helper-instance.json"
