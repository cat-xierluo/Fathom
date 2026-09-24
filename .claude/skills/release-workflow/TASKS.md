# Tasks

> 本地开发文件：由根目录 `.gitignore` 排除，不属于公开 Skill 交付内容。
> 最近梳理：2026-09-21

## 使用规则

- 本文件是 `release-workflow` 当前任务的权威入口。只有 `READY` 任务可以开始；`IN_PROGRESS` 只能继续当前任务。
- `DRAFT` 只记录方向，不构成执行授权；输入、边界或验收不足时不得自行补全。
- 执行前读取完整任务卡；基线变化足以影响方案时退回 `DRAFT`，依赖或权限未解决时转为 `BLOCKED`。
- 任务卡描述目标、边界和验收，不绑定执行主体、会话、工作树或具体编排方式。
- 本轮整理前未带 Task ID 的存量条目统一视为 `DRAFT`；升级为 `READY` 时再补齐完整任务卡。
- 任务完成后，按需将用户可见变化同步到 `CHANGELOG.md`、重要取舍同步到 `DECISIONS.md`，然后从活跃区移除；不为 `TASKS.md` 建立快照或专用历史文件。

## 当前队列

| 顺序 | Task | 配置 | 状态 | 主结果 | 形成 READY 前需补充 |
|---|---|---|---|---|---|
| 1 | `Task-001` | `Strict` | `DRAFT` | 增加 Apple Developer 签名 / 公证流程（macOS Gatekeeper 提示处理） | 缘由、输入、边界与验收证据 |
| 2 | `Task-002` | `Strict` | `DRAFT` | 增加 Windows 代码签名流程 | 缘由、输入、边界与验收证据 |
| 3 | `Task-003` | `Strict` | `DRAFT` | 支持 Linux 平台构建（AppImage / deb / rpm） | 缘由、输入、边界与验收证据 |

## 草案与后续任务

- `Task-001` — `DRAFT`：增加 Apple Developer 签名 / 公证流程（macOS Gatekeeper 提示处理）
- `Task-002` — `DRAFT`：增加 Windows 代码签名流程
- `Task-003` — `DRAFT`：支持 Linux 平台构建（AppImage / deb / rpm）
- `Task-004` — `DRAFT`：支持 auto-generate Release Notes（从 PR 标题和标签自动生成草稿）。注：v1.5.0 已把 generate-notes API 收录为「贡献者致谢」的手工兜底（见 DEC-008），完整自动生成流程仍是本任务范围
- `Task-005` — `DRAFT`：支持多语言 Release Notes（中文 + 英文，参考 cc-switch 的 i18n release notes）
- `Task-006` — `DRAFT`：集成 conventional-changelog 或 git-cliff 自动从 commit 生成 CHANGELOG
- `Task-007` — `DRAFT`：支持预发布（pre-release）和金丝雀（canary）版本发布策略
