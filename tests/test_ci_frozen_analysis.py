"""ISS-140：Intel 原生冻结 helper 解读回归流水线控制入口的聚焦单测（全离线）。

被测对象是 scripts/ci_frozen_analysis.py（CI 控制入口）与
.github/workflows/frozen-analysis.yml（Intel 原生流水线）。本测试钉住：

1. Mach-O 头解析：x86_64/arm64 判定、魔数/字节序/截断输入 fail-closed；
2. manifest 正负例：成功 manifest 记录 ok/source_sha/helper_sha256(64hex)/
   arch/version；helper 缺失、架构不符、版本不符、--version 非 JSON、
   source_sha 非 40 位 hex 都不得产生 ok=true 的 manifest（冻结失败
   不得发布成功 manifest）；
3. run 编排：build_helper.sh → 身份 manifest → 显式把 manifest 传给
   verify_frozen_analysis.py（--helper/--output/--build-ready 接线）；
   构建失败保留原退出码且不调用 verify、不产生 ok=true manifest；
   verify 失败原退出码透传（manifest 保持真实构建事实 ok=true）；
4. workflow 合同：合并前可验证触发（限定路径、目标 main 的 pull_request
   检出 PR head SHA=固定 head；另留 workflow_dispatch 供合并后复跑；不挂
   push 避免双重重复）、macos-15-intel 原生断言 uname -m=x86_64、
   contents: read、无 secrets.*、无容错续跑标记、第三方 action 全 40 位
   SHA、锁定依赖安装走 constraints、调用本入口；
5. 真实入口行为：--help 退出 0；真实失败路径（helper 缺失）非零退出且
   不产生成功 manifest（不 freeze、不装依赖，worker 本机合同内可跑）。

测试不访问网络、不安装依赖、不真实 freeze：所有子进程调用经
monkeypatch 替换；对真实入口的两个子进程检查只用 --help 与
helper 缺失路径（不触碰冻结工具链）。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ci_frozen_analysis as cfa  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "ci_frozen_analysis.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "frozen-analysis.yml"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_frozen_analysis.py"

BASE_SHA = "cbae70429d7c2c6827bb7be0dda84c3b2d7e775b"

# Mach-O 64 位小头魔数 + cputype（CPU_TYPE_X86_64=0x01000007，
# CPU_TYPE_ARM64=0x0100000c），与 PyInstaller onedir 原生产物一致。
MH_MAGIC_64 = b"\xcf\xfa\xed\xfe"


def fake_macho(cputype: int, size: int = 4096) -> bytes:
    import struct
    return MH_MAGIC_64 + struct.pack("<I", cputype) + b"\x00" * (size - 8)


X86_64_BYTES = fake_macho(0x01000007)
ARM64_BYTES = fake_macho(0x0100000C)


def write_helper(path: Path, blob: bytes, executable: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    if executable:
        path.chmod(0o755)
    return path


def repo_version() -> str:
    """真实单一版本源（manifest 正例的版本必须与它一致）。"""
    return cfa.single_source_version(REPO_ROOT)


def version_json(version: str) -> str:
    return json.dumps({"service": "fathom", "protocol_version": 1,
                       "version": version, "python": "3.14.6",
                       "machine": "x86_64", "exe": "fathom-helper"})


class FakeRun:
    """按 argv[0] 名分派的 subprocess.run 替身。

    handlers: {basename(argv[0]) -> callable(cmd, **kw) -> CompletedProcess}
    calls:    记录每次调用 (cmd, kwargs) 供断言
    """

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kw):  # noqa: ANN001, ANN003
        import subprocess as real_sp
        self.calls.append((list(cmd), dict(kw)))
        name = Path(cmd[0]).name
        handler = self.handlers.get(name)
        if handler is None:
            return real_sp.CompletedProcess(cmd, 127, stdout="", stderr="")
        return handler(cmd, **kw)

    def called(self, marker: str) -> list[list[str]]:
        return [cmd for cmd, _ in self.calls
                if any(marker in str(a) for a in cmd)]


@pytest.fixture()
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fr = FakeRun()
    monkeypatch.setattr(cfa.subprocess, "run", fr)
    return fr


# ---- 1. Mach-O 头解析 ------------------------------------------------------


def test_parse_macho_arch_native_headers() -> None:
    assert cfa.parse_macho_arch(X86_64_BYTES) == "x86_64"
    assert cfa.parse_macho_arch(ARM64_BYTES) == "arm64"


@pytest.mark.parametrize("blob", [
    b"",                                     # 空文件
    MH_MAGIC_64,                             # 只有魔数，缺 cputype
    b"\x7fELF\x02\x01\x01" + b"\x00" * 32,   # 非 Mach-O（ELF）
    b"\xce\xfa\xed\xfe" + b"\x00" * 32,      # 32 位魔数
    b"\xfe\xed\xfa\xcf" + b"\x00" * 32,      # 大端字节序（非本机）
])
def test_parse_macho_arch_rejects_non_native(blob: bytes) -> None:
    assert cfa.parse_macho_arch(blob) is None


def test_parse_macho_arch_rejects_unknown_cputype() -> None:
    assert cfa.parse_macho_arch(fake_macho(0x0000000C)) is None  # 32 位 arm
    assert cfa.parse_macho_arch(fake_macho(0x0100000A)) is None  # 未知


# ---- 2. manifest 子命令：正例 ----------------------------------------------


def test_manifest_positive_full_identity(tmp_path: Path,
                                          fake_run: FakeRun) -> None:
    helper = write_helper(tmp_path / "helper" / "fathom-helper", X86_64_BYTES)
    fake_run.handlers["fathom-helper"] = lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=version_json(repo_version()) + "\n", stderr="")
    fake_run.handlers["file"] = lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=f"{cmd[1]}: Mach-O 64-bit executable x86_64", stderr="")
    out = tmp_path / "build-ready.json"

    rc = cfa.main(["manifest", "--helper", str(helper), "--out", str(out),
                   "--source-sha", BASE_SHA, "--repo-root", str(REPO_ROOT),
                   "--expected-arch", "x86_64"])

    assert rc == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["ok"] is True
    assert manifest["source_sha"] == BASE_SHA
    assert manifest["arch"] == "x86_64"
    assert manifest["version"] == repo_version()
    assert manifest["helper_sha256"] == hashlib.sha256(X86_64_BYTES).hexdigest()
    assert len(manifest["helper_sha256"]) == 64
    # 架构判定以 Mach-O 头为执法源，file(1) 输出仅作旁证记录
    assert "x86_64" in manifest["file_output"]


def test_manifest_sha256_is_real_file_digest(tmp_path: Path,
                                             fake_run: FakeRun) -> None:
    """指纹必须是 helper 文件真实字节的 SHA256（不是占位或路径哈希）。"""
    blob = X86_64_BYTES + b"iss140-payload-marker"
    helper = write_helper(tmp_path / "h", blob)
    fake_run.handlers["h"] = lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=version_json(repo_version()) + "\n", stderr="")
    out = tmp_path / "m.json"
    rc = cfa.main(["manifest", "--helper", str(helper), "--out", str(out),
                   "--source-sha", BASE_SHA, "--repo-root", str(REPO_ROOT)])
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["helper_sha256"] == \
        hashlib.sha256(blob).hexdigest()


# ---- 2. manifest 子命令：负例（不得产生成功 manifest） --------------------


def run_manifest_expect_failure(tmp_path: Path, fake_run: FakeRun,
                                 helper: Path, *, source_sha: str = BASE_SHA,
                                 version_out: str | None = None,
                                 expected_arch: str = "x86_64") -> tuple:
    if version_out is not None:
        fake_run.handlers[helper.name] = lambda cmd, **kw: (
            subprocess.CompletedProcess(cmd, 0, stdout=version_out, stderr="")
            if version_out is not None else subprocess.CompletedProcess(cmd, 1))
    fake_run.handlers.setdefault("file", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="file: cannot open", stderr=""))
    out = tmp_path / "build-ready.json"
    argv = ["manifest", "--helper", str(helper), "--out", str(out),
            "--source-sha", source_sha, "--repo-root", str(REPO_ROOT),
            "--expected-arch", expected_arch]
    rc = cfa.main(argv)
    manifest: dict = {}
    if out.exists():
        manifest = json.loads(out.read_text(encoding="utf-8"))
    return rc, manifest


def test_manifest_missing_helper_no_success_manifest(tmp_path: Path,
                                                     fake_run: FakeRun) -> None:
    rc, manifest = run_manifest_expect_failure(
        tmp_path, fake_run, tmp_path / "nope" / "fathom-helper")
    assert rc != 0
    assert manifest.get("ok") is not True
    assert "helper" in manifest.get("reason", "")


def test_manifest_arch_mismatch_negative(tmp_path: Path,
                                         fake_run: FakeRun) -> None:
    """arm64 产物在期望 x86_64 的流水线上必须拒绝（arm 本机不冒充 Intel）。"""
    helper = write_helper(tmp_path / "h", ARM64_BYTES)
    rc, manifest = run_manifest_expect_failure(
        tmp_path, fake_run, helper, version_out=version_json(repo_version()))
    assert rc != 0
    assert manifest.get("ok") is not True
    assert "arm64" in manifest.get("reason", "") or \
        "x86_64" in manifest.get("reason", "")


def test_manifest_version_mismatch_negative(tmp_path: Path,
                                            fake_run: FakeRun) -> None:
    helper = write_helper(tmp_path / "h", X86_64_BYTES)
    rc, manifest = run_manifest_expect_failure(
        tmp_path, fake_run, helper,
        version_out=version_json("9.9.9"))  # 与单一版本源不符
    assert rc != 0
    assert manifest.get("ok") is not True


def test_manifest_version_not_json_negative(tmp_path: Path,
                                            fake_run: FakeRun) -> None:
    helper = write_helper(tmp_path / "h", X86_64_BYTES)
    rc, manifest = run_manifest_expect_failure(
        tmp_path, fake_run, helper, version_out="not-json\n")
    assert rc != 0
    assert manifest.get("ok") is not True


def test_manifest_invalid_source_sha_rejected(tmp_path: Path,
                                              fake_run: FakeRun) -> None:
    helper = write_helper(tmp_path / "h", X86_64_BYTES)
    fake_run.handlers["h"] = lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=version_json(repo_version()) + "\n", stderr="")
    rc, manifest = run_manifest_expect_failure(
        tmp_path, fake_run, helper, source_sha="abc123")
    assert rc != 0
    assert manifest.get("ok") is not True


# ---- 3. run 子命令：编排与 fail-closed ------------------------------------


def make_fake_repo(tmp_path: Path) -> Path:
    """最小假仓库：单一版本源 + 两个被复用脚本的存在占位。"""
    (tmp_path / "fathom").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fathom" / "__init__.py").write_text(
        '__version__ = "0.3.6"\n', encoding="utf-8")
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "build_helper.sh").write_text("#!/bin/bash\n",
                                                          encoding="utf-8")
    (tmp_path / "scripts" / "verify_frozen_analysis.py").write_text(
        "# placeholder\n", encoding="utf-8")
    return tmp_path


def install_success_fakes(fake_run: FakeRun, repo: Path,
                          verify_rc: int = 0) -> dict:
    helper_rel = cfa.HELPER_SUBPATH
    state: dict = {"verify_manifest_at_call": None}

    def build(cmd, **kw):  # noqa: ANN001, ANN202
        write_helper(repo / helper_rel, X86_64_BYTES)
        return subprocess.CompletedProcess(cmd, 0,
                                           stdout="[build_helper] OK\n", stderr="")

    def version(cmd, **kw):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(
            cmd, 0, stdout=version_json("0.3.6") + "\n", stderr="")

    def verify(cmd, **kw):  # noqa: ANN001, ANN202
        manifest_arg = cmd[cmd.index("--build-ready") + 1]
        state["verify_manifest_at_call"] = json.loads(
            Path(manifest_arg).read_text(encoding="utf-8"))
        out_dir = Path(cmd[cmd.index("--output") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(
            json.dumps({"status": "PASSED"}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, verify_rc, stdout="", stderr="")

    def git(cmd, **kw):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(cmd, 0, stdout=BASE_SHA + "\n", stderr="")

    fake_run.handlers.update({
        "bash": build, "fathom-helper": version,
        "python": verify, "git": git,
        "file": lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout="Mach-O 64-bit executable x86_64", stderr=""),
    })
    return state


def test_run_success_wires_manifest_into_verify(tmp_path: Path,
                                                fake_run: FakeRun) -> None:
    repo = make_fake_repo(tmp_path / "repo")
    work = tmp_path / "work"
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (venv / "bin" / "python").chmod(0o755)
    state = install_success_fakes(fake_run, repo)

    rc = cfa.main(["run", "--venv", str(venv), "--work-dir", str(work),
                   "--repo-root", str(repo), "--expected-arch", "x86_64"])

    assert rc == 0
    manifest_path = work / "build-ready.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["ok"] is True and manifest["source_sha"] == BASE_SHA
    # verify 收到的就是这份 manifest（显式传 --build-ready，当时已是 ok=true）
    assert state["verify_manifest_at_call"] == manifest
    verify_cmd = fake_run.called("verify_frozen_analysis.py")[0]
    assert verify_cmd[0] == str(venv / "bin" / "python")
    for flag in ("--helper", "--output", "--build-ready"):
        assert flag in verify_cmd
    summary = json.loads((work / "ci-summary.json").read_text(encoding="utf-8"))
    assert summary["stages"]["build"]["exit"] == 0
    assert summary["stages"]["verify"]["exit"] == 0


def test_run_build_failure_keeps_exit_and_blocks_verify(
        tmp_path: Path, fake_run: FakeRun) -> None:
    repo = make_fake_repo(tmp_path / "repo")
    work = tmp_path / "work"
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    def build_fail(cmd, **kw):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="[build_helper] FAIL：冻结失败\n")

    def git(cmd, **kw):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(cmd, 0, stdout=BASE_SHA + "\n", stderr="")

    fake_run.handlers.update({"bash": build_fail, "git": git})

    rc = cfa.main(["run", "--venv", str(venv), "--work-dir", str(work),
                   "--repo-root", str(repo)])

    assert rc == 1  # 原退出码保留
    manifest = json.loads((work / "build-ready.json").read_text(encoding="utf-8"))
    assert manifest.get("ok") is False
    assert manifest.get("stage") == "build"
    # 冻结失败：verify 绝不被调用
    assert fake_run.called("verify_frozen_analysis.py") == []
    summary = json.loads((work / "ci-summary.json").read_text(encoding="utf-8"))
    assert summary["stages"]["build"]["exit"] == 1


def test_run_build_blocked_exit_three_propagates(tmp_path: Path,
                                                 fake_run: FakeRun) -> None:
    repo = make_fake_repo(tmp_path / "repo")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    def build_blocked(cmd, **kw):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(
            cmd, 3, stdout="", stderr="[build_helper] BLOCKED：venv 未就绪\n")

    fake_run.handlers.update({
        "bash": build_blocked,
        "git": lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=BASE_SHA + "\n", stderr=""),
    })

    rc = cfa.main(["run", "--venv", str(venv), "--work-dir", str(tmp_path / "w"),
                   "--repo-root", str(repo)])
    assert rc == 3
    assert fake_run.called("verify_frozen_analysis.py") == []


def test_run_verify_failure_propagates_exit(tmp_path: Path,
                                            fake_run: FakeRun) -> None:
    """verify 失败原退出码透传；构建事实为真，manifest 不因此改写为 ok=false。"""
    repo = make_fake_repo(tmp_path / "repo")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    install_success_fakes(fake_run, repo, verify_rc=1)

    rc = cfa.main(["run", "--venv", str(venv), "--work-dir", str(tmp_path / "w"),
                   "--repo-root", str(repo)])
    assert rc == 1
    manifest = json.loads(
        (tmp_path / "w" / "build-ready.json").read_text(encoding="utf-8"))
    assert manifest["ok"] is True
    summary = json.loads(
        (tmp_path / "w" / "ci-summary.json").read_text(encoding="utf-8"))
    assert summary["stages"]["verify"]["exit"] == 1


def test_run_identity_failure_after_build_blocks_verify(
        tmp_path: Path, fake_run: FakeRun) -> None:
    """构建成功但产物架构不符：manifest ok=false、verify 不跑（负例回退）。"""
    repo = make_fake_repo(tmp_path / "repo")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    def build_arm(cmd, **kw):  # noqa: ANN001, ANN202
        write_helper(repo / cfa.HELPER_SUBPATH, ARM64_BYTES)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    fake_run.handlers.update({
        "bash": build_arm,
        "git": lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=BASE_SHA + "\n", stderr=""),
        "fathom-helper": lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=version_json("0.3.6") + "\n", stderr=""),
    })

    rc = cfa.main(["run", "--venv", str(venv), "--work-dir", str(tmp_path / "w"),
                   "--repo-root", str(repo), "--expected-arch", "x86_64"])
    assert rc == 1
    manifest = json.loads(
        (tmp_path / "w" / "build-ready.json").read_text(encoding="utf-8"))
    assert manifest.get("ok") is False
    assert manifest.get("stage") == "identity"
    assert fake_run.called("verify_frozen_analysis.py") == []


# ---- 4. workflow 合同（.github/workflows/frozen-analysis.yml） -------------


def workflow_text() -> str:
    assert WORKFLOW.is_file(), f"缺少 {WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_intel_native_and_trigger_contract() -> None:
    text = workflow_text()
    assert "runs-on: macos-15-intel" in text
    assert '[ "$(uname -m)" = "x86_64" ]' in text
    # 合并前可验证：限定路径、目标 main 的 pull_request（workflow_dispatch
    # 要求文件先在 default branch，无法单独承担合并前 Intel 验收）
    assert re.search(r"^  pull_request:", text, re.M)
    assert "branches: [main]" in text
    assert re.search(r"^\s+paths:", text, re.M)
    assert ".github/workflows/frozen-analysis.yml" in text
    assert "workflow_dispatch:" in text
    # 不挂 push 触发：避免 push+PR 双重重复消耗 Intel 分钟
    assert re.search(r"^\s+push:", text, re.M) is None
    # 被测固定 head：PR 事件显式检出 PR head SHA（source_sha 与实测同源）
    assert "github.event.pull_request.head.sha" in text


def test_workflow_minimal_permissions_no_secret_no_skip() -> None:
    text = workflow_text()
    assert "contents: read" in text
    assert "secrets." not in text
    assert "continue-on-error" not in text
    assert "persist-credentials: 'false'" in text


def test_workflow_third_party_actions_full_sha() -> None:
    text = workflow_text()
    uses_lines = re.findall(r"^\s*-?\s*uses:\s*(\S+)\s*(?:#.*)?$", text, re.M)
    assert len(uses_lines) >= 2, "至少 checkout + setup-python 两个 action"
    for ref in uses_lines:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), \
            f"第三方 action 未固定 40 位完整 SHA：{ref}"
    # 与 ci.yml 同源的两个官方 action（沿现 CI 已验证 SHA，不猜新 SHA）
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in text
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in text


def test_workflow_pinned_env_and_locked_install() -> None:
    text = workflow_text()
    m_new = re.search(r'PYTHON_VERSION:\s*"([^"]+)"', text)
    m_ci = re.search(r'PYTHON_VERSION:\s*"([^"]+)"',
                     (REPO_ROOT / ".github" / "workflows" / "ci.yml")
                     .read_text(encoding="utf-8"))
    assert m_new and m_ci and m_new.group(1) == m_ci.group(1), \
        "Python 版本必须与 ci.yml 同口径"
    assert "-r packaging/requirements-runtime.txt" in text
    assert "-r packaging/requirements-runtime-build.txt" in text  # pyinstaller pin
    assert "-c packaging/constraints.txt" in text


def test_workflow_calls_ci_entry_with_expected_arch() -> None:
    text = workflow_text()
    assert "scripts/ci_frozen_analysis.py run" in text
    for flag in ("--venv", "--work-dir", "--expected-arch x86_64"):
        assert flag in text


# ---- 5. 复用接口与真实入口行为 ---------------------------------------------


def test_verify_frozen_interface_still_wired() -> None:
    """run 传的三个参数必须仍是 verify_frozen_analysis.py 的真实接口。"""
    src = VERIFY_SCRIPT.read_text(encoding="utf-8")
    for flag in ('"--helper"', '"--output"', '"--build-ready"'):
        assert flag in src, f"verify_frozen_analysis.py 缺 {flag} 接口"


def test_real_entry_help_exits_zero() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert "run" in proc.stdout and "manifest" in proc.stdout


def test_real_entry_failure_path_no_success_manifest(tmp_path: Path) -> None:
    """真实失败路径（helper 缺失）：非零退出且不写成功 manifest。"""
    out = tmp_path / "build-ready.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "manifest",
         "--helper", str(tmp_path / "absent"), "--out", str(out),
         "--source-sha", BASE_SHA, "--repo-root", str(REPO_ROOT)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    if out.exists():
        assert json.loads(out.read_text(encoding="utf-8")).get("ok") is not True
