# doc-curator Changelog

## [0.10.0] - Unreleased

- 新增 opt-in `context_truth.value_claims` / `exact_value`：唯一整行声明、一个非空捕获组、允许值集与精确比较，用于角色/状态镜像漂移；不把标签一致当成完成真实性或访谈语义正确性证据。
- 新能力要求 schema 3，reader 同时保留 schema 2 旧能力；防止 v0.9 reader 忽略新字段后与旧 index claims 一起假绿。
- 两类 claims 均阻断跨仓绝对路径/`..`/symlink、缺失 ID、跨类型重复 ID 及未知字段；新增专用离线正常/故障 fixtures，与既有完整回归串行集成。
- 修复大合法配置被 `grep -q` 早退与 `pipefail` 误判失败、getter 吞掉配置读取错误的已复现路径；checker 开始时一次性加载配置，读取失败交总入口形成 hard。基线初始化改为复用实际 config 合同，支持 schema 2/3，非法合同不写状态。
- 确定性测试与独立复审证据见 DOC-007；真实项目语义审计与跨项目稳定性不由该 checker 保证。

## [0.9.0] - Unreleased

> 状态：candidate。下列实现、本地确定性回归与消费项目 working-tree dogfood 已完成；消费项目全量历史 range 和无旧上下文 Agent 前向验证仍为 `NOT_VERIFIED`。

### 新增

- 恢复 SKILL §0 默认 subagent 隔离协议，闭合 v0.6.0 重写时误删、但项目 AGENTS/DEC-081 和 Skill 历史文档仍引用该协议的回归。
- 新增 `config` checker 与 `schema_version: 2`：报告配置来源，阻断旧 schema、业务字段缺 `enabled`、必需 checker 被关闭，以及自动发现候选配置内容不同造成的静默遮蔽。
- 决策检查新增 `decisions-id-unique`，并把连续编号的输入收窄为可配置的决策标题，不再由正文引用填平跳号或掩盖重复 ID。
- 新增 `context-truth` checker；v0.9.0 首版提供 `index_claims` / `max_numeric_suffix`，用于确定性比较权威条目最大编号与 README/TASKS 等镜像范围。
- 新增 `scan.sh --profile merge-gate` 紧凑门禁模式（DOC-006 / D-2026-09-02-01）：同一单次 scan 输出一行固定 schema（`doc-curator.merge-gate.v1`）摘要——精确 base/head/range、config provenance、hard/adaptive/soft/ok 计数、阻断与 adaptive rule ID、`next_action`、`exit_code`；完整 JSONL 仅在显式 `--jsonl-out <file>` 时原子写入指定文件，默认不进入调用方上下文。SKILL §0 增加对应路由例外：`pass` 或仅机械可修 hard 不要求默认启动 subagent。

### 修复与改进

- Markdown 链接检查增加 URL decode、repo-relative 词法归一化、逃出仓库阻断，以及 `source_severity_paths` / 归一化目标 `severity_paths` / `pseudo_target_patterns` 三层分流，降低编码路径和概念编号误报。
- scan 总入口严格校验 child finding 严重度与退出码：hard 必须退出 1，仅 adaptive 必须退出 2；修复子 checker 输出 hard/adaptive 却 rc0 的假绿链。
- 显式请求 disabled checker 现在返回 `checker-disabled` hard；全量扫描中的禁用项改报“未验证”soft，不再用 ok 表示未执行。
- `--report` 既然是显式交付物，渲染、临时文件或写盘失败现在产生 `report-generation-error` hard；成功时先写同目录临时文件再原子替换。
- 配置 flatten 后新增路径唯一性检查，重复 schema/scalar/list field 统一失败闭合；修复 checker 直接遍历 flatten 时受 command-substitution 子进程隔离导致的模糊 enabled 漏检。
- `context-truth` 启用但零 claims 现在 hard；mirror claim 必须唯一命中，多条新旧范围不再由最大值掩盖；不可解析后缀也不再被 `set -e` 提前截断成 child 空输出。
- Markdown 目标在词法边界后增加物理路径边界：仓内 symlink 通过，穿透到仓外的 symlink 按 `markdown-link-outside-repo` 阻断。
- merge-gate 模式失败闭合语义：缺项目配置（回落 bundled-default）时 emit `config-required` hard（NOT_VERIFIED）并路由 `provide-config`，不自动生成任何 `*.local.yaml`；range/since 无法解析精确 base/head 时 emit `merge-gate-range-unresolved` hard；`--jsonl-out` 写盘失败 emit `jsonl-output-error` hard。退出码与 legacy 完全一致；不带 `--profile` 的既有调用零变化。代表 fixture 实测 stdout 带宽 4273 → 618 bytes（14.5%，1 行）；该数字为单样本带宽实测，不是端到端 token savings 结论。

### 验证

