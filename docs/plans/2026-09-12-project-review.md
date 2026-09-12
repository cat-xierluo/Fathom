# 全项目审查与证据

日期：2026-09-12；代码基线 main=`33e81f9`；规划任务 ISS-017。**这是该基线的历史审查，不是实时缺陷队列。** 修复状态只在 [TASKS](../TASKS.md) 维护；重现时必须记录所测 commit。

## 判断

现有 Python + SQLite + 原生前端 + Tauri 组合足以继续迭代，没有证据要求整体重写。主要缺口是扫描失败/缺失数据的语义、前端状态与真实交互、运行时隔离，以及从开发壳到陌生用户安装的完整生命周期。新增智能化应该建立在这些事实可靠之后。

旧计划过早把 `scan_runs` 作为小优化、把 `.app` 当作图标/打包任务，并把功能列表与版本号直接绑定。当前改为结果门槛：先防止事实错误，再升级 UX、验证分发、最后引入可选解释。具体方向由 [ROADMAP](../ROADMAP.md) 维护。

## 范围与证据等级

已读：全部 Python 业务模块、前端页面/脚本/CSS、Tauri 配置/capability/loader/Rust 入口、现有测试与项目文档；另读 ISS-003/007/008 分支 diff。测试使用独立工作区及生产 venv 的解释器，源码来自冻结基线；测试数据、根、报告、日志与端口另行注入。

- **实测**：在合成目录/数据库运行真实入口，或用 mock 子进程注入明确故障，并检查真实生成的数据库/响应。
- **代码确认**：可由代码直接证明，未声称已在生产发生。
- **待实机**：Tauri 菜单栏、TCC、登录/睡眠、干净账户、跨日和签名行为；不能用代码阅读替代。

本次没有调用生产 `scan/install/uninstall`，没有读取/提交生产日报或目录清单。浏览器只连接隔离夹具服务 `127.0.0.1:8799`。

## 已识别问题

