# 决策记录

## [DEC-008] - 2026-09-21 - 外部贡献者致谢（Attribution）为 Release Notes 必选项

**背景**
Folia v0.8.1 发布后 Release Notes 整版遗漏外部贡献者致谢——该版三个修复全部源自同一位外部贡献者 @Yillan-lamb（#169 直接合入；#166 / #167 为 #171 / #170 的承接来源），notes 与 CHANGELOG 均无一字提及。原 `references/release-notes-guide.md` 调研表虽列出 bat「@贡献者」、Typst「贡献者致谢」，但没有任何落地规则。

**决策**
外部贡献者的 PR 必须在 Release Notes 中致谢，格式双轨：条目行内 `(#N, @user)`（必选）+ 文末「贡献者」汇总节（有外部贡献者时推荐）。

**规则要点**
1. **致谢对象**：直接合入的外部 PR、被「承接 #N」重做的原始 PR（方案来源同样致谢）、Co-Authored-By 外部作者；维护者自身条目与 bot（renovate / dependabot / github-actions）不标——让致谢信号聚焦。
2. **识别**：`gh pr list --state merged` 列 author login 对比维护者名单（单人项目 = owner 之外所有人）；squash commit 查 Co-Authored-By trailer。
3. **兜底**：GitHub generate-notes API（`by @user in #PR` + Contributors 段）可直接取用，漏识别时补搬。

**理由**
- eslint（`(#PR) (author)` 全条目）/ bat（行内 `(@handle)` + 文末 Contributors 节）/ orca（GitHub 原生 `by @user in #PR` + Contributors 头像墙）三方共识：行内 + 汇总节可叠加
- `@user` 在 GitHub Release 页面自动渲染为链接并通知本人——致谢即触达
- 漏掉致谢直接打击贡献者积极性，是社区项目的基本礼仪

**影响**
- `references/release-notes-guide.md` 新增「贡献者致谢」章节；`desktop-standard` 固定结构新增第 9 项「贡献者」节
- `SKILL.md` 第 2 步新增「来源 3 — PR 作者信息」，第 6 步验证与发布完成清单各加致谢检查
- `config/projects.yaml` / example 的 `always_include` 新增 `contributor_attribution`（folia / faropdf 已启用）
- 追溯修复：Folia v0.8.1 GitHub Release notes 与仓库 CHANGELOG.md 补 @Yillan-lamb 致谢

## [DEC-007] - 2026-06-08 - 强制：Release ≠ 测试，禁止把 release workflow 当作 CI 验证机制

**背景**
Folia 项目在 2026-06 账单周期（6/1-6/30）的 Actions 用量达到 1825/2000 分钟（91%）。调查发现，22 天内 15 个 release tag 中，大部分是"打 tag 看一下 CI 跑不跑得通"或"我下载个 artifact 自己测一下"，而不是真实用户发布。6/1 一天发 3 个 patch 版本（v0.3.16/17/18），全部是小修连续发版。这种模式消耗 80%+ 配额的根因是 **macOS × 2 target 是 10× 费率，单次 release 吃掉 300-600 配额分钟**。

**决策**
打 tag / 创建 GitHub Release 是把版本号给真实用户，不是 CI 验证机制。任何"把 release 当测试"的行为（用 tag smoke test、用 release 看 artifact、draft release 试探、dry-run 单平台验构建、纯 typo 改文档就发 patch）都是反模式，必须禁止。

**规则要点**
1. **强制五问自检**：发布前必须确认（a）给真实用户、（b）CHANGELOG 就绪、（c）距上次 ≥ 24h、（d）有实质改动、（e）能合并到下次就合并。
2. **替代方案**：CI 验证用 `pull_request` 触发的 preview workflow 或 `workflow_dispatch` 手动 dry build，不消耗 macOS 高倍率配额。
3. **抗借口设计**：列出 9 类常见借口（"用户催"、"之前都这么干"、"小改动"等）和 7 类红灯，逐条堵漏。

