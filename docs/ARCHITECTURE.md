# Fathom 当前架构

**事实基线：main `7aef239`（仍为开发版），2026-09-13 核对。** 本文件描述该基线代码；通知显示、原生菜单与实际 Tauri WebView 安全边界尚未完成真机验收。待实施设计见 [交付与智能方案](plans/2026-09-12-delivery-and-intelligence.md)。

## 入口与边界

```text
launchd scan → main.py → cli.cmd_scan ──┐
                                     ├─ scanner → SQLite
HTTP POST /api/scan → 安全守卫 → daemon thread ─┘       └─ reports → Markdown → notify（尝试系统通知）
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
| scanner.py | `/usr/bin/du -xk`，以 `DuResult` 返回大小、退出码、耗时和 stderr 质量；完整采集或仅有明确权限拒绝且根记录有效时才替换同日快照 | 特殊路径仍可能误解析；数据库只持久化 denied_count/du_seconds，详细质量尚未入库 |
| reports.py | 比较 entries、在完整候选集上用路径 Trie 做父子折叠、最终稳定排序并截取 Top-N、生成 Markdown，文件名按日期 | 未记录即 added/removed；忽略传入 sid 取全局最新两条；无独立报告状态 |
| notify.py | 日报写完后尝试 osascript 通知；首次记录目录单列；摘要限长；低空间阈值 10 GB | 显示受系统策略控制；首扫无日报不通知；阈值未与 UI 统一；日志可能含路径 |
| bigfiles.py | `/usr/bin/find -xdev -type f -size +... -mtime -... -print0` 后 stat | 大小为 st_size 逻辑字节；每次请求实时遍历；无超时/去重/失败呈现 |
| api.py | 查询、扫描线程、scan_runs 状态持久化；Host/Origin/写令牌守卫；受监控根约束的 reveal；挂载静态文件 | 首扫生成报告报错；扫描锁仍限单 Web 进程；实际 Tauri WebView 尚未真机验证 |
| cli.py | scan/report/bigfiles/status/serve/install/uninstall | scan 的首份报告缺基线会被单独捕获，与 API 成功语义不同 |
| launchd.py | 拼接 XML，安装扫描/常驻 Web 两个 plist | 路径不做 XML 转义；bootstrap 失败只打印，不能可靠表示安装失败 |
| frontend/ | 原生 HTML/JS/CSS、ECharts、hash 五页 | 请求/状态/页面同文件；部分异常未接；重扫后列表不刷新 |
| apps/desktop/ | Tauri 2；显式授权 update_tray_status；单一 sentinel tray 绑定图标/菜单/事件并更新状态行 | bundle.active=false；无自包含 Python、安装/升级/卸载 UI；tray 实机待验 |
| apps/desktop/experiments/iss029/ | PyInstaller onedir 与 helper 生命周期合同原型；只写指定数据根，结果被版本化规则忽略 | 仅技术验证，尚未接入生产 helper 或 `.app`；冻结健康仍受固定 7952 端口阻塞 |

## SQLite 与保留事实

| 表 | 字段概要 | 含义 |
|---|---|---|
| snapshots | id, created_at, root, dir_count, denied_count, du_seconds, total_kb | 时间为本地无时区 ISO 字符串；目录总数包含未持久化小目录 |
| entries | snapshot_id, path, size_kb | 复合主键，WITHOUT ROWID；父目录大小已含子目录 |
| volume_stats | snapshot_id, total_bytes, free_bytes | 扫描时 statvfs，free 为 f_bavail × f_frsize |
| scan_runs | id, started_at, finished_at, status, message | API 写 running/done/failed；done 的 message 为 JSON 结果，failed 为错误；CLI/定时尚不写此表 |

状态恢复仍依赖单 Web 进程约定：锁空闲且记录超过 3600 秒时，在状态读取中收尾为 failed；新扫描持锁时收尾遗留记录。这不能证明其他进程 owner 已退出，多实例共享库不受支持。线程启动失败会释放锁并记录 failed；扫描阶段失败先回滚未提交事务，再独立写入结束态。统一跨进程运行协议归 ISS-020。

同根同一天的新扫描在判定采集有效后，才进入“删除旧快照并写入新条目和卷统计”的事务。致命非零退出、信号退出、空输出、缺失根记录、负数和混合/非权限错误都在写入前失败；仅有明确权限拒绝、根记录有效且数值非负时可记录为部分覆盖。事务中的 SQL 错误会整体回滚，保留原有效快照。

保留近 35 天每日快照，更早按 ISO 周保留一份；weekly_cutoff 实际从今天向前 12 周计算，总跨度约 84 天，不是“35 天再加 12 周”，更不是旧 DEC-006 所写约 9 个月。周分组当前未按根隔离；支持 `scan --root` 不代表多根产品已经正确。

DB 文件尺寸只统计主 `.db`，没包括 WAL/SHM。历史“几十 MB 长期稳定”属于估算，不能代替持续测量；报告/日志也没有独立保留上限。

## 口径

- du 目录值是累计 KiB，父子不可直接求和。目录测量不是原子文件系统快照，采集期间文件可变化。
- 卷 free/used 来自 statvfs；监控根默认仅 HOME 且不跨挂载点。卷已用量与 HOME 目录合计不是同一范围。
- 大文件 st_size 是逻辑大小，与 du 占用、共享块/稀疏文件可能不同。界面当前把 1024 基数标为 KB/MB/GB；目标合同要求明确二进制口径。
- `denied_count` 只数 stderr 每行最后一个错误消息段精确等于 `Permission denied` 或 `Operation not permitted` 的记录；路径文字中的同名片段不会作为权限证据。它不是完整覆盖率，也不能证明未显示的目录被删除。
- 阈值过滤后缺失可能是小于阈值、首次记录、权限/读取失败或移除。当前差分没有足够元数据区分。

## HTTP 接口

| 方法 | 端点 | 当前返回/行为 |
|---|---|---|
| GET | /api/bootstrap | 在同源读取边界内返回当前进程写令牌；令牌不进入 URL、日志或 localStorage |
| GET | /api/status | 实时卷容量、根、快照总数、最新元数据、db_bytes、scan、port |
| GET | /api/snapshots | 全局快照降序列表，含卷统计 |
| GET | /api/volume-trend?limit= | ASC LIMIT，目前取最早 N 条 |
| GET | /api/trees?snapshot_id=&min_kb= | 最新/指定快照目录树；默认 ≥51200 KiB；先取所有行，再限 20000 节点 |
| GET | /api/diff?a=&b=&topn= | b 相对 a；默认全局最近两条；不足两条 409，不存在 404；未校验同根 |
| GET | /api/trend?path=&limit= | 有该路径记录的历史点；缺失不补点；ASC LIMIT |
| GET | /api/bigfiles?days=&min_mb=&topn= | 同步遍历，返回 files；上限 200 |
| POST | /api/scan | 需 `X-Fathom-Token`；running 先落库；进程锁冲突 409；成功返回 200 和 run_id；线程启动失败 503 |
| GET | /api/scan/status?history= | 持久化最新状态，保留原字段并增加 id/status；history=1..100 附 runs，默认不附历史 |
| GET | /api/browse?path= | 最新快照子目录、前一快照差值、趋势、面包屑；首次子目录错误地给全量 delta |
| GET | /api/reports | reports/*.md 文件列表 |
| GET | /api/reports/{date} | 仅接受完整 `YYYY-MM-DD` 片段，返回对应 Markdown 原文；不存在时 404 |
| POST | /api/reveal | 需 `X-Fathom-Token`；只接受对象 JSON；规范化并解析路径后校验位于受监控根内、实际存在，再调用 `/usr/bin/open -R`；拒绝利用 `..` 越界、符号链接逃逸和相似前缀根 |

所有请求只接受回环 Host；带 Origin 的请求只接受同源或允许的 Tauri loader，非安全方法还必须通过进程内写令牌。应用页面使用严格 CSP；`/docs`、`/redoc` 和 `/openapi.json` 仅在精确路径使用文档所需策略。令牌生命周期、多进程协调和桌面发行身份仍需后续任务收口。

API 文档版本为 0.2.0；Tauri config 为 0.3.0，Cargo package 为 0.2.0。接口实际合同以后端代码为准，版本同源化归 ISS-037。

## 当前验证覆盖

当前 main 候选已通过全量 **144 pytest** 与 **39 项 Chromium/API 安全检查**。GitHub CI 在原生 arm64/x86_64 runner 运行 pytest 与 Rust 1.88 locked build，并在 arm64 运行浏览器/API；五项最近验收均成功。覆盖扫描完整性、真实 BSD `du` 反例、事务回滚、scan_runs、Host/Origin/写令牌、reveal 越界/符号链接逃逸、路径与报告名转义、CSP 及浏览器资源清理。隔离真实 API 已执行 du、次日报告、通知 stub、运行历史及实际服务重启；首扫仍复现“有快照但报告不足而失败”，归 ISS-020。实际 Tauri WebView、系统通知、tray、自包含 helper 和签名发行仍为 `NOT_VERIFIED`。

扫描回归包含真实 du、小目录阈值、同日覆盖、差分、保留及失败前不写入；安全浏览器夹具使用合成临时根和结构化 `DuResult`，不会扫描生产 HOME。折叠回归已移除恒真断言，并覆盖 `topn=1` 的父子替换、独立高排名目录、根路径、相似前缀、尾斜杠、正负变化与大输入复杂度。早期隔离反例与页面实测见 [审查证据](plans/2026-09-12-project-review.md)，隔离操作见 [TESTING](TESTING.md)。

不把现有单元测试、其他分支的提交说明或 cargo build 作为安装、系统通知、权限、tray 与定时任务已经可靠的证据。