| 证据 ID | 等级/影响 | 依据与具体结果 | 任务 |
|---|---|---|---|
| AUD-01 | 实测 / P0 数据丢失 | `scanner.run_du/create_snapshot` 不检查 du 返回码。注入 returncode=1、空 stdout、读错误 stderr 后，已有 20000 KiB 当日快照变 0，原 id 被删除；du_seconds=0 | ISS-018 |
| AUD-02 | 实测 / P0 路径错误 | 在真实目录创建字面反斜杠+t、内嵌换行、中文名称后执行 BSD du；前两者键无法原样找回，中文能找回。纯 unescape 样例与真实命令假设不一致 | ISS-019 |
| AUD-03 | 实测 / P0 首扫错误 | 临时空 DB 调 POST scan，返回 200，最终 error 为“至少需要两个快照才能生成对比报告”，但 snapshot_count=1。CLI 对同类 ValueError 有单独处理 | ISS-020 |
| AUD-04 | 实测+代码 / P0 事实误导 | entries 缺失直接进入 removed；11 MiB→低于 10 MiB 与真实移除无法区分。单快照 browse 的子目录 delta 实测为完整 20000 KiB，无比较基线仍看似增长 | ISS-021/024 |
| AUD-05 | 实测+代码 / P0 混根 | 两个根在 40 天前同一 ISO 周各一快照，prune 后仅剩一根。diff/日报取全局最近两条，write_daily_report 未使用 sid 选择目标；跨根报告可错配 | ISS-021 |
| AUD-06 | 实测+代码 / P0 本地边界 | `root/../outside` 在存在性检查后返回 200 并调用 mock open；不启动真实 Finder。TestClient 的不可信 Origin POST scan 被接受；CORS 未提供写入鉴权。文件路径直接进入 HTML tooltip（见下方浏览器证据） | ISS-022 |
| AUD-07 | 浏览器实测 / P1 误显与过期 | `fmtDelta(-1024)` 显示 `1.0 MB`；Finder 按钮出现源码字符串；重扫后 selector 仍 [2,1]，后端为 [3,1]，表单保留已删除快照 | ISS-023 |
| AUD-08 | 代码确认 / P0 隔离不足 | config 中 FATHOM_DB 只覆盖 DB_PATH；REPORTS_DIR/LOGS_DIR 仍指向源码工作区，DEFAULT_ROOT 仍为 HOME，端口仍固定 | ISS-025 |
| AUD-09 | 代码确认 / P1 查询错误 | volume-trend/trend 使用 ASC LIMIT 取最旧 N；树先全量取行后切 20000；root=/ 被 rstrip 变空；缺失历史点无质量信息 | ISS-024 |
| AUD-10 | 代码确认 / P0 并发与生命周期 | CLI/launchd 不共享 API 的 threading.Lock；API 用 daemon 线程。两条入口的报告/保留顺序也不同。ISS-007 分支持久化状态但仍不能协调不同进程 | ISS-020 |
| AUD-11 | 实测+代码 / P1 验证不足 | 基线 9 passed；折叠测试有 `assert ... or True`。Python 依赖只有最低版本，运行/测试混在一份 requirements，无 CI | ISS-031 |
| AUD-12 | 实测+代码 / P1 折叠与留存 | 父 +100000、子 +99000、topn=1 时直接返回父 `/r`，提前 break 阻止下沉。weekly_cutoff 从今天向前 12 周，旧文档“约9个月”错误 | ISS-021 |
| AUD-13 | 实测+代码 / P1 服务安装 | 路径 `/tmp/A&B/...` 经 `_scan_plist` 生成的 XML 被 plistlib 拒绝；bootstrap 非零只打印，调用入口仍可返回成功 | ISS-029/030 |
| AUD-14 | 代码确认+待实机 / 发行缺口 | bundle.active=false；依赖外部 venv/launchd/固定端口；无运行时迁移、完整安装卸载或干净账户验收；版本 0.2/0.3 漂移，许可证仍私有声明 | ISS-009/029/030/037 |
| AUD-15 | 代码确认+待实机 / tray | 配置 trayIcon 与 Rust TrayIconBuilder 两处创建路径需核查实际数量；Rust status MenuItem 未随标题更新；隐藏页面定时器与恢复行为未验证 | ISS-008 |
| AUD-16 | 代码确认 / P1 资源与反馈 | bigfiles 每次 GET 同步全量 find，无超时/缓存/并发去重；stderr 被忽略；日志/报告无预算，db_bytes 未算 WAL/SHM | ISS-032 |
| AUD-17 | 浏览器+代码 / P1 UX 不完整 | 只有部分区域处理失败；总览 catch 将各种错误附上“需要两个快照”；大文件进入即扫描；设置含旧库名/硬编码；键盘不可见的行按钮、图表优先于结论 | ISS-026/027/028/016 |

本地接口测试证明服务端拒绝边界缺失，**没有宣称已从任意网站穿透所有浏览器的本地网络限制**。tooltip 证据是合成文件名送入现有渲染函数，不是发现生产路径中有攻击文件。没有发现或宣称远程任意命令执行。

## 执行证据摘要

### 基线回归

在冻结工作区执行 `.venv/bin/python -m pytest tests/ -q`（解释器来自主工作区同版本 venv）：`9 passed in 0.35s`。未对生产数据库运行 pytest。环境：Python 3.14.6、FastAPI 0.141.1、Starlette 1.6.0、uvicorn 0.52.4、httpx 0.28.1、pytest 9.1.1；不是已锁定的发行依赖承诺。

### 后端/脚本探针

