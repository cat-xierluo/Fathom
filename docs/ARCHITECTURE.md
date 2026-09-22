# Fathom 当前架构

**事实基线：main `e033ef659bc838dc69ca003f160d58e4569c9c71`，2026-09-19 代码与任务证据交叉核对。** 本次为静态合同核对，未重跑业务测试或实机验收。历史验证边界见文末与 [TASKS](TASKS.md)；目标方案不代表已实现能力。

## 入口与边界

```text
launchd scan / CLI / HTTP POST /api/scan
                 ↓
       scan_coordinator → 跨进程 flock → 分阶段 scan_run_details
                 ↓
 scanner → SQLite snapshot → reports → Markdown → notify（尝试系统通知）→ prune
launchd web → main.py serve → FastAPI :7952 → frontend/ 五页
                                               ↑
Tauri loader → 开发态外部服务；打包态 /health 身份握手 → 本地 HTTP ─┘
前端定期 invoke → Rust tray 标题；tray-action → 前端 → /api/scan
```

API、CLI 与 launchd 定时入口统一调用 `scan_coordinator`；全生命周期 `flock` 是跨进程 owner 真值，进程内锁只用于 API 线程状态。协调器不读取或终止外部 PID，只取消并回收自己启动的 `du`；单次 `du` 的安全时限由 `config.DU_TIMEOUT_S`（环境变量 `FATHOM_DU_TIMEOUT_S`，默认 14400 秒，非有限正数在导入时 fail-closed）决定，超时记为 `interrupted` 并保留上次有效快照（`6789245` 起）。开发态 Tauri 只承担窗口与 tray，不启动或修复后端，后台服务需通过开发版 `install` 安装；打包态（`a158889` 起）壳在启动时定位 `Contents/Resources/helper/` 内的冻结 helper，经 `helper-instance.json`（0600）与 `/health` 身份握手后导航，同服务已运行则复用，端口被占按 ISS-029 语义让位且不向外部进程发信号，任何退出路径都只回收本壳拉起的 helper（SIGTERM，10s 上限）。

## 模块与实际行为

