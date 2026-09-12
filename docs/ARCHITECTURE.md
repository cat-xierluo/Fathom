# Fathom 架构文档

## 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 扫描引擎 | 系统 `du -xk`（BSD） | C 级性能，输出天然是"目录→累计大小"；`-x` 不跨挂载点 |
| 大文件引擎 | 系统 `find -xdev` | 按 mtime 近似"新增/近期写入" |
| 数据层 | SQLite（stdlib sqlite3） | WAL 模式，单文件 `data/fathom.db` |
| 服务层 | FastAPI + uvicorn | 只读查询 + 手动扫描触发；自动 OpenAPI 文档 |
| 前端 | 原生 HTML/JS/CSS + ECharts 5（本地化） | 无构建链，无 npm 依赖 |
| 调度 | launchd（两个 LaunchAgent） | 每日扫描 + 常驻 Web |

## 系统架构

```
┌─────────────────────────── launchd ───────────────────────────┐
│                                                               │
│  com.maoscripts.fathom-scan      每日 12:00            │
│    └─ main.py scan                                            │
│         ├─ du -xk $HOME ──► 解析（含八进制转义还原）            │
│         ├─ 快照写入 SQLite（entries ≥10MB；同日覆盖）           │
│         ├─ 差分 → reports/YYYY-MM-DD.md                       │
│         └─ prune（35 日 + 12 周）                              │
│                                                               │
│  com.maoscripts.fathom-web       常驻 KeepAlive        │
│    └─ main.py serve ──► FastAPI :7952                          │
└───────────────────────────────────────────────────────────────┘
                 │
                 ▼
        浏览器 http://127.0.0.1:7952
        （状态卡 / 卷趋势 / 快照对比 / 旭日图 / 大文件）
```

## 数据流

```
du -xk ~ ──stdout──► scanner.run_du() ──dict[path, size_kb]──► 过滤 ≥10MB
  ──► INSERT snapshots + entries + volume_stats（事务）
  ──► reports.compute_diff(old_entries, new_entries)
         ├─ grown / shrunk：按 |delta| 排序 + fold_changes 父子折叠
         ├─ added：new 有 old 无（≥100MB）
         └─ removed：old 有 new 无
  ──► render_markdown() ──► reports/YYYY-MM-DD.md
```

## 数据库 Schema

```sql
snapshots(id, created_at, root, dir_count, denied_count, du_seconds, total_kb)
entries(snapshot_id, path, size_kb)          -- 主键 (snapshot_id, path)，WITHOUT ROWID
volume_stats(snapshot_id, total_bytes, free_bytes)
scan_runs(id, started_at, finished_at, status, message)   -- 预留：手动扫描历史
```

容量估算：每快照仅数千行（≥10MB 目录），单行 ~60B；90 天保留约 10-50MB，对剩余 23GB 的盘不构成负担。

## API 清单

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 磁盘剩余、快照数、DB 大小、扫描任务状态 |
| `/api/snapshots` | GET | 快照列表（含卷容量） |
| `/api/volume-trend?limit=` | GET | 整卷已用/剩余时间序列 |
| `/api/trees?snapshot_id=&min_kb=` | GET | 目录嵌套树（旭日图数据），默认最新快照、≥50MB |
| `/api/diff?a=&b=&topn=` | GET | 两快照差分，默认最近两个 |
| `/api/trend?path=&limit=` | GET | 单目录历史大小序列 |
| `/api/bigfiles?days=&min_mb=&topn=` | GET | 近期大文件 |
| `/api/scan` | POST | 触发后台扫描（409 = 已在进行） |
| `/api/scan/status` | GET | 扫描任务状态 |

## 核心算法：fold_changes 父子折叠

du 的累计语义使父目录变化必然包含子目录变化，朴素 Top-N 会被同一条链刷屏。折叠规则：

1. 候选有已入选**祖先**、变化量 ≥ 祖先的 90% → **替换**祖先（单链下沉，更深层更精确）
2. 候选有已入选祖先、变化量 < 90% → **保留**（兄弟分支；父 +100 = a +33 + b +33 + 其他 +34 时四个都有定位价值）
3. 候选无入选祖先但有入选**后代** → 残余量（自身 - 同向后代覆盖和）< max(1MB, 自身 10%) 时不入选

## 测试策略

- `tests/test_scanner.py`：9 个用例，临时目录造真实文件走完整 du→SQLite→diff 链路；数据库隔离到 tmp（autouse fixture monkeypatch `config.DB_PATH`）
- 覆盖点：阈值过滤、同日覆盖、UTF-8 八进制转义还原、增长/新增/消失识别、兄弟不折叠、单链下沉、周/日保留策略
- 冒烟：`FATHOM_DB=/tmp/x.db main.py serve` + curl 各端点

## 部署形态与升级路径

三层形态共用同一个 FastAPI 后端与前端（DEC-008）：

```
launchd（后端生命周期）                     用户入口（UI 壳）
├─ com.maoscripts.fathom-scan      ├─ 桌面壳 apps/desktop（Tauri 2，推荐）
│    每日 12:00 → main.py scan            │    tray 菜单栏 + 原生窗口
└─ com.maoscripts.fathom-web       │    loader 页(tauri://localhost) 轮询可达
     常驻 → main.py serve :7952  ◄────────┼──── 跳转 127.0.0.1:7952
                                           └─ 任意浏览器（同一 URL）
```

- 壳不管后端生命周期（无 supervisor，比 Badminton Lab 的 sidecar 模式简一档）
- 前端通过 `window.__TAURI__` 探测运行环境：壳内推送 tray 状态（剩余 GB）+ 监听 tray-action；浏览器内静默降级
- 未来如需原生窗口强化，仅动壳与 capability，内核（scanner/reports/db）零改动
