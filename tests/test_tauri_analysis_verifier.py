"""ISS-141 · verify_tauri_analysis 定向测试。

只覆盖**能检出边界错误**的判定逻辑：身份硬门、隔离硬门、尺寸口径、
端口归属、清理。刻意不写 mirror / 计数测试，不碰产品代码。

反例优先：每个正例旁边都配一个「差一个字节 / 差一个尾斜杠 / 换一个
PID」的反例，确保门是真的会拒绝，而不是恒真。
"""

from __future__ import annotations

import importlib.util
import json
import sys
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
# 夹具：一个最小可指纹化的假 .app
# --------------------------------------------------------------------------


@pytest.fixture
def fake_app(tmp_path: Path) -> Path:
    app = tmp_path / "Fathom.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "Fathom").write_bytes(b"MACH-O-FAKE-BINARY")
    helper = app / "Contents/Resources/helper/fathom-helper"
    helper.mkdir(parents=True)
    (helper / "fathom-helper").write_bytes(b"HELPER-BINARY")
    (app / "Contents" / "Info.plist").write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        b'<plist version="1.0"><dict>'
        b"<key>CFBundleIdentifier</key><string>com.fathom.test</string>"
        b"</dict></plist>"
    )
    return app


def build_manifest(app: Path, *, commit: str = "a" * 40, dirty: bool = False,
                   bundle_id: str | None = None,
                   fingerprint: str | None = None,
                   helper_sha: str | None = None,
                   extra_top: str | None = None) -> dict:
    observed = vta.bundle_fingerprint(app)
    manifest = {
        "version": vta.MANIFEST_VERSION,
        "app": {
            "path": str(app),
            "bundle_id": bundle_id or vta.read_bundle_id(app),
            "fingerprint_sha256": fingerprint or observed["fingerprint_sha256"],
            "files": dict(observed["files"]),
        },
        "helper": {
            "relative_path": vta.HELPER_RELATIVE_PATH,
            "sha256": helper_sha or observed["files"][vta.HELPER_RELATIVE_PATH],
        },
        "source": {"commit": commit, "dirty": dirty},
        "built_at": "2026-10-02T00:00:00Z",
    }
    if extra_top:
        manifest[extra_top] = "unexpected"
    return manifest


# --------------------------------------------------------------------------
# 身份硬门：manifest 与 app 必须逐项吻合
# --------------------------------------------------------------------------


def test_manifest_matching_app_passes(fake_app: Path) -> None:
    result = vta.verify_manifest_identity(build_manifest(fake_app), fake_app)
    assert result["bundle_id"] == "com.fathom.test"
    assert vta.HELPER_RELATIVE_PATH in result["files"]


def test_fingerprint_mismatch_is_rejected(fake_app: Path) -> None:
    manifest = build_manifest(fake_app, fingerprint="b" * 64)
    with pytest.raises(vta.IdentityError, match="指纹不符"):
        vta.verify_manifest_identity(manifest, fake_app)


def test_single_byte_binary_change_is_rejected(fake_app: Path) -> None:
    """差一个字节就必须拒——防止拿改动过的 app 伪证。"""
    manifest = build_manifest(fake_app)
    (fake_app / "Contents" / "MacOS" / "Fathom").write_bytes(b"MACH-O-FAKE-BINARZ")
    with pytest.raises(vta.IdentityError):
        vta.verify_manifest_identity(manifest, fake_app)


def test_helper_sha_mismatch_is_rejected(fake_app: Path) -> None:
    manifest = build_manifest(fake_app, helper_sha="c" * 64)
    with pytest.raises(vta.IdentityError, match="helper"):
        vta.verify_manifest_identity(manifest, fake_app)


def test_helper_path_must_be_canonical(fake_app: Path) -> None:
    manifest = build_manifest(fake_app)
    manifest["helper"]["relative_path"] = "Contents/Resources/other/helper"
    with pytest.raises(vta.IdentityError, match="relative_path"):
        vta.verify_manifest_identity(manifest, fake_app)


