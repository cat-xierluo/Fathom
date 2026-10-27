"""Runtime 入口身份与能力快照（ISS-200）。

职责边界：

- 业务层（analysis_contract/analysis_manager）不关心具体引擎；兼容层负责把
  「用户配置的入口」变成一份有证据的能力描述。
- 本模块只做纯计算与只读文件元数据采集：不做模型请求、不读凭据、不安装、
  不后台轮询，也不写数据库。
- 证据分层（ISS-199 §5.2：不同证据强度不得互相冒充）：
  - ``VERIFIED``  只用于「本机、某版本、某调用配置下的行为实测」，必须带
    具体的 scope（版本 + 调用配置 + 入口），不得用品牌静态文案充当。
  - ``NEGOTIATED`` 协议握手/能力协商中由引擎自报。
  - ``HELP``      命令帮助文本只能证明「参数存在」，不能证明行为。
  - ``NONE``      无证据。
- 能力逐项取 supported/unsupported/unknown；必需能力与可选能力分离：
  必需能力缺失使该入口不可用，可选能力缺失只让那项降级。
- 旧负例（某家在某构建/配置/失败能力下不可用）绑定到具体能力与配置，
  不无条件封禁该品牌的新入口。
"""

from __future__ import annotations

import enum
import hashlib
import os
import re
import json
import shlex
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping


class CapabilityState(enum.Enum):
    """单项能力的三态。unknown 表示尚未取得该能力的证据。"""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class EvidenceStrength(enum.Enum):
    """证据强度。强度不同不得互相冒充。"""

    VERIFIED = "verified"      # 本机行为实测（必须绑定版本与调用配置）
    NEGOTIATED = "negotiated"  # 协议握手/能力协商中引擎自报
    HELP = "help"              # 命令帮助文本线索，最弱
    NONE = "none"              # 无证据


#: 必需能力：缺失则该入口不可用。
REQUIRED_CAPABILITIES = frozenset({
    "bounded_input",       # 能限定输入范围
    "executable_identity",  # 可确认执行身份
    "terminal_state",      # 能识别成功/失败终态
    "final_result",        # 能取得完整最终结果
    "cancellable",         # 可取消或由共同进程管理终止
    "no_duplicate_send",   # 不明结果时不重复发送
})

#: 可选能力：缺失只降级对应功能，不阻断整家。
OPTIONAL_CAPABILITIES = frozenset({
    "streaming",           # token 流
    "usage_reporting",     # token/费用统计
    "model_reporting",     # 模型身份
    "native_schema",       # 原生结构化输出约束
    "session_resume",      # 会话恢复
    "image_input",         # 图片输入
    "multi_turn",          # 多轮
    "tool_progress",       # 工具进度事件
    "ephemeral_session",   # 原生临时会话开关
})