**理由**
- macOS runner 是 10× 费率，是单次 release 成本的主要贡献者
- draft release 一样跑完整 CI、一样消耗配额、一样污染 release 历史
- "已经做了"不是继续做的理由；沉没成本不应放大
- SemVer 的 patch 版本允许累积，单个 hotfix 完全可以等到 3-5 个 fix 一起发
- 用户不知道你的 Actions 配额，催你发版时应该让 ta 在"立刻发"和"合并到明天"之间选

**影响**
- `SKILL.md` 新增 `## ⚠️ Release ≠ 测试 — 强制约束` 章节（位于"发布前检查"之后）
- 发布完成检查清单拆分为"打 tag 前（强制）"和"发布完成后"两段
- description 触发词扩展为包含配额告急、频繁发版等反模式场景
- 适用于所有使用本 skill 的项目，不限于 Folia

## [DEC-006] - 2026-06-01 - 固定桌面应用 Release Notes 结构并支持项目级配置

**背景**
Folia 的历史 Release Notes 格式不完全一致：有的只有 changelog 分类，有的缺下载区，有的缺 macOS Gatekeeper 提示。桌面应用发布页需要稳定展示用户可读摘要、安装风险提示和下载入口。

**决策**
为桌面应用固定 `desktop-standard` 结构：摘要、Highlights、新增、变更、修复、Warning、下载、自动更新产物说明和完整变更日志。通用 skill 保留多项目适配能力，具体项目通过 `config/projects.yaml` 的 `release_notes` 配置指定 profile、必选分区和固定提示。

**影响**
- `references/release-notes-guide.md` 新增 `desktop-standard` 固定结构。
- `config/projects.example.yaml` 新增 `release_notes` 配置示例。
- 本机 `config/projects.yaml` 中 Folia 配置为 `desktop-standard`，并固定 macOS Gatekeeper warning、下载区和自动更新产物说明。

## [DEC-005] - 2026-06-01 - Release Notes 正文不重复版本标题

**背景**
GitHub Release 页面自身已经显示 release title。如果 Release Notes 正文再以 `# <项目名> vX.Y.Z` 开头，页面会连续出现两个版本标题，尤其在桌面应用发布页中显得重复。

**决策**
Release Notes 正文不写一级版本标题，也不写其他重复版本标题；正文直接从一句话摘要、升级提示或 Highlights 开始。

**影响**
- `references/release-notes-guide.md` 推荐模板移除顶部 `# <项目名> vX.Y.Z`。
- `SKILL.md` 的发布后检查清单增加“正文没有重复版本标题”。

## [DEC-004] - 2026-05-20 - 从 Tauri 专用改为通用发布工作流

**背景**
v1.0.0 的 SKILL.md 绑定了 Tauri v2 桌面应用，但发布流程的步骤（版本号管理、Release Notes、tag、CI 监控、验证、清理）对所有 GitHub 项目通用。

**决策**
SKILL.md 只描述通用流程，项目类型特定的内容（构建配置、产物格式、签名机制等）放入 `references/` 下的独立文档。

**理由**
- 版本号管理、tag、CI 监控、Release Notes 撰写是跨项目的通用能力
- Tauri 的 pnpm 配置、签名、latest.json 等细节不应污染通用流程
- 未来添加 Electron、Python 等项目类型只需新增 references 文档
- 降低 skill 的使用门槛，不限于特定技术栈

**影响**
- SKILL.md 重写为通用 7 步流程
- 新增 `references/tauri-release.md` 承接 Tauri 特定内容
- 新增 `references/release-notes-guide.md` 独立承载撰写指南
- `references/ci-troubleshooting.md` 改为通用 CI 故障排查

## [DEC-001] - 2026-05-20 - Release Notes 模板选型

**背景**
需要为 Tauri v2 桌面应用确定一个稳定的 Release Notes 格式。调研了 Zettlr、Clash Verge Rev、SiYuan、Obsidian、bat、Typst、NiceHash、Claude Code 等 8 个项目。

**选项**
1. 叙述式（Typst/NiceHash）：自然语言段落为主
2. 分类条目式（Zettlr/bat/SiYuan）：Features / Bugfixes / Breaking 分区
3. 极简平铺式（Claude Code）：所有条目平铺在 `## What's changed` 下

