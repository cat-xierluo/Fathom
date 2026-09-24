# 决策记录与工作日志

## 决策记录

### [DEC-001] - 2026-04-23 - 凭据仅保留在本地忽略配置或环境变量中

**背景**
原配置文件曾包含 GitHub PAT，且配置文件被 Git 跟踪。该做法会导致凭据进入提交历史，也会让不同 Agent 在复制仓库时扩散敏感信息。

**选项**
1. 继续跟踪 `config/monorepo.yaml`：使用方便，但泄露风险高。
2. 使用本地未跟踪配置保存 token：兼容已有使用方式，但必须依赖 `.gitignore` 防误提交。
3. 仅通过 `GITHUB_TOKEN` 环境变量提供 token：最安全，但会改变现有使用习惯。

**决策**
选择选项 2，并推荐在自动化环境中优先使用选项 3。

**理由**
该 skill 的核心场景是跨 Agent 共享仓库，凭据必须和共享内容解耦；同时保留本地忽略配置可以兼容现有私有工作流。

**影响**
`config/monorepo.yaml` 从 Git 跟踪中移除并加入 `.gitignore`；脚本优先读取 `GITHUB_TOKEN`，其次读取本地忽略配置中的 token。

### [DEC-002] - 2026-04-23 - 统一任务命名为 `{YYMMDDNNN}-{中文分类}-{title}`

**背景**
原文档同时存在 `YYMMDD-{type}-{topic}`、中文 type、英文 type、完整 ID 等多套命名方式，导致不同 Agent 可能创建不兼容的目录和分支。

**选项**
1. 保留云端现状的中文分类：便于人类阅读，并与现有目录一致。
2. 统一英文 type，标题保留中文：便于脚本处理，但会偏离云端现状。
3. 同时支持中英文 type 且不归一：短期灵活，但会继续制造分歧。

**决策**
选择选项 1，并让脚本兼容英文别名输入后归一到中文分类。

**理由**
云端 `Monorepo-Collab` 当前目录已经采用 `260305001-法律-...`、`260305002-研究-...` 等中文分类格式，skill 应顺应现状，而不是强行迁移已有任务。

**影响**
`SKILL.md`、`references/naming.md`、`references/task-types.yaml`、模板和脚本均统一到同一命名模型；dashboard 优先使用文件夹名，避免被旧 README frontmatter 中的历史 slug 误导。

### [DEC-003] - 2026-04-23 - 历史残留先审计后渐进迁移

**背景**
云端 `Monorepo-Collab` 已经存在旧版本残留，例如目录名使用中文分类，但部分 README frontmatter 中的 `slug`、`type` 仍是旧值或业务细分值。

**选项**
1. 立即批量重写所有历史任务 metadata：结果整齐，但容易破坏溯源和合并记录。
2. 只规定未来新任务格式：风险低，但旧任务持续干扰 dashboard 和 Agent 判断。
3. 增加只读审计工具，按当前任务逐步修复：能识别问题，同时避免批量误改。

**决策**
选择选项 3。

**理由**
该 monorepo 的价值在于长期交接和溯源，历史残留应作为迁移对象处理，而不是直接覆盖。

**影响**
新增 `scripts/audit_monorepo.py` 与 `references/legacy-migration.md`；后续 Agent 在修改旧任务前先审计，再做小范围修复。

### [DEC-004] - 2026-04-23 - 区分 Git Author 与 GitHub Actor

**背景**
Monorepo-Collab 需要让 Manus、AnyGen、OpenClaw、Codex、Claude Code、Coze 等不同来源的 Agent 共同提交工作，并能追溯具体是谁做了什么。

**选项**
1. 所有 Agent 继续共用用户的 GitHub 身份：最简单，但 PR 和提交难以追溯来源。
2. 仅在 PR 描述中写 Agent 名称：有上下文，但 Git 历史本身不可靠。
3. 每个 Agent 配置独立 Git author；需要平台级归属时再配置独立 token 或 GitHub App token。

**决策**
选择选项 3。

**理由**
Git author 可以稳定记录 commit 内容作者；GitHub PR opener 由 token 决定，不能通过 `git config` 伪造。两者必须分层处理。

**影响**
`config/monorepo.yaml.example` 增加 agent 身份字段；`gh_git.py` 和 `monorepo_scaffold.py` 在提交前设置 repository-local Git author；`gh_git.py pr` 创建 PR 时写入 Agent Attribution；文档新增 `references/agent-identity.md`。