| 模块 | 实现 | 已确认局限 |
|---|---|---|
| config.py | 单一 `RuntimeConfig`；development/release 模式；运行根派生 data/reports/logs；扫描根、只读资源根、回环端口与兼容 `FATHOM_DB` 入口；运行根下 `settings.json` 用户设置持久化（ISS-016A：原子写、校验 fail-closed、优先级 CLI > `FATHOM_*` 环境变量 > settings.json > 默认，覆盖项经 `/api/config` 读写并发布回 `MIN_DIR_KB`/`FREE_ALERT_GB`/`SCAN_HOUR`/`SCAN_MINUTE` 常量） | 计划时间的 launchd plist 写入（含分钟级）与真实服务重载留父卡 ISS-016；release 模式只定义路径合同，不证明 `.app` 已自包含 |
| db.py | sqlite3、WAL、外键、schema v5；跨进程迁移锁、结构/完整性 fail-closed 校验、事务迁移与 0600 SQLite 一致备份 | 支持旧版 v0–v4→v5；磁盘满/掉电与真实历史用户库升级仍待发行验收 |
| scanner.py | `/usr/bin/du -xk` 原始 bytes 采集，以请求根前缀无损映射特殊路径；`DuResult` 承载采集结果、退出码、耗时、权限/瞬时/其他错误分类计数与样例；`du_process_context`/`run_du` 管理取消、超时及扫描锁 FD 传递；有效采集才替换同数据集同日快照 | 无法无歧义映射/解码时拒绝采集；瞬时系统错误（Interrupted system call/Resource temporarily unavailable，按行尾 errno 段精确匹配）单独计数且使采集归 partial、永不 full，与真实致命错误并存仍整体拒绝；快照持久化 min_kb 与 collection_status（full/partial），v3 之前旧行为 NULL；瞬时计数尚未入库 |
| reports.py | 比较 entries、在完整候选集上用路径 Trie 做父子折叠、最终稳定排序并截取 Top-N、生成 Markdown；按传入 sid 查找同数据集（同根同 `min_kb` 同 `exclude_names`）前驱，报头带 a/b 快照 ID 与记录口径说明 | 单条目仍无法区分低于阈值与移除，措辞如实表达为未记录/首次记录；报告状态由协调器单独记录 |
| notify.py | 已支持首扫、对比、零变化、部分覆盖和中断通知；正文上限 200 字；低空间阈值读 `config.FREE_ALERT_GB` | 通知调用成功不等于系统横幅展示；权限与系统通知实测仍属 ISS-003；中断消息含路径线索，诊断共享须脱敏 |
| bigfiles.py | `BigfilesManager` 显式触发查询任务：同参数 (root, days, min_mb, topn) 并发去重只启动一次 `find`，进程组 SIGTERM→SIGKILL 升级回收，TTL 缓存（过期为瞬态：驱逐后重启 find），结果上限截断，五态 ok/no_match/permission_denied/failed/truncated（expired 仅作合同兼容），find stderr/退出码不再当空结果，日志路径 sha8+basename 脱敏，`resource.getrusage` 记录墙钟/峰值内存/输出行 | 大小为 st_size 逻辑字节；同步包装 `find_big_files` 对过期结果抛 `BigfilesError` 而非静默；预算常量 `BIGFILE_*` 在 config.py，日志/报告保留由协调器收尾消费 |
| scan_coordinator.py | API/CLI/定时统一扫描；`flock`、owner 元数据、分阶段状态、首扫/故障/取消语义 | 开发版两有效定时日期与日报已有 ISS-001 记录；发行后台计划、登录/退出/休眠语义仍待 ISS-010 |
| api.py | 查询、非 daemon 扫描线程；Host/Origin/写令牌守卫；受监控根约束的 reveal；挂载静态文件 | 打包态主界面已有 ISS-009 切片 2 实机记录；权限、更新等发行路径仍待对应父卡 |
| cli.py | scan/report/bigfiles/status/serve/install/uninstall；scan 可标记 cli/scheduled 来源；report 按同数据集前驱生成（可 --snapshot-id 指定 b），无前驱明确文案并非零退出；`--version`；serve 支持 `--port/--port-range` 让位与零击杀 | 与 API 共用协调合同；install/uninstall 仍是开发版入口 |
| launchd.py | 开发版 XML/plist 安装；只读 `status()`/`dry_run_plan()`；ISS-010B 发行态写路径 `release_plan/register_release/unregister_release`（confirmed 门+失败回滚，fake 全覆盖）；ISS-016B 只读 `read_registered_scan_time()` + `service_reload_state()` 纯函数；Rust `autostart.rs` 提供状态/注册/注销桥（ISS-010B，恰四权限） | 真实注册/重载/睡眠恢复留 G8 实机门；SMAppService 登录项恒 unknown（不引 objc）；发行 plist 未钉 EnvironmentVariables（G8 核对项） |
| frontend/ | 无构建链原生 ES modules：modules/ 下 request（世代号+pageScoped 防倒序覆盖、apiPost/apiPut 写令牌）、format、charts（隐藏 stale/重显 resume）、polling（幂等单实例）、tauri（浏览器降级）、router（hash 路由+单一刷新入口）、status 与五页 enter/leave 模块；设置页读 `/api/config`+`/api/status` 渲染真实值并可编辑保存（ISS-016A，校验以服务端为准、失败保持旧值可辨）；R3 测深视觉签名已实装（ISS-072：海沟蓝 token、深度环字标、等深线纹理、tabular-nums）；`favicon.svg` 使用深度环、`apple-touch-icon.png` 使用深潭位图（DEC-023 双形态，与 App 深潭图标分别维护）；ECharts 本地 vendor | 打包态真实 WebView 下页面渲染已于 2026-09-18 实机验证（ISS-009 切片 2）；移动布局未支持 |
| apps/desktop/ | Tauri 2；显式授权 update_tray_status；单一 sentinel tray 绑定图标/菜单/事件并更新状态行；打包态 `helper.rs` 负责冻结 helper 的 locate/spawn/握手（陈旧 `helper-instance.json` 经 `/health` 探活 + 只读 pid 判定后跳过，`d53a7af` 起）/让位/幂等回收，端口耗尽经 `helper_status` 以 `state=exhausted`+`recovery` 交握手页渲染并可 `helper_retry`；`bundle.resources` 深键 map 把 helper 落到 `Contents/Resources/helper/`；`scripts/build_helper.sh`/`build_app.sh`/`verify_app_bundle.sh` 产出并校验未签名 .app/.dmg（22 项）；图标为 ISS-045/DEC-023 双形态体系（App 图标=层叠深潭位图系：`assets/brand/` 原稿 + `scripts/build_app_icon.py` 抠图 1024 + `build_icons.sh` 全尺寸；界面小尺寸=深度环矢量系：tray 为 `make_tray_icon.py` 单色 template `icon_as_template(true)`，几何由 `ci_brand_geometry.sh` 门禁同步） | 未签名、未公证、仅 arm64；新账户首启、含空格/中文路径、tray 菜单实机退出未验（握手页 exhausted 已于 2026-09-18 实机验证）；图标 18pt 小菜单栏下 tray 可辨性未实测（Launchpad/Spotlight 与深浅色模式已于 2026-09-19 实测通过） |
| apps/desktop/experiments/iss029/ | PyInstaller onedir 与 helper 生命周期合同原型；只写指定数据根，结果被版本化规则忽略 | 生产 `build_helper.sh` 默认复用其 `.venv-build`（PyInstaller 6.22.3）冻结 helper 并打进 `.app`（`a158889` 起）；x86_64 未冻结（ISS-041） |