- `scripts/test-doc-curator.sh`：95 → 167 → 226 PASS / 0 FAIL；merge-gate 新增 35 项：compact 与 legacy 的 hard/adaptive 阻断 rule IDs 与四档计数完全一致、单次 scan 各 checker 恰好调用一次（影子 wrapper 序列比对）、compact stdout bytes ≤ legacy 的 50%、受控 TMPDIR + manifest 证明扫描不在 repo/worktree 外写文件、`--jsonl-out` 落盘与 legacy stdout 逐字节一致、config-required/错误路径/range 解析/working-tree scope。
- 全部 Shell 脚本 `bash -n` 与 `git diff --check` 通过。
- `quick_validate.py` 通过；候选 Skill 自扫 rc 0（hard 0 / adaptive 0 / ok 4 / soft 8，soft 均为基线缺失或未显式启用的 `NOT_VERIFIED`）。
- 动态 dogfood 快照（不是稳定产品常量）：候选入口 `feat-doc-curator-retrospective-audit/doc-curator`，消费 worktree `retrospective-full-audit-20260828`，`HEAD=baa0459c`，`base/origin-main=69426fc5`，13 项 working-tree 变化；显式 `--working-tree` 全扫 rc 1，14 hard（13 duplicate + 1 gap）/ 181 adaptive（179 broken + 1 pseudo + 1 active-count）/ 15 ok / 8 soft。
- `NOT_VERIFIED`：消费项目完整历史 range；无旧上下文 Agent 是否稳定遵循恢复后的 §0；跨多轮 evaluator-signed instruction-stability；merge-gate 在真实 merge review 工作流中的端到端 token 节省。

## [0.8.0] - 2026-08-14

### 新增

- **per-project 配置自动发现**：`resolve_config` 新增优先级档位——Skill 内 `config/<repo-basename>.local.yaml` / `.yaml`，按被体检项目目录名自动匹配，插在项目根 config 之后、`default.yaml` 之前。私有项目配置不再必须手动 `--config` 显式加载；现有项目根 config（第 2 档）与显式 `--config`（第 1 档）行为不变，向后兼容。

### 改进

- **state.json 迁入 Skill 内部**：`STATE_FILE` 默认位置从被体检项目根 `.doc-curator/state.json` 改为 Skill 内 `state/<project_id>.state.json`（`project_id` = 项目绝对路径 SHA-256）。所有 per-project 副作用（config + state）集中收进 Skill 目录，被体检项目根目录零污染。按 `project_id` 命名彻底规避同名项目基线互相覆盖（`--init-baseline` 是无条件 `mv`，basename 命名会让同名不同路径项目互冲）。
- **`.gitignore` 同步**：Skill 仓库新增忽略 `doc-curator/state/`；`config/*.local.yaml` 既有忽略保留。被体检项目侧无需再配置 `.doc-curator/` 忽略。
- `SKILL.md` 同步更新 config 优先级、私有配置语义、state 位置、安全边界与发布包表述。

### 决策

- 反转 v0.5.x 起「state 不写入 Skill 安装目录、只写项目本地」的旧原则：用户要求所有 per-project 副作用统一收进 Skill 内集中管理。用 `project_id`（绝对路径哈希）给 state 命名消除同名碰撞，`state_project_matches` 的 project_id + config_sha256 校验天然兼容。config 仍按 basename（人维护、可读，碰撞可接受）；state 按 project_id（机器维护、强隔离）。详见 `DECISIONS.md` D-2026-08-14-01。

### 验证

- `scripts/test-doc-curator.sh`：92 → 95 PASS / 0 FAIL。基线写入断言从项目本地 `.doc-curator/` 改为 Skill 内 `state/<project_id>`；新增「同 basename 不同父目录 → 不同 project_id → state 文件互不覆盖」隔离用例。
- 全部脚本 `bash -n` 通过。
- folia 真机：`scan.sh`（不带 `--config`）自动发现 `config/folia.local.yaml`，state 写入 Skill 内 `state/<folia_project_id>.state.json`，项目根不产生 `.doc-curator/`。

## [0.7.2] - 2026-08-01

### 修复

- 修复 `render-report.sh` 用 sed 提取字段导致合法转义引号截断的问题；改为按固定 JSONL schema 分隔字段并受控解码 `\"`、`\\`、`\n`、`\r`、`\t`，不引入 jq/Python 运行依赖。
- 修复报告端到端测试用 `|| true` 丢弃基线初始化退出码的假绿路径；初始化和正式扫描现在分别断言退出码及结构化 rule，关闭 `skill-lint` `HRA-001`。
- `scan.sh --report` 的报告头只传入项目目录名和配置文件名，不再写入本机绝对路径；checker、rule、message、suggestion 与元数据统一做 Markdown 表格安全处理。
- 启用 `task_source_contract` 时跳过旧的 H3 标题总数型 `tasks-active-count`，避免累计的 `DONE` 历史被误报为活跃任务；AgentCMD 档案删除不再生效的旧计数配置，状态合同继续检查多进行中任务。
- 修正文档中“只有初始化基线会写入”的旧表述，明确默认扫描只读，`--init-baseline` 与 `--report` 是两个显式、限域写入入口。

### 验证

