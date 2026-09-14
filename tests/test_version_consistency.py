"""ISS-037 单一版本源与 fail-closed 校验器的行为钉住。

反例（修复前现状，apps/desktop/experiments/iss029/findings.md §3 G5）：
`fathom/__init__.py` 0.1.0、`api.py` FastAPI(version="0.2.0")、Cargo 0.2.0、
Tauri 0.3.0 四处漂移且无校验拦截。

本测试钉住：
1. 真实仓库四处版本全部等于单一版本源，`scripts/check_version_consistency.sh`
   退出 0；
2. 在临时副本上制造任一处漂移（含 api.py 硬编码回潮、tauri 嵌套/预发行
   配置版本、pyproject 未来补充的 version 声明），校验器退出 1 且打印
   差异信息——真实文件不被修改；
3. 权威源读不出或文件缺失（结构性失败）时退出 2；
4. 运行时 FastAPI 实例的 version 与 `fathom.__version__` 同源相等。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import fathom
import fathom.api

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_version_consistency.sh"

# 校验器读取的最小文件集（与脚本内路径一致）。
CHECKED_FILES = [
    "fathom/__init__.py",
    "fathom/api.py",
    "apps/desktop/src-tauri/tauri.conf.json",
    "apps/desktop/src-tauri/Cargo.toml",
    "pyproject.toml",
]


def _make_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in CHECKED_FILES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    return root


def _run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["FATHOM_VERSION_CHECK_ROOT"] = str(root)
    return subprocess.run(
        ["bash", str(CHECKER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"反例前置失败：{path} 中找不到待替换片段 {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


# ---- 真实仓库：一致 → 退出 0 ----


def test_real_repo_consistent() -> None:
    result = _run_checker(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "version consistency: ok" in result.stdout
    # 单一版本源本身被表格引用。
    assert fathom.__version__ in result.stdout


# ---- 临时副本：各来源漂移 → 退出 1 ----


def test_tauri_conf_drift_detected(tmp_path: Path) -> None:
    root = _make_copy(tmp_path)
    _rewrite(root / "apps/desktop/src-tauri/tauri.conf.json", '"version": "0.3.0"', '"version": "9.9.9"')
    result = _run_checker(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "9.9.9" in result.stdout


def test_cargo_toml_drift_detected(tmp_path: Path) -> None:
    root = _make_copy(tmp_path)
    _rewrite(root / "apps/desktop/src-tauri/Cargo.toml", 'version = "0.3.0"', 'version = "0.2.0"')
    result = _run_checker(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "0.2.0" in result.stdout


def test_single_source_bump_without_sync_detected(tmp_path: Path) -> None:
    # 只升单一源、不同步消费方：权威源变 0.4.0，其余三处立即被比对为漂移。
    root = _make_copy(tmp_path)
    _rewrite(root / "fathom/__init__.py", '__version__ = "0.3.0"', '__version__ = "0.4.0"')
    result = _run_checker(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "0.4.0" in result.stdout


def test_api_hardcoded_version_reintroduced(tmp_path: Path) -> None:
    root = _make_copy(tmp_path)
    _rewrite(
        root / "fathom/api.py",
        "app = FastAPI(title=\"Fathom\", version=__version__)",
        'app = FastAPI(title="Fathom", version="0.3.0")',
    )
    result = _run_checker(root)
    # 即使字面量恰好等于当前版本，也必须红：单一源不允许被旁路。
    assert result.returncode == 1, result.stdout + result.stderr
    assert "硬编码" in result.stdout


def test_api_hardcoded_stale_version_reintroduced(tmp_path: Path) -> None:
    root = _make_copy(tmp_path)
    _rewrite(
        root / "fathom/api.py",
        "app = FastAPI(title=\"Fathom\", version=__version__)",
        'app = FastAPI(title="Fathom", version="0.2.0")',
    )
    result = _run_checker(root)
    assert result.returncode == 1, result.stdout + result.stderr


def test_tauri_nested_prerelease_version_drift(tmp_path: Path) -> None:
    # 预发行配置（bundle 等）未来若新增 version 键，同样纳入比对。
    root = _make_copy(tmp_path)
    _rewrite(
        root / "apps/desktop/src-tauri/tauri.conf.json",
        '"bundle": {\n    "active": false\n  }',
        '"bundle": {\n    "active": false,\n    "version": "0.2.0"\n  }',
    )
    result = _run_checker(root)
    assert result.returncode == 1, result.stdout + result.stderr


def test_pyproject_declared_version_must_match(tmp_path: Path) -> None:
    # 当前 pyproject.toml 无 version 字段（fathom 非安装包）；若未来补充声明，
    # 必须与单一版本源一致，否则必红。
    root = _make_copy(tmp_path)
    pyproject = root / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.2.0"\n' + pyproject.read_text(encoding="utf-8"), encoding="utf-8")
    result = _run_checker(root)
    assert result.returncode == 1, result.stdout + result.stderr
    # 反向对照：与单一源一致的声明保持绿。
    _rewrite(pyproject, 'version = "0.2.0"', 'version = "0.3.0"')
    result = _run_checker(root)
    assert result.returncode == 0, result.stdout + result.stderr


# ---- 临时副本：结构性失败 → 退出 2 ----


def test_missing_authoritative_version_fails_closed(tmp_path: Path) -> None:
    root = _make_copy(tmp_path)
    init = root / "fathom/__init__.py"
    init.write_text(
        re.sub(r'^__version__.*$', "", init.read_text(encoding="utf-8"), flags=re.M),
        encoding="utf-8",
    )
    result = _run_checker(root)
    assert result.returncode == 2, result.stdout + result.stderr


def test_missing_checked_file_fails_closed(tmp_path: Path) -> None:
    root = _make_copy(tmp_path)
    (root / "apps/desktop/src-tauri/tauri.conf.json").unlink()
    result = _run_checker(root)
    assert result.returncode == 2, result.stdout + result.stderr


# ---- 运行时同源：API 实例版本来自单一版本源 ----


def test_fastapi_app_version_reads_single_source() -> None:
    assert fathom.api.app.version == fathom.__version__


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", fathom.__version__), fathom.__version__
