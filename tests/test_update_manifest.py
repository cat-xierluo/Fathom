"""ISS-040A：latest.json 双架构生成与 fail-closed 校验工具。

背景：ISS-040 的应用内更新需要一份 Tauri updater 兼容的静态清单
`latest.json`，且 v0.3 分别发布 `darwin-aarch64` 与 `darwin-x86_64`
（docs/plans/2026-09-13-v0.3-release-design.md §4）。清单缺平台、版本与
产物不一致、`.sig` 缺失/为空时若静默生成，用户会拿到无法校验或错误的更新。

本测试钉住 `scripts/generate_update_manifest.py` / `scripts/verify_update_manifest.py`
的行为（不引 crate、不动 Tauri 配置、不做真实签名；`.sig` 由调用方提供）：

1. 双架构齐全 → 生成清单含 `darwin-aarch64`/`darwin-x86_64`，schema 字段
   与 Tauri v2 一致（顶层 version/notes/pub_date/platforms；平台项 url/signature），
   且 `signature` 等于对应 `.sig` 文件内容（verify 规则可复读同一清单）；
2. 运行时也接受单一平台（卡片允许"单架构运行时"，仅"生成"必须双平台）；
3. fail-closed（exit 非 0 + 中文原因 + 不落盘/不覆盖既有清单）：
   - 缺任一 `darwin-aarch64`/`darwin-x86_64`；
   - 版本号非法（空、非 semver）；
   - 产物缺失、空文件；
   - `.sig` 缺失、为空、只含空白；
   - URL 与产物文件名不一致（version 埋点时产物名不含版本）；
   - 同一平台重复传入；
   - 平台为未知值；
4. 生成器与校验器同规则：生成后 `verify_update_manifest.py` 退出 0；
   对生成物逐项破坏（删平台、改 signature、空 signature、坏 pub_date、
   版本与产物不一致、重复 key）后校验器退出非 0；
5. 真实仓库契约：两脚本存在并可被 `--help` 调用；`fathom.__version__`
   可作为权威版本源传给工具并原样落入清单。

测试不写仓库文件：所有产物落在 tmp_path。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import fathom

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "generate_update_manifest.py"
VERIFIER = REPO_ROOT / "scripts" / "verify_update_manifest.py"

DARWIN_ARM64 = "darwin-aarch64"
DARWIN_X86_64 = "darwin-x86_64"


# ---- 夹具：合成两个架构的“产物 + .sig” ----


def _make_artifacts(tmp_path: Path, version: str = "0.3.0") -> tuple[Path, Path, Path, Path]:
    """构造 arm64/x86_64 两个 updater tar.gz 与各自 .sig，返回四个路径。"""
    rel = f"Fathom_{version}_aarch64.app.tar.gz"
    rel_x = f"Fathom_{version}_x86_64.app.tar.gz"
    arm = tmp_path / rel
    x86 = tmp_path / rel_x
    arm.write_bytes(b"arm64-payload")
    x86.write_bytes(b"x86_64-payload")
    arm_sig = arm.with_suffix(arm.suffix + ".sig")
    x86_sig = x86.with_suffix(x86.suffix + ".sig")
    arm_sig.write_text("dW50cnVzdGVkIGNvbW1lbnQ6IGFybTY0LXNpZw==\n", encoding="utf-8")
    x86_sig.write_text("dW50cnVzdGVkIGNvbW1lbnQ6IHg4Ni02NC1zaWc=\n", encoding="utf-8")
    return arm, x86, arm_sig, x86_sig


def _run_generate(
    *,
    version: str,
    arm: Path | None,
    x86: Path | None,
    arm_sig: Path | None,
    x86_sig: Path | None,
    out: Path,
    notes: str | None = None,
    pub_date: str | None = None,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(GENERATOR), "--version", version, "--out", str(out)]
    if arm is not None:
        args += ["--darwin-aarch64", str(arm)]
    if x86 is not None:
        args += ["--darwin-x86_64", str(x86)]
    if arm_sig is not None:
        args += ["--darwin-aarch64-sig", str(arm_sig)]
    if x86_sig is not None:
        args += ["--darwin-x86_64-sig", str(x86_sig)]
    if notes is not None:
        args += ["--notes", notes]
    if pub_date is not None:
        args += ["--pub-date", pub_date]
    if extra:
        args += extra
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def _run_verify(manifest: Path, *, root: Path | None = None, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(VERIFIER), "--manifest", str(manifest)]
    if root is not None:
        args += ["--artifacts-root", str(root)]
    if extra:
        args += extra
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


# ---- 1. 双架构齐全：生成成功且 schema 正确 ----


def test_generate_both_architectures_succeeds(tmp_path: Path) -> None:
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    result = _run_generate(
        version="0.3.0", arm=arm, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig, out=out
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.exists(), "生成失败：未写出 latest.json"

    manifest = json.loads(out.read_text(encoding="utf-8"))
    # 顶层字段与 Tauri v2 RemoteRelease 一致。
    assert set(manifest) >= {"version", "notes", "pub_date", "platforms"}
    assert manifest["version"] == "0.3.0"
    assert set(manifest["platforms"]) == {DARWIN_ARM64, DARWIN_X86_64}

    arm_entry = manifest["platforms"][DARWIN_ARM64]
    x86_entry = manifest["platforms"][DARWIN_X86_64]
    assert set(arm_entry) == {"url", "signature"}
    assert set(x86_entry) == {"url", "signature"}
    # signature 必须是 .sig 文件内容（Tauri 会读它做 minisign 校验）。
    assert arm_entry["signature"] == arm_sig.read_text(encoding="utf-8").strip()
    assert x86_entry["signature"] == x86_sig.read_text(encoding="utf-8").strip()
    # url 指向对应产物文件名。
    assert arm_entry["url"].endswith(arm.name)
    assert x86_entry["url"].endswith(x86.name)


def test_generate_url_uses_artifact_filename_and_base(tmp_path: Path) -> None:
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    result = _run_generate(
        version="0.3.0",
        arm=arm,
        x86=x86,
        arm_sig=arm_sig,
        x86_sig=x86_sig,
        out=out,
        extra=["--base-url", "https://example.invalid/fathom/v0.3.0/"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["platforms"][DARWIN_ARM64]["url"] == (
        "https://example.invalid/fathom/v0.3.0/" + arm.name
    )
    assert manifest["platforms"][DARWIN_X86_64]["url"] == (
        "https://example.invalid/fathom/v0.3.0/" + x86.name
    )


def test_generate_pub_date_is_rfc3339(tmp_path: Path) -> None:
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    result = _run_generate(
        version="0.3.0",
        arm=arm,
        x86=x86,
        arm_sig=arm_sig,
        x86_sig=x86_sig,
        out=out,
        pub_date="2026-09-17T09:00:00Z",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["pub_date"] == "2026-09-17T09:00:00Z"


# ---- 2. 运行时允许单平台，但生成器必须双平台 ----


def test_verifier_accepts_single_platform_manifest(tmp_path: Path) -> None:
    """卡片："生成必须双架构，运行时允许单架构"。校验器默认不强制双平台。"""
    arm, _x86, arm_sig, _x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    gen = _run_generate(
        version="0.3.0", arm=arm, x86=None, arm_sig=arm_sig, x86_sig=None, out=out,
        extra=["--allow-single-platform"],
    )
    assert gen.returncode == 0, gen.stdout + gen.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert set(manifest["platforms"]) == {DARWIN_ARM64}
    verify = _run_verify(out)
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_generator_requires_both_platforms(tmp_path: Path) -> None:
    arm, _x86, arm_sig, _x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    result = _run_generate(
        version="0.3.0", arm=arm, x86=None, arm_sig=arm_sig, x86_sig=None, out=out,
        extra=["--require-both-platforms"],
    )
    assert result.returncode != 0, "缺失 x86_64 平台时生成器必须 fail-closed"
    assert "x86_64" in result.stderr, result.stderr
    assert not out.exists(), "拒绝生成时不得留下 latest.json"


# ---- 3. fail-closed：生成器 ----


def test_generate_rejects_missing_platform(tmp_path: Path) -> None:
    arm, _x86, arm_sig, _x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    # 发行口径：生成默认必须双架构，缺 x86_64 直接拒绝且不落盘。
    result = _run_generate(
        version="0.3.0", arm=arm, x86=None, arm_sig=arm_sig, x86_sig=None, out=out
    )
    assert result.returncode != 0
    assert DARWIN_X86_64 in result.stderr
    assert not out.exists()

    bad = _run_generate(
        version="0.3.0", arm=arm, x86=None, arm_sig=arm_sig, x86_sig=None,
        out=tmp_path / "bad.json",
        extra=["--darwin-aarch64", str(arm)],  # 重复平台
    )
    assert bad.returncode != 0
    assert "重复" in bad.stderr or "duplicate" in bad.stderr.lower()
    assert not (tmp_path / "bad.json").exists()


def test_generate_rejects_invalid_version(tmp_path: Path) -> None:
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)
    for bad_version in ("", "1.2", "v0.3.0", "0.3.0.1", "abc"):
        out = tmp_path / f"bad-{bad_version or 'empty'}.json"
        result = _run_generate(
            version=bad_version, arm=arm, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig, out=out,
        )
        assert result.returncode != 0, f"版本 {bad_version!r} 应被拒绝"
        assert "版本" in result.stderr, result.stderr
        assert not out.exists()


def test_generate_rejects_missing_or_empty_artifact(tmp_path: Path) -> None:
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)

    # 产物不存在
    missing = tmp_path / "nope.tar.gz"
    result = _run_generate(
        version="0.3.0", arm=missing, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig,
        out=tmp_path / "a.json",
    )
    assert result.returncode != 0
    assert not (tmp_path / "a.json").exists()

    # 产物为空文件
    empty = tmp_path / "Fathom_0.3.0_aarch64.app.tar.gz"
    empty.write_bytes(b"")
    result = _run_generate(
        version="0.3.0", arm=empty, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig,
        out=tmp_path / "b.json",
    )
    assert result.returncode != 0
    assert "空" in result.stderr, result.stderr
    assert not (tmp_path / "b.json").exists()


def test_generate_rejects_missing_or_empty_signature(tmp_path: Path) -> None:
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)

    # .sig 缺失
    result = _run_generate(
        version="0.3.0", arm=arm, x86=x86, arm_sig=tmp_path / "no.sig", x86_sig=x86_sig,
        out=tmp_path / "a.json",
    )
    assert result.returncode != 0
    assert not (tmp_path / "a.json").exists()

    # .sig 只含空白
    blank = tmp_path / "blank.sig"
    blank.write_text("   \n\t\n", encoding="utf-8")
    result = _run_generate(
        version="0.3.0", arm=arm, x86=x86, arm_sig=blank, x86_sig=x86_sig,
        out=tmp_path / "b.json",
    )
    assert result.returncode != 0
    assert "签名" in result.stderr, result.stderr
    assert not (tmp_path / "b.json").exists()


def test_generate_rejects_version_artifact_mismatch(tmp_path: Path) -> None:
    """.sig 已由调用方提供，但产物文件名埋点与 version 不一致时必须拒绝。"""
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path, version="0.3.1")
    out = tmp_path / "latest.json"
    result = _run_generate(
        version="0.3.0",  # 与产物名中的 0.3.1 不一致
        arm=arm, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig, out=out,
    )
    assert result.returncode != 0
    assert "版本" in result.stderr, result.stderr
    assert not out.exists()


def test_generate_does_not_overwrite_existing_manifest_on_failure(tmp_path: Path) -> None:
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    out.write_text('{"sentinel": true}\n', encoding="utf-8")
    result = _run_generate(
        version="0.3.0", arm=arm, x86=x86, arm_sig=tmp_path / "no.sig", x86_sig=x86_sig,
        out=out,
    )
    assert result.returncode != 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"sentinel": True}


def test_generate_rejects_unknown_platform(tmp_path: Path) -> None:
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    result = _run_generate(
        version="0.3.0", arm=arm, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig, out=out,
        extra=["--platform", "darwin-arm64"],
    )
    assert result.returncode != 0
    assert not out.exists()


# ---- 4. 生成 → 校验 round-trip，以及逐项破坏 ----


def test_generate_then_verify_round_trip(tmp_path: Path) -> None:
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    gen = _run_generate(
        version="0.3.0", arm=arm, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig, out=out
    )
    assert gen.returncode == 0, gen.stdout + gen.stderr
    verify = _run_verify(out)
    assert verify.returncode == 0, verify.stdout + verify.stderr


def _tamper(tmp_path: Path, mutate) -> Path:
    """生成一份有效清单，按 mutate 改写后写回新文件。"""
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    gen = _run_generate(
        version="0.3.0", arm=arm, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig, out=out
    )
    assert gen.returncode == 0, gen.stdout + gen.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    mutate(data)
    bad = tmp_path / "tampered.json"
    bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return bad


def test_verifier_rejects_missing_platform_entry(tmp_path: Path) -> None:
    bad = _tamper(tmp_path, lambda d: d["platforms"].pop(DARWIN_X86_64))
    # 缺平台在宽松默认下可行（运行时单架构），发行门禁收紧后必须拒绝。
    lenient = _run_verify(bad)
    assert lenient.returncode == 0, lenient.stdout + lenient.stderr
    result = _run_verify(bad, extra=["--require-both-platforms"])
    assert result.returncode != 0
    assert DARWIN_X86_64 in result.stderr


def test_verifier_rejects_blank_signature(tmp_path: Path) -> None:
    bad = _tamper(tmp_path, lambda d: d["platforms"][DARWIN_ARM64].__setitem__("signature", "  "))
    result = _run_verify(bad)
    assert result.returncode != 0
    assert "签名" in result.stderr


def test_verifier_rejects_bad_pub_date(tmp_path: Path) -> None:
    bad = _tamper(tmp_path, lambda d: d.__setitem__("pub_date", "2026-09-17 09:00:00"))
    result = _run_verify(bad)
    assert result.returncode != 0


def test_verifier_rejects_missing_url(tmp_path: Path) -> None:
    bad = _tamper(tmp_path, lambda d: d["platforms"][DARWIN_X86_64].pop("url"))
    result = _run_verify(bad)
    assert result.returncode != 0


def test_verifier_rejects_non_object(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    result = _run_verify(bad)
    assert result.returncode != 0


def test_verifier_rejects_absent_file(tmp_path: Path) -> None:
    result = _run_verify(tmp_path / "does-not-exist.json")
    assert result.returncode != 0
    assert "不存在" in result.stderr or "not found" in result.stderr.lower()


def test_verifier_can_require_both_platforms(tmp_path: Path) -> None:
    arm, _x86, arm_sig, _x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    gen = _run_generate(
        version="0.3.0", arm=arm, x86=None, arm_sig=arm_sig, x86_sig=None, out=out,
        extra=["--allow-single-platform"],
    )
    assert gen.returncode == 0, gen.stdout + gen.stderr
    # 生成器默认即双架构；单平台需显式 --allow-single-platform。
    # 校验器默认放开单平台（运行时清单），--require-both-platforms 收紧后拒绝。
    ok = _run_verify(out, extra=["--allow-single-platform"])
    assert ok.returncode == 0, ok.stdout + ok.stderr
    strict = _run_verify(out, extra=["--require-both-platforms"])
    assert strict.returncode != 0
    assert DARWIN_X86_64 in strict.stderr


def test_verifier_checks_signature_against_sig_file(tmp_path: Path) -> None:
    """--artifacts-root 下应能按 url 基名回读 .sig 做一致性比对。"""
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path)
    out = tmp_path / "latest.json"
    gen = _run_generate(
        version="0.3.0", arm=arm, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig, out=out
    )
    assert gen.returncode == 0, gen.stdout + gen.stderr
    verify = _run_verify(out, root=tmp_path)
    assert verify.returncode == 0, verify.stdout + verify.stderr


# ---- 5. 真实仓库契约 ----


def test_repo_version_source_matches_manifest_contract(tmp_path: Path) -> None:
    """权威版本源 fathom.__version__ 可导入且为 semver，能直接传给生成器。"""
    arm, x86, arm_sig, x86_sig = _make_artifacts(tmp_path, version=fathom.__version__)
    out = tmp_path / "latest.json"
    result = _run_generate(
        version=fathom.__version__,
        arm=arm, x86=x86, arm_sig=arm_sig, x86_sig=x86_sig, out=out,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == fathom.__version__


def test_scripts_are_executable_entrypoints() -> None:
    assert GENERATOR.is_file(), f"缺少生成器 {GENERATOR}"
    assert VERIFIER.is_file(), f"缺少校验器 {VERIFIER}"
    for script in (GENERATOR, VERIFIER):
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"{script.name} --help 失败：{result.stderr}"


def test_scripts_do_not_import_third_party() -> None:
    """工具只依赖标准库，CI 锁定依赖闭包后仍可运行。"""
    stdlib_ok = {
        "argparse", "hashlib", "json", "os", "pathlib", "re", "sys",
        "__future__", "datetime", "tempfile", "urllib", "typing",
        # 校验器刻意 import 同目录生成器以共用校验原语（非第三方依赖）。
        "generate_update_manifest",
    }
    for script in (GENERATOR, VERIFIER):
        text = script.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                module = stripped.split()[1].split(".")[0]
                assert module in stdlib_ok, f"{script.name} 引入了非标准库模块 {module}"