- `scripts/test-doc-curator.sh`：92 PASS / 0 FAIL，新增覆盖 JSON 转义不截断、控制字符归一、基线与扫描退出码、报告元数据不泄露绝对路径，以及多条 DONE 历史不触发旧活跃计数。
- 全部 Shell 脚本 `bash -n` 通过；ShellCheck warning 级 0；`git diff --check` 通过；`skill-lint` Harness 静态审计 PASS、0 finding；自体 AgentCMD 全量扫描 hard 0、adaptive 0。
- instruction-stability 仍缺少约束追踪合同和三轮 evaluator-signed evidence，保持 `NOT_VERIFIED`。

## [0.7.1] - 2026-07-31

### 新增

- **`scripts/render-report.sh`**：把 scan.sh 的 JSONL 输出渲染成人读 Markdown 报告。按 hard/adaptive/soft 三档分组（每档一张表：检查项/规则/问题/建议），含汇总计数表与结论行；ok 档只给计数不逐条列。接受 stdin 或文件参数，非法 JSONL 行跳过并 stderr 警告（不崩溃），空输入生成「无结果」报告。
- **`scan.sh --report <file.md>`**：跑完体检后把 JSONL 结果喂给 render-report.sh，报告写入指定文件。stdout 仍为 JSONL（输出契约不变），报告是附加产物；生成失败只 stderr 警告，不影响 scan 退出码。与 `--init-baseline` 合用时返回 64 并 emit 结构化 finding。

### 改进

- **`SKILL.md` 快速开始**：新增「生成 Markdown 报告」子节，说明 `--report` 参数与 `render-report.sh` 管道两种用法；并指引如需 Word 版本可交给 `md2word` skill 二次转换。

### 验证

- `scripts/test-doc-curator.sh`：55 → 81 PASS / 0 FAIL；新增覆盖 render 合法 JSONL（汇总表/分组表/计数）、空输入（无结果报告）、含非法行（跳过+stderr 警告+合法行仍渲染）、message 含 `|` 转义、scan `--report` 端到端（报告生成+stdout 仍 JSONL）、`--report` 与 `--init-baseline` 互斥。
- 全部脚本 `bash -n` 通过。

### 决策

- 选 Markdown 而非 Word/HTML：零外部依赖，纯 bash 生成，GitHub/IDE 直接渲染，符合 skill 轻量定位。不内置 Word 生成避免引入 pandoc 等重依赖；如需 Word 经 md2word 二次转换。
- 只报问题项（hard/adaptive/soft 逐条，ok 仅计数）：报告聚焦「哪里要修」，避免 ok 多时冗长。
- 不改 scan.sh 的 stdout JSONL 契约：保护机器消费链，报告写文件是附加通道。详见 DECISIONS.md D-2026-07-31-02。

## [0.7.0] - 2026-07-31

### 新增

- 新增可显式启用的 `task_source_contract`：将 `TASKS.md` 作为当前任务源，识别 `DRAFT / READY / IN_PROGRESS / BLOCKED / REVIEW / DONE / CANCELLED`，并按状态检查任务卡完整性。
- 新增公开档案 `config/agentcmd-v4.example.yaml`，可直接以其它 Skill 根目录为 `--repo`，审计其根目录 `TASKS.md` 是否符合用户级 `AGENTS.md` v4 的任务职责语义。
- 新增任务源 hard finding：缺任务源、非法合同配置、状态冲突、缺状态、缺任务卡和任务卡字段不完整；同时提供明确空队列与合同通过结果，多个进行中任务作为 adaptive 提示。

### 改进

- 保持 `config/default.yaml` 的严格任务源审计关闭，避免旧项目因未采用相同任务体系而误报；完整示例和 AgentCMD 档案均展示可配置任务编号、状态词、字段正则与按状态要求。
- `DRAFT` 可保留不完整信息；只有进入可执行、验收、完成、阻塞或取消状态后，才按相应合同实施阻断检查。
- SKILL 使用说明新增 AgentCMD v4 一条命令审计入口、状态语义、空队列写法和项目级适配边界。

### 验证

- `scripts/test-doc-curator.sh`：55 PASS / 0 FAIL；新增覆盖合规 READY、不完整 DRAFT、READY 缺字段、BLOCKED 缺原因、DONE 缺证据、显式空队列、状态冲突和缺 TASKS。
- 全部 Shell 脚本 `bash -n` 通过；ShellCheck warning 级 0；`git diff --check` 通过；`skill-lint` Harness 静态审计 0 finding；自体 Markdown 引用 0 broken。
- 真实 Skill 前向样本：`doc-curator`、`legal-visualization`、`universal-media-downloader` 均以 AgentCMD 档案通过，分别覆盖完成合同、完整 READY 及不完整 DRAFT。
- 长期 instruction-stability 门禁仍未完成三轮 evaluator-signed evidence，保持 `NOT_VERIFIED`，不据单轮回归宣称长期稳定。

## [0.6.0] - 2026-07-30

### 新增

- 将首跑基线并入统一入口：`scripts/scan.sh --init-baseline` 只写被体检项目的 `.doc-curator/state.json`。
- 新增去具体化完整示例 `config/doc-curator.example.yaml`；通用默认配置只启用低误报、只读检查。
- 新增单一确定性回归入口 `scripts/test-doc-curator.sh`，覆盖参数、配置、基线隔离、adaptive 分支、断链和 checker 故障注入。
- 结构化结果新增 `checker` 字段，stdout 固定为 JSONL，stderr 固定为运行日志。

