#!/usr/bin/env python3
"""ISS-040A：校验已生成的 Tauri updater `latest.json`。

与 `scripts/generate_update_manifest.py` 共用同一组校验原语（直接 import，
避免两处规则漂移），因此生成物必然可通过；这里主要服务于发行前复核、
Release CI 门禁与人工排查。

校验内容：
1. 顶层结构：version（X.Y.Z）/ notes / pub_date（RFC 3339）/ platforms；
2. 平台项：每项只允许 url 与 signature，且都为非空字符串；url 若带 scheme
   则必须为 https（http 亦拒），无 scheme 时视为相对产物名放行；
3. 默认放行单架构（兼容 Tauri 运行时清单）；发行门禁须显式加
   `--require-both-platforms`（v0.3 双架构口径），`--allow-single-platform`
   仅用于显式声明放宽意图；
4. 可选 `--artifacts-root`：把 url 当成该目录下的相对名回读 `.sig`，
   逐平台比对 signature 是否等于 `.sig` 内容（防止清单与签名错配）。

只依赖标准库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

# 与生成器同源：直接把脚本目录加入 import 路径，导入共用规则。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_update_manifest import (  # noqa: E402
    KNOWN_PLATFORMS,
    ManifestError,
    read_signature,
    validate_pub_date,
    validate_version,
)


def verify_manifest(
    manifest: object,
    *,
    require_both_platforms: bool,
    artifacts_root: Path | None,
) -> list[tuple[str, str]]:
    """返回 (平台, signature原文) 列表；任一项不合规则抛 ManifestError。"""
    if not isinstance(manifest, dict):
        raise ManifestError(f"清单根必须是 JSON 对象，实际为 {type(manifest).__name__}")

    unknown_top = sorted(set(manifest) - {"version", "notes", "pub_date", "platforms"})
    if unknown_top:
        raise ManifestError(f"清单含未知顶层字段 {'、'.join(unknown_top)}：Tauri schema 不接受")

    for key in ("version", "notes", "pub_date", "platforms"):
        if key not in manifest:
            raise ManifestError(f"清单缺少顶层字段 {key}")

    version = manifest["version"]
    if not isinstance(version, str):
        raise ManifestError(f"version 必须是字符串，实际为 {type(version).__name__}")
    version = validate_version(version)

    pub_date = manifest["pub_date"]
    if not isinstance(pub_date, str):
        raise ManifestError(f"pub_date 必须是字符串，实际为 {type(pub_date).__name__}")
    validate_pub_date(pub_date)

    notes = manifest["notes"]
    if not isinstance(notes, str):
        raise ManifestError(f"notes 必须是字符串，实际为 {type(notes).__name__}")

    platforms = manifest["platforms"]
    if not isinstance(platforms, dict):
        raise ManifestError(f"platforms 必须是对象，实际为 {type(platforms).__name__}")
    if not platforms:
        raise ManifestError("platforms 为空：至少需要一个平台")

    unknown = sorted(set(platforms) - set(KNOWN_PLATFORMS))
    if unknown:
        raise ManifestError(
            f"未知平台 {'、'.join(unknown)}：只接受 {'、'.join(KNOWN_PLATFORMS)}"
        )

    if require_both_platforms:
        absent = [p for p in KNOWN_PLATFORMS if p not in platforms]
        if absent:
            raise ManifestError(
                f"缺少平台 {'、'.join(absent)}：双架构清单必须同时提供 "
                f"{' 与 '.join(KNOWN_PLATFORMS)}"
            )

    out: list[tuple[str, str]] = []
    for platform, entry in platforms.items():
        if not isinstance(entry, dict):
            raise ManifestError(f"{platform} 项必须是对象，实际为 {type(entry).__name__}")
        extra = sorted(set(entry) - {"url", "signature"})
        if extra:
            raise ManifestError(f"{platform} 项含未知字段 {'、'.join(extra)}")
        for key in ("url", "signature"):
            if key not in entry:
                raise ManifestError(f"{platform} 项缺少字段 {key}")
            if not isinstance(entry[key], str):
                raise ManifestError(f"{platform} 项 {key} 必须是字符串")
            if not entry[key].strip():
                label = "签名" if key == "signature" else "URL"
                raise ManifestError(f"{platform} 项 {label} 为空")

        url = entry["url"]
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme not in ("https", "http"):
            raise ManifestError(f"{platform} 项 URL scheme 不支持：{url}")
        if url.startswith("https://") is False and parsed.scheme == "http":
            raise ManifestError(f"{platform} 项 URL 必须为 https：{url}")

        signature = entry["signature"]
        if platform in KNOWN_PLATFORMS and version not in url and parsed.scheme:
            # 仅当 URL 为完整地址时用基名核对版本埋点；相对名由生成器保证。
            raise ManifestError(f"{platform} 项 URL 未包含版本 {version}：{url}")

        if artifacts_root is not None:
            name = Path(parsed.path if parsed.scheme else url).name
            if not name:
                raise ManifestError(f"{platform} 项 URL 无法解析出产物文件名：{url}")
            expected = read_signature(artifacts_root / f"{name}.sig", platform)
            if expected != signature:
                raise ManifestError(
                    f"{platform} 项 signature 与 {name}.sig 内容不一致："
                    "清单签名错配（可能把旧产物配到新版本）"
                )
        out.append((platform, signature))
    return out


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="校验 Tauri updater latest.json（默认允许单架构，可收紧）"
    )
    parser.add_argument("--manifest", required=True, help="待校验的 latest.json 路径")
    parser.add_argument(
        "--artifacts-root",
        default=None,
        help="产物所在目录；提供时按 url 基名回读 .sig 比对 signature",
    )
    parser.add_argument(
        "--allow-single-platform",
        action="store_true",
        help="放行单架构清单（默认行为，提供以便脚本显式声明运行时口径）",
    )
    parser.add_argument(
        "--require-both-platforms",
        action="store_true",
        help="收紧为双架构（发行门禁使用；与生成器默认一致）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    path = Path(args.manifest)
    if not path.is_file():
        print(f"verify_update_manifest: 清单不存在：{path}", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"verify_update_manifest: 清单不是合法 JSON —— {exc}", file=sys.stderr)
        return 1

    # 默认允许单架构（Tauri 运行时清单）；发行门禁显式加 --require-both-platforms。
    require_both = args.require_both_platforms
    try:
        entries = verify_manifest(
            manifest,
            require_both_platforms=require_both,
            artifacts_root=Path(args.artifacts_root) if args.artifacts_root else None,
        )
    except ManifestError as exc:
        print(f"verify_update_manifest: 校验失败 —— {exc}", file=sys.stderr)
        return 1

    platforms = "、".join(sorted(p for p, _ in entries))
    print(
        f"verify_update_manifest: ok（version {manifest['version']}，平台 {platforms}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
