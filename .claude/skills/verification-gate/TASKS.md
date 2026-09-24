# Verification Loop Skill 任务

## 当前状态

v1.3.0（2026-09-05）：已定位为“验证执行与证据记录层”，新增
`verification-gate-stage-report/v1` → PEA staged receipt 确定性适配器；16/16、真实临时 Git
repo→adapter→PEA consumer、长输入复杂度探针和独立复核通过。适配器只转换已执行事实，不运行
阶段命令、不裁定 READY/RELEASED。真实项目尚未自动生成 stage report。

## 已完成

- [x] SKILL.md（8 阶段 + 项目类型分支 + 触发 + 完成定义 + 依赖 + 参考文档 + Related Skills）
- [x] references/eight-phases-rationale.md（为什么 8 阶段：e2e + 真机补编译层盲区）
- [x] references/assertion-depth.md（断言深度指南，跨项目案例）
- [x] references/e2e-practice.md（e2e/CI/etv/fixture 案例）
- [x] references/test-pyramid.md（测试金字塔/verify/回归/lint 案例）
- [x] references/lessons-from-practice.md（实践教训反哺：pre-existing 失败判定、e2e 缺失、Tauri invoke 限制等 7 条）
- [x] LICENSE.txt（MIT，与 skill-lint 模板一致）
- [x] CHANGELOG / DECISIONS / TASKS（skill 级初版）
- [x] v1.3.0 staged receipt 适配器、输入样例与 PEA 真实 consumer 回归；首轮 reviewer 阻断
  敏感路径/凭证泄漏，后续两轮阻断平方级 URI 扫描，最终 immutable `e84f4541` ACCEPT，
  legal-skills 集成提交 `b05f5588`。

## 待办

### 优先（验证 skill 有效性）

- [ ] **VG-001｜frontmatter 与当前 Codex validator 对齐（P1 / timeboxed_fix）**：当前 16/16
  行为回归通过，但系统 `quick_validate.py` 拒绝顶层 `author/homepage/version`，只允许这些信息
  进入 `metadata`。在不改变 name/description/触发边界的前提下迁移字段，并同时跑 Codex
  quick_validate、现有 skill-lint 与 16 项 adapter 回归；三者均 exit 0 前不得把 v1.3.0 表述为
  当前 Codex 结构验证已通过。该问题是本轮 TASKS 维护时发现的既有兼容缺口，不与 VG-002 混修。
- [ ] **VG-002｜机器执行/可信导入 stage report（P1）**：当前适配器验证“报告结构可信”，但
  stage report 仍可能由 Agent 事后手填。设计两种明确来源：①项目配置白名单内的命令由有界
  executor 直接运行并捕获 exit/failure/duration/timeout/evidence；②从受保护 CI job 导入绑定
  commit 的结果。不得执行输入报告里的任意命令，也不得把计划执行写成已执行。
  验收：成功、失败、timeout、suite 悬挂、跳过、子进程遗留和伪造 PASS mutation 均有反例；
  executor→adapter→PEA 在临时 Git repo 真实闭环，进程/临时文件可靠收口。
- [ ] **VG-003｜Bank NPL 首个真实 staged receipt（P1，依赖 VG-002）**：复用 Runner 的终态合同
  和代表性 E2E/negative control 生成 stage report，交 PEA 裁决。初期只支持
  `E2E_VERIFIED`；provider/live/release 未授权时列入 unsupported，不启用虚假的 completion hard
  gate。consumer 任务以 canonical `custom-skills` 的 Bank NPL Runner `TASKS.md` 为权威，本条只
  负责通用执行器验收。
- [ ] **FaroPDF UI 专项回归（P2）**：在 VG-002 稳定后，用 8 阶段重新跑 FaroPDF 的 canvas、
  textLayer 与真机路径，校准 Web/Tauri 断言；它是第二技术栈校准，不再表述为 Skill 的第一个
  真实用例。

### 中期（skill 增强）

- [ ] **project-init 集成**：project-init 初始化项目时，按项目类型建 e2e spec + CI playwright job + fixture 目录脚手架（让新项目一出生就有验证体系，不用事后补）。
- [ ] **CI 模板**：提供 `.github/workflows/ci.yml` playwright job 模板（各项目复用，参照 Folia）。
- [ ] **etv 模板**：提供 `scripts/etv-*.mjs`（Tauri 桌面真机验证）模板，仿 Folia etv-folia.mjs。
- [ ] **VG-004｜Skill fresh-context 证据（P2 / validate_first）**：与 `eval-harness` 协作，在无旧
  结论上下文中运行代表性触发并保存脱敏 transcript/产物；verification-gate 只生产 runtime
  evidence，PEA 继续要求人工抽查并裁决。没有两个真实 Skill 样本前不做通用自动评分器。
- [ ] **固定额外 `git rev-parse`（P2 / backlog）**：当前每次 adapter 固定多一次 Git 元数据读取，
  不阻断发布；仅当批量生成回执或实测 latency/IO 可见时合并解析路径，不为理论微优化提前改码。

### 按需（覆盖更多项目类型）

- [ ] Python 项目分支（pytest / mypy / ruff / integration）。
- [ ] Go 项目分支（go test / golangci-lint）。
- [ ] 移动项目分支（iOS XCTest / Android Espresso）——如有需求。

### 持续

- [ ] 每次踩「编译过但实机崩」的坑，回头补本 skill（阶段/断言/项目分支），让坑不重复踩。
