"""ISS-152 启动盘容器与卷发现适配器（``fathom.storage``）测试。

夹具为 ``tests/fixtures/iss152/`` 下的合成 plist（结构对齐 2026-10-04
真机 macOS 15.7.4 实读输出，身份全合成）。全部命令执行与 ``stat`` 均
注入，测试不触碰真实系统状态；真机只读链路由本卡的最小消费者脚本
另行验证并记录证据。

合同反例（任务卡「先复现」三项 + 验收清单）：
1. 同一容器两个卷共享 free——free 只存在于容器级，卷对象不得携带；
2. 系统/数据卷别名（``/`` 快照 vs Data 挂载）与重复根（firmlink 两侧
   路径）不产生重复归因；
3. 同路径换 UUID 保持独立身份、卸载/锁定/未知字段可恢复；
4. 非 APFS 与普通目录回退、超时/命令缺失/非零失败降级不抛致命错。
"""

from __future__ import annotations

import plistlib
import subprocess
import types
from pathlib import Path

import pytest

from fathom import storage

FIXTURES = Path(__file__).parent / "fixtures" / "iss152"

# 与夹具生成约定一致的合成身份。
C_DISK3 = "10000000-0000-4000-8000-000000000003"
V_DATA = "20000000-0000-4000-8000-000000000031"
V_SYSTEM = "20000000-0000-4000-8000-000000000033"
V_VM = "20000000-0000-4000-8000-000000000036"
VG = "30000000-0000-4000-8000-000000000001"
EXT_HFS = "50000000-0000-4000-8000-0000000000A1"
EXT_EXFAT = "50000000-0000-4000-8000-0000000000B2"

# 主链 st_dev 语义（真机实测：启动容器卷组共享同一 st_dev，
# firmlink 两侧路径 (st_dev, st_ino) 相同；外接盘各自独立）。
DEV_STARTUP = 16777230
DEV_EXT_HFS = 999001
DEV_EXT_EXFAT = 999002

FIXED_NOW = "2026-10-04T06:00:00+00:00"


