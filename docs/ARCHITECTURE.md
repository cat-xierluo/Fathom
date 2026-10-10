# Fathom 当前架构

**原始事实基线：main `31396a08ed90a37f787614d6f32024e30836c3aa`，2026-09-22 代码与任务证据交叉核对；2026-09-28 主干已补核 ISS-116 路径状态分类、schema 与相关 API，以及 ISS-114 原生滚动限位。** 历史验证边界见文末；目标方案不代表已实现能力，实机与发行验收状态以任务卡为准。

**0.4.0 对账补记（基线 main `7b280e9`）**：本轮把范围配置、多范围协调、schema v8、整盘摘要、索引提速与 plan 身份两档口径按 `7b280e9` 代码逐条重核后写入，每条均可锚到文件:模块。`NOT_VERIFIED` 边界见文末「当前验证覆盖」，未被本轮实现的目标一律保留原状表述。

## 入口与边界

```text
launchd scan / CLI / HTTP POST /api/scan
                 ↓
       scan_coordinator → 跨进程 flock → 分阶段 scan_run_details
                 ↓
 scanner → SQLite snapshot → reports → Markdown → notify（尝试系统通知）→ prune
launchd web → python -m fathom serve → FastAPI :7952 → frontend/ 六页（含 Agent）
                                               ↑
Tauri loader → 开发态外部服务；打包态 /health 身份握手 → 本地 HTTP ─┘
前端定期 invoke → Rust tray 标题；tray-action → 前端 → /api/scan
```

API、CLI 与 launchd 定时入口统一调用 `scan_coordinator`；全生命周期 `flock` 是跨进程 owner 真值，进程内锁只用于 API 线程状态。协调器不读取或终止外部 PID，只取消并回收自己启动的 `du`；单次 `du` 的安全时限由 `config.DU_TIMEOUT_S`（环境变量 `FATHOM_DU_TIMEOUT_S`，默认 14400 秒，非有限正数在导入时 fail-closed）决定，超时记为 `interrupted` 并保留上次有效快照（`6789245` 起）。开发态 Tauri 只承担窗口与 tray，不启动或修复后端，后台服务需通过开发版 `install` 安装；打包态（`a158889` 起）壳在启动时定位 `Contents/Resources/helper/` 内的冻结 helper，经 `helper-instance.json`（0600）与 `/health` 身份握手后导航，同服务已运行则复用，端口被占按 ISS-029 语义让位且不向外部进程发信号，任何退出路径都只回收本壳拉起的 helper（SIGTERM，10s 上限）。

## 模块与实际行为

总览到变化页的交接由 `overview.js` 的单次入口对象和 `changes.js` 的选择意图世代共同约束。目录请求绑定发起时的世代；较新入口或手动选择出现后，迟到响应可更新快照目录，但不能回填旧区间。入口等待器只消费自己的对象，离开变化页会撤销待交接对象。启用既有 `window.__diag` 探针时记录请求、响应与过时落定，诊断不参与控制流，也不新增生产日志。

