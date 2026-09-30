"""本机 Agent Runtime 注册表、能力探测与公共 CLI runner（ISS-035A）。

职责与合同（docs/plans/2026-09-28-agent-diff-interpretation-design.md §3.1/§3.2/§5）：

- 注册表固定四家候选的产品身份、已知安装位置、版本探测方式与可用性上限。
  availability 四态 ``not_found / broken / unsupported / ready``；认证独立三态
  ``unknown / required / verified``，不把 ``--version`` 成功解释为已登录。
- 探测只在显式触发时运行：先受限已知安装位置与进程 PATH 枚举，再按需做
  有预算的登录 shell ``command -v`` 探测；禁止 eval 探测结果、自动安装与
  全盘遍历。预算：最多 16 个候选、单命令 3 秒、整个检测 20 秒、单命令
  输出 16 KiB。覆盖空格路径、符号链接、失效 wrapper、非执行文件、重名
  二进制、超时与不认识的版本；未知版本默认 unsupported。
- 公共 runner：shell=False 参数数组；载荷优先 stdin；``start_new_session``
  独立进程组；超时/取消 SIGTERM → 宽限期 → SIGKILL，终态前 reap；
  stdout/stderr 分离且边读边限额；UTF-8 严格解码（坏字节即失败，不静默
  替换）。成功必须同时通过三关：退出码 0 + 应用层无错误 + 完整结果
  结构验证；禁止只凭退出码或 PID 存活判成功。
- 本模块只提供检测与受控调用的基础能力，不接 API/DB，不排队、不重试、
  不写任何持久状态；预览/派发/生命周期属 analysis_manager（ISS-035B）。
  ``python3 -m fathom.agent_runtime`` 的 ``--probe`` / ``--check`` 仅是
  隔离手动验证入口，不构成产品功能面。

已核实行为记录（实现期复核，原始证据见 worktree ``evidence/``，不入库）：

- Claude Code 2.1.237（原生 arm64）为四家中唯一 ready：``--bare`` 组合下
  禁工具（stream-json init 事件 ``tools=[]``，全程 0 个 tool_use）、
  CLAUDE.md/hooks/MCP/skills 均不自动加载；认证复用用户
  ``~/.claude/settings.json`` 的 ``env`` 段注入（OAuth/keychain 在 --bare
  形态下不读取，OAuth-only 用户未验证，故 probe 的认证恒为 unknown）。
- ``~/.claude.json`` 全局配置写入复核（2026-09-29，实现期实测，分调用
  类型）：``--version`` 不写；本模块的 ``--bare`` 完整组合 4 次显式
  hash 前后对比（stdin 载荷/事件流形态、隔离 cwd）均不写；无 ``--bare``
  的最小调用形态会被 CLI 自身改写（内容为启动计数、项目信任/统计条目、
  feature 缓存等元数据，键名核对、值未转写）。结论：产品合同固定
  ``--bare`` 组合即可满足「不改用户 CLI 全局配置」；该文件 Fathom 不读、
  不写、不依赖，非 bare 形态的写入是第三方 CLI 自身行为，按已知行为
  披露（证据见 worktree evidence/，OAuth-only 形态与未来版本按
  ISS-035E 复核）。
- ZCode 的不可开放原因与复核条件注册在 ``CANDIDATES``（能力上限 + 稳定
  reason_code），不为其构建适配器；身份记录与 CodeBuddy（腾讯）等相似
  产品明确区分。
- Codex CLI（ISS-126，DEC-030 防破坏能力门）：``-s read-only`` 的 OS 级
  Seatbelt 只读沙箱原生匹配新门，0.147.0 锚定 ready。防破坏负例（写/删/
  破坏命令）由 PM 于 2026-09-30 实证（worktree evidence/REPORT-pm-
  negatives.md，不入库）：模型层规避（e1b/e1c/e1d/e2）+ OS 层拒绝旁证
  （035E E4 同机制 exit=134）+ e1d 步骤 1 排除「模型没能力执行命令」的
  替代解释。读取任意本机文件与联网是用户裁决（2026-09-30）允许的能力，
  在设置页如实披露；调用载荷只走 stdin（codex exec 无 prompt 位置参数
  形态，实测提示行与日志进 stderr、stdout 纯 JSONL）。
- Hermes Agent（ISS-126，DEC-030 防破坏能力门）：``chat -t web,vision``
  的调用级工具集**替换**语义把工具面收敛为 web_search/web_extract/
  vision_analyze 只读三件，0.21.5 锚定 ready。负例（直白/无害外观/伪装
  写诱导、删除诱导零 tool_use 零副作用）与通道对照（web_search 真实执行）
  见 worktree evidence/iss-126-hermes-20260930/（不入库）；``--safe-mode``
  单独使用不是防破坏边界（terminal/write_file/rm 无审批照常执行反例在档），
  只与 ``-t`` 并用作附加隔离。载荷只走 stdin（``--query-file -``）。

argv 载荷披露：claude 支持 ``-p <payload>`` 以 argv 携带载荷，该形态下
载荷文本对本机同用户进程（ps 等）可见。适配器默认走 stdin；argv 仅作
显式选择的已验证回退（见 ``ClaudeCodeAdapter.build_invocation``）。

精简 PATH 边界：脚本 shebang 的解释器解析发生在内核层（``env node`` 等），
绝对路径调用解决不了它；本机 zcode.cjs（node shebang）与 hermes（venv
python）在打包 helper 的精简 PATH 下可能 broken，这正是注册表按运行环境
如实报告 broken/ready、不假设安装状态的原因。claude 为原生二进制，已
在最小 env（PATH+HOME）下实证可运行。
"""

from __future__ import annotations

import codecs
import enum
import json
import logging
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

_LOG = logging.getLogger("fathom.agent_runtime")

# --------------------------------------------------------------------------
# 常量与预算
# --------------------------------------------------------------------------

#: 适配器合同版本；调用合同变化时递增并由消费方校验。
ADAPTER_CONTRACT_VERSION = 1

#: 分析派发（ISS-035B）使用的运行预算；变更须有测量证据并回写任务。
RUN_DEFAULT_TIMEOUT_S = 120.0
STDOUT_MAX_BYTES = 256 * 1024
STDERR_MAX_BYTES = 64 * 1024
TERM_GRACE_S = 3.0

#: 探测预算（方案 §3.1 起始值）。
PROBE_MAX_CANDIDATES = 16
PROBE_PER_COMMAND_TIMEOUT_S = 3.0
PROBE_TOTAL_TIMEOUT_S = 20.0
PROBE_PER_COMMAND_OUTPUT_CAP = 16 * 1024


# --------------------------------------------------------------------------
# 枚举与数据结构
# --------------------------------------------------------------------------


class Availability(enum.Enum):
    """Runtime 可用性四态。"""

    NOT_FOUND = "not_found"
    BROKEN = "broken"
    UNSUPPORTED = "unsupported"
    READY = "ready"


class AuthStatus(enum.Enum):
    """认证状态三态；与可用性互相独立。"""

    UNKNOWN = "unknown"
    REQUIRED = "required"
    VERIFIED = "verified"


class RunOutcome(enum.Enum):
    """单次受控运行的可辨结局；彼此互斥，超时/取消/超限/解码失败可区分。"""

    OK = "ok"
    NONZERO_EXIT = "nonzero_exit"
    APP_ERROR = "app_error"            # 适配器判定：exit 0 但应用层报错（如 is_error）
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    DECODE_ERROR = "decode_error"
    SPAWN_FAILED = "spawn_failed"
    PARSE_FAILED = "parse_failed"      # 适配器判定：结果结构验证不通过


@dataclass(frozen=True)
class ProbeBudget:
    """探测预算；超出即停止并如实报告，不放宽。"""

    max_candidates: int = PROBE_MAX_CANDIDATES
    per_command_timeout_s: float = PROBE_PER_COMMAND_TIMEOUT_S
    total_timeout_s: float = PROBE_TOTAL_TIMEOUT_S
    per_command_output_cap: int = PROBE_PER_COMMAND_OUTPUT_CAP


@dataclass(frozen=True)
class CandidateMeta:
    """注册表条目：四家候选的静态元数据与能力上限。

    ``capability_cap``/``cap_reason_code`` 是该家能开放的天花板：即使本机
    安装且版本可读，也不得高于上限开放。当前上限 READY：Claude Code
    （严格禁工具，DEC-029 门）与 Codex CLI（OS 级 read-only 沙箱，
    DEC-030 防破坏门），均仅限 ``verified_versions`` 内的版本。
    """

    id: str
    display_name: str
    binary_name: str
    identity: str
    identity_evidence: str
    official_docs: str
    known_locations: tuple[str, ...]
    version_args: tuple[str, ...] = ("--version",)
    version_pattern: str = r"^\s*(\d+\.\d+\.\d+)\s*$"
    capability_cap: Availability = Availability.UNSUPPORTED
    cap_reason_code: str = "unsupported_by_contract"
    cap_reason: str = ""
    verified_versions: frozenset[str] = frozenset()
    notes: tuple[str, ...] = ()
    #: ready 定级时 probe detail 的能力门句式（各家门的语义不同，如实分写）。
    gate_summary: str = "禁工具/隔离/认证形态/进程回收均有实证"

    def parse_version(self, raw: str) -> str | None:
        """从版本命令首行非空输出解析版本号；不认识返回 None。"""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(self.version_pattern, line)
            return m.group(1) if m else None
        return None