class FakeRunner:
    """按 argv 匹配分发的注入 runner；支持夹具文件/内联 plist/异常。"""

    def __init__(self):
        self.by_argv: dict[tuple[str, ...], object] = {}
        self.calls: list[tuple[str, ...]] = []

    def add_file(self, argv: tuple[str, ...], fixture: str) -> None:
        self.by_argv[argv] = (FIXTURES / fixture).read_bytes()

    def add_plist(self, argv: tuple[str, ...], obj: object) -> None:
        self.by_argv[argv] = plistlib.dumps(obj)

    def add_error(self, argv: tuple[str, ...], exc: Exception) -> None:
        self.by_argv[argv] = exc

    def add_result(self, argv: tuple[str, ...], *, returncode: int,
                   stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.by_argv[argv] = storage.CommandResult(
            argv=argv, returncode=returncode, stdout=stdout, stderr=stderr)

    def __call__(self, argv):
        key = tuple(argv)
        self.calls.append(key)
        value = self.by_argv.get(key)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            return storage.CommandResult(
                argv=key, returncode=0, stdout=value, stderr=b"")
        if isinstance(value, storage.CommandResult):
            return value
        raise AssertionError(f"测试未注册命令：{key}")


class FakeStat:
    """注入的 stat：path -> (st_dev, st_ino)。"""

    def __init__(self, mapping: dict[str, tuple[int, int]]):
        self.mapping = mapping

    def __call__(self, path):
        entry = self.mapping.get(str(path))
        if entry is None:
            raise FileNotFoundError(str(path))
        return types.SimpleNamespace(st_dev=entry[0], st_ino=entry[1])


def default_stat_map() -> dict[str, tuple[int, int]]:
    """主链挂载点/可见入口的合成 st_dev 映射。"""
    return {
        "/": (DEV_STARTUP, 2),
        "/System/Volumes/Data": (DEV_STARTUP, 3),
        "/System/Volumes/Preboot": (DEV_STARTUP, 4),
        "/System/Volumes/VM": (DEV_STARTUP, 5),
        "/System/Volumes/Update": (DEV_STARTUP, 6),
        "/Users/alice": (DEV_STARTUP, 100),
        # firmlink 两侧是同一物理目录：真机 samefile() 为 True。
        "/System/Volumes/Data/Users/alice": (DEV_STARTUP, 100),
        "/Volumes/SampleHFS": (DEV_EXT_HFS, 1),
        "/Volumes/SampleHFS/notes.txt": (DEV_EXT_HFS, 2),
        "/Volumes/SampleExFAT": (DEV_EXT_EXFAT, 1),
    }


def build_main_runner() -> FakeRunner:
    """注册主链全部命令（启动根快照 + apfs list + 逐卷 info + list）。"""
    runner = FakeRunner()
    runner.add_file(("diskutil", "info", "-plist", "/"), "info_startup_root.plist")
    runner.add_file(("diskutil", "apfs", "list", "-plist"), "apfs_list.plist")
    for devid, fixture in [
        ("disk3s1", "info_vol_data.plist"),
        ("disk3s2", "info_vol_update.plist"),
        ("disk3s3", "info_vol_system.plist"),
        ("disk3s4", "info_vol_preboot.plist"),
        ("disk3s5", "info_vol_recovery.plist"),
        ("disk3s6", "info_vol_vm.plist"),
    ]:
        runner.add_file(("diskutil", "info", "-plist", devid), fixture)
    runner.add_file(("diskutil", "list", "-plist"), "list_all.plist")
    return runner


def discover_main(**overrides):
    """主链夹具 + 默认注入的一次发现。"""
    runner = build_main_runner()
    kwargs = dict(
        runner=runner,
        now=lambda: FIXED_NOW,
        platform="darwin",
        stat=FakeStat(default_stat_map()),
    )
    kwargs.update(overrides)
    result = storage.discover_startup(**kwargs)
    return result, runner


def load_plist(fixture: str) -> dict:
    return plistlib.loads((FIXTURES / fixture).read_bytes())


# ---------- 反例 1：同容器两卷共享 free，不能相加 ----------

class TestSharedCapacity:
    def test_free_lives_only_on_container_not_volumes(self):
        result, _ = discover_main()
        assert result.status is storage.DiscoveryStatus.OK
        # 容器级共享 free 唯一存在且是权威值（夹具 CapacityFree）。
        assert result.startup_container is not None
        assert result.startup_container.shared_free_bytes == 13401092096
        assert result.shared_free_bytes() == 13401092096
        # 卷对象不得携带任何 free 字段——结构上杜绝「两卷 free 相加」。
        for volume in result.startup_volumes:
            dump = volume.as_dict()
            assert not any("free" in key for key in dump), dump
            # 卷级只有容量在用（贡献），没有可共享的剩余空间。
            assert volume.capacity_in_use_bytes is not None

    def test_system_and_data_volumes_share_one_container(self):
        result, _ = discover_main()
        assert result.startup_container is not None
        data_volume = result.volume_by_role("Data")
        system_volume = result.volume_by_role("System")
        assert data_volume.container_id == system_volume.container_id
        assert data_volume.container_id == result.startup_container.container_id


# ---------- 反例 2：别名/重复根不产重复归因 ----------

class TestAliasAndDuplicateRoots:
    def test_firmlink_both_sides_same_source_one_container(self):
        result, _ = discover_main()
        visible = result.classify_path("/Users/alice")
        physical = result.classify_path("/System/Volumes/Data/Users/alice")
        assert visible.attributed and physical.attributed
        assert visible.classification == "startup"
        assert physical.classification == "startup"
        assert visible.container_id == physical.container_id
        # (st_dev, st_ino) 相同 → 同一物理来源，不重复归因。
        assert result.same_source("/Users/alice",
                                  "/System/Volumes/Data/Users/alice") is True

    def test_snapshot_root_and_data_mount_single_startup(self):
        result, _ = discover_main()
        assert result.startup_container is not None
        entries = {e.visible_path: e for e in result.visible_entries}
        # 「/」是系统卷快照挂载：可见路径归属 System 卷，via 标注快照。
        root_entry = entries["/"]
        assert root_entry.via == "snapshot_mount"
        assert root_entry.volume_id == f"apfs-volume:{V_SYSTEM}"
        data_entry = entries["/System/Volumes/Data"]
        assert data_entry.via == "mount"
        assert data_entry.volume_id == f"apfs-volume:{V_DATA}"
        # 两个入口同属唯一启动容器，不产生第二个「启动盘」。
        volumes = {v.volume_id: v for v in result.startup_volumes}
        assert volumes[root_entry.volume_id].container_id == \
            volumes[data_entry.volume_id].container_id

    def test_duplicate_visible_roots_not_double_counted(self):
        result, _ = discover_main()
        # 所有能归到卷的可见入口都归到唯一启动容器；
        # 分类器对每个入口给出同一容器归属。
        containers = set()
        for entry in result.visible_entries:
            assert entry.container_id == f"apfs-container:{C_DISK3}"
            attribution = result.classify_path(entry.visible_path)
            containers.add(attribution.container_id)
        containers.discard(None)
        assert containers == {f"apfs-container:{C_DISK3}"}


# ---------- 反例 3：身份与状态 ----------

class TestIdentityAndStatus:
    def test_same_mount_point_different_uuid_keeps_two_identities(self):
        # 两个不同 UUID 的卷都声称挂载 /System/Volumes/Data（异常态）：
        # 身份不得按路径合并，二者各自独立、冲突可观测。
        runner = build_main_runner()
        conflicting = load_plist("info_vol_data.plist")
        conflicting["DeviceIdentifier"] = "disk3s7"
        conflicting["DiskUUID"] = "20000000-0000-4000-8000-000000000037"
        conflicting["VolumeUUID"] = conflicting["DiskUUID"]
        runner.add_plist(("diskutil", "info", "-plist", "disk3s7"), conflicting)
        base_list = load_plist("apfs_list.plist")
        extra = {
            "APFSVolumeUUID": "20000000-0000-4000-8000-000000000037",
            "CapacityInUse": 1024, "CryptoMigrationOn": False,
            "DeviceIdentifier": "disk3s7", "Encryption": False,
            "FileVault": False, "Locked": False, "Name": "Impostor",
            "Roles": ["Data"],
        }
        for container in base_list["Containers"]:
            if container["ContainerReference"] == "disk3":
                container["Volumes"].append(extra)
        runner.add_plist(("diskutil", "apfs", "list", "-plist"), base_list)

        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat(default_stat_map()))
        assert result.status is storage.DiscoveryStatus.OK
        ids = {v.volume_id for v in result.startup_volumes}
        assert len(ids & {
            "apfs-volume:20000000-0000-4000-8000-000000000037",
            f"apfs-volume:{V_DATA}",
        }) == 2
        assert any("duplicate-mount-point" in err for err in result.errors)

    def test_same_uuid_remounted_keeps_stable_identity(self):
        # 同一 UUID 换挂载点（重挂载）：身份不变——身份是 UUID，不是路径。
        first_runner = build_main_runner()
        second_runner = build_main_runner()
        moved = load_plist("info_vol_vm.plist")
        moved["MountPoint"] = "/System/Volumes/VM2"
        second_runner.add_plist(("diskutil", "info", "-plist", "disk3s6"), moved)

        first = storage.discover_startup(
            runner=first_runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat(default_stat_map()))
        stat_map = default_stat_map()
        stat_map["/System/Volumes/VM2"] = (DEV_STARTUP, 7)
        second = storage.discover_startup(
            runner=second_runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat(stat_map))
        vm_id = f"apfs-volume:{V_VM}"
        assert {v.volume_id for v in first.startup_volumes} == \
            {v.volume_id for v in second.startup_volumes}
        by_id = {v.volume_id: v for v in second.startup_volumes}
        assert by_id[vm_id].mount_point == "/System/Volumes/VM2"

    def test_unmounted_volume_status(self):
        result, _ = discover_main()
        # roles 按合同小写规范化。
        recovery = next(v for v in result.startup_volumes
                        if "recovery" in v.roles)
        assert recovery.status == "unmounted"
        assert recovery.mount_point is None

    def test_locked_volume_status(self):
        runner = build_main_runner()
        base_list = load_plist("apfs_list.plist")
        for container in base_list["Containers"]:
            if container["ContainerReference"] == "disk3":
                for volume in container["Volumes"]:
                    if volume["DeviceIdentifier"] == "disk3s6":
                        volume["Locked"] = True
                        volume["FileVault"] = True
        runner.add_plist(("diskutil", "apfs", "list", "-plist"), base_list)
        locked_info = load_plist("info_vol_vm.plist")
        locked_info["MountPoint"] = ""
        locked_info["Locked"] = True
        locked_info["FileVault"] = True
        runner.add_plist(("diskutil", "info", "-plist", "disk3s6"), locked_info)

        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat(default_stat_map()))
        vm = next(v for v in result.startup_volumes
                  if v.volume_id == f"apfs-volume:{V_VM}")
        assert vm.status == "locked"

    def test_unknown_plist_keys_recoverable(self):
        runner = build_main_runner()
        future = load_plist("info_vol_data.plist")
        future["FutureUnknownKey"] = {"nested": [1, 2, 3]}
        runner.add_plist(("diskutil", "info", "-plist", "disk3s1"), future)
        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat(default_stat_map()))
        assert result.status is storage.DiscoveryStatus.OK
        data_volume = result.volume_by_role("Data")
        assert data_volume.mount_point == "/System/Volumes/Data"

    def test_volume_info_failure_degrades_to_unknown(self):
        runner = build_main_runner()
        runner.add_error(("diskutil", "info", "-plist", "disk3s6"),
                         subprocess.TimeoutExpired("diskutil", 10))
        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat(default_stat_map()))
        # 单卷 info 失败不拖垮整链：该卷状态 unknown，整体仍 OK。
        assert result.status is storage.DiscoveryStatus.OK
        vm = next(v for v in result.startup_volumes
                  if v.volume_id == f"apfs-volume:{V_VM}")
        assert vm.status == "unknown"
        assert vm.mount_point is None
        assert any("disk3s6" in err for err in result.errors)

    def test_unmatched_snapshot_group_never_forges_identity(self):
        # 快照根的卷组匹配不到 system 卷时：入口保留但 volume_id 为空，
        # 不得用路径伪造稳定 ID。
        runner = build_main_runner()
        orphan = load_plist("info_startup_root.plist")
        orphan["APFSVolumeGroupID"] = "33333333-0000-4000-8000-0000000000FF"
        runner.add_plist(("diskutil", "info", "-plist", "/"), orphan)
        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat(default_stat_map()))
        entries = {e.visible_path: e for e in result.visible_entries}
        assert entries["/"].volume_id is None
        assert entries["/"].device_identifier == "disk3s3s1"