### 改进

- adaptive 行数与活跃任务规则改为真实比较“项目基线或 seed × multiplier”，不再无条件告警；状态同时绑定项目路径哈希与配置哈希。
- 项目约定型硬规则改为显式 opt-in；`context_sync`、`dec_ref_sync`、`active_zone_residue` 默认关闭。
- Markdown 断链检查按源文件目录解析相对路径，排除 fenced/inline code 示例；反引号路径检查默认关闭、可配置开启。
- 明确 Bash 4+、Git 和系统文本工具依赖，以及 macOS Homebrew Bash 的调用要求。

### 修复

- 总入口改为失败闭合：未知/缺失参数、未知 checker、非法 YAML/regex、checker crash、空输出、非法 JSONL、身份或退出码不一致均阻断。
- YAML 子集解析遇到 Tab 或不支持语法时不再静默忽略；JSON 字符串统一转义并在汇总前逐行校验 schema。
- 修复 BSD awk 列表字段解析和 Markdown 扫描错误被 process substitution 隐藏的问题。

### 技术优化

- 删除版本化 `evals/` runner、独立 `first-baseline.sh`、写入型 `maintenance-pr.sh`、Skill 根运行态 `state.json` 和过期 `V0.3.0-SPEC.md`。
- 按当前项目结构规范将 `scripts/lib/` 提升为扁平 `scripts/`，入口、checker、解析器与回归脚本同层放置。
- 根据运行时读取要求，将 FaroPDF、legal-ai-skill-book 具名配置保留在 Skill 的 `config/` 中并改名为 `*.local.yaml`；Git 忽略这些本地配置，发布面仍只包含 `default.yaml` 与 `doc-curator.example.yaml`。
- LICENSE 版权信息对齐项目统一口径；frontmatter 版本升级至 `0.6.0`。

### 验证

- `scripts/test-doc-curator.sh`：39 PASS / 0 FAIL，包含 CLI 作用域、context-sync 与 decision-sync 代表性正反分支。
- `bash -n`：通过；ShellCheck warning 级：0；`skill-lint` Harness 静态审计：0 finding。
- 当前候选未执行三轮 evaluator-signed instruction-stability 门禁，正式稳定性结论为 `NOT_VERIFIED`。

## 0.5.1 - 2026-07-25

### Added — config 驱动化补强（修复 3 项 Known Limitations）

合并修复 CHANGELOG 0.3.1 / 0.4.0 段声明「留待后续版本」但 TASKS 此前漏登的 3 项已知盲区。三项本质同类（规则模式硬编码 → config 驱动），一次推完避免割裂。

- **`scripts/lib/check-tasks.sh` 规则 2**：`tasks-active-count` 的活跃任务标题 pattern 改读 config `adaptive_rules.tasks-active-count.active_count_pattern`。默认值扩展为 `^### (ISS-[0-9]+|Task[# ]+T?[0-9A-Za-z-]+)`，修复对 `### Task#N` 项目（如 legal-ai-skill-book）盲报 0 的 v0.3.1 已知盲区；ISS-N 项目无副作用（或关系第一支）。
- **`scripts/lib/check-markdown-link-broken.sh` severity 细分**：新增 `severity_for_target()` 函数，按 target path 匹配 config `markdown_link_broken.severity_paths`（bash case glob，首匹配生效）决定 hard/adaptive；未匹配走 `default_severity`（默认 hard，向后兼容 v0.4.0 全 hard 行为）。实现 v0.4.0 原计划的「hard（核心文档）+ adaptive（可选引用如 reviews/）」双档。
- **`scripts/lib/check-active-zone-residue.sh` config 驱动**：段标题改读 `active_zone_residue.zone_headings.{active,pending,deferred,archive}`，编号 pattern 改读 `active_zone_residue.residue_pattern`。默认 pattern 扩展兼容 ISS-N（修复 v0.4.0 盲区），自定义段标题（如英文 Active/Archive）也支持。

### Fixed — BSD awk 兼容性（v0.4.0 潜在崩溃）

- **`check-active-zone-residue.sh` awk `match()` 三参捕获组**：v0.4.0 原代码用 `match(title, pat, arr)`（gawk 扩展，第三参捕获组），在 BSD awk（macOS 默认）上语法错误崩溃。本版改用 `match(title, pat)` + `substr(title, RSTART, RLENGTH)`（POSIX 兼容），pattern 语义相应调整为「描述编号形态、无捕获组要求」。此前未暴露是因为旧版只跑 Task#N 输入且测试环境是 gawk；ISS-N 输入或 BSD awk 环境会触发崩溃。

### Changed

