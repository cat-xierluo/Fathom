# 当前任务

> Last updated: 2026-09-13

## 当前进行中

### DOC-006 — merge-gate 紧凑门禁：单次 scan 输出固定 schema 摘要，降低 routine merge review 成本

- **状态**：`REVIEW`（等待独立 reviewer，不得在本任务内写 DONE）
- **来源**：2026-09-02 编排器任务：用真实实现和基准降低 routine merge review 的 Agent/token 成本，而不是减少扫描正确性。
- **缘由与证据**：legacy 全量入口把全部 JSONL 灌进调用方上下文（代表性 merge-review fixture 实测 4273 bytes / 20 行）；SKILL §0 又默认为此 spawn subagent。对零 finding 或机械 hard 的常规场景，绝大部分字节是 ok/soft 明细，主 Agent 只需要计数、阻断 rule ID 和下一步动作。
- **主目标**：向后兼容的单次紧凑门禁入口 `scan.sh --profile merge-gate`（可选 `--jsonl-out`）：一次 scan 完成所需 checker，stdout 输出一行固定 schema（`doc-curator.merge-gate.v1`）摘要——精确 base/head/range、config provenance、hard/adaptive/soft/ok 计数、阻断/adaptive rule ID、next_action；完整 JSONL 仅在显式 `--jsonl-out` 时落盘，默认不进主 Agent 上下文。
- **非目标**：不改变 legacy 模式的 stdout 契约、checker 集合与退出码语义；不为节省成本减少任何 checker 或严重度；不自动生成项目配置；不做 subagent 编排实现（只约定协议）；不发布、不推送。
- **输入与依赖**：scan.sh 既有编排与 JSONL 合同；common.sh 配置解析；Bash 4+ 与现有系统文本工具；代表性 merge-review fixture（断链 + 上下文不同步 + 可通过项）。
- **允许范围**：`doc-curator/scripts/scan.sh`、`doc-curator/scripts/test-doc-curator.sh`、`SKILL.md`、`TASKS.md`、`DECISIONS.md`、`CHANGELOG.md`。
- **禁止范围**：其他 Skill、根文件、`config/*.local.yaml`（不创建不修改）、Badminton Lab 主工作区、安装依赖、push/merge、回滚他人改动。
- **停止条件**：若紧凑摘要无法与 legacy 阻断结论保持一致，或必须第二次调用 scan 才能计算摘要，则停止并保留 legacy 入口不动。
- **交付物**：`--profile merge-gate`（固定 schema 单行摘要 + fail-closed 错误摘要）、`--jsonl-out`（显式 JSONL 留档）、`config-required` / `merge-gate-range-unresolved` / `jsonl-output-error` 门禁 hard、SKILL §0 路由例外与紧凑门禁说明、回归测试与基准断言。
- **验收标准**：① legacy 与 compact 的 hard/adaptive 阻断 rule IDs 完全一致；② 单次 scan 各 checker 恰好调用一次且与 legacy 序列一致；③ compact stdout bytes ≤ legacy 的 50%；④ 扫描不在 repo/worktree 外写文件；⑤ 缺项目配置时 `config-required` hard（NOT_VERIFIED）且不生成配置；⑥ 退出码与 legacy 完全一致；⑦ 基准原始数字写入任务证据。
- **验收证据**：
  - `scripts/test-doc-curator.sh`：226 PASS / 0 FAIL（含 merge-gate 新增 35 项：等价性、单次调用、带宽、写边界、config-required、错误路径、range 解析、working-tree scope、jsonl-out）。
  - 基准（代表性 fixture，`base..head` 全量 9 checker）：legacy stdout 4273 bytes / 20 行（hard=3、adaptive=6、soft=6、ok=5，阻断 IDs：hard=markdown-link-broken；adaptive=context-sync-changelog、markdown-link-broken）；compact stdout 618 bytes / 1 行，ratio=14.5%（≤50% 达标）；调用序列 9 个 checker 与 legacy 完全一致。
  - 写边界：受控 TMPDIR 无残留（scan 临时文件全部清理）；repo worktree（排除 .git）与 Skill 影子目录 manifest 前后一致；`--jsonl-out` 落盘内容与 legacy stdout 逐字节一致。
  - `bash -n doc-curator/scripts/*.sh` 通过；`git diff --check` 通过；legacy 模式回归（原 191 项）零退化。
  - `NOT_VERIFIED`：真实 merge review 工作流中的 Agent token 节省比例（本任务只断言 stdout bytes 带宽，不夸大为端到端 token savings）；无旧上下文 Agent 对 §0 路由例外的稳定性。

### DOC-005 — v0.9.0 retrospective 审计闭环