| 模块 | 实现 | 已确认局限 |
|---|---|---|
| config.py | 单一 `RuntimeConfig`；development/release 模式；运行根派生 data/reports/logs；扫描根、只读资源根、回环端口与兼容 `FATHOM_DB` 入口；运行根下 `settings.json` 用户设置持久化（ISS-016A：原子写、校验 fail-closed、优先级 CLI > `FATHOM_*` 环境变量 > settings.json > 默认，覆盖项经 `/api/config` 读写并发布回 `MIN_DIR_KB`/`FREE_ALERT_GB`/`SCAN_HOUR`/`SCAN_MINUTE` 常量） | 分钟级 plist 写入与经确认重装已有 ISS-010B/016B 接线；发行账户中的真实计划/运行根一致性仍待 ISS-016 验收。ISS-155 新增 `storage_scope` 范围选择（`config.py:454` 未落盘即 `None`＝尚未选择；读取不迁移，release 主动采集默认解析启动盘）：`ScopeSelection`（`config.py:507`）校验 fail-closed（未知 mode/相对路径/不存在目录/重复根/超过 16 项/NUL 字节/scope_ids 错位/身份版本不匹配一律 `ConfigurationError`，见 `config.py:553-572` 与 `_validated_storage_scope` `config.py:577`）、`save_scope_selection` 乐观版本冲突保护、`_SETTINGS_LOCK` 改可重入 RLock（`config.py:391`）、排除集被环境变量覆盖时原样透传；落盘为 wire 形态（`config.py:476`）。**能力边界（ISS-174 核实结论，0.4.0 未变）**：范围经 Web 设置页 / `/api/config` / CLI 生效，桌面壳**不读取范围配置来驱动扫描**；lib.rs 侧只有形态容忍的只读接缝（`storage_scope_setting`，容忍未知键、畸形结构降级 `None`），不是行为接线 |
| db.py | sqlite3、WAL、外键、schema v8（`SCHEMA_VERSION = 8`，`db.py:20`）；跨进程迁移锁、结构/完整性 fail-closed 校验、事务迁移与 0600 SQLite 一致备份。v8（ISS-153）新增五表：scan_scopes（范围/卷/容器稳定 ID）、scan_plans（规范根计划，数据集身份＝计划＝范围+规范根+度量版本+阈值+掩码）、scan_rounds、scan_round_members（`snapshot_id` 无外键）、container_capacity_samples（容量样本另存来源/时间），并给 snapshots 加三可空列（plan_id/round_id/metric_version，`db.py:208-210`）；legacy 行身份保持 NULL 不补造；同日替换/保留按 plan 隔离；删除显式 expired 不级联 AI 证据。ISS-168 增 `entries(path)` 二级索引 `idx_entries_path`（`db.py:31`）——纯索引不加列、不重建表、不迁移数据、不改 `SCHEMA_VERSION`；ISS-173 把 connect 快路径的补建改为**写锁让步三段式**（`db.py:40-49`、`db.py:616`、`db.py:917`）：DDL 需写锁而 connect 是普通读路径，预算试探抢不到即登记待稍后建，只对 `SQLITE_BUSY`/`database is locked` 竞争让步，语法错误、只读库、库损坏等 `OperationalError` 原样上抛不伪装成「已让步」 | 支持旧版 v0–v7→v8；磁盘满/掉电与真实历史用户库升级仍待发行验收；百万档首次建索引库体积约 **+93MB**（ISS-168 实测代价，README 已知限制同源）；待办索引在扫描写入期间补建会再次让步，不保证「打开一次即建成」 |
| scanner.py | `/usr/bin/du -xk` 原始 bytes 采集，以请求根前缀无损映射特殊路径；`DuResult` 承载采集结果、退出码、耗时、权限/瞬时/其他错误分类计数与样例；`du_process_context`/`run_du` 管理取消、超时及扫描锁 FD 传递；有效采集才替换同数据集同日快照 | 无法无歧义映射/解码时拒绝采集；瞬时系统错误（Interrupted system call/Resource temporarily unavailable，按行尾 errno 段精确匹配）单独计数且使采集归 partial、永不 full，与真实致命错误并存仍整体拒绝；快照持久化 min_kb 与 collection_status（full/partial），v3 之前旧行为 NULL；瞬时计数尚未入库 |
| reports.py | 比较 entries、在完整候选集上用路径 Trie 做父子折叠、最终稳定排序并截取 Top-N、生成 Markdown；按传入 sid 查找同数据集（同根同 `min_kb` 同 `exclude_names`）前驱，报头带 a/b 快照 ID 与记录口径说明 | 单条目仍无法区分低于阈值与移除，措辞如实表达为未记录/首次记录；报告状态由协调器单独记录 |
| hierarchy.py | 绑定历史区间的同级差分查询（ISS-147）：entries 主键 (snapshot_id, path) 前缀范围扫描 + 双侧有序游标归并，按路径段聚合直属子行与祖先/父行；四态行（measured/first_recorded/unrecorded/structural）、nullable delta、changed 筛选保留命中后代导航节点、稳定分页游标；供 `/api/diff/children`，不改既有 diff/报告行为 | 根层耗时随子树规模线性（10 万条目/快照合成库根层约 2.6s 冷/0.8–1.5s 热，深链 <70ms）；更大生产库未实测；恒定时间根层需物化聚合（schema 变更，未做） |
| notify.py | 已支持首扫、对比、零变化、部分覆盖和中断通知；正文上限 200 字；低空间阈值读 `config.FREE_ALERT_GB` | 通知调用成功不等于系统横幅展示；权限与系统通知实测仍属 ISS-003；中断消息含路径线索，诊断共享须脱敏 |
| bigfiles.py | `BigfilesManager` 显式触发查询任务：同参数 (root, days, min_mb, topn) 并发去重只启动一次 `find`，进程组 SIGTERM→SIGKILL 升级回收，TTL 缓存（过期为瞬态：驱逐后重启 find），结果上限截断，五态 ok/no_match/permission_denied/failed/truncated（expired 仅作合同兼容），find stderr/退出码不再当空结果，日志路径 sha8+basename 脱敏，`resource.getrusage` 记录墙钟/峰值内存/输出行 | 大小为 st_size 逻辑字节；同步包装 `find_big_files` 对过期结果抛 `BigfilesError` 而非静默；预算常量 `BIGFILE_*` 在 config.py，日志/报告保留由协调器收尾消费 |
| scan_coordinator.py | API/CLI/定时统一扫描；`flock`、owner 元数据、分阶段状态、首扫/故障/取消语义。ISS-154 增加一轮多范围协调（`7b280e9` 已合并）：`ScopeSpec`（`scan_coordinator.py:106`，缺省按规范根派生 `path:` 型内容 ID，不伪造卷身份）、`RoundMember`（:169）/`RoundPlan`（:188）去重后成员序列（范围输入顺序即采集顺序）、按计划身份五元组（scope_id/规范根/metric_version/min_kb/exclude_names）去重并复用 ISS-153 身份登记；成员六态 `MEMBER_PENDING`…（:71-78：pending/running/done/failed/cancelled/skipped）与轮次终态同源推导（取消优先，其次全成功 full、零成功 failed、其余 partial，刻意不产出整轮用量百分比）；成员失败不拖垮整轮，未开始的成员记 skipped 而非 failed（:771-791）；每成功成员各写同计划对比日报并出轮次汇总（`_round_summary_text` :397），仅 full 才逐范围发完成通知；轮末采一组容器/卷容量样本（容器 free 为唯一权威，卷级 free 恒空，:501-526），零读数即记 unavailable 且不拿目录 statvfs 替补；整轮时限取显式值或「每范围 du 时限 × 成员数」（`round_timeout_seconds` :597/680、deadline :864-865）。ISS-175 取消响应合同区分「本次受理」与「到达前已终结」（具名 409） | 开发版两有效定时日期与日报已有 ISS-001 记录；发行后台计划、登录/退出/休眠语义仍待 ISS-010；真实 HTTP E2E（保存→扫描→轮次→状态闭环、多根部分成功）待 ISS-161/163；容量样本与摘要只读已落库事实，不隐式扫描 |
| api.py | 查询、非 daemon 扫描线程；Host/Origin/写令牌守卫；受监控根约束的 reveal；按真实前端内容 revision 提供冻结静态资源图，裸入口兼容。ISS-176 起 plan 身份**两档口径**（`7b280e9` 现状，与 ISS-155 时期「一律 409」占位闸门不同）：跨身份闸门 `_reject_mixed_plan_identity`（`api.py:464`）只拒「plan_id 不同」与「一侧新身份一侧 legacy」两类矛盾，全 legacy 放行走旧三元组、全新身份且 plan_id 相同放行（plan_id 已编码规范根/卷/计量版本/阈值/掩码）；可拒错误码 `plan_identity_unsupported`（`api.py:453`），窄行（旧库/测试夹具）无 `plan_id` 列按 legacy 处理（`_row_plan_id` `api.py:456`）。放行后的同口径判定由 `reports.same_dataset` 单一承担，四个消费者一致：`/api/diff`（`api.py:585`）、`/api/diff/children`（:650）、`/api/trend`（:768 经 `find_same_dataset_snapshot_rows`）、`/api/bigfiles`（范围查询）。`/api/snapshots` 下发 `plan_id`（`api.py:298`，新身份行非空、legacy `null`），前端 `datasetKey` 据此分两档（plan 档不拼三元组）。`/api/volume-trend` 无身份参数可拒，窗口与锚点限定 `plan_id IS NULL`（`api.py:333`/`:342`）；legacy 行行为逐字节不变 | 打包态主界面已有 ISS-009 切片 2 实机记录；权限、更新等发行路径仍待对应父卡；helper 侧 storage_scope 仅形态读取接缝（lib.rs），非行为接线；AI 解读对新范围身份的拒绝闸门独立于此闸门（稳定 code `plan_identity_unsupported`），未随范围能力放开 |
| cli.py | scan/report/bigfiles/status/serve/install/uninstall；scan 可标记 cli/scheduled 来源；report 按同数据集前驱生成（可 --snapshot-id 指定 b），无前驱明确文案并非零退出；`--version`；serve 支持 `--port/--port-range` 让位与零击杀。ISS-154 起 scan 支持 `--scope`（可重复，采集顺序=参数顺序）、按位置配对的 `--scope-id`、整轮时限 `--round-timeout-s`（缺省为「每范围 du 时限 × 范围数」）；`--scope` 与 `--root` 互斥并 exit 1，ISS-155 补齐范围参数错配时同样以可读原因 exit 1 而非抛 traceback | 与 API 共用协调合同；install/uninstall 仍是开发版入口 |
| launchd.py | 开发版 XML/plist 安装；只读 `status()`/`dry_run_plan()`；ISS-010B 发行态写路径 `release_plan/register_release/unregister_release`（confirmed 门+失败回滚，fake 全覆盖）；ISS-016B 只读 `read_registered_scan_time()` + `service_reload_state()` 纯函数；Rust `autostart.rs` 提供状态/注册/注销桥（ISS-010B，恰四权限） | 真实注册/重载/睡眠恢复留 G8 实机门；SMAppService 登录项恒 unknown（不引 objc）；发行 plist 未钉 EnvironmentVariables（G8 核对项） |
| frontend_resources.py | 构造时读取完整前端文件图并计算 SHA256 revision；相对 parent fd 与 stat/open/fstat 校验固定根及目录链，拒绝符号链接/替换越界；冻结字节按 `/_fathom/resources/<revision>/` 提供，未知 revision 404；HTML/裸入口/manifest no-store、版本空间内其它文件 immutable | 读取仅限已指定资源目录，快照/分析数据不参与 revision |
| frontend/ | 无构建链原生 ES modules：modules/ 下 request（世代号+pageScoped 防倒序覆盖、apiPost/apiPut 写令牌）、format、charts（隐藏 stale/重显 resume）、polling（幂等单实例）、tauri（浏览器降级）、router（hash 路由+单一刷新入口）、status 与六页 enter/leave 模块；设置页读 `/api/config`+`/api/status` 渲染真实值并可编辑保存（ISS-016A，校验以服务端为准、失败保持旧值可辨）；R3 测深视觉签名已实装（ISS-072：海沟蓝 token、深度环字标、等深线纹理、tabular-nums）；`favicon.svg` 使用深度环、`apple-touch-icon.png` 使用深潭位图（DEC-023 双形态，与 App 深潭图标分别维护）；ECharts 本地 vendor | 打包态真实 WebView 下页面渲染已于 2026-09-18 实机验证（ISS-009 切片 2）；移动布局未支持 |
| apps/desktop/ | Tauri 2；显式授权 update_tray_status；单一 sentinel tray 绑定图标/菜单/事件并更新状态行；打包态 `helper.rs` 负责冻结 helper 的 locate/spawn/握手（陈旧 `helper-instance.json` 经 `/health` 探活 + 只读 pid 判定后跳过，`d53a7af` 起）/让位/幂等回收，端口耗尽经 `helper_status` 以 `state=exhausted`+`recovery` 交握手页渲染并可 `helper_retry`；`bundle.resources` 深键 map 把 helper 落到 `Contents/Resources/helper/`；`scripts/build_helper.sh`/`build_app.sh`/`verify_app_bundle.sh` 产出并校验未签名 .app/.dmg（22 项）；图标为 ISS-045/DEC-023 双形态体系（App 图标=层叠深潭位图系：`assets/brand/` 原稿 + `scripts/build_app_icon.py` 抠图 1024 + `build_icons.sh` 全尺寸；界面小尺寸=深度环矢量系：tray 为 `make_tray_icon.py` 单色 template `icon_as_template(true)`，几何由 `ci_brand_geometry.sh` 门禁同步） | 未签名、未公证、仅 arm64；新账户首启、含空格/中文路径、tray 菜单实机退出未验（握手页 exhausted 已于 2026-09-18 实机验证）；图标 18pt 小菜单栏下 tray 可辨性未实测（Launchpad/Spotlight 与深浅色模式已于 2026-09-19 实测通过） |
| src-tauri/src/lib.rs 更新入口 + settings.js 更新区 | ISS-040B 注册 updater/process 插件；应用级三命令与最小 ACL；延迟检查、确认安装、独立确认重启；安装委托插件；升级 prepare 的有界等待中轮询回收本壳已退出的 helper 子进程，避免兄弟协调进程的 `ps -p` 把僵尸 PID 判为仍在运行（只回收、不发退出信号；存活 helper 和外部实例保护不变）；壳内存持有更新状态/活动终态及 generation/revision，复用 `updater_check(recover=true)` 只读回读，后台重启导致页面换 origin 后由设置页恢复；自动检查保留失败/待重启终态，较旧回读与事件不得覆盖新操作 | 生产公钥与更新源已配置（G10，v0.3.1 起公开启用）；`updater_install` 已接入六步升级协调（停写/旧 helper 退出/一致备份/进度与取消边界，ISS-040C），helper 正常退出自清实例文件的兼容已修（ISS-096）。升级协调六步与恢复闭环已在代码层落地（ISS-096/097/098/102），恢复经隔离冻结形态三类故障注入验证；真实 .app 全链、签名/公证与发行矩阵实测 NOT_VERIFIED（父卡 ISS-030/ISS-041）。重启回收不等于安装前协调 |
| agent_runtime.py | Runtime 注册表（四家候选：claude-code、codex-cli 0.147.0 与 hermes-agent 0.21.5 ready——claude 严格禁工具（DEC-029）；codex OS 级 read-only 沙箱防破坏门（DEC-030，ISS-126：写/删/破坏命令负例有实证，读任意本机文件与联网为用户裁决披露事项）；hermes 调用级只读工具集防破坏门（DEC-030，ISS-126：`chat -t web,vision` 替换收敛为 web_search/web_extract/vision_analyze 三件，写/删诱导负例零 tool_use 零副作用，`--safe-mode` 单独不构成边界、仅与 `-t` 并用）；zcode unsupported 且有可复查证据与复核条件）；预算化探测（16 候选/单命令 3s/总 20s/输出 16KiB，登录 shell 兜底禁 eval）；公共 `AgentCliRunner`（shell=False、stdin 载荷、进程组 TERM→3s→KILL、stdout 256KiB/stderr 64KiB 限额、UTF-8 严格解码、成功三关）；父进程存活看门 shim（`_agent-supervisor` 直通子命令，冻结包内三路径实测 ISS-125）；Claude 适配器（--bare+禁工具组合）、Codex 适配器（exec --json+ephemeral+ignore 三 flag+read-only 沙箱，JSONL 解析：turn.completed + 该事件**之前**最后一条非空 agent_message 判成功（Codex 会把进度说明也发成独立 agent_message，实测 0.147.0 最多三条且 item 无 phase 字段，故按流顺序取末条而非按 id 排序；收集在 turn.completed 处截断），降级 error item 披露不阻断、响应缺失判 app_error）与 Hermes 适配器（chat --query-file - stdin 载荷+stream-json+run-budget 150+-t web,vision+ignore 双 flag+--source tool，JSONL 解析：result 事件 exit_code 0 且 text 非空判成功，error 字段/exit_code≠0 判 app_error，只读工具集内 tool_use 计数披露） | 认证形态 unknown（版本探测不解释登录）；zcode 待官方提供工具白名单/全禁开关后重审（ISS-035E）；codex 版本锚定 0.147.0、0.158.0-alpha 未验证不开放，hermes 版本锚定 0.21.5、`-t` 替换语义变更需重审（ISS-126）；hermes 失败请求会在其自身 ~/.hermes/sessions/ 留 request_dump（成功 oneshot 不落会话文件） |
| analysis_contract.py | 同事务事实包构造（同口径拒绝/程序净变化/三态条目/全局 100 条/128KiB 截断/canonical digest）；版本化 prompt（指令与数据分隔）；有界 JSON 验证器（31 类拒绝路径）；`deterministic_diff` 修 compute_diff 跨进程序不定 | 结构验证不证明语义正确（质量门由 13 个固定样本+真实跑保证，ISS-035D） |
| analysis_manager.py | 预览（TTL 300s/8 份/冻结字节+双 digest+双 revision）；独立租约单在途；九态状态机终态单写；成功三关+验证同事务落库；提交资格门；升级双租约次序不变量；保留 35 天/100 条；历史过期读取层评估 | 跨会话在途 job 发现经 ISS-120 查询端点；冻结包端到端归实机批次 |
| api.py（analysis 段） | 六端点：detect（POST+令牌）/previews/jobs（三字段严格）/jobs{id}/cancel/analyses+delete；GET 无副作用；错误稳定 reason_code 不透出 stderr/路径 | detect 无 recommended 元数据（前端首位硬编码，后端补后改驱动） |
| apps/desktop/experiments/iss029/ | PyInstaller onedir 与 helper 生命周期合同原型；只写指定数据根，结果被版本化规则忽略 | 生产 `build_helper.sh` 默认复用其 `.venv-build`（PyInstaller 6.22.3）冻结 helper 并打进 `.app`（`a158889` 起）；x86_64 未冻结（ISS-041） |

