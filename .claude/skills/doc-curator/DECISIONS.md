# 决策记录

> 本文件记录 doc-curator 的核心技术决策，与 [CHANGELOG.md](CHANGELOG.md)（版本变更）互补：CHANGELOG 记"改了什么"，DECISIONS 记"为什么这么做"。

---

## D-2026-09-13-01 单值声明核对与语义真实性分层，新增能力用 schema 3 防旧 reader 假绿

- 日期: 2026-09-13
- 背景: 项目 retrospective 中存在“问题清单被入口描述成已有回答”和“状态镜像与源卡不一致”的确定性风险；已有最大编号检查不能发现它们，但这不意味着需要自动裁决访谈含义。
- 决策: 新增 opt-in `value_claims`，通过各文件唯一整行 ERE 的一个非空捕获组提取值，核对允许值集后作 exact comparison。保留旧 `index_claims`；任务字段完整性与队列表状态继续归 `task_source_contract`。正确声明的标签同样可能没有真实执行证据，因此 ok 只称“声明一致”。
- 兼容选择: 新字段要求 schema 3；新版仍读取 schema 2 的原有配置。拒绝“schema 2 加可忽略字段”的方案，因为旧 reader 在同时存在 index claims 时会静默跳过新检查。
- 安全边界: 两类 claim 共用仓内读取边界及唯一 ID；未知字段、缺失/重复/提取错误均阻断，不能用更宽 regex/更新标签掩盖事实冲突。消费项目先做显式小范围 pilot，不扩张到全部历史任务卡。
- 配置读取补证: 大合法输入的 `printf | grep -q` 在 pipefail 下可因 producer SIGPIPE 误报失败；command substitution 内的 getter 又可能吞掉 load 失败。改为完整消费、显式返回错误与 checker 入口一次性加载，不重写 YAML 解析器；基线初始化沿用同一 config 合同而非另写 schema 白名单。原书仓单次 warning 未复现，不把这些隔离反例写成该次事故的已证原因。
- 验证: DOC-007 记录确定性测试；标签所述工作是否真实发生仍由原始证据审计确认。

---

## D-2026-09-02-01 merge-gate 以同一单次 scan 输出固定 schema 紧凑摘要，配置缺失失败闭合

- 日期: 2026-09-02
- 背景: routine merge review 中，主 Agent 为得到"阻断什么、下一步做什么"，被迫消费全量 JSONL（代表性 fixture 实测 4273 bytes / 20 行，其中大部分是 ok/soft 明细），SKILL §0 又默认为此 spawn subagent。成本集中在传输与上下文，而不是扫描本身；因此正确解法是压缩输出契约，不是减少 checker。
- 决策:
  1. **同一入口、同一单次扫描**：`--profile merge-gate` 是 scan.sh 的显式输出模式，不是新编排器。checker 集合、执行顺序、逐条 finding 与退出码计算全部复用 legacy 路径；摘要从同一次运行的 RESULTS_FILE 聚合，不二次调用 scan。回归以影子 Skill wrapper 断言 legacy 与 compact 调用序列逐行一致（9 checker 各一次）。
  2. **固定 schema 单行摘要**：`doc-curator.merge-gate.v1`，键序固定——精确 base/head/range（range/since 解析为完整 SHA；解析失败 `merge-gate-range-unresolved` hard 并置空，不以未解析范围出结论）、config provenance、hard/adaptive/soft/ok 计数、`blocking_rule_ids`/`adaptive_rule_ids`、`next_action`（pass/resolve-hard/review-adaptive/provide-config/fix-invocation）、`exit_code`。错误路径（64/65/66/78 与配置解析失败）输出同 schema 的 `status:error` 摘要，`exit_code` 恒为末键。
  3. **fail-closed 平移，不降级**：退出码与 legacy 完全一致（0/1/2/64/65/66/78）；摘要计数与 rule IDs 经回归断言与 legacy 逐条一致。省的是 stdout 带宽与主 Agent 上下文，不是审查严格度。
  4. **配置缺失失败闭合，不自动生成**：门禁模式解析到 `bundled-default` 兜底时 emit `config-required` hard（NOT_VERIFIED）并路由 `provide-config`；SKILL 同步约定 reviewer 不得在 Skill 安装目录自动写 `<repo>.local.yaml`。legacy 模式不受影响（回归断言仍 exit 0），因为该 hard 只属于显式门禁合同。
  5. **完整 JSONL 是显式旁路**：`--jsonl-out <file>` 原子写入调用方指定文件（mktemp + mv，失败 `jsonl-output-error` hard），内容与 legacy stdout 逐字节一致；默认不写任何文件、不输出全量 JSONL。`--profile merge-gate` 与 `--init-baseline`/`--report` 互斥，`--jsonl-out` 仅限门禁模式。
  6. **subagent 协议按摘要路由**：§0 增加 merge-gate 例外——`pass` 或仅机械可修 hard 时主 Agent 直接消费摘要，不为解释结果默认启动 subagent；adaptive、`provide-config`、`fix-invocation` 或语义不明时仍需 Agent 解释。机器给路由信号（next_action），协议留给调用方执行。
- 影响:
  - routine merge review 的扫描结论从 4273 bytes / 20 行降到 618 bytes / 1 行（14.5%）；结合 §0 例外，零 finding 场景不再有 subagent 启动成本。
  - 门禁模式新增三类自有 hard（`config-required`、`merge-gate-range-unresolved`、`jsonl-output-error`），都只在显式新模式下出现；legacy 消费者零影响。
  - 后续若增改 checker，无需改摘要 schema——计数与 rule ID 集合自动跟随 RESULTS_FILE。
- 权衡: 放弃"摘要含全部 finding 明细"（需要明细时走 `--jsonl-out` 或 legacy 模式）；接受错误行比成功行多一个 `error` 字段（其余键名与键序完全一致）。
- 验证: `scripts/test-doc-curator.sh` 226 PASS / 0 FAIL（新增 35 项：rule ID/计数等价、单次调用序列、618 vs 4273 bytes ≤50%、受控 TMPDIR + manifest 写边界、config-required 不生成配置、错误路径单行 fail-closed、range 解析失败、working-tree 精确 head、jsonl-out 逐字节一致）；`bash -n doc-curator/scripts/*.sh`、`git diff --check` 通过。真实工作流端到端 token 节省与无旧上下文 Agent §0 稳定性 `NOT_VERIFIED`。
- 状态: 待独立 reviewer 复核（DOC-006 保持 REVIEW）。

---

## D-2026-08-28-01 v0.9.0 以显式配置合同和声明式真相检查闭合静默假绿

