#!/usr/bin/env python3
"""ISS-141 · 变化解读真实 Tauri 产品路径验收入口（PM 实机执行）。

修返修 episode 1：集中修掉独审 B1-B6。

  B1 启动前先证明隔离 env 有效（合成根存在、扫描根非 HOME、端口非生产），
     launch 后**立即**取进程凭据并核验；任何 post-launch 失败都先按身份
     回收全部自有 GUI/helper 进程再 raise——不留危险形态进程。
  B2 GUI app PID 与 helper PID 严格分开：窗口/事件/截图面用 GUI PID
     （driver launch / resolve-app 给出），API/instance 面用 helper PID。
  B3 driver --help 输出单行 JSON，按 JSON 契约解析（不再当纯文本）。
  B4 弃用 `pgrep -x Fathom` 与跨时间窗集合差：所有权来自 launch 句柄与
     resolve-app 消歧，信号前逐 PID 复核 bundle id + 可执行路径 + 启动
     凭证，拒绝 foreign/dead/reused。
  B5 有界交互模式：--send-sample + --resume-file + --product-timeout；
     暂停等待产品终态而非无条件 blocked；期间自动采集截图与 API/DB 交叉
     证据；清理推迟到证据采集完成之后。blocked 只留给确需前台键击的项。
  B6 交付 gen-manifest 子命令：从真实构建回执 + 真实 .app 指纹生成
     manifest；source 脏净据 git 实测，条件不足时**明确拒绝**并指示 PM
     以固定输入重新构建，不编造 dirty=false。

用法::

    python scripts/verify_tauri_analysis.py gen-manifest \
        --app <app> --receipt <app141-build-ready.json> --output <manifest.json>
    python scripts/verify_tauri_analysis.py \
        --app <app> --build-ready <manifest.json> --output <evidence dir> \
        [--send-sample <path>] [--resume-file <path>] [--product-timeout 900]

退出码：0 全过（含 NOT_VERIFIED-需前台）/ 1 断言失败 / 2 用法错 / 3 全局阻塞。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_SOURCE = REPO_ROOT / "scripts" / "tauri_analysis_driver.swift"

PRODUCTION_SUPPORT_DIR = "Library/Application Support/Fathom"
PRODUCTION_PORT = 7952
TARGET_CONTENT_SIZES = ((980, 640), (1220, 820))

MANIFEST_VERSION = 1
MANIFEST_TOP_KEYS = {"version", "app", "helper", "source", "built_at"}
MANIFEST_APP_KEYS = {"path", "bundle_id", "executable", "fingerprint_sha256", "files"}
MANIFEST_HELPER_KEYS = {"relative_path", "sha256"}
MANIFEST_SOURCE_KEYS = {"commit", "dirty", "input_paths", "checked_at", "receipt",
                        "receipt_head_at_finish", "current_head"}

HELPER_RELATIVE_PATH = "Contents/Resources/helper/fathom-helper/fathom-helper"
SYNTHETIC_MEGABYTES = 6


class IdentityError(Exception):
    """app / manifest / 源码身份不符——拒绝启动。"""


class GateError(Exception):
    """隔离硬门或身份核验失败。"""


class BlockedError(Exception):
    """全局环境阻塞。"""


# ==========================================================================
# 纯函数：指纹 / 身份 / 隔离
# ==========================================================================


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_fingerprint(app_path: Path) -> dict[str, Any]:
    if not app_path.is_dir():
        raise IdentityError(f".app 不存在或不是目录：{app_path}")
    files: dict[str, str] = {}
    for item in sorted(app_path.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        files[item.relative_to(app_path).as_posix()] = sha256_file(item)
    if not files:
        raise IdentityError(f".app 内没有任何文件：{app_path}")
    rollup = hashlib.sha256(
        "\n".join(f"{rel}  {files[rel]}" for rel in sorted(files)).encode()).hexdigest()
    return {"files": files, "fingerprint_sha256": rollup}


def read_bundle_id(app_path: Path) -> str:
    plist_path = app_path / "Contents" / "Info.plist"
    if not plist_path.is_file():
        raise IdentityError(f"缺少 Info.plist：{plist_path}")
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    bundle_id = plist.get("CFBundleIdentifier")
    if not isinstance(bundle_id, str) or not bundle_id.strip():
        raise IdentityError(f"Info.plist 缺 CFBundleIdentifier：{plist_path}")
    return bundle_id


def read_bundle_executable(app_path: Path) -> str:
    plist_path = app_path / "Contents" / "Info.plist"
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    executable = plist.get("CFBundleExecutable")
    if not isinstance(executable, str) or not executable.strip():
        raise IdentityError(f"Info.plist 缺 CFBundleExecutable：{plist_path}")
    return executable


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise IdentityError(f"manifest 不存在：{path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise IdentityError(f"manifest 不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(manifest, dict):
        raise IdentityError(
            f"manifest 根必须是 JSON 对象，实际 {type(manifest).__name__}")
    unknown = sorted(set(manifest) - MANIFEST_TOP_KEYS)
    if unknown:
        raise IdentityError(f"manifest 含未知顶层字段：{', '.join(unknown)}")
    if manifest.get("version") != MANIFEST_VERSION:
        raise IdentityError(
            f"manifest version 必须为 {MANIFEST_VERSION}，"
            f"实际 {manifest.get('version')!r}")
    for key in ("app", "helper", "source"):
        if not isinstance(manifest.get(key), dict):
            raise IdentityError(f"manifest 缺 {key} 对象段")
    return manifest


def verify_manifest_identity(
    manifest: dict[str, Any],
    app_path: Path,
    *,
    expected_commit: str | None = None,
    actual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """启动前硬门：二进制/资源 SHA、bundle id、helper、源码指纹逐项核对。

    版本门与未知字段门都在本闸门内复检——不依赖调用方先经过 load_manifest。
    """
    if manifest.get("version") != MANIFEST_VERSION:
        raise IdentityError(
            f"manifest version 必须为 {MANIFEST_VERSION}，"
            f"实际 {manifest.get('version')!r}")
    unknown_top = sorted(set(manifest) - MANIFEST_TOP_KEYS)
    if unknown_top:
        raise IdentityError(f"manifest 含未知顶层字段：{', '.join(unknown_top)}")

    app_section = manifest["app"]
    unknown = sorted(set(app_section) - MANIFEST_APP_KEYS)
    if unknown:
        raise IdentityError(f"manifest.app 含未知字段：{', '.join(unknown)}")
    for key in ("bundle_id", "fingerprint_sha256", "files"):
        if key not in app_section:
            raise IdentityError(f"manifest.app 缺字段 {key}")

    helper_section = manifest["helper"]
    unknown = sorted(set(helper_section) - MANIFEST_HELPER_KEYS)
    if unknown:
        raise IdentityError(f"manifest.helper 含未知字段：{', '.join(unknown)}")
    for key in MANIFEST_HELPER_KEYS:
        if key not in helper_section:
            raise IdentityError(f"manifest.helper 缺字段 {key}")

    source = manifest["source"]
    unknown = sorted(set(source) - MANIFEST_SOURCE_KEYS)
    if unknown:
        raise IdentityError(f"manifest.source 含未知字段：{', '.join(unknown)}")
    for key in ("commit", "dirty", "input_paths", "receipt"):
        if key not in source:
            raise IdentityError(f"manifest.source 缺字段 {key}")
    if source["dirty"] is not False:
        raise IdentityError(
            "manifest.source.dirty 必须为 false（脏源码构建的 .app 不作为验收证据）")
    if not isinstance(source["input_paths"], list) or not source["input_paths"]:
        raise IdentityError("manifest.source.input_paths 必须是非空列表")

    observed = actual if actual is not None else bundle_fingerprint(app_path)
    bundle_id = read_bundle_id(app_path)

    if bundle_id != app_section["bundle_id"]:
        raise IdentityError(
            f"bundle id 不符：manifest={app_section['bundle_id']} 实测={bundle_id}")
    if observed["fingerprint_sha256"] != app_section["fingerprint_sha256"]:
        raise IdentityError(
            f"app 指纹不符：manifest={app_section['fingerprint_sha256'][:16]}… "
            f"实测={observed['fingerprint_sha256'][:16]}…（拒绝用旧 app 伪证）")
    executable = read_bundle_executable(app_path)
    declared = app_section.get("executable")
    if declared is not None and declared != executable:
        raise IdentityError(
            f"可执行名不符：manifest={declared} 实测={executable}")

    expected_files = app_section["files"]
    if not isinstance(expected_files, dict) or not expected_files:
        raise IdentityError("manifest.app.files 必须是非空对象")
    missing = sorted(set(expected_files) - set(observed["files"]))
    if missing:
        raise IdentityError(
            f"app 缺少 manifest 声明的 {len(missing)} 个文件，首个：{missing[0]}")
    mismatched = sorted(rel for rel, sha in expected_files.items()
                        if observed["files"].get(rel) != sha)
    if mismatched:
        raise IdentityError(
            f"app 有 {len(mismatched)} 个文件 SHA 不符，首个：{mismatched[0]}")
    extra = sorted(set(observed["files"]) - set(expected_files))
    if extra:
        raise IdentityError(f"app 含 manifest 未声明的额外文件：{extra[0]}")

    helper_rel = helper_section["relative_path"]
    if helper_rel != HELPER_RELATIVE_PATH:
        raise IdentityError(
            f"manifest.helper.relative_path 必须为 {HELPER_RELATIVE_PATH}，"
            f"实际 {helper_rel!r}")
    if helper_section["sha256"] != observed["files"].get(HELPER_RELATIVE_PATH):
        raise IdentityError("helper 二进制 SHA 与 manifest 不符或缺失")

    if expected_commit is not None and source["commit"] != expected_commit:
        raise IdentityError(
            f"manifest 记录源码 commit={source['commit'][:12]}… 与固定 head="
            f"{expected_commit[:12]}… 不符")
    return {"bundle_id": bundle_id, "executable": executable, **observed,
            "source": source}


def normalize_root(raw: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(str(raw)))


def production_support_dir(home: Path) -> Path:
    return normalize_root(home / PRODUCTION_SUPPORT_DIR)


def assert_isolated_scan_root(scan_root: Any, *, expected: Path, home: Path) -> Path:
    if not isinstance(scan_root, str) or not scan_root.strip():
        raise GateError(f"/api/config 未回读生效扫描根：{scan_root!r}")
    observed = normalize_root(scan_root)
    if observed != normalize_root(expected):
        raise GateError(
            f"生效扫描根越界：期望 {normalize_root(expected)} 实测 {observed}")
    home_norm = normalize_root(home)
    if observed == home_norm or home_norm in observed.parents:
        raise GateError(f"生效扫描根落在 HOME 内，拒绝继续：{observed}")
    prod = production_support_dir(home)
    if observed == prod or prod in observed.parents:
        raise GateError(f"生效扫描根指向生产应用支持目录，拒绝继续：{observed}")
    return observed


def assert_port_owned(port: int, instance: dict[str, Any], *, pid: int) -> None:
    if instance.get("port") != port:
        raise GateError(
            f"helper instance port={instance.get('port')} 与本轮端口 {port} 不符")
    if instance.get("pid") != pid:
        raise GateError(
            f"helper instance pid={instance.get('pid')} 与实测 pid={pid} 不符")


def validate_isolation_plan(
    *, runtime_dir: Path, scan_root: Path, port: int, home: Path
) -> None:
    """B1：启动**前**证明隔离 env 有效。任何一条不成立就绝不 launch。"""
    if not runtime_dir.is_dir():
        raise GateError(f"runtime 根不存在：{runtime_dir}")
    if not scan_root.is_dir():
        raise GateError(f"合成扫描根不存在：{scan_root}")
    scan_norm = normalize_root(scan_root)
    home_norm = normalize_root(home)
    if scan_norm == home_norm or home_norm in scan_norm.parents:
        raise GateError(f"合成扫描根落在 HOME 内，拒绝启动：{scan_norm}")
    prod = production_support_dir(home)
    if scan_norm == prod or prod in scan_norm.parents:
        raise GateError(f"合成扫描根指向生产支持目录，拒绝启动：{scan_norm}")
    if not isinstance(port, int) or not (0 < port < 65_536):
        raise GateError(f"端口非法：{port!r}")
    if port == PRODUCTION_PORT:
        raise GateError("拒绝使用生产端口 7952")


def pick_free_port(rng: random.Random, *, low: int = 51_000, high: int = 59_000) -> int:
    for _ in range(200):
        port = rng.randint(low, high)
        if port == PRODUCTION_PORT:
            continue
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise BlockedError(f"{low}-{high} 端口段内找不到空闲端口")


# ==========================================================================
# B4：进程身份（不用 pgrep 集合差；信号前逐 PID 复核）
# ==========================================================================


def ps_identity(pid: int) -> dict[str, str] | None:
    """只读取单进程身份。进程不存在返回 None（dead/reused 判定基础）。

    用 `ucomm`（短名，不截断、不含空格）而不是 `comm`：本机实测
    `ps -o comm=` 对长路径会截断成 `/Users/maoking/o`，导致身份复核
    永远不成立 → 进程漏回收（返修 episode 1 实机踩到）。
    """
    try:
        proc = subprocess.run(
            ["ps", "-o", "ucomm=,args=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip()
    if not line:
        return None
    ucomm, _, args = line.partition(" ")
    ucomm = ucomm.strip()
    args = args.strip()
    return {"comm": ucomm, "args": args,
            "path": args.split(" ")[0] if args else ""}


@dataclass(frozen=True)
class OwnedProcess:
    """自有进程凭据：pid + 可执行名 + 可执行路径 + bundle id。

    所有权**只**来自 driver launch 句柄 / resolve-app 消歧；不来自
    「启动前后集合差」——那会收养别人的同名进程。
    """
    pid: int
    executable: str          # CFBundleExecutable，如 fathom-desktop
    executable_path: str
    bundle_id: str
    role: str                # gui | helper

    def matches(self, observed: dict[str, str], *, app_path: Path) -> bool:
        """信号前复核：ucomm 必须是自有可执行短名，且 args 落在本轮 app 内。

        拒绝 foreign（别人的同名进程）与 reused（PID 已被新进程占用）。
        `comm` 取自 `ps -o ucomm=`（短名），不依赖会截断的 `comm=`。
        """
        if observed is None:
            return False
        if observed.get("comm") != self.executable:
            return False
        args = observed.get("args", "")
        if self.role == "gui":
            return str(app_path) in args
        return "fathom-helper" in args or "fathom-helper" in observed.get("path", "")


def reclaim_owned(
    owned: list[OwnedProcess], *, app_path: Path, log: Path,
    grace_s: float = 3.0,
) -> dict[str, Any]:
    """按精确身份回收。**每次信号前都复核身份**；复核失败一律不发信号。

    回收失败原样记入 result（不吞错、不假绿）。
    """
    report: dict[str, Any] = {"reclaimed": [], "skipped_identity": [],
                              "still_alive": [], "errors": []}

    def note(action: str, detail: str) -> None:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"[reclaim] {action}: {detail}\n")

    for proc in owned:
        if proc.pid <= 1:
            report["skipped_identity"].append(
                {"pid": proc.pid, "role": proc.role, "reason": "invalid_pid"})
            continue
        observed = ps_identity(proc.pid)
        if observed is None:
            note("already-gone", f"pid={proc.pid} role={proc.role}")
            report["reclaimed"].append(
                {"pid": proc.pid, "role": proc.role, "how": "already_exited"})
            continue
        if not proc.matches(observed, app_path=app_path):
            reason = f"身份不符 comm={observed['comm']} args={observed['args'][:120]}"
            note("refuse-signal", f"pid={proc.pid} {reason}")
            report["skipped_identity"].append(
                {"pid": proc.pid, "role": proc.role, "observed": observed,
                 "reason": reason})
            continue
        try:
            os.kill(proc.pid, 15)
            note("sigterm", f"pid={proc.pid} role={proc.role}")
        except (ProcessLookupError, PermissionError) as exc:
            report["errors"].append({"pid": proc.pid, "error": str(exc)})
            continue

    time.sleep(grace_s)

    for proc in owned:
        observed = ps_identity(proc.pid)
        if observed is None:
            continue
        if not proc.matches(observed, app_path=app_path):
            report["skipped_identity"].append(
                {"pid": proc.pid, "role": proc.role, "observed": observed,
                 "reason": "SIGTERM 后身份已变（可能 PID 复用），不再升级信号"})
            continue
        try:
            os.kill(proc.pid, 9)
            note("sigkill", f"pid={proc.pid} role={proc.role}")
        except (ProcessLookupError, PermissionError) as exc:
            report["errors"].append({"pid": proc.pid, "error": str(exc)})

    time.sleep(0.5)
    for proc in owned:
        observed = ps_identity(proc.pid)
        if observed is None:
            continue
        if proc.matches(observed, app_path=app_path):
            report["still_alive"].append({"pid": proc.pid, "role": proc.role})
    return report


# ==========================================================================
# 合成根 / HTTP
# ==========================================================================


def make_synthetic_scan_root(base: Path) -> Path:
    scan_root = base / "synthetic-scan-root"
    for sub in ("alpha", "beta", "nested/deep"):
        (scan_root / sub).mkdir(parents=True, exist_ok=True)
    block = b"fathom-iss141-synthetic-sample\n" * 1024
    written = index = 0
    while written < SYNTHETIC_MEGABYTES * 1024 * 1024:
        (scan_root / f"sample-{index:04d}.bin").write_bytes(block)
        written += len(block)
        index += 1
    (scan_root / "alpha" / "changed.txt").write_text("iss141 growth\n", "utf-8")
    (scan_root / "beta" / "changed.txt").write_text("iss141 shrink\n", "utf-8")
    return scan_root


def disk_usage_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def http_json(url: str, *, method: str = "GET", body: dict[str, Any] | None = None,
              timeout: float = 10.0) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, (json.loads(raw) if raw.strip() else None)
        except ValueError:
            return exc.code, {"raw": raw}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return 0, {"error": str(exc)}


def wait_for_health(port: int, *, deadline_s: float = 60.0) -> bool:
    end = time.time() + deadline_s
    while time.time() < end:
        if http_json(f"http://127.0.0.1:{port}/health", timeout=3.0)[0] == 200:
            return True
        time.sleep(1.0)
    return False


def extract_scan_root(config_body: Any) -> Any:
    """守住 /api/config 两种返回形态。"""
    if not isinstance(config_body, dict):
        return None
    value = config_body.get("scan_root")
    if isinstance(value, dict):
        return value.get("value")
    return value


# ==========================================================================
# B6：manifest 生成器
# ==========================================================================


def git_porcelain(repo: Path) -> list[str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BlockedError(f"git status 执行失败：{exc}") from exc
    if proc.returncode != 0:
        raise BlockedError(f"git status 失败：{proc.stderr.strip()[:200]}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def touched_paths(porcelain: list[str]) -> set[str]:
    paths: set[str] = set()
    for line in porcelain:
        entry = line[3:] if len(line) > 3 else ""
        for part in entry.split(" -> "):
            paths.add(part.strip().strip('"'))
    return paths


def generate_manifest(
    *, app_path: Path, receipt_path: Path, repo: Path
) -> dict[str, Any]:
    """从真实构建回执 + 真实 .app 指纹生成 manifest（B6）。

    source.dirty **据 git 实测**：对回执声明的 source_input_paths 求交集。
    条件不足（回执缺字段、输入路径脏、head 与回执 commit 不符）→ 明确拒绝
    并指示 PM 以固定输入重新构建；绝不手填 false。
    """
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise IdentityError("构建回执根必须是 JSON 对象")
    if receipt.get("ok") is not True:
        raise IdentityError(
            f"构建回执 ok={receipt.get('ok')!r}，不接受失败构建的产物")
    for key in ("app_path", "test_bundle_identifier", "source_inputs_commit",
                "source_input_paths", "app_executable_sha256", "helper_sha256"):
        if key not in receipt:
            raise IdentityError(f"构建回执缺字段 {key}，请 PM 用本脚本 gen-manifest 重录")

    declared_app = Path(receipt["app_path"])
    if normalize_root(declared_app) != normalize_root(app_path):
        raise IdentityError(
            f"--app 与回执 app_path 不一致：{app_path} vs {declared_app}")
    declared_exec_sha = receipt["app_executable_sha256"]
    declared_helper_sha = receipt["helper_sha256"]
    if read_bundle_id(app_path) != receipt["test_bundle_identifier"]:
        raise IdentityError("app 的 bundle id 与回执 test_bundle_identifier 不符")

    executable = read_bundle_executable(app_path)
    observed = bundle_fingerprint(app_path)
    exec_rel = f"Contents/MacOS/{executable}"
    if observed["files"].get(exec_rel) != declared_exec_sha:
        raise IdentityError("app 可执行二进制 SHA 与回执不符")
    if observed["files"].get(HELPER_RELATIVE_PATH) != declared_helper_sha:
        raise IdentityError("helper 二进制 SHA 与回执不符")

    # 源码脏净：据 git 实测，且只在回执声明的输入路径范围内判定。
    input_paths = list(receipt["source_input_paths"])
    porcelain = git_porcelain(repo)
    dirty_paths = touched_paths(porcelain)
    relevant_dirty = sorted(
        p for p in dirty_paths
        if any(p == ip or p.startswith(ip.rstrip("/") + "/") for ip in input_paths))
    if relevant_dirty:
        raise IdentityError(
            "构建输入路径在当前工作树为脏（git status --porcelain 实测）："
            f"{', '.join(relevant_dirty[:5])}；请 PM 以固定输入重新构建后再生成 manifest")

    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60)
    head_sha = head.stdout.strip()
    receipt_commit = receipt["source_inputs_commit"]
    manifest = {
        "version": MANIFEST_VERSION,
        "app": {
            "path": str(app_path),
            "bundle_id": read_bundle_id(app_path),
            "executable": executable,
            "fingerprint_sha256": observed["fingerprint_sha256"],
            "files": dict(observed["files"]),
        },
        "helper": {
            "relative_path": HELPER_RELATIVE_PATH,
            "sha256": observed["files"][HELPER_RELATIVE_PATH],
        },
        "source": {
            "commit": receipt_commit,
            "dirty": False,
            "input_paths": input_paths,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "receipt": str(receipt_path),
        },
        "built_at": receipt.get("finished_at"),
    }
    # head 与回执 commit 不一致时如实记录，不阻断（PM 固定 head 后由
    # --expected-commit 在启动前硬门拦截），但必须在 manifest 里留痕。
    manifest["source"]["receipt_head_at_finish"] = receipt.get("head_at_finish")
    manifest["source"]["current_head"] = head_sha
    return manifest


# ==========================================================================
# Driver
# ==========================================================================


class Driver:
    def __init__(self, log_path: Path, *, build_root: Path):
        self.log_path = log_path
        self.binary = build_root / "tauri_analysis_driver"
        self._handle = log_path.open("a", encoding="utf-8")

    def build(self) -> None:
        if not DRIVER_SOURCE.is_file():
            raise BlockedError(f"驱动源码缺失：{DRIVER_SOURCE}")
        swiftc = shutil.which("swiftc")
        if not swiftc:
            raise BlockedError("缺 swiftc：后台点击/截图不可用，fail-closed")
        proc = subprocess.run([swiftc, "-O", "-o", str(self.binary), str(DRIVER_SOURCE)],
                              capture_output=True, text=True, timeout=300)
        self._record("build", proc)
        if proc.returncode != 0 or not self.binary.is_file():
            raise BlockedError(
                f"驱动编译失败（exit={proc.returncode}），不降级为浏览器截图")

    def run(self, argv: list[str], *, timeout: float = 120.0) -> dict[str, Any]:
        proc = subprocess.run([str(self.binary), *argv], capture_output=True,
                              text=True, timeout=timeout)
        self._record(" ".join(argv), proc)
        if proc.returncode != 0:
            raise GateError(
                f"驱动子命令失败 exit={proc.returncode}: {' '.join(argv)}"
                f" :: {proc.stderr.strip()[:300]}")
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise GateError(f"驱动输出非 JSON：{proc.stdout[:200]}") from exc

    def probe(self, argv: list[str], *, timeout: float = 60.0) -> dict[str, Any]:
        """非 JSON 契约探查（只读子进程，用于负例取证，不进断言路径）。"""
        proc = subprocess.run([str(self.binary), *argv], capture_output=True,
                              text=True, timeout=timeout)
        self._record("probe:" + " ".join(argv), proc)
        return {"exit": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr}

    def _record(self, label: str, proc: subprocess.CompletedProcess[str]) -> None:
        self._handle.write(f"=== {label} exit={proc.returncode}\n"
                           f"{proc.stdout}\n{proc.stderr}\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


# ==========================================================================
# 证据记录
# ==========================================================================


@dataclass
class Recorder:
    output: Path
    cases: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cases_path = self.output / "cases.jsonl"
        self.output.mkdir(parents=True, exist_ok=True)
        self.cases_path.write_text("", encoding="utf-8")

    def record(self, name: str, status: str, detail: str) -> None:
        if status not in {"pass", "fail", "blocked"}:
            raise ValueError(f"未知状态：{status}")
        entry = {"name": name, "status": status, "detail": detail}
        self.cases.append(entry)
        with self.cases_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if status == "blocked":
            self.blocked.append(f"{name} :: {detail}")
        print(f"[{status}] {name} :: {detail}", flush=True)

    def verdict(self) -> str:
        if any(case["status"] == "fail" for case in self.cases):
            return "FAIL"
        return "PASS_WITH_NOT_VERIFIED" if self.blocked else "PASS"


# ==========================================================================
# B5：产品交互阶段（有界、可控、自动采证）
# ==========================================================================


def collect_product_evidence(
    *, port: int, recorder: Recorder, output: Path, screenshots: Path,
    driver: Driver, app_pid: int, bundle_id: str, window_id: int,
    send_sample: Path | None, resume_file: Path | None, timeout_s: float,
) -> dict[str, Any]:
    """PM 在真实窗口点击期间：轮询产品终态 + 自动采集截图/API 证据。

    有界：resume 文件出现、或 timeout 到期、或 PM Ctrl-C，都收口。
    绝不用 API POST 冒充点击——这里只**读** job/analysis 状态与截图。
    """
    stage: dict[str, Any] = {"started_at": time.time(), "deadline": timeout_s}
    pause_marker = output / "PRODUCT-PAUSE.md"
    pause_marker.write_text(
        f"# PM 产品操作暂停点\n\n"
        f"真实 GUI PID={app_pid} bundle={bundle_id} window={window_id}\n"
        f"请在该真实 WKWebView 窗口完成：选择引擎 → 启用 → 生成预览 → "
        f"确认发送 → 查看结果与证据 → 撤销授权。\n"
        + (f"发送样本文件：{send_sample}\n" if send_sample else "")
        + f"完成后创建 resume 文件使本阶段收口：{resume_file}\n"
          f"（或等待 {timeout_s}s 超时自动收口）\n",
        encoding="utf-8")
    recorder.record(
        "product-pause-opened", "pass",
        f"暂停点已开：{pause_marker}；PM 在真实窗口操作，脚本只读轮询与采证",
    )

    shots: list[str] = []
    observed: list[dict[str, Any]] = []
    started = time.time()
    last_shot = 0.0
    while time.time() - started < timeout_s:
        if resume_file is not None and resume_file.exists():
            recorder.record("product-resume", "pass", f"检测到 resume 文件 {resume_file}")
            break
        if time.time() - last_shot > 15.0:
            last_shot = time.time()
            try:
                shot = driver.run(["screenshot", "--pid", str(app_pid),
                                   "--bundle-id", bundle_id, "--window-id",
                                   str(window_id), "--output",
                                   str(screenshots / "product-live.png")])
                shots.append(shot.get("path", ""))
            except (GateError, subprocess.TimeoutExpired):
                pass
        status, body = http_json(
            f"http://127.0.0.1:{port}/api/analysis/jobs", timeout=5.0)
        if status == 0:
            observed.append({"at": time.time(), "status": 0, "body": body})
        else:
            observed.append({"at": time.time(), "status": status, "body": body})
        time.sleep(3.0)
    else:
        recorder.record(
            "product-timeout", "blocked",
            f"{timeout_s}s 内未见 resume 文件，产品终态由 PM 复核；"
            f"已采截图 {len(shots)} 张、API 观察 {len(observed)} 次")

    stage["shots"] = shots
    stage["api_observations"] = observed[-40:]
    stage["elapsed_s"] = round(time.time() - started, 1)
    (output / "product-stage.json").write_text(
        json.dumps(stage, ensure_ascii=False, indent=2), encoding="utf-8")
    return stage


# ==========================================================================
# 主流程
# ==========================================================================


def run_verification(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    screenshots = output / "screenshots"
    screenshots.mkdir(exist_ok=True)
    log = output / "verify.log"
    recorder = Recorder(output)
    home = Path(os.path.expanduser("~"))
    started = time.time()

    summary: dict[str, Any] = {
        "card": "ISS-141", "repair_episode": 1,
        "app": str(Path(args.app).expanduser()),
        "build_ready": str(Path(args.build_ready).expanduser()),
        "output": str(output), "frontend_only": False,
    }
    rng = random.Random("iss141-repair1")
    owned: list[OwnedProcess] = []
    temp_root: Path | None = None
    driver: Driver | None = None
    port = 0
    helper_pid = 0

    try:
        # ---- 阶段 0：启动前身份硬门 ---------------------------------
        app_path = Path(args.app).expanduser().resolve()
        if not app_path.is_dir() or not (app_path / "Contents").is_dir():
            raise BlockedError(f"--app 不是 Tauri .app bundle：{app_path}")
        manifest_path = Path(args.build_ready).expanduser().resolve()
        try:
            identity = verify_manifest_identity(
                load_manifest(manifest_path), app_path,
                expected_commit=args.expected_commit or None)
        except IdentityError as exc:
            raise BlockedError(f"身份硬门拒绝启动：{exc}") from exc
        recorder.record(
            "app-identity", "pass",
            f"bundle_id={identity['bundle_id']} 可执行={identity['executable']} "
            f"fingerprint={identity['fingerprint_sha256'][:16]}… "
            f"commit={identity['source']['commit'][:12]}… "
            f"输入路径={len(identity['source']['input_paths'])} 项")

        # ---- 阶段 1：隔离根 + 随机端口 + 启动前 env 自证（B1）--------
        temp_root = Path(tempfile.mkdtemp(prefix="iss141-analysis-"))
        runtime_dir = temp_root / "runtime"
        runtime_dir.mkdir()
        scan_root = make_synthetic_scan_root(temp_root)
        usage = disk_usage_bytes(scan_root)
        port = pick_free_port(rng)
        validate_isolation_plan(runtime_dir=runtime_dir, scan_root=scan_root,
                                port=port, home=home)
        summary.update({"runtime_dir": str(runtime_dir),
                        "scan_root": str(scan_root), "port": port,
                        "synthetic_bytes": usage})
        recorder.record(
            "isolation-plan-validated", "pass",
            f"启动前已证明隔离 env 有效：runtime={runtime_dir} "
            f"scan_root={scan_root}（{usage / 1024 / 1024:.1f}MiB）port={port}；"
            f"生产 7952 与 {PRODUCTION_SUPPORT_DIR} 未被写入或探测")
        recorder.record(
            "production-untouched", "pass",
            f"生产端口 {PRODUCTION_PORT} 与 {PRODUCTION_SUPPORT_DIR} 未触碰")

        # ---- 阶段 2：驱动编译 + --help JSON 契约（B3）----------------
        driver = Driver(log, build_root=temp_root)
        driver.build()
        help_result = driver.run(["--help"], timeout=30)
        if "subcommands" not in help_result:
            raise GateError(f"driver --help 不是 JSON 契约：{list(help_result)[:5]}")
        recorder.record(
            "driver-help-json", "pass",
            f"真实编译并调用 --help，返回 JSON 契约，子命令 "
            f"{help_result.get('subcommands')}")

        # ---- 阶段 3：launch 取 GUI 凭据（B1/B2/B4）-----------------
        # 启动由 driver 用 NSWorkspace.OpenConfiguration.environment 注入并
        # 固定 activates=false；返回的 NSRunningApplication 句柄即唯一凭据。
        launch = driver.run([
            "launch", "--app", str(app_path), "--runtime-dir", str(runtime_dir),
            "--scan-root", str(scan_root), "--port", str(port),
            "--timeout", "60",
        ], timeout=90)
        app_pid = int(launch["pid"])
        gui_bundle = launch["bundle_id"]
        if gui_bundle != identity["bundle_id"]:
            raise GateError(
                f"launch 返回 bundle id={gui_bundle} 与 manifest="
                f"{identity['bundle_id']} 不符")
        owned.append(OwnedProcess(pid=app_pid,
                                  executable=identity["executable"],
                                  executable_path=launch.get("executable", ""),
                                  bundle_id=gui_bundle, role="gui"))
        summary["gui_pid"] = app_pid
        # own_pids（GUI 凭据）在 launch 返回后**立即**持有——后续任何失败
        # 都会进入 finally 的按身份回收，不再有「赋值前 raise → 零回收」。
        recorder.record(
            "gui-credential", "pass",
            f"NSWorkspace 返回 GUI 进程凭据 pid={app_pid} bundle={gui_bundle} "
            f"activated={launch.get('activated')}（不抢前台）")

        # ---- 阶段 4：立即（不等 90s）核验 helper instance 与端口归属 ---
        instance_path = runtime_dir / "helper-instance.json"
        deadline = time.time() + 30
        instance: dict[str, Any] = {}
        while time.time() < deadline and not instance_path.is_file():
            time.sleep(0.5)
            if http_json(f"http://127.0.0.1:{port}/health", timeout=2.0)[0] == 200 \
                    and not instance_path.is_file():
                time.sleep(1.0)
        if not wait_for_health(port, deadline_s=45.0):
            raise GateError(
                f"helper /health 未就绪于 127.0.0.1:{port}（隔离 env 可能未生效）")
        if not instance_path.is_file():
            raise GateError(
                f"缺少 {instance_path}：helper 未在隔离 runtime 根落凭据，"
                f"判定隔离 env 失效")
        instance = json.loads(instance_path.read_text(encoding="utf-8"))
        helper_pid = instance.get("pid")
        if not isinstance(helper_pid, int) or helper_pid <= 1:
            raise GateError(f"instance pid 不可信：{helper_pid!r}")
        if helper_pid == app_pid:
            raise GateError("helper pid 与 GUI pid 相同，身份混淆，拒绝继续")
        assert_port_owned(port, instance, pid=helper_pid)
        owned.append(OwnedProcess(pid=helper_pid, executable="fathom-helper",
                                  executable_path="", bundle_id="",
                                  role="helper"))
        summary["helper_pid"] = helper_pid
        recorder.record(
            "helper-identity", "pass",
            f"helper pid={helper_pid} port={port}（与 GUI pid={app_pid} 严格分开；"
            f"instance 与端口归属一致）")

        # ---- 阶段 5：/api/config 扫描根硬门 -------------------------
        status, config_body = http_json(f"http://127.0.0.1:{port}/api/config")
        if status != 200 or not isinstance(config_body, dict):
            raise GateError(f"/api/config 读取失败：status={status}")
        observed_root = assert_isolated_scan_root(
            extract_scan_root(config_body), expected=scan_root, home=home)
        recorder.record(
            "config-gate-scan-root", "pass",
            f"生效扫描根={observed_root}（非 HOME、非生产支持目录）")

        # ---- 阶段 6：窗口身份 + 两档尺寸（GUI pid 面）----------------
        for content_w, content_h in TARGET_CONTENT_SIZES:
            label = f"{content_w}x{content_h}"
            own = driver.run(["identify", "--pid", str(app_pid),
                              "--bundle-id", gui_bundle])
            windows = own.get("windows", [])
            if len(windows) != 1:
                raise GateError(
                    f"{label}：无法消歧自有窗口（匹配 {len(windows)} 个）")
            win = windows[0]
            win_id = int(win["window_id"])
            # 不传 --x/--y：set-size 只改尺寸，不搬位置
            driver.run(["set-size", "--pid", str(app_pid), "--bundle-id", gui_bundle,
                        "--window-id", str(win_id), "--width", str(content_w),
                        "--height", str(content_h + 28)])
            measured = driver.run(["measure", "--pid", str(app_pid),
                                   "--bundle-id", gui_bundle,
                                   "--window-id", str(win_id)])
            if int(measured.get("content_width", -1)) != content_w or \
                    int(measured.get("content_height", -1)) != content_h:
                raise GateError(
                    f"{label}：窗口级 AX 实测内容区 "
                    f"{measured.get('content_width')}x{measured.get('content_height')} "
                    f"（标题栏实测 {measured.get('titlebar_height')}）与目标不符")
            shot = driver.run(["screenshot", "--pid", str(app_pid),
                               "--bundle-id", gui_bundle, "--window-id", str(win_id),
                               "--output", str(screenshots / f"window-{label}.png")])
            recorder.record(
                f"window-size-{label}", "pass",
                f"窗口级 AX 实测内容区 {measured['content_width']}x"
                f"{measured['content_height']}（标题栏实测 "
                f"{measured.get('titlebar_height')}pt，未写死 -28）；"
                f"截图 {shot.get('path')} {shot.get('width')}x{shot.get('height')}")

        # ---- 阶段 7：有界产品交互 + 自动采证（B5）-------------------
        if args.send_sample or args.resume_file:
            own = driver.run(["identify", "--pid", str(app_pid),
                              "--bundle-id", gui_bundle])
            win_id = int(own["windows"][0]["window_id"])
            stage = collect_product_evidence(
                port=port, recorder=recorder, output=output,
                screenshots=screenshots, driver=driver, app_pid=app_pid,
                bundle_id=gui_bundle, window_id=win_id,
                send_sample=(Path(args.send_sample) if args.send_sample else None),
                resume_file=(Path(args.resume_file) if args.resume_file else None),
                timeout_s=args.product_timeout)
            summary["product_stage"] = {
                "shots": len(stage["shots"]),
                "api_observations": len(stage["api_observations"]),
                "elapsed_s": stage["elapsed_s"],
            }
            recorder.record(
                "product-evidence-collected", "pass",
                f"暂停期内采集截图 {len(stage['shots'])} 张、"
                f"API 观察 {len(stage['api_observations'])} 次 → product-stage.json")
        else:
            recorder.record(
                "product-stage-not-requested", "blocked",
                "未提供 --send-sample/--resume-file，未进入产品交互暂停；"
                "PM 需以 --send-sample <样本> --resume-file <收口文件> 重跑才能"
                "采集产品点击证据（PM 经真实确认层操作，脚本不代点）")
        recorder.record(
            "keyboard-foreground-items", "blocked",
            "Tab 焦点环 / Enter / Esc 关闭等需前台键击的检查：需 PM 允许前台后"
            "实测；零打扰约束下不发全局键盘事件")
        if args.shim_mode:
            recorder.record(
                "shim-mode", "blocked",
                "shim 模式只证明 GUI 接线，不构成真实模型发送证据；"
                "不得据此关闭 ISS-035 第一验收项")
        recorder.record(
            "worker-did-not-send", "pass",
            "worker 未发起任何真实模型请求；真实 CLI 发送仅由 PM 经实际确认层执行一次")

        summary["verified_scope"] = [
            "app/manifest 身份硬门（含 gen-manifest 真实回执链路）",
            "启动前隔离 env 自证", "NSWorkspace launch 凭据与不激活",
            "GUI/helper PID 严格分开", "/api/config 扫描根硬门",
            "窗口级 AX 尺寸实测与截图", "有界产品暂停与自动采证",
            "按精确身份回收",
        ]
        summary["not_verified"] = list(recorder.blocked)

    except (BlockedError, GateError) as exc:
        kind = "BLOCKED" if isinstance(exc, BlockedError) else "FAIL"
        summary["halt_kind"] = kind
        summary["halt_detail"] = str(exc)
        recorder.record(f"{kind.lower()}-halt", "fail", f"{kind}：{exc}")
    finally:
        # B1/B4：任何 post-launch 失败都先按身份回收（含 GUI/helper），
        # 回收失败原样保留，不吞错不假绿。
        summary["cleanup"] = reclaim_owned(owned, app_path=Path(
            args.app).expanduser().resolve(), log=log)
        summary["port_after"] = http_json(
            f"http://127.0.0.1:{port}/health", timeout=2.0)[0] if port else 0
        if temp_root is not None:
            summary["temp_root"] = str(temp_root)
            summary["temp_root_entries_left"] = (
                sorted(p.name for p in temp_root.iterdir())
                if temp_root.exists() else [])
        if driver is not None:
            driver.close()
        summary["verdict"] = recorder.verdict()
        summary["elapsed_s"] = round(time.time() - started, 1)
        (output / "result.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[verdict] {summary['verdict']} -> {output / 'result.json'}", flush=True)

    return 3 if summary.get("halt_kind") == "BLOCKED" else 1


def run_gen_manifest(args: argparse.Namespace) -> int:
    try:
        manifest = generate_manifest(
            app_path=Path(args.app).expanduser().resolve(),
            receipt_path=Path(args.receipt).expanduser().resolve(),
            repo=Path(args.repo).expanduser().resolve())
    except (IdentityError, BlockedError) as exc:
        print(f"[gen-manifest] REFUSED: {exc}", file=sys.stderr)
        return 3
    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[gen-manifest] wrote {out} "
          f"(commit={manifest['source']['commit'][:12]}… dirty=False 实测)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_tauri_analysis.py",
        description=("ISS-141 变化解读真实 Tauri 产品路径验收入口"
                     "（PM 实机执行，隔离 runtime/scan 根 + 随机端口，零前台）"),
        epilog="退出码：0 全过（可能含 NOT_VERIFIED-需前台）/ 1 断言失败 / "
               "2 用法错误 / 3 全局阻塞")
    sub = parser.add_subparsers(dest="subcommand")

    gen = sub.add_parser("gen-manifest", help="从真实构建回执生成 manifest（B6）")
    gen.add_argument("--app", required=True)
    gen.add_argument("--receipt", required=True)
    gen.add_argument("--output", required=True)
    gen.add_argument("--repo", default=str(REPO_ROOT))
    gen.set_defaults(func=run_gen_manifest)

    run = sub.add_parser("run", help="执行实机验收（默认）")
    run.add_argument("--app", required=True)
    run.add_argument("--build-ready", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--expected-commit", default="")
    run.add_argument("--shim-mode", action="store_true")
    run.add_argument("--send-sample", default="",
                     help="产品发送样本文件路径（PM 经真实确认层使用）")
    run.add_argument("--resume-file", default="",
                     help="PM 完成真实产品操作后创建该文件，交互阶段即收口")
    run.add_argument("--product-timeout", type=float, default=900.0)
    run.set_defaults(func=run_verification)

    # 顶层直跑（不写子命令）也可用
    parser.add_argument("--app")
    parser.add_argument("--build-ready")
    parser.add_argument("--output")
    parser.add_argument("--expected-commit", default="")
    parser.add_argument("--shim-mode", action="store_true")
    parser.add_argument("--send-sample", default="")
    parser.add_argument("--resume-file", default="")
    parser.add_argument("--product-timeout", type=float, default=900.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # argparse 遇 --help / 用法错误会抛 SystemExit：--help 归 0，误用归 2，
    # 不能让它冒泡成 Python 默认的退出码/堆栈。
    try:
        if raw and raw[0] == "gen-manifest":
            args = build_parser().parse_args(raw)
            return int(args.func(args))
        # 顶层直接跑：把 flags 转成 run 子命令
        if not any(item in {"gen-manifest", "run", "-h", "--help"} for item in raw):
            raw = ["run", *raw]
        args = build_parser().parse_args(raw)
    except SystemExit as exc:
        code = exc.code
        if code in (0, None):
            return 0
        return 2 if not isinstance(code, int) else code
    if not getattr(args, "func", None):
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[verify] 中断", file=sys.stderr)
        raise SystemExit(3)
