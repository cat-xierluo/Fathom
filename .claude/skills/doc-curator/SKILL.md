---
name: doc-curator
description: '本技能应在用户要求“文档体检”“审计 TASKS 当前任务源”“检查上下文同步”“检查归档一致性”“检查决策引用”或提交/发布前验收项目文档时使用。检查任务状态与合同、执行证据、文档规模、断链及 Git 改动是否同步上下文。默认只读；不要用于单文件审稿、正文改写、代码维护、自动提交、推送或创建 PR。'
license: MIT
metadata:
  version: "0.10.0"
  author: 杨卫薪律师（微信ywxlaw）
  homepage: https://github.com/cat-xierluo/legal-skills
---

# doc-curator 文档健康体检

运行确定性脚本，解析 JSONL 结果，并向用户报告 hard、adaptive、soft 三类问题。默认扫描只读取被体检项目；只有用户显式调用 `--init-baseline` 或 `--report` 时，才分别写入项目基线或用户指定的报告文件。任何入口都不修改业务文档、Git 状态或远端资源。

## 0. 默认调用协议：用 subagent 隔离体检上下文

当 Agent 因用户请求或项目收口规则调用本 Skill 时，默认把体检派给一个新的通用 subagent；不要在主会话内展开全量 JSONL 和被检文档。此协议随 Skill 自带，不要求项目建立 `.claude/agents/` 或其它薄壳配置。只有运行环境没有 subagent 能力、用户明确要求当前会话执行，或调用者本身已经是负责该体检的隔离 worker 时，才在当前会话运行。

subagent 指令至少包含：被检项目根、配置选择、Git 范围或 `--working-tree`、需要的 checker、只读边界，以及“读取本 SKILL.md 后运行 `scripts/scan.sh`，按退出码和 JSONL 报告 hard/adaptive/soft，不自行修改业务文档”。需要报告文件或初始化基线时，必须把对应显式写入参数和目标一并授权。主会话只消费结构化结论和必要证据，不重复跑同一全量扫描。

例外：显式使用下文「紧凑门禁模式」（`--profile merge-gate`）时，主 Agent 只消费一行固定 schema 的摘要，不需要展开全量 JSONL；`next_action` 为 `pass`（零 hard/adaptive）或仅含机械可修 hard（配置、断链、路径类确定性规则）时，不要求为解释结果再启动 subagent，只有摘要含 adaptive、`provide-config`、`fix-invocation` 或语义不明的 finding 时才需要 Agent 解释或人工判断。

## 快速开始

先确定本 Skill 根目录和被体检项目根目录，再执行：

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh --repo <project-root>
```

stdout 只包含 JSONL；人类可读日志写入 stderr。根据退出码和 JSONL 一起判断结果，不要只看命令是否打印内容。

需要一份可分享的人读报告时，加 `--report <file.md>`，体检完成后会额外生成 Markdown 报告（按 hard/adaptive/soft 分组，含汇总表；stdout 仍为 JSONL，报告写入文件，不改变输出契约）：

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh --repo <project-root> --report <path>/report.md
```

也可用管道把既有 JSONL 结果渲染成报告（`render-report.sh` 接受 stdin 或文件参数）：

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh --repo <project-root> 2>/dev/null \
  | <bash-4+> <doc-curator-root>/scripts/render-report.sh > report.md
```

报告只逐条列出 hard/adaptive/soft 三档（需关注项），ok 档只给计数；项目与配置元数据只写目录名和文件名，不写本机绝对路径。如需 Word 版本，把生成的 `.md` 交给 `md2word` skill 二次转换。

### 紧凑门禁模式（merge-gate）

routine merge review 不需要把全量 JSONL 灌进调用方上下文。显式加 `--profile merge-gate` 时，同一次 scan（checker 集合与执行顺序与 legacy 完全相同）改为输出一行固定 schema 的紧凑 JSON 摘要：

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh --repo <project-root> \
  --range <base>..<head> --profile merge-gate
```

