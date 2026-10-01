#!/usr/bin/env python3
"""ISS-141 · 变化解读真实 Tauri 产品路径验收入口（PM 实机执行）。

目标：给 PM 一个可重复的、隔离的、fail-closed 的验收入口，用真实
打包 Fathom.app（Tauri / WKWebView）验证「选择引擎 → 启用 → 生成预览 →
确认发送 → 查看结果与证据 → 撤销授权」这条**产品交互**路径。

设计边界（与卡合同一致，改动前先读 ISS-141 卡面）：
  - 只验**产品入口**。不许自制页面 / mock invoke / 直接 POST 冒充用户
    点击：交互必须经 CGEventPostToPid 投递到**自有 PID** 的真实窗口，
    截图经 `screencapture -l<window id>` 抓真实窗口。
  - 隔离：独立临时 runtime 根 + 合成扫描根 + 随机端口。绝不写真实
    ``~/Library/Application Support/Fathom``，绝不扫 HOME，绝不碰生产
    7952 端口。环境只用 `open -g --env` 注入，**不做 launchctl setenv
    全局 fallback**（ISS-141 明确不照搬既有窗口脚本的全局回退）。
  - 启动前硬门：.app 二进制 / 资源 SHA 与 bundle id 必须与 --build-ready
    manifest 逐项一致，否则拒绝启动（防止拿旧 app 伪造证）。
  - 启动后硬门：``GET /api/config`` 回读生效扫描根，realpath 归一后必须
    等于本轮合成根且**不得**在 HOME 或生产应用支持目录下。
  - 零打扰：只 `open -g`，不 activate、不发全局键盘事件。Tab / Esc 等
    需前台键击的项目如实记 NOT_VERIFIED-需前台并给出停止条件，绝不假绿。
  - 真实模型发送（至少一家真实 CLI）由 PM 经**真实确认层**点一次合成
    固定样本；本脚本只提供 GUI 接线证据与「适配器可用性」观测，绝不把
    shim 模式的成功当成产品发送成功。

用法（PM 构建就绪后串行跑一次）::

    python scripts/verify_tauri_analysis.py \
        --app <固定隔离 app 路径> \
        --build-ready <manifest.json> \
        --output <任务证据目录>

退出码：
  0  可执行断言全过（存在 NOT_VERIFIED-需前台 项时 verdict=PASS_WITH_NOT_VERIFIED）
  1  任一可执行断言失败
  2  用法错误
  3  全局阻塞（app/manifest 缺失或身份不符、端口段被占、无法消歧的
     既有 Fathom 进程、缺只读工具、Swift 驱动不可用、后台点击/截图不可用）

证据落盘：<output>/cases.jsonl、result.json、screenshots/、driver.log。
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

# 生产常量：仅用于**反向断言**（确认我们没碰生产），从不写入。
PRODUCTION_SUPPORT_DIR = "Library/Application Support/Fathom"
PRODUCTION_PORT = 7952

# 窗口内容区目标（TESTING UX 矩阵视口语义）。本机实测 tauri.conf.json 的
# width/height 映射为外框，故设置时按 外框 = 目标 + 标题栏 28pt。
TARGET_CONTENT_SIZES = ((980, 640), (1220, 820))
TITLEBAR_HEIGHT = 28

MANIFEST_VERSION = 1
MANIFEST_TOP_KEYS = {"version", "app", "helper", "source", "built_at"}
MANIFEST_APP_KEYS = {"path", "bundle_id", "fingerprint_sha256", "files"}
MANIFEST_HELPER_KEYS = {"relative_path", "sha256"}
MANIFEST_SOURCE_KEYS = {"commit", "dirty"}

HELPER_RELATIVE_PATH = "Contents/Resources/helper/fathom-helper/fathom-helper"

# 合成扫描根的最小体量（MB 级真实文件，与既有窗口脚本同口径）。
SYNTHETIC_MEGABYTES = 6


class IdentityError(Exception):
    """app / manifest / 源码指纹身份不符——拒绝启动（fail-closed）。"""


class GateError(Exception):
    """启动后隔离硬门失败（扫描根越界、端口不是自有 helper 等）。"""


class BlockedError(Exception):
    """全局环境阻塞，无法开始实机验证。"""


# --------------------------------------------------------------------------
# 纯函数：身份与隔离判定（可被 tests 直接覆盖，不依赖实机）
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_fingerprint(app_path: Path) -> dict[str, Any]:
    """只读遍历 .app，逐文件 SHA + 汇总指纹（对齐 verify_app_bundle.sh 口径）。"""
    if not app_path.is_dir():
        raise IdentityError(f".app 不存在或不是目录：{app_path}")
    files: dict[str, str] = {}
    for item in sorted(app_path.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        rel = item.relative_to(app_path).as_posix()
        files[rel] = sha256_file(item)
    if not files:
        raise IdentityError(f".app 内没有任何文件：{app_path}")
    rollup = hashlib.sha256(
        "\n".join(f"{rel}  {files[rel]}" for rel in sorted(files)).encode()
    ).hexdigest()
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


def load_manifest(path: Path) -> dict[str, Any]:
    """严格解析 --build-ready manifest：未知字段一律拒绝（不静默忽略）。"""
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
    for key in ("app", "helper", "source"):
        section = manifest.get(key)
        if not isinstance(section, dict):
            raise IdentityError(f"manifest 缺 {key} 对象段")
    if manifest.get("version") != MANIFEST_VERSION:
        raise IdentityError(
            f"manifest version 必须为 {MANIFEST_VERSION}，"
            f"实际 {manifest.get('version')!r}")
    return manifest


def verify_manifest_identity(
    manifest: dict[str, Any],
    app_path: Path,
    *,
    expected_commit: str | None = None,
    actual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """启动前硬门：app 二进制/资源 SHA、bundle id、helper、源码 SHA 逐项核对。

    任一不符即抛 IdentityError，**不允许**「先跑起来再说」。
    """
    # 版本门也在这里再查一次：本函数是唯一启动前闸门，不能依赖调用方
    # 一定先经过 load_manifest（直接调用本函数时同样要 fail-closed）。
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
    for key in MANIFEST_SOURCE_KEYS:
        if key not in source:
            raise IdentityError(f"manifest.source 缺字段 {key}")
    if source["dirty"] is not False:
        raise IdentityError(
            "manifest.source.dirty 必须为 false（脏源码构建的 .app 不作为验收证据）")

    observed = actual if actual is not None else bundle_fingerprint(app_path)
    bundle_id = read_bundle_id(app_path)

    if bundle_id != app_section["bundle_id"]:
        raise IdentityError(
            f"bundle id 不符：manifest={app_section['bundle_id']} 实测={bundle_id}")
    if observed["fingerprint_sha256"] != app_section["fingerprint_sha256"]:
        raise IdentityError(
            "app 指纹不符：manifest="
            f"{app_section['fingerprint_sha256'][:16]}… 实测="
            f"{observed['fingerprint_sha256'][:16]}…（拒绝用旧 app 伪证）")

    expected_files = app_section["files"]
    if not isinstance(expected_files, dict) or not expected_files:
        raise IdentityError("manifest.app.files 必须是非空对象")
    missing = sorted(set(expected_files) - set(observed["files"]))
    if missing:
        raise IdentityError(
            f"app 缺少 manifest 声明的 {len(missing)} 个文件，"
            f"首个：{missing[0]}")
    mismatched = sorted(
        rel for rel, sha in expected_files.items()
        if observed["files"].get(rel) != sha)
    if mismatched:
        raise IdentityError(
            f"app 有 {len(mismatched)} 个文件 SHA 不符，首个：{mismatched[0]}")
    if set(observed["files"]) - set(expected_files):
        raise IdentityError(
            "app 含 manifest 未声明的额外文件（构建产物与记录不一致）："
            f"{sorted(set(observed['files']) - set(expected_files))[0]}")

    helper_rel = helper_section["relative_path"]
    helper_sha = helper_section["sha256"]
    if helper_rel != HELPER_RELATIVE_PATH:
        raise IdentityError(
            f"manifest.helper.relative_path 必须为 {HELPER_RELATIVE_PATH}，"
            f"实际 {helper_rel!r}")
    if helper_sha != observed["files"].get(HELPER_RELATIVE_PATH):
        raise IdentityError("helper 二进制 SHA 与 manifest 不符或缺失")

    if expected_commit is not None and source["commit"] != expected_commit:
        raise IdentityError(
            f"manifest 记录源码 commit={source['commit'][:12]}… 与固定 head="
            f"{expected_commit[:12]}… 不符")

    return {"bundle_id": bundle_id, **observed, "source": source}


def normalize_root(raw: str | os.PathLike[str]) -> Path:
    """realpath 归一（消 symlink / 尾斜杠 / /private 前缀差异）。"""
    return Path(os.path.realpath(str(raw)))


def production_support_dir(home: Path) -> Path:
    return normalize_root(home / PRODUCTION_SUPPORT_DIR)


def assert_isolated_scan_root(
    scan_root: Any,
    *,
    expected: Path,
    home: Path,
) -> Path:
    """隔离硬门：生效扫描根必须**恰好**是本轮合成根，且不在 HOME /
    生产应用支持目录下。``/api/config`` 回读值异常时抛 GateError。"""
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
    """端口归属硬门：instance 记录的 pid/port 必须与实测自有 helper 一致。"""
    if instance.get("port") != port:
        raise GateError(
            f"helper instance port={instance.get('port')} 与本轮端口 {port} 不符")
    if instance.get("pid") != pid:
        raise GateError(
            f"helper instance pid={instance.get('pid')} 与实测 pid={pid} 不符")


def outer_frame_size(content_w: int, content_h: int) -> tuple[int, int]:
    """内容区 → 外框（本机实测 28pt 标题栏）。"""
    return content_w, content_h + TITLEBAR_HEIGHT


def readback_content_size(frame_w: int, frame_h: int) -> tuple[int, int]:
    """外框回读 → 内容区（尺寸断言口径 = 视口）。"""
    return frame_w, frame_h - TITLEBAR_HEIGHT


def size_assertion(content_w: int, content_h: int, target: tuple[int, int]) -> None:
    if (content_w, content_h) != target:
        raise GateError(
            f"窗口内容区尺寸不符：期望 {target[0]}x{target[1]} "
            f"实测 {content_w}x{content_h}")


def pick_free_port(rng: random.Random, *, low: int = 51_000, high: int = 59_000) -> int:
    """随机端口 + 实测占用检查（避免与生产 7952 及其他任务撞车）。"""
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


# --------------------------------------------------------------------------
# 合成根
# --------------------------------------------------------------------------


def make_synthetic_scan_root(base: Path) -> Path:
    """在隔离根下造 MB 级真实文件（不用符号链接，体积可被 du 实测）。"""
    scan_root = base / "synthetic-scan-root"
    for sub in ("alpha", "beta", "nested/deep"):
        (scan_root / sub).mkdir(parents=True, exist_ok=True)
    block = b"fathom-iss141-synthetic-sample\n" * 1024  # 32KiB
    written = 0
    index = 0
    while written < SYNTHETIC_MEGABYTES * 1024 * 1024:
        target = scan_root / f"sample-{index:04d}.bin"
        target.write_bytes(block)
        written += len(block)
        index += 1
    (scan_root / "alpha" / "changed.txt").write_text("iss141 growth\n", "utf-8")
    (scan_root / "beta" / "changed.txt").write_text("iss141 shrink\n", "utf-8")
    return scan_root


def disk_usage_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


# --------------------------------------------------------------------------
# HTTP 观察面（只读核对 + 与 GUI 结果交叉印证；绝不用它代替点击）
# --------------------------------------------------------------------------


def http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any]:
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
            return exc.code, json.loads(raw) if raw.strip() else None
        except ValueError:
            return exc.code, {"raw": raw}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return 0, {"error": str(exc)}


def wait_for_health(port: int, *, deadline_s: float = 90.0) -> bool:
    end = time.time() + deadline_s
    while time.time() < end:
        status, _ = http_json(f"http://127.0.0.1:{port}/health", timeout=3.0)
        if status == 200:
            return True
        time.sleep(1.0)
    return False


# --------------------------------------------------------------------------
# Swift 驱动
# --------------------------------------------------------------------------


class Driver:
    """编译并调用 tauri_analysis_driver.swift（只读工具，随机端口编译到临时根）。

    驱动**自己**再按 --pid / --bundle-id 消歧：找不到唯一自有窗口即拒绝
    发事件、拒绝截图。Python 侧不复用任何窗口测量逻辑。
    """

    def __init__(self, log_path: Path, *, build_root: Path):
        self.log_path = log_path
        self.binary = build_root / "tauri_analysis_driver"
        self._log_handle = log_path.open("a", encoding="utf-8")

    def build(self) -> None:
        if not DRIVER_SOURCE.is_file():
            raise BlockedError(f"驱动源码缺失：{DRIVER_SOURCE}")
        swiftc = shutil.which("swiftc")
        if not swiftc:
            raise BlockedError("缺 swiftc：无法编译只读驱动（后台点击/截图不可用，fail-closed）")
        result = subprocess.run(
            [swiftc, "-O", "-o", str(self.binary), str(DRIVER_SOURCE)],
            capture_output=True, text=True, timeout=300,
        )
        self._record("build", result)
        if result.returncode != 0 or not self.binary.is_file():
            raise BlockedError(
                f"驱动编译失败（exit={result.returncode}），不降级为浏览器/自制页面截图")

    def run(self, argv: list[str], *, timeout: float = 120.0) -> dict[str, Any]:
        proc = subprocess.run(
            [str(self.binary), *argv], capture_output=True, text=True,
            timeout=timeout,
        )
        self._record(" ".join(argv), proc)
        if proc.returncode != 0:
            raise GateError(
                f"驱动子命令失败 exit={proc.returncode}: {' '.join(argv)}"
                f" :: {proc.stderr.strip()[:300]}")
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise GateError(
                f"驱动输出非 JSON：{proc.stdout[:200]}") from exc

    def _record(self, label: str, proc: subprocess.CompletedProcess[str]) -> None:
        self._log_handle.write(
            f"=== {label} exit={proc.returncode}\n{proc.stdout}\n{proc.stderr}\n")
        self._log_handle.flush()

    def close(self) -> None:
        self._log_handle.close()


# --------------------------------------------------------------------------
# 证据记录
# --------------------------------------------------------------------------


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
        """status ∈ {pass, fail, blocked}。blocked 一律是需前台的未验项。"""
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


# --------------------------------------------------------------------------
# 实机主流程
# --------------------------------------------------------------------------


def running_fathom_pids() -> list[int]:
    """只读列出自有候选进程（不向任何进程发信号）。"""
    try:
        output = subprocess.run(
            ["pgrep", "-x", "Fathom"], capture_output=True, text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(line) for line in output.split() if line.strip().isdigit()]


def launch_app(
    app_path: Path, runtime_dir: Path, scan_root: Path, port: int, log: Path
) -> None:
    """`open -g --env` 隔离启动（不用 launchctl 全局 setenv 回退）。"""
    env = {
        "FATHOM_RUNTIME_DIR": str(runtime_dir),
        "FATHOM_SCAN_ROOT": str(scan_root),
        "FATHOM_PORT": str(port),
    }
    argv = ["open", "-g", "-n"]
    for key, value in env.items():
        argv += ["--env", f"{key}={value}"]
    argv.append(str(app_path))
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"launch argv={argv}\n")
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        handle.write(f"launch exit={proc.returncode} out={proc.stdout} err={proc.stderr}\n")
    if proc.returncode != 0:
        raise BlockedError(
            f"open -g 启动失败 exit={proc.returncode}：{proc.stderr.strip()[:300]}")


def terminate_owned(pids: list[int], log: Path) -> dict[str, Any]:
    """按精确自有 PID 回收（SIGTERM → SIGKILL），不按名字批量杀。"""
    result = {"requested": pids, "survivors": []}
    for pid in pids:
        try:
            os.kill(pid, 15)
        except (ProcessLookupError, PermissionError):
            continue
    time.sleep(3.0)
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            result["survivors"].append(pid)
            continue
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            continue
    time.sleep(1.0)
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        else:
            result["survivors"].append(pid)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"terminate {result}\n")
    return result


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
        "card": "ISS-141",
        "app": str(Path(args.app).expanduser()),
        "build_ready": str(Path(args.build_ready).expanduser()),
        "output": str(output),
        "frontend_only": False,
        "own_processes_before": running_fathom_pids(),
    }
    rng = random.Random(f"iss141-{summary['own_processes_before']}")

    cleanup_state: dict[str, Any] = {}
    temp_root: Path | None = None
    driver: Driver | None = None
    port = 0
    own_pids: list[int] = []

    try:
        # ---- 阶段 0：启动前身份硬门（失败即拒跑，不留任何进程）----------
        app_path = Path(args.app).expanduser().resolve()
        if not app_path.is_dir() or not (app_path / "Contents").is_dir():
            raise BlockedError(f"--app 不是 Tauri .app bundle：{app_path}")
        manifest_path = Path(args.build_ready).expanduser().resolve()
        try:
            identity = verify_manifest_identity(
                load_manifest(manifest_path), app_path,
                expected_commit=args.expected_commit or None,
            )
        except IdentityError as exc:
            raise BlockedError(f"身份硬门拒绝启动：{exc}") from exc
        recorder.record(
            "app-identity", "pass",
            f"bundle_id={identity['bundle_id']} fingerprint="
            f"{identity['fingerprint_sha256'][:16]}… commit="
            f"{identity['source']['commit'][:12]}… helper SHA 已逐项核对",
        )

        # ---- 阶段 1：隔离根 + 随机端口 --------------------------------
        temp_root = Path(tempfile.mkdtemp(prefix="iss141-analysis-"))
        runtime_dir = temp_root / "runtime"
        runtime_dir.mkdir()
        scan_root = make_synthetic_scan_root(temp_root)
        usage = disk_usage_bytes(scan_root)
        port = pick_free_port(rng)
        summary.update({
            "runtime_dir": str(runtime_dir), "scan_root": str(scan_root),
            "port": port, "synthetic_bytes": usage,
        })
        recorder.record(
            "isolated-roots", "pass",
            f"runtime={runtime_dir} scan_root={scan_root} "
            f"({usage / 1024 / 1024:.1f}MiB 合成样本) port={port}",
        )
        recorder.record(
            "production-untouched", "pass",
            f"生产端口 {PRODUCTION_PORT} 与 {PRODUCTION_SUPPORT_DIR} 未被写入或探测",
        )

        # ---- 阶段 2：驱动可用性（后台点击/截图 fail-closed）-----------
        driver = Driver(log, build_root=temp_root)
        try:
            driver.build()
        except BlockedError as exc:
            raise BlockedError(str(exc)) from exc
        help_result = driver.run(["--help"], timeout=30)
        recorder.record(
            "driver-built", "pass",
            f"swiftc 只读编译通过；子命令 {help_result.get('subcommands', [])}",
        )

        # ---- 阶段 3：启动 + helper 身份 --------------------------------
        before = set(summary["own_processes_before"])
        launch_app(app_path, runtime_dir, scan_root, port, log)
        if not wait_for_health(port):
            raise BlockedError(f"helper /health 在 90s 内未就绪于 127.0.0.1:{port}")
        instance_path = runtime_dir / "helper-instance.json"
        if not instance_path.is_file():
            raise GateError(f"缺少 helper instance 文件：{instance_path}")
        instance = json.loads(instance_path.read_text(encoding="utf-8"))
        pid = instance.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            raise GateError(f"instance pid 不可信：{pid!r}")
        if pid in before:
            raise GateError("instance pid 指向启动前既有 Fathom 进程，拒绝接管")
        own_pids = [pid] + sorted(set(running_fathom_pids()) - before)
        assert_port_owned(port, instance, pid=pid)
        recorder.record(
            "helper-identity", "pass",
            f"自有 helper pid={pid} port={port}（instance 与端口归属一致）",
        )

        # ---- 阶段 4：/api/config 隔离硬门 ------------------------------
        status, config_body = http_json(f"http://127.0.0.1:{port}/api/config")
        if status != 200 or not isinstance(config_body, dict):
            raise GateError(f"/api/config 读取失败：status={status}")
        observed_root = assert_isolated_scan_root(
            (config_body.get("scan_root") or {}).get("value")
            if isinstance(config_body.get("scan_root"), dict)
            else config_body.get("scan_root"),
            expected=scan_root, home=home,
        )
        recorder.record(
            "config-gate-scan-root", "pass",
            f"生效扫描根={observed_root}（非 HOME、非生产支持目录）",
        )

        # ---- 阶段 5：窗口身份 + 两档尺寸 --------------------------------
        for content_w, content_h in TARGET_CONTENT_SIZES:
            label = f"{content_w}x{content_h}"
            own = driver.run([
                "identify", "--pid", str(pid), "--bundle-id", identity["bundle_id"],
            ])
            windows = own.get("windows", [])
            if len(windows) != 1:
                raise GateError(
                    f"{label}：无法消歧自有窗口（匹配 {len(windows)} 个）")
            win = windows[0]
            win_id = win["window_id"]
            frame_w, frame_h = outer_frame_size(content_w, content_h)
            driver.run([
                "set-size", "--pid", str(pid), "--bundle-id", identity["bundle_id"],
                "--window-id", str(win_id), "--width", str(frame_w),
                "--height", str(frame_h),
            ])
            measured = driver.run([
                "measure", "--pid", str(pid), "--bundle-id", identity["bundle_id"],
                "--window-id", str(win_id),
            ])
            size_assertion(
                int(measured["content_width"]), int(measured["content_height"]),
                (content_w, content_h),
            )
            shot = driver.run([
                "screenshot", "--pid", str(pid), "--bundle-id", identity["bundle_id"],
                "--window-id", str(win_id), "--output",
                str(screenshots / f"window-{label}.png"),
            ])
            recorder.record(
                f"window-size-{label}", "pass",
                f"内容区回读 {measured['content_width']}x{measured['content_height']}；"
                f"截图 {shot.get('path')} {shot.get('width')}x{shot.get('height')}",
            )

        # ---- 阶段 6：产品交互路径（PM 真实操作，按实机结果记录）----------
        # 交互断言由 PM 在本入口暂停点用真实点击完成；脚本只提供窗口定位与
        # 截图。未完成的项目必须显式 NOT_VERIFIED，不允许默认绿。
        recorder.record(
            "product-path", "blocked",
            "选择引擎→启用→预览→确认发送→结果/证据→撤销：需 PM 在本入口运行期间"
            "用真实鼠标点击操作真实 WKWebView 窗口，并按 --send-sample 记录真实"
            "CLI 版本/权限/实际成功正文；worker 不代为点击、不以 API 冒充",
        )
        recorder.record(
            "keyboard-foreground-items", "blocked",
            "Tab 焦点环 / Enter / Esc 关闭等需前台键击的检查：需 PM 允许前台后"
            "实测；零打扰约束下不发全局键盘事件",
        )
        if args.shim_mode:
            recorder.record(
                "shim-mode", "blocked",
                "shim 模式只证明 GUI 接线，不构成真实模型发送证据；"
                "不得据此关闭 ISS-035 第一验收项",
            )

        summary["verified_scope"] = [
            "app/manifest 身份硬门", "隔离根与随机端口", "驱动编译与身份消歧",
            "helper 身份", "/api/config 扫描根硬门", "两档窗口内容区尺寸与截图",
        ]
        summary["not_verified"] = list(recorder.blocked)

    except (BlockedError, GateError) as exc:
        kind = "BLOCKED" if isinstance(exc, BlockedError) else "FAIL"
        summary["halt_kind"] = kind
        summary["halt_detail"] = str(exc)
        recorder.record(f"{kind.lower()}-halt", "fail", f"{kind}：{exc}")
    finally:
        # ---- 清理：只按自有精确身份回收 ------------------------------
        if own_pids:
            cleanup_state = terminate_owned(own_pids, log)
        summary["cleanup"] = cleanup_state
        if driver is not None:
            try:
                alive = http_json(f"http://127.0.0.1:{port}/health", timeout=2.0)[0]
            except Exception:  # noqa: BLE001 - 清理路径不得再抛
                alive = 0
            summary["port_after"] = alive
        if temp_root is not None:
            left = sorted(p.name for p in temp_root.iterdir()) if temp_root.exists() else []
            summary["temp_root"] = str(temp_root)
            summary["temp_root_entries_left"] = left
        summary["verdict"] = recorder.verdict()
        summary["elapsed_s"] = round(time.time() - started, 1)
        summary["own_processes_after"] = running_fathom_pids()
        (output / "result.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[verdict] {summary['verdict']} -> {output / 'result.json'}", flush=True)

    return 3 if summary.get("halt_kind") == "BLOCKED" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_tauri_analysis.py",
        description=(
            "ISS-141 变化解读真实 Tauri 产品路径验收入口（PM 实机执行，"
            "隔离 runtime/scan 根 + 随机端口，零前台）"),
        epilog=(
            "退出码：0 全过（可能含 NOT_VERIFIED-需前台）/ 1 断言失败 / "
            "2 用法错误 / 3 全局阻塞"),
    )
    parser.add_argument("--app", required=True,
                        help="固定隔离 .app 路径（必须是本轮构建产物）")
    parser.add_argument("--build-ready", required=True,
                        help="构建就绪 manifest JSON（app/helper/source 指纹）")
    parser.add_argument("--output", required=True, help="任务证据目录")
    parser.add_argument("--expected-commit", default="",
                        help="可选：固定源码 SHA，与 manifest.source.commit 核对")
    parser.add_argument("--shim-mode", action="store_true",
                        help="显式标注 shim 模式：只证明 GUI 接线，不构成发送证据")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 用法错误 → 退出码 2
        return 2 if exc.code not in (0, None) else int(exc.code or 0)
    try:
        return run_verification(args)
    except KeyboardInterrupt:
        print("[verify] 中断", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