## SQLite 与保留事实

| 表 | 字段概要 | 含义 |
|---|---|---|
| schema | `PRAGMA user_version=8`（`db.py:SCHEMA_VERSION=8`） | v0–v7 开发库经完整性/结构校验和一致备份后逐级事务迁移；未来版本、损坏或不兼容结构拒绝打开；v8 新增身份/轮次表外键（`scan_plans.scope_id`→scan_scopes、scan_round_members.round_id→scan_rounds 级联） |
| snapshots | id, created_at, root, dir_count, denied_count, du_seconds, total_kb, min_kb, collection_status, vanished_count, exclude_names, confirmed_missing_count, path_unverified_count, plan_id, round_id, metric_version | 时间为本地无时区 ISO 字符串；目录总数包含未持久化小目录；min_kb/collection_status 自 v3 起持久化，旧行 NULL 不补造；v4 新增 vanished_count 默认 0，v5 新增 exclude_names 默认空串（规范掩码串）；v6 两个细分计数对旧行保持 NULL，新扫描显式写入且和等于兼容汇总；v8 三身份列对 legacy 行保持 NULL 不补造 |
| scan_scopes | scope_id, … | 范围/卷/容器稳定 ID；数据集身份在新口径下等于计划 |
| scan_plans | plan_id, scope_id, canonical_root, metric_version, min_kb, exclude_names | 规范根计划；身份五元组载体，`plan_id` 即数据集身份 |
| scan_rounds | id, started_at, finished_at, status, … | 扫描轮次，成员各自时间/状态 |
| scan_round_members | round_id, snapshot_id, scope/plan 关联, status, … | 轮次成员；`snapshot_id` 无外键（旧库/保留策略可空），随轮次级联删除 |
| container_capacity_samples | 容器/卷容量样本（含来源与时间） | 整盘容量读数单独留痕，容器 free 为唯一权威、卷级 free 恒空 |
| snapshots | id, created_at, root, dir_count, denied_count, du_seconds, total_kb, min_kb, collection_status, vanished_count, exclude_names, confirmed_missing_count, path_unverified_count | 时间为本地无时区 ISO 字符串；目录总数包含未持久化小目录；min_kb/collection_status 自 v3 起持久化，旧行 NULL 不补造；v4 新增 vanished_count 默认 0，v5 新增 exclude_names 默认空串（规范掩码串）；v6 两个细分计数对旧行保持 NULL，新扫描显式写入且和等于兼容汇总 |
| entries | snapshot_id, path, size_kb | 复合主键，WITHOUT ROWID；父目录大小已含子目录 |
| volume_stats | snapshot_id, total_bytes, free_bytes | 扫描时 statvfs，free 为 f_bavail × f_frsize |
| scan_runs | id, started_at, finished_at, status, message | API/CLI/定时统一写 running/done/failed/interrupted；done message 保留兼容结果，failed/interrupted 为错误或取消原因 |
| scan_run_details | run_id, source, phase, owner_*, heartbeat_at, snapshot_id, report/notification 状态, pruned_count, warnings | 自 schema v2 引入的一对一阶段详情；来源为 api/cli/scheduled，快照成功与报告/通知结果分开 |

