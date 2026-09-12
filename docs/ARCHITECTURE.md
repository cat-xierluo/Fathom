# Fathom 当前架构

**事实基线：main `33e81f9`（v0.3.0 文档版本），2026-09-12 核对。** 本文件只描述该基线代码。其他分支的通知、scan_runs 持久化、tray 补丁尚不计入本基线；合并后由对应任务更新事实。待实施设计见 [交付与智能方案](plans/2026-09-12-delivery-and-intelligence.md)。

## 入口与边界

```text
launchd scan → main.py → cli.cmd_scan ──┐
                                     ├─ scanner → SQLite
HTTP POST /api/scan → daemon thread ──┘       └─ reports → Markdown
launchd web → main.py serve → FastAPI :7952 → frontend/ 五页
                                               ↑
Tauri loader → 探测 /api/status → 跳转本地 HTTP ─┘
前端定期 invoke → Rust tray 标题；tray-action → 前端 → /api/scan
```

CLI 和 API 的执行流程目前各自实现，只有 API 有进程内 threading.Lock。Tauri 只承担窗口与 tray，不启动或修复后端；关闭窗口隐藏，tray 菜单退出 UI。后台服务需另行通过开发版 `install` 安装，壳与后端生命周期分离。

## 模块与实际行为

| 模块 | 实现 | 已确认局限 |
|---|---|---|
| config.py | 常量、项目相对运行目录、HOME 根、127.0.0.1:7952 | 仅 DB 支持 FATHOM_DB，不能隔离其他输出/扫描范围 |
| db.py | sqlite3、WAL、外键、CREATE TABLE IF NOT EXISTS | 无 schema version/迁移协议；读取也打开可写连接并执行建表 |
| scanner.py | `/usr/bin/du -xk`，内存捕获并解析，保留 ≥10240 KiB 的 entries | 不校验退出码/根存在；du_seconds 写 0；特殊路径存在误解析 |
| reports.py | 比较 entries、父子折叠、Markdown，文件名按日期 | 未记录即 added/removed；忽略传入 sid 取全局最新两条；无独立报告状态 |
| bigfiles.py | `/usr/bin/find -xdev -type f -size +... -mtime -... -print0` 后 stat | 大小为 st_size 逻辑字节；每次请求实时遍历；无超时/去重/失败呈现 |
| api.py | 查询、扫描线程、reveal，挂载静态文件 | 首扫生成报告报错；写接口无鉴权；根检查仅字符串前缀 |
| cli.py | scan/report/bigfiles/status/serve/install/uninstall | scan 的首份报告缺基线会被单独捕获，与 API 成功语义不同 |
| launchd.py | 拼接 XML，安装扫描/常驻 Web 两个 plist | 路径不做 XML 转义；bootstrap 失败只打印，不能可靠表示安装失败 |
| frontend/ | 原生 HTML/JS/CSS、ECharts、hash 五页 | 请求/状态/页面同文件；部分异常未接；重扫后列表不刷新 |
| apps/desktop/ | Tauri 2，远端本地 HTTP 页面获 capability | bundle.active=false；无自包含 Python、安装/升级/卸载 UI；tray 实机待验 |

## SQLite 与保留事实

| 表 | 字段概要 | 含义 |
|---|---|---|
| snapshots | id, created_at, root, dir_count, denied_count, du_seconds, total_kb | 时间为本地无时区 ISO 字符串；目录总数包含未持久化小目录 |
| entries | snapshot_id, path, size_kb | 复合主键，WITHOUT ROWID；父目录大小已含子目录 |
| volume_stats | snapshot_id, total_bytes, free_bytes | 扫描时 statvfs，free 为 f_bavail × f_frsize |
| scan_runs | id, started_at, finished_at, status, message | 基线中只建表，API 用内存 `_scan_state`；ISS-007 分支已接表 |

同根同一天的新扫描事务中先删除旧快照再写入新条目和卷统计；事务可回滚 SQL 错误，但 **du 失败目前不会被识别为写入前的失败**。

保留近 35 天每日快照，更早按 ISO 周保留一份；weekly_cutoff 实际从今天向前 12 周计算，总跨度约 84 天，不是“35 天再加 12 周”，更不是旧 DEC-006 所写约 9 个月。周分组当前未按根隔离；支持 `scan --root` 不代表多根产品已经正确。

DB 文件尺寸只统计主 `.db`，没包括 WAL/SHM。历史“几十 MB 长期稳定”属于估算，不能代替持续测量；报告/日志也没有独立保留上限。

## 口径

- du 目录值是累计 KiB，父子不可直接求和。目录测量不是原子文件系统快照，采集期间文件可变化。
- 卷 free/used 来自 statvfs；监控根默认仅 HOME 且不跨挂载点。卷已用量与 HOME 目录合计不是同一范围。
- 大文件 st_size 是逻辑大小，与 du 占用、共享块/稀疏文件可能不同。界面当前把 1024 基数标为 KB/MB/GB；目标合同要求明确二进制口径。
- `denied_count` 只数包含两种英文权限报错的 stderr 行；不是完整的覆盖率，也不能证明未显示的目录被删除。
- 阈值过滤后缺失可能是小于阈值、首次记录、权限/读取失败或移除。当前差分没有足够元数据区分。

## HTTP 接口

| 方法 | 端点 | 当前返回/行为 |
|---|---|---|
| GET | /api/status | 实时卷容量、根、快照总数、最新元数据、db_bytes、scan、port |
| GET | /api/snapshots | 全局快照降序列表，含卷统计 |
| GET | /api/volume-trend?limit= | ASC LIMIT，目前取最早 N 条 |
| GET | /api/trees?snapshot_id=&min_kb= | 最新/指定快照目录树；默认 ≥51200 KiB；先取所有行，再限 20000 节点 |
| GET | /api/diff?a=&b=&topn= | b 相对 a；默认全局最近两条；不足两条 409，不存在 404；未校验同根 |
| GET | /api/trend?path=&limit= | 有该路径记录的历史点；缺失不补点；ASC LIMIT |
| GET | /api/bigfiles?days=&min_mb=&topn= | 同步遍历，返回 files；上限 200 |
| POST | /api/scan | 线程启动；同一 API 进程锁冲突 409；成功启动返回 200 |
| GET | /api/scan/status | 基线内存状态，重启丢失；ISS-007 的 history 参数不属于本基线 |
| GET | /api/browse?path= | 最新快照子目录、前一快照差值、趋势、面包屑；首次子目录错误地给全量 delta |
| GET | /api/reports | reports/*.md 文件列表 |
| GET | /api/reports/{date} | Markdown 原文；日期校验不完整 |
| POST | /api/reveal | JSON path → 字符串前缀/存在检查 → open -R；未规范化路径 |

API 文档版本为 0.2.0；Tauri config 为 0.3.0，Cargo package 为 0.2.0。接口实际合同以后端代码为准，版本同源化归 ISS-037。

## 当前验证覆盖

`tests/test_scanner.py` 有 9 个测试，包含真实 du、小目录阈值、同日覆盖、差分及保留。折叠测试中的 `or True` 是恒真断言，原来宣称的单链行为没有被有效验证。此次隔离反例与页面实测见 [审查证据](plans/2026-09-12-project-review.md)，隔离操作见 [TESTING](TESTING.md)。

不把现有单元测试、其他分支的提交说明或 cargo build 作为安装、系统通知、权限、tray 与定时任务已经可靠的证据。
