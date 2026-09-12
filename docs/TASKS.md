# Disk Sentinel 待办追踪

编号规则：ISS-XXX。状态：待处理 / 进行中 / 已完成。

## 待处理

### ISS-008 · tray 实测与打磨（P1）
- 用户在菜单栏实际查看：圆环图标显示效果（深浅色菜单栏）、"31 GB"标题推送是否成功（依赖远程页面 invoke，capability 已配）
- 若 title 不刷新：排查 capability 的 remote IPC 权限（前端 console 可见 invoke 错误）
- 验收：菜单栏可见图标+剩余 GB，左键点开窗口，菜单项全部可用

### ISS-009 · .app 打包（P1）
- `cargo tauri build`（或 npx @tauri-apps/cli）生成 .app/.dmg；补 bundle 图标（512 应用图标，可从 tray 圆环风格扩展）
- 安装到 /Applications 或 ~/Applications，启动台/Spotlight 可搜"容量哨兵"
- 验收：不依赖终端即可启动

### ISS-010 · 桌面壳开机自启（P2）
- 壳加入登录项（SMAppService / tauri autostart 插件），开机即菜单栏常驻（后端 launchd 已自启）

### ISS-001 · 真实基线扫描与首份日报验证（P0）· 基线已完成，待次日日报
- 2026-09-12 基线落地：93.7 万目录、无权限仅 6 个（TCC 影响远小于预估，ISS-002 优先级可降）、总量 1610 GB、数据库 3.6 MB / 入库目录 22201 个（DEC-005 容量预估兑现）
- 待办：次日 12:00 验证 launchd 定时扫描与首份真实对比日报
- 验收：日报能定位到真实增长目录

### ISS-002 · 完全磁盘访问权限授予（P3，影响已证实很小）
- launchd 场景下扫描进程为 venv python，需在系统设置中为其授予 FDA，否则 ~/Library 受保护区域（邮件等）持续 denied
- 基线实测仅 6 个目录受限，优先级下调

### ISS-003 · 扫描通知（P1）
- 每日扫描完成后发 macOS 通知（osascript display notification），内容为"今日增长 Top1 目录 + 剩余空间"
- 异常（剩余 <10GB）时醒目告警；tray 菜单也可展示当日摘要

### ISS-004 · 清理建议生成（P2）
- 基于差分结果 + 已知安全清理模式（Xcode DerivedData、npm cache、~/Library/Caches、转码临时目录、微信文件夹说明等）生成建议清单
- 建议仅提示路径与收益，删除动作由用户执行（Badminton Lab 交互规则：破坏性操作必须预览+确认）

### ISS-011 · 文件类型分布（P2，来自群晖功能映射）
- 扫描时按扩展名聚合大小（schema 加聚合表），分布页增加"按类型"视图

### ISS-012 · 重复文件检测（P3，来自群晖功能映射）
- name+size 粗筛 → 大文件 hash 精查；入口放大文件页

### ISS-005 · duc 集成作为可选引擎（P3）
- 为需要交互式旭日浏览的场景提供 `duc index` 集成（仅保留最近一份索引库，控制磁盘占用）

### ISS-013 · treemap 矩形树图（P3，WinDirStat 模式）
- 分布页可选双图模式（旭日/树图），等真实使用反馈再决定

### ISS-007 · scan_runs 表接线（P3）
- api.py 的手动扫描目前用内存态 `_scan_state`，重启丢失；接入 scan_runs 表持久化历史

## 已完成

- ISS-006 · Tauri 桌面壳（v0.2.0，DEC-008）：菜单栏 tray + 原生窗口，五页仪表盘实测渲染正常；后续打磨归 ISS-008/009/010