- 日期: 2026-08-28
- 背景: 法律书仓 retrospective 全量审计暴露一组同根问题：v0.2.1 已下沉到 SKILL §0 的默认 subagent 协议在 v0.6.0 重写中被删除，但本文件、CHANGELOG 和消费项目继续引用；项目根旧 `doc-curator.yaml` 优先于 Skill profile 且缺 `context_sync.enabled`，v0.8 以默认 false 静默跳过；Markdown URL 编码和概念型伪链接制造大量 hard；DEC 连续性把正文引用当 ID；子 checker 与报告失败可在入口层假绿。
- 决策:
  1. 恢复 SKILL §0，并把“默认新 subagent、Skill 自包含、无项目薄壳”重新作为调用合同。若调用者本身已是隔离 worker 或平台无 subagent，允许当前会话执行并明确原因。
  2. 配置升级为 `schema_version: 2`。总入口先跑 `config` checker；业务字段缺 `enabled`、required checker 关闭、未知 required checker、重复 flatten 路径、自动发现候选配置内容不同均 hard。选择“显式迁移”而非兼容旧配置，是因为继续填默认值或对重复 key 默认首值胜出，都会把“未验证”伪装成“无需验证”。
  3. 决策身份只来自可配置标题 pattern；`decisions-id-unique` 检重复，连续性复用同一标题集合。正文 DEC 引用不再参与身份集合。
  4. Markdown 先 URL decode 和 repo-relative 词法归一化，再检查目标；已存在目标额外解析物理路径，允许仓内 symlink，禁止穿透仓外。严重度分别允许按源文件、归一化目标和伪目标 pattern 配置。文件存在、路径逃逸、编号形态和严重度由脚本确定；“概念编号是否应改为文件链接”只给 soft/adaptive，不擅自改写。
  5. 新增声明式 `context_truth.index_claims`，首版只支持 `max_numeric_suffix`。它确定性比较 P/L 等权威条目最大编号与镜像范围；启用即必须有 claim，mirror pattern 必须唯一命中，旧范围不得被另一条更大数字掩盖。ROADMAP 里程碑是否真实完成、任务证据是否足以支持状态，仍需 adaptive/人工审核，避免用关键词假装语义理解。
  6. child finding 与退出码严格双向绑定（hard→1，仅 adaptive→2，否则→0）；显式 disabled checker hard。显式 `--report` 是用户要求的交付物，生成失败 hard，supersede D-2026-07-31-02 中“报告失败不影响 scan 退出码”的选择。
- 影响:
  - 所有项目配置必须迁移 schema v2；这是有意的 fail-closed 变化。全量扫描可保留未启用 checker 的 soft `NOT_VERIFIED`，但显式验收不能用 disabled checker 取得 exit 0。
  - 确定性脚本负责：schema/provenance、enable contract、标题 ID、路径 decode/归一化/存在性、声明式索引、child/report 执行完整性。adaptive/人工负责：路线图完成真实性、任务证据质量、伪链接意图和无旧上下文 Agent 的长期协议遵循。
  - 配置来源 hard 会促使项目只保留一个权威配置；需要临时比较时用显式 `--config`，其 provenance 仍写入结果。
- 验证: `scripts/test-doc-curator.sh` 167 PASS / 0 FAIL；`quick_validate.py`、Bash 语法、ShellCheck warning 级、diff check 通过；Skill 自扫 rc 0（hard 0 / adaptive 0 / ok 4 / soft 8）。动态 dogfood 快照（非稳定常量）使用候选入口 `feat-doc-curator-retrospective-audit/doc-curator`、消费 worktree `retrospective-full-audit-20260828`，其 `HEAD=baa0459c`、`base/origin-main=69426fc5`、13 项 working-tree 变化；显式 `--working-tree` 全扫 rc 1，14 hard（13 duplicate + 1 gap）/ 181 adaptive（179 broken + 1 pseudo + 1 active-count）/ 15 ok / 8 soft。消费项目完整历史 range 与无旧上下文 Agent 前向测试保持 `NOT_VERIFIED`，不得据本地脚本回归扩大声明。
- supersede: D-2026-07-31-02 第 4 点（显式报告失败只警告、不改变退出码）；D-2026-07-01-01 的 subagent 设计不变，本决策仅恢复其被后续重写误删的 SKILL 落点。

## D-2026-08-14-01 per-project config + state 收进 Skill 内部统一管理

- 日期: 2026-08-14
- 背景: 用户在 folia 接入 doc-curator 时，不愿把 `doc-curator.yaml` 裸放项目根（"随地大小便"），进一步要求所有 doc-curator 产生的副作用文件（config + state.json）都收进 Skill 内部统一管理，被体检项目根目录零污染，"各地统一调用时所有状态都存 Skill 内部更方便管理"。旧设计（v0.5.x 起，SKILL.md 原文「不写入 Skill 安装目录」）把 state 写项目本地 `.doc-curator/`，与本诉求冲突。
- 决策:
  1. **config 按 repo basename 自动发现**：`resolve_config` 新增档位 `$SKILL_ROOT/config/<basename>.local.yaml` / `.yaml`，插在项目根 config 之后、`default.yaml` 之前。私有项目配置不再必须手动 `--config`，但显式 `--config` 仍最高优先级，项目根 config 仍第 2 档（Funes 等已接入项目零影响）。
  2. **state 按 project_id 命名进 Skill 内**：`STATE_FILE` 默认改为 `$SKILL_ROOT/state/$PROJECT_ID.state.json`，`PROJECT_ID = sha256(规范化 $REPO_ROOT)`。同名不同路径的项目（如 folia 与 personal-site/dist/folia）project_id 不同，state 文件互不覆盖，彻底规避 `--init-baseline` 无条件 `mv` 冲掉同名项目基线的风险。
  3. **反转「state 不进 Skill」旧原则**：接受 Skill 目录变可写（symlink 安装时穿透写源）；多项目共享同一 Skill 物理目录时，`state/` 下按 project_id 各一份，不互相覆盖。
- 影响:
  - 被体检项目根目录不再产生 `.doc-curator/`；所有 per-project 副作用集中在 Skill 的 `config/` 与 `state/`。
  - `state_project_matches` 的 project_id + config_sha256 校验语义不变，天然兼容新位置。
  - Skill 仓库 `.gitignore` 新增 `doc-curator/state/`；`config/*.local.yaml` 既有忽略保留。state 与私有 config 都不入库、不进发布包。
  - 现有项目根 config（Funes）与显式 `--config`（FaroPDF）调用方式不中断；FaroPDF 还能省掉手动 `--config`（basename 匹配自动发现）。
- 权衡:
  - config 用 basename（人维护、可读）而非 project_id：接受极低碰撞风险（仅当对同名构建产物目录跑 `--repo` 体检时）；state 用 project_id 强隔离，零碰撞。
  - Skill 目录变可写：放弃「Skill 安装目录只读」的洁癖，换取 per-project 副作用集中管理。state 含 project_id 哈希与行数基线，不含明文路径，不泄露用户目录结构。
- 验证: `test-doc-curator.sh` 92 → 95 PASS / 0 FAIL（新增同 basename project_id 隔离用例）；folia 真机确认 config 自动发现 + state 进 Skill 内 + 项目根无 `.doc-curator/`。
- 来源: 用户要求 doc-curator 的 per-project config 与 state 收进 Skill 内部统一管理，避免污染被体检项目根。

---

## D-2026-08-01-01 报告渲染采用固定 schema 受控解码并收窄元数据

- 日期: 2026-08-01
- 背景: v0.7.1 的报告渲染器先用正则认可转义字符，却用 `sed [^\"]*` 提取字段，合法 message 一旦包含 `\"` 就在引号处截断；端到端测试还用 `|| true` 丢弃基线初始化退出码，触发 `skill-lint` `HRA-001`。报告被定位为可分享产物，但头部写入完整本机绝对路径，也超出了最小必要披露。
- 决策:
  1. 保持零新增运行依赖，不引入 jq/Python；利用 doc-curator JSONL 字段顺序固定的合同，按相邻字段分隔符提取原始字符串，再只解码允许的 JSON 转义。`\n / \r / \t / \b / \f` 在 Markdown 单元格中归一为空格，避免破坏表格。
  2. checker、rule、message、suggestion 和报告元数据统一经过 Markdown 转义；由 `scan.sh` 只向报告传项目目录名与配置文件名，不传绝对路径。
  3. 回归测试不得丢弃被测命令退出码。基线初始化、扫描与报告生成分别保存并断言真实结果；正文的写入边界同步改为“默认只读，两个显式限域写入口”。
  4. 启用任务源合同时，显式状态是活跃性的单点真相，跳过旧的 H3 标题总数型 `tasks-active-count`；否则完成历史会随着时间积累并产生必然误报。多个 `IN_PROGRESS` 继续由状态合同单独提示。
