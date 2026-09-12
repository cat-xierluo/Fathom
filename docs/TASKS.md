# Fathom 待办追踪

编号规则：ISS-XXX（编号一经使用不再变更或复用，保证历史引用不断链）。
状态：待处理 / 进行中（附分支名）/ 已完成（附版本）。

## 任务推进规范（自动化执行入口，新会话先读本节）

1. **认领**：从下方索引表选「阶段 ≤ 当前阶段（v0.3.x）」且「前置全部已完成」的任务，按 P0 > P1 > P2 > P3、同优先级取编号最小。无符合条件任务时停下问用户，不自造任务、不提前做未来阶段。
2. **建分支**：`git switch -c iss-NNN-slug`（slug 用英文短横线，如 `iss-009-app-bundle`）。单分支单任务，不混入无关改动（文档回写除外）。仓库无远端，合并即本地 merge。
3. **实现**：约定见 AGENTS.md；UI 遵循 docs/DESIGN.md 合同（合同缺项先补合同再实现）；与既有 DEC 冲突时先记新 DEC-XXX 再动手。
4. **验证**：通用回归 + 任务「验证」栏全部通过才算完成。
   - 后端/共享代码：`.venv/bin/python -m pytest tests/ -q`
   - 前端：`FATHOM_DB=/tmp/x.db .venv/bin/python main.py serve` 冒烟后过相关页面
   - 桌面壳：`cd apps/desktop/src-tauri && cargo build`
5. **回写**（同一分支内完成）：本文件状态与验收框、CHANGELOG.md（用户可见变更）、必要时 DECISIONS.md。
6. **合并**：回报结果与验证证据，用户确认后合并回 main；主干始终可发布。

## 依赖与并行（轨道间互不阻塞，可多分支并行）

```
观察轨（无代码）   ISS-001 次日报验证 ──► 结论回写（校准 DEC-005/006 预估）
                  ISS-002 FDA 授权（人工，随时可做）
桌面壳轨（串行）   ISS-008 tray 实测 → ISS-009 .app 打包 → ISS-010 开机自启
后端轨（可并行）   ISS-003 扫描通知 ／ ISS-007 scan_runs（小，宜早）
                  ISS-011 类型分布（动 schema，待 ISS-007 合并后开工）
功能轨（v0.4+）    ISS-004 清理建议 ／ ISS-012 重复文件 ／ ISS-015 周报月报
                  ISS-016 设置可写 ／ ISS-013 treemap ／ ISS-014 窄屏 ／ ISS-005 duc
```

- ISS-007 与 ISS-011 都改 `fathom/db.py` schema，串行避免迁移冲突。
- ISS-003/004/011/012 的前端部分都改 `frontend/app.js`：并行时先后端后前端，或错开页面。

## 任务索引

| 编号 | 任务 | 优先级 | 轨道 | 前置 | 阶段 |
|------|------|--------|------|------|------|
| ISS-001 | 次日真实日报验证 | P0 | 观察 | — | v0.3.x |
| ISS-008 | tray 实测与打磨 | P1 | 壳 | — | v0.3.x |
| ISS-009 | .app 打包 | P1 | 壳 | ISS-008 | v0.3.x |
| ISS-003 | 扫描通知 | P1 | 后端 | — | v0.3.x |
| ISS-010 | 开机自启 | P2 | 壳 | ISS-009 | v0.3.x |
| ISS-007 | scan_runs 接线 | P3 | 后端 | — | v0.3.x（小任务宜早） |
| ISS-004 | 清理建议生成 | P2 | 功能 | ISS-001 | v0.4 |
| ISS-011 | 文件类型分布 | P2 | 后端+功能 | ISS-007 | v0.4 |
| ISS-012 | 重复文件检测 | P3 | 功能 | — | v0.4 |
| ISS-015 | 周报/月报聚合 | P3 | 功能 | ISS-001 + 数据积累 | v0.4 |
| ISS-016 | 设置页可写配置 | P2 | 功能 | — | v1.0 |
| ISS-013 | treemap 双图 | P3 | 功能 | 真实使用反馈 | v1.0 |
| ISS-014 | 窄屏适配 | P3 | 功能 | — | v1.0 |
| ISS-005 | duc 可选引擎 | P3 | 后端 | — | v1.0 |
| ISS-002 | FDA 完全磁盘访问 | P3 | 观察（人工） | — | 随时 |