协调器取得 `flock` 后才把遗留 running 记录收尾为 failed；无法取得锁时返回 busy 并只展示非权威 owner 元数据，不以 PID 或超时推断 owner 已死。扫描、报告、通知和保留逐阶段提交，首扫保存有效快照且 `report_status=not_available` 时整体成功；报告/通知失败作为警告，不抹掉快照。API shutdown、CLI SIGTERM/KeyboardInterrupt 与超时只回收本次会话拥有的 `du`，最后释放锁。

同数据集（同根同 `min_kb` 同 `exclude_names` 口径）同一天的新扫描在判定采集有效后，才进入“删除旧快照并写入新条目和卷统计”的事务；更换阈值口径属另一数据集，同日共存不互相替换。致命非零退出、信号退出、空输出、缺失根记录、负数和混合/非权限错误都在写入前失败；有明确权限/瞬时错误或 `du` 输出路径在后校验时不存在/无法确认、根记录有效且数值非负并通过无歧义解析时可记录为部分覆盖。新扫描先以 `stat` 区分 errno；遇 `ENOENT` 再用 `lstat` 排除断开的符号链接，二者均确认不存在才计入该类；权限或其他 `OSError` 表示状态无法确认，实存非目录、根外仍拒绝采集。测量时的 `du` 数值保留，私人受限路径不另存。事务中的 SQL 错误会整体回滚，保留原有效快照。

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
| GET | /api/status | 实时卷容量、根、快照总数、最新元数据（含可空的路径细分计数）、db_bytes、scan、port，以及不含令牌/凭据的实际 runtime 路径与模式 |
| GET | /api/permissions | 本机完全磁盘访问探测、通知状态与带时间的最近快照覆盖记录；受限错误为 `du` stderr 行数，路径细分字段旧快照为 NULL，授权探测不重写历史 |
| GET | /api/snapshots | 全局快照降序列表，含卷统计与 min_kb/collection_status/vanished_count/exclude_names；v6 行另有 confirmed_missing_count/path_unverified_count，旧行两项为 NULL；v8 起**下发 `plan_id`**（`api.py:298`，新身份行非空、legacy `null`），前端据此做两档数据集收敛 |
| GET | /api/storage/summary?limit= | 容器容量与目录归因摘要（ISS-157，`api.py:1347`，只读**已落库**事实，请求本身不触发 du/statvfs/发现调用，更不隐式全盘扫描）：共享 free 只计一次（容器级 `storage.shared_free_once`，卷级 free 绝不相加）；归因只用互不重叠测量根（`storage.non_overlapping_roots`，子根列为已吸收）；`unexplained` 为有符号未知差额并带「尚无法由目录变化解释」限制，不可比则 `null` 且给出 reason（缺失读数不补 0）；失败成员的旧有效值标 stale；整轮跨时间给成员各自时刻与显式跨度；无覆盖率字段 |
| GET | /api/volume-trend?limit= | 以最新快照为锚取最新 N 条后正序输出；按数据集 (root, min_kb, exclude_names) 隔离，跨根/跨阈值不混点 |
| GET | /api/trees?snapshot_id=&min_kb= | 最新/指定快照目录树；默认 ≥51200 KiB；节点预算在 SQL 层生效（LIMIT+1 探测），响应含 truncated/matched_count/node_count/node_limit；截断时子孙提升为顶层且无孤儿重复；指定快照不存在 404、空库 200 snapshot_id=null、低于阈值 200 空结果；root=/ 归一化不再成为自己的孩子 |
| GET | /api/diff?a=&b=&topn= | b 相对 a；默认 a=最新快照的同数据集前驱；不足/无同数据集前驱 409，不存在 404；a/b 跨根、跨阈值或跨排除集 400 |
| GET | /api/diff/children?a=&b=&path=&cursor=&limit=&filter=&sort= | 绑定历史区间的同级差分（ISS-147）：a/b 必填、严格按请求区间不换向；path 按段在数据集根内，越界/`..`/空段 400；返回 dataset/ancestors/parent/children/pagination/counts；行为含 old_kb/new_kb、nullable delta_kb、measured/first_recorded/unrecorded/structural 四态、has_children/has_changed_descendants；单侧缺测与结构节点不填 0；filter=changed 保留有命中后代的导航节点（父净 0 不漏）；游标 base64url 绑定 v/a/b/path/filter/sort，错配 400；limit 默认 100 上限 500；实现于 hierarchy.py（entries 主键前缀范围扫描+双侧有序归并，不整快照装载） |
| GET | /api/trend?path=&limit=&anchor_snapshot_id= | 无锚时以最新记录该路径的快照为锚取最新 N 点后正序输出，按数据集隔离；缺失不补点（保留 gap）；无记录路径 200 空 points。`anchor_snapshot_id`（ISS-149）按该快照的数据集身份取同数据集快照窗口：缺条目 size_kb=null+recorded=false 不补 0，每点带 snapshot_id 与完整扫描时间，响应含 dataset/total_snapshots/truncated；缺失锚 404、数据集内从未记录 200 全 null；谓词统一走 reports 同数据集辅助 |
| GET | /api/bigfiles?days=&min_mb=&topn=&mode=&path=&wait= | 经 BigfilesManager 显式触发/去重/TTL；返回 state、task_id、scope、stats（wall_ms/peak_rss_bytes/find_output_lines/find_exit_code/…）、files、truncated、cached、cache_age_s、error_message；topn 上限 200。`wait=false`（缺省 true）立即 202 返回 task_id/state/scope 不阻塞等 find（ISS-164）；`task_id` 由去重键确定性派生，同参去重与按句柄寻址指向同一任务 |
| GET | /api/bigfiles/status?task_id= | 纯读、无需写令牌；返回真实状态（在途 running，终态为既有六态或 cancelled）+ terminal/cancel_requested/created_at/finished_at/error_message/scope；不含结果本体（仍从 /api/bigfiles 按原参数取）；未知或超出保留期 404，缺参 400 |
| POST | /api/bigfiles/cancel | 需 `X-Fathom-Token`；body 只收 `{"task_id": ...}`（严格拒绝未知字段），只取消该句柄的 find 进程组（SIGTERM→SIGKILL，只回收自有进程）；已取消/已完成幂等 200 且状态如实，未知 404；句柄即凭证，越权面由写令牌闸门承担 |
| POST | /api/scan | 需 `X-Fathom-Token`；先取得跨进程 `flock` 再落 running；冲突 409；成功返回 run_id；线程启动失败 503 并释放租约 |
| GET | /api/scan/status?history= | 返回最新统一状态、source/phase/snapshot/report/notification/pruned/warnings；history=1..100 附 API/CLI/定时运行 |
| GET | /api/browse?path=&snapshot_id=&cursor=&limit= | 目录子目录、同数据集前驱差值、趋势、面包屑；无基线时 delta_kb=null、is_new=false。缺省 snapshot_id 恒绑最新快照（旧行为）；显式 snapshot_id（ISS-159）以所选快照身份约束路径，主读数只指该快照（measured/structural，不填 0），与前一可比快照的差分仅为次级且 comparison 显式携带基线（单快照或跨口径为 null），直属子目录稳定分页（游标绑定快照/路径），质量字段随快照返回；快照不存在或路径无记录 404 |
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

