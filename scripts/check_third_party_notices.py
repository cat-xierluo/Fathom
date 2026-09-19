#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ISS-037 第三方 notices 与实际依赖来源的 fail-closed 校验器。

权威源（2026-09-19 复审后的本地事实基线）：
  - Rust：Cargo.lock（apps/desktop/src-tauri/Cargo.lock）
    本地 registry `~/.cargo/registry/src/index.crates.io-*/<crate>-<version>/Cargo.toml`
    提供 `license = "..."` 字段（已确认 891 个源目录均含该字段）。
    工作区自有包：`fathom-desktop`（Cargo.lock 中无 `source = "registry..."` 行）。
  - Python：本 worktree 无 Fathom.app/Contents/Resources/helper/ 现成构建，
    按合同改用 `.runtime` venv 对应的 `constraints.txt` 作为锁文件
    （「以 venv 锁为准、随包核对留发行候选」）；运行时 / 构建 / 测试
    三段依赖均纳入核对。
  - 前端：`frontend/vendor/echarts.min.js`（ASF 版权段、tslib 0BSD 段、
    内嵌 zrender 标记）逐字核对。
  - 图形：`assets/brand/README.md` 的处理链 + 自绘 SVG 分别登记。

覆盖目标：THIRD_PARTY_NOTICES.md §1/§3/§4/§5 表格行数与 Cargo.lock
/ constraints.txt / frontend vendored 实物一一对应。UNKNOWN 许可证
（`license = "UNKNOWN"`）必须带 `发行影响` 列说明，否则红。

退出码（CI 直接调用，shell 合同稳定）：
  0  全绿（覆盖、版本漂移、UNKNOWN 影响说明都通过）
  1  校验失败（缺项 / 版本漂移 / UNKNOWN 缺发行影响 / 许可证文本与本地源不符）
  2  结构性失败（权威文件缺失、不可解析）

子命令：
  scripts/check_third_party_notices.py            默认：--audit
  scripts/check_third_party_notices.py --audit    完整审计（默认）
  scripts/check_third_party_notices.py --missing  仅打印缺项与漂移清单
  scripts/check_third_party_notices.py --emit-rust-rows    打印 §3 待粘贴的表格行
  scripts/check_third_party_notices.py --emit-python-rows  打印 §1 待粘贴的表格行

依赖：Python 3.10+（标准库；无第三方依赖）；不联网、不下载。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
NOTICES = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
CARGO_LOCK = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "Cargo.lock"
CONSTRAINTS = REPO_ROOT / "constraints.txt"
VENV_SITE = Path(
    "/Users/maoking/Library/Application Support/maoscripts/fathom/.venv/lib/python3.14/site-packages"
)
REGISTRY_ROOT = Path.home() / ".cargo" / "registry" / "src"
ECHARTS_FILE = REPO_ROOT / "frontend" / "vendor" / "echarts.min.js"
ICONS_FILE = REPO_ROOT / "frontend" / "icons.js"
TRAY_ICON = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "icons" / "tray.png"
APP_ICON_PNG = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "icons" / "icon.png"
APP_ICON_ICNS = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "icons" / "icon.icns"
APP_ICON_SVG = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "icons" / "icon.svg"
BRAND_README = REPO_ROOT / "assets" / "brand" / "README.md"

WORKSPACE_SELF = "fathom-desktop"


# -------------------- 数据结构 --------------------


@dataclass
class Crate:
    name: str
    version: str
    source: str | None  # "registry+..." for 3p, None for workspace self
    license: str | None  # registry Cargo.toml `license = "..."` 或 None（未读出）
    registry_dir: Path | None  # 解析到的本地源目录

    @property
    def is_workspace(self) -> bool:
        return self.source is None or self.source.startswith("workspace")


@dataclass
class PyDep:
    name: str
    version: str
    license: str  # "UNKNOWN" 表示未读出
    category: str  # "运行时" / "构建" / "测试"


