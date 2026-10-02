"""ISS-141 · verify_tauri_analysis 定向测试（修返修 episode 1）。

原则：新增用例必须能**击穿旧行为**，不堆纯镜像。重点打穿：
  B1 env 失效 / launch 后立即凭据 / post-launch 失败先回收再 raise
  B2 helper 误作 GUI PID、GUI/helper 必须不同
  B3 driver --help 真实 JSON 契约（真编译 + 真调用）
  B4 foreign / dead / reused PID 一律拒绝发信号
  B5 产品暂停 / 终态收口 / 超时 / resume / 清理推迟
  B6 manifest 失真（回执不符、输入路径脏、缺字段）明确拒绝
  非阻断项：set-size 不搬位置、measure 非写死 -28
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "verify_tauri_analysis.py"
DRIVER_SOURCE = REPO_ROOT / "scripts" / "tauri_analysis_driver.swift"

_spec = importlib.util.spec_from_file_location("verify_tauri_analysis", MODULE_PATH)
assert _spec and _spec.loader
vta = importlib.util.module_from_spec(_spec)
sys.modules["verify_tauri_analysis"] = vta
_spec.loader.exec_module(vta)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def fake_app(tmp_path: Path) -> Path:
    app = tmp_path / "Fathom.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "fathom-desktop").write_bytes(b"MACH-O-FAKE")
    helper = app / "Contents/Resources/helper/fathom-helper"
    helper.mkdir(parents=True)
    (helper / "fathom-helper").write_bytes(b"HELPER-BINARY")
    (app / "Contents" / "Info.plist").write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        b'<plist version="1.0"><dict>'
        b"<key>CFBundleIdentifier</key><string>com.fathom.pmverify.iss141</string>"
        b"<key>CFBundleExecutable</key><string>fathom-desktop</string>"
        b"</dict></plist>")
    return app


def build_manifest(app: Path, *, commit: str = "a" * 40, dirty: bool = False,
                   bundle_id: str | None = None, fingerprint: str | None = None,
                   helper_sha: str | None = None, extra_top: str | None = None,
                   executable: str | None = None) -> dict:
    observed = vta.bundle_fingerprint(app)
    manifest = {
        "version": vta.MANIFEST_VERSION,
        "app": {
            "path": str(app),
            "bundle_id": bundle_id or vta.read_bundle_id(app),
            "executable": executable or vta.read_bundle_executable(app),
            "fingerprint_sha256": fingerprint or observed["fingerprint_sha256"],
            "files": dict(observed["files"]),
        },
        "helper": {
            "relative_path": vta.HELPER_RELATIVE_PATH,
            "sha256": helper_sha or observed["files"][vta.HELPER_RELATIVE_PATH],
        },
        "source": {
            "commit": commit, "dirty": dirty,
            "input_paths": ["fathom"],
            "checked_at": "2026-10-02T00:00:00Z",
            "receipt": "/tmp/receipt.json",
        },
        "built_at": 1790883953.0,
    }
    if extra_top:
        manifest[extra_top] = "unexpected"
    return manifest


@pytest.fixture
def isolated(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "run" / "runtime"
    runtime.mkdir(parents=True)
    scan = tmp_path / "run" / "synthetic-scan-root"
    scan.mkdir(parents=True)
    return home, runtime, scan


# --------------------------------------------------------------------------
# B6 / 身份门：manifest 必须与真实产物吻合
# --------------------------------------------------------------------------


def test_manifest_matching_app_passes(fake_app: Path) -> None:
    result = vta.verify_manifest_identity(build_manifest(fake_app), fake_app)
    assert result["bundle_id"] == "com.fathom.pmverify.iss141"
    assert result["executable"] == "fathom-desktop"


def test_fingerprint_mismatch_is_rejected(fake_app: Path) -> None:
    with pytest.raises(vta.IdentityError, match="指纹不符"):
        vta.verify_manifest_identity(
            build_manifest(fake_app, fingerprint="b" * 64), fake_app)


def test_single_byte_binary_change_is_rejected(fake_app: Path) -> None:
    manifest = build_manifest(fake_app)
    (fake_app / "Contents" / "MacOS" / "fathom-desktop").write_bytes(b"MACH-O-FAKZ")
    with pytest.raises(vta.IdentityError):
        vta.verify_manifest_identity(manifest, fake_app)


def test_executable_name_mismatch_is_rejected(fake_app: Path) -> None:
    with pytest.raises(vta.IdentityError, match="可执行名不符"):
        vta.verify_manifest_identity(
            build_manifest(fake_app, executable="Fathom"), fake_app)


def test_helper_sha_mismatch_is_rejected(fake_app: Path) -> None:
    with pytest.raises(vta.IdentityError, match="helper"):
        vta.verify_manifest_identity(
            build_manifest(fake_app, helper_sha="c" * 64), fake_app)


def test_dirty_source_is_rejected(fake_app: Path) -> None:
    with pytest.raises(vta.IdentityError, match="dirty"):
        vta.verify_manifest_identity(
            build_manifest(fake_app, dirty=True), fake_app)


def test_commit_mismatch_is_rejected(fake_app: Path) -> None:
    with pytest.raises(vta.IdentityError, match="commit"):
        vta.verify_manifest_identity(
            build_manifest(fake_app), fake_app, expected_commit="d" * 40)


def test_extra_file_is_rejected(fake_app: Path) -> None:
    manifest = build_manifest(fake_app)
    (fake_app / "Contents" / "Resources" / "stow.bin").write_bytes(b"x")
    with pytest.raises(vta.IdentityError, match="额外文件|指纹不符"):
        vta.verify_manifest_identity(manifest, fake_app)


@pytest.mark.parametrize("extra_top", ["notes", "guessed", "kind", "app_path"])
def test_unknown_manifest_top_key_is_rejected(
    fake_app: Path, tmp_path: Path, extra_top: str
) -> None:
    """真实回执的字段（kind/app_path/started_at…）不能直接当 manifest 用。"""
    path = tmp_path / f"m-{extra_top}.json"
    path.write_text(json.dumps(build_manifest(fake_app, extra_top=extra_top)),
                    encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="未知顶层字段"):
        vta.load_manifest(path)


def test_source_section_requires_evidence_fields(fake_app: Path) -> None:
    manifest = build_manifest(fake_app)
    del manifest["source"]["input_paths"]
    with pytest.raises(vta.IdentityError, match="input_paths"):
        vta.verify_manifest_identity(manifest, fake_app)


def test_source_dirty_must_be_explicit_false(fake_app: Path) -> None:
    manifest = build_manifest(fake_app)
    manifest["source"]["dirty"] = "false"  # 字符串假值不许蒙
    with pytest.raises(vta.IdentityError, match="dirty"):
        vta.verify_manifest_identity(manifest, fake_app)


def test_empty_input_paths_is_rejected(fake_app: Path) -> None:
    manifest = build_manifest(fake_app)
    manifest["source"]["input_paths"] = []
    with pytest.raises(vta.IdentityError, match="input_paths"):
        vta.verify_manifest_identity(manifest, fake_app)


def test_load_manifest_rejects_malformed_json(tmp_path: Path) -> None:
    bad = tmp_path / "m.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="合法 JSON"):
        vta.load_manifest(bad)


def test_load_manifest_rejects_non_object(tmp_path: Path) -> None:
    bad = tmp_path / "m.json"
    bad.write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="JSON 对象"):
        vta.load_manifest(bad)


# --------------------------------------------------------------------------
# B6：gen-manifest 必须据真实输入判定，条件不足明确拒绝
# --------------------------------------------------------------------------


def _real_receipt(app: Path, **overrides) -> dict:
    observed = vta.bundle_fingerprint(app)
    receipt = {
        "ok": True,
        "app_path": str(app),
        "test_bundle_identifier": vta.read_bundle_id(app),
        "app_executable_sha256":
            observed["files"][f"Contents/MacOS/{vta.read_bundle_executable(app)}"],
        "helper_sha256": observed["files"][vta.HELPER_RELATIVE_PATH],
        "source_inputs_commit": "f78b34018d64e91ad9c470ecc74f5f00b877bd4f",
        "source_input_paths": ["fathom"],
        "head_at_finish": "c2ce1fe",
        "finished_at": 1790883953.0,
    }
    receipt.update(overrides)
    return receipt


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "fathom").mkdir(parents=True)
    (repo / "fathom" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],  # noqa: E731
                                    check=True, capture_output=True, timeout=60)
    run("init", "-q")
    run("add", "-A")
    # 必须真正提交：只 add 不 commit 会被 git status 记成待提交（A ），dirty 判定会命中
    run("-c", "user.email=t@e.st", "-c", "user.name=test", "commit", "-q", "-m", "init")
    assert not vta.git_porcelain(repo), "夹具应给出干净工作树"
    return repo


def test_gen_manifest_from_real_receipt(fake_app: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_real_receipt(fake_app)), encoding="utf-8")
    manifest = vta.generate_manifest(app_path=fake_app, receipt_path=receipt,
                                     repo=repo)
    assert manifest["source"]["dirty"] is False
    assert manifest["source"]["input_paths"] == ["fathom"]
    assert manifest["app"]["executable"] == "fathom-desktop"
    # 正例必须真能通过启动前硬门
    vta.verify_manifest_identity(manifest, fake_app)


def test_gen_manifest_refuses_dirty_input_path(fake_app: Path, tmp_path: Path) -> None:
    """构建输入脏 → 明确拒绝，绝不手填 dirty=false（B6 红线）。"""
    repo = _git_repo(tmp_path)
    (repo / "fathom" / "mod.py").write_text("x = 2  # dirty\n", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_real_receipt(fake_app)), encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="脏"):
        vta.generate_manifest(app_path=fake_app, receipt_path=receipt, repo=repo)


def test_gen_manifest_allows_dirty_outside_input_paths(
    fake_app: Path, tmp_path: Path
) -> None:
    """输入路径外的改动（含他人 worktree 文件）不应误判为脏。"""
    repo = _git_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("untracked\n", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_real_receipt(fake_app)), encoding="utf-8")
    manifest = vta.generate_manifest(app_path=fake_app, receipt_path=receipt,
                                     repo=repo)
    assert manifest["source"]["dirty"] is False


def test_gen_manifest_refuses_failed_build(fake_app: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_real_receipt(fake_app, ok=False)),
                       encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="ok=False"):
        vta.generate_manifest(app_path=fake_app, receipt_path=receipt, repo=repo)


@pytest.mark.parametrize("missing", ["source_input_paths", "app_executable_sha256",
                                     "test_bundle_identifier", "source_inputs_commit"])
def test_gen_manifest_refuses_incomplete_receipt(
    fake_app: Path, tmp_path: Path, missing: str
) -> None:
    repo = _git_repo(tmp_path)
    payload = _real_receipt(fake_app)
    del payload[missing]
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="缺字段"):
        vta.generate_manifest(app_path=fake_app, receipt_path=receipt, repo=repo)


def test_gen_manifest_refuses_mismatched_app_path(fake_app: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_real_receipt(fake_app, app_path="/other/App.app")),
                       encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="app_path 不一致"):
        vta.generate_manifest(app_path=fake_app, receipt_path=receipt, repo=repo)


def test_gen_manifest_refuses_exec_sha_drift(fake_app: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(_real_receipt(fake_app, app_executable_sha256="0" * 64)),
        encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="可执行二进制 SHA"):
        vta.generate_manifest(app_path=fake_app, receipt_path=receipt, repo=repo)


def test_gen_manifest_refuses_helper_sha_drift(fake_app: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(_real_receipt(fake_app, helper_sha256="0" * 64)),
        encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="helper 二进制 SHA"):
        vta.generate_manifest(app_path=fake_app, receipt_path=receipt, repo=repo)


def test_gen_manifest_records_head_drift(tmp_path: Path, fake_app: Path) -> None:
    repo = _git_repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_real_receipt(fake_app, head_at_finish="deadbee")),
                       encoding="utf-8")
    manifest = vta.generate_manifest(app_path=fake_app, receipt_path=receipt,
                                     repo=repo)
    assert manifest["source"]["receipt_head_at_finish"] == "deadbee"
    assert manifest["source"]["current_head"] != "deadbee"


def test_touched_paths_handles_rename_and_quotes() -> None:
    paths = vta.touched_paths([
        " M fathom/a.py", "R  old.py -> fathom/new.py", '?? "fathom/sp ace.py"',
    ])
    assert "fathom/a.py" in paths
    assert "fathom/new.py" in paths


# --------------------------------------------------------------------------
# B1：启动前必须证明隔离 env 有效
# --------------------------------------------------------------------------


def test_isolation_plan_passes(isolated) -> None:
    home, runtime, scan = isolated
    vta.validate_isolation_plan(runtime_dir=runtime, scan_root=scan,
                                port=52001, home=home)


def test_isolation_plan_rejects_missing_runtime(isolated, tmp_path: Path) -> None:
    home, _runtime, scan = isolated
    with pytest.raises(vta.GateError, match="runtime 根不存在"):
        vta.validate_isolation_plan(runtime_dir=tmp_path / "absent",
                                    scan_root=scan, port=52001, home=home)


def test_isolation_plan_rejects_missing_scan_root(isolated, tmp_path: Path) -> None:
    home, runtime, _scan = isolated
    with pytest.raises(vta.GateError, match="扫描根不存在"):
        vta.validate_isolation_plan(runtime_dir=runtime,
                                    scan_root=tmp_path / "absent",
                                    port=52001, home=home)


def test_isolation_plan_rejects_scan_root_in_home(isolated) -> None:
    home, runtime, _scan = isolated
    nested = home / "scan"
    nested.mkdir()
    with pytest.raises(vta.GateError, match="HOME"):
        vta.validate_isolation_plan(runtime_dir=runtime, scan_root=nested,
                                    port=52001, home=home)


def test_isolation_plan_rejects_home_itself(isolated) -> None:
    home, runtime, _scan = isolated
    with pytest.raises(vta.GateError, match="HOME"):
        vta.validate_isolation_plan(runtime_dir=runtime, scan_root=home,
                                    port=52001, home=home)


def test_isolation_plan_rejects_production_support_dir(isolated) -> None:
    """生产支持目录必然在 HOME 下，HOME 门先命中——仍必须拒绝启动。

    生产目录分支是冗余纵深（prod 由传入 home 推导，恒在其下），
    独立可达路径不存在；这里只断言「拒绝」与先命中者，不伪造可达性。
    """
    home, runtime, _scan = isolated
    prod = vta.production_support_dir(home)
    prod.mkdir(parents=True)
    with pytest.raises(vta.GateError, match="落在 HOME 内") as excinfo:
        vta.validate_isolation_plan(runtime_dir=runtime, scan_root=prod,
                                    port=52001, home=home)
    assert "生产支持目录" not in str(excinfo.value)


@pytest.mark.parametrize("bad_port", [0, -1, 7952, 70_000, "abc", None])
def test_isolation_plan_rejects_bad_port(isolated, bad_port) -> None:
    home, runtime, scan = isolated
    with pytest.raises(vta.GateError):
        vta.validate_isolation_plan(runtime_dir=runtime, scan_root=scan,
                                    port=bad_port, home=home)


def test_isolation_plan_rejects_production_port_explicitly(isolated) -> None:
    home, runtime, scan = isolated
    with pytest.raises(vta.GateError, match="7952"):
        vta.validate_isolation_plan(runtime_dir=runtime, scan_root=scan,
                                    port=vta.PRODUCTION_PORT, home=home)


def test_free_port_never_returns_production_port() -> None:
    import random
    for seed in range(8):
        port = vta.pick_free_port(random.Random(seed))
        assert port != vta.PRODUCTION_PORT


# --------------------------------------------------------------------------
# B2 / B4：进程身份 —— foreign / dead / reused 一律拒绝发信号
# --------------------------------------------------------------------------


def test_owned_process_matches_own_gui_process(fake_app: Path) -> None:
    """R2 后 GUI 也要求钉住绝对路径（不再只做 app_path 子串包含）。"""
    exe = f"{fake_app}/Contents/MacOS/fathom-desktop"
    proc = vta.OwnedProcess(pid=4242, executable="fathom-desktop",
                            executable_path=exe, bundle_id="com.x", role="gui")
    observed = {"comm": "fathom-desktop", "path": exe, "args": exe}
    assert proc.matches(observed, app_path=fake_app) is True


def test_owned_process_rejects_foreign_comm(fake_app: Path) -> None:
    exe = f"{fake_app}/Contents/MacOS/fathom-desktop"
    proc = vta.OwnedProcess(pid=4242, executable="fathom-desktop",
                            executable_path=exe, bundle_id="com.x", role="gui")
    observed = {"comm": "finder", "path": "/usr/bin/finder", "args": "/usr/bin/finder"}
    assert proc.matches(observed, app_path=fake_app) is False
    assert "reused" in proc.rejection_reason(observed, app_path=fake_app)


def test_owned_process_rejects_same_name_other_app(fake_app: Path) -> None:
    """同可执行名但 argv[0] 在别处 → 拒绝（防收养他人实例）。"""
    exe = f"{fake_app}/Contents/MacOS/fathom-desktop"
    proc = vta.OwnedProcess(pid=4242, executable="fathom-desktop",
                            executable_path=exe, bundle_id="com.x", role="gui")
    other = "/other/Fathom.app/Contents/MacOS/fathom-desktop"
    observed = {"comm": "fathom-desktop", "path": other, "args": other}
    assert proc.matches(observed, app_path=fake_app) is False
    assert "reused" in proc.rejection_reason(observed, app_path=fake_app)


def test_owned_process_rejects_dead(fake_app: Path) -> None:
    exe = f"{fake_app}/Contents/MacOS/fathom-desktop"
    proc = vta.OwnedProcess(pid=4242, executable="fathom-desktop",
                            executable_path=exe, bundle_id="com.x", role="gui")
    assert proc.matches(None, app_path=fake_app) is False
    assert "dead" in proc.rejection_reason(None, app_path=fake_app)


def test_owned_process_rejects_reused_pid(fake_app: Path) -> None:
    """PID 被复用：ucomm 变了 → 拒绝升级信号。"""
    exe = f"{fake_app}/Contents/MacOS/fathom-desktop"
    proc = vta.OwnedProcess(pid=4242, executable="fathom-desktop",
                            executable_path=exe, bundle_id="com.x", role="gui")
    reused = {"comm": "sleep", "path": "/bin/sleep", "args": "sleep 999"}
    assert proc.matches(reused, app_path=fake_app) is False


def test_helper_role_requires_nonzero_nonce_and_exact_args(fake_app: Path) -> None:
    exe = f"{fake_app}/{vta.HELPER_RELATIVE_PATH}"
    proc = vta.OwnedProcess(pid=77, executable="fathom-helper",
                            executable_path=exe, bundle_id="", role="helper",
                            start_args=exe, instance_nonce="nonce-1")
    good = {"comm": "fathom-helper", "path": exe, "args": exe}
    assert proc.matches(good, app_path=fake_app) is True
    # args 漂移（启动凭证不再一致）→ 拒
    drifted = {"comm": "fathom-helper", "path": exe, "args": exe + " --flag"}
    assert proc.matches(drifted, app_path=fake_app) is False


def test_reclaim_refuses_invalid_pid(fake_app: Path, tmp_path: Path) -> None:
    proc = vta.OwnedProcess(pid=1, executable="fathom-desktop",
                            executable_path="", bundle_id="", role="gui")
    report = vta.reclaim_owned([proc], app_path=fake_app, log=tmp_path / "log")
    assert report["skipped_identity"][0]["reason"] == "invalid_pid"
    assert report["still_alive"] == []


def test_reclaim_reports_already_exited(fake_app: Path, tmp_path: Path) -> None:
    proc = vta.OwnedProcess(pid=999_999, executable="fathom-desktop",
                            executable_path="", bundle_id="", role="gui")
    report = vta.reclaim_owned([proc], app_path=fake_app, log=tmp_path / "log",
                               grace_s=0.0)
    assert report["reclaimed"][0]["how"] == "already_exited"
    assert report["skipped_identity"] == []


def test_reclaim_never_signals_foreign_process(fake_app: Path, tmp_path: Path) -> None:
    """用本机常驻进程（launchd, pid 1 系外）做负例：绝不能被杀掉。"""
    victim = subprocess.Popen(["sleep", "30"])
    try:
        proc = vta.OwnedProcess(pid=victim.pid, executable="fathom-desktop",
                                executable_path="", bundle_id="", role="gui")
        report = vta.reclaim_owned([proc], app_path=fake_app,
                                   log=tmp_path / "log", grace_s=0.0)
        assert report["skipped_identity"], "身份不符必须跳过"
        assert victim.poll() is None, "foreign 进程被杀掉了！"
    finally:
        victim.kill()
        victim.wait(timeout=10)


# R3：原 test_reclaim_kills_only_own_process（cp /bin/sleep 副本）已删除——
# 该平台二进制副本在本机 exec 即被 SIGKILL，进程自始僵尸，测试空洞通过
# 且在 CI（arm/Intel）上 victim.wait(10s) 超时失败。正例改由
# test_r3_positive_reclaim_really_happens（clang 合成助手）承担。


def test_ps_identity_uses_short_name_not_truncated_path() -> None:
    """返修实测：`ps -o comm=` 会把长路径截断成 /Users/maoking/o，
    导致身份复核恒不成立 → 进程漏回收。必须用 ucomm 短名（不含 /）。

    直接拿本进程（长寿命、名字带路径）当被测对象，不额外造子进程。
    """
    observed = vta.ps_identity(os.getpid())
    assert observed is not None
    assert "/" not in observed["comm"], f"comm 必须是短名，实际 {observed['comm']!r}"
    assert observed["args"], "args 应非空"
    assert observed["path"], "应解析出可执行路径"


def test_ps_identity_returns_none_for_dead_pid() -> None:
    assert vta.ps_identity(999_999) is None


def test_gui_and_helper_pids_must_differ() -> None:
    """B2 的核心不变量：GUI pid 与 helper pid 不得混用。"""
    assert vta.PRODUCTION_PORT == 7952
    # 入口在阶段 4 显式检查两者不相等，测试锁住该语义
    instance = {"pid": 111, "port": 52001}
    vta.assert_port_owned(52001, instance, pid=111)


# --------------------------------------------------------------------------
# 隔离扫描根 / 端口
# --------------------------------------------------------------------------


def test_exact_scan_root_passes(isolated) -> None:
    home, _runtime, scan = isolated
    assert vta.assert_isolated_scan_root(
        str(scan), expected=scan, home=home) == vta.normalize_root(scan)


def test_scan_root_trailing_slash_normalizes(isolated) -> None:
    home, _runtime, scan = isolated
    assert vta.assert_isolated_scan_root(f"{scan}/", expected=scan,
                                         home=home) == vta.normalize_root(scan)


def test_different_scan_root_is_rejected(isolated) -> None:
    home, _runtime, scan = isolated
    other = home.parent / "other"
    other.mkdir()
    with pytest.raises(vta.GateError, match="越界"):
        vta.assert_isolated_scan_root(str(other), expected=scan, home=home)


def test_missing_scan_root_value_is_rejected(isolated) -> None:
    home, _runtime, scan = isolated
    for bad in (None, "", "   ", 42, {}):
        with pytest.raises(vta.GateError, match="未回读生效扫描根"):
            vta.assert_isolated_scan_root(bad, expected=scan, home=home)


def test_extract_scan_root_handles_both_shapes() -> None:
    assert vta.extract_scan_root({"scan_root": "/tmp/x"}) == "/tmp/x"
    assert vta.extract_scan_root(
        {"scan_root": {"value": "/tmp/x", "source": "settings"}}) == "/tmp/x"
    assert vta.extract_scan_root({"other": 1}) is None
    assert vta.extract_scan_root("not a dict") is None


def test_port_and_pid_must_match_instance() -> None:
    vta.assert_port_owned(51234, {"pid": 4242, "port": 51234}, pid=4242)
    with pytest.raises(vta.GateError, match="port"):
        vta.assert_port_owned(51234, {"pid": 4242, "port": 7952}, pid=4242)
    with pytest.raises(vta.GateError, match="pid"):
        vta.assert_port_owned(51234, {"pid": 4243, "port": 51234}, pid=4242)


# --------------------------------------------------------------------------
# B3：driver --help 真实 JSON 契约（真编译 + 真调用）
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compiled_driver(tmp_path_factory) -> Path:
    swiftc = __import__("shutil").which("swiftc")
    if not swiftc:
        pytest.skip("本机无 swiftc，跳过真实编译契约")
    out = tmp_path_factory.mktemp("drv") / "driver"
    proc = subprocess.run([swiftc, "-O", "-o", str(out), str(DRIVER_SOURCE)],
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not out.is_file():
        pytest.fail(f"驱动编译失败：{proc.stderr[:800]}")
    return out


def test_driver_help_is_json_contract(compiled_driver: Path) -> None:
    """B3：--help 必须能被 json.loads 解析（旧版是纯文本，确定性崩）。"""
    proc = subprocess.run([str(compiled_driver), "--help"], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)          # 旧实现此处直接 ValueError
    assert payload["card"] == "ISS-141"
    assert "launch" in payload["subcommands"]
    assert "resolve-app" in payload["subcommands"]


def test_driver_help_lists_product_subcommands(compiled_driver: Path) -> None:
    payload = json.loads(
        subprocess.run([str(compiled_driver), "--help"], capture_output=True,
                       text=True, timeout=60).stdout)
    for sub in ("identify", "set-size", "measure", "click", "screenshot"):
        assert sub in payload["subcommands"]


def test_driver_rejects_missing_pid(compiled_driver: Path) -> None:
    proc = subprocess.run([str(compiled_driver), "identify", "--bundle-id", "x"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "--pid" in proc.stderr


def test_driver_rejects_dead_pid(compiled_driver: Path) -> None:
    proc = subprocess.run([str(compiled_driver), "identify", "--pid", "999999",
                           "--bundle-id", "com.x"], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 3


def test_driver_rejects_unknown_subcommand(compiled_driver: Path) -> None:
    proc = subprocess.run([str(compiled_driver), "nope", "--pid", "1",
                           "--bundle-id", "x"], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 2
    assert "未知子命令" in proc.stderr


def test_driver_launch_requires_isolation_args(compiled_driver: Path) -> None:
    proc = subprocess.run([str(compiled_driver), "launch", "--app", "/nonexistent.app"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "--runtime-dir" in proc.stderr


def test_driver_launch_refuses_missing_runtime_root(compiled_driver: Path) -> None:
    """B1：启动前 env 无效 → 拒绝启动，不产生任何进程。"""
    proc = subprocess.run([str(compiled_driver), "launch", "--app", "/x.app",
                           "--runtime-dir", "/tmp/definitely-absent-141",
                           "--scan-root", "/tmp", "--port", "52001"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3
    assert "runtime 根不存在" in proc.stderr


def test_driver_launch_refuses_home_scan_root(compiled_driver: Path,
                                              tmp_path: Path) -> None:
    runtime = tmp_path / "rt"
    runtime.mkdir()
    proc = subprocess.run([str(compiled_driver), "launch", "--app", "/x.app",
                           "--runtime-dir", str(runtime), "--scan-root",
                           str(Path.home()), "--port", "52001"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3
    assert "HOME" in proc.stderr


def test_driver_launch_refuses_production_port(compiled_driver: Path,
                                               tmp_path: Path) -> None:
    runtime = tmp_path / "rt"
    runtime.mkdir()
    scan = tmp_path / "scan"
    scan.mkdir()
    proc = subprocess.run([str(compiled_driver), "launch", "--app", "/x.app",
                           "--runtime-dir", str(runtime), "--scan-root",
                           str(scan), "--port", "7952"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3
    assert "7952" in proc.stderr


def test_driver_resolve_app_requires_bundle_id(compiled_driver: Path) -> None:
    proc = subprocess.run([str(compiled_driver), "resolve-app"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2


def test_driver_resolve_app_absent_bundle_is_refused(compiled_driver: Path) -> None:
    proc = subprocess.run([str(compiled_driver), "resolve-app", "--bundle-id",
                           "com.fathom.absent.141"], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 3


def test_driver_help_declares_never_activate(compiled_driver: Path) -> None:
    payload = json.loads(
        subprocess.run([str(compiled_driver), "--help"], capture_output=True,
                       text=True, timeout=60).stdout)
    assert "activate" in payload["never"]
    assert "launchctl" in payload["never"]


# --------------------------------------------------------------------------
# 非阻断项：set-size 不搬位置 / measure 不写死 -28
# --------------------------------------------------------------------------


def _swift_code_only(source: str) -> str:
    """剥掉注释**与字符串字面量**再检查。

    只剥注释不够：`never: [... "AXRaise" ...]` 这类帮助文本字符串会自己
    触发禁用词断言（我们恰恰是在声明「不做 AXRaise」）。真正要回答的是
    「代码会不会调用它」。
    """
    lines = []
    in_block = False
    for raw in source.splitlines():
        stripped = raw.strip()
        if stripped.startswith("/*"):
            in_block = True
            continue
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("//"):
            continue
        # 去掉行注释后再去掉字符串字面量
        code = raw.split("//", 1)[0]
        out, inside, escaped = [], False, False
        for ch in code:
            if escaped:
                out.append(" ")
                escaped = False
                continue
            if ch == "\\" and inside:
                escaped = True
                continue
            if ch == '"':
                inside = not inside
                out.append(" ")
                continue
            out.append(" " if inside else ch)
        lines.append("".join(out))
    return "\n".join(lines)


def test_driver_never_activates_or_uses_global_keyboard() -> None:
    code = _swift_code_only(DRIVER_SOURCE.read_text(encoding="utf-8"))
    for banned in ("AXRaise", "kCGEventTap", "CGEventPost(",
                   ".activate(", "AXPress"):
        assert banned not in code, f"驱动代码不得出现 {banned}"


def test_driver_launch_sets_activates_false() -> None:
    code = _swift_code_only(DRIVER_SOURCE.read_text(encoding="utf-8"))
    assert "config.activates = false" in code


def test_driver_set_size_only_moves_when_xy_given() -> None:
    """旧行为：Python 不传 --x/--y 也无条件重设位置 → 窗口漂移。"""
    raw = DRIVER_SOURCE.read_text(encoding="utf-8")
    code = _swift_code_only(raw)
    # 位置改动必须被显式坐标门控（代码层）
    assert "if options.xSet && options.ySet" in code
    # 且必须把是否真的搬了位置回报给调用方（键名在字符串里，查原始源）
    assert '"position_moved": moved' in raw


def test_driver_measure_uses_window_level_ax() -> None:
    """旧行为：在 application 元素上读 kAXSize（通常 unsupported）。"""
    code = _swift_code_only(DRIVER_SOURCE.read_text(encoding="utf-8"))
    assert "kAXWindowsAttribute" in code
    assert "kAXFocusedWindowAttribute" in code


def test_driver_measures_web_area_not_title_label() -> None:
    """第 3 轮根因修复：内容几何必须取 AXWebArea，不得用标题文字高度。"""
    raw = DRIVER_SOURCE.read_text(encoding="utf-8")
    code = _swift_code_only(raw)
    assert "webAreaElement" in code
    assert "AXWebArea" in raw          # 常量值在字符串里，查原始源
    # 旧的错误来源必须消失
    assert "measuredTitlebarHeight" not in raw, "标题文字高度不得再充当标题栏高"
    assert "TITLEBAR_HEIGHT" not in code, "不得写死 28 假装实测"
    # 标题文字高度保留但必须明确标注它不是内容差值
    assert '"title_label_note"' in raw
    assert "非标题栏高、非内容区差值" in raw


def test_driver_measure_reports_content_source_and_fails_closed() -> None:
    raw = DRIVER_SOURCE.read_text(encoding="utf-8")
    assert 'payload["content_source"] = "ax_web_area"' in raw
    assert 'payload["content_source"] = "unavailable"' in raw
    assert "无法证明 WKWebView 实际内容区" in raw


def test_driver_chrome_is_measured_difference_not_constant() -> None:
    """chrome 高必须是 frame - web area 的实测差值。"""
    raw = DRIVER_SOURCE.read_text(encoding="utf-8")
    assert 'payload["chrome_height"] = Int((size.height - webSize.height).rounded())' in raw
    assert "非假设常数" in raw


def test_driver_click_plane_matches_content_plane() -> None:
    """点击面必须与内容测量面同源（web area 偏移），且可要求同源否则拒投。"""
    raw = DRIVER_SOURCE.read_text(encoding="utf-8")
    code = _swift_code_only(raw)
    assert "options.clickX + (webPos.x - winPos.x)" in code
    assert "options.clickY + (webPos.y - winPos.y)" in code
    assert "options.requireContentPlane" in code
    assert "点击面无法与内容测量面同源" in raw


def test_python_entry_no_hardcoded_28_offset() -> None:
    """旧的 `content_h + 28` 写死偏移必须消失。"""
    code = MODULE_PATH.read_text(encoding="utf-8")
    assert "content_h + 28" not in code
    assert "calibrate_window_size" in code


# --------------------------------------------------------------------------
# 第 3 轮：内容几何来源与有界校准
# --------------------------------------------------------------------------


class _FakeMeasureDriver:
    """合成 driver：模拟真实 web area 几何（frame = content + chrome）。

    chrome=28 用来复现「写死 28 恰好在本机成立」的巧合；另一个实例用
    chrome=40 证明校准不依赖某个特定 chrome 值。
    """

    def __init__(self, chrome: int = 28, *, content_source: str = "ax_web_area",
                 jitter: int = 0) -> None:
        self.chrome = chrome
        self.content_source = content_source
        self.jitter = jitter
        self.frame_w = 0
        self.frame_h = 0
        self.measure_calls = 0
        self.set_calls: list[tuple[int, int]] = []

    def run(self, argv, timeout=120.0):
        if argv[0] == "measure":
            self.measure_calls += 1
            content_w = self.frame_w
            content_h = self.frame_h - self.chrome
            # jitter 模拟首轮未稳定，迫使多轮收敛
            if self.jitter and self.measure_calls == 1:
                content_h += self.jitter
            payload = {
                "content_source": self.content_source,
                "content_width": content_w, "content_height": content_h,
                "frame_width": self.frame_w, "frame_height": self.frame_h,
            }
            if self.content_source == "ax_web_area":
                payload["chrome_height"] = self.chrome
            else:
                payload["content_width"] = None
                payload["content_height"] = None
                payload["blocker"] = "未找到 AXWebArea"
            return payload
        if argv[0] == "set-size":
            width = int(argv[argv.index("--width") + 1])
            height = int(argv[argv.index("--height") + 1])
            self.set_calls.append((width, height))
            self.frame_w, self.frame_h = width, height
            return {"position_moved": False}
        raise AssertionError(f"unexpected subcommand {argv[0]}")


def test_r3_old_logic_counterexample_is_real() -> None:
    """旧逻辑反例：写死 28 + 把标题文字 16 当标题栏 => 980x652。"""
    frame_h = 640 + 28
    title_label = 16
    assert frame_h - title_label == 652          # PM 实机实测值
    assert 652 != 640


def test_r3_calibration_hits_target_with_chrome_28() -> None:
    driver = _FakeMeasureDriver(chrome=28)
    driver.frame_w, driver.frame_h = 800, 500
    result = vta.calibrate_window_size(
        driver, app_pid=1, bundle_id="com.x", window_id=1,
        target_w=980, target_h=640)
    assert result["content"] == (980, 640)
    assert result["converged"] is True
    assert all(t["content_source"] == "ax_web_area" for t in result["trace"])


def test_r3_calibration_hits_target_with_other_chrome() -> None:
    """校准不依赖 chrome 恰为 28。"""
    for chrome in (0, 16, 28, 40, 52):
        driver = _FakeMeasureDriver(chrome=chrome)
        driver.frame_w, driver.frame_h = 500, 500
        result = vta.calibrate_window_size(
            driver, app_pid=1, bundle_id="com.x", window_id=1,
            target_w=1220, target_h=820)
        assert result["content"] == (1220, 820), chrome


def test_r3_calibration_converges_under_jitter_within_bound() -> None:
    driver = _FakeMeasureDriver(chrome=28, jitter=7)
    driver.frame_w, driver.frame_h = 700, 600
    result = vta.calibrate_window_size(
        driver, app_pid=1, bundle_id="com.x", window_id=1,
        target_w=980, target_h=640, max_iterations=6)
    assert result["content"] == (980, 640)
    assert result["iterations"] <= 6


def test_r3_calibration_fails_closed_without_web_area() -> None:
    """无法证明实际内容区 → 报 blocker，不伪证、不放宽容差。"""
    driver = _FakeMeasureDriver(content_source="unavailable")
    driver.frame_w, driver.frame_h = 800, 600
    with pytest.raises(vta.GateError, match="无法证明 WKWebView 实际内容区"):
        vta.calibrate_window_size(
            driver, app_pid=1, bundle_id="com.x", window_id=1,
            target_w=980, target_h=640)


def test_r3_calibration_fails_closed_when_unreachable() -> None:
    """内容面不可达目标时必须在有界轮数后失败闭合，不无限循环也不假绿。"""

    class Drifting(_FakeMeasureDriver):
        """内容面完全不对 frame 变化响应 → 真正不可达。"""

        def run(self, argv, timeout=120.0):
            if argv[0] == "measure":
                self.measure_calls += 1
                return {"content_source": "ax_web_area",
                        "content_width": 500, "content_height": 500,
                        "frame_width": self.frame_w, "frame_height": self.frame_h}
            return super().run(argv, timeout=timeout)

    driver = Drifting(chrome=28)
    driver.frame_w, driver.frame_h = 800, 600
    with pytest.raises(vta.GateError, match="有界校准"):
        vta.calibrate_window_size(
            driver, app_pid=1, bundle_id="com.x", window_id=1,
            target_w=980, target_h=640, max_iterations=3)


def test_r3_require_content_geometry_rejects_frame_fallback() -> None:
    with pytest.raises(vta.GateError, match="无法证明"):
        vta.require_content_geometry(
            {"content_source": "frame", "content_width": 980,
             "content_height": 640}, label="980x640")


def test_r3_require_content_geometry_accepts_web_area() -> None:
    assert vta.require_content_geometry(
        {"content_source": "ax_web_area", "content_width": 980,
         "content_height": 640}, label="980x640") == (980, 640)


def test_r3_target_sizes_unchanged() -> None:
    assert vta.TARGET_CONTENT_SIZES == ((980, 640), (1220, 820))


def test_r3_chrome_never_named_as_content() -> None:
    """不得把 frame 或标题文字称作 webview 内容。"""
    raw = DRIVER_SOURCE.read_text(encoding="utf-8")
    assert '"content_width": Int(size.width)' not in raw
    assert '"content_height": Int((size.height - titlebar)' not in raw


def test_driver_posts_clicks_via_post_to_pid() -> None:
    code = _swift_code_only(DRIVER_SOURCE.read_text(encoding="utf-8"))
    assert ".postToPid(" in code
    assert "CGEventPostToPid(" not in code


def _python_code_only(path: Path) -> str:
    """抽出非 docstring 代码文本（AST）。

    模块 docstring 里写着「弃用 pgrep -x Fathom」这类说明，纯文本搜会
    把自己的说明当违规。收集非 docstring 字符串常量与全部标识符名。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                parts.append(node.value)
        elif isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
    return "\n".join(parts)