- 影响:
  - 合法 finding 中的引号、反斜线和控制字符不再造成报告截断或表格错列。
  - 分享报告时不再默认暴露本机目录结构；finding 本身仍可能包含项目相对路径，分享前继续按安全边界复核。
  - JSONL schema 和 scan stdout 契约不变，既有机器消费者不受影响。
- 验证: `scripts/test-doc-curator.sh` 92 PASS / 0 FAIL；新增转义、退出码与 DONE 历史反例。instruction-stability 正式门禁仍为 `NOT_VERIFIED`。
- 来源: 2026-08-01 用户要求再次审查并提交；`skill-lint` Harness 静态审计与合法 JSONL 转义反例。

---

## D-2026-07-31-02 体检报告可视化：Markdown 渲染 + 不改 stdout 契约

- 日期: 2026-07-31
- 背景: scan.sh 的 stdout 是纯 JSONL（机器可读），用户需要一份人读的可分享报告（类似 Word 的文档），便于发给协作者或纳入 PR 描述。这是 TASKS 历史待办「体检报告可视化」的延续（v0.6.0 整改时旧待办被重组，但可视化需求仍在）。
- 决策:
  1. **格式选 Markdown 而非 Word/HTML**：零外部依赖，纯 bash + awk + sed 生成，GitHub/IDE 直接渲染，符合 doc-curator「轻量、失败闭合、无重依赖」定位。Word 需 pandoc 等重依赖，与 skill 风格冲突；HTML 观感不如 docx 且属第二种人读格式。如后续真需 Word，让 render-report.sh 输出的 .md 经 md2word skill 二次转换，不在 doc-curator 内置。
  2. **只报问题项（hard/adaptive/soft 逐条，ok 仅计数）**：报告聚焦「哪里要修」，避免 ok 档多时报告冗长。全量输出仍可通过 scan.sh 的 JSONL 获取（报告是衍生视图，不是替代）。
  3. **不改 scan.sh 的 stdout JSONL 契约**：新增独立脚本 `render-report.sh` 消费 JSONL 产出 .md；scan.sh 加 `--report <path>` 参数仅作编排（跑完后调 render 写文件），stdout 仍纯 JSONL。保护机器消费链（CI、其它脚本依赖 JSONL）不被破坏。
  4. **报告生成失败不影响 scan 退出码**：报告是附加产物，render 失败只 stderr 警告，scan 退出码仍由 finding 严重度决定。避免「报告写盘失败」误判为「体检 hard 失败」。
  5. **容错**：render-report.sh 对非法 JSONL 行跳过并 stderr 警告（不崩溃），空输入生成「无结果」报告（非空文件）。与 scan.sh 失败闭合风格一致——但 render 的失败是软失败（尽力渲染），不是 hard fail。
- 影响:
  - 用户一条命令（`scan.sh --report report.md`）即可获得人读报告，无需手动解析 JSONL。
  - 报告可纳入 PR 描述、邮件附件或存档；需要 Word 时经 md2word 转换。
  - stdout 契约不变，所有既有 JSONL 消费者（CI、脚本）零影响。
- 验证: `test-doc-curator.sh` 55 → 81 PASS，新增 26 个报告相关断言覆盖合法/空/非法输入、`|` 转义、端到端、参数互斥。
- 来源: 用户要求体检报告可视化（「至少需要一份类似 Word 文件之类的东西，或者是 Markdown 文件之类的」）。

---

## D-2026-07-31-01 以状态驱动、配置适配的任务源合同落实 AgentCMD v4

- 日期: 2026-07-31
- 背景: 用户级 `AGENTS.md` v4 将 `TASKS.md` 或项目指定文件定义为当前任务源，要求承载当前队列、任务边界、状态、验收与执行证据；信息不足时应保持草案或阻塞，完成时应回写证据或未完成原因。`doc-curator` 原有 tasks checker 只检查数量、行数、日志 trim 和归档指针，无法审计这组职责。用户确认该能力应由 `doc-curator` 承担，并可用于检查其它 Skill 的上下文文件。
- 决策:
  1. 新增 `task_source_contract` 配置段，但在 `default.yaml` 保持关闭；通过 `agentcmd-v4.example.yaml` 显式启用，直接审计被检查 Skill 根目录的 `TASKS.md`。这使新规范可立即使用，同时不把一种任务体系强加给历史项目。
  2. 以任务状态决定合同完整度：`DRAFT` 可不完整；`READY / IN_PROGRESS` 要求缘由、目标与非目标、输入依赖、范围、停止条件和验收；`REVIEW` 要求交付物与证据；`DONE` 要求证据；`BLOCKED / CANCELLED` 分别要求原因。
  3. 不固定 Markdown 标题正文。任务 ID、排除 ID、任务卡标题、空队列表达、状态字段、状态词、字段语义正则和各状态 requirements 均由配置声明；项目级规则可覆盖公开档案。
  4. 对无法可信继续的情形失败闭合：启用合同却缺任务源、正则或字段配置非法、队列与任务卡状态冲突、需完整合同却缺卡或字段时返回 hard。多个 `IN_PROGRESS` 只作为 adaptive，因为并行执行可能是项目真实选择。
  5. `TASKS.md` 可以没有当前任务，但必须显式声明空队列；历史完成项仍可保留，审计器按显式状态检查其证据，不通过“没有活跃任务”等同于没有任务源。
- 影响:
  - 其它 Skill 可以用一条命令获得确定性 TASKS 审计，但首次适配时仍需确认项目状态词、任务编号和字段表达是否与公开档案一致。
  - `DONE` 的存量任务若只有勾选或状态而没有证据，会被标为 hard；这是对 AgentCMD v4 完成边界的直接落实，不是格式美化提示。
  - 默认扫描行为不变；只有显式选择 AgentCMD 档案或在项目配置中启用合同，才执行新增规则。
- 验证:
  - `scripts/test-doc-curator.sh`：55 PASS / 0 FAIL，覆盖各关键状态的正反分支、状态冲突、空队列和缺文件。
  - `doc-curator`、`legal-visualization` 与 `universal-media-downloader` 三个真实 Skill 样本均通过 AgentCMD 档案，覆盖完整合同与 DRAFT 宽容分支。
  - `bash -n`、ShellCheck、差异格式、Harness 静态审计与 Markdown 引用检查均通过。
  - 三轮 evaluator-signed instruction-stability evidence 尚未建立，保持 `NOT_VERIFIED`。
- 来源: 2026-07-31 用户要求将最新 `AGENTS.md` 中的 `TASKS.md` 规范加入 `doc-curator`，用于审计其它 Skill 的上下文文件。

---

## D-2026-07-30-01 公开候选采用只读核心、项目状态外置与显式 opt-in

