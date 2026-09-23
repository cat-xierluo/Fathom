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

ISS-016A 起运行根下可有 ``settings.json`` 持久化用户设置（扫描根/计划
时间/入库阈值/低空间阈值）。生效优先级：显式 CLI 覆盖 > ``FATHOM_*``
环境变量 > ``settings.json`` > 内置默认。环境变量优先的理由：它是
ISS-025 以来的运维与隔离合同入口（TESTING 的隔离验证、launchd plist、
helper 都靠它钉住扫描根），若落盘设置能反超环境变量，一份意外落盘的
``settings.json`` 就能把扫描重定向到错误根并静默形成新数据集。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
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
    """在打开任何运行资源前应用显式的进程级 CLI/helper 覆盖。

    显式 ``--scan-root`` 之后再不被 settings.json 覆盖（CLI > 环境变量 >
    settings.json，见模块 docstring）；运行根变化后按新根重读 settings.json。
    """
    global _ACTIVE, _CLI_SCAN_ROOT_PINNED
    _ACTIVE = _ACTIVE.with_overrides(
        runtime_dir=runtime_dir,
        scan_root=scan_root,
        port=port,
        resource_dir=resource_dir,
        mode=mode,
    )
    if scan_root is not None:
        _CLI_SCAN_ROOT_PINNED = True
    _publish_compatibility_values(_ACTIVE)
    refresh_user_settings()
    return _ACTIVE


def ensure_runtime_dirs(value: RuntimeConfig | None = None) -> None:
    """创建可写运行目录；绝不创建或修改资源目录。"""
    current = value or _ACTIVE
    for directory in (current.data_dir, current.reports_dir, current.logs_dir):
        directory.mkdir(parents=True, exist_ok=True)


# 快照保留策略与通知阈值。MIN_DIR_KB / FREE_ALERT_GB / SCAN_HOUR /
# SCAN_MINUTE 四项可被运行根 settings.json 覆盖（ISS-016A，模块尾部统一
# 重发布）；保留策略与大文件默认值当前是代码策略，不入 settings.json。
_DEFAULT_MIN_KB = 10 * 1024
_DEFAULT_FREE_ALERT_GB = 10
MIN_DIR_KB = _DEFAULT_MIN_KB
FREE_ALERT_GB = _DEFAULT_FREE_ALERT_GB
KEEP_DAILY_DAYS = 35
KEEP_WEEKLY_WEEKS = 12
BIGFILE_DEFAULT_DAYS = 7
BIGFILE_DEFAULT_MB = 100

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
# 生产根（约 1100 万文件、93.7 万目录）一次扫描远超 1 小时），当日
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
# 计划时间（launchd 安装时读取；SCAN_MINUTE 的 plist 写入留父卡 ISS-016）
SCAN_HOUR = 12
SCAN_MINUTE = 0

# 端口策略（ISS-029 G6）：默认绑定 7952；占用时按 PORT_RANGE 个候选
# 端口依序让位（7952..7952+PORT_RANGE）。零击杀：从不向任何进程发信号；
# 同服务身份实例在目标端口时让位退出 0；未知占用者落 exhausted 退 3。
# 默认 0（不主动让位）以保留 ISS-025 「端口冲突退 3」原合同；应用壳可
# 通过 ``--port-range N`` 显式启用让位。
PORT_RANGE = 0
# helper 进程发现文件（0600）由 cli.cmd_serve 写入运行根供应用壳读取。
HELPER_INSTANCE_FILENAME = "helper-instance.json"


# ---------- 用户设置持久化（ISS-016A：运行根下 settings.json） ----------

SETTINGS_FILENAME = "settings.json"
DEFAULT_SCAN_TIME = "12:00"
_SCAN_TIME_PATTERN = re.compile(r"\A([01]\d|2[0-3]):([0-5]\d)\Z")
_SETTING_KEYS = ("scan_root", "scan_time", "min_kb", "free_alert_gb",
                 "exclude_names")
# ISS-066：du ``-I mask`` 按名字（fnmatch）跳过整棵子树；超过该数就退回
# 逐项路径排除或考虑拆分运行根（防御性上限，避免配置层把 du argv 撑爆）。
MAX_EXCLUDE_NAMES = 50

# 生效排除集：扫描器按此向 du 注入 -I <每项>（按规范顺序：排序去重）。
EXCLUDE_NAMES: list[str] = []

