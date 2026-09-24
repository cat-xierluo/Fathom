# 决策记录

### [DEC-001] - 2026-05-17 - 定位为平台桥接 Skill，而非 OpenClaw 原生 Skill

**背景**

ClawEmail 官方提供了 OpenClaw/Hermes 原生支持（Email Channel 模式）。用户使用的是 Claude Code 和 Codex 平台，无法直接使用 Email Channel。

**选项**

1. 等待 ClawEmail 官方适配更多平台。
2. 创建桥接 Skill，通过 mail-cli CLI 工具让非 OpenClaw 平台的 Agent 也能操作 ClawEmail 邮箱。

**决策**

选择方案 2。

**理由**

mail-cli 已经是成熟稳定的 CLI 工具，零 token 消耗，且不依赖特定平台。桥接模式让任何能执行 shell 命令的 Agent 都能接入。

**影响**

Skill 核心围绕 mail-cli 命令封装，不涉及 Email Channel 模式。

### [DEC-002] - 2026-05-17 - 内化官方文档为离线参考

**背景**

ClawEmail 官方文档在 claw.163.com，需要联网访问。Skill 用户可能离线或网络受限。

**选项**

1. 只放链接，让用户自行查阅。
2. 将官方文档关键内容内化到 `references/` 目录。

**决策**

选择方案 2。

**理由**

开箱即用是 Skill 的核心价值。内化文档涵盖：ClawEmail 介绍、申请流程、两种模式对比、场景速查、mail-cli 命令参考。

**影响**

新增 `references/clawemail-overview.md` 和 `references/clawemail-mail-cli-guide.md`。官方文档更新时需手动同步。

### [DEC-003] - 2026-05-17 - 移入正式 skills 目录发布

**背景**

ClawEmail 服务是通用工具，不限于法律场景，但已经作为法律工作流中的基础设施工具纳入正式发布目录。

**决策**

移至 `skills/agent-email/`。

**理由**

邮件服务是 Agent 协作基础设施，可支持法律材料收发、任务分发和跨平台协作。放入正式 `skills/` 目录便于随项目分发。

### [DEC-004] - 2026-05-17 - 纯 mail-cli 模式，移除 claw-setup 依赖

**背景**

claw-setup 脚本默认走 OpenClaw 插件安装流程，在 Claude Code 环境下 npm 依赖安装失败。实际上 mail-cli 可以独立于 OpenClaw Gateway 运行，直接通过 API Key 认证。

**选项**

1. 修复 OpenClaw 插件安装问题，继续用 claw-setup 全流程。
2. 绕过 claw-setup，直接用 curl 解析 auth-url 获取凭证，再通过 mail-cli auth 配置。

**决策**

选择方案 2。

**理由**

用户的场景是 Claude Code 发邮件派任务，结果走 git PR 回来。不需要 Email Channel 的实时监听能力，也不需要 OpenClaw Gateway 运行。纯 mail-cli 足够，且更轻量。

**影响**

删除 setup.sh 和 .env.example。mail-ops.sh 新增 `claw_init` 一键配置函数。SKILL.md v0.2.0 全面重写。

### [DEC-005] - 2026-07-20 - 多 provider 融合：接入腾讯 Agent Mail，统一一套 API

**背景**

腾讯 QQ 邮箱团队推出 Agent Mail（`@tencent-qqmail/agently-cli`），并发布纯文档型官方 skill `agently-mail`。用户希望由现有 agent-email skill 统一管理所有 Agent 邮箱（网易 + 腾讯），不平行安装第二个 skill。

**选项**

1. 平行安装腾讯官方 `agently-mail` skill，与 agent-email 各管一家。
2. 在 agent-email 内做 provider 路由，把腾讯 agently-cli 作为第二个后端，统一一套 `mail_*` API。
3. 用 IMAP/SMTP 通用协议层抽象，自实现协议适配。

**决策**

选择方案 2。

**理由**

- 方案 1 会让两个 skill 抢同一触发场景（"发邮件"），造成歧义；腾讯官方 skill 是纯文档（零脚本），本 skill 的 bash 封装 + 统一 API 比裸命令更好用。
- 方案 3 违背 DEC-001 桥接定位（mail-cli 已是成熟工具，不重造协议轮子）；且两家各有独有能力（claw 的子邮箱管理、agently 的 reply/forward/watch/两步确认），通用协议层会抹平差异。
- 方案 2 复用现有 `mail-ops.sh` 的 `_CLI` 集中调用点，改为 `_provider_cli` 路由；保留 `claw_*` 别名不破坏现有 cron/脚本；命名层 v0.3.0 已通用化（agent-email），代码层这次补上。
- 腾讯官方 skill 的可吸收内容（命令集、两步确认协议、exit code 矩阵、prompt injection 安全规则）按 DEC-002 同路内化为 references，不平行安装。

