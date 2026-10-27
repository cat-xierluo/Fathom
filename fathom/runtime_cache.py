"""Runtime 能力快照的最小持久缓存（ISS-200 生产接线）。

职责边界：

- 缓存的是**已观测事实**：某个入口在某次探测下的运行时事实与能力快照。
- 缓存**不是能力背书**：命中缓存只省掉重复探测，不代表入口此刻仍然可用。
  任何要据此放行的场合仍须重新比对入口指纹——同版换文件会使缓存失效。
- **失败不致命**：读写缓存的任何异常都被吞掉并如实报告「未命中」，
  绝不阻断探测或派发，也绝不覆盖最近一次成功事实。
- 不发模型请求、不读凭据、不安装、不后台轮询。

落盘位置：运行根 ``data/`` 下的 ``runtime-capabilities.json``（独立文件，
不进 ``settings.json``）。理由：``settings.json`` 是**用户授权设置**，有严格
的键白名单与旧版本兼容合同；把机器观测事实塞进去会让「用户授权」与
「探测缓存」混在一处，且旧版 Fathom 读到未知键会 fail-closed。``data/``
已是既有的运行时数据目录（不入库），符合项目既有落盘策略。
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger("fathom.runtime_cache")

#: 缓存文件名（运行根 data/ 下）。
CACHE_FILENAME = "runtime-capabilities.json"

#: 缓存格式版本。读入版本不符 → 当作无缓存（fail-open 到重新探测）。
CACHE_SCHEMA_VERSION = 1

#: 条目有效期：超过即视为过期（仍不删除，只是重新探测）。
CACHE_TTL_S = 24 * 3600.0


@dataclass(frozen=True, slots=True)
class CachedEntry:
    """一次成功探测留下的观测事实。

    ``success`` 为 False 表示这次探测本身失败（broken/not_found 等）。
    失败条目**只作诊断参考**，绝不能当作可用事实覆盖上一次成功事实。
    """

    runtime_id: str
    executable: str
    #: 入口指纹（``runtime_capabilities.EntryIdentity.fingerprint()``）。
    fingerprint: str
    #: 观测时刻（epoch 秒）。
    observed_at: float
    #: 该次探测是否成功（得出可定级结论）。
    success: bool
    #: 该次探测的完整运行时事实（``RuntimeInfo.to_dict()`` 形态）。
    runtime_info: dict
    version: str | None = None

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.observed_at)

    @property
    def expired(self) -> bool:
        return self.age_s > CACHE_TTL_S

    def to_dict(self) -> dict:
        return {
            "runtime_id": self.runtime_id,
            "executable": self.executable,
            "fingerprint": self.fingerprint,
            "observed_at": self.observed_at,
            "success": self.success,
            "runtime_info": self.runtime_info,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "CachedEntry | None":
        if not isinstance(raw, dict):
            return None
        try:
            runtime_id = raw["runtime_id"]
            executable = raw["executable"]
            fingerprint = raw["fingerprint"]
            timestamp = raw["observed_at"]
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                return None
            observed_at = float(timestamp)
            if not math.isfinite(observed_at) or observed_at < 0:
                return None
            success = bool(raw["success"])
            runtime_info = raw["runtime_info"]
            version = raw.get("version")
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if not isinstance(runtime_id, str) or not isinstance(executable, str):
            return None
        if not isinstance(fingerprint, str) or not isinstance(runtime_info, dict):
            return None
        if not isinstance(version, str) and version is not None:
            return None
        return cls(
            runtime_id=runtime_id,
            executable=executable,
            fingerprint=fingerprint,
            observed_at=observed_at,
            success=success,
            runtime_info=runtime_info,
            version=version,
        )


def cache_path(runtime_dir: Path | str | None = None) -> Path:
    """缓存文件位置；缺省用当前运行根的 data 目录。"""
    if runtime_dir is not None:
        return Path(runtime_dir) / CACHE_FILENAME
    from . import config
    return Path(config.DATA_DIR) / CACHE_FILENAME


def load_entries(path: Path | str | None = None) -> dict[str, CachedEntry]:
    """读缓存；任何失败都当作「没有缓存」，绝不抛错阻断调用方。

    返回按 ``runtime_id`` 索引的条目字典。损坏文件、版本不符、字段形状
    不对——一律返回空字典（fail-open 到重新探测）。
    """
    target = Path(path) if path is not None else cache_path()
    try:
        raw_text = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return {}
    try:
        data = json.loads(raw_text)
    except ValueError:
        _LOG.warning("runtime cache 不是合法 JSON，已忽略：%s", target)
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("schema") != CACHE_SCHEMA_VERSION:
        # 版本不符 → 整体作废（不尝试逐条兼容），重新探测即可。
        return {}
    entries_raw = data.get("entries")
    if not isinstance(entries_raw, dict):
        return {}
    out: dict[str, CachedEntry] = {}
    for key, value in entries_raw.items():
        entry = CachedEntry.from_dict(value)
        if entry is not None:
            out[key] = entry
    return out


def save_entries(entries: dict[str, CachedEntry],
                 path: Path | str | None = None) -> bool:
    """原子写缓存；任何失败都返回 False 且**不覆盖旧文件**。

    调用方把返回值当作「尽力而为」：缓存写不进去不影响探测结论。
    """
    target = Path(path) if path is not None else cache_path()
    try:
        payload = {
            "schema": CACHE_SCHEMA_VERSION,
            "entries": {key: entry.to_dict() for key, entry in entries.items()},
        }
        if any(CachedEntry.from_dict(raw) is None
               for raw in payload["entries"].values()):
            return False
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True,
                          allow_nan=False) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    except (OSError, UnicodeError, TypeError, ValueError, OverflowError) as exc:
        _LOG.warning("runtime cache 写入失败（不影响探测结论）：%s", exc)
        return False
    return True


def record_probe(
    runtime_id: str,
    executable: str,
    fingerprint: str,
    runtime_info: dict,
    *,
    success: bool,
    version: str | None = None,
    path: Path | str | None = None,
) -> bool:
    """记一次探测结果。

    **不覆盖最近成功事实**：若同一 ``runtime_id`` 已有成功条目，而本次
    探测失败，则保留旧的成功条目不动。缓存写失败返回 False（不致命）。
    """
    entries = load_entries(path)
    entry = CachedEntry(
        runtime_id=runtime_id,
        executable=executable,
        fingerprint=fingerprint,
        observed_at=time.time(),
        success=success,
        runtime_info=runtime_info,
        version=version,
    )
    previous = entries.get(runtime_id)
    if previous is not None and previous.success and not success:
        # 失败不得抹掉最近一次成功事实：旧成功条目原样保留。
        entries[runtime_id] = previous
        return save_entries(entries, path)
    entries[runtime_id] = entry
    return save_entries(entries, path)


def record_capability_evidence(
    runtime_id: str,
    executable: str,
    fingerprint: str,
    capability_snapshot: dict,
    *,
    version: str | None = None,
    path: Path | str | None = None,
) -> bool:
    """回填**真实受控调用**取得的能力证据（VERIFIED）。

    只在调用成功且结果校验通过后由调用方调用（ISS-200）。本函数不判断
    是否真的调用过——它只负责落盘；判断在 ``runtime_capabilities.
    apply_call_evidence`` 里做（那里核对快照/调用记录/证据三方绑定）。

    落盘时**只覆盖快照部分**：该条目仍是「成功事实」，但 ``runtime_info``
    保留最近一次探测的原始事实，不被证据回填改写。
    """
    entries = load_entries(path)
    previous = entries.get(runtime_id)
    runtime_info = dict(previous.runtime_info) if previous is not None else {}
    runtime_info["capability_snapshot"] = capability_snapshot
    entry = CachedEntry(
        runtime_id=runtime_id,
        executable=executable,
        fingerprint=fingerprint,
        observed_at=time.time(),
        success=True,
        runtime_info=runtime_info,
        version=version if version is not None else (previous.version if previous else None),
    )
    entries[runtime_id] = entry
    return save_entries(entries, path)


def lookup(runtime_id: str, executable: str | None = None,
           fingerprint: str | None = None,
           path: Path | str | None = None) -> CachedEntry | None:
    """取缓存条目；过期、入口不符或指纹不符时返回 None（调用方须重新探测）。

    ``fingerprint`` 是**当前**入口指纹：传入时只有指纹一致才命中，
    这正是「同版换文件使缓存失效」的判据。
    """
    entry = load_entries(path).get(runtime_id)
    if entry is None or entry.expired:
        return None
    if executable is not None and entry.executable != executable:
        return None
    if fingerprint is not None and entry.fingerprint != fingerprint:
        return None
    return entry