- 日期: 2026-07-30
- 背景: 发布前总体审查发现，0.5.1 把具名项目配置作为版本化发布内容，并把跨项目共享 `state.json`、自动改写与 Git/PR 副作用、三个版本化 eval runner 和过期 spec 一并纳入发布面；同时总入口吞 checker 错误、adaptive 基线未参与判断。这种内部部署形态不适合直接公开。用户随后确认：私有配置仍需物理保留在 Skill 内，才能由运行时稳定读取；需要隔离的是 Git/发布面，而不是读取位置。
- 决策:
  1. `doc-curator` 0.6.0 定位为公开发布候选，不在本任务内迁入 `skills/`、登记 Marketplace、推送或发布。
  2. `config/` 同时承载公开配置和本地配置：Git 发布树只跟踪通用 `default.yaml` 与去具体化 `doc-curator.example.yaml`；具名项目配置以 `config/*.local.yaml` 物理保留在 Skill 内，通过 `.gitignore` 排除，不参与 Git 子目录发布。
  3. 默认运行路径只读；删除 `maintenance-pr.sh`。公开入口唯一写入动作是用户显式调用 `--init-baseline`，且只原子写入被体检项目 `.doc-curator/state.json`。
  4. 状态同时绑定 `REPO_ROOT` 路径哈希与当前 config 哈希；不再跟踪 Skill 根 state，不维护未实现的 scan history/last_scan 承诺。
  5. 总入口负责验证 checker 的退出码、非空 JSONL、schema 和身份；所有调用、配置与执行异常失败闭合。项目约定型硬规则和 Git 语义检查改为显式 opt-in，降低公共默认值误报。
  6. 用 `scripts/test-doc-curator.sh` 取代版本化 `evals/`。测试只在临时 fixture 执行，断言 0/1/2/64 退出路径、基线隔离、非法输入和 checker crash/空输出/非法输出。
  7. 明确依赖 Bash 4+。不为兼容 macOS Bash 3.2 牺牲数组与严格模式；在首次执行位置提供 Homebrew 安装提示。
- 影响:
  - 首次使用 macOS 的调用方需安装并显式使用 Homebrew Bash。
  - 私有项目配置可直接通过 `--config <doc-curator-root>/config/<project>.local.yaml` 加载；复制或发布 Skill 时不会随 Git 自动分发，需在目标机器单独配置。
  - 从 0.5.x 升级后应在每个项目重新运行 `--init-baseline`；旧 Skill 根 state 不再读取。
  - 自动 trim、分支、commit、push 和 PR 能力不属于首发候选；如未来恢复，应作为独立写入工作流重新设计和验收。
  - 旧版本化 eval 和 spec 只保留在 Git 历史；公开包不携带开发演进残留。
- 验证:
  - `scripts/test-doc-curator.sh`：39 PASS / 0 FAIL，包含 CLI 作用域、context-sync 与 decision-sync 代表性正反分支。
  - `bash -n` 通过；ShellCheck warning 级 0；`skill-lint` Harness 静态审计 0 finding。
  - 三轮 evaluator-signed instruction-stability 门禁未执行，标记 `NOT_VERIFIED`，不得据单轮回归宣称长期稳定。
- 来源: 2026-07-30 用户要求先按最新项目协议更新 TASKS，再继续公开发布整改。

---

## D-2026-07-25-01 v0.5.1 config 驱动化补强（修复 3 项 Known Limitations + BSD awk 兼容）

- 日期: 2026-07-25
- 背景: CHANGELOG 0.3.1 / 0.4.0 段都声明了「留待后续版本」的 3 项已知限制（`tasks-active-count` pattern 硬编码 / `markdown-link-broken` severity 未细分 / `active-zone-residue` 标题模式不兼容），但 TASKS.md 漏登，存在登记漂移。用户审查时发现并提出「记录后推进修改」。三项本质同类（规则模式硬编码 → config 驱动化），合并为一个 patch 版本一次推完，避免割裂成三个独立版本导致三件套同步开销 ×3。
- 决策:
  1. **版本号 v0.5.1（patch）**：三项均为修复已知限制（硬编码 → config 可配），不新增检测维度、不破坏现有接口，属补强。config schema 扩展为新增可选字段 + 向后兼容默认值，不构成 breaking change，故 patch 而非 minor。
  2. **`tasks-active-count` 默认 pattern 扩展**：从 `^### ISS-[0-9]+` 扩展为 `^### (ISS-[0-9]+|Task[# ]+T?[0-9A-Za-z-]+)`。或关系第一支保留 ISS-N 兼容，第二支覆盖 Task#N / Task TN / Task TNN。adaptive 不阻断，对正常项目影响可忽略。
  3. **`markdown-link-broken` severity 实现**：新增 `severity_for_target()` 按 path glob（bash `case`）首匹配决定 severity，未匹配走 `default_severity`。选 bash case glob 而非正则：config 作者更熟悉 glob 语法，且 case 是 POSIX shell 内建无额外依赖。默认 `default_severity: hard` 保证 v0.4.0 行为向后兼容。
  4. **`active-zone-residue` pattern 语义调整 + BSD awk 兼容修复**：v0.4.0 原代码用 `match(title, pat, arr)`（gawk 第三参捕获组扩展），在 BSD awk（macOS 默认 awk version 20200816）上语法错误崩溃。本版改用 POSIX `match(title, pat)` + `substr(title, RSTART, RLENGTH)`，pattern 语义相应从「含 `^### ` 前缀 + 捕获组」调整为「描述编号形态、无前缀、无捕获组要求」。此前未暴露是因为旧版只跑 Task#N 输入且测试环境是 gawk；ISS-N 输入或 BSD awk 环境会触发崩溃——这是 v0.4.0 的潜在 bug，本版顺带修复。
  5. **合并修复而非分版**：三项 + 一项潜在崩溃修复同属「config 化 + 兼容性」主题，合并为一个版本便于 review 和回退（若某项出问题可单独 revert 对应脚本 + config 段，config 未配置时全部回退到当前行为，active_count_pattern 例外其默认值已扩展）。
  6. **evals 沿用「按版本分文件」模式**：新建 `run-evals-v051.sh`（11 例），与现有 `run-evals.sh`（context-sync 26 例）/ `run-evals-v03.sh`（decision-sync 6 例）解耦，向后兼容现有回归。
- 影响:
  - **行为变化（需注意）**：默认 `active_count_pattern` 扩展后，使用 Task#N 编号的项目的 `tasks-active-count` 数值会从盲报 0 修正为实际数量，可能触发 state.json baselines 失配。缓解：adaptive 不阻断；seed_value=3 × 1.5 = 4.5 阈值，正常项目影响小；项目可显式配置更精确的 pattern。
  - **BSD awk 兼容**：v0.4.0 在 macOS 默认 awk 上的潜在崩溃修复，active-zone-residue 现在在 BSD/gawk 都正常工作。
  - **config schema 扩展**：新增 3 个可选 config 段（`markdown_link_broken` / `active_zone_residue`）+ 1 个 rule 内字段（`active_count_pattern`），全部向后兼容（未配置走默认值）。
  - **登记漂移闭合**：TASKS.md 补登 3 项漏登任务并勾选完成，与 CHANGELOG 0.3.1 / 0.4.0 的 Known Limitations 声明对齐。
- 验证:
  - `evals/run-evals-v051.sh`：11 PASS / 0 FAIL（覆盖三类新能力 + 向后兼容 + 自定义配置）。
  - `evals/run-evals.sh`（context-sync 26 例）+ `evals/run-evals-v03.sh`（decision-sync 6 例）回归不破坏（Phase 5 验证）。
- 来源: 用户审查 doc-curator 待办时发现 CHANGELOG 声明与 TASKS 漂移，提出「记录后推进修改」。

---

## D-2026-07-12-02 working-tree 与历史证据分轨；Skill 根以最近 SKILL.md 为准