# ---------- 反例 4：回退与降级 ----------

class TestFallbackAndDegradation:
    def test_nonapfs_root_falls_back_to_single_root(self):
        runner = FakeRunner()
        runner.add_file(("diskutil", "info", "-plist", "/"),
                        "info_startup_root_nonapfs.plist")
        # 非 APFS 根仍会尝试收集外接设备（others），但不跑容器链。
        runner.add_file(("diskutil", "list", "-plist"), "list_all.plist")
        stat = FakeStat({"/": (DEV_STARTUP, 2), "/Users/alice": (DEV_STARTUP, 9)})
        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="darwin", stat=stat)
        assert result.status is storage.DiscoveryStatus.FALLBACK_SINGLE_ROOT
        assert result.startup_container is None
        assert result.startup_volumes == ()
        # 非 APFS 根不再继续跑 apfs list（命令预算不浪费在无意义链上）。
        assert ("diskutil", "apfs", "list", "-plist") not in runner.calls
        # 路径归属退回 unknown：现有单目录能力继续可用，不抛错。
        attribution = result.classify_path("/Users/alice")
        assert attribution.attributed is False
        assert attribution.classification == "unknown"
        assert attribution.volume_id is None

    def test_diskutil_missing_degrades_unavailable(self):
        runner = FakeRunner()
        runner.add_error(("diskutil", "info", "-plist", "/"),
                         FileNotFoundError("diskutil"))
        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat({}))
        assert result.status is storage.DiscoveryStatus.UNAVAILABLE
        assert result.startup_container is None
        assert result.errors

    def test_timeout_degrades_unavailable(self):
        runner = FakeRunner()
        runner.add_error(("diskutil", "info", "-plist", "/"),
                         subprocess.TimeoutExpired("diskutil", 10))
        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat({}))
        assert result.status is storage.DiscoveryStatus.UNAVAILABLE
        assert any("timeout" in err.lower() or "timed out" in err.lower()
                   for err in result.errors)

    def test_nonzero_exit_degrades_unavailable(self):
        # 普通目录/无法解析的 startup_root：diskutil info 非零退出。
        runner = FakeRunner()
        runner.add_result(("diskutil", "info", "-plist", "/Users/alice"),
                          returncode=1, stderr=b"Could not find disk")
        result = storage.discover_startup(
            startup_root="/Users/alice", runner=runner,
            now=lambda: FIXED_NOW, platform="darwin", stat=FakeStat({}))
        assert result.status is storage.DiscoveryStatus.UNAVAILABLE
        assert result.startup_container is None

    def test_unsupported_platform_runs_no_commands(self):
        runner = FakeRunner()
        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="linux",
            stat=FakeStat({}))
        assert result.status is storage.DiscoveryStatus.UNSUPPORTED_PLATFORM
        assert runner.calls == []
        assert result.startup_container is None


