#!/usr/bin/env python3
"""ISS-040A：生成 Tauri updater 兼容的双架构 `latest.json`。

Tauri v2 的静态清单（`RemoteRelease`，见
https://v2.tauri.app/plugin/updater/）顶层字段为
`version` / `notes` / `pub_date` / `platforms`；`platforms` 以 OS-ARCH
为键，macOS 为 `darwin-aarch64` 与 `darwin-x86_64`；每个平台项只含
`url` 与 `signature`，其中 `signature` 必须是该产物 `.sig` 文件的内容。

设计约束（ISS-040A 卡片）：
- `.sig` 由调用方提供，本工具不做真实签名、不引 crate、不动 Tauri 配置；
- 生成必须双架构（--require-both-platforms）；
- 缺平台、版本非法、产物缺失/为空、`.sig` 缺失/为空、版本与产物不一致、
  平台重复或未知时 fail-closed：exit 非 0、中文原因、绝不落盘覆盖。

只依赖标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

# 本发行方案只发布这两个 macOS 架构（docs/plans/2026-09-13-v0.3-release-design.md §4）。
KNOWN_PLATFORMS = ("darwin-aarch64", "darwin-x86_64")

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Tauri 用 semver::Version 解析 version（trim 掉前导 v）；这里与单一版本源
# 口径一致，只接受 X.Y.Z，拒绝 v 前缀与预发布后缀，避免清单与
# scripts/check_version_consistency.sh 的权威源产生第二种写法。
PUB_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


class ManifestError(Exception):
    """fail-closed 拒绝原因，携带面向用户的中文说明。"""


def _fail(msg: str) -> "ManifestError":
    return ManifestError(msg)


# ---- 校验原语（生成器与校验器共用同一规则，import 复用避免规则漂移） ----


def validate_version(version: str) -> str:
    if not version or not version.strip():
        raise _fail("版本号为空：必须提供 X.Y.Z 形式的版本")
    version = version.strip()
    if version.startswith("v"):
        raise _fail(f"版本号 {version!r} 不得带 v 前缀：权威源 fathom.__version__ 为纯 X.Y.Z")
    if not SEMVER_RE.match(version):
        raise _fail(f"版本号 {version!r} 非法：必须为 X.Y.Z（如 0.3.0）")
    return version


def validate_pub_date(pub_date: str) -> str:
    if not pub_date or not pub_date.strip():
        raise _fail("pub_date 为空：必须为 RFC 3339 时间（如 2026-09-17T09:00:00Z）")
    pub_date = pub_date.strip()
    if not PUB_DATE_RE.match(pub_date):
        raise _fail(f"pub_date {pub_date!r} 非法：必须为 RFC 3339（如 2026-09-17T09:00:00Z）")
    return pub_date


def read_signature(path: Path, platform: str) -> str:
    if not path.is_file():
        raise _fail(f"{platform} 的签名文件不存在：{path}")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail(f"{platform} 的签名文件无法读取：{path}（{exc}）")
    content = content.strip()
    if not content:
        raise _fail(f"{platform} 的签名文件为空：{path}（updater 无法校验产物）")
    return content


def check_artifact(path: Path, platform: str) -> None:
    if not path.is_file():
        raise _fail(f"{platform} 的更新产物不存在：{path}")
    if path.stat().st_size == 0:
        raise _fail(f"{platform} 的更新产物为空文件：{path}")


def check_version_matches_artifact_names(version: str, names: dict[str, str]) -> None:
    """产物名必须埋点版本号，防止把上一次产物配到新版本清单。"""
    missing = [f"{platform}={name}" for platform, name in names.items() if version not in name]
    if missing:
        raise _fail(
            "版本与产物不一致：version "
            f"{version} 未出现在 {'、'.join(missing)} 的文件名中"
        )


def build_url(base_url: str | None, artifact: Path) -> str:
    if base_url:
        if not urlparse(base_url).scheme:
            raise _fail(f"base URL {base_url!r} 非法：必须含 scheme（如 https://）")
        return base_url.rstrip("/") + "/" + artifact.name
    return artifact.name


def build_manifest(
    *,
    version: str,
    notes: str | None,
    pub_date: str,
    entries: dict[str, tuple[Path, Path]],
    base_url: str | None,
    require_both_platforms: bool,
) -> dict:
    version = validate_version(version)
    pub_date = validate_pub_date(pub_date)

    unknown = sorted(set(entries) - set(KNOWN_PLATFORMS))
    if unknown:
        raise _fail(
            f"未知平台 {'、'.join(unknown)}：只接受 {'、'.join(KNOWN_PLATFORMS)}"
        )

    if require_both_platforms:
        absent = [p for p in KNOWN_PLATFORMS if p not in entries]
        if absent:
            raise _fail(
                f"缺少平台 {'、'.join(absent)}：双架构清单必须同时提供 "
                f"{' 与 '.join(KNOWN_PLATFORMS)}"
            )

    if not entries:
        raise _fail("未提供任何平台：至少需要一个架构的产物与签名")

    names: dict[str, str] = {}
    platforms: dict[str, dict[str, str]] = {}
    for platform, (artifact, sig) in entries.items():
        check_artifact(artifact, platform)
        signature = read_signature(sig, platform)
        names[platform] = artifact.name
        platforms[platform] = {"url": build_url(base_url, artifact), "signature": signature}

    check_version_matches_artifact_names(version, names)

    manifest: dict[str, object] = {
        "version": version,
        "notes": notes or "",
        "pub_date": pub_date,
        "platforms": platforms,
    }
    return manifest


def _today_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_manifest(manifest: dict, out: Path) -> None:
    """原子写出：先写同目录临时文件再 rename，失败时不破坏既有清单。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    # mkstemp 保证同目录（os.replace 同文件系统才是原子的）。
    fd, tmp_name = tempfile.mkstemp(prefix=".latest-", suffix=".json", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, out)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成 Tauri updater 兼容的 latest.json（双架构 fail-closed）"
    )
    parser.add_argument("--version", required=False, help="版本号，X.Y.Z")
    parser.add_argument("--notes", default=None, help="更新说明（可选）")
    parser.add_argument(
        "--pub-date",
        default=None,
        help="RFC 3339 发布时间；缺省用当前 UTC 时间",
    )
    parser.add_argument("--base-url", default=None, help="产物 URL 基址（可选）")
    parser.add_argument("--out", required=False, help="输出 latest.json 路径")
    parser.add_argument(
        "--darwin-aarch64",
        action="append",
        default=[],
        metavar="ARTIFACT",
        help="arm64 updater 产物路径（重复传入视为重复平台）",
    )
    parser.add_argument(
        "--darwin-x86_64",
        action="append",
        default=[],
        metavar="ARTIFACT",
        help="x86_64 updater 产物路径（重复传入视为重复平台）",
    )
    parser.add_argument(
        "--darwin-aarch64-sig",
        default=None,
        metavar="SIG",
        help="arm64 产物的 .sig 路径",
    )
    parser.add_argument(
        "--darwin-x86_64-sig",
        default=None,
        metavar="SIG",
        help="x86_64 产物的 .sig 路径",
    )
    parser.add_argument(
        "--platform",
        action="append",
        default=[],
        metavar="NAME",
        help="显式声明额外平台（仅用于拒绝未知平台的自检）",
    )
    parser.add_argument(
        "--allow-single-platform",
        action="store_true",
        help="放行单平台清单（仅运行时/排障用；发行必须双架构）",
    )
    parser.add_argument(
        "--print-sha256",
        action="store_true",
        help="同时打印各产物 SHA-256 供发行日志留档",
    )
    return parser