```json
{"summary_schema":"doc-curator.merge-gate.v1","profile":"merge-gate","status":"complete","repo":"…","config_file":"…","config_source":"project-root","scope_type":"range","base":"<sha>","head":"<sha>","range":"<base>..<head>","hard_count":0,"adaptive_count":0,"soft_count":0,"ok_count":0,"blocking_rule_ids":[],"adaptive_rule_ids":[],"next_action":"pass","exit_code":0}
```

字段语义：

- `scope_type` + `base`/`head`/`range`：精确扫描范围。range/since 会解析为完整 commit SHA；解析失败产生 `merge-gate-range-unresolved` hard 并置空，不以未解析范围报告门禁结论。`working-tree` 模式 `head` 为当前 HEAD。
- `config_file` + `config_source`：配置 provenance。`bundled-default`（未提供项目配置、自动回落兜底）在门禁模式产生 `config-required` hard 并标记 `NOT_VERIFIED`，不作为通过证据；此时 `next_action` 为 `provide-config`。调用方必须显式传 `--config` 或提供项目配置，**不得**在 Skill 安装目录自动生成 `<repo>.local.yaml`。
- `hard_count` / `adaptive_count` / `soft_count` / `ok_count` 与 `blocking_rule_ids` / `adaptive_rule_ids`：与 legacy stdout 全量 JSONL 逐条对齐（回归测试断言完全一致）。
- `next_action`：`pass`（无需处理，不要求 subagent）、`resolve-hard`（按 rule ID 修复机械阻断）、`review-adaptive`（需要 Agent/人工解释）、`provide-config`（先补项目配置）、`fix-invocation`（参数/环境错误）。
- 退出码与 legacy 完全一致（0/1/2/64/65/66/78）；`status` 为 `error` 时额外带 `error` 字段。

默认不输出完整 JSONL，也不写任何文件；需要留档给后续会话时，显式加 `--jsonl-out <file>` 把全量 JSONL 原子写入指定文件（写失败按 `jsonl-output-error` hard 闭合）。`--profile merge-gate` 不可与 `--init-baseline`、`--report` 合用；`--jsonl-out` 只能在 merge-gate 模式使用。

### 依赖

#### 系统依赖

| 依赖 | 用途 | 安装方式 |
| --- | --- | --- |
| Bash 4+ | 所有脚本 | macOS：`brew install bash`；Debian/Ubuntu：`sudo apt-get install bash` |
| `awk`、`grep`、`sed`、`find`、SHA-256 工具 | 配置、文本与状态检查 | macOS/Linux 系统通常自带；SHA-256 支持 `shasum` 或 `sha256sum` |
| Git | 仅 `context-sync` 和 `decision-sync` | macOS：`xcode-select --install`；Debian/Ubuntu：`sudo apt-get install git` |

不需要 Python 包。macOS 自带 `/bin/bash` 通常为 3.2，不能运行本 Skill；安装后使用 Homebrew Bash 的实际路径。缺少 Bash 4+ 时脚本返回 `78` 并给出安装提示。

## 工作流

### 1. 选择配置

按以下优先级解析配置：

1. `--config <path>` 或 `DOC_CURATOR_CONFIG`（显式，最高）。
2. 项目根 `doc-curator.yaml`。
3. 项目根 `.doc-curator.yaml`。
4. Skill 内 `config/<repo-basename>.local.yaml`（按被体检项目目录名自动匹配，集中管理 per-project 配置）。
5. Skill 内 `config/<repo-basename>.yaml`。
6. Skill 内 `config/default.yaml`（兜底）。

内置默认配置只启用通用、低误报检查。需要项目约定时，把 `config/doc-curator.example.yaml` 复制为 Skill 内 `config/<project>.local.yaml` 或项目根 `doc-curator.yaml`，再按项目实际文档职责修改。per-project 配置统一放 Skill 内 `config/*.local.yaml`，避免污染项目根；仓库已忽略 `config/*.local.yaml`，不随公开发布。

