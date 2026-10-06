# ISS-176 交付报告：新 plan 身份数据贯通基本排查链（worker 交付 + PM 验收收口）

基线 `cfd480d`。M Code worker 交付实现（工程改动全部在写域内；未 commit 由 PM 代收），PM 修正验证与报告后收口。

## 交付（对照上游终审 P1 反例）

1. **四消费者贯通**（同 plan 两快照可比）：`fathom/api.py`（/api/diff、/api/trend、/api/snapshots）、`fathom/bigfiles.py`（范围查询）按 `reports.same_dataset` 两档口径（plan 档按 plan_id、legacy 档三元组）放行/拒绝；「范围能力未落地时一律 409」的占位闸门退役（ISS-155 时代占位，两例旧测试替换为贯通断言）。
2. **/api/snapshots 下发 plan_id**（新身份行非空、legacy null，字段命名随既有响应），前端 datasetKey 升级两档——plan 档不拼三元组（plan_id 已编码根/卷/计量版本/阈值/掩码）。
3. **跨 plan / legacy↔plan 混搭拒绝语义保留**；AI dataset_mismatch 闸门未动。
4. R2 的不对称收敛/意图票据语义零改动。

## 测试

- 新增 `tests/test_plan_identity_consumers.py`（14 例：四端点×三身份组 HTTP 回归）+ `test_scope_config.py`/`test_trend_anchor.py` 增补；**净 +11 新 −2 旧占位（diff 核对），EXPECTED_PYTEST 1472→1481（PM 按净数核定）**。
- 前端 +5 断言（plan 档收敛/混搭拒绝/snapshots 字段）→ EXPECTED_INVESTIGATION 50→**55**（实测 55/55 自洽）。

## 验证（PM 实跑）

- test_plan_identity_consumers：14 passed；test_scope_config+trend_anchor：66 passed
- 160 套件 55/55（exit 0）；先红后绿证据见 worker 会话（占位闸门反例先行）
- NOT_VERIFIED：CI 冷环境首跑（本 PR run）；受控卷/真实整盘 plan 数据（归 161/163）
