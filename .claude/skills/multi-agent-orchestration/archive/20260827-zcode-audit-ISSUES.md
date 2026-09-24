# 排查报告

排查日期：2026-08-27。范围：SKILL.md、references/*.md（15 个）、scripts/（.sh/.py）、templates、config、CHANGELOG.md。

## 问题清单

| # | 类型 | 位置（文件:行） | 问题描述 | 建议修复 |
|---|------|----------------|----------|----------|
| 1 | 断链（历史条目） | CHANGELOG.md:631 | `references/08-workbuddy-cli-worker.md` 文件不存在（正确名 `08-codebuddy-cli-worker.md`） | 属 v1.x 历史条目。建议保留原文（历史记录原则），或统一批量订正为现名并注明"编号顺移后订正" |
| 2 | 断链（历史条目） | CHANGELOG.md:756 | 同上，`references/08-workbuddy-cli-worker.md` 第二处 | 同 #1 |
| 3 | 章节号过期（历史条目） | CHANGELOG.md:631 | 引用 `§14`，但现 08 文档最高章节为 §13（后续重构移除/合并了 §14） | 历史条目，保留；如订正需同步核对 §10/§14 现对应内容 |
| 4 | 断链（旧编号，历史条目） | CHANGELOG.md:506 | `09-parallel-lessons.md` 不存在（编号顺移后现为 `10-parallel-lessons.md`） | 历史条目，建议保留原文；若 PM 决定统一订正，改为 `10-parallel-lessons.md（原 09）` |
| 5 | 断链（旧编号，历史条目） | CHANGELOG.md:550 | `11-issue-grouping.md` 不存在（现为 `12-issue-grouping.md`） | 同 #4 |
| 6 | 文档缺失（观察项） | skill 根目录 | 无 `DECISIONS.md` / `TASKS.md`，但 CHANGELOG 多处引用 `[DEC-033]`、`DEC-037`、`DEC-042` 等 skill 侧决策编号，无处落地；WORKER_PROMPT 预期 DECISIONS 内约 3 处 workbuddy 断链实际不存在 | 补建 DECISIONS.md 或确认决策记录迁移至项目侧，CHANGELOG 中决策编号注明出处 |

说明：CHANGELOG.md:389 出现的 `08-qodebuddy-cli-worker.md` 是该条目在**描述** qodebuddy→CodeBuddy 拼写订正本身（`旧名 → 新名`），非断链，不列为问题。

## 已核对无问题的项

- **文件引用存在性**：SKILL.md（17 处）、references/04:161、references/06:614、scripts/（check-dependencies.sh:272/276、claude-provider-env.sh:308、sentinel.sh:10、spawn-worker-deps.sh:13、spawn-worker.sh:116、zcode-worker-driver.py:4/89/100）引用的 `references/NN-xxx.md` 全部命中实际文件，编号顺移后的替换无遗漏、无误改
- **短引用语义**：ref 07=qoderwork（qoderclicn-interactive-spawn.sh:20/45、render-runtime-profile.sh:486、scope-guard.py:8、spawn-worker.sh:981）、ref 08=codebuddy（render-runtime-profile.sh:447、scope-guard.py:9、spawn-worker.sh:981、references/07:13）、ref 09=zcode（SKILL.md:366、references/06:23、render-runtime-profile.sh:522）——全部与上下文主题一致
- **章节引用（抽查 10 处全部匹配）**：
  - SKILL.md:366 `ref 09 §2` → 09 §2「二进制位置与首次配置」✓（凭证上下文）
  - references/07:13 `ref 08 §5` → 08 §5「MCP 工具链集成」✓（MCP 付费 API 上下文）
  - SKILL.md:248 `references/15 §8` → 15 §8「守夜模式」✓（night-watch 上下文）
  - scripts/check-dependencies.sh:272/276、zcode-worker-driver.py:89/100 `09 §2` ✓
  - qoderclicn-interactive-spawn.sh:20/45 `ref 07 §6.2` → §6.2「交互式模式（可人工接管）」✓
  - render-runtime-profile.sh:486 `ref 07 §5.1` → §5.1「SDK 环境变量冲突」✓
  - render-runtime-profile.sh:522 `ref 09 §6` → §6「tmux Worker 启动（driver 模式）」✓
  - scope-guard.py:8-9 `ref 07 §9.3`（PreToolUse hook unbypassable）/ `ref 08 §12.3`（PreToolUse hook）✓；spawn-worker.sh:981 `ref 07 §9` / `ref 08 §12` ✓
- **孤儿文件**：references/ 下 15 个文件均被 ≥2 个其他文件引用，无孤儿
- CHANGELOG.md:11/46/52/67/150/176/289/311/326/335/388/390/420/504/530/543/589/642/67/755/783/813/828/840/888/945/960 等其余 `references/NN` 引用均指向现存文件

## PM 验收后修复

- **修复 #4**：CHANGELOG.md:506 `09-parallel-lessons.md` → `10-parallel-lessons.md`
- **修复 #5**：CHANGELOG.md:550 `11-issue-grouping.md` → `12-issue-grouping.md`
- **确认**：grep 全文（含无 `references/` 前缀形式）已无任何旧编号文件名残留：`09-parallel-lessons`、`10-agent-teams`、`11-issue-grouping`、`12-orca-cli`、`13-pm-orchestrate`、`14-wave-autopilot` 均 0 命中
- **diff 摘要**：仅 `skills/multi-agent-orchestration/CHANGELOG.md` 2 处替换（2 insertions / 2 deletions），其余文件未动