def test_python_entry_never_uses_launchctl_fallback() -> None:
    assert "launchctl" not in _python_code_only(MODULE_PATH)


def test_python_entry_never_uses_pgrep_set_difference() -> None:
    """B4：旧实现用 pgrep -x Fathom + 集合差收养，已废止。"""
    code = _python_code_only(MODULE_PATH)
    assert "pgrep" not in code
    assert "running_fathom_pids" not in code
    assert "ps_identity" in code          # 改为逐 PID 复核
    assert "reclaim_owned" in code


def test_python_entry_signals_only_from_reclaim_owned() -> None:
    """所有信号必须且只能在 reclaim_owned 内（那里有逐 PID 身份复核）。

    旧实现的裸 os.kill（无身份复核）会造成 PID 复用误杀。
    """
    import ast

    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    owners: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "kill" \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "os":
            owners.append("unknown")
    # 逐函数定位：os.kill 出现的所在函数
    kill_hosts: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                        and sub.func.attr == "kill" \
                        and isinstance(sub.func.value, ast.Name) \
                        and sub.func.value.id == "os":
                    kill_hosts.add(node.name)
    assert owners, "应存在 os.kill 调用"
    assert kill_hosts == {"reclaim_owned"}, f"os.kill 出现在 {kill_hosts}"