- 日期: 2026-07-12
- 背景: legal-ai-skill-book T154 收口时，doc-curator 产生两类不可信信号：① 18 个文件尚在工作树、尚无新 commit，`--range origin/main..HEAD` 却返回 `context-sync-no-changes`；② private-skills monorepo 的改动位于 `writing-reviewer/**`，旧 checker 只识别 `.claude/skills/<name>/**`，既漏掉内部三件套，又把多文件改动交给通用 fallback，hard 误报缺仓库顶层 `docs/DECISIONS.md`。人工核对虽确认 T154 文档齐全，但 harness 本身是 fail-open。
- 决策:
  1. 新增显式 `--working-tree` source，取 staged diff、unstaged diff、`git ls-files --others --exclude-standard` 的去重并集；文件数与改动行统计纳入未跟踪文件。Git 或文件读取失败 emit hard，不把执行错误降格为“无改动”。
  2. 工作树与历史范围保持两条证据轨：`--working-tree` 不得与 `--range` / `--since` 合用，混用 emit `context-sync-invalid-scope` hard；显式历史 range 不读取工作树，避免同一命令结果随未提交状态漂移。
  3. Skill 根不再由固定前缀推断，而是从改动路径向上寻找最近的 `SKILL.md`，从而兼容项目内 `.claude/skills/<name>/`、private-skills 仓根 `<name>/` 和单 Skill 仓根三种物理布局。
  4. config 中 `expect_skill_internal: true` 继续作为显式布局声明。声明命中却找不到 `SKILL.md` 时 adaptive fail 并列路径；没有 `SKILL.md`、也未被 config 声明的普通目录不视为 Skill。
  5. 自动发现的 monorepo 仓根 Skill 由自身 CHANGELOG / DECISIONS / TASKS 三件套闭环，不套通用 `**` 的仓库级 systemic DEC 规则；显式 `.claude/skills/**` mapping 的既有 expectations 保持不变。
  6. PR #96 review correction：历史范围的 revision、`git diff --name-only/--shortstat`，以及 working-tree `--numstat` / untracked 文件读取均显式验错；任何失败 emit `context-sync-scan-error` hard。
  7. 体系性与 local-reversible 阈值统一比较 `churn = insertions + deletions`。现有 config 键名 `max_insertions/min_insertions` 暂不改名以保持兼容，但纯删除不再被计为 0。
  8. 历史 Skill 根绑定证据范围：`--range A..B` 只查 `B` tree，`--since A` 只查 `HEAD` tree；只有 `--working-tree` 查当前文件系统 + `HEAD`。当前 HEAD 后续删除不能污染早期 A..B 结果。
- 影响:
  - pre-commit 调用方必须改用 `--working-tree`；合并后或指定 commit 审计继续用 `--range` / `--since`。模式名称进入日志与 evidence，来源可辨识。
  - Skill 目录移动不再要求改 shell 中的路径拼接；只需在被检范围的右端 tree 保留 `SKILL.md` marker，或由项目 config 显式声明 change type。
  - 仍沿用逐行路径处理，极端含换行符的 Git 文件名不在支持范围；常规空格与中文路径继续由 `core.quotepath=false` 覆盖。
- 验证: 首轮 TDD 由 17 例中 8 例 RED 转为 17/17 GREEN；PR #96 review correction 新增 9 例，其中 8 RED、1 个 `--since` 保护例先天 GREEN，修复后 26/26 PASS；decision-sync 既有 6/6 PASS。forward test 包含临时 book pre-commit（未跟踪 manuscript 可见）与真实 private-skills `bb4eee47^..bb4eee47`（writing-reviewer 四件套识别为 ok、无顶层 DEC hard）。
- 来源: legal-ai-skill-book Task T157 / DEC-128。

---

## D-2026-07-12-01 markdown-link-broken + active-zone-residue 双盲区检测规则（legal-ai-skill-book 沉淀）

- 日期: 2026-07-12
- 背景: doc-curator v0.3.1 已知盲区（DEC-121 / DEC-123 两侧）。legal-ai-skill-book 项目 2026-07-10 文档治理深度轮暴露：① 跨文档 markdown 链接断链（指向已删除的 `docs/audit-ssot-matrix.md` 由 DEC-071 删除，6 处引用未同步，DEC-123）；② TASKS.md 活跃区残留已归档条目（T146/T147 完整 80 行未清，DEC-121）。这两个盲区均为现有 `--only decision-sync`（`dec-ref-sync-removed-still-referenced`）未覆盖——前者只检 DEC 编号物理删除，不检 markdown 文件链接断链；后者只检「归档指向 DECISIONS」，不检「活跃区是否残留已归档条目」。
- 决策: 新增两个独立 check (`markdown-link-broken` hard / `active-zone-residue` adaptive)，与现有五类 check 并列；`scan.sh` 注册 `--only` + `all` 默认跑；`config/legal-ai-skill-book.yaml` 按 hard/adaptive 段登记；`SKILL.md` §2 健康检查项表新增两行。
- 影响:
  - **初判（自主标注·非最终结论）**：本仓（private-skills）主目录 first-scan 应 0 broken / 0 active-residue（v0.4.0 first-run），scan.sh `--only markdown-link-broken` 与 `--only active-zone-residue` 应 all-ok。
  - legal-ai-skill-book 仓 v0.2.x 旧 config 扫应命中 hard findings，PM 据此补 DEC-123 / DEC-121 已记录的 stale 链接与活跃残留。
  - `markdown-link-broken` v0.4.0 实现是 hard-only 单档（不对"已删除核心 vs 可选引用"细分）。TASKS.md line 24 原计划区分两类严重度（DEC-123），v0.4.0 实现主动简化，分类逻辑留 config-driven path-severity-mapping（v0.4.1+）。
- 验证:
  - 私有仓 private-skills 主目录 first-scan 0 broken / 0 active-residue。
  - legal-ai-skill-book 仓受调用方触发后命中 known 6 处 stale 链接 + N 处活跃↔归档同号项，PM 据此补同步。
- 跨仓 PR 路径：memory [feedback_maoscripts_main_divergence]——本仓 local main 与 origin main 历史分叉（code2patent.skill remote），`gh pr create` 拒开。走本地 FF merge `feat/doc-curator-t150-t252-blind-spots` → local main（symlink 立即生效，本书仓 doc-curator 升级），push feature branch 到 origin，PM 用 GitHub UI 手动合。

---

## D-2026-07-05-01 归档指针检测鲁棒化（去中文字面串依赖）

- 日期: 2026-07-05
- 背景: `tasks-archived-iss-pointer` 硬规则（`check-tasks.sh` 规则 3）原同时 grep `DECISIONS_BASE` + 中文字面串「归档任务」。legal-ai-skill-book TASKS 四区重组（PR#194）把引语"以下归档任务"改"以下任务"，字面串失配触发 hard 误报，需 PR#195 临时补回字面串。暴露 pattern 脆弱：硬编码中文字面串，文档措辞正常演进即回归。
- 决策: 规则 3 改检测归档段标题（`grep -cE '^##[^#].*归档'`，H2 标题含「归档」字样）+ DECISIONS 引用。覆盖「归档区」「已归档索引」「归档任务」等各种归档段命名，不依赖正文措辞。
- 影响: 文档措辞演进不再触发 hard 误报；归档段须保留 H2 标题含「归档」字样（语义要求合理）。规则 2 `tasks-active-count` 的同类前缀脆弱（`^### ISS-` 不匹配 `### Task#N`）未改——adaptive 不阻塞，且改 pattern 会扰动 state.json 基线，留 config 驱动 active-count-pattern 后续版本处理。

## D-2026-06-03-01 作为 subagent 而非 hook 实现