**影响**

- `mail-ops.sh` 三层 API（`mail_*` / `_claw_*` + `_agently_*` / `claw_*` 别名）+ `_dispatch` 路由。
- 腾讯写操作走两步确认（`ctk_xxx`）：函数层透传 `--confirmation-token`，协议层（停下等用户）写在 SKILL.md，不在 bash 里 read 交互（避免破坏 Agent 非交互式调用）。
- 腾讯 OAuth（`auth login` 交互式）由 `agently_login` 工程化：macOS 原生 `script` pty + grep 抓 URL，补上腾讯官方甩给宿主的部分。
- 新增 references：`agently-{overview,setup-guide,cli-guide}.md` + `backends-install.md`（集中两家 CLI 安装）。
- 同步网易 mail-cli 官方最新参数（`--id`/`--json`/`--unread`/`--part`/`--since`/`--body-file`/`master-user`）。

### [DEC-006] - 2026-07-20 - 腾讯 ctk 两步确认改为自动完成 + 对话层确认

**背景**

DEC-005 接入腾讯 agently 时，按官方协议把两步确认（ctk_xxx，5 分钟有效）做成「函数透传 + SKILL.md 协议层停下等用户」。用户反馈：手动两步体验差，且 ctk 过期机制让自动化（cron）失败。

**选项**

1. 保持官方 ctk 两步（函数透传，用户手动走两步）。
2. 函数内部自动完成两步（拿 ctk + 立即带 ctk），确认移到对话层。
3. 完全绕过 ctk（不可行，agently-cli 协议强制）。

**决策**

选择方案 2。

**理由**

- 方案 1 的 ctk 5 分钟过期对自动化致命（cron 任务可能因间隔过期失败），手动两步对交互也是负担。
- 方案 3 不可行：agently-cli 写操作强制要 ctk，无法跳过。
- 方案 2 在函数层把两步合二为一（`_agently_auto_two_step`），调用方一次完成，无间隔不过期；「用户确认」上移到对话层（Agent 拟稿 → 用户说"发" → 一次发出），体验更自然，安全等价（人类仍看了内容才确认）。
- 自动化（cron）场景：任务已预设授权，直接发，不再被 ctk 过期阻塞。
- 实现注意：agently-cli 输出含 JSON + tip 提示行，`_agently_auto_two_step` 用 grep 提取 `confirmation_required` / `ctk_xxx`（不用 jq，不怕 tip 行干扰）。

**影响**

- `mail-ops.sh` 新增 `_agently_auto_two_step`；`_agently_send` / `_agently_reply` / `_agently_forward` / `_agently_trash` 改为调它。
- 默认自动两步；显式传 `--confirmation-token` 时透传（手动兼容）。
- SKILL.md「两步确认协议」改为「写操作确认（对话层）」；`agently-cli-guide.md` 加自动完成说明。
- 安全规则不变（仍防 prompt injection）。

---

## 工作日志

### 2026-05-17 14:31 (Codex)

- **目标:** 修复 agent-email 发布前检查发现的小问题
- **操作:**
  - 修复 mail-cli 的 npx 调用方式，移除多余参数分隔符
  - 使用数组重写 `claw_send` 参数组装，避免 `--from` 重复和空格拆分
  - 修正 `.env.example`、首次配置指南和本地 `.env` 的 `DISPLAY_NAME` 写法
  - 移除函数库顶层 shell 选项修改，避免 source 后影响调用方终端
  - 同步 SKILL、README、Marketplace 和 CHANGELOG 版本到 `0.3.1`
- **结果:** 脚本 source、参数构造和 mail-cli 帮助命令验证通过
- **下一步:** 可做一次真实发信/收信端到端验证

### 2026-05-17 15:00 (Claude Code)

- **目标:** 完成邮箱配置并重写 Skill 为纯 mail-cli 模式
- **操作:**
  - 从 auth-url 解析出邮箱（maoking.agent@claw.163.com）和 API Key
  - 通过 mail-cli auth apikey set + auth login 完成配置
  - 验证 folder list 和 mail list 连通正常
  - 重写 SKILL.md v0.2.0（纯 mail-cli，无 claw-setup 依赖）
  - 重写 mail-ops.sh（npx 调用，新增 claw_init）
  - 删除 setup.sh 和 .env.example
  - 新增 DECISIONS.md
- **结果:** 邮箱 maoking.agent@claw.163.com 已配置可用；Skill v0.2.0 完成
- **下一步:** 实际使用中测试发信功能

### 2026-05-17 10:00 (Claude Code)

- **目标:** 创建 agent-email Skill 初始框架
- **操作:** 创建 SKILL.md、scripts/、references/、templates/，尝试 claw-setup（失败）
- **结果:** 框架完成，claw-setup 因 OpenClaw 插件 npm 依赖问题失败