### [DEC-005] - 2026-04-23 - 新建任务前必须查重

**背景**
用户可能忘记某个主题已经研究过，再次要求 Agent 研究同一内容，导致 monorepo 出现多个重复任务文件夹，后续难以整合与追溯。

**选项**
1. 继续依赖 Agent 自觉搜索：实现成本低，但容易漏查。
2. 创建任务前默认查重，命中相似主题就阻止新建：能减少重复，但可能需要 `--force-new` 处理少数误判。
3. 允许重复创建，事后再合并：会增加整理成本。

**决策**
选择选项 2。

**理由**
Monorepo-Collab 的核心价值是同一主题持续沉淀，而不是产生多个相互割裂的二级文件夹。

**影响**
新增 `scripts/find_task.py`；`monorepo_scaffold.py create` 默认查找相似任务并在命中时停止新建；确需新建时必须显式使用 `--force-new`。

### [DEC-006] - 2026-04-23 - 邮件触发先生成草稿，不默认发送

**背景**
部分外部 Agent 可能支持通过邮箱触发任务。用户希望不用打开网页，也能把任务派发给这些 Agent，并要求它们提交到对应 monorepo。

**选项**
1. 直接接入 SMTP 自动发送：方便，但误触发和凭据风险较高。
2. 生成标准邮件草稿，由用户或上层工具发送：安全，可审查，可适配不同邮箱系统。
3. 只写文档，不提供脚本：实现简单，但每次容易漏掉仓库、分支、PR 规则。

**决策**
选择选项 2。

**理由**
邮件是跨系统触发入口，应先保证内容标准化和可审查。真实发送可以作为后续适配器扩展。

**影响**
新增 `scripts/email_trigger.py`、`references/email-trigger.md` 和 `agents.<id>.trigger_email` 配置字段；邮件正文默认包含查重、分支、提交、PR 和归属要求。

### [DEC-007] - 2026-05-17 - 项目适配层优先于 Skill 源码定制

**背景**
书籍写作项目暴露了一个泛化问题：如果每个新项目都要求修改 Skill 源码中的任务类型、模板和配置路径，后续多项目复用会持续返工。

**选项**
1. 为书籍项目硬编码专用字段和任务类型：落地快，但会污染通用 Skill。
2. 保留旧路径兼容并继续双命名：迁移压力小，但配置与文档长期混乱。
3. 固定新接口，项目通过 `config/collab.yaml`、`config/task-types.yaml` 和 `templates/tasks/` 适配。

**决策**
选择选项 3，并直接移除旧 `github-monorepo-collab` / `config/monorepo.yaml` 路径读取。

**理由**
`cross-agent-collab` 应承担跨平台协作底座职责，而不是承载某个项目的业务模型。项目适配层可以让书籍、法律项目、研究项目等复用同一套脚本。

**影响**
新增共享 helper 和项目 starter；新任务使用 `assignee`、`dependencies`、`artifact_paths`；脚本支持动态任务类型、模板渲染、可执行任务过滤和 README 上下文注入。

### [DEC-008] - 2026-05-17 - `docs/TASKS.md` 作为常规任务唯一主状态源

**背景**
新的项目协作规范已经把 `docs/TASKS.md` 定义为常规任务登记簿。旧方案中任务文件夹 README、GitHub Issue、脚本状态字段都可能表达任务状态，容易让不同 Agent 更新不同位置。

**选项**
1. 继续让任务文件夹 README 作为状态源，`docs/TASKS.md` 只做索引。
2. 保留旧 `git-task-orchestrator` 的 canonical task registry 思路，另建项目级任务表。
3. 直接以项目 `docs/TASKS.md` 为唯一主状态源，任务文件夹只承载材料、产物和交接上下文。

**决策**
选择选项 3。

**理由**
`docs/TASKS.md` 已经是项目上下文的一部分，迁移成本低，也足够泛化；任务文件夹适合承载大任务材料，但不适合与 Issue 文档并列维护状态。旧 canonical registry 思想有价值，但在当前 Skill 边界中会重新制造第四个总控层。

**影响**
`find_task.py --available` 默认读取 `docs/TASKS.md`；`email_trigger.py --issue` 从 Issue 抽取目标和验收标准；配置增加 `issue_file`、`status_map`、`available_statuses`、`task_context_mode`；任务 README 状态只作为补充上下文。

### [DEC-009] - 2026-05-17 - 外部 Agent 作为能力 Adapter 而非协作总控

