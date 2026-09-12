# Disk Sentinel 待办追踪

编号规则：ISS-XXX。状态：待处理 / 进行中 / 已完成。

## 待处理

### ISS-001 · 真实基线扫描与首份日报验证（P0）
- 在本机执行首次 `main.py scan`（全量，5-15 分钟），确认 1100 万文件量级下 du 耗时、无权限目录数、数据库实际大小
- 次日 12:00 验证 launchd 定时扫描与首份真实对比日报
- 验收：日报能定位到真实增长目录

### ISS-002 · 完全磁盘访问权限授予（P1）
- launchd 场景下扫描进程为 venv python，需在系统设置中为其授予 FDA，否则 ~/Library 受保护区域（邮件等）持续 denied
- 验收：denied_count 显著下降

### ISS-003 · 扫描通知（P1）
- 每日扫描完成后发 macOS 通知（osascript display notification），内容为"今日增长 Top1 目录 + 剩余空间"
- 异常（剩余 <10GB）时醒目告警

### ISS-004 · 清理建议生成（P2）
- 基于差分结果 + 已知安全清理模式（Xcode DerivedData、npm cache、~/Library/Caches、转码临时目录等）生成建议清单
- 建议仅提示路径与收益，删除动作由用户执行

### ISS-005 · duc 集成作为可选引擎（P3）
- 为需要交互式旭日浏览的场景提供 `duc index` 集成（仅保留最近一份索引库，控制磁盘占用）

### ISS-006 · Tauri 壳（P3）
- 参照 Folia（Tauri v2）包壳指向 http://127.0.0.1:7952，获得原生窗口与 Dock 图标
- 前置条件：v0.1 真实使用反馈稳定

### ISS-007 · scan_runs 表接线（P3）
- api.py 的手动扫描目前用内存态 `_scan_state`，重启丢失；接入 scan_runs 表持久化历史

## 已完成

（暂无）
