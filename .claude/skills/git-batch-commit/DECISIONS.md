# 决策记录

> 本文件记录 git-batch-commit 的核心技术决策，与 [CHANGELOG.md](CHANGELOG.md) 互补。

## D-2026-02-07-01 小写英文前缀 + 中文描述，支持 GitHub 彩色标签

- 日期: 2026-02-07
- 背景: 初版提交格式用中文前缀（`类型：描述`），GitHub 不识别，无法显示彩色标签，提交历史可读性差。
- 决策: 统一改为小写英文前缀加英文冒号（`docs:` / `feat:` / `fix:` 等），描述部分保持中文。
- 影响: `generate_commit_message.py` 的 `CATEGORY_TO_TYPE` 映射改用小写英文；references/commit-types.md 补充 GitHub 彩色标签说明。

## D-2026-02-07-02 SKILL.md 视为功能文件，不归类为 docs

- 日期: 2026-02-07
- 背景: SKILL.md 虽然是 Markdown 格式，但它是定义技能行为/功能的核心文件，与 README、CHANGELOG 等记录性文档性质不同。
- 决策: 分类规则中，`SKILL.md` 的修改归类为 `feat`/`style`/`fix`（功能变更），而非 `docs`。同 skill 内由同一功能变更引起的 CHANGELOG 更新合并进同一条 commit，不单独拆 `docs`。
- 影响: 同 skill 配套变更合并规则写入 SKILL.md；提交粒度更聚焦于"一个功能变更 = 一条 commit"。

## D-2026-03-26-01 ClawHub 同步检查作为提交后步骤

- 日期: 2026-03-26
- 背景: skill 提交后经常忘记同步到 ClawHub，导致线上版本滞后。
- 决策: 在批量提交工作流末尾新增 ClawHub 同步检查：检测版本号升级 + 白名单命中后提示用户同步。
- 影响: 后续 1.2.4 补充检测 B（新增 MIT skill 首次同步）和检测 C（白名单新增但未同步），并加发布前敏感文件检查。

## D-2026-04-10-01 Subtree 推送检查作为提交后步骤

- 日期: 2026-04-10
- 背景: 部分已注册 subtree 的 skill 提交后忘记推送到独立仓库。
- 决策: 工作流新增第 6 步：当 `subtree-skills.json` 存在且提交涉及已注册子目录、且对应 standalone remote 存在时，提示推送。
- 影响: 与 ClawHub 检查并列，构成完整的"提交后发布检查"。

## D-2026-05-15-01 与 git-workflow 划界，聚焦 commit 拆分

- 日期: 2026-05-15
- 背景: 早期 git-batch-commit 的 references 里混入了 Issue/PR 命名规范，职责与 git-workflow 重叠。
- 决策: 把 `issue-pr-format.md` 迁移到 git-workflow；git-batch-commit 聚焦"已暂存变更的拆分和提交信息生成"本职。
- 影响: 职责边界表写入 SKILL.md；1.4.1 进一步明确不生成 `Closes #N`，Issue 关闭语义归 git-workflow。

## D-2026-05-19-01 轻量 Issue/Task 引用，不生成 Closes #N

- 日期: 2026-05-19
- 背景: 一组批量提交常关联某个 Issue 或本地任务，但本 skill 不负责判断是否应关闭 Issue。
- 决策: 新增 `--issue N`（标题追加 `(#N)`、正文写 `Refs #N`）和 `--local-ref`（正文写本地任务引用）；明确不生成 `Closes #N`。
- 影响: 引用与关闭语义分离；避免批量提交意外关闭仍需跟进的 Issue。

## 工作日志

### 2026-06-23

- 补建 DECISIONS.md 和 TASKS.md。从 CHANGELOG 1.0.0–1.4.1 回溯提炼 6 条核心决策。