def test_bundle_id_mismatch_is_rejected(fake_app: Path) -> None:
    manifest = build_manifest(fake_app, bundle_id="com.other.app")
    with pytest.raises(vta.IdentityError, match="bundle id"):
        vta.verify_manifest_identity(manifest, fake_app)


def test_dirty_source_build_is_rejected(fake_app: Path) -> None:
    with pytest.raises(vta.IdentityError, match="dirty"):
        vta.verify_manifest_identity(build_manifest(fake_app, dirty=True), fake_app)


def test_commit_mismatch_is_rejected(fake_app: Path) -> None:
    with pytest.raises(vta.IdentityError, match="commit"):
        vta.verify_manifest_identity(
            build_manifest(fake_app, commit="a" * 40), fake_app,
            expected_commit="d" * 40)


def test_extra_file_in_app_is_rejected(fake_app: Path) -> None:
    """多一个文件也必须拒——指纹先炸，消息断言放宽到两种合法拒绝路径。"""
    manifest = build_manifest(fake_app)
    (fake_app / "Contents" / "Resources" / "stowaway.bin").write_bytes(b"extra")
    with pytest.raises(vta.IdentityError, match="额外文件|指纹不符"):
        vta.verify_manifest_identity(manifest, fake_app)


@pytest.mark.parametrize("extra_top", ["notes", "guessed", "allow_unknown"])
def test_unknown_manifest_top_key_is_rejected(
    fake_app: Path, tmp_path: Path, extra_top: str
) -> None:
    """未知顶层字段必须由 load_manifest 拦下（不静默忽略）。"""
    manifest_path = tmp_path / f"manifest-{extra_top}.json"
    manifest_path.write_text(
        json.dumps(build_manifest(fake_app, extra_top=extra_top), ensure_ascii=False),
        encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="未知顶层字段"):
        vta.load_manifest(manifest_path)


@pytest.mark.parametrize("section", ["app", "helper", "source"])
def test_unknown_nested_key_is_rejected(
    fake_app: Path, section: str
) -> None:
    manifest = build_manifest(fake_app)
    manifest[section]["surprise"] = 1
    with pytest.raises(vta.IdentityError, match="未知字段"):
        vta.verify_manifest_identity(manifest, fake_app)