- **状态**：`REVIEW`
- **来源**：2026-08-28 用户要求全面读取项目 Git/retrospective 沉淀并同步升级 `doc-curator`；书仓 dogfood 同时暴露 267 个 hard finding、重复 DEC ID、索引漂移和旧配置静默关闭 context-sync。
- **缘由与证据**：v0.6.0 的 SKILL 重写删除了 v0.2.1 §0 subagent 协议，但 CHANGELOG、DECISIONS 和消费项目仍宣称存在；项目根旧配置优先级高于 Skill profile 且缺 `context_sync.enabled`，v0.8 将其静默解释为 false；断链 checker 未 URL decode，决策连续性混入正文引用，总入口也未约束 child finding 与退出码。
- **主目标**：形成 v0.9.0 candidate，恢复调用协议，并以确定性、低误报、失败闭合的检查覆盖配置 provenance、决策 ID、Markdown 路径、跨文档索引和执行链真实状态。
- **非目标**：不自动修改被审计项目文档；不对路线图里程碑、任务完成语义作 LLM 式自动裁决；不提交、推送、发布或安装依赖。
- **输入与依赖**：doc-curator Git 历史与四件套；法律书仓 retrospective 和 v0.8 dogfood JSONL；Bash 4+ 与现有系统文本工具。
- **允许范围**：`doc-curator/` 内 SKILL、配置、脚本、fixture/test、CHANGELOG、TASKS、DECISIONS。
- **禁止范围**：其它 Skill、私有 `*.local.yaml`、消费项目写入、Git 远端和 repo-local 身份配置。
- **停止条件**：若规则无法用项目声明和确定性证据控制误报，则保留 adaptive/人工，不升级为 hard；若需要新增运行依赖则停止。
- **交付物**：schema v2 config contract、标题级 decision identity、URL decode/link policy、`context-truth`、child/report fail-closed、§0 协议和同步四件套。
- **验收标准**：旧回归不退化；新增 fixture 覆盖已知反例；`bash -n`、diff check、自扫描通过；消费项目配置烟测结果与无旧上下文前向测试如未执行必须标 `NOT_VERIFIED`。
- **验收证据**：
  - `scripts/test-doc-curator.sh`：167 PASS / 0 FAIL（覆盖 v0.9 全部新增 hard rule、重复配置路径、仓内外 symlink、context-truth 零/多重声明、child/report 错配链）。
  - `quick_validate.py`、全部 Shell `bash -n`、ShellCheck warning 级、`git diff --check` 已通过；候选 Skill 自扫 rc 0（hard 0 / adaptive 0 / ok 4 / soft 8）。
  - 消费项目 sibling worktree 烟测：`--only config` exit 0；`--only context-truth` exit 0，P18/L12 在 README 与 TASKS 的 4 条镜像断言全部同步。
  - 消费项目动态 dogfood 快照（不是稳定常量）：候选入口 `feat-doc-curator-retrospective-audit/doc-curator`，消费 worktree `retrospective-full-audit-20260828`，`HEAD=baa0459c`，`base/origin-main=69426fc5`，13 项 working-tree 变化；显式 `--working-tree` 全扫 exit 1，14 hard（13 个重复决策 ID + 132→135 跳号）、181 adaptive（179 条已按源/目标政策降级的历史断链 + 1 伪链接 + 1 活跃任务数）、15 ok / 8 soft。完整 Git 历史 range 仍 `NOT_VERIFIED`。
  - 无旧上下文 Agent 对 §0 的独立前向测试、跨三轮 instruction-stability evidence 仍 `NOT_VERIFIED`。

## 已完成

### DOC-007 — v0.10.0 显式跨文档角色/状态声明一致性