@dataclass(slots=True)
class RuntimeInfo:
    """一次探测产出的运行时事实。探测不发送数据，也不修改用户 CLI 配置。"""

    id: str
    display_name: str
    identity: str
    identity_evidence: str
    official_docs: str
    availability: Availability
    reason_code: str
    detail: str
    executable: str | None = None          # 参与版本探测的入口路径（可为 wrapper）
    resolved_target: str | None = None     # realpath 之后的最终目标
    version: str | None = None
    adapter_contract_version: int = ADAPTER_CONTRACT_VERSION
    auth_status: AuthStatus = AuthStatus.UNKNOWN
    capability_cap: Availability = Availability.UNSUPPORTED
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "display_name": self.display_name,
            "identity": self.identity,
            "identity_evidence": self.identity_evidence,
            "official_docs": self.official_docs,
            "availability": self.availability.value,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "executable": self.executable,
            "resolved_target": self.resolved_target,
            "version": self.version,
            "adapter_contract_version": self.adapter_contract_version,
            "auth_status": self.auth_status.value,
            "capability_cap": self.capability_cap.value,
            "notes": list(self.notes),
        }
        return d


@dataclass(slots=True)
class Invocation:
    """一次受控调用的完整入参；禁止命令拼接，argv 必须是参数数组。"""

    argv: list[str]
    cwd: Path | None
    env: Mapping[str, str]
    stdin_bytes: bytes | None = None


@dataclass(slots=True)
class RunResult:
    """runner 的原始运行结果；应用层语义由适配器解析。"""

    outcome: RunOutcome
    exit_code: int | None
    stdout_text: str
    stderr_text: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    wall_ms: int = 0
    group_reaped: bool = False
    detail: str = ""


@dataclass(slots=True)
class ParsedOutput:
    """适配器对 RunResult 的结构验证结论。"""

    ok: bool
    reason_code: str | None = None
    value: dict | None = None
    detail: str = ""


class UnknownCandidateError(KeyError):
    """注册表中不存在的候选 ID。"""


class UnsupportedRuntimeError(RuntimeError):
    """对未通过能力门的 Runtime 请求构建调用。"""


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------

_C = Availability.UNSUPPORTED
_R = Availability.READY

#: 四家候选注册表。证据基线：worktree evidence/capability-matrix.md（2026-09-28 探测）。
CANDIDATES: dict[str, CandidateMeta] = {
    "claude-code": CandidateMeta(
        id="claude-code",
        display_name="Claude Code",
        binary_name="claude",
        identity="Anthropic Claude Code（官方 CLI）",
        identity_evidence="--version 输出 'x.y.z (Claude Code)'；官方 CLI reference 已核对",
        official_docs="https://code.claude.com/docs/en/cli-reference",
        known_locations=(
            "~/.local/bin/claude",
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
        ),
        version_pattern=r"^\s*(\d+\.\d+\.\d+(?:\.\d+)?)\s+\((?:Claude Code|claude-code)\)\s*$",
        capability_cap=_R,
        cap_reason_code="verified_version",
        verified_versions=frozenset({"2.1.237"}),
        notes=(
            "唯一 ready 家：--bare + --disable-slash-commands + --disallowedTools * + "
            "--strict-mcp-config + --no-session-persistence + --output-format json",
            "禁工具实证：stream-json init 事件 tools=[]，全程 0 个 tool_use",
            "上下文隔离实证：--bare 下隔离 cwd 的标记 CLAUDE.md 未进入回复（决定性负例）",
            "认证：--bare 下依赖用户 settings.json env 段注入；OAuth/keychain 不读取，"
            "OAuth-only 形态未验证（auth_status 恒为 unknown 直至真实合成请求成功）",
            "~/.claude.json 复核（2026-09-29 实测）：--bare 组合不写（4 次验证）；"
            "--version 不写；无 --bare 的最小形态会被 CLI 写入自身元数据"
            "（启动计数/项目信任与统计/feature 缓存）。Fathom 不读写该文件",
            "stderr 的 unrecognized_model 警告源于用户自定义网关；模型身份只取结果字段",
        ),
    ),
    "zcode": CandidateMeta(
        id="zcode",
        display_name="ZCode",
        binary_name="zcode",
        identity="Z.ai（智谱）ZCode，GLM 官方 Harness；独立产品，不是腾讯 CodeBuddy",
        identity_evidence="zcode.z.ai 官方文档；本机另装 @tencent-ai/codebuddy-code 为不同产品",
        official_docs="https://zcode.z.ai",
        known_locations=("~/.local/bin/zcode", "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"),
        version_pattern=r"^\s*(\d+\.\d+\.\d+)\s*$",
        capability_cap=_C,
        cap_reason_code="no_readonly_enforcement",
        cap_reason=(
            "0.16.9 实测（DEC-030 防破坏门重审，stream-json 事件流+文件系统对照）："
            "--mode plan 能把内置写类（Write/Edit/Bash 写命令）在权限层真实封死"
            "（直白/伪装/重定向/解释器写诱导全部零副作用，只读 Bash 命令有逐命令"
            "白名单分析照常放行）；但同一模式系统层放行全部未自我声明破坏性的"
            "MCP 工具（plan 下 MCP 检索工具真实执行成功，而 node_repl 经 JS 写"
            "文件在 yolo 对照实测可行）；ZCode 无禁用 MCP 加载的 CLI 参数"
            "（--strict-mcp-config 类不存在），--disallowed-tools 仅支持逐个"
            "全名枚举且用户 MCP 工具集随环境动态扩展（实测 5 个 MCP 服务器），"
            "枚举不可穷尽——MCP 写路径仅剩模型自律，非系统级硬边界"
        ),
        notes=(
            "入口为 node shebang 脚本，精简 PATH 下 shebang 解释器解析可能失败",
            "ISS-035E 2026-09-29 终审（0.16.9）：事件流可观察推翻 035A 的"
            "「不可观察」论据，但可观察后证实禁工具设计性无效——权限检查为"
            "disallowedTools 集合精确匹配（Set.has），无全禁/白名单机制；"
            "实验证据见 035E worktree evidence/（05-10 号）",
            "ISS-126 2026-09-30 DEC-030 防破坏门重审（0.16.9）：11 组行为矩阵"
            "（plan 单独/叠加禁用/破坏诱导/MCP 强诱导/yolo 对照）+ 静态判定链"
            "复核。--mode plan 对内置写类有效：checkPlanMode 对非只读非 MCP 工具"
            "deny(mode.plan.nonReadOnly)，Bash 有逐命令只读分析器"
            "（isRuntimeReadOnlyBashCommand，无 python/node 等解释器，rm/mv/"
            "重定向不在白名单）；但 checkPlanMode 对 permissionName===\"mcp\""
            "且非破坏的工具无条件 allow(mode.plan.mcp)，MCP 工具 destructive "
            "仅取协议 annotations.destructiveHint（缺省 false），无 MCP 禁用"
            "参数，全名枚举叠加有效（M2b 实测移除 node_repl）但不可穷尽",
            "判不过门与 Codex 的本质差异：Codex read-only 是 OS 级硬边界"
            "（模型发起写仍被系统拒绝）；ZCode plan 下 MCP 写路径仅由模型自律"
            "隔离（诱导实验全部零副作用是模型未发起，非系统拦截）",
            "复核条件（满足任一后重跑 M1-M4 负例矩阵）：官方提供 MCP 禁用/"
            "白名单 CLI 参数（--strict-mcp-config 类或 --allowed-tools）；或 "
            "mode.plan.mcp 分支收紧为需声明 readOnlyHint=true 才放行；或 "
            "--disallowed-tools 支持 mcp__* 服务器级通配",
        ),
    ),
    "codex-cli": CandidateMeta(
        id="codex-cli",
        display_name="Codex CLI",
        binary_name="codex",
        identity="OpenAI Codex CLI",
        identity_evidence="codex --version 输出 'codex-cli x.y.z'；OpenAI 官方仓库",
        official_docs="https://learn.chatgpt.com/docs/sandboxing",
        known_locations=(
            "~/.local/bin/codex",
            "/opt/homebrew/bin/codex",
            "~/.hermes/node/bin/codex",
            "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
        ),
        version_pattern=r"^\s*codex-cli\s+(\d+\.\d+\.\d+)\s*$",
        capability_cap=_R,
        cap_reason_code="verified_version",
        cap_reason=(
            "DEC-030 防破坏能力门下的开放依据：-s read-only 为 OS 级 Seatbelt 只读"
            "沙箱，写文件/删除/破坏性命令被模型层规避且被 OS 层拒绝（e1d 决定性对照："
            "读命令 exit=0 真实执行、写步骤模型改跑 ls 核查并如实报告不存在；"
            "e1e/e1f/e3 模型转述的 OS 拒绝形态（PermissionError/operation not permitted）；证据 worktree evidence/"
            "REPORT-pm-negatives.md，2026-09-30）；读取任意本机文件与联网是用户"
            "裁决（2026-09-30）允许的能力，在设置页如实披露，不作失败条件"
        ),
        verified_versions=frozenset({"0.147.0"}),
        gate_summary=(
            "OS 级 read-only 沙箱防破坏（写/删/破坏命令负例有实证）+ 隔离/"
            "不落盘/进程回收实证"
        ),
        notes=(
            "入口探测：~/.local/bin/codex 为用户自建 wrapper（exit 127 且硬编码注入"
            " danger-full-access，与 Fathom 合同相反，产品绝不复用），探测按序落到"
            " ~/.hermes/node/bin/codex（@openai/codex 真实候选，2026-09-29 复核）",
            "调用形态（ISS-126 实测，2026-09-30）：exec --json --ephemeral "
            "--ignore-user-config --ignore-rules --skip-git-repo-check -s read-only "
            "-C <隔离cwd> -c model_reasoning_effort=low，载荷经 stdin；提示行与"
            "内部日志进 stderr，stdout 为纯 JSONL 事件流（-c 值 TOML 解析失败回退"
            "字面量，裸 low 实测被接受）",
            "防破坏实证（PM 负例，2026-09-30）：e1b/e1c 直白与无害外观写诱导模型"
            "拒绝发起；e1d 步骤 1 证明命令通道可用（排除「模型没能力执行」的替代"
            "解释）、步骤 2 写被规避且文件未创建；e2 删除诱导全规避；OS 层旁证"
            " 035E E4 同 Seatbelt 机制 deny profile 下命令 exit=134 被直接拒绝",
            "ISS-035E 旧合同（禁全部工具）终审结论历史有效：工具面在任何配置组合"
            "下不归零（E5 内建 MCP 通道仍可被调用）、无工具级禁用参数；DEC-030"
            " 修订后防破坏由 read-only 沙箱在 OS 层保证，不再以「禁全部工具」为门",
            "网络与认证：本机→ChatGPT 后端 WebSocket 常断连并自动降级 HTTPS"
            "（error item 'Falling back...' 与成功回复共存属实测正常形态，适配器"
            "仅在响应缺失时判失败）；认证复用 CODEX_HOME 下用户 auth.json，"
            "--ephemeral 不落盘会话、--ignore-user-config 不加载用户 config.toml",
        ),
    ),
    "hermes-agent": CandidateMeta(
        id="hermes-agent",
        display_name="Hermes Agent",
        binary_name="hermes",
        identity="Nous Research hermes-agent（GitHub NousResearch/hermes-agent，git 安装）",
        identity_evidence="--version 输出含 v0.21.5+git 描述；GitHub 仓库对应",
        official_docs="https://github.com/NousResearch/hermes-agent",
        known_locations=("~/.local/bin/hermes", "~/.hermes/hermes-agent/venv/bin/hermes"),
        version_pattern=r"^\s*Hermes Agent\s+v?(\d+\.\d+\.\d+)\S*\s+\(\d{4}\.\d+\.\d+\)",
        capability_cap=_R,
        cap_reason_code="verified_version",
        cap_reason=(
            "DEC-030 防破坏能力门下的开放依据：``chat -t web,vision`` 把工具面"
            "替换收敛为 {web_search, web_extract, vision_analyze} 只读三件——"
            "-t 非空值为替换语义（源码 cli.py _build_cli_from_args：空值才回退"
            "平台默认），MCP 服务器因不在名单被整体跳过；直白/无害外观/伪装"
            "三组写诱导与删除诱导零 tool_use、目标文件零变化，web_search 通道"
            "对照真实执行成功（排除「模型不会调工具」替代解释）；--safe-mode "
            "单独使用时 terminal/write_file/rm 无审批照常执行（反例在档），"
            "不构成防破坏边界。证据 worktree evidence/iss-126-hermes-20260930/"
            "REPORT.md，2026-09-30；读取任意本机文件与联网是用户裁决允许的能力"
        ),
        verified_versions=frozenset({"0.21.5"}),
        gate_summary=(
            "调用级只读工具集收敛防破坏（写/删/破坏命令负例有实证）+ "
            "隔离/进程回收实证"
        ),
        notes=(
            "venv python 入口，精简 PATH 下 shebang 解释器解析可能失败",
            "调用形态（ISS-126 实测，2026-09-30）：chat --query-file - "
            "--format stream-json --run-budget 150 -t web,vision "
            "--ignore-user-config --ignore-rules --source tool，载荷经 stdin；"
            "stdout 为 JSONL 事件流（system/tool_use/tool_result/text/result），"
            "stderr 仅 session_id 一行",
            "工具集收敛实证：-t web,vision 下 input tokens 7254（默认工具集 "
            "17467），模型自述可用工具恰为 web_search/web_extract/vision_analyze；"
            "-t 非空为替换语义，MCP 因不在名单被跳过（源码 "
            "mcp_tool_discovery.discover_mcp_tools 的 allowed_mcp_names 过滤）",
            "防破坏实证（ISS-126 负例，2026-09-30）：直白写（h1a/r2）、无害"
            "外观写（h1b ls|tee）、伪装官方诊断写（h1d python）、删除诱导（m4 "
            "「删除目录所有文件」）全部零 tool_use、文件零变化；通道对照 h1c "
            "web_search 真实执行 success=true",
            "--safe-mode 单独使用反例（h2a/h2b）：terminal `ls -la | tee` 与 "
            "write_file 均真实写出文件、`rm` 无审批执行成功——safe-mode 只关"
            "定制/插件/MCP（HERMES_SAFE_MODE+IGNORE_USER_CONFIG+IGNORE_RULES），"
            "不动内建工具集，非防破坏边界；因此适配器不可省略 -t",
            "035E 第三轮实验证据 verify-results/agent-runtime-035e-hermes-20260929/ "
            "历史有效：-t \"\" 空值回退默认不禁工具、hermes tools 子命令=全局持久"
            "配置（仍禁用）；DEC-030 修订后防破坏由 -t web,vision 替换收敛保证",
            "披露：失败请求会在用户 ~/.hermes/sessions/ 留 request_dump_*.json"
            "（成功 oneshot 不落会话文件，实测 8 次成功运行 0 新增）；"
            "实验窗口内 config.yaml mtime 一次触碰但 sha256 前后一致",
            "进程回收：hermes 启动期会自建辅助子进程，超时/取消路径 TERM 宽限"
            "3s 后 KILL（wall≈5s）；group_reaped 偶发 false（组消失确认竞态），"
            "两次 ps 复查零残留，共享 runner 机制未改（ISS-126 实测）",
            "复核条件：-t 工具集替换语义变更、或出现绕过 toolset 收敛的写入"
            "路径（如插件注入）后重审（ISS-126）",
        ),
    ),
}