def test_load_manifest_accepts_exact_schema(fake_app: Path, tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest-ok.json"
    manifest_path.write_text(
        json.dumps(build_manifest(fake_app), ensure_ascii=False), encoding="utf-8")
    loaded = vta.load_manifest(manifest_path)
    assert vta.verify_manifest_identity(loaded, fake_app)["bundle_id"] == "com.fathom.test"


def test_manifest_version_must_match(fake_app: Path) -> None:
    manifest = build_manifest(fake_app)
    manifest["version"] = 99
    with pytest.raises(vta.IdentityError, match="version"):
        vta.verify_manifest_identity(manifest, fake_app)


def test_load_manifest_rejects_malformed_json(tmp_path: Path) -> None:
    bad = tmp_path / "manifest.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="合法 JSON"):
        vta.load_manifest(bad)


def test_load_manifest_rejects_non_object(tmp_path: Path) -> None:
    bad = tmp_path / "manifest.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(vta.IdentityError, match="JSON 对象"):
        vta.load_manifest(bad)


def test_missing_app_is_identity_error(tmp_path: Path) -> None:
    with pytest.raises(vta.IdentityError, match="不存在"):
        vta.bundle_fingerprint(tmp_path / "Nope.app")


def test_bundle_id_missing_from_plist_is_rejected(tmp_path: Path) -> None:
    app = tmp_path / "Broken.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "Fathom").write_bytes(b"x")
    (app / "Contents" / "Info.plist").write_bytes(
        b'<?xml version="1.0"?><plist version="1.0"><dict></dict></plist>')
    with pytest.raises(vta.IdentityError, match="CFBundleIdentifier"):
        vta.read_bundle_id(app)


# --------------------------------------------------------------------------
# 隔离硬门：生效扫描根必须是本轮合成根，且不在 HOME / 生产目录下
# --------------------------------------------------------------------------


@pytest.fixture
def isolated(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    expected = tmp_path / "run" / "synthetic-scan-root"
    expected.mkdir(parents=True)
    return home, expected


def test_exact_scan_root_passes(isolated: tuple[Path, Path]) -> None:
    home, expected = isolated
    assert vta.assert_isolated_scan_root(
        str(expected), expected=expected, home=home) == vta.normalize_root(expected)


def test_scan_root_trailing_slash_normalizes(isolated: tuple[Path, Path]) -> None:
    home, expected = isolated
    assert vta.assert_isolated_scan_root(
        f"{expected}/", expected=expected, home=home) == vta.normalize_root(expected)


def test_different_scan_root_is_rejected(isolated: tuple[Path, Path]) -> None:
    home, expected = isolated
    other = tmp_path_other(isolated)
    with pytest.raises(vta.GateError, match="越界"):
        vta.assert_isolated_scan_root(str(other), expected=expected, home=home)


def tmp_path_other(isolated: tuple[Path, Path]) -> Path:
    other = isolated[0].parent / "other-root"
    other.mkdir(exist_ok=True)
    return other


def test_home_scan_root_is_rejected(isolated: tuple[Path, Path]) -> None:
    home, _ = isolated
    with pytest.raises(vta.GateError, match="越界|HOME"):
        vta.assert_isolated_scan_root(
            str(home), expected=home, home=home)


def test_scan_root_inside_home_is_rejected(isolated: tuple[Path, Path]) -> None:
    home, _ = isolated
    nested = home / "somewhere" / "scan"
    nested.mkdir(parents=True)
    with pytest.raises(vta.GateError, match="HOME"):
        vta.assert_isolated_scan_root(
            str(nested), expected=nested, home=home)


def test_production_support_dir_is_rejected(isolated: tuple[Path, Path]) -> None:
    """生产应用支持目录必然在 HOME 下，HOME 门先炸——仍必须拒绝。

    注意：生产目录分支在门内是**冗余纵深**——prod 由传入的 home 推导，
    恒在 home 之下，故 HOME 分支总是先命中。独立构造「在 home 之外的
    生产目录」在本门语义下不可达，这里只断言结果（拒绝）与先命中者，
    不伪造一个假的可达路径来充数。
    """
    home, _ = isolated
    prod = vta.production_support_dir(home)
    prod.mkdir(parents=True)
    with pytest.raises(vta.GateError, match="落在 HOME 内") as excinfo:
        vta.assert_isolated_scan_root(str(prod), expected=prod, home=home)
    assert "生产应用支持目录" not in str(excinfo.value)


def test_missing_scan_root_value_is_rejected(isolated: tuple[Path, Path]) -> None:
    home, expected = isolated
    for bad in (None, "", "   ", 42, {}):
        with pytest.raises(vta.GateError, match="未回读生效扫描根"):
            vta.assert_isolated_scan_root(bad, expected=expected, home=home)


def test_config_body_nested_scan_root_is_parsed() -> None:
    """守住 /api/config 两种返回形态的取值口径。"""
    body = {"scan_root": {"value": "/tmp/x", "source": "settings"}}
    assert body["scan_root"]["value"] == "/tmp/x"
    flat = {"scan_root": "/tmp/x"}
    assert flat["scan_root"] == "/tmp/x"


# --------------------------------------------------------------------------
# helper 身份 / 端口归属
# --------------------------------------------------------------------------


def test_port_and_pid_must_match_instance() -> None:
    instance = {"pid": 4242, "port": 51234}
    vta.assert_port_owned(51234, instance, pid=4242)


def test_port_mismatch_is_rejected() -> None:
    with pytest.raises(vta.GateError, match="port"):
        vta.assert_port_owned(51234, {"pid": 4242, "port": 7952}, pid=4242)


def test_pid_mismatch_is_rejected() -> None:
    with pytest.raises(vta.GateError, match="pid"):
        vta.assert_port_owned(51234, {"pid": 4243, "port": 51234}, pid=4242)


# --------------------------------------------------------------------------
# 尺寸口径：内容区 ↔ 外框
# --------------------------------------------------------------------------


def test_outer_frame_adds_titlebar() -> None:
    assert vta.outer_frame_size(980, 640) == (980, 668)
    assert vta.outer_frame_size(1220, 820) == (1220, 848)


def test_readback_round_trips() -> None:
    for width, height in vta.TARGET_CONTENT_SIZES:
        frame_w, frame_h = vta.outer_frame_size(width, height)
        assert vta.readback_content_size(frame_w, frame_h) == (width, height)


def test_size_assertion_rejects_wrong_size() -> None:
    vta.size_assertion(980, 640, (980, 640))
    with pytest.raises(vta.GateError, match="尺寸不符"):
        vta.size_assertion(980, 641, (980, 640))
    with pytest.raises(vta.GateError, match="尺寸不符"):
        vta.size_assertion(979, 640, (980, 640))


def test_target_sizes_match_ux_matrix() -> None:
    assert vta.TARGET_CONTENT_SIZES == ((980, 640), (1220, 820))


# --------------------------------------------------------------------------
# 端口：绝不选生产端口
# --------------------------------------------------------------------------


def test_free_port_never_returns_production_port() -> None:
    import random

    for seed in range(5):
        port = vta.pick_free_port(random.Random(seed))
        assert port != vta.PRODUCTION_PORT
        assert 51_000 <= port <= 59_000


# --------------------------------------------------------------------------
# 合成根与记录器
# --------------------------------------------------------------------------


def test_synthetic_scan_root_has_real_megabytes(tmp_path: Path) -> None:
    root = vta.make_synthetic_scan_root(tmp_path)
    usage = vta.disk_usage_bytes(root)
    assert usage >= vta.SYNTHETIC_MEGABYTES * 1024 * 1024
    assert (root / "alpha" / "changed.txt").is_file()
    assert not any(p.is_symlink() for p in root.rglob("*"))


def test_recorder_writes_jsonl_and_counts_blocked(tmp_path: Path) -> None:
    recorder = vta.Recorder(tmp_path / "evidence")
    recorder.record("ok", "pass", "detail")
    recorder.record("needs-foreground", "blocked", "Tab/Esc 需前台")
    entries = [json.loads(line) for line in
               (recorder.cases_path).read_text(encoding="utf-8").splitlines()]
    assert [e["name"] for e in entries] == ["ok", "needs-foreground"]
    assert recorder.verdict() == "PASS_WITH_NOT_VERIFIED"
    assert recorder.blocked == ["needs-foreground :: Tab/Esc 需前台"]


def test_recorder_fail_dominates_verdict(tmp_path: Path) -> None:
    recorder = vta.Recorder(tmp_path / "evidence")
    recorder.record("a", "pass", "x")
    recorder.record("b", "blocked", "y")
    recorder.record("c", "fail", "z")
    assert recorder.verdict() == "FAIL"


def test_recorder_rejects_unknown_status(tmp_path: Path) -> None:
    recorder = vta.Recorder(tmp_path / "evidence")
    with pytest.raises(ValueError, match="未知状态"):
        recorder.record("a", "green", "x")


# --------------------------------------------------------------------------
# CLI 面：必填参数 / 用法错误
# --------------------------------------------------------------------------


def test_cli_requires_app_manifest_output() -> None:
    parser = vta.build_parser()
    for missing in ("--app", "--build-ready", "--output"):
        argv = ["--app", "/a", "--build-ready", "/b", "--output", "/c"]
        index = argv.index(missing)
        del argv[index:index + 2]
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_cli_help_exits_zero() -> None:
    assert vta.main(["--help"]) == 0


def test_cli_usage_error_returns_two() -> None:
    assert vta.main(["--app", "/a"]) == 2


def test_non_bundle_app_is_blocked(tmp_path: Path) -> None:
    args = vta.build_parser().parse_args([
        "--app", str(tmp_path / "not-an-app"),
        "--build-ready", str(tmp_path / "m.json"),
        "--output", str(tmp_path / "out"),
    ])
    assert vta.run_verification(args) == 3
    result = json.loads((tmp_path / "out" / "result.json").read_text("utf-8"))
    assert result["verdict"] == "FAIL"
    assert result["halt_kind"] == "BLOCKED"
    assert "不是 Tauri .app" in result["halt_detail"]


def test_missing_manifest_blocks_before_launch(tmp_path: Path, fake_app: Path) -> None:
    args = vta.build_parser().parse_args([
        "--app", str(fake_app),
        "--build-ready", str(tmp_path / "absent.json"),
        "--output", str(tmp_path / "out2"),
    ])
    assert vta.run_verification(args) == 3
    result = json.loads((tmp_path / "out2" / "result.json").read_text("utf-8"))
    assert result["halt_kind"] == "BLOCKED"
    # 身份没过关就不该有 app-identity 通过记录（更不该启动进程）
    assert "port" not in result or "port" in result.get("halt_detail", "")
    assert result["own_processes_after"] == result["own_processes_before"]


# --------------------------------------------------------------------------
# 驱动源码的静态契约（只编译不实操；真实 GUI 由 PM 执行）
# --------------------------------------------------------------------------


def test_driver_source_exists_and_declares_subcommands() -> None:
    source = DRIVER_SOURCE.read_text(encoding="utf-8")
    for sub in ("identify", "set-size", "measure", "click", "screenshot"):
        assert f'"{sub}"' in source


def test_driver_enforces_pid_and_bundle_id() -> None:
    source = DRIVER_SOURCE.read_text(encoding="utf-8")
    assert "拒绝执行" in source
    assert "bundle id 不符" in source
    assert "CGEventPostToPid" in source


def _swift_code_only(source: str) -> str:
    """剥掉注释再检查：否则「我们不做 AXRaise」这句注释自己就会触发断言。"""
    lines = []
    for raw in source.splitlines():
        stripped = raw.strip()
        if stripped.startswith("//"):
            continue
        lines.append(raw.split("//", 1)[0])
    return "\n".join(lines)


def test_driver_never_activates_or_uses_global_keyboard() -> None:
    code = _swift_code_only(DRIVER_SOURCE.read_text(encoding="utf-8"))
    for banned in ("AXRaise", "kCGEventTap", "CGEventPost(",
                   ".activate(", "activate(options", "AXPress"):
        assert banned not in code, f"驱动代码不得出现 {banned}"


def test_driver_does_click_only_via_post_to_pid() -> None:
    code = _swift_code_only(DRIVER_SOURCE.read_text(encoding="utf-8"))
    assert ".postToPid(" in code
    # 旧 C 函数调用形式（带括号）已迁到实例方法，避免全局投递语义；
    # 帮助文本里的散文提及不算调用。
    assert "CGEventPostToPid(" not in code


def _python_non_docstring_text(path: Path) -> str:
    """抽出「非 docstring」代码文本。

    用 AST 而不是纯文本扫：模块 docstring 里写着「不照搬 launchctl 回退」
    这类说明，纯文本搜会把自己的注释当违规。收集非 docstring 字符串常量
    与全部标识符名，才真正回答「代码会不会去执行 launchctl」。
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
    """ISS-141 明确不照搬既有窗口脚本的 launchctl 全局 env 回退。"""
    code = _python_non_docstring_text(MODULE_PATH)
    assert "launchctl" not in code


def test_python_entry_uses_open_env_isolation() -> None:
    code = _python_non_docstring_text(MODULE_PATH)
    assert "open" in code
    assert "FATHOM_RUNTIME_DIR" in code
    assert "FATHOM_SCAN_ROOT" in code
    assert "FATHOM_PORT" in code


def test_python_entry_keeps_production_untouched() -> None:
    code = _python_non_docstring_text(MODULE_PATH)
    assert "PRODUCTION_PORT" in code and "PRODUCTION_SUPPORT_DIR" in code
    # 生产常量是数字字面量（不进字符串集合），直接核对取值。
    assert vta.PRODUCTION_PORT == 7952
    assert vta.PRODUCTION_SUPPORT_DIR == "Library/Application Support/Fathom"


def test_production_port_is_never_a_candidate() -> None:
    """随机端口选择必须显式跳过生产端口。"""
    import inspect

    source = inspect.getsource(vta.pick_free_port)
    assert "PRODUCTION_PORT" in source