- **状态**：`DONE`（PM 独立验收完成；PR #204 已合入，未安装或发布）
- **来源**：2026-09-13 用户批准从项目 retrospective 发现更新 doc-curator。
- **缘由与证据**：索引数量同步不能发现问题清单被镜像标为已作答，或 READY 被另一入口标为 DONE；相同声明也不能证明工作真实发生。
- **主目标**：以 opt-in `context_truth.value_claims` 精确比较唯一声明的一个值，并阻断配置、读取及路径边界错误。
- **非目标**：不自动判断访谈内容、任务完成真实性；不重复既有 `task_source_contract` 状态卡合同引擎；不写消费项目或安装依赖。
- **输入与依赖**：v0.9.0 context-truth/config 合同、Bash 4+、系统文本工具；真实项目审计提供的声明漂移场景。
- **允许范围**：仅 `doc-curator/` 脚本、示例、测试和四件套。
- **禁止范围**：其它 Skill、业务文档、全局配置和身份配置、安装、合并及发布；2026-09-13 PM 验收后仅授权通过 safe-push 创建本 Skill 的 PR。
- **停止条件**：若必须语义推断或新增依赖才能判断，则保留人工审计；旧版本能静默忽略新规则时不交付配置。
- **交付物**：exact_value 模式、schema 3 能力边界（兼容旧 schema 2）、角色/状态示例、离线反例测试。
- **验收标准**：正常与异常声明、混合旧索引、缺失/重复/未知/空字段、非法正则、读错误、路径逃逸/符号链接均有实测；扫描只读；旧 reader 拒绝 schema 3；原回归不退化。
- **验收证据**：
  - `scripts/test-context-truth-values.sh`：105 PASS / 0 FAIL（其中 2 项通过 `DOC_CURATOR_LEGACY_ROOT` 使用未修改的 v0.9 reader，来源 `origin/main=5c0f59213286621f65c6100e0dcc9adcd13f9921`；默认独立运行 103 项）。覆盖正常角色/状态、漂移 hard/adaptive、缺失/重复/空捕获、允许集、配置错误、schema 降级、两类声明路径/循环 symlink、实际 grep exit 2 读失败，以及项目/Skill manifest 不变和临时文件清理。
  - 同行评审发现仅检查 pattern 首尾不足以保证整行，已增加完整 match 等于输入行的断言，并用顶层 alternation 与转义美元符后带额外文本的反例复测。
  - `bash -n`、ShellCheck warning 级、`quick_validate.py`、`git diff --check` 已通过。
  - PM 独立完整回归（候选 `ec565410b69ad7277071b5a4e135286d436403fa`）：227 PASS / 0 FAIL + exact-value 105 PASS / 0 FAIL，退出 0。此证据仅绑定该候选，不追认其后修订。
  - 后续诊断：当时的 7894-byte 书仓 flat 样本在 3300 次直接管道运行及一次 v0.10 真实扫描中未复现旧 warning，原事件根因保持未确认。隔离的大合法 flat 却确实出现 `printf | grep -q` 的 `PIPESTATUS=141,0`；缺 files 的 helper 在 command substitution 内打印错误却返回 0/空值也已复现。另 schema 3 的初始化被旧硬编码 schema 2 拒绝。
  - 窄修：配置存在性与字段查询完整消费输入，helper 显式传播 load 失败，每个 checker 在条件/命令替换前加载配置快照，基线初始化复用 config 合同。`scripts/test-config-read.sh` 42 PASS / 0 FAIL，覆盖 200KB 合法配置、9 种 getter 返回、子 checker 实际读取故障及 schema 2/3 初始化/非法合同不写盘；全部 Shell 逐文件 `bash -n` 与 ShellCheck warning 级通过。
  - PM 再次独立完整回归（实现候选 `0a6b300d3184e2e675189d13b90ae196903a59ee`，2026-09-13）：退出 0，原始输出末段为 `TOTAL pass=227 fail=0`、`TOTAL exact-value pass=105 fail=0`、`TOTAL config-read pass=42 fail=0`。PM 已复核后续代码差异和 42 项测试；其后的本条证据回填仅改文档，不以新提交号追认旧测试。
  - 消费项目独立验证（同一 `0a6b300d` reader）：法律书仓显式 `retrospective/doc-curator-value-claims.pilot.yaml` 正例退出 0 / ok 3；根配置 `context-sync --range 3de4f512..cd4da597 --profile merge-gate` 退出 0 / ok 4，未出现相矛盾的解析 stderr。此结果仅证明本次输入运行正常，不反向确定旧 warning 的原因。
  - push 前远端 base 更新至 `c305031b7ce71f9299456ccf9525841e569fb996`；其相对原 base 的本 Skill 内容差异为空。未推送短分支 rebase 映射为 `ec565410→0689b10f`、`0a6b300d→c36e20df`、证据文档提交 `1419a823→1562a415`。rebase 前后完整模块 tree 同为 `a2dc3ee20fba9daa636ca08616f7a25cb1037e9d`；代码及测试的 scripts tree 为 `dc493fcc80fcfaab991281cdfff245ee2d2fc71e`，config tree 为 `6629cd7d5811ccf75ddc790a21824ea09deecd29`，均与原受验 `0a6b300d` 相同。此后仅追加本条文档证据。
  - 对 rebase 后候选 `1562a415c00e80b44bb0af274bc39ec576bbf921` 重新实跑定向回归：exact-value 105 PASS / 0 FAIL、config-read 42 PASS / 0 FAIL，退出 0；逐文件 Shell 语法、quick_validate 与 diff check 通过。旧完整回归仍按原执行 SHA 记录，不改称在新 SHA 全量运行。
  - `NOT_VERIFIED`：无旧上下文 Agent 的配置使用稳定性、跨项目推广效果；标签本身的真实性不在本 checker 的验证范围。发布/安装未执行。
  - 合并闭环（2026-09-13）：[PR #204](https://github.com/cat-xierluo/private-skills/pull/204) 已 squash merge，merge OID `9c92e45069fac896c72c92df2b3d7d423d187b0e`；GitHub REST 复核 `merged=true` / `state=closed`。PM 已确认 4 项远端检查成功，合并内容与独立验收 diff 一致；不以合并状态推断上述未验证效果或安装/发布完成。
  - 所有权交接：原 doc-curator worker 已完成；PM 仅将本卡状态与合并事实的收口元数据交由 retrospective-skills worker 写回，不扩展到本 Skill 脚本或其他任务卡。

### DOC-004 — v0.7.1 提交前复审修正

- **状态**：`DONE`（2026-08-01）
- **来源**：2026-08-01 用户要求“再次审查一下，然后提交”；以当前 `main` 上 v0.7.1 为候选重新执行 `skill-lint` 与真实渲染反例。
- **缘由与证据**：Harness 静态审计命中 `HRA-001`，定位到报告端到端测试用 `|| true` 丢弃基线初始化退出码；合法 JSONL 中含转义引号时，`render-report.sh` 的 sed 字段提取会把 message 截断；SKILL 仍保留“唯一写入是初始化基线”的旧表述，与 `--report` 不一致。
- **主目标**：修复报告渲染的转义字符串解析、测试退出码假绿和公开说明漂移，形成范围仅限 `doc-curator` 的 v0.7.2 聚焦提交。
- **非目标**：不补建 instruction-stability 三轮签名证据；不发布、推送或迁入公开 `skills/`；不处理私有仓库中其他 Skill 的未提交改动。
- **输入与依赖**：当前 v0.7.1 候选；`skill-lint` Harness finding；Bash 4+、ShellCheck 与既有回归入口。
- **允许范围**：`doc-curator/scripts/render-report.sh`、`scripts/scan.sh`、`scripts/test-doc-curator.sh`、`SKILL.md`、`TASKS.md`、`DECISIONS.md`、`CHANGELOG.md`。
- **禁止范围**：`config/*.local.yaml`、其他 Skill、根 README/Marketplace、远端操作和破坏性 Git 操作。
- **停止条件**：若修复需要引入 jq/Python 等新运行依赖，或会改变 scan stdout JSONL 合同，则停止并改用更小的 Bash 固定 schema 解析方案。
- **交付物**：转义安全的固定 schema 解码、退出码显式断言、报告元数据去绝对路径、说明同步、回归与一个聚焦 Git commit。
- **验收标准**：带 `\"`、`\\`、`\n`、`\t` 的合法 finding 不截断；测试不再丢弃退出码；Harness 0 finding；全量回归、bash -n、ShellCheck、diff check、自体扫描通过；只暂存 `doc-curator`。
- **验收证据**：
  - `scripts/test-doc-curator.sh`：92 PASS / 0 FAIL；覆盖原 81 项回归，以及转义字符串、显式退出码、元数据绝对路径和 DONE 历史误报反例。
  - 全部 Shell 脚本 `bash -n` 通过；ShellCheck warning 级 0；`git diff --check` 通过。
  - `skill-lint` Harness 静态审计由 1 个 `HRA-001 hard` 归零为 PASS / 0 finding；自体 AgentCMD 全量扫描 exit 0、hard 0、adaptive 0。
  - Git 跟踪面未包含 `.DS_Store`、`.doc-curator/` 或 `config/*.local.yaml`；新增 diff 未命中绝对用户路径或常见凭证模式。
  - `instruction_stability_gate.py assess`：exit 2 / `NOT_VERIFIED`，仍缺约束追踪合同和三轮 evaluator-signed evidence，不作为本次修复提交的稳定性声明。
  - 提交范围限定为 `doc-curator` 9 个文件；commit SHA 由本次 Git 提交生成并在交付回执报告。

### DOC-003 — 体检报告可视化（Markdown 渲染）

- **状态**：`DONE`（2026-07-31）
- **来源**：用户要求体检报告可视化（「至少需要一份类似 Word 文件之类的东西，或者是 Markdown 文件之类的」）；TASKS 历史待办「体检报告可视化」的延续。
- **缘由与证据**：scan.sh 的 stdout 是纯 JSONL（机器可读），用户需要一份人读的可分享报告（类似 Word），便于发给协作者或纳入 PR 描述。v0.6.0 整改时旧待办被重组，但可视化需求仍在。
- **主目标**：新增人读 Markdown 报告生成能力，一条命令即可获得按 severity 分组的可分享报告。
- **非目标**：不生成 Word/HTML（避免重依赖，需 Word 时经 md2word 转换）；不改 scan.sh 的 stdout JSONL 契约；不做交互式查看器。
- **允许范围**：新增 `scripts/render-report.sh`；`scripts/scan.sh` 加 `--report` 参数；`scripts/test-doc-curator.sh` 加用例；`SKILL.md`/`CHANGELOG.md`/`DECISIONS.md`/`TASKS.md` 同步。
- **交付物**：`render-report.sh`（渲染脚本）、scan.sh `--report` 编排、26 个新测试断言、SKILL 使用说明、三件套同步。
- **验收标准**：合法 JSONL 渲染含汇总表+分组表；空输入生成「无结果」报告；非法行跳过+stderr 警告；message 含 `|` 正确转义；scan `--report` 端到端生成报告且 stdout 仍 JSONL；`--report` 与 `--init-baseline` 互斥返回 64。
- **验收证据**：
  - `scripts/test-doc-curator.sh`：55 → 81 PASS / 0 FAIL；新增覆盖合法/空/非法输入、`|` 转义、端到端、参数互斥。
  - 全部脚本 `bash -n` 通过。
  - 真实样本：doc-curator 自身用 AgentCMD 档案 scan + render，生成含汇总表（hard=0/adaptive=0/soft=4/ok=7）的完整 Markdown 报告。

### DOC-002 — AgentCMD v4 TASKS 当前任务源审计

- **状态**：`DONE`（2026-07-31）
- **来源**：2026-07-31 用户要求；用户确认上下文审计应由 `doc-curator` 承担，并以用户级 `AGENTS.md` v4.0 为权威语义来源。
- **缘由与证据**：现有 `check-tasks.sh` 只检查任务数量、总行数、进度日志与归档指针，无法识别任务源是否区分草案与可执行任务，也无法阻断 `READY` 任务卡缺边界、`BLOCKED` 缺原因或 `DONE` 缺执行证据。
- **主目标**：新增可显式启用、可配置且失败闭合的 AgentCMD v4 任务源合同审计，使 `doc-curator` 能直接检查其他 Skill 根目录的 `TASKS.md`。
- **非目标**：不强制所有项目采用同一 Markdown 标题；不自动改写被审计文件；不把 `DRAFT` 的信息不完整判为错误；不在本任务内迁入公开 `skills/`、更新 Marketplace 或推送远端。
- **输入与依赖**：用户级 `AGENTS.md` v4.0；仓库内已采用 `DRAFT / READY / IN_PROGRESS / BLOCKED / REVIEW / DONE / CANCELLED` 的 Skill 任务源；现有 Bash 4+、YAML 子集解析器与 JSONL 输出合同。
- **允许范围**：`doc-curator/SKILL.md`、`config/*.example.yaml`、`scripts/check-tasks.sh`、`scripts/test-doc-curator.sh`、`TASKS.md`、`DECISIONS.md`、`CHANGELOG.md`；必要时对通用配置 helper 做最小修改。
- **禁止范围**：两份 `config/*.local.yaml` 私有配置、其他 Skill、根 README、Marketplace、Git 发布或远端操作。
- **实施阶段**：先定义状态与字段合同；再实现配置驱动检查；补合规、缺字段、缺原因、缺证据、空队列及兼容性回归；最后用真实 Skill 样本复核。
- **决策规则**：只有显式启用 `task_source_contract` 时执行严格审计；`DRAFT` 允许不完整；可执行、验收、完成或阻塞状态按各自合同检查；无法可靠机判的语义不伪装成精确结论。
- **停止条件**：若 AgentCMD 权威语义与项目级规则冲突，或必须依赖固定中文标题才能判断，则停止并改为更薄的配置适配；若新增规则使默认配置产生破坏性误报，则不得默认开启。
- **交付物**：配置 schema、AgentCMD v4 Skill 审计档案、确定性 checker、回归用例、SKILL 使用说明及三件套同步记录。
- **验收标准**：合规 `READY` 卡通过；`READY / IN_PROGRESS` 缺合同字段、`BLOCKED` 缺原因、`DONE` 缺证据均 hard；`DRAFT` 不完整不阻断；明确空队列可通过；默认配置保持兼容；私有配置不进入 Git。
- **验收证据**：
  - `scripts/test-doc-curator.sh`：55 PASS / 0 FAIL；覆盖合规 READY、不完整 DRAFT、READY 缺字段、BLOCKED 缺原因、DONE 缺证据、显式空队列、状态冲突、缺 TASKS 和既有回归。
  - 全部 Shell 脚本 `bash -n` 通过；ShellCheck warning 级 0；`git diff --check` 通过；`skill-lint` Harness 静态审计 PASS、0 finding。
  - AgentCMD 档案真实样本：`doc-curator` 识别 2 个有状态任务/检查 2 张合同卡，`legal-visualization` 识别 7 个/检查 1 张，`universal-media-downloader` 识别 4 个 DRAFT/检查 0 张；三者 exit 0，仅有未初始化行数基线 soft 提示。
  - 自体 Markdown 引用检查：4 个文件、1 个链接、0 broken；新增公开档案未命中用户绝对路径、具名私有项目或常见凭证关键词；两份 `config/*.local.yaml` 继续被 `.gitignore` 命中。
  - `instruction_stability_gate.py assess`：exit 2 / `NOT_VERIFIED`，尚缺约束追踪合同和三轮 evaluator-signed evidence；不扩大为长期稳定性结论。

### PUB-001 公开发布候选整改

- **状态**：已完成（2026-07-30）
- **来源**：2026-07-30 用户要求；同日 `skill-lint` 发布前静态审查；用户反馈确认私有配置需保留在 Skill 内供运行时读取
- **目标**：把 `doc-curator` 从带私有项目适配和运行态数据的内部版本，整改为可公开分发、默认只读、配置可移植且失败闭合的发布候选。
- **范围**：
  1. 将通用默认配置、公开示例、项目私有适配和运行状态分层；私有适配保留在 Skill 的 `config/*.local.yaml` 并由 Git 忽略，公开候选不跟踪具名项目配置或项目基线。
  2. 修复 `scan.sh`、配置解析和 checker 的假通过路径；未知参数、非法配置、checker 崩溃、空输出、非法 JSON 或预期 checker 缺失必须非零退出并生成 hard finding。
  3. 让 adaptive 基线真实参与判断，并把状态按被体检项目隔离。
  4. 将首跑基线并入统一 CLI；首个公开候选不包含自动切分支、改文件、push 或创建 PR 的写入流程。
  5. 合并版本化 eval runner，补退出码、结构化结果完整性、故障注入、相对断链和项目状态隔离断言；开发测试与发布运行单元分离。
  6. 补齐依赖、兼容性、隐私、LICENSE、版本和公开发布文档口径。
- **非目标**：
  - 本任务不实际迁入公开 `skills/`、不登记 Marketplace、不开 PR、不推送或发布版本；这些动作在候选验收通过后另行执行。
  - 不改写被体检项目的业务文档语义，不处理其它 Skill 的任务。
- **验收标准**：
  - [x] Git 发布候选只跟踪通用 `default` 与去具体化 example；具名项目配置以 `config/*.local.yaml` 留在本地 Skill 中供运行时读取，且不被跟踪；Skill 根 `state.json` 不再被跟踪。
  - [x] 默认扫描、单 checker 扫描、基线初始化、未知参数、非法 YAML/regex、checker crash/空输出/非法输出均有确定退出码和结构化结果。
  - [x] adaptive 规则按当前项目 state/config 阈值分支，不再无条件告警；两个项目使用同一安装副本时状态互不污染。
  - [x] 首发运行路径默认只读；任何 Git/PR 写入能力不在公开入口中。
  - [x] 固定回归断言退出码、合法 JSONL、关键 rule、相对断链、故障阻断和项目状态隔离。
  - [x] macOS/Linux 依赖与版本要求在首次执行位置可见，缺依赖时给出清晰提示。
  - [x] `bash -n`、ShellCheck、敏感信息扫描、引用检查、`skill-lint` 静态审计及代表性端到端用例均有可复查证据；正式稳定性门禁标记 `NOT_VERIFIED`。
- **执行证据**：
  - 两份现有项目配置已迁回 `config/faropdf.local.yaml` 与 `config/legal-ai-skill-book.local.yaml`；`.gitignore` 的 `doc-curator/config/*.local.yaml` 规则命中，两者物理可读但不进入 Git 发布面。
  - `scripts/test-doc-curator.sh`：39 PASS / 0 FAIL；覆盖退出码 0/1/2/64、CLI 作用域、默认/单 checker、双项目基线隔离、非法 YAML/regex、相对断链、context/decision-sync 正反分支及三类 checker 故障。
  - `bash -n`：通过；ShellCheck `--severity=warning`：0；`skill-lint` `harness_failure_audit.py audit`：PASS，0 finding。
  - 自体 Markdown 引用检查：exit 0，1 条实际链接、0 broken；敏感关键词/绝对用户路径文件扫描：0 命中。
  - `instruction_stability_gate.py assess`：exit 2 / `NOT_VERIFIED`，明确缺少 constraint contract、逐约束映射和三轮 evaluator-signed evidence；不属于本次发布候选整改的完成声明。

- [x] 0.5.1 config 驱动化补强（2026-07-25）：合并修复 CHANGELOG 0.3.1 / 0.4.0 段声明「留待后续」但 TASKS 漏登的 3 项已知盲区——① `tasks-active-count` pattern 改读 config（默认扩展兼容 Task#N，修复 v0.3.1 盲区）；② `markdown-link-broken` severity 按 path glob 细分（实现 v0.4.0 原计划 hard + adaptive 双档）；③ `active-zone-residue` 段标题与编号 pattern 改读 config（默认兼容 ISS-N）。顺带修复 v0.4.0 BSD awk `match()` 三参捕获组崩溃（改 POSIX 写法）。新增 `common.sh` `cfg_list_indices`/`cfg_list_field` helper；4 个 config 文件全部更新；`evals/run-evals-v051.sh` 11 例全 PASS。决策见 D-2026-07-25-01。
- [x] 0.5.0 working-tree + monorepo Skill 根识别（2026-07-12；PR #96 review correction 2026-07-13）：新增 `--working-tree`，完整纳入 staged/unstaged/untracked，且与 `--range/--since` 互斥 hard；历史/工作树扫描错误统一 hard；规模按 insertions+deletions churn 判定；历史 Skill 根绑定 range 右端 tree。`context-sync-skill-internal` 兼容 `.claude/skills/<name>/` 和仓根 `<name>/`，未知配置布局 adaptive、无关路径不误判，仓根 Skill 不再误报顶层 DEC。26/26 context-sync + 6/6 decision-sync 回归通过。来源 legal-ai-skill-book Task T157 / DEC-128；决策见 D-2026-07-12-02。
- [x] 0.4.0 markdown-link-broken + active-zone-residue 双盲区检测（2026-07-12）：实现见下方功能增强完成项与 D-2026-07-12-01。0.5.0 同步纠正该版 CHANGELOG 已声明升版、但 SKILL frontmatter 仍为 0.3.1 的版本漂移。
- [x] 0.3.1 `tasks-archived-iss-pointer` 鲁棒化（2026-07-05）：规则 3 去中文字面串依赖，改检测归档段标题（`^##[^#].*归档`）+ DECISIONS 引用。来源 legal-ai-skill-book TASKS 四区重组 PR#194 措辞回归（"归档任务"→"任务"）暴露的脆弱，PR#195 临时补字面串，本版彻底修。frontmatter 0.3.0 → 0.3.1。规则 2 active-count 前缀脆弱（ISS- vs Task#）未改，留 config 驱动后续。决策见 D-2026-07-05-01。
- [x] 0.2.4 删除 `GENERALIZATION-SPEC.md`（2026-07-03）：v0.2.0 泛化设计 spec 已执行落地，原始 spec 中 A+B+C 三目标已沉淀到 DECISIONS D-2026-06-30-01 与 CHANGELOG 0.2.0 段；删 spec 避免双份信息源漂移。SKILL.md §参考段移除相应条目并加注脚；frontmatter version 0.2.3 → 0.2.4；evals/run-evals.sh 顶部注释去除 §6 引用。
- [x] 0.2.3 skill-lint 审查闭环（2026-07-02）：LICENSE.txt copyright 改 `doc-curator contributors`；frontmatter `homepage` / `author` 与 `skills/` 下其它公开 Skill 统一口径；`scripts/maintenance-pr.sh` 新增 `--dry-run` / `--help`；SKILL.md §4 加「真跑前先跑 `--dry-run`」条款；SKILL.md §6 末尾加「跨项目分发提醒」段（state.json baselines 跨项目需重建）；删 `.DS_Store`；description 精炼到 ≤250 字符。来源：skill-lint 审查报告。
- [x] 0.2.2 context-sync-skill-internal 三件套检测（2026-07-01）：emit_skill 检查 skill 内部 CHANGELOG+DECISIONS+TASKS 三件套（之前只查 CHANGELOG），对齐 feedback_legal_skill_internal_docs。来源本项目用户 feedback。
- [x] 0.2.1 SKILL.md §0 调用协议（2026-07-01）：subagent 调用协议下沉 skill 内部（默认 spawn inline subagent 隔离上下文，不建项目级 `.claude/agents/`，可移植）。来源本项目 DEC-081 + 用户 feedback。
- [x] 0.2.0 泛化（2026-06-30）：config 彻底驱动（`resolve_config` + `yaml-flatten.awk` + 3 check 改读 config）+ 上下文同步检测（`check-context-sync.sh`，5 个 `context-sync-*` rule，DEC-052 动态选查）+ SKILL.md 去 FaroPDF + scan.sh 参数化（`--config`/`--since`/`--range`/`--only`/`--repo`）。来源：legal-ai-skill-book 游初定稿收口链（PR #145-159），目标 A+B+C（config 泛化 + 上下文同步检测 + 去 FaroPDF）。
- [x] 0.1.0 首版发布：`scan.sh` 体检（hard/adaptive/soft 三档）、`first-baseline.sh` 首跑基线、`maintenance-pr.sh` 自动 trim PR、`config/faropdf.yaml` 规则集、`state.json` 基线与历史
- [x] 0.1.1 新增 §1.4 与 worker PR 协调的窗口期约束（来源：FaroPDF v0.1 Wave 1 PR #18 / #19 合并复盘）

## 待办事项

### 功能增强

- [x] **P0：总入口与子 checker 全链路 fail-closed（0.6.0）**：逐 checker 验证退出码、身份、非空 JSONL 和 schema；异常统一 hard 阻断。
- [x] **P0：移除不安全 maintenance 写入链（0.6.0）**：首发候选不再包含 trim、切分支、commit、push 或 PR；数据保全与远端回执风险随入口删除而闭合。
- [x] **P1：自适应基线真实判定并按项目隔离（0.6.0）**：使用项目 state/config 阈值；状态绑定项目与配置哈希。
- [x] **P1：状态副作用与文档声明一致（0.6.0）**：默认 scan 保持只读，删除 history/last_scan 未实现承诺；只有 `--init-baseline` 写项目本地状态。
- [x] **P1：配置、CLI 与结构化输出 fail-closed（0.6.0）**：非法 YAML/regex、缺参数、未知参数/checker、JSON 转义和 schema 校验均有固定回归。
- [x] **P2：首跑基线与 config/state 模型统一（0.6.0）**：并入 `scan.sh --init-baseline`，按 files/role/pattern 建立当前项目基线。
- [x] 规则集扩展 → 0.2.0 已完成：config 泛化（`template.yaml`/`default.yaml`/`legal-ai-skill-book.yaml` + `resolve_config` 自动发现 + 现有 check 读 config）
- [x] **决策变更同步检测（v0.3.0 已完成 2026-07-03）**：DEC supersede / 改变 → 引用该决策的 TASKS / 规范文件 / 其他文档是否同步更新（用户反馈"运营中决策后续改变但文档未同步"，2026-07-01，本项目 DEC-081）。落地：`scripts/lib/check-dec-ref-sync.sh`（237 行）+ scan.sh 注册 `--only decision-sync` + 4 yaml `dec_ref_sync` 段 + `evals/run-evals-v03.sh` 6 用例 6/6 PASS + SKILL.md §2 加 `dec-ref-sync-*` rule 行。spec 见 `V0.3.0-SPEC.md`，决策见 D-2026-07-03-02。
- [x] **体检报告可视化（v0.7.1 已完成 2026-07-31）**：`render-report.sh` 与 `scan.sh --report` 已生成按 severity 分组的 Markdown 报告；v0.9.0 进一步让显式报告生成失败以 hard 闭合。
- [x] **取消 `state.json` history（0.6.0）**：默认 scan 只读，状态只保留显式初始化的当前基线，不再维护无消费方的无限 history。
- [x] **`tasks-active-count` 规则 config 驱动化（v0.5.1 已完成 2026-07-25）**：规则 2 当前写死 `^### ISS-[0-9]+`，对用 `### Task#N` 组织的项目盲报 0。改读 config `adaptive_rules.tasks-active-count.active_count_pattern`；默认值扩展为 `^### (ISS-[0-9]+|Task[# ]+T?[0-9A-Za-z-]+)` 兼容两类编号。**来源**：CHANGELOG 0.3.1 Known Limitations（留待后续），TASKS 此前漏登。
- [x] **`markdown-link-broken` 严重度细分（v0.5.1 已完成 2026-07-25）**：v0.4.0 实现是全 hard 单档，原计划区分 hard（指向已删除核心文档如 DEC-NNN/`docs/`）+ adaptive（指向可选引用如 `reviews/`/`source-material/`）。加 config 段 `markdown_link_broken.severity_paths`（path glob → severity），未匹配走 `default_severity`（默认 hard，向后兼容）。**来源**：CHANGELOG 0.4.0 Known Limitations（留待 v0.4.1+），TASKS 此前漏登。
- [x] **`active-zone-residue` 标题模式 config 驱动（v0.5.1 已完成 2026-07-25）**：当前 awk 写死 `## 活跃区` 等段标题 + `Task[#　 ]+T?([0-9A-Za-z-]+)` 编号 pattern；其它项目（如用 `### ISS-N`）需 config 驱动。加 config 段 `active_zone_residue.zone_headings` + `active_zone_residue.residue_pattern`；默认 pattern 扩展兼容 `ISS-N`。顺带修复 v0.4.0 BSD awk `match()` 三参捕获组崩溃（改 POSIX `match()` + `substr`）。**来源**：CHANGELOG 0.4.0 Known Limitations（留待 v0.4.1+），TASKS 此前漏登。
- [x] **markdown-link-broken 检测（v0.4.0 已完成 2026-07-12）**：检出 markdown 链接 `[text](path)` 或反引号 `` `path` `` 引用指向仓内不存在文件 → emit hard（指向已删除核心文档如 DEC-NNN 段或 `docs/` 路径）或 adaptive（指向可选引用如 `reviews/`、`source-material/`）。**来源**：legal-ai-skill-book 项目 2026-07-10 文档一致性治理深度轮 4（PR #306）。实地勘察发现 `docs/audit-ssot-matrix.md` 已由法律书仓 DEC-071 决定删除（2026-06-24, PR #140），但 6 处跨文档引用未同步（STYLE-GUIDE / PROJECT-SCOPE / WORD-COUNT-PLAN / REVIEW-PROCESS / source-material/章节知识依赖矩阵 5 处 + 表格 inline 引用），构成 stale 引用。doc-curator scan.sh 现有 `dec-ref-sync-removed-still-referenced` 规则只检 DEC 编号物理删除，不检 markdown 文件链接断链——是已知盲区。**实施路径**：`scan.sh` 加 awk/grep 规则扫 `[text](path)` 与反引号路径指向不存在文件；`config/template.yaml` + `config/faropdf.yaml` + `config/legal-ai-skill-book.yaml` 加 `markdown_link_broken` 段；SKILL.md §2 表格加新 `rule_id`；CHANGELOG/TASKS/DECISIONS 三件套同步。**严重度**：hard（指向已删除核心文档）+ adaptive（指向可选引用）。**关联**：法律书仓 DEC-123 / Task T150 / `feedback_legal_skill_internal_docs`。
- [x] **active-zone-residue 检测（v0.4.0 已完成 2026-07-12）**：对每个活跃区 `### Task#N` / `### Task TN` 编号，与归档区同编号比对；对每对同号项输出上下文片段（活跃区段标题 + 归档区段标题）供 PM 人工判别是「真残留」还是「合理交叉引用/编号撞号/多批次」。**emit adaptive**（不是 hard，因为需人工判别合理性；不阻断 PM merge）。**来源**：legal-ai-skill-book 项目 2026-07-10 文档治理深度轮 1（PR #300, DEC-121）。手工清理时发现 T146/T147 完整条目（80 行）残留——归档区已有归档行（`TASKS.md` 归档区 line 255-257），活跃区未清。doc-curator scan.sh `tasks-archived-iss-pointer` 规则只检「归档条目指向 DECISIONS」，不检「活跃区是否残留已归档条目」——是已知盲区。法律书仓审计 subagent 人工核验确认当时 6 个活跃↔归档同号（Task#86 / Task#102-107 / Task#104 撞号 / Task#110 指针引用 / Task#124 子任务拆分）都是合理交叉引用/编号撞号/多批次场景，无真残留——但这是人工判别，不是 doc-curator 自动规则。**实施路径**：`scan.sh` 加 awk 规则扫活跃↔归档同号；config 加 `active_zone_residue` 段；SKILL.md §2 表格更新；CHANGELOG/TASKS/DECISIONS 三件套同步。**与 markdown-link-broken 互补**：一个 hard（机械判断文件存在性），一个 adaptive（人工判别同号合理性）。**关联**：法律书仓 DEC-124 / Task T152 / DEC-121 已知盲区。

### 测试与回归

- [x] **统一确定性回归（0.6.0）**：删除三个版本化 eval runner；新入口保留真实退出码并覆盖合法 JSONL、关键 rule、非法 YAML/regex、未知参数、双项目 state、相对断链和 checker crash/空输出/非法输出。
- [x] **取消 maintenance 端到端链（0.6.0）**：发布候选不包含写入 Git/PR 的实现，因此不再以测试掩盖高风险设计；未来恢复时另立任务和隔离验收。
- [x] **补齐 `dec-ref-sync-synced` 可达分支（0.6.0）**：临时 Git fixture 保留旧 ID，引用行提交时间晚于 supersede 日期，明确断言 `dec-ref-sync-synced` 与 exit 0；另有未来日期 stale 反例。
- [ ] **建立 instruction-stability contract 与候选绑定回执**：为 fail-closed、只读边界、项目 state 隔离和结构化结果完整性分配稳定 constraint ID，建立“约束 → checker → stage → positive/fault/historical case”映射；至少三轮运行并保留 evaluator-signed 候选绑定 evidence。当前 39 项单轮回归只证明本次候选的代表性行为，不作为长期稳定性证明。
- [x] **hard / adaptive / soft 端到端退出路径（0.6.0）**：固定回归覆盖 exit 0/1/2，参数错误覆盖 64；发布候选无 maintenance 触发逻辑。
- [ ] 自适应基线 × 1.5 阈值在真实成长期项目上的误报率统计
- [x] **取消 PR 窗口期耦合（0.6.0）**：Skill 不再自动建分支、push 或提 PR，不与 worker 合并窗口抢占共享 HEAD。

### 边界维护

- [x] **明确只读交接边界（0.6.0）**：SKILL.md 规定只报告 finding，不改写 CHANGELOG 或其它业务文档。
- [x] **不提供隐式 hook（0.6.0）**：公开候选只接受显式调用，不安装 hook、cron 或后台触发器。
- [ ] **发布通道元数据定稿**：当前 frontmatter 遵循 legal-skills / ClawHub 项目规范，保留顶层 `version`、`author`、`homepage`；官方 `skill-creator quick_validate.py` 只接受最小字段并会拒绝这三个发布字段。实际发布前按目标平台决定保留、转换或生成平台专用包，避免把某一平台 schema 冒充通用标准。
- [ ] **正式发布动作**：候选验收后再迁入公开 `skills/`、同步根 README 与 Marketplace、执行发布提交/推送；本任务未授权这些外部发布动作。