## 任务详情

字段合同：目标一句话可判定；范围列出预计改动文件（实现前先核对存在性）；验收框是完成的唯一标准，全部勾选才算完成。

### ISS-001 · 次日真实日报验证（P0 · 观察轨 · v0.3.x）

- **目标**：验证 launchd 定时扫描与首份真实差分日报端到端可用
- **前置**：无（基线 2026-09-12 已建立：93.7 万目录 / 1610 GB / 库 3.6MB / 无权限仅 6 个）
- **范围**：无代码；只读验证 + 结论回写（du 实际耗时 / denied 分布 / 库增速，校准 DEC-005/DEC-006 预估）
- **要点**：
  - 次日 12:00 后查 `launchctl list | grep fathom` 与 `logs/launchd-scan.err.log`
  - 快照数 ≥2 且日期不同（同日覆盖语义）；`reports/` 出现新日期的日报
  - 日报 Top 增长非全零，能指向真实目录
- **验证**：
  ```bash
  .venv/bin/python main.py status
  .venv/bin/python main.py report | head -40
  ls -la reports/
  ```
- **验收**：
  - [ ] 定时任务自动执行（非手动触发），err.log 无致命错误
  - [ ] 快照 ≥2（不同日期），首份日报生成且增长/新增/消失非全零
  - [ ] 观察结论（耗时 / denied / 库增速）回写本条目，必要时校准 DEC-005 阈值
- **分支**：无（观察任务，结论直接回写 main）

### ISS-008 · tray 实测与打磨（P1 · 壳轨 · v0.3.x）

- **目标**：菜单栏 tray 图标 + 剩余 GB 标题在真实菜单栏稳定可用
- **前置**：无
- **范围**：`apps/desktop/src-tauri/`（tray 事件/菜单/capabilities）、`frontend/app.js`（心跳推送）
- **要点**：
  - 深浅色菜单栏下 template 圆环图标可见性；不可见则调线宽/留白后重生成（scripts/make_tray_icon.py）
  - 标题链路：前端心跳 invoke `update_tray_status` → 菜单栏显示剩余 GB；不刷新时查 capability 的 remote IPC 权限（前端 console 的 invoke 错误）
  - tray 菜单四项（状态行/打开主界面/立即扫描/退出）逐项实测
- **验证**：`cd apps/desktop/src-tauri && cargo run` 菜单栏实测，截图留档
- **验收**：
  - [ ] 深浅色菜单栏图标均清晰
  - [ ] 剩余 GB 与 `/api/status` 一致，扫描完成后自动刷新
  - [ ] 左键开窗、菜单四项全部可用
- **分支**：`iss-008-tray-polish`

### ISS-009 · .app 打包（P1 · 壳轨 · v0.3.x）

- **目标**：产出不依赖终端的 Fathom.app，启动台/Spotlight 可启动
- **前置**：ISS-008（tray 图标与行为定型后再打包，避免返工）
- **范围**：`apps/desktop/src-tauri/tauri.conf.json`（bundle 段）、`apps/desktop/src-tauri/icons/`、`scripts/`（应用图标生成）
- **要点**：
  - `cargo tauri build`（或 `npx @tauri-apps/cli build`）出 .app/.dmg；identifier 已是 com.maoscripts.fathom
  - 512 应用图标从 tray 圆环/测深锚风格扩展（复用 make_tray_icon.py 的纯 stdlib PNG 编码思路）
  - .app 与后端 launchd 解耦：未装 launchd 时 loader 页应有安装指引（兜底已存在，验证即可）
- **验证**：产物拷入 /Applications，脱离 cargo/终端启动实测
- **验收**：
  - [ ] 启动台/Spotlight 搜 "Fathom" 可启动
  - [ ] 关窗=隐藏、退出走 tray，全程不开终端
  - [ ] 后端未启动时窗口显示安装指引而非白屏