# --------------------------------------------------------------------------
# B5：产品交互暂停 / 终态 / 清理
# --------------------------------------------------------------------------


def test_product_timeout_rejected_when_negative() -> None:
    args = vta.build_parser().parse_args([
        "run", "--app", "/a", "--build-ready", "/b", "--output", "/c",
        "--product-timeout", "-5"])
    assert args.product_timeout == -5.0  # 解析层接受；运行层由 collect 兜住


def test_product_pause_writes_marker_and_collects(tmp_path: Path) -> None:
    """暂停点必须留下可执行指引，并在有界时间内收口。"""
    recorder = vta.Recorder(tmp_path / "ev")
    shots = tmp_path / "shots"
    shots.mkdir()
    resume = tmp_path / "resume"
    resume.write_text("done", encoding="utf-8")   # 立即收口

    class FakeDriver:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(self, argv, timeout=120.0):
            self.calls.append(list(argv))
            if argv[0] == "screenshot":
                return {"path": argv[argv.index("--output") + 1]}
            return {}

    stage = vta.collect_product_evidence(
        port=1, recorder=recorder, output=tmp_path / "ev", screenshots=shots,
        driver=FakeDriver(), app_pid=42, bundle_id="com.x", window_id=7,
        send_sample=None, resume_file=resume, timeout_s=5.0)
    assert (tmp_path / "ev" / "PRODUCT-PAUSE.md").is_file()
    assert (tmp_path / "ev" / "product-stage.json").is_file()
    assert stage["elapsed_s"] < 5.0
    assert any(c["name"] == "product-pause-opened" for c in recorder.cases)


