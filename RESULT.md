# ISS-170 R2 交付报告：变化页迟到 diff 渲染竞争修复（PM 接手收口）

基线 main `17c9df1`。本卡前半由 M Code worker 执行（复核门+钓鱼框架），因两轮未 commit/未写报告由 PM 接手完成诊断与收口；以下为完整证据链。

## 一、根因（PM 诊断定案，网络监听+DOM trace 双重实证）

**CI 反例**（main run 37450199526）：`journey-history-non-latest` 选 #1→#2，净变化行渲染 `9.5 GB → 12.4 GB`（#4 的读数）。

**请求序铁证**（Playwright request/response 监听）：selectOption(b=2) 前后出现三连发
`(1,4) → (1,2) → (1,4)`——最后一发以**最新世代**作废用户自己的 (1,2)，其响应又渲染旧意图读数（R1 无复核门时直接渲染 12.4；worker 加复核门后双双丢弃变 TIMEOUT）。

**三连发的来源链**：
1. `loadSnapshotsForDiff` 的迟到二发（enter/restore/rescan 流程）：fetch 期间用户改选后，旧逻辑仅切换「取发起时值/实时值」，**仍按旧意图收敛落定并以最新世代兜底 loadDiff**；
2. 其内部「先写 sel 再比对」的守卫必然自我满足（写后读相等），拦不住自己；
3. 总览交接 `_handOffChangeEntry` 的 8s tick 迟到写入同样覆盖用户选择。

## 二、修复（frontend/modules/pages/changes.js + overview.js）

1. **意图票据**：`diffIntentAB` 只由两个合法发起方设置（onSelectionChange / 目录收敛落定），loadDiff 门口校验，不符即拒不发请求——幽灵调用零成本拒绝。
2. **revision 放弃**：`/api/snapshots` fetch 期间用户改选（revision 推进）→ 二发整体放弃（不收敛、不落定、不发），只重建 b 侧全列数据。
3. **渲染复核门**（worker 版保留）：响应到达时 sel 值 ≠ 发起值 → 丢弃。
4. **交接放弃**（overview.js）：用户改选后交接 tick 放弃写入（isTrusted 置标志；合成事件下不触发，真实用户场景生效）。

## 三、验证

- **套件等待条件修正**：worker 曾把等待改为「净变化行含两日期」——renderNetLine 正常态**不含日期**（读 renderNetLine 源码证实），该条件永不满足，是套件缺陷。改为「状态行完成态 `#a → #b`」为切档信号 + 原读数断言把关。
- 修复后 160 套件 8 轮：**49/49 ×5**，另 3 轮仅 `journey-keyboard-expand-esc-focus-return` 单项挂（见下）。`journey-history-non-latest` 8/8 全过（基线必挂）。
- 树形 74/74 ✓；refresh 217 ✓（exit 0）。
- **对照实验**：干净 `17c9df1` 基线三轮**全部**挂 keyboard 同一项——该 flaky 为 main 既有债，与本卡改动无关，登记 ISS-172（ISS-169 B 组同族：焦点恢复等待/选择器类）。
- PM 断言核对：STATE dump `a=1 b=2 status=已对比快照 #1 → #2`、NETLINE `9.5 GB → 10.5 GB`——渲染与读数正确。

## 四、NOT_VERIFIED / 后继

- CI 冷环境首跑（本 PR 的 run 即首跑）；keyboard flaky 的修复归 ISS-172。
- 新 plan 身份档（plan_id）前端收敛边界与 R1 相同（后端闸门兜底），归上游终审 P1 项的新 plan 消费链卡统一处理。