**决策**
选择选项 2 的简化版本：中文撰写 + Highlights 一句话 + 分类条目 + 下载表格 + callout 破坏性变更。

**理由**
- Folia 面向中文法律用户，中文 Release Notes 更自然（Clash Verge Rev 验证可行）
- 小项目用户不会逐条读 changelog，顶部 Highlights 是必需品（Zettlr 实践）
- 表格列下载链接比 `###`/`####` 分级轻量，比纯链接结构化
- `> [!WARNING]` callout 是 GitHub 原生功能，视觉醒目
- 每条附 PR 号保证可追溯性

## [DEC-002] - 2026-05-20 - 信息来源：CHANGELOG + git log 双源

**背景**
Release Notes 的信息来源有两种常见做法：只读 CHANGELOG.md，或只读 git log。

**决策**
综合使用两个来源：CHANGELOG.md 提供结构化分类（Added / Changed / Fixed），git log 补充上下文和细节。

**理由**
- CHANGELOG.md 可能在版本迭代中被编辑过，比原始 commit 更准确
- git log 包含 CHANGELOG 中可能遗漏的技术细节和 issue/PR 关联
- 两者交叉验证可以避免遗漏或重复

## [DEC-003] - 2026-05-20 - CI 包管理器：pnpm 替代 bun/npm

**背景**
GitHub Actions 全平台构建（macOS ARM + Intel + Windows）持续失败。根本原因是 npm/bun 的 optional dependencies bug（npm/cli#4828）。

**决策**
CI 中使用 pnpm 安装前端依赖。本地开发仍可用 bun/npm。

**理由**
- pnpm 在每个 CI runner 上独立解析 optional dependencies，不依赖本地 lock file
- 已在 Folia v0.3.7 发布中验证三个平台全部构建成功
- 参考 cc-switch 项目的实践

## 工作日志

### 2026-09-21 (GLM-5.3 / ZCode) - v1.5.0

- **目标**: 补齐外部贡献者致谢规则，追溯修复 Folia v0.8.1 的整版遗漏
- **操作**:
  - 调研 stablyai/orca（GitHub 原生 generate-notes：`by @user in #PR` + Contributors 头像墙）、bat（行内 `(@handle)` + 文末 Contributors 节）、eslint（全条目 `(#PR) (author)`）的致谢做法
  - `references/release-notes-guide.md` 新增「贡献者致谢」章节；`desktop-standard` 固定结构 / 推荐模板 / 设计决策 / 调研来源表同步
  - `SKILL.md` 第 2 步加「来源 3 — PR 作者信息」，第 6 步验证与发布完成检查清单加致谢检查
  - `config/projects.yaml` + example 加 `contributor_attribution` 约束键
  - 追溯修复：Folia v0.8.1 GitHub Release notes 与仓库 CHANGELOG.md 补 @Yillan-lamb 致谢
- **结果**: skill v1.5.0 就绪；决策沉淀为 DEC-008

### 2026-05-20 (Claude Opus 4.7) - v1.1.0

- **目标**: 通用化重构 + 审查 Funes 项目
- **操作**:
  - 审查 Funes 项目发布流程，发现 7 个可优化点
  - SKILL.md 重写为通用流程
  - Tauri 内容拆入 references/tauri-release.md，新增「常见配置问题与优化」
  - 新增 references/release-notes-guide.md
- **结果**: skill 适用于任何 GitHub 项目，Tauri 特定经验已沉淀

### 2026-05-20 (Claude Opus 4.7) - v1.0.0

- **目标**: 创建 release-workflow skill
- **操作**:
  - 调研 8 个开源项目的 Release Notes 格式
  - 编写 SKILL.md（7 步发布流程 + Release Notes 模板 + CI 故障排查）
  - 编写 references/ci-troubleshooting.md（5 个常见错误）
  - 编写 CHANGELOG.md / DECISIONS.md / TASKS.md
- **结果**: skill v1.0.0 就绪，已通过 Folia v0.3.7 发布验证