- 日期: 2026-06-03
- 背景: FaroPDF 项目需要持续监控 `docs/TASKS.md`、`docs/DECISIONS.md` 等项目级文档的膨胀与归档一致性。可选实现方式包括 git hooks、CI 检查、subagent 主动调起。
- 决策: 以 subagent 形式实现，无 hooks 依赖；由 Agent 在 `gh pr create` / `gh pr merge` 之后、ISS 任务汇报前主动调起 `scan.sh`。
- 影响: 不污染项目 `.git/hooks/`，不依赖 CI 配额；调用时机由 Agent 协议控制，可被 `git-workflow` 编排。代价是依赖 Agent 自觉执行协议，所以 SKILL.md 显式写入「Skill 强制调用」约束。

## D-2026-06-03-02 三档严重度（hard / adaptive / soft）+ 自适应基线

- 日期: 2026-06-03
- 背景: 项目文档大小天然会随版本增长，固定阈值（如"DECISIONS 不得超过 200 行"）既误伤成长期项目，又无法发现真正异常膨胀。
- 决策: 体检结果分三档——hard（必须修，如 TASKS 进度日志超 5 条、DEC 编号跳号）、adaptive（基于 `state.json` 基线 × 1.5 倍）、soft（仅提示不阻断，如 CHANGELOG 最近 release entry）。基线由 `first-baseline.sh` 在首跑时建立。
- 影响: 阈值随项目演进自适应，避免硬编码误判；hard 才触发 maintenance PR，adaptive 建议提，soft 仅记录。`config/faropdf.yaml` 可覆盖规则集。

## D-2026-06-03-03 Maintenance PR 只做机械 trim，不重写文档内容

- 日期: 2026-06-03
- 背景: 自动化 PR 若改写文档语义内容（重组段落、补充论述），会和人工 worker PR 频繁冲突，且难以 review。
- 决策: `maintenance-pr.sh` 只执行 trim 进度日志这类机械动作，并在工作区不干净时拒绝执行；不修改 `src/`、不改写文档语义、不写 CHANGELOG（CHANGELOG 归 `release-workflow`）。
- 影响: 自动 PR 边界清晰、可 review；不与内容性 worker PR 抢同一文件。回退也只需删 skill 目录 + 移除 AGENTS.md 调用行。

## D-2026-06-03-04 与 worker PR 协调的窗口期约束

- 日期: 2026-06-03（0.1.1 补强）
- 背景: FaroPDF v0.1 Wave 1 合并 PR #18 / #19 时复盘发现：doc-curator 在 PM 准备合并 worker PR 期间自动跑体检并提 maintenance PR，抢跑 main，造成冲突。
- 决策: 写入两条协调约束——① PM 合并 worker PR 期间不并发跑 doc-curator；多个 worker PR 串行合并时每个合并后独立跑；② maintenance PR 不重复改 worker PR 已写的 CHANGELOG / DEC / TASKS 段。
- 影响: SKILL.md §1.4 明确窗口期，避免自动 subagent 与人工 PR 在 main 上互相覆盖。如必须改 worker PR 已写错的 DEC 编号，让 PM 走 `git-workflow` 的冲突决策表。

---

## D-2026-06-30-01 v0.2.0 泛化：从 FaroPDF 专用到通用 + 上下文同步检测

- 日期: 2026-06-30
- 背景: v0.1.0 是 FaroPDF 专用（config 写死路径，check 不读 config，`check-files` 写死 DESIGN/ARCHITECTURE），且完全缺失「上下文同步」维度。legal-ai-skill-book 游初定稿收口链（PR #145-159）多次漏同步（#145 全漏、#151-154 figures 漏 FIGURES-OUTLINE、#157 事后补 CHANGELOG 详细段），靠用户肉眼发现（本项目 DEC-078）。
- 决策:
  1. config 彻底泛化——所有 check 从 config 读文件清单与阈值（不只是加路径解析），config 真正驱动检测，使 skill 可移植到任意项目。
  2. 新增 `check-context-sync.sh`，按改动类型动态选查（manuscript/figures/skill/体系性 → 对应上下文文档），不机械全查（用户明确要求省上下文）。
  3. DEC-052 局部可逆二分：小改动（≤1 文件 ≤50 行，非 figures/skill）不强制同步，emit ok，避免误报。
  4. `context-sync-decision` 对体系性改动判 hard（≥3 文件或 ≥200 插入行）。
- 影响: skill 可服务任意项目（config 驱动）；上下文同步检测填补"漏同步"盲区。supersede v0.1.0 FaroPDF 专用定位；`faropdf.yaml` 保留作向后兼容兜底。

## D-2026-06-30-02 纯 awk 解析 yaml（yq 不可用）

- 日期: 2026-06-30
- 背景: config 泛化需读 yaml，但运行环境无 yq（仅 awk）。嵌套 yaml 用纯 bash 难可靠解析。
- 决策: 写独立 `scripts/lib/yaml-flatten.awk`（缩进栈 flat 化为 `path=value`），`common.sh` 提供窄查询接口（`cfg_file_paths`/`cfg_file_by_role`/`cfg_rule_get`/`cfg_scalar`/`cfg_change_type_*`）。config 字段刻意保持标量（bool/enum，数组用独立字段）以适配 awk 解析子集。
- 影响: 无外部依赖（规避 sed 解析 yaml 的脆弱性），可分发到任意 macOS/Linux。代价：config 不支持 flow 数组 `[a,b]`，需用独立字段（template.yaml 注释已说明）。

## D-2026-06-30-03 REPO_ROOT 用运行 cwd 而非 SKILL_ROOT 上溯

- 日期: 2026-06-30
- 背景: symlink 部署下（本项目 `.claude/skills/doc-curator` → maoscripts 源），原 `SKILL_ROOT/../../..` 算 REPO_ROOT 依赖"从项目侧 logical 路径调用"。虽 `pwd`（logical）保留 symlink 使其在项目侧调用时正确，但语义脆弱。
- 决策: `REPO_ROOT = --repo / $DOC_CURATOR_REPO` 优先，否则运行 cwd（`$PWD`）。`SKILL_ROOT`（找 skill 自身 config/scripts）仍用 `BASH_SOURCE` 上溯。
- 影响: 被体检项目 = 运行 scan.sh 的目录，语义明确；支持 `--repo` 显式指定。

---

## D-2026-07-01-01 subagent 调用协议下沉 skill 内部（自包含可移植）

- 日期: 2026-07-01
- 背景: v0.2.0 初版 SKILL.md 只描述 scan.sh 用法，没规定「如何调用 skill」（PM 主会话跑 vs spawn subagent）。本项目 DEC-081 演进：PR #158 双轨 subagent（自带映射表，与 skill 重复逻辑）→ v0.2.0 去 subagent 改纯 skill 主会话跑（占 PM 上下文）→ 中间方案项目级 `.claude/agents/` 薄壳 subagent（不可移植）→ 用户反馈「subagent 调用协议应写 skill 内部，不建项目级配置，保证可移植」。
- 决策:
  1. SKILL.md 加 §0「调用协议」：skill 被调用时，调用方读 §0 后用 Task tool spawn inline subagent（`general-purpose` + §0.1 prompt 模板）跑 `scan.sh`，隔离上下文不占 PM 主会话。
  2. **不建项目级 `.claude/agents/` 配置**——协议在 skill 内部，任何项目部署（symlink/复制）即默认 subagent 隔离上下文，换项目零配置、行为一致。
- 影响: skill 自包含（SKILL.md = 调用协议 + scan.sh 逻辑），可移植性最大化。supersede PR #158 双轨 subagent、v0.2.0 初版纯主会话跑、项目级薄壳三套中间方案。

---

## D-2026-07-01-02 context-sync-skill-internal 升级为三件套检测