若要按用户级 `AGENTS.md` v4 的语义审计另一个 Skill 根目录，直接加载内置档案：

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh \
  --repo <other-skill-root> \
  --config <doc-curator-root>/config/agentcmd-v4.example.yaml \
  --only tasks
```

该档案把被审计 Skill 根目录的 `TASKS.md` 视为当前任务源，并按状态检查任务卡：

- `DRAFT` 允许信息不完整。
- `READY`、`IN_PROGRESS` 必须说明缘由、目标与非目标、输入依赖、允许与禁止范围、停止条件和验收方式。
- `REVIEW` 必须列出交付物与验收证据；`DONE` 必须保留执行或验收证据。
- `BLOCKED`、`CANCELLED` 必须分别说明阻塞或取消原因。
- 队列与任务卡状态冲突、需完整合同但缺任务卡、启用审计却缺少 `TASKS.md`，均返回 hard finding。

字段标题和任务编号不是全局固定格式；需要适配项目级规则时，复制档案并修改 `task_source_contract` 下的正则、状态词和字段要求。若当前队列为空，应在 `TASKS.md` 中显式声明“当前无任务”或配置等价表达。

不要把客户名称、绝对路径、凭证或未脱敏业务材料写进公开配置。私有项目配置统一放在本 Skill 的 `config/` 中，命名为 `*.local.yaml`：被体检项目目录名（basename）匹配时自动加载（见上文优先级第 4 档），也可用 `--config <doc-curator-root>/config/<project>.local.yaml` 显式覆盖。仓库已忽略 `config/*.local.yaml`；公开发布时只包含 `default.yaml`、`doc-curator.example.yaml` 与 `agentcmd-v4.example.yaml`。

### 2. 初始化项目基线（按需）

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh \
  --repo <project-root> \
  --init-baseline
```

这是两个显式写入入口之一，原子写入 Skill 内 `<doc-curator-root>/state/<project_id>.state.json`（`project_id` = 被体检项目绝对路径的 SHA-256，按项目隔离，同名不同路径的项目互不覆盖）；另一个入口是 `--report <file.md>` 写用户指定的报告文件。状态包含项目路径哈希、配置哈希和文件基线，项目和配置任一变化后旧基线自动失效。所有 per-project 副作用（config 与 state）都集中在 Skill 目录下，被体检项目根目录零污染。

Skill 仓库已忽略 `state/` 与 `config/*.local.yaml`；不要提交运行状态，也不要在项目之间复制状态文件。

### 3. 运行体检

全量运行：

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh --repo <project-root>
```

只运行一个 checker：

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh \
  --repo <project-root> \
  --only tasks
```

可选 checker：

- `config`
- `tasks`
- `decisions`
- `files`
- `context-sync`
- `decision-sync`
- `markdown-link-broken`
- `active-zone-residue`
- `context-truth`
- `all`

提交前检查工作树：

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh \
  --repo <project-root> \
  --only context-sync \
  --working-tree
```

检查历史范围：

```bash
<bash-4+> <doc-curator-root>/scripts/scan.sh \
  --repo <project-root> \
  --only context-sync \
  --range <base>..<head>
```

`--working-tree` 不得与 `--since` 或 `--range` 合用；`--since` 不得与 `--range` 合用。未知参数、未知 checker 和缺失参数值必须视为调用错误，不得自动回退到全量扫描。

### 4. 解释结果

每一行使用固定结构：

```json
{"checker":"tasks","severity":"adaptive","rule_id":"tasks-active-count","message":"...","suggestion":"..."}
```

`severity` 含义：

| 值 | 含义 | 处理方式 |
| --- | --- | --- |
| `hard` | 规则失败或扫描无法可信完成 | 必须处理；不得报告为通过 |
| `adaptive` | 超过当前项目基线或需要人工判断 | 报告风险和建议，由用户决定 |
| `soft` | 非阻断提示或缺少可选基线 | 说明即可 |
| `ok` | 对应规则已实际执行并通过 | 无需处理 |

退出码：

- `0`：无 hard、无 adaptive；允许存在 soft。
- `1`：至少一个 hard。
- `2`：无 hard、至少一个 adaptive。
- `64`：参数错误。
- `65`：数据或配置格式错误。
- `66`：输入路径不存在。
- `69`：必要系统能力不可用。
- `78`：运行环境不满足要求。

扫描器验证每个 checker 的退出码、finding 严重度、非空输出、JSONL 结构和 checker 身份。checker 崩溃、空输出、非法输出，或 hard/adaptive 与退出码不一致，都会转成 `checker-*` hard finding。显式 `--only` 请求一个未启用 checker 会返回 `checker-disabled` hard，而不是把“未运行”报告为通过。

## 配置原则

`config/default.yaml` 是保守的公开默认值；`config/doc-curator.example.yaml` 展示完整可选能力。配置使用 `scripts/yaml-flatten.awk` 支持的 YAML 子集：缩进必须使用空格，值保持单行标量，列表使用逐项字段，不使用 flow array、多行字符串、锚点或引用。

配置必须声明 schema：既有能力仍支持 `schema_version: 2`；使用 v0.10.0 新增的 `context_truth.value_claims` 必须用 `schema_version: 3`。旧 reader 会明确拒绝 schema 3，防止忽略新声明后假绿；不要只把版本号降回 2。`config` checker 总是先运行，输出配置来源，并在自动发现到内容不同的多份候选配置时以 `config-shadowed-divergence` 失败闭合。flatten 后的任何配置路径都必须唯一；重复 `schema_version`、`enabled` 或 list field 会拒绝整份配置，不采用“首值胜出”。配置了某个 checker 的业务字段时必须同时显式声明它的 `enabled: true/false`；需要把某项作为项目验收证据时，再在 `config_contract.required_checkers` 中用空格分隔列出其 CLI 名称，例如：

```yaml
schema_version: 2
config_contract:
  required_checkers: "context-sync markdown-link-broken context-truth"
```

以下规则依赖具体项目约定，只有在 `hard_rules` 中显式列出时才运行：

- `tasks-progress-log-trim`
- `tasks-archived-iss-pointer`
- `decisions-iss-archive-ascending`
- `decisions-dec-numbering-continuous`
- `decisions-id-unique`

`task_source_contract.enabled`、`context_sync.enabled`、`dec_ref_sync.enabled`、`active_zone_residue.enabled` 和 `context_truth.enabled` 在公开默认配置中关闭。禁用项全量扫描时只产生“未验证”的 soft，不构成通过证据。启用任务源合同时应同时提供完整 `fields` 定义；配置缺失、字段正则非法或 requirement 引用未定义字段时失败闭合。任务源合同启用后，以显式状态为准，并跳过旧的 H3 标题总数型 `tasks-active-count`，避免完成历史被误算为活跃任务；多个 `IN_PROGRESS` 仍由合同规则给出 adaptive 提示。启用其它项目约定型检查前，先确认 Git 历史、决策编号、任务分区和文档职责符合项目实际。

Markdown 断链先去 fragment/query、URL decode，再按源文件目录归一化为 repo-relative 目标；已存在目标还会解析物理路径，允许指向仓内目标的 symlink，阻断穿透到仓外的 symlink。`source_severity_paths` 按源文件、`severity_paths` 按归一化目标、`pseudo_target_patterns` 按非文件型目标分流，均使用 bash glob 且首个匹配生效。

`context_truth.index_claims` 用 `source_file/item_pattern/id_pattern` 与 `mirror_file/claim_pattern` 比较镜像索引，`mode` 仍只支持 `max_numeric_suffix`；每条 mirror claim 必须唯一命中，不会用全文最大值掩盖陈旧声明。

角色/状态的显式单值一致性采用 opt-in `context_truth.value_claims`（配置时读 [示例](config/context-value-claims.example.yaml)）：

- 必填 `id`、`mode: exact_value`、`source_file`、`source_pattern`、`mirror_file`、`mirror_pattern`、`allowed_values`；`severity` 默认 hard，也可设 adaptive，仅改变**值漂移**严重度，配置/读取错误仍 hard。
- 两个 pattern 都是 `^...$` 整行 ERE，各用**恰好一个捕获组**提取非空值；匹配行在各自文件中必须唯一。`allowed_values` 是空格分隔的非空 token（可中文），不做大小写、空白或同义词归一化。
- 例如问题清单的 `question-draft` 被入口标为 `answers`，或源卡 `READY` 被镜像标为 `DONE`，都会产生 `context-truth-value-drift`。标签同为 DONE 只证明声明一致，**不证明任务已完成、作者已作答或回答提取准确**；仍需核对原始证据。现有 `task_source_contract` 负责任务卡字段、状态要求与队列表对照，不在这里复制合同引擎。
- 可与旧 index claims 混用；两类 ID 全局唯一。启用却零 claims、未知字段/模式、缺失/多重声明、提取失败、不在允许集或读错误均失败闭合。两类 source/mirror 都只能是仓内相对普通文件，拒绝绝对路径、`..` 组件和指向仓外/循环的 symlink；仓内 symlink 可用。

先在单独显式配置中验证少量真实声明，不因名称相似直接开启所有历史任务门禁，也不要为 checker 给原始访谈追加重复状态权威源。

adaptive 行数规则从 Skill 内状态读取基线；`tasks-active-count` 可在没有状态时使用配置的 `seed_value`。只有当前项目 ID 和配置哈希同时匹配时，状态才生效。

## 安全边界

- 默认扫描只读；`--init-baseline`、`--report` 和 merge-gate 模式的 `--jsonl-out` 分别只写 Skill 内状态文件（`state/<project_id>.state.json`）、用户指定报告与用户指定 JSONL 留档，不修改业务文档、源码、Git 索引、分支或远端，也不在被体检项目根留任何文件。门禁模式不会自动生成任何项目配置。
- 不提供自动 trim、commit、push 或创建 PR 的入口。
- 不把运行状态、项目私有配置、用户路径、密钥、Token 或客户材料放入发布包：`state/` 与 `config/*.local.yaml` 均被仓库忽略，state 只存项目路径哈希与文件行数基线，不含明文路径。
- 断链检查只验证本地相对目标是否存在，不访问网络。
- 报告只复述最小必要的路径和规则结果；处理敏感项目时先审查输出再分享。

## 开发验收

修改脚本或配置后运行：

```bash
<bash-4+> <doc-curator-root>/scripts/test-doc-curator.sh
```

该脚本在临时目录创建 fixture，覆盖参数错误、schema 缺失/旧版、非法 boolean、模糊 enabled、required checker、重复配置路径、配置来源、默认扫描、基线隔离、adaptive 分支、非法 regex、AgentCMD 任务状态合同、决策标题重复/跳号、URL 编码/伪链接/严重度/路径逃逸/仓内外 symlink、context truth 配置/缺文件/缺声明/不可解析/多重声明/索引漂移、Markdown 报告渲染，以及 checker crash、空输出、非法输出、身份错配、各严重度/退出码错配和报告 render/空产物/原子替换失败。它不访问网络、不改调用方仓库。

单值合同可单独运行 `scripts/test-context-truth-values.sh`（完整套件也调用它）。若持有未修改的 v0.9 Skill checkout，可加 `DOC_CURATOR_LEGACY_ROOT=<path>` 实测旧 reader 拒绝 schema 3；不提供时不声称已经做过跨版本运行验证。

配置读取可单独运行 `scripts/test-config-read.sh`，覆盖大合法配置、读取失败显式传播及 schema 2/3 基线初始化；初始化和扫描共用配置合同，不能在错误配置上建立“通过”基线。

同时执行 `bash -n`、ShellCheck（如已安装）、敏感信息扫描和引用检查。版本与设计理由分别记录在 `CHANGELOG.md` 和 `DECISIONS.md`，当前任务及验证证据记录在 `TASKS.md`。