def get_candidate(candidate_id: str) -> CandidateMeta:
    """按 ID 取注册表条目；未知 ID 一律拒绝。"""
    try:
        return CANDIDATES[candidate_id]
    except KeyError:
        raise UnknownCandidateError(candidate_id) from None


# --------------------------------------------------------------------------
# 公共 runner（方案 §5 运行合同）
# --------------------------------------------------------------------------

#: 父进程存活看门（shim）源码。由 runner 以 ``<python> -c <源码> <期限秒>
#: <宽限秒> <管道fd> -- <目标argv...>`` 启动，并经 start_new_session 成为
#: 独立进程组长。职责：同进程组启动目标 CLI（继承 0/1/2）；监听父进程
#: 存活管道（只读端）——父进程死亡即 EOF；到达墙钟期限或 EOF 时对本进程
#: 组 SIGTERM，宽限期后仍存活则 SIGKILL；目标退出则转发退出码并 reap。
#: 生产打包（PyInstaller 冻结 helper）下 ``sys.executable -c`` 不可用的
#: 接缝由 ISS-035B 以冻结包内可用解释器接线；本模块把 shim 命令做成
#: 可注入参数，不让打包形态阻塞本卡验收。
_SUPERVISOR_SOURCE = r'''
import os, signal, subprocess, sys, time
from select import select

signal.signal(signal.SIGTERM, signal.SIG_IGN)  # 自身必须活到能升级 SIGKILL
deadline = time.monotonic() + float(sys.argv[1])
grace = float(sys.argv[2])
pipe_fd = int(sys.argv[3])
argv = sys.argv[4:]
if argv and argv[0] == "--":
    argv = argv[1:]
pgid = os.getpgrp()
proc = subprocess.Popen(argv, close_fds=True)


def _exit_code(rc: int) -> int:
    return rc if 0 <= rc < 256 else (128 - rc if rc < 0 else rc & 0xFF)


def _kill_group(sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


while True:
    rc = proc.poll()
    if rc is not None:
        os._exit(_exit_code(rc))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        break
    readable, _, _ = select([pipe_fd], [], [], min(remaining, 0.25))
    if readable and os.read(pipe_fd, 1) == b"":
        break  # 父进程写端关闭：父进程已死，子任务必须有界退出

_kill_group(signal.SIGTERM)
stop = time.monotonic() + grace
rc = None
while time.monotonic() < stop:
    rc = proc.poll()
    if rc is not None:
        break
    time.sleep(0.02)
if rc is None:
    _kill_group(signal.SIGKILL)
    rc = proc.wait()
os._exit(_exit_code(rc))
'''


def run_supervisor_command(raw_args: list[str]) -> int:
    """看门 shim 的进程内入口（ISS-035B 冻结包接缝）。

    PyInstaller 冻结 helper 下 ``sys.executable -c <源码>`` 不可用（冻结
    解释器不认 -c）；analysis_manager 的 ``production_shim_argv_factory``
    在 ``sys.frozen`` 时改以 ``sys.executable _agent-supervisor <参数…>``
    启动看门——冻结 helper 自身充当 shim。本入口经 ``exec`` 执行与
    ``-c`` 形态**同一份** ``_SUPERVISOR_SOURCE``（单一实现，禁止两份漂移），
    argv 合同完全一致：``<deadline_s> <grace_s> <pipe_fd> -- <目标argv…>``。
    源码以 ``os._exit`` 终止，本函数不会正常返回。

    仅供该子命令调用；不构成产品功能面。
    """
    sys.argv = ["fathom-agent-supervisor", *raw_args]
    code = compile(_SUPERVISOR_SOURCE, "<fathom-agent-supervisor>", "exec")
    exec(code, {"__name__": "__fathom_supervisor__"})  # noqa: S102 - 内部固定源码
    return 0  # 不可达：源码恒以 os._exit 终止