# PUT /api/config 与 configure() 可能并发触发合并写；设置写入低频，互斥足够。
_SETTINGS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class UserSettings:
    """已持久化的用户设置；``None`` 表示该项未持久化（按内置默认）。"""

    scan_root: str | None = None
    scan_time: str | None = None
    min_kb: float | None = None
    free_alert_gb: float | None = None
    exclude_names: str | None = None  # 规范串（排序去重后 ``;`` 拼接），无配置 = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "scan_root": self.scan_root,
            "scan_time": self.scan_time,
            "min_kb": self.min_kb,
            "free_alert_gb": self.free_alert_gb,
            "exclude_names": self.exclude_names,
        }


def _validated_scan_time(raw: object) -> str:
    if not isinstance(raw, str) or not _SCAN_TIME_PATTERN.fullmatch(raw.strip()):
        raise ConfigurationError(f"scan_time 必须是 HH:MM 格式（00:00–23:59）：{raw!r}")
    return raw.strip()


def _parse_hhmm(text: str) -> tuple[int, int]:
    match = _SCAN_TIME_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ConfigurationError(f"scan_time 必须是 HH:MM 格式（00:00–23:59）：{text!r}")
    return int(match.group(1)), int(match.group(2))


def _validated_positive_number(raw: object, name: str) -> float:
    # bool 是 int 的子类，先排除；字符串/None/容器一律拒绝。
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigurationError(f"{name} 必须是数值：{raw!r}")
    value = float(raw)
    # nan 与任何值比较均为 False、inf > 0 为真——两者都会绕过单纯的
    # ``<= 0`` 检查（ISS-061 同款 fail-closed），必须同时要求有限且为正。
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{name} 必须是正的有限数值：{raw!r}")
    return value