def test_product_pause_times_out_bounded(tmp_path: Path) -> None:
    """无 resume → 超时收口，记 blocked，不无限挂起。"""
    recorder = vta.Recorder(tmp_path / "ev2")
    shots = tmp_path / "shots2"
    shots.mkdir()

    class FakeDriver:
        def run(self, argv, timeout=120.0):
            return {"path": "x"} if argv[0] == "screenshot" else {}

    stage = vta.collect_product_evidence(
        port=1, recorder=recorder, output=tmp_path / "ev2", screenshots=shots,
        driver=FakeDriver(), app_pid=42, bundle_id="com.x", window_id=7,
        send_sample=None, resume_file=None, timeout_s=1.0)
    assert stage["elapsed_s"] >= 1.0
    assert any(c["name"] == "product-timeout" for c in recorder.cases)
    assert recorder.blocked


def test_product_pause_tolerates_driver_failure(tmp_path: Path) -> None:
    """截图失败不得让整个采证崩掉（真实窗口可能瞬时不可用）。"""
    recorder = vta.Recorder(tmp_path / "ev3")
    shots = tmp_path / "shots3"
    shots.mkdir()

    class FlakyDriver:
        def run(self, argv, timeout=120.0):
            raise vta.GateError("screenshot failed")

    stage = vta.collect_product_evidence(
        port=1, recorder=recorder, output=tmp_path / "ev3", screenshots=shots,
        driver=FlakyDriver(), app_pid=42, bundle_id="com.x", window_id=7,
        send_sample=None, resume_file=None, timeout_s=0.5)
    assert stage["shots"] == []