版本单一源为 `fathom.__version__ = 0.4.0`（`fathom/__init__.py:10`）：API 文档、Tauri config、Cargo package 与 Cargo.lock 本地包行同源，`scripts/check_version_consistency.sh` 在任一漂移时 fail-closed（一致 0 / 漂移 1 / 缺失 2）。接口实际合同以后端代码为准。

## 当前验证覆盖

固定基线 `31396a0` 的本地 pytest 配置门禁为 **671**，ISS-040C 后本地口径 **685**；ISS-037 repair3 后 **696**（新增 `tests/test_third_party_notices.py` 11 项 notices 许可证值级核对用例）；ISS-081 后 **697**（通知语义固定 `_volume_stat` 受控输入+空间告警真实链路受控用例，满盘机器门禁不假红）、ISS-091 后 **699**（API 契约测试 +2）、ISS-090 后 **720**（新增 `tests/test_scan_progress.py` 21 项：du 流式计数对拍/状态文件节流写入/live 心跳判活/慢速回放 du 的 run_du 与 run_scan 集成）、ISS-096 后 **727**（`tests/test_upgrade_helper_exit.py` 7 项：升级准备兼容 helper 正常退出后实例文件清理）、ISS-097 后 **739**（`tests/test_upgrade_txn_journal.py` 12 项：持续停写/journal 所有权/中断恢复）、ISS-098 后 **753**（`tests/test_upgrade_restore.py` 14 项：恢复材料/三类故障注入/恢复接续）、ISS-101 后 **766**（`tests/test_release_gate.py` 13 项：发行门 SHA 绑定/必需 job/tag↔版本/指纹判定）——CI/`ci_pytest.sh` 均已同步；ISS-040B 任务记录的前端检查为 **105**、浏览器/API **39**、`cargo test` **49**（ISS-040C 后 **57**：lib.rs 新增 8 项内联单测，`cargo test --locked --offline` 实测 57 passed）。ISS-040C 已把 `updater_install` 接入生产更新协调入口（`fathom/upgrade.py` 六步协议 + 冻结 helper upgrade-* 子命令 + 进度/取消边界），030A 夹具仅作协议参照。覆盖扫描完整性、特殊路径真实 BSD `du`→bytes→SQLite、v0–v4→v5 迁移/WAL 一致备份、真实跨进程 `flock`、API 空库首扫、CLI/定时来源、报告/通知故障、SIGTERM/超时回收（含超时进度条数线索）、扫描进度实时计数（ISS-090）、Host/Origin/写令牌、reveal 越界、前端重扫/乱序/错误状态、CSP 及浏览器资源清理。GitHub Actions 已恢复实跑（2026-09-26 起 main 全绿：双架构 pytest、API/浏览器+前端 refresh、双架构 cargo locked build+单测、品牌几何；ISS-101 起发行门把同 SHA CI 结果绑定进 release 判定）；构建成功不等于功能回归通过。