- **`scripts/lib/common.sh` 新增 helper**：`cfg_list_indices <prefix> [sub]` + `cfg_list_field <prefix> <idx> <field>`，通用化遍历任意 list 段（与现有 `cfg_change_type_indices` 同构）。供 `check-markdown-link-broken.sh` 遍历 `severity_paths` 用。
- **4 个 config 文件全部更新**：`template.yaml` / `default.yaml` / `faropdf.yaml` / `legal-ai-skill-book.yaml` 均加 `adaptive_rules.tasks-active-count.active_count_pattern` 字段 + `markdown_link_broken` 段 + `active_zone_residue` 段。`legal-ai-skill-book.yaml` 因项目用 Task#N 显式配置（不依赖默认值）。
- **frontmatter `version`**：0.5.0 → 0.5.1（patch，三项均为修复已知限制 + 一项潜在崩溃修复，不新增检测维度）。

### Added — evals

- **`evals/run-evals-v051.sh`**（11 用例）：沿用「按版本分文件」模式（run-evals.sh = context-sync 26 例、run-evals-v03.sh = decision-sync 6 例）。覆盖三类新能力：
  - active-count-pattern（Task#N 默认检出 / ISS-N 向后兼容 / 自定义 pattern 覆盖）
  - markdown-link-broken severity（docs → hard / reviews → adaptive / 无 config → hard 兼容 / 自定义 default）
  - active-zone-residue（ISS-N 默认检出 / 英文段标题 / 无残留 ok / 四区无碰撞 ok）
  - **11 PASS / 0 FAIL**。

### Decision / Reason

- 来源：CHANGELOG 0.3.1 / 0.4.0 段声明的 Known Limitations（TASKS 漏登，登记漂移）。
- 详细决策：DECISIONS.md D-2026-07-25-01（config 驱动化补强 + BSD awk 兼容性修复 + 默认 pattern 行为变化）。

## 0.5.0 - 2026-07-12

### Added — working-tree source 与通用 Skill 根识别（legal-ai-skill-book T157）

- **`scan.sh --working-tree`**：显式扫描 staged + unstaged + untracked 三类未提交改动，修复 `origin/main..HEAD` 在 pre-commit 阶段无 commit 时返回 `context-sync-no-changes` 的假绿。工作树模式会把未跟踪文件计入文件数与改动行；Git 或文件读取失败 emit `context-sync-scan-error` hard。
- **范围互斥 fail-closed**：`--working-tree` 与 `--range` / `--since` 同传时 emit `context-sync-invalid-scope` hard；显式历史 range 保持原语义，不隐式混入工作树。
- **Skill 根自动发现**：`context-sync-skill-internal` 从每条改动路径向上寻找最近的 `SKILL.md`，同时支持 `.claude/skills/<name>/` 与 private-skills monorepo 仓根 `<name>/`。自动识别的仓根 Skill 由内部 CHANGELOG / DECISIONS / TASKS 闭环，不再误报缺少仓库顶层 `docs/DECISIONS.md`。
- **未知布局不假绿**：config 的 change type 若声明 `expect_skill_internal: true`，但路径祖先没有 `SKILL.md`，emit adaptive 并列出无法定位的路径；无关仓根目录不会被误判为 Skill。
- **TDD 回归**：`evals/run-evals.sh` 首轮从 5 例扩至 17 例；PR #96 review correction 再增 9 例，覆盖 invalid range/since、shortstat/numstat/文件读取错误、历史/工作树纯删除 500 行、A..B 右端 tree 与 `--since` HEAD tree；26/26 PASS，原 decision-sync 6/6 PASS。

### Fixed — PR #96 review correction（2026-07-13，同版补丁、不升版本）

- **历史范围错误 fail-closed**：`git diff --name-only/--shortstat` 与 revision 解析错误不再被 `|| true` 吞成无改动；统一 emit hard `context-sync-scan-error`。working-tree 的 `--numstat` 和 untracked 文件/符号链接读取同样显式验错。
- **体系性规模改用 churn**：同时统计 insertions + deletions，阈值比较改为总改动行；单文件纯删除 500 行在历史 range 与 working-tree 均要求 DEC，不再走 local-reversible。
- **历史 Skill 根绑定 range 右端**：`A..B` 从 `B` tree 查最近 `SKILL.md`，`--since` 从 `HEAD` tree 查；当前 HEAD 的后续删除或脏工作区不再改写过去范围的判断。working-tree 保持文件系统 + HEAD 双源。

### Changed

- **frontmatter `version`**：升至 0.5.0（新增 CLI 扫描模式与 Skill 根识别能力，minor bump）。同时纠正 0.4.0 变更日志已声明升版、但 `SKILL.md` 仍停留 0.3.1 的版本漂移。
- **调用协议**：pre-commit / 未提交收口明确使用 `--working-tree`；PR/合并后的历史验收继续使用 `--range` / `--since`。

### Decision / Reason

- 来源：legal-ai-skill-book Task T157 / DEC-128。T154 收口实测暴露工作树假绿与 monorepo Skill 根误分类。
- 详细决策：DECISIONS.md D-2026-07-12-02。

## 0.4.0 - 2026-07-12

### Added — markdown-link-broken + active-zone-residue 双盲区检测（T150+T152）