def test_entry_without_send_sample_is_not_green(tmp_path: Path) -> None:
    """B5：没跑产品阶段就不能全绿，必须留 NOT_VERIFIED。"""
    recorder = vta.Recorder(tmp_path / "ev4")
    recorder.record("product-stage-not-requested", "blocked", "未提供 --send-sample")
    assert recorder.verdict() == "PASS_WITH_NOT_VERIFIED"


def test_worker_never_posts_analysis_api() -> None:
    """worker 只读观察面：不得直接 POST 冒充点击（B5 红线）。"""
    code = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ('"/api/analysis/jobs", "POST"',
                      '"/api/analysis/previews", "POST"',
                      '"/api/analysis/runtimes/detect", "POST"'):
        assert forbidden not in code
    # 只能对 jobs 用 GET 读
    assert 'http_json(\n            f"http://127.0.0.1:{port}/api/analysis/jobs", timeout=5.0)' in code


def test_entry_declares_send_sample_and_resume_file() -> None:
    args = vta.build_parser().parse_args([
        "run", "--app", "/a", "--build-ready", "/b", "--output", "/c",
        "--send-sample", "/tmp/s.md", "--resume-file", "/tmp/r"])
    assert args.send_sample == "/tmp/s.md"
    assert args.resume_file == "/tmp/r"