## SQLite 与保留事实

| 表 | 字段概要 | 含义 |
|---|---|---|
| schema | `PRAGMA user_version=5` | v0–v4 开发库经完整性/结构校验和一致备份后逐级事务迁移；未来版本、损坏或不兼容结构拒绝打开 |
| snapshots | id, created_at, root, dir_count, denied_count, du_seconds, total_kb, min_kb, collection_status, vanished_count, exclude_names | 时间为本地无时区 ISO 字符串；目录总数包含未持久化小目录；min_kb/collection_status 自 v3 起持久化，旧行 NULL 不补造；v4 新增 vanished_count 默认 0，v5 新增 exclude_names 默认空串（规范掩码串） |
| entries | snapshot_id, path, size_kb | 复合主键，WITHOUT ROWID；父目录大小已含子目录 |
| volume_stats | snapshot_id, total_bytes, free_bytes | 扫描时 statvfs，free 为 f_bavail × f_frsize |
| scan_runs | id, started_at, finished_at, status, message | API/CLI/定时统一写 running/done/failed/interrupted；done message 保留兼容结果，failed/interrupted 为错误或取消原因 |
| scan_run_details | run_id, source, phase, owner_*, heartbeat_at, snapshot_id, report/notification 状态, pruned_count, warnings | 自 schema v2 引入的一对一阶段详情；来源为 api/cli/scheduled，快照成功与报告/通知结果分开 |

协调器取得 `flock` 后才把遗留 running 记录收尾为 failed；无法取得锁时返回 busy 并只展示非权威 owner 元数据，不以 PID 或超时推断 owner 已死。扫描、报告、通知和保留逐阶段提交，首扫保存有效快照且 `report_status=not_available` 时整体成功；报告/通知失败作为警告，不抹掉快照。API shutdown、CLI SIGTERM/KeyboardInterrupt 与超时只回收本次会话拥有的 `du`，最后释放锁。

同数据集（同根同 `min_kb` 同 `exclude_names` 口径）同一天的新扫描在判定采集有效后，才进入“删除旧快照并写入新条目和卷统计”的事务；更换阈值口径属另一数据集，同日共存不互相替换。致命非零退出、信号退出、空输出、缺失根记录、负数和混合/非权限错误都在写入前失败；有明确权限/瞬时错误或扫描期间目录消失、根记录有效且数值非负并通过无歧义解析时可记录为部分覆盖。事务中的 SQL 错误会整体回滚，保留原有效快照。

保留近 35 天每日快照，更早按 ISO 周保留一份；weekly_cutoff 实际从今天向前 12 周计算，总跨度约 84 天，不是“35 天再加 12 周”，更不是旧 DEC-006 所写约 9 个月。周分组按数据集 (root, min_kb, exclude_names) 隔离，两根或双数据集同周历史各保留一份；支持 `scan --root` 不代表多根产品已经正确。

DB 文件尺寸只统计主 `.db`，没包括 WAL/SHM。历史“几十 MB 长期稳定”属于估算，不能代替持续测量。报告与日志按 `BIGFILE_REPORT_RETENTION_DAYS` / `BIGFILE_LOG_RETENTION_DAYS` 在扫描保留阶段清理：只删运行根内文件名日期可解析且早于阈值的文件，不可解析一律保留，失败作为 warning 不影响快照。

## 口径

- du 目录值是累计 KiB，父子不可直接求和。目录测量不是原子文件系统快照，采集期间文件可变化。
- 卷 free/used 来自 statvfs；监控根默认仅 HOME 且不跨挂载点。卷已用量与 HOME 目录合计不是同一范围。
- 大文件 st_size 是逻辑大小，与 du 占用、共享块/稀疏文件可能不同。界面当前把 1024 基数标为 KB/MB/GB；目标合同要求明确二进制口径。
- `denied_count` 只数 stderr 每行最后一个错误消息段精确等于 `Permission denied` 或 `Operation not permitted` 的记录；路径文字中的同名片段不会作为权限证据。它不是完整覆盖率，也不能证明未显示的目录被删除。
- 阈值过滤后缺失可能是小于阈值、权限/读取失败或移除；差分已按数据集区分首次记录与未记录，但单条目仍无法区分低于阈值与移除，措辞不冒充文件系统事实。

