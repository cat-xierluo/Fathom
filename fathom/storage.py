"""启动盘容器与卷发现适配器（ISS-152，只读）。

为整盘空间追踪提供只读的启动盘容器/卷发现数据来源。全部元数据来自
结构化 plist 输出（``diskutil info -plist`` / ``diskutil apfs list
-plist`` / ``diskutil list -plist``），**不解析本地化显示文本**
（如 ``MediaType``）；本模块对这类字段只透传、不消费。

合同要点（详见任务卡 ISS-152）：

- **版本化对象**：所有返回对象携带 ``schema``/``version`` 字段，供
  ISS-153 持久化、ISS-154 规划消费；``as_dict()`` 输出 JSON 可序列化。
- **共享容量只在容器级**：APFS 同容器各卷共享剩余空间，因此
  ``DiscoveredVolume`` 结构上不携带任何 free 字段（唯一权威值在
  ``DiscoveredContainer.shared_free_bytes`` / ``StorageDiscovery
  .shared_free_bytes()``），从结构上杜绝「两卷 free 相加」。
- **稳定身份 = UUID，不是路径**：``volume_id = "apfs-volume:<UUID>"``。
  同路径换 UUID 保持两个独立身份；同 UUID 换挂载点身份不变；无法权威
  确认归属的入口（如快照根匹配不到 system 卷）``volume_id`` 为 None，
  绝不用路径伪造稳定 ID。容器/卷 UUID 缺失、空白或重复时拒绝生成
  空前缀/重复伪身份：整体降级 ``unavailable`` + ``identity-missing:``/
  ``identity-duplicate:`` 错误可观测（``status=ok`` 不伴随身份缺失）。
- **可见路径与物理路径映射**：``visible_entries`` 记录实测到的可见入口
  （卷挂载点 ``via="mount"``；启动根系统卷快照 ``via="snapshot_mount"``，
  经 ``APFSVolumeGroupID`` + system 角色权威匹配归属）。macOS firmlink
  不会被 ``realpath()`` 展开（2026-10-04 实测），路径归属与同源判定用
  ``st_dev`` / ``(st_dev, st_ino)``（真机实测：启动容器卷组共享同一
  ``st_dev``，firmlink 两侧路径 ``samefile()`` 为 True）——``st_dev`` 是
  即时值、重启可变，只用于即时归类，绝不进入持久化身份。
- **网络/其他设备独立分类**：外接/非 APFS 分区进 ``other_devices``，
  ``classify_path`` 对它们给出 ``other_device`` 分类，不与启动盘合并、
  不自动选择。未匹配任何已发现设备的路径保持 ``unknown``。
- **降级不抛致命错**：非 macOS 平台 / 命令缺失 / 超时 / 非零退出 /
  plist 损坏 → 明确的 ``unsupported_platform`` / ``unavailable`` /
  ``fallback_single_root`` 状态 + ``errors`` 可观测原因，调用方可回退
  现有单目录能力。
- **预算约束**：每条命令 ``DISCOVERY_TIMEOUT_S`` 墙钟上限；逐卷
  ``info`` 探测数受 ``info_probe_cap`` 限制，超出部分按已知信息登记
  （挂载状态 unknown），绝不无上限地派生子进程。

本模块不写 schema/api/config/scanner；不执行 du、不挂载/格式化、
不读取目录内容（``classify_path`` 仅对给定路径与已发现挂载点做
``stat``）。
"""

from __future__ import annotations

import datetime as dt
import enum
import os
import plistlib
import subprocess
import sys
import xml.parsers.expat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

DISCOVERY_SCHEMA = "fathom.storage.discovery"
CONTAINER_SCHEMA = "fathom.storage.container"
VOLUME_SCHEMA = "fathom.storage.volume"
OTHER_DEVICE_SCHEMA = "fathom.storage.other-device"
VISIBLE_ENTRY_SCHEMA = "fathom.storage.visible-entry"
ATTRIBUTION_SCHEMA = "fathom.storage.path-attribution"
DISCOVERY_VERSION = 1

# 命令预算（ISS-152 合同：受预算约束的只读命令链）。
DISCOVERY_TIMEOUT_S = 10.0
INFO_PROBE_CAP = 32

# plist Content 类型前缀：属于 APFS 容器机制的分区不进 other_devices
# （它们由容器/卷清单描述，重复登记会制造第二个归因来源）。
_APFS_CONTENT_PREFIX = "Apple_APFS"


class DiscoveryStatus(str, enum.Enum):
    """发现链结果状态；除 OK 外均为明确降级态，不抛致命错。"""

    OK = "ok"
    # 启动根不是 APFS（或缺少容器字段）：回退现有单目录能力。
    FALLBACK_SINGLE_ROOT = "fallback_single_root"
    # 命令缺失/超时/非零退出/plist 损坏等：发现适配器不可用。
    UNAVAILABLE = "unavailable"
    # 非 macOS 平台：不派生任何命令。
    UNSUPPORTED_PLATFORM = "unsupported_platform"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """单条只读命令的结果（注入 runner 与默认实现的共同契约）。"""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


CommandRunner = Callable[[Sequence[str]], CommandResult]
StatFn = Callable[[str], object]