- 日期: 2026-07-01
- 背景: v0.2.0 的 emit_skill 只查 skill 内部 CHANGELOG.md 是否在改动集。但 feedback_legal_skill_internal_docs 要求"改 skill 必须更新 TASKS+DECISIONS+CHANGELOG 三件套"——只查 CHANGELOG 漏了 DECISIONS/TASKS。用户反馈"项目中 skill 更新沉淀也要遵循相同规则，skill 内部多上下文需同步"。
- 决策: emit_skill 遍历 skill 内部三件套（CHANGELOG.md / DECISIONS.md / TASKS.md），任一未在改动集 → adaptive（建议补）。对齐 feedback_legal_skill_internal_docs 全口径。
- 影响: skill 更新同步检测从"只查 CHANGELOG"扩展到"三件套全查"。代价：小 skill 改（typo）可能只改 CHANGELOG，三件套检测会报 DECISIONS/TASKS adaptive——属建议不阻断，PM 据此判断（小改可忽略，大改必补）。evals 5 用例回归 PASS。

---

## D-2026-07-03-01 GENERALIZATION-SPEC.md 删除原则（info 已沉淀到 DECISIONS+CHANGELOG）

- 日期: 2026-07-03
- 背景: `GENERALIZATION-SPEC.md`（v0.2.0 泛化设计 spec，含 A config 泛化 + B check-context-sync + C 去 FaroPDF 三目标）自 2026-06-30 v0.2.0 实施后已结束使命，但其全文仍作为独立文档存在；与 DECISIONS D-2026-06-30-01 + CHANGELOG 0.2.0 段形成同一信息双份来源，存在漂移风险。用户反馈"如果那个文档的都已经落地了，那这个文档可以删除了"。
- 决策:
  1. **删除原则**：spec / 设计文档一旦落地，其设计意图必须同步沉淀到 DECISIONS（决策原因）+ CHANGELOG（变更内容），原文保留无信息增益，只会引入「双份真相漂移」风险——与 doc-curator 自身的「文档健康单点真相」原则矛盾。落地后应主动删除源文档。
  2. **删除前清单**：删除 spec 文档前必须确认 (a) DECISIONS 已含对应 D-编号段，(b) CHANGELOG 已含对应版本段，(c) skill 内部其它文档（SKILL.md / TASKS.md / evals/run-evals.sh 注释等）已无指向已删文档的断链。
  3. **本案例沉淀**：
     - A+B+C 目标内容 → DECISIONS D-2026-06-30-01 + CHANGELOG 0.2.0 段
     - 五用例 fixture 测试清单 → DECISIONS D-2026-06-30-01 + `evals/run-evals.sh` fixture 自身（脚本即真相）；不再以「§6」序号引用 spec 文档
     - SKILL.md「参考」段 → 加注脚指向本 D-编号 + CHANGELOG 段
- 影响:
  - 正面：消除 spec 与 DECISIONS/CHANGELOG 之间的双份真相漂移风险。
  - 代价：未来若需要查阅「v0.2.0 当初的设计动机」，依赖 DECISIONS D-2026-06-30-01（结构化决策记录）+ CHANGELOG 0.2.0（结构化变更记录）。如真需原始 spec 长文，可从 git log 找回历史版本。
  - supersede：「保留原始 spec 文档作历史参考」方案——放弃，理由是「真相单一原则」优先于「历史可读性」。
- supersede 候选：未来若需要按 feedback 类的「历史 spec 文档目录化」模式（如 `.drafts/` 或 `archive/specs/`）整体归档，可独立再设计。本案例未走存档路线。

---

## D-2026-07-03-02 v0.3.0 决策变更同步检测（decision-sync）

- 日期: 2026-07-03
- 背景: 用户反馈（2026-07-01，本项目 DEC-081）"运营中决策后续改变但文档未同步"。doc-curator v0.2.0 的 context-sync 检测「改动→文档同步」，但缺少「DECISION supersede/修改 → 引用文档同步」的检测维度。当某条 DECISION 被新决策 supersede 后，TASKS.md、spec 文档等引用方可能仍指向旧 DEC 编号或未反映新决策，造成「决策已变，引用未动」的信息漂移。
- 决策:
  1. 新增独立检测维度 `check-dec-ref-sync.sh`，作为 `scan.sh` 的新可选检测（`--only decision-sync`），与现有 context-sync 互补（改动→文档 vs 决策→引用）。
  2. 检测原理：awk 解析 DECISIONS.md 识别 `supersede D-XXX` 标记 → grep 项目内对旧 DEC 的引用 → git log 比对引用方是否在决策变更后更新。
  3. 严重度：引用存在且完全未同步 → hard（退出码 1），引用存在但有 commit 但引用行未变 → adaptive（退出码 2），无引用或已同步 → ok。
  4. 搜索范围：`$REPO_ROOT` 下所有 `.md` 和 `.yaml`，排除 `.git/`、DECISIONS.md 自身、第三方目录。
  5. 无外部依赖（纯 awk + grep + git log），复用 v0.2.0 的轻量解析策略。
  6. 回退策略：性能退化/误报率高/兼容性破坏时，config 级关闭（`decision_sync.enabled: false`）或代码级 revert。
- 影响:
  - 正面：填补"决策变更→引用同步"盲区，与 context-sync 形成完整的文档同步检测矩阵。
  - 代价：增加一个检测脚本和 config 段；大型仓库 grep 可能耗时（通过 config `enabled` 开关和空扫描快速退出缓解）。
  - 不改变现有 context-sync/文件体检行为。
- supersede 候选：无（本决策为新增维度，不取代任何已有决策）。

### Implementation 完成备注（2026-07-03，v0.3.0 落地）

spec（Wave 1 W1）→ IMPLEMENTATION（Wave 2A/2B）双阶段完整落地：

- **`scripts/lib/check-dec-ref-sync.sh`**（237 行，94237425）：三步管道 awk + grep + git blame，复用 common.sh 的 `emit_result` / `cfg_scalar` / `cfg_file_by_role` / `REPO_ROOT`。severity 三档（ok / adaptive / hard），rule 前缀 `dec-ref-sync-*`。
- **scan.sh 注册 `--only decision-sync`**（5a7b7456）：与 tasks/decisions/files/context-sync 并列；默认 all 也跑。
- **4 个 config yaml 加 `dec_ref_sync` 段**（94237425）：template/default/faropdf/legal-ai-skill-book 各加 enabled + scan_extensions + exclude_paths。
- **`evals/run-evals-v03.sh`**（208 行 + a0726930 fixture fix）：6 用例 fixture（全同步 / 引用行未改 / 无 supersede 标记 / 多层 supersede 链 / 无引用 / 引用早于 supersede）。**6 PASS / 0 FAIL**，与 v0.2.0 五用例（仍 5/5 PASS）并列。
- **IMPLEMENTATION 过程修了 2 个 bug**：(1) `rel_path` 解析 bug（grep -rn 输出 strip :line）；(2) 用例 2 fixture 行带旧 ID 字串误匹配。

实测：v0.3.0 与 v0.2.0 双套 fixture 全 PASS，scan.sh 端到端 dispatch 正常。决策变更同步检测正式上线，进入 doc-curator 默认体检矩阵。

---

## D-2026-07-02-01 skill-lint v0.2.3 修复（LICENSE / frontmatter / maintenance-pr.sh）