## HTTP 接口

| 方法 | 端点 | 当前返回/行为 |
|---|---|---|
| GET | /api/bootstrap | 在同源读取边界内返回当前进程写令牌；令牌不进入 URL、日志或 localStorage |
| GET | /api/status | 实时卷容量、根、快照总数、最新元数据、db_bytes、scan、port，以及不含令牌/凭据的实际 runtime 路径与模式 |
| GET | /api/snapshots | 全局快照降序列表，含卷统计与 min_kb/collection_status/vanished_count/exclude_names |
| GET | /api/volume-trend?limit= | 以最新快照为锚取最新 N 条后正序输出；按数据集 (root, min_kb, exclude_names) 隔离，跨根/跨阈值不混点 |
| GET | /api/trees?snapshot_id=&min_kb= | 最新/指定快照目录树；默认 ≥51200 KiB；节点预算在 SQL 层生效（LIMIT+1 探测），响应含 truncated/matched_count/node_count/node_limit；截断时子孙提升为顶层且无孤儿重复；指定快照不存在 404、空库 200 snapshot_id=null、低于阈值 200 空结果；root=/ 归一化不再成为自己的孩子 |
| GET | /api/diff?a=&b=&topn= | b 相对 a；默认 a=最新快照的同数据集前驱；不足/无同数据集前驱 409，不存在 404；a/b 跨根、跨阈值或跨排除集 400 |
| GET | /api/trend?path=&limit= | 以最新记录该路径的快照为锚取最新 N 点后正序输出，按数据集隔离；缺失不补点（保留 gap）；无记录路径 200 空 points |
| GET | /api/bigfiles?days=&min_mb=&topn= | 经 BigfilesManager 显式触发/去重/TTL；返回 state、scope、stats（wall_ms/peak_rss_bytes/find_output_lines/find_exit_code/…）、files、truncated、cached、cache_age_s、error_message；topn 上限 200 |
| POST | /api/scan | 需 `X-Fathom-Token`；先取得跨进程 `flock` 再落 running；冲突 409；成功返回 run_id；线程启动失败 503 并释放租约 |
| GET | /api/scan/status?history= | 返回最新统一状态、source/phase/snapshot/report/notification/pruned/warnings；history=1..100 附 API/CLI/定时运行 |
| GET | /api/browse?path= | 最新快照子目录、同数据集前驱差值、趋势、面包屑；无基线时 delta_kb=null、is_new=false |
| GET | /api/reports | reports/*.md 文件列表 |
| GET | /api/reports/{date} | 仅接受完整 `YYYY-MM-DD` 片段，返回对应 Markdown 原文；不存在时 404 |
| POST | /api/reveal | 需 `X-Fathom-Token`；只接受对象 JSON；规范化并解析路径后校验位于受监控根内、实际存在，再调用 `/usr/bin/open -R`；拒绝利用 `..` 越界、符号链接逃逸和相似前缀根 |
| GET | /api/config | 当前生效用户设置（ISS-016A）：scan_root/scan_time/min_kb/free_alert_gb/exclude_names 生效值 + 逐项来源（env/settings/default/cli）+ 恢复默认值 + 只读策略（保留/du 时限/大文件默认）+ settings_path；ISS-016B 起附 `service_reload_state`（in_sync/drift/not_registered/unknown，只读解析已注册 plist，ISS-016A 旧键并存）；只读无副作用 |
| PUT | /api/config | 需 `X-Fathom-Token`；部分更新（缺省键不变），校验 scan_time HH:MM、scan_root 存在且为目录、min_kb/free_alert_gb 有限正数（拒绝 nan/inf/0/负/非数值），无效 400 + 中文 detail 且旧值不动；有效则原子写运行根 settings.json（失败 500 且旧文件保留）并在当前进程生效；不注册/不重载 launchd；返回 `{"applied": true, "service_reload": "requires_user_action", hint}`（ISS-016A 兼容键）与重算后的 `service_reload_state`（ISS-016B） |

参数校验失败统一返回 400 + 中文 detail（原 FastAPI 默认 422 已全局收敛，前端与测试无 422 依赖）。所有请求只接受回环 Host；带 Origin 的请求只接受同源或允许的 Tauri loader，非安全方法还必须通过进程内写令牌。应用页面使用严格 CSP；`/docs`、`/redoc` 和 `/openapi.json` 仅在精确路径使用文档所需策略。令牌生命周期和桌面发行身份仍需后续任务收口。

### 自包含 helper 的运行合同（ISS-029 切片 2 已落地）

- CLI `serve` 支持 `--port`/`--port-range`：目标端口被占用时按身份判定——`/health` 报告同服务（service=fathom 且 protocol_version 一致）则让位并退出 0；否则在 `port..port+range` 内寻找空闲端口绑定，找不到则以非零退出。全程不向任何外部进程发信号。
- 端口发现文件写入运行根 `helper-instance.json`（0600，含 pid/port/identity），进程退出时按身份匹配清理。
- `--version` 与 `/health` 的身份字段同源于 `fathom.__init__` 的常量；`/health` 返回 service/protocol_version/pid/port/runtime_mode/status。
- 冻结产物（PyInstaller onedir）在无 hidden-import 时可服务；data/reports/logs 由 `FATHOM_RUNTIME_DIR` 指向冻结树之外。x86_64 与 .app 签名公证仍未验证（ISS-041/Tauri 端）。

版本单一源为 `fathom.__version__ = 0.3.0`：API 文档、Tauri config、Cargo package 与 Cargo.lock 本地包行同源，`scripts/check_version_consistency.sh` 在任一漂移时 fail-closed（一致 0 / 漂移 1 / 缺失 2）。接口实际合同以后端代码为准。

## 当前验证覆盖

当前 main 的精确门禁为 **647 pytest**（ISS-016B 起 +53、ISS-078 +7、ISS-078 repair1 +1，截至该基线的任务记录，非本次复跑）与 94 项前端检查（ISS-069 +10、ISS-002A +11、ISS-016A +4、ISS-073 +2、ISS-016B +6）与 `cargo test` 42 项（ISS-068 +2、ISS-010A +9、ISS-077 +1、ISS-010B +16）；另通过 39 项 Chromium/API 检查、94 项前端检查（模块生命周期、大文件五态、三条旅程、全状态矩阵、键盘/复制/视口、设置持久化）与版本一致性校验器。ISS-040B（updater 接线切片）后门禁为 **655 pytest**（+8 updater ACL 守护）、**105 项前端检查**（+8 应用更新断言；此前账面 94 与脚本实测 97 项存在 3 项口径差，本次按实测 105=97+8 修正记录）与 `cargo test` **49 项**（+7 updater 状态映射/名字合同/延迟合同单测）；ISS-030A（升级协调协议夹具切片）后 pytest 门禁为 **671**（+16：tests/upgrade_fixture.py 假环境/六步协议/失败注入与 tests/test_upgrade_coordination.py 的 happy path、四类失败回滚、schema 拒绝自动验，真实 flock 与 WAL checkpoint/backup API、零生产触碰；真实 N→N+1 与发行产物验证留父卡 ISS-030）。覆盖扫描完整性、特殊路径真实 BSD `du`→bytes→SQLite、v0–v4→v5 迁移/WAL 一致备份、真实跨进程 `flock`、API 空库首扫、CLI/定时来源、报告/通知故障、SIGTERM/超时回收（含超时进度条数线索）、Host/Origin/写令牌、reveal 越界、前端重扫/乱序/错误状态、CSP 及浏览器资源清理。GitHub Actions 因账户额度在 job 步骤前拒绝，当前云端结果记为 `NOT_RUN`；恢复额度后重新启用。

已有历史实测记录：ISS-009 切片 2 的打包态主界面/端口耗尽页、ISS-045 图标入口、ISS-001 的两个有效定时日期与日报。仍 `NOT_VERIFIED`：系统通知实际展示、完整 tray 菜单与发行生命周期、新账户/断网/实际下载首启、原生 x86_64 冻结及真实更新。Developer ID 签名/公证/stapling 按 DEC-022 延期，未通过，不可与 updater 签名混同。
扫描回归包含真实 du、小目录阈值、同日覆盖、差分、保留及失败前不写入；安全浏览器夹具使用合成临时根和结构化 `DuResult`，不会扫描生产 HOME。折叠回归已移除恒真断言，并覆盖 `topn=1` 的父子替换、独立高排名目录、根路径、相似前缀、尾斜杠、正负变化与大输入复杂度。早期隔离反例与页面实测见 [审查证据](plans/2026-09-12-project-review.md)，隔离操作见 [TESTING](TESTING.md)。

不把现有单元测试、其他分支的提交说明或 cargo build 作为安装、系统通知、权限、tray 与定时任务已经可靠的证据。
