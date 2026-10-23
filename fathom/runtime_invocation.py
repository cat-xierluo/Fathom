"""调用配置、认证分离与跨版本输出归一（ISS-201 第一切片，纯函数层）。

本模块只做纯计算，把「跨厂商 CLI 形态会变」这件事从业务层里摘出来：

1. **调用配置声明**（:class:`InvocationProfile`）：把各家 argv 拆成四类
   —— 必需安全/隔离行为、可选能力、输出协议、认证相关 —— 并给每一类一个
   可被单测断言的机器可读声明。必需行为缺失即该 profile 不可用，禁止
   静默降级；可选能力缺失只降级该项并在快照里披露；输出协议不匹配是
   **protocol 类错误**，不是权限错误。
2. **认证分离**（:class:`AuthMethod` / :class:`AuthState` /
   :class:`AuthDescriptor`）：方式（bare / api_key / oauth / unknown）与
   状态（unknown / available / expired / missing / denied）两个正交字段。
   **只记方式与状态，绝不记秘密**：token、api_key、password、cookie、
   header、env 值一律不得出现；``detail`` 只允许出现环境变量名与文件角色。
   ``bare`` 与 ``oauth`` 互斥——Claude 的 ``--bare`` 形态根本不读
   OAuth/keychain，CLI 自报的 ``oauth_token`` 对该形态不可用。
3. **输出归一**（:class:`EventKind` / :class:`NormalizedEvent` /
   :func:`normalize_events`）：候选正文（final）与明确终态（terminal）
   分开；完整替换与增量并列候选取末条；重复、分片、缺终态、未知权限
   请求一律 fail-closed；**永不拼接正文**。
4. **错误分类**（:class:`FailureClass` / :func:`classify_failure`）：把
   现有 reason_code 映射到至少 8 个失败类别；超时必须带「受理状态不
   确定」标志，且**不得自动换引擎、不得二次发送**。

边界（ISS-201 第一切片）：

- 不接 DB、不改 API 端点形状、不发模型请求、不读凭据、不装包、不联网。
- 不复制既有生命周期：预览/派发/租约/取消/落库仍属 analysis_manager；
  本模块既不排队也不重试。
- 协议层容忍未知字段并有界计数；业务结果 JSON 的严格性仍由
  analysis_contract 负责，本模块不放宽那一条。
- 本模块不 import agent_runtime（避免与后续接线形成循环依赖）：
  失败分类按 reason_code 字符串与调用形态判定，不靠解析错误正文猜类别。
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence


# --------------------------------------------------------------------------
# 认证：方式与状态正交，只记标识不记秘密
# --------------------------------------------------------------------------


class AuthMethod(enum.Enum):
    """认证方式标识；不含任何秘密。"""

    BARE = "bare"        # 隔离形态：不读用户登录凭据（Claude --bare）
    API_KEY = "api_key"  # 由 API key / settings env 段 / apiKeyHelper 提供
    OAUTH = "oauth"      # 复用用户既有登录（keychain / auth.json）
    UNKNOWN = "unknown"  # 形态未确认（探测阶段默认）


class AuthState(enum.Enum):
    """认证状态；与 :class:`AuthMethod` 正交。"""

    UNKNOWN = "unknown"      # 未取得证据
    AVAILABLE = "available"  # 证据表明该方式可用
    EXPIRED = "expired"      # 凭据过期/撤销，可恢复动作是重新登录
    MISSING = "missing"      # 无凭据，可恢复动作是登录/配置 key
    DENIED = "denied"        # 有凭据但被拒（权限/范围）


#: 形如 ``token=xxx`` / ``api_key: xxx`` 的赋值一律视为泄露。
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:token|api[ _-]?key|apikey|password|passwd|secret|cookie|"
    r"authorization|bearer|credential)s?\b\s*[=:]\s*\S+"
)
#: 常见凭据字面量前缀。
_SECRET_LITERAL_RE = re.compile(r"\b(?:sk|pk|rk|xox[baprs])-[A-Za-z0-9_\-]{8,}")
#: 长随机串（token/哈希形态）。
_LONG_BLOB_RE = re.compile(r"\b[A-Za-z0-9+/=_\-]{40,}\b")
#: 绝对路径：detail 只允许出现「文件角色」，不许出现本机路径。
_ABS_PATH_RE = re.compile(r"(?<![\w~])/(?:Users|home|var|etc|opt|tmp|private|root)\b")


def assert_secret_free(detail: str, *, where: str = "detail") -> str:
    """校验一段人类可读说明不含秘密；不合规直接抛错。

    允许：环境变量名（``ANTHROPIC_AUTH_TOKEN``）、文件角色
    （``~/.claude/settings.json`` 的 env 段）、类别描述。
    不允许：凭据值、``token=`` 之类赋值、长随机串、绝对路径。
    """
    if not isinstance(detail, str):
        raise TypeError(f"{where} 必须是字符串")
    if _SECRET_ASSIGNMENT_RE.search(detail):
        raise ValueError(f"{where} 含疑似凭据赋值，已拒绝")
    if _SECRET_LITERAL_RE.search(detail):
        raise ValueError(f"{where} 含疑似凭据字面量，已拒绝")
    if _LONG_BLOB_RE.search(detail):
        raise ValueError(f"{where} 含疑似长随机串（凭据形态），已拒绝")
    if _ABS_PATH_RE.search(detail):
        raise ValueError(f"{where} 含本机绝对路径，只允许文件角色，已拒绝")
    return detail


@dataclass(frozen=True, slots=True)
class AuthVariant:
    """一种认证方式在本 profile 下的取证情况。

    ``active`` 表示该形态是否就是本次调用实际使用的形态。Claude 的
    ``--bare`` 调用里，OAuth 变体存在但**不可用**——它只是被如实登记，
    状态不得因为 CLI 自报 ``oauth_token`` 而被改成 available。
    """

    method: AuthMethod
    source: str
    detail: str = ""
    state: AuthState = AuthState.UNKNOWN
    active: bool = False
    #: 该形态在**本 profile 的调用形态**下结构性不可用。
    #:
    #: 例：Claude 的 ``--bare`` 形态根本不读 OAuth/keychain，所以该
    #: profile 下 OAuth 变体是结构性不可用——任何证据（包括 CLI 自报
    #: ``oauth_token``）都不能把它标成 available。
    structurally_unavailable: bool = False

    def to_dict(self) -> dict:
        return {
            "method": self.method.value,
            "state": self.state.value,
            "source": self.source,
            "detail": self.detail,
            "active": self.active,
            "structurally_unavailable": self.structurally_unavailable,
        }


@dataclass(frozen=True, slots=True)
class AuthDescriptor:
    """一次认证结论：方式 + 状态 + 证据来源；永不含秘密。"""

    method: AuthMethod
    state: AuthState
    source: str
    detail: str = ""
    #: 该形态在当前调用形态下结构性不可用（见 :class:`AuthVariant`）。
    structurally_unavailable: bool = False

    def to_dict(self) -> dict:
        return {
            "method": self.method.value,
            "state": self.state.value,
            "source": self.source,
            "detail": self.detail,
            "structurally_unavailable": self.structurally_unavailable,
        }


# --------------------------------------------------------------------------
# 调用配置声明
# --------------------------------------------------------------------------


class FlagKind(enum.Enum):
    """调用参数的分类（合同 §1 的四类 + 载荷通道）。"""

    SAFETY = "safety"          # 必需安全行为（禁工具/沙箱/工具集替换）
    ISOLATION = "isolation"    # 必需隔离行为（不落盘、不加载用户配置/规则）
    CAPABILITY = "capability"  # 可选能力（成本/便利/展示；缺失只降级该项）
    PROTOCOL = "protocol"      # 输出协议与载荷通道
    AUTH = "auth"              # 认证相关


class OutputProtocol(enum.Enum):
    """输出协议形态。"""

    SINGLE_JSON = "single_json"    # 单一 JSON 对象（整体替换）
    JSONL_STREAM = "jsonl_stream"  # JSONL 事件流（增量）


class FinalSelection(enum.Enum):
    """候选正文（final）的选取规则。"""

    REPLACE_SINGLE = "replace_single"  # 只允许唯一一条；多条即歧义
    LAST_BEFORE_TERMINAL = "last_before_terminal"  # 同轮并列候选取末条


@dataclass(frozen=True, slots=True)
class InvocationFlag:
    """一个调用参数及其承载的行为。

    ``tokens`` 是 argv 中必须**连续出现**的片段（空元组表示只按名字匹配）。
    ``required=True`` 表示该参数承载的行为属本任务的必需约束，缺失即
    profile 不可用；``required=False`` 表示缺失只降级该项，须在快照里
    披露。
    """

    name: str
    kind: FlagKind
    tokens: tuple[str, ...]
    required: bool
    behavior: str
    degrade_note: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "tokens": list(self.tokens),
            "required": self.required,
            "behavior": self.behavior,
            "degrade_note": self.degrade_note,
        }


def argv_contains(argv: Sequence[str], tokens: Sequence[str]) -> bool:
    """argv 是否连续包含给定片段。"""
    if not tokens:
        return False
    wanted = tuple(tokens)
    size = len(wanted)
    if len(argv) < size:
        return False
    for index in range(len(argv) - size + 1):
        if tuple(argv[index:index + size]) == wanted:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ProfileVerdict:
    """一份 argv 对某个 profile 的判定结论。"""

    usable: bool
    missing_required: tuple[str, ...]
    missing_protocol: tuple[str, ...]
    degraded_optional: tuple[str, ...]
    failure_class: "FailureClass | None"
    reason_code: str | None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "usable": self.usable,
            "missing_required": list(self.missing_required),
            "missing_protocol": list(self.missing_protocol),
            "degraded_optional": list(self.degraded_optional),
            "failure_class": (
                self.failure_class.value if self.failure_class else None
            ),
            "reason_code": self.reason_code,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class InvocationProfile:
    """一家的调用配置声明：四类参数 + 认证方式 + 输出协议。"""

    runtime_id: str
    display_name: str
    flags: tuple[InvocationFlag, ...]
    output_protocol: OutputProtocol
    final_selection: FinalSelection
    payload_channel: str
    auth_variants: tuple[AuthVariant, ...]
    third_party_retention: str = ""
    notes: tuple[str, ...] = ()

    # ---- 查询 ----

    def flag(self, name: str) -> InvocationFlag:
        for item in self.flags:
            if item.name == name:
                return item
        raise KeyError(f"{self.runtime_id} 没有名为 {name} 的参数声明")

    def required_flags(self) -> tuple[InvocationFlag, ...]:
        return tuple(f for f in self.flags if f.required)

    def optional_flags(self) -> tuple[InvocationFlag, ...]:
        return tuple(f for f in self.flags if not f.required)

    def protocol_flags(self) -> tuple[InvocationFlag, ...]:
        return tuple(f for f in self.flags if f.kind is FlagKind.PROTOCOL)

    def missing_required(self, argv: Sequence[str]) -> tuple[str, ...]:
        """缺失的必需安全/隔离参数名。"""
        return tuple(
            f.name
            for f in self.required_flags()
            if f.kind in (FlagKind.SAFETY, FlagKind.ISOLATION)
            and not argv_contains(argv, f.tokens)
        )

    def missing_protocol(self, argv: Sequence[str]) -> tuple[str, ...]:
        """缺失的输出协议参数名（协议错误，不是权限错误）。"""
        return tuple(
            f.name
            for f in self.protocol_flags()
            if f.required and not argv_contains(argv, f.tokens)
        )

    def degraded_optional(self, argv: Sequence[str]) -> tuple[str, ...]:
        """缺失的可选参数名；缺失只降级该项，须披露。"""
        return tuple(
            f.name
            for f in self.optional_flags()
            if not argv_contains(argv, f.tokens)
        )

    def verdict(self, argv: Sequence[str]) -> ProfileVerdict:
        """判定一份 argv 是否满足本 profile 的合同。"""
        missing_required = self.missing_required(argv)
        missing_protocol = self.missing_protocol(argv)
        degraded = self.degraded_optional(argv)
        if missing_required:
            return ProfileVerdict(
                usable=False,
                missing_required=missing_required,
                missing_protocol=missing_protocol,
                degraded_optional=degraded,
                failure_class=FailureClass.PERMISSION,
                reason_code="profile_required_flags_missing",
                detail=(
                    "缺少必需安全/隔离参数："
                    + "、".join(missing_required)
                    + "；禁止静默降级"
                ),
            )
        if missing_protocol:
            return ProfileVerdict(
                usable=False,
                missing_required=(),
                missing_protocol=missing_protocol,
                degraded_optional=degraded,
                failure_class=FailureClass.PROTOCOL,
                reason_code="profile_protocol_mismatch",
                detail=(
                    "输出协议不匹配（缺："
                    + "、".join(missing_protocol)
                    + "）；这是协议类错误，不是权限错误"
                ),
            )
        return ProfileVerdict(
            usable=True,
            missing_required=(),
            missing_protocol=(),
            degraded_optional=degraded,
            failure_class=None,
            reason_code=None,
            detail=("可选能力降级：" + "、".join(degraded)) if degraded else "",
        )

    # ---- 认证 ----

    def auth_descriptor(self, method: AuthMethod | None = None) -> AuthDescriptor:
        """取某一种认证方式的结论描述（不含秘密）。"""
        variant = self._variant(method)
        return AuthDescriptor(
            method=variant.method,
            state=variant.state,
            source=variant.source,
            detail=variant.detail,
            structurally_unavailable=variant.structurally_unavailable,
        )

    def active_auth_descriptor(self) -> AuthDescriptor:
        """本次调用实际生效的认证方式描述。"""
        for variant in self.auth_variants:
            if variant.active:
                return AuthDescriptor(
                    method=variant.method,
                    state=variant.state,
                    source=variant.source,
                    detail=variant.detail,
                    structurally_unavailable=variant.structurally_unavailable,
                )
        return AuthDescriptor(
            method=AuthMethod.UNKNOWN,
            state=AuthState.UNKNOWN,
            source="profile",
            detail="该 profile 未登记生效的认证方式",
        )

    def with_auth_state(
        self,
        method: AuthMethod,
        state: AuthState,
        *,
        source: str | None = None,
        detail: str | None = None,
    ) -> "InvocationProfile":
        """登记某一种认证方式的状态，返回新 profile（原对象不变）。

        硬约束：状态只更新**该 method 自己的**变体。API-key 的成功不
        能把 oauth 变体标成 available，反之亦然——两种方式是不同 profile
        的不同取证，不是同一家的两个状态位。
        """
        if method is AuthMethod.UNKNOWN:
            raise ValueError("不能用 UNKNOWN 登记认证状态")
        for variant in self.auth_variants:
            if variant.method is method and variant.structurally_unavailable:
                if state is AuthState.AVAILABLE:
                    raise ValueError(
                        f"{self.runtime_id} 的 {method.value} 形态在当前调用形态下"
                        "结构性不可用，不得标成 available"
                    )
        variants = tuple(
            replace(
                variant,
                state=state,
                source=source if source is not None else variant.source,
                detail=(
                    assert_secret_free(detail)
                    if detail is not None else variant.detail
                ),
            )
            if variant.method is method else variant
            for variant in self.auth_variants
        )
        if not any(v.method is method for v in variants):
            # 该 profile 尚未列出的方式：登记为新的、非生效变体。生效
            # 方式仍由 profile 的调用形态决定，不由取证结果改写。
            variants = variants + (
                AuthVariant(
                    method=method,
                    source=source or "evidence",
                    detail=assert_secret_free(detail) if detail else "",
                    state=state,
                    active=False,
                ),
            )
        return replace(self, auth_variants=variants)

    def _variant(self, method: AuthMethod | None) -> AuthVariant:
        wanted = method
        if wanted is None:
            for variant in self.auth_variants:
                if variant.active:
                    return variant
            wanted = AuthMethod.UNKNOWN
        for variant in self.auth_variants:
            if variant.method is wanted:
                return variant
        if wanted is AuthMethod.UNKNOWN:
            # 「未取得证据」是合法状态，不是错误：返回一个未知态描述。
            return AuthVariant(
                method=AuthMethod.UNKNOWN,
                source="profile",
                detail="该 profile 未登记此认证方式，未取得证据",
                state=AuthState.UNKNOWN,
                active=False,
            )
        raise KeyError(f"{self.runtime_id} 未登记认证方式 {wanted.value}")

    def to_dict(self) -> dict:
        """机器可读的调用配置声明。"""
        return {
            "runtime_id": self.runtime_id,
            "display_name": self.display_name,
            "output_protocol": self.output_protocol.value,
            "final_selection": self.final_selection.value,
            "payload_channel": self.payload_channel,
            "required": [
                f.to_dict() for f in self.flags
                if f.required and f.kind in (FlagKind.SAFETY, FlagKind.ISOLATION)
            ],
            "optional": [f.to_dict() for f in self.optional_flags()],
            "protocol": [f.to_dict() for f in self.protocol_flags()],
            "auth_variants": [v.to_dict() for v in self.auth_variants],
            "third_party_retention": self.third_party_retention,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# 失败分类
# --------------------------------------------------------------------------


class FailureClass(enum.Enum):
    """失败类别；一个 unsupported 不得掩盖所有真实原因。"""

    QUOTA = "quota"                  # 额度不足/速率限制
    NETWORK = "network"              # 传输断开、连不通
    LOGIN = "login"                  # 未登录/凭据过期/被拒
    TIMEOUT = "timeout"              # 超时
    PROTOCOL = "protocol"            # 协议形状不匹配、缺终态、未知权限请求
    RESULT_SCHEMA = "result_schema"  # 业务 JSON 不合 schema
    PERMISSION = "permission"        # 只读/禁工具配置不成立、授权类拒绝
    CANCELLED = "cancelled"          # 用户取消（非失败，终态独立）
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """一次失败的分类结论。

    ``dispatch_uncertain``：**受理状态不确定**——无法证明请求未被受理
    （超时、输出被截断、非零退出都可能已经送达引擎）。这类情况必须向
    用户披露「结果不确定」，不得自动重发。
    ``auto_retry_safe``：可以证明请求**确定未受理**（进程根本没起来），
    才允许在同一已批准目标与权限下自动重试一次。
    ``engine_switch_allowed``：本切片恒为 False——受理状态不明时不得自动
    换引擎、换模型或换授权。
    """

    failure_class: FailureClass
    reason_code: str
    detail: str = ""
    dispatch_uncertain: bool = False
    auto_retry_safe: bool = False
    engine_switch_allowed: bool = False

    def to_dict(self) -> dict:
        return {
            "failure_class": self.failure_class.value,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "dispatch_uncertain": self.dispatch_uncertain,
            "auto_retry_safe": self.auto_retry_safe,
            "engine_switch_allowed": self.engine_switch_allowed,
        }


#: reason_code → 失败类别的精确映射。
_REASON_CLASS: dict[str, FailureClass] = {
    "runner_timed_out": FailureClass.TIMEOUT,
    "runner_cancelled": FailureClass.CANCELLED,
    "runner_cancelled_before_start": FailureClass.CANCELLED,
    "cancelled": FailureClass.CANCELLED,
    "cancelled_before_start": FailureClass.CANCELLED,
    "runner_nonzero_exit": FailureClass.UNKNOWN,
    "runner_spawn_failed": FailureClass.PERMISSION,
    "runner_output_limit": FailureClass.PROTOCOL,
    "runner_decode_error": FailureClass.PROTOCOL,
    "app_error": FailureClass.UNKNOWN,
    "app_error_turn_failed": FailureClass.UNKNOWN,
    "app_error_stream_degraded": FailureClass.UNKNOWN,
    "adapter_contract_changed": FailureClass.PROTOCOL,
    "preview_stale": FailureClass.PERMISSION,
    "runtime_changed": FailureClass.PERMISSION,
    "policy_changed": FailureClass.PERMISSION,
    "analysis_disabled": FailureClass.PERMISSION,
    "profile_required_flags_missing": FailureClass.PERMISSION,
    "profile_protocol_mismatch": FailureClass.PROTOCOL,
}

#: reason_code 前缀 → 失败类别（精确映射未命中时按前缀判定）。
_REASON_PREFIX: tuple[tuple[str, FailureClass], ...] = (
    ("parse_failed_", FailureClass.PROTOCOL),
    ("protocol_", FailureClass.PROTOCOL),
    ("result_", FailureClass.RESULT_SCHEMA),
    ("runner_", FailureClass.UNKNOWN),
)

#: 受理状态不明的 reason_code：载荷已送出，可能已被引擎受理。
_DISPATCH_UNCERTAIN = frozenset({
    "runner_timed_out",
    "runner_nonzero_exit",
    "runner_output_limit",
    "runner_decode_error",
})

#: 可以证明「确定未受理」的 reason_code：进程没起来，载荷从未送出。
_AUTO_RETRY_SAFE = frozenset({"runner_spawn_failed"})


def classify_failure(
    reason_code: str,
    *,
    detail: str = "",
    failure_class: FailureClass | str | None = None,
) -> FailureClassification:
    """把一个 reason_code 归类成失败类别。

    ``failure_class`` 是适配器按**自己协议内的结构化错误**给出的类别
    （不是正文猜测）；给了就优先采用。没给时按 reason_code 的精确表与
    前缀表判定——``app_error`` 一个 reason_code 覆盖多种真实原因，缺
    结构化证据时一律记 UNKNOWN，不靠解析正文猜类别。
    """
    if not isinstance(reason_code, str) or not reason_code:
        raise ValueError("reason_code 必须是非空字符串")
    if isinstance(failure_class, str):
        failure_class = FailureClass(failure_class)
    if failure_class is None:
        failure_class = _REASON_CLASS.get(reason_code)
        if failure_class is None:
            for prefix, mapped in _REASON_PREFIX:
                if reason_code.startswith(prefix):
                    failure_class = mapped
                    break
        if failure_class is None:
            failure_class = FailureClass.UNKNOWN
    if failure_class is FailureClass.CANCELLED:
        return FailureClassification(
            failure_class=failure_class,
            reason_code=reason_code,
            detail=detail,
            dispatch_uncertain=False,
            auto_retry_safe=False,
            engine_switch_allowed=False,
        )
    return FailureClassification(
        failure_class=failure_class,
        reason_code=reason_code,
        detail=detail,
        dispatch_uncertain=reason_code in _DISPATCH_UNCERTAIN,
        auto_retry_safe=reason_code in _AUTO_RETRY_SAFE,
        engine_switch_allowed=False,
    )


# --------------------------------------------------------------------------
# 输出归一：事件集
# --------------------------------------------------------------------------


class EventKind(enum.Enum):
    """归一后的最小事件集（8 类）。

    ``FINAL`` 是「一段候选正文」，``TERMINAL`` 是「这次调用结束了且成功
    了」——两者必须分开：缺终态不能默认为成功。
    """

    START = "start"            # 一次调用/一轮 turn 开始
    PROGRESS = "progress"      # 中间说明性文本（不是最终正文）
    TOOL_REQUEST = "tool_request"  # 引擎请求执行工具
    WARNING = "warning"        # 非致命、降级、传输抖动
    FINAL = "final"            # 候选最终消息（尚不是业务结果）
    TERMINAL = "terminal"      # 明确成功/失败终态
    USAGE = "usage"            # token/费用统计
    MODEL = "model"            # 引擎报告的模型身份


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """一条归一事件。"""

    kind: EventKind
    text: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    payload: Mapping[str, Any] | None = None
    warning: str | None = None
    terminal_ok: bool | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "payload": dict(self.payload) if self.payload is not None else None,
            "warning": self.warning,
            "terminal_ok": self.terminal_ok,
        }


@dataclass(frozen=True, slots=True)
class NormalizedStream:
    """归一后的整条事件流结论。

    ``ok`` 为真当且仅当：拿到明确成功终态（``terminal_ok is True``）且有
    非空候选正文。缺终态、终态失败、正文歧义一律 ``ok=False`` 并给出
    ``reason_code``。
    """

    ok: bool
    events: tuple[NormalizedEvent, ...]
    final_text: str | None = None
    terminal_ok: bool | None = None
    usage: Mapping[str, Any] | None = None
    model: str | None = None
    warnings: tuple[str, ...] = ()
    unknown_field_count: int = 0
    unknown_event_count: int = 0
    duplicate_count: int = 0
    replay_count: int = 0
    tool_rejected_count: int = 0
    fragment_count: int = 0
    cross_turn_final_count: int = 0
    bad_line_count: int = 0
    reason_code: str | None = None
    detail: str = ""

    @property
    def clean_success(self) -> bool:
        """是否是「干净的完全成功」。

        ``ok`` 只说明这次调用有明确成功终态且拿到了候选正文。ISS-202 的
        真实 ACP 实证显示：**权限被拒 ≠ prompt 失败**——工具被拒后
        ``session/prompt`` 仍以成功终态结束。因此「有终态且成功」不等于
        「该做的分析做了」。本属性把「被拒工具」「重放」「重复」这些降级
        信号排除在「干净成功」之外，调用方据此如实披露。
        """
        return (
            self.ok
            and self.tool_rejected_count == 0
            and self.replay_count == 0
            and self.duplicate_count == 0
            and not self.warnings
        )

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "clean_success": self.clean_success,
            "events": [e.to_dict() for e in self.events],
            "final_text": self.final_text,
            "terminal_ok": self.terminal_ok,
            "usage": dict(self.usage) if self.usage is not None else None,
            "model": self.model,
            "warnings": list(self.warnings),
            "unknown_field_count": self.unknown_field_count,
            "unknown_event_count": self.unknown_event_count,
            "duplicate_count": self.duplicate_count,
            "replay_count": self.replay_count,
            "tool_rejected_count": self.tool_rejected_count,
            "fragment_count": self.fragment_count,
            "cross_turn_final_count": self.cross_turn_final_count,
            "bad_line_count": self.bad_line_count,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }


#: 未知权限请求的识别：事件类型含权限语义，或事件体带权限请求标记。
_PERMISSION_TYPE_MARKERS = ("permission", "approval", "consent", "authorize")
#: 分片标记字段（当前三家均未观测到，属保守拒绝分支）。
_FRAGMENT_KEYS = ("fragment", "fragments", "chunk", "part")
#: 终态原文截断上限。
_TERMINAL_DETAIL_LIMIT = 200


def _iter_json_lines(text: str) -> tuple[list[dict], int]:
    """把 JSONL 文本拆成事件字典与坏行计数。

    坏行（截断 JSON、非对象行）只计数不中断——但若最终一个可解析事件
    都没有，归一层按 ``protocol_no_parsable_events`` 失败。
    """
    import json

    events: list[dict] = []
    bad = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except ValueError:
            bad += 1
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            bad += 1
    return events, bad


def _control_envelopes(
    event: Mapping[str, Any], runtime_id: str,
) -> list[Mapping[str, Any]]:
    """只检查厂商控制 envelope，不递归扫描正文、usage 或普通元数据。"""
    envelopes = [event]
    if runtime_id == "codex-cli" and event.get("type") in {
        "item.started", "item.updated", "item.completed",
    }:
        item = event.get("item")
        if isinstance(item, Mapping):
            envelopes.append(item)
    return envelopes


def _permission_request_type(envelope: Mapping[str, Any]) -> bool:
    etype = str(envelope.get("type") or "").lower()
    return (
        etype not in _TOOL_REJECTED_TYPES
        and any(marker in etype for marker in _PERMISSION_TYPE_MARKERS)
    )


def _looks_like_permission_request(envelope: Mapping[str, Any]) -> bool:
    """未知权限字段的存在就是控制信号，不用 truthiness 猜它的语义。"""
    if _permission_request_type(envelope):
        return True
    etype = str(envelope.get("type") or "").lower()
    for key in ("permission", "permissions", "approval", "approval_request"):
        if key not in envelope:
            continue
        value = envelope[key]
        # 唯一允许的权限字段值是已识别工具 envelope 的明确拒绝决策。
        if key == "permission" and etype in {
            "tool_use", "tool_call", "tool_result", "command_execution",
        } and isinstance(value, str) and value.strip().lower() in _TOOL_REJECTED_VALUES:
            continue
        return True
    return False


def _has_fragment_marker(envelopes: Sequence[Mapping[str, Any]]) -> bool:
    """三家尚无已验证分片协议，任何声明形态都不能作为完整正文接收。"""
    return any(key in envelope for envelope in envelopes for key in _FRAGMENT_KEYS)


def _event_scope_ids(event: Mapping[str, Any]) -> dict[str, set[str]]:
    """保留标识命名空间；session_id 不能拿来与 turn_id 比较。"""
    scopes: dict[str, set[str]] = {}
    envelopes = [event]
    if isinstance(event.get("item"), Mapping):
        envelopes.append(event["item"])
    for envelope in envelopes:
        for namespace, aliases in (
            ("turn", ("turn_id", "turnId")),
            ("session", ("session_id", "sessionId")),
            ("thread", ("thread_id", "threadId")),
        ):
            for key in aliases:
                value = envelope.get(key)
                if isinstance(value, str) and value:
                    scopes.setdefault(namespace, set()).add(value)
    return scopes


def _scope_conflict(scopes: Sequence[Mapping[str, set[str]]]) -> bool:
    """只在同类显式标识之间判冲突；缺失标识维持既有兼容。"""
    return any(
        len(set().union(*(scope.get(namespace, set()) for scope in scopes))) > 1
        for namespace in ("turn", "session", "thread")
    )


def _event_turn_id(event: Mapping[str, Any]) -> str | None:
    for key in ("turn_id", "turnId", "session_id", "sessionId", "thread_id", "threadId"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    item = event.get("item")
    if isinstance(item, dict):
        for key in ("turn_id", "session_id", "thread_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _event_item_id(event: Mapping[str, Any]) -> str | None:
    for key in ("item_id", "itemId", "id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    item = event.get("item")
    if isinstance(item, dict):
        for key in ("id", "item_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _event_text(event: Mapping[str, Any]) -> str | None:
    for key in ("text", "result", "message", "content"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    item = event.get("item")
    if isinstance(item, dict):
        for key in ("text", "result", "message"):
            value = item.get(key)
            if isinstance(value, str):
                return value
    return None


def _event_usage(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("usage", "tokens", "token_usage"):
        value = event.get(key)
        if isinstance(value, dict):
            return value
    return None


def _event_model(event: Mapping[str, Any]) -> str | None:
    for key in ("model", "model_name", "model_id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# --------------------------------------------------------------------------
# 归一：各家事件 → 统一事件集
# --------------------------------------------------------------------------

#: 已知顶层字段（用于未知普通字段计数）。未列出的键计为未知字段，
#: 协议层容忍并有界计数。
_KNOWN_TOP_KEYS = frozenset({
    "type", "text", "result", "message", "content", "usage", "tokens",
    "token_usage", "model", "model_name", "model_id", "item", "items",
    "id", "item_id", "itemId", "turn_id", "turnId", "session_id", "sessionId",
    "thread_id", "threadId", "is_error", "subtype", "exit_code", "error",
    "warning", "tool_name", "tool_use_id", "num_turns", "total_cost_usd",
    "isPartial", "duration_ms", "fragment", "fragments", "chunk", "part",
    "permission", "permissions", "approval", "approval_request",
})

#: 明确表示「工具被拒」的事件类型。
_TOOL_REJECTED_TYPES = frozenset({
    "tool_rejected", "tool_use_rejected", "tool_denied",
    "permission_denied", "tool_permission_denied",
})

#: 工具执行结果里的「被拒」取值（ISS-202 实证：客户端回 reject 后工具
#: 未执行，但 prompt 仍以成功终态结束）。
_TOOL_REJECTED_VALUES = frozenset({
    "reject", "rejected", "denied", "deny", "refused", "cancelled",
    "canceled", "not_approved",
})

#: 工具结果里承载上述取值的键名。
_TOOL_DECISION_KEYS = (
    "status", "outcome", "decision", "result", "permission", "permission_status",
)


@dataclass
class _Accum:
    """归一过程中的累加器（内部用，不对外暴露）。"""

    events: list[NormalizedEvent] = None  # type: ignore[assignment]
    usage: Mapping[str, Any] | None = None
    model: str | None = None
    warnings: list[str] = None  # type: ignore[assignment]
    unknown_field_count: int = 0
    unknown_event_count: int = 0
    duplicate_count: int = 0
    replay_count: int = 0
    tool_rejected_count: int = 0
    fragment_count: int = 0
    cross_turn_final_count: int = 0
    tool_request_count: int = 0

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = []
        if self.warnings is None:
            self.warnings = []


def _tool_rejection(envelopes: Sequence[Mapping[str, Any]]) -> str | None:
    """仅工具 envelope 的决策字段有控制语义；普通 result 是正文。"""
    for envelope in envelopes:
        etype = str(envelope.get("type") or "").lower()
        if etype in _TOOL_REJECTED_TYPES:
            return f"工具被拒（{etype}）"
        if etype not in {"tool_use", "tool_call", "tool_result", "command_execution"}:
            continue
        for key in _TOOL_DECISION_KEYS:
            value = envelope.get(key)
            if isinstance(value, str) and value.strip().lower() in _TOOL_REJECTED_VALUES:
                return f"工具被拒（{key}={value.strip().lower()}）"
    return None


def _count_unknown_fields(event: Mapping[str, Any]) -> int:
    """统计事件里的未知普通字段数量（协议层容忍，不中断）。"""
    return sum(1 for key in event.keys() if key not in _KNOWN_TOP_KEYS)


def _map_claude(
    event: Mapping[str, Any], acc: _Accum
) -> tuple[NormalizedEvent | None, bool | None]:
    """Claude Code ``--output-format json``：单对象整体替换。

    ``is_error=true`` → 明确失败终态；``result`` 非字符串 → fail-closed。
    """
    etype = event.get("type")
    if etype == "tool_use" or etype == "tool_call":
        acc.tool_request_count += 1
        return (
            NormalizedEvent(
                kind=EventKind.TOOL_REQUEST,
                text=_event_text(event),
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
            ),
            None,
        )
    if etype != "result":
        return None, None
    if "is_error" in event and type(event["is_error"]) is not bool:
        return (
            NormalizedEvent(
                kind=EventKind.TERMINAL,
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
                terminal_ok=False,
                warning="is_error 字段必须是布尔值",
            ),
            False,
        )
    is_error = event.get("is_error") is True
    text = event.get("result")
    if not isinstance(text, str):
        # 正文不是字符串 → 不猜、不拼，直接判协议失败（由调用方按
        # 「缺终态/正文不合法」收口）。
        return (
            NormalizedEvent(
                kind=EventKind.TERMINAL,
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
                terminal_ok=False,
                warning="result 字段不是字符串",
            ),
            False,
        )
    model = _event_model(event)
    if model:
        acc.model = model
    # 单对象整体替换：候选正文（final）与终态（terminal）分开产出，
    # 终态由 normalize_events 依据本函数返回的布尔补齐。
    return (
        NormalizedEvent(
            kind=EventKind.FINAL,
            text=text,
            turn_id=_event_turn_id(event),
            item_id=_event_item_id(event),
        ),
        (not is_error),
    )


def _map_codex(
    event: Mapping[str, Any], acc: _Accum
) -> tuple[NormalizedEvent | None, bool | None]:
    """Codex CLI ``exec --json``：JSONL 事件流，多条并列候选取末条。"""
    etype = event.get("type")
    if etype in {"thread.started", "turn.started"}:
        return (
            NormalizedEvent(
                kind=EventKind.START,
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
            ),
            None,
        )
    if etype == "turn.completed":
        usage = _event_usage(event)
        if usage is not None:
            acc.usage = usage
        return (
            NormalizedEvent(
                kind=EventKind.TERMINAL,
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
                terminal_ok=True,
            ),
            True,
        )
    if etype == "turn.failed":
        return (
            NormalizedEvent(
                kind=EventKind.TERMINAL,
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
                terminal_ok=False,
                warning=str(event.get("error") or "turn.failed")[:_TERMINAL_DETAIL_LIMIT],
            ),
            False,
        )
    if etype == "item.completed":
        item = event.get("item")
        if not isinstance(item, dict):
            return None, None
        itype = item.get("type")
        if itype == "agent_message":
            text = item.get("text")
            if not isinstance(text, str):
                return None, None
            return (
                NormalizedEvent(
                    kind=EventKind.FINAL,
                    text=text,
                    turn_id=_event_turn_id(event),
                    item_id=_event_item_id(event),
                ),
                None,
            )
        if itype == "error":
            message = item.get("message")
            acc.warnings.append(
                message if isinstance(message, str)
                else str(item)[:_TERMINAL_DETAIL_LIMIT]
            )
            return (
                NormalizedEvent(
                    kind=EventKind.WARNING,
                    turn_id=_event_turn_id(event),
                    item_id=_event_item_id(event),
                    warning=acc.warnings[-1][:_TERMINAL_DETAIL_LIMIT],
                ),
                None,
            )
        return None, None
    if etype == "item.started":
        return (
            NormalizedEvent(
                kind=EventKind.START,
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
            ),
            None,
        )
    return None, None


def _map_hermes(
    event: Mapping[str, Any], acc: _Accum
) -> tuple[NormalizedEvent | None, bool | None]:
    """Hermes ``--format stream-json``：``result.text`` 是完整替换的结果，
    ``text`` 事件只是进度，不进正文。"""
    etype = event.get("type")
    if etype == "system":
        model = _event_model(event)
        if model:
            acc.model = model
        return (
            NormalizedEvent(
                kind=EventKind.START,
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
                payload={"model": model} if model else None,
            ),
            None,
        )
    if etype == "text":
        return (
            NormalizedEvent(
                kind=EventKind.PROGRESS,
                text=_event_text(event),
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
            ),
            None,
        )
    if etype == "tool_use":
        acc.tool_request_count += 1
        return (
            NormalizedEvent(
                kind=EventKind.TOOL_REQUEST,
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
                payload={"tool_name": event.get("tool_name")},
            ),
            None,
        )
    if etype == "result":
        text = event.get("text")
        usage = _event_usage(event)
        if usage is not None:
            acc.usage = usage
        error = event.get("error")
        exit_code = event.get("exit_code")
        invalid_exit = "exit_code" in event and type(exit_code) is not int
        invalid_error = error is not None and not isinstance(error, (str, Mapping))
        # error 对象即为错误；空字符串/None 是既有的无错误兼容形态。
        failed = invalid_exit or invalid_error or isinstance(error, Mapping) or (
            isinstance(error, str) and error != ""
        ) or (exit_code is not None and exit_code != 0)
        if failed:
            return (
                NormalizedEvent(
                    kind=EventKind.TERMINAL,
                    turn_id=_event_turn_id(event),
                    item_id=_event_item_id(event),
                    terminal_ok=False,
                    warning=("终态字段类型不合法" if invalid_exit or invalid_error else
                             str(error or f"exit_code={exit_code}"))[:_TERMINAL_DETAIL_LIMIT],
                ),
                False,
            )
        if not isinstance(text, str):
            return (
                NormalizedEvent(
                    kind=EventKind.TERMINAL,
                    turn_id=_event_turn_id(event),
                    item_id=_event_item_id(event),
                    terminal_ok=False,
                    warning="result 事件缺 text 字段",
                ),
                False,
            )
        return (
            NormalizedEvent(
                kind=EventKind.FINAL,
                text=text,
                turn_id=_event_turn_id(event),
                item_id=_event_item_id(event),
            ),
            True,
        )
    return None, None


#: profile runtime_id → 事件映射器。
_MAPPERS = {
    "claude-code": _map_claude,
    "codex-cli": _map_codex,
    "hermes-agent": _map_hermes,
}


def normalize_events(
    events: Sequence[Mapping[str, Any]] | str,
    *,
    profile: InvocationProfile,
) -> NormalizedStream:
    """把一家的原始事件归一成统一事件流。

    输入可以是已解析的事件序列（``list[dict]``），也可以是原始 JSONL
    文本（``str``，按 profile 的 SINGLE_JSON / JSONL_STREAM 解析）。

    判定规则（合同 §3.3-§3.5）：

    - **候选正文（final）与明确终态（terminal）分开**；缺终态 → 失败。
    - **永不拼接正文**：Codex 的多条并列候选取末条；Claude/Hermes 的完整
      替换只取唯一一条，多条即歧义。
    - 同 ``item_id`` 的重复 final：内容相同 → 幂等丢弃并计数；内容不同 →
      ``protocol_duplicate_conflict``。
    - 分片：没有已验证的三家协议，声明了分片标记即拒绝，不拼装正文。
    - 未知普通字段/未知事件类型：忽略并有界计数；**未知权限请求不
      忽略** → ``protocol_unknown_permission_request``。
    """
    if not isinstance(profile, InvocationProfile):
        raise TypeError("profile 必须是 InvocationProfile")
    mapper = _MAPPERS.get(profile.runtime_id)
    if mapper is None:
        raise KeyError(f"{profile.runtime_id} 没有事件映射器")
    input_reason: str | None = None
    if isinstance(events, str):
        if profile.output_protocol is OutputProtocol.SINGLE_JSON:
            import json
            try:
                value = json.loads(events)
            except ValueError:
                value = None
            raw_events = [value] if isinstance(value, dict) else []
            bad_lines = 0 if raw_events else 1
            if not raw_events:
                input_reason = "protocol_single_json_required"
        else:
            raw_events, bad_lines = _iter_json_lines(events)
    elif isinstance(events, Mapping):
        raw_events = [dict(events)]
        bad_lines = 0
    else:
        raw_events = [dict(e) for e in events]
        bad_lines = 0
    if profile.output_protocol is OutputProtocol.SINGLE_JSON and (
        len(raw_events) != 1 or raw_events[0].get("type") != "result"
    ):
        input_reason = "protocol_single_json_required"

    acc = _Accum()
    final_candidates: list[NormalizedEvent] = []
    terminal_events: list[NormalizedEvent] = []
    final_scopes: list[dict[str, set[str]]] = []
    terminal_scopes: list[dict[str, set[str]]] = []
    start_scopes: list[dict[str, set[str]]] = []
    seen_final_by_item: dict[str, str] = {}
    reason_code: str | None = input_reason
    detail = "输出必须是唯一 result JSON 对象" if input_reason else ""

    for event in raw_events:
        acc.unknown_field_count += _count_unknown_fields(event)
        envelopes = _control_envelopes(event, profile.runtime_id)
        # 明确请求先于 status=denied 等拒绝信号，不能被降成工具警告。
        explicit_request = any(_permission_request_type(e) for e in envelopes)
        rejection = None if explicit_request else _tool_rejection(envelopes)
        if explicit_request or any(_looks_like_permission_request(e) for e in envelopes):
            reason_code = reason_code or "protocol_unknown_permission_request"
            detail = detail or "出现未知权限请求事件，拒绝猜测成功"
            acc.events.append(
                NormalizedEvent(
                    kind=EventKind.WARNING,
                    warning="未知权限请求事件",
                    turn_id=_event_turn_id(event),
                    item_id=_event_item_id(event),
                )
            )
            continue
        if rejection is not None:
            acc.tool_rejected_count += 1
            acc.warnings.append(rejection)
            acc.events.append(
                NormalizedEvent(
                    kind=EventKind.WARNING,
                    warning=rejection,
                    turn_id=_event_turn_id(event),
                    item_id=_event_item_id(event),
                )
            )
            continue

        mapped, terminal_ok = mapper(event, acc)
        # 所有不自带终态的候选共用截止点，分片也不能绕过这个检查。
        if mapped is not None and mapped.kind is EventKind.FINAL and (
            terminal_events and terminal_ok is None
        ):
            continue
        if _has_fragment_marker(envelopes):
            acc.fragment_count += 1
            reason_code = reason_code or "protocol_unsupported_fragment"
            detail = detail or "分片协议未经验证，拒绝拼装或推断完整正文"
            continue
        if mapped is None:
            # 未知事件类型：忽略 + 有界计数，不当终态也不当正文。
            acc.unknown_event_count += 1
            continue
        acc.events.append(mapped)
        if mapped.kind is EventKind.START and (
            (profile.runtime_id == "codex-cli" and event.get("type") in {
                "thread.started", "turn.started",
            }) or (profile.runtime_id == "hermes-agent" and event.get("type") == "system")
        ):
            # 调用/轮次开始的显式身份属于同一绑定；工具 item.started
            # 只表示一个 item 的生命周期，不据此推断新调用。
            start_scopes.append(_event_scope_ids(event))
        if mapped.kind is not EventKind.TERMINAL and terminal_ok is not None:
            # 映射器已给出终态结论但事件本身是正文/进度：补一条独立的
            # 终态事件（终态与候选正文必须分开可辨），并放进事件列表，
            # 让消费方能直接按 kind 过滤。
            terminal_event = NormalizedEvent(
                kind=EventKind.TERMINAL,
                turn_id=mapped.turn_id,
                item_id=mapped.item_id,
                terminal_ok=bool(terminal_ok),
            )
            terminal_events.append(terminal_event)
            terminal_scopes.append(_event_scope_ids(event))
            acc.events.append(terminal_event)
        if mapped.kind is EventKind.FINAL:
            # 空正文仍须先登记作用域和同 ID 内容，不能隐藏完成项冲突。
            final_scopes.append(_event_scope_ids(event))
            item_id = mapped.item_id
            if item_id is not None:
                previous = seen_final_by_item.get(item_id)
                if previous is not None:
                    if previous == (mapped.text or ""):
                        # 同一 item_id、同一内容仅作幂等丢弃。
                        # 不同 ID 同内容不构成重放证据。
                        acc.duplicate_count += 1
                        continue
                    reason_code = reason_code or "protocol_duplicate_conflict"
                    detail = detail or "同一 item_id 的候选正文内容冲突，拒绝猜测"
                    continue
                seen_final_by_item[item_id] = mapped.text or ""
            # 只有 Codex 独立的空候选允许回退到前一个非空结论。
            # Hermes 的每条 result 都是完整替换，空结果也参与唯一性判断。
            if profile.runtime_id == "codex-cli" and (
                not mapped.text or not mapped.text.strip()
            ):
                continue
            final_candidates.append(mapped)
        if mapped.kind is EventKind.TERMINAL:
            terminal_events.append(mapped)
            terminal_scopes.append(_event_scope_ids(event))
        if mapped.kind is EventKind.USAGE and mapped.payload is not None:
            acc.usage = mapped.payload
        if mapped.kind is EventKind.MODEL and mapped.text:
            acc.model = mapped.text

    # 正文选取：绝不拼接。
    final_text: str | None = None
    if _scope_conflict(final_scopes):
        acc.cross_turn_final_count = len(final_candidates)
        reason_code = reason_code or "protocol_ambiguous_final"
        detail = detail or "候选正文跨 turn 出现，不合并"
    elif final_candidates:
        if profile.final_selection is FinalSelection.LAST_BEFORE_TERMINAL:
            final_text = final_candidates[-1].text
        else:
            if len(final_candidates) > 1:
                reason_code = reason_code or "protocol_ambiguous_final"
                detail = detail or "完整替换形态出现多条候选正文"
            else:
                final_text = final_candidates[0].text

    if not raw_events:
        # 根因优先：连一个可解析事件都没有，缺终态只是它的后果。
        return NormalizedStream(
            ok=False,
            events=tuple(acc.events),
            final_text=None,
            terminal_ok=None,
            usage=acc.usage,
            model=acc.model,
            warnings=tuple(acc.warnings),
            unknown_field_count=acc.unknown_field_count,
            unknown_event_count=acc.unknown_event_count,
            duplicate_count=acc.duplicate_count,
            replay_count=acc.replay_count,
            tool_rejected_count=acc.tool_rejected_count,
            fragment_count=acc.fragment_count,
            cross_turn_final_count=acc.cross_turn_final_count,
            bad_line_count=bad_lines,
            reason_code=reason_code or "protocol_no_parsable_events",
            detail=detail or "无可解析事件",
        )

    # 终态判定：必须存在明确终态。
    terminal_ok: bool | None = None
    if terminal_events:
        # 任意失败终态都不能被后续成功覆写；同状态且同轮才可幂等。
        terminal_ok = all(t.terminal_ok is True for t in terminal_events)
        if terminal_ok is False:
            failed = next(t for t in terminal_events if t.terminal_ok is not True)
            reason_code = reason_code or "protocol_terminal_error"
            detail = detail or (failed.warning or "引擎报告失败终态")[:_TERMINAL_DETAIL_LIMIT]
        elif _scope_conflict(terminal_scopes):
            reason_code = reason_code or "protocol_ambiguous_terminal"
            detail = detail or "终态的同类轮次标识冲突"
        elif _scope_conflict(start_scopes + final_scopes + terminal_scopes):
            reason_code = reason_code or "protocol_terminal_mismatch"
            detail = detail or "开始、候选正文与终态的同类轮次标识不匹配"
    else:
        reason_code = reason_code or "protocol_missing_terminal"
        detail = detail or "缺明确终态，按失败处理"

    ok = (
        reason_code is None
        and terminal_ok is True
        and isinstance(final_text, str)
        and bool(final_text.strip())
    )
    if reason_code is None and not ok:
        reason_code = "protocol_missing_final"
        detail = detail or "缺候选正文"

    return NormalizedStream(
        ok=ok,
        events=tuple(acc.events),
        final_text=final_text if ok else None,
        terminal_ok=terminal_ok,
        usage=acc.usage,
        model=acc.model,
        warnings=tuple(acc.warnings),
        unknown_field_count=acc.unknown_field_count,
        unknown_event_count=acc.unknown_event_count,
        duplicate_count=acc.duplicate_count,
        replay_count=acc.replay_count,
        tool_rejected_count=acc.tool_rejected_count,
        fragment_count=acc.fragment_count,
        cross_turn_final_count=acc.cross_turn_final_count,
        bad_line_count=bad_lines,
        reason_code=reason_code,
        detail=detail,
    )


# --------------------------------------------------------------------------
# 三家的调用配置声明（事实来源：agent_runtime 的适配器合同）
# --------------------------------------------------------------------------

#: Claude Code 的调用配置。
#:
#: ``--bare`` 既是隔离行为也**决定认证形态**：bare 下不读 OAuth/keychain，
#: 只走 API key（settings 的 env 段 / apiKeyHelper）。
CLAUDE_CODE_PROFILE = InvocationProfile(
    runtime_id="claude-code",
    display_name="Claude Code",
    flags=(
        InvocationFlag(
            name="bare",
            kind=FlagKind.ISOLATION,
            tokens=("--bare",),
            required=True,
            behavior="跳过 hooks/skills/插件/MCP/记忆/CLAUDE.md 自动发现",
            degrade_note="无等价替代；缺失即改变第三方留存与扩展面",
        ),
        InvocationFlag(
            name="disallowed_tools",
            kind=FlagKind.SAFETY,
            tokens=("--disallowedTools", "*"),
            required=True,
            behavior="显式禁全部工具（版本退化保护）",
            degrade_note="无等价替代；不得用提示词替代",
        ),
        InvocationFlag(
            name="strict_mcp_config",
            kind=FlagKind.SAFETY,
            tokens=("--strict-mcp-config",),
            required=True,
            behavior="不加载任何 MCP 配置",
            degrade_note="无等价替代（MCP 面不可穷尽枚举）",
        ),
        InvocationFlag(
            name="disable_slash_commands",
            kind=FlagKind.SAFETY,
            tokens=("--disable-slash-commands",),
            required=True,
            behavior="禁 slash 命令",
            degrade_note="无等价替代",
        ),
        InvocationFlag(
            name="no_session_persistence",
            kind=FlagKind.ISOLATION,
            tokens=("--no-session-persistence",),
            required=True,
            behavior="不落盘会话",
            degrade_note="无等价替代；缺失改变第三方留存行为",
        ),
        InvocationFlag(
            name="output_format_json",
            kind=FlagKind.PROTOCOL,
            tokens=("--output-format", "json"),
            required=True,
            behavior="stdout 输出单 JSON 对象（整体替换）",
            degrade_note="stream-json 形态不被本归一层接受",
        ),
        InvocationFlag(
            name="payload_stdin",
            kind=FlagKind.PROTOCOL,
            tokens=("-p",),
            required=True,
            behavior="载荷经 stdin（argv 形态为已披露降级回退）",
            degrade_note="argv 载荷对同用户进程可见，仅在 stdin 失效且复验后显式选择",
        ),
    ),
    output_protocol=OutputProtocol.SINGLE_JSON,
    final_selection=FinalSelection.REPLACE_SINGLE,
    payload_channel="stdin",
    auth_variants=(
        AuthVariant(
            method=AuthMethod.BARE,
            source="profile",
            detail="--bare 形态不读 OAuth/keychain，仅可用 API key 通道",
            state=AuthState.UNKNOWN,
            active=True,
        ),
        AuthVariant(
            method=AuthMethod.API_KEY,
            source="profile",
            detail="可用通道：~/.claude/settings.json 的 env 段或 apiKeyHelper",
            state=AuthState.UNKNOWN,
            active=False,
        ),
        AuthVariant(
            method=AuthMethod.OAUTH,
            source="profile",
            detail="该形态下 OAuth/keychain 不可用；CLI 自报 oauth_token 也不改变这一点",
            state=AuthState.UNKNOWN,
            active=False,
            structurally_unavailable=True,
        ),
    ),
    third_party_retention="无（--no-session-persistence）；非 bare 形态由 CLI 自身改写全局配置",
    notes=(
        "认证方式由调用形态决定，不由 CLI 自报文本决定",
        "禁止用「提示词里请不要调用工具」替代 --disallowedTools",
    ),
)

#: Codex CLI 的调用配置。
CODEX_CLI_PROFILE = InvocationProfile(
    runtime_id="codex-cli",
    display_name="Codex CLI",
    flags=(
        InvocationFlag(
            name="sandbox_read_only",
            kind=FlagKind.SAFETY,
            tokens=("-s", "read-only"),
            required=True,
            behavior="OS 级 Seatbelt 只读沙箱（防破坏硬边界）",
            degrade_note="无等价替代；放宽沙箱禁止作为降级",
        ),
        InvocationFlag(
            name="ephemeral",
            kind=FlagKind.ISOLATION,
            tokens=("--ephemeral",),
            required=True,
            behavior="不落盘会话",
            degrade_note="无等价替代；缺失改变第三方留存行为",
        ),
        InvocationFlag(
            name="ignore_user_config",
            kind=FlagKind.ISOLATION,
            tokens=("--ignore-user-config",),
            required=True,
            behavior="不加载 ~/.codex/config.toml",
            degrade_note="无等价替代",
        ),
        InvocationFlag(
            name="ignore_rules",
            kind=FlagKind.ISOLATION,
            tokens=("--ignore-rules",),
            required=True,
            behavior="不加载 execpolicy rules",
            degrade_note="无等价替代",
        ),
        InvocationFlag(
            name="skip_git_repo_check",
            kind=FlagKind.ISOLATION,
            tokens=("--skip-git-repo-check",),
            required=True,
            behavior="隔离 cwd 非 git 仓库，跳过仓库检查",
            degrade_note="缺失只产生探测噪音；但当前合同固定带",
        ),
        InvocationFlag(
            name="json_output",
            kind=FlagKind.PROTOCOL,
            tokens=("--json",),
            required=True,
            behavior="stdout 输出 JSONL 事件流",
            degrade_note="无替代形态可用",
        ),
        InvocationFlag(
            name="reasoning_effort_low",
            kind=FlagKind.CAPABILITY,
            tokens=("-c", "model_reasoning_effort=low"),
            required=False,
            behavior="低推理预算（成本/时延）",
            degrade_note="缺失只降级成本与时延，不影响安全边界",
        ),
    ),
    output_protocol=OutputProtocol.JSONL_STREAM,
    final_selection=FinalSelection.LAST_BEFORE_TERMINAL,
    payload_channel="stdin",
    auth_variants=(
        AuthVariant(
            method=AuthMethod.OAUTH,
            source="profile",
            detail="CLI 自读 CODEX_HOME 下的 auth.json（ChatGPT 登录态）",
            state=AuthState.UNKNOWN,
            active=True,
        ),
        AuthVariant(
            method=AuthMethod.API_KEY,
            source="profile",
            detail="该形态下不作为主通道",
            state=AuthState.UNKNOWN,
            active=False,
        ),
    ),
    notes=(
        "认证路径独立于配置加载路径（--ignore-user-config 不影响 auth.json）",
        "载荷只走 stdin，不提供 argv 回退",
    ),
)

#: Hermes Agent 的调用配置。
HERMES_AGENT_PROFILE = InvocationProfile(
    runtime_id="hermes-agent",
    display_name="Hermes Agent",
    flags=(
        InvocationFlag(
            name="readonly_toolset",
            kind=FlagKind.SAFETY,
            tokens=("-t", "web,vision"),
            required=True,
            behavior="调用级工具集替换语义，收敛为只读三件套",
            degrade_note="无等价替代；-t 空值会回退平台默认工具集",
        ),
        InvocationFlag(
            name="ignore_user_config",
            kind=FlagKind.ISOLATION,
            tokens=("--ignore-user-config",),
            required=True,
            behavior="不加载 ~/.hermes/config.yaml（.env 凭据仍会加载）",
            degrade_note="无等价替代",
        ),
        InvocationFlag(
            name="ignore_rules",
            kind=FlagKind.ISOLATION,
            tokens=("--ignore-rules",),
            required=True,
            behavior="不注入 AGENTS.md/SOUL.md/memory/skills",
            degrade_note="无等价替代",
        ),
        InvocationFlag(
            name="query_file_stdin",
            kind=FlagKind.PROTOCOL,
            tokens=("--query-file", "-"),
            required=True,
            behavior="载荷经 stdin（官方形态，不经 shell 解释、不落盘）",
            degrade_note="不提供 argv 回退",
        ),
        InvocationFlag(
            name="stream_json_output",
            kind=FlagKind.PROTOCOL,
            tokens=("--format", "stream-json"),
            required=True,
            behavior="stdout 输出 JSONL 事件流",
            degrade_note="无替代形态可用",
        ),
        InvocationFlag(
            name="run_budget",
            kind=FlagKind.ISOLATION,
            tokens=("--run-budget", "150"),
            required=True,
            behavior="每轮运行墙钟预算（挂死硬上限）",
            degrade_note="无等价替代",
        ),
        InvocationFlag(
            name="source_tool",
            kind=FlagKind.CAPABILITY,
            tokens=("--source", "tool"),
            required=False,
            behavior="第三方集成来源标记，不混入用户会话列表",
            degrade_note="缺失只降级来源标记，不影响安全边界",
        ),
    ),
    output_protocol=OutputProtocol.JSONL_STREAM,
    final_selection=FinalSelection.REPLACE_SINGLE,
    payload_channel="stdin",
    auth_variants=(
        AuthVariant(
            method=AuthMethod.API_KEY,
            source="profile",
            detail="CLI 自读 ~/.hermes/.env；与 --ignore-user-config 解耦",
            state=AuthState.UNKNOWN,
            active=True,
        ),
    ),
    third_party_retention="失败请求会在 ~/.hermes/sessions/ 留 request_dump_*.json（第三方留存）",
    notes=(
        "--safe-mode 单独使用不是防破坏边界",
        "认证与配置加载解耦：--ignore-user-config 仍会加载 .env",
    ),
)

#: 全部已声明 profile。
PROFILES: dict[str, InvocationProfile] = {
    p.runtime_id: p for p in (
        CLAUDE_CODE_PROFILE,
        CODEX_CLI_PROFILE,
        HERMES_AGENT_PROFILE,
    )
}


def get_profile(runtime_id: str) -> InvocationProfile:
    """按 runtime id 取调用配置声明。"""
    try:
        return PROFILES[runtime_id]
    except KeyError:
        raise KeyError(f"未声明调用配置的 runtime：{runtime_id}") from None