**背景**
用户的协作体系中存在 Manus、AnyGen、邮箱触发等外部连接件。它们能力不同：有的适合复杂网络搜索，有的适合图片生成，有的只是通信通道。若每个连接件都扩张出自己的任务状态或执行协议，会重新制造边界混乱。

**决策**
在 `cross-agent-collab` 中把外部 Agent 定义为能力 adapter：通过 `agents.<id>.capabilities`、`trigger_modes` 和 `handoff_format` 声明能力与返回方式。Adapter 只能接收绑定到 `docs/TASKS.md` 的任务上下文，并通过 PR 或 durable handoff 回写结果。

**理由**
任务状态应继续归 `docs/TASKS.md`，本地 session 归 `multi-agent-orchestration`，Git 安全归 `git-workflow`。外部连接件只解决“谁更适合执行某类任务”和“如何触发/交接”的问题。

**影响**
配置示例、邮件协议和 Agent 指南均补充 adapter 能力路由。后续新增 Manus/AnyGen/Lark/SMTP 适配时，只扩展 adapter 字段和触发器，不新增第四个协作总控。

### [DEC-010] - 2026-05-20 - 重命名为 cross-agent-coordination

**背景**
`cross-agent-collab` 中的 `collab` 过于泛化，容易和 `multi-agent-orchestration`、`git-workflow` 混淆。该 Skill 的实际职责是跨平台 Agent 的任务协调：任务源解析、负责人/归属、能力 adapter 路由和交接上下文。

**决策**
将 Skill 目录和 frontmatter `name` 重命名为 `cross-agent-coordination`，显示标题改为 Cross-Agent Coordination。

**理由**
`coordination` 更准确表达“任务协调层”的边界，能和 `multi-agent-orchestration` 的“本地执行编排层”、`git-workflow` 的“Git 安全层”形成稳定命名体系。

**影响**
同步更新 SKILL.md、CHANGELOG、TASKS、脚本提示、邮件主题前缀、测试文件名和相关参考文档。`config/collab.yaml` 继续保留，避免破坏既有项目配置。

### [DEC-011] - 2026-06-01 - 发布包只保留任务协调核心资料

**背景**
发布前审查发现，Skill 目录中仍有弱相关的 Git LFS 策略、Profile 模板、旧 monorepo 模板空目录、书籍写作项目 adapter 样例和脚本冲突副本。这些内容不属于跨平台任务协调的核心边界，会增加公开发布包的噪音。

**决策**
发布候选版本只保留任务协调所需资料：任务命名、Agent 启动与交接、身份归属、邮件触发、旧任务迁移和任务类型注册表。Git 存储策略、项目偏好模板、书籍写作 adapter、通用 project starter 和旧 dashboard/monorepo 模板不纳入本 Skill。

**理由**
`cross-agent-coordination` 的职责是项目任务源解析、Agent 归属、能力路由和交接上下文。Git LFS、项目 Profile、书籍写作 adapter 和旧 monorepo 资产属于其他层级或历史实现，保留会模糊职责边界。

**影响**
frontmatter 版本更新为 `1.0.0` 正式发布候选；删除弱相关参考文档、项目型 adapter 样例、通用 project starter、旧模板空目录、macOS 缓存文件和冲突副本。后续若需要发布到公开目录，仍需同步根 README 和 marketplace 索引。

## 工作日志

### 2026-06-01 (Codex)

- **目标:** 清理 `cross-agent-coordination` 发布包。
- **操作:** 删除弱相关 reference、项目型 adapter 样例、通用 project starter、旧 monorepo 模板残留、macOS 缓存文件和脚本冲突副本；将版本更新为 `1.0.0`；同步 TASKS 和 CHANGELOG。
- **结果:** 发布包只保留任务协调核心资料和当前脚本实现。
- **下一步:** 正式迁移到公开 `skills/` 目录时，同步根 README 和 `.claude-plugin/marketplace.json`。

### 2026-05-20 (Codex)

- **目标:** 将跨平台 Agent 任务协调 Skill 定稿为 `cross-agent-coordination`。
- **操作:** 重命名目录、frontmatter `name` 和标题；更新脚本提示、邮件主题前缀、测试文件名、参考文档和 `multi-agent-orchestration` 相关引用；保留 `config/collab.yaml` 作为兼容配置名。
- **结果:** Skill 升级为 v0.7.0，命名与三层协作边界一致。
- **下一步:** 后续如要进一步统一配置命名，可另行评估 `collab.yaml` 的兼容迁移策略。

