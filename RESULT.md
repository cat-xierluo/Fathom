# 0.4.0 发版文档对账交付报告（README / DESIGN / ARCHITECTURE / TESTING）

基线 main `7b280e9`（0.4.0 候选，版本已统一）。CHANGELOG 已定版，仅作只读参照，未改。本轮只改四个文档，不碰 CHANGELOG/TASKS/代码。

## 逐文档改动摘要

### 1. README.md（用户能使用什么）

- **功能区新增四条**：扫描范围可配置（设置页分区 / 首次引导 / 多范围一轮 / 部分成功明细，未启用时旧行为逐字节不变）；整盘总览与「排查这次变化」（有符号差额，负值口径写明）；快照选项按数据集收敛（基准侧全量作切换入口，历史点侧收敛，单快照数据集保留出口）；跨页排查上下文按会话保持。
- **已知限制新增三条**：范围配置生效路径（Web 设置页/API/CLI；桌面壳不读范围配置驱动扫描，ISS-174 核实结论）；新 plan 身份数据的 AI 解读仍受 `dataset_mismatch` 闸门保护；发布形态为未签名 arm64、Intel 无桌面形态。同时在超大目录树一条补 93MB 索引体积代价。
- **故障处理新增四行**：范围改了不生效、一轮部分范围没采到、变化页选不出组合、整盘差额为负。
- **版本同步**：应用内更新说明补 v0.4.0 未签名 arm64 候选形态；版本单一源经核对为 `fathom/__init__.py:10` = `0.4.0`（README 无硬编码版本号徽章，平台徽章与 arm64 事实一致，无需改）。

### 2. docs/DESIGN.md（页面职责与交互合同）

- 总览页表格行补入整盘三区块（整盘容量读数 / 目录归因 / 未知差额）与「排查这次变化」动作。
- 新增「总览整盘三区块合同（ISS-157/158）」：三区块口径互相独立、共享 free 只计一次、归因只用互不重叠测量根；**未知差额为有符号差值，负值新表述**为「目录测量增长大于容器占用增长」，不得写成「容器占用减少」；不可比写 reason 不补 0；失败成员标 stale；整轮跨时间不压成单一时点；无覆盖率字段。
- 新增「变化页数据集收敛合同（ISS-170/171）」：不对称收敛（b 侧全列为跨数据集唯一入口、a 侧收敛到 b 数据集）、身份两档（plan 档按 plan_id、legacy 档按三元组）、单快照数据集出路、跨页会话恢复合同。
- **明确标注未实现部分保持原状**：范围配置在桌面壳侧的行为接线未发生（ISS-174）；plan 身份数据未打通 AI `dataset_mismatch` 闸门；未签名 arm64 形态。

### 3. docs/ARCHITECTURE.md（已实现模块/数据流/接口）

每条「已实现」均以 `7b280e9` 代码为锚（文件:行号或模块级）：

- **config.py**：`storage_scope`（:454）、`ScopeSelection`（:507）、fail-closed 校验（:553-572、:577）、`_SETTINGS_LOCK` 可重入（:391）、wire 落盘（:476）；并写明 ISS-174 边界（壳不读范围配置驱动扫描，lib.rs 仅形态读取接缝）。
- **db.py**：`SCHEMA_VERSION=8`（:20）、v8 五表（:231-271/:358-384）、snapshots 三可空列（:208-210）、`idx_entries_path`（:31）、**connect 快路径索引让步三段式**（:40-49、:616、:917，只对锁竞争让步、其它 `OperationalError` 原样上抛），含 93MB/百万档空间备注。
- **scan_coordinator.py**：`ScopeSpec`（:106）/`RoundMember`（:169）/`RoundPlan`（:188）、成员六态（:71-78）、skipped 非 failed（:771-791）、容量样本（:501-526）、整轮时限（:597/:680、:864-865）、ISS-175 取消合同。
- **api.py**：**plan_id 两档身份口径**（ISS-176，`_reject_mixed_plan_identity` :464、错误码 :453、窄行按 legacy :456）、`reports.same_dataset` 贯通四消费者（:585/:650/:768/bigfiles）、`/api/snapshots` 下发 plan_id（:298）、`/api/volume-trend` 限 legacy（:333/:342）。
- **SQLite 事实表**：schema 行由过时的 `user_version=6` 更正为 8，并补五表与 snapshots 身份列语义。
- **HTTP 接口表**：`/api/snapshots` 补 plan_id 下发；新增 `/api/storage/summary`（:1347，ISS-157，只读已落库事实、共享 free 只计一次、有符号 unknown 差额、不可比为 null）。
- **版本单一源**：`0.3.0` → `0.4.0`（`fathom/__init__.py:10`）。
- **验证覆盖**：新增 0.4.0 门禁口径段落（11 个 EXPECTED 家族数字 + 权威来源 + 新增覆盖清单）。

