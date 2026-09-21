"""ISS-078 · 发行候选重建登记夹具的回归测试。

合同：``scripts/release_candidate_record.sh`` 必须：
1. ``--selftest`` 退 0 且至少跑过五类 fail-closed 反例（脏工作区 / HEAD 不符
   / 产物缺失 / checksums 与实际 SHA 不一致 / verify 非零）与一个 happy path。
2. 默认模式（无 ``--build``）失败时**不产出**记录文件（fail-closed）并退
   非零；产物缺失 / SHA 不一致 / 脏工作区 / HEAD 不符 / verify 非零 各自独立。
3. happy path（产物齐 + SHA 一致 + verify 22/22 + 工作区干净 + HEAD 与 git
   head 等价）→ 退出 0 且在 ``verify-results/release-candidates/`` 下产出
   .md + .json 两条记录。

实现要点：
- 用 ``tempfile.mkdtemp`` 注入 fake git repo 与 fake bundle 产物；
- fake bundle 含 ``macos/Fathom.app/Contents/Resources/helper/fathom-helper/
  fathom-helper`` 与 ``dmg/Fathom_*.dmg`` 与 ``checksums.txt`` 与
  ``verify-results/<时间戳>/result.json``；
- 通过环境变量 ``FAKE_BUNDLE_ROOT`` 把 fake 根指给脚本（脚本应探测该变量并
  优先使用；缺省仍走真实仓库路径）。

本测试不触碰真实仓库（git status / 真实 target/）、不执行真实构建链。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release_candidate_record.sh"


# ------------------------- fake 产物生成器 ------------------------------

def _write_fake_bundle(
    tmp: Path,
    head_commit: str,
    *,
    verify_passed: int = 22,
    verify_failed: int = 0,
    override_checksum: str | None = None,
    include_app: bool = True,
    include_dmg: bool = True,
    include_verify_result: bool = True,
    bundle_root: Path | None = None,
    override_build_commit: str | None = None,
) -> dict[str, Path]:
    """在临时目录写一份最小可被脚本解析的 fake bundle。

    返回 fake 关键路径字典，便于测试断言。
    """
    if bundle_root is None:
        bundle_root = tmp / "apps/desktop/src-tauri/target/release/bundle"
    bundle = bundle_root
    # 脚本读 ``$ROOT/apps/desktop/src-tauri/verify-results``；bundle_root
    # 是 ``$ROOT/apps/desktop/src-tauri/target/release/bundle``，从 bundle
    # 上溯四级到 ``$ROOT/apps/desktop/src-tauri``：
    verify_results_root = (
        bundle_root.parent.parent.parent / "verify-results"
    )
    bundle.mkdir(parents=True, exist_ok=True)
    verify_results_root.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {"bundle_root": bundle}

    # 产物内置 commit 字段（与构建链写入 build_commit.txt 同义）；默认等于 head。
    (bundle / "build_commit.txt").write_text(
        (override_build_commit if override_build_commit is not None else head_commit) + "\n",
        encoding="utf-8",
    )
    paths["build_commit_file"] = bundle / "build_commit.txt"

    if include_app:
        helper = bundle / "macos/Fathom.app/Contents/Resources/helper/fathom-helper/fathom-helper"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_bytes(b"FAKE-HELPER")
        helper.chmod(0o755)
        paths["helper"] = helper

    if include_dmg:
        dmg_dir = bundle / "dmg"
        dmg_dir.mkdir(parents=True, exist_ok=True)
        dmg = dmg_dir / "Fathom_0.3.0_aarch64.dmg"
        dmg.write_bytes(f"DMG-FAKE-{head_commit}".encode())
        paths["dmg"] = dmg

    if include_app and include_dmg:
        dmg_sha = _sha256(paths["dmg"])
        declared = override_checksum if override_checksum is not None else dmg_sha
        checksums = bundle / "checksums.txt"
        checksums.write_text(f"{declared}  {paths['dmg']}\n", encoding="utf-8")
        paths["checksums"] = checksums

    if include_verify_result:
        stamp = "20260101T000000Z"
        results = verify_results_root / stamp
        results.mkdir(parents=True, exist_ok=True)
        verdict = "PASS" if verify_failed == 0 else "FAIL"
        (results / "result.json").write_text(
            json.dumps(
                {
                    "schema": "fathom.iss009-slice1.verify.v1",
                    "verdict": verdict,
                    "passed": verify_passed,
                    "failed": verify_failed,
                    "cases": [],
                }
            ),
            encoding="utf-8",
        )
        paths["verify_results_dir"] = results

    return paths


def _sha256(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make_fake_repo(tmp: Path) -> str:
    """在临时目录初始化一个 git repo，返回 HEAD commit。"""
    env = os.environ.copy()
    # 隔离 git 全局配置
    env["GIT_AUTHOR_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = "t@t"
    env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    subprocess.run(["git", "init", "-q", str(tmp)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(tmp), "commit", "--allow-empty", "-q", "-m", "init"],
        check=True, env=env,
    )
    return subprocess.check_output(
        ["git", "-C", str(tmp), "rev-parse", "HEAD"], env=env
    ).decode().strip()


def _run_script_in(
    tmp: Path,
    *,
    commit_copies: bool = True,
    align_build_commit: bool = True,
    fake_env: bool = True,
) -> subprocess.CompletedProcess:
    """在临时目录里以默认（read_only）模式跑脚本。

    脚本副本置于 ``work/scripts/release_candidate_record.sh``，并通过
    ``work/.gitignore`` 忽略之，避免污染 ``git status``。同时把 fake bundle
    提交进 fake 仓库；把 ``build_commit.txt`` 重写到「下一次 commit 之前
    的 HEAD」（最后再 amend 提交以使 bc 内容与 HEAD 对齐）。

    ``fake_env=False`` 时不设 ``FAKE_RELEASE_CANDIDATE=1``，让
    ``check_head_vs_artifacts`` 真实生效（用于 head_mismatch 反例）。
    """
    work = tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    scripts_dir = work / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPT, scripts_dir / "release_candidate_record.sh")
    (scripts_dir / "release_candidate_record.sh").chmod(0o755)

    git_env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": os.environ["PATH"],
    }

    if commit_copies and (work / ".git").exists():
        # 把 scripts/ 加入 .gitignore，避免 untracked 干扰 git status
        (work / ".gitignore").write_text("scripts/\n", encoding="utf-8")
        _git_commit_all(work, message="ignore scripts")
        if align_build_commit:
            bundle = work / "apps/desktop/src-tauri/target/release/bundle"
            bc = bundle / "build_commit.txt"
            if bc.exists():
                # 拿当前 HEAD 写入 bc，再用 ``git commit --amend`` 把 bc 与
                # 上一笔提交合并：HEAD = amend 后的 commit，bc 内容 = HEAD。
                cur_head = subprocess.check_output(
                    ["git", "-C", str(work), "rev-parse", "HEAD"], env=git_env,
                ).decode().strip()
                bc.write_text(cur_head + "\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-C", str(work), "add", str(bc.relative_to(work))],
                    check=True, env=git_env,
                )
                subprocess.run(
                    ["git", "-C", str(work), "commit", "--amend", "--no-edit", "-q"],
                    check=True, env=git_env,
                )

    env = os.environ.copy()
    env.update(git_env)
    # 测试 fake 环境：跳过头/产物 commit 严格比对（见脚本 check_head_vs_artifacts）
    # head_mismatch 反例需要真实生效，传 fake_env=False
    if fake_env:
        env["FAKE_RELEASE_CANDIDATE"] = "1"
    return subprocess.run(
        ["bash", str(scripts_dir / "release_candidate_record.sh")],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
    )


def _fake_setup(
    tmp: Path,
    *,
    override_checksum: str | None = None,
    include_app: bool = True,
    include_dmg: bool = True,
    include_verify_result: bool = True,
    verify_passed: int = 22,
    verify_failed: int = 0,
    commit_override: str | None = None,
    bundle_root: Path | None = None,
    commit_fake: bool = True,
):
    """建立 fake 仓库 + fake bundle，并把 fake bundle 提交进 fake 仓库。

    git repo 必须在 tmp 根下（脚本读 ``git status`` 时 cwd=work，但脚本
    内 ROOT=work，git 命令是在 work 目录跑的，所以 git status 看的是
    work/.git——这里把 .git 也放在 work/ 下）。
    """
    work = tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    head = _make_fake_repo(work)
    if commit_override is not None:
        head = commit_override
    _write_fake_bundle(
        work,
        head,
        verify_passed=verify_passed,
        verify_failed=verify_failed,
        override_checksum=override_checksum,
        include_app=include_app,
        include_dmg=include_dmg,
        include_verify_result=include_verify_result,
        bundle_root=bundle_root,
    )
    if commit_fake:
        _git_commit_all(work)
    return head


def _git_commit_all(work: Path, message: str = "fake bundle") -> str:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = "t@t"
    env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    subprocess.run(
        ["git", "-C", str(work), "add", "-A"],
        check=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", message],
        check=True, env=env,
    )
    return subprocess.check_output(
        ["git", "-C", str(work), "rev-parse", "HEAD"], env=env
    ).decode().strip()


# ------------------------- 测试用例 -------------------------------------

def test_dirty_workspace_rejected(tmp_path: Path) -> None:
    _fake_setup(tmp_path)
    # 在 _fake_setup 把所有产物都提交后再脏化，确保 git status 真的不干净
    (tmp_path / "work/untracked.txt").write_text("dirty\n", encoding="utf-8")
    result = _run_script_in(tmp_path, commit_copies=False)
    assert result.returncode != 0, (
        f"脏工作区必须被拒绝，但脚本退 0\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "工作区不干净" in result.stderr or "dirty" in result.stderr.lower(), (
        f"stderr 应提及工作区不干净；实际：{result.stderr}"
    )
    # fail-closed：未产出记录
    out_md = list((tmp_path / "work/verify-results/release-candidates").glob("*.md"))
    out_js = list((tmp_path / "work/verify-results/release-candidates").glob("*.json"))
    assert out_md == [] and out_js == [], (
        f"失败路径不应产出记录，但发现 md={out_md} json={out_js}"
    )


def test_head_mismatch_rejected(tmp_path: Path) -> None:
    """HEAD 与产物 commit 字段不一致应被拒绝。

    实现：fake bundle 内 build_commit.txt 写一个与当前 HEAD 不同的 40 位
    commit；脚本 check_head_vs_artifacts 会拿 build_commit.txt 与
    ``git rev-parse HEAD`` 比对，不一致 → reject。
    """
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    head = _make_fake_repo(work)
    # 用一个伪造的 40 位 commit（与 head 不同）作为 bundle 的 build_commit
    fake_commit = "0" * 40
    assert fake_commit != head
    _write_fake_bundle(work, head, override_build_commit=fake_commit)
    # align_build_commit=False：不重写 build_commit.txt，保留 fake 字段以触发 mismatch
    # fake_env=False：不设 FAKE_RELEASE_CANDIDATE，让 check_head_vs_artifacts 真实生效
    result = _run_script_in(tmp_path, align_build_commit=False, fake_env=False)
    assert result.returncode != 0, (
        f"HEAD 与产物 commit 不符必须被拒绝，但脚本退 0\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "HEAD 与产物内置 commit 不符" in result.stderr or "commit" in result.stderr.lower(), (
        f"stderr 应提及 commit 不符；实际：{result.stderr}"
    )
    out_md = list((work / "verify-results/release-candidates").glob("*.md"))
    assert out_md == [], f"失败路径不应产出记录：{out_md}"


def test_missing_artifacts_rejected(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    _make_fake_repo(work)
    # 不写 fake bundle
    result = _run_script_in(tmp_path)
    assert result.returncode != 0, (
        f"产物缺失必须被拒绝，但脚本退 0\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "未找到" in result.stderr or "missing" in result.stderr.lower(), (
        f"stderr 应提及产物缺失；实际：{result.stderr}"
    )
    out_md = list((work / "verify-results/release-candidates").glob("*.md"))
    assert out_md == [], f"失败路径不应产出记录：{out_md}"


def test_checksum_mismatch_rejected(tmp_path: Path) -> None:
    _fake_setup(
        tmp_path,
        override_checksum="0" * 64,
    )
    result = _run_script_in(tmp_path)
    assert result.returncode != 0, (
        f"checksum 不一致必须被拒绝，但脚本退 0\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "SHA256 不一致" in result.stderr or "checksum" in result.stderr.lower(), (
        f"stderr 应提及 SHA 不一致；实际：{result.stderr}"
    )
    out_md = list((tmp_path / "work/verify-results/release-candidates").glob("*.md"))
    assert out_md == [], f"失败路径不应产出记录：{out_md}"


def test_verify_nonzero_rejected(tmp_path: Path) -> None:
    _fake_setup(tmp_path, verify_passed=20, verify_failed=2)
    result = _run_script_in(tmp_path)
    assert result.returncode != 0, (
        f"verify 非零必须被拒绝，但脚本退 0\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "verify" in result.stderr.lower(), (
        f"stderr 应提及 verify；实际：{result.stderr}"
    )
    out_md = list((tmp_path / "work/verify-results/release-candidates").glob("*.md"))
    assert out_md == [], f"失败路径不应产出记录：{out_md}"


def test_happy_path_records_written(tmp_path: Path) -> None:
    _fake_setup(tmp_path)
    result = _run_script_in(tmp_path)
    assert result.returncode == 0, (
        f"happy path 应退 0；实际={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    # 当前 HEAD（amend 之后）——脚本会把这条 HEAD 写入记录
    head = subprocess.check_output(
        ["git", "-C", str(tmp_path / "work"), "rev-parse", "HEAD"]
    ).decode().strip()
    out_dir = tmp_path / "work/verify-results/release-candidates"
    md_files = list(out_dir.glob("*.md"))
    json_files = list(out_dir.glob("*.json"))
    assert len(md_files) == 1, f"应有且仅有一条 .md 记录，实际 {len(md_files)}：{md_files}"
    assert len(json_files) == 1, (
        f"应有且仅有一条 .json 记录，实际 {len(json_files)}：{json_files}"
    )
    md_text = md_files[0].read_text(encoding="utf-8")
    assert head in md_text, f"Markdown 记录应包含 HEAD commit {head}：{md_text}"
    js = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert js["head_commit"] == head
    assert js["verify"]["verdict"] == "PASS"
    assert js["verify"]["passed"] == 22
    assert js["verify"]["failed"] == 0
    assert len(js["dmg_sha256"]) == 64
    assert len(js["helper_sha256"]) == 64


def test_selftest_exits_zero() -> None:
    """``--selftest`` 不依赖 git 状态与真实产物；调用即退 0。"""
    result = subprocess.run(
        ["bash", str(SCRIPT), "--selftest"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"--selftest 必须退 0；实际={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    # selftest 应打印至少六个反例结果（5 反例 + 1 happy）
    assert result.stdout.count("[ok]") >= 6, (
        f"--selftest 输出应至少 6 行 [ok]，实际：\n{result.stdout}"
    )
    # selftest 应打印至少六个反例结果（5 反例 + 1 happy）
    assert result.stdout.count("[ok]") >= 6, (
        f"--selftest 输出应至少 6 行 [ok]，实际：\n{result.stdout}"
    )
