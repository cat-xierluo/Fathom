#!/usr/bin/env python3
"""ISS-140 · Intel 原生冻结 helper 解读回归流水线的 CI 控制入口。

编排三阶段（全部真实子进程，无模拟）：

  1. build   ：复用 ``scripts/build_helper.sh <VENV>`` 在原生 runner 冻结
               helper（PyInstaller pin 由脚本自身断言；本入口不改其行为）。
  2. identity：对冻结产物做身份绑定——真实文件 SHA256、Mach-O 头 cputype
               （执法源；``file(1)`` 仅旁证记录）、``--version`` JSON 与单一
               版本源 ``fathom/__init__.py`` 一致——并写 build-ready
               manifest（``ok`` 仅在构建+身份全部成立时为 true）。
  3. verify  ：以 ``--helper/--output/--build-ready <manifest>`` 显式调用
               ``scripts/verify_frozen_analysis.py``（ISS-138 的 33 项真实
               serve/HTTP/DB/子进程生命周期检查），退出码原样透传。

fail-closed 合同：

  - 构建/身份任一失败：manifest 落盘为 ``ok=false``（含 stage/reason/原退出
    码），verify **不被调用**，整体退出码保留原子进程退出码（含
    build_helper.sh 的 3=BLOCKED）。冻结失败绝不发布成功 manifest。
  - verify 失败：退出码透传；manifest 不回改——构建与身份是既成事实，
    回归结论由 verify 的 report.json 承载。
  - 本入口自身：0 全过；1 断言失败；2 阻塞/用法（venv 缺失、git 不可用、
    source_sha 非 40 位 hex 等）。

边界：不安装依赖（venv 由调用方准备）、不引用 secrets、不做 release/
push/PR、不依赖任何 session 私有路径——所有证据写调用方给的 ``--work-dir``
（CI 用 ``$RUNNER_TEMP``）。证据（build_helper.log、verify.log、report.json
回显、ci-summary.json）同时落盘并回显到 stdout，进 GitHub job 日志留存。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# build_helper.sh 的冻结产物相对仓库根路径（其内部固定，勿改）
HELPER_SUBPATH = Path(
    "apps/desktop/src-tauri/resources/helper/fathom-helper/fathom-helper")

TASK = "ISS-140"
BUILD_TIMEOUT_S = 2400.0
VERIFY_TIMEOUT_S = 2400.0
PROBE_TIMEOUT_S = 60.0

# Mach-O 64 位小端（本机字节序）魔数与 cputype；只认原生 x86_64/arm64，
# 大端/32 位/未知 cputype 一律判 None（fail-closed，不用交叉/替身产物）。
MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_X86_64 = 0x01000007
CPU_TYPE_ARM64 = 0x0100000C
_CPUTYPE_ARCH = {
    CPU_TYPE_X86_64: "x86_64",
    CPU_TYPE_ARM64: "arm64",
}

_SHA40_RE = re.compile(r"[0-9a-f]{40}")


class StageFailure(Exception):
    """阶段失败：携带 stage 名、原因与应透传的退出码。"""

    def __init__(self, stage: str, reason: str, exit_code: int = 1) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# 纯函数工具
# --------------------------------------------------------------------------
def parse_macho_header(head: bytes) -> dict[str, Any] | None:
    """解析 Mach-O 头前 8 字节；非本机小端 64 位魔数返回 None。"""
    if len(head) < 8:
        return None
    magic, cputype = struct.unpack("<II", head[:8])
    if magic != MH_MAGIC_64:
        return None
    return {"magic": f"0x{magic:08x}", "cputype": f"0x{cputype:08x}",
            "cputype_value": cputype}


def parse_macho_arch(head: bytes) -> str | None:
    """Mach-O 头 → 'x86_64'/'arm64'；无法确证原生 64 位架构返回 None。"""
    header = parse_macho_header(head)
    if header is None:
        return None
    return _CPUTYPE_ARCH.get(header["cputype_value"])


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def single_source_version(repo_root: Path) -> str:
    """单一版本源 fathom/__init__.py 的 __version__（与 build_helper.sh 同口径）。"""
    src = repo_root / "fathom" / "__init__.py"
    if not src.is_file():
        raise StageFailure("identity", f"单一版本源缺失：{src}", 2)
    m = re.search(r'^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
                  src.read_text(encoding="utf-8"), re.M)
    if not m:
        raise StageFailure("identity", f"单一版本源无 __version__ 三段号：{src}", 2)
    return m.group(1)


def pyinstaller_pin(repo_root: Path) -> str:
    """冻结工具链 pin（证据记录；版本执法在 build_helper.sh 内部）。"""
    pin_file = repo_root / "packaging" / "requirements-runtime-build.txt"
    if not pin_file.is_file():
        return "unknown"
    m = re.search(r"^pyinstaller==(\S+)", pin_file.read_text(encoding="utf-8"),
                  re.M)
    return m.group(1) if m else "unknown"


def git_head(repo_root: Path) -> str:
    proc = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise StageFailure("source", f"git rev-parse HEAD 失败：{proc.stderr[-200:]}",
                           2)
    sha = proc.stdout.strip()
    if not _SHA40_RE.fullmatch(sha):
        raise StageFailure("source", f"HEAD 非 40 位 hex SHA：{sha!r}", 2)
    return sha


def file_probe(helper: Path) -> str:
    """``file(1)`` 旁证（仅证据，不执法；不可用时记 n/a 不放行/否决）。"""
    exe = shutil.which("file")
    if not exe:
        return "n/a"
    try:
        proc = subprocess.run([exe, str(helper)], capture_output=True,
                              text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "n/a"
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else "n/a"


# --------------------------------------------------------------------------
# 身份绑定（manifest 执法核心）
# --------------------------------------------------------------------------
def collect_identity(repo_root: Path, helper: Path,
                     expected_arch: str) -> dict[str, Any]:
    """收集并断言冻结 helper 身份；任一不符抛 StageFailure（fail-closed）。

    架构执法源是 Mach-O 头 cputype（不信任文件名/宿主猜测）；版本执法源是
    单一版本源 fathom/__init__.py（与 build_helper.sh 同一正则口径）。
    """
    helper = Path(helper)
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise StageFailure("identity", f"helper 不存在或不可执行：{helper}", 2)
    with helper.open("rb") as fh:
        head = fh.read(8)
    header = parse_macho_header(head)
    if header is None:
        raise StageFailure(
            "identity",
            f"非本机小端 64 位 Mach-O 头（前 8 字节={head.hex()}）：{helper}", 1)
    arch = _CPUTYPE_ARCH.get(header["cputype_value"])
    if arch != expected_arch:
        raise StageFailure(
            "identity",
            f"Mach-O 架构 {arch}（cputype={header['cputype']}）≠ 期望 "
            f"{expected_arch}——arm64/交叉产物不得冒充 Intel 实测", 1)
    try:
        proc = subprocess.run([str(helper), "--version"], capture_output=True,
                              text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise StageFailure("identity", f"--version 超时（>{PROBE_TIMEOUT_S}s）：{exc}") from exc
    if proc.returncode != 0:
        raise StageFailure(
            "identity",
            f"--version 退出 {proc.returncode}：{(proc.stderr or '')[-200:]}", 1)
    try:
        version_obj = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise StageFailure(
            "identity", f"--version 输出非 JSON：{proc.stdout[:120]!r}") from exc
    if not isinstance(version_obj, dict) or \
            version_obj.get("service") != "fathom" or \
            version_obj.get("protocol_version") != 1:
        raise StageFailure(
            "identity", f"--version 身份面不符（service/protocol_version）：{version_obj}", 1)
    source_version = single_source_version(repo_root)
    if version_obj.get("version") != source_version:
        raise StageFailure(
            "identity",
            f"--version 版本 {version_obj.get('version')!r} ≠ 单一版本源 "
            f"{source_version!r}", 1)
    return {
        "helper": str(helper),
        "helper_sha256": sha256_of(helper),
        "helper_bytes": helper.stat().st_size,
        "arch": arch,
        "macho": {"magic": header["magic"], "cputype": header["cputype"]},
        "file_output": file_probe(helper),
        "version": source_version,
        "version_output": proc.stdout.strip(),
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def success_manifest(source_sha: str, identity: dict[str, Any],
                     repo_root: Path) -> dict[str, Any]:
    return {
        "task": TASK,
        "ok": True,
        "source_sha": source_sha,
        "helper": identity["helper"],
        "helper_sha256": identity["helper_sha256"],
        "helper_bytes": identity["helper_bytes"],
        "arch": identity["arch"],
        "macho": identity["macho"],
        "file_output": identity["file_output"],
        "version": identity["version"],
        "pyinstaller_pin": pyinstaller_pin(repo_root),
        "created_at": _now(),
    }


def failure_manifest(stage: str, reason: str, exit_code: int, *,
                     source_sha: str | None, helper: str | None) -> dict[str, Any]:
    return {
        "task": TASK,
        "ok": False,
        "stage": stage,
        "reason": reason,
        "exit_code": exit_code,
        "source_sha": source_sha,
        "helper": helper,
        "created_at": _now(),
    }


# --------------------------------------------------------------------------
# 子进程执行（日志落盘 + 回显 job 日志）
# --------------------------------------------------------------------------
def _coerce(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def run_logged(cmd: list[str], *, log_path: Path, cwd: Path | None = None,
               timeout: float) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(
            cmd, 124, stdout=_coerce(exc.stdout),
            stderr=_coerce(exc.stderr) + f"\n[iss140] 超时（>{timeout}s）")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"$ {' '.join(cmd)}\n\n-- stdout --\n{proc.stdout or ''}"
        f"\n-- stderr --\n{proc.stderr or ''}", encoding="utf-8")
    if proc.stdout:
        print(proc.stdout, end="", flush=True)
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr, flush=True)
    return proc


# --------------------------------------------------------------------------
# 子命令：manifest（身份绑定单步；可独立负向验证）
# --------------------------------------------------------------------------
def cmd_manifest(args: argparse.Namespace) -> int:
    out = Path(args.out)
    source_sha = args.source_sha.strip()
    if not _SHA40_RE.fullmatch(source_sha):
        print(f"[iss140] FAIL：--source-sha 非 40 位 hex：{source_sha!r}",
              file=sys.stderr)
        return 2
    helper = Path(args.helper)
    try:
        identity = collect_identity(args.repo_root, helper, args.expected_arch)
    except StageFailure as exc:
        manifest = failure_manifest(exc.stage, exc.reason, exc.exit_code,
                                    source_sha=source_sha, helper=str(helper))
        write_manifest(out, manifest)
        print(f"[iss140] FAIL（{exc.stage}）：{exc.reason}", file=sys.stderr)
        print(json.dumps({"stage": "manifest", "ok": False,
                          "manifest": str(out)}, ensure_ascii=False))
        return exc.exit_code
    manifest = success_manifest(source_sha, identity, args.repo_root)
    write_manifest(out, manifest)
    print(f"[iss140] manifest ok=true → {out}")
    print(json.dumps({"stage": "manifest", "ok": True, "manifest": str(out),
                      "helper_sha256": identity["helper_sha256"],
                      "arch": identity["arch"],
                      "version": identity["version"]}, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# 子命令：run（CI 编排：build → identity → verify）
# --------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    repo_root: Path = args.repo_root
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    manifest_path = work / "build-ready.json"
    helper_path = repo_root / HELPER_SUBPATH
    summary: dict[str, Any] = {
        "task": TASK, "mode": "run", "repo_root": str(repo_root),
        "expected_arch": args.expected_arch,
        "started_at": _now(), "stages": {},
    }

    def finish(exit_code: int) -> int:
        summary["exit"] = exit_code
        summary["finished_at"] = _now()
        (work / "ci-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"stage": "run", "stages": summary["stages"],
                          "manifest": str(manifest_path), "exit": exit_code},
                         ensure_ascii=False))
        return exit_code

    # ---- 阻塞预检（不进入构建）----
    venv_python = Path(args.venv) / "bin" / "python"
    if not venv_python.is_file():
        print(f"[iss140] BLOCKED：venv Python 缺失：{venv_python}", file=sys.stderr)
        return finish(2)
    build_script = Path(args.build_script) if args.build_script \
        else repo_root / "scripts" / "build_helper.sh"
    verify_script = Path(args.verify_script) if args.verify_script \
        else repo_root / "scripts" / "verify_frozen_analysis.py"
    for path in (build_script, verify_script):
        if not path.is_file():
            print(f"[iss140] BLOCKED：复用脚本缺失：{path}", file=sys.stderr)
            return finish(2)

    # ---- source SHA（固定 head 证据）----
    try:
        source_sha = git_head(repo_root)
    except StageFailure as exc:
        summary["stages"]["source"] = {"exit": exc.exit_code,
                                        "reason": exc.reason}
        return finish(exc.exit_code)
    summary["source_sha"] = source_sha

    # ---- build：真实冻结（复用 build_helper.sh，不改其行为）----
    build_proc = run_logged(["bash", str(build_script), str(args.venv)],
                            log_path=work / "build_helper.log", cwd=repo_root,
                            timeout=BUILD_TIMEOUT_S)
    summary["stages"]["build"] = {"exit": build_proc.returncode,
                                  "helper": str(helper_path)}
    if build_proc.returncode != 0:
        reason = (f"build_helper.sh 退出 {build_proc.returncode}："
                  f"{(build_proc.stderr or build_proc.stdout or '').strip()[-300:]}")
        write_manifest(manifest_path, failure_manifest(
            "build", reason, build_proc.returncode,
            source_sha=source_sha, helper=str(helper_path)))
        print(f"[iss140] FAIL（build）：{reason}", file=sys.stderr)
        return finish(build_proc.returncode)  # 原退出码保留（含 3=BLOCKED）

    # ---- identity：身份绑定 + 成功 manifest（唯一写 ok=true 的路径）----
    try:
        identity = collect_identity(repo_root, helper_path, args.expected_arch)
    except StageFailure as exc:
        write_manifest(manifest_path, failure_manifest(
            exc.stage, exc.reason, exc.exit_code,
            source_sha=source_sha, helper=str(helper_path)))
        summary["stages"]["identity"] = {"exit": exc.exit_code,
                                         "reason": exc.reason}
        print(f"[iss140] FAIL（identity）：{exc.reason}", file=sys.stderr)
        return finish(exc.exit_code)
    write_manifest(manifest_path, success_manifest(source_sha, identity,
                                                   repo_root))
    summary["stages"]["identity"] = {
        "exit": 0, "helper_sha256": identity["helper_sha256"],
        "arch": identity["arch"], "version": identity["version"],
        "source_sha": source_sha}

    # ---- verify：ISS-138 33 项真实回归（显式传 manifest）----
    evidence_dir = work / "evidence"
    verify_cmd = [str(venv_python), str(verify_script),
                  "--helper", str(helper_path),
                  "--output", str(evidence_dir),
                  "--build-ready", str(manifest_path)]
    verify_proc = run_logged(verify_cmd, log_path=work / "verify_frozen.log",
                             cwd=repo_root, timeout=VERIFY_TIMEOUT_S)
    summary["stages"]["verify"] = {"exit": verify_proc.returncode,
                                   "evidence_dir": str(evidence_dir)}
    # 断言证据回显进 job 日志（runner 临时目录不可留存）
    report = evidence_dir / "report.json"
    if report.is_file():
        print(f"[iss140] verify report.json 回显：\n{report.read_text(encoding='utf-8')}")
    serve_log = evidence_dir / "serve.log"
    if serve_log.is_file():
        tail = "\n".join(serve_log.read_text(encoding="utf-8",
                                             errors="replace").splitlines()[-40:])
        print(f"[iss140] serve.log 尾部回显：\n{tail}")
    return finish(verify_proc.returncode)  # 原退出码透传


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ci_frozen_analysis.py",
        description="ISS-140：Intel 原生冻结 helper 解读回归流水线控制入口"
                    "（build_helper.sh 冻结 → 身份 manifest → "
                    "verify_frozen_analysis.py 33 项回归；fail-closed）")
    sub = ap.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser(
        "run", help="完整编排：冻结 + 身份绑定 + ISS-138 33 项回归")
    run_p.add_argument("--venv", required=True,
                       help="PyInstaller venv 路径（CI：$RUNNER_TEMP/fathom-venv；"
                            "须已按 requirements-runtime(-build)+constraints 装好）")
    run_p.add_argument("--work-dir", required=True,
                       help="证据输出根（CI：$RUNNER_TEMP 下的目录；不写仓库）")
    run_p.add_argument("--repo-root", default=None,
                       help="仓库根（默认：脚本所在仓库）")
    run_p.add_argument("--expected-arch", default="x86_64",
                       choices=["x86_64", "arm64"],
                       help="期望的原生 Mach-O 架构（默认 x86_64；Mach-O 头执法）")
    run_p.add_argument("--build-script", default=None,
                       help="覆盖 build_helper.sh 路径（默认 <repo>/scripts/）")
    run_p.add_argument("--verify-script", default=None,
                       help="覆盖 verify_frozen_analysis.py 路径（默认 <repo>/scripts/）")
    run_p.set_defaults(func=cmd_run)

    man_p = sub.add_parser(
        "manifest", help="单步身份绑定：对既有 helper 写 build-ready manifest")
    man_p.add_argument("--helper", required=True, help="冻结 helper 可执行文件")
    man_p.add_argument("--out", required=True, help="manifest 输出路径")
    man_p.add_argument("--source-sha", required=True,
                       help="来源提交 40 位 hex SHA")
    man_p.add_argument("--repo-root", default=None, help="仓库根（默认：脚本所在仓库）")
    man_p.add_argument("--expected-arch", default="x86_64",
                       choices=["x86_64", "arm64"],
                       help="期望的原生 Mach-O 架构（默认 x86_64）")
    man_p.set_defaults(func=cmd_manifest)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "repo_root", None) is None:
        args.repo_root = REPO_ROOT
    args.repo_root = Path(args.repo_root).resolve()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