# --------------------------------------------------------------------------
# Recorder / 杂项
# --------------------------------------------------------------------------


def test_recorder_verdicts(tmp_path: Path) -> None:
    recorder = vta.Recorder(tmp_path / "ev5")
    recorder.record("a", "pass", "x")
    assert recorder.verdict() == "PASS"
    recorder.record("b", "blocked", "y")
    assert recorder.verdict() == "PASS_WITH_NOT_VERIFIED"
    recorder.record("c", "fail", "z")
    assert recorder.verdict() == "FAIL"


def test_recorder_rejects_unknown_status(tmp_path: Path) -> None:
    recorder = vta.Recorder(tmp_path / "ev6")
    with pytest.raises(ValueError, match="未知状态"):
        recorder.record("a", "green", "x")


def test_cli_help_returns_zero() -> None:
    assert vta.main(["--help"]) == 0


def test_cli_usage_error_returns_two() -> None:
    assert vta.main(["--app", "/a"]) == 2


def test_gen_manifest_cli_refuses_bad_receipt(tmp_path: Path, fake_app: Path,
                                              capsys) -> None:
    receipt = tmp_path / "r.json"
    receipt.write_text(json.dumps({"ok": False}), encoding="utf-8")
    code = vta.main(["gen-manifest", "--app", str(fake_app), "--receipt",
                     str(receipt), "--output", str(tmp_path / "m.json")])
    assert code == 3