def _validated_scan_root(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigurationError(f"scan_root 必须是非空字符串：{raw!r}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"scan_root 必须是绝对路径：{raw}")
    resolved = path.resolve()
    if not resolved.exists():
        raise ConfigurationError(f"scan_root 路径不存在：{resolved}")
    if not resolved.is_dir():
        raise ConfigurationError(f"scan_root 必须是目录：{resolved}")
    return str(resolved)


def _canonicalize_exclude_names(items: list[str]) -> str:
    """校验每项并产出排序去重的规范串（``;`` 拼接）。

    设计要求（ISS-066 实施边界）：
    - 每项非空、无 ``/``（BSD du -I 按名字匹配，路径分隔会改变语义）；
    - 无 NUL；不得为 ``.`` 或 ``..``（无意义且会被 du 拒绝）；
    - fnmatch.translate 必须能解析（捕获真正的语法错误）；
    - 排序去重后 ≤50 项（防御性上限）。排序顺序使持久化与扫描器
      argv 注入顺序确定；与扫描器 argv 注入一致——同名掩码反复出现的
      语义不依赖插入顺序。
    """
    seen: set[str] = set()
    canonical: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(
                f"exclude_names 每项必须是非空字符串：{item!r}"
            )
        if "/" in item:
            raise ConfigurationError(
                f"exclude_names 不得包含路径分隔符 /：{item!r}"
            )
        if "\x00" in item:
            raise ConfigurationError(
                f"exclude_names 不得包含 NUL 字节：{item!r}"
            )
        if item in (".", ".."):
            raise ConfigurationError(
                f"exclude_names 不得为 {item!r}"
            )
        try:
            fnmatch.translate(item)
        except re.error as exc:
            raise ConfigurationError(
                f"exclude_names 不是合法 fnmatch 模式：{item!r}（{exc}）"
            ) from exc
        seen.add(item)
    canonical = sorted(seen)
    if len(canonical) > MAX_EXCLUDE_NAMES:
        raise ConfigurationError(
            f"exclude_names 项数不得超过 {MAX_EXCLUDE_NAMES}："
            f"当前 {len(canonical)} 项"
        )
    return ";".join(canonical)


def _validated_exclude_names(raw: object) -> str:
    """校验 + 规范化为规范串（空列表视为显式清空 → 规范空串）。

    接受两种形态：
    - 列表（PUT/合并入口）：``["a", "b"]`` → 校验 + 排序去重 + ``;`` 拼接；
    - 字符串（settings.json 已落盘的规范串）：``"a;b"`` → 仅校验每项符合
      规则后原样保留，避免回写后再次 ``;`` 分割再合并的双重转换开销。
    """
    if isinstance(raw, str):
        items = [m for m in raw.split(";") if m]
        return _canonicalize_exclude_names(items)
    if isinstance(raw, list):
        return _canonicalize_exclude_names(raw)
    raise ConfigurationError(
        f"exclude_names 必须是字符串列表或规范串：{raw!r}"
    )


def parse_user_settings(data: Mapping[str, object]) -> UserSettings:
    """把（部分）设置字典校验为 UserSettings；未知键或坏值 fail-closed。"""
    unknown = sorted(set(data) - set(_SETTING_KEYS))
    if unknown:
        raise ConfigurationError(f"未知的配置项：{', '.join(unknown)}")
    values: dict[str, object] = {}
    validators = {
        "scan_root": _validated_scan_root,
        "scan_time": _validated_scan_time,
        "min_kb": lambda raw: _validated_positive_number(raw, "min_kb"),
        "free_alert_gb": lambda raw: _validated_positive_number(raw, "free_alert_gb"),
        "exclude_names": _validated_exclude_names,
    }
    for key, validate in validators.items():
        raw = data.get(key)
        values[key] = None if raw is None else validate(raw)
    return UserSettings(**values)  # type: ignore[arg-type]


def merge_user_settings(
    current: UserSettings, changes: Mapping[str, object]
) -> UserSettings:
    """部分更新：只改 ``changes`` 里出现的键，其余保持 ``current``。"""
    merged = dict(current.as_dict())
    for key, value in parse_user_settings(changes).as_dict().items():
        if key in changes:
            merged[key] = value
    return UserSettings(**merged)  # type: ignore[arg-type]


def load_user_settings(path: Path) -> UserSettings:
    """读取 settings.json；文件不存在 → 全部默认。

    旧运行根没有该文件时零迁移零报错；文件存在但损坏/含坏值则
    fail-closed（ConfigurationError）——静默回落默认值会把扫描根换掉并
    静默形成新数据集，比拒绝启动更糟。
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return UserSettings()
    except OSError as exc:
        raise ConfigurationError(f"settings.json 无法读取（{path}）：{exc}") from exc
    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        raise ConfigurationError(f"settings.json 不是合法 JSON（{path}）：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"settings.json 必须是 JSON 对象：{path}")
    try:
        return parse_user_settings(data)
    except ConfigurationError as exc:
        raise ConfigurationError(f"settings.json 内容无效（{path}）：{exc}") from exc


def _atomic_write_text(path: Path, text: str) -> None:
    """临时文件 + rename 原子写；任一步失败都保留旧文件不动。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def save_user_settings(path: Path, settings: UserSettings) -> None:
    """原子写入 settings.json（只落已持久化的键）。失败抛 OSError。"""
    payload = {key: value for key, value in settings.as_dict().items()
               if value is not None}
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)


# 进程内当前生效的持久化设置（_USER_SETTINGS）与是否被 CLI 显式钉住扫描根。
_USER_SETTINGS = UserSettings()
_CLI_SCAN_ROOT_PINNED = False


def get_user_settings() -> UserSettings:
    return _USER_SETTINGS


def settings_path() -> Path:
    """settings.json 的规范位置：当前运行根下。"""
    return _ACTIVE.runtime_dir / SETTINGS_FILENAME


def _publish_policy_values(settings: UserSettings) -> None:
    """把（合并后的）设置发布为模块常量；未持久化项回落内置默认。"""
    global MIN_DIR_KB, FREE_ALERT_GB, SCAN_HOUR, SCAN_MINUTE, EXCLUDE_NAMES
    MIN_DIR_KB = settings.min_kb if settings.min_kb is not None else _DEFAULT_MIN_KB
    FREE_ALERT_GB = (
        settings.free_alert_gb
        if settings.free_alert_gb is not None
        else _DEFAULT_FREE_ALERT_GB
    )
    SCAN_HOUR, SCAN_MINUTE = _parse_hhmm(settings.scan_time or DEFAULT_SCAN_TIME)
    # exclude_names：规范串已排序去重；解析回列表供扫描器按序注入 -I。
    if settings.exclude_names:
        EXCLUDE_NAMES = [m for m in settings.exclude_names.split(";") if m]
    else:
        EXCLUDE_NAMES = []


def _exclude_names_from_env(env: Mapping[str, str]) -> list[str] | None:
    """从 FATHOM_EXCLUDE_NAMES 环境变量解析（分号分隔）。空/未设返回 None。"""
    raw = env.get("FATHOM_EXCLUDE_NAMES")
    if raw is None:
        return None
    items = [m.strip() for m in raw.split(";") if m.strip()]
    return items or None


def refresh_user_settings(
    settings: UserSettings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> UserSettings:
    """把持久化设置合并进进程内生效值（优先级见模块 docstring）。

    - 扫描根：CLI 显式覆盖 > ``FATHOM_SCAN_ROOT`` > settings.json > 默认；
    - 排除集：``FATHOM_EXCLUDE_NAMES`` > settings.json > 默认（与既有链一致）；
    - 计划时间/入库阈值/低空间阈值：settings.json > 默认（无环境变量入口）。
    """
    global _ACTIVE, _USER_SETTINGS
    env = os.environ if environ is None else environ
    if settings is None:
        settings = load_user_settings(_ACTIVE.runtime_dir / SETTINGS_FILENAME)
    # 排除集的环境变量优先级在 settings.json 之上：复用同一 fail-closed
    # 校验链（_canonicalize_exclude_names）拒绝任何非合法项，与持久化层
    # 行为一致——CLI/运维/服务都共用同一规则。
    env_items = _exclude_names_from_env(env)
    if env_items is not None:
        settings = UserSettings(
            scan_root=settings.scan_root,
            scan_time=settings.scan_time,
            min_kb=settings.min_kb,
            free_alert_gb=settings.free_alert_gb,
            exclude_names=_canonicalize_exclude_names(env_items),
        )
    _USER_SETTINGS = settings
    env_scan_root = env.get("FATHOM_SCAN_ROOT", "").strip()
    if (not _CLI_SCAN_ROOT_PINNED and not env_scan_root
            and settings.scan_root is not None):
        _ACTIVE = _ACTIVE.with_overrides(scan_root=settings.scan_root)
        _publish_compatibility_values(_ACTIVE)
    _publish_policy_values(settings)
    return settings


def update_user_settings(changes: Mapping[str, object]) -> UserSettings:
    """校验 + 原子写入 + 刷新进程内生效值（PUT /api/config 的入口）。

    任一步失败：旧文件不动、进程内生效值不变（校验失败抛
    ConfigurationError，写失败抛 OSError，两者都不产生半更新状态）。
    """
    with _SETTINGS_LOCK:
        merged = merge_user_settings(_USER_SETTINGS, changes)
        save_user_settings(settings_path(), merged)
        return refresh_user_settings(merged)


def effective_settings_view() -> dict[str, object]:
    """GET /api/config 数据源：生效值 + 每项来源 + 默认值与只读策略。"""
    env_scan_root = os.environ.get("FATHOM_SCAN_ROOT", "").strip()
    env_exclude_names = os.environ.get("FATHOM_EXCLUDE_NAMES", "").strip()
    settings = _USER_SETTINGS
    if env_scan_root or _CLI_SCAN_ROOT_PINNED:
        scan_root_source = "env" if env_scan_root else "cli"
    elif settings.scan_root is not None:
        scan_root_source = "settings"
    else:
        scan_root_source = "default"
    if env_exclude_names:
        exclude_names_source = "env"
    elif settings.exclude_names:
        exclude_names_source = "settings"
    else:
        exclude_names_source = "default"
    sources = {
        "scan_root": scan_root_source,
        "scan_time": "settings" if settings.scan_time is not None else "default",
        "min_kb": "settings" if settings.min_kb is not None else "default",
        "free_alert_gb": "settings" if settings.free_alert_gb is not None else "default",
        "exclude_names": exclude_names_source,
    }
    return {
        "scan_root": str(_ACTIVE.scan_root),
        "scan_time": settings.scan_time or DEFAULT_SCAN_TIME,
        "min_kb": MIN_DIR_KB,
        "free_alert_gb": FREE_ALERT_GB,
        "exclude_names": EXCLUDE_NAMES,
        "sources": sources,
        "defaults": {
            "scan_root": str(_ACTIVE.home_dir),
            "scan_time": DEFAULT_SCAN_TIME,
            "min_kb": _DEFAULT_MIN_KB,
            "free_alert_gb": _DEFAULT_FREE_ALERT_GB,
        },
        "policies": {
            "keep_daily_days": KEEP_DAILY_DAYS,
            "keep_weekly_weeks": KEEP_WEEKLY_WEEKS,
            "du_timeout_s": DU_TIMEOUT_S,
            "bigfile_default_days": BIGFILE_DEFAULT_DAYS,
            "bigfile_default_mb": BIGFILE_DEFAULT_MB,
        },
        "settings_path": str(settings_path()),
    }


# 启动即合并一次：无 settings.json 的旧运行根全部默认值，不迁移不报错；
# 有则按上述优先级生效（保存后重启进程仍生效即依赖这一行）。
refresh_user_settings()