def discovery_command_forms() -> tuple[tuple[str, ...], ...]:
    """发现链使用的命令形态（静态说明，供审计；不含实际执行）。"""
    return (
        ("diskutil", "info", "-plist", "<startup-root-or-device>"),
        ("diskutil", "apfs", "list", "-plist"),
        ("diskutil", "list", "-plist"),
    )


def _make_subprocess_runner(timeout_s: float) -> CommandRunner:
    """默认 runner：subprocess + 墙钟上限；超时转 timed_out 结果。

    ``FileNotFoundError``（命令缺失）按原样抛出，由 ``discover_startup``
    统一捕获降级——runner 协议里它不是可恢复的命令级失败。
    """

    def runner(argv: Sequence[str]) -> CommandResult:
        key = tuple(argv)
        try:
            proc = subprocess.run(
                list(argv), capture_output=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                argv=key, returncode=-1, stdout=b"",
                stderr=str(exc).encode(), timed_out=True)
        return CommandResult(
            argv=key, returncode=proc.returncode,
            stdout=proc.stdout, stderr=proc.stderr)

    return runner


@dataclass(frozen=True, slots=True)
class DiscoveredContainer:
    """APFS 启动容器：共享容量的唯一权威归属。"""

    schema: str
    version: int
    container_id: str                 # "apfs-container:<APFSContainerUUID>"
    container_reference: str          # 如 disk3（诊断用，非稳定身份）
    capacity_ceiling_bytes: int | None
    shared_free_bytes: int | None     # 同容器全部卷共享；唯一 free 来源
    physical_store_ids: tuple[str, ...]
    source: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "container_id": self.container_id,
            "container_reference": self.container_reference,
            "capacity_ceiling_bytes": self.capacity_ceiling_bytes,
            "shared_free_bytes": self.shared_free_bytes,
            "physical_store_ids": list(self.physical_store_ids),
            "source": list(self.source),
        }


@dataclass(frozen=True, slots=True)
class DiscoveredVolume:
    """启动容器内的一个 APFS 卷。

    注意：结构上没有 free 字段——同容器卷共享剩余空间，卷级 free
    是错误口径（反例：两卷 free 相加会重复计账）。
    """

    schema: str
    version: int
    volume_id: str                    # "apfs-volume:<APFSVolumeUUID>"
    container_id: str
    volume_group_id: str | None       # "apfs-vg:<UUID>"；系统/数据卷组
    name: str
    roles: tuple[str, ...]            # 小写：system/data/preboot/...
    device_identifier: str            # 如 disk3s1（诊断用，非稳定身份）
    mount_point: str | None           # 物理挂载点；未挂载为 None
    status: str                       # accessible|unmounted|locked|unknown
    filevault: bool | None
    capacity_in_use_bytes: int | None
    sources: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "volume_id": self.volume_id,
            "container_id": self.container_id,
            "volume_group_id": self.volume_group_id,
            "name": self.name,
            "roles": list(self.roles),
            "device_identifier": self.device_identifier,
            "mount_point": self.mount_point,
            "status": self.status,
            "filevault": self.filevault,
            "capacity_in_use_bytes": self.capacity_in_use_bytes,
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class OtherDevice:
    """非启动容器的设备分区（外接 HFS/exFAT/EFI 等）；独立分类不自动选择。"""

    schema: str
    version: int
    device_id: str                    # "partition:<DiskUUID>"
    name: str
    device_identifier: str            # 如 disk4s1
    mount_point: str | None
    filesystem_content: str | None    # plist Content 原值（非本地化）
    internal: bool | None
    capacity_bytes: int | None
    source: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "device_id": self.device_id,
            "name": self.name,
            "device_identifier": self.device_identifier,
            "mount_point": self.mount_point,
            "filesystem_content": self.filesystem_content,
            "internal": self.internal,
            "capacity_bytes": self.capacity_bytes,
            "source": list(self.source),
        }


@dataclass(frozen=True, slots=True)
class VisibleEntry:
    """一个用户可见入口及其权威归属（映射明确；归属不了则 volume_id=None）。"""

    schema: str
    version: int
    visible_path: str
    volume_id: str | None
    container_id: str | None
    via: str                          # "mount" | "snapshot_mount"
    device_identifier: str | None
    source: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "visible_path": self.visible_path,
            "volume_id": self.volume_id,
            "container_id": self.container_id,
            "via": self.via,
            "device_identifier": self.device_identifier,
            "source": list(self.source),
        }


@dataclass(frozen=True, slots=True)
class PathAttribution:
    """单条路径的即时归属判定。

    ``st_dev`` 是即时值（重启可变），仅用于本次归类，不得进入持久化
    身份；持久化请用 ``container_id`` / ``volume_id`` / ``device_id``。
    """

    schema: str
    version: int
    path: str
    st_dev: int | None
    attributed: bool
    classification: str               # startup|other_device|unknown
    startup: bool
    container_id: str | None
    volume_id: str | None
    device_id: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "path": self.path,
            "st_dev": self.st_dev,
            "attributed": self.attributed,
            "classification": self.classification,
            "startup": self.startup,
            "container_id": self.container_id,
            "volume_id": self.volume_id,
            "device_id": self.device_id,
        }