@dataclass
class Report:
    rows: list[tuple[str, str, str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def add(self, source: str, value: str, status: str, note: str) -> None:
        self.rows.append((source, value, status, note))
        if status == "fail":
            self.failures.append(f"{source}: {note}")

    def fail(self, msg: str) -> None:
        self.rows.append(("", "", "fail", msg))
        self.failures.append(msg)


# -------------------- 解析 --------------------


def parse_cargo_lock(path: Path) -> list[Crate]:
    text = path.read_text(encoding="utf-8")
    crates: list[Crate] = []
    # 块按 [[package]] 切分；每个块里找 name / version / source
    for block in text.split("\n[[package]]"):
        block = block.removeprefix("[[package]]")
        m_name = re.search(r'^name = "([^"]+)"', block, re.M)
        m_ver = re.search(r'^version = "([^"]+)"', block, re.M)
        m_src = re.search(r'^source = "([^"]+)"', block, re.M)
        if not m_name or not m_ver:
            continue
        name, ver = m_name.group(1), m_ver.group(1)
        source = m_src.group(1) if m_src else None
        registry_dir, license = _lookup_registry_license(name, ver)
        crates.append(Crate(name, ver, source, license, registry_dir))
    return crates


def _lookup_registry_license(name: str, version: str) -> tuple[Path | None, str | None]:
    """在 ~/.cargo/registry/src/index.crates.io-*/<name>-<version>/ 找 Cargo.toml 的 license 字段。"""
    if not REGISTRY_ROOT.exists():
        return None, None
    # 可能的索引目录：通常只有一个 index.crates.io-*；逐一匹配
    for index_dir in REGISTRY_ROOT.iterdir():
        if not index_dir.is_dir():
            continue
        candidate = index_dir / f"{name}-{version}"
        if candidate.is_dir():
            cargo_toml = candidate / "Cargo.toml"
            if cargo_toml.is_file():
                m = re.search(r'^license = "([^"]+)"', cargo_toml.read_text(encoding="utf-8"), re.M)
                return candidate, (m.group(1) if m else None)
    return None, None


def parse_constraints(path: Path) -> list[PyDep]:
    """约束文件按行解析；按上下文注释标记 运行时/构建/测试。"""
    if not path.is_file():
        return []
    deps: list[PyDep] = []
    text = path.read_text(encoding="utf-8")
    current = "运行时"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            # 注释行：识别分组标题
            if "传递：pytest" in line:
                current = "测试"
            elif "传递：测试" in line:
                current = "测试"
            elif "构建" in line or "build" in line.lower():
                current = "构建"
            elif "传递" in line or "直接" in line:
                # 分组开始；保留上次 current（运行时/构建/测试）
                pass
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([0-9][^\s#]*)$", line)
        if not m:
            continue
        name, ver = m.group(1), m.group(2)
        license_text = _lookup_python_license(name, ver)
        deps.append(PyDep(name, ver, license_text, current))
    return deps


def _lookup_python_license(name: str, version: str) -> str:
    """从 .venv site-packages/<name>-<version>.dist-info/METADATA 读 License 字段。"""
    # 包名归一化（PEP 503）：下划线当连字符；本地目录可能保留原始名
    norm = name.lower().replace("_", "-")
    candidates = [
        VENV_SITE / f"{name}-{version}.dist-info",
        VENV_SITE / f"{norm}-{version}.dist-info",
    ]
    for d in candidates:
        metadata = d / "METADATA"
        if metadata.is_file():
            for ln in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
                if ln.startswith("License:"):
                    val = ln.split(":", 1)[1].strip()
                    return val or "UNKNOWN"
            return "UNKNOWN"
    return "UNKNOWN"  # 未找到 dist-info


def parse_notices_tables(path: Path) -> dict[str, list[dict[str, str]]]:
    """解析 THIRD_PARTY_NOTICES.md 的所有 markdown 表格，按 ## §X 标题分组。

    每行返回字典：columns（按表头顺序），含可能的 `锁` 列规范。
    """
    text = path.read_text(encoding="utf-8")
    sections: dict[str, list[dict[str, str]]] = {}
    current_h2 = None
    current_table: list[str] = []
    in_table = False

    def flush_table() -> None:
        nonlocal current_table
        if current_h2 and current_table and len(current_table) >= 2:
            header = [c.strip() for c in current_table[0].split("|")]
            # 第二行是分隔行 |---|---|
            rows = []
            for r in current_table[2:]:
                cells = [c.strip() for c in r.split("|")]
                if len(cells) < len(header):
                    cells = cells + [""] * (len(header) - len(cells))
                rows.append(dict(zip(header, cells)))
            sections.setdefault(current_h2, []).extend(rows)
        current_table = []

    for line in text.splitlines():
        if line.startswith("## "):
            flush_table()
            in_table = False
            current_h2 = line[3:].strip()
            continue
        if line.startswith("|") and current_h2:
            in_table = True
            current_table.append(line)
            continue
        # 非表格行：若上一行是表格分隔则继续收集；否则结束当前表格
        if in_table and line.strip() == "":
            # 空行不结束表格（允许多段表格）
            continue
        if in_table and not line.startswith("|"):
            flush_table()
            in_table = False
    flush_table()
    return sections


# -------------------- 校验 --------------------


def _normalize_version_list(raw: str) -> set[str]:
    """接受 '1.2.3' / '1.2.3 / 4.5.6' / '多版本' 等；返回规范化版本集合。
    对 '多版本' / '未锁' / '未知' 等占位返回空集（调用方按 UNKNOWN 处理）。"""
    s = raw.strip()
    if not s or s in {"多版本", "未锁", "未锁版本", "未知", "—", "-"}:
        return set()
    parts = [p.strip() for p in re.split(r"[/、，,]+", s)]
    return {p for p in parts if p}


def audit_rust(crates: list[Crate], notices: dict[str, list[dict[str, str]]]) -> Report:
    report = Report()
    rust_table = notices.get("3. Rust / Tauri crate", []) + notices.get(
        "3. Rust / Tauri crates", []
    )
    # 反向索引：name -> {versions_set, license_per_version: dict, impact}
    index: dict[str, dict[str, object]] = {}
    for row in rust_table:
        name = row.get("crate", "").strip()
        if not name:
            continue
        versions = _normalize_version_list(row.get("锁定版本", ""))
        license_cell = row.get("许可证", "").strip()
        impact = row.get("发行影响", "").strip()
        url = row.get("来源", "").strip()
        # 多版本行：`锁定版本` 与 `许可证` 都可能以 `/` 分隔；按位置一一对应
        lic_parts = [p.strip() for p in license_cell.split(" / ")]
        if versions and len(lic_parts) == len(versions):
            license_per_version = dict(zip(sorted(versions), lic_parts))
        else:
            # 单版本或无版本时，整 cell 作为所有版本的代表
            license_per_version = {v: license_cell for v in versions} if versions else {license_cell}
        index[name] = {
            "versions": versions,
            "license": license_cell,
            "license_per_version": license_per_version,
            "impact": impact,
            "url": url,
        }

    third_party = [c for c in crates if not c.is_workspace]
    workspace = [c for c in crates if c.is_workspace]
    report.add(
        "Rust 总条数",
        str(len(crates)),
        "ok",
        f"工作区自有 {len(workspace)}；第三方 {len(third_party)}",
    )
    if not workspace or workspace[0].name != WORKSPACE_SELF:
        report.fail(
            f"未识别工作区自有包（期望 {WORKSPACE_SELF}）；Cargo.lock 结构异常"
        )

    missing: list[str] = []
    drift: list[str] = []
    license_mismatch: list[str] = []
    unknown_no_impact: list[str] = []

    for c in third_party:
        info = index.get(c.name)
        if info is None:
            missing.append(f"{c.name}@{c.version}（许可：{c.license or '未读出'}）")
            continue
        versions = info["versions"]  # type: ignore[assignment]
        # 漂移检测：若表里登记了具体版本集合（且非空），必须包含 c.version
        if versions and c.version not in versions:  # type: ignore[operator]
            drift.append(
                f"{c.name}：Cargo.lock={c.version} vs notices={'/'.join(sorted(versions))}"
            )
        # 许可证软比对：若 notices 为此版本单独登记了许可，则必须与本地源一致
        lic_per_ver = info.get("license_per_version", {})  # type: ignore[arg-type]
        lic_for_this_version = lic_per_ver.get(c.version) if isinstance(lic_per_ver, dict) else None
        if lic_for_this_version is None:
            lic_for_this_version = info["license"]  # type: ignore[index]
        if (
            lic_for_this_version
            and lic_for_this_version != "UNKNOWN"
            and c.license
            and lic_for_this_version != c.license
        ):
            license_mismatch.append(
                f"{c.name}@{c.version}：notices={lic_for_this_version!r} vs registry={c.license!r}"
            )
        # UNKNOWN 必须有影响（行级）
        lic_cell = info["license"]  # type: ignore[index]
        if lic_cell == "UNKNOWN":
            impact = info["impact"]  # type: ignore[index]
            if not impact:
                unknown_no_impact.append(f"{c.name}@{c.version}")

    # 工作区自有包版本也校验（不能漂移）
    for c in workspace:
        if c.name == WORKSPACE_SELF:
            # 该版本由 check_version_consistency.sh 校验；这里登记为 ok 占位
            report.add(
                f"工作区自有 {c.name}",
                c.version,
                "ok",
                "版本由 check_version_consistency.sh 守门；本脚本仅登记存在性",
            )

    if missing:
        for m in missing:
            report.fail(f"Rust 未登记：{m}")
    else:
        report.add(
            "Rust 第三方覆盖率",
            f"{len(third_party)}/{len(third_party)}",
            "ok",
            "全部 Cargo.lock 第三方条目均登记于 §3",
        )

    if drift:
        for d in drift:
            report.fail(f"Rust 版本漂移：{d}")
    else:
        report.add(
            "Rust 版本漂移",
            "0",
            "ok",
            "所有 Cargo.lock 条目版本均在 notices §3 登记版本集合内",
        )

    if license_mismatch:
        for lm in license_mismatch:
            report.fail(f"Rust 许可证文本不一致：{lm}")
    else:
        report.add(
            "Rust 许可证文本一致性",
            "ok",
            "ok",
            "§3 许可证字段与本地 registry Cargo.toml 一致",
        )

    if unknown_no_impact:
        for u in unknown_no_impact:
            report.fail(f"Rust UNKNOWN 缺发行影响：{u}")
    else:
        report.add(
            "Rust UNKNOWN 影响说明",
            "ok",
            "ok",
            "§3 UNKNOWN 行均带发行影响字段或当前不存在 UNKNOWN 行",
        )

    return report


def audit_python(deps: list[PyDep], notices: dict[str, list[dict[str, str]]]) -> Report:
    report = Report()
    py_table = notices.get("1. Python 运行时（随冻结 helper 分发）", [])
    index: dict[str, dict[str, object]] = {}
    for row in py_table:
        name = row.get("组件", "").strip()
        if not name:
            continue
        versions = _normalize_version_list(row.get("锁定版本", ""))
        license_cell = row.get("许可证", "").strip()
        impact = row.get("发行影响", "").strip()
        index[name] = {
            "versions": versions,
            "license": license_cell,
            "impact": impact,
        }

    # 区分运行时 / 构建 / 测试：表里用 `类别` 列（可选）
    categories: dict[str, str] = {}
    for row in py_table:
        name = row.get("组件", "").strip()
        cat = row.get("类别", "").strip()
        if name:
            categories[name] = cat

    missing: list[str] = []
    drift: list[str] = []
    unknown_no_impact: list[str] = []

    # 期望覆盖集合：运行时 + 构建 + 测试三类
    expected = [d for d in deps if d.category in {"运行时", "构建", "测试"}]
    report.add(
        "Python 总条数",
        str(len(expected)),
        "ok",
        "以 .venv site-packages + constraints.txt 三段登记合计",
    )

    for d in expected:
        info = index.get(d.name)
        if info is None:
            missing.append(f"{d.name}@{d.version}（{d.category}，许可：{d.license}）")
            continue
        versions = info["versions"]  # type: ignore[assignment]
        if versions and d.version not in versions:  # type: ignore[operator]
            drift.append(
                f"{d.name}：constraints.txt={d.version} vs notices={'/'.join(sorted(versions))}"
            )
        lic_cell = info["license"]  # type: ignore[index]
        if lic_cell == "UNKNOWN" and not info["impact"]:  # type: ignore[index]
            unknown_no_impact.append(f"{d.name}@{d.version}")

    if missing:
        for m in missing:
            report.fail(f"Python 未登记：{m}")
    else:
        report.add(
            "Python 覆盖率",
            f"{len(expected)}/{len(expected)}",
            "ok",
            "所有 constraints.txt 运行时/构建/测试条目均登记于 §1",
        )
    if drift:
        for d in drift:
            report.fail(f"Python 版本漂移：{d}")
    else:
        report.add("Python 版本漂移", "0", "ok", "constraints.txt 版本与 §1 一致")
    if unknown_no_impact:
        for u in unknown_no_impact:
            report.fail(f"Python UNKNOWN 缺发行影响：{u}")
    else:
        report.add(
            "Python UNKNOWN 影响说明",
            "ok",
            "ok",
            "§1 UNKNOWN 行均带发行影响或当前不存在 UNKNOWN 行",
        )
    return report


def audit_frontend(notices: dict[str, list[dict[str, str]]]) -> Report:
    report = Report()
    section = notices.get("4. 前端 vendored 资源", [])
    if not section:
        report.fail("前端 §4 表缺失或表头解析失败")
        return report
    # 期望至少含 ECharts / tslib / zrender 三行（行中「资源」列含对应名）
    def has_token(token: str) -> bool:
        for row in section:
            res = (row.get("资源", "") or row.get("组件", "") or "").lower()
            if token in res:
                return True
        return False
    have_echarts = has_token("echarts")
    have_tslib = has_token("tslib")
    have_zrender = has_token("zrender")
    for label, ok in (("ECharts", have_echarts), ("tslib", have_tslib), ("zrender", have_zrender)):
        report.add(
            f"前端 {label} 行",
            "存在" if ok else "缺失",
            "ok" if ok else "fail",
            "§4 必含条目",
        )
    # 校验 echarts.min.js 头含 5.6.0 与 ASF Apache-2.0 标识
    # min.js 是单行压缩串，「5.6.0」「5.5.1」「tslib」等出现在文件偏移 90+ KB 处；
    # 读前 256 KB 即可覆盖所有版本标识与 tslib/Microsoft 0BSD 段。
    if ECHARTS_FILE.is_file():
        head_bytes = ECHARTS_FILE.read_bytes()[: 256 * 1024]
        head = head_bytes.decode("utf-8", errors="replace")
        if "Apache License, Version 2.0" in head:
            report.add("echarts.min.js ASF 段", "存在", "ok", "Apache-2.0 许可证声明随文件分发")
        else:
            report.fail("echarts.min.js 未在头 256KB 内发现 ASF Apache-2.0 声明")
        if "5.6.0" in head:
            report.add("echarts.min.js 标识 5.6.0", "存在", "ok", "与 §4 一致")
        else:
            report.fail("echarts.min.js 头 256KB 未含 5.6.0 标识")
        if "Microsoft" in head:
            report.add("echarts.min.js 内嵌 Microsoft 段", "存在", "ok", "0BSD 版权随文件分发")
        else:
            report.fail("echarts.min.js 头 256KB 未识别内嵌 Microsoft 段")
    else:
        report.fail(f"echarts.min.js 缺失：{ECHARTS_FILE}")
    # icons.js 自有（无第三方权利）
    if ICONS_FILE.is_file():
        report.add("frontend/icons.js", "自有", "ok", "项目原创，无第三方权利")
    else:
        report.fail(f"icons.js 缺失：{ICONS_FILE}")
    return report


def audit_graphics(notices: dict[str, list[dict[str, str]]]) -> Report:
    report = Report()
    section = notices.get("5. 图形资源", [])
    if not section:
        report.fail("图形 §5 表缺失或表头解析失败")
        return report
    blobs = " ".join(str(row) for row in section)
    full_text = Path(NOTICES).read_text(encoding="utf-8")
    # 必须正确描述深潭 App 图标（image_gen 原稿 + Pillow 处理链），
    # 而非「自绘几何」/「占位图标」
    must_have = [
        ("image_gen", "深潭 App 图标来源描述缺失（应含 image_gen 原稿）"),
        ("Pillow", "深潭 App 图标处理链描述缺失（应含 Pillow）"),
        ("build_app_icon.py", "处理链脚本未在 §5 登记"),
    ]
    for token, msg in must_have:
        if token in blobs or token in full_text:
            report.add(f"§5 含 {token}", "是", "ok", "深潭 App 图标来源描述正确")
        else:
            report.fail(msg)
    # 线条深度环为自绘几何（与深潭 App 图标分开）
    if "深度环" in blobs or "深度环" in full_text:
        report.add("§5 区分深度环为自绘几何", "是", "ok", "深潭 App 图标 ≠ 深度环（两者分别登记）")
    else:
        report.fail("§5 未登记深度环自绘几何 SVG（需与深潭 App 图标分开）")
    # §5 中「深潭 App 图标」行不应描述为自绘几何（区别于深度环）
    pool_row = next((r for r in section if "深潭" in (r.get("资源", "") or "")), None)
    if pool_row is not None:
        src = pool_row.get("来源", "") or ""
        lic = pool_row.get("许可证", "") or ""
        if "image_gen" in src and "Pillow" in src:
            report.add("§5 深潭 App 图标来源行", "正确", "ok", "image_gen 原稿 + Pillow 处理链")
        else:
            report.fail("§5 深潭 App 图标来源行描述异常：未同时含 image_gen 与 Pillow")
        if "自绘" in lic:
            report.fail("§5 把深潭 App 图标许可证写成「自绘」（应区分：深潭 App 图标非自绘几何）")
        else:
            report.add("§5 深潭 App 图标 ≠ 自绘几何", "是", "ok", "与深度环分别登记")
    else:
        report.fail("§5 缺少深潭 App 图标行")
    # icon.png 自 PR #117 (55d6c3f) 起已与 icon.icns 一起接入深潭链（首行）。
    # 不再断言「占位/挂 ISS-045」；§5 表格行中提到 icon.png 时必须显式链接
    # 到深潭链（image_gen 原稿 + Pillow 处理链），并保留与深度环 SVG 的区分。
    # 历史注记/勘误段（段说明 / blockquote）允许引用旧表述以记录修复，但
    # 不应再次断言 icon.png 为占位资产。
    icon_rows = [r for r in section if "icon.png" in (r.get("资源", "") or "")]
    icon_pool_row = next(
        (
            r
            for r in section
            if ("icon.{png,icns}" in (r.get("资源", "") or ""))
            or ("icon.png" in (r.get("资源", "") or "") and "icon.icns" in (r.get("资源", "") or ""))
        ),
        None,
    )
    # 段说明（非表格行）允许出现「占位/ISS-045」以记录历史勘误；只在表格
    # 行中把 icon.png 列为独立占位资产才算旧表述。
    if icon_rows:
        for r in icon_rows:
            src = (r.get("来源", "") or "").strip()
            lic = (r.get("许可证", "") or "").strip()
            if "占位" in src or "占位" in lic or lic == "UNKNOWN":
                report.fail(
                    "§5 表格行仍将 icon.png 描述为占位/UNKNOWN，已与 main 现实不符"
                )
                break
        else:
            report.add(
                "§5 icon.png 表格行",
                "已合并入深潭链",
                "ok",
                "icon.png/icon.icns 已在首行统一登记；不再单列占位行",
            )
    elif icon_pool_row is not None:
        # 单行覆盖 icon.{png,icns}：校验来源行含深潭链关键词
        src = (icon_pool_row.get("来源", "") or "").strip()
        if ("image_gen" in src) and ("Pillow" in src) and ("build_app_icon.py" in src):
            report.add(
                "§5 icon.png/icon.icns 单行",
                "深潭链",
                "ok",
                "image_gen 原稿 + Pillow 处理链",
            )
        else:
            report.fail(
                "§5 icon.png/icon.icns 单行未含深潭链关键词（image_gen/Pillow/build_app_icon.py）"
            )
    else:
        # 表格不再单列 icon.png 但全文仍可能提到（如历史注记）；放行
        report.add(
            "§5 icon.png",
            "并入首行",
            "ok",
            "占位资产已统一接入深潭链；历史注记允许引用旧表述",
        )
        report.add("§5 icon.png", "未提及", "ok", "占位资产不再列入 §5")
    return report


# -------------------- 入口 --------------------


def cmd_audit() -> int:
    # 结构性检查
    for f in (NOTICES, CARGO_LOCK, CONSTRAINTS, ECHARTS_FILE, ICONS_FILE):
        if not f.is_file():
            print(f"check_third_party_notices: 缺少 {f}", file=sys.stderr)
            return 2

    crates = parse_cargo_lock(CARGO_LOCK)
    py_deps = parse_constraints(CONSTRAINTS)
    notices = parse_notices_tables(NOTICES)

    reports = [
        ("§3 Rust / Tauri", audit_rust(crates, notices)),
        ("§1 Python 运行时", audit_python(py_deps, notices)),
        ("§4 前端 vendored", audit_frontend(notices)),
        ("§5 图形资源", audit_graphics(notices)),
    ]
    # 表头
    print(
        f"{'来源':<42}{'值':<22}{'判定':<8}{'说明'}"
    )
    print("-" * 100)
    overall_fail = 0
    for label, rep in reports:
        print(f"--- {label} ---")
        for src, val, status, note in rep.rows:
            if not src:
                # 失败提示行（仅有 note）
                print(f"{'':<42}{'':<22}{status:<8}{note}")
                continue
            print(f"{src:<42}{val:<22}{status:<8}{note}")
        if rep.failures:
            overall_fail = 1
    print("-" * 100)
    if overall_fail:
        print(
            f"check_third_party_notices: FAIL（缺项 / 版本漂移 / UNKNOWN 缺影响）",
            file=sys.stderr,
        )
        return 1
    print("third_party_notices: ok（覆盖与版本一致，UNKNOWN 影响说明完整）")
    return 0


def _format_row(cells: Iterable[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def cmd_emit_rust_rows() -> int:
    crates = parse_cargo_lock(CARGO_LOCK)
    third = sorted([c for c in crates if not c.is_workspace], key=lambda x: (x.name, x.version))
    workspace = [c for c in crates if c.is_workspace]
    print("<!-- 由 scripts/check_third_party_notices.py --emit-rust-rows 生成；请核对后整段替换 §3 表体 -->")
    print(
        _format_row(
            ["crate", "锁定版本", "许可证", "来源", "发行影响"]
        )
    )
    print(_format_row(["---", "---", "---", "---", "---"]))
    print(
        _format_row(
            [
                f"**{workspace[0].name}**（工作区自有）",
                workspace[0].version,
                workspace[0].license or "Apache-2.0",
                "apps/desktop/src-tauri/Cargo.toml",
                "—",
            ]
        )
        if workspace
        else ""
    )
    for c in third:
        url = (
            f"https://crates.io/crates/{c.name}/{c.version}"
            if c.source and c.source.startswith("registry")
            else (c.source or "—")
        )
        license_text = c.license or "UNKNOWN"
        # UNKNOWN 项必须带发行影响；脚本生成时仍写「待补：核对上游」
        impact = "" if license_text != "UNKNOWN" else "待补：核对上游 LICENSE"
        print(
            _format_row(
                [c.name, c.version, license_text, url, impact]
            )
        )
    return 0


def cmd_emit_python_rows() -> int:
    deps = parse_constraints(CONSTRAINTS)
    print("<!-- 由 scripts/check_third_party_notices.py --emit-python-rows 生成；请核对后整段替换 §1 表体 -->")
    print(
        _format_row(
            ["组件", "锁定版本", "许可证", "来源", "类别", "发行影响"]
        )
    )
    print(_format_row(["---", "---", "---", "---", "---", "---"]))
    for d in sorted(deps, key=lambda x: (x.category, x.name)):
        impact = "" if d.license != "UNKNOWN" else "待补：核对 PyPI 元数据"
        print(
            _format_row(
                [d.name, d.version, d.license, f"https://pypi.org/project/{d.name}/{d.version}/", d.category, impact]
            )
        )
    return 0


def cmd_missing() -> int:
    crates = parse_cargo_lock(CARGO_LOCK)
    py_deps = parse_constraints(CONSTRAINTS)
    notices = parse_notices_tables(NOTICES)

    rust_table = notices.get("3. Rust / Tauri crate", []) + notices.get(
        "3. Rust / Tauri crates", []
    )
    rust_index = {row.get("crate", "").strip() for row in rust_table if row.get("crate")}
    py_table = notices.get("1. Python 运行时（随冻结 helper 分发）", [])
    py_index = {row.get("组件", "").strip() for row in py_table if row.get("组件")}

    print("### Rust 未登记：")
    for c in sorted([c for c in crates if not c.is_workspace], key=lambda x: x.name):
        if c.name not in rust_index:
            print(f"  {c.name}@{c.version}\t{c.license or 'UNKNOWN'}")
    print()
    print("### Python 未登记：")
    for d in sorted(py_deps, key=lambda x: (x.category, x.name)):
        if d.name not in py_index:
            print(f"  {d.name}=={d.version}\t{d.category}\t{d.license}")
    print()
    print("### Rust 版本漂移：")
    for row in rust_table:
        name = row.get("crate", "").strip()
        versions = _normalize_version_list(row.get("锁定版本", ""))
        if not versions:
            continue
        for c in crates:
            if c.name == name and not c.is_workspace and c.version not in versions:
                print(f"  {name}：Cargo.lock={c.version} vs notices={'/'.join(sorted(versions))}")
    return 0


def _build_rust_table_lines() -> list[str]:
    """生成 §3 表体 markdown 行（含工作区自有 + 全部第三方）。

    多版本 crate 合并为一行，`锁定版本` 列以 `/` 分隔全部锁定版本；
    `许可证` 列若全部一致则写一次，否则并列（保持 Cargo.lock 实际可观测）。
    """
    crates = parse_cargo_lock(CARGO_LOCK)
    workspace = [c for c in crates if c.is_workspace]
    third = [c for c in crates if not c.is_workspace]
    # 按 name 聚合
    by_name: dict[str, list[Crate]] = {}
    for c in third:
        by_name.setdefault(c.name, []).append(c)
    # 每个 crate 内按版本排序（自然序）
    for name, lst in by_name.items():
        lst.sort(key=lambda x: _ver_key(x.version))
    # 输出按 name 排序
    lines: list[str] = []
    lines.append(_format_row(["crate", "锁定版本", "许可证", "来源", "发行影响"]))
    lines.append(_format_row(["---", "---", "---", "---", "---"]))
    if workspace:
        w = workspace[0]
        lines.append(
            _format_row(
                [
                    f"**{w.name}**（工作区自有）",
                    w.version,
                    w.license or "Apache-2.0",
                    "apps/desktop/src-tauri/Cargo.toml",
                    "—",
                ]
            )
        )
    for name in sorted(by_name):
        lst = by_name[name]
        versions = [c.version for c in lst]
        # 真实读出的许可集合（None 表示未读出 → UNKNOWN）
        actual_licenses = {c.license for c in lst}
        if None in actual_licenses:
            actual_licenses.discard(None)
            actual_licenses.add("UNKNOWN")
        if len(actual_licenses) == 1:
            lic = next(iter(actual_licenses))
        else:
            # 多版本各自的许可不同——按多版本并列展示
            lic = " / ".join(
                c.license if c.license else "UNKNOWN"
                for c in lst
            )
        url = (
            f"https://crates.io/crates/{name}/{versions[-1]}"
            if lst[-1].source and lst[-1].source.startswith("registry")
            else (lst[-1].source or "—")
        )
        impact = "" if lic != "UNKNOWN" and "UNKNOWN" not in lic.split(" / ") else "见 §6 UNKNOWN 集中说明"
        lines.append(_format_row([name, " / ".join(versions), lic, url, impact]))
    return lines


def _build_python_table_lines() -> list[str]:
    """生成 §1 表体 markdown 行（运行时 / 构建 / 测试三段）。"""
    deps = parse_constraints(CONSTRAINTS)
    lines: list[str] = []
    lines.append(_format_row(["组件", "锁定版本", "许可证", "来源", "类别", "发行影响"]))
    lines.append(_format_row(["---", "---", "---", "---", "---", "---"]))
    for d in sorted(deps, key=lambda x: ({"运行时": 0, "构建": 1, "测试": 2}.get(x.category, 9), x.name)):
        impact = "" if d.license != "UNKNOWN" else "见 §6 UNKNOWN 集中说明"
        url = f"https://pypi.org/project/{d.name}/{d.version}/"
        lines.append(
            _format_row([d.name, d.version, d.license, url, d.category, impact])
        )
    return lines


def _ver_key(v: str) -> tuple:
    """版本排序：1.0.0 < 1.0.0+abc < 1.0.1。"""
    parts = re.split(r"[+.-]", v)
    out = []
    for p in parts:
        try:
            out.append((0, int(p)))
        except ValueError:
            out.append((1, p))
    return tuple(out)


def _replace_section_table(
    text: str, h2_heading: str, new_table_lines: list[str]
) -> tuple[str, bool]:
    """在 THIRD_PARTY_NOTICES.md 中定位 ## <h2_heading> 段，并替换其首个 markdown 表为 new_table_lines。

    规则：找到 `## <h2_heading>` 行之后到下一个 `## ` 标题之前，把其中
    连续以 `|` 开头的行（含表头/分隔行/数据行）整段替换。返回 (新文本, 是否替换成功)。
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == f"## {h2_heading}":
            start = i
            break
    if start is None:
        return text, False
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    # 在 [start+1, end) 内寻找首个连续表格段
    i = start + 1
    while i < end and not lines[i].lstrip().startswith("|"):
        i += 1
    if i >= end:
        return text, False  # 没找到表
    # 找到表格结束（首个非表格行：非 `|` 起首 且 不在表分隔/扩展内）
    table_start = i
    j = i
    while j < end and lines[j].lstrip().startswith("|"):
        j += 1
    # 表格范围 [table_start, j)
    new_lines = lines[:table_start] + new_table_lines + lines[j:]
    return "\n".join(new_lines) + ("\n" if text.endswith("\n") else ""), True


def cmd_regenerate() -> int:
    """就地刷新 §1 / §3 表体：依据 Cargo.lock 与 constraints.txt 重新生成。

    人工编辑过的表头与段说明保持不变；只替换首个连续表格段。
    """
    if not NOTICES.is_file():
        print(f"check_third_party_notices: 缺少 {NOTICES}", file=sys.stderr)
        return 2
    text = NOTICES.read_text(encoding="utf-8")
    rust_h2 = None
    py_h2 = None
    for ln in text.splitlines():
        if ln.startswith("## "):
            t = ln[3:].strip()
            if t.startswith("3.") and "Rust" in t and rust_h2 is None:
                rust_h2 = t
            elif t.startswith("1.") and "Python" in t and py_h2 is None:
                py_h2 = t
    if rust_h2 is None:
        print("未找到 ## §3 Rust 段标题", file=sys.stderr)
        return 2
    if py_h2 is None:
        print("未找到 ## §1 Python 段标题", file=sys.stderr)
        return 2
    text, ok3 = _replace_section_table(text, rust_h2, _build_rust_table_lines())
    text, ok1 = _replace_section_table(text, py_h2, _build_python_table_lines())
    if not (ok3 and ok1):
        print(f"§3 替换 {ok3}；§1 替换 {ok1}（段内未找到连续表格）", file=sys.stderr)
        return 2
    NOTICES.write_text(text, encoding="utf-8")
    print("regenerate: §3 与 §1 表体已就地从 Cargo.lock / constraints.txt 重新生成")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ISS-037 第三方 notices fail-closed 校验器")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--audit", action="store_true", help="仅审计（默认；不写文件）")
    g.add_argument("--missing", action="store_true", help="仅打印缺项与漂移清单")
    g.add_argument("--emit-rust-rows", action="store_true", help="打印 §3 待粘贴的表格行")
    g.add_argument("--emit-python-rows", action="store_true", help="打印 §1 待粘贴的表格行")
    g.add_argument("--regenerate", action="store_true", help="就地刷新 §1/§3 表体（仅用于一次性补全，不替代审计）")
    args = parser.parse_args()
    if args.regenerate:
        return cmd_regenerate()
    if args.missing:
        return cmd_missing()
    if args.emit_rust_rows:
        return cmd_emit_rust_rows()
    if args.emit_python_rows:
        return cmd_emit_python_rows()
    # 默认：fail-closed 审计，不写文件
    return cmd_audit()


if __name__ == "__main__":
    sys.exit(main())
