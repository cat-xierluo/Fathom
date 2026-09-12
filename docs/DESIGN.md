# Disk Sentinel 前端设计规范

## 定位

数据仪表盘（dashboard），信息密度优先，视觉退到背景层。参考群晖 DSM 的浅色简洁风格。

## 设计令牌

```css
--bg: #f5f6f8          /* 页面底 */
--card: #ffffff        /* 卡片/面板 */
--border: #e4e7ec
--text: #1a2233
--muted: #6b7486       /* 次要文字、提示 */
--primary: #2f6fed     /* 主操作、已用容量折线 */
--danger: #d64545      /* 增长条形、低容量告警 */
--ok: #2e9e5b          /* 缩减条形、剩余容量 */
```

字体：系统栈（-apple-system, PingFang SC）。路径一律等宽字体（ui-monospace / SF Mono / Menlo，12px，`word-break: break-all`）。

## 布局

- 顶栏（sticky）：品牌 + 监控根路径 + 扫描状态徽章 + 「立即扫描」主按钮
- 状态卡行：4 张（磁盘剩余 / 已用 / 快照数 / 最近扫描），剩余 <50GB 或已用 ≥95% 时数值变 `--danger`
- 面板（panel）纵向堆叠，max-width 1180px 居中；每面板 = 标题行（h2 + hint/控件）+ 内容
- 响应式：<900px 状态卡 2 列、图表/表格单列

## 图表规范

| 图表 | 类型 | 配色/规则 |
|------|------|-----------|
| 卷容量趋势 | 双折线 + 区域 | 已用 `--primary`（区域 12% 透明度）、剩余 `--ok` |
| 增长/缩减 | 横向条形 | 增长 `--danger`、缩减 `--ok`；类目轴只显示路径末两段，tooltip 给全路径与前后值 |
| 占用分布 | 旭日图 | nodeClick: rootToNode 下钻；内三环；minAngle 8 防细条文字重叠 |
| 目录趋势 | 折线 + 区域 | `--primary`，联动旭日图点击 |

- 所有 size 轴 label 用 B/KB/MB/GB 自适应格式化（fmtBytes）
- ECharts 实例全局复用（charts 字典），window resize 统一 resize

## 交互

- 「立即扫描」：POST /api/scan；进行中禁用按钮 + 徽章变蓝；每 5s 轮询完成后刷新全部区块
- 快照对比：两个下拉默认「最新 vs 前一」，手动可改
- 表格空态显示"无"或说明文字，不留白块
- 所有用户可控输入（天数/MB 阈值）带 min/max 限制，服务端再校验