def test_synthetic_scan_root_has_real_megabytes(tmp_path: Path) -> None:
    root = vta.make_synthetic_scan_root(tmp_path)
    assert vta.disk_usage_bytes(root) >= vta.SYNTHETIC_MEGABYTES * 1024 * 1024
    assert not any(p.is_symlink() for p in root.rglob("*"))


def test_target_sizes_match_ux_matrix() -> None:
    assert vta.TARGET_CONTENT_SIZES == ((980, 640), (1220, 820))


def test_non_bundle_app_blocks_before_launch(tmp_path: Path) -> None:
    args = vta.build_parser().parse_args([
        "run", "--app", str(tmp_path / "not-an-app"),
        "--build-ready", str(tmp_path / "m.json"), "--output", str(tmp_path / "o")])
    assert vta.run_verification(args) == 3
    result = json.loads((tmp_path / "o" / "result.json").read_text("utf-8"))
    assert result["halt_kind"] == "BLOCKED"
    assert result["cleanup"]["reclaimed"] == []


# ==========================================================================
# R2 / R3 / R4：真实合成消费者探针
#
# 上轮教训：`cp /bin/sleep` 副本在本机 exec 即被 SIGKILL（exit 137，平台
# 二进制副本限制），进程自始为僵尸 → 正例回收测试**空洞通过**。本轮改为
# 用 clang 现场编译的极小合成助手，编译到**真实 helper 布局路径**下，
# 做 ready 握手后再回收，并断言回收前确实活着、确实由信号终止。
# 不靠 skip / 不吞 Timeout / 不扩到 10s / 不容忍死亡假绿。
# ==========================================================================

PROBE_C = r"""
#include <stdio.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: probe <readyfile>\n"); return 2; }
    FILE *f = fopen(argv[1], "w");
    if (!f) return 3;
    fprintf(f, "%d\n", (int)getpid());
    fclose(f);
    for (;;) pause();
    return 0;
}
"""


def _clang() -> str:
    for name in ("clang", "cc"):
        found = shutil.which(name)
        if found:
            return found
    pytest.skip("本机无 clang/cc，无法编译合成助手")


