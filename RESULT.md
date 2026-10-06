# ISS-160 交付报告：跨页上下文、恢复与重扫核对闭环（独立前端套件）

基线：main `5a032d8`（worktree sparse，fathom/tests/.github/frontend/scripts）。分支 `iss-160-storage-investigation-journey`，两笔提交（工程 + ci 计数），未 push。

## 一、改动摘要

**本卡只新增回归套件与 CI 计数，未改动任何生产前端/后端文件。**

`git diff --stat 5a032d8..HEAD`：3 文件、+859 行、零删除、零生产代码改动。

| 文件 | 改动 |
|---|---|
| `scripts/verify_storage_investigation_frontend.cjs` | 新增独立套件（49 项）。真实 FastAPI 隔离入口 + 真实 Chromium 实点生产页面，单实例串行 |
| `scripts/ci_browser_checks.sh` | 追加 ISS-160 套件执行块 + `EXPECTED_INVESTIGATION_PASSED`（默认 49），同树形口径 |
| `.github/workflows/ci.yml` | 追加 `EXPECTED_INVESTIGATION_PASSED: "49"` |

**为什么不改生产代码**：ISS-160 的合同是「复用既有共享比较上下文/请求取消/焦点恢复能力，补一致绑定」，既有实现（148 sessionTree 跨页保留、151 共享查询域单例、159 browse 双形态、request 世代号 + pageScoped）在 49 项实点断言中**全部按合同工作**，未复现需要修的缺陷。真实流程中唯一"看起来不对"的三处，经查证都是既有正确行为或本卡边界外（见第三、四节）。

**套件机制**（与 ISS-159 同源，口径一致）：随机端口、合成快照种子（纯 DB，隔离运行根断言 fail-closed）、起服务前断言 `/health` 的 `pid` 等于自 spawn 进程且 `runtime_mode=development`、结束杀进程并复核端口释放。驱动只经真实 UI（选择器/点击/键盘/原生 select），不调用前端内部函数。三视口 980×640 / 1220×820 / 1440×900 截图。

## 二、验证（真实入口，全部实际执行）

环境：worktree `.venv/bin/python`；playwright@1.61.1 + chromium 装于 worktree `.runtime/playwright`（本卡显式授权）。为 `verify_api_security` 另建 `.runtime/bin/python` 包装器（`ci_browser_checks.sh` 既有口径，仓库不入库）。

| 命令 | 退出码 | 结果 |
|---|---|---|
| `node scripts/verify_storage_investigation_frontend.cjs` | **0** | **49/49 通过**（ok=true failed=0） |
| `node scripts/verify_api_security.cjs` | 0 | 39/39 |
| `node scripts/verify_frontend_refresh.cjs` | 0 | 217/217 |
| `node scripts/verify_analysis_frontend.cjs` | 0 | 77/77 |
| `node scripts/verify_tree_changes_frontend.cjs` | 0 | 74/74 |
| `node scripts/verify_scope_settings_frontend.cjs` | 0 | 37/37 |
| `node scripts/verify_storage_overview_frontend.cjs` | 0 | 57/57 |
| `node scripts/verify_directory_bigfiles_frontend.cjs` | 0 | 36/36 |
| `node scripts/verify_browse_snapshot_frontend.cjs` | 0 | 33/33 |

**既有八套件零破坏**（39/217/77/74/37/57/36/33 全绿）——本次未追加 pytest 用例，pytest 计数不变，故未单独提交 ci 计数（已在 ci 计数提交内说明）。

**TDD**：套件先红后绿——前 13 轮真实失败逐步收敛（红→绿）。红灯来源分两类，如实留档：
- 夹具/断言缺陷（已修）：总览「查看变化」入口需离开「正在读取…」占位；净变化行呈现的是**根同口径 GB 总量而非日期**；日期排序取错导致选出反向区间；树/详情重渲染导致 elementHandle 脱离 DOM（改每次按选择器重取）；分布页浏览器表在隐藏分区（须先点 `browse-tab-browser`）；`#chart-grown` 在隐藏 tab 内（改量当前可见页图表）。
- 真实产品行为（保留为断言，未改生产代码）：见第三节。

49 项对任务卡验收五组的覆盖（`verify-results/iss160-storage-journey/result.json`，截图 + `http-ledger.json` 129 条真实 API 请求关联台账）：