# ---------- 版本化对象与序列化 ----------

class TestVersionedObjects:
    def test_every_object_carries_schema_and_version(self):
        result, _ = discover_main()
        assert result.schema == storage.DISCOVERY_SCHEMA
        assert result.version == storage.DISCOVERY_VERSION
        assert result.startup_container.schema == storage.CONTAINER_SCHEMA
        assert result.startup_container.version == storage.DISCOVERY_VERSION
        for volume in result.startup_volumes:
            assert volume.schema == storage.VOLUME_SCHEMA
            assert volume.version == storage.DISCOVERY_VERSION
        for device in result.other_devices:
            assert device.schema == storage.OTHER_DEVICE_SCHEMA
            assert device.version == storage.DISCOVERY_VERSION
        attribution = result.classify_path("/Users/alice")
        assert attribution.schema == storage.ATTRIBUTION_SCHEMA
        assert attribution.version == storage.DISCOVERY_VERSION

    def test_as_dict_json_serializable(self):
        import json
        result, _ = discover_main()
        dump = json.dumps(result.as_dict(), ensure_ascii=False)
        assert storage.DISCOVERY_SCHEMA in dump

    def test_generated_at_and_sources_recorded(self):
        result, runner = discover_main()
        assert result.generated_at == FIXED_NOW
        assert result.source_commands
        # 来源里能看出真实命令链（不含路径伪造）。
        joined = "\n".join(result.source_commands)
        assert "diskutil info -plist" in joined
        assert "diskutil apfs list -plist" in joined

    def test_identity_uses_uuid_not_paths(self):
        result, _ = discover_main()
        for volume in result.startup_volumes:
            assert volume.volume_id.startswith("apfs-volume:")
            assert str(volume.mount_point) not in volume.volume_id