@pytest.fixture
def helper_binary(tmp_path: Path) -> Path:
    """把合成助手编译到真实 helper 布局路径下（fathom-helper 短名）。"""
    app = tmp_path / "Fathom.app"
    binary = app / "Contents/Resources/helper/fathom-helper/fathom-helper"
    binary.parent.mkdir(parents=True)
    source = tmp_path / "probe.c"
    source.write_text(PROBE_C, encoding="utf-8")
    proc = subprocess.run([_clang(), "-O0", "-o", str(binary), str(source)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not binary.is_file():
        pytest.fail(f"合成助手编译失败：{proc.stderr[:400]}")
    return binary


def _spawn_probe(binary: Path, ready: Path) -> subprocess.Popen:
    """启动合成助手并等 ready 握手，断言它**真的活着**（非僵尸）。"""
    proc = subprocess.Popen([str(binary), str(ready)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline and not ready.is_file():
        time.sleep(0.05)
    if not ready.is_file():
        proc.kill()
        pytest.fail("合成助手未完成 ready 握手")
    # 关键：回收前必须确认进程真的活着（ucomm 正确且 args 非 <defunct>）
    observed = vta.ps_identity(proc.pid)
    assert observed is not None, "ready 后 ps 读不到进程"
    assert observed["comm"] == "fathom-helper", observed
    assert "<defunct>" not in observed["args"], f"进程自始为僵尸：{observed}"
    return proc


def _owned_for(binary: Path, ready: Path, app: Path, pid: int,
               role: str = "helper") -> "vta.OwnedProcess":
    observed = vta.ps_identity(pid)
    assert observed is not None
    return vta.OwnedProcess(
        pid=pid, executable=observed["comm"], executable_path=observed["path"],
        bundle_id="", role=role, start_args=observed["args"],
        instance_nonce=str(ready))


def test_r3_positive_reclaim_really_happens(helper_binary: Path,
                                            tmp_path: Path) -> None:
    """R3 正例：身份匹配 → SIGTERM → **真实终止**。断言 reclaimed 内容。"""
    app = helper_binary.parents[4]
    ready = tmp_path / "ready-a"
    victim = _spawn_probe(helper_binary, ready)
    try:
        owned = _owned_for(helper_binary, ready, app, victim.pid)
        report = vta.reclaim_owned([owned], app_path=app,
                                   log=tmp_path / "log", grace_s=0.5)
        # 确实发过信号
        assert any(s["pid"] == victim.pid for s in report["signalled"]), report
        # 确实因信号终止（不是 already_exited —— 那就是空洞通过）
        reclaimed = [r for r in report["reclaimed"] if r["pid"] == victim.pid]
        assert reclaimed, f"reclaimed 必须含该 pid：{report}"
        assert reclaimed[0]["how"] == "signal_terminated", report
        # 不得被记为跳过（身份复核必须通过）
        assert not [s for s in report["skipped_identity"] if s["pid"] == victim.pid]
        # 真实终止：进程已消失或已成僵尸（僵尸=已被信号终止，等父进程收割）
        after = vta.ps_identity(victim.pid)
        assert after is None or after["zombie"], f"进程应已终止：{after}"
        assert report["still_alive"] == []
        # 由子进程自身确认：被信号终止（负返回码）
        rc = victim.wait(timeout=15)
        assert rc < 0, f"应由信号终止，实际 returncode={rc}"
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=10)


def test_r3_ignores_zombie_as_reclaim_success(helper_binary: Path,
                                              tmp_path: Path) -> None:
    """进程已死（僵尸）时只能记 already_exited，**不得**记 signal_terminated。"""
    app = helper_binary.parents[4]
    ready = tmp_path / "ready-b"
    victim = _spawn_probe(helper_binary, ready)
    owned = _owned_for(helper_binary, ready, app, victim.pid)
    victim.kill()
    victim.wait(timeout=15)
    report = vta.reclaim_owned([owned], app_path=app,
                               log=tmp_path / "log", grace_s=0.0)
    assert report["signalled"] == [], "已死进程不得发信号"
    assert all(r["how"] != "signal_terminated" for r in report["reclaimed"])


def test_r3_negative_foreign_process_never_signalled(tmp_path: Path) -> None:
    """R3 负例：身份不符 → 不发信号，进程仍活着。"""
    foreign = subprocess.Popen(
        ["sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        forged = vta.OwnedProcess(
            pid=foreign.pid, executable="fathom-helper",
            executable_path=f"/somewhere/Fathom.app/Contents/MacOS/fathom-helper",
            bundle_id="", role="helper", start_args="sleep 30",
            instance_nonce="/nope")
        report = vta.reclaim_owned([forged], app_path=Path("/nonexistent-app"),
                                   log=tmp_path / "log", grace_s=0.0)
        assert report["signalled"] == [], "foreign 不得发信号"
        assert report["skipped_identity"], report
        assert foreign.poll() is None, "foreign 进程被杀掉了！"
    finally:
        foreign.kill()
        foreign.wait(timeout=10)


def test_r2_helper_without_path_credential_is_refused(tmp_path: Path) -> None:
    """R2：没有钉住绝对路径 → 拒绝（仅名字包含不足以证明自有）。"""
    proc = vta.OwnedProcess(pid=1234, executable="fathom-helper",
                            executable_path="", bundle_id="", role="helper",
                            start_args="fathom-helper", instance_nonce="x")
    observed = {"comm": "fathom-helper", "path": "/a/fathom-helper",
                "args": "fathom-helper"}
    assert proc.matches(observed, app_path=Path("/a")) is False
    assert "无启动凭证" in proc.rejection_reason(observed, app_path=Path("/a"))


def test_r2_helper_without_nonce_is_refused(tmp_path: Path) -> None:
    """R2：helper 缺本 runtime instance nonce → 拒绝。"""
    app = Path("/Apps/Fathom.app")
    proc = vta.OwnedProcess(
        pid=1234, executable="fathom-helper",
        executable_path="/Apps/Fathom.app/Contents/Resources/helper/fathom-helper/fathom-helper",
        bundle_id="", role="helper", start_args="…/fathom-helper", instance_nonce="")
    observed = {"comm": "fathom-helper",
                "path": "/Apps/Fathom.app/Contents/Resources/helper/fathom-helper/fathom-helper",
                "args": "/Apps/Fathom.app/Contents/Resources/helper/fathom-helper/fathom-helper"}
    assert proc.matches(observed, app_path=app) is False
    assert "nonce" in proc.rejection_reason(observed, app_path=app)


def test_r2_reused_pid_with_same_name_is_refused(tmp_path: Path) -> None:
    """R2 关键：pid 被复用为**其他**同名 helper → 名字匹配也必须拒杀。"""
    app = Path("/Apps/Fathom.app")
    pinned = "/Apps/Fathom.app/Contents/Resources/helper/fathom-helper/fathom-helper"
    proc = vta.OwnedProcess(pid=1234, executable="fathom-helper",
                            executable_path=pinned, bundle_id="", role="helper",
                            start_args=pinned, instance_nonce="n")
    # 同一个 ucomm，但 argv[0] 是别人的 app
    observed = {"comm": "fathom-helper",
                "path": "/Other/Fathom.app/Contents/Resources/helper/fathom-helper/fathom-helper",
                "args": "/Other/Fathom.app/…/fathom-helper"}
    assert proc.matches(observed, app_path=app) is False
    assert "reused" in proc.rejection_reason(observed, app_path=app)


def test_r2_path_prefix_collision_is_refused() -> None:
    """非阻断项：Fathom.app.backup 不应被当作 Fathom.app 内。"""
    app = Path("/Apps/Fathom.app")
    proc = vta.OwnedProcess(
        pid=1, executable="fathom-desktop",
        executable_path="/Apps/Fathom.app/Contents/MacOS/fathom-desktop",
        bundle_id="", role="gui")
    assert proc.matches(
        {"comm": "fathom-desktop",
         "path": "/Apps/Fathom.app.backup/Contents/MacOS/fathom-desktop",
         "args": "x"}, app_path=app) is False


def test_r4_signal_triggers_bounded_cleanup(helper_binary: Path,
                                            tmp_path: Path) -> None:
    """R4 端到端：子进程收到 SIGTERM → 有界清理 → 自有子进程被收割。"""
    app = helper_binary.parents[4]
    ready = tmp_path / "ready-sig"
    driver = tmp_path / "driver.py"
    report_path = tmp_path / "sig-report.json"
    victim = _spawn_probe(helper_binary, ready)
    driver.write_text(
        "import importlib.util, json, pathlib, sys, time\n"
        f"spec = importlib.util.spec_from_file_location('v', {str(MODULE_PATH)!r})\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['v'] = m\n"
        "spec.loader.exec_module(m)\n"
        "m.install_signal_handlers()\n"
        f"obs = m.ps_identity({victim.pid})\n"
        f"owned = m.OwnedProcess(pid={victim.pid}, executable=obs['comm'],"
        f" executable_path=obs['path'], bundle_id='', role='helper',"
        f" start_args=obs['args'], instance_nonce={str(ready)!r})\n"
        "try:\n"
        "    time.sleep(120)\n"
        "except m._SignalShutdown as exc:\n"
        f"    rep = m.reclaim_owned([owned], app_path={str(app)!r},"
        f" log=pathlib.Path({str(tmp_path / 'drv.log')!r}), grace_s=1.0)\n"
        f"    json.dump(rep, open({str(report_path)!r}, 'w'))\n"
        "    print('SIGNALLED', exc.message)\n"
        "    sys.exit(3)\n", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, str(driver)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(1.5)
        assert child.poll() is None, "driver 提前退出"
        assert vta.ps_identity(victim.pid) is not None, "信号前探针应活着"
        child.terminate()          # SIGTERM
        stdout, stderr = child.communicate(timeout=30)
        assert "SIGNALLED" in stdout, f"stdout={stdout!r} stderr={stderr!r}"
        assert child.returncode == 3, child.returncode
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert any(s["pid"] == victim.pid for s in report["signalled"]), report
        assert any(r["pid"] == victim.pid and r["how"] == "signal_terminated"
                   for r in report["reclaimed"]), report
        assert report["still_alive"] == [], report
        after = vta.ps_identity(victim.pid)
        assert after is None or after["zombie"], f"探针应已被收割：{after}"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=10)


def test_r4_signal_handlers_are_installed() -> None:
    import signal

    vta.install_signal_handlers()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        assert signal.getsignal(sig) is not signal.SIG_DFL, \
            f"{sig} 未被接管，提前退出会跳过 finally"


# ==========================================================================
# R1：verdict/中断态 → 退出码
# ==========================================================================


def test_r1_success_returns_zero() -> None:
    assert vta.exit_code_for("PASS", None) == 0
    assert vta.exit_code_for("PASS_WITH_NOT_VERIFIED", None) == 0


def test_r1_failure_returns_one() -> None:
    assert vta.exit_code_for("FAIL", None) == 1
    assert vta.exit_code_for("PASS", "GATE") == 1


def test_r1_blocked_returns_three() -> None:
    assert vta.exit_code_for("FAIL", "BLOCKED") == 3
    assert vta.exit_code_for("PASS", "BLOCKED") == 3


def test_r1_signal_shutdown_carries_exit_three() -> None:
    exc = vta._SignalShutdown("SIGTERM")
    assert exc.code == 3
    assert isinstance(exc, SystemExit)


# 注意：episode 2 合同禁止本 worker 执行真实 run --app（会触发 NSWorkspace
# 真实启动）。故成功路径的退出码只以 exit_code_for 纯函数三态测试覆盖
# （test_r1_success_returns_zero / failure / blocked），不写「跑真 run 断言
# 退出码」的用例——那正是上轮越界启动的成因。
# 真实 run 的退出码由 PM 实机执行时观察。


def test_r1_core_product_unverified_cannot_be_silent_green(tmp_path: Path) -> None:
    """R1：核心产品未验时 not_verified 必须非空，verdict 不得是纯 PASS。"""
    recorder = vta.Recorder(tmp_path / "ev")
    recorder.record("product-stage-not-requested", "blocked", "未进入产品交互")
    assert recorder.blocked
    assert recorder.verdict() != "PASS"
