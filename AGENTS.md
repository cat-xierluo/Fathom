# Disk Sentinel（容量哨兵）项目协作指南

## 项目简介

容量哨兵是 macOS 本机的目录容量变化追踪软件，对标群晖「存储空间分析器（Storage Analyzer）」中最有价值、但 Mac 上没有任何成熟开源工具提供的能力——**目录级历史变化**：每日定时扫描磁盘，差分出"哪个文件夹冒出来了"，配合 Web 仪表盘可视化趋势，支撑有地放矢的清理。

技术栈：Python 3.14 + FastAPI + SQLite + 原生 HTML/JS（无构建链）+ ECharts（本地化）+ launchd

> 行为规则（中文优先、决策记录、变更透明）遵循 legal-skills 全局 AGENTS.md 惯例，本文件只补充本项目上下文。

## 基本约定

- 全程使用中文回复与写作
- 遵循 `docs/ROADMAP.md` 路线图驱动开发
- 待办事项记录到 `docs/TASKS.md`（ISS-XXX 编号）
- 重要决策记录到 `docs/DECISIONS.md`（DEC-XXX 编号，含背景/决策/验证/影响）
- 用户可见变更写入 `CHANGELOG.md`
- 涉及前端 UI 时遵循 `docs/DESIGN.md`

## 文件清单

| 文档 | 位置 | 职责 |
|------|------|------|
| README.md | 根目录 | 项目介绍、快速开始、故障排查 |
| CHANGELOG.md | 根目录 | 版本变更记录 |
| main.py | 根目录 | CLI 入口（scan/report/bigfiles/status/serve/install/uninstall） |
| disk_sentinel/ | 根目录 | Python 包：内核与 API |
| frontend/ | 根目录 | 无构建链静态前端（ECharts 已本地化到 vendor/） |
| tests/ | 根目录 | pytest 单元测试 |
| docs/ARCHITECTURE.md | docs/ | 系统架构、数据流、API 清单、DB schema |
| docs/DECISIONS.md | docs/ | 技术决策记录（含为什么不用 duc / 不 fork disktracker） |
| docs/TASKS.md | docs/ | 待办追踪 |
| docs/ROADMAP.md | docs/ | 阶段路线图 |
| docs/DESIGN.md | docs/ | 前端视觉与交互规范 |
| data/ reports/ logs/ | 根目录 | 运行时产物（gitignore，含全盘路径，绝不入库） |

## 模块速查

| 模块 | 职责 |
|------|------|
| `disk_sentinel/config.py` | 全部路径/阈值/端口常量；`DISK_SENTINEL_DB` 环境变量可覆盖数据库位置 |
| `disk_sentinel/scanner.py` | 调 `du -xk` 生成快照；同日覆盖；周/日保留策略清理 |
| `disk_sentinel/reports.py` | 快照差分（父子折叠算法 `fold_changes`）+ Markdown 日报 |
| `disk_sentinel/bigfiles.py` | `find` 近 N 天大文件 |
| `disk_sentinel/api.py` | FastAPI 只读查询 + 手动扫描触发（API 文档自动生成在 `/docs`） |
| `disk_sentinel/launchd.py` | 两个 plist 的生成与安装（每日扫描 + 常驻 Web） |

## 开发命令

```bash
.venv/bin/python -m pytest tests/ -q   # 单元测试
.venv/bin/python main.py status        # 状态总览
.venv/bin/python main.py serve         # 前台启动 Web 服务（开发用）
.venv/bin/python main.py install       # 安装 launchd 任务（部署用）
.venv/bin/python main.py uninstall     # 卸载 launchd 任务
```

冒烟测试不污染生产库：`DISK_SENTINEL_DB=/tmp/x.db .venv/bin/python main.py serve`

## 关键设计决策（详见 docs/DECISIONS.md）

- 扫描引擎用系统 `du -xk`，不引入 duc/自研遍历（DEC-002）
- 快照只持久化 ≥10MB 的目录，数据库增长可控（DEC-005）
- 前端无构建链（无 npm/Vite），ECharts 本地化，AI 维护成本最低（DEC-003）
- 运行时数据（data/reports/logs）gitignore，内含全盘目录路径属敏感信息（DEC-004）