@dataclass(frozen=True, slots=True)
class StorageDiscovery:
    """一次发现的结果集（版本化，供 ISS-153 持久化 / ISS-154 规划）。"""

    schema: str
    version: int
    status: DiscoveryStatus
    generated_at: str
    platform: str
    startup_container: DiscoveredContainer | None
    startup_volumes: tuple[DiscoveredVolume, ...]
    visible_entries: tuple[VisibleEntry, ...]
    other_devices: tuple[OtherDevice, ...]
    errors: tuple[str, ...]
    source_commands: tuple[str, ...]
    # 发现时使用的 stat 函数（测试注入）；classify_path/same_source 未
    # 显式传 stat 时沿用同一实现，保证一次发现与其归属判定同源。
    _default_stat: StatFn | None = None

    def shared_free_bytes(self) -> int | None:
        """启动容器共享剩余空间的唯一权威入口（卷级不设该口径）。"""
        return None if self.startup_container is None \
            else self.startup_container.shared_free_bytes

    def volume_by_role(self, role: str) -> DiscoveredVolume | None:
        """第一个携带该角色的卷（角色大小写不敏感）；无则 None。"""
        wanted = role.strip().lower()
        for volume in self.startup_volumes:
            if wanted in volume.roles:
                return volume
        return None

    def volume_by_id(self, volume_id: str) -> DiscoveredVolume | None:
        for volume in self.startup_volumes:
            if volume.volume_id == volume_id:
                return volume
        return None

    # ---- 路径归属（即时计算；不缓存，stat 可注入） ----

    def _startup_device_ids(self, stat: StatFn) -> dict[int, tuple[str, str | None]]:
        """st_dev -> (container_id, volume_id)；对启动侧入口逐个 stat。

        启动容器卷组共享同一 st_dev（真机实测），同一 st_dev 上后登记
        的入口不覆盖已有 volume 归属——快照根先登记，保证 ``/`` 归到
        system 卷。
        """
        mapping: dict[int, tuple[str, str | None]] = {}
        if self.startup_container is None:
            return mapping
        entries = list(self.visible_entries) + [
            _entry_like_mount(v) for v in self.startup_volumes
            if v.mount_point
        ]
        for entry in entries:
            try:
                st_dev = int(stat(entry.visible_path).st_dev)  # type: ignore[attr-defined]
            except (OSError, ValueError):
                continue
            if st_dev not in mapping:
                mapping[st_dev] = (entry.container_id, entry.volume_id)
        return mapping

    def _other_device_ids(self, stat: StatFn) -> dict[int, OtherDevice]:
        mapping: dict[int, OtherDevice] = {}
        for device in self.other_devices:
            if not device.mount_point:
                continue
            try:
                st_dev = int(stat(device.mount_point).st_dev)  # type: ignore[attr-defined]
            except (OSError, ValueError):
                continue
            mapping.setdefault(st_dev, device)
        return mapping

    def classify_path(self, path: str | Path, *,
                      stat: StatFn | None = None) -> PathAttribution:
        """把一条路径归属到启动容器 / 其他设备 / unknown。

        判据是 ``st_dev``（同一 APFS 容器卷组 + firmlink 语义下这是
        「同一来源」的权威粒度）。子路径只能归到容器级——卷级归属仅
        对挂载点/可见入口本身给出，不猜 firmlink 映射表。
        """
        stat_fn = stat if stat is not None else (self._default_stat or os.stat)
        normalized = os.path.normpath(str(path))
        try:
            st_dev = int(stat_fn(normalized).st_dev)  # type: ignore[attr-defined]
        except (OSError, ValueError):
            st_dev = None
        # 权威入口优先：路径恰为可见入口/挂载点时直接采用其登记归属。
        # 系统卷快照 "/" 与 Data 卷共享 st_dev（真机实测），st_dev 推断
        # 在卷级不可分辨——不把入口归属交给「先到先得」的映射顺序。
        entry = next((e for e in self.visible_entries
                      if e.visible_path == normalized), None)
        if entry is not None:
            return PathAttribution(
                schema=ATTRIBUTION_SCHEMA, version=DISCOVERY_VERSION,
                path=normalized, st_dev=st_dev, attributed=True,
                classification="startup", startup=True,
                container_id=entry.container_id,
                volume_id=entry.volume_id, device_id=None)
        mounted = next((v for v in self.startup_volumes
                        if v.mount_point == normalized), None)
        if mounted is not None:
            return PathAttribution(
                schema=ATTRIBUTION_SCHEMA, version=DISCOVERY_VERSION,
                path=normalized, st_dev=st_dev, attributed=True,
                classification="startup", startup=True,
                container_id=mounted.container_id,
                volume_id=mounted.volume_id, device_id=None)
        mounted_other = next((d for d in self.other_devices
                              if d.mount_point == normalized), None)
        if mounted_other is not None:
            return PathAttribution(
                schema=ATTRIBUTION_SCHEMA, version=DISCOVERY_VERSION,
                path=normalized, st_dev=st_dev, attributed=True,
                classification="other_device", startup=False,
                container_id=None, volume_id=None,
                device_id=mounted_other.device_id)
        if st_dev is None:
            return PathAttribution(
                schema=ATTRIBUTION_SCHEMA, version=DISCOVERY_VERSION,
                path=normalized, st_dev=None, attributed=False,
                classification="unknown", startup=False,
                container_id=None, volume_id=None, device_id=None)
        startup_map = self._startup_device_ids(stat_fn)
        if st_dev in startup_map:
            container_id, volume_id = startup_map[st_dev]
            # 卷级身份仅当路径恰为该卷的挂载点/可见入口时成立。
            if not self._path_is_known_entry(normalized):
                volume_id = None
            return PathAttribution(
                schema=ATTRIBUTION_SCHEMA, version=DISCOVERY_VERSION,
                path=normalized, st_dev=st_dev, attributed=True,
                classification="startup", startup=True,
                container_id=container_id, volume_id=volume_id,
                device_id=None)
        other_map = self._other_device_ids(stat_fn)
        device = other_map.get(st_dev)
        if device is not None:
            return PathAttribution(
                schema=ATTRIBUTION_SCHEMA, version=DISCOVERY_VERSION,
                path=normalized, st_dev=st_dev, attributed=True,
                classification="other_device", startup=False,
                container_id=None, volume_id=None, device_id=device.device_id)
        return PathAttribution(
            schema=ATTRIBUTION_SCHEMA, version=DISCOVERY_VERSION,
            path=normalized, st_dev=st_dev, attributed=False,
            classification="unknown", startup=False,
            container_id=None, volume_id=None, device_id=None)

    def _path_is_known_entry(self, normalized: str) -> bool:
        known = {e.visible_path for e in self.visible_entries}
        known.update(v.mount_point for v in self.startup_volumes if v.mount_point)
        return normalized in known

    def same_source(self, a: str | Path, b: str | Path, *,
                    stat: StatFn | None = None) -> bool | None:
        """两条路径是否同一物理来源（(st_dev, st_ino) 判据）。

        与 ``os.path.samefile`` 同语义；任一路径 stat 失败返回 None
        （无法判定，不猜）。典型用途：firmlink 两侧重复根去重。
        """
        stat_fn = stat if stat is not None else (self._default_stat or os.stat)
        try:
            st_a = stat_fn(os.path.normpath(str(a)))
            st_b = stat_fn(os.path.normpath(str(b)))
            return (int(st_a.st_dev), int(st_a.st_ino)) == \
                (int(st_b.st_dev), int(st_b.st_ino))  # type: ignore[attr-defined]
        except (OSError, ValueError, TypeError):
            return None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "status": self.status.value,
            "generated_at": self.generated_at,
            "platform": self.platform,
            "startup_container": (
                None if self.startup_container is None
                else self.startup_container.as_dict()),
            "startup_volumes": [v.as_dict() for v in self.startup_volumes],
            "visible_entries": [e.as_dict() for e in self.visible_entries],
            "other_devices": [d.as_dict() for d in self.other_devices],
            "errors": list(self.errors),
            "source_commands": list(self.source_commands),
        }


