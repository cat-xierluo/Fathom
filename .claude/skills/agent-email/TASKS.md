# Tasks

> 本地开发文件：由根目录 `.gitignore` 排除，不属于公开 Skill 交付内容。
> 最近梳理：2026-07-30

## 使用规则

- 本文件是 `agent-email` 当前任务的权威入口。只有 `READY` 任务可以开始；`IN_PROGRESS` 只能继续当前任务。
- `DRAFT` 只记录方向，不构成执行授权；输入、边界或验收不足时不得自行补全。
- 执行前读取完整任务卡；基线变化足以影响方案时退回 `DRAFT`，依赖或权限未解决时转为 `BLOCKED`。
- 任务卡描述目标、边界和验收，不绑定执行主体、会话、工作树或具体编排方式。
- 本轮整理前未带 Task ID 的存量条目统一视为 `DRAFT`；升级为 `READY` 时再补齐完整任务卡。
- 任务完成后，按需将用户可见变化同步到 `CHANGELOG.md`、重要取舍同步到 `DECISIONS.md`，然后从活跃区移除；不为 `TASKS.md` 建立快照或专用历史文件。

## 当前队列

| 顺序 | Task | 配置 | 状态 | 主结果 | 形成 READY 前需补充 |
|---|---|---|---|---|---|
| 1 | `Task-001` | `Strict` | `DRAFT` | 收信任务的 PR 回流流程文档化（用户场景：Claude Code 发信派任务，结果走 git PR 回来） | 缘由、输入、边界与验收证据 |
| 2 | `Task-002` | `Strict` | `DRAFT` | mail-cli / agently-cli 版本升级跟踪与兼容性回归 | 缘由、输入、边界与验收证据 |
| 3 | `Task-003` | `Strict` | `DRAFT` | 评估 token / API Key 与 OAuth 凭据的安全存储与轮换 | 缘由、输入、边界与验收证据 |

## 草案与后续任务

- `Task-001` — `DRAFT`：收信任务的 PR 回流流程文档化（用户场景：Claude Code 发信派任务，结果走 git PR 回来）
- `Task-002` — `DRAFT`：mail-cli / agently-cli 版本升级跟踪与兼容性回归
- `Task-003` — `DRAFT`：评估 token / API Key 与 OAuth 凭据的安全存储与轮换