- **`scripts/lib/check-markdown-link-broken.sh`**：扫描项目根下 .md 文件中的 `[text](path)` 与反引号 `\`path\`` 链接，验证路径是否存在。exclude: `.claude/skills/`、`.git/`、`node_modules/`、`vendor/`。severity: hard / ok。来源：legal-ai-skill-book 项目 2026-07-10 文档一致性治理深度轮 4（PR #306，DEC-123）。
- **`scripts/lib/check-active-zone-residue.sh`**：awk 解析 TASKS.md H2 段（活跃区 / 待核实区 / 暂缓区 / 归档区）+ 提取 `### Task#N` / `### Task TN` / `### Task TNN` H3 标题，diff 活跃区与其它三区同号 Task 条目。severity: adaptive（人工判别真残留 vs 合理交叉引用/编号撞号/多批次）。来源：legal-ai-skill-book 项目 2026-07-10 文档治理深度轮 1（PR #300，DEC-121）。
- **`scripts/scan.sh`** 注册 `--only markdown-link-broken` 和 `--only active-zone-residue`；`all` 默认也跑。
- **`config/legal-ai-skill-book.yaml`**：`hard_rules` 加 `markdown-link-broken`；`adaptive_rules` 加 `active-zone-residue`。
- **`SKILL.md` §2 健康检查项表**：加 `markdown-link-broken` (硬性) / `active-zone-residue` (自适应) 两行。
- **frontmatter `version`**：0.3.1 → 0.4.0（minor bump，新增两个独立 check + 项目配置）。

### Known Limitations（v0.4.0 简化实现）

- **`markdown-link-broken` 严重度未细分**：本项目 TASKS 候选阶段原计划区分「hard（指向已删除核心文档如 DEC-NNN/`docs/`）+ adaptive（指向可选引用如 `reviews/`/`source-material/`）」。v0.4.0 实现是全 hard 单档（统一判定文件存在性），分类逻辑留 config-driven path-severity-mapping（v0.4.1+）。**主动简化的取舍**：避免一次双档位+config-array 复杂度，方便 PM 主会话即刻落地（TASKS.md line 24 原 plan）。
- **`active-zone-residue` 标题模式**：`### Task#N` 与 `### Task TN` / `### Task TNN` 兼容性已验证；其它项目（如用 `### ISS-N` 编号）需 config 驱动 residue-extract-pattern（v0.4.1+）。

### Decision / Reason

- 来源：本项目任务池 T150（TASKS line 24）+ T152（TASKS line 25）。沉淀：legal-ai-skill-book 项目 DEC-123 / DEC-121 已知盲区。
- 详细决策：DECISIONS.md D-2026-07-12-01。
- 跨仓 PR 路径：memory [feedback_maoscripts_main_divergence]——本地 main 与远端分叉（code2patent.skill remote），`gh pr create` 拒开。本版本走本地 FF merge feature → main（symlink 立即生效，本书仓 doc-curator 升级），push feature branch 到 origin，PM 用 GitHub UI 手动合。

## 0.3.1 - 2026-07-05

### Fixed — `tasks-archived-iss-pointer` 鲁棒化（去字面串依赖）

- **`scripts/lib/check-tasks.sh` 规则 3**：原检测同时要求 `DECISIONS_BASE` + 字面串「归档任务」，文档措辞稍变（如"归档任务"→"归档区"）即失配 → hard 误报。改检测归档段标题（`^##[^#].*归档`，H2 含「归档」字样）+ DECISIONS 引用，覆盖「归档区」「已归档索引」「归档任务」等各种归档段命名。
- 来源：legal-ai-skill-book TASKS 四区重组（PR#194）时措辞从"以下归档任务"改"以下任务"，字面串失配触发 hard（PR#195 临时补字面串修复，本版彻底鲁棒化）。
- 验证：legal-ai-skill-book scan 报"归档段存在（1 处 ## 归档标题）且引用 DECISIONS.md"，hard 0。

### Known Limitations（未改，留后续）

- **规则 2 `tasks-active-count`** 仍匹配 `^### ISS-[0-9]+`，对用 `### Task#N` 组织的项目（如 legal-ai-skill-book）盲报 0。adaptive 不阻塞；彻底修复需 config 驱动 active-count-pattern，留待后续版本。

## 0.3.0 - 2026-07-03

### Added — 决策变更同步检测（decision-sync）落地

`V0.3.0-SPEC.md` spec 阶段（Wave 1 W1）→ IMPLEMENTATION 阶段（Wave 2A/2B）完整落地。

- **`scripts/lib/check-dec-ref-sync.sh`**（237 行）：三步管道（awk 解析 DECISIONS.md 识别 `supersede D-XXX` → grep 项目内引用 → `git blame` 比对引用行最后 commit 与 supersede 日期）。rule 前缀 `dec-ref-sync-*`。
- **`scripts/scan.sh` 注册 `--only decision-sync`**：与 `tasks/decisions/files/context-sync` 并列；默认 `all` 也跑。
- **4 个 config yaml 加 `dec_ref_sync` 段**：`enabled` / `scan_extensions` / `exclude_paths`，复用 `template/default/faropdf/legal-ai-skill-book`。
- **`evals/run-evals-v03.sh`**（208 行，6 用例 fixture）：全同步 / 引用行未改 / 无 supersede 标记 / 多层 supersede 链 / 无引用 / 引用早于 supersede。**6 PASS / 0 FAIL**。
- **SKILL.md §2 健康检查项表**加 `dec-ref-sync-stale` (adaptive) / `dec-ref-sync-removed-still-referenced` (hard) / `dec-ref-sync-clean`/`synced`/`no-markers` (ok)。
- **frontmatter `version` 0.2.4 → 0.3.0**；§1 标题与 §1.1 `--only` 参数同步加 `decision-sync`。