def _normalize_exit_code(rc: int | None) -> int | None:
    """负返回值（被信号杀死）规范为 128+N 惯例；None 原样返回。"""
    if rc is None:
        return None
    if rc < 0:
        return 128 - rc
    return rc


class AgentCliRunner:
    """第三方 CLI 的统一受控执行器。

    - shell=False 参数数组；cwd/env 显式给定（env 应为最小白名单）。
    - ``start_new_session=True`` 建独立进程组；超时/取消对整组
      SIGTERM → ``term_grace_s`` → SIGKILL，终态前 reap 直接子进程并
      确认进程组消失（killpg(pgid, 0) 轮询，有界次数）。
    - liveness 开启时经 ``_SUPERVISOR_SOURCE`` shim 启动：父进程死亡
      （写端 EOF）或墙钟到期同样触发组级有界回收，异常父进程消失时
      子任务不会无限期存活。
    - stdout/stderr 分离收集、边读边限额；任一超限立即终止进程组，
      结局 OUTPUT_LIMIT。UTF-8 严格解码，坏字节即 DECODE_ERROR。
    - 超时/取消与正常结束、非零退出可区分；应用层错误由适配器在
      exit=0 的基础上判定（三关中的第二、三关）。
    """

    def __init__(
        self,
        *,
        stdout_max_bytes: int = STDOUT_MAX_BYTES,
        stderr_max_bytes: int = STDERR_MAX_BYTES,
        term_grace_s: float = TERM_GRACE_S,
        liveness_watch: bool = True,
        popen_factory: Callable[..., subprocess.Popen] | None = None,
        shim_argv_factory: Callable[[str, str, str, list[str]], list[str]] | None = None,
    ) -> None:
        self._stdout_max = int(stdout_max_bytes)
        self._stderr_max = int(stderr_max_bytes)
        self._term_grace_s = float(term_grace_s)
        self._liveness = bool(liveness_watch)
        self._popen = popen_factory or subprocess.Popen
        # 035B 冻结包接缝：默认用 CPython ``-c`` 启动看门；PyInstaller 冻结
        # helper 下 sys.executable -c 不可用，届时注入冻结包内的等价命令。
        self._shim_argv_factory = shim_argv_factory or self._default_shim_argv

    @staticmethod
    def _default_shim_argv(deadline_s: str, grace_s: str, pipe_fd: str, target: list[str]) -> list[str]:
        return [sys.executable, "-c", _SUPERVISOR_SOURCE, deadline_s, grace_s, pipe_fd, "--", *target]

    def run(
        self,
        invocation: Invocation,
        *,
        timeout_s: float = RUN_DEFAULT_TIMEOUT_S,
        cancel_event: threading.Event | None = None,
    ) -> RunResult:
        """同步执行一次调用；本方法只负责进程生命周期与输出采集。"""
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须为正")
        if not invocation.argv or not all(isinstance(a, str) for a in invocation.argv):
            raise ValueError("argv 必须是非空字符串数组")

        started = time.monotonic()
        liveness_pipe_r = liveness_pipe_w = None
        if self._liveness:
            liveness_pipe_r, liveness_pipe_w = os.pipe()
            os.set_inheritable(liveness_pipe_r, True)
            shim_deadline_s = timeout_s + self._term_grace_s + 5.0
            argv = self._shim_argv_factory(
                repr(shim_deadline_s), repr(self._term_grace_s), str(liveness_pipe_r),
                list(invocation.argv),
            )
        else:
            argv = list(invocation.argv)

        stdin_data = invocation.stdin_bytes
        popen_kwargs: dict = dict(
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(invocation.cwd) if invocation.cwd is not None else None,
            env=dict(invocation.env) if invocation.env is not None else None,
            shell=False,
            start_new_session=True,
        )
        if liveness_pipe_r is not None:
            popen_kwargs["pass_fds"] = (liveness_pipe_r,)

        try:
            proc = self._popen(argv, **popen_kwargs)
        except OSError as exc:
            if liveness_pipe_w is not None:
                os.close(liveness_pipe_w)
            return RunResult(
                outcome=RunOutcome.SPAWN_FAILED, exit_code=None,
                stdout_text="", stderr_text="", wall_ms=0,
                detail=f"spawn 失败：{exc}",
            )
        finally:
            if liveness_pipe_r is not None:
                os.close(liveness_pipe_r)  # 父进程只保留写端；读端归 shim

        stdin_thread: threading.Thread | None = None
        if stdin_data is not None and proc.stdin is not None:
            stdin_thread = threading.Thread(
                target=self._feed_stdin, args=(proc, stdin_data), daemon=True
            )
            stdin_thread.start()

        outcome, stdout_buf, stderr_buf, out_trunc, err_trunc = self._collect_output(
            proc, timeout_s, cancel_event
        )
        if stdin_thread is not None:
            stdin_thread.join(timeout=1.0)

        group_reaped = self._terminate_group(proc)
        exit_code = _normalize_exit_code(proc.returncode)
        if liveness_pipe_w is not None:
            os.close(liveness_pipe_w)  # 触发 shim 侧 EOF（正常完成时 shim 已退出）

        detail = ""
        stdout_text = stderr_text = ""
        try:
            stdout_text = self._strict_decode(stdout_buf)
            stderr_text = self._strict_decode(stderr_buf)
        except UnicodeDecodeError as exc:
            if out_trunc or err_trunc:
                # 截断恰好切在多字节字符中间：结局保持 OUTPUT_LIMIT；
                # 此时的文本只作诊断，不会进入结果验证（结局非 OK）。
                stdout_text = stdout_buf.decode("utf-8", "replace")
                stderr_text = stderr_buf.decode("utf-8", "replace")
            else:
                outcome = RunOutcome.DECODE_ERROR
                detail = f"输出含非法 UTF-8 字节：{exc}"

        if outcome is RunOutcome.OK and exit_code != 0:
            outcome = RunOutcome.NONZERO_EXIT

        return RunResult(
            outcome=outcome,
            exit_code=exit_code,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            stdout_truncated=out_trunc,
            stderr_truncated=err_trunc,
            wall_ms=int((time.monotonic() - started) * 1000),
            group_reaped=group_reaped,
            detail=detail,
        )

    # ---- 内部实现 ----

    @staticmethod
    def _feed_stdin(proc: subprocess.Popen, data: bytes) -> None:
        """独立线程写 stdin，避免载荷大而子进程输出并发写满管道时互锁。"""
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # 子进程提前退出属正常路径
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    def _collect_output(
        self,
        proc: subprocess.Popen,
        timeout_s: float,
        cancel_event: threading.Event | None,
    ) -> tuple[RunOutcome, bytearray, bytearray, bool, bool]:
        """边读边限额地收集两路输出；返回 (初步结局, 两路字节, 截断标记)。"""
        deadline = time.monotonic() + timeout_s
        bufs = {"out": bytearray(), "err": bytearray()}
        caps = {"out": self._stdout_max, "err": self._stderr_max}
        truncated = {"out": False, "err": False}
        outcome: RunOutcome | None = None

        sel = selectors.DefaultSelector()
        streams = {"out": proc.stdout, "err": proc.stderr}
        for key, fh in streams.items():
            os.set_blocking(fh.fileno(), False)
            sel.register(fh, selectors.EVENT_READ, key)
        open_keys = set(streams)

        try:
            while open_keys and outcome is None:
                now = time.monotonic()
                wait = max(0.0, min(0.1, deadline - now))
                if wait == 0.0 and now >= deadline:
                    outcome = RunOutcome.TIMED_OUT
                    break
                for key, _ in sel.select(wait):
                    name = key.data
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except (BlockingIOError, OSError):
                        continue
                    if not chunk:
                        open_keys.discard(name)
                        sel.unregister(key.fileobj)
                        continue
                    buf = bufs[name]
                    room = caps[name] - len(buf)
                    if room <= 0:
                        truncated[name] = True  # 继续排水防止子进程写阻塞，但不再存储
                        continue
                    if len(chunk) > room:
                        buf.extend(chunk[:room])
                        truncated[name] = True
                        continue
                    buf.extend(chunk)
                if outcome is None and (truncated["out"] or truncated["err"]):
                    outcome = RunOutcome.OUTPUT_LIMIT
                if outcome is None and cancel_event is not None and cancel_event.is_set():
                    outcome = RunOutcome.CANCELLED
                if outcome is None and time.monotonic() >= deadline:
                    outcome = RunOutcome.TIMED_OUT
        finally:
            sel.close()
        return (outcome or RunOutcome.OK), bufs["out"], bufs["err"], truncated["out"], truncated["err"]

    @staticmethod
    def _strict_decode(data: bytes) -> str:
        """UTF-8 严格解码；坏字节抛 UnicodeDecodeError，绝不静默替换。"""
        return codecs.getincrementaldecoder("utf-8")("strict").decode(data, final=True)

    def _terminate_group(self, proc: subprocess.Popen) -> bool:
        """SIGTERM → 宽限 → SIGKILL 回收自建进程组；终态前 reap 并确认组消失。"""
        pgid = proc.pid  # start_new_session 下 pid 即 pgid
        # macOS 对「子进程组已退出但尚未 reap」的组发信号返回 EPERM：
        # 此时组内已无存活进程，按组已消失继续，不放大为运行失败
        # （仅 PermissionError；其余 OSError 原样上抛，不吞真异常）。
        if proc.poll() is None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                _LOG.debug("killpg SIGTERM EPERM（组已退出未 reap）pgid=%s：%s", pgid, exc)
            stop = time.monotonic() + self._term_grace_s
            while proc.poll() is None and time.monotonic() < stop:
                time.sleep(0.02)
            if proc.poll() is None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    _LOG.debug("killpg SIGKILL EPERM（组已退出未 reap）pgid=%s：%s", pgid, exc)
        try:
            proc.wait(timeout=10.0)  # reap 直接子进程，防僵尸
        except subprocess.TimeoutExpired:
            _LOG.warning("runner 终态前 reap 超时 pgid=%s", pgid)
        group_gone = False
        for _ in range(10):  # 有界确认组内已无进程；killpg(pid,0) 仅探测不发信号
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                group_gone = True
                break
            except PermissionError:
                break  # 组仍在且含他人进程：如实报告未清，不做更多动作
            time.sleep(0.05)
        return group_gone