### 4. docs/TESTING.md（隔离验证方法）

- §1 期望值段改写为 0.4.0 口径：pytest 1492 / 浏览器 39 / refresh 217 / 分析 77 / Rust 71，并补六个前端真实入口家族（TREE 74、DIR_BIGFILES 36、BROWSE_SNAPSHOT 33、SCOPE_SETTINGS 37、STORAGE_OVERVIEW 59、INVESTIGATION 55）及对应脚本名。
- 补 0.4.0 新增测试族说明（v8 迁移与身份隔离、范围配置 fail-closed、多范围协调、整盘摘要口径、索引 EXPLAIN 反例与写锁让步、plan 身份两档 HTTP 14 例、取消合同交错）。
- CI 段与 §1.1 本地替代链的数字同步为 1492 / 71 / 217。
- 真实入口复跑方法不变（未改任何命令与环境隔离要求）。

## 门禁数字核对（红线项）

`EXPECTED_*` 的权威来源是 `.github/workflows/ci.yml:87-101`（CI env 注入），**不是脚本内默认值**：

| 家族 | 值 | 权威位置 |
|---|---|---|
| PYTEST | 1492 | ci.yml:87 |
| BROWSER | 39 | ci.yml:88 |
| REFRESH | 217 | ci.yml:89 |
| ANALYSIS | 77 | ci.yml:90 |
| TREE | 74 | ci.yml:92 |
| DIR_BIGFILES | 36 | ci.yml:94 |
| BROWSE_SNAPSHOT | 33 | ci_browser_checks.sh:200（默认） |
| SCOPE_SETTINGS | 37 | ci_browser_checks.sh:213（默认） |
| STORAGE_OVERVIEW | 59 | ci.yml:98 |
| INVESTIGATION | 55 | ci.yml:99 |
| CARGO | 71 | ci.yml:100 |

**须提请 PM 注意的一处漂移**：`scripts/ci_pytest.sh:99` 默认 1463、`scripts/ci_cargo_locked.sh:38` 默认 67，均低于 CI env 的 1492 / 71。本地不带 env 直接跑这两个脚本会判红。已按「以 CI env 为权威」写入 TESTING.md 并显式记录该差异，**未改脚本**（脚本不在写域）。建议 PM 择机把脚本默认值对齐，避免本地误判。

## 保留的 NOT_VERIFIED / 未实现清单

1. **helper 侧范围行为接线未发生**（ISS-174）：桌面壳不读范围配置驱动扫描；lib.rs `storage_scope_setting` 仅形态容忍读取。README 限制、DESIGN 未实现标注、ARCHITECTURE config.py 行三处一致保留。
2. **AI 解读 `dataset_mismatch` 闸门未打通**：plan 身份数据仍受保护，范围能力未解除该闸门。
3. **发行形态**：未签名、未公证、仅 arm64；Developer ID 签名/公证/stapling 按 DEC-022 延期，未通过。
4. **0.4.0 CI 冷环境首跑 / 真实受控卷与整盘 plan 数据**：`NOT_VERIFIED`（归 ISS-161/163），本轮未实跑全量门禁，仅完成代码锚点与门禁数字核对。
5. **Tauri WebView 实机走查**（跨页会话恢复、移动布局未支持、系统通知实际展示、tray 菜单与 18pt 可辨性）：维持原有 NOT_VERIFIED，未因本轮文档更新改写。
6. **db.connect 索引让步的边界**：「打开一次即建成」不保证（待办索引在扫描写入期间会再次让步），已在 ARCHITECTURE 局限列写明。
7. **未兑现的旧标注一律保留**：真实 HTTP E2E 闭环（ISS-161/163）、发行后台计划语义（ISS-010）、双架构冻结（ISS-041）、范围能力在发行账户的接线，均未被改写为已完成。

## 写域与提交

- 已提交：`docs/DESIGN.md`、`docs/ARCHITECTURE.md`、`docs/TESTING.md`（一笔 docs 提交）。
- 未提交（执行器限制）：`README.md` 在仓库根、不在 allowed_paths 内，**改动已落盘但留给 PM 验收后代为提交**——与本仓库 RESULT.md 的既有惯例一致。
- 未 push（按约定由 PM 代推）。CHANGELOG.md、TASKS 与任何代码文件均未改动。
