# Tasks

> 本地开发文件：由根目录 `.gitignore` 排除，不属于公开 Skill 交付内容。
> 最近梳理：2026-09-05

## 使用规则

- 本文件是 `git-workflow` 当前任务的权威入口。只有 `READY` 任务可以开始；`IN_PROGRESS` 只能继续当前任务。
- `DRAFT` 只记录方向，不构成执行授权；输入、边界或验收不足时不得自行补全。
- 执行前读取完整任务卡；基线变化足以影响方案时退回 `DRAFT`，依赖或权限未解决时转为 `BLOCKED`。
- 任务卡描述目标、边界和验收，不绑定执行主体、会话、工作树或具体编排方式。
- 本轮整理前未带 Task ID 的存量条目统一视为 `DRAFT`；升级为 `READY` 时再补齐完整任务卡。
- 任务完成后，按需将用户可见变化同步到 `CHANGELOG.md`、重要取舍同步到 `DECISIONS.md`，然后从活跃区移除；不为 `TASKS.md` 建立快照或专用历史文件。

## 当前队列

| 顺序 | Task | 配置 | 状态 | 主结果 | 形成 READY 前需补充 |
|---|---|---|---|---|---|
| 1 | `Task-001` | `Standard` | `DRAFT` | 评估"开 worktree 前 3 查"是否可脚本化（pre-worktree-check.sh），目前文档级已覆盖主流程 | 缘由、输入、边界与验收证据 |
| 2 | `Task-002` | `Standard` | `DRAFT` | 评估"PR 创建后 mergeable 检查"是否可前置到"开 worktree 前"，避免基于过期 main 开 worktree | 缘由、输入、边界与验收证据 |
| 3 | `Task-003` | `Standard` | `DRAFT` | 为常见事故补充恢复路径：误 amend、误 stash、误删分支 | 缘由、输入、边界与验收证据 |

## 完成记录

- `Task-009` — `DONE`（2026-09-19）：外部 PR 审查两层保障入册（v1.8.6/1.8.7）。§4 新增「分层审查——必要性 gate 先于代码审查」（需求来源核对、核心主张判据、伪需求识别；gate 不通过直接礼貌关闭，不投入代码审查）与「本地验证的环境卫生」（隔离 worktree 检出、主工作区动手前体检、中间态收尾纪律、报错文件不在 PR diff 内=污染信号）。实战来源：Folia #165 深审沉没（伪需求）+ #166 遗留中断 merge 被 `gh pr checkout` 叠加致 typecheck 假阳性。DEC-020 留档。
- `Task-008` — `DONE`（2026-09-05）：把分支清理从主文档拆到 `references/branch-lifecycle-and-cleanup.md`，统一一次性/长期生命周期、单 Worker 自动清理、批量 stale 审计和长期功能线关闭；主文档仅保留判定入口，安全规则与授权边界不变。
- `Task-007` — `DONE`（2026-09-05）：区分一次性 Worker head 与长期功能/集成基线。自动清理只允许 `ephemeral-worker`，长期分支及固定 Worktree 明确保留；PR `baseRefName`、metadata `base_ref` 与 integration target 必须一致，调用方不能把 `long-lived` 降级。机械实现由 `multi-agent-orchestration` v2.16.1 提供，确定性回归 cleanup 37/37。
- `Task-006` — `DONE`（2026-09-05）：固化编排 worker 验收后的单任务自动清理协议。交付、PR head、远端 tip、worktree/lifecycle 与本地 ref 全部绑定精确身份；squash/rebase 场景以 expected tip 的 `git update-ref -d` 取代无条件 `git branch -D`；输出统一为 `CLEANED`、`RETAINED_WITH_REASON`、`CLEANUP_PENDING`，清理失败不重放 merge/push。

## 草案与后续任务

- `Task-001` — `DRAFT`：评估"开 worktree 前 3 查"是否可脚本化（pre-worktree-check.sh），目前文档级已覆盖主流程
- `Task-002` — `DRAFT`：评估"PR 创建后 mergeable 检查"是否可前置到"开 worktree 前"，避免基于过期 main 开 worktree
- `Task-003` — `DRAFT`：为常见事故补充恢复路径：误 amend、误 stash、误删分支
- `Task-004` — `DRAFT`：评估是否需要轻量脚本化检查，但不要把本 Skill 扩张成提交生成器或任务状态系统
- `Task-005` — `DRAFT`：评估 agent-worktree 的 wt sync / 原子 wt merge 思路是否可转化为 Git 安全检查；在 Monorepo 中不得默认直接 merge feature 分支，必须保持目录级 checkout 或 PR diff 门禁
