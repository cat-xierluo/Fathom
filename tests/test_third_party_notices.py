# -*- coding: utf-8 -*-
"""ISS-037 repair3：notices 校验器许可证值级交叉核对的单测。

背景（缺陷③反例）：repair2 后 THIRD_PARTY_NOTICES.md 中 certifi 与 pluggy
许可证值互换（本地 METADATA 实测 certifi=MPL-2.0、pluggy=MIT，表内写反），
而校验器只解析许可证字段、未做值级核对，互换静默通过（exit 0）。
本轮为 audit_python 补「表内具体许可证值 vs 本地 METADATA（PEP 639 三级
解析）」的 fail-closed 核对，并显式区分「本地无该包 → SKIP 放行」与
「核对过且一致」。以下用假 venv / 假表结构覆盖各分支。

不联网、不读真实 HOME 扫描；VENV_SITE 经 monkeypatch 指向 tmp_path。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_third_party_notices as ctpn  # noqa: E402


PY_SECTION = "1. Python 运行时（随冻结 helper 分发）"


def _py_dep(
    name: str,
    version: str = "1.0.0",
    license_: str = "MIT",
    category: str = "构建",
    metadata_found: bool = True,
) -> ctpn.PyDep:
    return ctpn.PyDep(name, version, license_, category, metadata_found)


def _notices_rows(*rows: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    base = {
        "锁定版本": "1.0.0",
        "许可证": "MIT",
        "来源": "https://example.invalid/",
        "类别": "构建",
        "发行影响": "",
    }
    merged = [{**base, **r} for r in rows]
    return {PY_SECTION: merged}


def _failures(report: ctpn.Report) -> list[str]:
    return report.failures


def _notes(report: ctpn.Report) -> str:
    return "\n".join(note for *_head, note in report.rows)


def _make_fake_venv(
    tmp_path: Path, packages: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """构造假 site-packages：<name>-1.0.0.dist-info/METADATA，正文为给定文本。"""
    site = tmp_path / "site-packages"
    site.mkdir()
    for name, metadata_text in packages.items():
        d = site / f"{name}-1.0.0.dist-info"
        d.mkdir()
        (d / "METADATA").write_text(metadata_text, encoding="utf-8")
    monkeypatch.setattr(ctpn, "VENV_SITE", site)


# ---------- _lookup_python_license：metadata_found 语义与三级解析 ----------


def test_lookup_found_reads_license_field(tmp_path, monkeypatch):
    monkeypatch.setattr(ctpn, "VENV_SITE", tmp_path / "site-packages")
    (tmp_path / "site-packages").mkdir()
    d = tmp_path / "site-packages" / "demo-1.0.0.dist-info"
    d.mkdir()
    (d / "METADATA").write_text(
        "Name: demo\nLicense: MPL-2.0\n", encoding="utf-8"
    )
    value, found = ctpn._lookup_python_license("demo", "1.0.0")
    assert (value, found) == ("MPL-2.0", True)


def test_lookup_missing_dist_info_reports_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(ctpn, "VENV_SITE", tmp_path / "site-packages")
    (tmp_path / "site-packages").mkdir()
    value, found = ctpn._lookup_python_license("absent", "1.0.0")
    assert value == "UNKNOWN"
    assert found is False  # 本地无该包：值级核对必须走 SKIP 分支


def test_lookup_license_expression_precedence(tmp_path, monkeypatch):
    """PEP 639 License-Expression 优先于旧式 License 与分类器（repair2 成果）。"""
    monkeypatch.setattr(ctpn, "VENV_SITE", tmp_path / "site-packages")
    (tmp_path / "site-packages").mkdir()
    d = tmp_path / "site-packages" / "modern-1.0.0.dist-info"
    d.mkdir()
    (d / "METADATA").write_text(
        "Name: modern\n"
        "License-Expression: BSD-3-Clause\n"
        "License: legacy-not-this\n"
        "Classifier: License :: OSI Approved :: MIT License\n",
        encoding="utf-8",
    )
    value, found = ctpn._lookup_python_license("modern", "1.0.0")
    assert (value, found) == ("BSD-3-Clause", True)


def test_lookup_all_levels_empty_returns_unknown_found(tmp_path, monkeypatch):
    monkeypatch.setattr(ctpn, "VENV_SITE", tmp_path / "site-packages")
    (tmp_path / "site-packages").mkdir()
    d = tmp_path / "site-packages" / "silent-1.0.0.dist-info"
    d.mkdir()
    (d / "METADATA").write_text("Name: silent\nVersion: 1.0.0\n", encoding="utf-8")
    value, found = ctpn._lookup_python_license("silent", "1.0.0")
    assert value == "UNKNOWN"
    assert found is True  # METADATA 在，但三级全空


# ---------- audit_python：值级交叉核对 ----------


def test_value_mismatch_fails_closed_with_both_sides():
    """表内值 ≠ METADATA 值 → fail，且消息指明包与两侧值（缺陷③防回归核心）。"""
    deps = [_py_dep("demo", license_="MPL-2.0", metadata_found=True)]
    notices = _notices_rows({"组件": "demo", "许可证": "MIT"})
    report = ctpn.audit_python(deps, notices)
    failures = _failures(report)
    assert any(
        "许可证值不一致" in f and "demo" in f and "MIT" in f and "MPL-2.0" in f
        for f in failures
    ), failures


def test_value_match_ok_counts_checked():
    deps = [_py_dep("demo", license_="MIT", metadata_found=True)]
    notices = _notices_rows({"组件": "demo", "许可证": "MIT"})
    report = ctpn.audit_python(deps, notices)
    assert not _failures(report), _failures(report)
    # 报告行：值列为核对数 1、判定 ok
    row = next(r for r in report.rows if r[0] == "Python 许可证值级核对")
    assert row[1] == "1" and row[2] == "ok"


def test_no_local_metadata_skips_explicitly():
    """本地无该包 METADATA → 放行但显式登记 SKIP，不假装核对过。"""
    deps = [_py_dep("demo", license_="UNKNOWN", metadata_found=False)]
    notices = _notices_rows({"组件": "demo", "许可证": "Apache-2.0"})
    report = ctpn.audit_python(deps, notices)
    assert not _failures(report), _failures(report)
    notes = _notes(report)
    assert "SKIP" in notes and "本地无 dist-info" in notes


def test_metadata_present_but_valueless_skips():
    """METADATA 在但三级全空 → SKIP（与「本地无包」分开注明）。"""
    deps = [_py_dep("demo", license_="UNKNOWN", metadata_found=True)]
    notices = _notices_rows({"组件": "demo", "许可证": "Apache-2.0"})
    report = ctpn.audit_python(deps, notices)
    assert not _failures(report), _failures(report)
    notes = _notes(report)
    assert "三级均未提供许可证值" in notes


def test_unknown_cell_skips_value_check_keeps_impact_channel():
    """表内 UNKNOWN 不做值比对（不是事实错误）；影响说明通道仍生效。"""
    deps = [_py_dep("demo", license_="MIT", metadata_found=True)]
    notices = _notices_rows({"组件": "demo", "许可证": "UNKNOWN", "发行影响": ""})
    report = ctpn.audit_python(deps, notices)
    failures = _failures(report)
    # 不产生值不一致失败；但 UNKNOWN 缺发行影响必须红（既有通道）
    assert not any("许可证值不一致" in f for f in failures), failures
    assert any("UNKNOWN 缺发行影响" in f for f in failures), failures


def test_swapped_certifi_pluggy_regression():
    """本卡反例回归：certifi=MPL-2.0 / pluggy=MIT 的 METADATA 下，
    表内互换值（certifi=MIT、pluggy=MPL-2.0）必须两处都被抓出。"""
    deps = [
        _py_dep("certifi", version="2026.7.22", license_="MPL-2.0", category="构建"),
        _py_dep("pluggy", version="1.6.0", license_="MIT", category="测试"),
    ]
    notices = _notices_rows(
        {"组件": "certifi", "锁定版本": "2026.7.22", "许可证": "MIT", "类别": "构建"},
        {"组件": "pluggy", "锁定版本": "1.6.0", "许可证": "MPL-2.0", "类别": "测试"},
    )
    report = ctpn.audit_python(deps, notices)
    failures = _failures(report)
    assert any("certifi" in f and "MPL-2.0" in f for f in failures), failures
    assert any("pluggy" in f and "'MIT'" in f for f in failures), failures


def test_full_parse_pipeline_value_check(tmp_path, monkeypatch):
    """端到端：假 venv + 假 constraints + 假 notices 经 parse_* 后核对生效。"""
    _make_fake_venv(
        tmp_path,
        {
            "demo": "Name: demo\nLicense: MPL-2.0\n",
        },
        monkeypatch,
    )
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(
        "# 运行时与构建直接依赖\ndemo==1.0.0\n", encoding="utf-8"
    )
    notices_file = tmp_path / "THIRD_PARTY_NOTICES.md"
    notices_file.write_text(
        "# Third-Party Notices\n\n"
        f"## {PY_SECTION}\n\n"
        "| 组件 | 锁定版本 | 许可证 | 来源 | 类别 | 发行影响 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| demo | 1.0.0 | MIT | https://example.invalid/ | 构建 |  |\n",
        encoding="utf-8",
    )
    deps = ctpn.parse_constraints(constraints)
    assert deps and deps[0].metadata_found is True
    notices = ctpn.parse_notices_tables(notices_file)
    report = ctpn.audit_python(deps, notices)
    assert any("许可证值不一致" in f and "demo" in f for f in _failures(report))