1. **J1 真实 serve 全流程**：总览结论文本非占位且渲染「查看变化」→点击进变化页（真实 hash 路由）→三层树展开（media→vault1→old）→详情绑定历史区间（`区间#3 → #4 之前3.7 GB 现在4.0 GB 净变化+293.0 MB`）→largest 当前文件（`当前大文件（此刻 st_size 实测，非 #3 → #4 历史区间）`，且进详情 0 次自动 bigfiles 请求）→返回后展开层数=2（148 sessionTree 跨页保留真实生效）→分布页历史快照 #1 →设置范围分区 →重扫产生新快照 #8 @2026-10-06T14:32:20。
2. **J2**：net0 内部变化走根同口径差分（`+488.3 MB 根目录 11.9 GB → 12.4 GB（行值不可相加）`）；历史非 latest 选中 a1→a2 后读数换成 `9.5 GB → 10.5 GB`（≠默认区间 11.9→12.4）；分页 130 子目录第一页 101 行 →「显示更多」追加至 130 且**唯一 130**（无重复无漏项）；旧 HOME（#1–#4）与新整盘（#5–#6）各自可查、行全属本根，**跨身份区间 `/api/diff?a=4&b=6` 实测 400**（不混比）。
3. **J3**：快速切页/改范围/重复请求后落到 `#sel-b=#4` 且净变化行与所选一致（旧 `+488.3 MB` 未残留）；受控 500 注入计数可数且**净变化未被伪造成该区间成功读数**；离页后新增 bigfiles 请求 ≤1（151 按句柄取消生效）；同日替换 a2(10-02 上午)→a2b(10-02 晚) 后读数为 `9.5 GB` 真实区间，**未捏造第二个跨日基线**；真实删行淘汰 a2b 后页面未出现「尚无快照」/NaN 错读。
4. **J4**：键盘 Enter 打开详情 → Esc 关闭 → 焦点回到行内按钮（非 NONE/BODY）；三视口横向溢出均 0px；跨页往返后 `#chart-volume` 宽 >100px 且内层 canvas 同宽（图表隐藏恢复）。
5. **J5**：撤除 500 替身后真实 `/api/diff` 仍 200 且 `b=#4`（生产 API 未被替身取代）；页面无测试替身全局变量；台账记录 129 条真实 API 请求（截图↔HTTP 关联，非页面烟测数）。`page.route` 仅一处受控故障注入，无系统动作替身。

## 三、缺陷登记（回原责任卡，本卡不改）

**D1｜变化页 a/b 快照选项未按数据集收敛**（实测）
`#sel-a`/`#sel-b` 的选项包含异数据集快照（实测 `#5`/`#6` 新整盘、`#7` decoy）。选中跨身份组合时后端 400（`http-cross-dataset-not-mixed` 已钉住），页面净变化行停在上一区间不更新。影响：用户可构造无效区间，界面无就地说明。
- 归属：变化页快照选择器（148/157 责任域），非 ISS-160 的 enter/leave 接缝。
- 处置：按卡片「发现层级职责缺陷回原责任卡/登记返修」，本卡不改该语义；套件显式记录该事实（`journey-snapshot-options-cross-dataset-exposed`），并在**同数据集内**断言历史区间，避免用缺陷态伪造通过。

**D2｜重扫后总览主体身份切换如实呈现（既有正确行为，已作断言）**
真实重扫扫的是配置扫描根（身份 ≠ 种子里 oldhome 系列），新快照 #8 与旧系列不可比。总览**不显示整盘结论、不把目录结果冒充整盘**（原文：`尚未启用整盘范围…也不把目录结果冒充整盘`），也不声称「由用户处理释放」。这是合同要求的 fail-closed 表现，断言改为「绑定新快照或如实说明暂不可比」。

## 四、NOT_VERIFIED 与后继

- **NOT_VERIFIED：Tauri 原生点击、真实 Finder 定位、受控 APFS/整盘扫描**——本卡按边界只做浏览器真实入口，父卡（144/145/146）继续开放，ISS-161 才可开始实机验收。
- **NOT_VERIFIED：生产扫描/用户 HOME/真实卷**——全部走隔离运行根 + 合成种子，重扫只作用于自建 `scanroot` 临时目录。
- **NOT_VERIFIED：8 套件在 CI 冷环境的首跑**——本地按既有口径全绿；`ci.yml` 计数门禁将在标准 CI run 以实测对齐。
- **NOT_VERIFIED：图表隐藏恢复只量了总览容量图**——各 tab 内图表（`#chart-grown`/`#chart-shrunk`/`#chart-sunburst`/`#chart-browser-trend`）的逐图恢复未逐项断言，只确认了「隐藏容器不塌成 0 宽」这一共用机制。
- **未复现 ⇒ 未修**：D1 之外，本卡 49 项未复现任何需要改生产代码的缺陷；因此 `frontend/modules/{state,router,request}.js`、五页 enter/leave、`directory-detail.js`、`fathom/` 语义**零改动**，共享 API 状态兼容修补**未动用**（无逐条列出项）。
- **后继**：D1 派回变化页责任卡；合并后解锁 ISS-161 实机验收。