1. 配置 `DATA_DIR/REPORTS_DIR/LOGS_DIR/DB_PATH/DEFAULT_ROOT` 全部置于临时目录，FastAPI TestClient 运行真实 handler 与 SQLite。
2. 首扫注入有效 du 结果 `{root: 20000}`，检查运行状态和存储数量；随后注入非零退出空输出，检查旧 snapshot id 与新 total_kb。
3. `reveal` 的 open 用 mock 替代，构造根内 `../outside` 指向同一个临时父目录的普通文件，检查副作用是否被调用。
4. 特殊文件名用实际 mkdir/写 4KiB 文件 + `/usr/bin/du` 验证，不以构造 stdout 代替真实命令。
5. prune 用同一周两根合成 rows，检查剩余数；plist 用 `plistlib.loads`；折叠用固定 parent/child + topn=1。

这些步骤是复现旧缺陷的方法；修复后的预期相反，按任务卡写回归。不要把本报告中的错误返回值写成新测试的通过条件。

### 真实浏览器

启动 uvicorn 8799，根与三类输出均临时目录，数据库预置两个日期及 Archive/Notes 合成路径；手动扫描替换为固定 fixture 的 run_du，不扫描真实 HOME。使用 ego-browser 走总览 → 变化 → 立即扫描：

- 总览按钮 DOM 文本为 `"+icon("folderOpen", 14)+"`。
- 页面执行 `fmtDelta(-1024)` 得到 `1.0 MB`，`fmtDelta(null)` 得到“新”。
- 重扫前 options 为 [2,1]；完成后 options 仍 [2,1]，GET snapshots 返回 [3,1]。
- tooltip 通过现有 `renderDeltaBars` 注入仅设置无害页面 marker 的合成名称，用 showTip 检查 HTML 是否被解释；实际 DOM 出现 `img[onerror]`（true），无害 marker 读取为 null。确认 HTML 被解释；未确认事件脚本执行，不能将其扩大为已验证脚本执行。

截图为临时合成数据证据，不入库；可按 [TESTING](../TESTING.md) 重建相同交互条件。服务与临时浏览器会话在审查后关闭。另从 TESTING 文档提取原样夹具（仅端口改为空闲端口 62800；首次选择 8800 被占用后退出，未停止该占用服务）启动真实服务：初始 2 快照、重扫后 IDs [3,1]、生成 1 份日报，全部通过。

## 既有分支的接续判断

| 分支/提交 | 已有价值 | 不能扩大解释的部分 |
|---|---|---|
| iss-003-scan-notification / 6488cf5 | 通知模块、报告写完后 hook、mock 用例 | 首扫没有日报不会走 hook；系统通知/阈值一致性待实测 |
| iss-007-scan-runs / 71fea47 | 手动运行记录、重启遗留状态处理 | 仍然进程内锁；不能推断其他进程没有 owner；首扫报告错误仍存在 |
| iss-008-tray-polish / 27b579f | 显式权限定义、错误日志、图标与标题容错 | 不以提交作者的“权限已修复”陈述替代真实 IPC/tray 证据 |

另一个会话在 integration/wave1 集成它们；本次不修改其工作区、不合并它们。发布/领取前刷新 main 与分支图。

## NOT_VERIFIED

- 次日 12:00 的真实定时触发与跨日首报（本次仍为 9 月 12 日）。
- 生产全盘性能、覆盖重要性、长期 DB/WAL/报告/日志增长。
- tray 真实数量/深浅色/睡眠恢复、TCC 权限、系统通知、重启/登录。
- 自包含 Python helper、macOS/CPU 支持范围、全新账户安装、升级、卸载、签名与公证。
- 所有远期本地依赖识别、Agent provider、标签质量与远程分析授权实现。

## 文档纠偏

去除“无任何成熟开源工具”“6 个受限目录影响很小”“全盘固定 5–15 分钟”“长期 DB 必定几十 MB”等无法由本基线充分证明的现状承诺。历史 DEC/CHANGELOG 保留，在新决策中注明适用范围和纠正；当前架构只写可核查事实。未来功能进入任务卡，不能因为有方案就标已完成。