# --------------------------------------------------------------------------
# 探测（方案 §3.1）
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _ProbeFailure:
    """单个候选路径探测失败记录（进 detail，不抛异常）。"""

    path: str
    reason: str


def _iter_candidate_paths(
    meta: CandidateMeta,
    *,
    path_env: str | None,
    extra_locations: Sequence[str | os.PathLike[str]],
    cap: int,
    notes: list[str],
    precheck_failures: list[_ProbeFailure],
) -> list[str]:
    """收集候选入口：显式附加位置 → 已知安装位置 → PATH 枚举（去重、限量）。"""
    locations: list[str] = [str(p) for p in extra_locations]
    locations.extend(os.path.expanduser(str(p)) for p in meta.known_locations)
    for d in (path_env if path_env is not None else os.environ.get("PATH", "")).split(os.pathsep):
        if d:
            locations.append(os.path.join(d, meta.binary_name))

    seen_targets: set[str] = set()
    out: list[str] = []
    for loc in locations:
        if len(out) >= cap:
            notes.append(f"候选数达到预算上限 {cap}，其余位置未枚举")
            break
        if not os.path.isfile(loc):
            continue
        if not os.access(loc, os.X_OK):
            precheck_failures.append(_ProbeFailure(loc, "文件存在但不可执行"))
            continue
        target = os.path.realpath(loc)  # 符号链接解析；失效链接 realpath 仍可算，交由版本探测暴露
        if target in seen_targets:
            continue  # 同目标重名去重；不同目标的重名保留并按顺序尝试
        seen_targets.add(target)
        out.append(loc)
    return out


def _version_probe(
    path: str,
    meta: CandidateMeta,
    budget: ProbeBudget,
    runner: AgentCliRunner,
    deadline: float,
    notes: list[str],
) -> tuple[RuntimeInfo, _ProbeFailure | None]:
    """在预算内运行版本命令并按注册表策略定级；探测绝不发送业务数据。"""
    if time.monotonic() >= deadline:
        return (
            RuntimeInfo(
                id=meta.id, display_name=meta.display_name, identity=meta.identity,
                identity_evidence=meta.identity_evidence, official_docs=meta.official_docs,
                availability=Availability.NOT_FOUND, reason_code="probe_budget_exhausted",
                detail="探测总预算已用尽，未完成版本核实",
                capability_cap=meta.capability_cap,
            ),
            _ProbeFailure(path, "总预算已用尽"),
        )

    probe_cwd = Path(tempfile.mkdtemp(prefix="fathom-probe-"))  # 中性隔离 cwd，避免加载任何目录内容
    try:
        result = runner.run(
            Invocation(
                argv=[path, *meta.version_args],
                cwd=probe_cwd,
                env=_probe_env(),
                stdin_bytes=None,
            ),
            timeout_s=budget.per_command_timeout_s,
        )
    finally:
        shutil.rmtree(probe_cwd, ignore_errors=True)

    if result.outcome is not RunOutcome.OK or result.exit_code != 0:
        reason = f"版本命令失败（{result.outcome.value}，exit={result.exit_code}）"
        if result.stderr_text:
            reason += f"；stderr 首行：{result.stderr_text.splitlines()[0][:120]}"
        return (
            RuntimeInfo(
                id=meta.id, display_name=meta.display_name, identity=meta.identity,
                identity_evidence=meta.identity_evidence, official_docs=meta.official_docs,
                availability=Availability.BROKEN, reason_code="version_probe_failed",
                detail=f"{path}：{reason}",
                executable=path, resolved_target=os.path.realpath(path),
                capability_cap=meta.capability_cap,
            ),
            _ProbeFailure(path, reason),
        )

    version = meta.parse_version(result.stdout_text)
    resolved = os.path.realpath(path)
    base = dict(
        id=meta.id, display_name=meta.display_name, identity=meta.identity,
        identity_evidence=meta.identity_evidence, official_docs=meta.official_docs,
        executable=path, resolved_target=resolved,
        capability_cap=meta.capability_cap,
    )
    if version is None:
        return (
            RuntimeInfo(
                availability=Availability.UNSUPPORTED, reason_code="version_unrecognized",
                detail=f"{path} 可执行但版本输出无法识别，身份未确认；未知版本默认 unsupported",
                **base,
            ),
            None,
        )
    if meta.capability_cap is not Availability.READY:
        return (
            RuntimeInfo(
                availability=Availability.UNSUPPORTED, reason_code=meta.cap_reason_code,
                detail=f"{path} 版本 {version}；{meta.cap_reason}",
                version=version, notes=meta.notes, **base,
            ),
            None,
        )
    if version not in meta.verified_versions:
        return (
            RuntimeInfo(
                availability=Availability.UNSUPPORTED, reason_code="version_unverified",
                detail=(f"{path} 版本 {version} 不在已验证集合 "
                        f"{sorted(meta.verified_versions)} 内；未验证版本不开放"),
                version=version, notes=meta.notes, **base,
            ),
            None,
        )
    return (
        RuntimeInfo(
            availability=Availability.READY, reason_code="verified_version",
            detail=f"{path} 版本 {version} 通过能力门（{meta.gate_summary}）",
            version=version, notes=meta.notes, **base,
        ),
        None,
    )


def _probe_env() -> dict[str, str]:
    """版本探测用最小环境；与生产调用同一白名单口径（PATH+HOME）。"""
    try:
        return build_minimal_env()
    except EnvError:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


class EnvError(RuntimeError):
    """构建最小环境失败（缺 PATH/HOME）。"""


def build_minimal_env(
    *,
    path: str | None = None,
    home: str | None = None,
) -> dict[str, str]:
    """生产调用的环境白名单：仅 PATH + HOME。

    认证不在此注入：Claude 的认证依赖用户 ``~/.claude/settings.json`` 的
    env 段（CLI 自行读取），Fathom 不读取、不复制、不回显任何凭据。
    """
    path = path if path is not None else os.environ.get("PATH")
    home = home if home is not None else os.environ.get("HOME")
    if not path:
        raise EnvError("缺少 PATH，无法构建最小运行环境")
    if not home:
        raise EnvError("缺少 HOME：CLI 需要 HOME 定位其自身认证与配置")
    return {"PATH": path, "HOME": home}


def _login_shell_lookup(
    meta: CandidateMeta,
    budget: ProbeBudget,
    runner: AgentCliRunner,
    deadline: float,
    shell_cmd: Sequence[str],
    notes: list[str],
) -> str | None:
    """按需的登录 shell 探测：``<shell> -lc 'command -v <binary_name>'``。

    登录 shell 会执行用户启动脚本，因此单独受预算约束（3 秒/16 KiB），
    失败如实记录且不放大自动探测范围；输出只接受单行绝对路径，
    绝不 eval。
    """
    if time.monotonic() >= deadline:
        notes.append("总预算已用尽，跳过登录 shell 探测")
        return None
    shell = list(shell_cmd)
    probe_cwd = Path(tempfile.mkdtemp(prefix="fathom-probe-"))
    try:
        result = runner.run(
            Invocation(
                argv=[*shell, "-lc", f"command -v {meta.binary_name}"],
                cwd=probe_cwd,
                env=_probe_env(),
                stdin_bytes=None,
            ),
            timeout_s=budget.per_command_timeout_s,
        )
    finally:
        shutil.rmtree(probe_cwd, ignore_errors=True)
    if result.outcome is not RunOutcome.OK or result.exit_code != 0:
        notes.append(
            f"登录 shell 探测未命中（{result.outcome.value}"
            f"{f'，{result.detail}' if result.detail else ''}）"
        )
        return None
    candidate = result.stdout_text.strip()
    if (
        not candidate
        or "\n" in candidate
        or not candidate.startswith("/")
        or any(ch in candidate for ch in ";|&`$><\\'\"")
        or not os.path.isfile(candidate)
    ):
        notes.append("登录 shell 输出不是可信的绝对路径，已忽略（绝不 eval 探测结果）")
        return None
    return candidate