- 日期: 2026-07-02
- 背景: skill-lint 审查（2026-07-02，本项目 `skills/.claude/skills/skill-lint`）发现 doc-curator 1 项严重 + 3 项警告 + 多项信息。核心问题：
  1. **严重**：LICENSE.txt 仍署名「FaroPDF Project Contributors」，与 CHANGELOG 0.2.0「supersede FaroPDF」段不一致——v0.2.0 supersede 时漏改 LICENSE。
  2. **警告-1**：frontmatter `homepage: https://github.com/cat-xierluo/maoscripts` + `author: maoscripts` 与 `skills/` 下其它公开 Skill 不一致（统一口径 `homepage: https://github.com/cat-xierluo/legal-skills` / `author: 杨卫薪律师（微信ywxlaw）`）。
  3. **警告-2**：`scripts/maintenance-pr.sh` 自动 `git push -u origin` + `gh pr create`，虽然边界清楚但缺 dry-run 护栏（参考 `feedback_prefer_existing_toolchain`、general security「允许但需说明」原则）。
  4. **警告-3**：`state.json` baselines 是项目特定快照，跨项目 symlink 部署时未文档化分发约束——「Skill 可分发」原则下属 recall 漏洞。
  5. 其它：`.DS_Store` 入 release、`description` 偏长含命令路径。
- 决策:
  1. LICENSE copyright 改 `Copyright (c) 2026 doc-curator contributors`，与 frontmatter `author` 口径对齐。
  2. frontmatter `homepage` 改 `https://github.com/cat-xierluo/legal-skills`、`author` 改 `杨卫薪律师（微信ywxlaw）`、`version` 升 `0.2.3`，description 精炼控制在前 250 字符建议区间（移除内嵌命令路径与实现细节子句）。
  3. `maintenance-pr.sh` 加 `--dry-run` 与 `--help`：仅跑 scan + 检查 git 状态 + 打印将要 commit/push/PR 预告（分支名 / 命中 rule_id / 推送目标端 / gh 命令），不做任何文件修改 / commit / push / PR。SKILL.md §4 加「真跑前先跑 `--dry-run`」条款。
  4. SKILL.md §6 末尾加「跨项目分发提醒」段：明确 `state.json.baselines` 是项目特定基线，跨项目复制或 symlink 部署时必须先删 baselines 再跑 `first-baseline.sh`，否则新项目沿用旧基线判错。
  5. `rm .DS_Store`。
- 影响:
  - LICENSE / frontmatter / homepage / author 一致性回填完成（之前「supersede FaroPDF」时漏改 LICENSE 与 homepage，本次闭环）。
  - `maintenance-pr.sh --dry-run` 补齐「自动外联」能力的安全护栏，与 `feedback_prefer_existing_toolchain`、脚本审计原则一致。代价：dry-run 退出码 0；真跑时仍走原流程。
  - 「跨项目分发」段文档化 state.json 真实语义（按 memory `feedback_skill_no_real_project_info` 延伸：从「文档无真实项目信息」拓展到「运行时缓存无项目耦合」），避免下游误沿用 FaroPDF baselines 误报。
  - description 精炼让模型更容易在描述触达时识别「何时使用」。
- 不在范围:
  - skill 文档里 PR 编号 / DEC 编号 / ISS 编号的脱敏（skill 自带 DECISIONS.md 是 self-contained accountability 必需；SPEC 长篇叙述里的锚点是否泛化属次级风险，留待 v0.3.0 再评估——`feedback_skill_no_real_project_info` 主类针对「客户/案件/申请号」）。
  - 其它信息提示（evals harness 描述、CHANGELOG 接管协议声明加强）属可选。

---

## 工作日志

### 2026-07-02

- v0.2.3：skill-lint 审查闭环。
  - LICENSE.txt `Copyright (c) 2026 doc-curator contributors`（v0.2.0 supersede FaroPDF 时漏改）。
  - frontmatter `homepage` → `https://github.com/cat-xierluo/legal-skills`、`author` → `杨卫薪律师（微信ywxlaw）`、`version` → 0.2.3、description 精炼。
  - `scripts/maintenance-pr.sh` 新增 `--dry-run` / `--help`：仅打印预告，不真做 commit/push/PR。
  - SKILL.md §4 加「真跑前先跑 `--dry-run`」条款；§6 末尾加「跨项目分发提醒」段（state.json baselines 跨项目需重建）。
  - 删 `.DS_Store`。
  - 内部三件套同步（CHANGELOG 0.2.3 + DECISIONS D-2026-07-02-01 + TASKS 0.2.3 已完成），对齐 feedback_legal_skill_internal_docs。

### 2026-07-01

- v0.2.2：context-sync-skill-internal 升级三件套检测（CHANGELOG+DECISIONS+TASKS）。来源本项目用户 feedback（skill 更新也要遵循同步规则）。evals 回归 PASS。

- v0.2.1：SKILL.md 加 §0 调用协议（subagent 协议下沉 skill 内部）。来源本项目 DEC-081 + 用户 feedback。**补记 CHANGELOG v0.2.1 + 本 DEC——之前改 SKILL.md §0 后漏同步 skill 内部文档（违反 feedback_legal_skill_internal_docs），用户提醒后补。**

### 2026-06-30

- v0.2.0 泛化落地：config 彻底驱动 + `check-context-sync.sh` + SKILL.md 去 FaroPDF。新增 `yaml-flatten.awk`（纯 awk）、3 个新 config、`check-context-sync.sh`。现有 3 check 改读 config。`scan.sh` 加 `--config`/`--since`/`--range`/`--only`/`--repo`。经 legal-ai-skill-book 真实样本（HEAD~6..HEAD）验证：manuscript/figures/skill 动态选查正确，DEC-052 局部可逆二分正确。

### 2026-06-23

- 补建 DECISIONS.md（和 TASKS.md）。此前 doc-curator 仅有 CHANGELOG.md 记录 0.1.0 / 0.1.1 两版变更，缺少决策上下文。本次从 CHANGELOG 回溯提炼 4 条核心技术决策：subagent 形式选型、三档严重度+自适应基线、maintenance PR 边界、worker PR 窗口期协调。

### 2026-07-10

- 沉淀 `markdown-link-broken` 与 `active-zone-residue` 检测规则（v0.4.0 候选）：来源 legal-ai-skill-book 项目 2026-07-10 文档一致性治理深度轮（PR #300-307 共 8 PR）。发现 doc-curator 两大盲区——
  1. **`markdown-link-broken`**：markdown 链接 `[text](path)` 或反引号 `` `path` `` 引用指向已删除文件。法律书仓 `docs/audit-ssot-matrix.md` 已由 DEC-071 删（2026-06-24, PR #140）但 6 处跨文档引用未同步指向 DECISIONS.md DEC-063。现有 `dec-ref-sync-removed-still-referenced` 只检 DEC 编号物理删除，不检 markdown 文件路径。严重度 hard（核心文档）+ adaptive（可选引用）。
  2. **`active-zone-residue`**：活跃区残留已归档条目。法律书仓 PR #300 手工清 T146/T147（80 行）。现有 `tasks-archived-iss-pointer` 只检归档指针指向 DECISIONS，不检活跃区是否残留。严重度 adaptive（需人工判别同号合理性，不阻断 merge）。
  两个盲区互补：hard（机械）+ adaptive（人工判别）。**实施计划**：scan.sh 加 awk 规则 + config 加 context 段 + SKILL.md §2 表格更新 + CHANGELOG/TASKS/DECISIONS 三件套同步；evals 加新用例回归。**优先级**：v0.4.0 候选（与 v0.3.0 决策变更同步检测并列）。**关联**：法律书仓 DEC-123/124 / Task T150/T152 / `feedback_legal_skill_internal_docs`（skill 内部三件套同步协议）。