### Fixed（IMPLEMENTATION 过程中发现）

- **`check-dec-ref-sync.sh` `rel_path` 解析 bug**：`grep -rn` 输出 `path:line:content`，原代码 `rel_path="${hit#${REPO_ROOT}/}"` 只 strip REPO_ROOT 没 strip `:line:content` → `git blame` 找不到文件 → BLAME_TS 空 → 误报 adaptive。修：加 `rel_path="${rel_path%%:*}"`。
- **`evals/run-evals-v03.sh` 用例 2 fixture bug**：fixture 行注释说"与 D-2026-06-23-01 无关"但字串带旧 ID → grep 匹配第 2 行（blame 晚于 supersede）→ 误判 synced (ok)。修：fixture 行去掉旧 ID 字串。

### Decision

- 见 DECISIONS.md D-2026-07-03-02（v0.3.0 决策变更同步检测，spec + implementation 双阶段）。

### Reason

- 用户反馈（2026-07-01，本项目 DEC-081）"运营中决策后续改变但文档未同步"。doc-curator v0.2.0 的 context-sync 检测「改动→文档同步」，但缺少「DECISION supersede/修改 → 引用文档同步」的检测维度。
- 与 context-sync 互补（改动→文档 vs 决策→引用），形成完整文档同步检测矩阵。

## 0.2.4 - 2026-07-03

### Removed
- **删除 `GENERALIZATION-SPEC.md`**：v0.2.0 泛化（A config 泛化 + B check-context-sync + C 去 FaroPDF）原始设计 spec 已执行落地，A+B+C 三目标内容已沉淀到 DECISIONS D-2026-06-30-01 与本 CHANGELOG 0.2.0 段；保留 spec 文档会产生「同一信息双份来源 → 漂移风险」，与 doc-curator 自身维护「文档健康单点真相」原则矛盾。

### Changed
- `SKILL.md`「参考」段去掉对已删除文件的引用、加注脚指向 DECISIONS D-2026-07-03-01 + 本 CHANGELOG 0.2.4 段。
- `TASKS.md`：v0.2.0 完成项的来源引用从 `GENERALIZATION-SPEC.md` 改成「legal-ai-skill-book 游初定稿收口链（PR #145-159），目标 A+B+C」+ 顶部增加 0.2.4 完成条目。
- `evals/run-evals.sh` 顶部注释去除「GENERALIZATION-SPEC §6」指代，改为「对应 v0.2.0 上下文同步 design 用例清单；spec 文档 v0.2.4 删除后此注释保留以指代同一组用例」（用例与历史一致）。
- frontmatter `version`：0.2.3 → 0.2.4（housekeeping 增量版本号）。

### Reason
- 用户反馈："如果那个文档的都已经落地了，那这个文档可以删除了"（2026-07-03）。前提：A+B+C 目标已在 v0.2.0 / v0.2.1 / v0.2.2 / v0.2.3 完成且内容已沉淀。

### Decision
- 见 DECISIONS.md D-2026-07-03-01（spec 文档删除原则 + 信息沉淀去向）。

## 0.2.3 - 2026-07-02

### Fixed
- **LICENSE 归属对齐**：`LICENSE.txt` 版权方由「FaroPDF Project Contributors」改为「doc-curator contributors」，与 CHANGELOG 0.2.0 段「supersede v0.1.0 FaroPDF 专用定位」、frontmatter `author` / `homepage` 同步——v0.2.0 supersede 后漏改 LICENSE 一致性回填。
- **frontmatter 字段统一**：`homepage` 改 `https://github.com/cat-xierluo/legal-skills`、`author` 改 `杨卫薪律师（微信ywxlaw）`，与 `skills/` 下其它公开 Skill 的发布字段口径一致（`agent-email` 除外）；`version` 升至 0.2.3。
- **description 精炼**：移除内嵌命令路径（搬到 §1.1）+ 实现细节子句（上下文同步措辞），控制在前 250 字符建议区间，让触发指纹更聚焦。
- **state.json 跨项目分发提醒**：SKILL.md §6 末尾加「跨项目分发」段（symlink 部署 / 路径迁移时必须先删 baselines 再重建），对应 v0.2.0 阶段漏写。

### Added
- **`maintenance-pr.sh --dry-run`**：仅跑 scan + 检查 git 状态 + 打印将要 commit / push / PR 的预告（分支名 / 命中 rule_id / 推送目标端 / gh 命令），不做任何文件修改、commit、push、PR。SKILL.md §4 加「真跑前先跑 --dry-run」条款。

### Reason
- 来源：skill-lint 审查（2026-07-02，1 项严重 + 3 项警告 + 多项信息提示）。审查报告见聊天记录，finding：LICENSE-FAROPDF / frontmatter-homepage-isolation / maintenance-pr-auto-push / state-json-cross-project-reminder。