### 2026-05-20 (Codex)

- **目标:** 同步本地多 Agent 执行层的定稿命名。
- **操作:** 将 `multi-agent-workflow` 相关引用更新为 `multi-agent-orchestration`，同步 SKILL、Agent guide 和 CHANGELOG。
- **结果:** `cross-agent-collab` 升级为 v0.6.4，三层边界命名保持一致。
- **下一步:** 无。

### 2026-05-20 (Codex)

- **目标:** 同步本地多 Agent 执行层的 Skill 重命名。
- **操作:** 将 `parallel-agent-workflow` 相关引用更新为 `multi-agent-workflow`，同步 SKILL、Agent guide 和 CHANGELOG。
- **结果:** `cross-agent-collab` 升级为 v0.6.3，三层边界命名保持一致。
- **下一步:** 无。

### 2026-05-17 (Codex)

- **目标:** 收口外部 Agent 连接件边界。
- **操作:** 为 `collab.yaml.example` 增加 adapter 能力声明；更新邮件触发协议和 Agent guide，明确外部 Agent 只作为能力 adapter。
- **结果:** `cross-agent-collab` 升级为 v0.6.0，支持按能力选择 Manus/AnyGen 等外部 Agent，同时保持 `docs/TASKS.md` 为唯一主状态源。
- **下一步:** 后续如新增真实发送器，只作为 `trigger_modes` 的实现扩展。

### 2026-05-17 (Codex)

- **目标:** 按 `docs/TASKS.md` 主状态源原则收口 `cross-agent-collab`。
- **操作:** 新增 Issue 解析、可执行任务过滤、`email_trigger.py --issue`、书籍 adapter 状态映射和三层边界说明。
- **结果:** `cross-agent-collab` 升级为 v0.5.0，任务文件夹从状态源降级为材料包/交接包。
- **下一步:** 继续补 PR 模板校验和可选邮件发送适配器。

### 2026-05-17 (Codex)

- **目标:** 按泛化优化计划升级 `cross-agent-collab`，让书籍项目只作为适配样例。
- **操作:** 新增共享 helper；重写任务创建、查找、邮件触发、Git 归属和审计脚本的配置读取；新增动态任务类型、项目模板、依赖过滤、`--field` 自定义字段、starter 和书籍适配样例；同步 SKILL、references、TASKS、CHANGELOG。
- **结果:** `cross-agent-collab` 升级为 v0.4.0，固定使用 `config/collab.yaml` 和项目适配层，不再读取旧路径；回归测试覆盖主要泛化场景。
- **下一步:** 可继续补 TASKS.md assignee 解析和 PR 模板校验。

### 2026-05-16 (Claude Code)

- **目标:** 重构 `github-monorepo-collab` → `cross-agent-collab`，去除 GitHub + Monorepo 绑定，聚焦跨平台 Agent 协作。
- **操作:** 重命名目录、脚本和配置文件；删除过时的 Dashboard 和 Obsidian 脚本；重写 SKILL.md，新增任务来源与分配机制（TASKS.md + GitHub Issues）、单 Repo 模式、Related Skills 章节；Agent 归属提升为第一优先级。
- **结果:** Skill 重命名为 `cross-agent-collab` v0.3.0，支持 Monorepo 和单 Repo 两种模式，Agent 提交归属规则明确，任务分配通过 `docs/TASKS.md` 的 `assignee` 字段实现。
- **下一步:** 脚本内部引用 `monorepo` 的地方需要逐步清理（`task_scaffold.py`、`gh_git.py`、`find_task.py` 等）。

### 2026-04-23 16:48 (Codex)

- **目标:** 修复 `github-monorepo-collab` 的安全、可运行性和规范一致性问题。
- **操作:** 将真实配置改为本地忽略文件；读取云端 `Monorepo-Collab` 现状；修复 `gh_git.py` CLI；修复任务 ID 生成；移除 `requests` 依赖；补充 PyYAML 依赖提示；统一为中文分类命名规范；补齐技能级文档；增加 Agent 身份归属规则。
- **结果:** `config/monorepo.yaml` 不再被 Git 跟踪但可在本地保留 token；脚本和文档与云端现有中文分类目录保持一致；提交可按 Agent 写入独立 Git author。
- **下一步:** 如该仓库已推送过包含 token 的历史提交，评估是否需要撤销 token 或清理 Git 历史。