@dataclass(frozen=True, slots=True)
class EntryIdentity:
    """一个已发现入口的多层身份。

    ``layers`` 逐层记录「配置的入口 → 符号链接目标 → 脚本包装的底层引擎 →
    解释器」。只解 realpath 不足以表达 shell wrapper，所以额外做一层
    包装脚本解析（只读、只取已知形态，不执行任何东西）。
    """

    configured_path: str          # 归一化后的用户配置路径
    resolved_path: str | None     # realpath 之后的真实目标
    size_bytes: int | None = None
    mtime_ns: int | None = None
    content_digest: str | None = None   # 全文件分块原始哈希
    readable: bool = True                 # 能否读到内容摘要
    layers: tuple[str, ...] = ()          # 入口 → 底层引擎 的层级链
    wrapper_kind: str = "direct"          # direct / symlink / script / unknown
    #: 每个已采集文件均为 full_sha256；none 表示未能完成采集。
    digest_scope: str = "none"
    layer_identities: tuple[tuple[str, str, int, int, str], ...] = ()
    identity_complete: bool = False
    # A uniquely resolved interpreter/program chain may be sendable without
    # proving its dynamic application body. It never authorizes VERIFIED evidence.
    execution_chain_resolved: bool = False
    identity_issues: tuple[str, ...] = ()

    @property
    def sendable(self) -> bool:
        return self.identity_complete or self.execution_chain_resolved

    @property
    def assurance(self) -> str:
        return "complete" if self.identity_complete else ("probed" if self.sendable else "unresolved")

    @property
    def is_wrapper(self) -> bool:
        """是否为包装入口（符号链接或脚本包装）。"""
        return self.wrapper_kind in {"symlink", "script"}

    def to_dict(self) -> dict:
        return {
            "configured_path": self.configured_path,
            "resolved_path": self.resolved_path,
            "is_wrapper": self.is_wrapper,
            "wrapper_kind": self.wrapper_kind,
            "layers": list(self.layers),
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "content_digest": self.content_digest,
            "readable": self.readable,
            "digest_scope": self.digest_scope,
            "fingerprint": self.fingerprint(),
            "layer_identities": [list(layer) for layer in self.layer_identities],
            "identity_complete": self.identity_complete,
            "execution_chain_resolved": self.execution_chain_resolved,
            "assurance": self.assurance,
            "identity_issues": list(self.identity_issues),
        }

    def fingerprint(self) -> str:
        """入口指纹：同路径但文件变了必须得到不同值。"""
        return opaque_digest(json.dumps({
            "configured_path": self.configured_path,
            "resolved_path": self.resolved_path,
            "size": self.size_bytes, "mtime": self.mtime_ns,
            "digest": self.content_digest, "scope": self.digest_scope,
            "layers": self.layers, "identities": self.layer_identities,
            "complete": self.identity_complete, "resolved": self.execution_chain_resolved,
            "issues": self.identity_issues,
        }, sort_keys=True, separators=(",", ":")))

    def public_dict(self) -> dict:
        """面向用户可见响应的脱敏形态。

        API 错误卫生合同：用户可见响应里不出现本机绝对路径与内容摘要。
        保留仍可判定的结构信息（包装形态、层数、体积、摘要覆盖范围）与一个
        **不透明**指纹——同一文件稳定、文件一变即变，但不可反推路径。
        """
        return {
            "is_wrapper": self.is_wrapper,
            "wrapper_kind": self.wrapper_kind,
            "layer_count": len(self.layers),
            "size_bytes": self.size_bytes,
            "digest_scope": self.digest_scope,
            "readable": self.readable,
            "identity_complete": self.identity_complete,
            "execution_chain_resolved": self.execution_chain_resolved,
            "assurance": self.assurance,
            "fingerprint": opaque_digest(self.fingerprint()),
            "fingerprint_scope": "opaque_sha256",
        }


def _normalized(path: str | os.PathLike[str]) -> str:
    """归一化配置路径：绝对、展开 ~、去掉冗余分量。"""
    text = os.path.expanduser(str(path))
    return os.path.normpath(os.path.abspath(text))