**0.4.1 发行基线门禁口径**：pytest **1517**、浏览器/API **39**、前端 refresh **217**、AI 解读前端 **77**、树形变化 **74**、目录限定大文件 **36**、分布页快照绑定 **33**、范围设置 **37**、整盘总览 **52**、跨页排查 **55**、升级状态恢复 **16**、Rust 单测 **77**。权威值在 `.github/workflows/ci.yml` 的 `EXPECTED_*`；本地 `ci_local.sh` 同步配置。`ci_pytest.sh` 的默认1463须由完整入口覆盖，`ci_cargo_locked.sh` 默认77与CI一致。ISS-179新增父壳回收真实子进程回归；ISS-182新增壳内呈现状态机与双origin前端IPC桥回归，桥夹具不替代打包GUI验证。空间排查的受控卷、整盘plan和跨日实测仍由ISS-161/163独立验收，不因本次更新热修关闭。

已有历史实测记录：ISS-009 切片 2 的打包态主界面/端口耗尽页、ISS-045 图标入口、ISS-001 的两个有效定时日期与日报。仍 `NOT_VERIFIED`：系统通知实际展示、完整 tray 菜单与发行生命周期、新账户/断网/实际下载首启、原生 x86_64 冻结及真实更新。Developer ID 签名/公证/stapling 按 DEC-022 延期，未通过，不可与 updater 签名混同。
扫描回归包含真实 du、小目录阈值、同日覆盖、差分、保留及失败前不写入；安全浏览器夹具使用合成临时根和结构化 `DuResult`，不会扫描生产 HOME。折叠回归已移除恒真断言，并覆盖 `topn=1` 的父子替换、独立高排名目录、根路径、相似前缀、尾斜杠、正负变化与大输入复杂度。早期隔离反例与页面实测见 2026-09-12 项目内部审查记录（未随公开库分发），隔离操作见 [TESTING](TESTING.md)。