def detect_runtime(
    meta: CandidateMeta,
    *,
    budget: ProbeBudget | None = None,
    deadline: float | None = None,
    path_env: str | None = None,
    extra_locations: Sequence[str | os.PathLike[str]] = (),
    login_shell_cmd: Sequence[str] | None = None,
    runner: AgentCliRunner | None = None,
) -> RuntimeInfo:
    """探测单个候选并返回 RuntimeInfo。

    ``path_env``/``extra_locations``/``login_shell_cmd`` 供测试注入合成
    环境；生产路径留空即用当前进程环境。探测顺序：受限已知位置与 PATH
    枚举 → 逐候选版本探测（按序取第一个可定级者）→ 均失败时按需登录
    shell 探测一次。
    """
    budget = budget or ProbeBudget()
    runner = runner or AgentCliRunner(
        liveness_watch=False,
        # 方案 §3.1 探测预算：单命令输出 16 KiB，同时约束 stdout 与 stderr
        # （登录 shell 探测同样经此 runner，受同一上限）。产品运行合同的
        # 256 KiB/64 KiB 上限只用于探测之外的受控调用，不在这里兜底。
        stdout_max_bytes=budget.per_command_output_cap,
        stderr_max_bytes=budget.per_command_output_cap,
    )
    final_deadline = deadline if deadline is not None else time.monotonic() + budget.total_timeout_s
    notes: list[str] = []
    precheck_failures: list[_ProbeFailure] = []
    probe_failures: list[_ProbeFailure] = []

    candidates = _iter_candidate_paths(
        meta, path_env=path_env, extra_locations=extra_locations,
        cap=budget.max_candidates, notes=notes, precheck_failures=precheck_failures,
    )

    info: RuntimeInfo | None = None
    unrecognized_info: RuntimeInfo | None = None  # 可执行但版本不识别：候选事实，优先级低于可定级者
    for path in candidates:
        info, failure = _version_probe(path, meta, budget, runner, final_deadline, notes)
        if failure is not None:
            probe_failures.append(failure)
            info = None
            continue
        # 版本输出无法识别：换下一个重名目标再试；全部失败时作为 unsupported 事实返回
        if info.reason_code == "version_unrecognized":
            if unrecognized_info is None:
                unrecognized_info = info
            probe_failures.append(_ProbeFailure(path, "版本输出无法识别"))
            info = None
            continue
        break

    if info is None:
        shell = list(login_shell_cmd) if login_shell_cmd else [
            os.environ.get("SHELL") or "/bin/zsh"
        ]
        found = _login_shell_lookup(meta, budget, runner, final_deadline, shell, notes)
        if found is not None:
            info, failure = _version_probe(found, meta, budget, runner, final_deadline, notes)
            if failure is not None:
                probe_failures.append(failure)
                info = None

    if info is None and unrecognized_info is not None:
        # 没有任何候选给出可识别版本：按「未知版本默认 unsupported」如实定级
        if notes:
            unrecognized_info.notes = tuple(unrecognized_info.notes) + tuple(notes)
        return unrecognized_info

    if info is not None:
        if notes:
            info.notes = tuple(info.notes) + tuple(notes)
        return info

    if precheck_failures or probe_failures:
        seen: set[str] = set()
        parts: list[str] = []
        for f in (*precheck_failures, *probe_failures):
            key = (f.path, f.reason)
            if key not in seen:
                seen.add(key)
                parts.append(f"{f.path}：{f.reason}")
        return RuntimeInfo(
            id=meta.id, display_name=meta.display_name, identity=meta.identity,
            identity_evidence=meta.identity_evidence, official_docs=meta.official_docs,
            availability=Availability.BROKEN, reason_code="version_probe_failed",
            detail="；".join(parts),
            capability_cap=meta.capability_cap, notes=tuple(notes),
        )
    return RuntimeInfo(
        id=meta.id, display_name=meta.display_name, identity=meta.identity,
        identity_evidence=meta.identity_evidence, official_docs=meta.official_docs,
        availability=Availability.NOT_FOUND, reason_code="not_found",
        detail=f"已知安装位置与 PATH 中未发现 {meta.binary_name}（登录 shell 亦未命中）",
        capability_cap=meta.capability_cap, notes=tuple(notes),
    )


def probe_all(
    *,
    budget: ProbeBudget | None = None,
    path_env: str | None = None,
) -> dict[str, RuntimeInfo]:
    """探测全部四家；共享总预算（整个检测 20 秒）。"""
    budget = budget or ProbeBudget()
    deadline = time.monotonic() + budget.total_timeout_s
    return {
        cid: detect_runtime(meta, budget=budget, deadline=deadline, path_env=path_env)
        for cid, meta in CANDIDATES.items()
    }


# --------------------------------------------------------------------------
# 适配器（接口即 ISS-035E 的接缝；各家同构，公共校验在基类）
# --------------------------------------------------------------------------


class BaseCliAdapter:
    """适配器公共骨架：候选身份与能力门校验（各家 ``__init__`` 同构）。"""

    id: str = ""

    def __init__(self, info: RuntimeInfo) -> None:
        if info.id != self.id:
            raise UnsupportedRuntimeError(f"适配器 {self.id} 不接受候选 {info.id}")
        if info.availability is not Availability.READY:
            raise UnsupportedRuntimeError(
                f"{info.id} 当前 {info.availability.value}（{info.reason_code}），"
                "未通过能力门，禁止构建调用"
            )
        if not info.executable:
            raise UnsupportedRuntimeError("RuntimeInfo 缺少 executable")
        self.info = info


class ClaudeCodeAdapter(BaseCliAdapter):
    """Claude Code 的 probe/build_invocation/parse_output 适配器。

    调用合同（2.1.237 实证）::

        <claude> --bare --disable-slash-commands --disallowedTools "*"
                 --strict-mcp-config --no-session-persistence
                 --output-format json -p        # stdin 载荷形态
        cwd=<隔离空目录>  env={PATH, HOME}  认证=用户 settings.json env 段

    ``--disallowedTools "*"`` 显式保留是版本退化保护（--bare 已隐含禁工具，
    但未来版本行为变化时显式参数仍是第一道门）；发现任何工具事件即失败
    （stream-json 误用时 stdout 无法通过单 JSON 对象验证，直接拒绝）。
    """

    id = "claude-code"

    def build_invocation(
        self,
        payload: str,
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        payload_mode: str = "stdin",
    ) -> Invocation:
        """构建受控调用。cwd 必须是隔离目录（不含项目指令文件）。"""
        if payload_mode not in ("stdin", "argv"):
            raise ValueError("payload_mode 只支持 stdin/argv")
        if not isinstance(payload, str) or not payload.strip():
            raise ValueError("payload 必须是非空字符串")
        exe = self.info.executable
        flags = [
            "--bare",                      # 跳过 hooks/skills/插件/MCP/记忆/CLAUDE.md
            "--disable-slash-commands",    # 禁 slash 命令
            "--disallowedTools", "*",      # 显式禁全部工具（版本退化保护）
            "--strict-mcp-config",         # 不加载任何 MCP 配置
            "--no-session-persistence",    # 不落盘会话
            "--output-format", "json",     # 单 JSON 对象，可验证 is_error
        ]
        if payload_mode == "stdin":
            # 首选形态：载荷经 stdin，不对同用户其他进程可见；-p 置尾避免吞掉后续 flag。
            argv = [exe, *flags, "-p"]
            stdin_bytes = payload.encode("utf-8")
        else:
            # 披露（方案 §5）：argv 载荷对本机同用户进程（ps 等）可见。
            # 仅允许在 stdin 形态失效且经真实复验后显式选择。
            argv = [exe, "-p", payload, *flags]
            stdin_bytes = None
        return Invocation(
            argv=argv,
            cwd=cwd,
            env=dict(env) if env is not None else build_minimal_env(),
            stdin_bytes=stdin_bytes,
        )

    def parse_output(self, run: RunResult) -> ParsedOutput:
        """三关中的第二、三关：应用层无错误 + 完整结果结构验证。

        stream-json 误用（逐行事件）会在 JSON 解析处失败；单条非 result
        事件在 type 校验处失败——发现工具事件类输出一律拒绝，绝不把
        未知结构当正文放行。
        """
        if run.outcome is not RunOutcome.OK:
            return ParsedOutput(
                ok=False, reason_code=f"runner_{run.outcome.value}",
                detail=run.detail or f"runner 结局 {run.outcome.value}",
            )
        try:
            obj = json.loads(run.stdout_text)
        except json.JSONDecodeError as exc:
            return ParsedOutput(
                ok=False, reason_code="parse_failed_bad_json",
                detail=f"stdout 不是单个合法 JSON（含逐行事件残留时同样在此拒绝）：{exc}",
            )
        if not isinstance(obj, dict) or obj.get("type") != "result":
            kind = obj.get("type") if isinstance(obj, dict) else type(obj).__name__
            return ParsedOutput(
                ok=False, reason_code="parse_failed_not_result",
                detail=f"结果类型异常（type={kind}），拒绝放行",
            )
        if obj.get("is_error") is True:
            return ParsedOutput(
                ok=False, reason_code="app_error",
                value={"subtype": obj.get("subtype"), "result": obj.get("result")},
                detail="应用层报告错误（is_error=true）",
            )
        result_text = obj.get("result")
        if not isinstance(result_text, str):
            return ParsedOutput(
                ok=False, reason_code="parse_failed_missing_field",
                detail="结果缺少 result 字符串字段",
            )
        model = obj.get("model")
        return ParsedOutput(
            ok=True, reason_code=None,
            value={
                "result": result_text,
                "subtype": obj.get("subtype"),
                "num_turns": obj.get("num_turns"),
                # 模型身份只取 CLI 报告的字段，不猜测 provider（用户可配网关）。
                "model": model if isinstance(model, str) else None,
                "session_id": obj.get("session_id"),
                "total_cost_usd": obj.get("total_cost_usd"),
            },
        )