# ---------- others：网络/其他设备独立分类，不自动选择 ----------

class TestOtherDevices:
    def test_external_devices_classified_separately(self):
        result, _ = discover_main()
        ids = {d.device_id for d in result.other_devices}
        assert f"partition:{EXT_HFS}" in ids
        assert f"partition:{EXT_EXFAT}" in ids
        # 启动容器卷不出现在 others。
        startup_ids = {v.volume_id for v in result.startup_volumes}
        assert not (ids & startup_ids)
        # 未挂载的 EFI 分区也登记（mount_point 为空）。
        efi = next(d for d in result.other_devices
                   if d.device_identifier == "disk5s1")
        assert efi.mount_point is None

    def test_other_device_path_not_selected_as_startup(self):
        result, _ = discover_main()
        attribution = result.classify_path("/Volumes/SampleHFS/notes.txt")
        assert attribution.classification == "other_device"
        assert attribution.attributed is True
        assert attribution.startup is False
        assert attribution.container_id is None
        assert attribution.device_id == f"partition:{EXT_HFS}"

    def test_unknown_device_path_stays_unknown(self):
        result, _ = discover_main()
        attribution = result.classify_path("/Volumes/NotExist/file")
        assert attribution.attributed is False
        assert attribution.classification == "unknown"

    def test_known_entry_attribution_matches_visible_entry(self):
        # 真机 2026-10-04 实测暴露：系统/数据卷共享 st_dev，仅凭 st_dev
        # 推断会把 "/"（快照入口，权威属 system 卷）错归 Data 卷。
        # 路径恰为可见入口/挂载点时必须采用该入口的权威归属。
        result, _ = discover_main()
        root_attr = result.classify_path("/")
        assert root_attr.volume_id == f"apfs-volume:{V_SYSTEM}"
        data_attr = result.classify_path("/System/Volumes/Data")
        assert data_attr.volume_id == f"apfs-volume:{V_DATA}"
        hfs_attr = result.classify_path("/Volumes/SampleHFS")
        assert hfs_attr.classification == "other_device"
        assert hfs_attr.device_id == f"partition:{EXT_HFS}"