不把现有单元测试、其他分支的提交说明或 cargo build 作为安装、系统通知、权限、tray 与定时任务已经可靠的证据。


## 桌面前端资源身份

`GET /api/frontend-manifest` 返回只读 revision 与 entry_path；`/health` 同时给出 frontend_revision。打包壳及首屏 loader 首次直接导航该版本空间，覆盖所有相对 ES module、CSS 与图片依赖。当前 origin 和 revision 都匹配时保留页面内存及 hash；端口或资源变化时重新导航并保 hash。缺少有效 revision 的旧 helper 显式显示握手失败，不把裸缓存页标成当前版本。CSP 与更新信任不变；资源身份不扩大扫描或分析读取范围。

## macOS 主窗口滚动边界（ISS-114）

`apps/desktop/src-tauri/src/scroll_boundary.rs` 隔离平台限位：setup 在首次 helper 导航前，对 main WebView 通过 `with_webview` 主线程闭包应用原生回弹 mask=0。调用前检查 selector 与 ABI；不支持时输出诊断并保留窗口正常运行，不启用全局 macOSPrivateApi 配置。页面 `.page-container` 同时采用 `overscroll-behavior:none`，阻止内部容器自身的越界反馈，普通内容滚动保留。

该 native selector 属于 WebKit 私有 SPI，适用性需运行时检查及系统版本实测；本模块不改变 helper、扫描或更新事务。生命周期与视觉证据须分开：初始化 mask 读回及导航保持证明接线，真实物理手势证明用户可见效果。

