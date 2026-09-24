# Verification Loop Skill 决策记录

## DEC-001 8 阶段验证 + e2e/真机硬门禁（2026-08-05）

- **决策**：验证门禁 8 阶段（构建 / 类型 / lint / 单测 / **e2e 功能** / **真机** / 安全 / diff），其中 e2e（阶段 5）+ 真机（阶段 6）是 READY 的**硬门禁**。
- **理由**：编译层（1-4）全过 ≠ 功能可用。实测教训：改 reader worker 加载，`typecheck` / `build` / `lint` / 单测全过，实机却「文字层未知」（`textLayerStatus` 卡 unknown）崩——编译层根本抓不到运行时功能问题。e2e + 真机是「编译过 ≠ 功能可用」的唯一解。
- **影响**：完成定义升级——e2e/真机不过 = 未完成（不 PR / 不 merge / 不 release / worker STATUS 不写 done / 不向用户声称「修完」）。

## DEC-002 项目类型分支（2026-08-05）

- **决策**：按项目类型（Tauri 桌面 / Web / 服务 / Skill）选验证命令，不通用一套。
- **理由**：不同类型验证命令本质不同——Tauri 要 `cargo check` + 真机 WKWebView（etv）；Web 要 CI Playwright + build 产物；服务要 staging 请求；Skill 用 skill-lint。通用命令覆盖不深。
- **影响**：skill 跨项目通用（四类分支），每类有精确命令。

## DEC-003 断言深度（功能结果 vs 存在元素）（2026-08-05）

- **决策**：e2e 断言**功能结果**（canvas 像素非空 / 节点文字可见 / textLayerStatus ≠ unknown / 点击后状态变化），**非「存在元素」**。
- **理由**：存在元素 ≠ 功能对——canvas 存在但可能空白、svg 存在但可能无文字、按钮存在但点击可能无效。断言功能结果才能防「伪渲染 / 假成功」。
- **影响**：`references/assertion-depth.md` 指南 + 阶段 5 门禁。

## DEC-004 回归规范（Bug 修复新增复现测试）（2026-08-05）

- **决策**：Bug 修复**必须**新增能复现该 bug 的 e2e/单测。
- **理由**：防回归（修了再坏）。没有复现测试的 bug 修复，下次同样 bug 还会过 typecheck。
- **影响**：完成定义含回归测试；阶段 5 e2e 应含 bug 复现 spec。

## DEC-005 不采信生产者自报 PASS（2026-08-05）

- **决策**：验证报告必须基于**实际命令输出**，不采信 worker / PM 自己「我说过了」。
- **理由**：worker（尤其多 agent 编排）会自报 done/PASS 但实际漏 commit / 漏验证 / 实机崩（实测：多个 worker STATUS 写 done 但 git 未 commit；typecheck 全过但实机功能崩）。
- **影响**：阶段 8 Diff 审查 + 各阶段跑实际命令看输出（skill-lint 同款精神）。