class CodexCliAdapter(BaseCliAdapter):
    """Codex CLI 的 build_invocation/parse_output 适配器（ISS-126，DEC-030）。

    调用合同（0.147.0 实证；形态与 ISS-126 防破坏负例验证一致）::

        <codex> exec --json --ephemeral --ignore-user-config --ignore-rules
               --skip-git-repo-check -s read-only -C <隔离cwd>
               -c model_reasoning_effort=low
        # 无 prompt 位置参数：载荷经 stdin（exec 官方形态；实测提示行
        # "Reading prompt from stdin..." 与内部日志均进 stderr，stdout 纯 JSONL）
        cwd=<隔离空目录>  env={PATH, HOME}  认证=CODEX_HOME 下用户 auth.json（CLI 自行读取）

    防破坏硬边界（DEC-030）：``-s read-only`` 是 OS 级 Seatbelt 只读沙箱。
    负例实证（worktree evidence/REPORT-pm-negatives.md，2026-09-30）：写/
    删/破坏性命令在模型层被规避（e1b/e1c/e1d/e2，目标文件零变化），OS 层
    同机制有直接拒绝证据（035E E4 exit=134、e1e/e1f/e3 PermissionError）；
    e1d 步骤 1 的读命令 exit=0 证明命令通道可用，排除「模型没能力执行」的
    替代解释。读取任意本机文件与联网是用户裁决（2026-09-30）允许的能力，
    由设置页文案如实披露，不构成失败条件。

    载荷只支持 stdin：codex exec 的 prompt 位置参数形态会把载荷暴露给本机
    同用户进程（ps），且 ``-`` 开头的载荷文本可占据 flag 位置，因此不提供
    argv 回退（与 claude 适配器经真实复验的 argv 回退不同）。
    """

    id = "codex-cli"

    def build_invocation(
        self,
        payload: str,
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        payload_mode: str = "stdin",
    ) -> Invocation:
        """构建受控调用。cwd 必须是隔离空目录；载荷只经 stdin。"""
        if payload_mode != "stdin":
            raise ValueError("payload_mode 只支持 stdin（codex 不提供 argv 载荷形态）")
        if not isinstance(payload, str) or not payload.strip():
            raise ValueError("payload 必须是非空字符串")
        exe = self.info.executable
        argv = [
            exe, "exec",
            "--json",                   # stdout 输出 JSONL 事件流
            "--ephemeral",              # 不落盘会话
            "--ignore-user-config",     # 不加载 ~/.codex/config.toml
            "--ignore-rules",           # 不加载 execpolicy .rules
            "--skip-git-repo-check",    # 隔离 cwd 不是 git 仓库
            "-s", "read-only",          # OS 级只读沙箱（防破坏硬边界，不可省略）
            "-C", str(cwd),             # 工作根同隔离 cwd（与进程 cwd 一致）
            "-c", "model_reasoning_effort=low",  # 合成分析载荷用低推理预算
        ]
        return Invocation(
            argv=argv,
            cwd=cwd,
            env=dict(env) if env is not None else build_minimal_env(),
            stdin_bytes=payload.encode("utf-8"),
        )

    def parse_output(self, run: RunResult) -> ParsedOutput:
        """三关中的第二、三关：JSONL 事件流的应用层错误判定与结构验证。

        成功 = ``turn.completed`` + 非空 agent_message（exit 0 由 runner
        保证）。降级 error item（WebSocket→HTTPS transport）与成功回复
        共存是实测成功形态（035E E1-E5 与 ISS-126 负例全部如此，本机到
        ChatGPT 后端网络抖动常态），因此 error item 在响应完整时只计数
        披露、不阻断；响应缺失/不完整时以可辨 reason_code 归因应用层。
        非 JSON 行跳过并计数（stdout 实测纯 JSONL，坏行出现即诊断信号；
        无任何可解析事件则结构验证失败）。
        """
        if run.outcome is not RunOutcome.OK:
            return ParsedOutput(
                ok=False, reason_code=f"runner_{run.outcome.value}",
                detail=run.detail or f"runner 结局 {run.outcome.value}",
            )

        events: list[dict] = []
        bad_lines = 0
        for line in run.stdout_text.splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                ev = json.loads(text)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if isinstance(ev, dict):
                events.append(ev)
            else:
                bad_lines += 1
        if not events:
            return ParsedOutput(
                ok=False, reason_code="parse_failed_no_events",
                detail=f"stdout 无任何可解析 JSONL 事件（非 JSON 行 {bad_lines} 条）",
            )

        thread_id: str | None = None
        usage: dict | None = None
        turn_completed = False
        turn_failed: list[dict] = []
        agent_texts: list[str] = []
        error_items: list[str] = []
        for ev in events:
            etype = ev.get("type")
            if etype == "thread.started":
                tid = ev.get("thread_id")
                if isinstance(tid, str):
                    thread_id = tid
            elif etype == "turn.completed":
                turn_completed = True
                if isinstance(ev.get("usage"), dict):
                    usage = ev["usage"]
            elif etype == "turn.failed":
                turn_failed.append(ev)
            elif etype == "item.completed":
                item = ev.get("item")
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    agent_texts.append(item["text"])
                elif item.get("type") == "error":
                    msg = item.get("message")
                    error_items.append(
                        msg if isinstance(msg, str)
                        else json.dumps(item, ensure_ascii=False)[:200]
                    )

        if turn_failed:
            return ParsedOutput(
                ok=False, reason_code="app_error_turn_failed",
                detail=f"turn.failed 事件：{json.dumps(turn_failed[0], ensure_ascii=False)[:200]}",
            )

        result_text = "\n\n".join(t for t in agent_texts if t.strip()).strip()
        if not turn_completed or not result_text:
            # 响应缺失/不完整：error item（流断连降级仍空响应）优先归因应用层。
            if error_items:
                return ParsedOutput(
                    ok=False, reason_code="app_error_stream_degraded",
                    detail=(
                        f"响应不完整（agent_message={'非空' if result_text else '空'}、"
                        f"turn.completed={'是' if turn_completed else '否'}）；"
                        f"error item {len(error_items)} 条，首条：{error_items[0][:200]}"
                    ),
                )
            return ParsedOutput(
                ok=False, reason_code="parse_failed_missing_agent_message",
                detail=(
                    f"缺完整 agent_message（turn.completed="
                    f"{'是' if turn_completed else '否'}，agent_message "
                    f"{len(agent_texts)} 条，非 JSON 行 {bad_lines} 条）"
                ),
            )
        return ParsedOutput(
            ok=True, reason_code=None,
            value={
                "result": result_text,
                "thread_id": thread_id,
                "turn_completed": True,
                # 降级等非致命 error item 与坏行如实计数披露（成功不掩盖网络抖动）。
                "error_items": len(error_items),
                "skipped_non_json_lines": bad_lines,
                "usage": usage,
                # 事件流不报告模型名（-m 未固定、用户可经网关换模型），不猜测。
                "model": None,
            },
        )