### 桌面默认启动盘计划

启动盘发现固定查询 `/`，不把任意扫描目录传入 `diskutil info`。API 的启动盘空候选由已发现的容器、卷及可见入口生成根与稳定卷 ID，未挂载/锁定/归属未知入口不猜测。`config.default_startup_scope_requested` 只判断默认意图；release 模式未选范围且无持久化根、环境根或 CLI 固定根时，CLI/API 的主动采集入口解析并原子保存默认计划，再共用多范围协调器。只读配置/预览不保存也不采集。开发隔离与已有自定义设置保留，旧 legacy 快照不回填计划身份、不与新计划混比。

范围已启用时，普通设置合并保留 `ScopeSelection` 与分析配置的内部对象类型，线协议字典仅用于序列化；显式 CLI/环境扫描根先于持久化范围生效。设置更新采用进程 RLock → 运行根稳定 `.settings.lock` 的 flock → 读取并校验磁盘当前值 → CAS/合并 → 原子保存 → 发布进程视图的单一事务；范围与普通字段共用同一写链，不嵌套 flock。通用 `update_user_settings` / `PUT /api/config` 拒绝任何 `storage_scope` 键（含null与混合字段），在锁、落盘或发布其他字段前报400；范围保存只经专用版本入口，不能用整体patch回退修订。旧文件解析与只读回显仍兼容。陈旧范围版本返回409，拒绝失败patch并刷新为已验证的磁盘当前配置；校验、锁或写入失败保留旧文件与旧内存。锁文件持续保留，避免unlink导致锁inode分裂。启动盘采集前重新只读发现并校验卷、容器及挂载入口绑定，同一规范入口的所有发现归属必须唯一且属于该卷和容器；将已验证的卷类型、容器和卷组身份保留到采集规格及落库范围，未知、不匹配或跨卷/设备歧义停止采集。旧范围首次登记身份与新发现冲突时明确拒绝，不静默回填历史；身份不足的整盘摘要仍保持不可比。

`process_scan_root_override` 为 CLI/API/设置范围呈现共用的只读覆盖判定，CLI 优先于环境；范围读接口附带 `scan_override`。无参数计划预览在覆盖时返回 `plan=null`、`plan_source=process_override` 并说明真实 legacy 单根，显式候选预览标为 candidate。保存仍持久化选择，不解除本进程覆盖；前端范围卡分开呈现保存选择和实际根。

## Agent 变化解读与历史中心（独立入口）

`#/agent` 由 router 统一进入、刷新和离页；Agent页按同一 `datasetKey` 校验真实历史区间，查询跨区间任务列表，选择原任务后按记录ID读取保存原文。`components/analysis-panel.js` 供变化页与Agent页使用，隔离DOM挂载点、区间获取及请求域，共用既有预览、确认、幂等、轮询、取消、结果/证据和撤销流程。离页不取消后台授权任务，不隐式发送；改选/离页作废各自请求域。

新增纯读 `GET /api/analysis/history?limit=20&offset=0`（limit 1–100、offset非负）提供跨区间生命周期分页，在途优先；列表只含任务及过期元数据，不展开事实包和正文。`GET /api/analyses/{analysis_id}` 按ID读取仍保留的原文与证据，沿用过期评估及撤销404语义。旧带a/b的查询保持兼容；启用状态不限制历史读取，新plan身份依旧不能生成AI预览。底层保留与删除规则未改变，历史仅代表本机仍保留的记录。

### 跨进程设置事务的兼容边界

事务读入既有设置时验证结构与身份，但允许保留已失效的旧目录或引擎资源路径，使无关设置更新及关闭失效引擎仍可恢复；正常启动与新提交字段继续严格检查实际资源。环境排除集覆盖保留范围与分析配置对象，并在写盘前验证最终生效配置。事务不清理旧settings、数据库或未知临时文件；仅磁盘版本冲突会刷新进程配置，其他失败不发布新视图。