### Decision
- 见 DECISIONS.md D-2026-07-02-01（skill-lint v0.2.3 修复）。

## 0.2.2 - 2026-07-01

### Changed
- **`context-sync-skill-internal` 升级为三件套检测**：`.claude/skills/` 改动 → 检查 skill 内部 CHANGELOG + DECISIONS + TASKS 三件套是否同步（之前只查 CHANGELOG）。对齐 `feedback_legal_skill_internal_docs`（改 skill 必须三件套同步；新 skill 至少建 CHANGELOG）。来源：本项目 DEC-081 + 用户 feedback（项目中 skill 更新沉淀也要遵循相同规则，skill 内部多上下文需同步）。

### Decision
- 见 DECISIONS.md D-2026-07-01-02（skill 内部三件套检测）。

## 0.2.1 - 2026-07-01

### Added
- **SKILL.md §0 调用协议**：skill 被调用时默认 spawn inline subagent（`general-purpose` + §0.1 prompt 模板）跑 `scan.sh`，隔离上下文不占调用方（PM/主会话）。**协议写 skill 内部，不建项目级 `.claude/agents/` 配置**——任何项目部署（symlink/复制）即默认如此，可移植。来源：legal-ai-skill-book DEC-081 + 用户 feedback（doc-curator 是独立任务，应默认 subagent 隔离上下文，且协议要写 skill 内部保证换项目零配置）。

### Decision
- 见 DECISIONS.md D-2026-07-01-01（subagent 协议下沉 skill 内部 + 演进脉络：双轨→纯skill→项目级薄壳→协议下沉）。

## 0.2.0 - 2026-06-30

### Added
- **上下文同步检测（B）**：新增 `scripts/lib/check-context-sync.sh`，检测合并到 main 的改动是否同步更新 TASKS/CHANGELOG/DECISIONS/FIGURES-OUTLINE/skill 内部文档。按改动类型动态选查（不机械全查，省上下文），rule 前缀 `context-sync-*`（decision=hard，其余=adaptive），DEC-052 局部可逆二分。来源：legal-ai-skill-book 游初定稿收口链（PR #145-159）多次漏同步（DEC-078 教训）。
- **config 泛化（A）**：`common.sh` 加 `resolve_config()` 自动发现（`--config` / 项目根 `doc-curator.yaml` / skill config / `faropdf.yaml` 兜底）+ 纯 awk yaml 解析（`scripts/lib/yaml-flatten.awk`，无 yq 依赖）。现有 3 个 check 改为从 config 读文件清单与阈值，不再写死路径。
- 新增 config：`template.yaml` / `default.yaml` / `legal-ai-skill-book.yaml`；`faropdf.yaml` 加 `limit` / `context_sync` 段（向后兼容）。
- `scan.sh` 加参数：`--config` / `--since` / `--range` / `--only` / `--repo`。

### Changed
- **SKILL.md 泛化（C）**：去 FaroPDF（description/author/homepage 通用化），§2 加上下文同步维度，§6 加 config 机制，触发场景加 PR 合并/收口检测。
- `check-files.sh` 遍历 config files 段报行数，修掉非 FaroPDF 项目误报 DESIGN/ARCHITECTURE 不存在的问题。
- 修 `tasks-active-count` 在无活跃任务卡时重复输出 0 的 bug（`|| echo 0` → `|| true`）。

### Decision
- 见 DECISIONS.md：D-2026-06-30-01（泛化决策）/ -02（纯 awk yaml）/ -03（REPO_ROOT 用 cwd）。supersede v0.1.0 FaroPDF 专用定位。

## 0.1.1 - 2026-06-03

### Added
- **§1.4 与 worker PR 协调的窗口期**（精简版）：
  1. PM 合并 worker PR 期间不并发跑 doc-curator；多个 worker PR 串行合并时每个合并后独立跑。
  2. doc-curator 体检不重复改 worker PR 已写的 CHANGELOG / DEC / TASKS。

### Reason
- 来源：FaroPDF v0.1 Wave 1 真实合并 PR #18 / #19 前的根因复盘。
- 主要根因：doc-curator 在 PM 准备合并 worker PR 期间自动跑体检并提 maintenance PR，抢跑 main。

## 0.1.0 - 2026-06-03

- 首版发布。
- 体检脚本 `scan.sh`：检查 `docs/TASKS.md` / `docs/DECISIONS.md` / `docs/ROADMAP.md` / `docs/DESIGN.md` / `docs/ARCHITECTURE.md` / `CHANGELOG.md` / `README.md` / `AGENTS.md` 的硬性 / 自适应 / 软提示项。
- 首跑基线脚本 `first-baseline.sh`：测量各文件大小并写入 `state.json`。
- Maintenance PR 脚本 `maintenance-pr.sh`：在工作区干净时自动 trim 进度日志并提 PR。
- 配置：`config/faropdf.yaml` 声明监控文件、规则集和种子阈值。
- 状态：`state.json` 跟踪基线和历史。
- 触发：Agent 在 `gh pr create` 后 / `gh pr merge` 后 / 完成 ISS 汇报前主动调起；无 hooks 依赖。