def opaque_digest(text: str) -> str:
    """把含本机路径/内容的指纹压成不可反推的摘要。

    用户可见响应需要「同一入口稳定、入口一变即变」的可比性，但不该把
    绝对路径或内容摘要交给前端。SHA-256 在此足够：它只是可比性锚点，
    不自行证明调用授权；身份摘要仍须覆盖所有已解析的执行层。
    """
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _file_digest(path: Path, *, deadline: float | None = None) -> tuple[str | None, bool, str]:
    """授权身份只用分块全量 SHA-256；到期或读取失败即丢弃部分摘要。

    内存与文件大小无关。每次 identify 独立采集，绝不跨调用复用旧摘要。
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    return None, False, "none"
                chunk = handle.read(_DIGEST_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest(), True, "full_sha256"
    except OSError:
        return None, False, "none"


#: 分块哈希的块大小（1 MiB）：内存占用与文件大小无关。
_DIGEST_CHUNK_BYTES = 1024 * 1024
_SHEBANG_PROBE_BYTES = 4
_SCRIPT_PARSE_MAX_BYTES = 1024 * 1024
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
_LITERAL_ASSIGNMENT = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)=(?:\"([^\"$]*)\"|'([^'$]*)'|([^\s\"'$]*))")


def _program_interpreter(name: str) -> bool:
    return name == "node" or bool(re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", name))


def _shell_dependencies(raw: str) -> tuple[tuple[str, ...], bool]:
    """证明一个刻意有限的 shell 子集；未知语法一律 incomplete。

    仅接受逐行的字面赋值、无外部执行的少量 builtin，以及字面绝对路径
    （或前置字面赋值变量）的直接 exec。不解释条件、函数、命令列表、
    重定向、替换或普通命令转发；不能因为没找到 exec 就宣称无依赖。
    依赖采集和完整性判定共用同一个解析结果，不回退到路径正则猜测。
    """
    dependencies: list[str] = []
    variables: dict[str, str] = {}
    complete = True
    for line in raw.split("\n")[1:]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Tokenize separators independently of quoted text; shlex alone is not a
        # shell parser. Any operator token is outside this proven subset (even a
        # quoted token consisting solely of an operator is conservatively rejected).
        if (any(char in line for char in "\\`")
                or any(ord(char) < 32 and char != "\t" for char in line)):
            complete = False
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";|&<>(){}")
            lexer.whitespace_split = True
            lexer.commenters = ""
            words = list(lexer)
        except ValueError:
            complete = False
            continue
        if any(word and all(char in ";|&<>(){}" for char in word) for word in words):
            complete = False
            continue
        assignment = _LITERAL_ASSIGNMENT.fullmatch(line)
        if assignment:
            variables[assignment[1]] = next(
                value for value in assignment.groups()[1:] if value is not None)
            continue
        if not words:
            continue
        # No printf: options / %n can mutate variables in some shells.
        if words[0] in {"echo", "true", "false", ":", "exit"}:
            if any("$" in word for word in words[1:]):
                complete = False
            continue
        if words[0] != "exec" or len(words) < 2:
            complete = False
            continue
        def expand_literal(word: str) -> str:
            match = re.fullmatch(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})(/[^$*?\[\]~]*)?", word)
            if not match:
                return word
            # Expansion must stay a single quoted word, with a previously known
            # literal value. Unquoted command selection is never inferred.
            if not re.search(r'(?:^|\s)"' + re.escape(word) + r'"(?:\s|$)', line):
                return ""
            value = variables.get(match[1] or match[2])
            return value + (match[3] or "") if value is not None else ""

        target = expand_literal(words[1])
        if (not target.startswith("/")
                or any(char in target for char in "$*?[]~")):
            complete = False
            continue
        dependencies.append(target)
        args = [expand_literal(word) if word != "$@" else word for word in words[2:]]
        if (any("$" in word and word != "$@" for word in args)
                or any("$" in original and original != "$@" and not expanded
                       for original, expanded in zip(words[2:], args))
                or ("$@" in args and '\"$@\"' not in line)):
            complete = False
        name = Path(target).name
        if name == "env":
            complete = False  # env dispatch is not part of the direct-exec subset.
        if name in _SHELLS or _program_interpreter(name):
            # An explicit interpreter adds a script layer; -c/-m, stdin, and
            # dynamic/relative script selection require semantics we do not infer.
            if (args and args[0].startswith("/")
                    and not any(char in args[0] for char in "$*?[]~")):
                dependencies.append(args[0])
            else:
                complete = False
    return tuple(dependencies), complete


def _script_layers(path: Path) -> tuple[str, ...]:
    """只读有界脚本，返回有限语法证明的 exec 层；非脚本只读头部。"""
    try:
        with path.open("rb") as handle:
            if not handle.read(_SHEBANG_PROBE_BYTES).startswith(b"#!"):
                return ()
            if path.stat().st_size > _SCRIPT_PARSE_MAX_BYTES:
                return ()
            handle.seek(0)
            bounded = handle.read(_SCRIPT_PARSE_MAX_BYTES + 1)
        if len(bounded) > _SCRIPT_PARSE_MAX_BYTES:
            return ()
        raw = bounded.decode("utf-8")
    except (OSError, UnicodeError):
        return ()
    layers = _shell_dependencies(raw)[0]
    if layers:
        return layers
    # The normalized dirname launcher is also a script wrapper. A plain script
    # program (for example a shell echo) keeps the existing direct classification.
    if any(line.strip().startswith("exec ") for line in raw.splitlines()[1:]):
        dependencies, _, _, resolved = _script_dependencies(path)
        if resolved:
            return dependencies
    return ()


def _script_dependencies(path: Path, *, path_env: str | None = None,
                         invoked_path: str | None = None):
    """Resolve the executable chain, independently of dynamic program semantics.

    Native files and the finite shell grammar are fully proven. A known
    interpreter running one fixed script is PROBED; we do not interpret JavaScript
    or Python imports, or claim complete behavioral identity for these programs.
    Unknown dispatch targets, interpreter flags and missing layers fail closed.
    """
    try:
        with path.open("rb") as handle:
            if not handle.read(_SHEBANG_PROBE_BYTES).startswith(b"#!"):
                return (), False, True, True
            handle.seek(0)
            line = handle.readline(4096).decode("utf-8", errors="strict").strip()
        words = shlex.split(line[2:])
    except (OSError, UnicodeError, ValueError):
        return (), True, False, False
    if not words or not words[0].startswith("/"):
        return (), True, False, False
    dependencies = [words[0]]
    interpreter = words[0]
    if Path(interpreter).name == "env":
        args = words[1:]
        if args and args[0] == "-S":
            args = args[1:]
            if len(args) == 1:
                try:
                    args = shlex.split(args[0])
                except ValueError:
                    return tuple(dependencies), True, False, False
        if not args or args[0].startswith("-") or "=" in args[0]:
            return tuple(dependencies), True, False, False
        interpreter = shutil.which(args[0], path=path_env)
        if interpreter is None:
            return tuple(dependencies), True, False, False
        dependencies.append(interpreter)
        if len(args) != 1:
            # Preserve the known interpreter identity, but do not approve flags
            # whose startup-file or dispatch semantics have not been proven.
            return tuple(dependencies), True, False, False
    elif len(words) != 1:
        return tuple(dependencies), True, False, False
    name = Path(interpreter).name
    if _program_interpreter(name):
        return tuple(dependencies), True, False, True
    if name not in _SHELLS:
        return tuple(dependencies), True, False, False
    try:
        if path.stat().st_size > _SCRIPT_PARSE_MAX_BYTES:
            return tuple(dependencies), True, False, False
        with path.open("rb") as handle:
            bounded = handle.read(_SCRIPT_PARSE_MAX_BYTES + 1)
        if len(bounded) > _SCRIPT_PARSE_MAX_BYTES:
            return tuple(dependencies), True, False, False
        raw = bounded.decode("utf-8")
    except (OSError, UnicodeError):
        return tuple(dependencies), True, False, False
    # A common launcher uses only dirname($0) to select a fixed sibling. Resolve
    # relative to the actual invoked path (which may itself be a symlink), and
    # fingerprint the standard dirname utility as well. No shell is executed here.
    dynamic = False
    lines = raw.splitlines()
    for index, body_line in enumerate(lines[1:], 1):
        assignment = re.fullmatch(
            r'\s*([A-Za-z_][A-Za-z0-9_]*)=(?:"\$\(dirname "\$0"\)"|\$\(dirname "\$0"\))\s*', body_line)
        if assignment:
            dirname = shutil.which("dirname", path=path_env)
            if dirname is None or os.path.realpath(dirname) not in {"/usr/bin/dirname", "/bin/dirname"}:
                return tuple(dependencies), True, False, False
            lines[index] = assignment[1] + "=" + shlex.quote(str(Path(invoked_path or path).parent))
            dependencies.append(dirname)
            dynamic = True
            continue
        match = re.fullmatch(r'\s*exec "\$\(dirname "\$0"\)(/[^"$`\\*?\[\]]+)" "\$@"\s*', body_line)
        if not match:
            continue
        dirname = shutil.which("dirname", path=path_env)
        if dirname is None or os.path.realpath(dirname) not in {"/usr/bin/dirname", "/bin/dirname"}:
            return tuple(dependencies), True, False, False
        target = str(Path(invoked_path or path).parent) + match[1]
        lines[index] = "exec " + shlex.quote(target) + ' "$@"'
        dependencies.append(dirname)
        dynamic = True
    targets, resolved = _shell_dependencies("\n".join(lines))
    dependencies.extend(targets)
    dynamic = dynamic or any(_program_interpreter(Path(target).name) for target in targets)
    return tuple(dependencies), True, resolved and not dynamic, resolved


def identify_entry(path, *, deadline: float | None = None, path_env: str | None = None):
    """采集每一静态执行层的内容身份；到期/动态层仅报告不完整。"""
    candidate = Path(_normalized(path))
    if deadline is not None and time.monotonic() >= deadline:
        return EntryIdentity(str(candidate), None, readable=False, wrapper_kind="unknown",
                             digest_scope="none", identity_issues=("budget_exhausted",))
    try:
        stat = candidate.stat()
        if not candidate.is_file():
            return None
    except OSError:
        return None
    layers: list[str] = []
    records = []
    issues = []
    unresolved = False
    # Deduplicate only within this identify operation, including shared interpreters.
    digests: dict[str, tuple[str | None, bool, str]] = {}
    visited: set[str] = set()
    digest_stats: dict[str, tuple[int, ...]] = {}
    wrapper_kind = "symlink" if candidate.is_symlink() else "direct"

    def collect(item: Path, depth: int = 0):
        nonlocal wrapper_kind, unresolved
        if deadline is not None and time.monotonic() >= deadline:
            issues.append("budget_exhausted")
            return
        if depth > 8 or len(records) >= 32:
            issues.append("layer_limit")
            return
        configured = _normalized(item)
        real = os.path.realpath(configured)
        if configured not in layers:
            layers.append(configured)
        if Path(configured).is_symlink() and real not in layers:
            layers.append(real)
        try:
            before = Path(real).stat()
            if not Path(real).is_file():
                issues.append("not_regular_file")
                return
            signature = (before.st_dev, before.st_ino, before.st_size,
                         before.st_mtime_ns, before.st_ctime_ns)
            if real not in digests:
                digests[real] = _file_digest(Path(real), deadline=deadline)
                digest_stats[real] = signature
            elif digest_stats[real] != signature:
                issues.append("changed_during_read")
            digest, readable, scope = digests[real]
            after = Path(real).stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                            after.st_mtime_ns, after.st_ctime_ns):
                issues.append("changed_during_read")
            if not readable or scope != "full_sha256":
                issues.append("unreadable_layer")
            records.append((configured, real, before.st_size, before.st_mtime_ns, digest or ""))
        except OSError:
            issues.append("missing_layer")
            return
        if real in visited:
            return
        visited.add(real)
        if deadline is not None and time.monotonic() >= deadline:
            issues.append("budget_exhausted")
            return
        deps, is_script, complete, resolved = _script_dependencies(
            Path(real), path_env=path_env, invoked_path=configured)
        if not resolved:
            unresolved = True
            issues.append("dynamic_or_unresolved_layer")
        elif not complete:
            issues.append("dynamic_application")
        if depth == 0 and is_script and _script_layers(Path(real)):
            wrapper_kind = "script"
        for dependency in deps:
            collect(Path(dependency), depth + 1)

    collect(candidate)
    outer = digests.get(os.path.realpath(candidate), (None, False, "none"))
    return EntryIdentity(
        configured_path=str(candidate), resolved_path=os.path.realpath(candidate),
        size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
        content_digest=outer[0], readable=outer[1], digest_scope=outer[2],
        layers=tuple(layers), wrapper_kind=wrapper_kind,
        layer_identities=tuple(records), identity_complete=bool(records) and not issues,
        execution_chain_resolved=bool(records) and not unresolved
            and not (set(issues) - {"dynamic_application"}),
        identity_issues=tuple(sorted(set(issues))),
    )


def entry_changed(before, after):
    """入口是否发生变化（用于缓存失效）。任一侧缺失即视为变化。"""
    if before is None or after is None:
        return before is not after
    if not before.sendable or not after.sendable:
        return True
    return before.fingerprint() != after.fingerprint()


@dataclass(frozen=True, slots=True)
class Capability:
    """单项能力与其证据。

    ``state`` 与 ``evidence`` 是两件事，不得互相冒充：

    - ``state`` 是对**行为**的判定。探测期（只跑过 ``--version``/``--help``）
      没有任何行为证据，故只能是 ``UNKNOWN``——``SUPPORTED`` 只在真实受控
      调用成功后由 ``apply_call_evidence`` 回填。
    - ``evidence`` 是支撑该判定的**证据强度**。探测期最高只到 ``HELP``
      （参数存在性），``VERIFIED`` 必须绑定具体的一次受控调用。
    """

    name: str
    state: CapabilityState
    evidence: EvidenceStrength = EvidenceStrength.NONE
    source: str = ""          # 证据来源：本次探测所得，不是品牌静态文案
    scope: str = ""           # 证据适用范围（版本 + 调用配置）
    required: bool = False
    detail: str = ""          # 结构化说明：为何是这个状态/这个证据

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "evidence": self.evidence.value,
            "source": self.source,
            "scope": self.scope,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ScopedBlocker:
    """有范围的负例：绑定失败能力、调用配置与版本，而非整个品牌。"""

    capability: str
    reason_code: str
    scope: str
    detail: str = ""
    runtime_id: str = ""
    version: str | None = None
    profile: str = ""
    entry_fingerprint: str = ""

    def matches(self, runtime_id, version, profile, identity) -> bool:
        """历史负例仅在完整已观测作用域匹配时应用；缺身份不能猜。"""
        return bool(identity and identity.identity_complete and self.entry_fingerprint
                    and self.runtime_id == runtime_id and self.version == version
                    and self.profile == profile
                    and self.entry_fingerprint == identity.fingerprint())

    def to_dict(self) -> dict:
        return {
            "capability": self.capability,
            "reason_code": self.reason_code,
            "scope": self.scope,
            "runtime_id": self.runtime_id,
            "version": self.version,
            "profile": self.profile,
            "entry_fingerprint": self.entry_fingerprint,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """一次探测产出的能力快照。"""

    runtime_id: str
    identity: EntryIdentity | None
    capabilities: tuple[Capability, ...] = ()
    scoped_blockers: tuple[ScopedBlocker, ...] = ()
    version: str | None = None
    invocation_profile: str = ""
    observed_at: str = ""
    #: 本 profile 声明需要的调用参数（来自注册表，不塞进能力名字）。
    declared_required_flags: tuple[str, ...] = ()
    #: 已验证版本集合（来自注册表）。
    verified_versions: tuple[str, ...] = ()
    #: 该 profile 的能力门上限（如 READY / UNSUPPORTED）。
    capability_cap: str = ""

    def get(self, name):
        for capability in self.capabilities:
            if capability.name == name:
                return capability
        return None

    def missing_required(self):
        return tuple(
            c.name for c in self.capabilities
            if c.required and c.state is not CapabilityState.SUPPORTED
        )

    def degraded_optional(self):
        return tuple(
            c.name for c in self.capabilities
            if not c.required and c.state is not CapabilityState.SUPPORTED
        )

    def to_dict(self) -> dict:
        """完整形态（含本机路径与内容摘要），供本机内部消费。"""
        return {
            "runtime_id": self.runtime_id,
            "identity": self.identity.to_dict() if self.identity else None,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "missing_required": list(self.missing_required()),
            "degraded_optional": list(self.degraded_optional()),
            "scoped_blockers": [b.to_dict() for b in self.scoped_blockers],
            "version": self.version,
            "invocation_profile": self.invocation_profile,
            "observed_at": self.observed_at,
            "declared_required_flags": list(self.declared_required_flags),
            "verified_versions": list(self.verified_versions),
            "capability_cap": self.capability_cap,
        }

    def public_dict(self) -> dict:
        """面向用户可见响应的脱敏形态。

        不含本机绝对路径与内容摘要（API 错误卫生合同）；保留能力判定、
        证据强度、缺口清单与注册表声明的结构化事实。
        """
        return {
            "runtime_id": self.runtime_id,
            "identity": self.identity.public_dict() if self.identity else None,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "missing_required": list(self.missing_required()),
            "degraded_optional": list(self.degraded_optional()),
            "scoped_blockers": [b.to_dict() for b in self.scoped_blockers],
            "version": self.version,
            "invocation_profile": self.invocation_profile,
            "observed_at": self.observed_at,
            "declared_required_flags": list(self.declared_required_flags),
            "verified_versions": list(self.verified_versions),
            "capability_cap": self.capability_cap,
        }


def build_snapshot(
    runtime_id,
    *,
    identity,
    supported=None,
    unsupported=None,
    unknown=None,
    evidence=None,
    scope=None,
    detail=None,
    scoped_blockers=(),
    version=None,
    invocation_profile="",
    observed_at="",
    declared_required_flags=(),
    verified_versions=(),
    capability_cap="",
):
    """由证据构造能力快照。

    ``supported``/``unsupported``/``unknown`` 的键为能力名，值为来源说明；
    未出现在任何映射中的能力按 unknown 记录，不默认支持。

    ``detail`` 给出每项能力的结构化说明（如「仅参数存在性检查」、
    「版本探测失败，未取得行为证据」），避免 ``source`` 一句自由文本
    独自承担全部语义。

    状态与证据强度分离：调用方若在探测期把某能力标为 supported，必须
    同时给出不超过 ``HELP`` 的证据强度——否则本函数拒绝该组合。
    ``VERIFIED`` 只能由 ``apply_call_evidence`` 回填后出现。
    """
    supported = dict(supported or {})
    unsupported = dict(unsupported or {})
    unknown = dict(unknown or {})
    evidence = dict(evidence or {})
    scope = scope or {}
    detail = detail or {}

    names = set(supported) | set(unsupported) | set(unknown)
    names |= set(REQUIRED_CAPABILITIES) | set(OPTIONAL_CAPABILITIES)

    capabilities = []
    for name in sorted(names):
        if name in supported:
            state = CapabilityState.SUPPORTED
            source = supported[name]
        elif name in unsupported:
            state = CapabilityState.UNSUPPORTED
            source = unsupported[name]
        else:
            state = CapabilityState.UNKNOWN
            source = unknown.get(name, "")
        strength = evidence.get(name, EvidenceStrength.NONE)
        if strength is EvidenceStrength.VERIFIED:
            # VERIFIED 只能来自真实受控调用回填；探测期不得直接写。
            raise ValueError(
                f"能力 {name} 不得在探测期直接标为 verified；"
                "请用 apply_call_evidence 回填真实调用证据")
        capabilities.append(Capability(
            name=name,
            state=state,
            evidence=strength,
            source=source,
            scope=scope.get(name, ""),
            required=name in REQUIRED_CAPABILITIES,
            detail=detail.get(name, ""),
        ))
    return CapabilitySnapshot(
        runtime_id=runtime_id,
        identity=identity,
        capabilities=tuple(capabilities),
        scoped_blockers=tuple(scoped_blockers),
        version=version,
        invocation_profile=invocation_profile,
        observed_at=observed_at,
        declared_required_flags=tuple(declared_required_flags),
        verified_versions=tuple(verified_versions),
        capability_cap=capability_cap,
    )


@dataclass(frozen=True, slots=True)
class CallBinding:
    """可信调用方在启动受控调用前保存的记录；不可从返回证据反推。

    此模块不执行调用、不验证外部日志真实性。未来消费者必须保管独立记录，
    完成成功终态与结果校验后，才把证据和原记录一起传入回填接口。
    """
    runtime_id: str
    version: str
    profile: str
    entry_fingerprint: str
    call_id: str
    argv_digest: str


class CallIdentityError(ValueError):
    """Missing, unreadable or unresolved executable identity."""


class CallConfigurationError(ValueError):
    """Invocation/runtime/profile does not match the selected configuration."""


def bind_call(snapshot, argv: tuple[str, ...] | list[str], call_id: str,
              *, require_complete: bool = True) -> CallBinding:
    """Bind a sendable chain; complete identity remains mandatory for VERIFIED."""
    if (not snapshot or not snapshot.identity or not snapshot.identity.sendable
            or (require_complete and not snapshot.identity.identity_complete)):
        raise CallIdentityError("调用记录需要可解析入口身份")
    if not snapshot.runtime_id or not snapshot.version or not snapshot.invocation_profile:
        raise CallConfigurationError("调用记录需要 runtime、版本和配置")
    if not call_id or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", call_id):
        raise CallConfigurationError("调用关联标识缺失或格式错误")
    if not argv or any(not isinstance(arg, str) for arg in argv):
        raise CallConfigurationError("调用 argv 必须是非空字符串序列")
    if _normalized(argv[0]) != snapshot.identity.configured_path:
        raise CallConfigurationError("调用入口与快照不一致")
    digest = "sha256:" + opaque_digest(json.dumps(list(argv), ensure_ascii=False, separators=(",", ":")))
    return CallBinding(snapshot.runtime_id, snapshot.version, snapshot.invocation_profile,
                       snapshot.identity.fingerprint(), call_id, digest)


@dataclass(frozen=True, slots=True)
class CallEvidence:
    """仅由成功且完成结果校验的受控调用产出，所有能力采用相同绑定规则。"""
    capability: str
    argv_digest: str
    profile: str
    version: str | None = None
    detail: str = ""
    runtime_id: str = ""
    entry_fingerprint: str = ""
    call_id: str = ""
    succeeded: bool = False
    result_validated: bool = False


def apply_call_evidence(snapshot, evidence: CallEvidence, *, expected_call: CallBinding | None = None):
    """逐项核对快照、独立调用记录与证据；任一不匹配则拒绝回填。

    纯函数，不接生产。expected_call 必须是可信调用方预先保管的原记录，
    不能根据 evidence 现场生成。格式校验不能证明外部证据的真实性。
    """
    if snapshot is None or expected_call is None:
        raise ValueError("缺少快照或独立预期调用记录")
    if evidence.capability not in REQUIRED_CAPABILITIES | OPTIONAL_CAPABILITIES:
        raise ValueError("未知能力名")
    if not snapshot.identity or not snapshot.identity.identity_complete:
        raise ValueError("入口身份不完整")
    if evidence.succeeded is not True or evidence.result_validated is not True:
        raise ValueError("调用未成功或结果未校验")
    expected_scope = (snapshot.runtime_id, snapshot.version, snapshot.invocation_profile,
                      snapshot.identity.fingerprint())
    if not all(expected_scope) or expected_scope != (
            expected_call.runtime_id, expected_call.version, expected_call.profile,
            expected_call.entry_fingerprint):
        raise ValueError("预期调用记录与目标快照不一致")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", evidence.argv_digest):
        raise ValueError("调用 argv 摘要格式错误")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_call.argv_digest):
        raise ValueError("预期调用 argv 摘要格式错误")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", expected_call.call_id):
        raise ValueError("调用关联标识格式错误")
    for field in ("runtime_id", "version", "profile", "entry_fingerprint", "call_id", "argv_digest"):
        if getattr(evidence, field) != getattr(expected_call, field):
            raise ValueError("调用证据绑定不匹配：" + field)
    scope_text = (f"runtime={evidence.runtime_id};version={evidence.version};profile={evidence.profile}"
                  f";entry={evidence.entry_fingerprint};call={evidence.call_id};argv={evidence.argv_digest}")
    updated = tuple(replace(capability, state=CapabilityState.SUPPORTED,
                            evidence=EvidenceStrength.VERIFIED,
                            source="本机受控调用实测（成功且结果校验通过）",
                            scope=scope_text, detail=evidence.detail or capability.detail)
                    if capability.name == evidence.capability else capability
                    for capability in snapshot.capabilities)
    return replace(snapshot, capabilities=updated)