- **分支**：`iss-009-app-bundle`

### ISS-010 · 开机自启（P2 · 壳轨 · v0.3.x）

- **目标**：开机登录即完整形态（壳菜单栏常驻；后端 launchd 已自启）
- **前置**：ISS-009（有 .app 才有登录项）
- **范围**：`apps/desktop/`（tauri-plugin-autostart 或 SMAppService）
- **要点**：注册/注销入口放 tray 菜单；默认策略（默认开 vs 用户手动开）实现前问用户
- **验证**：注销重登/重启后菜单栏自动出现图标
- **验收**：
  - [ ] 登录后菜单栏自动出现 Fathom，无需手动打开
  - [ ] 可从 tray 菜单关闭自启
- **分支**：`iss-010-autostart`

### ISS-003 · 扫描通知（P1 · 后端轨 · v0.3.x）

- **目标**：每日扫描完成后弹 macOS 通知；低容量醒目告警
- **前置**：无（开发与验证用手动扫描，不依赖定时链路；上线后的每日通知观察随 ISS-001 一并验证，避免通知掩盖定时故障）
- **范围**：`fathom/scanner.py` 或 `main.py` scan 收尾钩子；不动 API/前端
- **要点**：
  - `osascript display notification`，正文 = 今日 Top1 增长目录 + 剩余 GB
  - 剩余低于告警阈值（现 10GB，取自 config）时带声音告警
  - 通知失败绝不阻断扫描（try/except + logs 记录）；LaunchAgent 同用户会话可直接弹，若被系统策略拦截则记录现象并给授权指引
- **验证**：`.venv/bin/python main.py scan` 手动触发应弹通知；pytest 回归
- **验收**：
  - [ ] 扫描落库后 3 秒内收到通知，内容含 Top1 增长与剩余
  - [ ] 低剩余场景（临时调阈值模拟）通知带告警标识
  - [ ] 通知异常时快照与日报不受影响
- **分支**：`iss-003-scan-notification`

### ISS-007 · scan_runs 表接线（P3 · 后端轨 · v0.3.x，小任务宜早）

- **目标**：手动扫描状态持久化（现 `api.py` 内存态 `_scan_state` 重启即丢）
- **前置**：无（`fathom/db.py:41` scan_runs 表已预留）
- **范围**：`fathom/api.py`（/api/scan 与 /api/scan/status 读写表）、必要时 `fathom/db.py`
- **要点**：CREATE TABLE IF NOT EXISTS 兼容旧库；开始/结束/失败三态写表；/api/status 的扫描状态改查表
- **验证**：pytest 新增三态转换用例 + 冒烟 curl /api/scan/status
- **验收**：
  - [ ] 扫描进行中重启服务，/api/scan/status 仍能如实报告（或超时收尾）
  - [ ] 历次扫描（含失败）可查；旧库无损升级
- **分支**：`iss-007-scan-runs`
- **注**：ISS-011 同样动 schema，本任务先合并可错峰

### ISS-004 · 清理建议生成（P2 · 功能轨 · v0.4）

- **目标**：差分 × 已知安全清理模式 → 按收益排序的建议清单（只提示，不代删）
- **前置**：ISS-001（用真实差分数据校准模式库）
- **范围**：新 `fathom/advisor.py` + `/api/advice` + 总览页建议区块；**先补 DESIGN.md 总览页合同再实现**
- **要点**：
  - 模式库：Xcode DerivedData、npm/pnpm cache、~/Library/Caches、下载/转码临时目录、微信等大文件夹说明（路径模板 + 风险说明 + 当前大小）
  - 每条建议含"为什么安全/需谨慎"；删除动作一律用户执行（Badminton Lab 规则：破坏性操作必须预览+确认）
- **验证**：pytest（模式匹配/大小聚合/排序）+ curl /api/advice + 冒烟过总览页
- **验收**：
  - [ ] 本机真实数据给出 ≥3 条建议，大小与 du 口径一致
  - [ ] 总览区块空态/有数据两态齐全（DESIGN 状态全覆盖）
- **分支**：`iss-004-cleanup-advice`

### ISS-011 · 文件类型分布（P2 · 后端+功能 · v0.4）