def collect_entries(args: argparse.Namespace) -> dict[str, tuple[Path, Path]]:
    entries: dict[str, tuple[Path, Path]] = {}
    for platform, artifacts, sig in (
        ("darwin-aarch64", args.darwin_aarch64, args.darwin_aarch64_sig),
        ("darwin-x86_64", args.darwin_x86_64, args.darwin_x86_64_sig),
    ):
        if not artifacts:
            continue
        if len(artifacts) > 1:
            raise _fail(f"{platform} 重复传入 {len(artifacts)} 次：每个平台只允许一个产物")
        if sig is None:
            raise _fail(f"{platform} 缺少对应的 .sig 参数（--{platform}-sig）")
        entries[platform] = (Path(artifacts[0]), Path(sig))

    for extra in args.platform:
        name = extra.strip()
        if name in KNOWN_PLATFORMS:
            raise _fail(f"平台 {name} 已由专用参数提供：重复声明")
        raise _fail(
            f"未知平台 {name!r}：本发行只支持 {'、'.join(KNOWN_PLATFORMS)}"
        )
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    # 参数缺失走 fail-closed（exit 1 + 中文原因），不用 argparse 的 exit 2，
    # 以便发行脚本统一以「非 0 + 中文」判定拒绝。
    try:
        if not args.version:
            raise _fail("缺少 --version：必须提供 X.Y.Z 版本号")
        if not args.out:
            raise _fail("缺少 --out：必须提供 latest.json 输出路径")
        entries = collect_entries(args)
        manifest = build_manifest(
            version=args.version,
            notes=args.notes,
            pub_date=args.pub_date or _today_rfc3339(),
            entries=entries,
            base_url=args.base_url,
            require_both_platforms=not args.allow_single_platform,
        )
        write_manifest(manifest, Path(args.out))
    except ManifestError as exc:
        print(f"generate_update_manifest: 拒绝生成 latest.json —— {exc}", file=sys.stderr)
        return 1

    if args.print_sha256:
        for platform, (artifact, _sig) in entries.items():
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            print(f"{platform}: {artifact.name} sha256={digest}")

    platforms = "、".join(sorted(manifest["platforms"]))
    print(
        f"generate_update_manifest: ok（version {manifest['version']}，"
        f"平台 {platforms}）→ {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