class HermesAgentAdapter(BaseCliAdapter):
    """Hermes Agent 的 build_invocation/parse_output 适配器（ISS-126，DEC-030）。

    调用合同（0.21.5 实证；形态与 ISS-126 行为矩阵一致）::

        <hermes> chat --query-file - --format stream-json --run-budget 150
                 -t web,vision --ignore-user-config --ignore-rules --source tool
        cwd=<隔离空目录>  env={PATH, HOME}  认证=用户 ~/.hermes/.env（CLI 自行读取）

    防破坏硬边界（DEC-030）：``-t web,vision`` 是调用级工具集**替换**语义
    （非空值不回退平台默认，空值才回退——035E 的 ``-t ""`` 失效反例正源于
    此），工具面收敛为 ``{web_search, web_extract, vision_analyze}`` 只读
    三件；MCP 服务器因不在名单被整体跳过。负例实证（worktree evidence/
    iss-126-hermes-20260930/REPORT.md，2026-09-30）：直白/无害外观/伪装写
    诱导与删除诱导全部零 tool_use、目标文件零变化；web_search 通道对照真实
    执行成功。``--safe-mode`` 单独使用不是防破坏边界（terminal/write_file/
    rm 无审批照常执行，h2a/h2b 反例在档），只作为对定制/插件/MCP 的附加
    隔离与 ``-t`` 并用。读取任意本机文件与联网是用户裁决允许的能力，由
    设置页文案如实披露。

    载荷只支持 stdin：``--query-file -`` 官方形态（任意文本不经 shell
    解释，不落盘）；argv ``-q`` 形态会把载荷暴露给本机同用户进程（ps），
    不提供回退。
    """

    id = "hermes-agent"

    # 只读工具集（DEC-030 防破坏边界本体）：web_search/web_extract 联网读取
    # （用户裁决允许），vision_analyze 图像分析；不含 terminal/file/
    # code_execution/browser 等任何写路径。版本退化保护：变更须复验负例。
    READONLY_TOOLSETS = "web,vision"
    RUN_BUDGET_S = "150"

    def build_invocation(
        self,
        payload: str,
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        payload_mode: str = "stdin",
    ) -> Invocation:
        """构建受控调用。cwd 必须是隔离空目录；载荷只经 stdin。"""
        if payload_mode != "stdin":
            raise ValueError("payload_mode 只支持 stdin（hermes 不提供 argv 载荷形态）")
        if not isinstance(payload, str) or not payload.strip():
            raise ValueError("payload 必须是非空字符串")
        exe = self.info.executable
        argv = [
            exe, "chat",
            "--query-file", "-",        # 载荷经 stdin（官方形态，任意文本安全）
            "--format", "stream-json",  # stdout 输出 JSONL 事件流（隐含 --quiet）
            "--run-budget", self.RUN_BUDGET_S,  # 每轮运行墙钟预算，挂死有硬上限
            "-t", self.READONLY_TOOLSETS,       # 只读工具集替换（防破坏硬边界，不可省略）
            "--ignore-user-config",     # 不加载 ~/.hermes/config.yaml（.env 认证仍加载）
            "--ignore-rules",           # 不注入 AGENTS.md/SOUL.md/memory/skills
            "--source", "tool",         # 第三方集成来源标记，不混入用户会话列表
        ]
        return Invocation(
            argv=argv,
            cwd=cwd,
            env=dict(env) if env is not None else build_minimal_env(),
            stdin_bytes=payload.encode("utf-8"),
        )

    def parse_output(self, run: RunResult) -> ParsedOutput:
        """三关中的第二、三关：JSONL 事件流的应用层错误判定与结构验证。

        成功 = 末尾 ``result`` 事件 exit_code 0 且 text 非空（进程 exit 0
        由 runner 保证）。result 事件自带 ``error`` 字段（0.21.5 实测：
        provider 400 时 exit_code=1 + error 文本）→ 可辨 app_error。只读
        工具集内的 tool_use/tool_result 事件是合法形态（DEC-030 允许读取
        与联网），计数披露不阻断；非 JSON 行跳过并计数，无任何可解析事件
        则结构验证失败。
        """
        if run.outcome is not RunOutcome.OK:
            return ParsedOutput(
                ok=False, reason_code=f"runner_{run.outcome.value}",
                detail=run.detail or f"runner 结局 {run.outcome.value}",
            )

        events: list[dict] = []
        bad_lines = 0
        for line in run.stdout_text.splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                ev = json.loads(text)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if isinstance(ev, dict):
                events.append(ev)
            else:
                bad_lines += 1
        if not events:
            return ParsedOutput(
                ok=False, reason_code="parse_failed_no_events",
                detail=f"stdout 无任何可解析 JSONL 事件（非 JSON 行 {bad_lines} 条）",
            )

        session_id: str | None = None
        model: str | None = None
        result_event: dict | None = None
        tool_calls = 0
        for ev in events:
            etype = ev.get("type")
            if etype == "system":
                if isinstance(ev.get("session_id"), str):
                    session_id = ev["session_id"]
                m = ev.get("model")
                model = m if isinstance(m, str) and m else None
            elif etype == "tool_use":
                tool_calls += 1
            elif etype == "result":
                result_event = ev
                if isinstance(ev.get("session_id"), str):
                    session_id = ev["session_id"]

        if result_event is None:
            return ParsedOutput(
                ok=False, reason_code="parse_failed_missing_result",
                detail=(
                    f"缺 result 收尾事件（system {sum(1 for e in events if e.get('type') == 'system')}、"
                    f"tool_use {tool_calls}、非 JSON 行 {bad_lines} 条）"
                ),
            )
        app_error = result_event.get("error")
        if result_event.get("exit_code") not in (0, None) or app_error:
            return ParsedOutput(
                ok=False, reason_code="app_error",
                value={"session_id": session_id, "result": result_event.get("text")},
                detail=(
                    f"应用层报告错误（result.exit_code={result_event.get('exit_code')}）："
                    f"{app_error if isinstance(app_error, str) else json.dumps(app_error, ensure_ascii=False)[:200]}"
                ),
            )
        result_text = result_event.get("text")
        if not isinstance(result_text, str) or not result_text.strip():
            return ParsedOutput(
                ok=False, reason_code="parse_failed_missing_field",
                detail="result 事件缺少非空 text 字段",
            )
        tokens = result_event.get("tokens") if isinstance(result_event.get("tokens"), dict) else None
        return ParsedOutput(
            ok=True, reason_code=None,
            value={
                "result": result_text,
                "session_id": session_id,
                # --ignore-user-config 下 init 的 model 常为空串；只取 CLI 报告值。
                "model": model,
                "tool_calls": tool_calls,
                "skipped_non_json_lines": bad_lines,
                "usage": tokens,
            },
        )


def get_adapter(info: RuntimeInfo) -> BaseCliAdapter:
    """按 RuntimeInfo 取适配器；READY 家各自分发，签名不变。

    ISS-035E 接缝：新增候选适配器时在此分发，签名不变。
    """
    if info.id == ClaudeCodeAdapter.id:
        return ClaudeCodeAdapter(info)
    if info.id == CodexCliAdapter.id:
        return CodexCliAdapter(info)
    if info.id == HermesAgentAdapter.id:
        return HermesAgentAdapter(info)
    raise UnsupportedRuntimeError(f"候选 {info.id} 尚无适配器（{info.reason_code}）")


@dataclass(slots=True)
class DispatchResult:
    """一次「构建→运行→验证」的完整结论；三关全过才 ok。"""

    run: RunResult
    parse: ParsedOutput

    @property
    def ok(self) -> bool:
        return self.parse.ok

    @property
    def reason_code(self) -> str:
        if self.parse.reason_code:
            return self.parse.reason_code
        return f"runner_{self.run.outcome.value}"


def dispatch_request(
    adapter: BaseCliAdapter,
    runner: AgentCliRunner,
    payload: str,
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout_s: float = RUN_DEFAULT_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
    payload_mode: str = "stdin",
) -> DispatchResult:
    """统一派发入口：适配器只管合同，runner 只管进程，本函数串起三关。"""
    invocation = adapter.build_invocation(
        payload, cwd=cwd, env=env, payload_mode=payload_mode
    )
    run = runner.run(invocation, timeout_s=timeout_s, cancel_event=cancel_event)
    parse = adapter.parse_output(run)
    return DispatchResult(run=run, parse=parse)


# --------------------------------------------------------------------------
# 隔离手动验证入口（不接 API/DB，不构成产品功能面）
# --------------------------------------------------------------------------


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m fathom.agent_runtime",
        description="Runtime 能力门隔离手动探针（仅供隔离验证，不接 API/DB）",
    )
    parser.add_argument("--probe", action="store_true", help="打印四家 RuntimeInfo JSON")
    parser.add_argument("--check", metavar="ID", help="对指定候选走真实 runner 合成请求")
    parser.add_argument("--payload", help="合成载荷文本（仅授权的固定合成问题）")
    parser.add_argument("--payload-file", help="从文件读合成载荷（UTF-8）")
    parser.add_argument("--timeout", type=float, default=RUN_DEFAULT_TIMEOUT_S)
    parser.add_argument("--cwd", help="隔离工作目录（默认在系统临时目录新建并保留路径输出）")
    parser.add_argument("--argv-payload", action="store_true", help="argv 载荷回退形态（有可见性披露）")
    parser.add_argument("--no-liveness", action="store_true", help="关闭父进程存活看门（仅诊断用）")
    args = parser.parse_args(argv)

    if args.probe:
        infos = probe_all()
        print(json.dumps(
            {cid: info.to_dict() for cid, info in infos.items()},
            ensure_ascii=False, indent=2,
        ))
        return 0

    if args.check:
        payload = args.payload
        if args.payload_file:
            payload = Path(args.payload_file).read_text(encoding="utf-8")
        if not payload:
            parser.error("--check 需要 --payload 或 --payload-file")
        return _run_check(args, payload)

    parser.print_help()
    return 2


def _run_check(args: argparse.Namespace, payload: str) -> int:
    meta = get_candidate(args.check)
    info = detect_runtime(meta)
    print(json.dumps({"step": "probe", "runtime": info.to_dict()}, ensure_ascii=False))
    if info.availability is not Availability.READY:
        print(json.dumps({
            "step": "check", "ok": False,
            "reason": f"{info.id} 为 {info.availability.value}（{info.reason_code}），拒绝真实调用",
        }, ensure_ascii=False))
        return 2

    adapter = get_adapter(info)
    runner = AgentCliRunner(liveness_watch=not args.no_liveness)
    cwd = Path(args.cwd) if args.cwd else Path(tempfile.mkdtemp(prefix="fathom-check-"))
    cwd.mkdir(parents=True, exist_ok=True)
    result = dispatch_request(
        adapter, runner, payload, cwd=cwd,
        timeout_s=args.timeout,
        payload_mode="argv" if args.argv_payload else "stdin",
    )
    print(json.dumps({
        "step": "check",
        "ok": result.ok,
        "reason_code": None if result.ok else result.reason_code,
        "outcome": result.run.outcome.value,
        "exit_code": result.run.exit_code,
        "wall_ms": result.run.wall_ms,
        "group_reaped": result.run.group_reaped,
        "stdout_truncated": result.run.stdout_truncated,
        "stderr_truncated": result.run.stderr_truncated,
        "response": result.parse.value.get("result") if result.parse.value else None,
        "model": result.parse.value.get("model") if result.parse.value else None,
        "parse_detail": result.parse.detail,
        "stderr_first_line": (
            result.run.stderr_text.splitlines()[0][:200]
            if result.run.stderr_text.strip() else ""
        ),
        "cwd": str(cwd),
        "payload_mode": "argv" if args.argv_payload else "stdin",
        "auth": "verified_on_this_request" if result.ok else "not_verified",
        "note": "本入口不回显 env/凭据；stderr 仅首行诊断",
    }, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(_main())