- **目标**：按扩展名聚合占用，分布页增加"按类型"视图（对齐群晖）
- **前置**：ISS-007（schema 错峰）；采集策略参考 ISS-001 的耗时结论
- **范围**：`fathom/`（采集器 + 聚合表）、`fathom/api.py`、分布页前端
- **要点**：
  - **动手前先记新 DEC**：du 只有目录级数据，类型聚合需文件级遍历（find/stat 约 1100 万文件的耗时与库增速先评估，可采样推算）
  - 同分支三步顺序提交：a) DEC + 采集器与聚合表；b) /api/type-distribution；c) 分布页"目录/类型"双视图（DESIGN 合同先行）
- **验证**：pytest（聚合正确性/口径一致）+ 冒烟双视图切换
- **验收**：
  - [ ] 类型聚合总和与快照总量的偏差可解释（未统计项显式列出）
  - [ ] 分布页双视图切换可用，含空态
- **分支**：`iss-011-type-distribution`

### ISS-012 · 重复文件检测（P3 · 功能轨 · v0.4）

- **目标**：name+size 粗筛 → 大文件 hash 精查，定位重复占用
- **前置**：无
- **范围**：`fathom/bigfiles.py` 引擎扩展 + API + 大文件页入口
- **要点**：hash 只对粗筛命中集做（控 IO）；结果给说明不代删
- **验证**：pytest + 构造重复文件目录实测
- **验收**：
  - [ ] 能列出真实重复大文件组，路径可 Finder 打开
- **分支**：`iss-012-duplicates`

### ISS-015 · 周报/月报聚合（P3 · 功能轨 · v0.4，自 ROADMAP 立项）

- **目标**：周 Top 增长 / 月度趋势摘要，报告不止日粒度
- **前置**：ISS-001；数据积累 ≥2 周再做才有意义
- **范围**：`fathom/reports.py` + 变化页档案筛选
- **验收**：
  - [ ] 能生成某周摘要（Top 增长/缩减 + 周净变化），档案页可按周/月过滤
- **分支**：`iss-015-weekly-report`

### ISS-016 · 设置页可写配置（P2 · 功能轨 · v1.0，自 ROADMAP 立项）

- **目标**：扫描根/扫描时间/阈值可在设置页修改（当前只读）
- **前置**：无
- **范围**：配置持久化（覆盖 `fathom/config.py` 默认值）+ API + 设置页表单；DESIGN 合同先行
- **要点**：写配置后同步重载 launchd plist；危险项（扫描根）需二次确认
- **验收**：
  - [ ] 改扫描时间后 launchctl 下次触发时间随之变化；页面有保存反馈
- **分支**：`iss-016-settings`

### ISS-013 · treemap 双图（P3 · 功能轨 · v1.0）

- 分布页可选双图（旭日/树图，WinDirStat 模式）；等真实使用反馈再决定；ECharts treemap 本地已有，成本低
- **分支**：`iss-013-treemap`

### ISS-014 · 窄屏适配（P3 · 功能轨 · v1.0）

- <980px 侧边导航折叠为顶栏横向 tab（DESIGN.md 信息架构注记）；当前明确不支持窄屏，先保证桌面
- **分支**：`iss-014-narrow`

### ISS-005 · duc 可选引擎（P3 · 后端轨 · v1.0）

- `duc index` 集成交互式终端浏览；仅保留最近一份索引库（控盘）；与主数据链互不依赖（DEC-002 边界）
- **分支**：`iss-005-duc`

### ISS-002 · FDA 完全磁盘访问授权（P3 · 观察轨 · 人工）

- 基线实测仅 6 目录受限，影响小；如需完整覆盖：系统设置 → 隐私与安全性 → 完全磁盘访问 → 为 launchd 场景的 venv python 授权
- **验收**：
  - [ ] denied_count 归零，或明确决定不值得授权后关闭本条

## 已完成

- ISS-006 · Tauri 桌面壳（v0.2.0，DEC-008）：菜单栏 tray + 原生窗口，五页仪表盘实测渲染正常；后续打磨归 ISS-008/009/010