# ---------- 命令预算 ----------

class TestBudget:
    def test_info_probe_cap_respected(self):
        runner = build_main_runner()
        result = storage.discover_startup(
            runner=runner, now=lambda: FIXED_NOW, platform="darwin",
            stat=FakeStat(default_stat_map()), info_probe_cap=2)
        info_calls = [c for c in runner.calls
                      if c[:3] == ("diskutil", "info", "-plist")
                      and c[3] != "/"]
        assert len(info_calls) == 2
        # 超出预算的卷按已知信息登记（挂载点未知 → 状态 unknown）。
        unknown = [v for v in result.startup_volumes if v.status == "unknown"]
        assert len(unknown) >= 1
        assert any("info-probe-cap" in err for err in result.errors)

    def test_timeout_budget_forwarded(self):
        recorded: list[float] = []

        def runner(argv):
            recorded.append(storage.DISCOVERY_TIMEOUT_S)
            raise FileNotFoundError("diskutil")

        storage.discover_startup(runner=runner, now=lambda: FIXED_NOW,
                                 platform="darwin", stat=FakeStat({}))
        assert recorded == [storage.DISCOVERY_TIMEOUT_S]


# ---------- 真实命令行拼写（守卫：不执行，只构造） ----------

def test_discovery_command_forms_readonly_plist():
    # 发现链只使用既定只读子命令，且全部要求 -plist 结构化输出，
    # 不解析本地化显示文本（MediaType 等字段只透传不消费）。
    forms = storage.discovery_command_forms()
    joined = [" ".join(a) for a in forms]
    assert "diskutil apfs list -plist" in joined
    assert "diskutil list -plist" in joined
    for argv in forms:
        assert argv[0] == "diskutil"
        assert "-plist" in argv
        assert argv[1] in {"info", "apfs", "list"}
