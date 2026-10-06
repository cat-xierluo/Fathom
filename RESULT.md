# ISS-171 + 容量文案交付报告（worker 交付 + PM 验收修正收口）

基线 `17c9df1`。M Code worker 交付主体实现（未提交由 PM 代收），PM 修正两处后收口。

## 交付

1. **ISS-171 单快照数据集禁用锁死**（changes.js）：`setDiffControlsEnabled` 增 `exitViaDatasetSwitch` 语义——数据集内不足两快照时 **b 侧保持可用**（b 侧全列=数据集切换入口），仅 a 侧禁用+就地说明；重载/返回时解开跨页存续的禁用。用户永远有出路 ✓。
2. **容量负差额文案**（overview.js + storage.py）：负值改「目录测量增长大于容器占用增长（可能因压缩/克隆/共享）」，storage.py `sign_semantics`/注释同步；数值计算零改动。

## 套件

- 新增 `ui-negative-unexplained-not-claimed-as-shrink`（overview 套件，route 替身负差额 -14MB：断言文案不含「容器占用减少」且含新表述）→ 计数 57→**59**（实测对齐，worker 写 58 有误），双处同步（ci.yml + ci_browser_checks.sh）；160 套件 +1 断言 → EXPECTED_INVESTIGATION 49→**50**。
- **PM 修正一**：worker 替身 scope 走 legacy 分支且 round:null 落入 waiting 块（无 headline/unexplained）→ 2×waitForSelector 超时；补 `mode/roots/round/previous_round` 两轮形态后 59/59。
- **PM 修正二**：worker 曾把 160 套件等待改为「净变化行含日期」——renderNetLine 正常态不含日期，已按状态行完成态信号修正（沿 R2 定案）。

## 验证（真实执行）

| 套件 | 结果 |
|---|---|
| verify_storage_investigation_frontend.cjs | 50/50 ×2（另 1 轮 keyboard 单项=ISS-172 既有债） |
| verify_storage_overview_frontend.cjs | 59/59 ×2（修正后复跑再绿） |
| verify_frontend_refresh.cjs | exit 0 |

NOT_VERIFIED：CI 冷环境首跑（本 PR run 即首跑）。