def _entry_like_mount(volume: DiscoveredVolume) -> VisibleEntry:
    """把卷挂载点包装成 entry 形态供 st_dev 归属使用（不进 visible_entries）。"""
    return VisibleEntry(
        schema=VISIBLE_ENTRY_SCHEMA, version=DISCOVERY_VERSION,
        visible_path=volume.mount_point or "", volume_id=volume.volume_id,
        container_id=volume.container_id, via="mount",
        device_identifier=volume.device_identifier, source=volume.sources)


def _argv_str(argv: Sequence[str]) -> str:
    return " ".join(argv)


def _stderr_tail(stderr: bytes, limit: int = 200) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    return text[-limit:]


def _run_plist(runner: CommandRunner, argv: Sequence[str],
               errors: list[str]) -> dict | None:
    """执行一条 -plist 命令并解析；任何失败记录 errors 并返回 None。"""
    try:
        result = runner(argv)
    except FileNotFoundError as exc:
        errors.append(f"command-missing: {_argv_str(argv)}: {exc}")
        return None
    except subprocess.TimeoutExpired as exc:
        errors.append(f"command-timeout: {_argv_str(argv)}: {exc}")
        return None
    except OSError as exc:
        errors.append(f"command-error: {_argv_str(argv)}: {exc}")
        return None
    if result.timed_out:
        errors.append(f"command-timeout: {_argv_str(argv)}")
        return None
    if result.returncode != 0:
        errors.append(
            f"command-failed: {_argv_str(argv)}: exit={result.returncode}"
            f": {_stderr_tail(result.stderr)}")
        return None
    try:
        data = plistlib.loads(result.stdout)
    except (ValueError, OSError, xml.parsers.expat.ExpatError) as exc:
        errors.append(f"plist-invalid: {_argv_str(argv)}: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"plist-invalid: {_argv_str(argv)}: not a dict")
        return None
    return data


def _volume_status(apfs_volume: dict, volume_info: dict | None,
                   mount_point: str | None) -> str:
    """卷状态判定（顺序即优先级）：unknown → locked → accessible/unmounted。

    - info 探测失败或超出预算 → unknown（挂载情况不可确认，不猜）；
    - Locked=True（apfs list 或 info 任一来源）或 FileVault 卷未挂载
      → locked（FileVault 卷锁定态在解锁前不可访问）；
    - 其余按挂载点有无可访问性。
    """
    if volume_info is None:
        return "unknown"
    if apfs_volume.get("Locked") is True or volume_info.get("Locked") is True:
        return "locked"
    if volume_info.get("FileVault") is True and not mount_point:
        return "locked"
    return "accessible" if mount_point else "unmounted"


def _snapshot_volume_id(root_info: dict, volumes: list[DiscoveredVolume]):
    """快照挂载根的权威归属：VolumeGroup + system 角色恰好一个匹配。

    匹配不到（组缺失、多候选）返回 None——不通过路径或设备名前缀
    伪造稳定身份。
    """
    group = root_info.get("APFSVolumeGroupID")
    if not isinstance(group, str) or not group:
        return None
    wanted = f"apfs-vg:{group}"
    candidates = [v for v in volumes
                  if v.volume_group_id == wanted and "system" in v.roles]
    if len(candidates) == 1:
        return candidates[0].volume_id
    return None


def _parse_other_devices(listing: dict, startup_reference: str | None,
                         source: str) -> list[OtherDevice]:
    """从 diskutil list -plist 提取 other_devices。

    跳过：whole-disk 条目本身、APFS 容器机制分区（``Apple_APFS*``，
    含启动容器成员——避免制造第二个归因来源）。其余分区按
    ``DiskUUID`` 构造 ``partition:`` 身份；无 DiskUUID 的条目跳过
    （无稳定身份可登记，宁缺毋假）。
    """
    devices: list[OtherDevice] = []
    for disk in listing.get("AllDisksAndPartitions", []):
        if not isinstance(disk, dict):
            continue
        internal = disk.get("Internal")
        for part in disk.get("Partitions", []):
            if not isinstance(part, dict):
                continue
            content = part.get("Content")
            if isinstance(content, str) and content.startswith(_APFS_CONTENT_PREFIX):
                continue
            if startup_reference is not None and \
                    part.get("APFSContainerReference") == startup_reference:
                continue
            disk_uuid = part.get("DiskUUID")
            if not isinstance(disk_uuid, str) or not disk_uuid:
                continue
            mount = part.get("MountPoint")
            devices.append(OtherDevice(
                schema=OTHER_DEVICE_SCHEMA, version=DISCOVERY_VERSION,
                device_id=f"partition:{disk_uuid}",
                name=str(part.get("VolumeName") or ""),
                device_identifier=str(part.get("DeviceIdentifier") or ""),
                mount_point=mount if isinstance(mount, str) and mount else None,
                filesystem_content=content if isinstance(content, str) else None,
                internal=internal if isinstance(internal, bool) else None,
                capacity_bytes=part.get("Size")
                if isinstance(part.get("Size"), int) else None,
                source=(source,),
            ))
    return devices


def discover_startup(
    startup_root: str | Path = "/",
    *,
    runner: CommandRunner | None = None,
    timeout_s: float = DISCOVERY_TIMEOUT_S,
    info_probe_cap: int = INFO_PROBE_CAP,
    now: Callable[[], str] | None = None,
    platform: str | None = None,
    stat: StatFn | None = None,
) -> StorageDiscovery:
    """执行一次只读发现链，返回版本化 ``StorageDiscovery``。

    参数均可注入（runner/now/platform/stat 供测试与隔离消费者使用）。
    任何命令级失败都收敛为降级状态 + ``errors``，不抛致命错。

    命令链（预算约束：每条 ``timeout_s``；逐卷 info 至多
    ``info_probe_cap`` 条）：

    1. ``diskutil info -plist <startup_root>`` —— 启动根归属（容器引用、
       卷组、快照标志、挂载点）；失败即整体 ``unavailable``；
       非 APFS 根 → ``fallback_single_root``（现有单目录能力继续可用）。
    2. ``diskutil apfs list -plist`` —— 全部容器/卷（身份/角色/容量在用/
       锁定）；失败即整体 ``unavailable``（容器清单是本发现的核心）。
    3. 启动容器逐卷 ``diskutil info -plist <device>`` —— 挂载点/卷组；
       单卷失败仅该卷 ``unknown``。
    4. ``diskutil list -plist`` —— 外接/非 APFS 分区（other_devices）；
       失败仅记 errors，不影响启动侧结果。
    """
    plat = platform if platform is not None else sys.platform
    generated_at = (now or
                    (lambda: dt.datetime.now(dt.timezone.utc).isoformat()))()
    stat_fn = stat if stat is not None else os.stat
    errors: list[str] = []
    executed: list[str] = []

    def unavailable() -> StorageDiscovery:
        return StorageDiscovery(
            schema=DISCOVERY_SCHEMA, version=DISCOVERY_VERSION,
            status=DiscoveryStatus.UNAVAILABLE, generated_at=generated_at,
            platform=plat, startup_container=None, startup_volumes=(),
            visible_entries=(), other_devices=(), errors=tuple(errors),
            source_commands=tuple(executed), _default_stat=stat_fn)

    if plat != "darwin":
        return StorageDiscovery(
            schema=DISCOVERY_SCHEMA, version=DISCOVERY_VERSION,
            status=DiscoveryStatus.UNSUPPORTED_PLATFORM,
            generated_at=generated_at, platform=plat,
            startup_container=None, startup_volumes=(), visible_entries=(),
            other_devices=(), errors=(), source_commands=(),
            _default_stat=stat_fn)

    run = runner if runner is not None else _make_subprocess_runner(timeout_s)
    root_argv = ("diskutil", "info", "-plist", str(startup_root))
    root_info = _run_plist(run, root_argv, errors)
    executed.append(_argv_str(root_argv))
    if root_info is None:
        return unavailable()

    if root_info.get("FilesystemType") != "apfs" or \
            not isinstance(root_info.get("APFSContainerReference"), str):
        # 非 APFS 启动根：明确回退单目录能力；仍尝试收集 others，
        # 但不再跑 apfs 容器链（对非 APFS 根无意义）。
        errors.append(
            "fallback-single-root: startup root is not an APFS volume"
            f" (FilesystemType={root_info.get('FilesystemType')!r})")
        listing_argv = ("diskutil", "list", "-plist")
        listing = _run_plist(run, listing_argv, errors)
        other_devices: tuple[OtherDevice, ...] = ()
        if listing is not None:
            executed.append(_argv_str(listing_argv))
            other_devices = tuple(_parse_other_devices(
                listing, None, _argv_str(listing_argv)))
        return StorageDiscovery(
            schema=DISCOVERY_SCHEMA, version=DISCOVERY_VERSION,
            status=DiscoveryStatus.FALLBACK_SINGLE_ROOT,
            generated_at=generated_at, platform=plat,
            startup_container=None, startup_volumes=(), visible_entries=(),
            other_devices=other_devices, errors=tuple(errors),
            source_commands=tuple(executed), _default_stat=stat_fn)

    container_reference = root_info["APFSContainerReference"]
    apfs_argv = ("diskutil", "apfs", "list", "-plist")
    apfs_listing = _run_plist(run, apfs_argv, errors)
    executed.append(_argv_str(apfs_argv))
    if apfs_listing is None:
        return unavailable()

    container = next(
        (c for c in apfs_listing.get("Containers", [])
         if isinstance(c, dict)
         and c.get("ContainerReference") == container_reference), None)
    if container is None:
        errors.append(
            f"container-not-found: {container_reference} not in apfs list")
        return unavailable()

    container_uuid = str(container.get("APFSContainerUUID") or "").strip()
    if not container_uuid:
        # 容器 UUID 缺失：空 prefix 的 container_id 是可碰撞伪身份，
        # 整体降级 unavailable（与 container-not-found 同构）。
        errors.append(
            f"identity-missing: apfs container {container_reference}:"
            " APFSContainerUUID missing/blank")
        return unavailable()
    container_id = f"apfs-container:{container_uuid}"

    # ---- 逐卷 info（预算约束） ----
    raw_volumes = [v for v in container.get("Volumes", [])
                   if isinstance(v, dict)]
    probed: list[tuple[dict, dict | None]] = []
    for index, raw in enumerate(raw_volumes):
        if index >= info_probe_cap:
            errors.append(
                f"info-probe-cap: probed {info_probe_cap} of"
                f" {len(raw_volumes)} startup volumes")
            probed.extend((raw, None) for raw in raw_volumes[index:])
            break
        devid = str(raw.get("DeviceIdentifier") or "")
        volume_argv = ("diskutil", "info", "-plist", devid)
        before = len(errors)
        volume_info = _run_plist(run, volume_argv, errors)
        if volume_info is not None:
            executed.append(_argv_str(volume_argv))
        else:
            # 该卷 info 失败已在 errors 记录命令级原因；这里补卷级标记。
            errors.append(f"volume-info-failed: {devid}")
            if len(errors) == before:  # 理论不可达；防御 errors 空洞
                pass
        probed.append((raw, volume_info))

    volumes: list[DiscoveredVolume] = []
    seen_uuids: dict[str, str] = {}
    for raw, volume_info in probed:
        devid = str(raw.get("DeviceIdentifier") or "")
        uuid = str(raw.get("APFSVolumeUUID") or "").strip()
        if not uuid:
            # 卷 UUID 缺失/空白：空前缀 volume_id 是可碰撞伪身份，
            # 整体降级 unavailable，绝不用路径或设备名顶替。
            errors.append(
                f"identity-missing: apfs volume {devid}:"
                " APFSVolumeUUID missing/blank")
            return unavailable()
        prior_owner = seen_uuids.get(uuid)
        if prior_owner is not None:
            # 两卷同一 UUID：稳定身份不再唯一，降级并观测冲突双方。
            errors.append(
                f"identity-duplicate: apfs volume uuid {uuid}:"
                f" {prior_owner},{devid}")
            return unavailable()
        seen_uuids[uuid] = devid
        roles = tuple(str(role).lower()
                      for role in raw.get("Roles", []) if isinstance(role, str))
        mount = None
        group_id = None
        if volume_info is not None:
            raw_mount = volume_info.get("MountPoint")
            if isinstance(raw_mount, str) and raw_mount:
                mount = raw_mount
            raw_group = volume_info.get("APFSVolumeGroupID")
            if isinstance(raw_group, str) and raw_group:
                group_id = f"apfs-vg:{raw_group}"
        filevault = volume_info.get("FileVault") if volume_info is not None \
            else raw.get("FileVault")
        volumes.append(DiscoveredVolume(
            schema=VOLUME_SCHEMA, version=DISCOVERY_VERSION,
            volume_id=f"apfs-volume:{uuid}",
            container_id=container_id,
            volume_group_id=group_id,
            name=str(raw.get("Name") or ""),
            roles=roles,
            device_identifier=devid,
            mount_point=mount,
            status=_volume_status(raw, volume_info, mount),
            filevault=filevault if isinstance(filevault, bool) else None,
            capacity_in_use_bytes=raw.get("CapacityInUse")
            if isinstance(raw.get("CapacityInUse"), int) else None,
            sources=("diskutil apfs list -plist",)
            + (() if volume_info is None else
               (f"diskutil info -plist {devid}",)),
        ))

    # ---- 可见入口与重复挂载检测 ----
    entries: list[VisibleEntry] = []
    mount_owners: dict[str, list[str]] = {}
    for volume in volumes:
        if volume.mount_point:
            mount_owners.setdefault(volume.mount_point,
                                    []).append(volume.device_identifier)
            entries.append(VisibleEntry(
                schema=VISIBLE_ENTRY_SCHEMA, version=DISCOVERY_VERSION,
                visible_path=volume.mount_point,
                volume_id=volume.volume_id,
                container_id=container_id,
                via="mount",
                device_identifier=volume.device_identifier,
                source=volume.sources))
    for mount_path, owners in sorted(mount_owners.items()):
        if len(owners) > 1:
            errors.append(
                f"duplicate-mount-point: {mount_path}: {','.join(owners)}")

    # 快照挂载根（用户可见的 '/' 通常是 system 卷快照）：
    # 归属匹配失败时保留入口但 volume_id=None，不用路径伪造身份。
    snapshot_mount = root_info.get("MountPoint")
    if root_info.get("APFSSnapshot") is True and \
            isinstance(snapshot_mount, str) and snapshot_mount:
        snapshot_id = _snapshot_volume_id(root_info, volumes)
        if snapshot_id is None:
            errors.append(
                f"snapshot-unattributed: {snapshot_mount}"
                f" (device {root_info.get('DeviceIdentifier')})")
        existing = next((e for e in entries
                         if e.visible_path == snapshot_mount), None)
        if existing is None:
            entries.append(VisibleEntry(
                schema=VISIBLE_ENTRY_SCHEMA, version=DISCOVERY_VERSION,
                visible_path=snapshot_mount,
                volume_id=snapshot_id,
                container_id=container_id,
                via="snapshot_mount",
                device_identifier=str(root_info.get("DeviceIdentifier") or ""),
                source=(_argv_str(root_argv),)))
        # 同一可见路径已被某卷挂载占有时（如启动根即挂载点），
        # 保留 mount 语义条目；快照归属已通过 volume 归属体现。

    discovered_container = DiscoveredContainer(
        schema=CONTAINER_SCHEMA, version=DISCOVERY_VERSION,
        container_id=container_id,
        container_reference=container_reference,
        capacity_ceiling_bytes=container.get("CapacityCeiling")
        if isinstance(container.get("CapacityCeiling"), int) else None,
        shared_free_bytes=container.get("CapacityFree")
        if isinstance(container.get("CapacityFree"), int) else None,
        physical_store_ids=tuple(
            str(store.get("DeviceIdentifier"))
            for store in container.get("PhysicalStores", [])
            if isinstance(store, dict)),
        source=(_argv_str(apfs_argv),),
    )

    # ---- others（失败仅降级记录） ----
    listing_argv = ("diskutil", "list", "-plist")
    listing = _run_plist(run, listing_argv, errors)
    others: tuple[OtherDevice, ...] = ()
    if listing is not None:
        executed.append(_argv_str(listing_argv))
        others = tuple(_parse_other_devices(
            listing, container_reference, _argv_str(listing_argv)))

    return StorageDiscovery(
        schema=DISCOVERY_SCHEMA, version=DISCOVERY_VERSION,
        status=DiscoveryStatus.OK, generated_at=generated_at,
        platform=plat, startup_container=discovered_container,
        startup_volumes=tuple(volumes), visible_entries=tuple(entries),
        other_devices=others, errors=tuple(errors),
        source_commands=tuple(executed), _default_stat=stat_fn)


# ---------------------------------------------------------------------------
# ISS-157 摘要辅助：只读纯函数，不落库、不触发任何扫描
# ---------------------------------------------------------------------------

#: 差额说明的固定措辞。差额是「两次容量读数之间、目录测量没能解释掉」的那
#: 部分余量，**不是垃圾、不是可回收量**：本工具没有删除权限结论。
UNEXPLAINED_LIMITATION = "尚无法由目录变化解释"


def non_overlapping_roots(roots: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把测量根压成互不重叠的集合，返回 (保留根, 被吸收根)。

    目录归因的父子不可加是硬红线：``/Users/me`` 与 ``/Users/me/Downloads``
    都测过时，二者的 measured 差分**不能相加**（子目录的字节已含在父目录
    的累计值里，相加等于把同一份空间数两遍）。这里保留最长（最外层）的
    那个根，较短的被判为其子根而吸收掉。

    判据用「规范化后是否为另一根的真路径前缀」，并要求分段边界，避免
    ``/Users/me`` 误吞 ``/Users/melissa`` 这类同前缀不同目录。
    """
    normalized = sorted(
        {os.path.normpath(str(r)) for r in roots if str(r).strip()},
        key=lambda p: (p.count(os.sep), len(p), p),
    )
    kept: list[str] = []
    absorbed: list[str] = []
    for candidate in normalized:
        prefix = candidate if candidate.endswith(os.sep) else candidate + os.sep
        if any(candidate.startswith(k if k.endswith(os.sep) else k + os.sep)
               for k in kept):
            absorbed.append(candidate)
        else:
            kept.append(candidate)
    return tuple(kept), tuple(absorbed)


def shared_free_once(samples: Sequence[dict], container_id: str | None) -> dict:
    """在同一容器内取**唯一** free 读数，同容器多卷不得翻倍。

    ``samples`` 为已落库的容量样本行（含 container_id/source/free_bytes/
    total_bytes/sampled_at）。反例：APFS 启动容器里系统卷与 Data 卷共享
    同一份剩余空间，两条卷级 free 相加会把剩余空间算成两倍——本函数只
    返回容器级读数一次，并如实标出被忽略的同容器卷级样本数。
    """
    if not container_id:
        return {"container_id": None, "free_bytes": None, "total_bytes": None,
                "source": None, "sampled_at": None,
                "ignored_shared_samples": 0,
                "note": "无容器身份：容量主体不可确定。"}
    same = [s for s in samples if s.get("container_id") == container_id]
    if not same:
        return {"container_id": container_id, "free_bytes": None,
                "total_bytes": None, "source": None, "sampled_at": None,
                "ignored_shared_samples": 0,
                "note": "该容器在所选窗口内没有容量样本。"}
    # 容器级样本优先；同容器内仍有多条时取时间最新的一条（同容器的 free
    # 只有一个物理含义，多条只可能是同一次发现的不同来源表述）。
    # 容器级来源优先（storage-discovery）；只有 legacy statvfs 时如实标
    # statvfs 口径，不冒充容器级发现。
    latest_at = max((s.get("sampled_at") or "") for s in same)
    newest = [s for s in same if (s.get("sampled_at") or "") == latest_at]
    chosen = next((s for s in newest
                   if s.get("source") == "storage-discovery"), newest[0])
    # 同容器内其余样本不参与剩余空间求和。
    #
    # ISS-157 返修 B2（按现有 schema 重定义）：``container_capacity_samples``
    # **没有** subject_kind / 主体类型列，且当前无生产写入方按主体类型标记
    # （scan_coordinator 目前只落容器级读数）。因此不能声称「被忽略的是
    # 卷级样本」——那是凭空的类型断言。故诚实重定义为：**同容器内除被
    # 选中那条以外的样本条数**（纯行数推导，语义真实）。若将来 ISS-153 域
    # 给该表扩展主体类型列，此处再改回按类型计数。
    ignored = len(same) - 1
    return {
        "container_id": container_id,
        "free_bytes": chosen.get("free_bytes"),
        "total_bytes": chosen.get("total_bytes"),
        "source": chosen.get("source"),
        "sampled_at": chosen.get("sampled_at"),
        "ignored_shared_samples": ignored,
        "ignored_definition": ("同容器内除被选中样本外的其余样本条数"
                              "（该表无主体类型列，故不按容器/卷分类计数）。"),
        "note": ("同容器剩余空间共享，free 只计一次；"
                 "同容器其余样本不参与剩余空间求和。"),
    }


def difference_view(*, comparable: bool, free_before: int | None,
                    free_after: int | None, measured_before: int | None,
                    measured_after: int | None,
                    reason: str | None = None) -> dict:
    """构造「尚无法由目录变化解释」的差额视图（带符号，可为负）。

    三条红线：

    1. 只有**同主体 + 同计划 + 两侧都有效**时才给数字；否则差额为
       ``None`` 并给出原因，绝不用 0 冒充「没有变化」。
    2. 差额是**有符号**的差值，不是垃圾量、不是可回收量：free 增加
       （用户删了东西）会得到负差额，措辞与字段名都不得暗示可回收。
    3. 目录测量缺失成员/错时点时不算差额——缺失不是「变化为零」。
    """
    if not comparable:
        return {"bytes": None, "comparable": False,
                "limitation": None,
                "reason": reason or "两侧不可比：缺同主体/同计划身份或存在无效样本。"}
    if None in (free_before, free_after, measured_before, measured_after):
        return {"bytes": None, "comparable": False, "limitation": None,
                "reason": reason or "两侧存在缺失读数，差额不可计算（缺失不补 0）。"}
    # 符号约定：free 减少 = 容器占用增加（正方向）。free 增加（用户删了
    # 东西）得到负值，措辞与字段名都不得暗示可回收。
    free_delta = int(free_before) - int(free_after)     # 正=容器占用增加
    measured_delta = int(measured_after) - int(measured_before)
    return {
        "bytes": free_delta - measured_delta,
        "comparable": True,
        "limitation": UNEXPLAINED_LIMITATION,
        "free_delta_bytes": free_delta,
        "measured_delta_bytes": measured_delta,
        "sign_semantics": ("有符号差值：正=容器占用增加多于目录测量，"
                           "负=容器占用减少；不代表垃圾量或可回收空间。"),
        "reason": None,
    }
